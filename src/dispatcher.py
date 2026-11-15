"""中枢制调度器 v2（2026-11-08 老板拍板「整」）——鹤辞的手脚分配。

v2 相对 v1 的三处升级（借鉴 dsh-superagent，老板圈阅的四件）：
  ① 失败落库：执行失败写 task_events（留痕）+ ntfy_push effect（去重通知，
     每单只提醒一次），任务保持 ACCEPTED 下轮自动重试——静默丢失消失。
  ② Effects 化：对外动作不再 fire-and-forget——微信出站/ntfy 推送统一
     进 effects 队列（合并/指数退避/健康条目），由 worker 驱动执行。
  ④ 汇报降噪：done 里**只有对外类（wx_send）推手机**——「只在有人必须
     行动或能用到结果时发言」；file_write/remind 静默落账（result 在
     任务里随时可查），防止「弄好啦」刷屏把老板变成橡皮图章。

v1 的诚实边界不变：只执行结构化指令（context.action）；自然语言任务
v1 不碰——参数抽取是 v2-LLM 的活，别用正则硬解析骗自己。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import companion_core as core
import core_fs
import risk_gate
import storage
from companion_core import Later, enqueue_effect

# 微信出站队列（#3 本地半边）：effect 执行器把消息落到这里，
# 云端桥读 → 发微信 → status=sent/failed 回写。
WEIXIN_OUTBOX_DIR = core.CORE_DIR / "sync" / "weixin-outbox"

REPORT_TEMPLATE = "弄好啦：{title}。{detail}"

# ④ 汇报降噪白名单：只有对外类动作完成后推手机；其余静默落账。
NOTIFY_ON_DONE = {"wx_send"}


def _handle_ntfy_push(payload: dict) -> None:
    """effect 处理器：ntfy 推送。没配 topic = 现在不方便（Later，不算失败）。"""
    from heartbeat import deliver_to_ntfy, ntfy_topic_from_env
    topic = ntfy_topic_from_env()
    if not topic:
        raise Later("NTFY_TOPIC 未配置")
    if not deliver_to_ntfy(topic, str(payload.get("text") or "")):
        raise RuntimeError("ntfy 推送返回失败")


def _handle_wx_outbox(payload: dict) -> None:
    """effect 处理器：微信出站落盘（云端桥读发）。"""
    WEIXIN_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    msg_id = f"wx_{uuid.uuid4().hex[:10]}"
    doc = {
        "msg_id": msg_id,
        "task_id": payload.get("task_id"),
        "from": "heci_001",
        "content": payload.get("content"),
        "created_at": core._now(),
        "status": "pending-send",
    }
    (WEIXIN_OUTBOX_DIR / f"{msg_id}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_weixin_receipt(msg_id: str, status: str, error: str | None = None) -> dict:
    """云端回执（方案 B，2026-11-08）：状态经鉴权 API 回来，不给只读区开口子。

    status: sent / failed。更新出站文件状态字段；文件保留（本地 24h 清理）。
    """
    status = str(status or "").lower()
    if status not in ("sent", "failed"):
        raise ValueError("status 只能是 sent / failed")
    safe = str(msg_id or "").strip()
    if "/" in safe or "\\" in safe or not safe.startswith("wx_"):
        raise ValueError("msg_id 非法")
    path = WEIXIN_OUTBOX_DIR / f"{safe}.json"
    if not path.exists():
        raise ValueError(f"出站消息不存在：{safe}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["status"] = status
    doc["receipt_at"] = core._now()
    if error:
        doc["error"] = error[:300]
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"msg_id": safe, "status": status}


# ── 队列 #1（2026-11-08 老板拍板）：微信直发重复防护 · 本地 sweeper ────────
# 事故（11-08 凌晨）：云端消费端缺"已消费"持久账本时，pending-send 文件每轮
# 重发（实测同一条发了 3 次）。云端已热修持久名单；本地这半边是兜底账本：
#   ① pending-send 超 30 分钟无回执 → presumed-sent（本地不再当"待发"，
#     回执晚到仍会被 apply_weixin_receipt 升级为 sent/failed——只进不退）
#   ② 终态文件超 24h → 移入 archive\（不删，证据链保留；apply_weixin_receipt
#     docstring 注明的"本地 24h 清理"由此兑现，用归档代替删除）
# 群规教训：「回写可能被拒的消费模型，幂等必须自带账本，不依赖对方状态变化」。
WEIXIN_PRESUME_AFTER_MIN = 30   # 云端 cron ≤5 分钟/轮，30 分钟无回执 = 链路异常
WEIXIN_ARCHIVE_AFTER_H = 24


def _parse_iso_ts(value) -> datetime | None:
    """容忍解析 ISO8601 时间戳。

    P1 清单第 4 条（deliver_at 字符串比较）的教训在这里前置规避：
    不做字符串比较，解析成 aware datetime 再比较；解析不了返回 None，
    调用方 fail closed（宁可不动作，不凭猜测动作）。
    """
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # 朴素时间按本地时区补齐（core._now 同源口径）
        dt = dt.astimezone()
    return dt


def _file_age_ts(path, doc: dict, keys: tuple[str, ...]) -> datetime | None:
    """取业务时间戳：按 keys 顺序找第一个可解析的；全失败退回文件 mtime。"""
    for k in keys:
        ts = _parse_iso_ts(doc.get(k))
        if ts is not None:
            return ts
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _atomic_update_status(path, expect_status: str, mutate) -> bool:
    """读-改-写，落盘前复核状态没被并发改掉（CE-4 审批单 TOCTOU 同款教训）。

    并发窗口内的写用 os.replace 原子替换，云端桥不会读到半截 JSON。
    """
    try:
        fresh = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(fresh, dict) or str(fresh.get("status")) != expect_status:
        return False
    mutate(fresh)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True


def sweep_weixin_outbox(conn=None, *, now: datetime | None = None) -> dict:
    """扫微信出站队列，两个动作（队列 #1 本地半边）：

    ① pending-send 超 WEIXIN_PRESUME_AFTER_MIN 分钟无回执 → presumed-sent。
       本地账就此不再视其为"待发"——云端侧即便状态丢失也不会借本地之手
       重发同一条。真丢了怎么办：effect 去重通知老板（每条只提醒一次），
       人来定夺重发——「宁慢勿重」。
    ② 终态（sent/failed/presumed-sent）超 WEIXIN_ARCHIVE_AFTER_H 小时 →
       移入 archive\\ 子目录（不删，留证）。

    conn 可选：给了就把 presumed 动作写 task_events（动作入账铁律）。
    幂等：重复调用无副作用；时间戳全不可用的文件跳过不动（fail closed）。
    """
    now = now or datetime.now().astimezone()
    presumed: list[str] = []
    archived: list[str] = []
    skipped = 0
    if not WEIXIN_OUTBOX_DIR.is_dir():
        return {"presumed": presumed, "archived": archived, "skipped": skipped}
    for path in sorted(WEIXIN_OUTBOX_DIR.glob("wx_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("顶层不是对象")
        except (OSError, ValueError):
            skipped += 1
            continue
        status = str(doc.get("status") or "")
        if status == "pending-send":
            ts = _file_age_ts(path, doc, ("created_at",))
            if ts is None:
                skipped += 1  # 时间戳全不可用：不动，留给下轮/人工
                continue
            if ((now - ts).total_seconds()
                    < WEIXIN_PRESUME_AFTER_MIN * 60):
                continue  # 还年轻，等回执

            def _mark(d: dict) -> None:
                d["status"] = "presumed-sent"
                d["presumed_at"] = core._now()
                d["presume_reason"] = (
                    f"pending 超 {WEIXIN_PRESUME_AFTER_MIN} 分钟无回执，"
                    "sweeper 自动认定（防重发兜底；回执晚到仍可升级）")

            if not _atomic_update_status(path, "pending-send", _mark):
                continue  # 并发窗口里回执先到/文件变了：尊重新状态
            presumed.append(path.name)
            _trace_presumed(conn, doc, path.name)
        elif status in ("sent", "failed", "presumed-sent"):
            ts = _file_age_ts(path, doc, ("receipt_at", "presumed_at", "created_at"))
            if ts is None:
                skipped += 1
                continue
            if (now - ts).total_seconds() < WEIXIN_ARCHIVE_AFTER_H * 3600:
                continue
            archive_dir = WEIXIN_OUTBOX_DIR / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, archive_dir / path.name)
            except OSError:
                skipped += 1
                continue
            archived.append(path.name)
    return {"presumed": presumed, "archived": archived, "skipped": skipped}


def _trace_presumed(conn, doc: dict, name: str) -> None:
    """动作入账：presumed 动作写 task_events（有 conn 且消息挂过任务才写）。"""
    if conn is None:
        return
    tid = str(doc.get("task_id") or "")
    if not tid:
        return
    try:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        if row is None:
            return  # 任务不在本库（如测试临时库已换）：不硬写
        conn.execute(
            "INSERT INTO task_events (task_id, at, actor_instance, action, note, "
            "status_after) VALUES (?,?,?,?,?,?)",
            (tid, core._now(), str(doc.get("from") or "heci_001"),
             "wx_presumed_sent",
             f"出站 {name} pending 超 {WEIXIN_PRESUME_AFTER_MIN} 分钟无回执，"
             "自动 presumed-sent（防重发兜底，人工确认是否重发）",
             str(row["status"] or "")))
        conn.commit()
        _notify_via_effect(
            conn,
            f"⚠️ 微信消息 {name} 超 {WEIXIN_PRESUME_AFTER_MIN} 分钟无回执，已标"
            " presumed-sent（可能没发出去）——要重发就说一声",
            dedup_key=f"wx-sweep:{name}")
    except Exception as exc:  # noqa: BLE001 —— 入账失败不拖垮巡检循环
        print(f"[调度] presumed 入账失败 {name}: {type(exc).__name__}: {exc}")


# worker 驱动 run_due_effects 时带的处理器注册表
HANDLERS = {
    "ntfy_push": _handle_ntfy_push,
    "wx_outbox": _handle_wx_outbox,
}


def _notify_via_effect(conn, text: str, *, dedup_key: str | None = None) -> None:
    """ntfy 推送统一走 effects 队列（合并/退避/健康，不再 fire-and-forget）。"""
    enqueue_effect(conn, kind="ntfy_push", payload={"text": text},
                   dedup_key=dedup_key)


def _heci_report(title: str, detail: str) -> str:
    """鹤辞口吻汇报（v2 仍模板，个性由 v2-LLM 接管）。"""
    return REPORT_TEMPLATE.format(title=title, detail=detail.strip() or "搞定了。")


def _record_failure(conn, task: dict, error: str) -> None:
    """① 失败落库：task_events 留痕 + ntfy effect 通知（每单去重，只提醒一次）。

    任务保持 ACCEPTED —— 下轮调度自然重试；连续失败由 effects 的 dead
    状态与这里的重复通知兜底暴露，不会静默吞掉。
    """
    conn.execute(
        "INSERT INTO task_events (task_id, at, actor_instance, action, note, "
        "status_after) VALUES (?,?,?,?,?,?)",
        (task["task_id"], core._now(), task["to_instance"],
         "dispatch_error", error[:900], "ACCEPTED"))
    conn.commit()
    _notify_via_effect(
        conn,
        f"⚠️ 任务执行失败（自动重试中）：{task['title']} —— {error[:80]}",
        dedup_key=f"disp-fail:{task['task_id']}")


def _backfill_waiting(conn) -> list[dict]:
    """WAITING 父任务的收口（搜索调度链后半段）：

    子任务（云端搜索）COMPLETED → 结果回填父任务 → 鹤辞口吻 COMPLETED；
    子任务 FAILED/REJECTED/CANCELLED → 按老板拍板「云端不行就问我」——
    ntfy 通知老板定夺（可派 ZCode/dsh 或人工），父任务保持 WAITING。
    """
    out: list[dict] = []
    parents = conn.execute(
        "SELECT task_id, to_instance, title FROM tasks WHERE status='WAITING'"
    ).fetchall()
    for p in parents:
        p = dict(p)
        child = conn.execute(
            "SELECT * FROM tasks WHERE client_request_id=?",
            (f"dispatch-child:{p['task_id']}",)).fetchone()
        if child is None:
            continue
        c = dict(child)
        if c["status"] == "COMPLETED":
            report = _heci_report(p["title"], f"查到了：{c.get('result') or '（云端没给正文）'}")
            try:
                core.task_action(conn, p["task_id"], actor_instance=p["to_instance"],
                                 action="resume", note="子任务搜索完成，回填结果")
                core.task_action(conn, p["task_id"], actor_instance=p["to_instance"],
                                 action="result", result=report, note="中枢制回填")
                out.append({"task_id": p["task_id"], "report": report, "notify": False})
            except ValueError as exc:
                _record_failure(conn, p, f"回填失败：{exc}")
        elif c["status"] in ("FAILED", "REJECTED", "CANCELLED"):
            _record_failure(conn, p,
                            f"云端搜索未成（子任务 {c['status']}）——等你定：派 ZCode/dsh 或人工")
            out.append({"task_id": p["task_id"], "notify": True, "fallback": True})
    return out


def dispatch_tasks(conn, *, actor_note: str = "中枢制调度") -> dict:
    """扫 ACCEPTED 任务，按规则表执行结构化指令。可反复调用（自愈式重试）。"""
    backfilled = _backfill_waiting(conn)
    rows = conn.execute(
        "SELECT task_id, to_instance, risk, title, detail, context FROM tasks "
        "WHERE status='ACCEPTED' ORDER BY created_at").fetchall()
    done: list[dict] = []
    waiting: list[dict] = []
    skipped: list[dict] = []
    failures: list[dict] = []
    for t in rows:
        t = dict(t)
        tid = t["task_id"]
        try:
            ctx = json.loads(t.get("context") or "{}")
        except (TypeError, json.JSONDecodeError):
            ctx = {}
        if not isinstance(ctx, dict):
            ctx = {}
        action = str(ctx.get("action") or "").lower()
        out: dict = {}
        if not action:
            skipped.append({"task_id": tid,
                            "reason": "无结构化指令（自然语言任务 v1 不碰）"})
            continue

        try:
            if action == "wx_send":
                content = str(ctx.get("content") or "").strip()
                if not content:
                    raise ValueError("wx_send 缺 content")
                ap_id = str(ctx.get("approval_id") or "")
                if not ap_id:
                    # 审批自愈：开单 → 推手机（effect）→ 回写 context → 下轮重试
                    ap = core.create_approval(
                        conn, action="wx_send", target=content[:80],
                        risk="high", instance_id=t["to_instance"],
                        reason="任务要求对外发送微信消息")
                    _notify_via_effect(
                        conn,
                        f"⏸ 待批准 {ap['approval_id']}：wx_send {content[:60]}"
                        "（批准后任务自动继续）",
                        dedup_key=f"apv:{ap['approval_id']}")
                    ctx = dict(ctx)
                    ctx["approval_id"] = ap["approval_id"]
                    conn.execute("UPDATE tasks SET context=? WHERE task_id=?",
                                 (json.dumps(ctx, ensure_ascii=False), tid))
                    conn.commit()
                    waiting.append({"task_id": tid,
                                    "approval_id": ap["approval_id"],
                                    "note": "等老板批准，批准后下轮自动发送"})
                    continue
                core.consume_approval(conn, ap_id, action="wx_send",
                                      target=content[:80])
                # v2：出站走 effects 队列（写盘由处理器做，失败可重试）
                out = enqueue_effect(conn, kind="wx_outbox",
                                     payload={"content": content, "task_id": tid},
                                     dedup_key=f"wx:{tid}")
                summary = "微信消息已进发送队列，云端桥发出后你手机就能看到。"
            elif action == "file_write":
                path = str(ctx.get("path") or "").strip()
                content = str(ctx.get("content") or "")
                gate = risk_gate.classify("write", path=path)
                if gate["tier"] == risk_gate.TIER_HIGH:
                    raise ValueError(f"file_write 目标高危（{gate['reason']}），"
                                     "v1 不自动执行，请人工处理")
                out = core_fs.write_text(path, content)
                summary = f"文件已写好：{out['path']}（{out['bytes']} 字节）。"
            elif action == "remind":
                deliver_at = str(ctx.get("deliver_at") or "").strip()
                content = str(ctx.get("content") or t["title"])
                if not deliver_at:
                    raise ValueError("remind 缺 deliver_at（ISO8601）")
                # pending_actions 在**对话库**（storage），不在核心库——
                # conn 是核心库连接，必须另开 storage 连接（实测踩过）。
                _sc = storage.connect()
                try:
                    pending_id = storage.record_pending_action(
                        _sc, run_id=f"dispatch:{tid}", kind="scheduled_reminder",
                        message=content, reason=f"dispatch:{tid}",
                        deliver_at=deliver_at)
                    _sc.commit()
                finally:
                    _sc.close()
                summary = f"闹钟上好了，到点（{deliver_at[:16]}）她会喊你。"
                out = {"pending_id": pending_id}
            elif action == "web_search":
                # 搜索调度链（老板拍板：云端为主→不行→问老板）：
                # 父任务 → WAITING，子任务派 heci_001_muse（云端有完整网络）；
                # 云端 result 后由本模块的回填规则（见下）收口父任务。
                query = str(ctx.get("query") or t["detail"] or t["title"]).strip()
                child_id, created = core.create_task(
                    conn, from_instance=t["to_instance"], to_instance="heci_001_muse",
                    title=f"子任务·搜索：{query[:40]}", detail=query,
                    risk="low", max_rounds=1,
                    client_request_id=f"dispatch-child:{tid}")
                core.task_action(conn, tid, actor_instance=t["to_instance"],
                                 action="wait",
                                 note=f"等待云端搜索结果（子任务 {child_id}）")
                if created:
                    core.task_action(conn, child_id, actor_instance="heci_001_muse",
                                     action="receive",
                                     note="中枢制转发（云端为主搜索）")
                out = {"child_task_id": child_id}
                summary = f"已派云端搜索（子任务 {child_id}），结果回来自动汇报。"
                # 特例：本单 result 延后——父任务保持 WAITING，回填时再 COMPLETED
                done.append({"task_id": tid, "action": action,
                             "report": _heci_report(t["title"], summary),
                             "pending_id": None, "notify": False,
                             "deferred": True})
                continue
            else:
                skipped.append({"task_id": tid, "reason": f"未知 action={action}"})
                continue
        except Exception as exc:  # noqa: BLE001 —— 单单失败不拖垮整批
            err = f"{type(exc).__name__}: {exc}"
            _record_failure(conn, t, err)
            failures.append({"task_id": tid, "error": err, "recorded": True})
            continue

        # 鹤辞口吻 result 回任务；④ 降噪：只有对外类推手机
        report = _heci_report(t["title"], summary)
        try:
            core.task_action(conn, tid, actor_instance=t["to_instance"],
                             action="result", result=report, note=actor_note)
        except ValueError as exc:
            err = f"result 回写失败：{exc}"
            _record_failure(conn, t, err)
            failures.append({"task_id": tid, "error": err, "recorded": True})
            continue
        done.append({"task_id": tid, "action": action, "report": report,
                     "pending_id": out.get("pending_id"),
                     "notify": action in NOTIFY_ON_DONE})
    return {"scanned": len(rows), "done": done, "waiting": waiting,
            "skipped": skipped, "failures": failures, "backfilled": backfilled}
