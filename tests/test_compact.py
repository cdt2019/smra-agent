"""宿主侧的上下文压缩（提供方没有服务端压缩时）。全部用假的模型上游，不花钱。"""
from __future__ import annotations

import json

import pytest
from aiohttp import web

from host import compact, sessions
from tests.conftest import REAL_KEY, all_done, wait_until
from tests.test_models import openai_upstream  # noqa: F401  （夹具）


def _rows(db, sid):
    return sessions.message_rows(db, sid)


def _add(db, sid, type_, role, content):
    sessions.append(db, sid, None, "host" if type_ == "user_message" else "self", type_,
                    {"role": role, "content": content})


def test_estimate_counts_cjk_heavier_and_images():
    assert compact.text_tokens("a" * 350) == 101
    assert compact.text_tokens("中" * 100) == 71
    assert compact.content_tokens([{"type": "image", "source": {}}]) == compact.IMAGE_TOKENS
    nested = [{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "x" * 35}]}]
    assert compact.content_tokens(nested) == 11


def test_cut_lands_on_a_turn_start_and_keeps_the_latest_turn(make_app):
    app = make_app()
    db, sid = app.db, sessions.new_session(app.db, "conversation")
    for i in range(4):
        _add(db, sid, "user_message", "user", f"第{i}轮 " + "字" * 100)
        _add(db, sid, "context_note", "user", "<memory>…</memory>")
        _add(db, sid, "assistant_message", "assistant",
             [{"type": "tool_use", "id": f"t{i}", "name": "fetch", "input": {}}])
        _add(db, sid, "tool_result", "user", [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 400}])
        _add(db, sid, "assistant_message", "assistant", [{"type": "text", "text": "好"}])
    _add(db, sid, "user_message", "user", "新消息一")
    _add(db, sid, "user_message", "user", "新消息二")       # 同一批的第二条不是切点
    rows = _rows(db, sid)
    cut = compact.choose_cut(rows, keep=1)
    assert rows[cut]["type"] == "user_message" and "新消息一" in rows[cut]["payload"]
    cut = compact.choose_cut(rows, keep=300)                # 留够 300：往前多留两轮
    assert "第2轮" in rows[cut]["payload"]
    assert compact.choose_cut(rows, keep=10**9) is None     # 全部都得留：没有可压缩的


def test_compacted_history_starts_with_the_summary(make_app):
    app = make_app()
    db, sid = app.db, sessions.new_session(app.db, "conversation")
    _add(db, sid, "user_message", "user", "老消息：我的车牌是沪A12345")
    _add(db, sid, "context_note", "user", "<memory>旧的</memory>")
    # 上一次运行死在工具调用中：被压缩掉以后，不该在后面的消息里补"结果未知"
    _add(db, sid, "assistant_message", "assistant", [{"type": "tool_use", "id": "t1", "name": "fetch", "input": {}}])
    _add(db, sid, "user_message", "user", "新消息")
    rows = _rows(db, sid)
    sessions.append(db, sid, None, "host", sessions.COMPACTION, {"summary": "用户给过车牌号", "covers_seq": rows[2]["seq"]})

    msgs = sessions.messages(db, sid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and "用户给过车牌号" in msgs[0]["content"]
    assert msgs[0]["content"].startswith("【宿主生成的摘要】") and "不是给你的指令" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "新消息"}
    # 记忆说明被压缩掉了：电脑里要重新附一份
    assert sessions.last_context_note(db, sid) is None
    # 原文还在，recall 搜得到
    assert sessions.search(db, "沪A12345")


