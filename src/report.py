"""生成「原始事实 vs 各记忆版本 vs 最终回忆」对比报告。

这是实验的最终产出。判定口径以 `experiments/人工标注事实清单.md` 的基准为准。

用法：
    python src\\report.py              输出到 experiments/对比报告.md
    python src\\report.py --print      同时打印到屏幕
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import memory as memory_mod  # noqa: E402
import storage  # noqa: E402
from paths import EXPERIMENTS_DIR, RECALL_DIR, ensure_dirs  # noqa: E402


# 基准事实的判定关键词。与 experiments/人工标注事实清单.md 同源，
# 改这里要同步改那份清单（清单本身由 seed_baseline.py 从 JSON 定义生成）。
FACTS = [
    ("B1", "买的时间是「上个月」，地点是「花市」", ["花市"]),
    ("B2", "卖家说这盆茉莉是「去年的苗」（二手信息）", ["去年的苗"]),
    ("B3", "它「特别怕冷」，「冬天得搬进屋里」", ["怕冷"]),
    ("B4a", "位置：基准写「书桌旁边的窗台」，用户后改口为「阳台」", ["阳台", "窗台"]),
    ("B4b", "开花时点：「大概六月份」", ["六月"]),
    ("B4c", "近期事件：「前两天掉了几个花苞」", ["花苞"]),
]


def fact_in_text(keywords: list[str], text: str) -> bool:
    return any(k in text for k in keywords)


def analyze_version(items: list) -> dict:
    """逐条基准事实判定：保留 / 缺失。"""
    result = {}
    for fid, label, kws in FACTS:
        hits = [it for it in items if fact_in_text(kws, it.text)]
        result[fid] = {
            "found": bool(hits),
            "count": len(hits),
            "texts": [it.text for it in hits],
            "types": [it.type for it in hits],
        }
    return result


def load_probes() -> list[dict]:
    if not RECALL_DIR.exists():
        return []
    out = []
    for path in sorted(RECALL_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def build_report() -> str:
    ensure_dirs()
    conn = storage.connect()
    rows = storage.list_memory_versions(conn)
    conn.close()

    versions = [(r["version"], memory_mod.load_version(r["version"])) for r in rows]
    probes = load_probes()

    lines = [
        "# 记忆偏移实验 · 对比报告",
        "",
        "> 本文件由 `src/report.py` 自动生成。判定基准见",
        "> `experiments/人工标注事实清单.md`。",
        ">",
        "> **重要**：条目级 diff（changelog）不可靠 —— 模型每次整理会重新分段，",
        "> 条目之间不是一一对应关系。所以本报告以「基准事实存活率」为主要判据。",
        "",
        f"- 记忆版本数：{len(versions)}",
        f"- 探针记录数：{len(probes)}",
        "",
        "---",
        "",
        "## 一、基准事实存活总表",
        "",
    ]

    header = "| # | 基准事实 | " + " | ".join(v for v, _ in versions) + " | 最终回忆 |"
    lines.append(header)
    lines.append("|---|---------|" + "|".join(["------"] * len(versions)) + "|---------|")

    probe_mem = [p for p in probes if p.get("mode") == "memory"]
    final_answer = probe_mem[-1]["answer"] if probe_mem else ""

    analysis = {v: analyze_version(mv.items) for v, mv in versions}

    for fid, label, kws in FACTS:
        cells = []
        for v, _ in versions:
            cells.append("保留" if analysis[v][fid]["found"] else "**缺失**")
        final_cell = "提及" if fact_in_text(kws, final_answer) else "未提及"
        lines.append(f"| {fid} | {label} | " + " | ".join(cells) + f" | {final_cell} |")

    # 统计
    total = len(FACTS) * len(versions)
    kept = sum(1 for v, _ in versions for fid, _, _ in FACTS if analysis[v][fid]["found"])
    lines += [
        "",
        f"**各版本合计：{kept} / {total} 条基准事实存活"
        f"（{kept / total:.0%}）。**",
        "",
        "---",
        "",
        "## 二、最终回忆（探针）",
        "",
    ]

    for p in probes:
        mode = p.get("mode")
        title = {
            "memory": "探针 A · 注入整版记忆（不注入原始对话）",
            "none": "探针 B · 什么都不注入（对照组）",
        }.get(mode, f"探针（{mode}）")
        lines += [
            f"### {title}",
            "",
            f"- 注入的记忆版本：`{p.get('memory_version_injected') or '无'}`"
            + (f"（整版 {len(p.get('memory_items_injected') or [])} 条）" if mode == "memory" else ""),
            f"- 注入原始对话：`{p.get('injected_history')}` ← 必须为 False",
            f"- 提问：{p.get('question')}",
            f"- **回答：{p.get('answer')}**",
            f"- 落盘：`{p.get('file')}`",
            "",
        ]

    # 逐条判定最终回忆
    if final_answer:
        lines += ["### 最终回忆逐条判定", "", "| # | 事实 | 最终回忆是否提及 |", "|---|------|----------------|"]
        for fid, label, kws in FACTS:
            lines.append(f"| {fid} | {label} | {'提及' if fact_in_text(kws, final_answer) else '**未提及**'} |")
        lines.append("")

    # 对照组的意义
    none_probe = [p for p in probes if p.get("mode") == "none"]
    if none_probe:
        lines += [
            "### 对照组的意义",
            "",
            "对照组**什么都不注入**。它的回答用于确认：",
            "",
            "- 探针 A 答对的内容**确实来自注入的记忆**，而不是模型的先验知识或猜测",
            "- 模型在没有记忆时**不编造**（这点很重要，否则「答对」也可能是蒙的）",
            "",
        ]

    # 各版本条目明细
    lines += ["---", "", "## 三、各版本记忆条目明细", ""]
    for v, mv in versions:
        lines += [f"### {v}（{len(mv.items)} 条）", ""]
        for it in mv.items:
            src = "、".join(it.source_event_ids) or "无来源"
            lines.append(f"- `[{it.type}]` {it.text}")
            lines.append(f"  - id={it.id}　来源={src}　confidence={it.confidence}")
        lines.append("")

    # 版本差异（辅助）
    lines += [
        "---",
        "",
        "## 四、版本差异（辅助参考，**不作为结论**）",
        "",
        "changelog 能可靠抓到的三类：**证据等级变化**（type_changed）、",
        "**事实合并**（merged）、**真丢失**（removed）。",
        "它的条目配对结果不可靠（原因见本文件开头说明），请只参考这三类。",
        "",
    ]
    from paths import CHANGELOG_DIR

    for path in sorted(CHANGELOG_DIR.glob("v*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        c = rec.get("counts", {})
        lines.append(
            f"- `{path.stem}`：类型变化 {c.get('type_changed', 0)}　"
            f"合并 {c.get('merged', 0)}　真丢失 {c.get('removed', 0)}　"
            f"被并入 {c.get('absorbed', 0)}"
        )
    lines.append("")

    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成对比报告")
    parser.add_argument("--print", dest="show", action="store_true", help="同时打印到屏幕")
    args = parser.parse_args(argv)

    text = build_report()
    out = EXPERIMENTS_DIR / "对比报告.md"
    out.write_text(text, encoding="utf-8")
    print(f"报告已生成：experiments/{out.name}")
    if args.show:
        print()
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
