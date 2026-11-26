"""U2：/usage 按天、按角色、按会话的花费。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from host import usage


def _ago(days: float = 0, minutes: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days, minutes=minutes)).isoformat()


def _seed(db):
    for sid, kind, role in (("ses_a", "conversation", None), ("ses_b", "timer", "mail")):
        db.execute("INSERT INTO session (id, kind, status, role, created_at, last_event_at) "
                   "VALUES (?, ?, 'OPEN', ?, ?, ?)", (sid, kind, role, _ago(3), _ago(3)))
    db.execute("INSERT INTO event (session_id, origin, type, payload, created_at) VALUES "
               "('ses_a', 'host', 'user_message', ?, ?)",
               (json.dumps({"role": "user", "content": "帮我  整理一下\n这周的发票和报销单据，按月份归档放好"}), _ago(3)))
    runs = [("run_1", "ses_a", None, "agent", _ago(minutes=5)),
            ("run_2", "ses_b", "mail", "agent", _ago(days=2)),
            ("run_3", "ses_b", None, "precheck", _ago(days=2)),
            ("run_old", "ses_a", None, "agent", _ago(days=20))]
    for rid, sid, role, kind, at in runs:
        db.execute("INSERT INTO run (id, session_id, trigger_key, kind, role, started_at) VALUES (?, ?, 'k', ?, ?, ?)",
                   (rid, sid, kind, role, at))
    logs = [("run_1", 200, 1000, _ago(minutes=5)), ("run_1", 200, 500, _ago(minutes=4)),
            ("run_1", 500, 0, _ago(minutes=3)),
            ("run_2", 200, 3000, _ago(days=2)), ("run_3", 200, 100, _ago(days=2)),
            ("compact:ses_a", 200, 700, _ago(minutes=6)),
            ("run_old", 200, 99999, _ago(days=20))]
    for rid, status, tokens, at in logs:
        db.execute("INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
                   "VALUES (?, ?, '', '', ?, ?)", (rid, status, tokens, at))


def test_collect_groups_by_day_role_session(make_app):
    app = make_app()
    _seed(app.db)
    data = usage.collect(app.db, 7)
    assert len(data["days"]) == 7
    assert data["total"] == 1000 + 500 + 3000 + 100 + 700 and data["calls"] == 6 and data["failed"] == 1
    assert data["days"][-1]["tokens"] in (2200, 1500 + 700)     # 今天：run_1 和压缩
    assert sum(d["tokens"] for d in data["days"]) == data["total"]
    roles = dict(data["roles"])
    assert roles == {"": 2200, "mail": 3000, "前置检查": 100}
    top = data["sessions"]
    assert [s["id"] for s in top] == ["ses_b", "ses_a"]
    assert top[0]["runs"] == 2 and top[1]["runs"] == 1             # 压缩不算一次运行
    assert top[1]["topic"] == "帮我 整理一下 这周的发票和报销单据，按月份归档…"
    assert data["compaction"] == {"calls": 1, "tokens": 700}

    text = usage.text_summary(data, budget=100_000)
    assert text.startswith("最近 7 天花费 5,300 token，调用模型 6 次（失败 1）")
    assert "今天 2,200 / 每日上限 100,000（2%）" in text
    assert "按角色：" in text and "· 默认角色  2,200" in text and "· mail  3,000" in text
    assert "定时器·mail" in text and "「帮我 整理一下" in text
    assert text.rstrip().endswith("其中压缩长对话 1 次，700 token")

    today = usage.collect(app.db, 1)
    assert len(today["days"]) == 1 and today["total"] == 2200
    assert "按天" not in usage.text_summary(today)
    assert usage.collect(app.db, 30)["total"] == 5300 + 99999


def test_parse_days_and_empty():
    assert usage.parse_days("") == 7 and usage.parse_days("3") == 3 and usage.parse_days("99") == 30
    assert usage.parse_days("1.5") is None and usage.parse_days("0") is None and usage.parse_days("x") is None


async def test_usage_command(make_app):
    app = make_app(browser="off")
    await app.start(with_channel=False)
    await app.on_user_text("u0", "/usage")
    assert app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:u0'").fetchone()["body"] == "最近 7 天没有花费。"
    _seed(app.db)
    await app.on_user_text("u1", "/usage 30")
    assert "最近 30 天花费" in app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:u1'").fetchone()["body"]
    await app.on_user_text("u2", "/usage 一周")
    assert "用法" in app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:u2'").fetchone()["body"]
