"""本地网页界面（零新依赖）。

## 为什么用网页而不是 tkinter

三个原因：
1. **标准库就够** —— `http.server` 在标准库里，`requirements.txt` 仍是两行
2. **逻辑全复用** —— `chat.new_session` / `send_once` / 工具层 / 各查看模块原样调用
3. **样式可改** —— 换配色、加按钮只改 `webui.html`；tkinter 得重学控件

## 它解决的真问题

命令行里「她记着什么」「关系怎么样」「花了多少」都得打命令才看得见。
界面把这些放在侧栏**一直可见** —— 这才是「看得懂」，不只是换个壳。

## 用法

    python src\\webui.py            自动开浏览器
    python src\\webui.py --no-open  不开浏览器（手动访问）
    python src\\webui.py --port 8899

只监听 127.0.0.1，**不对外网开放**。
"""

from __future__ import annotations

import argparse
import json
import os             # 读 LOCALAPPDATA，决定令牌文件放哪
import re
import secrets          # 令牌生成与常量时间比较（secrets.compare_digest）
import socket           # 取本机 IP，给手机经局域网访问时放行 Host/Origin
import urllib.request   # 审批面板代理到 core_api（仅本机回环）

# 图片引用只可能是我们自己算出来的内容寻址名。挡在这里，也挡在 images.load_image。
_IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp|bmp)$")
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

HTML_FILE = SRC / "webui.html"

# PWA 静态资源白名单：URL 路径 → src/ 下的文件名（**固定映射，不接受用户输入**）
# 注意：manifest 与 sw.js 都**动态生成**（见 `_pwa_manifest` / `_pwa_sw`），
# 因为 manifest 的 `start_url` 要带上令牌 —— 否则装好的 App 点开会 401。
_PWA_ASSETS = {
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
}

# 最小 Service Worker：**只为满足「可安装」判据，不缓存任何东西**。
# 有个空的 fetch 监听器就够；绝不拦截和缓存，避免界面变成旧版。
_SW_JS = """// 小满 · 最小 Service Worker（不缓存）
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => { /* 一律走网络，不缓存 */ });
"""

# ---------------------------------------------------------------------------
# 控制台编码兜底（2026-10-28 修）
#
# ## 踩到的坑
#
# Windows 控制台默认代码页是 **GBK**。启动提示里有 `⚠️` 这类字符，
# `print` 时会抛：
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'
# **界面还没开始监听就崩了**，而且只在没设 `PYTHONIOENCODING=utf-8` 时复现
# —— 开发时用 utf-8 跑，正好把这个坑盖住。
#
# ## 为什么用 errors="replace" 而不是强制 utf-8
#
# 强制 utf-8 会让中文在 GBK 控制台里变成乱码；`replace` 保留控制台本身的编码
# （GBK 本来就能显示中文），只把编不出来的符号（如 emoji）换成 `?`。
# **中文照样看得懂，只是个别符号降级** —— 比崩溃好得多。
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass    # 被重定向到不支持 reconfigure 的对象时就算了，别因此启动失败

# ---------------------------------------------------------------------------
# 访问控制（2026-10-26 加）
#
# 起因：界面原先【零鉴权】，且只靠 `bind 127.0.0.1` 当安全边界。
# 但本机不是边界 —— 你打开任意一个恶意网页，它的 JS 就能 POST
# `http://127.0.0.1:8765/api/env` 改掉 `.env`（读不到响应也无妨，
# 侧效应已达成）；而白名单恰好允许改 `LLM_BASE_URL`，改完调
# `/api/env/test` 就会把**真 Key 发往攻击者服务器**。
#
# 因此加四道，**都走同一个 `_guard()`**（一处守卫覆盖所有端点，
# 好过在每个处理器里各加一遍 —— 漏一个就是洞）：
#
#   ① Host 校验     —— 防 DNS rebinding
#   ② Origin 校验   —— 防跨源 CSRF（恶意网页必带自己的 Origin）
#   ③ 自定义请求头  —— POST 强制带 `X-Xiaoman-Request`，会触发 CORS 预检，
#                      恶意网页的「简单请求」伪造不了
#   ④ Token 校验    —— httpOnly cookie；**不放 query string**
#                      （图片响应是 `max-age=31536000`，URL 带 token 会被缓存一年）
#
# 用标准库 `secrets` + `hmac.compare_digest`，**不新增依赖**。
# ---------------------------------------------------------------------------

_TOKEN = ""                    # 启动时取（见 main()：优先持久文件）
_HOME_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

# ---------------------------------------------------------------------------
# 令牌持久化（2026-10-31 加）
#
# ## 为什么需要
#
# 原来每次启动都随机生成新令牌。装上桌面 App 之后，**电脑一重启令牌就变**，
# App 点开显示「未授权」—— 每次都得重新复制链接，那就白装了。
#
# ## 为什么存在项目目录【外面】
#
# `_build_publish.py` 的 `INCLUDE_DIRS = ["src", "config", "personality"]`
# 会把这些目录**同步到公开的发布副本**。令牌放进去就等于进版本库。
# 所以放 `%LOCALAPPDATA%\xiaoman\`。
#
# ## 安全说明（如实）
#
# 这是**明文文件**，长期不变 —— 比每次随机弱一点。
# 但它和 `.env`（存着 API Key）处同一安全级别：本机用户可读。
# 想换令牌：删掉这个文件再启动即可。
# ---------------------------------------------------------------------------
_TOKEN_FILE = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "xiaoman" / "webui-token"


def _load_or_create_token() -> str:
    """读持久令牌；没有/太短就生成并写回。任何失败都不阻断启动。"""
    try:
        if _TOKEN_FILE.is_file():
            old = _TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(old) >= 32:
                return old
    except OSError:
        pass

    fresh = secrets.token_urlsafe(32)
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(fresh, encoding="utf-8")
        try:
            os.chmod(_TOKEN_FILE, 0o600)      # 尽量只给本人读写（Windows 上可能无效）
        except OSError:
            pass
    except OSError:
        pass                                   # 写不了就用内存里的，功能不受影响
    return fresh

