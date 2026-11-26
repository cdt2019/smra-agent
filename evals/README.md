# 评测

> **会调用真实模型，产生费用。** 自动化测试（`uv run pytest`）不会跑这里的任何东西。

| 脚本 | 做什么 | 要密钥 | 大约用量 |
|---|---|---|---|
| `notify_eval.py` | "发不发"评测：对 `notify_cases.yaml` 的 27 个用例各调一次模型，看它选即时通知、摘要还是保持安静 | 是 | 每个用例 1–3 次请求；全部跑一遍几十次请求 |
| `quality_score.py` | 给真实模型跑出来的结果打分（来源、引用、结论对不对），只做机械检查 | 否 | 不调用模型 |

```bash
uv run python evals/notify_eval.py                                # 全部用例，用 .env 里的提供方和模型
uv run python evals/notify_eval.py --only weather-rain --repeat 3 # 只跑一个用例，重复三次
uv run python evals/notify_eval.py --model <模型名> --effort low
```

结果写到 `evals/results/`（不进 git）。改了系统提示词、通知规则或者换了模型之后跑一遍。
用例格式和怎么读结果见 [Agent 什么时候说话](../docs/agent.md#发不发评测)。
