# 部署与运维

[← 回到 README](../README.md)

需要 Python 3.12 以上（mitmproxy 的要求）。

```bash
uv sync
```

把 `.env.example` 复制为 `.env`，选模型提供方并填密钥。

## 模型提供方

不绑定某一家。用 `SMRA_MODEL_PROVIDER` 选一个预设，其余设置都可以单独覆盖：

| 提供方 | 协议 | 默认地址 | 默认模型 / 注入检测模型 | 密钥 |
|---|---|---|---|---|
| `anthropic`（默认） | anthropic | api.anthropic.com | claude-opus-5-5 / claude-haiku-5-5 | `ANTHROPIC_API_KEY` |
| `deepseek` | anthropic | api.deepseek.com/anthropic | deepseek-v4-pro / deepseek-flash | `DEEPSEEK_API_KEY` |
| `openai` | openai | api.openai.com/v1 | 必须用 `SMRA_MODEL` 指定 | `OPENAI_API_KEY` |

其他厂商这样接：
- 提供 Anthropic 兼容接口的（Kimi、GLM 等）：用 `deepseek` 或 `anthropic` 预设，改 `SMRA_MODEL_UPSTREAM` 和 `SMRA_MODEL`。
- 只有 OpenAI 兼容接口的（通义、OpenRouter、本地 vLLM、Ollama 等）：用 `openai` 预设，改地址和模型名。
- 密钥也可以统一写在 `SMRA_MODEL_API_KEY` 里。

电脑里的运行程序始终用 Anthropic Messages 协议和宿主说话。宿主的模型代理负责对接：
- anthropic 协议原样转发；
- openai 协议翻译成 Chat Completions，再把结果翻译回来。

各家支持的功能不同，用 `SMRA_MODEL_FEATURES` 声明（逗号分隔），运行程序只发声明过的字段：
- `stream`：流式响应。
- `thinking`：思考。
- `effort`：思考深度。
- `cache`：提示词缓存。
- `betas`：思考块绑定、拒绝时换后备模型。
- `compaction`：服务端上下文压缩。
- `text_editor`：Anthropic 定义的文本编辑器工具。没有时换成同名、同参数的普通工具。
- `web_search`：服务端网页搜索。没有时由宿主代为搜索（`SMRA_SEARCH_PROVIDER`，deepseek 预设自动打开）；两者都没有才不提供这个工具。
- `structured_output`：注入检测用结构化输出。没有时要求模型只输出 JSON；看不懂就当作命中。
- `vision`：主模型能看工具结果里的图片（浏览器截图）。没有时宿主在转发前把图换成一句说明，叫 Agent 看无障碍树
  （deepseek-v4-pro 看不了图，deepseek 预设不带这一项）。

截图也过注入检测：模型代理把新截图和新文字一起交给检测模型，图里写着的指令同样算。检测模型能不能看图由
`SMRA_GUARD_VISION`（1 / 0）决定，anthropic 和 deepseek 预设默认能（deepseek-flash 支持工具结果里的图片），openai 预设默认不能。
主模型能看图、检测模型不能时，读到截图就按命中处理；一次超过 4 张截图也按命中处理。主模型看不到的图（被换掉了，或者走 openai 协议）不检查。

除 Anthropic 官方以外，提供方都没有服务端压缩，由宿主来压缩（`host/compact.py`）：
- 每次运行开始前估一下会话有多大：优先用上一次调用模型实际报来的输入长度（含系统提示和工具定义），再和按字数估的取大者。
- 超过 `SMRA_COMPACT_AT`（默认 10 万 token）时，把较早的部分连同上一次的摘要交给主模型（关掉思考）写成一份交接摘要，
  最近约 `SMRA_COMPACT_KEEP`（默认 2 万）token 原样保留。切点只落在你发来的消息上，不会把工具调用和结果拆开。
- 之后模型看到的是"宿主生成的摘要 + 最近几轮"。摘要开头写明它是宿主写的，里面提到的外部内容仍然只是资料。
- 原文一条不删，`recall` 照样搜得到，活动记录照样看得到。这次调用记在 `compact:<会话号>` 名下，计入每日花费；
  今天的预算用完了就不压缩，压缩失败只记日志，运行照常。
- 一次运行内部的增长不管，那由运行预算和 50 步上限兜住。声明了 `compaction` 功能（Anthropic 官方）时不做，`SMRA_COMPACT_AT=0` 关掉。

宿主代搜用 DeepSeek 的 `deepseek_web_search`，并且关掉思考：开思考时它还能打开网页，等于绕过出网代理。响应里出现搜索以外的动作，整次结果作废。每次运行最多搜 20 次，用量计入预算。

所有 token 预算里，缓存读取按 `SMRA_CACHE_READ_WEIGHT`（默认 0.1）折算，其余按全量计入。这个比例按你用的模型的缓存价格调。

## 运行方式

没有 Docker 时，在终端里试用（不隔离，默认没有命令行工具）：

```bash
uv run python -m host.main --channel console --driver local
```

