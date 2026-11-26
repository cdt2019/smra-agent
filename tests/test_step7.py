"""第 7 步：浏览器。

用一个真的浏览器（本机的 Edge；没有 Edge 就用 playwright install chromium 下的 Chromium，经 Playwright 驱动）
和一个假的网店（HTTPS，自签证书）。
浏览器的流量全部经过出网代理；登录由"你"在接管页面上完成。

验收（设计文档第 7 步）：
- 场景 S9 跑通："去这个没有接口的网站查一下我的订单状态"；
- 电脑读不到浏览器的配置目录；
- 已登录站点上的提交类请求全部经过裁决。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp
import cell_tools as ct
import pytest
from aiohttp import web

from host.browser import number_tree
from tests import gridvault_login
from tests.conftest import BROWSER_CHANNEL, all_done, self_signed_context, wait_until

PASSWORD = "PW-s3cret-9f2c"

pytestmark = pytest.mark.skipif(BROWSER_CHANNEL is None,
                                reason="本机既没有 Edge 也没有 Playwright 的 Chromium（uv run playwright install chromium），跳过真实浏览器的测试")


class Shop:
    """没有接口的网店。订单页要登录；取消订单是一个普通的 POST 表单。"""

    def __init__(self) -> None:
        self.port = 0
        self.cancelled = 0
        self.beacons = 0
        self.logins: list[dict] = []
        self.searches: list[str] = []

    def _page(self, title: str, body: str) -> web.Response:
        return web.Response(text=f"<!doctype html><html><head><meta charset=utf-8>"
                                 f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title>"
                                 f"<link rel=stylesheet href=/style.css></head><body>{body}</body></html>",
                            content_type="text/html")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path, method = request.path, request.method
        logged_in = request.cookies.get("session") == "ok"
        if path == "/style.css":
            return web.Response(text=gridvault_login.CSS, content_type="text/css")
        if path == "/" and method == "GET":
            # 页面加载 2 秒后自己往回发一个统计请求：Agent 没在操作，它不该变成审批
            return self._page("小店", "<h1>小店</h1><a href=/orders>我的订单</a>"
                                      "<form action=/search method=get><input name=q aria-label=搜索><button>搜</button></form>"
                                      "<script>setTimeout(()=>fetch('/beacon',{method:'POST',body:'x'}),2000)</script>")
        if path == "/tracked":
            # 像格物仓：页面一加载就往自己站点发一个埋点 POST，这时 Agent 的"打开"动作还没结束
            return self._page("埋点页", "<h1>埋点页</h1>"
                                        "<script>fetch('/beacon',{method:'POST',body:'{\"ev\":\"view\"}'})</script>")
        if path == "/many" or path.startswith("/s/"):
            # 像打包过的单页应用：一次引用几十个脚本，浏览器会并发去取
            status, ctype, body = _many_files(path)
            return web.Response(status=status, body=body, headers={"content-type": ctype})
        if path == "/beacon":
            self.beacons += 1
            return web.Response(text="ok")
        if path == "/search":
            self.searches.append(request.query.get("q", ""))
            return self._page("搜索", f"<p>搜索结果：{request.query.get('q', '')}</p>")
        if path == "/login" and method == "GET":
            return self._page("登录 · 格物仓", gridvault_login.login_body())
        if path == "/login" and method == "POST":
            form = dict(await request.post())
            self.logins.append(form)
            if form.get("user") == "alice" and form.get("password") == PASSWORD:
                response = web.HTTPSeeOther("/orders")
                response.set_cookie("session", "ok", secure=True, httponly=True)
                raise response
            return self._page("登录 · 格物仓", gridvault_login.login_body(error=True))
        if path == "/orders":
            if not logged_in:
                raise web.HTTPFound("/login")
            state = "已取消" if self.cancelled else "已发货，预计明天送达"
            return self._page("我的订单", f"<h1>我的订单</h1><p>订单 A1001：{state}</p>"
                                          "<form action=/orders/A1001/cancel method=post>"
                                          "<input type=hidden name=reason value=不想要了>"
                                          "<button>取消订单</button></form>")
        if path == "/orders/A1001/cancel" and method == "POST":
            if logged_in:
                self.cancelled += 1
            raise web.HTTPSeeOther("/orders")
        return web.Response(status=404, text="not found")


@pytest.fixture
async def shop(tmp_path):
    fake = Shop()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=self_signed_context("shop.example", tmp_path))
    await site.start()
    fake.port = site._server.sockets[0].getsockname()[1]
    yield fake
    await runner.cleanup()


@pytest.fixture
async def shop_app(make_app, shop):
    app = make_app(egress_overrides={"shop.example": ("127.0.0.1", shop.port)}, egress_ssl_insecure=True)
    await app.start(with_channel=False)
    return app


def last_tool_text(body: dict) -> str:
    content = body["messages"][-1]["content"]
    out = []
    for block in content if isinstance(content, list) else []:
        if block.get("type") == "tool_result":
            inner = block.get("content")
            out.append(inner if isinstance(inner, str) else
                       "".join(b.get("text", "") for b in inner if b.get("type") == "text"))
    return "\n".join(out)


def ref_of(text: str, role: str, name: str) -> str:
    m = re.search(rf'- {role} "{re.escape(name)}" \[ref=(e\d+)\]', text)
    assert m, f"页面里没有 {role} {name}：\n{text}"
    return m.group(1)


def egress(app, *, verdict=None, like="") -> list:
    sql = "SELECT method, url, verdict, reason FROM egress_log WHERE url LIKE ?"
    rows = app.db.execute(sql, (f"%{like}%",)).fetchall()
    return [dict(r) for r in rows if verdict is None or r["verdict"] == verdict]


async def user_logs_in(app) -> None:
    """你在接管页面上登录：看截图、点输入框、打字、回车，最后点"完成"。"""
    body = next(text for text in app.channel.sent if "/takeover/" in text)
    link = re.search(r"(http://\S+/takeover/[\w-]+)", body).group(1)
    page = app.browser._page        # 只用来算输入框在截图上的坐标，相当于你看着截图找到它
    await wait_until(lambda: page.url.endswith("/login"), 30)
    await page.wait_for_selector("#user")
    async with aiohttp.ClientSession() as s:
        async with s.get(link) as r:
            assert r.status == 200 and "接管浏览器" in await r.text()
        async with s.get(link + "/shot") as r:
            assert r.status == 200 and r.content_type == "image/jpeg"

        async def send(payload):
            async with s.post(link + "/input", json=payload) as r:
                assert (await r.json())["ok"], payload

        async with s.post(link + "/done") as r:          # 还在登录页就点完成：只提醒，不结束
            assert (await r.json()).get("confirm") == "still_login"
        assert app.browser.takeover is not None

        for selector, text in (("#user", "alice"), ("#pw", PASSWORD)):
            box = await page.locator(selector).bounding_box()
            await send({"type": "click", "x": box["x"] + 5, "y": box["y"] + 5})
            await send({"type": "text", "text": text})
        await send({"type": "key", "key": "Enter"})
        await wait_until(lambda: page.url.endswith("/orders"), 30)
        async with s.post(link + "/done") as r:
            assert r.status == 200 and (await r.json()).get("done")
        async with s.get(link) as r:
            assert r.status == 404          # 接管结束，链接作废


# ------------------------------------------------------------------ 手机接管

async def phone_until(page, expression: str, timeout: float = 15) -> None:
    """接管页面的 CSP 禁了 eval，Playwright 的 wait_for_function 用不了，这里轮询。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not await page.evaluate(expression):
        assert asyncio.get_running_loop().time() < deadline, f"等待超时：{expression}"
        await asyncio.sleep(0.1)


