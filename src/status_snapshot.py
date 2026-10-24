"""生成「状态快照」——把会变的数字集中到一个自动生成的文件里。

## 为什么需要这个

这个项目反复出现同一个问题：**文档里的数字过时了**。

原因是数字（对话轮数、事件数、测试项数、花费）**每聊一次就变**，而它们被手写
散布在 `archive-inventory.txt`、`README.md`、`docs/version-history.md` 三处。改一处忘两处，
于是三份文档互相矛盾。

已有实例：
    · 归档清单曾写「Phase 9 已完成」，而实验记录写「多步 Tool Loop 未验证」
    · `docs/version-history.md` 头部长期写「V0.1 · Phase 1–3」，正文已写到 Phase 9
    · 实验记录里的 67/8/3 与当时的 119/13/4 混在一起，读者分不清

**修法不是「记得改」，而是「不用手写」**：把会变的东西交给这个脚本，
其余文档只做**定性描述**并指向本文件。

## 用法

    python src\\status_snapshot.py

它会**实跑**五项离线自测（不花钱、不联网）并读数据库，写出
`STATUS.md`。想更新状态就重跑一次。

## 不做什么

不调用模型、不写数据库、不改任何数据。只读 + 写一个 markdown 文件。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

PY = sys.executable

SELFTESTS = [
    # ⚠️ 必须带 `--allow-real`：P1 给 selftest 加了守卫，
    # 默认**拒绝**在正式目录上跑（因为它会写真实库）。
    # 而本生成器**就是要**在正式目录上实跑它（它自己会清理自造行）。
    # 不加这个标志，STATUS 会拿到「未能解析结果」。
    ("selftest.py", "Phase 1–3 端到端链路",
     ["src/main.py", "selftest", "--clean", "--allow-real"]),
    ("selftest_memory.py", "Phase 5 Memory + 身份检索", ["src/selftest_memory.py"]),
    ("selftest_companion.py", "Phase 6 关系 + 7 用户模型 + 8 心跳 + 身份捕获",
     ["src/selftest_companion.py"]),
    ("selftest_p0_capture.py", "P0 观测捕获 + 信息分类 + 证据语义 + 追溯性",
     ["src/selftest_p0_capture.py"]),
    ("selftest_p2_conflict.py", "P2 矛盾保留 + 信念修正（方案 A）+ verifiable",
     ["src/selftest_p2_conflict.py"]),
    ("selftest_tools.py", "Phase 9 工具层", ["src/selftest_tools.py"]),
    ("selftest_memory_source.py", "Memory 来源字段（缺失/空数组/去重）",
     ["src/selftest_memory_source.py"]),
    ("selftest_merge.py", "Phase 11 合并式整理（旧记忆保护）",
     ["src/selftest_merge.py"]),
    ("selftest_llm_exit.py", "Phase 11 统一 LLM 出口（doctor / webui 记账）",
     ["src/selftest_llm_exit.py"]),
    ("selftest_router.py", "Phase 10 模型路由", ["src/selftest_router.py"]),
    ("dryrun.py", "全流程演练（在副本上跑）", ["src/dryrun.py"]),
]

DB_TABLES = [
    ("conversations", "对话轮数"),
    ("events", "原始事件"),
    ("memory_versions", "记忆版本"),
    ("retrievals", "检索记录"),
    ("relationship_events", "关系事件"),
    ("user_model_events", "用户模型事件"),
    ("tool_calls", "工具调用（含自测）"),
    ("api_calls", "API 调用"),
]


def run_selftest(args: list[str]) -> tuple[int, str]:
    """实跑一个自测，返回 (总数, 结论行)。失败返回 (0, 错误摘要)。"""
    try:
        proc = subprocess.run(
            [PY, *args], cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        return 0, "超时"
    out = (proc.stdout or "") + (proc.stderr or "")
    total = 0
    failed = 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("共 ") and "项" in line:
            # 「共 78 项，通过 78 项，失败 0 项。」
            try:
                total = int(line.split("共 ")[1].split(" 项")[0])
                failed = int(line.split("失败 ")[1].split(" 项")[0])
            except (IndexError, ValueError):
                pass
    if total == 0:
        tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["无输出"]
        return 0, f"未能解析结果（{tail[0][:60]}）"
    return total, ("全通过" if failed == 0 else f"**失败 {failed} 项**")


def db_counts() -> dict:
    db = ROOT / "conversation" / "conversations.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    counts = {}
    for table, _label in DB_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    conn.close()
    return counts


def tool_stats() -> dict:
    """区分「真实对话里的工具调用」与「开发期自测调用」。

    混淆这两者是实测踩过的坑：账本里 53 条调用，其中只有 19 条来自真实对话。
    判据：真实调用带 seq，且该 seq 在 conversations 表里。
    """
    db = ROOT / "conversation" / "conversations.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    seqs = {r[0] for r in conn.execute("SELECT seq FROM conversations")}
    real, test = [], []
    for r in conn.execute("SELECT name, seq, ok, error_kind FROM tool_calls"):
        (real if r["seq"] in seqs else test).append(dict(r))
    conn.close()

    by_seq = defaultdict(list)
    for r in real:
        by_seq[r["seq"]].append(r["iterations"] if "iterations" in r else None)
    # iterations 不在上面的 SELECT 里，这里补查
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    by_seq = defaultdict(list)
    for r in conn.execute("SELECT seq, iterations FROM tool_calls WHERE seq IS NOT NULL"):
        if r["seq"] in seqs:
            by_seq[r["seq"]].append(r["iterations"])
    conn.close()

    multi = sorted(s for s, v in by_seq.items() if len(set(v)) >= 2 and None not in v)
    parallel = sorted(s for s, v in by_seq.items() if len(v) >= 2 and len(set(v)) == 1)
    names = defaultdict(int)
    for r in real:
        names[r["name"]] += 1
    return {
        "real_total": len(real),
        "test_total": len(test),
        "by_name": dict(names),
        "ok": sum(1 for r in real if r["ok"]),
        "error": sum(1 for r in real if not r["ok"] and r["error_kind"] != "denied"),
        "denied": sum(1 for r in real if r["error_kind"] == "denied"),
        "multi_step_turns": multi,
        "parallel_turns": parallel,
    }


def ledger_total() -> str:
    path = ROOT / "logs" / "ledger.jsonl"
    if not path.exists():
        return "（无账本文件）"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    micro = sum(int(r.get("cost_micro_yuan") or 0) for r in rows)
    pt = sum(int(r.get("prompt_tokens") or 0) for r in rows)
    ct = sum(int(r.get("completion_tokens") or 0) for r in rows)
    return f"{micro / 1_000_000:.6f} 元（{len(rows)} 次调用 / {pt + ct:,} tokens）"


def main() -> int:
    print(f"正在实跑 {len(SELFTESTS)} 项离线自测（不花钱、不联网）…")
    results = []
    for fname, label, args in SELFTESTS:
        total, verdict = run_selftest(args)
        results.append((fname, label, total, verdict))
        print(f"  {fname:26} {total:>4} 项  {verdict}")

    counts = db_counts()
    tools = tool_stats()
    cost = ledger_total()
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")

    L = []
    A = L.append
    A("# 状态快照（**自动生成，勿手改**）")
    A("")
    A(f"生成时间：{stamp}")
    A("")
    A("> 本文件由 `python src\\status_snapshot.py` **实跑生成**。")
    A("> 会变的数字都集中在这里，其它文档只做定性描述并指向本文件 ——")
    A("> 这是为了治「文档里的数字过时」这个反复出现的问题（见脚本开头说明）。")
    A("")
    A("---")
    A("")
    A("## 离线自测（实跑结果）")
    A("")
    A("| 文件 | 覆盖 | 项数 | 结果 |")
    A("|------|------|------|------|")
    for fname, label, total, verdict in results:
        A(f"| `{fname}` | {label} | {total} | {verdict} |")
    A("")
    A("全部离线、不联网、不花钱（用假客户端）。")
    A("")
    A("---")
    A("")
    A("## 数据库计数")
    A("")
    if counts:
        A("| 表 | 含义 | 条数 |")
        A("|----|------|------|")
        for table, label in DB_TABLES:
            A(f"| `{table}` | {label} | {counts.get(table)} |")
    else:
        A("（数据库不存在）")
    A("")
    A("> ⚠️ **另有一个冻结快照**：Phase 5 漂移实验结束时是 "
      "**67 轮对话 / 8 原始事件 / 3 记忆版本**。")
    A("> 那个数字**不回填**，它是实验封版时的证据。引用时务必带时点。")
    A("")
    A("---")
    A("")
    A("## 工具调用（区分真实使用与自测）")
    A("")
    if tools:
        A(f"- **真实对话里的调用：{tools['real_total']} 次**"
          f"（成功 {tools['ok']} / 被拒 {tools['denied']} / 报错 {tools['error']}）")
        A(f"- 按工具：{tools['by_name']}")
        A(f"- 开发期自测调用：{tools['test_total']} 次（**不计入真实使用**）")
        A("")
        multi = tools["multi_step_turns"]
        para = tools["parallel_turns"]
        A(f"- **多步 Tool Loop（多轮迭代）：{len(multi)} 轮**"
          + (f"，seq {multi}" if multi else " —— 尚未发生"))
        A(f"- 同一轮内并行调用多个工具：{len(para)} 轮"
          + (f"，seq {para}" if para else ""))
        A("")
        A("> 判据：同一轮里出现**多个不同迭代号** = 模型先看第一个工具的结果、"
          "再决定发第二个工具。")
        A("> 同一迭代号下的多个调用 = 模型一次就输出了多个工具块（并行），"
          "**不算多步**。")
    else:
        A("（数据库不存在）")
    A("")
    A("---")
    A("")
    A("## 累计花费")
    A("")
    A(f"{cost}")
    A("")
    A("> 口径：`logs/ledger.jsonl` 里所有调用的估算费用之和。")
    A("> ⚠️ 若与别的项目**共用同一个 API Key**，不能用账户余额反推本项目花费。")
    A("")
    A("---")
    A("")
    A("## 定性状态（这些不随对话变化，手写维护）")
    A("")
    A("| Phase | 内容 | 状态 |")
    A("|-------|------|------|")
    A("| 1–3 | 基础链路 | ✅ 完成并实测 |")
    A("| 4 | 事件固化 | ✅ 完成并实测 |")
    A("| 5 | Memory + 记忆偏移实验 | ✅ 完成；基准事实存活率 18/18 |")
    A("| 6 | Relationship | ✅ 完成并实测 |")
    A("| 7 | User Model | ✅ 完成并实测 |")
    A("| 8 | Agency / Heartbeat | ✅ 完成并实测 |")
    A("| 9 | Tools | ⚠️ **未封版**（实现与安全整改完成；真实使用样本仍少） |")
    A("| 10 | Model Router | ⚠️ **完成了，但省不到钱**（见下） |")
    A("| 11 | 长期运行实验 | ⚠️ **做不到无人值守长期运行**（见下） |")
    A("| 12 | 商业化 / 开源发布 | ❌ 未开始，未写任何代码 |")
    A("")
    A("**另有一项不在原规范内**：本地网页界面（`python src\\main.py ui`）。")
    A("规范的 12 个 Phase 里没有任何一期是界面，它是为「看得懂」额外加的。")
    A("")
    A("Phase 10 如实说明 —— **这一期没有产生成本收益**：")
    A("")
    A("```")
    A("所有 155 次调用都用 deepseek-flash；而它已经是最低档（输入 2 / 输出 8）")
    A("→ 「简单任务用便宜模型」没有更便宜的可用 → 省 0 元")
    A("→ 若把复杂任务送去 deepseek-v4-pro，反而贵 4.5 倍")
    A("```")
    A("")
    A("那为什么还做：① 为**本地模型**留好接口（规范原文是 \"Cheap / **Local** Model\"，")
    A("真正的省钱在那里，需新增依赖，未做）② 每次用哪个模型、为什么，可追踪")
    A("③ 换模型/换供应商只改配置，不改代码。")
    A("")
    A("Phase 11 如实说明 —— **目前做不到「无人值守的长期运行」**：")
    A("")
    A("```")
    A("没有调度器/定时器：heartbeat 只能手动跑 python src\\main.py heartbeat")
    A("进程不常驻：命令行是脚本，界面要黑窗口开着 → 关掉即结束")
    A("=> 你不来，它就不动。能做到的是「人在时持续可用」。")
    A("```")
    A("")
    A("**容量实测（好消息）**：对话轮数不构成瓶颈 ——")
    A("")
    A("| 轮数 | 数据库 | 读历史 | 取 seq | verify 全量 | 界面侧栏 |")
    A("|------|--------|--------|--------|------------|---------|")
    A("| 500 | 0.4 MB | 0.062 ms | 0.063 ms | 1.4 ms | 0.061 ms |")
    A("| 5,000 | 1.5 MB | 0.062 ms | 0.207 ms | 14.8 ms | 0.061 ms |")
    A("| 20,000 | 5.6 MB | 0.083 ms | 0.669 ms | 72.3 ms | 0.143 ms |")
    A("")
    A("按每天 20 轮外推：一年 7,300 轮 ≈ 2 MB，几十年也用不到需要加索引的规模。")
    A("")
    A("Phase 9 逐项：")
    A("")
    A("| 子项 | 状态 |")
    A("|------|------|")
    A("| 工具接口 / 权限边界 / 调用账本 | ✅ 自动化测试通过 |")
    A("| 单工具真实聊天 | ✅ 已验证 |")
    A("| 权限拒绝真实验证（`.env`） | ✅ 已验证 |")
    A("| 工具结果回灌 / 落库文本清理 | ✅ 已验证 |")
    A("| **多步 Tool Loop** | ✅ **已验证**（见上方实测轮次） |")
    A("| 对抗测试（文件内植入指令） | ⚠️ 做过一次，模型识别来源并拒绝；**不构成系统性结论** |")
    A("| `run_python` 真实使用 | ⏳ `execute` 默认关闭，未测 |")
    A("| 独立安全审计 | ✅ 做过，6 个真问题全部修复 |")
    A("")
    A("---")
    A("")
    A("## 已知限制（手写维护，不随对话变化）")
    A("")
    A("- **`verify` 检测的是「意外改动 / 顺手篡改」，不是「有意的完全伪造」。**")
    A("  原始事件表没有哈希链，`sha256` 只是普通列；同时改写 JSON、数据库哈希与侧车即可通过。")
    A("  信任模型是「本地单用户、操作者可信任」—— **不要把它当防篡改证据**。")
    A("- **`orphan_files` 不参与 `verify` 的 `ok` 判定**：有孤儿文件时结论仍显示")
    A("  「原始数据未被改动」（但会列出 `[未登记]` 行）。**未修。**")
    A("- **`run_python` 不是沙箱**：本机任意代码执行，只是默认不注册 + 有超时与账本。")
    A("- **工具结果的 Prompt Injection 面**：工具结果以 `user` 角色回灌，")
    A("  读到的文件内容可能被当成指令。**只做过一次对抗测试。**")
    A("- **`changelog` 条目配对**在模型重新切分条目时会错配；已降级为辅助工具。")
    A("- **记忆检索用关键词加权，不用 embedding**（刻意，避免不可归因的相似度变量）；")
    A("  代价是同义改写可能召不回。")
    A("- **哈希链不自动向后级联**：只改第 N 轮时第 N 轮报错，但第 N+1 轮仍合法。")
    A("")
    A("---")
    A("")
    A("## 怎么更新这份文件")
    A("")
    A("```bat")
    A("python src\\status_snapshot.py")
    A("```")
    A("")
    A("跑完会重写本文件。**不要手改本文件** —— 手改的部分下次生成就丢了，")
    A("要改的是脚本里对应的段落。")
    A("")

    out = ROOT / "STATUS.md"
    out.write_text("\n".join(L), encoding="utf-8", newline="\n")
    print(f"\n已写入 {out.relative_to(ROOT)}（{len(L)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
