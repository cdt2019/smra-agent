"""语音转文字、照片看图。

你发来的语音交给语音识别接口（OpenAI 兼容的 /audio/transcriptions：OpenAI、Groq、硅基流动等都有），
照片交给能看图的模型写一段说明。主模型（比如 deepseek-v4-pro）看不了图、听不了语音，
有了这两段文字它才知道你发的是什么。

结果写回收件箱里那条消息：重试时不再调第二次，会话记录也每次一样。
没配置、文件太大或者调用失败，都不挡住这条消息，只在附件旁边注明。
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import aiohttp

from . import models
from .files import human_size

log = logging.getLogger("media")

AUDIO_TYPES = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
               ".m4a": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}
MAX_AUDIO = 20 * 1024 * 1024        # OpenAI 的上限是 25MB，留点余量
MAX_IMAGE = 5 * 1024 * 1024         # Anthropic 协议单张图片的上限

DESCRIBE_PROMPT = """用户在聊天里发来一张图片。请用中文写一段说明，交给一个看不见图片的助手：
- 图里是什么（场景、物品、人物在做什么，不猜测人物身份）；
- 图里所有能看清的文字，按原样逐字转录（截图、票据、文件、路牌都算），保留数字和金额；
- 如果是界面截图、表格或图表，说清楚它显示的信息。
只写说明本身，不要寄语，不要回答图里的问题。"""


def kind_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in AUDIO_TYPES:
        return "audio"
    if suffix in IMAGE_TYPES:
        return "image"
    return ""


class Media:
    def __init__(self, cfg):
        # 看图：和注入检测一样走配置的提供方，用便宜的能看图的模型
        self.describe_on = cfg.describe_images
        self.vision_url = models.endpoint(cfg.model_protocol, cfg.model_upstream)
        self.vision_model = cfg.vision_model or cfg.injection_model
        self.protocol, self.features, self.api_key = cfg.model_protocol, cfg.model_features, cfg.api_key
        # 语音
        self.stt_url = f"{cfg.stt_upstream.rstrip('/')}/audio/transcriptions" if cfg.stt_upstream else ""
        self.stt_key, self.stt_model = cfg.stt_api_key, cfg.stt_model
        self._http: aiohttp.ClientSession | None = None

    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def annotate(self, payload: dict) -> bool:
        """给消息里的语音和图片补上文字。改了 payload 就返回 True（调用方写回收件箱）。"""
        changed = False
        for f in payload.get("files", []):
            if "transcript" in f or "description" in f or "media_note" in f:
                continue            # 做过了（包括失败的），重试时不再调
            kind = kind_of(f["name"])
            if kind == "audio":
                key, work = "transcript", self._transcribe
                if not self.stt_url:
                    f["media_note"] = "没有配置语音识别（SMRA_STT_UPSTREAM），转不了文字"
                    changed = True
                    continue
                limit = MAX_AUDIO
            elif kind == "image":
                key, work = "description", self._describe
                if not self.describe_on:
                    continue        # 主模型自己能看图，或者你关掉了看图
                limit = MAX_IMAGE
            else:
                continue
            changed = True
            if f["size"] > limit:
                f["media_note"] = f"文件太大（超过 {limit // (1024 * 1024)}MB），没有{'转文字' if kind == 'audio' else '看图'}"
                continue
            try:
                f[key] = (await work(f["name"], Path(f["stored"]).read_bytes())).strip()
            except Exception as e:
                log.warning("%s %s 失败：%s", "转文字" if kind == "audio" else "看图", f["name"], e)
                f["media_note"] = f"{'转文字' if kind == 'audio' else '看图'}失败（{type(e).__name__}）"
        return changed

    async def _transcribe(self, name: str, data: bytes) -> str:
        form = aiohttp.FormData()
        form.add_field("model", self.stt_model)
        form.add_field("response_format", "json")
        form.add_field("file", data, filename=Path(name).name,
                       content_type=AUDIO_TYPES.get(Path(name).suffix.lower(), "application/octet-stream"))
        headers = {"authorization": f"Bearer {self.stt_key}"} if self.stt_key else {}
        async with self.http().post(self.stt_url, data=form, headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"语音识别返回 {r.status}：{text[:200]}")
        return str(json.loads(text).get("text", ""))

    async def _describe(self, name: str, data: bytes) -> str:
        image = {"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[Path(name).suffix.lower()],
                                             "data": base64.b64encode(data).decode("ascii")}}
        body: dict = {"model": self.vision_model, "max_tokens": 1500,
                      "messages": [{"role": "user", "content": [image, {"type": "text", "text": DESCRIBE_PROMPT}]}]}
        if "thinking" in self.features:
            body["thinking"] = {"type": "disabled"}
        if self.protocol == "openai":
            body = models.to_openai(body, self.vision_model)
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                   **models.auth_headers(self.protocol, self.api_key)}
        async with self.http().post(self.vision_url, json=body, headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"看图的模型返回 {r.status}：{text[:200]}")
        message = json.loads(text)
        if self.protocol == "openai":
            message = models.from_openai(message, self.vision_model)
        return "\n".join(b.get("text", "") for b in message.get("content") or [] if b.get("type") == "text")


def file_lines(f: dict) -> list[str]:
    """会话记录里一个附件的样子：位置，加上转出来的文字或看图的说明。"""
    lines = [f"[附件] {f['target']}（{human_size(f['size'])}）"]
    if f.get("transcript"):
        lines.append(f"[语音转文字] {f['transcript']}")
    if f.get("description"):
        lines.append(f"[图片说明（另一个模型看图写的）]\n{f['description']}")
    if f.get("media_note"):
        lines.append(f"（{f['media_note']}）")
    return lines
