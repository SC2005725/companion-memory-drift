"""首版存档（archive anchor）——补上哈希链的一个原理性缺口。

## ⚠️ 这个文件要解决什么问题

`verify` 的哈希链**检测不出尾部截断**。实测证明：

| 实验 | 结果 |
|------|------|
| 删掉**中间** 5 轮 | ✅ 检测出来（前序哈希不匹配） |
| 删掉**尾部** 5 轮 | ❌ **通过**，报告「原始数据未被改动」 |
| 删掉**尾部** 50 轮（144 → 94） | ❌ **通过**，报告「原始数据未被改动」 |

**根因**：链上每一行只记录「前一条的哈希」。
把尾巴整段切掉之后，剩下那些行的 `prev_hash` 依然自洽 —— 
**因为没有任何东西记录「总共应该有多少条」。**

这不是实现瑕疵，是**设计缺口**：链保证「顺序没被改」，
但保证不了「没被砍掉一截」。

## 后果（已经真实发生过）

给用户的一个归档包里，数据是 119 轮 / 13 事件；
而打包那一刻真实库是 134 轮 / 14 事件。
**包里的数据是静默截断的，但它依然报告「原始数据未被改动」。**

用户拿到那个包，会以为手上的就是全部证据。

## 修法：给链加一个**外部锚点**

链自己无法证明完整性，因为「整条链被重写」和「链没被动过」在链内部无法区分。
但只要有**一个链外的、记录了当时长度的东西**，就能回答：

> 「这条链在被存档时有多少行？最后一行是谁？」

所以本脚本生成 `first_archive.json`：

```json
{
  "packaged_at": "...",
  "conversations": {"count": 144, "max_seq": 144, "last_turn_id": "...", "last_sha256": "..."},
  "events": {"count": 14, "ids_sha256": "..."},
  "memory_versions": ["v001", ...]
}
```

`verify` 拿它一比对，尾部截断立刻暴露。

## 诚实说明它的边界

**这个锚点不能防「有意篡改」** —— 谁都能连它一起改。
它防的是我实际犯过的那个错：**打包/复制时数据不完整，却没人发现**。
本地单用户工具的信任模型下，这是恰当的防护级别。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

ANCHOR_FILE = ROOT / "experiments" / "first_archive.json"


def _sha_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def snapshot(conn) -> dict:
    """读当前库，生成锚点内容。**只读，不改任何数据。**"""
    conv_n = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    conv_max = conn.execute("SELECT MAX(seq) AS m FROM conversations").fetchone()["m"]
    last = conn.execute(
        "SELECT turn_id, sha256, content_hash FROM conversations ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    ev_ids = [r["event_id"] for r in conn.execute("SELECT event_id FROM events ORDER BY event_id")]
    vers = [r["version"] for r in conn.execute(
        "SELECT version FROM memory_versions ORDER BY version")]

    return {
        "packaged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "conversations": {
            "count": conv_n,
            "max_seq": conv_max,
            "last_turn_id": last["turn_id"] if last else None,
            "last_sha256": last["sha256"] if last else None,
        },
        "events": {
            "count": len(ev_ids),
            "ids_sha256": _sha_text("\n".join(ev_ids)),
            "ids": ev_ids,
        },
        "memory_versions": vers,
        "note": "哈希链检测不出尾部截断；这个锚点记录了存档当时的规模。见 src/archive_anchor.py。",
    }


def write_anchor(conn, *, note: str = "") -> dict:
    data = snapshot(conn)
    if note:
        data["note"] = f"{note}｜{data['note']}"
    ANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANCHOR_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return data


def read_anchor() -> dict | None:
    if not ANCHOR_FILE.exists():
        return None
    try:
        return json.loads(ANCHOR_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def compare(conn) -> dict:
    """把当前库与锚点比对。返回 {ok, problems, anchor, current}。

    ⚠️ 关键约定：**锚点只用于向后比较，不覆盖更新**。

    如果每次打包都重写锚点，那「包里比锚点少」就永远发现不了 ——
    锚点会跟着一起缩水，失去意义。
    """
    anchor = read_anchor()
    current = snapshot(conn)
    problems: list[str] = []

    # 显式退出开关。
    #
    # 为什么不是"判断这是不是真库"：那类判断我已经错了两次
    # （`for_test` 守卫用行数猜、锚点第一版用行数猜），
    # **只要有合成数据或隔离测试库，靠猜必然误判**。
    #
    # 所以改成显式声明：跑隔离测试的一方设 DSH_SKIP_ANCHOR=1，
    # 锚点就完全不参与判定。
    if os.environ.get("DSH_SKIP_ANCHOR"):
        return {"ok": True, "problems": [], "anchor": anchor, "current": current,
                "skipped": "DSH_SKIP_ANCHOR 已设置（隔离测试），锚点不参与判定"}

    if anchor is None:
        return {"ok": True, "problems": [], "anchor": None, "current": current,
                "note": "还没有锚点文件；用 archive-anchor 命令生成第一份。"}

    a_conv = anchor.get("conversations") or {}
    c_conv = current["conversations"]

    # ⚠️ 只校验「锚点记录的那一轮还在不在」，**不要求当前库达到锚点规模**。
    #
    # 第一版我写的是「当前轮数 < 锚点轮数 → 判定截断」，结果**误报**：
    # `dryrun.py` 会在同目录跑一个只有 12 轮的合成库，被当成"严重截断"。
    # 这跟我在 `storage.connect(for_test=)` 守卫上犯的错是同一类 ——
    # **拿行数去猜"这是不是真库"，在存在合成/测试数据时必然误判。**
    #
    # 改成基于主键判定：锚点记录的最后那条 turn_id 还在，就说明尾巴没被砍。
    # 好处：不管是"真被截断"还是"跑在测试库上"，判据都成立。
    last_turn_id = a_conv.get("last_turn_id")
    if last_turn_id:
        alive = conn.execute("SELECT 1 FROM conversations WHERE turn_id = ?",
                             (last_turn_id,)).fetchone()
        if not alive:
            now_max = c_conv.get("max_seq")
            problems.append(
                f"锚点记录的最后一条对话（turn_id={last_turn_id}，seq={a_conv.get('max_seq')}）"
                f"**不见了** → 链尾被截断（当前 max_seq={now_max}）"
            )
    elif c_conv["count"] < a_conv.get("count", 0):
        # 老锚点没有 last_turn_id 时退回数量判断
        problems.append(
            f"对话轮数**变少**了：锚点 {a_conv.get('count')} → 现在 {c_conv['count']}"
            f"（少了 {a_conv.get('count', 0) - c_conv['count']} 轮）"
        )

    a_ev = anchor.get("events") or {}
    c_ev = current["events"]
    anchor_ids = list(a_ev.get("ids") or [])
    if anchor_ids:
        # 同样基于主键：锚点记录的每个事件都还应在
        missing_ev = [eid for eid in anchor_ids
                      if not conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                                          (eid,)).fetchone()]
        if missing_ev:
            problems.append(
                f"锚点记录的原始事件丢了 {len(missing_ev)} 条：{missing_ev[:5]}"
                + ("…" if len(missing_ev) > 5 else "")
            )
    elif c_ev["count"] < a_ev.get("count", 0):
        problems.append(
            f"原始事件**变少**了：锚点 {a_ev.get('count')} → 现在 {c_ev['count']}")

    a_v = anchor.get("memory_versions") or []
    c_v = current["memory_versions"]
    if c_v and a_v and not set(a_v).issubset(set(c_v)):
        problems.append(f"记忆版本少了：锚点有 {a_v}，现在只有 {c_v}")

    return {"ok": not problems, "problems": problems,
            "anchor": anchor, "current": current}


def format_report(result: dict) -> str:
    lines = []
    if result.get("anchor") is None:
        lines.append("存档锚点：不存在")
        lines.append("  " + result.get("note", ""))
        return "\n".join(lines)
    a = result["anchor"]
    c = result["current"]
    lines.append(f"存档锚点：{a.get('packaged_at')}")
    lines.append(f"  锚点当时：{a['conversations'].get('count')} 轮对话 / "
                 f"{a['events'].get('count')} 事件 / "
                 f"{len(a.get('memory_versions') or [])} 记忆版本")
    lines.append(f"  当前实际：{c['conversations']['count']} 轮对话 / "
                 f"{c['events']['count']} 事件 / "
                 f"{len(c['memory_versions'])} 记忆版本")
    if result["ok"]:
        lines.append("  → ✅ 没有变少（尾部截断会在这里暴露）")
    else:
        lines.append("  → ❌ 发现数据变少/不一致：")
        for p in result["problems"]:
            lines.append(f"      · {p}")
    return "\n".join(lines)


def main() -> int:
    """命令行：生成或校验锚点。

        python src\\archive_anchor.py            校验（默认）
        python src\\archive_anchor.py --write    生成/覆盖锚点（打包时用）
    """
    import storage

    conn = storage.connect()
    try:
        if "--write" in sys.argv:
            data = write_anchor(conn, note="由 archive_anchor.py --write 生成")
            print(f"已写入 {ANCHOR_FILE.relative_to(ROOT)}")
            print(f"  对话 {data['conversations']['count']} 轮（max_seq="
                  f"{data['conversations']['max_seq']}）")
            print(f"  事件 {data['events']['count']} 条")
            print(f"  记忆版本 {data['memory_versions']}")
            return 0
        result = compare(conn)
        print(format_report(result))
        return 0 if result["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
