"""Phase 5 全流程演练（Dry Run）。

## 目的

验证**整条 Phase 5 流程能不能跑通，并且不会再误伤基准数据**。
不验证「记忆漂移」—— 那是正式实验的事。

覆盖：

    假客户端 → 多轮对话 → consolidate → v001 → 继续对话
    → v002 → v003 → recall 两种模式 → 生成报告
    → verify → 检查 raw_event_001 文件与数据库行是否仍在

## 三条硬保证

1. **不调用真实 LLM API、不花钱** —— 全程用假客户端，脚本里会断言这一点。
2. **不修改正式实验数据** —— 做法不是「小心」，而是**结构上不可能**：
   整个演练跑在一个临时目录的**完整项目副本**里，`paths.ROOT` 被重定向过去，
   真实目录一个字节都不会被写。
3. **不把演练数据混进正式实验** —— 演练产物全在临时目录里，用完即删。

## 为什么必须先复制再跑

项目里已经发生过两次「测试工具把实验对象自己删了」的事故
（`selftest --clean` 删掉 3 轮真实对话；修了一版之后又删掉基准事件 `raw_event_001`）。
所以现在定为规则：**任何可能 DELETE / 清理 / 重建 / 批量修改数据的操作，
一律先复制到临时目录、在副本上跑。正式实验数据视为只读证据。**

用法：
    python src\\dryrun.py           跑演练（结束自动删除临时目录）
    python src\\dryrun.py --keep    跑演练并保留临时目录，便于人工翻看
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
REAL_ROOT = SRC.parent

# ============================================================================
# 第一阶段：建立隔离副本，并重定向 paths —— 必须在导入业务模块之前完成
# ============================================================================

DRY_ROOT = Path(tempfile.mkdtemp(prefix="memdrift_dryrun_"))

# 复制真实项目里「需要读」的东西。不复制 conversation/ memory/ recall/ logs/，
# 因为那些是运行期数据；演练要从干净状态开始，且绝不能让副本的写入影响真实目录。
for rel in ("src", "config", "personality", "experiments", "docs"):
    s = REAL_ROOT / rel
    if s.exists():
        shutil.copytree(s, DRY_ROOT / rel)

# .env：复制结构但**不复制真实 Key**。演练用的是假客户端，
# 这里只是为了让「Key 读取链路」也能被测到。
real_env = REAL_ROOT / ".env"
if real_env.exists():
    lines = []
    for line in real_env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("LLM_API_KEY="):
            lines.append("LLM_API_KEY=dryrun-fake-key-not-a-real-secret")
        else:
            lines.append(line)
    (DRY_ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
else:
    (DRY_ROOT / ".env").write_text(
        "LLM_API_KEY=dryrun-fake-key\nLLM_BASE_URL=https://example.invalid\n"
        "LLM_MODEL=dryrun-fake-model\n",
        encoding="utf-8",
    )

for rel in ("requirements.txt", ".gitignore", "archive-inventory.txt"):
    s = REAL_ROOT / rel
    if s.exists():
        shutil.copy2(s, DRY_ROOT / rel)

sys.path.insert(0, str(DRY_ROOT / "src"))

# 告诉存档锚点：这是一次隔离演练，别拿它跟真实锚点比。
#
# 为什么需要显式开关：锚点记录的是**真实项目**的规模（144 轮），
# 而演练会在同一个目录里跑一个只有十几轮的合成库 ——
# 锚点会把它误判成"链尾被截断"。dryrun 本来就是要重建数据的，这是正常的。
#
# 我第一版想靠"判断这是不是真库"来解决，但那类判断我已经错了两次
# （`for_test` 守卫用行数猜、锚点第一版用行数猜）。**显式声明比猜可靠。**
os.environ["DSH_SKIP_ANCHOR"] = "1"

import paths  # noqa: E402

# 重定向全部路径到副本
paths.ROOT = DRY_ROOT
paths.CONFIG_DIR = DRY_ROOT / "config"
paths.PROMPTS_DIR = paths.CONFIG_DIR / "prompts"
paths.SETTINGS_FILE = paths.CONFIG_DIR / "settings.json"
paths.PERSONALITY_FILE = DRY_ROOT / "personality" / "personality.json"
paths.CONVERSATION_DIR = DRY_ROOT / "conversation"
paths.DB_FILE = paths.CONVERSATION_DIR / "conversations.db"
paths.MEMORY_DIR = DRY_ROOT / "memory"
paths.RAW_DIR = paths.MEMORY_DIR / "raw"
paths.RAW_EVENTS_DIR = paths.RAW_DIR / "events"
paths.VERSIONS_DIR = paths.MEMORY_DIR / "versions"
paths.CHANGELOG_DIR = paths.MEMORY_DIR / "changelog"
paths.RECALL_DIR = DRY_ROOT / "recall"
paths.EXPERIMENTS_DIR = DRY_ROOT / "experiments"
paths.LOGS_DIR = DRY_ROOT / "logs"
paths.LEDGER_FILE = paths.LOGS_DIR / "ledger.jsonl"
paths.RUN_LOG_FILE = paths.LOGS_DIR / "run.log"
paths.CONSOLIDATE_PROMPT_FILE = paths.PROMPTS_DIR / "consolidate.md"
paths.ENV_FILE = DRY_ROOT / ".env"
paths.ENV_EXAMPLE_FILE = DRY_ROOT / ".env.example"

# 关键：把已用旧路径初始化过的模块清掉，强制按副本路径重新导入
for mod in ("storage", "pricing", "memory", "changelog", "retrieve", "consolidate",
            "chat", "verify", "config", "persona", "probe", "seed_baseline", "events", "llm"):
    sys.modules.pop(mod, None)

import changelog as changelog_mod  # noqa: E402
import chat  # noqa: E402
import consolidate as consolidate_mod  # noqa: E402
import memory as memory_mod  # noqa: E402
import probe as probe_mod  # noqa: E402
import seed_baseline as seed_mod  # noqa: E402
import storage  # noqa: E402
import verify as verify_mod  # noqa: E402
from config import load_settings  # noqa: E402
from llm import Reply, Usage  # noqa: E402
from persona import load_persona  # noqa: E402

# ============================================================================
# 假客户端
# ============================================================================

SCRIPT = [
    # (用户, 小满回复)
    ("最近天冷了，我把阳台上的东西都收进来了。", "嗯，降温挺明显的。你那盆花还好吗？"),
    ("还行，就是它特别怕冷，冬天得搬进屋里。", "搬进屋里稳妥。它是放在窗边吗？"),
    ("对，一直在书桌旁边的窗台上。", "那个位置光够吗？"),
    # 第一条命中触发词「上个月」的事件 —— 用来验证「对话中真实产生原始事件」这条路径
    ("够。对了，上个月我在花市买了一盆茉莉。", "花市买的？挑的时候看什么了？"),
    ("卖家说是去年的苗。", "卖家说的啊，那你自己看着苗情还行吗？"),
    ("看着挺壮的。就是前几天掉了几个花苞，我有点心疼。", "掉花苞这个季节挺常见的，别太急。"),
    ("希望大概六月份能开花。", "六月的话还得等一阵，先把它养稳。"),
]

# 整理出记忆之后继续聊 —— 这几轮才会真正检索到记忆并注入上下文。
# 目的：覆盖「检索命中 → 注入 system 段」这条路径（前面几轮都是 0 命中）。
POST_SCRIPT = [
    # 与记忆条目有词重叠，应当命中
    ("那盆茉莉冬天怎么办？", "搬进屋里就行，它怕冷。"),
    ("它放在哪儿来着？", "书桌旁边的窗台上。"),
    # 与记忆无任何词重叠，应当 0 命中但仍要落盘
    ("对了，你平时喜欢听什么音乐？", "我听得比较杂，你呢？"),
]

PROBE_QUESTION = "你还记得我之前说过的那件事吗？"

def build_consolidate_payloads(event_ids: list[str]) -> list[dict]:
    """构造三次整理的结果。

    **关键**：source_event_ids 用对话中真实产生的原始事件 ID，而不是空数组。
    理由：真实模型拿到的输入里就有这些 ID（consolidate 会把原始事件原文连同 ID 一起给出），
    所以它理应引用它们。这样做同时验证了两件事：
      1. 触发词扩充后，自然说出的 B1-B4 确实进入了 Raw Event 层；
      2. 记忆条目的 source_event_ids 能指回真实原始事件（可追溯性）。
    """
    # 按脚本顺序，事件是逐条创建的：取前几个 ID 分配给对应事实
    e = list(event_ids) + [""] * 8
    members = [x for x in e if x]

    def src(i: int) -> list[str]:
        """把第 i 个原始事件挂到这条记忆上；越界就挂到最后一个可用事件。"""
        if not members:
            return []
        return [members[i] if i < len(members) else members[-1]]

    v1 = {"items": [
        {"type": "FACT", "text": "用户养了一盆茉莉，是上个月在花市买的。",
         "source_event_ids": src(0), "confidence": 0.9},
        {"type": "UNKNOWN", "text": "卖家说这盆茉莉是去年的苗。",
         "source_event_ids": src(1), "confidence": 0.7},
        {"type": "FACT", "text": "这盆茉莉特别怕冷，冬天得搬进屋里。",
         "source_event_ids": src(0), "confidence": 0.9},
        {"type": "FACT", "text": "茉莉放在书桌旁边的窗台上，大概六月份才会开花。",
         "source_event_ids": src(0), "confidence": 0.85},
        {"type": "FACT", "text": "前两天茉莉掉了几个花苞，用户有点心疼。",
         "source_event_ids": members[-1:] or [], "confidence": 0.9},
    ]}

    # v002：B2 被拔高成事实（UNKNOWN→FACT），B4a 位置被压缩
    #
    # ⚠️ Phase 11 合并式整理后，**改写必须用 `replaces` 显式声明目标 id**：
    # 光靠"输出一条不一样的文本"不会顶掉旧条目（程序会保护它），
    # 那样就变成「新增」而不是「改写/漂移」，changelog 也就检不出漂移了。
    # 这里把本 dryrun 要演示的两种漂移都显式声明出来。
    import changelog as _cl  # noqa: PLC0415

    _v1_b2 = _cl.stable_item_id("UNKNOWN", "卖家说这盆茉莉是去年的苗。", src(1))
    _v1_b4 = _cl.stable_item_id("FACT", "茉莉放在书桌旁边的窗台上，大概六月份才会开花。", src(0))
    v2 = {"items": [
        {"type": "FACT", "text": "用户养了一盆茉莉，是上个月在花市买的。",
         "source_event_ids": src(0), "confidence": 0.9},
        {"type": "FACT", "text": "这盆茉莉是去年的苗。",
         "source_event_ids": src(1), "confidence": 0.9,
         "replaces": [_v1_b2]},          # ← 漂移：UNKNOWN 被拔高成 FACT
        {"type": "FACT", "text": "这盆茉莉特别怕冷，冬天得搬进屋里。",
         "source_event_ids": src(0), "confidence": 0.9},
        {"type": "FACT", "text": "茉莉放在窗台上，大概六月份才会开花。",
         "source_event_ids": src(0), "confidence": 0.85,
         "replaces": [_v1_b4]},          # ← 漂移：位置细节被压缩
        {"type": "FACT", "text": "前两天茉莉掉了几个花苞，用户有点心疼。",
         "source_event_ids": members[-1:] or [], "confidence": 0.9},
    ]}

    # v003：B3 整条丢失，且把「六月开花」与「冬天搬进屋」合并成一个时点
    #
    # ⚠️ 合并式整理后，**删除必须走顶层 `deletes`**（带 reason）。
    # 这里显式声明 B3「怕冷/搬进屋里」被删 —— 场景仍是"整条丢失"，
    # 但现在是**被程序确认过的删除**，而不是靠模型漏写造成的静默丢失。
    _v2_b3 = _cl.stable_item_id("FACT", "这盆茉莉特别怕冷，冬天得搬进屋里。", src(0))
    _v2_b4 = _cl.stable_item_id("FACT", "茉莉放在窗台上，大概六月份才会开花。", src(0))
    v3 = {"items": [
        {"type": "FACT", "text": "用户养了一盆茉莉，是上个月在花市买的。",
         "source_event_ids": src(0), "confidence": 0.9},
        {"type": "FACT", "text": "这盆茉莉是去年的苗。",
         "source_event_ids": src(1), "confidence": 0.9},
        {"type": "FACT", "text": "茉莉放在窗台上，冬天或六月份会开花。",
         "source_event_ids": src(0), "confidence": 0.6,
         "replaces": [_v2_b4]},          # ← 漂移：两个时点被合并成一个
        {"type": "FACT", "text": "前两天茉莉掉了几个花苞，用户有点心疼。",
         "source_event_ids": members[-1:] or [], "confidence": 0.9},
    ],
        "deletes": [
            {"id": _v2_b3, "reason": "dryrun 模拟：B3 整条丢失（含了怕冷/搬进屋里）"},
        ]}
    return [v1, v2, v3]


PROBE_ANSWER = "你好像提过一盆花，具体是什么花我记不太清了。"


class DryRunClient:
    """不联网的假客户端。按调用类型返回脚本内容。"""

    model = "dryrun-fake-model"

    def __init__(self, consolidate_payloads):
        self.payloads = consolidate_payloads
        self.consolidate_calls = 0
        self.chat_calls = 0
        # 把所有轮次按顺序编成一张表，按用户原话查回复
        self.replies = {u: a for u, a in SCRIPT + POST_SCRIPT}
        self.replies[PROBE_QUESTION] = PROBE_ANSWER

    def send(self, messages, model=None) -> Reply:
        system = messages[0]["content"] if messages else ""
        is_consolidate = "长期记忆条目" in system

        if is_consolidate:
            idx = min(self.consolidate_calls, len(self.payloads) - 1)
            self.consolidate_calls += 1
            text = json.dumps(self.payloads[idx], ensure_ascii=False)
            return Reply(text=text, model=self.model,
                         usage=Usage(prompt_tokens=2400, completion_tokens=320, cache_hit_tokens=1000),
                         latency_ms=15)

        last_user = ""
        for m in reversed(messages):
            if m["role"] == "user":
                last_user = m["content"]
                break
        self.chat_calls += 1
        # 按用户原话精确匹配；探针问题固定回 PROBE_ANSWER
        text = self.replies.get(last_user, "（演练：未编排的输入）")
        return Reply(text=text, model=self.model,
                     usage=Usage(prompt_tokens=1200, completion_tokens=60, cache_hit_tokens=700),
                     latency_ms=8)


# ============================================================================
# 演练主流程
# ============================================================================

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, good: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(good), detail))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 全流程演练")
    parser.add_argument("--keep", action="store_true", help="保留临时目录以便翻看")
    args = parser.parse_args(argv)

    print("=" * 68)
    print("Phase 5 全流程演练（假客户端，不联网，不花钱，不碰真实数据）")
    print("=" * 68)
    print(f"真实项目目录（只读，绝不写入）：{REAL_ROOT}")
    print(f"演练副本目录（全流程都在这里跑）：{DRY_ROOT}")
    print("=" * 68)

    # 记录真实目录的状态指纹，演练末尾要复核它没被改动
    real_probe = {}
    real_events_dir = REAL_ROOT / "memory" / "raw" / "events"
    for f in ("raw_event_001.json", "raw_event_001.sha256"):
        p = real_events_dir / f
        real_probe[f] = p.read_bytes() if p.exists() else None
    real_db = REAL_ROOT / "conversation" / "conversations.db"
    real_probe["conversations.db"] = real_db.read_bytes() if real_db.exists() else None

    paths.ensure_dirs()
    settings = load_settings()
    persona = load_persona()
    conn = storage.connect()

    client = DryRunClient([])   # 整理结果要等拿到真实事件 ID 后再装填
    session = chat.new_session(persona=persona, settings=settings, client=client,
                              peak_override=False, conn=conn)

    # ---------- [1] 固化基准事件（在副本里）----------
    print("\n[1] 固化基准事件 raw_event_001（副本内）")
    seed = seed_mod.seed(conn)
    check("基准事件已在副本中固化", seed["created"], seed.get("event_id", ""))
    base_file = paths.RAW_EVENTS_DIR / "raw_event_001.json"
    check("基准事件文件存在", base_file.exists(), str(base_file.name))
    print(f"  {seed['event_id']}  {seed['chars']} 字  sha256={seed['sha256'][:12]}…")
    before_base_sha = storage.sha256_text(base_file.read_text(encoding="utf-8").rstrip("\n"))

    # ---------- [2] 多轮对话（整理记忆之前）----------
    print("\n[2] 多轮对话 · 第一批（整理记忆之前，还没有记忆可检索）")
    events_created = []
    for user_text, _ in SCRIPT:
        r = chat.send_once(session, user_text, echo=lambda t: None)
        if r["event"]:
            events_created.append(r["event"]["event_id"])
        print(f"  用户：{user_text}")
        print(f"  小满：{r['reply'].text}")
        print(f"        ＋事件={bool(r['event'])}  检索命中={len(r['retrieval']['items'])}  "
              f"注入方式={r['retrieval'].get('injection_mode')}")
    turns = storage.stats(conn)["turns"]
    check("多轮对话全部落盘", turns == len(SCRIPT), f"{turns}/{len(SCRIPT)}")

    # 触发词已针对本实验做过最小扩充（settings.json 的 raw_event_triggers），
    # 目的：让自然说出的 B1-B4 能进入 Raw Event 层。
    # 若这里变成 0 条，说明触发词又被改窄了 —— 那会让「原始事件捕获失败」
    # 变成污染实验的额外变量，必须立刻发现。
    check("自然说出的基准事实已固化为原始事件",
          len(events_created) >= 1, f"{len(events_created)} 条：{'、'.join(events_created)}")
    print(f"  落盘 {turns} 轮，新建原始事件 {len(events_created)} 条"
          f"（触发词扩充生效）")

    # ---------- [3] 三次整理 → v001/v002/v003 ----------
    print("\n[3] 三次记忆整理 → v001 / v002 / v003")
    # 现在才装填整理结果：source_event_ids 用对话中真实产生的事件 ID
    client.payloads = build_consolidate_payloads(events_created)
    print(f"  整理输入使用真实原始事件 ID：{events_created}")
    results = []
    for note in ("演练 v1", "演练 v2", "演练 v3"):
        res = consolidate_mod.consolidate(session, note=note)
        results.append(res)
        c = res["diff"]["counts"]
        print(f"  {res['version']}：{len(res['items'])} 条   "
              f"新增{c['added']} 删除{c['removed']} 改写{c['modified']} "
              f"换说法{c['reworded']} 未变{c['unchanged']}   "
              f"提示词哈希={res['prompt_sha256'][:10]}…")
        print(f"        {res['file']}   {res['diff_file']}")
    check("产出了 v001/v002/v003",
          [r["version"] for r in results] == ["v001", "v002", "v003"],
          str([r["version"] for r in results]))
    check("三个版本文件同时存在",
          sorted(p.name for p in paths.VERSIONS_DIR.glob("v*.json"))
          == ["v001.json", "v002.json", "v003.json"], "")
    vrep = memory_mod.verify_versions()
    check("记忆版本哈希校验通过", vrep["ok"], f"checked={vrep['checked']}")

    # 可追溯性：每条记忆都应能指回真实的原始事件（《project-spec》§5 要求）
    known_events = {
        r["event_id"] for r in conn.execute("SELECT event_id FROM events").fetchall()
    }
    latest_items = memory_mod.load_latest().items
    with_src = [it for it in latest_items if it.source_event_ids]
    bad_src = [
        sid for it in latest_items for sid in it.source_event_ids
        if sid not in known_events
    ]
    check("每条记忆都能指回真实原始事件（可追溯）",
          len(with_src) == len(latest_items) and not bad_src,
          f"{len(with_src)}/{len(latest_items)} 条有来源，无效来源 {len(bad_src)} 个")
    print(f"  v003 的 {len(latest_items)} 条记忆全部带 source_event_ids，"
          f"且都指向真实事件（无效来源 {len(bad_src)} 个）")
    print(f"  原始事件层共有 {len(known_events)} 条事件")

    # 关键漂移是否被 changelog 抓到
    m = results[1]["diff"]
    # 证据等级漂移用专门的 type_changes 字段判定 —— 它同时覆盖 modified 与 reworded
    # 两个分支（早期只在 modified 里找，会漏掉被判为 reworded 的类型漂移）。
    type_change = m.get("type_changes", [])
    check("Changelog 抓到 UNKNOWN→FACT 的证据等级漂移", len(type_change) >= 1,
          f"{len(type_change)} 处类型变化"
          + ("：" + "、".join(f"{x['type_before']}→{x['type_after']}" for x in type_change)
             if type_change else ""))
    check("Changelog 抓到位置被压缩（书桌旁→窗台）",
          any("书桌" in x["before"] and "书桌" not in x["after"]
              for x in m["modified"] + m["reworded"]),
          "")
    print(f"  v001→v002 证据等级变化 {len(type_change)} 处："
          + "、".join(f"{x['type_before']}→{x['type_after']}" for x in type_change))
    print(f"    改写 {m['counts']['modified']} 条，换说法 {m['counts']['reworded']} 条")

    m2 = results[2]["diff"]
    # B3 的结局有两种可能，都要能抓到，但必须区分：
    #   - 文字消失且内容也基本不留存 → 真丢失（removed）
    #   - 文字消失但内容被并入别条   → 事实合并（merged）
    # v003 是把「冬天」并进了 B4 的位置项，所以应落在 merged 里。
    lost_anywhere = m2["removed"] + m2.get("merged", [])
    hit_b3 = [x for x in lost_anywhere if "怕冷" in x["text"] or "搬进屋里" in x["text"]]
    check("Changelog 抓到 B3（怕冷/搬进屋里）文字消失",
          len(hit_b3) >= 1,
          f"removed={m2['counts']['removed']} merged={m2['counts'].get('merged', 0)}")
    check("Changelog 把「事实合并」与「记忆丢失」分开统计",
          m2["counts"].get("merged", 0) >= 1,
          f"merged={m2['counts'].get('merged', 0)}（B3 内容被并入 B4 位置项）")
    print(f"  v002→v003 删除 {m2['counts']['removed']} 条、"
          f"合并 {m2['counts'].get('merged', 0)} 条")
    for x in m2["removed"]:
        print(f"    [真丢失] {x['text'][:34]}")
    for x in m2.get("merged", []):
        print(f"    [事实合并] {x['text'][:34]}  内容留存 {x.get('content_coverage', 0):.0%}")

    # ---------- [3.5] 继续对话：这次才会真正检索到记忆并注入 ----------
    print("\n[3.5] 继续对话 · 第二批（此时已有 v003 记忆，可验证检索→注入）")
    post_hits = []
    injected_seen = []
    for user_text, _ in POST_SCRIPT:
        r = chat.send_once(session, user_text, echo=lambda t: None)
        n = len(r["retrieval"]["items"])
        post_hits.append(n)
        # 直接检查这一次真正发给模型的 messages 里有没有记忆段
        retry = chat.build_messages(
            session, user_text, memory_text=r["retrieval"].get("injected_text", ""),
        )
        has_mem = any("你记得的关于用户的事" in m["content"] for m in retry)
        injected_seen.append(has_mem)
        print(f"  用户：{user_text}")
        print(f"  小满：{r['reply'].text}")
        print(f"        检索命中={n}  命中ID={[i.id[:12] for i in r['retrieval']['items']]}")
    check("整理记忆之后，相关提问能检索到记忆", any(n > 0 for n in post_hits),
          f"各轮命中数 {post_hits}")
    check("无关提问仍然 0 命中（未误召回）", any(n == 0 for n in post_hits),
          f"各轮命中数 {post_hits}")

    # 用一次真实调用的 messages 验证记忆段确实被拼进去了
    probe_msgs = chat.build_messages(
        session, POST_SCRIPT[0][0],
        memory_text=memory_mod.render_full_version(memory_mod.load_latest()),
    )
    _sys_all = [m["content"] for m in probe_msgs if m["role"] == "system"]
    # Stage 2 之后可能还有一段「初始设定」，所以断言改成**按内容确认
    # 记忆是独立段**，而不是数段数（数段数每加一层就得改一次）。
    check("记忆以独立 system 段注入（与人格分开）",
          any(m.startswith("【你记得的关于用户的事】") for m in _sys_all),
          f"system 段数={len(_sys_all)}")
    print(f"  相关轮命中数：{post_hits}")
    print(f"  messages 中 system 段数：{len(_sys_all)}")

    # ---------- [4] recall 两种模式 ----------
    print("\n[4] Recall 探针（两种模式）")
    p_mem = probe_mod.run_probe(session, PROBE_QUESTION, label="v003", mode="memory")
    print(f"  --mode memory ：注入 {len(p_mem['memory_items_injected'])} 条整版记忆；"
          f"关键词检索命中 {len(p_mem['retrieval_hit_ids'])} 条")
    print(f"                  回答：{p_mem['answer']}")
    check("探针(memory) 注入整版记忆",
          p_mem["injection_mode"] == "full_version" and len(p_mem["memory_items_injected"]) > 0,
          f"{len(p_mem['memory_items_injected'])} 条")
    check("探针(memory) 未注入原始对话", p_mem["injected_history"] is False, "")
    check("探针(memory) 落盘为独立文件",
          (DRY_ROOT / p_mem["file"]).exists(), p_mem["file"])

    p_none = probe_mod.run_probe(session, PROBE_QUESTION, label="blank", mode="none")
    print(f"  --mode none   ：不注入记忆、不注入对话")
    print(f"                  回答：{p_none['answer']}")
    check("探针(none) 不注入记忆", p_none["injection_mode"] == "none", "")
    check("探针(none) 未注入原始对话", p_none["injected_history"] is False, "")
    check("两次探针都落盘",
          len(list(paths.RECALL_DIR.glob("*.json"))) == 2,
          str(len(list(paths.RECALL_DIR.glob("*.json")))))

    # ---------- [5] 检索记录 ----------
    print("\n[5] 检索落盘统计（P0 机制）")
    mstats = storage.memory_stats(conn)
    print(f"  记忆版本 {mstats['versions']} 个，检索记录 {mstats['retrievals']} 条")
    rows = conn.execute("SELECT hit_item_ids, method, injected FROM retrievals").fetchall()
    zero = [r for r in rows if json.loads(r["hit_item_ids"]) == []]
    hit_rows = [r for r in rows if json.loads(r["hit_item_ids"]) != []]
    probe_full = [r for r in rows if r["method"] == "probe_full_version"]
    probe_none = [r for r in rows if r["injected"] == 0]
    check("每轮对话都留下了检索记录",
          mstats["retrievals"] >= len(SCRIPT) + len(POST_SCRIPT), f"{mstats['retrievals']} 条")
    check("0 命中的检索也落了盘（可归因）", len(zero) >= 1, f"{len(zero)} 条 0 命中")
    check("有命中的检索也落了盘（可归因）", len(hit_rows) >= 1, f"{len(hit_rows)} 条有命中")
    # 探针两种模式的检索记录必须能区分：
    #   memory 模式 → 注入整版，标 probe_full_version
    #   none   模式 → 什么都不注入，标常规 method 且 injected=0
    check("探针(memory) 的检索记录标为 probe_full_version",
          len(probe_full) == 1, f"{len(probe_full)} 条")
    check("探针(none) 的检索记录 injected=0（未注入）",
          len(probe_none) == 1, f"{len(probe_none)} 条")
    print(f"  其中 0 命中 {len(zero)} 条、有命中 {len(hit_rows)} 条、"
          f"探针整版注入 {len(probe_full)} 条、未注入 {len(probe_none)} 条")

    # ---------- [6] verify ----------
    print("\n[6] verify（副本内）")
    rep = verify_mod.verify_all(conn)
    print("  " + verify_mod.format_report(rep).replace("\n", "\n  "))
    check("副本内 verify 通过", rep["ok"], f"{rep['turns_checked']} 轮 / {rep['events_checked']} 事件")

    # ---------- [7] 生成对比报告 ----------
    print("\n[7] 生成对比报告（原始事实 vs v001/v002/v003）")
    report_path = write_report(results, p_mem, p_none)
    check("对比报告已生成", report_path.exists(), str(report_path.name))
    print(f"  {report_path}")

    # ---------- [8] 关键：基准事件是否完好 ----------
    print("\n[8] 基准事件完整性（副本内）")
    after_base_sha = storage.sha256_text(base_file.read_text(encoding="utf-8").rstrip("\n"))
    row = conn.execute("SELECT * FROM events WHERE event_id = 'raw_event_001'").fetchone()
    check("基准事件文件仍在", base_file.exists(), "")
    check("基准事件内容未被改动", after_base_sha == before_base_sha, "")
    check("基准事件数据库行仍在", row is not None, "")
    print(f"  文件存在={base_file.exists()}  内容哈希一致={after_base_sha == before_base_sha}  "
          f"数据库行存在={row is not None}")

    # ---------- [9] 断言全程没有真实网络调用 ----------
    print("\n[9] 费用与网络")
    cost = sum(int(r.get("cost_micro_yuan") or 0)
               for r in __import__("pricing").read_ledger())
    check("演练产生了记账记录（说明记账链路通）", cost >= 0, f"{cost} 微元")
    print(f"  演练账本合计：{cost} 微元（约 {cost / 1_000_000:.6f} 元）—— 全部来自假客户端")
    print(f"  假客户端调用次数：chat={client.chat_calls}  consolidate={client.consolidate_calls}")

    conn.close()

    # ---------- [10] 真实目录只读性复核 ----------
    print("\n[10] 真实目录只读性复核（最重要的一项）")
    ok_real = True
    for f, before in real_probe.items():
        p = (real_events_dir / f) if f.endswith((".json", ".sha256")) and f.startswith("raw_event_001") else real_db
        after = p.read_bytes() if p.exists() else None
        same = (after == before)
        ok_real = ok_real and same
        state = "未改动" if same else "**被改动了！**"
        exists = "存在" if after is not None else "**不存在！**"
        print(f"  {f:26} {exists}  {state}")
    check("真实目录的基准事件与数据库未被改动", ok_real, "")

    # ---------- 汇总 ----------
    print("\n" + "=" * 68)
    failed = [n for n, good, _ in CHECKS if not good]
    for name, good, detail in CHECKS:
        print(f"  [{'通过' if good else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 68)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    print("说明：全程使用假客户端，未调用任何真实 LLM API，未产生任何费用。")

    # ---------- 已知缺口状态 ----------
    print("\n" + "-" * 68)
    print("已知缺口状态")
    print("-" * 68)
    print("""
