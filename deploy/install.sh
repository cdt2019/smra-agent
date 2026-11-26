#!/bin/sh
# 一条命令装成 systemd 服务。在仓库目录里用 root 运行：
#     sudo sh deploy/install.sh              # 安装或更新，然后（重新）启动服务
#     sudo sh deploy/install.sh --dry-run    # 只显示会写哪些文件、和现在的有什么不同，什么都不改
#     sudo sh deploy/install.sh --no-hardening   # 不装权限收紧（排查问题时用）
#
# 做的事：
#   1. 检查 systemd、docker、uv；装依赖（uv sync --frozen）
#   2. 没有 .env 就从 .env.example 复制一份（600），让你填好后再跑一次
#   3. 自检（python -m host.doctor）
#   4. 主密钥放进 systemd 加密凭证（/etc/credstore.encrypted/smra-master-key）：
#      新装直接生成在那里，明文不落盘；已有 data/master.key 的照它加密一份（原文件不删，等你确认后自己挪走）
#   5. 写 /etc/systemd/system/smra-agent.service 和 smra-agent.service.d/ 下的 hardening.conf、credentials.conf，
#      启用并（重新）启动。这台机器特有的设置放 smra-agent.service.d/local.conf，这个脚本不碰它
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNIT=/etc/systemd/system/smra-agent.service
DROPIN=/etc/systemd/system/smra-agent.service.d
CRED=/etc/credstore.encrypted/smra-master-key
DRY=0
HARDEN=1
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --no-hardening) HARDEN=0 ;;
        *) echo "不认识的参数：$arg" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf '不行：%s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "要用 root 运行（宿主要调 docker、写 /etc/systemd）"
command -v systemctl >/dev/null || die "这台机器没有 systemd"
command -v docker >/dev/null || die "没装 docker"
if [ -x "$ROOT/.bin/uv" ]; then UV="$ROOT/.bin/uv"; else UV=$(command -v uv || true); fi
[ -n "$UV" ] || die "没装 uv。装法见 https://docs.astral.sh/uv/ ，或把 uv 放进 $ROOT/.bin/"
cd "$ROOT"

if [ ! -f .env ]; then
    [ "$DRY" = 1 ] && die "还没有 .env（--dry-run 不会替你建）"
    (umask 077 && cp .env.example .env)
    say "已从 .env.example 复制出 .env（只有 root 能读）。填好模型密钥和 TELEGRAM_BOT_TOKEN 后再跑一次这个脚本。"
    exit 0
fi
chmod 600 .env

if [ "$DRY" = 0 ]; then
    say "== 装依赖"
    "$UV" sync --frozen -q
fi

say "== 自检"
RUNNING=0
systemctl is-active -q smra-agent 2>/dev/null && RUNNING=1
if [ "$RUNNING" = 1 ]; then
    "$UV" run --frozen python -m host.doctor --channel telegram --no-ports || die "自检没通过，见上面"
else
    "$UV" run --frozen python -m host.doctor --channel telegram || die "自检没通过，见上面"
fi

if grep -q '^SMRA_DRIVER=firecracker' .env && [ "$HARDEN" = 1 ]; then
    say "电脑驱动是 firecracker：要用 /dev/kvm 和网络设备，不装权限收紧"
    HARDEN=0
fi

# ---- 生成单元文件
HOME_DIR=$(getent passwd root | cut -d: -f6)       # sudo 下 $HOME 可能还是原来那个用户的
PATH_VALUE="$(dirname "$UV"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EXTRA_ENV=""
[ -d "$ROOT/.browsers" ] && EXTRA_ENV=" \"PLAYWRIGHT_BROWSERS_PATH=$ROOT/.browsers\""
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
render() {
    sed -e "s#@ROOT@#$ROOT#g" -e "s#@UV@#$UV#g" -e "s#@HOME@#$HOME_DIR#g" \
        -e "s#@PATH@#$PATH_VALUE#g" -e "s#@EXTRA_ENV@#$EXTRA_ENV#g" "$ROOT/deploy/$1" > "$OUT/$1"
}
render smra-agent.service
render hardening.conf
render credentials.conf