# ---------------------------------------------------------------------------
# 允许的 Host / Origin（2026-10-28 扩）
#
# ## 为什么原来只有回环地址，现在要加本机 IP
#
# 手机经局域网访问时写的是 `http://<lan-ip>:8765/`，于是：
#   · Host 头 = `<lan-ip>:8765`
#   · Origin  = `http://<lan-ip>:8765`
# 两者都不在 `_HOME_HOSTS` 里 → **403，手机根本连不上**。
#
# 所以把「**本机自己**的 IPv4」和「当前绑定地址」加进白名单。
#
# ## 这样加安全吗
#
# 安全：这些地址**就是这台机器本身**，放行它们不等于放行外人。
# Host 校验防的是 **DNS rebinding** —— 攻击者让它自己的域名解析到
# `<lan-ip>`，但请求带的 Host 仍是 `evil.example` → 依然被拒 ✅
# Origin 校验防的是跨源 CSRF —— 恶意页面带的是它自己的 Origin → 依然被拒 ✅
#
# **令牌仍然必查**，局域网不等于免鉴权。
# ---------------------------------------------------------------------------
_ALLOWED_HOSTS: set[str] = set(_HOME_HOSTS)


def _is_tailscale_ip(host: str) -> bool:
    """是不是 Tailscale 的地址（100.64.0.0/10）。

    Tailscale 给每台设备分 `100.64.x.x`–`100.127.x.x` 的地址。
    放行这个网段是安全的：**只有你自己 tailnet 里的设备能到这个地址**，
    外部互联网根本路由不到它 —— 不像公网隧道那样把服务暴露出去。

    另外 `*.ts.net` 是 `tailscale serve` 给 HTTPS 用的域名，也一并放行。
    """
    if host.endswith(".ts.net"):
        return True
    parts = host.split(".")
    if len(parts) != 4 or parts[0] != "100":
        return False
    try:
        second = int(parts[1])
    except ValueError:
        return False
    return 64 <= second <= 127        # 100.64.0.0/10 的第二段范围


def _host_ok(host: str) -> bool:
    """Host / Origin 里出现的主机名是否允许。"""
    return host in _ALLOWED_HOSTS or _is_tailscale_ip(host)


def _own_ipv4() -> set[str]:
    """本机自身的 IPv4（用来放行「手机经局域网访问」时出现的 Host/Origin）。

    两种取法并集，任一失败都不影响另一种：
      · UDP connect 到外部地址**不发包**，只借它选出出口网卡地址
      · gethostbyname_ex 兜底（多网卡时能拿到更多）
    """
    found: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))     # 注意：UDP connect 不产生流量
            found.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    # 回环另有条目（127.0.0.1），这里只要真实网卡地址
    return {ip for ip in found if ip and not ip.startswith("127.")}

# 只控制一件事：**远程请求能不能改 `LLM_BASE_URL`**。
# 默认 False。放启动参数，**不上网页 UI**（否则等于把开关交给攻击者）。
# ⚠️ 打开它**不豁免** Host / Origin / 自定义头 / Token 四道校验。
REMOTE_CONFIG_WRITE = False

# 改动 `LLM_BASE_URL` 时，非本机来源要求这个开关打开
_SENSITIVE_ENV_KEYS = frozenset({"LLM_BASE_URL"})

# 会话在进程内保持一个，这样界面里连续说话属于同一段会话（上下文不断）。
# **但数据库连接每请求新建** —— 连接不跨线程用，避免 sqlite3 的线程限制。
_state = {"session_id": None}
_lock = threading.Lock()


def _make_session(conn):
    """复用命令行那套装配，保证界面与终端行为一致。"""
    import chat
    import tools as tools_mod
    from config import load_env, load_settings, resolve_model
    from llm import LLMClient
    from persona import load_persona

    settings = load_settings()
    persona = load_persona()
    env = load_env()
    client = LLMClient(
        api_key=env.api_key,
        base_url=env.base_url,
        model=resolve_model(settings, env),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )
    try:
        registry = tools_mod.build_registry(settings)
    except Exception:
        registry = None
    session = chat.new_session(
        persona=persona, settings=settings, client=client,
        peak_override=env.peak_override, conn=conn, tools=registry,
    )
    # 首次调用时沿用已存在的会话号，让界面里的连续性成立
    if _state["session_id"] is None:
        _state["session_id"] = session.session_id
    else:
        session.session_id = _state["session_id"]
    return session, settings


# ----------------------------------------------------------------------------
# 侧栏数据：把「得打命令才看得见」的东西一次性取出来
# ----------------------------------------------------------------------------

# 前端 UI 适配层（2026-11-21）：把领域数据翻译成 webui 页面需要的展示口径。
# 情绪域（emotion.py）只有 name/description/half_life —— 效价和配色是展示层口径，
# 放这里不污染领域模块；记忆分类配色同理。
_EMOTION_PRESENTATION = {
    "calm":   (0.0,  "#64748b"),
    "fond":   (0.5,  "#f26d85"),
    "glad":   (0.8,  "#34d399"),
    "low":    (-0.7, "#6d5bd8"),
    "uneasy": (-0.4, "#f59e0b"),
    "care":   (0.3,  "#0ea5e9"),
    "tired":  (-0.3, "#94a3b8"),
}
_MEMORY_CAT_COLOR = {
    "个人事实": "#0c9b8a", "偏好": "#6d5bd8", "计划": "#0866ff",
    "项目状态": "#f59e0b", "关系经历": "#f26d85", "生活事项": "#34d399",
    "共同经历": "#0ea5e9", "未分类": "#c9cfdd",
}


