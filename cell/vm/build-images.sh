#!/bin/sh
# 在一台有 KVM 的 Linux 机器上准备 microVM 驱动需要的东西（第 9 步）：
#   data/vm/vmlinux        客户机内核（Firecracker CI 构建的，带 virtio-blk 和 vsock）
#   data/vm/rootfs.ext4    系统盘：Python、anthropic SDK、git、smra-init
#   bin/firecracker、bin/jailer
# 主目录盘和核心盘由宿主在第一次用到时自己建（host/computer/firecracker.py）。
#
# 需要：docker（只用来拼系统盘的文件树）、e2fsprogs（mkfs.ext4 -d）、curl、tar。
# 用法：在仓库根目录执行  sh cell/vm/build-images.sh
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT="${SMRA_DATA_DIR:-$ROOT/data}/vm"
BIN="$ROOT/bin"
ARCH=$(uname -m)
mkdir -p "$OUT" "$BIN"

[ -e /dev/kvm ] || echo "警告：没有 /dev/kvm，这台机器跑不了 microVM" >&2

# ---- Firecracker 与监禁程序：取最新的发布版
if [ ! -x "$BIN/firecracker" ]; then
    release_url="https://github.com/firecracker-microvm/firecracker/releases"
    latest=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$release_url/latest")")
    echo "下载 Firecracker $latest"
    curl -fsSL "$release_url/download/$latest/firecracker-$latest-$ARCH.tgz" | tar -xz -C "$BIN"
    mv "$BIN/release-$latest-$ARCH/firecracker-$latest-$ARCH" "$BIN/firecracker"
    mv "$BIN/release-$latest-$ARCH/jailer-$latest-$ARCH" "$BIN/jailer"
    rm -rf "$BIN/release-$latest-$ARCH"
fi

# ---- 内核：Firecracker CI 用的那份（和 Firecracker 入门文档里的做法一样）
if [ ! -f "$OUT/vmlinux" ]; then
    latest=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        https://github.com/firecracker-microvm/firecracker/releases/latest)")
    ci="${latest%.*}"
    key=$(curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/$ci/$ARCH/vmlinux-&list-type=2" \
        | grep -oE "firecracker-ci/$ci/$ARCH/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3}" | sort -V | tail -1)
    [ -n "$key" ] || { echo "找不到 CI 内核，请按 Firecracker 的 getting-started 文档手动下载到 $OUT/vmlinux" >&2; exit 1; }
    echo "下载内核 $key"
    curl -fsSL -o "$OUT/vmlinux" "https://s3.amazonaws.com/spec.ccfc.min/$key"
fi

# ---- 系统盘
echo "构建系统盘"
docker build -f "$ROOT/cell/vm/Dockerfile.rootfs" -t smra-rootfs:dev "$ROOT"
tree=$(mktemp -d)
cid=$(docker create smra-rootfs:dev)
docker export "$cid" | tar -x -C "$tree" --exclude='dev/*'
docker rm "$cid" >/dev/null
rm -f "$OUT/rootfs.ext4"
size_mb=$(( $(du -sm "$tree" | cut -f1) + 300 ))
mkfs.ext4 -q -L smra-root -d "$tree" "$OUT/rootfs.ext4" "${size_mb}M"
rm -rf "$tree"

echo "完成：$OUT/vmlinux、$OUT/rootfs.ext4、$BIN/firecracker、$BIN/jailer"
echo "启动：SMRA_DRIVER=firecracker uv run python -m host.main --channel console"
