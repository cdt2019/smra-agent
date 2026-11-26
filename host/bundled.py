"""仓库自带的技能（仓库根目录的 skills/）。

第一次运行前放进 Agent 主目录的 skills/<名字>/，之后就是 Agent 的东西：它可以改、可以删，宿主不再动。
宿主看不到主目录，所以装过哪些、装的是哪个版本记在宿主状态表里（host_state 的 bundled_skills）：
- 主目录里已经有同名目录（Agent 或你自己写的）：不动，记作"原来就有"
- 仓库里的技能更新了：不自动覆盖（会冲掉 Agent 的改动），/skills 里提示，/skills reset <名字> 用新版覆盖
- Agent 删掉了：不再装回来；想要回来也是 /skills reset <名字>
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ROOT
from .db import get_state, set_state

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("bundled")

BUNDLED_DIR = ROOT / "skills"
STATE_KEY = "bundled_skills"
# 在电脑里看一个目录在不在：退出码 1 = 在。用电脑自己的 Python，不依赖 sh
_EXISTS = "import os, sys; sys.exit(1 if os.path.exists(sys.argv[1]) else 0)"


def bundled(folder: Path = BUNDLED_DIR) -> dict[str, dict[str, bytes]]:
    """技能名 -> {技能目录里的相对路径: 内容}。只收有 SKILL.md 的目录，跳过缓存文件。"""
    out: dict[str, dict[str, bytes]] = {}
    if not folder.is_dir():
        return out
    for skill in sorted(p for p in folder.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()):
        files = {}
        for f in sorted(skill.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                files[f.relative_to(skill).as_posix()] = f.read_bytes()
        out[skill.name] = files
    return out


def digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(files.items()):
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()[:16]


def description(files: dict[str, bytes]) -> str:
    for line in files.get("SKILL.md", b"").decode("utf-8", "replace").splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip()
    return ""


class BundledSkills:
    def __init__(self, app: App, folder: Path = BUNDLED_DIR):
        self.app = app
        self.folder = folder

    def state(self) -> dict[str, dict]:
        raw = get_state(self.app.db, STATE_KEY)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _save(self, state: dict) -> None:
        set_state(self.app.db, STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True))

    async def _exists(self, rel: str) -> bool:
        computer = self.app.computer
        result = await computer.exec([computer.agent_argv[0], "-c", _EXISTS, f"{computer.home_path}/{rel}"],
                                     {}, 30)
        if result.exit_code not in (0, 1) or result.timed_out:
            raise RuntimeError(f"看不了主目录里有没有 {rel}：{result.stderr.strip()[-300:]}")
        return result.exit_code == 1

    async def _put(self, name: str, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            await self.app.computer.put_file(f"skills/{name}/{rel}", data)

    async def install_missing(self) -> list[str]:
        """电脑已经醒着时调用。装上还没装过的自带技能，返回这次装了哪些。出错只记日志，不影响运行。"""
        if not self.app.cfg.bundled_skills:
            return []
        state = self.state()
        todo = {n: f for n, f in bundled(self.folder).items() if n not in state}
        if not todo:
            return []
        installed = []
        for name, files in todo.items():
            try:
                if await self._exists(f"skills/{name}"):
                    state[name] = {"hash": digest(files), "status": "existed"}
                    log.info("主目录里已经有 skills/%s，自带的同名技能没有放进去", name)
                else:
                    await self._put(name, files)
                    state[name] = {"hash": digest(files), "status": "installed"}
                    installed.append(name)
            except Exception:
                log.exception("放自带技能 %s 失败，下次运行再试", name)
        self._save(state)
        if installed:
            log.info("放进主目录的自带技能：%s", ", ".join(installed))
        return installed

    async def reset(self, name: str) -> str:
        """用仓库里的版本覆盖主目录里的同名技能。只覆盖自带的那几个文件，Agent 另加的文件留着。"""
        files = bundled(self.folder).get(name)
        if files is None:
            return f"没有叫 {name} 的自带技能。/skills 列出它们"
        await self.app.computer.ensure_awake()
        await self._put(name, files)
        state = self.state()
        state[name] = {"hash": digest(files), "status": "installed"}
        self._save(state)
        return f"已用仓库里的版本覆盖 skills/{name}。"

    def listing(self) -> str:
        skills = bundled(self.folder)
        if not skills:
            return "仓库里没有自带技能。"
        if not self.app.cfg.bundled_skills:
            return "自带技能关着（SMRA_BUNDLED_SKILLS=0）。"
        state = self.state()
        lines = ["自带技能（主目录里的技能 Agent 可以改；/skill <名字> 现在做一次）："]
        for name, files in skills.items():
            entry = state.get(name)
            if entry is None:
                where = "还没放进主目录（下次运行时放）"
            elif entry.get("status") == "existed":
                where = "主目录里原来就有同名技能，没动它（/skills reset 用自带的覆盖）"
            elif entry.get("hash") != digest(files):
                where = f"仓库里有新版本（/skills reset {name} 覆盖；Agent 对它的改动会丢）"
            else:
                where = "已放进主目录"
            lines.append(f"- {name}：{description(files)}\n    {where}")
        return "\n".join(lines)
