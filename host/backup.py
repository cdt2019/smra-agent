"""备份与恢复数据目录。

    uv run python -m host.backup create [-o 文件]     # 服务开着也能做：数据库用 SQLite 的在线备份
    uv run python -m host.backup verify 文件          # 检查一个备份能不能用来恢复
    uv run python -m host.backup restore 文件 [--yes] # 要先停服务（systemctl stop smra-agent）

备份是一个 .tar.gz，里面是数据库的一致快照加数据目录里的其他文件（服务档案、Webhook 来源、
浏览器登录状态、快照、发出的文件……）。不含：
  - 主密钥 master.key：备份里的凭证是加密的，备份和密钥分开放，丢了一份不等于丢了凭证。
    恢复到新机器时要另外带上密钥（systemd 加密凭证、SMRA_MASTER_KEY 或 master.key），否则凭证解不开
  - 数据库的 -wal / -shm（快照已经包含了）、宿主的锁文件
  - microVM 可以重建的系统盘、核心盘和内核（data/vm/rootfs.ext4、core.ext4、vmlinux）；主目录盘 home.ext4 在里面
  - docker 驱动下电脑的主目录在 docker 卷 smra-home 里，不在数据目录，要另外备份（见 docs/deploy.md 的"备份与恢复"）

备份里有对话记录和浏览器的登录状态：文件权限是 600，自己保管好。

恢复把现有数据目录整个改名成 <数据目录>.pre-restore-<时间>（不删），再把备份解到原来的位置；
原来目录里的 master.key 会搬回来。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from .db import SCHEMA_VERSION, schema_version

DB_NAME = "host.db"
LOCK_NAME = "host.lock"
MANIFEST = "smra-backup.json"
# 不进备份的东西（相对数据目录的路径）
_SKIP = {"master.key", DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal", LOCK_NAME,
         ".write-test", "vm/rootfs.ext4", "vm/core.ext4", "vm/vmlinux"}


class BackupError(RuntimeError):
    pass


class HostRunning(BackupError):
    pass


# ------------------------------------------------------------ 宿主锁

class HostLock:
    """宿主运行期间一直拿着数据目录里的 host.lock。拿不到说明另一个宿主（或者恢复）正在用这个目录。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK_NAME
        self._f: IO[bytes] | None = None

    def acquire(self) -> HostLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            f.close()
            raise HostRunning(f"{self.path.parent} 正被另一个宿主进程使用（先停掉它，如 systemctl stop smra-agent）") from None
        self._f = f
        return self

    def release(self) -> None:
        if self._f is None:
            return
        try:
            if os.name != "posix":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        self._f.close()
        self._f = None

    def __enter__(self) -> HostLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# ------------------------------------------------------------ 备份

def _skipped(rel: str) -> bool:
    return rel in _SKIP or rel.endswith(".part")


def _files(data_dir: Path):
    """数据目录里要备份的普通文件和目录（相对路径），符号链接、套接字之类跳过。"""
    for root, dirs, files in os.walk(data_dir):
        base = Path(root)
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
        for name in sorted(dirs):
            yield (base / name).relative_to(data_dir).as_posix(), True
        for name in sorted(files):
            p = base / name
            rel = p.relative_to(data_dir).as_posix()
            if _skipped(rel) or p.is_symlink() or not p.is_file():
                continue
            yield rel, False


def _snapshot_db(src: Path, dst: Path) -> int:
    """在线备份：服务开着也能拿到一致的快照，不会读到写了一半的页。"""
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        return schema_version(target)
    finally:
        target.close()
        source.close()


def default_output(data_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path(data_dir).resolve().parent / "backups" / f"smra-{stamp}.tar.gz"


def create(data_dir: Path, out: Path | None = None) -> Path:
    data_dir = Path(data_dir)
    if not (data_dir / DB_NAME).exists():
        raise BackupError(f"{data_dir / DB_NAME} 不存在：没有可备份的东西")
    out = Path(out) if out else default_output(data_dir)
    if out.resolve().is_relative_to(data_dir.resolve()):
        raise BackupError("备份文件不能放在数据目录里面")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory(prefix="smra-backup-") as tmp:
        snap = Path(tmp) / DB_NAME
        version = _snapshot_db(data_dir / DB_NAME, snap)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                count = 0
                tar.add(snap, arcname=DB_NAME)
                for rel, is_dir in _files(data_dir):
                    tar.add(data_dir / rel, arcname=rel, recursive=False)
                    count += not is_dir
                manifest = {"format": 1, "schema_version": version,
                            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "files": count + 1}
                body = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
                info = tarfile.TarInfo(MANIFEST)
                info.size, info.mode = len(body), 0o600
                tar.addfile(info, io.BytesIO(body))
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    os.replace(part, out)
    return out


# ------------------------------------------------------------ 检查与恢复

def _check_members(tar: tarfile.TarFile) -> dict:
    """只接受普通文件和目录，路径不能跑出数据目录；必须有数据库和说明文件。"""
    names = set()
    for m in tar.getmembers():
        name = m.name
        parts = Path(name).parts
        if name.startswith(("/", "\\")) or ".." in parts or ":" in name or not (m.isfile() or m.isdir()):
            raise BackupError(f"备份里有不该出现的条目：{name!r}")
        if name.split("/")[-1] == "master.key":
            raise BackupError("备份里有 master.key：这不是本工具做的备份")
        names.add(name)
    if DB_NAME not in names or MANIFEST not in names:
        raise BackupError("不是 smra 的备份：缺数据库或说明文件")
    manifest = json.loads(_member(tar, MANIFEST).read())
    if manifest.get("format") != 1:
        raise BackupError(f"不认识的备份格式：{manifest.get('format')}")
    return manifest


def _member(tar: tarfile.TarFile, member: str | tarfile.TarInfo) -> IO[bytes]:
    f = tar.extractfile(member)
    if f is None:
        raise BackupError(f"备份里的 {member} 不是普通文件")
    return f


def _check_db(path: Path) -> int:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"备份里的数据库坏了：{result}")
        version = schema_version(db)
    finally:
        db.close()
    if version > SCHEMA_VERSION:
        raise BackupError(f"备份的数据库版本 {version} 比这份代码认识的 {SCHEMA_VERSION} 新：先更新代码")
    return version


