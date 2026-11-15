"""Muse 侧自检：一条命令验完「能不能连、能不能读、能不能写、游标能不能续」。

**单文件、只依赖标准库**，直接复制到 Muse 那台机器就能跑。

    python muse_check.py --base https://<tailnet-host>.ts.net:8443 --token <令牌>

它会依次检查：网络可达 → 认证 → 主体身份 → 实例登记 → 心跳 → 人格/状态 →
增量事件 → 提交事件（幂等）→ 本地游标持久化，最后打印 PASS/FAIL 清单。

退出码：0 全通；1 有失败。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


class Api:
    def __init__(self, base: str, token: str, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict | None = None,
             auth: bool = True) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {e.code}: {body}") from None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Muse 侧连通性自检")
    ap.add_argument("--base", required=True, help="例如 https://xxx.ts.net:8443")
    ap.add_argument("--token", required=True, help="core/api-token 的内容")
    ap.add_argument("--instance", default="heci_001_muse")
    ap.add_argument("--cursor-file", default="muse_sync_cursor.json")
    args = ap.parse_args(argv)

    api = Api(args.base, args.token)
    cursor_path = Path(args.cursor_file)

    print("=" * 66)
    print(f"Muse 自检 · {args.base}")
    print(f"实例   · {args.instance}")
    print("=" * 66)

    print("\n[1] 网络与证书")
    try:
        h = api.call("GET", "/api/health", auth=False)
        check("HTTPS 可达（能建立 TLS 连接）", True, args.base)
        check("服务是 companion-core", h.get("service") == "companion-core", str(h))
    except Exception as exc:  # noqa: BLE001
        check("HTTPS 可达（能建立 TLS 连接）", False, str(exc)[:120])
        print("\n→ 网络不通。Muse 侧先确认：")
        print("  1) 装了 Tailscale 且 tailscale status 能看到 <tailnet-host>")
        print("  2) 登录的是同一个账号（ht1486663@）")
        print("  3) serve 还在：在 DSH 那台机器上跑 tailscale serve status 应看到 8443")
        return 1

    print("\n[2] 认证")
    try:
        api.call("GET", "/api/subject")
        check("Bearer 令牌被接受", True)
        bad = Api(args.base, "definitely-wrong-token")
        try:
            bad.call("GET", "/api/subject")
            check("错误令牌应被拒", False, "竟然通过了")
        except RuntimeError as e:
            check("错误令牌被拒（401）", "401" in str(e), str(e)[:60])
    except Exception as exc:  # noqa: BLE001
        check("Bearer 令牌被接受", False, str(exc)[:120])
        print("\n→ 令牌不对。到 DSH 那台机器读 core/api-token，逐字复制。")
        return 1

    print("\n[3] 主体身份")
    try:
        who = api.call("GET", "/api/subject")
        sid = who["subject"]["subject_id"]
        check("读到主体", sid == "heci_001", f"{sid} / {who['subject']['name']}")
        check("主体带版本", bool(who["subject"].get("version")), str(who["subject"].get("version")))
        check("能看到事件游标", isinstance(who.get("cursor"), int), str(who.get("cursor")))
    except Exception as exc:  # noqa: BLE001
        check("读到主体", False, str(exc)[:120])

    print("\n[4] 实例登记与心跳")
    try:
        r = api.call("POST", "/api/instances/register",
                     {"environment": "muse", "capabilities": ["云端检索", "独立判断"]})
        check("muse 实例可登记/已存在", r.get("instance_id") == args.instance, r.get("instance_id"))
        hb = api.call("POST", "/api/instances/heartbeat", {"instance_id": args.instance})
        online = [i["instance_id"] for i in hb.get("instances", []) if i.get("online")]
        check("心跳成功", True, f"在线实例 {online}")
        check("inbox 能取到", "inbox" in hb, str(hb.get("inbox", {}).get("should_act")))
    except Exception as exc:  # noqa: BLE001
        check("心跳成功", False, str(exc)[:120])

    print("\n[5] 读人格与状态")
    try:
        p = api.call("GET", "/api/persona")["persona"]
        check("人格可读", p.get("available") is True, f"{p.get('name')} legacy={p.get('legacy')}")
        check("人格不是小满", p.get("name") != "小满", str(p.get("name")))
        st = api.call("GET", "/api/state")["state"]
        check("状态可读", bool(st.get("subject_id")),
              f"事件 {st.get('event_count')} 记忆 {st.get('memory_version')}")
    except Exception as exc:  # noqa: BLE001
        check("人格可读", False, str(exc)[:120])

    print("\n[6] 增量事件（断点续传）")
    since = 0
    if cursor_path.is_file():
        try:
            since = int(json.loads(cursor_path.read_text(encoding="utf-8")).get("cursor", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            since = 0
    try:
        ev = api.call("GET", f"/api/events?since={since}&limit=20&exclude_instance={args.instance}")
        events = ev.get("events") or []
        check("按游标拉取成功", True, f"since={since} 拉到 {len(events)} 条，服务端游标 {ev.get('cursor')}")
        if events:
            check("事件带来源", all("source_environment" in e for e in events),
                  str({e["source_environment"] for e in events}))
            check("事件带实例", all("instance_id" in e for e in events))
            newest = max(int(e["cursor"]) for e in events)
        else:
            newest = since
    except Exception as exc:  # noqa: BLE001
        check("按游标拉取成功", False, str(exc)[:120])
        newest = since

    print("\n[7] 提交事件（幂等）")
    try:
        key = "muse-check-probe"
        r1 = api.call("POST", "/api/events", {
            "instance_id": args.instance, "event_type": "diagnostic",
            "content": "Muse 侧自检：这条是连通性探针，可重复运行。",
            "source_environment": "muse", "dedup_key": key, "provenance": "muse_check"})
        r2 = api.call("POST", "/api/events", {
            "instance_id": args.instance, "event_type": "diagnostic",
            "content": "Muse 侧自检：这条是连通性探针，可重复运行。",
            "source_environment": "muse", "dedup_key": key, "provenance": "muse_check"})
        check("写事件成功", r1.get("ok") is True, r1.get("event_id"))
        check("重复写不新增（幂等）", r2.get("created") is False,
              f"created={r2.get('created')}")
        newest = max(newest, int(r1.get("cursor") or 0))
    except Exception as exc:  # noqa: BLE001
        check("写事件成功", False, str(exc)[:120])

    print("\n[8] 本地游标持久化")
    try:
        cursor_path.write_text(json.dumps(
            {"instance_id": args.instance, "cursor": newest, "base": args.base},
            ensure_ascii=False, indent=2), encoding="utf-8")
        back = json.loads(cursor_path.read_text(encoding="utf-8"))
        check("游标写入本地", back["cursor"] == newest, str(cursor_path))
        check("下次可从这里续传", True, f"cursor={newest}")
    except Exception as exc:  # noqa: BLE001
        check("游标写入本地", False, str(exc)[:120])

    print("\n" + "=" * 66)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项。")
    if failed:
        for n in failed:
            print(f"  失败：{n}")
    else:
        print("Muse 侧全部打通：能连、能读、能写、游标能续。")
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())