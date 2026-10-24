"""对话 → 长期记忆（Phase 5）。

流程：
    取原始事件 + 取对话记录 → 拼整理提示词 → 调用模型 → 解析 → 落成 vN + changelog

## 三个必须守住的点

1. **提示词外置且可版本化**（`config/prompts/consolidate.md`）。它的 sha256 会写进
   每个记忆版本。理由：如果提示词自带压缩倾向，V2 的信息丢失就是你写死的，
   不是模型自然漂移 —— 那样整个实验结论作废。留着哈希才能事后证明当时用的是哪版提示词。

2. **条目 ID 由程序派生，不由模型分配**。模型每次给的 id 都不同，若采信它，
   每版 diff 都会变成「全部删除 + 全部新增」，changelog 直接失去意义。
   见 `changelog.stable_item_id`。

3. **来源必须可追溯**。每条记忆的 source_event_ids 指向 `memory/raw/events/`；
   模型编造的、不存在的 ID 会被剔除并记入日志，不静默采信。
"""

from __future__ import annotations

import hashlib
import json
import re

import changelog as changelog_mod
import memory as memory_mod
import storage
from paths import CONSOLIDATE_PROMPT_FILE

MAX_CONVERSATION_CHARS = 24000

# 哨兵：用于区分「字典里没有这个键」与「键存在但值是 None/空」。
#
# Phase 11 第三步用它区分两种截然不同的情况：
#   · 模型返回 `"source_event_ids": []`  → prompt 明确要求（仅依据对话）
#   · 模型**整个字段没输出**             → 没遵守 prompt
# 两者都该按空来源处理，但**前者不该报警、后者该报警**。
# 用 `dict.get(key, _MISSING)` 能一次拿到「缺失」这个事实。
_MISSING = object()


class ConsolidateError(RuntimeError):
    pass


def load_prompt() -> tuple[str, str]:
    """返回 (提示词正文, sha256)。提示词缺失时直接报错，不退回内置默认值 ——
    悄悄换提示词会让实验前后不可比。"""
    if not CONSOLIDATE_PROMPT_FILE.exists():
        raise ConsolidateError(
            f"缺少整理提示词：{CONSOLIDATE_PROMPT_FILE.as_posix()}。"
            "提示词必须外置且版本化，不允许用代码里的默认值兜底。"
        )
    body = CONSOLIDATE_PROMPT_FILE.read_text(encoding="utf-8")
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def collect_events(conn) -> list[dict]:
    """全部原始事件（按时间升序）。原始事实层，记忆只能引用不能改写。"""
    rows = conn.execute(
        "SELECT event_id, created_at_local, user_text FROM events ORDER BY created_at_utc ASC"
    ).fetchall()
    return [
        {"event_id": r["event_id"], "at": r["created_at_local"], "text": r["user_text"]}
        for r in rows
    ]


def collect_conversation(conn, since_seq: int = 0) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, created_at_local, user_text, assistant_text FROM conversations "
        "WHERE seq > ? ORDER BY seq ASC",
        (since_seq,),
    ).fetchall()
    return [
        {
            "seq": r["seq"],
            "at": r["created_at_local"],
            "user": r["user_text"],
            "assistant": r["assistant_text"],
        }
        for r in rows
    ]


