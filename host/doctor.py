"""启动自检：缺东西时启动就说清楚，不等到第一次运行才失败。

宿主启动时自动跑；有"不行"的项就不启动（退出码 78，systemd 单元里设了不自动重启）。
也可以在终端里单独跑（服务在跑时端口那几项会显示被占用，这是正常的）：
    uv run python -m host.doctor [--channel telegram]
"""
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "好", "注意", "不行"
EXIT_CONFIG = 78        # EX_CONFIG：配置有问题，重启也没用


@dataclass
class Check:
    level: str
    name: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.name}：{self.detail}"


def _docker(*args: str, timeout: float = 20) -> subprocess.CompletedProcess | None:
    if not shutil.which("docker"):
        return None
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _local_chromium() -> Path | None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = Path(p.chromium.executable_path)
        return path if path.exists() else None
    except Exception:
        return None


def run_checks(cfg, channel: str = "console", ports: bool = True) -> list[Check]:
    out: list[Check] = []
    add = lambda level, name, detail: out.append(Check(level, name, detail))

    if cfg.api_key:
        add(OK, "模型密钥", f"{cfg.model_provider} / {cfg.model}")
    else:
        add(FAIL, "模型密钥", f"没配（提供方 {cfg.model_provider}）。在 .env 里填，见 .env.example")

    if channel == "telegram":
        if not cfg.telegram_token:
            add(FAIL, "Telegram", "没有 TELEGRAM_BOT_TOKEN")
        elif not cfg.telegram_chat_id:
            add(WARN, "Telegram", "没设 TELEGRAM_CHAT_ID：启动后给机器人发一条消息，日志里会打印出 chat id")
        else:
            add(OK, "Telegram", "令牌和 chat id 都有")

    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        add(OK, "数据目录", str(cfg.data_dir))
    except OSError as e:
        add(FAIL, "数据目录", f"{cfg.data_dir} 写不进去：{e}")

    needs_docker = cfg.driver == "docker" or cfg.browser == "docker"
    if needs_docker:
        info = _docker("version", "--format", "{{.Server.Version}}")
        if info is None:
            add(FAIL, "Docker", "找不到 docker 命令")
        elif info.returncode != 0:
            add(FAIL, "Docker", "连不上 docker 服务：" + (info.stderr.strip().splitlines() or ["?"])[-1])
        else:
            add(OK, "Docker", f"服务端 {info.stdout.strip()}")
            if cfg.driver == "docker":
                from .computer.docker import IMAGE
                found = _docker("image", "inspect", IMAGE)
                if found is not None and found.returncode == 0:
                    add(OK, "电脑镜像", IMAGE)
                else:
                    add(WARN, "电脑镜像", f"还没有 {IMAGE}，第一次运行时自动构建（要几分钟）")
    if cfg.driver == "firecracker":
        vm = cfg.data_dir / "vm"
        missing = [str(p) for p in (Path(cfg.fc_bin), Path(cfg.fc_kernel or vm / "vmlinux"),
                                    Path(cfg.fc_rootfs or vm / "rootfs.ext4")) if not p.exists()]
        if not Path("/dev/kvm").exists():
            add(FAIL, "microVM", "没有 /dev/kvm：这台机器不能跑 firecracker 驱动")
        elif missing:
            add(FAIL, "microVM", "缺文件（sh cell/vm/build-images.sh 准备）：" + "、".join(missing))
        else:
            add(OK, "microVM", "KVM 和镜像都在")
    if cfg.driver == "local":
        add(WARN, "电脑驱动", "local 不隔离，只供开发" + ("，而且开了命令行（SMRA_LOCAL_EXEC=1）" if cfg.local_exec else ""))

    if cfg.browser == "local" and cfg.browser_channel == "chromium":
        if _local_chromium():
            add(OK, "浏览器", "本机 Chromium")
        else:
            add(WARN, "浏览器", "没装 Chromium：uv run playwright install chromium；不装的话 Agent 用不了浏览器")
    elif cfg.browser == "docker":
        add(OK, "浏览器", "容器里的 Chromium（镜像第一次用时自动构建）")

    if not ports:
        return out
    wanted = [("宿主接口", cfg.bind, cfg.port), ("出网代理", cfg.bind, cfg.egress_port),
              ("Webhook", cfg.webhook_bind, cfg.webhook_port)]
    if cfg.browser != "off":
        wanted.append(("接管页面", cfg.takeover_bind, cfg.takeover_port))
    busy = [f"{name} {host}:{port}" for name, host, port in wanted if port > 0 and not _port_free(host, port)]
    if busy:
        add(FAIL, "端口", "被占用（服务本身在跑时这是正常的）：" + "、".join(busy))
    else:
        add(OK, "端口", "都空着")
    return out


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.level == FAIL]


def main() -> None:
    from .config import Config
    parser = argparse.ArgumentParser(description="宿主自检")
    parser.add_argument("--channel", choices=["console", "telegram"], default="telegram")
    parser.add_argument("--no-ports", action="store_true", help="不查端口（服务正在跑时用）")
    args = parser.parse_args()
    checks = run_checks(Config.from_env(), args.channel, ports=not args.no_ports)
    for c in checks:
        print(c)
    sys.exit(1 if failed(checks) else 0)


if __name__ == "__main__":
    main()
