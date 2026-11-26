from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

log = logging.getLogger("telegram")

API = "https://api.telegram.org"
MAX_LEN = 4096      # Telegram 单条消息的长度上限
MAX_CAPTION = 1024  # 文件说明的长度上限
MAX_DOWNLOAD = 20 * 1024 * 1024     # 机器人用 getFile 能下载的上限（官方 Bot API 服务器）
MAX_UPLOAD = 50 * 1024 * 1024       # 机器人能发出的文件上限


class TelegramChannel:
    """Telegram 渠道，长轮询方式，不需要公网地址。

    只接受配置的那一个 chat id 的消息，也只往那里发。
    """

    def __init__(self, token: str, chat_id: str, data_dir: Path):
        if not token:
            raise ValueError("缺少 TELEGRAM_BOT_TOKEN")
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self._offset_file = data_dir / "telegram_offset.txt"
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _call(self, method: str, form: aiohttp.FormData | None = None, **params) -> dict:
        kwargs = {"data": form} if form is not None else {"json": params}
        try:
            async with self._http().post(f"{API}/bot{self.token}/{method}",
                                         timeout=aiohttp.ClientTimeout(total=120), **kwargs) as response:
                data = await response.json()
        except aiohttp.ClientError as e:
            # aiohttp 的错误信息带完整地址，而地址里有令牌：不能原样进日志
            raise RuntimeError(f"Telegram {method} 网络错误：{type(e).__name__}") from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{data.get('description')}")
        return data

    async def _download(self, file_id: str) -> bytes:
        info = await self._call("getFile", file_id=file_id)
        file_path = info["result"]["file_path"]
        try:
            async with self._http().get(f"{API}/file/bot{self.token}/{file_path}",
                                        timeout=aiohttp.ClientTimeout(total=300)) as response:
                response.raise_for_status()
                return await response.read()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Telegram 下载文件失败：{type(e).__name__}") from None

    @staticmethod
    def _attachments(message: dict) -> list[tuple[str, dict]]:
        """消息里的附件：(文件名, Telegram 的文件对象)。文件名是对方给的，存盘前宿主还会再清理一遍。"""
        out = []
        document = message.get("document")
        if document:
            out.append((document.get("file_name") or f"file_{document['file_unique_id']}", document))
        photos = message.get("photo")
        if photos:
            largest = photos[-1]            # 同一张图的多个尺寸，最后一个最大
            out.append((f"photo_{largest['file_unique_id']}.jpg", largest))
        voice = message.get("voice")            # 按住说话录的语音，OGG/Opus
        if voice:
            out.append((f"voice_{voice['file_unique_id']}.ogg", voice))
        audio = message.get("audio")            # 当作音乐发来的音频文件
        if audio:
            out.append((audio.get("file_name") or f"audio_{audio['file_unique_id']}.mp3", audio))
        for kind in ("video", "video_note", "animation"):     # 视频、圆形视频消息、GIF
            video = message.get(kind)
            if video:
                out.append((video.get("file_name") or f"{kind}_{video['file_unique_id']}.mp4", video))
        return out

    async def _files_of(self, message: dict) -> tuple[list[tuple[str, bytes]], list[str]]:
        """下载附件。返回 (收到的文件, 没收到的说明)：一个附件失败不影响别的。"""
        files, problems = [], []
        for name, obj in self._attachments(message):
            size = obj.get("file_size") or 0
            if size > MAX_DOWNLOAD:
                problems.append(f"{name}（{size // (1024 * 1024)} MB）超过 Telegram 机器人能下载的 20 MB，没收到")
                continue
            try:
                files.append((name, await self._download(obj["file_id"])))
            except Exception as e:
                log.warning("下载附件失败：%s", e)
                problems.append(f"{name} 下载失败")
        return files, problems

    def _load_offset(self) -> int:
        try:
            return int(self._offset_file.read_text().strip())
        except (OSError, ValueError):
            return 0

    async def listen(self, on_text) -> None:
        offset = self._load_offset()
        while True:
            try:
                data = await self._call("getUpdates", offset=offset, timeout=30,
                                        allowed_updates=["message", "callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("拉取消息失败：%s", e)
                await asyncio.sleep(5)
                continue
            for update in data["result"]:
                offset = update["update_id"] + 1
                callback = update.get("callback_query")
                if callback:
                    await self._on_button(callback, update["update_id"], on_text)
                    self._save_offset(offset)
                    continue
                message = update.get("message") or {}
                chat = str((message.get("chat") or {}).get("id", ""))
                text = message.get("text") or message.get("caption") or ""
                if not text and not self._attachments(message):
                    continue        # 贴纸、位置之类：没有能交给 Agent 的内容
                if not self.chat_id:
                    log.warning("收到来自 chat id %s 的消息。把它填进 TELEGRAM_CHAT_ID 后重启。", chat)
                elif chat != self.chat_id:
                    log.warning("忽略来自未授权 chat id %s 的消息", chat)
                else:
                    files, problems = await self._files_of(message)
                    if problems:        # 让 Agent 知道少了什么，它会照实告诉你
                        text = (text + "\n" + "\n".join(f"（附件 {p}）" for p in problems)).strip()
                    # update_id 是来源标识：同一条更新被重复投递时，收件箱只收一次
                    await on_text(f"tg:{update['update_id']}", text, files=files or None)
                # 先写入收件箱，再推进偏移量
                self._save_offset(offset)

    def _save_offset(self, offset: int) -> None:
        self._offset_file.parent.mkdir(parents=True, exist_ok=True)
        self._offset_file.write_text(str(offset))

    async def _on_button(self, callback: dict, update_id: int, on_text) -> None:
        """审批按钮。只认配置的那个 chat 的点击；按钮数据形如 ap:<审批号>:<范围> 或 dn:<审批号>。"""
        chat = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback["id"])
        except Exception as e:
            log.warning("应答按钮失败：%s", e)
        if not self.chat_id or chat != self.chat_id:
            log.warning("忽略来自未授权 chat id %s 的按钮", chat)
            return
        kind, _, rest = (callback.get("data") or "").partition(":")
        approval_id, _, scope = rest.partition(":")
        if kind == "ap" and approval_id:
            await on_text(f"tg:{update_id}", f"/approve {approval_id} {scope or 'once'}")
        elif kind == "dn" and approval_id:
            await on_text(f"tg:{update_id}", f"/deny {approval_id}")
        elif kind in ("pa", "pr", "wa", "wr") and approval_id:    # 审阅按钮：pa/pr = 档案，wa/wr = Webhook
            target = "/profile" if kind[0] == "p" else "/webhook"
            await on_text(f"tg:{update_id}", f"{target} {'approve' if kind[1] == 'a' else 'reject'} {approval_id}")
        elif kind == "fb" and approval_id:      # 通知的反馈按钮：fb:<通知号>:u|n
            value = "useful" if scope == "u" else "unnecessary"
            await on_text(f"tg:{update_id}", f"/feedback {approval_id} {value}")

    async def send_review(self, text: str, kind: str, proposal_id: str) -> None:
        if not self.chat_id:
            raise RuntimeError("没有配置 TELEGRAM_CHAT_ID，不知道发给谁")
        p = "p" if kind == "profile" else "w"
        keyboard = [[{"text": "确认", "callback_data": f"{p}a:{proposal_id}"},
                     {"text": "拒绝", "callback_data": f"{p}r:{proposal_id}"}]]
        await self._call("sendMessage", chat_id=self.chat_id, text=text[:MAX_LEN],
                         reply_markup={"inline_keyboard": keyboard})

    async def send_notice(self, text: str, notice_id: str) -> None:
        if not self.chat_id:
            raise RuntimeError("没有配置 TELEGRAM_CHAT_ID，不知道发给谁")
        keyboard = [[{"text": "有用", "callback_data": f"fb:{notice_id}:u"},
                     {"text": "没必要", "callback_data": f"fb:{notice_id}:n"}]]
        await self._call("sendMessage", chat_id=self.chat_id, text=text[:MAX_LEN],
                         reply_markup={"inline_keyboard": keyboard})

    async def send_approval(self, text: str, approval_id: str, scopes: list[str]) -> None:
        if not self.chat_id:
            raise RuntimeError("没有配置 TELEGRAM_CHAT_ID，不知道发给谁")
        labels = {"once": "批准一次", "run": "本次运行", "session": "本次会话", "1h": "一小时", "forever": "永久"}
        row = [{"text": labels.get(s, s), "callback_data": f"ap:{approval_id}:{s}"} for s in scopes]
        keyboard = [row[i:i + 3] for i in range(0, len(row), 3)]
        keyboard.append([{"text": "拒绝", "callback_data": f"dn:{approval_id}"}])
        await self._call("sendMessage", chat_id=self.chat_id, text=text[:MAX_LEN],
                         reply_markup={"inline_keyboard": keyboard})

    async def send(self, text: str) -> None:
        if not self.chat_id:
            raise RuntimeError("没有配置 TELEGRAM_CHAT_ID，不知道发给谁")
        for start in range(0, len(text), MAX_LEN):
            await self._call("sendMessage", chat_id=self.chat_id, text=text[start:start + MAX_LEN])

    async def send_file(self, path: str, caption: str) -> None:
        if not self.chat_id:
            raise RuntimeError("没有配置 TELEGRAM_CHAT_ID，不知道发给谁")
        p = Path(path)
        # 宿主存文件时在名字前加了运行号，发给你时去掉
        name = p.name.split("-", 2)[-1] if p.name.startswith("run_") else p.name
        if p.stat().st_size > MAX_UPLOAD:
            raise RuntimeError(f"{name} 超过 Telegram 的 50 MB 上限")
        if len(caption) > MAX_CAPTION:
            # 说明太长放不进文件的说明栏：先整段发文字，文件不带说明，免得截掉一半
            await self.send(caption)
            caption = ""
        # aiohttp 默认把非 ASCII 的文件名转成 %XX，Telegram 不解码，"周报.pdf" 会显示成 "%E5%91%A8%E6%8A%A5.pdf"。
        # 关掉转义，按 UTF-8 原样放进 filename="…"（名字已经过 safe_name，没有引号和换行）
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("chat_id", self.chat_id)
        if caption:
            form.add_field("caption", caption)
        form.add_field("document", p.read_bytes(), filename=name)
        await self._call("sendDocument", form=form)
