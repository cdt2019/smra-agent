"""测试用的假模型上游和应用装配。

测试走的是真实路径：真实的工作进程、真实的子进程运行程序、真实的 Anthropic SDK、
真实的模型代理和工具。只有模型提供方被换成了本机的一个假服务，所以不需要密钥，也不花钱。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time

import pytest
from aiohttp import web

from host.app import App
from host.computer.local import LocalComputer
from host.config import Config

REAL_KEY = "sk-test-real-key-only-on-host"


def _find_browser() -> str | None:
    """第 7 步的测试用哪个真实浏览器：本机装了 Edge 就用 Edge，否则用 playwright install 下的 Chromium，都没有返回 None。"""
    import shutil
    from pathlib import Path
    if Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe").exists() or shutil.which("msedge"):
        return "msedge"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if Path(p.chromium.executable_path).exists():
                return "chromium"
    except Exception:
        pass
    return None


BROWSER_CHANNEL = _find_browser()

_TIME_PREFIX = re.compile(r"^\[[^\]]*\] ?")


def user_texts(message: dict) -> list[str]:
    """一条用户消息里用户自己说的话：去掉时间前缀，跳过 <memory> 和工具结果。"""
    content = message["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    out = []
    for b in blocks:
        if b.get("type") == "text" and not b["text"].startswith("<memory>"):
            out.append(_TIME_PREFIX.sub("", b["text"]))
    return out


class FakeUpstream:
    """假的模型提供方。

    默认回一句 "echo: <最后一组用户消息>"。往 script 里放回合可以编排工具调用：
        {"text": "..."}                          结束回合
        {"tools": [("工具名", {输入})], "text": ...}  发起工具调用
    script 里也可以放函数 f(请求体) -> 上面的字典。
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []      # 每个元素：{"headers": ..., "body": ..., "path": ...}
        self.script: list = []
        self.mode = "ok"                    # ok / error400
        self.cache_read = 0                 # 每次响应报告的缓存读取量
        self.url = ""
        self._ids = itertools.count(1)

    def _echo(self, body: dict) -> dict:
        tail = []
        for message in reversed(body["messages"]):
            if message["role"] != "user":
                break
            tail = user_texts(message) + tail
        return {"text": "echo: " + " | ".join(t for t in tail if t)}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append({"headers": dict(request.headers), "body": body, "path": request.path_qs})
        if self.mode == "error400":
            return web.json_response(
                {"type": "error", "error": {"type": "invalid_request_error", "message": "fake bad request"}},
                status=400)
        spec = self.script.pop(0) if self.script else self._echo(body)
        if callable(spec):
            spec = spec(body)

        blocks = []
        for query, urls in spec.get("search", []):
            # 服务端网页搜索：一个 server_tool_use 块，加一个完整的搜索结果块
            sid = f"srvtoolu_{next(self._ids):04d}"
            blocks.append(({"type": "server_tool_use", "id": sid, "name": "web_search", "input": {}},
                           {"type": "input_json_delta", "partial_json": json.dumps({"query": query})}))
            blocks.append(({"type": "web_search_tool_result", "tool_use_id": sid, "content": [
                {"type": "web_search_result", "url": u, "title": u, "encrypted_content": "x", "page_age": None}
                for u in urls]}, None))
        if spec.get("text"):
            blocks.append(({"type": "text", "text": ""}, {"type": "text_delta", "text": spec["text"]}))
        for name, data in spec.get("tools", []):
            blocks.append(({"type": "tool_use", "id": f"toolu_{next(self._ids):04d}", "name": name, "input": {}},
                           {"type": "input_json_delta", "partial_json": json.dumps(data, ensure_ascii=False)}))
        stop = "tool_use" if spec.get("tools") else "end_turn"

        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": self.cache_read}}})]
        for i, (start, delta) in enumerate(blocks):
            events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
            if delta is not None:
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events += [
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": 20, "cache_read_input_tokens": None}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        for name, data in events:
            await response.write(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        await response.write_eof()
        return response


class CaptureChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.files: list[tuple[str, str, bytes]] = []     # (路径, 说明, 内容)
        self.approvals: list[tuple[str, str, list]] = []  # (正文, 审批号, 可选范围)
        self.notices: list[tuple[str, str]] = []          # (正文, 通知号)：带反馈按钮的通知

        self.reviews: list[tuple[str, str, str]] = []     # (正文, 种类, 编号)：等你审阅的提交

    async def send_notice(self, text: str, notice_id: str) -> None:
        self.notices.append((text, notice_id))

    async def send_review(self, text: str, kind: str, proposal_id: str) -> None:
        self.reviews.append((text, kind, proposal_id))

    async def send_approval(self, text: str, approval_id: str, scopes: list) -> None:
        self.approvals.append((text, approval_id, scopes))

    async def listen(self, on_text) -> None:
        await asyncio.Event().wait()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_file(self, path: str, caption: str) -> None:
        with open(path, "rb") as f:
            self.files.append((path, caption, f.read()))


class SpyComputer(LocalComputer):
    """记录每次交给电脑的环境变量，用来检查真密钥没有被带进去。"""

    def __init__(self, home, allow_exec=False) -> None:
        super().__init__(home, allow_exec)
        self.envs: list[dict] = []

    async def exec(self, argv, env, timeout):
        self.envs.append(dict(env))
        return await super().exec(argv, env, timeout)


@pytest.fixture
async def upstream():
    fake = FakeUpstream()
    web_app = web.Application()
    web_app.router.add_post("/{tail:.*}", fake.handle)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


@pytest.fixture
async def make_app(tmp_path, upstream):
    apps: list[App] = []

    def factory(allow_exec: bool = False, **overrides) -> App:
        cfg = Config(data_dir=tmp_path, port=0, model_upstream=upstream.url, api_key=REAL_KEY,
                     driver="local", retry_delay_s=0, run_timeout_s=120,
                     egress_port=0, egress_hold_s=0.5, egress_bounce=False,
                     # 测试不受跑的时刻影响：没有安静时段；夜间整理默认不建；Webhook 用随机端口
                     quiet_hours="", consolidate_cron="", webhook_port=0, bundled_skills=False,
                     # 浏览器只在第 7 步的测试里真的启动；接管页面用随机端口
                     takeover_port=0, browser_channel=BROWSER_CHANNEL or "chromium", browser_hold_s=1.0,
                     # SMRA_TEST_BROWSER=docker：第 7 步的测试改用容器里的浏览器（只在 Linux 上、有 docker 时）
                     browser=os.environ.get("SMRA_TEST_BROWSER", "local"))
        for key, value in overrides.items():
            setattr(cfg, key, value)
        app = App(cfg, CaptureChannel(), SpyComputer(tmp_path / "home", allow_exec))
        app.injection = None        # 注入检测默认关掉，否则它的模型调用会吃掉编排好的回合
        apps.append(app)
        return app

    yield factory
    for app in apps:
        try:
            await app.stop()
        except Exception:
            pass


def self_signed_context(host: str, folder):
    """给测试用的 HTTPS 假网站生成自签证书，返回服务端用的 SSL 上下文。"""
    import datetime as dt
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = folder / f"{host}.crt", folder / f"{host}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


async def wait_until(predicate, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待的条件在时限内没有成立")


def status_of(app: App) -> list[str]:
    return [r["status"] for r in app.db.execute("SELECT status FROM inbox ORDER BY created_at, rowid")]


def all_done(app: App) -> bool:
    statuses = status_of(app)
    return bool(statuses) and all(s in ("DONE", "FAILED") for s in statuses)
