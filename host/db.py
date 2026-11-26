from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,              -- user / command / approval / handoff / webhook / timer
    source_id   TEXT NOT NULL,              -- 来源方的唯一标识，用于去重
    session_id  TEXT,                       -- 路由后写入，重试时保持不变
    priority    INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / CLAIMED / DONE / FAILED
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS session (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,            -- conversation / timer / webhook / approval
    status        TEXT NOT NULL,            -- OPEN / CLOSED
    agent_md      TEXT,                     -- 会话开始时 AGENT.md 的内容，会话内不变
    session_key   TEXT,                     -- 关联键，如 "tickets:T-123"。同一个键的 Webhook 进同一会话
    role          TEXT,                     -- 这个会话属于哪个角色；空表示默认角色
    role_md       TEXT,                     -- 会话开始时的角色说明，会话内不变
    chain_root    TEXT,                     -- 交接来的会话：引发这串交接的那个事件
    hops          INTEGER NOT NULL DEFAULT 0,   -- 交接来的会话：这是第几跳
    chain_fg      INTEGER NOT NULL DEFAULT 0,   -- 交接来的会话：用户在等结果（源头是前台运行）
    created_at    TEXT NOT NULL,
    last_event_at TEXT NOT NULL
);

-- 定时器库：以后才该处理的事。到期后由调度器变成收件箱事件，它自己从不启动运行
CREATE TABLE IF NOT EXISTS timer (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,            -- follow_up（一次）/ schedule（按 cron 重复）
    name          TEXT NOT NULL,            -- 一句话说明，给你看
    due_at        TEXT NOT NULL,            -- 下一次到期（UTC）
    cron          TEXT,                     -- 仅日程，按本地时间解释
    session_id    TEXT,                     -- 仅跟进：设定它的会话
    payload       TEXT NOT NULL,            -- 到时候做什么、判断依据、进度文件路径（JSON）
    precheck      TEXT,                     -- 前置检查：主目录里的脚本，或 builtin:<名字>
    status        TEXT NOT NULL,            -- ACTIVE / DONE / CANCELLED
    created_by    TEXT NOT NULL,            -- agent / user / system
    created_at    TEXT NOT NULL,
    last_fired_at TEXT
);

