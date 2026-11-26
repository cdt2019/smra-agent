from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field

from .db import new_id, now


@dataclass
class RunCtx:
    id: str
    token: str          # 本次运行的通行证。电脑里只有它，没有任何真密钥
    session_id: str
    trigger_key: str    # 触发这次运行的第一个收件箱事件，用于发件去重
    trigger: str        # user / timer / ...
    sends: int = 0
    tokens: int = 0
    deferred: list[str] = field(default_factory=list)   # 本次运行里等你批准的审批号
    suspended: bool = False     # 读到了疑似注入的内容：本次运行里长期授权暂停
    kind: str = "agent"         # agent：一次 Agent 运行；precheck：定时器的前置检查，只能出网
    role: str = ""              # 哪个角色在跑（第 17 节）
    chain_root: str = ""        # 这串交接的源头事件。没有交接时就是本次的触发事件
    hops: int = 0               # 交接来的第几跳
    chain_fg: bool = False      # 交接链的源头是前台运行：用户在等结果
    handoffs: int = 0           # 本次运行里发起的交接数，用于去重
    searches: int = 0           # 本次运行里经宿主做的网页搜索次数
    bounced: set[str] = field(default_factory=set)      # 已经退回过一次的请求（指纹）
    quality_bounced: set[str] = field(default_factory=set)  # 发送前检查已经退回过一次的文件名
    host_texts: set[str] = field(default_factory=set)       # 宿主自己写给 Agent 的工具结果，不当外部内容查注入
    user_sites: set[str] | None = None      # 你在这次对话里亲手发过的网址所在的网站（用到时才查）

    @property
    def foreground(self) -> bool:
        """你在等着的运行：由你的动作触发（消息、审批答复、连接了账号、审阅了档案、在浏览器里登录完），
        或者是你等着的那件事交接过来的。最终答复直接回给你。"""
        return self.trigger in ("user", "approval", "connection", "review", "browser") \
            or (self.trigger == "handoff" and self.chain_fg)


class Runs:
    """进行中的运行。通行证只存在内存里，运行结束即失效。"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._by_token: dict[str, RunCtx] = {}

    def start(self, session_id: str, trigger_key: str, trigger: str, kind: str = "agent", role: str = "",
              chain_root: str = "", hops: int = 0, chain_fg: bool = False) -> RunCtx:
        ctx = RunCtx(new_id("run"), secrets.token_urlsafe(32), session_id, trigger_key, trigger, kind=kind,
                     role=role, chain_root=chain_root or trigger_key, hops=hops, chain_fg=chain_fg)
        self.db.execute(
            "INSERT INTO run (id, session_id, trigger_key, kind, role, chain_root, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ctx.id, session_id, trigger_key, kind, role or None, ctx.chain_root, now()),
        )
        self._by_token[ctx.token] = ctx
        return ctx

    def chain_tokens(self, chain_root: str) -> int:
        """一个任务（含它引发的所有交接）到目前为止的总花费。"""
        row = self.db.execute("SELECT COALESCE(SUM(tokens_used), 0) AS n FROM run WHERE chain_root = ?",
                              (chain_root,)).fetchone()
        return int(row["n"])

    def by_token(self, token: str | None) -> RunCtx | None:
        """任何进行中的运行，包括前置检查。出网代理用它。"""
        return self._by_token.get(token) if token else None

    def agent_by_token(self, token: str | None) -> RunCtx | None:
        """只认 Agent 运行。模型代理和宿主接口用它：前置检查调不了模型，也碰不到会话和发件箱。"""
        run = self.by_token(token)
        return run if run is not None and run.kind == "agent" else None

    def by_id(self, run_id: str | None) -> RunCtx | None:
        """进行中的运行；已经结束的返回 None。"""
        return next((r for r in self._by_token.values() if r.id == run_id), None)

    def add_tokens(self, ctx: RunCtx, n: int) -> None:
        ctx.tokens += n
        self.db.execute("UPDATE run SET tokens_used = ? WHERE id = ?", (ctx.tokens, ctx.id))

    def close_orphans(self) -> int:
        """宿主启动时还没结束的运行，是上次宿主被强行停掉时留下的（事件本身会由收件箱重新处理）。"""
        return self.db.execute(
            "UPDATE run SET outcome = 'FAILED', error = '宿主停止时没结束', finished_at = ? WHERE finished_at IS NULL",
            (now(),)).rowcount

    def finish(self, ctx: RunCtx, outcome: str, error: str | None = None) -> None:
        self._by_token.pop(ctx.token, None)
        self.db.execute(
            "UPDATE run SET outcome = ?, error = ?, finished_at = ? WHERE id = ?",
            (outcome, error, now(), ctx.id),
        )
