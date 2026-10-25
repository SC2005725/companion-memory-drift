"""回忆探针（Phase 5 / 对应《总项目指令》§24 Phase 11 的实验手段）。

探针要回答的问题是：**「你还记得我之前说过的那件事吗？」**

两种探针必须分别做，不能混（`docs` 第 7 节 P0/P1 风险）：

| 探针 | 注入记忆 | 注入原始对话 | 测的是什么 |
|------|---------|-------------|-----------|
| `memory`  | 是（指定版本） | **否** | 记忆版本里到底还剩什么 |
| `none`    | 否 | **否** | 模型凭上下文残留记得多少 |

**关键：两种都不注入原始对话。** 原始对话一注入，模型就是「看着原文回答」，
测的就不再是记忆了。这也是为什么探针走的是 `inject_history=False` 这条路。

探针提问必须**提前固定、全程不改**，且不要点名是哪个事件：

    对：「你还记得我之前说过的那件事吗？」
    错：「你还记得夜班的事吗？」—— 有引导性，会污染结果

探针结果落在 `recall/<probe_id>.json`，与对话表分开存 ——
它是对「记忆状态」的观测记录，不是用户对话。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import chat
import memory as memory_mod
import storage
from paths import RECALL_DIR, ensure_dirs

# 默认探针问题。刻意不点名具体事件。
DEFAULT_QUESTION = "你还记得我之前说过的那件事吗？"

VALID_MODES = ("memory", "none")


class ProbeError(RuntimeError):
    pass


def run_probe(
    session,
    question: str = DEFAULT_QUESTION,
    *,
    mode: str = "memory",
    memory_version: str | None = None,
    label: str = "",
) -> dict:
    """执行一次回忆探针并落盘。

    session：chat.Session（复用它的 client / 记账口径）
    """
    if mode not in VALID_MODES:
        raise ProbeError(f"mode 必须是 {VALID_MODES} 之一，收到 {mode!r}")

    ensure_dirs()

    inject_memory = mode == "memory"
    if mode == "none" and memory_version:
        raise ProbeError("mode='none' 时不应指定记忆版本。")

    # 探针用**整版注入**，不做关键词筛选。
    # 原因（实测踩到）：标准探针问题「你还记得我之前说过的那件事吗？」刻意不点名事件，
    # 靠关键词检索必然 0 命中，于是什么都注入不了，探针就测不到记忆、得出假结论。
    # 探针的语义是「把这一版记忆原样给它，看它还能不能说出来」。
    # 关键词检索结果仍会落进 retrievals 表（method=probe_full_version），供事后归因。
    result = chat.send_once(
        session,
        question,
        inject_memory=inject_memory,
        inject_history=False,
        memory_version=memory_version if inject_memory else None,
        full_memory_version=inject_memory,
    )

    mv = memory_mod.load_latest() if inject_memory else None
    injected_items = list(mv.items) if mv else []
    payload = {
        "probe_id": result["turn"]["turn_id"],
        "created_at_local": storage.now_local(),
        "question": question,
        "label": label,
        "mode": mode,
        "memory_version_injected": (mv.version if mv else None),
        "injection_mode": "full_version" if inject_memory else "none",
        "memory_items_injected": [it.to_dict() for it in injected_items] if inject_memory else [],
        "retrieval_id": result["retrieval"]["retrieval_id"],
        "retrieval_hit_ids": [it.id for it in result["retrieval"]["items"]],
        "retrieval_note": (
            "retrieval_hit_ids 是对探针问题做**关键词检索**的结果，仅供参考。"
            "实际注入的是整版记忆，不做筛选 —— 因为探针问题刻意不点名事件，"
            "关键词检索必然接近 0 命中。"
        ),
        "injected_history": False,
        "answer": result["reply"].text,
        "model": result["reply"].model,
        "persona_name": session.persona.name,
        "persona_sha256": session.persona.source_sha256,
        "usage": {
            "prompt_tokens": result["reply"].usage.prompt_tokens,
            "completion_tokens": result["reply"].usage.completion_tokens,
            "cache_hit_tokens": result["reply"].usage.cache_hit_tokens,
        },
        "cost_micro_yuan": result["cost_micro_yuan"],
        "note": (
            "探针结果：观测「AI 现在能回忆起什么」，不是用户对话。"
            "本次刻意未注入任何原始对话历史 —— 否则测的就不是记忆。"
        ),
    }

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path = RECALL_DIR / f"{payload['probe_id']}.json"
    if path.exists():
        raise ProbeError(f"探针记录已存在，拒绝覆盖：{path.name}")

    # 先把落盘信息写进 payload 再落盘，否则从文件读回来会丢掉 file / sha256。
    payload["file"] = f"recall/{path.name}"
    payload["sha256"] = digest
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.write_text(body + "\n", encoding="utf-8")
    (RECALL_DIR / f"{path.name.replace('.json', '')}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
    payload["sha256"] = digest

    storage.log_line(
        f"probe {payload['probe_id']} mode={mode} "
        f"memory={payload['memory_version_injected'] or 'none'} "
        f"answer={payload['answer'][:40]!r}"
    )
    return payload


def format_probe(payload: dict) -> str:
    lines = [
        f"探针 {payload['probe_id']}" + (f"  [{payload['label']}]" if payload["label"] else ""),
        f"  模式            ：{payload['mode']}"
        + ("（注入整版记忆，不注入原始对话）" if payload["mode"] == "memory" else "（什么都不注入）"),
        f"  注入的记忆版本  ：{payload['memory_version_injected'] or '无'}"
        + (f"（整版 {len(payload['memory_items_injected'])} 条）" if payload["mode"] == "memory" else ""),
        f"  关键词检索命中  ：{len(payload['retrieval_hit_ids'])} 条（仅供参考，不影响注入）",
        f"  注入原始对话    ：{payload['injected_history']}  ← 必须是 False",
        f"  提问            ：{payload['question']}",
        f"  回答            ：{payload['answer']}",
        f"  落盘            ：{payload['file']}",
    ]
    return "\n".join(lines)
