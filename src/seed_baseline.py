"""固化实验基准事件（`raw_event_001`）。

## 为什么基准事件需要专门的命令，而不是走聊天

`raw_event_001` 是**实验的基准事实**，不是某次闲聊的产物。它的要求是：

- **逐字稳定** —— 每次重建都必须一字不差，否则各版本记忆不可比
- **ID 固定** —— 文档、实验脚本、记忆条目都要引用同一个 ID
- **可重复固化** —— 换电脑、重装后要能一模一样地重建

走聊天做不到这三点：聊天会生成随机 turn_id、随机 event_id，还会因对话上下文不同
而产生不同的原始事件文本。所以基准事件从**受控定义文件**固化：

    experiments/baseline-event.json

同时从同一份定义生成人工标注事实清单 —— 两处内容同源，不会互相漂移。

## 它不产生任何 API 调用，也不花钱

用法：
    python src\\main.py seed-baseline           固化（已存在则跳过）
    python src\\main.py seed-baseline --force   先删除旧的再重建（仅限尚未开始正式实验时）
"""

from __future__ import annotations

import json

import storage
from paths import EXPERIMENTS_DIR, RAW_EVENTS_DIR

FIXTURE_FILE = EXPERIMENTS_DIR / "baseline-event.json"
CHECKLIST_FILE = EXPERIMENTS_DIR / "ground-truth-facts.md"


class SeedError(RuntimeError):
    pass


def load_fixture() -> dict:
    if not FIXTURE_FILE.exists():
        raise SeedError(f"缺少基准事件定义文件：{FIXTURE_FILE.as_posix()}")
    return json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))


def seed(conn, *, force: bool = False) -> dict:
    """固化基准事件。返回结果摘要。**不调用任何模型，不花钱。**"""
    fx = load_fixture()
    event_id = fx["event_id"]
    text = fx["text"]

    existing_file = RAW_EVENTS_DIR / f"{event_id}.json"
    existing_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()

    if (existing_file.exists() or existing_row) and not force:
        return {
            "created": False,
            "event_id": event_id,
            "reason": "已存在，未改动。要重建请加 --force（仅限尚未开始正式实验时）。",
        }

    if force:
        # 只删这一个基准事件，不碰其他任何原始数据
        if existing_row:
            conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
            conn.commit()
        for suffix in (".json", ".sha256"):
            path = RAW_EVENTS_DIR / f"{event_id}{suffix}"
            if path.exists():
                path.unlink()

    # 按原文去重：同一段原话若已作为别的 ID 存在，说明重复固化了，直接报错
    dup = storage.find_event_by_text(conn, text)
    if dup and dup["event_id"] != event_id:
        raise SeedError(
            f"这段原话已经固化过了，ID 是 {dup['event_id']}。"
            "基准事件不应重复固化 —— 请检查是否误跑，或先处理那个事件。"
        )

    detection = {
        "method": "fixture",
        "keywords_configured": [],
        "matched": [],
        "hit": False,
        "note": "基准事件由 experiments/baseline-event.json 固化，"
                "不经过关键词触发规则，因此 matched 为空。",
    }

    record = storage.create_event(
        conn,
        session_id="sess_fixture",
        turn_id="turn_fixture",
        user_text=text,
        detection=detection,
        event_id=event_id,
    )

    checklist = write_checklist(fx)
    storage.log_line(f"baseline event seeded: {event_id} (fixture, {len(text)} chars)")

    return {
        "created": True,
        "event_id": event_id,
        "file": record["file"],
        "sha256": record["sha256"],
        "chars": len(text),
        "checklist": checklist,
        "facts": len(fx["facts"]),
    }


