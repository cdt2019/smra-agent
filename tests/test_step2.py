"""第 2 步的验收项：电脑与记忆。逐条对应设计文档"第 2 步：电脑与记忆"。"""
from __future__ import annotations

import cell_tools as ct
import pytest

from host import sessions
from host.files import inbox_target
from tests.conftest import all_done, wait_until

EDIT = "str_replace_based_edit_tool"


def results_in(body: dict) -> list[dict]:
    """请求体里最后一条用户消息中的工具结果。"""
    content = body["messages"][-1]["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


def memory_notes(body: dict) -> list[str]:
    return [m["content"] for m in body["messages"]
            if m["role"] == "user" and isinstance(m["content"], str) and m["content"].startswith("<memory>")]


def tool_names(body: dict) -> set[str]:
    return {t["name"] for t in body["tools"]}


# ------------------------------------------------------------------ 工具本身


def test_text_editor_commands(tmp_path):
    home = ct.Home(tmp_path)
    ct.ensure_layout(home)
    assert "已创建" in ct.text_editor(home, {"command": "create", "path": "work/a.md", "file_text": "一\n二\n三"})
    assert ct.text_editor(home, {"command": "view", "path": "work/a.md"}) == "1\t一\n2\t二\n3\t三"
    ct.text_editor(home, {"command": "str_replace", "path": "work/a.md", "old_str": "二", "new_str": "贰"})
    ct.text_editor(home, {"command": "insert", "path": "work/a.md", "insert_line": 0, "insert_text": "零"})
    assert (tmp_path / "work/a.md").read_text(encoding="utf-8") == "零\n一\n贰\n三"
    assert ct.text_editor(home, {"command": "view", "path": "work/a.md", "view_range": [2, 3]}) == "2\t一\n3\t贰"

    # 覆盖已有文件前留备份
    ct.text_editor(home, {"command": "create", "path": "work/a.md", "file_text": "新内容"})
    assert (tmp_path / ".backups/work/a.md").read_text(encoding="utf-8") == "零\n一\n贰\n三"

    with pytest.raises(ct.ToolError, match="没有找到"):
        ct.text_editor(home, {"command": "str_replace", "path": "work/a.md", "old_str": "不存在", "new_str": ""})
    (tmp_path / "work/b.md").write_text("x x", encoding="utf-8")
    with pytest.raises(ct.ToolError, match="2 次"):
        ct.text_editor(home, {"command": "str_replace", "path": "work/b.md", "old_str": "x", "new_str": "y"})

    listing = ct.text_editor(home, {"command": "view", "path": "."})
    assert "memory/" in listing and "memory/index.md" in listing and ".backups" not in listing


def test_paths_are_confined_to_home(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    home = ct.Home(tmp_path / "home")
    for bad in ["../secret.txt", str(tmp_path / "secret.txt"), "memory/../../secret.txt"]:
        with pytest.raises(ct.ToolError, match="不在主目录内"):
            ct.text_editor(home, {"command": "view", "path": bad})
    try:
        (tmp_path / "home" / "link").symlink_to(tmp_path / "secret.txt")
    except OSError:
        return      # 这台机器不允许建符号链接，跳过这一项
    with pytest.raises(ct.ToolError, match="不在主目录内"):
        ct.text_editor(home, {"command": "view", "path": "link"})


def test_long_output_is_bounded(tmp_path):
    home = ct.Home(tmp_path)
    text = "a" * 50_000 + "END"
    out = ct.bound_output(text, home, "toolu_1")
    assert len(out) < 25_000 and out.endswith("END") and ".outputs/toolu_1.txt" in out
    assert (tmp_path / ".outputs/toolu_1.txt").read_text(encoding="utf-8") == text


def test_input_validation():
    schema = ct.EXEC_TOOL["input_schema"]
    assert ct.validate({"command": "ls"}, schema) is None
    assert "缺少" in ct.validate({}, schema)
    assert ct.validate({"command": "ls", "extra": 1}, schema)
    assert ct.validate({"command": 1}, schema)
    assert ct.validate({"command": "ls", "timeout_s": True}, schema)
    assert ct.validate("not an object", schema)


# ------------------------------------------------------------------ 场景 S1：文件进来，报告出去


async def test_files_in_and_out(make_app, upstream):
    app = make_app()
    await app.start(with_channel=False)
    target = inbox_target("sales.csv")
    upstream.script = [
        {"tools": [(EDIT, {"command": "view", "path": target})]},
        {"tools": [(EDIT, {"command": "create", "path": "work/report.md", "file_text": "# 报告\n两个月合计 300"})]},
        {"tools": [("send_file", {"path": "work/report.md", "caption": "报告在这"})]},
        {"text": "做好了"},
    ]
    await app.on_user_text("t:1", "分析一下", files=[("sales.csv", b"month,amount\n1,100\n2,200\n")])
    await wait_until(lambda: all_done(app) and app.channel.sent)

    first = upstream.requests[0]["body"]
    assert f"[附件] {target}" in first["messages"][0]["content"]
    assert tool_names(first) == {EDIT, "fetch", "web_search", "recall", "send_file",
                                 "notify", "set_timer", "list_timers", "cancel_timer",
                                 "connection_request", "profile_propose", "webhook_request",
                                 "browser", "handoff", "keep_awake"}   # local 驱动默认没有命令行
    assert (app.computer.home / target).read_bytes() == b"month,amount\n1,100\n2,200\n"
    assert "1,100" in results_in(upstream.requests[1]["body"])[0]["content"]
    assert [(c, d) for _, c, d in app.channel.files] == [("报告在这", "# 报告\n两个月合计 300".encode())]
    assert app.channel.sent == ["做好了"]
    # 自动快照在收件箱标记完成之后才做，要等一下
    await wait_until(lambda: app.snapshots.list())
    assert "auto" in app.snapshots.list()[0]


# ------------------------------------------------------------------ 场景 S2 与进度文件：跨会话接着做


async def test_progress_file_carries_a_task_across_fresh_sessions(make_app, upstream):
    """一个要三次运行才能完成的任务。每次运行之间都开新会话，只能凭记忆和进度文件接上。"""
    app = make_app()
    await app.start(with_channel=False)
    progress = "memory/progress/q4.md"
    placeholder = "（每条一行：文件路径 — 一句话说明）"

    upstream.script = [
        {"tools": [(EDIT, {"command": "create", "path": progress,
                           "file_text": "目标：季度报告\n已完成：阶段1\n下一步：阶段2"})]},
        {"tools": [(EDIT, {"command": "str_replace", "path": "memory/index.md", "old_str": placeholder,
                           "new_str": f"{progress} — 季度报告的进度"})]},
        {"text": "阶段1做完了"},
    ]
    await app.on_user_text("t:1", "开始做季度报告")
    await wait_until(lambda: len(app.channel.sent) == 1)

    for n, (done_before, done_after) in enumerate([("阶段1", "阶段1、阶段2"), ("阶段1、阶段2", "阶段1、阶段2、阶段3")], 2):
        await app.on_user_text(f"new:{n}", "/new")
        before = len(upstream.requests)
        upstream.script = [
            {"tools": [(EDIT, {"command": "view", "path": progress})]},
            {"tools": [(EDIT, {"command": "str_replace", "path": progress,
                               "old_str": f"已完成：{done_before}", "new_str": f"已完成：{done_after}"})]},
            {"text": f"做到 {done_after}"},
        ]
        await app.on_user_text(f"t:{n}", "接着做")
        await wait_until(lambda: all_done(app) and len(upstream.requests) == before + 3)

        first = upstream.requests[before]["body"]
        # 新会话里没有之前的对话，只有这条消息和一份记忆；记忆里的索引指向进度文件
        assert [m["role"] for m in first["messages"]] == ["user", "user"]
        assert progress in memory_notes(first)[0]
        # 读进度文件得到的是上一次运行留下的状态
        assert f"已完成：{done_before}" in results_in(upstream.requests[before + 1]["body"])[0]["content"]

    final = (app.computer.home / progress).read_text(encoding="utf-8")
    assert "已完成：阶段1、阶段2、阶段3" in final
    assert app.db.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"] == 3


# ------------------------------------------------------------------ 历史只追加：系统提示词和工具在会话内不变


async def test_history_stays_append_only_within_a_session(make_app, upstream):
    app = make_app()
    await app.start(with_channel=False)
    placeholder = "（每条一行：文件路径 — 一句话说明）"

    # 第一次运行改了记忆
    upstream.script = [
        {"tools": [(EDIT, {"command": "str_replace", "path": "memory/index.md", "old_str": placeholder,
                           "new_str": "memory/notes/prefs.md — 用户偏好"})]},
        {"text": "记下了"},
    ]
    await app.on_user_text("t:1", "记一下我的偏好")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 1)
    run1_last = upstream.requests[-1]["body"]

    # 会话进行中你改了 AGENT.md：这个会话不受影响
    app.cfg.agent_md_path.write_text("# AGENT.md\n- 名字：小周\n", encoding="utf-8")
    await app.on_user_text("t:2", "继续")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 2)
    run2 = upstream.requests[-1]["body"]

    assert run2["system"] == run1_last["system"]
    assert run2["tools"] == run1_last["tools"]
    assert run2["messages"][:len(run1_last["messages"])] == run1_last["messages"]
    # 记忆变了，所以追加了一份新的 <memory>，而不是去改旧的
    notes = memory_notes(run2)
    assert len(notes) == 2 and "prefs.md" not in notes[0] and "prefs.md" in notes[1]
    assert "小周" not in run2["system"][0]["text"]

    # 新会话才用新的 AGENT.md
    await app.on_user_text("t:3", "/new")
    await app.on_user_text("t:4", "你叫什么")
    await wait_until(lambda: all_done(app) and len(upstream.requests) == 4)
    assert "小周" in upstream.requests[-1]["body"]["system"][0]["text"]


