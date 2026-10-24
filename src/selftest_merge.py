"""Phase 11 · 合并式记忆整理自测（合并式保护 + 来源校验 + 异常拦截）

## 覆盖

  M1  模型漏写旧条目 → 旧条目**原样保留**（不视为删除）
  M2  模型显式 deletes + 有 reason + 目标存在 → 才允许删除
  M3  deletes 目标不存在 → 拒绝该删除
  M4  deletes 没有 reason → 拒绝该删除
  M5  deletes 不是数组 → 整体忽略（旧条目全保留）
  M6  replaces 指向存在的旧 id → 该旧条目被取代（不重复保留）
  M7  replaces 指向不存在的 id → 忽略并警告
  M8  虚构 source_event_ids → 被剔除（既有 parse_items 校验）
  M9  来源字段缺失 → 有 warning（Phase 11-3 修复）
  M10 重复来源 → 保序去重（Phase 11-3 修复）
  M11 空 items + 无 deletes → 旧条目全部保留（**不是清空**）
  M12 第一版（无旧条目）→ 正常产出
  M13 build_user_message 真的把旧条目列进了输入
  M14 旧条目保留时 first_seen_version 不被改写
  M15 save_version 拒绝覆盖既有版本
  M16 异常 payload（items 不是数组）→ 抛 ConsolidateError

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

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_merge_"))
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
(TEMP_ROOT / ".env").write_text("LLM_API_KEY=fake\nLLM_MODEL=m\n", encoding="utf-8")
(TEMP_ROOT / "personality").mkdir(parents=True, exist_ok=True)
(TEMP_ROOT / "personality" / "personality.json").write_text(
    '{"name":"小满","description":"t","system_prompt":"你是小满。"}', encoding="utf-8")

for m in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
          "chat", "verify", "config", "persona", "probe", "relationship",
          "user_model", "events", "llm", "model_router", "archive_anchor", "heartbeat"):
    sys.modules.pop(m, None)

import memory as memory_mod  # noqa: E402
from consolidate import (  # noqa: E402
    ConsolidateError, build_user_message, merge_items, parse_items,
)

CHECKS: list[tuple[str, bool, str]] = []
KNOWN = {"e1", "e2", "e3"}


def check(name, good, detail=""):
    CHECKS.append((name, bool(good), detail))


def mk_prev():
    """造 3 条旧条目（模拟上一版）。"""
    return [
        memory_mod.MemoryItem(id="mem_old1", type="FACT", text="用户养了一盆茉莉。",
                              source_event_ids=["e1"], confidence=0.9,
                              first_seen_version="v001", last_modified_version="v001"),
        memory_mod.MemoryItem(id="mem_old2", type="FACT", text="用户喜欢喝茶。",
                              source_event_ids=["e2"], confidence=0.8,
                              first_seen_version="v001", last_modified_version="v001"),
        memory_mod.MemoryItem(id="mem_old3", type="UNKNOWN", text="卖家说是去年的苗。",
                              source_event_ids=[], confidence=0.6,
                              first_seen_version="v001", last_modified_version="v001"),
    ]


def run(payload, prev=None):
    return merge_items(payload, KNOWN, "v999", prev if prev is not None else mk_prev())


def main() -> int:
    print("=" * 76)
    print("合并式记忆整理 · 自测")
    print("=" * 76)

    # ---------------- M1 漏写 → 保留 ----------------
    print("\n[M1] 模型完全漏写旧条目 → 必须原样保留")
    items, warns = run({"items": [
        {"type": "FACT", "text": "用户最近在军训。", "source_event_ids": []}]})
    ids = {i.id for i in items}
    check("新条目在", any("军训" in i.text for i in items))
    check("★三条旧条目全部保留",
          {"mem_old1", "mem_old2", "mem_old3"} <= ids, str(sorted(ids)))
    check("有『保留』warning", any("未提及" in w for w in warns), str(warns))

    # ---------------- M2 合法删除 ----------------
    print("\n[M2] 合法删除（目标存在 + 有 reason）")
    items, warns = run({
        "items": [],
        "deletes": [{"id": "mem_old3", "reason": "用户已更正，该说法作废"}]})
    ids = {i.id for i in items}
    check("mem_old3 已删除", "mem_old3" not in ids)
    check("其余两条保留", {"mem_old1", "mem_old2"} <= ids, str(sorted(ids)))

    # ---------------- M3 目标不存在 ----------------
    print("\n[M3] deletes 目标不存在 → 拒绝")
    items, warns = run({"items": [], "deletes": [{"id": "mem_NOPE", "reason": "x"}]})
    ids = {i.id for i in items}
    check("三条旧条目全在", {"mem_old1", "mem_old2", "mem_old3"} <= ids)
    check("有『不存在』warning", any("不存在" in w for w in warns), str(warns))

    # ---------------- M4 无 reason ----------------
    print("\n[M4] deletes 无 reason → 拒绝")
    items, warns = run({"items": [], "deletes": [{"id": "mem_old1", "reason": "  "}]})
    ids = {i.id for i in items}
    check("mem_old1 仍在", "mem_old1" in ids)
    check("有『reason』warning", any("reason" in w for w in warns), str(warns))

    # ---------------- M5 deletes 非数组 ----------------
    print("\n[M5] deletes 不是数组 → 整体忽略")
    items, warns = run({"items": [], "deletes": "mem_old1"})
    ids = {i.id for i in items}
    check("三条全保留", {"mem_old1", "mem_old2", "mem_old3"} <= ids)
    check("有 warning", any("不是数组" in w for w in warns), str(warns))

    # ---------------- M6 replaces 合法 ----------------
    print("\n[M6] replaces 指向存在旧 id → 被取代，不重复保留")
    items, warns = run({"items": [
        {"type": "FACT", "text": "用户养了一盆茉莉，很在意它。",
         "source_event_ids": ["e1"], "replaces": ["mem_old1"]}]})
    ids = {i.id for i in items}
    check("mem_old1 已被取代（不在）", "mem_old1" not in ids)
    check("新文本在", any("很在意" in i.text for i in items))
    check("另两条仍保留", {"mem_old2", "mem_old3"} <= ids, str(sorted(ids)))

    # ---------------- M7 replaces 非法 ----------------
    print("\n[M7] replaces 指向不存在 → 忽略 + warning")
    items, warns = run({"items": [
        {"type": "FACT", "text": "新内容。", "source_event_ids": [],
         "replaces": ["mem_NOPE"]}]})
    ids = {i.id for i in items}
    check("旧条目未被误删", {"mem_old1", "mem_old2", "mem_old3"} <= ids)
    check("有 warning", any("replaces" in w for w in warns), str(warns))

    # ---------------- M8 虚构来源 ----------------
    print("\n[M8] 虚构 source_event_ids → 剔除")
    items, warns = parse_items({"items": [
        {"type": "FACT", "text": "虚构来源。", "source_event_ids": ["e1", "raw_event_FAKE"]}]},
        KNOWN, "v999")
    check("保留真实来源", items[0].source_event_ids == ["e1"], str(items[0].source_event_ids))
    check("有『不存在』warning", any("不存在" in w for w in warns), str(warns))

    # ---------------- M9 来源字段缺失 ----------------
    print("\n[M9] 来源字段缺失 → warning（Phase 11-3 修复）")
    items, warns = parse_items({"items": [{"type": "FACT", "text": "没写来源。"}]}, KNOWN, "v999")
    check("来源为空", items[0].source_event_ids == [])
    check("有『没有输出』warning", any("没有输出" in w for w in warns), str(warns))

    # ---------------- M10 重复来源 ----------------
    print("\n[M10] 重复来源 → 保序去重")
    items, _ = parse_items({"items": [
        {"type": "FACT", "text": "重复。", "source_event_ids": ["e3", "e1", "e3"]}]},
        KNOWN, "v999")
    check("去重且保序", items[0].source_event_ids == ["e3", "e1"],
          str(items[0].source_event_ids))

    # ---------------- M11 空 items 无 deletes ----------------
    print("\n[M11] ★空 items + 无 deletes → 旧条目全保留（不是清空）")
    items, warns = run({"items": []})
    ids = {i.id for i in items}
    check("★三条旧条目全在（防止整库清空）",
          {"mem_old1", "mem_old2", "mem_old3"} <= ids, str(sorted(ids)))
    check("有『未提及』warning", any("未提及" in w for w in warns))

    # ---------------- M12 第一版 ----------------
    print("\n[M12] 第一版（无旧条目）→ 正常产出")
    items, warns = merge_items({"items": [
        {"type": "FACT", "text": "首版条目。", "source_event_ids": ["e1"]}]},
        KNOWN, "v001", None)
    check("产出 1 条", len(items) == 1, str(len(items)))
    check("无『未提及』warning", not any("未提及" in w for w in warns), str(warns))

    # ---------------- M13 build_user_message 含旧条目 ----------------
    print("\n[M13] build_user_message 真的把旧条目列进输入")
    msg = build_user_message([], [], mk_prev())
    check("含『现有记忆』段", "现有记忆" in msg)
    check("含旧 id", "mem_old1" in msg and "mem_old3" in msg)
    check("含 replaces 说明", "replaces" in msg)
    check("含 deletes 说明", "deletes" in msg)
    msg0 = build_user_message([], [], None)
    check("无旧条目时不列该段", "第一版" in msg0, "")

    # ---------------- M14 保留条目版本标记 ----------------
    print("\n[M14] 保留的旧条目不改写 first_seen_version")
    items, _ = run({"items": []})
    kept1 = next(i for i in items if i.id == "mem_old1")
    check("first_seen_version 仍为 v001", kept1.first_seen_version == "v001",
          str(kept1.first_seen_version))

    # ---------------- M15 拒绝覆盖 ----------------
    print("\n[M15] save_version 拒绝覆盖")
    mv = memory_mod.MemoryVersion(version="v001", created_at="2026-01-01T00:00:00+08:00",
                                  model="m", items=[], source_range={}, note="t")
    memory_mod.save_version(mv)
    try:
        memory_mod.save_version(mv)
        check("第二次被拒", False, "竟然覆盖了")
    except Exception as e:
        check("第二次被拒", "拒绝覆盖" in str(e) or "已存在" in str(e), type(e).__name__)

    # ---------------- M16 异常 payload ----------------
    print("\n[M16] items 不是数组 → 抛 ConsolidateError")
    try:
        parse_items({"items": "nope"}, KNOWN, "v999")
        check("抛错", False, "没抛")
    except ConsolidateError:
        check("抛 ConsolidateError", True)

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