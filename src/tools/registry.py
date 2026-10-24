"""工具注册表与调用入口（Phase 9）。

project-spec §10：Companion Core 只依赖 `Tool` 接口，不依赖任何具体平台。
换平台 = 换注册表里的实现，Core 一行不改。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import storage

from .base import PERMISSIONS, Tool, ToolResult

# 模型输出工具调用的约定格式（平台无关的文本协议）：
#
#     ```tool
#     {"name": "read_file", "arguments": {"path": "README.md"}}
#     ```
#
# 也接受不带围栏的单行 JSON 对象。**刻意不用** OpenAI 原生 tools 字段 —— 见 base.py。
_TOOL_BLOCK_RE = re.compile(r"```(?:tool|json)?[ \t]*\r?\n(.*?)```", re.S)
_LOOSE_RE = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}', re.S)
# 围栏整块（含围栏标记）—— 用于「裸对象扫描要先排除围栏内部」
_FENCE_ANY_RE = re.compile(r"```.*?```", re.S)

MAX_ITERATIONS = 5

# JSON 解析的嵌套深度上限。
# 独立审计攻破过：`"[" * 3000 + "]" * 3000`（仅约 6KB）会让 json.loads 抛
# **RecursionError**，而原异常元组只捕 (ValueError, TypeError) —— 异常穿透
# parse_calls，最终打崩整轮对话。加深度预检可在解析前直接拒掉。
_MAX_JSON_DEPTH = 200


def _too_deep(text: str) -> bool:
    """快速估算嵌套深度，避免把会触发 RecursionError 的负载交给 json.loads。"""
    depth = 0
    for ch in text:
        if ch in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _json_objects(text: str) -> list[str]:
    """从一段文本里**按括号配对**切出所有顶层 JSON 对象。

    为什么不能只用正则：模型可能在一个围栏块里写**多个对象**（协议说明里写了
    「可以一次输出多个」）。原实现把整块文本当一个 JSON 解析 → 失败 → 全部丢弃。
    这里按括号配对切分，一次拿全。字符串内的括号会被正确跳过。
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "]}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def _load_calls(pieces: list[str]) -> list[dict]:
    """把若干 JSON 文本片段解析成调用列表。**任何解析失败都跳过，绝不抛出。**"""
    calls: list[dict] = []
    for raw in pieces:
        if _too_deep(raw):
            continue
        try:
            obj = json.loads(raw)
        except RecursionError:
            # 深嵌套：上面已预检，这里是双保险
            continue
        except Exception:
            # 异常元组给全 —— 审计证明只捕 (ValueError, TypeError) 会漏掉 RecursionError
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = obj.get("arguments")
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name.strip(), "arguments": args})
    return calls


def parse_calls(text: str) -> list[dict]:
    """从模型回复里抽出工具调用。抽不到就返回空列表（不是错误）。

    容错策略（每一条都对应一个被审计攻破过的真实缺陷）：

      1. 先扫**围栏块**，块内按括号配对切分 → 支持一个块里放多个对象
      2. 围栏里解析不出来时，**回退到块外**的裸对象扫描
         （原实现只要有围栏就整个跳过裸对象，坏块会连带吃掉后面的合法调用）
      3. 裸对象扫描**排除围栏内部**，避免把「示例 JSON」当成真调用
         （原实现会把 `{"name":...}` 这种举例真的执行掉，还从正文里删掉）
      4. 深度预检 + 异常元组给全 → 深嵌套负载不会打崩会话
    """
    if not text:
        return []

    block_pieces: list[str] = []
    for m in _TOOL_BLOCK_RE.finditer(text):
        block_pieces.extend(_json_objects(m.group(1)))
    calls = _load_calls(block_pieces)
    if calls:
        return calls

    # 回退：先摘掉所有围栏（含内容），只扫正文里的裸对象
    outside = _FENCE_ANY_RE.sub(" ", text)
    return _load_calls(_json_objects(outside) or [m.group(0) for m in _LOOSE_RE.finditer(outside)])