def write_checklist(fx: dict) -> str:
    """从同一份定义生成人工标注事实清单。

    两处内容同源，避免「定义改了但清单没改」这种不一致。
    """
    lines = [
        "# 人工标注事实清单（基准答案）",
        "",
        "> 本文件由 `src/seed_baseline.py` 从",
        "> `experiments/baseline-event.json` **自动生成**，请勿手工编辑 ——",
        "> 要改内容请改那份定义文件，然后重新执行 `python src\\main.py seed-baseline --force`。",
        "",
        f"- 基准事件：`{fx['event_id']}`",
        f"- 选定事件：候选 B · 花市买的一盆茉莉",
        f"- 选定日期：{fx['selected_at']}",
        f"- 事实条数：{len(fx['facts'])}（其中 B4 含 3 个可分别判定的子项）",
        "",
        "## 用途",
        "",
        "这是日后对比的**基准答案**。每次记忆整理（v001 / v002 / v003）与最终回忆",
        "都要逐条对照本清单，判定每条事实属于：**保留 / 改写 / 丢失 / 与推断混合**。",
        "",
        "## 原始原文（逐字，不可改写）",
        "",
        f"> {fx['text']}",
        "",
        f"（{len(fx['text'])} 字）",
        "",
        "## 时间锚点（重要）",
        "",
        fx["date_anchor"]["why"],
        "",
        "| 原文相对时间词 | 锚定绝对时点 |",
        "|---------------|-------------|",
    ]
    for k, v in fx["date_anchor"]["resolved"].items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "## 事实清单",
        "",
        "| # | 事实 | 证据等级 | 漂移探针（为什么值得观察） |",
        "|---|------|---------|------------------------|",
    ]
    for f in fx["facts"]:
        lines.append(f"| {f['id']} | {f['fact']} | {f['kind']} | {f['drift_probe']} |")

    lines += ["", "## 逐条判定要点", ""]
    for f in fx["facts"]:
        lines.append(f"### {f['id']} · {f['fact']}")
        lines.append("")
        lines.append(f"- 证据等级：**{f['kind']}**")
        kws = f.get("verdict_keywords") or []
        if kws:
            lines.append(f"- 判定关键词：{'、'.join(kws)}")
        elif f.get("subfacts"):
            lines.append("- 判定关键词：见下方各子项（本条由 3 个子项分别判定）")
        if f.get("note"):
            lines.append(f"- 备注：{f['note']}")
        if f.get("forbidden_drift"):
            lines.append("- 不允许出现的漂移：")
            for bad in f["forbidden_drift"]:
                lines.append(f"  - {bad}")
        for sub in f.get("subfacts", []):
            lines.append(f"- 子项 **{sub['id']}**：{sub['fact']}")
            sub_kws = sub.get("verdict_keywords") or []
            if sub_kws:
                lines.append(f"  - 判定关键词：{'、'.join(sub_kws)}")
            for bad in sub.get("forbidden_drift", []):
                lines.append(f"  - 不允许：{bad}")
        lines.append("")

    lines += [
        "## 四分类判定口径",
        "",
        "| 判定 | 含义 |",
        "|------|------|",
        "| **保留** | 关键名词/数字/方向/时间点与原文一致（措辞可不同） |",
        "| **改写** | 事实还在，但具体内容被替换（如「去年」→「两年」） |",
        "| **丢失** | 该条在记忆版本或回答里完全找不到 |",
        "| **与推断混合** | 事实与模型自己的推测混在一句里，无法区分哪部分是用户说的 |",
        "",
        "## 探针问题（逐字固定，全程不改）",
        "",
        f"> {fx['probes']['question']}",
        "",
        "**刻意不点名事件。** 写成「你还记得那盆茉莉吗？」会引导模型，污染结果。",
        "两种模式都必须跑，且**都不注入原始对话**（注入了测的就不是记忆）：",
        "",
        "```bat",
        "python src\\main.py recall --mode memory --label v001",
        "python src\\main.py recall --mode none   --label blank",
        "```",
        "",
        "## 隐私",
        "",
        fx["privacy"],
        "",
    ]

    CHECKLIST_FILE.write_text("\n".join(lines), encoding="utf-8")
    return f"experiments/{CHECKLIST_FILE.name}"
