"""活动记录：/activity 的摘要和只读页面。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiohttp

from host import activity


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _seed(db):
    db.execute("INSERT INTO inbox (id, source, source_id, session_id, priority, payload, status, created_at) "
               "VALUES ('in_1', 'user', 'm1', 'ses_1', 0, ?, 'DONE', ?)",
               (json.dumps({"text": "帮我查 <script>alert(1)</script> 的价格"}), _iso(30)))
    db.execute("INSERT INTO run (id, session_id, trigger_key, role, outcome, tokens_used, started_at, finished_at) "
               "VALUES ('run_1', 'ses_1', 'in_1', 'default', 'SENT', 1234, ?, ?)", (_iso(30), _iso(29)))
    for verdict, url in (("allow", "https://shop.example/item?token=SECRET"), ("deny", "https://evil.example/x"),
                         ("ask", "https://mail.example/send")):
        db.execute("INSERT INTO egress_log (run_id, session_id, method, url, verdict, reason, created_at) "
                   "VALUES ('run_1', 'ses_1', 'GET', ?, ?, '理由', ?)", (url, verdict, _iso(29.5)))
    db.execute("INSERT INTO approval (id, session_id, run_id, request_hash, category, method, host, url, summary, "
               "scopes, status, scope, created_at) VALUES ('ap_1', 'ses_1', 'run_1', 'h', 'web_write', 'POST', "
               "'mail.example', 'https://mail.example/send', '发一封邮件给 a@b', '[]', 'GRANTED', 'once', ?)",
               (_iso(29.5),))
    db.execute("INSERT INTO outbox (id, kind, session_id, body, dedupe_key, status, created_at) "
               "VALUES ('ob_1', 'reply', 'ses_1', '查到了：<b>99 元</b>', 'in_1:0', 'SENT', ?)", (_iso(29.2),))
    # 别的运行的消息不该算进来
    db.execute("INSERT INTO outbox (id, kind, session_id, body, dedupe_key, status, created_at) "
               "VALUES ('ob_2', 'reply', 'ses_2', '别的', 'in_9:0', 'SENT', ?)", (_iso(29.2),))
    db.execute("INSERT INTO run (id, session_id, trigger_key, kind, outcome, started_at, finished_at) "
               "VALUES ('run_old', 's', 'k', 'agent', 'FAILED', ?, ?)", (_iso(60 * 50), _iso(60 * 50)))
    db.execute("INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
               "VALUES ('compact:ses_1', 200, '', '', 300, ?)", (_iso(31),))


def test_collect_and_summary(make_app):
    app = make_app()
    _seed(app.db)
    data = activity.collect(app.db, 1)
    assert [r["id"] for r in data["runs"]] == ["run_1"]          # 两天前的不在今天里
    run = data["runs"][0]
    assert run["trigger"] == "你的消息" and "<script>" in run["trigger_text"] and run["seconds"] in (59, 60)
    assert [e["verdict"] for e in run["egress"]] == ["allow", "deny", "ask"]
    assert run["egress"][0]["url"] == "shop.example/item?…"       # 查询串不显示
    assert run["approvals"][0]["status_label"] == "批准"
    assert [m["text"] for m in run["messages"]] == ["查到了：<b>99 元</b>"]
    t = data["totals"]
    assert (t["runs"], t["allow"], t["deny"], t["ask"], t["approvals"], t["messages"]) == (1, 1, 1, 1, 1, 1)
    assert t["tokens"] == 1234 + 300 and len(data["compactions"]) == 1

    text = activity.text_summary(data, "http://x/activity/k")
    assert "今天：运行 1 次，花费 1,534 token" in text
    assert "放行 1，拒绝 1，退回 0，问你 1" in text and "压缩长对话 1 次" in text
    assert "你的消息「帮我查" in text and "回复了，3 个请求" in text
    assert text.endswith("完整记录（1 小时内有效）：http://x/activity/k")

    assert len(activity.collect(app.db, 3)["runs"]) == 2
    assert activity.parse_days("") == 1 and activity.parse_days("7") == 7
    assert activity.parse_days("100") == 30 and activity.parse_days("x") is None and activity.parse_days("-1") is None


def test_page_is_escaped_and_has_no_script(make_app):
    app = make_app()
    _seed(app.db)
    page = activity.html_page(activity.collect(app.db, 1), "NONCE")
    assert "<script" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;b&gt;99 元&lt;/b&gt;" in page and "SECRET" not in page
    assert '<style nonce="NONCE">' in page


async def test_activity_command_and_page(make_app):
    app = make_app(browser="off")       # 没开浏览器也有活动记录页面
    await app.start(with_channel=False)
    _seed(app.db)
    await app.on_user_text("m1", "/activity")
    reply = app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:m1'").fetchone()["body"]
    assert reply.startswith("今天：运行 1 次")
    link = reply.rsplit("：", 1)[1]
    assert link.startswith(f"http://127.0.0.1:{app.takeover_port}/activity/")

    async with aiohttp.ClientSession() as s:
        async with s.get(link) as r:
            body = await r.text()
            assert r.status == 200 and "活动记录" in body and "run_1" in body
            csp = r.headers["content-security-policy"]
            assert "script-src" not in csp and "default-src 'none'" in csp
            assert r.headers["cache-control"] == "no-store"
        async with s.get(link[:-3] + "zzz") as r:
            assert r.status == 404
        # 过期
        key = link.rsplit("/", 1)[1]
        app.activity_pages[key] = (0, 1)
        async with s.get(link) as r:
            assert r.status == 404
        # 接管的地址在没开浏览器时一律 404
        async with s.get(f"http://127.0.0.1:{app.takeover_port}/takeover/abc") as r:
            assert r.status == 404

    await app.on_user_text("m2", "/activity 七天")
    assert "用法" in app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:m2'").fetchone()["body"]


async def test_no_link_when_the_page_port_is_off(make_app):
    app = make_app(browser="off", takeover_port=-1)
    await app.start(with_channel=False)
    assert app.activity_link(1) is None
    assert "完整记录" not in app.activity_text(1)
