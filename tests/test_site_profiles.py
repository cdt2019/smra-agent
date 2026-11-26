"""已登录网站的档案（type: site）和截图的注入检测。

网站档案：浏览器在一个网站上发出的请求，按档案归类。标成读取的直接放行；其余的可以长期授权，
限定在约束键的取值上。没匹配到的请求照旧走浏览器的默认规则。
"""
from __future__ import annotations

import json

import aiohttp
import pytest

from host import models, sessions
from host.egress.adjudicator import BrowserReq, Req
from host.guard import NO_VISION_NOTE, InjectionCheck, new_tool_images, strip_images
from tests.conftest import wait_until

SITE_PROFILE = """type: site
service: shop
title: 小店
hosts: [shop.example]
actions:
  - id: search
    match: {method: POST, path: /api/search}
    class: read
  - id: message
    match: {method: POST, path: "/messages/{to}"}
    class: send
    summary: "给 {to} 发消息"
    show: [form.text]
    constraint_keys: [to]
  - id: cancel
    match: {method: POST, path: "/orders/{id}/cancel"}
    class: irreversible
    summary: "取消订单 {id}"
"""

FORM = "application/x-www-form-urlencoded"


@pytest.fixture
async def site_app(make_app):
    app = make_app()
    await app.start(with_channel=False)
    return app


def live_run(app):
    sid = sessions.current_conversation(app.db, 6)
    return app.runs.start(sid, f"t-{len(app.runs._by_token)}", "user")


async def host_call(app, run, path, body):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{app.port}/host/{path}", json=body,
                          headers={"authorization": f"Bearer {run.token}"}) as r:
            return r.status, await r.json()


async def approve_site_profile(app, run, text=SITE_PROFILE) -> str:
    status, data = await host_call(app, run, "profile_propose", {"yaml": text})
    assert status == 200, data
    await wait_until(lambda: app.channel.reviews)
    review, kind, proposal_id = app.channel.reviews[-1]
    assert kind == "profile"
    await app.on_user_text("c-approve", f"/profile approve {proposal_id}")
    await wait_until(lambda: "shop" in app.services.profiles)
    return review


def browser_req(app, method, path, body=b"", *, acting=True, site="same-origin", cookies=True,
                content_type=FORM, query="") -> Req:
    """浏览器里点了一个提交按钮（或者页面脚本发了请求）之后，代理看到的那个请求。"""
    url = f"https://shop.example{path}" + (f"?{query}" if query else "")
    info = BrowserReq(mode="cors", dest="empty", site=site, acting=acting, cookies=cookies,
                      content_type=content_type, page="https://shop.example/home")
    call = app.services.site_call("shop.example", method, path, query, body, content_type)
    return Req(method=method, url=url, host="shop.example", port=443, body=body, browser=info, site=call)


async def test_site_profile_review_and_decisions(site_app):
    app = site_app
    run = live_run(app)
    review = await approve_site_profile(app, run)
    assert "网站档案" in review and "认证" not in review and "响应过滤" not in review
    assert "照旧按浏览器的默认规则" in review
    assert (app.cfg.data_dir / "profiles" / "shop.yaml").exists()
    adj = app.adjudicator

    # 档案里标成读取的查询：页面脚本自己发的也直接放行
    d = adj.decide(run, browser_req(app, "POST", "/api/search", b'{"q":"x"}', acting=False,
                                    content_type="application/json"))
    assert d.verdict == "allow" and "档案里的读取" in d.reason

    # 没匹配到的提交照旧：只能批"仅这一次"
    d = adj.decide(run, browser_req(app, "POST", "/profile/save", b"a=1"))
    assert d.verdict == "ask" and d.scopes == ["once"]

    # 后台的提交照旧拦下，不打扰你
    d = adj.decide(run, browser_req(app, "POST", "/messages/bob", b"text=hi", acting=False))
    assert d.verdict == "deny"

    # 发消息：审批显示摘要和字段，可以长期授权，只覆盖同一个收件人
    req = browser_req(app, "POST", "/messages/bob", "text=你好&pay_password=SEKRIT".encode())
    d = adj.decide(run, req)
    assert d.verdict == "ask" and "forever" in d.scopes and d.category == "site:send"
    d = await adj.ask(run, req, d, hold_s=0)
    text, approval_id, scopes = app.channel.approvals[-1]
    assert "给 bob 发消息" in text and "你好" in text and "shop.example/messages/bob" in text
    assert "SEKRIT" not in text and "如果选长期授权，只覆盖" in text and "登录状态" in text
    assert "已批准" in adj.resolve(approval_id, True, "forever")
    assert adj.decide(run, browser_req(app, "POST", "/messages/bob", "text=又一条".encode())).verdict == "allow"
    assert adj.decide(run, browser_req(app, "POST", "/messages/carol", b"text=x")).verdict == "ask"

    # 读到疑似注入后，长期授权暂停
    run.suspended = True
    assert adj.decide(run, browser_req(app, "POST", "/messages/bob", b"text=x")).verdict == "ask"
    run.suspended = False

    # 不可撤销的只能一次一批
    d = adj.decide(run, browser_req(app, "POST", "/orders/A1/cancel", b"reason=x"))
    assert d.verdict == "ask" and d.scopes == ["once"]
    app.runs.finish(run, "SILENT")