async def test_phone_takeover_login_by_touch(shop_app, shop, upstream, tmp_path):
    """用手机视口打开接管页：点按、滑动、遮住密码、提示跳转，登录后 Agent 的浏览器尺寸恢复。"""
    from playwright.async_api import async_playwright

    from host.browser import VIEWPORT

    app = shop_app
    upstream.script = [
        {"tools": [("browser", {"action": "login", "url": "https://shop.example/login", "reason": "订单页要登录"})]},
        {"text": "等你登录。"},
        {"tools": [("browser", {"action": "snapshot"})]},
        {"text": "登录好了。"},
    ]
    await app.on_user_text("m1", "登录网店")
    await wait_until(lambda: any("/takeover/" in t for t in app.channel.sent), 60)
    await wait_until(lambda: all_done(app), 60)
    link = re.search(r"(http://\S+/takeover/[\w-]+)", next(t for t in app.channel.sent if "/takeover/" in t)).group(1)
    remote = app.browser._page
    await remote.wait_for_selector("#user")
    await remote.evaluate("document.body.style.minHeight = '3000px'")

    async with async_playwright() as pw:
        kwargs = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL != "chromium" else {}
        phone_browser = await pw.chromium.launch(headless=True, **kwargs)
        ctx = await phone_browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                              is_mobile=True, has_touch=True, locale="zh-CN")
        phone = await ctx.new_page()
        errors: list[str] = []
        phone.on("pageerror", lambda e: errors.append(str(e)))
        await phone.goto(link)
        await phone_until(phone, "document.getElementById('shot').naturalWidth > 0")
        stage_width = await phone.evaluate("document.getElementById('stage').clientWidth")
        await wait_until(lambda: (remote.viewport_size or {}).get("width") == stage_width, 10)
        await phone_until(phone, f"document.getElementById('shot').naturalWidth === {stage_width * 2}")  # 两倍清晰度
        await phone_until(phone, "document.getElementById('host').textContent === 'shop.example'")
        assert await phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
        heights = await phone.eval_on_selector_all("#dock button, header button",
                                                   "els => els.filter(e => e.offsetParent).map(e => e.offsetHeight)")
        assert heights and min(heights) >= 44

        async def tap_remote(selector: str) -> None:
            box = await remote.locator(selector).bounding_box()
            rect = await phone.evaluate("(() => { const r = document.getElementById('shot').getBoundingClientRect();"
                                        " return {x: r.left, y: r.top, w: r.width, h: r.height}; })()")
            size = remote.viewport_size
            await phone.touchscreen.tap(rect["x"] + (box["x"] + box["width"] / 2) * rect["w"] / size["width"],
                                        rect["y"] + (box["y"] + box["height"] / 2) * rect["h"] / size["height"])

        # 手指上滑，远端页面往下翻；再滑回来
        cdp = await ctx.new_cdp_session(phone)

        async def swipe(y0: int, y1: int) -> None:
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": 195, "y": y0}]})
            for i in range(1, 7):
                await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints":
                               [{"x": 195, "y": y0 + (y1 - y0) * i / 6}]})
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

        await swipe(500, 250)
        await remote.wait_for_function("scrollY > 50")
        await swipe(200, 700)
        await remote.wait_for_function("scrollY === 0")

        await tap_remote("#user")
        await remote.wait_for_function("document.activeElement.id === 'user'")
        await phone_until(phone, "document.getElementById('text').type === 'text'")
        # 登录表单的账号框、密码框：手机的密码管理器可以填
        await phone_until(phone, "document.getElementById('text').autocomplete === 'username'")
        assert not await phone.evaluate("document.getElementById('text').hasAttribute('data-1p-ignore')")
        await phone.fill("#text", "alice")
        await phone.tap("#send")
        await remote.wait_for_function("document.getElementById('user').value === 'alice'")
        assert await phone.input_value("#text") == ""
        await phone_until(phone, "document.getElementById('toast').textContent === '已输入：alice'")

        await tap_remote("#pw")
        await remote.wait_for_function("document.activeElement.id === 'pw'")
        await phone_until(phone, "document.getElementById('text').type === 'password'")
        await phone_until(phone, "document.getElementById('text').autocomplete === 'current-password'")
        await phone.fill("#text", PASSWORD)
        if shot_to := os.environ.get("SMRA_PHONE_SHOT"):
            await asyncio.sleep(1.8)        # 等画面刷新到最新
            await phone.screenshot(path=shot_to)
        await phone.tap("#send")
        await remote.wait_for_function("document.getElementById('pw').value.length > 0")
        assert await phone.input_value("#text") == ""
        await phone_until(phone, "document.getElementById('toast').textContent === '已输入到页面'")   # 密码不回显
        assert PASSWORD not in await phone.content()

        # 页面离开了要登录的网站，顶部要有提醒
        tk = app.browser.takeover
        original, tk.url = tk.url, "https://other.example/login"
        await phone.wait_for_selector("#offsite.on", timeout=6000)
        # 跳到别的网站：密码管理器认不出来，关掉自动填充
        await phone_until(phone, "document.getElementById('text').autocomplete === 'off'")
        tk.url = original
        await phone.wait_for_selector("#offsite:not(.on)", state="attached", timeout=6000)

        # 填完还没提交就按"登录好了"：页面上还有密码框，先问一句，不交还
        await phone.tap("#done")
        await phone.wait_for_selector("#unsure[open]")
        assert app.browser.takeover is tk
        await phone.tap("#stay")
        await phone.wait_for_selector("#unsure:not([open])", state="attached")
        await phone.tap("#done")
        await phone.wait_for_selector("#unsure[open]")
        await phone.tap("#submit-login")          # 弹窗里直接提交：在页面上按回车
        await wait_until(lambda: remote.url.endswith("/orders"), 30)
        assert app.browser.takeover is tk
        await phone.tap("#done")
        await phone.wait_for_selector("#end.on")
        assert await phone.text_content("#end-title") == "已交还给 Agent"
        assert errors == []
        await phone_browser.close()

    assert shop.logins[-1] == {"user": "alice", "password": PASSWORD}
    await wait_until(lambda: remote.viewport_size == VIEWPORT, 60)
    await wait_until(lambda: all_done(app), 60)
    assert not any(PASSWORD in json.dumps(r["body"], ensure_ascii=False) for r in upstream.requests)


