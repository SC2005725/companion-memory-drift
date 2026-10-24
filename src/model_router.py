"""模型路由（Phase 10）。

对应《project-spec》§11「Model Router」与 §31「Phase 10」。

## 规范原话的意图

§11：

    Low-cost Model        → 简单检查 / Heartbeat、状态判断、低成本任务
    Main Model            → 深度对话、复杂推理、重要决策、复杂工具调用

§31：

    Cheep / Local Model     ← 注意「Local」
    目标不是「模型越多越高级」，是**在成本、速度、质量之间取得合理平衡**

## ⚠️ 必须如实说明：**本路由目前省不到钱**

实测（155 次调用）：

    chat         151 次   0.195778 元   79.7%
    consolidate    4 次   0.049849 元   20.3%
    模型分布：deepseek-flash 155 次  ← **全部**

而可用的两个模型是：

    deepseek-flash      输入 2.0   输出 8.0    ← 当前用的，**已经是最低档**
    deepseek-v4-pro     输入 9.0   输出 27.0   ← 贵 4.5 倍

所以「简单任务 → 低成本模型」这句话在当前配置下**省 0 元** ——
**低成本模型就是它自己**。而如果路由把复杂任务送去 v4-pro，反而**更贵**。

**那为什么还做？** 三个真实价值：

1. **为本地模型留好位置。** 规范写的是 "Cheap / **Local** Model" ——
   真正的省钱是本地模型（零 API 费用）。今天无法交付（要新增依赖），
   但接口先立起来：将来接上只需改配置，不用动 `chat.py`。
2. **让模型选择可追踪。** 每次调用用哪个模型、为什么，都进账本与日志 ——
   「为什么这次贵」以后查得出来。
3. **换供应商/换模型只改配置**，代码零改动。

## 设计：确定性规则，不用模型来判断

与记忆检索、关系推导、用户模型、心跳决策同理 —— **「为什么用这个模型」
必须可复现、可解释**。让模型自己决定用哪个模型，既多花一次调用，
又让成本不可归因。

## 路由优先级

    显式指定  →  按用途配置  →  默认模型

`purpose` 已经存在于 `chat.call_model`，所以接入点只有一个。
"""

from __future__ import annotations

# 已知用途。`chat` 覆盖对话与工具循环；其余各自独立。
PURPOSES = ("chat", "consolidate", "probe", "doctor")

PURPOSE_LABELS = {
    "chat": "对话与工具循环",
    "consolidate": "记忆整理",
    "probe": "回忆探针",
    "doctor": "环境自检",
}

# 规范 §11 建议的归类（写在这里作为默认建议值，实际以配置为准）。
# 注意：这是**建议**，不是硬编码 —— 配置里写什么就用什么。
SUGGESTED_TIER = {
    # 低成本档适合的：简单检查、状态判断
    "doctor": "cheap",
    # 主模型档：深度对话、复杂推理、重要决策
    "chat": "main",
    "consolidate": "main",
    "probe": "main",
}


class ModelRouter:
    """按 `purpose` 决定用哪个模型。**纯函数式，无副作用。**"""

    def __init__(self, rules: dict | None = None, *, fallback: str = ""):
        rules = rules or {}
        self.default = str(rules.get("default") or fallback or "")
        self.by_purpose = {
            str(k): str(v) for k, v in (rules.get("by_purpose") or {}).items() if v
        }
        self.known_models = list(rules.get("known_models") or [])
        self.enabled = bool(rules.get("enabled", True))
        self.fallback = fallback
        # 记录每次决策，便于自测与排查
        self.decisions: list[dict] = []

    # -- 决策 -----------------------------------------------------------------

    def resolve(self, purpose: str | None = None, explicit: str | None = None) -> dict:
        """返回 {model, source, reason}。

        `source` 记录这个决定是从哪来的 —— 出问题时能立刻定位是配置错了
        还是代码写死了。
        """
        if not self.enabled:
            model = explicit or self.default or self.fallback
            return self._note(purpose, model, "router_disabled",
                              "路由已关闭，直接用默认模型")

        if explicit:
            return self._note(purpose, explicit, "explicit", "调用方显式指定了模型")

        key = (purpose or "chat").strip() or "chat"
        if key in self.by_purpose:
            return self._note(purpose, self.by_purpose[key], "purpose",
                              f"配置里为用途「{key}」指定了模型")

        model = self.default or self.fallback
        return self._note(purpose, model, "default",
                          f"用途「{key}」没有单独配置，用默认模型")

    def _note(self, purpose, model: str, source: str, reason: str) -> dict:
        d = {"purpose": purpose or "chat", "model": model,
             "source": source, "reason": reason}
        self.decisions.append(d)
        return d

    # -- 展示 -----------------------------------------------------------------

    def describe(self) -> str:
        lines = [f"路由开关：{'开' if self.enabled else '关'}"]
        lines.append(f"默认模型：{self.default or '（未设）'}")
        if self.by_purpose:
            lines.append("按用途指定：")
            for k in sorted(self.by_purpose):
                label = PURPOSE_LABELS.get(k, "")
                lines.append(f"  {k:12} → {self.by_purpose[k]}"
                             + (f"    （{label}）" if label else ""))
        else:
            lines.append("按用途指定：（无）")
        unused = [p for p in PURPOSES if p not in self.by_purpose]
        if unused:
            lines.append("未单独指定（走默认）：" + "、".join(unused))
        if self.known_models:
            lines.append("已知模型单价（元/百万 tokens）：")
            for name, pr in self.known_models:
                lines.append(f"  {name:18} 输入命中 {pr.get('cache_hit')}  "
                             f"输入 {pr.get('input')}  输出 {pr.get('output')}")
        return "\n".join(lines)


def build_router(settings, *, fallback_model: str = "") -> ModelRouter:
    """从 settings.json 的 `model_router` 段装配。缺失时退回默认行为。"""
    rules = getattr(settings, "model_router", None) or {}
    default = str(rules.get("default") or "")
    if not default:
        # 没配 model_router 就用 settings.model / 环境变量给的模型当默认，
        # **行为与加路由之前完全一致**。
        default = getattr(settings, "model", "") or fallback_model
    merged = dict(rules)
    merged["default"] = default
    return ModelRouter(merged, fallback=fallback_model or default)
