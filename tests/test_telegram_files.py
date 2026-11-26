"""U7：Telegram 收发文件，对着一个假的 Bot API 服务器（不连真的 Telegram）。

收：照片取最大尺寸、语音、文档、视频；超过 20 MB 的不下载并说明；单个附件失败不连累别的；
    文件名里的路径和怪字符在宿主存盘时清理掉；陌生 chat 的附件根本不下载。
发：multipart 上传，文件名去掉运行号前缀；说明超过 1024 字先单独发文字；超过 50 MB 拒绝。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from host.channels import telegram

TOKEN = "123456:FAKE-token"
BIG = 25 * 1024 * 1024


class FakeBotApi:
    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.uploads: list[dict] = []
        self.files = {"PH_L": b"large-photo", "VO": b"OggS-voice", "DOC": b"%PDF-1.4 doc", "VID": b"mp4-bytes",
                      "STRANGER": b"should-not-download"}
        self.broken = {"BROKEN"}

    async def handle(self, request: web.Request) -> web.Response:
        path = request.path
        assert path.startswith((f"/bot{TOKEN}/", f"/file/bot{TOKEN}/")), path
        method = path.rsplit("/", 1)[-1]
        if path.startswith("/file/"):
            file_id = method
            if file_id not in self.files:
                return web.Response(status=404)
            return web.Response(body=self.files[file_id])
        if request.content_type == "multipart/form-data":
            form = {}
            reader = await request.multipart()
            async for part in reader:
                if part.filename:
                    form[part.name] = {"filename": part.filename, "data": await part.read()}
                else:
                    form[part.name] = (await part.read()).decode()
            self.uploads.append(form)
            self.calls.append((method, form))
            return web.json_response({"ok": True, "result": {}})
        body = await request.json()
        self.calls.append((method, body))
        if method == "getUpdates":
            batch = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            if not batch:
                await asyncio.sleep(0.2)
            return web.json_response({"ok": True, "result": batch})
        if method == "getFile":
            file_id = body["file_id"]
            if file_id in self.broken:
                return web.json_response({"ok": False, "description": "Bad Request: file is temporarily unavailable"})
            return web.json_response({"ok": True, "result": {"file_path": file_id}})
        return web.json_response({"ok": True, "result": {}})


@pytest.fixture
async def bot(tmp_path, monkeypatch):
    api = FakeBotApi()
    server = web.Application(client_max_size=64 * 1024 * 1024)
    server.router.add_route("*", "/{tail:.*}", api.handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(telegram, "API", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
    channel = telegram.TelegramChannel(TOKEN, "42", tmp_path)
    yield api, channel
    await channel._http().close()
    await runner.cleanup()


def _msg(update_id: int, chat: int = 42, **fields) -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat}, **fields}}


async def _listen_until(channel, received: list, count: int) -> None:
    async def on_text(source_id, text, files=None):
        received.append((source_id, text, files))

    task = asyncio.create_task(channel.listen(on_text))
    try:
        for _ in range(200):
            if len(received) >= count:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_receiving_photo_voice_document_video(bot):
    api, channel = bot
    api.updates = [
        _msg(1, caption="这张发票", photo=[{"file_id": "PH_S", "file_unique_id": "s", "file_size": 10},
                                         {"file_id": "PH_L", "file_unique_id": "big", "file_size": 11}]),
        _msg(2, voice={"file_id": "VO", "file_unique_id": "v1", "duration": 2, "file_size": 10}),
        _msg(3, document={"file_id": "DOC", "file_unique_id": "d1", "file_name": "../../etc/合同 v2?.pdf",
                          "file_size": 12}),
        _msg(4, video={"file_id": "VID", "file_unique_id": "m1", "file_size": 9}),
        _msg(5, document={"file_id": "HUGE", "file_unique_id": "h1", "file_name": "录像.mov", "file_size": BIG},
             caption="太大了"),
        _msg(6, chat=999, document={"file_id": "STRANGER", "file_unique_id": "x", "file_name": "a.txt"}),
        _msg(7, sticker={"file_id": "ST", "file_unique_id": "st"}),
        _msg(8, text="最后一条"),
    ]
    received: list = []
    await _listen_until(channel, received, 6)
    by_id = {sid: (text, files) for sid, text, files in received}
    assert set(by_id) == {"tg:1", "tg:2", "tg:3", "tg:4", "tg:5", "tg:8"}    # 陌生 chat、贴纸都没交上来

    assert by_id["tg:1"] == ("这张发票", [("photo_big.jpg", b"large-photo")])       # 只下最大的一张
    assert by_id["tg:2"] == ("", [("voice_v1.ogg", b"OggS-voice")])
    assert by_id["tg:3"][1] == [("../../etc/合同 v2?.pdf", b"%PDF-1.4 doc")]       # 原名交给宿主，宿主存盘时清理
    assert by_id["tg:4"][1] == [("video_m1.mp4", b"mp4-bytes")]
    text, files = by_id["tg:5"]
    assert files is None and text.startswith("太大了") and "录像.mov（25 MB）超过" in text

    downloaded = [body["file_id"] for method, body in api.calls if method == "getFile"]
    assert downloaded == ["PH_L", "VO", "DOC", "VID"]            # 大文件、陌生人的文件都没去下载
    assert (channel._offset_file.read_text()) == "9"


async def test_one_failed_attachment_does_not_drop_the_rest(bot):
    api, channel = bot
    message = {"document": {"file_id": "BROKEN", "file_unique_id": "b", "file_name": "坏的.txt"},
               "voice": {"file_id": "VO", "file_unique_id": "v1"}}
    files, problems = await channel._files_of(message)
    assert files == [("voice_v1.ogg", b"OggS-voice")] and problems == ["坏的.txt 下载失败"]


async def test_received_file_names_are_made_safe(make_app):
    app = make_app()
    await app.on_user_text("tg:3", "看看这个", files=[("../../etc/合同 v2?.pdf", b"%PDF"),
                                                     ("..\\..\\win.ini", b"x"), ("...", b"y")])
    import json
    payload = json.loads(app.db.execute("SELECT payload FROM inbox WHERE source_id = 'tg:3'").fetchone()["payload"])
    uploads = (Path(app.cfg.data_dir) / "uploads").resolve()
    for f in payload["files"]:
        stored = Path(f["stored"]).resolve()
        assert stored.parent == uploads and stored.read_bytes() in (b"%PDF", b"x", b"y")
        assert ".." not in f["target"].split("/") and f["target"].startswith("inbox/")
        assert "/" not in Path(f["target"]).name and "\\" not in f["target"]
    assert [Path(f["target"]).name for f in payload["files"]] == ["合同_v2_.pdf", "win.ini", "file"]


async def test_sending_files(bot, tmp_path):
    api, channel = bot
    out = tmp_path / "outgoing"
    out.mkdir()
    report = out / "run_abc123-1-周报 final.pdf"
    report.write_bytes(b"%PDF report")
    await channel.send_file(str(report), "这是周报")
    upload = api.uploads[-1]
    assert upload["chat_id"] == "42" and upload["caption"] == "这是周报"
    assert upload["document"] == {"filename": "周报 final.pdf", "data": b"%PDF report"}

    long_caption = "说明" * 600         # 1200 字，放不进说明栏
    await channel.send_file(str(report), long_caption)
    methods = [m for m, _ in api.calls[-2:]]
    assert methods == ["sendMessage", "sendDocument"]
    assert api.calls[-2][1]["text"] == long_caption and "caption" not in api.uploads[-1]

    plain = out / "notes.txt"           # 不是运行发出的文件：名字原样
    plain.write_bytes(b"hi")
    await channel.send_file(str(plain), "")
    assert api.uploads[-1]["document"]["filename"] == "notes.txt" and "caption" not in api.uploads[-1]


async def test_sending_too_large_is_refused(bot, tmp_path, monkeypatch):
    api, channel = bot
    monkeypatch.setattr(telegram, "MAX_UPLOAD", 10)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 11)
    before = len(api.calls)
    with pytest.raises(RuntimeError, match="50 MB"):
        await channel.send_file(str(big), "")
    assert len(api.calls) == before
