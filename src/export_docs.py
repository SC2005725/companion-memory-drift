"""生成「发给我的文件」：把项目文档转成纯文本，记事本直接可看。

用 Python 生成而不是直接写 .txt，是为了：
1. 保证 UTF-8 编码且不带 BOM（记事本才不会在开头显示乱码方块）；
2. Markdown 转纯文本的规则集中在一处，将来文档更新可以重跑。

用法：python src\\export_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "发给我的文件"

RULES = [
    (re.compile(r"^#{1,6}\s*", re.M), ""),          # 标题井号
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),           # 粗体
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),  # 斜体
    (re.compile(r"`([^`]+)`"), r"\1"),               # 行内代码
    (re.compile(r"^\s*>\s?", re.M), ""),             # 引用
    (re.compile(r"^\s*[-*]\s+", re.M), "  · "),      # 无序列表
    (re.compile(r"^(\s*)\[ \]\s+", re.M), r"\1[ ] "),  # 复选框保持
]


def md_to_text(md: str) -> str:
    # 先处理代码块围栏：直接删掉 ``` 行（保留块内内容，缩进已有）
    md = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*$", "", md, flags=re.M)
    # 水平分隔线换成一行短横线
    md = re.sub(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", "-" * 68, md, flags=re.M)

    text = md
    for pattern, repl in RULES:
        text = pattern.sub(repl, text)
    # Markdown 表格转成靠空格对齐的纯文本
    lines_out: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*\|.*\|\s*$", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # 分隔行丢掉
            lines_out.append("  " + " | ".join(cells))
        else:
            lines_out.append(line)
    text = "\n".join(lines_out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def write_text(name: str, body: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    # newline="\n" + 无 BOM
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def main() -> int:
    made: list[Path] = []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    made.append(write_text("项目说明书.txt", md_to_text(readme)))

    plan = (ROOT / "docs" / "00-方案与可行性分析.md").read_text(encoding="utf-8")
    made.append(write_text("方案与可行性分析.txt", md_to_text(plan)))

    events = (ROOT / "experiments" / "候选事件-请选择一个.md").read_text(encoding="utf-8")
    made.append(write_text("候选事件-请选择一个.txt", md_to_text(events)))

    # 待办说明本身就是纯文本，原样带一份
    todo = (ROOT / "待办-从这里开始.txt").read_text(encoding="utf-8")
    made.append(write_text("待办-从这里开始.txt", todo))

    for path in made:
        size = path.stat().st_size
        print(f"{path.name}  {size} 字节")
    print(f"\n共生成 {len(made)} 个文件，位于：{OUT_DIR.name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
