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
import subprocess
import sys
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