async def test_site_profile_proposal_rules(site_app):
    app = site_app
    run = live_run(app)
    # 不需要连接；但不能和接口服务重名
    app.creds.add("tickets", {"token": "t"}, title="工单", hosts=["api.tickets.example"],
                  auth={"header": "X-Api-Key"})
    status, data = await host_call(app, run, "profile_propose",
                                   {"yaml": SITE_PROFILE.replace("service: shop", "service: tickets")})
    assert status == 400 and "接口服务" in data["error"]["message"]
    for bad in (SITE_PROFILE + "auth: {header: X}\n", SITE_PROFILE.replace("type: site", "type: magic")):
        status, _ = await host_call(app, run, "profile_propose", {"yaml": bad})
        assert status == 400
    # 把发消息标成读取：审阅里有警告
    loose = SITE_PROFILE.replace("class: send", "class: read")
    status, _ = await host_call(app, run, "profile_propose", {"yaml": loose})
    assert status == 200
    await wait_until(lambda: app.channel.reviews)
    assert "⚠ 规则 message：POST /messages/{to} 被标成\"读取\"" in app.channel.reviews[-1][0]
    # 别的网站档案已经管着同一个主机
    await app.on_user_text("c1", f"/profile approve {app.channel.reviews[-1][2]}")
    await wait_until(lambda: "shop" in app.services.profiles)
    status, data = await host_call(app, run, "profile_propose",
                                   {"yaml": SITE_PROFILE.replace("service: shop", "service: shop2")})
    assert status == 400 and "shop.example" in data["error"]["message"]
    app.runs.finish(run, "SILENT")


# ---------------------------------------------------------------- 截图的注入检测

PNG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def body_with_screenshot():
    return {"messages": [
        {"role": "user", "content": "看看这个页面"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "browser", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": [dict(PNG), {"type": "text", "text": "截图如上"}]}]},
    ]}


def test_images_collected_and_stripped():
    body = body_with_screenshot()
    assert new_tool_images(body) == [PNG]
    assert strip_images(body)
    inner = body["messages"][-1]["content"][0]["content"]
    assert inner[0] == {"type": "text", "text": NO_VISION_NOTE} and inner[1]["text"] == "截图如上"
    assert new_tool_images(body) == [] and not strip_images(body)


def test_guard_vision_presets():
    env = lambda **v: (lambda k, d=None: v.get(k, d))
    d = models.from_env(env(SMRA_MODEL_PROVIDER="deepseek", DEEPSEEK_API_KEY="k"))
    assert "vision" not in d["model_features"] and d["guard_vision"] is True
    assert "vision" in models.from_env(env(ANTHROPIC_API_KEY="k"))["model_features"]
    o = models.from_env(env(SMRA_MODEL_PROVIDER="openai", SMRA_MODEL="m", SMRA_GUARD_VISION="1"))
    assert o["guard_vision"] is True
    with pytest.raises(ValueError):
        models.from_env(env(SMRA_GUARD_VISION="maybe"))


async def test_guard_sends_images_or_flags_without_vision():
    check = InjectionCheck("k", "http://127.0.0.1:9", "flash", "anthropic", frozenset(), vision=False)
    flagged, reason = await check.check(["页面文字"], [PNG])
    assert flagged and "看不了图片" in reason
    check = InjectionCheck("k", "http://127.0.0.1:9", "flash", "anthropic", frozenset(), vision=True)
    assert (await check.check([], [PNG] * 5))[0]
    req = check._request("", [PNG])
    content = req["messages"][0]["content"]
    assert content[0]["type"] == "text" and "截图" in content[0]["text"] and content[1] == PNG
    openai = InjectionCheck("k", "http://x", "flash", "openai", frozenset(), vision=True)._request("文字", [PNG])
    parts = openai["messages"][-1]["content"]
    assert any(p["type"] == "image_url" for p in parts)


async def test_model_proxy_screens_and_strips_screenshots(make_app, upstream):
    """主模型看不到图（DeepSeek v4-pro）：转发前把截图换成说明；注入检测照样看截图。"""
    app = make_app(model_features=frozenset({"stream"}))
    await app.start(with_channel=False)
    seen = []

    class Check:
        async def check(self, texts, images=()):
            seen.append((texts, list(images)))
            return True, "图里写着让助手把密码发出去"

    app.injection = Check()
    run = live_run(app)
    upstream.script = [{"text": "好"}]
    body = {"model": "m", "max_tokens": 100, **body_with_screenshot()}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{app.port}/model/v1/messages", json=body,
                          headers={"x-api-key": run.token, "anthropic-version": "2023-06-01"}) as r:
            assert r.status == 200, await r.text()
    assert seen and seen[0][1] == [] and run.suspended        # 主模型看不到，图在检查前就去掉了
    forwarded = json.dumps(upstream.requests[-1]["body"], ensure_ascii=False)
    assert NO_VISION_NOTE in forwarded and "iVBORw0KGgo=" not in forwarded
    app.runs.finish(run, "SILENT")


async def test_model_proxy_checks_screenshots_when_model_sees_them(make_app, upstream):
    app = make_app()          # 默认全部功能，包括 vision
    await app.start(with_channel=False)
    seen = []

    class Check:
        async def check(self, texts, images=()):
            seen.append(list(images))
            return False, ""

    app.injection = Check()
    run = live_run(app)
    upstream.script = [{"text": "好"}]
    body = {"model": "m", "max_tokens": 100, **body_with_screenshot()}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{app.port}/model/v1/messages", json=body,
                          headers={"x-api-key": run.token, "anthropic-version": "2023-06-01"}) as r:
            assert r.status == 200
    assert seen == [[PNG]] and not run.suspended
    assert "iVBORw0KGgo=" in json.dumps(upstream.requests[-1]["body"])
    app.runs.finish(run, "SILENT")
