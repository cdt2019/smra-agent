"""给夜间整理用的活动概要。由宿主根据自己的记录生成，不依赖 Agent 的自述。"""
from __future__ import annotations

import json
import sqlite3

MAX_CHARS = 6000


def clip(text, n: int) -> str:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "…"
_KINDS = {"conversation": "对话", "timer": "日程", "webhook": "Webhook", "approval": "审批", "consolidate": "整理"}
_FEEDBACK = {"useful": "有用", "unnecessary": "没必要"}


def has_activity(db: sqlite3.Connection, since: str) -> bool:
    """since 之后除了整理本身，还有没有会话有过动静。"""
    row = db.execute(
        "SELECT 1 FROM session WHERE kind != 'consolidate' AND last_event_at > ? LIMIT 1", (since,)
    ).fetchone()
    return row is not None


def daily_review(db: sqlite3.Connection, since: str) -> str:
    lines = ["## 活动概要（宿主记录）"]
    sessions = db.execute(
        "SELECT id, kind, session_key FROM session WHERE kind != 'consolidate' AND last_event_at > ? "
        "ORDER BY created_at", (since,)
    ).fetchall()
    for s in sessions:
        first = db.execute(
            "SELECT payload FROM event WHERE session_id = ? AND type = 'user_message' AND created_at > ? "
            "ORDER BY seq LIMIT 1", (s["id"], since)
        ).fetchone()
        opening = clip(json.loads(first["payload"])["content"], 120) if first else "（无新事件）"
        outcomes = db.execute(
            "SELECT outcome, COUNT(*) AS n FROM run WHERE session_id = ? AND started_at > ? AND kind = 'agent' "
            "GROUP BY outcome", (s["id"], since)
        ).fetchall()
        runs = "，".join(f"{r['outcome'] or '进行中'} {r['n']}" for r in outcomes) or "无运行"
        key = f" {s['session_key']}" if s["session_key"] else ""
        lines.append(f"- 会话 {s['id']}（{_KINDS.get(s['kind'], s['kind'])}{key}）：{opening}　运行：{runs}")
    if len(sessions) == 0:
        lines.append("- （没有会话活动）")

    sent = db.execute(
        "SELECT kind, requested, body, feedback FROM outbox WHERE status = 'SENT' AND sent_at > ? "
        "AND kind IN ('now', 'digest') ORDER BY sent_at", (since,)
    ).fetchall()
    lines.append("\n## 发给用户的通知与用户的反馈")
    for r in sent:
        tag = _FEEDBACK.get(r["feedback"] or "", "未反馈")
        demoted = "（要求即时，超出上限降为摘要）" if r["requested"] == "now" and r["kind"] == "digest" else ""
        lines.append(f"- [{r['kind']}{demoted}，用户：{tag}] {clip(r['body'], 150)}")
    if not sent:
        lines.append("- （没有）")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n……（概要太长，后面省略了）"
