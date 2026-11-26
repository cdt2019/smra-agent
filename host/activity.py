"""活动记录：Agent 这段时间做了什么。

以一次运行为单位：什么叫醒了它、哪个角色、结果如何、花了多少；运行里每一个对外请求和裁决、
问过你的审批、发给你的消息。数据全部来自宿主自己写的表（run、egress_log、approval、outbox、
inference_log），不依赖电脑里的程序如实上报。

/activity [天数] 在聊天里给一段摘要和一个只读页面的链接；页面挂在接管页面的端口上，
地址里带一个一小时后失效的随机串。页面没有脚本，所有内容都转义过。
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

PAGE_TTL_S = 3600
MAX_DAYS = 30
MAX_RUNS = 300

SOURCE_LABEL = {"user": "你的消息", "timer": "定时器", "webhook": "Webhook", "approval": "审批答复",
                "handoff": "角色交接", "browser": "浏览器登录", "review": "审阅", "command": "命令"}
OUTCOME_LABEL = {"SENT": "回复了", "SILENT": "做完了（没发消息）", "YIELDED": "让路", "DEFERRED": "等你批准",
                 "FAILED": "失败", "TIMEOUT": "超时", "CHANGED": "有变化", "UNCHANGED": "没变化", None: "进行中"}
VERDICT_LABEL = {"allow": "放行", "deny": "拒绝", "ask": "问你", "bounce": "退回"}
APPROVAL_LABEL = {"PENDING": "等你处理", "GRANTED": "批准", "DENIED": "拒绝", "EXPIRED": "过期"}


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M:%S")


def _seconds(start: str, end: str | None) -> int | None:
    if not end:
        return None
    return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))


def _short_url(url: str) -> str:
    """只留主机和路径：查询串里可能有令牌或个人信息。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:120]
    path = parts.path if len(parts.path) <= 80 else parts.path[:80] + "…"
    return f"{parts.hostname or ''}{path}" + ("?…" if parts.query else "")


def _preview(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def collect(db: sqlite3.Connection, days: float, at: datetime | None = None) -> dict:
    """最近 days 天的活动。返回 {since, runs: [...], compactions: [...], totals: {...}}。"""
    at = at or datetime.now(UTC)
    since = (at - timedelta(days=days)).isoformat()
    runs = db.execute(
        "SELECT r.*, i.source, i.payload AS trigger_payload FROM run r LEFT JOIN inbox i ON i.id = r.trigger_key "
        "WHERE r.started_at >= ? ORDER BY r.started_at DESC LIMIT ?", (since, MAX_RUNS)).fetchall()
    items = []
    totals = {"runs": len(runs), "failed": 0, "tokens": 0, "allow": 0, "deny": 0, "ask": 0, "bounce": 0,
              "approvals": 0, "messages": 0}
    for r in runs:
        try:
            trigger_text = json.loads(r["trigger_payload"] or "{}").get("text", "")
        except ValueError:
            trigger_text = ""
        end = r["finished_at"] or at.isoformat()
        egress = db.execute(
            "SELECT method, url, verdict, reason, created_at FROM egress_log WHERE run_id = ? ORDER BY id",
            (r["id"],)).fetchall()
        approvals = db.execute(
            "SELECT id, summary, status, scope, method, host, created_at FROM approval WHERE run_id = ? "
            "ORDER BY created_at", (r["id"],)).fetchall()
        messages = db.execute(
            "SELECT kind, body, attachment, status, created_at FROM outbox "
            "WHERE (dedupe_key LIKE ? ESCAPE '\\' OR (session_id = ? AND kind IN ('system', 'approval'))) "
            "AND created_at >= ? AND created_at <= ? ORDER BY created_at",
            (r["trigger_key"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",
             r["session_id"] or "-", r["started_at"], end)).fetchall()
        kind = "前置检查" if r["kind"] == "precheck" else SOURCE_LABEL.get(r["source"] or "", r["source"] or "")
        items.append({
            "id": r["id"], "started": r["started_at"], "when": _local(r["started_at"]),
            "seconds": _seconds(r["started_at"], r["finished_at"]), "trigger": kind,
            "trigger_text": _preview(trigger_text), "role": r["role"] or "", "outcome": r["outcome"],
            "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"] or ""), "error": r["error"] or "",
            "tokens": int(r["tokens_used"] or 0),
            "egress": [{"when": _local(e["created_at"]), "method": e["method"], "url": _short_url(e["url"]),
                        "verdict": e["verdict"], "verdict_label": VERDICT_LABEL.get(e["verdict"], e["verdict"]),
                        "reason": e["reason"]} for e in egress],
            "approvals": [{"id": a["id"], "summary": a["summary"], "status": a["status"],
                           "status_label": APPROVAL_LABEL.get(a["status"], a["status"]), "scope": a["scope"] or "",
                           "when": _local(a["created_at"])} for a in approvals],
            "messages": [{"when": _local(m["created_at"]), "kind": m["kind"], "status": m["status"],
                          "text": _preview(m["body"], 300),
                          "file": (m["attachment"] or "").rsplit("/", 1)[-1].split("-", 2)[-1]} for m in messages],
        })
        totals["failed"] += r["outcome"] in ("FAILED", "TIMEOUT")
        totals["tokens"] += int(r["tokens_used"] or 0)
        for e in egress:
            totals[e["verdict"]] = totals.get(e["verdict"], 0) + 1
        totals["approvals"] += len(approvals)
        totals["messages"] += len(messages)
    compactions = db.execute(
        "SELECT run_id, status, tokens, created_at FROM inference_log WHERE run_id LIKE 'compact:%' "
        "AND created_at >= ? ORDER BY created_at DESC", (since,)).fetchall()
    totals["tokens"] += sum(int(c["tokens"] or 0) for c in compactions)
    return {"since": since, "at": at.isoformat(), "days": days, "runs": items, "totals": totals,
            "compactions": [{"when": _local(c["created_at"]), "ok": c["status"] == 200,
                             "tokens": int(c["tokens"] or 0)} for c in compactions]}


