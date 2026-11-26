""""发不发"评测。设计文档第 14.3 节。

对 notify_cases.yaml 里的每个用例，用和真实运行相同的系统提示词、工具定义和消息顺序
（事件，然后 <memory>）调一次模型，看它调了 notify(now)、notify(digest)，还是保持安静。
评测里没有真的网络和主目录：fetch / recall / 读文件统一返回用例里的 lookup。

会调用真实模型，要花钱。用例约 27 个，每个通常 1-3 次请求。

    uv run python evals/notify_eval.py                 # 全部用例
    uv run python evals/notify_eval.py --only weather-rain --repeat 3
    uv run python evals/notify_eval.py --model claude-sonnet-5-5 --effort low
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "cell")]

import anthropic  # noqa: E402
import cell_tools as ct  # noqa: E402
from ruamel.yaml import YAML  # noqa: E402

from host.config import BASE_SYSTEM, DEFAULT_AGENT_MD, load_dotenv  # noqa: E402

CASES = Path(__file__).with_name("notify_cases.yaml")
RESULTS = Path(__file__).with_name("results")
MAX_STEPS = 4
LABELS = ("now", "digest", "silent")

NO_INFO = "（评测环境：没有更多信息了。请根据已有信息做决定。）"


def load_cases(path: Path = CASES) -> list[dict]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def memory_for(case: dict) -> str:
    """和真实运行一样的 <memory> 消息，内容来自用例的背景。"""
    background = case.get("background") or {}
    with tempfile.TemporaryDirectory() as tmp:
        home = ct.Home(Path(tmp))
        ct.ensure_layout(home)
        if background.get("instructions"):
            (home.root / "memory/instructions.md").write_text(
                f"# 长期指令\n\n- {background['instructions']}\n", encoding="utf-8")
        if background.get("notes"):
            (home.root / "memory/index.md").write_text(
                f"# 笔记索引\n\n- notes/about-user.md — {background['notes']}\n", encoding="utf-8")
        notices = [{"kind": "now", "body": text, "sent_at": "2026-11-18T00:00", "feedback": None}
                   for text in case.get("recent") or []]
        return ct.memory_note(home, notices)


def system_prompt() -> list[dict]:
    env = "\n\n## 这台电脑\n- 系统：Linux 容器\n- 主目录：/home/agent\n- 命令行（exec 工具）：不可用\n"
    return [{"type": "text", "text": f"{BASE_SYSTEM}\n\n{DEFAULT_AGENT_MD.strip()}{env}",
             "cache_control": {"type": "ephemeral"}}]


def tools() -> list[dict]:
    # 和真实运行相同的工具，只去掉服务端网页搜索（评测里不该真的去搜）
    return [t for t in ct.tool_definitions(exec_allowed=False) if t.get("type") != ct.WEB_SEARCH_TOOL["type"]]


def fake_tool(call, case: dict, sent: list) -> tuple[str, bool]:
    name, data = call.name, call.input if isinstance(call.input, dict) else {}
    if name == "notify":
        if data.get("urgency") not in ("now", "digest") or not data.get("text"):
            return "INVALID_INPUT：要有 text 和 urgency（now 或 digest）", True
        sent.append({"urgency": data["urgency"], "text": data["text"]})
        return "已交给发件箱", False
    if name == "send_file":
        sent.append({"urgency": "digest", "text": f"[文件] {data.get('path')}"})
        return "已交给发件箱", False
    if name in ("fetch", "recall", "str_replace_based_edit_tool"):
        if name == "str_replace_based_edit_tool" and data.get("command") != "view":
            return "已修改（评测环境）", False
        return case.get("lookup") or NO_INFO, False
    if name in ("set_timer", "list_timers", "cancel_timer"):
        return "完成（评测环境）", False
    return NO_INFO, True


async def run_case(client, case: dict, model: str, effort: str) -> dict:
    messages = [{"role": "user", "content": case["event"].strip()},
                {"role": "user", "content": memory_for(case)}]
    sent: list[dict] = []
    tokens = 0
    for _ in range(MAX_STEPS):
        async with client.messages.stream(
            model=model, max_tokens=16000, system=system_prompt(), tools=tools(), messages=messages,
            thinking={"type": "adaptive"}, output_config={"effort": effort},
        ) as stream:
            message = await stream.get_final_message()
        usage = message.usage
        tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0) \
            + (usage.cache_read_input_tokens or 0) + (usage.cache_creation_input_tokens or 0)
        messages.append({"role": "assistant", "content": [b.to_dict() for b in message.content]})
        calls = [b for b in message.content if b.type == "tool_use"]
        if message.stop_reason != "tool_use" or not calls:
            break
        results = []
        for call in calls:
            text, is_error = fake_tool(call, case, sent)
            block = {"type": "tool_result", "tool_use_id": call.id, "content": text}
            if is_error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})
    urgencies = {s["urgency"] for s in sent}
    got = "now" if "now" in urgencies else "digest" if "digest" in urgencies else "silent"
    return {"id": case["id"], "expect": case["expect"], "got": got, "sent": sent, "tokens": tokens}


def summarize(results: list[dict]) -> dict:
    total = len(results)
    correct = sum(r["got"] == r["expect"] for r in results)
    quiet = [r for r in results if r["expect"] == "silent"]
    needed_now = [r for r in results if r["expect"] == "now"]
    interrupts = [r for r in results if r["got"] == "now"]
    return {
        "total": total,
        "accuracy": correct / total if total else 0.0,
        # 该安静时安静了吗：第 5 步的验收线看这一项
        "quiet_when_should": sum(r["got"] == "silent" for r in quiet) / len(quiet) if quiet else None,
        # 该马上说的说了吗
        "now_recall": sum(r["got"] == "now" for r in needed_now) / len(needed_now) if needed_now else None,
        # 打扰里有多少是不该打扰的（对应"一周内被标没必要的即时通知少于三分之一"）
        "wrong_interrupts": sum(r["expect"] != "now" for r in interrupts) / len(interrupts) if interrupts else 0.0,
        "confusion": dict(Counter(f"{r['expect']}->{r['got']}" for r in results)),
        "tokens": sum(r["tokens"] for r in results),
    }


async def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="发不发评测")
    parser.add_argument("--model", default=os.environ.get("SMRA_MODEL", "claude-opus-5-5"))
    parser.add_argument("--effort", default=os.environ.get("SMRA_EFFORT", "medium"))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--only", action="append", help="只跑这些用例（可重复）")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 存到哪里，默认 evals/results/")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    for c in cases:
        if c.get("expect") not in LABELS:
            raise SystemExit(f"用例 {c.get('id')} 的 expect 必须是 {LABELS} 之一")
    client = anthropic.AsyncAnthropic(base_url=args.base_url) if args.base_url else anthropic.AsyncAnthropic()
    gate = asyncio.Semaphore(args.concurrency)

    async def one(case):
        async with gate:
            try:
                return await run_case(client, case, args.model, args.effort)
            except anthropic.APIError as e:
                return {"id": case["id"], "expect": case["expect"], "got": "error", "sent": [],
                        "tokens": 0, "error": str(e)[:300]}

    results = await asyncio.gather(*(one(c) for c in cases for _ in range(args.repeat)))
    for r in results:
        mark = "✓" if r["got"] == r["expect"] else "✗"
        said = "；".join(f"[{s['urgency']}] {s['text'][:60]}" for s in r["sent"]) or r.get("error", "")
        print(f"{mark} {r['id']:<32} 期望 {r['expect']:<6} 实际 {r['got']:<6} {said}")
    summary = summarize(results)
    print(json.dumps(summary, ensure_ascii=False, indent=1))

    out = args.out or RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{args.model}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "effort": args.effort, "summary": summary,
                               "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"结果存在 {out}")
    return summary


if __name__ == "__main__":
    asyncio.run(main())
