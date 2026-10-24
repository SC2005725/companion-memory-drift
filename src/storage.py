"""原始数据落盘（Phase 3）。

设计要点，全部对应任务书十二、十五节：
- **只追加**：本模块不提供任何 UPDATE / DELETE 接口。
- 每条记录有唯一 ID（turn_id / event_id）。
- 记录时间戳（UTC + 本地）。
- 每个原始事件另存一份带 sha256 的 JSON 文件，便于日后核验未被修改。
- 原始数据（conversation/、memory/raw/）与未来的长期记忆（memory/versions/）物理分开。
- Phase 1-3 不生成也不读取任何 Memory V1/V2/V3。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from paths import DB_FILE, RAW_EVENTS_DIR, RUN_LOG_FILE, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    turn_id         TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    model           TEXT,
    persona_name    TEXT,
    persona_sha256  TEXT,
    user_text       TEXT NOT NULL,
    assistant_text  TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    prev_hash       TEXT,
    sha256          TEXT NOT NULL,
    content_hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, seq);

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    source_turn_id  TEXT NOT NULL,
    user_text       TEXT NOT NULL,
    detection       TEXT NOT NULL,
    file            TEXT NOT NULL,
    sha256          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_calls (
    call_id            TEXT PRIMARY KEY,
    created_at_utc     TEXT NOT NULL,
    session_id         TEXT,
    turn_id            TEXT,
    model              TEXT,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens   INTEGER NOT NULL DEFAULT 0,
    peak               INTEGER NOT NULL DEFAULT 0,
    cost_micro_yuan    INTEGER,
    latency_ms         INTEGER
);

-- Phase 5：记忆版本登记。文件本体在 memory/versions/vNNN.json，这里只是索引。
-- 刻意不放进 conversations 的哈希链：加列会改变既有记录的链哈希，
-- 导致 verify 把 Phase 1-3 的历史数据报成「被改动过」。
CREATE TABLE IF NOT EXISTS memory_versions (
    version         TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    model           TEXT,
    item_count      INTEGER NOT NULL DEFAULT 0,
    file            TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    previous_version TEXT,
    note            TEXT
);

-- Phase 5：每轮检索记录（P0 机制）。
-- 用途：区分「记忆真的漂移了」和「这一轮根本没检索到那条记忆」。
CREATE TABLE IF NOT EXISTS retrievals (
    retrieval_id      TEXT PRIMARY KEY,
    created_at_utc    TEXT NOT NULL,
    session_id        TEXT,
    turn_id           TEXT,
    memory_version    TEXT,
    query_text        TEXT NOT NULL,
    method            TEXT NOT NULL,
    top_k             INTEGER NOT NULL DEFAULT 0,
    hit_item_ids      TEXT NOT NULL,
    injected          INTEGER NOT NULL DEFAULT 0
);

-- Phase 6：关系事件流。
-- project-spec §4 要求关系来自「实际互动事件」而非天数累积，所以关系状态
-- 完全由这张表的事件**确定性推导**（见 src/relationship.py 的 derive_state），
-- 不存在任何按轮次自增的计数器。
CREATE TABLE IF NOT EXISTS relationship_events (
    event_id        TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    session_id      TEXT,
    turn_id         TEXT,
    seq             INTEGER,
    type            TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    matched         TEXT NOT NULL,
    note            TEXT
);

-- Phase 7：用户模型变更事件（审计留痕）。
-- 用户模型的条目本体在 user_model/model_vNNN.json；这张表记录「什么时候新增/修正了什么」，
-- 这样「小满为什么认为用户是这样的人」以及「这条判断什么时候被推翻的」都能追溯。
CREATE TABLE IF NOT EXISTS user_model_events (
    event_id        TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    session_id      TEXT,
    turn_id         TEXT,
    seq             INTEGER,
    kind            TEXT NOT NULL,
    claim_id        TEXT,
    belief          TEXT,
    detail          TEXT
);

-- Phase 8：心跳（Heartbeat）。每次「检查机会」落一行，**包括决定不行动的情况**。
--
-- project-spec §29：『「选择不行动」本身也是 Agency』。
-- 所以不行动必须留痕 —— 否则无法回答「她多久主动想过一次」「为什么没说话」。
CREATE TABLE IF NOT EXISTS heartbeat_runs (
    run_id          TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    action          TEXT,
    message         TEXT,
    context         TEXT
);

-- Phase 8：待处理项。心跳做出的「主动消息」意图先落这里，
-- 下次用户开口时注入上下文 —— 命令行项目没有投递通道，
-- 主动意图只能在「下次见面」时兑现。这一点如实记录，不假装能主动推送。
CREATE TABLE IF NOT EXISTS pending_actions (
    pending_id      TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    run_id          TEXT,
    kind            TEXT NOT NULL,
    message         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    delivered_at    TEXT,
    turn_id         TEXT
);

-- Phase 9：工具调用账本。
--
-- project-spec §30：工具调用必须「可记录、可追踪、有输入输出、有错误处理、有权限边界」。
-- 每次调用（**包括被拒绝和失败的**）都落一行 —— 只记成功的话，
-- 就无法回答「她有没有尝试越界」。
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id         TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    created_at_local TEXT NOT NULL,
    turn_id         TEXT,
    seq             INTEGER,
    name            TEXT NOT NULL,
    args            TEXT,
    ok              INTEGER NOT NULL,
    result          TEXT,
    error           TEXT,
    error_kind      TEXT,
    denied          INTEGER NOT NULL DEFAULT 0,
    denied_reason   TEXT,
    duration_ms     INTEGER,
    iterations      INTEGER
);
"""


