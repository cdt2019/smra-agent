"""凭证服务。设计文档第 12 节。

真凭证加密存在宿主的数据库里，只在出网代理把请求发往该服务的绑定主机时取出来换上。
电脑里只有替身令牌 sur_<服务>_<随机串>，发到别处就是一串无用的字符。

命令行（在宿主上运行，凭证从终端输入，不经过聊天）：
    uv run python -m host.creds add <服务>            # 静态令牌（API key、个人访问令牌）
    uv run python -m host.creds add <服务> --oauth    # OAuth2：client id/secret + refresh token
    uv run python -m host.creds add gmail --google-login   # 没有 refresh token：在浏览器里授权一次，命令自己换
    uv run python -m host.creds list
    uv run python -m host.creds remove <服务>
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import builtins
import getpass
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

from .db import new_id, now

log = logging.getLogger("creds")

_REFRESH_MARGIN_S = 120


CRED_NAME = "smra-master-key"


class MasterKeyError(RuntimeError):
    """找不到能用的主密钥。宁可不启动，也不生成新密钥把已有的凭证变成解不开的乱码。"""


def _has_secrets(data_dir: Path) -> bool:
    db_path = data_dir / "host.db"
    if not db_path.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            return db.execute("SELECT 1 FROM connection LIMIT 1").fetchone() is not None
        finally:
            db.close()
    except sqlite3.Error:
        return False


def load_key(data_dir: Path) -> bytes:
    """主密钥，按这个顺序找：

    1. systemd 加密凭证（LoadCredentialEncrypted=smra-master-key:…）：服务运行时解密到只有它看得到的
       内存目录，磁盘上存的是用本机密钥加密过的文件，不在数据目录里，不会跟着数据目录被拷走或备份。
    2. 环境变量 SMRA_MASTER_KEY。
    3. SMRA_MASTER_KEY_CRED 指向的加密凭证文件：在宿主终端里跑连接命令时用（要 root，调 systemd-creds 解密）。
    4. 数据目录里的 master.key（老办法；第一次启动时自动生成）。

    库里已经有加密的凭证、却一个密钥都找不到时直接报错，不生成新的。
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and (Path(cred_dir) / CRED_NAME).is_file():
        return (Path(cred_dir) / CRED_NAME).read_bytes().strip()
    env = os.environ.get("SMRA_MASTER_KEY")
    if env:
        return env.encode()
    store = os.environ.get("SMRA_MASTER_KEY_CRED")
    if store:
        import subprocess
        try:
            out = subprocess.run(["systemd-creds", "decrypt", f"--name={CRED_NAME}", store, "-"],
                                 capture_output=True, check=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            raise MasterKeyError(f"解不开主密钥凭证 {store}（要用 root 运行）：{type(e).__name__}") from None
        if out:
            return out
    path = data_dir / "master.key"
    if not path.exists():
        if _has_secrets(data_dir):
            raise MasterKeyError("数据库里有加密的凭证，但找不到主密钥（systemd 凭证、SMRA_MASTER_KEY、"
                            "SMRA_MASTER_KEY_CRED、data/master.key 都没有）。不会生成新密钥，否则已有的凭证都解不开")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.warning("生成了新的主密钥 %s。正式部署建议改用 systemd 加密凭证，见 docs/security.md", path)
    return path.read_bytes().strip()


@dataclass
class Connection:
    service: str
    kind: str
    surrogate: str
    title: str | None = None
    hosts: list[str] | None = None      # 替身令牌的绑定主机；None 表示沿用仓库档案的主机
    auth: dict | None = None
    expired_at: str | None = None       # 刷新被拒的时刻：授权失效了，要你重连


_COLUMNS = "service, kind, surrogate, title, hosts, auth, expired_at"


def _conn(row) -> Connection:
    return Connection(row["service"], row["kind"], row["surrogate"], row["title"],
                      json.loads(row["hosts"]) if row["hosts"] else None,
                      json.loads(row["auth"]) if row["auth"] else None, row["expired_at"])


class CredentialExpired(Exception):
    """OAuth 的 refresh token 被拒：授权被撤销，或者过期了（Google 测试状态的应用 7 天就过期）。
    重试没有用，只能你重新连接。"""

    def __init__(self, service: str):
        super().__init__(f"{service} 的授权已失效，已提醒用户重新连接；在那之前这个服务用不了，不要重试")
        self.service = service


# 令牌端点说"这个授权不能用了"的错误码。其余错误（网络、5xx、限流）可能是暂时的，下次照常再试。
_DEAD_GRANT = {"invalid_grant", "unauthorized_client", "invalid_client"}


def reconnect_command(service: str, secret: dict) -> str:
    if "google" in secret.get("token_url", ""):
        return f"uv run python -m host.creds add {service} --google-login"
    return f"uv run python -m host.creds add {service} --oauth"


class CredentialStore:
    def __init__(self, db: sqlite3.Connection, key: bytes, http_factory=None):
        self.db = db
        self.fernet = Fernet(key)
        self._http_factory = http_factory           # 返回宿主自己的 aiohttp 会话（不经出网代理）
        self._access: dict[str, tuple[str, float]] = {}   # OAuth 访问令牌缓存：服务 -> (令牌, 过期时间)
        self._locks: dict[str, asyncio.Lock] = {}
        # 授权失效时调用一次：on_expired(连接, 重连命令)。宿主用它给你发提醒。
        self.on_expired: Callable[[Connection, str], None] | None = None

    # ------------------------------------------------------------ 管理

    def add(self, service: str, secret: dict, kind: str = "bearer", *, title: str | None = None,
            hosts: list[str] | None = None, auth: dict | None = None) -> str:
        """保存凭证，返回替身令牌。同名服务会被替换（替身令牌也换新的）。

        hosts 是替身令牌的绑定范围：只有发往这些主机的请求，替身才会被换成真凭证。
        """
        surrogate = f"sur_{service}_{secrets.token_hex(16)}"
        blob = self.fernet.encrypt(json.dumps(secret).encode())
        self.db.execute("DELETE FROM connection WHERE service = ?", (service,))
        self.db.execute(
            "INSERT INTO connection (id, service, kind, surrogate, secret_enc, title, hosts, auth, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("con"), service, kind, surrogate, blob, title,
             json.dumps([h.lower() for h in hosts]) if hosts else None,
             json.dumps(auth) if auth else None, now()),
        )
        self._access.pop(service, None)
        return surrogate

    def remove(self, service: str) -> bool:
        self._access.pop(service, None)
        return bool(self.db.execute("DELETE FROM connection WHERE service = ?", (service,)).rowcount)

    def list(self) -> list[Connection]:
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM connection WHERE revoked_at IS NULL ORDER BY service"
        ).fetchall()
        return [_conn(r) for r in rows]

    def get(self, service: str) -> Connection | None:
        row = self.db.execute(
            f"SELECT {_COLUMNS} FROM connection WHERE service = ? AND revoked_at IS NULL", (service,)
        ).fetchone()
        return _conn(row) if row else None

    def by_surrogate(self, surrogate: str) -> Connection | None:
        row = self.db.execute(
            f"SELECT {_COLUMNS} FROM connection WHERE surrogate = ? AND revoked_at IS NULL", (surrogate,),
        ).fetchone()
        return _conn(row) if row else None

    def _secret(self, service: str) -> dict:
        row = self.db.execute("SELECT secret_enc FROM connection WHERE service = ?", (service,)).fetchone()
        if row is None:
            raise KeyError(service)
        try:
            return json.loads(self.fernet.decrypt(row["secret_enc"]))
        except InvalidToken:
            raise RuntimeError(f"{service} 的凭证解不开：主密钥变了？") from None

    # ------------------------------------------------------------ 取用（只有出网代理调用）

    async def real_token(self, conn: Connection) -> str:
        """换上去的真令牌。OAuth2 的访问令牌在宿主侧刷新，电脑不参与。"""
        secret = self._secret(conn.service)
        if conn.kind == "bearer":
            return secret["token"]
        lock = self._locks.setdefault(conn.service, asyncio.Lock())
        async with lock:
            cached = self._access.get(conn.service)
            if cached and cached[1] - _REFRESH_MARGIN_S > time.time():
                return cached[0]
            if self._expired(conn.surrogate):
                raise CredentialExpired(conn.service)      # 已经知道失效了，不再去敲令牌端点
            try:
                token, ttl = await self._refresh(secret)
            except CredentialExpired:
                self._mark_expired(conn, secret)
                raise CredentialExpired(conn.service) from None
            self._access[conn.service] = (token, time.time() + ttl)
            return token

    async def _refresh(self, secret: dict) -> tuple[str, int]:
        form = {"grant_type": "refresh_token", "refresh_token": secret["refresh_token"],
                "client_id": secret["client_id"], "client_secret": secret["client_secret"]}
        http = self._http_factory()
        async with http.post(secret["token_url"], data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or "access_token" not in data:
                error = data.get("error") if isinstance(data, dict) else None
                if r.status in (400, 401) and error in _DEAD_GRANT:
                    raise CredentialExpired("")
                raise RuntimeError(f"刷新访问令牌失败：HTTP {r.status} {str(data)[:200]}")
        return data["access_token"], int(data.get("expires_in", 3600))

    def _expired(self, surrogate: str) -> bool:
        row = self.db.execute("SELECT expired_at FROM connection WHERE surrogate = ?", (surrogate,)).fetchone()
        return bool(row and row["expired_at"])

    def _mark_expired(self, conn: Connection, secret: dict) -> None:
        # 按替身令牌认行：你重连后是新的一行，旧的失效标记不会落到新连接上
        cur = self.db.execute("UPDATE connection SET expired_at = ? WHERE surrogate = ? AND expired_at IS NULL",
                              (now(), conn.surrogate))
        self._access.pop(conn.service, None)
        log.warning("%s 的授权已失效，需要重新连接", conn.service)
        if cur.rowcount and self.on_expired:
            try:
                self.on_expired(conn, reconnect_command(conn.service, secret))
            except Exception:
                log.exception("发送授权失效提醒失败")

    def secret_values(self, service: str) -> builtins.list[str]:
        """这个服务所有不能回流到电脑的值：令牌、刷新令牌、client secret、当前的访问令牌。"""
        try:
            secret = self._secret(service)
        except KeyError:
            return []
        values = [v for k, v in secret.items() if k in ("token", "refresh_token", "client_secret") and v]
        cached = self._access.get(service)
        if cached:
            values.append(cached[0])
        return [v for v in values if len(v) >= 8]


# ---------------------------------------------------------------- 连接请求


def describe_auth(auth: dict) -> str:
    if "query" in auth:
        return f"查询参数 {auth['query']}=<令牌>"
    scheme = f"{auth['scheme']} " if auth.get("scheme") else ""
    return f"请求头 {auth['header']}: {scheme}<令牌>"


def pending_request(db: sqlite3.Connection, request_id: str):
    return db.execute("SELECT * FROM connection_request WHERE id = ? AND status = 'PENDING'",
                      (request_id,)).fetchone()


def fulfil_request(db: sqlite3.Connection, store: CredentialStore, request_id: str,
                   secret: dict, kind: str = "bearer") -> str:
    """按 Agent 的连接请求保存你给的凭证，并在发起请求的会话里放一条事件，让 Agent 接着做。

    绑定主机和认证方式取自请求本身（你在命令行里确认过），不取自之后的任何东西。
    """
    from .inbox import Inbox

    req = pending_request(db, request_id)
    if req is None:
        raise KeyError(f"没有待处理的连接请求 {request_id}")
    hosts, auth = json.loads(req["hosts"]), json.loads(req["auth"])
    surrogate = store.add(req["service"], secret, kind, title=req["title"], hosts=hosts, auth=auth)
    db.execute("UPDATE connection_request SET status = 'DONE' WHERE id = ?", (request_id,))
    Inbox(db).add("connection", request_id, {
        "text": f"[连接] 用户已连接 {req['title']}（{req['service']}），令牌只发往：{', '.join(hosts)}。"
                f"用法见 <memory> 里的已连接服务。还没有服务档案：GET 直接放行，其余请求每次都要用户批准。",
        "at": now()}, session_id=req["session_id"])
    return surrogate


# ---------------------------------------------------------------- 命令行


# ------------------------------------------------------------ Google 授权（拿 refresh token）
#
# 宿主多半是一台没有浏览器的服务器，所以不在宿主上开回调端口：授权页跳回的 127.0.0.1 地址在你的电脑上打不开，
# 没关系，网址里已经带着授权码，你把整个网址粘回终端，命令拿它去换 refresh token。

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_REDIRECT = "http://127.0.0.1:53682/"
# 读、发、存草稿、改标签、移到垃圾箱；不含永久删除（那要整个邮箱的权限）
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# 仓库自带档案的 Google 服务各自申请的权限。每个服务单独授权、单独一个 refresh token：
# 断开或失效互不影响，一个令牌泄露也只够用那一个服务。
GOOGLE_SCOPES = {
    "gmail": GMAIL_SCOPE,
    # 读写日程；日历本身只读（看有哪些日历，不能建、删日历）
    "gcalendar": "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly",
    # 整个云盘读写。drive.file 只能碰本应用建的文件，读不了你已有的文档
    "gdrive": "https://www.googleapis.com/auth/drive",
}


class LoginError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def google_login_url(client_id: str, scope: str, state: str, challenge: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": GOOGLE_REDIRECT, "response_type": "code", "scope": scope,
        "access_type": "offline", "prompt": "consent",      # 每次都要 refresh token
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def code_from_redirect(pasted: str, state: str) -> str:
    """从你粘回来的网址里取授权码。state 对不上说明不是这次发起的授权，不收。"""
    query = parse_qs(urlsplit(pasted.strip()).query)
    if "error" in query:
        raise LoginError(f"Google 没有授权：{query['error'][0]}")
    if query.get("state", [""])[0] != state:
        raise LoginError("网址不是这次授权跳回来的（state 对不上），请重新运行命令")
    code = query.get("code", [""])[0]
    if not code:
        raise LoginError("网址里没有授权码。要粘的是浏览器跳转后地址栏里的完整网址（以 http://127.0.0.1 开头）")
    return code


def exchange_code(token_url: str, client_id: str, client_secret: str, code: str, verifier: str) -> str:
    form = urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": GOOGLE_REDIRECT,
                      "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier}).encode()
    req = urllib.request.Request(token_url, data=form,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error_description") or e.reason
        except ValueError:
            detail = e.reason
        raise LoginError(f"换 refresh token 失败（HTTP {e.code}）：{detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise LoginError(f"连不上 {token_url}：{e}") from None
    if not data.get("refresh_token"):
        raise LoginError("Google 没有给 refresh token。到 https://myaccount.google.com/permissions "
                         "移除这个应用的授权后重新运行命令")
    return data["refresh_token"]


def _google_login(client_id: str, client_secret: str, token_url: str, scope: str) -> str:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    print("\n1. 在你自己电脑的浏览器里打开下面这个链接，登录要连接的 Google 账号并同意授权：\n")
    print(google_login_url(client_id, scope, state, challenge))
    print("\n2. 同意后浏览器会跳到一个打不开的 http://127.0.0.1:53682/... 页面，这是正常的。")
    print("   把地址栏里的完整网址复制下来，粘到这里（不回显），回车。授权码几分钟内有效。\n")
    pasted = getpass.getpass("跳转后的网址: ")
    return exchange_code(token_url, client_id, client_secret, code_from_redirect(pasted, state), verifier)


def _cli() -> None:
    from .config import ROOT, Config
    from .db import connect
    from .egress.services import load_profiles

    parser = argparse.ArgumentParser(prog="python -m host.creds", description="管理已连接的外部账号")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="连接一个服务（凭证从终端输入，不回显）")
    add.add_argument("service")
    add.add_argument("--request", help="按 Agent 的连接请求（请求号见它发给你的消息）")
    add.add_argument("--hosts", help="令牌只发往这些主机，逗号分隔（没有档案、也没有请求时必填）")
    add.add_argument("--header", default="Authorization")
    add.add_argument("--scheme", default="Bearer", help="空字符串表示请求头里只放令牌本身")
    add.add_argument("--oauth", action="store_true", help="OAuth2 刷新令牌方式")
    add.add_argument("--token-url", default="https://oauth2.googleapis.com/token")
    add.add_argument("--google-login", action="store_true",
                     help="没有 refresh token 时用：在浏览器里授权一次，命令自己去换（隐含 --oauth）")
    add.add_argument("--scope", help="--google-login 申请的权限。gmail、gcalendar、gdrive 有默认值，其他服务必填")
    sub.add_parser("list", help="列出已连接的服务")
    sub.add_parser("requests", help="列出 Agent 待处理的连接请求")
    rm = sub.add_parser("remove", help="断开一个服务")
    rm.add_argument("service")
    args = parser.parse_args()

    cfg = Config.from_env()
    db = connect(cfg.db_path)
    store = CredentialStore(db, load_key(cfg.data_dir))
    if args.cmd == "list":
        for c in store.list():
            where = ", ".join(c.hosts) if c.hosts else "（按档案）"
            state = f"\t授权已失效（{c.expired_at[:16]}），要重新连接" if c.expired_at else ""
            print(f"{c.service}\t{c.kind}\t主机 {where}\t替身令牌 {c.surrogate[:20]}…{state}")
        return
    if args.cmd == "requests":
        for r in db.execute("SELECT * FROM connection_request WHERE status = 'PENDING' ORDER BY created_at"):
            print(f"{r['id']}\t{r['service']}\t{', '.join(json.loads(r['hosts']))}\t{r['why'][:60]}")
        return
    if args.cmd == "remove":
        print("已断开" if store.remove(args.service) else "没有这个服务")
        return

    req = pending_request(db, args.request) if args.request else None
    if args.request and (req is None or req["service"] != args.service):
        sys.exit(f"没有服务 {args.service} 的待处理请求 {args.request}")
    if req is not None:
        hosts, auth = json.loads(req["hosts"]), json.loads(req["auth"])
    elif args.hosts:
        hosts = [h.strip().lower() for h in args.hosts.split(",") if h.strip()]
        auth = {"header": args.header, "scheme": args.scheme} if args.scheme else {"header": args.header}
    elif args.service in load_profiles(ROOT / "profiles", cfg.data_dir / "profiles"):
        hosts, auth = None, None        # 仓库或你自己的档案里写了主机
    else:
        sys.exit("这个服务没有档案，要用 --hosts 指定令牌只发往哪些主机，或者用 --request")
    if hosts:
        print(f"令牌只会被发往：{', '.join(hosts)}")
        print(f"认证方式：{describe_auth(auth)}")
        if input("确认这些主机？[y/N] ").strip().lower() != "y":
            sys.exit("没有保存")

    scope = args.scope or GOOGLE_SCOPES.get(args.service)
    if args.google_login and not scope:
        sys.exit(f"{args.service} 没有默认的 Google 权限，要用 --scope 指定")

    if args.oauth or args.google_login:
        # 重连（授权失效后）：client id 和 secret 没变，回车就沿用已保存的。
        # 新连一个 Google 服务时，沿用已经连着的另一个 Google 服务的（同一个 Google Cloud 项目）
        current = store.get(args.service)
        old = store._secret(args.service) if current is not None and current.kind == "oauth2" else {}
        hint = "（回车沿用已保存的）" if old.get("client_id") else ""
        if not old and args.google_login:
            other = next((c.service for c in store.list() if c.kind == "oauth2" and c.service != args.service
                          and store._secret(c.service).get("token_url") == args.token_url), None)
            if other:
                old = store._secret(other)
                hint = f"（回车沿用 {other} 的）"
        client_id = input(f"client id{hint}: ").strip() or old.get("client_id", "")
        client_secret = (getpass.getpass(f"client secret（不回显）{hint}: ").strip()
                         or old.get("client_secret", ""))
        if args.google_login:
            if not (client_id and client_secret):
                sys.exit("client id 和 client secret 都要填，没有保存")
            try:
                refresh_token = _google_login(client_id, client_secret, args.token_url, scope or "")
            except LoginError as e:
                sys.exit(f"{e}。没有保存")
            print("已拿到 refresh token。")
        else:
            refresh_token = getpass.getpass("refresh token（不回显）: ").strip()
        secret = {"client_id": client_id, "client_secret": client_secret,
                  "refresh_token": refresh_token, "token_url": args.token_url}
        kind = "oauth2"
    else:
        secret = {"token": getpass.getpass("令牌（不回显）: ").strip()}
        kind = "bearer"
    if not all(secret.values()):
        sys.exit("有字段是空的，没有保存")
    if req is not None:
        fulfil_request(db, store, req["id"], secret, kind)
        print(f"已连接 {args.service}。Agent 会在原来的对话里接着做（宿主几秒内会注意到）。")
    else:
        store.add(args.service, secret, kind, hosts=hosts, auth=auth)
        print(f"已连接 {args.service}，下一次运行起可用。电脑里只会看到替身令牌。")


if __name__ == "__main__":
    _cli()
