"""花费统计：/usage [天数]。

按本地日期、按角色、按会话汇总模型花费。数据来自 inference_log（宿主转发每一次模型调用、
每一次搜索时自己记的账），和每日上限用的是同一个数。压缩长对话的花费单独列出。
裁决模型（守门的小模型）的调用不经过这张表，不在里面。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta

DEFAULT_DAYS = 7
MAX_DAYS = 30
TOP_SESSIONS = 5
WEEKDAYS = "一二三四五六日"
KIND_LABEL = {"conversation": "对话", "timer": "定时器", "webhook": "Webhook", "approval": "审批"}


def _topic(db: sqlite3.Connection, session_id: str, limit: int = 24) -> str:
    row = db.execute("SELECT payload FROM event WHERE session_id = ? AND type = 'user_message' "
                     "ORDER BY seq LIMIT 1", (session_id,)).fetchone()
    if not row:
        return ""
    try:
        content = json.loads(row[0]).get("content", "")
    except ValueError:
        return ""
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    text = " ".join(str(content).split())
    return text if len(text) <= limit else text[:limit] + "…"


def collect(db: sqlite3.Connection, days: float, at: datetime | None = None) -> dict:
    at = at or datetime.now(UTC)
    local_now = at.astimezone()
    # 从本地日期的零点算起：/usage 1 就是"今天"
    start_local = (local_now - timedelta(days=max(1, int(days)) - 1)).replace(hour=0, minute=0, second=0,
                                                                                 microsecond=0)
    since = start_local.astimezone(UTC).isoformat()
    rows = db.execute(
        "SELECT l.run_id, l.status, l.tokens, l.created_at, r.session_id, r.role, r.kind, s.kind AS session_kind, "
        "s.role AS session_role FROM inference_log l LEFT JOIN run r ON r.id = l.run_id "
        "LEFT JOIN session s ON s.id = COALESCE(r.session_id, CASE WHEN l.run_id LIKE 'compact:%' "
        "THEN substr(l.run_id, 9) END) WHERE l.created_at >= ? ORDER BY l.created_at", (since,)).fetchall()

    by_day: dict[str, int] = defaultdict(int)
    by_role: dict[str, int] = defaultdict(int)
    by_session: dict[str, dict] = {}
    compaction = {"calls": 0, "tokens": 0}
    total = calls = failed = 0
    for r in rows:
        tokens = int(r["tokens"] or 0)
        total += tokens
        calls += 1
        failed += r["status"] != 200
        by_day[datetime.fromisoformat(r["created_at"]).astimezone().date().isoformat()] += tokens
        is_compact = (r["run_id"] or "").startswith("compact:")
        if is_compact:
            compaction["calls"] += 1
            compaction["tokens"] += tokens
            session_id = r["run_id"][len("compact:"):]
        else:
            session_id = r["session_id"]
        role = r["role"] or r["session_role"] or ""
        if r["kind"] == "precheck":
            role = "前置检查"
        by_role[role] += tokens
        if session_id:
            s = by_session.setdefault(session_id, {"id": session_id, "tokens": 0, "runs": set(),
                                                   "kind": r["session_kind"] or "", "role": role})
            s["tokens"] += tokens
            if not is_compact:
                s["runs"].add(r["run_id"])

    days_list = []
    day = start_local.date()
    while day <= local_now.date():
        days_list.append({"date": day.isoformat(), "label": f"{day:%m-%d} 周{WEEKDAYS[day.weekday()]}",
                          "tokens": by_day.get(day.isoformat(), 0)})
        day += timedelta(days=1)
    top = sorted(by_session.values(), key=lambda s: s["tokens"], reverse=True)[:TOP_SESSIONS]
    for s in top:
        s["runs"] = len(s["runs"])
        s["topic"] = _topic(db, s["id"])
    return {"days": days_list, "total": total, "calls": calls, "failed": failed,
            "roles": sorted(by_role.items(), key=lambda kv: kv[1], reverse=True),
            "sessions": top, "session_count": len(by_session), "compaction": compaction}


def _bar(value: int, peak: int, width: int = 10) -> str:
    if not peak or not value:
        return ""
    return "▇" * max(1, round(value * width / peak))


def text_summary(data: dict, budget: int = 0) -> str:
    span = "今天" if len(data["days"]) == 1 else f"最近 {len(data['days'])} 天"
    if not data["calls"]:
        return f"{span}没有花费。"
    lines = [f"{span}花费 {data['total']:,} token，调用模型 {data['calls']} 次"
             + (f"（失败 {data['failed']}）" if data["failed"] else "")]
    if budget and data["days"]:
        today = data["days"][-1]["tokens"]
        lines.append(f"今天 {today:,} / 每日上限 {budget:,}（{today * 100 // budget}%）")
    if len(data["days"]) > 1:
        lines += ["", "按天："]
        peak = max(d["tokens"] for d in data["days"])
        lines += [f"· {d['label']}  {d['tokens']:>10,}  {_bar(d['tokens'], peak)}" for d in data["days"]]
    if len(data["roles"]) > 1 or (data["roles"] and data["roles"][0][0]):
        lines += ["", "按角色："]
        lines += [f"· {role or '默认角色'}  {tokens:,}" for role, tokens in data["roles"]]
    if data["sessions"]:
        lines += ["", f"花得最多的会话（共 {data['session_count']} 个）："]
        for s in data["sessions"]:
            what = KIND_LABEL.get(s["kind"], s["kind"]) + (f"·{s['role']}" if s["role"] else "")
            topic = f"「{s['topic']}」" if s["topic"] else ""
            lines.append(f"· {s['tokens']:,}  {what}{topic}，{s['runs']} 次运行  {s['id']}")
    c = data["compaction"]
    if c["calls"]:
        lines += ["", f"其中压缩长对话 {c['calls']} 次，{c['tokens']:,} token"]
    return "\n".join(lines)


def parse_days(arg: str) -> int | None:
    """'/usage' 后面的天数。默认 7，最多 30。"""
    arg = arg.strip()
    if not arg:
        return DEFAULT_DAYS
    try:
        days = int(arg)
    except ValueError:
        return None
    return min(MAX_DAYS, days) if days > 0 else None
