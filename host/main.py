"""入口：uv run python -m host.main --channel console --driver local"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from . import doctor
from .app import App
from .backup import HostLock, HostRunning
from .computer import make as make_computer
from .config import Config
from .creds import MasterKeyError
from .db import SchemaTooNew


def make_channel(name: str, cfg: Config):
    if name == "console":
        from .channels.console import ConsoleChannel
        return ConsoleChannel()
    if name == "telegram":
        from .channels.telegram import TelegramChannel
        return TelegramChannel(cfg.telegram_token, cfg.telegram_chat_id, cfg.data_dir)
    raise ValueError(f"未知的渠道：{name}")


async def amain(args: argparse.Namespace) -> None:
    cfg = Config.from_env()
    if args.driver:
        cfg.driver = args.driver
    checks = await asyncio.to_thread(doctor.run_checks, cfg, args.channel)
    for c in checks:
        if c.level != doctor.OK:
            (logging.error if c.level == doctor.FAIL else logging.warning)("自检 %s", c)
    if bad := doctor.failed(checks):
        logging.error("自检有 %d 项不行，不启动。改好后重启；单独跑自检：uv run python -m host.doctor", len(bad))
        raise SystemExit(doctor.EXIT_CONFIG)
    # 运行期间一直拿着数据目录的锁：恢复备份、第二个宿主进程看到它就不动这个目录
    lock = HostLock(cfg.data_dir)
    try:
        lock.acquire()
    except HostRunning as e:
        logging.error("%s", e)
        raise SystemExit(doctor.EXIT_CONFIG) from None
    try:
        await _serve(cfg, args)
    finally:
        lock.release()


async def _serve(cfg: Config, args: argparse.Namespace) -> None:
    try:
        app = App(cfg, make_channel(args.channel, cfg), make_computer(cfg))
    except (MasterKeyError, SchemaTooNew) as e:
        logging.error("%s", e)
        raise SystemExit(doctor.EXIT_CONFIG) from None
    logging.info("模型：%s / %s（%s 协议，%s）", cfg.model_provider, cfg.model, cfg.model_protocol, cfg.model_upstream)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:        # systemctl stop/restart 发 SIGTERM：好好收尾（浏览器录像在关浏览器时才写完）
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, RuntimeError):     # Windows 上没有
            pass
    await app.start()
    try:
        await stopping.wait()
        logging.info("收到停止信号，正在收尾")
    finally:
        await app.stop(cfg.stop_grace_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal agent 宿主进程")
    parser.add_argument("--channel", choices=["console", "telegram"], default="console")
    parser.add_argument("--driver", choices=["docker", "firecracker", "local"], default=None,
                        help="电脑驱动；默认取 SMRA_DRIVER。local 不隔离，仅供开发")
    args = parser.parse_args()
    if os.name == "posix":
        # 数据目录里有对话、模型请求的明文和加密的凭证：新建的文件和目录只给运行它的用户看
        os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