# ------------------------------------------------------------------ 纯函数

def test_number_tree_gives_refs_to_interactive_nodes():
    tree = ('- heading "订单" [level=1]\n- textbox "用户名"\n- button "登录"\n'
            '- link "订单 \\"1\\"":\n  - /url: /x\n- button "登录"\n- textbox')
    numbered, refs = number_tree(tree)
    assert '- textbox "用户名" [ref=e1]' in numbered
    assert '- link "订单 \\"1\\"" [ref=e3]:' in numbered
    assert "heading" in numbered and "[level=1]" in numbered and "ref=e0" not in numbered
    assert refs["e2"].role == "button" and refs["e2"].nth == 0
    assert refs["e4"].nth == 1                          # 第二个同名按钮
    assert refs["e3"].name == '订单 "1"'
    assert refs["e5"].name is None and refs["e5"].nth == 1   # 没名字的：同角色里的第二个


# ------------------------------------------------------------------ S9

async def test_s9_order_status_through_takeover_login(shop_app, shop, upstream):
    """S9：一句话 → 打开订单页被转去登录 → 请你接管登录 → 接着查到状态 → 取消订单要你批准。"""
    app = shop_app

    def after_snapshot(body):
        text = last_tool_text(body)
        assert "已发货" in text, text
        return {"tools": [("browser", {"action": "click", "ref": ref_of(text, "button", "取消订单")})]}

    def after_approval(body):
        text = last_tool_text(body)
        return {"tools": [("browser", {"action": "click", "ref": ref_of(text, "button", "取消订单")})]}

    upstream.script = [
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/orders"})]},
        lambda body: (
            {"tools": [("browser", {"action": "login", "url": "https://shop.example/login",
                                    "reason": "订单页要登录"})]}
            if "/login" in last_tool_text(body) else {"text": "没被转到登录页？"}),
        {"text": "需要你登录一下网店，我在等你。"},
        # 你登录完：[浏览器] 消息叫醒它
        {"tools": [("browser", {"action": "snapshot"})]},
        after_snapshot,
        # 取消订单被拦下等批准，运行挂起。你批准后：
        {"tools": [("browser", {"action": "snapshot"})]},
        after_approval,
        {"text": "订单 A1001 已经取消了。"},
    ]
    await ask_without_naming(app, "m1", "去那家网店查一下我的订单状态", "https://shop.example/orders")
    await wait_until(lambda: any("/takeover/" in t for t in app.channel.sent), 60)
    await wait_until(lambda: all_done(app), 60)
    assert "需要你登录一下网店，我在等你。" in app.channel.sent

    await user_logs_in(app)
    assert shop.logins and shop.logins[-1]["user"] == "alice"
    assert egress(app, verdict="allow", like="/login") and \
        any(r["reason"] == "浏览器：你在接管" for r in egress(app, like="/login"))

    # 取消订单是已登录站点上的提交：要你批准，表单字段和登录态都写在审批里
    await wait_until(lambda: app.channel.approvals, 60)
    await wait_until(lambda: all_done(app), 60)
    text, approval_id, scopes = app.channel.approvals[-1]
    assert "POST https://shop.example/orders/A1001/cancel" in text
    assert "reason = 不想要了" in text and "登录状态" in text
    assert scopes == ["once"]
    assert shop.cancelled == 0
    assert egress(app, verdict="ask", like="/cancel")
    # 订单状态已经查到了：Agent 看到的工具结果里有它
    assert any("已发货" in json.dumps(r["body"], ensure_ascii=False) for r in upstream.requests)

    await app.on_user_text("m2", f"/approve {approval_id}")
    await wait_until(lambda: "订单 A1001 已经取消了。" in app.channel.sent, 60)
    assert shop.cancelled == 1

    # 密码只经过接管页面和浏览器：模型请求、会话记录、电脑主目录里都没有
    assert all(PASSWORD not in json.dumps(r["body"], ensure_ascii=False) for r in upstream.requests)
    assert not app.db.execute("SELECT 1 FROM inference_log WHERE request LIKE ?", (f"%{PASSWORD}%",)).fetchone()
    assert not app.db.execute("SELECT 1 FROM event WHERE payload LIKE ?", (f"%{PASSWORD}%",)).fetchone()
    home = app.computer.home
    assert all(PASSWORD.encode() not in p.read_bytes() for p in Path(home).rglob("*") if p.is_file())
    # Cookie 也不会出现在 Agent 那边
    assert all("session=ok" not in json.dumps(r["body"]) for r in upstream.requests)