RELATIONSHIP_EVENT_TYPES = ("positive", "negative", "conflict", "shared_experience")

# 心跳可做出的决定（project-spec §8）
HEARTBEAT_DECISIONS = ("no_action", "wait", "proactive_message", "update_state")


def _now() -> tuple[str, str]:
    now = datetime.now(timezone.utc).astimezone()
    return now.astimezone(timezone.utc).isoformat(), now.isoformat()


def now_local() -> str:
    """当前本地时间（ISO 字符串）。账本与日志统一用它，便于与对话记录对齐。"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def looks_like_real_data(conn: sqlite3.Connection) -> bool:
    """这个库是不是**项目真实数据文件本身**。

    ⚠️ **不要用行数判断。** 我第一版就是这么写的，结果**误判**：
    临时副本是从真库复制来的，行数跟真库一样（134 行），
    于是「切到副本后」也被拦住了。

    改判据：是真实数据，当且仅当**它就在 `paths.DB_FILE` 指向的位置**
    **且没有被 redirect_db 重定向过**。
    """
    try:
        same_path = os.path.abspath(str(DB_FILE)) == os.path.abspath(str(_REAL_DB_FILE))
    except (OSError, ValueError):
        return False
    return same_path and not _REDIRECTED


# 记录真实数据库路径与是否被重定向过 —— 供 looks_like_real_data 判断
_REAL_DB_FILE = DB_FILE
_REDIRECTED = False


def connect(*, for_test: bool = False) -> sqlite3.Connection:
    """打开数据库连接。

    `for_test=True` **仅供可能写入大量数据的测试脚本**使用：
    它会拒绝连接真实项目数据库，逼你先用 `redirect_db()` 切到副本。

    ## 为什么要这个开关（我在这上面出过严重事故）

    做容量测试时我以为在副本上跑，实际把 **50000 行合成数据写进了真实数据库**
    （13.8 MB），只能从备份恢复。根因是 `from paths import DB_FILE` 在导入时
    绑死了路径，`paths.DB_FILE = ...` 不生效。

    但**更根本的问题是：写错地方时没有任何提示**。
    所以加这个开关 —— 破坏性测试加一个 `for_test=True`，
    写错地方时它会直接拒绝，而不是默默把真数据毁掉。
    """
    ensure_dirs()
    if for_test and not os.environ.get("DSH_ALLOW_REAL_DB_WRITE"):
        conn = sqlite3.connect(str(DB_FILE))
        try:
            real = looks_like_real_data(conn)
        finally:
            conn.close()
        if real:
            raise RuntimeError(
                f"拒绝在真实数据库上运行测试：{DB_FILE}\n"
                "请先调用 storage.redirect_db(副本路径) 切到临时副本，"
                "或设置环境变量 DSH_ALLOW_REAL_DB_WRITE=1 显式确认。"
            )
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def redirect_db(path) -> None:
    """**仅供测试**把数据库切到别处（通常是临时副本）。

    ## 为什么必须有这个函数（我在这上面出过严重事故）

    本模块用的是 `from paths import DB_FILE` —— 这在**导入时**就把值绑死了。
    所以测试脚本里写 `paths.DB_FILE = 副本路径` **完全不起作用**，
    `connect()` 仍然连真实的 `conversation/conversations.db`。

    事故经过：做「长期运行」的容量测试时，我以为在副本上跑，
    实际把 **50000 行合成数据写进了真实数据库**（13.8 MB），
    只好从备份恢复。

    **这类错误不报错、不提示，静默写错地方** —— 比崩溃危险得多。
    所以提供一个**显式的**重定向入口，并在这里多说一句：
    需要换库时用这个函数，不要在 `paths` 上赋值。
    """
    if str(path) == str(DB_FILE):
        return
    log_line(f"WARNING: 数据库被重定向到 {path}（应仅在测试中使用）")
    globals()["DB_FILE"] = path
    globals()["_REDIRECTED"] = True



def _ensure_columns(conn: sqlite3.Connection) -> None:
    """兼容早期版本建的库：缺列就补列（纯结构变更，不动原始数据内容）。"""
    wanted = {
        "conversations": {
            # 结构变更，不动原始数据内容。早期版本建的库会缺这两列。
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "prev_content_hash": "TEXT",
        },
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
    conn.commit()


def log_line(message: str) -> None:
    RUN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with RUN_LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {message}\n")


def last_turn(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conversations ORDER BY created_at_utc DESC, rowid DESC LIMIT 1"
    ).fetchone()


def next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM conversations").fetchone()
    return int(row["s"]) + 1


def recent_turns(conn: sqlite3.Connection, limit: int, session_id: str | None = None) -> list[dict]:
    """最近 N 轮对话。

    **必须按 session_id 过滤。** 踩过的坑：早期版本读整张表、不带会话条件，
    于是新开一段会话时，模型会看到**上一段会话最后几轮**的内容，
    表现为「它知道还没聊过的事」。

    对实验的影响更实际：不同会话是不同阶段的记录，混在一起会让「上下文里到底有什么」
    变得不可控，而上下文里有什么会直接改变模型的回答 —— 那会污染记忆漂移的归因。
    """
    if session_id is not None:
        rows = conn.execute(
            "SELECT user_text, assistant_text FROM conversations "
            "WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT user_text, assistant_text FROM conversations ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"user": r["user_text"], "assistant": r["assistant_text"]} for r in reversed(rows)]


def chain_payload(bundle: dict, prev_hash: str | None, prev_content_hash: str | None = None) -> str:
    """哈希链的输入。

    覆盖本记录全部字段 + 前一条的链哈希与内容哈希。

    实际防护能力（已用对照实验验证，不要高估）：
    - 就地改某一条的文本 → 该条自己的 content_hash 立刻对不上，能发现；
    - 插入伪造记录、删除中间记录 → 后继记录的 prev_hash 对不上，能发现；
    - 但**不会**自动向后级联：如果只改第 2 轮的文本而不动第 3 轮，
      第 3 轮仍然合法（因为它吃的是第 2 轮那个没被改动的哈希值）。
      攻击者若想让链条重新自洽，必须重算第 2 轮之后的所有哈希，成倍增加篡改成本。
    """
    return (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        + (prev_hash or "")
        + (prev_content_hash or "")
    )


def content_hash_of(seq: int, user_text: str, assistant_text: str) -> str:
    """只覆盖「这一轮说了什么」，不含时间戳等元数据。

    单独拆出来的用处：能区分「对话内容被改了」和「元数据被改了」，
    verify 报错时这句话更具体。
    """
    return sha256_text(
        json.dumps(
            {"seq": seq, "user_text": user_text, "assistant_text": assistant_text},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def new_turn_id() -> str:
    """预先生成一个 turn_id。

    存在的理由：调用模型时要立刻记账，而记账要带 turn_id；但回复文本要到
    调用返回后才知道。所以先占 ID、后落盘，避免为了回填 turn_id 而写 UPDATE。
    """
    return f"turn_{uuid.uuid4().hex[:12]}"


def save_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    seq: int,
    user_text: str,
    assistant_text: str,
    model: str,
    persona_name: str,
    persona_sha256: str,
    turn_id: str | None = None,
) -> dict:
    """原样保存一次往返对话。调用方必须传入未经清洗的原始文本。"""
    created_utc, created_local = _now()
    prev = last_turn(conn)
    prev_hash = prev["sha256"] if prev else None
    prev_content_hash = prev["content_hash"] if prev else None

    turn_id = turn_id or new_turn_id()
    content_hash = content_hash_of(seq, user_text, assistant_text)

    bundle = {
        "turn_id": turn_id,
        "created_at_utc": created_utc,
        "created_at_local": created_local,
        "session_id": session_id,
        "seq": seq,
        "model": model,
        "persona_name": persona_name,
        "persona_sha256": persona_sha256,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "content_hash": content_hash,
    }
    digest = sha256_text(chain_payload(bundle, prev_hash, prev_content_hash))

    conn.execute(
        """
        INSERT INTO conversations (
            turn_id, created_at_utc, created_at_local, session_id, seq, model,
            persona_name, persona_sha256, user_text, assistant_text,
            raw_json, prev_hash, sha256, content_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            turn_id, created_utc, created_local, session_id, seq, model,
            persona_name, persona_sha256, user_text, assistant_text,
            json.dumps(bundle, ensure_ascii=False), prev_hash, digest, content_hash,
        ),
    )
    conn.commit()
    return {**bundle, "prev_hash": prev_hash, "sha256": digest, "content_hash": content_hash}


