"""Phase 6（关系）+ Phase 7（用户模型）离线自测。

在**独立临时目录**里跑，绝不碰项目真实的 conversation/ relationship/ user_model/。

## 为什么这两块合在一起测

它们都是「伴随对话演化、需要跨多轮才看得出效果」的状态层，
共用同一套隔离环境与假客户端。分开写两份会重复一遍脚手架（违反「能省就省」）。

## 各自要证明的硬要求

- **Phase 6**（总项目指令 §4）：关系状态能**保存、读取，并因实际互动事件而变化**
- **Phase 7**（总项目指令 §7 / §28）：用户模型能形成「她眼中的用户」，
  且必须允许**正确理解 / 错误理解 / 不确定 / 修正**

用法：
    python src\\selftest_companion.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

# --- 先建隔离环境，再导入业务模块 -------------------------------------------
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_companion_"))

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

for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe", "relationship",
            "user_model", "events", "llm"):
    sys.modules.pop(mod, None)

import chat  # noqa: E402
import relationship as rel_mod  # noqa: E402
import storage  # noqa: E402
import user_model as um_mod  # noqa: E402
from config import load_settings  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from persona import load_persona  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def _persona_marks() -> tuple[str, ...]:
    """当前对话可能用哪种人格标记（PX-1：不再硬编码「小满」）。

    鹤辞主体下 `chat.build_messages` 优先用 `core/persona-heci.json` 渲染的人格，
    第一句是「你是鹤辞。」；小满主体下是 persona 文件里的「你叫小满」。
    断言只关心「人格段在不在」，不该锁死某一个人的名字。
    """
    marks: list[str] = []
    try:
        import core_persona

        p = core_persona.load_persona()
        if p and p.get("name"):
            n = str(p["name"])
            marks += [f"你是{n}", f"你叫{n}"]
    except Exception:  # noqa: BLE001
        pass
    marks += ["你叫小满", "你是小满"]
    return tuple(marks)


def _has_persona(texts) -> bool:
    marks = _persona_marks()
    return any(any(m in t for m in marks) for t in texts)


class FakeChat:
    """不联网的假客户端。记录每次实际发出的 messages，供注入检查。"""

    model = "fake-chat"

    def __init__(self):
        self.messages: list[list[dict]] = []

    def send(self, messages, model=None) -> Reply:
        self.messages.append(messages)
        return Reply(text="（自测回复）", model=self.model,
                     usage=Usage(prompt_tokens=500, completion_tokens=30, cache_hit_tokens=100),
                     latency_ms=2)


def setup_files() -> None:
    paths.ensure_dirs()
    real = json.loads((SRC.parent / "config" / "settings.json").read_text(encoding="utf-8"))
    paths.SETTINGS_FILE.write_text(
        json.dumps({
            "model": "fake-chat",
            "temperature": 0.7,
            "max_tokens": 800,
            "history_turns": 10,
            "memory": {"top_k": 3},
            "max_tokens_by_purpose": {"chat": 800},
            "cost": {"models": {"fake-chat": {"cache_hit": 0.04, "input": 2.0, "output": 8.0}}},
            # 直接复用项目里的真触发词与真规则，确保测的是实际会生效的那套
            "raw_event_triggers": {"keywords": real["raw_event_triggers"]["keywords"]},
            # 直接复用项目里的真规则，确保测的是实际会生效的那套
            "relationship": real["relationship"],
            "user_model": real["user_model"],
            "heartbeat": real["heartbeat"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    paths.PERSONALITY_FILE.write_text(
        json.dumps({"name": "小满", "description": "测试人格",
                    "system_prompt": "你叫{name}。{description}"}, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> int:
    # Windows GBK 控制台打不出 emoji（⏰ 提醒消息会进 check 详情）→ 不可编码字符
    # 替换为 ?，别让测试崩在「打印结果」而不是「断言本身」。不改 encoding，
    # 只放宽 errors，避免重新包装 stdout 的孤儿句柄问题。
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 环境差异兜底
            pass
    print("=" * 68)
    print("Phase 6（关系）+ Phase 7（用户模型）离线自测")
    print(f"隔离临时目录：{TEMP_ROOT}")
    print("=" * 68)

    setup_files()
    conn = storage.connect()
    persona = load_persona()
    settings = load_settings()
    rel_rules = settings.relationship
    um_rules = settings.user_model

    # ========================================================================
    # Phase 6 关系
    # ========================================================================
    print("\n" + "=" * 68)
    print("Phase 6 · 关系状态")
    print("=" * 68)

    print("\n[6.1] 状态能保存与读取")
    empty = rel_mod.current_state(conn, rel_rules)
    check("初始阶段为「初识」", empty["stage"] == "初识", empty["stage"])
    check("初始各数值为 0",
          all(empty[k] == 0 for k in ("trust", "closeness", "familiarity", "conflict")),
          str({k: empty[k] for k in ("trust", "closeness", "familiarity")}))
    saved = rel_mod.save_snapshot(empty, note="初始状态")
    check("快照可落盘", Path(TEMP_ROOT, saved["file"]).exists(), saved["file"])
    loaded = rel_mod.load_latest_snapshot()
    check("快照可读回且内容一致", loaded is not None and loaded["state"] == empty,
          f"version={loaded['version'] if loaded else None}")
    saved2 = rel_mod.save_snapshot(empty, note="第二份")
    check("快照版本递增而非覆盖", saved2["version"] == "v002", saved2["version"])
    check("旧快照内容未被改动",
          "初始状态" in Path(TEMP_ROOT, saved["file"]).read_text(encoding="utf-8"), "")
    print(f"  初始：{empty['stage']}  快照 {saved['version']} → {saved2['version']}（不覆盖）")

    print("\n[6.2] 状态因实际互动事件而变化")
    fake = FakeChat()
    session = chat.new_session(persona=persona, settings=settings, client=fake,
                              peak_override=False, conn=conn)
    for text, expect in [
        ("谢谢你，跟你聊挺舒服的", "positive"),
        ("今天多亏你帮忙，我们一起把它弄完了", "shared_experience"),
        ("你懂我，有你在真好", "positive"),
        ("谢谢你一直陪我聊这些", "positive"),
    ]:
        r = chat.send_once(session, text, echo=lambda *_: None)
        types = [e["type"] for e in r["relationship_events"]]
        check(f"「{text[:12]}…」判为 {expect}", expect in types, str(types))
    after = rel_mod.current_state(conn, rel_rules)
    check("关系状态确实变了（不再全 0）",
          after["trust"] > 0 and after["closeness"] > 0 and after["familiarity"] > 0,
          f"trust={after['trust']} closeness={after['closeness']}")
    check("阶段从「初识」推进", after["stage"] != "初识", after["stage"])
    print(f"  4 轮正面互动 → 阶段={after['stage']}  信任={after['trust']}  "
          f"亲近={after['closeness']}  事件={after['counts']}")

    print("\n[6.3] 状态注入未来对话")
    sys_texts = [m["content"] for m in fake.messages[-1] if m["role"] == "system"]
    check("关系段以独立 system 段注入",
          any("你与用户的关系" in t for t in sys_texts), f"system 段数={len(sys_texts)}")
    check("关系段含当前阶段", any(after["stage"] in t for t in sys_texts), after["stage"])
    check("注入可读描述而非裸数值",
          not any("trust=" in t for t in sys_texts), "")
    print(f"  system 段数={len(sys_texts)}；关系段首行："
          f"{[t for t in sys_texts if '你与用户的关系' in t][0].splitlines()[1]}")

    print("\n[6.4] 状态是纯函数（可重放）")
    a = rel_mod.derive_state([{"type": "positive", "session_id": "s1"}] * 3, rel_rules)
    b = rel_mod.derive_state([{"type": "positive", "session_id": "s1"}] * 3, rel_rules)
    check("重复推导结果完全一致（无计数器自增）", a == b, f"trust={a['trust']}")
    print(f"  3 条 positive → trust={a['trust']}（两次一致）")

    print("\n[6.5] Counter Mode（§17）")
    sid = "sess_conflict_test"
    one = rel_mod.derive_state([{"type": "conflict", "session_id": sid}], rel_rules)
    two = rel_mod.derive_state([{"type": "conflict", "session_id": sid}] * 2, rel_rules)
    split = rel_mod.derive_state(
        [{"type": "conflict", "session_id": "sA"}, {"type": "conflict", "session_id": "sB"}],
        rel_rules)
    check("单次冲突不触发", one["counter_mode"] is False, "")
    check("同会话两次冲突触发", two["counter_mode"] is True, "")
    check("跨会话各一次不触发（临时状态不延续）", split["counter_mode"] is False, "")
    check("冲突会记录边界", len(two["boundaries"]) >= 1, str(two["boundaries"])[:40])
    mode_text = rel_mod.render_for_prompt(two)
    check("Counter Mode 注入含「临时状态」定性", "临时状态" in mode_text, "")
    check("Counter Mode 注入含不泄露约束", "泄露" in mode_text, "")
    print(f"  1 次={one['counter_mode']}　同会话 2 次={two['counter_mode']}　"
          f"跨会话各 1 次={split['counter_mode']}")

    print("\n[6.6] 通过对话真实触发并落盘")
    chat.send_once(session, "你真蠢，闭嘴", echo=lambda *_: None)
    chat.send_once(session, "废物，滚", echo=lambda *_: None)
    counts = storage.relationship_event_counts(conn)
    check("冲突事件已落盘", counts["conflict"] >= 2, str(counts))
    check("通过对话触发后进入 Counter Mode",
          rel_mod.current_state(conn, rel_rules)["counter_mode"] is True, "")
    rel_rows = storage.list_relationship_events(conn)
    check("事件带原文与命中词",
          all(r["evidence"] and json.loads(r["matched"]) for r in rel_rows), f"{len(rel_rows)} 条")
    check("事件挂了 turn_id 与 seq",
          all(r["turn_id"] and r["seq"] is not None for r in rel_rows), "")
    print(f"  事件：{counts}")

    # ========================================================================
    # Phase 7 用户模型
    # ========================================================================
    print("\n" + "=" * 68)
    print("Phase 7 · 用户模型（她眼中的用户）")
    print("=" * 68)

    print("\n[7.1] 判定信号：正确理解 / 推断 / 不确定")
    sigs_self = um_mod.detect_signals("我是那种不太爱说话的人", um_rules)
    check("自我描述判为 confirmed",
          sigs_self and sigs_self[0]["status"] == um_mod.STATUS_CONFIRMED,
          str(sigs_self[0]["status"]) if sigs_self else "无")
    sigs_beh = um_mod.detect_signals("我一般晚上才有精神", um_rules)
    check("行为表达判为 inferred",
          sigs_beh and sigs_beh[0]["status"] == um_mod.STATUS_INFERRED,
          str(sigs_beh[0]["status"]) if sigs_beh else "无")
    check("无关句子不产生信号", um_mod.detect_signals("今天天气不错", um_rules) == [], "")
    print(f"  自我描述 → {sigs_self[0]['status']}；行为表达 → {sigs_beh[0]['status']}")

    print("\n[7.2] 修正信号必须优先且独占（修过的 bug）")
    sigs_corr = um_mod.detect_signals("我没说过我不喜欢甜的", um_rules)
    check("修正句只产生 correction", len(sigs_corr) == 1 and sigs_corr[0]["type"] == "correction",
          str([s["type"] for s in sigs_corr]))
    check("修正句不会反向推断出「不喜欢甜的」",
          not any(s["type"] == "behavior" for s in sigs_corr), "")
    print(f"  「我没说过我不喜欢甜的」→ {[s['type'] for s in sigs_corr]}（不再自相矛盾）")

    print("\n[7.3] 条目结构带齐 §28 要求的字段")
    claim = um_mod.build_claims(sigs_beh, session_id="sX", seq=99, turn_id="turn_x")[0]
    for field in ("belief", "confidence", "source", "last_updated", "status"):
        check(f"条目含 {field}", field in claim, "")
    check("source 记录了来源轮次", claim["source"]["seq"] == 99, str(claim["source"]["seq"]))
    print(f"  id={claim['id']}  status={claim['status']}  confidence={claim['confidence']}")
    print(f"  source={claim['source']['kind']} seq={claim['source']['seq']}")

    print("\n[7.4] 通过对话累积出「她眼中的用户」")
    items_before = um_mod.load_items()
    r = chat.send_once(session, "我是那种不太爱说话的人", echo=lambda *_: None)
    check("对话中产生用户模型变更", len(r["user_model_changes"]) >= 1,
          str(r["user_model_changes"]))
    items_after = um_mod.load_items()
    check("条目数增加", len(items_after) > len(items_before),
          f"{len(items_before)} → {len(items_after)}")
    check("用户自己的描述标为 confirmed",
          any(x["status"] == um_mod.STATUS_CONFIRMED for x in items_after),
          str([x["status"] for x in items_after]))
    um_snap = um_mod.load_latest_snapshot()
    check("用户模型快照已落盘", um_snap is not None, um_snap["version"] if um_snap else "")
    print(f"  条目 {len(items_before)} → {len(items_after)}；快照 {um_snap['version']}")

    print("\n[7.5] 用户模型注入未来对话")
    chat.send_once(session, "我一般周末都在家", echo=lambda *_: None)
    sys_texts2 = [m["content"] for m in fake.messages[-1] if m["role"] == "system"]
    check("用户模型以独立 system 段注入",
          any("你对用户的理解" in t for t in sys_texts2), f"system 段数={len(sys_texts2)}")
    # P0 更新：注入标签从 `[confirmed]` 变成 `[confirmed｜claim_kind]`
    # （同时标注证据状态**与**信息类型）。改成前缀匹配，
    # 既保留原断言意图，又容纳新增的分类标注。
    check("注入时标注了 status（区分明说/推断）",
          any("[confirmed" in t or "[inferred" in t for t in sys_texts2), "")
    # P0 新增：注入时必须同时给出信息类型，否则模型看不出「自我评价 ≠ 客观事实」
    check("注入时标注了 claim_kind（信息类型）",
          any("｜" in t for t in sys_texts2), "")
    # 本自测里**没有生成记忆版本**（Phase 5 的记忆未建），所以记忆段本就不该出现。
    # ⚠️ Phase 11 人格协同新增「表达调整」段，所以层数从 3 变 4。
    # 断言改成**按内容核对每层都在**，避免以后每加一层就改一次数字。
    check("人格 + 关系 + 用户模型三层都在",
          _has_persona(sys_texts2)
          and any("你与用户的关系" in t for t in sys_texts2)
          and any("你对用户的理解" in t for t in sys_texts2),
          f"system 段数={len(sys_texts2)}")
    check("表达调整段也在（人格协同）",
          any("表达调整" in t for t in sys_texts2), "")
    check("没有记忆版本时不凭空注入记忆段",
          not any("你记得的关于用户的事" in t for t in sys_texts2), "")
    print(f"  system 段数={len(sys_texts2)}（人格 + 表达调整 + 关系 + 用户模型；本测试无记忆版本）")

    # 补验：造一个记忆版本后，确认记忆段也会一起注入（共 4 层）
    import memory as memory_mod

    mv = memory_mod.MemoryVersion(
        version="v900", created_at="2026-10-07T00:00:00+08:00", model="fake-chat",
        items=[memory_mod.MemoryItem(id="mem_x", type="FACT",
                                    text="用户养了一盆茉莉。", source_event_ids=[])],
        previous_version=None,
    )
    saved_mv = memory_mod.save_version(mv)
    storage.record_memory_version(
        conn, version="v900", model="fake-chat", item_count=1,
        file=saved_mv["file"], file_sha256=saved_mv["sha256"],
        previous_version=None, note="自测用",
    )
    chat.send_once(session, "那盆茉莉怎么样了", echo=lambda *_: None)
    sys_texts3 = [m["content"] for m in fake.messages[-1] if m["role"] == "system"]
    check("有记忆版本时五层上下文齐备（人格+表达调整+记忆+关系+用户模型）",
          _has_persona(sys_texts3)
          and any("表达调整" in t for t in sys_texts3)
          and any("你记得的关于用户的事" in t for t in sys_texts3)
          and any("你与用户的关系" in t for t in sys_texts3)
          and any("你对用户的理解" in t for t in sys_texts3),
          f"system 段数={len(sys_texts3)}")
    print(f"  建成记忆版本后 system 段数={len(sys_texts3)}（五层齐备）")

    print("\n[7.6] 修正：错误理解必须能被推翻，且旧条目留痕")
    # 先制造一条 inferred 条目
    chat.send_once(session, "我不喜欢太吵的地方", echo=lambda *_: None)
    before_revise = um_mod.load_items()
    inferred = [x for x in before_revise if x["status"] == um_mod.STATUS_INFERRED]
    check("已有 inferred 条目可供修正", len(inferred) >= 1, f"{len(inferred)} 条")

    r_rev = chat.send_once(session, "我没说过我不喜欢太吵的地方", echo=lambda *_: None)
    kind = r_rev["user_model_changes"][0]["type"] if r_rev["user_model_changes"] else ""
    check("修正被识别为 revise 变更", kind == "revise", str([c["type"] for c in r_rev["user_model_changes"]]))
    after_revise = um_mod.load_items()
    superseded = [x for x in after_revise if x.get("superseded_by")]
    check("被推翻的旧条目**仍然保留在文件里**（不删除）",
          len(superseded) >= 1, f"{len(superseded)} 条")
    check("旧条目标记了被谁取代与时间",
          all(x["superseded_by"] and x["superseded_at"] for x in superseded), "")
    check("旧条目保留了 revision_history",
          all(x.get("revision_history") for x in superseded), "")
    check("已作废条目不再注入",
          not any(x["belief"] in " ".join(sys_texts2) for x in superseded), "")
    active = um_mod.active_items(after_revise)
    check("生效条目数少于总条目数（作废的不算）",
          len(active) < len(after_revise), f"生效 {len(active)} / 总 {len(after_revise)}")
    print(f"  总条目 {len(after_revise)}，生效 {len(active)}，已作废 {len(superseded)}")
    for x in superseded:
        print(f"    [已作废] {x['belief'][:36]}")
        print(f"             被 {x['superseded_by']} 取代，依据："
              f"{x['revision_history'][0].get('evidence','')[:36]}")

    print("\n[7.7] 用户模型快照版本化且拒绝覆盖")
    v_now = um_mod.next_snapshot_version()
    check("快照版本在递增", v_now >= 2, f"下一个版本=v{v_now:03d}")
    rep = um_mod.verify_snapshots()
    check("全部快照哈希校验通过", rep["ok"] and rep["checked"] >= 2,
          f"checked={rep['checked']}")
    print(f"  已有快照 {rep['checked']} 个，校验通过；下一个版本 v{v_now:03d}")

    print("\n[7.8] 变更事件留痕可追溯")
    um_events = storage.list_user_model_events(conn)
    check("变更事件已落盘", len(um_events) >= 3, f"{len(um_events)} 条")
    kinds = {r["kind"] for r in um_events}
    check("既有 add 也有 revise", {"add", "revise"} <= kinds, str(kinds))
    check("事件带 belief 与 seq",
          all(r["belief"] and r["seq"] is not None for r in um_events), "")
    print(f"  变更事件 {len(um_events)} 条，类型={sorted(kinds)}")

    print("\n[7.9] 修正必须打中正确的目标（真实聊天踩到的 bug）")

    def mk_claim(text, kind):
        sig = [{"type": kind, "claim": text, "matched": ["x"],
                "status": (um_mod.STATUS_CONFIRMED if kind == "self_description"
                           else um_mod.STATUS_INFERRED)}]
        return um_mod.build_claims(sig, session_id="sessX", seq=1, turn_id="turnX")[0]

    # 复刻真实场景：一条 confirmed + 一条 inferred，用户否定 confirmed 那条
    base = []
    for txt, kind in [("我是那种不太爱说话的人", "self_description"),
                      ("我一般晚上才有精神", "behavior")]:
        base, _ = um_mod.merge_claims(base, [mk_claim(txt, kind)], um_rules)
    check("底座有 2 条生效条目", len(um_mod.active_items(base)) == 2,
          f"{len(um_mod.active_items(base))} 条")

    revised, _ch = um_mod.merge_claims(
        base, [mk_claim("我没说过我不太爱说话", "correction")], um_rules)

    superseded = [x for x in revised if x.get("superseded_by")]
    check("修正打中了正确目标（「不太爱说话」那条）",
          len(superseded) == 1 and "不太爱说话" in superseded[0]["belief"],
          f"作废了：{[x['belief'] for x in superseded]}")
    check("没有误伤「晚上才有精神」那条",
          not any("晚上才有精神" in x["belief"] for x in superseded), "")
    check("confirmed 条目也能被修正（矛盾必须被发现）",
          bool(superseded) and superseded[0]["status"] == um_mod.STATUS_CONFIRMED,
          superseded[0]["status"] if superseded else "")
    check("作废保留了 revision_history",
          bool(superseded and superseded[0].get("revision_history")), "")

    active_now = um_mod.active_items(revised)
    check("修正指令本身不作为生效条目",
          not any(x.get("is_correction") for x in active_now),
          str([x["belief"] for x in active_now]))
    check("修正后生效条目只剩 1 条",
          len(active_now) == 1, f"{len(active_now)} 条：{[x['belief'] for x in active_now]}")
    print(f"  作废：{[x['belief'] for x in superseded]}")
    print(f"  生效：{[x['belief'] for x in active_now]}")

    print("\n[7.10] 找不到唯一目标时不猜（宁可不生效也不误伤）")
    ambiguous = []
    for txt in ("我一般晚上才有精神", "我通常白天精神很好"):
        ambiguous, _ = um_mod.merge_claims(ambiguous, [mk_claim(txt, "behavior")], um_rules)
    before_n = len([x for x in ambiguous if x.get("superseded_by")])
    um_mod.merge_claims(ambiguous, [mk_claim("我没说过那件事", "correction")], um_rules)
    after_n = len([x for x in ambiguous if x.get("superseded_by")])
    check("目标不明时不误伤任何一条", after_n == before_n,
          f"作废数 {before_n} → {after_n}")
    print(f"  模糊修正未作废任何条目（作废数保持 {after_n}）")

    # ========================================================================
    print("\n[8] 两层状态互不干扰")
    st = rel_mod.current_state(conn, rel_rules)
    check("关系状态与用户模型是两套独立数据",
          "stage" in st and "items" not in st, "")
    check("用户模型条目不写进关系事件表",
          all(r["type"] in storage.RELATIONSHIP_EVENT_TYPES for r in rel_rows), "")
    print(f"  关系：{st['total_events']} 个事件　用户模型：{len(after_revise)} 个条目（各自独立）")

    # ========================================================================
    # Phase 8 心跳 / 主动性
    # ========================================================================
    print("\n" + "=" * 68)
    print("Phase 8 · 心跳与主动性（总项目指令 §8 §9 §29）")
    print("=" * 68)

    import heartbeat as hb_mod

    hb_rules = settings.heartbeat

    def ctx_at(hours_idle, *, now=None, question_tail=False, pending=0, shared=1):
        """造一个确定性的心跳上下文，不依赖真实时钟也不依赖库里的数据。"""
        now = now or datetime(2026, 10, 8, 14, 0)
        return {
            "now": now.isoformat(),
            "now_dt": now,
            "last_turn_at": (now - timedelta(hours=hours_idle)).isoformat(timespec="seconds"),
            "last_user_text": "今天我们把这个项目一起弄完了",
            "last_assistant_text": ("你还想聊点别的吗？" if question_tail
                                    else "嗯，那就先这样。"),
            "idle_hours": float(hours_idle),
            "conversation_count": 10,
            "relationship": {"stage": "信任", "counts": {"shared_experience": shared}},
            "user_model_active": 2,
            "memory_versions": 1,
            "pending_count": pending,
            "pending": [],
            "last_proactive_at": None,
            "last_heartbeat_at": None,
        }

    print("\n[P8.1] 默认结论是「等待」而不是主动")
    r = hb_mod.decide(ctx_at(1.0), hb_rules)
    check("刚聊完不久 → wait", r["decision"] == "wait", r["decision"])
    print(f"  空闲 1 小时 → {r['decision']}")

    print("\n[P8.2] 免打扰时段不主动（含跨午夜区间）")
    r = hb_mod.decide(ctx_at(20, now=datetime(2026, 10, 8, 23, 30), question_tail=True), hb_rules)
    r2 = hb_mod.decide(ctx_at(20, now=datetime(2026, 10, 8, 6, 30), question_tail=True), hb_rules)
    check("23:30 免打扰 → wait", r["decision"] == "wait", r["decision"])
    check("06:30 也免打扰（跨午夜区间生效）→ wait", r2["decision"] == "wait", r2["decision"])
    check("理由说明了免打扰", "免打扰" in r["reason"], r["reason"][:34])
    print(f"  23:30 → {r['decision']}；06:30 → {r2['decision']}")

    print("\n[P8.3] 空闲够久 + 有话可说 → 主动消息")
    r = hb_mod.decide(ctx_at(20, question_tail=True), hb_rules)
    check("空闲 20h + 上次停在问句 → proactive_message",
          r["decision"] == "proactive_message", r["decision"])
    check("主动意图有具体内容", bool(r["message"].strip()), r["message"][:30])
    print(f"  空闲 20h + 停在问句 → {r['decision']}：{r['message'][:30]}")

    print("\n[P8.4] 空闲够久但没什么可说 → 不行动（**这也是 Agency**）")
    bare = ctx_at(20, question_tail=False, shared=0)
    bare["relationship"] = {"stage": "初识", "counts": {"shared_experience": 0}}
    r_bare = hb_mod.decide(bare, hb_rules)
    check("无事可说 → no_action", r_bare["decision"] == "no_action", r_bare["decision"])
    check("理由点明「选择不打扰也是主动性」",
          "主动性" in r_bare["reason"], r_bare["reason"][:46])
    print(f"  → {r_bare['decision']}（{r_bare['reason'][:40]}…）")

    print("\n[P8.5] 冷却：不变成「每隔几分钟发消息」")
    c = ctx_at(20, question_tail=True)
    c["last_proactive_at"] = datetime(2026, 10, 8, 12, 0).isoformat()
    r = hb_mod.decide(c, hb_rules)   # now=14:00，距上次主动 2h < cooldown 6h
    check("距上次主动不足冷却 → no_action", r["decision"] == "no_action", r["decision"])
    check("理由说明冷却中", "冷却" in r["reason"], r["reason"][:30])
    print(f"  2 小时前刚主动过 → {r['decision']}")

    print("\n[P8.6] 待处理项积压 → 不再新增（主动 ≠ 刷屏）")
    r = hb_mod.decide(ctx_at(20, question_tail=True, pending=5), hb_rules)
    check("积压超上限 → no_action", r["decision"] == "no_action", r["decision"])
    check("理由说明积压", "待处理" in r["reason"], r["reason"][:30])
    print(f"  已有 5 条待处理 → {r['decision']}")

    print("\n[P8.7] 决策是纯函数（可重放）")
    a = hb_mod.decide(ctx_at(20, question_tail=True), hb_rules)
    b = hb_mod.decide(ctx_at(20, question_tail=True), hb_rules)
    check("同样输入两次结果一致", a == b, a["decision"])
    print(f"  两次都是 {a['decision']}")

    print("\n[P8.8] 不行动也必须落盘留痕（§29）")
    before_hb = len(storage.list_heartbeat_runs(conn))
    hb_mod.tick(conn, settings, now=datetime(2026, 10, 8, 14, 0))
    hb_mod.tick(conn, settings, now=datetime(2026, 10, 8, 23, 30))
    rows_hb = storage.list_heartbeat_runs(conn)
    check("心跳记录增加", len(rows_hb) > before_hb, f"{before_hb} → {len(rows_hb)}")
    check("不行动/等待也被记录",
          any(r["decision"] in ("no_action", "wait") for r in rows_hb), "")
    check("每条带理由", all(r["reason"] for r in rows_hb), "")
    check("上下文也存下来（便于复盘）", all(r["context"] for r in rows_hb), "")
    print(f"  心跳 {before_hb} → {len(rows_hb)} 条，其中非主动 "
          f"{sum(1 for r in rows_hb if r['decision'] != 'proactive_message')} 条")

    print("\n[P8.9] 主动消息落进待处理项，并在下次对话兑现")
    # 注意：这里**不用 tick(now=...) 造主动消息**。
    #
    # 原因（测试设计上的坑）：tick 用模拟时间，但 collect_context 从真实数据库读
    # 「上次对话时间」（那是真实当下），两个时钟不一致 → 算出的空闲是负数 →
    # 永远触发不了主动。所以：
    #   · 主动消息的**判定**用确定性上下文测（见 P8.3）
    #   · 这里只测「有了主动意图之后」的落盘 → 注入 → 兑现这条链路
    fake_seed = FakeChat()
    seed_session = chat.new_session(persona=persona, settings=settings, client=fake_seed,
                                   peak_override=False, conn=conn)
    chat.send_once(seed_session, "先聊两句铺个底", echo=lambda *_: None)

    run_id = storage.record_heartbeat_run(
        conn, decision="proactive_message", reason="上次对话停在一个问题上，适合跟进",
        action="followup_question", message="上次你还没回我：你还想聊点别的吗？",
        context={"idle_hours": 20.0},
    )
    pending_id = storage.record_pending_action(
        conn, run_id=run_id, kind="followup_question",
        message="上次你还没回我：你还想聊点别的吗？",
        reason="上次对话停在一个问题上，适合跟进",
    )
    pending = storage.list_pending_actions(conn, status="pending")
    check("主动意图已落盘为待处理项", len(pending) == 1, f"{len(pending)} 条")
    check("待处理项带原因", bool(pending[0]["reason"]), "")
    check("待处理项初始状态是 pending", pending[0]["status"] == "pending", pending[0]["status"])

    pend_text = hb_mod.render_for_prompt(
        [{"message": p["message"], "reason": p["reason"]} for p in pending])
    check("注入文本说明是「想主动说的话」", "想主动说的话" in pend_text, "")

    fake2 = FakeChat()
    s2 = chat.new_session(persona=persona, settings=settings, client=fake2,
                         peak_override=False, conn=conn)
    rr = chat.send_once(s2, "在吗", echo=lambda *_: None)
    sys_txt = [m["content"] for m in fake2.messages[-1] if m["role"] == "system"]
    check("待处理项确实注入进上下文",
          any("想主动说的话" in t for t in sys_txt), f"system 段数={len(sys_txt)}")
    check("注入后标记为已投递", rr["pending_delivered"] == [pending_id],
          str(rr["pending_delivered"]))
    check("已投递的不再出现在待处理列表",
          len(storage.list_pending_actions(conn, status="pending")) == 0, "")
    # 已投递的仍留在表里（保留痕迹，不删除）
    all_pa = storage.list_pending_actions(conn, status=None)
    check("已投递的记录保留在表里（不删除）",
          any(p["status"] == "delivered" for p in all_pa), f"{len(all_pa)} 条")
    check("已投递记录带 turn_id", any(p["turn_id"] for p in all_pa), "")
    print("  主动意图 → 待处理项 → 下次对话注入 → 标记已投递（保留痕迹，不重复）")

    # 再确认一次：投递过的不会二次注入
    fake3 = FakeChat()
    s3 = chat.new_session(persona=persona, settings=settings, client=fake3,
                         peak_override=False, conn=conn)
    chat.send_once(s3, "再聊一句", echo=lambda *_: None)
    sys_txt3 = [m["content"] for m in fake3.messages[-1] if m["role"] == "system"]
    check("已投递的不会二次注入",
          not any("想主动说的话" in t for t in sys_txt3), "")

    print("\n[P8.10] 心跳不调用模型、不花钱")
    import pricing as pricing_mod
    ledger_before = len(pricing_mod.read_ledger())
    hb_mod.tick(conn, settings, now=datetime(2026, 10, 9, 15, 0), dry_run=True)
    hb_mod.tick(conn, settings, now=datetime(2026, 10, 9, 15, 1))
    check("心跳没有产生任何 API 记账",
          len(pricing_mod.read_ledger()) == ledger_before,
          f"{ledger_before} → {len(pricing_mod.read_ledger())}")
    print(f"  账本行数 {ledger_before} → {len(pricing_mod.read_ledger())}（未增加）")

    print("\n[P8.11] dry-run 只算不写")
    before_dry = len(storage.list_heartbeat_runs(conn))
    hb_mod.tick(conn, settings, now=datetime(2026, 10, 9, 16, 0), dry_run=True)
    check("dry-run 不写心跳记录",
          len(storage.list_heartbeat_runs(conn)) == before_dry,
          f"{before_dry} → {len(storage.list_heartbeat_runs(conn))}")
    print(f"  dry-run 后心跳记录数不变（{before_dry}）")

    # --- ★ 2026-10-26 新增：maybe_tick 也必须支持 dry_run（污染修复的回归测试）---
    #
    # ## 这是一次真实事故的回归测试
    #
    # `selftest.py` 的端到端自测会调 `chat.send_once()`，而 `send_once` 会调
    # 本路径（`chat.maybe_heartbeat_tick` → `heartbeat.maybe_tick`）。
    # 修复前 `maybe_tick` **没有透传 `dry_run`**，于是：
    #
    #   `status_snapshot.py` 每跑一次 → 往真实库写入 1 条 heartbeat_runs
    #   → 且当判定为 proactive 时，还写入 1 条 pending_actions（status=delivered）
    #
    # 后果：`pending_actions` 曾显示 **2** 条「已投递主动消息」，
    # 而**真实用户可见的只有 1 条** —— 被污染的那条从未送达任何人。
    # 这两张表没有 `session_id`，既无法归属、也无法被 `_clean` 清掉。
    #
    # 修法：让 `maybe_tick` 透传 `dry_run`（判定照走、不落盘），
    # 而不是「生成后再删记录」——后者会误删且依赖「能识别出测试行」。
    print("\n[P8.11b] maybe_tick 的 dry_run 不写心跳、也不写待处理项")
    before_mt = len(storage.list_heartbeat_runs(conn))
    before_pa = len(storage.list_pending_actions(conn, status=None))

    # 先把节流清掉：maybe_tick 会因 MIN_GAP_MINUTES 直接返回 throttled，
    # 那样就测不到「真的执行了但没写」这条关键路径。
    # 传**朴素** datetime 即可 —— maybe_tick 内部会补上 tzinfo。
    far_future = datetime.now() + timedelta(hours=48)
    r_mt = hb_mod.maybe_tick(session, now=far_future, dry_run=True)

    after_mt = len(storage.list_heartbeat_runs(conn))
    after_pa = len(storage.list_pending_actions(conn, status=None))
    check("maybe_tick(dry_run) 不新增心跳记录", after_mt == before_mt,
          f"{before_mt} → {after_mt}")
    check("maybe_tick(dry_run) 不新增待处理项", after_pa == before_pa,
          f"{before_pa} → {after_pa}")
    check("maybe_tick(dry_run) 仍真的跑到了执行路径（不是被节流掉）",
          r_mt.get("status") == hb_mod.TICK_EXECUTED
          and r_mt.get("dry_run") is True,
          f"status={r_mt.get('status')} dry_run={r_mt.get('dry_run')}")
    check("dry_run 的结果里不含 result（没有落盘记录）",
          r_mt.get("result") is None or r_mt.get("dry_run") is True, "")
    print(f"  status={r_mt.get('status')} · 心跳 {before_mt}→{after_mt} · "
          f"待处理 {before_pa}→{after_pa}")

    # --- ★ 2026-11-07 新增：pending 不得短路整次心跳（HB-0 修复回归）---
    #
    # ## 这是一次真实缺陷的回归测试
    #
    # 2026-11-04 的 maybe_tick 把「已有 pending」短路放在节流**之前**：
    # 只要存在 1 条未兑现 pending，就直接 return、**一条 heartbeat_runs 都不写**。
    # 后果（dsh 复核 A1 时发现、ZCode 2026-11-06 确认）：
    #   · pending 未兑现期间「上次检查时间」冻结 → 节流失真、监控全盲；
    #   · schedule 触发一旦启用（TRIGGER_SCHEDULE 预留位），会被一条卡住的
    #     pending **永久挡死**——主动心跳彻底停摆。
    # 修法：节流先行；pending 存在时照常 tick 并留痕，但压制新的主动意图
    #   （suppress_new_intent → 决策降级为 wait，action=pending_deferred）。
    print("\n[P8.11c] pending 存在时心跳照常检查留痕，且不重复生成主动意图")
    _seed_run = storage.record_heartbeat_run(
        conn, decision="no_action", reason="P8.11c 种子 run：给 pending 一个合法 run_id")
    _pa_id = storage.record_pending_action(
        conn, run_id=_seed_run, kind="followup_question",
        message="P8.11c 测试用未兑现主动意图", reason="回归测试")
    before_runs_c = len(storage.list_heartbeat_runs(conn))
    before_pend_c = len(storage.list_pending_actions(conn, status="pending"))

    # 场景1：pending 存在 + 距上次检查 ≥ MIN_GAP（now 抬到 48h 后，必过节流）
    #   → 期望：照常执行（executed=True）、run +1、pending 不新增、决策非 proactive
    r_c1 = hb_mod.maybe_tick(session, now=datetime.now() + timedelta(hours=48),
                             trigger=hb_mod.TRIGGER_SCHEDULE)
    after_runs_c = len(storage.list_heartbeat_runs(conn))
    after_pend_c = len(storage.list_pending_actions(conn, status="pending"))
    check("pending 存在时心跳照常执行（不再短路装死）",
          r_c1["status"] == hb_mod.TICK_PENDING_ACTION and r_c1["executed"] is True,
          f"status={r_c1['status']} executed={r_c1.get('executed')}")
    check("heartbeat_runs 照常 +1（留痕恢复，不再停更）",
          after_runs_c == before_runs_c + 1, f"{before_runs_c} → {after_runs_c}")
    check("不重复生成：pending 数不变", after_pend_c == before_pend_c,
          f"{before_pend_c} → {after_pend_c}")
    check("压制的决策不是 proactive_message",
          r_c1["result"]["decision"] != "proactive_message",
          r_c1["result"]["decision"])
    if r_c1["result"].get("action") == "pending_deferred":
        check("降级理由指向已有的 pending",
              "不重复生成" in r_c1["result"]["reason"],
              r_c1["result"]["reason"][:46])

    # 场景2：刚检查过（30 秒前，run 落库打的是真实时间）再触发 → 节流胜出，
    #   pending 也不能让它提前（节流先行的回归）。
    #   ⚠️ 这里必须传**带时区**的 UTC 时间：heartbeat_runs 落库的是真实 UTC，
    #   而朴素 datetime.now()（本地时间）会被 maybe_tick 补成 UTC —— 本机
    #   UTC+8，朴素传入等于把 now 拔快 8 小时，30 秒的间隔瞬间变 8 小时，
    #   节流就永远测不到（本测试第一版就栽在这里）。
    r_c2 = hb_mod.maybe_tick(
        session, now=datetime.now(timezone.utc) + timedelta(seconds=30),
        trigger=hb_mod.TRIGGER_SCHEDULE)
    check("节流先行：间隔内再触发 → throttled（不因 pending 提前）",
          r_c2["status"] == hb_mod.TICK_THROTTLED, f"status={r_c2['status']}")

    # 清场：把测试 pending 划为 delivered，不影响后续用例
    storage.mark_pending_delivered(conn, _pa_id, "P8.11c")
    print(f"  r1.status={r_c1['status']} 决策={r_c1['result']['decision']} · "
          f"心跳 {before_runs_c}→{after_runs_c} · pending {before_pend_c}→{after_pend_c}（已清场）")

    # --- ★ 2026-11-07（外部调研 #6/#7）：digest 合并 + 定时承诺兑现 ---
    #
    # #6：多条没说出口的意图 → 合并成一次推送（限流+防打扰），全部划掉
    # #7：kind=scheduled_reminder 且 deliver_at 到点 → 不经 decide() 直接兑现
    #     （提醒是用户自己约的，免打扰时段也照送）；未到点的不动
    # 顺带验证 schedule 触发路径（TRIGGER_SCHEDULE 第一次有生产者）
    print("\n[P8.12] 通知 digest 合并 + 定时承诺兑现 + schedule 触发")
    import os as _os
    from datetime import timezone as _tz2

    _os.environ["NTFY_TOPIC"] = "selftest-topic"
    captured: list[tuple[str, str]] = []
    _real_deliver = hb_mod.deliver_to_ntfy
    hb_mod.deliver_to_ntfy = lambda topic, message: (captured.append((topic, message)) or True)
    _quiet_backup = hb_mod._in_quiet_hours
    _pf_backup = hb_mod.pick_followup
    hb_mod._in_quiet_hours = lambda *a, **k: False
    hb_mod.pick_followup = lambda context, rules: {
        "reason": "P8.12 测试：有值得跟进的话题", "kind": "followup_question",
        "message": "这轮的新想法"}
    try:
        _seed_run12 = storage.record_heartbeat_run(
            conn, decision="no_action", reason="P8.12 种子 run")
        # 场景1 digest：先塞 1 条旧 pending，新一轮 proactive 推送时应合并全部
        storage.record_pending_action(conn, run_id=_seed_run12, kind="followup_question",
                                      message="上次你说的那本书", reason="P8.12 旧意图")
        r_d1 = hb_mod.tick(conn, settings,
                           now=datetime.now(_tz2.utc) + timedelta(hours=48),
                           trigger=hb_mod.TRIGGER_SCHEDULE)
        check("digest：多条 pending 合并为一次推送",
              any("上次你说的那本书" in m and "这轮的新想法" in m for _, m in captured),
              str(captured)[:120])
        left = storage.list_pending_actions(conn, status="pending")
        check("digest：相关 pending 全部划掉（pushed）",
              all(r["status"] == "pushed" for r in left
                  if r["kind"] == "followup_question"), str(len(left)))
        check("schedule 触发留痕：run 的 context 带 trigger=schedule",
              any("schedule" in (r["context"] or "")
                  for r in storage.list_heartbeat_runs(conn, limit=3)),
              str([r["decision"] for r in storage.list_heartbeat_runs(conn, limit=3)]))

        # 场景2 定时承诺：到点的提醒 → 不经 decide() 直接兑现；未到点的不动
        captured.clear()
        due_iso = (datetime.now(_tz2.utc) - timedelta(minutes=5)).isoformat()
        fut_iso = (datetime.now(_tz2.utc) + timedelta(hours=6)).isoformat()
        _pa_due = storage.record_pending_action(
            conn, run_id=_seed_run12, kind="scheduled_reminder",
            message="明早叫我开会", reason="定时承诺", deliver_at=due_iso)
        _pa_fut = storage.record_pending_action(
            conn, run_id=_seed_run12, kind="scheduled_reminder",
            message="三天后提醒我复查", reason="定时承诺", deliver_at=fut_iso)
        r_d2 = hb_mod.tick(conn, settings, now=datetime.now(_tz2.utc),
                           trigger=hb_mod.TRIGGER_SCHEDULE)
        check("到点提醒不经 decide() 直接兑现",
              any("明早叫我开会" in m for _, m in captured), str(captured))
        st_due = [r["status"] for r in storage.list_pending_actions(conn, status=None)
                  if r["pending_id"] == _pa_due][0]
        check("到点提醒标记 pushed", st_due == "pushed", st_due)
        st_fut = [r["status"] for r in storage.list_pending_actions(conn, status=None)
                  if r["pending_id"] == _pa_fut][0]
        check("未到点提醒保持 pending", st_fut == "pending", st_fut)

        # 场景3 提醒缓存同步（工单二：断机期兜底的本地半边）
        # 每次 tick 把未兑现的定时承诺写成根目录 reminders.json，云端 cron 读它兜底
        _rem_file = Path(paths.ROOT) / "reminders.json"
        check("reminders.json 已写（断机期兜底通道）", _rem_file.exists(),
              str(_rem_file))
        _data = json.loads(_rem_file.read_text(encoding="utf-8"))
        _ids = [r["pending_id"] for r in _data["reminders"]]
        check("未来提醒在清单里", _pa_fut in _ids, str(_ids))
        check("已兑现（pushed）的不在清单里", _pa_due not in _ids, str(_ids))
        _entry = [r for r in _data["reminders"] if r["pending_id"] == _pa_fut][0]
        check("条目带 deliver_at/message/kind",
              bool(_entry["deliver_at"]) and bool(_entry["message"])
              and _entry["kind"] == "scheduled_reminder",
              str(_entry))
    finally:
        hb_mod.deliver_to_ntfy = _real_deliver
        hb_mod._in_quiet_hours = _quiet_backup
        hb_mod.pick_followup = _pf_backup
        if _os.environ.get("NTFY_TOPIC") == "selftest-topic":
            del _os.environ["NTFY_TOPIC"]
    print(f"  测试期推送 {len(captured)} 条 · monkeypatch 已还原")

    # --- ★ 2026-11-07（雷2/3 回归）：未到点提醒不进 chat 清单、不进 digest ---
    print("\n[P8.14] 雷2/3 回归：双路径过滤")
    _pa_due14 = storage.record_pending_action(
        conn, run_id=_seed_run12, kind="scheduled_reminder",
        message="已到点的提醒", reason="P8.14", deliver_at=(
            datetime.now(_tz2.utc) - timedelta(minutes=1)).isoformat())
    _pa_fut14 = storage.record_pending_action(
        conn, run_id=_seed_run12, kind="scheduled_reminder",
        message="三天后的提醒", reason="P8.14", deliver_at=(
            datetime.now(_tz2.utc) + timedelta(days=3)).isoformat())
    _ids14 = [r["pending_id"] for r in storage.list_pending_for_chat(conn)]
    check("chat 清单：未到点提醒不在（不被提前消费）", _pa_fut14 not in _ids14, str(_ids14))
    check("chat 清单：到点提醒在（到点就该说）", _pa_due14 in _ids14, str(_ids14))

    _os.environ["NTFY_TOPIC"] = "selftest-topic"
    captured.clear()
    # 2026-11-07 修复：P8.13 的 finally 已把 deliver_to_ntfy 还原成真函数，
    # 这里必须重新打桩，否则 tick 走真实 HTTP 推送（沙箱里失败 → captured=0）。
    hb_mod.deliver_to_ntfy = lambda topic, message: (captured.append((topic, message)) or True)
    hb_mod._in_quiet_hours = lambda *a, **k: False
    hb_mod.pick_followup = lambda context, rules: {
        "reason": "P8.14", "kind": "followup_question", "message": "P8.14 新意图"}
    try:
        # 清场：前面场景遗留的 pending 会让 decide 的防刷屏规则（≥3 条）拦住
        # proactive——测试要的是受控环境，先把账清干净再种测试数据。
        for r in storage.list_pending_actions(conn, status="pending"):
            storage.mark_pending_delivered(conn, r["pending_id"], "P8.14-cleanup")
        r14 = hb_mod.tick(conn, settings, now=datetime.now(_tz2.utc) + timedelta(hours=48),
                    trigger=hb_mod.TRIGGER_SCHEDULE)
        merged_all = " ｜ ".join(m for _, m in captured)
        check("digest：未到点提醒不被合并推送", "三天后的提醒" not in merged_all,
              merged_all[:100])
        check("digest：proactive 意图正常合并", "P8.14 新意图" in merged_all,
              merged_all[:80])
    finally:
        hb_mod.deliver_to_ntfy = _real_deliver
        hb_mod._in_quiet_hours = _quiet_backup
        hb_mod.pick_followup = _pf_backup
        if _os.environ.get("NTFY_TOPIC") == "selftest-topic":
            del _os.environ["NTFY_TOPIC"]
    storage.mark_pending_delivered(conn, _pa_due14, "P8.14")
    storage.mark_pending_delivered(conn, _pa_fut14, "P8.14")
    print("  双路径过滤验证完成 · monkeypatch 已还原")

    # --- P8.15（P1 批回归，2026-11-08）：ntfy 4KB 护栏 + deliver_at 解析后比较 ---
    print("\n[P8.15] P1 回归：ntfy 4KB 护栏 + deliver_at 解析后比较")
    import urllib.request as _ur15
    from datetime import timezone as _tz15
    _real_urlopen15 = _ur15.urlopen
    _sent15: dict = {}

    class _FakeResp15:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def _fake_urlopen15(req, timeout=None):
        _sent15["data"] = req.data.decode("utf-8")
        return _FakeResp15()

    _ur15.urlopen = _fake_urlopen15
    try:
        _big15 = "很长的主动消息。" * 800   # 5600 字 > ntfy 4096 上限
        _ok15 = hb_mod.deliver_to_ntfy("selftest-topic", _big15)
        check("超长消息仍推送成功（截断护栏，不再永久卡 pending）",
              _ok15 is True
              and len(_sent15.get("data", "")) <= hb_mod.NTFY_MAX_MESSAGE_CHARS + 60,
              f"len={len(_sent15.get('data', ''))}")
        check("截断带说明尾注（全文在本地账可查）",
              "超长已截断" in _sent15.get("data", ""), "")
        _ok15b = hb_mod.deliver_to_ntfy("selftest-topic", "正常长度")
        check("正常消息原样不截断",
              _ok15b is True and _sent15.get("data") == "正常长度",
              str(_sent15.get("data", ""))[:40])
    finally:
        _ur15.urlopen = _real_urlopen15

    # deliver_at：P1 前 SQL 字符串比较，"+08:00" 格式永远判未到点（迟送 6.5h+）
    _pa_0815 = storage.record_pending_action(
        conn, run_id=_seed_run12, kind="scheduled_reminder",
        message="北京时区到点提醒", reason="P8.15",
        deliver_at="2026-11-09T12:00:00+08:00")   # = 04:00 UTC
    _due15 = storage.list_due_reminders(conn, "2026-11-09T05:00:00+00:00")
    check("+08:00 格式被判到点（解析后比较，不再字典序误判）",
          _pa_0815 in [r["pending_id"] for r in _due15],
          str([r["pending_id"] for r in _due15]))
    _notdue15 = storage.list_due_reminders(conn, "2026-11-09T03:00:00+00:00")
    check("未到点不误判（提前 1 小时不算到）",
          _pa_0815 not in [r["pending_id"] for r in _notdue15], "")
    _row15 = [r for r in storage.list_pending_actions(conn, status=None)
              if r["pending_id"] == _pa_0815][0]
    check("入库归一成 UTC ISO（写侧格式不再影响读侧）",
          str(_row15["deliver_at"]).endswith("+00:00"), str(_row15["deliver_at"]))

    # ========================================================================
    # 身份信息捕获（真实事故的回归测试）
    # ========================================================================
    print("\n" + "=" * 68)
    print("身份信息捕获（真实事故：用户说「我叫（网名）」，系统始终不记得）")
    print("=" * 68)
    print("""
