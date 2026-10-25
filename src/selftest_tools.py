"""Phase 9 工具层离线自测。

在**独立临时目录**里跑，绝不碰项目真实的 conversation/ memory/ 等。

## 要证明的硬要求（总项目指令 §30）

    工具调用必须：可记录 · 可追踪 · 有输入输出 · 有错误处理 · 有权限边界

自测按这五条逐项验。另外验 §10 的**解耦**要求：
`parse_calls` / `render_tools` 都是纯文本协议，不依赖任何平台的 function calling。

用法：
    python src\\selftest_tools.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_tools_"))

import paths  # noqa: E402

paths.ROOT = TEMP_ROOT
paths.CONFIG_DIR = TEMP_ROOT / "config"
paths.PROMPTS_DIR = paths.CONFIG_DIR / "prompts"
paths.SETTINGS_FILE = paths.CONFIG_DIR / "settings.json"
paths.PERSONALITY_FILE = TEMP_ROOT / "personality" / "personality.json"
paths.CONVERSATION_DIR = TEMP_ROOT / "conversation"
paths.DB_FILE = paths.CONVERSATION_DIR / "conversations.db"
paths.MEMORY_DIR = TEMP_ROOT / "memory"
paths.RAW_DIR = paths.MEMORY_DIR / "raw"
paths.RAW_EVENTS_DIR = paths.RAW_DIR / "events"
paths.VERSIONS_DIR = paths.MEMORY_DIR / "versions"
paths.CHANGELOG_DIR = paths.MEMORY_DIR / "changelog"
paths.RECALL_DIR = TEMP_ROOT / "recall"
paths.RELATIONSHIP_DIR = TEMP_ROOT / "relationship"
paths.USER_MODEL_DIR = TEMP_ROOT / "user_model"
paths.NOTES_DIR = TEMP_ROOT / "notes"
paths.EXPERIMENTS_DIR = TEMP_ROOT / "experiments"
paths.LOGS_DIR = TEMP_ROOT / "logs"
paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"

for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe", "relationship",
            "user_model", "events", "llm", "tools", "tools.base", "tools.registry",
            "tools.builtin", "tools.prompt"):
    sys.modules.pop(mod, None)

import storage  # noqa: E402
import tools as tools_mod  # noqa: E402
from config import load_settings  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def setup_files() -> None:
    paths.ensure_dirs()
    real = json.loads((SRC.parent / "config" / "settings.json").read_text(encoding="utf-8"))
    paths.SETTINGS_FILE.write_text(
        json.dumps({
            "model": "fake-chat", "temperature": 0.7, "max_tokens": 800,
            "history_turns": 10, "memory": {"top_k": 3},
            "max_tokens_by_purpose": {"chat": 800},
            "cost": {"models": {"fake-chat": {"cache_hit": 0.04, "input": 2.0, "output": 8.0}}},
            "raw_event_triggers": {"keywords": []},
            "relationship": real["relationship"],
            "user_model": real["user_model"],
            "heartbeat": real["heartbeat"],
            "tools": real["tools"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    paths.PERSONALITY_FILE.write_text(
        json.dumps({"name": "小满", "description": "测试人格",
                    "system_prompt": "你叫{name}。{description}"}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 造点可读的文件
    (TEMP_ROOT / "README.md").write_text("# 测试用\n\n这是给 read_file 读的。\n",
                                         encoding="utf-8")
    (TEMP_ROOT / "big.txt").write_text("x" * 500, encoding="utf-8")
    (TEMP_ROOT / ".env").write_text("SECRET_KEY=should-never-be-read\n", encoding="utf-8")
    (TEMP_ROOT / "sub").mkdir(exist_ok=True)
    (TEMP_ROOT / "sub" / "nested.md").write_text("嵌套文件\n", encoding="utf-8")


def main() -> int:
    print("=" * 68)
    print("Phase 9 · 工具层离线自测")
    print(f"隔离临时目录：{TEMP_ROOT}")
    print("=" * 68)

    setup_files()
    settings = load_settings()
    reg = tools_mod.build_registry(settings)
    conn = storage.connect()

    # ========================================================================
    print("\n" + "=" * 68)
    print("§30-① 权限边界")
    print("=" * 68)

    print("\n[T.1] 权限档决定工具是否可用")
    check("read 在白名单", "read" in reg.allowed_permissions, str(reg.allowed_permissions))
    check("execute 不在白名单（默认不开）",
          "execute" not in reg.allowed_permissions, str(reg.allowed_permissions))
    check("run_python 已注册但不可用",
          "run_python" in reg.names()
          and "run_python" not in [t.name for t in reg.enabled()], str(reg.names()))
    print(f"  注册：{reg.names()}")
    print(f"  可用：{[t.name for t in reg.enabled()]}（execute 未开，run_python 不给模型看到）")

    r = reg.call(conn, "run_python", {"code": "print(1)"})
    check("调用超出权限的工具被拒", not r.ok and r.error_kind == "denied", r.error_kind)
    print(f"  run_python → {r.error_kind}：{r.error[:40]}")

    print("\n[T.2] 路径越界被拒")
    for bad in ("../../etc/passwd", "../outside.md", "sub/../../escape.md"):
        r = reg.call(conn, "read_file", {"path": bad})
        check(f"越界路径 {bad} 被拒", not r.ok and r.error_kind == "denied",
              f"{r.error_kind}: {r.error[:30]}")
    r = reg.call(conn, "read_file", {"path": "sub/nested.md"})
    check("允许目录内的子路径正常", r.ok, r.error[:40])
    print("  3 条越界路径全部被拒；目录内子路径正常")

    print("\n[T.3] 敏感文件显式拒绝（不是靠扩展名偶然挡住）")
    r = reg.call(conn, "read_file", {"path": ".env"})
    check(".env 被拒", not r.ok and r.error_kind == "denied", r.error_kind)
    check("拒绝理由点明「敏感文件」而不是「扩展名不对」",
          "敏感" in r.error, r.error[:44])
    print(f"  .env → {r.error[:50]}")

    print("\n[T.3b] 敏感文件的**变体名**也必须被拒（独立审计攻破过）")
    # 实测漏洞：原实现是精确名匹配（`name in DENY_NAMES`），于是
    # `.env.bak` / `credentials.json` / `secrets.yaml` / `id_rsa.txt`
    # 全部读出明文 —— 21 个变体里 20 个绕过。
    # **`.env` 这类文件天生就靠变体名管理**，用精确名单堵变体等于没堵。
    variant_dir = TEMP_ROOT / "variants"
    variant_dir.mkdir(exist_ok=True)
    variants = [".env.txt", ".env.bak", ".env.json", ".env.local", ".env.production",
                "credentials.json", "credentials.yaml", "secrets.yaml", "secrets.txt",
                "id_rsa.txt", "id_ed25519.md", "api_key.txt", "token.txt",
                "password.txt", "private_key.pem"]
    for name in variants:
        (variant_dir / name).write_text(f"假密钥 {name}", encoding="utf-8")
    reg_v = tools_mod.build_registry(settings, root=TEMP_ROOT)
    leaked = []
    for name in variants:
        rv = reg_v.call(conn, "read_file", {"path": f"variants/{name}"})
        if rv.ok:
            leaked.append(name)
    check("全部 15 个敏感变体被拒", not leaked, f"泄漏了：{leaked}")
    # 正常文件不受影响（别修过头）
    (variant_dir / "config.json").write_text("普通配置", encoding="utf-8")
    rv = reg_v.call(conn, "read_file", {"path": "variants/config.json"})
    check("普通 config.json 仍可读", rv.ok, rv.error[:40])
    print(f"  15 个变体全被拒；普通文件仍可读")

    # ========================================================================
    print("\n" + "=" * 68)
    print("§30-② 输入输出 · 参数校验 · 错误处理")
    print("=" * 68)

    print("\n[T.4] 正常读取")
    r = reg.call(conn, "read_file", {"path": "README.md"})
    check("读取成功", r.ok and "测试用" in r.output, r.output[:30])
    r = reg.call(conn, "read_file", {"path": "README.md", "start_line": 3})
    check("start_line 生效", r.ok and "read_file" in r.output, r.output[:30])
    print(f"  README.md 读到 {len(r.output)} 字符")

    print("\n[T.5] 参数校验")
    r = reg.call(conn, "read_file", {})
    check("缺必填参数 → denied", not r.ok and r.error_kind == "denied", r.error_kind)
    r = reg.call(conn, "read_file", {"path": 123})
    check("参数类型不对 → denied", not r.ok and r.error_kind == "denied", r.error_kind)
    r = reg.call(conn, "take_note", {"text": ""})
    check("空笔记 → denied", not r.ok and r.error_kind == "denied", r.error_kind)
    r = reg.call(conn, "take_note", {"text": "x" * 2001})
    check("超长笔记 → denied", not r.ok and r.error_kind == "denied", r.error_kind)
    print("  缺参数 / 类型错 / 空值 / 超长 全部被拒")

    print("\n[T.5b] 拒绝理由必须**可操作**（真实事故：模型原地重试空参数）")
    # 实测事故：用户说「帮我记一条笔记：<完整文本>」，模型却发出空参数
    # `{"name":"take_note","arguments":{}}`，收到的拒绝只有「缺少必填参数：text」——
    # 没说该怎么调，于是它**原样重发同一个空参数**，第二次又被拒就放弃了。
    # 用户要记的笔记最终没进系统。
    reason = reg._tools["take_note"].validate({})
    check("拒绝理由含「缺少必填参数」", "缺少必填参数" in reason, reason[:30])
    check("拒绝理由给出可直接照抄的调用示例", "```tool" in reason, reason[:60])
    check("示例里参数名正确", '"text"' in reason, "")
    check("拒绝理由明确劝阻原样重发", "不要原样重发" in reason, "")
    r2 = reg._tools["read_file"].validate({})
    check("read_file 的拒绝同样可操作",
          "```tool" in r2 and '"path"' in r2, r2[:50])
    check("参数齐全时不拦",
          reg._tools["take_note"].validate({"text": "abc"}) == "", "")
    print("  拒绝理由可照抄重发；参数齐全时正常放行")

    print("\n[T.5c] 空参数但工具本身有参数定义 → 也要给示例")
    empty_reason = reg._tools["take_note"].validate({})
    check("空 arguments 被识别为参数问题", "缺少必填参数" in empty_reason, "")

    print("\n[T.6] 错误处理：工具出错不崩对话")
    r = reg.call(conn, "read_file", {"path": "不存在.md"})
    check("文件不存在 → error（不是 denied）",
          not r.ok and r.error_kind == "error", r.error_kind)
    r = reg.call(conn, "no_such_tool", {})
    check("工具不存在 → error", not r.ok and r.error_kind == "error", r.error_kind)

    # 故意让工具抛异常，验证注册表兜住
    def boom(_args):
        raise RuntimeError("工具内部炸了")

    reg.register(tools_mod.Tool(
        name="boom", description="故意抛异常", input_schema={"type": "object", "properties": {}},
        execute=boom, permission="read"))
    r = reg.call(conn, "boom", {})
    check("工具抛异常被兜住（不带崩对话）",
          not r.ok and r.error_kind == "error" and "炸了" in r.error, r.error[:40])
    print(f"  异常被兜住：{r.error[:40]}")

    # ========================================================================
    print("\n" + "=" * 68)
    print("§30-③ 可记录 · 可追踪")
    print("=" * 68)

    print("\n[T.7] 每次调用都落账，含被拒与失败")
    rows = storage.list_tool_calls(conn)
    check("账本有记录", len(rows) > 0, f"{len(rows)} 条")
    check("被拒的也记了", any(r["denied"] for r in rows), "")
    check("失败的也记了", any(not r["ok"] and not r["denied"] for r in rows), "")
    check("成功的也记了", any(r["ok"] for r in rows), "")
    check("每条都有耗时", all(r["duration_ms"] is not None for r in rows), "")
    check("每条都有输入（args）", all(r["args"] for r in rows), "")
    kinds = {r["error_kind"] for r in rows if r["error_kind"]}
    check("error_kind 区分了 denied 与 error", {"denied", "error"} <= kinds, str(kinds))
    print(f"  账本 {len(rows)} 条；error_kind 取值 {sorted(kinds)}")

    print("\n[T.8] 拒绝理由可追溯（能回答「她试图越界了吗」）")
    denied = [r for r in rows if r["denied"]]
    check("被拒记录带拒绝理由", all(r["denied_reason"] for r in denied), f"{len(denied)} 条")
    check("有一条是读 .env 被拒",
          any("敏感" in (r["denied_reason"] or "") for r in denied), "")
    print(f"  被拒 {len(denied)} 条，理由都可查")
    for r in denied[:2]:
        print(f"    {r['name']}: {r['denied_reason'][:44]}")

    # ========================================================================
    print("\n" + "=" * 68)
    print("§10 解耦：纯文本协议，不绑任何平台")
    print("=" * 68)

    print("\n[T.9] 解析模型输出（不依赖 function calling）")
    body = '我查一下。\n```tool\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```\n'
    calls = tools_mod.parse_calls(body)
    check("能从围栏块解析", len(calls) == 1 and calls[0]["name"] == "read_file", str(calls))
    check("参数解析正确", calls[0]["arguments"] == {"path": "README.md"}, str(calls[0]))
    check("strip_calls 去掉工具块只留正文",
          tools_mod.strip_calls(body).strip() == "我查一下。",
          repr(tools_mod.strip_calls(body)))
    print(f"  解析：{calls}")

    print("\n[T.10] 解析容错（坏块不能毁掉整轮）")
    check("空文本 → 空列表", tools_mod.parse_calls("") == [], "")
    check("普通回复 → 空列表", tools_mod.parse_calls("今天天气不错") == [], "")
    check("坏 JSON → 跳过而不是抛异常",
          tools_mod.parse_calls('```tool\n{not json}\n```') == [], "")
    check("缺 name → 跳过",
          tools_mod.parse_calls('```tool\n{"arguments": {}}\n```') == [], "")
    check("arguments 类型错 → 退化为空 dict",
          tools_mod.parse_calls(
              '```tool\n{"name": "read_file", "arguments": "xxx"}\n```'
          )[0]["arguments"] == {}, "")
    multi = ('```tool\n{"name":"a","arguments":{}}\n```\n'
             '```tool\n{"name":"b","arguments":{}}\n```')
    check("多个围栏块都能解析",
          [c["name"] for c in tools_mod.parse_calls(multi)] == ["a", "b"], "")
    print("  空/普通/坏JSON/缺name/类型错/多块 —— 6 种情况都正确")

    print("\n[T.10b] 独立审计攻破过的四类解析缺陷")
    # 🔴 深嵌套 JSON 让 json.loads 抛 RecursionError，原异常元组不含它 → 打崩整轮
    deep = "[" * 3000 + "]" * 3000        # 仅约 6KB
    try:
        tools_mod.parse_calls('```tool\n{"name":"read_file","arguments":{"path":' + deep + '}}\n```')
        check("深嵌套负载不抛 RecursionError", True, "")
    except RecursionError:
        check("深嵌套负载不抛 RecursionError", False, "仍抛 RecursionError")
    except Exception as exc:
        check("深嵌套负载不抛异常", False, type(exc).__name__)

    # 🟠 一个围栏块里放两个对象 → 原实现整块当一个 JSON，全丢
    two = ('```tool\n{"name":"read_file","arguments":{"path":"a.md"}}\n'
           '{"name":"read_file","arguments":{"path":"b.md"}}\n```')
    parsed_two = tools_mod.parse_calls(two)
    check("一个块里两个对象都能解析", len(parsed_two) == 2,
          str([c["arguments"] for c in parsed_two]))

    # 🟠 坏围栏块连带吃掉后面的合法裸对象
    mix = ('```tool\n{坏JSON}\n```\n'
           '然后 {"name":"read_file","arguments":{"path":"ok.md"}}')
    parsed_mix = tools_mod.parse_calls(mix)
    check("坏围栏不吃掉后面的裸对象",
          len(parsed_mix) == 1 and parsed_mix[0]["arguments"]["path"] == "ok.md",
          str(parsed_mix))

    # 🟠 正文里的 JSON 举例被从用户可见回复里删掉
    prose = '正文 {"name":"read_file","arguments":{"path":"a.md"}} 还在'
    stripped = tools_mod.strip_calls(prose)
    check("正文里的裸对象不再被删掉（用户能看懂自己的对话）",
          "正文" in stripped and "还在" in stripped, repr(stripped))
    print("  深嵌套 / 一块多对象 / 坏围栏 / 正文误删 —— 四类全部修正")

    print("\n[T.11] 工具提示渲染")
    text = tools_mod.prompt.render_tools(reg.enabled())
    check("提示里列出了可用工具", "read_file" in text and "take_note" in text, "")
    check("提示里不含被权限挡住的工具", "run_python" not in text, "")
    check("提示里说明了调用格式", "```tool" in text, "")
    check("无可用工具时返回空串", tools_mod.prompt.render_tools([]) == "", "")
    print(f"  提示 {len(text)} 字符，含调用格式说明")

    # ========================================================================
    print("\n" + "=" * 68)
    print("写入类工具：take_note（唯一与项目本体耦合的工具）")
    print("=" * 68)

    print("\n[T.12] 记笔记 → 文件 + 原始事件层")
    before_ev = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    r = reg.call(conn, "take_note", {"text": "用户说下周三要去一趟花市，想看看有没有茉莉。"})
    check("记笔记成功", r.ok, r.error[:40])
    notes = list(paths.NOTES_DIR.glob("note_*.md"))
    check("笔记文件已落盘", len(notes) == 1, f"{len(notes)} 个")
    check("笔记内容含原文",
          notes and "下周三" in notes[0].read_text(encoding="utf-8"), "")
    after_ev = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    check("同时进入原始事件层（可被后续检索召回）", after_ev > before_ev,
          f"{before_ev} → {after_ev}")
    check("返回里说明了已进事件层", "原始事件层" in r.output, r.output[:60])
    # 说出的事要准确：这里只是进了原始事件层，真正进 Memory 还要 consolidate
    check("返回没有夸大成「已进入长期记忆」",
          "长期记忆" not in r.output or "后续" in r.output, r.output[:70])
    print(f"  {r.output}")

    print("\n[T.12b] 同一秒连续记两条不能互相覆盖（真实测试踩到的 bug）")
    # 实测事故：文件名原来用 `note_YYYYMMDD_HHMMSS.md`，同一秒内记两条
    # → **第二条覆盖第一条**：2 条原始事件都在，磁盘上却只剩 1 个文件。
    # 现在改用 event_id 命名，天然一一对应。
    before_files = set(p.name for p in paths.NOTES_DIR.glob("note_*.md"))
    before_n = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    outs = []
    for txt in ("同秒测试甲：去买茉莉土", "同秒测试乙：记得带伞"):
        res = reg.call(conn, "take_note", {"text": txt}, turn_id="same_second")
        outs.append(res.output)
        time.sleep(0.02)          # 远小于 1 秒，确保落在同一秒
    new_files = sorted(set(p.name for p in paths.NOTES_DIR.glob("note_*.md")) - before_files)
    after_n = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    check("同秒两条产生 2 个文件（不覆盖）", len(new_files) == 2, f"{len(new_files)} 个：{new_files}")
    check("同秒两条产生 2 条原始事件", after_n - before_n == 2, f"{before_n} → {after_n}")
    bodies = [ (paths.NOTES_DIR / f).read_text(encoding="utf-8") for f in new_files ]
    check("两个文件内容各不相同（没有谁被覆盖）",
          sum("同秒测试甲" in b for b in bodies) == 1
          and sum("同秒测试乙" in b for b in bodies) == 1,
          " / ".join("甲" if "甲" in b else "乙" for b in bodies))
    import re as _re
    eids = _re.findall(r"（(raw_event_[0-9a-f]+)）", " ".join(outs))
    check("返回里报出了各自的 event_id（两个不同）",
          len(eids) == 2 and len(set(eids)) == 2, str(eids))
    check("文件名与 event_id 一一对应",
          sorted(f"note_{e}.md" for e in eids) == new_files,
          f"events={eids} files={new_files}")
    print(f"  同秒两条 → {len(new_files)} 个文件 / {after_n - before_n} 条事件，内容互不覆盖")

    print("\n[T.13] execute 打开后 run_python 才可用（改配置即可，不改代码）")
    cfg = dict(settings.tools)
    cfg["allowed_permissions"] = ["read", "write", "execute"]
    settings.tools = cfg
    reg2 = tools_mod.build_registry(settings)
    check("放开 execute 后 run_python 可用",
          "run_python" in [t.name for t in reg2.enabled()], str([t.name for t in reg2.enabled()]))
    r = reg2.call(conn, "run_python", {"code": "print('hello from python')"})
    check("run_python 正常执行", r.ok and "hello from python" in r.output, r.output[:40])
    r = reg2.call(conn, "run_python", {"code": "import sys; sys.stderr.write('oops'); sys.exit(3)"})
    check("非零退出码 → error 且带回 stderr",
          not r.ok and r.error_kind == "error" and "oops" in r.error, r.error[:50])
    r = reg2.call(conn, "run_python", {"code": "import time; time.sleep(30)"})
    check("超时 → timeout（与 error 区分开）",
          not r.ok and r.error_kind == "timeout", r.error_kind)
    print(f"  execute 打开后：正常执行 ✅ / 非零退出 ✅ / 超时 {r.error_kind} ✅")

    print("\n[T.14] 输出截断是显式标注的，不静默")
    r = reg2.call(conn, "run_python", {"code": "print('y' * 20000)"})
    check("超长输出被截断", r.ok and len(r.output) < 20000, f"{len(r.output)} 字符")
    check("截断有明确标注", "截断" in r.output, r.output[-40:])
    print(f"  输出 {len(r.output)} 字符，末尾标注：{r.output[-30:]!r}")

    print("\n[T.15] 回灌额度必须与工具宣称的额度对齐（真实测试踩到的 bug）")
    # 实测事故：read_file 宣称能读 200,000 字节，但 render() 默认只放 2,000 字符。
    # README 才 7,335 字符就被砍掉 73% —— 用户问「本项目用什么模型」侥幸答对，
    # 因为答案恰好在文件开头（第 15 行）；问靠后内容只会得到「没看到」。
    r = reg.call(conn, "read_file", {"path": "big.txt"})
    check("小文件完整回灌（不被砍）", r.render() == r.output,
          f"output={len(r.output)} render={len(r.render())}")
    check("render 上限不低于 read_file 宣称的读取能力",
          tools_mod.base.DEFAULT_RENDER_CHARS >= 10_000,
          f"DEFAULT_RENDER_CHARS={tools_mod.base.DEFAULT_RENDER_CHARS}")

    # 大文件仍必须明确标注截断（不能静默）
    huge = tools_mod.ToolResult(True, output="字" * (tools_mod.base.DEFAULT_RENDER_CHARS + 5000))
    out = huge.render()
    check("超大输出被截断", len(out) < len(huge.output), f"{len(huge.output)} → {len(out)}")
    check("截断标注在末尾且完整可见（不被自己的切片吃掉）",
          out.endswith("字符）") and "截断" in out, out[-30:])
    print(f"  小文件完整回灌；超大输出截断到 {len(out):,} 字符且标注可见")

    print("\n[T.16] 权限检查必须下沉到 execute（独立审计攻破过）")
    # 实测：权限检查原本只在 ToolRegistry.call() 里。绕过 call 直接
    # `reg._tools["run_python"].execute({"code": ...})` **能真的跑本机代码**。
    # 一个咽喉被绕过就够了，所以把校验也装到 execute 外面。
    bypass = reg._tools["run_python"].execute({"code": "print('逃逸')"})
    check("直接调 execute 也被权限拦住",
          not bypass.ok and bypass.error_kind == "denied", bypass.error[:50])
    empty_args = reg._tools["take_note"].execute({})
    check("直接调 execute 也被参数校验拦住",
          not empty_args.ok and empty_args.error_kind == "denied", empty_args.error[:40])
    ok_direct = reg._tools["read_file"].execute({"path": "README.md"})
    check("有权限的工具直接调 execute 仍正常", ok_direct.ok, ok_direct.error[:40])
    print("  execute 路径已被同样约束；正常工具不受影响")

    conn.close()

    # --- 汇总 ---
    print("\n" + "=" * 68)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 68)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本次自测完全离线，未产生任何真实 API 费用。")

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    print(f"已清理临时目录：{TEMP_ROOT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
