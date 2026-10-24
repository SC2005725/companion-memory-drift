"""成本计算与账本（任务书十七节）。

两件事：
1. 按官方计价口径估算每次调用的费用（元/百万 tokens）；
2. 把每次调用追加写入 logs/ledger.jsonl，随时能回答「调用了几次、用了多少 token、花了多少钱」。

金额一律用「微元」（1e-6 元）整数累计，避免浮点累加误差。

注意：单价的**数值**来自 config/settings.json 的 cost.models（方便你自己改价），
但「哪些时段算高峰」的逻辑写在本模块的 PEAK_WINDOWS 里，settings.json 中的
peak_hours / peak_days 只是给人看的备注，不参与计算。
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime

from paths import LEDGER_FILE

PEAK_WINDOWS = ((dtime(9, 0), dtime(12, 0)), (dtime(14, 0), dtime(18, 0)))
MICRO = 1_000_000


def is_peak(when: datetime | None = None, override: bool | None = None) -> bool:
    """高峰时段：周一至周五 9:00-12:00、14:00-18:00；其余（含周末与节假日）为空闲。

    节假日需要人工判断，可用环境变量 LLM_PEAK 强制指定。
    """
    if override is not None:
        return override
    now = when or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(start <= t < end for start, end in PEAK_WINDOWS)


def _rates(cost_cfg: dict, model: str) -> dict | None:
    models = (cost_cfg or {}).get("models") or {}
    if model in models:
        return models[model]
    # 未登记模型不做猜测，返回 None，只记 token 不记金额。
    return None


def estimate_micro_yuan(
    *,
    cost_cfg: dict,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int,
    peak: bool,
) -> int | None:
    rates = _rates(cost_cfg, model)
    if not rates:
        return None

    factor = 1.0 if peak else 0.5
    cache_hit = max(0, min(cache_hit_tokens, prompt_tokens))
    cache_miss = max(0, prompt_tokens - cache_hit)

    per_million = (
        cache_hit * float(rates.get("cache_hit", 0.0))
        + cache_miss * float(rates.get("input", 0.0))
        + completion_tokens * float(rates.get("output", 0.0))
    )
    # 元/百万 → 微元
    return int(round(per_million * factor))


def format_yuan(micro: int | None) -> str:
    if micro is None:
        return "未计价"
    return f"{micro / MICRO:.6f} 元"


def append_ledger(record: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    out = []
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarize() -> dict:
    rows = read_ledger()
    total_micro = 0
    priced = 0
    unpriced = 0
    for r in rows:
        m = r.get("cost_micro_yuan")
        if m is None:
            unpriced += 1
        else:
            total_micro += int(m)
            priced += 1
    return {
        "calls": len(rows),
        "priced_calls": priced,
        "unpriced_calls": unpriced,
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in rows),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in rows),
        "cache_hit_tokens": sum(int(r.get("cache_hit_tokens") or 0) for r in rows),
        "total_micro_yuan": total_micro,
    }


def format_summary(s: dict) -> str:
    lines = [
        f"API 调用次数        : {s['calls']}",
        f"输入 tokens         : {s['prompt_tokens']}（其中缓存命中 {s['cache_hit_tokens']}）",
        f"输出 tokens         : {s['completion_tokens']}",
        f"合计 tokens         : {s['prompt_tokens'] + s['completion_tokens']}",
        f"估算费用            : {format_yuan(s['total_micro_yuan'])}",
    ]
    if s["unpriced_calls"]:
        lines.append(f"未计价调用          : {s['unpriced_calls']} 次（该模型未登记在 settings.json 的 cost.models 中）")
    lines.append("说明：费用为按官方公开单价的估算值，实际扣费以服务商账单为准。")
    return "\n".join(lines)
