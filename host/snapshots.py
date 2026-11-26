from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .computer import Computer

log = logging.getLogger("snapshots")


class Snapshots:
    """主目录快照。存在宿主上，电脑碰不到历史。

    自动快照在运行结束后做，最多每 interval_s 一次；没有运行就没有新快照。
    """

    def __init__(self, directory: Path, computer: Computer, keep: int = 48, interval_s: int = 3600):
        self.dir = Path(directory)
        self.computer = computer
        self.keep = keep
        self.interval_s = interval_s

    def list(self) -> list[str]:
        """最新的在前。"""
        if not self.dir.exists():
            return []
        files = [p for p in self.dir.glob("home-*.tar.gz")]
        return [p.name for p in sorted(files, key=lambda p: p.name, reverse=True)]

    def _new_path(self, label: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{label}" if label else ""
        path = self.dir / f"home-{stamp}{suffix}.tar.gz"
        n = 2
        while path.exists():
            path = self.dir / f"home-{stamp}{suffix}-{n}.tar.gz"
            n += 1
        return path

    async def take(self, label: str = "") -> str:
        path = self._new_path(label)
        await self.computer.snapshot_home(path)
        self._prune()
        log.info("主目录快照：%s", path.name)
        return path.name

    async def maybe_take(self) -> str | None:
        names = self.list()
        if names:
            age = time.time() - (self.dir / names[0]).stat().st_mtime
            if age < self.interval_s:
                return None
        return await self.take("auto")

    def resolve(self, ref: str) -> str | None:
        """接受快照名，或 /snapshots 列表里的序号（1 是最新的）。"""
        names = self.list()
        ref = ref.strip()
        if ref.isdigit() and 1 <= int(ref) <= len(names):
            return names[int(ref) - 1]
        return ref if ref in names else None

    async def restore(self, name: str) -> str:
        """恢复到某个快照。先给当前状态做一个快照，所以恢复本身也可以撤销。返回那个快照的名字。"""
        safety = await self.take("before-restore")
        await self.computer.restore_home(self.dir / name)
        log.info("主目录已恢复到 %s（恢复前的状态存为 %s）", name, safety)
        return safety

    def _prune(self) -> None:
        for name in self.list()[self.keep:]:
            try:
                (self.dir / name).unlink()
            except OSError:
                pass
