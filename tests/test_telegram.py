"""Telegram 渠道：令牌在请求地址里，任何错误信息都不能带出来。"""
from __future__ import annotations

import pytest
from aiohttp import web

from host.channels import telegram

TOKEN = "123456:TEST-token-must-not-leak"


@pytest.fixture
async def channel(tmp_path, monkeypatch):
    async def handle(request: web.Request) -> web.Response:
        if request.path.endswith("/getFile"):
            return web.json_response({"ok": True, "result": {"file_path": "docs/a.txt"}})
        if "/file/" in request.path:
            return web.Response(status=404, text="missing")
        return web.Response(status=502, text="bad gateway", content_type="text/html")

    server = web.Application()
    server.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(telegram, "API", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
    ch = telegram.TelegramChannel(TOKEN, "42", tmp_path)
    yield ch
    await ch._http().close()
    await runner.cleanup()


async def test_call_error_hides_token(channel):
    with pytest.raises(RuntimeError) as e:
        await channel._call("getUpdates", offset=0)
    assert TOKEN not in str(e.value) and e.value.__suppress_context__


async def test_download_error_hides_token(channel):
    with pytest.raises(RuntimeError) as e:
        await channel._download("file-1")
    assert TOKEN not in str(e.value) and e.value.__suppress_context__
