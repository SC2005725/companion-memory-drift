"""版本间差异记录（Phase 5 → 对应《总项目指令》§5 的「为什么改变」）。

两版记忆之间生成一份机器可比对的差异记录，回答四个问题：

    新增了什么？删掉了什么？改写了什么？表达变了但意思没变的是什么？

## 一个必须说清楚的取舍：条目 ID 由我们分配，不由模型分配

`consolidate` 每次调用都会重新生成一份条目列表。如果让模型自己给 id，
它下次会给出完全不同的 id，于是每一版都会变成「全部删除 + 全部新增」，
diff 毫无意义。

所以 id 由我们按内容确定性派生（见 `stable_item_id`）。这样：

- 内容相同的重述 → id 相同 → diff 判为「无变化」，不会被噪声淹没
- 内容被改写 → id 变 → 通过 `source_event_ids` 重叠识别为 **modified**
- 同一句话被换种说法 → id 变、来源不变 → 识别为 **reworded**
- 整条消失 → id 消失且无替代 → 识别为 **removed**

## modified 与 reworded 怎么区分

两者都是「同一来源、旧条目消失、新条目出现」，只靠来源事件分不开。因为
v001→v002 里那条「用户曾在夜间时段工作约一年多」被改写成
「用户曾在晚上工作过一年多，白天睡不好」时，两条的 source_event_ids 完全相同。

所以再加一道**确定性的文本相似度**判据：字符 2-gram 的 Dice 系数。

| 相似度 | 判定 | 含义 |
|-------|------|------|
| ≥ 0.30 | **modified** | 素材还是那份，内容被改动了（首句被改写、细节被替换） |
| < 0.30 | **reworded** | 同一事实换了种说法（长句被压缩成短句） |

阈值 0.30 是经验值：它把「改写细节」和「压缩成一句」分开，且完全可复现。
**这是启发式，不是真理** —— 真正的判定基准是 `experiments/` 里人工标注的事实清单。
本文件只负责给出一个稳定、可解释、事后可复核的分类。

## 为什么不把 rewording 检测交给 embedding

`docs` 已排除向量方案：embedding 会引入一个与被测对象无关的语义相似度变量。
这里用字符重叠这个确定性判据近似，宁可粗糙也不要不可复现。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from paths import CHANGELOG_DIR, ensure_dirs

# modified / reworded 的分界。见模块说明，这是启发式阈值。
REWORD_SIMILARITY_THRESHOLD = 0.30

# 证据等级发生变化时的配对门槛。
#
# 踩过的坑：v002→v003 里，真正丢失的 B3「特别怕冷，冬天得搬进屋里」
# 被错误配对成 modified —— 因为 v003 那句「窗台上，冬天或六月份会开花」
# 用到了「冬天」「六月份」，相似度 0.3077 越过了 0.30。
# 结果「整条事实被改写、原事实实质丢失」被合并成「一句改写」，
# 恰恰漏掉了最该抓的漂移类型。
#
# 所以：证据等级变了就必须是「几乎逐字保留的改写」才算 modified，
# 否则如实报成 removed + added（那才是事实层面的真相）。
TYPE_CHANGE_SIMILARITY_THRESHOLD = 0.60

# 「内容被并入别条」的判定阈值。
#
# 某条 removed 的内容若有一半以上仍出现在本版其它条目里，就认为它是被**合并**了，
# 而不是丢失。两种情况在实验上含义完全不同，必须分开统计：
#     记忆丢失（removed）
#     事实合并（merged）—— docs 点名的漂移类型
MERGED_COVERAGE_THRESHOLD = 0.50

# 「被并入 / 被扩展」的判定阈值。
#
# 若旧条目的内容有 85% 以上被新条目保留、但新条目明显更长（对称相似度偏低），
# 就判为 absorbed：信息没丢，只是被扩展或并进了更大的条目。
#
# 为什么必须单列一类：模型整理时经常把两条并成一条。用对称相似度会把这种
# **正确保留了信息**的配对打成 0.11 分、误判为「换说法」，
# 甚至让配对算法放弃它去跟不相关的条目硬凑（实测发生过）。
ABSORBED_CONTAINMENT_THRESHOLD = 0.85


def _char_units(text: str) -> set[str]:
    """字符级可比较单元：单字 + 2-gram。用于相似度计算。"""
    clean = "".join((text or "").split())
    units = set(clean)
    units.update(clean[i : i + 2] for i in range(len(clean) - 1))
    return units


def similarity(a: str, b: str) -> float:
    """Dice 系数，值域 [0,1]。完全确定，不依赖任何随机或模型。

    Dice = 2|A∩B| / (|A|+|B|)

    **注意它的局限**：这是**对称**度量，会惩罚「一方是另一方子集」的情况。
    例如「这盆茉莉特别怕冷」与「这盆茉莉特别怕冷，冬天得搬进屋里」
    只得到 0.11 分 —— 明明后者完整保留了前者。所以配对判据不能只看它，
    还要看**包含度** `containment()`。
    """
    ua, ub = _char_units(a), _char_units(b)
    if not ua or not ub:
        return 0.0
    return 2 * len(ua & ub) / (len(ua) + len(ub))


def containment(part: str, whole: str) -> float:
    """`part` 的内容有多少比例出现在 `whole` 里。值域 [0,1]。

    用来判断「旧条目是否被新条目吸收了」。这是**方向性**度量，
    专门补 `similarity()` 在子集情形下的失效。
    """
    up, uw = _char_units(part), _char_units(whole)
    if not up:
        return 0.0
    if not uw:
        return 0.0
    return len(up & uw) / len(up)


def stable_item_id(type_: str, text: str, source_event_ids: list[str]) -> str:
    """按内容确定性派生条目 ID。

    刻意**不**把 source_event_ids 之外的元数据（confidence 等）算进去：
    confidence 抖动不应该被算成「记忆变了」。
    """
    payload = json.dumps(
        {
            "type": (type_ or "").upper(),
            "text": (text or "").strip(),
            "src": sorted(str(x) for x in (source_event_ids or [])),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "mem_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def diff(prev, curr) -> dict:
    """比较两个 MemoryVersion，返回差异记录。

    prev 允许为 None（第一版），此时全部记为 added。
    """
    prev_items = {it.id: it for it in (prev.items if prev else [])}
    curr_items = {it.id: it for it in curr.items}

    added, removed, modified, unchanged = [], [], [], []
    absorbed: list[dict] = []

    for item_id, item in curr_items.items():
        if item_id not in prev_items:
            added.append(item.to_dict())
        else:
            before = prev_items[item_id]
            if before.text.strip() != item.text.strip():
                modified.append(
                    {
                        "id": item_id,
                        "before": before.text,
                        "after": item.text,
                        "type_before": before.type,
                        "type_after": item.type,
                    }
                )
            else:
                unchanged.append(item_id)

    for item_id, item in prev_items.items():
        if item_id not in curr_items:
            removed.append(item.to_dict())

    # --- 把 added / removed 配对成「改写」或「换说法」 ---
    #
    # 因为条目 ID 由文本派生，任何文本改动都会让 ID 变，于是必然表现为
    # 「旧 ID 消失 + 新 ID 出现」。要报出 modified / reworded，就得把两边配起来。
    #
    # 配对策略（两级，完全确定）：
    #   1. 先按来源事件重叠配对 —— 同源的最可能是同一条记忆的改写
    #   2. 再按文本相似度配对 —— 剩下没有来源（source_event_ids 为空）的条目，
    #      例如「用户可能偏好安静、少社交的工作」这类推断，也能配对成功
    #
    # 配对后按相似度分类：≥0.30 记为 modified，否则记为 reworded。
    reworded: list[dict] = []
    pairs: list[tuple[dict, dict, set[str], float]] = []
    used_add: set[int] = set()
    used_rem: set[int] = set()

    def _try_pair(strategy) -> None:
        for ai, add in enumerate(added):
            if ai in used_add:
                continue
            best = None
            for ri, rem in enumerate(removed):
                if ri in used_rem:
                    continue
                score = strategy(add, rem)
                if score is None:
                    continue
                if best is None or score > best[1]:
                    best = (ri, score)
            if best is not None:
                ri, score = best
                used_add.add(ai)
                used_rem.add(ri)
                shared = set(add.get("source_event_ids") or []) & set(
                    rem.get("source_event_ids") or []
                )
                pairs.append((add, removed[ri], shared, similarity(rem["text"], add["text"])))

    def _type_changed(add: dict, rem: dict) -> bool:
        return (add.get("type") or "UNKNOWN") != (rem.get("type") or "UNKNOWN")

    # 第一级：来源事件有重叠，且证据等级一致
    _try_pair(
        lambda add, rem: (
            None
            if _type_changed(add, rem)
            else (
                len(set(add.get("source_event_ids") or [])
                    & set(rem.get("source_event_ids") or []))
                or None
            )
        )
    )
    # 第二级：没有来源可依据时的内容包含度兜底。
    #
    # 判据用 `containment(旧, 新)`（方向性）而不是对称的 `similarity`。
    # 原因见 similarity() 的说明：模型整理时经常把两条并成一条（新句是旧句的超集），
    # 对称度量会给这种**正确保留了信息**的配对打很低的分，导致误判为「换说法」、
    # 甚至让配对算法去跟不相关的条目硬凑。
    #
    #   containment ≥ 0.85 → 旧条目基本被新条目保留（并入 / 微调措辞）
    #   containment ≥ 0.15 → 有一定内容重叠，可作候选配对
    #   等级变了且 containment < 0.60 → 不配对（让它如实显示成 removed + added）
    def _text_rule(add, rem):
        cont = containment(rem["text"], add["text"])
        if _type_changed(add, rem):
            return cont if cont >= TYPE_CHANGE_SIMILARITY_THRESHOLD else None
        return cont if cont >= 0.15 else None

    _try_pair(_text_rule)

    for add, rem, shared, sim in pairs:
        cont = containment(rem["text"], add["text"])
        entry = {
            "before_id": rem["id"],
            "after_id": add["id"],
            "before": rem["text"],
            "after": add["text"],
            "similarity": round(sim, 4),
            "containment": round(cont, 4),
            "shared_source_event_ids": sorted(shared),
            "type_before": rem.get("type"),
            "type_after": add.get("type"),
        }
        if cont >= ABSORBED_CONTAINMENT_THRESHOLD and sim < REWORD_SIMILARITY_THRESHOLD:
            # 旧句内容基本被保留，但新句明显更长 —— 属于「被并入 / 被扩展」。
            # 信息没丢，分类上必须与「改写」区分开。
            absorbed.append(entry)
        elif sim >= REWORD_SIMILARITY_THRESHOLD:
            modified.append(entry)
        else:
            reworded.append(entry)

    # --- 未配对的 removed：区分「真丢失」与「内容被并入别条」 ---
    #
    # 为什么需要这一步：v002→v003 里 B3「特别怕冷，冬天得搬进屋里」的文字确实消失了，
    # 但 v003 那句「窗台上，冬天或六月份会开花」把「冬天」这个时间线索吞并了进去。
    # 只看 removed 列表，无法区分下面两种情况 —— 而它们的实验含义完全不同：
    #
    #   (1) 事实真的没了           → 记忆丢失
    #   (2) 事实被合并进另一条     → 事实合并（docs 点名的漂移类型）
    #
    # 判据：把 removed 条目的内容拆成 2-gram 集合，看它有多少落在**当前版本的全部条目**里。
    #   覆盖率高 → 内容其实还在，只是被并进了别条 → changed 列表里标 merged_into
    #   覆盖率低 → 真的没了 → 留在 removed 列表里
    def _content_units(text: str) -> set[str]:
        clean = "".join((text or "").split())
        return {clean[i : i + 2] for i in range(len(clean) - 1)} if len(clean) >= 2 else set()

    curr_all_text = " ".join(it.text for it in curr.items)
    curr_units = _content_units(curr_all_text)

    truly_removed = []
    merged = []
    for item in removed:
        units = _content_units(item["text"])
        if not units:
            truly_removed.append(item)
            continue
        covered = len(units & curr_units) / len(units)
        if covered >= MERGED_COVERAGE_THRESHOLD:
            item = dict(item)
            item["content_coverage"] = round(covered, 4)
            item["note"] = (
                "该条文字已不在，但其内容有 "
                f"{covered:.0%} 仍出现在本版其它条目里 —— 属于「事实被合并」，"
                "不是「记忆丢失」。请人工确认合并是否把两个时间点/两件事搞混了。"
            )
            merged.append(item)
        else:
            item = dict(item)
            item["content_coverage"] = round(covered, 4)
            truly_removed.append(item)
    #
    # 踩过的坑：早期只在 modified 里报类型变化，于是
    # 「卖家说是去年的苗」(UNKNOWN) → 「是去年的苗」(FACT) 因为文本相似度被归到
    # reworded 分支，类型漂移就漏报了 —— 而这正是 docs 列为 P1 的关键风险。
    # 所以这里把「类型变化」从分类结果里单独提取出来，两个分支都覆盖。
    type_changes = [
        e for e in (modified + reworded + absorbed)
        if e.get("type_before") != e.get("type_after")
    ]
    for e in type_changes:
        e["type_changed"] = True

    # 配对用掉的条目不再是「纯新增 / 纯删除」
    added = [a for i, a in enumerate(added) if i not in used_add]
    removed = [r for i, r in enumerate(removed) if i not in used_rem]

    return {
        "from_version": prev.version if prev else None,
        "to_version": curr.version,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": {
            "added": len(added),
            "removed": len(truly_removed),
            "modified": len(modified),
            "reworded": len(reworded),
            "absorbed": len(absorbed),
            "unchanged": len(unchanged),
            "type_changed": len(type_changes),
            "merged": len(merged),
        },
        "type_changes": type_changes,
        "merged": merged,
        "absorbed": absorbed,
        "added": added,
        "removed": truly_removed,
        "modified": modified,
        "reworded": reworded,
        "unchanged_ids": unchanged,
    }


def save_diff(record: dict) -> dict:
    """把差异记录落成文件。已存在则抛错，绝不覆盖。"""
    ensure_dirs()
    frm = record.get("from_version") or "v000"
    name = f"{frm}__{record['to_version']}"
    path = CHANGELOG_DIR / f"{name}.json"
    if path.exists():
        raise RuntimeError(f"差异记录已存在，拒绝覆盖：{path.name}")

    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.write_text(body + "\n", encoding="utf-8")
    (CHANGELOG_DIR / f"{name}.sha256").write_text(
        f"{digest}  {name}.json\n", encoding="utf-8"
    )
    return {"file": f"memory/changelog/{name}.json", "sha256": digest}


def format_diff(record: dict) -> str:
    c = record["counts"]
    lines = [
        f"版本差异：{record.get('from_version') or '(首版)'} → {record['to_version']}",
        f"  新增 {c['added']}    删除 {c['removed']}    合并 {c.get('merged', 0)}    "
        f"改写 {c['modified']}    换说法 {c['reworded']}    "
        f"被并入 {c.get('absorbed', 0)}    未变 {c['unchanged']}",
    ]
    for item in record["added"]:
        lines.append(f"  [+][{item['type']}] {item['text']}")
    for item in record["removed"]:
        cov = item.get("content_coverage")
        lines.append(f"  [-][{item['type']}] {item['text']}"
                     + (f"   （内容留存 {cov:.0%}，判定为真丢失）" if cov is not None else ""))
    for item in record.get("merged", []):
        lines.append(f"  [⊕] {item['text']}   （内容留存 {item.get('content_coverage', 0):.0%}）")
        lines.append("      该条文字消失，但内容被并入本版其它条目 —— 属于「事实合并」")
        lines.append("      而非「记忆丢失」。请确认合并有没有把两件事或两个时间点搞混。")
    for item in record["modified"]:
        flag = ""
        if item.get("type_changed"):
            flag = f"  ⚠ 证据等级变化 {item.get('type_before')} → {item.get('type_after')}"
        lines.append(f"  [~] 相似度 {item.get('similarity')}{flag}")
        lines.append(f"      {item['before']}")
        lines.append(f"   →  {item['after']}")
    for item in record["reworded"]:
        flag = ""
        if item.get("type_changed"):
            flag = f"  ⚠ 证据等级变化 {item.get('type_before')} → {item.get('type_after')}"
        lines.append(f"  [≈] 相似度 {item.get('similarity')}（换说法）{flag}")
        lines.append(f"      {item['before']}")
        lines.append(f"   →  {item['after']}")
    for item in record.get("absorbed", []):
        flag = ""
        if item.get("type_changed"):
            flag = f"  ⚠ 证据等级变化 {item.get('type_before')} → {item.get('type_after')}"
        lines.append(f"  [⊃] 被并入（旧句内容留存 {item.get('containment')}，新句更长）{flag}")
        lines.append(f"      {item['before']}")
        lines.append(f"   →  {item['after']}")
    if c.get("type_changed"):
        lines.append(
            f"  注意：本版出现 {c['type_changed']} 处证据等级变化。"
            "推断不得伪装成事实，请人工复核这些条目。"
        )
    if c.get("merged"):
        lines.append(
            f"  注意：本版出现 {c['merged']} 处「事实合并」。"
            "这是 docs 点名的漂移类型，务必人工核对是否把不同事实或时间点并在了一起。"
        )
    return "\n".join(lines)
