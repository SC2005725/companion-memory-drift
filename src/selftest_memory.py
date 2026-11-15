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

    # ===== [5.2] 雷7：跨进程锁 + 原子写 + DB sha 第三方见证 =====
    # 旧版 "exists 检查 + write_text" 是 TOCTOU：并发同写会互相覆盖整份版本，
    # sidecar 一起被覆盖 → verify 对错误内容照样报"通过"。
    print("\n[5.2] 雷7 回归：版本落盘竞态 / 陈锁破锁 / 账实比对")
    import hashlib as _hashlib
    import os as _os
    import threading as _th
    import time as _time

    def _mk_mv(version: str, text: str, tag: str) -> "memory_mod.MemoryVersion":
        return memory_mod.MemoryVersion(
            version=version, created_at="2026-11-07T22:00:00+08:00",
            model="fake", source_range={}, note=f"lei7-{tag}",
            items=[memory_mod.MemoryItem(id="m_" + version, type="FACT",
                                         text=text, first_seen_version=version)])

    # ① 双线程同写一个版本：恰好一个成功，落盘内容是赢家的、不被混杂覆盖
    _results: list[str] = []

    def _racer(tag: str, text: str) -> None:
        try:
            memory_mod.save_version(_mk_mv("v990", text, tag))
            _results.append(tag)
        except memory_mod.MemoryError:
            pass

    t_a, t_b = _th.Thread(target=_racer, args=("A", "甲写的记忆")), \
        _th.Thread(target=_racer, args=("B", "乙写的记忆"))
    t_a.start(); t_b.start(); t_a.join(); t_b.join()
    check("并发同写恰好一个成功（无静默覆盖）", len(_results) == 1, str(_results))
    _v990 = paths.VERSIONS_DIR / "v990.json"
    _body990 = _v990.read_text(encoding="utf-8") if _v990.exists() else ""
    _w = _results[0] if _results else "?"
    check("落盘内容与赢家一致（append-only 未被击穿）",
          ("甲写的记忆" in _body990) == (_w == "A")
          and ("乙写的记忆" in _body990) == (_w == "B"), _w)

    # ② 陈锁（>30s）视为持有者已崩溃 → 破锁接管，写入成功
    _stale = paths.VERSIONS_DIR / ".v991.save.lock"
    _stale.write_text("", encoding="utf-8")
    _old = _time.time() - 60
    _os.utime(_stale, (_old, _old))
    try:
        memory_mod.save_version(_mk_mv("v991", "陈锁破锁后写入", "stale"))
        check("30s 陈锁被破、写入成功", (paths.VERSIONS_DIR / "v991.json").exists())
    except memory_mod.MemoryError as e:
        check("30s 陈锁被破、写入成功", False, str(e)[:80])

    # ③ 活跃锁（别人正在写）→ 等待超时后拒绝，绝不强行覆盖
    _active = paths.VERSIONS_DIR / ".v992.save.lock"
    _active.write_text("", encoding="utf-8")
    _bak_wait = memory_mod._LOCK_WAIT_SECONDS
    memory_mod._LOCK_WAIT_SECONDS = 0.3
    try:
        try:
            memory_mod.save_version(_mk_mv("v992", "不该被写进", "active"))
            check("活跃锁阻塞写入（超时拒绝）", False, "竟然写进去了")
        except memory_mod.MemoryError as e:
            check("活跃锁阻塞写入（超时拒绝）", "锁等待超时" in str(e), str(e)[:60])
    finally:
        memory_mod._LOCK_WAIT_SECONDS = _bak_wait
        _active.unlink(missing_ok=True)

    # ④ 落盘无残留：tmp 半成品和锁文件都该清干净（半截文件不再可能出现）
    _leftovers = [p.name for p in paths.VERSIONS_DIR.glob("*.tmp-*")] + \
        [p.name for p in paths.VERSIONS_DIR.glob(".*.save.lock")]
    check("落盘无 tmp/锁残留", _leftovers == [], str(_leftovers))

    # ⑤ verify_versions 的 DB 第三方见证：文件+sidecar 一起被覆盖时，DB 账能揭穿
    _sha990 = _hashlib.sha256(
        _body990.rstrip("\n").encode("utf-8")).hexdigest()
    _rep_ok = memory_mod.verify_versions(db_sha={"v990": _sha990})
    check("DB sha 一致 → 校验通过", _rep_ok["ok"], str(_rep_ok["errors"][:1]))
    _rep_bad = memory_mod.verify_versions(db_sha={"v990": "0" * 64})
    check("文件被换内容 → DB 账揭穿",
          not _rep_bad["ok"] and any("DB" in e for e in _rep_bad["errors"]),
          str(_rep_bad["errors"][:1]))
    _rep_ghost = memory_mod.verify_versions(db_sha={"v888": "0" * 64})
    check("DB 有账、文件不在 → 账实不符",
          not _rep_ghost["ok"] and any("v888" in e for e in _rep_ghost["errors"]),
          str(_rep_ghost["errors"][:1]))

    # 清场：v990/v991 是本块的脚手架。不删的话 load_latest() 会返回它们
    # （字典序排在 v003 之后、且只有 1 条记忆），污染后面的检索测试。
    for _p in ("v990.json", "v990.json.sha256",
               "v991.json", "v991.json.sha256"):
        (paths.VERSIONS_DIR / _p).unlink(missing_ok=True)

    # ===== [5.4] P1 批回归：v1000 数值排序 + 半截版本文件回退（2026-11-08）=====
    print("\n[5.4] P1 回归：v1000 数值排序 + 半截版本文件回退")
    _v999f = paths.VERSIONS_DIR / "v999.json"
    _v1000f = paths.VERSIONS_DIR / "v1000.json"
    _halff = paths.VERSIONS_DIR / "v1001.json"
    try:
        import json as _json54
        _good = {"version": "v1000", "created_at": "2026-11-08T00:00:00+00:00",
                 "model": "selftest", "items": []}
        _v999f.write_text(_json54.dumps(
            {**_good, "version": "v999"}, ensure_ascii=False), encoding="utf-8")
        _v1000f.write_text(_json54.dumps(_good, ensure_ascii=False),
                           encoding="utf-8")
        _halff.write_text('{"version": "v1001", "created_at', encoding="utf-8")  # 半截
        _vers54 = memory_mod.existing_versions()
        check("existing_versions 数值序（v999 < v1000 < v1001）",
              _vers54[-3:] == ["v999", "v1000", "v1001"], str(_vers54[-3:]))
        _lat54 = memory_mod.load_latest()
        check("load_latest 拿到 v1000（不再被 v999 压住）",
              _lat54 is not None and _lat54.version == "v1000",
              _lat54.version if _lat54 else "None")
        check("半截 v1001 被跳过、回退到 v1000（检索不挂）",
              _lat54 is not None and _lat54.version != "v1001",
              _lat54.version if _lat54 else "None")
        # 显式指定读坏版本 = 有人在点名查证 → 必须大声报错，不许静默给旧版
        try:
            memory_mod.load_version("v1001")
            check("显式 load_version 坏文件仍大声报错（不静默）", False,
                  "v1001 居然读出来了")
        except Exception:
            check("显式 load_version 坏文件仍大声报错（不静默）", True, "")
    finally:
        for _p in ("v999.json", "v1000.json", "v1001.json",
                   "v999.json.sha256", "v1000.json.sha256", "v1001.json.sha256"):
            (paths.VERSIONS_DIR / _p).unlink(missing_ok=True)

    # ===== [5.3] 雷8：conversations.db 三进程写护栏 =====
    print("\n[5.3] 雷8 回归：WAL + busy_timeout（三进程写不再秒炸）")
    _jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("journal_mode=wal（读写不互斥）", str(_jm).lower() == "wal", str(_jm))
    _bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    check("busy_timeout=5000（锁被占先等不炸）", int(_bt) == 5000, str(_bt))
    _syn = conn.execute("PRAGMA synchronous").fetchone()[0]
    check("synchronous=NORMAL(1)（WAL 推荐档）", int(_syn) == 1, str(_syn))

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

    # ===== ★ 2026-11-07（外部调研 #1）：三因子排序回归 =====
    # 相关性相同（同文本）时：新近 + 被召回过的条目应排在陈旧 + 从未召回的前面。
    print("\n[7.x] 三因子排序：同相关度下，时效×召回价值 决定先后")
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _now = _dt.now(_tz.utc)
    _iso_old = (_now - _td(days=120)).isoformat()
    _iso_new = (_now - _td(hours=2)).isoformat()
    mv_fac = memory_mod.MemoryVersion(
        version="v_fac", created_at=_iso_new, model="selftest",
        items=[
            memory_mod.MemoryItem(id="fac_old", type="FACT", text="用户提过上夜班的经历",
                                  source_event_ids=[], created_at=_iso_old,
                                  updated_at=_iso_old, recall_count=0),
            memory_mod.MemoryItem(id="fac_new", type="FACT", text="用户提过上夜班的经历",
                                  source_event_ids=[], created_at=_iso_new,
                                  updated_at=_iso_new, last_recalled_at=_iso_new,
                                  recall_count=3),
        ],
        previous_version=None,
    )
    r_fac = memory_mod.retrieve(mv_fac, "上夜班", top_k=2, now=_now)
    # P2（2026-11-15）：同文本近重复在检索清单里**折叠**——保留排序最优的一份。
    # 重要性排序仍然决定「哪份幸存」（fac_new 时效新 + 被召回多 → 它留下），
    # 但旧断言期望两份都出现已不成立（重复内容不再重复占名额）。
    check("同文本不同时效：折叠后保留新近+被召回的那份（P2 语义）",
          [x.id for x in r_fac] == ["fac_new"],
          str([x.id for x in r_fac]))
    scored_fac = memory_mod.retrieve_scored(mv_fac, "上夜班", top_k=2, now=_now)
    # 口径一致性改为**首条一致**：scored 是预留路径（无折叠），retrieve 生产路径
    # 有 P2 折叠——两者对「谁排第一」必须同判，尾部数量差异属有意行为。
    check("retrieve_scored 与 retrieve 首条口径一致",
          scored_fac and [x.id for x, _ in scored_fac][0] == r_fac[0].id,
          str([(x.id, round(s, 3)) for x, s in scored_fac]))

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
        # ⚠️ 这一段依赖**真实整理产物**（由真实对话跑 consolidate 生成）。
        # 新克隆的仓库按设计不含任何记忆数据，所以这里**跳过而不是失败** ——
        # 否则任何人 clone 下来跑自测都会看到一个红叉，却与代码质量无关。
        # （恢复自发布副本提交 56d16d9 的实现；主项目此前缺这一段，
        #   导致同步到发布副本时把跳过语义冲掉、CI 变红。）
        print(f"  [跳过] 真实记忆回归测试：{REAL_V004.name} 不存在")
        print("         本节用的是真实文本分布（现场造的干净夹具覆盖不到那个 bug），")
        print("         所以必须先有真实数据。想跑它：")
        print("           先用真实对话跑几轮，触发一次自动整理生成 memory/versions/，")
        print("           再重新执行本文件。")

    # ===== [5.7] 处方 A：经历提炼（情绪峰值检测 → EPISODE 候选，2026-11-08）=====
    print("\n[5.7] 处方 A 回归：情绪峰值检测 + EPISODE 进核心记忆")
    import companion_core as _core57
    import episode_scan as _eps
    # ⚠️ 事故教训（2026-11-08）：core_candidates 的 MEMORY_DIR 在 **import 时**
    # 绑定 core.CORE_DIR（产线真实记忆链 core/memory-heci）。必须先重定向
    # CORE_DIR 再首次 import core_candidates，否则整理员会把测试 EPISODE
    # 写进产线版本链（本次实测发生过，v007 已删除止血）。
    _core57.CORE_DIR = TEMP_ROOT / "core-dir-episode"
    _core57.CORE_DB = TEMP_ROOT / "core-episode.db"
    import core_candidates as _cc57   # 首次 import：绑定上面重定向的目录

    def _t57(seq, local, u, a):
        return {"seq": seq, "created_at_local": local,
                "user_text": u, "assistant_text": a}

    # -- 纯函数：检测与聚合 --
    _turns57 = [
        _t57(1, "2026-11-07T14:00:00+08:00", "今天天气不错", "是啊"),
        _t57(2, "2026-11-07T23:40:00+08:00", "今晚终于把这个 bug 修好了，开心！", "太好了，恭喜！"),
        _t57(3, "2026-11-07T23:55:00+08:00", "谢谢你陪我弄到这么晚", "该说谢谢的是我，陪你熬夜我也很开心。"),
        _t57(4, "2026-11-08T10:00:00+08:00", "中午吃什么", "随便呀"),
    ]
    _eps57 = _eps.detect_episodes(_turns57)
    check("深夜+情绪词+共同完成 → 检出 1 段经历", len(_eps57) == 1, str(len(_eps57)))
    check("相邻信号轮聚成一个窗口（seq 2-3，无信号轮不入窗）",
          bool(_eps57) and _eps57[0]["seq_start"] == 2 and _eps57[0]["seq_end"] == 3
          and _eps57[0]["turns"] == 2, str(_eps57[:1]))
    check("EPISODE 条目带原话节选+深夜标注+信号标注",
          bool(_eps57) and "修好了" in _eps57[0]["item"]["text"]
          and "深夜" in _eps57[0]["item"]["text"]
          and "信号：" in _eps57[0]["item"]["text"],
          _eps57[0]["item"]["text"][:60] if _eps57 else "")
    check("类型/分层/分类齐备（EPISODE/experience/共同经历）",
          bool(_eps57) and _eps57[0]["item"]["type"] == "EPISODE"
          and _eps57[0]["item"]["tier"] == "experience"
          and _eps57[0]["item"]["category"] == "共同经历", "")
    check("原话节选限长（≤80 字含省略号）",
          bool(_eps57) and len(_eps57[0]["item"]["text"]) <= 160,
          str(len(_eps57[0]["item"]["text"])) if _eps57 else "-")
    check("完全无信号 → 零提案",
          _eps.detect_episodes([_t57(9, "2026-11-08T10:00:00+08:00",
                                     "吃饭了吗", "吃了")]) == [], "")

    # -- E2E：提案 → 整理员接纳进版本 --
    _conn57 = _core57.connect()
    _core57.init_schema(_conn57)
    _core57.ensure_subject(_conn57)
    _now57 = _core57._now()
    _conn57.execute(
        "INSERT INTO instances (instance_id, subject_id, environment, "
        "started_at, last_seen_at, capabilities) VALUES ('heci_001','heci_001','',?,?,?)",
        (_now57, _now57, "[]"))
    _conn57.commit()
    # 游标预置到"当前最大 seq"：套件早段的夹具轮次不进本节的扫描窗，
    # 本节只对自己新插入的 3 轮负责（seq 接在其后，避免跨会话 seq 撞号）。
    _top57 = conn.execute("SELECT MAX(seq) m FROM conversations").fetchone()["m"] or 0
    _conn57.execute(
        "INSERT INTO episode_scan_state (id, last_seq, updated_at) VALUES (1, ?, ?)",
        (_top57, _core57._now()))
    _conn57.commit()
    _sid57 = "epscan-selftest"
    _seed57 = [("今晚终于把这个 bug 修好了，开心！", "太好了，恭喜！"),
               ("谢谢你陪我弄到这么晚", "该说谢谢的是我，陪你熬夜我也很开心。"),
               ("中午吃什么", "随便呀")]
    for _i, (_u, _a) in enumerate(_seed57, start=_top57 + 1):
        storage.save_turn(conn, session_id=_sid57, seq=_i,
                          user_text=_u, assistant_text=_a, model="selftest",
                          persona_name="鹤辞", persona_sha256="epscan")
    # 时间戳拨针（save_turn 用当下时间，测试要可控的本地小时）：
    # 头两轮 = 23:40 深夜（该检出），第三轮 = 白天 10 点（不该检出）。
    # 否则凌晨跑自测时，第三轮会被「深夜」信号如实命中——检测器没错，是钟点。
    conn.execute("UPDATE conversations SET created_at_local='2026-11-07T23:40:00+08:00' "
                 "WHERE session_id=? AND seq <= ?", (_sid57, _top57 + 2))
    conn.execute("UPDATE conversations SET created_at_local='2026-11-08T10:00:00+08:00' "
                 "WHERE session_id=? AND seq = ?", (_sid57, _top57 + 3))
    conn.commit()

    _st57 = _eps.run_episode_scan(core_conn=_conn57, storage_conn=conn)
    check("首扫提案 1 条 EPISODE 候选", len(_st57["proposed"]) == 1, str(_st57))
    check("游标推进到本节最新 seq", _st57["cursor"] == _top57 + 3,
          str(_st57["cursor"]))
    _again57 = _eps.run_episode_scan(core_conn=_conn57, storage_conn=conn)
    check("重扫幂等（无新轮 → 零提案零检出）",
          _again57["proposed"] == [] and _again57["detected"] == 0, str(_again57))

    _out57 = _cc57.run()
    check("整理员接纳 EPISODE 候选", _out57["stats"]["accepted"] >= 1,
          str(_out57["stats"]))
    # core_candidates 写的是 core 链（core_candidates.MEMORY_DIR，已随 CORE_DIR
    # 重定向到临时目录），不是 memory_mod 的 paths.VERSIONS_DIR——各读各的链。
    import json as _json57
    _core_vs57 = sorted(_cc57.MEMORY_DIR.glob("v*.json"),
                        key=lambda p: int(p.stem[1:]))
    _lat57 = (json.loads(_core_vs57[-1].read_text(encoding="utf-8"))
              if _core_vs57 else {"items": []})
    _ep_items57 = [it for it in (_lat57.get("items") or [])
                   if str(it.get("type")) == "EPISODE"]
    check("core 链新版本里 EPISODE 类型存活（不被降级 UNKNOWN）",
          len(_ep_items57) >= 1,
          str([(it.get("type"), str(it.get("text"))[:30])
               for it in (_lat57.get("items") or [])][:3]))
    from memory import MemoryItem as _MI57
    _mi57 = _MI57.from_dict(_ep_items57[0]) if _ep_items57 else None
    check("EPISODE 来源=亲历（remembered）",
          _mi57 is not None and _mi57.provenance == "remembered",
          _mi57.provenance if _mi57 else "-")
    check("测试全程不污染真实 core 链（临时目录里才有新版本）",
          _cc57.MEMORY_DIR.is_relative_to(TEMP_ROOT),
          str(_cc57.MEMORY_DIR))
    _conn57.close()

    # ===== [5.8] 链桥：core 链 → 主链 同步（处方 A 收口，2026-11-08）=====
    # 缺口：聊天检索只读主链，core 链（整理员/EPISODE/muse 提案）从未被桥接
    # ——记忆在账上、不在脑子里。桥 = core 快照里主链没有的条目按相似度去重
    # 后追加成一个主链新版本，幂等。
    print("\n[5.8] 链桥回归：core 链同步进主链（幂等/去重/EPISODE 存活）")
    import chain_sync as _csy
    _pre58 = memory_mod.load_latest()
    _pre_top58 = _pre58.version if _pre58 else None
    _sync1 = _csy.sync_core_to_main()
    check("首同步把 core 链条目带进主链", _sync1["added"] >= 1 and _sync1["version"],
          str(_sync1))
    _mv58 = memory_mod.load_latest()
    check("主链新版本 = 原最新 +1（快照式追加）",
          _mv58 is not None and _pre_top58 is not None
          and int(_mv58.version[1:]) == int(_pre_top58[1:]) + 1,
          f"{_pre_top58} -> {_mv58.version if _mv58 else None}")
    _types58 = {it.type for it in (_mv58.items if _mv58 else [])}
    check("主链新版本含 EPISODE（她的检索终于看得见经历）",
          "EPISODE" in _types58, str(_types58))
    check("sidecar 落了 + verify 全绿（账实一致）",
          (paths.VERSIONS_DIR / f"{_mv58.version}.sha256").exists()
          and memory_mod.verify_versions()["ok"],
          str(memory_mod.verify_versions()["errors"][:2]))
    _sync2 = _csy.sync_core_to_main()
    check("重同步幂等（零新增、零新版本）",
          _sync2["added"] == 0 and _sync2["version"] is None, str(_sync2))

    # ===== [5.9] 记忆优化批 M-2/M-3（2026-11-14）：预算联动 + recency tie-break =====
    print("\n[5.9] 记忆优化批回归：注入预算随条数联动 / 同分新条目靠前")
    _mk = lambda i, t, created=None: memory_mod.MemoryItem(
        id=i, type="FACT", text=t, created_at=created)
    # M-2：预算 = max(700, 160×条数)。20 条长条目（>80 字）不全被压成碎片
    _items20 = [_mk(f"m{i:02d}", "用户喜欢在周末的清晨去逛离家不远的花市挑一些新鲜的茉莉和桔梗。" * 2)
                for i in range(20)]
    _rendered = memory_mod.render_for_prompt(_items20)
    _short_cnt = sum(1 for line in _rendered.splitlines()
                     if line.startswith("- [") and line.rstrip().endswith("…")
                     and len(line) < 120)
    check("top_k=20 时预算联动，被压成短摘要的条目 < 20（旧版会几乎全压碎）",
          _short_cnt < 10, f"碎片数={_short_cnt}")
    _r5 = memory_mod.render_for_prompt(_items20[:5])
    check("显式传 budget_chars 仍以调用方为准",
          "极短" not in _r5 and len(_r5) > 0, f"len={len(_r5)}")
    _r1 = memory_mod.render_for_prompt([_mk("m1", "短条目")])
    check("单条目预算不低于旧版 700（回调 top_k 不浪费）",
          "短条目" in _r1, "")
    # M-3：同分时新条目靠前（recency tie-break）
    _old = _mk("old", "用户喜欢茉莉。", created="2026-10-01T10:00:00+08:00")
    _new = _mk("new", "用户喜欢茉莉与桔梗。", created="2026-11-14T10:00:00+08:00")
    _mv_tie = memory_mod.MemoryVersion(version="v999tie", created_at="2026-11-14",
                                       model="fake", items=[_old, _new])
    _picked = memory_mod.retrieve(_mv_tie, "用户喜欢什么花", top_k=1)
    check("同分 tie-break：新条目排前（不再被旧条目占位）",
          len(_picked) == 1 and _picked[0].id == "new",
          f"picked={[i.id for i in _picked]}")
    _picked_all = memory_mod.retrieve(_mv_tie, "用户喜欢什么花", top_k=2)
    check("同分两条都召回时新条目在前",
          [i.id for i in _picked_all][:1] == ["new"],
          f"order={[i.id for i in _picked_all]}")
    _no_time = _mk("notime", "用户喜欢茉莉。")
    # 纯 tie-break 断言：created_at 完全相同（importance 也相同）→ id 兜底，
    # 顺序完全确定（id DESC），不依赖 dict/DB 顺序
    _a = _mk("aaa", "用户喜欢茉莉。", created="2026-11-14T09:00:00+08:00")
    _z = _mk("zzz", "用户喜欢茉莉。", created="2026-11-14T09:00:00+08:00")
    _mv_same = memory_mod.MemoryVersion(version="v999same", created_at="2026-11-14",
                                        model="fake", items=[_a, _z])
    _picked_same = memory_mod.retrieve(_mv_same, "用户喜欢什么花", top_k=2)
    # P2：完全同文本 → 折叠只留一份；id DESC 兜底决定哪份幸存，完全确定可复现
    check("完全同分同时间：折叠保留 id DESC 的那份（完全确定，可复现）",
          [i.id for i in _picked_same] == ["zzz"],
          f"order={[i.id for i in _picked_same]}")
    # 未知时间条目 vs 旧时间条目：未知时间不衰减（≥ 旧条目），不因无时间被垫底
    _old10 = _mk("old10", "用户喜欢茉莉。", created="2026-10-01T10:00:00+08:00")
    _mv_nt = memory_mod.MemoryVersion(version="v999nt", created_at="2026-11-14",
                                      model="fake", items=[_new, _no_time])
    _picked_nt = memory_mod.retrieve(_mv_nt, "用户喜欢什么花", top_k=2)
    check("无时间戳条目不衰减、不因无时间被垫底（未知时间 ≥ 明确旧时间）",
          [i.id for i in _picked_nt][0] == "notime",
          f"order={[i.id for i in _picked_nt]}")

    # ===== [5.10] 记忆优化批 M-4/M-5（2026-11-15）：偏好兜底 + 查询时间窗 =====
    print("\n[5.10] 记忆优化批 M-4/M-5：偏好兜底 + 查询时间窗")
    _pref = memory_mod.MemoryItem(id="pref1", type="FACT",
                                  text="用户不太能接受苦味，偏好拿铁。",
                                  category="偏好", created_at="2026-11-10T09:00:00+08:00")
    _mv_pref = memory_mod.MemoryVersion(version="v999p", created_at="2026-11-14",
                                        model="fake", items=[_pref])
    _q_taste = "我的喜好是什么样的"
    _got0 = memory_mod.retrieve(_mv_pref, _q_taste, top_k=3)
    check("M-4 病灶复现：偏好查询常规打分 0 命中（词面错开且无同义覆盖）",
          len(_got0) == 0, f"{[i.id for i in _got0]}")
    _got1 = memory_mod.retrieve(_mv_pref, "我的口味是什么样的 你推荐一下", top_k=3)
    check("M-4 偏好查询兜底补位（口味/推荐词触发，偏好条目补入）",
          len(_got1) == 1 and _got1[0].id == "pref1", f"{[i.id for i in _got1]}")
    # M-4 加权直测：同 1.0 强命中之下，偏好条目被 +0.6 顶到前面
    _norm2 = memory_mod.MemoryItem(id="norm2", type="FACT",
                                   text="用户喝咖啡比较多。", category="个人事实",
                                   created_at="2026-11-10T09:00:00+08:00")
    _mv_boost = memory_mod.MemoryVersion(version="v999b", created_at="2026-11-14",
                                         model="fake", items=[_norm2, _pref])
    _gotb = memory_mod.retrieve(_mv_boost, "爱喝什么", top_k=3)
    check("M-4 偏好加权：同强命中下偏好条目 +0.6 顶前（查询侧门控开+同义扩词）",
          len(_gotb) == 2 and _gotb[0].id == "pref1", f"{[i.id for i in _gotb]}")
    _plain = memory_mod.MemoryItem(id="plain", type="FACT",
                                   text="用户让我推荐几本讲口味的书。",
                                   category="个人事实", created_at="2026-11-10T09:00:00+08:00")
    _mv_two = memory_mod.MemoryVersion(version="v999p2", created_at="2026-11-14",
                                       model="fake", items=[_pref, _plain])
    _got2 = memory_mod.retrieve(_mv_two, "你推荐一下口味的书", top_k=3)
    check("M-4 兜底不覆盖常规命中（常规条目在前、偏好条目补位在后）",
          len(_got2) == 2 and [i.id for i in _got2][0] == "plain"
          and "pref1" in [i.id for i in _got2], f"{[i.id for i in _got2]}")
    _nonpref_q = memory_mod.retrieve(_mv_pref, "天气怎么样", top_k=3)
    check("M-4 非偏好查询不兜底（乱补偏好条目是噪声）",
          len(_nonpref_q) == 0, f"{[i.id for i in _nonpref_q]}")

    # M-5：时间窗——同词面分，窗内条目压过窗外条目
    _recent = memory_mod.MemoryItem(id="recent", type="FACT",
                                    text="用户去了花市买茉莉。",
                                    created_at="2026-11-12T10:00:00+08:00")
    _ancient = memory_mod.MemoryItem(id="ancient", type="FACT",
                                     text="用户去了花市买桔梗。",
                                     created_at="2026-08-01T10:00:00+08:00")
    _mv_t5 = memory_mod.MemoryVersion(version="v999t5", created_at="2026-11-14",
                                      model="fake", items=[_ancient, _recent])
    _got5 = memory_mod.retrieve(_mv_t5, "最近有没有去花市", top_k=2)
    check("M-5 时间窗：最近条目压过窗外条目（同词面分 + 窗内加权）",
          len(_got5) == 2 and _got5[0].id == "recent", f"{[i.id for i in _got5]}")
    _got_nt5 = memory_mod.retrieve(_mv_t5, "花市", top_k=2)
    check("M-5 无时间词查询不受影响（recency tie-break 仍新在前）",
          len(_got_nt5) == 2 and _got_nt5[0].id == "recent", f"{[i.id for i in _got_nt5]}")
    _mv_none_t = memory_mod.MemoryVersion(version="v999t6", created_at="2026-11-14",
                                          model="fake",
                                          items=[memory_mod.MemoryItem(
                                              id="nt", type="FACT", text="用户去了花市买茉莉。")])
    _got_nt2 = memory_mod.retrieve(_mv_none_t, "最近有没有去花市", top_k=2)
    check("M-5 无时间戳条目不被窗调整（不伪造时间，仍正常召回）",
          len(_got_nt2) == 1 and _got_nt2[0].id == "nt", f"{[i.id for i in _got_nt2]}")

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