# ------------------------------------------------------------------ 执行工具时中断


async def test_interrupted_tool_call_is_reported_as_unknown(make_app, upstream):
    """运行死在执行工具的过程中。重试时不重跑那个工具，而是告诉模型结果未知。"""
    app = make_app()
    app.inbox.add("user", "t:1", {"text": "把文件整理一下"})
    event = app.inbox.pending()[0]
    sid = sessions.current_conversation(app.db, 6)
    app.inbox.set_session(event["id"], sid)
    sessions.append(app.db, sid, None, "host", "user_message",
                    {"role": "user", "content": "把文件整理一下"}, inbox_id=event["id"])
    sessions.append(app.db, sid, "run_old", "self", "assistant_message", {"role": "assistant", "content": [
        {"type": "text", "text": "我先删掉旧文件"},
        {"type": "tool_use", "id": "toolu_dead", "name": "exec", "input": {"command": "rm old/*"}}]})
    app.inbox.claim([event["id"]])                  # 进程死在这里

    await app.start(with_channel=False)
    await wait_until(lambda: all_done(app) and app.channel.sent)
    unknown = results_in(upstream.requests[0]["body"])
    assert unknown == [{"type": "tool_result", "tool_use_id": "toolu_dead", "is_error": True,
                        "content": unknown[0]["content"]}]
    assert "结果未知" in unknown[0]["content"]

    # 之后你又发了一条消息：历史里那条未知结果保持原样，继续往后追加
    await app.on_user_text("t:2", "好了吗")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) == 2)
    second = upstream.requests[-1]["body"]["messages"]
    assert second[:len(upstream.requests[0]["body"]["messages"])] == upstream.requests[0]["body"]["messages"]


