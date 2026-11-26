"""发件箱与分发器。设计文档第 14 节。

| 类型     | 谁写入                         | 送达时机                                   |
| reply    | 你的消息、你的审批答复触发的运行 | 立刻                                       |
| now      | 后台运行，Agent 要求即时         | 立刻；每日上限，超出降为摘要；安静时段延后 |
| digest   | 后台运行，不急                   | 攒到下一个摘要时间，合成一条               |
| approval | 裁决者                           | 立刻，不受上限和安静时段约束               |
| review   | 宿主（Agent 提交了档案等待审阅） | 立刻，不受上限和安静时段约束               |
| system   | 宿主自己的提醒（注入、新日程……） | 不受上限约束；安静时段延后                 |

节奏在写入时就决定好（降级、送达时间），分发器只按 deliver_after 送。上限只能在配置里改，Agent 改不了。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, time, timedelta

from .db import new_id, now

log = logging.getLogger("outbox")

# Markdown 里的远程图片。图片地址可以夹带数据，是一条外泄通道，送达前去掉。
_REMOTE_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*https?://[^)]*\)", re.IGNORECASE)

MAX_ATTEMPTS = 5
FEEDBACK = {"useful": "有用", "unnecessary": "没必要"}


def sanitize(text: str) -> str:
    return _REMOTE_IMAGE.sub("[已移除远程图片]", text)


def _hhmm(text: str) -> time:
    h, _, m = text.strip().partition(":")
    return time(int(h), int(m or 0))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class Outbox:
    def __init__(self, db: sqlite3.Connection, cfg):
        self.db = db
        self.cfg = cfg
        self.clock = lambda: datetime.now(UTC)
        self._wake = asyncio.Event()

    # ---------------------------------------------------------------- 节奏

    def _quiet_end(self, at: datetime) -> datetime | None:
        """at 落在安静时段里时，返回安静时段结束的时刻；否则 None。"""
        if not self.cfg.quiet_hours:
            return None
        start_text, _, end_text = self.cfg.quiet_hours.partition("-")
        start, end = _hhmm(start_text), _hhmm(end_text)
        local = at.astimezone().replace(tzinfo=None)
        t = local.time()
        inside = (start <= t < end) if start < end else (t >= start or t < end)
        if not inside:
            return None
        day = local.date() if t < end else local.date() + timedelta(days=1)
        return datetime.combine(day, end).astimezone(UTC)

    def _next_digest(self, at: datetime) -> datetime:
        local = at.astimezone().replace(tzinfo=None)
        candidates = []
        for text in self.cfg.digest_times:
            slot = datetime.combine(local.date(), _hhmm(text))
            candidates.append(slot if slot > local else slot + timedelta(days=1))
        return min(candidates).astimezone(UTC)

    def _now_sent_today(self, at: datetime) -> int:
        midnight = datetime.combine(at.astimezone().date(), time()).astimezone(UTC)
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE kind = 'now' AND created_at >= ?", (_iso(midnight),)
        ).fetchone()["n"]

    def enqueue(self, kind: str, session_id: str | None, body: str, dedupe_key: str,
                attachment: str | None = None, meta: dict | None = None) -> bool:
        """写入一条待发消息。dedupe_key 相同的只保留第一条，返回 False 表示重复。"""
        if self.db.execute("SELECT 1 FROM outbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone():
            return False
        at = self.clock()
        requested, deliver_after = kind, None
        if kind == "now" and self._now_sent_today(at) >= self.cfg.notify_daily_cap:
            kind = "digest"         # 今天的即时通知用完了
        if kind in ("now", "system"):
            deliver_after = self._quiet_end(at)
        elif kind == "digest":
            deliver_after = self._next_digest(at)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO outbox (id, kind, requested, session_id, body, attachment, meta, dedupe_key, "
            "deliver_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("out"), kind, requested, session_id, body, attachment,
             json.dumps(meta, ensure_ascii=False) if meta else None, dedupe_key,
             _iso(deliver_after) if deliver_after else None, _iso(at)),
        )
        if cur.rowcount:
            self._wake.set()
        return bool(cur.rowcount)

    def poke(self) -> None:
        """让分发器马上再看一遍（时钟变了，或者有新消息）。"""
        self._wake.set()

    # ---------------------------------------------------------------- 留底与反馈

    def recent_notices(self, limit: int = 10) -> list[dict]:
        """最近送达的通知和摘要项，即"已经告诉过你的"。由宿主记录，比 Agent 自己记的可靠。

        回复不在其中：它们本来就在对话记录里。审批也不在：审批只在你和裁决者之间。
        """
        rows = self.db.execute(
            "SELECT kind, body, sent_at, feedback FROM outbox WHERE status = 'SENT' "
            "AND kind IN ('now', 'digest') ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"kind": r["kind"], "body": r["body"], "sent_at": r["sent_at"],
                 "feedback": FEEDBACK.get(r["feedback"] or "")} for r in reversed(rows)]

    def set_feedback(self, outbox_id: str, value: str) -> str:
        if value not in FEEDBACK:
            return "反馈只能是 useful（有用）或 unnecessary（没必要）。"
        cur = self.db.execute(
            "UPDATE outbox SET feedback = ? WHERE id = ? AND kind IN ('now', 'digest')", (value, outbox_id)
        )
        if not cur.rowcount:
            return f"找不到通知 {outbox_id}。"
        return f"已记下：{FEEDBACK[value]}。夜间整理时 Agent 会看到。"

    # ---------------------------------------------------------------- 分发器

    def _due(self, at: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM outbox WHERE status = 'PENDING' AND (deliver_after IS NULL OR deliver_after <= ?) "
            "ORDER BY created_at, rowid", (_iso(at),)
        ).fetchall()

    def _seconds_to_next(self, at: datetime) -> float:
        row = self.db.execute(
            "SELECT MIN(deliver_after) AS t FROM outbox WHERE status = 'PENDING' AND deliver_after > ?", (_iso(at),)
        ).fetchone()
        wait = float(self.cfg.dispatch_poll_s)
        if row["t"]:
            wait = min(wait, max(0.05, (datetime.fromisoformat(row["t"]) - at).total_seconds()))
        return wait

    def _mark_sent(self, ids: list[str]) -> None:
        marks = ",".join("?" * len(ids))
        self.db.execute(f"UPDATE outbox SET status = 'SENT', sent_at = ? WHERE id IN ({marks})", [now(), *ids])

    def _mark_failed_attempt(self, rows: list[sqlite3.Row], at: datetime) -> None:
        for row in rows:
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                log.error("消息 %s 发送 %d 次都失败，放弃", row["id"], attempts)
                self.db.execute("UPDATE outbox SET status = 'FAILED', attempts = ? WHERE id = ?",
                                (attempts, row["id"]))
            else:
                retry = at + timedelta(seconds=min(5 * 2 ** (attempts - 1), 300))
                self.db.execute("UPDATE outbox SET attempts = ?, deliver_after = ? WHERE id = ?",
                                (attempts, _iso(retry), row["id"]))

    async def _deliver(self, channel, row: sqlite3.Row) -> None:
        body = sanitize(row["body"])
        if row["kind"] == "approval" and hasattr(channel, "send_approval"):
            meta = json.loads(row["meta"] or "{}")
            await channel.send_approval(row["body"], meta["approval_id"], meta["scopes"])
        elif row["kind"] == "review" and hasattr(channel, "send_review"):
            meta = json.loads(row["meta"] or "{}")
            await channel.send_review(row["body"], meta["kind"], meta["id"])   # 带"确认 / 拒绝"按钮
        elif row["attachment"]:
            await channel.send_file(row["attachment"], body)
        elif row["kind"] == "now" and hasattr(channel, "send_notice"):
            await channel.send_notice(body, row["id"])      # 带"有用 / 没必要"两个按钮
        else:
            await channel.send(body)

    async def _deliver_digest(self, channel, rows: list[sqlite3.Row]) -> None:
        """到点的摘要项合成一条发出。"""
        if len(rows) == 1:
            text = f"摘要：\n{sanitize(rows[0]['body'])}"
        else:
            items = "\n\n".join(f"{i}. {sanitize(r['body'])}" for i, r in enumerate(rows, 1))
            text = f"摘要（{len(rows)} 条）：\n\n{items}"
        if hasattr(channel, "send_notice"):
            await channel.send_notice(text, rows[0]["id"])
        else:
            await channel.send(text)

    async def dispatch(self, channel) -> None:
        """分发器：按送达时间把消息送出去。发送失败的稍后重试，多次失败标记 FAILED。"""
        while True:
            at = self.clock()
            rows = self._due(at)
            if not rows:
                try:
                    await asyncio.wait_for(self._wake.wait(), self._seconds_to_next(at))
                except TimeoutError:
                    pass
                self._wake.clear()
                continue
            digests = [r for r in rows if r["kind"] == "digest" and not r["attachment"]]
            digest_ids = {r["id"] for r in digests}
            for row in [r for r in rows if r["id"] not in digest_ids]:
                try:
                    await self._deliver(channel, row)
                    self._mark_sent([row["id"]])
                except Exception:
                    log.exception("发送失败，稍后重试")
                    self._mark_failed_attempt([row], at)
            if digests:
                try:
                    await self._deliver_digest(channel, digests)
                    self._mark_sent([r["id"] for r in digests])
                except Exception:
                    log.exception("摘要发送失败，稍后重试")
                    self._mark_failed_attempt(digests, at)