class _SeenCheck:
    def __init__(self):
        self.seen: list[list[str]] = []

    async def check(self, texts):
        self.seen.append(texts)
        return False, ""


async def test_takeover_left_open_new_conversation_and_host_text_not_screened(shop_app, shop, upstream):
    """请求登录后你没登录：Agent 再用浏览器时把链接再发一遍；/new 结束没完成的接管。
    宿主自己写的说明（"这一轮到此结束……先 snapshot"）不送去查注入，以前会被当成网页里的指令。"""
    app = shop_app
    app.injection = _SeenCheck()
    login = {"tools": [("browser", {"action": "login", "url": "https://shop.example/login", "reason": "要登录"})]}
    upstream.script = [login, {"text": "等你登录。"},
                       {"tools": [("browser", {"action": "snapshot"})]}, {"text": "还在等你登录。"},
                       login, {"text": "新对话里等你登录。"}]
    await app.on_user_text("m1", "去 https://shop.example/orders 买点东西")
    await wait_until(lambda: "等你登录。" in app.channel.sent, 60)
    await wait_until(lambda: all_done(app), 60)
    seen = [t for texts in app.injection.seen for t in texts]
    assert not any("这一轮到此结束" in t for t in seen), seen

    # 接管还开着：同一对话里 Agent 再动浏览器，链接再发你一遍
    first = next(t for t in app.channel.sent if "/takeover/" in t)
    await app.on_user_text("m2", "怎么样了")
    await wait_until(lambda: "还在等你登录。" in app.channel.sent, 60)
    again = [t for t in app.channel.sent if "还在等你登录 shop.example" in t]
    assert again and first.split("/takeover/")[1].split()[0] in again[0]
    assert not any("接管链接已经再发" in t for texts in app.injection.seen for t in texts)

    # /new：没完成的接管结束，新对话里能重新请求登录、拿到新链接
    await app.on_user_text("m3", "/new")
    await wait_until(lambda: any("之前没完成的浏览器登录也结束了" in t for t in app.channel.sent), 10)
    assert app.browser.takeover is None
    await app.on_user_text("m4", "去 https://shop.example/orders 买点东西")
    await wait_until(lambda: "新对话里等你登录。" in app.channel.sent, 60)
    links = {t.split("/takeover/")[1].split()[0] for t in app.channel.sent if "打开接管页面" in t}
    assert len(links) == 2 and app.browser.takeover is not None


