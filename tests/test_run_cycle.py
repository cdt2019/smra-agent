"""第 1 步的验收项。逐条对应设计文档"第 1 步：一次运行"。"""
from __future__ import annotations

import json

import aiohttp

from host import sessions
from host.outbox import sanitize
from tests.conftest import REAL_KEY, all_done, wait_until


def outbox_rows(app):
    return app.db.execute("SELECT body, status, dedupe_key FROM outbox ORDER BY created_at, rowid").fetchall()


def run_count(app) -> int:
    return app.db.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"]


async def test_conversation_roundtrip(make_app, upstream):
    """能对话：消息进收件箱，触发一次运行，答复经发件箱送达。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "你好")
    await wait_until(lambda: all_done(app) and app.channel.sent)

    assert app.channel.sent == ["echo: 你好"]
    assert run_count(app) == 1
    row = app.db.execute("SELECT outcome, tokens_used FROM run").fetchone()
    assert row["outcome"] == "SENT"
    assert row["tokens_used"] == 120                 # 假上游报的 100 输入 + 20 输出，由代理记账
    assert not app.runs._by_token                    # 运行结束后通行证失效

    # 第二轮：上一轮的对话作为历史带回。记忆没变，所以不再追加 <memory>
    await app.on_user_text("t:2", "还在吗")
    await wait_until(lambda: len(app.channel.sent) == 2)
    roles = [m["role"] for m in upstream.requests[-1]["body"]["messages"]]
    assert roles == ["user", "user", "assistant", "user"]
    assert app.channel.sent[1] == "echo: 还在吗"


async def test_burst_is_merged_into_one_run(make_app, upstream):
    """连发三条消息，只产生一次运行，三条都被看到。"""
    app = make_app()
    for i, text in enumerate(["第一条", "第二条", "第三条"]):
        await app.on_user_text(f"t:{i}", text)
    await app.start(with_channel=False)
    await wait_until(lambda: all_done(app))

    assert run_count(app) == 1
    assert len(upstream.requests) == 1
    assert app.channel.sent == ["echo: 第一条 | 第二条 | 第三条"]


async def test_duplicate_delivery_is_processed_once(make_app, upstream):
    """同一条消息重复投递两次，只处理一次。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:same", "只说一次")
    await app.on_user_text("t:same", "只说一次")
    await wait_until(lambda: all_done(app))

    assert app.db.execute("SELECT COUNT(*) AS n FROM inbox").fetchone()["n"] == 1
    assert len(upstream.requests) == 1
    assert app.channel.sent == ["echo: 只说一次"]


async def test_crash_after_answer_does_not_reply_twice(make_app, upstream):
    """运行已经答复、但工作进程在收尾前崩溃：重启后事件被重新处理，你只收到一次回复。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "只回一次")
    await wait_until(lambda: all_done(app) and app.channel.sent)

    # 模拟"答复已发出，但没来得及把事件标成完成就崩溃了"
    app.db.execute("UPDATE inbox SET status = 'CLAIMED'")
    assert app.inbox.recover() == 1
    await wait_until(lambda: all_done(app) and run_count(app) == 2)

    assert len(outbox_rows(app)) == 1
    assert app.channel.sent == ["echo: 只回一次"]
    assert len(upstream.requests) == 1               # 重试时没有再调模型
    session_id = app.db.execute("SELECT id FROM session").fetchone()["id"]
    assert [m["role"] for m in sessions.messages(app.db, session_id)] == ["user", "user", "assistant"]


async def test_crash_before_answer_is_retried(make_app, upstream):
    """事件已入会话记录、但运行还没产出答复就崩溃：重启后正常处理，用户消息不会重复入账。"""
    app = make_app()
    app.inbox.add("user", "t:1", {"text": "中途崩溃"})
    event = app.inbox.pending()[0]
    session_id = sessions.current_conversation(app.db, 6)
    app.inbox.set_session(event["id"], session_id)
    sessions.append(app.db, session_id, None, "host", "user_message",
                    {"role": "user", "content": "中途崩溃"}, inbox_id=event["id"])
    app.inbox.claim([event["id"]])                   # 停在"处理中"，就像进程死在了这里

    await app.start(with_channel=False)              # 启动时恢复
    await wait_until(lambda: all_done(app))

    assert app.channel.sent == ["echo: 中途崩溃"]
    assert [m["role"] for m in sessions.messages(app.db, session_id)] == ["user", "user", "assistant"]


async def test_real_key_never_enters_the_computer(make_app, upstream):
    """电脑里拿不到模型密钥：交给它的只有通行证，真密钥由代理在宿主侧加上。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "密钥在哪")
    await wait_until(lambda: all_done(app))

    env = app.computer.envs[0]
    assert {"RUN_TOKEN", "HOST_URL", "AGENT_HOME", "CELL_EXEC", "HTTPS_PROXY", "SSL_CERT_FILE"} <= set(env)
    assert REAL_KEY not in json.dumps(env)
    assert upstream.requests[0]["headers"]["x-api-key"] == REAL_KEY
    assert env["RUN_TOKEN"] != REAL_KEY
    # 运行程序发出的请求体原样留底，里面也不该出现真密钥
    logged = app.db.execute("SELECT request FROM inference_log").fetchone()["request"]
    assert REAL_KEY not in logged


