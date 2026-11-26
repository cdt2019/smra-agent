from __future__ import annotations

import asyncio
import shlex
import sys
import uuid
from pathlib import Path


class ConsoleChannel:
    """终端渠道：一行输入就是一条消息。用来在没有 Telegram 的情况下试用。

    发文件：/file <本机路径> [附带的话]
    """

    async def listen(self, on_text) -> None:
        print("输入消息后回车。/file <路径> 发文件，/help 看命令，Ctrl+C 退出。", flush=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:        # 输入流结束
                return
            source_id = f"console:{uuid.uuid4().hex}"
            if line.startswith("/file "):
                try:
                    parts = shlex.split(line[6:].strip(), posix=False)
                    path = Path(parts[0].strip('"'))
                    data = path.read_bytes()
                except (IndexError, OSError, ValueError) as e:
                    print(f"读不了这个文件：{e}", flush=True)
                    continue
                await on_text(source_id, " ".join(parts[1:]), files=[(path.name, data)])
                continue
            await on_text(source_id, line)

    async def send(self, text: str) -> None:
        print(f"\n[agent] {text}\n", flush=True)

    async def send_review(self, text: str, kind: str, proposal_id: str) -> None:
        print(f"\n[审阅] {text}\n", flush=True)

    async def send_notice(self, text: str, notice_id: str) -> None:
        print(f"\n[通知] {text}\n（/feedback {notice_id} useful 或 unnecessary）\n", flush=True)

    async def send_approval(self, text: str, approval_id: str, scopes: list[str]) -> None:
        print(f"\n[审批] {text}\n", flush=True)

    async def send_file(self, path: str, caption: str) -> None:
        note = f"（{caption}）" if caption else ""
        print(f"\n[agent] 发来文件：{path}{note}\n", flush=True)