def text_summary(data: dict, link: str | None = None, recent: int = 8) -> str:
    t = data["totals"]
    span = "今天" if data["days"] == 1 else f"最近 {data['days']:g} 天"
    if not t["runs"] and not data["compactions"]:
        lines = [f"{span}没有活动。"]
    else:
        lines = [f"{span}：运行 {t['runs']} 次" + (f"（失败 {t['failed']}）" if t["failed"] else "")
                 + f"，花费 {t['tokens']:,} token",
                 f"对外请求：放行 {t['allow']}，拒绝 {t['deny']}，退回 {t['bounce']}，问你 {t['ask']}",
                 f"审批 {t['approvals']} 个，发给你的消息 {t['messages']} 条"]
        if data["compactions"]:
            lines.append(f"压缩长对话 {len(data['compactions'])} 次")
        if data["runs"]:
            lines.append("")
            lines.append("最近的运行：")
            for run in data["runs"][:recent]:
                head = f"{run['when'][6:]} {run['trigger']}"
                if run["trigger_text"]:
                    head += f"「{_preview(run['trigger_text'], 30)}」"
                extra = f"，{len(run['egress'])} 个请求" if run["egress"] else ""
                lines.append(f"· {head} → {run['outcome_label']}{extra}")
    if link:
        lines += ["", f"完整记录（1 小时内有效）：{link}"]
    return "\n".join(lines)


STYLE = """
:root{--bg:#f5f4ef;--surface:#fffefb;--line:#dfddd3;--ink:#1d1c18;--soft:#6d6a60;--ok:#1d6b52;--bad:#a83a26;--ask:#8a5208;--tint:#ecebe4}
@media (prefers-color-scheme:dark){:root{--bg:#131412;--surface:#1d1e1b;--line:#33342f;--ink:#f1f0ea;--soft:#a3a297;--ok:#4cc29a;--bad:#f0806b;--ask:#f1c063;--tint:#2a2b27}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",system-ui,sans-serif}
main{max-width:860px;margin:auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--soft);font-size:13px;margin:0 0 18px}
.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:20px}
.totals div{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.totals b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.totals span{color:var(--soft);font-size:12px}
details{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin:0 0 8px}
summary{cursor:pointer;padding:10px 14px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;list-style:none}
summary::-webkit-details-marker{display:none}
summary .when{font-variant-numeric:tabular-nums;color:var(--soft);font-size:13px;min-width:7.5em}
summary .what{flex:1;min-width:12em;overflow-wrap:anywhere}
.tag{font-size:12px;padding:1px 8px;border-radius:9px;background:var(--tint);white-space:nowrap}
.ok{color:var(--ok)}.bad{color:var(--bad)}.ask{color:var(--ask)}
.body{padding:0 14px 12px;border-top:1px solid var(--line)}
h3{font-size:12px;color:var(--soft);font-weight:600;margin:12px 0 4px}
ul{margin:0;padding:0;list-style:none}
li{padding:4px 0;border-bottom:1px dashed var(--line);font-size:13.5px;overflow-wrap:anywhere}
li:last-child{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.muted{color:var(--soft)}
.empty{color:var(--soft);padding:24px 0;text-align:center}
"""

