"""Google 日历和云盘：仓库自带的档案、连接命令的默认权限、经出网代理的读写和审批。

Google 换成本机的假服务器，不连真的账号。
"""
from __future__ import annotations

import base64
import json
import ssl
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web

from host import creds, sessions
from host.app import PROFILES_DIR
from host.creds import GOOGLE_REDIRECT, CredentialStore, load_key
from host.db import connect
from host.egress.services import NO_ATTENDEES, load_profiles, request_fields
from tests.conftest import self_signed_context, wait_until
from tests.test_creds_login import google, run_cli  # noqa: F401  （google 是 fixture）

API = "https://www.googleapis.com"
CAL_REAL = "ya29.REAL-CALENDAR-do-not-leak"
DRIVE_REAL = "ya29.REAL-DRIVE-do-not-leak"


def body(data) -> bytes:
    return json.dumps(data).encode()


# ---------------------------------------------------------------- 档案

def test_calendar_profile_rules_and_fields():
    cal = load_profiles(PROFILES_DIR)["gcalendar"]
    assert cal.match("GET", "/calendar/v3/calendars/primary/events")[0].cls == "read"
    create, params = cal.match("POST", "/calendar/v3/calendars/primary/events")
    assert create.id == "create" and params == {"calendar": "primary"}
    assert cal.match("POST", "/calendar/v3/calendars/primary/events/quickAdd")[0].id == "quick_add"
    assert cal.match("PATCH", "/calendar/v3/calendars/primary/events/e1")[0].id == "update"
    assert cal.match("DELETE", "/calendar/v3/calendars/primary/events/e1")[0].cls == "irreversible"

    fields, problem = request_fields(create, params, body({
        "summary": "牙医", "start": {"dateTime": "2026-11-26T15:00:00+08:00"},
        "end": {"dateTime": "2026-11-26T16:00:00+08:00"}, "location": "诊所"}))
    assert problem is None and fields["title"] == "牙医" and fields["start"].startswith("2026-11-26T15")
    assert fields["attendees"] == [NO_ATTENDEES]
    fields, _ = request_fields(create, params, body({"summary": "周会", "attendees": [{"email": "Li@Example.com"}]}))
    assert fields["attendees"] == ["li@example.com"]
    update = cal.match("PATCH", "/calendar/v3/calendars/primary/events/e1")[0]
    fields, _ = request_fields(update, {"id": "e1"}, body({"summary": "改个名"}))
    assert fields["attendees"] == ["（参与人不变）"]
    _, problem = request_fields(create, params, b"[1, 2]")
    assert problem                                     # 看不懂的请求体：审批里给原始内容


def test_drive_profile_rules_and_fields():
    drive = load_profiles(PROFILES_DIR)["gdrive"]
    assert drive.match("GET", "/drive/v3/files/f1")[0].cls == "read"
    share = drive.match("POST", "/drive/v3/files/f1/permissions")[0]
    assert share.cls == "send" and share.constraint_keys == ["grantee"]
    assert drive.match("DELETE", "/drive/v3/files/f1/permissions/p1")[0].id == "unshare"
    assert drive.match("DELETE", "/drive/v3/files/f1")[0].cls == "irreversible"
    assert drive.match("POST", "/upload/drive/v3/files")[0].id == "upload"
    assert drive.match("PATCH", "/drive/v3/files/f1")[0].cls == "write"
    for data, who in (({"type": "user", "role": "reader", "emailAddress": "Zhang@Example.com"}, "zhang@example.com"),
                      ({"type": "anyone", "role": "reader"}, "anyone"),
                      ({"type": "domain", "role": "reader", "domain": "corp.example"}, "domain:corp.example")):
        fields, problem = request_fields(share, {"id": "f1"}, body(data))
        assert problem is None and fields["grantee"] == [who]


# ---------------------------------------------------------------- 连接命令

def test_calendar_login_uses_its_own_scope_and_reuses_the_google_client(monkeypatch, tmp_path, google):  # noqa: F811
    ok = lambda q: f"{GOOGLE_REDIRECT}?state={q['state']}&code=CODE-1"
    run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"], ok)
    printed = run_cli(monkeypatch, tmp_path, google, ["add", "gcalendar", "--google-login"], ok,
                      client_id="", client_secret="")
    url = next(line for line in printed if line.startswith("https://accounts.google.com/"))
    scope = parse_qs(urlsplit(url).query)["scope"][0]
    assert scope == creds.GOOGLE_SCOPES["gcalendar"] and "gmail" not in scope
    assert google.forms[-1]["client_id"] == "cid.apps.googleusercontent.com"     # 沿用 gmail 的
    store = CredentialStore(connect(tmp_path / "data" / "host.db"), load_key(tmp_path / "data"))
    assert store.get("gcalendar").hosts is None                                   # 主机按仓库的档案
    assert store._secret("gcalendar")["refresh_token"] == "1//REFRESH-xyz"


