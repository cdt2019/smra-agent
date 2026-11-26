"""服务档案：一份声明式的数据，告诉裁决者某个服务的请求怎么归类、怎么向你描述。设计文档 11.3。

档案是数据不是代码。少数需要解析请求体才能看懂的服务（比如 Gmail 发信的 raw 字段），
用这里固定的一小组"提取器"；档案只能引用它们的名字。
"""
from __future__ import annotations

import base64
import binascii
import email
import email.policy
import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML

if TYPE_CHECKING:
    from ..creds import Connection

log = logging.getLogger("services")

CLASSES = ("read", "write", "send", "irreversible", "money")

# 档案里的固定请求头（auth.headers），比如 Notion 每个请求都要的 Notion-Version。出网代理换真凭证时补上。
# 只有仓库自带的和你自己放进 data/profiles 的档案能写：Agent 提交的档案要是能加请求头，
# 一个 X-HTTP-Method-Override 就能把归类成"读取"的 GET 变成删除。这些头名谁写都不行：
FIXED_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,40}")
FORBIDDEN_FIXED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "host", "content-length", "content-type",
    "transfer-encoding", "connection", "x-http-method-override", "x-http-method", "x-method-override",
    "x-original-url", "x-rewrite-url", "x-forwarded-host", "x-forwarded-for", "forwarded",
})


# ---------------------------------------------------------------- 档案


@dataclass
class Action:
    id: str
    method: str
    pattern: str
    regex: re.Pattern
    cls: str
    summary: str = ""
    extract: str | None = None
    show: list[str] = field(default_factory=list)
    constraint_keys: list[str] = field(default_factory=list)


@dataclass
class Profile:
    service: str
    title: str
    hosts: list[str]
    auth: dict
    usage: str
    actions: list[Action]
    response_filters: list[str]
    reviewed: bool = True       # False：连接了但还没有你确认过的档案，用的是默认规则
    site: bool = False          # 网站档案：管浏览器在这个网站上发的请求，没有令牌（登录态是浏览器的 Cookie）

    def match(self, method: str, path: str) -> tuple[Action | None, dict]:
        for action in self.actions:
            if action.method not in ("*", method):
                continue
            m = action.regex.fullmatch(path)
            if m:
                return action, {k: v for k, v in m.groupdict().items() if v is not None}
        return None, {}


def _compile(pattern: str) -> re.Pattern:
    """路径模式：{名字} 匹配一段并取出，* 匹配一段，** 匹配剩下的全部。"""
    out = ""
    for token in re.split(r"(\{[a-zA-Z_][a-zA-Z0-9_]*\}|\*\*|\*)", pattern):
        if not token:
            continue
        if token == "**":
            out += ".*"
        elif token == "*":
            out += "[^/]+"
        elif token.startswith("{"):
            out += f"(?P<{token[1:-1]}>[^/]+)"
        else:
            out += re.escape(token)
    return re.compile(out)


