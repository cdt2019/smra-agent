from __future__ import annotations

import asyncio
import json
import sqlite3

from .db import new_id, now

# 数字越小越先处理
PRIORITY = {"command": 0, "user": 0, "approval": 1, "connection": 1, "review": 1, "browser": 1,
            "handoff": 2, "webhook": 3, "timer": 4}

# 你在宿主终端里连接账号时，命令行进程直接往库里写事件，叫不醒这个进程里的等待。
# 所以等待时每隔一会儿也看一眼库。这只是一次查询，不调用模型。
EXTERNAL_CHECK_S = 5.0


class Inbox:
    """持久的事件列表，是"有什么事要处理"的唯一事实来源。

    事件来源只做一件事：调用 add 写入一行。启动运行是工作进程的事。
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._wake = asyncio.Event()

    def add(self, source: str, source_id: str, payload: dict, session_id: str | None = None) -> bool:
        """写入一个事件。来源和来源标识相同的重复投递被忽略，返回 False。"""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbox (id, source, source_id, session_id, priority, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("in"), source, source_id, session_id, PRIORITY[source],
             json.dumps(payload, ensure_ascii=False), now()),
        )
        if cur.rowcount:
            self._wake.set()
        return bool(cur.rowcount)

    def recover(self) -> int:
        """启动时调用：上次没处理完的事件放回待处理。"""
        cur = self.db.execute("UPDATE inbox SET status = 'PENDING' WHERE status = 'CLAIMED'")
        if cur.rowcount:
            self._wake.set()
        return cur.rowcount

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM inbox WHERE status = 'PENDING' ORDER BY priority, created_at, rowid"
        ).fetchall()

    def has_pending_user(self) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM inbox WHERE status = 'PENDING' AND source = 'user' LIMIT 1"
        ).fetchone()
        return row is not None

    def update_payload(self, inbox_id: str, payload: dict) -> None:
        self.db.execute("UPDATE inbox SET payload = ? WHERE id = ?",
                        (json.dumps(payload, ensure_ascii=False), inbox_id))

    def set_session(self, inbox_id: str, session_id: str) -> None:
        self.db.execute("UPDATE inbox SET session_id = ? WHERE id = ?", (session_id, inbox_id))

    def _set_status(self, ids: list[str], status: str, bump: bool = False) -> None:
        marks = ",".join("?" * len(ids))
        extra = ", attempts = attempts + 1" if bump else ""
        self.db.execute(f"UPDATE inbox SET status = ?{extra} WHERE id IN ({marks})", [status, *ids])

    def claim(self, ids: list[str]) -> None:
        self._set_status(ids, "CLAIMED", bump=True)

    def done(self, ids: list[str]) -> None:
        self._set_status(ids, "DONE")

    def release(self, ids: list[str]) -> None:
        self._set_status(ids, "PENDING")
        self._wake.set()

    def fail(self, ids: list[str]) -> None:
        self._set_status(ids, "FAILED")

    async def wait(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), EXTERNAL_CHECK_S)
        except TimeoutError:
            pass
        self._wake.clear()
