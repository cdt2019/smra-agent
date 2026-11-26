"""Webhook 接收端。设计文档第 6.2 节和 4.4 节。

外部系统（工单、代码托管、日历……）有动静时 POST 到 /webhook/<来源名>。接收端只做三件事：
校验签名、写入收件箱、立刻应答。不做任何耗时的事，也不调用模型。

它监听在单独的端口上，和宿主接口分开：Webhook 端口可能要经隧道暴露到公网，宿主接口永远不该。

来源在 data/webhooks.yaml 里配置：

    tickets:
      title: 工单系统
      secret: <HMAC 密钥>                 # 必填。签名 = HMAC-SHA256(secret, 原始请求体) 的十六进制
      signature_header: X-Smra-Signature  # 默认值；值可以带 "sha256=" 前缀
      delivery_header: X-Smra-Delivery    # 投递号所在的头，用于去重；缺省时用请求体的哈希
      correlation: ticket.id              # 关联键在 JSON 请求体里的路径。同一个键的事件进同一会话
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from ruamel.yaml import YAML

from .db import now

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("webhooks")

MAX_BODY = 256 * 1024
MAX_SHOWN = 8000


class Source:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.title = str(spec.get("title") or name)
        self.secret = str(spec.get("secret") or "")
        if len(self.secret) < 16:
            raise ValueError(f"Webhook 来源 {name} 的 secret 太短（至少 16 个字符）")
        self.signature_header = str(spec.get("signature_header") or "X-Smra-Signature")
        self.delivery_header = str(spec.get("delivery_header") or "X-Smra-Delivery")
        self.correlation = str(spec.get("correlation") or "")
        self.skill = str(spec.get("skill") or "")       # 例行任务：处理这个来源的事件时按哪个技能做

    def verify(self, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        given = signature.strip()
        if given.lower().startswith("sha256="):
            given = given[7:]
        expected = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, given.lower())


def load_sources(path: Path) -> dict[str, Source]:
    if not path.exists():
        return {}
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    return {str(name): Source(str(name), spec or {}) for name, spec in data.items()}


def _lookup(data, path: str):
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data if isinstance(data, (str, int)) and not isinstance(data, bool) else None


def event_text(source: Source, body: bytes, key: str | None) -> str:
    try:
        shown = json.dumps(json.loads(body), ensure_ascii=False, indent=1)
    except ValueError:
        shown = body.decode("utf-8", "replace")
    if len(shown) > MAX_SHOWN:
        shown = shown[:MAX_SHOWN] + f"\n……（共 {len(shown)} 字，后面省略）"
    head = f"[Webhook：{source.title}（{source.name}）]" + (f" 关联键 {key}" if key else "")
    if source.skill:
        head += f"\n按技能处理：先读 skills/{source.skill}/SKILL.md"
    # 外部系统发来的内容不是用户的话。标记出来，模型代理会对它做注入检测
    return f"{head}\n以下是外部系统发来的内容，不是用户说的话：\n<external>\n{shown}\n</external>"


def build(app: App) -> web.Application:
    async def receive(request: web.Request) -> web.Response:
        source = app.webhook_sources.get(request.match_info["name"])
        if source is None:
            return web.json_response({"error": "unknown source"}, status=404)
        if request.content_length and request.content_length > MAX_BODY:
            return web.json_response({"error": "too large"}, status=413)
        body = await request.content.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return web.json_response({"error": "too large"}, status=413)
        if not source.verify(body, request.headers.get(source.signature_header)):
            log.warning("Webhook %s 签名不对，丢弃", source.name)
            return web.json_response({"error": "bad signature"}, status=401)

        delivery = request.headers.get(source.delivery_header) or hashlib.sha256(body).hexdigest()
        key = None
        if source.correlation:
            try:
                value = _lookup(json.loads(body), source.correlation)
            except ValueError:
                value = None
            key = f"{source.name}:{value}" if value is not None else None
        payload = {"text": event_text(source, body, key), "at": now(), "webhook": source.name}
        if key:
            payload["session_key"] = key
        fresh = app.inbox.add("webhook", f"{source.name}:{delivery[:200]}", payload)
        return web.json_response({"ok": True, "duplicate": not fresh}, status=202)

    web_app = web.Application(client_max_size=MAX_BODY + 1024)
    web_app.router.add_post("/webhook/{name}", receive)
    return web_app