SERVICE_NAME = re.compile(r"[a-z][a-z0-9_-]{1,30}")
HOST_NAME = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
METHODS = ("*", "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def valid_host(host: str) -> bool:
    """公网域名。不收 IP、通配符、localhost 一类的名字：替身令牌的绑定范围必须明确。"""
    host = host.strip().lower()
    return bool(HOST_NAME.fullmatch(host)) and not host.endswith((".local", ".localhost", ".internal"))


def _fixed_headers(service: str, auth: dict, trusted: bool) -> None:
    headers = auth.get("headers")
    if headers is None:
        return
    if not trusted:
        raise ValueError(f"{service}: 提交的档案不能写 auth.headers（固定请求头）。需要的话由用户自己加进 data/profiles")
    if not isinstance(headers, dict) or not headers:
        raise ValueError(f"{service}: auth.headers 要是 请求头名: 值 的映射")
    for name, value in headers.items():
        if (not isinstance(name, str) or not FIXED_HEADER_NAME.fullmatch(name)
                or name.lower() in FORBIDDEN_FIXED_HEADERS or name.lower() == str(auth.get("header", "")).lower()):
            raise ValueError(f"{service}: auth.headers 里不能有 {name!r}")
        if not isinstance(value, str) or len(value) > 200 or "\r" in value or "\n" in value:
            raise ValueError(f"{service}: auth.headers 的 {name} 要是一行不超过 200 字的文字")


def parse_profile(data: dict, trusted: bool = False) -> Profile:
    """trusted：仓库自带或你自己放的档案文件。Agent 提交的档案不是。"""
    if not isinstance(data, dict):
        raise ValueError("档案应该是一个 YAML 映射")
    service = str(data.get("service") or "")
    if not SERVICE_NAME.fullmatch(service):
        raise ValueError(f"服务名 {service!r} 不对：小写字母开头，只含小写字母、数字、- 和 _")
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and valid_host(h) for h in hosts):
        raise ValueError(f"{service}: hosts 要是一组公网域名")
    kind = data.get("type", "service")
    if kind not in ("service", "site"):
        raise ValueError(f"{service}: type 只能是 service（接口服务）或 site（浏览器里的网站）")
    site = kind == "site"
    if site:
        # 网站档案没有令牌，也没有响应过滤（浏览器的响应不经过它）
        if data.get("auth") or data.get("response_filters"):
            raise ValueError(f"{service}: 网站档案不写 auth 和 response_filters")
        auth: dict = {}
    else:
        auth = data.get("auth") or {"header": "Authorization", "scheme": "Bearer"}
        if not isinstance(auth, dict) or not (isinstance(auth.get("header"), str) or isinstance(auth.get("query"), str)):
            raise ValueError(f"{service}: auth 要写明令牌放在哪个请求头（header）或查询参数（query）")
        _fixed_headers(service, auth, trusted)
    actions = []
    for n, raw in enumerate(data.get("actions") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"{service}: 规则 {n} 应该是一个映射")
        match = raw.get("match") or {}
        cls = raw.get("class")
        if cls not in CLASSES:
            raise ValueError(f"{service}: 规则 {n} 的类别 {cls!r} 不对")
        if str(match.get("method", "*")).upper() not in METHODS:
            raise ValueError(f"{service}: 规则 {n} 的方法 {match.get('method')!r} 不对")
        if raw.get("extract") and (site or raw["extract"] not in EXTRACTORS):
            raise ValueError(f"{service}: 规则 {n} 引用了不存在的提取器 {raw['extract']!r}")
        for key in ("show", "constraint_keys"):
            if not isinstance(raw.get(key) or [], list):
                raise ValueError(f"{service}: 规则 {n} 的 {key} 要是列表")
        actions.append(Action(
            id=raw.get("id") or f"{match.get('method', '*')} {match.get('path', '/**')}",
            method=str(match.get("method", "*")).upper(),
            pattern=match.get("path", "/**"),
            regex=_compile(match.get("path", "/**")),
            cls=cls,
            summary=raw.get("summary", ""),
            extract=raw.get("extract"),
            show=list(raw.get("show") or []),
            constraint_keys=list(raw.get("constraint_keys") or []),
        ))
    unknown = [f for f in data.get("response_filters") or [] if f not in RESPONSE_FILTERS]
    if unknown:
        raise ValueError(f"{data.get('service')}: 不认识的响应过滤器 {unknown}")
    return Profile(
        service=service, title=str(data.get("title") or service),
        hosts=[h.lower() for h in hosts], auth=auth,
        usage=str(data.get("usage") or ""), actions=actions,
        response_filters=list(data.get("response_filters") or []), site=site,
    )


def load_profiles(*dirs: Path) -> dict[str, Profile]:
    """后面的目录覆盖前面的同名档案（你的 data/profiles 覆盖仓库自带的）。服务档案和网站档案都在里面，
    用 Profile.site 区分；同一个名字只能是其中一种。"""
    yaml = YAML(typ="safe")
    profiles: dict[str, Profile] = {}
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(d.glob("*.yaml")):
            try:
                profile = parse_profile(yaml.load(path.read_text(encoding="utf-8")), trusted=True)
                earlier = profiles.get(profile.service)
                # 覆盖仓库档案的（比如你批准了 Agent 改写的 notion 档案）沿用原来的固定请求头：
                # Agent 的提交写不了 auth.headers，不沿用的话 Notion 就少了 Notion-Version
                if (earlier is not None and earlier.auth.get("headers") and not profile.site
                        and "headers" not in profile.auth):
                    profile.auth = {**profile.auth, "headers": dict(earlier.auth["headers"])}
                    try:
                        _fixed_headers(profile.service, profile.auth, True)
                    except ValueError:
                        profile.auth.pop("headers")
                profiles[profile.service] = profile
            except Exception as e:
                log.error("档案 %s 读不了：%s", path, e)
    return profiles


