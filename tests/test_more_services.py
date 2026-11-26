"""仓库自带的 GitHub、Notion、Slack 档案，和档案里的固定请求头（auth.headers）。

不连真的服务：规则直接测，经出网代理的部分换成本机的假服务器。
"""
from __future__ import annotations

import base64
import json
import ssl

import aiohttp
import pytest
from aiohttp import web

from host import sessions
from host.app import PROFILES_DIR
from host.egress.services import load_profiles, parse_profile, render_summary, request_fields
from host.proposals import ProposalError
from tests.conftest import self_signed_context, wait_until

NOTION_REAL = "secret_NOTION-REAL-do-not-leak"
SLACK_REAL = "xoxb-SLACK-REAL-do-not-leak"


def body(data) -> bytes:
    return json.dumps(data).encode()


def classify(profile, method, path, data=None):
    action, params = profile.match(method, path)
    cls = action.cls if action else ("read" if method in ("GET", "HEAD") else "irreversible")
    fields, _ = request_fields(action, params, body(data) if data is not None else b"")
    return action, cls, fields


# ---------------------------------------------------------------- 档案本身


def test_every_bundled_profile_loads():
    names = {p.stem for p in PROFILES_DIR.glob("*.yaml")}
    loaded = load_profiles(PROFILES_DIR)
    assert names <= set(loaded), f"读不了的档案：{names - set(loaded)}"
    assert {"github", "notion", "slack"} <= set(loaded)
    for name in ("github", "notion", "slack"):
        assert loaded[name].usage and loaded[name].hosts


def test_github_rules():
    gh = load_profiles(PROFILES_DIR)["github"]
    assert classify(gh, "GET", "/repos/o/r/issues")[1] == "read"
    action, cls, fields = classify(gh, "POST", "/repos/octo/demo/issues/7/comments", {"body": "看过了"})
    assert action.id == "comment" and cls == "send"
    assert render_summary(action.summary, fields) == "在 octo/demo #7 下面评论"
    assert classify(gh, "POST", "/repos/o/r/issues", {"title": "t"})[0].id == "issue"
    assert classify(gh, "PUT", "/repos/o/r/pulls/3/merge")[1] == "irreversible"
    assert classify(gh, "PUT", "/repos/o/r/contents/docs/a.md")[0].id == "commit_file"
    assert classify(gh, "DELETE", "/repos/o/r")[1] == "irreversible"
    assert classify(gh, "POST", "/graphql")[1] == "write"          # 查询和修改分不开：不当读取
    assert classify(gh, "POST", "/user/repos")[1] == "irreversible"  # 没列出的写操作


def test_notion_rules_and_fixed_header():
    notion = load_profiles(PROFILES_DIR)["notion"]
    assert notion.auth["headers"] == {"Notion-Version": "2022-06-28"}
    assert classify(notion, "POST", "/v1/search", {"query": "周报"})[1] == "read"
    assert classify(notion, "POST", "/v1/databases/d1/query", {})[1] == "read"
    assert classify(notion, "POST", "/v1/databases", {})[1] == "write"     # 建数据库不是查询
    assert classify(notion, "PATCH", "/v1/databases/d1")[1] == "write"
    assert classify(notion, "POST", "/v1/pages")[1] == "write"
    assert classify(notion, "PATCH", "/v1/blocks/b1/children")[0].id == "append"
    assert classify(notion, "POST", "/v1/comments")[1] == "send"


def test_slack_rules_go_by_method_name_not_http_verb():
    slack = load_profiles(PROFILES_DIR)["slack"]
    # 发消息：用 GET 也一样是发送
    assert classify(slack, "GET", "/api/chat.postMessage")[1] == "send"
    action, cls, fields = classify(slack, "POST", "/api/chat.postMessage", {"channel": "C1", "text": "hi"})
    assert cls == "send" and render_summary(action.summary, fields) == "在 Slack 的 C1 发消息"
    assert action.constraint_keys == ["body.channel"]
    assert classify(slack, "GET", "/api/conversations.history")[1] == "read"
    assert classify(slack, "POST", "/api/conversations.history")[1] == "read"
    assert classify(slack, "GET", "/api/files.upload")[1] == "send"
    assert classify(slack, "GET", "/api/chat.delete")[1] == "irreversible"
    # 档案没列出的方法：哪怕是 GET 也每次问
    action, cls, fields = classify(slack, "GET", "/api/admin.users.remove")
    assert cls == "irreversible" and "admin.users.remove" in render_summary(action.summary, fields)
    assert classify(slack, "GET", "/somewhere/else")[1] == "irreversible"


# ---------------------------------------------------------------- 固定请求头的校验

BASE = {"service": "notes", "hosts": ["api.notes.example"]}


def test_fixed_headers_validation():
    ok = parse_profile({**BASE, "auth": {"header": "Authorization", "scheme": "Bearer",
                                         "headers": {"Notes-Version": "3"}}}, trusted=True)
    assert ok.auth["headers"] == {"Notes-Version": "3"}
    bad = [{"X-HTTP-Method-Override": "DELETE"}, {"Cookie": "a=b"}, {"Host": "evil.example"},
           {"authorization": "x"}, {"Content-Type": "text/plain"}, {"Bad Name": "1"},
           {"Notes-Version": "1\r\nX-Evil: 1"}, {"Notes-Version": 3}, {"Notes-Version": "x" * 201}, {}]
    for headers in bad:
        with pytest.raises(ValueError):
            parse_profile({**BASE, "auth": {"header": "Authorization", "headers": headers}}, trusted=True)
    with pytest.raises(ValueError):          # 令牌放在哪个头里，就不能再拿它当固定头
        parse_profile({**BASE, "auth": {"header": "X-Api-Key", "headers": {"x-api-key": "1"}}}, trusted=True)