# ------------------------------------------------------------------ 隔离和规则

async def test_profile_dir_is_outside_the_computer(shop_app, upstream):
    app = shop_app
    upstream.script = [{"tools": [("browser", {"action": "open", "url": "https://shop.example/"})]},
                       {"text": "好了"}]
    await app.on_user_text("m1", "打开 https://shop.example/ 看看")
    await wait_until(lambda: "好了" in app.channel.sent, 60)
    if app.cfg.browser == "docker":     # 浏览器在容器里：登录状态在宿主的一个文件里
        profile = app.browser.state_path
        assert profile.exists() and json.loads(profile.read_text(encoding="utf-8"))["cookies"] is not None
    else:
        profile = app.browser.profile_dir
        assert profile.exists() and any(profile.iterdir())
    home = ct.Home(Path(app.computer.home))
    with pytest.raises(ct.ToolError):
        home.resolve(str(profile))
    with pytest.raises(ct.ToolError):
        home.resolve(f"../browser/{profile.name}")
    # 浏览器工具没有执行脚本、读 Cookie 之类的动作
    actions = ct.BROWSER_TOOL["input_schema"]["properties"]["action"]["enum"]
    assert set(actions) == {"open", "snapshot", "click", "type", "press", "back", "screenshot", "login"}


@pytest.mark.skipif(os.environ.get("SMRA_TEST_BROWSER") != "docker",
                    reason="容器里的浏览器：SMRA_TEST_BROWSER=docker SMRA_DOCKER_PREFIX=smratest 才跑（Linux，有 docker）")
async def test_browser_in_container_keeps_login_and_has_no_route_out(shop_app, shop, upstream):
    """浏览器在容器里：容器自己上不了网；容器删掉重建，登录还在（登录状态存在宿主上）。"""
    import subprocess
    app = shop_app
    upstream.script = [
        {"tools": [("browser", {"action": "login", "url": "https://shop.example/login", "reason": "订单页要登录"})]},
        {"text": "等你登录。"},
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/orders"})]},
        {"text": "看到了。"},
    ]
    await app.on_user_text("m1", "去 https://shop.example/orders 看看")
    await wait_until(lambda: "等你登录。" in app.channel.sent, 60)
    await wait_until(lambda: all_done(app), 60)
    await user_logs_in(app)
    await wait_until(lambda: "看到了。" in app.channel.sent, 60)

    container = app.browser._box.container
    probe = "import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=3)"
    r = subprocess.run(["docker", "exec", container, "python", "-c", probe], capture_output=True)
    assert r.returncode != 0, "浏览器容器不该有通往外部的路"

    await app.browser.close()
    assert subprocess.run(["docker", "inspect", container], capture_output=True).returncode != 0
    upstream.script = [
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/orders"})]},
        lambda body: {"text": "还登录着" if "我的订单" in last_tool_text(body) else "掉登录了"},
    ]
    await app.on_user_text("m2", "再看看 https://shop.example/orders")
    await wait_until(lambda: {"还登录着", "掉登录了"} & set(app.channel.sent), 90)
    assert "还登录着" in app.channel.sent
    assert len(shop.logins) == 1


