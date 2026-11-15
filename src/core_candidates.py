"""P1-1：记忆候选自动消费。

## 规则（照任务的九条要求）

1. 自动读 pending 候选
2. 判断值不值得留（和现有条目比相似度）
3. 高度相似 → 合并，不新增重复条目
4. 中等相似 → **暂缓**（保留来源，不擅自覆盖原记忆）
5. 支持接纳 / 合并 / 拒绝 / 暂缓
6. 每个候选的处理结果都留记录（`candidate_decisions` 表，只追加）
7. 处理失败的候选**不改状态**，下次还能重试
8. 已处理过的候选跳过（按 decision 记录判断，天然幂等）
9. 处理完更新候选状态

## 三层分离

候选 → 这里是「筛选合并」→ 生成新的记忆版本（只追加，不覆盖旧版本）。
**不改 Memory V1/V1.1 的机制**，也不改小满的记忆。

用法：
    python src\\core_candidates.py            # 处理一批
    python src\\core_candidates.py --dry-run  # 只看会怎么判
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from pathlib import Path

import companion_core as core


def _setup_stdout() -> None:
    """只在当脚本跑时改 stdout；被 import 时不动（否则会重复包装）。"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = core.CORE_DIR / "memory-heci"

# 相似度阈值
MERGE_AT = 0.90      # ≥ 视为同一条 → 合并
DEFER_AT = 0.55      # 介于两者之间 → 暂缓，留给人看（宁保守，不擅自覆盖）

# 否定表述：和已有条目扯上关系却带否定 → 判为「疑似冲突」，不自动接纳
_NEGATIONS = ("没有", "并非", "不是", "不存在", "未建立", "未发生", "不会", "不能", "否认")


# ---------------------------------------------------------------------------
# 相似度：字符 2-gram Jaccard，够用且零依赖
# ---------------------------------------------------------------------------

def _bigrams(text: str) -> set[str]:
    t = "".join((text or "").split()).lower()
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# 记忆版本读写（只追加）
# ---------------------------------------------------------------------------

def _latest_version() -> tuple[int, list[dict]]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    nums = []
    for f in MEMORY_DIR.glob("v*.json"):
        if f.stem[1:].isdigit():
            nums.append(int(f.stem[1:]))
    if not nums:
        return 0, []
    n = max(nums)
    data = json.loads((MEMORY_DIR / f"v{n:03d}.json").read_text(encoding="utf-8"))
    return n, data.get("items") or []


