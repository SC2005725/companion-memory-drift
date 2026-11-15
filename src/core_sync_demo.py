"""M2 验收演示：同一个主体下，DSH 与 Muse 双向交换事件。

它连的是**真实运行中的服务**（127.0.0.1:8770）和**真实核心库**，
不是临时沙箱——因为 M2 要证明的就是「真能跑起来」。

游标文件放在临时目录，避免污染正式的同步书签。
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core
from core_client import CoreClient
from core_sync import SyncWorker

BASE = "http://127.0.0.1:8770"


def main() -> int:
    token = (core.CORE_DIR / "api-token").read_text(encoding="utf-8").strip()
    cli = CoreClient(BASE, token, timeout=15)
    cli.health()

    cli.register_instance("muse", ["云端检索", "独立判断"])
    who = cli.whoami()
    print(f"主体    : {who['subject']['subject_id']}")
    print(f"实例    : {[i['instance_id'] for i in who['instances']]}")
    print()

    state_dir = Path(tempfile.mkdtemp(prefix="m2_sync_"))
    dsh = SyncWorker(cli, instance_id="heci_001_dsh", state_dir=state_dir)
    muse = SyncWorker(cli, instance_id="heci_001_muse", state_dir=state_dir)

    print("① DSH 写一条经历")
    r = dsh.post(event_type="experience",
                 content="在本地把测试副本的主体 API 跑起来了，M1 的 7 个接口全通",
                 dedup_key="m2-dsh-001")
    print(f"   event_id={r['event_id']} created={r['created']} cursor={r['cursor']}")

    print("\n② Muse 拉取（应只看到 DSH 的，不含自己）")
    got = muse.pull()
    for ev in got:
        print(f"   [{ev['source_environment']}] cursor={ev['cursor']} {ev['event_content']}")
    print(f"   Muse 游标现在 = {muse.cursor}")

    print("\n③ Muse 写一条独立判断")
    r2 = muse.post(event_type="analysis",
                   content="我这边看到 DSH 的进度，判断可以在云端补一份报告结构",
                   dedup_key="m2-muse-001")
    print(f"   event_id={r2['event_id']} created={r2['created']}")

    print("\n④ DSH 拉取（应只看到 Muse 的）")
    got2 = dsh.pull()
    for ev in got2:
        print(f"   [{ev['source_environment']}] cursor={ev['cursor']} {ev['event_content']}")
    print(f"   DSH 游标现在 = {dsh.cursor}")

    print("\n⑤ Muse 再拉一次（游标已推进，应为空）")
    again = muse.pull()
    print(f"   拉到 {len(again)} 条")

    print("\n⑥ 重复提交同一 dedup_key（应不新建）")
    r3 = dsh.post(event_type="experience", content="重复内容", dedup_key="m2-dsh-001")
    print(f"   created={r3['created']}  同一 event_id={r3['event_id'] == r['event_id']}")

    print("\n⑦ 最终账本")
    st = cli.state()["state"]
    print(f"   事件总数 {st['event_count']}  游标 {st['cursor']}")
    ev = cli.events(since=0, limit=50)["events"]
    for e in ev:
        print(f"   #{e['cursor']} [{e['source_environment']}/{e['instance_id']}] "
              f"{e['event_type']}: {e['event_content'][:40]}")

    ok = (len(got) >= 1 and len(got2) >= 1 and len(again) == 0
          and r3["created"] is False and all(
              e["subject_id"] == "heci_001" for e in ev))
    print()
    print("M2 双向同步：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())