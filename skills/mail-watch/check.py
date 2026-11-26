"""mail-watch 的前置检查：有没有还没看过的新邮件。不经过模型。

退出码：0 = 有新邮件（id 追加进 work/mail-watch/new.json，叫醒 Agent）；10 = 没有；其他 = 出错（也会叫醒 Agent）。
只调 Gmail 的列表接口，拿到的只有邮件 id，不读内容。只用标准库。
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UNCHANGED = 10
DEFAULT_QUERY = "is:unread in:inbox newer_than:2d -category:promotions -category:social"
API = os.environ.get("MAIL_WATCH_API", "https://gmail.googleapis.com/gmail/v1/users/me")
MAX_SEEN = 2000

HOME = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[2])
WORK = HOME / "work" / "mail-watch"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def get(url: str, token: str) -> dict:
    ca = os.environ.get("SSL_CERT_FILE")
    ctx = ssl.create_default_context(cafile=ca) if ca and os.path.exists(ca) else None
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    token = os.environ.get("SMRA_CRED_GMAIL")
    if not token:
        print("没有连接 gmail（缺 SMRA_CRED_GMAIL）。请用户在宿主上 creds add gmail", file=sys.stderr)
        return 2
    query = read_json(WORK / "config.json", {}).get("query") or DEFAULT_QUERY
    url = f"{API.rstrip('/')}/messages?" + urllib.parse.urlencode({"q": query, "maxResults": 50})
    try:
        data = get(url, token)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        print(f"Gmail 返回 {e.code}：{body}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"连不上 Gmail：{e}", file=sys.stderr)
        return 2

    ids = [m["id"] for m in data.get("messages") or [] if isinstance(m, dict) and m.get("id")]
    state = read_json(WORK / "state.json", {})
    seen = list(state.get("seen") or [])
    seen_set = set(seen)
    fresh = [i for i in ids if i not in seen_set]
    if not fresh:
        print("没有新邮件")
        return UNCHANGED

    pending = read_json(WORK / "new.json", [])
    if not isinstance(pending, list):
        pending = []
    pending += [i for i in fresh if i not in pending]
    write_json(WORK / "new.json", pending)
    write_json(WORK / "state.json", {**state, "seen": (seen + fresh)[-MAX_SEEN:]})
    print(f"{len(fresh)} 封新邮件，待处理共 {len(pending)} 封，id 在 work/mail-watch/new.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
