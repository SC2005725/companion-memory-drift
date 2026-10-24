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
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

HTML_FILE = SRC / "webui.html"

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

def collect_status(conn, settings) -> dict:
    import memory as memory_mod
    import pricing
    import relationship as relationship_mod
    import storage
    import user_model as user_model_mod

    out: dict = {}

    # 记忆
    try:
        mv = memory_mod.load_latest()
        out["memory"] = {
            "version": mv.version if mv else None,
            "count": len(mv.items) if mv else 0,
            "items": ([{"type": it.type, "text": it.text} for it in mv.items]
                      if mv else []),
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

    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaomanUI/0.2"

    # -- 输出工具 -------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

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
            self._route_get()
        except Exception as exc:
            self._safe_error(exc)

    def do_POST(self) -> None:
        try:
            self._route_post()
        except Exception as exc:
            self._safe_error(exc)

    def _safe_error(self, exc: BaseException) -> None:
        try:
            self._json({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}, 500)
        except Exception:
            pass   # 连错误都发不出去就没办法了，至少不让它冒泡

    def _route_get(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not HTML_FILE.exists():
                self._send(500, f"缺少界面文件：{HTML_FILE}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._status()
            return
        if path == "/api/history":
            self._history()
            return
        if path == "/api/env":
            self._env_status()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._chat()
            return
        if path == "/api/env":
            self._env_save()
            return
        if path == "/api/env/test":
            self._env_test()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

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
        for key, value in updates.items():
            if not isinstance(value, str):
                errors.append(f"{key}：值必须是字符串")
                continue
            value = value.strip()
            if not value:
                continue          # 空值 = 不改这一项
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
        """用当前 Key 发一次**最小**请求验证可用性。

        **这会花一次钱**（极少，约几百个 token）。界面上要提前说清。
        """
        from config import load_env, load_settings, resolve_model

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
        if not text:
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
                result = chat.send_once(session, text, echo=lambda *_: None)
                turn = result.get("turn") or {}
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


def main() -> int:
    ap = argparse.ArgumentParser(description="小满 · 本地网页界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

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
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"[错误] {args.port}–{args.port + 19} 端口都被占用")
        return 1

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print("=" * 56)
    print("小满 · 本地界面已启动")
    print(f"  地址：{url}")
    print("  只监听本机；关掉这个窗口即结束")
    print("=" * 56)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
