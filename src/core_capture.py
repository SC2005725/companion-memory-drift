"""P0-1：把真实对话自动记入主体事件账本。

## 为什么放在「调用点」而不是 `send_once` 里面

`send_once` 在自测里也会被调用（`selftest_companion` 等）。把钩子放进去，
跑一次自测就会往主体账本里灌一堆测试回合。

所以钩子挂在**两个真实入口**上：
  · `webui.py` 的 `_chat()`（网页说话）
  · `chat.run_repl()`（命令行说话）

自测不经过这两处，因此不会污染账本。

## 三层分离（任务要求）

| 层 | 谁产生 | 落在哪 |
|---|---|---|
| **交互事件** | 本轮对话结束，自动 | `subject_events`，`event_type=interaction` |
| **重要事件** | 交互里识别出关系/用户模型变化，或用户明确要求记住 | 同表，`event_type=significant` |
| **长期记忆** | 现有记忆整理流程筛选合并 | `memory/versions/`（**不由本模块写**） |

本模块**只做前两层**，绝不直接生成正式记忆。

## 幂等

`dedup_key = "turn:<turn_id>"`，同一轮重复调用只入账一次。
"""
from __future__ import annotations

import json
import os

import companion_core as core

# 关掉自动入账：HEXI_CAPTURE=0（出问题时的逃生阀）
ENABLED = os.environ.get("HEXI_CAPTURE", "1") != "0"

# 「重要事件」的识别信号
_SIGNIFICANT_HINTS = (
    "记住", "不要忘记", "别忘了", "以后都", "以后要", "永远", "答应我",
    "我们约定", "说好了", "重要",
)


def _clip(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def classify(turn: dict, result: dict) -> tuple[str, list[str]]:
    """返回 (event_type, 触发原因)。真实识别，不是拍脑袋。"""
    reasons = []
    if (result.get("relationship_events") or []):
        reasons.append("关系事件：" + ",".join(
            str(e.get("type")) for e in result["relationship_events"][:3]))
    if (result.get("user_model_changes") or []):
        reasons.append("用户模型变化：" + ",".join(
            str(c.get("type")) for c in result["user_model_changes"][:3]))
    user_text = (turn.get("user_text") or "")
    hit = [h for h in _SIGNIFICANT_HINTS if h in user_text]
    if hit:
        reasons.append("用户显式要求：" + ",".join(hit[:3]))
    return ("significant" if reasons else "interaction"), reasons


def record_turn(
    *,
    instance_id: str,
    session_id: str | None,
    turn: dict,
    result: dict | None = None,
    subject_id: str = core.DEFAULT_SUBJECT_ID,
) -> dict | None:
    """记录一轮真实对话。

    任何异常都被吞掉并返回 None —— **事件记账绝不能影响聊天本身**。
    """
    if not ENABLED or not turn:
        return None
    result = result or {}
    turn_id = turn.get("turn_id")
    if not turn_id:
        return None
    try:
        event_type, reasons = classify(turn, result)
        env = instance_id.split("_")[-1] if instance_id else "dsh"
        payload = {
            "turn_id": turn_id,
            "seq": turn.get("seq"),
            "session_id": session_id,
            "user": _clip(turn.get("user_text") or "", 300),
            "assistant": _clip(turn.get("assistant_text") or "", 500),
            "memory_hits": len((result.get("retrieval") or {}).get("hit_item_ids") or []),
            "relationship_events": [e.get("type") for e in (result.get("relationship_events") or [])],
            "cost_yuan": round((result.get("cost_micro_yuan") or 0) / 1_000_000, 6),
            "why": reasons,
        }
        content = (
            f"用户：{payload['user']}\n"
            f"{'鹤辞' if subject_id == 'heci_001' else '小满'}：{payload['assistant']}"
        )
        conn = core.connect()
        try:
            core.init_schema(conn)
            core.ensure_subject(conn, subject_id)
            # 实例没登记就顺手登记（真实入口迟早会来）
            try:
                core.register_instance(conn, subject_id, env)
            except Exception:  # noqa: BLE001
                pass
            eid, created = core.append_event(
                conn,
                instance_id=instance_id,
                event_type=event_type,
                content=content,
                source_environment=env,
                occurred_at=turn.get("created_at_local"),
                participants=["用户", subject_id],
                provenance=f"chat/{turn_id}",
                dedup_key=f"turn:{turn_id}",
            )
        finally:
            conn.close()
        return {"event_id": eid, "created": created, "event_type": event_type,
                "why": reasons, "payload": payload}
    except Exception:  # noqa: BLE001 — 记账失败绝不影响聊天
        return None


def record_significant(
    *,
    instance_id: str,
    content: str,
    dedup_key: str,
    source_environment: str | None = None,
    task_id: str | None = None,
    provenance: str | None = None,
) -> dict | None:
    """人工/流程判定为「重要事件」时调用。同样是静默失败。"""
    if not ENABLED:
        return None
    try:
        env = source_environment or (
            instance_id.split("_")[-1] if instance_id else "dsh")
        conn = core.connect()
        try:
            core.init_schema(conn)
            core.ensure_subject(conn)
            eid, created = core.append_event(
                conn, instance_id=instance_id, event_type="significant",
                content=content, source_environment=env,
                task_id=task_id, provenance=provenance, dedup_key=dedup_key)
        finally:
            conn.close()
        return {"event_id": eid, "created": created}
    except Exception:  # noqa: BLE001
        return None


def record_rate_limit(
    *,
    attempt: int,
    delay: float,
    reason: str = "",
    instance_id: str = "heci_001_dsh",
    slot: str | None = None,
) -> dict | None:
    """记一次 429 限速命中。跨进程、跨环境可见——两个实例都能拉到。

    幂等键按「分钟槽」去重：同一波限速里重试 3 次只留 1 条，不刷屏。
    """
    if not ENABLED:
        return None
    try:
        import time as _time
        slot = slot or _time.strftime("%Y%m%d%H%M", _time.localtime())
        env = instance_id.split("_")[-1] if instance_id else "dsh"
        conn = core.connect()
        try:
            core.init_schema(conn)
            core.ensure_subject(conn)
            eid, created = core.append_event(
                conn, instance_id=instance_id, event_type="rate_limit",
                content=(f"触发上游限速（429）：第 {attempt} 次命中，"
                         f"自动退避 {delay:.0f} 秒后重试。\n原始信息：{reason[:300]}"),
                source_environment=env,
                provenance="rate_guard",
                dedup_key=f"429:{slot}")
        finally:
            conn.close()
        return {"event_id": eid, "created": created}
    except Exception:  # noqa: BLE001
        return None


# 给命令行做自检用
if __name__ == "__main__":
    import io
    import sys
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    demo_turn = {"turn_id": "turn_selftest_capture", "seq": 0,
                 "user_text": "以后记住这个原则", "assistant_text": "好。",
                 "created_at_local": None}
    out = record_turn(instance_id="heci_001_dsh", session_id="s_demo",
                      turn=demo_turn, result={})
    print(json.dumps(out, ensure_ascii=False, indent=2)[:600])
    again = record_turn(instance_id="heci_001_dsh", session_id="s_demo",
                        turn=demo_turn, result={})
    print("幂等：第二次 created =", (again or {}).get("created"))