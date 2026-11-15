"""共生 · M2 事件同步器（每个实例各跑一份）。

## 它解决什么

M1 提供了「拉事件」的接口，但**没有地方记住我拉到哪儿了**。每次从头拉，
要么重复处理，要么漏掉。这个模块补的就是那枚书签。

## 两条游标，职责不同

- **本地游标**（`core/sync/<instance_id>.json`）——**权威**。
  处理成功才前进；处理失败就停在这儿，下次接着来。这是「失败后可继续同步」。
- **服务端游标**（`sync_cursors` 表）——只用于观测谁读到哪儿，可以不看。

## 用法

    from core_client import CoreClient
    from core_sync import SyncWorker

    c = CoreClient("http://127.0.0.1:8770", token="…")
    w = SyncWorker(c, instance_id="heci_001_muse")
    for ev in w.pull():            # 拉别人写的、我还没处理的事件
        print(ev["source_environment"], ev["event_content"])
"""
from __future__ import annotations

import json
from pathlib import Path

import companion_core as core


class SyncWorker:
    def __init__(self, client, *, instance_id: str, state_dir: Path | None = None):
        self.client = client
        self.instance_id = instance_id
        self.dir = state_dir or (core.CORE_DIR / "sync")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / f"{instance_id}.json"
        self._state = self._load()

    # --- 游标 ---------------------------------------------------------------
    def _load(self) -> dict:
        if self.state_file.is_file():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"cursor": 0, "updated_at": None}

    def _save(self) -> None:
        self.state_file.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def cursor(self) -> int:
        return int(self._state.get("cursor") or 0)

    # --- 拉取 ---------------------------------------------------------------
    def pull(self, limit: int = 100, handler=None, exclude_self: bool = True) -> list[dict]:
        """拉取并处理新事件。

        `handler(event)` 抛异常时**游标不前进**，下次还会拿到同一批 ——
        这是「失败后可继续同步」的实现方式，不靠重试计数。
        """
        resp = self.client.events(
            since=self.cursor, limit=limit,
            exclude_instance=self.instance_id if exclude_self else None)
        events = resp.get("events") or []
        if not events:
            return []

        if handler is not None:
            for ev in events:
                handler(ev)          # 抛异常 → 直接冒泡，游标不动

        highest = max(int(ev["cursor"]) for ev in events)
        if highest > self.cursor:
            self._state["cursor"] = highest
            self._state["updated_at"] = core._now()
            self._save()

        # 顺手把服务端游标也记一下（纯观测用）
        try:
            conn = core.connect()
            core.init_schema(conn)
            core.set_sync_cursor(conn, self.instance_id, highest)
            conn.close()
        except Exception:            # noqa: BLE001  观测失败不影响正确性
            pass
        return events

    def post(self, *, event_type: str, content: str, dedup_key: str | None = None,
             **kw) -> dict:
        """以本实例身份写一条事件。`dedup_key` 保证重试不重复。"""
        return self.client.post_event(
            instance_id=self.instance_id,
            event_type=event_type,
            content=content,
            source_environment=kw.pop("source_environment", self.instance_id.split("_")[-1]),
            dedup_key=dedup_key,
            **kw,
        )