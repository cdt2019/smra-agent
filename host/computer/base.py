from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class State(enum.Enum):
    ABSENT = "absent"          # 还没创建
    STOPPED = "stopped"        # 创建过，但没在运行
    RUNNING = "running"
    SUSPENDED = "suspended"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Computer(Protocol):
    """Agent 运行所在的"电脑"。宿主侧只通过这个接口使用它，不关心底下是容器还是 microVM。

    设计文档第 5.4 节。
    """

    #: 在电脑里启动一次运行的命令行
    agent_argv: list[str]
    #: 电脑里的程序访问宿主（模型代理、宿主接口）用的地址
    host_url: str
    #: 主目录在电脑里的路径
    home_path: str
    #: 是否给 Agent 提供命令行工具
    exec_allowed: bool
    #: 电脑里的程序访问出网代理用的主机名
    egress_host: str
    #: 不经出网代理直连的地址（只有宿主接口和模型代理）
    no_proxy: str

    def configure(self, host_port: int, egress_port: int) -> None:
        """宿主侧服务起来后告知端口。"""

    async def state(self) -> State: ...

    async def create(self) -> None: ...

    async def start(self) -> None: ...

    async def ensure_awake(self) -> None:
        """无论当前什么状态，返回时电脑可以执行命令。"""

    async def exec(self, argv: list[str], env: dict[str, str], timeout: float) -> ExecResult: ...

    async def put_file(self, rel_path: str, data: bytes) -> None:
        """把宿主上的数据写进主目录（用户发来的附件）。"""

    async def snapshot_home(self, dest: Path) -> None:
        """把整个主目录打包到宿主上的 dest。电脑碰不到快照的历史。"""

    async def restore_home(self, src: Path) -> None:
        """用快照替换整个主目录。"""

    async def suspend(self) -> None: ...

    async def resume(self) -> None: ...

    async def destroy(self) -> None: ...
