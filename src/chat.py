"""对话主循环（Phase 1 + 2 + 3 装配）。

一轮的完整流程：
    用户输入
      → 拼装 system prompt（人格）
      → 带上最近 N 轮历史
      → 调用 LLM
      → 记账（token / 估算费用）
      → 原样落盘这次往返
      → 必要时生成原始事件（只追加）

刻意不做：记忆检索、长期记忆、Memory V1/V2/V3、Changelog。这些属于 Phase 4 以后。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass

import events as events_mod
import pricing
import storage
from config import Settings
from llm import LLMClient, LLMError
from persona import Persona, assert_persona_unchanged

HELP_TEXT = """可用命令：
  /help        显示本帮助
  /stats       当前落盘统计（对话 / 事件 / 调用 / 记忆版本 / 关系事件）
  /memory      查看当前记忆版本与条目
  /retrieval   查看最近一次检索命中了什么
  /relationship 查看关系状态与最近事件
  /user        查看「她眼中的用户」（用户模型）
  /tools       查看可用工具与权限
  /cost        费用账本汇总
  /verify      校验原始数据、记忆版本是否被改动
  /exit        退出（也可用 /quit 或 Ctrl+C）

其他任何输入都会原样发给模型，并把这一往返完整落盘。

注意：每轮都会做一次记忆检索并落盘（即使 0 命中），也会判定关系事件。
这是刻意设计 —— 只有记录下来，日后才能区分「记忆漂移」和「检索失败」，
也才能解释「关系状态为什么变成这样」。"""


@dataclass
class Session:
    session_id: str
    persona: Persona
    settings: Settings
    client: LLMClient
    peak_override: bool | None
    conn: object
    tools: object = None      # Phase 9：ToolRegistry，None = 本轮不接工具
    router: object = None     # Phase 10：ModelRouter，None = 不路由（用客户端默认模型）
    # Memory V1：自动整理计数。**挂在会话上而不是全局** ——
    # 这样隔离副本/自测各跑各的，不会互相影响触发节奏。
    turns_since_consolidate: int = 0


def new_session(*, persona: Persona, settings: Settings, client: LLMClient, peak_override,
                conn, tools=None, router=None) -> Session:
    """建一个会话。

    `tools` 传 None 就不挂工具层 —— 探针与自测走这条路，
    保证「记忆观测」不被工具调用干扰（工具会额外消耗 token 与轮次）。

    `router` 传 None 就不做模型路由 —— 同样为探针与自测保留，
    免得自测因为路由而换了模型，掩盖真实行为。
    """
    if router is None:
        # 默认按配置装配。这样所有现有调用点（chat / webui / 命令行）
        # 什么都不用改就获得路由能力。
        import model_router as router_mod

        router = router_mod.build_router(settings, fallback_model=client.model)
    return Session(
        session_id=f"sess_{uuid.uuid4().hex[:10]}",
        persona=persona,
        settings=settings,
        client=client,
        peak_override=peak_override,
        conn=conn,
        tools=tools,
        router=router,
    )


def run_tool_loop(session, messages, *, turn_id, seq, called_first, purpose="chat"):
    """执行「模型要工具 → 我们跑 → 把结果回灌 → 模型接着说」的循环。

    返回 (最后一轮的 called 结果, 最终给用户看的文本, 去掉了工具调用的原始文本,
          执行过的调用列表, 迭代次数)。

    ## 为什么循环上限必须有

    模型可以一直要工具，形成死循环（跑一次、看一眼、再跑一次…）。
    `MAX_ITERATIONS` 是硬上限，到顶就停并如实说明 —— 不静默截断。

    ## 为什么工具结果用 user 角色回灌

    这是在**复用同一个对话流**，不引入新的消息角色。
    好处：换任何 OpenAI 兼容端点都还能用，Core 不被绑死（§10）。
    """
    import tools as tools_mod

    cfg = session.settings.tools or {}
    max_iter = int(cfg.get("max_iterations") or tools_mod.MAX_ITERATIONS)

    called = called_first
    raw = called["reply"].text or ""
    calls = tools_mod.parse_calls(raw)
    executed: list[dict] = []
    iterations = 0
    total_cost = called["cost_micro_yuan"]
    seen: set[str] = set()          # 完全相同的调用签名，用于拦住原地重试

    while calls and iterations < max_iter:
        iterations += 1
        messages.append({"role": "assistant", "content": raw})

        observations = []
        for call in calls:
            signature = _call_signature(call)
            if signature in seen:
                # 同一轮里把**完全相同**的调用再发一次，重试必然同样失败。
                #
                # 实测事故：模型发空参数被拒后，原样重发同一个空参数，
                # 第二次又被拒就放弃了 —— 用户要记的笔记没进系统。
                # 与其再执行一次注定相同的失败，不如直接告诉它「这样重试没用」。
                observations.append(
                    f"【{call['name']} 的结果】\n"
                    f"[denied] 这次调用与上一次**完全相同**，重试不会有不同结果。\n"
                    f"请改参数后重发，或直接告诉用户你做不到。"
                )
                executed.append({"name": call["name"], "arguments": call["arguments"],
                                 "ok": False, "error_kind": "denied", "repeat": True})
                storage.record_tool_call(
                    session.conn, name=call["name"], args=call["arguments"], ok=False,
                    error="与上一次调用完全相同，已拦截", error_kind="denied",
                    turn_id=turn_id, seq=seq, iterations=iterations,
                )
                continue
            seen.add(signature)
            result = session.tools.call(
                session.conn, call["name"], call["arguments"],
                turn_id=turn_id, seq=seq, iterations=iterations,
            )
            executed.append({"name": call["name"], "arguments": call["arguments"],
                             "ok": result.ok, "error_kind": result.error_kind})
            observations.append(
                f"【{call['name']} 的结果】\n{result.render()}"
            )
        messages.append({
            "role": "user",
            "content": "（工具执行结果）\n\n" + "\n\n".join(observations)
                       + "\n\n请据此继续回答。"
                         "**如果结果里让你「改参数后重发」，就带上正确参数重发；"
                         "如果是权限或能力不允许，就直接如实告诉用户做不到，不要反复重试。**"
                         "不需要工具就直接说话。",
        })

        called = call_model(session, messages, purpose=purpose, turn_id=turn_id)
        total_cost += called["cost_micro_yuan"]
        raw = called["reply"].text or ""
        calls = tools_mod.parse_calls(raw)

    truncated = bool(calls) and iterations >= max_iter
    final_text = tools_mod.strip_calls(raw)
    if truncated:
        # 说**真实**次数，不许虚报。
        # 实测事故：模型在最终回答里说「试了三次」，而账本只有 2 次调用 ——
        # 它自己编了迭代次数。这里由代码给出确切数字。
        final_text = (final_text
                      + f"\n\n（我连续试了 {iterations} 次工具仍未成功，先停下。）").strip()
    elif iterations and not any(e.get("ok") for e in executed):
        final_text = (final_text
                      + f"\n\n（工具没成功，以上是我能说的。）").strip()

    called = {**called, "cost_micro_yuan": total_cost}
    return called, final_text, raw, executed, iterations, truncated


def _call_signature(call: dict) -> str:
    """一次调用的稳定指纹，用于判断「是不是原样重试」。

    参数按键排序后再序列化，所以 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 视为同一次。
    """
    import json as _json

    try:
        args = _json.dumps(call.get("arguments") or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(call.get("arguments"))
    return f"{call.get('name')}|{args}"


def build_messages(
    session: Session,
    user_text: str,
    *,
    memory_text: str = "",
    relationship_text: str = "",
    user_model_text: str = "",
    self_model_text: str = "",
    pending_text: str = "",
    tools_text: str = "",
    expression_text: str = "",
    inject_history: bool = True,
    user_image: str | None = None,
) -> list[dict]:
    """组装一次调用的 messages。

    人格每次都重新拼装，保证 system prompt 恒定且来源唯一。

    注入顺序（都是独立的 system 段，便于事后核对「这一轮到底给模型看了什么」）：
        0. 表达调整（由当前状态推出，**不改性格**；无状态时不注入）
        1. 人格
        2. 记忆（Phase 5）
        3. 关系状态（Phase 6）
        4. 用户模型（Phase 7）—— 小满眼中的用户
        5. 自我模型（V1.1）—— 小满自己的做法（小满可自改）
        6. 未兑现的主动意图（Phase 8）

    `inject_history=False` 是回忆探针模式：只给人格 + 记忆/关系，不带任何原始对话。
    """
    messages = [{"role": "system", "content": session.persona.render_system_prompt()}]
    # Stage 2：初始角色模型（身份/人生史/欲望/自我认知）——
    # 紧跟在稳定底色之后、表达调整之前。
    # **它也是静态的**（生成一次后不变），所以不放进"表达调整"那段。
    try:
        import persona as persona_mod
        profile_text = persona_mod.render_profile(persona_mod.load_profile())
    except Exception:  # noqa: BLE001 — 没有初始模型时不影响聊天
        profile_text = ""
    if profile_text:
        messages.append({"role": "system", "content": profile_text})
    # 表达调整放在人格**之后**：先立住性格，再说明"这一轮可以怎么调整表达"。
    # 顺序有实质意义 —— 反过来会让状态读起来像在覆盖性格。
    if expression_text:
        messages.append({"role": "system", "content": expression_text})
    if memory_text:
        messages.append({"role": "system", "content": memory_text})
    if relationship_text:
        messages.append({"role": "system", "content": relationship_text})
    if user_model_text:
        messages.append({"role": "system", "content": user_model_text})
    # 自我模型紧跟用户模型 —— 两个模型并列：上一个是「小满眼中的用户」，
    # 这一个是「小满自己的做法」。放在一起便于模型对照，但内容互不替代。
    if self_model_text:
        messages.append({"role": "system", "content": self_model_text})
    if pending_text:
        messages.append({"role": "system", "content": pending_text})
    if tools_text:
        messages.append({"role": "system", "content": tools_text})
    if inject_history:
        # 只带**本会话**的历史。跨会话混合会让「上下文里有什么」不可控，
        # 而上下文内容会直接改变模型回答，会污染记忆漂移的归因。
        for item in storage.recent_turns(
            session.conn, session.settings.history_turns, session_id=session.session_id
        ):
            messages.append({"role": "user", "content": item["user"]})
            if item["assistant"]:
                messages.append({"role": "assistant", "content": item["assistant"]})
    # 用户这一轮的消息。带图时用**多模态 content**（列表），否则仍是纯字符串 ——
    # 不带图的路径**逐字不变**，所以原有行为零影响。
    #
    # ⚠️ 图放在**文字之前**：模型先看到图再看问题，比反过来更少出现"没看图"。
    if user_image:
        try:
            import images as images_mod
            _uri = images_mod.as_data_uri(user_image)
            _parts = [{"type": "image_url", "image_url": {"url": _uri}}]
            if (user_text or "").strip():
                _parts.append({"type": "text", "text": user_text})
            else:
                _parts.append({"type": "text",
                               "text": "（我发了张图，没打字）"})
            messages.append({"role": "user", "content": _parts})
        except Exception as exc:  # noqa: BLE001 — 图读不出来不能让对话挂掉
            # 降级：退回纯文字，并**明确告诉她发生了什么** ——
            # 否则她会以为用户什么都没发，而用户以为发出去了。
            storage.log_line(f"image attach FAILED（降级为纯文字）: {exc}")
            messages.append({"role": "user",
                             "content": (user_text or "")
                             + "\n（我本来想发张图，但没读出来）"})
    else:
        messages.append({"role": "user", "content": user_text})
    return messages


def call_model(
    session: Session,
    messages: list[dict],
    *,
    purpose: str = "chat",
    turn_id: str | None = None,
) -> dict:
    """调用模型并记账。**所有** LLM 调用都应走这里，否则费用会漏记。

    Phase 4 复盘发现：`doctor --online` 自己 new 了 LLMClient 直接 send，
    绕过了这一层，导致那次真实调用既没进 api_calls 表也没进 ledger.jsonl，
    使 `cost` 汇总系统性偏低。此处把记账集中到一处，供 chat / consolidate /
    回忆探针共用，避免同类漏记再次发生。

    `purpose` 用于区分调用类型（chat / consolidate / probe），
    同时决定**用哪个模型**（Phase 10 路由，见 src/model_router.py）。
    """
    # Phase 10：按用途选模型。路由是纯函数，决策可复现、可追踪。
    # 没有 router 时（老调用方/自测）退回客户端自带模型 —— 行为与加路由前一致。
    decision = None
    if getattr(session, "router", None) is not None:
        decision = session.router.resolve(purpose)
        model_override = decision["model"]
    else:
        model_override = None

    reply = session.client.send(messages, model=model_override)
    if decision is not None:
        storage.log_line(
            f"router: purpose={purpose} → {decision['model']} "
            f"({decision['source']}: {decision['reason']})"
        )

    peak = pricing.is_peak(override=session.peak_override)
    cache_hit = reply.usage.cache_hit_tokens
    # 推理模型的思考 token 也计入输出计费，这里如实记录，不做任何折算。
    reasoning = _extract_reasoning_tokens(reply.usage.raw)
    cost_micro = pricing.estimate_micro_yuan(
        cost_cfg=session.settings.cost,
        model=reply.model,
        prompt_tokens=reply.usage.prompt_tokens,
        completion_tokens=reply.usage.completion_tokens,
        cache_hit_tokens=cache_hit,
        peak=peak,
    )

    call_id = f"call_{uuid.uuid4().hex[:12]}"
    finish_reason = getattr(reply, "finish_reason", None)
    retried = bool(getattr(reply, "retried_for_empty", False))
    if retried:
        # 空回复重试是罕见但重要的事件：它意味着「思考吃光了预算」。
        # 记进运行日志，便于事后统计这种现象在实验期间出现得多频繁。
        storage.log_line(
            f"empty-reply retry: purpose={purpose} finish_reason={finish_reason} "
            f"attempts={getattr(reply, 'attempts', '?')} "
            f"tokens={reply.usage.prompt_tokens}+{reply.usage.completion_tokens}"
        )
    storage.record_api_call(
        session.conn,
        call_id=call_id,
        session_id=session.session_id,
        turn_id=turn_id,
        model=reply.model,
        prompt_tokens=reply.usage.prompt_tokens,
        completion_tokens=reply.usage.completion_tokens,
        cache_hit_tokens=cache_hit,
        peak=peak,
        cost_micro_yuan=cost_micro,
        latency_ms=reply.latency_ms,
    )
    pricing.append_ledger(
        {
            "call_id": call_id,
            "at": storage.now_local(),
            "session_id": session.session_id,
            "turn_id": turn_id,
            "purpose": purpose,
            "model": reply.model,
            "prompt_tokens": reply.usage.prompt_tokens,
            "completion_tokens": reply.usage.completion_tokens,
            "cache_hit_tokens": cache_hit,
            "reasoning_tokens": reasoning,
            "peak": peak,
            "cost_micro_yuan": cost_micro,
            "latency_ms": reply.latency_ms,
            "finish_reason": finish_reason,
            "attempts": getattr(reply, "attempts", 1),
            "retried_for_empty": retried,
        }
    )
    return {"reply": reply, "peak": peak, "cost_micro_yuan": cost_micro, "call_id": call_id}


def call_model_standalone(
    client: LLMClient,
    messages: list[dict],
    *,
    settings: Settings,
    conn,
    purpose: str = "chat",
) -> dict:
    """不建完整 Session 的一次性 LLM 调用 —— **只为统一记账出口**。

    ## 为什么需要它（Phase 11 审计的结论）
    全仓库的记账都由 `call_model` 负责，而它需要 `Session`。
    但**自检场景没有 Session**（`doctor --online` 与网页界面的「测试 Key」），
    于是那两处各自 `LLMClient(...)` 后直接 `client.send(...)` ——
    **真实调用既不进 api_calls 表、也不进 ledger.jsonl**，使 `cost` 系统性偏低。

    本函数只是把它们接回既有出口：**复用 `call_model` 的全部记账与计费逻辑，
    不复制一行**。它不新建类、不新建文件、不新增依赖。

    ## 为什么 router 显式关掉（**不自动选模**）
    `new_session(router=None)` 会**按配置自动装配真实 router**
    （见 `new_session` 的注释）。那样自检就可能被路由到另一个模型，
    既改变了自检行为，也违背「不加入自动选模」的要求。

    所以这里传一个 **`enabled=False`** 的 router：`resolve()` 会返回
    `router_disabled` + 客户端自带模型 ——
    行为与"没有路由"完全一致，**且决策仍进运行日志**（可追踪，不静默）。

    ## 与 `call_model` 的差异（只有这些）
    - `persona=None`（`call_model` 不用它，自检也没有人格）
    - `tools=None`、无历史注入（自检只发一条最小请求）
    - `turn_id=None`：自检不属于任何一轮对话（`api_calls.turn_id` 本就允许为空）

    **异常不捕获** —— `LLMError` 原样上抛，调用方的异常语义不变。
    """
    import model_router as router_mod

    session = new_session(
        persona=None,
        settings=settings,
        client=client,
        peak_override=None,
        conn=conn,
        tools=None,
        # 显式关掉路由：返回客户端自带模型，决策记为 router_disabled
        router=router_mod.ModelRouter({"enabled": False}, fallback=client.model),
    )
    return call_model(session, messages, purpose=purpose, turn_id=None)


def _extract_reasoning_tokens(raw) -> int | None:
    """取推理模型的思考 token 数，取不到就返回 None。

    deepseek-flash 是推理模型：思考 token 按**输出价**计费却不出现在回复文本里，
    单独记一笔，便于日后核对成本为何高于「可见文本量」的估算。
    """
    if not isinstance(raw, dict):
        return None
    details = raw.get("completion_tokens_details")
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if isinstance(value, int):
            return value
    value = raw.get("reasoning_tokens")
    return value if isinstance(value, int) else None


def send_once(
    session: Session,
    user_text: str,
    *,
    echo=lambda *_: None,
    inject_memory: bool = True,
    inject_history: bool = True,
    memory_version: str | None = None,
    full_memory_version: bool = False,
    use_relationship: bool = True,
    user_image: str | None = None,
) -> dict:
    """执行一次完整往返。返回本轮落盘结果摘要。

    Phase 5 起，每轮都会做一次记忆检索并**落盘**（即使 0 命中），
    这样日后才能区分「记忆真的漂移了」和「这一轮没检索到」。

    `full_memory_version=True` 用于**回忆探针**：把整个记忆版本原样注入，
    不做关键词筛选。原因见 `memory.render_full_version` —— 探针问题刻意不点名事件，
    靠关键词检索必然 0 命中，那样探针就测不到记忆了。
    """
    # 运行期守护：人格文件被改动就立刻停下，而不是静默继续。
    assert_persona_unchanged(session.persona.source_sha256)

    import retrieve as retrieve_mod

    # 先占 seq 与 turn_id，检索记录才能挂到本轮上。注意此刻本轮还没落盘，
    # 所以检索查的是「本轮之前」的历史 —— 符合直觉，也不会自己检索到自己。
    seq = storage.next_seq(session.conn)
    turn_id = storage.new_turn_id()

    ret = retrieve_mod.retrieve_for_turn(
        session.conn,
        session_id=session.session_id,
        persona=session.persona,
        settings=session.settings,
        user_text=user_text,
        memory_version=memory_version,
        inject_memory=inject_memory,
        inject_history=inject_history,
        turn_id=turn_id,
        full_version=full_memory_version,
    )

    # Phase 6：关系事件检测 + 状态注入。
    # 检测在**本轮的模型调用之前**做，这样新事件能立刻影响本轮注入的关系状态；
    # 事件写入放在本轮落盘之后（见下方），保证事件能挂上 turn_id。
    import relationship as relationship_mod

    rel_rules = session.settings.relationship or {}
    rel_events = relationship_mod.detect(user_text, rel_rules) if rel_rules else []
    rel_text = ""
    if use_relationship and rel_rules:
        state = relationship_mod.current_state(session.conn, rel_rules)
        rel_text = relationship_mod.render_for_prompt(state)

    # Phase 7：用户模型。和记忆/关系不同 —— 它是**会变的累积状态**，
    # 所以先并入本轮信号，再用更新后的模型注入，让模型立刻看到新的理解。
    import user_model as user_model_mod

    um_rules = session.settings.user_model or {}
    um_signals = user_model_mod.detect_signals(user_text, um_rules) if um_rules else []
    um_incoming = user_model_mod.build_claims(
        um_signals, session_id=session.session_id, seq=seq, turn_id=turn_id,
        source_text=user_text,          # P0：存原话，供事后追溯
    ) if um_signals else []
    um_existing = user_model_mod.load_items()
    um_items, um_changes = user_model_mod.merge_claims(um_existing, um_incoming, um_rules)
    um_text = user_model_mod.render_for_prompt(um_items) if um_items else ""

    # V1.1：小满的**自我模型**（「小满眼中的自己」）—— 与用户模型并列的第二个模型。
    #
    # 这里做的是「自我修改」的落点：
    #   · 用户对**小满自己/聊天方式**的反馈（「人机感太重」「不知道聊啥」）
    #     → 学成一条自我模型条目 → 立刻注入，本轮就开始按它调整
    #   · 同方面的新反馈取代旧反馈（标记取代、不删除，可追溯）
    #
    # 刻意**不碰 personality.json**：核心性格/价值观/边界仍然 readonly +
    # sha256 守护。自我修改只开放到「做法」层 —— 理由见 self_model.py 顶部。
    import self_model as self_model_mod

    sm_rules = session.settings.self_model or {}
    sm_existing = self_model_mod.load_items()
    sm_items, sm_changes = self_model_mod.learn_from_text(
        sm_existing, user_text, sm_rules
    )
    sm_text = self_model_mod.render_for_prompt(sm_items) if sm_items else ""

    # Phase 8：未兑现的主动意图。心跳做出的「想主动说」的话，在这里兑现 ——
    # 命令行项目没有投递通道，所以只能在用户开口时注入，不假装能推送。
    import heartbeat as heartbeat_mod

    # Phase 13：先跑一次心跳检查（零模型调用），**再**读待处理项 ——
    # 顺序有实质意义：本次检查若产生新的主动意图，本轮就能兑现，
    # 不会白等一整轮。
    heartbeat_result = maybe_heartbeat_tick(session)

    pending_rows = storage.list_pending_actions(session.conn, status="pending")
    pending_text = heartbeat_mod.render_for_prompt(
        [{"message": r["message"], "reason": r["reason"]} for r in pending_rows]
    )

    # Phase 9：工具。把当前权限下可用的工具渲染成文本协议注入。
    # 被权限挡住的工具**不出现在提示里** —— 免得模型去调一个必然被拒的工具。
    import tools as tools_mod

    tools_text = ""
    if getattr(session, "tools", None) is not None:
        tools_text = tools_mod.prompt.render_tools(session.tools.enabled())

    # 人格协同（最小实现）：把「当前关系状态 + 已知用户信息 + 本轮记忆命中数」
    # 交给 persona 渲染一段**表达调整**。
    #
    # 关键边界：这段只调**表达方式**，不碰性格/价值观/边界 ——
    # 核心人格仍由 render_system_prompt() 静态提供，任何状态都改不动它。
    # 无状态时它返回空串 → 该段不注入 → 行为与补齐前逐字一致。
    # 话题种子（V1.1）：本轮检索**没命中**时，从长期记忆里挑几条最可能引起
    # 对话的旧事递给小满 —— 解决「对方没给话题时它无话可说」。
    #
    # 只在没命中时给，且**只在需要时才读记忆版本**（load_latest 是文件读，
    # 命中时不必付这个代价）。命中说明话题已存在，再塞种子会让它另起话头。
    topic_seeds: list[str] = []
    concerns: list[dict] = []
    try:
        import memory as memory_mod
        _mv = memory_mod.load_latest()
        if _mv is not None:
            # 持续关注点：只有与本轮输入**真相关**时才产出（见 memory.pick_concerns）。
            # 零模型调用，纯读 + 匹配。
            concerns = memory_mod.pick_concerns(_mv, user_text, limit=1,
                                                rel_rules=(rel_rules or None))
            if not (ret["retrieval"].get("items") or []):
                topic_seeds = _pick_topic_seeds(_mv, session.conn, limit=2)
    except Exception:  # noqa: BLE001 — 增强功能，失败就当作没有
        topic_seeds = []
        concerns = []

    # ① 人格出口：她自己的价值观 / 哲学能不能参与这一轮的表达。
    #
    # 由 `persona.pick_stance` 判定「本轮是否出现值得表态的判断点」——
    # 两道闸门（对方在表态 + 落在她的价值主题内）都过才有值。
    # 零模型调用；不产出时 `expression_text` 与补齐前**逐字一致**。
    stance: dict | None = None
    try:
        import persona as persona_mod
        stance = persona_mod.pick_stance(persona_mod.load_profile(), user_text, limit=1)
    except Exception:  # noqa: BLE001 — 增强功能，失败就当作没有
        stance = None

    expression_text = session.persona.render_expression_adjustment(
        relationship_state=(state if use_relationship and rel_rules else None),
        user_model_items=um_items,
        memory_hit_count=len(ret["retrieval"].get("items") or []),
        topic_seeds=topic_seeds,
        concerns=concerns,
        stance=stance,
    )

    messages = build_messages(
        session,
        user_text,
        memory_text=ret["memory_text"],
        relationship_text=rel_text,
        user_model_text=um_text,
        self_model_text=sm_text,
        pending_text=pending_text,
        tools_text=tools_text,
        expression_text=expression_text,
        inject_history=inject_history,
        user_image=user_image,
    )

    # 待续线索 P0：**关注点注入留痕**。
    #
    # ## 为什么放在这个位置
    #
    # 这里是「最终上下文已经确定、模型还没被调用」的唯一时刻。
    # 之前（L558）只知道"谁被选为候选"，之后（L599）就已经发生调用了。
    # 只有在这里才能回答"它到底进没进上下文"。
    #
    # ## 怎么判断"真的进去了"
    #
    # **不看变量、看真实的 messages**：`render_expression_adjustment` 会把
    # 线索片段截到 54 字并去掉尾部标点（见 persona.py），所以这里用**同一口径**
    # 截出 snippet，再在组装好的 messages 全文里找它。
    # 找不到 → `injected=0`（选为候选但没进上下文）—— **不记成已注入**。
    #
    # ## 绝不影响聊天
    #
    # 全程吞异常并记日志：留痕是观测，**任何时候都不该让正常对话失败**。
    # 写失败会留一行日志，**不会静默当作已观测**。
    if concerns:
        try:
            _blob = "\n".join(str(m.get("content") or "") for m in messages)
            _ev = {}
            try:
                for _it in (_mv.items or []):          # noqa: F821 — 上面 try 里已赋值
                    _ev[_it.id] = list(getattr(_it, "source_event_ids", None) or [])
            except Exception:  # noqa: BLE001 — 拿不到来源就不填，不影响留痕
                _ev = {}
            _rows = []
            for _c in concerns:
                _c = _c or {}
                _t = str(_c.get("text") or "")
                if not _t:
                    continue
                _snip = _t.strip().rstrip("。！？；，、 ")[:54]
                _iid = str(_c.get("id") or "")
                _rows.append({
                    "item_id": _iid,
                    "text": _t,
                    "matched": _c.get("matched") or [],
                    "category": _c.get("category") or "",
                    "source_event_ids": _ev.get(_iid, []),
                    "injected": bool(_snip) and _snip in _blob,
                    "method": "pick_concerns",
                })
            _n = storage.record_concern_injections(
                session.conn,
                session_id=session.session_id,
                turn_id=turn_id,
                seq=seq,
                items=_rows,
            )
            if _rows and _n == 0:
                # 幂等命中（本轮已有记录）——留一行，避免被当成"没观测到"
                storage.log_line(
                    f"concern trace: {len(_rows)} candidate(s) already recorded "
                    f"for turn {turn_id} (idempotent skip)"
                )
        except Exception as _exc:  # noqa: BLE001 — 留痕失败不得影响聊天
            try:
                storage.log_line(f"concern trace FAILED（不影响聊天）: {_exc}")
            except Exception:  # noqa: BLE001
                pass

    called_first = call_model(session, messages, purpose="chat", turn_id=turn_id)
    called, final_text, raw_text, executed_calls, tool_iterations, tool_truncated = (
        run_tool_loop(session, messages, turn_id=turn_id, seq=seq, called_first=called_first)
        if tools_text else
        (called_first, tools_mod.strip_calls(called_first["reply"].text or ""),
         called_first["reply"].text or "", [], 0, False)
    )
    reply = called["reply"]
    peak = called["peak"]
    cost_micro = called["cost_micro_yuan"]

    turn = storage.save_turn(
        session.conn,
        turn_id=turn_id,
        session_id=session.session_id,
        seq=seq,
        user_text=user_text,
        assistant_text=final_text,
        model=reply.model,
        persona_name=session.persona.name,
        persona_sha256=session.persona.source_sha256,
    )

    created_event = None
    detection = events_mod.detect(user_text, session.settings.triggers)
    if detection["hit"]:
        created_event = storage.create_event(
            session.conn,
            session_id=session.session_id,
            turn_id=turn["turn_id"],
            user_text=user_text,
            detection=detection,
        )
        storage.log_line(f"raw event created: {created_event['event_id']} (matched {detection['matched']})")

    # Phase 6：把本轮检测到的关系事件落盘（此时才拿得到 turn_id 与 seq）
    created_rel_events = []
    for ev in rel_events:
        eid = storage.record_relationship_event(
            session.conn,
            session_id=session.session_id,
            turn_id=turn["turn_id"],
            seq=seq,
            type_=ev["type"],
            evidence=ev["evidence"],
            matched=ev["matched"],
            note=ev.get("note", ""),
        )
        created_rel_events.append({"event_id": eid, **ev})
        storage.log_line(
            f"relationship event: {eid} type={ev['type']} matched={ev['matched']}"
        )

    # Phase 12 P0：关系状态快照**自动落盘**。
    #
    # ## 修的是什么（实测发现的断点）
    #
    # `relationship.save_snapshot` 此前**只在手工 CLI 命令里被调用**
    # （main.py 的 relationship 命令），`send_once` 里从来没有。
    # 后果：关系事件一路累积到 5 条、状态已从「初识」走到「信任」，
    # 而磁盘上永远只有 10-07 那份 `state_v001`（初识）。
    #
    # 注入是走 `current_state()` 实时派生的，所以**行为一直是对的** ——
    # 但「关系什么时候变的」这件事**没有历史可查**，三系统协同不可观测。
    #
    # ## 三条纪律
    #
    # 1. **只在本轮真有新事件时写** —— 否则每轮都产生内容相同的快照，
    #    把「关系真正变化的那一刻」淹没掉（与自我模型同一考虑）。
    # 2. **绝不覆盖** —— save_snapshot 已存在即抛错，这里接住并跳过。
    # 3. **失败不影响聊天** —— 主路径优先。
    #
    # ## 为什么不担心「重复计入同一事件」
    #
    # 事件去重在**写入侧**已经成立：`rel_events` 来自本轮对 user_text 的
    # `detect()`，同一句话只在它自己那一轮被检测一次；快照只是**读**事件表
    # 派生出的状态，不产生事件，所以不会重复计数。
    rel_saved = None
    if created_rel_events:
        try:
            rel_state_now = relationship_mod.current_state(session.conn, rel_rules)
            rel_saved = relationship_mod.save_snapshot(
                rel_state_now,
                note=f"seq {seq} 新增 {len(created_rel_events)} 条关系事件"
                     f"（{'+'.join(e['type'] for e in created_rel_events)}）",
            )
            storage.log_line(
                f"relationship snapshot: {rel_saved['version']} "
                f"stage={rel_state_now.get('stage')} "
                f"trust={rel_state_now.get('trust')} "
                f"closeness={rel_state_now.get('closeness')}"
            )
        except Exception as exc:  # noqa: BLE001 — 落盘失败不阻断聊天
            storage.log_line(f"relationship snapshot FAILED（不影响聊天）: {exc}")

    # V1.1：自我模型的变更落盘。**只有真的学到东西时才写快照** ——
    # 每轮都写会产生大量内容相同的快照，把「我什么时候改了自己」淹没掉。
    sm_saved = None
    if sm_changes:
        try:
            sm_saved = self_model_mod.save_snapshot(
                sm_items, note=f"seq {seq} 学到 {len(sm_changes)} 条自我修正"
            )
            storage.log_line(
                f"self model updated: {len(sm_changes)} 条, "
                f"snapshot={sm_saved['version']} "
                f"aspects={[c.get('aspect') or '未归类' for c in sm_changes]}"
            )
        except Exception as exc:  # noqa: BLE001 — 主路径优先，落盘失败不影响聊天
            storage.log_line(f"self model snapshot FAILED（不影响聊天）: {exc}")

    # Phase 7：把用户模型的变更落盘（快照 + 事件），此时才拿得到 turn_id 与 seq
    um_saved = None
    if um_changes:
        um_saved = user_model_mod.save_snapshot(um_items, note=f"seq {seq} 自动保存")
        for ch in um_changes:
            storage.record_user_model_event(
                session.conn,
                session_id=session.session_id,
                turn_id=turn["turn_id"],
                seq=seq,
                kind=ch["type"],
                claim_id=ch.get("id") or ch.get("target_id"),
                belief=ch.get("belief") or ch.get("target_belief") or "",
                detail=json.dumps(ch, ensure_ascii=False),
            )
        storage.log_line(
            f"user model updated: {len(um_changes)} changes, snapshot={um_saved['version']}"
        )

    # Phase 8：待处理的主动意图在本轮兑现，标记为已投递（不删除，保留痕迹）
    delivered_pending = []
    for row in pending_rows:
        storage.mark_pending_delivered(session.conn, row["pending_id"], turn["turn_id"])
        delivered_pending.append(row["pending_id"])
    if delivered_pending:
        storage.log_line(
            f"pending actions delivered this turn: {delivered_pending}"
        )

    storage.log_line(f"turn {turn['turn_id']} seq={seq} tokens={reply.usage.prompt_tokens}+{reply.usage.completion_tokens}")

    # Memory V1：批量自动整理。放在落盘**之后**，确保本轮对话已被计入；
    # 失败不影响本轮返回（maybe_auto_consolidate 内部已吞异常）。
    auto_result = maybe_auto_consolidate(session, echo=echo)

    echo(reply.text)
    return {
        "turn": turn,
        "reply": reply,
        "peak": peak,
        "cost_micro_yuan": cost_micro,
        "event": created_event,
        "retrieval": ret["retrieval"],
        "relationship_events": created_rel_events,
        "relationship_text": rel_text,
        "relationship_snapshot": rel_saved,
        "user_model_changes": um_changes,
        "user_model_text": um_text,
        "user_model_snapshot": um_saved,
        "self_model_changes": sm_changes,
        "self_model_text": sm_text,
        "self_model_snapshot": sm_saved,
        "pending_delivered": delivered_pending,
        "pending_text": pending_text,
        "heartbeat": heartbeat_result,
        "tool_calls": executed_calls,
        "tool_iterations": tool_iterations,
        "tool_truncated": tool_truncated,
        "tools_text": tools_text,
        "raw_text": raw_text,
        "auto_consolidate": auto_result,
    }


# ----------------------------------------------------------------------------
# Phase 13 起：心跳的运行链路接入
# Phase B 起：**触发策略已移入 heartbeat.py**（`MIN_GAP_MINUTES` /
#             `maybe_tick`），本模块只保留一个薄委托，见下方 `maybe_heartbeat_tick`。
#             原来这里的 `_HEARTBEAT_MIN_GAP_MINUTES` 已随之删除，避免两处各有一个阈值。
# ----------------------------------------------------------------------------


def maybe_heartbeat_tick(session: Session) -> dict:
    """在聊天生命周期里跑一次心跳检查。**零模型调用、零依赖**。

    ## Phase B：实现已移出 chat.py

    触发策略（已有待处理 → 跳过；距上次检查过近 → 跳过）与 tick 调用
    原在本模块，现全部移入 `heartbeat.maybe_tick()` ——
    **心跳对自己的触发负责**，不再住在聊天模块里。

    本函数保留为**薄委托**，原因只有一个：让 `send_once` 的调用点逐字不变。

    ## Phase D.2：返回类型由 `dict | None` 改为 `dict`

    原实现把「节流 / 配置错误 / 运行时异常」压成同一个 `None`。
    现在 `heartbeat.maybe_tick` 总是返回带 `status` 的字典
    （`throttled` / `pending_action` / `executed` / `configuration_error` /
    `runtime_error`），配置错误不再伪装成节流。

    **调用点（`send_once`）无需改动**：它只把这个返回值放进结果字典
    （`"heartbeat": heartbeat_result`），没有任何地方判断 `is None`。
    """
    import heartbeat as heartbeat_mod
    return heartbeat_mod.maybe_tick(session, trigger=heartbeat_mod.TRIGGER_CHAT)


# ----------------------------------------------------------------------------
# 话题种子：对方没给话题时，小满能主动开口
# ----------------------------------------------------------------------------

# 哪些类别的记忆适合当谈资。**刻意排除「未分类」** —— 拿不准内容的条目
# 不适合被主动提起（可能是零碎事实，聊起来会显得突兀）。
_TOPIC_CATEGORIES = ("共同经历", "项目状态", "偏好", "计划", "个人事实", "关系经历")


def _pick_topic_seeds(mv, conn, *, limit: int = 2) -> list[str]:
    """从长期记忆里挑几条**最可能引起对话**的旧事。

    ## 挑选规则（全部确定性，可复现）

    1. 只取 `tier != archive`（归档层不该被主动提起）
    2. 只取类别在 `_TOPIC_CATEGORIES` 里的
    3. **优先挑「还没聊过」的** —— 用 retrievals 表统计每条被召回的次数，
       召回过越少越新（这正是「你之前说过……后来呢」的素材）
    4. 排序：未召回优先 → 重要性降序 → id 升序（保证完全确定）
    5. 文本裁到 40 字以内，避免种子本身占太多上下文

    这条能力**不新增模型调用**：纯粹是读已有的记忆与检索记录。
    """
    try:
        items = [i for i in (mv.items or [])
                 if getattr(i, "tier", "experience") != "archived"
                 and getattr(i, "tier", "experience") != "archive"
                 and getattr(i, "category", "") in _TOPIC_CATEGORIES]
        if not items:
            return []

        # 统计每条被召回过几次（用于「还没聊过」排序）
        recalled: dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT hit_item_ids FROM retrievals ORDER BY rowid DESC LIMIT 200"
            ).fetchall()
            import json as _json
            for r in rows:
                raw = r["hit_item_ids"] if hasattr(r, "keys") else r[0]
                try:
                    for iid in (_json.loads(raw or "[]") or []):
                        recalled[str(iid)] = recalled.get(str(iid), 0) + 1
                except (ValueError, TypeError):
                    continue
        except Exception:  # noqa: BLE001 — 统计失败不影响主路径
            recalled = {}

        def rank_key(it):
            # 未召回过 = 0 次 → 排最前；次数相同按重要性降序，再按 id 升序
            return (recalled.get(it.id, 0), -_seed_weight(it), it.id)

        import memory as memory_mod
        ordered = sorted(items, key=rank_key)
        seeds = []
        for it in ordered:
            t = (it.text or "").strip()
            if not t or t in seeds:
                continue
            seeds.append(t[:40])
            if len(seeds) >= limit:
                break
        return seeds
    except Exception:  # noqa: BLE001 — 话题种子是增强，失败就当作没有
        return []


def _seed_weight(item) -> float:
    """种子的优先级权重：类别匹配度 + 内容长度适中。

    刻意不直接用 memory.importance() —— 那个是为**检索**设计的，
    这里要的是「聊起来有意思」。两件事不一样。
    """
    w = 0.0
    cat = getattr(item, "category", "")
    # 共同经历 / 项目状态 最适合当谈资（有具体进展可问）
    w += {"共同经历": 1.0, "项目状态": 0.9, "计划": 0.8,
          "偏好": 0.5, "个人事实": 0.4, "关系经历": 0.6}.get(cat, 0.0)
    n = len(getattr(item, "text", "") or "")
    # 太短的没信息量，太长的塞不进种子；20-45 字最合适
    if 20 <= n <= 45:
        w += 0.5
    elif n < 12:
        w -= 0.5
    if getattr(item, "salience", 0):
        w += 0.3
    return w


# ----------------------------------------------------------------------------
# Memory V1：自动整理触发
# ----------------------------------------------------------------------------

# 每积累多少轮对话触发一次整理。**批量而非每轮** ——
# 整理本身要一次模型调用（约 0.03 元），每轮都做会显著抬高成本。
AUTO_CONSOLIDATE_EVERY = 20

# 并发保护：同一时刻只允许一个整理在跑。
# 用途：webui 的 ThreadingHTTPServer 可能并发处理请求，
# 两个线程同时判断"该整理了"会各发一次模型调用（重复扣费 + 版本号竞争）。
# 用非阻塞 acquire —— 拿不到锁就直接跳过，绝不排队等待。
_CONSOLIDATE_LOCK = threading.Lock()


def pending_turns_since_consolidate(session: Session) -> int:
    """自**上次成功整理**以来积累的新对话轮数。

    ## V1.1 关键改动：从数据库推导，不再用内存计数

    原先用 `Session.turns_since_consolidate` 累加，有三个真实缺陷：

      1. **进程退出就归零** —— 用户聊 15 轮关掉、再开聊 15 轮，永远攒不到 20
      2. **跨会话不累计** —— 换个 session_id 重来
      3. **失败后强行清零** —— 失败的整理本该"保留待处理状态"

    现在改为从**已有数据**推导：

        待整理轮数 = 现有对话总数 - 最新记忆版本的 source_range.conversation_to_seq

    这个定义天然满足全部要求，**且不需要新增任何表或字段**：
      · 跨重启/跨会话 → 数据在库里，重启后照样算得出来 ✅
      · 失败保留待处理 → 整理失败时版本号没推进，待整理轮数原样保持 ✅
      · 成功才清零     → 成功整理后 to_seq 前移，待整理轮数自动归零 ✅
    """
    try:
        import memory as memory_mod
        previous = memory_mod.load_latest()
        since = 0
        if previous is not None:
            since = int((previous.source_range or {}).get("conversation_to_seq") or 0)
        row = session.conn.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE seq > ?", (since,)
        ).fetchone()
        return int(row["n"] if row is not None else 0)
    except Exception:  # noqa: BLE001 — 推导失败时按 0 处理（不触发整理）
        return 0


def maybe_auto_consolidate(session: Session, *, echo=lambda *_: None,
                           every: int = AUTO_CONSOLIDATE_EVERY) -> dict | None:
    """按轮数批量触发记忆整理。**失败绝不阻断聊天**。

    ## 为什么是批量触发

    整理 = 1 次模型调用（实测约 0.03 元 / 12000 token 预算）。
    每轮都整理会让闲聊成本翻很多倍，而记忆更新并不需要那么及时。
    按 `every` 轮攒一批，是成本与"记忆不过期"之间的折中。

    ## 为什么失败不抛错、且不清零

    用户要求：「整理失败不得阻断聊天」「失败时保留待处理状态」。
    所以异常被吞掉并记日志，而**待整理计数由数据库推导、失败时自然不变** ——
    下一轮聊天会再次尝试，不会因为一次网络抖动而把积累的 20 轮丢掉。

    ## 触发条件（四者都满足才跑）

      1. 配置里 `enabled` 不为 false
      2. **待整理轮数**（数据库推导）>= every
      3. 拿到并发锁（没拿到说明另一次整理正在跑 → 直接跳过）
    """
    cfg = (session.settings.memory or {}).get("auto_consolidate") or {}
    if cfg.get("enabled") is False:
        return None

    # V1.1：只读地推导，不做任何"自增" —— 自增会导致重启丢失与失败清零
    pending = pending_turns_since_consolidate(session)
    # 同步到会话字段，仅供显示/向后兼容；**判断依据是 pending，不是它**
    session.turns_since_consolidate = pending

    if pending < every:
        return None

    if not _CONSOLIDATE_LOCK.acquire(blocking=False):
        storage.log_line("auto-consolidate: 已有整理在运行，本轮跳过（并发保护）")
        return None

    try:
        import consolidate as consolidate_mod

        result = consolidate_mod.consolidate(
            session, note=f"自动整理（待整理 {pending} 轮）"
        )
        session.conn.commit()
        # 成功后待整理轮数由 to_seq 前移自动归零，无需手动清
        session.turns_since_consolidate = pending_turns_since_consolidate(session)

        # Phase 12 P3：**搭车**做一次行为自我观察 —— 零额外模型调用。
        #
        # 为什么挂在这里：整理本来就已经每 20 轮跑一次模型调用，
        # 而行为审计**根本不需要模型**（纯读回复文本算指标）。
        # 借这个已有的触发点落地，既满足「不新增每轮调用」，
        # 又天然获得「每 20 轮复查一次」的节奏 —— 正好对应
        # 「观察后续真实聊天是否出现改善」。
        try:
            import self_model as self_model_mod
            _sm_items = self_model_mod.load_items()
            _triggered = self_model_mod.audit_behavior(session.conn)
            _sm_items, _obs_changes = self_model_mod.sync_observations(_sm_items, _triggered)
            if _obs_changes:
                _obs_snap = self_model_mod.save_snapshot(
                    _sm_items, note=f"行为审计：{len(_triggered)} 项达标"
                )
                storage.log_line(
                    f"behavior audit: 达标 {len(_triggered)} 项 "
                    f"[{', '.join(t.get('aspect','?') for t in _triggered) or '无'}] "
                    f"变更 {len(_obs_changes)} 条 snapshot={_obs_snap['version']}"
                )
            else:
                storage.log_line(
                    f"behavior audit: 达标 {len(_triggered)} 项，无变化"
                )
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响整理与聊天
            storage.log_line(f"behavior audit FAILED（不影响聊天）: {exc}")

        storage.log_line(
            f"auto-consolidate: {result['version']} "
            f"items={len(result['items'])} cost={result['cost_micro_yuan']}"
        )
        echo(f"（已自动整理记忆 → {result['version']}，{len(result['items'])} 条）")
        return result

    except Exception as exc:  # noqa: BLE001 — 主路径优先，任何失败都不许冒泡
        # **不清零** —— 待整理状态保留，下一轮会再试（见 docstring）
        try:
            storage.log_line(
                f"auto-consolidate FAILED (已跳过，不影响聊天；待整理状态保留): {exc}"
            )
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        _CONSOLIDATE_LOCK.release()


def print_header(session: Session) -> None:
    """打印会话头。

    抽成独立函数是为了能被自测覆盖 —— 它原先内联在 `run_repl()` 里，
    结果带着一个 `sqlite3.Row` 属性访问错误上线了：
    `mem.item_count` 应为 `mem['item_count']`（Row 只支持下标访问）。
    因为没有记忆版本时 `mem` 是 None、走不到那一分支，所以一直没暴露，
    直到 v001 生成后一点「开始聊天」就直接崩。

    教训：**只在实际会执行到的路径上测过的代码，才算是测过的。**
    """
    conn = session.conn
    mem = storage.latest_memory_version(conn)
    print("=" * 60)
    print("最小聊天原型 · Phase 1-9")
    print(f"人格：{session.persona.name}    模型：{session.client.model}")
    if mem:
        # 注意：sqlite3.Row 必须用下标访问，不能用属性访问
        print(f"记忆：{mem['version']}（{mem['item_count']} 条）"
              f"    top_k={session.settings.memory_top_k}")
    else:
        print(f"记忆：尚无记忆版本（用 consolidate 生成第一版）"
              f"    top_k={session.settings.memory_top_k}")
    # Phase 6：关系状态也是每轮都会注入的上下文，开头就让人看得见
    try:
        import relationship as relationship_mod

        state = relationship_mod.current_state(conn, session.settings.relationship)
        flag = "    ⚠ Counter Mode" if state.get("counter_mode") else ""
        print(f"关系：{state['stage']}（信任 {state['trust']}　亲近 {state['closeness']}　"
              f"熟悉 {state['familiarity']}　事件 {state['total_events']}）{flag}")
    except Exception as exc:  # 关系模块异常不该挡住聊天
        print(f"关系：读取失败（{type(exc).__name__}）")
    # Phase 7：她眼中的用户
    try:
        import user_model as user_model_mod

        um_items = user_model_mod.load_items()
        um_active = user_model_mod.active_items(um_items)
        um_superseded = len(um_items) - len(um_active)
        print(f"用户模型：{len(um_active)} 条生效"
              + (f"　{um_superseded} 条已修正" if um_superseded else "")
              + ("" if um_items else "（会在聊天中自动累积）"))
    except Exception as exc:
        print(f"用户模型：读取失败（{type(exc).__name__}）")
    # Phase 8：有没有「想说但还没说出口」的话
    try:
        _pending = storage.list_pending_actions(conn, status="pending")
        print(f"主动性：{len(_pending)} 条待兑现的主动意图"
              + ("" if _pending else "（心跳决定不必主动）"))
    except Exception as exc:
        print(f"主动性：读取失败（{type(exc).__name__}）")
    # Phase 9：工具
    try:
        reg = getattr(session, "tools", None)
        if reg is None:
            print("工具：未挂载")
        else:
            enabled = reg.enabled()
            names = "、".join(t.name for t in enabled) or "（无）"
            print(f"工具：{len(enabled)} 个可用（权限 {'/'.join(reg.allowed_permissions)}）"
                  f"　{names}")
    except Exception as exc:
        print(f"工具：读取失败（{type(exc).__name__}）")
    print(f"会话：{session.session_id}    数据目录：conversation/  memory/  relationship/  user_model/")
    print("输入 /help 查看命令，/exit 退出。")
    print("=" * 60)
    print(f"{session.persona.name}：{session.persona.data.get('first_message', '')}".rstrip("："))
    print("-" * 60)


def run_repl(session: Session) -> None:
    """命令行对话。用命令行而不是 Web，是为了让项目保持最小、最易迁移。"""
    print_header(session)

    while True:
        try:
            line = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return

        if not line:
            continue

        low = line.lower()
        if low in {"/exit", "/quit", ":q", "exit"}:
            print("已退出。")
            return
        if low == "/help":
            print(HELP_TEXT)
            continue
        if low == "/stats":
            s = storage.stats(session.conn)
            m = storage.memory_stats(session.conn)
            r = storage.relationship_event_counts(session.conn)
            print(f"对话轮数：{s['turns']}    原始事件：{s['events']}    API 调用：{s['api_calls']}")
            print(f"记忆版本：{m['versions']}    检索记录：{m['retrievals']}")
            print(f"关系事件：{sum(r.values())}"
                  f"（肯定 {r['positive']}　共同经历 {r['shared_experience']}　"
                  f"不满 {r['negative']}　冲突 {r['conflict']}）")
            continue
        if low == "/relationship":
            import relationship as relationship_mod

            state = relationship_mod.current_state(session.conn, session.settings.relationship)
            print(relationship_mod.format_state(state))
            rows = storage.list_relationship_events(session.conn)[-5:]
            if rows:
                print("\n最近 5 条关系事件：")
                for row in rows:
                    label = relationship_mod.TYPE_LABELS.get(row["type"], row["type"])
                    matched = json.loads(row["matched"])
                    print(f"  [{label}] {row['evidence'][:40]}   命中={matched}")
            continue
        if low == "/memory":
            _print_memory(session.conn)
            continue
        if low == "/user":
            import user_model as user_model_mod

            print(user_model_mod.format_model(user_model_mod.load_items()))
            continue
        if low == "/tools":
            reg = getattr(session, "tools", None)
            if reg is None:
                print("本轮没有挂载工具层。")
            else:
                print(f"允许权限：{'/'.join(reg.allowed_permissions)}")
                for t in reg.enabled():
                    print(f"  ✅ {t.name}（{t.permission}）— {t.description}")
                blocked = [n for n in reg.names() if n not in [x.name for x in reg.enabled()]]
                for n in blocked:
                    print(f"  ⛔ {n}（权限未放开）")
            continue
        if low == "/retrieval":
            _print_last_retrieval(session.conn)
            continue
        if low == "/cost":
            print(pricing.format_summary(pricing.summarize()))
            continue
        if low == "/verify":
            import verify as verify_mod

            print(verify_mod.format_report(verify_mod.verify_all(session.conn)))
            continue

        try:
            result = send_once(session, line, echo=lambda text: print(f"{session.persona.name}：{text}"))
        except LLMError as exc:
            print(f"[调用失败] {exc}")
            continue
        except Exception as exc:  # 不让单轮异常打断整个会话
            print(f"[本轮出错，已跳过] {type(exc).__name__}: {exc}")
            continue

        if result["event"]:
            print(f"  ↳ 已记录原始事件 {result['event']['event_id']}（不可覆盖）")
        r = result["retrieval"]
        print(f"  ↳ 检索：{r['memory_version'] or '无记忆'} 命中 {len(r['items'])} 条"
              f"{'（已注入）' if r['injected'] else '（未注入）'}")
        print(f"  ↳ 本轮 {result['reply'].usage.prompt_tokens}+{result['reply'].usage.completion_tokens} tokens"
              f"，估算 {pricing.format_yuan(result['cost_micro_yuan'])}")


def _print_memory(conn) -> None:
    import memory as memory_mod

    versions = storage.list_memory_versions(conn)
    if not versions:
        print("尚无记忆版本。用 python src\\main.py consolidate 生成第一个版本。")
        return
    for row in versions:
        print(f"{row['version']}  {row['created_at_local']}  {row['item_count']} 条  "
              f"model={row['model']}  ← {row['previous_version'] or '(首版)'}")
    latest = memory_mod.load_latest()
    if latest:
        print(f"\n当前版本 {latest.version} 的条目：")
        for it in latest.items:
            src = "、".join(it.source_event_ids) or "无来源"
            print(f"  [{it.type}] {it.text}")
            print(f"        id={it.id}  来源={src}  confidence={it.confidence}")


def _print_last_retrieval(conn) -> None:
    row = conn.execute(
        "SELECT * FROM retrievals ORDER BY created_at_utc DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("还没有任何检索记录。")
        return
    import json as _json

    ids = _json.loads(row["hit_item_ids"])
    print(f"最近一次检索  {row['created_at_utc']}")
    print(f"  使用的记忆版本：{row['memory_version'] or '无'}")
    print(f"  检索方式：{row['method']}   top_k={row['top_k']}   是否注入={bool(row['injected'])}")
    print(f"  查询原文：{row['query_text']}")
    print(f"  命中 {len(ids)} 条：{ids or '（0 命中 —— 已落盘，这是关键证据）'}")
