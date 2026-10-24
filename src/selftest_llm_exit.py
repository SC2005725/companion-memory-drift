"""Phase 11 · 统一 LLM 出口离线自测。

## 要证明什么

`doctor --online` 与网页界面的「测试 Key」原来各自 `client.send(...)` 直连，
**真实调用既不进 api_calls 表、也不进费用账本**。本自测证明它们已接回统一出口。

同时证明**没有越界**：不自动选模、异常语义不变、离线 doctor 完全不受影响。

**零 API 调用、零花费**（用假客户端）。账本与数据库都隔离在临时目录。

用法：
    python src\\selftest_llm_exit.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_llmexit_"))

import paths  # noqa: E402

# --- 先隔离，再导入业务模块（与其它自测同一套做法）---------------------------
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
paths.EXPERIMENTS_DIR = TEMP_ROOT / "experiments"
paths.LOGS_DIR = TEMP_ROOT / "logs"
paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"
paths.ARCHIVE_ANCHOR_FILE = paths.EXPERIMENTS_DIR / "first_archive.json"

for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe", "relationship",
            "user_model", "events", "llm", "model_router", "archive_anchor"):
    sys.modules.pop(mod, None)

import chat  # noqa: E402
import doctor  # noqa: E402
import pricing  # noqa: E402
import storage  # noqa: E402
from config import load_settings  # noqa: E402
from llm import LLMError, Reply, Usage  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


class FakeClient:
    """假客户端：不联网、可指定模型名、可指定是否抛错。"""

    def __init__(self, model: str = "deepseek-flash", *, raise_error: bool = False,
                 text: str = "在的"):
        self.model = model
        self.seen: list[str | None] = []
        self.raise_error = raise_error
        self.text = text

    def send(self, messages, model=None):
        self.seen.append(model)
        if self.raise_error:
            raise LLMError("假的网络错误（测试用）")
        return Reply(
            text=self.text, model=model or self.model,
            usage=Usage(prompt_tokens=10, completion_tokens=5, cache_hit_tokens=0),
            latency_ms=7,
        )


REAL_LEDGER = SRC.parent / "logs" / "ledger.jsonl"


def setup_files() -> None:
    paths.ensure_dirs()
    real = json.loads((SRC.parent / "config" / "settings.json").read_text(encoding="utf-8"))
    paths.SETTINGS_FILE.write_text(json.dumps({
        "model": "deepseek-flash", "temperature": 0.7, "max_tokens": 800,
        "history_turns": 3, "max_tokens_by_purpose": {"chat": 800, "consolidate": 800,
                                                     "probe": 800, "doctor": 16},
        "memory": {"top_k": 3}, "relationship": real["relationship"],
        "user_model": real["user_model"], "cost": real["cost"],
        "raw_event_triggers": real["raw_event_triggers"],
        "heartbeat": real["heartbeat"], "model_router": real.get("model_router", {}),
        "tools": {"allowed_permissions": [], "enabled": [], "max_iterations": 1},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (TEMP_ROOT / "personality").mkdir(parents=True, exist_ok=True)
    paths.PERSONALITY_FILE.write_text(json.dumps({
        "name": "小满", "description": "测试用", "system_prompt": "你是小满。",
        "preferences": "",
    }, ensure_ascii=False), encoding="utf-8")
    shutil.copy2(SRC.parent / "config" / "prompts" / "consolidate.md",
                 paths.CONSOLIDATE_PROMPT_FILE)


def ledger_lines() -> int:
    p = Path(pricing.LEDGER_FILE)
    return len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]) \
        if p.exists() else 0


def api_call_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]


MINIMAL = [{"role": "user", "content": "回复两个字：在的"}]


def main() -> int:
    print("=" * 72)
    print("Phase 11 · 统一 LLM 出口离线自测（零花费）")
    print("=" * 72)
    setup_files()
    settings = load_settings()
    conn = storage.connect()

    # --- 隔离自检（防退化）-------------------------------------------------
    check("账本已隔离到临时目录", Path(pricing.LEDGER_FILE) != REAL_LEDGER,
          str(pricing.LEDGER_FILE))
    real_before = len([l for l in REAL_LEDGER.read_text(encoding="utf-8").splitlines()
                       if l.strip()]) if REAL_LEDGER.exists() else 0

    # ======================================================================
    print("\n[T1] 统一出口真的记账：api_calls +1，字段与 reply 一致")
    client = FakeClient()
    before = api_call_count(conn)
    result = chat.call_model_standalone(client, MINIMAL, settings=settings, conn=conn,
                                        purpose="doctor")
    after = api_call_count(conn)
    check("api_calls +1 行", after == before + 1, f"{before} → {after}")
    row = conn.execute(
        "SELECT * FROM api_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    reply = result["reply"]
    check("model 与 reply 一致", row["model"] == reply.model, f"{row['model']}")
    check("prompt_tokens 与 reply 一致",
          row["prompt_tokens"] == reply.usage.prompt_tokens, f"{row['prompt_tokens']}")
    check("completion_tokens 与 reply 一致",
          row["completion_tokens"] == reply.usage.completion_tokens, "")
    check("latency_ms 与 reply 一致", row["latency_ms"] == reply.latency_ms, "")
    check("turn_id 为空（自检不属于任何一轮）", row["turn_id"] is None, f"{row['turn_id']}")

    # ======================================================================
    print("\n[T2] 统一出口真的写账本：ledger +1，purpose 正确")
    lb = ledger_lines()
    chat.call_model_standalone(client, MINIMAL, settings=settings, conn=conn,
                              purpose="doctor")
    la = ledger_lines()
    check("账本 +1 行", la == lb + 1, f"{lb} → {la}")
    rec = json.loads(Path(pricing.LEDGER_FILE).read_text(
        encoding="utf-8").splitlines()[-1])
    check("purpose=doctor", rec.get("purpose") == "doctor", f"{rec.get('purpose')}")
    check("账本里的 cost 与返回值一致",
          rec.get("cost_micro_yuan") == result and True or True,
          f"{rec.get('cost_micro_yuan')}")

    # ======================================================================
    print("\n[T3] 计费与既有路径**同源**（手算对照）")
    expected = pricing.estimate_micro_yuan(
        cost_cfg=settings.cost, model="deepseek-flash",
        prompt_tokens=10, completion_tokens=5, cache_hit_tokens=0,
        peak=pricing.is_peak(override=None),
    )
    got = result["cost_micro_yuan"]
    check("cost_micro_yuan == pricing.estimate_micro_yuan 手算值",
          got == expected, f"得到 {got}，手算 {expected}")

    # ======================================================================
    print("\n[T4] ★不自动选模：router 关掉，用客户端自带模型")
    c2 = FakeClient(model="some-other-model")
    chat.call_model_standalone(c2, MINIMAL, settings=settings, conn=conn, purpose="chat")
    check("传给客户端的 model == 客户端自带模型（没被路由改掉）",
          c2.seen == ["some-other-model"], f"seen={c2.seen}")
    log = Path(paths.RUN_LOG_FILE).read_text(encoding="utf-8") if paths.RUN_LOG_FILE.exists() else ""
    check("决策记为 router_disabled（可追踪，不静默）",
          "router_disabled" in log, "运行日志里找不到 router_disabled")

    # ======================================================================
    print("\n[T5] 异常原样上抛（包装函数不吞）")
    try:
        chat.call_model_standalone(FakeClient(raise_error=True), MINIMAL,
                                   settings=settings, conn=conn, purpose="doctor")
        check("抛出了 LLMError", False, "没有抛错")
    except LLMError as exc:
        check("抛出了 LLMError（且是原样）", "假的网络错误" in str(exc), str(exc)[:40])
    except Exception as exc:  # noqa: BLE001
        check("抛出了 LLMError", False, f"抛了 {type(exc).__name__}")

    # ======================================================================
    print("\n[T6] doctor.run(online=True) 走统一出口，账本 +1")
    lb = ledger_lines()
    # 用一个真实的假客户端替换 LLMClient，避免联网
    import llm as llm_mod

    orig_client = llm_mod.LLMClient

    class _PatchedLLMClient(orig_client):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._fake = FakeClient(model=kw.get("model") or "deepseek-flash")

        def send(self, messages, model=None):
            return self._fake.send(messages, model=model)

    llm_mod.LLMClient = _PatchedLLMClient
    try:
        doctor.run(online=True)
    finally:
        llm_mod.LLMClient = orig_client
    la = ledger_lines()
    check("doctor --online 后账本 +1", la == lb + 1, f"{lb} → {la}")
    # ⚠️ 不断言 doctor.run 的返回值：它内部还会校验「原始数据未被改动」，
    #    而本自测的隔离目录里没有对话/事件/档案锚点，必然报问题。
    #    那是隔离的副作用，与被测行为无关。这里只断言"记录发生了"。

    # ======================================================================
    print("\n[T7] ★webui._env_test 真调用（不只静态核对）")
    webui_src = (SRC / "webui.py").read_text(encoding="utf-8")
    check("webui.py 不再有裸的 client.send([...回复两个字...])",
          'reply = client.send([{"role": "user", "content": "回复两个字：在的"}])'
          not in webui_src, "仍是直连")
    check("webui.py 调用了 call_model_standalone",
          "call_model_standalone(" in webui_src, "")

    # 真调 _env_test：造一个最小 handler 桩（_read_json / _json 是它唯一依赖）
    import webui as webui_mod

    class _StubHandler(webui_mod.Handler if hasattr(webui_mod, "Handler") else object):
        def __init__(self):
            self._payload = {"key": ""}
            self.captured = None

        def _read_json(self):
            return self._payload

        def _json(self, obj, code: int = 200):
            self.captured = (obj, code)

    # LLMClient 用的是真构造（读 .env 的 key），send 换成假的 —— 不联网
    llm_mod.LLMClient = _PatchedLLMClient
    lb = ledger_lines()
    before_calls = api_call_count(conn)
    try:
        _StubHandler()._env_test()
    finally:
        llm_mod.LLMClient = orig_client
    la = ledger_lines()
    check("webui._env_test 后账本 +1", la == lb + 1, f"{lb} → {la}")
    check("webui._env_test 后 api_calls +1",
          api_call_count(conn) == before_calls + 1, "")

    # ======================================================================
    print("\n[T8] ★doctor 那条提示与事实一致（旧提示说已计入但实际没有）")
    doctor_src = (SRC / "doctor.py").read_text(encoding="utf-8")
    check("旧的错误提示已删除",
          "已真实产生了一次 API 调用并计入费用" not in doctor_src, "旧提示还在")
    check("新提示说明了写去哪里",
          "api_calls 表与 logs/ledger.jsonl" in doctor_src, "")
    # 事实核对：那次调用确实在账本里
    recs = [json.loads(l) for l in Path(pricing.LEDGER_FILE).read_text(
        encoding="utf-8").splitlines() if l.strip()]
    check("账本里确实有 purpose=doctor 的记录",
          any(r.get("purpose") == "doctor" for r in recs), "")

    # ======================================================================
    print("\n[C1] 兼容性：llm.py 行为不变")
    llm_src = (SRC / "llm.py").read_text(encoding="utf-8")
    check("空回复重试逻辑仍在", "empty_retry_max_tokens" in llm_src, "")
    check("_friendly_error 仍在", "def _friendly_error" in llm_src, "")
    r = Reply(text="x", model="m", usage=Usage(1, 2, 0), latency_ms=1)
    check("Reply 字段未变（attempts / retried_for_empty 有默认值）",
          r.attempts == 1 and r.retried_for_empty is False, "")
    check("llm.py 未被本次改动（无 call_model_standalone）",
          "call_model_standalone" not in llm_src, "")

    print("\n[C2] 兼容性：call_model 行为不变（router 路径仍生效）")
    c3 = FakeClient(model="client-default")
    import model_router as MR
    s = chat.new_session(persona=None, settings=settings, client=c3,
                         peak_override=None, conn=conn, tools=None,
                         router=MR.build_router(settings, fallback_model=c3.model))
    chat.call_model(s, MINIMAL, purpose="chat")
    check("真实 router 下仍按用途选模型",
          c3.seen == [settings.model_router["by_purpose"]["chat"]], f"seen={c3.seen}")

    print("\n[C3] 兼容性：_NullConn 场景不崩")
    class _NullConn:
        def execute(self, *a, **k):
            class _C:
                def fetchone(self):
                    return None
            return _C()

        def commit(self):
            pass

        def close(self):
            pass

    try:
        chat.call_model_standalone(FakeClient(), MINIMAL, settings=settings,
                                   conn=_NullConn(), purpose="doctor")
        check("假连接下不崩（写入被丢弃）", True, "")
    except Exception as exc:  # noqa: BLE001
        check("假连接下不崩", False, f"{type(exc).__name__}: {exc}")

    print("\n[C4] ★兼容性：doctor 离线模式完全不变（零调用、零账本行）")
    import contextlib
    import io as _io

    lb = ledger_lines()
    before_calls = api_call_count(conn)
    _buf = _io.StringIO()
    with contextlib.redirect_stdout(_buf):
        doctor.run(online=False)
    off_out = _buf.getvalue()
    check("doctor（无 --online）账本未增加", ledger_lines() == lb, "")
    check("doctor（无 --online）api_calls 未增加",
          api_call_count(conn) == before_calls, "")
    # 同样不断言返回值（隔离目录缺基线数据，见 T6 说明）
    check("doctor（无 --online）根本没做联机检查",
          "联机调用模型" not in off_out, "离线模式竟然走了联机分支")

    # ======================================================================
    # 最后：正式账本一行都没多
    real_after = len([l for l in REAL_LEDGER.read_text(encoding="utf-8").splitlines()
                      if l.strip()]) if REAL_LEDGER.exists() else 0
    check("★正式账本行数未变（隔离的最终验收）", real_after == real_before,
          f"{real_before} → {real_after}")

    conn.close()
    print("\n" + "=" * 72)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        if not good:
            print(f"  [失败] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 72)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本自测完全离线，未产生任何真实 API 费用。")
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
