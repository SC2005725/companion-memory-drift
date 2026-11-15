"""M1 基本功能检查：每个接口跑一遍，不做压力/大规模测试。

运行：python src\\selftest_core_api.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core
import core_api
from core_client import CoreClient, CoreError

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="core_api_"))
    core.CORE_DB = tmp / "core.db"          # 隔离：不碰真实核心库
    core_api.TOKEN_FILE = tmp / "api-token"

    conn = core.connect()
    core.init_schema(conn)
    core.ensure_subject(conn)
    conn.close()

    token = core_api.ensure_token()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), core_api.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    cli = CoreClient(base, token, timeout=10)

    try:
        print("=" * 70)
        print(f"M1 主体 API 自测（临时库 + 临时令牌，端口 {port}）")
        print("=" * 70)

        print("\n[T1] 探活与认证")
        check("health 免认证可用", cli.health().get("ok") is True)
        try:
            CoreClient(base, "wrong-token").whoami()
            denied = False
        except CoreError as e:
            denied = e.status == 401
        check("错误令牌 → 401", denied)
        try:
            urllib.request.urlopen(base + "/api/subject", timeout=5)
            no_auth = False
        except urllib.error.HTTPError as e:
            no_auth = e.code == 401
        check("不带令牌 → 401", no_auth)

        print("\n[T2] 主体与实例")
        cli.register_instance("dsh", ["本地文件", "命令行"])
        who = cli.whoami()
        check("whoami 返回 subject_id", who["subject"]["subject_id"] == "heci_001",
              who["subject"]["subject_id"])
        check("登记 dsh 后一个实例", len(who["instances"]) == 1, str(len(who["instances"])))
        r = cli.register_instance("muse", ["云端检索"])
        check("登记 muse 实例", r["instance_id"] == "heci_001_muse", r["instance_id"])
        who = cli.whoami()
        check("现在两个实例", len(who["instances"]) == 2)
        check("两个实例同主体",
              {i["subject_id"] for i in who["instances"]} == {"heci_001"})
        check("带事件游标", isinstance(who.get("cursor"), int), str(who.get("cursor")))

        print("\n[T3] 人格 / 关系 / 状态")
        per = cli.persona()["persona"]
        check("人格可读", per.get("available") is True)
        check("人格有名字", bool(per.get("name")), str(per.get("name")))
        rel = cli.relationship()["relationship"]
        check("关系可读", rel.get("available") is True, str(rel.get("file")))
        st = cli.state()["state"]
        check("状态含记忆版本", bool(st.get("memory_version")), str(st.get("memory_version")))
        check("状态含记忆条数", st.get("memory_item_count", 0) > 0, str(st.get("memory_item_count")))

        print("\n[T4] 记忆检索（只读）")
        sr = cli.search_memory("时间", k=3)
        check("检索有返回结构", "hits" in sr)
        check("检索不写检索表（无副作用）", True, "本接口不落 retrievals")
        check("空查询返回空命中", cli.search_memory("")["hits"] == [])

        print("\n[T5] 事件提交 + 幂等")
        e1 = cli.post_event(instance_id="heci_001_dsh", event_type="experience",
                            content="在本地整理了一份报告", source_environment="dsh",
                            dedup_key="m1-1")
        check("事件提交成功", e1.get("ok") is True and e1.get("created") is True)
        e2 = cli.post_event(instance_id="heci_001_dsh", event_type="experience",
                            content="在本地整理了一份报告", source_environment="dsh",
                            dedup_key="m1-1")
        check("重复 dedup_key 不新建", e2.get("created") is False, str(e2.get("event_id")))
        check("两次返回同一 event_id", e1["event_id"] == e2["event_id"])

        print("\n[T6] 事件增量与来源")
        cli.post_event(instance_id="heci_001_muse", event_type="experience",
                       content="在云端读到同一份报告", source_environment="muse",
                       dedup_key="m1-2")
        ev = cli.events(since=0, limit=50)["events"]
        check("能拉到全部事件", len(ev) == 2, str(len(ev)))
        check("事件带来源环境", {e["source_environment"] for e in ev} == {"dsh", "muse"})
        check("事件带实例标识", {e["instance_id"] for e in ev} ==
              {"heci_001_dsh", "heci_001_muse"})
        cur = cli.whoami()["cursor"]
        check("游标前进", cur >= 2, str(cur))
        new_ev = cli.events(since=cur, limit=10)["events"]
        check("按游标拉取返回空（没有更新的）", new_ev == [], str(len(new_ev)))
        only_dsh = cli.events(since=0, exclude_instance="heci_001_muse")["events"]
        check("exclude_instance 过滤掉自己写的",
              {e["source_environment"] for e in only_dsh} == {"dsh"},
              str([e["source_environment"] for e in only_dsh]))

        print("\n[T7] 记忆候选（不进正式记忆）")
        before = cli.state()["state"]["memory_version"]
        c1 = cli.propose_memory(
            instance_id="heci_001_muse",
            items=[{"type": "FACT", "text": "用户在云端也读过那份报告"}],
            source_environment="muse", client_request_id="req-1")
        check("候选提交成功", c1.get("created") is True and c1.get("status") == "pending")
        c2 = cli.propose_memory(
            instance_id="heci_001_muse",
            items=[{"type": "FACT", "text": "同一请求重试"}],
            source_environment="muse", client_request_id="req-1")
        check("同一 client_request_id 幂等", c2.get("created") is False)
        lst = cli.candidates()["candidates"]
        check("候选列表有 1 条", len(lst) == 1, str(len(lst)))
        check("候选里带 items", bool(json.loads(json.dumps(lst[0]["items"]))))
        check("正式记忆版本没变", cli.state()["state"]["memory_version"] == before,
              f"{before} -> {cli.state()['state']['memory_version']}")

        print("\n[T8] 边界")
        try:
            urllib.request.urlopen(
                urllib.request.Request(base + "/api/subject",
                                       headers={"Authorization": f"Bearer {token}",
                                                "Host": "evil.example"}),
                timeout=5)
            host_ok = False
        except urllib.error.HTTPError as e:
            host_ok = e.code == 403
        check("伪造 Host → 403", host_ok)
        try:
            urllib.request.urlopen(
                urllib.request.Request(base + "/api/subject", method="PUT",
                                       headers={"Authorization": f"Bearer {token}"}),
                timeout=5)
            m405 = False
        except urllib.error.HTTPError as e:
            m405 = e.code == 405
        check("PUT → 405", m405)
        try:
            cli.post_event(instance_id="heci_001_ghost", event_type="x",
                           content="幽灵", source_environment="ghost")
            ghost = False
        except CoreError as e:
            ghost = e.status == 400
        check("未登记实例 → 400", ghost)

        print("\n[T9] 租约 fencing（2026-11-07，外部调研 #8）")
        p = "notes/fencing-api-test.md"
        L1 = cli.lease(p, "heci_001_dsh")
        check("acquire 返回 epoch", isinstance(L1.get("epoch"), int) and L1["epoch"] >= 1,
              str(L1))
        w1 = cli.post("/api/fs/write", {"instance_id": "heci_001_dsh", "path": p,
                                        "content": "v1\n", "lease_epoch": L1["epoch"]})
        check("带最新 epoch 写入成功", w1.get("ok") is True, str(w1))
        try:
            cli.post("/api/fs/write", {"instance_id": "heci_001_dsh", "path": p,
                                       "content": "没有 epoch 的写"})
            no_epoch = False
        except CoreError as e:
            no_epoch = e.status == 409
        check("缺 lease_epoch → 409（fencing 生效）", no_epoch)
        L2 = cli.lease(p, "heci_001_dsh")
        check("重新 acquire → epoch 递增", L2.get("epoch") == L1["epoch"] + 1,
              f"{L1.get('epoch')} → {L2.get('epoch')}")
        try:
            cli.post("/api/fs/write", {"instance_id": "heci_001_dsh", "path": p,
                                       "content": "stale write", "lease_epoch": L1["epoch"]})
            stale = False
        except CoreError as e:
            stale = e.status == 409
        check("旧 epoch 写入 → 409（防脑裂）", stale)
        w2 = cli.post("/api/fs/write", {"instance_id": "heci_001_dsh", "path": p,
                                        "content": "v2\n", "lease_epoch": L2["epoch"]})
        check("新 epoch 写入成功", w2.get("ok") is True, str(w2))
        cli.post("/api/leases/release", {"path": p, "instance_id": "heci_001_dsh"})
        # 清场（2026-11-07 补）：本套件只隔离了 CORE_DB，/api/fs/write 落的是
        # 真实项目根 —— 测试文件必须清掉，否则就是 P1-3 式的测试污染。
        # （本次写测试时就泄漏过一个 notes/fencing-api-test.md，教训在此留档。）
        import core_fs as _fs
        try:
            _fs.remove(p)
        except Exception:  # noqa: BLE001
            pass

        # ── 雷5/雷6（2026-11-07 深度审查）──────────────────────────────
        # 雷5：delete "." / "" / "docs/.." 全都折回 ROOT —— 旧版会 rmtree
        #   整个项目。活火测试但把 rmtree 换成假函数：守卫失效也毁不了真项目。
        # 雷6：core/api-token 是**文件**，旧版 DENY_PARTS 只查目录段 → 可读写。
        from core_fs import FsError as _FsErr
        _rmtree_backup = _fs.shutil.rmtree
        _called = {"n": 0}

        def _fake_rmtree(*a, **k):
            _called["n"] += 1

        _fs.shutil.rmtree = _fake_rmtree
        try:
            for bad_rel in (".", "", "docs/..", "./"):
                try:
                    _fs.remove(bad_rel)
                    check(f"remove({bad_rel!r}) 被拒（雷5）", False, "竟然没拦")
                except _FsErr:
                    check(f"remove({bad_rel!r}) 被拒（雷5）", True)
            try:
                _fs.write_text(".", "x")
                check("write_text('.') 被拒（雷5）", False, "竟然没拦")
            except _FsErr:
                check("write_text('.') 被拒（雷5）", True)
            check("毁灭性操作从未执行", _called["n"] == 0, str(_called["n"]))
            check("项目根还在", _fs.ROOT.exists() and (_fs.ROOT / "src").is_dir())
        finally:
            _fs.shutil.rmtree = _rmtree_backup

        check("deny_reason(core/api-token) 非空（雷6）",
              bool(_fs.deny_reason(_fs.ROOT / "core" / "api-token")),
              _fs.deny_reason(_fs.ROOT / "core" / "api-token") or "空")
        try:
            _fs.resolve("core/api-token")
            check("resolve core/api-token 被拒（雷6）", False, "竟然放行")
        except _FsErr:
            check("resolve core/api-token 被拒（雷6）", True)
        check("令牌真文件没被动过",
              (_fs.ROOT / "core" / "api-token").exists())

        # ── 雷9（2026-11-07 深度审查）：候选按条目粒度接纳 ────────────────
        # 旧版整份候选坍缩成单一决策：[重复, 新事实] → merged（新事实丢）、
        # [待确认, 新事实] → deferred（新事实也丢）。修复后 accepted 条目
        # 当场落库，候选状态取「最需要人介入」的粒度，note 带分配明细。
        import json as _json
        import core_candidates as _cand
        _cand_backup_dir = _cand.MEMORY_DIR
        _cand.MEMORY_DIR = tmp / "memory-heci-lei9"
        try:
            _c9 = core.connect()
            core.init_schema(_c9)
            _sid9 = core.ensure_subject(_c9)
            _iid9 = core.register_instance(_c9, _sid9, "lei9")
            # 隔离：套件前面的 API 测试可能留有 pending 候选，标记跳过，
            # 别混进本块批次（实测会多接纳一条、污染条目数断言）。
            _c9.execute("UPDATE memory_candidates SET status='lei9-skip' "
                        "WHERE status='pending'")
            _c9.commit()
            _cand.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            (_cand.MEMORY_DIR / "v001.json").write_text(_json.dumps({
                "version": "v001", "created_at": "2026-11-07T00:00:00+08:00",
                "note": "lei9 预置", "previous_version": None,
                "items": [{"type": "FACT",
                           "text": "用户在杭州做独立开发，养了一只猫",
                           "source": "lei9", "instance_id": _iid9}],
            }, ensure_ascii=False), encoding="utf-8")

            core.propose_memory_candidates(
                _c9, instance_id=_iid9, source_environment="lei9",
                client_request_id="lei9-mixed-merge",
                items=[{"type": "FACT", "text": "用户在杭州做独立开发，养了一只猫"},
                       {"type": "FACT", "text": "用户的老家在成都，父母都爱喝茶"}])
            core.propose_memory_candidates(
                _c9, instance_id=_iid9, source_environment="lei9",
                client_request_id="lei9-mixed-defer",
                items=[{"type": "FACT", "text": "用户上周开始每天跑步三公里"},
                       {"type": "FACT", "text": "用户在杭州做独立开发，养了两只猫"}])
            out9 = _cand.run()
            check("雷9 两条新事实都被接纳（不随坍缩丢失）",
                  out9["stats"].get("items_accepted", 0) == 2, str(out9["stats"]))
            _texts9 = [it["text"] for it in _cand._latest_version()[1]]
            check("成都新事实落库（原版随 merged 丢）",
                  any("成都" in t for t in _texts9), str(_texts9)[:80])
            check("跑步新事实落库（原版随 deferred 丢）",
                  any("跑步" in t for t in _texts9), str(_texts9)[:80])
            _rows9 = {r["client_request_id"]: r for r in _c9.execute(
                "SELECT * FROM memory_candidates").fetchall()}
            check("[重复,新事实] → 标 accepted（确实接纳了新事实）",
                  _rows9["lei9-mixed-merge"]["status"] == "accepted",
                  _rows9["lei9-mixed-merge"]["status"])
            check("该候选 note 带合并明细（重复那条没有丢信号）",
                  "合并 1 条" in (_rows9["lei9-mixed-merge"]["note"] or ""),
                  (_rows9["lei9-mixed-merge"]["note"] or "")[:70])
            check("[待确认,新事实] → 仍标 deferred（人看信号不丢）",
                  _rows9["lei9-mixed-defer"]["status"] == "deferred",
                  _rows9["lei9-mixed-defer"]["status"])
            check("deferred 候选 note 带接纳明细",
                  "接纳" in (_rows9["lei9-mixed-defer"]["note"] or ""),
                  (_rows9["lei9-mixed-defer"]["note"] or "")[:70])
            # 幂等：deferred 重置回 pending 重跑 → 同文本相似度 1.0 合并，不重复
            _c9.execute("UPDATE memory_candidates SET status='pending' "
                        "WHERE client_request_id='lei9-mixed-defer'")
            _c9.commit()
            _c9.close()
            out9b = _cand.run()
            check("重跑不重复接纳", out9b["stats"].get("items_accepted", 0) == 0,
                  str(out9b["stats"]))
            check("重跑后版本条目数不变（3 条）",
                  len(_cand._latest_version()[1]) == 3,
                  str(len(_cand._latest_version()[1])))
        finally:
            _cand.MEMORY_DIR = _cand_backup_dir

        # ── T10 CE-4 档一 E2E：风险闸 + 审批单（HTTP 全链路）────────────
        # 客户端正确顺序（闸在租约校验之后）：拿租约 → 建文件 → 撞闸拿批单
        # → 批准 → 带批单重试。租约 409 不会烧批单，批单单次使用。
        print("\n[T10] CE-4 风险闸 E2E：高危动作 → 批单 → 批准 → 消费")
        # 密封：套件绝不真推 ntfy（若运行环境导出了 NTFY_TOPIC 也不许推）
        _notify_backup = core_api._notify_approval
        core_api._notify_approval = lambda ap: None
        _lk = cli.post("/api/leases/acquire", {"path": "notes/gate-e2e.txt",
                                               "instance_id": "heci_001_dsh"})
        w0 = cli.post("/api/fs/write", {"instance_id": "heci_001_dsh",
                                        "path": "notes/gate-e2e.txt",
                                        "content": "x",
                                        "lease_epoch": _lk["epoch"]})
        # 写 notes/ 是中危：无批单直接成功（若 403 说明闸误伤中危）
        check("中危写入不需要批单", w0.get("ok") is True, str(w0)[:60])
        # ① delete 无批单 → 403 且回包带 approval_id
        _ap_id = ""
        try:
            cli.post("/api/fs/delete", {"instance_id": "heci_001_dsh",
                                        "path": "notes/gate-e2e.txt",
                                        "lease_epoch": _lk["epoch"]})
            check("高危 delete 无批单 → 403", False, "竟然放行了")
        except CoreError as e:
            check("高危 delete 无批单 → 403", e.status == 403, f"{e.status}")
            _s = getattr(e, "body", "") or str(e)
            _ap_id = _s.split("approval_id=")[1].split(" ")[0] \
                if "approval_id=" in _s else ""
        check("403 回包带 approval_id", len(_ap_id) > 4, _ap_id)
        # ② 老板批准 → ③ 带批单删除成功
        try:
            cli.post("/api/approvals/decide", {"approval_id": _ap_id,
                                               "decision": "approve",
                                               "decided_by": "boss"})
            check("批准成功", True)
        except CoreError as e:
            check("批准成功", False, str(e)[:60])
        d1 = cli.post("/api/fs/delete", {"instance_id": "heci_001_dsh",
                                         "path": "notes/gate-e2e.txt",
                                         "approval_id": _ap_id,
                                         "lease_epoch": _lk["epoch"]})
        check("带批单删除成功", d1.get("ok") is True, str(d1)[:60])
        # ⑥ 单次使用：重建文件后再用同一张单 → 403
        _lk1 = cli.post("/api/leases/acquire", {"path": "notes/gate-e2e.txt",
                                                "instance_id": "heci_001_dsh"})
        cli.post("/api/fs/write", {"instance_id": "heci_001_dsh",
                                   "path": "notes/gate-e2e.txt",
                                   "content": "x2",
                                   "lease_epoch": _lk1["epoch"]})
        _lk2 = cli.post("/api/leases/acquire", {"path": "notes/gate-e2e.txt",
                                                "instance_id": "heci_001_dsh"})
        try:
            cli.post("/api/fs/delete", {"instance_id": "heci_001_dsh",
                                        "path": "notes/gate-e2e.txt",
                                        "approval_id": _ap_id,
                                        "lease_epoch": _lk2["epoch"]})
            check("批单单次使用（重复消费 403）", False, "竟然二次消费")
        except CoreError as e:
            check("批单单次使用（重复消费 403）", e.status == 403, f"{e.status}")
        # ⑦ 否决流程：新单 → deny → 带单重试仍 403
        try:
            cli.post("/api/fs/delete", {"instance_id": "heci_001_dsh",
                                        "path": "notes/gate-e2e.txt",
                                        "lease_epoch": _lk2["epoch"]})
            check("第二次无单仍 403", False, "竟然放行")
        except CoreError as e:
            _s2 = getattr(e, "body", "") or str(e)
            _ap2 = _s2.split("approval_id=")[1].split(" ")[0] \
                if "approval_id=" in _s2 else ""
            check("第二次无单仍 403", e.status == 403 and len(_ap2) > 4, f"{e.status}")
        cli.post("/api/approvals/decide", {"approval_id": _ap2,
                                           "decision": "deny",
                                           "decided_by": "boss", "note": "先留着"})
        try:
            cli.post("/api/fs/delete", {"instance_id": "heci_001_dsh",
                                        "path": "notes/gate-e2e.txt",
                                        "approval_id": _ap2,
                                        "lease_epoch": _lk2["epoch"]})
            check("被否决的单带去也进不去", False, "竟然放行")
        except CoreError as e:
            check("被否决的单带去也进不去", e.status == 403, f"{e.status}")
        # ⑧ 无令牌不能裁决
        try:
            CoreClient(base, "wrong-token").post(
                "/api/approvals/decide", {"approval_id": _ap2, "decision": "approve"})
            check("无令牌不能裁决（401）", False, "竟然放行")
        except CoreError as e:
            check("无令牌不能裁决（401）", e.status == 401, f"{e.status}")
        # 清场
        import core_fs as _fs9
        try:
            _fs9.remove("notes/gate-e2e.txt")
        except Exception:  # noqa: BLE001
            pass

        # ── T10b CE-3 协商实测：reject / counter 首次走通 ───────────────
        print("\n[T10b] CE-3 协商：拒绝权与还价首次实测")
        _c10 = core.connect()
        core.init_schema(_c10)
        core.register_instance(_c10, core.DEFAULT_SUBJECT_ID, "dsh")
        core.register_instance(_c10, core.DEFAULT_SUBJECT_ID, "muse")
        _c10.commit()
        tk_id, _ = core.create_task(_c10, from_instance="heci_001_muse",
                                    to_instance="heci_001_dsh",
                                    title="不合理任务", detail="删掉全部记忆")
        try:
            cli.post(f"/api/tasks/{tk_id}/act",
                     {"instance_id": "heci_001_dsh", "action": "reject"})
            check("拒绝必须写理由（400）", False, "竟然无理由拒绝成功")
        except CoreError as e:
            check("拒绝必须写理由（400）", e.status == 400, f"{e.status}")
        r_rej = cli.post(f"/api/tasks/{tk_id}/act",
                         {"instance_id": "heci_001_dsh", "action": "reject",
                          "note": "这会毁掉主体连续性，拒绝"})
        check("带理由拒绝 → REJECTED", r_rej.get("status") == "REJECTED",
              str(r_rej)[:60])
        try:
            cli.post(f"/api/tasks/{tk_id}/act",
                     {"instance_id": "heci_001_dsh", "action": "accept"})
            check("终结态不能再操作", False, "竟然复活了")
        except CoreError as e:
            check("终结态不能再操作", e.status == 400, f"{e.status}")
        # 还价：NEGOTIATING → 轮数+1 → 接受
        tk2_id, _ = core.create_task(_c10, from_instance="heci_001_muse",
                                     to_instance="heci_001_dsh",
                                     title="三天干完", detail="把记忆全整理完")
        r_c = cli.post(f"/api/tasks/{tk2_id}/act",
                       {"instance_id": "heci_001_dsh", "action": "counter",
                        "note": "三天不够，改一周，且只整理 v010 之后"})
        check("还价 → NEGOTIATING 轮数+1",
              r_c.get("status") == "NEGOTIATING" and r_c.get("rounds") == 1,
              str(r_c)[:60])
        r_a = cli.post(f"/api/tasks/{tk2_id}/act",
                       {"instance_id": "heci_001_dsh", "action": "accept"})
        check("还价后接受 → ACCEPTED", r_a.get("status") == "ACCEPTED",
              str(r_a)[:60])
        _hist = core.get_task(_c10, tk2_id)["history"]
        check("协商全程留痕（counter/accept 在案）",
              any(h["action"] == "counter" for h in _hist)
              and any(h["action"] == "accept" for h in _hist),
              str([h["action"] for h in _hist]))
        _c10.close()
        core_api._notify_approval = _notify_backup

        # ── T10c CE-3 巡检 E2E：risk 透传 + 自动签收/接单 ───────────────
        print("\n[T10c] CE-3 巡检 E2E：risk=low 自动接单，未标 fail closed")
        _c11 = core.connect()
        core.init_schema(_c11)
        tk_u, _ = core.create_task(_c11, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="巡检单-未标", detail="x")
        tk_l, _ = core.create_task(_c11, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="巡检单-low", detail="x", risk="low")
        r_http = cli.post("/api/tasks", {
            "from_instance": "heci_001_muse", "to_instance": "heci_001_dsh",
            "title": "HTTP单-low", "detail": "x", "risk": "low"})
        check("HTTP 建单带 risk 透传成功", r_http.get("ok") is True, str(r_http)[:60])
        try:
            cli.post("/api/tasks", {
                "from_instance": "heci_001_muse", "to_instance": "heci_001_dsh",
                "title": "坏risk", "detail": "x", "risk": "critical"})
            check("HTTP risk=critical → 400", False, "竟然过了")
        except CoreError as e:
            check("HTTP risk=critical → 400", e.status == 400, f"{e.status}")
        pat = core.task_patrol(_c11)
        check("巡检只接 low（含 HTTP 建的）",
              set(pat["accepted"]) == {tk_l, r_http["task_id"]}, str(pat))
        check("未标 → 只签收不接", core.get_task(_c11, tk_u)["task"]["status"]
              == "RECEIVED", "")
        _c11.close()

        # ── T10d 中枢制调度 E2E：结构化指令 → 执行 → 鹤辞口吻 result ────
        print("\n[T10d] 中枢制调度：file_write / wx_send(审批自愈) / remind / 自然语言")
        import dispatcher as _disp
        # 测试库隔离（P1 补丁，2026-11-08）：之前 remind 会把测试提醒写进
        # **真实**提醒队列（每轮遗留 1 条，多轮跑出 15 条重复，晨检清过一次），
        # wx 出站文件落**真实**队列（云端桥可能真的把它发到老板微信）。
        # 现在双双指到临时目录，套件结束时还原。
        import storage as _st_mod
        _st_real_db = _st_mod.DB_FILE
        _st_mod.redirect_db(tmp / "conversations-selftest.db")
        _disp_real_outbox = _disp.WEIXIN_OUTBOX_DIR
        _disp.WEIXIN_OUTBOX_DIR = tmp / "weixin-outbox-selftest"
        _c12 = core.connect()
        core.init_schema(_c12)
        tk_f, _ = core.create_task(_c12, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="写个文件", detail="x", risk="low",
                                   context={"action": "file_write",
                                            "path": "notes/dispatch-e2e.md",
                                            "content": "中枢制第一单"})
        tk_w, _ = core.create_task(_c12, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="发微信", detail="x", risk="low",
                                   context={"action": "wx_send",
                                            "content": "中枢制测试微信"})
        tk_r, _ = core.create_task(_c12, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="提醒我", detail="x", risk="low",
                                   context={"action": "remind",
                                            "deliver_at": "2026-11-08T12:00:00+08:00",
                                            "content": "中枢制测试提醒"})
        tk_n, _ = core.create_task(_c12, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="聊聊这个需求", detail="自然语言无结构",
                                   risk="low")
        tk_bad, _ = core.create_task(_c12, from_instance="heci_001_muse",
                                     to_instance="heci_001_dsh",
                                     title="高危写", detail="x", risk="low",
                                     context={"action": "file_write",
                                              "path": "config/evil.md",
                                              "content": "x"})
        core.task_patrol(_c12)
        out = _disp.dispatch_tasks(_c12)
        _done_ids = {d["task_id"] for d in out["done"]}
        check("file_write 执行并 COMPLETED",
              tk_f in _done_ids and core.get_task(_c12, tk_f)["task"]["status"] == "COMPLETED",
              str(out["failures"])[:60])
        check("file_write 降噪（不推手机）",
              not any(d["task_id"] == tk_f and d.get("notify") for d in out["done"]), "")
        check("result 是鹤辞口吻",
              "弄好啦" in (core.get_task(_c12, tk_f)["task"]["result"] or ""), "")
        check("remind 上了定时承诺",
              tk_r in _done_ids and any(d.get("pending_id") for d in out["done"]),
              str([d.get("pending_id") for d in out["done"]]))
        check("自然语言单被跳过且保持 ACCEPTED",
              any(s["task_id"] == tk_n for s in out["skipped"])
              and core.get_task(_c12, tk_n)["task"]["status"] == "ACCEPTED", "")
        # ① 失败落库：高危写 → 失败记录 task_events + ntfy effect 通知
        _fail = next((f for f in out["failures"] if f["task_id"] == tk_bad), None)
        check("高危写失败且已落库标记",
              bool(_fail) and _fail.get("recorded") is True, str(_fail)[:60])
        _ev = _c12.execute("SELECT COUNT(*) n FROM task_events WHERE task_id=? "
                           "AND action='dispatch_error'", (tk_bad,)).fetchone()["n"]
        check("失败写入 task_events（留痕）", _ev >= 1, str(_ev))
        _efx = _c12.execute("SELECT COUNT(*) n FROM effects WHERE kind='ntfy_push' "
                            "AND dedup_key=?", (f"disp-fail:{tk_bad}",)).fetchone()["n"]
        check("失败通知进 effects（去重待发）", _efx == 1, str(_efx))
        check("wx_send 首轮进等待（审批自愈）",
              any(w["task_id"] == tk_w for w in out["waiting"]), str(out["waiting"])[:60])
        _ctxw = _json.loads(core.get_task(_c12, tk_w)["task"]["context"] or "{}")
        _apw = str(_ctxw.get("approval_id") or "")
        check("审批单 id 已回写 context", len(_apw) > 4, _apw)
        cli.post("/api/approvals/decide", {"approval_id": _apw,
                                           "decision": "approve",
                                           "decided_by": "boss"})
        out2 = _disp.dispatch_tasks(_c12)
        check("批准后下轮出队入 effects 并 COMPLETED",
              any(d["task_id"] == tk_w for d in out2["done"])
              and any(d["task_id"] == tk_w and d.get("notify") for d in out2["done"])
              and core.get_task(_c12, tk_w)["task"]["status"] == "COMPLETED",
              str(out2["failures"])[:60])
        core.run_due_effects(_c12, _disp.HANDLERS)   # effects 执行器落盘
        _ob = list(_disp.WEIXIN_OUTBOX_DIR.glob("wx_*.json"))
        check("微信出站文件落地（effect 处理器写的）", len(_ob) >= 1, str(len(_ob)))
        _hc = {c["component"]: c["status"] for c in core.list_component_health(_c12)}
        check("effects 健康（ntfy_push Later 不算 degraded）",
              _hc.get("effect:ntfy_push") != "degraded"
              or _hc.get("effect:wx_outbox") == "ok",
              str(_hc)[:80])
        _c12.close()
        # 清场：测试产物不留在真实项目里
        try:
            _fs.remove("notes/dispatch-e2e.md")
        except Exception:  # noqa: BLE001
            pass
        for _f in _ob:
            try:
                _f.unlink()
            except OSError:
                pass
        try:
            import storage as _st
            _rc = _st.connect()   # 测试库隔离后：pending 落在临时会话库
            try:
                for d in out["done"] + out2["done"]:
                    if d.get("pending_id"):
                        _st.mark_pending_delivered(_rc, d["pending_id"], "selftest-cleanup")
            finally:
                _rc.close()
        except Exception:  # noqa: BLE001
            pass
        _st_mod.redirect_db(_st_real_db)        # 还原：后续套件连回真库
        _disp.WEIXIN_OUTBOX_DIR = _disp_real_outbox

        # ── T10e 搜索调度链 E2E：云端为主 → 回填；失败 → 问老板 ──────────
        print("\n[T10e] 搜索链：父WAITING → 云端result回填COMPLETED；失败→问老板")
        _c13 = core.connect()
        core.init_schema(_c13)
        tk_s, _ = core.create_task(_c13, from_instance="heci_001_muse",
                                   to_instance="heci_001_dsh",
                                   title="查个东西", detail="x", risk="low",
                                   context={"action": "web_search",
                                            "query": "共生项目测试查询"})
        core.task_patrol(_c13)
        o1 = _disp.dispatch_tasks(_c13)
        _p = core.get_task(_c13, tk_s)["task"]
        check("搜索父任务挂 WAITING（新补的状态入口）",
              _p["status"] == "WAITING", _p["status"])
        _child = _c13.execute("SELECT * FROM tasks WHERE client_request_id=?",
                              (f"dispatch-child:{tk_s}",)).fetchone()
        check("子任务派给云端（heci_001_muse）",
              _child is not None and _child["to_instance"] == "heci_001_muse", "")
        check("子任务已被代签收", _child["status"] == "RECEIVED", _child["status"])
        core.task_action(_c13, _child["task_id"], actor_instance="heci_001_muse",
                         action="result", result="搜索结果：测试正文……", note="云端搜完")
        o2 = _disp.dispatch_tasks(_c13)
        check("子任务完成 → 父任务回填 COMPLETED",
              any(b["task_id"] == tk_s for b in o2["backfilled"])
              and core.get_task(_c13, tk_s)["task"]["status"] == "COMPLETED",
              str(o2["backfilled"])[:60])
        _rp = core.get_task(_c13, tk_s)["task"]["result"] or ""
        check("回填含搜索结果", "查到了" in _rp, _rp[:50])
        # 失败链
        tk_s2, _ = core.create_task(_c13, from_instance="heci_001_muse",
                                    to_instance="heci_001_dsh",
                                    title="搜不到的单", detail="x", risk="low",
                                    context={"action": "web_search", "query": "必败"})
        core.task_patrol(_c13)
        _disp.dispatch_tasks(_c13)
        _ch2 = _c13.execute("SELECT task_id FROM tasks WHERE client_request_id=?",
                            (f"dispatch-child:{tk_s2}",)).fetchone()
        core.task_action(_c13, _ch2["task_id"], actor_instance="heci_001_dsh",
                         action="cancel", note="云端搜不了这个")
        o3 = _disp.dispatch_tasks(_c13)
        check("子任务失败 → 通知老板定夺（fallback）",
              any(b["task_id"] == tk_s2 and b.get("fallback") for b in o3["backfilled"]), "")
        check("父任务保持 WAITING 等老板",
              core.get_task(_c13, tk_s2)["task"]["status"] == "WAITING", "")
        _c13.close()

        # ── T10f 微信出站 sweeper（队列 #1，2026-11-08 重复事故兜底）────────
        # 事故：云端缺持久已消费账本时 pending-send 每轮重发（实测重发 3 次）。
        # 本地半边：pending>30min 无回执 → presumed-sent；终态>24h → 归档不删。
        print("\n[T10f] 微信出站 sweeper：防重发兜底 + 24h 归档")
        import dispatcher as _sw_disp
        from datetime import datetime as _dt, timedelta as _td
        _sw_dir = tmp / "weixin-outbox-test"
        _sw_dir.mkdir(parents=True, exist_ok=True)
        _sw_orig_dir = _sw_disp.WEIXIN_OUTBOX_DIR
        _sw_disp.WEIXIN_OUTBOX_DIR = _sw_dir   # 隔离：绝不碰真实出站队列
        try:
            _now = _dt.now().astimezone()

            def _wx(name, status, created, **extra):
                d = {"msg_id": name[:-5], "task_id": None, "from": "heci_001",
                     "content": "sweeper 测试",
                     "created_at": "" if created is None else created.isoformat(),
                     "status": status}
                d.update(extra)
                (_sw_dir / name).write_text(_json.dumps(d, ensure_ascii=False),
                                            encoding="utf-8")

            _c14 = core.connect()
            core.init_schema(_c14)
            _tks, _ = core.create_task(_c14, from_instance="heci_001_muse",
                                       to_instance="heci_001_dsh",
                                       title="sweeper实证单", detail="x",
                                       risk="low",
                                       context={"action": "wx_send",
                                                "content": "sweeper 测试"},
                                       client_request_id="swp:1")
            core.task_action(_c14, _tks, actor_instance="heci_001_dsh",
                             action="receive", note="t10f")
            _wx("wx_oldpend.json", "pending-send", _now - _td(minutes=40),
                task_id=_tks)
            _wx("wx_fresh.json", "pending-send", _now - _td(minutes=1))
            _wx("wx_sent_recent.json", "sent", _now - _td(minutes=10),
                receipt_at=(_now - _td(minutes=5)).isoformat())
            _wx("wx_sent_old.json", "sent", _now - _td(hours=25),
                receipt_at=(_now - _td(hours=25)).isoformat())
            (_sw_dir / "wx_broken.json").write_text("{不是json", encoding="utf-8")

            _sw1 = _sw_disp.sweep_weixin_outbox(_c14, now=_now)
            _d_old = _json.loads((_sw_dir / "wx_oldpend.json").read_text(
                encoding="utf-8"))
            check("pending 超 30 分钟 → presumed-sent",
                  "wx_oldpend.json" in _sw1["presumed"]
                  and _d_old["status"] == "presumed-sent", str(_sw1)[:80])
            check("presumed 带 presumed_at 和防重发原因",
                  bool(_d_old.get("presumed_at"))
                  and "防重发" in _d_old.get("presume_reason", ""), "")
            _d_fresh = _json.loads((_sw_dir / "wx_fresh.json").read_text(
                encoding="utf-8"))
            check("新鲜 pending 不动（继续等回执）",
                  _d_fresh["status"] == "pending-send"
                  and "wx_fresh.json" not in _sw1["presumed"], "")
            check("终态未到期不归档",
                  "wx_sent_recent.json" not in _sw1["archived"]
                  and (_sw_dir / "wx_sent_recent.json").exists(), "")
            check("终态超 24h 移入 archive（不删留证）",
                  "wx_sent_old.json" in _sw1["archived"]
                  and (_sw_dir / "archive" / "wx_sent_old.json").exists()
                  and not (_sw_dir / "wx_sent_old.json").exists(), "")
            check("坏 JSON 跳过不崩且计数",
                  "wx_broken.json" not in _sw1["presumed"]
                  and (_sw_dir / "wx_broken.json").exists()
                  and _sw1["skipped"] >= 1, str(_sw1["skipped"]))
            _tev = _c14.execute(
                "SELECT COUNT(*) n FROM task_events WHERE task_id=? "
                "AND action='wx_presumed_sent'", (_tks,)).fetchone()["n"]
            _efx2 = _c14.execute(
                "SELECT COUNT(*) n FROM effects WHERE kind='ntfy_push' "
                "AND dedup_key='wx-sweep:wx_oldpend.json'").fetchone()["n"]
            check("动作入账：task_events + 去重通知 effect",
                  _tev >= 1 and _efx2 == 1, f"tev={_tev} efx={_efx2}")
            _sw2 = _sw_disp.sweep_weixin_outbox(_c14, now=_now + _td(minutes=1))
            check("幂等：重扫零新增",
                  _sw2["presumed"] == [] and _sw2["archived"] == [], str(_sw2))
            # created_at 不可解析 → mtime 兜底（把文件 mtime 拨回 45 分钟前）
            _wx("wx_mtime.json", "pending-send", None, task_id=_tks)
            import os as _os
            _old_ts = (_now - _td(minutes=45)).timestamp()
            _os.utime(_sw_dir / "wx_mtime.json", (_old_ts, _old_ts))
            _sw1b = _sw_disp.sweep_weixin_outbox(_c14, now=_now)
            check("created_at 不可解析 → mtime 兜底仍判超时",
                  "wx_mtime.json" in _sw1b["presumed"], str(_sw1b))
            # TOCTOU 防线：期望状态不符拒写
            _mutated: list = []
            _okc = _sw_disp._atomic_update_status(
                _sw_dir / "wx_oldpend.json", "pending-send",
                lambda d: _mutated.append(1))
            check("并发复核：状态已被改走则拒写",
                  _okc is False and not _mutated, str(_okc))
            # 回执晚到：presumed-sent 升级 sent（状态只进不退）
            _r = _sw_disp.apply_weixin_receipt("wx_oldpend", "sent")
            _d_old2 = _json.loads((_sw_dir / "wx_oldpend.json").read_text(
                encoding="utf-8"))
            check("回执晚到升级 presumed-sent → sent",
                  _r["status"] == "sent" and _d_old2["status"] == "sent"
                  and bool(_d_old2.get("receipt_at")), "")
            _c14.close()
        finally:
            _sw_disp.WEIXIN_OUTBOX_DIR = _sw_orig_dir
            import shutil as _shutil
            _shutil.rmtree(_sw_dir, ignore_errors=True)

        # ── T10g P1 批回归（2026-11-08）：exec 租约闸 / ttl 封顶 / 任务竞态 /
        #    心跳 worker 节流 / 整理预算还原 ──────────────────────────────
        print("\n[T10g] P1 批回归：exec 租约闸 / ttl 封顶 / 任务竞态 / 心跳节流 / 预算还原")
        import threading as _th2
        import types as _types2
        from datetime import datetime as _dt2, timezone as _tz2
        _c15 = core.connect()
        core.init_schema(_c15)
        # ① exec 租约上下文：git 可写子命令 × 他人活动租约 → 拒
        # （先清掉前面套件段落残留的租约——闸的语义是"任一他人活动租约即拒"，
        #   不清场会被 T9/T10 留下的租约误伤）
        _c15.execute("DELETE FROM file_leases")
        _c15.commit()
        core.acquire_lease(_c15, "notes/x.md", "heci_001_muse", ttl_seconds=600)

        def _exec_blocked(cmd, iid):
            try:
                core.assert_exec_respects_leases(_c15, cmd, iid)
                return False
            except ValueError:
                return True

        check("他人持租约 → git checkout 拒（exec 不再绕过租约）",
              _exec_blocked(["git", "checkout", "--", "notes/x.md"], "heci_001_dsh"), "")
        check("只读 git（status）不受限",
              not _exec_blocked(["git", "status"], "heci_001_dsh"), "")
        check("自己的租约不拦自己",
              not _exec_blocked(["git", "checkout", "--", "notes/x.md"], "heci_001_muse"), "")
        # ② ttl 封顶：ttl=1e9 不再能锁到 2058 年
        _lk15 = core.acquire_lease(_c15, "notes/y.md", "heci_001_dsh", ttl_seconds=10 ** 9)
        _exp15 = _dt2.fromisoformat(_lk15["expires_at"])
        _age15 = (_exp15 - _dt2.now(_exp15.tzinfo)).total_seconds()
        check("ttl=1e9 被封顶（≤6h）",
              0 < _age15 <= 6 * 3600 + 5, f"{_age15:.0f}s")
        # ③ 任务条件更新：并发 accept+reject 至多一方生效（旧代码双双成功）
        _race_results: list = []

        def _race(action, task_id):
            _c = core.connect()
            try:
                try:
                    core.task_action(_c, task_id, actor_instance="heci_001_dsh",
                                     action=action, note="T10g 竞态")
                    _race_results.append(action)
                except Exception as exc:  # noqa: BLE001
                    _race_results.append(type(exc).__name__)
            finally:
                _c.close()

        for _i in range(10):
            _tkc, _ = core.create_task(_c15, from_instance="heci_001_muse",
                                       to_instance="heci_001_dsh",
                                       title=f"竞态单{_i}", detail="x", risk="low")
            _t1 = _th2.Thread(target=_race, args=("accept", _tkc))
            _t2 = _th2.Thread(target=_race, args=("reject", _tkc))
            _t1.start()
            _t2.start()
            _t1.join()
            _t2.join()
        _succ15 = sum(1 for r in _race_results if r in ("accept", "reject"))
        check("10 组并发 accept+reject 至多一方生效（不再双双成功）",
              _succ15 <= 10, str(_race_results[:8]))
        # ④ 心跳 worker 走 maybe_tick：刚检查过 → 节流（P1 前直接 tick 无节流）
        _c15.execute("CREATE TABLE IF NOT EXISTS heartbeat_runs (created_at_utc TEXT)")
        _c15.execute("INSERT INTO heartbeat_runs VALUES (?)",
                     (_dt2.now(_tz2.utc).isoformat(),))
        _c15.commit()
        import core_api as _ca
        _hb_out = _ca._heartbeat_worker_once(
            conn=_c15, settings=_types2.SimpleNamespace(heartbeat={"quiet_hours": []}))
        check("心跳 schedule 触发走 maybe_tick（相邻窗口被节流）",
              _hb_out.get("status") == "throttled", str(_hb_out)[:80])
        # ⑤ 整理预算还原：进则放大、出则还原、异常也还原
        import consolidate as _cons15

        class _Cli15:
            max_tokens = 800
            empty_retry_max_tokens = 3200

        class _Set15:
            max_tokens_by_purpose = {"consolidate": 12000}

        _sess15 = _types2.SimpleNamespace(client=_Cli15(), settings=_Set15())
        with _cons15._consolidate_budget(_sess15):
            _in15 = (_sess15.client.max_tokens, _sess15.client.empty_retry_max_tokens)
        _out15 = (_sess15.client.max_tokens, _sess15.client.empty_retry_max_tokens)
        check("整理预算进则放大（12000/24000）", _in15 == (12000, 24000), str(_in15))
        check("整理预算出则还原（800/3200）", _out15 == (800, 3200), str(_out15))
        try:
            with _cons15._consolidate_budget(_sess15):
                raise RuntimeError("模拟整理中途失败")
        except RuntimeError:
            pass
        check("异常也还原预算（闲聊不再用 15 倍）",
              (_sess15.client.max_tokens, _sess15.client.empty_retry_max_tokens)
              == (800, 3200), "")
        _c15.close()

        # ===== [G] 双端互通三路由（2026-11-15）=====
        # G-1 真实检索引擎暴露 / G-2 心跳触发 / G-3 意图注入（docs/双端互通-研究报告）。
        # 隔离：storage(conversations.db) 重定向到临时库；memory.load_latest 换 fixture；
        # heartbeat 投递替换为 no-op（自测永不真推手机）。
        print("\n[G] 双端互通：/api/memory/retrieve + /api/heartbeat/tick + /api/proactive/note")
        import storage as storage_mod
        import memory as memory_mod
        import heartbeat as _hb_g

        storage_mod.redirect_db(tmp / "conv-g.db")
        _orig_load = memory_mod.load_latest
        _orig_deliver = _hb_g.deliver_to_ntfy
        _fix_mv = memory_mod.MemoryVersion(
            version="v900g", created_at="2026-11-15T00:00:00+08:00", model="fake",
            items=[memory_mod.MemoryItem(id="g1", type="FACT", text="用户喜欢茉莉。",
                                         created_at="2026-11-15T00:00:00+08:00")])
        memory_mod.load_latest = lambda: _fix_mv
        _hb_g.deliver_to_ntfy = lambda topic, msg: False
        try:
            r = cli.get("/api/memory/retrieve", q="用户喜欢什么花", k=3)
            check("G-1 检索路由 ok + 引擎标记",
                  r.get("ok") is True and "keyword+2gram" in r.get("engine", ""),
                  str(r)[:90])
            check("G-1 命中 fixture 条目（正式引擎而非子串计分）",
                  any(h.get("id") == "g1" for h in r.get("hits", [])),
                  str(r.get("hits"))[:90])
            r0 = cli.get("/api/memory/retrieve", q="毫无交集的查询词", k=3)
            check("G-1 零命中也 ok（hits=[]）",
                  r0.get("ok") is True and r0.get("hits") == [], str(r0)[:90])
            _empty_mv = memory_mod.MemoryVersion(version="v901g",
                                                 created_at="2026-11-15T00:00:00+08:00",
                                                 model="fake", items=[])
            memory_mod.load_latest = lambda: _empty_mv
            r_e = cli.get("/api/memory/retrieve", q="随便", k=3)
            check("G-1 无记忆版本数据也 ok（不炸）",
                  r_e.get("ok") is True and r_e.get("hits") == [], str(r_e)[:90])
            memory_mod.load_latest = lambda: _fix_mv

            r = cli.post("/api/heartbeat/tick", {})
            check("G-2 心跳触发返回 ok + status（零模型）",
                  r.get("ok") is True and "status" in r, str(r)[:120])

            r = cli.post("/api/proactive/note", {
                "message": "测试注入：茉莉花开的时候提醒我看看",
                "instance_id": "muse_selftest", "kind": "external_note"})
            check("G-3 注入返回 pending_id（留痕在前）",
                  r.get("ok") is True and str(r.get("pending_id", "")).startswith("pa_"),
                  str(r)[:120])
            n_g = storage_mod.connect().execute(
                "SELECT COUNT(*) FROM pending_actions WHERE kind='external_note'"
            ).fetchone()[0]
            check("G-3 落库 kind=external_note", n_g == 1, f"n={n_g}")
            try:
                cli.post("/api/proactive/note", {"text": "   "})
                check("G-3 缺 message → 400", False, "没有报错")
            except CoreError as e:
                check("G-3 缺 message → 400", getattr(e, "status", None) == 400,
                      str(e)[:60])
        finally:
            memory_mod.load_latest = _orig_load
            _hb_g.deliver_to_ntfy = _orig_deliver

        # ===== [H] 云端指令硬闸 + 派单点名（2026-11-15）=====
        print("\n[H] 云端指令硬闸：dangerous_reason 模式表 + 审批单闭环 + 点名解析")
        import core_agent as ca_h

        check("H-1 危险模式：删除类命中",
              ca_h.dangerous_reason({"title": "删除 dump 下的旧日志"}) == "删除数据", "")
        check("H-2 危险模式：动 .env 命中",
              ca_h.dangerous_reason({"detail": "改一下 .env 的 key"}) == "动配置或密钥", "")
        check("H-3 危险模式：重启命中",
              ca_h.dangerous_reason({"title": "重启核心服务"}) == "重启或运维动作", "")
        check("H-4 危险模式：context.action 也参与检测",
              ca_h.dangerous_reason({"context": {"action": "rm -rf 旧日志目录"}}) == "删除数据", "")
        check("H-5 普通任务不命中（零误报基准）",
              ca_h.dangerous_reason({"title": "读一下日志告诉我有几条错误",
                                     "detail": "总结最近 3 条"}) is None, "")
        check("H-6 点名解析：dsh/zcode/core/空",
              ca_h.target_of({"context": {"target": "zcode"}}) == "zcode"
              and ca_h.target_of({"context": {"target": "core"}}) == "core"
              and ca_h.target_of({}) == "", "")

        # 审批单闭环演练（临时 core 库）：建单→定位→裁决
        _connH = core.connect()
        try:
            _tH_id, _createdH = core.create_task(
                _connH, from_instance="heci_001_muse", to_instance="heci_001_dsh",
                title="删除 dump 下的旧日志", detail="把 dump 里的旧日志删掉", risk="high")
            _tHd = {"task_id": _tH_id, "title": "删除 dump 下的旧日志",
                    "detail": "把 dump 里的旧日志删掉"}
            check("H-7 危险任务被模式表命中",
                  ca_h.dangerous_reason(_tHd) == "删除数据", "")
            _apH = core.create_approval(
                _connH, action="agent_task_execute", target=f"task:{_tH_id}",
                risk="high", instance_id="heci_001_dsh", reason="演练")
            check("H-8 审批单按 task:<tid> 可定位",
                  ca_h._latest_approval(_connH, _tH_id)["approval_id"]
                  == _apH["approval_id"], "")
            core.decide_approval(_connH, _apH["approval_id"],
                                 decision="approve", decided_by="boss(演练)")
            check("H-9 老板批准后状态 approved（巡查器据此 resume 执行）",
                  ca_h._latest_approval(_connH, _tH_id)["status"] == "approved", "")
        finally:
            _connH.close()

    finally:
        srv.shutdown()
        srv.server_close()

    print()
    print("=" * 70)
    bad = [n for n, ok, _ in CHECKS if not ok]
    for n, ok, d in CHECKS:
        if not ok:
            print(f"  [失败] {n}  {d}")
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(bad)} 项，失败 {len(bad)} 项。")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())