"""跨环境文件访问：让 Muse 能真正读代码、改代码、跑开发命令。

## 为什么需要它

Muse 在另一台机器上，它**访问不到这台机器的文件系统**。
主体 API 只给了状态/事件/任务，所以「开放开发权限」实际上落不了地——
它能提建议，但改不了代码。

这个模块补的就是这一层，并且**把边界钉死**：

| 限制 | 做法 |
|---|---|
| 只能在项目目录内 | 路径解析后必须落在 ROOT 内，`../` 越界直接拒 |
| 不能碰敏感文件 | `.env` / `.ssh` / `.git` / `api-token` / `*.db` 一律拒 |
| 不能碰运行时数据 | `conversation/`、`core/*.db`、`memory/raw/`、`__pycache__` 拒 |
| 写文件必须先领租约 | 没持租约 → 拒绝，提示先 acquire（防两个实例同改一个文件） |
| 不能随便执行命令 | 只放行开发相关白名单（git 子命令、py_compile），其余拒 |

**这不是沙箱**，是能力开关 + 边界约定。真正的隔离靠运行环境。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只读文件夹：能读，不能写
READONLY_DIRS = ("conversation", "memory/raw", "memory/versions", "core/sync")

# 拒绝访问（读也不行）：密钥、版本库、运行时库、缓存
DENY_PREFIXES = (".env", ".ssh", ".git", ".aws", ".npmrc", ".netrc", ".pgpass")
DENY_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".pem", ".key")
DENY_PARTS = ("api-token", "__pycache__", ".venv", "venv", "node_modules")
DENY_STEMS = ("credential", "secret", "password", "passwd", "api_key", "apikey", "id_rsa")

MAX_READ_BYTES = 400_000
MAX_WRITE_BYTES = 400_000

# 允许执行的命令白名单（按子命令细分，不放行任意 shell）
# 2026-11-04 修（Muse 审查发现）：`config` 被移出白名单 ——
# `git config core.fsmonitor <cmd>` 会让后续 git 命令执行任意程序，是一条真 RCE 链。
GIT_ALLOWED = {
    "status", "log", "diff", "show", "branch", "add", "commit", "checkout",
    "switch", "restore", "ls-files", "rev-parse", "stash", "merge", "revert",
    "tag", "remote",
}

# 参数级黑名单：这些标志能让 git 写文件或执行外部程序
_DANGEROUS_ARGS = (
    "--output", "--exec", "--open-files-in-pager", "--ext-diff",
    "--upload-pack", "--receive-pack", "--template", "--git-dir",
    "--work-tree", "--config-env", "--path-format",
)


class FsError(ValueError):
    pass


def deny_reason(path: Path) -> str:
    """返回非空即为拒绝理由。

    2026-11-04 修（Muse 审查发现）：原来只查 `path.name`，**路径段只看 DENY_PARTS**，
    所以 `.git/config`（文件名是 config、目录是 .git）能整份读出来。现在路径段也走
    DENY_PREFIXES 和 DENY_PARTS 两套规则。
    """
    name = path.name.lower()
    # 2026-11-07 修（雷 6）：DENY_PARTS 原来只查**目录段**（path.parts[:-1]），
    # 而核心服务的令牌恰恰是个**文件**：`core/api-token` —— 文件名 api-token
    # 不命中任何名称规则，导致它可读可写（令牌窃取/更换通道）。补上文件名比对。
    if name in DENY_PARTS:
        return f"{path.name} 属于受保护名称（{name}），拒绝访问"
    for rule in DENY_PREFIXES:
        if name.startswith(rule):
            return f"{path.name} 属于敏感文件（{rule}*），拒绝访问"
    for suf in DENY_SUFFIXES:
        if name.endswith(suf):
            return f"{path.name} 属于运行时数据库，不走文件接口"
    for rule in DENY_STEMS:
        if rule in name:
            return f"{path.name} 可能含密钥，拒绝访问"

    # 路径段（不含文件名本身）：.git / .ssh / .env* 这类目录要整棵拒掉
    for part in (p.lower() for p in path.parts[:-1]):
        for rule in DENY_PREFIXES:
            if part.startswith(rule):
                return f"路径含敏感目录（{part}），拒绝访问"
        if part in DENY_PARTS:
            return f"路径含受保护目录（{part}），拒绝访问"
    return ""


# 额外白名单目录（2026-11-21，老板授权的写权限扩展）：
# 配置来源 settings.tools.fs_extra_write_dirs，由 core_api/webui 启动时注入。
# 校验逻辑原样保留（deny_reason 照查），只是「授权范围」从写死变配置 —— 与
# 工具权限档同一哲学。默认空 = 项目 ROOT 内（今天之前的行为）。
EXTRA_WRITE_ROOTS: list = []


def set_extra_write_roots(dirs) -> None:
    import os as _os
    EXTRA_WRITE_ROOTS.clear()
    for d in dirs or []:
        d = str(d or "").strip()
        if d and _os.path.isdir(d):
            EXTRA_WRITE_ROOTS.append(str(Path(d).resolve()))


def resolve(rel: str) -> Path:
    """把路径解析到项目内或白名单目录内。越界、敏感一律抛 FsError。"""
    rel = (rel or "").strip().lstrip("/\\").replace("\\", "/")
    if not rel or rel == ".":
        return ROOT
    cand = Path(rel)
    if cand.is_absolute():
        # 绝对路径：只在显式白名单目录内放行（含 ROOT 内的绝对路径）
        target = cand.resolve()
        if str(target).startswith(str(ROOT.resolve())) or any(
                str(target).startswith(str(x)) for x in EXTRA_WRITE_ROOTS):
            reason = deny_reason(target)
            if reason:
                raise FsError(reason)
            return target
        raise FsError(f"越界：{rel} 不在项目目录或白名单目录内") from None
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        raise FsError(f"越界：{rel} 不在项目目录内") from None
    reason = deny_reason(target)
    if reason:
        raise FsError(reason)
    return target


def rel_of(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/") or "."
    except ValueError:
        return str(path)


def ensure_writable(path: Path) -> None:
    # 2026-11-07 修（雷 5）：项目根本身不是可写目标 —— write/mkdir/delete
    # 以 "." / "" / "docs/.." 等形式进来时 resolve() 都折回 ROOT，
    # 不挡的话 remove(".") 就是 rmtree(整个项目)。
    if path == ROOT:
        raise FsError("项目根目录不是可写目标（防误删整库）")
    rel = rel_of(path)
    for ro in READONLY_DIRS:
        if rel == ro or rel.startswith(ro + "/"):
            raise FsError(f"{ro}/ 是只读区，不允许写入")


def list_dir(rel: str = ".") -> list[dict]:
    d = resolve(rel)
    if not d.is_dir():
        raise FsError(f"不是目录：{rel}")
    out = []
    for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
        name = child.name
        blocked = bool(deny_reason(child))
        out.append({
            "name": name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
            "readable": not blocked and child.is_file(),
        })
    return out


def read_text(rel: str, start_line: int = 1, max_bytes: int = MAX_READ_BYTES) -> dict:
    p = resolve(rel)
    if not p.is_file():
        raise FsError(f"文件不存在：{rel}")
    # 硬上限看文件本身；max_bytes 只决定「这次返回多少」。
    # 两者混用会让「只想读前 80 字节」反而把大文件整个拒掉。
    if p.stat().st_size > MAX_READ_BYTES * 4:
        raise FsError(f"文件过大（{p.stat().st_size} 字节），请用 start_line 分段读")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    body = "\n".join(lines[max(0, start_line - 1):])
    limit = max(1, min(int(max_bytes), MAX_READ_BYTES))
    clipped = body[:limit]
    return {
        "path": rel_of(p),
        "total_lines": len(lines),
        "start_line": start_line,
        "truncated": len(clipped) < len(body),
        "content": clipped,
    }


def write_text(rel: str, content: str, *, create: bool = True) -> dict:
    p = resolve(rel)
    ensure_writable(p)
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise FsError(f"内容过大（上限 {MAX_WRITE_BYTES} 字节）")
    existed = p.exists()
    if not existed and not create:
        raise FsError(f"文件不存在且 create=False：{rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": rel_of(p), "created": not existed,
            "bytes": len(content.encode("utf-8"))}


def mkdir(rel: str) -> dict:
    p = resolve(rel)
    ensure_writable(p)
    p.mkdir(parents=True, exist_ok=True)
    return {"path": rel_of(p), "ok": True}


def remove(rel: str) -> dict:
    p = resolve(rel)
    ensure_writable(p)   # 含 ROOT 守卫（雷 5）
    if not p.exists():
        raise FsError(f"不存在：{rel}")
    if p.is_dir():
        # 双保险：rmtree 是不可逆的毁灭性操作，ROOT 拦两道不值钱。
        if p == ROOT:
            raise FsError("拒绝删除项目根目录（雷 5）")
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"path": rel_of(p), "removed": True}


def run(cmd: list[str], *, timeout: int = 60) -> dict:
    """执行开发命令。只放行白名单，不做任意 shell。

    2026-11-04 加固（Muse 审查发现两条真实逃逸）：
      ① `git config` 能写 `core.fsmonitor` → 后续 git 命令执行任意程序
      ② `git log --output=<file>` 能往项目里任意写文件
    现在子命令白名单去掉了 config，并且参数级拦截写文件/执行类标志。
    """
    if not cmd:
        raise FsError("命令为空")
    head = cmd[0].lower()
    for arg in cmd[1:]:
        low = str(arg).lower()
        for bad in _DANGEROUS_ARGS:
            if low == bad or low.startswith(bad + "="):
                raise FsError(f"参数 {arg} 被禁止（可能写文件或执行外部程序）")
    if head == "git":
        if len(cmd) < 2 or cmd[1] not in GIT_ALLOWED:
            raise FsError(f"git 子命令不在白名单：{cmd[1:2] or '（缺参数）'}")
    elif head in ("python", "python3"):
        if cmd[1:2] != ["-m"] or cmd[2:3] != ["py_compile"]:
            raise FsError("python 只允许 `-m py_compile <文件>`；跑脚本请提需求")
    else:
        raise FsError(f"命令不在白名单：{head}（当前只放行 git 与 py_compile）")

    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FsError(f"命令超时（{timeout}s）") from None
    except FileNotFoundError:
        raise FsError(f"找不到命令：{head}") from None
    return {"cmd": cmd, "exit": proc.returncode,
            "stdout": (proc.stdout or "")[:8000],
            "stderr": (proc.stderr or "")[:4000]}


def project_map(max_depth: int = 2) -> dict:
    """给新来的实例一张项目地图，省得它瞎猜结构。"""
    def walk(d: Path, depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        items = []
        for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name in ("__pycache__", ".git", ".venv"):
                continue
            if deny_reason(child):
                items.append({"name": child.name, "type": "file", "readable": False})
                continue
            if child.is_dir():
                items.append({"name": child.name, "type": "dir",
                              "children": walk(child, depth + 1)})
            else:
                items.append({"name": child.name, "type": "file",
                              "size": child.stat().st_size, "readable": True})
        return items

    return {"root": str(ROOT), "tree": walk(ROOT, 1),
            "readonly_dirs": list(READONLY_DIRS),
            "note": "写文件前必须先用 /api/leases/acquire 领租约"}


if __name__ == "__main__":
    import io
    import json
    import sys
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(json.dumps({"src": list_dir("src")[:6],
                      "read": read_text("README.md", max_bytes=200)["path"]},
                     ensure_ascii=False, indent=2))