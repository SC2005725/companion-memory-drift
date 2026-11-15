"""core 链 → 主链 同步桥（处方 A 收口时发现的缺口，2026-11-08）。

## 缺口（audit 实锤）

她的聊天检索只读**主链** `memory/versions`（consolidate 写，v014/116 条），
而整理员/候选链 `core/memory-heci`（muse 提案、经历提炼 EPISODE 写）**从未
被桥接**——muse 的纪要、今晚的 4 段经历，她自己在对话里检索不到。
记忆在账上、不在脑子里——这正是这套系统最忌讳的「报告写了已完成、实际没通」。

## 做什么

把 core 链最新快照里「主链还没有的条目」按相似度去重后追加成**一个**新的
主链版本（快照式：此前条目全量保留 + 新增条目）。幂等：重跑零新增、零新版本。

## 诚实边界

- 同步条目 `model="chain-sync（程序拼装，无模型）"`、`prompt_sha256=None`、
  note 注明来源——与整理产物在版本链上**可区分**，不冒充整理产出。
- 相似度 ≥0.72 视为已存在（与 core_candidates 的重复判定同阈值）；
  已添加的条目参与后续比对（同批内近重复也拦）。
- 只追加不删除：core 链是证据链，主链只进不退。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import core_candidates
import memory as memory_mod
import storage

SIM_DUP = 0.72          # 与 core_candidates 重复判定同阈值


def _stable_id(text: str) -> str:
    """同步条目的稳定 id（按文本内容派生，主链 id 同款 mem_ 前缀）。"""
    return "mem_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def sync_core_to_main() -> dict:
    """core 链最新快照 → 主链追加版本。返回统计，幂等可反复跑。"""
    core_n, core_items = core_candidates._latest_version()
    if not core_items:
        return {"core_version": None, "added": 0, "version": None}

    mv = memory_mod.load_latest()
    known: list[str] = [it.text for it in (mv.items if mv else [])]

    added: list[memory_mod.MemoryItem] = []
    skipped_dup = 0
    skipped_empty = 0
    for raw in core_items:
        text = str(raw.get("text") or "").strip()
        if not text:
            skipped_empty += 1
            continue
        best = max((core_candidates.similarity(text, t) for t in known),
                   default=0.0)
        if best >= SIM_DUP:
            skipped_dup += 1
            continue
        known.append(text)      # 同批内近重复也拦
        added.append(memory_mod.MemoryItem(
            id=_stable_id(text),
            type=str(raw.get("type") or "FACT"),
            text=text,
            source_event_ids=[str(x) for x in (raw.get("source_event_ids") or [])],
            confidence=raw.get("confidence"),
            category=str(raw.get("category") or "") or None,
            tier=str(raw.get("tier") or "") or memory_mod.TIER_EXPERIENCE,
            salience=raw.get("salience") or 0.6,
        ))

    if not added:
        return {"core_version": f"v{core_n:03d}", "added": 0,
                "version": None, "skipped_dup": skipped_dup}

    version = memory_mod.next_version_name()
    now = datetime.now().astimezone().isoformat()
    for it in added:
        it.first_seen_version = version
        it.last_modified_version = version
        it.created_at = now
        it.updated_at = now

    from memory import MemoryVersion
    new_mv = MemoryVersion(
        version=version,
        created_at=now,
        model="chain-sync（程序拼装，无模型）",
        items=list(mv.items) + added if mv else list(added),
        previous_version=(mv.version if mv else None),
        prompt_sha256=None,   # 同步不经整理提示词——诚实留空，不冒充整理产出
        note=(f"core 链同步：新增 {len(added)} 条（相似度阈值 {SIM_DUP}，"
              f"来自 core/memory-heci v{core_n:03d}；程序拼装，未经整理提示词）"),
    )
    saved = memory_mod.save_version(new_mv)

    # 主链的 DB 登记与 consolidate 同口径（verify 的第三方见证要账实一致）
    sc = storage.connect()
    try:
        storage.record_memory_version(
            sc,
            version=version,
            model=new_mv.model,
            item_count=len(new_mv.items),
            file=saved["file"],
            file_sha256=saved["sha256"],
            previous_version=new_mv.previous_version,
            note=new_mv.note,
        )
        sc.commit()
    finally:
        sc.close()

    return {"core_version": f"v{core_n:03d}", "added": len(added),
            "version": version, "skipped_dup": skipped_dup,
            "file": saved["file"], "sha256": saved["sha256"]}