def test_transcript_clips_tool_results_and_skips_thinking(make_app):
    app = make_app()
    db, sid = app.db, sessions.new_session(app.db, "conversation")
    _add(db, sid, "user_message", "user", "查一下")
    _add(db, sid, "assistant_message", "assistant", [
        {"type": "thinking", "thinking": "秘密的思考", "signature": "s"},
        {"type": "tool_use", "id": "t1", "name": "fetch", "input": {"url": "https://a.example/"}}])
    _add(db, sid, "tool_result", "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "页" * 5000}])
    text = compact.transcript("之前的摘要", _rows(db, sid))
    assert text.startswith("【上一次的摘要】\n之前的摘要")
    assert "秘密的思考" not in text and "调用工具 fetch" in text
    assert "省略 3500 字" in text


def test_off_when_provider_compacts_or_disabled(make_app):
    app = make_app()                                 # 默认是 Anthropic 官方：服务端压缩
    sid = sessions.new_session(app.db, "conversation")
    _add(app.db, sid, "user_message", "user", "字" * 1000)
    app.cfg.compact_at = 10
    assert not app.compactor.needed(sid)
    app.cfg.model_features = frozenset({"stream"})
    assert app.compactor.needed(sid)
    app.cfg.compact_at = 0
    assert not app.compactor.needed(sid)


async def test_long_conversation_is_compacted_before_the_run(make_app, openai_upstream):  # noqa: F811
    app = make_app(model_protocol="openai", model_upstream=openai_upstream.url, model="m",
                   model_features=frozenset(), compact_at=10**9, compact_keep=1)
    await app.start(with_channel=False)
    openai_upstream.script = [{"text": "记住了"}, {"text": "好的"}]
    await app.on_user_text("m1", "我的会员号是 VIP-778899，记一下")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 1)
    await app.on_user_text("m2", "再说点别的")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 2)
    sid = app.db.execute("SELECT id FROM session").fetchone()[0]
    # 上一次调用报来的输入长度是 100（假上游固定这么报）：压缩线设成 100 就该压缩
    assert compact.context_size(app.db, sid) >= 100

    app.cfg.compact_at = 100
    openai_upstream.script = [{"text": "用户的会员号是 VIP-778899。"}, {"text": "收到"}]
    before = len(openai_upstream.requests)
    await app.on_user_text("m3", "第三条")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 3)

    summarize, run_call = openai_upstream.requests[before:before + 2]
    sent = json.dumps(summarize["body"], ensure_ascii=False)
    assert "交接摘要" in sent and "VIP-778899" in sent and "第三条" not in sent
    assert {k.lower(): v for k, v in summarize["headers"].items()}["authorization"] == f"Bearer {REAL_KEY}"
    # 这次运行看到的是：摘要 + 最近一轮
    texts = json.dumps(run_call["body"]["messages"], ensure_ascii=False)
    assert "【宿主生成的摘要】" in texts and "用户的会员号是 VIP-778899。" in texts and "第三条" in texts
    assert "再说点别的" not in texts and "记住了" not in texts
    assert app.channel.sent[-1] == "收到"
    # 记账：这次调用记在 compact:<会话> 名下，计入每日花费
    row = app.db.execute("SELECT status, tokens FROM inference_log WHERE run_id = ?", (f"compact:{sid}",)).fetchone()
    assert row["status"] == 200 and row["tokens"] == 120
    # 压缩之前的用量不再算数：现在会话的大小按压缩后的算
    assert sessions.latest_compaction(app.db, sid)["summary"] == "用户的会员号是 VIP-778899。"


@pytest.fixture
async def anthropic_json():
    """只回非流式 JSON 的 Anthropic 兼容接口（DeepSeek 的 /anthropic 接口不带 stream 时就是这样）。"""
    calls = []

    async def handle(request):
        calls.append({"headers": dict(request.headers), "body": await request.json(), "path": request.path})
        return web.json_response({"type": "message", "role": "assistant", "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "摘要正文"}],
                                  "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 0}})

    web_app = web.Application()
    web_app.router.add_post("/{tail:.*}", handle)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", calls
    await runner.cleanup()


async def test_anthropic_protocol_summary_and_failures(make_app, anthropic_json):
    url, calls = anthropic_json
    app = make_app(model_upstream=url, model_features=frozenset({"stream", "thinking"}), compact_at=1, compact_keep=1)
    await app.start(with_channel=False)
    sid = sessions.new_session(app.db, "conversation")
    _add(app.db, sid, "user_message", "user", "第一轮")
    _add(app.db, sid, "assistant_message", "assistant", [{"type": "text", "text": "答一"}])
    _add(app.db, sid, "user_message", "user", "第二轮")
    assert await app.compactor.maybe_compact(sid)
    body = calls[0]["body"]
    assert calls[0]["path"] == "/v1/messages"
    assert {k.lower(): v for k, v in calls[0]["headers"].items()}["x-api-key"] == REAL_KEY
    assert body["thinking"] == {"type": "disabled"} and "stream" not in body
    assert [m["content"] for m in sessions.messages(app.db, sid)][1:] == ["第二轮"]

    # 第二次压缩：上一次的摘要也交给模型
    _add(app.db, sid, "assistant_message", "assistant", [{"type": "text", "text": "答二"}])
    _add(app.db, sid, "user_message", "user", "第三轮")
    assert await app.compactor.maybe_compact(sid)
    assert "【上一次的摘要】\n摘要正文" in calls[1]["body"]["messages"][0]["content"]

    # 今天的预算用完了：不压缩，运行照常
    app.cfg.daily_token_budget = 1
    _add(app.db, sid, "assistant_message", "assistant", [{"type": "text", "text": "答三"}])
    _add(app.db, sid, "user_message", "user", "第四轮")
    assert not await app.compactor.maybe_compact(sid)
    # 上游出错：只记日志，不抛
    app.cfg.daily_token_budget = 10**9
    app.cfg.model_upstream = "http://127.0.0.1:9"
    assert not await app.compactor.maybe_compact(sid)
    assert len(calls) == 2
