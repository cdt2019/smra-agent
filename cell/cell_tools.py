"""电脑里的工具实现和主目录约定。由 agent_run.py 使用，也可以单独导入做测试。

这些代码在不可信的一侧运行。路径限制在主目录内，是为了让开发模式（local 驱动，没有隔离）
下的文件工具也不至于碰到主目录以外的东西；真正的边界是电脑本身。
"""
from __future__ import annotations

import base64
import html.parser
import json
import locale
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_RESULT_CHARS = 30_000       # 工具结果超过这个长度就只给头尾
KEEP_CHARS = 10_000
MAX_SEND_BYTES = 20 * 1024 * 1024
MEMORY_FILE_CHARS = 12_000

LAYOUT_DIRS = ("memory/notes", "memory/progress", "inbox", "work", "skills", "tools")
DEFAULT_FILES = {
    "memory/instructions.md": "# 长期指令\n\n（用户给你的长期指令，每条一行）\n",
    "memory/index.md": "# 笔记索引\n\n（每条一行：文件路径 — 一句话说明）\n",
}
MEMORY_FILES = ("memory/instructions.md", "memory/index.md")


class ToolError(Exception):
    """工具执行失败。消息原样作为错误结果交给模型。"""


class Home:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def resolve(self, path) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path 不能为空")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise ToolError(f"路径不在主目录内：{path}")
        return resolved

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."


def _role_files(memory_dir: str) -> list[str]:
    """一个角色的记忆文件。默认角色是 memory/，其他角色是 roles/<名字>/memory/。"""
    return [rel.replace("memory/", f"{memory_dir}/", 1) for rel in MEMORY_FILES]


def ensure_layout(home: Home, memory_dir: str = "memory") -> None:
    for d in LAYOUT_DIRS:
        (home.root / d).mkdir(parents=True, exist_ok=True)
    for d in ("notes", "progress"):
        (home.root / memory_dir / d).mkdir(parents=True, exist_ok=True)
    for rel, text in DEFAULT_FILES.items():
        for target in {rel, rel.replace("memory/", f"{memory_dir}/", 1)}:
            p = home.root / target
            if not p.exists():
                p.write_text(text, encoding="utf-8", newline="")


# 已连接的服务：服务名 -> {title, hosts, auth, usage, surrogate}。由 agent_run 在启动时填好。
CONNECTIONS: dict[str, dict] = {}


def connections_text() -> str:
    if not CONNECTIONS:
        return "## 已连接的服务\n（没有）"
    parts = ["## 已连接的服务\n用 fetch 调用时加上 service 参数，认证会自动带上。命令行里的程序可以用环境变量 "
             "SMRA_CRED_<服务名大写> 作为令牌。哪些操作需要用户批准由服务档案决定。"]
    for name, c in sorted(CONNECTIONS.items()):
        state = "" if c.get("reviewed", True) else "，档案未审阅"
        if c.get("expired"):
            state += "，授权已失效：用户重新连接之前用不了，不要尝试（宿主已经提醒过用户）"
        parts.append(f"### {name}（{c['title']}，主机：{', '.join(c['hosts'])}{state}）\n{c['usage'].strip()}")
    return "\n\n".join(parts)


MAX_SKILLS_LISTED = 40


def skill_info(path: Path) -> tuple[str, str] | None:
    """读 SKILL.md 开头的 name / description。没有就用目录名和第一行正文。"""
    try:
        text = path.read_text(encoding="utf-8")[:4000]
    except (OSError, UnicodeDecodeError):
        return None
    name, desc = path.parent.name, ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            if key.strip() == "name" and value.strip():
                name = value.strip()
            elif key.strip() == "description":
                desc = value.strip()
    if not desc:
        desc = next((ln.strip("# ").strip() for ln in lines if ln.strip() and ln.strip() != "---"
                     and ":" not in ln[:20]), "")
    return name, " ".join(desc.split())[:150]


