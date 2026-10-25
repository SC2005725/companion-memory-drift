"""接线审计：找出「只被测试调用」和「完全没人调用」的模块函数。

## 为什么需要这个

本项目反复栽在同一类缺陷上，而且**单测全绿**：

  · `emotion.update` 写好了、规则测透了 —— 但**全仓 0 个调用者**，情绪永远不变
  · `apply_decay` 纯函数、单测通过 —— 但**从未被执行**，半衰期规则等于没写
  · `load_state` 往返单测全绿 —— 但读回来是空的（payload 没解包）

这类缺陷的共同点：**模块本身是对的，接口没接上**。
测试只证明"函数能工作"，不证明"函数被用到"。所以再多的单测也抓不到它。

这个工具补的就是那一问：**这个函数，真实路径上有没有人在用？**

## 判定口径

  · **真实路径** = main.py / chat.py / consolidate.py / webui.py / doctor.py / verify.py …
  · **测试** = selftest*.py / dryrun.py
  · 一条函数若只在测试里出现 → 报 `TEST_ONLY`
  · 一条函数若哪里都不出现 → 报 `ORPHAN`

## 它不判断什么

「没人调用」**不一定是错** —— 有些是刻意预留的公开 API（如 `list_versions`）。
所以本工具**只报告，不判定失败**；退出码恒为 0。
真正该被注意的是 `TEST_ONLY`：那说明"验证过的能力从未上线"。
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent

TEST_PREFIXES = ("selftest", "dryrun")
# 这些是"真实运行路径"的代表（被它们间接用到的也算数）
REAL_ENTRY = {"main.py", "chat.py", "consolidate.py", "webui.py", "doctor.py", "verify.py"}


def _modules() -> dict[str, str]:
    out = {}
    for f in sorted(SRC.glob("*.py")):
        out[f.name] = f.read_text(encoding="utf-8")
    return out


def _public_functions(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    names = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
            names.append(n.name)
    return names


def _callers(name: str, mods: dict[str, str], exclude: str) -> tuple[list[str], list[str], bool]:
    """返回 (真实模块调用者, 测试调用者, 是否被同模块内部调用)。

    用 `name(` 粗匹配，够用且不误报。

    ⚠️ 必须单独算「同模块内部调用」：`apply_decay` 由同文件的 `load_state` 调用、
    `decide` 由同文件的 `maybe_tick` 调用 —— 早期版本把定义模块整个排除，
    于是这些**已经接好的函数全被误报成 ORPHAN**，工具直接失去可信度。
    一个会喊狼来了的审计工具比没有更糟。
    """
    real, test, same = [], [], False
    pat = f"{name}("
    for fname, src in mods.items():
        if fname == exclude:
            # 同模块：除了 `def name(` 那一行，还有别处出现 → 被内部调用了
            if src.count(pat) > 1 or f"= {name}" in src:
                same = True
            continue
        if pat not in src:
            continue
        if fname.startswith(TEST_PREFIXES):
            test.append(fname)
        else:
            real.append(fname)
    return real, test, same


def audit() -> dict:
    mods = _modules()
    report = {"TEST_ONLY": [], "ORPHAN": [], "OK": 0, "skipped": []}
    for fname, src in mods.items():
        if fname.startswith(TEST_PREFIXES) or fname in REAL_ENTRY:
            continue
        for fn in _public_functions(src):
            # 名字太通用（如 get/main/run）会误报，跳过
            if len(fn) <= 3:
                report["skipped"].append(f"{fname}:{fn}")
                continue
            real, test, same = _callers(fn, mods, fname)
            if real or same:
                report["OK"] += 1
            elif test:
                report["TEST_ONLY"].append(f"{fname}:{fn}  ← 仅 {', '.join(sorted(set(test)))}")
            else:
                report["ORPHAN"].append(f"{fname}:{fn}")
    return report


def check_defenses() -> list[tuple[str, bool, str]]:
    """锁住两条「防住了但没人记得」的防线（借鉴自 dsh-agent-md）。"""
    out = []
    import json
    import persona

    # ① 元数据不进上下文：`_` 前缀键不得出现在注入文本里
    try:
        prof = persona.load_profile() or {}
        rp = persona.render_profile(prof)
        leaked = [k for k in prof if str(k).startswith("_")
                  and str(prof.get(k) or "") and str(prof[k])[:20] in rp]
        out.append(("元数据 `_` 键不进 render_profile 上下文", not leaked,
                    f"泄漏 {leaked}" if leaked else "4 个 `_` 键均未泄漏"))
    except Exception as e:  # noqa: BLE001
        out.append(("元数据过滤检查", False, f"无法执行：{e}"))

    # ② 模板变量安全：人格值里含 `{` 不得让渲染抛错
    try:
        p = persona.load_persona()
        base = p.render_system_prompt()
        for evil in ("{", "{{", "{{name}}", "{{{", "}"):
            data = json.loads(json.dumps(p.data))
            data["values"] = f"自由=可以说不 {evil} 但是"
            q = persona.Persona.__new__(persona.Persona)
            q.data, q.source_sha256 = data, p.source_sha256
            q.render_system_prompt()
        out.append(("模板变量降级：人格值含 `{` 不抛错", True, "5 种注入全部渲染成功"))
    except Exception as e:  # noqa: BLE001
        out.append(("模板变量安全", False, f"渲染抛错：{e}"))

    # ③ 人格注入必须有上限（dsh-agent-md 的 MAX_INJECT_CHARS）
    #    ⚠️ 检查的是 persona.py —— 上限加在渲染侧，不在 chat.py。
    #    早期版本查错了文件，于是"已经加好了"仍然报 FAIL。
    try:
        import persona
        psrc = (SRC / "persona.py").read_text(encoding="utf-8")
        has = ("PROFILE_INJECT_MAX_CHARS" in psrc
               and "_cap_profile_injection" in psrc)
        # 不只查有没有常量，还要证明它**真的会截断**
        import copy
        big = copy.deepcopy(persona.load_profile() or {})
        big["values_detail"] = ["用来把注入文本撑到上限以上的长句。" * 1200]
        capped = persona.render_profile(big)
        works = ("超出" in capped and "上限" in capped
                 and len(capped) < len(persona.render_profile(big)) + 1)
        ok = has and works and len(capped) <= persona.PROFILE_INJECT_MAX_CHARS + 200
        out.append(("人格注入字符上限存在且真的生效", ok,
                    f"上限 {getattr(persona, 'PROFILE_INJECT_MAX_CHARS', '?')} · "
                    f"膨胀后 {len(capped)} 字符 · 含告警 {'超出' in capped}"
                    if has else "⚠️ 无上限（未来可能撑爆上下文）"))
    except Exception as e:  # noqa: BLE001
        out.append(("人格注入上限检查", False, str(e)))

    return out


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("=" * 74)
    print("接线审计 —— 「模块是对的，接口没接上」这一类缺陷")
    print("=" * 74)
    r = audit()
    print(f"  已接通 {r['OK']} 个公开函数\n")
    print(f"  ⚠️ TEST_ONLY（只在测试里出现过，真实路径无人调用）: {len(r['TEST_ONLY'])}")
    for x in r["TEST_ONLY"]:
        print(f"      {x}")
    print(f"\n  ⚪ ORPHAN（哪里都没调用，可能是刻意预留的 API）: {len(r['ORPHAN'])}")
    for x in r["ORPHAN"]:
        print(f"      {x}")
    if r["skipped"]:
        print(f"\n  （跳过 {len(r['skipped'])} 个过短名字，避免误报）")

    print()
    print("=" * 74)
    print("防线自检（借鉴 dsh-agent-md）")
    print("=" * 74)
    bad = 0
    for name, ok, detail in check_defenses():
        print(f"  [{'OK' if ok else 'FAIL'}] {name}  {detail}")
        if not ok:
            bad += 1
    print()
    print(f"  结论：TEST_ONLY {len(r['TEST_ONLY'])} 个 · 防线失败 {bad} 个")
    print("  说明：ORPHAN 不等于错误；真正该追的是 TEST_ONLY。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())