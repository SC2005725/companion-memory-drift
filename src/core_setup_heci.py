"""一次性初始化：把测试副本的主体从「小满」切到「鹤辞」。

做四件事：
  1. 确认 heci_001 = 鹤辞（开发者主体）
  2. 登记 xiaoman_001 = 小满（原测试主体，**数据保留、只读**）
  3. 给两个实例写清能力标签
  4. 打印结果供核对

小满的数据一个字节都不动（在 conversation/、memory/、relationship/、personality/）。
"""
from __future__ import annotations

import io
import sqlite3
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import companion_core as core

HECI = "heci_001"
XIAOMAN = "xiaoman_001"

CAPS_DSH = ["本地文件", "命令行", "代码编辑", "git", "运行调试"]
CAPS_MUSE = ["云端检索", "独立判断", "代码审查", "方案设计"]


def main() -> int:
    conn = core.connect()
    core.init_schema(conn)

    # 1) 鹤辞
    core.ensure_subject(conn, HECI, "鹤辞",
                        note="共生项目的共同开发者主体（2026-11-04 建立）")
    conn.execute(
        "UPDATE subjects SET name=?, note=? WHERE subject_id=?",
        ("鹤辞", "共生项目的共同开发者主体（2026-11-04 建立）", HECI))

    # 2) 小满（保留，只读）
    core.ensure_subject(conn, XIAOMAN, "小满",
                        note="原测试主体。人格/记忆/关系/对话数据保留在原目录，"
                             "不再作为开发者主体；不得删除、不得覆盖。")

    # 3) 两个实例
    for env, caps in (("dsh", CAPS_DSH), ("muse", CAPS_MUSE)):
        iid = core.register_instance(conn, HECI, env, caps)
        conn.execute("UPDATE instances SET capabilities=? WHERE instance_id=?",
                     (str(caps).replace("'", '"'), iid))
    conn.commit()

    # 4) 打印
    print("=" * 66)
    print("主体")
    print("=" * 66)
    for r in conn.execute("SELECT subject_id, name, note FROM subjects ORDER BY created_at"):
        print(f"  {r['subject_id']:<14} {r['name']:<6} {r['note']}")
    print()
    print("实例")
    print("=" * 66)
    for r in conn.execute("SELECT instance_id, subject_id, environment, capabilities "
                          "FROM instances ORDER BY instance_id"):
        print(f"  {r['instance_id']:<18} 主体={r['subject_id']:<12} 能力={r['capabilities']}")
    print()
    print("数据归属（小满的保留原位，未复制、未覆盖）")
    print("=" * 66)
    for label, path in (
        ("小满 对话库", "conversation/conversations.db"),
        ("小满 记忆版本", "memory/versions/"),
        ("小满 关系状态", "relationship/"),
        ("小满 人格", "personality/personality.json"),
        ("鹤辞 人格", "core/persona-heci.json"),
        ("鹤辞 关系", "core/relationship-heci.json"),
        ("鹤辞 记忆", "core/memory-heci/"),
        ("鹤辞 核心库", "core/companion_core.db"),
    ):
        print(f"  {label:<14} {path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())