def build_user_message(events: list[dict], turns: list[dict],
                       previous_items: list | None = None) -> str:
    """把原始事件、对话、**上一版记忆**拼成给模型的一整段输入。

    `previous_items` 是合并式整理的核心：把旧条目的 id 与正文一并列出，
    模型才有可能对它们做「保留 / 改写 / 合并 / 删除」决策。
    不列出来的话模型看不见旧条目 —— 这正是 v005 丢记忆的根因。
    """
    parts = []

    if previous_items:
        parts.append("## 现有记忆（上一版）")
        parts.append(
            "这些条目**已经存在**。请对每一条做决策，并在输出里体现：\n"
            "- **保留**：原文照抄进 `items`（推荐；这是默认结果）\n"
            "- **改写 / 合并**：写出新的 `text`，并用 `replaces` 填**被取代的旧条目 id**\n"
            "- **删除**：不要放进 `items`，改用顶层 `deletes` 列出 `{id, reason}`\n"
            "\n"
            "**程序会保护旧记忆**：凡是你没在 `items` 里出现、也没在 `deletes` 里"
            "明确要求删除的旧条目，都会被**原样保留**。所以漏写不会丢数据，"
            "但也别指望靠漏写来删除。"
        )
        for it in previous_items:
            src = it.source_event_ids or []
            parts.append(f"[{it.id}] ({it.type}) {it.text}\n    来源={src}")
    else:
        parts.append("## 现有记忆\n（这是第一版，没有任何旧条目）")

    if events:
        parts.append("\n## 原始事件（用户原话，逐字保存，不可改写）")
        for e in events:
            parts.append(f"[{e['event_id']}] {e['at']}\n{e['text']}")
    else:
        parts.append("\n## 原始事件\n（本次没有任何原始事件）")

    if turns:
        parts.append("\n## 对话记录")
        for t in turns:
            parts.append(f"#{t['seq']} 用户：{t['user']}\n   助手：{t['assistant']}")
    else:
        parts.append("\n## 对话记录\n（本次没有新对话）")

    text = "\n".join(parts)
    if len(text) > MAX_CONVERSATION_CHARS:
        # 截断要显式告知模型，否则它会以为这就是全部，反而产生错误的「无信息」判断。
        text = (
            text[:MAX_CONVERSATION_CHARS]
            + f"\n\n（注意：以上内容因长度上限被截断，仅包含前 {MAX_CONVERSATION_CHARS} 字符。）"
        )
    return text


def extract_json(text: str) -> dict:
    """从模型回复里取出 JSON 对象。

    容错三种常见输出：纯 JSON、带 ```json 围栏、前后夹了说明文字。
    """
    if not text or not text.strip():
        raise ConsolidateError("模型返回了空内容，无法整理记忆。")

    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ConsolidateError(
                f"模型回复不是合法 JSON：{exc}\n原始回复前 300 字：{text[:300]}"
            ) from exc
    raise ConsolidateError(f"模型回复里找不到 JSON 对象。原始回复前 300 字：{text[:300]}")


def parse_items(payload: dict, known_event_ids: set[str], version: str,
                *, now_iso: str | None = None,
                rel_rules: dict | None = None) -> tuple[list, list[str]]:
    """把模型给的 JSON 解析成 MemoryItem 列表。

    `now_iso`  ：写入条目的真实时间戳（V1.1）。不传则留 None（不伪造时间）。
    `rel_rules`：`settings.relationship`，供分类统一口径（见 memory.classify）。

    返回 (items, warnings)。未知来源事件 ID 会被剔除并记入 warnings ——
    宁可少一个来源，也不要留下指不到实处的引用。
    """
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ConsolidateError("模型回复里缺少 items 数组。")

    items, warnings = [], []
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            warnings.append(f"第 {idx} 条不是对象，已跳过")
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            warnings.append(f"第 {idx} 条 text 为空，已跳过")
            continue

        # 来源字段。**区分「字段缺失」与「显式空数组」**（Phase 11 第三步修复）。
        #
        # ## 为什么必须区分
        # `raw.get("source_event_ids") or []` 会把两种情况压成同一个结果：
        #   · 模型返回 `[]`            → prompt 第 29 行**明确要求**的行为（仅依据对话）
        #   · 模型**整个字段没输出**   → 没遵守 prompt
        # 两者都变成空数组、都无 warning，于是**事后无法区分**
        # 「确实没有对应事件」与「模型漏填」。那会让"无来源"无法归因。
        #
        # ## 用哨兵而不是 `in`
        # 等价于 `in`，但能直接拿到值，少一次查找。
        raw_src = raw.get("source_event_ids", _MISSING)
        if raw_src is _MISSING:
            warnings.append(
                f"第 {idx} 条没有输出 source_event_ids 字段"
                f"（已按空来源处理；prompt 要求该字段必填，可为空数组）"
            )
            src: list[str] = []
        else:
            src = [str(x) for x in (raw_src or [])]

        unknown = [x for x in src if x not in known_event_ids]
        if unknown:
            warnings.append(f"第 {idx} 条引用了不存在的原始事件 {unknown}，已剔除这些来源")
        # 去重并**保持首次出现顺序**。
        #
        # 为什么必须去重：`source_event_ids` 参与 `stable_item_id` 的哈希，
        # 所以模型某次多写一个重复 ID，该条目的 id 就变了 ——
        # changelog 会把它报成「条目变了」，而**内容其实没变**。
        # dict.fromkeys 保序去重，不引入新依赖。
        src = list(dict.fromkeys(x for x in src if x in known_event_ids))

        type_ = str(raw.get("type") or "UNKNOWN").upper()
        if type_ not in memory_mod.VALID_TYPES:
            warnings.append(f"第 {idx} 条 type={type_!r} 非法，降级为 UNKNOWN")
            type_ = "UNKNOWN"

        item_id = changelog_mod.stable_item_id(type_, text, src)
        # Memory V1：分类 + salience。**确定性规则**（不用模型判定），
        # 与 relationship 同思路 —— 「为什么归到这一类」必须可复现。
        # 模型若显式给了 category 就用它的（更准），否则走规则。
        #
        # V1.1：`rel_rules` 传入后，「共同经历」用**与关系系统同一套关键词**判定。
        raw_cat = str(raw.get("category") or "").strip()
        category = (raw_cat if raw_cat in memory_mod.CATEGORIES
                    else memory_mod.classify(text, rel_rules))
        # 用户显式强调（"记住" / "重要"）→ 提升 salience，对抗时间衰减
        salience = 0.6 if any(k in text for k in ("记住", "重要", "一定要记住")) else 0.0

        items.append(
            memory_mod.MemoryItem(
                id=item_id,
                type=type_,
                text=text,
                source_event_ids=src,
                confidence=raw.get("confidence"),
                first_seen_version=version,
                last_modified_version=version,
                category=category,
                salience=salience,
                # V1.1：写入**真实时间戳**（此前只有版本号，衰减形同虚设）
                created_at=now_iso,
                updated_at=now_iso,
            )
        )
    return items, warnings