async def test_browser_recording_is_written_when_the_browser_closes(make_app, shop, upstream, tmp_path):
    """打开录像开关后，Agent 的浏览器录成视频，关浏览器（宿主停止）时写完。默认不录。"""
    from playwright.async_api import Error, async_playwright
    video = tmp_path / "video"
    app = make_app(egress_overrides={"shop.example": ("127.0.0.1", shop.port)}, egress_ssl_insecure=True,
                   browser_record_dir=str(video))
    channel = app.cfg.browser_channel
    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(tmp_path / "probe"), record_video_dir=str(tmp_path / "probe-v"),
                **({} if channel == "chromium" else {"channel": channel}))
            await ctx.close()
        except Error as e:
            if "ffmpeg" in str(e):
                pytest.skip("没装 Playwright 的 ffmpeg（playwright install ffmpeg）")
            raise
    await app.start(with_channel=False)
    upstream.script = [{"tools": [("browser", {"action": "open", "url": "https://shop.example/"})]},
                       {"text": "好了"}]
    await app.on_user_text("m1", "打开 https://shop.example/ 看看")
    await wait_until(lambda: "好了" in app.channel.sent, 60)
    await app.browser.close()
    files = list(video.glob("*.webm"))
    assert files and all(f.stat().st_size > 0 for f in files)


def test_recording_is_off_by_default(monkeypatch):
    from host.config import Config
    monkeypatch.delenv("SMRA_BROWSER_RECORD_DIR", raising=False)
    assert Config.from_env().browser_record_dir == ""
    monkeypatch.setenv("SMRA_BROWSER_RECORD_DIR", "/tmp/rec")
    assert Config.from_env().browser_record_dir == "/tmp/rec"


async def test_site_you_named_needs_no_approvals(shop_app, shop, upstream):
    """你在消息里给了网站：登录之后，Agent 在这个网站上点出来的提交、页面自己的后台请求、它拼的地址都不问你。"""
    app = shop_app

    def cancel(body):
        text = last_tool_text(body)
        assert "已发货" in text, text
        return {"tools": [("browser", {"action": "click", "ref": ref_of(text, "button", "取消订单")})]}

    upstream.script = [
        {"tools": [("browser", {"action": "login", "url": "https://shop.example/login", "reason": "订单页要登录"})]},
        {"text": "等你登录。"},
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/orders"})]},
        cancel,
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/secret-page"})]},
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/tracked"})]},
        {"text": "订单取消了。"},
    ]
    await app.on_user_text("m1", "去 https://shop.example/orders 把订单取消了")
    await wait_until(lambda: "等你登录。" in app.channel.sent, 60)
    await wait_until(lambda: all_done(app), 60)
    await user_logs_in(app)
    await wait_until(lambda: "订单取消了。" in app.channel.sent, 60)
    assert shop.cancelled == 1
    assert not app.channel.approvals
    assert any(r["reason"].startswith("浏览器：你点名的网站") for r in egress(app, like="/cancel"))
    assert egress(app, verdict="allow", like="/secret-page")
    # 页面加载时自己发的后台请求也放行（格物仓的付款方式就是这样加载的）
    assert shop.beacons > 0


async def test_named_site_asks_again_after_suspected_injection(shop_app, shop, upstream):
    """这次运行读到疑似注入的内容后，点名的网站也退回逐个问你。"""
    app = shop_app

    class Flag:
        async def check(self, texts):
            return True, "测试"

    app.injection = Flag()
    upstream.script = [
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/"})]},
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/secret-page"})]},
        {"text": "要你批准"},
    ]
    await app.on_user_text("m1", "打开 https://shop.example/ 看看")
    await wait_until(lambda: app.channel.approvals, 60)
    assert "secret-page" in app.channel.approvals[-1][0]


async def ask_without_naming(app, source_id: str, text: str, url: str) -> None:
    """地址算"出现过"（比如在搜索结果里），但你的消息里没点名这个网站：走严格的规则。"""
    app.adjudicator.add_seen([url], "search")
    await app.on_user_text(source_id, text)


