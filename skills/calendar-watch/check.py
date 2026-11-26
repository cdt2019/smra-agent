"""calendar-watch 的前置检查：接下来一段时间里有没有还没提醒过的日程。不经过模型。

退出码：0 = 有要提醒的（追加进 work/calendar-watch/due.json，叫醒 Agent）；10 = 没有；其他 = 出错（也会叫醒 Agent）。
只用标准库。
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

UNCHANGED = 10
API = os.environ.get("CALENDAR_WATCH_API", "https://www.googleapis.com/calendar/v3")
KEEP_REMINDED = timedelta(days=2)

HOME = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[2])
WORK = HOME / "work" / "calendar-watch"


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


def now() -> datetime:
    fixed = os.environ.get("CALENDAR_WATCH_NOW")       # 测试用
    return datetime.fromisoformat(fixed) if fixed else datetime.now().astimezone()


def main() -> int:
    token = os.environ.get("SMRA_CRED_GCALENDAR")
    if not token:
        print("没有连接 gcalendar（缺 SMRA_CRED_GCALENDAR）。请用户在宿主上 creds add gcalendar --google-login",
              file=sys.stderr)
        return 2
    config = read_json(WORK / "config.json", {})
    try:
        lookahead = timedelta(minutes=max(5, min(int(config.get("lookahead_minutes", 60)), 24 * 60)))
    except (TypeError, ValueError):
        lookahead = timedelta(minutes=60)
    calendar = str(config.get("calendar") or "primary")
    all_day = bool(config.get("all_day"))

    t0 = now()
    params = {"timeMin": t0.isoformat(), "timeMax": (t0 + lookahead).isoformat(),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": 50}
    url = f"{API.rstrip('/')}/calendars/{urllib.parse.quote(calendar, safe='')}/events?" + urllib.parse.urlencode(params)
    try:
        data = get(url, token)
    except urllib.error.HTTPError as e:
        print(f"日历返回 {e.code}：{e.read()[:300].decode('utf-8', 'replace')}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"连不上日历：{e}", file=sys.stderr)
        return 2

    state = read_json(WORK / "state.json", {})
    reminded: dict = state.get("reminded") or {}
    due = []
    for ev in data.get("items") or []:
        if not isinstance(ev, dict) or ev.get("status") == "cancelled":
            continue
        start = ev.get("start") or {}
        if "dateTime" not in start and not all_day:
            continue                                 # 全天日程默认不提醒
        begins = start.get("dateTime") or start.get("date") or ""
        key = f"{ev.get('id')}@{begins}"             # 改了时间的日程算新的一次
        if key in reminded:
            continue
        if any(a.get("self") and a.get("responseStatus") == "declined"
               for a in ev.get("attendees") or [] if isinstance(a, dict)):
            continue                                 # 用户拒绝了的邀请
        reminded[key] = t0.isoformat()
        due.append({
            "id": ev.get("id"), "title": ev.get("summary") or "（无标题）", "start": begins,
            "end": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date") or "",
            "location": ev.get("location") or "", "link": ev.get("hangoutLink") or "",
            "attendees": [a.get("email") for a in ev.get("attendees") or []
                          if isinstance(a, dict) and a.get("email") and not a.get("self")][:10],
        })

    # 两天前提醒过的不用再记
    cutoff = t0 - KEEP_REMINDED
    kept = {}
    for k, v in reminded.items():
        try:
            if datetime.fromisoformat(v) >= cutoff:
                kept[k] = v
        except (TypeError, ValueError):
            pass
    if not due:
        if kept != (state.get("reminded") or {}):
            write_json(WORK / "state.json", {**state, "reminded": kept})
        print("没有要提醒的日程")
        return UNCHANGED

    pending = read_json(WORK / "due.json", [])
    if not isinstance(pending, list):
        pending = []
    write_json(WORK / "due.json", pending + due)
    write_json(WORK / "state.json", {**state, "reminded": kept})
    print(f"{len(due)} 个日程快开始了：" + "；".join(f"{d['start']} {d['title']}" for d in due))
    return 0


if __name__ == "__main__":
    sys.exit(main())