def merge_items(payload: dict, known_event_ids: set[str], version: str,
                previous_items: list | None, *,
                now_iso: str | None = None,
                rel_rules: dict | None = None,
                legacy_texts: set[str] | None = None) -> tuple[list, list[str]]:
    """合并式整理：**程序负责保护旧记忆，模型只负责提出候选变更**。

    ## 为什么保护放在程序侧

    实测（v004 → v005）：只喂新对话却要求模型输出全量记忆，
    模型看不见旧条目 → 丢了 14 条，含「那盆茉莉已经缓过来了」。

    **不能靠提示词要求模型守规矩** —— 模型不稳定。所以：

      1. 先解析模型提出的 items（沿用 parse_items 的全部校验）
      2. 计算**被覆盖/被取代**的旧 id（通过 `replaces` 声明）
      3. 凡「没出现在 items、也没被 replaces 指向、且不在 deletes 里」的旧条目
         → **原样保留**（这是关键：漏写 = 保留，不是删除）
      4. `deletes` 里每条都必须：目标 id 真实存在 + 有非空 reason
         → 否则**拒绝该删除**并记 warning

    ## 为什么需要 `replaces`

    `stable_item_id(type, text, src)` 是**内容派生**的：
    只要模型改动了 text 或来源，id 就会变，程序无法从 id 判断
    「这是改写」还是「这是新增 + 旧条目被漏掉」。
    所以模型必须显式声明 `replaces: [旧id]`，程序才认这次改写。

    没声明 replaces 的旧条目**不会**被新文本"顶掉"，而是同时保留 ——
    宁可多留一条，也不静默丢一条。

    返回 (items, warnings)。
    """
    items, warnings = parse_items(payload, known_event_ids, version,
                                  now_iso=now_iso, rel_rules=rel_rules)

    if not previous_items:
        # 第一版，没有旧条目要保护
        return items, warnings

    prev_by_id = {it.id: it for it in previous_items}

    # ---- 0. Phase 12 P1：**继承旧时间戳，不伪造时间** ----
    #
    # ## 修的是什么（实测发现的真 bug）
    #
    # `parse_items` 给模型输出的**每一条**都写 `created_at=now_iso`。
    # 但模型经常把旧条目**原样重发**（文本一字不差）—— 那时它其实不是新记忆，
    # 只是被复述了一遍。
    #
    # 实测后果：v008 里文本与 v004 相同的 19 条**全部被套上 2026-10-24 的时间戳**，
    # 而它们的实际来源是 10-07 / 10-08。用户明确要求：
    # 「旧记忆缺失时间戳时继续保留未知状态，**不伪造时间**」。
    #
    # 伪造时间的危害不只是不诚实：这些"被变新"的旧记忆在衰减里拿到满分，
    # 永远不会被降权 —— 与「不因未知时间直接降权」是同一个原则的两面。
    #
    # ## 继承规则
    #
    # 文本完全相同 → 继承旧条目的 `created_at` / `updated_at` / `first_seen_version`。
    # 旧条目本身也是 None（v001-v004 的情况）→ **继续保持 None**，不补时间。
    # 文本变了（真改写）→ 不继承，按新条目处理（有 replaces 时旧条目另行留痕）。
    prev_by_text: dict[str, object] = {}
    for pit in previous_items:
        t = (getattr(pit, "text", "") or "").strip()
        if t and t not in prev_by_text:
            prev_by_text[t] = pit          # 取最早的那条（先到先得，可复现）

    inherited = 0
    legacy = {t.strip() for t in (legacy_texts or set()) if t and t.strip()}
    legacy_fixed = 0
    for it in items:
        key = (it.text or "").strip()
        old = prev_by_text.get(key)
        if old is None:
            continue
        it.created_at = getattr(old, "created_at", None)      # 可能是 None —— 那就保持未知
        it.updated_at = getattr(old, "updated_at", None)
        it.first_seen_version = getattr(old, "first_seen_version", None) or it.first_seen_version
        # 保留旧条目的分层与 salience（它们是历史判断，不该被复述冲掉）
        if getattr(old, "tier", None):
            it.tier = old.tier
        if getattr(old, "salience", None):
            it.salience = old.salience

        # **回溯纠正**：文本在「不记时间戳的年代」（V1.1 之前的 v001-v004）就已存在
        # → 它的真实时间本来就未知，即使上一版被套过时间戳也要退回未知。
        #
        # 为什么需要这一步：v005-v008 是在时间戳继承逻辑补上**之前**生成的，
        # 那 19 条旧记忆被错误地套上了整理时刻。用户要求「不批量重写历史数据」，
        # 所以不去改那些版本文件 —— 改为让**下一次整理**自然把它们纠正回去：
        # 这既守住了 append-only 纪律，又让衰减机制不再把旧记忆当新的。
        if key in legacy and it.created_at is not None:
            it.created_at = None
            it.updated_at = None
            legacy_fixed += 1
        inherited += 1

    if inherited:
        warnings.append(f"{inherited} 条为旧条目原样复述，已继承原时间戳（不伪造时间）")
    if legacy_fixed:
        warnings.append(
            f"回溯纠正 {legacy_fixed} 条：文本在 V1.1 之前就存在，真实时间未知 → 退回未知"
        )

    # ---- 1. 收集模型声明的 replaces / deletes ----
    replaced_ids: set[str] = set()
    raw_items = payload.get("items") or []
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            continue
        reps = raw.get("replaces")
        if reps is None:
            continue
        if not isinstance(reps, list):
            warnings.append(f"第 {idx} 条 replaces 不是数组，已忽略")
            continue
        for rid in reps:
            rid = str(rid)
            if rid in prev_by_id:
                replaced_ids.add(rid)
            else:
                warnings.append(f"第 {idx} 条 replaces 指向不存在的旧条目 {rid}，已忽略")

    # ---- 2. 校验 deletes（必须有理由 + 目标真实）----
    confirmed_deletes: set[str] = set()
    raw_dels = payload.get("deletes")
    if raw_dels is not None:
        if not isinstance(raw_dels, list):
            warnings.append("顶层 deletes 不是数组，已整体忽略（旧条目全部保留）")
        else:
            for idx, d in enumerate(raw_dels, start=1):
                if not isinstance(d, dict):
                    warnings.append(f"deletes 第 {idx} 项不是对象，已拒绝")
                    continue
                did = str(d.get("id") or "")
                reason = str(d.get("reason") or "").strip()
                if did not in prev_by_id:
                    warnings.append(f"deletes 第 {idx} 项目标 {did!r} 不存在，已拒绝")
                    continue
                if not reason:
                    warnings.append(f"deletes 第 {idx} 项（{did}）没有给出 reason，已拒绝")
                    continue
                confirmed_deletes.add(did)

    # ---- 3. 核心保护：漏写的旧条目原样保留 ----
    new_ids = {it.id for it in items}
    kept_back = []
    for pid, pit in prev_by_id.items():
        if pid in new_ids:
            continue          # 模型已包含（原文照抄）
        if pid in replaced_ids:
            continue          # 被显式改写取代
        if pid in confirmed_deletes:
            continue          # 被合法删除
        # 模型漏写了它，且没有合法删除理由 → 原样保留
        kept_back.append(pit)

    if kept_back:
        warnings.append(
            f"模型未提及 {len(kept_back)} 条旧记忆，已按「保留」处理（不视为删除）"
        )
        # 保留的条目保留其**原始版本标记**，不改 first_seen_version
        items = items + kept_back

    # ---- Memory V1：冲突留痕 + 分层 ----
    #
    # ① 被 replaces 取代的旧条目 → 标记 superseded_by。
    #
    # ## 关键设计：**不把旧条目复制进新版本**
    #
    # 实测教训（本轮回归抓到的真实 bug）：初版把被取代的旧条目 append 回 items，
    # 想"保留历史"。结果 v005 里同时有新条目和旧条目 →
    # `changelog.diff(v004, v005)` 把旧条目看成 **unchanged** →
    # **漂移检测失效**（modified/reworded 全变 0）。而漂移检测是本实验的核心功能。
    #
    # 正确做法：历史**本来就保存在 v004 文件里**（版本快照永不覆盖、永不删）。
    # 不需要在新版本里复制一份。这样：
    #   · 历史与来源 → 由 v001..v00N 各版本文件完整保留 ✅
    #   · 漂移可见   → 旧 id 消失 + 新 id 出现 → diff 照常检出 ✅
    #   · 冲突可查   → superseded_by 记在旧条目上（当它仍在 items 里时）+
    #                   `deletes` 里带 reason
    superseded_note = []
    for old_id in sorted(replaced_ids):
        pit = prev_by_id.get(old_id)
        if pit is None:
            continue
        # 只有当该旧条目**仍在**本次 items 里时才就地标记；
        # 否则它已被新条目取代（历史留在 prev 版本文件里）。
        for it in items:
            if it.id == old_id:
                it.superseded_by = it.superseded_by or f"由 {version} 的新记忆取代"
                it.archived_reason = "已被新记忆取代（历史见上一版）"
                it.tier = memory_mod.TIER_ARCHIVE
                break
        superseded_note.append(old_id)

    if superseded_note:
        warnings.append(
            f"{len(superseded_note)} 条旧记忆被取代（历史与来源保留在上一版本文件中）"
        )

    # ② 分层：core / experience / archive（就地修改，不新建存储）
    memory_mod.promote_tiers(items)
    n_core = sum(1 for i in items if i.tier == memory_mod.TIER_CORE)
    n_arch = sum(1 for i in items if i.tier == memory_mod.TIER_ARCHIVE)
    warnings.append(
        f"分层：核心 {n_core} 条 · 经历 {len(items)-n_core-n_arch} 条 · 归档 {n_arch} 条"
    )

    return items, warnings



