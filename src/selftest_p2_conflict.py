"""P2 专项自测：矛盾保留 · 信念修正（方案 A）· verifiable 标记。

在**独立临时目录**里跑，绝不碰真实的 conversation/ user_model/。

## 方案 A 的行为定义（这份测试要证明的东西）

> **用户模型层只记录、不自动改。** 矛盾双方都保留，标 `unresolved` / `conflict`；
> **没有可靠的显式主题键时，不执行版本取代、不执行裁定推翻。**

三条路径：
  · **版本取代**     → 无键 → **不执行**，记 unresolved
  · **系统裁定**     → 无键 → 判据①永不满足 → **不执行**
  · **用户明确裁定** → 不依赖主题键（correction 关键词是**显式指向**）→ **可执行**

另加 `verifiable` 正交标记（只表示"内容可被核实"，**不代表命题已证实**）。

用法：
    python src\\selftest_p2_conflict.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_p2_"))

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

import user_model as um  # noqa: E402
from config import load_settings  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def setup_files() -> None:
    paths.ensure_dirs()
    real = json.loads((SRC.parent / "config" / "settings.json").read_text(encoding="utf-8"))
    paths.SETTINGS_FILE.write_text(
        json.dumps({
            "model": "fake", "temperature": 0.7, "max_tokens": 800, "history_turns": 3,
            "max_tokens_by_purpose": {"chat": 800, "consolidate": 800, "probe": 800},
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


def mk(i, belief, status="inferred", ck="behavior", seq=900, extra=None):
    """造一条既有条目（可选带 P2 新字段）。"""
    src = {"kind": "stated", "source_kind": "stated", "claim_kind": ck, "matched": [],
           "rule": "stated", "evidence": belief, "seq": seq, "turn_id": f"t{seq}",
           "session_id": "s"}
    if extra:
        src.update(extra)
    return {"id": f"um_t{i:04d}", "belief": belief, "status": status, "confidence": 0.5,
            "source": src, "last_updated": "2026-10-10T00:00:00+08:00", "revision": 1,
            "superseded_by": None, "superseded_at": None, "revision_history": []}


def main() -> int:
    print("=" * 72)
    print("P2 专项自测：矛盾保留 · 信念修正（方案 A）· verifiable")
    print("=" * 72)
    setup_files()
    settings = load_settings()
    rules = {k: v for k, v in settings.user_model.items() if not k.startswith("_")}

    def signals(text):
        return um.detect_signals(text, rules)

    def claims(text, seq):
        return um.build_claims(signals(text), session_id="s", seq=seq,
                               turn_id=f"t{seq}", source_text=text)

    # ======================================================================
    print("\n[P2-1] 目标并列 → 记 unresolved，且**不自动改**（旧行为：什么都不发生）")
    A, B = "我一般晚上才有精神", "我晚上才有力气做事"
    TIE = "我没说过我晚上精神好"          # 实测 A=B=0.4，真并列
    pool = [mk(1, A), mk(2, B)]
    diag = um._correction_diagnosis(pool, {"belief": TIE}, rules)
    check("诊断出 tie（并列）", diag["outcome"] == "tie", f"{diag['outcome']}")
    check("并列候选是那两条", len(diag["candidates"]) == 2, f"{diag['candidates']}")
    items2, ch = um.merge_claims(pool, claims(TIE, 101), rules)
    uns = [c for c in ch if c["type"] == "unresolved"]
    check("产生了 unresolved 变更", len(uns) == 1, f"{[c['type'] for c in ch]}")
    check("**没有任何条目被作废**",
          not [x for x in items2 if x.get("superseded_by")],
          f"{[x['belief'] for x in items2 if x.get('superseded_by')]}")
    check("unresolved 带候选清单", uns and len(uns[0]["candidates"]) == 2, "")
    check("signature 只由 id 组成（不掺 belief 文本）",
          uns and uns[0]["signature"] == "unresolved:um_t0001,um_t0002",
          f"{uns[0]['signature'] if uns else None}")

    print("\n[P2-2] unresolved 能落进 user_model_events（kind 断言）")
    import storage
    conn = storage.connect()
    try:
        for c in ch:
            storage.record_user_model_event(
                conn, session_id="s", turn_id="t101", seq=101,
                kind=c["type"], claim_id=c.get("id") or c.get("target_id"),
                belief=c.get("belief") or c.get("target_belief") or "",
                detail=json.dumps(c, ensure_ascii=False))
        rows = storage.list_user_model_events(conn)
        kinds = [r["kind"] for r in rows]
        check("user_model_events 里出现 kind=unresolved", "unresolved" in kinds, f"{kinds}")
        detail = json.loads([r for r in rows if r["kind"] == "unresolved"][0]["detail"])
        check("detail 里带 candidates", len(detail.get("candidates") or []) == 2, "")
    finally:
        conn.close()

    # ======================================================================
    print("\n[P2-3] 用户明确裁定 → **可执行**，记 adjudicated / user")
    pool = [mk(1, "我是那种不太爱说话的人", "confirmed", "self_description")]
    items2, ch = um.merge_claims(pool, claims("我没说过我不太爱说话", 102), rules)
    rev = [c for c in ch if c["type"] == "revise"]
    check("产生 revise", len(rev) == 1, f"{[c['type'] for c in ch]}")
    check("change 标为 adjudicated", rev and rev[0].get("change") == "adjudicated",
          f"{rev[0].get('change') if rev else None}")
    check("adjudicated_by=user", rev and rev[0].get("adjudicated_by") == "user", "")
    tgt = [x for x in items2 if x.get("superseded_by")]
    check("旧条目被标 superseded_by（未删除）", len(tgt) == 1, "")
    hist = tgt[0]["revision_history"][-1] if tgt else {}
    check("revision_history 记 change=adjudicated",
          hist.get("change") == "adjudicated", f"{hist.get('change')}")
    check("revision_history 记 adjudicated_by=user",
          hist.get("adjudicated_by") == "user", "")
    check("★没有出现 reinstated（F2 只加 adjudicated）",
          "reinstated" not in json.dumps(hist, ensure_ascii=False), "")

    # ======================================================================
    print("\n[P2-4] 版本取代 **不自动执行**（主题键恒 None）")
    check("_topic_key_of 恒返回 None（接口预留，未实现）",
          um._topic_key_of(mk(1, A)) is None, "")
    pool = [mk(1, A)]
    # 这句话经检测器判定后**零信号**（不属于任何已支持的信息类型）——
    # 所以它既不新增条目、也不作废条目。这正是「不自动改」的一种形态。
    sigs_v = signals("我最近改早睡了，晚上反而困")
    check("该陈述本身零信号（不属已支持类型）", len(sigs_v) == 0,
          f"得到 {[(s['type'], s.get('claim_kind')) for s in sigs_v]}")
    items2, ch = um.merge_claims(pool, claims("我最近改早睡了，晚上反而困", 103), rules)
    check("**不改任何条目**", not [x for x in items2 if x.get("superseded_by")], "")
    check("也不产生 revise / unresolved", not [c for c in ch if c["type"] in ("revise", "unresolved")],
          f"{[c['type'] for c in ch]}")
    # 另取一条**确实产生条目**的版本变化陈述，确认它只做 add、不做取代
    pool2 = [mk(1, A, extra={"verifiable": um.VERIFIABLE_UNKNOWN})]
    items3, ch3 = um.merge_claims(pool2, claims("我一般白天才有精神", 104), rules)
    check("新陈述只产生 add，不产生 revise",
          "add" in [c["type"] for c in ch3] and "revise" not in [c["type"] for c in ch3],
          f"{[c['type'] for c in ch3]}")
    check("旧条目仍未被作废", not [x for x in items3 if x.get("superseded_by")], "")

    # ======================================================================
    print("\n[P2-5] 系统裁定 **不自动执行**：self_evaluation 禁裁 + 无键")
    r = um.adjudicate_by_observation(
        [mk(1, "我执行力很差", "uncertain", "self_evaluation")],
        {"topic_key": "topic:execution", "new_value": "多次按时完成", "old_value": "很差"},
        rules)
    check("系统裁定返回 not_executed", r.get("executed") is False, f"{r}")
    check("理由是『无候选（无可靠主题键）』或『禁止裁定自评』",
          any(w in (r.get("reason") or "") for w in ("主题键", "自评", "候选")),
          f"{r.get('reason')}")

    print("\n[P2-6] 判据④：old_value 与现有记录不一致 → unresolved（不覆盖）")
    # ⚠️ 方案 A 下 `_topic_key_of` 恒 None，**判据④ 不可达**（判据① 先拦住）。
    #    要测判据④ 的逻辑，必须**临时替换主题键**来模拟"接口落地后"。
    #    这是对该判据的白盒测试 —— 它现在不生效，但将来会。
    import user_model as _um
    orig = _um._topic_key_of
    try:
        _um._topic_key_of = lambda item: (item.get("source") or {}).get("topic_key")

        # 先确认：方案 A（未替换）下确实不可达
        _um._topic_key_of = orig
        r0 = um.adjudicate_by_observation(
            [mk(1, "用户在学 Python", "inferred", "objective_attribute",
                extra={"topic_key": "topic:programming_language"})],
            {"topic_key": "topic:programming_language", "new_value": "已不学 Python",
             "old_value": "在学 Java"}, rules)
        check("★方案 A 下判据④不可达（判据①先拦住）",
              "无候选" in (r0.get("reason") or ""), f"{r0.get('reason')}")

        # 替换主题键后，判据④ 才可达
        _um._topic_key_of = lambda item: (item.get("source") or {}).get("topic_key")
        pool = [mk(1, "用户在学 Python", "inferred", "objective_attribute",
                   extra={"topic_key": "topic:programming_language"})]
        r = um.adjudicate_by_observation(
            pool,
            {"topic_key": "topic:programming_language", "new_value": "已不学 Python",
             "old_value": "在学 Java"},          # ← 与我们记录不一致
            rules)
        check("（接口落地后）old_value 不一致 → 不执行", r.get("executed") is False, f"{r}")
        check("理由是 old_value 不一致", "old_value" in (r.get("reason") or ""),
              f"{r.get('reason')}")

        # 一致时**才**执行（证明判据④真的在把关，不是永远拒绝）
        r2 = um.adjudicate_by_observation(
            pool,
            {"topic_key": "topic:programming_language", "new_value": "已不学 Python",
             "old_value": "在学 Python"},        # ← 一致
            rules)
        check("（接口落地后）old_value 一致 → 执行", r2.get("executed") is True, f"{r2}")
        check("执行时 change=adjudicated / by=system",
              r2.get("change") == "adjudicated" and r2.get("adjudicated_by") == "system",
              f"{r2}")

        # 判据③：自评禁裁（即使主题键对得上）
        pool_self = [mk(2, "我执行力很差", "uncertain", "self_evaluation",
                        extra={"topic_key": "topic:execution"})]
        r3 = um.adjudicate_by_observation(
            pool_self,
            {"topic_key": "topic:execution", "new_value": "多次按时完成", "old_value": "很差"},
            rules)
        check("★判据③：自评禁裁（主题键对上也拒）",
              r3.get("executed") is False and "自我评价" in (r3.get("reason") or ""),
              f"{r3.get('reason')}")
    finally:
        _um._topic_key_of = orig

    # ======================================================================
    print("\n[P2-7] verifiable 三档（收紧白名单）")
    for text, want in [("我执行力很差", um.VERIFIABLE_NO),
                       ("我喜欢喝咖啡", um.VERIFIABLE_UNKNOWN),
                       ("我的专业是物流", um.VERIFIABLE_YES),
                       ("我叫（网名）", um.VERIFIABLE_YES),
                       ("我今年二十岁", um.VERIFIABLE_YES),
                       ("我住城东", um.VERIFIABLE_YES)]:
        sg = signals(text)
        ck = sg[0].get("claim_kind") if sg else ""
        got = um._verifiable_of(text, ck)
        check(f"verifiable({text}) = {want}", got == want, f"得到 {got}（claim_kind={ck}）")

    print("\n[P2-8] ★verifiable 白名单的易混淆**反例**（不得标 yes）")
    for text in ("我在忙", "我在吃饭", "我在写代码", "我在想事情", "我在等回复",
                 "我在看书", "我有个想法", "我喜欢安静", "我执行力很差",
                 "我可能有点社恐"):
        sg = signals(text)
        ck = sg[0].get("claim_kind") if sg else ""
        got = um._verifiable_of(text, ck)
        check(f"不得标 yes：{text}", got != um.VERIFIABLE_YES,
              f"得到 {got}（claim_kind={ck}）")

    print("\n[P2-9] ★verifiable 不表示「已证实」（注入时写清）")
    items = [mk(1, "我的专业是物流管理", "confirmed", "objective_attribute",
                extra={"verifiable": um.VERIFIABLE_YES})]
    txt = um.render_for_prompt(items)
    check("注入文本含 verifiable 说明", "verifiable" in txt, "")
    check("明确写「可以被核实」", "可以被核实" in txt, "")
    check("★明确写「不代表已被证实」", "不代表它已经被证实" in txt, "")

    # ======================================================================
    print("\n[P2-C1] 兼容性：claim_kind 取值集**未变**（P0 分类不动）")
    check("5 个 claim_kind 常量齐全",
          all(hasattr(um, n) for n in
              ("CLAIM_SELF_EVALUATION", "CLAIM_OBJECTIVE_ATTRIBUTE",
               "CLAIM_SELF_DESCRIPTION", "CLAIM_BEHAVIOR", "CLAIM_UNCERTAIN")), "")
    check("claim_kind 标签表仍是 5 项", len(um.CLAIM_KIND_LABELS) == 5,
          f"{len(um.CLAIM_KIND_LABELS)}")
    check("未新增 claim_kind 取值（verifiable 是正交字段）",
          not any(v == "verifiable_record" for v in um.CLAIM_KIND_LABELS), "")
    # P0 的分类断言抽样（应与 selftest_p0_capture 一致）
    for text, want in [("我执行力很差", um.CLAIM_SELF_EVALUATION),
                       ("我在物流专业", um.CLAIM_OBJECTIVE_ATTRIBUTE),
                       ("我是那种不太爱说话的人", um.CLAIM_SELF_DESCRIPTION),
                       ("我喜欢喝咖啡", um.CLAIM_BEHAVIOR)]:
        sg = signals(text)
        got = sg[0].get("claim_kind") if sg else None
        check(f"P0 分类未变：{text} → {want}", got == want, f"得到 {got}")

    print("\n[P2-C2] 兼容性：旧快照条目（无 verifiable / claim_kind）可读且不触发修改")
    legacy = {"id": "um_legacy001", "belief": "我是那种不太爱说话的人",
              "status": "confirmed", "confidence": 0.9,
              "source": {"kind": "self_description", "matched": ["我是那种"],
                         "session_id": "s", "seq": 74, "turn_id": "t74"},
              "last_updated": "2026-10-08T13:50:45+08:00", "revision": 1,
              "superseded_by": None, "superseded_at": None, "revision_history": []}
    txt = um.render_for_prompt([legacy])
    check("旧条目能渲染不报错", bool(txt), "")
    check("旧条目缺 verifiable → 视为未标（不误报 yes）",
          "verifiable=yes" not in txt, "")
    check("旧条目缺 claim_kind → _verifiable_of 不崩",
          um._verifiable_of(legacy["belief"], "") in um.VALID_VERIFIABLE, "")
    check("旧条目缺 source_event_ids / topic_key → _topic_key_of 仍 None",
          um._topic_key_of(legacy) is None, "")

    print("\n[P2-C3] 兼容性：format_model 遇到新 change 值不崩")
    old = mk(1, "我是那种不太爱说话的人", "confirmed", "self_description")
    old["superseded_by"] = "um_x"
    old["superseded_at"] = "2026-10-10T00:00:00+08:00"
    old["revision_history"] = [{"at": "2026-10-10T00:00:00+08:00", "change": "adjudicated",
                                "adjudicated_by": "user", "by": "um_x",
                                "reason": "测试", "evidence": "我没说过…"}]
    out = um.format_model([old])
    check("format_model 渲染新 change 值不抛异常", isinstance(out, str) and bool(out), "")
    check("format_model 显示的是 evidence（不受 change 影响）", "我没说过" in out, "")

    print("\n[P2-C4] 兼容性：_find_correction_target 语义未被改坏")
    # 唯一命中时仍返回目标（不该被 P2 改动影响）
    pool = [mk(1, "我是那种不太爱说话的人", "confirmed", "self_description")]
    inc = claims("我没说过我不太爱说话", 104)[0]
    tgt = um._find_correction_target(pool, inc, rules)
    check("唯一命中时仍返回目标", tgt is not None and tgt["id"] == "um_t0001",
          f"{tgt['belief'] if tgt else None}")
    # 无关联时返回 None
    pool2 = [mk(1, "我在物流专业", "inferred", "objective_attribute")]
    inc2 = claims("我没说过我喜欢猫", 105)[0]
    check("无关联时返回 None", um._find_correction_target(pool2, inc2, rules) is None, "")

    # ======================================================================
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
