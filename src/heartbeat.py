"""心跳与主动性（Phase 8）。

对应《总项目指令》§8「Agency / 主动性」、§9「Heartbeat / Timer」、§29「Phase 8 Agency」。

## 三句话先说清这个模块的定位

1. **Heartbeat 不是 Companion 本身。** §9：它只提供「时间流逝」和「检查机会」。
2. **主动行动 ≠ 每隔几分钟发消息。** §8 的原话。所以规则默认结论是**不行动**。
3. **「选择不行动」本身也是 Agency。** §29 的原话。所以 `no_action` 也会落盘留痕。

## 一个必须如实说明的限制

本项目是**命令行聊天，没有投递通道** —— 小满无法主动给用户发消息。

所以 `proactive_message` 在实现上是一条**「主动意图」**，写进 `pending_actions`，
等用户下次开口时注入上下文。她可以说「上次我想起你提过…」，
但**不会有凭空弹出来的消息**。

**我不假装能做到做不到的事。** 真要做到推送，得先有投递渠道（Phase 9 的 Tool 层或外部 API）。

## 决策流程（§8）

    Heartbeat
        ↓
    收集当前状态（关系 / 记忆 / 用户模型 / 上次对话时间 / 待处理项）
        ↓
    按确定性规则判定
        ↓
    决定：不行动 / 等待 / 主动消息 / 更新内部状态

**为什么用确定性规则而不是让模型决定**：与记忆、关系、用户模型同理 ——
「她为什么选择说话 / 不说话」必须可复现、可解释、可审计。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import storage

DECISIONS = storage.HEARTBEAT_DECISIONS

DECISION_LABELS = {
    "no_action": "不行动",
    "wait": "等待",
    "proactive_message": "主动消息",
    "update_state": "更新内部状态",
}


# ---------------------------------------------------------------------------
# Phase B：触发来源
# ---------------------------------------------------------------------------
#
# 心跳只有三种"被叫醒"的方式，全部是**外部驱动**（本模块不自建后台任务）：
#
#   manual    CLI：`python src\main.py heartbeat`
#   chat      用户开口时顺带检查（chat.send_once 调用 maybe_tick）
#   schedule  预留：未来定时器。**本阶段不实现，也没有任何代码会产生它。**
#
# trigger 只用于留痕，**不参与判定** —— `decide()` 是纯函数，不看它。
#
# ⚠️ 这几个常量必须在 `tick()` **之前**定义：`tick` 用 `TRIGGER_MANUAL`
# 作默认参数，而默认参数在 def 时就求值（放到后面会 NameError）。
TRIGGER_MANUAL = "manual"
TRIGGER_CHAT = "chat"
TRIGGER_SCHEDULE = "schedule"      # ← 预留位，当前无生产者
TRIGGERS = (TRIGGER_MANUAL, TRIGGER_CHAT, TRIGGER_SCHEDULE)

# 距上次检查的最小间隔（分钟）。同一触发源在间隔内重复触发会被跳过。
#
# 实测教训：初版用「距上次对话不足 1 小时就跳过」做节流 ——
# 但正常聊天间隔通常小于 1 小时，结果是心跳**永远不会跑**，
# 连一行运行记录都看不到，等于把功能藏起来了。改成按"上次检查"去重。
MIN_GAP_MINUTES = 5.0


# ----------------------------------------------------------------------------
# 上下文收集（全部复用现成模块，不重复实现）
# ----------------------------------------------------------------------------

def collect_context(conn, settings, *, now: datetime | None = None) -> dict:
    """收集一次心跳检查需要的全部状态。

    刻意把「读取状态」和「做决定」分开：前者可被多处复用，后者是纯函数。
    """
    import relationship as relationship_mod
    import user_model as user_model_mod

    now = now or datetime.now().astimezone()

    # 只看**真实聊天会话**的最后一条。
    #
    # 踩过的坑：回忆探针（`recall` 命令）也是走 send_once 落盘的，所以它同样在
    # conversations 表里。若不排除，心跳会去跟进一句探针的自动回答
    # （实测抓到「我一时没对上，也不想假装记得…」这种），非常怪。
    # 判据：探针会话不出现在真实聊天里 —— 但两者都在同一张表，所以改用
    # 「排除探针用的会话」：探针跑在一次性会话里，其 session_id 只出现 1 次且
    # 没有对应的用户后续交互，不可靠。
    #
    # 因此改用更直接的办法：**探针轮次的问句是固定的探针问题**，按它排除。
    import probe as probe_mod

    probe_questions = {probe_mod.DEFAULT_QUESTION}

    rows = conn.execute(
        "SELECT session_id, created_at_local, user_text, assistant_text FROM conversations "
        "ORDER BY seq DESC LIMIT 50"
    ).fetchall()
    last = None
    for r in rows:
        if (r["user_text"] or "").strip() in probe_questions:
            continue
        last = r
        break

    last_at = None
    idle_hours = None
    if last:
        try:
            last_at = datetime.fromisoformat(last["created_at_local"])
            idle_hours = (now - last_at).total_seconds() / 3600
        except (TypeError, ValueError):
            last_at = None

    rel_state = relationship_mod.current_state(conn, settings.relationship or {})
    um_items = user_model_mod.load_items()
    um_active = user_model_mod.active_items(um_items)
    pending = storage.list_pending_actions(conn, status="pending")

    last_hb = storage.list_heartbeat_runs(conn, limit=1)
    last_proactive_at = None
    for row in storage.list_heartbeat_runs(conn, limit=50):
        if row["decision"] == "proactive_message":
            last_proactive_at = row["created_at_local"]
            break

    return {
        "now": now.isoformat(timespec="seconds"),
        "now_dt": now,
        "last_turn_at": last_at.isoformat(timespec="seconds") if last_at else None,
        "last_user_text": last["user_text"] if last else "",
        "last_assistant_text": last["assistant_text"] if last else "",
        "idle_hours": round(idle_hours, 2) if idle_hours is not None else None,
        "conversation_count": conn.execute(
            "SELECT COUNT(*) AS n FROM conversations"
        ).fetchone()["n"],
        "relationship": rel_state,
        "user_model_active": len(um_active),
        "memory_versions": conn.execute(
            "SELECT COUNT(*) AS n FROM memory_versions"
        ).fetchone()["n"],
        "pending_count": len(pending),
        "pending": [
            {"pending_id": p["pending_id"], "message": p["message"], "reason": p["reason"]}
            for p in pending
        ],
        "last_proactive_at": last_proactive_at,
        "last_heartbeat_at": last_hb[0]["created_at_local"] if last_hb else None,
    }


# ----------------------------------------------------------------------------
# 决策（纯函数，可重放）
# ----------------------------------------------------------------------------

def _in_quiet_hours(now: datetime, ranges: list) -> bool:
    """是否处于免打扰时段。支持跨午夜区间（如 23:00-07:00）。"""
    t = now.strftime("%H:%M")
    for pair in ranges or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = pair
        if start <= end:
            if start <= t < end:
                return True
        else:  # 跨午夜
            if t >= start or t < end:
                return True
    return False


def decide(context: dict, rules: dict) -> dict:
    """根据状态决定做什么。**纯函数** —— 同样的输入永远得到同样的输出。

    返回 {decision, reason, action, message}。

    判定顺序（先排除，再考虑主动）：
        1. 免打扰时段          → wait（夜里不打扰，但不是「没想法」）
        2. 待处理项过多        → no_action（主动 ≠ 刷屏）
        3. 最近主动过（冷却中） → no_action
        4. 空闲够久 + 有值得跟进的话题 → proactive_message
        5. 空闲够久但没什么可说 → no_action（**这也是 Agency**）
        6. 刚聊完              → wait
    """
    now = context["now_dt"]
    idle = context.get("idle_hours")
    idle_threshold = float(rules.get("idle_hours_for_proactive", 12) or 12)
    cooldown = float(rules.get("cooldown_hours", 6) or 6)
    max_pending = int(rules.get("max_pending_before_silence", 3) or 3)

    # 1) 免打扰
    if _in_quiet_hours(now, rules.get("quiet_hours")):
        return {
            "decision": "wait",
            "reason": f"当前 {now.strftime('%H:%M')} 处于免打扰时段，不打扰用户",
            "action": "quiet_hours",
            "message": "",
        }

    # 2) 待处理项已积压 —— 先让用户看到已有的，不再新增
    if context["pending_count"] >= max_pending:
        return {
            "decision": "no_action",
            "reason": f"已有 {context['pending_count']} 条待处理主动意图，先不新增（主动不等于刷屏）",
            "action": "pending_backlog",
            "message": "",
        }

    # 3) 冷却：距上次主动消息太近
    last_proactive = context.get("last_proactive_at")
    if last_proactive:
        try:
            gap = (now - datetime.fromisoformat(last_proactive)).total_seconds() / 3600
            if gap < cooldown:
                return {
                    "decision": "no_action",
                    "reason": f"距上次主动消息仅 {gap:.1f} 小时，未到冷却时间 {cooldown} 小时",
                    "action": "cooldown",
                    "message": "",
                }
        except (TypeError, ValueError):
            pass

    # 4) 还没聊过任何东西 —— 无事可做
    if not context.get("last_turn_at"):
        return {
            "decision": "no_action",
            "reason": "还没有任何对话记录，没有可跟进的事项",
            "action": "no_history",
            "message": "",
        }

    # 5) 空闲时间不够 —— 等待
    if idle is None or idle < idle_threshold:
        shown = f"{idle:.1f}" if idle is not None else "未知"
        return {
            "decision": "wait",
            "reason": f"距上次对话 {shown} 小时，未到主动阈值 {idle_threshold} 小时",
            "action": "not_idle_enough",
            "message": "",
        }

    # 6) 空闲够久：看有没有值得跟进的话题
    candidate = pick_followup(context, rules)
    if candidate:
        return {
            "decision": "proactive_message",
            "reason": candidate["reason"],
            "action": candidate["kind"],
            "message": candidate["message"],
        }

    # 7) 空闲够久但确实没什么可说 —— **这也是 Agency**
    return {
        "decision": "no_action",
        "reason": f"已空闲 {idle:.1f} 小时，但没有值得跟进的事项（选择不打扰也是主动性）",
        "action": "nothing_worth_saying",
        "message": "",
    }


def pick_followup(context: dict, rules: dict) -> dict | None:
    """挑一个值得跟进的话题。**优先复用已有的关系/用户模型/记忆数据**，不另造。

    候选来源（按优先级）：
        1. 上次对话结束在问句上 → 跟进那个问题
        2. 关系进入「信任/亲近」且有共同经历 → 关心一下
        3. 待处理项里已有内容 → 不重复挑（由调用方处理）
    """
    last_assistant = (context.get("last_assistant_text") or "").strip()
    kws = rules.get("question_keywords") or []
    if last_assistant and any(k in last_assistant for k in kws):
        # 从她上一句里截一段作为跟进点，不额外调模型
        snippet = last_assistant.replace("\n", " ")[:40]
        return {
            "kind": "followup_question",
            "reason": "上次对话停在一个问题上，适合跟进",
            "message": f"上次你还没回我：{snippet}",
        }

    rel = context.get("relationship") or {}
    if rel.get("stage") in ("信任", "亲近") and rel.get("counts", {}).get("shared_experience"):
        return {
            "kind": "relationship_care",
            "reason": f"关系已到「{rel.get('stage')}」且有过共同经历，可以主动关心一下",
            "message": "想起之前我们一起弄成的那件事了，你那边后来还顺利吗？",
        }

    return None


# ----------------------------------------------------------------------------
# 执行一次心跳
# ----------------------------------------------------------------------------

def tick(conn, settings, *, now: datetime | None = None, dry_run: bool = False,
         trigger: str = TRIGGER_MANUAL) -> dict:
    """跑一次心跳检查：收集状态 → 决策 → 落盘。

    `dry_run=True` 时只算不写，方便查看「如果现在检查，会怎么决定」。

    `trigger` 记录**这次检查是被谁触发的**（manual / chat / schedule）——
    只为可追溯，不改变任何判定逻辑（`decide` 是纯函数，不看 trigger）。

    **注意**：本函数不调用模型，不花钱。
    落盘时会显式记下 `model_called: False`，让"这次到底花没花钱"可事后核对。
    """
    rules = settings.heartbeat or {}
    if not rules:
        raise RuntimeError("settings.json 里没有 heartbeat 规则，无法执行心跳")

    trigger = trigger if trigger in TRIGGERS else TRIGGER_MANUAL
    context = collect_context(conn, settings, now=now)
    result = decide(context, rules)

    if dry_run:
        return {**result, "context": context, "run_id": None, "pending_id": None,
                "dry_run": True, "trigger": trigger}

    run_id = storage.record_heartbeat_run(
        conn,
        decision=result["decision"],
        reason=result["reason"],
        action=result["action"],
        message=result["message"],
        context={
            "idle_hours": context.get("idle_hours"),
            "pending_count": context.get("pending_count"),
            "stage": (context.get("relationship") or {}).get("stage"),
            "user_model_active": context.get("user_model_active"),
            # Phase B 起新增（放在既有的 JSON context 列里，**不动表结构**）：
            "trigger": trigger,          # 谁触发的：manual / chat / schedule
            "model_called": False,       # 本阶段心跳**全程不调模型**，显式留痕
        },
    )

    pending_id = None
    if result["decision"] == "proactive_message":
        pending_id = storage.record_pending_action(
            conn,
            run_id=run_id,
            kind=result["action"],
            message=result["message"],
            reason=result["reason"],
        )
        storage.log_line(
            f"heartbeat proactive: {pending_id} kind={result['action']} reason={result['reason']}"
        )
    else:
        storage.log_line(
            f"heartbeat {result['decision']}: {result['reason']}"
        )

    return {**result, "context": context, "run_id": run_id, "pending_id": pending_id,
            "dry_run": False, "trigger": trigger}


# ---------------------------------------------------------------------------
# Phase B：扩展点（**只预留，不实现**）
# ---------------------------------------------------------------------------
#
# ## Phase C 预留：模型适配器挂钩
#
# Phase C 会建立统一模型适配接口（本地 / 免费 / 现有客户端三种）。
# 本阶段**只留位置**，明确三件事：
#
#   1. 心跳当前**全程不调模型** —— 注册了适配器也不会被调用；
#   2. 接口形状固定为 `available() -> bool` 与 `generate(prompt) -> str`；
#   3. `maybe_tick` 用 try/except 包住一切，**适配器失败不得影响心跳与聊天**。
#
# 不实现任何适配器，不接入任何模型服务。
MODEL_ADAPTER = None


def set_model_adapter(adapter) -> None:
    """注册模型适配器（Phase C 用）。本阶段不会调用它。

    `adapter` 需实现：
        available() -> bool         当前是否可用（免费额度耗尽、超时都应返回 False）
        generate(prompt: str) -> str

    传入 `None` 可注销。**心跳在任何情况下都不因适配器失败而中断。**
    """
    global MODEL_ADAPTER
    MODEL_ADAPTER = adapter


def enable_model_adapter(router=None) -> dict:
    """Phase C 对接：把**适配层路由**接到 Phase B 的扩展点。

    ⚠️ **必须显式调用才会生效。** 模块导入、心跳运行都不会自动调用它 ——
    这正是"默认不执行任何模型调用"的保证。

    返回一份自述（包含可用性），方便在 CLI 里确认接的是哪一层。
    """
    import model_adapter as ma
    adapter = ma.as_heartbeat_adapter(router)
    set_model_adapter(adapter)
    return {"attached": True, "adapter": adapter.name,
            "available": adapter.available()}


# —— maybe_tick 的结果状态（Phase D.2）——
#
# 修复前：节流、配置错误、运行时异常**全部压成同一个 `None`**，
# 调用方无法区分「没到时候」和「坏了」——
# 实测症状：`settings.heartbeat` 缺失时心跳永远不跑，界面上看不出来。
#
# 现在每种原因一个状态，**配置错误不再伪装成节流**。
TICK_THROTTLED = "throttled"                # 距上次检查太近，本次不跑
TICK_PENDING_ACTION = "pending_action"      # 已有待处理项，先兑现、不重复生成
TICK_EXECUTED = "executed"                  # 真的跑了一次 tick（内含 result）
TICK_CONFIG_ERROR = "configuration_error"   # 配置有问题（心跳规则缺失等）
TICK_RUNTIME_ERROR = "runtime_error"        # 其他运行时异常（含异常类型，可追踪）
TICK_STATUSES = (TICK_THROTTLED, TICK_PENDING_ACTION, TICK_EXECUTED,
                 TICK_CONFIG_ERROR, TICK_RUNTIME_ERROR)


def maybe_tick(session, *, now: datetime | None = None,
               trigger: str = TRIGGER_CHAT) -> dict:
    """**节流版触发**：满足间隔才真跑。

    此前这段策略写在 `chat.py` 里（`maybe_heartbeat_tick`）——
    那等于"心跳的触发策略住在聊天模块"。Phase B 把它移回心跳自己这里，
    让心跳对自己的触发负责。**判定逻辑一行没动**（仍走 `tick` → `decide`）。

    ## 返回：**总是 dict**（Phase D.2 改）

    原实现返回 `dict | None`，把三种完全不同的情况压成同一个 `None`。
    现在返回 `{status, executed, trigger, ...}`，`status` ∈ `TICK_STATUSES`：

    | status | 含义 | 额外字段 |
    |---|---|---|
    | `throttled` | 距上次检查不到 `MIN_GAP_MINUTES` | `gap_minutes` |
    | `pending_action` | 已有待处理主动意图，先兑现 | `pending_count` |
    | `executed` | 真的跑了一次 | `result`（tick 的完整返回） |
    | `configuration_error` | 心跳规则缺失/格式不可用 | `error` |
    | `runtime_error` | 其他异常 | `error`（含异常类型） |

    ## 兼容性

    `chat.py` 的调用点只把它存进返回字典（`"heartbeat": heartbeat_result`），
    **没有任何调用者判断 `is None`**，所以改返回类型不影响现有流程。
    `chat.maybe_heartbeat_tick` 的类型标注已同步为 `dict`。

    ## 失败绝不阻断聊天

    一切异常都被接住并转成 `runtime_error`，仍然不抛给聊天；
    但**不再静默** —— `status` 与 `error` 都带出去，并写一行日志。
    """
    import storage
    trigger = trigger if trigger in TRIGGERS else TRIGGER_CHAT

    # ① 配置先自查 —— 让「配置错误」与「节流」在**产生点**就分开，
    #    而不是靠事后匹配异常文案
    rules = getattr(session.settings, "heartbeat", None) or {}
    if not rules:
        msg = "settings.heartbeat 缺失或为空，心跳无法执行"
        try:
            storage.log_line(f"heartbeat CONFIG ERROR: {msg}")
        except Exception:  # noqa: BLE001
            pass
        return {"status": TICK_CONFIG_ERROR, "executed": False, "trigger": trigger,
                "error": msg}

    try:
        # ② 已有待处理 → 先让它兑现，不重复生成
        if storage.list_pending_actions(session.conn, status="pending"):
            n = len(storage.list_pending_actions(session.conn, status="pending"))
            return {"status": TICK_PENDING_ACTION, "executed": False,
                    "trigger": trigger, "pending_count": n}

        # ③ 距**上次心跳检查**太近 → 跳过
        last = session.conn.execute(
            "SELECT created_at_utc FROM heartbeat_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if last is not None:
            raw = last["created_at_utc"] if hasattr(last, "keys") else last[0]
            last_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            now_dt = now or datetime.now(timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            gap_min = (now_dt - last_dt).total_seconds() / 60
            if gap_min < MIN_GAP_MINUTES:
                return {"status": TICK_THROTTLED, "executed": False,
                        "trigger": trigger, "gap_minutes": round(gap_min, 2)}
        else:
            gap_min = None

        result = tick(session.conn, session.settings, now=now, trigger=trigger)
        session.conn.commit()
        return {"status": TICK_EXECUTED, "executed": True, "trigger": trigger,
                "gap_minutes": None if gap_min is None else round(gap_min, 2),
                "result": result}

    except Exception as exc:  # noqa: BLE001 — 心跳失败不得影响聊天
        msg = f"{type(exc).__name__}: {exc}"
        try:
            storage.log_line(f"heartbeat RUNTIME ERROR（不影响聊天）: {msg}")
        except Exception:  # noqa: BLE001
            pass
        return {"status": TICK_RUNTIME_ERROR, "executed": False, "trigger": trigger,
                "error": msg}


def render_for_prompt(pending: list) -> str:
    """把待处理的主动意图拼成注入上下文的一段。

    刻意说明「这是你想说但还没说的」—— 让模型知道这是它自己的意图，
    而不是用户刚说的话，避免它误以为用户已经问过。
    """
    if not pending:
        return ""
    lines = ["【你之前想主动说的话（还没机会说出口）】"]
    for p in pending:
        lines.append(f"- {p['message']}")
        lines.append(f"  （当时因为：{p['reason']}）")
    lines.append("如果现在合适，可以用自然的方式提起；不合适就继续聊眼前的，不必勉强。")
    return "\n".join(lines)


def format_decision(result: dict) -> str:
    label = DECISION_LABELS.get(result["decision"], result["decision"])
    lines = [
        f"决定        ：{label}（{result['decision']}）",
        f"依据        ：{result['reason']}",
        f"动作标签    ：{result.get('action') or '—'}",
    ]
    if result.get("message"):
        lines.append(f"主动意图    ：{result['message']}")
    if result.get("pending_id"):
        lines.append(f"待处理项    ：{result['pending_id']}（等下次用户开口时注入）")
    ctx = result.get("context") or {}
    if ctx:
        lines.append("")
        lines.append("当时的状态：")
        lines.append(f"  距上次对话 ：{ctx.get('idle_hours')} 小时")
        lines.append(f"  对话总数   ：{ctx.get('conversation_count')}")
        lines.append(f"  关系阶段   ：{(ctx.get('relationship') or {}).get('stage')}")
        lines.append(f"  用户模型   ：{ctx.get('user_model_active')} 条生效")
        lines.append(f"  记忆版本   ：{ctx.get('memory_versions')} 个")
        lines.append(f"  待处理项   ：{ctx.get('pending_count')} 条")
    return "\n".join(lines)