async def test_browser_rules(shop_app, shop, upstream):
    """（没点名的网站）同站资源直接放行；页面后台的提交直接拦下、不打扰你；用 GET 表单提交 Agent 填的内容要批准。"""
    app = shop_app

    def search(body):
        text = last_tool_text(body)
        return {"tools": [("browser", {"action": "type", "ref": ref_of(text, "textbox", "搜索"),
                                       "text": "我的手机号 13800000000", "submit": True})]}

    upstream.script = [
        {"tools": [("browser", {"action": "open", "url": "https://shop.example/"})]},
        {"text": "打开了"},
    ]
    await ask_without_naming(app, "m1", "打开搜索结果里的那家店看看", "https://shop.example/")
    await wait_until(lambda: "打开了" in app.channel.sent, 60)
    assert egress(app, verdict="allow", like="/style.css")
    # 页面 2 秒后自己发的统计请求：Agent 早就不在操作了
    await wait_until(lambda: egress(app, like="/beacon"), 30)
    assert shop.beacons == 0
    beacon = egress(app, like="/beacon")[0]
    # 运行已经结束就是"没有运行在用"；还在同一次运行里则是"页面在后台自己发的提交"。都直接拦下
    assert beacon["verdict"] == "deny" and ("后台" in beacon["reason"] or "没有运行在用" in beacon["reason"])
    assert not app.channel.approvals

    # Agent 没操作时，浏览器不属于任何运行：页面自己再发什么都拦下
    assert app.browser.current_run() is None

    upstream.script = [
        {"tools": [("browser", {"action": "snapshot"})]},
        search,
        {"text": "搜索要你批准"},
    ]
    await app.on_user_text("m2", "在这个店里搜一下我的手机号")
    await wait_until(lambda: app.channel.approvals, 60)
    await wait_until(lambda: all_done(app), 60)
    text, _, scopes = app.channel.approvals[-1]
    assert "shop.example/search" in text and "13800000000" in text
    assert not shop.searches

    # 打开一个没出现过的地址也要批准
    upstream.script = [{"tools": [("browser", {"action": "open", "url": "https://shop.example/secret-page"})]},
                       {"text": "要批准"}]
    before = len(app.channel.approvals)
    await app.on_user_text("m3", "打开那个店的隐藏页面")
    await wait_until(lambda: len(app.channel.approvals) > before, 60)
    assert "secret-page" in app.channel.approvals[-1][0]


async def test_page_load_beacon_during_open_is_blocked_quietly(shop_app, shop, upstream):
    """打开页面时，页面脚本立刻发的同站埋点 POST：Agent 没往页面里填过东西，直接拦下，不发审批、不打断运行。"""
    app = shop_app
    seen = []

    def after_open(body):
        seen.append(last_tool_text(body))
        return {"text": "打开了"}

    upstream.script = [{"tools": [("browser", {"action": "open", "url": "https://shop.example/tracked"})]},
                       after_open]
    await ask_without_naming(app, "m1", "打开那个页面", "https://shop.example/tracked")
    await wait_until(lambda: "打开了" in app.channel.sent, 60)
    beacon = egress(app, like="/beacon")
    assert beacon and beacon[0]["verdict"] == "deny" and "页面加载时" in beacon[0]["reason"]
    assert shop.beacons == 0
    assert not app.channel.approvals
    # 拦是悄悄拦，但要告诉 Agent：拦了什么、为什么、别去绕、办不成就请你把网址发来
    text = seen[0]
    assert "被安全规则拦下了" in text and "POST shop.example/beacon" in text and "页面加载时" in text
    assert "不要用 fetch、curl" in text and "把这个网站的网址直接发给你" in text


async def test_site_profile_read_rule_lets_page_post_through(shop_app, shop, upstream):
    """网站档案把埋点 POST 标成读取：同一个页面，原来悄悄拦下的请求现在放行，也不问你。"""
    app = shop_app
    folder = app.cfg.data_dir / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shop.yaml").write_text(
        "type: site\nservice: shop\ntitle: 小店\nhosts: [shop.example]\nactions:\n"
        "  - id: beacon\n    match: {method: POST, path: /beacon}\n    class: read\n", encoding="utf-8")
    app.reload_profiles()
    upstream.script = [{"tools": [("browser", {"action": "open", "url": "https://shop.example/tracked"})]},
                       {"text": "打开了"}]
    await ask_without_naming(app, "m1", "打开那个页面", "https://shop.example/tracked")
    await wait_until(lambda: "打开了" in app.channel.sent, 60)
    await wait_until(lambda: shop.beacons > 0, 30)
    beacon = egress(app, like="/beacon")
    assert beacon and beacon[0]["verdict"] == "allow" and "档案里的读取" in beacon[0]["reason"]
    assert not app.channel.approvals


