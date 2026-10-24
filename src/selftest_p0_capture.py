"""P0 专项自测：观测写入链路的漏捕修复 + 信息类型分类 + 证据语义 + 追溯性。

在**独立临时目录**里跑，绝不碰真实的 conversation/ user_model/。

## 这份测试要证明什么（对应 P0 的六项要求）

1. **客观事实漏捕** —— 「我在物流专业」「我今年二十岁」以前零信号，现在必须被捕获
2. **自我评价漏捕** —— 「我执行力很差」「我害怕失败」以前零信号，现在必须被捕获
3. **信息类型分类** —— 每类信息被分到正确的 `claim_kind`
4. **证据语义** —— 「用户表达已确认」**不等于**「表达内容客观成立」；
   自评/客观属性不得冒充 `confirmed`；注入时必须提示二者不同
5. **历史可追溯性** —— 每条条目能回答「谁说的、哪一轮、命中什么规则、原话是什么」
6. **原有功能回归** —— 疑问句/修正句/意向句仍不得被误收

全部离线、零 API 花费、确定性。

用法：
    python src\\selftest_p0_capture.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

# --- 先建隔离环境，再导入业务模块（与 selftest_companion 同一套做法）----------
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_p0_"))

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

import chat  # noqa: E402
import storage  # noqa: E402
import user_model as um  # noqa: E402
from config import load_settings  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from persona import load_persona  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


class FakeChat:
    """不联网的假客户端。记录每次实际发出的 messages，供注入检查。"""

    model = "fake-chat"

    def __init__(self):
        self.messages: list[list[dict]] = []

    def send(self, messages, model=None) -> Reply:
        self.messages.append(messages)
        return Reply(text="（自测回复）", model=self.model,
                     usage=Usage(prompt_tokens=300, completion_tokens=20, cache_hit_tokens=50),
                     latency_ms=1)


def setup_files() -> None:
    paths.ensure_dirs()
    real = json.loads((SRC.parent / "config" / "settings.json").read_text(encoding="utf-8"))
    paths.SETTINGS_FILE.write_text(
        json.dumps({
            "model": "fake", "temperature": 0.7, "max_tokens": 800, "history_turns": 3,
            "max_tokens_by_purpose": {"chat": 800, "consolidate": 800, "probe": 800},
            "memory": {"top_k": 3},
            "relationship": real["relationship"],
            # ✅ 用**真实**的 user_model 规则（这是被测对象，不能伪造）
            "user_model": real["user_model"],
            "cost": real["cost"],
            "raw_event_triggers": real["raw_event_triggers"],
            "heartbeat": real["heartbeat"],
            "model_router": real.get("model_router", {}),
            "tools": {"allowed_permissions": [], "enabled": [], "max_iterations": 1},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    (TEMP_ROOT / "personality").mkdir(parents=True, exist_ok=True)
    paths.PERSONALITY_FILE.write_text(json.dumps({
        "name": "小满", "description": "测试用", "system_prompt": "你是小满。",
        "preferences": "",
    }, ensure_ascii=False), encoding="utf-8")
    shutil.copy2(SRC.parent / "config" / "prompts" / "consolidate.md",
                 paths.CONSOLIDATE_PROMPT_FILE)


def main() -> int:
    print("=" * 70)
    print("P0 专项自测：观测写入链路漏捕修复")
    print("=" * 70)
    setup_files()
    settings = load_settings()
    rules = {k: v for k, v in settings.user_model.items() if not k.startswith("_")}

    # ========================================================================
    print("\n[P0-1] 自我评价漏捕（修复前：全部无信号）")
    for t in ("我执行力很差", "我害怕失败", "我做不到早起", "我总是拖延", "我很懒"):
        sigs = um.detect_signals(t, rules)
        ok = len(sigs) == 1 and sigs[0]["claim_kind"] == um.CLAIM_SELF_EVALUATION
        check(f"自我评价被捕获并分类：{t}", ok,
              f"得到 {[(s['type'], s.get('claim_kind')) for s in sigs]}")

    print("\n[P0-2] 客观事实漏捕（修复前：全部无信号）")
    for t in ("我在物流专业", "我今年二十岁", "我住在城东", "我老家在南方"):
        sigs = um.detect_signals(t, rules)
        ok = len(sigs) == 1 and sigs[0]["claim_kind"] == um.CLAIM_OBJECTIVE_ATTRIBUTE
        check(f"客观事实被捕获并分类：{t}", ok,
              f"得到 {[(s['type'], s.get('claim_kind')) for s in sigs]}")

    # ========================================================================
    print("\n[P0-3] 信息类型分类正确")
    EXPECT = [
        ("我是那种不太爱说话的人", um.CLAIM_SELF_DESCRIPTION),
        ("我这个人比较内向", um.CLAIM_SELF_DESCRIPTION),
        ("我叫（网名）", um.CLAIM_SELF_DESCRIPTION),
        ("我一般晚上才有精神", um.CLAIM_BEHAVIOR),
        ("我喜欢喝咖啡", um.CLAIM_BEHAVIOR),
        ("我不喜欢社交", um.CLAIM_BEHAVIOR),
        ("我执行力很差", um.CLAIM_SELF_EVALUATION),
        ("我在物流专业", um.CLAIM_OBJECTIVE_ATTRIBUTE),
        ("我是搞设计的", um.CLAIM_UNCERTAIN),      # 句式兜底，未细分
    ]
    for text, want in EXPECT:
        sigs = um.detect_signals(text, rules)
        got = sigs[0].get("claim_kind") if sigs else None
        check(f"分类正确：{text} → {want}", got == want, f"得到 {got!r}")

    # ========================================================================
    print("\n[P0-4] 证据语义：说过 ≠ 客观成立")
    sigs = um.detect_signals("我执行力很差", rules)
    s = sigs[0] if sigs else {}
    # 关键断言：自我评价**不得**被标成 confirmed（那会读成"已确认成立"）
    check("自我评价不得标为 confirmed", s.get("status") != um.STATUS_CONFIRMED,
          f"status={s.get('status')!r}")
    check("自我评价标为 inferred（保守）", s.get("status") == um.STATUS_INFERRED,
          f"status={s.get('status')!r}")
    check("自我评价带 source_kind=stated（确实是用户说的）",
          s.get("source_kind") == um.SOURCE_STATED, f"source_kind={s.get('source_kind')!r}")
    # 客观属性同理：它是"用户陈述的属性"，不是"系统核实的属性"
    sigs2 = um.detect_signals("我在物流专业", rules)
    check("客观属性也不得标为 confirmed",
          sigs2 and sigs2[0].get("status") != um.STATUS_CONFIRMED,
          f"status={sigs2[0].get('status') if sigs2 else None!r}")

    # 注入文本必须把两者分开说清
    items = um.build_claims(um.detect_signals("我执行力很差", rules),
                            session_id="s_x", seq=1, turn_id="t_x",
                            source_text="我执行力很差")
    rendered = um.render_for_prompt(items)
    check("注入时标注了 claim_kind", "self_evaluation" in rendered, rendered[:60])
    check("注入时明确写出「不等于客观事实」", "不等于客观事实" in rendered, "")
    check("注入时提示不要当定论", "定论" in rendered, "")

    # ========================================================================
    print("\n[P0-5] 历史可追溯性")
    sigs = um.detect_signals("我执行力很差", rules)
    claims = um.build_claims(sigs, session_id="sess_trace", seq=42, turn_id="turn_trace",
                             source_text="我执行力很差，真的")
    c = claims[0] if claims else {}
    src = c.get("source") or {}
    check("记了 source_kind", src.get("source_kind") == um.SOURCE_STATED, str(src.get("source_kind")))
    check("记了 claim_kind", src.get("claim_kind") == um.CLAIM_SELF_EVALUATION, "")
    check("记了命中的规则/关键词", src.get("rule") and src.get("matched"), f"rule={src.get('rule')!r}")
    check("记了会话与轮次", src.get("session_id") == "sess_trace" and src.get("seq") == 42
          and src.get("turn_id") == "turn_trace", "")
    check("记了触发判断的**原话**（含上下文，不只是抽取片段）",
          src.get("evidence") == "我执行力很差，真的", f"evidence={src.get('evidence')!r}")
    # 兜底捞出来的，rule 必须标 fallback（便于事后区分"有关键词依据"与"句式兜底"）
    c2 = um.build_claims(um.detect_signals("我是搞设计的", rules),
                         session_id="s", seq=1, turn_id="t", source_text="我是搞设计的")
    check("兜底条目标 rule=fallback",
          c2 and (c2[0]["source"] or {}).get("rule") == "fallback",
          f"rule={(c2[0]['source'].get('rule') if c2 else None)!r}")

    # ========================================================================
    print("\n[P0-6] 原有功能回归：不该收的仍然不收")
    for t in ("我是想问你", "我是说这样", "我打算明天去", "我想去散步",
              "我喜欢喝啥", "我一般几点睡", "我执行力很差吗？",
              "我没说过我不太爱说话"):
        sigs = um.detect_signals(t, rules)
        # 修正句是**允许**产生信号的（它是指令），其余必须零信号
        if "我没说过" in t:
            check(f"修正句仍被识别为 correction：{t}",
                  len(sigs) == 1 and sigs[0]["type"] == "correction", f"{sigs}")
        else:
            check(f"不得误收：{t}", len(sigs) == 0, f"得到 {[(s['type'], s['claim']) for s in sigs]}")

    print("\n[P0-6b] 指令/请求句闸门（历史重放抓到的真实假阳性）")
    # 这一条来自真实历史 seq=128：一条「帮我读取文件」的指令被 `我读` 误判成
    # 「用户的客观属性」。它不是虚构用例 —— 是从 144 轮真实对话重放里捞出来的。
    cmd_cases = [
        "先帮我读取 README.md，再读取当前项目的配置文件，然后告诉我这两个文件"
        "对项目当前阶段的描述有没有矛盾",          # 真实 seq=128 原文
        "帮我读一下 README",
        "请你运行一下测试",
        "麻烦你改成这样",
        "先读取配置文件",
        "然后告诉我结果",
    ]
    for t in cmd_cases:
        sigs = um.detect_signals(t, rules)
        check(f"指令句不得被当成关于用户的陈述：{t[:24]}…", len(sigs) == 0,
              f"得到 {[(s['type'], s.get('claim_kind')) for s in sigs]}")
    # 反向保护：修闸门时**不得**误杀自述句。
    # 我在这上面连续踩了三次（单字泛词 / 动作词无条件下标 / 白名单前缀），
    # 所以把「含动作词的自评」单独立成用例锁住。
    for t, want in (("我执行力很差", um.CLAIM_SELF_EVALUATION),
                    ("我总是拖延", um.CLAIM_SELF_EVALUATION),
                    ("我做不到早起", um.CLAIM_SELF_EVALUATION)):
        sigs = um.detect_signals(t, rules)
        got = sigs[0].get("claim_kind") if sigs else None
        check(f"闸门不得误杀含动作词的自述：{t}", got == want, f"得到 {got!r}")

    print("\n[P0-7] 端到端：写入 → 读回 → 可追溯")
    conn = storage.connect()
    persona = load_persona()
    client = FakeChat()
    session = chat.new_session(persona=persona, settings=settings, client=client,
                               peak_override=False, conn=conn, router=None)
    try:
        for text in ("我执行力很差", "我在物流专业"):
            chat.send_once(session, text, echo=lambda *_: None)
        snap = um.load_latest_snapshot()
        items = (snap or {}).get("items") or []
        beliefs = [x.get("belief") for x in items]
        check("端到端：自我评价进了用户模型", "我执行力很差" in beliefs, f"{beliefs}")
        check("端到端：客观属性进了用户模型", "我在物流专业" in beliefs, f"{beliefs}")
        ev = [x for x in items if x.get("belief") == "我执行力很差"]
        check("端到端：分类随条目落盘",
              ev and (ev[0].get("source") or {}).get("claim_kind") == um.CLAIM_SELF_EVALUATION, "")
        check("端到端：原话随条目落盘（可追溯）",
              ev and (ev[0].get("source") or {}).get("evidence") == "我执行力很差", "")
        # user_model_events 也要能查到
        rows = storage.list_user_model_events(conn) if hasattr(storage, "list_user_model_events") else []
        kinds = [r["kind"] for r in rows]
        check("端到端：变更事件已落 user_model_events 表", len(rows) >= 2, f"rows={len(rows)} kinds={kinds}")
        # 注入文本里两项都要在，且自评被明确标注
        rendered = um.render_for_prompt(items)
        check("端到端：注入文本含自评警示", "不等于客观事实" in rendered, "")
    finally:
        conn.close()

    # ========================================================================
    print("\n[P0-8] uncertain：从死枚举变成真的会被产生")
    # 修复前：status 有三种取值，但 uncertain 只有常量定义、**零产出、零读取**
    # （赋值点 1 = 定义本身；读取点 0）。这一节证明它现在两样都有了。
    UNCS = [
        "我可能有点社恐", "我好像不太擅长社交", "我大概是个内向的人",
        "我有时会拖延", "我不确定自己是不是喜欢咖啡", "我算是比较安静的人",
        "我想我可能是内向的",
    ]
    for t in UNCS:
        sigs = um.detect_signals(t, rules)
        got = sigs[0]["status"] if sigs else None
        check(f"犹疑表达产生 uncertain：{t}", got == um.STATUS_UNCERTAIN, f"status={got!r}")

    # 反面：**明确**的陈述不得被降级成 uncertain
    # （否则等于把确定的东西说成不确定，是另一种失真）
    for t in ("我执行力很差", "我喜欢喝咖啡", "我叫（网名）"):
        sigs = um.detect_signals(t, rules)
        got = sigs[0]["status"] if sigs else None
        check(f"明确陈述不得降级为 uncertain：{t}",
              got in (um.STATUS_INFERRED, um.STATUS_CONFIRMED), f"status={got!r}")

    # 修正句是「作废既有判断」的指令，用词犹疑不代表这条否定本身不确定
    sigs = um.detect_signals("我好像没说过我不太爱说话", rules)
    check("修正句不因犹疑而降级",
          bool(sigs) and sigs[0]["type"] == "correction" and sigs[0]["status"] == um.STATUS_CONFIRMED,
          f"{[(s['type'], s['status']) for s in sigs]}")

    check("uncertain 仍是合法 status（取值范围未被改坏）",
          um.STATUS_UNCERTAIN in um.VALID_STATUS, "")

    print("\n[P0-9] uncertain 的**消费**：status 真的被读来改变行为")
    # ⚠️ 这是"决策机制"的关键：以前**没有任何**分支读 status。

    def _mk(i, belief, status, kind="self_description", conf=None):
        return {"id": f"um_{i:04d}", "belief": belief, "status": status,
                "confidence": conf if conf is not None else (0.9 if status == "confirmed" else 0.5),
                "source": {"kind": "stated", "source_kind": "stated", "claim_kind": kind,
                           "evidence": belief, "seq": i, "turn_id": f"t{i}", "session_id": "s",
                           "matched": [], "rule": "stated"},
                "last_updated": "2026-10-09T20:00:00+08:00", "revision": 1,
                "superseded_by": None, "superseded_at": None, "revision_history": []}

    mixed = [_mk(i, f"确定{i}", "confirmed") for i in range(6)]
    mixed += [_mk(i + 100, f"不确定{i}", "uncertain") for i in range(6)]
    txt = um.render_for_prompt(mixed, limit=8)

    # 决策 1：不确定的**单独成段**
    check("uncertain 单独成段（不混在已确认的列表里）",
          "还不确定" in txt and "不要当成事实用" in txt, "")
    # 决策 2：改写措辞 —— 要求问用户而不是猜
    check("要求「直接问用户」而不是自己推测",
          "直接问用户" in txt and "不要把它当定论" in txt, "")
    # 决策 3：限制占比 —— 不确定项不许挤掉已确认的信息
    n_c = sum(1 for l in txt.splitlines() if l.startswith("- [confirmed"))
    n_u = sum(1 for l in txt.splitlines() if l.startswith("- [uncertain"))
    check("已确认条目没有被不确定项挤掉", n_c >= 1, f"确定 {n_c} / 不确定 {n_u}")
    check("不确定项不超过总槽位一半", n_u <= 8 // 2, f"不确定 {n_u} 条（上限 4）")
    check("槽位没有超用", n_c + n_u <= 8, f"合计 {n_c + n_u} > 8")
    only_u = [_mk(i, f"只有不确定{i}", "uncertain") for i in range(3)]
    txt_u = um.render_for_prompt(only_u, limit=8)
    check("全是 uncertain 时仍注入且带警示", "还不确定" in txt_u and "不确定0" in txt_u, "")

    print("\n[P0-10] UNCERTAIN_EVENT_THRESHOLD：只标记，不升级")
    # 用户明确否掉「够次数就自动升级」。所以阈值只能用来**提请人工复核**。

    def _ev(i, belief, evidence):
        x = _mk(i, belief, "uncertain")
        x["source"]["evidence"] = evidence
        return x

    same = [_ev(1, "我可能社恐", "我可能有点社恐"), _ev(1, "我可能社恐", "我可能有点社恐")]
    check("同一段原话重复引用只算 1 段独立证据",
          um.independent_evidence(same, "um_0001") == 1,
          f"得到 {um.independent_evidence(same, 'um_0001')}")
    diff = [_ev(1, "我可能社恐", "我可能有点社恐"), _ev(1, "我可能社恐", "我好像怕跟人说话")]
    check("两段不同原话算 2 段独立证据",
          um.independent_evidence(diff, "um_0001") == 2,
          f"得到 {um.independent_evidence(diff, 'um_0001')}")
    # ⚠️ 复核候选是**按条目**列的。
    #    上面 `diff` 里两条共用同一个 id，那是不现实的构造
    #    （真实流程里 `merge_claims` 会按「同 status + 同文本」去重成一一条）。
    #    所以这里用一个**单一**条目、证据来自两段不同原话的构造来验。
    one = _ev(7, "我可能社恐", "我可能有点社恐")
    one["source"]["evidence"] = "我可能有点社恐"      # 条目上挂的是最近一段...
    cands = um.review_candidates([one])
    # 单条目只有 1 段证据 → 不够阈值（阈值 2）
    check("单条目只有 1 段证据时不够阈值",
          um.review_candidates([one]) == [], f"{um.review_candidates([one])}")
    # 构造"够阈值"：同一条目 id 下挂两段不同原话（模拟多轮累积后的快照）
    acc = _ev(8, "我可能社恐", "我好像怕跟人说话")
    acc2 = dict(acc)
    acc2["source"] = dict(acc["source"])
    acc2["source"]["evidence"] = "我可能有点社恐"
    cands = um.review_candidates([acc, acc2])
    check("够阈值时进入复核候选", len(cands) >= 1 and cands[0]["independent"] == 2, f"{cands}")
    # ★ 关键：进入候选**不得改变任何东西**（不是自动升级）
    check("复核候选不改变 status（只标记不升级）",
          bool(cands) and cands[0]["item"]["status"] == um.STATUS_UNCERTAIN,
          f"status={cands[0]['item']['status'] if cands else None!r}")
    check("复核候选不改变 confidence",
          bool(cands) and cands[0]["item"]["confidence"] == 0.5,
          f"confidence={cands[0]['item']['confidence'] if cands else None}")
    check("不够阈值时不进候选", um.review_candidates([one]) == [], f"{um.review_candidates([one])}")
    fmt = um.format_model([acc, acc2])
    check("usermodel 输出提示了复核候选", "复核" in fmt, "")
    check("usermodel 输出里明确写了「次数≠可信度」", "次数≠可信度" in fmt, "")

    print("\n[P0-11] 意向闸门：打算做的事 ≠ 我是什么样的人")
    for t in ("我想去散步", "我打算明天去", "我准备睡觉", "我要去买东西"):
        sigs = um.detect_signals(t, rules)
        check(f"意向句不得进用户模型：{t}", len(sigs) == 0,
              f"得到 {[(s['type'], s['claim']) for s in sigs]}")

    # ========================================================================
    print("\n" + "=" * 70)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        if not good:
            print(f"  [失败] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 70)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本自测完全离线，未产生任何真实 API 费用。")
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
