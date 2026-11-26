from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import ExecResult, State
from .forward import forward_to

log = logging.getLogger("computer.docker")

ROOT = Path(__file__).resolve().parent.parent.parent

IMAGE = "smra-cell:dev"
_PREFIX = os.environ.get("SMRA_DOCKER_PREFIX", "smra")     # 测试换个前缀，不碰正在运行的服务
NETWORK = f"{_PREFIX}-cellnet"     # internal 网络：没有通往外部的路由
CELL = f"{_PREFIX}-cell"
RELAY = f"{_PREFIX}-relay"
VOLUME = f"{_PREFIX}-home"


class DockerComputer:
    """容器实现。设计文档第 5 节的过渡实现，第 9 步换成 microVM。

    网络结构：
        电脑容器 --(只连 internal 网络)--> 中继容器 --(默认网桥)--> 网桥地址上的转发 --> 宿主进程（127.0.0.1）
    电脑容器能到达的只有中继，中继只把连接转给宿主上的两个端口。
    宿主进程只听 127.0.0.1；驱动在默认网桥的网关地址（通常是 172.17.0.1）上开两个转发，中继连的是它们。
    """

    def __init__(self) -> None:
        self.agent_argv = ["python", "/opt/agent/agent_run.py"]
        self.host_url = ""
        self.home_path = "/home/agent"
        self.exec_allowed = True        # 命令在容器里执行
        self.egress_host = RELAY        # 电脑唯一能到达的就是中继，中继只转发这两个端口
        self.no_proxy = RELAY
        self._port = 0
        self._egress_port = 0
        self._forwarders: list[asyncio.base_events.Server] = []

    def configure(self, host_port: int, egress_port: int) -> None:
        self._port = host_port
        self._egress_port = egress_port
        self.host_url = f"http://{RELAY}:{host_port}"

    async def _docker(self, *args: str, check: bool = True, timeout: float | None = 300,
                      input: bytes | None = None) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), timeout)
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        result = ExecResult(proc.returncode if proc.returncode is not None else -1,
                            out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out)
        if check and result.exit_code != 0:
            raise RuntimeError(f"docker {' '.join(args[:3])} 失败：{result.stderr.strip()[-500:]}")
        return result

    async def _status(self, name: str) -> str | None:
        r = await self._docker("inspect", "-f", "{{.State.Status}}", name, check=False)
        return r.stdout.strip() if r.exit_code == 0 else None

    async def state(self) -> State:
        status = await self._status(CELL)
        if status is None:
            return State.ABSENT
        if status == "running":
            return State.RUNNING
        if status == "paused":
            return State.SUSPENDED
        return State.STOPPED

    async def create(self) -> None:
        if (await self._docker("image", "inspect", IMAGE, check=False)).exit_code != 0:
            log.info("构建镜像 %s", IMAGE)
            await self._docker("build", "-f", str(ROOT / "cell" / "Dockerfile"), "-t", IMAGE, str(ROOT),
                               timeout=1200)
        if (await self._docker("network", "inspect", NETWORK, check=False)).exit_code != 0:
            await self._docker("network", "create", "--internal", NETWORK)

        # 中继：每次重建，保证转发的端口和宿主当前监听的一致
        await self._docker("rm", "-f", RELAY, check=False)
        await self._docker(
            "run", "-d", "--name", RELAY,
            "--add-host", "host.docker.internal:host-gateway",
            "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-e", f"PORTS={self._port},{self._egress_port}",
            "-e", "TARGET_HOST=host.docker.internal",
            IMAGE, "python", "/opt/relay/relay.py",
        )
        await self._docker("network", "connect", NETWORK, RELAY)

        if await self._status(CELL) is None:
            await self._docker(
                "run", "-d", "--name", CELL,
                "--network", NETWORK,                      # 只有 internal 网络
                # 1 号进程用 docker 自带的 tini：回收后台命令留下的孤儿进程，不然僵尸条目越积越多、
                # 占满 --pids-limit。老容器没有这一项，删掉容器（docker rm -f smra-cell）后宿主会按新参数重建，
                # 主目录在卷里不受影响
                "--init",
                "--user", "1000:1000",                     # 非 root
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--read-only",                             # 根文件系统只读，运行程序改不了
                "--tmpfs", "/tmp:rw,size=256m",
                "--pids-limit", "256",
                # 和别的服务共用机器时调低，见 .env.example
                "--memory", os.environ.get("SMRA_CELL_MEMORY", "2g"),
                "--cpus", os.environ.get("SMRA_CELL_CPUS", "2"),
                "--cpu-shares", os.environ.get("SMRA_CELL_CPU_SHARES", "1024"),
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{VOLUME}:/home/agent",             # 主目录：唯一持久可写的地方
                IMAGE, "sleep", "infinity",
            )

    async def start(self) -> None:
        await self._docker("start", RELAY, CELL)

    async def _ensure_forwarders(self) -> None:
        """中继从默认网桥连宿主，到的是网关地址；宿主进程只听 127.0.0.1，所以在网关地址上开转发。"""
        if self._forwarders:
            return
        r = await self._docker("network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}")
        gateway = r.stdout.strip()
        for port in (self._port, self._egress_port):
            self._forwarders.append(await asyncio.start_server(forward_to("127.0.0.1", port), gateway, port))
        log.info("在网桥地址 %s 上转发端口 %d、%d", gateway, self._port, self._egress_port)

    async def ensure_awake(self) -> None:
        await self._ensure_forwarders()
        current = await self.state()
        if current is State.ABSENT:
            await self.create()
        elif current is State.STOPPED:
            await self.create()          # 重建中继
            await self._docker("start", CELL)
        elif current is State.SUSPENDED:
            await self.resume()
        elif await self._status(RELAY) != "running":
            await self.create()

    async def exec(self, argv: list[str], env: dict[str, str], timeout: float) -> ExecResult:
        args = ["exec"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        # 时限在容器里用 timeout 执行，这样超时后容器里的进程确实被杀掉
        args += [CELL, "timeout", "-k", "5", str(int(timeout)), *argv]
        result = await self._docker(*args, check=False, timeout=timeout + 30)
        if result.exit_code == 124:
            result.timed_out = True
        return result

    async def put_file(self, rel_path: str, data: bytes) -> None:
        target = f"{self.home_path}/{rel_path.lstrip('/')}"
        if "/../" in f"/{rel_path}/":
            raise ValueError(f"路径不在主目录内：{rel_path}")
        # 以 agent 用户在电脑里写入，文件归属和 Agent 自己写的一样
        await self._docker("exec", "-i", "-u", "1000:1000", CELL, "sh", "-c",
                           'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", target, input=data)

    async def snapshot_home(self, dest: Path) -> None:
        # 用一个临时容器以只读方式挂载主目录卷来打包。快照存在宿主上，电脑碰不到
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._docker(
            "run", "--rm", "--network", "none", "--user", "0:0",
            "-v", f"{VOLUME}:/src:ro", "-v", f"{dest.parent.resolve()}:/out",
            IMAGE, "tar", "-czf", f"/out/{dest.name}", "-C", "/src", ".",
            timeout=1200,
        )

    async def restore_home(self, src: Path) -> None:
        state = await self.state()
        if state is State.SUSPENDED:
            await self.resume()
        await self._docker(
            "run", "--rm", "--network", "none", "--user", "0:0",
            "-v", f"{VOLUME}:/dst", "-v", f"{src.parent.resolve()}:/in:ro",
            IMAGE, "sh", "-c", f'find /dst -mindepth 1 -delete && tar -xzpf "/in/{src.name}" -C /dst',
            timeout=1200,
        )

    async def suspend(self) -> None:
        # 冻结全部进程：CPU 归零，内存仍占着。真正连内存一起释放要等 microVM 实现
        await self._docker("pause", CELL)

    async def resume(self) -> None:
        await self._docker("unpause", CELL)

    async def destroy(self) -> None:
        await self._docker("rm", "-f", CELL, RELAY, check=False)
        for server in self._forwarders:
            server.close()
        self._forwarders = []
