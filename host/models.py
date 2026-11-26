"""模型提供方。不和某一家绑定。

电脑里的运行程序只会说一种话：Anthropic Messages 协议（用官方 SDK）。宿主的模型代理负责把它送到你配置的提供方：
  - protocol = anthropic：原样转发（Anthropic 官方，或 DeepSeek、Kimi、GLM 等提供的 Anthropic 兼容接口）；
  - protocol = openai：翻译成 OpenAI Chat Completions 再翻译回来（OpenAI、通义、OpenRouter、本地 vLLM/Ollama 等）。

各家支持的功能不一样，用 features 声明。运行程序按 features 决定请求里带哪些字段，不支持的就不发：
  stream            流式响应
  thinking          思考（adaptive）
  effort            output_config.effort
  cache             提示词缓存断点 cache_control
  betas             Anthropic 的 beta 功能（思考块绑定、拒绝时换后备模型）
  compaction        服务端上下文压缩
  text_editor       Anthropic 定义的文本编辑器工具（否则用同名的普通工具）
  web_search        服务端网页搜索（否则不提供这个工具，Agent 用 fetch）
  structured_output 注入检测用结构化输出（否则要求模型只输出 JSON，再宽松地解析）
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

ALL_FEATURES = frozenset({"stream", "thinking", "effort", "cache", "betas", "compaction",
                          "text_editor", "web_search", "structured_output", "vision"})
# vision：主模型能看工具结果里的图片（浏览器截图）。看不了的话宿主把图换成一句说明。


@dataclass(frozen=True)
class Preset:
    protocol: str
    upstream: str
    model: str
    guard_model: str
    features: frozenset
    key_env: str
    max_tokens: int
    guard_vision: bool      # 注入检测用的模型能不能看图


PRESETS: dict[str, Preset] = {
    "anthropic": Preset("anthropic", "https://api.anthropic.com", "claude-opus-5-5", "claude-haiku-5-5",
                        ALL_FEATURES, "ANTHROPIC_API_KEY", 64000, True),
    # https://api-docs.deepseek.com/guides/anthropic_api
    # deepseek-v4-pro 不处理图片（换成 [Unsupported Image]）；deepseek-flash 能看用户消息和工具结果里的图片
    "deepseek": Preset("anthropic", "https://api.deepseek.com/anthropic", "deepseek-v4-pro", "deepseek-flash",
                       frozenset({"stream", "thinking", "effort"}), "DEEPSEEK_API_KEY", 32000, True),
    # 任何 OpenAI 兼容的接口：改 SMRA_MODEL_UPSTREAM 和 SMRA_MODEL 即可
    "openai": Preset("openai", "https://api.openai.com/v1", "", "", frozenset(), "OPENAI_API_KEY", 16000, False),
}


def parse_features(text: str) -> frozenset:
    names = {f.strip() for f in text.split(",") if f.strip()}
    unknown = names - ALL_FEATURES
    if unknown:
        raise ValueError(f"不认识的模型功能：{', '.join(sorted(unknown))}。可选：{', '.join(sorted(ALL_FEATURES))}")
    return frozenset(names)


def from_env(env=os.environ.get) -> dict:
    """读环境变量，返回 Config 里模型相关的字段。没设的按提供方预设补齐。"""
    provider = env("SMRA_MODEL_PROVIDER", "anthropic").strip().lower()
    if provider not in PRESETS:
        raise ValueError(f"不认识的模型提供方 {provider}。可选：{', '.join(PRESETS)}")
    p = PRESETS[provider]
    model = env("SMRA_MODEL", "") or p.model
    if not model:
        raise ValueError(f"提供方 {provider} 需要用 SMRA_MODEL 指定模型名")
    features = env("SMRA_MODEL_FEATURES")
    cache_read_weight = float(env("SMRA_CACHE_READ_WEIGHT", "") or 0.1)
    if not 0 <= cache_read_weight <= 1:
        raise ValueError("SMRA_CACHE_READ_WEIGHT 要在 0 到 1 之间")
    api_key = env("SMRA_MODEL_API_KEY", "") or env(p.key_env, "")
    search_provider = env("SMRA_SEARCH_PROVIDER", "deepseek" if provider == "deepseek" else "").strip().lower()
    if search_provider not in ("", "off", "deepseek"):
        raise ValueError(f"不认识的搜索提供方 {search_provider}。可选：deepseek、off")
    search_key = env("SMRA_SEARCH_API_KEY", "") or env("DEEPSEEK_API_KEY", "") \
        or (api_key if provider == "deepseek" else "")
    if search_provider == "off" or not search_key:
        search_provider = ""
    model_features = parse_features(features) if features is not None else p.features
    guard_vision = _flag(env("SMRA_GUARD_VISION", ""), p.guard_vision)
    stt_key = env("SMRA_STT_API_KEY", "")
    return dict(
        # 照片看图：默认在主模型看不了图、检查用的模型能看图时开
        describe_images=_flag(env("SMRA_DESCRIBE_IMAGES", ""), guard_vision and "vision" not in model_features),
        vision_model=env("SMRA_VISION_MODEL", ""),
        # 语音转文字：只填了密钥就用 OpenAI；国内可以用硅基流动（https://api.siliconflow.cn/v1，
        # 模型 FunAudioLLM/SenseVoiceSmall）或别的 OpenAI 兼容接口
        stt_upstream=env("SMRA_STT_UPSTREAM", "") or ("https://api.openai.com/v1" if stt_key else ""),
        stt_api_key=stt_key,
        stt_model=env("SMRA_STT_MODEL", "") or "whisper-1",
        search_provider=search_provider,
        search_upstream=(env("SMRA_SEARCH_UPSTREAM", "") or "https://api.deepseek.com/anthropic").rstrip("/"),
        search_api_key=search_key,
        search_model=env("SMRA_SEARCH_MODEL", "") or "deepseek-flash",
        search_max_per_run=int(env("SMRA_SEARCH_MAX_PER_RUN", "") or 20),
        model_provider=provider,
        model_protocol=env("SMRA_MODEL_PROTOCOL", "") or p.protocol,
        model_upstream=(env("SMRA_MODEL_UPSTREAM", "") or p.upstream).rstrip("/"),
        api_key=api_key,
        model=model,
        injection_model=env("SMRA_GUARD_MODEL", "") or p.guard_model or model,
        model_features=model_features,
        guard_vision=guard_vision,
        max_tokens=int(env("SMRA_MAX_TOKENS", "") or p.max_tokens),
        cache_read_weight=cache_read_weight,
    )


def _flag(text: str, default: bool) -> bool:
    text = text.strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"开关的值看不懂：{text}（用 1 或 0）")


def auth_headers(protocol: str, key: str) -> dict:
    if protocol == "openai":
        return {"authorization": f"Bearer {key}"}
    return {"x-api-key": key}


def endpoint(protocol: str, upstream: str) -> str:
    return f"{upstream}/chat/completions" if protocol == "openai" else f"{upstream}/v1/messages"


# ---------------------------------------------------------------- Anthropic -> OpenAI

def _text(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, dict) and b.get("type") == "document":
            src = b.get("source") or {}
            if src.get("type") == "text":
                parts.append(src.get("data", ""))
            else:
                parts.append("（附件文档，这个模型看不到内容）")
        elif isinstance(b, dict) and b.get("type") == "image":
            parts.append("（图片，这个模型看不到）")
    return "\n".join(parts)


def _user_parts(blocks: list) -> list | str:
    """用户消息里的文字和图片。图片转成 data URI；不支持图片的模型由提供方自己报错或忽略。"""
    parts: list[dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
            src = b["source"]
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}})
        elif b.get("type") in ("text", "document", "image"):
            parts.append({"type": "text", "text": _text([b])})
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def to_openai(body: dict, model: str) -> dict:
    """把运行程序发来的 Anthropic 请求翻译成 Chat Completions 请求。思考块不回传。"""
    messages: list[dict] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text(system)})
    for m in body.get("messages") or []:
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = _text([b for b in content if b.get("type") == "text"])
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in content if b.get("type") == "tool_use"]
            reply: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                reply["tool_calls"] = calls
            messages.append(reply)
            continue
        # 用户一侧：工具结果各自成一条 tool 消息，其余内容合成一条用户消息
        rest = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                inner = b.get("content")
                text = _text(inner) if inner is not None else ""
                if b.get("is_error"):
                    text = f"[错误] {text}"
                messages.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text})
            else:
                rest.append(b)
        if rest:
            messages.append({"role": "user", "content": _user_parts(rest)})
    out: dict = {"model": model, "messages": messages, "max_tokens": body.get("max_tokens", 4096)}
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t.get("input_schema") or {"type": "object"}}}
             for t in body.get("tools") or [] if t.get("type") in (None, "custom")]
    if tools:
        out["tools"] = tools
    for key in ("temperature", "top_p", "stop_sequences"):
        if key in body:
            out["stop" if key == "stop_sequences" else key] = body[key]
    return out


_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use",
         "length": "max_tokens", "content_filter": "refusal"}


def from_openai(data: dict, model: str) -> dict:
    """把 Chat Completions 的响应翻译回 Anthropic 的 Message。"""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}     # 交给运行程序的参数校验去报错，让模型重来
        content.append({"type": "tool_use", "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {"_raw": args}})
    stop = _STOP.get(choice.get("finish_reason") or "stop", "end_turn")
    if stop == "end_turn" and any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    usage = data.get("usage") or {}
    return {"id": data.get("id") or f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant",
            "model": data.get("model") or model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0),
                      "output_tokens": int(usage.get("completion_tokens") or 0)}}


def error_body(status: int, text: str) -> dict:
    """把提供方的错误包成 Anthropic 的错误格式，运行程序的 SDK 才能认出来。"""
    message = text
    try:
        data = json.loads(text)
        err = data.get("error")
        message = (err.get("message") if isinstance(err, dict) else err) or data.get("message") or text
    except (ValueError, AttributeError):
        pass
    kind = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 429: "rate_limit_error"}.get(status, "api_error")
    return {"type": "error", "error": {"type": kind, "message": str(message)[:2000]}}