def strip_calls(text: str) -> str:
    """把工具调用块从回复里去掉，只留给用户看的正文。

    否则用户会看到一堆 JSON。
    **只删围栏块**；正文里的裸对象不删 —— 那可能是模型在举例说明，
    删掉会让用户看不懂自己的对话（审计指出过这个副作用）。
    """
    if not text:
        return ""
    return _TOOL_BLOCK_RE.sub("", text).strip()


class ToolRegistry:
    """工具注册表。权限白名单在构造时确定，运行期不可扩大。"""

    def __init__(self, *, allowed_permissions: tuple[str, ...] = ("read",),
                 allowed_root: Path | None = None):
        for p in allowed_permissions:
            if p not in PERMISSIONS:
                raise ValueError(f"未知权限档：{p}")
        self.allowed_permissions = allowed_permissions
        self.allowed_root = allowed_root
        self._tools: dict[str, Tool] = {}

    # -- 注册 -----------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.permission not in PERMISSIONS:
            raise ValueError(f"工具 {tool.name} 的权限档非法：{tool.permission}")
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        # 装上「执行期二次校验」。
        #
        # 独立审计指出：权限检查原本**只在 call() 里**，任何绕过 call 的路径
        # （新调用方、重构、直接 `tool.execute(...)`）都能拿到 execute 权限跑本机代码。
        # 所以把校验下沉到 execute 自身 —— 一个咽喉变两个，纵深防御。
        self._install_permission_gate(tool)
        self._tools[tool.name] = tool

    def _install_permission_gate(self, tool: Tool) -> None:
        """把权限与参数校验包在 `tool.execute` 外面。"""
        original = tool.execute
        registry = self

        def guarded(args):
            if tool.permission not in registry.allowed_permissions:
                return ToolResult(
                    False,
                    error=f"工具 {tool.name} 需要 {tool.permission} 权限，当前只允许 "
                          f"{'/'.join(registry.allowed_permissions)}",
                    error_kind="denied",
                )
            reason = tool.validate(args if isinstance(args, dict) else {})
            if reason:
                return ToolResult(False, error=reason, error_kind="denied")
            return original(args)

        tool.execute = guarded

    def names(self) -> list[str]:
        return sorted(self._tools)

    def enabled(self) -> list[Tool]:
        """当前权限下**实际可用**的工具。被权限挡住的工具不会出现在提示里 ——
        免得模型去调一个必然被拒的工具，浪费一轮。
        """
        return [self._tools[n] for n in self.names()
                if self._tools[n].permission in self.allowed_permissions]

    # -- 调用 -----------------------------------------------------------------

    def call(self, conn, name: str, args: dict, *, turn_id: str | None = None,
             seq: int | None = None, iterations: int | None = None) -> ToolResult:
        """执行一次工具调用，**无论如何都落账**（含被拒与失败）。

        §30：可记录、可追踪、有错误处理、有权限边界 —— 都在这里落地。
        """
        started = time.perf_counter()
        tool = self._tools.get(name)

        if tool is None:
            result = ToolResult(False, error=f"没有名为 {name} 的工具", error_kind="error")
        elif tool.permission not in self.allowed_permissions:
            result = ToolResult(
                False,
                error=f"工具 {name} 需要 {tool.permission} 权限，当前只允许 "
                      f"{'/'.join(self.allowed_permissions)}",
                error_kind="denied",
            )
        else:
            reason = tool.validate(args)
            if reason:
                result = ToolResult(False, error=reason, error_kind="denied")
            else:
                try:
                    result = tool.execute(args)
                    if not isinstance(result, ToolResult):
                        result = ToolResult(False, error="工具返回了非法结果类型",
                                            error_kind="error")
                except Exception as exc:  # 工具再坏也不能带崩对话
                    result = ToolResult(False, error=f"{type(exc).__name__}: {exc}",
                                        error_kind="error")

        duration_ms = int((time.perf_counter() - started) * 1000)
        storage.record_tool_call(
            conn, name=name, args=args, ok=result.ok,
            result=result.output, error=result.error, error_kind=result.error_kind,
            duration_ms=duration_ms, turn_id=turn_id, seq=seq, iterations=iterations,
        )
        storage.log_line(
            f"tool {name} ok={result.ok} kind={result.error_kind or '-'} "
            f"{duration_ms}ms args={json.dumps(args, ensure_ascii=False)[:120]}"
        )
        return result
