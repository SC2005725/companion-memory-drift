"""人机感 A/B 渲染器（外部调研 #2 / PX-3 工具）：同一份数据，两种提示词形态。

目的：验证「叙述式提示词 vs JSON 结构式提示词」哪个更能压住 Spark/模型的人机感——
先渲染出两版 system 段样本，人工或盲测对比；**不改产线任何代码**。

数据（全部只读）：
  - 人格：core/persona-heci.json
  - 关系：relationship/ 最新 state_vNNN.json（stage/trust/closeness）
  - 用户模型：persona profile（confirmed/inferred）
  - 记忆：memory 最新版本，按 importance 取 top 5

用法：
    python src/persona_ab.py                    # 两种形态打印到 stdout
    python src/persona_ab.py --out ab_out       # 同时落盘到 ab_out/ 目录
    （A/B 的「真跑」= 把两版 prompt 分别贴进对话环境问同一组问题——老板当评委）
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import paths  # noqa: E402

import memory as memory_mod  # noqa: E402
import persona as persona_mod  # noqa: E402


def _latest_relationship() -> dict | None:
    d = Path(paths.RELATIONSHIP_DIR)
    if not d.is_dir():
        return None
    files = sorted(d.glob("state_v*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _top_memories(k: int = 5) -> list[str]:
    mv = memory_mod.load_latest()
    if mv is None or not mv.items:
        return []
    items = sorted(mv.items, key=lambda it: -memory_mod.importance(it))
    return [f"{it.type}：{it.text}" for it in items[:k]]


def gather() -> dict:
    """聚合同一份原始数据（只读）。"""
    persona_file = Path(paths.ROOT) / "core" / "persona-heci.json"
    persona = {}
    if persona_file.is_file():
        try:
            persona = json.loads(persona_file.read_text(encoding="utf-8"))
        except ValueError:
            pass
    profile = persona_mod.load_profile() or {}
    # profile 的真实 schema（2026-11-07 实测）：分节字符串列表
    # （self_view / life_history / left_open / values_detail / boundaries / drives...）
    # ——不是 confirmed/inferred（那是 user_model_events 的 claim 结构）。
    um_items = []
    # self_view 是短标签（may_differ 等），跳过；只取像句子的分节（>8 字）
    for section in ("life_history", "left_open", "values_detail", "self_view"):
        for text in (profile.get(section) or []):
            if isinstance(text, str) and len(text.strip()) > 8:
                um_items.append({"section": section, "text": text.strip()})
    rel_raw = _latest_relationship() or {}
    # state_vNNN.json 的真实结构：{"created_at", "note", "state": {...}, "version"}
    # —— stage/trust/closeness 在 state 键里（2026-11-07 实测修正）。
    rel = rel_raw.get("state", rel_raw) if isinstance(rel_raw, dict) else {}
    return {
        "persona": persona,
        "relationship": rel if isinstance(rel, dict) else {},
        "user_model": um_items,
        "memories": _top_memories(),
    }


def render_json_style(data: dict) -> str:
    """现产线风格：结构化 JSON 分层（便于机器核对，人读起来像配置）。"""
    rel = data["relationship"] or {}
    um = data["user_model"]
    mem = data["memories"]
    persona = data["persona"] or {}
    return json.dumps({
        "人格": {k: persona.get(k) for k in
                 ("name", "description", "personality", "values", "communication_style")},
        "关系": {"stage": rel.get("stage"), "trust": rel.get("trust"),
                 "closeness": rel.get("closeness")},
        "用户模型": um,
        "长期记忆": mem,
    }, ensure_ascii=False, indent=1)


def render_narrative(data: dict) -> str:
    """叙述式：同样的数据说成人话（调研 §0 预算 ~80 token）。

    依据：关系人格组调研 #4——Spark 对叙述比 JSON 数值敏感（A/B 待验证）。
    """
    rel = data["relationship"] or {}
    stage = rel.get("stage", "初识")
    trust = rel.get("trust", 50)
    closeness = rel.get("closeness", 50)
    um = data["user_model"]
    mem = data["memories"]
    persona = data["persona"] or {}
    name = persona.get("name", "鹤辞")
    desc = persona.get("description", "")
    style = persona.get("communication_style", "")
    um_line = "；".join(u["text"] for u in um[:4]) or "还在了解中"
    mem_line = "；".join(m.rstrip("。") for m in mem[:4]) or "还没有值得提起的旧事"
    return (
        f"你是{name}——{desc}\n"
        f"你与用户的关系已经走到「{stage}」：信任 {trust:.0f}/100，亲密 {closeness:.0f}/100。"
        f"以这个亲疏程度说话，不过分客套也不过分亲昵。\n"
        f"你对用户的了解：{um_line}。\n"
        f"你记得的事：{mem_line}。\n"
        f"说话方式：{style}\n"
        f"像朋友一样自然地回复，别像客服。"
    )


def main() -> int:
    out_dir = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        out_dir = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else Path("ab_out")
        out_dir.mkdir(parents=True, exist_ok=True)

    data = gather()
    variants = {"json": render_json_style(data), "narrative": render_narrative(data)}
    for name, text in variants.items():
        print(f"\n{'=' * 30} 形态：{name} {'=' * 30}\n{text}")
    if out_dir:
        for name, text in variants.items():
            (out_dir / f"system-{name}.md").write_text(text, encoding="utf-8")
        print(f"\n已落盘：{out_dir}/system-{{json,narrative}}.md")
    print("\n用法：把两版分别作为 system 提示问同一组问题，对比人机感（盲测更佳）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
