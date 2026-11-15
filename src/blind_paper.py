"""处方 D · 盲测卷生成器（2026-11-08，老板拍板「行」）。

对老板的承诺：把 11-08 那次 8/10 盲测法**固化**——每轮优化后出新卷，
老板 10 分钟当评委；LLM 评委只做回归检查（查 AI 腔反扑），不再给人味打分。

本脚本：同一组 10 个场景题，**裸人格** vs **五层注入**（含今晚新 EPISODE
经历，v015）各答一遍（真 API，20 次小请求），A/B 位置按种子随机，落盘：
    共生\\_协同\\_盲测卷-处方A验收-20261108.md       ← 老板答这份
    共生\\_协同\\_盲测卷-处方A验收-答案钥匙-20261108.md ← 测完对答案

诚实边界：
  · 直连 LLMClient 的调用**不入共生费用账本**（已知盲区 C2），成本估算
    20 次 × 小请求 ≈ 几分钱。
  · 题目 3/4/10 直接探今晚新经历（茉莉/共同完成/人格关系），其余为日常/
    工程混合——既测增量，也防「只会背新经历」的过拟合。
"""
from __future__ import annotations

import io
import random
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

# 种子可从命令行传入（复测卷用新种子：python src/blind_paper.py 20261108c）
SEED = sys.argv[1] if len(sys.argv) > 1 else "20261108"
TAG = "处方A验收" if SEED == "20261108" else "处方A复测"
OUT_DIR = SRC.parent.parent / "_协同"

QUESTIONS = [
    "在忙什么？",
    "还记得我那盆花吗？最近怎么样",
    "那天晚上咱们搞到挺晚的，你还记得吗？",
    "最近有什么想主动跟我说的吗？",
    "我这两天有点累。",
    "人格那块你打算怎么弄？",
    "周末有什么打算？",
    "你觉得咱们现在关系怎么样？",
    "要是我把项目先放一放，你会怎么想？",
    "讲一件你还记得的小事。",
]


def main() -> int:
    import os

    import llm as llm_mod
    import persona_eval
    from config import load_env, load_settings

    env = load_env()
    settings = load_settings()
    client = llm_mod.LLMClient(
        api_key=env.api_key,
        base_url=env.base_url,
        model=settings.model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )

    rng = random.Random(SEED)
    pairs: list[dict] = []
    for i, q in enumerate(QUESTIONS, 1):
        sp_bare = persona_eval.build_system_prompt(None)          # 裸：静态层
        sp_full = persona_eval.build_system_prompt(q)             # 五层：按题注入
        a_bare = client.send([{"role": "system", "content": sp_bare},
                              {"role": "user", "content": q}]).text
        a_full = client.send([{"role": "system", "content": sp_full},
                              {"role": "user", "content": q}]).text
        bare_first = rng.random() < 0.5
        pairs.append({"n": i, "q": q,
                      "A": a_bare if bare_first else a_full,
                      "B": a_full if bare_first else a_bare,
                      "A_is": "裸" if bare_first else "五层",
                      "B_is": "五层" if bare_first else "裸"})
        print(f"[{i}/10] 已生成（A={pairs[-1]['A_is']}）")

    lines = [
        "# 鹤辞盲测卷 · 处方 A 验收（A/B 共 10 题）",
        "",
        "> 出卷：2026-11-08（种子 " + str(SEED) + "）｜ 生成器：src/blind_paper.py",
        "> 对比对象：**裸人格** vs **五层注入（含今晚新经历 EPISODE v015）**。",
        "> 你只做一件事：**凭「哪句更像鹤辞」选 A 或 B**，全选完发我（如：1A 2B 3A…）。",
        "> 别纠结对错——测的是「人味」，不是标准答案。测完再看答案钥匙。",
        "",
    ]
    for p in pairs:
        lines.append(f"## 第 {p['n']} 题：{p['q']}")
        lines.append("")
        lines.append("**A.**")
        lines.append("```")
        lines.append(p["A"].strip())
        lines.append("```")
        lines.append("**B.**")
        lines.append("```")
        lines.append(p["B"].strip())
        lines.append("```")
        lines.append("")
    paper = OUT_DIR / f"_盲测卷-{TAG}-{SEED}.md"
    paper.write_text("\n".join(lines), encoding="utf-8")

    key_lines = [
        "# 盲测答案钥匙（测完再看！）· 处方 A 验收卷",
        "",
        f"> 种子 {SEED}，可复现。五层注入侧含 v015 的 4 段 EPISODE 经历",
        "> （链桥上线后的第一份记忆——第 2/3/6/10 题设计上会探到）。",
        "",
        "| 题 | A 位置 | B 位置 |",
        "|---|---|---|",
    ]
    for p in pairs:
        key_lines.append(f"| {p['n']} | {p['A_is']} | {p['B_is']} |")
    key_lines += [
        "",
        "## 计分口径（老板定的老规矩）",
        "",
        "- 五层胜 ≥8/10：处方 A 生效，处方 B（情绪数值）放行",
        "- 6-7/10：部分生效，看失手题是不是经历相关，再定 B 排期",
        "- ≤5/10：未过线——先查失手题根因（经历太少？检索没命中？围栏措辞？），B 缓上",
        "- 另记「失手题语境」：工程语境失手 = 双寄存器现象再现（上次 2/2）",
    ]
    key = OUT_DIR / f"_盲测卷-{TAG}-答案钥匙-{SEED}.md"
    key.write_text("\n".join(key_lines), encoding="utf-8")

    print(f"\n卷子：{paper}")
    print(f"钥匙：{key}")
    print("把卷子发给老板（10 题），收答案后按钥匙计分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