# ------------------------------------------------------------------ recall


async def test_recall_finds_earlier_conversations(make_app, upstream):
    app = make_app()
    old = sessions.new_session(app.db, "conversation")
    sessions.append(app.db, old, None, "host", "user_message",
                    {"role": "user", "content": "季度报告的第二张图以后都按月画"})
    sessions.close_conversation(app.db)
    await app.start(with_channel=False)
    upstream.script = [{"tools": [("recall", {"query": "第二张图"})]}, {"text": "找到了"}]
    await app.on_user_text("t:1", "上次那张图怎么画来着")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    found = results_in(upstream.requests[1]["body"])[0]["content"]
    assert "按月画" in found and old in found


# ------------------------------------------------------------------ 命令行（需要显式开启）


async def test_exec_tool_when_enabled(make_app, upstream):
    app = make_app(allow_exec=True)
    await app.start(with_channel=False)
    upstream.script = [{"tools": [("exec", {"command": "echo hello-from-cell"})]}, {"text": "好"}]
    await app.on_user_text("t:1", "跑个命令")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    assert "exec" in tool_names(upstream.requests[0]["body"])
    out = results_in(upstream.requests[1]["body"])[0]["content"]
    assert "退出码：0" in out and "hello-from-cell" in out


# ------------------------------------------------------------------ 快照与恢复


async def test_snapshot_restores_a_damaged_memory(make_app, upstream):
    app = make_app()
    await app.start(with_channel=False)
    notes = app.computer.home / "memory/notes/prefs.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("喜欢按月的图", encoding="utf-8")

    await app.on_user_text("c:1", "/snapshot")
    await wait_until(lambda: len(app.channel.sent) == 1)
    assert "已做快照" in app.channel.sent[0]

    notes.write_text("被改坏了", encoding="utf-8")
    (app.computer.home / "memory/junk.md").write_text("多出来的", encoding="utf-8")
    await app.on_user_text("c:2", "/restore 1")
    await wait_until(lambda: len(app.channel.sent) == 2)

    assert "已恢复" in app.channel.sent[1]
    assert notes.read_text(encoding="utf-8") == "喜欢按月的图"
    assert not (app.computer.home / "memory/junk.md").exists()
    # 恢复前的状态也留了快照，恢复本身可以撤销
    assert any("before-restore" in n for n in app.snapshots.list())
    assert upstream.requests == []                   # 命令不经过模型