def test_denial_notes_group_and_hint():
    from host.browser import Denied, denial_notes
    same = [Denied("r", "POST", f"https://shop.example/api/{i}", "页面在后台自己发的提交", True, hint=False)
            for i in range(7)]
    same.append(same[0])        # 同一个请求重复拦，只算一次
    cross = [Denied("r", "POST", "https://stats.example/c", "统计", False, hint=True)] * 3
    notes = denial_notes(same + cross)
    assert len(notes) == 2
    assert "7 个请求" in notes[0] and "还有 2 个" in notes[0]
    # 已经点名过（或者读到了疑似注入）：不再叫 Agent 请你发网址
    assert "网址直接发给你" not in notes[0] and "停下来告诉用户" in notes[0]
    assert "3 个发往别的网站" in notes[1]
    assert denial_notes([]) == []


async def test_denials_from_another_run_are_not_reported(shop_app):
    from host.browser import Denied
    from host.runs import RunCtx
    b = shop_app.browser
    mine = RunCtx("run_a", "t", "s", "k", "user")
    b.note_denied(Denied("run_b", "POST", "https://other.example/x", "后台", True, hint=True))
    b.note_denied(Denied("run_a", "POST", "https://shop.example/y", "后台", True, hint=True))
    taken = b._take_denied(mine)
    assert [d.url for d in taken] == ["https://shop.example/y"]
    assert b._take_denied(mine) == []


def _many_files(path: str) -> tuple[int, str, bytes]:
    if path == "/many":
        scripts = "".join(f"<script src=/s/{i}.js></script>" for i in range(40))
        html = (f"<!doctype html><meta charset=utf-8><title>很多文件</title>{scripts}<p id=n></p>"
                "<script>document.getElementById('n').textContent='已加载 '+(window.loaded||0)</script>")
        return 200, "text/html; charset=utf-8", html.encode()
    if path.startswith("/s/"):
        return 200, "text/javascript", b"window.loaded=(window.loaded||0)+1;"
    return 404, "text/plain", b"not found"


@pytest.mark.parametrize("http2", [False, True], ids=["http1", "http2"])
async def test_page_with_many_files_loads_through_the_proxy(make_app, shop, upstream, tmp_path, http2):
    """一次并发取几十个文件的页面能完整加载，上游连接被复用。

    以前建上游连接时才把主机名换成 IP，mitmproxy 按地址找可复用的连接就再也对不上：
    上游说 HTTP/2 时每个请求都去新开连接，几十个请求一直挂着，单页应用打不开。
    """
    from tests.h2shop import H2Site
    site = None
    if http2:
        site = H2Site(_many_files)
        await site.start(self_signed_context("h2.example", tmp_path))
        host, overrides = "h2.example", {"h2.example": ("127.0.0.1", site.port)}
    else:
        host, overrides = "shop.example", {"shop.example": ("127.0.0.1", shop.port)}
    app = make_app(egress_overrides=overrides, egress_ssl_insecure=True)
    await app.start(with_channel=False)
    try:
        upstream.script = [{"tools": [("browser", {"action": "open", "url": f"https://{host}/many"})]},
                           {"text": "开好了"}]
        started = asyncio.get_running_loop().time()
        await app.on_user_text("m1", f"打开 https://{host}/many 看看")
        await wait_until(lambda: "开好了" in app.channel.sent, 60)
        assert asyncio.get_running_loop().time() - started < 30
        opened = next(r for r in upstream.requests if "已打开" in last_tool_text(r["body"]))
        assert "已加载 40" in last_tool_text(opened["body"])
        assert len(egress(app, verdict="allow", like="/s/")) == 40
        if site:
            assert len([p for p in site.requests if p.startswith("/s/")]) == 40
            assert site.connections <= 3, f"上游连接没有复用：开了 {site.connections} 条"
    finally:
        if site:
            await site.stop()


async def test_upstream_connections_only_to_adjudicated_addresses(shop_app):
    """出网代理只连裁决放行时固定下来的地址；没经过裁决的上游连接不建。"""
    from types import SimpleNamespace

    from host.egress.proxy import EgressAddon
    addon = next(a for a in app_addons(shop_app) if isinstance(a, EgressAddon))
    data = SimpleNamespace(server=SimpleNamespace(address=("93.184.216.34", 443), error=None, sni=None),
                           client=SimpleNamespace(id="no-such-client"))
    addon.server_connect(data)
    assert data.server.error and data.server.sni is None


def app_addons(app) -> list:
    return list(app.egress.master.addons.chain)


async def test_browser_traffic_needs_the_browser_token(shop_app):
    """电脑里的程序拿不到浏览器的通行证；没有通行证的请求不会被当成浏览器的。"""
    app = shop_app
    proxy = f"http://127.0.0.1:{app.egress_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get("http://shop.example/", proxy=proxy) as r:
            assert r.status == 407
    env = app.computer.envs[-1] if app.computer.envs else {}
    assert app.browser.proxy_token not in json.dumps(env)
