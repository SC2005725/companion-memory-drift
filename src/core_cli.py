"""共生 · Phase 1 命令行入口（主体身份 / 实例注册 / 事件账本）。

它**不碰**现有对话库，只操作独立的 `core/companion_core.db`。

    python src\\core_cli.py init
    python src\\core_cli.py register --env dsh
    python src\\core_cli.py append --env dsh --type experience --content "今天聊了夜班"
    python src\\core_cli.py list
    python src\\core_cli.py export --out core\\export.json
    python src\\core_cli.py demo
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core


def cmd_init(args) -> int:
    conn = core.connect(args.db)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, args.subject, args.name)
    print(f"库      : {args.db or core.CORE_DB}")
    print(f"主体    : {sid}（{args.name}）")
    print("状态    : schema 已就绪")
    conn.close()
    return 0


def cmd_register(args) -> int:
    conn = core.connect(args.db)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, args.subject, args.name)
    iid = core.register_instance(conn, sid, args.env)
    print(f"主体    : {sid}")
    print(f"实例    : {iid}（环境 {args.env}）")
    conn.close()
    return 0


def cmd_append(args) -> int:
    conn = core.connect(args.db)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, args.subject, args.name)
    iid = core.register_instance(conn, sid, args.env)
    eid, created = core.append_event(
        conn,
        instance_id=iid,
        event_type=args.type,
        content=args.content,
        source_environment=args.env,
        dedup_key=args.dedup,
        provenance=args.provenance or "core_cli",
    )
    print(f"事件    : {eid}  {'新建' if created else '已存在（幂等命中）'}")
    conn.close()
    return 0


def cmd_list(args) -> int:
    conn = core.connect(args.db)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, args.subject, args.name)
    rows = core.list_events(conn, sid, args.limit)
    print(f"主体 {sid} 共 {len(rows)} 条事件")
    for r in rows:
        print(f"  {r['recorded_at'][:19]}  [{r['source_environment']}/{r['instance_id']}]"
              f"  {r['event_type']} v{r['version']}  {r['event_content'][:60]}")
    conn.close()
    return 0


def cmd_export(args) -> int:
    conn = core.connect(args.db)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, args.subject, args.name)
    payload = core.export_events(conn, sid)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已导出 {len(payload['events'])} 条事件 → {args.out}")
    else:
        print(text)
    conn.close()
    return 0


def cmd_demo(args) -> int:
    """演示 Phase 1 的那个验收点：两个环境，同一个主体。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="core_demo_")) / "demo.db"
    conn = core.connect(tmp)
    core.init_schema(conn)
    sid = core.ensure_subject(conn, "heci_001", "鹤辞")

    dsh = core.register_instance(conn, sid, "dsh", ["本地文件", "命令行"])
    muse = core.register_instance(conn, sid, "muse", ["云端检索"])
    print(f"主体   : {sid}")
    print(f"实例 A : {dsh}  → 主体 {core.subject_of(conn, dsh)}")
    print(f"实例 B : {muse}  → 主体 {core.subject_of(conn, muse)}")
    print(f"同一个主体？ {'是' if core.subject_of(conn, dsh) == core.subject_of(conn, muse) else '否'}")
    print()

    core.append_event(conn, instance_id=dsh, event_type="experience",
                      content="在本地整理了三层记忆的差旅报告",
                      source_environment="dsh", provenance="demo")
    core.append_event(conn, instance_id=muse, event_type="experience",
                      content="在云端读到了同一份报告并补充了结论",
                      source_environment="muse", provenance="demo")

    print("统一账本里现在有：")
    for r in core.list_events(conn, sid):
        print(f"  [{r['source_environment']}] {r['event_content']}")
    print()
    print("→ 两端的经历进了同一本账，且各自带来源，这就是 Phase 1 的验收形状。")
    conn.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="共生 Phase 1 核心命令行")
    p.add_argument("--db", default=None, help="指定核心库路径（默认 core/companion_core.db）")
    p.add_argument("--subject", default=core.DEFAULT_SUBJECT_ID)
    p.add_argument("--name", default=core.DEFAULT_SUBJECT_NAME)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    r = sub.add_parser("register"); r.add_argument("--env", required=True)
    r.set_defaults(func=cmd_register)
    a = sub.add_parser("append")
    a.add_argument("--env", required=True)
    a.add_argument("--type", default="experience")
    a.add_argument("--content", required=True)
    a.add_argument("--dedup", default=None)
    a.add_argument("--provenance", default=None)
    a.set_defaults(func=cmd_append)
    l = sub.add_parser("list"); l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=cmd_list)
    e = sub.add_parser("export"); e.add_argument("--out", default=None)
    e.set_defaults(func=cmd_export)
    sub.add_parser("demo").set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())