def collect_status(conn, settings) -> dict:
    import memory as memory_mod
    import pricing
    import relationship as relationship_mod
    import storage
    import user_model as user_model_mod

    out: dict = {}

    # 记忆（2026-11-21 增补：前端「记忆 · 漂移星图」还需要 cat/categories/versions）
    try:
        mv = memory_mod.load_latest()
        items = ([{"type": it.type, "cat": it.category or "未分类", "text": it.text}
                  for it in mv.items] if mv else [])
        cat_count: dict = {}
        for it in items:
            cat_count[it["cat"]] = cat_count.get(it["cat"], 0) + 1
        out["memory"] = {
            "version": mv.version if mv else None,
            "count": len(items),
            "items": items,
            "categories": [{"name": k, "count": v,
                            "color": _MEMORY_CAT_COLOR.get(k, "#c9cfdd")}
                           for k, v in sorted(cat_count.items(), key=lambda kv: -kv[1])],
            "versions": [row["version"] for row in storage.list_memory_versions(conn)],
        }
    except Exception as exc:
        out["memory"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 关系
    try:
        state = relationship_mod.current_state(conn, settings.relationship or {})
        out["relationship"] = {
            "stage": state.get("stage"),
            "trust": round(state.get("trust", 0), 1),
            "closeness": round(state.get("closeness", 0), 1),
            "familiarity": round(state.get("familiarity", 0), 1),
            "conflict": round(state.get("conflict", 0), 1),
            "total_events": state.get("total_events", 0),
            "counter_mode": state.get("counter_mode", False),
        }
    except Exception as exc:
        out["relationship"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 用户模型
    try:
        items = user_model_mod.load_items()
        active = user_model_mod.active_items(items)
        out["user_model"] = {
            "active": len(active),
            "superseded": len(items) - len(active),
            "items": [{"status": x.get("status"), "belief": x.get("belief")}
                      for x in active],
        }
    except Exception as exc:
        out["user_model"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 待兑现的主动意图
    try:
        pending = storage.list_pending_actions(conn, status="pending")
        out["pending"] = [{"message": p["message"], "reason": p["reason"]}
                          for p in pending]
    except Exception:
        out["pending"] = []

    # 工具
    try:
        import tools as tools_mod
        reg = tools_mod.build_registry(settings)
        enabled = reg.enabled()
        out["tools"] = {
            "permissions": list(reg.allowed_permissions),
            "enabled": [{"name": t.name, "permission": t.permission,
                         "description": t.description} for t in enabled],
            "blocked": [n for n in reg.names() if n not in [x.name for x in enabled]],
        }
    except Exception as exc:
        out["tools"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 计数与花费
    try:
        stats = storage.stats(conn)
        mstats = storage.memory_stats(conn)
        summary = pricing.summarize()
        out["stats"] = {
            "turns": stats.get("turns", 0),
            "events": stats.get("events", 0),
            "api_calls": stats.get("api_calls", 0),
            "versions": mstats.get("versions", 0),
            "retrievals": mstats.get("retrievals", 0),
            "cost_yuan": round(summary.get("total_micro_yuan", 0) / 1_000_000, 6),
            "tokens": (summary.get("prompt_tokens", 0)
                       + summary.get("completion_tokens", 0)),
        }
    except Exception as exc:
        out["stats"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 情绪（2026-11-21：前端「情感 · 极光场」页需要 st.emotion —— 此前 collect_status
    # 从未提供这个键，页面永远渲染「待机」空态。数据来自 emotion 模块的持久状态；
    # emotion/ 目录还没有首批快照时如实给空态，不编造数值。）
    try:
        import emotion as emotion_mod
        state = emotion_mod.load_state(decay=True)
        if state and state.emotions and any(v > 0 for v in state.emotions.values()):
            total = sum(state.emotions.values()) or 1.0
            mix = []
            for name, inten in sorted(state.emotions.items(), key=lambda kv: -kv[1]):
                if inten <= 0:
                    continue
                val, color = _EMOTION_PRESENTATION.get(name, (0.0, "#64748b"))
                mix.append({"name": name, "pct": round(inten / total * 100),
                            "color": color})
            dom = state.dominant() or ""
            dom_val, _dom_color = _EMOTION_PRESENTATION.get(dom, (0.0, "#64748b"))
            out["emotion"] = {
                "current": {"label": dom, "valence": dom_val},
                "mix": mix,
                "days": [],   # 情绪快照历史积累起来后接入曲线（emotion/ 目录首批出现时）
            }
        else:
            out["emotion"] = {"current": {}, "mix": [], "days": []}
    except Exception as exc:
        out["emotion"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 心跳（2026-11-21：前端「心跳 · 节律」页需要 st.heartbeat —— 同样一直缺键。
    # 数据全部从 heartbeat_runs 表只读派生，间隔口径与 core_api 心跳 worker 一致（1800s）。）
    try:
        hb_rows = storage.list_heartbeat_runs(conn, limit=12)
        hb_total = conn.execute(
            "SELECT COUNT(*) AS c FROM heartbeat_runs").fetchone()["c"]
        hb_last = hb_rows[0] if hb_rows else None

        def _fmt(s):
            return ((s or "")[5:16].replace("T", " "))   # MM-DD HH:MM

        hb_next = "—"
        if hb_last:
            try:
                from datetime import datetime as _dt
                from datetime import timedelta as _td
                hb_next = (_dt.fromisoformat(hb_last["created_at_local"])
                           + _td(minutes=30)).strftime("%m-%d %H:%M")
            except ValueError:
                hb_next = "—"

        out["heartbeat"] = {
            "interval": "30 分钟",
            "runs": hb_total,
            "last": _fmt(hb_last["created_at_local"]) if hb_last else "—",
            "next": hb_next,
            "levels": [
                {"proactive_message": 1.0, "pending": 1.0,
                 "delivered": 1.0}.get(r["decision"], 0.4 if r["decision"] == "no_action" else 0.3)
                for r in hb_rows],
            "recent": [{"t": _fmt(r["created_at_local"]), "d": r["decision"],
                        "note": (r["message"] or r["reason"] or "")[:80]}
                       for r in hb_rows[:6]],
        }
    except Exception as exc:
        out["heartbeat"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 记忆版本历史（系统 · 脉络页曲线）：最近 20 版（升序取尾部，画增速放缓用）
    try:
        rows = storage.list_memory_versions(conn)
        out["memory"]["version_history"] = [
            {"version": r["version"], "items": r["item_count"],
             "at": ((r["created_at_local"] or "")[5:16]).replace("T", " ")}
            for r in rows][-20:]
    except Exception as exc:  # noqa: BLE001
        out["memory"]["version_history"] = []

    # 观察判据（系统 · 脉络页徽章）：三个「等自然发生」的里程碑
    out["flags"] = _observation_flags(out)

    return out


# 「按条蒸馏」首触发标志的缓存：以 run.log 大小为指纹，日志没涨就不重扫
_SEG_LOG_CACHE = {"size": -1, "hit": False}

# 系统 · 脉络页聚合接口的 TTL 缓存：手机端 30s 轮询一次即可，
# 多标签/快速刷新在 8s 内直接命中同一份，不连环打 core
_SYS_CACHE = {"ts": 0.0, "payload": None}


def _segment_distilled_flag() -> bool:
    """run.log 里出现 consolidate 按条蒸馏 v2 的留痕 = 首次真实触发。"""
    log_path = HTML_FILE.parent.parent / "logs" / "run.log"
    try:
        size = log_path.stat().st_size
    except OSError:
        return False
    c = _SEG_LOG_CACHE
    if c["hit"]:
        return True          # 一旦点亮就是事实，日志轮转也不回头
    if size == c["size"]:
        return False
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - 400_000))
            if "consolidate 按条蒸馏" in f.read():
                c["hit"] = True
    except OSError:
        return False
    c["size"] = size
    return c["hit"]


def _observation_flags(out: dict) -> dict:
    """观察判据（数据驱动，后端加键前端即多一枚徽章）：
    segment_distilled 分段蒸馏首触发 · provisional 人格发展闭环 ·
    emotion_days 情绪快照积累到可画曲线。全部「等自然发生」。"""
    try:
        seg = _segment_distilled_flag()
    except Exception:  # noqa: BLE001
        seg = False
    try:
        prof = json.loads((HTML_FILE.parent.parent / "personality" / "profile.json")
                          .read_text(encoding="utf-8"))
        prov = "provisional" in prof
    except Exception:  # noqa: BLE001
        prov = False
    try:
        emo = bool((out.get("emotion") or {}).get("days"))
    except Exception:  # noqa: BLE001
        emo = False
    return {"segment_distilled": seg, "provisional": prov, "emotion_days": emo}


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaomanUI/0.2"

    # -- 输出工具 -------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")   # 防 token 经 Referer 外泄
        self.send_header("X-Frame-Options", "DENY")          # 防被 iframe 套
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- 访问控制 -------------------------------------------------------------

    def _is_local_client(self) -> bool:
        """请求是否来自本机。**用对端地址判断，不看 Host 头**（Host 可伪造）。"""
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _cookie_token(self) -> str:
        """从 Cookie 里取令牌。**不用 query string** —— 那会被缓存与留在历史里。"""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "xiaoman_token":
                return v
        return ""

    def _guard(self, *, is_bootstrap: bool = False) -> bool:
        """四道校验，返回 True 才放行。

        `is_bootstrap=True` 只用于 `GET /?token=…` 那一次引导（此时还没有 cookie）。
        **即使引导，①②③也照查** —— 引导只是省掉第 ④ 道的 cookie 要求。
        """
        port = self.server.server_address[1]

        # ① Host —— 防 DNS rebinding
        # 允许集 = 回环 + 本机自身 IP + Tailscale 地址 + `--allow-host` 显式指定的域名
        # 攻击者域名即使解析到本机 IP，Host 仍是它自己的域名 → 照样被拒。
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        if not _host_ok(host):
            self._json({"ok": False, "error": "非法 Host"}, 403)
            return False

        # ② Origin —— 浏览器发跨源请求一定带 Origin，同源才放行
        origin = self.headers.get("Origin")
        if origin:
            ok = False
            for scheme in ("http", "https"):
                prefix = f"{scheme}://"
                if origin.rstrip("/").startswith(prefix):
                    o_host = origin.rstrip("/")[len(prefix):].rsplit(":", 1)[0].lower()
                    if _host_ok(o_host):
                        ok = True
                        break
            if not ok:
                self._json({"ok": False, "error": "跨源请求被拒绝"}, 403)
                return False

        # ③ POST 强制自定义头 —— 触发 CORS 预检，恶意网页的简单请求伪造不了
        if self.command == "POST" and self.headers.get("X-Xiaoman-Request") != "1":
            self._json({"ok": False, "error": "缺少请求标记"}, 403)
            return False

        # ④ Token
        if is_bootstrap:
            return True
        if not _TOKEN or not secrets.compare_digest(self._cookie_token(), _TOKEN):
            self._json({"ok": False, "error": "未授权：请用启动时打印的地址打开界面"}, 401)
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):   # 别把每个请求都刷到控制台
        pass

    # -- 路由 -----------------------------------------------------------------

    def do_GET(self) -> None:
        # 顶层兜底：任何未预料的异常都要变成 500 响应，
        # 而不是让连接**直接断掉**（那样前端只能看到「连不上后端」，查不出原因）。
        try:
            if not self._guard(is_bootstrap=self._is_bootstrap()):
                return
            self._route_get()
        except Exception as exc:
            self._safe_error(exc)

    def do_POST(self) -> None:
        try:
            if not self._guard():
                return
            self._route_post()
        except Exception as exc:
            self._safe_error(exc)

    # 未定义方法显式拒绝 —— 不要落到默认行为
    def _method_not_allowed(self) -> None:
        self._send(405, b"method not allowed", "text/plain; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _method_not_allowed

    def _is_bootstrap(self) -> bool:
        """`GET /?token=…` —— 一次性引导，用来把令牌写进 httpOnly cookie。"""
        return (self.command == "GET" and urlparse(self.path).path in ("/", "/index.html")
                and "token=" in (urlparse(self.path).query or ""))

    def _safe_error(self, exc: BaseException) -> None:
        try:
            self._json({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}, 500)
        except Exception:
            pass   # 连错误都发不出去就没办法了，至少不让它冒泡

    # -- CE-4 审批面板代理（2026-11-08）---------------------------------------
    # webui 不落任何审批状态，纯转发到本机 core_api（带核心令牌）。
    # 鉴权走 _guard 统一护栏（令牌 cookie + 同源 + POST 自定义头）。

    def _core_token(self) -> str:
        """核心服务令牌（本机同盘文件，不经网络传输）。"""
        p = HTML_FILE.parent.parent / "core" / "api-token"
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _approvals_proxy(self, method: str) -> None:
        if method == "POST":
            body = self._read_json()
            decision = str(body.get("decision") or "").lower()
            if decision not in ("approve", "deny"):
                self._json({"ok": False, "error": "decision 只能是 approve/deny"}, 400)
                return
            payload = {"approval_id": str(body.get("approval_id") or ""),
                       "decision": decision,
                       "decided_by": "boss-webui",
                       "note": body.get("note")}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:8770/api/approvals/decide", data=data,
                headers={"Authorization": f"Bearer {self._core_token()}",
                         "Content-Type": "application/json"}, method="POST")
        else:
            req = urllib.request.Request(
                "http://127.0.0.1:8770/api/approvals/list?status=pending",
                headers={"Authorization": f"Bearer {self._core_token()}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                out = json.loads(r.read().decode("utf-8"))
            self._json(out, 200)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False,
                        "error": f"核心服务不可达（127.0.0.1:8770，"
                                 f"{type(exc).__name__}）——启动核心服务了吗？"}, 502)

    def _components_proxy(self) -> None:
        """HealthBoard 透传：各部件健康（中枢制 v2）。"""
        token = self._core_token()
        if not token:
            self._json({"ok": False, "error": "读不到核心令牌"}, 502)
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:8770/api/health/components",
                    headers={"Authorization": f"Bearer {token}"}), timeout=8) as r:
                self._json(json.loads(r.read().decode("utf-8")), 200)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"核心服务不可达（{type(exc).__name__}）"}, 502)

    def _core_get(self, path: str, timeout: float = 6.0):
        """带核心令牌的只读代理 GET（本机 core_api）。"""
        req = urllib.request.Request(
            f"http://127.0.0.1:8770{path}",
            headers={"Authorization": f"Bearer {self._core_token()}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _system(self) -> None:
        """系统 · 脉络页聚合接口（前后端连通优化，2026-11-21）：

        手机端**一次轮询一个接口**拿全页数据：
          · status（曲线/计数/摘要/判据）—— 本地只读，零费用
          · pool / instances / approvals —— 服务端代拉 core（同机 HTTP，零令牌暴露）
        8 秒 TTL 缓存；core 不可达时对应块降级，页面其余部分照常。
        """
        import time as _time
        now = _time.time()
        c = _SYS_CACHE
        if c["payload"] is not None and now - c["ts"] < 8.0:
            self._json(c["payload"], 200)
            return

        import storage
        from config import load_settings

        conn = storage.connect()
        try:
            settings = load_settings()
            st = collect_status(conn, settings)
        except Exception as exc:  # noqa: BLE001
            st = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()

        sysout = {"status": st, "pool": [], "instances": [],
                  "approvals": [], "core_ok": True}
        for key, core_path in (("pool", "/api/tasks/pool?limit=50"),
                               ("instances", "/api/instances"),
                               ("approvals",
                                "/api/approvals/list?status=pending")):
            try:
                d = self._core_get(core_path)
                sysout[key] = (d.get("tasks") or d.get("instances")
                               or d.get("approvals") or [])
            except Exception as exc:  # noqa: BLE001
                sysout["core_ok"] = False
                sysout[key + "_error"] = type(exc).__name__

        payload = {"ok": True, "system": sysout}
        c["payload"] = payload
        c["ts"] = now
        self._json(payload, 200)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            # 引导：`/?token=…` → 校验通过就写 httpOnly cookie 并**立刻重定向**，
            # 令牌不留在地址栏、不进历史、不被图片缓存规则带走。
            if parsed.query:
                given = (parse_qs(parsed.query).get("token") or [""])[0]
                if _TOKEN and secrets.compare_digest(given, _TOKEN):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie",
                                     # Max-Age：桌面 App 反复开关时不用重新贴令牌。
                                     # 7 天，且【服务重启后令牌会变】—— 所以它只是
                                     # 「同一次运行期间免重输」，不是长期凭据。
                                     f"xiaoman_token={_TOKEN}; HttpOnly; SameSite=Strict; "
                                     f"Path=/; Max-Age={7 * 24 * 3600}")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    return
                self._send(401, "令牌不正确。请用启动时打印的完整地址打开。".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            if not HTML_FILE.exists():
                self._send(500, f"缺少界面文件：{HTML_FILE}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._status()
            return
        if path == "/api/system":
            self._system()
            return
        if path == "/api/history":
            self._history()
            return
        if path == "/api/env":
            self._env_status()
            return
        # ---- CE-4 审批面板（2026-11-08）：代理到 core_api -------------------
        # 鉴权已由 _guard 统一兜住（令牌 cookie + 同源）；这里只是把请求
        # 转发给本机 core_api（带核心令牌），高危动作的批准入口。
        if path == "/api/approvals":
            self._approvals_proxy("GET")
            return
        if path == "/api/components":
            self._components_proxy()
            return
        # ---- PWA 静态资源（2026-10-31 加，让手机能把它装成 App）----
        # 只放行白名单里的文件名，**不做目录读取** —— 别把 src/ 暴露成静态目录。
        if path in _PWA_ASSETS:
            self._pwa_asset(_PWA_ASSETS[path])
            return
        if path == "/webui.webmanifest":
            self._pwa_manifest()
            return
        if path == "/sw.js":
            self._pwa_sw()
            return
        # 图片回显：`<img src="/uploads/<name>">`
        if path.startswith("/uploads/"):
            self._image_serve(path[len("/uploads/"):])
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _pwa_manifest(self) -> None:
        """动态生成 manifest。

        ## 为什么不能是静态文件

        `start_url` 必须带上令牌 —— 否则装好的 App 点开是「未授权」。
        而令牌是每次启动确定的（现已持久化），所以在这里拼。

        ## Chrome 的「可安装」判据（缺一项就不给安装按钮）

        · `name` 或 `short_name`  ✅
        · `icons` 含 192 与 512    ✅
        · `start_url`              ✅（带令牌）
        · `display` = standalone   ✅
        · HTTPS 提供               ✅（走 tailscale serve）
        · 已注册 Service Worker    ✅（见 /sw.js）
        """
        manifest = {
            "name": "鹤辞",
            "short_name": "鹤辞",
            "description": "共生 Companion —— 你的鹤辞，随身带着",
            "lang": "zh-CN",
            "id": "/",
            # 装好后点图标直接带令牌进去，不用再贴链接
            "start_url": f"/?token={_TOKEN}",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#1a2038",
            "theme_color": "#1a2038",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/manifest+json; charset=utf-8")

    def _pwa_sw(self) -> None:
        """回最小 Service Worker（不缓存，只为满足安装判据）。"""
        self._send(200, _SW_JS.encode("utf-8"), "text/javascript; charset=utf-8")

    def _pwa_asset(self, filename: str) -> None:
        """回一个 PWA 静态文件。文件名固定（来自 `_PWA_ASSETS`），不接受用户输入。"""
        target = SRC / filename
        if not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        mime = {
            ".webmanifest": "application/manifest+json; charset=utf-8",
            ".png": "image/png",
        }.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        # 图标按内容长期缓存；manifest 走 no-store（改完要立刻生效）
        cache = ("public, max-age=86400" if target.suffix == ".png" else "no-store")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _image_serve(self, name: str) -> None:
        """回显一张已存的图。**名字不合规或不存在就是 404**，不回退到别的路径。"""
        import images
        try:
            data, mime = images.load_image(unquote(name))
        except images.ImageError as exc:
            self._send(404, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        # 图片内容按 sha256 命名 → 内容变了名字就变，所以可以长期缓存
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._chat()
            return
        if path == "/api/approvals/decide":
            self._approvals_proxy("POST")
            return
        if path == "/api/env":
            self._env_save()
            return
        if path == "/api/env/test":
            self._env_test()
            return
        if path == "/api/upload":
            self._upload()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _upload(self) -> None:
        """收一张图，存盘，返回内容寻址的名字。

        **只做存储，不碰数据库、不写记忆** ——
        图片进入系统靠的是下一轮 `send_once(user_image=...)`，
        以及事件里记一句「分享了一张图片」。这样 schema 零变化。

        `_read_json` 的请求体上限是 1MB，base64 后对应约 750KB 原图；
        前端会先在 canvas 里缩到长边 1600 再传，正常远低于这个数。
        """
        import base64
        import binascii
        import images

        payload = self._read_json()
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return
        b64 = payload.get("data")
        if not isinstance(b64, str):
            self._json({"ok": False, "error": "data 必须是 base64 字符串"}, 400)
            return
        # 去掉可能存在的 data URI 前缀
        if b64.startswith("data:"):
            comma = b64.find(",")
            if comma < 0:
                self._json({"ok": False, "error": "data URI 不完整"}, 400)
                return
            b64 = b64[comma + 1:]
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            self._json({"ok": False, "error": "base64 解码失败"}, 400)
            return
        try:
            info = images.save_image(raw)
        except images.ImageError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        self._json({"ok": True, "image": info})

    # -- API Key 设置（唯一会写密钥的地方）------------------------------------

    def _env_status(self) -> None:
        """返回 API Key 的**状态**（有没有、后四位），**绝不返回密钥本身**。"""
        from config import api_key_status

        try:
            self._json({"ok": True, "env": api_key_status()})
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _env_save(self) -> None:
        """保存 API Key 等到 `.env`。

        ⚠️ 这是唯一会**写密钥**的接口。三条纪律：

          1. **校验必须严** —— 值里带一个换行就能往 `.env` 注入任意键
             （例如把 `LLM_BASE_URL` 指向别处，密钥就发出去了）
          2. **绝不回显密钥** —— 响应里只有「已保存」与后四位
          3. **只改指定的键** —— `.env` 里其他内容（含注释）原样保留
        """
        from config import api_key_status, load_env, save_env_value

        payload = self._read_json()
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return
        updates = payload.get("updates")
        if not isinstance(updates, dict) or not updates:
            self._json({"ok": False, "error": "没有要保存的内容"}, 400)
            return

        saved, errors = [], []
        remote = not self._is_local_client()
        for key, value in updates.items():
            if not isinstance(value, str):
                errors.append(f"{key}：值必须是字符串")
                continue
            value = value.strip()
            if not value:
                continue          # 空值 = 不改这一项
            # 敏感项：远程来源必须显式打开 REMOTE_CONFIG_WRITE 才允许改。
            # 默认 False —— 因为「改 LLM_BASE_URL」= 把真 Key 送去别的地址。
            if remote and key in _SENSITIVE_ENV_KEYS and not REMOTE_CONFIG_WRITE:
                errors.append(f"{key}：不允许远程修改（需以 --remote-config-write 启动）")
                continue
            try:
                save_env_value(key, value)
                saved.append(key)   # 只记键名，不记值
            except ValueError as exc:
                errors.append(f"{key}：{exc}")

        if not saved and errors:
            self._json({"ok": False, "error": "；".join(errors)}, 400)
            return

        # 让新 Key 立刻生效（不用重启界面）
        try:
            load_env(fresh=True)
        except Exception:
            pass

        self._json({"ok": True, "saved": saved, "errors": errors,
                    "env": api_key_status(),
                    "note": "已写入 .env（本地明文保存；不要把这个文件发给别人）"})

    def _env_test(self) -> None:
        """用 Key 发一次**最小**请求验证可用性。

        **这会花一次钱**（极少，约几百个 token）。界面上要提前说清。

        ## ⚠️ 2026-10-26 安全修复（**这次就改，不留到以后**）

        原来这里写的是 `api_key = candidate or env.api_key` —— 不传 `key` 就
        **回落到 `.env` 里存的真 Key**，而 `base_url` 用的是**文件里的最新值**。
        两者一凑就是一条完整的偷 Key 链：

            ① POST /api/env   改 LLM_BASE_URL 指向攻击者服务器
            ② POST /api/env/test  （不带 key）→ 真 Key 发往那个地址

        **现在**：不带 `key` 时，**只有端点是已知默认端点**才允许用存储的 Key。
        端点被改过 → 必须**重新输入 Key** 才测（输入什么就测什么，不回落到存储值）。
        """
        from config import DEFAULT_BASE_URL, load_env, load_settings, resolve_model

        payload = self._read_json()
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return
        raw = payload.get("key")
        if raw is not None and not isinstance(raw, str):
            self._json({"ok": False, "error": "key 必须是字符串"}, 400)
            return
        candidate = (raw or "").strip()

        try:
            settings = load_settings()
            env = load_env(fresh=True)      # 先用文件里的最新值覆盖进程环境
            # —— 安全闸：不允许把【存储的真 Key】发往【非默认端点】——
            if not candidate and env.base_url.rstrip("/") != DEFAULT_BASE_URL.rstrip("/"):
                self._json({"ok": False,
                            "error": "端点已被改为非默认地址，为安全起见必须重新输入 Key 才能测试。"
                                     "（不会把已保存的 Key 发往新地址）"}, 400)
                return
            api_key = candidate or env.api_key
            if not api_key:
                self._json({"ok": False, "error": "还没有填 API Key"}, 400)
                return

            from llm import LLMClient

            client = LLMClient(
                api_key=api_key, base_url=env.base_url,
                model=resolve_model(settings, env),
                temperature=0.0, max_tokens=64,
            )
            # Phase 11：改走统一出口 —— 这次真实调用从此进 api_calls 与费用账本。
            # 原来 `client.send(...)` 直连，绕过了 `chat.call_model` 的记账。
            import chat as chat_mod
            import storage

            conn = storage.connect()
            try:
                result = chat_mod.call_model_standalone(
                    client, [{"role": "user", "content": "回复两个字：在的"}],
                    settings=settings, conn=conn, purpose="doctor",
                )
            finally:
                conn.close()
            reply = result["reply"]
            self._json({"ok": True,
                        "model": reply.model,
                        "latency_ms": reply.latency_ms,
                        "reply": (reply.text or "").strip()[:40]})
        except Exception as exc:
            # 报错信息里可能含端点地址，但**不含密钥** —— LLMClient 的错误不带 key
            self._json({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"[:300]})

    def _status(self) -> None:
        import storage
        from config import load_settings

        conn = storage.connect()
        try:
            settings = load_settings()
            self._json({"ok": True, "status": collect_status(conn, settings)})
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()

    def _history(self) -> None:
        import storage

        conn = storage.connect()
        try:
            sid = _state["session_id"]
            rows = storage.recent_turns(conn, 200, session_id=sid) if sid else []
            # recent_turns 只回 user / assistant 两个字段（它按 seq 倒序取再反转），
            # 所以这里不要假装有 seq。
            self._json({"ok": True, "turns": [
                {"user": r.get("user"), "assistant": r.get("assistant")}
                for r in rows
            ]})
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()

    def _chat(self) -> None:
        import chat
        import storage

        payload = self._read_json()
        # 类型必须挡住。
        #
        # 踩过的坑：`{"text": 123}` 让 `.strip()` 抛 AttributeError，
        # 连接**直接断掉** —— 浏览器只看到「连不上后端」，看不到真实原因。
        # 请求可能来自任何地方（手写 curl、旧版页面、别的脚本），
        # 所以每个字段都要自己验，不能假设前端一定发对。
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            self._json({"ok": False, "error": "text 必须是字符串"}, 400)
            return
        text = raw_text.strip()

        # 图片：可以只发图不打字，所以"空消息"的判定要放宽。
        # 名字必须是我们自己算出来的那种（内容 sha256）—— `images.load_image` 会再验一次，
        # 这里先挡一道，避免把任意字符串传下去。
        raw_image = payload.get("image")
        image = raw_image.strip() if isinstance(raw_image, str) else None
        if image is not None and not _IMAGE_NAME_RE.match(image):
            self._json({"ok": False, "error": "非法的图片引用"}, 400)
            return

        if not text and not image:
            self._json({"ok": False, "error": "空消息"}, 400)
            return
        if len(text) > 8000:
            self._json({"ok": False, "error": "消息过长（上限 8000 字符）"}, 400)
            return

        # 同一时刻只处理一条消息。
        # 理由：会话对象与人格校验都假设串行；并发进来会互相踩状态。
        # ponytail: 全局串行锁，单用户界面下够用；要多用户再谈并发。
        with _lock:
            conn = storage.connect()
            try:
                session, settings = _make_session(conn)
                # 2026-11-21：自动整理改**后台**跑——M 批后整理一轮 31-45 秒，
                # 同步执行会把回复扣住（实测 01:09 两轮：模型 1.5s 答完，
                # 整理扣 31s）。回复先回，整理后台补。
                result = chat.send_once(session, text, echo=lambda *_: None,
                                        user_image=image, auto_consolidate=False)
                turn = result.get("turn") or {}

                # P0-1：真实对话自动进入主体事件账本。
                # 同理放真实入口，不放 send_once；失败不影响这轮回复。
                try:
                    import core_capture

                    core_capture.record_turn(
                        instance_id="heci_001_dsh",
                        session_id=getattr(session, "session_id", None),
                        turn=turn,
                        result=result,
                    )
                except Exception:  # noqa: BLE001
                    pass

                # 自动整理后台补跑（独立连接+独立会话，同 _heartbeat_worker_once 模式）
                import threading as _th

                def _bg_consolidate():
                    try:
                        _conn = storage.connect()
                        try:
                            _sess = chat.new_session(
                                persona=getattr(session, 'persona', None),
                                settings=session.settings, client=session.client,
                                peak_override=False, conn=_conn)
                            chat.maybe_auto_consolidate(_sess, echo=lambda *_: None)
                        finally:
                            _conn.close()
                    except Exception as exc:  # noqa: BLE001
                        storage.log_line(f'后台整理失败（不影响回复）: {type(exc).__name__}: {exc}')

                _th.Thread(target=_bg_consolidate, name='auto-consolidate-bg', daemon=True).start()

                try:
                    status = collect_status(conn, settings)
                except Exception:
                    status = {}
                self._json({
                    "ok": True,
                    "reply": turn.get("assistant_text") or "",
                    "turn_id": turn.get("turn_id"),
                    "seq": turn.get("seq"),
                    "tool_calls": result.get("tool_calls") or [],
                    "tool_iterations": result.get("tool_iterations") or 0,
                    "tool_truncated": bool(result.get("tool_truncated")),
                    "memory_hits": len((result.get("retrieval") or {}).get("hit_item_ids") or []),
                    "relationship_events": [
                        e.get("type") for e in (result.get("relationship_events") or [])
                    ],
                    "user_model_changes": [
                        {"type": c.get("type"), "belief": c.get("belief")
                         or c.get("target_belief")}
                        for c in (result.get("user_model_changes") or [])
                    ],
                    "cost_yuan": round((result.get("cost_micro_yuan") or 0) / 1_000_000, 6),
                    "status": status,
                })
            except Exception as exc:
                self._json({"ok": False,
                            "error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                conn.close()


def _startup_refusal(host: str, token: str) -> str:
    """启动硬规则。返回空串 = 可以启动；非空 = 拒绝理由。

    两条**不可绕过**（想要远程，就必须先有合格的令牌）：
      1. 令牌为空或短于 32 字节 → 拒绝
      2. 绑定非本机地址但没有可用令牌 → 拒绝

    独立成函数是为了能被测试直接调用（不必真的去 bind）。
    """
    if not token or len(token) < 32:
        return "令牌为空或短于 32 字节 —— 拒绝启动（这会让界面变成零鉴权）"
    if host not in _HOME_HOSTS and not token:
        return f"绑定非本机地址 {host} 但没有可用令牌 —— 拒绝启动"
    return ""


def _log_startup(host: str, port: int) -> None:
    """把界面启动时的安全姿态记进 run.log（出事后能查当时开没开）。"""
    try:
        from datetime import datetime

        log = ROOT / "logs" / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        line = (f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                f"webui 启动：host={host} port={port} "
                f"token={'on' if _TOKEN else 'off'} "
                f"remote_config_write={'on' if REMOTE_CONFIG_WRITE else 'off'}\n")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass   # 记不上日志不该阻止界面启动


def main() -> int:
    global _TOKEN, REMOTE_CONFIG_WRITE

    ap = argparse.ArgumentParser(description="鹤辞 · 本地网页界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址，默认 127.0.0.1（只本机）。"
                         "要手机经局域网访问用 --host 0.0.0.0")
    ap.add_argument("--remote-config-write", action="store_true",
                    help="允许【远程】请求修改 LLM_BASE_URL（默认否）。"
                         "打开它不豁免令牌/Origin/Host/自定义头校验。")
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)   # 仅供测试注入
    ap.add_argument("--allow-host", action="append", default=[],
                    metavar="HOST",
                    help="额外放行的主机名（可重复）。例如 Tailscale 的 "
                         "xxx.tailnet.ts.net，或自定义域名。"
                         "Tailscale 的 100.64.0.0/10 与 *.ts.net 已自动放行，一般不用填。")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    # 令牌：`--token` 优先（仅供测试）；否则用持久文件里的，没有就生成一个新的。
    # 持久化的意义：桌面 App 装好之后，电脑重启也不会失效。
    _TOKEN = args.token or _load_or_create_token()
    REMOTE_CONFIG_WRITE = bool(args.remote_config_write)

    # 允许集：回环 + 本机自身 IP + 具体绑定地址 + 命令行显式指定的域名
    # （手机经局域网访问时 Host/Origin 会是本机 IP；经 Tailscale 时是 100.x / *.ts.net）
    _ALLOWED_HOSTS.update(_own_ipv4())
    for extra in args.allow_host or []:
        if extra.strip():
            _ALLOWED_HOSTS.add(extra.strip().lower())
    if args.host not in ("", "0.0.0.0", "::"):
        _ALLOWED_HOSTS.add(args.host.lower())

    refusal = _startup_refusal(args.host, _TOKEN)
    if refusal:
        print(f"[拒绝启动] {refusal}")
        return 2

    if not HTML_FILE.exists():
        print(f"[错误] 缺少界面文件：{HTML_FILE}")
        return 2

    # 先做一次启动自检，免得浏览器打开后才发现 Key 或配置有问题
    try:
        import storage
        from config import load_settings
        conn = storage.connect()
        try:
            load_settings()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[配置错误] {type(exc).__name__}: {exc}")
        print("提示：先跑 python src\\main.py doctor 看环境。")
        return 2

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"[错误] {args.port}–{args.port + 19} 端口都被占用")
        return 1

    actual_port = httpd.server_address[1]
    _log_startup(args.host, actual_port)

    # 引导地址带令牌 → 打开即写入 httpOnly cookie 并重定向，令牌不留在地址栏
    bound_wild = args.host in ("0.0.0.0", "::")
    local_url = f"http://127.0.0.1:{actual_port}/?token={_TOKEN}"
    print("=" * 56)
    print("小满 · 界面已启动")
    print(f"  本机地址：{local_url}")
    if bound_wild:
        # 监听所有网卡 → 列出手机可用的局域网地址
        for ip in sorted(_own_ipv4()):
            print(f"  手机地址：http://{ip}:{actual_port}/?token={_TOKEN}")
    elif args.host.lower() not in _HOME_HOSTS:
        print(f"  手机地址：http://{args.host}:{actual_port}/?token={_TOKEN}")
    print(f"  绑定：{args.host}" + ("（所有网卡，手机可访问）" if bound_wild
                                 else "（只本机）" if args.host in _HOME_HOSTS else ""))
    print(f"  令牌：已启用（{len(_TOKEN)} 字符）")
    print(f"  令牌来源：{'命令行 --token' if args.token else str(_TOKEN_FILE)}")
    print("           （持久化 → 电脑重启后 App 仍能用；想换令牌就删掉这个文件）")
    print(f"  远程改 LLM_BASE_URL：{'允许 ⚠️' if REMOTE_CONFIG_WRITE else '禁止（默认）'}")
    print("  ⚠️ 带令牌的地址等同于密码，不要发给别人。")
    print("  关掉这个窗口即结束")
    print("=" * 56)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
