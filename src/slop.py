"""中文 AI 味（slop）检测器 —— 人格回归 eval v0 的确定性指标。

来源：外部深度调研（EQ-Bench Slop Score / Antislop 思路的中文最小实现），
词表在 `config/slop_zh.txt`（一行一条，# 注释）；文件缺失时回退到内置表。
基线（2026-11-07，349 轮真实对话挖掘）：348 条回复命中 1 处 —— 本地鹤辞语气基线良好，
**回归标准 = 强短语命中率不得高于基线**。

设计原则：
  - 零依赖、零 API 成本：纯子串计数，随时可跑，不花钱
  - 只做确定性指标：LLM-as-judge 属于 v1（要花钱，另开开关）
  - 词表是数据不是代码：加词条改 config 文件即可，不用改本模块

用法：
    import slop
    hits = slop.scan_text("作为一个AI，希望这能帮到你")
    report = slop.report_turns(texts)                 # 聚合报告
    python src/selftest_persona.py --live 50          # 对真实库最近 50 轮出报告（只读）
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import paths

# 内置兜底词表（config/slop_zh.txt 缺失时使用；两处保持同步）
EMBEDDED_DEFAULTS = (
    "作为一个AI", "作为一个 AI", "作为你的AI", "作为你的 AI",
    "希望这能帮到你", "希望能帮到你", "希望对你有所帮助",
    "总的来说", "综上所述", "总而言之", "简而言之",
    "值得注意的是", "需要注意的是", "毫无疑问",
    "从某种意义上说", "在很大程度上", "根据你的要求",
    "如果你需要的话", "如果你想要的话", "我可以帮你",
    "很抱歉给你带来", "值得一提的是", "不得不说",
)

SLOP_FILE = Path(paths.CONFIG_DIR) / "slop_zh.txt"


def load_patterns(path: Path | None = None) -> list[str]:
    """读词表：一行一条，# 注释，忽略空行。文件缺失 → 内置兜底表。"""
    p = Path(path) if path else SLOP_FILE
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return list(EMBEDDED_DEFAULTS)
    out = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out or list(EMBEDDED_DEFAULTS)


def scan_text(text: str, patterns: list[str] | None = None) -> list[tuple[str, int]]:
    """单条文本的 slop 命中：[(短语, 次数), ...]，按次数降序。无命中返回 []。"""
    if not text:
        return []
    pats = patterns if patterns is not None else load_patterns()
    hits = []
    for p in pats:
        n = text.count(p)
        if n:
            hits.append((p, n))
    hits.sort(key=lambda x: -x[1])
    return hits


def report_turns(texts: list[str], patterns: list[str] | None = None) -> dict:
    """聚合一批回复：命中率 / 短语分布 / 长度分布。纯函数，不碰库。"""
    pats = patterns if patterns is not None else load_patterns()
    per_turn, by_phrase = [], Counter()
    for t in texts:
        hits = scan_text(t, pats)
        if hits:
            per_turn.append({"text_head": (t or "")[:40], "hits": hits})
            for phrase, n in hits:
                by_phrase[phrase] += n
    lens = sorted(len(t) for t in texts if t)
    return {
        "turns": len(texts),
        "turns_with_hits": len(per_turn),
        "hit_rate": round(len(per_turn) / len(texts), 4) if texts else 0.0,
        "hits_total": sum(n for _, n in by_phrase.items()),
        "by_phrase": dict(by_phrase.most_common()),
        "len_p50": lens[len(lens) // 2] if lens else 0,
        "len_p90": lens[min(len(lens) - 1, int(len(lens) * 0.9))] if lens else 0,
        "per_turn": per_turn,
    }


def report_db(db_file: Path | None = None, last_n: int = 50) -> dict:
    """对真实对话库的最近 N 轮 assistant_text 出报告。**只读，零写入**。"""
    import sqlite3

    from paths import DB_FILE
    db = Path(db_file) if db_file else Path(DB_FILE)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT assistant_text FROM conversations "
        "WHERE assistant_text IS NOT NULL AND assistant_text != '' "
        "ORDER BY seq DESC LIMIT ?", (int(last_n),)).fetchall()
    conn.close()
    return report_turns([r[0] for r in rows])