CREATE TABLE IF NOT EXISTS run (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'agent',   -- agent / precheck
    role        TEXT,
    chain_root  TEXT,                       -- 同一串交接的运行共用一个，用来算一个任务的总花费
    outcome     TEXT,                       -- SENT / SILENT / YIELDED / FAILED / TIMEOUT
    error       TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS event (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    run_id      TEXT,
    origin      TEXT NOT NULL,              -- self（电脑里上报）/ host（宿主实证）
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    inbox_id    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_session ON event (session_id, seq);
-- 同一个收件箱事件只入会话记录一次，重试不重复
CREATE UNIQUE INDEX IF NOT EXISTS event_inbox ON event (inbox_id) WHERE inbox_id IS NOT NULL;

-- recall 用的全文索引。trigram 分词支持中文子串检索
CREATE VIRTUAL TABLE IF NOT EXISTS event_text USING fts5(
    text, session_id UNINDEXED, seq UNINDEXED, role UNINDEXED, created_at UNINDEXED,
    tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS outbox (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- reply / now / digest / approval / system
    session_id  TEXT,
    body        TEXT NOT NULL,
    attachment  TEXT,                       -- 要发送的文件在宿主上的路径
    meta        TEXT,                       -- 审批消息的附加信息（JSON）
    dedupe_key  TEXT NOT NULL UNIQUE,       -- 同一触发的第 n 条消息只发一次
    status      TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / SENT / FAILED
    deliver_after TEXT,                     -- 早于这个时间不送（安静时段、等摘要时间）
    requested   TEXT,                       -- Agent 要的节奏（now / digest）；被降级时和 kind 不同
    attempts    INTEGER NOT NULL DEFAULT 0,
    feedback    TEXT,                       -- useful / unnecessary，你点的
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);

-- 宿主"见过"的地址：出现在经过代理的响应、搜索结果或你的消息里。只有这些地址能直接 GET
CREATE TABLE IF NOT EXISTS seen_url (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,              -- response / search / user
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    category      TEXT NOT NULL,            -- web_read / web_write / svc:<类别>
    service       TEXT,                     -- 已连接服务的请求：服务名
    action        TEXT,                     -- 匹配到的档案规则
    constraint_   TEXT,                     -- 这个请求的约束值（JSON），批准长期授权时成为授权的约束
    method        TEXT NOT NULL,
    host          TEXT NOT NULL,
    url           TEXT NOT NULL,
    summary       TEXT NOT NULL,            -- 裁决者生成，不来自 Agent
    scopes        TEXT NOT NULL,            -- 可选的授权范围（JSON 数组）
    status        TEXT NOT NULL,            -- PENDING / GRANTED / DENIED / EXPIRED
    deferred      INTEGER NOT NULL DEFAULT 0,   -- 请求已经以 403 返回，运行挂起了
    scope         TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);

CREATE TABLE IF NOT EXISTS grant_ (
    id            TEXT PRIMARY KEY,
    approval_id   TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- request（绑定请求哈希）/ domain（同一主机的 GET）/ service
    host          TEXT NOT NULL,
    service       TEXT,
    action        TEXT,
    constraint_   TEXT,                     -- 授权覆盖的约束值（JSON，值可用通配符）
    request_hash  TEXT,
    scope         TEXT NOT NULL,            -- once / run / session / 1h / forever
    run_id        TEXT,
    session_id    TEXT NOT NULL,
    expires_at    TEXT,
    used_at       TEXT,
    created_at    TEXT NOT NULL,
    revoked_at    TEXT
);

-- 你授权的外部账号。真凭证加密存放；电脑里只有替身令牌
CREATE TABLE IF NOT EXISTS connection (
    id            TEXT PRIMARY KEY,
    service       TEXT NOT NULL UNIQUE,     -- 对应一份服务档案
    kind          TEXT NOT NULL,            -- bearer / oauth2
    surrogate     TEXT NOT NULL UNIQUE,
    secret_enc    BLOB NOT NULL,            -- 加密后的凭证（JSON）
    title         TEXT,
    hosts         TEXT,                     -- 替身令牌的绑定主机（JSON 数组）。空表示沿用仓库档案的主机
    auth          TEXT,                     -- 令牌放在哪（JSON，如 {"header": "Authorization", "scheme": "Bearer"}）
    created_at    TEXT NOT NULL,
    revoked_at    TEXT
);

-- Agent 请你连接一个新账号（第 15.1 节）。真正的连接由你在宿主终端里完成
CREATE TABLE IF NOT EXISTS connection_request (
    id          TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    title       TEXT NOT NULL,
    hosts       TEXT NOT NULL,              -- JSON 数组
    auth        TEXT NOT NULL,              -- JSON
    why         TEXT NOT NULL,              -- Agent 自述，不可信
    session_id  TEXT NOT NULL,
    status      TEXT NOT NULL,              -- PENDING / DONE / DISMISSED
    created_at  TEXT NOT NULL
);

-- Agent 起草、等你审阅的东西：服务档案、Webhook 来源
CREATE TABLE IF NOT EXISTS proposal (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- profile / webhook
    name        TEXT NOT NULL,              -- 服务名或 Webhook 来源名
    content     TEXT NOT NULL,              -- 档案 YAML，或 Webhook 来源的设置（JSON）
    review      TEXT NOT NULL,              -- 宿主生成的审阅文字
    session_id  TEXT NOT NULL,
    status      TEXT NOT NULL,              -- PENDING / APPROVED / REJECTED / SUPERSEDED
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS egress_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    session_id  TEXT,
    method      TEXT NOT NULL,
    url         TEXT NOT NULL,
    verdict     TEXT NOT NULL,              -- allow / deny / ask / bounce（先退回，没发出去）
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inference_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    status      INTEGER NOT NULL,
    request     TEXT NOT NULL,
    response    TEXT NOT NULL,
    tokens      INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# 库的版本号存在 PRAGMA user_version 里。改表结构的办法：在 MIGRATIONS 末尾加一个函数，SCHEMA_VERSION 加一。
# 每个迁移在一个事务里执行，成功后才写版本号；中途失败整个回滚，下次启动从同一个版本重来。
# 比代码新的库（版本号更大）不打开：老代码不认识新结构，硬开可能写坏。

# 版本号出现之前（第 1 步到 U3）的库靠补列升级：这些列缺了就补上。版本 1 的迁移就是做这件事
_ADDED_COLUMNS = [
    ("session", "agent_md", "TEXT"), ("outbox", "attachment", "TEXT"), ("outbox", "meta", "TEXT"),
    ("approval", "service", "TEXT"), ("approval", "action", "TEXT"), ("approval", "constraint_", "TEXT"),
    ("grant_", "service", "TEXT"), ("grant_", "action", "TEXT"), ("grant_", "constraint_", "TEXT"),
    ("session", "session_key", "TEXT"), ("run", "kind", "TEXT NOT NULL DEFAULT 'agent'"),
    ("outbox", "deliver_after", "TEXT"), ("outbox", "requested", "TEXT"),
    ("outbox", "attempts", "INTEGER NOT NULL DEFAULT 0"), ("outbox", "feedback", "TEXT"),
    ("connection", "title", "TEXT"), ("connection", "hosts", "TEXT"), ("connection", "auth", "TEXT"),
    ("connection", "expired_at", "TEXT"),
    ("session", "role", "TEXT"), ("session", "role_md", "TEXT"), ("session", "chain_root", "TEXT"),
    ("session", "hops", "INTEGER NOT NULL DEFAULT 0"), ("session", "chain_fg", "INTEGER NOT NULL DEFAULT 0"),
    ("run", "role", "TEXT"), ("run", "chain_root", "TEXT"),
    # 这次调用模型实际读到的输入有多长（含缓存），宿主侧压缩用它判断会话有多大
    ("inference_log", "context_tokens", "INTEGER"),
]


def _v1_columns(db: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    db.execute("CREATE INDEX IF NOT EXISTS run_chain ON run (chain_root)")


def _v2_host_state(db: sqlite3.Connection) -> None:
    # 宿主自己要跨重启记住的小状态，如保持清醒的截止时间
    db.execute("CREATE TABLE IF NOT EXISTS host_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
               "updated_at TEXT NOT NULL)")


MIGRATIONS = [_v1_columns, _v2_host_state]     # 第 n 个把库从版本 n-1 升到 n
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNew(RuntimeError):
    """库是更新的代码建的。"""


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _migrate(db: sqlite3.Connection) -> None:
    version = schema_version(db)
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(f"数据库版本 {version} 比这份代码认识的 {SCHEMA_VERSION} 新：先更新代码再启动")
    for target in range(version + 1, SCHEMA_VERSION + 1):
        db.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[target - 1](db)
            db.execute(f"PRAGMA user_version = {target}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def get_state(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM host_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM host_state WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO host_state (key, value, updated_at) VALUES (?, ?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                   (key, value, now()))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        # 老版本建出来的是 755 / 644：收紧到只有运行它的用户能读（新建的由启动时的 umask 管）
        path.parent.chmod(0o700)
        for f in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if f.exists():
                f.chmod(0o600)
    db = sqlite3.connect(path, isolation_level=None)  # 自动提交；所有访问都在事件循环线程上
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if schema_version(db) > SCHEMA_VERSION:     # 建表之前就拦下，不往新库里写任何东西
        db.close()
        raise SchemaTooNew(f"{path} 的版本比这份代码认识的 {SCHEMA_VERSION} 新：先更新代码再启动")
    db.executescript(SCHEMA)     # 新库直接建全；已有的表跳过
    _migrate(db)                 # 按版本号补上后来的改动
    return db


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
