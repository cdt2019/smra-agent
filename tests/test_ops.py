"""运维：/status、停止时等运行做完、启动时收拾上次没结束的运行、启动自检。"""
from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime, timedelta

from host import doctor, status
from host.db import connect, now


async def test_status_report(make_app):
    app = make_app(daily_token_budget=1000)
    await app.start(with_channel=False)
    db = app.db
    db.execute("INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
               "VALUES ('r', 200, '', '', 250, ?)", (now(),))
    db.execute("INSERT INTO run (id, session_id, trigger_key, started_at) VALUES ('run_x', 's', 'k', ?)",
               ((datetime.now(UTC) - timedelta(minutes=3)).isoformat(),))
    db.execute("INSERT INTO approval (id, session_id, run_id, request_hash, category, method, host, url, summary, "
               "scopes, status, created_at) VALUES ('ap', 's', 'r', 'h', 'web_write', 'POST', 'a.example', "
               "'https://a.example/', 'x', '[]', 'PENDING', ?)", (now(),))
    app.creds.add("tickets", {"token": "t"}, "bearer", title="工单", hosts=["api.example"])
    app.timers.create(kind="follow_up", name="回来看看", payload={"task": "x"}, created_by="agent",
                      at=datetime.now(UTC), due_at=datetime.now(UTC) + timedelta(hours=2))

    text = await app.status_text()
    assert "run_x（默认角色，已 3 分钟）" in text
    assert "1 个网络请求" in text
    assert "今日花费：250 / 1,000 token（25%）" in text
    assert "已连接：工单" in text
    assert "定时器：" in text and "回来看看" in text
    assert "电脑：" in text and "宿主已运行" in text

    await app.on_user_text("m1", "/status")
    assert app.db.execute("SELECT body FROM outbox WHERE dedupe_key = 'cmd:m1'").fetchone()["body"].startswith("运行中")


async def test_unfinished_runs_from_a_killed_host_are_closed(make_app):
    app = make_app()
    app.db.execute("INSERT INTO run (id, session_id, trigger_key, started_at) VALUES ('run_old', 's', 'k', ?)", (now(),))
    await app.start(with_channel=False)
    row = app.db.execute("SELECT outcome, finished_at FROM run WHERE id = 'run_old'").fetchone()
    assert row["outcome"] == "FAILED" and row["finished_at"]
    assert status.running(app.db) == []


async def test_stop_waits_for_the_current_work(make_app):
    app = make_app()
    done = []

    async def slow(name, arg):
        await asyncio.sleep(0.5)
        done.append(name)
        return "好了"

    app.run_command = slow
    await app.start(with_channel=False)
    await app.on_user_text("m1", "/snapshot")
    for _ in range(50):
        if app.worker.busy:
            break
        await asyncio.sleep(0.02)
    assert app.worker.busy
    await app.stop(grace_s=5)
    assert done == ["/snapshot"]
    assert connect(app.cfg.db_path).execute("SELECT status FROM inbox").fetchone()["status"] == "DONE"


async def test_stop_gives_up_after_the_grace_period(make_app):
    app = make_app()

    async def stuck(name, arg):
        await asyncio.sleep(30)

    app.run_command = stuck
    await app.start(with_channel=False)
    await app.on_user_text("m1", "/snapshot")
    for _ in range(50):
        if app.worker.busy:
            break
        await asyncio.sleep(0.02)
    t0 = asyncio.get_running_loop().time()
    await app.stop(grace_s=0.3)
    assert asyncio.get_running_loop().time() - t0 < 5
    # 没做完的事件留在收件箱里，下次启动时重新处理
    assert connect(app.cfg.db_path).execute("SELECT status FROM inbox").fetchone()["status"] != "DONE"


def test_doctor(make_app):
    app = make_app(api_key="", telegram_token="", browser="off", webhook_port=-1, takeover_port=-1)
    checks = {c.name: c for c in doctor.run_checks(app.cfg, "telegram")}
    assert checks["模型密钥"].level == doctor.FAIL
    assert checks["Telegram"].level == doctor.FAIL
    assert checks["电脑驱动"].level == doctor.WARN          # local 驱动
    assert checks["数据目录"].level == doctor.OK
    assert "Docker" not in checks

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        app.cfg.api_key, app.cfg.telegram_token, app.cfg.telegram_chat_id = "k", "t", "1"
        app.cfg.port = taken.getsockname()[1]
        checks = doctor.run_checks(app.cfg, "telegram")
        assert [c.name for c in doctor.failed(checks)] == ["端口"]
        assert doctor.failed(doctor.run_checks(app.cfg, "telegram", ports=False)) == []
