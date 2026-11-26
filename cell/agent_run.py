"""一次运行。由宿主侧的工作进程在电脑里启动，跑完即退出。

这个程序在不可信的一侧。它手里只有本次运行的通行证（RUN_TOKEN）：
  - 调模型时把通行证当作密钥发给模型代理，代理在宿主侧换成真密钥；
  - 读写会话记录、给用户发消息、发文件、检索过去的对话，都经宿主接口。
这里没有任何权限判断，那是出口的事。

为了和保留思考块的历史校验兼容，发给模型的历史只追加、不改写：
  - 系统提示词在会话内不变（AGENT.md 由宿主按会话冻结）；
  - 记忆文件不放进系统提示词，而是有变化时作为一条新消息追加。

退出码：0 正常结束；3 让路（稍后在同一会话里继续）；其他为失败。
"""
from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path

import anthropic

import cell_tools as ct

EXIT_OK, EXIT_YIELD, EXIT_DEFER = 0, 3, 4
MAX_STEPS = 50

COMPACTION_INSTRUCTIONS = (
    "把到目前为止的对话写成一份交接摘要，供你之后接着工作。必须保留："
    "用户最初和当前的目标；已经做出的决定及理由；已完成和未完成的事；"
    "主目录里相关文件的路径，尤其是进度文件；用户表达过的偏好和纠正。"
    "省略寒暄和已经无关的细节。用对话所用的语言写。"
)

HOST_URL = ""
RUN_TOKEN = ""


class HostError(RuntimeError):
    """宿主接口拒绝了请求。消息是宿主给的说明，原样交给模型。"""


