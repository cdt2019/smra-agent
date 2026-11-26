# Security Policy / 安全问题报告

smra-agent 的核心承诺是：电脑（隔离区）里的程序拿不到真凭证，也不能绕过宿主把数据发出去。
任何能打破这一点的问题我们都当作安全漏洞处理。

## 报告方式

**请不要开公开 issue。** 用 GitHub 的 [Private vulnerability reporting](../../security/advisories/new)
（仓库页面 Security → Report a vulnerability）私下提交。中文或英文都可以。

请尽量写清楚：

- 影响的版本或提交
- 使用的驱动（docker / firecracker / local）和渠道
- 复现步骤，最好附一个电脑里运行的最小脚本
- 你认为的影响（拿到了什么、发出了什么）

我们会在 7 天内回复确认，修复后在 [CHANGELOG](CHANGELOG.md) 里致谢（你不愿意署名的话除外）。

## 范围

属于漏洞的，例如：

- 电脑里的程序拿到真凭证、模型密钥、主密钥，或者宿主上的其他文件
- 绕过出网代理或裁决者：连到内网 / 云元数据地址、不经批准发出写请求、批准内容和实际发出的请求不一致
- 替身令牌被发往未绑定的主机
- 伪造或答复审批、冒充宿主消息、调用宿主接口里不允许的动作
- 登录接管页面被未授权的人访问或操作
- 从电脑逃逸到宿主（容器 / microVM）

不算漏洞的：

- **local 驱动**下的任何问题：它没有隔离，只用于开发
- 需要先拿到宿主 root 或 `.env` 的攻击
- 你自己批准了的请求造成的后果
- 模型提供方或第三方服务本身的问题

已知的设计取舍（例如宿主以 root 运行的原因）见 [安全模型](docs/security.md)。

## English

Please report vulnerabilities privately via GitHub's *Private vulnerability reporting* (Security → Report a vulnerability),
not via public issues. In scope: anything that lets code inside the sandbox obtain real credentials, bypass the egress
proxy or the approval flow, forge approvals, or escape to the host. The `local` driver has no isolation and is out of scope.
We aim to acknowledge reports within 7 days.
