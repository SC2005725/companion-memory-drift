"""工具接口（Phase 9）。

对应《project-spec》§10「Tool / Plugin Layer」与 §30「Phase 9 Tools」。

## 核心约束：Core 不被工具平台绑死

§10 的原话：

    Companion → Tool Interface → DSH
    Companion → Tool Interface → MCP
    Companion → Tool Interface → Local Agent

    **工具可替换，Companion Core 不应该被工具平台绑死。**

所以这里**刻意不用** OpenAI 兼容 API 的原生 `tools` 字段 ——
用了就把 Companion 绑死在「必须有个支持 function calling 的模型端点」上，
换个本地模型或 MCP 就跑不起来。

而是定义**自己的 Tool 描述结构**，由 `prompt.py` 渲染成文本协议喂给模型。
任何能读写文本的模型都能驱动它，不含 function-calling 能力也行。

## §30 的五项硬要求

    工具调用必须：可记录 · 可追踪 · 有输入输出 · 有错误处理 · 有权限边界

对应实现：
    可记录/可追踪 → 每次调用（含被拒与失败）落 `tool_calls` 表
    输入输出      → `Tool.execute(args) -> ToolResult`，输入输出都在结果里
    错误处理      → `ToolResult` 显式带 `ok/error/error_kind`，不抛给上层
    权限边界      → `Tool.permission` + `check_arguments` 的路径/命令校验

## 权限三档

    read          只读，不改动任何东西（读文件、列目录）
    write         会写文件（仅限允许的目录内）
    execute       会执行命令（风险最高，默认关闭）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PERMISSIONS = ("read", "write", "execute")

# 回灌给模型的单次工具输出上限（字符）。
#
# ## 为什么必须和工具自己宣称的额度对齐（踩过的坑）
#
# 实测：`read_file` 宣称能读 200,000 字节，但 `render()` 默认只放 **2,000 字符** ——
# README 才 7,335 字符就被砍掉 73%。用户问「本项目用什么模型」侥幸答对，
# 因为答案恰好在文件开头（第 15 行）；**问靠后的内容（如第 237 行的计价表），
# 模型只会说「没看到」** —— 而它并不知道自己只看到了一部分的开头。
#
# 额度自相矛盾比额度小更危险：工具承诺的读取范围要真的兑现。
#
# 这个值按上下文预算给：中文约 1.5 字节/字符 → 100,000 字符 ≈ 67,000 token，
# 远低于 1M 上下文。单次成本约 0.13 元（输入 2 元/百万 token），
# 属于「读一个大文件」的合理代价，且进缓存后重复轮次更便宜。
DEFAULT_RENDER_CHARS = 100_000


@dataclass
class ToolResult:
    """一次工具调用的结果。**错误也是一等公民**，不抛异常给上层。

    `error_kind` 区分三类失败，因为它们对用户的意义完全不同：
        denied   —— 权限边界拦下的（她试图越界，需要让用户知道）
        timeout  —— 超时（可能只是慢，不代表错）
        error    —— 真的出错（文件不存在、语法错误…）
    """

    ok: bool
    output: str = ""
    error: str = ""
    error_kind: str = ""      # "" | "denied" | "timeout" | "error"

    def render(self, max_chars: int = DEFAULT_RENDER_CHARS) -> str:
        """回灌给模型的文本。被截断时明确说明 —— 不静默截断。

        注意 `text[:max_chars] + 说明` 这个写法：说明是**额外拼上去的**，
        所以不会被自己的切片吃掉（实测确认过）。
        """
        if self.ok:
            text = self.output or "(无输出)"
        else:
            text = f"[{self.error_kind or 'error'}] {self.error}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（输出过长，已截断，原长 {len(text)} 字符）"
        return text


@dataclass
class Tool:
    """统一工具接口（§10 的 name / description / input_schema / execute / result）。

    `check_arguments` 是权限边界的落点：它在**执行之前**被调用，
    返回错误字符串即拒绝执行。默认实现放行，具体工具按需覆盖。
    """

    name: str
    description: str
    input_schema: dict
    execute: Callable[..., ToolResult]
    permission: str = "read"
    check_arguments: Callable[[dict], str] | None = None
    meta: dict = field(default_factory=dict)

    def validate(self, args: dict) -> str:
        """返回空串 = 放行；返回非空 = 拒绝理由。

        ## 拒绝理由必须**可操作**（踩过的坑）

        实测事故：用户说「帮我记一条笔记：<完整文本>」，模型却发出了
        **空参数** `{"name":"take_note","arguments":{}}`。
        收到的拒绝是：

            [denied] 缺少必填参数：text

        **只说缺什么、没说该怎么调** —— 于是模型重试了**一模一样**的空参数，
        第二次又被拒，就放弃了。用户要记的笔记最终没进系统。

        修法：拒绝理由里**附上一条正确的调用示例**，让模型知道下一步该发什么，
        而不是原地重试。
        """
        if not isinstance(args, dict):
            return "参数必须是对象"
        missing = [k for k in (self.input_schema.get("required") or []) if k not in args]
        if missing:
            return (
                f"缺少必填参数：{'、'.join(missing)}。"
                f"请重新发一次，**把参数填进 arguments**，例如：\n"
                f"```tool\n{self.example_call()}\n```\n"
                f"注意：arguments 不能为空对象，也不要原样重发上一次的调用。"
            )
        if not args and (self.input_schema.get("properties") or {}):
            return (
                f"参数为空。这个工具需要参数，例如：\n"
                f"```tool\n{self.example_call()}\n```"
            )
        if self.check_arguments is not None:
            return self.check_arguments(args) or ""
        return ""

    def example_call(self, overrides: dict | None = None) -> str:
        """生成一条**可直接照抄**的调用示例 JSON。

        值用占位符，但形状必须与真实调用完全一致 ——
        模型照抄形状就能构造出合法调用。
        """
        props = self.input_schema.get("properties") or {}
        required = self.input_schema.get("required") or []
        example: dict = {}
        for key in (required or list(props)):
            placeholder = {
                "string": "这里填内容",
                "integer": 1,
                "number": 1,
                "boolean": True,
                "array": [],
                "object": {},
            }.get((props.get(key) or {}).get("type", "string"), "这里填内容")
            example[key] = placeholder
        if overrides:
            example.update(overrides)
        import json as _json

        return _json.dumps({"name": self.name, "arguments": example}, ensure_ascii=False)


# ----------------------------------------------------------------------------
# 权限边界：路径校验（所有涉及文件系统的工具共用一个实现）
# ----------------------------------------------------------------------------

def resolve_within(root: Path, raw: str) -> tuple[Path | None, str]:
    """把用户/模型给的路径解析到 `root` 之内。

    返回 (绝对路径, 错误)。错误非空即为越界或非法，**调用方必须拒绝执行**。

    ## 为什么必须做这一步

    §30 要求「有权限边界」。模型可以输出任意路径字符串，
    `../../.env` 这种就能读到 API Key。所以**不允许信任模型给的路径** ——
    一律解析成绝对路径后检查是否在允许目录内。

    解析用 `Path.resolve()`，它会把 `..` 与符号链接都展开，
    所以 `root/sub/../../etc/passwd` 也会被正确判定为越界。
    """
    if not raw or not isinstance(raw, str):
        return None, "路径不能为空"
    try:
        base = Path(root).resolve()
        target = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"路径无法解析：{exc}"
    if target != base and base not in target.parents:
        return None, f"路径越界：{raw}（只允许访问 {base} 之内）"
    return target, ""


def clamp_text(text: str, limit: int) -> str:
    """截断过长的文本，并明确标注 —— 不静默截断。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"
