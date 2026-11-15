"""M4 验收演示：在线状态、心跳、以及「回来一问就知道该干什么」。

不用长连接、不用推送——每次心跳顺带拿回 inbox 摘要，
发现有事就主动开工。这是最小可行的「主动协作」。
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
MUSE = "heci_001_muse"


def main() -> int:
    token = (core.CORE_DIR / "api-token").read_text(encoding="utf-8").strip()
    cli = CoreClient(BASE, token, timeout=15)
    cli.register_instance("muse", ["云端检索", "独立判断"])

    print("=" * 70)
    print("M4 · 在线状态与心跳")
    print("=" * 70)

    print("\n① 两个实例报到")
    for iid in (DSH, MUSE):
        r = cli.heartbeat(iid)
        print(f"   {iid}: 下次心跳 {r['next_heartbeat_in']}s  "
              f"该干活={r['inbox']['should_act']}")

    print("\n② 在线状态")
    for i in cli.instances()["instances"]:
        print(f"   {i['instance_id']:<16} online={i['online']!s:<5} "
              f"空闲 {i['idle_seconds']}s  能力={i['capabilities']}")

    print("\n③ Muse 现在的待办")
    ib = cli.inbox(MUSE)["inbox"]
    print(f"   未读事件 {ib['unread_events']}  待处理任务 {ib['tasks_waiting']}  "
          f"进行中 {ib['tasks_running']}  → 该干活={ib['should_act']}")

    print("\n④ DSH 给 Muse 派一个任务，Muse 下次心跳就能看见")
    r = cli.create_task(from_instance=DSH, to_instance=MUSE,
                        title="心跳触发的任务：统计一下今天的事件数",
                        client_request_id="m4-req-1")
    ib2 = cli.inbox(MUSE)["inbox"]
    print(f"   派单后 Muse 待处理任务 = {ib2['tasks_waiting']}  该干活={ib2['should_act']}")

    print("\n⑤ Muse 心跳回来 → 自己发现并处理")
    hb = cli.heartbeat(MUSE)
    print(f"   heartbeat 返回的 inbox: 待处理 {hb['inbox']['tasks_waiting']}")
    tid = r["task_id"]
    cli.task_act(tid, instance_id=MUSE, action="receive")
    cli.task_act(tid, instance_id=MUSE, action="accept")
    cli.task_act(tid, instance_id=MUSE, action="result",
                 result="今天主体事件 4 条，协作任务 3 个")
    print(f"   任务终态 {cli.task(tid)['task']['status']}")

    ib3 = cli.inbox(MUSE)["inbox"]
    print(f"   处理完：待处理 {ib3['tasks_waiting']}  进行中 {ib3['tasks_running']}")

    print("\n⑥ 离线判定：把心跳窗口调小看效果")
    print(f"   窗口 = {core.ONLINE_WINDOW_SECONDS}s；"
          f"DSH 空闲 {[i for i in cli.instances()['instances'] if i['instance_id'] == DSH][0]['idle_seconds']}s")

    ok = (hb["inbox"]["tasks_waiting"] >= 1
          and cli.task(tid)["task"]["status"] == "COMPLETED"
          and ib3["tasks_waiting"] == 0
          and all(i["online"] for i in cli.instances()["instances"]))
    print()
    print("M4 主动协作：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())