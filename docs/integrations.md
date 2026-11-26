# 接入外部服务

[← 回到 README](../README.md)

## 连接外部账号

凭证在宿主上的终端里输入，不经过聊天，电脑和模型都看不到：

```bash
uv run python -m host.creds add gmail --oauth
```

静态令牌（API key、个人访问令牌）去掉 `--oauth`。没有档案的服务要用 `--hosts` 指定令牌只发往哪些主机，
或者用 Agent 发来的 `--request <请求号>`。`list` 列出、`requests` 列出待处理的连接请求、`remove <服务>` 断开。

### 连接 Gmail

Gmail 要你自己在 Google Cloud 里建一个 OAuth 客户端，这一步只能你来做（大约 10 分钟，免费）：

1. 打开 [Google Cloud 控制台](https://console.cloud.google.com/)，新建一个项目（名字随便，比如 smra）。
2. "API 和服务" → "库"，搜 Gmail API，启用。
3. "Google Auth Platform"（旧版叫"OAuth 同意屏幕"）：用户类型选"外部"，应用名、支持邮箱、开发者邮箱填你自己的；
   "目标对象"里把你要连接的 Gmail 地址加为测试用户。
4. "客户端" → "创建客户端"，应用类型选**桌面应用**，记下客户端 ID 和客户端密钥。
5. **发布应用**："目标对象"里点"发布应用"，改成"正式版"。不发布的话（"测试"状态），refresh token 七天后失效，
   到时 Agent 读不了邮件。发布后不用提交 Google 审核：只有你自己用，授权时会看到"Google 尚未验证此应用"，
   点"高级" → "转至……（不安全）"继续即可。

然后在宿主上运行（不需要先拿 refresh token）：

```bash
uv run python -m host.creds add gmail --google-login
```

按提示填客户端 ID、客户端密钥（不回显），命令打印一个授权链接。在你自己电脑的浏览器里打开，登录要连接的账号、同意授权。
浏览器会跳到一个打不开的 `http://127.0.0.1:53682/?...` 页面，这是正常的：把地址栏里的完整网址复制下来粘回终端，
命令拿里面的授权码去换 refresh token，加密存进宿主的数据库。整个过程不经过聊天，电脑和模型都看不到。
申请的权限是 `gmail.modify`：读、发、存草稿、改标签、移到垃圾箱，不含永久删除。

已经有 refresh token 的话，用 `--oauth` 直接填。要断开：`uv run python -m host.creds remove gmail`，
再到 [Google 账号的第三方访问](https://myaccount.google.com/permissions) 里移除这个应用。

连接之后：

- 电脑里只有替身令牌 `sur_gmail_…`。只有当请求发往 Gmail 的主机、而且裁决放行时，出网代理才把它换成真凭证；
  发到别处就是一串无用的字符。
- 读邮件直接放行，但验证码、密码重置链接、免密登录链接会被抹掉；任何回显出来的真凭证也会被抹掉。
- 发邮件要你批准。审批里的收件人（包括抄送、密送）、主题、正文，都是从实际要发出的请求里解出来的，
  "仅这一次"的批准绑定这些字节。长期授权只覆盖同样的收件人。
- 删除这类不可撤销的操作只能一次一批。
- 某次运行读到疑似注入的内容时，这次运行里的长期授权全部暂停，你会收到提醒。
- **授权失效**（Google 回 `invalid_grant`：测试状态 7 天到期、你撤销了授权、改了密码等）：宿主把这个连接标成失效，
  给你发一条提醒，附重连命令，同一个连接只提醒一次。之后不再去刷新，Agent 在已连接的服务里看到它暂时用不了。
  令牌端点暂时出错（网络、5xx）不算失效，下次照常重试。重连就是再跑一次 `--google-login`，
  client id 和 secret 直接回车沿用已保存的，只需要在浏览器里再授权一次。`creds list` 会标出失效的连接。

### 连接 Google 日历和云盘

用连 Gmail 时建的同一个 Google Cloud 项目和 OAuth 客户端：

1. "API 和服务" → "库"，再启用 **Google Calendar API** 和 **Google Drive API**。
2. "Google Auth Platform" → "数据访问"，添加下面用到的权限（不加也能授权，只是同意页面上不列出来）。
3. 在宿主上分别连接，client id 和 secret 回车就沿用 Gmail 的：

```bash
uv run python -m host.creds add gcalendar --google-login
uv run python -m host.creds add gdrive --google-login
```

每个服务各自一个连接、各自一个 refresh token，可以单独断开。申请的权限：

| 服务 | 权限 | 能做什么 |
|---|---|---|
| gcalendar | `calendar.events`、`calendar.readonly` | 读所有日历；新建、修改、删除日程 |
| gdrive | `drive` | 读、上传、改名、移动、分享、删除云盘里的文件 |

其他 Google 服务没有默认权限，要用 `--scope` 指定。日历和云盘共用主机 `www.googleapis.com`，
出网代理按请求带的替身令牌认出是哪个服务、换成哪个真令牌。

审批怎么问：

- 读取直接放行。
- **日历**：新建和修改日程要批准，审批里写标题、开始结束时间、地点、说明和参与人。拉了别人（attendees）的日程
  会给对方发邀请，所以长期授权按参与人算：批准过"（只有你自己）"的日程，以后拉人的日程还会再问；
  修改时没动参与人的，记作"（参与人不变）"。删除日程只能一次一批。
- **云盘**：新建、上传、改名、移动、移到垃圾箱要批准；分享按"分享给谁"算，`anyone` 表示任何拿到链接的人，
  长期授权只覆盖同一个人。永久删除（不经过垃圾箱）只能一次一批。

注意：Agent 读到的日程和文件内容会交给你配置的主模型提供方。不想让模型看到的文件不要放在连接的云盘里，
或者不连接云盘。

### 连接 GitHub、Notion、Slack

仓库自带这三份档案（`profiles/github.yaml`、`notion.yaml`、`slack.yaml`），用的都是你自己建的长期令牌，
在宿主上连接，令牌从终端输入，不经过聊天：

```bash
uv run python -m host.creds add github    # fine-grained personal access token，只勾要用的仓库和权限
uv run python -m host.creds add notion    # 内部集成的令牌；再在要用的页面"连接"菜单里加上这个集成
uv run python -m host.creds add slack     # Slack 应用的 Bot User OAuth Token（xoxb-...）
```

令牌能做的事以你在服务那边给的权限为上限；档案再决定哪些直接放行、哪些要问你：

| 服务 | 直接放行 | 要批准 | 每次单独批准 |
|---|---|---|---|
| GitHub | 所有 GET | 开 issue / PR、评论、评审（长期授权按仓库算）；改 issue、加标签、提交文件、GraphQL | 合并 PR、删除、档案没列出的写操作 |
| Notion | GET、搜索（`POST /v1/search`）、查数据库（`POST .../query`） | 新建和修改页面、追加内容、改数据库结构、删块（进垃圾箱）、评论 | 档案没列出的写操作 |
| Slack | 列频道、读消息和回复、查人、搜索等只读方法 | 发消息（长期授权按频道算）、定时消息、上传文件、改消息、加表情 | 删消息、档案没列出的方法 |

- Slack 的方法名在路径里，而且同一个方法 GET 和 POST 都收，所以档案按方法名分，不按 HTTP 方法：
  `GET chat.postMessage?channel=...&text=...` 照样是发消息。
- Notion 每个请求都要带 `Notion-Version`，档案里写成固定请求头（`auth.headers`），出网代理换凭证时补上；
  电脑自己带了就用它的。只有仓库自带的和你自己放进 `data/profiles` 的档案能写固定请求头，Agent 提交的不行；
  你批准了 Agent 改写的同名档案，沿用仓库档案的固定请求头。
- GraphQL 查询和修改走同一个地址，分不出来，所以每次都问；Agent 被告知尽量用 REST。

注意：Agent 读到的仓库、页面和消息会交给主模型。

## 让 Agent 自己接入新服务

你说"我们用的那个工单系统，你接一下"，之后你只需要做两件事：

1. **填令牌。** Agent 读完接口文档后发来一条连接请求，写明令牌只会发往哪些主机、放在哪个请求头。你在宿主上运行消息里给的命令：

   ```bash
   uv run python -m host.creds add tickets --request <请求号>
   ```

   命令再给你看一遍这些主机，你确认后从终端输入令牌。宿主几秒内发现，Agent 在原来的对话里接着做。
   此时已经能用：GET 直接放行，其余请求每次问你，审批里是原始请求。
2. **确认档案。** Agent 摸清接口后提交一份服务档案。你收到的审阅文字由宿主从档案本身生成，不转述 Agent 的说法；
   危险的地方单独用 ⚠ 标出：会改东西的请求被标成"读取"、范围过宽的写规则、档案里超出绑定范围的主机、
   比现有档案放松的规则、被去掉的响应过滤。用 `/profile approve <编号>` 确认（Telegram 里是按钮），确认之前不生效。

两条硬规则：令牌的绑定主机以你在终端里确认的为准，档案只能收窄、不能扩大；Agent 永远拿不到真令牌。

Agent 也可以用 `webhook_request` 申请开通一个 Webhook 来源。你 `/webhook approve <编号>` 后，宿主生成签名密钥，
只在给你的回复里出现，你去对方系统里配置。

**技能**是主目录 `skills/<名字>/` 下的一个目录（`SKILL.md` 加脚本），名字和一句话说明出现在 Agent 的 `<memory>` 里。
**例行任务**是技能加一个日程（`set_timer` 的 `skill`）或一个 Webhook 来源（`webhook_request` 的 `skill`），
到时候事件里会写明"先读哪个 SKILL.md"。设定时和申请时会检查这个 `SKILL.md` 存在，没有就不让设。
分工：技能写"怎么做"，日程的 `task` 写"这次做什么"；做法改了只改 `SKILL.md`，用它的日程一起生效。
你也可以随时 `/skill <技能名> [要求]` 让 Agent 照某个技能做一次，这条会作为普通消息进当前对话。

### 自带的技能

仓库的 `skills/` 下有三个现成的技能，第一次运行前宿主把它们放进主目录，之后就是 Agent 的：它可以照你的反馈改。
放进去不等于开始用：没有日程，什么都不会发生。你对 Agent 说"每天早上 8 点给我发简报""帮我盯着邮件"，它才设日程。

| 技能 | 做什么 | 要连接 | 默认日程 |
|---|---|---|---|
| `morning-brief` | 早间简报：今天的日程、要处理的邮件、手上没做完的事，一条消息 | gcalendar，可选 gmail | `0 8 * * *` |
| `mail-watch` | 盯邮件：有新邮件才叫醒 Agent，重要的马上说，其余进每日摘要 | gmail | `*/30 8-22 * * *` |
| `calendar-watch` | 盯日历：日程开始前一小时内提醒，带地点、会议链接、参与人 | gcalendar | `*/15 7-22 * * *` |

- 两个"盯"的技能带前置检查脚本（`check.py`，只用标准库）：没有新邮件、没有快开始的日程时退出码 10，
  不调模型、不产生运行。检查脚本只能读（拿到的是替身令牌，发信之类的请求照样被拒），
  `mail-watch` 的检查只调列表接口，拿到的只有邮件 id。筛选条件、提前多久提醒写在 `work/<技能>/config.json`，见各自的 `SKILL.md`。
- `mail-watch` 和简报的邮件部分会读邮件，读到的内容和别的内容一样交给你配置的主模型提供方。
  只连接了日历时，简报没有邮件部分。
- `/skills` 列出自带技能和它们在主目录里的状态。宿主看不到主目录，状态记在宿主的数据库里：
  - 主目录里原来就有同名目录的，不动；
  - Agent 改了、删了的，不覆盖、不装回来；
  - 仓库里的技能更新了，只在 `/skills` 里提示，`/skills reset <名字>` 才用新版覆盖（Agent 对它的改动会丢）。
- 不想要自带技能：`.env` 里 `SMRA_BUNDLED_SKILLS=0`（已经放进去的留在主目录，让 Agent 删掉即可）。

## Webhook

**Webhook** 发到 `http://<webhook 地址>:8789/webhook/<来源名>`，用 HMAC-SHA256 签名。来源在 `data/webhooks.yaml` 里配置：

```yaml
tickets:
  title: 工单系统
  secret: <至少 16 个字符>
  correlation: ticket.id        # 关联键：同一张工单的事件进同一会话
```

Webhook 的内容被标成外部内容，模型代理会对它做注入检测。它监听在单独的端口上，要暴露到公网时只暴露这个端口。
