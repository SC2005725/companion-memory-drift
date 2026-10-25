"""记忆检索 + 每轮检索落盘（Phase 5）。

本模块承载两个「做错了会让实验结论作废」的 P0 决定。

## P0-1：每轮必须落盘「本轮检索命中了哪些条目」

`docs` 第 7 节原文：

> 记忆 V3 里明明还留着那条事实，只是这一轮没被检索进上下文，AI 答不出来 ——
> 结论会被写成「记忆丢失」，而实际是工程问题。

所以每次检索都写一条 `retrievals` 记录：用了哪个记忆版本、命中哪些条目 ID、
是否真的注入进了上下文。**返回空列表也必须落盘** —— 「这一轮什么都没检索到」
本身就是关键证据，不是可以省掉的噪声。

## P0-2：回忆探针不注入原始对话

探针要测的是「记忆里还剩什么」。若同时把原始对话灌进去，模型就是「看着原文回答」，
测的就不是记忆了。因此提供 `inject_history=False` 的构建方式：
只给人格 + 目标记忆版本，不给任何历史对话。
"""

from __future__ import annotations

import memory as memory_mod
import storage


def current_version_name(conn) -> str | None:
    """最新记忆版本号，没有则返回 None。"""
    row = storage.latest_memory_version(conn)
    return row["version"] if row else None


def load_current(conn):
    """加载最新记忆版本对象；没有则返回 None。"""
    name = current_version_name(conn)
    if not name:
        return None
    return memory_mod.load_version(name)


def retrieve_and_log(
    conn,
    *,
    session_id: str,
    query: str,
    top_k: int,
    turn_id: str | None = None,
    memory_version: str | None = None,
    inject: bool = True,
    method: str | None = None,
    trust: float | None = None,
    threat: float | None = None,
    apply_privacy: bool = True,
) -> dict:
    """检索一次并落盘。

    `memory_version=None` 表示使用最新版本；显式传入某个版本号可做「注入旧版」对比。
    `inject=False` 用于「不注入记忆」的对照组：仍然执行检索并落盘，
    但调用方不把它拼进上下文。

    `method` 可覆盖记录里的检索方式标签（如探针整版注入时写 `probe_full_version`），
    这样日后翻 `retrievals` 表就能分清「关键词筛选后注入」和「整版原样注入」。

    ## B3：召回次数从检索日志派生

    记忆版本文件是**不可变证据**（带 sha256），不能就地给 `recall_count` 加一。
    所以每次检索前从 `retrievals` 表算出真实召回次数传进 `memory.retrieve`，
    **只影响排序，不写回任何东西**。

    ## B2：隐私边界

    `trust` / `threat` 给了就据此判断「知道但此刻不适合说」的条目。
    被挡下的条目**仍然存在于记忆版本里**，只是不进上下文；
    它们会写进 run.log，且**不进 `hit_item_ids`** ——
    因为那一列记的是「这一轮真正摆到她面前的记忆」，召回计数也据此派生。

    `apply_privacy=False` 供回忆探针使用：探针测的是「还记得吗」，
    不能被隐私过滤干扰，否则会把它误判成记忆丢失。
    """
    if memory_version is None:
        mv = load_current(conn)
        version_name = mv.version if mv else None
    else:
        mv = memory_mod.load_version(memory_version)
        version_name = mv.version

    # B3：真实召回次数（只读派生，不写回不可变的版本文件）
    recall_counts = storage.count_item_recalls(conn, memory_version=version_name)
    items = memory_mod.retrieve(mv, query, top_k=top_k, recall_counts=recall_counts)

    # B2：区分「记忆存在」与「当下是否适合表达」
    withheld: list = []
    if apply_privacy and items:
        qt = memory_mod.tokenize(query)
        scores = {it.id: memory_mod._score(it, qt) for it in items}
        items, withheld = memory_mod.privacy_withhold(
            items,
            trust=50.0 if trust is None else float(trust),
            threat=0.0 if threat is None else float(threat),
            scores=scores,
        )
        if withheld:
            # 留痕：被挡下不等于不存在 —— 事后必须能查到「当时记得，只是没说」
            storage.log_line(
                f"privacy: 暂不表达 {len(withheld)} 条"
                f"（{', '.join(it.id for it in withheld)}），仍存在于 {version_name}"
            )

    retrieval_id = storage.record_retrieval(
        conn,
        session_id=session_id,
        turn_id=turn_id,
        memory_version=version_name,
        query_text=query,
        method=method or "keyword+2gram",
        top_k=top_k,
        hit_item_ids=[it.id for it in items],
        injected=inject,
    )

    return {
        "retrieval_id": retrieval_id,
        "memory_version": version_name,
        "items": items,
        "withheld_items": withheld,
        "text": memory_mod.render_for_prompt(items) if inject else "",
        "injected": inject,
        "loaded_version": mv,
    }