# ---------------------------------------------------------------- 从请求里取出给你看的字段


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _mail_fields(raw_b64: str) -> dict:
    msg = email.message_from_bytes(_b64url_decode(raw_b64), policy=email.policy.default)
    fields: dict = {}
    for key in ("to", "cc", "bcc"):
        addrs = [a for _, a in getaddresses(msg.get_all(key, []))]
        if addrs:
            fields[key] = addrs
    # 约束按全部收件人算：只授权了发给张三，就不能顺手密送给别人
    fields["recipients"] = fields.get("to", []) + fields.get("cc", []) + fields.get("bcc", [])
    fields["subject"] = str(msg.get("subject", ""))
    body = msg.get_body(preferencelist=("plain", "html"))
    fields["text"] = body.get_content() if body is not None else ""
    return fields


def _extract_gmail_raw(body: dict) -> dict:
    return _mail_fields(body["raw"])


def _extract_gmail_draft(body: dict) -> dict:
    return _mail_fields(body["message"]["raw"])


NO_ATTENDEES = "（只有你自己）"
ATTENDEES_UNCHANGED = "（参与人不变）"


def _when(value) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("dateTime") or value.get("date") or "") + (
        f"（{value['timeZone']}）" if value.get("timeZone") else "")


def _calendar_event(body: dict, absent: str) -> dict:
    """日程的标题、时间、地点、参与人。参与人会收到邀请邮件，所以约束按参与人算：
    长期授权"只有你自己"的日程，不覆盖拉了别人的日程。"""
    if not isinstance(body, dict):
        raise TypeError("请求体不是一个 JSON 对象")
    fields: dict[str, Any] = {"title": str(body.get("summary") or ""), "start": _when(body.get("start")),
              "end": _when(body.get("end")), "location": str(body.get("location") or ""),
              "text": str(body.get("description") or "")}
    if "attendees" in body:
        people = [str(a.get("email", "")).lower() for a in body["attendees"] or [] if isinstance(a, dict)]
        fields["attendees"] = [p for p in people if p] or [NO_ATTENDEES]
    else:
        fields["attendees"] = [absent]
    return fields


def _extract_gcal_event(body: dict) -> dict:
    return _calendar_event(body, NO_ATTENDEES)          # 新建、整个替换：没写参与人就是没有


def _extract_gcal_patch(body: dict) -> dict:
    return _calendar_event(body, ATTENDEES_UNCHANGED)   # 部分修改：没写参与人就是不动


def _extract_drive_permission(body: dict) -> dict:
    """云盘分享给谁。约束按对象算：授权过分享给张三，不覆盖分享给所有人。"""
    if not isinstance(body, dict):
        raise TypeError("请求体不是一个 JSON 对象")
    kind = str(body.get("type") or "")
    if kind == "anyone":
        grantee = "anyone"
    elif kind == "domain":
        grantee = f"domain:{body.get('domain', '')}".lower()
    else:
        grantee = str(body.get("emailAddress") or "").lower()
    if not grantee or grantee == "domain:":
        raise ValueError("看不出分享给谁")
    return {"grantee": [grantee], "role": str(body.get("role") or "")}


EXTRACTORS = {"gmail_raw": _extract_gmail_raw, "gmail_draft_raw": _extract_gmail_draft,
              "gcal_event": _extract_gcal_event, "gcal_patch": _extract_gcal_patch,
              "drive_permission": _extract_drive_permission}

FIELD_LABELS = {"to": "收件人", "cc": "抄送", "bcc": "密送", "subject": "主题", "text": "正文",
                "recipients": "全部收件人", "title": "标题", "start": "开始", "end": "结束",
                "location": "地点", "attendees": "参与人（会收到邀请）",
                "grantee": "分享给（anyone 表示任何拿到链接的人）", "role": "权限"}


def request_fields(action: Action | None, params: dict, body: bytes) -> tuple[dict, str | None]:
    """返回 (字段, 解析问题)。字段包括路径参数、JSON 请求体的顶层字段（body.<名字>）、提取器的结果。"""
    fields: dict = dict(params)
    problem = None
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            if isinstance(v, (str, int, float, bool)):
                fields[f"body.{k}"] = v
    if action and action.extract:
        try:
            # 空请求体当 {}；有内容却不是 JSON 对象的，原样交给提取器，让它报错而不是当成空日程
            fields.update(EXTRACTORS[action.extract](parsed if body else {}))  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError, binascii.Error) as e:
            problem = f"请求体解析不了（{type(e).__name__}），下面给出原始内容"
    return fields, problem


