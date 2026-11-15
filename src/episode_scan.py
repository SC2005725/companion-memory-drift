"""处方 A · 情绪峰值检测 → EPISODE 候选（2026-11-08，老板拍板「整」）。

病根（docs/关系情绪人格优化调研-20261108.md §一）：核心记忆全是 FACT 型
工程事实，没有一段"带温度的经历"可引用——人机感的根因在记忆成分。
本通道扫描**新对话**，把符合信号的轮次提炼成 EPISODE 型记忆：
  · 强烈情绪词（开心/难过/委屈/感动/破防/担心…）
  · 深夜长谈（本地 23:00-05:00 连续多轮）
  · 共同完成一件事（搞定/成了/修好了/跑通了…）
  · 争执与和解（对不起/原谅/别生气/和好…）

提炼走**现成的候选提案制**（propose_memory_candidates → 整理员按条目粒度
接纳进核心记忆），零新建管线；整理提示词一字不动（实验基线不漂）。

诚实边界（本模块的措辞纪律）：
  · 文本是**程序拼装的原话节选**，不是 LLM 改写——不编造"当时的感受"，
    温度由原话本身与信号标注承载。要不要上模型加工，等处方 D 盲测数据说话。
  · 每轮扫描最多产出 MAX_EPISODES_PER_RUN 条提案，防止一次回填灌爆候选池。
  · 首扫只回看最近 MAX_TURNS_FIRST_SCAN 轮——处方语义是"新对话"，不做全史回填。
"""
from __future__ import annotations

from datetime import datetime

import companion_core as core

# ---- 信号词表（宁缺毋滥：命中即提炼，误报由整理员的重复/冲突判定兜底）----
EMOTION_WORDS = (
    "开心", "开心死", "高兴", "难过", "难受", "委屈", "生气", "气死",
    "感动", "破防", "崩溃", "害怕", "担心", "想你", "舍不得", "心疼",
    "焦虑", "紧张", "踏实", "放松", "无聊死", "爽", "治愈", "委屈死",
)
ACCOMPLISH_WORDS = ("搞定", "弄好", "成了", "修好", "跑通", "上线", "发布", "清零")
RECONCILE_WORDS = ("对不起", "原谅", "别生气", "和好", "是我不好", "怪我")
LATE_NIGHT_HOURS = frozenset({23, 0, 1, 2, 3, 4})

MAX_EPISODES_PER_RUN = 5      # 单轮扫描的提案上限
MAX_TURNS_FIRST_SCAN = 200    # 首扫回看窗口（轮）
MAX_TURNS_PER_SCAN = 400      # 单轮扫描的读取上限
WINDOW_SEQ_GAP = 4            # 相邻信号轮 seq 差 ≤4 视为同一段经历
WINDOW_TIME_HOURS = 2.0       # 且时间差 ≤2 小时
QUOTE_CHARS = 60              # 原话节选长度


def _quote(text: str, limit: int = QUOTE_CHARS) -> str:
    t = " ".join(str(text or "").split())
    return t[:limit] + ("…" if len(t) > limit else "")


