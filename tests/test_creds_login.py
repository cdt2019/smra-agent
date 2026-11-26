"""连接命令行的 Google 授权：在浏览器里授权一次，把跳转后的网址粘回来，命令自己换 refresh token。

Google 换成本机的一个假令牌端点；终端输入用 monkeypatch 代替。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from host import creds
from host.creds import GOOGLE_REDIRECT, CredentialStore, LoginError, code_from_redirect, load_key
from host.db import connect


class FakeGoogle:
    """假的令牌端点：只认这次发出的授权码，并按 PKCE 校验 code_verifier。"""

    def __init__(self) -> None:
        self.challenge = ""
        self.forms: list[dict] = []
        self.give_refresh = True
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                form = {k: v[0] for k, v in parse_qs(body).items()}
                outer.forms.append(form)
                digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                ok = (form.get("code") == "CODE-1" and form.get("redirect_uri") == GOOGLE_REDIRECT
                      and base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == outer.challenge)
                data = ({"access_token": "ya29.x", "expires_in": 3599,
                         **({"refresh_token": "1//REFRESH-xyz"} if outer.give_refresh else {})}
                        if ok else {"error": "invalid_grant", "error_description": "Bad Request"})
                raw = json.dumps(data).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/token"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def google():
    fake = FakeGoogle()
    yield fake
    fake.server.shutdown()


def run_cli(monkeypatch, tmp_path, google, argv, answer_redirect,
            client_id="cid.apps.googleusercontent.com", client_secret="GOCSPX-secret"):
    """模拟你在终端里：填 client id、client secret，打开链接授权，把跳转后的网址粘回来。"""
    monkeypatch.setenv("SMRA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SMRA_MASTER_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["host.creds", *argv, "--token-url", google.url])
    monkeypatch.setattr("builtins.input", lambda prompt="": client_id)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    def fake_getpass(prompt=""):
        if "secret" in prompt:
            return client_secret
        url = next(line for line in printed if line.startswith("https://accounts.google.com/"))
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        google.challenge = query["code_challenge"]
        return answer_redirect(query)

    monkeypatch.setattr(creds.getpass, "getpass", fake_getpass)
    creds._cli()
    return printed


def test_google_login_stores_refresh_token(monkeypatch, tmp_path, google):
    printed = run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"],
                      lambda q: f"{GOOGLE_REDIRECT}?state={q['state']}&code=CODE-1&scope=x")
    url = next(line for line in printed if line.startswith("https://accounts.google.com/"))
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["scope"] == creds.GMAIL_SCOPE and query["access_type"] == "offline"
    assert query["code_challenge_method"] == "S256" and query["redirect_uri"] == GOOGLE_REDIRECT
    assert "GOCSPX-secret" not in url          # client secret 不进授权链接
    assert google.forms[-1]["client_secret"] == "GOCSPX-secret"

    db = connect(tmp_path / "data" / "host.db")
    store = CredentialStore(db, load_key(tmp_path / "data"))
    conn = store.get("gmail")
    assert conn.kind == "oauth2" and conn.hosts is None          # 主机按仓库的 Gmail 档案
    secret = store._secret("gmail")
    assert secret == {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-secret",
                      "refresh_token": "1//REFRESH-xyz", "token_url": google.url}
    assert not any("1//REFRESH-xyz" in line for line in printed)    # refresh token 不打印出来


def test_reconnect_reuses_saved_client_and_clears_expired(monkeypatch, tmp_path, google):
    """授权失效后重连：client id 和 secret 直接回车沿用，只重新在浏览器里授权一次。"""
    ok = lambda q: f"{GOOGLE_REDIRECT}?state={q['state']}&code=CODE-1"
    run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"], ok)
    db = connect(tmp_path / "data" / "host.db")
    db.execute("UPDATE connection SET expired_at = '2026-11-30T00:00:00' WHERE service = 'gmail'")
    db.close()

    printed: list[str] = []
    monkeypatch.setattr(sys, "argv", ["host.creds", "list"])
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    creds._cli()
    assert any("授权已失效" in line for line in printed)

    run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"], ok, client_id="", client_secret="")
    assert google.forms[-1]["client_id"] == "cid.apps.googleusercontent.com"
    assert google.forms[-1]["client_secret"] == "GOCSPX-secret"
    db = connect(tmp_path / "data" / "host.db")
    conn = CredentialStore(db, load_key(tmp_path / "data")).get("gmail")
    assert conn.kind == "oauth2" and conn.expired_at is None


def test_google_login_rejects_wrong_state_and_saves_nothing(monkeypatch, tmp_path, google):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"],
                lambda q: f"{GOOGLE_REDIRECT}?state=someone-else&code=CODE-1")
    assert "state" in str(e.value) and "没有保存" in str(e.value)
    assert google.forms == []          # 根本没去换
    db = connect(tmp_path / "data" / "host.db")
    assert CredentialStore(db, load_key(tmp_path / "data")).get("gmail") is None


def test_google_login_without_refresh_token_explains(monkeypatch, tmp_path, google):
    google.give_refresh = False
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"],
                lambda q: f"{GOOGLE_REDIRECT}?state={q['state']}&code=CODE-1")
    assert "myaccount.google.com/permissions" in str(e.value)


def test_google_login_bad_code_reports_google_error(monkeypatch, tmp_path, google):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, tmp_path, google, ["add", "gmail", "--google-login"],
                lambda q: f"{GOOGLE_REDIRECT}?state={q['state']}&code=EXPIRED")
    assert "HTTP 400" in str(e.value) and "Bad Request" in str(e.value)


def test_code_from_redirect_cases():
    assert code_from_redirect(f" {GOOGLE_REDIRECT}?code=4/abc&state=s1 ", "s1") == "4/abc"
    with pytest.raises(LoginError, match="access_denied"):
        code_from_redirect(f"{GOOGLE_REDIRECT}?error=access_denied&state=s1", "s1")
    with pytest.raises(LoginError, match="完整网址"):
        code_from_redirect(f"{GOOGLE_REDIRECT}?state=s1", "s1")
    with pytest.raises(LoginError, match="state"):
        code_from_redirect("4/abc", "s1")         # 只粘了授权码，没粘网址
