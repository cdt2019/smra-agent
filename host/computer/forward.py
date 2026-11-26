"""把一个监听上收到的连接原样转给宿主上的某个端口。microVM 和容器驱动都用。"""
from __future__ import annotations

import asyncio


def forward_to(host: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    return handle


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
