"""共生 · M1 客户端（给另一个运行实例用）。

它只依赖标准库，复制到 Muse 那台机器上就能跑。

    from core_client import CoreClient
    c = CoreClient("https://<tailnet-host>.ts.net:8770", token="…")
    print(c.whoami())

    # 拉别人写的事件（增量）
    r = c.events(since=0, exclude_instance="heci_001_muse")

    # 写一条我这边的事件
    c.post_event(instance_id="heci_001_muse", event_type="experience",
                 content="在云端读完了一份报告", source_environment="muse",
                 dedup_key="msg-20261104-01")

命令行也能直接用：

    python src\\core_client.py --base http://127.0.0.1:8770 --token <token> state
    python src\\core_client.py ... events --since 0
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def _setup_stdout() -> None:
    """只在当脚本跑时改 stdout；被 import 时不动（否则会重复包装、关掉上层的流）。"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


class CoreError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class CoreClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # --- 底层 ---------------------------------------------------------------
    def _req(self, method: str, path: str, params: dict | None = None,
             payload: dict | None = None) -> dict:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise CoreError(e.code, e.read().decode("utf-8", "replace")) from None

    def get(self, route: str, **params) -> dict:
        return self._req("GET", route, params=params)

    def post(self, route: str, payload: dict | None = None) -> dict:
        return self._req("POST", route, payload=payload or {})

    # --- 接口 ---------------------------------------------------------------
    def health(self) -> dict:
        return self.get("/api/health")

    def whoami(self) -> dict:
        """主体 + 实例列表 + 事件游标。"""
        return self.get("/api/subject")

    def persona(self) -> dict:
        return self.get("/api/persona")

    def relationship(self) -> dict:
        return self.get("/api/relationship")

    def state(self) -> dict:
        return self.get("/api/state")

    def register_instance(self, environment: str, capabilities: list[str] | None = None,
                          subject_id: str | None = None) -> dict:
        return self.post("/api/instances/register", {
            "environment": environment,
            "capabilities": capabilities or [],
            "subject_id": subject_id,
        })

    def search_memory(self, q: str, k: int = 8) -> dict:
        return self.get("/api/memory/search", q=q, k=k)

    def retrieve_memory(self, q: str, k: int = 5) -> dict:
        """G-1：正式检索引擎（只读）。与 search_memory 的差别是引擎质量——
        本方法走主链同款 tokenize/扩词/recency tie-break，见
        docs/双端互通-研究报告-20261114.md。"""
        return self.get("/api/memory/retrieve", q=q, k=k)

    def heartbeat_tick(self) -> dict:
        """G-2：触发一次心跳检查（零模型；MIN_GAP 节流内会如实返回未执行）。"""
        return self.post("/api/heartbeat/tick", {})

    def proactive_note(self, *, message: str, instance_id: str | None = None,
                       kind: str | None = None, reason: str | None = None,
                       push: bool = False) -> dict:
        """G-3：注入一条「想对用户说的话」（留痕在前，push=True 时顺带推手机）。
        兑现走既有通道：用户开口时注入或心跳 tick。"""
        return self.post("/api/proactive/note", {
            "message": message,
            "instance_id": instance_id,
            "kind": kind,
            "reason": reason,
            "push": push,
        })

    def propose_memory(self, *, instance_id: str, items: list[dict],
                       source_environment: str, note: str | None = None,
                       client_request_id: str | None = None) -> dict:
        return self.post("/api/memory/propose", {
            "instance_id": instance_id,
            "items": items,
            "source_environment": source_environment,
            "note": note,
            "client_request_id": client_request_id,
        })

    def candidates(self, status: str = "pending", limit: int = 50) -> dict:
        return self.get("/api/memory/candidates", status=status, limit=limit)

    def events(self, since: int = 0, limit: int = 100,
               exclude_instance: str | None = None) -> dict:
        return self.get("/api/events", since=since, limit=limit,
                        exclude_instance=exclude_instance)

    def post_event(self, *, instance_id: str, event_type: str, content: str,
                   source_environment: str, dedup_key: str | None = None,
                   occurred_at: str | None = None, participants: list[str] | None = None,
                   task_id: str | None = None, result: str | None = None,
                   provenance: str | None = None,
                   supersedes: str | None = None) -> dict:
        return self.post("/api/events", {
            "instance_id": instance_id,
            "event_type": event_type,
            "content": content,
            "source_environment": source_environment,
            "dedup_key": dedup_key,
            "occurred_at": occurred_at,
            "participants": participants,
            "task_id": task_id,
            "result": result,
            "provenance": provenance,
            "supersedes": supersedes,
        })

    # --- M3：协作任务 -------------------------------------------------------
    def create_task(self, *, from_instance: str, to_instance: str, title: str,
                    detail: str | None = None, context: dict | None = None,
                    max_rounds: int = 3,
                    client_request_id: str | None = None) -> dict:
        return self.post("/api/tasks", {
            "from_instance": from_instance,
            "to_instance": to_instance,
            "title": title,
            "detail": detail,
            "context": context,
            "max_rounds": max_rounds,
            "client_request_id": client_request_id,
        })

    def tasks(self, instance_id: str, role: str = "inbox",
              status: str | None = None, limit: int = 50) -> dict:
        return self.get("/api/tasks", instance_id=instance_id, role=role,
                        status=status, limit=limit)

    def task(self, task_id: str) -> dict:
        return self.get(f"/api/tasks/{task_id}")

    def task_act(self, task_id: str, *, instance_id: str, action: str,
                 note: str | None = None, result: str | None = None) -> dict:
        return self.post(f"/api/tasks/{task_id}/act", {
            "instance_id": instance_id,
            "action": action,
            "note": note,
            "result": result,
        })

    # --- M4：心跳与在线状态 -------------------------------------------------
    def heartbeat(self, instance_id: str) -> dict:
        """报到一次，顺便拿回「我该干什么」。"""
        return self.post("/api/instances/heartbeat", {"instance_id": instance_id})

    def instances(self) -> dict:
        return self.get("/api/instances")

    def inbox(self, instance_id: str) -> dict:
        return self.get("/api/inbox", instance_id=instance_id)

    # --- 文件租约（避免两个实例同时改同一个文件）---------------------------
    def lease(self, path: str, instance_id: str, ttl_seconds: int = 3600,
              task_id: str | None = None, note: str | None = None) -> dict:
        return self.post("/api/leases/acquire", {
            "path": path, "instance_id": instance_id,
            "ttl_seconds": ttl_seconds, "task_id": task_id, "note": note,
        })

    def unlease(self, path: str, instance_id: str) -> dict:
        return self.post("/api/leases/release", {
            "path": path, "instance_id": instance_id})

    def leases(self) -> dict:
        return self.get("/api/leases")

    # --- 文件访问（改文件前必须先领租约）-----------------------------------
    def fs_map(self) -> dict:
        return self.get("/api/fs/map")

    def ls(self, path: str = ".") -> dict:
        return self.get("/api/fs/list", path=path)

    def read(self, path: str, start_line: int = 1, max_bytes: int | None = None) -> dict:
        return self.get("/api/fs/read", path=path, start_line=start_line,
                        max_bytes=max_bytes)

    def write(self, path: str, content: str, instance_id: str,
              lease_epoch: int | None = None) -> dict:
        """写文件。**必须已持有该文件租约，并携带当前 lease_epoch**，否则 409。

        2026-11-07（fencing）：每次 acquire 都让该 path 的 epoch +1，
        写请求必须带上 acquire 响应里那个数。旧 epoch 一律 409 —— 防的是
        「租约被抢/过期后重新领取，前任以为自己还在持锁」的脑裂。

        不知道 epoch 就改用 `write_with_lease()`，它自动领、自动带、自动放。
        """
        payload = {"instance_id": instance_id, "path": path, "content": content}
        if lease_epoch is not None:
            payload["lease_epoch"] = lease_epoch
        return self.post("/api/fs/write", payload)

    def write_with_lease(self, path: str, content: str, instance_id: str,
                         ttl_seconds: int = 1800, release: bool = True) -> dict:
        """领租约 → 写 → 释放，一步到位。

        适合「改一下就完事」的场景。要连续改多个文件时别用它，
        手动 acquire 一次更省事（同一 path 反复 acquire 会让 epoch 一直涨）。
        """
        L = self.lease(path, instance_id, ttl_seconds=ttl_seconds)
        epoch = L.get("epoch")
        if epoch is None:
            raise CoreError(500, f"acquire 没返回 epoch：{L}")
        try:
            return self.write(path, content, instance_id, lease_epoch=epoch)
        finally:
            if release:
                try:
                    self.unlease(path, instance_id)
                except CoreError:
                    pass      # 释放失败不该盖掉写入结果

    def mkdir(self, path: str, instance_id: str, lease_epoch: int | None = None) -> dict:
        payload = {"instance_id": instance_id, "path": path}
        if lease_epoch is not None:
            payload["lease_epoch"] = lease_epoch
        return self.post("/api/fs/mkdir", payload)

    def rm(self, path: str, instance_id: str, lease_epoch: int | None = None) -> dict:
        """删文件。同样要租约 + epoch（服务端对 delete 一视同仁）。删完自动释放租约。"""
        payload = {"instance_id": instance_id, "path": path}
        if lease_epoch is not None:
            payload["lease_epoch"] = lease_epoch
        return self.post("/api/fs/delete", payload)

    def rm_with_lease(self, path: str, instance_id: str, ttl_seconds: int = 600) -> dict:
        """领租约 → 删 → （服务端已自动释放）"""
        L = self.lease(path, instance_id, ttl_seconds=ttl_seconds)
        return self.rm(path, instance_id, lease_epoch=L.get("epoch"))

    def exec(self, cmd: list[str], instance_id: str | None = None) -> dict:
        """执行开发命令（白名单：git 子命令、python -m py_compile）。

        2026-11-07 起 exec 必须带已登记 instance_id（之前漏了，可无身份执行）。
        """
        return self.post("/api/fs/exec",
                         {"cmd": cmd, "instance_id": instance_id})

    def edit(self, path: str, instance_id: str, find: str, replace: str,
             lease_epoch: int | None = None) -> dict:
        """读-改-写一步做完，省一次往返。仍需先持租约，且写时带 epoch。"""
        cur = self.read(path)
        if find not in cur["content"]:
            raise CoreError(404, f"{path} 里找不到要替换的片段")
        new = cur["content"].replace(find, replace, 1)
        return self.write(path, new, instance_id, lease_epoch=lease_epoch)