USE_CRED=0
command -v systemd-creds >/dev/null && USE_CRED=1

if [ "$DRY" = 1 ]; then
    for pair in "smra-agent.service:$UNIT" "hardening.conf:$DROPIN/hardening.conf" "credentials.conf:$DROPIN/credentials.conf"; do
        src=${pair%%:*}; dst=${pair#*:}
        [ "$src" = hardening.conf ] && [ "$HARDEN" = 0 ] && { say "== 不装 $dst"; continue; }
        [ "$src" = credentials.conf ] && [ "$USE_CRED" = 0 ] && { say "== 没有 systemd-creds，不装 $dst"; continue; }
        say "== $dst"
        if [ -f "$dst" ]; then diff -u "$dst" "$OUT/$src" && say "（没变）" || true
        else cat "$OUT/$src"; fi
    done
    [ -f "$DROPIN/local.conf" ] && say "== 保留 $DROPIN/local.conf（不碰）"
    [ "$USE_CRED" = 1 ] && [ ! -f "$CRED" ] && say "== 会把主密钥放进 $CRED"
    say "（--dry-run：什么都没改）"
    exit 0
fi

# ---- 主密钥
if [ "$USE_CRED" = 1 ] && [ ! -f "$CRED" ]; then
    mkdir -p "$(dirname "$CRED")" && chmod 700 "$(dirname "$CRED")"
    if [ -f data/master.key ]; then
        systemd-creds encrypt --name=smra-master-key data/master.key "$CRED"
        say "主密钥已加密放进 $CRED。确认服务正常后，把 data/master.key 挪出数据目录离线保存。"
    elif "$UV" run --frozen python -c "import sys; from pathlib import Path; from host.creds import _has_secrets; sys.exit(_has_secrets(Path('data')))"; then
        "$UV" run --frozen python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
            | systemd-creds encrypt --name=smra-master-key - "$CRED"
        say "新的主密钥已生成在 $CRED（明文没有落盘）。"
    else
        die "库里有已连接的账号，却找不到主密钥（data/master.key 和 $CRED 都没有）。把原来的密钥放回 data/master.key 再跑"
    fi
    chmod 600 "$CRED"
fi
if [ -f "$CRED" ] && ! grep -q '^SMRA_MASTER_KEY_CRED=' .env; then
    [ -n "$(tail -c1 .env)" ] && echo >> .env
    echo "SMRA_MASTER_KEY_CRED=$CRED" >> .env      # 让 python -m host.creds 命令行也找得到
fi

# ---- 装单元文件
mkdir -p "$DROPIN"
install -m 644 "$OUT/smra-agent.service" "$UNIT"
if [ "$HARDEN" = 1 ]; then install -m 644 "$OUT/hardening.conf" "$DROPIN/hardening.conf"; else rm -f "$DROPIN/hardening.conf"; fi
if [ -f "$CRED" ]; then install -m 644 "$OUT/credentials.conf" "$DROPIN/credentials.conf"; fi
rm -f "$DROPIN/killmode.conf"       # 老版本单独放的，已经并进主单元
systemctl daemon-reload
systemctl enable -q smra-agent

if [ "$RUNNING" = 1 ]; then
    say "== 重启（宿主会先等手上的运行做完，最多 5 分钟）"
    systemctl restart smra-agent
else
    say "== 启动"
    systemctl start smra-agent
fi
sleep 5
if systemctl is-active -q smra-agent; then
    say "服务在跑。看日志：journalctl -u smra-agent -f    看状态：$UV run python -m host.status"
else
    journalctl -u smra-agent -n 30 --no-pager -o cat
    die "服务没起来，见上面的日志"
fi
