"""宿主现在的状态：/status 命令，以及部署前在终端里看一眼能不能重启。

    uv run python -m host.status          # 打印状态（只读打开数据库，不打扰正在跑的服务）
    uv run python -m host.status --idle   # 没有运行在跑、收件箱没有待处理的才返回 0，部署脚本用
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime

from .timers import local_str


def tokens_today(db: sqlite3.Connection) -> int:
    """今天（UTC）的模型花费，每日上限按它算。"""
    day = datetime.now(UTC).date().isoformat()
    return int(db.execute("SELECT COALESCE(SUM(tokens), 0) FROM inference_log WHERE created_at >= ?",
                          (day,)).fetchone()[0])


def _ago(iso: str, at: datetime) -> str:
    seconds = max(0, int((at - datetime.fromisoformat(iso)).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"


def running(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT id, kind, role, started_at FROM run WHERE finished_at IS NULL "
                      "ORDER BY started_at").fetchall()


def pending_inbox(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM inbox WHERE status IN ('PENDING', 'CLAIMED')").fetchone()[0]


def report(db: sqlite3.Connection, cfg, *, computer: str | None = None,
           started_at: datetime | None = None, at: datetime | None = None) -> str:
    at = at or datetime.now(UTC)
    one = lambda sql: db.execute(sql).fetchone()[0]
    lines = []

    runs = running(db)
    if runs:
        lines.append("运行中：" + "；".join(
            f"{r['id']}（{'前置检查' if r['kind'] == 'precheck' else (r['role'] or '默认角色')}，"
            f"已 {_ago(r['started_at'], at)}）" for r in runs))
    else:
        lines.append("运行中：没有")
    queued = pending_inbox(db)
    if queued:
        lines.append(f"排队：{queued} 件事等着处理")

    waiting = []
    if n := one("SELECT COUNT(*) FROM approval WHERE status = 'PENDING'"):
        waiting.append(f"{n} 个网络请求（/approve 或 /deny）")
    if n := one("SELECT COUNT(*) FROM proposal WHERE status = 'PENDING'"):
        waiting.append(f"{n} 个服务档案或 Webhook（/profile、/webhook）")
    if n := one("SELECT COUNT(*) FROM connection_request WHERE status = 'PENDING'"):
        waiting.append(f"{n} 个连接请求（在宿主终端里连）")
    lines.append("等你处理：" + ("、".join(waiting) if waiting else "没有"))

    spent = tokens_today(db)
    budget = cfg.daily_token_budget
    share = f"（{spent * 100 // budget}%）" if budget else ""
    lines.append(f"今日花费：{spent:,} / {budget:,} token{share}")

    conns = db.execute("SELECT service, title, expired_at FROM connection WHERE revoked_at IS NULL "
                       "ORDER BY service").fetchall()
    lines.append("已连接：" + ("、".join(
        (c["title"] or c["service"]) + ("（授权已失效）" if c["expired_at"] else "") for c in conns) if conns else "没有"))

    timers = db.execute("SELECT name, due_at FROM timer WHERE status = 'ACTIVE' ORDER BY due_at").fetchall()
    if timers:
        lines.append(f"定时器：{len(timers)} 个，下一个 {local_str(timers[0]['due_at'])} {timers[0]['name']}")
    else:
        lines.append("定时器：没有")

    if computer:
        lines.append(f"电脑：{computer}")
    if started_at:
        lines.append(f"宿主已运行 {_ago(started_at.isoformat(), at)}")
    return "\n".join(lines)


def main() -> None:
    from .config import Config
    parser = argparse.ArgumentParser(description="宿主状态（只读）")
    parser.add_argument("--idle", action="store_true", help="空闲（没有运行、没有待处理事件）才返回 0")
    args = parser.parse_args()
    cfg = Config.from_env()
    if not cfg.db_path.exists():
        print("还没有数据库：宿主没启动过")
        return
    db = sqlite3.connect(f"file:{cfg.db_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if args.idle:
            busy, queued = running(db), pending_inbox(db)
            if busy or queued:
                print(f"忙：{len(busy)} 个运行在跑，{queued} 件事排队")
                sys.exit(1)
            print("空闲")
            return
        print(report(db, cfg))
    finally:
        db.close()


if __name__ == "__main__":
    main()
