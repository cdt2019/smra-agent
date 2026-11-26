"""数据保留期限和主密钥的来源。"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from host import retention, sessions
from host.creds import CredentialStore, MasterKeyError, load_key
from host.db import connect


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def count(db, sql, *params) -> int:
    return db.execute(sql, params).fetchone()[0]


def old_session(db, at: datetime, text: str) -> str:
    sid = sessions.new_session(db, "conversation")
    sessions.append(db, sid, None, "host", "user_message", {"role": "user", "content": text})
    db.execute("UPDATE session SET status = 'CLOSED', last_event_at = ? WHERE id = ?", (iso(at), sid))
    return sid


def test_purge_old_data(make_app):
    app = make_app()
    db, now = app.db, datetime.now(UTC)
    long_ago, recently = now - timedelta(days=400), now - timedelta(days=3)

    db.execute("INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
               "VALUES ('r1', 200, '{\"邮件\": \"正文\"}', 'reply', 120, ?), ('r2', 200, 'req', 'resp', 80, ?)",
               (iso(now - timedelta(days=20)), iso(recently)))
    gone = old_session(db, long_ago, "很久以前的对话")
    waiting = old_session(db, long_ago, "还有跟进要回来")
    app.timers.create(kind="follow_up", name="回来看看", payload={"task": "x"}, created_by="agent", at=now,
                      due_at=now + timedelta(days=2), session_id=waiting)
    fresh = old_session(db, recently, "前几天的对话")
    for i, (status, at) in enumerate([("DONE", long_ago), ("PENDING", long_ago), ("DONE", recently)]):
        db.execute("INSERT INTO inbox (id, source, source_id, priority, payload, status, created_at) "
                   "VALUES (?, 'user', ?, 1, '{}', ?, ?)", (f"in{i}", f"s{i}", status, iso(at)))
    for i, status in enumerate(["SENT", "PENDING"]):
        db.execute("INSERT INTO outbox (id, kind, body, dedupe_key, status, created_at) VALUES (?, 'reply', 'b', ?, ?, ?)",
                   (f"ob{i}", f"d{i}", status, iso(long_ago)))
    for i in range(2):
        db.execute("INSERT INTO approval (id, session_id, run_id, request_hash, category, method, host, url, summary, "
                   "scopes, status, created_at) VALUES (?, 's', 'r', 'h', 'web_write', 'POST', 'a.example', "
                   "'https://a.example/', '发邮件给张三', '[]', 'GRANTED', ?)", (f"ap{i}", iso(long_ago)))
    db.execute("INSERT INTO grant_ (id, approval_id, kind, host, scope, session_id, created_at) "
               "VALUES ('g1', 'ap1', 'domain', 'a.example', 'forever', 's', ?)", (iso(long_ago),))
    db.execute("INSERT INTO egress_log (method, url, verdict, reason, created_at) VALUES "
               "('GET', 'https://a.example/', 'allow', 'x', ?), ('GET', 'https://b.example/', 'allow', 'x', ?)",
               (iso(long_ago), iso(recently)))
    uploads = app.cfg.data_dir / "uploads"
    outgoing = app.cfg.data_dir / "outgoing"
    uploads.mkdir(parents=True)
    outgoing.mkdir(parents=True)
    old_file, new_file, pending_file = uploads / "old.jpg", uploads / "new.jpg", outgoing / "report.pdf"
    for f in (old_file, new_file, pending_file):
        f.write_bytes(b"x")
    stale = time.time() - 40 * 86400
    os.utime(old_file, (stale, stale))
    os.utime(pending_file, (stale, stale))
    db.execute("INSERT INTO outbox (id, kind, body, attachment, dedupe_key, created_at) "
               "VALUES ('ob9', 'reply', '报告', ?, 'd9', ?)", (str(pending_file), iso(now)))

    counts = retention.purge(db, app.cfg, now)

    rows = db.execute("SELECT run_id, request, response, tokens FROM inference_log ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("r1", "", "", 120), ("r2", "req", "resp", 80)]   # 花费留着
    assert count(db, "SELECT COUNT(*) FROM event WHERE session_id = ?", gone) == 0
    assert count(db, "SELECT COUNT(*) FROM event_text WHERE session_id = ?", gone) == 0
    assert sessions.search(db, "很久以前") == [] and sessions.search(db, "前几天")
    assert count(db, "SELECT COUNT(*) FROM event WHERE session_id = ?", waiting) == 1
    assert count(db, "SELECT COUNT(*) FROM event WHERE session_id = ?", fresh) == 1
    assert {r[0] for r in db.execute("SELECT id FROM inbox")} == {"in1", "in2"}
    assert {r[0] for r in db.execute("SELECT id FROM outbox")} == {"ob1", "ob9"}
    assert {r[0] for r in db.execute("SELECT id FROM approval")} == {"ap1"}       # 授权还指着它
    assert [r[0] for r in db.execute("SELECT url FROM egress_log")] == ["https://b.example/"]
    assert not old_file.exists() and new_file.exists() and pending_file.exists()
    assert counts["模型请求日志"] == 1 and counts["文件"] == 1

    assert retention.purge(db, app.cfg, now) == {}           # 再清一次没有东西


def test_zero_means_keep_forever(make_app):
    app = make_app(retain_days=0, retain_inference_days=0, retain_file_days=0)
    long_ago = datetime.now(UTC) - timedelta(days=4000)
    app.db.execute("INSERT INTO inference_log (run_id, status, request, response, tokens, created_at) "
                   "VALUES ('r', 200, 'req', 'resp', 1, ?)", (iso(long_ago),))
    old_session(app.db, long_ago, "老对话")
    assert retention.purge(app.db, app.cfg) == {}
    assert sessions.search(app.db, "老对话")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("SMRA_MASTER_KEY", "SMRA_MASTER_KEY_CRED", "CREDENTIALS_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_master_key_from_systemd_credential(tmp_path, clean_env):
    data = tmp_path / "data"
    first = load_key(data)                                   # 新装：生成 master.key
    assert (data / "master.key").read_bytes().strip() == first

    creds = tmp_path / "creds"
    creds.mkdir()
    key = Fernet.generate_key()
    (creds / "smra-master-key").write_bytes(key + b"\n")
    clean_env.setenv("CREDENTIALS_DIRECTORY", str(creds))
    clean_env.setenv("SMRA_MASTER_KEY", "ignored")
    assert load_key(data) == key                             # systemd 给的优先


def test_master_key_is_never_regenerated_over_existing_secrets(tmp_path, clean_env):
    data = tmp_path / "data"
    store = CredentialStore(connect(data / "host.db"), load_key(data))
    store.add("tickets", {"token": "real"}, "bearer", title="工单", hosts=["api.example"])
    (data / "master.key").rename(tmp_path / "moved.key")     # 挪走了，却没配好凭证
    with pytest.raises(MasterKeyError, match="不会生成新密钥"):
        load_key(data)
    assert not (data / "master.key").exists()

    clean_env.setenv("SMRA_MASTER_KEY_CRED", str(tmp_path / "missing.cred"))
    with pytest.raises(MasterKeyError, match="解不开主密钥凭证"):
        load_key(data)

    clean_env.delenv("SMRA_MASTER_KEY_CRED")
    clean_env.setenv("SMRA_MASTER_KEY", (tmp_path / "moved.key").read_text().strip())
    assert CredentialStore(connect(data / "host.db"), load_key(data)).get("tickets")
