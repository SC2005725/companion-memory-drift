"""把可用的工具渲染成给模型看的说明（Phase 9）。

刻意用**文本协议**而不是 API 原生 tools 字段 —— 见 base.py 的说明：
Core 不该被工具平台绑死。任何能读文本的模型都能驱动这些工具。
"""

from __future__ import annotations

from .base import Tool


def render_tools(tools: list[Tool]) -> str:
    """渲染工具清单 + 调用格式说明。没有可用工具时返回空串。"""
    if not tools:
        return ""

    lines = [
        "【你可以调用的工具】",
        "需要时，在回复里**单独一行**输出下面这种代码块（可以一次输出多个）：",
        "",
        "```tool",
        '{"name": "工具名", "arguments": {"参数名": "参数值"}}',
        "```",
        "",
        "我会执行它，并把结果告诉你，然后你继续回答。",
        "**不需要工具时就不要输出这个代码块** —— 普通聊天直接说话即可。",
        "",
        "可用工具：",
    ]
    for t in tools:
        params = t.input_schema.get("properties") or {}
        required = set(t.input_schema.get("required") or [])
        sig = "、".join(
            f"{k}{'（必填）' if k in required else ''}" for k in params
        ) or "无参数"
        lines.append(f"- `{t.name}`（{t.permission}）— {t.description}")
        lines.append(f"    参数：{sig}")
    return "\n".join(lines)
