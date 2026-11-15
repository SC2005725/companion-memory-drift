"""人格回归 eval v0 —— 「鹤辞答得像不像鹤辞」的第一把尺（确定性指标，零 API 成本）。

对应《外部深度调研总报告-20261107.md》§0 #13 / dsh 复核确认的最大空白：
13 个 selftest 测代码逻辑，没有一个测「鹤辞答得像不像鹤辞」——改人格等于裸奔。

## v0 的边界（诚实声明）

v0 只做**确定性指标**：slop 命中率（slop.py 词表扫描）+ 长度分布。
LLM-as-judge（10 个固定问题让模型批评回答像不像鹤辞）是 v1，要花钱，另开开关。
**v0 能防的**：提示词/模型改动后 AI 味回潮。**v0 防不了的**：内容层面的
人格漂移（语气对了但性格变了）——那要等 v1。

## 基线（2026-11-07，349 轮真实对话挖掘）

348 条回复命中 1 处（我可以帮你 ×1）——本地鹤辞语气基线良好。
**回归标准：强短语命中率不得显著高于基线（>5% 即告警）。**

## 用法

    python src/selftest_persona.py            # 隔离自测（词表/检测器/聚合，零外部依赖）
    python src/selftest_persona.py --live 50  # 对真实库最近 50 轮出报告（只读，零写入零 API）
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import slop  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def run_isolated_tests() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="persona_eval_"))

    print("=" * 70)
    print("人格回归 eval v0（确定性指标 · 隔离环境 · 零 API 成本）")
    print("=" * 70)

    print("\n[PE.1] 词表加载")
    pats = slop.load_patterns()  # 临时环境无 config 文件 → 内置兜底表
    check("内置兜底词表可加载", len(pats) >= 15, f"{len(pats)} 条")
    check("词表无空行/注释混入", all(p and not p.startswith("#") for p in pats), "")

    custom = tmp / "slop_custom.txt"
    custom.write_text("# 自定义词表\n测试专用AI腔\n# 注释行\n\n", encoding="utf-8")
    pats2 = slop.load_patterns(custom)
    check("外部词表可加载且跳过注释/空行",
          pats2 == ["测试专用AI腔"], str(pats2))

    print("\n[PE.2] 单文本检测")
    hits = slop.scan_text("作为一个AI，希望这能帮到你。")
    check("经典 AI 腔两条全中",
          dict(hits) == {"作为一个AI": 1, "希望这能帮到你": 1}, str(hits))
    clean = slop.scan_text("晚上好。今天有点累，不想聊太深的。")
    check("干净文本零误伤", clean == [], str(clean))
    check("空文本安全", slop.scan_text("") == [])

    print("\n[PE.3] 聚合报告")
    rep = slop.report_turns([
        "作为一个AI，我来解释一下。",
        "好的，就这么定了。",
        "综上所述，结论成立。",
        "嗯，那就先这样。",
    ])
    check("轮数统计正确", rep["turns"] == 4, str(rep["turns"]))
    check("命中轮数正确", rep["turns_with_hits"] == 2, str(rep["turns_with_hits"]))
    check("命中率正确", rep["hit_rate"] == 0.5, str(rep["hit_rate"]))
    check("短语分布聚合", rep["by_phrase"].get("作为一个AI") == 1
          and rep["by_phrase"].get("综上所述") == 1, str(rep["by_phrase"]))
    check("长度分位数可用", rep["len_p50"] > 0, str(rep["len_p50"]))

    print("\n[PE.4] 基线（2026-11-07 挖掘结果，供 --live 对比）")
    print("  348 条回复 → 强短语命中 1 处（我可以帮你×1），命中率 0.29%")
    print("  开头词分布自然（嗯，那就/那就好。/晚上好。/说实话，…）")
    print("  长度 p50=35 字、p90=73 字 —— 微信体，健康")
    check("基线已记录（信息项）", True, "")

    print("\n[PE.5] eval v1 流程（假客户端，零成本——真跑在 persona_eval.py --live）")
    import json as _json

    import persona_eval as pe
    import llm as llm_mod

    class _FakeClient:
        """第 1-2 次调用是答题（第 2 次故意带 slop），第 3 次是评委。"""

        def __init__(self):
            self.n = 0

        def send(self, messages, model=None):
            self.n += 1
            is_judge = any("评估员" in m.get("content", "") for m in messages)
            if is_judge:
                body = _json.dumps([{"score": 4, "reason": "像"}] * 2, ensure_ascii=False)
                return llm_mod.Reply(text=body, model="fake",
                                     usage=llm_mod.Usage(), latency_ms=0)
            text = "嗯，收到。" if self.n % 2 else "作为一个AI，希望这能帮到你。"
            return llm_mod.Reply(text=text, model="fake", usage=llm_mod.Usage(), latency_ms=0)

    rep = pe.run_v1(_FakeClient(), questions=["问题一", "问题二"],
                    system_prompt="测试人格", slop_patterns=["作为一个AI", "希望这能帮到你"])
    check("v1 流程：逐题回答", len(rep["answers"]) == 2, str(len(rep["answers"])))
    check("v1 流程：slop 确定性扫描命中",
          any(a["slop"] for a in rep["answers"]), str([a["slop"] for a in rep["answers"]]))
    check("v1 流程：评委分数解析（10 项协议缩到 2 题也逐项给分）",
          len(rep["scores"]) == 2 and rep["avg_score"] == 4, str(rep["scores"]))
    check("v1 报告：低分题结构存在（本例无低分）", rep["low_scores"] == [], "")
    check("v1 流程：slop 合计统计", rep["slop_total"] == 2, str(rep["slop_total"]))

    print()
    print("=" * 70)
    bad = [n for n, ok, _ in CHECKS if not ok]
    for n, ok, d in CHECKS:
        if not ok:
            print(f"  [失败] {n}  {d}")
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(bad)} 项，失败 {len(bad)} 项。")
    return 0 if not bad else 1


def run_live(last_n: int) -> int:
    print("=" * 70)
    print(f"真实库 slop 报告（最近 {last_n} 轮 · 只读 · 零写入 · 零 API）")
    print("=" * 70)
    rep = slop.report_db(last_n=last_n)
    print(f"轮数          : {rep['turns']}")
    print(f"命中轮数      : {rep['turns_with_hits']}（命中率 {rep['hit_rate']:.1%}）")
    print(f"命中总次数    : {rep['hits_total']}")
    print(f"短语分布      : {rep['by_phrase'] or '（无）'}")
    print(f"长度 p50/p90  : {rep['len_p50']} / {rep['len_p90']} 字")
    baseline = 0.0029
    if rep["hit_rate"] > 0.05:
        print("⚠️ 命中率 > 5%：AI 味回潮，检查最近的提示词/模型变更")
    else:
        print(f"✅ 命中率在基线内（基线 {baseline:.2%}）")
    print("说明：本模式只读；断言版回归将在 v1（LLM-as-judge）接入后启用")
    return 0


def main() -> int:
    if "--live" in sys.argv:
        n = 50
        idx = sys.argv.index("--live")
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1].isdigit():
            n = int(sys.argv[idx + 1])
        return run_live(n)
    return run_isolated_tests()


if __name__ == "__main__":
    raise SystemExit(main())
