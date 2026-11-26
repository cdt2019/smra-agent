"""TCP 中继。跑在中继容器里，把电脑容器发来的连接原样转给宿主上的同号端口。

电脑容器只接在 internal 网络上，够不着宿主；中继同时接在 internal 网络和默认网桥上，
是电脑通向宿主侧服务的唯一一条路。它只转发两个端口：宿主接口（含模型代理）和出网代理。
"""
import asyncio
import os

PORTS = [int(p) for p in os.environ["PORTS"].split(",") if p.strip()]
TARGET_HOST = os.environ["TARGET_HOST"]


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def handler(port: int):
    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, port)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, target_writer), pipe(target_reader, client_writer))
    return handle


async def main() -> None:
    servers = [await asyncio.start_server(handler(p), "0.0.0.0", p) for p in PORTS]
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
