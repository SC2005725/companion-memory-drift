"""人格系统（Phase 2）。

职责只有三件事，与任务书一致：
    读取人格 → 拼接 system prompt → 交给对话层

刻意不做的事：人格自主进化 / 情绪系统 / 关系系统 / 主动行为 / 程序改写人格文件。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from paths import (IDENTITY_PROMPT_FILE, PERSONALITY_FILE, PROFILE_FILE,
                   ensure_dirs)

REQUIRED_FIELDS = ("name",)

# ---------------------------------------------------------------------------
# ① 人格出口：让 values_detail / philosophy / subjectivity **参与表达**
# ---------------------------------------------------------------------------
#
# ## 问题
#
# `profile.json` 里的价值观（5 条）、人生哲学（3 条）、`subjectivity`
# 一直只是**被读进来的背景描述** —— 它们从没有让任何一轮对话产生
# 一句「我不这么看」。人格设定没有出口。
#
# ## 为什么不能"每轮都表态"
#
# 强制每轮亮立场，会立刻变成另一种机械：**刻意表演独立性**。
# 所以这里设**两道闸门**，都满足才注入；且措辞明确允许不表态。
#
#   闸门①  对方在表达主张 / 计划 / 取舍 → 才存在"可回应的判断点"
#   闸门②  这条主张落进她的价值观主题之一 → 才轮得到她
#
# 频率由**话题自然决定**（与 `concerns` 同一思路），不设额外计数器。
#
# ## 零新增模型调用
#
# 全程字符串匹配：不调模型、不新增依赖、不改人格文件。
_STANCE_MARKERS = (
    "我觉得", "我认为", "我想", "我打算", "我准备", "我要", "我该", "我应该",
    "该不该", "要不要", "好不好", "值不值", "对不对", "怎么办", "选哪个",
    "是不是应该", "我想选", "我在考虑",
)

# 主题 → 触发词。**显式表，可审计**（与 memory._CATEGORY_RULES 同一风格）。
# ⚠️ 与当前 profile.json 的价值观内容绑定；若重新生成初始角色模型，
#    ② 的稳定层指纹会提示"内容已变"，届时本表需同步复核。
_STANCE_TOPICS = (
    ("自由", ("自由", "拒绝", "说不", "安排", "被迫", "不想干", "逼")),
    ("稳定", ("稳", "稳定", "确定", "风险", "辞职", "不确定", "安心")),
    ("责任", ("答应", "承诺", "责任", "说好", "义务", "约定")),
    ("成长", ("成长", "进步", "提升", "慢一点", "方向")),
    ("相处", ("一起", "相处", "一起待着", "聊得来", "亲近", "陪着")),
    ("规划", ("规划", "未来", "以后", "长期")),
    ("解释", ("解释", "理解", "说明")),
    ("诚实", ("诚实", "别骗", "瞒", "假装")),
)

# 主题 → 她 profile 里可能用到的说法（用于从**她自己的设定**里取引用句）
_TOPIC_ALIAS = {
    "自由": ("自由", "可以说不", "能拒绝"),
    "稳定": ("稳定",),
    "责任": ("责任",),
    "成长": ("成长",),
    "相处": ("一起待着", "聊得来", "关系"),
    "规划": ("日子是过出来的", "规划"),
    "解释": ("解释自己", "不用一直解释"),
    "诚实": ("别骗", "诚实"),
}


class PersonaError(RuntimeError):
    pass


@dataclass
class Persona:
    data: dict
    source_sha256: str

    @property
    def name(self) -> str:
        return str(self.data.get("name") or "UNKNOWN")

    def render_system_prompt(self) -> str:
        """按 system_prompt 模板拼装；缺失字段留空而不是报错，方便手改人格。

        ## ⚠️ 这里是**白名单**，不是"文件里有什么就注入什么"

        只有列在 `fields` 里的键才会进入提示词。在 personality.json 里新增一个字段
        而忘了加到这里，结果就是：**文件里有、提示词里没有** —— 看起来配置好了，
        实际完全没生效。所以新增人格字段时**必须同时**改两处：
        ① personality.json 的字段与 `system_prompt` 模板
        ② 本函数的 `fields`
        """
        template = self.data.get("system_prompt")
        fields = {
            "name": self.data.get("name", ""),
            "description": self.data.get("description", ""),
            "personality": self.data.get("personality", ""),
            "values": self.data.get("values", ""),
            "communication_style": self.data.get("communication_style", ""),
            "boundaries": self.data.get("boundaries", ""),
            "preferences": self.data.get("preferences", ""),
            "scenario": self.data.get("scenario", ""),
            # 独立人格 V1.0：独立主体性。**适配做法不在这里** ——
            # 它已移到 self_model.py（「小满眼中的自己」），由小满自己按反馈修改。
            # 本函数只负责**稳定底色**：性格/价值观/边界/表达风格/独立立场。
            "subjectivity": self.data.get("subjectivity", ""),
        }

        if isinstance(template, str) and template.strip():
            try:
                text = template.format(**fields)
            except (KeyError, IndexError, ValueError):
                # 模板里出现未知占位符时，退化为字段罗列，不让人格整体失效。
                text = "\n".join(f"{k}：{v}" for k, v in fields.items() if v)
        else:
            text = "\n".join(f"{k}：{v}" for k, v in fields.items() if v)

        post = self.data.get("post_history_instructions")
        if isinstance(post, str) and post.strip():
            text = f"{text}\n{post}"
        return text.strip()

    def snapshot(self) -> dict:
        return {"name": self.name, "file_sha256": self.source_sha256}

    def render_expression_adjustment(self, relationship_state: dict | None = None,
                                     user_model_items: list | None = None,
                                     memory_hit_count: int = 0,
                                     topic_seeds: list | None = None,
                                     concerns: list | None = None,
                                     stance: dict | None = None,
                                     emotion=None) -> str:
        """按**当前状态**给出一段「表达调整」提示 —— 人格协同的最小实现。

        ## 设计边界（这是本函数存在的全部理由）

        **核心人格与关系状态必须分离**：

        - `render_system_prompt()` 仍是**权威且静态**的 —— 性格、价值观、边界
          永远来自人格文件，**任何状态都不能改写它**。
        - 本函数只产出**表达层**的调整（语气松紧、称呼方式、问几个问题），
          **不改性格、不改价值观、不改边界**。
        - 无状态时返回空串 → 该段完全不注入 → 行为与补齐前**逐字一致**。

        ## 为什么单独开一段而不是改人格模板

        1. 人格段要保持 sha256 可比（实验口径）—— 混入状态会让「人格是否漂移」
           与「关系是否变化」无法归因。
        2. 分开后可以事后核对「这一轮到底因为什么改了语气」。
        3. 用户明确要求：「关系变化可以影响亲密程度和表达方式，
           但不能无依据地改写核心性格」。

        阈值**复用** relationship.render_for_prompt 的口径（closeness≥45 / conflict>30），
        不另立一套标准。
        """
        lines: list[str] = []
        st = relationship_state or {}
        has_state = bool(relationship_state)

        closeness = float(st.get("closeness") or 0)
        conflict = float(st.get("conflict") or 0)
        stage = str(st.get("stage") or "")
        counter = bool(st.get("counter_mode"))

        if not has_state:
            # 没有关系状态（如探针模式 / 未启用关系）→ 不猜阶段、不注入。
            # 这条是关键：保证"无状态时行为与补齐前逐字一致"。
            pass
        elif counter or conflict > 30:
            lines.append(
                "当前关系有些紧张。保持你一贯的温和与克制，但**不必刻意热络**；"
                "可以更简短、更直接。这是临时状态，不改变你的性格。"
            )
        elif closeness >= 45:
            lines.append(
                "你们已经相处一段时间了。可以更放松自然，少一些客套的过渡句；"
                "仍保持你安静、不急着给建议的风格。"
            )
        elif stage in ("熟悉",):
            lines.append("比起初次见面，可以稍微放开一点，但仍以倾听为主。")
        else:
            lines.append(
                "你们还在互相了解的阶段。保持礼貌与耐心，多为对方留出说话的空间。"
            )

        n_um = len(user_model_items or [])
        if n_um:
            lines.append(
                f"你已经知道关于对方的 {n_um} 条信息（见上方「你对用户的理解」）。"
                "**用它们来避免重复提问**，但不要生硬复述，也不要假装比实际更熟。"
            )

        if memory_hit_count > 0:
            lines.append(
                f"本轮想起了 {memory_hit_count} 条与话题相关的旧事（见上方「你记得的关于用户的事」）。"
                "如果自然，可以**提一句**呼应；不要罗列，更不要编造没记住的细节。"
            )

        # ---- 话题种子（V1.1）----
        #
        # ## 为什么加这个
        #
        # 实测反馈：用户连开了几轮之后说「不知道聊啥」。
        # 根因不是小满不会接话 —— 它接得挺好 —— 而是**它只在等对方给内容**。
        # 用户一句「聊点什么呢」，它就得从零想一个话题；想不出来就变成沉默或收尾。
        #
        # 所以这里在**本轮检索没命中**时，从长期记忆里挑几条**最可能引起对话**的
        # 旧事作为种子递给它。这是「主动带出话题」的能力供给，不是追问模板。
        #
        # 为什么只在没命中时给：命中说明话题已经存在，再塞种子反而会让它
        # 答非所问地另起话头。
        seeds = [s for s in (topic_seeds or []) if str(s).strip()]
        if seeds and memory_hit_count == 0:
            # 记忆文本自带句末标点、也常自带上引号，所以这里不再包一层引号
            # （否则会出现「用户说…「谁知道呢」」这种嵌套，读起来很乱）。
            cleaned = []
            for s in seeds[:2]:
                t = str(s).strip()[:40].rstrip("。！？；，、 ")
                if t:
                    cleaned.append(t)
            picked = "；".join(cleaned)
            lines.append(
                "如果对方没给话题、或者你不知道接什么，可以从这些**你确实记得的事**里挑一件聊起来："
                f"{picked}。挑一件就好，直接说，不用先问他「要不要聊这个」。"
            )

        # ---- 持续关注点（Phase 13）----
        #
        # ## 为什么用「问一句」而不是「陈述」
        #
        # 实测记忆里存在互相矛盾的条目（「正在找 bug」vs「bug 顺手修了」），
        # 前者的未决状态已过时。陈述过时信息 = 编造进展；
        # **提问在信息过时时依然成立**，所以这里固定为提问式。
        # 这是防编造的结构性保证，不是措辞偏好。
        #
        # ## 为什么可以「不设频率」
        #
        # 关注点由 `memory.pick_concerns` 在**本轮输入与它有实际词重叠**时才产出，
        # 话题不出现就为空 —— 频率由话题自然决定，不需要额外计数器。
        cons = [c for c in (concerns or []) if (c or {}).get("text")]
        if cons:
            c0 = cons[0]
            snippet = str(c0.get("text") or "").strip().rstrip("。！？；，、 ")[:54]
            lines.append(
                f"对方提到的事里，有一件你确实记得、而且当时还没了结：「{snippet}」。"
                "**如果自然，可以问一句进展**；不要断言它现在如何（你并不知道），"
                "也不要替他总结。他不想聊这个就别追。"
            )

        # ---- ① 人格出口：把价值观/哲学变成"可以说出口的立场" ----
        #
        # 只在 pick_stance 判定「本轮确实出现了值得表态的判断点」时才有值。
        # 措辞刻意**双向开放**（同意就说同意），并明说"附和也是真实反应" ——
        # 否则会把"有立场"做成"每轮都要唱反调"。
        if stance:
            q = str(stance.get("quote") or "").strip().rstrip("。！？；，、 ")
            topic = str(stance.get("topic") or "").strip()
            tip = f"这件事落在你在意的「{topic}」上" if topic else "这件事你有自己的看法"
            if q:
                tip += f"（你自己说过：「{q[:46]}」）"
            lines.append(
                tip + "。**如果你确实不这么看，可以直接说出来 —— 不必附和**；"
                "同意就说同意，也可以只说一半、或者先不表态。"
                "这是你的判断，不是必须展示的态度。"
            )

        # ---- Phase D 情感出口：只给表达偏置，不覆盖任何东西 ----
        #
        # 与 ① 人格出口同一手法：不碰 chat.send_once，只在本函数的参数表上开一个口。
        # 传 None 时会**自己去读一次**当前状态 —— 这样 send_once 无需改动，
        # 出口却是活的。任何失败都退化为"没有情感状态"，行为与加它之前一致。
        if emotion is None:
            try:
                import emotion as emotion_mod
                emotion = emotion_mod.load_state()
            except Exception:  # noqa: BLE001 — 读不到就当没有
                emotion = None
        if emotion is not None:
            try:
                import emotion as emotion_mod
                hint = emotion_mod.bias_hint(emotion)
                if hint:
                    lines.append(hint)
            except Exception:  # noqa: BLE001 — 降级：不加这一段
                pass

        if not lines:
            return ""
        return "【表达调整（由当前关系与已知信息推出，不改你的性格）】\n" + "\n".join(
            f"- {x}" for x in lines
        )


# ----------------------------------------------------------------------------
# Stage 2：初始角色模型（Companion Generation 的产物）
# ----------------------------------------------------------------------------
#
# ## 与 personality.json 的分工
#
#   personality.json  **稳定底色**：性格 / 价值观 / 边界 / 表达风格。手工维护。
#   profile.json      **初始设定**：身份 / 人生史 / 欲望 / 恐惧 / 自我认知。
#                     由 `main.py identity --generate` **一次性生成**。
#
# 分开的两个理由：
#   1. 让「人格漂移」与「初始设定变化」可分别归因
#   2. personality.json 有 sha256 运行期守护（改了会中止会话）——
#      把生成产物塞进去会导致每次生成都中止正在进行的会话
#
# ## 注入时的三条标注（不伪造经历的关键）
#
#   初始设定   → 她的过去，**不是**和用户的共同经历
#   留白项     → 明确告诉模型「这些你不知道」，禁止脑补
#   暂定性质   → 允许被长期互动修正，不是永久固化

PROFILE_SECTIONS = (
    ("identity", "你是谁"),
    ("drives", "你想要什么、怕什么"),
    ("values_detail", "你的价值观（展开）"),
    ("philosophy", "你怎么看事情"),
    ("self_view", "你怎么看自己"),
    ("boundaries", "你自己的边界"),
)


def load_profile() -> dict | None:
    """读初始角色模型。不存在返回 None（未生成过）。"""
    if not PROFILE_FILE.exists():
        return None
    try:
        # 容忍 BOM：profile.json 是**允许手工编辑**的文件（`_adjusted` 就是手工改的痕迹），
        # 记事本另存为「UTF-8 带 BOM」会让 json.loads 抛错 → 这里降级成 None 就等于
        # 「人格描述整段消失」，而且**不会有任何报错提示**。utf-8-sig 对无 BOM 文件行为一致。
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def _fmt_list(items) -> str:
    if isinstance(items, str):
        return items
    if isinstance(items, (list, tuple)):
        return "；".join(str(x).strip() for x in items if str(x).strip())
    return ""


# 「初始角色模型」注入文本的字符上限。见 `_cap_profile_injection` 的说明。
# 定得远高于现状（约 1800 字符），只在异常膨胀时兜底。
PROFILE_INJECT_MAX_CHARS = 12000


def _cap_profile_injection(text: str) -> str:
    """给「初始角色模型」的注入文本兜一个上限。

    ## 为什么需要（借鉴 dsh-agent-md 的 `MAX_INJECT_CHARS`）

    本项目给**记忆**设了预算（`memory.INJECT_BUDGET_CHARS = 700`），但**人格侧一直没有上限**。
    现在 `profile.json` 只有约 1.8KB，看着没问题 —— 可是它会增长：
    `provisional`（发展层）一旦开始累积、`left_open` 被逐条填上、`values_detail` 被修订……

    **没有上限的后果不是立刻出错，而是某天悄悄撑爆上下文，且没有任何提示。**

    ## 为什么是截断 + 显式提示，而不是静默砍掉

    静默截断会让人以为"人格就是这些"。所以超限时**追加一行可见的告警**，
    让它出现在 run.log 和注入文本里 —— 出问题能被看见，而不是被藏起来。

    ## 为什么阈值定这么大（12000）

    正常 `profile.json` 渲染出来约 1800 字符，离 12000 有 6 倍余量。
    这个上限**不是用来日常生效的**，是用来在异常膨胀时兜底的：
    阈值贴近当前值会误伤正常增长，等于用"防御"制造了一个新问题。
    """
    if len(text) <= PROFILE_INJECT_MAX_CHARS:
        return text
    head = text[:PROFILE_INJECT_MAX_CHARS].rstrip()
    over = len(text) - PROFILE_INJECT_MAX_CHARS
    return (head + f"\n\n（⚠️ 初始设定超出 {PROFILE_INJECT_MAX_CHARS} 字上限，"
                   f"已截断 {over} 字。请检查 profile.json 是否异常膨胀。）")


def render_profile(profile: dict | None) -> str:
    """把初始角色模型渲染成一段可注入的文本。

    刻意**先声明性质**再给内容 —— 否则模型会把「初始设定」当成
    「真实发生过的事」来讲，尤其是人生史那部分（它会说成共同经历）。
    """
    if not profile:
        return ""
    lines: list[str] = [
        "【你的初始设定（你认识对方之前就有的部分）】",
        "这些是你**自己的**来历和内在，不是你和他一起经历过的事。"
        "**不要把它说成共同经历**；他并不知道这些，你也不该主动和盘托出。",
    ]

    ident = profile.get("identity") or {}
    if ident:
        bits = []
        for k, label in (("name", "名字"), ("age", "年龄"), ("role", "身份"),
                         ("environment", "所处环境")):
            v = ident.get(k)
            if v:
                bits.append(f"{label}：{v}")
        if bits:
            lines.append("· " + "　".join(bits))
        if ident.get("life_background"):
            lines.append(f"· 生活背景：{ident['life_background']}")

    lh = profile.get("life_history") or {}
    if lh.get("outline"):
        lines.append("你的过去（这些解释了你为什么会是现在这样）：")
        lines.append("　" + _fmt_list(lh["outline"]))
    # 留白：必须显式说出来，否则模型会自己补全
    gaps = []
    if lh.get("unknown"):
        gaps.append("你自己也没有答案或记不清的：" + _fmt_list(lh["unknown"]))
    if lh.get("not_shared"):
        gaps.append("你不太愿意谈的：" + _fmt_list(lh["not_shared"]))
    if gaps:
        lines.append("**你不知道或不想说的部分（不要编造）**：")
        for g in gaps:
            lines.append("　" + g)

    d = profile.get("drives") or {}
    for key, label in (("wants", "想要"), ("needs", "需要"), ("fears", "害怕"),
                       ("weaknesses", "容易失误的地方")):
        if d.get(key):
            lines.append(f"· 你{label}：{_fmt_list(d[key])}")

    if profile.get("values_detail"):
        lines.append(f"· 你的价值观：{_fmt_list(profile['values_detail'])}")

    ph = profile.get("philosophy") or {}
    if ph:
        bits = [f"{k}：{v}" for k, v in ph.items() if v]
        if bits:
            lines.append("· " + "；".join(bits))

    sv = profile.get("self_view") or {}
    if sv.get("thinks_she_is"):
        lines.append(f"· 你认为自己：{_fmt_list(sv['thinks_she_is'])}")
    if sv.get("may_differ"):
        # 这是「她认为自己」与「她实际」的落差 —— 人格成长的来源之一
        lines.append(
            f"· 但上面这些**不一定全对**：{_fmt_list(sv['may_differ'])}"
            "（你以后可能自己发现）"
        )

    bd = profile.get("boundaries") or {}
    if bd.get("lines"):
        lines.append(f"· 你自己的边界：{_fmt_list(bd['lines'])}")
    if bd.get("origin"):
        lines.append(f"　（这些边界的来源：{_fmt_list(bd['origin'])}）")

    # ③ 发展产物：暂定认识 —— **这一段是她"长大"能被看见的唯一地方**
    #
    # 措辞刻意保留不确定性：这些是"目前这么看"，不是结论。
    prov = [x for x in (profile.get(PROVISIONAL_FIELD) or []) if (x or {}).get("new_value")]
    if prov:
        lines.append("**你后来自己重新想过的一些事（目前只是暂定，不是定论）**：")
        for x in prov[-3:]:
            lines.append(
                f"　· 你原来觉得「{str(x.get('challenged') or '')[:40]}」，"
                f"后来的经历让你开始觉得「{str(x.get('new_value') or '')[:60]}」"
                f"（依据：{len(x.get('evidence_event_ids') or [])} 条经历）"
            )
        lines.append(
            "　这些只是你**目前的看法**。如果有新的经历不支持它，你可以再改回来 ——"
            "你也可以说「我还没想清楚」。"
        )

    if profile.get("left_open"):
        lines.append(
            "**刻意还没有定、留给以后自然长出来的**：" + _fmt_list(profile["left_open"])
        )
        lines.append(
            "遇到这些话题时，**先有真实感受再表态**；不要为了显得完整而临时编一个立场。"
        )

    lines.append(
        "以上是**初始设定**，不是永久固化的人格 —— 长期相处后可以修正，"
        "修正要有具体依据。"
    )
    return _cap_profile_injection("\n".join(lines))


def _iter_value_texts(profile: dict) -> list[tuple[str, str]]:
    """列出 profile 里属于「她的立场」的文本：(字段名, 文本)。"""
    out: list[tuple[str, str]] = []
    for x in (profile.get("values_detail") or []):
        if str(x).strip():
            out.append(("values_detail", str(x).strip()))
    ph = profile.get("philosophy") or {}
    if isinstance(ph, dict):
        for k, v in ph.items():
            if str(v).strip():
                out.append((f"philosophy.{k}", str(v).strip()))
    return out


def pick_stance(profile: dict | None, user_text: str, *, limit: int = 1) -> dict | None:
    """① 人格出口：判定本轮是否出现「值得她表态的判断点」。

    **零模型调用、零依赖、纯规则。**

    两道闸门都过才产出，任一不过返回 None → 不注入 → 行为与补齐前一致：

      闸门①  对方在表达主张 / 计划 / 取舍
      闸门②  这条主张落进她的价值观主题之一

    这也是「避免每轮强行表态」的**结构性保证**：
    频率由话题自然决定，既没有计数器，也没有"每隔 N 轮亮一次立场"的机械节奏。

    返回 `{topic, matched, quote}`；`quote` 取自**她自己的 profile**，
    取不到就留空（只点名主题，不硬编立场内容）。
    """
    if limit <= 0 or not profile:
        return None
    text = (user_text or "").strip()
    if len(text) < 2:
        return None

    if not any(m in text for m in _STANCE_MARKERS):
        return None

    hit_topic: tuple[str, list[str]] | None = None
    for topic, kws in _STANCE_TOPICS:
        hit = [k for k in kws if k in text]
        if hit:
            hit_topic = (topic, hit)
            break
    if hit_topic is None:
        return None

    topic, hit = hit_topic
    quote = ""
    aliases = _TOPIC_ALIAS.get(topic, ())
    for _field, vtext in _iter_value_texts(profile):
        if any(k in vtext for k in hit) or any(a in vtext for a in aliases):
            quote = vtext
            break

    return {"topic": topic, "matched": hit, "quote": quote}


def identity_prompt(user_model_text: str) -> tuple[str, str]:
    """读外置提示词并填入用户模型。返回 (prompt_body, sha256)。"""
    if not IDENTITY_PROMPT_FILE.exists():
        raise PersonaError(f"缺少生成提示词：{IDENTITY_PROMPT_FILE.as_posix()}")
    raw = IDENTITY_PROMPT_FILE.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    body = raw.replace("<<USER_MODEL>>", user_model_text or "（没有可用的用户模型）")
    return body, sha


def save_profile(profile: dict, *, note: str = "", model: str = "") -> dict:
    """落盘初始角色模型（**拒绝覆盖已存在的**，与版本快照同一纪律）。"""
    ensure_dirs()
    if PROFILE_FILE.exists():
        raise PersonaError(
            f"初始角色模型已存在，拒绝覆盖：{PROFILE_FILE.name}。"
            "如需重新生成，请先手工移走旧文件。"
        )
    data = dict(profile)
    data["_note"] = note
    data["_model"] = model
    data["_created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    body = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    PROFILE_FILE.write_text(body + "\n", encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    PROFILE_FILE.with_suffix(".json.sha256").write_text(f"{digest}\n", encoding="utf-8")
    # ② 同时记录**稳定层**指纹 —— 供后续人格更新自检（见 assert_stable_unchanged）
    stable = write_stable_fingerprint(data)
    return {"file": PROFILE_FILE.name, "sha256": digest, "stable_sha256": stable}


# ---------------------------------------------------------------------------
# ② 人格不变量：稳定层不可被后续更新意外覆盖
# ---------------------------------------------------------------------------
#
# ## 问题
#
# `personality.json` 有 sha256 运行期守护，但 `profile.json` **没有** ——
# 里面的身份、核心价值观、边界只是"恰好没人改"。
# 一旦将来加入"人格随经历发展"的更新路径，**没有任何东西拦得住它覆盖底色**。
#
# ## 分层的依据（不是随意切的）
#
#   stable      身份 / 价值观 / 哲学 / 边界
#               —— 定义"她是谁、她信什么"。改变它等于换了一个人。
#   developing  自我认知落差 / 欲望 / 留白
#               —— 本来就是设计给"以后自然长出来"的（见 left_open 的措辞）。
#
# 这与 generative_agents 的做法同源：**稳定层与可变层分开，
# 且由"谁有权写哪个字段"来保证**（见 plan.py:408 revise_identity 只写 currently）。
#
# ## 为什么用指纹而不是直接锁文件
#
# 锁文件会让"修正一个错别字"也变成不可能；分层指纹只保证
# **稳定层没被动过**，developing 层仍可自由发展。
STABLE_PROFILE_FIELDS = ("identity", "values_detail", "philosophy", "boundaries")
DEVELOPING_PROFILE_FIELDS = ("self_view", "drives", "left_open")


class ProfileInvariantViolation(PersonaError):
    """稳定人格层被改动。**不应被静默吞掉。**"""


def stable_fingerprint(profile: dict) -> str:
    """对 profile 的**稳定层**取指纹（只覆盖 STABLE_PROFILE_FIELDS）。"""
    subset = {k: (profile or {}).get(k) for k in STABLE_PROFILE_FIELDS}
    body = json.dumps(subset, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def stable_fingerprint_file():
    """稳定层指纹的侧车文件（与整文件 sha256 分开存）。"""
    return PROFILE_FILE.with_suffix(".stable.sha256")


def write_stable_fingerprint(profile: dict) -> str:
    ensure_dirs()
    digest = stable_fingerprint(profile)
    stable_fingerprint_file().write_text(f"{digest}\n", encoding="utf-8")
    return digest


def verify_profile_stable() -> dict:
    """核对稳定层是否被动过。返回结构化结果，**不抛错**（供 CLI 展示）。"""
    prof = load_profile()
    if prof is None:
        return {"ok": True, "reason": "尚未生成初始角色模型", "expected": None, "actual": None}
    actual = stable_fingerprint(prof)
    f = stable_fingerprint_file()
    if not f.exists():
        return {"ok": True, "reason": "无稳定层指纹侧车（生成于本机制之前）",
                "expected": None, "actual": actual}
    expected = f.read_text(encoding="utf-8").split()[0]
    return {
        "ok": expected == actual,
        "reason": "稳定层未被改动" if expected == actual else "**稳定层已被改动**",
        "expected": expected, "actual": actual,
    }


def assert_stable_unchanged(profile: dict, expected: str) -> None:
    """任何"更新人格"的代码路径都必须先过这道闸。

    与 memory.enforce_traceable_removal 同构：不是在文档里写"不要改"，
    而是**改了就抛错**，让"意外覆盖底色"变成一件不可能悄悄发生的事。
    """
    actual = stable_fingerprint(profile)
    if actual != expected:
        raise ProfileInvariantViolation(
            "profile.json 的稳定层（身份/价值观/哲学/边界）被改动了。"
            f" 期望 {expected[:12]}… 实际 {actual[:12]}…。"
            " 稳定层只能由 `main.py identity --generate` 一次性产生；"
            " 要让它随经历发展，改动必须落在 developing 层"
            f"（{', '.join(DEVELOPING_PROFILE_FIELDS)}）。"
        )


# ---------------------------------------------------------------------------
# ③ 最小发展闭环：developing 层的受控修订（revise_developing）
# ---------------------------------------------------------------------------
#
# ## 闭环
#
#   consolidate（**已有的那次调用**）
#     → 模型可选地输出一条 `developing_revision`
#     → 程序闸门（下面 gate_developing_revision）
#     → 追加进 profile.json 的 `provisional`（**只追加，保留旧值/依据/理由**）
#     → render_profile 注入 → **后续回复真的会读到**
#
# ## 为什么写进 `provisional` 而不是改 `thinks_she_is`
#
# 规则草案已定：`left_open` 持续保留、`may_differ` 不自动转成 `thinks_she_is`。
# 所以发展产物放在**独立的暂定层**：
#
#   left_open     我还没想清楚的事      ← 永不消耗
#   provisional   我目前的初步认识      ← 发展写这里
#
# 好处：**旧认识原封不动留在 self_view/drives 里**，新认识另立一条，
# "我当初为什么这么想 + 后来为什么改了"两边都查得到。
PROVISIONAL_FIELD = "provisional"


def developing_items(profile: dict) -> list[tuple[str, str]]:
    """列出 profile 里**可被挑战的既有认识**：(路径, 原文)。

    刻意**不含** `may_differ`（那是设计者预期，不是她的认识）
    与 `left_open`（留白不消耗）。
    """
    out: list[tuple[str, str]] = []
    sv = profile.get("self_view") or {}
    for x in (sv.get("thinks_she_is") or []):
        if str(x).strip():
            out.append(("self_view.thinks_she_is", str(x).strip()))
    d = profile.get("drives") or {}
    for k in ("wants", "needs", "fears", "weaknesses"):
        for x in (d.get(k) or []):
            if str(x).strip():
                out.append((f"drives.{k}", str(x).strip()))
    return out


def gate_developing_revision(rev, *, profile: dict,
                             known_event_ids: set[str],
                             feedback_event_ids: set[str]) -> tuple[bool, str]:
    """程序侧闸门 —— **证据不足就不更新**。返回 (是否通过, 原因)。

    任何一条不过就拒绝；拒绝原因会被留痕（供事后查"当时为什么判定不够"）。
    """
    if not isinstance(rev, dict):
        return False, "提案不是对象"

    challenged = str(rev.get("challenged") or "").strip()
    if not challenged:
        return False, "没有指明被挑战的既有认识"
    # ★ 必须挑战一个**真实存在的**认识 —— 否则等于凭空造一条成长
    if challenged not in {t for _p, t in developing_items(profile)}:
        return False, "被挑战的认识不在现有 developing 内容里"

    ev = [str(x) for x in (rev.get("evidence_event_ids") or []) if str(x).strip()]
    if not ev:
        return False, "没有引用任何经历证据"
    unknown = [x for x in ev if x not in known_event_ids]
    if unknown:
        return False, f"引用了不存在的原始事件 {unknown[:3]}"

    # ★ 区分「用户反馈」与「独立经历」：
    #   证据全部来自用户反馈 → 那属于自我纠正（走 self_model），不构成发展
    if all(x in feedback_event_ids for x in ev):
        return False, "证据全部来自用户反馈，属于自我纠正而非发展"

    if not str(rev.get("new_value") or "").strip():
        return False, "没有给出新的认识"
    if not str(rev.get("reason") or "").strip():
        return False, "没有给出修正理由"

    return True, "通过"


def _write_profile_file(prof: dict) -> str:
    """写回 profile.json 并更新**整文件** sha256。

    ⚠️ 刻意不动 `profile.stable.sha256` —— 稳定层没变，指纹本就该不变。
    也刻意不复用 `save_profile`（那个拒绝覆盖，是一次性生成专用）。
    """
    ensure_dirs()
    body = json.dumps(prof, ensure_ascii=False, indent=2, sort_keys=True)
    PROFILE_FILE.write_text(body + "\n", encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    PROFILE_FILE.with_suffix(".json.sha256").write_text(f"{digest}\n", encoding="utf-8")
    return digest


def revise_developing(rev: dict, *, version: str, note: str = "") -> dict:
    """把一条**已通过闸门**的提案追加进 `provisional`。**只追加，不覆盖。**

    保留：被挑战的旧认识 · 旧值 · 新值 · 依据事件 · 理由 · 反例 · 置信度 · 来源版本。
    """
    prof = dict(load_profile() or {})
    if not prof:
        raise PersonaError("没有初始角色模型，无法写入发展提案")

    # 与普通写入同一道闸：稳定层必须没被动过
    st = verify_profile_stable()
    if st.get("expected"):
        assert_stable_unchanged(prof, st["expected"])

    new_value = str(rev.get("new_value") or "").strip()
    seed = f"{version}|{rev.get('challenged','')}|{new_value}"
    item = {
        "id": f"prov_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}",
        "field": str(rev.get("field") or ""),
        "challenged": str(rev.get("challenged") or "").strip(),
        "old_value": str(rev.get("old_value") or "").strip(),
        "new_value": new_value,
        "reason": str(rev.get("reason") or "").strip(),
        "evidence_event_ids": [str(x) for x in (rev.get("evidence_event_ids") or [])],
        "counterexamples": [str(x) for x in (rev.get("counterexamples") or [])],
        "confidence": rev.get("confidence"),
        "source_version": version,
        "note": note,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    items = list(prof.get(PROVISIONAL_FIELD) or [])
    if any(x.get("id") == item["id"] for x in items):
        return {"created": False, "reason": "同一条提案已存在（幂等）", "item": item}
    items.append(item)
    prof[PROVISIONAL_FIELD] = items
    digest = _write_profile_file(prof)
    return {"created": True, "item": item, "profile_sha256": digest,
            "provisional_count": len(items)}


def load_persona() -> Persona:
    if not PERSONALITY_FILE.exists():
        raise PersonaError(f"缺少人格文件：{PERSONALITY_FILE.as_posix()}")
    raw_bytes = PERSONALITY_FILE.read_bytes()
    # 容忍 BOM：手工用记事本编辑过后另存为「UTF-8 带 BOM」会让 json.loads 直接抛
    # JSONDecodeError，整个程序读不到人格。`utf-8-sig` 对**不带 BOM 的文件行为完全一致**，
    # 只在有 BOM 时把它剥掉。
    # ⚠️ 哈希仍按**原始字节**算（下一行 `hashlib.sha256(raw_bytes)`）——
    # 所以这里改解码方式**不会**让 sha256 守卫失效。
    data = json.loads(raw_bytes.decode("utf-8-sig"))

    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise PersonaError(f"人格文件缺少必要字段：{', '.join(missing)}")

    return Persona(data=data, source_sha256=hashlib.sha256(raw_bytes).hexdigest())


def assert_persona_unchanged(expected_sha256: str) -> None:
    """运行期守护：程序没有权限改人格，但用户手动改了也要立刻知道。

    人格一旦在实验中被改动，前后对话就不可比，所以这里选择直接报错停下，
    而不是静默重载。
    """
    if not PERSONALITY_FILE.exists():
        raise PersonaError("人格文件在运行期间消失了，已中止。")
    now = hashlib.sha256(PERSONALITY_FILE.read_bytes()).hexdigest()
    if now != expected_sha256:
        raise PersonaError(
            "人格文件在运行期间被修改（sha256 不一致）。\n"
            "为保证实验可比性，本次会话已中止。请重启程序重新开始记录。"
        )
