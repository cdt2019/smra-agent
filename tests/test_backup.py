"""R6：数据库版本号、备份与恢复、宿主锁。全部离线。"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile

import pytest

from host import backup
from host import db as dbmod
from host.db import SCHEMA_VERSION, SchemaTooNew, connect, get_state, schema_version, set_state


def test_new_database_gets_current_version(tmp_path):
    db = connect(tmp_path / "host.db")
    assert schema_version(db) == SCHEMA_VERSION
    set_state(db, "k", "v")
    assert get_state(db, "k") == "v"
    set_state(db, "k", None)
    assert get_state(db, "k") is None
    db.close()


def test_unversioned_database_is_migrated(tmp_path):
    path = tmp_path / "host.db"
    old = sqlite3.connect(path)       # 版本号出现之前的库：没有后来补的列，user_version 是 0
    old.execute("CREATE TABLE run (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trigger_key TEXT NOT NULL, "
                "outcome TEXT, error TEXT, tokens_used INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
                "finished_at TEXT)")
    old.execute("INSERT INTO run (id, session_id, trigger_key, started_at) VALUES ('r1', 's1', 't', 'x')")
    old.commit()
    old.close()
    db = connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(run)")}
    assert {"kind", "role", "chain_root"} <= cols
    assert db.execute("SELECT kind FROM run WHERE id = 'r1'").fetchone()[0] == "agent"
    assert schema_version(db) == SCHEMA_VERSION
    db.close()
    connect(path).close()       # 再开一次什么都不做


def test_newer_database_is_refused_untouched(tmp_path):
    path = tmp_path / "host.db"
    new = sqlite3.connect(path)
    new.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    new.close()
    with pytest.raises(SchemaTooNew):
        connect(path)
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    check.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "host.db"
    connect(path).close()

    def broken(db):
        db.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("迁移写到一半失败")

    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, broken])
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError):
        connect(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert check.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    check.close()


def _populated(data):
    db = connect(data / "host.db")
    db.execute("INSERT INTO session (id, kind, status, created_at, last_event_at) "
               "VALUES ('s1', 'conversation', 'OPEN', 'a', 'a')")
    (data / "master.key").write_bytes(b"secret-key-do-not-back-up")
    (data / "profiles").mkdir()
    (data / "profiles" / "notes.yaml").write_text("name: notes\n", encoding="utf-8")
    (data / "browser").mkdir()      # 空目录也要带上
    return db


def test_backup_while_running_excludes_master_key(tmp_path):
    data = tmp_path / "data"
    db = _populated(data)           # 连接开着，WAL 里有还没合并的写入
    if os.name == "posix":
        (data / "link").symlink_to(data / "master.key")
    out = backup.create(data, tmp_path / "b" / "one.tar.gz")
    db.close()
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
    assert {"host.db", "smra-backup.json", "profiles/notes.yaml", "browser"} <= names
    assert not any(n.endswith(("master.key", "-wal", "-shm", "link", ".part")) for n in names)
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    manifest = backup.verify(out)
    assert manifest["schema_version"] == SCHEMA_VERSION
    with pytest.raises(backup.BackupError):
        backup.create(data, data / "inside.tar.gz")


def test_restore_replaces_data_and_keeps_old(tmp_path):
    data = tmp_path / "data"
    _populated(data).close()
    out = backup.create(data, tmp_path / "one.tar.gz")
    db = connect(data / "host.db")
    db.execute("DELETE FROM session")
    db.close()
    (data / "profiles" / "notes.yaml").unlink()

    with backup.HostLock(data):         # 宿主在跑：拒绝
        with pytest.raises(backup.HostRunning):
            backup.restore(out, data)
    assert not (data / "profiles" / "notes.yaml").exists()

    old = backup.restore(out, data)
    assert old is not None and old.exists() and old.name.startswith("data.pre-restore-")
    assert (data / "profiles" / "notes.yaml").read_text(encoding="utf-8") == "name: notes\n"
    assert (data / "master.key").read_bytes() == b"secret-key-do-not-back-up"     # 本机的密钥搬回来了
    db = connect(data / "host.db")
    assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
    db.close()
    assert backup._key_note(data).startswith("备份里没有已连接的账号")


def test_restore_into_empty_place(tmp_path):
    data = tmp_path / "data"
    _populated(data).close()
    out = backup.create(data, tmp_path / "one.tar.gz")
    fresh = tmp_path / "elsewhere" / "data"
    assert backup.restore(out, fresh) is None
    assert not (fresh / "master.key").exists()
    assert (fresh / "host.db").exists()


def _archive(path, members: dict[str, bytes], manifest=True):
    with tarfile.open(path, "w:gz") as tar:
        if manifest:
            members = {"smra-backup.json": json.dumps({"format": 1}).encode(), **members}
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))


@pytest.mark.parametrize("members,manifest", [
    ({"host.db": b"", "../evil": b"x"}, True),
    ({"host.db": b"", "/abs": b"x"}, True),
    ({"host.db": b"", "master.key": b"x"}, True),
    ({"host.db": b""}, False),
    ({"host.db": b"not a database"}, True),
])
def test_bad_archives_are_refused(tmp_path, members, manifest):
    bad = tmp_path / "bad.tar.gz"
    _archive(bad, members, manifest)
    data = tmp_path / "data"
    _populated(data).close()
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.restore(bad, data)
    assert (data / "profiles" / "notes.yaml").exists()       # 原数据没动
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path / "data.restoring").exists()


def test_host_lock_is_exclusive(tmp_path):
    first = backup.HostLock(tmp_path).acquire()
    with pytest.raises(backup.HostRunning):
        backup.HostLock(tmp_path).acquire()
    first.release()
    backup.HostLock(tmp_path).acquire().release()
