"""定时器库与调度器。设计文档第 7 节。

定时器只放"以后才该处理"的事。调度器每分钟看一次哪些到期了，把它们变成收件箱事件，
自己从不启动运行：这样定时器和别的事件一起排队、去重、合并，不会和正在进行的运行撞车。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from . import review
from .db import new_id, now

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("timers")

CONSOLIDATE = "consolidate"


class TimerError(ValueError):
    """设定定时器的请求不合规矩。消息原样告诉 Agent 或你。"""


# ---------------------------------------------------------------- cron

_FIELDS = (("分钟", 0, 59), ("小时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("星期", 0, 7))


def _parse_field(text: str, label: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if base == "*":
                a, b = lo, hi
            elif "-" in base:
                a, b = (int(x) for x in base.split("-", 1))
            else:
                a = int(base)
                b = hi if step_text else a      # "5/10" 表示从 5 开始每 10 个
        except ValueError:
            raise TimerError(f"cron 的{label}字段看不懂：{text}") from None
        if step < 1 or a < lo or b > hi or a > b:
            raise TimerError(f"cron 的{label}字段超出范围（{lo}-{hi}）：{text}")
        values.update(range(a, b + 1, step))
    return values


class Cron:
    """标准五段 cron（分 时 日 月 星期），按宿主的本地时间解释。星期 0 和 7 都是周日。"""

    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise TimerError("cron 要有五段：分 时 日 月 星期，例如 \"0 8 * * *\" 是每天 8 点")
        self.expr = " ".join(parts)
        sets = [_parse_field(p, *f) for p, f in zip(parts, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, dows = (sorted(s) for s in sets)
        self.dows = {d % 7 for d in dows}
        self._dom_any, self._dow_any = parts[2] == "*", parts[4] == "*"

    def _day_ok(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.days, d.isoweekday() % 7 in self.dows
        # cron 的老规矩：日和星期都限定时，满足其一即可
        return dom or dow if not (self._dom_any or self._dow_any) else dom and dow

    def next_after(self, after: datetime) -> datetime:
        """严格晚于 after 的下一次，返回 UTC 时间。"""
        start = after.astimezone().replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        d = start.date()
        for _ in range(366 * 5):
            if self._day_ok(d):
                for h in self.hours:
                    for m in self.minutes:
                        candidate = datetime.combine(d, time(h, m))
                        if candidate >= start:
                            return candidate.astimezone(UTC)   # 不带时区的时间按本地时间解释
            d += timedelta(days=1)
        raise TimerError(f"这个 cron 五年内都不会触发：{self.expr}")

    def min_gap(self, start: datetime, samples: int = 200) -> timedelta:
        """接下来若干次之间的最短间隔，用来限制日程的频率。"""
        t = self.next_after(start)
        gap = timedelta(days=3650)
        for _ in range(samples):
            nxt = self.next_after(t)
            gap = min(gap, nxt - t)
            t = nxt
        return gap


# ---------------------------------------------------------------- 时间

def parse_local(text: str) -> datetime:
    """"2026-11-19 09:00" 这样的本地时间，返回 UTC。带时区的 ISO 时间也接受。"""
    try:
        value = datetime.fromisoformat(text.strip().replace("T", " "))
    except ValueError:
        raise TimerError(f"时间格式看不懂：{text}。请用 \"YYYY-MM-DD HH:MM\"（本地时间）") from None
    return value.astimezone(UTC)      # 不带时区的按本地时间


def local_str(iso: str | datetime) -> str:
    value = datetime.fromisoformat(iso) if isinstance(iso, str) else iso
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


# ---------------------------------------------------------------- 定时器库

class Timers:
    def __init__(self, db: sqlite3.Connection, cfg):
        self.db = db
        self.cfg = cfg

    def get(self, timer_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM timer WHERE id = ?", (timer_id,)).fetchone()

    def _get(self, timer_id: str) -> sqlite3.Row:
        """刚写进去的定时器，一定在。"""
        row = self.get(timer_id)
        assert row is not None
        return row

    def active(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM timer WHERE status = 'ACTIVE' ORDER BY due_at").fetchall()

    def create(self, *, kind: str, name: str, payload: dict, created_by: str, at: datetime,
               due_at: datetime | None = None, cron: str | None = None,
               session_id: str | None = None, precheck: str | None = None) -> sqlite3.Row:
        """按第 7.4 节的上限检查后写入。at 是"现在"，由调用方给，方便测试。"""
        name = (name or "").strip()[:200]
        if not name:
            raise TimerError("定时器要有一句话的名字")
        if created_by != "system":
            count = self.db.execute(
                "SELECT COUNT(*) AS n FROM timer WHERE status = 'ACTIVE' AND created_by != 'system'"
            ).fetchone()["n"]
            if count >= self.cfg.timer_max:
                raise TimerError(f"定时器已经有 {count} 个，到上限了。先取消不需要的")
        if kind == "follow_up":
            if due_at is None or cron:
                raise TimerError("跟进要给一个时间（at 或 in_minutes），不能用 cron")
            if due_at < at + timedelta(seconds=self.cfg.follow_up_min_s):
                raise TimerError(f"跟进最早只能设在 {self.cfg.follow_up_min_s} 秒之后")
            if due_at > at + timedelta(days=366):
                raise TimerError("跟进不能设在一年以后")
        elif kind == "schedule":
            if not cron or due_at is not None:
                raise TimerError("日程要给 cron 表达式")
            parsed = Cron(cron)
            gap = parsed.min_gap(at)
            if gap < timedelta(seconds=self.cfg.schedule_min_interval_s):
                raise TimerError(f"日程最多每 {self.cfg.schedule_min_interval_s // 60} 分钟一次，"
                                 f"这个 cron 最短间隔是 {int(gap.total_seconds() // 60)} 分钟")
            cron, due_at, session_id = parsed.expr, parsed.next_after(at), None
        else:
            raise TimerError(f"不认识的定时器种类：{kind}（follow_up 或 schedule）")
        timer_id = new_id("tm")
        self.db.execute(
            "INSERT INTO timer (id, kind, name, due_at, cron, session_id, payload, precheck, status, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (timer_id, kind, name, iso(due_at), cron, session_id,
             json.dumps(payload, ensure_ascii=False), precheck, created_by, now()),
        )
        return self._get(timer_id)

    def cancel(self, timer_id: str, by: str) -> sqlite3.Row:
        row = self.get(timer_id)
        if row is None or row["status"] != "ACTIVE":
            raise TimerError(f"没有进行中的定时器 {timer_id}")
        # Agent 只能取消它自己设的；你设的和系统内置的（夜间整理）只有你能取消
        if by == "agent" and row["created_by"] != "agent":
            raise TimerError("这个定时器不是你设的，只有用户能取消")
        self.db.execute("UPDATE timer SET status = 'CANCELLED' WHERE id = ?", (timer_id,))
        return row

    def resume(self, timer_id: str, at: datetime) -> sqlite3.Row:
        """把取消了的日程重新打开，从现在往后算下一次。只有你能做（跟进过了时间就没意义了）。"""
        row = self.get(timer_id)
        if row is None or row["status"] != "CANCELLED" or row["kind"] != "schedule":
            raise TimerError(f"没有已取消的日程 {timer_id}。/timers 里能看到最近取消的")
        if row["created_by"] != "system":
            count = self.db.execute(
                "SELECT COUNT(*) AS n FROM timer WHERE status = 'ACTIVE' AND created_by != 'system'"
            ).fetchone()["n"]
            if count >= self.cfg.timer_max:
                raise TimerError(f"定时器已经有 {count} 个，到上限了。先取消不需要的")
        self.db.execute("UPDATE timer SET status = 'ACTIVE', due_at = ? WHERE id = ?",
                        (iso(Cron(row["cron"]).next_after(at)), timer_id))
        return self._get(timer_id)

    def cancelled_schedules(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM timer WHERE status = 'CANCELLED' AND kind = 'schedule' "
            "ORDER BY COALESCE(last_fired_at, created_at) DESC LIMIT ?", (limit,)
        ).fetchall()

    def history(self, timer_id: str, limit: int = 3) -> list[str]:
        """最近几次到期各自怎样了：收件箱里的到期事件，加上它触发的前置检查和运行。"""
        rows = self.db.execute(
            "SELECT id, source_id, status, created_at FROM inbox WHERE source = 'timer' AND source_id LIKE ? "
            "ORDER BY created_at DESC LIMIT ?", (f"{timer_id}:%", limit)
        ).fetchall()
        out = []
        for ev in rows:
            runs = self.db.execute("SELECT kind, outcome FROM run WHERE trigger_key = ? ORDER BY started_at",
                                   (ev["id"],)).fetchall()
            if any(r["kind"] == "precheck" and r["outcome"] == "UNCHANGED" for r in runs):
                result = "前置检查说没变化，没叫醒"
            elif any(r["kind"] == "agent" for r in runs):
                last = [r for r in runs if r["kind"] == "agent"][-1]["outcome"]
                result = {"SENT": "发了消息", "SILENT": "做完，没打扰你", "YIELDED": "让路，稍后接着做",
                          "FAILED": "失败", "TIMEOUT": "超时", None: "进行中"}.get(last, last)
            elif ev["status"] in ("PENDING", "CLAIMED"):
                result = "排队中"
            elif ev["status"] == "FAILED":
                result = "失败"
            else:
                result = "和别的事一起处理了"
            manual = "（手动）" if ":manual:" in ev["source_id"] else ""
            out.append(f"{local_str(ev['created_at'])}{manual} {result}")
        return out

    def due(self, at: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM timer WHERE status = 'ACTIVE' AND due_at <= ? ORDER BY due_at", (iso(at),)
        ).fetchall()

    def advance(self, row: sqlite3.Row, at: datetime) -> None:
        """跟进标记完成；日程推进到"现在"之后的下一次。

        所以停机期间错过的日程，不管错过几次，都只补一次。
        """
        if row["kind"] == "schedule":
            nxt = Cron(row["cron"]).next_after(max(at, datetime.fromisoformat(row["due_at"])))
            self.db.execute("UPDATE timer SET due_at = ?, last_fired_at = ? WHERE id = ?",
                            (iso(nxt), now(), row["id"]))
        else:
            self.db.execute("UPDATE timer SET status = 'DONE', last_fired_at = ? WHERE id = ?",
                            (now(), row["id"]))

    def describe(self, row: sqlite3.Row) -> str:
        when = f"下次 {local_str(row['due_at'])}"
        rule = f"，cron {row['cron']}" if row["cron"] else ""
        check = f"，前置检查 {row['precheck']}" if row["precheck"] else ""
        kind = {"follow_up": "跟进", "schedule": "日程"}[row["kind"]]
        same = "，固定会话" if json.loads(row["payload"]).get("same_session") else ""
        return f"{row['id']} [{kind}，{row['created_by']}] {row['name']}（{when}{rule}{check}{same}）"


# ---------------------------------------------------------------- 事件文字

def event_text(row: sqlite3.Row, payload: dict) -> str:
    kind = {"follow_up": "跟进", "schedule": "日程"}[row["kind"]]
    lines = [f"[定时器到期：{kind} {row['id']}] {row['name']}"]
    if payload.get("task"):
        lines.append(f"要做的事：{payload['task']}")
    if payload.get("criteria"):
        lines.append(f"判断依据：{payload['criteria']}")
    if payload.get("progress_file"):
        lines.append(f"进度文件：{payload['progress_file']}")
    if payload.get("skill"):
        lines.append(f"按技能处理：先读 skills/{payload['skill']}/SKILL.md")
    return "\n".join(lines)


CONSOLIDATE_TASK = """夜间整理。下面是过去 24 小时的活动概要，需要细节时用 recall 搜索或读主目录里的文件。请做这几件事：
1. 合并重复的笔记，替换过时的条目，让 memory/index.md 和 notes/ 里的文件一致。
2. 找出只有跨多次运行才看得出的规律，例如用户反复改同一类内容、反复把某类通知标成"没必要"。记进 notes。
   memory/instructions.md 是用户亲口给的指令，不要自己往里加；你的推断只能记在 notes 里。
