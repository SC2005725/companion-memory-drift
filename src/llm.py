"""LLM 调用层（Phase 1）。

只用 OpenAI 兼容接口，不绑定任何一家厂商：改 .env 里的 LLM_BASE_URL 和 LLM_MODEL
就能换供应商，代码不用动。

同时把 API 返回的 usage 原样取回来，交给 pricing 记账。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """LLM 调用失败。

    F-1（费用可信度批 2026-11-14）：失败前已真实发生的用量挂在 `usage` 上
    （已发出的 HTTP attempt 数在 `attempts`），由统一出口 call_model 兜底入账
    —— **失败的调用也可能真实扣费**（典型：思考吃满预算→重试仍空→抛错，
    这两次请求服务端都已计费），不能因为抛异常就漏记。
    """

    def __init__(self, message: str, *, usage: "Usage | None" = None, attempts: int = 0):
        super().__init__(message)
        self.usage = usage
        self.attempts = attempts


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    model: str
    usage: Usage
    latency_ms: int
    finish_reason: str | None = None
    attempts: int = 1
    retried_for_empty: bool = False


def _extract_cache_hit(usage_obj) -> int:
    """各家 SDK / 供应商把「缓存命中的输入 tokens」放在不同位置，逐个尝试。"""
    if usage_obj is None:
        return 0

    def get(name):
        try:
            value = getattr(usage_obj, name)
        except Exception:
            return None
        return value if isinstance(value, int) else None

    for name in ("prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"):
        v = get(name)
        if v is not None:
            return v

    details = None
    for name in ("prompt_tokens_details", "input_tokens_details", "prompt_cache_details"):
        try:
            details = getattr(usage_obj, name)
        except Exception:
            details = None
        if details is not None:
            break
    if details is not None:
        for name in ("cached_tokens", "cache_hit_tokens"):
            v = getattr(details, name, None)
            if isinstance(v, int):
                return v

    dump = _as_dict(usage_obj)
    for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"):
        v = dump.get(key)
        if isinstance(v, int):
            return v
    for key in ("prompt_tokens_details", "input_tokens_details"):
        sub = dump.get(key)
        if isinstance(sub, dict):
            v = sub.get("cached_tokens") or sub.get("cache_hit_tokens")
            if isinstance(v, int):
                return v
    return 0


def _as_dict(obj) -> dict:
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    return {}


def _usage_from_resp(resp) -> Usage:
    """从一次响应里取用量（F-1：每个 attempt 都要取，不再只看最后一次）。"""
    usage_obj = getattr(resp, "usage", None)
    return Usage(
        prompt_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
        cache_hit_tokens=_extract_cache_hit(usage_obj),
        raw=_as_dict(usage_obj),
    )


def _reasoning_tokens_of(raw: dict) -> int:
    """取 raw 里的思考 token 数（口径与 chat._extract_reasoning_tokens 一致）。"""
    details = raw.get("completion_tokens_details") if isinstance(raw, dict) else None
    if isinstance(details, dict):
        v = details.get("reasoning_tokens")
        if isinstance(v, int):
            return v
    v = raw.get("reasoning_tokens") if isinstance(raw, dict) else None
    return v if isinstance(v, int) else 0


def _merge_usage(first: Usage, last: Usage) -> Usage:
    """合并多次 attempt 的用量（F-1）：token 相加，费用不再漏掉第一次请求。

    raw 保留末次原文 + 首次存档在 `first_attempt_usage`，审计时可还原逐次明细。
    思考 token（藏在 raw.completion_tokens_details.reasoning_tokens）单独相加，
    相加后仍是单一数值，chat._extract_reasoning_tokens 的记账口径不破。
    """
    raw = dict(last.raw or {})
    if first.raw:
        raw["first_attempt_usage"] = dict(first.raw)
    r_first = _reasoning_tokens_of(first.raw or {})
    r_last = _reasoning_tokens_of(last.raw or {})
    if r_first or r_last:
        details = dict(raw.get("completion_tokens_details") or {})
        details["reasoning_tokens"] = r_first + r_last
        raw["completion_tokens_details"] = details
    return Usage(
        prompt_tokens=first.prompt_tokens + last.prompt_tokens,
        completion_tokens=first.completion_tokens + last.completion_tokens,
        cache_hit_tokens=first.cache_hit_tokens + last.cache_hit_tokens,
        raw=raw,
    )


def _extract_text(message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            else:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return ""


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
        empty_retry_max_tokens: int = 0,
    ):
        if not api_key:
            raise LLMError(
                "没有读到 API Key。请把 .env.example 复制成 .env，并在 LLM_API_KEY= 后面填上你的 Key。"
            )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # 「被截断且无内容」时重试用的更大预算。
        # 默认取 max_tokens 的 4 倍（至少 3000），0 表示关闭重试。
        self.empty_retry_max_tokens = (
            empty_retry_max_tokens
            if empty_retry_max_tokens > 0
            else max(3000, max_tokens * 4)
        )

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少依赖 openai，请先执行：pip install -r requirements.txt") from exc

        # 2026-11-21 事故：Windows 系统代理指向一个**没有运行的**本地代理端口
        # （注册表 ProxyServer=127.0.0.1:7890，代理软件未开），openai 客户端
        # 走 getproxies 拿到它 → 每次 LLM 调用 10061 连接拒绝。
        # 对策：按 LLM 端点的主机名精确绕过代理（DeepSeek 国内直连本就更稳），
        # 只改本进程的 NO_PROXY 环境变量，不动系统设置、不影响其他应用。
        # base_url 将来换成需要代理的境外端点时，这里会自动跟着换成新主机名。
        import os as _os
        from urllib.parse import urlparse as _urlparse
        _llm_host = (_urlparse(base_url).hostname or "").lower()
        if _llm_host:
            _current = _os.environ.get("NO_PROXY", _os.environ.get("no_proxy", ""))
            _parts = [p.strip().lower() for p in _current.split(",") if p.strip()]
            if "*" not in _parts and _llm_host not in _parts:
                _parts.append(_llm_host)
                _os.environ["NO_PROXY"] = ",".join(_parts)
                _os.environ["no_proxy"] = ",".join(_parts)

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        # 供 dryrun / 统计用：记录发生过多少次空回复重试
        self.empty_retries = 0

    def _raw_send(self, messages: list[dict], max_tokens: int, model: str | None = None):
        # 2026-11-04：429 自动退避。检测到限速不再直接作废这一轮，
        # 而是记下「限速到几点」→ 等 → 重试；rate_guard 的定时器会在
        # 到点自动解除闸门（不发探测请求，恢复由下一次真实调用验证）。
        import time

        import rate_guard

        g = rate_guard.guard()
        attempt = 0
        while True:
            # 上一次被限速、闸门还没开 → 先等，别白撞一次
            g.wait_if_limited()
            try:
                resp = self._client.chat.completions.create(
                    # 每次请求可以覆盖模型（Phase 10 Model Router 需要）。
                    # 不传就用构造时定的默认模型 —— 老调用方行为完全不变。
                    model=model or self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                )
                g.note_success()
                return resp
            except Exception as exc:
                if rate_guard.looks_like_429(exc) and attempt < g.max_retries:
                    attempt += 1
                    delay = g.note_limit(str(exc), rate_guard.retry_after_of(exc))
                    try:
                        import storage

                        storage.log_line(
                            f"rate limit 429：第 {attempt} 次命中，退避 {delay:.0f}s 后重试")
                    except Exception:  # noqa: BLE001
                        pass
                    # 写进主体事件账本 —— 这样限速不只在本进程可见，
                    # 另一个实例（Muse）也能拉到「这边正在被限速」。
                    try:
                        import core_capture

                        core_capture.record_rate_limit(
                            attempt=attempt, delay=delay, reason=str(exc))
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(delay)
                    continue
                raise LLMError(_friendly_error(exc)) from exc

    def send(self, messages: list[dict], model: str | None = None) -> Reply:
        """发一次请求，并在「被截断且无内容」时自动重试。

        ## 为什么需要这个重试（实测踩到的坑）

        `deepseek-flash` 是**推理模型**：它先在 `message.reasoning_content` 里产出思考，
        再填 `content`。思考 token 也计入 `max_tokens` 预算。

        实测事故：某轮对话 `completion_tokens` 正好等于上限 800，`content` 为空字符串
        —— 整个预算被思考吃光，模型一个字都没来得及说。
        对话记录里那一轮就成了空白回复。

        **为什么这对本实验是致命的**：如果回忆探针（「你还记得我之前说过的那件事吗？」）
        也这样，AI 会因为「想不完」而交出空回答，而分析时它会被记成**「记忆丢失」**
        —— 实际只是被截断了。这会直接伪造实验结论。

        所以：检测到「`finish_reason == 'length'` 且 content 为空」就加大预算重试一次。
        重试次数会记进 `Reply.retried_for_empty` 与 `LLMClient.empty_retries`，
        便于事后统计这种现象有多频繁。
        """
        started = time.perf_counter()
        # F-1（费用可信度批 2026-11-14）：用量按 attempt 累计。
        # 旧实现只读最后一次的 usage —— 第一次请求（已真实计费）被无声丢掉；
        # 重试抛错时更是两次全丢。现在每次 attempt 都取用量并合并，
        # 失败路径把累计用量挂在 LLMError 上，由 call_model 兜底入账。
        usage_total = Usage()
        attempts = 0
        retried = False

        try:
            resp = self._raw_send(messages, self.max_tokens, model)
        except LLMError as exc:
            exc.usage = exc.usage or usage_total
            exc.attempts = exc.attempts or attempts + 1
            raise
        attempts += 1
        usage_total = _merge_usage(usage_total, _usage_from_resp(resp))

        if not _has_content(resp):
            reason = _finish_reason(resp)
            if reason == "length" and self.empty_retry_max_tokens > self.max_tokens:
                # 预算被思考吃光 —— 加大预算重试
                try:
                    resp2 = self._raw_send(messages, self.empty_retry_max_tokens, model)
                except LLMError as exc:
                    exc.usage = exc.usage or usage_total
                    exc.attempts = exc.attempts or attempts
                    raise
                resp = resp2
                attempts += 1
                retried = True
                self.empty_retries += 1
                usage_total = _merge_usage(usage_total, _usage_from_resp(resp))

        latency_ms = int((time.perf_counter() - started) * 1000)

        if not getattr(resp, "choices", None):
            raise LLMError(
                "模型没有返回任何回复（choices 为空）。",
                usage=usage_total, attempts=attempts)

        text = _extract_text(resp.choices[0].message)
        usage = usage_total
        if not text.strip():
            # 重试后仍然为空 —— 不静默当作正常回复，明确报错让调用方决定怎么处理。
            raise LLMError(
                "模型返回了空内容"
                f"（finish_reason={_finish_reason(resp)!r}，"
                f"completion_tokens={usage.completion_tokens}，"
                f"重试={'是' if retried else '否'}）。"
                "推理模型的思考 token 会占用 max_tokens 预算，"
                "可调大 config/settings.json 的 max_tokens。",
                usage=usage, attempts=attempts,
            )
        return Reply(
            text=text,
            model=str(getattr(resp, "model", self.model) or self.model),
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=_finish_reason(resp),
            attempts=attempts,
            retried_for_empty=retried,
        )


def _finish_reason(resp) -> str | None:
    try:
        return resp.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None


def _has_content(resp) -> bool:
    try:
        return bool(_extract_text(resp.choices[0].message).strip())
    except (AttributeError, IndexError, TypeError):
        return False


def _friendly_error(exc: Exception) -> str:
    """把常见报错翻译成能直接照做的中文提示，避免让人对着堆栈猜。"""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()

    if "401" in text or "invalid api key" in low or "authentication" in low or "unauthorized" in low:
        return f"鉴权失败（{name}）：API Key 不正确或已失效。请检查 .env 里的 LLM_API_KEY。\n原始信息：{text}"
    if "insufficient balance" in low or "402" in text or "quota" in low:
        return f"账户余额不足（{name}）：请到服务商后台充值。\n原始信息：{text}"
    if "model" in low and ("not exist" in low or "not found" in low or "invalid" in low):
        return f"模型名不可用（{name}）：请检查 .env 里的 LLM_MODEL 是否拼写正确。\n原始信息：{text}"
    if "429" in text or "rate limit" in low:
        return f"触发限速（{name}）：稍等一会儿再试。\n原始信息：{text}"
    if "connection" in low or "timeout" in low or "timed out" in low or "ssl" in low:
        return f"网络连接失败（{name}）：请检查网络或代理设置，以及 LLM_BASE_URL 是否可达。\n原始信息：{text}"
    return f"调用模型失败（{name}）：{text}"