def _write_version(items: list[dict], note: str) -> str:
    n, _ = _latest_version()
    nxt = n + 1
    name = f"v{nxt:03d}"
    payload = {
        "version": name,
        "created_at": core._now(),
        "note": note,
        "previous_version": f"v{n:03d}" if n else None,
        "items": items,
    }
    (MEMORY_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# 决策记录
# ---------------------------------------------------------------------------

def _processed(conn) -> set[str]:
    return {r["candidate_id"] for r in
            conn.execute("SELECT candidate_id FROM candidate_decisions").fetchall()}


def _record(conn, candidate_id: str, decision: str, note: str,
            matched: str | None = None, version: str | None = None) -> None:
    conn.execute(
        "INSERT INTO candidate_decisions (decision_id, candidate_id, at, decision, "
        "note, matched, memory_version) VALUES (?,?,?,?,?,?,?)",
        (f"dec_{uuid.uuid4().hex[:10]}", candidate_id, core._now(),
         decision, note[:500], matched, version))
    # 雷 9 补：note 一并回写候选行 —— 分配明细（接纳/合并/待确认各几条）
    # 在候选列表 API 里直接可见，不用去翻决策流水。
    conn.execute("UPDATE memory_candidates SET status=?, note=? WHERE candidate_id=?",
                 (decision, note[:500], candidate_id))
    conn.commit()


def run(*, limit: int = 50, dry_run: bool = False) -> dict:
    conn = core.connect()
    core.init_schema(conn)
    done = _processed(conn)

    rows = conn.execute(
        "SELECT * FROM memory_candidates WHERE status='pending' "
        "ORDER BY created_at ASC LIMIT ?", (limit,)).fetchall()

    _, existing = _latest_version()
    working = list(existing)          # 本批次累积，用于批内去重
    accepted_now: list[dict] = []
    stats = {"scan": len(rows), "accepted": 0, "merged": 0,
             "deferred": 0, "rejected": 0, "skipped": 0, "errors": 0}
    log: list[dict] = []
    # 决策先攒着，等记忆版本写成功再落库。
    # （原实现先落决策再写版本：中间崩溃就会出现「决策说接纳了、记忆却没写」）
    decisions: list[dict] = []

    for row in rows:
        cid = row["candidate_id"]
        if cid in done:
            stats["skipped"] += 1
            continue
        try:
            items = json.loads(row["items"])
        except (TypeError, json.JSONDecodeError):
            # 畸形候选只记错，保持 pending 让人能修了重跑（原来会静默消失）
            stats["errors"] += 1
            log.append({"candidate": cid, "decision": "error", "sim": 0.0,
                        "text": "", "note": "items 不是合法 JSON，保持 pending 待重跑"})
            continue
        if not isinstance(items, list):
            stats["errors"] += 1
            log.append({"candidate": cid, "decision": "error", "sim": 0.0,
                        "text": "", "note": "items 不是数组，保持 pending 待重跑"})
            continue

        # 逐条判定（原实现只评第一条、其余静默丢弃 —— Muse 审查报出）
        per: list[dict] = []
        matched_text: str | None = None

        for it in items:
            if not isinstance(it, dict):
                per.append({"decision": "rejected", "note": "条目不是对象",
                            "sim": 0.0, "text": ""})
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                per.append({"decision": "rejected", "note": "条目没有 text",
                            "sim": 0.0, "text": ""})
                continue

            best, best_idx = 0.0, -1
            for i, old in enumerate(working):
                s = similarity(text, str(old.get("text") or ""))
                if s > best:
                    best, best_idx = s, i
            if best_idx >= 0:
                matched_text = str(working[best_idx].get("text"))[:200]
            negated = any(n in text for n in _NEGATIONS)

            # 否定检测必须在合并判定**之前**：
            # 否则长句里插一个「不」，相似度还有 0.92，就会被当成重复合并掉。
            if negated:
                per.append({"decision": "deferred", "sim": best, "text": text,
                            "note": f"含否定表述（相似度 {best:.2f}），"
                                    f"可能改写已有事实，留给人确认"})
            elif best >= MERGE_AT:
                per.append({"decision": "merged", "sim": best, "text": text,
                            "note": f"与已有条目相似度 {best:.2f}，合并不新增"})
            elif best >= DEFER_AT:
                per.append({"decision": "deferred", "sim": best, "text": text,
                            "note": f"相似度 {best:.2f}，可能重复或冲突，留给人看"})
            else:
                per.append({"decision": "accepted", "sim": best, "text": text,
                            "note": f"新内容（最高相似度 {best:.2f}）",
                            "type": str(it.get("type") or "FACT")})

        # 雷 9（2026-11-07 深度审查）：决策按**条目**粒度落地。
        #
        # 旧版把整份候选坍缩成单一决策：只要有一条 deferred/merged，整份
        # 就按住不写 —— 同候选里被判明为「新内容」的事实跟着一起消失，
        # 而候选又标成 merged/deferred 不会再喂 → **新事实静默丢失（实测）**。
        #
        # 现在：
        #   · accepted 条目**当场进工作集**（带 candidate_id 溯源）；
        #   · 候选状态取「最需要人介入」的那个：有 deferred 仍标 deferred
        #     （人要看的信号不能丢），否则有 accepted 标 accepted，
        #     再否则 merged —— note 里带完整分配明细；
        #   · 若人工把 deferred 候选重置回 pending 重跑，旧条目会以相似度
        #     1.0 命中 MERGE_AT → 合并不重复，天然幂等。
        by_kind: dict[str, list[dict]] = {
            "accepted": [], "merged": [], "deferred": [], "rejected": []}
        for p in per:
            by_kind[p["decision"]].append(p)

        if not per or all(p["decision"] == "rejected" for p in per):
            decision = "rejected"
            note = f"没有可用条目（共 {len(per)} 条，全部无效）"
        else:
            parts: list[str] = []
            if by_kind["accepted"]:
                parts.append("接纳 " + "；".join(
                    p["text"][:40] for p in by_kind["accepted"]))
            if by_kind["merged"]:
                parts.append(f"合并 {len(by_kind['merged'])} 条"
                             "（与已有条目高度相似，不新增）")
            if by_kind["deferred"]:
                parts.append("待确认 " + "；".join(
                    p["note"][:80] for p in by_kind["deferred"]))
            if by_kind["rejected"]:
                parts.append(f"无效 {len(by_kind['rejected'])} 条")
            note = " ｜ ".join(parts)[:500]
            if by_kind["deferred"]:
                decision = "deferred"
            elif by_kind["accepted"]:
                decision = "accepted"
            elif by_kind["merged"]:
                decision = "merged"
            else:
                decision = "rejected"

        for p in by_kind["accepted"]:
            entry = {
                "type": p.get("type") or "FACT",
                "text": p["text"],
                "source": row["source_environment"],
                "instance_id": row["instance_id"],
                "candidate_id": cid,
                "accepted_at": core._now(),
            }
            working.append(entry)
            accepted_now.append(entry)
        stats["items_accepted"] = stats.get("items_accepted", 0) \
            + len(by_kind["accepted"])

        stats[decision] += 1
        log.append({"candidate": cid, "decision": decision,
                    "sim": round(max((p["sim"] for p in per), default=0.0), 3),
                    "text": (per[0]["text"] if per else "")[:50],
                    "note": note})
        decisions.append({"cid": cid, "decision": decision,
                          "note": note, "matched": matched_text})

    version = None
    if accepted_now and not dry_run:
        version = _write_version(
            working,
            f"候选自动消费：接纳 {len(accepted_now)} 条（来源 "
            f"{', '.join(sorted({e['source'] for e in accepted_now}))}）")

    # 阶段 3：记忆版本写成功了，才把决策落库改状态。
    if not dry_run:
        for d in decisions:
            _record(conn, d["cid"], d["decision"], d["note"],
                    matched=d["matched"], version=version)

    # HealthBoard（2026-11-08）：整理员自报健康——唯一写者=自己。
    try:
        _st = "degraded" if stats.get("errors") else "ok"
        core.set_component_health(
            conn, "consolidator", _st,
            f"scan={stats.get('scan')} accepted={stats.get('accepted')} "
            f"deferred={stats.get('deferred')} errors={stats.get('errors')}")
    except Exception:  # noqa: BLE001 — 健康记录失败不影响整理
        pass

    conn.close()
    return {"stats": stats, "new_version": version, "log": log, "dry_run": dry_run}


def main(argv=None) -> int:
    _setup_stdout()
    ap = argparse.ArgumentParser(description="记忆候选自动消费")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)
    out = run(limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())