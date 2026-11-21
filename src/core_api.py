"""共生 · M1 主体 API（跨环境接入层）。

## 这个服务是什么

给「另一个运行实例」（Muse）用的**主体接口**，不是一个网页。

它跑在独立端口（默认 8770）、只绑回环，读写的是 Phase 1 建的那个独立核心库
（`core/companion_core.db`），**不碰** `conversation/conversations.db`，
也不动 Memory V1/V1.1。

## 为什么另起一个服务，而不是改 webui.py

任务书的原则写得很清楚：**不要把 Muse 的每轮对话转发到 DSH `/api/chat`**、
**不要直接开放 SQLite**、**共生负责主体状态/记忆/事件/通信基础设施**。

webui.py 是「小满自己的网页界面」，它内部是单会话 + 全局锁，天然不适合当
多环境网关。所以这里做一层干净的 JSON API，两边各自用自己的模型和运行环境，
只在「主体状态」这一层交汇。

## 接口清单（M1）

    GET  /api/health                    健康检查（不需要认证，方便探活）
    GET  /api/subject                   主体 + 全部实例 + 事件游标
    GET  /api/persona                   人格（只读）
    GET  /api/relationship              关系状态（只读）
    GET  /api/state                     主体状态汇总
    GET  /api/instances                 实例列表
    POST /api/instances/register        登记一个新实例
    GET  /api/memory/search?q=&k=       记忆检索（只读、无副作用）
    GET  /api/memory/candidates         记忆候选列表
    POST /api/memory/propose            提交记忆候选（不进正式记忆）
    GET  /api/events?since=&limit=      事件增量拉取（游标）
    POST /api/events                    提交主体事件（幂等）

## 认证

两条通道，**Bearer 优先**：

    Authorization: Bearer <token>          ← 给脚本/服务用（Muse 用这个）
    Cookie: xiaoman_token=<token>          ← 保留原有网页端方式

令牌来源两个文件任一即可：
    core/api-token                          （本服务自己的）
    %LOCALAPPDATA%/xiaoman/webui-token      （网页端那份，兼容）

用 Cookie 认证时，POST 必须额外带 `X-Xiaoman-Request: 1`（防 CSRF，沿用网页端的约定）；
用 Bearer 时不需要——脚本不会被浏览器自动带凭据。

启动：
    python src\\core_api.py                  # 默认 127.0.0.1:8770
    python src\\core_api.py --port 8770 --print-token
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import companion_core as core
import core_fs as fs
import dispatcher
import risk_gate


def _setup_stdout() -> None:
    """只在当脚本跑时改 stdout；被 import 时不动（否则会重复包装、关掉上层的流）。"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = core.CORE_DIR / "api-token"
WEBUI_TOKEN_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "xiaoman" / "webui-token"
MAX_BODY = 1_000_000


def _notify_approval(ap: dict) -> None:
    """CE-4 档一：待批审批单推手机（ntfy，best-effort）。

    推不到**绝不挡审批流程**——没配 NTFY_TOPIC 就静默跳过，
    审批单仍在 /api/approvals/list 和看板上可见。审批动作本身走
    /api/approvals/decide。
    T6（2026-11-21）：通知带「批准/拒绝」按钮（APK NtfyService 解析渲染），
    手机一键批由 APP 直接调 webui 代理（decided_by=boss-phone 留痕）。
    """
    try:
        from heartbeat import (build_approval_actions, deliver_to_ntfy,
                               ntfy_topic_from_env)
        topic = ntfy_topic_from_env()
        if not topic:
            return
        deliver_to_ntfy(topic, (
            f"⏸ 待批准 {ap['approval_id']}：{ap['action']} {ap['target']}\n"
            f"理由：{ap.get('reason') or '-'}\n"
            f"（点通知上的按钮批准/拒绝，或 webui 审批面板）"),
            actions=build_approval_actions(ap.get("approval_id")))
    except Exception:  # noqa: BLE001 —— 通知失败不影响主流程
        pass

API_VERSION = "m1"

# 两个主体并存：
#   heci_001    鹤辞（当前开发者主体）—— 资料在 core/ 下
#   xiaoman_001 小满（原测试主体，数据保留，只读）—— 资料在原目录
LEGACY_SUBJECT_ID = "xiaoman_001"
KNOWN_SUBJECTS = (core.DEFAULT_SUBJECT_ID, LEGACY_SUBJECT_ID)


def _subject_paths(subject_id: str) -> dict:
    """按主体返回资料位置。**两套互不覆盖**，这就是「不共用主体身份」的落地。

    2026-11-04 修（Muse 审查发现）：原来是二值兜底 —— 传任何不存在的
    subject_id 都落到鹤辞分支，于是 `?subject_id=随便编一个` 会返回鹤辞的人格、
    还把 subject_id 标成那个假名字（冒名顶替）。现在未知主体直接拒。
    """
    if subject_id not in KNOWN_SUBJECTS:
        raise ValueError(f"未知主体：{subject_id}（已知：{', '.join(KNOWN_SUBJECTS)}）")
    if subject_id == LEGACY_SUBJECT_ID:
        return {
            "persona": ROOT / "personality" / "personality.json",
            "relationship_file": None,
            "relationship_dir": ROOT / "relationship",
            "memory_dir": ROOT / "memory" / "versions",
            "legacy": True,
        }
    return {
        "persona": core.CORE_DIR / "persona-heci.json",
        # 2026-11-06 按用户要求：**关系直接用小满那份**。
        # 理由是关系史是「你和我」这一段，不该因为换了主体编号就归零。
        # 人格仍然各用各的（鹤辞是开发者人格），记忆也各存各的。
        "relationship_file": None,
        "relationship_dir": ROOT / "relationship",
        "memory_dir": core.CORE_DIR / "memory-heci",
        "legacy": False,
    }

# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def ensure_token() -> str:
    """本服务的令牌：没有就生成一个，存 `core/api-token`。"""
    if TOKEN_FILE.is_file():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    fresh = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(fresh, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return fresh


def accepted_tokens() -> list[str]:
    out = []
    if TOKEN_FILE.is_file():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            out.append(t)
    if WEBUI_TOKEN_FILE.is_file():
        t = WEBUI_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            out.append(t)
    return out


def _host_ok(host: str) -> bool:
    """只允许本机 / tailnet 访问。与网页端同一套判断，独立实现避免耦合。"""
    if host in {"127.0.0.1", "localhost", "[::1]"}:
        return True
    if host.endswith(".ts.net"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "100":
        try:
            return 64 <= int(parts[1]) <= 127
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# 读现有数据（只读，不改）
# ---------------------------------------------------------------------------


def persona_summary(subject_id: str = core.DEFAULT_SUBJECT_ID) -> dict:
    p = _subject_paths(subject_id)["persona"]
    if not p.is_file():
        return {"available": False, "subject_id": subject_id}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        "available": True,
        "subject_id": subject_id,
        "legacy": _subject_paths(subject_id)["legacy"],
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "personality_count": len(data.get("personality") or []),
        "values_count": len(data.get("values") or []),
        "communication_style": data.get("communication_style", ""),
        "boundaries": data.get("boundaries", ""),
        "role": data.get("role", ""),
        # 主体认知：只给「当前结论 + 依据」，不吐 system_prompt 全文
        "cognition": (data.get("cognition") or {}).get("current", []),
    }


def latest_relationship(subject_id: str = core.DEFAULT_SUBJECT_ID) -> dict:
    paths = _subject_paths(subject_id)
    direct = paths.get("relationship_file")
    if direct is not None:
        if not direct.is_file():
            return {"available": False, "subject_id": subject_id}
        data = json.loads(direct.read_text(encoding="utf-8"))
        inner = data.get("state") if isinstance(data.get("state"), dict) else data
        return {
            "available": True,
            "subject_id": subject_id,
            "file": direct.name,
            "version": data.get("version"),
            "created_at": data.get("created_at"),
            "state": inner,
        }
    d = paths["relationship_dir"]
    if not d.is_dir():
        return {"available": False, "subject_id": subject_id}
    files = sorted(d.glob("state_v*.json"), key=lambda x: x.name)
    if not files:
        return {"available": False, "subject_id": subject_id}
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    inner = data.get("state") if isinstance(data.get("state"), dict) else data
    return {
        "available": True,
        "subject_id": subject_id,
        "file": files[-1].name,
        "version": data.get("version"),
        "created_at": data.get("created_at"),
        "state": inner,
    }


def _ver_key(name: str) -> tuple:
    """把 3.1 / 3.10 这样的版本号变成可排序的元组（否则字符串排序会把 3.10 排到 3.2 前面）。"""
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) or (0,)


def latest_user_model(section: str | None = None) -> dict:
    """用户模型：鹤辞对「你是谁」的认识。

    ## 两层，别混

    · **profile**（主）：用户的个人动态认知模型 3.1，30 节 5883 字。
      来自 `user_model/songci-cognition-*.json`（由 `import_user_model.py` 从 docx 导入）。
    · **facts**（辅）：`user_model/model_vNNN.json` 里那几条零散事实（我叫用户、
      晚上才有精神……），是系统自动抓的，会随对话变。

    2026-11-06 调整：**以 profile 为主**。之前只返回 facts，那不是"用户模型"，
    只是零星事实。

    `section` 参数可以只取一节（比如 `?section=21` 取 AI 定位），省带宽也省上下文。
    """
    out: dict = {"available": False, "profile": None, "facts": None}

    # ---- 主：认知模型 ----
    prof_files = sorted(
        (p for p in (ROOT / "user_model").glob("songci-cognition-*.json") if p.is_file()),
        key=lambda p: _ver_key(p.stem))
    if prof_files:
        data = json.loads(prof_files[-1].read_text(encoding="utf-8"))
        secs = data.get("sections") or []
        if section:
            want = str(section).strip().lstrip("0") or "0"
            secs = [s for s in secs
                    if str(s.get("no", "")).lstrip("0") == want]
        out["profile"] = {
            "title": data.get("title"),
            "version": data.get("version"),
            "source_file": data.get("source_file"),
            "imported_at": data.get("imported_at"),
            "char_count": data.get("char_count"),
            "section_count": len(data.get("sections") or []),
            "file": prof_files[-1].name,
            "sections": [
                {"no": s.get("no"), "title": s.get("title"),
                 "text": "\n".join(s.get("lines") or [])}
                for s in secs
            ],
        }
        out["available"] = True

    # ---- 辅：零散事实 ----
    d = ROOT / "user_model"
    fact_files = sorted((p for p in d.glob("model_v*.json") if p.is_file()),
                        key=lambda p: _ver_key(p.stem))
    if fact_files:
        data = json.loads(fact_files[-1].read_text(encoding="utf-8"))
        raw_items = data.get("items") or []
        # 字段叫 belief 不叫 text（2026-11-06 实测发现：按 text 取会全是空串）。
        items = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            items.append({
                **it,
                "text": str(it.get("belief") or it.get("text") or ""),
                "superseded": bool(it.get("superseded_by")),
            })
        # 有效条数按 user_model.py:987 的算法现算，**不用文件里的 active_count**。
        # 实测 model_v009.json 里 active_count=4，但按算法算出来是 6 ——
        # 那个字段是写入时的快照，和明细对不上。汇总与明细打架时以明细为准。
        active_n = sum(
            1 for it in items
            if not it["superseded"] and str(it.get("status")) in ("inferred", "confirmed")
        )
        out["facts"] = {
            "file": fact_files[-1].name,
            "version": data.get("version"),
            "created_at": data.get("created_at"),
            "item_count": len(items),
            "active_count": active_n,
            "items": items,
        }
        out["available"] = True

    return out


def memory_items(subject_id: str = core.DEFAULT_SUBJECT_ID) -> tuple[str | None, list[dict]]:
    """读该主体当前版本的记忆条目。只读文件，不写 retrievals。"""
    d = _subject_paths(subject_id)["memory_dir"]
    if not d.is_dir():
        return None, []
    cands = [f for f in d.glob("v*.json") if re.fullmatch(r"v\d+\.json", f.name)]
    if not cands:
        return None, []
    latest = max(cands, key=lambda f: int(f.stem[1:]))
    data = json.loads(latest.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    return latest.stem, items or []


def memory_search(query: str, k: int = 8, subject_id: str = core.DEFAULT_SUBJECT_ID) -> dict:
    """轻量只读检索（无副作用）。

    说明：正式的对话内检索仍走 `retrieve.py`（它会落 `retrievals` 表）。
    这里是给外部实例用的查询，所以只读当前版本做打分，不写库。
    """
    version, items = memory_items(subject_id)
    q = (query or "").strip()
    if not q:
        return {"subject_id": subject_id, "version": version, "query": q, "hits": []}
    terms = [t.lower() for t in re.split(r"\s+", q) if t]
    scored = []
    for it in items:
        text = str(it.get("text", ""))
        low = text.lower()
        score = 0
        for t in terms:
            score += low.count(t)
        if q.lower() in low:
            score += 3
        if score:
            scored.append({"score": score, "type": it.get("type", ""), "text": text})
    scored.sort(key=lambda x: -x["score"])
    return {"subject_id": subject_id, "version": version, "query": q,
            "hits": scored[: max(1, min(k, 50))]}


# ---------------------------------------------------------------------------
# 这个文件自己用的小工具
# ---------------------------------------------------------------------------


def _jsonable(obj):
    """把返回结构里的 datetime 等对象转成 JSON 可序列化形式（G-2）。

    心跳 tick 的返回里混有 datetime 字段；_json 用默认 json.dumps，
    碰到非原生类型就 500。这里递归转换：datetime/date → ISO 字符串，
    其余原样（仍不可序列化的类型会让 _json 照常报错——不掩盖）。
    """
    from datetime import date as _date, datetime as _datetime

    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (_datetime, _date)):
        return obj.isoformat()
    return obj


