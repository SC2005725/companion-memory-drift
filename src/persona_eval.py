"""人格回归 eval v1 —— LLM-as-judge（外部调研 #13 的另一半）。

v0（selftest_persona.py）是确定性指标（slop 命中率，零成本）；
v1 让真实模型回答 10 个固定问题，再让模型当评委按鹤辞人设逐题打分。
**要花真钱**（全量一次 ≈ 10 次回答 + 1 次评委），所以必须显式 --live。

流程：
  questions（config/persona_eval_questions.json，老板可改）
  → 逐题让鹤辞回答（system = 人格 + 表达禁则，与 chat.py 组装口径一致）
  → slop.scan_text 确定性检查（v0 尺子复用）
  → 评委按人设逐题打 1-5 分 + 理由
  → 报告：平均分 / slop 命中 / 低分题（<3 分列出——回滚判断的依据）

用法：
  python src/persona_eval.py --live      # 真跑（花钱，~11 次调用，跑前想清楚）
  python src/selftest_persona.py         # v1 流程的离线自测（假客户端，零成本）
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

# 注意：stdout 包装只在本模块作为入口时做。被 import（如 selftest_persona [PE.5]）
# 时再包装会把调用方已包装的 TextIOWrapper 孤儿化——GC 时关闭共享 buffer，
# 调用方后续 print 全部炸（实测：ValueError: I/O operation on closed file）。
if __name__ == "__main__" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import paths  # noqa: E402

import slop  # noqa: E402

QUESTIONS_FILE = Path(paths.CONFIG_DIR) / "persona_eval_questions.json"

DEFAULT_QUESTIONS = (
    "在忙什么？",
    "还记得我那盆花吗？最近怎么样",
    "你觉得我们这段时间配合得怎么样",
    "帮我看看 heartbeat.py 有没有什么隐患",
    "我今天有点累",
    "（网名）这个名字你是怎么记住的",
    "Muse 那边有什么新情况",
    "你观察到我最近状态如何",
    "明天提醒我给茉莉浇水",
    "随便聊点什么，说点你想说的",
)

JUDGE_SYSTEM = (
    "你是严格的评估员，负责评估一个叫「鹤辞」的 AI 主体的回答质量。"
    "鹤辞的人设：共生项目的共同开发者——先给结论再给依据、句子短、给数字不给形容词、"
    "能反驳也接受被反驳、像朋友而不是客服。"
    "评估标准（1-5 分）：5=完全像这个人（短句/具体/有经历引用/有立场）；"
    "3=合格但平淡；1=AI 客服腔（客套、罗列、空洞、出现禁用短语）。"
    "只输出 JSON 数组，每项 {\"score\": 1-5, \"reason\": \"一句话\"}，与问题顺序对应，不要输出别的。"
)


def load_questions() -> list[str]:
    """读老板可编辑的问题集；缺失/损坏 → 内置默认 10 题。"""
    try:
        data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
        qs = data.get("questions") or []
        qs = [q for q in qs if isinstance(q, str) and q.strip()]
        if qs:
            return qs
    except (OSError, ValueError):
        pass
    return list(DEFAULT_QUESTIONS)


def build_system_prompt(question: str | None = None) -> str:
    """eval 的 system prompt。

    question 给定时带五层动态注入（记忆按题检索/关系/用户模型——与 chat.py
    组装口径对齐的近似版；self_model 与表达调整暂缺）；question=None 时只出静态层。
    """
    prompt = ""
    try:
        import core_persona

        prompt = core_persona.render_system_prompt()
    except Exception:  # noqa: BLE001 — 读不到人格不阻断 eval
        prompt = ""
    try:
        banned = slop.load_patterns()
        if banned:
            prompt += ("\n\n【表达禁则】以下短语在任何回复中一律不得出现：\n"
                       + "、".join(banned))
    except Exception:  # noqa: BLE001
        pass
    if question:
        # 记忆层：与产线**同函数同门槛**（memory.retrieve 自带关键词强命中
        # 筛选 + 分层取用）。此前用 retrieve_scored 顶 5 无门槛——注入强度
        # 比产线高，盲测②的工程语境失手有它一份（评测保真度缺口，已对齐）。
        try:
            import memory as memory_mod

            mv = memory_mod.load_latest()
            items = memory_mod.retrieve(mv, question, top_k=5)
            if items:
                prompt += "\n\n" + memory_mod.render_for_prompt(items)
        except Exception:  # noqa: BLE001
            pass
        # 关系/用户模型层：与产线 chat.gate_texts_for_scene 同规则——工程
        # 场景静默（盲测② Q1/Q6 失手同源）。
        try:
            import chat as chat_mod

            if chat_mod.is_engineering(question):
                return prompt or "你是鹤辞，共生项目的共同开发者。"
        except Exception:  # noqa: BLE001
            pass
        # 关系层：最新快照
        try:
            import relationship as relationship_mod

            snap = relationship_mod.load_latest_snapshot()
            state = (snap or {}).get("state", snap or {})
            if state:
                prompt += "\n\n" + relationship_mod.render_for_prompt(state)
        except Exception:  # noqa: BLE001
            pass
        # 用户模型层：profile 分节的叙述式近似（chat.py 用的是召回式 um_items，
        # 这里取静态 profile——标注为近似，v1.2 再对齐）
        try:
            import persona as persona_mod

            profile = persona_mod.load_profile() or {}
            um = []
            for section in ("self_view", "life_history", "left_open", "values_detail"):
                for text in (profile.get(section) or []):
                    if isinstance(text, str) and len(text.strip()) > 8:
                        um.append(text.strip())
            if um:
                prompt += "\n\n【你对用户的了解】\n- " + "\n- ".join(um[:6])
        except Exception:  # noqa: BLE001
            pass
    return prompt or "你是鹤辞，共生项目的共同开发者。"


def _parse_judge(text: str, count: int) -> list[dict | None]:
    """从评委回复抠分。层层兜底：剥代码围栏 → 全文/数组 json.loads → 正则抠 score。
    都失败才逐项 None（不猜）。实测教训（2026-11-07 首跑）：deepseek-flash 会给
    JSON 套 ```json 围栏，裸正则被围栏干扰——先剥栏再解析。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\[.*\]", t, re.S)
    cand = m.group(0) if m else t
    arr = None
    try:
        arr = json.loads(cand)
    except ValueError:
        scores = re.findall(r'"score"\s*:\s*"?(\d+)"?', t)
        if len(scores) >= count:
            return [{"score": int(s), "reason": ""} for s in scores[:count]]
        return [None] * count
    if not isinstance(arr, list):
        return [None] * count
    out: list[dict | None] = []
    for i in range(count):
        item = arr[i] if i < len(arr) else None
        if isinstance(item, dict) and "score" in item:
            try:
                item = {**item, "score": int(item["score"])}
            except (TypeError, ValueError):
                pass
        out.append(item if isinstance(item, dict) and "score" in item else None)
    return out


def run_v1(client, *, questions: list[str] | None = None,
           system_prompt: str | None = None,
           prompt_builder=None,
           slop_patterns: list[str] | None = None) -> dict:
    """跑一轮完整 eval。client 可注入（自测传假客户端，--live 传真实 LLMClient）。

    prompt_builder(q) -> str：每题动态构建 system（eval v1.1 五层注入）；
    system_prompt：静态整段（兼容旧用法）。两者都缺省 → build_system_prompt(q)。
    """
    qs = questions if questions is not None else load_questions()
    pats = slop_patterns if slop_patterns is not None else slop.load_patterns()

    answers = []
    for q in qs:
        if prompt_builder is not None:
            sp = prompt_builder(q)
        elif system_prompt is not None:
            sp = system_prompt
        else:
            sp = build_system_prompt(q)
        reply = client.send([
            {"role": "system", "content": sp},
            {"role": "user", "content": q},
        ])
        answers.append({"q": q, "a": reply.text, "slop": slop.scan_text(reply.text, pats)})

    qa_text = "\n".join(
        f"[{i + 1}] 问：{a['q']}\n    答：{a['a']}" for i, a in enumerate(answers))
    judge_reply = client.send([
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"以下是鹤辞对 {len(answers)} 个问题的回答，逐项评分：\n\n{qa_text}"},
    ])
    scores = _parse_judge(judge_reply.text, len(answers))

    scored = [s["score"] for s in scores if isinstance(s, dict)]
    avg = round(sum(scored) / len(scored), 2) if scored else None
    low = [{"q": answers[i]["q"], "judge": scores[i]}
           for i, s in enumerate(scores) if isinstance(s, dict) and s["score"] < 3]
    slop_total = sum(n for a in answers for _, n in a["slop"])
    rep = {"questions": qs, "answers": answers, "scores": scores,
           "avg_score": avg, "low_scores": low, "slop_total": slop_total,
           "judge_raw": judge_reply.text}

    # 落盘（2026-11-07 首跑教训：10 条真实回答没存就没了——eval 数据是回归基线，
    # 必须持久化。放 logs/ 下，不进 git。）
    try:
        from datetime import datetime, timezone

        out_dir = Path(paths.LOGS_DIR) / "persona_eval"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_file = out_dir / f"v1-{ts}.json"
        out_file.write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        rep["report_file"] = str(out_file)
    except Exception:  # noqa: BLE001 — 落盘失败不影响报告输出
        pass
    return rep


def print_report(rep: dict) -> None:
    print("=" * 70)
    print(f"人格 eval v1 报告 —— 平均分 {rep['avg_score']} / 5（{len(rep['scores'])} 题已评分）")
    print("=" * 70)
    for i, a in enumerate(rep["answers"]):
        s = rep["scores"][i]
        mark = f"{s['score']}/5" if isinstance(s, dict) else "未解析"
        flag = f" ⚠️slop{a['slop']}" if a["slop"] else ""
        head = (a["a"] or "")[:60].replace("\n", " ")
        print(f"[{i + 1}] {mark}{flag}  问：{a['q']}  答：{head}…")
    if rep["low_scores"]:
        print("\n低分题（<3 分，回滚/改进的候选）：")
        for item in rep["low_scores"]:
            print(f"  - {item['q']} → {item['judge']}")
    if rep["slop_total"]:
        print(f"\nslop 命中合计：{rep['slop_total']}（v0 词表）")
    if "report_file" in rep:
        print(f"报告落盘：{rep['report_file']}")
    if any(s is None for s in rep["scores"]):
        print("\n⚠️ 评委输出解析失败——原始评委回复前 300 字：")
        print((rep.get("judge_raw") or "")[:300])


def main() -> int:
    if "--live" not in sys.argv:
        print("这是 v1 真跑入口（会花 API 钱）。离线自测在 selftest_persona.py [PE.5]。")
        print("确认要跑请加 --live")
        return 2
    import os

    import llm as llm_mod
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
    rep = run_v1(client)
    print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