3. 删掉已经完成的任务的进度文件和对应的索引条目。
4. 反复出现的做法，可以提议存成技能。
5. 有 roles/<名字>/memory/ 的话，那是其他角色各自的记忆：按同样的规矩各自整理，不要把一个角色的笔记挪给另一个。
最后，有值得用户知道的整理结果时用 notify（urgency=digest）发一段简短摘要；没有就保持安静。"""


# ---------------------------------------------------------------- 调度器

class Scheduler:
    """每分钟执行一次 tick。没有到期的定时器时，它就是一次数据库查询，不调用模型。"""

    def __init__(self, app: App):
        self.app = app
        self.clock = lambda: datetime.now(UTC)

    def ensure_builtin(self) -> None:
        """内置的夜间整理日程。你用 /cancel 取消后不会再自动建回来，除非清空数据。"""
        cron = self.app.cfg.consolidate_cron
        if not cron:
            return
        exists = self.app.db.execute(
            "SELECT 1 FROM timer WHERE created_by = 'system' AND json_extract(payload, '$.purpose') = ?",
            (CONSOLIDATE,),
        ).fetchone()
        if not exists:
            self.app.timers.create(kind="schedule", name="夜间整理记忆", cron=cron, created_by="system",
                                   payload={"purpose": CONSOLIDATE}, precheck="builtin:activity",
                                   at=self.clock())

    async def loop(self) -> None:
        while True:
            try:
                self.tick(self.clock())
            except Exception:
                log.exception("调度器执行出错")
            await asyncio.sleep(self.app.cfg.scheduler_interval_s)

    def _event(self, row: sqlite3.Row, at: datetime, manual: bool = False) -> dict | None:
        """一次到期要放进收件箱的事件。返回 None 表示这次不用叫醒 Agent。

        manual 是你用 /run 叫的：不做前置检查（你就是要它现在做）。
        """
        app = self.app
        payload = json.loads(row["payload"])
        event = {"at": iso(at) if manual else row["due_at"], "timer_id": row["id"]}
        if payload.get("purpose") == CONSOLIDATE:
            since = row["last_fired_at"] or iso(at - timedelta(days=1))
            # 内置的前置检查在宿主上做：一天里什么都没发生，就不叫醒 Agent
            if not manual and row["precheck"] == "builtin:activity" and not review.has_activity(app.db, since):
                log.info("过去一天没有活动，跳过夜间整理")
                return None
            event["session_kind"] = CONSOLIDATE
            event["snapshot_first"] = True
            event["text"] = (f"[定时器到期：日程 {row['id']}] {row['name']}\n{CONSOLIDATE_TASK}\n\n"
                             + review.daily_review(app.db, since))
            return event
        event["text"] = event_text(row, payload)
        if manual:
            event["text"] = event["text"].replace("[定时器到期：", "[用户让定时器现在运行：", 1)
        elif row["precheck"]:
            event["precheck"] = row["precheck"]
        if row["kind"] == "schedule":
            event["session_kind"] = "timer"
            if payload.get("role"):
                event["role"] = payload["role"]     # 设这个日程的角色来处理
            if payload.get("same_session"):
                event["session_key"] = self._session_key(row["id"])
        return event

    def _session_key(self, timer_id: str) -> str:
        """固定会话的日程每次进同一个会话；接满 schedule_session_fires 次就关掉，下次开新的。"""
        key = f"timer:{timer_id}"
        db = self.app.db
        open_ = db.execute("SELECT id FROM session WHERE session_key = ? AND status = 'OPEN'", (key,)).fetchall()
        for s in open_:
            fired = db.execute("SELECT COUNT(*) AS n FROM inbox WHERE session_id = ? AND source = 'timer'",
                               (s["id"],)).fetchone()["n"]
            busy = db.execute("SELECT 1 FROM inbox WHERE session_id = ? AND status IN ('PENDING', 'CLAIMED')",
                              (s["id"],)).fetchone()
            if fired >= self.app.cfg.schedule_session_fires and not busy:
                db.execute("UPDATE session SET status = 'CLOSED' WHERE id = ?", (s["id"],))
        return key

    def run_now(self, timer_id: str) -> str:
        """你发 /run：现在就按这个日程做一次，不改它的下一次时间。"""
        row = self.app.timers.get(timer_id)
        if row is None or row["status"] != "ACTIVE":
            return f"没有进行中的定时器 {timer_id}"
        if row["kind"] != "schedule":
            return "跟进只会触发一次，到时间会自己来；现在就想处理的话直接跟我说"
        at = self.clock()
        event = self._event(row, at, manual=True)
        assert event is not None        # 手动叫的不做前置检查，一定有事件
        self.app.inbox.add("timer", f"{row['id']}:manual:{iso(at)}", event)
        return f"已排队：{row['name']}。下一次照旧 {local_str(row['due_at'])}"

    def tick(self, at: datetime) -> int:
        """把到期的定时器变成收件箱事件。返回放进收件箱的事件数。"""
        app = self.app
        added = 0
        for row in app.timers.due(at):
            event = self._event(row, at)
            if event is not None:
                session = row["session_id"] if row["kind"] == "follow_up" else None
                # 去重键 = 定时器号 + 这一次的到期时间：调度器崩在写入之后、推进之前，重启后不会重复
                if app.inbox.add("timer", f"{row['id']}:{row['due_at']}", event, session_id=session):
                    added += 1
            app.timers.advance(row, at)
        return added
