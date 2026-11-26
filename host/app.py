from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp
from aiohttp import web

from . import activity, retention, server, sessions, status, usage
from .browser import BrowserAgent
from .bundled import BundledSkills
from .compact import Compactor
from .computer import Computer
from .config import DEFAULT_AGENT_MD, ROOT, Config
from .creds import CredentialStore, load_key
from .db import connect, new_id, now
from .egress.adjudicator import Adjudicator
from .egress.proxy import EgressProxy
from .egress.services import ServiceRegistry, load_profiles
from .egress.urls import extract_links
from .files import inbox_target, safe_name
from .guard import InjectionCheck
from .inbox import Inbox
from .media import Media
from .outbox import Outbox
from .proposals import SKILL_NAME, Proposals
from .roles import Roles
from .runs import Runs
from .sleeper import Sleeper
from .snapshots import Snapshots
from .takeover import build as build_takeover
from .timers import Scheduler, TimerError, Timers
from .webhooks import build as build_webhooks
from .webhooks import load_sources
from .worker import Worker

log = logging.getLogger("app")

PROFILES_DIR = ROOT / "profiles"     # 仓库自带的服务档案；data/profiles 里的同名档案覆盖它们

HELP = """可用命令：
@<角色> 消息  对某个角色说话（不点名就交给默认角色）
/status  现在在忙什么、等你处理的、今日花费、已连接的账号
/activity [天数]  Agent 做了什么：每次运行的对外请求、审批、发出的消息（默认今天）
/usage [天数]  花费：按天、按角色、花得最多的会话（默认 7 天）
/roles  列出角色
/new  开一个新对话（记忆和文件都保留）
/snapshot  给主目录做一个快照
/snapshots  列出快照
/restore <序号或名字>  把主目录恢复到某个快照（恢复前的状态会另存一个快照）
/approve <审批号> [范围]  批准一个网络请求，范围默认 once
/deny <审批号>  拒绝一个网络请求
/timers  列出定时器和最近几次的运行情况
/cancel <定时器号>  取消一个定时器
/run <定时器号>  让一个日程现在就做一次（不改它的下次时间）
/resume <定时器号>  重新打开一个取消了的日程
/skill <技能名> [要求]  让 Agent 现在按某个技能做一次
/skills  列出自带技能（早间简报、盯邮件、盯日历）和它们在主目录里的状态
/skills reset <技能名>  用仓库里的版本覆盖主目录里的自带技能
/feedback <通知号> useful|unnecessary  给一条通知打分
/profile approve|reject <编号>  确认或拒绝 Agent 提交的服务档案
/webhook approve|reject <编号>  确认或拒绝 Agent 申请的 Webhook 来源
/help  显示这段说明"""

