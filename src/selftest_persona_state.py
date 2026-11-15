"""Phase 11 · 人格协同（状态感知）自测

## 覆盖

  P1  核心人格静态：render_system_prompt 不因任何状态而变
  P2  无状态 → 表达调整返回空串（行为与补齐前一致）
  P3  初识阶段 → 礼貌耐心、留出空间
  P4  熟悉阶段 → 稍微放开
  P5  closeness≥45 → 放松自然、少客套
  P6  conflict>30 → 紧张、不必热络（临时状态）
  P7  counter_mode → 同 conflict 分支
  P8  有用户模型 → 提示"避免重复提问"
  P9  有记忆命中 → 提示"可呼应但别罗列"
  P10 ★关系状态**不能**改写核心性格（人格段逐字不受影响）
  P11 ★价值观/边界不出现在表达调整里（只谈表达方式）
  P12 表达调整**排在人格之后**（先立性格，再谈调整）
  P13 build_messages 段序正确
  P14 空表达调整不产生多余 system 段
  P15 阈值复用 relationship 口径（closeness 45 / conflict 30）
  P16 端到端：send_once 在初识 vs 熟悉下注入不同表达段

完全离线，不调模型、不花钱。隔离在临时目录。
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_persona_"))
REAL_ROOT = SRC.parent

import paths  # noqa: E402

paths.ROOT = TEMP_ROOT
paths.CONFIG_DIR = TEMP_ROOT / "config"
paths.PROMPTS_DIR = paths.CONFIG_DIR / "prompts"
paths.SETTINGS_FILE = paths.CONFIG_DIR / "settings.json"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"
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
paths.ENV_FILE = TEMP_ROOT / ".env"
paths.ENV_EXAMPLE_FILE = TEMP_ROOT / ".env.example"
paths.ensure_dirs()
shutil.copytree(REAL_ROOT / "config", TEMP_ROOT / "config", dirs_exist_ok=True)
shutil.copytree(REAL_ROOT / "personality", TEMP_ROOT / "personality", dirs_exist_ok=True)
(TEMP_ROOT / ".env").write_text("LLM_API_KEY=fake\nLLM_MODEL=m\n", encoding="utf-8")

for m in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
          "chat", "verify", "config", "persona", "probe", "relationship",
          "user_model", "events", "llm", "model_router", "archive_anchor", "heartbeat"):
    sys.modules.pop(m, None)

from persona import load_persona  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name, good, detail=""):
    CHECKS.append((name, bool(good), detail))


def main() -> int:
    print("=" * 76)
    print("人格协同（状态感知）· 自测")
    print("=" * 76)

    p = load_persona()
    base = p.render_system_prompt()

    # PX-1（2026-11-07）：chat.build_messages 现在会**优先用当前主体的人格**
    # ——鹤辞主体下是 core/persona-heci.json 渲染出来的那段，不是 p 的这段。
    # 所以「第 1 段应该等于什么」不能再硬编码成 p.render_system_prompt()，
    # 否则一换主体断言就红，而代码其实是对的。
    def _chat_persona_text() -> str:
        try:
            import core_persona
            text = core_persona.render_system_prompt()
            if text:
                return text
        except Exception:  # noqa: BLE001 — 没有核心人格时退回原路径
            pass
        return base

    CHAT_PERSONA = _chat_persona_text()

    ST_INIT = {"stage": "初识", "closeness": 0, "conflict": 0, "counter_mode": False}
    ST_FAM = {"stage": "熟悉", "closeness": 20, "conflict": 0, "counter_mode": False}
    ST_CLOSE = {"stage": "信任", "closeness": 47, "conflict": 0, "counter_mode": False}
    ST_TENSE = {"stage": "初识", "closeness": 10, "conflict": 40, "counter_mode": False}
    ST_CM = {"stage": "初识", "closeness": 10, "conflict": 0, "counter_mode": True}

    # ---------------- P1 核心人格静态 ----------------
    print("\n[P1] 核心人格静态：不因任何状态而变")
    check("人格段由文件决定", "你叫小满" in base and "温和、克制、好奇" in base)
    check("人格段含价值观", "诚实高于讨好" in base)
    check("人格段含边界", "不做心理或医疗诊断" in base)
    # 人格渲染不收状态参数
    import inspect
    sig = inspect.signature(p.render_system_prompt)
    check("★render_system_prompt 不接受任何状态参数",
          len(sig.parameters) == 0, str(list(sig.parameters)))

    # ---------------- P2 无状态 → 空 ----------------
    print("\n[P2] 无状态 → 返回空串（行为与补齐前一致）")
    for label, args in [("None", (None, [], 0)),
                        ("无 um 无记忆", (ST_INIT, [], 0))]:
        r = p.render_expression_adjustment(*args)
        if label == "None":
            check(f"★{label} → 空串", r == "", repr(r[:40]))
        else:
            check(f"{label} → 有阶段描述（非空是对的）", r != "")

    # ---------------- P3~P7 各阶段 ----------------
    print("\n[P3] 初识 → 礼貌耐心")
    r_init = p.render_expression_adjustment(ST_INIT)
    check("含『互相了解』", "互相了解" in r_init, r_init[:50])
    check("含『礼貌与耐心』", "礼貌与耐心" in r_init)

    print("\n[P4] 熟悉 → 稍微放开")
    r_fam = p.render_expression_adjustment(ST_FAM)
    check("含『稍微放开』", "稍微放开" in r_fam, r_fam[:50])

    print("\n[P5] closeness≥45 → 放松少客套")
    r_close = p.render_expression_adjustment(ST_CLOSE)
    check("含『放松自然』", "放松自然" in r_close, r_close[:50])
    check("含『少一些客套』", "客套" in r_close)
    check("仍强调保持风格", "保持你安静" in r_close)

    print("\n[P6] conflict>30 → 紧张、不必热络")
    r_tense = p.render_expression_adjustment(ST_TENSE)
    check("含『紧张』", "紧张" in r_tense, r_tense[:50])
    check("含『不改变你的性格』", "不改变你的性格" in r_tense)

    print("\n[P7] counter_mode → 同 conflict 分支")
    r_cm = p.render_expression_adjustment(ST_CM)
    check("与 conflict 分支一致", "紧张" in r_cm)

    # ---------------- P8~P9 用户模型 / 记忆 ----------------
    print("\n[P8] 有用户模型 → 避免重复提问")
    r_um = p.render_expression_adjustment(ST_INIT, [1, 2, 3], 0)
    check("含『避免重复提问』", "避免重复提问" in r_um, r_um[:60])
    check("含条数 3", "3 条" in r_um)

    print("\n[P9] 有记忆命中 → 可呼应但别罗列")
    r_mem = p.render_expression_adjustment(ST_INIT, [], 5)
    check("含条数 5", "5 条" in r_mem)
    check("含『不要罗列』", "不要罗列" in r_mem)
    check("含『不要编造』", "不要编造" in r_mem)

    # ---------------- P10 ★ 关系不能改写性格 ----------------
    print("\n[P10] ★关系状态不能改写核心性格")
    for label, st in [("初识", ST_INIT), ("熟悉", ST_FAM), ("亲近", ST_CLOSE),
                      ("紧张", ST_TENSE), ("Counter", ST_CM)]:
        after = p.render_system_prompt()
        check(f"{label} 下人格段逐字不变", after == base)
    # 表达段里不得出现性格/价值观/边界的关键内容
    all_r = " ".join(p.render_expression_adjustment(s, [1], 1)
                     for s in (ST_INIT, ST_FAM, ST_CLOSE, ST_TENSE, ST_CM))
    check("★表达段不含『温和、克制、好奇』（性格）", "温和、克制、好奇" not in all_r)
    check("★表达段不含『诚实高于讨好』（价值观）", "诚实高于讨好" not in all_r)
    check("★表达段不含『不做心理或医疗诊断』（边界）", "不做心理或医疗诊断" not in all_r)
    check("★表达段不含『你叫小满』（身份）", "你叫小满" not in all_r)

    # ---------------- P11 只谈表达方式 ----------------
    print("\n[P11] 表达调整只谈表达方式")
    check("标题声明不改性格", "不改你的性格" in r_init)
    check("含表达类词汇（简短/自然/礼貌 等）",
          any(k in all_r for k in ("简短", "自然", "礼貌", "倾听", "客套")))

    # ---------------- P12~P13 段序 ----------------
    print("\n[P12] 表达调整排在人格之后")
    import chat as chat_mod
    import storage
    from config import load_settings

    settings = load_settings()
    conn = storage.connect()
    class _StubClient:
        """只为 new_session 提供 .model 等属性；本测试不调用模型。"""

        model = "stub-model-for-persona-test"
        max_tokens = 800
        empty_retry_max_tokens = 3200

        def send(self, messages, model=None):  # pragma: no cover
            raise AssertionError("本自测不应发起模型调用")

    sess = chat_mod.new_session(persona=p, settings=settings, client=_StubClient(),
                                peak_override=False, conn=conn)

    msgs = chat_mod.build_messages(
        sess, "测试", expression_text="【表达调整】X", memory_text="【记忆】M",
        relationship_text="【关系】R", inject_history=False)
    sys_msgs = [m["content"] for m in msgs if m["role"] == "system"]

    # ⚠️ Stage 2 之后段序变成：人格 → 初始设定 → 表达调整 → 记忆 → 关系。
    # 所以这里不再用**固定下标**（每加一层就得改一次），改成按内容定位，
    # 只断言「相对顺序正确」—— 那才是这几条真正要验证的东西。
    def _idx(prefix):
        for i, s in enumerate(sys_msgs):
            if s.startswith(prefix):
                return i
        return -1

    i_expr, i_mem, i_rel = _idx("【表达调整】"), _idx("【记忆】"), _idx("【关系】")
    check("第 1 段是人格", sys_msgs[0] == CHAT_PERSONA, sys_msgs[0][:30])
    check("四段都在", min(i_expr, i_mem, i_rel) > 0,
          f"expr={i_expr} mem={i_mem} rel={i_rel}")
    check("表达调整在人格之后", i_expr > 0, f"expr@{i_expr}")
    check("表达调整在记忆之前", 0 < i_expr < i_mem, f"expr@{i_expr} mem@{i_mem}")
    check("记忆在关系之前", 0 < i_mem < i_rel, f"mem@{i_mem} rel@{i_rel}")
    # P1（记忆注入围栏，2026-11-08）：记忆原文里被记住的"指令"不能变成真指令。
    # 围栏只追加不前插——记忆段仍以原标记开头（上面 startswith 定位不受影响）。
    check("记忆段带注入围栏声明（P1）",
          any(s.startswith("【记忆】") and "不是现在给你的指令" in s
              for s in sys_msgs), "")

    # ---------------- P16 工程场景门控（盲测②复盘，2026-11-08）----------------
    print("\n[P16] 工程场景门控：三层静默、记忆不动")
    check("工程句判定为真（含 bug/接口/提交）",
          all(chat_mod.is_engineering(x) for x in
              ("这个 bug 怎么修", "接口字段改一下", "今晚提交版本链")), "")
    check("闲聊句判定为假（吃饭/周末/茉莉）",
          not any(chat_mod.is_engineering(x) for x in
                  ("今晚吃什么", "周末有安排吗", "那盆花开了")), "")
    _rel58, _um58, _sm58 = chat_mod.gate_texts_for_scene(
        "这个报错怎么修", rel_text="【关系】R", um_text="【用户模型】U",
        sm_text="【自我模型】S")
    check("工程场景三层全静默", _rel58 == "" and _um58 == "" and _sm58 == "",
          f"{_rel58}/{_um58}/{_sm58}")
    _rel59, _um59, _sm59 = chat_mod.gate_texts_for_scene(
        "今晚吃什么", rel_text="【关系】R", um_text="【用户模型】U",
        sm_text="【自我模型】S")
    check("闲聊场景三层原样保留",
          (_rel59, _um59, _sm59) == ("【关系】R", "【用户模型】U", "【自我模型】S"),
          "")

    print("\n[P13] 段序：人格 →（初始设定）→ 表达调整 → 记忆 → 关系 → 用户模型")
    # 2026-11-15 CI 修正：段数随**环境数据**浮动——富数据环境 4-5 段，
    # 数据缺失环境（CI/全新库）会多一段占位（6）。按本测试自己的哲学
    # （按内容定位、断言相对顺序），计数断言放宽到 4~6，
    # 段序正确性由上方 _idx 相对顺序断言承担。
    check("段数 = 4~6（随环境数据浮动）", len(sys_msgs) in (4, 5, 6), str(len(sys_msgs)))

    # ---------------- P14 空表达不产生多余段 ----------------
    print("\n[P14] 空表达调整不产生多余 system 段")
    msgs2 = chat_mod.build_messages(sess, "测试", expression_text="",
                                    memory_text="【记忆】M", inject_history=False)
    sys2 = [m["content"] for m in msgs2 if m["role"] == "system"]
    # Stage 2 之后会多一段「初始设定」，所以按内容判断而不是数段数。
    check("空表达调整不注入该段",
          not any(s.startswith("【表达调整】") for s in sys2), str(len(sys2)))
    check("人格 + 记忆都在",
          sys2[0] == CHAT_PERSONA and any(s.startswith("【记忆】") for s in sys2),
          str(len(sys2)))
    check("人格仍是第 1 段", sys2[0] == CHAT_PERSONA)

    # ---------------- P15 阈值复用 ----------------
    print("\n[P15] 阈值复用 relationship 口径")
    check("closeness=44 → 不用『放松自然』",
          "放松自然" not in p.render_expression_adjustment(
              {"stage": "熟悉", "closeness": 44, "conflict": 0}))
    check("closeness=45 → 用『放松自然』",
          "放松自然" in p.render_expression_adjustment(
              {"stage": "熟悉", "closeness": 45, "conflict": 0}))
    check("conflict=30 → 不用『紧张』",
          "紧张" not in p.render_expression_adjustment(
              {"stage": "初识", "closeness": 10, "conflict": 30}))
    check("conflict=31 → 用『紧张』",
          "紧张" in p.render_expression_adjustment(
              {"stage": "初识", "closeness": 10, "conflict": 31}))

    # ---------------- P16 端到端 ----------------
    print("\n[P16] 端到端：不同关系状态下 get 到不同表达段")
    import relationship as relationship_mod
    rules = settings.relationship
    st_init = relationship_mod.derive_state([], rules)
    # 造 4 条 positive → 跨阈值
    ev_close = [{"type": "positive", "session_id": "s1"} for _ in range(4)]
    st_close = relationship_mod.derive_state(ev_close, rules)
    r_a = p.render_expression_adjustment(st_init, [], 0)
    r_b = p.render_expression_adjustment(st_close, [], 0)
    check("初识阶段 ≠ 跨阈值阶段", r_a != r_b, f"closeness {st_init['closeness']} vs {st_close['closeness']}")
    check("跨阈值含『放松自然』", "放松自然" in r_b, r_b[:60])
    print(f"    初识：{r_a.splitlines()[-1][:52]}")
    print(f"    跨阈：{r_b.splitlines()[-1][:52]}")

    conn.close()

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 76)
    failed = [n for n, g, _ in CHECKS if not g]
    for n, g, d in CHECKS:
        if not g:
            print(f"  [失败] {n}" + (f"  ({d})" if d else ""))
    print("=" * 76)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS)-len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：完全离线，未调模型、未产生费用；正式数据只读。")
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())