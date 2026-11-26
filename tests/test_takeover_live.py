"""接管页面的实时画面：用真浏览器和本地页面，不调用模型。"""
from __future__ import annotations

import asyncio
import base64
import secrets
import time

import aiohttp
import pytest

from host.browser import LiveView, Takeover
from host.db import new_id
from tests.conftest import BROWSER_CHANNEL

pytestmark = pytest.mark.skipif(BROWSER_CHANNEL is None, reason="本机没有可用的浏览器")

JPEG = b"\xff\xd8"


@pytest.fixture
async def live_app(make_app):
    app = make_app()
    await app.start(with_channel=False)
    page = await app.browser._ensure()
    await page.set_content("<!doctype html><title>t</title><body style='background:#fff'><div id=box>0</div>")
    tk = Takeover(new_id("tk"), secrets.token_urlsafe(24), "s", "https://shop.example/login", "测试")
    app.browser.takeover = tk
    return app, page, f"http://127.0.0.1:{app.takeover_port}/takeover/{tk.key}"


async def frame(ws, timeout: float = 8) -> bytes:
    message = await ws.receive(timeout=timeout)
    while message.type == aiohttp.WSMsgType.TEXT and message.data in ("p", "a"):
        if message.data == "p":     # 服务器量延迟；a 表示下一帧是操作之后截的
            await ws.send_str("q")
        message = await ws.receive(timeout=timeout)
    assert message.type == aiohttp.WSMsgType.BINARY, message
    assert message.data.startswith(JPEG)
    return message.data


async def test_change_is_pushed_without_polling(live_app):
    app, page, link = live_app
    async with aiohttp.ClientSession() as s, s.ws_connect(link + "/live") as ws:
        await frame(ws)         # 静止页面也有第一帧
        await ws.send_str("ok")
        await asyncio.sleep(0.5)
        await page.evaluate("document.body.style.background = '#123456'")
        await frame(ws)
        await ws.send_str("ok")


async def test_no_new_frame_until_page_says_shown(live_app):
    app, page, link = live_app
    async with aiohttp.ClientSession() as s, s.ws_connect(link + "/live") as ws:
        await frame(ws)
        app.browser._live.touch()
        for i in range(5):      # 手机没回 ok，画面再变也不能堆过来
            await page.evaluate(f"document.getElementById('box').textContent = 'x{i}'")
            await asyncio.sleep(0.05)
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive(timeout=0.8)
        await ws.send_str("ok")
        await frame(ws)


async def test_caret_blink_is_not_streamed_but_typing_is():
    class FakeCdp:
        def on(self, _event, _handler): pass
        async def send(self, *_args): return {}

    class NoShot:       # 截图失败时退回浏览器推来的那一帧，这里正好能看出发的是哪张
        async def takeover_shot(self):
            raise RuntimeError("页面在跳转")

    view = LiveView(NoShot(), FakeCdp())
    view._first = False
    push = lambda i, body: view._on_frame({"sessionId": i, "data": base64.b64encode(body).decode()})
    push(1, b"a" * 3000)
    assert await view.next() == b"a" * 3000
    push(2, b"b" * 3000)                # 大小几乎一样、又没人操作：先压住
    assert not view._fresh.is_set()
    assert await view.next() is None
    view._last_sent = time.monotonic() - 5
    assert await view.next() == b"b" * 3000     # 到时间了，细小变化也不能丢
    view.touch()
    push(3, b"c" * 3000)                # 刚操作过：同样的小变化要立刻推
    assert await view.next() == b"c" * 3000
    push(4, b"d" * 6000)
    assert await view.next() == b"d" * 6000
    await view.close()


def test_picture_size_follows_measured_bandwidth():
    class FakeCdp:
        def on(self, _event, _handler): pass

    view = LiveView(None, FakeCdp())
    assert view._tier() == 0                    # 还没测过，先给清晰的
    view.note_rtt(0.2)
    for _ in range(6):
        view.delivered(47_000, 3.3)             # 约 15KB/s
    assert view._tier() == 2 and view._sharp_tier() == 0    # 操作时先发小图，停手后补最清晰的
    assert view._settle_wait() == 3             # 慢网多等一会，确认停手了再补
    assert view._quiet_gap() > 10               # 慢网下光标闪烁不能占住线路
    for _ in range(8):
        view.delivered(47_000, 8)               # 约 6KB/s：清晰图太慢，只补中等的
    assert view._sharp_tier() == 1
    for _ in range(12):
        view.delivered(47_000, 0.35)            # 网好了要升回去，不被延迟误判成慢
    assert view._tier() == 0 and view._sharp_tier() == 0