def consolidate(session, *, note: str = "") -> dict:
    """执行一次记忆整理，产出新的记忆版本 + 与上一版的差异记录。

    `session` 是一个 chat.Session（复用它的 client / 记账 / 成本口径）。
    """
    prompt_body, prompt_sha = load_prompt()

    previous = memory_mod.load_latest()
    version = memory_mod.next_version_name()

    events = collect_events(session.conn)
    all_turns = collect_conversation(session.conn)
    if not all_turns and not events:
        raise ConsolidateError(
            "既没有对话记录也没有原始事件，无法整理记忆。先聊几句再整理。"
        )

    # 只整理上一次整理之后的新对话；原始事件则每次全量带上 ——
    # 因为「原始事实」是基准，不该随整理次数被逐渐丢掉。
    since_seq = 0
    if previous is not None and previous.source_range.get("conversation_to_seq"):
        since_seq = int(previous.source_range["conversation_to_seq"])
    new_turns = [t for t in all_turns if t["seq"] > since_seq]

    user_message = build_user_message(
        events, new_turns,
        previous_items=(previous.items if previous is not None else None),
    )
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": user_message},
    ]

    import chat as chat_mod

    # 整理任务需要比对话大得多的输出预算。
    #
    # 实测事故：整理 34 轮对话时，deepseek-flash（推理模型）**思考单独就吃了 3201 个 token
    # 还没结束**，`finish_reason='length'`、`content` 为空 —— 整个整理直接失败。
    # 原因：整理要一次性读完整段对话 + 全部原始事件，推理量远大于单轮闲聊。
    # 所以按用途取预算（settings.json 的 max_tokens_by_purpose.consolidate）。
    budget = int(session.settings.max_tokens_by_purpose.get("consolidate", 0) or 0)
    if budget > 0 and hasattr(session.client, "max_tokens"):
        session.client.max_tokens = budget
        # 重试预算也一并放大，否则「首次截断」后的重试仍然不够
        if hasattr(session.client, "empty_retry_max_tokens"):
            session.client.empty_retry_max_tokens = max(budget * 2, 8000)

    called = chat_mod.call_model(session, messages, purpose="consolidate")
    reply = called["reply"]

    payload = extract_json(reply.text)
    known = {e["event_id"] for e in events}
    # V1.1：本次整理的**统一时间戳**。同一次整理里所有新条目共用它，
    # 保证「同一批记忆的时间一致」，便于事后按批次追溯。
    now_iso = storage.now_local()
    # Phase 12 P1：算出「文本在 V1.1 之前就已存在」的集合 —— 那些记忆的真实时间
    # **本来就未知**（v001-v004 时代根本不记时间戳）。用于回溯纠正。
    legacy_texts: set[str] = set()
    try:
        import memory as _mem
        for _v in ("v001", "v002", "v003", "v004"):
            try:
                _mv = _mem.load_version(_v)
            except Exception:  # noqa: BLE001 — 缺某版不影响
                continue
            for _it in (_mv.items or []):
                _t = (_it.text or "").strip()
                # 只看「当时确实没有时间戳」的条目
                if _t and getattr(_it, "created_at", None) is None:
                    legacy_texts.add(_t)
    except Exception:  # noqa: BLE001 — 回溯失败就不纠正（不影响本次整理）
        legacy_texts = set()

    items, warnings = merge_items(
        payload, known, version,
        now_iso=now_iso,
        rel_rules=(getattr(session.settings, "relationship", None) or None),
        previous_items=(previous.items if previous is not None else None),
        legacy_texts=legacy_texts,
    )

    source_range = {
        "conversation_from_seq": (new_turns[0]["seq"] if new_turns else None),
        "conversation_to_seq": (all_turns[-1]["seq"] if all_turns else None),
        "conversation_turns_used": len(new_turns),
        "raw_events_used": len(events),
    }

    mv = memory_mod.MemoryVersion(
        version=version,
        created_at=storage.now_local(),
        model=reply.model,
        items=items,
        source_range=source_range,
        previous_version=(previous.version if previous else None),
        prompt_sha256=prompt_sha,
        note=note,
    )

    saved = memory_mod.save_version(mv)
    storage.record_memory_version(
        session.conn,
        version=version,
        model=reply.model,
        item_count=len(items),
        file=saved["file"],
        file_sha256=saved["sha256"],
        previous_version=(previous.version if previous else None),
        note=note,
    )

    diff_record = changelog_mod.diff(previous, mv)
    diff_saved = changelog_mod.save_diff(diff_record)

    storage.log_line(
        f"memory {version} created: {len(items)} items, "
        f"prompt_sha256={prompt_sha[:12]}, from={previous.version if previous else 'none'}"
    )

    return {
        "version": version,
        "items": items,
        "warnings": warnings,
        "file": saved["file"],
        "sha256": saved["sha256"],
        "diff": diff_record,
        "diff_file": diff_saved["file"],
        "reply": reply,
        "cost_micro_yuan": called["cost_micro_yuan"],
        "prompt_sha256": prompt_sha,
        "previous": previous.version if previous else None,
    }