async def test_exits_reject_callers_without_a_live_run(make_app, upstream):
    """没有有效通行证，两个出口都进不去；通行证在运行结束后失效。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "hi")
    await wait_until(lambda: all_done(app))
    stale = app.computer.envs[0]["RUN_TOKEN"]
    base = f"http://127.0.0.1:{app.port}"

    async with aiohttp.ClientSession() as http:
        for token in ("guess", stale, REAL_KEY):
            headers = {"x-api-key": token, "authorization": f"Bearer {token}"}
            async with http.post(f"{base}/model/v1/messages", json={}, headers=headers) as r:
                assert r.status == 401
            async with http.post(f"{base}/host/run_context", json={}, headers=headers) as r:
                assert r.status == 401
            async with http.post(f"{base}/host/send", json={"text": "x"}, headers=headers) as r:
                assert r.status == 401
    assert len(upstream.requests) == 1               # 被拒的请求没有到达上游


async def test_run_budget_is_enforced_by_the_proxy(make_app, upstream):
    """预算由宿主侧的代理执行：超限后请求不再转发。"""
    app = make_app(run_token_budget=0, max_attempts=1)
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "超预算")
    await wait_until(lambda: all_done(app) and app.channel.sent)

    assert upstream.requests == []
    assert "预算" in app.channel.sent[0]
    assert app.db.execute("SELECT outcome FROM run").fetchone()["outcome"] == "FAILED"


async def test_failed_run_is_retried_then_reported(make_app, upstream):
    """运行失败会重试；次数用完后告诉你，而不是悄悄丢掉。"""
    upstream.mode = "error400"
    app = make_app(max_attempts=2)
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "会失败")
    await wait_until(lambda: all_done(app) and app.channel.sent)

    assert run_count(app) == 2
    assert len(upstream.requests) == 2
    assert len(app.channel.sent) == 1 and "试了 2 次" in app.channel.sent[0]
    assert app.db.execute("SELECT status FROM inbox").fetchone()["status"] == "FAILED"


async def test_new_command_starts_a_fresh_session(make_app, upstream):
    """/new 之后的消息进新会话，不带旧历史。"""
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("t:1", "旧对话")
    await wait_until(lambda: len(app.channel.sent) == 1)
    await app.on_user_text("t:2", "/new")
    await app.on_user_text("t:3", "新对话")
    await wait_until(lambda: len(app.channel.sent) == 3)

    assert app.db.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"] == 2
    # 新会话只有这条消息和一份 <memory>
    assert [m["role"] for m in upstream.requests[-1]["body"]["messages"]] == ["user", "user"]


async def test_idle_means_nothing_runs(make_app, upstream):
    """没有消息时不产生运行，也不调用模型。"""
    app = make_app()
    await app.start(with_channel=False)
    import asyncio
    await asyncio.sleep(1.0)
    assert run_count(app) == 0
    assert upstream.requests == []
    assert app.computer.envs == []


def test_remote_images_are_stripped_before_delivery():
    text = "看这张图 ![x](https://evil.example/p.png?d=secret) 以及 [链接](https://ok.example)"
    cleaned = sanitize(text)
    assert "evil.example" not in cleaned
    assert "[链接](https://ok.example)" in cleaned