def create_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    detection: dict,
    event_id: str | None = None,
) -> dict:
    """把一段用户原话固化成一个不可覆盖的原始事件。

    事件内容来自用户原话本身，不做任何润色或摘要 —— 一旦改写，它就不再是「原始事实」。

    `event_id` 可显式指定，用于**实验基准事件**（如 `baseline-event`）：
    基准需要跨版本稳定引用，随机 ID 会让文档与实验脚本对不上。
    只允许 `raw_event_` 开头的 ID，避免误写成别的东西。
    """
    ensure_dirs()
    if event_id is None:
        event_id = f"raw_event_{uuid.uuid4().hex[:8]}"
    elif not event_id.startswith("raw_event_"):
        raise RuntimeError(
            f"事件 ID 必须以 raw_event_ 开头，收到：{event_id!r}"
        )
    created_utc, created_local = _now()

    record = {
        "event_id": event_id,
        "created_at_utc": created_utc,
        "created_at_local": created_local,
        "session_id": session_id,
        "source_turn_id": turn_id,
        "kind": "user_statement",
        "immutable": True,
        "user_text": user_text,
        "detection": detection,
        "note": "原始事实，禁止覆盖。后续 Memory V1/V2/V3 只能引用，不能改写本记录。",
    }

    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
    digest = sha256_text(body)

    json_path = RAW_EVENTS_DIR / f"{event_id}.json"
    if json_path.exists():  # 保底不做覆盖
        raise RuntimeError(f"事件文件已存在，拒绝覆盖：{json_path.name}")
    json_path.write_text(body + "\n", encoding="utf-8")

    sha_path = RAW_EVENTS_DIR / f"{event_id}.sha256"
    sha_path.write_text(f"{digest}  {event_id}.json\n", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO events (
            event_id, created_at_utc, created_at_local, session_id, source_turn_id,
            user_text, detection, file, sha256
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, created_utc, created_local, session_id, turn_id, user_text,
            json.dumps(detection, ensure_ascii=False),
            f"memory/raw/events/{event_id}.json", digest,
        ),
    )
    conn.commit()
    return {**record, "file": f"memory/raw/events/{event_id}.json", "sha256": digest}


