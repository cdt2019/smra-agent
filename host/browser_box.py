"""把 Agent 的浏览器放进容器（SMRA_BROWSER=docker）。

浏览器要打开任意网页，网页里的代码就在浏览器进程里跑。浏览器放在宿主上时，它一旦被攻破就站在宿主上；
放进容器后，它碰得到的只有容器自己：

    浏览器容器 --(只接 internal 网络)--> 这个网络的网关地址上的转发 --> 宿主上的出网代理（127.0.0.1）
    宿主 --(直接连容器在 internal 网络上的地址)--> 容器里的 Playwright 服务

- internal 网络没有通往外部的路由，浏览器上网只能走出网代理，和放在宿主上时一样；
- 宿主只在这个网络的网关地址上开一个转发，只转给出网代理，别的容器（包括电脑容器）不在这个网络上；
- Playwright 服务的地址里带一段每次随机的口令，只有宿主知道；
- 容器根文件系统只读、非 root、去掉所有特权、限内存和 CPU；容器里不存登录状态，
  登录状态（Cookie 和本地存储）存在宿主的 data/browser/state.json，每次连上时交给浏览器。
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import secrets
from pathlib import Path

from .computer.forward import forward_to

log = logging.getLogger("browser.box")

ROOT = Path(__file__).resolve().parent.parent
SERVER_PORT = 3000


def playwright_version() -> str:
    return importlib.metadata.version("playwright")


class BrowserBox:
    def __init__(self, egress_port: int, prefix: str | None = None) -> None:
        prefix = prefix or os.environ.get("SMRA_DOCKER_PREFIX", "smra")   # 测试换个前缀，不碰正在运行的服务
        self.egress_port = egress_port
        self.image = f"smra-browser:{playwright_version()}"
        self.network = f"{prefix}-browsernet"
        self.container = f"{prefix}-browser"
        self._forwarder: asyncio.base_events.Server | None = None
        self._gateway = ""

    async def _docker(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        proc = await asyncio.create_subprocess_exec("docker", *args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"docker {' '.join(args[:2])} 超时") from None
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args[:2])} 失败：{err.decode('utf-8', 'replace').strip()[-500:]}")
        return out.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""

    async def _ok(self, *args: str) -> bool:
        proc = await asyncio.create_subprocess_exec("docker", *args, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    async def start(self) -> tuple[str, str]:
        """起一个新的浏览器容器。返回 (Playwright 服务地址, 浏览器该用的出网代理地址)。"""
        if not await self._ok("image", "inspect", self.image):
            log.info("构建浏览器镜像 %s（第一次要几分钟）", self.image)
            await self._docker("build", "-f", str(ROOT / "browser" / "Dockerfile"),
                               "--build-arg", f"PLAYWRIGHT_VERSION={playwright_version()}",
                               "-t", self.image, str(ROOT / "browser"), timeout=1800)
        if not await self._ok("network", "inspect", self.network):
            await self._docker("network", "create", "--internal", self.network)
        self._gateway = await self._docker("network", "inspect", self.network,
                                           "-f", "{{(index .IPAM.Config 0).Gateway}}")
        if self._forwarder is None:
            # 只开在这个网络的网关地址上：够得着它的只有这个网络里的浏览器容器
            self._forwarder = await asyncio.start_server(forward_to("127.0.0.1", self.egress_port),
                                                         self._gateway, self.egress_port)
        token = secrets.token_urlsafe(24)
        await self._docker("rm", "-f", self.container, check=False)
        await self._docker(
            "run", "-d", "--name", self.container,
            "--network", self.network,                    # 只有 internal 网络
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,size=1g,mode=1777",
            "--pids-limit", "512",
            # 和别的服务共用机器时调低，见 .env.example
            "--memory", os.environ.get("SMRA_BROWSER_MEMORY", "1536m"),
            "--cpus", os.environ.get("SMRA_BROWSER_CPUS", "1.5"),
            "--cpu-shares", os.environ.get("SMRA_BROWSER_CPU_SHARES", "512"),
            self.image,
            "playwright", "run-server", "--host", "0.0.0.0", "--port", str(SERVER_PORT),
            "--path", f"/{token}", "--max-clients", "1",
            "--unsafe",       # 允许宿主传 Chromium 启动参数；只有拿着口令的宿主连得上
        )
        ip = await self._docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                self.container)
        await self._wait_port(ip)
        log.info("浏览器容器已启动（%s，%s）", self.container, ip)
        return f"ws://{ip}:{SERVER_PORT}/{token}", f"http://{self._gateway}:{self.egress_port}"

    async def _wait_port(self, ip: str, timeout: float = 60) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(ip, SERVER_PORT), 2)
                writer.close()
                return
            except (TimeoutError, OSError):
                if asyncio.get_running_loop().time() > deadline:
                    logs = await self._docker("logs", "--tail", "20", self.container, check=False)
                    raise RuntimeError(f"浏览器容器没起来：{logs[-500:]}") from None
                await asyncio.sleep(0.3)

    async def stop(self) -> None:
        await self._docker("rm", "-f", self.container, check=False)
        if self._forwarder is not None:
            self._forwarder.close()
            self._forwarder = None