# 这些命令要排进收件箱，由工作进程在两次运行之间执行
QUEUED_COMMANDS = {"/snapshot", "/snapshots", "/restore"}


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class App:
    """宿主进程。把各个部件接起来：接收端、工作进程、分发器、两个出口。"""

    def __init__(self, cfg: Config, channel, computer: Computer):
        self.cfg = cfg
        self.channel = channel
        self.computer = computer
        self.db = connect(cfg.db_path)
        self.inbox = Inbox(self.db)
        self.outbox = Outbox(self.db, cfg)
        self.runs = Runs(self.db)
        self.roles = Roles(cfg.roles_dir, cfg.default_role, cfg.handoff_max_hops)
        self.timers = Timers(self.db, cfg)
        self.scheduler = Scheduler(self)
        self.webhook_sources = load_sources(cfg.data_dir / "webhooks.yaml")
        self.webhook_port = 0
        self.snapshots = Snapshots(cfg.data_dir / "snapshots", computer,
                                   keep=cfg.snapshot_keep, interval_s=cfg.snapshot_interval_s)
        self.adjudicator = Adjudicator(self.db, self, hold_s=cfg.egress_hold_s)
        self.creds = CredentialStore(self.db, load_key(cfg.data_dir), lambda: self.http)
        self.creds.on_expired = self._credential_expired
        self.services = ServiceRegistry(load_profiles(PROFILES_DIR, cfg.data_dir / "profiles"), self.creds)
        self.proposals = Proposals(self)
        self.bundled = BundledSkills(self)
        self.injection = InjectionCheck(cfg.api_key, cfg.model_upstream, cfg.injection_model,
                                        cfg.model_protocol, cfg.model_features, cfg.guard_vision) \
            if cfg.injection_check and cfg.api_key else None
        self.media = Media(cfg)
        self.egress = EgressProxy(self)
        self.egress_port = 0
        # 浏览器第一次被用到时才启动
        self.browser = BrowserAgent(self) if cfg.browser != "off" else None
        self.takeover_port = 0
        self._takeover_runner: web.AppRunner | None = None
        self.worker = Worker(self)
        self.compactor = Compactor(self)
        self.activity_pages: dict[str, tuple[float, float]] = {}    # 活动记录页面的临时钥匙 → (过期时间, 天数)
        self.sleeper = Sleeper(self)
        self.http: aiohttp.ClientSession | None = None
        self.port = 0
        self._runner: web.AppRunner | None = None
        self._hook_runner: web.AppRunner | None = None
        self._tasks: list[asyncio.Task] = []
        self.started_at: datetime | None = None

    def reload_profiles(self) -> None:
        self.services.profiles = load_profiles(PROFILES_DIR, self.cfg.data_dir / "profiles")

    def reload_webhooks(self) -> None:
        self.webhook_sources = load_sources(self.cfg.data_dir / "webhooks.yaml")

    def read_agent_md(self) -> str:
        """AGENT.md 只有你能改。新会话开始时读一次，会话内不变。"""
        path = self.cfg.agent_md_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_AGENT_MD, encoding="utf-8")
        return path.read_text(encoding="utf-8")

    def _store_upload(self, name: str, data: bytes) -> dict:
        folder = self.cfg.data_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        stored = folder / f"{new_id('up')}-{safe_name(name)}"
        stored.write_bytes(data)
        return {"name": name, "stored": str(stored), "size": len(data), "target": inbox_target(name)}

    async def on_user_text(self, source_id: str, text: str,
                           files: list[tuple[str, bytes]] | None = None) -> None:
        """接收端：只做最少的事，写入收件箱后立刻返回。"""
        text = (text or "").strip()
        if not text and not files:
            return
        command, _, arg = text.partition(" ")
        if command == "/new":
            sessions.close_conversation(self.db)
            dropped = self.browser is not None and self.browser.drop_takeover()
            self.outbox.enqueue("reply", None, "已开启新对话。记忆和文件都还在。"
                                + ("之前没完成的浏览器登录也结束了。" if dropped else ""), f"cmd:{source_id}")
            return
        if command == "/help":
            self.outbox.enqueue("reply", None, HELP, f"cmd:{source_id}")
            return
        if command == "/roles":
            self.outbox.enqueue("reply", None, self.roles.listing(), f"cmd:{source_id}")
            return
        if command == "/status":
            self.outbox.enqueue("reply", None, await self.status_text(), f"cmd:{source_id}")
            return
        if command == "/activity":
            days = activity.parse_days(arg)
            reply = self.activity_text(days) if days else "用法：/activity [天数]，天数默认 1，最多 30"
            self.outbox.enqueue("reply", None, reply, f"cmd:{source_id}")
            return
        if command == "/usage":
            days = usage.parse_days(arg)
            reply = (usage.text_summary(usage.collect(self.db, days), self.cfg.daily_token_budget) if days
                     else f"用法：/usage [天数]，天数默认 {usage.DEFAULT_DAYS}，最多 {usage.MAX_DAYS}")
            self.outbox.enqueue("reply", None, reply, f"cmd:{source_id}")
            return
        if command in ("/approve", "/deny"):
            # 立刻处理，不排队：被挂住的请求正等着这个答复
            approval_id, _, scope = arg.strip().partition(" ")
            reply = self.adjudicator.resolve(approval_id, command == "/approve", scope.strip() or "once")
            self.outbox.enqueue("reply", None, reply, f"cmd:{source_id}")
            return
        if command in ("/profile", "/webhook"):
            verb, _, proposal_id = arg.strip().partition(" ")
            if verb in ("approve", "reject") and proposal_id.strip():
                reply = self.proposals.resolve(command[1:], proposal_id.strip(), verb == "approve")
            else:
                reply = f"用法：{command} approve|reject <编号>"
            self.outbox.enqueue("reply", None, reply, f"cmd:{source_id}")
            return
        if command in ("/timers", "/cancel", "/run", "/resume", "/feedback"):
            self.outbox.enqueue("reply", None, self._quick_command(command, arg.strip()), f"cmd:{source_id}")
            return
        if command == "/skills":
            verb, _, name = arg.strip().partition(" ")
            if verb == "reset" and SKILL_NAME.fullmatch(name.strip()):
                # 要写主目录：排进收件箱，和运行错开
                self.inbox.add("command", source_id, {"name": "/skills reset", "arg": name.strip()})
                return
            reply = self.bundled.listing() if not arg.strip() else "用法：/skills 或 /skills reset <技能名>"
            self.outbox.enqueue("reply", None, reply, f"cmd:{source_id}")
            return
        if command == "/skill":
            name, _, wish = arg.strip().partition(" ")
            if not SKILL_NAME.fullmatch(name):
                self.outbox.enqueue("reply", None, "用法：/skill <技能名> [要求]。技能名是 skills/ 下的目录名",
                                    f"cmd:{source_id}")
                return
            # 宿主看不到主目录，技能在不在由 Agent 去看；这条就是一条普通的消息，进当前对话
            text = f"[用户要求按技能处理] 先读 skills/{name}/SKILL.md，照它做一次。" \
                   + (f"\n这次的要求：{wish.strip()}" if wish.strip() else "")
        if command in QUEUED_COMMANDS:
            self.inbox.add("command", source_id, {"name": command, "arg": arg.strip()})
            return
        payload: dict[str, Any] = {"text": text, "at": now()}
        if files:
            payload["files"] = [self._store_upload(name, data) for name, data in files]
        # 你消息里的地址是你给的，之后 Agent 可以直接访问
        self.adjudicator.add_seen(extract_links(text, "https://invalid/"), "user")
        self.inbox.add("user", source_id, payload)

    async def status_text(self) -> str:
        try:
            state = (await self.computer.state()).value
        except Exception as e:      # 查不到电脑的状态不影响别的几项
            state = f"查不到（{type(e).__name__}）"
        computer = {"running": "开着", "suspended": "挂起（下一件事来时唤醒）", "stopped": "停着",
                    "absent": "还没建"}.get(state, state)
        return status.report(self.db, self.cfg, computer=computer, started_at=self.started_at)

    def activity_text(self, days: float) -> str:
        return activity.text_summary(activity.collect(self.db, days), self.activity_link(days))

    def activity_link(self, days: float) -> str | None:
        """只读的活动记录页面，挂在接管页面的端口上。钥匙一小时后失效。没开那个端口就没有链接。"""
        if not self.takeover_port:
            return None
        clock = time.monotonic()
        self.activity_pages = {k: v for k, v in self.activity_pages.items() if v[0] > clock}
        key = secrets.token_urlsafe(18)
        self.activity_pages[key] = (clock + activity.PAGE_TTL_S, days)
        base = self.cfg.takeover_url or f"http://{self.cfg.takeover_bind}:{self.takeover_port}"
        return f"{base.rstrip('/')}/activity/{key}"

    def activity_page_days(self, key: str) -> float | None:
        entry = self.activity_pages.get(key)
        if entry is None or entry[0] <= time.monotonic():
            self.activity_pages.pop(key, None)
            return None
        return entry[1]

    def _quick_command(self, command: str, arg: str) -> str:
        """只动宿主自己的表、不碰电脑的命令，立刻处理。"""
        if command == "/timers":
            lines = []
            for r in self.timers.active():
                lines.append(self.timers.describe(r))
                lines += [f"    {h}" for h in self.timers.history(r["id"])]
            text = "定时器：\n" + "\n".join(lines) if lines else "没有进行中的定时器。"
            cancelled = self.timers.cancelled_schedules()
            if cancelled:
                text += "\n\n最近取消的日程（/resume <定时器号> 重新打开）：\n" + "\n".join(
                    f"{r['id']} {r['name']}（cron {r['cron']}）" for r in cancelled)
            return text
        if command == "/cancel":
            try:
                row = self.timers.cancel(arg, by="user")
            except TimerError as e:
                return str(e)
            extra = f"。要恢复发 /resume {row['id']}" if row["kind"] == "schedule" else ""
            return f"已取消：{row['name']}{extra}"
        if command == "/run":
            return self.scheduler.run_now(arg)
        if command == "/resume":
            try:
                row = self.timers.resume(arg, self.scheduler.clock())
            except TimerError as e:
                return str(e)
            return f"已恢复：{self.timers.describe(row)}"
        outbox_id, _, value = arg.partition(" ")
        return self.outbox.set_feedback(outbox_id, value.strip())

    async def run_command(self, name: str, arg: str) -> str:
        snaps = self.snapshots
        if name == "/snapshot":
            return f"已做快照：{await snaps.take('manual')}"
        if name == "/snapshots":
            names = snaps.list()
            if not names:
                return "还没有快照。"
            return "快照（最新的在前）：\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names[:20], 1))
        if name == "/restore":
            target = snaps.resolve(arg)
            if not target:
                return "找不到这个快照。用 /snapshots 查看序号。"
            safety = await snaps.restore(target)
            return f"主目录已恢复到 {target}。恢复前的状态存为 {safety}，需要时可以再恢复回去。"
        if name == "/skills reset":
            return await self.bundled.reset(arg)
        return f"不认识的命令：{name}"

    async def start(self, with_channel: bool = True) -> None:
        self.started_at = datetime.now(UTC)
        if orphans := self.runs.close_orphans():
            log.info("上次停止时有 %d 个运行没结束，已记为失败（事件会重新处理）", orphans)
        self.http = aiohttp.ClientSession()
        self._runner = web.AppRunner(server.build(self))
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.bind, self.cfg.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # 配置为 0 时取实际端口
        self.egress_port = self.cfg.egress_port or _free_port(self.cfg.bind)
        await self.egress.start(self.cfg.bind, self.egress_port)
        self.computer.configure(self.port, self.egress_port)
        log.info("宿主接口与模型代理监听在 %s:%d", self.cfg.bind, self.port)
        if self.cfg.webhook_port >= 0:
            self._hook_runner = web.AppRunner(build_webhooks(self))
            await self._hook_runner.setup()
            hook_site = web.TCPSite(self._hook_runner, self.cfg.webhook_bind, self.cfg.webhook_port)
            await hook_site.start()
            self.webhook_port = hook_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            log.info("Webhook 接收端监听在 %s:%d（来源 %d 个）", self.cfg.webhook_bind, self.webhook_port,
                     len(self.webhook_sources))

        # 给你看的页面（浏览器接管、活动记录）。没开浏览器时接管的地址一律 404，活动记录照常
        if self.cfg.takeover_port >= 0:
            self._takeover_runner = web.AppRunner(build_takeover(self))
            await self._takeover_runner.setup()
            tk_site = web.TCPSite(self._takeover_runner, self.cfg.takeover_bind, self.cfg.takeover_port)
            await tk_site.start()
            self.takeover_port = tk_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            log.info("给你看的页面（浏览器接管、活动记录）监听在 %s:%d", self.cfg.takeover_bind, self.takeover_port)

        self.scheduler.ensure_builtin()
        self._tasks = [
            asyncio.create_task(self.worker.loop(), name="worker"),
            asyncio.create_task(self.outbox.dispatch(self.channel), name="dispatcher"),
            asyncio.create_task(self.scheduler.loop(), name="scheduler"),
            asyncio.create_task(self._expire_loop(), name="approval-expiry"),
            asyncio.create_task(self._retention_loop(), name="retention"),
        ]
        if with_channel:
            self._tasks.append(asyncio.create_task(self.channel.listen(self.on_user_text), name="channel"))

    def _credential_expired(self, conn, command: str) -> None:
        """某个 OAuth 连接的授权失效了：提醒你一次怎么重连（每个连接只提醒一次，重连后是新连接）。"""
        name = conn.title or conn.service
        google = "--google-login" in command
        self.outbox.enqueue("system", None, (
            f"{name} 的授权已失效，Agent 现在用不了它。\n"
            + ("常见原因：Google 上的应用还在“测试”状态，授权 7 天就过期；或者你在 Google 账号里撤销了授权。\n"
               if google else "可能是授权被撤销或过期了。\n")
            + f"在宿主上重新连接（client id 和 secret 回车沿用）：\n{command}"),
            f"cred-expired:{conn.surrogate}")

    async def _expire_loop(self) -> None:
        while True:
            try:
                self.adjudicator.expire_stale()
            except Exception:
                log.exception("处理过期审批失败")
            await asyncio.sleep(60)

    async def _retention_loop(self) -> None:
        """每天清一次过期的数据（启动时先清一次）。"""
        while True:
            try:
                retention.purge(self.db, self.cfg)
            except Exception:
                log.exception("清理过期数据失败")
            await asyncio.sleep(24 * 3600)

    async def stop(self, grace_s: float = 0) -> None:
        """grace_s > 0 时先等手上的运行做完（期间照常收消息、发回复），超时再强停。"""
        if grace_s > 0 and self.worker.busy:
            log.info("有运行在跑，最多等 %g 秒让它做完", grace_s)
            if await self.worker.drain(grace_s):
                log.info("运行做完了，接着收尾")
            else:
                log.warning("等了 %g 秒还没做完，强行停止；没做完的事件下次启动时重新处理", grace_s)
            await asyncio.sleep(1)      # 让发件器把刚产生的回复发出去
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.browser is not None:
            await self.browser.close()
        if self._takeover_runner:
            await self._takeover_runner.cleanup()
        await self.egress.stop()
        if self._runner:
            await self._runner.cleanup()
        if self._hook_runner:
            await self._hook_runner.cleanup()
        if self.http:
            await self.http.close()
        if close := getattr(self.injection, "close", None):
            await close()
        await self.media.close()
        self.db.close()