def find_event_by_text(conn: sqlite3.Connection, user_text: str) -> sqlite3.Row | None:
    """按原文精确查找已存在的原始事件。用于防止同一段原话被重复固化。"""
    return conn.execute(
        "SELECT * FROM events WHERE user_text = ? ORDER BY created_at_utc ASC LIMIT 1",
        (user_text,),
    ).fetchone()


def record_api_call(
    conn: sqlite3.Connection,
    *,
    call_id: str,
    session_id: str,
    turn_id: str | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int,
    peak: bool,
    cost_micro_yuan: int | None,
    latency_ms: int,
) -> None:
    created_utc, _ = _now()
    conn.execute(
        """
        INSERT INTO api_calls (
            call_id, created_at_utc, session_id, turn_id, model, prompt_tokens,
            completion_tokens, cache_hit_tokens, peak, cost_micro_yuan, latency_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            call_id, created_utc, session_id, turn_id, model, prompt_tokens,
            completion_tokens, cache_hit_tokens, 1 if peak else 0,
            cost_micro_yuan, latency_ms,
        ),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    c = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    e = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    a = conn.execute("SELECT COUNT(*) AS n FROM api_calls").fetchone()["n"]
    return {"turns": int(c), "events": int(e), "api_calls": int(a)}


def memory_stats(conn: sqlite3.Connection) -> dict:
    return {
        "versions": int(conn.execute("SELECT COUNT(*) AS n FROM memory_versions").fetchone()["n"]),
        "retrievals": int(conn.execute("SELECT COUNT(*) AS n FROM retrievals").fetchone()["n"]),
    }


# ----------------------------------------------------------------------------
# Phase 5：记忆版本登记（只追加，不提供 UPDATE / DELETE）
# ----------------------------------------------------------------------------

def record_memory_version(
    conn: sqlite3.Connection,
    *,
    version: str,
    model: str,
    item_count: int,
    file: str,
    file_sha256: str,
    previous_version: str | None,
    note: str = "",
) -> None:
    """登记一个记忆版本。版本号已存在时直接抛错 —— 版本快照永不覆盖。"""
    exists = conn.execute(
        "SELECT 1 FROM memory_versions WHERE version = ?", (version,)
    ).fetchone()
    if exists:
        raise RuntimeError(f"记忆版本已存在，拒绝覆盖：{version}")

    created_utc, created_local = _now()
    conn.execute(
        """
        INSERT INTO memory_versions (
            version, created_at_utc, created_at_local, model, item_count,
            file, file_sha256, previous_version, note
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            version, created_utc, created_local, model, item_count,
            file, file_sha256, previous_version, note,
        ),
    )
    conn.commit()


