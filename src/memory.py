"""长期记忆（Phase 5）。

职责：
    记忆条目的数据结构 → 版本快照落盘（只增不改）→ 关键词检索

三条硬约束（来自《project-spec》与 docs 的 P0 风险清单）：

1. **版本永不覆盖**：v001.json 写好后，v002 是一个新文件，绝不修改 v001。
   目录里同时存在 v1/v2/v3 完整快照，可独立打开对比。
2. **条目必须能指回原始事实**：每条记忆带 source_event_ids，指向
   memory/raw/events/ 里的原始事件。「原始事实」与「加工品」物理分开，永不互相覆盖。
3. **检索不用 embedding、不用向量库**：关键词 + 标签匹配，行为完全确定、可复现。
   embedding 会引入一个与「被测对象」无关的语义相似度变量，让
   「AI 记不清」和「检索没召回」混在一起，无法归因。

刻意不做：向量检索、重排序、自动总结一切、自动生成几千条记忆。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict

from paths import VERSIONS_DIR, ensure_dirs

# 证据等级。推断不得伪装成事实。
VALID_TYPES = ("FACT", "INFERENCE", "UNKNOWN")

# ----------------------------------------------------------------------------
# Memory V1：分类 / 分层 / 衰减
# ----------------------------------------------------------------------------

# 记忆分类。**允许「未分类」** —— 拿不准就归这里，比硬塞一个错类别好。
UNCLASSIFIED = "未分类"
CATEGORIES = (
    "个人事实", "偏好", "共同经历", "项目状态", "关系经历", "计划",
    # Phase 12 P1 新增：具体生活事项（那盆茉莉、比赛、日常吃睡买）。
    # 必须同时加进 CATEGORIES —— 否则 classify() 返回的值会被
    # MemoryItem.__post_init__ 判为非法并退回「未分类」，白改一场。
    "生活事项",
    UNCLASSIFIED,
)

# 记忆分层。复用现有存储（不新建表、不迁移）：
#   core       常驻优先，检索时优先给（身份/长期偏好/重要关系）
#   experience 默认层，多数经历与事实
#   archive    归档层，被取代或长期未用；**默认不参与检索**，必要时才查
TIER_CORE = "core"
TIER_EXPERIENCE = "experience"
TIER_ARCHIVE = "archive"
TIERS = (TIER_CORE, TIER_EXPERIENCE, TIER_ARCHIVE)

# 衰减参数。刻意温和 —— 目标是「降权」而不是「删除」。
DECAY_HALF_LIFE_DAYS = 120.0     # 半年衰减一半，不激进
DECAY_FLOOR = 0.35               # 时间最多把权重压到 35%，永远不归零
CORE_MAX_ITEMS = 12              # 核心层上限，防止它无限膨胀
CORE_MIN_IMPORTANCE = 0.72       # 升入核心层的最低重要性 —— 否则核心层会被平庸条目填满
ARCHIVE_IMPORTANCE_BELOW = 0.50  # 低于此重要性且从未被召回 → 归档（只筛真噪声）

# 分类关键词。确定性规则（不用模型判定），与 relationship 同一思路。
#
# **顺序有意义**：先匹到先返回。项目状态 / 偏好这类**更具体**的类别放前面。
#
# ⚠️ 实测教训：初版只按**用户原话**写规则（"我喜欢…"），但记忆条目是
# 模型**用第三人称改写**的（"用户自称叫（网名）"、"用户一般晚上才有精神"）。
# 结果 35 条里 22 条落到「未分类」。所以规则必须同时覆盖两种语态：
#   · 第一人称（用户原话）  → "我喜欢"
#   · 第三人称（记忆条目）  → "用户喜欢"、"用户自称"
#
# ⚠️ 「共同经历」的关键词**不在这里硬编码** —— 见下方 `_shared_experience_kws()`：
# 它直接复用 relationship 的 `shared_experience.keywords`，
# 保证「同一句话在记忆侧和关系侧得到一致的语境判定」（不新增依赖、不新增调用）。
_CATEGORY_RULES = (
    ("个人事实", (
        # 第一人称
        "我叫", "我的名字", "我姓", "我是那种", "我读", "我学", "专业是", "岁", "住在", "老家",
        # 第三人称（记忆条目）
        "用户自称", "用户叫", "用户的", "用户否认", "用户擅长", "用户在意", "用户说自己",
        "用户有时", "用户感觉", "用户觉得", "用户表示自己", "不是本名", "网名", "军训",
        # 具体的人/物/持续状况（实测：茉莉相关 8 条全靠这条归类）
        "养了", "买了一盆", "养的", "相处的", "相处还", "更正：", "掉了几个",
    )),
    ("偏好", (
        "喜欢", "不喜欢", "更喜欢", "偏好", "习惯", "一般会", "爱喝", "爱吃", "讨厌", "宁愿",
        # 第三人称的习惯描述
        "一般是", "才有精神", "喝得最多", "很少", "经常", "每天都",
        # Phase 12 P1 补：阅读/看法类倾向（实测「读《毛选》当方法论看」「反复看几遍」
        # 两条一直落未分类）
        "读《", "看书", "读书", "反复看", "当方法论", "爱看",
    )),
    ("计划", ("打算", "准备要", "计划", "接下来", "明天", "下周", "要去做", "想去")),
    ("项目状态", (
        "项目", "共生", "Companion", "bug", "测试", "流程", "阶段",
        "版本", "部署", "跑通", "模型", "代码", "接口",
        # Phase 12 P1 补：用户在讨论自己这套系统（实测「现在有记忆系统人格系统和关系系统」
        # 「在考虑要不要再加一个情感系统」落未分类）
        "记忆系统", "人格系统", "关系系统", "情感系统", "验证两件事",
    )),
    ("关系经历", (
        "谢谢你", "跟你聊", "和你聊", "陪我", "聊得挺", "你说得对", "记得我", "你懂我",
        # 第三人称的关系表述
        "跟助手", "聊天挺舒服", "被记住", "助手是否记得",
        # Phase 12 P1 补：用户对这段关系/助手的评价（实测「人机感太重」
        # 「助手的人格系统应该重新调一下」落未分类）
        "人机感", "对助手", "助手的人格",
    )),
    # Phase 12 P1 新增：**具体生活事项**。
    #
    # 为什么单开一类：实测 v008 的 22 条未分类里，12 条是「那盆茉莉…」
    # 「比赛没进省赛」「中午买了个拿铁」这类**关于具体事物与日常事件**的记忆。
    # 它们既不是自我描述、也不是偏好或项目，前六类都装不下；硬塞进「个人事实」
    # 会让那一类失去意义。所以给它们一个明确的归处，而不是继续留未分类。
    ("生活事项", (
        "茉莉", "花", "养的", "植物", "宠物", "比赛", "省赛", "考试",
        "买", "吃", "喝", "睡", "休息", "代金券", "拿铁",
    )),
    ("共同经历", ()),   # ← 关键词在运行时从 relationship 规则取，见 classify()
)

# 核心层关键词：身份、长期偏好、重要关系 —— 命中即升为核心
_CORE_MARKERS = ("我叫", "我的名字", "网名", "本名", "我的名字是")

# 「共同经历」类别的兜底关键词。仅当拿不到 relationship 规则时使用
# （例如单元测试直接调 classify(text, rel_rules=None)）。
_SHARED_FALLBACK = ("我们一起", "一起", "接着搓", "帮我", "找bug", "搞定", "弄好", "做完了")


def _shared_experience_kws(rel_rules: dict | None) -> tuple:
    """统一口径：直接复用 relationship 的 `shared_experience.keywords`。

    ## 为什么这么做

    用户要求「统一 Memory 分类与 Relationship 事件识别使用的类别定义和语境规则，
    优先复用现有规则」。原先两边各写一份关键词表 —— 同一句「接着搓共生项目」
    在关系侧被识别为 shared_experience，在记忆侧却因为词表不同可能落到别的类别。

    这里**不复制**关系侧的词表，而是**引用**它：
      · 只有一份事实来源（settings.json 的 relationship.shared_experience）
      · 改关系词表时记忆分类**自动跟着变**，不会再漂移
      · 零新增依赖、零新增模型调用
    """
    if isinstance(rel_rules, dict):
        kws = ((rel_rules.get("shared_experience") or {}).get("keywords")) or []
        kws = tuple(k for k in kws if k)
        if kws:
            return kws
    return _SHARED_FALLBACK


def classify(text: str, rel_rules: dict | None = None) -> str:
    """确定性分类。拿不准返回「未分类」。

    与 relationship 同思路：**不用模型判定**，保证「为什么归到这一类」可复现。

    `rel_rules` 传 `settings.relationship` 时，「共同经历」用**与关系系统同一套**
    关键词判定（见 `_shared_experience_kws`）—— 这是两个模块口径统一的落点。
    """
    t = text or ""
    shared = _shared_experience_kws(rel_rules)
    for cat, kws in _CATEGORY_RULES:
        use = shared if cat == "共同经历" else kws
        if use and any(k in t for k in use):
            return cat
    return UNCLASSIFIED


def should_be_core(text: str, category: str = "") -> bool:
    """是否应升入核心层。只认**身份类**明确表述，避免核心层被塞满。"""
    t = text or ""
    return any(k in t for k in _CORE_MARKERS)


def _parse_ts(value: str | None):
    """解析 ISO 时间戳，失败返回 None（不抛错 —— 旧数据可能格式不一）。"""
    if not value:
        return None
    try:
        from datetime import datetime
        s = str(value).replace("Z", "+00:00")
        if s.endswith("+08:00") or "+" in s[10:]:
            return datetime.fromisoformat(s)
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def has_known_time(item: "MemoryItem") -> bool:
    """是否有**可靠的时间信息**（V1.1）。

    判据：`created_at` / `updated_at` / `last_recalled_at` 任一能解析成真实时间。
    `first_seen_version` / `last_modified_version` 是版本号（如 "v004"），**不算**。
    """
    return any(_parse_ts(x) is not None for x in
               (item.created_at, item.updated_at, item.last_recalled_at))


def decay_factor(item: "MemoryItem", now=None) -> float:
    """时间衰减系数 ∈ [DECAY_FLOOR, 1.0]。

    ## 设计原则（用户明确要求）

    **不允许仅因时间久就直接删除重要记忆。** 所以下限是 0.35 ——
    再久也只是**降权**，条目仍在、仍可被强相关查询召回。

    ## V1.1：未知时间 = **完全不衰减**（1.0），不是"轻微降权"

    用户要求：「历史条目没有可靠时间时保留未知，不伪造时间，
    **不让未知时间自动导致降权或归档**」。

    所以时间未知时直接返回 1.0。之前用的中性 0.75 仍隐含"没证据就假定它旧"，
    与要求冲突 —— 我们不知道它多老，就不该猜。

    salience（显式重要性）对抗衰减：salience=1.0 时完全不衰减。
    """
    import math
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)

    ref = None
    for cand in (item.updated_at, item.created_at, item.last_recalled_at):
        ref = _parse_ts(cand)
        if ref is not None:
            break

    if ref is None:
        raw = 1.0          # 未知时间 → 不衰减（不猜、不降权）
    else:
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - ref).total_seconds() / 86400.0)
        raw = math.exp(-age_days * math.log(2) / DECAY_HALF_LIFE_DAYS)

    floored = DECAY_FLOOR + (1.0 - DECAY_FLOOR) * raw

    # 显式重要性拉回：salience 越高越接近 1.0
    s = max(0.0, min(1.0, float(item.salience or 0.0)))
    return floored + (1.0 - floored) * s


def importance(item: "MemoryItem", now=None) -> float:
    """综合重要性 ∈ [0, 1] —— 决定检索优先级。

    组成（借鉴 Hindsight 的「重要性/时效/召回价值」加权）：
      · 稳定性：confidence（模型自评的把握）
      · 时效性：decay_factor（越久越轻，但有下限）
      · 重复程度：recall_count（被反复召回说明有用）
      · 显式重要性：salience
    层级直接加成：core 更高，archive 更低。
    """
    conf = 0.6 if item.confidence is None else float(item.confidence)
    base = 0.5 * conf + 0.5 * decay_factor(item, now=now)

    # 召回价值：被用过最多加到 +0.15（对数，避免刷次数）
    import math
    if item.recall_count:
        base += min(0.15, 0.05 * math.log1p(item.recall_count))

    base += 0.15 * max(0.0, min(1.0, float(item.salience or 0.0)))

    if item.tier == TIER_CORE:
        base += 0.20
    elif item.tier == TIER_ARCHIVE:
        base -= 0.25

    return max(0.0, min(1.0, base))


def promote_tiers(items: list["MemoryItem"], *, max_core: int = CORE_MAX_ITEMS) -> list["MemoryItem"]:
    """按重要性把条目分配到 core / experience / archive。**就地修改并返回**。

    规则（刻意保守，先降权不删除）：
      · 身份类表述 → core（上限见下）
      · 被 superseded_by 标记 → archive，并记 archived_reason
      · 其余重要性 ≥ CORE_MIN_IMPORTANCE 才升 core
      · 极低重要性 + 从未被召回 → archive

    ## 核心层上限是**双重**的

    实测教训：初版只用一个固定上限（12），结果 22 条里 12 条进了核心 ——
    核心层本该是"少数最关键的记忆"，占一半就失去意义了。
    所以现在取 `min(max_core, 总数//3)`：核心层**永远是少数派**。
    """
    for it in items:
        if it.superseded_by:
            it.tier = TIER_ARCHIVE
            it.archived_reason = it.archived_reason or "已被新记忆取代"

    effective_cap = max(1, min(max_core, len(items) // 3 or 1))

    # 身份类强制核心
    forced = [it for it in items if should_be_core(it.text, it.category)]
    forced.sort(key=lambda x: -importance(x))
    for it in forced[:effective_cap]:
        if it.tier != TIER_ARCHIVE:
            it.tier = TIER_CORE

    # 其余**高重要性**条目补充核心名额
    core_now = [it for it in items if it.tier == TIER_CORE]
    room = max(0, effective_cap - len(core_now))
    if room:
        rest = [it for it in items if it.tier not in (TIER_CORE, TIER_ARCHIVE)]
        rest.sort(key=lambda x: -importance(x))
        for it in rest:
            if room <= 0:
                break
            if importance(it) < CORE_MIN_IMPORTANCE:
                break
            it.tier = TIER_CORE
            room -= 1

    # 低重要性 + 从未被召回 → 归档（不删）
    #
    # ## V1.1：**时间未知的条目不参与自动归档**
    #
    # 用户要求「不让未知时间自动导致降权或归档」。
    # 所以 `has_known_time(it)` 为假时直接跳过 —— 我们不知道它多老、
    # 也不知道它是否只是"很久没被问到"，凭这两点归档是不成立的。
    # 明确的取代（superseded_by）仍会归档，那是有证据的。
    #
    # 阈值 0.50 的校准依据（用实际公式反推）：
    #   · conf=0.1 + **已知**很旧 → 0.5*0.1 + 0.5*0.35 = 0.175 → 归档 ✅
    #   · conf=0.9 + 已知很旧      → 0.625               → 保留 ✅
    #     ↑ **高置信的旧记忆不因久远被归档** —— 用户明确要求
    for it in items:
        if it.tier == TIER_ARCHIVE:
            continue
        if not has_known_time(it):
            continue          # 时间未知 → 不判、不归档
        if importance(it) < ARCHIVE_IMPORTANCE_BELOW and not it.recall_count \
                and it.tier != TIER_CORE:
            it.tier = TIER_ARCHIVE
            it.archived_reason = it.archived_reason or "长期未使用且重要性低"

    return items

_VERSION_RE = re.compile(r"^v(\d{3,})$")

# 标点与符号。用于生成连续中文串（见 tokenize 的坑 1）。
_PUNCT = re.compile(
    r"[\s，。！？、；：,.!?;:()（）「」『』【】《》\-—_/\\'\"]+"
)


class MemoryError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------

@dataclass
class MemoryItem:
    """一条长期记忆。

    字段沿用 docs 第 3.2 节的模型（借 Mem0 结构 + 任务书要求的 type 三分标记）。
    """

    id: str
    type: str
    text: str
    source_event_ids: list[str] = field(default_factory=list)
    confidence: float | None = None
    first_seen_version: str | None = None
    last_modified_version: str | None = None

    # ---- Memory V1 生命周期字段（全部可选，旧数据反序列化不受影响）----
    #
    # category 记忆分类 / tier 记忆分层 / salience 显式重要性（对抗衰减）
    # recall_count + last_recalled_at 召回价值 / superseded_by + archived_reason 留痕
    #
    # **为什么全部设默认值**：v001-v004 的 JSON 里没有这些字段，
    # 默认值让旧版本能**原样读回**，不需要任何数据迁移。
    category: str = UNCLASSIFIED
    tier: str = TIER_EXPERIENCE
    salience: float = 0.0
    last_recalled_at: str | None = None
    recall_count: int = 0
    superseded_by: str | None = None
    archived_reason: str | None = None
    # 真实时间戳（ISO 8601 本地时间）。V1.1 新增：此前只有版本号（"v004"），
    # 无法判断真实年龄，导致衰减机制形同虚设。
    # **历史条目保持 None —— 不伪造时间**。
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        self.type = (self.type or "UNKNOWN").upper()
        if self.type not in VALID_TYPES:
            # 不静默改写模型给的类型，降级为 UNKNOWN 并保留原意无从考证 —— 记 UNKNOWN 更诚实
            self.type = "UNKNOWN"
        if self.confidence is not None:
            try:
                self.confidence = max(0.0, min(1.0, float(self.confidence)))
            except (TypeError, ValueError):
                self.confidence = None
        self.source_event_ids = [str(x) for x in (self.source_event_ids or []) if x]

        if self.category not in CATEGORIES:
            self.category = UNCLASSIFIED
        if self.tier not in TIERS:
            self.tier = TIER_EXPERIENCE
        try:
            self.salience = max(0.0, min(1.0, float(self.salience or 0.0)))
        except (TypeError, ValueError):
            self.salience = 0.0
        try:
            self.recall_count = max(0, int(self.recall_count or 0))
        except (TypeError, ValueError):
            self.recall_count = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or "UNKNOWN"),
            text=str(data.get("text") or ""),
            source_event_ids=list(data.get("source_event_ids") or []),
            confidence=data.get("confidence"),
            first_seen_version=data.get("first_seen_version"),
            last_modified_version=data.get("last_modified_version"),
            category=data.get("category") or UNCLASSIFIED,
            tier=data.get("tier") or TIER_EXPERIENCE,
            salience=data.get("salience") or 0.0,
            last_recalled_at=data.get("last_recalled_at"),
            recall_count=data.get("recall_count") or 0,
            superseded_by=data.get("superseded_by"),
            archived_reason=data.get("archived_reason"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class MemoryVersion:
    version: str
    created_at: str
    model: str
    items: list[MemoryItem]
    source_range: dict = field(default_factory=dict)
    previous_version: str | None = None
    prompt_sha256: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "model": self.model,
            "source_range": self.source_range,
            "previous_version": self.previous_version,
            "prompt_sha256": self.prompt_sha256,
            "note": self.note,
            "items": [it.to_dict() for it in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryVersion":
        return cls(
            version=str(data.get("version") or ""),
            created_at=str(data.get("created_at") or ""),
            model=str(data.get("model") or ""),
            items=[MemoryItem.from_dict(x) for x in (data.get("items") or [])],
            source_range=dict(data.get("source_range") or {}),
            previous_version=data.get("previous_version"),
            prompt_sha256=data.get("prompt_sha256"),
            note=str(data.get("note") or ""),
        )


# ----------------------------------------------------------------------------
# 版本号与落盘（只追加）
# ----------------------------------------------------------------------------

def existing_versions() -> list[str]:
    ensure_dirs()
    out = []
    for path in VERSIONS_DIR.glob("v*.json"):
        if _VERSION_RE.match(path.stem):
            out.append(path.stem)
    return sorted(out)


def next_version_name() -> str:
    """下一个版本号。绝不复用已存在的版本号，也绝不覆盖已有文件。"""
    versions = existing_versions()
    if not versions:
        return "v001"
    highest = max(int(_VERSION_RE.match(v).group(1)) for v in versions)
    return f"v{highest + 1:03d}"


def version_path(version: str):
    return VERSIONS_DIR / f"{version}.json"


def save_version(mv: MemoryVersion) -> dict:
    """把一个记忆版本写成新文件。**已存在则抛错，绝不覆盖。**

    返回 {file, sha256}。sha256 覆盖文件正文（不含末尾换行），
    与原始事件的校验口径保持一致（见 storage.create_event）。
    """
    ensure_dirs()
    path = version_path(mv.version)
    if path.exists():
        raise MemoryError(
            f"记忆版本文件已存在，拒绝覆盖：{path.name}。"
            "版本快照是实验证据，不允许改写。"
        )

    body = json.dumps(mv.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    path.write_text(body + "\n", encoding="utf-8")
    (VERSIONS_DIR / f"{mv.version}.sha256").write_text(
        f"{digest}  {mv.version}.json\n", encoding="utf-8"
    )
    return {"file": f"memory/versions/{mv.version}.json", "sha256": digest}


def load_version(version: str) -> MemoryVersion:
    path = version_path(version)
    if not path.exists():
        raise MemoryError(f"记忆版本不存在：{path.name}")
    return MemoryVersion.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_latest() -> MemoryVersion | None:
    versions = existing_versions()
    if not versions:
        return None
    return load_version(versions[-1])


def verify_versions() -> dict:
    """校验每个版本文件与其 .sha256 是否一致，并检查是否有未登记的文件。

    只读操作。与 verify.py 对原始事件的口径一致。
    """
    report = {"checked": 0, "ok": True, "errors": [], "missing_sidecar": []}
    for version in existing_versions():
        report["checked"] += 1
        path = version_path(version)
        sidecar = VERSIONS_DIR / f"{version}.sha256"
        body = path.read_text(encoding="utf-8").rstrip("\n")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if not sidecar.exists():
            report["missing_sidecar"].append(version)
            report["ok"] = False
            continue
        recorded = sidecar.read_text(encoding="utf-8").split()[0]
        if recorded != digest:
            report["ok"] = False
            report["errors"].append(f"{version}：文件内容与 .sha256 不一致")
    return report


# ----------------------------------------------------------------------------
# 检索（关键词 + 标签，确定性）
# ----------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """极简切词：整个连续文本上取「单字 + 2-gram」，外加 ASCII 整词。

    不做分词、不做词干化、不做同义词 —— 目标是可复现，不是高召回。

    ## 两个踩过的坑（都不要重犯）

    **坑 1：按标点切成 chunk 再取 n-gram。** 实测「上夜班」切出 `上夜/夜班`，
    而条目「用户曾在晚上工作过一年多」切出 `晚上/上工`，两者一个 2-gram 都不共享 ——
    即使措辞几乎相同，相关查询也会 0 命中。所以必须在**去掉标点后的连续文本**上
    取 n-gram，让跨标点的组合（如「…一年多，白天…」→ `多白`）也能参与匹配。

    **坑 2：只收单字。** 误召回会爆炸：「今天天气怎么样」与
    「用户曾在晚上工作过一年多，白天睡不好」共用「天」「上」「过」等常用字。
    所以单字必须收（见 `_score`），但只能算很低的权重。

    切词结果刻意**不去重、保序**，便于事后人工复核。
    """
    raw = text or ""
    out: list[str] = []

    # ASCII 整词（英文/数字）单独处理，避免被逐字符切碎
    for match in re.findall(r"[A-Za-z0-9_]+", raw):
        out.append(match.lower())

    # 去掉所有标点与空白，得到连续中文串，在其上取单字 + 2-gram
    cleaned = _PUNCT.sub("", raw)
    cleaned = "".join(cleaned.split())
    if cleaned:
        out.extend(cleaned.lower())
        if len(cleaned) >= 2:
            low = cleaned.lower()
            out.extend(low[i : i + 2] for i in range(len(low) - 1))
    return out


# 身份探询：用户问「我叫什么」时，必须把**名字本身**召回来。
#
# ## 踩过的坑（实测）
#
# 记忆里明明写着「用户自称叫（网名）」，但问「我叫什么」时**召不回来** ——
# 召回的是「用户在意自己是否被记住，多次确认…记得他叫**什么**名字」这条推断。
#
# 根因：名字条与「我叫什么」**一个 2-gram 都不重叠**（`叫什`/`么名` vs 条目的
# `用户`/`自称`/`（网名）`…），所以它**连 strong 门槛都过不了**，压根进不了候选。
# 而那条推断因为含「叫什」「么名」反而命中。**问名字永远拿不到答案。**
#
# 修法：识别「在问身份」的查询，**跳过 strong 门槛**并给含身份信息的条目加权。
# 判据完全确定，不引入 embedding（符合project-spec §15 的约束）。
#
# 注意：门槛放宽只对**身份查询**生效，且只对**含身份词**的条目生效 ——
# 所以「今天天气怎么样」这类无关查询不会被误召回。
_IDENTITY_QUERY_MARKERS = (
    "我叫什么", "我叫啥", "我的名字", "我名字", "记得我叫", "我叫什么名字",
    "我是谁", "你记得我", "还记得我吗",
)

# 条目里「揭示用户姓名」的模式。
#
# ## 为什么不能用粗判据（踩过两次坑）
#
# 一开始我用「条目含『叫』或『名字』」来判断身份条 —— 结果
# **「用户点咖啡一般是下午叫外卖」也被当成身份条**，在问名字时排到第 1 位。
# 「叫」这个字太常见了。
#
# 所以改成**姓名式模式**：`叫` 或 `名字是` 后面**紧跟至少 2 个非标点字符**
# （姓名通常 2–4 字），且不能是「什么/啥/外卖/出来」这类。
# 实测能正确区分「用户自称叫（网名）」与「下午叫外卖」。
_NAME_PATTERN = re.compile(
    r"(?:叫|名字是|名字叫)\s*([^\s，。！？、；：,!?;:]{2,4})"
)
_NOT_NAME = ("什么", "啥", "外卖", "出来", "起来", "不上", "不醒", "名字")

# 条目里出现这些，说明它在讲身份（放宽门槛用）
_IDENTITY_ITEM_MARKERS = ("名字", "姓名", "自称")

# 身份命中的加权。
#
# 给得高（大于任何纯 2-gram 重叠的分数），确保**揭示姓名的条目**在问身份时必定居首。
# 实测：不给足加权时，「用户在意自己是否被记住…记得他叫什么名字」这条推断
# 靠 `叫什`/`么名` 等 2-gram 重叠就拿到 13.30 分，压过名字条的 13.15 ——
# **问名字却答不出名字**。
_IDENTITY_BOOST = 30.0


# ----------------------------------------------------------------------------
# Phase 12 P2：查询侧的两个低成本改进（停用词 + 同义扩展）
# ----------------------------------------------------------------------------
#
# 实测基准（v008 / 66 条 / 8 个真实问法）：期望关键词命中率 59%。
# 三个明显缺口：
#
#   ① 「我在读什么书」的 top1 是「用户在意自己是否被**记住**…叫什么**名字**」
#      —— 因为疑问词「什么」本身成了 2-gram 命中项，把无关条目顶上来。
#   ② 「我作息怎么样」只召回「作息变了」一条，漏掉「睡得早」「熬夜」
#      —— 措辞不同就零重叠。
#   ③ 「项目进展如何」漏掉「在找记忆模型的 bug」—— 没有相关词扩展。
#
# 两个改法都在**查询侧**，不动 `_score` 的判定规则：
#   停用词：疑问词/泛功能词不参与匹配（它们既无信息量又会造成假阳性）
#   同义表：把查询词扩成记忆里**实际会用到的说法**（多为 2 字以上，能形成强命中）
#
# 刻意保持小而可解释：只有一张手写表，事后能一眼看出「为什么这条被召回」。

# 不参与匹配的词。只收**疑问词与纯功能词** ——
# 收敛原则：宁可少收，也不要误伤内容词（那会直接掉召回）。
_QUERY_STOPWORDS = frozenset({
    # 单字：疑问/语气
    "怎", "么", "样", "什", "吗", "呢", "吧", "啊", "呀", "嘛",
    # 2-gram：疑问与泛问法
    "什么", "怎么", "么样", "怎样", "如何", "为什", "哪些", "哪个",
    "是否", "有没", "没有", "知道", "告诉", "说说", "觉得",
    # Phase 13 补：低信息量的口语 2-gram。
    # 实测误触发：「随便聊聊」与「差不多随便聊」重叠 → 把无信息闲聊
    # 当成了「相关话题」，进而触发一次关注点提示。这类词没有指向性，
    # 不能作为「相关」的证据。
    "随便", "便聊", "差不", "不多", "聊聊", "么样",
})

# 查询词 → 记忆里可能采用的同义/相关说法。
# 只放**2 字以上**的词（`_score` 要求 ≥2 字才算强命中，单字同义起不了作用）。
_SYNONYM_MAP = {
    "作息": ("熬夜", "早睡", "精神", "睡觉", "睡眠"),
    "睡":   ("熬夜", "早睡", "睡觉", "睡眠", "作息"),
    "熬夜": ("作息", "早睡", "睡觉"),
    "喝":   ("咖啡", "拿铁", "喝茶", "饮料", "茶"),
    "喝茶": ("咖啡", "拿铁", "茶"),
    "茶":   ("咖啡", "拿铁", "喝茶"),
    "咖啡": ("拿铁", "喝茶", "茶"),
    "拿铁": ("咖啡", "茶", "喝"),
    "项目": ("模型", "系统", "共生", "进展", "跑通"),
    "进展": ("项目", "进度", "状态", "跑通"),
    "书":   ("毛选", "看书", "读书"),
    "读":   ("毛选", "看书", "读书"),
    "毛选": ("读书", "看书", "方法论"),
    "茉莉": ("开花", "花苞", "花"),
    "花":   ("茉莉", "开花", "花苞"),
    "开花": ("茉莉", "花苞", "花"),
    "比赛": ("省赛", "队伍", "结果"),
    "省赛": ("比赛", "队伍"),
}


def expand_query_tokens(tokens: list[str]) -> list[str]:
    """查询侧扩词：去掉停用词，并补上同义/相关说法。

    只扩查询、**不改条目文本** —— 所以不会污染记忆，
    也不会改变「条目之间」的排序规则（相关性 + 重要性的算法原样保留）。
    """
    out: list[str] = []
    for t in tokens or []:
        if not t or t in _QUERY_STOPWORDS:
            continue
        out.append(t)
        for syn in _SYNONYM_MAP.get(t, ()):
            if syn not in _QUERY_STOPWORDS:
                out.append(syn)
    return out


# ----------------------------------------------------------------------------
# Phase 13：持续关注点（轻量）
# ----------------------------------------------------------------------------
#
# 目标：让「那盆茉莉」这类**真实存在、且还没了结**的事，在小满这边留个念想，
# 相关时自然问一句 —— 而不是每轮都提，也不是凭空关心。
#
# ## 三条硬门槛（缺一不提）
#
#   ① 类别属于「会持续存在的事」：生活事项 / 项目状态 / 计划
#      （偏好、个人事实这类是稳定属性，不构成"关注点"）
#   ② 文本里有**未决状态**标记：还没 / 争取 / 打算 / 在找 / 正在 …
#      （已经了结的事没有可追问的余地）
#   ③ 与本轮用户输入**有实际词重叠** —— 相关才提。
#      这一条同时承担了「不设固定追问频率」：话题不出现就不提。
#
# ## 为什么注入的是「问一句」而不是「陈述事实」（关键设计）
#
# 实测数据里存在**互相矛盾的条目**：
#     「用户现在在找记忆模型的 bug，正在瞎测试」
#     「用户说记忆模型的 bug『没有吧也可能顺手修了』」
# 前者的"未决"状态其实已经过时。如果让模型陈述它，就会说出过时的进展。
# 所以注入文案固定为**提问式**（「如果自然，可以问一句进展」）——
# 问一句在信息过时时依然成立，陈述则不成立。**这是防编造的结构性保证**。

_CONCERN_CATEGORIES = ("生活事项", "项目状态", "计划")

_CONCERN_OPEN_MARKERS = (
    "还没", "没有", "不确定", "争取", "打算", "准备", "在找", "正在",
    "希望", "会不会", "不清楚", "再看看", "等着", "回头",
)


def concern_candidates(mv: MemoryVersion | None,
                       rel_rules: dict | None = None) -> list[MemoryItem]:
    """筛出「可能值得持续关注」的条目（门槛 ①②）。与具体话题无关。"""
    if mv is None or not mv.items:
        return []
    out = []
    for it in mv.items:
        if getattr(it, "tier", "") == TIER_ARCHIVE:
            continue
        # 已被取代的条目不再关注
        if getattr(it, "superseded_by", None):
            continue
        cat = getattr(it, "category", "") or classify(it.text, rel_rules)
        if cat not in _CONCERN_CATEGORIES:
            continue
        if not any(m in (it.text or "") for m in _CONCERN_OPEN_MARKERS):
            continue
        out.append(it)
    return out


def pick_concerns(mv: MemoryVersion | None, user_text: str, *,
                  limit: int = 1, rel_rules: dict | None = None) -> list[dict]:
    """挑「与本轮相关」的持续关注点。返回 [{text, matched, category}]。

    门槛 ③ 用与检索同一套 tokenize（2-gram），**要求 ≥2 字重叠**，
    避免靠单字巧合触发。排序：重叠数降序 → id 升序（完全确定）。
    """
    text = (user_text or "").strip()
    if not text:
        return []
    q = {t for t in expand_query_tokens(tokenize(text)) if len(t) >= 2}
    if not q:
        return []

    scored = []
    for it in concern_candidates(mv, rel_rules):
        body = it.text or ""
        hits = sorted(t for t in q if t in body)
        if hits:
            scored.append((len(hits), it, hits))
    scored.sort(key=lambda p: (-p[0], p[1].id))

    return [
        {"text": it.text, "matched": hits, "category": getattr(it, "category", ""),
         "id": it.id}
        for _, it, hits in scored[: max(0, limit)]
    ]


def reveals_name(text: str) -> bool:
    """这条文本是否**揭示了一个姓名**（而不只是含「叫」字）。"""
    for m in _NAME_PATTERN.finditer(text or ""):
        candidate = m.group(1)
        if any(bad in candidate for bad in _NOT_NAME):
            continue
        return True
    return False


def _is_identity_query(query: str) -> bool:
    return any(m in (query or "") for m in _IDENTITY_QUERY_MARKERS)


def _identity_applicable(query: str, item: "MemoryItem") -> bool:
    """这条条目是否应该享受「身份查询」的放宽。

    两个条件都要满足：
      1. 查询确实在问身份
      2. 条目确实**揭示了一个姓名**（不是只含「叫」字，否则会误召回「叫外卖」）
    """
    if not _is_identity_query(query):
        return False
    return reveals_name(item.text)


def _score(item: MemoryItem, query_tokens: list[str], *, identity_query: bool = False) -> float:
    """按重叠长度加权打分。

    ## 为什么单字必须有，但只能算很低的权重

    实测教训：只用 2-gram 时，「上夜班」切出 `上夜/夜班`，而条目写的是
    「夜间时段工作」，切出 `夜间/时段` —— 两者永远碰不上，相关查询直接 0 命中。
    所以必须收单字。

    但单字一收，误召回立刻出现：「今天天气怎么样」与
    「用户曾在晚上工作过一年多，白天睡不好」共用了「天」「上」「过」三个常用字。
    如果单字也算分，无关查询就会被召回，而**误召回比漏召回更危险** ——
    它会把「检索到了但答错」伪装成「记忆漂移」。

    所以这里按重叠长度加权：
      ≥2 字的 n-gram 命中  → 计 1.0 分（真词重叠）
      单字命中            → 只计 0.15 分（弱证据）
    并且要求**至少有一次 ≥2 字的命中**才算相关，单字永远不能单独构成命中。

    另外：`identity_query=True` 时给含身份信息的条目加权（见上方说明）。

    这个阈值是启发式，不是真理。它的行为完全确定、可复现，事后可复核。
    """
    item_tokens = set(tokenize(item.text))
    if not item_tokens:
        return 0.0

    strong = 0      # ≥2 字的命中次数
    weak = 0        # 单字命中次数
    for token in set(query_tokens):
        if token not in item_tokens:
            continue
        if len(token) >= 2:
            strong += 1
        else:
            weak += 1

    if strong == 0:
        # 只有单字重叠 —— 通常不足以判定相关，直接不召回。
        # 例外：身份查询下的**揭示姓名的条目**（见 _identity_applicable 的说明），
        # 否则「我叫什么」永远召不回名字条（二者零 2-gram 重叠）。
        if not (identity_query and reveals_name(item.text)):
            return 0.0
        strong = 1   # 给一个基础分，让它能进入候选

    score = strong + weak * 0.15

    # 身份查询下，**揭示姓名的条目**直接加分到最高优先。
    # 只认 reveals_name（真带姓名），不认泛泛的「叫什么名字」推断。
    if identity_query and reveals_name(item.text):
        score += _IDENTITY_BOOST

    return score


def retrieve(
    mv: MemoryVersion | None,
    query: str,
    *,
    top_k: int = 5,
    layered: bool = True,
) -> list[MemoryItem]:
    """从给定记忆版本里取 top-k 条目。

    返回空列表是**合法且重要**的结果：它表示「这一轮没检索到任何记忆」，
    必须原样落盘，否则日后无法区分漂移与检索失败。

    ## 分层检索（Memory V1）

    `layered=True` 时按 **core → experience → (按需 archive)** 三档取：

      1. 先在 core + experience 里按「相关度 × 重要性」排序取 top_k
      2. 只有**前期结果不足**（命中数 < top_k）时，才去 archive 补齐
      3. archive 里的条目**相关性要求更严**（必须 ≥2 字强命中），
         避免无关旧记忆挤占上下文

    这解决的是用户提的实际问题：「旧记忆不该挤占上下文，
    但也不该因为时间久就被丢掉」。分层只是改变**取用顺序与门槛**，
    任何条目都仍在库里、仍可被强相关查询召回。

    `layered=False` 保持旧行为（全层平铺）—— 供对照与测试使用。
    """
    if mv is None or not mv.items:
        return []
    qt = tokenize(query)
    if not qt:
        return []

    # Phase 12 P2：查询侧扩词（去停用词 + 补同义说法）。
    # 原始 token 若**全部**是停用词（例如 query 就是「什么」），
    # 扩词后可能为空 → 退回原始 token，避免把「有查询但召不回」变成「查询无效」。
    qt_expanded = expand_query_tokens(qt)
    if not qt_expanded:
        qt_expanded = qt

    ident = _is_identity_query(query)

    def rank(items):
        scored = [(it, _score(it, qt_expanded, identity_query=ident)) for it in items]
        scored = [(it, s) for it, s in scored if s > 0]
        # 相关度为主，重要性为微调（乘 0.3 权重，避免重要性压过相关性）。
        # 分数相同按 id 升序 —— 完全确定，不依赖 dict/DB 顺序。
        scored.sort(key=lambda p: (-(p[1] + 0.3 * importance(p[0])), p[0].id))
        return scored

    if not layered:
        return [it for it, _ in rank(mv.items)[: max(0, top_k)]]

    primary = [it for it in mv.items if it.tier != TIER_ARCHIVE]
    archived = [it for it in mv.items if it.tier == TIER_ARCHIVE]

    out = [it for it, _ in rank(primary)[: max(0, top_k)]]

    if len(out) < top_k and archived:
        seen = {x.id for x in out}
        # 归档层门槛更严：必须强命中（≥2 字重叠），不给弱相关旧记忆机会
        for it, s in rank(archived):
            if len(out) >= top_k:
                break
            if s < 1.0 or it.id in seen:
                continue
            out.append(it)
            seen.add(it.id)

    return out[: max(0, top_k)]


def retrieve_scored(
    mv: MemoryVersion | None,
    query: str,
    *,
    top_k: int = 5,
) -> list[tuple[MemoryItem, float]]:
    """与 retrieve 相同，但把分数一并返回，便于核对「为什么排这个顺序」。"""
    if mv is None or not mv.items:
        return []
    qt = tokenize(query)
    if not qt:
        return []
    ident = _is_identity_query(query)
    scored = [(it, _score(it, qt, identity_query=ident)) for it in mv.items]
    scored = [(it, s) for it, s in scored if s > 0]
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored[: max(0, top_k)]


def render_for_prompt(items: list[MemoryItem]) -> str:
    """把记忆条目拼成注入上下文的一段文字。

    刻意标注 type 与来源事件：让模型知道哪些是用户原话、哪些是它自己先前的推断，
    避免 INFERENCE 在后续轮次里被当成 FACT 使用。
    """
    if not items:
        return ""
    lines = ["【你记得的关于用户的事】"]
    for it in items:
        src = "、".join(it.source_event_ids) if it.source_event_ids else "（无来源）"
        tag = it.type
        lines.append(f"- [{tag}] {it.text}（来源：{src}）")
    lines.append("以上是长期记忆。若与本次对话冲突，以用户当前说法为准。")
    return "\n".join(lines)


def render_full_version(mv: "MemoryVersion | None") -> str:
    """把**整个记忆版本**拼成注入文字，不做任何检索筛选。

    ## 为什么回忆探针必须用这个，而不是用 retrieve()

    实测发现的真实陷阱：标准的探针提问是「你还记得我之前说过的那件事吗？」——
    它**刻意不点名具体事件**（点名会污染结果，见 docs 的 P1 风险）。
    但关键词检索要靠词重叠才能召回，一个不点名的问题几乎必然 0 命中，
    于是探针什么都没注入，测出来的只是「模型对空上下文怎么答」，
    **完全测不到记忆** —— 实验会得出假结论。

    所以探针的语义是「把这一版记忆原样给它，看它还能不能说出来」，
    不做检索筛选。检索结果仍然单独落盘，供事后归因。
    """
    return render_for_prompt(mv.items if mv else [])
