from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tarfile
from pathlib import Path

from .base import ExecResult, State

log = logging.getLogger("computer.local")

AGENT_PROGRAM = Path(__file__).resolve().parent.parent.parent / "cell" / "agent_run.py"

# 这些前缀的环境变量不传给子进程，避免把宿主的密钥带进去
_STRIP_PREFIXES = ("ANTHROPIC_", "TELEGRAM_", "SMRA_")


class LocalComputer:
    """把运行程序直接当作本机子进程来跑，主目录是宿主上的一个普通目录。

    **没有任何隔离。** 它只用来在没有 Docker 的机器上开发和跑测试。
    设计文档里的安全性质在这个驱动下都不成立。出于同样的原因，命令行工具默认不提供给
    Agent，要用得显式设置 SMRA_LOCAL_EXEC=1，意味着模型生成的命令会直接在这台机器上执行。
    """

    def __init__(self, home: Path, allow_exec: bool = False) -> None:
        self.home = Path(home)
        self.agent_argv = [sys.executable, str(AGENT_PROGRAM)]
        self.host_url = ""
        self.home_path = str(self.home.resolve())
        self.exec_allowed = allow_exec
        # 没有隔离：电脑里的程序愿意的话完全可以不走代理。这里只是让守规矩的程序走代理
        self.egress_host = "127.0.0.1"
        self.no_proxy = "127.0.0.1,localhost"
        self._state = State.ABSENT

    def configure(self, host_port: int, egress_port: int) -> None:
        self.host_url = f"http://127.0.0.1:{host_port}"

    async def state(self) -> State:
        return self._state

    async def create(self) -> None:
        log.warning("正在使用 local 驱动：运行程序不受隔离，仅供开发")
        if self.exec_allowed:
            log.warning("SMRA_LOCAL_EXEC=1：模型生成的命令会直接在这台机器上执行")
        self.home.mkdir(parents=True, exist_ok=True)
        self._state = State.STOPPED

    async def start(self) -> None:
        self._state = State.RUNNING

    async def ensure_awake(self) -> None:
        if self._state is State.ABSENT:
            await self.create()
        self._state = State.RUNNING

    async def exec(self, argv: list[str], env: dict[str, str], timeout: float) -> ExecResult:
        full_env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_STRIP_PREFIXES)}
        full_env.update(env)
        full_env["NO_PROXY"] = "127.0.0.1,localhost"
        full_env["PYTHONIOENCODING"] = "utf-8"
        proc = await asyncio.create_subprocess_exec(
            *argv, env=full_env, cwd=self.home,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=timed_out,
        )

    async def put_file(self, rel_path: str, data: bytes) -> None:
        target = (self.home / rel_path).resolve()
        if not target.is_relative_to(self.home.resolve()):
            raise ValueError(f"路径不在主目录内：{rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def snapshot_home(self, dest: Path) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def pack():
            tmp = dest.with_suffix(dest.suffix + ".partial")
            with tarfile.open(tmp, "w:gz") as tar:
                for child in sorted(self.home.iterdir()):
                    tar.add(child, arcname=child.name)
            tmp.replace(dest)

        await asyncio.to_thread(pack)

    async def restore_home(self, src: Path) -> None:
        def unpack():
            self.home.mkdir(parents=True, exist_ok=True)
            for child in self.home.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            with tarfile.open(src, "r:gz") as tar:
                tar.extractall(self.home, filter="data")

        await asyncio.to_thread(unpack)

    async def suspend(self) -> None:
        self._state = State.SUSPENDED

    async def resume(self) -> None:
        self._state = State.RUNNING

    async def destroy(self) -> None:
        self._state = State.ABSENT