def host(path: str, body: dict | None = None) -> dict:
    """调用宿主接口。"""
    request = urllib.request.Request(
        f"{HOST_URL}/host/{path}",
        data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {RUN_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            message = json.loads(raw)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = raw[:300]
        raise HostError(f"宿主接口 {path} 拒绝了（{e.code}）：{message}") from e


def log(type_: str, role: str, content) -> None:
    host("log_append", {"type": type_, "payload": {"role": role, "content": content}})


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


def tool_uses(content) -> list[dict]:
    if isinstance(content, str):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def build_system(ctx: dict, home: ct.Home, exec_allowed: bool) -> list[dict]:
    machine = "Linux 容器" if sys.platform.startswith("linux") else f"{platform.system()}（开发模式）"
    env = (
        "\n\n## 这台电脑\n"
        f"- 系统：{machine}\n"
        f"- 主目录：{home.root}（文件工具里的相对路径都相对于它）\n"
        f"- 命令行（exec 工具）：{'可用' if exec_allowed else '不可用'}\n"
    )
    # 系统提示词会话内不变，放一个缓存断点
    block = {"type": "text", "text": ctx["system"] + env}
    if "cache" in features_of(ctx):
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def features_of(ctx: dict) -> set[str]:
    """模型提供方支持的功能，宿主按配置给出。旧宿主没给就当作 Anthropic 官方，全开。"""
    if "features" in ctx:
        return set(ctx["features"])
    return {"stream", "thinking", "effort", "cache", "betas", "compaction", "text_editor", "web_search"}


def model_request(ctx: dict, system: list, tools: list, messages: list) -> dict:
    """按提供方支持的功能组请求。不支持的字段一律不发，免得被拒或被悄悄忽略。"""
    features = features_of(ctx)
    kwargs: dict = {"model": ctx["model"], "max_tokens": ctx.get("max_tokens", 64000),
                    "system": system, "tools": tools, "messages": messages}
    betas: list[str] = []
    if "thinking" in features:
        kwargs["thinking"] = {"type": "adaptive"}
        if "betas" in features:
            # 历史若意外变了，丢掉失效的思考块继续，而不是整个请求失败
            kwargs["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            betas.append("thinking-binding-controls-2026-08-01")
    if "effort" in features:
        kwargs["output_config"] = {"effort": ctx["effort"]}
    if "cache" in features:
        kwargs["cache_control"] = {"type": "ephemeral"}     # 自动缓存到最后一条消息
    if "compaction" in features:
        # 输入超过 15 万 token 时由服务端把较早的内容压缩成摘要
        kwargs["context_management"] = {"edits": [{
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": 150000},
            "instructions": COMPACTION_INSTRUCTIONS,
        }]}
        betas.append("compact-2026-01-12")
    if "betas" in features:
        kwargs["fallbacks"] = "default"                    # 模型按安全策略拒绝时换后备模型重试
        betas.append("server-side-fallback-2026-07-01")
    if betas:
        kwargs["betas"] = betas
    return kwargs


def call_model(client, ctx: dict, system: list, tools: list, messages: list):
    kwargs = model_request(ctx, system, tools, messages)
    api = client.beta.messages if "betas" in kwargs else client.messages
    if "stream" in features_of(ctx):
        with api.stream(**kwargs) as stream:
            return stream.get_final_message()
    return api.create(**kwargs, timeout=900)


def execute(call: dict, home: ct.Home, exec_allowed: bool) -> tuple[str | list, bool]:
    """执行一个工具调用，返回 (结果文本, 是否出错)。"""
    name, data = call["name"], call.get("input")
    schemas = {t["name"]: t.get("input_schema") for t in ct.tool_definitions(exec_allowed)}
    if name not in schemas:
        return f"没有名为 {name} 的工具", True
    if schemas[name]:
        problem = ct.validate(data, schemas[name])
        if problem:
            return f"INVALID_INPUT：{problem}。请重新生成完整的调用。", True
    try:
        if name == "str_replace_based_edit_tool":
            return ct.text_editor(home, data), False
        if name == "exec":
            return ct.run_command(home, data), False
        if name == "fetch":
            return ct.fetch(home, data), False
        if name == "recall":
            return ct.recall(host, data), False
        if name == "web_search":
            return ct.web_search(host, data), False
        if name == "send_file":
            return ct.send_file(host, home, data), False
        if name == "notify":
            return ct.notify(host, data), False
        if name == "set_timer":
            return ct.set_timer(host, home, data), False
        if name == "list_timers":
            return ct.list_timers(host, data), False
        if name == "cancel_timer":
            return ct.cancel_timer(host, data), False
        if name == "connection_request":
            return ct.connection_request(host, data), False
        if name == "profile_propose":
            return ct.profile_propose(host, data), False
        if name == "webhook_request":
            return ct.webhook_request(host, home, data), False
        if name == "browser":
            return ct.browser(host, data), False
        if name == "handoff":
            return ct.handoff(host, data), False
        if name == "keep_awake":
            return ct.keep_awake(host, data), False
    except (ct.ToolError, HostError) as e:
        return str(e), True
    except Exception as e:      # 工具失败原样告诉模型，由它决定怎么办
        return f"{type(e).__name__}: {e}", True
    return f"没有名为 {name} 的工具", True


def run_tools(calls: list[dict], home: ct.Home, exec_allowed: bool) -> list[dict]:
    results = []
    for call in calls:
        text, is_error = execute(call, home, exec_allowed)
        # 截图是内容块列表（图片加文字），原样交给模型；其余是文字，太长的存成文件
        content = text if isinstance(text, list) else ct.bound_output(text, home, call["id"])
        block = {"type": "tool_result", "tool_use_id": call["id"], "content": content}
        if is_error:
            block["is_error"] = True
        results.append(block)
    return results


def main() -> int:
    global HOST_URL, RUN_TOKEN
    HOST_URL = os.environ["HOST_URL"].rstrip("/")
    RUN_TOKEN = os.environ["RUN_TOKEN"]
    home = ct.Home(Path(os.environ["AGENT_HOME"]))
    exec_allowed = os.environ.get("CELL_EXEC") == "1"

    ctx = host("run_context")
    # 每个角色有自己的记忆目录；默认角色是 memory/
    memory_dir = ctx.get("memory_dir") or "memory"
    ct.ensure_layout(home, memory_dir)
    # 已连接的服务：只有替身令牌。命令行里的程序也能用（环境变量）
    ct.CONNECTIONS.update({c["service"]: c for c in ctx.get("connections", [])})
    for name, c in ct.CONNECTIONS.items():
        os.environ[f"SMRA_CRED_{name.upper()}"] = c["surrogate"]
    messages: list[dict] = ctx["messages"]
    if not messages:
        return EXIT_OK
    # 前台运行的最终答复直接回给用户；后台运行的最终文字只留在记录里，要说话得调 notify
    foreground = ctx.get("foreground", True)

    last = messages[-1]
    if last["role"] == "assistant":
        calls = tool_uses(last["content"])
        if not calls:
            # 上一次运行已经得出答复，只是没来得及收尾。不再调模型，把同一份答复再交给发件箱，
            # 发件箱按去重键保证只送达一次。
            answer = text_of(last["content"])
            if answer and foreground:
                host("send", {"text": answer})
            return EXIT_OK
        # 上一次运行死在执行工具的过程中：这些调用的结果未知
        unknown = [{"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                    "content": "这次工具调用执行期间运行被中断，结果未知。需要的话先检查当前状态，再决定是否重做。"}
                   for c in calls]
        log("tool_result", "user", unknown)
        messages.append({"role": "user", "content": unknown})
    else:
        note = ct.memory_note(home, ctx.get("recent_notices"), memory_dir)
        if note != ctx.get("last_context_note"):
            log("context_note", "user", note)
            messages.append({"role": "user", "content": note})

    system = build_system(ctx, home, exec_allowed)
    tools = ct.tool_definitions(exec_allowed, features_of(ctx))
    # base_url 指向宿主侧的模型代理；api_key 位置放的是通行证，不是真密钥
    client = anthropic.Anthropic(base_url=f"{HOST_URL}/model", api_key=RUN_TOKEN)

    for _ in range(MAX_STEPS):
        message = call_model(client, ctx, system, tools, messages)
        content = message.to_dict()["content"]
        # 完整内容（包括思考块和压缩块）原样入账、原样回放，不能只存文本
        log("assistant_message", "assistant", content)
        messages.append({"role": "assistant", "content": content})

        if message.stop_reason == "refusal":
            host("send", {"text": "这个请求被模型拒绝了，我没法继续处理它。", "urgency": "digest"})
            return EXIT_OK
        if message.stop_reason == "pause_turn":
            continue        # 服务端工具（网页搜索）没做完，原样带上内容再请求一次

        calls = tool_uses(content)
        if message.stop_reason != "tool_use" or not calls:
            answer = text_of(content)
            if message.stop_reason == "max_tokens":
                answer = (answer + "\n\n（输出达到长度上限，被截断了。）").strip()
            if answer and foreground:
                host("send", {"text": answer})
            return EXIT_OK      # 没有要说的就保持安静；后台运行的结论只留在记录里

        results = run_tools(calls, home, exec_allowed)
        # 同一轮的全部工具结果放在一条消息里
        log("tool_result", "user", results)
        messages.append({"role": "user", "content": results})

        verdict = host("should_yield")
        if verdict["yield"]:
            # 有请求在等用户批准：先结束，用户处理后会有新事件把这个会话接上
            return EXIT_DEFER if verdict.get("reason") == "deferred" else EXIT_YIELD

    host("send", {"text": f"这次处理超过了 {MAX_STEPS} 步的上限，我先停在这里。进度见主目录里的进度文件。",
                  "urgency": "digest"})
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except anthropic.RateLimitError as e:
        print(f"模型限流：{e.message}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIStatusError as e:
        print(f"模型接口错误 {e.status_code}：{e.message}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIConnectionError as e:
        print(f"连不上模型代理：{e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
