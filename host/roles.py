"""多角色（第 17 节）。

一个角色 = 一份角色说明 + 一套自己的记忆。角色不是进程，也不是安全边界：
它只是工作进程启动一次运行时带上的参数，决定系统提示词里的角色说明和记忆目录。
收件箱、电脑、工具、技能、账号连接和授权，所有角色共用。

角色说明放在 data/core/roles/<名字>.md，和 AGENT.md 一样只有你能改：

    ---
    title: 研究员
    description: 查资料、读网页，把结论整理成笔记
    ---
    （正文：这个角色怎么做事）

默认角色（协调者）接所有没点名的消息。没有它的文件也行，用内置的说明。
除默认角色外一个角色文件都没有时，就是单角色模式，行为和第 7 步完全一样。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROLE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
DEFAULT_TITLE = "协调者"
DEFAULT_DESCRIPTION = "接用户没点名的消息；自己能做的直接做，更适合别的角色的就交给它"


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    description: str
    body: str = ""


def parse_role(path: Path) -> Role | None:
    name = path.stem
    if not ROLE_NAME.fullmatch(name):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    title, desc, body = name, "", text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
            key, _, value = line.partition(":")
            if key.strip() == "title" and value.strip():
                title = value.strip()[:20]
            elif key.strip() == "description":
                desc = " ".join(value.split())[:200]
    return Role(name, title, desc, body.strip())


class Roles:
    """每次用到时从磁盘读：你改了角色文件，下一个新会话就生效，不用重启。"""

    def __init__(self, folder: Path, default: str, max_hops: int):
        self.folder = folder
        self.default_name = default
        self.max_hops = max_hops

    def all(self) -> list[Role]:
        found = {}
        if self.folder.is_dir():
            for path in sorted(self.folder.glob("*.md")):
                role = parse_role(path)
                if role:
                    found[role.name] = role
        default = found.pop(self.default_name, None) or Role(self.default_name, DEFAULT_TITLE, DEFAULT_DESCRIPTION)
        return [default, *found.values()]

    @property
    def default(self) -> Role:
        return self.all()[0]

    @property
    def multi(self) -> bool:
        return len(self.all()) > 1

    def get(self, name_or_title: str | None) -> Role | None:
        if not name_or_title:
            return None
        key = name_or_title.strip().lstrip("@")
        return next((r for r in self.all() if key in (r.name, r.title)), None)

    def title_of(self, name: str | None) -> str:
        role = self.get(name)
        return role.title if role else (name or "")

    def addressed(self, text: str) -> Role:
        """消息开头的 @角色名 或 @显示名 点名一个角色；没点名（或点了不存在的）交给默认角色。"""
        m = re.match(r"\s*@(\S+?)(?:[\s:：,，]|$)", text or "")
        role = self.get(m.group(1)) if m else None
        return role or self.default

    def memory_dir(self, name: str | None) -> str:
        """默认角色沿用 memory/，其他角色各用 roles/<名字>/memory/（第 9 节的主目录布局）。"""
        if not name or name == self.default_name:
            return "memory"
        return f"roles/{name}/memory"

    def section(self, role: Role) -> str:
        """拼进系统提示词的角色说明。会话开始时生成一次，存在会话里，会话内不变。"""
        if not self.multi:
            return ""
        lines = ["## 角色", f"你是「{role.title}」（{role.name}）。{role.description}".rstrip()]
        if role.body:
            lines.append(role.body)
        mem = self.memory_dir(role.name)
        if mem != "memory":
            lines.append(f"- 你的记忆在 {mem}/ 下。上文说的 memory/instructions.md、memory/index.md、memory/notes/、"
                         f"memory/progress/，对你来说都在 {mem}/ 里。")
        lines.append("- 别的角色的记忆目录不要改。skills/、tools/、work/、inbox/、已连接的账号和用户给的授权是所有角色共用的。")
        lines.append("- 其他角色：")
        lines += [f"  - {r.name}（{r.title}）：{r.description or '（没有说明）'}" for r in self.all() if r.name != role.name]
        lines.append("- 更适合别的角色做的事，用 handoff 交给它。对方在自己的新会话里处理，看不到这段对话，只看到你写的 task，"
                     "所以 task 要自包含：目标、已知的信息、材料文件的路径（放在 work/ 下）、做完之后交给谁或者直接回复用户。")
        lines.append("- 交接之后，你这边简短告诉用户交给了谁就行，不用等结果；用户在等的话，对方会直接回复用户。")
        lines.append(f"- 一条用户消息引发的交接最多 {self.max_hops} 跳，一个任务的总花费也有上限。不要来回推。")
        return "\n".join(lines)

    def listing(self) -> str:
        roles = self.all()
        if len(roles) == 1:
            return ("现在只有一个角色。要加角色，在 data/core/roles/ 下放 <名字>.md（名字用小写字母），"
                    "开头写 title 和 description。")
        lines = ["角色（消息开头写 @名字 点名；不点名交给默认角色）："]
        for r in roles:
            mark = "（默认）" if r.name == self.default_name else ""
            lines.append(f"- @{r.name} {r.title}{mark}：{r.description or '（没有说明）'}")
        return "\n".join(lines)
