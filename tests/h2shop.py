"""只会 HTTP/2 的假网站（用 mitmproxy 自带的 h2 库），测出网代理对 HTTP/2 上游的连接复用。

真实网站前面的 Caddy、Nginx 都和浏览器说 HTTP/2。只说 HTTP/1.1 的假网站测不出复用问题：
mitmproxy 在浏览器说 HTTP/2、上游说 HTTP/1.1 时本来就每个请求各开一条连接。
"""
from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable

import h2.config
import h2.connection
import h2.events

# path -> (状态码, 内容类型, 正文)
Routes = Callable[[str], tuple[int, str, bytes]]


class H2Site:
    def __init__(self, routes: Routes) -> None:
        self.routes = routes
        self.connections = 0
        self.requests: list[str] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self, ctx: ssl.SSLContext) -> None:
        ctx.set_alpn_protocols(["h2"])
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server.close_clients()   # 代理会一直留着复用的连接；不关掉 wait_closed 会一直等
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        try:
            await writer.drain()
            while data := await reader.read(65535):
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        path = dict(event.headers)[":path"]
                        self.requests.append(path)
                        status, ctype, body = self.routes(path)
                        conn.send_headers(event.stream_id, [(":status", str(status)), ("content-type", ctype),
                                                            ("content-length", str(len(body)))])
                        conn.send_data(event.stream_id, body, end_stream=True)   # 正文都很小，一帧放得下
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        return
                writer.write(conn.data_to_send())
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
