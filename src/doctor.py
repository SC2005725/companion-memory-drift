"""环境自检（doctor）。

迁移到新电脑后先跑这个：能立刻回答「缺什么、通不通、能不能写」，
不需要真的发一次 API 请求就能把大部分问题挡在前面。

用法：
    python src\\main.py doctor            离线检查（不花钱、不需要 Key 也能跑大部分）
    python src\\main.py doctor --online   额外发一次最小请求，确认 Key / 模型 / 网络可用
"""

from __future__ import annotations

import platform
import sys

import storage
from config import ConfigError, load_env, load_settings, resolve_model
from paths import (
    CONVERSATION_DIR,
    DB_FILE,
    ENV_EXAMPLE_FILE,
    ENV_FILE,
    LOGS_DIR,
    PERSONALITY_FILE,
    ROOT,
    SETTINGS_FILE,
    display,
    ensure_dirs,
)
from persona import PersonaError, load_persona


def run(online: bool = False) -> int:
    ok = True
    lines: list[str] = []

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "OK  " if good else "问题"
        lines.append(f"[{mark}] {name}" + (f"：{detail}" if detail else ""))
        if not good:
            ok = False

    lines.append(f"项目根目录：{display(ROOT)}")
    lines.append(f"Python     ：{platform.python_version()} ({sys.executable})")
    check("Python 版本 >= 3.11", sys.version_info >= (3, 11), "当前 " + platform.python_version())

    for module, hint in (("openai", "pip install openai"), ("dotenv", "pip install python-dotenv")):
        try:
            __import__(module)
            check(f"依赖 {module}", True)
        except ImportError:
            check(f"依赖 {module}", False, f"缺失，请执行 {hint}")

    check("目录结构", True, "已确保存在")
    ensure_dirs()

    check("config/settings.json 存在", SETTINGS_FILE.exists(), display(SETTINGS_FILE))
    check("personality/personality.json 存在", PERSONALITY_FILE.exists(), display(PERSONALITY_FILE))
    check(".env.example 存在", ENV_EXAMPLE_FILE.exists(), display(ENV_EXAMPLE_FILE))
    check(
        ".env 存在",
        ENV_FILE.exists(),
        "已创建，但 LLM_API_KEY 还是空的" if ENV_FILE.exists() else "还没有 .env：复制 .env.example 为 .env 并填写 LLM_API_KEY",
    )

    for name, path in (("conversation/", CONVERSATION_DIR), ("memory/raw/events/", ROOT / "memory" / "raw" / "events"), ("logs/", LOGS_DIR)):
        check(f"{name} 可写", storage.is_writable(path))

    settings = None
    try:
        settings = load_settings()
        check("settings.json 可解析", True, f"默认模型 {settings.model}")
    except (ConfigError, ValueError) as exc:
        check("settings.json 可解析", False, str(exc))

    try:
        persona = load_persona()
        check("人格文件可用", True, f"{persona.name}（sha256 {persona.source_sha256[:12]}…）")
    except (PersonaError, ValueError) as exc:
        check("人格文件可用", False, str(exc))

    env = load_env()
    check("已读到 API Key", bool(env.api_key), "未填 LLM_API_KEY" if not env.api_key else "已设置（内容不显示）")
    check("BASE_URL 已配置", bool(env.base_url), env.base_url)
    if settings:
        check("模型已确定", bool(resolve_model(settings, env)), resolve_model(settings, env))

    try:
        conn = storage.connect()
        check("数据库可打开", True, display(DB_FILE))
        s = storage.stats(conn)
        lines.append(f"       当前已落盘：{s['turns']} 轮对话、{s['events']} 个原始事件、{s['api_calls']} 次调用")
        import verify as verify_mod

        report = verify_mod.verify_all(conn)
        check("原始数据未被改动", report["ok"], f"校验 {report['turns_checked']} 轮 / {report['events_checked']} 事件")
    except Exception as exc:
        check("数据库可打开", False, f"{type(exc).__name__}: {exc}")

    if online:
        from llm import LLMClient, LLMError

        try:
            if settings is None:
                raise LLMError("settings.json 不可用，无法联机测试。")
            client = LLMClient(
                api_key=env.api_key,
                base_url=env.base_url,
                model=resolve_model(settings, env),
                temperature=0.0,
                max_tokens=16,
            )
            # Phase 11：改走统一出口 —— 这次调用从此进 api_calls 表与费用账本。
            #
            # 原来这里是 `client.send(...)` 直连，绕过了 `chat.call_model` 的记账，
            # 于是下面那句"已计入费用"**与事实不符**（Phase 11 审计实测确认）。
            # 现在真的计入了，提示也就不再是错的。
            import chat as chat_mod

            conn = storage.connect()
            try:
                result = chat_mod.call_model_standalone(
                    client, [{"role": "user", "content": "回复两个字：在的"}],
                    settings=settings, conn=conn, purpose="doctor",
                )
            finally:
                conn.close()
            reply = result["reply"]
            cost = result["cost_micro_yuan"]
            detail = f"{reply.model} 返回「{reply.text.strip()[:20]}」，{reply.usage.prompt_tokens}+{reply.usage.completion_tokens} tokens，{reply.latency_ms} ms"
            check("联机调用模型", True, detail)
            lines.append(
                f"       这次自检真实调用了一次 API，已计入费用"
                f"（{cost} µ元 ≈ {cost / 1_000_000:.6f} 元），"
                f"并写入 conversation/conversations.db 的 api_calls 表与 logs/ledger.jsonl。"
            )
        except LLMError as exc:
            check("联机调用模型", False, str(exc))

    print("\n".join(lines))
    print("-" * 60)
    print("自检结论：全部通过。" if ok else "自检结论：存在问题，请按上面的提示处理。")
    return 0 if ok else 1
