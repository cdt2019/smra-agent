"""语音转文字、照片看图。语音识别和看图的模型都换成本机的假服务器，不花真的钱。"""
from __future__ import annotations

import base64
import json

import pytest
from aiohttp import web

from host import models
from host.channels.telegram import TelegramChannel
from host.config import Config
from host.media import Media
from tests.conftest import all_done, wait_until

VOICE = b"OggS-fake-voice"
PHOTO = b"\xff\xd8\xff-fake-jpeg"


class FakeMediaApi:
    def __init__(self):
        self.stt: list[dict] = []
        self.vision: list[dict] = []
        self.fail = False
        self.url = ""

    async def transcribe(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.stt.append({"auth": request.headers.get("Authorization"), "model": form["model"],
                         "name": form["file"].filename, "data": form["file"].file.read()})
        if self.fail:
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response({"text": "明天下午三点提醒我去牙医"})

    async def messages(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.vision.append({"key": request.headers.get("x-api-key"), "body": body})
        return web.json_response({"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                                  "content": [{"type": "text", "text": "一张停车票，金额 ¥35，时间 14:20"}],
                                  "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})


@pytest.fixture
async def media_api():
    fake = FakeMediaApi()
    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", fake.transcribe)
    app.router.add_post("/v1/messages", fake.messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


def media_cfg(tmp_path, api, **kw) -> Config:
    base = dict(data_dir=tmp_path, model_upstream=api.url, api_key="sk-model", model_features=frozenset({"thinking"}),
                injection_model="deepseek-flash", describe_images=True,
                stt_upstream=f"{api.url}/v1", stt_api_key="sk-stt", stt_model="FunAudioLLM/SenseVoiceSmall")
    base.update(kw)
    return Config(**base)


def stored(tmp_path, name, data) -> dict:
    path = tmp_path / name
    path.write_bytes(data)
    return {"name": name, "stored": str(path), "size": len(data), "target": f"inbox/2026-11-25/{name}"}


async def test_voice_and_photo_get_text_once(tmp_path, media_api):
    media = Media(media_cfg(tmp_path, media_api))
    payload = {"text": "", "files": [stored(tmp_path, "voice_a.ogg", VOICE), stored(tmp_path, "photo_b.jpg", PHOTO),
                                     stored(tmp_path, "notes.csv", b"a,b")]}
    assert await media.annotate(payload)
    voice, photo, csv = payload["files"]
    assert voice["transcript"] == "明天下午三点提醒我去牙医"
    assert media_api.stt == [{"auth": "Bearer sk-stt", "model": "FunAudioLLM/SenseVoiceSmall",
                              "name": "voice_a.ogg", "data": VOICE}]
    assert photo["description"].startswith("一张停车票")
    sent = media_api.vision[0]
    assert sent["key"] == "sk-model" and sent["body"]["model"] == "deepseek-flash"
    assert sent["body"]["thinking"] == {"type": "disabled"}
    image = sent["body"]["messages"][0]["content"][0]
    assert image["source"]["media_type"] == "image/jpeg" and base64.b64decode(image["source"]["data"]) == PHOTO
    assert set(csv) == {"name", "stored", "size", "target"}        # 别的文件不动
    # 再处理一次（重试）不再调接口
    assert not await media.annotate(payload)
    assert len(media_api.stt) == 1 and len(media_api.vision) == 1
    await media.close()


async def test_failures_and_missing_config_only_leave_a_note(tmp_path, media_api):
    media_api.fail = True
    media = Media(media_cfg(tmp_path, media_api, describe_images=False))
    payload = {"files": [stored(tmp_path, "voice_a.ogg", VOICE), stored(tmp_path, "photo_b.jpg", PHOTO)]}
    assert await media.annotate(payload)
    assert "转文字失败" in payload["files"][0]["media_note"]
    assert set(payload["files"][1]) == {"name", "stored", "size", "target"}     # 关了看图：图片原样
    assert not media_api.vision
    await media.close()

    media = Media(media_cfg(tmp_path, media_api, stt_upstream=""))
    big = stored(tmp_path, "photo_c.png", PHOTO)
    big["size"] = 6 * 1024 * 1024
    payload = {"files": [stored(tmp_path, "voice_d.ogg", VOICE), big]}
    assert await media.annotate(payload)
    assert "SMRA_STT_UPSTREAM" in payload["files"][0]["media_note"]
    assert "太大" in payload["files"][1]["media_note"]
    assert len(media_api.stt) == 1 and not media_api.vision                    # 都没再调
    await media.close()


def test_defaults_from_env():
    deepseek = models.from_env({"SMRA_MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "k"}.get)
    assert deepseek["describe_images"] and deepseek["stt_upstream"] == ""
    anthropic = models.from_env({"ANTHROPIC_API_KEY": "k", "SMRA_STT_API_KEY": "s"}.get)
    assert not anthropic["describe_images"]                 # 主模型自己能看图
    assert anthropic["stt_upstream"] == "https://api.openai.com/v1" and anthropic["stt_model"] == "whisper-1"
    off = models.from_env({"SMRA_MODEL_PROVIDER": "deepseek", "SMRA_DESCRIBE_IMAGES": "0"}.get)
    assert not off["describe_images"]


async def test_agent_sees_the_transcript_and_description(make_app, upstream, media_api):
    app = make_app(describe_images=True, stt_upstream=f"{media_api.url}/v1", stt_api_key="sk-stt")
    app.media.vision_url = f"{media_api.url}/v1/messages"
    await app.start(with_channel=False)
    upstream.script = [{"text": "好的"}]
    await app.on_user_text("t:1", "", files=[("voice_x.ogg", VOICE), ("photo_y.jpg", PHOTO)])
    await wait_until(lambda: all_done(app) and app.channel.sent)
    first = upstream.requests[0]["body"]["messages"][0]["content"]
    assert "[语音转文字] 明天下午三点提醒我去牙医" in first
    assert "一张停车票，金额 ¥35" in first and "[附件] inbox/" in first
    row = app.db.execute("SELECT payload FROM inbox WHERE source = 'user'").fetchone()
    assert json.loads(row["payload"])["files"][0]["transcript"]          # 写回了收件箱


async def test_telegram_downloads_voice_messages(tmp_path):
    channel = TelegramChannel("123:abc", "42", tmp_path)

    async def fake_download(file_id):
        return f"bytes-of-{file_id}".encode()
    channel._download = fake_download
    files, problems = await channel._files_of({"voice": {"file_id": "V1", "file_unique_id": "u1", "duration": 3},
                                     "audio": {"file_id": "A1", "file_unique_id": "u2", "file_name": "memo.m4a"}})
    assert files == [("voice_u1.ogg", b"bytes-of-V1"), ("memo.m4a", b"bytes-of-A1")] and problems == []