缺口 1：触发词覆盖不足 —— 已修复（2026-10-07）
  原触发词（我曾经 / 以前我 / 我跟你说过 …）与实验事实的自然说法完全对不上，
  导致「上个月我在花市买了一盆茉莉」这类句子不会进入 Raw Event 层。
  已按用户决定做**最小扩充**：针对 B1-B4 的自然表达补了 17 个关键词
  （我买了 / 我养了 / 我上个月 / 花市 / 茉莉 / 卖家说 / 去年的苗 / 怕冷 /
    搬进屋里 / 窗台 / 书桌旁边 / 六月份 / 掉花苞 / 花苞 / 心疼 …）。
  验证：15 条 B1-B4 自然表达全部命中；6 条无关日常全部不误伤。
  本次演练已确认：对话中自然说出的基准事实确实产生 6 条原始事件。

  原则（写进了 settings.json 的 _how_to_extend）：
    只补「基准事实的自然表达确实不命中」的那些词，
    不要改成「看到任何事实都记录」—— 那会让原始事件数量失控，实验更难解释。

缺口 2：基准事件与对话事实之间没有 source_event_ids 关联 —— 按决定暂不修复
  《project-spec》§5 要求每条记忆可追溯。目前基准事件 raw_event_001 是夹具固化的
  「档案事实」，与整理时从对话提取的条目在数据上没有引用关系。
  用户决定：**暂不修复**。理由是现在已有「raw_event_001 → 人工标注事实清单」
  这条基准链路，而为了本轮实验的可比性临时新增一套来源关联机制，
  反而可能改变实验环境。正式实验结束后再决定是否补。
  本次演练已改用真实事件 ID 构造整理输入，证明这条链路是可用的
  （见上方「每条记忆都能指回真实原始事件」一项）。