事故经过：用户 seq 10 说「好的了解我叫（网名）」，seq 69 又说「我叫（网名）」，
系统在 seq 88/89 连续答「你还没跟我说过名字」「这是你第一次说」。
原因：①「我叫」不在触发词里 → 从未进入 Raw Event 层；
      ② v001 里那条记忆在 v002 整理时丢了。
用户对这次事故的定性（比原始结论更准确）：
  **原实验只验证了「基准事实进入系统后的存活率」，没有覆盖
    「自然对话中的记忆捕获完整性」。**
""")
    import events as events_mod

    ident_rules = settings.triggers

    print("\n[ID.1] 身份句式能被触发词捕获")
    for t in ("我叫（网名）", "好的了解我叫（网名）", "我的名字是（网名）", "叫我（网名）就行", "我姓宋"):
        d = events_mod.detect(t, ident_rules)
        check(f"「{t}」命中", d["hit"], str(d["matched"]))
    print("  5 句身份表达全部命中")

    print("\n[ID.2] 不能误伤日常话（这是身份词最容易出问题的地方）")
    for t in ("我是说这个", "我是这么想的", "我今天有点累", "你叫什么名字",
              "我叫不出来", "我叫什么"):
        d = events_mod.detect(t, ident_rules)
        check(f"「{t}」不命中", not d["hit"],
              f"误命中 {d['matched']}" if d["hit"] else "")
    print("  6 句日常/疑问表达全部不命中")

    print("\n[ID.3] 端到端：说名字会真的产生原始事件")
    fake_id = FakeChat()
    sid_session = chat.new_session(persona=persona, settings=settings, client=fake_id,
                                  peak_override=False, conn=conn)
    before_ev = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    rr = chat.send_once(sid_session, "我叫（网名）", echo=lambda *_: None)
    after_ev = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    check("说「我叫（网名）」产生了原始事件", after_ev > before_ev,
          f"{before_ev} → {after_ev}")
    check("事件内容保留了原话",
          rr["event"] is not None and "（网名）" in (rr["event"] or {}).get("user_text", ""),
          (rr["event"] or {}).get("user_text", ""))
    print(f"  原始事件 {before_ev} → {after_ev}；原文："
          f"{(rr['event'] or {}).get('user_text', '')}")

    print("\n[ID.4] 身份事件能被记忆整理引用（可追溯）")
    if rr["event"]:
        eid = rr["event"]["event_id"]
        check("事件文件已落盘", Path(paths.RAW_EVENTS_DIR, f"{eid}.json").exists(), eid)
        import consolidate as consolidate_mod
        evs = consolidate_mod.collect_events(conn)
        check("身份事件进入了整理输入", any(e["event_id"] == eid for e in evs),
              f"整理输入共 {len(evs)} 条事件")
        print(f"  {eid} 已进入整理输入（共 {len(evs)} 条）")

    # ========================================================================
    # 工具循环的失败路径（真实事故的回归测试）
    # ========================================================================
    print("\n" + "=" * 68)
    print("工具循环失败路径（真实事故：模型原地重试空参数、并虚报次数）")
    print("=" * 68)
    print("""
