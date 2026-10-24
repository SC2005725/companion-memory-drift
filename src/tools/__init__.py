"""工具层（Phase 9）。

对外只暴露三样东西：接口（base）、注册表（registry）、内置工具（builtin）。
Companion Core 只依赖这里的接口，不直接依赖任何平台。
"""

from . import builtin, prompt
from .base import PERMISSIONS, Tool, ToolResult, clamp_text, resolve_within
from .prompt import render_tools
from .registry import MAX_ITERATIONS, ToolRegistry, parse_calls, strip_calls

__all__ = [
    "PERMISSIONS", "Tool", "ToolResult", "clamp_text", "resolve_within",
    "ToolRegistry", "MAX_ITERATIONS", "parse_calls", "strip_calls",
    "build_registry", "render_tools", "prompt", "builtin",
]


def build_registry(settings, *, root=None):
    """按配置装配工具注册表。

    配置在 `settings.json` 的 `tools` 段：
        allowed_permissions  当前放行的权限档（默认只有 read）
        enabled              工具级开关（默认全开，但受权限档约束）

    **权限从配置来、不在代码里写死** —— 用户要放开 execute 时只改配置，
    不用改代码、不用重新测试。
    """
    from pathlib import Path

    import paths as paths_mod
    from . import builtin

    cfg = getattr(settings, "tools", None) or {}
    allowed = tuple(cfg.get("allowed_permissions") or ("read",))
    enabled = set(cfg.get("enabled") or ["read_file", "run_python", "take_note", "now"])
    base_root = Path(root or paths_mod.ROOT).resolve()

    reg = ToolRegistry(allowed_permissions=allowed, allowed_root=base_root)
    candidates = [
        builtin.make_read_file(base_root),
        builtin.make_run_python(base_root),
        builtin.make_take_note(base_root, paths_mod.NOTES_DIR),
        # 「现在几点」—— 用户明确要求过（「你可以接入时间吗」），
        # 且中午说「晚安」的事故就是缺它。零依赖、无副作用。
        builtin.make_now(),
    ]
    for tool in candidates:
        if tool.name in enabled:
            reg.register(tool)
    return reg
