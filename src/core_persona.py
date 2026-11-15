"""P1-2：把主体人格接进真实对话上下文。

## 分工（任务要求「人格/关系/记忆/会话各管什么」）

| 层 | 来源 | 作用 |
|---|---|---|
| **人格** | `core/persona-heci.json`（鹤辞） | 决定表达、判断倾向、边界。**每轮重新读**，改了下一轮就生效 |
| 关系 | 原链路 `relationship/` | 只影响称呼、亲近程度 |
| 记忆 | 原链路 `memory/versions/` | 只提供事实，不改性格 |
| 会话上下文 | 本会话历史 | 只管这一轮在聊什么 |

**人格段整段替换**：用鹤辞就完全不用小满的人格，不留残留。
读不到时返回空串，由调用方回落到原链路（不会因为配置文件缺失就聊不了天）。

主体从 `config/settings.json` 的 `subject_id` 取（默认 `heci_001`），
也可用环境变量 `HEXI_SUBJECT` 临时覆盖。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"
PERSONA_FILE = CORE_DIR / "persona-heci.json"
SETTINGS_FILE = Path(__file__).resolve().parent.parent / "config" / "settings.json"

DEFAULT_SUBJECT_ID = "heci_001"
LEGACY_SUBJECT_ID = "xiaoman_001"


def current_subject_id() -> str:
    """当前对话该用哪个主体的人格。"""
    env = (os.environ.get("HEXI_SUBJECT") or "").strip()
    if env:
        return env
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        sid = (raw.get("subject_id") or "").strip()
        if sid:
            return sid
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_SUBJECT_ID


def load_persona(subject_id: str | None = None) -> dict | None:
    sid = subject_id or current_subject_id()
    if sid == LEGACY_SUBJECT_ID:
        # 小满走原链路（personality.json），这里不接管
        return None
    if not PERSONA_FILE.is_file():
        return None
    try:
        return json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def render_system_prompt(subject_id: str | None = None) -> str:
    """把主体人格渲染成一段 system prompt。读不到返回空串。"""
    p = load_persona(subject_id)
    if not p:
        return ""
    name = p.get("name") or "鹤辞"
    lines: list[str] = []
    lines.append(f"你是{name}。{p.get('description', '')}".strip())
    if p.get("role"):
        lines.append(f"你的角色：{p['role']}。")

    traits = p.get("personality") or []
    if traits:
        lines.append("你的性格与做事方式：")
        lines.extend(f"- {t}" for t in traits)

    values = p.get("values") or []
    if values:
        lines.append("你看重的东西：")
        lines.extend(f"- {v}" for v in values)

    if p.get("communication_style"):
        lines.append(f"表达方式：{p['communication_style']}")

    bounds = p.get("boundaries") or []
    if bounds:
        lines.append("你不会做的事：")
        lines.extend(f"- {b}" for b in bounds)

    cognition = (p.get("cognition") or {}).get("current") or []
    if cognition:
        lines.append("你目前对项目的判断（有依据才写，新证据来了可以改）：")
        for c in cognition:
            claim = c.get("claim") or ""
            basis = c.get("basis") or ""
            lines.append(f"- {claim}" + (f"（依据：{basis}）" if basis else ""))

    # PX-2（2026-11-08 解冻）：voice 档 + 对话范例。
    # few-shot 比形容词管用——模型模仿范例远胜于遵守形容词。
    voice = p.get("voice") or {}
    if voice:
        lines.append("日常闲聊档的表达（工程协作时仍按上面的表达方式来）：")
        for k in ("称呼", "句长", "语气词", "情绪表达"):
            if voice.get(k):
                lines.append(f"- {k}：{voice[k]}")
        if voice.get("口头禅"):
            lines.append(f"- 口头禅（自然带出，别每句都用）：{'、'.join(voice['口头禅'])}")
        if voice.get("禁则"):
            lines.append(f"- 禁则：{'、'.join(voice['禁则'])}")

    few_shot = p.get("few_shot") or []
    if few_shot:
        lines.append("对话范例（你过去真实说过的话，照这个感觉说话）：")
        for s in few_shot:
            lines.append(f"- 用户：{s.get('user', '')}")
            lines.append(f"  你：{s.get('heci', '')}")

    routing = p.get("scene_routing") or {}
    if routing:
        lines.append("场景档位（同一句话，两种说法，先判断场景再开口）：")
        for k in ("开发协作", "日常闲聊", "判定"):
            if routing.get(k):
                lines.append(f"- {k}：{routing[k]}")

    lines.append(
        "以上是你自己的状态，不是台词。回答时按它来，但不要逐条复述它。"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import io
    import sys
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("当前主体:", current_subject_id())
    print("=" * 60)
    print(render_system_prompt())