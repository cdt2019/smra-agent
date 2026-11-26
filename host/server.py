"""穿过边界的两个出口（第 1 步）：模型代理和宿主接口。

电脑里的程序只带着本次运行的通行证来调用这里。真密钥在这一侧加上，不进电脑。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout, web

from . import models, quality, sessions
from . import search as web_search_backend
from .browser import BrowserError
from .creds import describe_auth
from .db import new_id, now
from .egress.services import SERVICE_NAME, valid_host
from .egress.urls import normalize
from .files import safe_name
from .guard import new_tool_images, new_tool_texts, strip_images
from .proposals import SKILL_NAME, ProposalError
from .runs import RunCtx
from .status import tokens_today as spent_today
from .timers import TimerError, local_str, parse_local

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("server")

# 电脑上报会话记录时允许的类型。user_message 只能由宿主写入。
SELF_EVENT_TYPES = {"assistant_message", "tool_result", "context_note"}
_ROLE_OF = {"assistant_message": "assistant", "tool_result": "user", "context_note": "user"}

MAX_SEND_FILE_BYTES = 20 * 1024 * 1024

# 转发给模型提供方的请求头白名单。x-api-key 不在其中，由代理另行加上。
FORWARD_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta")

# 电脑能在模型请求里声明的工具。自定义工具（没有 type）都可以；服务端工具只允许网页搜索。
# 服务端抓取网页（web_fetch）、代码执行、MCP 连接器这些会让流量绕过出网代理，一律不放行。
ALLOWED_TOOL_TYPES = {None, "custom", "text_editor_20250728", "web_search_20260209"}
FORBIDDEN_PARAMS = ("mcp_servers", "container")


def check_model_request(body: dict) -> str | None:
    """返回拒绝原因，没问题返回 None。"""
    for key in FORBIDDEN_PARAMS:
        if body.get(key):
            return f"不允许使用参数 {key}"
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") not in ALLOWED_TOOL_TYPES:
            kind = tool.get("type") if isinstance(tool, dict) else type(tool).__name__
            return f"不允许使用工具类型 {kind}"
    return None


def search_result_urls(body: bytes, content_type: str) -> set[str]:
    """从模型响应里取出网页搜索结果的地址。只取搜索结果块，不取模型自己写的文字。"""
    blocks: list[dict] = []
    try:
        if "text/event-stream" in content_type:
            for line in body.decode("utf-8", "replace").splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:])
                    if data.get("type") == "content_block_start":
                        blocks.append(data.get("content_block") or {})
        else:
            blocks = json.loads(body).get("content") or []
    except (ValueError, AttributeError):
        return set()
    urls: set[str] = set()
    for block in blocks:
        if block.get("type") != "web_search_tool_result" or not isinstance(block.get("content"), list):
            continue
        for item in block["content"]:
            if isinstance(item, dict) and item.get("type") == "web_search_result":
                n = normalize(str(item.get("url") or ""))
                if n:
                    urls.add(n)
    return urls


def _error(status: int, type_: str, message: str) -> web.Response:
    # 与模型接口相同的错误格式，电脑里的 SDK 能据此抛出对应的异常
    return web.json_response({"type": "error", "error": {"type": type_, "message": message}}, status=status)


def _token(request: web.Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else None


def _usage(body: bytes, content_type: str) -> dict:
    """模型响应里的 usage。解析不了就是空的。"""
    usage: dict = {}
    try:
        if "text/event-stream" in content_type:
            for line in body.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:])
                if data.get("type") == "message_start":
                    usage.update(data["message"].get("usage") or {})
                elif data.get("type") == "message_delta":
                    # 结束事件里没更新的字段可能是 null，不能盖掉开始事件里的值
                    usage.update({k: v for k, v in (data.get("usage") or {}).items() if v is not None})
        else:
            usage = json.loads(body).get("usage") or {}
    except (ValueError, KeyError, AttributeError):
        return {}
    return usage if isinstance(usage, dict) else {}


def _context_tokens(usage: dict) -> int | None:
    """这次调用模型读到的输入总长（新输入 + 缓存写入 + 缓存读取）。没有用量就是 None。"""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    try:
        total = sum(int(usage.get(k) or 0) for k in keys)
    except (TypeError, ValueError):
        return None
    return total or None


def _usage_tokens(body: bytes, content_type: str, cache_read_weight: float) -> int:
    """从模型响应里取出本次计入预算的用量。解析不了就按 0 记。

    每一轮都会把整段历史再读一遍，缓存读取的量随轮数平方增长，单价却只有输入的一成左右。
    按全价算，一个正常的长任务也会撞上预算，所以它按权重折算。"""
    usage = _usage(body, content_type)
    keys = ("input_tokens", "cache_creation_input_tokens", "output_tokens")
    full = sum(int(usage.get(k) or 0) for k in keys)
    return full + math.ceil(int(usage.get("cache_read_input_tokens") or 0) * cache_read_weight)


def build(app: App) -> web.Application:
    cfg = app.cfg

    def require_run(request: web.Request) -> RunCtx:
        run = app.runs.agent_by_token(_token(request))
        if run is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"type": "error", "error": {
                    "type": "authentication_error", "message": "无效或已过期的运行通行证"}}),
                content_type="application/json",
            )
        return run

    def tokens_today() -> int:
        return spent_today(app.db)

    def report_chain_stop(run: RunCtx, why: str, detail: str = "") -> None:
        """交接链被上限终止时告诉你。同一串交接只说一次。"""
        app.outbox.enqueue(
            "system", run.session_id,
            f"{why}，已停止。\n{detail}需要的话你直接对相应的角色说（@角色名），让它接着做。",
            f"chainstop:{run.chain_root}")

    # ---------- 出口一：模型代理 ----------

    async def screen_for_injection(run: RunCtx, body: dict) -> None:
        """转发前检查新读到的外部内容。检查失败按命中处理：宁可多问你几次。"""
        if app.injection is None or run.suspended:
            return
        texts = new_tool_texts(body)
        for own in run.host_texts:      # 宿主自己写的说明（比如登录后"这一轮到此结束"）不是外部内容
            texts = [t.replace(own, "") for t in texts]
        texts = [t for t in texts if t.strip()]
        # 工具结果里的图片只有按 Anthropic 协议转发时才会到主模型（OpenAI 协议的翻译把它们换成了文字）
        images = new_tool_images(body) if cfg.model_protocol == "anthropic" else []
        if not texts and not images:
            return
        try:
            flagged, reason = await (app.injection.check(texts, images) if images else app.injection.check(texts))
        except Exception as e:
            log.warning("注入检测失败，按命中处理：%s", e)
            flagged, reason = True, f"检测本身失败了（{type(e).__name__}）"
        if not flagged:
            return
        run.suspended = True
        log.warning("运行 %s 读到了疑似注入的内容：%s", run.id, reason)
        app.outbox.enqueue(
            "system", run.session_id,
            "提醒：Agent 刚读到的外部内容里可能有针对它的指令（疑似提示注入）。"
            f"\n判断依据：{reason[:200]}\n这次运行里，之前给过的长期授权暂停使用，所有对外操作都会单独问你。",
            f"injection:{run.id}",
        )

    def over_budget(run: RunCtx) -> web.Response | None:
        if run.tokens >= cfg.run_token_budget:
            return _error(403, "permission_error", "本次运行的 token 预算已用完")
        if tokens_today() >= cfg.daily_token_budget:
            return _error(403, "permission_error", "今天的 token 预算已用完")
        if app.runs.chain_tokens(run.chain_root) >= cfg.task_token_budget:
            report_chain_stop(run, f"这个任务（含角色之间的交接）的花费到了上限（{cfg.task_token_budget} token）")
            return _error(403, "permission_error", "这个任务的总 token 预算已用完")
        return None

    async def _openai_call(parsed: dict) -> tuple[int, str, bytes]:
        """按 OpenAI 协议调用，结果翻译回 Anthropic 格式。返回 (状态码, 内容类型, 响应体)。"""
        payload = models.to_openai(parsed, cfg.model)
        headers = {"content-type": "application/json", **models.auth_headers("openai", cfg.api_key)}
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=900)
        assert app.http is not None
        async with app.http.post(models.endpoint("openai", cfg.model_upstream), json=payload,
                                 headers=headers, timeout=timeout) as upstream:
            text = await upstream.text()
            status = upstream.status
        if status == 200:
            try:
                out = models.from_openai(json.loads(text), cfg.model)
            except (ValueError, AttributeError) as e:
                status, out = 502, models.error_body(502, f"模型提供方的响应解析不了：{e}")
        else:
            out = models.error_body(status, text)
        return status, "application/json", json.dumps(out, ensure_ascii=False).encode()

    async def model_proxy(request: web.Request) -> web.StreamResponse:
        run = require_run(request)
        tail = request.match_info["tail"]
        if not tail.startswith("v1/messages"):
            return _error(404, "not_found_error", "模型代理只转发 v1/messages")
        if not cfg.api_key:
            return _error(401, "authentication_error", "宿主侧没有配置模型密钥")
        if over := over_budget(run):
            return over

        body = await request.read()
        try:
            parsed = json.loads(body)
            problem = check_model_request(parsed)
        except ValueError:
            parsed, problem = None, "请求体不是 JSON"
        if problem:
            return _error(400, "permission_error", problem)
        if cfg.model_protocol == "anthropic" and "vision" not in cfg.model_features and strip_images(parsed):
            body = json.dumps(parsed, ensure_ascii=False).encode()
        await screen_for_injection(run, parsed)
        if cfg.model_protocol == "openai":
            if parsed.get("stream"):
                return _error(400, "invalid_request_error", "这个模型提供方按 OpenAI 协议接入，不支持流式")
            status, content_type, answer = await _openai_call(parsed)
            collected = bytearray(answer)
            response: web.StreamResponse = web.json_response(json.loads(collected), status=status)
        else:
            headers = {k: request.headers[k] for k in FORWARD_HEADERS if k in request.headers}
            headers.update(models.auth_headers("anthropic", cfg.api_key))     # 真密钥只在这里出现
            url = f"{cfg.model_upstream}/{tail}"
            if request.query_string:
                url += "?" + request.query_string

            collected = bytearray()
            timeout = ClientTimeout(total=None, sock_connect=30, sock_read=600)
            assert app.http is not None
            async with app.http.post(url, data=body, headers=headers, timeout=timeout) as upstream:
                content_type = upstream.headers.get("content-type", "application/json")
                out_headers = {"content-type": content_type}
                if "request-id" in upstream.headers:
                    out_headers["request-id"] = upstream.headers["request-id"]
                response = web.StreamResponse(status=upstream.status, headers=out_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    collected += chunk
                    await response.write(chunk)
                await response.write_eof()
                status = upstream.status

        # 留底：这份记录是宿主自己写的，不依赖电脑里的程序如实上报
        tokens = _usage_tokens(bytes(collected), content_type, cfg.cache_read_weight) if status == 200 else 0
        context = _context_tokens(_usage(bytes(collected), content_type)) if status == 200 else None
        app.db.execute(
            "INSERT INTO inference_log (run_id, status, request, response, tokens, context_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run.id, status, body.decode("utf-8", "replace"), collected.decode("utf-8", "replace"),
             tokens, context, now()),
        )
        app.runs.add_tokens(run, tokens)
        if status == 200:
            # 搜索结果里的地址来自搜索引擎，不是 Agent 拼的，之后可以直接访问
            app.adjudicator.add_seen(search_result_urls(bytes(collected), content_type), "search")
        return response

    # ---------- 出口三：宿主接口 ----------

    async def run_context(request: web.Request) -> web.Response:
        run = require_run(request)
        info = sessions.info(app.db, run.session_id)
        agent_md = ((info["agent_md"] if info else None) or "").strip()
        role_md = ((info["role_md"] if info else None) or "").strip()
        system = cfg.base_system + (f"\n\n{agent_md}" if agent_md else "") + (f"\n\n{role_md}" if role_md else "")
        # 已连接的服务：电脑拿到的只有替身令牌
        connections = [{"service": p.service, "title": p.title, "hosts": p.hosts, "auth": p.auth,
                        "usage": p.usage, "surrogate": c.surrogate, "reviewed": p.reviewed,
                        "expired": bool(c.expired_at)}
                       for p, c in app.services.connected()]
        return web.json_response({
            "run_id": run.id,
            "session_id": run.session_id,
            "trigger": run.trigger,
            "foreground": run.foreground,
            "model": cfg.model,
            "effort": cfg.effort,
            "features": sorted(cfg.model_features | ({"host_search"} if cfg.search_provider else set())),
            "max_tokens": cfg.max_tokens,
            "system": system,
            "messages": sessions.messages(app.db, run.session_id),
            "last_context_note": sessions.last_context_note(app.db, run.session_id),
            "recent_notices": app.outbox.recent_notices(),
            "connections": connections,
            "role": run.role,
            "memory_dir": app.roles.memory_dir(run.role),
        })

    async def log_append(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        type_, payload = data.get("type"), data.get("payload")
        if type_ not in SELF_EVENT_TYPES or not isinstance(payload, dict) \
                or payload.get("role") != _ROLE_OF[type_] or "content" not in payload:
            return _error(400, "invalid_request_error", "不支持的记录")
        sessions.append(app.db, run.session_id, run.id, "self", type_, payload)
        return web.json_response({"ok": True})

    def _kind(run: RunCtx, data: dict) -> str:
        # 前台运行的消息就是回复；后台运行由 Agent 选节奏，分发器再按上限和安静时段调整
        return "reply" if run.foreground else ("now" if data.get("urgency") == "now" else "digest")

    def _signed(run: RunCtx, text: str) -> str:
        """非默认角色说的话，开头标上是谁。由宿主加，角色自己改不了。"""
        if not run.role or run.role == app.roles.default_name or not app.roles.multi:
            return text
        return f"【{app.roles.title_of(run.role)}】{text}"

    def researched(run: RunCtx) -> bool:
        """这次运行上过网：搜索过，或者有请求被放行。"""
        return run.searches > 0 or app.db.execute(
            "SELECT 1 FROM egress_log WHERE run_id = ? AND verdict = 'allow' LIMIT 1", (run.id,)).fetchone() is not None

    async def send(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        text = str(data.get("text") or "").strip()
        if not text:
            return _error(400, "invalid_request_error", "消息为空")
        if warning := quality.reply_warning(text, researched(run)):
            text = f"{text}\n\n{warning}"
        text = _signed(run, text)
        # 去重键 = 触发事件 + 序号。同一批事件重试时，第 n 条消息不会被发第二次
        fresh = app.outbox.enqueue(_kind(run, data), run.session_id, text, f"{run.trigger_key}:{run.sends}")
        run.sends += 1
        return web.json_response({"ok": True, "duplicate": not fresh})

    async def send_file(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        try:
            blob = base64.b64decode(data.get("data_b64") or "", validate=True)
        except (ValueError, binascii.Error):
            return _error(400, "invalid_request_error", "文件内容不是合法的 base64")
        if len(blob) > MAX_SEND_FILE_BYTES:
            return _error(400, "invalid_request_error", "文件超过 20 MB")
        name = safe_name(str(data.get("name") or "file"))
        dedupe = f"{run.trigger_key}:{run.sends}"
        if app.db.execute("SELECT 1 FROM outbox WHERE dedupe_key = ?", (dedupe,)).fetchone():
            run.sends += 1
            return web.json_response({"ok": True, "duplicate": True})
        caption = str(data.get("caption") or "")
        problems = quality.check(app.db, name, blob, researched(run))
        if problems and name not in run.quality_bounced:
            run.quality_bounced.add(name)       # 只退回一次，不会把文件卡死
            log.info("发送前检查退回 %s：%d 个未见过的链接，%d 个无来源的价格小节，%d 处趋势说法",
                     name, len(problems.unseen), len(problems.unsourced), len(problems.trends))
            return _error(422, "invalid_request_error", problems.for_agent())
        if problems:
            caption = f"{problems.for_user()}\n{caption}".strip()
        run.sends += 1
        out_dir = cfg.data_dir / "outgoing"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{run.id}-{run.sends}-{name}"
        path.write_bytes(blob)
        app.outbox.enqueue(_kind(run, data), run.session_id, caption, dedupe, attachment=str(path))
        return web.json_response({"ok": True, "duplicate": False, "note": quality.REPLY_REMINDER})

    async def recall(request: web.Request) -> web.Response:
        require_run(request)
        data = await request.json()
        limit = data.get("limit", 10)
        results = sessions.search(app.db, str(data.get("query") or ""), int(limit) if isinstance(limit, int) else 10)
        return web.json_response({"results": results})

    async def web_search(request: web.Request) -> web.Response:
        """宿主一侧的网页搜索。电脑只给关键词；结果里的地址记为见过，之后可以直接 fetch。"""
        run = require_run(request)
        if not cfg.search_provider:
            return _error(400, "invalid_request_error", "没有配置网页搜索")
        data = await request.json()
        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error(400, "invalid_request_error", "query 不能为空")
        query = query.strip()
        if len(query) > web_search_backend.MAX_QUERY_CHARS:
            return _error(400, "invalid_request_error", f"关键词最长 {web_search_backend.MAX_QUERY_CHARS} 字")
        if run.searches >= cfg.search_max_per_run:
            return _error(429, "rate_limit_error", f"这次运行已经搜索了 {cfg.search_max_per_run} 次，到上限了")
        if over := over_budget(run):
            return over
        run.searches += 1
        try:
            assert app.http is not None
            results, sent, raw = await web_search_backend.search(app.http, cfg, query)
        except web_search_backend.SearchError as e:
            return _error(502, "api_error", str(e))
        tokens = _usage_tokens(raw, "application/json", cfg.cache_read_weight)
        app.db.execute(
            "INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (run.id, 200, sent, raw.decode("utf-8", "replace"), tokens, now()))
        app.runs.add_tokens(run, tokens)
        app.adjudicator.add_seen([normalize(r["url"]) for r in results], "search")
        return web.json_response({"results": results})

    async def should_yield(request: web.Request) -> web.Response:
        run = require_run(request)
        if run.deferred:
            # 有请求在等你批准：这次运行先结束，你处理后再接着做
            return web.json_response({"yield": True, "reason": "deferred", "approvals": run.deferred})
        # 后台运行在你发来消息时让路
        if run.trigger != "user" and app.inbox.has_pending_user():
            return web.json_response({"yield": True, "reason": "user"})
        return web.json_response({"yield": False})

    def _timer_view(row) -> dict:
        payload = json.loads(row["payload"])
        return {"id": row["id"], "kind": row["kind"], "name": row["name"], "due": local_str(row["due_at"]),
                "cron": row["cron"], "precheck": row["precheck"], "created_by": row["created_by"],
                "skill": payload.get("skill"), "same_session": bool(payload.get("same_session"))}

    async def timer_set(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        at = app.scheduler.clock()
        kind = data.get("kind")
        try:
            due_at = None
            if kind == "follow_up":
                if data.get("at"):
                    due_at = parse_local(str(data["at"]))
                elif isinstance(data.get("in_minutes"), (int, float)) and not isinstance(data["in_minutes"], bool):
                    due_at = at + timedelta(minutes=data["in_minutes"])
                else:
                    raise TimerError("跟进要给 at（本地时间）或 in_minutes")
            precheck = data.get("precheck") or None
            if precheck:
                precheck = str(precheck).replace("\\", "/").lstrip("/")
                if not app.computer.exec_allowed:
                    raise TimerError("这台电脑不能运行脚本，前置检查不可用")
                if ".." in precheck.split("/") or precheck.startswith("builtin:"):
                    raise TimerError("前置检查要是主目录里的一个脚本路径")
                if kind != "schedule":
                    raise TimerError("只有日程能带前置检查")
            payload: dict[str, Any] = {k: str(data[k])[:2000] for k in ("task", "criteria", "progress_file", "skill") if data.get(k)}
            if not payload.get("task"):
                raise TimerError("要写清楚到时候做什么（task）")
            if payload.get("skill") and not SKILL_NAME.fullmatch(payload["skill"]):
                raise TimerError("skill 是 skills/ 下的目录名（小写字母、数字、- 和 _）")
            if data.get("same_session") is True:
                if kind != "schedule":
                    raise TimerError("same_session 只对日程有意义：跟进本来就回到设它的会话")
                payload["same_session"] = True
            if run.role and run.role != app.roles.default_name:
                payload["role"] = run.role      # 日程到期时由设它的角色处理
            row = app.timers.create(kind=kind, name=str(data.get("name") or ""), payload=payload,
                                    created_by="agent", at=at, due_at=due_at, cron=data.get("cron"),
                                    session_id=run.session_id if kind == "follow_up" else None,
                                    precheck=precheck)
        except TimerError as e:
            return _error(400, "invalid_request_error", str(e))
        sessions.append(app.db, run.session_id, run.id, "host", "timer_set", _timer_view(row))
        if kind == "schedule":
            # 日程会反复叫醒 Agent、反复花钱，所以每个新日程都告诉你一声
            app.outbox.enqueue("system", run.session_id,
                               f"Agent 设了一个日程：{app.timers.describe(row)}\n"
                               f"不需要的话发 /cancel {row['id']}；想现在先试一次发 /run {row['id']}",
                               f"timer:{row['id']}")
        return web.json_response({"ok": True, "timer": _timer_view(row)})

    async def timer_list(request: web.Request) -> web.Response:
        require_run(request)
        return web.json_response({"timers": [_timer_view(r) for r in app.timers.active()]})

    async def timer_cancel(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        try:
            row = app.timers.cancel(str(data.get("id") or ""), by="agent")
        except TimerError as e:
            return _error(400, "invalid_request_error", str(e))
        sessions.append(app.db, run.session_id, run.id, "host", "timer_cancel", {"id": row["id"]})
        return web.json_response({"ok": True})

    async def connection_request(request: web.Request) -> web.Response:
        """Agent 请你连接一个账号。它只能"请"：真凭证由你在宿主终端里输入，绑定主机也由你确认。"""
        run = require_run(request)
        data = await request.json()
        service = str(data.get("service") or "")
        hosts = data.get("hosts")
        auth = data.get("auth") or {"header": "Authorization", "scheme": "Bearer"}
        why = str(data.get("why") or "").strip()[:500]
        if not SERVICE_NAME.fullmatch(service):
            return _error(400, "invalid_request_error", "service 要小写字母开头，只含小写字母、数字、- 和 _")
        if not isinstance(hosts, list) or not 1 <= len(hosts) <= 5 \
                or not all(isinstance(h, str) and valid_host(h) for h in hosts):
            return _error(400, "invalid_request_error", "hosts 要是 1 到 5 个公网域名（不能是 IP 或通配符）")
        if not isinstance(auth, dict) or not (
                (isinstance(auth.get("header"), str) and auth["header"].lower() not in ("cookie", "host"))
                or isinstance(auth.get("query"), str)):
            return _error(400, "invalid_request_error", "auth 要写明令牌放在哪个请求头（header，可带 scheme）或查询参数（query）")
        auth = {k: str(v) for k, v in auth.items() if k in ("header", "scheme", "query") and v}
        if not why:
            return _error(400, "invalid_request_error", "要写明为什么需要这个连接（why）")
        if app.creds.get(service):
            return _error(400, "invalid_request_error", f"{service} 已经连接了")
        title = str(data.get("title") or service)[:60]
        hosts = [h.lower() for h in hosts]
        app.db.execute("UPDATE connection_request SET status = 'DISMISSED' WHERE service = ? AND status = 'PENDING'",
                       (service,))
        request_id = new_id("cr")
        app.db.execute(
            "INSERT INTO connection_request (id, service, title, hosts, auth, why, session_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
            (request_id, service, title, json.dumps(hosts), json.dumps(auth), why, run.session_id, now()),
        )
        sessions.append(app.db, run.session_id, run.id, "host", "connection_request",
                        {"id": request_id, "service": service, "hosts": hosts})
        app.outbox.enqueue("system", run.session_id, (
            f"Agent 请你连接一个新账号：{title}（{service}）\n"
            f"Agent 说的用途（不可信，仅供参考）：{why}\n"
            f"令牌只会被发往：{', '.join(hosts)}\n认证方式：{describe_auth(auth)}\n"
            f"同意的话，在运行宿主的机器上执行：\n"
            f"uv run python -m host.creds add {service} --request {request_id}\n"
            f"命令会再给你看一遍这些主机，确认后从终端输入令牌。不同意就不用理它。"),
            f"connreq:{request_id}")
        return web.json_response({"ok": True, "request_id": request_id})

    async def profile_propose(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        try:
            proposal_id = app.proposals.propose_profile(run.session_id, str(data.get("yaml") or ""))
        except ProposalError as e:
            return _error(400, "invalid_request_error", str(e))
        sessions.append(app.db, run.session_id, run.id, "host", "proposal", {"id": proposal_id, "kind": "profile"})
        return web.json_response({"ok": True, "proposal_id": proposal_id})

    async def webhook_request(request: web.Request) -> web.Response:
        run = require_run(request)
        data = await request.json()
        try:
            proposal_id = app.proposals.propose_webhook(
                run.session_id, str(data.get("name") or ""), str(data.get("title") or ""),
                str(data.get("correlation") or ""), str(data.get("skill") or ""))
        except ProposalError as e:
            return _error(400, "invalid_request_error", str(e))
        sessions.append(app.db, run.session_id, run.id, "host", "proposal", {"id": proposal_id, "kind": "webhook"})
        return web.json_response({"ok": True, "proposal_id": proposal_id})

    async def browser(request: web.Request) -> web.Response:
        """操控浏览器。只有这几个动作；没有执行脚本，也拿不到 Cookie 和配置目录。"""
        run = require_run(request)
        if app.browser is None:
            return _error(400, "invalid_request_error", "这台宿主没有开浏览器")
        data = await request.json()
        try:
            result = await app.browser.act(run, data if isinstance(data, dict) else {})
        except BrowserError as e:
            return _error(400, "invalid_request_error", str(e))
        except Exception as e:
            log.exception("浏览器动作失败")
            return _error(500, "api_error", f"浏览器出错：{type(e).__name__}: {str(e)[:300]}")
        if data.get("action") in ("open", "click", "type", "press", "back", "login"):
            sessions.append(app.db, run.session_id, run.id, "host", "browser",
                            {k: data[k] for k in ("action", "url", "ref") if k in data})
        return web.json_response(result)

    async def handoff(request: web.Request) -> web.Response:
        """把任务交给另一个角色：往收件箱里写一条事件，由目标角色在自己的新会话里处理（第 17.2 节）。

        两个上限防止互相踢皮球：一条用户消息引发的交接不超过 handoff_max_hops 跳；
        一个任务（这串交接里所有运行加起来）的花费不超过 task_token_budget。到了上限就拒绝并告诉你。
        """
        run = require_run(request)
        data = await request.json()
        roles = app.roles
        if not roles.multi:
            return _error(400, "invalid_request_error", "现在只有一个角色，没有可以交接的对象")
        target = roles.get(str(data.get("role") or ""))
        if target is None:
            names = ", ".join(r.name for r in roles.all() if r.name != run.role)
            return _error(400, "invalid_request_error", f"没有这个角色。可以交给：{names}")
        if target.name == run.role:
            return _error(400, "invalid_request_error", "不能交给自己")
        task = str(data.get("task") or "").strip()
        if not task:
            return _error(400, "invalid_request_error", "要写清楚交给对方做什么（task）")
        if len(task) > 4000:
            return _error(400, "invalid_request_error", "task 太长（上限 4000 字）。材料写进 work/ 下的文件，在 task 里写路径")
        me = roles.title_of(run.role)
        hops = run.hops + 1
        stop = None
        if hops > cfg.handoff_max_hops:
            stop = f"角色之间的交接到了上限（{cfg.handoff_max_hops} 跳）"
        elif app.runs.chain_tokens(run.chain_root) >= cfg.task_token_budget:
            stop = f"这个任务（含角色之间的交接）的花费到了上限（{cfg.task_token_budget} token）"
        if stop:
            sessions.append(app.db, run.session_id, run.id, "host", "handoff_refused",
                            {"to": target.name, "hops": hops, "why": stop})
            report_chain_stop(run, stop, f"最后一次是「{me}」想交给「{target.title}」：{task[:200]}\n")
            return _error(403, "permission_error",
                          f"{stop}，交接被拒绝，已经告诉用户。不要再交接，把目前的进展直接告诉用户。")
        payload = {
            "text": f"[交接] 「{me}」（{run.role}）交给你的任务（第 {hops} 跳）：\n{task}",
            "at": now(), "role": target.name, "session_kind": "handoff", "from_role": run.role,
            "chain": {"root": run.chain_root, "hops": hops, "fg": run.foreground},
        }
        # 去重键 = 触发事件 + 会话 + 序号：同一批事件重试时，第 n 次交接不会重复
        fresh = app.inbox.add("handoff", f"{run.trigger_key}:{run.session_id}:{run.handoffs}", payload)
        run.handoffs += 1
        sessions.append(app.db, run.session_id, run.id, "host", "handoff",
                        {"to": target.name, "hops": hops, "task": task[:500]})
        return web.json_response({"ok": True, "duplicate": not fresh, "role": target.name,
                                  "title": target.title, "hops": hops})

    async def keep_awake(request: web.Request) -> web.Response:
        """请求电脑暂不挂起（第 5.5 节）。上限由宿主执行，到点照常挂起。"""
        run = require_run(request)
        data = await request.json()
        minutes = data.get("minutes")
        reason = str(data.get("reason") or "").strip()[:200]
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
            return _error(400, "invalid_request_error", "minutes 要是数字（0 表示撤销）")
        if minutes > 0 and not reason:
            return _error(400, "invalid_request_error", "要写明为什么（reason）")
        until, capped = app.sleeper.keep_awake(reason, float(minutes))
        sessions.append(app.db, run.session_id, run.id, "host", "keep_awake",
                        {"reason": reason, "minutes": minutes, "until": until.isoformat() if until else None})
        return web.json_response({"ok": True, "until": local_str(until.isoformat()) if until else None,
                                  "capped": capped, "max_minutes": int(cfg.keep_awake_max_s // 60)})

    web_app = web.Application(client_max_size=64 * 1024 * 1024)
    web_app.router.add_post("/host/keep_awake", keep_awake)
    web_app.router.add_post("/host/handoff", handoff)
    web_app.router.add_post("/host/browser", browser)
    web_app.router.add_post("/host/connection_request", connection_request)
    web_app.router.add_post("/host/profile_propose", profile_propose)
    web_app.router.add_post("/host/webhook_request", webhook_request)
    web_app.router.add_post("/host/timer_set", timer_set)
    web_app.router.add_post("/host/timer_list", timer_list)
    web_app.router.add_post("/host/timer_cancel", timer_cancel)
    web_app.router.add_post("/model/{tail:.*}", model_proxy)
    web_app.router.add_post("/host/run_context", run_context)
    web_app.router.add_post("/host/log_append", log_append)
    web_app.router.add_post("/host/send", send)
    web_app.router.add_post("/host/send_file", send_file)
    web_app.router.add_post("/host/recall", recall)
    web_app.router.add_post("/host/web_search", web_search)
    web_app.router.add_post("/host/should_yield", should_yield)
    return web_app
