"""U6：保持清醒的请求跨宿主重启；docker 电脑容器用 --init 回收孤儿进程。"""
from __future__ import annotations

from datetime import timedelta

from host import db as dbmod
from host.sleeper import STATE_KEY


def test_keep_awake_survives_restart(make_app):
    first = make_app()
    until, capped = first.sleeper.keep_awake("跑一个长任务", 30)
    assert not capped

    second = make_app()             # 同一个数据目录上重新起一个宿主
    assert second.sleeper.awake_until == until and second.sleeper.awake_reason == "跑一个长任务"
    assert not second.sleeper.due()

    # 到期：照常挂起，记录也清掉
    real = second.sleeper.clock
    second.sleeper.clock = lambda: real() + timedelta(minutes=31)
    assert second.sleeper.due()
    assert dbmod.get_state(second.db, STATE_KEY) is None
    assert make_app().sleeper.awake_until is None


def test_revoked_or_expired_or_garbled_is_not_loaded(make_app):
    app = make_app()
    app.sleeper.keep_awake("x", 30)
    app.sleeper.keep_awake("", 0)       # 撤销
    assert make_app().sleeper.awake_until is None

    dbmod.set_state(app.db, STATE_KEY, '{"until": "2000-01-01T00:00:00+00:00", "reason": "早过了"}')
    assert make_app().sleeper.awake_until is None
    assert dbmod.get_state(app.db, STATE_KEY) is None

    dbmod.set_state(app.db, STATE_KEY, "不是 JSON")
    assert make_app().sleeper.awake_until is None
    assert dbmod.get_state(app.db, STATE_KEY) is None


async def test_docker_cell_runs_with_init(monkeypatch):
    from host.computer import docker
    calls = []

    async def fake(self, *args, check=True, timeout=300, input=None):
        calls.append(args)
        absent = args[0] == "inspect"       # 电脑容器还没有；镜像和网络都在
        return docker.ExecResult(1 if absent else 0, "", "", False)

    monkeypatch.setattr(docker.DockerComputer, "_docker", fake)
    computer = docker.DockerComputer()
    computer.configure(1, 2)
    await computer.create()
    cell_run = [a for a in calls if a[:1] == ("run",) and docker.CELL in a]
    assert len(cell_run) == 1 and "--init" in cell_run[0]
    assert cell_run[0].index("--init") < cell_run[0].index(docker.IMAGE)     # 是 docker 的参数，不是命令的


def test_shorter_request_does_not_rewrite_the_saved_deadline(make_app):
    app = make_app()
    long_until, _ = app.sleeper.keep_awake("长的", 60)
    app.sleeper.keep_awake("短的", 5)
    again = make_app()
    assert again.sleeper.awake_until == long_until and again.sleeper.awake_reason == "长的"
