"""裁决者：对电脑发出的每个网络请求判定放行、拒绝或问你。设计文档 11.2–11.5。

它只看实际发出的请求（方法、地址、请求体），不看 Agent 说自己要干什么。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ..db import new_id, now
from ..runs import RunCtx
from .services import FIELD_LABELS, ServiceCall, constraint_covers, render_summary
from .urls import extract_links

log = logging.getLogger("adjudicator")

ALLOWED_PORTS = (80, 443)

# 基础设施白名单：包管理源和代码托管的只读访问
INFRA_HOSTS = (
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
    "gitlab.com", "proxy.golang.org", "sum.golang.org", "crates.io", "static.crates.io", "index.crates.io",
)

SCOPE_LABELS = {"once": "仅这一次", "run": "本次运行", "session": "本次会话", "1h": "一小时内", "forever": "永久"}
READ_SCOPES = ["once", "run", "session", "1h", "forever"]
WRITE_SCOPES = ["once"]        # 往外发送数据的请求，每次都要你看过
ALL_SCOPES = READ_SCOPES

CLASS_REASONS = {
    "write": "会修改你在这个服务里的数据（可以撤销）",
    "send": "会以你的名义发出去，别人能看到",
    "irreversible": "不可撤销",
    "money": "涉及资金",
}

APPROVAL_TTL = timedelta(hours=24)
_SHOW_BODY_CHARS = 2000


def is_infra(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in INFRA_HOSTS)


@dataclass
class Req:
    method: str
    url: str            # 规范化后的地址
    host: str
    port: int
    body: bytes = b""
    purpose: str = ""   # Agent 自述的目的（X-Purpose 头），不可信，只用于展示
    service: ServiceCall | None = None   # 发往已连接服务的调用
    browser: BrowserReq | None = None  # 浏览器发出的请求
    site: ServiceCall | None = None      # 浏览器请求匹配到了网站档案里的一条规则

    @property
    def call(self) -> ServiceCall | None:
        """按档案归类的调用：已连接服务的，或者网站档案的。授权和审批对两者一视同仁。"""
        return self.service or self.site

    @property
    def is_read(self) -> bool:
        return self.method in ("GET", "HEAD")

    @property
    def hash(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.method}\n{self.url}\n".encode())
        h.update(self.body)
        return h.hexdigest()


@dataclass
class BrowserReq:
    """浏览器请求的来历。这些头由浏览器自己填，电脑里的程序改不了（它只能点击和输入）。"""
    mode: str = ""          # Sec-Fetch-Mode：navigate 是页面跳转
    dest: str = ""          # Sec-Fetch-Dest：document / script / image / empty（脚本发的数据请求）……
    site: str = ""          # Sec-Fetch-Site：same-origin / same-site / cross-site / none
    acting: bool = False    # 发生在 Agent 的一个动作期间
    only_navigating: bool = False   # 那个动作只是打开或后退，Agent 没往页面里点过、填过东西
    cookies: bool = False   # 带着这个网站的登录态
    content_type: str = ""
    page: str = ""          # 当时浏览器停在哪个页面

    @property
    def navigation(self) -> bool:
        return self.mode == "navigate"


# 页面加载的静态资源，跨站也放行（CDN 上的脚本、样式、图片、字体）
_STATIC_DESTS = ("script", "style", "image", "font", "audio", "video", "track", "manifest", "worker",
                 "sharedworker", "serviceworker", "iframe", "frame", "embed", "object")


@dataclass
class Decision:
    verdict: str                    # allow / deny / ask / pending
    reason: str
    category: str = "web_read"      # infra / web_read / web_write
    approval_id: str | None = None
    scopes: list[str] = field(default_factory=list)


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _browser_body(req: Req) -> list[str]:
    """浏览器请求要发出去的内容：表单拆成字段（密码类隐藏），别的给出原文开头。"""
    from ..browser import form_fields
    b = req.browser
    assert b is not None
    fields = form_fields(req.body, b.content_type)
    if fields:
        return ["表单字段：\n" + "\n".join(f"  {k} = {v[:300]}" for k, v in fields[:40])]
    if req.body:
        text = req.body[:_SHOW_BODY_CHARS].decode("utf-8", "replace")
        return [f"请求体（{b.content_type or '未知类型'}，{len(req.body)} 字节）：\n{text}"]
    return []


class Adjudicator:
    def __init__(self, db: sqlite3.Connection, app, hold_s: float = 30.0):
        self.db = db
        self.app = app              # 用到 outbox、inbox、runs
        self.hold_s = hold_s
        self._waiters: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------ 已见地址

    def add_seen(self, urls, source: str) -> int:
        ts = now()
        rows = [(u, source, ts) for u in urls if u]
        if not rows:
            return 0
        cur = self.db.executemany("INSERT OR IGNORE INTO seen_url (url, source, created_at) VALUES (?, ?, ?)", rows)
        return cur.rowcount

    def is_seen(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen_url WHERE url = ?", (url,)).fetchone() is not None

    def user_named(self, run: RunCtx, host: str) -> bool:
        """这个网站是不是你在这次对话里亲手发过的网址（只认你自己的消息，不认 Webhook 之类的外部内容）。"""
        if run.user_sites is None:
            sites: set[str] = set()
            for row in self.db.execute(
                    "SELECT i.payload FROM event e JOIN inbox i ON i.id = e.inbox_id "
                    "WHERE e.session_id = ? AND e.type = 'user_message' AND i.source = 'user'",
                    (run.session_id,)):
                try:
                    text = json.loads(row["payload"]).get("text") or ""
                except (ValueError, AttributeError):
                    continue
                sites |= {urlsplit(u).hostname or "" for u in extract_links(text, "https://invalid/")}
            run.user_sites = sites - {"", "invalid"}
        return host in run.user_sites

    # ------------------------------------------------------------ 判定

    def decide(self, run: RunCtx, req: Req) -> Decision:
        if req.port not in ALLOWED_PORTS:
            return Decision("deny", f"只允许 80 和 443 端口，这个请求是 {req.port}")
        if req.browser:
            return self._decide_browser(run, req)
        if req.service:
            return self._decide_service(run, req)
        if is_infra(req.host):
            if req.is_read:
                return Decision("allow", "基础设施白名单（只读）", "infra")
            if req.method == "POST" and req.url.split("?", 1)[0].endswith("/git-upload-pack"):
                return Decision("allow", "git 拉取", "infra")
            # 其余写操作按公网的写请求处理
        if req.is_read and self.is_seen(req.url):
            return Decision("allow", "地址出现过（搜索结果、网页链接或你的消息）", "web_read")
        grant = self._match_grant(run, req)
        if grant:
            return Decision("allow", f"已授权（{SCOPE_LABELS[grant['scope']]}）",
                            "web_read" if req.is_read else "web_write")
        if req.is_read:
            return Decision("ask", "地址是 Agent 自己拼出来的，没有出现在任何搜索结果或网页里",
                            "web_read", scopes=READ_SCOPES)
        return Decision("ask", f"{req.method} 请求会向外发送数据", "web_write", scopes=WRITE_SCOPES)

    def _decide_browser(self, run: RunCtx, req: Req) -> Decision:
        """第 16 节：不判断"这次点击危不危险"，只看点击之后浏览器实际发出的请求。"""
        b = req.browser
        assert b is not None
        # 你在这次对话里点名的网站：同站的请求都不问（页面自己的后台请求、Agent 拼的地址、Agent 点出来的提交）。
        # 别的网站照旧；这次运行读到疑似注入的内容时，退回逐个问你。
        named = not run.suspended and b.site in ("same-origin", "same-site", "none", "") \
            and self.user_named(run, req.host) \
            and (b.site != "" or urlsplit(b.page).hostname == req.host)
        site = req.site
        if site is not None:
            title = site.profile.title
            if site.cls == "read":
                # 你确认过的网站档案说这是读取（比如用 POST 的查询）：直接放行
                return Decision("allow", f"浏览器：{title} 档案里的读取（{site.action.id if site.action else ''}）", "web_read")
            if named:
                return Decision("allow", "浏览器：你点名的网站，同站提交不问", "web_write")
            if req.is_read and not (b.navigation and b.acting):
                pass        # 页面自己加载的东西不算这条规则的操作，按下面的默认规则判
            elif not b.acting:
                return Decision("deny", "浏览器：页面在后台自己发的提交，不是 Agent 操作的结果", "web_write")
            elif b.only_navigating and not b.navigation:
                return Decision("deny", "浏览器：页面加载时脚本自己发的提交，不是 Agent 操作的结果", "web_write")
            elif not b.navigation and b.site == "cross-site":
                return Decision("deny", "浏览器：页面脚本向别的网站提交数据（多半是统计脚本）", "web_write")
            else:
                return self._decide_site(run, req)
        if req.is_read:
            if self.is_seen(req.url):
                return Decision("allow", "浏览器：地址出现过", "web_read")
            if named:
                return Decision("allow", "浏览器：你点名的网站", "web_read")
            if not b.navigation and b.site in ("same-origin", "same-site"):
                return Decision("allow", "浏览器：页面加载同站的资源", "web_read")
            if not b.navigation and b.dest in _STATIC_DESTS:
                return Decision("allow", f"浏览器：页面加载静态资源（{b.dest}）", "web_read")
            if is_infra(req.host):
                return Decision("allow", "浏览器：基础设施白名单（只读）", "web_read")
            if self._match_grant(run, req):
                return Decision("allow", "浏览器：已授权", "web_read")
            if not (b.navigation and b.acting):
                # 页面脚本发的跨站数据请求，地址又没出现过：不问你，直接拦下
                return Decision("deny", "浏览器：页面脚本发往别的网站的数据请求，地址没出现过", "web_read")
            return Decision("ask", "浏览器要打开一个没出现在任何网页里的地址（多半是 Agent 自己拼的，"
                                   "或者是表单用 GET 提交了它填的内容）", "web_read", scopes=READ_SCOPES)
        if named:
            return Decision("allow", "浏览器：你点名的网站，同站提交不问", "web_write")
        if not b.acting:
            # Agent 没在操作时页面自己发的提交（统计、心跳）不是 Agent 的行为，也不打扰你
            return Decision("deny", "浏览器：页面在后台自己发的提交，不是 Agent 操作的结果", "web_write")
        if b.only_navigating and not b.navigation:
            # 打开、后退时页面脚本自己发的提交（统计、埋点）：里面没有 Agent 填的东西，拦下也不打扰你
            return Decision("deny", "浏览器：页面加载时脚本自己发的提交，不是 Agent 操作的结果", "web_write")
        if not b.navigation and b.site == "cross-site":
            return Decision("deny", "浏览器：页面脚本向别的网站提交数据（多半是统计脚本）", "web_write")
        if self._match_grant(run, req):
            return Decision("allow", "浏览器：已授权（仅这一次）", "web_write")
        why = f"浏览器要向外提交数据（{req.method}）"
        if b.cookies:
            why += f"，带着你在 {req.host} 的登录状态"
        return Decision("ask", why, "web_write", scopes=WRITE_SCOPES)

    def _decide_service(self, run: RunCtx, req: Req) -> Decision:
        call = req.service
        assert call is not None
        category = f"svc:{call.cls}"
        if call.cls == "read":
            return Decision("allow", f"{call.profile.title}：读取", category)
        grant = self._match_grant(run, req)
        if grant:
            return Decision("allow", f"{call.profile.title}：已授权（{SCOPE_LABELS[grant['scope']]}）", category)
        return Decision("ask", CLASS_REASONS[call.cls], category, scopes=self._scopes(call))

    def _decide_site(self, run: RunCtx, req: Req) -> Decision:
        """网站档案里非读取的规则：和服务一样，可以长期授权这一类操作（限定在约束值上）。"""
        call = req.site
        assert call is not None
        category = f"site:{call.cls}"
        grant = self._match_grant(run, req)
        if grant:
            return Decision("allow", f"浏览器：{call.profile.title} 已授权（{SCOPE_LABELS[grant['scope']]}）",
                            category)
        why = CLASS_REASONS[call.cls]
        if req.browser is not None and req.browser.cookies:
            why += f"；带着你在 {req.host} 的登录状态"
        return Decision("ask", why, category, scopes=self._scopes(call))

    @staticmethod
    def _scopes(call: ServiceCall) -> list[str]:
        if call.cls in ("irreversible", "money") or call.problem:
            return WRITE_SCOPES
        if call.cls == "send" and not (call.action and call.action.constraint_keys):
            return WRITE_SCOPES       # 没有约束的发送只能一次一批
        return ALL_SCOPES

    def _match_grant(self, run: RunCtx, req: Req):
        call = req.call
        rows = self.db.execute(
            "SELECT * FROM grant_ WHERE revoked_at IS NULL AND host = ? AND ("
            "(kind = 'request' AND request_hash = ?) OR (kind = 'domain' AND ?) "
            "OR (kind = 'service' AND service = ? AND action = ?)) ORDER BY created_at",
            (req.host, req.hash, 1 if req.is_read and not call else 0,
             call.profile.service if call else None, call.action.id if call and call.action else None),
        ).fetchall()
        current = now()
        for g in rows:
            if g["expires_at"] and g["expires_at"] <= current:
                continue
            scope = g["scope"]
            if run.suspended and scope != "once":
                continue        # 这次运行读到了疑似注入的内容：长期授权暂停，逐个问你
            if g["kind"] == "service" and call is not None and not constraint_covers(json.loads(g["constraint_"] or "{}"),
                                                                call.constraint):
                continue
            if scope == "once":
                if g["used_at"]:
                    continue
                self.db.execute("UPDATE grant_ SET used_at = ? WHERE id = ?", (current, g["id"]))
                return g
            if scope == "run":
                if g["run_id"] == run.id:
                    return g
                # 发起审批的那次运行已经挂起结束了：授权转给同一会话里接着做的那次运行
                if g["session_id"] == run.session_id and self.app.runs.by_id(g["run_id"]) is None:
                    self.db.execute("UPDATE grant_ SET run_id = ? WHERE id = ?", (run.id, g["id"]))
                    return g
                continue
            if scope == "session" and g["session_id"] != run.session_id:
                continue
            return g        # session（同一会话）/ 1h（未过期）/ forever
        return None

    # ------------------------------------------------------------ 问你

    def _service_summary(self, req: Req, decision: Decision) -> str:
        """给你看的内容全部来自实际要发出的请求字节，而且批准"仅这一次"时绑定这些字节。"""
        call = req.call
        assert call is not None
        title = call.profile.title
        what = render_summary(call.action.summary, call.fields) if call.action and call.action.summary \
            else f"{req.method} 请求（档案里没有对应的规则）"
        lines = [f"需要你批准：{title} — {what}", f"为什么要问：{decision.reason}"]
        if req.site is not None:
            assert req.browser is not None and call.action is not None     # 网站档案只匹配浏览器请求，且总有规则
            lines.insert(1, f"浏览器要{'打开' if req.is_read else '提交'} {req.method} {req.url}"
                            + (f"（当时在 {req.browser.page}）" if req.browser.page else ""))
            # 网站：档案挑出来的字段之外，整张表单也给你看（密码类字段只显示被隐藏了）
            for key in call.action.show:
                value = call.fields.get(key)
                if value not in (None, ""):
                    lines.append(f"{FIELD_LABELS.get(key, key)}：{str(value)[:300]}")
            lines += _browser_body(req)
        elif call.action and not call.problem:
            for key in call.action.show:
                value = call.fields.get(key)
                if value in (None, "", []):
                    continue
                text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
                if key == "text" or "\n" in text:
                    lines.append(f"{FIELD_LABELS.get(key, key)}：\n{text[:_SHOW_BODY_CHARS]}")
                else:
                    lines.append(f"{FIELD_LABELS.get(key, key)}：{text}")
        else:
            if call.problem:
                lines.append(call.problem)
            if req.body:
                lines.append(f"请求体：\n{req.body[:_SHOW_BODY_CHARS].decode('utf-8', 'replace')}")
        if call.constraint and len(decision.scopes) > 1:
            pairs = "；".join(f"{FIELD_LABELS.get(k, k)} = {', '.join(v)}" for k, v in call.constraint.items())
            lines.append(f"如果选长期授权，只覆盖：{pairs}")
        lines.append(f"（原始请求：{req.method} {req.url}，{len(req.body)} 字节）")
        if req.purpose:
            lines.append(f"Agent 自述的目的（不可信，仅供参考）：{req.purpose[:300]}")
        return "\n".join(lines)

    def _summary(self, req: Req, decision: Decision) -> str:
        if req.call:
            return self._service_summary(req, decision)
        if req.browser:
            return self._browser_summary(req, decision)
        lines = ["需要你批准一个网络请求", f"请求：{req.method} {req.url}"]
        if req.body:
            text = req.body[:_SHOW_BODY_CHARS].decode("utf-8", "replace")
            more = f"\n……（共 {len(req.body)} 字节）" if len(req.body) > _SHOW_BODY_CHARS else ""
            lines.append(f"请求体：\n{text}{more}")
        lines.append(f"为什么要问：{decision.reason}")
        if req.purpose:
            lines.append(f"Agent 自述的目的（不可信，仅供参考）：{req.purpose[:300]}")
        return "\n".join(lines)

    def _browser_summary(self, req: Req, decision: Decision) -> str:
        b = req.browser
        assert b is not None
        lines = [f"需要你批准：浏览器要{'打开' if req.is_read else '提交'} {req.method} {req.url}"]
        if b.page:
            lines.append(f"当时所在页面：{b.page}")
        lines += _browser_body(req)
        lines.append(f"为什么要问：{decision.reason}")
        return "\n".join(lines)

    def bounce_first(self, run: RunCtx, req: Req) -> bool:
        """没见过的普通读取，第一次只退回给 Agent，不发出去，也不打扰你；同一请求再来才开审批。
        写请求、已连接服务和浏览器不走这一步。"""
        if not self.app.cfg.egress_bounce or not req.is_read or req.body or req.service or req.browser:
            return False
        if req.hash in run.bounced:
            return False
        pending = self.db.execute(
            "SELECT 1 FROM approval WHERE session_id = ? AND request_hash = ? AND status = 'PENDING'",
            (run.session_id, req.hash),
        ).fetchone()
        if pending:
            return False
        run.bounced.add(req.hash)
        return True

    def _open_approval(self, run: RunCtx, req: Req, decision: Decision) -> str:
        row = self.db.execute(
            "SELECT id FROM approval WHERE session_id = ? AND request_hash = ? AND status = 'PENDING'",
            (run.session_id, req.hash),
        ).fetchone()
        if row:
            return row["id"]
        approval_id = new_id("apr")
        summary = self._summary(req, decision)
        call = req.call
        self.db.execute(
            "INSERT INTO approval (id, session_id, run_id, request_hash, category, service, action, constraint_, "
            "method, host, url, summary, scopes, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
            (approval_id, run.session_id, run.id, req.hash, decision.category,
             call.profile.service if call else None,
             call.action.id if call and call.action else None,
             json.dumps(call.constraint, ensure_ascii=False) if call else None,
             req.method, req.host, req.url, summary, json.dumps(decision.scopes), now()),
        )
        options = " | ".join(f"{s}={SCOPE_LABELS[s]}" for s in decision.scopes)
        body = (f"{summary}\n\n审批号：{approval_id}\n"
                f"批准：/approve {approval_id} [范围]　拒绝：/deny {approval_id}\n范围：{options}")
        self.app.outbox.enqueue("approval", run.session_id, body, f"approval:{approval_id}",
                                meta={"approval_id": approval_id, "scopes": decision.scopes})
        return approval_id

    async def ask(self, run: RunCtx, req: Req, decision: Decision, hold_s: float | None = None) -> Decision:
        """生成审批请求，把这个请求挂住一会儿等你。等不到就以"待批准"返回，运行随后挂起。"""
        approval_id = self._open_approval(run, req, decision)
        future = self._waiters.get(approval_id)
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self._waiters[approval_id] = future
        try:
            approved = await asyncio.wait_for(asyncio.shield(future), self.hold_s if hold_s is None else hold_s)
        except TimeoutError:
            if self._waiters.get(approval_id) is future:
                del self._waiters[approval_id]
            self.db.execute("UPDATE approval SET deferred = 1 WHERE id = ?", (approval_id,))
            run.deferred.append(approval_id)
            return Decision("pending", "等待你批准", decision.category, approval_id, decision.scopes)
        if approved:
            # 授权刚建好，重新判定一次，用掉"仅这一次"的授权
            again = self.decide(run, req)
            if again.verdict == "allow":
                return again
        return Decision("deny", "你拒绝了这个请求", decision.category, approval_id)

    # ------------------------------------------------------------ 你的答复

    def resolve(self, approval_id: str, approve: bool, scope: str = "once") -> str:
        """处理你的批准或拒绝，返回给你的回复。只能从渠道调用，电脑里没有对应的接口。"""
        row = self.db.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return f"找不到审批 {approval_id}。"
        if row["status"] != "PENDING":
            return f"审批 {approval_id} 已经处理过了（{row['status']}）。"
        scopes = json.loads(row["scopes"])
        if approve and scope not in scopes:
            return f"这个请求只能选这些范围：{', '.join(scopes)}"

        ts = now()
        if approve:
            expires = (_now_dt() + timedelta(hours=1)).isoformat(timespec="microseconds") if scope == "1h" else None
            # "仅这一次"只覆盖这一个请求（绑定请求内容）。长期授权：网页读取覆盖整个主机；
            # 服务调用覆盖同一个动作，并且限定在这次请求的约束值上（比如同样的收件人）
            if scope == "once":
                kind = "request"
            elif row["category"].startswith(("svc:", "site:")):
                kind = "service"
            else:
                kind = "domain"
            self.db.execute(
                "INSERT INTO grant_ (id, approval_id, kind, host, service, action, constraint_, request_hash, "
                "scope, run_id, session_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("grt"), approval_id, kind, row["host"], row["service"], row["action"], row["constraint_"],
                 row["request_hash"], scope, row["run_id"], row["session_id"], expires, ts),
            )
        status = "GRANTED" if approve else "DENIED"
        self.db.execute("UPDATE approval SET status = ?, scope = ?, resolved_at = ? WHERE id = ?",
                        (status, scope if approve else None, ts, approval_id))
        self._notify(row, approve, scope)
        if approve:
            return f"已批准：{row['method']} {row['url']}（{SCOPE_LABELS[scope]}）"
        return f"已拒绝：{row['method']} {row['url']}"

    def _notify(self, row, approve: bool, scope: str | None, expired: bool = False) -> None:
        future = self._waiters.pop(row["id"], None)
        if future is not None and not future.done():
            future.set_result(approve)      # 请求还挂在代理上，直接放行或拒绝
            return
        if not row["deferred"]:
            return
        # 运行已经挂起了：往收件箱放一条事件，在原会话里接着做
        target = f"{row['method']} {row['url']}"
        if approve:
            text = f"[审批] 你批准了 {target}（{SCOPE_LABELS[scope or 'once']}）。可以重试刚才被拦下的请求。"
        elif expired:
            text = f"[审批] {target} 的审批 24 小时没有处理，按拒绝处理。不要再试这个请求。"
        else:
            text = f"[审批] 你拒绝了 {target}。不要再试这个请求，换个办法，或者问我。"
        outcome = "expired" if expired else ("granted" if approve else "denied")
        self.app.inbox.add("approval", f"{row['id']}:{outcome}",
                           {"text": text, "at": now()}, session_id=row["session_id"])

    def expire_stale(self) -> int:
        cutoff = (_now_dt() - APPROVAL_TTL).isoformat(timespec="microseconds")
        rows = self.db.execute(
            "SELECT * FROM approval WHERE status = 'PENDING' AND created_at < ?", (cutoff,)
        ).fetchall()
        for row in rows:
            self.db.execute("UPDATE approval SET status = 'EXPIRED', resolved_at = ? WHERE id = ?",
                            (now(), row["id"]))
            self._notify(row, False, None, expired=True)
        return len(rows)

    def log(self, run: RunCtx | None, method: str, url: str, verdict: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO egress_log (run_id, session_id, method, url, verdict, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run.id if run else None, run.session_id if run else None, method, url[:2000], verdict, reason, now()),
        )
