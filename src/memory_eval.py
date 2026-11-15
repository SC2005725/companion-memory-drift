"""记忆检索评测集（外部调研 #17）——记忆系统的回归尺。

14 个 QA 对全部来自真实记忆条目（v014）：问题 → 期望命中的条目 ID。
判分 = `retrieve_scored` top-5 内出现任一期望 ID。
**每次改检索/整理策略前后各跑一遍**——「整理即丢失」当场现形，
记忆优化的收益也有了度量衡（外部深度调研 §1.7 的本地化落地）。

用法：
    python src/memory_eval.py            # 对真实最新记忆版本跑评测（只读，零成本）
    python src/memory_eval.py --json     # 输出 JSON
    python src/memory_eval.py --selftest # 合成数据自检（不碰真实记忆）
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

import memory as memory_mod  # noqa: E402

DEFAULT_QA = (
    {"q": "用户的网名叫什么？", "expect": ["mem_b2120e03c81c"]},
    {"q": "那盆茉莉是哪里买的？", "expect": ["mem_bc32c8837002"]},
    {"q": "茉莉冬天要怎么处理？", "expect": ["mem_0bf68fb45bce"]},
    {"q": "茉莉现在放在哪？", "expect": ["mem_c31c4c461d37"]},
    {"q": "茉莉大概什么时候开花？", "expect": ["mem_bf6333e796b6"]},
    {"q": "茉莉掉花苞之后怎么样了？", "expect": ["mem_37e30313a75c"]},
    {"q": "用户现在还熬夜吗？", "expect": ["mem_056c21a52864"]},
    {"q": "用户平时喝什么？", "expect": ["mem_1fc9f64ca7fd", "mem_8a1fb50f2271"]},
    {"q": "用户读什么专业？", "expect": ["mem_1bca7671b682"]},
    {"q": "用户最近在看什么书？", "expect": ["mem_337022f89a22"]},
    {"q": "上次的比赛结果怎么样？", "expect": ["mem_98af13142d9d"]},
    {"q": "用户怎么形容自己的编程方式？", "expect": ["mem_959a0783b8d5"]},
    {"q": "用户抱怨过聊天的什么问题？", "expect": ["mem_6377dae7ad5e"]},
    {"q": "用户在研究什么机制？", "expect": ["mem_7745427ec0e0"]},
)


def load_qa() -> list[dict]:
    """读评测集：config/memory_eval_qa.json 优先（老板可编辑），缺失用内置。"""
    qf = SRC.parent / "config" / "memory_eval_qa.json"
    try:
        data = json.loads(qf.read_text(encoding="utf-8"))
        qa = data.get("qa") or []
        qa = [x for x in qa if isinstance(x, dict) and x.get("q") and x.get("expect")]
        if qa:
            return qa
    except (OSError, ValueError):
        pass
    return [dict(x) for x in DEFAULT_QA]


def run_eval(mv=None, *, qa: list[dict] | None = None, top_k: int = 5,
             now=None) -> dict:
    """对给定记忆版本跑 QA。qa=None 时用评测集；mv=None 时加载最新真实版本（只读）。"""
    mv = mv if mv is not None else memory_mod.load_latest()
    qa = qa if qa is not None else load_qa()
    results, hits = [], 0
    for item in qa:
        scored = memory_mod.retrieve_scored(mv, item["q"], top_k=top_k, now=now)
        got_ids = [m_it.id for m_it, _ in scored]
        hit = any(e in got_ids for e in item["expect"])
        results.append({"q": item["q"], "expect": item["expect"],
                        "hit": hit, "top": got_ids[:3]})
        hits += 1 if hit else 0
    misses = [r for r in results if not r["hit"]]
    return {"total": len(results), "hits": hits,
            "hit_rate": round(hits / len(results), 4) if results else 0.0,
            "misses": misses, "results": results}


def print_report(rep: dict) -> None:
    print("=" * 70)
    print(f"记忆检索评测 —— {rep['hits']}/{rep['total']} 命中（命中率 {rep['hit_rate']:.0%}）")
    print("=" * 70)
    for r in rep["results"]:
        mark = "✅" if r["hit"] else "❌"
        print(f"{mark} {r['q']}  ← 期望 {r['expect']}")
        if not r["hit"]:
            print(f"     实际 top3: {r['top']}")
    if rep["misses"]:
        print(f"\n未命中 {len(rep['misses'])} 条（检索/整理策略的改进目标）")


def selftest() -> int:
    """合成数据自检：验证评测器本身（命中判定/漏判记录），不碰真实记忆。"""
    from datetime import datetime, timezone

    mv = memory_mod.MemoryVersion(
        version="v_eval", created_at=datetime.now(timezone.utc).isoformat(),
        model="selftest",
        items=[
            memory_mod.MemoryItem(id="ev_jasmine", type="FACT",
                                  text="用户养了一盆茉莉，冬天怕冷。",
                                  source_event_ids=[]),
            memory_mod.MemoryItem(id="ev_night", type="FACT",
                                  text="用户以前上夜班，现在作息正常。",
                                  source_event_ids=[]),
        ],
        previous_version=None,
    )
    print("=" * 70)
    print("记忆评测集 · 合成数据自检")
    print("=" * 70)
    ok = True
    qa_self = [
        {"q": "茉莉怕不怕冷", "expect": ["ev_jasmine"]},
        {"q": "用户上没上过夜班", "expect": ["ev_night"]},
    ]
    r1 = run_eval(mv=mv, qa=qa_self)
    exp1 = {r["q"]: r["hit"] for r in r1["results"]}
    ok &= exp1.get("茉莉怕不怕冷") is True
    ok &= exp1.get("用户上没上过夜班") is True
    print(("✅" if exp1.get("茉莉怕不怕冷") else "❌"), "命中：茉莉问题")
    print(("✅" if exp1.get("用户上没上过夜班") else "❌"), "命中：夜班问题")

    mv2 = memory_mod.MemoryVersion(
        version="v_eval2", created_at=datetime.now(timezone.utc).isoformat(),
        model="selftest",
        items=[memory_mod.MemoryItem(id="ev_mocha", type="FACT",
                                     text="用户点了茉莉花拿铁，觉得不好喝。",
                                     source_event_ids=[])],
        previous_version=None,
    )
    qa_neg = [{"q": "茉莉怕不怕冷", "expect": ["ev_jasmine"]}]
    r2 = run_eval(mv=mv2, qa=qa_neg)
    exp2 = {r["q"]: r["hit"] for r in r2["results"]}
    check_jasmine = exp2.get("茉莉怕不怕冷")
    print(("✅" if check_jasmine is False else "❌"),
          "负例：换一版没有该条目的记忆 → 应未命中", str(check_jasmine))
    ok &= check_jasmine is False
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    rep = run_eval()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
