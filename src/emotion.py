"""最小情感状态模块（Phase D）。

## 设计来源

前置调研了 `companion-emergence` 的 `brain/emotion/`（7 子模块）。**只借了三样**：

  1. `Emotion` 是一张**数据表**（name / description / half_life_days）
  2. **半衰期是唯一的数值参数** —— 不搞 arousal / blend / 身体耦合
  3. `residue` 有界队列，每条带 `source`，使"这次变化从哪来"可追溯

**刻意没抄**：26 个情绪分类、7 档唤醒谱、共现混合、面部/声音表达、向量检索。

## 三条硬边界（机械保证，不是文档承诺）

| 边界 | 怎么保证 |
|---|---|
| 不能把关系分数直接转成情感 | 本模块**不 import `relationship`**（可 grep 验证） |
| 不能凭空生成经历 | 更新必须带**可核验**的 event_ref / turn seq，核验走 DB |
| 不能仅凭用户要求改情感 | 证据**全部**来自用户反馈 → 拒绝（复用 `self_model.detect_feedback`） |

## 不宣称主观感受

本模块是**数值状态的记账与衰减**。`dominant` 是"当前数值最高的那一项"，
不是"她此刻的心理体验"。任何把它当成主观情绪的说法都是过度解读。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import snapshots
from paths import EMOTION_DIR, ensure_dirs

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

INTENSITY_MIN = 0.0
INTENSITY_MAX = 10.0

# 低于此强度视为噪声，直接移除（借自 companion-emergence 的 _NOISE_FLOOR）
NOISE_FLOOR = 0.01

# residue 队列容量上限 —— 有界，防止无限累积
RESIDUE_CAPACITY = 20

# 更新来源。与 Phase B 的触发源对齐（chat/heartbeat），另加整理与手工。
SOURCE_CHAT = "chat"
SOURCE_HEARTBEAT = "heartbeat"
SOURCE_CONSOLIDATE = "consolidate"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_CHAT, SOURCE_HEARTBEAT, SOURCE_CONSOLIDATE, SOURCE_MANUAL)

_SNAP_PREFIX = "state_v"

SECONDS_PER_DAY = 86400.0


class EmotionError(RuntimeError):
    """情感模块的错误。与 relationship/self_model 同一风格（各自的错误类型）。"""


# ---------------------------------------------------------------------------
# 词表：7 条
# ---------------------------------------------------------------------------
#
# ⚠️ `half_life_days` 是**未经长期实测验证的种子设定** ——
#    来源是 companion-emergence 的同类口径（joy 3 天 / grief 60 天 /
#    love=None），按共生的词汇量级缩小后的初始猜测。
#    **它应当随真实交互数据调整**，现在没有任何数据支持它是对的。
#
# `None` = 不自动衰减（**不等于不可修改** —— 仍可由事件显式改变）。

@dataclass(frozen=True)
class Emotion:
    name: str
    description: str
    half_life_days: float | None


VOCABULARY: tuple[Emotion, ...] = (
    Emotion("calm", "心里是平的，没什么在推着自己", None),
    Emotion("fond", "对某个人或某件事有靠近的愿望", None),
    Emotion("glad", "事情顺了，轻快", 3.0),
    Emotion("low", "提不起劲，往下沉", 5.0),
    Emotion("uneasy", "有东西悬着，没落地", 2.0),
    Emotion("care", "把某件事放在心上，会惦记", 7.0),
    Emotion("tired", "被消耗掉了，需要停一停", 1.5),
)

_BY_NAME = {e.name: e for e in VOCABULARY}
_ORDER = {e.name: i for i, e in enumerate(VOCABULARY)}


def get(name: str) -> Emotion | None:
    return _BY_NAME.get(name)


def list_all() -> list[dict]:
    return [{"name": e.name, "description": e.description,
             "half_life_days": e.half_life_days} for e in VOCABULARY]


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return INTENSITY_MIN
    if f != f:  # NaN
        return INTENSITY_MIN
    return max(INTENSITY_MIN, min(INTENSITY_MAX, f))


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

@dataclass
class EmotionState:
    """当前情感状态。**所有修改都通过本模块的类型化方法**，
    绕过 clamp / 词表校验 / 容量上限在结构上做不到（字段只在方法里被写）。
    """
    emotions: dict[str, float] = field(default_factory=dict)
    residue: list[dict] = field(default_factory=list)
    updated_at: str = ""

    # ---- dominant：从当前有效状态**派生**，不存储 ----
    def dominant(self) -> str | None:
        """当前强度最高的情绪名。

        三种边界行为（明确定义，避免调用方各自解释）：

        | 情况 | 返回 |
        |---|---|
        | 无任何有效情感（空 / 全为 0 / 全低于噪声地板） | `None` |
        | 并列最高 | 按**词表顺序**取第一个（确定性，不随机） |
        | 词表外的残留项 | 忽略（只认词表内的） |
        """
        best: str | None = None
        best_v = NOISE_FLOOR
        for e in VOCABULARY:                    # 按词表顺序遍历 → 并列取先者
            v = _clamp(self.emotions.get(e.name, 0.0))
            if v > best_v:
                best, best_v = e.name, v
        return best

    def snapshot(self) -> dict:
        return {"emotions": dict(self.emotions), "dominant": self.dominant(),
                "residue_count": len(self.residue), "updated_at": self.updated_at}

    def to_dict(self) -> dict:
        return {"emotions": dict(self.emotions),
                "residue": list(self.residue),
                "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        """**宽容读取**：词表外的项丢弃，越界值 clamp，坏数据不抛错。

        这是降级保证的一部分 —— 旧文件、手改文件都不能让模块崩。
        """
        d = data or {}
        raw = d.get("emotions") or {}
        emo = {k: _clamp(v) for k, v in raw.items() if k in _BY_NAME}
        res = [r for r in (d.get("residue") or []) if isinstance(r, dict)]
        return cls(emotions=emo, residue=res[-RESIDUE_CAPACITY:],
                   updated_at=str(d.get("updated_at") or ""))


def empty_state() -> EmotionState:
    return EmotionState(emotions={k: 0.0 for k in _BY_NAME}, residue=[],
                        updated_at=_now_iso())


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 衰减：**纯函数**，输入状态输出新状态
# ---------------------------------------------------------------------------

def apply_decay(state: EmotionState, elapsed_seconds: float) -> EmotionState:
    """按每种情绪自己的半衰期做指数衰减，**返回新状态，不修改入参**。

    - `half_life_days=None` → 该情绪**不衰减**（仍可由事件显式改变）
    - 衰减后低于 `NOISE_FLOOR` → 移除该项（防残留累积）
    - `elapsed_seconds <= 0` → **静默 no-op**（返回等价副本）
    - **不产生新事件 · 不碰关系状态 · 不写人格 · 无模型调用 · 无外部副作用**
    """
    if elapsed_seconds is None or elapsed_seconds <= 0:
        return EmotionState(emotions=dict(state.emotions),
                            residue=list(state.residue),
                            updated_at=state.updated_at)

    elapsed_days = float(elapsed_seconds) / SECONDS_PER_DAY
    out: dict[str, float] = {}
    for e in VOCABULARY:
        v = _clamp(state.emotions.get(e.name, 0.0))
        if e.half_life_days is None:
            out[e.name] = v                      # 身份级：不衰减
            continue
        if v <= 0:
            continue
        decayed = v * math.pow(0.5, elapsed_days / float(e.half_life_days))
        if decayed >= NOISE_FLOOR:
            out[e.name] = decayed
    # 词表外的残留项一并丢弃（宽容收敛到词表）
    return EmotionState(emotions=out, residue=list(state.residue),
                        updated_at=_now_iso())


# ---------------------------------------------------------------------------
# 证据闸门
# ---------------------------------------------------------------------------

def verify_event_refs(conn, event_refs: list[str]) -> tuple[list[str], list[str]]:
    """核验引用是否真实存在。返回 `(有效, 无效)`。

    认两种引用（**都查 DB，不接受调用方自述**）：

      · `raw_event_xxx`  → `events.event_id`
      · `seq:123`        → `conversations.seq`

    ⚠️ 这是"不允许凭空生成经历"的机械保证：对不上的引用一律无效。
    """
    valid: list[str] = []
    invalid: list[str] = []
    for ref in event_refs or []:
        r = str(ref or "").strip()
        if not r:
            continue
        ok = False
        try:
            if r.startswith("raw_event_"):
                ok = conn.execute("SELECT 1 FROM events WHERE event_id = ? LIMIT 1",
                                  (r,)).fetchone() is not None
            elif r.startswith("seq:"):
                try:
                    n = int(r.split(":", 1)[1])
                except (IndexError, ValueError):
                    n = -1
                ok = n >= 0 and conn.execute(
                    "SELECT 1 FROM conversations WHERE seq = ? LIMIT 1", (n,)).fetchone() is not None
        except Exception:  # noqa: BLE001 — 查询失败按无效处理（宁可拒绝，不可放行）
            ok = False
        (valid if ok else invalid).append(r)
    return valid, invalid


def _rules_usable(rules) -> tuple[bool, str]:
    """规则是否**真的能驱动** `detect_feedback`。

    ## 校验依据是 `detect_feedback` 的**真实契约**，不是想当然的结构

        # self_model.detect_feedback 的实现只有这一行取规则：
        kws = (rules.get("feedback") or {}).get("keywords") or []
        return [k for k in kws if k and k in t]

    所以「可用」的充要条件是：`rules["feedback"]["keywords"]` 是**含至少一个
    非空字符串的列表**。除此之外的写法（多一层键、空 dict、空列表、字符串、
    只有 `_` 说明字段…）在 `detect_feedback` 眼里**一律等于没有关键词**，
    它会返回 `[]` → 所有证据都被判成"非反馈" → 闸门 fail-open。

    ## 为什么不做递归校验（Phase D.4 要求 5）

    盲目递归会假定"所有规则都长成同一种嵌套"。但 `detect_feedback` **只读
    `feedback.keywords` 这一条路径**，其它键（如 `_meaning`、`_design`、
    将来可能加的新分组）它根本不看。所以这里**只校验它真正读的那一个路径**，
    多一个子结构算不算"有效"不由我们臆断。

    ⚠️ Phase D.1/D.2 的旧实现只要求"feedback 里存在 ≥1 个非 `_` 键"——
    于是 `{"feedback": {"x": {}}}` 被判为可用，而它其实**没有任何关键词**，
    残余通道由此而来。D.4 按真实契约收紧。
    """
    if not isinstance(rules, dict):
        return False, f"规则不是字典（{type(rules).__name__}）"

    fb = rules.get("feedback")
    if fb is None:
        return False, "规则里没有 feedback 子字典"
    if not isinstance(fb, dict):
        return False, f"feedback 不是字典（{type(fb).__name__}）"

    if "keywords" not in fb:
        # 只有说明字段 / 换了别的键名 → detect_feedback 取不到关键词
        others = [k for k in fb if not str(k).startswith("_")]
        if others:
            return False, (f"feedback 里没有 keywords 键（只有 {others[:3]} 这类"
                           f"非说明字段）—— detect_feedback 只读 feedback.keywords")
        return False, "feedback 里只有说明字段（_ 前缀），没有 keywords"

    kws = fb.get("keywords")
    if not isinstance(kws, (list, tuple)):
        return False, f"keywords 不是列表（{type(kws).__name__}）"

    usable = [k for k in kws if isinstance(k, str) and k.strip()]
    if not usable:
        return False, "keywords 里没有可用的非空字符串关键词"
    return True, ""


def _feedback_refs(conn, refs: list[str], rules: dict) -> tuple[set[str], str]:
    """挑出**来源是用户反馈**的那些引用。返回 `(集合, 错误说明)`。

    复用 `self_model.detect_feedback`（同一套口径，不另立标准）。

    ⚠️ 与原实现的差别：**检测失败不再当作"没有反馈证据"**。

    原实现 `except: pass` 会让「检测器坏了」和「确实不是反馈」变成同一个结果，
    方向是 fail-open。现在改为返回错误说明，由 `update` **拒绝写入** ——
    对应 Phase D.2 要求 6：「不确定证据是否合格时拒绝写入；
    不得把验证失败解释为非反馈证据」。
    """
    out: set[str] = set()
    try:
        import self_model
    except Exception as exc:  # noqa: BLE001
        return set(), f"无法加载 self_model：{type(exc).__name__}: {exc}"

    for r in refs:
        try:
            if r.startswith("raw_event_"):
                row = conn.execute("SELECT user_text FROM events WHERE event_id = ?",
                                   (r,)).fetchone()
            else:
                row = conn.execute("SELECT user_text FROM conversations WHERE seq = ?",
                                   (int(r.split(":", 1)[1]),)).fetchone()
        except Exception as exc:  # noqa: BLE001 — 读不到原文 = 无法判定 = 拒绝
            return set(), f"读取 {r} 的原文失败：{type(exc).__name__}: {exc}"

        text = str((row[0] if row else "") or "")
        if not text.strip():
            return set(), f"{r} 取不到原文，无法判定是否为反馈"

        try:
            if self_model.detect_feedback(text, rules):
                out.add(r)
        except Exception as exc:  # noqa: BLE001 — 检测器异常 = 无法判定 = 拒绝
            return set(), f"detect_feedback 对 {r} 抛错：{type(exc).__name__}: {exc}"

    return out, ""


# ---------------------------------------------------------------------------
# 更新入口（唯一的写路径）
# ---------------------------------------------------------------------------

def update(state: EmotionState, *, changes: dict, source: str,
           event_refs: list[str], reason: str = "", conn=None,
           self_model_rules: dict | None = None) -> dict:
    """按**明确的事件输入**更新情感状态。返回结构化结果。

    ## 闸门（任一不过 → 拒绝，且不写任何东西）

    1. `source` 必须是 `SOURCES` 之一
    2. `changes` 非空，且名字必须在词表内
    3. `event_refs` 非空
    4. **引用必须能被 DB 核验**（`verify_event_refs`）
    5. **证据不能全部来自用户反馈** —— 否则那是"听他的话改心情"，不是情感变化
    6. **幂等**：同一批引用若已应用过，直接跳过（防重复应用）

    ## 重复 / 无效 / 空证据的安全处理

    | 情况 | 处理 |
    |---|---|
    | 空 `event_refs` | 拒绝（`empty_evidence`） |
    | 有无效引用 | **拒绝整次更新**（不"部分采纳"，避免用真引用掩护假引用） |
    | 同一引用重复提交 | 放行但当**重复应用**处理 → `duplicate` |
    | `changes` 全部越界为 0 | 仍放行（0 是合法值），但会记录 |
    """
    if source not in SOURCES:
        return {"ok": False, "status": "bad_source",
                "error": f"source 必须是 {SOURCES} 之一，收到 {source!r}"}

    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "status": "empty_changes", "error": "没有给出任何情感变化"}

    unknown = [k for k in changes if k not in _BY_NAME]
    if unknown:
        return {"ok": False, "status": "unknown_emotion",
                "error": f"词表里没有这些情感：{unknown}"}

    if not event_refs:
        return {"ok": False, "status": "empty_evidence",
                "error": "没有引用任何真实事件，拒绝更新"}

    if conn is None:
        return {"ok": False, "status": "no_conn",
                "error": "缺少数据库连接，无法核验事件引用"}

    # ⚠️ Phase D.1 抓到的真实缺陷：反馈闸门**不能因为调用方没传规则就失效**。
    #
    # 原实现是 `self_model_rules or {}` —— 传空字典时 `detect_feedback` 无关键词
    # 可匹配 → **所有证据都被判为非反馈 → 闸门静默放行**。这是 fail-open，
    # 而安全闸门的默认方向必须是 fail-safe。
    #
    # ⚠️ Phase D.1 抓到的真实缺陷（Phase D.2 完成修复）：反馈闸门**不能 fail-open**。
    #
    # 原实现是 `self_model_rules or {}` —— 传空字典时 `detect_feedback` 无关键词
    # 可匹配 → **所有证据都被判为非反馈 → 闸门静默放行**。
    # D.1 只把它标成 `no_rules` 并继续执行，**仍然放行**；D.2 改为**直接拒绝**。
    #
    # 规则从这个顺序拿：显式参数 → settings。**三条 fail-safe 出口**：
    #   读配置抛错        → 拒绝（load_failed）
    #   规则缺失/空/无关键词 → 拒绝（no_rules）
    #   反馈判定失败      → 拒绝（gate_error）
    gate_state = "rules_loaded"
    if self_model_rules is None:
        try:
            from config import load_settings
            self_model_rules = load_settings().self_model
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "no_rules", "feedback_gate": "load_failed",
                    "error": f"无法加载自我模型规则，拒绝写入情感状态："
                             f"{type(exc).__name__}: {exc}"}

    ok_rules, why = _rules_usable(self_model_rules)
    if not ok_rules:
        # ★ 显式传空规则 / 格式无效 / settings 里只有说明字段 —— 一律拒绝。
        # 「不允许显式传入空规则绕过默认安全策略」就是这一条。
        return {"ok": False, "status": "no_rules", "feedback_gate": "no_rules",
                "error": f"反馈闸门不可用，拒绝写入情感状态：{why}"}

    valid, invalid = verify_event_refs(conn, event_refs)
    if invalid:
        return {"ok": False, "status": "unverifiable_evidence",
                "feedback_gate": gate_state,
                "error": f"引用了不存在的事件，拒绝整次更新：{invalid[:3]}"}
    if not valid:
        return {"ok": False, "status": "empty_evidence", "feedback_gate": gate_state,
                "error": "没有可核验的引用"}

    fb, fb_err = _feedback_refs(conn, valid, self_model_rules)
    if fb_err:
        # ★ 不确定证据是否合格 → 拒绝；**绝不把它解释成"非反馈证据"**
        return {"ok": False, "status": "gate_error", "feedback_gate": gate_state,
                "error": f"无法判定证据是否为反馈，拒绝写入：{fb_err}"}
    if fb and len(fb) == len(valid):
        return {"ok": False, "status": "feedback_only",
                "feedback_gate": gate_state,
                "error": "证据全部来自用户反馈 —— 这属于自我纠正，不构成情感变化"}

    # 幂等：同一批引用已应用过
    applied_key = "|".join(sorted(valid))
    for r in state.residue:
        if r.get("_applied_key") == applied_key:
            return {"ok": False, "status": "duplicate",
                    "error": "这批事件已经应用过，跳过（防重复应用）",
                    "applied_key": applied_key}

    before = dict(state.emotions)
    after = dict(before)
    for k, delta in changes.items():
        # 允许两种写法：绝对量 {"calm": 4} 与增量 {"calm": "+2"} / {"calm": "-1"}
        s = str(delta).strip()
        if s.startswith(("+", "-")):
            after[k] = _clamp(after.get(k, 0.0) + float(s))
        else:
            after[k] = _clamp(delta)

    entry = {
        "timestamp": _now_iso(),
        "source": source,
        "emotions": {k: after[k] for k in changes},
        "reason": str(reason or "").strip(),
        "event_refs": list(valid),
        "before": before,
        "after": after,
        "changes": {k: after[k] - _clamp(before.get(k, 0.0)) for k in changes},
        "_applied_key": applied_key,
    }

    residue = (list(state.residue) + [entry])[-RESIDUE_CAPACITY:]
    new_state = EmotionState(emotions=after, residue=residue, updated_at=_now_iso())
    return {"ok": True, "status": "applied", "state": new_state,
            "record": entry, "dominant": new_state.dominant(),
            "verified_refs": valid, "feedback_refs": sorted(fb),
            "feedback_gate": gate_state}


# ---------------------------------------------------------------------------
# 持久化：复用 Phase A 抽出的 snapshots（版本化，只追加）
# ---------------------------------------------------------------------------

def snapshot_path(version: int):
    return snapshots.path_for(EMOTION_DIR, _SNAP_PREFIX, version)


def existing_snapshots() -> list[int]:
    return snapshots.scan_versions(EMOTION_DIR, prefix=_SNAP_PREFIX, ensure=False)


def _elapsed_seconds(iso: str, *, now: datetime | None = None) -> float | None:
    """从 ISO 时间戳算到现在的秒数。**任何异常都返回 None**（调用方据此跳过衰减）。

    容错三种坏数据（都不该让"读状态"失败）：
      · 空串 / None
      · 无法解析的字符串
      · 无时区的时间（按**本地**时区补，而不是当 UTC —— 否则会差 8 小时）
    """
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.astimezone()          # naive → 按本地时区解释
    n = now or datetime.now(timezone.utc).astimezone()
    if n.tzinfo is None:
        n = n.astimezone()
    try:
        return (n - t).total_seconds()
    except (TypeError, ValueError):
        return None


def load_state(*, decay: bool = True) -> EmotionState | None:
    """读最新一份状态。**任何失败都返回 None**（降级：调用方照常工作）。

    ⚠️ 落盘写的是外层 payload `{version, created_at, note, state:{...}}`，
    所以这里必须**先解出 `state` 再交给 `from_dict`**。
    （Phase D.1 隔离验证抓到的真实缺陷：原实现把整个 payload 传进去，
    读到的是空状态 —— save→load 往返根本不一致。）

    ## 衰减接在这里（Phase E2 工作线 A）

    `apply_decay` 此前**没有任何调用者** —— 半衰期规则写了却从不生效。
    接入点选**读时**，理由三条：

    1. **只有一个地方**，不会出现"这条路径衰减了、那条没衰减"
    2. **幂等**：衰减量由**存储的 `updated_at`** 算出，与读几次无关 ——
       读两遍得到同一结果，**不会重复衰减**
    3. **不写放大**：读是高频操作，衰减结果**不落盘**；
       真正落盘只在 `save_state`，那时会盖上新的 `updated_at`

    `decay=False` 供需要"原样读回"的场景（往返一致性测试）使用。
    """
    try:
        vs = existing_snapshots()
        if not vs:
            return None
        p = snapshot_path(vs[-1])
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        # 兼容两种形态：带 state 外层的新格式；以及只有状态字段的裸格式
        inner = payload.get("state") if "state" in payload else payload
        if not isinstance(inner, dict):
            return None
        st = EmotionState.from_dict(inner)
        if decay:
            elapsed = _elapsed_seconds(st.updated_at)
            if elapsed is not None and elapsed > 0:
                st = apply_decay(st, elapsed)
        return st
    except Exception:  # noqa: BLE001 — 读取失败=没有状态，绝不让上层崩
        return None


def save_state(state: EmotionState, *, note: str = "") -> dict:
    """落一份**只追加**的状态快照（沿用 snapshots 的撞号策略）。

    ⚠️ `ensure_dirs()` 只建 `paths.py` 里登记的目录。**本函数额外自己保证
    `EMOTION_DIR` 存在** —— 否则一旦目录被重定向（隔离测试）或注册遗漏，
    写文件会以 `FileNotFoundError` 崩溃。写入失败一律转成 `EmotionError`，
    **不静默吞掉**（宁可显式失败，也不要假装写成功）。
    """
    ensure_dirs()
    try:
        EMOTION_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — 目录建不出来就明确报错
        raise EmotionError(f"无法建立情感状态目录 {EMOTION_DIR}：{exc}") from exc

    # ⚠️ Phase D.4：`updated_at` 为空时**落盘补上**（此前会原样写入空串）。
    #
    # 为什么改：`update()` / `empty_state()` / `apply_decay()` 产出的状态都自带时间戳，
    # 但**手工构造**的 `EmotionState(emotions=...)` 没有。原实现会把它原样写盘，
    # 结果 `updated_at` 是空串 —— 消费方拿到后没有任何时间参照，
    # 属于"行为不明确"。落盘本身就是一次更新，所以这里补当前时间。
    #
    # 只补空值，**不覆盖已有时间戳**（那会破坏"往返一致"）。
    if not state.updated_at:
        state = EmotionState(emotions=dict(state.emotions),
                             residue=list(state.residue),
                             updated_at=_now_iso())

    version, path, status = snapshots.resolve_version(
        EMOTION_DIR, prefix=_SNAP_PREFIX, seek=True
    )
    if status == "exhausted":
        raise EmotionError(f"连续 {snapshots.SCAN_LIMIT} 个快照号被占用：{path.name}")

    payload = {"version": f"v{version:03d}", "created_at": _now_iso(),
               "note": note, "state": state.to_dict()}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = snapshots.digest_of(body)
    path.write_text(body + "\n", encoding="utf-8")
    (EMOTION_DIR / f"{_SNAP_PREFIX}{version:03d}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return {"version": f"v{version:03d}", "file": f"emotion/{path.name}",
            "sha256": digest}


# ---------------------------------------------------------------------------
# 表达出口：只给偏置，不覆盖任何东西
# ---------------------------------------------------------------------------

def bias_hint(state: EmotionState | None, *, rules: dict | None = None) -> str:
    """把情感状态转成**一段可注入的表达偏置**。无状态时返回空串。

    ## 边界

    - 只影响**表达**，不改人格 / 用户模型 / 关系阶段 / 稳定字段
    - 明确声明这是**数值状态的偏置**，不是"她此刻真的感受到了什么"
    - 无状态 / 全零 → 返回空串 → 调用方行为与没有本模块时**逐字一致**
    """
    if state is None:
        return ""
    dom = state.dominant()
    if dom is None:
        return ""
    e = get(dom)
    if e is None:
        return ""
    v = _clamp(state.emotions.get(dom, 0.0))
    strength = "明显" if v >= 6 else ("有一些" if v >= 3 else "轻微")
    return (
        f"当前状态偏置（由数值派生的表达倾向，**不是你此刻真实感受的断言**）："
        f"「{e.name}」（{e.description}）强度{strength}。"
        "可以让语气受它影响，但**不要把它当台词说出来**，"
        "也不要因为它改变你对事情的判断。"
    )