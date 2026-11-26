"""Agent 起草、要你审阅后才生效的东西。设计文档第 15 节。

- 服务档案：档案决定哪些请求算"读"（直接放行）。一份恶意的档案可以把"删除全部"标成 read，
  所以审阅文字由宿主从解析后的档案生成，把危险的地方单独列出来，而不是转述 Agent 的说法。
- Webhook 来源：签名密钥由宿主生成，只发给你，电脑看不到。

确认之前什么都不生效。确认或拒绝后，往提交它的会话里放一条事件，让 Agent 接着做。
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from .creds import describe_auth
from .db import new_id, now
from .egress.services import Profile, effective_profile, parse_profile

if TYPE_CHECKING:
    from .app import App

CLASS_LABELS = {"read": "读取，直接放行", "write": "修改，可撤销", "send": "他人可见",
                "irreversible": "不可撤销", "money": "涉及资金"}
MAX_YAML = 20_000
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


class ProposalError(ValueError):
    """提交的东西不合规矩，原样告诉 Agent。"""


# ---------------------------------------------------------------- 档案审阅

def _rule(n: int, a) -> str:
    text = f"{n}. [{CLASS_LABELS[a.cls]}] {a.method} {a.pattern}"
    if a.summary:
        text += f" — {a.summary}"
    if a.constraint_keys:
        text += f"（长期授权限定于：{', '.join(a.constraint_keys)}）"
    return text


def profile_warnings(profile: Profile, binding: list[str] | None, current: Profile | None) -> list[str]:
    warnings = []
    for a in profile.actions:
        if a.cls == "read" and a.method not in ("GET", "HEAD"):
            what = "任何方法" if a.method == "*" else a.method
            warnings.append(f"规则 {a.id}：{what} {a.pattern} 被标成\"读取\"。确认后这类请求会不经批准直接放行，"
                            "其中可能包括修改、发送、删除")
        if a.cls in ("write", "send") and (a.method == "*" or a.pattern in ("/**", "/*")):
            warnings.append(f"规则 {a.id}：{a.method} {a.pattern} 范围很宽。对它的长期授权会覆盖这个服务几乎所有的"
                            f"{'修改' if a.cls == 'write' else '发送'}操作")
    if binding is not None:
        outside = [h for h in profile.hosts if h not in binding]
        if outside:
            warnings.append(f"档案里的主机 {', '.join(outside)} 不在这个连接的绑定范围内，会被忽略"
                            "（令牌不会发往那里）")
    if current is not None:
        dropped = [f for f in current.response_filters if f not in profile.response_filters]
        if dropped:
            warnings.append(f"去掉了现有档案的响应过滤 {', '.join(dropped)}（比如邮件里的验证码将不再被隐藏）")
        before = {a.id: a.cls for a in current.actions}
        loosened = [a.id for a in profile.actions if a.id in before and a.cls == "read" and before[a.id] != "read"]
        if loosened:
            warnings.append(f"规则 {', '.join(loosened)} 原来要问你，现在改成了直接放行")
    return warnings


def review_profile(profile: Profile, binding: list[str] | None, current: Profile | None,
                   proposal_id: str) -> str:
    if profile.site:
        lines = [f"Agent 提交了一份网站档案，需要你审阅：{profile.title}（{profile.service}）",
                 "网站档案决定浏览器在这个网站上的哪些请求直接放行、哪些可以长期授权。"
                 "它管的是浏览器带着你的登录状态发出的请求。确认前不生效。"]
    else:
        lines = [f"Agent 提交了一份服务档案，需要你审阅：{profile.title}（{profile.service}）",
                 "档案决定这个服务的哪些请求直接放行、哪些要问你，以及审批里给你看什么。确认前不生效。"]
    warnings = profile_warnings(profile, binding, current)
    if warnings:
        lines.append("\n⚠ 需要特别注意：")
        lines += [f"⚠ {w}" for w in warnings]
    hosts = [h for h in profile.hosts if binding is None or h in binding]
    lines.append(f"\n主机：{', '.join(hosts) or '（没有在绑定范围内的主机）'}")
    if not profile.site:
        lines.append(f"认证：{describe_auth(profile.auth)}")
    lines.append("规则（从上到下，第一条匹配的生效）：")
    lines += [_rule(n, a) for n, a in enumerate(profile.actions, 1)] or ["（没有规则）"]
    if profile.site:
        lines.append("没有匹配到规则的请求：照旧按浏览器的默认规则（打开页面看来源，提交每次问你、只能批\"仅这一次\"）")
    else:
        lines.append("没有匹配到规则的请求：GET、HEAD 直接放行；其余按不可撤销处理，每次问你并展示原始请求")
    unconstrained = [a.id for a in profile.actions if a.cls == "send" and not a.constraint_keys]
    if unconstrained:
        lines.append(f"规则 {', '.join(unconstrained)} 没有约束键，只能一次一批地批准")
    if not profile.site:
        lines.append(f"响应过滤：{', '.join(profile.response_filters) or '无'}")
    if current is not None:
        before, after = {a.id: a.cls for a in current.actions}, {a.id: a.cls for a in profile.actions}
        changes = [f"新增 {k}" for k in after if k not in before] + [f"删除 {k}" for k in before if k not in after]
        changes += [f"{k}：{before[k]} → {after[k]}" for k in after if k in before and before[k] != after[k]]
        lines.append(f"这份档案会替换现有的档案。变化：{'；'.join(changes) or '规则的类别没有变化'}")
    lines.append(f"\n确认：/profile approve {proposal_id}\n拒绝：/profile reject {proposal_id}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 提交与处理

class Proposals:
    def __init__(self, app: App):
        self.app = app
        self.db: sqlite3.Connection = app.db

    def _insert(self, kind: str, name: str, content: str, review_fn, session_id: str) -> str:
        # 同一个东西的旧提交作废，只保留最新的一份待审
        self.db.execute("UPDATE proposal SET status = 'SUPERSEDED', resolved_at = ? "
                        "WHERE kind = ? AND name = ? AND status = 'PENDING'", (now(), kind, name))
        proposal_id = new_id("pp")
        review = review_fn(proposal_id)
        self.db.execute(
            "INSERT INTO proposal (id, kind, name, content, review, session_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)",
            (proposal_id, kind, name, content, review, session_id, now()),
        )
        self.app.outbox.enqueue("review", session_id, review, f"proposal:{proposal_id}",
                                meta={"kind": kind, "id": proposal_id})
        return proposal_id

    def propose_profile(self, session_id: str, yaml_text: str) -> str:
        if len(yaml_text) > MAX_YAML:
            raise ProposalError(f"档案太长（超过 {MAX_YAML} 字）")
        try:
            profile = parse_profile(YAML(typ="safe").load(yaml_text))
        except ProposalError:
            raise
        except Exception as e:
            raise ProposalError(f"档案解析不了：{e}") from None
        existing = self.app.services.profiles.get(profile.service)
        if profile.site:
            if self.app.creds.get(profile.service) is not None or (existing is not None and not existing.site):
                raise ProposalError(f"{profile.service} 已经是一个接口服务的名字，网站档案换个名字")
            taken = {h: p.service for p in self.app.services.profiles.values()
                     if p.site and p.service != profile.service for h in p.hosts}
            clash = [f"{h}（{taken[h]}）" for h in profile.hosts if h in taken]
            if clash:
                raise ProposalError(f"这些主机已经在别的网站档案里：{', '.join(clash)}")
            current = existing if existing is not None and existing.site else None
            return self._insert("profile", profile.service, yaml_text,
                                lambda pid: review_profile(profile, None, current, pid), session_id)
        if existing is not None and existing.site:
            raise ProposalError(f"{profile.service} 已经是一个网站档案的名字，换个名字")
        conn = self.app.creds.get(profile.service)
        if conn is None:
            raise ProposalError(f"还没有连接 {profile.service}。先用 connection_request 请用户连接")
        current = effective_profile(self.app.services.profiles.get(profile.service), conn)
        current = current if current is not None and current.reviewed else None
        return self._insert("profile", profile.service, yaml_text,
                            lambda pid: review_profile(profile, conn.hosts, current, pid), session_id)

    def propose_webhook(self, session_id: str, name: str, title: str, correlation: str, skill: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", name or ""):
            raise ProposalError("来源名要小写字母开头，只含小写字母、数字、- 和 _")
        if name in self.app.webhook_sources:
            raise ProposalError(f"Webhook 来源 {name} 已经有了")
        if correlation and not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*", correlation):
            raise ProposalError("correlation 是请求体里的字段路径，例如 ticket.id")
        if skill and not SKILL_NAME.fullmatch(skill):
            raise ProposalError("skill 是 skills/ 下的目录名")
        spec = {"title": (title or name)[:60], "correlation": correlation or "", "skill": skill or ""}

        def review(pid: str) -> str:
            lines = [f"Agent 想开通一个 Webhook 来源：{spec['title']}（{name}）",
                     "开通后，这个外部系统发来的事件会叫醒 Agent（后台运行，它决定要不要告诉你）。"]
            if spec["correlation"]:
                lines.append(f"关联键：请求体里的 {spec['correlation']}，同一个键的事件进同一个会话")
            if spec["skill"]:
                lines.append(f"处理时按技能 skills/{spec['skill']}/SKILL.md 做")
            lines.append(f"\n确认后宿主会生成签名密钥，只发给你。\n确认：/webhook approve {pid}\n拒绝：/webhook reject {pid}")
            return "\n".join(lines)

        return self._insert("webhook", name, json.dumps(spec, ensure_ascii=False), review, session_id)

    def resolve(self, kind: str, proposal_id: str, approve: bool) -> str:
        row = self.db.execute("SELECT * FROM proposal WHERE id = ? AND kind = ?", (proposal_id, kind)).fetchone()
        if row is None:
            return f"找不到 {proposal_id}。"
        if row["status"] != "PENDING":
            return f"{proposal_id} 已经处理过了（{row['status']}）。"
        reply, event = ("已拒绝。", f"用户拒绝了你提交的{'服务档案' if kind == 'profile' else 'Webhook 来源'} {row['name']}。")
        if approve:
            reply, event = (self._apply_profile(row) if kind == "profile" else self._apply_webhook(row))
        self.db.execute("UPDATE proposal SET status = ?, resolved_at = ? WHERE id = ?",
                        ("APPROVED" if approve else "REJECTED", now(), proposal_id))
        self.app.inbox.add("review", proposal_id, {"text": f"[审阅] {event}", "at": now()},
                           session_id=row["session_id"])
        return reply

    def _apply_profile(self, row) -> tuple[str, str]:
        folder: Path = self.app.cfg.data_dir / "profiles"
        folder.mkdir(parents=True, exist_ok=True)
        # 存的就是你审阅的那份原文
        (folder / f"{row['name']}.yaml").write_text(row["content"], encoding="utf-8")
        self.app.reload_profiles()
        return (f"服务档案 {row['name']} 已生效。",
                f"用户确认了服务档案 {row['name']}，从现在起生效。")

    def _apply_webhook(self, row) -> tuple[str, str]:
        spec = json.loads(row["content"])
        secret = secrets.token_urlsafe(32)
        path = self.app.cfg.data_dir / "webhooks.yaml"
        block = [f"\n{row['name']}:", f"  title: {json.dumps(spec['title'], ensure_ascii=False)}",
                 f"  secret: {json.dumps(secret)}"]
        if spec["correlation"]:
            block.append(f"  correlation: {json.dumps(spec['correlation'])}")
        if spec["skill"]:
            block.append(f"  skill: {json.dumps(spec['skill'])}")
        with path.open("a", encoding="utf-8") as f:       # 追加，不动你手写的部分
            f.write("\n".join(block) + "\n")
        self.app.reload_webhooks()
        reply = (f"Webhook 来源 {row['name']} 已开通。在对方系统里填：\n"
                 f"地址：<你的隧道地址>/webhook/{row['name']}（本机端口 {self.app.webhook_port}）\n"
                 f"签名：HMAC-SHA256，请求头 X-Smra-Signature: sha256=<十六进制>\n"
                 f"密钥：{secret}\n这条消息里的密钥只发给你，Agent 看不到。")
        return reply, f"用户开通了 Webhook 来源 {row['name']}。它的事件之后会叫醒你。"
