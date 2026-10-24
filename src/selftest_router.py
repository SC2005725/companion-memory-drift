"""Phase 10 模型路由离线自测。

**零 API 调用、零花费。** 路由是纯函数，不需要联网就能验。

## 要证明什么

1. 决策**确定性**：同样输入永远同样输出
2. 优先级正确：显式指定 > 按用途配置 > 默认模型
3. `source` 如实标注（能回答「这个模型是哪来的」）
4. 缺配置时**退回原行为**，不炸
5. **真的传到了客户端**（不是只在路由器里算了一下就丢掉）
6. 关闭开关后不影响正确性

用法：
    python src\\selftest_router.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# 账本隔离（Phase 10 审计发现：本文件是**唯一**没有隔离段的自测）
#
# ## 原来出了什么问题
# `chat.call_model` 会**无条件**调 `pricing.append_ledger()`。本自测虽然用
# `_NullConn()` 保护了数据库，但**没保护账本** —— 于是每跑一次就往**正式**
# `logs/ledger.jsonl` 追加 2 行 `sess_test`（实测累计污染 56 行）。
# 后果：`cost` 汇总报「未计价调用 56 次」，让人以为有真实调用漏记。
#
# ## 为什么改 `paths.LEDGER_FILE` 就够
# `pricing.py` 里是 `from paths import LEDGER_FILE`（**导入期绑定**），
# 所以只要在 `pricing` 被导入**之前**改掉 `paths.LEDGER_FILE`，
# `pricing.LEDGER_FILE` 就会绑定到临时路径。
# 其它 5 个自测（selftest_memory / companion / tools / p0_capture / p2_conflict）
# 用的就是这一套，这里照抄，**不引入新抽象**。
#
# ⚠️ 必须在**导入任何业务模块之前**执行 —— 所以放在模块顶层、
# 且用 `__name__ == "__main__"` 判定，避免被 import 时产生副作用。
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    _TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_router_"))
    import paths

    paths.LOGS_DIR = _TEMP_ROOT / "logs"
    paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
    paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)

import model_router as MR  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


class FakeClient:
    """记录每次调用实际用了哪个模型 —— 用来验证「路由真的传下去了」。"""

    def __init__(self, default_model: str = "client-default"):
        self.model = default_model
        self.seen: list[str | None] = []

    def send(self, messages, model=None):
        self.seen.append(model)
        from llm import Reply, Usage

        return Reply(text="ok", model=model or self.model,
                     usage=Usage(prompt_tokens=1, completion_tokens=1, cache_hit_tokens=0),
                     latency_ms=1)


def main() -> int:
    print("=" * 64)
    print("Phase 10 · 模型路由离线自测（零花费）")
    print("=" * 64)

    # ========================================================================
    # 先验证「账本隔离真的生效」—— 这是 Phase 10 审计发现的回归点。
    #
    # 断言防线：本自测绝不能往正式账本写任何东西。
    # 若不验，将来一次改动静默失效，又会在真实账本里堆 sess_test 行。
    # ========================================================================
    import pricing

    real_ledger = SRC.parent / "logs" / "ledger.jsonl"
    check("账本已重定向到临时目录（不碰正式）",
          Path(pricing.LEDGER_FILE).resolve() != real_ledger.resolve(),
          f"pricing.LEDGER_FILE={pricing.LEDGER_FILE}")
    check("重定向目标是临时目录",
          "memdrift_router_" in str(pricing.LEDGER_FILE), str(pricing.LEDGER_FILE))
    real_rows_before = 0
    if real_ledger.exists():
        real_rows_before = len([l for l in real_ledger.read_text(
            encoding="utf-8").splitlines() if l.strip()])
    check("跑之前正式账本里没有 sess_test 残留",
          all("sess_test" not in l for l in
              (real_ledger.read_text(encoding="utf-8").splitlines()
               if real_ledger.exists() else [])),
          f"正式账本 {real_rows_before} 行")

    # ========================================================================
    print("\n[R.1] 优先级：显式 > 按用途 > 默认")
    rules = {"enabled": True, "default": "D", "by_purpose": {"chat": "C"}}
    r = MR.ModelRouter(rules, fallback="F")

    d = r.resolve("chat")
    check("按用途配置生效", d["model"] == "C", f"{d['model']} [{d['source']}]")
    check("source 标为 purpose", d["source"] == "purpose", d["source"])

    d = r.resolve("没配过的用途")
    check("未配置的用途走默认", d["model"] == "D", f"{d['model']} [{d['source']}]")
    check("source 标为 default", d["source"] == "default", d["source"])

    d = r.resolve("chat", explicit="X")
    check("显式指定压过一切", d["model"] == "X", f"{d['model']} [{d['source']}]")
    check("source 标为 explicit", d["source"] == "explicit", d["source"])
    print(f"  chat → C（配置）／未配置 → D（默认）／显式 → X")

    # ========================================================================
    print("\n[R.2] 确定性：同样输入永远同样输出")
    a = MR.ModelRouter(rules, fallback="F").resolve("chat")
    b = MR.ModelRouter(rules, fallback="F").resolve("chat")
    check("两次独立实例结果一致", a == b, f"{a['model']} vs {b['model']}")
    seq1 = [MR.ModelRouter(rules, fallback="F").resolve(p)["model"]
            for p in ("chat", "other", "chat")]
    seq2 = [MR.ModelRouter(rules, fallback="F").resolve(p)["model"]
            for p in ("chat", "other", "chat")]
    check("调用顺序不影响结果", seq1 == seq2, str(seq1))
    print(f"  两次构造 + 三种顺序，结果一致：{seq1}")

    # ========================================================================
    print("\n[R.3] 缺配置时退回原行为，不炸")
    for label, cfg, fb, expect in [
        ("完全空配置（用 fallback）", {}, "F", "F"),
        ("只有 default", {"default": "D"}, "F", "D"),
        ("default 为空字符串", {"default": ""}, "F", "F"),
        ("enabled 缺失（默认视为开）", {"default": "D"}, "F", "D"),
    ]:
        rr = MR.ModelRouter(cfg, fallback=fb)
        got = rr.resolve("任意")["model"]
        check(label, got == expect, f"得到 {got!r} 期望 {expect!r}")
    print("  4 种缺配置情形都正确回退")

    # ========================================================================
    print("\n[R.4] 关闭开关后不影响正确性")
    rr = MR.ModelRouter({"enabled": False, "default": "D", "by_purpose": {"chat": "C"}},
                        fallback="F")
    d = rr.resolve("chat")
    check("关闭后不走 by_purpose", d["model"] == "D", f"{d['model']} [{d['source']}]")
    check("source 标为 router_disabled", d["source"] == "router_disabled", d["source"])
    d2 = rr.resolve("chat", explicit="X")
    check("关闭后显式指定仍生效", d2["model"] == "X", d2["model"])
    print(f"  关闭：chat → D（不是 C）／显式 X 仍生效")

    # ========================================================================
    print("\n[R.5] 决策可追踪（能回答「这个模型哪来的」）")
    r5 = MR.ModelRouter(rules, fallback="F")
    for p in ("chat", "other"):
        r5.resolve(p)
    check("记录了每次决策", len(r5.decisions) == 2, f"{len(r5.decisions)} 条")
    check("每条都带 model/source/reason",
          all({"model", "source", "reason"} <= set(x) for x in r5.decisions), "")
    check("reason 是人能读懂的中文",
          all("模型" in x["reason"] or "指定" in x["reason"] for x in r5.decisions), "")
    txt = r5.describe()
    check("describe() 能打印出来", "路由开关" in txt and "默认模型" in txt, "")
    print(f"  记录 {len(r5.decisions)} 条决策，describe() {len(txt)} 字符")

    # ========================================================================
    print("\n[R.6] 路由**真的传到了客户端**（不是算完就丢）")
    # 这是最关键的一条：光有决策没用，必须验证 chat.call_model 真的把模型传下去。
    client = FakeClient()
    r6 = MR.ModelRouter({"enabled": True, "default": "ROUTED", "by_purpose": {}},
                        fallback="F")

    # 直接调 call_model 需要 session，桩一个最小的
    class S:
        pass

    s = S()
    s.client = client
    s.router = r6
    s.peak_override = False
    s.session_id = "sess_test"
    s.conn = _NullConn()
    from config import Settings

    s.settings = Settings()

    import chat

    chat.call_model(s, [{"role": "user", "content": "hi"}], purpose="chat")
    check("call_model 把路由结果传给了 client.send",
          client.seen == ["ROUTED"], f"实际收到 {client.seen}")

    # 没有 router 时不传 model → 退回客户端默认（老行为不变）
    s2 = S()
    s2.client = FakeClient()
    s2.router = None
    s2.peak_override = False
    s2.session_id = "sess_test2"
    s2.conn = _NullConn()
    s2.settings = Settings()
    chat.call_model(s2, [{"role": "user", "content": "hi"}], purpose="chat")
    check("没有 router 时不传 model（保持旧行为）",
          s2.client.seen == [None], f"实际收到 {s2.client.seen}")
    print(f"  有 router → client 收到 'ROUTED'；无 router → 收到 None（旧行为）")

    # ========================================================================
    print("\n[R.7] 真实配置能装配出来")
    try:
        from config import load_settings

        st = load_settings()
        rr = MR.build_router(st, fallback_model="FB")
        check("能从真实 settings.json 装配", rr is not None, "")
        check("默认模型不为空", bool(rr.default), rr.default)
        # 当前配置下所有用途都用同一个模型 —— 这是**已知事实**，不是 bug
        models = {rr.resolve(p)["model"] for p in ("chat", "consolidate", "probe", "doctor")}
        check("（已知）当前所有用途同一个模型（所以省不到钱）",
              len(models) == 1, f"用到 {models}")
        print(f"  默认 {rr.default}；四个用途都指向 {models}")
        print("  ⚠️ 这是已知事实：flash 已是最低档，简单任务没有更便宜的可用")
    except Exception as exc:
        check("能从真实 settings.json 装配", False, f"{type(exc).__name__}: {exc}")

    # --- 汇总 ---
    #
    # 最后再验一次：整轮跑完，正式账本**一行都没多**。
    # 这一条是整个隔离段的最终验收 —— 前面只是设置，这里才是结果。
    real_rows_after = 0
    if real_ledger.exists():
        real_rows_after = len([l for l in real_ledger.read_text(
            encoding="utf-8").splitlines() if l.strip()])
    check("★跑完后正式账本行数未增加（隔离的最终验收）",
          real_rows_after == real_rows_before,
          f"{real_rows_before} → {real_rows_after}")
    check("★正式账本里依然没有 sess_test",
          all("sess_test" not in l for l in
              (real_ledger.read_text(encoding="utf-8").splitlines()
               if real_ledger.exists() else [])), "")
    # 而被重定向的那份临时账本，应该确实写进了东西（证明调用真发生了）
    tmp_rows = 0
    if Path(pricing.LEDGER_FILE).exists():
        tmp_rows = len([l for l in Path(pricing.LEDGER_FILE).read_text(
            encoding="utf-8").splitlines() if l.strip()])
    check("临时账本确实记录了本自测的调用（证明路由真跑过）",
          tmp_rows > 0, f"临时账本 {tmp_rows} 行")

    print("\n" + "=" * 64)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 64)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本自测完全离线，未产生任何真实 API 费用。")
    return 1 if failed else 0


class _NullConn:
    """最小的假连接：只接收写入，不真落库。"""

    def execute(self, *a, **k):
        return _NullCursor()

    def commit(self):
        pass

    def close(self):
        pass


class _NullCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
