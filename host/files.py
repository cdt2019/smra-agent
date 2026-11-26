from __future__ import annotations

import re
from datetime import datetime

_UNSAFE = re.compile(r"[^\w.\-一-鿿]+")


def safe_name(name: str) -> str:
    """把用户或 Agent 给的文件名变成只含安全字符的单个文件名。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).strip("._")[:100]
    return base or "file"


def inbox_target(name: str, when: datetime | None = None) -> str:
    """用户发来的文件在主目录里的位置：inbox/<日期>/<文件名>。"""
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    return f"inbox/{day}/{safe_name(name)}"


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"