事故经过：用户说「帮我记一条笔记：<完整文本>」，模型发出**空参数**
`{"name":"take_note","arguments":{}}`；被拒后**原样重发同一个空参数**；
第二次又被拒就放弃，最终回答里还写「试了三次」—— 而账本只有 2 次。
""")

    import tools as tools_layer

    reg_tools = tools_layer.build_registry(settings, root=TEMP_ROOT)

    class RetryClient:
        """复刻事故：发空参数 → 被拒 → 原样重发 → 放弃。"""

        model = "fake-chat"

        def __init__(self):
            self.n = 0
            self.observations = []

        def send(self, messages, model=None):
            self.n += 1
            if self.n > 1:
                self.observations.append(
                    [m["content"] for m in messages if m["role"] == "user"][-1])
            if self.n <= 2:
                return Reply(
                    text='我记一下。\n```tool\n{"name":"take_note","arguments":{}}\n```',
                    model=self.model,
                    usage=Usage(prompt_tokens=100, completion_tokens=10, cache_hit_tokens=0),
                    latency_ms=1)
            return Reply(text="试了三次都是同一个报错，我不再重试了。", model=self.model,
                         usage=Usage(prompt_tokens=100, completion_tokens=10, cache_hit_tokens=0),
                         latency_ms=1)

    fake_rt = RetryClient()
    s_rt = chat.new_session(persona=persona, settings=settings, client=fake_rt,
                            peak_override=False, conn=conn, tools=reg_tools)
    r_rt = chat.send_once(s_rt, "帮我记一条笔记：今天完成了 Phase 9 验证。", echo=lambda *_: None)

    repeats = [e for e in r_rt["tool_calls"] if e.get("repeat")]
    check("完全相同的重复调用被拦截", len(repeats) == 1, f"{r_rt['tool_calls']}")
    check("被拦截的调用也落账（可追踪）",
          any("完全相同" in (row["error"] or "")
              for row in storage.list_tool_calls(conn)), "")
    check("回灌文本明确告知「重试不会有不同结果」",
          bool(fake_rt.observations)
          and "重试不会有不同结果" in fake_rt.observations[-1], "")
    check("回灌文本给出下一步指引",
          bool(fake_rt.observations)
          and ("改参数后重发" in fake_rt.observations[-1]
               or "如实告诉用户" in fake_rt.observations[-1]), "")
    check("迭代次数不超过上限", r_rt["tool_iterations"] <= 5, str(r_rt["tool_iterations"]))
    check("工具全失败时回答末尾有代码给的事实说明",
          "工具没成功" in r_rt["turn"]["assistant_text"],
          r_rt["turn"]["assistant_text"][-40:])
    print(f"  迭代 {r_rt['tool_iterations']} 次；拦截 {len(repeats)} 次重复调用")
    print("  模型仍自称「三次」→ 属模型措辞，代码只能保证账本数字真实")

    conn.close()

    # --- 汇总 ---
    print("\n" + "=" * 68)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 68)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本次自测使用假客户端，未产生任何真实 API 费用。")

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    print(f"已清理临时目录：{TEMP_ROOT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