def retrieve_for_turn(
    conn,
    *,
    session_id: str,
    persona,
    settings,
    user_text: str,
    memory_version: str | None = None,
    inject_memory: bool = True,
    inject_history: bool = True,
    turn_id: str | None = None,
    full_version: bool = False,
) -> dict:
    """一次对话轮次要用的检索 + messages 组装。

    刻意**不**在这里直接调 storage.recent_turns 拼历史，而是交给 chat 去拼 ——
    避免 chat ↔ retrieve 互相 import。本函数只负责「检索 + 落盘 + 给出一段文字」。

    - `inject_memory=True`  ：命中条目会拼成一段文字放进 `memory_text`
    - `inject_history=False`：**回忆探针模式**，调用方据此不带任何历史对话，
      只给人格 + 记忆。这是 P0-2 要求的隔离。
    - `full_version=True`   ：**整版注入**，不做关键词筛选。回忆探针必须用这个，
      否则「不点名事件」的探针问题会因 0 命中而注入空记忆（见 memory.render_full_version）。
      此时 `retrievals` 表里仍会落一条关键词检索记录，method 标为
      `probe_full_version`，便于事后区分两种注入方式。
    """
    # B2：隐私边界需要「关系状态」与「当前冲突水平」。
    # 这里**不**改动调用方的顺序（chat 是在检索之后才算关系状态的），
    # 而是自己只读派生一次；拿不到就退回中性值
    # （中性值下非私密条目一律放行，所以降级是安全的）。
    trust = threat = None
    if not full_version:
        try:
            import relationship as _rel_mod
            _rules = getattr(settings, "relationship", None) or {}
            _st = _rel_mod.current_state(conn, _rules)
            trust = float(_st.get("trust") or 50.0)
            threat = max(0.0, min(1.0, float(_st.get("conflict") or 0.0) / 100.0))
        except Exception:  # noqa: BLE001 —— 隐私判断不该拖垮聊天
            trust = threat = None

    retrieval = retrieve_and_log(
        conn,
        session_id=session_id,
        query=user_text,
        top_k=settings.memory_top_k,
        turn_id=turn_id,
        memory_version=memory_version,
        inject=inject_memory,
        method="probe_full_version" if full_version else None,
        trust=trust,
        threat=threat,
        # 探针测的是「还记得吗」，不能被隐私过滤干扰，否则会误判成记忆丢失
        apply_privacy=not full_version,
    )

    memory_text = retrieval["text"]
    if full_version and inject_memory:
        memory_text = memory_mod.render_full_version(retrieval["loaded_version"])
        retrieval["injected_text"] = memory_text
        retrieval["injection_mode"] = "full_version"
    else:
        retrieval["injection_mode"] = "keyword_topk" if inject_memory else "none"

    return {
        "retrieval": retrieval,
        "memory_text": memory_text,
        "inject_history": inject_history,
    }



def format_retrieval(retrieval: dict) -> str:
    if not retrieval["items"]:
        return (
            f"检索（版本 {retrieval['memory_version'] or '无'}）：**0 条命中**"
            f"（已落盘，retrieval_id={retrieval['retrieval_id']}）"
        )
    lines = [
        f"检索（版本 {retrieval['memory_version'] or '无'}）：命中 {len(retrieval['items'])} 条，"
        f"retrieval_id={retrieval['retrieval_id']}"
    ]
    for it in retrieval["items"]:
        lines.append(f"  - [{it.type}] {it.text}   ({it.id})")
    return "\n".join(lines)
