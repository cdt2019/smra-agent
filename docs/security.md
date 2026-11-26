# 安全模型

[← 回到 README](../README.md)

整体思路见[设计文档](design.md)。一句话：Agent 跑在一台隔离的电脑里（容器或 microVM），
它的一切网络出口都由宿主裁决，真凭证永远不进电脑。

## 网络出口怎么判

电脑里的一切网络请求都经过出网代理，代理把每个请求交给裁决者：

| 请求 | 判定 |
| --- | --- |
| 目的地是内网、环回、云元数据等非公网地址 | 拒绝（代理自己解析域名，连接的就是检查过的地址） |
| 80、443 以外的端口；非 HTTP 协议；WebSocket | 拒绝 |
| 包管理源、代码托管的 GET（以及 git 拉取） | 放行 |
| GET 一个"见过"的地址：搜索结果、网页里的链接、你消息里的地址 | 放行，Cookie 和自定义请求头被去掉 |
| GET 一个 Agent 自己拼的地址 | 问你；可授权一次、本次运行、本次会话、一小时或永久 |
| POST 等会向外发数据的请求 | 问你；只能授权"仅这一次"，并且绑定请求内容 |

需要问你时，代理把请求挂住 30 秒。你在这期间批准，请求直接放行；否则这次运行挂起结束，
你答复后一条审批事件把原来的会话叫醒，Agent 重试原请求。审批内容由裁决者根据实际请求生成，
电脑看不到也答复不了。

浏览器的流量走同一个出网代理，但判定规则按浏览器的请求特点另定，见 [浏览器](browser.md)。
连接的外部账号（替身令牌、审批怎么问）见 [接入外部服务](integrations.md)。

## 宿主为什么还以 root 运行

**结论**：docker 驱动下宿主进程是 root，这是有意的；换成一个加进 docker 组的普通用户并不更安全。已经做的是在 root 的前提下把它能碰的东西收到最少。

**为什么不换用户**。宿主要调 docker 建电脑容器和浏览器容器。能对 docker 守护进程说话，就能
`docker run --privileged -v /:/host ...` 拿到整台机器，所以 docker 组本身就等同 root。把宿主换成 docker 组里的普通用户，
权限实际没变，反而让人误以为它是"非 root"的；还要处理数据目录、uv 缓存、浏览器配置目录的属主。

**考虑过的别的办法**，现在都没做：

| 办法 | 为什么先不做 |
|---|---|
| rootless docker（守护进程以普通用户运行） | 要为这个用户另起一套 docker，镜像、卷、网络都要重建；资源限制要 systemd 委派 cgroup。和别的服务共用一台机器时，往往不能改动系统的 docker |
| docker 套接字代理（只放行建、启停、删 smra 容器的几个接口） | 有用，但代理本身要写对：放过一个 `Binds` 或 `Privileged` 字段就等于没有。留到以后 |
| firecracker 驱动 | jailer 启动每台 microVM 时就切到一个无特权用户、chroot 进单独目录，宿主自己仍要 root 才能这样做。这条路已写好，没在 KVM 上跑过 |

**已经做的**（`deploy/hardening.conf`，`install.sh` 默认装上，删掉这个文件再 `daemon-reload` 就回退）：

- 没有任何 capability（`CapabilityBoundingSet=` 为空），`NoNewPrivileges`，不许 setuid/setgid。root 的能力基本只剩"文件属主是 root"和"能连 docker 套接字"。
- `ProtectSystem=strict`：整个文件系统只读，只有项目目录、`~/.docker`、`~/.cache` 可写。
- 自己的 `/tmp`、看不到设备，碰不到内核参数、内核模块、内核日志、cgroup、时钟、主机名；不许建新的命名空间。
- 只许 Unix 套接字、IPv4/IPv6 和 netlink 几种地址族，只许本机架构的系统调用。
- `UMask=0077`，宿主启动时再把数据目录收到 700、数据库收到 600：数据目录里有对话和模型请求的明文。
- 宿主接口和模型代理只听 127.0.0.1；接管页面只经 Tailscale 暴露给你自己的设备，不开 Funnel；Webhook 是单独的端口。
- 主密钥放进 systemd 的加密凭证，不和数据库放在一起（见下一节）。
- 结果：`systemd-analyze security smra-agent` 是 3.6（OK）。这套只在 docker 驱动下验证过。firecracker 驱动要用 `/dev/kvm` 和网络设备，不装它。

**这意味着什么**。信任边界在电脑和宿主之间：Agent 的代码只在电脑里跑，从不在宿主进程里执行；它能碰到宿主的只有三个入口——
模型代理、宿主接口、出网代理，都要带当次运行的通行证。对外另有接管页面（随机钥匙）和 Webhook 端口（HMAC 签名）。
宿主自己的代码被攻破（比如这些入口的解析出了漏洞），
攻击者经 docker 就是这台机器的 root。上面这些收紧限制的是宿主进程本身能直接做的事，挡不住"经 docker 再起一个特权容器"。

从别的机器 tar 过去的文件记得加 `--no-same-owner`，不然属主是源机器上的用户号。

## 主密钥别和数据库放一起

已连接账号的凭证用主密钥加密存在 `data/host.db`；第一次启动会在 `data/master.key`
生成主密钥，图省事，但拷走或备份数据目录就连钥匙一起带走了。`deploy/install.sh` 会把它放进 systemd 的加密凭证
（systemd 250+；没有 TPM 时用本机的 `/var/lib/systemd/credential.secret` 加密）。手动做是这样：

```bash
systemctl stop smra-agent
systemd-creds encrypt --name=smra-master-key data/master.key /etc/credstore.encrypted/smra-master-key
# drop-in 里加一行：
#   [Service]
#   LoadCredentialEncrypted=smra-master-key:/etc/credstore.encrypted/smra-master-key
echo 'SMRA_MASTER_KEY_CRED=/etc/credstore.encrypted/smra-master-key' >> .env   # 让 host.creds 命令行也找得到
systemctl daemon-reload && systemctl start smra-agent
# 确认 /connections 正常后，把 data/master.key 挪走（离线留一份），数据目录里就没有钥匙了
```

找密钥的顺序：systemd 凭证 → `SMRA_MASTER_KEY` → `SMRA_MASTER_KEY_CRED`（调 `systemd-creds decrypt`，要 root）→
`data/master.key`。库里已经有凭证却一个密钥都找不到时，宿主拒绝启动，不会生成新密钥把旧凭证变成乱码。
说清楚：同一台机器上的 root 照样拿得到密钥；这一步防的是数据目录和备份被拷走。

## 驱动和隔离

`local` 驱动没有任何隔离，设计文档里的安全性质在它之下都不成立。
