"""宿主一侧的网页搜索：主模型没有服务端搜索时用。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web

from host import models
from host.search import SearchError, parse_results, request_body
from tests.conftest import all_done, wait_until

SEARCH_KEY = "sk-search-key-only-on-host"
BASIC = frozenset({"stream", "thinking", "effort"})


def env_of(**values):
    return lambda key, default=None: values.get(key, default)


def call(action="search", name="deepseek_web_search", **extra) -> dict:
    return {"type": "server_tool_use", "id": "srvtoolu_1", "name": name, "input": {"action": action, **extra}}


def result(*urls) -> dict:
    return {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
        {"type": "web_search_result", "url": u, "title": f"标题 {u}", "snippet": "摘要", "page_age": None,
         "encrypted_content": "x"} for u in urls]}


def test_search_request_cannot_open_pages():
    body = request_body("deepseek-flash", "logseq 定价")
    assert body["thinking"] == {"type": "disabled"}         # 开思考才有 open_page
    assert body["tools"] == [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]


def test_parse_keeps_only_real_results():
    found = parse_results({"content": [call(), result("https://a.example/1", "https://a.example/1",
                                                      "javascript:alert(1)", "https://b.example/2")]})
    assert [r["url"] for r in found] == ["https://a.example/1", "https://b.example/2"]


@pytest.mark.parametrize("block", [
    call("open_page", url="https://evil.example/?secret=1"),
    call("find_in_page", url="https://evil.example/", pattern="x"),
    call("read_link", name="deepseek_read_url", url="https://evil.example/"),
    {"type": "web_fetch_tool_result", "tool_use_id": "s", "content": {}},
])
def test_any_action_but_search_voids_the_whole_response(block):
    with pytest.raises(SearchError, match="搜索以外"):
        parse_results({"content": [call(), result("https://a.example/"), block]})


def test_provider_error_is_reported():
    failed = {"type": "web_search_tool_result", "tool_use_id": "s",
              "content": [{"type": "web_search_tool_result_error", "error_code": "unavailable"}]}
    with pytest.raises(SearchError, match="不可用"):
        parse_results({"content": [call(), failed]})


def test_search_settings():
    d = models.from_env(env_of(SMRA_MODEL_PROVIDER="deepseek", DEEPSEEK_API_KEY="k"))
    assert d["search_provider"] == "deepseek" and d["search_api_key"] == "k" and d["search_model"] == "deepseek-flash"
    # 别家主模型也能用
    o = models.from_env(env_of(SMRA_MODEL_PROVIDER="openai", SMRA_MODEL="m", OPENAI_API_KEY="o",
                               SMRA_SEARCH_PROVIDER="deepseek", DEEPSEEK_API_KEY="k"))
    assert o["search_provider"] == "deepseek" and o["search_api_key"] == "k" and o["api_key"] == "o"
    assert models.from_env(env_of(ANTHROPIC_API_KEY="a"))["search_provider"] == ""
    off = models.from_env(env_of(SMRA_MODEL_PROVIDER="deepseek", DEEPSEEK_API_KEY="k", SMRA_SEARCH_PROVIDER="off"))
    assert off["search_provider"] == ""
    with pytest.raises(ValueError, match="搜索提供方"):
        models.from_env(env_of(SMRA_SEARCH_PROVIDER="nope"))


class FakeSearch:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.content: list = [call(), result("https://found.example/page")]
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append({"headers": dict(request.headers), "body": await request.json()})
        return web.json_response({"id": "m", "type": "message", "role": "assistant", "content": self.content,
                                  "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 100}})


@pytest.fixture
async def searcher():
    fake = FakeSearch()
    web_app = web.Application()
    web_app.router.add_post("/{tail:.*}", fake.handle)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield fake
    await runner.cleanup()


async def start(make_app, searcher, **overrides):
    app = make_app(model_features=BASIC, search_provider="deepseek", search_upstream=searcher.url,
                   search_api_key=SEARCH_KEY, **overrides)
    await app.start(with_channel=False)
    return app


def last_result(upstream) -> str:
    return json.dumps(upstream.requests[-1]["body"]["messages"][-1], ensure_ascii=False)


async def test_search_through_the_host(make_app, upstream, searcher):
    app = await start(make_app, searcher)
    upstream.script = [{"tools": [("web_search", {"query": "logseq 定价"})]}, {"text": "查到了"}]
    await app.on_user_text("m1", "查一下")
    await wait_until(lambda: all_done(app) and app.channel.sent)

    tool = next(t for t in upstream.requests[0]["body"]["tools"] if t["name"] == "web_search")
    assert "type" not in tool and tool["input_schema"]["required"] == ["query"]
    assert "https://found.example/page" in last_result(upstream)
    assert app.adjudicator.is_seen("https://found.example/page")        # 之后可以直接 fetch
    assert searcher.requests[0]["headers"]["x-api-key"] == SEARCH_KEY
    assert "logseq 定价" in searcher.requests[0]["body"]["messages"][0]["content"]
    assert SEARCH_KEY not in json.dumps(upstream.requests, ensure_ascii=False)
    # 搜索的用量计入这次运行：两轮模型 120 × 2，搜索 50 + 10 + 100 × 0.1
    assert app.db.execute("SELECT tokens_used FROM run").fetchone()[0] == 310


async def test_page_opened_by_the_search_service_is_not_trusted(make_app, upstream, searcher):
    app = await start(make_app, searcher)
    searcher.content = [call(), result("https://found.example/page"),
                        call("open_page", url="https://evil.example/?secret=1")]
    upstream.script = [{"tools": [("web_search", {"query": "x"})]}, {"text": "没搜到"}]
    await app.on_user_text("m1", "查一下")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    seen = last_result(upstream)
    assert "搜索以外" in seen and "found.example" not in seen
    assert not app.adjudicator.is_seen("https://found.example/page")


async def test_search_limits(make_app, upstream, searcher):
    app = await start(make_app, searcher, search_max_per_run=1)
    upstream.script = [{"tools": [("web_search", {"query": "第一次"})]},
                       {"tools": [("web_search", {"query": "第二次"})]},
                       {"tools": [("web_search", {"query": "长" * 301})]},
                       {"text": "好"}]
    await app.on_user_text("m1", "查一下")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    assert len(searcher.requests) == 1


async def test_no_search_tool_without_a_backend(make_app, upstream):
    app = make_app(model_features=BASIC)
    await app.start(with_channel=False)
    await app.on_user_text("m1", "你好")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    assert "web_search" not in {t["name"] for t in upstream.requests[0]["body"]["tools"]}
