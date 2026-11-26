"""自带技能：早间简报、盯邮件、盯日历。

- 安装器：第一次运行前放进主目录；不覆盖 Agent 改过的、原来就有的；/skills 和 /skills reset
- 前置检查拿得到已连接服务的替身令牌
- 两个检查脚本：对着本机的假 Gmail / 假日历跑，不连真的 Google
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web

from host.bundled import BUNDLED_DIR, bundled, digest
from tests.conftest import all_done, wait_until

SKILLS = ("calendar-watch", "mail-watch", "morning-brief")


# ---------------------------------------------------------------- 仓库里的技能本身


def test_bundled_skills_are_well_formed():
    found = bundled()
    assert set(SKILLS) <= set(found)
    for name in SKILLS:
        text = found[name]["SKILL.md"].decode("utf-8")
        assert text.startswith(f"---\nname: {name}\ndescription: "), name
    assert "check.py" in found["mail-watch"] and "check.py" in found["calendar-watch"]
    # 例行任务里写的检查脚本路径要和实际放的位置一致
    for name in ("mail-watch", "calendar-watch"):
        assert f"precheck=`skills/{name}/check.py`" in found[name]["SKILL.md"].decode("utf-8")


# ---------------------------------------------------------------- 安装器


async def test_installer_puts_skills_once_and_never_overwrites(make_app, tmp_path):
    app = make_app(bundled_skills=True)
    home = tmp_path / "home"
    # 你（或 Agent）原来就写了一个同名技能
    (home / "skills" / "morning-brief").mkdir(parents=True)
    (home / "skills" / "morning-brief" / "SKILL.md").write_text("我自己的简报", encoding="utf-8")
    await app.computer.ensure_awake()

    installed = await app.bundled.install_missing()
    assert installed == ["calendar-watch", "mail-watch"]
    assert (home / "skills" / "mail-watch" / "check.py").read_bytes() == (BUNDLED_DIR / "mail-watch" / "check.py").read_bytes()
    assert (home / "skills" / "morning-brief" / "SKILL.md").read_text(encoding="utf-8") == "我自己的简报"
    state = app.bundled.state()
    assert state["morning-brief"]["status"] == "existed" and state["mail-watch"]["status"] == "installed"

    # Agent 改了、删了：宿主不再动
    (home / "skills" / "mail-watch" / "SKILL.md").write_text("Agent 改过的", encoding="utf-8")
    import shutil
    shutil.rmtree(home / "skills" / "calendar-watch")
    assert await app.bundled.install_missing() == []
    assert (home / "skills" / "mail-watch" / "SKILL.md").read_text(encoding="utf-8") == "Agent 改过的"
    assert not (home / "skills" / "calendar-watch").exists()

    listing = app.bundled.listing()
    assert "mail-watch" in listing and "原来就有" in listing and "已放进主目录" in listing

    # 仓库里的版本更新了：只提示，不覆盖
    state["mail-watch"]["hash"] = "old"
    app.bundled._save(state)
    assert "/skills reset mail-watch" in app.bundled.listing()
    assert await app.bundled.install_missing() == []
    assert (home / "skills" / "mail-watch" / "SKILL.md").read_text(encoding="utf-8") == "Agent 改过的"

    # /skills reset：用仓库的覆盖，状态记成新版本
    reply = await app.run_command("/skills reset", "mail-watch")
    assert "已用仓库里的版本覆盖" in reply
    assert (home / "skills" / "mail-watch" / "SKILL.md").read_bytes() == (BUNDLED_DIR / "mail-watch" / "SKILL.md").read_bytes()
    assert app.bundled.state()["mail-watch"]["hash"] == digest(bundled()["mail-watch"])
    assert "没有叫" in await app.run_command("/skills reset", "nope")


async def test_skills_commands(make_app):
    app = make_app(bundled_skills=True)
    await app.on_user_text("c1", "/skills")
    reply = app.db.execute("SELECT body FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()["body"]
    assert "morning-brief" in reply and "还没放进主目录" in reply
    await app.on_user_text("c2", "/skills reset mail-watch")
    queued = [json.loads(e["payload"]) for e in app.inbox.pending() if e["source"] == "command"]
    assert queued == [{"name": "/skills reset", "arg": "mail-watch"}]
    await app.on_user_text("c3", "/skills reset ../etc")
    assert "用法" in app.db.execute("SELECT body FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()["body"]


def test_bundled_skills_off(make_app):
    app = make_app()            # 测试默认关掉
    assert "关着" in app.bundled.listing()


ENV_DUMP = """import json, os, sys
keys = {k: v for k, v in os.environ.items() if k.startswith("SMRA_CRED_")}
open(os.path.join(os.environ["AGENT_HOME"], "work", "env.json"), "w").write(json.dumps(keys))
sys.exit(10)
"""


async def test_precheck_gets_surrogates_and_bundled_skills(make_app, tmp_path):
    app = make_app(bundled_skills=True)
    app.creds.add("gmail", {"token": "ya29.REAL-do-not-leak"})
    await app.start(with_channel=False)
    home = tmp_path / "home"
    (home / "tools").mkdir(parents=True, exist_ok=True)
    (home / "work").mkdir(parents=True, exist_ok=True)
    (home / "tools" / "env.py").write_text(ENV_DUMP, encoding="utf-8")
    app.inbox.add("timer", "tm-1", {"text": "看邮件", "precheck": "tools/env.py"})
    await wait_until(lambda: all_done(app) and app.db.execute(
        "SELECT 1 FROM run WHERE kind = 'precheck' AND outcome = 'UNCHANGED'").fetchone())
    env = json.loads((home / "work" / "env.json").read_text())
    assert env == {"SMRA_CRED_GMAIL": app.creds.get("gmail").surrogate}
    assert (home / "skills" / "mail-watch" / "SKILL.md").exists()     # 前置检查之前就放好了


# ---------------------------------------------------------------- 检查脚本


class FakeGoogle:
    def __init__(self):
        self.messages: list[str] = []
        self.events: list[dict] = []
        self.status = 200
        self.requests: list[tuple[str, dict]] = []
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        if request.headers.get("Authorization") != "Bearer sur_test" or self.status != 200:
            return web.json_response({"error": "nope"}, status=self.status if self.status != 200 else 401)
        if request.path.endswith("/messages"):
            return web.json_response({"messages": [{"id": i, "threadId": i} for i in self.messages]})
        return web.json_response({"items": self.events})


@pytest.fixture
async def google_api():
    fake = FakeGoogle()
    app = web.Application()
    app.router.add_get("/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


async def run_check(skill: str, home: Path, env: dict) -> subprocess.CompletedProcess:
    full = {"AGENT_HOME": str(home), "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1",
            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), **env}
    return await asyncio.to_thread(subprocess.run, [sys.executable, str(BUNDLED_DIR / skill / "check.py")],
                                   env=full, capture_output=True, text=True, encoding="utf-8", timeout=60)


async def test_mail_watch_check(google_api, tmp_path):
    env = {"SMRA_CRED_GMAIL": "sur_test", "MAIL_WATCH_API": f"{google_api.url}/gmail/v1/users/me"}
    work = tmp_path / "work" / "mail-watch"

    assert (await run_check("mail-watch", tmp_path, env)).returncode == 10      # 没邮件
    google_api.messages = ["m1", "m2"]
    r = await run_check("mail-watch", tmp_path, env)
    assert r.returncode == 0 and "2 封新邮件" in r.stdout
    assert json.loads((work / "new.json").read_text(encoding="utf-8")) == ["m1", "m2"]
    assert "is:unread" in google_api.requests[-1][1]["q"]
    assert (await run_check("mail-watch", tmp_path, env)).returncode == 10      # 看过的不再叫醒

    # Agent 处理了一封，没处理完的留着，新的接在后面
    (work / "new.json").write_text('["m2"]', encoding="utf-8")
    google_api.messages = ["m3", "m1", "m2"]
    assert (await run_check("mail-watch", tmp_path, env)).returncode == 0
    assert json.loads((work / "new.json").read_text(encoding="utf-8")) == ["m2", "m3"]

    # 自定义筛选条件
    (work / "config.json").write_text('{"query": "from:boss@example.com"}', encoding="utf-8")
    await run_check("mail-watch", tmp_path, env)
    assert google_api.requests[-1][1]["q"] == "from:boss@example.com"

    # 出错：令牌失效、没连接。退出码不是 0 也不是 10，叫醒 Agent 看错误
    google_api.status = 401
    r = await run_check("mail-watch", tmp_path, env)
    assert r.returncode == 2 and "401" in r.stderr
    r = await run_check("mail-watch", tmp_path, {"MAIL_WATCH_API": env["MAIL_WATCH_API"]})
    assert r.returncode == 2 and "没有连接 gmail" in r.stderr


async def test_calendar_watch_check(google_api, tmp_path):
    now = "2026-11-26T09:00:00+08:00"
    env = {"SMRA_CRED_GCALENDAR": "sur_test", "CALENDAR_WATCH_API": f"{google_api.url}/calendar/v3",
           "CALENDAR_WATCH_NOW": now}
    work = tmp_path / "work" / "calendar-watch"

    assert (await run_check("calendar-watch", tmp_path, env)).returncode == 10
    query = google_api.requests[-1][1]
    assert google_api.requests[-1][0] == "/calendar/v3/calendars/primary/events"
    assert query["timeMin"] == now and query["timeMax"] == "2026-11-26T10:00:00+08:00"
    assert query["singleEvents"] == "true"

    google_api.events = [
        {"id": "e1", "summary": "周会", "start": {"dateTime": "2026-11-26T09:45:00+08:00"},
         "end": {"dateTime": "2026-11-26T10:30:00+08:00"}, "location": "会议室 A",
         "hangoutLink": "https://meet.example/abc",
         "attendees": [{"email": "me@example.com", "self": True}, {"email": "li@example.com"}]},
        {"id": "e2", "summary": "全天：出差", "start": {"date": "2026-11-26"}},
        {"id": "e3", "summary": "拒了的会", "start": {"dateTime": "2026-11-26T09:30:00+08:00"},
         "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "declined"}]},
        {"id": "e4", "summary": "取消了", "status": "cancelled", "start": {"dateTime": "2026-11-26T09:20:00+08:00"}},
    ]
    r = await run_check("calendar-watch", tmp_path, env)
    assert r.returncode == 0 and "周会" in r.stdout
    due = json.loads((work / "due.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in due] == ["e1"]
    assert due[0]["location"] == "会议室 A" and due[0]["link"] == "https://meet.example/abc"
    assert due[0]["attendees"] == ["li@example.com"]

    assert (await run_check("calendar-watch", tmp_path, env)).returncode == 10      # 每个日程只提醒一次

    # 改了时间的日程算新的一次；全天日程按配置提醒
    google_api.events[0]["start"] = {"dateTime": "2026-11-26T09:50:00+08:00"}
    (work / "config.json").write_text('{"all_day": true, "lookahead_minutes": 30}', encoding="utf-8")
    r = await run_check("calendar-watch", tmp_path, env)
    assert r.returncode == 0
    assert google_api.requests[-1][1]["timeMax"] == "2026-11-26T09:30:00+08:00"
    assert [d["id"] for d in json.loads((work / "due.json").read_text(encoding="utf-8"))] == ["e1", "e1", "e2"]

    # 两天前提醒过的记录清掉
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    assert len(state["reminded"]) == 3
    google_api.events = []
    r = await run_check("calendar-watch", tmp_path, {**env, "CALENDAR_WATCH_NOW": "2026-11-29T09:00:00+08:00"})
    assert r.returncode == 10
    assert json.loads((work / "state.json").read_text(encoding="utf-8"))["reminded"] == {}

    google_api.status = 403
    r = await run_check("calendar-watch", tmp_path, env)
    assert r.returncode == 2 and "403" in r.stderr
