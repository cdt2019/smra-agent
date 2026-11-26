"""例行任务：技能的存在检查和 /skill，定时器的 /run、/resume、运行记录，固定会话的日程。

模型行为是编排的；时间靠直接调用调度器的 tick(某个时刻) 来快进。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tests.conftest import all_done, wait_until
from tests.test_step5 import agent_runs, last_user_text


def utc_now() -> datetime:
    return datetime.now(UTC)


def write_skill(tmp_path, name: str) -> None:
    folder = tmp_path / "home" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试技能\n---\n步骤……", encoding="utf-8")


def due(app, timer_id: str) -> datetime:
    return datetime.fromisoformat(app.timers.get(timer_id)["due_at"])


async def reply_to(app, text: str) -> str:
    before = len(app.channel.sent)
    await app.on_user_text(f"c{before}", text)
    await wait_until(lambda: len(app.channel.sent) > before)
    return app.channel.sent[-1]


async def test_set_timer_needs_an_existing_skill(make_app, upstream, tmp_path):
    app = make_app()
    upstream.script = [
        {"tools": [("set_timer", {"kind": "schedule", "name": "周报", "cron": "0 9 * * 1",
                                  "task": "写本周周报", "skill": "weekly-report"})]},
        {"tools": [("set_timer", {"kind": "schedule", "name": "周报", "cron": "0 9 * * 1",
                                  "task": "写本周周报", "skill": "../etc"})]},
        {"text": "技能还没有，我先写技能。"},
    ]
    await app.start(with_channel=False)
    await app.on_user_text("m1", "每周一帮我写周报")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    results = [json.dumps(r["body"]["messages"][-1]["content"], ensure_ascii=False) for r in upstream.requests[1:3]]
    assert "没有 skills/weekly-report/SKILL.md" in results[0]
    assert "目录名" in results[1]
    assert app.db.execute("SELECT COUNT(*) AS n FROM timer WHERE created_by = 'agent'").fetchone()["n"] == 0

    write_skill(tmp_path, "weekly-report")
    upstream.script = [
        {"tools": [("set_timer", {"kind": "schedule", "name": "周报", "cron": "0 9 * * 1", "task": "写本周周报",
                                  "skill": "weekly-report", "same_session": True})]},
        {"text": "设好了。"},
    ]
    await app.on_user_text("m2", "技能写好了，再设一次")
    await wait_until(lambda: all_done(app) and "设好了。" in app.channel.sent)
    row = app.db.execute("SELECT * FROM timer WHERE created_by = 'agent'").fetchone()
    assert json.loads(row["payload"]) == {"task": "写本周周报", "skill": "weekly-report", "same_session": True}
    assert any("/run " + row["id"] in s for s in app.channel.sent)       # 新日程的提醒里教你先试一次


async def test_skill_command_becomes_a_message(make_app, upstream):
    app = make_app()
    await app.start(with_channel=False)
    assert "用法" in await reply_to(app, "/skill ../x")
    upstream.script = [{"text": "好的，照周报技能写了。"}]
    await app.on_user_text("m1", "/skill weekly-report 只写技术部分")
    await wait_until(lambda: all_done(app) and "好的，照周报技能写了。" in app.channel.sent)
    text = last_user_text(upstream.requests[0]["body"])
    assert "[用户要求按技能处理] 先读 skills/weekly-report/SKILL.md" in text and "只写技术部分" in text
    assert agent_runs(app)[0]["session_id"] == app.db.execute(
        "SELECT id FROM session WHERE kind = 'conversation'").fetchone()["id"]     # 进当前对话


async def test_run_now_resume_and_history(make_app, upstream):
    app = make_app()
    await app.start(with_channel=False)
    at = utc_now()
    row = app.timers.create(kind="schedule", name="看价格", payload={"task": "看价格"}, created_by="agent",
                            at=at, cron="0 8 * * *", precheck="tools/check.py")
    follow = app.timers.create(kind="follow_up", name="跟进", payload={"task": "x"}, created_by="agent",
                               at=at, due_at=at + timedelta(hours=1), session_id="ses_x")
    next_due = due(app, row["id"])

    assert "跟进只会触发一次" in await reply_to(app, f"/run {follow['id']}")
    assert "没有进行中的定时器" in await reply_to(app, "/run tm_nope")

    # /run：现在就做一次，跳过前置检查，下次时间不变
    upstream.script = [{"text": "看过了。"}]
    reply = await reply_to(app, f"/run {row['id']}")
    assert reply.startswith("已排队：看价格")
    await wait_until(lambda: all_done(app) and len(agent_runs(app)) == 1)
    text = last_user_text(upstream.requests[0]["body"])
    assert f"[用户让定时器现在运行：日程 {row['id']}]" in text and "前置检查" not in text
    assert not app.db.execute("SELECT 1 FROM run WHERE kind = 'precheck'").fetchone()
    assert due(app, row["id"]) == next_due

    listing = await reply_to(app, "/timers")
    assert "看价格" in listing and "（手动） 做完，没打扰你" in listing

    # 取消后能在 /timers 里看到，用 /resume 重新打开，从现在往后算
    assert f"/resume {row['id']}" in await reply_to(app, f"/cancel {row['id']}")
    listing = await reply_to(app, "/timers")
    assert "最近取消的日程" in listing and row["id"] in listing.split("最近取消的日程")[1]
    reply = await reply_to(app, f"/resume {row['id']}")
    assert reply.startswith("已恢复：") and app.timers.get(row["id"])["status"] == "ACTIVE"
    assert "没有已取消的日程" in await reply_to(app, f"/resume {row['id']}")
    assert "没有已取消的日程" in await reply_to(app, f"/resume {follow['id']}")


async def test_resume_brings_back_nightly_consolidation(make_app):
    app = make_app(consolidate_cron="30 3 * * *")
    app.scheduler.ensure_builtin()
    row = app.db.execute("SELECT * FROM timer WHERE created_by = 'system'").fetchone()
    app.timers.cancel(row["id"], by="user")
    app.scheduler.ensure_builtin()                    # 取消了不会自己建回来
    assert app.db.execute("SELECT COUNT(*) AS n FROM timer").fetchone()["n"] == 1
    app.timers.resume(row["id"], utc_now())
    assert app.timers.get(row["id"])["status"] == "ACTIVE"


async def test_same_session_schedule_reuses_and_rotates(make_app, upstream):
    app = make_app(schedule_session_fires=2)
    await app.start(with_channel=False)
    at = utc_now()
    same = app.timers.create(kind="schedule", name="盯工单", created_by="agent", at=at, cron="0 * * * *",
                             payload={"task": "看工单有没有新回复", "same_session": True})
    fresh = app.timers.create(kind="schedule", name="每小时简报", created_by="agent", at=at, cron="0 * * * *",
                              payload={"task": "简报"})
    assert "固定会话" in app.timers.describe(same) and "固定会话" not in app.timers.describe(fresh)

    sessions_seen = []
    for i in range(3):
        upstream.script = [{"text": f"第 {i} 次看过了。"}, {"text": f"简报 {i}。"}]
        count = len(agent_runs(app))
        app.scheduler.tick(due(app, same["id"]) + timedelta(seconds=1))
        await wait_until(lambda: all_done(app) and len(agent_runs(app)) == count + 2)
        runs = agent_runs(app)[-2:]
        by_kind = {}
        for r in runs:
            key = app.db.execute("SELECT session_key FROM session WHERE id = ?", (r["session_id"],)).fetchone()[0]
            by_kind["same" if key else "fresh"] = r["session_id"]
        sessions_seen.append(by_kind)

    # 固定会话的前两次在同一个会话，第三次接满了换新的；普通日程每次都是新会话
    assert sessions_seen[0]["same"] == sessions_seen[1]["same"] != sessions_seen[2]["same"]
    assert len({s["fresh"] for s in sessions_seen}) == 3
    kinds = {r["kind"] for r in app.db.execute("SELECT kind FROM session").fetchall()}
    assert kinds == {"timer"}
    # 第二次醒来看得到第一次的事件和回答
    second_same = [r["body"] for r in upstream.requests if "盯工单" in last_user_text(r["body"])][1]
    assert sum("盯工单" in json.dumps(m, ensure_ascii=False) for m in second_same["messages"]) == 2
    assert any(m["role"] == "assistant" for m in second_same["messages"])