def main(argv=None) -> int:
    _setup_stdout()
    ap = argparse.ArgumentParser(description="共生 M1 客户端")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    ap.add_argument("--token", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("whoami")
    sub.add_parser("persona")
    sub.add_parser("relationship")
    sub.add_parser("state")
    s = sub.add_parser("search"); s.add_argument("q"); s.add_argument("--k", type=int, default=8)
    c = sub.add_parser("candidates"); c.add_argument("--status", default="pending")
    e = sub.add_parser("events"); e.add_argument("--since", type=int, default=0)
    e.add_argument("--limit", type=int, default=20)
    e.add_argument("--exclude-instance", default=None)

    args = ap.parse_args(argv)
    cli = CoreClient(args.base, args.token)
    if args.cmd == "health":
        out = cli.health()
    elif args.cmd == "whoami":
        out = cli.whoami()
    elif args.cmd == "persona":
        out = cli.persona()
    elif args.cmd == "relationship":
        out = cli.relationship()
    elif args.cmd == "state":
        out = cli.state()
    elif args.cmd == "search":
        out = cli.search_memory(args.q, args.k)
    elif args.cmd == "candidates":
        out = cli.candidates(status=args.status)
    else:
        out = cli.events(since=args.since, limit=args.limit,
                         exclude_instance=args.exclude_instance)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())