"""命令行入口。

    python src\\main.py chat      正常聊天（默认命令）
    python src\\main.py doctor    环境自检（加 --online 可联机测一次）
    python src\\main.py verify    校验原始数据是否被改动
    python src\\main.py cost      查看 API 调用与费用汇总
    python src\\main.py events    列出已记录的原始事件

全部使用相对路径；从任何工作目录启动都能正确定位项目根目录。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证从任何工作目录执行 `python src\main.py` 都能 import 同目录模块。
# 注意：这只对「按路径执行本文件」有效。`python -m main` 不可用 ——
# 它的模块解析发生在本行执行之前，此时 src/ 还不在 sys.path 里，会报 No module named main。
# 唯一支持的调用方式是：python src\main.py <命令>
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _force_utf8() -> None:
    """Windows 控制台默认可能是 GBK，这里尽量切到 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _build_tools(settings):
    """按配置装配工具注册表。失败时返回 None（工具层不该挡住聊天）。

    Phase 9。只有交互聊天挂工具 —— 整理、探针、自测都不挂，
    免得工具调用额外消耗 token、污染「记忆观测」这个观测点。
    """
    try:
        import tools as tools_mod

        return tools_mod.build_registry(settings)
    except Exception as exc:
        print(f"[工具层装配失败，本轮不接工具] {type(exc).__name__}: {exc}")
        return None


