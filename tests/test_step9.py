"""第 9 步：microVM 驱动、休眠与唤醒、keep_awake。

这台机器没有 KVM，所以这里测的是不依赖虚拟机的部分：
  - 客户机代理程序的逻辑（执行、写文件、对时、本地端口转 vsock），在本机上直接跑；
  - microVM 驱动的生命周期（冷启动、挂起、恢复、恢复失败改冷启动、快照、恢复主目录），
    监视器和它的控制接口换成假的，客户机代理程序用真的；
  - 宿主的休眠规则和 keep_awake，用真实的工作进程和运行程序。
真机上的验收在 test_step9_vm.py，设 SMRA_TEST_FIRECRACKER=1 才跑。
"""
from __future__ import annotations

import asyncio
import socket
import sys
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest

from host.computer import State
from host.computer.firecracker import MEM, SNAP, FirecrackerComputer
from host.config import Config
from tests.conftest import all_done, wait_until

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cell" / "vm"))
from guest_agent import Agent

# ---------------------------------------------------------------- 客户机代理程序

async def test_guest_agent_exec_files_clock(tmp_path):
    clock = []
    agent = Agent(home=str(tmp_path), set_clock=clock.append)

    r = await agent.handle({"op": "exec", "argv": [sys.executable, "-c", "import os; print(os.environ['X'])"],
                            "env": {"X": "hello"}, "timeout": 30})
    assert r["ok"] and r["exit_code"] == 0 and r["stdout"].strip() == "hello"
    r = await agent.handle({"op": "exec", "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                            "timeout": 0.5})
    assert r["timed_out"]

    assert (await agent.handle({"op": "put_file", "path": "inbox/a.txt", "data_b64": "aGk="}))["ok"]
    assert (tmp_path / "inbox" / "a.txt").read_bytes() == b"hi"
    escape = await agent.handle({"op": "put_file", "path": "../evil.txt", "data_b64": "aGk="})
    assert not escape["ok"] and not (tmp_path.parent / "evil.txt").exists()

    r = await agent.handle({"op": "set_time", "epoch": 2_000_000_000.5})
    assert r["ok"] and clock == [2_000_000_000.5] and r["skew"] > 0
    assert not (await agent.handle({"op": "rm -rf"}))["ok"]


async def test_guest_agent_relays_local_port_to_vsock(tmp_path):
    """电脑里的程序连 127.0.0.1 上的端口，代理程序把连接原样转到 vsock（这里用一个本地回显服务代替）。"""
    async def echo(reader, writer):
        writer.write(b"echo:" + await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    dialed = []

    def connect(vport):
        dialed.append(vport)
        return socket.create_connection(("127.0.0.1", up_port))

    agent = Agent(home=str(tmp_path), vsock_connect=connect)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        local = s.getsockname()[1]
    assert (await agent.handle({"op": "relay", "ports": {str(local): 10001}}))["ports"] == [local]
    reader, writer = await asyncio.open_connection("127.0.0.1", local)
    writer.write(b"ping")
    await writer.drain()
    assert await reader.read(100) == b"echo:ping"
    writer.close()
    assert dialed == [10001]
    # 宿主换了端口：旧的监听关掉
    assert (await agent.handle({"op": "relay", "ports": {}}))["ports"] == []
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", local)
    upstream.close()


# ---------------------------------------------------------------- microVM 驱动（假的监视器）

class FakeProc:
    def __init__(self):
        self.returncode = None

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeFirecracker(FirecrackerComputer):
    """监视器、控制接口、外部工具是假的；客户机代理程序是真的（在本机上执行）。"""

    def __init__(self, cfg, guest_home: Path):
        super().__init__(cfg)
        self.api_calls: list[tuple[str, str, dict]] = []
        self.guest_ops: list[dict] = []
        self.clock_sets: list[float] = []
        self.guest = Agent(home=str(guest_home), set_clock=self.clock_sets.append)
        self.spawns: list[FakeProc] = []
        self.fail_load = False

    def _kvm_ok(self):
        return True

    async def _run_tool(self, *argv):
        if argv[0] == "mkfs.ext4":
            target = Path(argv[-2] if argv[-1].endswith("M") else argv[-1])
            target.write_bytes(b"ext4:" + argv[4].encode())
        elif argv[0] == "cp":
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        elif argv[0] == "tar" and argv[1] == "-czSf":
            with tarfile.open(argv[2], "w:gz") as tar:
                tar.add(Path(argv[4]) / argv[5], arcname=argv[5])
        elif argv[0] == "tar" and argv[1] == "-xzSf":
            with tarfile.open(argv[2], "r:gz") as tar:
                tar.extract(argv[5], argv[4], filter="data")

    async def _spawn(self):
        proc = FakeProc()
        self.spawns.append(proc)
        return proc

    async def _api(self, method, path, body=None):
        self.api_calls.append((method, path, body or {}))
        if path == "/snapshot/create":
            (self.workdir / SNAP).write_bytes(b"devices")
            (self.workdir / MEM).write_bytes(b"\0" * 4096)
        if path == "/snapshot/load":
            assert (self.workdir / SNAP).exists()
            if self.fail_load:
                raise RuntimeError("快照和内核对不上")
        return {}

    async def _guest_call(self, req, timeout=30):
        self.guest_ops.append(req)
        if req["op"] == "relay":        # 真的开端口会和宿主自己的端口撞上
            return {"ok": True}
        return await self.guest.handle(req)

    async def _start_listeners(self):
        self._listeners = ["listening"]

    async def _stop_listeners(self):
        self._listeners = []

    def paths(self):
        return [p for _, p, _ in self.api_calls]


@pytest.fixture
def fc(tmp_path):
    for name in ("vmlinux", "rootfs.ext4", "firecracker"):
        (tmp_path / name).write_bytes(b"x")
    cfg = Config(data_dir=tmp_path / "data", fc_kernel=str(tmp_path / "vmlinux"),
                 fc_rootfs=str(tmp_path / "rootfs.ext4"), fc_bin=str(tmp_path / "firecracker"), fc_jailer=False,
                 fc_home_gib=0.01)    # Windows 上 truncate 不是稀疏的，8G 会真写满磁盘
    computer = FakeFirecracker(cfg, tmp_path / "guest-home")
    (tmp_path / "guest-home").mkdir()
    computer.configure(8787, 8788)
    return computer


async def test_microvm_driver_lifecycle(fc, tmp_path):
    assert await fc.state() is State.ABSENT
    await fc.ensure_awake()
    assert await fc.state() is State.RUNNING
    # 冷启动：三块盘按顺序挂上（vda 系统盘只读、vdb 主目录盘、vdc 核心盘只读），只有 vsock，没有网卡
    assert fc.paths() == ["/machine-config", "/boot-source", "/drives/rootfs", "/drives/home", "/drives/core",
                          "/vsock", "/actions"]
    drives = {b["drive_id"]: b["is_read_only"] for m, p, b in fc.api_calls if p.startswith("/drives/")}
    assert drives == {"rootfs": True, "home": False, "core": True}
    assert not any("network" in p for p in fc.paths())
    assert [op["op"] for op in fc.guest_ops] == ["ping", "set_time", "relay"]
    assert fc.guest_ops[2]["ports"] == {"8787": 10001, "8788": 10002}
    assert (fc.dir / "core.ext4").exists() and (fc.dir / "home.ext4").exists()
    assert fc.host_url == "http://127.0.0.1:8787" and fc.egress_host == "127.0.0.1"

    r = await fc.exec([sys.executable, "-c", "print('in the vm')"], {}, 30)
    assert r.exit_code == 0 and r.stdout.strip() == "in the vm"
    await fc.put_file("inbox/a.txt", b"attachment")
    assert (tmp_path / "guest-home" / "inbox" / "a.txt").read_bytes() == b"attachment"

    # 挂起：写回缓存、暂停、存状态文件、结束监视器进程
    fc.api_calls.clear()
    fc.guest_ops.clear()
    await fc.suspend()
    assert [op["op"] for op in fc.guest_ops] == ["sync"]
    assert fc.api_calls[0] == ("PATCH", "/vm", {"state": "Paused"})
    assert fc.api_calls[1][1] == "/snapshot/create" and fc.api_calls[1][2]["snapshot_type"] == "Full"
    assert fc.spawns[0].returncode is not None and await fc.state() is State.SUSPENDED
    assert fc.metrics[-1]["what"] == "suspend" and fc.metrics[-1]["state_bytes"] == 4096 + 7

    # 事件到来：新的监视器进程加载状态文件接着跑；对时；状态文件随即删掉
    fc.api_calls.clear()
    fc.guest_ops.clear()
    await fc.ensure_awake()
    assert len(fc.spawns) == 2 and await fc.state() is State.RUNNING
    assert fc.api_calls == [("PUT", "/snapshot/load", {"snapshot_path": SNAP, "resume_vm": True,
                                                       "mem_backend": {"backend_type": "File", "backend_path": MEM}})]
    assert [op["op"] for op in fc.guest_ops] == ["ping", "set_time", "relay"] and len(fc.clock_sets) == 2
    assert not (fc.workdir / SNAP).exists() and not (fc.workdir / MEM).exists()
    assert fc.metrics[-1]["what"] == "resume"

    # 主目录快照：运行中先暂停、拷盘、再继续
    fc.api_calls.clear()
    snap = tmp_path / "snaps" / "home-1.tar.gz"
    await fc.snapshot_home(snap)
    assert [b.get("state") for _, p, b in fc.api_calls if p == "/vm"] == ["Paused", "Resumed"]
    with tarfile.open(snap) as tar:
        assert tar.getnames() == ["home.ext4"]

    # 恢复主目录：关机、丢掉挂起状态，下次冷启动
    (fc.dir / "home.ext4").write_bytes(b"changed")
    await fc.restore_home(snap)
    assert (fc.dir / "home.ext4").read_bytes().startswith(b"ext4:")
    assert await fc.state() is State.STOPPED
    fc.api_calls.clear()
    await fc.ensure_awake()
    assert fc.paths()[-1] == "/actions" and await fc.state() is State.RUNNING


async def test_microvm_resume_failure_falls_back_to_cold_start(fc):
    await fc.ensure_awake()
    await fc.suspend()
    fc.fail_load = True
    fc.api_calls.clear()
    await fc.ensure_awake()
    assert await fc.state() is State.RUNNING
    assert fc.paths()[0] == "/snapshot/load" and fc.paths()[-1] == "/actions"
    assert not (fc.workdir / SNAP).exists()
    assert fc.metrics[-1]["what"] == "cold_start"


async def test_microvm_driver_reports_missing_images(tmp_path):
    cfg = Config(data_dir=tmp_path, fc_jailer=False)
    with pytest.raises(RuntimeError, match=r"build-images\.sh"):
        await FirecrackerComputer(cfg).ensure_awake()


# ---------------------------------------------------------------- 休眠规则与 keep_awake

async def test_idle_computer_is_suspended_and_wakes_for_the_next_event(make_app, upstream):
    app = make_app(idle_suspend_s=1.0)
    await app.start(with_channel=False)
    await app.on_user_text("m1", "你好")
    await wait_until(lambda: app.channel.sent)
    await wait_until(lambda: app.sleeper.suspends == 1, timeout=20)
    assert await app.computer.state() is State.SUSPENDED
    await app.on_user_text("m2", "还在吗")
    await wait_until(lambda: len(app.channel.sent) >= 2)
    assert app.channel.sent[1] == "echo: 还在吗"
    await wait_until(lambda: app.sleeper.suspends == 2, timeout=20)


async def test_keep_awake_is_capped_and_expires(make_app, upstream):
    upstream.script = [
        {"tools": [("keep_awake", {"reason": "跑一个很长的数据处理", "minutes": 600})]},
        {"text": "开始跑了"},
    ]
    app = make_app(idle_suspend_s=1.0)
    await app.start(with_channel=False)
    await app.on_user_text("m1", "后台跑一下")
    await wait_until(lambda: all_done(app) and app.channel.sent)
    result = upstream.requests[1]["body"]["messages"][-1]["content"][0]["content"]
    assert "被截短" in result and "240 分钟" in result
    assert app.sleeper.awake_until is not None
    # 空闲远超 1 秒，但有有效的保持清醒请求：不挂起
    await asyncio.sleep(7)
    assert app.sleeper.suspends == 0 and await app.computer.state() is State.RUNNING
    # 4 小时上限过了：照常挂起
    real = app.sleeper.clock
    app.sleeper.clock = lambda: real() + timedelta(hours=4, minutes=1)
    await wait_until(lambda: app.sleeper.suspends == 1, timeout=20)
    assert app.sleeper.awake_until is None
