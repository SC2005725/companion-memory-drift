"""原始事件触发（Phase 3）。

为什么要这一步：任务书第五节要求「原始事件不能被覆盖」，那就得先有一个
「什么算原始事件」的确定性判据。

设计选择：**用关键词规则，不做额外的 API 判断**。理由有两条 —
1. 任务书要求 Phase 1-3 成本尽量低，每轮多发一次模型调用没必要；
2. 判据必须确定性、可复现 —— 用 LLM 判断「这句算不算事实」，本身就会引入随机性，
   将来无法解释「为什么这条被记了、那条没记」。

命中即把**用户原话整段**存为 raw_event（不改写、不摘要）。
"""

from __future__ import annotations

import re
from datetime import datetime

# 句末切分：中文标点 + 换行
_SPLIT = re.compile(r"[。！？!?；;\n]+")


def detect(text: str, keywords: list[str]) -> dict:
    matched: list[str] = []
    for kw in keywords or []:
        if not kw:
            continue
        if kw.isascii():
            if kw.lower() in text.lower():
                matched.append(kw)
        elif kw in text:
            # 身份类词要多做一道检查，否则会误命中。实测两个反例：
            #   「我叫不出来」 → 命中「我叫」，但这在说「说不出话」
            #   「我叫什么」   → 这是在**问**名字，不是自我介绍
            if kw.startswith(("我叫", "叫我", "我姓", "我名字", "我的名字")):
                if not _looks_like_identity(text, kw):
                    continue
            matched.append(kw)

    return {
        "method": "keyword",
        "keywords_configured": list(keywords or []),
        "matched": matched,
        "hit": bool(matched),
        "decided_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# 跟在身份词后面就不算「自我介绍」的字（否定 / 疑问 / 无法）
_IDENTITY_BLOCKERS = ("不", "没", "出", "错", "啥", "什", "谁", "怎", "为", "那", "哪")


def _looks_like_identity(text: str, kw: str) -> bool:
    """判断身份类关键词的出现是否真的像「自我介绍」。

    判据：紧跟其后的第一个有效字符，不能是否定/疑问词。行为完全确定、可复现。

    这是「宁可漏记」原则的一次具体应用 —— 漏掉一句自我介绍，
    好过把「我叫不出来」当成身份信息存进原始事件层。
    """
    idx = text.find(kw)
    if idx < 0:
        return False
    tail = text[idx + len(kw):].lstrip(" ：:，,。.是的就是")
    if not tail:
        return False
    return tail[0] not in _IDENTITY_BLOCKERS



def candidate_facts(text: str, max_items: int = 4) -> list[str]:
    """粗切「可能的事实句」，只保存切分结果，不做任何改写或摘要。

    Phase 1-3 不生成长期记忆，这里的作用是给后续 Memory 整理留下可对照的原文锚点。
    """
    parts = [p.strip() for p in _SPLIT.split(text)]
    facts = [p for p in parts if len(p) >= 4]
    if not facts and text.strip():
        facts = [text.strip()]
    return facts[:max_items]
