"""Phase 1-3 离线自测（不调用任何 API，不花钱，**不污染任何真实数据**）。

用一个假的 LLM 客户端喂固定的回复，把 Phase 1-3 的整条链路真跑一遍：
    人格拼装 → 组装 messages → 记账 → 原始对话落盘 → 事件判定 → 原始事件落盘 → 校验

## 2026-11-15 重写：**全量隔离**（取代"真实库+补偿清理"旧设计）

旧设计（真实库上跑 + `--clean` 只删自造行）有三个已实锤的失败模式：
    1. **真实库污染**：补偿清单漏一张表就静默失效——用户模型孤儿快照事故
       （引用不存在的 seq）、heartbeat_runs/pending_actions 污染（hence dry_run 补丁）。
    2. **守卫路径过期**：`_REAL_PROJECT_ROOT` 写死 D 盘旧路径，项目迁到
       AutoClaw 目录后守卫形同虚设——2026-11-15 独立体检实锤：
       **16 条假对话进了生产哈希链（seq 361/371-374）、22 条假账进了 api_calls**，
       且假对话经整理员进了记忆链（v016+ 夜班条目）。删除会断链，只能保留。
    3. **--clean 在哈希链时代失效**：删行 = 断链，旧补偿逻辑已不可用。

新设计（与 selftest_llm_exit 同款）：**一切写入重定向到临时目录**，断言与
链路覆盖完全不变，跑完整目录删除。`--clean` / `--allow-real` 两个旗标
保留但成为无害 no-op（status_snapshot.py 仍在用它们调用）。

用法：
    python src\\selftest.py          跑自测（临时目录，跑完即删，零污染）
    python src\\selftest.py --clean  兼容旧调用（现为 no-op）
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ⚠️ 隔离库里没有存档锚点（first_archive.json 记录的是真实主链的 seq）——
# archive_anchor.py:143 的既有约定：隔离测试方设 DSH_SKIP_ANCHOR=1，锚点不参与判定。
# （archive_anchor 的 ROOT 从自身文件位置推导，immune to paths 重定向——必须用这个开关。）
os.environ["DSH_SKIP_ANCHOR"] = "1"   # 强制置位：隔离库上锚点校验必然失真

SRC = Path(__file__).resolve().parent
PROJECT = SRC.parent
sys.path.insert(0, str(SRC))

# --- 全量隔离：先建临时目录、重定向 paths，再导入业务模块 ---------------------
# （业务模块在 import 时绑定 paths 的值——顺序错了隔离就失效。）
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="selftest_p13_"))

import paths  # noqa: E402

paths.ROOT = TEMP_ROOT
paths.CONFIG_DIR = TEMP_ROOT / "config"
paths.PROMPTS_DIR = paths.CONFIG_DIR / "prompts"
paths.SETTINGS_FILE = paths.CONFIG_DIR / "settings.json"
paths.PERSONALITY_FILE = TEMP_ROOT / "personality" / "personality.json"
paths.CONVERSATION_DIR = TEMP_ROOT / "conversation"
paths.DB_FILE = paths.CONVERSATION_DIR / "conversations.db"
paths.MEMORY_DIR = TEMP_ROOT / "memory"
paths.RAW_DIR = paths.MEMORY_DIR / "raw"
paths.RAW_EVENTS_DIR = paths.RAW_DIR / "events"
paths.VERSIONS_DIR = paths.MEMORY_DIR / "versions"
paths.CHANGELOG_DIR = paths.MEMORY_DIR / "changelog"
paths.RECALL_DIR = TEMP_ROOT / "recall"
paths.RELATIONSHIP_DIR = TEMP_ROOT / "relationship"
paths.USER_MODEL_DIR = TEMP_ROOT / "user_model"
paths.EXPERIMENTS_DIR = TEMP_ROOT / "experiments"
paths.LOGS_DIR = TEMP_ROOT / "logs"
paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"
paths.ARCHIVE_ANCHOR_FILE = paths.EXPERIMENTS_DIR / "first_archive.json"

paths.ensure_dirs()

# 拷贝真实配置：人格/设置/提示词——断言要测「真实人格渲染」「真实计价」。
# ⚠️ 不拷存档锚点（first_archive.json 记录 seq=144，隔离库只有 4 轮，
# 锚点校验在全新库上本就不适用——拷了反而 verify 失败）。
for _src_name, _dst in (
    ("config/settings.json", paths.SETTINGS_FILE),
    ("config/prompts/consolidate.md", paths.CONSOLIDATE_PROMPT_FILE),
    ("personality/personality.json", paths.PERSONALITY_FILE),
):
    _src = PROJECT / _src_name
    if _src.exists():
        shutil.copy2(_src, _dst)

# 旧路径绑定的业务模块全部重载（selftest_llm_exit 同款做法）
for _m in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
           "chat", "verify", "config", "persona", "probe", "relationship",
           "user_model", "events", "llm", "model_router", "archive_anchor",
           "heartbeat"):
    sys.modules.pop(_m, None)

import chat  # noqa: E402
import events as events_mod  # noqa: E402
import pricing  # noqa: E402
import storage  # noqa: E402
import verify  # noqa: E402
from config import load_settings  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from persona import load_persona  # noqa: E402

# 固定的假对话：第 1 句故意命中事件触发词，用来验证原始事件固化
SCRIPT = [
    ("我以前在晚上工作过一年多，那段时间挺难受的。", "那段夜班，最难受的是哪一点？"),
    ("主要是白天睡不好。", "那后来是怎么调过来的？"),
    ("换了工作以后就正常了。", "嗯，那挺好的。"),
    ("你记得我说过什么吗？", "你提过夜班那阵子，白天睡不好。"),
]


class FakeClient:
    """接口与 LLMClient 一致，但不联网。"""

    model = "fake-model-for-selftest"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def send(self, messages, model=None) -> Reply:
        self.calls.append(messages)
        index = len(self.calls) - 1
        text = SCRIPT[index][1] if index < len(SCRIPT) else "（自测回复）"
        return Reply(
            text=text,
            model=self.model,
            usage=Usage(prompt_tokens=1200 + index * 10, completion_tokens=40, cache_hit_tokens=700),
            latency_ms=5,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1-3 离线自测（全量隔离版）")
    # 兼容旧调用：status_snapshot.py 用 --clean --allow-real 调本自测。
    # 全量隔离后两者皆 no-op——保留旗标以免外部调用方报错。
    parser.add_argument("--clean", action="store_true", help="（兼容旧调用，现为 no-op）")
    parser.add_argument("--allow-real", action="store_true", help="（兼容旧调用，现为 no-op）")
    args = parser.parse_args(argv)
    if args.clean or args.allow_real:
        print("（--clean/--allow-real 已是 no-op：本自测全量隔离，不碰任何真实数据）")

    settings = load_settings()
    persona = load_persona()
    conn = storage.connect()
    client = FakeClient()

    # 自测在**临时库**上从零开始——既有集合恒为空，保留变量以少动断言
    preexisting_event_ids: set = set()

    session = chat.new_session(
        persona=persona, settings=settings, client=client, peak_override=False, conn=conn
    )

    print("=" * 64)
    print("Phase 1-3 离线自测（不调用真实 API；全量隔离，零污染）")
    print("=" * 64)
    print(f"隔离目录：{TEMP_ROOT}")

    # --- 人格拼装 ---
    system_prompt = persona.render_system_prompt()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("人格 system prompt 非空", bool(system_prompt.strip()), f"{len(system_prompt)} 字"))
    checks.append(("人格名字出现在 prompt 里", persona.name in system_prompt, persona.name))

    # --- 逐轮对话 ---
    print("\n[1] 对话链路")
    for user_text, _ in SCRIPT:
        result = chat.send_once(session, user_text, echo=lambda t: None)
        mark = "＋事件" if result["event"] else "      "
        print(f"  用户：{user_text}")
        print(f"  小满：{result['reply'].text}   {mark}"
              f"  [{result['reply'].usage.prompt_tokens}+{result['reply'].usage.completion_tokens} tokens]")
        if result["event"]:
            print(f"        原始事件 {result['event']['event_id']} → {result['event']['file']}")

    # --- messages 结构 ---
    print("\n[2] 发给模型的 messages 结构")
    msgs = client.calls[0]
    checks.append(("第一条是 system", msgs[0]["role"] == "system", msgs[0]["role"]))
    checks.append(("最后一条是 user", msgs[-1]["role"] == "user", msgs[-1]["role"]))
    checks.append(("历史轮数受 history_turns 限制",
                   len(msgs) <= 1 + settings.history_turns * 2 + 1, f"共 {len(msgs)} 条"))
    print(f"  角色序列：{', '.join(m['role'] for m in msgs)}")

    # --- 落盘统计 ---
    print("\n[3] 落盘统计")
    s = storage.stats(conn)
    print(f"  对话轮数：{s['turns']}    原始事件：{s['events']}    API 调用记录：{s['api_calls']}")
    checks.append(("4 轮对话全部落盘", s["turns"] >= len(SCRIPT), str(s["turns"])))
    checks.append(("命中触发词的句子已固化为事件", s["events"] >= 1, str(s["events"])))

    # --- 事件文件 ---
    created_event_ids = [
        r["event_id"]
        for r in conn.execute("SELECT event_id FROM events").fetchall()
    ]

    print("\n[4] 原始事件文件（临时目录内）")
    if not created_event_ids:
        print("  （本次没有新建事件 —— 这不符合预期）")
    for event_id in created_event_ids:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        path = storage.RAW_EVENTS_DIR / f"{event_id}.json"
        exists = path.exists()
        digest_ok = exists and storage.sha256_text(
            path.read_text(encoding="utf-8").rstrip("\n")
        ) == row["sha256"]
        print(f"  {event_id}  文件存在={exists}  哈希一致={digest_ok}")
        print(f"    原文：{row['user_text']}")
        checks.append((f"{event_id} 文件与哈希一致", bool(exists and digest_ok), ""))

    # --- 校验 ---
    print("\n[5] 完整性校验（未改动时应通过）")
    report = verify.verify_all(conn)
    print("  " + verify.format_report(report).replace("\n", "\n  "))
    checks.append(("校验通过", report["ok"], f"{report['turns_checked']} 轮 / {report['events_checked']} 事件"))

    # --- 篡改检测 ---
    print("\n[6] 篡改检测（改完会回滚）")
    first = conn.execute("SELECT * FROM conversations ORDER BY seq ASC LIMIT 1").fetchone()
    original = first["user_text"]
    conn.execute("UPDATE conversations SET user_text=? WHERE turn_id=?", ("我改成别的了。", first["turn_id"]))
    conn.commit()
    tampered = verify.verify_all(conn)
    print(f"  改掉第 1 轮用户原话后：校验 {'失败（正确）' if not tampered['ok'] else '仍通过（错误！）'}")
    for err in tampered["chain_errors"]:
        print(f"    {err}")
    checks.append(("篡改能被发现", not tampered["ok"], ""))
    conn.execute("UPDATE conversations SET user_text=? WHERE turn_id=?", (original, first["turn_id"]))
    conn.commit()
    restored = verify.verify_all(conn)
    checks.append(("回滚后校验恢复正常", restored["ok"], ""))

    # --- 计价 ---
    print("\n[7] 计价与账本")
    summary = pricing.summarize()
    print("  " + pricing.format_summary(summary).replace("\n", "\n  "))
    expected_micro = pricing.estimate_micro_yuan(
        cost_cfg=settings.cost, model="deepseek-flash",
        prompt_tokens=1200, completion_tokens=40, cache_hit_tokens=700, peak=False,
    )
    print(f"  手算校验（1200 输入 / 700 命中 / 40 输出，空闲价）：{pricing.format_yuan(expected_micro)}")
    checks.append(("空闲价是高峰价的一半",
                   expected_micro * 2 == pricing.estimate_micro_yuan(
                       cost_cfg=settings.cost, model="deepseek-flash",
                       prompt_tokens=1200, completion_tokens=40, cache_hit_tokens=700, peak=True),
                   ""))

    # --- 事件触发规则 ---
    print("\n[8] 事件触发规则")
    hit = events_mod.detect("我以前在晚上工作过一年多。", settings.triggers)
    miss = events_mod.detect("今天天气不错。", settings.triggers)
    print(f"  含触发词：「{hit['matched']}」→ hit={hit['hit']}")
    print(f"  不含触发词：matched={miss['matched']} → hit={miss['hit']}")
    checks.append(("触发词命中判定正确", hit["hit"] and not miss["hit"], ""))

    # --- 汇总 ---
    print("\n" + "=" * 64)
    failed = [name for name, good, _ in checks if not good]
    for name, good, detail in checks:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 64)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：本次自测使用的是假客户端，没有产生任何真实 API 费用；全程零污染。")

    # --- 清理：整个临时目录（隔离设计的收尾——不再需要任何补偿式删除）---
    conn.close()
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    print(f"已清理隔离目录：{TEMP_ROOT}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
