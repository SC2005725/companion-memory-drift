"""鹤辞桌面壳（2026-11-08 老板拍板「整」，三选一方案①：pywebview 独立窗口）。

## 这是什么

一个真正的桌面程序：任务栏有自己的图标、Alt-Tab 是独立应用、没有地址栏。
窗口里渲染的是现有 webui 界面（微信装）——界面零改动，壳只负责「窗」。

## 职责边界

- **壳只管窗**：后端是 webui.py（它自己会连 storage/LLM/core_api）。
  webui 在跑就复用（不动它）；没跑就拉起一个（`--no-open`，不弹浏览器），
  并只在「是我们拉起的」情况下于退出时收掉——别人的进程一个不碰。
- **关闭 = 缩托盘**（鹤辞还在，图标闪烁提醒可以后加）：右键托盘 → 显示 / 退出。
- **断线自愈**：后端重启期间页面自己显示「未连接」并每 15s 重试（webui 现成
  逻辑），恢复后自动续上——壳不需要做任何事。

## 用法

    python src\\desktop.py            # 或双击项目根的 启动鹤辞.bat
    （重复启动会被单实例锁挡掉：提示已在托盘）

依赖（各 MIT，pip 一把装）：pywebview（窗）+ pystray + pillow（托盘）。
内核 = Windows 自带 WebView2 运行时（本机已装 157.x，实测在）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI_PORT = 8901          # 8765 是 AutoClaw 的，8770 是 core_api——壳专用 8901
LOCK_PORT = 8902           # 单实例锁（谁占着 = 已有一个鹤辞在跑）
ICON_FILE = ROOT / "src" / "icon-192.png"
TOKEN_FILE = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
              / "xiaoman" / "webui-token")


def _already_running() -> bool:
    try:
        socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=0.4).close()
        return True
    except OSError:
        return False


def _hold_lock() -> socket.socket:
    srv = socket.socket()
    srv.bind(("127.0.0.1", LOCK_PORT))
    srv.listen(1)
    return srv


def _port_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 —— 探活失败统一按没起
        return False


def _backend_ready(port: int, token: str) -> bool:
    """**带令牌**探活：200/302 = 端口上跑的是「令牌同源」的 webui。

    教训两连（2026-11-08 实测）：
    1. Windows 的 SO_REUSEADDR 允许**双绑**——两个 webui 同口都"成功"，
       连接归属不可预期。所以 ready 判定必须过鉴权，不能只看端口通。
    2. urllib 跟随 302 重定向时**不带 cookie**——引导 URL 302 → 跳 / 变
       401，探活永远 False，壳会误判"后端没起"再起一个。修法：关掉自动
       重定向，**302 本身 = 令牌被接受**（webui 引导语义就是写 cookie 后跳 /）。
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/?token={token}")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, url):  # noqa: N802
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=2.0) as r:
            return r.status in (200, 302)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 302)   # 关重定向后 3xx 以 HTTPError 形态抛出
    except Exception:  # noqa: BLE001 —— 401/超时/拒连都算没就绪
        return False


def _ensure_backend(token: str) -> subprocess.Popen | None:
    """webui 后端（令牌同源）在跑就复用；没跑就拉起（--no-open 不弹浏览器）。

    返回我们拉起的子进程（复用别人的时候返回 None——退出时不收）。
    """
    if _backend_ready(WEBUI_PORT, token):
        return None
    exe = (sys.executable.replace("python.exe", "pythonw.exe")
           if sys.executable.endswith("python.exe") else sys.executable)
    proc = subprocess.Popen(
        [exe, "src/webui.py", "--port", str(WEBUI_PORT), "--no-open"],
        cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _backend_ready(WEBUI_PORT, token):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"后端进程退出（code {proc.returncode}）——"
                               "用 python src\\webui.py --port 8901 前台跑一次看报错")
        time.sleep(0.4)
    if _port_up(WEBUI_PORT):
        raise RuntimeError("8901 被一个令牌不同源的 webui 占着——"
                           "先关掉那个旧进程（或删 %LOCALAPPDATA%\\xiaoman\\webui-token "
                           "换新令牌），再启动鹤辞。")
    raise RuntimeError("后端 30 秒没起来（8901）")


def _read_token() -> str:
    """webui 的令牌（ensure_token 落盘的 webui-token）。引导 URL 带上它，
    打开即写 httpOnly cookie——令牌不留在地址栏历史里。"""
    for _ in range(25):
        try:
            if TOKEN_FILE.exists():
                t = TOKEN_FILE.read_text(encoding="utf-8").strip()
                if t:
                    return t
        except OSError:
            pass
        time.sleep(0.4)
    return ""


def main() -> int:
    if _already_running():
        print("鹤辞桌面版已经在跑了（看右下角托盘）。")
        return 0
    try:
        import webview
        import pystray
        from PIL import Image
    except ImportError as exc:
        print(f"缺依赖：{exc}\n先跑：pip install pywebview pystray pillow")
        return 2

    lock = _hold_lock()          # 占住单实例锁直到进程结束
    try:
        token = _read_token()
        backend = _ensure_backend(token)
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        lock.close()
        return 1
    url = f"http://127.0.0.1:{WEBUI_PORT}/?token={token}"

    state = {"quitting": False, "backend": backend}
    window = webview.create_window("鹤辞", url, width=1180, height=800,
                                   min_size=(760, 560))

    def _quit() -> None:
        state["quitting"] = True
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 —— 窗口可能已没了
            pass

    def _closing() -> bool:
        """点 × = 缩托盘（返回 False 拦下关闭）；真退出时放行。"""
        if state["quitting"]:
            return True
        try:
            window.hide()
        except Exception:  # noqa: BLE001
            pass
        return False

    window.events.closing += _closing

    def _tray() -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示鹤辞", lambda *_: window.show(), default=True),
            pystray.MenuItem("退出", lambda *_: _quit()),
        )
        pystray.Icon("heci", Image.open(ICON_FILE), "鹤辞", menu).run()

    threading.Thread(target=_tray, daemon=True, name="tray").start()

    # localStorage 持久化：pywebview 默认 private_mode=True（内存态），重启即清空——
    # 老板 2026-11-21 实测「手动换的头像重启就没」的根因。关隐私模式并指定落盘目录。
    storage_path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "鹤辞" / "webview-profile"
    storage_path.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=str(storage_path))   # 阻塞到窗口销毁（真退出）
    finally:
        lock.close()
        # 只收我们自己拉起的后端；复用的别人进程一个不碰
        if state["backend"] is not None and state["backend"].poll() is None:
            state["backend"].terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
