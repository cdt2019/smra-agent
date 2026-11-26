from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}

# 响应里的链接：HTML 属性里的，以及正文里出现的绝对地址
_ATTR_LINK = re.compile(r"""(?:href|src|action)\s*=\s*["']([^"'<>\s]+)["']""", re.IGNORECASE)
_BARE_URL = re.compile(r"""https?://[^\s"'<>\\)\]}]+""", re.IGNORECASE)
_TRAILING = ".,;:!?。，；：！？"

MAX_LINKS_PER_RESPONSE = 3000


def normalize(url: str) -> str | None:
    """规范化地址，用于"见过没有"的比较：小写协议和主机、去掉默认端口和片段，其余原样保留。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def extract_links(text: str, base: str) -> set[str]:
    found: set[str] = set()
    for raw in _ATTR_LINK.findall(text):
        if raw.startswith(("javascript:", "mailto:", "data:", "#")):
            continue
        n = normalize(urljoin(base, raw.replace("&amp;", "&")))
        if n:
            found.add(n)
        if len(found) >= MAX_LINKS_PER_RESPONSE:
            return found
    for raw in _BARE_URL.findall(text):
        n = normalize(raw.rstrip(_TRAILING))
        if n:
            found.add(n)
        if len(found) >= MAX_LINKS_PER_RESPONSE:
            break
    return found
