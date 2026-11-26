from __future__ import annotations

from .base import Computer, ExecResult, State


def make(cfg) -> Computer:
    if cfg.driver == "docker":
        from .docker import DockerComputer
        return DockerComputer()
    if cfg.driver == "firecracker":
        from .firecracker import FirecrackerComputer
        return FirecrackerComputer(cfg)
    if cfg.driver == "local":
        from .local import LocalComputer
        return LocalComputer(cfg.data_dir / "home", allow_exec=cfg.local_exec)
    raise ValueError(f"未知的电脑驱动：{cfg.driver}")


__all__ = ["Computer", "ExecResult", "State", "make"]
