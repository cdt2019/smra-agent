from __future__ import annotations

import asyncio
import ipaddress
import socket


def is_public(ip: str) -> bool:
    """只有公网地址能连。环回、私有网段、链路本地（含云元数据地址）、保留地址一律不行。"""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


class Resolver:
    """代理自己解析域名并检查，检查过的地址就是连接用的地址。

    overrides 只给测试用：把某个主机名当作公网地址，并指定实际连接的地址和端口。
    """

    def __init__(self, overrides: dict[str, tuple[str, int]] | None = None):
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}

    async def resolve_public(self, host: str, port: int) -> tuple[tuple[str, int] | None, str]:
        """返回 ((ip, port), "") 或 (None, 原因)。"""
        host = host.lower().strip("[]")
        if host in self.overrides:
            return self.overrides[host], ""
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            return None, f"解析不了 {host}：{e}"
        ips = [str(info[4][0]) for info in infos]
        if not ips:
            return None, f"解析不了 {host}"
        bad = [ip for ip in ips if not is_public(ip)]
        if bad:
            return None, f"{host} 解析到了非公网地址 {bad[0]}，不允许连接"
        return (ips[0], port), ""
