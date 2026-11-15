"""M3 验收演示：一次真实协作——发起、反驳、接受、交付结果，外加一次拒绝。

重点不在"流程跑通"，而在证明**接收方有独立判断权**：
Muse 收到请求后先是提了反驳，DSH 作为发起方没有权力拍板，只能等对方接受。

连的是真实运行中的服务（127.0.0.1:8770）与真实核心库。
"""
from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core
from core_client import CoreClient, CoreError

BASE = "http://127.0.0.1:8770"
DSH = "heci_001_dsh"
MUSE = "heci_001_muse"


def show(task_id: str, cli: CoreClient) -> None:
    d = cli.task(task_id)
    t = d["task"]
    print(f"   状态 {t['status']}  轮次 {t['rounds']}/{t['max_rounds']}")
    for h in d["history"]:
        actor = h["actor_instance"].split("_")[-1]
        note = (h["note"] or "").replace("\n", " ")[:46]
        print(f"     #{h['seq']} [{actor}] {h['action']:<9} → {h['status_after']:<11} {note}")


def main() -> int:
    token = (core.CORE_DIR / "api-token").read_text(encoding="utf-8").strip()
    cli = CoreClient(BASE, token, timeout=15)
    cli.register_instance("muse", ["云端检索", "独立判断"])

    print("=" * 74)
    print("M3 协作演示 · 场景一：发起 → 反驳 → 接受 → 交付")
    print("=" * 74)

    print("\n① DSH 发起协作请求")
    r = cli.create_task(
        from_instance=DSH, to_instance=MUSE,
        title="帮我把这次接入的结论整理成一份报告结构",
        detail="我这边跑通了 M1 的接口，你从云端视角给一份提纲，我再补本地细节。",
        context={"progress": "M1 done", "next": "M2 sync"},
        client_request_id="collab-req-1",
    )
    tid = r["task_id"]
    print(f"   task_id={tid}  创建={r['created']}")

    print("\n② Muse 收件箱")
    for t in cli.tasks(MUSE, role="inbox")["tasks"]:
        if t["task_id"] == tid:
            print(f"   [{t['status']}] {t['title']}")

    print("\n③ Muse 先接收，再提出反驳（不是机械服从）")
    cli.task_act(tid, instance_id=MUSE, action="receive")
    cli.task_act(tid, instance_id=MUSE, action="counter",
                 note="提纲可以给，但「报告结构」和「接入结论」最好分开写，混在一起会互相拖累。")

    print("\n④ DSH 作为发起方尝试取消 —— 应该能；但尝试替对方拍板 —— 应该被拒")
    try:
        cli.task_act(tid, instance_id=DSH, action="accept")
        print("   越权=未被拦住（不符合预期）")
        overreach_blocked = False
    except CoreError as e:
        print(f"   越权被拦：{e.status} {str(e.body)[:80]}")
        overreach_blocked = True

    print("\n⑤ Muse 接受修改后的方案，并交付结果")
    cli.task_act(tid, instance_id=MUSE, action="accept", note="按分开写来。")
    cli.task_act(tid, instance_id=MUSE, action="result",
                 result="提纲已给：1) 接入目标 2) 已实现接口 3) 未解决阻塞 4) 下一步")
    show(tid, cli)

    print("\n" + "=" * 74)
    print("M3 协作演示 · 场景二：接收方直接拒绝")
    print("=" * 74)
    r2 = cli.create_task(
        from_instance=DSH, to_instance=MUSE,
        title="把我这边的全部本地文件同步到你那里",
        detail="想让你直接读我电脑上的文件。",
        client_request_id="collab-req-2",
    )
    tid2 = r2["task_id"]
    cli.task_act(tid2, instance_id=MUSE, action="reject",
                 note="跨环境不共享文件系统，这条超出边界；需要数据请走事件账本。")
    show(tid2, cli)

    print("\n" + "=" * 74)
    print("M3 协作演示 · 场景三：协商无限循环的闸门")
    print("=" * 74)
    r3 = cli.create_task(
        from_instance=DSH, to_instance=MUSE,
        title="来回改方案测上限",
        max_rounds=2, client_request_id="collab-req-3",
    )
    tid3 = r3["task_id"]
    for i in range(3):
        try:
            cli.task_act(tid3, instance_id=MUSE, action="counter", note=f"第 {i + 1} 次改")
        except CoreError as e:
            print(f"   第 {i + 1} 轮被拦：{str(e.body)[:90]}")
            break
    d3 = cli.task(tid3)
    print(f"   终态 {d3['task']['status']}  轮次 {d3['task']['rounds']}/{d3['task']['max_rounds']}")

    ok = (overreach_blocked
          and cli.task(tid)["task"]["status"] == "COMPLETED"
          and cli.task(tid2)["task"]["status"] == "REJECTED"
          and d3["task"]["status"] == "FAILED")
    print()
    print("M3 协作协议：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())