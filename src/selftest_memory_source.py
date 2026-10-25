"""Phase 11 第三步 · Memory 来源字段的针对性自测。

## 覆盖（用户批准的 4 种情况 + 回归）

  T1  字段**缺失**        → 有 warning，来源 = 空
  T2  字段**显式空数组**  → **无** warning，来源 = 空      ← 不能误报
  T3  正常来源列表        → 全保留，无 warning
  T4  重复 ID             → **去重且保持原顺序**

回归（确保既有语义没被改坏）：
  T5  引用不存在的 ID      → 剔除 + 有 warning
  T6  部分存在部分不存在    → 保留存在的，剔除不存在的
  T7  多来源正常          → 全保留、顺序不变
  T8  `stable_item_id` 对来源顺序不敏感
  T9  去重后 id 与「无重复来源」一致（这正是修 B 的目的）
  T10 `to_dict ↔ from_dict` 保留来源
  T11 `save_version` 拒绝覆盖
  T12 类型非法 → 降级 UNKNOWN（既有行为）
  T13 text 为空 → 跳过（既有行为）
  T14 **历史版本文件 sha256 未被改动**

完全离线，隔离在临时目录，不碰正式数据。

用法：
    python src\\selftest_memory_source.py
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_src_"))
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
(TEMP_ROOT / ".env").write_text("LLM_API_KEY=fake\nLLM_MODEL=m\n", encoding="utf-8")
shutil.copytree(REAL_ROOT / "config", TEMP_ROOT / "config", dirs_exist_ok=True)
(TEMP_ROOT / "personality").mkdir(parents=True, exist_ok=True)
(TEMP_ROOT / "personality" / "personality.json").write_text(
    json.dumps({"name": "小满", "description": "测试", "system_prompt": "你是小满。"},
               ensure_ascii=False), encoding="utf-8")

for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe", "relationship",
            "user_model", "events", "llm", "model_router", "archive_anchor", "heartbeat"):
    sys.modules.pop(mod, None)

import changelog as changelog_mod  # noqa: E402
import memory as memory_mod  # noqa: E402
from consolidate import parse_items  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
KNOWN = {"e1", "e2", "e3"}


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def run(payload):
    return parse_items(payload, KNOWN, "v999")


def main() -> int:
    print("=" * 74)
    print("Memory 来源字段 · 针对性自测")
    print("=" * 74)

    # ---------------- T1 字段缺失 ----------------
    print("\n[T1] 字段**缺失** → 应有 warning")
    items, warns = run({"items": [
        {"type": "FACT", "text": "只依据对话，模型没输出该字段。"}]})
    check("来源为空", items[0].source_event_ids == [], str(items[0].source_event_ids))
    has_w = any("没有输出" in w or "缺失" in w or "未提供" in w for w in warns)
    check("★发出了『字段缺失』warning", has_w, f"warnings={warns}")

    # ---------------- T2 显式空数组 ----------------
    print("\n[T2] 字段**显式空数组** → 不应有 warning（prompt 允许）")
    items, warns = run({"items": [
        {"type": "FACT", "text": "只依据对话，显式填空数组。", "source_event_ids": []}]})
    check("来源为空", items[0].source_event_ids == [], "")
    check("★没有 warning", warns == [], f"warnings={warns}")

    # ---------------- T3 正常来源列表 ----------------
    print("\n[T3] 正常来源列表 → 全保留、无 warning")
    items, warns = run({"items": [
        {"type": "FACT", "text": "正常来源。", "source_event_ids": ["e1", "e3"]}]})
    check("来源全保留", items[0].source_event_ids == ["e1", "e3"],
          str(items[0].source_event_ids))
    check("无 warning", warns == [], f"warnings={warns}")

    # ---------------- T4 重复 ID ----------------
    print("\n[T4] 重复 ID → 去重**且保持原顺序**")
    items, warns = run({"items": [
        {"type": "FACT", "text": "重复来源。",
         "source_event_ids": ["e3", "e1", "e3", "e2", "e1"]}]})
    check("★去重且顺序为首次出现顺序",
          items[0].source_event_ids == ["e3", "e1", "e2"],
          str(items[0].source_event_ids))

    # ---------------- T5 不存在的 ID ----------------
    print("\n[T5] 引用不存在的 ID → 剔除 + warning（既有行为）")
    items, warns = run({"items": [
        {"type": "FACT", "text": "混入未知 ID。",
         "source_event_ids": ["e1", "raw_event_NOTEXIST"]}]})
    check("保留存在的", items[0].source_event_ids == ["e1"], str(items[0].source_event_ids))
    check("有『不存在』warning", any("不存在" in w for w in warns), f"warnings={warns}")

    # ---------------- T6 部分存在部分不存在 ----------------
    print("\n[T6] 部分存在 / 部分不存在")
    items, warns = run({"items": [
        {"type": "FACT", "text": "混合。",
         "source_event_ids": ["nope1", "e2", "nope2", "e2"]}]})
    check("只留存在的且已去重", items[0].source_event_ids == ["e2"],
          str(items[0].source_event_ids))
    check("有 warning", any("不存在" in w for w in warns), "")

    # ---------------- T7 多来源顺序 ----------------
    print("\n[T7] 多来源顺序不变")
    items, _ = run({"items": [
        {"type": "FACT", "text": "顺序。", "source_event_ids": ["e3", "e1", "e2"]}]})
    check("顺序保持 e3,e1,e2", items[0].source_event_ids == ["e3", "e1", "e2"],
          str(items[0].source_event_ids))

    # ---------------- T8 id 对顺序不敏感 ----------------
    print("\n[T8] stable_item_id 对来源顺序不敏感（既有行为）")
    a = changelog_mod.stable_item_id("FACT", "同一句话", ["e1", "e2"])
    b = changelog_mod.stable_item_id("FACT", "同一句话", ["e2", "e1"])
    check("顺序不同 → id 相同", a == b, f"{a} vs {b}")

    # ---------------- T9 去重让 id 稳定（修 B 的目的）----------------
    print("\n[T9] ★去重后 id 与「无重复来源」一致（这正是去重的目的）")
    _, _ = run({"items": []})   # 预热
    it_dup, _ = run({"items": [
        {"type": "FACT", "text": "同一句话", "source_event_ids": ["e1", "e1", "e2"]}]})
    it_clean, _ = run({"items": [
        {"type": "FACT", "text": "同一句话", "source_event_ids": ["e1", "e2"]}]})
    check("带重复与不带重复 → 同一 id",
          it_dup[0].id == it_clean[0].id, f"{it_dup[0].id} vs {it_clean[0].id}")

    # ---------------- T10 序列化往返 ----------------
    print("\n[T10] to_dict ↔ from_dict 保留来源")
    src_item = memory_mod.MemoryItem(id="x", type="FACT", text="往返",
                                     source_event_ids=["e1", "e2"])
    back = memory_mod.MemoryItem.from_dict(src_item.to_dict())
    check("往返后来源一致", back.source_event_ids == ["e1", "e2"],
          str(back.source_event_ids))

    # ---------------- T11 拒绝覆盖 ----------------
    print("\n[T11] save_version 拒绝覆盖已存在版本")
    mv = memory_mod.MemoryVersion(version="v001", created_at="2026-01-01T00:00:00+08:00",
                                  model="m", items=[src_item], source_range={}, note="t")
    memory_mod.save_version(mv)
    try:
        memory_mod.save_version(mv)
        check("第二次保存被拒", False, "没有抛错")
    except Exception as exc:  # noqa: BLE001
        check("第二次保存被拒（既有行为）",
              "拒绝覆盖" in str(exc) or "已存在" in str(exc), f"{type(exc).__name__}")

    # ---------------- T12 非法类型降级 ----------------
    print("\n[T12] 非法 type → 降级 UNKNOWN（既有行为）")
    items, warns = run({"items": [
        {"type": "BOGUS", "text": "类型非法。", "source_event_ids": []}]})
    check("降级为 UNKNOWN", items[0].type == "UNKNOWN", items[0].type)
    check("有降级 warning", any("非法" in w for w in warns), f"warnings={warns}")

    # ---------------- T13 text 为空跳过 ----------------
    print("\n[T13] text 为空 → 跳过（既有行为）")
    items, warns = run({"items": [
        {"type": "FACT", "text": "   ", "source_event_ids": []},
        {"type": "FACT", "text": "有效。", "source_event_ids": ["e1"]}]})
    check("只留有效条目", len(items) == 1 and items[0].text == "有效。", str(len(items)))
    check("有跳过 warning", any("text 为空" in w for w in warns), f"warnings={warns}")

    # ---------------- T14 历史版本文件未被改 ----------------
    print("\n[T14] ★正式项目的历史版本文件未被改动（只读校验）")
    real_versions = sorted((REAL_ROOT / "memory" / "versions").glob("v*.json"))
    sidecars = sorted((REAL_ROOT / "memory" / "versions").glob("v*.sha256"))
    check("正式版本文件数 >= 4", len(real_versions) >= 4, str(len(real_versions)))
    ok = True
    for p in real_versions:
        side = p.with_suffix(".sha256")
        if side.exists():
            body = p.read_text(encoding="utf-8")
            # 落盘时是 body + "\n"，sha256 覆盖「不含末尾换行」的正文
            expect = hashlib.sha256(body.rstrip("\n").encode("utf-8")).hexdigest()
            got = side.read_text(encoding="utf-8").split()[0]
            if expect != got:
                ok = False
                print(f"    ❌ {p.name} sha256 不符")
    check("全部正式版本 sha256 与侧车一致（未被改动）", ok, "")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 74)
    failed = [n for n, g, _ in CHECKS if not g]
    for name, good, detail in CHECKS:
        if not good:
            print(f"  [失败] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 74)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：完全离线，未联网、未产生费用；正式数据只读。")
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
