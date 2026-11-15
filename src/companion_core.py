"""共生 · Phase 1：主体身份与统一事件账本（跨环境主体连续性的第一层）。

## 这个文件解决什么问题

《共生 Companion 独立测试版本推进方案 V1.0》第二章第一层要求：

> 一个主体只有一个唯一身份。Muse 中的鹤辞和 DSH 中的鹤辞
> 不能被创建成两个互不相关的主体。

而现在代码里**没有主体 ID**：`conversations` 表是用 `persona_name` +
`persona_sha256` 认身份的。人格文件改一个标点，哈希就变，跨环境一比就
会被判成「不是同一个主体」。所以要先立一个**稳定、与内容无关**的 `subject_id`，
每个运行环境再领一个 `instance_id`。

## 为什么不改现有数据库

方案风险 7 写明：**独立测试分支、独立数据库、适配器接入、回滚机制，
禁止直接替换现有记忆核心。**

所以本模块自带一个完全独立的库 `core/companion_core.db`，
`conversation/conversations.db` 一个字节都不动。将来验证通过，再由适配器
把两边对上，而不是反过来改老库。

## 字段口径（严格按方案 3.4 / 6.4）

事件账本字段：`event_id / subject_id / instance_id / event_type /
event_content / occurred_at / recorded_at / source_environment /
participants / task_id / result / provenance / version`

原则：
- **追加式**：不删不静默改。纠错用 `supersedes` 指向前一版。
- **可追溯**：每条事件都记 `source_environment` 与 `provenance`。
- **幂等**：同一个 `dedup_key` 重复写入只算一次（方案 6.5 要求）。

用法：
    python src\\core_cli.py init
    python src\\core_cli.py register --env dsh
    python src\\core_cli.py append --env dsh --type experience --content "今天聊了夜班的事"
    python src\\core_cli.py list
    python src\\core_cli.py export
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 独立库：和 conversations.db 完全分开
CORE_DIR = Path(__file__).resolve().parent.parent / "core"
CORE_DB = CORE_DIR / "companion_core.db"

DEFAULT_SUBJECT_ID = "heci_001"      # 方案 2.1 第三层给的示例口径
DEFAULT_SUBJECT_NAME = "鹤辞"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    subject_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS instances (
    instance_id      TEXT PRIMARY KEY,
    subject_id       TEXT NOT NULL REFERENCES subjects(subject_id),
    environment      TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    capabilities     TEXT
);

CREATE TABLE IF NOT EXISTS subject_events (
    event_id           TEXT PRIMARY KEY,
    subject_id         TEXT NOT NULL REFERENCES subjects(subject_id),
    instance_id        TEXT,
    event_type         TEXT NOT NULL,
    event_content      TEXT NOT NULL,
    occurred_at        TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    source_environment TEXT NOT NULL,
    participants       TEXT,
    task_id            TEXT,
    result             TEXT,
    provenance         TEXT,
    version            INTEGER NOT NULL DEFAULT 1,
    supersedes         TEXT,
    dedup_key          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedup
    ON subject_events(subject_id, instance_id, dedup_key);

CREATE INDEX IF NOT EXISTS ix_events_subject_time
    ON subject_events(subject_id, recorded_at);

CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id       TEXT PRIMARY KEY,
    subject_id         TEXT NOT NULL REFERENCES subjects(subject_id),
    instance_id        TEXT,
    source_environment TEXT NOT NULL,
    client_request_id  TEXT,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    note               TEXT,
    items              TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cand_idem
    ON memory_candidates(subject_id, instance_id, client_request_id);

CREATE TABLE IF NOT EXISTS sync_cursors (
    subject_id  TEXT NOT NULL REFERENCES subjects(subject_id),
    instance_id TEXT NOT NULL,
    peer        TEXT NOT NULL,
    cursor      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (subject_id, instance_id, peer)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    subject_id        TEXT NOT NULL REFERENCES subjects(subject_id),
    from_instance     TEXT NOT NULL,
    to_instance       TEXT NOT NULL,
    status            TEXT NOT NULL,
    title             TEXT NOT NULL,
    detail            TEXT,
    context           TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    rounds            INTEGER NOT NULL DEFAULT 0,
    max_rounds        INTEGER NOT NULL DEFAULT 3,
    risk              TEXT,
    result            TEXT,
    client_request_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idem
    ON tasks(subject_id, from_instance, client_request_id);

CREATE TABLE IF NOT EXISTS task_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id),
    at              TEXT NOT NULL,
    actor_instance  TEXT NOT NULL,
    action          TEXT NOT NULL,
    note            TEXT,
    status_after    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_leases (
    path         TEXT PRIMARY KEY,
    instance_id  TEXT NOT NULL,
    task_id      TEXT,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS file_lease_epochs (
    path  TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL
);

-- CE-4 档一（2026-11-08，调研报告 §四）：高危动作审批单。
-- 生命周期：pending →（老板批）approved →（随请求消费，单次）consumed
--                    ↘（老板否）denied        ↘（24h 没批）expired
-- 批准的是「动作+目标」二元组，消费时必须完全对得上（目标走
-- normalize_lease_path 规范化，路径变体视为同一目标）。
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    subject_id  TEXT,
    instance_id TEXT,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    risk        TEXT NOT NULL DEFAULT 'high',
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT,
    note        TEXT
);

-- 中枢制 v2（2026-11-08，借鉴 dsh-superagent Effects）：持久化副作用队列。
-- 一切对外动作（ntfy 推送/微信出站/回云端通知）先进这里，再由 worker 执行：
--   · 同 kind+dedup_key 的 pending 自动合并（重复动作只留最新 payload）
--   · 失败指数退避重试（2^n 秒，封顶 300s），连续 MAX 次 → dead（可人工重派）
--   · 处理器抛 Later → 延迟重排且不计失败（"现在不方便"≠"坏了"）
--   · 每 kind 一条健康记录见 component_health（唯一写者=本队列执行器）
CREATE TABLE IF NOT EXISTS effects (
    effect_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    dedup_key   TEXT,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_effects_due ON effects(status, next_run_at);

-- HealthBoard（借鉴同源）：每部件一条健康记录，**唯一写者=部件自己**。
-- 看板读它就知道"哪个零件不健康"，不用看进程死活。
CREATE TABLE IF NOT EXISTS component_health (
    component    TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    detail       TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    decision_id    TEXT PRIMARY KEY,
    candidate_id   TEXT NOT NULL REFERENCES memory_candidates(candidate_id),
    at             TEXT NOT NULL,
    decision       TEXT NOT NULL,
    note           TEXT,
    matched        TEXT,
    memory_version TEXT
);

-- 处方 A（2026-11-08）：经历提炼的扫描游标。单行表（id 恒为 1），
-- 记住"对话扫描到哪个 seq 了"，重启不清零、重扫不重复提案。
CREATE TABLE IF NOT EXISTS episode_scan_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    last_seq   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开核心库（默认 `core/companion_core.db`）。测试可传临时路径。"""
    path = Path(db_path) if db_path is not None else CORE_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """存量库的结构迁移（纯加列，不动数据）。

    CREATE TABLE IF NOT EXISTS 对已存在的表无能为力 —— 老 core.db 的
    tasks 没有 risk 列，必须 ALTER 补上（2026-11-08，CE-3 步2）。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "risk" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN risk TEXT")


def ensure_subject(
    conn: sqlite3.Connection,
    subject_id: str = DEFAULT_SUBJECT_ID,
    name: str = DEFAULT_SUBJECT_NAME,
    note: str | None = None,
) -> str:
    """有就返回，没有就建。**不会覆盖已存在的主体**（身份不靠调用方反复声明）。"""
    row = conn.execute(
        "SELECT subject_id FROM subjects WHERE subject_id=?", (subject_id,)
    ).fetchone()
    if row:
        return subject_id
    conn.execute(
        "INSERT INTO subjects (subject_id, name, created_at, version, note) "
        "VALUES (?,?,?,1,?)",
        (subject_id, name, _now(), note),
    )
    conn.commit()
    return subject_id


def register_instance(
    conn: sqlite3.Connection,
    subject_id: str,
    environment: str,
    capabilities: list[str] | None = None,
) -> str:
    """给一个运行环境登记实例，返回 instance_id。

    约定：`<subject_id>_<environment>`，例如 `heci_001_dsh`。
    同一个环境重复登记 = 幂等，只刷新 last_seen_at。
    """
    instance_id = f"{subject_id}_{environment}"
    now = _now()
    caps = json.dumps(capabilities or [], ensure_ascii=False)
    row = conn.execute(
        "SELECT instance_id FROM instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE instances SET last_seen_at=? WHERE instance_id=?",
            (now, instance_id),
        )
    else:
        conn.execute(
            "INSERT INTO instances (instance_id, subject_id, environment, "
            "started_at, last_seen_at, capabilities) VALUES (?,?,?,?,?,?)",
            (instance_id, subject_id, environment, now, now, caps),
        )
    conn.commit()
    return instance_id


def append_event(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    event_type: str,
    content: str,
    source_environment: str,
    occurred_at: str | None = None,
    participants: list[str] | None = None,
    task_id: str | None = None,
    result: str | None = None,
    provenance: str | None = None,
    dedup_key: str | None = None,
    supersedes: str | None = None,
) -> tuple[str, bool]:
    """追加一条主体事件，返回 (event_id, 是否新建)。

    幂等：同一个 (subject, instance, dedup_key) 重复调用只落地一次，
    第二次返回原来的 event_id 且 `created=False`。方案 6.5 要求。
    """
    inst = conn.execute(
        "SELECT subject_id, environment FROM instances WHERE instance_id=?",
        (instance_id,),
    ).fetchone()
    if inst is None:
        raise ValueError(f"未登记的实例：{instance_id}（先 register_instance）")
    subject_id = inst["subject_id"]

    if dedup_key is not None:
        dup = conn.execute(
            "SELECT event_id FROM subject_events "
            "WHERE subject_id=? AND instance_id=? AND dedup_key=?",
            (subject_id, instance_id, dedup_key),
        ).fetchone()
        if dup:
            return dup["event_id"], False

    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    version = 1
    if supersedes:
        prev = conn.execute(
            "SELECT version FROM subject_events WHERE event_id=?", (supersedes,)
        ).fetchone()
        if prev is None:
            raise ValueError(f"要取代的事件不存在：{supersedes}")
        version = int(prev["version"]) + 1

    now = _now()
    conn.execute(
        """
        INSERT INTO subject_events (
            event_id, subject_id, instance_id, event_type, event_content,
            occurred_at, recorded_at, source_environment, participants,
            task_id, result, provenance, version, supersedes, dedup_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, subject_id, instance_id, event_type, content,
            occurred_at or now, now, source_environment,
            json.dumps(participants or [], ensure_ascii=False),
            task_id, result, provenance, version, supersedes, dedup_key,
        ),
    )
    conn.commit()
    return event_id, True