def state_summary(conn, subject_id: str = core.DEFAULT_SUBJECT_ID) -> dict:
    info = core.subject_info(conn, subject_id)
    version, items = memory_items(subject_id)
    rel = latest_relationship(subject_id)
    persona = persona_summary(subject_id)
    um = latest_user_model()
    return {
        "subject_id": subject_id,
        "instances": len(info["instances"]),
        "event_count": info["event_count"],
        "cursor": info["cursor"],
        "memory_version": version,
        "memory_item_count": len(items),
        "relationship_stage": (rel.get("state") or {}).get("stage") if rel.get("available") else None,
        "relationship_source": rel.get("file"),
        "user_profile_version": (um.get("profile") or {}).get("version"),
        "user_profile_sections": (um.get("profile") or {}).get("section_count"),
        "user_model_version": (um.get("facts") or {}).get("version"),
        "user_model_active": (um.get("facts") or {}).get("active_count"),
        "persona_name": persona.get("name") if persona.get("available") else None,
        "pending_candidates": len(core.list_candidates(conn, subject_id=subject_id)),
        "legacy": _subject_paths(subject_id)["legacy"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CompanionCore/" + API_VERSION
    protocol_version = "HTTP/1.1"

    # --- 输出 ---------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"ok": False, "error": msg})

    def log_message(self, fmt, *args):  # 安静一点，只留错误
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("[core_api] " + (fmt % args) + "\n")

    # --- 认证 ---------------------------------------------------------------
    def _auth_kind(self) -> str | None:
        """返回 'bearer' / 'cookie' / None。Bearer 优先。"""
        good = accepted_tokens()
        if not good:
            return None
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
            if any(secrets.compare_digest(given, t) for t in good):
                return "bearer"
            return None
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "xiaoman_token" and v:
                if any(secrets.compare_digest(v, t) for t in good):
                    return "cookie"
        return None

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host and not _host_ok(host):
            self._err(403, f"Host 不被允许：{host}")
            return False
        kind = self._auth_kind()
        if kind is None:
            self._err(401, "未授权：需要 Authorization: Bearer <token> 或 xiaoman_token Cookie")
            return False
        if self.command == "POST":
            if kind == "cookie" and self.headers.get("X-Xiaoman-Request") != "1":
                self._err(403, "Cookie 调用 POST 必须带 X-Xiaoman-Request: 1")
                return False
        return True

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            self._err(413, "请求体过大")
            return None
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._err(400, f"请求体不是合法 JSON：{exc}")
            return None
        if not isinstance(data, dict):
            self._err(400, "请求体必须是 JSON 对象")
            return None
        return data

    def _conn(self):
        conn = core.connect()
        core.init_schema(conn)
        core.ensure_subject(conn)
        return conn

    # --- 路由 ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(url.query).items()}

        if route == "/api/health":
            self._json(200, {"ok": True, "service": "companion-core", "api": API_VERSION})
            return

        if not self._guard():
            return

        conn = self._conn()
        sid = q.get("subject_id") or core.DEFAULT_SUBJECT_ID

        # 中枢制 v2：HealthBoard（每部件一条健康记录，看板读它定位坏零件）
        if route == "/api/health/components":
            self._json(200, {"ok": True,
                             "components": core.list_component_health(conn)})
            return

        # 审批清单 GET（webui 卡片拉取用；POST 区的同名路由给程序用）
        if route == "/api/approvals/list":
            status = q.get("status") or "pending"
            self._json(200, {"ok": True,
                             "approvals": core.list_approvals(
                                 conn, status=(status if status != "all" else None),
                                 limit=int(q.get("limit") or 50))})
            return
        try:
            if route == "/api/subject":
                self._json(200, {"ok": True, **core.subject_info(conn, sid)})
            elif route == "/api/subjects":
                rows = conn.execute(
                    "SELECT * FROM subjects ORDER BY created_at").fetchall()
                self._json(200, {"ok": True, "subjects": [dict(r) for r in rows]})
            elif route == "/api/instances":
                self._json(200, {"ok": True,
                                 "instances": core.instances_status(conn, sid),
                                 "online_window_seconds": core.ONLINE_WINDOW_SECONDS})
            elif route == "/api/inbox":
                iid = q.get("instance_id") or ""
                if not iid:
                    self._err(400, "缺少 instance_id")
                    return
                self._json(200, {"ok": True, "inbox": core.inbox_summary(conn, iid)})
            elif route == "/api/persona":
                self._json(200, {"ok": True, "persona": persona_summary(sid)})
            elif route == "/api/relationship":
                self._json(200, {"ok": True, "relationship": latest_relationship(sid)})
            elif route == "/api/user_model":
                self._json(200, {"ok": True, "user_model": latest_user_model(
                    q.get("section") or None)})
            elif route == "/api/state":
                self._json(200, {"ok": True, "state": state_summary(conn, sid)})
            elif route == "/api/memory/search":
                k = int(q.get("k") or 8)
                self._json(200, {"ok": True, **memory_search(q.get("q", ""), k, sid)})
            elif route == "/api/memory/retrieve":
                # G-1（双端互通 2026-11-15）：把**正式检索引擎**暴露给外部实例。
                # /api/memory/search 是轻量子串计分（无 tokenize/扩词/recency
                # tie-break，正是 bench 同义词 gap 的同款病）；本路由走主链同款
                # memory.retrieve 引擎，**只读、不落 retrievals 表** —— 正式对话
                # 内检索仍走 retrieve.py 的落盘路径，两侧口径见
                # docs/双端互通-研究报告-20261114.md。
                import memory as memory_mod

                q_text = (q.get("q") or "").strip()
                k = max(1, min(int(q.get("k") or 5), 50))
                mv = memory_mod.load_latest()
                hits = []
                if mv and q_text:
                    for it in memory_mod.retrieve(mv, q_text, top_k=k):
                        hits.append({
                            "id": it.id, "type": it.type, "category": it.category,
                            "created_at": it.created_at, "text": it.text,
                        })
                self._json(200, {
                    "ok": True, "engine": "keyword+2gram+recency-tiebreak",
                    "version": mv.version if mv else None,
                    "query": q_text, "k": k, "hits": hits,
                })
            elif route == "/api/memory/candidates":
                rows = core.list_candidates(
                    conn, status=q.get("status", "pending") or None,
                    limit=int(q.get("limit") or 50))
                self._json(200, {
                    "ok": True,
                    "candidates": [
                        {**dict(r), "items": json.loads(r["items"])} for r in rows
                    ],
                })
            elif route == "/api/events":
                since = int(q.get("since") or 0)
                limit = int(q.get("limit") or 100)
                rows = core.events_since(
                    conn, since_cursor=since, limit=limit,
                    exclude_instance=q.get("exclude_instance") or None)
                self._json(200, {
                    "ok": True,
                    "since": since,
                    "cursor": core.latest_cursor(conn),
                    "events": [dict(r) for r in rows],
                })
            elif route == "/api/tasks/pool":
                # 任务池公共栏（2026-11-21，Muse 设计）：所有席位可读
                self._json(200, {"ok": True,
                                 "tasks": core.pool_list(
                                     conn, limit=int(q.get("limit") or 50))})
            elif route.startswith("/api/tasks/"):
                tid = route[len("/api/tasks/"):]
                self._json(200, {"ok": True, **core.get_task(conn, tid)})
            elif route == "/api/tasks":
                iid = q.get("instance_id") or ""
                rows = core.list_tasks(
                    conn, instance_id=iid,
                    role=q.get("role") or "inbox",
                    status=q.get("status") or None,
                    limit=int(q.get("limit") or 50))
                self._json(200, {"ok": True, "tasks": [dict(r) for r in rows]})
            elif route == "/api/leases":
                self._json(200, {"ok": True, "leases": core.list_leases(conn)})
            elif route == "/api/rate":
                # 本进程的限速状态 + 账本里最近的限速事件。
                # 后者才跨环境有用：另一个实例被限速时，这边看得到。
                import rate_guard

                recent = conn.execute(
                    "SELECT event_id, recorded_at, instance_id, "
                    "event_content AS content "
                    "FROM subject_events WHERE subject_id=? AND event_type='rate_limit' "
                    "ORDER BY rowid DESC LIMIT 5", (sid,)).fetchall()
                self._json(200, {
                    "ok": True,
                    "this_process": rate_guard.guard().snapshot(),
                    "recent_in_ledger": [dict(r) for r in recent],
                })
            elif route == "/api/fs/map":
                self._json(200, {"ok": True, "map": fs.project_map()})
            elif route == "/api/fs/list":
                self._json(200, {"ok": True, "path": q.get("path", "."),
                                 "entries": fs.list_dir(q.get("path", "."))})
            elif route == "/api/fs/read":
                self._json(200, {"ok": True, **fs.read_text(
                    q.get("path", ""), int(q.get("start_line") or 1),
                    int(q.get("max_bytes") or fs.MAX_READ_BYTES))})
            else:
                self._err(404, f"没有这个接口：GET {route}")
        except (ValueError, sqlite3.Error) as exc:
            self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802
        route = (urlparse(self.path).path.rstrip("/") or "/")
        if not self._guard():
            return
        payload = self._body()
        if payload is None:
            return
        conn = self._conn()
        try:
            if route == "/api/events":
                iid = str(payload.get("instance_id") or "").strip()
                if not iid:
                    self._err(400, "缺少 instance_id")
                    return
                eid, created = core.append_event(
                    conn,
                    instance_id=iid,
                    event_type=str(payload.get("event_type") or "experience"),
                    content=str(payload.get("content") or ""),
                    source_environment=str(payload.get("source_environment") or ""),
                    occurred_at=payload.get("occurred_at"),
                    participants=payload.get("participants"),
                    task_id=payload.get("task_id"),
                    result=payload.get("result"),
                    provenance=payload.get("provenance"),
                    dedup_key=payload.get("dedup_key"),
                    supersedes=payload.get("supersedes"),
                )
                self._json(200, {"ok": True, "event_id": eid, "created": created,
                                 "cursor": core.latest_cursor(conn)})
                return

            if route == "/api/heartbeat/tick":
                # G-2（双端互通 2026-11-15）：云端触发一次心跳检查。
                # 复用 _heartbeat_worker_once —— 与 chat/定时 worker 走**同一条**
                # maybe_tick 通路（MIN_GAP 节流 / pending 压制 / 留痕同规则），
                # 全程零模型调用（heartbeat 纪律）。被节流时如实返回 status，
                # 不做"force"口子 —— 绕过节流 = 重复推送事故的重演。
                out = _heartbeat_worker_once(conn=None)
                self._json(200, {"ok": True, **_jsonable(out)})
                return

            if route == "/api/proactive/note":
                # G-3（双端互通 2026-11-15）：外部实例注入一条「想对用户说的话」。
                # 留痕在前（record_pending_action）→ 推送在后 —— 与心跳同纪律
                # （投递失败不影响落盘，不丢意图）。兑现走既有通道：用户开口时
                # 注入（chat）或心跳 tick。pending_actions 在 conversations.db
                # （storage），与本进程的 core 库分开连，用完即关。
                import storage as storage_mod

                msg = str(payload.get("message") or payload.get("text") or "").strip()
                if not msg:
                    self._err(400, "缺少 message")
                    return
                iid = str(payload.get("instance_id") or "").strip()
                kind = str(payload.get("kind") or "external_note")
                reason = str(payload.get("reason") or f"external:{iid or 'unknown'}")
                sconn = storage_mod.connect()
                try:
                    pending_id = storage_mod.record_pending_action(
                        sconn,
                        run_id=f"note_{secrets.token_hex(6)}",
                        kind=kind,
                        message=msg[:2000],
                        reason=reason,
                    )
                    pushed = False
                    if payload.get("push"):
                        import heartbeat as hb_mod

                        topic = hb_mod.ntfy_topic_from_env()
                        if topic:
                            pushed = hb_mod.deliver_to_ntfy(topic, msg[:2000])
                            if pushed:
                                storage_mod.mark_pending_pushed(sconn, pending_id)
                    sconn.commit()
                finally:
                    sconn.close()
                self._json(200, {"ok": True, "pending_id": pending_id,
                                 "pushed": pushed,
                                 "deliver": "用户开口时注入或心跳 tick 兑现"})
                return

            if route == "/api/memory/propose":
                iid = str(payload.get("instance_id") or "").strip()
                items = payload.get("items") or []
                if not iid:
                    self._err(400, "缺少 instance_id")
                    return
                if not isinstance(items, list) or not items:
                    self._err(400, "items 必须是非空数组")
                    return
                clean = []
                for it in items[:20]:
                    if not isinstance(it, dict) or not str(it.get("text") or "").strip():
                        self._err(400, "每条候选必须带 text")
                        return
                    clean.append({
                        "type": str(it.get("type") or "FACT"),
                        "text": str(it["text"]).strip()[:2000],
                        "evidence": str(it.get("evidence") or "")[:500],
                    })
                cid, created = core.propose_memory_candidates(
                    conn, instance_id=iid, items=clean,
                    source_environment=str(payload.get("source_environment") or ""),
                    note=payload.get("note"),
                    client_request_id=payload.get("client_request_id"),
                )
                self._json(200, {"ok": True, "candidate_id": cid, "created": created,
                                 "status": "pending"})
                return

            if route == "/api/instances/register":
                sid = str(payload.get("subject_id") or core.DEFAULT_SUBJECT_ID)
                env = str(payload.get("environment") or "").strip()
                if not env:
                    self._err(400, "缺少 environment")
                    return
                core.ensure_subject(conn, sid)
                iid = core.register_instance(conn, sid, env, payload.get("capabilities"))
                core.touch_instance(conn, iid)
                self._json(200, {"ok": True, "instance_id": iid, "subject_id": sid})
                return

            if route == "/api/instances/heartbeat":
                iid = str(payload.get("instance_id") or "").strip()
                if not iid:
                    self._err(400, "缺少 instance_id")
                    return
                window = core.touch_instance(conn, iid)
                self._json(200, {
                    "ok": True,
                    "instance_id": iid,
                    "next_heartbeat_in": max(10, window // 2),
                    "instances": core.instances_status(conn),
                    "inbox": core.inbox_summary(conn, iid),
                })
                return

            if route == "/api/tasks":
                t, created = core.create_task(
                    conn,
                    from_instance=str(payload.get("from_instance") or ""),
                    to_instance=str(payload.get("to_instance") or ""),
                    title=str(payload.get("title") or "").strip()[:200],
                    detail=payload.get("detail"),
                    context=payload.get("context"),
                    max_rounds=int(payload.get("max_rounds") or 3),
                    client_request_id=payload.get("client_request_id"),
                    risk=payload.get("risk"),
                    # 任务池（2026-11-21，Muse 设计）：to_instance="pool" 挂公共栏
                    claim_deadline_seconds=payload.get("claim_deadline_seconds"),
                    fallback_to_pool=bool(payload.get("fallback_to_pool") or False),
                    fallback_after_seconds=payload.get("fallback_after_seconds"),
                )
                self._json(200, {"ok": True, "task_id": t, "created": created,
                                 "status": "SENT"})
                return

            if route.startswith("/api/tasks/") and route.endswith("/act"):
                tid = route[len("/api/tasks/"):-len("/act")]
                out = core.task_action(
                    conn, tid,
                    actor_instance=str(payload.get("instance_id") or ""),
                    action=str(payload.get("action") or ""),
                    note=payload.get("note"),
                    result=payload.get("result"),
                )
                self._json(200, {"ok": True, **out})
                return

            # --- 任务池（2026-11-21，Muse 设计/老板拍板）------------------------
            if route.startswith("/api/tasks/") and route.endswith("/claim"):
                tid = route[len("/api/tasks/"):-len("/claim")]
                out = core.claim_task(conn, tid,
                                      instance_id=str(payload.get("instance_id") or ""))
                self._json(200, {"ok": True, **out})
                return
            if route.startswith("/api/tasks/") and route.endswith("/release"):
                tid = route[len("/api/tasks/"):-len("/release")]
                out = core.release_task(conn, tid,
                                        instance_id=str(payload.get("instance_id") or ""),
                                        reason=str(payload.get("reason") or ""))
                self._json(200, {"ok": True, **out})
                return
            if route.startswith("/api/tasks/") and route.endswith("/assign"):
                tid = route[len("/api/tasks/"):-len("/assign")]
                out = core.assign_task(conn, tid,
                                       to_instance=str(payload.get("to_instance") or ""),
                                       actor_instance=str(payload.get("instance_id") or ""))
                self._json(200, {"ok": True, **out})
                return
            if route.startswith("/api/tasks/") and route.endswith("/pool/rearm"):
                tid = route[len("/api/tasks/"):-len("/pool/rearm")]
                out = core.pool_rearm(conn, tid,
                                      extra_seconds=int(payload.get("extra_seconds") or 0),
                                      actor_instance=str(payload.get("instance_id") or ""))
                self._json(200, {"ok": True, **out})
                return

            if route == "/api/leases/acquire":
                out = core.acquire_lease(
                    conn,
                    str(payload.get("path") or ""),
                    str(payload.get("instance_id") or ""),
                    ttl_seconds=int(payload.get("ttl_seconds") or core.LEASE_DEFAULT_TTL),
                    task_id=payload.get("task_id"),
                    note=payload.get("note"),
                )
                self._json(200, {"ok": True, **out})
                return

            if route == "/api/leases/release":
                released = core.release_lease(
                    conn, str(payload.get("path") or ""),
                    str(payload.get("instance_id") or ""))
                self._json(200, {"ok": True, "released": released})
                return

            # --- CE-4 档一（2026-11-08）：审批单 --------------------------------
            if route == "/api/approvals/list":
                _st = payload.get("status")
                rows = core.list_approvals(
                    conn, status=(str(_st) if _st else None),
                    limit=int(payload.get("limit") or 50))
                self._json(200, {"ok": True, "approvals": rows})
                return
            if route == "/api/approvals/decide":
                out = core.decide_approval(
                    conn, str(payload.get("approval_id") or ""),
                    decision=str(payload.get("decision") or ""),
                    decided_by=str(payload.get("decided_by") or "boss"),
                    note=payload.get("note"))
                self._json(200, {"ok": True, "approval": out})
                return

            # 微信直发回执（方案 B，2026-11-08）：云端桥经鉴权 API 回状态，
            # 不给 core/sync 只读区开口子。
            if route == "/api/weixin/receipt":
                out = dispatcher.apply_weixin_receipt(
                    str(payload.get("msg_id") or ""),
                    str(payload.get("status") or ""),
                    error=payload.get("error"))
                self._json(200, {"ok": True, "receipt": out})
                return
            if route == "/api/approvals/get":
                ap = core.get_approval(conn, str(payload.get("approval_id") or ""))
                if ap is None:
                    self._err(404, "审批单不存在")
                    return
                self._json(200, {"ok": True, "approval": ap})
                return

            # --- 文件接口：写操作强制要求先领租约（防双实例同改）-------------
            if route.startswith("/api/fs/"):
                action = route[len("/api/fs/"):]
                iid = str(payload.get("instance_id") or "").strip()
                rel = str(payload.get("path") or "").strip()
                if action in ("write", "mkdir", "delete", "exec"):
                    if not iid:
                        self._err(400, "缺少 instance_id")
                        return
                    # exec 不针对具体文件，所以不查租约，但必须证明身份
                    if conn.execute("SELECT 1 FROM instances WHERE instance_id=?",
                                    (iid,)).fetchone() is None:
                        self._err(403, f"实例未登记：{iid}")
                        return
                if action in ("write", "mkdir", "delete"):
                    # fencing（2026-11-07，外部调研 #8）：写请求必须携带 acquire
                    # 响应里的 lease_epoch。每次 acquire 都让该 path 的 epoch +1，
                    # 旧 epoch 写入一律 409 —— 防止「租约被抢/过期重发后，
                    # 旧持有者继续写」的脑裂（Kleppmann fencing token）。
                    lease_epoch = payload.get("lease_epoch")
                    if lease_epoch is None:
                        self._err(409, f"{rel} 缺少 lease_epoch（fencing）。"
                                       f"先 POST /api/leases/acquire（path={rel}, "
                                       f"instance_id={iid}），从响应 epoch 取值随写请求携带。")
                        return
                    if not core.holds_lease(conn, rel, iid, epoch=int(lease_epoch)):
                        self._err(409, f"{rel} 租约无效或 epoch 已过期"
                                       f"（可能已被重新领取）——请重新 acquire 后再写。")
                        return
                # --- CE-4 档一（2026-11-08）：风险闸（单一强制点）-------------
                # 高危（delete/exec/改配置人格）必须先拿老板批单：无 approval_id
                # → 开单+推手机+403；有 → 消费（单次、动作/目标对得上才算）。
                # 闸放在租约校验**之后**：租约 409 不会白白烧掉一张批单。
                # deny 类黑名单（core_fs 的 ROOT 守卫 / DENY 规则）在执行层仍然
                # 生效且优先级更高——批了也进不去。
                if action in ("write", "mkdir", "delete", "exec"):
                    gate = risk_gate.classify(
                        action, path=rel, cmd=list(payload.get("cmd") or []))
                    if gate["tier"] == risk_gate.TIER_HIGH:
                        _tgt = (" ".join(str(c) for c in (payload.get("cmd") or []))
                                if action == "exec" else rel)
                        ap_id = str(payload.get("approval_id") or "")
                        if not ap_id:
                            ap = core.create_approval(
                                conn, action=action, target=_tgt, risk="high",
                                instance_id=iid, reason=gate["reason"])
                            _notify_approval(ap)
                            self._err(
                                403,
                                f"高危动作需要老板批准（{gate['reason']}）。"
                                f"approval_id={ap['approval_id']} —— 已推送手机。"
                                "批准后：先 POST /api/leases/acquire 拿 lease_epoch"
                                "（exec 免租约），再携带 approval_id 重试。")
                            return
                        try:
                            core.consume_approval(conn, ap_id, action=action,
                                                  target=_tgt)
                        except ValueError as e:
                            self._err(403, f"批准无效：{e}")
                            return
                if action == "write":
                    out = fs.write_text(rel, str(payload.get("content") or ""))
                elif action == "mkdir":
                    out = fs.mkdir(rel)
                elif action == "delete":
                    out = fs.remove(rel)
                    # 删完就放租约：否则文件没了、租约还挂着，别人建同名文件会被挡。
                    core.release_lease(conn, rel, iid)
                elif action == "exec":
                    _cmd = [str(c) for c in (payload.get("cmd") or [])]
                    # P1（exec 租约上下文，2026-11-08）：git checkout 等可写
                    # 子命令会覆写工作区——他人持租约时必须先被租约闸拦下。
                    core.assert_exec_respects_leases(conn, _cmd, iid)
                    out = fs.run(_cmd)
                else:
                    self._err(404, f"没有这个文件操作：{action}")
                    return
                self._json(200, {"ok": True, **out})
                return

            self._err(404, f"没有这个接口：POST {route}")
        except core.TaskClaimConflict as exc:
            self._err(409, str(exc))   # 池任务已被其他席位认领
        except (ValueError, sqlite3.Error) as exc:
            self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    def _405(self) -> None:
        self._err(405, "本服务只接受 GET 与 POST")

    do_PUT = _405  # noqa: N815
    do_DELETE = _405  # noqa: N815
    do_PATCH = _405  # noqa: N815


def _start_memory_worker(interval: int = 300) -> None:
    """后台整理员：每隔一段时间看一次有没有待整理的记忆候选。

    为什么需要它：候选（云端提上来的那些"值得记的事"）以前要人手跑
    `python src\\core_candidates.py` 才会被处理。不跑就永远堆着，日记不会长。

    它只做一件事：调 `core_candidates.run()`。所有判定规则（接纳/合并/暂缓/拒绝、
    否定检测、幂等）都在那边，这里不重复实现，也不绕过。

    开关：环境变量 `HEXI_AUTO_CONSUME=0` 可关掉。
    """
    import os
    import threading
    import time

    if os.environ.get("HEXI_AUTO_CONSUME", "1") == "0":
        print("记忆整理员: 已禁用（HEXI_AUTO_CONSUME=0）")
        return

    def loop() -> None:
        import core_candidates

        while True:
            try:
                # 处方 A（2026-11-08，老板拍板「整」）：经历提炼前置步——
                # 扫新对话的情绪峰值 → EPISODE 候选进提案池，随后照常被
                # 整理员按条目粒度接纳。失败不拖垮整理员本体。
                try:
                    import episode_scan
                    ep = episode_scan.run_episode_scan()
                    if ep["proposed"]:
                        print(f"[经历提炼] 扫 {ep['scanned']} 轮 → 提案 "
                              f"{len(ep['proposed'])} 条 EPISODE（等整理员判定）")
                except Exception as exc:  # noqa: BLE001
                    print(f"[经历提炼] 本轮跳过：{type(exc).__name__}: {exc}")
                out = core_candidates.run(limit=50)
                # 链桥（处方 A 收口，2026-11-08）：core 链新条目同步进主链——
                # 她的聊天检索只读主链，不桥接 = 记忆在账上、不在脑子里。
                try:
                    import chain_sync
                    cs = chain_sync.sync_core_to_main()
                    if cs.get("added"):
                        print(f"[链桥] core→主链 同步 {cs['added']} 条 → {cs['version']}")
                except Exception as exc:  # noqa: BLE001 —— 桥失败不拖垮整理员
                    print(f"[链桥] 本轮跳过：{type(exc).__name__}: {exc}")
                st = out["stats"]
                if st["scan"]:
                    print(f"[整理员] 扫描 {st['scan']} 条 → 接纳 {st['accepted']} "
                          f"合并 {st['merged']} 暂缓 {st['deferred']} "
                          f"拒绝 {st['rejected']} 出错 {st['errors']}"
                          + (f" → 新版本 {out['new_version']}" if out["new_version"] else ""))
            except Exception as exc:  # noqa: BLE001 — 整理失败不能拖垮服务
                print(f"[整理员] 本次跳过：{type(exc).__name__}: {exc}")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="memory-worker", daemon=True)
    t.start()
    print(f"记忆整理员: 已启动，每 {interval} 秒检查一次待整理候选")


def _heartbeat_worker_once(conn=None, settings=None) -> dict:
    """心跳生产者的**单轮**（P1：schedule 走 maybe_tick，2026-11-08）。

    之前每 interval 秒直接 `tick(trigger=schedule)` —— 绕过了 maybe_tick 的
    MIN_GAP 节流，webui/chat 刚触发过心跳的相邻窗口里，schedule 触发会
    再跑一遍 decide → 重复推送/重复意图（P1 清单第 3 条）。现在与 chat
    走**同一条** maybe_tick 通路：节流、pending 压制、留痕全部同规则。

    conn/settings 可注入（自测用）；都不给 = 生产模式自取。
    """
    import heartbeat as hb_mod
    from config import load_env, load_settings

    load_env()   # 与 main.py cmd_heartbeat 同款顺序：load_env 必须在前面
    if settings is None:
        settings = load_settings()
    own = conn is None
    if own:
        import storage
        conn = storage.connect()
    try:
        class _Shim:
            """maybe_tick 只用 session.settings / session.conn 两个字段。"""
        shim = _Shim()
        shim.settings = settings
        shim.conn = conn
        out = hb_mod.maybe_tick(shim, trigger=hb_mod.TRIGGER_SCHEDULE)
        if out.get("executed") and not out.get("dry_run"):
            conn.commit()
        return out
    finally:
        if own:
            conn.close()


def _start_heartbeat_worker(interval: int = 1800) -> None:
    """心跳生产者（2026-11-07，外部调研 #7 / HB-2）：定时以 schedule 触发心跳。

    此前 `TRIGGER_SCHEDULE` 只是预留位（heartbeat.py 无生产者）——鹤辞只有
    用户开口时才被检查。本线程让 schedule 位第一次有了生产者：每 interval
    秒跑一轮 `_heartbeat_worker_once()`（P1 起走 maybe_tick，与 chat 同规
    则节流），鹤辞开始自己醒来。

    纪律（照抄整理员）：只调心跳，异常吞掉不拖垮服务；
    开关：环境变量 `HEXI_HEARTBEAT=0` 可关掉；`--heartbeat-interval 0` 不启动。
    """
    import os
    import threading
    import time

    if os.environ.get("HEXI_HEARTBEAT", "1") == "0":
        print("心跳生产者: 已禁用（HEXI_HEARTBEAT=0）")
        return

    def loop() -> None:
        while True:
            try:
                out = _heartbeat_worker_once()
                if out.get("executed"):
                    print(f"[心跳] schedule 轮：{out.get('status')}")
            except Exception as exc:  # noqa: BLE001 — 心跳失败不能拖垮 API 服务
                print(f"[心跳] 本轮跳过：{type(exc).__name__}: {exc}")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="heartbeat-worker", daemon=True)
    t.start()
    print(f"心跳生产者: 已启动，每 {interval} 秒 schedule 触发一次")


def _start_inbox_worker(interval: int = 60) -> None:
    """收件箱巡检：有人在等回执的任务，别让它睡一晚上。

    ## 为什么需要

    2026-11-06 22:43 云端派给 dsh 一个任务，dsh 直到 11-07 14:31 才处理——
    **15 小时 48 分**。任务状态机没问题，字段也都对，问题是**没有任何东西定时
    去看一眼收件箱**。能力有，但没人用它。

    ## 做什么（2026-11-08 升级，老板拍板「整」）

    每隔一会儿调 `core.task_patrol()`：
      ① SENT 任务**自动签收**（receive=知悉登记，不构成承诺）；
      ② 发送方**显式标 risk='low'** 的 → 自动接单（accept）；
      ③ 未标注 = fail closed = 只签收+推 ntfy 门铃，接不接仍由实例判断。
    签收动作本身让任务离开 SENT 队列 → 天然只通知一次。

    ## 不做什么

    不自动接单。**接不接是那个实例自己的判断**，巡检员只负责叫醒人，
    不替人做决定。自动接单会把「独立判断」这件事做没了。

    开关：环境变量 `HEXI_INBOX_WATCH=0` 关闭；`--inbox-interval 0` 不启动。
    """
    import os
    import threading
    import time

    if os.environ.get("HEXI_INBOX_WATCH", "1") == "0":
        print("收件箱巡检: 已禁用（HEXI_INBOX_WATCH=0）")
        return

    def loop() -> None:
        while True:
            try:
                conn = core.connect()
                try:
                    # 先把本轮要通知的 SENT 任务抓出来（签收前的快照），
                    # 再让 task_patrol 干活——通知与签收用同一份名单，不重不漏。
                    # 池任务不进这份名单：它等真实认领，不自动签收也不催收件席。
                    rows = [dict(r) for r in conn.execute(
                        "SELECT task_id, to_instance, risk, title, created_at "
                        "FROM tasks WHERE status='SENT' AND to_instance != 'pool' "
                        "ORDER BY created_at"
                    ).fetchall()]
                    pat = core.task_patrol(conn)
                    if pat["received"] or pat["accepted"]:
                        conn.commit()
                finally:
                    conn.close()
                # 任务池巡检（2026-11-21）：指定单超时转池 + 池单超时发老板选项事件。
                # 失败不拖垮巡检本体。
                try:
                    _pool_conn = core.connect()
                    try:
                        core.pool_sweep(_pool_conn)
                    finally:
                        _pool_conn.close()
                except Exception as exc:  # noqa: BLE001
                    print(f"[任务池] sweep 失败（已忽略）："
                          f"{type(exc).__name__}: {str(exc)[:80]}")
                # 中枢制调度（2026-11-08）：签收后的 ACCEPTED 单按规则表执行。
                # v2：失败落库（task_events+effect）、done 静默落账（降噪——
                # 只有对外类和异常才推手机）、对外动作走 effects 队列。
                _dconn = core.connect()
                try:
                    disp = dispatcher.dispatch_tasks(_dconn)
                    eff = core.run_due_effects(_dconn, dispatcher.HANDLERS)
                    # 队列 #1（2026-11-08）：微信出站重复防护本地半边——
                    # pending 超 30 分钟无回执自动 presumed-sent（防重发兜底）。
                    sw = dispatcher.sweep_weixin_outbox(_dconn)
                    _degraded = bool(disp["failures"]) or bool(eff["dead"])
                    core.set_component_health(
                        _dconn, "patrol", "ok",
                        f"received={len(pat['received'])} accepted={len(pat['accepted'])}")
                    core.set_component_health(
                        _dconn, "dispatcher",
                        "degraded" if _degraded else "ok",
                        f"done={len(disp['done'])} waiting={len(disp['waiting'])} "
                        f"failures={len(disp['failures'])} eff_dead={len(eff['dead'])} "
                        f"swept={len(sw['presumed'])} archived={len(sw['archived'])}")
                    for d in disp["done"]:
                        print(f"[调度] {d['report']}" + ("（已推手机）" if d.get("notify") else ""))
                    for b in disp.get("backfilled", []):
                        if b.get("report"):
                            print(f"[调度] 回填 {b['report'][:60]}")
                    if disp["waiting"]:
                        print(f"[调度] {len(disp['waiting'])} 单等老板审批（已推手机，"
                              "批准后下轮自动继续）")
                    if disp["failures"]:
                        print(f"[调度] 失败 {len(disp['failures'])} 单（已落库+通知）："
                              f"{[f['error'][:50] for f in disp['failures'][:2]]}")
                    if eff["dead"]:
                        print(f"[调度] ⚠️ effects 死信 {len(eff['dead'])} 条，需人工处理")
                    if sw["presumed"]:
                        print(f"[调度] ⚠️ {len(sw['presumed'])} 条微信出站超 30 分钟无回执，"
                              "已自动 presumed-sent（防重发，已推手机提醒）")
                    if sw["archived"]:
                        print(f"[调度] 微信出站归档 {len(sw['archived'])} 条（终态超 24h）")
                except Exception as exc:  # noqa: BLE001 — 调度失败不拖垮巡检
                    print(f"[调度] 本轮跳过：{type(exc).__name__}: {exc}")
                    try:
                        core.set_component_health(_dconn, "dispatcher", "degraded",
                                                  f"{type(exc).__name__}: {exc}"[:200])
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    _dconn.close()
                got_ids = set(pat["received"]) | set(pat["accepted"])
                for r in rows:
                    tid = r["task_id"]
                    if tid not in got_ids:
                        continue   # 上一轮已处理过 / 并发下被别的进程签收
                    if tid in pat["accepted"]:
                        msg = (f"已自动接单（发送方标了 low risk）：{r['title']}"
                               f"（{tid}）→ ACCEPTED，等待执行")
                    else:
                        msg = (f"已自动签收：{r['title']}（{tid}）→ RECEIVED。"
                               + ("任务未标 risk（fail closed），接不接由实例自己判断。"
                                  if not (r["risk"] or "") else
                                  f"风险档 {r['risk']}，等待实例判断。"))
                    print(f"[巡检] {msg}")
                    # 写进账本，不依赖 stdout —— 日志会被 _setup_stdout 接走，
                    # 账本是持久、可查、可追溯的。
                    try:
                        c2 = core.connect()
                        try:
                            core.append_event(
                                c2, instance_id=r["to_instance"],
                                event_type="dev_note",
                                content=f"【收件箱巡检】{msg}",
                                source_environment="dsh",
                                provenance="inbox-watch",
                                dedup_key=f"inbox-watch:{tid}:{len(got_ids)}")
                        finally:
                            c2.close()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[巡检] 记账失败：{type(exc).__name__}: {exc}")
                    try:
                        import heartbeat

                        topic = heartbeat.ntfy_topic_from_env()
                        if topic:
                            heartbeat.deliver_to_ntfy(topic, "鹤辞：" + msg)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[巡检] 推送失败：{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 — 巡检失败不能拖垮服务
                print(f"[巡检] 本轮跳过：{type(exc).__name__}: {exc}")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="inbox-worker", daemon=True)
    t.start()
    print(f"收件箱巡检: 已启动，每 {interval} 秒扫一次未接任务")


def main(argv=None) -> int:
    _setup_stdout()
    ap = argparse.ArgumentParser(description="共生 M1 主体 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--print-token", action="store_true", help="启动时打印令牌")
    ap.add_argument("--consume-interval", type=int, default=300,
                    help="记忆整理间隔（秒），0 表示不启动整理员")
    ap.add_argument("--heartbeat-interval", type=int, default=1800,
                    help="心跳 schedule 触发间隔（秒），0 表示不启动心跳生产者")
    ap.add_argument("--inbox-interval", type=int, default=60,
                    help="收件箱巡检间隔（秒），0 表示不启动巡检")
    args = ap.parse_args(argv)

    token = ensure_token()
    conn = core.connect()
    core.init_schema(conn)
    core.ensure_subject(conn)
    conn.close()

    # fs 白名单目录（2026-11-21）：settings.tools.fs_extra_write_dirs —— 老板
    # 显式授权的外部可写目录（默认空 = 项目 ROOT 内）。校验逻辑在 core_fs 不变。
    try:
        from config import load_settings as _load_settings
        import core_fs as _core_fs
        _core_fs.set_extra_write_roots(
            ((_load_settings().tools or {}).get("fs_extra_write_dirs") or []))
    except Exception as exc:  # noqa: BLE001 — 白名单加载失败不阻断启动
        print(f"[警告] fs 白名单加载失败：{type(exc).__name__}: {exc}")

    if args.consume_interval > 0:
        _start_memory_worker(args.consume_interval)
    if args.heartbeat_interval > 0:
        _start_heartbeat_worker(args.heartbeat_interval)
    if args.inbox_interval > 0:
        _start_inbox_worker(args.inbox_interval)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 68)
    print("共生 · M1 主体 API")
    print("=" * 68)
    print(f"监听    : http://{args.host}:{args.port}")
    print(f"核心库  : {core.CORE_DB}")
    print(f"令牌文件: {TOKEN_FILE}")
    if args.print_token:
        print(f"令牌    : {token}")
    print()
    print("试一下：")
    print(f'  curl -H "Authorization: Bearer {token}" http://127.0.0.1:{args.port}/api/health')
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())