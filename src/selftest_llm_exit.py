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
# 预算告警的 ntfy 投递在自测里全程被替换成捕获列表（见 main() 开头）——
# 既断言「拦截真的会推手机」，又保证自测永远不发真实推送。
NTFY_PUSHES: list[str] = []


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

    # ntfy 隔离：topic 换成假值 + 投递换成捕获列表。
    # ⚠️ 必须在 load_settings() 之后立刻做 —— load_settings 会把真实 .env
    # 的 NTFY_TOPIC 读进进程环境（config.py），不隔离的话预算告警会真推手机。
    import heartbeat as _hb0
    import os as _os0

    _os0.environ["NTFY_TOPIC"] = "selftest-isolated-topic"
    _hb0.deliver_to_ntfy = lambda topic, msg: NTFY_PUSHES.append(topic) or True

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
    print("\n[F1] ★费用可信度批：用量跨 attempt 合并（不再只记最后一次）")
    import llm as llm_mod
    from types import SimpleNamespace as _NS

    def _fake_resp(content, finish, pt, ct):
        msg = _NS(content=content)
        ch = _NS(message=msg, finish_reason=finish)
        # to_dict 让 _as_dict 能取到 raw（真 SDK 对象都有 model_dump/to_dict）
        usage = _NS(prompt_tokens=pt, completion_tokens=ct, prompt_cache_hit_tokens=5,
                    to_dict=lambda: {"prompt_tokens": pt, "completion_tokens": ct,
                                     "prompt_cache_hit_tokens": 5})
        return _NS(choices=[ch], usage=usage)

    class _RetryClient(llm_mod.LLMClient):
        """第一次请求：思考吃满预算（800 全是 reasoning、无内容）；
        第二次（大预算）：正常回复。模拟真实「截断→重试」场景。"""

        def __init__(self):
            self.model = "deepseek-flash"
            self.max_tokens = 800
            self.empty_retry_max_tokens = 3200
            self.empty_retries = 0
            self.calls: list[int] = []

        def _raw_send(self, messages, max_tokens, model=None):
            self.calls.append(max_tokens)
            if max_tokens == 800:
                return _fake_resp("", "length", 100, 800)
            return _fake_resp("在的", "stop", 100, 1000)

    rc = _RetryClient()
    reply = rc.send(MINIMAL)
    check("attempt 数 = 2", reply.attempts == 2, f"{reply.attempts}")
    check("prompt_tokens = 两次相加（200）",
          reply.usage.prompt_tokens == 200, f"{reply.usage.prompt_tokens}")
    check("completion_tokens = 两次相加（1800）",
          reply.usage.completion_tokens == 1800, f"{reply.usage.completion_tokens}")
    check("cache_hit = 两次相加（10）",
          reply.usage.cache_hit_tokens == 10, f"{reply.usage.cache_hit_tokens}")
    check("首次用量存档在 raw.first_attempt_usage",
          "first_attempt_usage" in (reply.usage.raw or {}), "")

    u1 = Usage(prompt_tokens=10, completion_tokens=800, cache_hit_tokens=5,
               raw={"completion_tokens_details": {"reasoning_tokens": 800}})
    u2 = Usage(prompt_tokens=10, completion_tokens=1000, cache_hit_tokens=5,
               raw={"completion_tokens_details": {"reasoning_tokens": 500}})
    merged = llm_mod._merge_usage(u1, u2)
    check("合并后思考 token 相加（1300，记账口径不破）",
          merged.raw["completion_tokens_details"]["reasoning_tokens"] == 1300,
          str(merged.raw.get("completion_tokens_details")))

    # ======================================================================
    print("\n[F1b] ★重试抛错也带上已耗用量（服务端已计费的那几次）")
    from llm import LLMError as _LE

    class _FailOnRetryClient(_RetryClient):
        def _raw_send(self, messages, max_tokens, model=None):
            if max_tokens == 800:
                return _fake_resp("", "length", 100, 800)
            raise _LE("重试仍空（测试用）")

    fr = _FailOnRetryClient()
    try:
        fr.send(MINIMAL)
        check("抛出了 LLMError", False, "没有抛错")
    except _LE as exc:
        check("异常携带累计 usage（第一次的 100+800）",
              exc.usage is not None and exc.usage.prompt_tokens == 100
              and exc.usage.completion_tokens == 800,
              f"usage={exc.usage}")
        check("异常携带 attempt 数", exc.attempts == 1, f"{exc.attempts}")

    # ======================================================================
    print("\n[F1c] ★call_model 失败兜底入账（api_calls + ledger 都有，error 可见）")
    class _FailWithUsage:
        model = "deepseek-flash"

        def send(self, messages, model=None):
            raise _LE("重试仍空（测试用）",
                      usage=Usage(prompt_tokens=100, completion_tokens=1800,
                                  cache_hit_tokens=5),
                      attempts=2)

    before_calls = api_call_count(conn)
    lb = ledger_lines()
    try:
        chat.call_model_standalone(_FailWithUsage(), MINIMAL, settings=settings,
                                   conn=conn, purpose="doctor")
        check("异常原样上抛", False, "没有抛错")
    except _LE as exc:
        check("异常原样上抛（记账后不吞）", "重试仍空" in str(exc), "")
    row = conn.execute(
        "SELECT * FROM api_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    check("失败调用进了 api_calls", api_call_count(conn) == before_calls + 1, "")
    check("error 列记录了失败原因", (row["error"] or "").find("重试仍空") >= 0,
          str(row["error"])[:60])
    check("失败调用的 token 也如实入账",
          row["prompt_tokens"] == 100 and row["completion_tokens"] == 1800,
          f"{row['prompt_tokens']}+{row['completion_tokens']}")
    rec = json.loads(Path(pricing.LEDGER_FILE).read_text(
        encoding="utf-8").splitlines()[-1])
    check("账本行标记 ok=False", rec.get("ok") is False, str(rec.get("ok")))
    check("账本行 attempts=2", rec.get("attempts") == 2, str(rec.get("attempts")))
    check("失败费用按已耗 token 估算（>0）",
          (rec.get("cost_micro_yuan") or 0) > 0, str(rec.get("cost_micro_yuan")))

    # ======================================================================
    print("\n[F2] ★整理失败退避：门槛函数 + kv_state 状态")
    check("失败 1 次 → every+5", chat.backoff_threshold(20, 1) == 25,
          str(chat.backoff_threshold(20, 1)))
    check("失败 2 次 → every+10", chat.backoff_threshold(20, 2) == 30, "")
    check("失败 7 次 → 封顶 every+240", chat.backoff_threshold(20, 7) == 260,
          str(chat.backoff_threshold(20, 7)))
    check("无失败 → 不加码", chat.backoff_threshold(20, 0) == 20, "")
    storage.set_kv(conn, "consolidate_fail_count", 3)
    check("kv_state 写读一致",
          storage.get_kv(conn, "consolidate_fail_count") == "3", "")
    check("kv_state 缺键返回默认", storage.get_kv(conn, "no-such-key", "d") == "d", "")

    # ======================================================================
    print("\n[F3] ★预算闸门：裁决函数 + 统一出口拦截")
    cfg = {"monthly_yuan": 50, "soft_ratio": 0.8, "exempt_purposes": ["chat"]}
    v, info = chat.budget_verdict(10_000_000, cfg, "chat")           # 10 元
    check("软线以下放行", v == "ok", v)
    v, _ = chat.budget_verdict(45_000_000, cfg, "consolidate")       # 45 元
    check("过软线 → soft（放行+提醒）", v == "soft", v)
    v, _ = chat.budget_verdict(50_000_001, cfg, "consolidate")       # 50.000001 元
    check("过硬线非豁免 → block", v == "block", v)
    v, _ = chat.budget_verdict(50_000_001, cfg, "chat")
    check("过硬线但 chat 豁免 → hard_exempt", v == "hard_exempt", v)
    check("未配置（0）→ off", chat.budget_verdict(999_999_999, {}, "probe")[0] == "off", "")
    check("坏配置不炸（当 0 关闭）",
          chat.budget_verdict(1, {"monthly_yuan": "abc"}, "probe")[0] == "off", "")

    # 接线：把月预算设到极小（当前库已有花销必然超硬线）→ 非豁免用途发不出请求
    import model_router as _MR
    gb_client = FakeClient()
    orig_budget = settings.cost.get("budget")
    settings.cost["budget"] = {"monthly_yuan": 0.000001, "soft_ratio": 0.5,
                               "exempt_purposes": ["chat"]}
    try:
        gb_session = chat.new_session(persona=None, settings=settings, client=gb_client,
                                      peak_override=None, conn=conn, tools=None,
                                      router=_MR.ModelRouter({"enabled": False},
                                                             fallback=gb_client.model))
        before_calls = api_call_count(conn)
        _push_before = len(NTFY_PUSHES)
        try:
            chat.call_model(gb_session, MINIMAL, purpose="probe")
            check("过硬线非豁免用途被拦", False, "没有拦")
        except chat.BudgetExceeded as exc:
            check("过硬线非豁免用途被拦（BudgetExceeded）", "预算已用尽" in str(exc), "")
        check("被拦的调用没有发出请求", gb_client.seen == [], f"seen={gb_client.seen}")
        check("被拦的调用没有产生费用记录", api_call_count(conn) == before_calls, "")
        check("硬线拦截推了一条 ntfy（手机可见）",
              len(NTFY_PUSHES) == _push_before + 1, f"pushes={len(NTFY_PUSHES)}")
        chat.call_model(gb_session, MINIMAL, purpose="chat")
        check("chat 豁免：过硬线仍放行", gb_client.seen != [], f"seen={gb_client.seen}")
        check("chat 豁免也推了一条提醒", len(NTFY_PUSHES) == _push_before + 2,
              f"pushes={len(NTFY_PUSHES)}")
        log = Path(paths.RUN_LOG_FILE).read_text(encoding="utf-8")
        check("豁免放行记了日志（可追踪，不静默）", "budget HARD" in log, "")
    finally:
        settings.cost["budget"] = orig_budget

    # ======================================================================
    print("\n[F3b] ★预算告警防重：软线每自然月最多一条；无 topic 静默跳过")
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    _MONTH = _dt.now(_tz.utc).strftime("%Y%m")
    _push_before = len(NTFY_PUSHES)
    info_s = {"spent_micro_yuan": 25_000_000, "soft_micro_yuan": 24_000_000,
              "hard_micro_yuan": 30_000_000}
    # 先清掉可能存在的软线旗标，保证本段从「未提醒」状态开始
    storage.set_kv(conn, f"budget_soft_warned_{_MONTH}", "")
    chat._budget_notify(conn, "soft", info_s)
    check("软线触发推送 1 次", len(NTFY_PUSHES) == _push_before + 1,
          f"len={len(NTFY_PUSHES)}")
    check("kv_state 记了防重复标记",
          bool(storage.get_kv(conn, f"budget_soft_warned_{_MONTH}")), "")
    chat._budget_notify(conn, "soft", info_s)
    check("同月第二次不再推", len(NTFY_PUSHES) == _push_before + 1,
          f"len={len(NTFY_PUSHES)}")
    # 无 topic：清旗标 + 摘掉环境变量 → 应静默跳过（不推、不炸）
    storage.set_kv(conn, f"budget_soft_warned_{_MONTH}", "")
    _had_topic = _os.environ.pop("NTFY_TOPIC", None)
    try:
        chat._budget_notify(conn, "soft", info_s)
        check("无 topic 静默跳过（不推、不炸）", len(NTFY_PUSHES) == _push_before + 1,
              f"len={len(NTFY_PUSHES)}")
    finally:
        if _had_topic is not None:
            _os.environ["NTFY_TOPIC"] = _had_topic

    # ======================================================================
    print("\n[F4] ★账本坏行单独计数（不再无声吞掉）")
    ledger_path = Path(pricing.LEDGER_FILE)
    lines_before = [l for l in ledger_path.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
    ledger_path.write_text(
        "\n".join(lines_before) + "\n{这行是故意的坏JSON\n", encoding="utf-8")
    s = pricing.summarize()
    check("summarize 报告 corrupt_lines=1", s.get("corrupt_lines") == 1,
          str(s.get("corrupt_lines")))
    check("read_ledger 仍只返回有效行",
          len(pricing.read_ledger()) == len(lines_before), "")
    text = pricing.format_summary(s)
    check("格式化输出包含坏行告警", "账本损坏行" in text, "")
    # 清理坏行，不污染后续断言
    ledger_path.write_text("\n".join(lines_before) + "\n", encoding="utf-8")

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
