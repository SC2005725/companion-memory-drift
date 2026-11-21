"""心跳与主动性（Phase 8）。

对应《总项目指令》§8「Agency / 主动性」、§9「Heartbeat / Timer」、§29「Phase 8 Agency」。

## 三句话先说清这个模块的定位

1. **Heartbeat 不是 Companion 本身。** §9：它只提供「时间流逝」和「检查机会」。
2. **主动行动 ≠ 每隔几分钟发消息。** §8 的原话。所以规则默认结论是**不行动**。
3. **「选择不行动」本身也是 Agency。** §29 的原话。所以 `no_action` 也会落盘留痕。

## 投递通道：从「没有」到「可选」

**2026-10-28 之前**：本项目是命令行聊天，**没有投递通道** —— `proactive_message`
只是一条「主动意图」，写进 `pending_actions`，等你下次开口时注入。

**2026-10-28 起**：加了 **ntfy 可选投递**（`deliver_to_ntfy`）。行为变成：

    判定 proactive_message
      → ① 先写 pending_actions（**留痕在前**，投递失败也不丢意图）
      → ② 再看环境变量 `NTFY_TOPIC` 有没有值
           · 没值 → 到此为止，与旧行为完全一致
           · 有值 → 顺带推一条到手机（失败只记日志，绝不影响心跳）

**默认就是关的** —— 不设 `NTFY_TOPIC` 就等于旧行为。所以这不是「默认开始推送」。

**仍然不假装的事**：投递成功只代表 ntfy.sh 收下了，**不代表手机收到**。
「手机真的弹出」只能由人确认，代码无法自证。

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
    # 定时承诺（scheduled_reminder）不算「主动意图积压」——它们是承诺过的
    # 送达，到点自有 reminder 通道兑现；计入积压会让规则 2 被提醒误触发，
    # 用户有几个未响的提醒，鹤辞就永远不再主动说话（2026-11-07，探针实测）。
    pending = [p for p in storage.list_pending_actions(conn, status="pending")
               if p["kind"] != "scheduled_reminder"]

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


# 关怀模板池（独立体检第 2 条修复，2026-11-15）：确定性轮换，零模型调用。
# 语料刻意保持「她的口吻」：简短、不腻、不问两个问题——与人格卡的表达约束一致。
_CARE_TEMPLATES = (
    "想起之前我们一起弄成的那件事了，你那边后来还顺利吗？",
    "上次一起把事情搞定的感觉还挺怀念的。你最近还好吗？",
    "翻记忆的时候看到我们一块儿想办法的那段了。你这边一切都还顺？",
    "想起咱们从头到尾一起磨完一件事的时候了。最近有什么新进展吗？",
    "之前那段一起解决问题的日子挺难得的。你最近过得怎么样？",
)


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
        # 独立体检第 2 条（2026-11-15）：关怀推送**模板轮换**。
        # 旧版硬编码一句话 → 8 天 11 次推送全是同一句（dsh 11-04 预警的
        # 「复读」应验）。轮换 = 确定性（日期×6h 桶取模），**零模型调用**，
        # 5 条模板 30 小时一循环——相邻两次推送不再是同一句。
        from datetime import datetime as _dt

        idx = (_dt.now().toordinal() * 4 + _dt.now().hour // 6) % len(_CARE_TEMPLATES)
        return {
            "kind": "relationship_care",
            "reason": f"关系已到「{rel.get('stage')}」且有过共同经历，可以主动关心一下",
            "message": _CARE_TEMPLATES[idx],
        }

    return None


# ----------------------------------------------------------------------------
# ntfy 手机投递（2026-10-28 加）
#
# 为什么单独成函数、而且和 `tick` 解耦：
# **投递是可失败的旁路，不该混进决策主干。** 心跳的职责是「判定 + 留痕」；
# 能不能推出去是另一件事。所以这里所有异常都吞掉，`tick` 拿到的返回不受影响。
#
# 为什么用环境变量而不是 settings.json：
# 只改一个文件就够了（用户授权范围就是只动 heartbeat.py）。
# 而且 topic 属于**本机凭据**，放环境变量不必进版本库。
# ----------------------------------------------------------------------------

NTFY_ENV_KEY = "NTFY_TOPIC"        # 环境变量名（.env 里的同名键也会被 load_env 读进来）
NTFY_URL_BASE = "https://ntfy.sh"
NTFY_TIMEOUT_S = 10.0              # 够慢网络用，又不至于让心跳卡住
# P1（ntfy 4KB 护栏，2026-11-08）：ntfy 单条 message 上限 4096 字符，超长推送
# 永远失败 → digest 里那条 pending 永远推不出也永远不划掉 → 主动推送整体冻结。
# 护栏 = 发送前截断留余量；全文仍在 pending_actions / 任务账里可查。
NTFY_MAX_MESSAGE_CHARS = 3800


def ntfy_topic_from_env() -> str:
    """从环境变量取 topic。**空串 = 不投递** —— 这就是开关，不需要额外配置项。"""
    import os

    return (os.environ.get(NTFY_ENV_KEY) or "").strip()


def build_approval_actions(approval_id: str, title: str = "") -> list:
    """T6（2026-11-21）：构造审批通知的「批准/拒绝」按钮（ntfy actions 格式）。

    纯函数，零网络零 IO —— 离线可单测。
    安全面：载荷里只有 approval_id + decision 标记，**没有任何凭据**——
    令牌永远只存在手机 APP 里，裁决时由 APP 自己带 Cookie 调 webui。
    用 broadcast 型动作：桌面端 ntfy 订阅端（NtfyService 是我们自己的代码）
    自行解析并渲染按钮，不依赖 ntfy 官方 App 的语义。
    """
    aid = str(approval_id or "").strip()
    if not aid:
        return []
    base = {"action": "broadcast",
            "extras": {"gongsheng_cmd": "approval_decide", "approval_id": aid}}
    a1 = dict(base); a1["label"] = "批准"; a1["extras"] = dict(base["extras"], decision="approve")
    a2 = dict(base); a2["label"] = "拒绝"; a2["extras"] = dict(base["extras"], decision="deny")
    return [a1, a2]


def deliver_to_ntfy(topic: str, message: str, *, actions=None) -> bool:
    """把一条主动消息推到 ntfy。**任何异常都不会抛出去。**

    三条纪律：
      1. **不阻断心跳** —— 投递失败只是少一条通知，`tick` 必须照常落盘与返回
      2. **不重试** —— 一次失败就丢。断网时重试会变成风暴，而这条消息
         本来就已经写进 `pending_actions` 了，不会真的丢
      3. **不泄漏 topic** —— 日志只记成功/失败与状态码，**不记 topic 本身**，
         异常信息也只记类型名

    actions（T6，2026-11-21）：非空时改走 ntfy **JSON 发布通道**（POST 到根路径，
    body 为 JSON），把按钮数组随消息一起发；订阅端（APK NtfyService）解析
    actions 渲染「批准/拒绝」按钮。普通文本路径行为完全不变。

    返回值只用于留痕，调用方不必关心。
    """
    if not topic or not message:
        return False
    if len(message) > NTFY_MAX_MESSAGE_CHARS:
        # P1（4KB 护栏）：截断发送，保证「能推出去、能划掉」，不再永久卡 pending。
        message = (message[:NTFY_MAX_MESSAGE_CHARS].rstrip()
                   + "\n…（超长已截断：ntfy 单条上限；全文在本地 pending_actions 可查）")
    try:
        import urllib.request
        from urllib.parse import quote

        if actions:
            # JSON 发布通道：按钮必须走这里（文本通道无法携带结构化动作）。
            # 与文本通道同样的三条纪律；标题在 body 里，UTF-8 中文无碍。
            body = json.dumps({"topic": topic, "message": message,
                               "title": "Companion", "actions": list(actions)},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{NTFY_URL_BASE}/",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            req = urllib.request.Request(
                f"{NTFY_URL_BASE}/{quote(topic, safe='')}",
                # ★ 显式 UTF-8 字节 —— 实测过：PowerShell 那条路会把中文发成乱码，
                #   Python 这条不会。别改成 str 直传。
                data=message.encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    # 头只用 ASCII：HTTP 头按 latin-1 编码，中文标题会出错。
                    # 中文正文放 body，那里是 UTF-8，没有问题。
                    "Title": "Companion",
                    "Priority": "default",
                },
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT_S) as resp:
            ok = 200 <= resp.status < 300
            storage.log_line(f"heartbeat ntfy: ok={ok} status={resp.status} "
                             f"actions={len(actions) if actions else 0}")
            return ok
    except Exception as exc:                       # noqa: BLE001
        # 可能是 URLError / HTTPError / 代理 / 证书 / DNS —— 类型不可穷举，
        # 所以捕 Exception。**这里绝不能再抛**，否则会打断心跳。
        storage.log_line(
            f"heartbeat ntfy: FAILED {type(exc).__name__}（已忽略，不影响心跳）"
        )
        return False


# ----------------------------------------------------------------------------
# 执行一次心跳
# ----------------------------------------------------------------------------

def tick(conn, settings, *, now: datetime | None = None, dry_run: bool = False,
         trigger: str = TRIGGER_MANUAL, suppress_new_intent: bool = False) -> dict:
    """跑一次心跳检查：收集状态 → 决策 → 落盘。

    `dry_run=True` 时只算不写，方便查看「如果现在检查，会怎么决定」。

    `trigger` 记录**这次检查是被谁触发的**（manual / chat / schedule）——
    只为可追溯，不改变任何判定逻辑（`decide` 是纯函数，不看 trigger）。

    `suppress_new_intent=True`（2026-11-07，HB-0）：已有未兑现的主动意图时，
    本轮检查**照常执行与留痕**，但不生成新的主动意图 —— 若 decide 判出
    proactive_message，就地降级为 wait（action=pending_deferred）。
    「不重复生成」的语义由这一参数承担，`maybe_tick` 不再为此整体短路。

    **注意**：本函数不调用模型，不花钱。
    落盘时会显式记下 `model_called: False`，让"这次到底花没花钱"可事后核对。
    """
    rules = settings.heartbeat or {}
    if not rules:
        raise RuntimeError("settings.json 里没有 heartbeat 规则，无法执行心跳")

    trigger = trigger if trigger in TRIGGERS else TRIGGER_MANUAL

    # —— 定时承诺兑现（2026-11-07，外部调研 #7）——
    # kind=scheduled_reminder 且 deliver_at 到点的条目，**独立于 decide() 兑现**：
    # 提醒是用户/鹤辞自己约的（"明早8点叫我"），到点就送，免打扰时段也不拦。
    # ntfy 未配置 → 留在 pending，等 chat 注入兑现（render_for_prompt）。
    # 全程 try/except：提醒失败绝不打断心跳主干（与 ntfy 旁路同纪律）。
    # **必须先于 collect_context**：兑现会改变 pending 计数，旧顺序让 decide
    # 拿到过期计数（雷 2/3 的完整修法，2026-11-07 探针实测）。
    if not dry_run:
        try:
            now_dt = now or datetime.now(timezone.utc)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            due = storage.list_due_reminders(
                conn, now_dt.astimezone(timezone.utc).isoformat())
            for r in due:
                _rtopic = ntfy_topic_from_env()
                if not _rtopic:
                    break  # 无投递通道 → 留给 chat 注入兑现
                if deliver_to_ntfy(_rtopic, f"⏰ {r['message']}"):
                    storage.mark_pending_pushed(conn, r["pending_id"])
                    storage.log_line(
                        f"heartbeat reminder: {r['pending_id']} 已兑现 → pushed")
        except Exception as exc:  # noqa: BLE001
            storage.log_line(
                f"heartbeat reminder: 兑现失败（不影响心跳）{type(exc).__name__}: {exc}")

        # —— 提醒缓存同步（2026-11-07，云端工单二：断机期兜底的本地半边）——
        # 把未兑现的定时承诺（kind=scheduled_reminder，deliver_at 在 48h 窗口内）
        # 全量写成根目录 reminders.json。云端 cron 读它做「主机关机期间的兜底送信」；
        # 主机在线时云端让路（先探 health），本地仍是主通道。每次 tick 全量覆盖。
        # 云端纪律见 docs/云端工单-两件-20261107.md 工单二：只送提醒、幂等、不闲聊。
        try:
            from datetime import timedelta as _td
            from paths import ROOT as _root

            _cutoff = (datetime.now(timezone.utc) + _td(hours=48)).isoformat()
            _rows = storage.list_pending_reminders_window(conn, cutoff=_cutoff)
            _payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window_hours": 48,
                "reminders": [
                    {"pending_id": r["pending_id"], "deliver_at": r["deliver_at"],
                     "message": r["message"], "kind": r["kind"]}
                    for r in _rows
                ],
            }
            (_root / "reminders.json").write_text(
                json.dumps(_payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            storage.log_line(
                f"heartbeat reminders-sync: 失败（不影响心跳）{type(exc).__name__}: {exc}")

    # 上下文收集放在兑现**之后**：decide 拿到的是兑现后的真实计数
    context = collect_context(conn, settings, now=now)
    result = decide(context, rules)

    # —— 压制重复生成（2026-11-07，HB-0）——
    # 放在 decide 之后、落盘之前：decide 是**纯函数**（不查库），不改它；
    # 降级只影响「这条决策怎么落盘」，判定链路的可重放性不受影响。
    # wait 是既有决策值（storage.HEARTBEAT_DECISIONS），**不动枚举与表结构**。
    if suppress_new_intent and result["decision"] == "proactive_message":
        result = {**result,
                  "decision": "wait",
                  "reason": (f"已有 {context.get('pending_count') or 0} 条未兑现主动意图，"
                             f"本轮不重复生成（原意图先兑现再说）"),
                  "action": "pending_deferred",
                  "message": ""}

    if dry_run:
        return {**result, "context": context, "run_id": None, "pending_id": None,
                "dry_run": True, "trigger": trigger}

    # —— B（2026-11-21，老板批准）：主动消息**扩写** ——
    # 判定（decide）保持零模型（规则便宜、可复现）；唯一一次调用花在
    # 「决定要说了」之后的扩写上：把规则合成的短意图扩成有内容的详细消息。
    # 事件驱动低频（每天最多几次），费用可忽略；走 call_model_standalone
    # （统一记账出口，Phase 11 审计结论），费用不漏记。
    # 闸门：① NTFY_TOPIC 已配置（推送消费方在）② rules.proactive_expand 未显式关。
    # 失败/未启用 → 如实退回规则短消息（不阻塞链路、不假装发过）。
    expanded = False
    if (not dry_run and result["decision"] == "proactive_message"
            and (result["message"] or "").strip()
            and ntfy_topic_from_env()
            and rules.get("proactive_expand", True)):
        try:
            from chat import call_model_standalone
            from llm import LLMClient
            from config import load_env, load_settings as _load_settings
            _env = load_env()
            _set = _load_settings()
            _client = LLMClient(api_key=_env.api_key, base_url=_env.base_url,
                                model=_set.model, temperature=0.7, max_tokens=400)
            _sys = ("你是鹤辞。此刻你要主动给对方发一条消息。"
                    "用你的口吻写 2~4 句自然口语：可以围绕给出的由头展开一点点，"
                    "不要列表、不要解释自己为什么发消息、不要宣称内部状态、"
                    "不要连续问两个问题；总长不超过 120 字。直接输出消息本身，不要引号。")
            _usr = (f"由头：{result['reason']}（{result['action']}）\n"
                    f"想说的核心：{result['message']}\n"
                    f"关系阶段：{(context.get('relationship') or {}).get('stage')}\n"
                    f"上次对话：{str(context.get('last_user_text') or '')[:60]}")
            reply_x = call_model_standalone(
                _client,
                [{"role": "system", "content": _sys},
                 {"role": "user", "content": _usr}],
                settings=_set, conn=conn, purpose="proactive_expand")
            _text = (reply_x["reply"].text or "").strip()
            if _text:
                result["message"] = _text
                result["expanded"] = True
                expanded = True
        except Exception as exc:  # noqa: BLE001 —— 扩写失败如实退回短消息
            storage.log_line(f"heartbeat proactive 扩写失败（退回短消息）: "
                             f"{type(exc).__name__}: {str(exc)[:100]}")

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
            # 2026-11-21 修订（老板批准）：主动消息扩写启用后，这里如实记
            "model_called": expanded,
            "expanded": expanded,
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
        # —— 已经决定要说了，顺带推一条到手机 ——
        #
        # **顺序是有意的**：先 `record_pending_action`（留痕），再投递。
        # 这样即使投递失败，这条意图也已经落进 `pending_actions`，不会丢。
        # 反过来（先投递再落盘）在投递失败时会留下"发过但没记录"的不一致。
        #
        # `NTFY_TOPIC` 没设 → 直接跳过，与旧行为完全一致。
        _topic = ntfy_topic_from_env()
        if _topic:
            # 2026-11-04：推成功了就把这条从「欠着没说」里划掉（status=pushed）。
            # 不这么做的话：定时心跳每次都会看到一条 pending，然后直接退出，
            # 「主动」就只响一次。详见 storage.mark_pending_pushed 的说明。
            # 注意：推送失败**不划掉**，保证下次还能再试一次，意图不丢。
            #
            # 2026-11-07（外部调研 #6）：digest 合并——已有多条没说出口的意图时，
            # 合并成**一次**推送（移动推送限流 + 防打扰双重要求），全部划掉。
            # 2026-11-07（雷 2 修复）：digest 只合并 proactive 意图——
            # scheduled_reminder 有自己的到点兑现通道（tick 开头的 reminder 块），
            # 混进 digest 会被提前推走并标 pushed，到点永不兑现。
            all_pending = [p for p in storage.list_pending_actions(conn, status="pending")
                           if p["kind"] != "scheduled_reminder"]
            if len(all_pending) > 1:
                merged = "；\n".join(dict.fromkeys(
                    (p["message"] or "").strip() for p in all_pending if p["message"]))
                if deliver_to_ntfy(_topic, merged):
                    for p in all_pending:
                        storage.mark_pending_pushed(conn, p["pending_id"])
                    storage.log_line(
                        f"heartbeat ntfy: digest 合并推送 {len(all_pending)} 条 → 全部 pushed")
                else:
                    storage.log_line("heartbeat ntfy: digest 推送失败，pending 保留待重试")
            elif deliver_to_ntfy(_topic, result["message"]):
                storage.mark_pending_pushed(conn, pending_id)
                storage.log_line(
                    f"heartbeat ntfy: {pending_id} 已推送 → status=pushed"
                )

        # —— 她主动开口（2026-11-21，老板要求）：把这条消息**物化成真实对话轮次** ——
        # 此前"主动"只推通知 + 攒进卡片、等用户开口才注入 —— 手机上她永远不会
        # 先说话。现在：写进最近使用的会话（assistant-only 轮次，接在哈希链上），
        # 手机打开鹤辞就能在对话里看到她主动说的话；pending 同时标记 delivered
        # （已说出，下次开口不再重复注入）。零模型调用：话由规则合成，不新增成本。
        try:
            _last = storage.last_turn(conn)
            if _last is not None and (result["message"] or "").strip():
                # seq 用 MAX(seq)+1（确定性、防撞）——last_turn 按 created_at 排序，
                # 时间戳异常时返回的可能不是最大 seq，直接 +1 会撞车。
                _next_seq = int(conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS m FROM conversations"
                ).fetchone()[0]) + 1
                _turn = storage.save_turn(
                    conn,
                    session_id=_last["session_id"],
                    seq=_next_seq,
                    user_text="",
                    assistant_text=result["message"],
                    model=(_last["model"] or ""),
                    persona_name=(_last["persona_name"] or ""),
                    persona_sha256=(_last["persona_sha256"] or ""),
                )
                if pending_id:
                    storage.mark_pending_delivered(conn, pending_id,
                                                   _turn.get("turn_id"))
                storage.log_line(
                    f"heartbeat proactive 已物化为对话轮次 {_turn.get('turn_id')} "
                    f"(session {_last['session_id']})")
        except Exception as exc:  # noqa: BLE001 —— 物化失败不影响推送与意图留痕
            storage.log_line(f"heartbeat proactive 物化失败: "
                             f"{type(exc).__name__}: {exc}")
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
TICK_PENDING_ACTION = "pending_action"      # 已有待处理项：检查照常执行并留痕（executed=True），但不生成新的主动意图
TICK_EXECUTED = "executed"                  # 真的跑了一次 tick（内含 result）
TICK_CONFIG_ERROR = "configuration_error"   # 配置有问题（心跳规则缺失等）
TICK_RUNTIME_ERROR = "runtime_error"        # 其他运行时异常（含异常类型，可追踪）
TICK_STATUSES = (TICK_THROTTLED, TICK_PENDING_ACTION, TICK_EXECUTED,
                 TICK_CONFIG_ERROR, TICK_RUNTIME_ERROR)


def maybe_tick(session, *, now: datetime | None = None,
               trigger: str = TRIGGER_CHAT, dry_run: bool = False) -> dict:
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
    | `pending_action` | 已有待处理主动意图：检查照常执行并留痕，但**不生成新的**（2026-11-07 HB-0 起，`executed=True`） | `pending_count`, `result` |
    | `executed` | 真的跑了一次 | `result`（tick 的完整返回） |
    | `configuration_error` | 心跳规则缺失/格式不可用 | `error` |
    | `runtime_error` | 其他异常 | `error`（含异常类型） |

    ## 2026-11-07 修复（HB-0）：pending 不再短路整次心跳

    **事故**：旧实现把「已有 pending」短路放在节流**之前**——只要存在 1 条未兑现
    pending 就直接 return，**一条 heartbeat_runs 都不写**。后果：
    pending 未兑现期间「上次检查时间」冻结（节流失真、监控全盲）；
    schedule 触发启用后，会被一条卡住的 pending **永久挡死**。

    **修法**：节流先行；pending 存在时照常 `tick` 并留痕，把「不重复生成」
    下沉为 `tick(suppress_new_intent=True)` —— 决策降级为 wait
    （action=pending_deferred），不再整体装死。回归测试：`selftest_companion.py` [P8.11c]。

    ## 兼容性

    `chat.py` 的调用点只把它存进返回字典（`"heartbeat": heartbeat_result`），
    **没有任何调用者判断 `is None`**，所以改返回类型不影响现有流程。
    `chat.maybe_heartbeat_tick` 的类型标注已同步为 `dict`。

    ## 失败绝不阻断聊天

    一切异常都被接住并转成 `runtime_error`，仍然不抛给聊天；
    但**不再静默** —— `status` 与 `error` 都带出去，并写一行日志。

    ## `dry_run=True`：只算不写（2026-10-26 新增）

    **为什么加**：`selftest.py` 的端到端自测会调 `chat.send_once()`，
    而 `send_once` 会调本函数。结果是**跑一次状态生成就往真实库写进
    `heartbeat_runs` / `pending_actions`** —— 污染了它本该测量的数据。

    在此之前 `dry_run` 只存在于底层的 `tick()`，而 `maybe_tick` 没把它透传出来，
    所以「经过节流的那条路径」**无法只算不写**。

    现在透传：`dry_run=True` 时判定与节流照常走，但**不落盘、不提交**，
    返回值的 `dry_run` 为 `True`、且不含 `result`。

    **默认 `False`** —— 生产行为**一字未变**。
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
        # ② 节流先行（2026-11-07，HB-0 换位）：距**上次心跳检查**太近 → 跳过。
        #    「上次检查」只由 heartbeat_runs 定义，与有没有 pending 无关 ——
        #    否则 pending 未兑现期间 last-run 冻结，节流与监控全部失真。
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

        # ③ 已有待处理 → 检查**照常执行并留痕**，但压制新的主动意图。
        #    （2026-11-07 修，HB-0）：旧实现把这条短路放在节流**之前**，只要存在
        #    1 条未兑现 pending 就直接 return —— pending 期间一条 heartbeat_runs
        #    都不写（上次检查时间冻结、监控全盲），schedule 触发上线后更会被一条
        #    卡住的 pending **永久挡死**。「不重复生成」的语义保留，但下沉为
        #    tick(suppress_new_intent=True)：决策降级为 wait 并留痕，不再整体装死。
        #    （pending 的正常兑现出口：chat 注入→delivered（chat.py）、
        #     ntfy 推送成功→pushed（tick 内 mark_pending_pushed）。）
        pending_rows = storage.list_pending_actions(session.conn, status="pending")
        result = tick(session.conn, session.settings, now=now, trigger=trigger,
                      dry_run=dry_run, suppress_new_intent=bool(pending_rows))
        if not dry_run:
            session.conn.commit()
        return {"status": TICK_PENDING_ACTION if pending_rows else TICK_EXECUTED,
                "executed": True, "trigger": trigger, "dry_run": dry_run,
                "pending_count": len(pending_rows),
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
