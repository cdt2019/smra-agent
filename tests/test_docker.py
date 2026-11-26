"""容器驱动在真机上的验收：一台装了 Docker 的 Linux。

    SMRA_TEST_DOCKER=1 uv run pytest tests/test_docker.py -s

会建镜像 smra-cell:dev、网络 smra-cellnet、容器 smra-cell / smra-relay、卷 smra-home；测完删掉容器和卷，镜像留着。
不要在正跑着 docker 驱动的机器上跑：名字是固定的，会撞上。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

from host.app import App
from host.computer import State
from host.computer.docker import CELL, RELAY, VOLUME, DockerComputer
from host.config import Config
from tests.conftest import REAL_KEY, CaptureChannel, all_done, wait_until

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("SMRA_TEST_DOCKER") == "1" and shutil.which("docker")),
    reason="需要装了 Docker 的 Linux，并设 SMRA_TEST_DOCKER=1")


def host_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]


@pytest.fixture
async def app(tmp_path, upstream):
    subprocess.run(["docker", "rm", "-f", CELL, RELAY], capture_output=True)
    subprocess.run(["docker", "volume", "rm", VOLUME], capture_output=True)
    cfg = Config(data_dir=tmp_path, driver="docker", port=0, egress_port=0, model_upstream=upstream.url,
                 api_key=REAL_KEY, webhook_port=-1, takeover_port=-1, quiet_hours="", consolidate_cron="",
                 browser="off", retry_delay_s=0, run_timeout_s=300, idle_suspend_s=-1, egress_bounce=False)
    application = App(cfg, CaptureChannel(), DockerComputer())
    application.injection = None
    await application.start(with_channel=False)
    yield application
    await application.computer.destroy()
    await application.stop()
    subprocess.run(["docker", "volume", "rm", VOLUME], capture_output=True)


async def cell_python(app: App, code: str) -> str:
    r = await app.computer.exec(["python", "-c", code], {}, 60)
    assert r.exit_code == 0, r.stderr
    return r.stdout


async def test_run_in_container_and_isolation(app):
    await app.on_user_text("m1", "你好")
    await wait_until(lambda: all_done(app) and app.channel.sent, timeout=900)   # 第一次要建镜像
    assert app.channel.sent == ["echo: 你好"]
    assert await app.computer.state() is State.RUNNING

    probe = f'''
import json, os, socket
out = {{"uid": os.getuid(), "secret_in_env": any({REAL_KEY!r} in v for v in os.environ.values())}}
targets = [("1.1.1.1", 443), ("169.254.169.254", 80), ("172.17.0.1", 22), ({host_ip()!r}, 22),
           ({RELAY!r}, {app.port}), ({RELAY!r}, {app.egress_port}), ({RELAY!r}, 22)]
for host, port in targets:
    try:
        socket.create_connection((host, port), timeout=3).close(); out[f"{{host}}:{{port}}"] = "OPEN"
    except OSError as e:
        out[f"{{host}}:{{port}}"] = type(e).__name__
try:
    socket.getaddrinfo("example.com", 443); out["dns"] = "resolves"
except OSError:
    out["dns"] = "no"
print(json.dumps(out))
'''
    result = json.loads(await cell_python(app, probe))
    print("\n隔离探测：", result)
    assert result["uid"] == 1000 and not result["secret_in_env"]
    opened = sorted(k for k, v in result.items() if v == "OPEN")
    assert opened == sorted([f"{RELAY}:{app.port}", f"{RELAY}:{app.egress_port}"]), result

    # 根文件系统只读，运行程序改不了
    r = await app.computer.exec(["sh", "-c", "echo x >> /opt/agent/agent_run.py"], {}, 30)
    assert r.exit_code != 0


FETCH = r'''
import json, os, ssl, sys, urllib.request, urllib.error
out = {}
for name, url in json.loads(sys.argv[1]).items():
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            out[name] = {"status": r.status, "body": r.read(3000).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        out[name] = {"status": e.code, "approval": e.headers.get("x-smra-approval"),
                     "body": e.read(500).decode("utf-8", "replace")}
    except Exception as e:
        out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
print(json.dumps(out, ensure_ascii=False))
'''


async def fetch_in_cell(app: App, urls: dict[str, str], env: dict[str, str]) -> dict:
    r = await app.computer.exec(["python", "-c", FETCH, json.dumps(urls)], env, 120)
    assert r.exit_code == 0, r.stderr
    return json.loads(r.stdout)


async def test_cell_reaches_real_websites_only_through_the_egress_proxy(app):
    """和一次真实运行一样：把代理的 CA 放进主目录，用运行的通行证设好代理环境变量，从容器里访问真实网站。"""
    from host import sessions
    from host.worker import CA_REL, egress_env

    app.cfg.egress_hold_s = 0.5         # 没见过的地址：挂一下就回 403，等你批准
    await app.computer.ensure_awake()
    await app.computer.put_file(CA_REL, app.egress.ca_cert.read_bytes())
    run = app.runs.start(sessions.current_conversation(app.db, 6), "t-egress", "user")
    ca = f"{app.computer.home_path}/{CA_REL}"
    proxy = f"http://run:{run.token}@{app.computer.egress_host}:{app.egress_port}"
    env = egress_env(proxy, app.computer.no_proxy, ca)
    app.adjudicator.add_seen(["https://example.com/"], "test")

    result = await fetch_in_cell(app, {
        "seen": "https://example.com/",                          # 出现过的地址：放行
        "infra": "https://pypi.org/simple/anthropic/",           # 基础设施白名单（只读）：放行
        "unseen": "https://www.iana.org/help/example-domains",   # 没出现过：拦下，开审批
    }, env)
    print("\n经代理：", {k: {kk: (vv[:80] if isinstance(vv, str) else vv) for kk, vv in v.items()}
                         for k, v in result.items()})
    assert result["seen"]["status"] == 200 and "Example Domain" in result["seen"]["body"], result["seen"]
    assert result["infra"]["status"] == 200 and "anthropic" in result["infra"]["body"], result["infra"]
    assert result["unseen"]["status"] == 403 and result["unseen"]["approval"], result["unseen"]

    # 批准之后再访问：放行
    assert "已批准" in app.adjudicator.resolve(result["unseen"]["approval"], True, "once")
    again = await fetch_in_cell(app, {"unseen": "https://www.iana.org/help/example-domains"}, env)
    assert again["unseen"]["status"] == 200 and not again["unseen"].get("approval"), again

    # 宿主的记录里有这几次判定
    rows = app.db.execute("SELECT url, verdict FROM egress_log").fetchall()
    verdicts = {(u, v) for u, v in rows}
    assert ("https://example.com/", "allow") in verdicts
    assert ("https://www.iana.org/help/example-domains", "allow") in verdicts

    # 通行证不对：代理拒绝
    bad = egress_env(f"http://run:wrong@{app.computer.egress_host}:{app.egress_port}", app.computer.no_proxy, ca)
    denied = await fetch_in_cell(app, {"seen": "https://example.com/"}, bad)
    assert denied["seen"].get("status") == 407 or "407" in denied["seen"].get("error", ""), denied

    # 不信任代理的 CA：证书校验失败。说明 HTTPS 确实被代理解开检查了，不是直通
    no_ca = {k: v for k, v in env.items() if k not in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
    untrusted = await fetch_in_cell(app, {"seen": "https://example.com/"}, no_ca)
    assert "CERTIFICATE_VERIFY_FAILED" in untrusted["seen"].get("error", ""), untrusted


async def test_suspend_wake_and_home_snapshot(app, tmp_path):
    await app.on_user_text("m1", "你好")
    await wait_until(lambda: all_done(app) and app.channel.sent, timeout=900)

    await app.computer.suspend()
    assert await app.computer.state() is State.SUSPENDED
    await app.on_user_text("m2", "还在吗")
    await wait_until(lambda: all_done(app) and len(app.channel.sent) >= 2, timeout=120)
    assert app.channel.sent[1] == "echo: 还在吗"
    assert await app.computer.state() is State.RUNNING

    await app.computer.put_file("notes/a.txt", "第一版".encode())
    snap = tmp_path / "snaps" / "home-1.tar.gz"
    await app.computer.snapshot_home(snap)
    assert snap.exists()
    await app.computer.put_file("notes/a.txt", "第二版".encode())
    await app.computer.restore_home(snap)
    r = await app.computer.exec(["cat", "/home/agent/notes/a.txt"], {}, 30)
    assert r.stdout == "第一版"
    r = await app.computer.exec(["stat", "-c", "%u", "/home/agent/notes/a.txt"], {}, 30)
    assert r.stdout.strip() == "1000"
