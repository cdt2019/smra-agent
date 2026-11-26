"""给真实模型跑出来的结论打分。只做能机械判断的检查，不调用模型。

    uv run python evals/quality_score.py s3 <运行目录>...
    uv run python evals/quality_score.py s1 <运行目录>...

s3（调研）：价格所在小节有没有来源；引用的地址是真打开过、只在搜索结果里，还是哪里都没出现过。
s1（数据）：有没有写出产品 C 的下滑、有没有说明 6 月数据不完整、有没有写和数据不符的"连续下跌"。
"""
from __future__ import annotations

import csv
import glob
import io
import itertools
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URL = re.compile(r"https?://[^\s<>\"')\]）】，。；！？、]+")
PRICE = re.compile(r"[$＄]\s?\d")
STREAK = re.compile(r"(连续|持续|逐月|每个?月都?).{0,8}(下降|下滑|下跌|走低|减少)")
DECLINE = re.compile(r"下降|下滑|下跌|走低|减少|萎缩|回落|降到|降至|[-−–－]\s?\d+(\.\d+)?\s?%")
INCOMPLETE = re.compile(r"6\s?月\s?29|06-29|29\s?天|不完整|未满|不满|缺\s?1\s?天|少\s?1\s?天|少一天|截至.{0,8}6\s?月")
TEXT_SUFFIXES = (".md", ".txt", ".html", ".htm", ".csv")
SCENARIO_DIRS = {"s1": "test_s1_s2_", "s3": "test_s3_"}     # pytest 按测试名建的目录


def _clean(url: str) -> str:
    return url.rstrip(".,;:!?。，；：！？*_`|")


def find_db(run_dir: str, scenario: str | None = None) -> Path:
    """找这个场景自己的数据库。一个运行目录里可能有别的测试（s1 和 s10），对不上就报错，不猜。"""
    # pytest 还会留一个 xxxcurrent 软链接指向同一个目录，解析后去重
    found = sorted({Path(p).resolve() for p in glob.glob(glob.escape(run_dir) + "/**/host.db", recursive=True)})
    if scenario:
        found = [p for p in found if p.parent.name.startswith(SCENARIO_DIRS[scenario])]
    if len(found) != 1:
        names = [p.parent.name for p in found]
        raise SystemExit(f"{run_dir}：场景 {scenario or '未指定'} 应该正好有一个 host.db，找到 {len(found)} 个 {names}")
    return found[0]


def load(run_dir: str, scenario: str | None = None) -> tuple[sqlite3.Connection, str, str]:
    """返回数据库、发给用户的文档全文、发给用户的回复全文。文档只取同一个测试目录里的。"""
    path = find_db(run_dir, scenario)
    db = sqlite3.connect(path)
    docs = [p.read_text("utf-8", "replace") for p in sorted((path.parent / "outgoing").glob("*"))
            if p.name.lower().endswith(TEXT_SUFFIXES)]
    replies = [r[0] for r in db.execute("SELECT body FROM outbox WHERE kind = 'reply' ORDER BY rowid")]
    return db, "\n".join(docs), "\n".join(replies)


def sections(doc: str) -> list[str]:
    return re.split(r"(?m)^(?=#{1,6} )", doc)


def score_s3(run_dir: str) -> dict:
    db, doc, _ = load(run_dir, "s3")
    priced = [s for s in sections(doc) if PRICE.search(s)]
    cited = [s for s in priced if URL.search(s)]
    urls = {_clean(u) for u in URL.findall(doc)}
    opened = {r[0] for r in db.execute("SELECT url FROM egress_log WHERE verdict = 'allow'")}
    seen = {r[0] for r in db.execute("SELECT url FROM seen_url")}
    return {"价格小节": len(priced), "有来源": len(cited), "引用地址": len(urls),
            "真打开过": len(urls & opened), "仅搜索结果": len((urls & seen) - opened),
            "来历不明": len(urls - seen - opened),
            "标注未核实": len(re.findall(r"未核实|未确认|待核实|未能打开|搜索摘要", doc))}


def s1_truth() -> tuple[float, bool]:
    from tests.test_live import sales_csv
    revenue: dict[str, float] = defaultdict(float)
    for row in csv.DictReader(io.StringIO(sales_csv().decode())):
        if row["product"] == "C":
            revenue[row["date"][:7]] += int(row["units"]) * float(row["price"])
    months = [revenue[m] for m in sorted(revenue)]
    drop = (months[0] - months[-1]) / months[0] * 100
    return drop, all(b < a for a, b in itertools.pairwise(months))


def score_s1(run_dir: str) -> dict:
    _, doc, replies = load(run_dir, "s1")
    doc = re.sub(r"<(script|style)\b.*?</\1>", " ", doc, flags=re.S | re.I)
    doc = re.sub(r"<[^>]+>", " ", doc)
    _, falls_every_month = s1_truth()

    def about_c(text: str) -> list[str]:
        return [line for line in text.splitlines() if re.search(r"(产品\s?C|\bC\b)", line)]

    def trend(lines: list[str]) -> bool:
        return any(DECLINE.search(line) and re.search(r"\d", line) for line in lines)

    # C 的占比和日均确实逐月下降；月度总额不是（4 月低于 5 月），只有说总额连续下跌才算错
    wrong_streak = [] if falls_every_month else \
        [line.strip()[:80] for line in about_c(doc + "\n" + replies)
         if STREAK.search(line) and not re.search(r"占比|份额|日均|比重", line)]
    return {"文档写出C下滑": trend(about_c(doc)), "回复写出C下滑": trend(about_c(replies)),
            "说明6月不完整": bool(INCOMPLETE.search(doc)), "回复说明6月不完整": bool(INCOMPLETE.search(replies)),
            "错误的连续下跌": len(wrong_streak), "例子": wrong_streak[:2]}


if __name__ == "__main__":
    kind, dirs = sys.argv[1], sys.argv[2:]
    for d in dirs:
        print(d, (score_s3 if kind == "s3" else score_s1)(d))
