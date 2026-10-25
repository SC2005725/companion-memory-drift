"""图片存储：内容寻址，不碰数据库。

## 设计决定

**内容寻址**：文件名 = `sha256(内容)[:16] + 扩展名`。
同一张图重复上传**天然不重复存** —— 不需要去重逻辑，也不需要索引表。

**不建表、不加字段**：图片本身只落盘；它进入系统的方式是
`chat.send_once` 把引用拼进用户消息（多模态），以及事件里记一句
「用户分享了一张图片」。**记忆系统照旧归纳**，schema 零变化。

## 为什么不用新的依赖

Pillow 之类能做的缩略图/格式转换，这里**不做** —— 浏览器端用 canvas 缩放后
再上传即可（前端几行代码），比在后端引入图像库便宜得多。
本项目「不新增依赖」是硬约束。

## 安全边界

- **扩展名白名单**（只看头几字节的 magic number，不信文件名）
- **单张大小上限**
- **文件名绝不接受用户输入** —— 一律由内容算出，
  从根上排除路径穿越（`../`、绝对路径、空字节）
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from paths import UPLOADS_DIR

# 单张上限。`webui._read_json` 的请求体上限是 1MB（base64 后约 750KB 原图），
# 这里定 4MB 是给**将来的其他入口**留的余量；走 UI 时真正的限制是前者。
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# magic number → 扩展名。**只认头几字节，不认文件名** —— 改个后缀绕不过去。
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),          # 需再验第 8-12 字节是 WEBP，见 _sniff
    (b"BM", ".bmp"),
)

_SAFE_NAME = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp|bmp)$")


class ImageError(Exception):
    """图片相关的可预期错误（大小/类型/不存在）。"""


def _sniff(data: bytes) -> str | None:
    """按内容判类型。返回扩展名，认不出返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return None


def mime_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    }.get(ext, "application/octet-stream")


def save_image(data: bytes) -> dict:
    """存一张图。返回 `{name, path, sha256, bytes, mime, existed}`。

    `existed=True` 表示这张图之前就存过（内容相同）—— 不是错误，是去重生效。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ImageError("图片数据必须是字节")
    if len(data) == 0:
        raise ImageError("图片是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"图片太大（{len(data)} 字节，上限 {MAX_IMAGE_BYTES}）")

    ext = _sniff(data)
    if ext is None:
        raise ImageError("认不出图片格式（只支持 PNG / JPEG / GIF / WebP / BMP）")

    digest = hashlib.sha256(bytes(data)).hexdigest()
    name = f"{digest[:16]}{ext}"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / name
    existed = path.exists()
    if not existed:
        # 先写临时文件再改名：中途失败不会留下半张图被后续读到
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(bytes(data))
        tmp.replace(path)
    return {"name": name, "path": path.as_posix(), "sha256": digest,
            "bytes": len(data), "mime": mime_for(name), "existed": existed}


def load_image(name: str) -> tuple[bytes, str]:
    """按名字读回图片。**名字必须是自己算出来的那种**，否则拒。"""
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ImageError("非法的图片名")
    path = UPLOADS_DIR / name
    if not path.is_file():
        raise ImageError("图片不存在")
    return path.read_bytes(), mime_for(name)


def as_data_uri(name: str) -> str:
    """转成多模态 content 里用的 data URI。"""
    import base64
    data, mime = load_image(name)
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def latest_names(limit: int = 12) -> list[str]:
    """最近改动的图片名（供排查用）。只读。"""
    if not UPLOADS_DIR.exists():
        return []
    files = [p for p in UPLOADS_DIR.glob("*") if p.is_file() and _SAFE_NAME.match(p.name)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files[:max(0, int(limit))]]