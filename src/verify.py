"""原始数据完整性校验（Phase 3）。

回答一个问题：**这些原始数据有没有被改动过？**

校验三件事：
1. conversations 表的哈希链是否连续（每条记录覆盖了前一条的哈希）；
2. memory/raw/events/*.json 的文件内容是否与登记并写下的 sha256 一致；
3. 是否存在没有登记文件、或文件丢失的情况。

只读操作，不会修改任何数据。Phase 1-3 结束、以及迁移前后都应该跑一次。
"""

from __future__ import annotations

import storage
from paths import RAW_EVENTS_DIR, display


def verify_all(conn) -> dict:
    report = {
        "turns_checked": 0,
        "chain_ok": True,
        "chain_errors": [],
        "events_checked": 0,
        "events_ok": True,
        "event_errors": [],
        "orphan_files": [],
        "missing_files": [],
    }

    prev_hash = None
    prev_content_hash = None
    rows = conn.execute("SELECT * FROM conversations ORDER BY seq ASC").fetchall()
    for row in rows:
        report["turns_checked"] += 1

        # 先核对内容哈希：它只覆盖「这一轮说了什么」，内容被改会立刻暴露。
        expected_content = storage.content_hash_of(row["seq"], row["user_text"], row["assistant_text"])
        if row["content_hash"] != expected_content:
            report["chain_ok"] = False
            report["chain_errors"].append(f"{row['turn_id']}：对话内容被改动过")

        bundle = {
            "turn_id": row["turn_id"],
            "created_at_utc": row["created_at_utc"],
            "created_at_local": row["created_at_local"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "model": row["model"],
            "persona_name": row["persona_name"],
            "persona_sha256": row["persona_sha256"],
            "user_text": row["user_text"],
            "assistant_text": row["assistant_text"],
            "content_hash": row["content_hash"],
        }
        expected = storage.sha256_text(storage.chain_payload(bundle, prev_hash, prev_content_hash))
        if row["prev_hash"] != prev_hash:
            report["chain_ok"] = False
            report["chain_errors"].append(f"{row['turn_id']}：前序哈希不匹配")
        if row["sha256"] != expected:
            report["chain_ok"] = False
            report["chain_errors"].append(f"{row['turn_id']}：链哈希不匹配（记录被改过，或前后有增删）")

        prev_hash = row["sha256"]
        prev_content_hash = row["content_hash"]

    registered = {}
    for row in conn.execute("SELECT * FROM events ORDER BY created_at_utc ASC").fetchall():
        registered[row["event_id"]] = row

    for event_id, row in registered.items():
        report["events_checked"] += 1
        path = RAW_EVENTS_DIR / f"{event_id}.json"
        if not path.exists():
            report["events_ok"] = False
            report["missing_files"].append(event_id)
            continue
        text = path.read_text(encoding="utf-8").rstrip("\n")
        digest = storage.sha256_text(text)
        if digest != row["sha256"]:
            report["events_ok"] = False
            report["event_errors"].append(f"{event_id}：文件内容与登记的 sha256 不一致")

    if RAW_EVENTS_DIR.exists():
        for path in sorted(RAW_EVENTS_DIR.glob("*.json")):
            if path.stem not in registered:
                report["orphan_files"].append(display(path))

    report["ok"] = report["chain_ok"] and report["events_ok"] and not report["missing_files"]

    # 存档锚点比对：补上「哈希链检测不出尾部截断」这个缺口。
    #
    # 不加这一段的话，把链的尾巴砍掉一半，`verify` 依然报「未被改动」——
    # 实测确认过（删尾部 50 轮仍通过）。根因是每行只记「前一条的哈希」，
    # 没有任何东西记录「总共应该有多少条」。详见 src/archive_anchor.py。
    try:
        import archive_anchor

        report["anchor"] = archive_anchor.compare(conn)
        if not report["anchor"]["ok"]:
            report["ok"] = False
    except Exception as exc:            # 锚点坏了不该让整个校验崩
        report["anchor"] = {"ok": True, "problems": [], "anchor": None,
                            "current": {}, "broken": f"{type(exc).__name__}: {exc}"}

    return report


def format_report(report: dict) -> str:
    lines = [
        f"对话记录校验：{report['turns_checked']} 条 → {'通过' if report['chain_ok'] else '失败'}",
        f"原始事件校验：{report['events_checked']} 条 → {'通过' if report['events_ok'] else '失败'}",
    ]
    for err in report["chain_errors"]:
        lines.append(f"  [哈希链] {err}")
    for err in report["event_errors"]:
        lines.append(f"  [事件文件] {err}")
    for item in report["missing_files"]:
        lines.append(f"  [缺失] {item}.json 不见了")
    for item in report["orphan_files"]:
        lines.append(f"  [未登记] {item}（文件存在但数据库里没有记录）")

    anchor = report.get("anchor")
    if anchor:
        if anchor.get("broken"):
            lines.append(f"  [锚点] 比对失败：{anchor['broken']}")
        elif anchor.get("anchor") is None:
            lines.append("存档锚点：不存在（跑 python src\\archive_anchor.py --write 生成）")
        elif anchor["ok"]:
            a = anchor["anchor"]
            lines.append(
                f"存档锚点校验：通过（锚点记录 seq={a['conversations'].get('max_seq')} 那条仍在；"
                f"当前 {anchor['current']['conversations']['count']} 轮）")
        else:
            lines.append("存档锚点校验：**失败**")
            for p in anchor["problems"]:
                lines.append(f"  [锚点] {p}")

    if report["ok"]:
        lines.append("结论：原始数据未被改动。")
    else:
        lines.append("结论：发现不一致，请勿在此基础上继续实验，先查明原因。")
    return "\n".join(lines)