async def test_slow_page_gets_no_second_frame_before_ok(live_app):
    app, page, link = live_app
    async with aiohttp.ClientSession() as s, s.ws_connect(link + "/live") as ws:
        await frame(ws)
        app.browser._live.touch()
        await page.evaluate("document.getElementById('box').textContent = 'changed'")
        with pytest.raises(asyncio.TimeoutError):   # 一帧传了 3 秒多还没显示，不能再往线路里塞
            await ws.receive(timeout=3.6)
        await ws.send_str("ok")
        await frame(ws)


async def test_focus_ring_shows_before_any_new_frame(live_app):
    from playwright.async_api import async_playwright
    app, page, link = live_app
    await page.set_content("<!doctype html><body style='margin:0'><input id=a style='position:absolute;left:40px;top:120px;width:200px;height:40px'>")
    async with async_playwright() as pw:
        kwargs = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL != "chromium" else {}
        browser = await pw.chromium.launch(headless=True, **kwargs)
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        phone = await ctx.new_page()
        await phone.goto(link)
        async def until(expr: str, seconds: float) -> None:     # 页面的 CSP 不允许 wait_for_function
            end = time.monotonic() + seconds
            while not await phone.evaluate(expr):
                assert time.monotonic() < end, expr
                await asyncio.sleep(0.05)

        await until("document.getElementById('shot').naturalWidth > 0", 15)
        await asyncio.sleep(0.8)
        await phone.evaluate("WebSocket.prototype.send = function(){}")     # 手机不再确认，后面不会有新帧
        box = await page.locator("#a").bounding_box()
        at = await phone.evaluate("""([x, y]) => { const r = document.getElementById('shot').getBoundingClientRect();
            return [r.left + x * r.width / vw, r.top + y * r.height / vh]; }""", [box["x"] + 30, box["y"] + 20])
        await phone.mouse.click(at[0], at[1])
        await until("!document.getElementById('ring').hidden", 4)
        drawn, shot = await phone.evaluate("""() => { const g = document.getElementById('ring').getBoundingClientRect(),
            r = document.getElementById('shot').getBoundingClientRect();
            return [{x: g.left, w: g.width}, {x: r.left, k: r.width / vw}]; }""")
        assert abs(drawn["x"] - (shot["x"] + box["x"] * shot["k"] - 2)) < 2
        assert abs(drawn["w"] - (box["width"] * shot["k"] + 4)) < 2
        await browser.close()


async def test_focus_ring_leaves_when_real_picture_arrives(live_app):
    from playwright.async_api import async_playwright
    app, page, link = live_app
    await page.set_content("<!doctype html><body style='margin:0'><input id=a style='position:absolute;left:40px;top:120px;width:200px;height:40px'>")
    async with async_playwright() as pw:
        kwargs = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL != "chromium" else {}
        browser = await pw.chromium.launch(headless=True, **kwargs)
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        phone = await ctx.new_page()
        await phone.goto(link)
        end = time.monotonic() + 15
        while not await phone.evaluate("document.getElementById('shot').naturalWidth > 0 && liveOn"):
            assert time.monotonic() < end
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.8)
        box = await page.locator("#a").bounding_box()
        at = await phone.evaluate("""([x, y]) => { const r = document.getElementById('shot').getBoundingClientRect();
            return [r.left + x * r.width / vw, r.top + y * r.height / vh]; }""", [box["x"] + 30, box["y"] + 20])
        await phone.mouse.click(at[0], at[1])
        end = time.monotonic() + 5
        while await page.evaluate("document.activeElement.id") != "a" or not await phone.evaluate("freshAt >= inputStart && inputStart > 0"):
            assert time.monotonic() < end
            await asyncio.sleep(0.05)
        for _ in range(3):                  # 之后每次查状态，都不能把绿框重新画出来
            await phone.evaluate("state()")
            await asyncio.sleep(0.25)
            assert await phone.evaluate("document.getElementById('ring').hidden")
        await browser.close()