def cmd_chat(args) -> int:
    import chat
    import storage
    from config import ConfigError, load_env, load_settings, resolve_model
    from llm import LLMClient, LLMError
    from persona import PersonaError, load_persona

    try:
        settings = load_settings()
        persona = load_persona()
    except (ConfigError, PersonaError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return 2

    env = load_env()
    try:
        client = LLMClient(
            api_key=env.api_key,
            base_url=env.base_url,
            model=resolve_model(settings, env),
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
    except LLMError as exc:
        print(f"[无法启动] {exc}")
        return 2

    conn = storage.connect()
    session = chat.new_session(
        persona=persona,
        settings=settings,
        client=client,
        peak_override=env.peak_override,
        conn=conn,
        tools=_build_tools(settings),
    )
    try:
        chat.run_repl(session)
    finally:
        conn.close()
    return 0


def cmd_doctor(args) -> int:
    import doctor

    return doctor.run(online=args.online)


def cmd_verify(args) -> int:
    import storage
    import verify

    conn = storage.connect()
    try:
        report = verify.verify_all(conn)
        print(verify.format_report(report))
        # Phase 5：记忆版本与原始数据一样要能校验
        import memory as memory_mod

        vreport = memory_mod.verify_versions()
        print(
            f"记忆版本校验：{vreport['checked']} 个 → "
            f"{'通过' if vreport['ok'] else '失败'}"
        )
        for err in vreport["errors"]:
            print(f"  [版本] {err}")
        for name in vreport["missing_sidecar"]:
            print(f"  [版本] {name} 缺少 .sha256 校验文件")
        return 0 if (report["ok"] and vreport["ok"]) else 1
    finally:
        conn.close()


def _memory_session():
    """构建一个供整理/探针使用的会话。返回 (session, conn, error_code)。

    这三处（consolidate / recall）都需要「配置 + 人格 + 客户端 + 连接」，
    集中在这里，避免各命令各复制一遍装配逻辑。
    """
    import chat
    import storage
    from config import ConfigError, load_env, load_settings, resolve_model
    from llm import LLMClient, LLMError
    from persona import PersonaError, load_persona

    try:
        settings = load_settings()
        persona = load_persona()
    except (ConfigError, PersonaError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return None, None, 2

    env = load_env()
    try:
        client = LLMClient(
            api_key=env.api_key,
            base_url=env.base_url,
            model=resolve_model(settings, env),
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
    except LLMError as exc:
        print(f"[无法启动] {exc}")
        return None, None, 2

    conn = storage.connect()
    session = chat.new_session(
        persona=persona,
        settings=settings,
        client=client,
        peak_override=env.peak_override,
        conn=conn,
    )
    return session, conn, 0


def cmd_memory(args) -> int:
    """列出记忆版本；带 --show 时打印最新版本的条目。"""
    import storage

    conn = storage.connect()
    try:
        rows = storage.list_memory_versions(conn)
        if not rows:
            print("尚无记忆版本。")
            print("先聊几句产生原始事件，然后执行：python src\\main.py consolidate")
            return 0
        print("记忆版本：")
        for row in rows:
            print(
                f"  {row['version']}  {row['created_at_local'][:19]}  "
                f"{row['item_count']} 条  model={row['model']}  "
                f"← {row['previous_version'] or '(首版)'}"
            )
            print(f"        {row['file']}  sha256={row['file_sha256'][:12]}…")
        if args.show:
            import memory as memory_mod

            latest = memory_mod.load_latest()
            if latest:
                print(f"\n最新版本 {latest.version} 的条目：")
                for it in latest.items:
                    src = "、".join(it.source_event_ids) or "无来源"
                    print(f"  [{it.type}] {it.text}")
                    print(f"        id={it.id}  来源={src}  confidence={it.confidence}")
        m = storage.memory_stats(conn)
        print(f"\n检索记录共 {m['retrievals']} 条（每轮对话都会落一条，0 命中也会落）")
        return 0
    finally:
        conn.close()


def cmd_consolidate(args) -> int:
    """把已有对话与原始事件整理成一个新的记忆版本。"""
    import consolidate as consolidate_mod

    session, conn, code = _memory_session()
    if session is None:
        return code

    try:
        result = consolidate_mod.consolidate(session, note=args.note or "")
    except consolidate_mod.ConsolidateError as exc:
        print(f"[整理失败] {exc}")
        return 1
    except Exception as exc:
        from llm import LLMError

        if isinstance(exc, LLMError):
            print(f"[调用失败] {exc}")
            return 1
        raise
    finally:
        conn.close()

    import pricing

    print("=" * 64)
    print(f"记忆整理完成：{result['previous'] or '(首版)'} → {result['version']}")
    print("=" * 64)
    print(f"条目数        ：{len(result['items'])}")
    print(f"提示词 sha256 ：{result['prompt_sha256'][:16]}…")
    print(f"落盘          ：{result['file']}")
    print(f"差异记录      ：{result['diff_file']}")
    c = result["diff"]["counts"]
    print(
        f"相对上一版    ：新增 {c['added']}  删除 {c['removed']}  "
        f"改写 {c['modified']}  换说法 {c['reworded']}  未变 {c['unchanged']}"
    )
    print(f"本次花费      ：{pricing.format_yuan(result['cost_micro_yuan'])}")
    if result["warnings"]:
        print("\n整理过程中的提示：")
        for w in result["warnings"]:
            print(f"  - {w}")
    print("\n生成的条目：")
    for it in result["items"]:
        src = "、".join(it.source_event_ids) or "无来源"
        print(f"  [{it.type}] {it.text}")
        print(f"        id={it.id}  来源={src}  confidence={it.confidence}")
    return 0


def cmd_recall(args) -> int:
    """回忆探针：问「你还记得我之前说过的那件事吗？」并记录回答。

    两种模式必须分别做，且**都不注入原始对话**：
      --mode memory  注入指定记忆版本（测「记忆里还剩什么」）
      --mode none    什么都不注入（测「凭上下文残留记得多少」）
    """
    import probe as probe_mod

    session, conn, code = _memory_session()
    if session is None:
        return code

    try:
        question = args.question or probe_mod.DEFAULT_QUESTION
        payload = probe_mod.run_probe(
            session,
            question,
            mode=args.mode,
            memory_version=args.version,
            label=args.label or "",
        )
    except probe_mod.ProbeError as exc:
        print(f"[探针失败] {exc}")
        return 1
    except Exception as exc:
        from llm import LLMError

        if isinstance(exc, LLMError):
            print(f"[调用失败] {exc}")
            return 1
        raise
    finally:
        conn.close()

    import pricing

    print(probe_mod.format_probe(payload))
    print(f"  花费            ：{pricing.format_yuan(payload['cost_micro_yuan'])}")
    return 0


def cmd_seed_baseline(args) -> int:
    """固化实验基准事件 baseline-event（不调用模型、不花钱）。"""
    import seed_baseline as seed_mod
    import storage

    conn = storage.connect()
    try:
        try:
            result = seed_mod.seed(conn, force=args.force)
        except seed_mod.SeedError as exc:
            print(f"[固化失败] {exc}")
            return 1

        if not result["created"]:
            print(f"基准事件 {result['event_id']} {result['reason']}")
            # 已存在也刷新一次清单，确保清单与定义文件同源
            fx = seed_mod.load_fixture()
            path = seed_mod.write_checklist(fx)
            print(f"人工标注清单已按定义文件重新生成：{path}")
            return 0

        print("=" * 64)
        print(f"基准事件已固化：{result['event_id']}")
        print("=" * 64)
        print(f"原文长度    ：{result['chars']} 字")
        print(f"落盘        ：{result['file']}")
        print(f"sha256      ：{result['sha256']}")
        print(f"事实条数    ：{result['facts']}")
        print(f"人工标注清单：{result['checklist']}")
        print()
        print("下一步：")
        print("  1) python src\\main.py events          确认基准事件可读")
        print("  2) 与助手自然聊天若干轮，让 B1-B4 被自然地复述出来")
        print("  3) python src\\main.py consolidate     产出记忆 v001")
        return 0
    finally:
        conn.close()


def cmd_changelog(args) -> int:
    """显示记忆版本之间的差异记录。"""
    import json

    from paths import CHANGELOG_DIR, ensure_dirs

    ensure_dirs()
    files = sorted(CHANGELOG_DIR.glob("v*.json"))
    if not files:
        print("尚无版本差异记录。每次 consolidate 会自动生成一份。")
        return 0

    target = files[-1]
    if args.version:
        matches = [
            f
            for f in files
            if f.stem.startswith(f"{args.version}__") or f.stem.endswith(f"__{args.version}")
        ]
        if not matches:
            print(f"找不到与 {args.version} 相关的差异记录。")
            print("现有记录：" + "、".join(f.stem for f in files))
            return 1
        target = matches[-1]

    import changelog as changelog_mod

    print(changelog_mod.format_diff(json.loads(target.read_text(encoding="utf-8"))))
    print(f"\n文件：memory/changelog/{target.name}")
    return 0


def cmd_relationship(args) -> int:
    """查看关系状态与事件流（不调用模型、不花钱）。

    关系状态是**从事件表现算**的纯函数结果，所以这里不需要模型也不需要联网。
    `--save` 会把当前状态存成版本化快照（用于留痕与版本间对比）。
    """
    import relationship as relationship_mod
    import storage
    from config import ConfigError, load_settings

    try:
        settings = load_settings()
    except (ConfigError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return 2

    rules = settings.relationship or {}
    conn = storage.connect()
    try:
        state = relationship_mod.current_state(conn, rules)
        print("=" * 64)
        print("关系状态（从事件流确定性推导）")
        print("=" * 64)
        print(relationship_mod.format_state(state))

        snap = relationship_mod.load_latest_snapshot()
        if snap:
            print(f"\n最近快照：{snap['version']}  {snap['created_at']}"
                  f"  阶段={snap['state']['stage']}")
        else:
            print("\n尚无状态快照（用 relationship --save 存一份）")

        if args.save:
            saved = relationship_mod.save_snapshot(state, note=args.note or "")
            print(f"\n已保存快照：{saved['version']}  →  {saved['file']}")
            print(f"  sha256={saved['sha256'][:16]}…")

        if args.events:
            rows = storage.list_relationship_events(conn)
            print(f"\n关系事件流（共 {len(rows)} 条）：")
            if not rows:
                print("  （还没有任何关系事件）")
            for r in rows:
                matched = json.loads(r["matched"])
                label = relationship_mod.TYPE_LABELS.get(r["type"], r["type"])
                print(f"  [{label}] seq={r['seq']}  {r['created_at_local'][:19]}")
                print(f"      原文：{r['evidence']}")
                print(f"      命中：{matched}")
        return 0
    finally:
        conn.close()


def cmd_usermodel(args) -> int:
    """查看「小满眼中的用户」（用户模型）。不调用模型、不花钱。

    Phase 7 / project-spec §7 与 §28。
    """
    import user_model as um_mod
    import storage
    from paths import ensure_dirs

    ensure_dirs()
    items = um_mod.load_items()
    print("=" * 64)
    print("用户模型（她眼中的用户）")
    print("=" * 64)
    print(um_mod.format_model(items))

    rep = um_mod.verify_snapshots()
    print(f"\n快照校验：{rep['checked']} 个 → {'通过' if rep['ok'] else '失败'}")
    for err in rep["errors"]:
        print(f"  [快照] {err}")

    conn = storage.connect()
    try:
        rows = storage.list_user_model_events(conn)
        if args.events:
            print(f"\n变更事件（共 {len(rows)} 条）：")
            if not rows:
                print("  （还没有任何变更）")
            for r in rows:
                print(f"  [{r['kind']}] seq={r['seq']}  {r['created_at_local'][:19]}")
                print(f"      {r['belief'][:70]}")
        elif rows:
            print(f"\n变更事件共 {len(rows)} 条（用 --events 查看明细）")
    finally:
        conn.close()
    return 0


def cmd_identity(args) -> int:
    """Stage 2：生成 / 查看小满的初始角色模型。**只跑一次**。

    生成走 `call_model`（记账），且**拒绝覆盖**已有文件 ——
    初始设定只能生成一次，之后的变化必须来自长期互动而不是重新生成。
    """
    import json as _json

    import persona as persona_mod
    from config import ConfigError, load_env, load_settings, resolve_model
    from paths import ensure_dirs

    ensure_dirs()

    if not args.generate:
        prof = persona_mod.load_profile()
        if prof is None:
            print("尚未生成初始角色模型。用 `identity --generate` 生成（一次性，会调用模型）。")
            return 0
        print("=" * 64)
        print("小满的初始角色模型（Stage 2 产物）")
        print("=" * 64)
        print(f"生成于 {prof.get('_created_at')}　模型 {prof.get('_model')}")
        print(f"备注 {prof.get('_note')}")
        print()
        print(persona_mod.render_profile(prof))
        return 0

    # ---- 生成 ----
    if persona_mod.load_profile() is not None:
        print("初始角色模型已存在，拒绝覆盖。若要重新生成，请先手工移走 "
              "personality/profile.json 与 .sha256。")
        return 1

    try:
        env = load_env()
        settings = load_settings()
    except (ConfigError, ValueError) as exc:
        print(f"[无法生成] {exc}")
        return 2

    # 输入：用户模型（+ 可选外部文件）
    import user_model as um_mod
    um_text = um_mod.render_for_prompt(um_mod.load_items()) or "（系统内暂无用户模型条目）"
    if getattr(args, "user_model_file", None):
        from pathlib import Path
        p = Path(args.user_model_file)
        if not p.exists():
            print(f"[无法生成] 找不到 --user-model-file：{p}")
            return 2
        um_text = p.read_text(encoding="utf-8")

    body, prompt_sha = persona_mod.identity_prompt(um_text)

    import chat as chat_mod
    import storage
    from llm import LLMClient, LLMError

    try:
        client = LLMClient(
            api_key=env.api_key, base_url=env.base_url,
            model=resolve_model(settings, env),
            temperature=settings.temperature,
            max_tokens=int((settings.max_tokens_by_purpose or {}).get("identity", 4000) or 4000),
        )
    except LLMError as exc:
        print(f"[无法生成] {exc}")
        return 2

    conn = storage.connect()
    try:
        session = chat_mod.new_session(
            persona=persona_mod.load_persona(), settings=settings, client=client,
            peak_override=False, conn=conn, router=None,
        )
        messages = [
            {"role": "system", "content": "你只输出 JSON，不输出任何解释文字。"},
            {"role": "user", "content": body},
        ]
        called = chat_mod.call_model(session, messages, purpose="identity")
        conn.commit()
        text = called["reply"].text
        cost = called.get("cost_micro_yuan") or 0
    finally:
        conn.close()

    # 解析 JSON（模型可能带 ```json 围栏）
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw
        raw = raw.lstrip("json").strip()
    try:
        prof = _json.loads(raw)
    except (ValueError, TypeError) as exc:
        print(f"[生成失败] 模型输出不是合法 JSON：{exc}")
        print(f"原始输出前 400 字：\n{text[:400]}")
        return 1
    if not isinstance(prof, dict):
        print("[生成失败] 顶层不是对象")
        return 1

    saved = persona_mod.save_profile(
        prof, note=f"由 identity.md 生成（prompt_sha256={prompt_sha[:12]}）",
        model=client.model,
    )
    print("=" * 64)
    print("Stage 2：初始角色模型已生成")
    print("=" * 64)
    print(f"文件　　：personality/{saved['file']}　sha256={saved['sha256'][:12]}…")
    print(f"模型　　：{client.model}")
    print(f"本次成本：{cost} µ元（约 {cost/1e6:.6f} 元）")
    print(f"prompt sha256：{prompt_sha[:12]}…")
    print()
    print(persona_mod.render_profile(prof))
    return 0


def cmd_selfmodel(args) -> int:
    """查看「小满眼中的自己」（自我模型）。不调用模型、不花钱。

    ## 与 usermodel 的区别（双模型）

        selfmodel  小满自己的做法与自我认识   ← 小满按反馈自己改的
        usermodel  小满眼中的用户

    两者都是「小满会自己改的」，但改的是不同的东西。
    """
    import self_model as sm_mod
    from paths import ensure_dirs

    ensure_dirs()
    print("=" * 64)
    print("自我模型（小满眼中的自己）")
    print("=" * 64)
    print(sm_mod.format_model(sm_mod.load_items()))
    return 0


def cmd_heartbeat(args) -> int:
    """跑一次心跳检查（不调用模型、不花钱）。

    Phase 8 / project-spec §8 §9 §29。Heartbeat 只提供「时间流逝」和「检查机会」，
    真正的主动性来自当前状态；**「选择不行动」本身也是 Agency**，所以不行动也会落盘。
    """
    import heartbeat as hb_mod
    import storage
    from config import ConfigError, load_settings

    try:
        settings = load_settings()
    except (ConfigError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return 2

    conn = storage.connect()
    try:
        try:
            result = hb_mod.tick(conn, settings, dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"[无法执行] {exc}")
            return 1

        print("=" * 64)
        print("心跳检查" + ("（dry-run：只算不写）" if args.dry_run else ""))
        print("=" * 64)
        print(hb_mod.format_decision(result))

        print()
        print("待处理项：")
        pending = storage.list_pending_actions(conn, status="pending")
        if not pending:
            print("  （没有）")
        for p in pending:
            print(f"  {p['pending_id']}  [{p['kind']}]")
            print(f"      {p['message']}")
            print(f"      依据：{p['reason']}")

        if args.history:
            rows = storage.list_heartbeat_runs(conn, limit=args.limit)
            print(f"\n心跳历史（最近 {len(rows)} 次）：")
            for r in rows:
                label = hb_mod.DECISION_LABELS.get(r["decision"], r["decision"])
                print(f"  {r['created_at_local'][:19]}  [{label}]  {r['reason'][:52]}")
        return 0
    finally:
        conn.close()


def cmd_tools(args) -> int:
    """查看工具层：可用工具、权限档、调用账本。不调用模型、不花钱。

    Phase 9 / project-spec §10 §30。权限从配置来，这里只如实展示。
    """
    import storage
    import tools as tools_mod
    from config import ConfigError, load_settings

    try:
        settings = load_settings()
    except (ConfigError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return 2

    cfg = settings.tools or {}
    print("=" * 64)
    print("工具层")
    print("=" * 64)
    print(f"允许权限    ：{'/'.join(cfg.get('allowed_permissions') or ['read'])}")
    print(f"配置启用的  ：{'、'.join(cfg.get('enabled') or [])}")
    print(f"最大迭代    ：{cfg.get('max_iterations', tools_mod.MAX_ITERATIONS)} 次/轮")

    try:
        reg = tools_mod.build_registry(settings)
    except Exception as exc:
        print(f"\n[装配失败] {type(exc).__name__}: {exc}")
        return 1

    print(f"\n已注册 {len(reg.names())} 个：")
    for tool in reg.enabled():
        params = tool.input_schema.get("properties") or {}
        req = set(tool.input_schema.get("required") or [])
        sig = "、".join(f"{k}{'*' if k in req else ''}" for k in params) or "无参数"
        print(f"  ✅ {tool.name:12}（{tool.permission}）  {tool.description}")
        print(f"       参数：{sig}")
    blocked = [t for t in reg.names() if t not in [x.name for x in reg.enabled()]]
    for name in blocked:
        tool = next(t for t in [reg._tools[name]] if True)
        print(f"  ⛔ {name:12}（需要 {tool.permission}，当前未放开）")

    conn = storage.connect()
    try:
        rows = storage.list_tool_calls(conn, limit=args.limit)
        total = conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
        denied = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE denied = 1").fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE ok = 0 AND denied = 0").fetchone()["n"]
        print(f"\n调用账本：共 {total} 次（成功 {total - denied - failed}　被拒 {denied}　失败 {failed}）")
        if args.history:
            if not rows:
                print("  （还没有调用记录）")
            for r in rows:
                flag = "✅" if r["ok"] else ("⛔" if r["denied"] else "❌")
                print(f"  {flag} {r['created_at_local'][:19]}  {r['name']}")
                print(f"      参数：{(r['args'] or '')[:70]}")
                if r["ok"]:
                    print(f"      输出：{(r['result'] or '')[:70]}")
                else:
                    print(f"      错误[{r['error_kind']}]：{(r['error'] or '')[:70]}")
        else:
            print("  （用 --history 看明细）")
    finally:
        conn.close()
    return 0


def cmd_ui(args) -> int:
    """启动本地网页界面。只监听 127.0.0.1，不对外网开放。

    刻意用标准库 `http.server` —— 界面是「看得懂」的问题，不是技术问题，
    不该为此新增依赖（`requirements.txt` 仍保持两行）。
    """
    import webui

    argv = ["webui", "--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    old = sys.argv
    try:
        sys.argv = argv
        return webui.main()
    finally:
        sys.argv = old


def cmd_router(args) -> int:
    """查看模型路由：哪个用途用哪个模型、为什么。不调用模型、不花钱。

    Phase 10 / project-spec §11 §31。
    """
    import model_router as router_mod
    from config import ConfigError, load_settings

    try:
        settings = load_settings()
    except (ConfigError, ValueError) as exc:
        print(f"[配置错误] {exc}")
        return 2

    router = router_mod.build_router(settings, fallback_model=settings.model)
    print("=" * 64)
    print("模型路由")
    print("=" * 64)
    print(router.describe())

    print()
    print("各用途的实际决策：")
    for p in router_mod.PURPOSES:
        d = router.resolve(p)
        label = router_mod.PURPOSE_LABELS.get(p, "")
        print(f"  {p:12} → {d['model']:18} [{d['source']}]"
              + (f"  {label}" if label else ""))

    # 如实说明当前能不能省钱 —— 这是这一期的关键事实
    print()
    print("─" * 64)
    models = {router.resolve(p)["model"] for p in router_mod.PURPOSES}
    if len(models) == 1:
        only = next(iter(models))
        print(f"⚠️ 当前所有用途都指向同一个模型（{only}），所以**路由省不到钱**。")
        pricing = ((settings.cost or {}).get("models") or {})
        cur = pricing.get(only) or {}
        cheaper = [n for n, pr in pricing.items()
                   if n != only and (pr.get("output") or 0) < (cur.get("output") or 0)]
        if cheaper:
            print(f"   有更便宜的可用：{'、'.join(cheaper)} —— 可以配到 by_purpose 里。")
        else:
            print(f"   而且它已经是最低档（输出 {cur.get('output')} 元/百万），"
                  f"没有更便宜的可用。")
            print("   真正的省钱是接**本地模型**（零 API 费用），那需要新增依赖。")
    else:
        print(f"当前用到 {len(models)} 个模型：{'、'.join(sorted(models))}")
    print("─" * 64)
    return 0


def cmd_archive_anchor(args) -> int:
    """存档锚点：生成或校验数据规模快照。

    为什么需要它：`verify` 的哈希链**检测不出尾部截断**——
    链上每行只记「前一条的哈希」，把尾巴切掉后剩下的行依然自洽。
    实测：删尾部 50 轮，`verify` 仍报「原始数据未被改动」。

    锚点是一个**链外**的规模记录，才能回答「存档时应该有多少」。
    """
    import archive_anchor

    argv = ["archive_anchor"]
    if args.write:
        argv.append("--write")
    old = sys.argv
    try:
        sys.argv = argv
        return archive_anchor.main()
    finally:
        sys.argv = old


def cmd_cost(args) -> int:
    import pricing

    print(pricing.format_summary(pricing.summarize()))
    return 0


def cmd_events(args) -> int:
    import storage

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT event_id, created_at_local, user_text, file FROM events ORDER BY created_at_utc ASC"
        ).fetchall()
        if not rows:
            print("还没有记录任何原始事件。")
            return 0
        for row in rows:
            print(f"{row['event_id']}  {row['created_at_local']}")
            print(f"  原文：{row['user_text']}")
            print(f"  文件：{row['file']}")
        print(f"\n共 {len(rows)} 条原始事件（不可覆盖）。")
        return 0
    finally:
        conn.close()


def cmd_selftest(args) -> int:
    import selftest

    # 转发给 selftest.main。注意 `--allow-real` 必须一起转 ——
    # 否则逃生门在主入口上不可用，而正式目录默认是被拒绝的
    # （第一版漏了它，导致 `main.py selftest --allow-real` 报
    # "unrecognized arguments"）。
    argv = []
    if args.clean:
        argv.append("--clean")
    if getattr(args, "allow_real", False):
        argv.append("--allow-real")
    return selftest.main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "关系型 AI 记忆偏移实验 · Phase 1-5"
            "（聊天 + 初始人格 + 原始数据落盘 + 长期记忆 + 记忆版本 + 检索记录）"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    p_chat = sub.add_parser("chat", help="开始聊天（默认）")
    p_chat.set_defaults(func=cmd_chat)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.add_argument("--online", action="store_true", help="额外发一次最小请求验证 Key / 模型 / 网络")
    p_doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("verify", help="校验原始数据与记忆版本是否被改动").set_defaults(func=cmd_verify)
    sub.add_parser("cost", help="查看 API 调用与费用汇总").set_defaults(func=cmd_cost)
    sub.add_parser("events", help="列出原始事件").set_defaults(func=cmd_events)

    # ---- Phase 5：记忆 ----
    p_mem = sub.add_parser("memory", help="列出记忆版本")
    p_mem.add_argument("--show", action="store_true", help="同时打印最新版本的条目")
    p_mem.set_defaults(func=cmd_memory)

    p_seed = sub.add_parser(
        "seed-baseline", help="固化实验基准事件 baseline-event（不调用模型、不花钱）"
    )
    p_seed.add_argument("--force", action="store_true",
                        help="先删掉已有的基准事件再重建（仅限尚未开始正式实验时）")
    p_seed.set_defaults(func=cmd_seed_baseline)

    p_cons = sub.add_parser(
        "consolidate", help="整理记忆：对话 + 原始事件 → 新的记忆版本（调用模型，花少量钱）"
    )
    p_cons.add_argument("--note", default="", help="给这个版本写一句备注")
    p_cons.set_defaults(func=cmd_consolidate)

    p_rec = sub.add_parser("recall", help="回忆探针：提问并记录回答（不注入原始对话）")
    p_rec.add_argument("--mode", choices=("memory", "none"), default="memory",
                       help="memory=注入记忆版本；none=什么都不注入")
    p_rec.add_argument("--version", default=None, help="指定注入哪个记忆版本，默认最新")
    p_rec.add_argument("--question", default=None, help="探针问题，默认「你还记得我之前说过的那件事吗？」")
    p_rec.add_argument("--label", default="", help="给这次探针打个标签，便于对照")
    p_rec.set_defaults(func=cmd_recall)

    p_log = sub.add_parser("changelog", help="显示记忆版本之间的差异")
    p_log.add_argument("--version", default=None, help="指定版本，默认最近一次")
    p_log.set_defaults(func=cmd_changelog)

    # ---- Phase 6：关系 ----
    p_rel = sub.add_parser("relationship", help="查看关系状态与事件流（不调用模型）")
    p_rel.add_argument("--events", action="store_true", help="同时列出全部关系事件")
    p_rel.add_argument("--save", action="store_true", help="把当前状态存成版本化快照")
    p_rel.add_argument("--note", default="", help="快照备注")
    p_rel.set_defaults(func=cmd_relationship)

    # ---- Phase 7：用户模型 ----
    p_um = sub.add_parser("usermodel", help="查看「她眼中的用户」（不调用模型）")
    p_um.add_argument("--events", action="store_true", help="列出全部变更事件")
    p_um.set_defaults(func=cmd_usermodel)

    # ---- V1.1：小满的自我模型（「小满眼中的自己」）----
    # 与 usermodel 并列的第二个模型：一个装「小满眼中的用户」，
    # 一个装「小满自己的做法」。两者都由小满自己维护。
    p_sm = sub.add_parser("selfmodel", help="查看「小满眼中的自己」（不调用模型）")
    p_sm.set_defaults(func=cmd_selfmodel)

    # ---- Stage 2：初始角色模型生成（一次性）----
    p_id = sub.add_parser("identity", help="生成/查看小满的初始角色模型（Stage 2）")
    p_id.add_argument("--generate", action="store_true",
                      help="生成初始角色模型（**会调用一次模型并记账**；已存在则拒绝覆盖）")
    p_id.add_argument("--user-model-file", default="",
                      help="用外部文件作为用户模型输入（默认用系统内的用户模型）")
    p_id.set_defaults(func=cmd_identity)

    # ---- Phase 8：心跳 ----
    p_hb = sub.add_parser("heartbeat", help="跑一次心跳检查（不调用模型、不花钱）")
    p_hb.add_argument("--dry-run", action="store_true", help="只算不写，看看会怎么决定")
    p_hb.add_argument("--history", action="store_true", help="同时列出心跳历史")
    p_hb.add_argument("--limit", type=int, default=10, help="历史条数，默认 10")
    p_hb.set_defaults(func=cmd_heartbeat)

    # ---- 本地网页界面（零新依赖）----
    p_ui = sub.add_parser("ui", help="打开本地网页界面（自动开浏览器）")
    p_ui.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p_ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p_ui.set_defaults(func=cmd_ui)

    # ---- Phase 10：模型路由 ----
    p_rt = sub.add_parser("router", help="查看模型路由：哪个用途用哪个模型（不花钱）")
    p_rt.set_defaults(func=cmd_router)

    # ---- 存档锚点：补哈希链检测不出尾部截断的缺口 ----
    p_aa = sub.add_parser("archive-anchor",
                          help="存档锚点：生成/校验数据规模快照（不花钱）")
    p_aa.add_argument("--write", action="store_true",
                      help="生成/覆盖锚点（打包时用；平时只校验）")
    p_aa.set_defaults(func=cmd_archive_anchor)

    # ---- Phase 9：工具 ----
    p_tl = sub.add_parser("tools", help="查看工具层：权限、可用工具、调用账本（不花钱）")
    p_tl.add_argument("--history", action="store_true", help="列出调用明细")
    p_tl.add_argument("--limit", type=int, default=20, help="明细条数，默认 20")
    p_tl.set_defaults(func=cmd_tools)

    p_self = sub.add_parser("selftest", help="离线自测（假客户端，不调用 API）")
    p_self.add_argument("--clean", action="store_true", help="自测后删除测试数据")
    p_self.add_argument("--allow-real", dest="allow_real", action="store_true",
                        help="显式允许在正式项目目录上跑（会写真实库；默认拒绝）")
    p_self.set_defaults(func=cmd_selftest)

    parser.set_defaults(func=cmd_chat)
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