def _extract(tar: tarfile.TarFile, dest: Path) -> None:
    for m in tar.getmembers():
        if m.name == MANIFEST:
            continue
        target = dest / m.name
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f, _member(tar, m) as src:
            shutil.copyfileobj(src, f)


def verify(archive: Path) -> dict:
    """解到临时目录检查一遍，返回说明文件（加上实测的库版本）。"""
    with tarfile.open(archive, "r:gz") as tar, tempfile.TemporaryDirectory(prefix="smra-verify-") as tmp:
        manifest = _check_members(tar)
        _extract(tar, Path(tmp))
        manifest["schema_version"] = _check_db(Path(tmp) / DB_NAME)
    return manifest


def restore(archive: Path, data_dir: Path) -> Path | None:
    """恢复到 data_dir。返回旧数据目录改名后的位置（原来没有就是 None）。宿主在跑时拒绝。"""
    data_dir = Path(data_dir).resolve()
    archive = Path(archive)
    if data_dir.exists():
        with HostLock(data_dir):        # 拿得到锁说明没有宿主在用；马上放掉，Windows 上开着的文件不能改名
            pass
    staging = data_dir.with_name(data_dir.name + ".restoring")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        if os.name == "posix":
            staging.chmod(0o700)
        with tarfile.open(archive, "r:gz") as tar:
            _check_members(tar)
            _extract(tar, staging)
        _check_db(staging / DB_NAME)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    old = None
    if data_dir.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        old = data_dir.with_name(f"{data_dir.name}.pre-restore-{stamp}")
        os.replace(data_dir, old)
        if (old / "master.key").exists():
            shutil.copy2(old / "master.key", staging / "master.key")
    os.replace(staging, data_dir)
    return old


def _key_note(data_dir: Path) -> str:
    """恢复后看一眼凭证能不能解开。只报告，不改任何东西，也不生成新密钥。"""
    db = sqlite3.connect(f"file:{(data_dir / DB_NAME).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT secret_enc FROM connection WHERE revoked_at IS NULL LIMIT 1").fetchone()
    finally:
        db.close()
    if row is None:
        return "备份里没有已连接的账号，不需要主密钥。"
    if not (os.environ.get("CREDENTIALS_DIRECTORY") or os.environ.get("SMRA_MASTER_KEY")
            or os.environ.get("SMRA_MASTER_KEY_CRED") or (data_dir / "master.key").exists()):
        return "备份里有已连接的账号，但这里找不到主密钥：启动前把原来的主密钥配好，否则宿主拒绝启动。"
    try:
        from cryptography.fernet import Fernet

        from .creds import load_key
        Fernet(load_key(data_dir)).decrypt(row[0])
        return "主密钥能解开备份里的凭证。"
    except Exception as e:
        return f"注意：当前的主密钥解不开备份里的凭证（{type(e).__name__}）。确认用的是做备份那台机器的主密钥。"


def main(argv: list[str] | None = None) -> int:
    from .config import Config
    parser = argparse.ArgumentParser(description="备份与恢复数据目录（不含主密钥）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create", help="做一份备份；服务开着也行")
    p.add_argument("-o", "--output", type=Path, help="备份文件；默认 <数据目录旁边>/backups/smra-<时间>.tar.gz")
    p = sub.add_parser("verify", help="检查备份")
    p.add_argument("archive", type=Path)
    p = sub.add_parser("restore", help="从备份恢复；要先停服务")
    p.add_argument("archive", type=Path)
    p.add_argument("--yes", action="store_true", help="不再确认")
    args = parser.parse_args(argv)
    if os.name == "posix":
        os.umask(0o077)
    cfg = Config.from_env()
    try:
        if args.cmd == "create":
            out = create(cfg.data_dir, args.output)
            print(f"备份好了：{out}（{out.stat().st_size // 1024} KB，不含主密钥）")
        elif args.cmd == "verify":
            m = verify(args.archive)
            print(f"可以用来恢复：{m['created_at']} 做的，{m['files']} 个文件，库版本 {m['schema_version']}")
        else:
            m = verify(args.archive)
            print(f"要恢复的备份：{m['created_at']} 做的，{m['files']} 个文件，库版本 {m['schema_version']}")
            print(f"现有的 {cfg.data_dir} 会改名留着，不删除。")
            if not args.yes and input("继续？输入 yes：").strip() != "yes":
                print("没有恢复")
                return 1
            old = restore(args.archive, cfg.data_dir)
            if old:
                print(f"原来的数据目录在 {old}")
            print("恢复好了。" + _key_note(cfg.data_dir))
    except (BackupError, tarfile.TarError, OSError, sqlite3.Error) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
