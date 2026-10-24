"""用户模型（Phase 7）。

对应《project-spec》§7「User Model」与 §28「Phase 7 User Model」。

## 它和记忆层（Phase 5）的区别

这是最容易做重复的地方，所以先把边界划清：

| | Memory（Phase 5） | User Model（Phase 7） |
|---|---|---|
| 记什么 | 用户**说过的事实** | 小满**对用户形成的判断** |
| 例 | 「用户养了一盆茉莉」 | 「用户偏好把话说完整，不太喜欢被追问」 |
| 来源 | 用户原话 / 原始事件 | 长期互动里观察到的**行为与表达模式** |
| 能否错 | 事实层不该"错" | **允许错**，且必须能修正（§28） |

规范的原话：

> Companion 的 User Model 应该主要来自：长期互动 → 用户行为 → 用户表达
> → 共同经历 → Companion 自己的判断
> 而不是把用户所有个人资料一次性塞进 System Prompt。

## 三个硬要求（§28）

1. **必须允许「正确理解 / 错误理解 / 不确定 / 修正」** ——
   所以条目有 `status`（confirmed / inferred / uncertain），
   且**修正不会删除旧条目**，而是标记 `superseded_by` + `superseded_at`，保留 `revision_history`。
2. **条目必须带** `belief` / `confidence` / `source` / `last_updated`。
3. **允许存在一个很小的 User Seed 作为启动信息**，之后由 Companion 自己形成。

## 为什么修正必须留痕

如果用户说「我从来没说过我不喜欢甜的」，而我们直接把那条判断删掉，
就**无法回答**「小满当时为什么会那样认为」——
而「她眼中的用户」和「真实用户」之间的偏差，正是这个 Phase 要观察的东西。
所以旧条目永久保留，只标记被什么取代。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from paths import USER_MODEL_DIR, ensure_dirs
import storage

# 判断状态（§28 的 confirmed / inferred / uncertain）
STATUS_CONFIRMED = "confirmed"      # 用户明确说过的自我描述
STATUS_INFERRED = "inferred"        # 从互动里推断出来的
STATUS_UNCERTAIN = "uncertain"      # 观察到了但拿不准
VALID_STATUS = (STATUS_CONFIRMED, STATUS_INFERRED, STATUS_UNCERTAIN)

STATUS_LABELS = {
    STATUS_CONFIRMED: "用户明确说过",
    STATUS_INFERRED: "推断",
    STATUS_UNCERTAIN: "不确定",
}

# `uncertain` 条目要凑够多少条**独立陈述**，才够格提请人工复核。
#
# ## ⚠️ 这**不是**「够次数就自动升级」的判决规则
#
# 用户明确否掉过那个思路：「出现次数可以提高可信度，但不能直接等同于可信度」
# 以及「同一段话被重复引用，不算多份独立证据」。
#
# ## 所以它的真实用法是：**只标记，不升级**
#
# 够阈值时，把条目标记为「够格供人复核」并在 `usermodel` 输出里提示，
# **不改 status、不改 confidence** —— 让人来决定要不要升。
# 这样：
#   · 阈值常量**真的参与了一次判定**（不再是零引用）
#   · 但没有把「重复」变成「可信」
#
# 判据是**独立陈述数**（按 evidence 原文去重），不是"提到过几次"。
UNCERTAIN_EVENT_THRESHOLD = 2


class UserModelError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 判断信号的确定性判定
# ----------------------------------------------------------------------------

def _match(text: str, keywords: list[str]) -> list[str]:
    hit = []
    for kw in keywords or []:
        if not kw:
            continue
        if kw.isascii():
            if kw.lower() in text.lower():
                hit.append(kw)
        elif kw in text:
            hit.append(kw)
    return hit


# ----------------------------------------------------------------------------
# P0 修复：信息类型分类 + 兜底（原来不命中关键词就静默丢弃）
# ----------------------------------------------------------------------------

# `source_kind`：这句话**是怎么来的**（与「多可靠」正交）。
#
# 复用 `source` 字典（已有字段），**不新建表、不新建列**。
# 命名对齐我们既有的记忆层纪律与 endoxa 的 directness 概念：
#   stated   = 用户**亲口陈说**（含自我评价、自我描述、客观信息）
#   reported = 用户**转述**的二手信息（如「卖家说是去年的苗」）
#   inferred = 系统**推断**出来的
#   observed = 系统**直接观察到**的（工具结果、会话行为）
SOURCE_STATED = "stated"
SOURCE_REPORTED = "reported"
SOURCE_INFERRED = "inferred"
SOURCE_OBSERVED = "observed"
VALID_SOURCE_KINDS = (SOURCE_STATED, SOURCE_REPORTED, SOURCE_INFERRED, SOURCE_OBSERVED)

SOURCE_KIND_LABELS = {
    SOURCE_STATED: "用户亲口说的",
    SOURCE_REPORTED: "用户转述的二手信息",
    SOURCE_INFERRED: "系统推断",
    SOURCE_OBSERVED: "系统直接观察",
}

# `claim_kind`：「这句话是关于用户的**哪类**信息」。
#
# ## 为什么必须与 status 分开（P0 的核心）
#
# `status=confirmed` 的含义是「**用户明确说过**」（这一点为真且可追溯），
# 它**不是**「表达内容客观成立」。
# 两者分开，才不会出现「因为用户说过『我执行力很差』，
# 就把『用户执行力客观差』当成已确认事实」。
#
# ⚠️ 本阶段**不修改** `status` 的取值与语义，也不调整任何 confidence 阈值。
CLAIM_SELF_EVALUATION = "self_evaluation"           # 用户对自己能力/性格的评价（主观）
CLAIM_OBJECTIVE_ATTRIBUTE = "objective_attribute"   # 客观属性（专业/年龄/地点/持有物）
CLAIM_SELF_DESCRIPTION = "self_description"         # 描述自己是什么样的人
CLAIM_BEHAVIOR = "behavior"                         # 行为/偏好倾向
CLAIM_UNCERTAIN = "stated_unclassified"             # 兜底：确实是关于自己的陈述，但没细分

CLAIM_KIND_LABELS = {
    CLAIM_SELF_EVALUATION: "用户的自我评价（主观，不等于客观事实）",
    CLAIM_OBJECTIVE_ATTRIBUTE: "客观属性",
    CLAIM_SELF_DESCRIPTION: "自我描述",
    CLAIM_BEHAVIOR: "行为/偏好倾向",
    CLAIM_UNCERTAIN: "关于自己的陈述（未细分）",
}

# ----------------------------------------------------------------------------
# P2：`verifiable` —— 「该记录内容**可被核实**」的正交标记
# ----------------------------------------------------------------------------
#
# ## ⚠️ 语义边界（三条，必须与 status / claim_kind 分清）
#
# 1. `verifiable="yes"` 只表示**这条记录的内容可以被核实**
#    （例如「我的专业是物流」可以去核对学籍），
#    **绝不表示「它描述的客观命题已经被证实」**。
# 2. 用户说过 `verifiable="yes"` 的内容**仍然只是「用户说过」** ——
#    与 `status=confirmed` 的含义一致（confirmed = 用户说过，**不等于**客观成立）。
# 3. **只有结构化观察给出的值**才算「已核实」；
#    自然语言陈述**永远**只是「可核实」。
#
# ## 它为什么与 `claim_kind` 正交，而不是新增一个 claim_kind 取值
#
# `claim_kind` 表达「这条信息**是什么性质**」；「能否核实」是**另一维度**。
# 实测（真实配置，12 条陈述）：6 个 claim_kind 里 **4 个混类** ——
# `behavior` 混了「主观偏好/偏好/状态」，`objective_attribute` 混了可核实属性，
# `self_description` 混了「自我描述/身份（可核实）」，
# `stated_unclassified` 混了「发生过的事/经历/行为」。
# 若改成新增 claim_kind 取值，会与 3 个既有类别重叠，
# **要重写 P0 刚稳定的分类逻辑** —— 所以这里只加正交字段。
VERIFIABLE_YES = "yes"
VERIFIABLE_NO = "no"
VERIFIABLE_UNKNOWN = "unknown"
VALID_VERIFIABLE = (VERIFIABLE_YES, VERIFIABLE_NO, VERIFIABLE_UNKNOWN)

# 「高精度白名单」：**只收能明确核对的六类**。
#
# ## 为什么不追求全面覆盖（刻意的）
# 用户要求：「`_VERIFIABLE_ONLY` 先保留现有六类，作为**高精度**白名单，
# 不追求全面覆盖；未命中时**保持未确认，不扩张分类**。」
#
# ## ⚠️ 为什么不能直接判成 `claim_kind == objective_attribute`
# 实测（这是一个真坑）：`OBJECTIVE_ATTRIBUTE_KEYWORDS` 里的 `我在` 过宽，
# 会命中**临时状态**：
#     我在忙 / 我在想事情 / 我在等回复 / 我在吃饭 / 我在写代码 / 我在看书
# 这些全被判成 `objective_attribute`。把「我在忙」标成「内容可核实」是错的。
#
# **所以本表刻意不含 `我在`。** 代价：`我在物流专业` 会得 `unknown` 而不是 `yes`。
# 这是**保守方向**（少标一个标记），不会导致任何自动修改；
# 而**不改** P0 的 `objective_attribute` 分类逻辑（用户明确禁止）。
_VERIFIABLE_ONLY = (
    # 身份
    "我叫", "我的名字", "我名字", "我姓",
    # 专业 / 在读
    "我的专业", "我学的是", "我在读", "我读",
    # 年龄
    "我今年", "我岁",
    # 居住 / 籍贯
    "我住", "我来自", "我老家",
    # 学历
    "我是大", "我上大", "我毕业",
    # 职业
    "我是做", "我从事", "我的工作是",
)


def _verifiable_of(text: str, claim_kind: str) -> str:
    """给一条记录判 `verifiable`。**确定性、无阈值、不猜。**

    规则（先看硬否，再看高精度白名单，最后 unknown）：

        "no"       如果 claim_kind ∈ {self_evaluation, stated_unclassified}
                   （主观评价、以及兜底捞来的未细分陈述，都不属可核实内容）
        "yes"      命中 `_VERIFIABLE_ONLY` 的收紧句式
        "unknown"  其余（含 behavior）—— **拿不准保持未确认，不扩张分类**

    ⚠️ 注意 `self_description` 里既有「自我描述」（不可核实）
    也有「身份」（可核实，如「我叫（网名）」）—— 所以这一类**不能整类判**，
    必须落到句式白名单上。这也是为什么白名单与 `claim_kind` 判据并存。
    """
    if claim_kind in (CLAIM_SELF_EVALUATION, CLAIM_UNCERTAIN):
        return VERIFIABLE_NO
    t = text or ""
    if any(kw in t for kw in _VERIFIABLE_ONLY):
        return VERIFIABLE_YES
    return VERIFIABLE_UNKNOWN


# ----------------------------------------------------------------------------
# P2：主题键接口 —— **只留接口，当前恒返回 None**
# ----------------------------------------------------------------------------
#
# ## 为什么现在必须是 None
# 用户要求：「保留未来显式主题键的接口，但**暂不实现**词表匹配、
# 事件关键词映射或自动主题推断。」
#
# ## 为什么用户模型层现在拿不到可靠主题键
# 实测：唯一现成的结构化主题键是**记忆层**的 `source_event_ids`
# （6 条茉莉条目共享 `raw_event_001`，ID 相等即同主题、零歧义）。
# **但用户模型层没有这个字段**；而且事件层只覆盖 9.2% 的对话
# （`raw_event_triggers` 是刻意窄的事件白名单，不是主题分类体系）。
#
# ## 后果（这正是方案 A 的定义）
# 因为它恒返回 None：
#   · 版本取代    → 无键 → **不执行**，只记 unresolved
#   · 系统裁定    → 无键 → 判据①永不满足 → **不执行**
#   · 用户明确裁定 → 不依赖主题键（correction 关键词是**显式指向**）→ **可执行**
def _topic_key_of(item: dict) -> str | None:
    """显式主题键。

    **当前恒返回 `None`** —— 保留接口，不实现任何推断。
    未来来源（都不实现）：记忆层的 `source_event_ids` / 写入端显式声明。
    """
    return None

# 自我评价：明显的**自评性**说法。这类最容易与客观能力混淆，所以单独成类。
SELF_EVALUATION_KEYWORDS = [
    "执行力", "自制力", "意志力", "没毅力", "不自律",
    "我很差", "我太差", "我不行", "我做不好", "我做不了", "我做不到", "我干不了",
    "我害怕", "我怕", "我不敢", "我不擅长", "我学不会",
    "我总是", "我老是", "我从不", "我从来不会",
    "我容易", "我很难", "我配不上", "我没有天赋",
    "我不够", "我太懒", "我很懒", "我拖延",
]

# 客观属性：专业/年龄/地点/职业/持有物等**可核对**的信息。
OBJECTIVE_ATTRIBUTE_KEYWORDS = [
    "我在", "我是做", "我读", "我学的是", "我从事", "我的专业", "我在读",
    "我今年", "我岁", "我是18", "我是19", "我是20", "我是21", "我是22",
    "我住", "我来自", "我老家", "我是大", "我上大", "我毕业", "我的工作是",
]

# 兜底判据：确实是「关于自己的陈述」的句式，但没命中任何类。
#
# ## ⚠️ 这里我又踩了一次白名单的坑（本文件第四次）
#
# 原来写的是 `^我(?:是|很|比较|属于|挺|太|不太|有点|的确|确实|一向)` ——
# 结果 `我**可能**有点社恐` / `我**好像**不太擅长社交` 连兜底都进不去，
# 于是**犹疑路径根本没机会生效**（`uncertain` 依然是零产出）。
#
# **教训（第四次）：谓语是开放集，白名单必然漏。**
# 改成正向的**陈述谓语表** + 以「我」开头这个闭集前提。
# 「我」开头保证了它是第一人称自述；谓语表只用来排除疑问/意向/口语碎片。
_STATEMENT_PREDICATES = (
    "是", "很", "太", "比较", "挺", "不", "没", "有", "会", "能",
    "喜欢", "讨厌", "习惯", "经常", "总是", "有时", "老是", "从来",
    "可能", "好像", "大概", "似乎", "估计", "算", "觉得", "感觉",
    "在", "住", "来自", "今年", "姓", "叫", "属于", "从事", "做", "学",
    "擅长", "害怕", "怕", "敢", "想", "打算", "希望", "需要", "缺",
    "容易", "难", "懒", "拖延", "坚持", "受不了",
)
_FALLBACK_MIN_CHARS = 5
_FALLBACK_EXCLUDE = ("我是说", "我是想", "我是问", "我是开玩笑", "我是来", "我是看")

# 意向闸门：「我想去…」「我打算…」是**要做的事**，不是**我是什么样的人**。
#
# ## 为什么需要单独一道闸门
# 加宽兜底之后，`我想去散步` / `我打算明天去` 被收成了「关于用户的陈述」。
# 但**记录一个人的意向**和**记录他的属性**是两件事：
# 前者属于待办/计划，后者才是用户模型。混在一起会污染「她眼中的你」。
#
# 判据取「我 + 意向谓语 + 去/要/得…」这种明确句式；
# 「我想我可能是内向的」这种**后面跟的是判断**的，不算意向（它确实是在说自己）。
_INTENT_PREFIXES = ("想", "要", "打算", "计划", "准备", "决定", "考虑")
_INTENT_FOLLOWERS = ("去", "来", "做", "买", "吃", "玩", "学", "看", "睡", "休息")


def _is_intent(text: str) -> bool:
    """这是不是「我打算做某事」（属计划），而不是「我是什么样的人」（属用户模型）。"""
    t = (text or "").strip()
    if not t.startswith(_SELF_START_PREFIX):
        return False
    body = t[len(_SELF_START_PREFIX):]
    for p in _INTENT_PREFIXES:
        if not body.startswith(p):
            continue
        rest = body[len(p):]
        # 紧跟在「想/打算…」后面的头 3 个字里出现动作词 → 判为意向
        if any(f in rest[:3] for f in _INTENT_FOLLOWERS):
            return True
    return False


def _fallback_statement(text: str) -> str:
    """兜底抽取：把「不命中关键词、但确实是关于自己的陈述」捞出来。

    返回 claim 文本；返回空串表示这句不该被当成关于用户的判断。

    判据：`我` 开头（闭集，不会漏）**且**含陈述谓语（排除纯意向/口语）。
    """
    t = (text or "").strip()
    if len(t) < _FALLBACK_MIN_CHARS:
        return ""
    if any(x in t for x in _FALLBACK_EXCLUDE):
        return ""
    if _is_intent(t):
        return ""
    if not t.startswith(_SELF_START_PREFIX):
        return ""
    body = t[len(_SELF_START_PREFIX):]
    if not any(body.startswith(p) or p in body for p in _STATEMENT_PREDICATES):
        return ""
    for stop in ("。", "！", "？", "!", "?", "；", ";"):
        pos = t.find(stop)
        if pos > 0:
            t = t[:pos]
            break
    return t.strip()

# 指令 / 请求句闸门。
#
# ## 为什么必须有这道闸门（实测抓到的假阳性）
#
# 历史重放时发现：
#   `先帮我读取 README.md，再读取当前项目的配置文件，然后告诉我…`
# 被 `objective_attribute` 的 `我读` 命中，判成了「用户的客观属性」。
# **那是一条指令，不是关于用户的陈述** —— 「帮我读**取**」里的「我读」是巧合。
# 这类误命中在**技术类对话里必然反复出现**，所以不能靠删关键词解决。
#
# ## ⚠️ 我在这个闸门上连续踩了两次，记下来
#
# **第一次**：把「先 / 然后 / 接着 / **再** / 最后」当标记 →
#   `我执行力很差` 被挡掉（「真**的**」不含，但「真**再**」？不 —— 是别的）…
#   总之单字/泛词标记必然误命中。
# **第二次**：动作词出现在**词表里**（无条件 substring）→
#   `我执行力很差` 又被挡掉，因为含「**执行**」。
#
# **结论：动作词不能无条件 substring 匹配。**
# 所以现在分两类：
#   · 明确请求词（帮我 / 请你 / 麻烦你…）→ 出现即算指令（这些词不会出现在自述里）
#   · 动作词（读取/打开/运行/执行/写/改/查/跑）→ **只在句首附近才算**（正则锚定）
#     「我执行力很差」的动作词在句中 → 不算指令 ✅
#     「先帮我读取…」的「读」在句首区 → 算指令 ✅
_COMMAND_MARKERS = (
    "帮我", "帮忙", "请你", "麻烦你", "麻烦帮", "给我", "替我",
)
# 句首 8 个字内出现动作词 → 判为指令（例：「先读取配置文件」「然后告诉我结果」）
_COMMAND_START_RE = re.compile(r"^.{0,8}?(?:帮|读|看|写|跑|改|查|运行|执行|生成)")
# 以「我」开头的自述句 —— **无条件**视为自述，不看后面跟什么。
#
# ## ⚠️ 这是我在这个闸门上踩的第三次，前两次都是「白名单」的错
#
# 第一版：单字泛词标记（「再」）→ `我执行力很差` 被误杀。
# 第二版：动作词进词表无条件匹配（「执行」）→ 又被误杀。
# 第三版：白名单 `^我(?:是|很|不|…)` → 「我**执**行力很差」不在白名单里，**又被误杀**。
#
# **教训：白名单在这里必然漏 —— 自述的谓语是开放的，列不完。**
# 反过来「以我开头 = 自述」是**闭集**，不会漏。
# 误杀自述比漏判指令更糟：漏判只是多记一条噪音，误杀会丢掉用户的真实表达。
_SELF_START_PREFIX = "我"

# ----------------------------------------------------------------------------
# uncertain 的产出路径（把死枚举变成真的会被产生的状态）
# ----------------------------------------------------------------------------
#
# ## 先澄清语义（这一步不做，`uncertain` 会继续含糊）
#
# `status` 回答的是「**这条判断的证据状态**」，三个取值各管一件事：
#   · `confirmed` —— 用户**明确说过**这句话（**不等于**内容客观成立）
#   · `inferred`  —— 我们**从互动里推断**出来的
#   · `uncertain` —— **用户自己**对这句话就不确定，或者我们判定不了性质
#
# ## 核心原则
#
# > **我们给一条判断的定级，不能超过用户自己表达出来的确定程度。**
#
# 用户说「我可能有点社恐」时，他表达的是犹疑。我们若据此记一条
# 确定的"用户有社交恐惧"，就是把**用户的不确定**升级成了**系统的确定** ——
# 这正是 P0 在修的同一类错误（把"说过"当成"成立"）的一个变种。
_HEDGE_MARKERS = (
    "可能", "也许", "好像", "大概", "似乎", "估计", "说不定",
    "不确定", "不清楚", "说不好", "不太确定", "记不清",
    "算是", "勉强算", "有点算",
    "有时", "有时候",  # 条件性表达：不是稳定特征
)

# 「明确的自省」标记 —— 用户**在说"我不知道"**。
#
# ## 为什么必须单独有这一层（真实对话踩到的 bug）
#
# 实测（用户在真实对话里试出来的）：
#   `我有时候觉得自己害怕失败，但我不知道是不是这样。`
# 被判成了 **correction**（修正句），进而触发作废了一条**完全正确**的既有判断。
#
# 根因分两层：
#   ① 表层：`_HEDGE_MARKERS` 里没有「不知道是不是」，
#      所以「不**是**这样」这个否定残留下来，被 correction 关键词 `不是这样` 命中。
#   ② 深层：**我们把"否定"当成了"用户在修正"的证据**，
#      但这个否定可能只是**用户自己的不确定**。
#
# 所以修法不是「再补一个词」，而是**加一层语义闸门**：
# 命中这些标记 → 整句是**自省**，不是修正 → 走 `uncertain`。
_SELF_DOUBT_MARKERS = (
    "不知道是不是", "不知道算不算", "不知道对不对",
    "不知道这样算不算", "不确定是不是", "不确定算不算",
    "我也说不清", "说不清楚", "我也不确定",
)


def _is_self_doubt(text: str) -> bool:
    """用户在**说"我不知道"**吗（而不是在否定既有判断）。

    这一层优先于修正判定：自省里的否定不是修正。
    """
    return any(m in (text or "") for m in _SELF_DOUBT_MARKERS)


def _is_hedged(text: str) -> bool:
    """用户在这句话里**自己表达了不确定**吗。"""
    return any(m in text for m in _HEDGE_MARKERS)


def _strip_hedges(text: str) -> str:
    """剥掉犹疑词，用于**匹配修正词**（不改原文，只用于探测）。

    ⚠️ **必须长词优先 + 单趟替换。**

    实测踩到的坑：按 `_HEDGE_MARKERS` 的**声明顺序**逐个 `str.replace` 时，
    `有时候` 会先被 `有时` 命中、剥成 `候`，
    于是 `我有时候…` 变成 `我候…` —— **凭空造出一个错字**。
    这会给后续所有关键词匹配带来不可预期的偏差。

    改成按长度降序、一次扫过，消除顺序依赖。
    """
    t = text or ""
    # 长词优先：否则 `有时候` 会被 `有时` 提前吃掉。
    for m in sorted(_HEDGE_MARKERS, key=len, reverse=True):
        t = t.replace(m, "")
    return t


def _decide_status(text: str, base_status: str) -> str:
    """在基础状态之上，决定是否需要降级为 `uncertain`。

    ## 为什么是"降级"而不是"另立一档"
    基础状态由 `detect_signals` 按命中方式给出（说了/推断）。
    本函数只做一件事：**在用户自己不确定时，把它降到 `uncertain`** ——
    宁可标低，也不要让一条犹疑的自我描述看起来像既定事实。

    ## ⚠️ 不修改任何 confidence 阈值
    这里只改 `status`。`confidence` 的取值与赋值规则**原样不动** ——
    用户明确要求「暂不调整未经定义的 confidence 阈值」。
    """
    if not text or not base_status:
        return base_status
    if _is_hedged(text):
        return STATUS_UNCERTAIN
    return base_status


def _is_command(text: str) -> bool:
    """这是不是「在让我做事」而不是「在说自己」。

    判据刻意保守（宁可漏判指令、也不误杀自述）：
    只有**明确请求词**，或**句首**的步骤/动作词，才算指令。
    以「我」开头的自述句**永不**判为指令 —— 这条优先级最高。
    """
    t = (text or "").strip()
    if not t:
        return False
    # ① 明确请求词**优先**（帮我 / 请你 / 麻烦你…）
    #    这些词不会出现在真正的自述里，所以可以无条件判为指令。
    #    实测案例：`先帮我读取 README.md…` —— 「帮我」在这里，
    #    而句首是「先」，所以必须**先**查请求词。
    if any(m in t for m in _COMMAND_MARKERS):
        return True
    # ② 以「我」开头的自述句 → 不是指令（**闭集判据，不会漏**）
    #    这是三次误杀换来的：`我执行力很差` 必须能通过。
    if t.startswith(_SELF_START_PREFIX):
        return False
    # ③ 句首步骤/动作词（例：`先读取配置文件`、`然后告诉我结果`）
    return bool(_COMMAND_START_RE.match(t))


def _classify(text: str, matched_kind: str) -> str:
    """判定这句话属于哪类「关于用户的信息」。

    顺序有讲究：**先自评**（最需要与客观事实区分），再客观属性，
    再自我描述，最后行为。都判不出时给兜底类。
    """
    if matched_kind == "self_description":
        return CLAIM_SELF_DESCRIPTION
    if matched_kind == "behavior":
        return CLAIM_BEHAVIOR
    for kw in SELF_EVALUATION_KEYWORDS:
        if kw in text:
            return CLAIM_SELF_EVALUATION
    for kw in OBJECTIVE_ATTRIBUTE_KEYWORDS:
        if kw in text:
            return CLAIM_OBJECTIVE_ATTRIBUTE
    return CLAIM_UNCERTAIN


def _is_question(text: str) -> bool:
    """判断这是不是「在提问」而不是「在陈述」。

    ## 为什么必须有这道防线

    实测踩到的坑：用户问 **「我喜欢喝啥」**（在考它记不记得）时，
    因为含「我喜欢」被判成 `behavior` 并**存成一条关于用户的判断** ——
    于是用户模型里多了一条叫「我喜欢喝啥」的"事实"。

    同类：「我一般几点睡」也会中招。

    判据：句末疑问标点，或含疑问词。行为完全确定、可复现。
    """
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("?", "？")):
        return True
    # 疑问词出现在「我…」句式里，基本就是在问自己（而不是陈述自己）
    return any(w in t for w in ("啥", "什么", "几点", "哪个", "哪儿", "哪里", "多少", "怎么", "吗"))


def detect_signals(text: str, rules: dict) -> list[dict]:
    """从一句话里判定「关于用户自我的判断信号」。

    刻意用确定性规则，理由与记忆/关系一致：
    「小满为什么认为用户是这样的人」必须可复现、可审计。

    返回若干条 {type, claim, matched, status, source_kind, claim_kind}。

    ## P0 修复：两个新维度 + 兜底
    - `source_kind`：这句话是怎么来的（stated / reported / inferred / observed）
    - `claim_kind`：这句话是**关于用户的哪类信息**
      （self_evaluation / objective_attribute / self_description /
       behavior / stated_unclassified）
    - **兜底**：不命中任何关键词、但确实是关于自己的陈述 → 记为
      `stated_unclassified`，而不是像以前那样**静默丢弃**。
    """
    if not text:
        return []

    # ⓿ 先排除疑问句。
    #
    # 「我喜欢喝啥」「我一般几点睡」这类提问会被「我喜欢」「我一般」误命中，
    # 于是用户的**问题**被存成关于用户的**判断**。实测发生过。
    # 注意：修正句优先于这道排除 —— 「我没说过我不喜欢甜的」不是提问。
    #
    # ## 修正检测要先把犹疑词剥掉（实测踩到的坑）
    #
    # 用户说 **`我好像没说过我不太爱说话`** —— 修正词「我没说过」被「好像」隔开了，
    # 于是 `any(kw in text)` 命中不了，**整句落进了普通分支**，
    # 既没触发修正、又产生了一条新的 `uncertain` 判断。
    # 而它明明是在修正。
    #
    # 所以改成：先剥掉句子里的犹疑词，再做修正词匹配。
    # （这也顺带让「我可能不太喜欢」这类表达仍能被正确归类到 behavior）
    probe_text = _strip_hedges(text)
    corr_kws = (rules.get("correction") or {}).get("keywords") or []
    # 闸门：**自省里的否定不是修正**。
    # 实测 bug：`我有时候觉得自己害怕失败，但我不知道是不是这样。`
    # 因含「不是这样」被判成修正，作废了一条完全正确的既有判断。
    is_self_doubt = _is_self_doubt(text)
    is_corr = (not is_self_doubt) and any(kw in probe_text for kw in corr_kws if kw)

    # 说明：用户对**小满自己/聊天方式**的反馈（「人机感太重」「不知道聊啥」）
    # **不在这里处理** —— 那是关于「小满该怎么做」的，属于自我模型
    # （见 src/self_model.py）。用户模型只装「小满眼中的用户」，
    # 把对自己的反馈混进来会让两个模型的职责糊掉。
    if not is_corr and _is_question(text):
        return []

    found = []

    # ① 修正/纠正信号优先且**独占**。
    #
    # 踩过的坑：「我没说过我不喜欢甜的」同时命中了 correction（「我没说过」）
    # 和 behavior（「我不喜欢」—— 它只是前半句的一部分）。
    # 结果既记了一条修正，又反过来推断出一条新的「用户不喜欢甜的」，
    # 自相矛盾。所以命中即返回，不再往下判定。
    if is_corr:
        matched = [kw for kw in corr_kws if kw in probe_text]
        return [{
            "type": "correction",
            # 修正句保留**整句** —— 它是「指令」不是「判断」，
            # 原文完整才好追溯「用户到底否定的是什么」。
            "claim": text.strip(),
            "matched": matched,
            "status": STATUS_CONFIRMED,
            # 修正句是用户亲口说的，所以 source_kind=stated。
            # claim_kind 留空：它**不是**一条关于用户的判断，
            # 而是一条「作废既有判断」的指令（merge_claims 会置 is_correction）。
            "source_kind": SOURCE_STATED,
            "claim_kind": "",
        }]

    # ② 用户的自我描述 → confirmed（用户自己说的）
    for pattern in (rules.get("self_description") or {}).get("keywords") or []:
        if pattern in text:
            found.append({
                "type": "self_description",
                # 只取关键词**起头**的片段，不要把整句都当主张。
                # 实测踩到的坑：存成「好的了解我叫（网名）」「你好我叫（网名）」——
                # 前面的语气词与打招呼都被算进了「关于用户的判断」。
                "claim": _extract_claim(text, pattern),
                "matched": [pattern],
                "status": STATUS_CONFIRMED,
                "source_kind": SOURCE_STATED,
                "claim_kind": _classify(text, "self_description"),
            })
            break

    # ③ 行为/偏好表达 → inferred（可推断出倾向，但用户没直接定义自己）
    matched = _match(text, (rules.get("behavior") or {}).get("keywords") or [])
    if matched:
        found.append({
            "type": "behavior",
            "claim": _extract_claim(text, matched[0]),
            "matched": matched,
            "status": STATUS_INFERRED,
            "source_kind": SOURCE_STATED,
            "claim_kind": _classify(text, "behavior"),
        })

    # ④ P0 新增：自我评价 / 客观属性 —— 按**分类关键词**产出条目。
    #
    # 加这一步的理由：在它之前，以下句子**全部静默丢弃**（实测）——
    #   「我执行力很差」「我害怕失败」「我在物流专业」「我今年二十岁」
    # 用户的 observation 连进来的机会都没有，「该不该信」这个判断
    # 根本没发生过。**记下来比丢掉好：丢掉不可恢复，记错了可以修正。**
    #
    # 注意这一类**不靠 `rules` 里的关键词**（配置里没有这两组），
    # 而是用本模块的类级关键词 —— 这样「先分类、再决定怎么处理」，
    # 而不是「先看有没有词、没有就扔」。
    if not found:
        claim = _extract_claim(text, "")
        kind = _classify(text, "")
        hit_eval = [k for k in SELF_EVALUATION_KEYWORDS if k in text]
        hit_attr = [k for k in OBJECTIVE_ATTRIBUTE_KEYWORDS if k in text]
        # 指令/请求句闸门：它是「让我做事」，不是「在说自己」。
        # 实测假阳性：`先帮我读取 README.md…` 被 `我读` 命中。
        if (hit_eval or hit_attr) and not _is_command(text):
            found.append({
                "type": "stated",
                "claim": claim,
                "matched": hit_eval or hit_attr,
                # 保守：不知道用户是在陈述还是评价，标 inferred，
                # 不冒充「用户明确说过的」。宁可标低一档。
                "status": STATUS_INFERRED,
                "source_kind": SOURCE_STATED,
                "claim_kind": kind,
            })

    # ④ P0 最后一道兜底：不命中任何关键词，但**句式上确实是**关于自己的陈述。
    #
    # 只在明确的判定句式上兜底（见 _fallback_statement），排除疑问与修正
    # （它们在上面已被处理），避免把口语碎片收进来。
    if not found:
        claim = _fallback_statement(text)
        if claim:
            found.append({
                "type": "stated",
                "claim": claim,
                "matched": [],
                "status": STATUS_INFERRED,
                "source_kind": SOURCE_STATED,
                "claim_kind": _classify(text, ""),
            })

    # ⑥ P0 第二步：把「用户自己不确定」的陈述降级为 `uncertain`。
    #
    # 这一步让 `uncertain` 从**死枚举**变成真的会被产生的状态。
    # 它只改 `status`，**不动 confidence**（用户要求）。
    #
    # 修正句（correction）**不降级** —— 它是「作废既有判断」的指令，
    # 「我没说过我不太爱说话」用词的犹疑不代表这条否定本身不确定。
    for sig in found:
        if sig.get("type") == "correction":
            continue
        sig["status"] = _decide_status(text, sig["status"])

    return found


def _extract_claim(text: str, keyword: str) -> str:
    """从一句话里取出「关于用户」的那部分，去掉前缀的语气词与客套话。

    实测踩到的坑：`好的了解我叫（网名）` 被整句存成主张，于是用户模型里出现
    「好的了解我叫（网名）」这种条目 —— 前面的应答语被算进了「关于用户的判断」。

    判据：从关键词出现的位置起，截到第一个句末标点。行为完全确定。

    **空 keyword 是允许的**（P0 新增）：表示「没有关键词可依据」，
    此时从整句开头截到第一个句末标点 —— 与有关键词时走同一套截断逻辑，
    避免出现「有词时截断、没词时整句存」的不一致。
    """
    t = (text or "").strip()
    if keyword:
        idx = t.find(keyword)
        if idx >= 0:
            t = t[idx:]
    for stop in ("。", "！", "？", "!", "?", "；", ";"):
        pos = t.find(stop)
        if pos > 0:
            t = t[:pos]
            break
    return t.strip()


# ----------------------------------------------------------------------------
# 条目结构
# ----------------------------------------------------------------------------

def new_claim_id(belief: str, source_id: str) -> str:
    """按内容 + 首个来源确定性派生 ID。"""
    payload = json.dumps({"b": belief.strip(), "s": source_id}, ensure_ascii=False, sort_keys=True)
    return "um_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_claims(signals: list[dict], *, session_id: str, seq: int, turn_id: str,
                 source_text: str = "") -> list[dict]:
    """把信号转成用户模型条目。

    ## P0 新增字段（都塞进已有的 `source` 字典，不新建表）
    - `source_kind`：这句话是怎么来的（stated / reported / inferred / observed）
    - `claim_kind` ：这句话是关于用户的哪类信息
    - `rule`       ：命中的规则组名（`self_description` / `behavior` / `correction`）
                     或 `fallback`（兜底捞出来的）—— 便于事后追问「它凭什么这么说」
    - `evidence`   ：**触发这条判断的用户原话片段** —— 历史可追溯的关键
    """
    out = []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for sig in signals:
        source = f"turn:{turn_id}#seq{seq}"
        matched = sig.get("matched") or []
        out.append({
            "id": new_claim_id(sig["claim"], source),
            "belief": sig["claim"],
            "confidence": 0.9 if sig["status"] == STATUS_CONFIRMED else 0.5,
            "status": sig["status"],
            "source": {
                "kind": sig["type"],
                "matched": matched,
                "session_id": session_id,
                "seq": seq,
                "turn_id": turn_id,
                # ---- P0 新增 ----
                "source_kind": sig.get("source_kind") or SOURCE_STATED,
                "claim_kind": sig.get("claim_kind") or "",
                # 命中的规则组；matched 为空说明是兜底捞的
                "rule": "fallback" if not matched and sig["type"] == "stated" else sig["type"],
                # 触发这条判断的原话（截断保存，够追溯即可）
                "evidence": (source_text or "")[:200],
                # ---- P2 新增（正交标记：内容可否被核实；**不代表命题已证实**）----
                # 用**原话**（source_text）判，不用抽取片段 —— 与其他分类判据一致
                # （`_classify` 也用原话），避免"抽取把关键词截掉"的偏差。
                "verifiable": _verifiable_of(source_text or sig["claim"],
                                             sig.get("claim_kind") or ""),
            },
            "last_updated": now,
            "revision": 1,
            "superseded_by": None,
            "superseded_at": None,
            "revision_history": [],
        })
    return out


# ----------------------------------------------------------------------------
# 并入 + 修正（纯函数，可重放）
# ----------------------------------------------------------------------------

def merge_claims(
    existing: list[dict], incoming: list[dict], rules: dict | None = None
) -> tuple[list[dict], list[dict]]:
    """把新条目并入既有模型。

    返回 (全部条目, 本次发生的变更事件列表)。

    ## 修正规则（§28 要求允许「错误理解」与「修正」）

    当用户**否定**了一条既有判断时，我们**不删除**它，而是：

        superseded_by = 修正条目的 id
        superseded_at = 时间
        revision_history += [{at, from, to, reason}]

    这样「小满当时为什么那样认为」永远可追溯。
    """
    items = [dict(x) for x in existing]
    by_id = {x["id"]: x for x in items}
    changes: list[dict] = []

    # P2 / K6：本次调用内已登记过的 unresolved 目标标识。
    #
    # ## 作用域就是「这一次 merge_claims 调用」—— 这是一个**已知限制**
    # 条目本身**没有**承载 unresolved 状态的地方（实测：字段集里没有，
    # `status` 只有 confirmed/inferred），unresolved 只落在
    # `user_model_events` 表里。要做**跨轮**去重就得把"已知标识"传进来，
    # 那需要改 `chat.py` —— 而本轮边界明确「仅修改 user_model.py」，
    # 所以**不扩大范围**，如实记录为待办。
    #
    # 覆盖到的真实情形：一条消息里含**多次**并列修正（signals 一次进来），
    # 它们指向同一组候选 → 只留一条 unresolved。
    _active_unresolved: set[str] = set()

    for inc in incoming:
        if inc["id"] in by_id:
            # 同一句话重复出现，不重复记条目
            continue

        # 去重：同一条主张（同 status + 同文本）已经存在就不再新增。
        #
        # 实测踩到的坑：用户五次说「我叫（网名）」，因为原句措辞略有不同
        # （「好的了解我叫（网名）」「你好我叫（网名）」…），ID 各不相同，
        # 于是用户模型里**堆了 4 条一模一样的「我叫（网名）」**。
        # 抽取片段（_extract_claim）解决了大部分，这里再加一道保险。
        dup = next(
            (x for x in items
             if not x.get("superseded_by")
             and x.get("status") == inc.get("status")
             and (x.get("belief") or "").strip() == (inc.get("belief") or "").strip()),
            None,
        )
        if dup is not None and inc["source"]["kind"] != "correction":
            continue

        if inc["source"]["kind"] == "correction":
            # 让这条修正去否定既有的 inferred 条目中最相关的那条。
            # 这条条目本身是**指令**不是判断，标记 is_correction 以免被当成用户主张注入。
            inc["is_correction"] = True
            target = _find_correction_target(items, inc, rules)
            if target is not None:
                # 区分两种修正，因为它们的语义不同：
                #   · 目标含「喜欢/偏好」类词 → 这是**偏好更新**（咖啡 vs 茶可以并存，
                #     只是当前以新的为准），不是「用户说此前理解错了」
                #   · 否则 → **事实否定**（用户明确表示模型记岔了）
                # 两者都作废旧条目并留痕，但理由要写准，便于人工复核。
                is_pref = any(
                    w in target["belief"] for w in ("喜欢", "偏好", "习惯", "宁愿", "更喜欢")
                )
                reason = (
                    "用户更新了偏好，旧偏好被取代（注意：偏好可以并存，"
                    "此处按『以新说法为准』处理，如与原意不符请人工修正）"
                    if is_pref else
                    "用户明确表示此前的理解有误"
                )
                target["superseded_by"] = inc["id"]
                target["superseded_at"] = inc["last_updated"]
                # P2：修正的 change 从笼统的 "superseded" 细化为 "adjudicated"。
                #
                # 语义差别（这是 P2 的核心）：
                #   · "superseded"  = 版本取代：**那时为真，现在变了**（情况变了）
                #   · "adjudicated" = 裁定推翻：**从来不是对的**（用户说没这回事）
                # 用户明确修正属后者；并记 `adjudicated_by="user"`。
                #
                # ⚠️ 只有**用户明确裁定**（correction 关键词显式指向）才走这里。
                # 版本取代与系统裁定在方案 A 下都**不自动执行**（只记 unresolved）。
                target["revision_history"] = list(target.get("revision_history") or []) + [{
                    "at": inc["last_updated"],
                    "change": "adjudicated",
                    "adjudicated_by": "user",
                    "by": inc["id"],
                    "reason": reason,
                    "evidence": inc["belief"],
                }]
                changes.append({
                    "type": "revise",
                    "target_id": target["id"],
                    "target_belief": target["belief"],
                    "by_id": inc["id"],
                    "evidence": inc["belief"],
                    "reason": reason,
                    "change": "adjudicated",
                    "adjudicated_by": "user",
                })
            else:
                # ---- P2：目标定不了 → **不猜**，但**登记** ----
                #
                # 现状（P0 以前）：这时什么都不发生 → 矛盾**静默遗留**，
                # 谁也看不见「用户否定过，但我们没照做」。
                # 方案 A 的核心价值就在这里：**不改，但让它看得见。**
                #
                # ## K6 去重规则（用户明确要求）
                # 只对**具有可靠相同目标标识**的活动 unresolved 记录去重；
                # **没有可靠主题键时不根据字符相似度合并**。
                #
                # 所以：
                #   · outcome == "tie"（有并列候选）→ 有可靠标识（就是那组 id）
                #     → 同一组 id 的 unresolved 只保留最近一条
                #   · outcome == "no_match"（最高分 ≤ 0，无字面关联）
                #     → **不算可靠目标标识** → 每次都可以记（不去重）
                diag = _correction_diagnosis(items, inc, rules)
                if diag["outcome"] == "tie":
                    sig_id = _unresolved_signature(diag["candidates"])
                    # 只跟**活动**记录比（已解决的旧记录不参与去重）
                    if sig_id in _active_unresolved:
                        # 已达同一目标标识的活动记录 → 不重复追加
                        pass
                    else:
                        _active_unresolved.add(sig_id)
                        changes.append({
                            "type": "unresolved",
                            "signature": sig_id,
                            "reason": "修正目标不唯一（并列），按方案 A 不猜、不自动改",
                            "candidates": diag["candidates"],
                            "evidence": inc["belief"],
                        })
                else:
                    # 无可靠目标标识 → 不去重（用 seq 做一次性标识，仅用于记录）
                    changes.append({
                        "type": "unresolved",
                        "signature": f"unresolved:no-target@seq{inc['source'].get('seq')}",
                        "reason": "修正目标无法确定（无字面关联），按方案 A 不猜、不自动改",
                        "candidates": [],
                        "evidence": inc["belief"],
                    })

        items.append(inc)
        by_id[inc["id"]] = inc
        changes.append({"type": "add", "id": inc["id"], "belief": inc["belief"],
                        "status": inc["status"]})

    return items, changes


def _content_units(text: str) -> set[str]:
    """字符 2-gram 集合（去空白）。用于判断修正句指向哪一条既有判断。"""
    clean = "".join((text or "").split())
    return {clean[i:i + 2] for i in range(len(clean) - 1)} if len(clean) >= 2 else set()


def _correction_diagnosis(items: list[dict], correction: dict, rules: dict) -> dict:
    """P2：诊断「这条修正为什么没打到目标」—— **只读，不作废任何条目**。

    ## 为什么要单独有这个函数
    `_find_correction_target` 只返回「目标 或 None」，看不出**为什么**是 None。
    而 P2 的 `unresolved` 记录需要区分两种情况：

      · `tie`          —— 有并列的最高分候选 → **有可靠的目标标识**（就是那几条）
                          → K6 允许按这组标识去重
      · `no_match`     —— 最高分 ≤ 0，即**没有任何字面关联**
                          → **不算可靠目标标识** → K6 **不**据此去重
                          （用户原话：「没有可靠主题键时不根据字符相似度合并」）

    诊断结果是 `_content_units` 的交集，属**既有的修正定位判据**，
    **不引入任何新的相似度**。
    """
    candidates = [
        x for x in items
        if not x.get("superseded_by") and x.get("status") in (STATUS_INFERRED, STATUS_CONFIRMED)
    ]
    if not candidates:
        return {"outcome": "no_match", "candidates": [], "best_score": 0.0}

    stripped = correction.get("belief") or ""
    for kw in (rules.get("correction") or {}).get("keywords") or []:
        stripped = stripped.replace(kw, "")
    units = _content_units(stripped)
    if not units:
        return {"outcome": "no_match", "candidates": [], "best_score": 0.0}

    scored = []
    for cand in candidates:
        cu = _content_units(cand.get("belief") or "")
        if not cu:
            continue
        coverage = len(units & cu) / len(units)
        priority = 0 if cand.get("status") == STATUS_INFERRED else 1
        scored.append((cand, round(coverage, 4), priority))
    if not scored:
        return {"outcome": "no_match", "candidates": [], "best_score": 0.0}

    scored.sort(key=lambda t: (-t[1], t[2], t[0]["id"]))
    best_score = scored[0][1]
    if best_score <= 0:
        return {"outcome": "no_match", "candidates": [], "best_score": best_score}

    top = [t for t in scored if t[1] == best_score and t[2] == scored[0][2]]
    if len(top) > 1:
        return {
            "outcome": "tie",
            "best_score": best_score,
            # 按 id 排序，保证诊断结果**确定性**（去重标识要用它）
            "candidates": sorted(
                [{"id": c["id"], "belief": c.get("belief") or "", "score": s}
                 for c, s, _p in top],
                key=lambda d: d["id"],
            ),
        }
    # 唯一最高分 → 不是 unresolved（正常路径会作废它）
    return {"outcome": "unique", "best_score": best_score,
            "candidates": [{"id": top[0][0]["id"], "belief": top[0][0].get("belief") or "",
                            "score": best_score}]}


def _unresolved_signature(candidates: list[dict]) -> str:
    """K6 的去重标识：**只由候选条目的 id 组成**。

    ## 为什么必须只用 id
    用户原话：「没有可靠主题键时**不根据字符相似度合并**」。
    所以这里**不碰 belief 文本**，只用 id 集合 —— id 是稳定标识，
    同一组候选就视为同一件事，措辞变了也不影响。

    并列候选按 id 排序后拼接 → 结果确定性、可比较。
    """
    ids = sorted({c["id"] for c in (candidates or []) if c.get("id")})
    return "unresolved:" + ",".join(ids)


def _find_correction_target(items: list[dict], correction: dict, rules: dict) -> dict | None:
    """为一条修正找它要否定的既有条目。

    判据（按优先级）：

    1. **先剥掉修正词本身**（「我没说过」等），只留「关于什么」的部分。
       否则每条修正都会因共享「我没」而与所有条目乱配。
    2. **要求唯一最高分，并列就不猜。** 实测踩到的坑：用户说
       「我没说过我不太爱说话」时，真正的目标是「我是那种不太爱说话的人」，
       但与「我一般晚上才有精神」的重叠分**打平**（都是 2），
       代码退化成「取较新的一条」→ **作废了完全正确的那条判断**。
    3. **推断条目优先于明确说过的条目。** 同一分下先否定推断 ——
       「我没说过」通常是在否认模型的推测。
    4. **但明确说过的也能被修正。** 踩过的另一个坑：把候选限死在 inferred 后，
       用户否定一条 `confirmed` 判断时修正**完全失效**。
       而「模型记着用户明确说过 X、用户却说没说过」正是**最该发现的矛盾**，
       不能悄悄放过。作废时保留证据，便于人工核对。
    5. 找不出唯一目标就返回 None ——「修正不生效」比「误伤一条对的判断」安全。
    """
    candidates = [
        x for x in items
        if not x.get("superseded_by") and x.get("status") in (STATUS_INFERRED, STATUS_CONFIRMED)
    ]
    if not candidates:
        return None

    stripped = correction["belief"]
    for kw in (rules.get("correction") or {}).get("keywords") or []:
        stripped = stripped.replace(kw, "")
    units = _content_units(stripped)
    if not units:
        return None

    scored = []
    for cand in candidates:
        cu = _content_units(cand["belief"])
        if not cu:
            continue
        # 用覆盖率而非交集大小，避免长句凭长度取胜
        coverage = len(units & cu) / len(units)
        # 同分时推断优先（排在前面），再按 id 稳定排序
        priority = 0 if cand.get("status") == STATUS_INFERRED else 1
        scored.append((cand, round(coverage, 4), priority))

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[1], t[2], t[0]["id"]))
    best, best_score, _ = scored[0]
    if best_score <= 0:
        return None
    # 并列（同分且同优先级）→ 不猜
    for _, other_score, other_prio in scored[1:]:
        if other_score == best_score and other_prio == scored[0][2]:
            return None
    return best


def _overlap(a: str, b: str) -> int:
    """字符 2-gram 交集大小。纯计数，确定性。"""
    ua = {a[i:i + 2] for i in range(len(a) - 1)}
    ub = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(ua & ub)


def adjudicate_by_observation(items: list[dict], observation: dict,
                              rules: dict | None = None) -> dict:
    """系统裁定：由**结构化观察**决定是否推翻一条旧判断。

    ## ⚠️ 方案 A 下这个函数**几乎总是返回 not_executed** —— 这是设计，不是缺陷

    因为 `_topic_key_of` 恒返回 `None`（主题键接口只留未实现），
    所以判据①永不满足 → 永远没有候选 → 永远不执行。
    **它现在是"设计就位"，等主题键接口将来落地才生效。**

    ## 五条结构化判据（**完全不看任何文本标记**）

    实测依据：结构化观察 `专业 → 计算机（原：物流管理）` 若走自然语言的
    `_TIME`/`_NEG` 标记，两个都不命中 → 判 unclear → 该改没改。
    所以系统裁定**不能**用文本启发式，只能用结构化相等/不等。

    | # | 判据 | 不满足时 |
    |---|------|---------|
    | ① | `observation.topic_key` 与某条目 `topic_key` **相等** | 无候选 |
    | ② | 候选**唯一** | 不执行 |
    | ③ | 条目 `claim_kind != self_evaluation` | **禁止系统裁定** |
    | ④ | **`observation.old_value` 与条目当前值一致** | **不执行（不覆盖）** |
    | ⑤ | `new_value != old_value` | 无变化 |

    ## 判据③的由来（价值判断，用户已确认）
    **系统不得裁定推翻用户的主观体验。**
    发现「用户自评与行为不符」时，并列记下来，**不替他改自评** ——
    因为「她眼中的她」应当是**她对你的理解**，不是**系统对你下的判决**。

    ## 判据④防的是什么
    若观察的 `old_value` 与我们记录的不一致，说明**我们这边已经变了**
    （可能已被用户裁定过），此时该 `unresolved` 而**不是覆盖**。
    """
    topic_key = (observation or {}).get("topic_key")
    new_value = (observation or {}).get("new_value")
    old_value = (observation or {}).get("old_value")

    if not topic_key:
        return {"executed": False, "reason": "观察没有 topic_key（结构化主题键缺失）"}
    if new_value is None or old_value is None:
        return {"executed": False, "reason": "观察缺 new_value / old_value"}
    if new_value == old_value:
        return {"executed": False, "reason": "无变化（new_value == old_value）"}

    # 判据①：主题键相等（显式，不是相似）
    candidates = [
        x for x in items
        if not x.get("superseded_by") and _topic_key_of(x) == topic_key
    ]
    if not candidates:
        return {"executed": False,
                "reason": f"无候选（没有条目的显式主题键等于 {topic_key!r}）"}

    # 判据②：候选唯一
    if len(candidates) > 1:
        return {"executed": False,
                "reason": f"候选不唯一（{len(candidates)} 条同主题键）",
                "candidates": [c["id"] for c in candidates]}

    target = candidates[0]

    # 判据③：禁止系统裁定用户的自我评价
    t_ck = (target.get("source") or {}).get("claim_kind")
    if t_ck == CLAIM_SELF_EVALUATION:
        return {"executed": False, "target_id": target["id"],
                "reason": "目标是用户的自我评价（claim_kind=self_evaluation）"
                          "—— 系统**不得**推翻用户的主观体验"}

    # 判据④：old_value 必须与条目当前值一致
    #
    # ⚠️ 我们目前**没有**"条目当前值"这个字段（没有结构化的 value），
    # 所以只能拿观察声明的 old_value 与条目文本比对。
    # 方案 A 下这一步通常走不到（判据①已经拦住了），保留以备接口落地。
    belief = target.get("belief") or ""
    if old_value not in belief:
        return {"executed": False, "target_id": target["id"],
                "reason": f"观察的 old_value={old_value!r} 与现有记录不一致"
                          f"（记录为 {belief!r}）→ unresolved，**不覆盖**"}

    # 判据①～⑤ 全过 → 才执行
    return {"executed": True, "target_id": target["id"],
            "change": "adjudicated", "adjudicated_by": "system",
            "reason": f"结构化观察推翻：{old_value} → {new_value}"}


def active_items(items: list[dict]) -> list[dict]:
    """未被取代的条目，且**排除修正指令本身**。

    修正条目的作用是「指向一条既有判断并作废它」，它本身不是关于用户的判断，
    不该注入。踩过的坑：不排除时模型会同时持有
    「我是那种不太爱说话的人」和「我没说过我不太爱说话」两条互相矛盾的生效条目。
    """
    return [
        x for x in items
        if not x.get("superseded_by") and not x.get("is_correction")
    ]


# ----------------------------------------------------------------------------
# 落盘（版本化，永不覆盖）+ 读取
# ----------------------------------------------------------------------------

def snapshot_path(version: int):
    return USER_MODEL_DIR / f"model_v{version:03d}.json"


def existing_snapshots() -> list[int]:
    ensure_dirs()
    out = []
    for path in USER_MODEL_DIR.glob("model_v*.json"):
        try:
            out.append(int(path.stem.split("_v")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def next_snapshot_version() -> int:
    versions = existing_snapshots()
    return (versions[-1] + 1) if versions else 1


def save_snapshot(items: list[dict], *, note: str = "") -> dict:
    """落一份用户模型快照。**绝不覆盖已存在的版本**；撞号就往后找一个空号。

    ## ⚠️ 这里原来有个会让数据静默丢失的缺陷（真实对话暴露的）
    #
    # 原实现是「取 `next_snapshot_version()`（= 最大号 + 1）→ 若该文件已存在则**抛错**」。
    # 后果链条（在 19:51 那次真实对话里实际发生）：
    #
    #   1. 快照号是**磁盘推导**的（`max(已有号) + 1`），没有内存计数器
    #   2. 于是**任何进程外对 user_model/ 的改动**（例如 --clean 删掉最新那份）
    #      都会让"下一个号"**回退**到已被占用的号
    #   3. 撞号 → `save_snapshot` 抛 `UserModelError`
    #   4. `chat.send_once` **没有接住这个异常** →
    #      那一轮的用户模型变更**整轮丢失**，运行日志里连一行都没有
    #
    # 实测证据：`run.log` 里只有 `seq 148` 那轮有
    # `user model updated: 2 changes, snapshot=v008`，
    # 而 **seq 145 / 150 / 152 三轮一行都没有** —— 它们静默没落盘。
    #
    # ## 修法：撞号不再抛错，而是往后找空号
    #
    # 「绝不覆盖」这条纪律要保住（那是 append-only 的核心），
    # 但"号被占了"不该让整轮变更丢失 —— 往后找一个空号写就行。
    """
    ensure_dirs()
    version = next_snapshot_version()
    path = snapshot_path(version)
    # 撞号就递增找空号（上限防御：正常绝不会走到这么多）
    for _ in range(1000):
        if not path.exists():
            break
        version += 1
        path = snapshot_path(version)
    else:
        raise UserModelError(
            f"连续 1000 个快照号都被占用，拒绝继续：{path.name}（请检查 user_model/ 目录）"
        )

    payload = {
        "version": f"v{version:03d}",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": note,
        "item_count": len(items),
        "active_count": len(active_items(items)),
        "items": items,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.write_text(body + "\n", encoding="utf-8")
    (USER_MODEL_DIR / f"model_v{version:03d}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
    return {"version": payload["version"], "file": f"user_model/{path.name}",
            "sha256": digest, "items": len(items), "active": len(active_items(items))}


def load_latest_snapshot() -> dict | None:
    versions = existing_snapshots()
    if not versions:
        return None
    path = snapshot_path(versions[-1])
    return json.loads(path.read_text(encoding="utf-8"))


def load_items() -> list[dict]:
    """当前用户模型的条目列表。

    真值来源是**最新一份快照**（与关系状态不同 —— 关系状态可以从事件表重算，
    用户模型因为含 LLM 无关的合并且条目会演化，用快照作为真值更简单直白）。
    没有快照时返回空列表。
    """
    snap = load_latest_snapshot()
    if not snap:
        return []
    return list(snap.get("items") or [])


def independent_evidence(items: list[dict], claim_id: str) -> int:
    """数一条判断有多少**独立陈述**支持它 —— 按 evidence 原文去重。

    ## 为什么不数"提到过几次"
    用户明确要求：「**同一段话被重复引用，不算多份独立证据**」。
    所以判据是**不同的原话**有几段，而不是"被提到几次"。

    诚实说明：这仍是弱判据 —— 它分不出
    「同一句自我评价被说了两遍」与「两次独立情境下的相同陈述」。
    真正的独立性需要「不同时间 + 不同情境 + 行为印证」，
    那属于后续阶段（见 endoxa 审计里 `supports` 是佐证、不是理由那条）。
    """
    texts = set()
    for it in items:
        if it.get("id") != claim_id:
            continue
        ev = ((it.get("source") or {}).get("evidence") or "").strip()
        if ev:
            texts.add(ev)
    return len(texts)


def review_candidates(items: list[dict]) -> list[dict]:
    """挑出「够格供人工复核」的 `uncertain` 条目。

    ## 这是一个**提议**，不是一次升级
    返回的条目**不改 status、不改 confidence**。调用方（`usermodel` 命令）
    只把它们打印出来提示人去看。**升不升由人决定。**

    这样 `UNCERTAIN_EVENT_THRESHOLD` 真的参与了一次判定，
    又没有把「重复次数」冒充成「可信度」。
    """
    out = []
    for it in active_items(items):
        if (it.get("status") or "") != STATUS_UNCERTAIN:
            continue
        n = independent_evidence(items, it.get("id"))
        if n >= UNCERTAIN_EVENT_THRESHOLD:
            out.append({"item": it, "independent": n})
    return out


def verify_snapshots() -> dict:
    """校验快照与 .sha256 是否一致。只读。"""
    report = {"checked": 0, "ok": True, "errors": [], "missing_sidecar": []}
    for version in existing_snapshots():
        report["checked"] += 1
        path = snapshot_path(version)
        sidecar = USER_MODEL_DIR / f"model_v{version:03d}.sha256"
        body = path.read_text(encoding="utf-8").rstrip("\n")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if not sidecar.exists():
            report["ok"] = False
            report["missing_sidecar"].append(f"model_v{version:03d}")
            continue
        if sidecar.read_text(encoding="utf-8").split()[0] != digest:
            report["ok"] = False
            report["errors"].append(f"model_v{version:03d}：内容与 .sha256 不一致")
    return report


# ----------------------------------------------------------------------------
# 注入未来对话
# ----------------------------------------------------------------------------

def render_for_prompt(items: list[dict], *, limit: int = 8) -> str:
    """把「她眼中的用户」拼成注入上下文的一段。

    刻意标注状态：让模型知道自己对某条是**推断**还是**用户明确说过**，
    避免把推断当既定事实用（与 Phase 5 的 FACT/INFERENCE 是同一个原则）。
    也只注入**未被取代**的条目。

    ## P0 修复：现在**同时标注信息类型**
    只标 status 是不够的 —— 模型看到 `[confirmed] 我执行力很差` 时，
    仍可能把它当成"用户执行力客观差"来用。
    所以额外标出 `claim_kind`，并明确写出「自我评价不等于客观事实」。

    ## P0 第二步：`uncertain` 现在是**真的会被读到**
    在这之前 `status == uncertain` 从来没有出现在任何逻辑分支里（死枚举）。
    现在它参与三项决策：
      1. **单独成段** —— 不确定的与已确认的**分开列**，不让它混在事实里
      2. **改变措辞** —— 明确要求：不下定论、必要时**问用户**而不是猜
      3. **限制占比** —— 不确定项最多占一半槽位，**不许挤掉已确认的信息**
    """
    active = active_items(items)
    if not active:
        return ""

    # ---- 决策 1：按 status 把条目分开 -------------------------------
    #       这是 `uncertain` 第一次真正参与判定（此前零读取点）。
    def _status_of(x):
        return (x.get("status") or "").strip()

    uncertain_items = [x for x in active if _status_of(x) == STATUS_UNCERTAIN]
    certain_items = [x for x in active if _status_of(x) != STATUS_UNCERTAIN]

    # ---- 决策 3：不确定项最多占一半槽位（防止挤掉已确认的信息）------
    #       现状 bug 是"按 confidence 排序取前 N"，而 uncertain 与 inferred
    #       同 confidence(0.5)，纯按分数排会让不确定项随机挤掉确定项。
    u_cap = max(1, limit // 2) if uncertain_items else 0
    u_cap = min(u_cap, limit)
    c_cap = max(0, limit - len(uncertain_items[:u_cap]))

    def _rank(xs):
        return sorted(xs, key=lambda x: (-x.get("confidence", 0), x["id"]))

    certain_show = _rank(certain_items)[:c_cap]
    uncertain_show = _rank(uncertain_items)[:u_cap]

    def _label(it):
        ck = (it.get("source") or {}).get("claim_kind") or ""
        lab = f"[{_status_of(it)}"
        if ck:
            lab += f"｜{ck}"
        return lab + "]"

    lines = ["【你对用户的理解（由长期互动形成，可能有误）】"]
    has_eval = False
    for it in certain_show:
        if ((it.get("source") or {}).get("claim_kind")) == CLAIM_SELF_EVALUATION:
            has_eval = True
        lines.append(f"- {_label(it)} {it['belief']}")

    # ---- 决策 2：不确定的**单独成段 + 改变措辞** ---------------------
    if uncertain_show:
        lines.append("")
        lines.append("【下面这些你**还不确定**，不要当成事实用】")
        for it in uncertain_show:
            lines.append(f"- {_label(it)} {it['belief']}")
        lines.append(
            "这些是用户自己表达得不确定、或者你判断不了性质的。"
            "**不要把它当定论去陈述**；如果它和当前对话有关，"
            "应该**直接问用户**来确认，而不是自己推测。"
        )

    lines.append("注意：标注为 inferred / uncertain 的是你的推断，不是用户明说的。")
    # P2：把 `verifiable` 的语义边界在**注入时就写清**。
    #
    # 为什么要写：`verifiable="yes"` 只表示「这条记录的内容可以被核实」，
    # 若不加说明，模型很可能读成「这件事已经证实了」——
    # 那正是 P0 在修的同一类错误（把"可核对"当成"已成立"）。
    if any((it.get("source") or {}).get("verifiable") == VERIFIABLE_YES for it in certain_show):
        lines.append(
            "注意：标注 verifiable=yes 的只表示**这条记录的内容可以被核实**"
            "（例如「我的专业是物流」可以去核对学籍），"
            "**不代表它已经被证实**。它仍然只是用户说过的话。"
        )
    if has_eval:
        # 这一句是 P0 的核心：把「用户说过」与「客观成立」在**注入时就分开说清**
        lines.append(
            "注意：标注 self_evaluation 的是**用户对自己的主观评价**，"
            "它只说明「用户这样评价自己」，**不等于客观事实**。"
            "不要在缺少行为证据时把它当成对用户能力/性格的定论，"
            "也不要用它去否定用户当下的表现。"
        )
    lines.append("如果用户纠正你，以用户当前说法为准。")
    return "\n".join(lines)


def format_model(items: list[dict]) -> str:
    active = active_items(items)
    superseded = [x for x in items if x.get("superseded_by")]
    lines = [
        f"条目总数    ：{len(items)}"
        f"（生效 {len(active)}　已被修正 {len(superseded)}）",
    ]
    if not items:
        lines.append("（还没有任何对用户的理解 —— 它会在聊天过程中自动累积，无需手动生成）")
        return "\n".join(lines)

    lines.append("")
    lines.append("生效中的条目：")
    n_uncertain = 0
    for it in sorted(active, key=lambda x: (-x.get("confidence", 0), x["id"])):
        label = STATUS_LABELS.get(it["status"], it["status"])
        ck = (it.get("source") or {}).get("claim_kind") or ""
        mark = " ⚠" if it["status"] == STATUS_UNCERTAIN else ""
        if it["status"] == STATUS_UNCERTAIN:
            n_uncertain += 1
        lines.append(f"  [{label}｜{ck}] {it['belief']}{mark}"
                     if ck else f"  [{label}] {it['belief']}{mark}")
        lines.append(f"        置信 {it['confidence']}　来源 {it['source']['kind']}"
                     f"　更新于 {it['last_updated'][:19]}")
        ev = (it.get("source") or {}).get("evidence")
        if ev:
            lines.append(f"        原话：{ev[:60]}")

    # ---- P0 第二步：`uncertain` 的复核提示（只标记，不升级）------------
    cands = review_candidates(items)
    if n_uncertain or cands:
        lines.append("")
        lines.append(f"不确定的条目：{n_uncertain} 条"
                     f"（`uncertain` = 用户自己表达得不确定，或判断不了性质）")
        if cands:
            lines.append(f"  其中 {len(cands)} 条已攒到 "
                         f"{UNCERTAIN_EVENT_THRESHOLD} 段以上独立陈述，**够格供你复核**：")
            for c in cands:
                lines.append(f"    · {c['item']['belief']}（独立陈述 {c['independent']} 段）")
            lines.append("  → 这只是**提示**。系统不会自动升级它 ——")
            lines.append("     要不要当作更确定的判断，由你决定（次数≠可信度）。")

    if superseded:
        lines.append("")
        lines.append("已被修正的条目（保留以便追溯，不再注入）：")
        for it in superseded:
            lines.append(f"  [已作废] {it['belief']}")
            lines.append(f"        被 {it['superseded_by']} 取代于 {it['superseded_at'][:19]}")
            for h in it.get("revision_history") or []:
                lines.append(f"        依据：{h.get('evidence', '')[:50]}")
    return "\n".join(lines)
