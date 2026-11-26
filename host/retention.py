"""数据保留期限：每天清一次过期的数据。

库里大头是模型请求日志（每次调用的完整请求和回复，含邮件、日历等明文）；其次是会话记录。
- 模型请求日志：超过 retain_inference_days 天的抹掉请求和回复正文，只留花费（每日花费上限要用）。
- 内容：超过 retain_days 天的已关闭会话的记录和全文索引、处理完的收件箱事件、发出的消息、
  处理完的审批、出网记录、处理完的提议和连接请求。
- 文件：上传和待发送的暂存文件超过 retain_file_days 天就删（上传的已经放进电脑主目录了）。
任何一项设成 0 表示永久保留。不碰的：定时器、授权、已连接的账号、"见过的地址"、运行记录（只有花费和结果）。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("retention")


def _cutoff(at: datetime, days: int) -> str | None:
    return (at - timedelta(days=days)).astimezone(UTC).isoformat(timespec="microseconds") if days > 0 else None


def purge(db: sqlite3.Connection, cfg, at: datetime | None = None) -> dict[str, int]:
    at = at or datetime.now(UTC)
    counts: dict[str, int] = {}

    def run(name: str, sql: str, params: tuple) -> None:
        n = db.execute(sql, params).rowcount
        if n:
            counts[name] = counts.get(name, 0) + n

    cut = _cutoff(at, cfg.retain_inference_days)
    if cut:
        run("模型请求日志", "UPDATE inference_log SET request = '', response = '' "
                      "WHERE created_at < ? AND (request != '' OR response != '')", (cut,))

    cut = _cutoff(at, cfg.retain_days)
    if cut:
        # 还有跟进要回来的会话不清：跟进到期时要看得到之前的对话
        old_sessions = ("SELECT id FROM session WHERE status = 'CLOSED' AND last_event_at < ? "
                        "AND id NOT IN (SELECT session_id FROM timer WHERE status = 'ACTIVE' AND session_id IS NOT NULL)")
        run("全文索引", f"DELETE FROM event_text WHERE session_id IN ({old_sessions})", (cut,))
        run("会话记录", f"DELETE FROM event WHERE session_id IN ({old_sessions})", (cut,))
        run("收件箱", "DELETE FROM inbox WHERE status IN ('DONE', 'FAILED') AND created_at < ?", (cut,))
        run("发件箱", "DELETE FROM outbox WHERE status IN ('SENT', 'FAILED') AND created_at < ?", (cut,))
        run("审批", "DELETE FROM approval WHERE status != 'PENDING' AND created_at < ? "
                  "AND id NOT IN (SELECT approval_id FROM grant_)", (cut,))
        run("出网记录", "DELETE FROM egress_log WHERE created_at < ?", (cut,))
        run("提议", "DELETE FROM proposal WHERE status != 'PENDING' AND created_at < ?", (cut,))
        run("连接请求", "DELETE FROM connection_request WHERE status != 'PENDING' AND created_at < ?", (cut,))

    if cfg.retain_file_days > 0:
        limit = (at - timedelta(days=cfg.retain_file_days)).timestamp()
        waiting = {r["attachment"] for r in db.execute(
            "SELECT attachment FROM outbox WHERE status = 'PENDING' AND attachment IS NOT NULL")}
        for folder in ("uploads", "outgoing"):
            for f in (Path(cfg.data_dir) / folder).glob("*"):
                if f.is_file() and f.stat().st_mtime < limit and str(f) not in waiting:
                    f.unlink(missing_ok=True)
                    counts["文件"] = counts.get("文件", 0) + 1

    if counts:
        log.info("清理过期数据：%s", "，".join(f"{k} {v}" for k, v in counts.items()))
        pages, free = db.execute("PRAGMA page_count").fetchone()[0], db.execute("PRAGMA freelist_count").fetchone()[0]
        if pages and free / pages > 0.25:
            db.execute("VACUUM")        # 空出来的超过四分之一才整理，把文件真的变小
            log.info("数据库整理完：%d 页里空了 %d 页", pages, free)
    return counts
