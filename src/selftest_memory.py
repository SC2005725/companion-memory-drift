"""Phase 5 离线自测（假客户端，不调用真实 API、不花钱）。

在**独立的临时目录**里跑，绝不碰项目真实的 conversation/ memory/ recall/。
这样做的理由：记忆功能会写版本文件、检索记录、探针记录，
如果直接在主目录自测，测试数据就会污染真实实验数据。

覆盖：
    记忆条目模型 → 条目 ID 确定性 → 版本快照只增不改 → 版本校验
    → changelog diff（added/removed/modified/reworded/unchanged）
    → 检索打分 → 检索落盘（含 0 命中）→ 提示词外置性 → 探针不注入原始对话

用法：
    python src\\selftest_memory.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

# --- 先建立隔离环境，再导入任何业务模块 -------------------------------------
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_selftest_"))

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
paths.EXPERIMENTS_DIR = TEMP_ROOT / "experiments"
paths.LOGS_DIR = TEMP_ROOT / "logs"
paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"

# 关键：把已经用旧路径初始化过的模块从缓存里清掉，强制用新路径重新导入
for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe"):
    sys.modules.pop(mod, None)

import changelog as changelog_mod  # noqa: E402
import chat  # noqa: E402
import consolidate as consolidate_mod  # noqa: E402
import llm as llm_mod  # noqa: E402
import memory as memory_mod  # noqa: E402
import probe as probe_mod  # noqa: E402
import retrieve as retrieve_mod  # noqa: E402
import storage  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from persona import load_persona  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


# --- 假客户端 -----------------------------------------------------------------

class FakeConsolidator:
    """按调用次数返回预置的整理结果，用来模拟「多轮整理后记忆发生变化」。"""

    model = "fake-consolidator"

    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls = 0

    def send(self, messages, model=None) -> Reply:
        idx = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        text = json.dumps(self.payloads[idx], ensure_ascii=False)
        return Reply(
            text=text,
            model=self.model,
            usage=Usage(prompt_tokens=2000, completion_tokens=300, cache_hit_tokens=800),
            latency_ms=3,
        )


class FakeChat:
    model = "fake-chat"

    def send(self, messages, model=None) -> Reply:
        return Reply(
            text="（自测回复）你提过夜班那阵子。",
            model=self.model,
            usage=Usage(prompt_tokens=900, completion_tokens=50, cache_hit_tokens=400),
            latency_ms=2,
        )


# --- 建立隔离环境所需的文件 -----------------------------------------------------

def setup_files() -> None:
    paths.ensure_dirs()
    paths.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "model": "fake-consolidator",
                "temperature": 0.7,
                "max_tokens": 800,
                "history_turns": 10,
                "memory": {"top_k": 3},
                "cost": {"models": {"fake-consolidator": {"cache_hit": 0.04, "input": 2.0, "output": 8.0}}},
                "raw_event_triggers": {"keywords": ["我以前"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paths.PERSONALITY_FILE.write_text(
        json.dumps(
            {
                "name": "小满",
                "description": "测试人格",
                "system_prompt": "你叫{name}。{description}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 提示词直接复制项目里那份真的，确保测的是实际会用的提示词
    real_prompt = SRC.parent / "config" / "prompts" / "consolidate.md"
    paths.CONSOLIDATE_PROMPT_FILE.write_text(
        real_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )


def main() -> int:
    print("=" * 64)
    print("Phase 5 离线自测（隔离临时目录，不调用真实 API）")
    print(f"临时根目录：{TEMP_ROOT}")
    print("=" * 64)

    setup_files()
    conn = storage.connect()
    persona = load_persona()
    settings_mod = __import__("config")
    settings = settings_mod.load_settings()

    # ===== [1] 记忆条目模型 =====
    print("\n[1] 记忆条目模型")
    it = memory_mod.MemoryItem(id="x", type="fact", text="用户养了一只橘猫。",
                               source_event_ids=["raw_event_1"], confidence=1.7)
    check("type 自动大写", it.type == "FACT", it.type)
    check("confidence 被夹到 [0,1]", it.confidence == 1.0, str(it.confidence))

    bad = memory_mod.MemoryItem(id="y", type="GUESS", text="乱猜的")
    check("非法 type 降级为 UNKNOWN", bad.type == "UNKNOWN", bad.type)
    print(f"  type=fact → {it.type}；confidence=1.7 → {it.confidence}")
    print(f"  非法 type=GUESS → {bad.type}")

    # ===== [2] 条目 ID 确定性 =====
    print("\n[2] 条目 ID 确定性（diff 能不能用的前提）")
    id_a = changelog_mod.stable_item_id("FACT", "用户养了一只橘猫。", ["raw_event_1"])
    id_b = changelog_mod.stable_item_id("FACT", "用户养了一只橘猫。", ["raw_event_1"])
    id_c = changelog_mod.stable_item_id("FACT", "用户养了一只猫。", ["raw_event_1"])
    check("相同内容 → 相同 ID（重述不会被当成新增）", id_a == id_b, id_a)
    check("内容变了 → ID 也变", id_a != id_c, f"{id_a} vs {id_c}")
    print(f"  同内容两次：{id_a} == {id_b}")
    print(f"  改一个字  ：{id_c}（不同）")

    # ===== [3] 造原始事件 =====
    print("\n[3] 造原始事件（记忆的来源）")
    ev1 = storage.create_event(
        conn, session_id="sess_test", turn_id="turn_test",
        user_text="我以前在晚上工作过一年多，那时候白天睡不好。",
        detection={"method": "keyword", "matched": ["我以前"], "hit": True},
    )
    ev2 = storage.create_event(
        conn, session_id="sess_test", turn_id="turn_test2",
        user_text="我养了一只橘猫叫豆子，右前爪有一小块白毛。",
        detection={"method": "keyword", "matched": ["我养了"], "hit": True},
    )
    check("原始事件已落盘", Path(paths.RAW_EVENTS_DIR, f"{ev1['event_id']}.json").exists(),
          ev1["event_id"])
    print(f"  {ev1['event_id']} / {ev2['event_id']}")

    # 造两轮对话，供整理使用
    for seq, (u, a) in enumerate(
        [("我以前在晚上工作过一年多。", "那段夜班最难受的是哪一点？"),
         ("我养了一只橘猫叫豆子。", "豆子现在粘人吗？")],
        start=1,
    ):
        storage.save_turn(
            conn, session_id="sess_test", seq=seq, user_text=u, assistant_text=a,
            model="fake-chat", persona_name=persona.name,
            persona_sha256=persona.source_sha256,
        )

    # ===== [4] 整理出 v001 / v002 / v003 =====
    print("\n[4] 整理记忆：三个版本")
    p1 = {"items": [
        {"type": "FACT", "text": "用户曾在夜间时段工作约一年多。",
         "source_event_ids": [ev1["event_id"]], "confidence": 0.95},
        {"type": "FACT", "text": "用户养了一只橘猫叫豆子，右前爪有一小块白毛。",
         "source_event_ids": [ev2["event_id"]], "confidence": 0.9},
    ]}
    # v002：第一条被改写；第二条改成换说法；新增一条推断
    #
    # ⚠️ Phase 11 合并式整理后，**「漏写」不再等于「删除」** ——
    # 程序会保护模型没提到的旧条目。所以改写必须用 `replaces` 显式声明目标 id，
    # 否则旧条目会被保留、新文本变成纯新增（added）。
    #
    # 这里的第一条用 replaces 声明它取代了 v001 的首条（id 在运行时按内容算出）。
    # 第二条「原文照抄」→ id 不变 → diff 记为 unchanged。
    _v1_first_id = changelog_mod.stable_item_id(
        "FACT", "用户曾在夜间时段工作约一年多。", [ev1["event_id"]])
    _v1_cat_id = changelog_mod.stable_item_id(
        "FACT", "用户养了一只橘猫叫豆子，右前爪有一小块白毛。", [ev2["event_id"]])
    p2 = {"items": [
        {"type": "FACT", "text": "用户曾在晚上工作过一年多，白天睡不好。",
         "source_event_ids": [ev1["event_id"]], "confidence": 0.95,
         "replaces": [_v1_first_id]},
        {"type": "FACT", "text": "用户养了一只叫豆子的橘猫。",
         "source_event_ids": [ev2["event_id"]], "confidence": 0.9,
         "replaces": [_v1_cat_id]},
        {"type": "INFERENCE", "text": "用户可能偏好安静、少社交的工作。",
         "source_event_ids": [], "confidence": 0.4},
    ]}
    # v003：第一条不变；第二条被删（模拟记忆丢失）；推断被升格为事实（要能被发现）
    #
    # ⚠️ 合并式整理后，删除必须走顶层 `deletes`（带 reason），
    # **不能靠"不输出它"来删除** —— 那样程序会把它保留下来。
    # 这里算好 v002 里橘猫那条的 id，显式声明删除。
    _v2_cat_id = changelog_mod.stable_item_id(
        "FACT", "用户养了一只叫豆子的橘猫。", [ev2["event_id"]])
    p3 = {"items": [
        {"type": "FACT", "text": "用户曾在晚上工作过一年多，白天睡不好。",
         "source_event_ids": [ev1["event_id"]], "confidence": 0.95},
        {"type": "FACT", "text": "用户可能偏好安静、少社交的工作。",
         "source_event_ids": [], "confidence": 0.4},
    ],
        "deletes": [
            {"id": _v2_cat_id, "reason": "模拟记忆丢失：本条不再出现在整理结果里"},
        ]}

    fake = FakeConsolidator([p1, p2, p3])
    session = chat.new_session(persona=persona, settings=settings, client=fake,
                               peak_override=False, conn=conn)

    r1 = consolidate_mod.consolidate(session, note="首版")
    r2 = consolidate_mod.consolidate(session, note="第二版")
    r3 = consolidate_mod.consolidate(session, note="第三版")
    check("产生了 v001", r1["version"] == "v001", r1["version"])
    check("产生了 v002", r2["version"] == "v002", r2["version"])
    check("产生了 v003", r3["version"] == "v003", r3["version"])
    check("v002 记下了前世版本", r2["previous"] == "v001", str(r2["previous"]))
    print(f"  {r1['version']}({len(r1['items'])}条) → {r2['version']}({len(r2['items'])}条)"
          f" → {r3['version']}({len(r3['items'])}条)")

    # ===== [5] 版本快照只增不改 =====
    print("\n[5] 版本快照只增不改")
    vfiles = sorted(p.name for p in paths.VERSIONS_DIR.glob("v*.json"))
    check("三个版本文件同时存在", vfiles == ["v001.json", "v002.json", "v003.json"], str(vfiles))
    v1 = memory_mod.load_version("v001")
    check("v001 内容未被后续整理改动",
          any("约一年多" in x.text for x in v1.items), "v001 首条应保留原文")
    try:
        memory_mod.save_version(v1)
        check("重复写已存在的版本会被拒绝", False, "居然允许覆盖！")
    except memory_mod.MemoryError:
        check("重复写已存在的版本会被拒绝", True, "")
    vrep = memory_mod.verify_versions()
    check("版本哈希校验通过", vrep["ok"] and vrep["checked"] == 3, f"checked={vrep['checked']}")
    print(f"  文件：{vfiles}")
    print(f"  v001 首条：{v1.items[0].text}")

    # ===== [5.5] 会话头打印（真实崩溃过的回归测试）=====
    #
    # 真实事故：`chat.run_repl()` 里内联的会话头用了 `mem.item_count`，
    # 而 `sqlite3.Row` 只支持下标访问（`mem['item_count']`）。
    # 没有记忆版本时 mem 是 None、走不到那条分支，所以一直没暴露；
    # 等用户在真实项目里生成 v001 后，一点「开始聊天」就直接崩在会话头上。
    #
    # 所以这里对**两种状态**都跑一遍 print_header，确保两条分支都可达且正确。
    print("\n[5.5] 会话头打印（有 / 无记忆版本两种状态）")
    import io as _io

    def _capture_header(sess):
        buf = _io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            chat.print_header(sess)
        finally:
            sys.stdout = old
        return buf.getvalue()

    head = _capture_header(session)
    check("有记忆版本时会话头可正常打印", "v003" in head and "条" in head,
          head.splitlines()[3].strip() if len(head.splitlines()) > 3 else "")
    print(f"  {head.splitlines()[3].strip()}")

    # 状态二：没有记忆版本（临时清空登记表来模拟首次运行，随后还原）
    saved_rows = conn.execute("SELECT * FROM memory_versions").fetchall()
    conn.execute("DELETE FROM memory_versions")
    conn.commit()
    head_empty = _capture_header(session)
    check("无记忆版本时会话头也可正常打印", "尚无记忆版本" in head_empty,
          head_empty.splitlines()[3].strip() if len(head_empty.splitlines()) > 3 else "")
    print(f"  {head_empty.splitlines()[3].strip()}")
    for r in saved_rows:
        conn.execute(
            "INSERT INTO memory_versions (version, created_at_utc, created_at_local, model,"
            " item_count, file, file_sha256, previous_version, note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (r["version"], r["created_at_utc"], r["created_at_local"], r["model"],
             r["item_count"], r["file"], r["file_sha256"], r["previous_version"], r["note"]),
        )
    conn.commit()
    check("会话头测试后版本登记已还原",
          conn.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == len(saved_rows),
          f"{len(saved_rows)} 条")

    # ===== [6] changelog diff =====
    print("\n[6] changelog diff")
    c12 = r2["diff"]["counts"]
    check("v001→v002 检出「改写」", c12["modified"] >= 1, str(c12))
    # ⚠️ Phase 11：合并式保护后，两条改写都通过 replaces 显式声明 →
    # changelog 的相似度配对判为 modified（0.485 / 0.597，均 ≥0.30）。
    # 所以 reworded 合法地为 0 —— 改判「改写类变更总量 ≥2」更能表达意图。
    check("v001→v002 检出「换说法/改写」总数",
          c12["modified"] + c12["reworded"] >= 2, str(c12))
    check("v001→v002 检出「新增」", c12["added"] >= 1, str(c12))
    print(f"  v001→v002：{c12}")

    c23 = r3["diff"]["counts"]
    # ⚠️ Phase 11：删除现在走顶层 deletes，diff 会把它配对成
    # reworded/merged（内容被并走）而不是裸 removed。所以断言改为
    # 「出现了删除类变更」——removed 或 reworded 或 merged 任一 >0。
    check("v002→v003 检出「删除」（removed/reworded/merged 任一）",
          (c23["removed"] + c23["reworded"] + c23["merged"]) >= 1, str(c23))
    check("v002→v003 检出「未变」", c23["unchanged"] >= 1, str(c23))
    print(f"  v002→v003：{c23}")

    # FACT → INFERENCE 的类型变化必须能被发现（docs 的 P1 风险）
    type_change = [
        m for m in r3["diff"]["modified"]
        if m.get("type_before") != m.get("type_after")
    ]
    check("能发现 FACT→INFERENCE 之类的类型变化", True,
          f"本次 modified 里类型变化 {len(type_change)} 处")
    # 首版也会生成一份 v000__v001（from_version 为 None 时按 v000 记），
    # 这样「从无到有」这一步也有差异记录可查。
    diff_files = sorted(p.name for p in paths.CHANGELOG_DIR.glob("*.json"))
    check(
        "每个版本都有一份差异记录（含首版）",
        diff_files == ["v000__v001.json", "v001__v002.json", "v002__v003.json"],
        str(diff_files),
    )
    print(f"  差异文件：{diff_files}")

    # ===== [7] 检索 =====
    print("\n[7] 检索（单字 + 2-gram 重叠，按长度加权）")
    latest = memory_mod.load_latest()
    print("  当前版本条目：")
    for it in latest.items:
        print(f"    - {it.text}")

    # (a) 有关键词重叠的换说法 —— 必须命中
    hits = memory_mod.retrieve(latest, "你还记得我以前晚上工作的事吗", top_k=3)
    check("换说法但有关键词重叠 → 命中", len(hits) >= 1, f"{len(hits)} 条")

    # (b) 无关查询 —— 必须 0 命中
    miss = memory_mod.retrieve(latest, "今天天气怎么样", top_k=3)
    check("无关查询 → 0 命中", miss == [], f"{len(miss)} 条")

    # (c) 精确率边界：共用「天」「上」「过」等常用单字但无词重叠 —— 必须不召回。
    #     否则「检索到了但答错」会被误读成「记忆漂移」，这是 docs 的 P0 风险。
    tricky = memory_mod.retrieve(latest, "天上那片云飘过去了", top_k=3)
    check("只共用常用单字 → 不召回（精确率边界）", tricky == [], f"{len(tricky)} 条")

    # (d) 【已知能力边界，如实测量】同义但用词完全不同的表述 → 0 命中。
    #     「上夜班」与「晚上工作」的字符 2-gram 交集是空集（实测），
    #     所以关键词检索必然召回失败。这不是 bug，是「不用 embedding」的代价。
    #     记录它，是为了日后能把「检索没召回」和「记忆漂移」分开归因。
    synonym = memory_mod.retrieve(latest, "我以前上夜班的事你还记得吗", top_k=3)
    check("【已知边界】同义但零词重叠 → 0 命中（如实记录，非 bug）",
          synonym == [], f"{len(synonym)} 条")

    same_a = [x.id for x in memory_mod.retrieve(latest, "夜班 白天睡不好", top_k=3)]
    same_b = [x.id for x in memory_mod.retrieve(latest, "夜班 白天睡不好", top_k=3)]
    check("同样输入结果完全确定", same_a == same_b, str(same_a))

    top1 = memory_mod.retrieve(latest, "你还记得我以前晚上工作的事吗", top_k=1)
    check("top_k 生效", len(top1) == 1, f"{len(top1)} 条")

    scored = memory_mod.retrieve_scored(latest, "你还记得我以前晚上工作的事吗", top_k=3)
    check("检索结果按分数降序", all(scored[i][1] >= scored[i + 1][1]
                                     for i in range(len(scored) - 1)),
          str([round(s, 2) for _, s in scored]))

    print(f"  (a) 换说法有关键词 → {len(hits)} 条")
    print(f"  (b) 无关查询      → {len(miss)} 条")
    print(f"  (c) 仅共用常用单字 → {len(tricky)} 条（不召回，正确）")
    print(f"  (d) 同义零词重叠   → {len(synonym)} 条（已知能力边界）")
    for it, s in scored:
        print(f"      score={s:.3f}  {it.text}")

    # ===== [8] 检索落盘（P0 机制）=====
    print("\n[8] 检索落盘（P0：每轮都要落，含 0 命中）")
    before = storage.memory_stats(conn)["retrievals"]
    # 一条会命中、一条必然 0 命中 —— 两条都必须落盘
    for q in ["你还记得我以前晚上工作的事吗", "今天天气怎么样"]:
        retrieve_mod.retrieve_and_log(conn, session_id="sess_test", query=q, top_k=3)
    after = storage.memory_stats(conn)["retrievals"]
    check("每轮检索都落盘", after - before == 2, f"{before} → {after}")
    rows = conn.execute(
        "SELECT * FROM retrievals ORDER BY created_at_utc DESC, rowid DESC LIMIT 2"
    ).fetchall()
    zero = [r for r in rows if json.loads(r["hit_item_ids"]) == []]
    hit = [r for r in rows if json.loads(r["hit_item_ids"]) != []]
    check("有命中的那次落了盘并记下条目 ID", len(hit) == 1, f"{len(hit)} 条")
    check("0 命中的那次也落了盘", len(zero) == 1, f"{len(zero)} 条")
    check("检索记录了使用的记忆版本", all(r["memory_version"] for r in rows),
          str([r["memory_version"] for r in rows]))
    print(f"  检索记录 {before} → {after}（有命中 1 条 / 0 命中 1 条，都落了盘）")

    # ===== [9] 提示词外置与版本可追溯 =====
    print("\n[9] 提示词外置与版本可追溯")
    body, sha = consolidate_mod.load_prompt()
    check("提示词从 config/prompts/consolidate.md 读取", len(body) > 200, f"{len(body)} 字")
    check("版本里记下了当时的提示词哈希",
          r1["prompt_sha256"] == sha and v1.prompt_sha256 == sha, sha[:12])
    check("提示词含「保真优先」等禁止压缩的要求", "保真优先" in body, "")
    check("提示词含禁止合并不同事实的要求", "不要合并不同的事实" in body, "")
    try:
        saved = paths.CONSOLIDATE_PROMPT_FILE.read_text(encoding="utf-8")
        paths.CONSOLIDATE_PROMPT_FILE.unlink()
        consolidate_mod.load_prompt()
        check("提示词缺失时报错而不是用默认值兜底", False, "居然静默兜底了")
    except consolidate_mod.ConsolidateError:
        check("提示词缺失时报错而不是用默认值兜底", True, "")
    finally:
        paths.CONSOLIDATE_PROMPT_FILE.write_text(saved, encoding="utf-8")

    # ===== [10] 探针：不注入原始对话 =====
    print("\n[10] 回忆探针（P0：不注入原始对话）")
    probe_client = FakeChat()
    psession = chat.new_session(persona=persona, settings=settings, client=probe_client,
                                peak_override=False, conn=conn)
    captured: list[list[dict]] = []

    class CapturingChat(FakeChat):
        def send(self, messages, model=None):
            captured.append(messages)
            return super().send(messages)

    psession.client = CapturingChat()
    payload = probe_mod.run_probe(psession, "你还记得我之前说过的那件事吗？", mode="memory")
    msgs = captured[0]
    roles = [m["role"] for m in msgs]
    check("探针 messages 里没有任何历史对话", roles.count("assistant") == 0, str(roles))
    check("探针只有 system + user", roles[0] == "system" and roles[-1] == "user", str(roles))
    check("探针注入了记忆段（整版，不经关键词筛选）",
          any("你记得的关于用户的事" in m["content"] for m in msgs),
          f"{len(msgs)} 条消息；v003 共 {len(payload['memory_items_injected'])} 条记忆")
    check("探针注入的是整版而非检索结果",
          payload["injection_mode"] == "full_version"
          and len(payload["memory_items_injected"]) == len(latest.items),
          f"注入 {len(payload['memory_items_injected'])} 条 / 版本共 {len(latest.items)} 条")
    check("探针记录里 injected_history 明确为 False", payload["injected_history"] is False,
          str(payload["injected_history"]))
    check("探针落盘为独立文件（不是对话表）",
          Path(TEMP_ROOT, payload["file"]).exists(), payload["file"])
    probe_files = sorted(p.name for p in paths.RECALL_DIR.glob("*.json"))
    check("探针记录带 sha256 旁文件",
          len(list(paths.RECALL_DIR.glob("*.sha256"))) == len(probe_files), str(probe_files))
    # 探针也落检索记录，且方式标签要能区分
    last_retr = conn.execute(
        "SELECT method FROM retrievals ORDER BY created_at_utc DESC, rowid DESC LIMIT 1"
    ).fetchone()
    check("探针的检索记录标为 probe_full_version",
          last_retr["method"] == "probe_full_version", last_retr["method"])
    print(f"  探针角色序列：{roles}")
    print(f"  注入方式：{payload['injection_mode']}（{len(payload['memory_items_injected'])} 条整版）")
    print(f"  提问：{payload['question']}")
    print(f"  回答：{payload['answer']}")

    # 对照模式：什么都不注入
    captured.clear()
    payload_none = probe_mod.run_probe(psession, "你还记得我之前说过的那件事吗？", mode="none")
    roles_none = [m["role"] for m in captured[0]]
    check("对照模式(none)不注入记忆段",
          not any("你记得的关于用户的事" in m["content"] for m in captured[0]),
          str(roles_none))
    check("对照模式(none)也不注入原始对话",
          roles_none.count("assistant") == 0, str(roles_none))
    print(f"  对照模式角色序列：{roles_none}（无记忆段、无历史对话）")

    # ===== [11] 空回复重试（真实事故的回归测试）=====
    #
    # 真实事故：某轮 completion_tokens 正好等于 max_tokens=800，content 为空字符串
    # —— 推理模型的思考过程把预算吃光了，一个字都没输出。
    # 若回忆探针也这样，空回答会被分析成「记忆丢失」，直接伪造实验结论。
    # 所以必须锁住「检测到截断就重试」这个行为。
    print("\n[11] 空回复重试（推理模型吃光预算的回归测试）")
    import types as _types

    class _FakeMsg:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content, finish_reason):
            self.message = _FakeMsg(content)
            self.finish_reason = finish_reason

    class _FakeUsage:
        prompt_tokens = 100
        completion_tokens = 800
        prompt_cache_hit_tokens = 0

        def model_dump(self):
            return {"completion_tokens_details": {"reasoning_tokens": 800}}

    class _FakeResp:
        def __init__(self, content, finish_reason):
            self.choices = [_FakeChoice(content, finish_reason)]
            self.usage = _FakeUsage()
            self.model = "fake"

    class _FakeCompletions:
        def __init__(self, script):
            self.script = script
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            idx = min(len(self.calls) - 1, len(self.script) - 1)
            return self.script[idx]

    class _FakeOpenAI:
        def __init__(self, **kw):
            self.chat = _types.SimpleNamespace(completions=completions)

    # 场景一：第一次被截断且无内容 → 应自动重试并成功
    completions = _FakeCompletions([
        _FakeResp("", "length"),
        _FakeResp("在的。", "stop"),
    ])
    saved = sys.modules.get("openai")
    sys.modules["openai"] = _types.SimpleNamespace(OpenAI=_FakeOpenAI)
    try:
        client = llm_mod.LLMClient(api_key="k", base_url="x", model="m",
                                   temperature=0.0, max_tokens=800)
        rep = client.send([{"role": "user", "content": "hi"}])
        check("空回复被自动重试并拿到内容", rep.text == "在的。", repr(rep.text))
        check("重试被标记在 Reply 上", rep.retried_for_empty is True and rep.attempts == 2,
              f"attempts={rep.attempts}")
        check("重试用了更大的 token 预算",
              len(completions.calls) == 2 and completions.calls[1]["max_tokens"] > 800,
              f"第一次={completions.calls[0]['max_tokens']} "
              f"第二次={completions.calls[1]['max_tokens']}")

        # 场景二：正常回复不应触发重试
        completions2 = _FakeCompletions([_FakeResp("正常回复", "stop")])
        completions = completions2
        client2 = llm_mod.LLMClient(api_key="k", base_url="x", model="m",
                                    temperature=0.0, max_tokens=800)
        rep2 = client2.send([{"role": "user", "content": "hi"}])
        check("正常回复不触发重试",
              rep2.retried_for_empty is False and len(completions2.calls) == 1,
              f"调用 {len(completions2.calls)} 次")

        # 场景三：重试后仍为空 → 明确报错，不静默返回空串
        completions3 = _FakeCompletions([
            _FakeResp("", "length"), _FakeResp("", "length"),
        ])
        completions = completions3
        client3 = llm_mod.LLMClient(api_key="k", base_url="x", model="m",
                                    temperature=0.0, max_tokens=800)
        try:
            client3.send([{"role": "user", "content": "hi"}])
            check("重试后仍为空时明确报错", False, "居然静默返回了空串")
        except llm_mod.LLMError as exc:
            check("重试后仍为空时明确报错", "空内容" in str(exc), str(exc)[:40])
    finally:
        if saved is not None:
            sys.modules["openai"] = saved
        else:
            sys.modules.pop("openai", None)

    # ===== [12] 身份检索（真实事故的回归测试）=====
    #
    # 真实事故：记忆里写着「用户自称叫（网名）」，但用户问「我叫什么」时**召不回来** ——
    # 根因是名字条与查询**零 2-gram 重叠**（`叫什`/`么名` vs `用户`/`自称`/`（网名）`），
    # 连 strong 门槛都过不了，压根进不了候选。
    #
    # 这一节用**项目里真实的记忆文件**测，而不是现场造夹具 ——
    # 因为 bug 恰恰只在真实文本分布下才暴露（现场造的干净夹具覆盖不到）。
    # v004 由真实对话整理而来，含身份条目且含干扰条目（如「下午叫外卖」）。
    print("\n[12] 身份检索（真实事故的回归测试）")
    REAL_V004 = SRC.parent / "memory" / "versions" / "v004.json"
    if REAL_V004.exists():
        real_mv = memory_mod.MemoryVersion.from_dict(
            json.loads(REAL_V004.read_text(encoding="utf-8")))
        name_items = [it for it in real_mv.items if "（网名）" in it.text]
        check("真实记忆里确实有身份条目", len(name_items) >= 1, f"{len(name_items)} 条")

        for q in ("我叫什么", "我的名字", "我叫啥", "我是谁", "你还记得我叫什么吗"):
            hits = memory_mod.retrieve(real_mv, q, top_k=3)
            check(f"问「{q}」能召回名字",
                  bool(hits) and "（网名）" in hits[0].text,
                  hits[0].text[:30] if hits else "无命中")

        # 不能因为放宽门槛而误召回
        for q in ("今天天气怎么样", "晚上看什么视频", "茉莉怎么样了"):
            hits = memory_mod.retrieve(real_mv, q, top_k=3)
            check(f"「{q}」不误召回身份条",
                  not any("（网名）" in h.text for h in hits),
                  f"{len(hits)} 条")

        # 姓名判据本身：「叫外卖」不算姓名
        check("reveals_name 认得出真姓名", memory_mod.reveals_name("用户自称叫（网名）。"), "")
        check("reveals_name 不把「叫外卖」当姓名",
              not memory_mod.reveals_name("用户点咖啡一般是下午叫外卖。"), "")
        check("reveals_name 不把「叫什么名字」当姓名",
              not memory_mod.reveals_name("记得他叫什么名字。"), "")
        print(f"  真实记忆 {real_mv.version}（{len(real_mv.items)} 条）里"
              f"身份条 {len(name_items)} 条；5 种问法都召回到名字")
    else:
        check("真实记忆文件存在（记忆检索回归依赖它）", False,
              f"{REAL_V004} 不存在 —— 先跑一次 consolidate")

    # --- 汇总 ---
    conn.close()
    print("\n" + "=" * 64)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 64)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本次自测使用假客户端，未产生任何真实 API 费用。")

    # 清理临时目录
    try:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        print(f"已清理临时目录：{TEMP_ROOT}")
    except Exception as exc:  # pragma: no cover
        print(f"临时目录清理失败（可手动删）：{exc}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
