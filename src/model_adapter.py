"""统一模型适配层（Phase C）。

## 这个模块存在的理由

心跳和未来的情感系统需要**调模型的能力**，但不能各自 new 一个客户端 ——
本项目已经为此踩过坑：`doctor --online` 自己 `new LLMClient` 直接 send，
绕过了记账层，导致 `api_calls` 与 `ledger.jsonl` 双双漏记，`cost` 系统性偏低。

所以本模块**不实现任何 HTTP 客户端**，只做三件事：

   1. 定义**统一的请求 / 返回约定**（本文件下半部分）
   2. 提供**适配器注册与路由**（按名字选一个适配器）
   3. 保证**没有配置时明确失败**，绝不假装成功

真正发请求的代码仍然只有 `llm.py` 一个地方；
"现有客户端"适配器也只是**转调 `chat.call_model`**，记账链路原样保留。

## 三个预留方向（Phase C 只留结构，不接服务）

| 适配器 | 现状 | 说明 |
|---|---|---|
| `ExistingClientAdapter` | ✅ **唯一有实现的** | 转调 `chat.call_model`，**记账不变** |
| `LocalModelAdapter` | ⛔ **未实现** | 返回 `not_configured`；不连本地服务 |
| `FreeModelAdapter` | ⛔ **未实现** | 返回 `not_configured`；不连任何免费服务 |

⚠️ **本阶段默认不执行任何模型调用**：没有任何适配器会被自动注册，
`build_router()` 在无配置时返回一个"全部未配置"的路由。
真正的网络请求要等有人显式注册适配器之后才可能发生。

## 免费模型不保证长期可用

`FreeModelAdapter` 的存在只是为了让"换一个便宜来源"变成**换一个适配器**，
而不是改调用方。它被设计成**可随时关闭或替换**：注册表里没有它，路由就选不到它。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 状态常量 —— 调用方**必须**按状态分支，不允许只看 text 是否为空
# ---------------------------------------------------------------------------
#
# 这组状态是"不伪造结果"的机械保证：除 `ok` 之外，`text` 一律为空字符串。
STATUS_OK = "ok"                        # 成功，且有有效输出
STATUS_NOT_CONFIGURED = "not_configured"  # 没有配置这个适配器（**不是失败，是没打算用**）
STATUS_UNAVAILABLE = "unavailable"      # 配置了但当前不可用
STATUS_TIMEOUT = "timeout"              # 超时
STATUS_FAILED = "failed"                # 调用失败（网络/鉴权/服务端错误）
STATUS_EMPTY = "empty"                  # 调用了，但没有有效输出

STATUSES = (STATUS_OK, STATUS_NOT_CONFIGURED, STATUS_UNAVAILABLE,
            STATUS_TIMEOUT, STATUS_FAILED, STATUS_EMPTY,
            )


# ---------------------------------------------------------------------------
# 统一约定：请求
# ---------------------------------------------------------------------------

@dataclass
class ModelRequest:
    """一次模型请求。**与具体模型实现无关。**

    `messages` 与 `prompt` 二选一：
      · 走 `chat.call_model` 的适配器用 `messages`（那是它的既有约定）
      · 简单的文本生成用 `prompt`
    两者都给时以 `messages` 为准。
    """
    prompt: str = ""
    messages: list[dict] | None = None
    purpose: str = "heartbeat"          # 用途标签，路由与记账都用它
    model_id: str = ""                  # 期望模型；空 = 交给适配器/路由决定
    timeout_s: float | None = None      # 超时（秒）；None = 用适配器默认
    meta: dict = field(default_factory=dict)   # 附加信息（如触发来源），只用于留痕


# ---------------------------------------------------------------------------
# 统一约定：返回
# ---------------------------------------------------------------------------

@dataclass
class ModelReply:
    """一次模型调用的结果。**成功与失败共用同一个类型**，靠 `status` 区分。

    字段覆盖要求里的四项：输入侧由 `ModelRequest` 承担，
    输出 `text`、模型标识 `model_id`、成功与否 `ok`/`status`、错误 `error`。
    """
    ok: bool = False
    status: str = STATUS_NOT_CONFIGURED
    text: str = ""
    model_id: str = ""
    adapter: str = ""
    error: str = ""
    elapsed_ms: int = 0
    usage: dict | None = None           # token 用量（若适配器能拿到）

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "status": self.status, "text": self.text,
            "model_id": self.model_id, "adapter": self.adapter,
            "error": self.error, "elapsed_ms": self.elapsed_ms,
            "usage": self.usage,
        }


def _fail(adapter: str, status: str, error: str, *, model_id: str = "",
          elapsed_ms: int = 0) -> ModelReply:
    """构造失败返回。**text 恒为空** —— 失败绝不携带内容。

    这是"不静默伪造结果"的关键：调用方无法从 text 里读到任何编造的东西，
    只能通过 status / error 知道发生了什么。
    """
    return ModelReply(ok=False, status=status, text="", model_id=model_id,
                      adapter=adapter, error=error, elapsed_ms=elapsed_ms)


# ---------------------------------------------------------------------------
# 适配器基类
# ---------------------------------------------------------------------------

class ModelAdapter:
    """适配器基类。**未实现的适配器必须返回 not_configured，不得假装成功。**"""

    name = "base"

    def available(self) -> tuple[bool, str]:
        """`(是否可用, 原因)`。默认不可用 —— 子类必须显式说明自己怎么才可用。"""
        return False, "未配置"

    def generate(self, req: ModelRequest) -> ModelReply:
        raise NotImplementedError


class LocalModelAdapter(ModelAdapter):
    """本地模型适配器（**Phase C 只留结构**）。

    不连接任何本地服务、不加载任何权重、不引入任何依赖。
    要真正启用，需要在这里实现 `generate()` 并让 `available()` 返回 True。
    """

    name = "local"

    def available(self) -> tuple[bool, str]:
        return False, "本地模型适配器未配置（Phase C 只预留接口，未接入任何本地服务）"

    def generate(self, req: ModelRequest) -> ModelReply:
        _ok, why = self.available()
        return _fail(self.name, STATUS_NOT_CONFIGURED, why)


class FreeModelAdapter(ModelAdapter):
    """免费模型服务适配器（**Phase C 只留结构**）。

    ⚠️ 免费模型**不保证长期免费或持续可用**。因此它被设计成可随时移除：
    只要不注册它，路由就永远选不到它，调用方无需改动。
    """

    name = "free"

    def available(self) -> tuple[bool, str]:
        return False, "免费模型适配器未配置（Phase C 只预留接口，未接入任何服务）"

    def generate(self, req: ModelRequest) -> ModelReply:
        _ok, why = self.available()
        return _fail(self.name, STATUS_NOT_CONFIGURED, why)


class ExistingClientAdapter(ModelAdapter):
    """现有模型客户端适配器 —— **转调 `chat.call_model`**。

    ## 为什么是转调而不是直接用 `llm.LLMClient`

    `chat.call_model` 是项目的**记账入口**（"所有 LLM 调用都应走这里，
    否则费用会漏记"）。直接 new 客户端会重演 `doctor --online` 的漏记事故。
    所以这里把消息原样交给它，路由、记账、重试语义**全部沿用既有实现**。

    ## 它也是默认不启用的

    需要显式 `ExistingClientAdapter(session)` 并注册进路由才会被选中。
    没有注册时，`build_router()` 返回的路由不会包含它。
    """

    name = "existing"

    def __init__(self, session) -> None:
        self.session = session

    def available(self) -> tuple[bool, str]:
        if self.session is None:
            return False, "现有客户端适配器未绑定 session"
        if getattr(self.session, "client", None) is None:
            return False, "现有客户端适配器未绑定 client"
        return True, "可用（会经过 chat.call_model 记账）"

    def generate(self, req: ModelRequest) -> ModelReply:
        ok, why = self.available()
        if not ok:
            return _fail(self.name, STATUS_NOT_CONFIGURED, why)

        started = time.monotonic()
        msgs = req.messages or [{"role": "user", "content": req.prompt or ""}]
        try:
            import chat as chat_mod
            called = chat_mod.call_model(
                self.session, msgs,
                purpose=req.purpose or "heartbeat",
            )
        except TimeoutError as exc:
            return _fail(self.name, STATUS_TIMEOUT, f"调用超时：{exc}",
                         elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 — 一切失败都必须变成可追溯的状态
            return _fail(self.name, STATUS_FAILED, f"{type(exc).__name__}: {exc}",
                         elapsed_ms=int((time.monotonic() - started) * 1000))

        elapsed = int((time.monotonic() - started) * 1000)
        reply = called.get("reply") if isinstance(called, dict) else None
        text = (getattr(reply, "text", "") or "").strip()
        model_id = str(getattr(reply, "model", "") or called.get("model", "") or "")
        if not text:
            # 调用了但没拿到有效输出 —— **明确报 empty，不返回半个结果**
            return _fail(self.name, STATUS_EMPTY, "调用成功但无有效输出",
                         model_id=model_id, elapsed_ms=elapsed)
        return ModelReply(ok=True, status=STATUS_OK, text=text, model_id=model_id,
                          adapter=self.name, elapsed_ms=elapsed)


# ---------------------------------------------------------------------------
# 路由：按名字选适配器。**没有配置就明确 not_configured**
# ---------------------------------------------------------------------------

class AdapterRouter:
    """按名字把请求分发到一个适配器。**纯查表，不做任何调用。**"""

    def __init__(self, adapters: dict | None = None, default: str | None = None) -> None:
        self._adapters: dict[str, ModelAdapter] = dict(adapters or {})
        self._default = default

    def register(self, adapter: ModelAdapter, *, make_default: bool = False) -> None:
        self._adapters[adapter.name] = adapter
        if make_default or self._default is None:
            self._default = adapter.name

    def unregister(self, name: str) -> None:
        self._adapters.pop(name, None)
        if self._default == name:
            self._default = next(iter(self._adapters), None)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self) -> list[dict]:
        """列出已注册适配器及可用性 —— 供 CLI/doctor 展示，不触发调用。"""
        out = []
        for name in self.names():
            ad = self._adapters[name]
            ok, why = ad.available()
            out.append({"name": name, "available": ok, "reason": why,
                        "default": name == self._default})
        return out

    def resolve(self, name: str | None = None) -> ModelAdapter | None:
        return self._adapters.get(name or self._default or "")

    def generate(self, req: ModelRequest, *, name: str | None = None) -> ModelReply:
        """路由到一个适配器并调用。**没有任何适配器时返回 not_configured。**

        这是降级的唯一入口：没配置 ≠ 崩溃，也 ≠ 假装成功。
        """
        adapter = self.resolve(name or req.model_id or None)
        if adapter is None:
            return _fail("-", STATUS_NOT_CONFIGURED,
                         f"没有可用的模型适配器（已注册：{self.names() or '无'}）")
        ok, why = adapter.available()
        if not ok:
            return _fail(adapter.name, STATUS_NOT_CONFIGURED, why)
        return adapter.generate(req)


def build_router(settings=None) -> AdapterRouter:
    """从配置构建路由。**默认返回一个空路由**（全部 not_configured）。

    刻意**不读 settings.json 的新字段**，也**不注册任何适配器**：
    本阶段默认"没有模型"，这正是要求的行为 —— 没有模型时状态检查与基础规则照常工作。

    要启用某个适配器，调用方显式注册，例如：

        router = build_router()
        router.register(ExistingClientAdapter(session), make_default=True)

    未来若要按配置启用，只在这里加读取逻辑即可，调用方无需改动。
    """
    router = AdapterRouter()
    cfg = getattr(settings, "model_adapter", None) if settings is not None else None
    if isinstance(cfg, dict):
        # 只为"预留的配置入口"留位置：**本阶段不认识任何字段，也不会启用任何适配器。**
        # 未实现的适配器即便被点名，也仍然返回 not_configured（见其 available()）。
        for name in (cfg.get("enabled") or []):
            if name == LocalModelAdapter.name:
                router.register(LocalModelAdapter())
            elif name == FreeModelAdapter.name:
                router.register(FreeModelAdapter())
    return router


# ---------------------------------------------------------------------------
# 心跳扩展点对接
# ---------------------------------------------------------------------------

def as_heartbeat_adapter(router: AdapterRouter | None = None):
    """把路由包装成 **Phase B 预留的** `heartbeat.MODEL_ADAPTER` 形状。

    Phase B 规定的接口是 `available() -> bool` 与 `generate(prompt) -> str`。
    这里提供一个符合该形状的对象，但**心跳当前不会调用它** ——
    Phase B 明确"注册了适配器也不会被调用"。

    ⚠️ `generate(prompt)` 在失败时**抛 `ModelAdapterError`**，而不是返回空串 ——
    因为 Phase B 的接口形状没有状态字段，返回空串会让调用方误以为"模型说了空话"。
    """
    _router = router if router is not None else build_router()

    class _HeartbeatAdapter:
        name = "adapter-router"

        def available(self) -> bool:
            return _router.resolve() is not None

        def generate(self, prompt: str) -> str:
            rep = _router.generate(ModelRequest(prompt=prompt, purpose="heartbeat"))
            if not rep.ok:
                raise ModelAdapterError(rep.status, rep.error, adapter=rep.adapter)
            return rep.text

    return _HeartbeatAdapter()


class ModelAdapterError(RuntimeError):
    """适配层对外抛出的唯一异常。**带上 status 与 adapter，保证可追溯。**"""

    def __init__(self, status: str, message: str, *, adapter: str = "") -> None:
        super().__init__(f"[{status}] {message}" if status else message)
        self.status = status
        self.adapter = adapter
        self.message = message