def list_memory_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM memory_versions ORDER BY version ASC"
    ).fetchall()


def latest_memory_version(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM memory_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()


# ----------------------------------------------------------------------------
# Phase 5：检索落盘（P0 机制，只追加）
# ----------------------------------------------------------------------------

def record_retrieval(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str | None,
    memory_version: str | None,
    query_text: str,
    method: str,
    top_k: int,
    hit_item_ids: list[str],
    injected: bool,
) -> str:
    """记录「这一轮检索命中了哪些记忆条目」。

    这是区分「记忆漂移」与「检索失败」的唯一依据，不可省略：
    记忆 v3 里明明还留着某条事实、只是这一轮没被检索进上下文，AI 自然答不出来；
    若不落盘检索结果，事后会把它误判成「记忆丢失」。
    """
    retrieval_id = f"retr_{uuid.uuid4().hex[:12]}"
    created_utc, _ = _now()
    conn.execute(
        """
        INSERT INTO retrievals (
            retrieval_id, created_at_utc, session_id, turn_id, memory_version,
            query_text, method, top_k, hit_item_ids, injected
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            retrieval_id, created_utc, session_id, turn_id, memory_version,
            query_text, method, top_k,
            json.dumps(list(hit_item_ids), ensure_ascii=False),
            1 if injected else 0,
        ),
    )
    conn.commit()
    return retrieval_id


# ----------------------------------------------------------------------------
# Phase 6：关系事件（只追加）
# ----------------------------------------------------------------------------

def record_relationship_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str | None,
    seq: int | None,
    type_: str,
    evidence: str,
    matched: list[str],
    note: str = "",
) -> str:
    """记录一条关系事件。

    关系状态不直接改，而是由这些事件**确定性推导** ——
    这样「为什么关系变成这样」永远能追溯到具体哪句话引起的。
    """
    event_id = f"relevt_{uuid.uuid4().hex[:10]}"
    created_utc, created_local = _now()
    conn.execute(
        """
        INSERT INTO relationship_events (
            event_id, created_at_utc, created_at_local, session_id, turn_id,
            seq, type, evidence, matched, note
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, created_utc, created_local, session_id, turn_id,
            seq, type_, evidence, json.dumps(list(matched), ensure_ascii=False), note,
        ),
    )
    conn.commit()
    return event_id


def list_relationship_events(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[sqlite3.Row]:
    if session_id is not None:
        return conn.execute(
            "SELECT * FROM relationship_events WHERE session_id = ? "
            "ORDER BY created_at_utc ASC, rowid ASC",
            (session_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM relationship_events ORDER BY created_at_utc ASC, rowid ASC"
    ).fetchall()


def relationship_event_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT type, COUNT(*) AS n FROM relationship_events GROUP BY type"
    ).fetchall()
    counts = {t: 0 for t in RELATIONSHIP_EVENT_TYPES}
    for r in rows:
        counts[r["type"]] = int(r["n"])
    return counts


# ----------------------------------------------------------------------------
# Phase 7：用户模型变更事件（只追加）
# ----------------------------------------------------------------------------

def record_user_model_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str | None,
    seq: int | None,
    kind: str,
    claim_id: str | None,
    belief: str,
    detail: str = "",
) -> str:
    """记录一次用户模型变更（新增 / 修正 / 快照）。"""
    event_id = f"umevt_{uuid.uuid4().hex[:10]}"
    created_utc, created_local = _now()
    conn.execute(
        """
        INSERT INTO user_model_events (
            event_id, created_at_utc, created_at_local, session_id, turn_id,
            seq, kind, claim_id, belief, detail
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, created_utc, created_local, session_id, turn_id,
         seq, kind, claim_id, belief, detail),
    )
    conn.commit()
    return event_id


def list_user_model_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM user_model_events ORDER BY created_at_utc ASC, rowid ASC"
    ).fetchall()


# ----------------------------------------------------------------------------
# Phase 8：心跳与待处理项（只追加；待处理项会被标记已投递，但不删除）
# ----------------------------------------------------------------------------

def record_heartbeat_run(
    conn: sqlite3.Connection,
    *,
    decision: str,
    reason: str,
    action: str = "",
    message: str = "",
    context: dict | None = None,
) -> str:
    """记录一次心跳检查。**决定不行动时也要记**（§29）。"""
    run_id = f"hb_{uuid.uuid4().hex[:10]}"
    created_utc, created_local = _now()
    conn.execute(
        """
        INSERT INTO heartbeat_runs (
            run_id, created_at_utc, created_at_local, decision, reason, action, message, context
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (run_id, created_utc, created_local, decision, reason,
         action or None, message or None,
         json.dumps(context or {}, ensure_ascii=False)),
    )
    conn.commit()
    return run_id


def list_heartbeat_runs(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM heartbeat_runs ORDER BY created_at_utc DESC, rowid DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def record_pending_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    message: str,
    reason: str,
) -> str:
    pending_id = f"pa_{uuid.uuid4().hex[:10]}"
    created_utc, created_local = _now()
    conn.execute(
        """
        INSERT INTO pending_actions (
            pending_id, created_at_utc, created_at_local, run_id, kind, message, reason, status
        ) VALUES (?,?,?,?,?,?,?,'pending')
        """,
        (pending_id, created_utc, created_local, run_id, kind, message, reason),
    )
    conn.commit()
    return pending_id


def list_pending_actions(conn: sqlite3.Connection, status: str | None = "pending") -> list[sqlite3.Row]:
    if status is None:
        return conn.execute(
            "SELECT * FROM pending_actions ORDER BY created_at_utc ASC, rowid ASC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pending_actions WHERE status = ? ORDER BY created_at_utc ASC, rowid ASC",
        (status,),
    ).fetchall()


def mark_pending_delivered(conn: sqlite3.Connection, pending_id: str, turn_id: str) -> None:
    """标记某条待处理项已在某一轮兑现。不删除 —— 保留投递痕迹。"""
    created_utc, _ = _now()
    conn.execute(
        "UPDATE pending_actions SET status='delivered', delivered_at=?, turn_id=? "
        "WHERE pending_id=?",
        (created_utc, turn_id, pending_id),
    )
    conn.commit()


# ----------------------------------------------------------------------------
# Phase 9：工具调用账本
# ----------------------------------------------------------------------------

def record_tool_call(
    conn: sqlite3.Connection,
    *,
    name: str,
    args: dict | str | None,
    ok: bool,
    result: str = "",
    error: str = "",
    error_kind: str = "",
    duration_ms: int | None = None,
    turn_id: str | None = None,
    seq: int | None = None,
    iterations: int | None = None,
) -> str:
    """记录一次工具调用。

    **被拒绝与失败的调用也要记** —— 只记成功的话，就无法回答
    「她有没有尝试越界」（project-spec §30 要求可追踪）。
    """
    call_id = f"tc_{uuid.uuid4().hex[:12]}"
    created_utc, created_local = _now()
    if isinstance(args, (dict, list)):
        args_text = json.dumps(args, ensure_ascii=False)
    else:
        args_text = args or ""
    conn.execute(
        """
        INSERT INTO tool_calls (
            call_id, created_at_utc, created_at_local, turn_id, seq, name, args,
            ok, result, error, error_kind, denied, denied_reason, duration_ms, iterations
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            call_id, created_utc, created_local, turn_id, seq, name, args_text,
            1 if ok else 0, (result or "")[:4000], (error or "")[:2000],
            error_kind or None,
            1 if error_kind == "denied" else 0,
            error if error_kind == "denied" else None,
            duration_ms, iterations,
        ),
    )
    conn.commit()
    return call_id


def list_tool_calls(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM tool_calls ORDER BY created_at_utc DESC, rowid DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def is_writable(path) -> bool:
    """自检用：确认目录真的可写（迁移后权限异常时能早发现）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
