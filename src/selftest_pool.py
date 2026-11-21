"""任务池自测（2026-11-21，Muse 设计 §八验收 6 条 + 防御性补充）。

运行：python -X utf8 src\\selftest_pool.py
全部离线（隔离临时库，零模型调用、零费用、不碰生产库）。
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def main() -> int:
    print("=" * 72)
    print("任务池 自测（验收标准 §八 6 条 + 防御性补充）")
    print("=" * 72)

    old_db = Path(__file__).resolve().parent.parent / "core" / "companion_core.db"
    old_mtime = old_db.stat().st_mtime if old_db.exists() else None

    tmp = Path(tempfile.mkdtemp(prefix="pool_selftest_")) / "core.db"
    conn = core.connect(tmp)
    core.init_schema(conn)
    core.ensure_subject(conn, "heci_001", "鹤辞")
    core.register_instance(conn, "heci_001", "dsh")
    core.register_instance(conn, "heci_001", "zcode")
    core.register_instance(conn, "heci_001", "muse")
    DSH, ZC, MUSE = "heci_001_dsh", "heci_001_zcode", "heci_001_muse"

    # ── A1 抢认：两席位同时抢，只有一个成功，另一个 409 ────────────────
    print("\n[A1] 原子抢认（验收 #1）")
    tid1, created = core.create_task(
        conn, from_instance=DSH, to_instance="pool",
        title="池单：抢认测试", risk="low")
    check("建池单成功", created and tid1.startswith("task_"), tid1)
    r1 = core.claim_task(conn, tid1, instance_id=ZC)
    check("第一个认领成功 → RECEIVED", r1["status"] == "RECEIVED"
          and r1["claimed_by"] == ZC, str(r1))
    try:
        core.claim_task(conn, tid1, instance_id=MUSE)
        check("第二个认领必须冲突", False, "没有抛异常")
    except core.TaskClaimConflict as exc:
        check("第二个认领 409（TaskClaimConflict）", exc.claimed_by == ZC,
              str(exc))
    except ValueError as exc:
        check("第二个认领必须 409 而不是 400", False, str(exc))
    row = conn.execute("SELECT claim_count, to_instance, claimed_by FROM tasks "
                       "WHERE task_id=?", (tid1,)).fetchone()
    check("claim_count=1 且归属唯一", row["claim_count"] == 1
          and row["to_instance"] == ZC and row["claimed_by"] == ZC,
          str(dict(row)))

    # ── A5 释放回池：换人接（验收 #5）─────────────────────────────────
    print("\n[A5] 认领→释放→换人接（验收 #5）")
    try:
        core.release_task(conn, tid1, instance_id=MUSE, reason="不是认领者")
        check("非认领者释放必须被拒", False, "没有抛异常")
    except ValueError:
        check("非认领者释放必须被拒", True)
    r5 = core.release_task(conn, tid1, instance_id=ZC, reason="临时做不了，换人")
    check("认领者释放回池 → SENT", r5["status"] == "SENT"
          and r5["release_count"] == 1, str(r5))
    row = conn.execute("SELECT to_instance, claimed_by, release_reason, "
                       "release_count FROM tasks WHERE task_id=?",
                       (tid1,)).fetchone()
    check("释放后回池字段正确", row["to_instance"] == "pool"
          and row["claimed_by"] is None
          and "换人" in (row["release_reason"] or ""), str(dict(row)))
    r5b = core.claim_task(conn, tid1, instance_id=MUSE)
    check("另一席位接走", r5b["claimed_by"] == MUSE, str(r5b))
    ev = conn.execute("SELECT action, note FROM task_events WHERE task_id=? "
                      "ORDER BY seq", (tid1,)).fetchall()
    acts = [r["action"] for r in ev]
    check("任务事件留痕 create_pool/claim/release/claim",
          acts == ["create_pool", "claim", "release", "claim"], str(acts))
    se = conn.execute("SELECT COUNT(*) FROM subject_events WHERE task_id=? "
                      "AND event_type LIKE 'pool_task_%'",
                      (tid1,)).fetchone()[0]
    check("主体事件流同步 4 条（建/认/释/认）", se == 4, str(se))

    # ── A3 无人在线 → 上线后可见可认领（验收 #3）──────────────────────
    print("\n[A3] 上线后可见可认领（验收 #3）")
    tid3, _ = core.create_task(conn, from_instance=DSH, to_instance="pool",
                               title="池单：上线后接", risk="low")
    pool = core.pool_list(conn)
    check("公共栏可见", any(t["task_id"] == tid3 for t in pool), str(len(pool)))
    core.touch_instance(conn, MUSE)
    r3 = core.claim_task(conn, tid3, instance_id=MUSE)
    check("上线后认领成功", r3["status"] == "RECEIVED", str(r3))

    # ── A2 指定单转池：目标超时没 accept → 自动进池（验收 #2）─────────
    print("\n[A2] 指定单超时转池（验收 #2）")
    tid2, _ = core.create_task(
        conn, from_instance=DSH, to_instance=MUSE,
        title="指定单：带转池",
        # 刻意不标 risk：fail-closed 口径下巡检只代签收不接单——
        # 「没人接」才是转池要救的状态。risk=low 会被自动接单，
        # 那种单本来就不该转池。
        fallback_to_pool=True, fallback_after_seconds=1)
    core.task_patrol(conn)   # 模拟 60s 巡检代签收（receive，知悉登记）
    row = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                       (tid2,)).fetchone()
    check("巡检代签收后停 RECEIVED（模拟真实）", row["status"] == "RECEIVED",
          row["status"])
    time.sleep(1.1)
    sw = core.pool_sweep(conn)
    check("sweep 触发转池", tid2 in sw["fallback_to_pool"], str(sw))
    row = conn.execute("SELECT status, to_instance, claimed_by FROM tasks "
                       "WHERE task_id=?", (tid2,)).fetchone()
    check("转池后回 SENT/pool", row["status"] == "SENT"
          and row["to_instance"] == "pool", str(dict(row)))
    r2 = core.claim_task(conn, tid2, instance_id=ZC)
    check("在线席位从池里接走", r2["claimed_by"] == ZC, str(r2))
    try:
        core.create_task(conn, from_instance=DSH, to_instance="pool",
                         title="池单带fallback应被拒", risk="low",
                         fallback_to_pool=True)
        check("池单带 fallback_to_pool 必须被拒", False, "没有抛异常")
    except ValueError:
        check("池单带 fallback_to_pool 必须被拒", True)

    # ── A4 超时 → 老板选项事件 → 指派（验收 #4）───────────────────────
    print("\n[A4] 超时事件 + 老板指派（验收 #4）")
    tid4, _ = core.create_task(
        conn, from_instance=DSH, to_instance="pool",
        title="池单：超时待选派", risk="low", claim_deadline_seconds=1)
    time.sleep(1.1)
    sw4 = core.pool_sweep(conn)
    check("sweep 发超时事件", tid4 in sw4["timeout_notified"], str(sw4))
    row = conn.execute("SELECT pool_timeout_notified FROM tasks WHERE task_id=?",
                       (tid4,)).fetchone()
    check("通知位 once-only", row["pool_timeout_notified"] == 1)
    sw4b = core.pool_sweep(conn)
    check("第二次 sweep 不重复通知", tid4 not in sw4b["timeout_notified"], str(sw4b))
    se4 = conn.execute("SELECT event_content FROM subject_events WHERE task_id=? "
                       "AND event_type='pool_task_timeout'",
                       (tid4,)).fetchone()
    check("超时事件带席位选项清单",
          se4 is not None and "heci_001_zcode" in se4["event_content"]
          and "指派" in se4["event_content"],
          (se4["event_content"][:120] if se4 else "无"))
    r4 = core.assign_task(conn, tid4, to_instance=MUSE, actor_instance=DSH)
    check("老板点选指派成功", r4["to_instance"] == MUSE, str(r4))
    row = conn.execute("SELECT to_instance, status, claimed_by FROM tasks "
                       "WHERE task_id=?", (tid4,)).fetchone()
    check("指派后为指定单（未认领）", row["to_instance"] == MUSE
          and row["status"] == "SENT" and row["claimed_by"] is None,
          str(dict(row)))
    ra4 = core.task_action(conn, tid4, actor_instance=MUSE, action="receive")
    check("指派后走既有 directed 流程", ra4["status"] == "RECEIVED", str(ra4))

    # ── A6 回归：老指定单流程不受影响（验收 #6）───────────────────────
    print("\n[A6] 回归：老指定单 receive→accept→result（验收 #6）")
    tid6, created6 = core.create_task(conn, from_instance=DSH, to_instance=ZC,
                                      title="指定单：老流程回归", risk="low")
    check("指定单照常建单", created6, tid6)
    core.task_action(conn, tid6, actor_instance=ZC, action="receive")
    core.task_action(conn, tid6, actor_instance=ZC, action="accept")
    r6 = core.task_action(conn, tid6, actor_instance=ZC, action="result",
                          result="老流程照常")
    check("老流程三步全过 → COMPLETED", r6["status"] == "COMPLETED", str(r6))
    try:
        core.claim_task(conn, tid6, instance_id=MUSE)
        check("非池任务认领必须被拒（400 语义）", False, "没有抛异常")
    except ValueError:
        check("非池任务认领必须被拒（400 语义）", True)

    # ── A7 防御：巡检不许碰池单 / rearm / cancel ─────────────────────
    print("\n[A7] 防御补充")
    tid7, _ = core.create_task(conn, from_instance=DSH, to_instance="pool",
                               title="池单：巡检不许动", risk="low")
    core.task_patrol(conn)
    row = conn.execute("SELECT status, to_instance FROM tasks WHERE task_id=?",
                       (tid7,)).fetchone()
    check("task_patrol 不自动签收池单", row["status"] == "SENT"
          and row["to_instance"] == "pool", str(dict(row)))
    rr = core.pool_rearm(conn, tid7, extra_seconds=600, actor_instance=DSH)
    check("rearm 顺延并重新武装", rr["pool_deadline_at"] is not None, str(rr))
    core.task_action(conn, tid7, actor_instance=DSH, action="cancel",
                     note="老板选取消")
    row = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                       (tid7,)).fetchone()
    check("发起方取消池单 → CANCELLED", row["status"] == "CANCELLED",
          row["status"])

    # ── 收尾 ─────────────────────────────────────────────────────────
    conn.close()
    if old_mtime is not None:
        check("生产核心库 mtime 未变（零污染）",
              old_db.stat().st_mtime == old_mtime, "")

    print("\n" + "=" * 72)
    ok = sum(1 for _, g, _ in CHECKS if g)
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail and not good else ""))
    print("=" * 72)
    print(f"共 {len(CHECKS)} 项，通过 {ok} 项，失败 {len(CHECKS) - ok} 项。")
    print("说明：全程隔离临时库，零模型调用、零费用。")
    return 0 if ok == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