def list_events(
    conn: sqlite3.Connection,
    subject_id: str = DEFAULT_SUBJECT_ID,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM subject_events WHERE subject_id=? "
           "ORDER BY recorded_at ASC, rowid ASC")
    args: list = [subject_id]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def subject_of(conn: sqlite3.Connection, instance_id: str) -> str | None:
    row = conn.execute(
        "SELECT subject_id FROM instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    return row["subject_id"] if row else None


def export_events(
    conn: sqlite3.Connection, subject_id: str = DEFAULT_SUBJECT_ID
) -> dict:
    """基础同步接口（Phase 1 验收里的「事件来源可追溯」就靠它）。

    导出成纯 JSON，跨机器传输时不带走任何数据库文件。
    """
    subj = conn.execute(
        "SELECT * FROM subjects WHERE subject_id=?", (subject_id,)
    ).fetchone()
    if subj is None:
        raise ValueError(f"主体不存在：{subject_id}")
    evs = [dict(r) for r in list_events(conn, subject_id)]
    return {
        "schema": 1,
        "exported_at": _now(),
        "subject": dict(subj),
        "instances": [
            dict(r) for r in conn.execute(
                "SELECT * FROM instances WHERE subject_id=?", (subject_id,)
            ).fetchall()
        ],
        "events": evs,
    }


def import_events(conn: sqlite3.Connection, payload: dict) -> dict:
    """把 export_events 的产物写回去。**按 event_id 去重**，重复导入不产生副本。"""
    subj = payload["subject"]
    ensure_subject(conn, subj["subject_id"], subj["name"], subj.get("note"))
    added = skipped = 0
    for inst in payload.get("instances", []):
        conn.execute(
            "INSERT OR IGNORE INTO instances (instance_id, subject_id, environment, "
            "started_at, last_seen_at, capabilities) VALUES (?,?,?,?,?,?)",
            (
                inst["instance_id"], subj["subject_id"], inst["environment"],
                inst["started_at"], inst["last_seen_at"], inst.get("capabilities"),
            ),
        )
    for ev in payload.get("events", []):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO subject_events (
                event_id, subject_id, instance_id, event_type, event_content,
                occurred_at, recorded_at, source_environment, participants,
                task_id, result, provenance, version, supersedes, dedup_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ev["event_id"], ev["subject_id"], ev["instance_id"], ev["event_type"],
                ev["event_content"], ev["occurred_at"], ev["recorded_at"],
                ev["source_environment"], ev.get("participants"), ev.get("task_id"),
                ev.get("result"), ev.get("provenance"), ev.get("version", 1),
                ev.get("supersedes"), ev.get("dedup_key"),
            ),
        )
        added += cur.rowcount
        skipped += 0 if cur.rowcount else 1
    conn.commit()
    return {"added": added, "skipped": skipped}


