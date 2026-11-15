"""让鹤辞 DSH 实例按协作规则走一遍：领租约 → 记录开发事件 → 释放租约。

这是「实际参与开发」的最小证明：它不只会被配置，还会留下可追溯的开发记录。
"""
from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core
from core_client import CoreClient

BASE = "http://127.0.0.1:8770"
DSH = "heci_001_dsh"


def main() -> int:
    token = (core.CORE_DIR / "api-token").read_text(encoding="utf-8").strip()
    cli = CoreClient(BASE, token, timeout=15)

    print("① 报到")
    hb = cli.heartbeat(DSH)
    print(f"   在线实例 {[i['instance_id'] for i in hb['instances']]}")

    print("\n② 领文件租约（本次改动涉及的核心文件）")
    for path in ("src/companion_core.py", "src/core_api.py"):
        r = cli.lease(path, DSH, ttl_seconds=1800, note="鹤辞主体替换")
        print(f"   {r['path']} → 到 {r['expires_at']}")

    print("\n③ 记录开发事件（为什么改 / 结果 / 遗留）")
    ev = cli.post_event(
        instance_id=DSH,
        event_type="dev_note",
        content=(
            "【改动】建立鹤辞独立主体 heci_001（人格/关系/记忆在 core/），"
            "小满降为 legacy 主体 xiaoman_001，数据保留原位只读。"
            "新增文件租约（避免双实例同改一个文件）与开发协作规则文档。"
            "【结果】persona/relationship/memory 按 subject_id 路由生效（鹤辞=初识/0 信任，"
            "不继承小满 76.27）；租约独占验证通过（muse 抢同一文件被拒）。"
            "【遗留】DSH 真实对话尚未自动写入事件账本；Muse 仍未跨机接入；"
            "记忆候选尚无消费方。"
        ),
        source_environment="dsh",
        dedup_key="dev-20261104-heci-subject",
        provenance="heci_001_dsh/2026-11-04",
    )
    print(f"   event_id={ev['event_id']} created={ev['created']} cursor={ev['cursor']}")

    print("\n④ 释放租约")
    for path in ("src/companion_core.py", "src/core_api.py"):
        print(f"   {path} released={cli.unlease(path, DSH)['released']}")

    print("\n⑤ 账本核对")
    st = cli.state()["state"]
    print(f"   主体 {st['subject_id']} 事件 {st['event_count']} 实例 {st['instances']}")
    for e in cli.events(since=0, limit=10)["events"]:
        print(f"   #{e['cursor']} [{e['instance_id']}] {e['event_type']}: {e['event_content'][:50]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())