def render_summary(template: str, fields: dict) -> str:
    def value(m):
        v = fields.get(m.group(1), "?")
        return ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    return re.sub(r"\{([\w.]+)\}", value, template)


def constraint_of(action: Action | None, fields: dict) -> dict:
    """这个请求在约束键上的取值，比如 {"recipients": ["zhangsan@example.com"]}。"""
    if not action:
        return {}
    out = {}
    for key in action.constraint_keys:
        v = fields.get(key)
        out[key] = [str(x).lower() for x in (v if isinstance(v, list) else [v] if v is not None else [])]
    return out


def constraint_covers(grant: dict, request: dict) -> bool:
    """授权的约束是否覆盖这个请求：每个约束键上，请求的每个值都要被授权里的某个值（可含通配符）匹配。"""
    for key, allowed in grant.items():
        values = request.get(key)
        if not values:
            return False
        for v in values:
            if not any(fnmatch.fnmatchcase(v, pattern.lower()) for pattern in allowed):
                return False
    return True


# ---------------------------------------------------------------- 响应过滤


_OTP_DIGITS = re.compile(r"(?<![\d])\d{4,8}(?![\d])")
_OTP_WORDS = re.compile(r"验证码|校验码|动态码|动态密码|确认码|安全码|登录码|激活码|code|otp|passcode|pin\b|"
                        r"verification|one[- ]time", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_LINK = re.compile(r"reset|password|passwd|verify|verification|confirm|magic|login|signin|sign-in|"
                             r"auth|token|otp|activate|recover", re.IGNORECASE)
OTP_MASK = "[已隐藏的验证码]"
LINK_MASK = "[已隐藏的登录或重置链接]"


_SENTENCE_END = re.compile(r"[。！？!?\n；;]|\.\s")


def scrub_secrets(text: str) -> str:
    """抹掉验证码、密码重置链接、免密登录链接。能读邮箱不应该等于能接管绑定这个邮箱的账号。

    验证码的判断：同一句话里，数字前面不远处（或紧跟在后面）有"验证码""code"之类的词。
    """
    text = _URL.sub(lambda m: LINK_MASK if _SENSITIVE_LINK.search(m.group(0)) else m.group(0), text)

    def otp(m):
        before = text[max(0, m.start() - 24):m.start()]
        before = _SENTENCE_END.split(before)[-1]
        after = _SENTENCE_END.split(text[m.end():m.end() + 12])[0]
        return OTP_MASK if _OTP_WORDS.search(before) or _OTP_WORDS.search(after) else m.group(0)
    return _OTP_DIGITS.sub(otp, text)


def _scrub_json(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("data", "raw") and isinstance(v, str) and v:
                out[k] = _scrub_b64(v)
            else:
                out[k] = _scrub_json(v)
        return out
    if isinstance(node, list):
        return [_scrub_json(v) for v in node]
    if isinstance(node, str):
        return scrub_secrets(node)
    return node


def _scrub_b64(value: str) -> str:
    """Gmail 的正文是 base64url 编码的：解开、抹掉、再编回去。解不开的原样保留。"""
    try:
        decoded = _b64url_decode(value).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value
    cleaned = scrub_secrets(decoded)
    if cleaned == decoded:
        return value
    return base64.urlsafe_b64encode(cleaned.encode("utf-8")).decode("ascii").rstrip("=")


def filter_email_secrets(body: bytes, content_type: str) -> bytes:
    if "json" in content_type:
        try:
            return json.dumps(_scrub_json(json.loads(body)), ensure_ascii=False).encode("utf-8")
        except ValueError:
            pass
    if content_type.startswith("text/") or "json" in content_type or "message/" in content_type:
        return scrub_secrets(body.decode("utf-8", "replace")).encode("utf-8")
    return body


RESPONSE_FILTERS = {"email_secrets": filter_email_secrets}


# ---------------------------------------------------------------- 认出发往已连接服务的请求


@dataclass
class ServiceCall:
    profile: Profile
    connection: Connection | None   # 网站（浏览器）没有连接
    action: Action | None
    cls: str
    fields: dict
    problem: str | None
    constraint: dict


DEFAULT_USAGE = ("还没有用户确认过的服务档案。GET 和 HEAD 直接放行；其余请求每次都要用户批准，"
                 "审批里展示原始请求。写好调用方式后，可以用 profile_propose 提交一份档案给用户审阅。")


def effective_profile(profile: Profile | None, conn) -> Profile | None:
    """一个连接实际按什么规则判。

    绑定主机以连接为准（你在终端里确认过的）。档案只能在这个范围里收窄，不能扩大：
    就算档案写了别的主机，替身令牌也不会被换成真凭证发过去。
    """
    if conn.hosts is None:
        return profile                       # 仓库自带档案的服务（如 Gmail），主机写在档案里
    if profile is None:
        return Profile(service=conn.service, title=conn.title or conn.service, hosts=list(conn.hosts),
                       auth=conn.auth or {"header": "Authorization", "scheme": "Bearer"},
                       usage=DEFAULT_USAGE, actions=[], response_filters=[], reviewed=False)
    hosts = [h for h in profile.hosts if h in conn.hosts]
    return Profile(service=profile.service, title=profile.title, hosts=hosts, auth=profile.auth,
                   usage=profile.usage, actions=profile.actions, response_filters=profile.response_filters)


class ServiceRegistry:
    def __init__(self, profiles: dict[str, Profile], creds):
        self.profiles = profiles
        self.creds = creds

    def connected(self) -> list[tuple[Profile, Connection]]:
        """已经连接的服务和实际生效的规则。没有档案的连接按默认规则：读放行，其余逐个问你。"""
        out = []
        for conn in self.creds.list():
            own = self.profiles.get(conn.service)
            profile = effective_profile(None if own is not None and own.site else own, conn)
            if profile is None:
                log.warning("已连接的服务 %s 既没有档案也没有绑定主机，用不了", conn.service)
                continue
            out.append((profile, conn))
        return out

    def surrogate_in(self, profile: Profile, headers, query: dict) -> str | None:
        auth = profile.auth
        if "header" in auth:
            value = headers.get(auth["header"], "")
            scheme = auth.get("scheme")
            if scheme and value.lower().startswith(scheme.lower() + " "):
                value = value[len(scheme) + 1:]
            return value.strip() or None
        if "query" in auth:
            return query.get(auth["query"])
        return None

    def identify(self, host: str, method: str, path: str, headers, query: dict, body: bytes) -> ServiceCall | None:
        """请求带着某个服务的替身令牌、而且目的主机属于这个服务：这就是一次对该服务的调用。

        替身令牌发往别的主机不会被认出来，也就不会被换成真凭证。
        """
        for profile, conn in self.connected():
            if host not in profile.hosts:
                continue
            token = self.surrogate_in(profile, headers, query)
            if not token or token != conn.surrogate:
                continue
            action, params = profile.match(method, path)
            cls = action.cls if action else ("read" if method in ("GET", "HEAD") else "irreversible")
            fields, problem = request_fields(action, params, body)
            return ServiceCall(profile, conn, action, cls, fields, problem, constraint_of(action, fields))
        return None

    def site_call(self, host: str, method: str, path: str, query: str, body: bytes,
                  content_type: str) -> ServiceCall | None:
        """浏览器发往某个有网站档案的主机、而且匹配到了档案里的一条规则。没匹配到规则的照旧按浏览器的默认规则判。"""
        for profile in self.profiles.values():
            if not profile.site or host not in profile.hosts:
                continue
            action, params = profile.match(method, path)
            if action is None:
                return None
            fields = site_fields(params, query, body, content_type)
            return ServiceCall(profile, None, action, action.cls, fields, None, constraint_of(action, fields))
        return None


def site_fields(params: dict, query: str, body: bytes, content_type: str) -> dict:
    """网站请求里给你看、给约束用的字段：路径参数、查询参数（query.<名字>）、
    表单字段（form.<名字>，密码类只显示被隐藏了）、JSON 请求体的顶层字段（body.<名字>）。"""
    from urllib.parse import parse_qsl
    fields: dict = dict(params)
    for k, v in parse_qsl(query, keep_blank_values=True):
        fields.setdefault(f"query.{k}", v)
    ct = content_type.lower()
    if body and "application/x-www-form-urlencoded" in ct:
        from ..browser import form_fields  # 和审批里展示的表单一样：密码类只显示被隐藏了
        for k, v in form_fields(body, content_type):
            fields.setdefault(f"form.{k}", v)
    elif body and "json" in ct:
        fields.update(request_fields(None, {}, body)[0])
    return fields