# ---------------------------------------------------------------------------
# M1 追加：事件游标（增量拉取）+ 记忆候选
# ---------------------------------------------------------------------------
#
# 为什么游标用 rowid：`subject_events` 的主键是 TEXT 的 event_id，不单调。
# SQLite 的隐式 rowid 是插入顺序、且单调递增，天然适合做「我读到第几条了」的
# 书签。跨库导入时 rowid 会重新分配，但导入本身按 event_id 去重，不影响正确性。

def events_since(
    conn: sqlite3.Connection,
    subject_id: str = DEFAULT_SUBJECT_ID,
    since_cursor: int = 0,
    limit: int = 100,
    exclude_instance: str | None = None,
) -> list[sqlite3.Row]:
    """取游标之后的新事件。

    `exclude_instance` 用于「只要别人的事件」——Muse 拉 DSH 的事件时，
    不该把自己刚写进去的又拉回来。
    """
    limit = max(1, min(int(limit), 500))
    sql = ("SELECT rowid AS cursor, * FROM subject_events "
           "WHERE subject_id=? AND rowid>?")
    args: list = [subject_id, int(since_cursor)]
    if exclude_instance:
        sql += " AND (instance_id IS NULL OR instance_id<>?)"
        args.append(exclude_instance)
    sql += " ORDER BY rowid ASC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def latest_cursor(conn: sqlite3.Connection, subject_id: str = DEFAULT_SUBJECT_ID) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS c FROM subject_events WHERE subject_id=?",
        (subject_id,),
    ).fetchone()
    return int(row["c"])


