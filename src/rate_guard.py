"""429 自动退避：检测到限速 → 进入退避 → 定时器到点自动恢复。

## 解决什么问题

上游 LLM 的 RPM 限速会返回 429。原来的行为是**直接抛错、这一轮作废**；
在人已经坐在屏幕前的情况下，等于白白丢一次对话。

现在改成：检测到 429 → 记住「限速到几点」→ 到点自动恢复 → 期间的调用
要么等、要么明确告诉调用方「现在别调」。**不需要人干预。**

## 阶梯退避

连续命中就往上加：10s → 20s → 40s → 60s → 120s → 300s（封顶）。
成功一次就归零 —— 否则偶发一次限速会让后面所有请求都被拖慢。

## 定时器做什么

`note_limit()` 会起一个 `threading.Timer`，到点自动把状态清成「正常」，
并记一条事件。**不主动发探测请求**（那要花钱）——定时器只负责解除闸门，
真正的恢复由下一次真实调用验证。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

# 退避阶梯（秒）。第一档要够短以免小事拖久，最后一档防雪崩。
LADDER = (10, 20, 40, 60, 120, 300)

# 一次调用最多自动重试几次（含首次）
MAX_AUTO_RETRIES = 3


def _now_ts() -> float:
    return time.time()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


def looks_like_429(exc: BaseException) -> bool:
    """判断异常是不是限速。SDK 的 status_code 优先，文本兜底。"""
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    if code == 429:
        return True
    text = str(exc).lower()
    return ("429" in text) or ("rate limit" in text) or ("too many requests" in text)


def retry_after_of(exc: BaseException) -> float | None:
    """上游如果给了 Retry-After，优先听它的。"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


class RateGuard:
    """进程内的限速状态机。多线程安全。"""

    def __init__(self, ladder: tuple[int, ...] = LADDER, max_retries: int = MAX_AUTO_RETRIES):
        self._lock = threading.RLock()
        self._ladder = tuple(ladder) or LADDER
        self.max_retries = max_retries
        self._limited_until = 0.0
        self._streak = 0            # 连续命中次数（成功即归零）
        self._hits = 0              # 累计命中
        self._last_reason = ""
        self._last_hit_at = 0.0
        self._timer: threading.Timer | None = None
        self._on_clear = None       # 可选回调：恢复时通知外部（记账用）

    # --- 查询 ---------------------------------------------------------------
    def remain(self) -> float:
        """还要等多少秒。0 表示不限速。"""
        with self._lock:
            return max(0.0, self._limited_until - _now_ts())

    def is_limited(self) -> bool:
        return self.remain() > 0

    def snapshot(self) -> dict:
        with self._lock:
            remain = max(0.0, self._limited_until - _now_ts())
            return {
                "limited": remain > 0,
                "remain_seconds": round(remain, 1),
                "until": _fmt(self._limited_until) if self._limited_until else None,
                "streak": self._streak,
                "total_hits": self._hits,
                "last_reason": self._last_reason[:200],
                "last_hit_at": _fmt(self._last_hit_at) if self._last_hit_at else None,
                "ladder": list(self._ladder),
                "max_retries": self.max_retries,
            }

    # --- 状态变更 -----------------------------------------------------------
    def note_limit(self, reason: str = "", retry_after: float | None = None) -> float:
        """记一次限速命中，返回**建议等待秒数**，并起定时器到点自动恢复。"""
        with self._lock:
            self._hits += 1
            self._streak += 1
            idx = min(self._streak - 1, len(self._ladder) - 1)
            delay = float(self._ladder[idx])
            if retry_after is not None and retry_after > delay:
                delay = float(retry_after)          # 上游说得更久就听它的
            delay = min(delay, float(self._ladder[-1]) * 2)
            self._limited_until = _now_ts() + delay
            self._last_reason = reason or "429 rate limit"
            self._last_hit_at = _now_ts()
            self._arm_timer(delay)
            return delay

    def note_success(self) -> None:
        """一次成功 → 阶梯归零，但不清除正在倒计时的闸门（让定时器自然到点）。"""
        with self._lock:
            self._streak = 0

    def clear(self, *, fire_callback: bool = True) -> None:
        with self._lock:
            was = self._limited_until
            self._limited_until = 0.0
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
                self._timer = None
            cb = self._on_clear
        if fire_callback and was and cb is not None:
            try:
                cb({"cleared_at": _fmt(_now_ts())})
            except Exception:  # noqa: BLE001
                pass

    # --- 定时器 -------------------------------------------------------------
    def _arm_timer(self, delay: float) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        t = threading.Timer(delay, self._timer_fire)
        t.daemon = True
        self._timer = t
        t.start()

    def _timer_fire(self) -> None:
        """到点：自动解除闸门。不发探测请求。"""
        self.clear(fire_callback=True)

    def set_on_clear(self, fn) -> None:
        self._on_clear = fn

    # --- 等待 ---------------------------------------------------------------
    def wait_if_limited(self, cap: float = 310.0) -> float:
        """如果正在限速就阻塞等到解除，返回实际等待秒数（0 表示没等）。"""
        remain = self.remain()
        if remain <= 0:
            return 0.0
        remain = min(remain, cap)
        time.sleep(remain)
        return remain


_GUARD: RateGuard | None = None
_GUARD_LOCK = threading.Lock()


def guard() -> RateGuard:
    """进程内单例。"""
    global _GUARD
    if _GUARD is None:
        with _GUARD_LOCK:
            if _GUARD is None:
                _GUARD = RateGuard()
    return _GUARD


if __name__ == "__main__":
    import io
    import json
    import sys
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    g = RateGuard(ladder=(1, 2), max_retries=3)
    print("初始:", g.snapshot()["limited"])
    d1 = g.note_limit("模拟 429", None)
    print(f"第一次命中 → 等 {d1}s，现在 limited={g.is_limited()}")
    d2 = g.note_limit("再来一次", None)
    print(f"第二次命中 → 等 {d2}s（阶梯上升）")
    time.sleep(2.2)
    print("定时器到点后 limited =", g.is_limited())