用容器隔离（需要 Docker，Linux）：

```bash
uv run python -m host.main --channel console --driver docker
```

宿主进程只听 127.0.0.1；docker 驱动在默认网桥的网关地址（通常是 172.17.0.1）上另开两个转发，中继容器经它们连到宿主。
第一次启动会建镜像 `smra-cell:dev`。和别的服务同机时，可以用 `/etc/docker/daemon.json` 的 `cgroup-parent`
把容器放进一个限了 CPU 和内存的 systemd slice。容器驱动的验收：`SMRA_TEST_DOCKER=1 uv run pytest tests/test_docker.py -s`。

终端里发文件：`/file <路径> [附带的话]`。批准网络请求：`/approve <审批号> [范围]`，拒绝：`/deny <审批号>`。
定时器：`/timers` 列出（带最近几次的结果），`/cancel <定时器号>` 取消，`/run` 现在做一次，`/resume` 恢复取消了的日程。给通知打分：`/feedback <通知号> useful|unnecessary`。其他命令见 `/help`。

## 常驻运行（systemd）

Linux 上一条命令装成服务（要 root、docker、uv）：

```bash
sudo sh deploy/install.sh              # 第一次跑会复制出 .env 让你填，填好再跑一次
sudo sh deploy/install.sh --dry-run    # 只看会写什么、和现在的有什么不同
```

它会装依赖、跑自检、把主密钥放进 systemd 加密凭证，再写这几个文件并启动：

| 文件 | 内容 |
|---|---|
| `/etc/systemd/system/smra-agent.service` | 主单元，来自 `deploy/smra-agent.service` |
| `smra-agent.service.d/hardening.conf` | 收紧权限，来自 `deploy/hardening.conf`（firecracker 驱动不装，`--no-hardening` 也不装） |
| `smra-agent.service.d/credentials.conf` | 主密钥的加密凭证，来自 `deploy/credentials.conf` |
| `smra-agent.service.d/local.conf` | 这台机器特有的设置，**脚本不碰**，自己写 |

和别的服务同机时，在 `local.conf` 里加 `CPUQuota=`、`MemoryMax=`、`Nice=19`、`IOSchedulingClass=idle`，
以及 `Environment="SMRA_CELL_CPUS=1" "SMRA_CELL_MEMORY=1g"` 之类，让它别抢别人的资源。更新代码后重跑脚本即可。

**启动自检**。宿主启动时先查模型密钥、Telegram 令牌、数据目录、docker 和镜像、浏览器、端口；有"不行"的项就不启动，
退出码 78（单元里设了 `RestartPreventExitStatus=78`，不会反复重启），日志里写清楚缺什么。也可以单独跑：
`uv run python -m host.doctor`（服务在跑时加 `--no-ports`）。

**状态**。Telegram 里发 `/status`：有没有运行在跑、排队几件、等你处理的审批和提议、今日花费、已连接的账号、
下一个定时器、电脑开着还是挂起。终端里：`uv run python -m host.status`；部署脚本用 `--idle`，空闲才返回 0。

**活动记录**。`/activity [天数]`（默认今天，最多 30 天）回一段摘要：运行几次、失败几次、花费，对外请求放行/拒绝/退回/问你各多少，
审批和发给你的消息各多少，再列最近几次运行。后面附一个只读页面的链接，一小时内有效：每次运行是什么叫醒的、哪个角色、
结果、用时和花费，展开看这次运行的每个对外请求（只显示主机和路径，查询串不显示）和裁决理由、问过你的审批、发给你的消息。
数据全部来自宿主自己写的表，不靠电脑里的程序上报。页面挂在接管页面的端口上（`SMRA_TAKEOVER_PORT`，没开浏览器也开；
设 -1 就只有摘要没有链接），没有脚本，内容全部转义。

**花费**。`/usage [天数]`（默认 7 天，最多 30 天，从本地日期的零点算）：总花费和调用次数、今天占每日上限多少、
按天的数字和小条形图、按角色、花得最多的 5 个会话（类型、角色、第一句话、运行几次、会话号），压缩长对话的花费单独一行。
数字来自 `inference_log`，和每日上限是同一本账。守门的小模型（注入检测、看图）的调用不经过这本账，不在里面。

**备份与恢复**。

```bash
uv run python -m host.backup create              # 服务开着也行；默认写到数据目录旁边的 backups/smra-<时间>.tar.gz
uv run python -m host.backup verify 备份文件      # 解到临时目录检查一遍
systemctl stop smra-agent
uv run python -m host.backup restore 备份文件     # 宿主在跑时拒绝；会先问一句，--yes 跳过
systemctl start smra-agent
```

