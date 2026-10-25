"""快照通用逻辑 —— relationship / self_model / user_model 三处重复的**薄**抽取。

## 只抽真正一致的部分

| 抽取 | 为什么算通用 |
|---|---|
| `path_for()` | 三处都是 `<目录>/<前缀>NNN.json`，只差目录与前缀 |
| `scan_versions()` | 三处都是 glob + `split("_v")[1]` + 排序，逐字一致 |
| `next_version()` | 三处都是「最大值 + 1」 |
| `resolve_version()` | 撞号处理；self_model 与 user_model 的"往后找空号"循环**逐字一致** |
| `digest_of()` | 三处都是 `sha256(body.encode("utf-8"))` |

## 刻意**不抽**的部分（差异显式留在各模块）

按要求「不为了减少行数而过度抽象」—— 下面这些三处**都不同**，
硬抽会变成一堆 `if style == ...`：

| 不抽 | 差异 |
|---|---|
| payload 结构 | `state` / `items` / `items + item_count + active_count` |
| JSON 序列化 | relationship/user_model 带 `sort_keys=True`；self_model **不带** |
| 落盘换行 | self_model **不写**末尾换行；另两个写 |
| 侧车文件名 | `state_v001.sha256` / `model_v001.json.sha256` / `model_v001.sha256` |
| 侧车内容 | `"{sha}  {name}\n"` / `"{sha}\n"` |
| 返回值形状 | 三种不同 |
| 错误类型与文案 | `RelationshipError` / `SelfModelError` / `UserModelError` |

**因此本模块不抛业务异常** —— `resolve_version()` 只回报状态，
由各模块用**自己的**错误类型和文案抛错，错误处理行为逐字不变。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 撞号时往后找空号的扫描上限（防止目录异常时死循环）。
# 原 self_model / user_model 都写死 1000，这里保持一致。
SCAN_LIMIT = 1000


def path_for(directory: Path, prefix: str, version: int) -> Path:
    """`<目录>/<前缀>NNN.json`。三处命名规则一致，只差目录与前缀。"""
    return directory / f"{prefix}{version:03d}.json"


def scan_versions(directory: Path, *, prefix: str, ensure: bool = True) -> list[int]:
    """扫描 `<前缀>NNN.json`，返回**升序**版本号列表。

    `ensure=True` 时先建目录 —— 这是 relationship / user_model 原有的副作用，
    **必须保留**（它们的 `existing_snapshots()` 本来就会 `ensure_dirs()`）。
    `ensure=False` 时只在目录不存在时返回空列表，**不创建目录** ——
    这是 self_model 原有行为。
    """
    if ensure:
        from paths import ensure_dirs
        ensure_dirs()
    if not directory.exists():
        return []
    out: list[int] = []
    for path in directory.glob(f"{prefix}*.json"):
        try:
            out.append(int(path.stem.split("_v")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def next_version(versions: list[int]) -> int:
    """下一个版本号 = 最大值 + 1；没有则从 1 开始。"""
    return (versions[-1] + 1) if versions else 1


def resolve_version(directory: Path, *, prefix: str, seek: bool,
                    limit: int = SCAN_LIMIT) -> tuple[int, Path, str]:
    """定出一个版本号。返回 `(version, path, status)`。

    `status` 三态 —— **刻意不抛异常**，让各模块保留自己的错误类型：

    | status | 含义 | 各模块怎么处理 |
    |---|---|---|
    | `free` | 拿到了空号，可以写 | 继续落盘 |
    | `collision` | 撞号且 `seek=False` | relationship：抛"拒绝覆盖" |
    | `exhausted` | 连续 `limit` 个号都被占 | self/user：抛"号被占用" |
    """
    version = next_version(scan_versions(directory, prefix=prefix))
    path = path_for(directory, prefix, version)

    if not path.exists():
        return version, path, "free"

    if not seek:
        # relationship 的策略：撞号即拒绝，绝不往后挪
        return version, path, "collision"

    # self_model / user_model 的策略：往后找空号
    for _ in range(limit):
        version += 1
        path = path_for(directory, prefix, version)
        if not path.exists():
            return version, path, "free"

    return version, path, "exhausted"


def digest_of(body: str) -> str:
    """快照正文的 sha256。三处口径一致（均不含末尾换行）。"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()