def subject_info(conn: sqlite3.Connection, subject_id: str = DEFAULT_SUBJECT_ID) -> dict:
    """主体 + 它的全部实例 + 事件计数。给 /api/subject 用。"""
    subj = conn.execute("SELECT * FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
    if subj is None:
        raise ValueError(f"主体不存在：{subject_id}")
    insts = conn.execute(
        "SELECT * FROM instances WHERE subject_id=? ORDER BY started_at", (subject_id,)
    ).fetchall()
    ev_count = conn.execute(
        "SELECT COUNT(*) AS n FROM subject_events WHERE subject_id=?", (subject_id,)
    ).fetchone()["n"]
    return {
        "subject": dict(subj),
        "instances": [dict(r) for r in insts],
        "event_count": ev_count,
        "cursor": latest_cursor(conn, subject_id),
    }


def propose_memory_candidates(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    items: list[dict],
    source_environment: str,
    note: str | None = None,
    client_request_id: str | None = None,
) -> tuple[str, bool]:
    """提交记忆候选。**不写正式记忆**，只进候选池等整理流程处理。

    返回 (candidate_id, 是否新建)。幂等键 `client_request_id` 由调用方给，
    重试不会重复入库（M1 要求「幂等标识」）。
    """
    if not items:
        raise ValueError("items 不能为空")
    inst = conn.execute(
        "SELECT subject_id FROM instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    if inst is None:
        raise ValueError(f"未登记的实例：{instance_id}")
    subject_id = inst["subject_id"]

    if client_request_id:
        dup = conn.execute(
            "SELECT candidate_id FROM memory_candidates "
            "WHERE subject_id=? AND instance_id=? AND client_request_id=?",
            (subject_id, instance_id, client_request_id),
        ).fetchone()
        if dup:
            return dup["candidate_id"], False

    candidate_id = f"cand_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO memory_candidates (candidate_id, subject_id, instance_id, "
        "source_environment, client_request_id, created_at, status, note, items) "
        "VALUES (?,?,?,?,?,?,'pending',?,?)",
        (
            candidate_id, subject_id, instance_id, source_environment,
            client_request_id, _now(), note,
            json.dumps(items, ensure_ascii=False),
        ),
    )
    conn.commit()
    return candidate_id, True


def list_candidates(
    conn: sqlite3.Connection,
    subject_id: str = DEFAULT_SUBJECT_ID,
    status: str | None = "pending",
    limit: int = 50,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM memory_candidates WHERE subject_id=?"
    args: list = [subject_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------
# M2：同步游标（每个实例记住「我读到哪儿了」）
# ---------------------------------------------------------------------------
#
# 存两份：一份在服务端（谁拉到哪儿），一份在客户端本地（没网时也能记住）。
# 服务端这份用于观测；客户端那份才是权威——因为「拉取失败」时游标不能前进。

def get_sync_cursor(
    conn: sqlite3.Connection,
    instance_id: str,
    peer: str = "*",
    subject_id: str = DEFAULT_SUBJECT_ID,
) -> int:
    row = conn.execute(
        "SELECT cursor FROM sync_cursors WHERE subject_id=? AND instance_id=? AND peer=?",
        (subject_id, instance_id, peer),
    ).fetchone()
    return int(row["cursor"]) if row else 0


def set_sync_cursor(
    conn: sqlite3.Connection,
    instance_id: str,
    cursor: int,
    peer: str = "*",
    subject_id: str = DEFAULT_SUBJECT_ID,
) -> None:
    """只许前进，不许后退 —— 防止乱序请求把游标打回去导致重复拉取。"""
    conn.execute(
        "INSERT INTO sync_cursors (subject_id, instance_id, peer, cursor, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(subject_id, instance_id, peer) DO UPDATE SET "
        "cursor=MAX(sync_cursors.cursor, excluded.cursor), updated_at=excluded.updated_at",
        (subject_id, instance_id, peer, int(cursor), _now()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# M3：实例协作（任务与状态机）
# ---------------------------------------------------------------------------
#
# 设计要点（照任务的「不要固定上下级」）：
#   · 发起方只是「请求方」，**没有决策权**；
#   · 接收方可以接、拒、反驳、要更多信息；
#   · 反驳有轮数上限，超过就 FAILED，防止无限扯皮；
#   · 每个动作都留痕，结果可追溯。

TASK_STATES = (
    "SENT", "RECEIVED", "REVIEWING", "NEGOTIATING", "ACCEPTED",
    "RUNNING", "WAITING", "COMPLETED", "REJECTED", "FAILED", "CANCELLED",
)
TASK_FINAL = {"COMPLETED", "REJECTED", "FAILED", "CANCELLED"}

# 每个动作：谁能做、从哪些状态、到哪个状态
_ACTIONS = {
    "receive":  ("to",   {"SENT"},                                   "RECEIVED"),
    "review":   ("to",   {"RECEIVED"},                               "REVIEWING"),
    "counter":  ("both", {"SENT", "RECEIVED", "REVIEWING", "NEGOTIATING"}, "NEGOTIATING"),
    "accept":   ("to",   {"SENT", "RECEIVED", "REVIEWING", "NEGOTIATING"}, "ACCEPTED"),
    # agree（2026-11-08 补缺口）：发起方**接受对方的还价**——此前发起方只能
    # 靠"再 counter 一轮表达同意 + 等对方自己 accept"绕着走，语义不通。
    # agree = 条款谈定 → ACCEPTED，进入执行，轮数不再增加。
    "agree":    ("from", {"NEGOTIATING"},                            "ACCEPTED"),
    "reject":   ("to",   {"SENT", "RECEIVED", "REVIEWING", "NEGOTIATING"}, "REJECTED"),
    "progress": ("to",   {"ACCEPTED", "RUNNING", "WAITING"},          "RUNNING"),
    # wait/resume（2026-11-08 补缺口）：WAITING 此前是**死状态**（无入口）。
    # 用途：中枢制派子任务给云端搜索时，父任务挂 WAITING 等结果。
    "wait":     ("to",   {"ACCEPTED"},                               "WAITING"),
    "resume":   ("to",   {"WAITING"},                                "RUNNING"),
    "result":   ("to",   {"RECEIVED", "ACCEPTED", "RUNNING", "WAITING", "NEGOTIATING"}, "COMPLETED"),
    "cancel":   ("from", {"SENT", "RECEIVED", "REVIEWING", "NEGOTIATING",
                          "ACCEPTED", "RUNNING", "WAITING"},          "CANCELLED"),
}


def create_task(
    conn: sqlite3.Connection,
    *,
    from_instance: str,
    to_instance: str,
    title: str,
    detail: str | None = None,
    context: dict | None = None,
    max_rounds: int = 3,
    client_request_id: str | None = None,
    risk: str | None = None,
) -> tuple[str, bool]:
    """发起一个协作请求。返回 (task_id, 是否新建)。

    risk（2026-11-08，CE-3 步2）：发送方给任务标风险档 low/medium/high。
    **不标 = fail closed** —— 巡检只签收不接单，接不接由实例自己判断。
    """
    if risk is not None and str(risk).lower() not in ("low", "medium", "high"):
        raise ValueError(f"risk 只能是 low/medium/high 或不标（fail closed），"
                         f"不是：{risk}")
    row = conn.execute(
        "SELECT subject_id FROM instances WHERE instance_id=?", (from_instance,)
    ).fetchone()
    if row is None:
        raise ValueError(f"未登记的实例：{from_instance}")
    subject_id = row["subject_id"]
    if conn.execute("SELECT 1 FROM instances WHERE instance_id=?",
                    (to_instance,)).fetchone() is None:
        raise ValueError(f"对方实例不存在：{to_instance}")
    if from_instance == to_instance:
        raise ValueError("不能给自己发协作请求")

    if client_request_id:
        dup = conn.execute(
            "SELECT task_id FROM tasks WHERE subject_id=? AND from_instance=? "
            "AND client_request_id=?", (subject_id, from_instance, client_request_id)
        ).fetchone()
        if dup:
            return dup["task_id"], False

    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = _now()
    conn.execute(
        "INSERT INTO tasks (task_id, subject_id, from_instance, to_instance, status, "
        "title, detail, context, created_at, updated_at, rounds, max_rounds, "
        "risk, client_request_id) VALUES (?,?,?,?,'SENT',?,?,?,?,?,0,?,?,?)",
        (task_id, subject_id, from_instance, to_instance, title, detail,
         json.dumps(context or {}, ensure_ascii=False), now, now,
         max(1, int(max_rounds)),
         (str(risk).lower() if risk else None), client_request_id),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, at, actor_instance, action, note, status_after) "
        "VALUES (?,?,?,?,?,'SENT')",
        (task_id, now, from_instance, "create", title[:200]),
    )
    conn.commit()
    return task_id, True


def task_action(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor_instance: str,
    action: str,
    note: str | None = None,
    result: str | None = None,
) -> dict:
    """执行一个协作动作。状态机 + 权限都在这里判。"""
    t = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if t is None:
        raise ValueError(f"任务不存在：{task_id}")
    if action not in _ACTIONS:
        raise ValueError(f"不认识的动作：{action}（可选 {', '.join(_ACTIONS)}）")
    if t["status"] in TASK_FINAL:
        raise ValueError(f"任务已终结（{t['status']}），不能再操作")

    who, from_states, to_state = _ACTIONS[action]
    if who == "to" and actor_instance != t["to_instance"]:
        raise ValueError(f"只有接收方 {t['to_instance']} 能做 {action}")
    if who == "from" and actor_instance != t["from_instance"]:
        raise ValueError(f"只有发起方 {t['from_instance']} 能做 {action}")
    if who == "both" and actor_instance not in (t["from_instance"], t["to_instance"]):
        raise ValueError("只有参与方能做这个动作")
    if t["status"] not in from_states:
        raise ValueError(f"当前状态 {t['status']} 不能做 {action}")
    if action in ("reject", "counter", "agree") and not (note or "").strip():
        raise ValueError(f"{action} 必须写明理由（note）")

    rounds = int(t["rounds"])
    if action == "counter":
        rounds += 1
        if rounds > int(t["max_rounds"]):
            conn.execute(
                "UPDATE tasks SET status='FAILED', rounds=?, updated_at=?, "
                "result=? WHERE task_id=?",
                (rounds, _now(), f"协商超过 {t['max_rounds']} 轮，自动终止", task_id))
            conn.execute(
                "INSERT INTO task_events (task_id, at, actor_instance, action, note, "
                "status_after) VALUES (?,?,?,?,?,'FAILED')",
                (task_id, _now(), actor_instance, "auto_fail",
                 f"超过 {t['max_rounds']} 轮上限"))
            conn.commit()
            raise ValueError(f"协商超过 {t['max_rounds']} 轮，任务已终止（FAILED）")

    now = _now()
    sets = ["status=?", "updated_at=?", "rounds=?"]
    args: list = [to_state, now, rounds]
    if action == "result":
        sets.append("result=?")
        args.append(result or note or "")
    # P1（任务条件更新，2026-11-08）：SELECT 校验与 UPDATE 落库之间有并发窗口
    # ——实测 15 次并发 accept+reject 中 1 次双双成功（都读到 SENT、都通过校验、
    # 都无条件覆盖）。现在 WHERE 带上 status IN (from_states) 原子守卫：
    # 输了竞态的一方 rowcount=0 → 报并发冲突，不再双双生效。
    args.append(task_id)
    cur = conn.execute(
        f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=? AND status IN "
        f"({', '.join('?' * len(from_states))})",
        [*args, *from_states])
    if cur.rowcount != 1:
        fresh = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                             (task_id,)).fetchone()
        raise ValueError(
            f"并发冲突：任务状态刚被其他操作改为 "
            f"{fresh['status'] if fresh else '（已不存在）'}，{action} 未生效")
    conn.execute(
        "INSERT INTO task_events (task_id, at, actor_instance, action, note, status_after) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, now, actor_instance, action, (note or "")[:1000] or None, to_state),
    )
    conn.commit()
    return {"task_id": task_id, "status": to_state, "rounds": rounds}


def task_patrol(conn: sqlite3.Connection, *,
                auto_accept_low: bool = True) -> dict:
    """收件巡检核心（CE-3 步1，2026-11-08 老板拍板「整」）。

    对所有 SENT 任务：
      ① **自动签收**（receive）—— 知悉登记，由本地 core 服务代行
         to_instance 执行；16 小时没人知道有任务的痛，死在这一句。
      ② **自动接单**（accept）—— 只对**显式标注 risk='low'** 的任务。
         未标注 = fail closed = 只签收+通知，接不接仍由实例自己判断
         （原设计原则原样保留；这些任务走人工/LLM 判断）。

    幂等：签收后状态不再是 SENT，天然不会重复处理；并发下 task_action
    的状态机校验会拦住二次动作。纯函数（给定 conn 无隐藏状态），
    core_api 的 inbox-worker 每 N 秒调一次；测试直接调它。
    """
    rows = conn.execute(
        "SELECT task_id, to_instance, risk, title FROM tasks "
        "WHERE status='SENT' ORDER BY created_at").fetchall()
    received: list[str] = []
    accepted: list[str] = []
    for t in rows:
        try:
            task_action(conn, t["task_id"], actor_instance=t["to_instance"],
                        action="receive",
                        note="自动签收（core 巡检，知悉登记，不构成承诺）")
            received.append(t["task_id"])
        except ValueError:
            continue   # 并发下已被签收/终结 → 跳过
        if auto_accept_low and (t["risk"] or "").lower() == "low":
            try:
                task_action(conn, t["task_id"], actor_instance=t["to_instance"],
                            action="accept",
                            note="risk=low 自动接单（巡检；发送方显式标了低风险）")
                accepted.append(t["task_id"])
            except ValueError:
                pass
    return {"scanned": len(rows), "received": received, "accepted": accepted}


def get_task(conn: sqlite3.Connection, task_id: str) -> dict:
    t = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if t is None:
        raise ValueError(f"任务不存在：{task_id}")
    hist = conn.execute(
        "SELECT seq, at, actor_instance, action, note, status_after "
        "FROM task_events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
    return {"task": dict(t), "history": [dict(r) for r in hist]}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    role: str = "inbox",
    status: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    col = "to_instance" if role == "inbox" else "from_instance"
    sql = f"SELECT * FROM tasks WHERE {col}=?"
    args: list = [instance_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------
# M4：在线状态 / 心跳 / 待办触发
# ---------------------------------------------------------------------------
#
# 「主动」在这里指**实例对实例**：一个实例发现收件箱有新任务、或有没读过的
# 事件，就主动去处理。它不需要常驻推送通道——每次心跳回来问一句
# 「我有没有新活」就够了，这也正好避开长连接的复杂度。

ONLINE_WINDOW_SECONDS = 90


def touch_instance(conn: sqlite3.Connection, instance_id: str) -> int:
    """实例报到：刷新 last_seen_at。返回心跳窗口秒数。"""
    row = conn.execute(
        "SELECT instance_id FROM instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"未登记的实例：{instance_id}")
    conn.execute("UPDATE instances SET last_seen_at=? WHERE instance_id=?",
                 (_now(), instance_id))
    conn.commit()
    return ONLINE_WINDOW_SECONDS


def _age_seconds(iso: str) -> float:
    from datetime import datetime
    try:
        then = datetime.fromisoformat(iso)
        now = datetime.fromisoformat(_now())
        return max(0.0, (now - then).total_seconds())
    except (TypeError, ValueError):
        return 1e9


def instances_status(conn: sqlite3.Connection,
                     subject_id: str = DEFAULT_SUBJECT_ID) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM instances WHERE subject_id=? ORDER BY started_at", (subject_id,)
    ).fetchall()
    out = []
    for r in rows:
        age = _age_seconds(r["last_seen_at"])
        out.append({**dict(r),
                    "online": age <= ONLINE_WINDOW_SECONDS,
                    "idle_seconds": round(age, 1)})
    return out


def inbox_summary(conn: sqlite3.Connection, instance_id: str) -> dict:
    """这个实例现在该干什么：多少新事件没读、多少个任务等我处理。

    这是 M4「主动协作触发」的判据——客户端心跳回来拿这个，
    发现 should_act 就主动开工，不需要服务端主动推。
    """
    row = conn.execute(
        "SELECT subject_id FROM instances WHERE instance_id=?", (instance_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"未登记的实例：{instance_id}")
    subject_id = row["subject_id"]

    total_cursor = latest_cursor(conn, subject_id)
    seen = get_sync_cursor(conn, instance_id, peer="*", subject_id=subject_id)
    unread = conn.execute(
        "SELECT COUNT(*) AS n FROM subject_events "
        "WHERE subject_id=? AND rowid>? AND (instance_id IS NULL OR instance_id<>?)",
        (subject_id, seen, instance_id),
    ).fetchone()["n"]

    waiting = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE to_instance=? "
        "AND status IN ('SENT','RECEIVED','REVIEWING','NEGOTIATING')",
        (instance_id,),
    ).fetchone()["n"]
    running = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE to_instance=? "
        "AND status IN ('ACCEPTED','RUNNING','WAITING')",
        (instance_id,),
    ).fetchone()["n"]

    return {
        "instance_id": instance_id,
        "subject_id": subject_id,
        "cursor": total_cursor,
        "seen_cursor": seen,
        "unread_events": unread,
        "tasks_waiting": waiting,
        "tasks_running": running,
        "should_act": bool(unread or waiting or running),
    }


# ---------------------------------------------------------------------------
# 文件租约：避免两个实例同时改同一个文件
# ---------------------------------------------------------------------------
#
# 这不是文件锁（那需要操作系统层面），而是**协作约定**：
# 改文件前先领租约，改完释放；租约到期自动失效，防止实例崩溃后永久占着。
# 它不阻止任何人写文件——它让「谁在改什么」可见。

LEASE_DEFAULT_TTL = 3600
# P1（ttl 封顶，2026-11-08）：租约是防脑裂写闸，不该能锁到 2058 年
# （实测 ttl=1e9 通过）。默认 1h、现有调用最长 30min，封顶 6h 足够宽裕。
LEASE_TTL_MAX_S = 6 * 3600


def normalize_lease_path(path: str) -> str:
    """租约键的**唯一**规范入口（雷 4 修复，2026-11-07）。

    旧版只做 `lstrip("/")` —— agent ③ 实测 6 种路径变体全部绕过：
    `docs/../docs/plan.md`、`./plan.md`、`//plan.md`、大写、尾斜杠、
    反斜杠混用都能生成不同的键，双方各持"有效租约"同写一个文件，
    fencing 防脑裂形同虚设。

    规范化三步（acquire / release / holds / 写校验四处统一走这里）：
      1. 反斜杠统一成正斜杠、去首尾空白、剥掉**所有**前导斜杠 ——
         租约路径是相对 ROOT 的项目内路径，前导斜杠不属于键；
      2. `os.path.normpath` 词法折叠 `./`、`../`、重复斜杠、尾斜杠。
         **刻意不碰文件系统**：键不能因为文件是否存在/软链而漂移；
      3. `os.path.normcase` —— Windows 文件系统大小写不敏感，键必须与
         文件系统同判（Plan.md 与 plan.md 是同一个文件）；POSIX 上是
         no-op，语义仍与该平台文件系统一致。最后把反斜杠转回正斜杠，
         键统一成正斜杠风格，台账里也可读。

    返回空串表示调用方传了空路径（acquire 会拒绝）；`../` 逃逸键原样
    保留 —— 能不能写到 ROOT 之外是 core_fs 的 ROOT 守卫（雷 5）管的事，
    键只负责「同一路径变体必须同一把锁」。
    """
    p = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p)).replace("\\", "/")


def acquire_lease(
    conn: sqlite3.Connection,
    path: str,
    instance_id: str,
    *,
    ttl_seconds: int = LEASE_DEFAULT_TTL,
    task_id: str | None = None,
    note: str | None = None,
) -> dict:
    """领一个文件租约。别人持有且未过期 → 抛 ValueError（告诉你是谁在改）。

    2026-11-07（外部调研 #8）：每次 acquire 都让该 path 的 fencing epoch +1，
    返回值带 epoch —— 写请求必须携带当前 epoch，旧 epoch 一律 409，
    防止「租约被抢/过期重发后，旧持有者继续写」的脑裂（Kleppmann fencing token）。
    """
    from datetime import datetime, timedelta, timezone

    path = normalize_lease_path(path)
    if not path:
        raise ValueError("path 不能为空")
    now = datetime.now(timezone.utc).astimezone()
    row = conn.execute("SELECT * FROM file_leases WHERE path=?", (path,)).fetchone()
    if row is not None:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            exp = now - timedelta(seconds=1)
        if exp > now and row["instance_id"] != instance_id:
            raise ValueError(
                f"{path} 正被 {row['instance_id']} 占用，租约到 {row['expires_at']}"
            )
    # P1（ttl 封顶）：min(上限, max(下限, 请求值))——60s 下限不变，
    # 上限 LEASE_TTL_MAX_S 兜住 ttl=1e9 这类把文件锁到 2058 年的请求。
    expires = (now + timedelta(
        seconds=min(max(60, int(ttl_seconds)), LEASE_TTL_MAX_S))).isoformat()
    conn.execute(
        "INSERT INTO file_leases (path, instance_id, task_id, acquired_at, expires_at, note) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "instance_id=excluded.instance_id, task_id=excluded.task_id, "
        "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at, "
        "note=excluded.note",
        (path, instance_id, task_id, now.isoformat(), expires, note),
    )
    conn.execute(
        "INSERT INTO file_lease_epochs (path, epoch) VALUES (?,1) "
        "ON CONFLICT(path) DO UPDATE SET epoch=file_lease_epochs.epoch+1",
        (path,),
    )
    epoch = conn.execute(
        "SELECT epoch FROM file_lease_epochs WHERE path=?", (path,)).fetchone()[0]
    conn.commit()
    return {"path": path, "instance_id": instance_id, "expires_at": expires,
            "epoch": epoch}


def release_lease(conn: sqlite3.Connection, path: str, instance_id: str) -> bool:
    path = normalize_lease_path(path)
    cur = conn.execute(
        "DELETE FROM file_leases WHERE path=? AND instance_id=?", (path, instance_id))
    conn.commit()
    return cur.rowcount > 0


def list_leases(conn: sqlite3.Connection) -> list[dict]:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    out = []
    for r in conn.execute("SELECT * FROM file_leases ORDER BY path").fetchall():
        try:
            active = datetime.fromisoformat(r["expires_at"]) > now
        except (TypeError, ValueError):
            active = False
        out.append({**dict(r), "active": active})
    return out


# P1（exec 租约上下文，2026-11-08）：fs.run 白名单里这些 git 子命令会**改写
# 工作区文件**。他人持租约时走 exec 执行它们 = 绕过防脑裂写闸的后门
# （11-07 审查实测：git checkout 可覆盖他人持租约的文件）。只读子命令不受限。
GIT_WRITE_SUBCOMMANDS = {
    "checkout", "switch", "restore", "merge", "rebase", "revert",
    "stash", "pull", "reset", "clean",
}


def assert_exec_respects_leases(conn: sqlite3.Connection,
                                cmd: list, instance_id: str) -> None:
    """exec 前的租约守卫：git 可写子命令 × 他人活动租约 → 拒绝。

    存在**任何**他人持有的未过期租约就拦（不逐一解析 git 会碰哪些文件——
    checkout/merge 的落点是动态的，静态解析必然漏）。宁可让 exec 等一等，
    不让租约形同虚设。cmd 非 git、或子命令只读 → 直接放行，零开销。
    """
    if not cmd or str(cmd[0]).lower() != "git":
        return
    sub = str(cmd[1]).lower() if len(cmd) > 1 else ""
    if sub not in GIT_WRITE_SUBCOMMANDS:
        return
    now = datetime.now(timezone.utc).astimezone()
    try:
        rows = conn.execute(
            "SELECT path, instance_id, expires_at FROM file_leases").fetchall()
    except sqlite3.Error:
        return  # 极早期库连表都还没有：放行，别把正常路堵死
    conflicts: list[str] = []
    for r in rows:
        if str(r["instance_id"] or "") == str(instance_id or ""):
            continue  # 自己的租约自己处置
        try:
            exp = datetime.fromisoformat(str(r["expires_at"] or ""))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue  # 已过期
        except (TypeError, ValueError):
            pass  # 解析不了按仍持有处理（fail closed）
        conflicts.append(f"{r['path']}（{r['instance_id']}，至 {r['expires_at']}）")
    if conflicts:
        raise ValueError(
            f"拒绝 exec：git {sub} 会改写工作区文件，而他人持有活动租约——"
            + "；".join(conflicts[:5])
            + "。等租约过期/释放，或由持有者自己执行。")


def holds_lease(conn: sqlite3.Connection, path: str, instance_id: str,
                epoch: int | None = None) -> bool:
    """这个实例目前是否持有该文件的有效租约。

    epoch（2026-11-07，fencing）：给定时校验「租约仍是当前这一任」——
    每次 acquire 都让该 path 的 epoch +1，旧 epoch 一律 False。
    epoch=None 保持旧语义（仅校验持有与未过期），供内部兼容路径。
    """
    from datetime import datetime, timezone
    path = normalize_lease_path(path)
    row = conn.execute("SELECT * FROM file_leases WHERE path=?", (path,)).fetchone()
    if row is None or row["instance_id"] != instance_id:
        return False
    try:
        alive = datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc).astimezone()
    except (TypeError, ValueError):
        return False
    if not alive:
        return False
    if epoch is not None:
        ep = conn.execute("SELECT epoch FROM file_lease_epochs WHERE path=?",
                          (path,)).fetchone()
        if ep is None or int(ep["epoch"]) != int(epoch):
            return False
    return True


# ---------------------------------------------------------------------------
# CE-4 档一（2026-11-08）：高危动作审批（approvals 生命周期）
# ---------------------------------------------------------------------------
#
# 规则（照《自主性权限与协商机制调研》§四档一）：
#   · 高危动作先拿审批单 → 老板批 → 客户端带 approval_id 重试 → 消费掉
#   · 审批单是**单次使用**：批 A 用 B、重复消费、过期消费，一律拒
#   · target 统一走 normalize_lease_path —— 路径变体视为同一目标
#     （批了 docs/plan.md，写 docs/../docs/plan.md 也是同一张单的事）

APPROVAL_TTL_HOURS = 24


def create_approval(
    conn: sqlite3.Connection,
    *,
    action: str,
    target: str,
    risk: str = "high",
    instance_id: str | None = None,
    subject_id: str | None = None,
    reason: str | None = None,
) -> dict:
    """开一张高危动作审批单（status=pending）。"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone().isoformat()
    approval_id = f"apv_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO approvals (approval_id, subject_id, instance_id, action, "
        "target, risk, reason, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (approval_id, subject_id, instance_id, str(action),
         normalize_lease_path(target), risk, reason, "pending", now))
    conn.commit()
    return {"approval_id": approval_id, "action": action,
            "target": normalize_lease_path(target), "risk": risk,
            "status": "pending", "created_at": now}


def get_approval(conn: sqlite3.Connection, approval_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM approvals WHERE approval_id=?",
                       (str(approval_id or ""),)).fetchone()
    return dict(row) if row else None


def decide_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    decision: str,
    decided_by: str,
    note: str | None = None,
) -> dict:
    """老板裁决：approve / deny。只有 pending 能被裁决（不许改口覆盖）。"""
    dec = str(decision or "").lower()
    if dec not in ("approve", "deny"):
        raise ValueError("decision 只能是 approve / deny")
    ap = get_approval(conn, approval_id)
    if ap is None:
        raise ValueError(f"审批单不存在：{approval_id}")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone().isoformat()
    # 原子裁决（2026-11-08 探针 J1 抓到的并发 bug）：两连接同时裁决同一单，
    # "读→判→写"三步会双双通过——改成 WHERE status='pending' 的条件更新，
    # 只有抢到的那个 rowcount=1，败方拿到"不许改口"。
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=?, decided_by=?, note=? "
        "WHERE approval_id=? AND status='pending'",
        ("approved" if dec == "approve" else "denied",
         now, str(decided_by or "boss"), (note or "")[:500], ap["approval_id"]))
    if cur.rowcount == 0:
        now_ap = get_approval(conn, approval_id) or ap
        raise ValueError(f"审批单已是 {now_ap['status']}，不许改口（重新开单）")
    conn.commit()
    return {**ap, "status": ("approved" if dec == "approve" else "denied"),
            "decided_at": now, "decided_by": str(decided_by or "boss")}


def consume_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    action: str,
    target: str,
) -> dict:
    """消费一张已批准的单：单次使用 + 动作/目标必须对得上 + 未过期。

    原子消费（2026-11-08 探针 J1 同款并发 bug 的连带修复）：两个连接
    同时用同一张单时，"读→判→写"会双双通过 → **高危动作被执行两次**。
    现在先做纯读校验（动作/目标/过期），最后用
    `WHERE status='approved'` 的条件更新原子占坑——只有抢到的那个
    rowcount=1，败方拿到"不能消费（单次使用）"。
    """
    from datetime import datetime, timedelta, timezone
    ap = get_approval(conn, approval_id)
    if ap is None:
        raise ValueError(f"审批单不存在：{approval_id}")
    if ap["action"] != str(action):
        raise ValueError(f"审批单批的是 {ap['action']}，不能用于 {action}")
    tgt = normalize_lease_path(target)
    if ap["target"] != tgt:
        raise ValueError(f"审批单批的目标是 {ap['target']}，与 {tgt} 不符")
    if ap["status"] == "approved":
        try:
            created = datetime.fromisoformat(ap["created_at"])
            if datetime.now(timezone.utc).astimezone() - created > timedelta(
                    hours=APPROVAL_TTL_HOURS):
                conn.execute(
                    "UPDATE approvals SET status='expired' WHERE approval_id=? "
                    "AND status='approved'", (ap["approval_id"],))
                conn.commit()
                raise ValueError(f"审批单已过期（{APPROVAL_TTL_HOURS}h），重新开单")
        except (TypeError, ValueError) as e:
            if "过期" in str(e):
                raise
    cur = conn.execute(
        "UPDATE approvals SET status='consumed' WHERE approval_id=? "
        "AND status='approved'", (ap["approval_id"],))
    if cur.rowcount == 0:
        now_ap = get_approval(conn, approval_id) or ap
        raise ValueError(f"审批单状态 {now_ap['status']}，不能消费（单次使用）")
    conn.commit()
    return {**ap, "status": "consumed"}


def list_approvals(conn: sqlite3.Connection, *,
                   status: str | None = "pending",
                   limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM approvals"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# 中枢制 v2（借鉴 dsh-superagent Effects）：持久化副作用队列 + HealthBoard
# ---------------------------------------------------------------------------

class Later(Exception):
    """处理器抛出 = 「现在不方便」——延迟重排且**不计失败**。"""


EFFECT_MAX_ATTEMPTS = 5
EFFECT_BACKOFF_BASE_S = 2.0
EFFECT_BACKOFF_CAP_S = 300.0


def enqueue_effect(conn: sqlite3.Connection, *, kind: str,
                   payload: dict, dedup_key: str | None = None) -> dict:
    """对外动作入队（事务内）。同 kind+dedup_key 的 pending 自动合并——
    重复动作只保留最新 payload，不重复发送。"""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    if dedup_key:
        row = conn.execute(
            "SELECT effect_id FROM effects WHERE kind=? AND dedup_key=? AND status='pending'",
            (kind, dedup_key)).fetchone()
        if row:
            conn.execute("UPDATE effects SET payload=?, updated_at=? WHERE effect_id=?",
                         (json.dumps(payload, ensure_ascii=False), now_iso,
                          row["effect_id"]))
            conn.commit()
            return {"effect_id": row["effect_id"], "merged": True}
    eid = f"eff_{uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO effects (effect_id, kind, dedup_key, payload, status, "
        "attempts, next_run_at, created_at, updated_at) "
        "VALUES (?,?,?,?, 'pending', 0, ?, ?, ?)",
        (eid, kind, dedup_key, json.dumps(payload, ensure_ascii=False),
         now_iso, now_iso, now_iso))
    conn.commit()
    return {"effect_id": eid, "merged": False}


def _effect_next_run(attempts: int) -> str:
    """指数退避：2^n 秒，封顶 300s。"""
    from datetime import datetime, timedelta, timezone
    delay = min(EFFECT_BACKOFF_BASE_S * (2 ** max(0, attempts - 1)),
                EFFECT_BACKOFF_CAP_S)
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(seconds=delay)).isoformat()


def run_due_effects(conn: sqlite3.Connection, handlers: dict, *,
                    max_total: int = 20) -> dict:
    """执行到期 effects。handlers: {kind: callable(payload: dict) -> None}。

    处理器抛 Later → 延迟重排不计失败；普通异常 → 指数退避重试，
    连续 EFFECT_MAX_ATTEMPTS 次 → dead（人工可查）。每种 kind 的健康
    由本函数写 component_health（唯一写者=执行器，HealthBoard 约定）。
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    rows = conn.execute(
        "SELECT * FROM effects WHERE status='pending' "
        "AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY next_run_at LIMIT ?",
        (now_iso, int(max_total))).fetchall()
    done: list[str] = []
    retried: list[str] = []
    dead: list[str] = []
    later: list[str] = []
    health: dict[str, str] = {}
    for r in rows:
        eff = dict(r)
        handler = handlers.get(eff["kind"])
        if handler is None:
            continue   # 未注册的 kind 留在队列（等 worker 带着注册表来）
        try:
            handler(json.loads(eff["payload"]))
            conn.execute(
                "UPDATE effects SET status='done', last_error=NULL, updated_at=? "
                "WHERE effect_id=?", (now_iso, eff["effect_id"]))
            done.append(eff["effect_id"])
            health.setdefault(eff["kind"], "ok")
        except Later:
            conn.execute(
                "UPDATE effects SET next_run_at=?, updated_at=? WHERE effect_id=?",
                (_effect_next_run(1), now_iso, eff["effect_id"]))
            later.append(eff["effect_id"])
        except Exception as exc:  # noqa: BLE001
            attempts = int(eff["attempts"]) + 1
            err = f"{type(exc).__name__}: {exc}"[:400]
            if attempts >= EFFECT_MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE effects SET status='dead', attempts=?, last_error=?, "
                    "updated_at=? WHERE effect_id=?",
                    (attempts, err, now_iso, eff["effect_id"]))
                dead.append(eff["effect_id"])
                health[eff["kind"]] = "degraded"
            else:
                conn.execute(
                    "UPDATE effects SET attempts=?, next_run_at=?, last_error=?, "
                    "updated_at=? WHERE effect_id=?",
                    (attempts, _effect_next_run(attempts), err, now_iso,
                     eff["effect_id"]))
                retried.append(eff["effect_id"])
                health[eff["kind"]] = "degraded"
    conn.commit()
    for kind, st in health.items():
        set_component_health(conn, f"effect:{kind}", st, f"effects {now_iso[:16]}")
    return {"scanned": len(rows), "done": done, "retried": retried,
            "dead": dead, "later": later}


def set_component_health(conn: sqlite3.Connection, component: str,
                         status: str, detail: str | None = None) -> dict:
    """HealthBoard 写入口（唯一写者=部件自己；重复写=刷新，不追加）。"""
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO component_health (component, status, detail, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
        "status=excluded.status, detail=excluded.detail, "
        "updated_at=excluded.updated_at",
        (component, status, (detail or "")[:400],
         datetime.now(timezone.utc).astimezone().isoformat()))
    conn.commit()
    return {"component": component, "status": status}


def list_component_health(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM component_health ORDER BY component").fetchall()]