def _local_dt(created_at_local: str) -> datetime | None:
    s = str(created_at_local or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _signals(user_text: str, assistant_text: str,
             local_dt: datetime | None) -> list[str]:
    """一轮对话命中的信号（可多个）。深夜看本地小时，其余看双方文本。"""
    joined = f"{user_text}\n{assistant_text}"
    sigs: list[str] = []
    if any(w in joined for w in EMOTION_WORDS):
        sigs.append("情绪词")
    if local_dt is not None and local_dt.hour in LATE_NIGHT_HOURS:
        sigs.append("深夜")
    if any(w in joined for w in ACCOMPLISH_WORDS):
        sigs.append("共同完成")
    if any(w in joined for w in RECONCILE_WORDS):
        sigs.append("争执和解")
    return sigs


def detect_episodes(turns: list[dict]) -> list[dict]:
    """纯函数：把带信号标记的轮次聚成经历窗口 → 提案列表。

    turns: [{seq, created_at_local, user_text, assistant_text}]（按 seq 升序）
    返回: [{seq_start, seq_end, signals, item}]——item 是候选提案制兼容的
    MemoryItem 字典（type=EPISODE / tier=experience / category=共同经历）。

    聚合规则：相邻信号轮 seq 差 ≤WINDOW_SEQ_GAP 且时间差 ≤WINDOW_TIME_HOURS
    → 同一段经历（一次深夜长谈往往连续好几轮，不该拆成碎片）。
    """
    marked: list[tuple[dict, list[str], datetime | None]] = []
    for t in turns:
        dt = _local_dt(t.get("created_at_local"))
        sigs = _signals(str(t.get("user_text") or ""),
                        str(t.get("assistant_text") or ""), dt)
        if sigs:
            marked.append((t, sigs, dt))

    windows: list[list[tuple[dict, list[str], datetime | None]]] = []
    for cur in marked:
        if windows:
            prev = windows[-1][-1]
            prev_t, _, prev_dt = prev
            try:
                seq_gap = int(cur[0]["seq"]) - int(prev_t["seq"])
            except (TypeError, ValueError):
                seq_gap = WINDOW_SEQ_GAP + 1
            time_gap = None
            if prev_dt is not None and cur[2] is not None:
                time_gap = abs((cur[2] - prev_dt).total_seconds()) / 3600
            if (seq_gap <= WINDOW_SEQ_GAP
                    and (time_gap is None or time_gap <= WINDOW_TIME_HOURS)):
                windows[-1].append(cur)
                continue
        windows.append([cur])

    out: list[dict] = []
    for w in windows:
        first_t, first_sigs, first_dt = w[0]
        last_t = w[-1][0]
        sigs = list(dict.fromkeys(x for _, ss, _ in w for x in ss))  # 去重保序
        late = "深夜" in sigs
        when = (first_dt.strftime("%m-%d %H:%M") if first_dt is not None
                else f"seq {first_t.get('seq')}")
        user_quote = _quote(first_t.get("user_text"))
        my_quote = _quote(last_t.get("assistant_text"))
        text = (f"【一段经历】{when}{'（深夜）' if late else ''}"
                f"我们连着聊了 {len(w)} 轮：你说「{user_quote}」；"
                f"我说「{my_quote}」。（信号：{'、'.join(sigs)}）")
        item = {
            "text": text,
            "type": "EPISODE",
            "tier": "experience",
            "category": "共同经历",
            "salience": 0.85,
            "confidence": 1.0,
            "note": f"情绪峰值检测（信号：{'、'.join(sigs)}；"
                    f"seq {first_t.get('seq')}-{last_t.get('seq')}，原话节选）",
        }
        out.append({"seq_start": first_t.get("seq"),
                    "seq_end": last_t.get("seq"),
                    "signals": "、".join(sigs),
                    "turns": len(w), "item": item})
    return out


def _ensure_body_instance(conn) -> str:
    """本体 heci_001 必须在 instances 表（候选外键要求 instance 已登记）。

    产线此前只登记过带后缀的席位实例（dsh/muse/zcode），**本体行缺失**——
    2026-11-08 首扫实测踩到（ValueError: 未登记的实例：heci_001）。
    这里幂等补登记，新部署也不会再踩。
    """
    iid = "heci_001"
    row = conn.execute(
        "SELECT 1 FROM instances WHERE instance_id=?", (iid,)).fetchone()
    if row is None:
        now = core._now()
        conn.execute(
            "INSERT INTO instances (instance_id, subject_id, environment, "
            "started_at, last_seen_at, capabilities) VALUES (?,?,?,?,?,?)",
            (iid, iid, "", now, now, "[]"))
        conn.commit()
    return iid


def run_episode_scan(core_conn=None, *, storage_conn=None) -> dict:
    """整理员的经历提炼前置步：读新轮 → 检测 → 提案 → 推进游标。

    游标在 core 库 episode_scan_state（单行表，init_schema 自建）。
    首扫（无游标）从「当前最新 seq − MAX_TURNS_FIRST_SCAN」起步——
    处方语义是"新对话"，不做全史回填。
    连接可注入（自测用）；不注入则自开自关。
    健康条目：成功写 `episode_scan: ok`，失败写 `degraded` 后原样抛出
    ——pythonw 下 print 进 null，HealthBoard 是这个通道唯一的可见性。
    """
    import storage

    own = core_conn is None
    conn = core_conn if core_conn is not None else core.connect()
    try:
        try:
            out = _run_episode_scan_impl(conn, storage_conn)
            core.set_component_health(
                conn, "episode_scan", "ok",
                f"scanned={out['scanned']} proposed={len(out['proposed'])} "
                f"cursor={out['cursor']}")
            return out
        except Exception as exc:  # noqa: BLE001 —— 先落健康条目再抛给 worker
            core.set_component_health(
                conn, "episode_scan", "degraded",
                f"{type(exc).__name__}: {exc}"[:200])
            raise
    finally:
        if own:
            conn.close()


def _run_episode_scan_impl(conn, storage_conn) -> dict:
    import storage

    row = conn.execute(
        "SELECT last_seq FROM episode_scan_state WHERE id=1").fetchone()
    first_run = row is None
    last_seq = int(row["last_seq"]) if row else 0

    own_sc = storage_conn is None
    sc = storage_conn if storage_conn is not None else storage.connect()
    try:
        if first_run:
            top = sc.execute(
                "SELECT MAX(seq) m FROM conversations").fetchone()["m"] or 0
            last_seq = max(0, int(top) - MAX_TURNS_FIRST_SCAN)
        rows = sc.execute(
            "SELECT seq, created_at_local, user_text, assistant_text "
            "FROM conversations WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (last_seq, MAX_TURNS_PER_SCAN)).fetchall()
    finally:
        if own_sc:
            sc.close()

    turns = [dict(r) for r in rows]
    proposals = detect_episodes(turns)
    proposed: list[str] = []
    if proposals:
        _ensure_body_instance(conn)
    for p in proposals[:MAX_EPISODES_PER_RUN]:
        cid, created = core.propose_memory_candidates(
            conn, instance_id="heci_001",
            items=[p["item"]],
            source_environment="episode_scan",
            note=f"经历提炼（信号：{p['signals']}，{p['turns']} 轮）",
            client_request_id=f"episode:{p['seq_start']}-{p['seq_end']}")
        if created:
            proposed.append(cid)

    new_last = int(turns[-1]["seq"]) if turns else last_seq
    conn.execute(
        "INSERT INTO episode_scan_state (id, last_seq, updated_at) "
        "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "last_seq=excluded.last_seq, updated_at=excluded.updated_at",
        (new_last, core._now()))
    conn.commit()
    return {"scanned": len(turns), "detected": len(proposals),
            "proposed": proposed, "cursor": new_last, "first_run": first_run}
