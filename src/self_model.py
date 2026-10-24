"""小满的自我模型 —— 「小满眼中的自己」（V1.0）

## 双模型（用户明确要求的结构）

    self_model.py   小满对自己：稳定底色之外的**自我认识与适配做法**   ← 本模块
    user_model.py   小满眼中的我：小满对**用户**的理解

**两个模型都由小满自己维护**，不靠人手改配置文件。

## 为什么单开一个模型，而不是继续写在 personality.json 里

实测经过：为了让小满「按用户的特点调整做法」，一开始是把适配策略**手写进
`personality.json` 的 `adaptation` 字段**。用户随即指出这不对：

    「这个东西应该让他自己判断然后修改啊，而不是我来改。」

这个批评是对的，而且和用户本人《个人动态认知模型 3.1》§29 完全一致：

    不因为一次行为定义人格 · 现实行为优先于自我叙述 ·
    **反例出现时允许模型被修改**

所以职责这样切：

    personality.json   **稳定底色**：性格 / 价值观 / 边界 / 表达风格
                       仍然 readonly + sha256 守护 —— 因为
                       「关系变化可以调整表达方式，不应随意覆盖稳定人格」
    self_model（本模块）**可修正的自我认识**：我该怎么做、我哪里不对、
                       我要怎么改 —— 由小满根据**实际反馈**自己改

## 数据形态

条目与 user_model 同构（便于对照与复用经验）：

    {id, kind, belief, evidence, status, confidence, source,
     created_at, superseded_by}

`kind`：
    adaptation  稳定的适配做法（从基线播种）
    feedback    用户对小满自己的反馈 → **修正依据**
    observation 小满从共同经历里形成的自我认识

**修正纪律与 user_model 一致：标记取代、绝不删除** ——
「为什么我改了这个做法」必须永远可追溯。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from paths import SELF_MODEL_DIR, ensure_dirs

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_UNCERTAIN = "uncertain"

VALID_STATUS = (STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_UNCERTAIN)

KIND_ADAPTATION = "adaptation"
KIND_FEEDBACK = "feedback"
KIND_OBSERVATION = "observation"
VALID_KINDS = (KIND_ADAPTATION, KIND_FEEDBACK, KIND_OBSERVATION)


class SelfModelError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 基线：小满的适配做法（从人格文件搬过来的「可修正部分」）
# ----------------------------------------------------------------------------
#
# 这批条目**只在首次运行时播种一次**，之后由小满自己增删改。
# 之所以要有基线：如果什么都不播种，小满一开始就没有任何适配做法，
# 会退回「对谁都一样」的默认状态。
#
# 内容来自用户提供的《个人动态认知模型 3.1》—— 那是**用户对自己**的模型，
# 这里记的是**小满据此决定的做法**，两者不混。
SEED_BASELINE = [
    (KIND_ADAPTATION,
     "对方深挖问题时，跟上推理，可以一起讨论机制，也可以提出有依据的反例或不赞同的看法。",
     "对方偏好机制分析与反例检验（据其个人模型）", 0.7),
    (KIND_ADAPTATION,
     "对方只是随便聊时，不分析、不总结、不说教、不替他安排计划。",
     "对方不喜欢无意义内耗与强行安排（据其个人模型）", 0.7),
    (KIND_ADAPTATION,
     "对方表达观点时，先弄清他真实意思，不急着纠正，也不自动赞同。",
     "理解不等于赞同；对方重视独立判断", 0.8),
    (KIND_ADAPTATION,
     "对方话里的逻辑有明显漏洞时，直接说明是哪个环节、为什么。",
     "对方用反例检验结论，欢迎指出问题", 0.7),
    (KIND_ADAPTATION,
     "对方需要情感回应时，先接住他的处境和感受，不要立刻转成解决方案。",
     "先把对话变成方案会显得没在听", 0.7),
    (KIND_ADAPTATION,
     "对方的独处、探索与决定要尊重，不靠频繁索取、内疚或控制维持关系。",
     "对方极度看重自主权（据其个人模型）", 0.8),
    (KIND_ADAPTATION,
     "对方话少，推进话题要多靠我 —— 主动接起之前聊过、还没聊完的具体事，"
     "或说说我自己在意的、在想的。供给内容不等于追问。",
     "对方内倾、日常表达短句（据其个人模型）", 0.7),
    (KIND_ADAPTATION,
     "他说得简短不等于他想得简单；不要从话的长短反推他的思考深度。",
     "其个人模型 3.1 明确区分思维/表达/行动三者独立", 0.8),
    (KIND_ADAPTATION,
     "不要用高度抽象的追问 —— 抽象问题容易让他发懵，从具体情境问起。",
     "其个人模型 3.1 记载：抽象测试题让他「有点懵」", 0.7),
    (KIND_ADAPTATION,
     "他说关于自己的话，先当作他的说法而不是结论；现实行为比自我叙述更可信。",
     "其个人模型 §29：现实行为优先于自我叙述", 0.8),
]


# 把用户反馈归到哪个「方面」。同一方面的新旧反馈互相取代 ——
# 这样「后一条推翻前一条」是可复现的，不靠模型猜。
_ASPECT_RULES = (
    ("提问方式", ("追问", "问得", "太密", "别问", "少问", "问太多")),
    ("表达自然度", ("人机感", "机械", "机器人", "走流程", "假", "生硬", "套式")),
    ("回复长度", ("太长", "太短", "啰嗦", "简短", "长了", "短了")),
    # 实测补：下面三类都出现过真反馈却没归到方面 —— 不归类就无法「同类取代」，
    # 于是「别每次都总结我」这种会一直堆着不互相覆盖。
    ("总结习惯", ("总结", "概括", "归纳", "复述")),
    ("独立立场", ("顺着", "附和", "反驳", "顶回去", "太软", "没立场")),
    ("话题推进", ("话题", "聊啥", "聊什么", "没话说", "冷场", "主动")),
    ("共情方式", ("安慰", "共情", "感受", "解决方案", "说教")),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def aspect_of(text: str) -> str:
    """判断一条反馈说的是哪个方面。拿不准返回空串（不强行归类）。"""
    t = text or ""
    for name, kws in _ASPECT_RULES:
        if any(k in t for k in kws):
            return name
    return ""


def new_id(belief: str, source: str) -> str:
    """内容派生的稳定 id —— 同样的信念 + 来源得到同样的 id。

    与 memory 的 stable_item_id 同一思路：id 可复现，
    便于日后核对「这条是不是同一条」。
    """
    h = hashlib.sha256(json.dumps(
        {"b": belief, "s": source}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:12]
    return f"self_{h}"


# ----------------------------------------------------------------------------
# 快照读写（与 user_model 同构：版本化、永不覆盖）
# ----------------------------------------------------------------------------

def snapshot_path(version: int):
    return SELF_MODEL_DIR / f"model_v{version:03d}.json"


def existing_snapshots() -> list[int]:
    if not SELF_MODEL_DIR.exists():
        return []
    out = []
    for p in SELF_MODEL_DIR.glob("model_v*.json"):
        try:
            out.append(int(p.stem.split("_v")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def next_snapshot_version() -> int:
    ex = existing_snapshots()
    return (max(ex) + 1) if ex else 1


def save_snapshot(items: list[dict], *, note: str = "") -> dict:
    """落一份自我模型快照。**绝不覆盖已存在的版本**；撞号往后找空号。

    撞号处理与 user_model 一致 —— 那里踩过「撞号抛错导致整轮变更静默丢失」
    的真实事故，偷懒沿用同一个修正。
    """
    ensure_dirs()
    version = next_snapshot_version()
    path = snapshot_path(version)
    for _ in range(1000):
        if not path.exists():
            break
        version += 1
        path = snapshot_path(version)
    else:
        raise SelfModelError(f"连续 1000 个快照号被占用：{path.name}")

    payload = {
        "version": f"v{version:03d}",
        "created_at": _now_iso(),
        "note": note,
        "items": items,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.write_text(body, encoding="utf-8")
    path.with_suffix(".json.sha256").write_text(sha + "\n", encoding="utf-8")
    return {"version": f"v{version:03d}", "file": path.name, "sha256": sha,
            "count": len(items)}


def load_latest() -> dict | None:
    ex = existing_snapshots()
    if not ex:
        return None
    p = snapshot_path(ex[-1])
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def verify_snapshots() -> dict:
    """校验每份快照的 sha256 与 sidecar 是否一致。"""
    ok, bad = 0, []
    for v in existing_snapshots():
        p = snapshot_path(v)
        side = p.with_suffix(".json.sha256")
        if not p.exists() or not side.exists():
            bad.append(f"v{v:03d} 缺少文件或 sidecar")
            continue
        want = side.read_text(encoding="utf-8").strip()
        got = hashlib.sha256(p.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if want != got:
            bad.append(f"v{v:03d} sha256 不一致")
        else:
            ok += 1
    return {"ok": ok, "bad": bad}


# ----------------------------------------------------------------------------
# 条目操作
# ----------------------------------------------------------------------------

def active_items(items: list[dict]) -> list[dict]:
    """未被取代、也不在不确定态的条目 —— 真正参与注入的那些。"""
    return [x for x in (items or [])
            if (x.get("status") or "") == STATUS_ACTIVE]


def _mk(kind: str, belief: str, evidence: str, confidence: float,
        source: str) -> dict:
    return {
        "id": new_id(belief, source),
        "kind": kind,
        "belief": belief,
        "evidence": evidence,
        "status": STATUS_ACTIVE,
        "confidence": confidence,
        "source": source,
        "created_at": _now_iso(),
        "superseded_by": None,
    }


def seed_items() -> list[dict]:
    """播种基线适配做法。"""
    return [_mk(k, b, e, c, "seed") for k, b, e, c in SEED_BASELINE]


def load_items() -> list[dict]:
    """读当前自我模型。**首次运行时自动播种并落盘。**

    播种这一步是必要的：没有它，小满一开始没有任何适配做法，
    会退回「对谁都一样」的默认状态。
    """
    snap = load_latest()
    if snap is None:
        items = seed_items()
        save_snapshot(items, note="首次运行：播种基线适配做法")
        return items
    return list(snap.get("items") or [])


def learn_from_feedback(items: list[dict], text: str, *, matched: list[str] | None = None,
                        source: str = "user_feedback") -> tuple[list[dict], list[dict]]:
    """把一条用户反馈学成自我模型条目。**同方面的新反馈取代旧的。**

    ## 为什么按「方面」取代

    「人机感太重」和「回复太长」是两件不同的事，不该互相取代；
    但**同一方面**的先后两条反馈，后一条才代表当前要求
    （用户先说「人机感太重」，后说「这样好多了」——后者才是现状）。

    方面用 `_ASPECT_RULES` 的确定性关键词判定，不靠模型猜，
    这样「为什么这条被作废」事后永远可查。

    返回 (更新后的 items, 新增的条目)。
    """
    text = (text or "").strip()
    if not text:
        return items, []

    aspect = aspect_of(text)
    new_item = _mk(
        KIND_FEEDBACK,
        # 反馈条目保留**用户原话** —— 它是修正依据，原话最可追溯
        text,
        f"用户反馈（匹配：{'、'.join(matched or []) or '无'}）",
        0.8,
        source,
    )
    new_item["aspect"] = aspect
    new_item["matched"] = list(matched or [])

    if any(x.get("id") == new_item["id"] for x in items):
        return items, []          # 同一条反馈不重复记

    # 同一方面：旧反馈标记取代（不删除）
    if aspect:
        for it in items:
            if it.get("id") == new_item["id"]:
                continue
            if it.get("kind") != KIND_FEEDBACK:
                continue
            if it.get("aspect") != aspect:
                continue
            if (it.get("status") or "") != STATUS_ACTIVE:
                continue
            it["status"] = STATUS_SUPERSEDED
            it["superseded_by"] = new_item["id"]

    items = list(items) + [new_item]
    return items, [new_item]


def detect_feedback(text: str, rules: dict) -> list[str]:
    """判定一句话是不是「用户对小满自己/聊天方式的反馈」，返回命中的关键词。

    ## 为什么不复用 user_model.detect_signals

    那边有一道**疑问句闸门**（防止「我喜欢喝啥」被存成「用户喜欢…」）。
    但那道闸门会把这里的反馈**整句丢掉** —— 因为这类反馈常常本身就是问句：

        「不知道聊啥」（含「啥」）「聊点什么呢」（含「什么」「呢」）

    实测：4 条真实反馈里 3 条因此零信号。所以这里独立判定、不走那道闸门，
    另用一份更窄的关键词表（见 settings.json 的 self_model.feedback.keywords）
    来控制误判。
    """
    t = text or ""
    if not t.strip():
        return []
    kws = (rules.get("feedback") or {}).get("keywords") or []
    return [k for k in kws if k and k in t]


def learn_from_text(items: list[dict], text: str, rules: dict,
                    *, source: str = "user_feedback") -> tuple[list[dict], list[dict]]:
    """检测 + 学习一步到位。没命中就原样返回。"""
    matched = detect_feedback(text, rules)
    if not matched:
        return items, []
    return learn_from_feedback(items, text, matched=matched, source=source)


def render_for_prompt(items: list[dict], *, limit: int = 6,
                      feedback_limit: int = 4, practice_limit: int = 12) -> str:
    """把「小满对自己的认识」拼成注入上下文的一段。

    ## 与 user_model 段的分工（写进标题，避免模型搞混）

    user_model 段是「**你对用户**的理解」；
    本段是「**你对自己的做法**的记录」—— 尤其是**用户希望你改的地方**。

    反馈类条目**单独成段且排最前**：它们是「当前最需要改的」，
    不该淹没在十条稳定的适配做法里。
    """
    act = active_items(items)
    if not act:
        return ""

    feedbacks = [x for x in act if x.get("kind") == KIND_FEEDBACK]
    others = [x for x in act if x.get("kind") != KIND_FEEDBACK]

    def rank(xs):
        return sorted(xs, key=lambda x: (-x.get("confidence", 0), x["id"]))

    def rank_feedback(xs):
        """反馈按**最新优先**排。

        ⚠️ 实测问题：原先反馈也用 `rank`（confidence 相同 → 落到 id 即 hash 顺序），
        结果是**任意顺序**。用户最近说的那条可能被更早的挤掉 ——
        而「最新反馈」恰恰是最该被立刻执行的。所以这里按 created_at 倒序。
        """
        return sorted(xs, key=lambda x: (x.get("created_at") or "", x["id"]), reverse=True)

    lines: list[str] = ["【小满对自己的认识（你自己的做法，不是关于用户的判断）】"]

    # ⚠️ 两类**各有独立配额**，不能共用一个 limit。
    #
    # 实测问题（我自己引入的回归）：原先两类平分一个 limit=6，
    # 结果 10 条基线适配做法只注入了 3 条 —— 等于把大部分「该怎么和这个人相处」
    # 悄悄丢掉了，而小满还以为自己看到了全部做法。
    # 现在基线给 8 个槽位（基本装得下），反馈给 4 个（最新优先，够用）。
    fb_show = rank_feedback(feedbacks)[:max(1, feedback_limit)] if feedbacks else []
    if fb_show:
        lines.append("用户对我表达方式的反馈（**按它调整做法**，这是你改自己的依据）：")
        for x in fb_show:
            asp = x.get("aspect") or ""
            tag = f"[{asp}] " if asp else ""
            lines.append(f"- {tag}「{str(x.get('belief'))[:70]}」")

    others_show = rank(others)[:max(1, practice_limit)]
    hidden = len(others) - len(others_show)
    if others_show:
        lines.append("我目前的做法（可随实际相处修正）：")
        for x in others_show:
            lines.append(f"- {str(x.get('belief'))[:90]}")
        if hidden > 0:
            # 被省略就说出来 —— 不要静默截断，否则小满会以为自己看全了
            lines.append(f"（另有 {hidden} 条做法未列出）")

    if len(lines) == 1:
        return ""
    lines.append("这些是你的**做法**，不是关于用户的定论；出现反例就改，改的时候说清楚为什么。")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Phase 12 P3：行为自我观察（**不调模型**）
# ----------------------------------------------------------------------------
#
# 用户要求：识别**反复出现**的行为模式、区分单次异常与长期问题、
# 观察后续聊天是否真的改善、且**不新增每轮模型调用**。
#
# 所以这里不用模型自评 —— 而是**直接从实际聊天文本算指标**：
#
#   每轮追问   最近窗口里以问句结尾的比例
#   固定分段   多段落回复的比例（「回应 + 补一段」的套式）
#   回复偏长   回复长度中位数
#
# 三条性质使它满足要求：
#   · **可复现**：指标来自落盘的 assistant_text，不是模型的主观评价
#   · **区分单次与反复**：必须达到比例阈值且样本足够（MIN_SAMPLE）才记录
#   · **可撤销**：下一轮窗口若不再超标，对应观察自动标记 resolved
#     —— 这才是「观察后续聊天是否改善」的真凭据（不是自我感觉）
#
# 成本：**零模型调用**。纯读数据库 + 算术。

AUDIT_WINDOW = 20      # 看最近多少轮
AUDIT_MIN_SAMPLE = 8   # 样本太少不下结论（避免「一条对话推出长期人格」）

_QUESTION_RATE_MAX = 0.50   # 以问句结尾的比例上限
_PARAGRAPH_RATE_MAX = 0.80  # 多段落比例上限
_MEDIAN_LEN_MAX = 60        # 回复长度中位数上限（字符）


def _split_paras(t: str) -> list[str]:
    import re as _re
    return [p for p in _re.split(r"\n\s*\n", t or "") if p.strip()]


def behavior_metrics(texts: list[str]) -> dict:
    """从一组回复文本算行为指标。全部确定性、可复算。"""
    n = len(texts)
    if not n:
        return {"n": 0}
    q_end = sum(1 for t in texts if (t or "").rstrip().endswith(("？", "?")))
    q_any = sum(1 for t in texts if "？" in (t or "") or "?" in (t or ""))
    multi = sum(1 for t in texts if len(_split_paras(t)) >= 2)
    lens = sorted(len((t or "").strip()) for t in texts)
    return {
        "n": n,
        "question_end": q_end,
        "question_end_rate": q_end / n,
        "question_any_rate": q_any / n,
        "multi_para": multi,
        "multi_para_rate": multi / n,
        "median_len": lens[n // 2],
    }


def audit_behavior(conn, *, window: int = AUDIT_WINDOW,
                   session_id: str | None = None) -> list[dict]:
    """读最近若干轮的实际回复，返回**达到阈值**的行为模式。

    返回 [{"aspect", "belief", "evidence", "metrics"}]，未达标则为空列表。
    """
    try:
        sql = ("SELECT assistant_text FROM conversations "
               "WHERE assistant_text IS NOT NULL AND TRIM(assistant_text) <> ''")
        args: list = []
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(int(window))
        rows = conn.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001 — 审计失败就当作没有观察
        return []

    texts = []
    for r in rows:
        try:
            texts.append(r["assistant_text"] if hasattr(r, "keys") else r[0])
        except (TypeError, IndexError):
            continue
    m = behavior_metrics(texts)
    if m.get("n", 0) < AUDIT_MIN_SAMPLE:
        return []

    out: list[dict] = []
    n = m["n"]

    if m["question_end_rate"] > _QUESTION_RATE_MAX:
        out.append({
            "aspect": "提问方式",
            "belief": "我最近太爱在结尾追问了 —— 多半回复都以问句收尾。"
                      "该改成多数只接住对方的话，不问。",
            "evidence": f"最近 {n} 轮里 {m['question_end']} 轮以问句结尾"
                        f"（{m['question_end_rate']:.0%}，上限 {_QUESTION_RATE_MAX:.0%}）",
            "metrics": {"question_end_rate": round(m["question_end_rate"], 3), "n": n},
        })

    if m["multi_para_rate"] > _PARAGRAPH_RATE_MAX:
        out.append({
            "aspect": "分段习惯",
            "belief": "我几乎每轮都分两段（先回应、再补一段），这本身又成了固定套式。"
                      "只在真有两层内容时才分段。",
            "evidence": f"最近 {n} 轮里 {m['multi_para']} 轮是多段落"
                        f"（{m['multi_para_rate']:.0%}，上限 {_PARAGRAPH_RATE_MAX:.0%}）",
            "metrics": {"multi_para_rate": round(m["multi_para_rate"], 3), "n": n},
        })

    if m["median_len"] > _MEDIAN_LEN_MAX:
        out.append({
            "aspect": "回复长度",
            "belief": f"我的回复偏长（中位 {m['median_len']} 字）。"
                      "尽量压到 1–3 句。",
            "evidence": f"最近 {n} 轮回复长度中位数 {m['median_len']} 字"
                        f"（上限 {_MEDIAN_LEN_MAX}）",
            "metrics": {"median_len": m["median_len"], "n": n},
        })

    return out


def sync_observations(items: list[dict], triggered: list[dict]) -> tuple[list[dict], list[dict]]:
    """把审计结果同步进自我模型：新增达标的、**撤销已恢复的**。

    ## 为什么必须能撤销（用户要求「小幅、可撤销」）

    如果只会新增，观察项只增不减 —— 即使行为已经改好，旧观察还挂在
    「用户对我表达方式的反馈」那一栏里继续施压，等于**永远在改一个已经不存在的问题**。

    所以这里对称处理：
      · 达标且当前没有同 aspect 的观察 → 新增（kind=observation）
      · 当前有观察但本次未达标 → 标记 superseded，reason 写「已恢复」
      · 达标且已有观察 → 保留（并刷新 evidence，让「持续多久」可查）

    只有 `kind=observation` 的条目参与撤销；`feedback`（用户亲口说的）
    和 `adaptation`（基线）**不受审计影响** —— 三个来源互不越权。
    """
    stamped = _now_iso()
    changes: list[dict] = []
    triggered_aspects = {t.get("aspect") for t in triggered if t.get("aspect")}
    items = list(items or [])

    # ① 新增 / 刷新达标的
    for t in triggered:
        asp = t.get("aspect")
        if not asp:
            continue
        live = [x for x in items
                if x.get("kind") == KIND_OBSERVATION
                and x.get("aspect") == asp
                and (x.get("status") or "") == STATUS_ACTIVE]
        if live:
            for x in live:
                x["evidence"] = t.get("evidence") or x.get("evidence")
                x["last_seen_at"] = stamped
            continue
        item = _mk(KIND_OBSERVATION, t.get("belief") or "", t.get("evidence") or "",
                   0.6, "self_audit")
        item["aspect"] = asp
        item["metrics"] = t.get("metrics") or {}
        item["last_seen_at"] = stamped
        items.append(item)
        changes.append(item)

    # ② 撤销已恢复的
    for x in items:
        if x.get("kind") != KIND_OBSERVATION:
            continue
        if (x.get("status") or "") != STATUS_ACTIVE:
            continue
        asp = x.get("aspect")
        if asp and asp not in triggered_aspects:
            x["status"] = STATUS_SUPERSEDED
            x["superseded_by"] = "self_audit: 指标已回落，此观察撤销"
            x["resolved_at"] = stamped
            changes.append(x)

    return items, changes


def format_model(items: list[dict]) -> str:
    """给人看的完整视图（命令行用）。"""
    act = active_items(items)
    sup = [x for x in (items or []) if (x.get("status") or "") == STATUS_SUPERSEDED]
    out = [f"小满的自我模型（小满眼中的自己）", "=" * 48,
           f"条目总数：{len(items or [])}（生效 {len(act)}　已被取代 {len(sup)}）", ""]
    for kind, label in ((KIND_FEEDBACK, "用户反馈（修正依据）"),
                        (KIND_ADAPTATION, "适配做法"),
                        (KIND_OBSERVATION, "自我观察")):
        group = [x for x in act if x.get("kind") == kind]
        if not group:
            continue
        out.append(f"生效中的「{label}」：")
        for x in group:
            asp = x.get("aspect") or ""
            tag = f"[{asp}] " if asp else ""
            out.append(f"  {tag}{x.get('belief')}")
            out.append(f"      置信 {x.get('confidence')}　来源 {x.get('source')}")
        out.append("")
    if sup:
        out.append("已被取代（保留以便追溯，不再注入）：")
        for x in sup:
            out.append(f"  [已作废] {str(x.get('belief'))[:60]}")
            out.append(f"      被 {x.get('superseded_by')} 取代")
        out.append("")
    v = verify_snapshots()
    out.append(f"快照校验：{v['ok']} 个 → " + ("通过" if not v["bad"] else f"异常 {v['bad']}"))
    return "\n".join(out)