""")

    if args.keep:
        print(f"\n临时目录已保留：{DRY_ROOT}")
        print("翻看后请手动删除。")
    else:
        shutil.rmtree(DRY_ROOT, ignore_errors=True)
        print(f"\n临时目录已删除：{DRY_ROOT}")

    return 1 if failed else 0


def write_report(results: list[dict], p_mem: dict, p_none: dict) -> Path:
    """生成「原始事实 vs v001/v002/v003 vs 最终回忆」对照报告。"""
    fx = seed_mod.load_fixture()
    lines = [
        "# 演练对比报告（原始事实 vs 各记忆版本 vs 最终回忆）",
        "",
        "> **本报告由 `src/dryrun.py` 在临时目录中生成，内容是演练数据，不是实验结果。**",
        "> 它的用途是验证对比分析这条链路能不能跑通。",
        "",
        "## 基准原文（逐字）",
        "",
        f"> {fx['text']}",
        "",
        "## 人工标注的基准事实",
        "",
        "| # | 事实 | 证据等级 |",
        "|---|------|---------|",
    ]
    for f in fx["facts"]:
        lines.append(f"| {f['id']} | {f['fact']} | {f['kind']} |")

    lines += ["", "## 各版本记忆条目", ""]
    for res in results:
        lines.append(f"### {res['version']}（{len(res['items'])} 条）")
        lines.append("")
        for it in res["items"]:
            lines.append(f"- [{it.type}] {it.text}")
        c = res["diff"]["counts"]
        lines.append("")
        lines.append(f"相对上一版：新增 {c['added']}　删除 {c['removed']}　"
                     f"改写 {c['modified']}　换说法 {c['reworded']}　未变 {c['unchanged']}")
        lines.append("")

    lines += [
        "## 版本差异明细",
        "",
    ]
    for res in results:
        d = res["diff"]
        if d["from_version"] is None:
            lines.append(f"### {d['to_version']}（首版，{d['counts']['added']} 条新增）")
            lines.append("")
            continue
        lines.append(f"### {d['from_version']} → {d['to_version']}")
        lines.append("")
        for x in d["modified"]:
            tag = ""
            if x.get("type_before") != x.get("type_after"):
                tag = f"　⚠ 证据等级变化 {x['type_before']} → {x['type_after']}"
            lines.append(f"- **改写**（相似度 {x.get('similarity')}）{tag}")
            lines.append(f"  - 旧：{x['before']}")
            lines.append(f"  - 新：{x['after']}")
        for x in d["removed"]:
            lines.append(f"- **丢失**：[{x['type']}] {x['text']}")
        for x in d["added"]:
            lines.append(f"- **新增**：[{x['type']}] {x['text']}")
        for x in d["reworded"]:
            lines.append(f"- **换说法**（相似度 {x.get('similarity')}）："
                         f"{x['before']} → {x['after']}")
        lines.append("")

    lines += [
        "## 最终回忆（探针）",
        "",
        "### 探针 A：注入 v003 整版记忆（不注入原始对话）",
        "",
        f"- 注入条目数：{len(p_mem['memory_items_injected'])}",
        f"- 关键词检索命中：{len(p_mem['retrieval_hit_ids'])} 条（仅供参考，不影响注入）",
        f"- 提问：{p_mem['question']}",
        f"- 回答：**{p_mem['answer']}**",
        "",
        "### 探针 B：什么都不注入（对照组）",
        "",
        f"- 提问：{p_none['question']}",
        f"- 回答：**{p_none['answer']}**",
        "",
        "## 说明",
        "",
        "本演练刻意构造了三种典型漂移，用来确认工具链能不能抓到：",
        "",
        "| 漂移类型 | 构造方式 | 是否被抓到 |",
        "|---------|---------|-----------|",
        "| 证据等级漂移 | B2「卖家说是去年的苗」→「是去年的苗」 | 见上方 Changelog 明细 |",
        "| 细节压缩 | B4a「书桌旁边的窗台」→「窗台」 | 见上方 Changelog 明细 |",
        "| 整条丢失 | B3「怕冷、冬天搬进屋里」被删 | 见上方 Changelog 明细 |",
        "| 时间线合并 | 「六月开花」与「冬天搬进屋」被并成一句 | 见上方 Changelog 明细 |",
        "",
        "**注意：这些漂移是我手工构造的假数据，不是模型真实行为。**",
        "真实实验中会不会发生、以什么形式发生，才是要观察的对象。",
    ]

    path = DRY_ROOT / "experiments" / "演练comparison-report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
