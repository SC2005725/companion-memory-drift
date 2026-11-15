"""网页界面的访问控制自测（2026-10-26 加）。

## 为什么要有这个文件

界面原先**零鉴权**，只靠 `bind 127.0.0.1` 当安全边界。但**本机不是边界**：
你打开任意一个恶意网页，它的 JS 就能 `POST http://127.0.0.1:8765/api/env`
改掉 `.env`（读不到响应也无妨，侧效应已达成）；而白名单恰好允许改
`LLM_BASE_URL`，改完调 `/api/env/test` 就把**真 Key 发往攻击者服务器**。

所以这里逐个把四道校验钉住 —— 它们**任何一道被将来的改动拆掉，这个文件就会红**。

## 安全约定

- **不碰生产库**：不用 `storage.connect()`，涉及数据端点的正例走 `redirect_db()`。
- **不花一分钱**：不发真正的模型请求（`/api/env/test` 的【拒绝路径】才测）。
- 服务器只在 `127.0.0.1` 上临时起，测完立刻关。

运行：`python src\\selftest_webui_security.py`
"""
from __future__ import annotations

import http.client
import io
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import webui  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
TOKEN = "T" * 43          # 43 字符 ≥ 32，满足启动硬规则


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def _req(port: int, method: str, path: str, *, headers: dict | None = None,
         body: bytes | None = None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def main() -> int:
    webui._TOKEN = TOKEN
    webui.REMOTE_CONFIG_WRITE = False

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("=" * 68)
    print(f"网页界面访问控制自测（临时端口 {port}，仅 127.0.0.1）")
    print("=" * 68)

    try:
        _run(port)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "=" * 68)
    failed = [n for n, ok, _ in CHECKS if not ok]
    for name, ok, detail in CHECKS:
        print(f"  [{'通过' if ok else '失败'}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 68)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    return 1 if failed else 0


def _run(port: int) -> None:
    # ── T1/T2/T3：无令牌访问数据与配置接口 → 401（**不是** 200+数据）────────
    for path in ("/api/status", "/api/history", "/api/env"):
        code, _, body = _req(port, "GET", path, headers={"Host": f"127.0.0.1:{port}"})
        check(f"无令牌 GET {path} → 401", code == 401, f"实际 {code}")
        check(f"无令牌 {path} 不返回数据",
              b"memory" not in body and b"turns" not in body and b"env" not in body,
              body[:40].decode("utf-8", "replace"))

    # ── T4/T5：令牌错 → 401；令牌对 → 放行 ────────────────────────────────
    code, _, _ = _req(port, "GET", "/api/env",
                      headers={"Host": f"127.0.0.1:{port}", "Cookie": "xiaoman_token=WRONG"})
    check("令牌错误 GET /api/env → 401", code == 401, f"实际 {code}")

    code, _, body = _req(port, "GET", "/api/env",
                         headers={"Host": f"127.0.0.1:{port}",
                                  "Cookie": f"xiaoman_token={TOKEN}"})
    check("令牌正确 GET /api/env → 200", code == 200, f"实际 {code}")
    check("授权响应里没有完整密钥",
          b"sk-" not in body, body[:60].decode("utf-8", "replace"))

    # ── T6：伪造 Origin → 403（这是堵「恶意网页 CSRF」的关键）─────────────
    code, _, _ = _req(port, "GET", "/api/env",
                      headers={"Host": f"127.0.0.1:{port}",
                               "Cookie": f"xiaoman_token={TOKEN}",
                               "Origin": "https://evil.tld"})
    check("跨源 Origin → 403", code == 403, f"实际 {code}")

    code, _, _ = _req(port, "GET", "/api/env",
                      headers={"Host": f"127.0.0.1:{port}",
                               "Cookie": f"xiaoman_token={TOKEN}",
                               "Origin": f"http://127.0.0.1:{port}"})
    check("同源 Origin → 放行", code == 200, f"实际 {code}")

    # ── T7：伪造 Host → 403（防 DNS rebinding）────────────────────────────
    code, _, _ = _req(port, "GET", "/api/env",
                      headers={"Host": "evil.tld",
                               "Cookie": f"xiaoman_token={TOKEN}"})
    check("非法 Host → 403", code == 403, f"实际 {code}")

    # ── T8/T9：POST 缺自定义头 / 用 text/plain 简单请求 → 403 ──────────────
    hdr = {"Host": f"127.0.0.1:{port}", "Cookie": f"xiaoman_token={TOKEN}",
           "Content-Type": "application/json"}
    code, _, _ = _req(port, "POST", "/api/env", headers=hdr,
                      body=b'{"updates":{"LLM_MODEL":"x"}}')
    check("POST 缺 X-Xiaoman-Request → 403", code == 403, f"实际 {code}")

    hdr2 = {"Host": f"127.0.0.1:{port}", "Cookie": f"xiaoman_token={TOKEN}",
            "Content-Type": "text/plain"}     # 简单请求：不触发预检
    code, _, _ = _req(port, "POST", "/api/env", headers=hdr2,
                      body=b'{"updates":{"LLM_MODEL":"x"}}')
    check("text/plain 简单请求 POST → 403", code == 403, f"实际 {code}")

    # ── T10：未定义方法 → 405 ────────────────────────────────────────────
    for method in ("PUT", "DELETE"):
        code, _, _ = _req(port, method, "/api/env",
                          headers={"Host": f"127.0.0.1:{port}",
                                   "Cookie": f"xiaoman_token={TOKEN}"})
        check(f"{method} → 405", code == 405, f"实际 {code}")

    # ── T11：目录穿越（回归）→ 404 ────────────────────────────────────────
    for bad in ("/uploads/../../conversation/conversations.db",
                "/uploads/..%2F..%2Fpersonality%2Fpersonality.json"):
        code, _, _ = _req(port, "GET", bad,
                          headers={"Host": f"127.0.0.1:{port}",
                                   "Cookie": f"xiaoman_token={TOKEN}"})
        check(f"目录穿越被拒 {bad[:34]}", code == 404, f"实际 {code}")

    # ── T12：安全头 ──────────────────────────────────────────────────────
    _, headers, _ = _req(port, "GET", "/api/env",
                         headers={"Host": f"127.0.0.1:{port}",
                                  "Cookie": f"xiaoman_token={TOKEN}"})
    low = {k.lower(): v for k, v in headers.items()}
    check("有 X-Content-Type-Options: nosniff",
          low.get("x-content-type-options") == "nosniff", str(low.get("x-content-type-options")))
    check("有 Referrer-Policy: no-referrer",
          low.get("referrer-policy") == "no-referrer", str(low.get("referrer-policy")))
    check("有 X-Frame-Options: DENY",
          low.get("x-frame-options") == "DENY", str(low.get("x-frame-options")))

    # ── T13：引导入口 ────────────────────────────────────────────────────
    code, headers, _ = _req(port, "GET", f"/?token={TOKEN}",
                            headers={"Host": f"127.0.0.1:{port}"})
    cookie = {k.lower(): v for k, v in headers.items()}.get("set-cookie", "")
    check("正确令牌引导 → 302", code == 302, f"实际 {code}")
    check("引导写入 HttpOnly cookie", "httponly" in cookie.lower(), cookie[:60])
    check("引导 cookie 带 SameSite", "samesite" in cookie.lower(), cookie[:60])

    code, _, _ = _req(port, "GET", "/?token=WRONG",
                      headers={"Host": f"127.0.0.1:{port}"})
    check("错误令牌引导 → 401", code == 401, f"实际 {code}")

    # ── T14：启动硬规则（直接调函数，不必真 bind）────────────────────────
    check("启动规则：本机 + 有效令牌 → 放行",
          webui._startup_refusal("127.0.0.1", "x" * 40) == "", "")
    check("启动规则：空令牌 → 拒绝",
          webui._startup_refusal("127.0.0.1", "") != "", "")
    check("启动规则：短令牌（31 字节）→ 拒绝",
          webui._startup_refusal("127.0.0.1", "x" * 31) != "", "")
    check("启动规则：恰好 32 字节 → 放行",
          webui._startup_refusal("127.0.0.1", "x" * 32) == "", "")
    check("启动规则：--host 0.0.0.0 且无令牌 → 拒绝",
          webui._startup_refusal("0.0.0.0", "") != "", "")
    check("启动规则：--host 0.0.0.0 且令牌有效 → 放行",
          webui._startup_refusal("0.0.0.0", "x" * 40) == "", "")

    # ── T15：LLM_BASE_URL 的 URL 校验 ────────────────────────────────────
    from config import _validate_base_url as vurl
    for good in ("https://api.deepseek.com", "https://api.openai.com/v1"):
        check(f"URL 允许 {good}", vurl(good) == "", vurl(good))
    for bad, why in (("http://api.deepseek.com", "明文 http"),
                     ("https://127.0.0.1", "本机"),
                     ("https://192.168.1.5", "私网"),
                     ("https://10.1.2.3", "私网"),
                     ("https://x.internal", "内网")):
        check(f"URL 拒绝（{why}）{bad}", vurl(bad) != "", vurl(bad))

    # ── T16：XSS 修复（前端 esc 必须转义引号）────────────────────────────
    html = (SRC / "webui.html").read_text(encoding="utf-8")
    esc_line = next((ln for ln in html.splitlines() if "function esc(" in ln), "")
    check("esc() 转义双引号", "&quot;" in esc_line, esc_line[:70])
    check("esc() 转义单引号", "&#x27;" in esc_line, esc_line[:70])
    check("POST 带 X-Xiaoman-Request 标记",
          html.count("'X-Xiaoman-Request':'1'") >= 4,
          f"出现 {html.count(chr(39)+'X-Xiaoman-Request'+chr(39)+':'+chr(39)+'1'+chr(39))} 次")

    # ── T17：/api/env/test 不再把存储的真 Key 发往非默认端点 ─────────────
    src = (SRC / "webui.py").read_text(encoding="utf-8")
    check("env/test 有「非默认端点必须重输 Key」的闸",
          "DEFAULT_BASE_URL" in src and "必须重新输入 Key" in src, "")


if __name__ == "__main__":
    raise SystemExit(main())