async def test_phone_can_delete_what_was_typed(live_app):
    from playwright.async_api import async_playwright
    app, page, link = live_app
    await page.set_content("<!doctype html><input id=a>")
    await page.focus("#a")
    await page.keyboard.type("abcdefghijkl")

    async def value_is(check, seconds: float = 5) -> str:
        end = time.monotonic() + seconds
        while not check(value := await page.input_value("#a")):
            assert time.monotonic() < end, value
            await asyncio.sleep(0.05)
        return value

    async with async_playwright() as pw:
        kwargs = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL != "chromium" else {}
        browser = await pw.chromium.launch(headless=True, **kwargs)
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        phone = await ctx.new_page()
        await phone.goto(link)
        await phone.locator("#text").focus()
        box = await phone.locator("#del").bounding_box()
        assert await phone.evaluate("document.documentElement.scrollWidth <= innerWidth")   # 多一个按钮也不能撑破页面
        await phone.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        await phone.mouse.down()            # 点一下删一个
        await phone.mouse.up()
        await value_is(lambda v: v == "abcdefghijk")
        assert await phone.evaluate("document.activeElement.id") == "text"  # 键盘不收起

        await phone.keyboard.press("Backspace")     # 下方输入框是空的：键盘退格删页面里的字
        await value_is(lambda v: v == "abcdefghij")

        await phone.fill("#text", "xy")     # 下方有字时，退格只改下方，不碰页面
        await phone.keyboard.press("Backspace")
        assert await phone.input_value("#text") == "x"
        await asyncio.sleep(0.4)
        assert await page.input_value("#a") == "abcdefghij"
        await phone.fill("#text", "")

        await phone.mouse.down()            # 长按连续删，松手就停
        await value_is(lambda v: len(v) <= 5)
        await phone.mouse.up()
        await asyncio.sleep(0.5)
        left = await page.input_value("#a")
        await asyncio.sleep(0.5)
        assert await page.input_value("#a") == left
        await browser.close()


async def test_back_never_lands_on_blank_page(live_app):
    app, page, link = live_app
    login = "https://shop.example/login"
    await page.route("https://shop.example/**", lambda r: r.fulfill(content_type="text/html", body="<!doctype html><title>x</title>"))
    await page.goto(login)

    async def url_becomes(expected: str) -> None:
        end = time.monotonic() + 5
        while page.url != expected:
            assert time.monotonic() < end, page.url
            await asyncio.sleep(0.05)

    async with aiohttp.ClientSession() as s:
        async with s.post(link + "/input", json={"type": "back"}) as r:     # 登录页就是第一页
            assert r.status == 400 and (await r.json())["error"] == "已经是第一页"
        assert page.url == login

        await page.goto("https://shop.example/next")
        async with s.post(link + "/input", json={"type": "back"}) as r:     # 有上一页就正常后退
            assert r.status == 200
        await url_becomes(login)

        await page.goto("about:blank")      # 已经在空白页：回到要登录的页面，不会卡死
        async with s.post(link + "/input", json={"type": "back"}) as r:
            assert r.status == 200
        await url_becomes(login)


async def test_foreign_origin_and_wrong_key_refused(live_app):
    _, _, link = live_app
    async with aiohttp.ClientSession() as s:
        async with s.get(link + "/live", headers={"Origin": "https://evil.example", "Connection": "Upgrade",
                                                  "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                                                  "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}) as r:
            assert r.status == 403
        async with s.get(link[:-3] + "xyz/live") as r:
            assert r.status == 404


async def test_stream_closes_when_takeover_ends(live_app):
    app, _, link = live_app
    async with aiohttp.ClientSession() as s, s.ws_connect(link + "/live") as ws:
        await frame(ws)
        await ws.send_str("ok")
        app.browser.finish_takeover(app.browser.takeover)
        message = await ws.receive(timeout=5)
        assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.close_code == 4404 or message.data == 4404
    assert app.browser._live is None


async def test_second_page_takes_over_stream(live_app):
    app, _, link = live_app
    async with aiohttp.ClientSession() as s, s.ws_connect(link + "/live") as first:
        await frame(first)
        await first.send_str("ok")
        async with s.ws_connect(link + "/live") as second:
            await frame(second)
            message = await first.receive(timeout=5)
            assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            assert message.data == 4409 or first.close_code == 4409
