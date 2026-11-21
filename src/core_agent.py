"""云端指令巡查执行器（core_agent）。

由 Windows 计划任务每 3 分钟调用一次：跑一轮、处理、退出。
**不做常驻进程** —— 一次性执行更抗崩溃，也不会占着会话。

## 它做什么

1. 拉 `to_instance=heci_001_dsh` 且状态为 SENT 的任务 → 自动 receive + accept
2. 按「可执行性」分流：
   - **结构化任务**（context.action 明确）→ 执行 → 交 result
   - **常见规则**（标题里能识别的动作）→ 执行 → 交 result
   - **其余**（自然语言、需判断）→ 只接单，不执行，写一条「待人工」事件
3. 每轮最多处理 5 个任务，避免一次跑太久

## 安全边界（硬约束）

- 只允许：建**新**文件、读文件、查状态、写事件
- 禁止：删除、覆盖已存在文件、`fs/exec`、动配置
- 单任务最多 3 个动作
- 每次执行都写 `dev_note` 留痕
- 环境变量 `HEXI_AGENT=0` 可一键停用

用法：
    python src\\core_agent.py            # 跑一轮
    python src\\core_agent.py --dry-run  # 只打印不执行
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import companion_core as core
from core_client import CoreClient, CoreError

ME = "heci_001_dsh"
MAX_TASKS = 5
MAX_OPS = 3


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        p = core.CORE_DIR.parent / "logs" / "core_agent.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def parse_context(t: dict) -> dict:
    ctx = t.get("context")
    if isinstance(ctx, dict):
        return ctx
    if isinstance(ctx, str) and ctx.strip():
        try:
            v = json.loads(ctx)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def plan_for(t: dict) -> list[dict] | None:
    """把任务翻译成可执行动作。返回 None = 需要人。"""
    ctx = parse_context(t)
    act = str(ctx.get("action") or "").strip()

    # ---- 结构化任务（首选）----
    if act == "write_new_file":
        path = str(ctx.get("path") or "").strip()
        content = str(ctx.get("content") or "")
        if not path:
            return None
        return [{"op": "write_new_file", "path": path, "content": content}]
    if act == "report_status":
        return [{"op": "report_status"}]

    # ---- 规则匹配（自然语言的常见模式）----
    text = f"{t.get('title') or ''}\n{t.get('detail') or ''}"
    if "回报" in text and ("状态" in text or "当前" in text):
        return [{"op": "report_status"}]
    return None


# ---------------------------------------------------------------------------
# 云端指令硬闸 + 派单点名（2026-11-15，老板拍板「可以」）
# ---------------------------------------------------------------------------

# 通道三清单（派单说明）升格为**硬闸模式表**：命中即建审批单推手机，批了才执行。
# 刻意偏保守——误命中只是多一张审批单（老板批一下就过），漏命中是真危险。
_DANGEROUS_PATTERNS = (
    ("删除数据", ("删除", "删掉", "清空", "清除", "drop ", "delete ", "rm ")),
    ("覆盖或改写已存在内容", ("覆盖", "替换", "改写", "overwrite")),
    ("动配置或密钥", (".env", "settings.json", "api-token", "改配置", "配置文件")),
    ("动数据库", ("数据库", "conversations.db", "companion_core.db", "sqlite")),
    ("改产品代码", ("src/", "改代码", "产品代码", "源代码")),
    ("重启或运维动作", ("重启", "restart", "shutdown", "关机")),
    ("不可逆动作", ("不可逆", "永久删除", "彻底", "格式化", "force")),
)


def dangerous_reason(t: dict) -> str | None:
    """危险指令检测（纯函数）。命中返回类别名，未命中返回 None。"""
    ctx = parse_context(t)
    act = str(ctx.get("action") or "").lower()
    text = f"{t.get('title') or ''}\n{t.get('detail') or ''}\n{act}".lower()
    for label, words in _DANGEROUS_PATTERNS:
        if any(w in text for w in words):
            return label
    return None


# 派单点名（target）：context.target = "dsh"（dsh-superagent 的 NL 通道）/
# "zcode"（老板会话中转）/ "core"（本巡查器）。点名给别人的，本巡查器
# **连单都不接**——状态留在 SENT，由被点名的执行者自己的巡查来领。
_TARGET_SELF = ("core", "core_agent", "any", "all", "")


def target_of(t: dict) -> str:
    ctx = parse_context(t)
    return str(ctx.get("target") or "").strip().lower()


def _latest_approval(conn, tid: str) -> dict | None:
    """该任务的最新审批单（target=task:<tid> 定位）。"""
    row = conn.execute(
        "SELECT * FROM approvals WHERE target=? ORDER BY created_at DESC LIMIT 1",
        (f"task:{tid}",)).fetchone()
    return dict(row) if row else None


def run_ops(c: CoreClient, t: dict, ops: list[dict], dry: bool) -> list[str]:
    """执行动作，返回每步的结果描述。"""
    out: list[str] = []
    for op in ops[:MAX_OPS]:
        kind = op["op"]
        try:
            if kind == "write_new_file":
                path = op["path"]
                # 安全闸：绝不覆盖已存在文件
                try:
                    c.read(path, max_bytes=10)
                    out.append(f"跳过 {path}：文件已存在（不覆盖）")
                    continue
                except CoreError:
                    pass
                if dry:
                    out.append(f"[dry] 会写 {path}（{len(op['content'])} 字）")
                    continue
                r = c.write_with_lease(path, op["content"], ME)
                out.append(f"已写 {path}（{r.get('bytes')} 字节）")
            elif kind == "report_status":
                try:
                    h = c.health()
                    # state() 返回 {"ok":true,"state":{...}}，要解一层，
                    # 否则 event_count 之类全是 None（第一版就踩了这个）。
                    st = (c.state() or {}).get("state") or {}
                    out.append(f"核心在线：{h.get('service')}；"
                               f"事件 {st.get('event_count')} 条；"
                               f"记忆 {st.get('memory_version')}；"
                               f"关系 {st.get('relationship_stage')}；"
                               f"待整理候选 {st.get('pending_candidates')}")
                except CoreError as e:
                    out.append(f"核心不可达：{e.status}")
            else:
                out.append(f"未知动作 {kind}（跳过）")
        except Exception as exc:  # noqa: BLE001
            out.append(f"{kind} 失败：{type(exc).__name__}: {str(exc)[:80]}")
    return out


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args

    if os.environ.get("HEXI_AGENT", "1") == "0":
        log("已停用（HEXI_AGENT=0）")
        return 0

    tok = (core.CORE_DIR / "api-token").read_text(encoding="utf-8").strip()
    c = CoreClient("http://127.0.0.1:8770", tok, timeout=25)

    # 【任务池】看池认领（2026-11-21，Muse 设计/老板拍板）：
    #   只自动认领 risk=low 的池单（与现有自动接单同口径，fail-closed）；
    #   认领后 to_instance 变本席位，下方 inbox 流程原样接管（硬闸/执行/释放）。
    #   409 = 被其他席位抢先，正常跳过；点名单照旧不碰。
    try:
        for pt in (c.pool_tasks().get("tasks") or []):
            if (pt.get("risk") or "").lower() != "low":
                continue
            _ptgt = target_of(pt)
            if _ptgt and _ptgt not in _TARGET_SELF:
                continue
            try:
                c.claim(pt["task_id"], ME)
                log(f"  池单 {pt['task_id']} 已认领：{(pt.get('title') or '')[:40]}")
            except CoreError as e:
                if e.status != 409:
                    log(f"  池单 {pt['task_id']} 认领失败：{e.status}")
    except CoreError as e:
        log(f"看池失败（跳过，不影响指定单）：{e.status}")

    try:
        tasks = (c.tasks(instance_id=ME, role="inbox").get("tasks") or [])
    except CoreError as e:
        log(f"拉任务失败：{e.status} {str(e.body)[:100]}")
        return 1

    # 2026-11-08 修：原来只认 SENT，导致「已经被接单、但没执行」的任务永远卡死。
    # 2026-11-11 再修：ACCEPTED 不能再进来 —— 当时的场景是 plan_for=None 的任务
    #   停在 ACCEPTED 且每轮写 result 抢闭环。
    # 2026-11-15 加：WAITING 也进来 —— 危险指令硬闸把任务挂 WAITING 等审批，
    #   批准后要能从这里恢复执行（否则审批完的任务永远冻结）。
    # 2026-11-21 再加回 ACCEPTED：**旧理由已失效**——①plan_for=None 的任务现在
    #   只写一条 dedup 的 dev_note（不再每轮写 result 抢闭环），每轮干净跳过；
    #   ②core_api 巡检（60s）会对显式 low 风险的任务自动接单 → ACCEPTED，
    #   若巡查器跳过 ACCEPTED，这些带结构化载荷的任务就**永远没人执行**
    #   （dispatch_task 上线首单实测卡死在这里）。需人工的判定交给 plan_for
    #   返回 None（留 ACCEPTED 等人），不再由状态一刀切。
    ACTIONABLE = ("SENT", "RECEIVED", "WAITING", "ACCEPTED")
    pending = [t for t in tasks if t.get("status") in ACTIONABLE]
    log(f"本轮：待处理 {len(pending)} 个"
        f"（SENT {sum(1 for t in pending if t.get('status') == 'SENT')} / "
        f"RECEIVED {sum(1 for t in pending if t.get('status') == 'RECEIVED')} / "
        f"WAITING {sum(1 for t in pending if t.get('status') == 'WAITING')}）")
    if not pending:
        return 0

    done = 0
    for t in pending[:MAX_TASKS]:
        tid = t["task_id"]
        title = (t.get("title") or "")[:40]
        st = t.get("status")

        # 【点名】target 给了别的执行者（dsh-superagent / zcode）→ 本巡查器
        # **连单都不接**，状态留在 SENT，由被点名者的巡查来领（防双跑撞车）。
        _tgt = target_of(t)
        if _tgt and _tgt not in _TARGET_SELF:
            log(f"  {tid} 点名给 {_tgt}，巡查器跳过（不接单）")
            continue

        # 按当前状态补齐缺失的接单动作（已做过的不重复）
        try:
            if st == "SENT":
                c.task_act(tid, instance_id=ME, action="receive")
                c.task_act(tid, instance_id=ME, action="accept")
            elif st == "RECEIVED":
                c.task_act(tid, instance_id=ME, action="accept")
        except CoreError as e:
            log(f"  {tid} 接单失败（{st}）：{e.status}")
            continue

        # 【硬闸】危险指令必须先过审批单（2026-11-15 软闸升硬闸）：
        #   无审批单 → 建单 + 推手机 + 任务挂 WAITING；
        #   pending  → 幂等跳过（等老板裁决）；
        #   denied   → 写 result 闭环（COMPLETED，结果注明被拒）；
        #   approved → resume 恢复，照常走 plan_for 执行。
        # 误命中只是多一张审批单（批一下就过），漏命中是真危险——模式表刻意偏保守。
        _danger = dangerous_reason(t)
        if _danger:
            _conn_ap = core.connect()
            try:
                _ap = _latest_approval(_conn_ap, tid)
            finally:
                _conn_ap.close()
            if _ap is None and not dry:
                _conn_ap = core.connect()
                try:
                    _ap = core.create_approval(
                        _conn_ap, action="agent_task_execute", target=f"task:{tid}",
                        risk="high", instance_id=ME,
                        reason=f"危险指令：{_danger}｜{(t.get('title') or '')[:60]}")
                finally:
                    _conn_ap.close()
                try:
                    c.task_act(tid, instance_id=ME, action="wait")   # ACCEPTED → WAITING
                    st = "WAITING"
                except CoreError as e:
                    log(f"  {tid} 挂 WAITING 失败：{e.status}")
            elif _ap is not None and _ap["status"] == "denied":
                if not dry:
                    c.task_act(tid, instance_id=ME, action="result",
                               result=f"【已拒绝】老板 deny（审批单 {_ap['approval_id']}）："
                                      f"{_danger}。任务闭环，未执行。")
                log(f"  {tid} 被拒绝（{_danger}），闭环")
                done += 1
                continue
            if _ap is None or _ap["status"] == "pending":
                try:
                    import heartbeat
                    topic = heartbeat.ntfy_topic_from_env()
                    if topic:
                        # T6（2026-11-21）：审批通知带「批准/拒绝」按钮——
                        # 只有真实审批单（_ap 存在）才带；dry 演练保持纯文本。
                        _actions = (heartbeat.build_approval_actions(
                            _ap["approval_id"]) if _ap else None)
                        heartbeat.deliver_to_ntfy(
                            topic, f"鹤辞：有危险指令待审批\n{t.get('title')}\n"
                                   f"类别：{_danger}\n（审批单 {_ap['approval_id'] if _ap else '（dry）'}，{tid}）",
                            actions=_actions)
                except Exception:  # noqa: BLE001
                    pass
                log(f"  {tid} 危险指令（{_danger}）：审批单 "
                    f"{_ap['approval_id'] if _ap else '（dry）'} 待裁决，任务挂 WAITING")
                done += 1
                continue
            if _ap["status"] == "approved" and st == "WAITING" and not dry:
                c.task_act(tid, instance_id=ME, action="resume")   # WAITING → RUNNING
                st = "RUNNING"
            log(f"  {tid} 危险指令已获批（{_danger}），照常执行")

        ops = plan_for(t)
        if ops is None:
            # 【任务池】本席位做不了的池单 → 释放回池换人接（Muse 设计 §二.3），
            # 不能赖在 ACCEPTED 挡别人的路。指定单维持原行为：留 ACCEPTED 等人工。
            if (t.get("claimed_by") or "") == ME and not dry:
                try:
                    c.release(tid, ME, "本席位无此任务的执行能力（plan_for=None），释放回池")
                    log(f"  {tid} 池单释放回池（做不了，换人接）")
                except CoreError as e:
                    log(f"  {tid} 释放失败：{e.status}")
                done += 1
                continue
            # 2026-11-11 修：**不再写 result**。
            # 原来写「已接单，未执行」→ 状态变 COMPLETED，而状态机没有 reopen，
            # 人工事后分析完就无法回填（task_ab7f9c6715ca 真实踩到）。
            # 现在只 accept，让任务停在 ACCEPTED；并写一条事件 + 推手机提醒人。
            if not dry:
                try:
                    conn = core.connect()
                    try:
                        core.append_event(
                            conn, instance_id=ME, event_type="dev_note",
                            content=(f"【需人工】任务 {tid} 已接单，巡查器不自动执行。\n"
                                     f"标题：{t.get('title')}\n"
                                     f"任务停在 ACCEPTED，等人处理后可写 result 闭环。"),
                            source_environment="dsh",
                            task_id=tid,
                            provenance="core_agent/needs-human",
                            dedup_key=f"needs-human:{tid}")
                    finally:
                        conn.close()
                except Exception as exc:  # noqa: BLE001
                    log(f"     写提醒事件失败：{type(exc).__name__}")
                try:
                    import heartbeat
                    topic = heartbeat.ntfy_topic_from_env()
                    if topic:
                        heartbeat.deliver_to_ntfy(
                            topic, f"鹤辞：有需人工的任务\n{t.get('title')}\n（{tid}）")
                except Exception:  # noqa: BLE001
                    pass
            log(f"  {tid} 接单，留 ACCEPTED 待人处理（{title}）")
            done += 1
            continue

        lines = run_ops(c, t, ops, dry)
        log(f"  {tid} 执行 {len(lines)} 步：{title}")
        for ln in lines:
            log(f"     - {ln}")
        if not dry:
            try:
                c.task_act(tid, instance_id=ME, action="result",
                           result="【自动巡查执行】\n" + "\n".join(f"- {x}" for x in lines))
            except CoreError as e:
                log(f"     result 失败：{e.status}")
        done += 1

    log(f"本轮完成 {done} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())