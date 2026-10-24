"""心跳与主动性（Phase 8）。

对应《project-spec》§8「Agency / 主动性」、§9「Heartbeat / Timer」、§29「Phase 8 Agency」。

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
from datetime import datetime, timedelta

import storage

DECISIONS = storage.HEARTBEAT_DECISIONS

DECISION_LABELS = {
    "no_action": "不行动",
    "wait": "等待",
    "proactive_message": "主动消息",
    "update_state": "更新内部状态",
}


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

def tick(conn, settings, *, now: datetime | None = None, dry_run: bool = False) -> dict:
    """跑一次心跳检查：收集状态 → 决策 → 落盘。

    `dry_run=True` 时只算不写，方便查看「如果现在检查，会怎么决定」。

    **注意**：本函数不调用模型，不花钱。
    """
    rules = settings.heartbeat or {}
    if not rules:
        raise RuntimeError("settings.json 里没有 heartbeat 规则，无法执行心跳")

    context = collect_context(conn, settings, now=now)
    result = decide(context, rules)

    if dry_run:
        return {**result, "context": context, "run_id": None, "pending_id": None, "dry_run": True}

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
            "dry_run": False}


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