def test_unknown_service_login_needs_a_scope(monkeypatch, tmp_path, google):  # noqa: F811
    folder = tmp_path / "data" / "profiles"
    folder.mkdir(parents=True)
    (folder / "gtasks.yaml").write_text("service: gtasks\nhosts: [tasks.googleapis.com]\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, tmp_path, google, ["add", "gtasks", "--google-login"], lambda q: "")
    assert "--scope" in str(e.value)


# ---------------------------------------------------------------- 经出网代理

class FakeGoogleApis:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []     # (方法, 路径, 认证头)
        self.port = 0

    async def handle(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        self.calls.append((request.method, request.path, auth))
        if auth not in (f"Bearer {CAL_REAL}", f"Bearer {DRIVE_REAL}"):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.method == "GET":
            return web.json_response({"items": [{"id": "e1", "summary": "牙医"}]})
        return web.json_response({"id": "new"})


@pytest.fixture
async def apis(tmp_path):
    fake = FakeGoogleApis()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=self_signed_context("www.googleapis.com", tmp_path))
    await site.start()
    fake.port = site._server.sockets[0].getsockname()[1]
    yield fake
    await runner.cleanup()


@pytest.fixture
async def g_app(make_app, apis):
    app = make_app(egress_overrides={"www.googleapis.com": ("127.0.0.1", apis.port)},
                   egress_ssl_insecure=True, egress_hold_s=0.3)
    app.creds.add("gcalendar", {"token": CAL_REAL})
    app.creds.add("gdrive", {"token": DRIVE_REAL})
    await app.start(with_channel=False)
    return app


def live_run(app):
    sid = sessions.current_conversation(app.db, 6)
    return app.runs.start(sid, f"t-{len(app.runs._by_token)}", "user")


async def call(app, run, service, method, url, data=None):
    surrogate = app.creds.get(service).surrogate
    proxy_auth = "Basic " + base64.b64encode(f"run:{run.token}".encode()).decode()
    ctx = ssl.create_default_context(cafile=str(app.egress.ca_cert))
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, proxy=f"http://127.0.0.1:{app.egress_port}",
                             proxy_headers={"Proxy-Authorization": proxy_auth},
                             headers={"Authorization": f"Bearer {surrogate}", "content-type": "application/json"},
                             data=json.dumps(data) if data is not None else None, ssl=ctx) as r:
            return r.status, await r.text()


async def test_calendar_and_drive_through_the_proxy(g_app, apis):
    app = g_app
    run = live_run(app)
    events = f"{API}/calendar/v3/calendars/primary/events"

    # 读：直接放行，两个服务共用一个主机，各自换成自己的真令牌
    assert (await call(app, run, "gcalendar", "GET", events))[0] == 200
    assert apis.calls[-1][2] == f"Bearer {CAL_REAL}"
    assert (await call(app, run, "gdrive", "GET", f"{API}/drive/v3/files?q=x"))[0] == 200
    assert apis.calls[-1][2] == f"Bearer {DRIVE_REAL}"

    # 只有自己的日程：审批写清楚时间地点；长期授权只覆盖"只有你自己"的日程
    mine = {"summary": "牙医", "start": {"dateTime": "2026-11-26T15:00:00+08:00"},
            "end": {"dateTime": "2026-11-26T16:00:00+08:00"}, "location": "诊所"}
    status, _ = await call(app, run, "gcalendar", "POST", events, mine)
    assert status == 403
    await wait_until(lambda: app.channel.approvals)
    text, approval_id, scopes = app.channel.approvals[-1]
    assert "新建日程：牙医" in text and "诊所" in text and NO_ATTENDEES in text and "forever" in scopes
    assert "已批准" in app.adjudicator.resolve(approval_id, True, "forever")
    assert (await call(app, run, "gcalendar", "POST", events, {**mine, "summary": "理发"}))[0] == 200
    invite = {**mine, "summary": "周会", "attendees": [{"email": "li@example.com"}]}
    assert (await call(app, run, "gcalendar", "POST", events, invite))[0] == 403
    assert "li@example.com" in app.channel.approvals[-1][0]

    # 云盘：分享给所有人要问你，审批写明是"任何拿到链接的人"
    share = f"{API}/drive/v3/files/f1/permissions"
    assert (await call(app, run, "gdrive", "POST", share, {"type": "anyone", "role": "reader"}))[0] == 403
    assert "分享给 anyone" in app.channel.approvals[-1][0]
    # 永久删除只能一次一批
    assert (await call(app, run, "gdrive", "DELETE", f"{API}/drive/v3/files/f1"))[0] == 403
    assert app.channel.approvals[-1][2] == ["once"]
    assert not any(m in ("DELETE",) for m, _, _ in apis.calls)

    for table in ("egress_log", "approval", "outbox"):
        dump = json.dumps([dict(r) for r in app.db.execute(f"SELECT * FROM {table}")], ensure_ascii=False)
        assert CAL_REAL not in dump and DRIVE_REAL not in dump
    app.runs.finish(run, "SILENT")
