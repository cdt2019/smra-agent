from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import sessions
from .media import file_lines

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("worker")

EXIT_YIELD, EXIT_DEFER = 3, 4
PRECHECK_UNCHANGED = 10
CA_REL = ".ca/egress-ca.pem"


def egress_env(proxy_url: str, no_proxy: str, ca_path: str) -> dict[str, str]:
    """让电脑里常见的程序都走出网代理，并信任代理的 CA。"""
    env = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = proxy_url
    env["NO_PROXY"] = env["no_proxy"] = no_proxy
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                "GIT_SSL_CAINFO", "PIP_CERT"):
        env[key] = ca_path
    return env


def user_text(payload: dict) -> str:
    """一条用户消息在会话记录里的样子：发出时间 + 正文 + 附件位置。

    由收件箱里的数据确定地生成，同一个事件无论重试几次都一样。
    """
    at = datetime.fromisoformat(payload["at"]) if payload.get("at") else datetime.now()
    when = at.astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{when}] {payload.get('text', '')}".rstrip()]
    for f in payload.get("files", []):
        lines += file_lines(f)
    return "\n".join(lines)


class Worker:
    """全局唯一的工作进程：从收件箱取事件，合并，选会话，在电脑里启动一次运行。

    没有事件时它阻塞在 inbox.wait()，不调用模型，电脑里也没有 Agent 在跑。
    宿主命令（快照、恢复）也排在这里，这样它们不会和运行同时发生。
    """

    def __init__(self, app: App):
        self.app = app
        self.draining = False                # 宿主要停了：做完手上这批就不再取新的
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    async def drain(self, timeout: float) -> bool:
        """不再取新事件，等手上这批做完。超时返回 False（没做完的事件下次启动时重新处理）。"""
        self.draining = True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def loop(self) -> None:
        recovered = self.app.inbox.recover()
        if recovered:
            log.info("恢复了 %d 个上次没处理完的事件", recovered)
        while not self.draining:
            batch = self._pick_batch()
            if batch is None:
                # 两批事件之间：电脑空闲够久就挂起。这里和运行不会同时发生
                try:
                    await self.app.sleeper.maybe_suspend()
                except Exception:
                    log.exception("挂起电脑失败")
                await self.app.inbox.wait()
                continue
            self._idle.clear()
            try:
                if batch[0] == "command":
                    await self._command(batch[1][0])
                elif batch[0] == "precheck":
                    await self._precheck(batch[1][0])
                else:
                    await self._process(*batch)
            except Exception:
                log.exception("处理一批事件时出错")
                await asyncio.sleep(1)
            finally:
                self._idle.set()
                self.app.sleeper.touch()

    def _route(self, event) -> str:
        """决定事件进哪个会话（第 4.4 节），并把结果写回事件，这样重试时不会换会话。

        你的消息进当前对话；审批答复和跟进在入收件箱时就带着原会话；
        带关联键的 Webhook 和固定会话的日程进同一个键的会话；其余（日程、没有关联键的 Webhook）开新会话。
        """
        if event["session_id"]:
            return event["session_id"]
        app = self.app
        roles = app.roles
        agent_md = app.read_agent_md()
        payload = json.loads(event["payload"])
        if event["source"] == "user":
            # 消息开头 @角色 点名；没点名的交给默认角色。每个角色有自己的对话
            role = roles.addressed(payload.get("text", ""))
            session_id = sessions.current_conversation(app.db, app.cfg.session_idle_hours, agent_md,
                                                       role.name, roles.section(role), roles.default_name)
        elif payload.get("session_key"):
            # 带关联键的 Webhook、固定会话的日程：同一个键进同一个会话
            role = roles.get(payload.get("role")) or roles.default
            session_id = sessions.keyed_session(app.db, payload.get("session_kind") or event["source"],
                                                payload["session_key"], agent_md, role.name, roles.section(role))
        else:
            # 交接和角色设的日程带着角色名；角色文件被删了就交给默认角色
            role = roles.get(payload.get("role")) or roles.default
            session_id = sessions.new_session(app.db, payload.get("session_kind") or event["source"], agent_md,
                                              role=role.name, role_md=roles.section(role),
                                              chain=payload.get("chain"))
        app.inbox.set_session(event["id"], session_id)
        return session_id

    def _pick_batch(self):
        """取优先级最高的事件所在的会话，连同该会话里所有待处理的事件一起取出。

        你连发三条消息，Agent 只醒一次，看到的是三条。
        带前置检查的定时器事件先单独做检查，检查说"有变化"才进入正常流程。
        """
        pending = self.app.inbox.pending()
        if not pending:
            return None
        if pending[0]["source"] == "command":
            return "command", [pending[0]]
        pending = [e for e in pending if e["source"] != "command"]
        if json.loads(pending[0]["payload"]).get("precheck"):
            return "precheck", [pending[0]]
        # 还没做前置检查的事件不跟着别的事件一起进运行
        pending = [e for e in pending if not json.loads(e["payload"]).get("precheck")]
        # 第一个事件只路由一次：pending 里的行是路由前读的，再路由一次会给它再开一个会话
        first = pending[0]
        session_id = self._route(first)
        events = [first] + [e for e in pending[1:] if self._route(e) == session_id]
        return session_id, events

    def _precheck_argv(self, script: str) -> list[str]:
        path = f"{self.app.computer.home_path}/{script}"
        if script.endswith(".py"):
            return [self.app.computer.agent_argv[0], path]
        return ["sh", path]

    async def _precheck(self, event) -> None:
        """定时器的前置检查：在电脑里跑一个脚本，不经过模型。

        退出码 0 = 有变化，叫醒 Agent；10 = 没变化，这次到期就此结束，不产生运行；
        其他 = 脚本出错，也叫醒 Agent，让它看到错误、修脚本。
        脚本拿到的通行证只能出网（受同样的裁决），调不了模型，也碰不到宿主接口。
        """
        app = self.app
        payload = json.loads(event["payload"])
        script = payload["precheck"]
        app.inbox.claim([event["id"]])
        run = app.runs.start("", trigger_key=event["id"], trigger="timer", kind="precheck")
        outcome, result = "FAILED", None
        try:
            await app.computer.ensure_awake()
            await self._install_bundled()
            await app.computer.put_file(CA_REL, app.egress.ca_cert.read_bytes())
            env = {"AGENT_HOME": app.computer.home_path, "PYTHONUNBUFFERED": "1"}
            # 已连接服务的替身令牌，和正式运行里一样：检查脚本能读邮件、日历（发信之类的照样被拒）
            for conn in app.creds.list():
                env[f"SMRA_CRED_{conn.service.upper()}"] = conn.surrogate
            env.update(egress_env(f"http://run:{run.token}@{app.computer.egress_host}:{app.egress_port}",
                                  app.computer.no_proxy, f"{app.computer.home_path}/{CA_REL}"))
            result = await app.computer.exec(self._precheck_argv(script), env, app.cfg.precheck_timeout_s)
            outcome = "UNCHANGED" if result.exit_code == PRECHECK_UNCHANGED and not result.timed_out else "CHANGED"
        except Exception as e:
            log.exception("前置检查 %s 无法执行", script)
            report = f"前置检查 {script} 没能执行：{type(e).__name__}: {e}"
        finally:
            app.runs.finish(run, outcome)
        if outcome == "UNCHANGED":
            log.info("前置检查 %s 报告没有变化，不叫醒 Agent", script)
            app.inbox.done([event["id"]])
            return
        if result is not None:
            output = (result.stdout.strip()[-2000:] or "（没有输出）")
            if result.timed_out:
                report = f"前置检查 {script} 超时（{app.cfg.precheck_timeout_s} 秒）"
            elif result.exit_code == 0:
                report = f"前置检查 {script} 报告有变化"
            else:
                report = (f"前置检查 {script} 出错（退出码 {result.exit_code}），请检查脚本。"
                          f"标准错误的末尾：\n{result.stderr.strip()[-1000:]}")
            report += f"\n脚本的输出：\n<external>\n{output}\n</external>"
        payload["text"] = f"{payload['text']}\n{report}"
        payload["precheck"] = None
        app.inbox.update_payload(event["id"], payload)
        app.inbox.release([event["id"]])

    async def _install_bundled(self) -> None:
        try:
            await self.app.bundled.install_missing()
        except Exception:
            log.exception("放自带技能失败")

    async def _command(self, event) -> None:
        app = self.app
        payload = json.loads(event["payload"])
        app.inbox.claim([event["id"]])
        try:
            reply = await app.run_command(payload["name"], payload.get("arg", ""))
        except Exception as e:
            log.exception("命令 %s 失败", payload["name"])
            reply = f"命令失败：{type(e).__name__}: {e}"
        app.outbox.enqueue("reply", None, reply, f"{event['id']}:cmd")
        app.inbox.done([event["id"]])

    async def _process(self, session_id: str, events: list) -> None:
        app = self.app
        ids = [e["id"] for e in events]
        attempts = max(e["attempts"] for e in events) + 1
        app.inbox.claim(ids)
        payloads = [json.loads(e["payload"]) for e in events]

        # 语音转文字、照片看图。结果写回收件箱，重试时不再调
        for e, payload in zip(events, payloads):
            if e["source"] == "user" and payload.get("files"):
                try:
                    if await app.media.annotate(payload):
                        app.inbox.update_payload(e["id"], payload)
                except Exception:
                    log.exception("处理语音或图片时出错")

        # 事件先入会话记录。带 inbox_id，所以重试时不会重复写入
        for e, payload in zip(events, payloads):
            sessions.append(app.db, session_id, None, "host", "user_message",
                            {"role": "user", "content": user_text(payload)}, inbox_id=e["id"])

        if any(p.get("snapshot_first") for p in payloads) and attempts == 1:
            # 夜间整理会大改记忆文件，改之前留一份
            try:
                await app.snapshots.take("before-consolidate")
            except Exception:
                log.exception("整理前的快照失败")

        # 会话太长时先把较早的部分写成摘要（提供方没有服务端压缩时）。失败不影响这次运行
        await app.compactor.maybe_compact(session_id)

        info = sessions.info(app.db, session_id)
        run = app.runs.start(session_id, trigger_key=ids[0], trigger=events[0]["source"],
                             role=(info["role"] if info else None) or app.roles.default_name,
                             chain_root=(info["chain_root"] if info else None) or ids[0],
                             hops=info["hops"] if info else 0, chain_fg=bool(info and info["chain_fg"]))
        outcome, error = "FAILED", None
        try:
            await app.computer.ensure_awake()
            await self._install_bundled()
            # 附件放进主目录。重试时会再放一次，内容相同
            for payload in payloads:
                for f in payload.get("files", []):
                    await app.computer.put_file(f["target"], Path(f["stored"]).read_bytes())
            # 出网代理的 CA 证书放进主目录，电脑里的程序信任它，HTTPS 才能被解密裁决
            await app.computer.put_file(CA_REL, app.egress.ca_cert.read_bytes())
            # 交给电脑的只有通行证、宿主地址和主目录位置。通行证同时是出网代理的凭据
            env = {
                "RUN_TOKEN": run.token,
                "HOST_URL": app.computer.host_url,
                "AGENT_HOME": app.computer.home_path,
                "CELL_EXEC": "1" if app.computer.exec_allowed else "0",
                "PYTHONUNBUFFERED": "1",
            }
            env.update(egress_env(f"http://run:{run.token}@{app.computer.egress_host}:{app.egress_port}",
                                  app.computer.no_proxy, f"{app.computer.home_path}/{CA_REL}"))
            result = await app.computer.exec(app.computer.agent_argv, env, app.cfg.run_timeout_s)
            if result.timed_out:
                outcome, error = "TIMEOUT", f"运行超过 {app.cfg.run_timeout_s} 秒"
            elif result.exit_code == 0:
                outcome = "SENT" if run.sends else "SILENT"
            elif result.exit_code == EXIT_YIELD:
                outcome = "YIELDED"
            elif result.exit_code == EXIT_DEFER:
                outcome = "DEFERRED"
            else:
                lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()]
                error = (lines[-1] if lines else f"退出码 {result.exit_code}")[:300]
                log.warning("运行 %s 失败：%s", run.id, result.stderr.strip()[-2000:])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:300]
            log.exception("运行 %s 无法启动", run.id)
        finally:
            app.runs.finish(run, outcome, error)

        if outcome in ("SENT", "SILENT", "DEFERRED"):
            # 挂起的运行也算处理完了：接着做靠的是你答复审批时产生的那条新事件
            app.inbox.done(ids)
        elif outcome == "YIELDED":
            app.inbox.release(ids)
        elif attempts >= app.cfg.max_attempts:
            app.inbox.fail(ids)
            if run.foreground:
                app.outbox.enqueue("reply", session_id, f"这条消息我没能处理（试了 {attempts} 次）：{error}",
                                   f"{ids[0]}:failed")
            else:
                what = {"timer": "一个定时任务", "webhook": "一条外部系统的事件",
                        "handoff": "一个角色交接来的任务"}.get(run.trigger, "一个后台事件")
                app.outbox.enqueue("system", session_id, f"{what}没能处理（试了 {attempts} 次）：{error}",
                                   f"{ids[0]}:failed")
        else:
            await asyncio.sleep(min(app.cfg.retry_delay_s * 2 ** (attempts - 1), 60))
            app.inbox.release(ids)

        # 有过运行才可能有变化。最多每小时一次
        try:
            await app.snapshots.maybe_take()
        except Exception:
            log.exception("自动快照失败")
