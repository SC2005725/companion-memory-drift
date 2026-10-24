"""离线自测（不调用任何 API，不花钱）。

用一个假的 LLM 客户端喂固定的回复，把 Phase 1-3 的整条链路真跑一遍：
    人格拼装 → 组装 messages → 记账 → 原始对话落盘 → 事件判定 → 原始事件落盘 → 校验

为什么需要它：真实 API 调用要花钱、要联网、结果还随机；而链路上大部分环节
（存储、哈希链、事件固化、计价）跟模型无关，必须先确认这部分是对的。

用法：
    python src\\selftest.py          跑自测（会在 conversation/ 留下测试数据）
    python src\\selftest.py --clean  跑完自测后清掉测试数据
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat  # noqa: E402
import events as events_mod  # noqa: E402
import pricing  # noqa: E402
import storage  # noqa: E402
import verify  # noqa: E402
from config import load_settings  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from paths import USER_MODEL_DIR  # noqa: E402
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


# 已知的**正式项目目录**。只有跑在这里才需要守卫。
#
# ## 为什么不能靠 `paths.ROOT == Path(__file__).parent.parent`
# 那个判据在**任何副本里也成立**（副本自带 paths.py），
# 于是会连副本一起拒 —— 而 `dryrun.py` 正是在副本里调本自测的，
# 那样会把演练一起弄坏。
#
# ## 为什么写成字面路径是可以接受的
# 它只是**保护**用的默认值，不是运行必需 —— 换机器时守卫会失效（退化为不保护），
# 但不会妨碍运行。要临时在别处跑，加 `--allow-real` 或设环境变量即可。
_REAL_PROJECT_ROOT = Path(r"（本地路径）")


def _is_real_project_root() -> bool:
    """当前跑的是不是**正式项目目录**（而不是隔离副本）。

    ## 为什么加这道守卫
    本自测会 `storage.connect()` 连**真实库**并调 `send_once`，
    靠「只删自造行」的补偿机制收尾。实测踩到的坑：
    用户模型（Phase 7 才有）**不在那个补偿清单里**，
    于是留下了一份引用**不存在的 seq** 的孤儿快照，污染了正式数据。

    补偿机制只要漏一张表就静默失效 —— 所以加一道**显式**守卫：
    默认拒绝在正式目录上写。逃生门复用项目既有的
    `DSH_ALLOW_REAL_DB_WRITE`，免得出现两套约定。
    """
    try:
        from paths import ROOT
        here = os.path.abspath(str(ROOT))
    except Exception:
        return False
    return here == os.path.abspath(str(_REAL_PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1-3 离线自测")
    parser.add_argument("--clean", action="store_true", help="自测后删除测试数据")
    parser.add_argument("--allow-real", action="store_true",
                        help="显式允许在正式项目目录上跑（会写真实库；默认拒绝）")
    args = parser.parse_args(argv)

    if _is_real_project_root() and not (
        args.allow_real or os.environ.get("DSH_ALLOW_REAL_DB_WRITE")
    ):
        print("拒绝在正式项目目录上运行自测。")
        print("  本自测会写真实库；请任选一种方式：")
        print("    ① 复制项目到临时目录后再跑（推荐）")
        print("    ② 加 --allow-real 显式承担后果")
        print("    ③ 设环境变量 DSH_ALLOW_REAL_DB_WRITE=1")
        return 2

    settings = load_settings()
    persona = load_persona()
    conn = storage.connect()
    client = FakeClient()

    # --- 关键：先记下自测开始前已存在的事件 ID ---
    #
    # 自测会往 events 表写一条测试事件。--clean 必须只删**这次自测新建的**那一条，
    # 绝不能按「表里所有事件」去删 —— 那会把真实实验数据一起删掉。
    #
    # 这里踩过一次真实的坑：早期版本按「删除时刻表里的全部事件」清理，
    # 结果把实验基准事件 raw_event_001 删了（它是 ground truth，属于真实实验数据）。
    preexisting_event_ids = {
        r["event_id"] for r in conn.execute("SELECT event_id FROM events").fetchall()
    }
    if preexisting_event_ids:
        print(
            f"注意：events 表里已有 {len(preexisting_event_ids)} 条既有事件，"
            "它们不会被本次 --clean 触碰。"
        )

    # 记下跑之前 user_model/ 里已有哪些快照文件。
    # 快照是**文件**（不是数据库行），--clean 必须能区分「本次新建的」与「真实历史的」。
    # 只算快照本体（.json），不要把 .sha256 sidecar 也数进来 —— 否则数字翻倍
    preexisting_um_files = {q.name for q in USER_MODEL_DIR.glob("model_v*.json")}
    if preexisting_um_files:
        print(f"注意：user_model/ 里已有 {len(preexisting_um_files)} 份快照，本次 --clean 不会触碰它们。")

    # 同理记下已存在的会话 ID。--clean 靠它区分「自己的测试行」和「真实数据行」。
    preexisting_session_ids = {
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM conversations").fetchall()
        if r["session_id"]
    } | {
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM api_calls").fetchall()
        if r["session_id"]
    } | {
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM events").fetchall()
        if r["session_id"]
    }

    session = chat.new_session(
        persona=persona, settings=settings, client=client, peak_override=False, conn=conn
    )

    print("=" * 64)
    print("Phase 1-3 离线自测（不调用真实 API）")
    print("=" * 64)

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
    #
    # 先算出「本次自测新建了哪些事件」，后续的展示、校验、清理都只针对这些。
    # 这样即便 events 表里已有真实实验数据（如基准事件 raw_event_001），
    # 自测的项数与报告也不会被它们影响，更不会去动它们。
    created_event_ids = [
        r["event_id"]
        for r in conn.execute("SELECT event_id FROM events").fetchall()
        if r["event_id"] not in preexisting_event_ids
    ]

    print("\n[4] 原始事件文件（仅本次自测新建的）")
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
    print("\n[6] 篡改检测（自测完会回滚）")
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
    print("说明：本次自测使用的是假客户端，没有产生任何真实 API 费用。")

    # 注意：连接必须在 _clean 之后才关闭 —— _clean 需要 DELETE 本次自测写入的行。
    # 早期版本在这里先 close() 再 _clean()，导致清理静默失败（sqlite3.ProgrammingError），
    # 测试数据残留、留下孤儿事件行。
    if args.clean:
        removed = _clean(conn, created_event_ids, preexisting_session_ids, session.session_id,
                     preexisting_um_files)
        print(
            f"\n已清理测试数据：{removed['events']} 个事件文件、"
            f"{removed['turns']} 轮对话、{removed['api_calls']} 条调用记录、"
            f"{removed['ledger']} 条费用流水、"
            f"{len(removed.get('um_snapshots') or [])} 份用户模型快照。"
        )
        if removed["protected_skipped"]:
            print(f"已保护、未删除的基准事件：{'、'.join(removed['protected_skipped'])}")
        if removed["kept_sessions"]:
            print(f"已保留的既有会话：{'、'.join(removed['kept_sessions'])}")
        print("（数据库文件本身、既有真实数据、memory/versions/ 均未被触碰）")

    conn.close()
    return 1 if failed else 0


def _clean(conn, event_ids: list[str], keep_session_ids: set, test_session_id: str,
           preexisting_um_files: set | None = None) -> dict:
    """只删除**本次自测新建**的测试数据。绝不删除数据库文件本身。

    ## 三道保护，每一条都是踩过坑之后加的

    1. **不删 `conversation/` 目录**。早期版本用 `shutil.rmtree(CONVERSATION_DIR)`，
       结果把整个 `conversations.db` 删了 —— 连带删掉真实实验数据行，
       并且留下「文件还在、数据库没记录」的孤儿事件文件。
       现在改为只 DELETE 属于本次自测会话的行，数据库文件始终保留。

    2. **不按 `glob("*")` 通配删事件文件**，只按 ID 精确删。

    3. **硬保护基准事件 ID**（`raw_event_001`）。它是实验 ground truth，
       属于真实实验数据，不是测试产物。

    `keep_session_ids` 是自测开跑前就存在的会话 ID 集合；`test_session_id` 是本次自测的。
    只有二者不重叠时才会删行；重叠时宁可少删也不误删真实数据。
    """
    from paths import LEDGER_FILE, RAW_EVENTS_DIR

    PROTECTED_EVENT_IDS = {"raw_event_001"}

    # --- 1) 事件文件：按 ID 精确删，跳过受保护的 ---
    removed_events = 0
    skipped_protected = []
    reverted_event_ids = []
    for event_id in event_ids:
        if not event_id or "/" in event_id or "\\" in event_id:
            continue  # 事件 ID 来自数据库，仍然挡一道，避免目录穿越
        if event_id in PROTECTED_EVENT_IDS:
            skipped_protected.append(event_id)
            continue
        reverted_event_ids.append(event_id)
        for name in (f"{event_id}.json", f"{event_id}.sha256"):
            path = RAW_EVENTS_DIR / name
            if path.is_file():
                path.unlink()
                removed_events += 1

    # --- 2) 数据库行：只删本次自测会话的，且该会话不属于既有会话 ---
    turns = 0
    calls = 0
    kept_sessions = sorted(keep_session_ids)
    if test_session_id and test_session_id not in keep_session_ids:
        cur = conn.execute("DELETE FROM conversations WHERE session_id = ?", (test_session_id,))
        turns = cur.rowcount or 0
        cur = conn.execute("DELETE FROM api_calls WHERE session_id = ?", (test_session_id,))
        calls = cur.rowcount or 0
        conn.execute("DELETE FROM retrievals WHERE session_id = ?", (test_session_id,))
        # events 行也必须删，否则会留下「数据库有记录但文件已删」的缺失状态
        placeholders = ",".join("?" for _ in reverted_event_ids)
        if reverted_event_ids:
            conn.execute(
                f"DELETE FROM events WHERE event_id IN ({placeholders})", reverted_event_ids
            )

        # 顺带清理孤儿检索记录。
        #
        # 为什么需要：`retrievals` 是 Phase 5 才加的表。在它加入之前跑过的自测，
        # 只删了 conversations / api_calls，于是留下「会话已不存在、检索记录还在」的孤儿。
        # 这里把「既不在 conversations、也不在 api_calls、也不是既有会话」的检索记录
        # 一并清掉 —— 加 keep_session_ids 白名单，宁可少删也不碰真实数据。
        orphan_ids = [
            r["session_id"]
            for r in conn.execute("SELECT DISTINCT session_id FROM retrievals").fetchall()
            if r["session_id"]
            and r["session_id"] not in keep_session_ids
            and not conn.execute(
                "SELECT 1 FROM conversations WHERE session_id = ? LIMIT 1", (r["session_id"],)
            ).fetchone()
            and not conn.execute(
                "SELECT 1 FROM api_calls WHERE session_id = ? LIMIT 1", (r["session_id"],)
            ).fetchone()
        ]
        for sid in orphan_ids:
            conn.execute("DELETE FROM retrievals WHERE session_id = ?", (sid,))
        if orphan_ids:
            kept_sessions = kept_sessions + [f"(顺带清理孤儿检索: {len(orphan_ids)} 个会话)"]

        # ---- 顺带清理**用户模型**（Phase 7 才有的东西，原清理清单漏了它）----
        #
        # ## 实测踩到的坑（这是我造成的真实污染）
        #
        # 本自测跑在**真实库**上。加宽用户模型的捕获规则之后，
        # SCRIPT 里那句「我以前在晚上工作过一年多…」开始命中信号，
        # 于是 `send_once` 顺手写了一份用户模型快照 + 一条 user_model_events。
        # 而 `--clean` 只删 conversations / api_calls / retrievals / events ——
        # **用户模型原封不动**，于是正式数据里留下一条
        # 引用 **seq=145**（而 conversations 最大只到 144）的孤儿条目。
        #
        # 清理原则与上面几条一致：**只删属于本次自测会话的**。
        cur = conn.execute(
            "DELETE FROM user_model_events WHERE session_id = ?", (test_session_id,)
        )
        um_events = cur.rowcount or 0
        if um_events:
            kept_sessions = kept_sessions + [f"(顺带清理用户模型事件: {um_events} 行)"]
        conn.commit()

    # ---- 用户模型**快照文件**：只删本次自测新建的那几份 ----
    #
    # 快照是 `chat.send_once` 直接落到 `user_model/` 目录的**文件**，
    # 不属于数据库，所以上面的 DELETE 管不到它。
    # 实测：只删数据库行的话，会留下一份引用不存在 seq 的孤儿快照。
    #
    # 清理原则与别处一致：**先记下跑之前有哪些文件，只删多出来的**。
    # 绝不按 glob 全删 —— 那会把真实历史快照（v001–v007 是实验证据）清掉。
    removed_snaps = []
    try:
        # 快照本体与其 .sha256 sidecar 一起清，别留下「摘要没了原文还在」的半截状态
        for p in sorted(USER_MODEL_DIR.glob("model_v*.json")):
            if p.name in (preexisting_um_files or set()):
                continue
            p.unlink(missing_ok=True)
            removed_snaps.append(p.name)
            side = p.with_suffix(".sha256")
            if side.exists():
                side.unlink(missing_ok=True)
                removed_snaps.append(side.name)
    except OSError:
        pass

    # --- 3) 费用流水：只摘掉本次自测那几行，不整文件删除 ---
    removed_ledger = 0
    if LEDGER_FILE.exists():
        import json as _json

        rows = []
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                rows.append(line)
                continue
            if rec.get("session_id") == test_session_id and test_session_id not in keep_session_ids:
                removed_ledger += 1
                continue
            rows.append(line)
        if rows:
            LEDGER_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        else:
            LEDGER_FILE.unlink()

    return {
        "events": removed_events,
        "turns": turns,
        "api_calls": calls,
        "ledger": removed_ledger,
        "protected_skipped": skipped_protected,
        "kept_sessions": kept_sessions,
        "um_snapshots": removed_snaps,
    }


if __name__ == "__main__":
    raise SystemExit(main())
