"""microVM 实现（第 9 步，设计文档第 5 节）。

一台 Firecracker microVM 就是 Agent 的电脑：

    系统盘   data/vm/rootfs.ext4   只读，客户机里叠一层内存可写层（cell/vm/smra-init）
    主目录盘 data/vm/home.ext4     读写，永久；它的大小就是磁盘配额
    核心盘   data/vm/core.ext4     只读，运行程序。每次冷启动由宿主重建，所以改了代码冷启动就生效
    vsock                          唯一的通道。没有网卡

和设计文档的一处不同：文档写的是"一块 tap 网卡 + 宿主侧规则只放行到两个代理"。这里干脆不给网卡，
宿主接口和出网代理都走 vsock：客户机代理程序在客户机的 127.0.0.1 上开两个端口（号码和宿主的一样），
连接原样转到 vsock；宿主在 Firecracker 的 vsock 套接字旁边监听 <uds>_10001 / <uds>_10002，
再转给本机的宿主接口和出网代理。电脑没有路由、没有 DNS，也就不需要防火墙规则去堵。

挂起（suspend）：先让客户机把缓存写回主目录盘，暂停虚拟机，把内存和设备状态写成两个文件，
然后结束监视器进程。宿主上不再有它的进程，CPU 和内存都归零。
恢复（resume）：起一个新的监视器进程，加载这两个文件，接着跑；然后对时、重新告诉客户机端口号。
加载成功后状态文件立刻删掉：之后主目录盘会变，旧的内存快照和它对不上，留着只会在出错时被误用。

经监禁程序（jailer）启动时，监视器跑在一个独立的非特权用户、独立的根目录和 cgroup 里。
jailer 要求宿主进程是 root；开发时可以设 SMRA_FC_JAILER=0，以当前用户直接启动（要能读写 /dev/kvm）。

**这份实现写完时所在的机器没有 KVM，没有实际运行过。** 测试里用假的监视器和 API 覆盖了生命周期；
真机上的验收测试在 tests/test_step9_vm.py，设 SMRA_TEST_FIRECRACKER=1 才跑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from .base import ExecResult, State
from .forward import forward_to

log = logging.getLogger("computer.firecracker")

ROOT = Path(__file__).resolve().parent.parent.parent
CELL_FILES = [ROOT / "cell" / "agent_run.py", ROOT / "cell" / "cell_tools.py", ROOT / "cell" / "vm" / "guest_agent.py"]

AGENT_PORT = 52                 # 客户机代理程序在 vsock 上监听的端口
VPORT_HOST, VPORT_EGRESS = 10001, 10002
GUEST_CID = 3
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off quiet init=/sbin/smra-init"
JAIL_ID = "smra-cell"

# 挂进虚拟机的文件在工作目录（jailer 的根目录）里的名字。API 里用的都是这些相对路径
KERNEL, ROOTFS, HOME_DISK, CORE_DISK = "vmlinux", "rootfs.ext4", "home.ext4", "core.ext4"
SNAP, MEM, API_SOCK, VSOCK = "vm.snap", "vm.mem", "api.sock", "v.sock"


class FirecrackerComputer:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.dir = Path(cfg.data_dir) / "vm"
        self.agent_argv = ["python3", "/opt/agent/agent_run.py"]
        self.host_url = ""
        self.home_path = "/home/agent"
        self.exec_allowed = True            # 命令在虚拟机里执行
        self.egress_host = "127.0.0.1"      # 客户机代理程序在这里开着转到 vsock 的端口
        self.no_proxy = "127.0.0.1,localhost"
        self._host_port = 0
        self._egress_port = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._listeners: list[asyncio.base_events.Server] = []
        self.metrics: list[dict] = []

    # ---------------------------------------------------------------- 路径

    @property
    def workdir(self) -> Path:
        """监视器看到的根目录。用 jailer 时是它建的 chroot，否则是 data/vm/run。"""
        if self.cfg.fc_jailer:
            return self.chroot_base / "firecracker" / JAIL_ID / "root"
        return self.dir / "run"

    @property
    def chroot_base(self) -> Path:
        # 默认放在数据目录下：主目录盘要能硬链接进去，必须在同一个文件系统上
        return Path(self.cfg.fc_chroot_base) if self.cfg.fc_chroot_base else self.dir / "jail"

    def _source(self, name: str) -> Path:
        return {KERNEL: Path(self.cfg.fc_kernel or self.dir / KERNEL),
                ROOTFS: Path(self.cfg.fc_rootfs or self.dir / ROOTFS)}.get(name, self.dir / name)

    def configure(self, host_port: int, egress_port: int) -> None:
        self._host_port = host_port
        self._egress_port = egress_port
        self.host_url = f"http://127.0.0.1:{host_port}"

    # ---------------------------------------------------------------- 状态

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def state(self) -> State:
        if not (self.dir / HOME_DISK).exists():
            return State.ABSENT
        if self._alive():
            return State.RUNNING
        if (self.workdir / SNAP).exists() and (self.workdir / MEM).exists():
            return State.SUSPENDED
        return State.STOPPED

    # ---------------------------------------------------------------- 准备

    async def create(self) -> None:
        missing = [str(p) for p in (self._source(KERNEL), self._source(ROOTFS), Path(self.cfg.fc_bin))
                   if not p.exists()]
        if self.cfg.fc_jailer and not Path(self.cfg.fc_jailer_bin).exists():
            missing.append(self.cfg.fc_jailer_bin)
        if missing:
            raise RuntimeError(f"microVM 驱动缺少：{', '.join(missing)}。先在仓库根目录执行 sh cell/vm/build-images.sh")
        if not self._kvm_ok():
            raise RuntimeError("没有可用的 /dev/kvm：这台机器跑不了 microVM")
        self.dir.mkdir(parents=True, exist_ok=True)
        home = self.dir / HOME_DISK
        if not home.exists():
            tmp = home.with_suffix(".partial")
            with open(tmp, "wb") as f:  # noqa: ASYNC230  只建一次，截断不写数据
                f.truncate(int(self.cfg.fc_home_gib * 1024 ** 3))     # 稀疏文件：用多少占多少
            await self._run_tool("mkfs.ext4", "-q", "-F", "-L", "smra-home", str(tmp))
            tmp.replace(home)
            log.info("建了主目录盘 %s（%s GiB）", home, self.cfg.fc_home_gib)

    def _kvm_ok(self) -> bool:
        return os.access("/dev/kvm", os.R_OK | os.W_OK)

    async def _build_core(self) -> None:
        """核心盘：运行程序。只在冷启动时重建；挂起中的虚拟机挂着旧的，不能换。"""
        staging = self.dir / "core-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for f in CELL_FILES:
            shutil.copy2(f, staging / f.name)
        target = self.dir / CORE_DISK
        target.unlink(missing_ok=True)
        await self._run_tool("mkfs.ext4", "-q", "-L", "smra-core", "-d", str(staging), str(target), "16M")
        shutil.rmtree(staging, ignore_errors=True)

    def _prepare_workdir(self) -> None:
        """把要挂的文件硬链接进监视器的根目录。主目录盘必须是同一个文件（同一个 inode），
        虚拟机写进去的东西才留在 data/vm/home.ext4 里，所以链接失败时不退回到复制。"""
        wd = self.workdir
        wd.mkdir(parents=True, exist_ok=True)
        for name in (KERNEL, ROOTFS, HOME_DISK, CORE_DISK):
            dst = wd / name
            dst.unlink(missing_ok=True)
            try:
                os.link(self._source(name), dst)
            except OSError as e:
                if name == HOME_DISK:
                    raise RuntimeError(f"主目录盘没法硬链接进 {wd}（{e}）：jailer 的根目录要和数据目录在同一个文件系统上") from None
                shutil.copy2(self._source(name), dst)
            self._own(dst)
        for name in (API_SOCK, VSOCK):
            (wd / name).unlink(missing_ok=True)

    def _own(self, path: Path) -> None:
        if self.cfg.fc_jailer:
            os.chown(path, self.cfg.fc_uid, self.cfg.fc_gid)

    # ---------------------------------------------------------------- 外部程序与接口（测试里换成假的）

    async def _run_tool(self, *argv: str) -> None:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"{argv[0]} 失败：{err.decode('utf-8', 'replace').strip()[-500:]}")

    async def _spawn(self) -> asyncio.subprocess.Process:
        cfg = self.cfg
        if cfg.fc_jailer:
            argv = [cfg.fc_jailer_bin, "--id", JAIL_ID, "--exec-file", cfg.fc_bin,
                    "--uid", str(cfg.fc_uid), "--gid", str(cfg.fc_gid),
                    "--chroot-base-dir", str(self.chroot_base),
                    "--", "--api-sock", f"/{API_SOCK}"]
            cwd = None
        else:
            argv = [cfg.fc_bin, "--api-sock", API_SOCK]
            cwd = self.workdir
        logfile = open(self.dir / "firecracker.log", "ab")  # noqa: ASYNC230  交给子进程写
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=logfile, stderr=logfile)
        finally:
            logfile.close()
        deadline = time.monotonic() + 10
        while not (self.workdir / API_SOCK).exists():
            if proc.returncode is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Firecracker 没起来，见 {self.dir / 'firecracker.log'}")
            await asyncio.sleep(0.02)
        return proc

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Firecracker 的控制接口：unix 套接字上的 HTTP。"""
        reader, writer = await asyncio.open_unix_connection(str(self.workdir / API_SOCK))
        try:
            payload = json.dumps(body or {}).encode()
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                         f"Accept: application/json\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
            await writer.drain()
            status_line = await reader.readline()
            status = int(status_line.split()[1])
            length = 0
            while (line := await reader.readline()) not in (b"\r\n", b""):
                key, _, value = line.decode().partition(":")
                if key.strip().lower() == "content-length":
                    length = int(value.strip())
            data = await reader.readexactly(length) if length else b""
        finally:
            writer.close()
        if status >= 300:
            raise RuntimeError(f"Firecracker {method} {path} -> {status}：{data.decode('utf-8', 'replace')[:300]}")
        return json.loads(data) if data else {}

    async def _guest_call(self, req: dict, timeout: float = 30) -> dict:
        """宿主 -> 客户机代理程序：连 vsock 套接字，CONNECT 52，一问一答。"""
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.workdir / VSOCK), limit=64 * 1024 * 1024), 5)
        try:
            writer.write(f"CONNECT {AGENT_PORT}\n".encode())
            await writer.drain()
            ack = await asyncio.wait_for(reader.readline(), 5)
            if not ack.startswith(b"OK"):
                raise ConnectionError(f"vsock 握手失败：{ack!r}")
            writer.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
        if not line:
            raise ConnectionError("客户机代理程序没有回应")
        return json.loads(line)

    async def _start_listeners(self) -> None:
        """客户机 -> 宿主：Firecracker 把客户机连 CID 2 端口 P 的连接，转成连宿主上的 <uds>_P。"""
        await self._stop_listeners()
        for vport, port in ((VPORT_HOST, self._host_port), (VPORT_EGRESS, self._egress_port)):
            path = self.workdir / f"{VSOCK}_{vport}"
            path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(forward_to("127.0.0.1", port), path=str(path))
            self._own(path)
            self._listeners.append(server)

    async def _stop_listeners(self) -> None:
        for server in self._listeners:
            server.close()
        self._listeners = []

    # ---------------------------------------------------------------- 生命周期

    async def _wait_guest(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if (await self._guest_call({"op": "ping"}, 5)).get("ok"):
                    return
            except (TimeoutError, OSError, ConnectionError, ValueError):
                pass
            if time.monotonic() > deadline or not self._alive():
                raise RuntimeError(f"客户机没有就绪，见 {self.dir / 'firecracker.log'}")
            await asyncio.sleep(0.1)

    async def _attach(self) -> None:
        """启动或恢复之后：开宿主侧的监听、对时、告诉客户机当前的端口号。"""
        await self._start_listeners()
        await self._wait_guest()
        await self._sync_clock()
        reply = await self._guest_call({"op": "relay", "ports": {str(self._host_port): VPORT_HOST,
                                                                  str(self._egress_port): VPORT_EGRESS}})
        if not reply.get("ok"):
            raise RuntimeError(f"客户机没能开转发端口：{reply.get('error')}")

    async def _sync_clock(self) -> None:
        reply = await self._guest_call({"op": "set_time", "epoch": time.time()})
        if not reply.get("ok"):
            log.warning("客户机对时失败：%s", reply.get("error"))
        elif abs(reply.get("skew", 0)) > 2:
            log.info("客户机时钟差了 %.0f 秒，已对好", reply["skew"])

    def _record(self, what: str, **values) -> None:
        entry = {"at": time.time(), "what": what, **values}
        self.metrics.append(entry)
        with open(self.dir / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    async def start(self) -> None:
        """冷启动。挂起前的进程都没了；系统盘上的可写层清空。"""
        t0 = time.monotonic()
        self._discard_snapshot()
        await self._build_core()
        self._prepare_workdir()
        self._proc = await self._spawn()
        cfg = self.cfg
        await self._api("PUT", "/machine-config", {"vcpu_count": cfg.fc_vcpus, "mem_size_mib": cfg.fc_mem_mib})
        await self._api("PUT", "/boot-source", {"kernel_image_path": KERNEL, "boot_args": BOOT_ARGS})
        # 加的顺序就是客户机里的设备顺序：vda 系统盘、vdb 主目录盘、vdc 核心盘
        await self._api("PUT", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": ROOTFS,
                                                  "is_root_device": True, "is_read_only": True})
        await self._api("PUT", "/drives/home", {"drive_id": "home", "path_on_host": HOME_DISK,
                                                "is_root_device": False, "is_read_only": False})
        await self._api("PUT", "/drives/core", {"drive_id": "core", "path_on_host": CORE_DISK,
                                                "is_root_device": False, "is_read_only": True})
        await self._api("PUT", "/vsock", {"guest_cid": GUEST_CID, "uds_path": VSOCK})
        await self._api("PUT", "/actions", {"action_type": "InstanceStart"})
        await self._attach()
        self._record("cold_start", seconds=round(time.monotonic() - t0, 3))
        log.info("电脑冷启动完成（%.1f 秒）", time.monotonic() - t0)

    async def ensure_awake(self) -> None:
        current = await self.state()
        if current is State.ABSENT:
            await self.create()
            await self.start()
        elif current is State.STOPPED:
            await self.start()
        elif current is State.SUSPENDED:
            await self.resume()
        elif not self._listeners:
            await self._attach()        # 宿主进程重启过，虚拟机还在：重新接上

    async def suspend(self) -> None:
        if await self.state() is not State.RUNNING:
            return
        t0 = time.monotonic()
        try:
            await self._guest_call({"op": "sync"}, 30)      # 缓存写回主目录盘，快照主目录时才完整
        except (TimeoutError, OSError, ConnectionError, ValueError) as e:
            log.warning("挂起前写回缓存失败：%s", e)
        await self._api("PATCH", "/vm", {"state": "Paused"})
        await self._api("PUT", "/snapshot/create", {"snapshot_type": "Full", "snapshot_path": SNAP,
                                                    "mem_file_path": MEM})
        await self._kill()
        await self._stop_listeners()
        size = sum((self.workdir / n).stat().st_size for n in (SNAP, MEM))
        self._record("suspend", seconds=round(time.monotonic() - t0, 3), state_bytes=size)
        log.info("电脑已挂起（%.1f 秒，状态文件 %d MB），宿主上不再有它的进程", time.monotonic() - t0, size >> 20)

    async def resume(self) -> None:
        if await self.state() is not State.SUSPENDED:
            return
        t0 = time.monotonic()
        try:
            for name in (API_SOCK, VSOCK):
                (self.workdir / name).unlink(missing_ok=True)
            self._proc = await self._spawn()
            await self._api("PUT", "/snapshot/load", {
                "snapshot_path": SNAP, "mem_backend": {"backend_type": "File", "backend_path": MEM},
                "resume_vm": True})
            await self._attach()
        except Exception:
            # 恢复不了就冷启动：挂起前的进程没了，但主目录盘还在
            log.exception("从挂起状态恢复失败，改为冷启动")
            await self._kill()
            await self.start()
            return
        self._discard_snapshot()
        self._record("resume", seconds=round(time.monotonic() - t0, 3))
        log.info("电脑已恢复（%.2f 秒）", time.monotonic() - t0)

    def _discard_snapshot(self) -> None:
        for name in (SNAP, MEM):
            (self.workdir / name).unlink(missing_ok=True)

    async def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        self._proc = None

    async def destroy(self) -> None:
        """关掉虚拟机、丢掉挂起状态和工作目录。主目录盘留着。"""
        await self._kill()
        await self._stop_listeners()
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ---------------------------------------------------------------- 执行与文件

    async def exec(self, argv: list[str], env: dict[str, str], timeout: float) -> ExecResult:
        env = {**env, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        reply = await self._guest_call({"op": "exec", "argv": argv, "env": env, "timeout": timeout}, timeout + 30)
        if not reply.get("ok"):
            raise RuntimeError(f"客户机执行失败：{reply.get('error')}")
        return ExecResult(reply["exit_code"], reply["stdout"], reply["stderr"], reply.get("timed_out", False))

    async def put_file(self, rel_path: str, data: bytes) -> None:
        import base64
        reply = await self._guest_call({"op": "put_file", "path": rel_path,
                                        "data_b64": base64.b64encode(data).decode("ascii")}, 60)
        if not reply.get("ok"):
            raise ValueError(reply.get("error"))

    async def snapshot_home(self, dest: Path) -> None:
        """在暂停的瞬间拷一份主目录盘（能写时复制就写时复制），再接着跑。快照是装着盘镜像的 tar.gz。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = self.dir / "snapshot-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        running = await self.state() is State.RUNNING
        if running:
            await self._guest_call({"op": "sync"}, 30)
            await self._api("PATCH", "/vm", {"state": "Paused"})
        try:
            await self._run_tool("cp", "--reflink=auto", "--sparse=always", str(self.dir / HOME_DISK),
                                 str(staging / HOME_DISK))
        finally:
            if running:
                await self._api("PATCH", "/vm", {"state": "Resumed"})
        tmp = dest.with_suffix(dest.suffix + ".partial")
        try:
            # GNU tar 的 -S 保留稀疏：没用到的部分不占快照的空间，恢复出来也还是稀疏的
            await self._run_tool("tar", "-czSf", str(tmp), "-C", str(staging), HOME_DISK)
            tmp.replace(dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def restore_home(self, src: Path) -> None:
        """换掉主目录盘。内存里的东西和新盘对不上，所以虚拟机关掉、挂起状态丢掉，下次冷启动。"""
        await self._kill()
        await self._stop_listeners()
        self._discard_snapshot()

        staging = self.dir / "restore-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            await self._run_tool("tar", "-xzSf", str(src), "-C", str(staging), HOME_DISK)
            (staging / HOME_DISK).replace(self.dir / HOME_DISK)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

