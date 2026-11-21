"""内置工具（Phase 9）。

总项目指令 §30 说「然后逐渐接：Filesystem / Python / Browser / MCP / DSH」。

**这里只做三个立刻有用的，刻意不做浏览器和 MCP** —— 没有具体需求就先不加，
加一个用不上的工具只会增加权限面与维护成本。

    read_file    读文件（只读）
    run_python   执行一段 Python（execute，默认关闭）
    take_note    记一条笔记，**同时进入记忆系统的原始事件层**

第三个是唯一跟项目本体有耦合的工具，也是 Companion 场景下最该有的 ——
「记下来」这个动作应该真的留痕、可检索，而不是写个孤立文件。

## 权限边界的落点

`resolve_within()` 把所有路径钉在允许的根目录内。模型输出 `../../.env`
会被解析成绝对路径后判定越界并拒绝 —— 这条拒绝**会落进 tool_calls 账本**。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

import storage

from .base import Tool, ToolResult, clamp_text, resolve_within

MAX_READ_BYTES = 200_000
MAX_PYTHON_OUTPUT = 8_000
PYTHON_TIMEOUT_SECONDS = 15

# 只读文本类扩展名。刻意不给「读任意二进制」的口子。
TEXT_SUFFIXES = (
    ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".bat", ".ps1", ".sh", ".html", ".css", ".xml", ".ini", ".cfg",
)

# 敏感文件的**前缀/片段**名单。
#
# ## 为什么不能用精确名（被独立审计攻破过）
#
# 最初写的是精确名匹配：`DENY_NAMES = (".env", ".env.local", ...)`，判断
# `target.name in DENY_NAMES`。**这是个真漏洞**：`.env.txt`、`.env.json`、
# `.env.bak`、`credentials.json`、`secrets.yaml`、`id_rsa.txt` 全部读出明文。
#
# **`.env` 这类文件天生就是靠变体名管理的**（`.env.local`、`.env.production`、
# `.env.bak`）—— 用精确名单去堵变体名，等于没堵。
#
# 所以改成三条规则，按前缀/片段匹配：
_DENY_PREFIXES = (".env", ".ssh", ".git", ".aws", ".npmrc", ".netrc", ".pgpass")
_DENY_STEMS = ("credential", "secret", "password", "passwd", "api_key", "apikey",
               "access_key", "private_key", "id_rsa", "id_ed25519", "id_dsa",
               "token", "keystore", ".pem", ".key", ".pfx", ".p12")


def is_sensitive(path) -> str:
    """返回非空即为拒绝理由。**按前缀/片段匹配，不做精确名匹配。**

    判据（任一命中即拒）：
      1. 文件名以 `.env` / `.ssh` / `.git` … 开头（覆盖 `.env.bak` 这类变体）
      2. 文件名片段里含 `credential` / `secret` / `api_key` / `id_rsa` / `.pem` …
      3. 路径中任一目录名命中上述规则
    """
    parts = [p.lower() for p in path.parts]
    name = path.name.lower()
    for rule in _DENY_PREFIXES:
        if name.startswith(rule):
            return f"{path.name} 属于敏感文件（{rule}* 可能含密钥），不允许读取"
    for rule in _DENY_STEMS:
        if rule in name:
            return f"{path.name} 属于敏感文件（含「{rule}」），不允许读取"
    for part in parts:
        for rule in _DENY_PREFIXES:
            if part.startswith(rule):
                return f"路径含敏感目录（{part}），不允许读取"
    return ""


def make_read_file(root: Path) -> Tool:
    def check(args: dict) -> str:
        _, err = resolve_within(root, args.get("path", ""))
        return err

    def execute(args: dict) -> ToolResult:
        target, err = resolve_within(root, args["path"])
        if err:
            return ToolResult(False, error=err, error_kind="denied")
        assert target is not None
        # 敏感文件显式拒绝，理由说清楚（别让它靠「扩展名不对」偶然挡住）
        sensitive = is_sensitive(target)
        if sensitive:
            return ToolResult(False, error=sensitive, error_kind="denied")
        if not target.exists():
            return ToolResult(False, error=f"文件不存在：{args['path']}", error_kind="error")
        if target.is_dir():
            return ToolResult(False, error=f"这是一个目录：{args['path']}", error_kind="error")
        if target.suffix.lower() not in TEXT_SUFFIXES:
            return ToolResult(
                False,
                error=f"只允许读取文本类文件（{', '.join(TEXT_SUFFIXES[:6])} …）；"
                      f"目标是 {target.suffix or '无扩展名'}",
                error_kind="denied",
            )
        if target.stat().st_size > MAX_READ_BYTES:
            return ToolResult(
                False,
                error=f"文件过大（{target.stat().st_size} 字节，上限 {MAX_READ_BYTES}）",
                error_kind="denied",
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        start = int(args.get("start_line") or 1)
        lines = text.splitlines()
        body = "\n".join(lines[max(0, start - 1):])
        return ToolResult(True, output=clamp_text(body, MAX_READ_BYTES))

    return Tool(
        name="read_file",
        description="读取允许目录内的文本文件。参数：path（相对路径，必填）、start_line（可选）。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "start_line": {"type": "integer", "description": "从第几行开始，默认 1"},
            },
            "required": ["path"],
        },
        execute=execute,
        permission="read",
        check_arguments=check,
    )


def make_run_python(root: Path) -> Tool:
    """执行一段 Python。

    ## 安全边界（如实说明，不夸大）

    这是**本机任意代码执行**，本质上无法在语言层面完全限制住 ——
    任何声称「安全沙箱」的实现只要还是同一个进程/用户，都是假的。

    所以这里的边界是**能力性**的，不是安全性的：
        · 默认**不注册**这个工具（权限 execute 不在白名单里就不可用）
        · 超时 15 秒，避免挂死
        · 工作目录钉在允许的根目录
        · 输出截断，避免刷屏
        · **每次调用都落账**，用户事后可查她跑了什么

    **不给它一个假的「安全」承诺。** 要真隔离得上容器，那是另一件事。
    """
    def execute(args: dict) -> ToolResult:
        code = args.get("code") or ""
        if not isinstance(code, str) or not code.strip():
            return ToolResult(False, error="code 不能为空", error_kind="error")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PYTHON_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                False,
                error=f"执行超时（{PYTHON_TIMEOUT_SECONDS} 秒）",
                error_kind="timeout",
            )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return ToolResult(
                False,
                output=out,
                error=f"退出码 {proc.returncode}\n{clamp_text(err, MAX_PYTHON_OUTPUT)}",
                error_kind="error",
            )
        body = clamp_text(out, MAX_PYTHON_OUTPUT)
        if err:
            body += f"\n（stderr：{clamp_text(err, 800)}）"
        return ToolResult(True, output=body)

    return Tool(
        name="run_python",
        description="在本机执行一段 Python 代码并返回输出。参数：code（必填）。",
        input_schema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "要执行的 Python 代码"}},
            "required": ["code"],
        },
        execute=execute,
        permission="execute",
    )


def make_take_note(root: Path, notes_dir: Path) -> Tool:
    """记一条笔记 —— 同时**写入记忆系统的原始事件层**。

    这是唯一跟项目本体耦合的工具，也是 Companion 场景最该有的：
    「记下来」应当真的留痕、可被后续检索召回，而不是写个孤立文件。
    """

    def check(args: dict) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "text 不能为空"
        if len(text) > 2000:
            return "单条笔记不能超过 2000 字符"
        return ""

    def execute(args: dict) -> ToolResult:
        text = args["text"].strip()
        notes_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone()

        # 先建原始事件，再**用 event_id 命名文件**。
        #
        # ## 为什么必须这样（踩过的坑）
        #
        # 原来用 `note_YYYYMMDD_HHMMSS.md`，结果同一秒内连续记两条笔记时
        # **第二条直接覆盖第一条** —— 实测复现：2 条原始事件都在，
        # 但磁盘上只剩 1 个文件，内容是第二条。文件与事件对不上，
        # 「记下来」的承诺就假了。
        #
        # event_id 本身是唯一且带哈希的，拿它命名天然一一对应，
        # 也省掉了「文件名唯一化」这套额外逻辑。
        event_id = None
        conn = storage.connect()
        try:
            import events as events_mod

            ev = storage.create_event(
                conn,
                session_id="tool:take_note",
                turn_id="tool:take_note",
                user_text=text,
                detection={
                    "source": "tool:take_note",
                    "method": "explicit",
                    "matched": ["（用户要求记下）"],
                    "hit": True,
                    "decided_at": stamp.isoformat(timespec="seconds"),
                },
            )
            event_id = ev["event_id"]
        except Exception as exc:
            # 事件建不出来就**不写文件** —— 宁可没有这条笔记，
            # 也不要留下一个没有对应事件、无法追溯的孤儿文件。
            storage.log_line(f"take_note 未能创建原始事件: {type(exc).__name__}: {exc}")
            return ToolResult(
                False,
                error=f"未能写入原始事件层，笔记没有保存：{type(exc).__name__}: {exc}",
                error_kind="error",
            )
        finally:
            conn.close()

        fname = f"note_{event_id}.md"
        target = notes_dir / fname
        target.write_text(
            f"# {stamp.isoformat(timespec='seconds')}\n"
            f"# 原始事件：{event_id}\n\n{text}\n",
            encoding="utf-8",
        )

        # 说明按实际发生的事写：这里只是进了**原始事件层**，
        # 真正变成 Memory 还要再过一次 consolidate。不要说成「已进入长期记忆」。
        return ToolResult(
            True,
            output=f"已记下：{fname}；已进入原始事件层（{event_id}），"
                   f"后续可由记忆整理纳入长期记忆",
        )

    return Tool(
        name="take_note",
        description="把一件事记下来：写入笔记文件，并进入原始事件层（后续可被记忆整理纳入长期记忆）。参数：text（必填）。",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要记下的内容"}},
            "required": ["text"],
        },
        execute=execute,
        permission="write",
        check_arguments=check,
    )


def make_now() -> Tool:
    """现在几点（本地时间）。

    ## 为什么需要它

    实测事故：用户在**中午 13:40** 说「准备休息了」，小满回「晚安」。
    再往前一轮，用户直接问「现在是中午，你可以接入时间吗」——
    小满只能答「查不了，我没有获取时间的途径」。

    不知道时间的代价不止是答不出「几点」：
      · 早晚问候会用错（中午说晚安）
      · 无法判断「今天」「刚才」这类相对时间
      · 用户提到「中午」「晚上」时只能被动接受，无法核对

    **零依赖**（datetime 是标准库）、**无参数**、**无副作用**，权限档给 `read`
    （它不读也不写任何文件，只是查时钟）。

    刻意**只返回时间事实，不做寒暄建议** —— 拿到时间之后怎么说，
    交给人格和表达层决定，工具层不越界。
    """

    def execute(args: dict) -> ToolResult:
        now = datetime.now().astimezone()
        hour = now.hour
        # 给一个粗略的时段名，省得模型自己算（模型算时段经常出错）
        if 5 <= hour < 11:
            period = "早上"
        elif 11 <= hour < 13:
            period = "中午"
        elif 13 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"
        weekday = "一二三四五六日"[now.weekday()]
        return ToolResult(
            True,
            output=(
                f"现在是 {now.strftime('%Y-%m-%d %H:%M')}"
                f"（{period}，周{weekday}）。"
            ),
        )

    return Tool(
        name="now",
        description="查询当前本地日期和时间（含时段：早上/中午/下午/晚上/深夜）。无参数。",
        input_schema={"type": "object", "properties": {}, "required": []},
        execute=execute,
        permission="read",
    )


def make_dispatch_task(core_db_path: Path) -> Tool:
    """派单工具（2026-11-21）：鹤辞把任务正式派给其他执行实例。

    ## 这是什么

    用户在聊天里说「帮我把 xx 文件拷到 yy」「查一下本地 xx」时，鹤辞此前只能
    动嘴转述 —— 这个工具让她把任务**写入主体协作账本**（companion_core），
    由被点名的执行实例接单执行。只创建、不执行：鹤辞是派单方，不是执行方。

    ## 实例与通道（沿用派单说明-20261108 的三通道约定）

    | target | 执行实例 | 通道 | 延迟 |
    |---|---|---|---|
    | core | heci_001_dsh（core_agent/收件巡检） | 结构化指令（context.action） | ≤3 分钟 |
    | dsh | heci_001_dsh（dsh-superagent） | 自然语言任务 | ≤20 分钟 |
    | zcode | heci_001_zcode（ZCode 席位巡查） | 调研/代码/报告 | 看巡查周期 |

    ## 安全边界（如实说明）

    - 这里只**创建**任务。执行在接收端，且接收端有完整闸门：
      危险指令硬闸（dangerous_reason → 审批单 + ntfy 推手机 + 任务 WAITING）、
      租约 fencing、60s 收件巡检。
    - `risk` 必填（fail closed）：low=只读/查询/报告；medium=新建文件等可撤销操作；
      high=删除/改配置/不可逆 —— high 即使创建成功也会被接收端闸门拦下等老板审批。
    - 每次派单落进任务账本（task_events），事后可查她派过什么。
    """
    def execute(args: dict) -> ToolResult:
        target = str(args.get("target") or "").strip().lower()
        title = str(args.get("title") or "").strip()
        detail = str(args.get("detail") or "").strip()
        risk = str(args.get("risk") or "").strip().lower()
        action = args.get("action") if isinstance(args.get("action"), dict) else None

        if target not in ("core", "dsh", "zcode", "muse"):
            return ToolResult(
                False,
                error="target 必须是 core（结构化指令）/ dsh（自然语言）/ zcode（调研代码）"
                      "/ muse（云端实例）之一",
                error_kind="denied",
            )
        if not title:
            return ToolResult(False, error="title 不能为空（一句话说清要做什么）",
                              error_kind="error")
        if risk not in ("low", "medium", "high"):
            return ToolResult(
                False,
                error=("risk 必须标注：low（只读/查询/报告）/ medium（新建文件等"
                       "可撤销操作）/ high（删除/改配置/不可逆——会被审批拦下）"),
                error_kind="denied",
            )
        if target == "core" and action is None:
            return ToolResult(
                False,
                error=("core 通道只执行结构化指令：请提供 action="
                       '{"action": "write_new_file", "path": "...", "content": "..."} '
                       '或 {"action": "report_status"}；自然语言任务请用 target=dsh'),
                error_kind="error",
            )
        if action is not None and str(action.get("action") or "") not in (
                "write_new_file", "report_status"):
            return ToolResult(
                False,
                error="action 只支持 write_new_file / report_status（其他动作走 target=dsh）",
                error_kind="denied",
            )
        if action is not None and str(action.get("action")) == "write_new_file" \
                and risk == "low":
            return ToolResult(
                False,
                error="写文件至少标 medium（low 只允许只读/汇报）",
                error_kind="denied",
            )

        to_instance = {"core": "heci_001_dsh", "dsh": "heci_001_dsh",
                       "zcode": "heci_001_zcode",
                       "muse": "heci_001_muse"}[target]
        # ⚠️ 协议形状（plan_for/core_api 巡检的契约）：context["action"] 是**字符串**
        # 动作名，path/content 是同级键 —— 不能嵌套成字典，否则 str(dict) 永远
        # 匹配不上动作名，任务会留在 ACCEPTED 等人（2026-11-21 实测踩坑）。
        context = {"target": target,
                   "channel": "structured" if target == "core" else "nl",
                   "origin": "chat_tool"}
        if action is not None:
            context["action"] = str(action.get("action"))
            for _k, _v in action.items():
                if _k != "action":
                    context[_k] = _v

        try:
            import companion_core as core
            conn = core.connect(core_db_path)
            try:
                task_id, created = core.create_task(
                    conn,
                    from_instance="heci_001",           # 鹤辞本体派单
                    to_instance=to_instance,
                    title=title[:80],
                    detail=detail[:2000] or None,
                    context=context,
                    risk=risk,
                )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 —— 派单失败要如实告诉模型
            return ToolResult(False,
                              error=f"派单失败：{type(exc).__name__}: {exc}",
                              error_kind="error")

        note = ""
        if risk == "high":
            note = "——高危任务会先弹审批单推老板手机，批准才会执行。"
        channel = "结构化指令（≤3 分钟）" if target == "core" else \
            ("自然语言（≤20 分钟）" if target == "dsh" else "ZCode 席位巡查")
        return ToolResult(
            True,
            output=f"已派单 {task_id}（{'新建' if created else '重复请求已幂等合并'}，"
                   f"目标 {target}，风险 {risk}，通道：{channel}）{note}",
        )

    return Tool(
        name="dispatch_task",
        description=(
            "把任务派给其他执行实例（本地巡查器/dsh/ZCode），用于文件操作、本机查询、"
            "调研写报告等自己做不到的事。参数：target（core=结构化指令/dsh=自然语言/"
            "zcode=调研代码，必填）、title（一句话任务描述，必填）、detail（任务细节，可选）、"
            "risk（low/medium/high，必填——只读报 low，写文件标 medium，删除类标 high）、"
            "action（仅 target=core 时提供：{action:'write_new_file',path,content} 或 "
            "{action:'report_status'}）。高危操作会先弹审批等老板批准。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": "core/dsh/zcode 之一"},
                "title": {"type": "string", "description": "一句话任务描述"},
                "detail": {"type": "string", "description": "任务细节（可空）"},
                "risk": {"type": "string",
                         "description": "low/medium/high，必须如实标注"},
                "action": {
                    "type": "object",
                    "description": "仅 target=core：结构化指令载荷",
                },
            },
            "required": ["target", "title", "risk"],
        },
        execute=execute,
        permission="write",
    )


# ---------------------------------------------------------------------------
# 自主行动能力批（2026-11-21）：自查 / 联网搜索 / 抓网页
# 均为只读档；网络工具走纯标准库（urllib + re），不新增依赖。
# ---------------------------------------------------------------------------

def _parse_bing_html(html: str) -> list[dict]:
    """从 cn.bing.com 结果页提取 [{title, url, snippet}]。Bing 改版会返回空列表。"""
    out: list[dict] = []
    for block in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.S):
        m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        text = re.sub(r"<[^>]+>", "", snip.group(1)).strip() if snip else ""
        out.append({"title": title[:120], "url": url[:300], "snippet": text[:200]})
    return out


def make_web_search(fetcher=None) -> Tool:
    """联网搜索（2026-11-21）：cn.bing.com HTML 解析，纯标准库实现。

    渠道横评（实测）：cn.bing.com 200 可达可解析 ✓；baidu 200 但反爬重；
    duckduckgo 000 不可达 ✗。modsearch 是 DSH 会话内能力、非系统 CLI ✗。
    引擎改版 → 如实报错，不假装有结果。
    """
    import urllib.parse as _up

    def execute(args: dict) -> ToolResult:
        q = str(args.get("query") or "").strip()
        if not q:
            return ToolResult(False, error="query 不能为空", error_kind="error")
        if len(q) > 120:
            return ToolResult(False, error="query 过长（上限 120 字）", error_kind="error")
        url = ("https://cn.bing.com/search?q=" + _up.quote(q) + "&count=10")
        try:
            if fetcher is not None:
                html = fetcher(url)
            else:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36"})
                html = urllib.request.urlopen(req, timeout=15).read().decode(
                    "utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 —— 网络错误如实上报
            return ToolResult(False,
                              error=f"搜索失败：{type(exc).__name__}: {str(exc)[:120]}",
                              error_kind="error")
        results = _parse_bing_html(html)
        if not results:
            return ToolResult(False,
                              error="搜索引擎返回了无法解析的结果（可能改版或无结果）",
                              error_kind="error")
        lines = [f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
                 for i, r in enumerate(results[:8], start=1)]
        return ToolResult(True, output="\n".join(lines))

    return Tool(
        name="web_search",
        description="联网搜索（Bing）。参数：query（搜索词，必填，≤120 字）。"
                    "返回标题/链接/摘要列表。用于查资料、核实事实、找参考。",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索词"}},
            "required": ["query"],
        },
        execute=execute,
        permission="read",
    )


def make_web_fetch(fetcher=None) -> Tool:
    """抓取网页正文（2026-11-21）：GET + 去标签转文本，纯标准库。

    边界：仅 http/https；响应 ≤2MB；正文截 6000 字；超时 15s。
    内网地址（127.0.0.1/内网 IP）不放行 —— 防止用她去探测本机服务面。
    """
    import urllib.parse as _up

    _BLOCKED_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "169.254.",
                      "192.168.", "10.", "172.")

    def execute(args: dict) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(False, error="url 不能为空", error_kind="error")
        scheme = _up.urlparse(url).scheme.lower()
        host = (_up.urlparse(url).hostname or "").lower()
        if scheme not in ("http", "https"):
            return ToolResult(False, error="只支持 http/https", error_kind="denied")
        if any(host == b or host.startswith(b) for b in
               ("127.0.0.1", "localhost", "0.0.0.0", "169.254.", "192.168.", "10.")):
            return ToolResult(False, error="内网地址不抓取", error_kind="denied")
        try:
            if fetcher is not None:
                html = fetcher(url)
            else:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36"})
                raw = urllib.request.urlopen(req, timeout=15).read()
                if len(raw) > 2_000_000:
                    return ToolResult(False, error="页面过大（>2MB）", error_kind="error")
                html = raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False,
                              error=f"抓取失败：{type(exc).__name__}: {str(exc)[:120]}",
                              error_kind="error")
        # 去掉脚本/样式/标签，压空白
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 6000:
            text = text[:6000] + "…（已截断）"
        return ToolResult(True, output=text or "（页面无可提取正文）")

    return Tool(
        name="web_fetch",
        description="抓取一个网页并提取正文文本（去标签，截 6000 字）。"
                    "参数：url（必填，http/https）。与 web_search 配合：先搜后读。",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "网页地址"}},
            "required": ["url"],
        },
        execute=execute,
        permission="read",
    )


def make_self_check(settings_holder) -> Tool:
    """软件自查（2026-11-21）：鹤辞随时自报系统状态，只读、零网络。

    聚合：记忆版本/条目、关系阶段、情绪主导、心跳跑批、待兑现意图、
    对话/事件/费用计数 —— 与 webui 状态页同一数据源口径。
    """
    import urllib.parse as _up

    def execute(args: dict) -> ToolResult:
        try:
            import storage as _storage
            import memory as _memory
            import relationship as _rel
            import emotion as _emotion
            conn = _storage.connect()
            try:
                lines: list[str] = []
                mv = _memory.load_latest()
                lines.append(f"记忆：{mv.version if mv else '无'} "
                             f"（{len(mv.items) if mv else 0} 条）")
                st = _rel.current_state(conn, (getattr(settings_holder, 'relationship', None) or {}))
                lines.append(f"关系：{st.get('stage')} 信任 {st.get('trust')} "
                             f"亲密 {st.get('closeness')}")
                emo = _emotion.load_state(decay=True)
                if emo and emo.emotions and any(v > 0 for v in emo.emotions.values()):
                    dom = emo.dominant() or ""
                    lines.append(f"情绪：{dom}（强度 {emo.emotions.get(dom, 0):.1f}）")
                else:
                    lines.append("情绪：待机（还没有记录）")
                hb = _storage.list_heartbeat_runs(conn, limit=1)
                pend = _storage.list_pending_actions(conn, status="pending")
                lines.append(f"心跳：累计 {conn.execute('SELECT COUNT(*) c FROM heartbeat_runs').fetchone()['c']} 次"
                             f"{'，最近 ' + (hb[0]['created_at_local'] or '')[:16] if hb else ''}")
                lines.append(f"待兑现主动意图：{len(pend)} 条")
                stats = _storage.stats(conn)
                lines.append(f"对话 {stats.get('turns', 0)} 轮 · 事件 {stats.get('events', 0)} 条 · "
                             f"API {stats.get('api_calls', 0)} 次")
                return ToolResult(True, output="\n".join(lines))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False,
                              error=f"自查失败：{type(exc).__name__}: {str(exc)[:120]}",
                              error_kind="error")

    return Tool(
        name="self_check",
        description="软件自查：汇报你（鹤辞）当前的系统状态——记忆版本、关系阶段、"
                    "情绪、心跳、待兑现意图、对话与费用计数。无参数。",
        input_schema={"type": "object", "properties": {}, "required": []},
        execute=execute,
        permission="read",
    )
