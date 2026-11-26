# 参与贡献

欢迎 issue 和 PR，中文或英文都可以。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发环境

```bash
uv sync
uv run playwright install chromium   # 浏览器相关测试需要；有本机 Edge 也可以
```

## 提交 PR 之前

```bash
uv run pytest -q     # 全部测试
uv run ruff check    # 代码检查
uv run mypy          # 类型检查（host/）
```

三项都要通过。容器驱动相关的改动再跑一下 `SMRA_TEST_DOCKER=1 uv run pytest tests/test_docker.py -s`（要 Docker）。

## 约定

- **测试不调用真实模型、不连真实服务。** 模型提供方和外部网站都用 `tests/` 里本机的假服务代替。
  需要真实模型的东西放在 `evals/`，不进自动化测试。
- **安全边界的改动要带对抗测试。** 改出网代理、裁决者、凭证、宿主接口时，加一个"电脑里的程序拿着能拿到的一切去攻击"的测试，
  证明它做不到。现有的例子见 `tests/test_step3*.py`、`tests/test_step4*.py`。
- **`cell/` 的改动要重建电脑镜像**，请在 PR 里说明。
- 仓库内容保持通用：不写某个人的偏好、私有项目名或部署环境的细节；示例用 `example.com` 之类的保留域名。
- 文档和代码注释目前用中文。改了行为就同步改 `docs/` 里对应的文档，并在 `CHANGELOG.md` 的"未发布"一节记一笔。
- 提交信息写清楚"改了什么、为什么"。
