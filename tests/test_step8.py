"""第 8 步：多角色。角色说明与各自的记忆、点名与协调者、交接、跳数与预算上限。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tests.conftest import all_done, user_texts, wait_until

RESEARCHER = """---
title: 研究员
description: 查资料、读网页，把结论写成文件
---
只写有出处的结论。"""

SECRETARY = """---
title: 秘书
description: 起草邮件和文档
---
语气礼貌、简洁。"""


def write_roles(tmp_path, **roles: str) -> None:
    folder = tmp_path / "core" / "roles"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in roles.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")


def system_of(body: dict) -> str:
    return "".join(b["text"] for b in body["system"])


def memory_of(body: dict) -> str:
    for m in body["messages"]:
        content = m["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for b in blocks:
            if b.get("type") == "text" and b["text"].startswith("<memory>"):
                return b["text"]
    return ""


def is_tool_result(body: dict) -> bool:
    content = body["messages"][-1]["content"]
    return isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)


def handoff_sessions(app) -> list:
    return app.db.execute("SELECT * FROM session WHERE kind = 'handoff' ORDER BY created_at, rowid").fetchall()


async def test_s10_researcher_then_secretary(make_app, upstream, tmp_path):
    """S10：让研究员去查资料，让秘书整理成邮件草稿。"""
    write_roles(tmp_path, researcher=RESEARCHER, secretary=SECRETARY)
    edit = "str_replace_based_edit_tool"
    upstream.script = [
        # 协调者：交给研究员
        {"tools": [("handoff", {"role": "researcher", "task": "查 Opus 5.5 的主要变化，结论写到 work/research.md，"
                                                            "然后交给 secretary 整理成邮件草稿"})]},
        {"text": "已交给研究员去查，查完由秘书整理成邮件草稿。"},
        # 研究员：写结论，记一条自己的笔记，再交给秘书（用显示名点名）
        {"tools": [(edit, {"command": "create", "path": "work/research.md", "file_text": "# 资料\n- 结论 A\n"}),
                   (edit, {"command": "create", "path": "roles/researcher/memory/notes/sources.md",
                           "file_text": "常用来源：官方博客\n"})]},
        {"tools": [("handoff", {"role": "秘书", "task": "把 work/research.md 整理成给团队的邮件草稿，"
                                                      "存到 work/draft.md，然后回复用户"})]},
        {"text": "资料查好了，交给秘书整理。"},
        # 秘书：读资料，写草稿，回复用户
        {"tools": [(edit, {"command": "view", "path": "work/research.md"})]},
        {"tools": [(edit, {"command": "create", "path": "work/draft.md", "file_text": "各位好：\n结论 A\n"})]},
        {"text": "邮件草稿在 work/draft.md。"},
    ]
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("m1", "让研究员去查资料，让秘书整理成邮件草稿")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) >= 3)

    # 回复按顺序到达；非默认角色的话由宿主标上是谁
    assert app.channel.sent == ["已交给研究员去查，查完由秘书整理成邮件草稿。",
                                "【研究员】资料查好了，交给秘书整理。",
                                "【秘书】邮件草稿在 work/draft.md。"]
    home = app.computer.home
    assert (home / "work" / "draft.md").read_text(encoding="utf-8").startswith("各位好")

    # 每个角色一个会话：交接产生目标角色的新会话，跳数递增，用户在等（前台）
    conv = app.db.execute("SELECT * FROM session WHERE kind = 'conversation'").fetchone()
    assert conv["role"] == "coordinator"
    hand = handoff_sessions(app)
    assert [(s["role"], s["hops"], s["chain_fg"]) for s in hand] == [("researcher", 1, 1), ("secretary", 2, 1)]
    # 三次运行同属一串交接，花费合在一起算
    roots = {r["chain_root"] for r in app.db.execute("SELECT chain_root FROM run WHERE kind = 'agent'")}
    assert len(roots) == 1

    bodies = [r["body"] for r in upstream.requests]
    coord, researcher, secretary = bodies[0], bodies[2], bodies[5]
    # 系统提示词里有各自的角色说明；协调者的记忆在 memory/，其他角色在 roles/<名字>/memory/
    assert "你是「协调者」（coordinator）" in system_of(coord)
    assert "你是「研究员」（researcher）" in system_of(researcher) and "只写有出处的结论" in system_of(researcher)
    assert "你是「秘书」（secretary）" in system_of(secretary)
    assert "## memory/index.md" in memory_of(coord)
    assert "## roles/researcher/memory/index.md" in memory_of(researcher)
    assert "## roles/secretary/memory/index.md" in memory_of(secretary)
    assert (home / "roles" / "researcher" / "memory" / "notes" / "sources.md").exists()
    assert (home / "roles" / "secretary" / "memory" / "index.md").exists()
    # 秘书看不到前面的对话，只看到交接说明
    seen = " ".join(t for m in secretary["messages"] if m["role"] == "user" for t in user_texts(m))
    assert "[交接] 「研究员」（researcher）交给你的任务（第 2 跳）" in seen
    assert "查 Opus 5.5" not in json.dumps(secretary["messages"], ensure_ascii=False)


async def test_addressing_and_role_schedules(make_app, upstream, tmp_path):
    """@角色 点名；不点名交给协调者。每个角色一个对话；角色设的日程由它自己处理。"""
    write_roles(tmp_path, researcher=RESEARCHER, secretary=SECRETARY)
    app = make_app()
    await app.start(with_channel=False)

    await app.on_user_text("c1", "/roles")
    await wait_until(lambda: app.channel.sent)
    assert "@researcher 研究员" in app.channel.sent[0] and "（默认）" in app.channel.sent[0]

    await app.on_user_text("m1", "@秘书 帮我看看")
    await wait_until(lambda: len(app.channel.sent) >= 2)
    assert app.channel.sent[1] == "【秘书】echo: @秘书 帮我看看"
    await app.on_user_text("m2", "你好")
    await wait_until(lambda: len(app.channel.sent) >= 3)
    assert app.channel.sent[2] == "echo: 你好"
    await app.on_user_text("m3", "@secretary：再来")
    await wait_until(lambda: len(app.channel.sent) >= 4)
    assert app.channel.sent[3].startswith("【秘书】echo: ")
    convs = app.db.execute("SELECT id, role FROM session WHERE kind = 'conversation'").fetchall()
    assert sorted(r["role"] for r in convs) == ["coordinator", "secretary"]    # 秘书的两条进了同一个对话

    # 秘书设的日程，到期时由秘书在新会话里处理
    upstream.script = [
        {"tools": [("set_timer", {"kind": "schedule", "name": "周报提醒", "cron": "0 9 * * 1",
                                  "task": "提醒用户写周报"})]},
        {"text": "设好了"},
    ]
    await app.on_user_text("m4", "@secretary 每周一提醒我写周报")
    await wait_until(lambda: any("设好了" in s for s in app.channel.sent))
    timer = app.db.execute("SELECT * FROM timer WHERE created_by = 'agent'").fetchone()
    assert json.loads(timer["payload"])["role"] == "secretary"
    upstream.script = [{"text": "（后台）提醒过了"}]
    assert app.scheduler.tick(datetime.now(UTC) + timedelta(days=8)) == 1
    await wait_until(lambda: app.db.execute("SELECT 1 FROM session WHERE kind = 'timer'").fetchone() is not None
                     and all_done(app))
    timer_session = app.db.execute("SELECT * FROM session WHERE kind = 'timer'").fetchone()
    assert timer_session["role"] == "secretary"
    assert "你是「秘书」" in system_of(upstream.requests[-1]["body"])


def bounce(body: dict) -> dict:
    """两个角色互相踢皮球：每次都交给对方。"""
    if is_tool_result(body):
        return {"text": "交出去了"}
    other = "pong" if "你是「甲」" in system_of(body) else "ping"
    return {"tools": [("handoff", {"role": other, "task": "你来"})]}


PING = "---\ntitle: 甲\ndescription: 测试角色甲\n---\n"
PONG = "---\ntitle: 乙\ndescription: 测试角色乙\n---\n"


async def test_infinite_handoff_stopped_by_hop_limit(make_app, upstream, tmp_path):
    """两个角色互相无限交接：第 6 跳被拒绝，告诉用户。"""
    write_roles(tmp_path, ping=PING, pong=PONG)
    upstream.script = [bounce] * 40
    app = make_app()
    await app.start(with_channel=False)
    await app.on_user_text("m1", "@ping 开始")
    await wait_until(lambda: all_done(app) and any("跳" in s for s in app.channel.sent))

    hand = handoff_sessions(app)
    assert [s["hops"] for s in hand] == [1, 2, 3, 4, 5]
    assert [s["role"] for s in hand] == ["pong", "ping", "pong", "ping", "pong"]
    stops = [s for s in app.channel.sent if "上限" in s]
    assert len(stops) == 1
    assert "角色之间的交接到了上限（5 跳），已停止" in stops[0] and "「乙」想交给「甲」" in stops[0]
    # 被拒绝的那次交接，Agent 收到的是错误结果
    last = upstream.requests[-1]["body"]["messages"][-1]["content"]
    assert last[0]["is_error"] and "交接被拒绝" in last[0]["content"]
    assert len(upstream.requests) == 12      # 6 次运行，每次两轮，之后不再有人被叫醒


async def test_task_budget_stops_chain(make_app, upstream, tmp_path):
    """一个任务的总花费有上限：到了就不再交接，也不再调模型。"""
    write_roles(tmp_path, ping=PING, pong=PONG)
    upstream.script = [bounce] * 40
    app = make_app(task_token_budget=300, handoff_max_hops=50)    # 假模型每轮记 120 token
    await app.start(with_channel=False)
    await app.on_user_text("m1", "@ping 开始")
    await wait_until(lambda: all_done(app) and any("花费到了上限" in s for s in app.channel.sent))

    assert [s["hops"] for s in handoff_sessions(app)] == [1]
    stops = [s for s in app.channel.sent if "花费到了上限" in s]
    assert len(stops) == 1 and "300 token" in stops[0]
    # 乙交接时被拒，再调模型时代理直接拒绝，不再转发给上游
    assert len(upstream.requests) == 3
    tokens = app.db.execute("SELECT SUM(tokens_used) AS n FROM run").fetchone()["n"]
    assert tokens == 360