_VERDICT_CLASS = {"allow": "ok", "deny": "bad", "ask": "ask", "bounce": "ask"}
_OUTCOME_CLASS = {"FAILED": "bad", "TIMEOUT": "bad", "DEFERRED": "ask", "YIELDED": "ask", None: "ask"}


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def html_page(data: dict, nonce: str) -> str:
    t = data["totals"]
    span = "今天" if data["days"] == 1 else f"最近 {data['days']:g} 天"
    out = [f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           "<meta name=\"color-scheme\" content=\"light dark\"><meta name=\"robots\" content=\"noindex,nofollow\">"
           f"<title>活动记录</title><style nonce=\"{_e(nonce)}\">{STYLE}</style></head><body><main>",
           f"<h1>活动记录</h1><p class=\"sub\">{_e(span)} · 截至 {_e(_local(data['at']))} · 只读，数据来自宿主自己的记录</p>",
           "<div class=\"totals\">"]
    for value, label in ((t["runs"], "次运行"), (t["failed"], "次失败"), (f"{t['tokens']:,}", "token"),
                         (t["allow"], "个请求放行"), (t["deny"], "个请求拒绝"), (t["approvals"], "个审批"),
                         (t["messages"], "条消息")):
        out.append(f"<div><b>{_e(value)}</b><span>{_e(label)}</span></div>")
    out.append("</div>")
    if not data["runs"]:
        out.append(f"<p class=\"empty\">{_e(span)}没有运行。</p>")
    for run in data["runs"]:
        cls = _OUTCOME_CLASS.get(run["outcome"], "ok")
        what = run["trigger"] + (f"：{run['trigger_text']}" if run["trigger_text"] else "")
        meta = [run["role"]] if run["role"] else []
        if run["seconds"] is not None:
            meta.append(f"{run['seconds']} 秒")
        if run["tokens"]:
            meta.append(f"{run['tokens']:,} token")
        out.append("<details><summary>"
                   f"<span class=\"when\">{_e(run['when'])}</span><span class=\"what\">{_e(what)}</span>"
                   f"<span class=\"tag {cls}\">{_e(run['outcome_label'])}</span>"
                   f"<span class=\"muted\">{_e(' · '.join(meta))}</span></summary><div class=\"body\">")
        if run["error"]:
            out.append(f"<h3>错误</h3><p class=\"bad mono\">{_e(run['error'])}</p>")
        if run["egress"]:
            out.append(f"<h3>对外请求（{len(run['egress'])}）</h3><ul>")
            for e in run["egress"]:
                out.append(f"<li><span class=\"tag {_VERDICT_CLASS.get(e['verdict'], '')}\">{_e(e['verdict_label'])}</span> "
                           f"<span class=\"mono\">{_e(e['method'])} {_e(e['url'])}</span> "
                           f"<span class=\"muted\">{_e(e['when'][6:])} · {_e(e['reason'])}</span></li>")
            out.append("</ul>")
        if run["approvals"]:
            out.append("<h3>审批</h3><ul>")
            for a in run["approvals"]:
                scope = f"（{a['scope']}）" if a["scope"] else ""
                out.append(f"<li><span class=\"tag\">{_e(a['status_label'])}{_e(scope)}</span> {_e(a['summary'])} "
                           f"<span class=\"muted mono\">{_e(a['id'])}</span></li>")
            out.append("</ul>")
        if run["messages"]:
            out.append("<h3>发给你的消息</h3><ul>")
            for m in run["messages"]:
                file = f" 〔附件 {m['file']}〕" if m["file"] else ""
                out.append(f"<li><span class=\"muted\">{_e(m['when'][6:])}</span> {_e(m['text'])}{_e(file)}</li>")
            out.append("</ul>")
        if not (run["error"] or run["egress"] or run["approvals"] or run["messages"]):
            out.append("<p class=\"muted\">这次运行没有对外请求，也没发消息。</p>")
        out.append(f"<p class=\"muted mono\">{_e(run['id'])}</p></div></details>")
    if data["compactions"]:
        out.append("<h3>压缩长对话</h3><ul>")
        for c in data["compactions"]:
            out.append(f"<li>{_e(c['when'])} · {'成功' if c['ok'] else '失败'} · {c['tokens']:,} token</li>")
        out.append("</ul>")
    out.append("</main></body></html>")
    return "".join(out)


def parse_days(arg: str) -> float | None:
    """'/activity' 后面的天数。默认 1，最多 30。不是数字返回 None。"""
    arg = arg.strip()
    if not arg:
        return 1
    try:
        days = float(arg)
    except ValueError:
        return None
    return min(MAX_DAYS, days) if days > 0 else None
