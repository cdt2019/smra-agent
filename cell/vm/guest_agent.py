"""客户机代理程序（第 5.4 节）。在 microVM 里常驻，由 /sbin/smra-init 启动。

它在不可信的一侧，只是执行者：替宿主起进程、写文件、对时、把两个本地端口转到 vsock。
被攻破也不增加对手的能力，因为它手里没有任何宿主没给电脑的东西。

和宿主之间：
  - 宿主 -> 客户机：宿主连 Firecracker 的 vsock 套接字，发 "CONNECT 52"，之后一问一答，
    每条消息是一行 JSON。一条连接一个请求。
  - 客户机 -> 宿主：电脑里没有网卡。宿主接口和出网代理是客户机 127.0.0.1 上的两个端口，
    代理程序把连接原样转到 vsock 的 10001 / 10002 端口（宿主那边再转给实际的服务）。
    监听哪个本地端口由宿主在每次启动、恢复后用 relay 告诉它，和宿主当前的端口号一致。

只用标准库：根文件系统里有 Python 就能跑，也能在别的系统上单独导入做测试。
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

AGENT_PORT = 52
HOST_CID = 2
MAX_LINE = 64 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024        # 一次执行最多带回这么多输出，多了截掉头部


def _set_clock(epoch: float) -> None:
    """把客户机的系统时间设成宿主给的时间。挂起期间客户机的钟是停着的。"""
    class Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ts = Timespec(int(epoch), int((epoch % 1) * 1e9))
    if libc.clock_settime(0, ctypes.byref(ts)) != 0:      # CLOCK_REALTIME
        raise OSError(ctypes.get_errno(), "clock_settime 失败")


class Agent:
    def __init__(self, home: str = "/home/agent", set_clock=_set_clock, vsock_connect=None):
        self.home = Path(home)
        self.set_clock = set_clock
        self.vsock_connect = vsock_connect or self._vsock_connect
        self.relays: dict[int, tuple[int, asyncio.base_events.Server]] = {}   # 本地端口 -> (vsock 端口, 服务)

    # ---------------------------------------------------------------- 请求

    async def handle(self, req: dict) -> dict:
        op = req.get("op")
        try:
            if op == "ping":
                return {"ok": True, "time": time.time()}
            if op == "exec":
                return await self.exec(req["argv"], req.get("env") or {}, float(req.get("timeout") or 600))
            if op == "put_file":
                return self.put_file(req["path"], base64.b64decode(req["data_b64"]))
            if op == "set_time":
                before = time.time()
                self.set_clock(float(req["epoch"]))
                return {"ok": True, "skew": float(req["epoch"]) - before}
            if op == "sync":
                os.sync() if hasattr(os, "sync") else None
                return {"ok": True}
            if op == "relay":
                return await self.relay({int(k): int(v) for k, v in (req.get("ports") or {}).items()})
            return {"ok": False, "error": f"不认识的操作：{op}"}
        except Exception as e:      # 出错原样报回去，由宿主决定怎么办
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def exec(self, argv: list[str], env: dict, timeout: float) -> dict:
        full_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "HOME": str(self.home), "LANG": "C.UTF-8"}
        if sys.platform == "win32":     # 只为在 Windows 上跑测试：没有它 Python 起不来
            full_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        full_env.update({str(k): str(v) for k, v in env.items()})
        proc = await asyncio.create_subprocess_exec(
            *argv, env=full_env, cwd=self.home, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # 新会话：超时时连同它起的子进程一起杀掉。Agent 自己用 setsid/nohup 起的后台进程不受影响
            start_new_session=sys.platform != "win32",
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            out, err = await proc.communicate()
        return {"ok": True, "exit_code": proc.returncode if proc.returncode is not None else -1,
                "stdout": out[-MAX_OUTPUT:].decode("utf-8", "replace"),
                "stderr": err[-MAX_OUTPUT:].decode("utf-8", "replace"), "timed_out": timed_out}

    def put_file(self, rel: str, data: bytes) -> dict:
        root = self.home.resolve()
        target = (root / rel.lstrip("/")).resolve()
        if target != root and not target.is_relative_to(root):
            return {"ok": False, "error": f"路径不在主目录内：{rel}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"ok": True}

    # ---------------------------------------------------------------- 本地端口 -> vsock

    def _vsock_connect(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.connect((HOST_CID, port))
        return s

    async def relay(self, ports: dict[int, int]) -> dict:
        """ports：本地端口 -> vsock 端口。不在里面的旧监听关掉。"""
        for local in list(self.relays):
            if self.relays[local][0] != ports.get(local):
                self.relays.pop(local)[1].close()
        for local, vport in ports.items():
            if local not in self.relays:
                server = await asyncio.start_server(self._relay_handler(vport), "127.0.0.1", local)
                self.relays[local] = (vport, server)
        return {"ok": True, "ports": sorted(self.relays)}

    def _relay_handler(self, vport: int):
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                sock = await asyncio.to_thread(self.vsock_connect, vport)
                up_reader, up_writer = await asyncio.open_connection(sock=sock)
            except OSError:
                writer.close()
                return
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        return handle

    # ---------------------------------------------------------------- 宿主的请求

    async def serve_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if line:
                reply = await self.handle(json.loads(line))
                writer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        except (ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    async def serve_vsock(self) -> None:
        listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        listener.bind((socket.VMADDR_CID_ANY, AGENT_PORT))
        listener.listen(16)
        listener.setblocking(False)
        server = await asyncio.start_server(self.serve_conn, sock=listener, limit=MAX_LINE)
        await server.serve_forever()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


if __name__ == "__main__":
    asyncio.run(Agent().serve_vsock())