def test_agent_proposals_cannot_carry_fixed_headers():
    with pytest.raises(ValueError, match=r"auth.headers"):
        parse_profile({**BASE, "auth": {"header": "Authorization", "headers": {"Notes-Version": "3"}}})


def test_profile_propose_rejects_fixed_headers(make_app):
    app = make_app()
    sid = sessions.current_conversation(app.db, 6)
    text = ("service: notes\nhosts: [api.notes.example]\n"
            "auth: {header: Authorization, scheme: Bearer, headers: {X-HTTP-Method-Override: DELETE}}\n"
            "actions:\n  - {id: read, match: {method: GET, path: '/**'}, class: read}\n")
    with pytest.raises(ProposalError, match=r"auth.headers"):
        app.proposals.propose_profile(sid, text)


def test_overriding_profile_keeps_the_bundled_fixed_headers(tmp_path):
    mine = tmp_path / "profiles"
    mine.mkdir()
    # 你批准的、Agent 改写过的 notion 档案：写不了 auth.headers
    (mine / "notion.yaml").write_text(
        "service: notion\nhosts: [api.notion.com]\nauth: {header: Authorization, scheme: Bearer}\n"
        "actions:\n  - {id: read, match: {method: GET, path: '/**'}, class: read}\n", encoding="utf-8")
    notion = load_profiles(PROFILES_DIR, mine)["notion"]
    assert [a.id for a in notion.actions] == ["read"]
    assert notion.auth["headers"] == {"Notion-Version": "2022-06-28"}


# ---------------------------------------------------------------- 经出网代理


class FakeApi:
    def __init__(self):
        self.calls: list[dict] = []
        self.port = 0

    async def handle(self, request: web.Request) -> web.Response:
        self.calls.append({"method": request.method, "host": request.host, "path": request.path,
                           "auth": request.headers.get("Authorization", ""),
                           "version": request.headers.get("Notion-Version")})
        if request.headers.get("Authorization") not in (f"Bearer {NOTION_REAL}", f"Bearer {SLACK_REAL}"):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True, "results": []})


@pytest.fixture
async def fake_api(tmp_path):
    fake = FakeApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=self_signed_context("api.notion.com", tmp_path))
    await site.start()
    fake.port = site._server.sockets[0].getsockname()[1]
    yield fake
    await runner.cleanup()


@pytest.fixture
async def svc_app(make_app, fake_api):
    app = make_app(egress_overrides={"api.notion.com": ("127.0.0.1", fake_api.port),
                                     "slack.com": ("127.0.0.1", fake_api.port)},
                   egress_ssl_insecure=True, egress_hold_s=0.3)
    app.creds.add("notion", {"token": NOTION_REAL})
    app.creds.add("slack", {"token": SLACK_REAL})
    await app.start(with_channel=False)
    return app


async def call(app, run, service, method, url, data=None, headers=None):
    surrogate = app.creds.get(service).surrogate
    proxy_auth = "Basic " + base64.b64encode(f"run:{run.token}".encode()).decode()
    ctx = ssl.create_default_context(cafile=str(app.egress.ca_cert))
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, proxy=f"http://127.0.0.1:{app.egress_port}",
                             proxy_headers={"Proxy-Authorization": proxy_auth},
                             headers={"Authorization": f"Bearer {surrogate}", "content-type": "application/json",
                                      **(headers or {})},
                             data=json.dumps(data) if data is not None else None, ssl=ctx) as r:
            return r.status, await r.text()


async def test_notion_and_slack_through_the_proxy(svc_app, fake_api):
    app = svc_app
    run = app.runs.start(sessions.current_conversation(app.db, 6), "t-more", "user")

    # Notion 的搜索是 POST，但按档案是读取：直接放行，代理补上 Notion-Version 和真令牌
    assert (await call(app, run, "notion", "POST", "https://api.notion.com/v1/search", {"query": "周报"}))[0] == 200
    assert fake_api.calls[-1]["version"] == "2022-06-28"
    assert fake_api.calls[-1]["auth"] == f"Bearer {NOTION_REAL}"
    # 电脑自己带了版本号就用它的
    await call(app, run, "notion", "GET", "https://api.notion.com/v1/pages/p1", headers={"Notion-Version": "2025-09-03"})
    assert fake_api.calls[-1]["version"] == "2025-09-03"
    # 固定请求头只给 Notion：Slack 的请求里没有
    assert (await call(app, run, "slack", "GET", "https://slack.com/api/conversations.history?channel=C1"))[0] == 200
    assert fake_api.calls[-1]["version"] is None and fake_api.calls[-1]["auth"] == f"Bearer {SLACK_REAL}"

    # Slack 用 GET 发消息：照样要你批准，请求没有发出去
    sent = len(fake_api.calls)
    status, _ = await call(app, run, "slack", "GET", "https://slack.com/api/chat.postMessage?channel=C1&text=hi")
    assert status == 403
    await wait_until(lambda: app.channel.approvals)
    assert "Slack" in app.channel.approvals[-1][0]
    assert len(fake_api.calls) == sent

    for table in ("egress_log", "approval", "outbox"):
        dump = json.dumps([dict(r) for r in app.db.execute(f"SELECT * FROM {table}")], ensure_ascii=False)
        assert NOTION_REAL not in dump and SLACK_REAL not in dump
    app.runs.finish(run, "SILENT")