def skills_text(home: Home) -> str:
    found = []
    for skill_md in sorted((home.root / "skills").glob("*/SKILL.md")):
        info = skill_info(skill_md)
        if info:
            found.append(f"- {home.rel(skill_md.parent)}：{info[1] or '（没有说明）'}")
    if not found:
        return "## 技能\n（还没有）"
    more = f"\n……另有 {len(found) - MAX_SKILLS_LISTED} 个" if len(found) > MAX_SKILLS_LISTED else ""
    return "## 技能（用之前先读对应目录里的 SKILL.md）\n" + "\n".join(found[:MAX_SKILLS_LISTED]) + more


NOTICE_CHARS = 200


def notices_text(notices: list[dict]) -> str:
    if not notices:
        return "## 最近发给用户的通知（宿主记录）\n（没有）"
    lines = ["## 最近发给用户的通知（宿主记录）"]
    for n in notices:
        body = " ".join(n["body"].split())
        body = body if len(body) <= NOTICE_CHARS else body[:NOTICE_CHARS] + "…"
        mark = f"，用户标记：{n['feedback']}" if n.get("feedback") else ""
        kind = "即时" if n["kind"] == "now" else "摘要"
        lines.append(f"- [{n['sent_at'][:16].replace('T', ' ')} UTC，{kind}{mark}] {body}")
    return "\n".join(lines)


def memory_note(home: Home, notices: list[dict] | None = None, memory_dir: str = "memory") -> str:
    """每次运行开头给模型的记忆内容。只在内容变化时才作为新消息追加。各角色读自己的记忆目录。"""
    parts = ["<memory>"]
    for rel in _role_files(memory_dir):
        p = home.root / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = "（读取失败）"
        if len(text) > MEMORY_FILE_CHARS:
            text = text[:MEMORY_FILE_CHARS] + f"\n……（超过 {MEMORY_FILE_CHARS} 字，后面被截掉了，请精简这个文件）"
        parts.append(f"## {rel}\n{text.rstrip()}")
    parts.append(skills_text(home))
    parts.append(connections_text())
    parts.append(notices_text(notices or []))
    parts.append("</memory>")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 输入校验

def validate(data, schema: dict) -> str | None:
    """按工具的 input_schema 做最基本的校验。返回错误描述，没问题返回 None。

    工具输入是流式生成的（eager_input_streaming），可能被截断，所以执行前必须自己校验。
    """
    if not isinstance(data, dict):
        return "输入必须是一个 JSON 对象"
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in data:
            return f"缺少必填字段 {key}"
    if schema.get("additionalProperties") is False:
        extra = sorted(set(data) - set(props))
        if extra:
            return f"不认识的字段：{', '.join(extra)}"
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for key, value in data.items():
        expected = props.get(key, {}).get("type")
        if expected in types:
            if expected in ("integer", "number") and isinstance(value, bool):
                return f"字段 {key} 应该是 {expected}"
            if not isinstance(value, types[expected]):
                return f"字段 {key} 应该是 {expected}"
    return None


# ---------------------------------------------------------------- 结果截断

def bound_output(text: str, home: Home, tool_use_id: str) -> str:
    """超长的结果只放头尾进上下文，全文存进文件，模型需要时再去读。"""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    out_dir = home.root / ".outputs"
    out_dir.mkdir(exist_ok=True)
    full = out_dir / f"{tool_use_id}.txt"
    full.write_text(text, encoding="utf-8", newline="")
    omitted = len(text) - 2 * KEEP_CHARS
    return (text[:KEEP_CHARS]
            + f"\n\n……（中间省略 {omitted} 字。完整输出在 {home.rel(full)}）……\n\n"
            + text[-KEEP_CHARS:])


# ---------------------------------------------------------------- 文本编辑器（Anthropic 定义的工具）

TEXT_EDITOR_TOOL = {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}

