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
) -> dict:
    """检索一次并落盘。

    `memory_version=None` 表示使用最新版本；显式传入某个版本号可做「注入旧版」对比。
    `inject=False` 用于「不注入记忆」的对照组：仍然执行检索并落盘，
    但调用方不把它拼进上下文。

    `method` 可覆盖记录里的检索方式标签（如探针整版注入时写 `probe_full_version`），
    这样日后翻 `retrievals` 表就能分清「关键词筛选后注入」和「整版原样注入」。
    """
    if memory_version is None:
        mv = load_current(conn)
        version_name = mv.version if mv else None
    else:
        mv = memory_mod.load_version(memory_version)
        version_name = mv.version

    items = memory_mod.retrieve(mv, query, top_k=top_k)

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
    retrieval = retrieve_and_log(
        conn,
        session_id=session_id,
        query=user_text,
        top_k=settings.memory_top_k,
        turn_id=turn_id,
        memory_version=memory_version,
        inject=inject_memory,
        method="probe_full_version" if full_version else None,
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
