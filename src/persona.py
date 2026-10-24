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
                                     concerns: list | None = None) -> str:
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
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _fmt_list(items) -> str:
    if isinstance(items, str):
        return items
    if isinstance(items, (list, tuple)):
        return "；".join(str(x).strip() for x in items if str(x).strip())
    return ""


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
    return "\n".join(lines)


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
    return {"file": PROFILE_FILE.name, "sha256": digest}


def load_persona() -> Persona:
    if not PERSONALITY_FILE.exists():
        raise PersonaError(f"缺少人格文件：{PERSONALITY_FILE.as_posix()}")
    raw_bytes = PERSONALITY_FILE.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))

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