# 同一个工具的普通写法，给不认识 Anthropic 定义工具的模型。名字和参数完全一样，执行代码共用
TEXT_EDITOR_CUSTOM = {
    "name": "str_replace_based_edit_tool",
    "description": (
        "查看、创建、修改主目录里的文本文件。路径相对主目录。"
        "view：看文件（带行号，可用 view_range 指定 [起始行, 结束行]，-1 表示到末尾）或列出目录；"
        "create：用 file_text 新建或整个覆盖文件；"
        "str_replace：把文件里唯一出现的 old_str 换成 new_str，空白和换行要完全一致；"
        "insert：在第 insert_line 行之后插入 insert_text（0 表示文件开头）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
            "insert_text": {"type": "string"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["command", "path"],
    },
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path.name} 不是 UTF-8 文本文件，可以用命令行处理")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _listing(home: Home, root: Path, depth: int = 2) -> str:
    lines = []

    def walk(d: Path, level: int):
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError as e:
            raise ToolError(str(e))
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                lines.append(f"{home.rel(p)}/")
                if level < depth:
                    walk(p, level + 1)
            else:
                lines.append(f"{home.rel(p)}  ({p.stat().st_size} 字节)")

    walk(root, 1)
    return "\n".join(lines) if lines else "（空目录）"


def text_editor(home: Home, data: dict) -> str:
    command = data.get("command")
    path = home.resolve(data.get("path"))

    if command == "view":
        if path.is_dir():
            return _listing(home, path)
        if not path.exists():
            raise ToolError(f"文件不存在：{home.rel(path)}")
        lines = _read_text(path).split("\n")
        start, end = 1, len(lines)
        view_range = data.get("view_range")
        if view_range:
            if not (isinstance(view_range, list) and len(view_range) == 2):
                raise ToolError("view_range 应该是 [起始行, 结束行]")
            start, end = int(view_range[0]), int(view_range[1])
            if end == -1:
                end = len(lines)
            if start < 1 or start > len(lines) or end < start:
                raise ToolError(f"view_range 超出范围，文件共 {len(lines)} 行")
        return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1))

    if command == "create":
        text = data.get("file_text")
        if not isinstance(text, str):
            raise ToolError("create 需要 file_text")
        existed = path.exists()
        if existed:
            if path.is_dir():
                raise ToolError(f"{home.rel(path)} 是目录")
            backup = home.root / ".backups" / home.rel(path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        _write_text(path, text)
        return f"已{'覆盖' if existed else '创建'} {home.rel(path)}"

    if command == "str_replace":
        old, new = data.get("old_str"), data.get("new_str", "")
        if not isinstance(old, str) or old == "":
            raise ToolError("str_replace 需要非空的 old_str")
        if not path.is_file():
            raise ToolError(f"文件不存在：{home.rel(path)}")
        text = _read_text(path)
        count = text.count(old)
        if count == 0:
            raise ToolError("old_str 在文件里没有找到，注意空白和换行要完全一致")
        if count > 1:
            raise ToolError(f"old_str 在文件里出现了 {count} 次，请多带一些上下文让它唯一")
        _write_text(path, text.replace(old, new or "", 1))
        return f"已修改 {home.rel(path)}"

    if command == "insert":
        line, text = data.get("insert_line"), data.get("insert_text")
        if not isinstance(line, int) or not isinstance(text, str):
            raise ToolError("insert 需要 insert_line（整数）和 insert_text")
        if not path.is_file():
            raise ToolError(f"文件不存在：{home.rel(path)}")
        lines = _read_text(path).split("\n")
        if line < 0 or line > len(lines):
            raise ToolError(f"insert_line 超出范围，文件共 {len(lines)} 行")
        lines[line:line] = text.split("\n")
        _write_text(path, "\n".join(lines))
        return f"已在 {home.rel(path)} 第 {line} 行后插入"

    raise ToolError(f"不支持的命令：{command}")


# ---------------------------------------------------------------- 命令行

EXEC_TOOL = {
    "name": "exec",
    "description": (
        "在你的电脑上执行一条 shell 命令，当前目录是主目录。返回退出码和输出。"
        "命令是一次性的，不保留上一条命令的当前目录和环境变量。"
        "装在主目录以外的东西不保证一直在；需要长期用的放在主目录里。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout_s": {"type": "integer", "description": "超时秒数，默认 120，最多 600"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def run_command(home: Home, data: dict) -> str:
    timeout = min(max(int(data.get("timeout_s", 120)), 1), 600)
    # 本次运行的通行证不传给命令。这只是卫生习惯：同一台电脑里的进程本来就互相看得见
    env = {k: v for k, v in os.environ.items() if k not in ("RUN_TOKEN", "HOST_URL")}
    env["HOME"] = str(home.root)
    try:
        proc = subprocess.run(data["command"], shell=True, cwd=home.root, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = _decode(e.stdout or b"") + _decode(e.stderr or b"")
        return f"超时（{timeout} 秒），命令被终止。已有的输出：\n{out}"
    parts = [f"退出码：{proc.returncode}"]
    out, err = _decode(proc.stdout), _decode(proc.stderr)
    if out:
        parts.append(f"--- 标准输出 ---\n{out.rstrip()}")
    if err:
        parts.append(f"--- 标准错误 ---\n{err.rstrip()}")
    return "\n".join(parts)


# ---------------------------------------------------------------- 经宿主接口的工具

RECALL_TOOL = {
    "name": "recall",
    "description": "搜索过去的对话记录（包括已经结束的会话）。返回包含全部关键词的片段，最新的在前。",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，多个用空格隔开"},
            "limit": {"type": "integer", "description": "最多返回几条，默认 10"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

SEND_FILE_TOOL = {
    "name": "send_file",
    "description": "把主目录里的一个文件发给用户，可以附一句说明。文件最大 20 MB。",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对主目录"},
            "caption": {"type": "string", "description": "附带的说明，可以不填"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}


NOTIFY_TOOL = {
    "name": "notify",
    "description": (
        "在后台运行里告诉用户一件事。前台运行（用户在等你）不需要它，直接回复就行。"
        "urgency=now 只用于用户现在就需要知道的事，每天名额很少；不急的用 digest，会攒到每天的摘要里。"
        "不调用就是保持安静，这是正常结果。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要告诉用户的话，简短，说清楚是什么事、要不要用户做什么"},
            "urgency": {"type": "string", "enum": ["now", "digest"]},
        },
        "required": ["text", "urgency"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

SET_TIMER_TOOL = {
    "name": "set_timer",
    "description": (
        "设一个定时器。follow_up：一次性的跟进，到时在当前这段对话里叫醒你，用 at 或 in_minutes 指定时间。"
        "schedule：按 cron（本地时间，五段：分 时 日 月 星期）重复，默认每次在新会话里叫醒你，最多每 15 分钟一次。"
        "醒来时你不一定记得细节，task 和 criteria 要写清楚。"
        "有固定做法的例行任务：做法写进技能（skill），task 只写这次做什么。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["follow_up", "schedule"]},
            "name": {"type": "string", "description": "一句话说明，用户会看到"},
            "task": {"type": "string", "description": "到时候要做什么"},
            "criteria": {"type": "string", "description": "凭什么判断（例如什么情况下要告诉用户）"},
            "progress_file": {"type": "string", "description": "相关的进度文件路径"},
            "at": {"type": "string", "description": "follow_up 的时间，本地时间 \"YYYY-MM-DD HH:MM\""},
            "in_minutes": {"type": "number", "description": "follow_up：多少分钟之后"},
            "cron": {"type": "string", "description": "schedule 的 cron 表达式，例如 \"0 8 * * *\""},
            "precheck": {"type": "string", "description": "schedule 的前置检查脚本（主目录里的路径）。"
                                                          "退出码 0 = 有变化叫醒你，10 = 没变化"},
            "skill": {"type": "string", "description": "例行任务：到时按哪个技能做（skills/ 下的目录名，要已存在）"},
            "same_session": {"type": "boolean", "description": "schedule：每次接着同一个会话（能看到上几次的对话），"
                                                              "接满若干次自动换新会话。默认 false"},
        },
        "required": ["kind", "name", "task"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

LIST_TIMERS_TOOL = {
    "name": "list_timers",
    "description": "列出进行中的定时器（包括用户设的和系统内置的）。",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

CANCEL_TIMER_TOOL = {
    "name": "cancel_timer",
    "description": "取消一个你设的定时器。",
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}


CONNECTION_REQUEST_TOOL = {
    "name": "connection_request",
    "description": (
        "请用户连接一个新的外部账号（API 令牌）。你拿不到真令牌：用户在宿主终端里输入，你之后只会看到替身令牌，"
        "而且它只在发往 hosts 里的主机时才生效。用户连接好后会有一条 [连接] 消息叫醒你。"
        "先读对方的接口文档，弄清主机名和认证方式再请求。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "服务名，小写字母开头，如 tickets"},
            "title": {"type": "string", "description": "给用户看的名字，如 工单系统"},
            "hosts": {"type": "array", "items": {"type": "string"}, "description": "接口的主机名，1 到 5 个"},
            "auth": {"type": "object", "description": "令牌放在哪：{\"header\": \"Authorization\", \"scheme\": \"Bearer\"}"
                                                     " 或 {\"header\": \"X-Api-Key\"} 或 {\"query\": \"api_key\"}"},
            "why": {"type": "string", "description": "要它做什么，会显示给用户"},
        },
        "required": ["service", "hosts", "why"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

PROFILE_PROPOSE_TOOL = {
    "name": "profile_propose",
    "description": (
        "给一个已连接的服务提交服务档案，或者给一个在浏览器里登录过的网站提交网站档案（YAML，type: site），"
        "请用户审阅。档案决定哪些请求直接放行（read）、哪些要问用户、"
        "审批里显示什么。用户确认后才生效，之后会有一条 [审阅] 消息叫醒你。"
        "格式参考 <memory> 里已连接服务的说明；类别只有 read / write / send / irreversible / money。"
        "只有不改变任何东西的请求才能标成 read。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {"yaml": {"type": "string", "description": "完整的档案 YAML"}},
        "required": ["yaml"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

WEBHOOK_REQUEST_TOOL = {
    "name": "webhook_request",
    "description": (
        "申请开通一个 Webhook 来源，让外部系统有动静时叫醒你（例行任务）。用户确认后宿主生成签名密钥，"
        "由用户去对方系统里配置；你看不到密钥。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "来源名，小写字母开头，如 tickets"},
            "title": {"type": "string", "description": "给用户看的名字"},
            "correlation": {"type": "string", "description": "请求体里关联键的路径，如 ticket.id；同一个键的事件进同一会话"},
            "skill": {"type": "string", "description": "处理这类事件时用的技能（skills/ 下的目录名）"},
        },
        "required": ["name", "title"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}


BROWSER_TOOL = {
    "name": "browser",
    "description": (
        "操控一个浏览器（另一台机器，登录态在那边）。用于没有接口、只能在网页上做的事。\n"
        "- open {url}：打开地址。snapshot：取当前页面的无障碍树，可操作的节点带 [ref=eN]。\n"
        "- click {ref} / type {ref, text, submit?} / press {key} / back：操作页面，之后返回新的无障碍树。\n"
        "- screenshot：截图。login {url, reason}：请用户在接管页面上亲手登录，你看不到密码；登录完会有一条 [浏览器] 消息叫醒你。\n"
        "浏览器发出的请求都经过出网代理：提交表单、打开自己拼的地址要用户批准，被拦下时这一轮就结束。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "snapshot", "click", "type", "press", "back",
                                                  "screenshot", "login"]},
            "url": {"type": "string", "description": "open / login 用的完整地址"},
            "ref": {"type": "string", "description": "click / type 的节点编号，如 e12"},
            "text": {"type": "string", "description": "type 要输入的文字（会替换原有内容）"},
            "submit": {"type": "boolean", "description": "type 之后按回车"},
            "key": {"type": "string", "description": "press 的按键名，如 Enter、Tab、Escape"},
            "reason": {"type": "string", "description": "login：为什么需要登录，会显示给用户"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def browser(host, data: dict):
    """返回文字，或者（截图时）文字加图片的内容块列表。"""
    result = host("browser", data)
    if result.get("image_b64"):
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": result["image_b64"]}},
                {"type": "text", "text": result.get("text", "")}]
    return result.get("text", "")


def connection_request(host, data: dict) -> str:
    result = host("connection_request", data)
    return (f"已把连接请求发给用户（{result['request_id']}）。用户在宿主上输入令牌后会有一条 [连接] 消息叫醒你。"
            "在那之前不要假装已经连上。")


def profile_propose(host, data: dict) -> str:
    result = host("profile_propose", data)
    return f"已提交给用户审阅（{result['proposal_id']}）。确认之前按默认规则：读放行，其余每次问用户。"


def webhook_request(host, home: Home, data: dict) -> str:
    check_skill(home, data.get("skill"))
    result = host("webhook_request", data)
    return f"已提交给用户审阅（{result['proposal_id']}）。"


HANDOFF_TOOL = {
    "name": "handoff",
    "description": (
        "把一个任务交给另一个角色（角色列表见系统说明里的“角色”一节）。对方在自己的新会话里处理，"
        "看不到这段对话，只看到 task，所以 task 要自包含：目标、已知信息、材料文件的路径（放在 work/ 下）、"
        "做完之后交给谁或者直接回复用户。交接次数和总花费都有上限。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "目标角色的名字"},
            "task": {"type": "string", "description": "交给对方的任务说明"},
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}


KEEP_AWAKE_TOOL = {
    "name": "keep_awake",
    "description": (
        "请求电脑暂时不要挂起。电脑在没有运行时空闲一阵就会被挂起，里面的进程全部冻住。"
        "在后台起一个要跑很久的任务之前调用它，写明原因和预计要多少分钟；上限由宿主定（默认 4 小时），到点照常挂起。"
        "同时用 set_timer 设一个跟进，到时候醒来看结果。minutes=0 表示撤销。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "为什么要保持清醒"},
            "minutes": {"type": "number", "description": "预计要多少分钟"},
        },
        "required": ["minutes"],
        "additionalProperties": False,
    },
}


def keep_awake(host, data: dict) -> str:
    result = host("keep_awake", {"reason": data.get("reason", ""), "minutes": data["minutes"]})
    if result["until"] is None:
        return "已撤销，电脑空闲后会照常挂起。"
    note = f"（超过上限 {result['max_minutes']} 分钟，被截短了）" if result["capped"] else ""
    return f"电脑会保持清醒到 {result['until']}（本地时间）{note}。之后照常挂起。"


def handoff(host, data: dict) -> str:
    result = host("handoff", {"role": data["role"], "task": data["task"]})
    if result.get("duplicate"):
        return "这次交接之前已经发过了，没有重复发。"
    return (f"已交给「{result['title']}」（第 {result['hops']} 跳）。对方会在自己的会话里处理；"
            "你这边简短告诉用户交给了谁就可以结束了。")


def notify(host, data: dict) -> str:
    if data.get("urgency") not in ("now", "digest"):
        raise ToolError("urgency 只能是 now 或 digest")
    result = host("send", {"text": data["text"], "urgency": data["urgency"]})
    return "重复的消息，已忽略" if result.get("duplicate") else "已交给发件箱"


def check_skill(home: Home, name) -> None:
    """定时器和 Webhook 指向的技能要真的存在，不然到时候 Agent 醒来找不到做法。"""
    if not name:
        return
    if not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
        raise ToolError("skill 是 skills/ 下的目录名")
    if not (home.root / "skills" / name / "SKILL.md").is_file():
        raise ToolError(f"没有 skills/{name}/SKILL.md。先把技能写好，再设定时器")


def set_timer(host, home: Home, data: dict) -> str:
    check_skill(home, data.get("skill"))
    t = host("timer_set", data)["timer"]
    return f"已设定 {t['id']}：{t['name']}，下次 {t['due']}（本地时间）"


def list_timers(host, data: dict) -> str:
    timers = host("timer_list", {})["timers"]
    if not timers:
        return "没有进行中的定时器"
    return "\n".join(f"{t['id']} [{t['kind']}，{t['created_by']}] {t['name']}，下次 {t['due']}"
                     + (f"，cron {t['cron']}" if t["cron"] else "")
                     + (f"，前置检查 {t['precheck']}" if t["precheck"] else "") for t in timers)


def cancel_timer(host, data: dict) -> str:
    host("timer_cancel", {"id": data["id"]})
    return f"已取消 {data['id']}"


def recall(host, data: dict) -> str:
    found = host("recall", {"query": data["query"], "limit": data.get("limit", 10)})["results"]
    if not found:
        return "没有找到。可以换个关键词，或者少用几个关键词。"
    lines = [f"[{r['time']} {r['role']} 会话 {r['session_id']}]\n{r['snippet']}" for r in found]
    return "\n\n".join(lines)


def web_search(host, data: dict) -> str:
    found = host("web_search", {"query": data.get("query")})["results"]
    if not found:
        return "没有搜到结果，换个关键词试试。"
    lines = [f"{i}. {r['title']}\n{r['url']}" + (f"（{r['date']}）" if r.get("date") else "") + f"\n{r['snippet']}"
             for i, r in enumerate(found, 1)]
    return "\n\n".join(lines)


def send_file(host, home: Home, data: dict) -> str:
    path = home.resolve(data["path"])
    if not path.is_file():
        raise ToolError(f"文件不存在：{home.rel(path)}")
    size = path.stat().st_size
    if size > MAX_SEND_BYTES:
        raise ToolError(f"文件 {size} 字节，超过 20 MB 上限")
    sent = host("send_file", {"name": path.name, "caption": data.get("caption", ""),
                              "data_b64": base64.b64encode(path.read_bytes()).decode("ascii")})
    note = sent.get("note") if isinstance(sent, dict) else ""
    return f"已把 {home.rel(path)} 交给发件箱" + (f"。{note}" if note else "")


# ---------------------------------------------------------------- 上网

# Anthropic 的服务端网页搜索。搜索结果里的地址会被宿主记为"见过"，之后可以直接 fetch。
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

# 同名的普通工具：模型没有服务端搜索时，由宿主代为搜索。结果里的地址同样记为"见过"
WEB_SEARCH_HOST_TOOL = {
    "name": "web_search",
    "description": "搜索网页，返回标题、地址和摘要。摘要不完整，要细读就用 fetch 打开结果里的地址，这些地址不需要批准。想找某个具体页面（比如官方定价页）时，用它搜出地址，不要自己猜。",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索关键词，最长 300 字"}},
        "required": ["query"],
    },
}

FETCH_TOOL = {
    "name": "fetch",
    "description": (
        "通过网络取一个网页或接口，网页会转成文字，末尾列出页面里的链接。"
        "搜索结果里的地址、网页里的链接、用户发来的地址可以直接 GET，要原样使用；"
        "凭记忆、猜测或自己拼出来的地址和 POST 等会向外发数据的请求需要用户批准，"
        "所以想找某个页面时先用 web_search，不要猜网址。"
        "非文本的内容会存到 work/downloads/。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整地址"},
            "method": {"type": "string", "description": "默认 GET"},
            "body": {"type": "string", "description": "请求体，用于 POST 等"},
            "content_type": {"type": "string", "description": "请求体的类型，默认 application/json"},
            "purpose": {"type": "string", "description": "一句话说明为什么要访问，需要批准时会显示给用户"},
            "service": {"type": "string", "description": "调用已连接的服务时填服务名，认证会自动带上"},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    "eager_input_streaming": True,
}

MAX_FETCH_BYTES = 10 * 1024 * 1024
MAX_LINKS_SHOWN = 80


class _PageText(html.parser.HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self._href, self._anchor = urllib.parse.urljoin(self.base, href), []

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((" ".join("".join(self._anchor).split())[:80], self._href))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _readable(node):
    """只为显示：把 data/raw 字段里 base64url 编码的文字解开（Gmail 的正文就是这样编码的）。"""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("data", "raw") and isinstance(v, str) and len(v) >= 8:
                try:
                    decoded = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode("utf-8")
                    out[k] = f"（已解码）{decoded}"
                    continue
                except (ValueError, UnicodeDecodeError):
                    pass
            out[k] = _readable(v)
        return out
    if isinstance(node, list):
        return [_readable(v) for v in node]
    return node


def _describe(url: str, status: int, headers, raw: bytes, home: Home) -> str:
    ctype = (headers.get("content-type") or "").lower()
    head = f"HTTP {status} {url}"
    if headers.get("x-smra-egress") == "blocked":
        return f"出网代理拦下了这个请求（HTTP {status}）：{raw.decode('utf-8', 'replace')}"
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    if "html" in ctype:
        page = _PageText(url)
        page.feed(raw.decode(charset, "replace"))
        links = "\n".join(f"- {t or '（无文字）'} — {u}" for t, u in page.links[:MAX_LINKS_SHOWN])
        more = f"\n……另有 {len(page.links) - MAX_LINKS_SHOWN} 个链接" if len(page.links) > MAX_LINKS_SHOWN else ""
        return f"{head}\n\n{page.text()}\n\n链接：\n{links or '（没有）'}{more}"
    if "json" in ctype:
        try:
            shown = json.dumps(_readable(json.loads(raw.decode(charset, "replace"))), ensure_ascii=False, indent=1)
            return f"{head}\n\n{shown}"
        except ValueError:
            pass
    if ctype.startswith("text/") or "json" in ctype or "xml" in ctype or not ctype:
        return f"{head}\n\n{raw.decode(charset, 'replace')}"
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or "download"
    target = home.root / "work" / "downloads" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return f"{head}\n\n{ctype} 内容 {len(raw)} 字节，已存到 {home.rel(target)}"


def fetch(home: Home, data: dict) -> str:
    """经出网代理访问网络。代理地址和要信任的 CA 来自环境变量。"""
    method = (data.get("method") or "GET").upper()
    body = data.get("body")
    headers = {"accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    if body is not None:
        headers["content-type"] = data.get("content_type") or "application/json"
    if data.get("purpose"):
        # 请求头只能放 Latin-1，中文要先百分号编码；代理那边再解开
        headers["x-purpose"] = urllib.parse.quote(data["purpose"][:300], safe="")
    if data.get("service"):
        conn = CONNECTIONS.get(data["service"])
        if conn is None:
            raise ToolError(f"没有连接服务 {data['service']}。已连接的：{', '.join(CONNECTIONS) or '（没有）'}")
        # 放的是替身令牌。只有发往这个服务的主机、而且出网代理放行时，才会被换成真凭证
        auth = conn["auth"]
        if "header" in auth:
            scheme = auth.get("scheme")
            headers[auth["header"].lower()] = f"{scheme} {conn['surrogate']}" if scheme else conn["surrogate"]
        else:
            sep = "&" if "?" in data["url"] else "?"
            data = {**data, "url": f"{data['url']}{sep}{auth['query']}={conn['surrogate']}"}
    request = urllib.request.Request(data["url"], data=body.encode("utf-8") if body is not None else None,
                                     headers=headers, method=method)
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(),
                                         urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=60) as response:
            return _describe(response.geturl(), response.status, response.headers,
                             response.read(MAX_FETCH_BYTES), home)
    except urllib.error.HTTPError as e:
        return _describe(e.geturl() or data["url"], e.code, e.headers, e.read(MAX_FETCH_BYTES), home)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ToolError(f"访问失败：{e}")


ALL_FEATURES = frozenset({"text_editor", "web_search"})


def tool_definitions(exec_allowed: bool, features=ALL_FEATURES) -> list[dict]:
    # 工具列表在会话内不变（前台、后台运行都给全），否则提示词缓存和历史校验都会失效。
    # 模型提供方不支持 Anthropic 定义的工具时：文本编辑器换成同名的普通工具，服务端网页搜索不提供
    editor = TEXT_EDITOR_TOOL if "text_editor" in features else TEXT_EDITOR_CUSTOM
    tools = [editor, FETCH_TOOL, WEB_SEARCH_TOOL, RECALL_TOOL, SEND_FILE_TOOL,
             NOTIFY_TOOL, SET_TIMER_TOOL, LIST_TIMERS_TOOL, CANCEL_TIMER_TOOL,
             CONNECTION_REQUEST_TOOL, PROFILE_PROPOSE_TOOL, WEBHOOK_REQUEST_TOOL, BROWSER_TOOL, HANDOFF_TOOL,
             KEEP_AWAKE_TOOL]
    if "web_search" not in features:
        where = tools.index(WEB_SEARCH_TOOL)
        tools[where:where + 1] = [WEB_SEARCH_HOST_TOOL] if "host_search" in features else []
    if exec_allowed:
        tools.insert(1, EXEC_TOOL)
    return tools