数据库用 SQLite 的在线备份 API 拿一致的快照，服务不用停。备份里是数据库加数据目录里的其他文件（服务档案、Webhook 来源、
浏览器登录状态、快照、发出的文件），**不含主密钥**：凭证在库里是加密的，备份和钥匙分开放。恢复到新机器时要另外配好原来的主密钥，
恢复命令最后会说当前的密钥能不能解开备份里的凭证。备份里有对话记录和浏览器的登录状态，文件权限是 600，自己保管好。
microVM 可以重建的系统盘、核心盘、内核不进备份，主目录盘 `vm/home.ext4` 进。docker 驱动下电脑的主目录在 docker 卷 `smra-home` 里，
不在数据目录，要另外备份，比如 `docker run --rm --network none --user 0 -v smra-home:/h:ro -v "$PWD/backups":/b smra-cell:dev tar czf /b/home.tgz -C /h .`。

恢复不删东西：现有数据目录整个改名成 `data.pre-restore-<时间>`，备份解到原处，原来目录里的 `master.key` 搬回来。
备份里只许有普通文件和目录，路径不能跑出数据目录，数据库要过 `integrity_check`，版本不能比代码新，不合格就不动现有数据。
宿主运行期间一直拿着 `data/host.lock`，恢复和第二个宿主进程看到锁就不动这个目录。

**数据库版本**。库的版本号存在 `PRAGMA user_version`，启动时按顺序跑没跑过的迁移（`host/db.py` 的 `MIGRATIONS`），
每个迁移一个事务，失败整个回滚。版本号出现之前的库按"缺列就补"升到版本 1。库比代码新（比如回退了代码）时拒绝启动，
不会拿老代码去写新结构。

**停止**。收到 SIGTERM 后宿主不再取新事件，先等手上的运行做完（最多 `SMRA_STOP_GRACE_S`，默认 300 秒；
单元里 `TimeoutStopSec=360`），期间照常收消息、发回复；然后停后台任务，关浏览器、接管页、出网代理和端口，最后关数据库。
超时没做完的事件留在收件箱里，下次启动时重新处理；被强行杀掉时没结束的运行，下次启动时记为失败。
`KillMode=mixed` 必须有：默认的 `control-group` 会把停止信号同时发给整组进程，浏览器比宿主先被杀，录像也写不完。

安全相关的两件事（为什么宿主还是 root、主密钥放哪）在 [安全模型](security.md) 里。

**保留期限**。宿主启动时和之后每 24 小时清一次过期数据，0 表示永久保留：

| 环境变量 | 默认 | 清什么 |
|---|---|---|
| `SMRA_RETAIN_INFERENCE_DAYS` | 14 | 模型请求日志的请求和回复正文（含邮件等明文，库里的大头）；只留花费，每日花费上限照常算 |
| `SMRA_RETAIN_DAYS` | 365 | 已关闭会话的记录和全文索引（还有跟进要回来的会话不清）、处理完的收件箱/发件箱/审批/提议/连接请求、出网记录 |
| `SMRA_RETAIN_FILE_DAYS` | 30 | `data/uploads`、`data/outgoing` 里的暂存文件（还在等发送的附件不删） |

定时器、授权、已连接的账号、运行记录（只有花费和结果）不清。清掉的超过库的四分之一时顺手 `VACUUM`，文件真的变小。
电脑主目录（`home/`）里的东西归 Agent 和你管，不在这里清，快照也不清。

## microVM（第 9 步）

`SMRA_DRIVER=firecracker` 时，电脑是一台 Firecracker microVM：

| | 内容 |
| --- | --- |
| 系统盘 `data/vm/rootfs.ext4` | Python、anthropic SDK、git、1 号进程脚本。只读，客户机里叠一层内存可写层，冷启动清空 |
| 主目录盘 `data/vm/home.ext4` | `/home/agent`，读写，永久。大小就是磁盘配额（默认 8 GiB，稀疏文件） |
| 核心盘 `data/vm/core.ext4` | 运行程序和客户机代理程序。只读，每次冷启动由宿主重建 |
| vsock | 唯一的通道。没有网卡 |

在一台有 KVM 的 Linux 上准备镜像（需要 docker、e2fsprogs、curl），然后启动：

```bash
sh cell/vm/build-images.sh
```

```bash
sudo SMRA_DRIVER=firecracker uv run python -m host.main --channel console
```

经监禁程序（jailer）启动要求宿主是 root；开发时可以 `SMRA_FC_JAILER=0`，以当前用户直接启动（要能读写 `/dev/kvm`）。

**休眠**：最后一次运行结束后空闲 10 分钟（`SMRA_IDLE_SUSPEND_S`），宿主把电脑挂起：
让客户机把缓存写回盘、暂停、把内存和设备状态写成两个文件、结束监视器进程。有事件时起一个新的监视器加载它们，接着跑，
然后对时。Agent 起长任务前可以调 `keep_awake`，最长 4 小时（`SMRA_KEEP_AWAKE_MAX_S`），到点照常挂起。
挂起由工作进程在两批事件之间做，不会和运行同时发生。这套规则对所有驱动都生效：docker 驱动挂起是冻结容器（内存还占着），
local 驱动只是记个状态。

**量出来的数**（冷启动、挂起、恢复耗时、状态文件大小）每次都追加到 `data/vm/metrics.jsonl`。还没有真机上的数据。
