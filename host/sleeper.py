"""休眠与唤醒（第 5.5 节）。

规则：
  - 最后一次运行（或前置检查、宿主命令）结束后空闲 idle_suspend_s（默认 10 分钟），挂起电脑。
  - Agent 起长任务前可以请求保持清醒（keep_awake），上限 keep_awake_max_s（默认 4 小时），由宿主执行。
  - 有事件时工作进程照常调用 ensure_awake，挂起着就恢复。
  - 由宿主决定，电脑自己不能决定不睡。

挂起这个动作由工作进程在两批事件之间执行（Worker.loop 空闲时调用 maybe_suspend），
所以它永远不会和一次运行、一次前置检查同时发生。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .computer import State
from .db import get_state, set_state

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("sleeper")


STATE_KEY = "keep_awake"


class Sleeper:
    def __init__(self, app: App):
        self.app = app
        self.clock = lambda: datetime.now(UTC)
        self.last_active = self.clock()
        self.awake_until: datetime | None = None
        self.awake_reason = ""
        self.suspends = 0
        self._load()

    def _load(self) -> None:
        """宿主重启前收下的保持清醒请求还没到期，就接着算（长任务不会因为宿主重启被挂起）。"""
        raw = get_state(self.app.db, STATE_KEY)
        if not raw:
            return
        try:
            saved = json.loads(raw)
            until = datetime.fromisoformat(saved["until"])
        except (ValueError, KeyError, TypeError):
            set_state(self.app.db, STATE_KEY, None)
            return
        if until > self.clock():
            self.awake_until, self.awake_reason = until, saved.get("reason", "")
            log.info("接着保持清醒到 %s：%s", until.isoformat(timespec="seconds"), self.awake_reason)
        else:
            set_state(self.app.db, STATE_KEY, None)

    def _save(self) -> None:
        value = None
        if self.awake_until is not None:
            value = json.dumps({"until": self.awake_until.isoformat(), "reason": self.awake_reason},
                               ensure_ascii=False)
        set_state(self.app.db, STATE_KEY, value)

    def touch(self) -> None:
        """电脑刚被用过。"""
        self.last_active = self.clock()

    def keep_awake(self, reason: str, minutes: float) -> tuple[datetime | None, bool]:
        """请求保持清醒到 minutes 分钟之后。返回 (截止时间, 是否被上限截短)。minutes <= 0 表示撤销。"""
        if minutes <= 0:
            self.awake_until, self.awake_reason = None, ""
            self._save()
            return None, False
        cap = self.app.cfg.keep_awake_max_s
        capped = minutes * 60 > cap
        until = self.clock() + timedelta(seconds=min(minutes * 60, cap))
        # 新的请求不能把已有的截止时间往前拉
        if self.awake_until is None or until > self.awake_until:
            self.awake_until, self.awake_reason = until, reason
            self._save()
        log.info("保持清醒到 %s：%s", self.awake_until.isoformat(timespec="seconds"), reason)
        return self.awake_until, capped

    def due(self) -> bool:
        idle = self.app.cfg.idle_suspend_s
        if idle < 0:
            return False
        now = self.clock()
        if self.awake_until is not None:
            if now < self.awake_until:
                return False
            log.info("保持清醒的请求到期了（%s）", self.awake_reason)
            self.awake_until, self.awake_reason = None, ""
            self._save()
            # 到期就照常挂起，不再另等一个空闲期
            return True
        return (now - self.last_active).total_seconds() >= idle

    async def maybe_suspend(self) -> bool:
        if not self.due():
            return False
        computer = self.app.computer
        if await computer.state() is not State.RUNNING:
            return False
        await computer.suspend()
        self.suspends += 1
        log.info("电脑空闲，已挂起")
        return True
