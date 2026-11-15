"""把《（网名）｜个人动态认知模型》docx 导入成结构化数据。

## 为什么要转

docx 是二进制，接口读不了、git 也比不了。转成 JSON + Markdown 之后：
  · API 能按节返回（不用每次吐 5883 字）
  · git 能追踪每次修改（模型是会更新的，3.1 → 3.2 要看得见差异）
  · 人也能直接读 .md 那份

## 产物

  user_model/songci-cognition-<ver>.json   结构化（给程序）
  user_model/songci-cognition-<ver>.md     可读版（给人）

用法：
    python src\\import_user_model.py "<docx 路径>"
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "user_model"

SECTION_RE = re.compile(r"^(\d{1,2})[｜|]\s*(.+)$")


def extract(docx_path: Path) -> dict:
    from docx import Document

    doc = Document(docx_path)
    raw = "\n".join(p.text for p in doc.paragraphs)
    lines = []
    for ln in raw.splitlines():
        ln = ln.replace("\ufeff", "").strip()
        if not ln or ln == "---":
            continue
        lines.append(ln)

    title = lines[0] if lines else docx_path.stem
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", title) or re.search(r"(\d+\.\d+)", str(docx_path.name))
    version = m.group(1) if m else "unknown"

    sections: list[dict] = []
    head: list[str] = []          # 第一个章节前的内容
    cur: dict | None = None
    for ln in lines:
        hit = SECTION_RE.match(ln)
        if hit:
            if cur:
                sections.append(cur)
            cur = {"no": hit.group(1), "title": hit.group(2).strip(), "lines": []}
            continue
        if cur is None:
            head.append(ln)
        else:
            cur["lines"].append(ln)
    if cur:
        sections.append(cur)

    return {
        "title": title,
        "version": version,
        "source_file": docx_path.name,
        "imported_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "char_count": sum(len(x) for x in lines),
        "line_count": len(lines),
        "head": head,
        "sections": sections,
    }


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    src = Path(args[0])
    if not src.is_file():
        print(f"找不到文件：{src}")
        return 1

    data = extract(src)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"songci-cognition-{data['version']}"

    jp = OUT_DIR / f"{stem}.json"
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# {data['title']}", "",
          f"> 来源：{data['source_file']} ｜ 导入：{data['imported_at']}",
          f"> {data['char_count']} 字 / {len(data['sections'])} 节", ""]
    md += data["head"]
    md.append("")
    for sec in data["sections"]:
        md.append(f"## {sec['no']}｜{sec['title']}")
        md.append("")
        md.extend(sec["lines"])
        md.append("")
    mp = OUT_DIR / f"{stem}.md"
    mp.write_text("\n".join(md), encoding="utf-8")

    print(f"版本    : {data['version']}")
    print(f"字符数  : {data['char_count']}")
    print(f"章节数  : {len(data['sections'])}")
    print(f"JSON    : {jp}")
    print(f"MD      : {mp}")
    print("\n章节清单：")
    for sec in data["sections"]:
        print(f"  {sec['no']}｜{sec['title']}（{len(sec['lines'])} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())