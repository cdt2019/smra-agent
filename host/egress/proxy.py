"""出网代理：嵌在宿主进程里的 mitmproxy。

它负责把流量变成可以裁决的请求：认出是哪次运行发的、解密 HTTPS、检查目的地址、
清洗请求头、从响应里收集链接。判定交给 Adjudicator。
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit

from mitmproxy import http, options
from mitmproxy.tools.dump import DumpMaster

from ..browser import Denied
from .adjudicator import BrowserReq, Req
from .netcheck import Resolver
from .services import RESPONSE_FILTERS
from .urls import extract_links, normalize

SECRET_MASK = "[已隐藏的凭证]"

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("egress")

# 发往公网的请求只保留这些头；其余（Cookie、Authorization、自定义头……）全部去掉，
# 免得被用来夹带数据。User-Agent 换成固定值。
_KEEP_HEADERS = ("accept", "accept-encoding", "accept-language", "range", "if-none-match",
                 "if-modified-since", "cache-control", "content-type")
_USER_AGENT = "Mozilla/5.0 (compatible; smra-agent/0.1)"
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml", "application/rss",
               "application/atom", "application/ld+json")
_MAX_SCAN_BYTES = 5 * 1024 * 1024
CA_FILE = "mitmproxy-ca-cert.pem"


def _token(value: str | None) -> str | None:
    """从 Proxy-Authorization: Basic base64(run:<通行证>) 里取出通行证。"""
    if not value or not value.lower().startswith("basic "):
        return None
    try:
        user_pass = base64.b64decode(value[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return user_pass.partition(":")[2] or None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _host_only(host_header: str) -> str:
    """Host 头去掉端口。"""
    value = host_header.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _block(status: int, text: str, extra: dict | None = None) -> http.Response:
    headers = {"content-type": "text/plain; charset=utf-8", "x-smra-egress": "blocked"}
    headers.update(extra or {})
    return http.Response.make(status, text.encode("utf-8"), headers)


class EgressAddon:
    def __init__(self, app: App, resolver: Resolver):
        self.app = app
        self.adj = app.adjudicator
        self.resolver = resolver
        self._conn_tokens: dict[str, str | None] = {}
        # 裁决放行时固定下来的上游地址：客户端连接 → {(IP, 端口): 主机名}。建上游连接时只认这里有的
        self._pinned: dict[str, dict[tuple[str, int], str]] = {}

    def _pin(self, flow: http.HTTPFlow, target: tuple[str, int], host: str) -> None:
        """放行的请求直接发往裁决时解析出的地址，Host 头不变。

        地址在这里就定下来，而不是等到建连接时再换：mitmproxy 按目标地址复用上游连接，
        建连接时才把主机名换成 IP，后面的请求就再也对不上已有的连接，每个请求都去新开一条，
        页面一次加载几十个文件时会卡死。
        """
        r = flow.request
        host_header = r.host_header
        r.host, r.port = target[0], target[1]
        r.host_header = host_header or host
        self._pinned.setdefault(flow.client_conn.id, {})[(target[0], target[1])] = host

    # ------------------------------------------------------------ 认出是哪次运行

    def _is_browser(self, token: str | None) -> bool:
        browser = getattr(self.app, "browser", None)
        return bool(token) and browser is not None and secrets.compare_digest(token, browser.proxy_token)

    def http_connect(self, flow: http.HTTPFlow) -> None:
        token = _token(flow.request.headers.get("proxy-authorization"))
        run = self.app.runs.by_token(token)
        if run is None and self._is_browser(token):
            if flow.request.port not in (80, 443):
                flow.response = _block(403, f"只允许 80 和 443 端口，这个请求是 {flow.request.port}")
                self.adj.log(None, "CONNECT", f"{flow.request.host}:{flow.request.port}", "deny", "浏览器：端口不允许")
                return
            self._conn_tokens[flow.client_conn.id] = token
            return
        if run is None:
            flow.response = _block(407, "需要有效的运行通行证", {"proxy-authenticate": 'Basic realm="smra"'})
            self.adj.log(None, "CONNECT", f"{flow.request.host}:{flow.request.port}", "deny", "没有有效通行证")
            return
        if flow.request.port not in (80, 443):
            flow.response = _block(403, f"只允许 80 和 443 端口，这个请求是 {flow.request.port}")
            self.adj.log(run, "CONNECT", f"{flow.request.host}:{flow.request.port}", "deny", "端口不允许")
            return
        self._conn_tokens[flow.client_conn.id] = token

    def client_disconnected(self, client) -> None:
        self._conn_tokens.pop(client.id, None)
        self._pinned.pop(client.id, None)

    # ------------------------------------------------------------ 裁决

    async def request(self, flow: http.HTTPFlow) -> None:
        r = flow.request
        token = _token(r.headers.get("proxy-authorization")) or self._conn_tokens.get(flow.client_conn.id)
        if self._is_browser(token):
            await self._browser_request(flow)
            return
        run = self.app.runs.by_token(token)
        url = normalize(r.url) or r.url
        if run is None:
            # 没有通行证，或者通行证所属的运行已经结束
            flow.response = _block(407, "需要有效的运行通行证", {"proxy-authenticate": 'Basic realm="smra"'})
            self.adj.log(None, r.method, url, "deny", "没有有效通行证")
            return
        r.headers.pop("proxy-authorization", None)
        purpose = unquote(r.headers.pop("x-purpose", ""))[:300]

        host = r.host.lower().strip("[]")
        if r.host_header and _host_only(r.host_header) != host:
            flow.response = _block(403, "Host 头和连接的目标不一致")
            self.adj.log(run, r.method, url, "deny", "Host 头和连接目标不一致")
            return

        target, why = await self.resolver.resolve_public(host, r.port)
        if target is None:
            flow.response = _block(403, why)
            self.adj.log(run, r.method, url, "deny", why)
            return

        body = r.get_content(strict=False) or b""
        call = self.app.services.identify(host, r.method.upper(), r.path.split("?", 1)[0], r.headers,
                                          dict(r.query), body)
        req = Req(method=r.method.upper(), url=url, host=host, port=r.port,
                  body=body, purpose=purpose, service=call)
        decision = self.adj.decide(run, req)
        if decision.verdict == "ask" and run.kind == "precheck":
            # 前置检查没人值守，不发审批打扰你：只能用已有的授权和无需批准的规则
            decision = type(decision)("deny", f"前置检查不能发起审批（{decision.reason}）")
        if decision.verdict == "ask" and self.adj.bounce_first(run, req):
            flow.response = _block(
                403,
                "这个地址没有出现在搜索结果或网页里，所以没有访问，也没有打扰用户。"
                "先用 web_search 找真实地址，或者打开已有结果里的页面顺着链接找。"
                "确实找不到、又必须访问这个地址时，原样再请求一次，才会发给用户批准。",
            )
            self.adj.log(run, req.method, url, "bounce", "没见过的地址：先退回，让 Agent 自己找")
            return
        if decision.verdict == "ask":
            self.adj.log(run, req.method, url, "ask", decision.reason)
            decision = await self.adj.ask(run, req, decision)

        if decision.verdict == "pending":
            flow.response = _block(
                403,
                f"这个请求需要用户批准，已经发给用户了（审批号 {decision.approval_id}）。"
                "不要重试，也不要换个地址绕过去；这次运行会先结束，用户处理后你会收到消息。",
                {"x-smra-approval": decision.approval_id},
            )
            self.adj.log(run, req.method, url, "deny", "等待批准")
            return
        if decision.verdict != "allow":
            flow.response = _block(403, f"不允许：{decision.reason}")
            self.adj.log(run, req.method, url, "deny", decision.reason)
            return

        self.adj.log(run, req.method, url, "allow", decision.reason)
        if call is not None:
            # 发往已连接服务、并且裁决放行：这时才把替身令牌换成真凭证
            try:
                assert call.connection is not None      # 服务调用一定带着连接
                real = await self.app.creds.real_token(call.connection)
            except Exception as e:
                flow.response = _block(502, f"取不到 {call.profile.title} 的凭证：{e}")
                return
            auth = call.profile.auth
            if "header" in auth:
                scheme = auth.get("scheme")
                r.headers[auth["header"]] = f"{scheme} {real}" if scheme else real
            else:
                r.query[auth["query"]] = real
            for name, value in (auth.get("headers") or {}).items():
                if name not in r.headers:       # 档案的固定请求头（如 Notion-Version）；电脑自己带了就用它的
                    r.headers[name] = value
            r.headers.pop("cookie", None)
            flow.metadata["smra"] = {"pin": target, "category": decision.category, "host": host,
                                     "service": call.profile.service,
                                     "filters": call.profile.response_filters}
            self._pin(flow, target, host)
            return
        if decision.category != "infra":
            kept = {k: r.headers[k] for k in _KEEP_HEADERS if k in r.headers}
            r.headers.clear()
            r.headers["host"] = r.host_header or host
            for k, v in kept.items():
                r.headers[k] = v[:200]
            r.headers["user-agent"] = _USER_AGENT
            if r.raw_content:
                r.headers["content-length"] = str(len(r.raw_content))
        flow.metadata["smra"] = {"pin": target, "category": decision.category, "host": host}
        self._pin(flow, target, host)

    async def _browser_request(self, flow: http.HTTPFlow) -> None:
        """浏览器的请求。请求头原样保留（登录态要靠 Cookie），判定按第 16 节的规则。"""
        r = flow.request
        browser = self.app.browser
        assert browser is not None      # 只有浏览器开着才认得出它的请求
        url = normalize(r.url) or r.url
        r.headers.pop("proxy-authorization", None)
        host = r.host.lower().strip("[]")
        if r.host_header and _host_only(r.host_header) != host:
            flow.response = _block(403, "Host 头和连接的目标不一致")
            self.adj.log(None, r.method, url, "deny", "浏览器：Host 头和连接目标不一致")
            return
        target, why = await self.resolver.resolve_public(host, r.port)
        if target is None:
            flow.response = _block(403, why)
            self.adj.log(None, r.method, url, "deny", f"浏览器：{why}")
            return
        if browser.taking_over():
            # 你在接管：这些请求是你的操作
            self.adj.log(None, r.method, url, "allow", "浏览器：你在接管")
            flow.metadata["smra"] = {"pin": target, "category": "browser", "host": host}
            self._pin(flow, target, host)
            return
        run = browser.current_run()
        if run is None:
            flow.response = _block(403, "浏览器现在没有被任何运行使用")
            self.adj.log(None, r.method, url, "deny", "浏览器：没有运行在用")
            return
        h = r.headers
        info = BrowserReq(mode=h.get("sec-fetch-mode", ""), dest=h.get("sec-fetch-dest", ""),
                          site=h.get("sec-fetch-site", ""), acting=browser.acting, only_navigating=browser.only_navigating,
                          cookies="cookie" in h,
                          content_type=h.get("content-type", ""), page=browser.page_url())
        body = r.get_content(strict=False) or b""
        parts = urlsplit(url)
        site = self.app.services.site_call(host, r.method.upper(), parts.path or "/", parts.query, body,
                                           info.content_type)
        req = Req(method=r.method.upper(), url=url, host=host, port=r.port, body=body, browser=info, site=site)
        decision = self.adj.decide(run, req)
        if decision.verdict == "ask":
            self.adj.log(run, req.method, url, "ask", decision.reason)
            decision = await self.adj.ask(run, req, decision, hold_s=self.app.cfg.browser_hold_s)
        if decision.verdict == "pending":
            flow.response = _block(403, f"这个请求需要用户批准（审批号 {decision.approval_id}）。",
                                   {"x-smra-approval": decision.approval_id})
            self.adj.log(run, req.method, url, "deny", "浏览器：等待批准")
            return
        if decision.verdict != "allow":
            flow.response = _block(403, f"不允许：{decision.reason}")
            self.adj.log(run, req.method, url, "deny", decision.reason)
            same_site = info.site in ("same-origin", "same-site") \
                or (info.site in ("", "none") and urlsplit(info.page).hostname == host)
            browser.note_denied(Denied(run.id, req.method, url, decision.reason, same_site,
                                       hint=not run.suspended and not self.adj.user_named(run, host)))
            return
        self.adj.log(run, req.method, url, "allow", decision.reason)
        flow.metadata["smra"] = {"pin": target, "category": "browser", "host": host}
        self._pin(flow, target, host)

    def server_connect(self, data) -> None:
        """只连裁决放行时固定下来的地址；别的上游连接一律不建。"""
        address = (data.server.address[0], data.server.address[1])
        host = self._pinned.get(data.client.id, {}).get(address)
        if host is None:
            data.server.error = "这条上游连接没有经过裁决"
            log.warning("拒绝未经裁决的上游连接 %s", address)
            return
        # 上游 TLS 的 SNI 固定成裁决过的主机名，不让客户端借别的名字走 CDN
        data.server.sni = None if _is_ip(host) else host

    def response(self, flow: http.HTTPFlow) -> None:
        meta = flow.metadata.get("smra")
        if not meta or meta["category"] == "infra" or flow.response is None:
            return
        if meta.get("service"):
            self._filter_service_response(flow, meta)
        base = normalize(flow.request.pretty_url) or flow.request.pretty_url
        found: set[str] = set()
        location = flow.response.headers.get("location")
        if location:
            n = normalize(urljoin(base, location))
            if n:
                found.add(n)
        ctype = flow.response.headers.get("content-type", "").lower()
        if any(ctype.startswith(t) for t in _TEXT_TYPES) and len(flow.response.raw_content or b"") <= _MAX_SCAN_BYTES:
            text = flow.response.get_text(strict=False) or ""
            found |= extract_links(text, base)
        self.adj.add_seen(found, "response")

    def _filter_service_response(self, flow: http.HTTPFlow, meta: dict) -> None:
        """服务的响应回到电脑之前：按档案过滤（比如抹掉验证码），并去掉任何回显出来的真凭证。"""
        resp = flow.response
        assert resp is not None
        ctype = resp.headers.get("content-type", "").lower()
        content = resp.get_content(strict=False) or b""
        for name in meta["filters"]:
            content = RESPONSE_FILTERS[name](content, ctype)
        secrets_ = self.app.creds.secret_values(meta["service"])
        for value in secrets_:
            content = content.replace(value.encode(), SECRET_MASK.encode())
        resp.set_content(content)
        for key in list(resp.headers.keys()):
            value = resp.headers[key]
            for secret in secrets_:
                if secret in value:
                    value = value.replace(secret, SECRET_MASK)
            resp.headers[key] = value

    # ------------------------------------------------------------ 不是 HTTP 的一律不放

    def tcp_start(self, flow) -> None:
        flow.kill()

    def udp_start(self, flow) -> None:
        flow.kill()

    def websocket_start(self, flow) -> None:
        flow.kill()


class EgressProxy:
    def __init__(self, app: App):
        self.app = app
        self.confdir = app.cfg.data_dir / "mitmproxy"
        self.master: DumpMaster | None = None
        self._task: asyncio.Task | None = None

    @property
    def ca_cert(self) -> Path:
        return self.confdir / CA_FILE

    async def start(self, host: str, port: int) -> None:
        self.confdir.mkdir(parents=True, exist_ok=True)
        opts = options.Options(listen_host=host, listen_port=port, confdir=str(self.confdir))
        self.master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        # mitmproxy 自带的 errorcheck 会在启动时看整个进程的日志，只要任何地方记了一条错误就 sys.exit(1)，
        # 把宿主进程一起带走。去掉它，下面自己确认代理确实在监听
        if ec := self.master.addons.get("errorcheck"):
            ec.logger.uninstall()
            self.master.addons.remove(ec)
        self.master.options.update(
            connection_strategy="lazy",     # 先裁决，再连上游
            upstream_cert=False,            # 不为了拿证书先连上游
            http3=False,
            rawtcp=False,
            ssl_insecure=self.app.cfg.egress_ssl_insecure,
        )
        resolver = Resolver(self.app.cfg.egress_overrides)
        self.master.addons.add(EgressAddon(self.app, resolver))
        self._task = asyncio.create_task(self.master.run(), name="egress-proxy")
        for _ in range(100):            # 等它开始监听，并生成 CA 证书
            await asyncio.sleep(0.05)
            if self.ca_cert.exists() and await _listening(host, port):
                break
            if self._task.done():
                break
        else:
            raise RuntimeError(f"出网代理 5 秒内没有在 {host}:{port} 上监听")
        if self._task.done():
            raise RuntimeError(f"出网代理启动失败：{self._task.exception() or '提前退出'}")
        log.info("出网代理监听在 %s:%d", host, port)

    async def stop(self) -> None:
        if self.master:
            self.master.shutdown()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()


async def _listening(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    return True
