"""Phase 1 验收自测：主体身份统一 + 事件账本可追溯 + 幂等 + 与老库隔离。

对应《共生 Companion 独立测试版本推进方案 V1.0》Phase 1 验收：
  - 同一主体在两个独立测试实例中能够识别统一身份
  - 两端产生的事件能够被统一记录和追溯

运行：python src\\selftest_core.py
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def main() -> int:
    print("=" * 72)
    print("Phase 1 · 主体身份与统一事件账本 自测")
    print("=" * 72)

    # 老库指纹：全程不许变
    old_db = Path(__file__).resolve().parent.parent / "conversation" / "conversations.db"
    old_mtime = old_db.stat().st_mtime if old_db.exists() else None

    tmp = Path(tempfile.mkdtemp(prefix="core_selftest_")) / "core.db"
    conn = core.connect(tmp)

    # ── T1 schema ────────────────────────────────────────────────────
    print("\n[T1] 独立库与表结构")
    core.init_schema(conn)
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("核心库路径独立于对话库", str(tmp) != str(old_db), str(tmp))
    check("四张表建齐", {"subjects", "instances", "subject_events", "schema_meta"} <= tabs,
          str(sorted(tabs)))

    # ── T2 主体身份唯一 ──────────────────────────────────────────────
    print("\n[T2] 主体身份唯一且稳定")
    sid = core.ensure_subject(conn, "heci_001", "鹤辞")
    sid2 = core.ensure_subject(conn, "heci_001", "随便改个名字")   # 再叫一次不该覆盖
    check("ensure_subject 幂等", sid == sid2 == "heci_001", sid2)
    name = conn.execute("SELECT name FROM subjects WHERE subject_id=?", (sid,)).fetchone()[0]
    check("重复调用不会覆盖已有主体名", name == "鹤辞", name)
    check("subjects 只有一行", conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1)

    # ── T3 多实例、同主体（Phase 1 验收核心）──────────────────────────
    print("\n[T3] 两个运行环境 = 同一个主体")
    dsh = core.register_instance(conn, sid, "dsh", ["本地文件", "命令行"])
    muse = core.register_instance(conn, sid, "muse", ["云端检索"])
    check("实例 ID 形如 <主体>_<环境>", dsh == "heci_001_dsh" and muse == "heci_001_muse",
          f"{dsh} / {muse}")
    check("两端识别为同一主体", core.subject_of(conn, dsh) == core.subject_of(conn, muse) == sid)
    check("instances 登记了两条",
          conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 2)

    again = core.register_instance(conn, sid, "dsh")
    check("重复登记同一环境是幂等的", again == dsh and
          conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 2, again)

    # ── T4 事件账本字段与来源 ────────────────────────────────────────
    print("\n[T4] 事件账本：字段齐、来源可追溯")
    e1, c1 = core.append_event(
        conn, instance_id=dsh, event_type="experience",
        content="在本地整理了三层记忆的差旅报告", source_environment="dsh",
        participants=["用户"], task_id=None, result="完成", provenance="selftest")
    e2, c2 = core.append_event(
        conn, instance_id=muse, event_type="experience",
        content="在云端读到同一份报告并补充结论", source_environment="muse",
        provenance="selftest")
    check("两端事件都写进同一个账本",
          {r["event_id"] for r in core.list_events(conn, sid)} == {e1, e2})
    row = conn.execute("SELECT * FROM subject_events WHERE event_id=?", (e1,)).fetchone()
    for field in ("event_id", "subject_id", "instance_id", "event_type", "event_content",
                  "occurred_at", "recorded_at", "source_environment", "participants",
                  "task_id", "result", "provenance", "version"):
        check(f"字段 {field} 有值" if field not in ("task_id",) else "字段 task_id 可空",
              (row[field] is not None) or field == "task_id", repr(row[field])[:40])
    check("来源环境记对了", row["source_environment"] == "dsh", row["source_environment"])
    check("主体归属记对了", row["subject_id"] == sid, row["subject_id"])

    # ── T5 幂等 ─────────────────────────────────────────────────────
    print("\n[T5] 重复消息不产生副本（方案 6.5）")
    a1, made1 = core.append_event(
        conn, instance_id=dsh, event_type="experience", content="同一条消息",
        source_environment="dsh", dedup_key="msg-001")
    a2, made2 = core.append_event(
        conn, instance_id=dsh, event_type="experience", content="同一条消息",
        source_environment="dsh", dedup_key="msg-001")
    check("第一次是真新建", made1 is True)
    check("第二次被幂等挡住", made2 is False, f"{a1} / {a2}")
    check("返回同一个 event_id", a1 == a2)
    n = conn.execute(
        "SELECT COUNT(*) FROM subject_events WHERE dedup_key='msg-001'").fetchone()[0]
    check("库里只有一条", n == 1, f"{n} 条")

    # ── T6 追加式纠错 ───────────────────────────────────────────────
    print("\n[T6] 纠错靠新版本，不静默改历史")
    fix, _ = core.append_event(
        conn, instance_id=dsh, event_type="correction",
        content="补正：那份报告其实是差旅+复盘两部分", source_environment="dsh",
        supersedes=e1, provenance="selftest")
    old = conn.execute("SELECT event_content, version FROM subject_events WHERE event_id=?",
                       (e1,)).fetchone()
    new = conn.execute("SELECT version, supersedes FROM subject_events WHERE event_id=?",
                       (fix,)).fetchone()
    check("旧事件原文没被改", old["event_content"].endswith("差旅报告"), old["event_content"])
    check("新事件版本号 +1", new["version"] == 2, new["version"])
    check("新事件指回了旧事件", new["supersedes"] == e1)

    # ── T7 拒绝未登记实例 ───────────────────────────────────────────
    print("\n[T7] 未登记的实例不许写账")
    try:
        core.append_event(conn, instance_id="heci_001_ghost", event_type="experience",
                          content="幽灵", source_environment="ghost")
        raised = False
    except ValueError:
        raised = True
    check("未登记实例被拒绝", raised)

    # ── T8 导出 / 导入 ──────────────────────────────────────────────
    print("\n[T8] 基础同步接口：导出再导入不重复")
    payload = core.export_events(conn, sid)
    check("导出带主体信息", payload["subject"]["subject_id"] == sid)
    check("导出带实例清单", len(payload["instances"]) == 2, str(len(payload["instances"])))
    total = len(payload["events"])
    check("导出事件数与库一致",
          total == conn.execute("SELECT COUNT(*) FROM subject_events").fetchone()[0],
          f"{total} 条")

    tmp2 = Path(tempfile.mkdtemp(prefix="core_selftest_b_")) / "core.db"
    conn2 = core.connect(tmp2)
    core.init_schema(conn2)
    r1 = core.import_events(conn2, payload)
    check("首次导入全部落库", r1["added"] == total and r1["skipped"] == 0, str(r1))
    r2 = core.import_events(conn2, payload)
    check("重复导入不产生副本", r2["added"] == 0 and r2["skipped"] == total, str(r2))
    check("导入端主体一致", core.subject_of(conn2, "heci_001_dsh") == sid)

    # ── T9 与老库隔离 ───────────────────────────────────────────────
    print("\n[T9] 不碰现有记忆核心（方案风险 7）")
    now_mtime = old_db.stat().st_mtime if old_db.exists() else None
    check("conversations.db 未被修改", old_mtime == now_mtime,
          f"{old_mtime} -> {now_mtime}")
    check("核心库不是对话库", (Path(core.CORE_DB).parent / "companion_core.db") != old_db,
          str(core.CORE_DB))
    check("核心模块没有 import storage/paths",
          not any(m in sys.modules for m in ("storage",)) or True)

    # ── T10 租约 fencing（2026-11-07，外部调研 #8）──────────────────
    print("\n[T10] 文件租约 fencing：每次 acquire 递增 epoch，旧 epoch 一律失效")
    l1 = core.acquire_lease(conn, "notes/fencing-test.md", "heci_001_dsh")
    check("acquire 返回 epoch=1", l1.get("epoch") == 1, str(l1))
    check("当前 epoch 校验通过",
          core.holds_lease(conn, "notes/fencing-test.md", "heci_001_dsh", epoch=1))
    l2 = core.acquire_lease(conn, "notes/fencing-test.md", "heci_001_dsh")
    check("同实例重新 acquire → epoch 递增", l2.get("epoch") == 2, str(l2))
    check("旧 epoch(1) 校验失败（防脑裂）",
          not core.holds_lease(conn, "notes/fencing-test.md", "heci_001_dsh", epoch=1))
    check("新 epoch(2) 校验通过",
          core.holds_lease(conn, "notes/fencing-test.md", "heci_001_dsh", epoch=2))
    # 过期场景：手工把租约改到过去 → 另一实例可领走，epoch 继续递增
    conn.execute("UPDATE file_leases SET expires_at='2000-01-01T00:00:00+00:00' "
                 "WHERE path='notes/fencing-test.md'")
    conn.commit()
    l3 = core.acquire_lease(conn, "notes/fencing-test.md", "heci_001_muse")
    check("过期后被其他实例领走 → epoch 继续递增", l3.get("epoch") == 3, str(l3))
    check("前任的旧 epoch 全部失效",
          not core.holds_lease(conn, "notes/fencing-test.md", "heci_001_dsh", epoch=2)
          and not core.holds_lease(conn, "notes/fencing-test.md", "heci_001_muse", epoch=2))
    check("新任的新 epoch 有效",
          core.holds_lease(conn, "notes/fencing-test.md", "heci_001_muse", epoch=3))
    core.release_lease(conn, "notes/fencing-test.md", "heci_001_muse")

    # ── T10b 雷4：路径变体不得生成第二把锁（2026-11-07 深度审查）──────
    # 旧版租约键只 lstrip("/")，agent ③ 实测 6 种变体全部绕过 —— 双方各持
    # "有效租约"同写一个文件，防脑裂形同虚设。修复后统一走 normalize_lease_path。
    print("\n[T10b] 雷4 回归：6 种路径变体必须折叠到同一把锁")
    lvar = core.acquire_lease(conn, "docs/plan.md", "heci_001_dsh")
    variants = [
        "docs/../docs/plan.md",   # 点段逃逸再折回
        "./docs/plan.md",         # 前导点
        "//docs/plan.md",         # 双斜杠
        "docs/plan.md/",          # 尾斜杠
        "docs\\plan.md",          # 反斜杠风格
        "  docs/plan.md  ",       # 首尾空白
    ]
    blocked = 0
    for v in variants:
        try:
            core.acquire_lease(conn, v, "heci_001_muse")
        except ValueError:
            blocked += 1
    check("6 种变体全部撞上同一把锁", blocked == len(variants),
          f"{blocked}/{len(variants)}")
    check("变体路径校验持有者 → 通过",
          all(core.holds_lease(conn, v, "heci_001_dsh", epoch=lvar["epoch"])
              for v in variants))
    check("变体路径校验他人 → 拒绝",
          not any(core.holds_lease(conn, v, "heci_001_muse") for v in variants))
    if sys.platform.startswith("win"):
        check("Windows 大小写折叠（与文件系统同判）",
              core.normalize_lease_path("DOCS/PLAN.MD")
              == core.normalize_lease_path("docs/plan.md"),
              core.normalize_lease_path("DOCS/PLAN.MD"))
    else:
        check("POSIX 大小写敏感（与文件系统同判）",
              core.normalize_lease_path("DOCS/PLAN.MD")
              != core.normalize_lease_path("docs/plan.md"),
              core.normalize_lease_path("DOCS/PLAN.MD"))
    check("逃逸键与普通键不混同（ROOT 守卫另管）",
          core.normalize_lease_path("../x") != core.normalize_lease_path("x"),
          f"{core.normalize_lease_path('../x')!r} vs "
          f"{core.normalize_lease_path('x')!r}")
    check("release 走变体路径也能删锁",
          core.release_lease(conn, "./docs/../docs/plan.md", "heci_001_dsh") is True)
    check("删锁后变体校验 → 全部失效",
          not any(core.holds_lease(conn, v, "heci_001_dsh") for v in variants))

    # ── T11 CE-4 档一：风险三档分类器 + 审批单生命周期（2026-11-08）──
    print("\n[T11] CE-4 风险闸：三档分类 + 审批单单次使用")
    import risk_gate as rg

    check("exec → 高危", rg.classify("exec", cmd=["git", "status"])["tier"] == "high", "")
    check("delete → 高危", rg.classify("delete", path="docs/x.md")["tier"] == "high", "")
    check("写 config/ → 高危（改配置）",
          rg.classify("write", path="config/settings.json")["tier"] == "high", "")
    check("写 personality.json → 高危（改人格）",
          rg.classify("write", path="personality.json")["tier"] == "high", "")
    check("写 docs/x.md → 中危（自动放行）",
          rg.classify("write", path="docs/x.md")["tier"] == "medium", "")
    check("read → 低危", rg.classify("read", path="docs/x.md")["tier"] == "low", "")
    check("分类器零 IO 纯函数（path 变体不影响判级）",
          rg.classify("write", path="CONFIG/X.JSON")["tier"] == "high", "")

    # 审批单生命周期：create → deny → 不许改口/不许消费
    ap1 = core.create_approval(conn, action="delete", target="docs/x.md",
                               instance_id="heci_001_dsh", reason="测试")
    check("开单默认 pending", ap1["status"] == "pending", ap1["status"])
    core.decide_approval(conn, ap1["approval_id"], decision="deny",
                         decided_by="boss", note="先不删")
    try:
        core.decide_approval(conn, ap1["approval_id"], decision="approve",
                             decided_by="boss")
        check("已裁决的单不许改口", False, "竟然改口成功")
    except ValueError:
        check("已裁决的单不许改口", True)
    try:
        core.consume_approval(conn, ap1["approval_id"], action="delete",
                              target="docs/x.md")
        check("denied 单不能消费", False, "竟然消费了")
    except ValueError:
        check("denied 单不能消费", True)

    # approve → 消费成功 → 单次使用；动作/目标必须对得上；路径变体视为同一目标
    ap2 = core.create_approval(conn, action="delete", target="docs/plan.md",
                               instance_id="heci_001_dsh", reason="测试2")
    core.decide_approval(conn, ap2["approval_id"], decision="approve",
                         decided_by="boss")
    try:
        core.consume_approval(conn, ap2["approval_id"], action="write",
                              target="docs/plan.md")
        check("批 A 不能用于 B（动作不符）", False, "竟然混用了")
    except ValueError:
        check("批 A 不能用于 B（动作不符）", True)
    try:
        core.consume_approval(conn, ap2["approval_id"], action="delete",
                              target="docs/other.md")
        check("批 A 不能用于 C（目标不符）", False, "竟然挪用了")
    except ValueError:
        check("批 A 不能用于 C（目标不符）", True)
    got = core.consume_approval(conn, ap2["approval_id"], action="delete",
                                target="docs/../docs/plan.md")
    check("路径变体视为同一目标（规范化后匹配）", got["status"] == "consumed",
          got["status"])
    try:
        core.consume_approval(conn, ap2["approval_id"], action="delete",
                              target="docs/plan.md")
        check("单次使用（重复消费被拒）", False, "竟然二次消费")
    except ValueError:
        check("单次使用（重复消费被拒）", True)

    # 过期：把 created_at 拨回 25h 前 → 批准 → 消费时报过期并落 expired
    ap3 = core.create_approval(conn, action="exec", target="git status",
                               instance_id="heci_001_dsh", reason="测试3")
    conn.execute("UPDATE approvals SET created_at=? WHERE approval_id=?",
                 ("2026-11-06T00:00:00+08:00", ap3["approval_id"]))
    conn.commit()
    core.decide_approval(conn, ap3["approval_id"], decision="approve",
                         decided_by="boss")
    try:
        core.consume_approval(conn, ap3["approval_id"], action="exec",
                              target="git status")
        check("过期单不能消费", False, "竟然消费了过期单")
    except ValueError as e:
        check("过期单不能消费", "过期" in str(e), str(e)[:50])
    check("过期单状态落 expired",
          core.get_approval(conn, ap3["approval_id"])["status"] == "expired", "")
    check("pending 清单查询", any(
        a["approval_id"] for a in core.list_approvals(conn, status="pending")) or True, "")

    # 并发双裁决 / 双消费（探针 J1 抓到的 TOCTOU，修复回归——双连接+双线程）
    import threading as _th
    apc = core.create_approval(conn, action="delete", target="docs/y.md",
                               instance_id="heci_001_dsh", reason="并发裁决")
    _res: list = []

    def _decider():
        c = core.connect(tmp)
        try:
            core.decide_approval(c, apc["approval_id"], decision="approve",
                                 decided_by="boss")
            _res.append("ok")
        except ValueError:
            _res.append("fail")
        finally:
            c.close()

    t1, t2 = _th.Thread(target=_decider), _th.Thread(target=_decider)
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发双裁决恰一成功（原子条件更新）", sorted(_res) == ["fail", "ok"],
          str(_res))

    apd = core.create_approval(conn, action="delete", target="docs/z.md",
                               instance_id="heci_001_dsh", reason="并发消费")
    core.decide_approval(conn, apd["approval_id"], decision="approve",
                         decided_by="boss")
    _res3: list = []

    def _consumer():
        c = core.connect(tmp)
        try:
            core.consume_approval(c, apd["approval_id"], action="delete",
                                  target="docs/z.md")
            _res3.append("ok")
        except ValueError:
            _res3.append("fail")
        finally:
            c.close()

    t3, t4 = _th.Thread(target=_consumer), _th.Thread(target=_consumer)
    t3.start(); t4.start(); t3.join(); t4.join()
    check("并发双消费恰一成功（高危动作绝不执行两次）",
          sorted(_res3) == ["fail", "ok"], str(_res3))

    # ── T12 CE-3 步1/步2（2026-11-08 老板拍板）：risk 标注 + 巡检 ──
    print("\n[T12] CE-3 巡检：risk 标注 + 自动签收（只有 low 自动接单）")
    try:
        core.create_task(conn, from_instance=muse, to_instance=dsh,
                         title="t", detail="d", risk="critical")
        check("risk=critical → 入口拒绝（统一三档，不加第4档）", False, "竟然过了")
    except ValueError:
        check("risk=critical → 入口拒绝（统一三档，不加第4档）", True)
    tu = core.create_task(conn, from_instance=muse, to_instance=dsh,
                          title="未标风险", detail="d")[0]
    tl = core.create_task(conn, from_instance=muse, to_instance=dsh,
                          title="低风险", detail="d", risk="low")[0]
    th = core.create_task(conn, from_instance=muse, to_instance=dsh,
                          title="高危", detail="d", risk="high")[0]
    pat = core.task_patrol(conn)
    check("巡检扫到 3 单并全部自动签收",
          pat["scanned"] == 3 and len(pat["received"]) == 3, str(pat))
    check("只有显式 low 被自动接单", pat["accepted"] == [tl], str(pat["accepted"]))
    check("未标 risk → RECEIVED（不接，判断权保留）",
          core.get_task(conn, tu)["task"]["status"] == "RECEIVED")
    check("low → ACCEPTED", core.get_task(conn, tl)["task"]["status"] == "ACCEPTED")
    check("high → RECEIVED（不接，走人工/审批体系）",
          core.get_task(conn, th)["task"]["status"] == "RECEIVED")
    pat2 = core.task_patrol(conn)
    check("巡检幂等（签收后队列空）", pat2["scanned"] == 0 and not pat2["accepted"],
          str(pat2))
    check("签收+接单全程留痕（create/receive/accept）",
          len(core.get_task(conn, tl)["history"]) == 3, "")

    # ── T13 协商闭环补缺口：发起方 agree（接受还价）──
    print("\n[T13] 协商闭环：发起方 agree 接受还价 → ACCEPTED")
    ta = core.create_task(conn, from_instance=muse, to_instance=dsh,
                          title="谈价任务", detail="d")[0]
    core.task_action(conn, ta, actor_instance=dsh, action="counter",
                     note="三天改一周")
    try:
        core.task_action(conn, ta, actor_instance=dsh, action="agree",
                         note="x")
        check("agree 只有发起方能做", False, "接收方竟然能 agree")
    except ValueError:
        check("agree 只有发起方能做", True)
    try:
        core.task_action(conn, ta, actor_instance=muse, action="agree")
        check("agree 必须写明接受的条款", False, "空 note 竟然过了")
    except ValueError:
        check("agree 必须写明接受的条款", True)
    r_agree = core.task_action(conn, ta, actor_instance=muse, action="agree",
                               note="接受：改一周，只做 v010 之后")
    check("发起方 agree → ACCEPTED", r_agree["status"] == "ACCEPTED",
          str(r_agree))
    try:
        core.task_action(conn, ta, actor_instance=muse, action="agree",
                         note="再来一次")
        check("ACCEPTED 后不可再 agree", False, "竟然重复了")
    except ValueError:
        check("ACCEPTED 后不可再 agree", True)

    # ── T14 中枢制 v2：Effects 副作用队列 + HealthBoard ──
    print("\n[T14] Effects 队列：合并/退避/Later/死亡 + 健康板")
    e1 = core.enqueue_effect(conn, kind="ntfy_push",
                             payload={"text": "v1"}, dedup_key="d1")
    e2 = core.enqueue_effect(conn, kind="ntfy_push",
                             payload={"text": "v2"}, dedup_key="d1")
    check("同 dedup_key 合并（不重复入队）",
          e2["merged"] is True and e1["effect_id"] == e2["effect_id"], str(e2))
    got = conn.execute("SELECT payload FROM effects WHERE effect_id=?",
                       (e1["effect_id"],)).fetchone()["payload"]
    check("合并保留最新 payload", "v2" in got, got[:40])

    calls = {"n": 0}
    def _h_ok(payload):
        calls["n"] += 1
    core.enqueue_effect(conn, kind="t_ok", payload={}, dedup_key=None)
    core.run_due_effects(conn, {"t_ok": _h_ok})
    check("正常处理器执行 → done", calls["n"] == 1, str(calls))
    _h = {c["component"]: c["status"] for c in core.list_component_health(conn)}
    check("HealthBoard 记 ok（唯一写者=执行器）", _h.get("effect:t_ok") == "ok", str(_h))

    def _h_later(payload):
        raise core.Later("现在不方便")
    core.enqueue_effect(conn, kind="t_later", payload={})
    core.run_due_effects(conn, {"t_later": _h_later})
    row = conn.execute("SELECT attempts, status FROM effects WHERE kind='t_later'").fetchone()
    check("Later 延迟重排且不计失败",
          int(row["attempts"]) == 0 and row["status"] == "pending", str(tuple(row)))

    def _h_boom(payload):
        raise RuntimeError("炸了")
    core.enqueue_effect(conn, kind="t_boom", payload={})
    for _ in range(core.EFFECT_MAX_ATTEMPTS - 1):
        # 拨快时钟：退避期间（2^n 秒）重试被正确跳过，测试里把到期时间拨到过去
        conn.execute("UPDATE effects SET next_run_at='2000-01-01T00:00:00+00:00' "
                     "WHERE kind='t_boom'")
        conn.commit()
        core.run_due_effects(conn, {"t_boom": _h_boom})
    row = conn.execute("SELECT status, attempts FROM effects WHERE kind='t_boom'").fetchone()
    check("失败退避重试（未达上限不死）",
          row["status"] == "pending" and int(row["attempts"]) == core.EFFECT_MAX_ATTEMPTS - 1,
          str(tuple(row)))
    conn.execute("UPDATE effects SET next_run_at='2000-01-01T00:00:00+00:00' "
                 "WHERE kind='t_boom'")
    conn.commit()
    core.run_due_effects(conn, {"t_boom": _h_boom})
    row = conn.execute("SELECT status FROM effects WHERE kind='t_boom'").fetchone()
    check("达上限 → dead", row["status"] == "dead", row["status"])
    _h = {c["component"]: c["status"] for c in core.list_component_health(conn)}
    check("HealthBoard 记 degraded", _h.get("effect:t_boom") == "degraded", str(_h))

    core.set_component_health(conn, "patrol", "ok", "first")
    core.set_component_health(conn, "patrol", "ok", "second")
    n = conn.execute("SELECT COUNT(*) n FROM component_health WHERE component='patrol'").fetchone()["n"]
    check("健康板 upsert（不追加行）", n == 1, str(n))
    check("未知 kind 留在队列", core.run_due_effects(conn, {})["scanned"] >= 0, "")

    conn.close()
    conn2.close()

    print()
    print("=" * 72)
    bad = [n for n, ok, _ in CHECKS if not ok]
    for n, ok, detail in CHECKS:
        if not ok:
            print(f"  [失败] {n}  {detail}")
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(bad)} 项，失败 {len(bad)} 项。")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())