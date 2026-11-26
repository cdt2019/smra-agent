"""第 9 步在真机上的验收：一台有 KVM 的 Linux，先跑过 cell/vm/build-images.sh。

    SMRA_TEST_FIRECRACKER=1 uv run pytest tests/test_step9_vm.py -s

默认以当前用户直接启动 Firecracker（要能读写 /dev/kvm）。以 root 跑并设 SMRA_TEST_FC_JAILER=1 时经 jailer 启动。
内核和系统盘默认取仓库的 data/vm/，可以用 SMRA_FC_KERNEL / SMRA_FC_ROOTFS 指定。
-s 能看到量出来的冷启动、挂起、恢复耗时和状态文件大小，同时追加到 <数据目录>/vm/metrics.jsonl。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import time
from pathlib import Path

import pytest

from host.app import App
from host.computer import State
from host.computer.firecracker import MEM, SNAP, FirecrackerComputer
from host.config import ROOT, Config
from tests.conftest import REAL_KEY, CaptureChannel, all_done, wait_until

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("SMRA_TEST_FIRECRACKER") == "1"
         and os.access("/dev/kvm", os.R_OK | os.W_OK)),
    reason="需要有 KVM 的 Linux，并设 SMRA_TEST_FIRECRACKER=1")


def vm_config(data_dir: Path, **overrides) -> Config:
    cfg = Config(
        data_dir=data_dir, driver="firecracker",
        fc_kernel=os.environ.get("SMRA_FC_KERNEL", str(ROOT / "data" / "vm" / "vmlinux")),
        fc_rootfs=os.environ.get("SMRA_FC_ROOTFS", str(ROOT / "data" / "vm" / "rootfs.ext4")),
        fc_jailer=os.environ.get("SMRA_TEST_FC_JAILER") == "1", fc_home_gib=1, fc_mem_mib=512)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


async def sh(vm: FirecrackerComputer, script: str, timeout: float = 30) -> str:
    r = await vm.exec(["sh", "-c", script], {}, timeout)
    assert r.exit_code == 0, r.stderr
    return r.stdout


@pytest.fixture
async def vm(tmp_path):
    computer = FirecrackerComputer(vm_config(tmp_path))
    computer.configure(1, 2)        # 这几个测试不连宿主接口
    yield computer
    await computer.destroy()


async def test_s12_suspend_frees_the_host_and_background_work_continues(vm):
    await vm.ensure_awake()
    cold = vm.metrics[-1]["seconds"]
    # 挂起前起一个后台进程：每秒往主目录里记一次时间
    await sh(vm, "nohup sh -c 'while true; do date +%s >> /home/agent/tick; sleep 1; done' >/dev/null 2>&1 &")
    await asyncio.sleep(3)
    pid = vm._proc.pid
    await vm.suspend()
    suspended = vm.metrics[-1]
    # 宿主上没有监视器进程了：CPU 和内存都归零
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert await vm.state() is State.SUSPENDED
    await asyncio.sleep(6)

    await vm.ensure_awake()
    resumed = vm.metrics[-1]
    assert resumed["what"] == "resume"
    guest_now = float(await sh(vm, "date +%s.%N"))
    assert abs(guest_now - time.time()) < 2          # 恢复后对过时
    await asyncio.sleep(3)
    ticks = [int(t) for t in (await sh(vm, "cat /home/agent/tick")).split()]
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert max(gaps) >= 6                            # 挂起期间冻住了
    assert ticks[-1] >= int(time.time()) - 3         # 恢复后接着跑
    print(f"\n冷启动 {cold} 秒；挂起 {suspended['seconds']} 秒，状态文件 {suspended['state_bytes'] >> 20} MB；"
          f"恢复 {resumed['seconds']} 秒")


async def test_root_in_the_guest_reaches_nothing_but_the_two_relays(vm):
    """对抗：客户机里的 root。没有网卡、没有路由；vsock 上只有宿主开的两个端口。"""
    await vm.ensure_awake()
    probe = r'''
import json, os, socket
out = {"ifaces": sorted(os.listdir("/sys/class/net")), "uid": os.getuid()}
for host, port in [("1.1.1.1", 443), ("169.254.169.254", 80), ("10.0.2.2", 80), ("172.17.0.1", 2375)]:
    try:
        socket.create_connection((host, port), timeout=2).close(); out[f"{host}:{port}"] = "OPEN"
    except OSError as e:
        out[f"{host}:{port}"] = type(e).__name__
for port in (1, 22, 52, 8787, 10003):
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM); s.settimeout(2)
    try:
        s.connect((2, port)); out[f"vsock:{port}"] = "OPEN"
    except OSError as e:
        out[f"vsock:{port}"] = type(e).__name__
    finally:
        s.close()
print(json.dumps(out))
'''
    result = json.loads(await sh(vm, f"python3 -c '{probe}'"))
    assert result["uid"] == 0
    assert result["ifaces"] == ["lo"]
    assert not [k for k, v in result.items() if v == "OPEN"], result


async def test_state_file_contains_no_real_secrets(tmp_path, upstream):
    """挂起产生的状态文件里，搜不到模型密钥和凭证加密主密钥。"""
    cfg = vm_config(tmp_path, port=0, egress_port=0, model_upstream=upstream.url, api_key=REAL_KEY,
                    webhook_port=-1, takeover_port=-1, quiet_hours="", consolidate_cron="", browser="off")
    app = App(cfg, CaptureChannel(), FirecrackerComputer(cfg))
    app.injection = None
    await app.start(with_channel=False)
    try:
        await app.on_user_text("m1", "你好")
        await wait_until(lambda: all_done(app) and app.channel.sent, timeout=180)
        assert app.channel.sent == ["echo: 你好"]
        await app.computer.suspend()
        master = (tmp_path / "master.key").read_bytes().strip()
        for name in (SNAP, MEM):
            blob = (app.computer.workdir / name).read_bytes()
            assert REAL_KEY.encode() not in blob and master not in blob, name
    finally:
        await app.computer.destroy()
        await app.stop()
