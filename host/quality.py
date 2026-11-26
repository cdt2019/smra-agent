"""发文件前的检查：引用的地址是否真的出现过，调研里的价格有没有来源。

只做能机械判断的事，不调用模型。第一次有问题退回让 Agent 改；还有问题就照发，并把问题告诉用户。
"""
from __future__ import annotations

import html
import itertools
import re
import sqlite3
from dataclasses import dataclass, field

from .egress.urls import normalize

SUFFIXES = (".md", ".markdown", ".txt", ".html", ".htm")
MAX_CHECK_BYTES = 2_000_000
_SHOW = 8

_URL = re.compile(r"""https?://[^\s"'<>\\()\[\]{}（）【】「」《》，。；、|`*]+""", re.IGNORECASE)
_PRICE = re.compile(r"[$＄€£]\s?\d|\d\s?(?:美元|欧元|英镑)")
_REF = re.compile(r"\[\^?(\d{1,3})\]")

# 宿主写给 Agent 的固定文字。注入检测会先删掉它们，其余内容（比如退回信息里列出的链接）照常检查
REPLY_REMINDER = "最后给用户的回复要直接写出文件里最重要的发现（尤其是下滑、风险和反常的地方）和未核实的部分，不要只说文件发了。"
_NOT_SENT = "文件没有发出。发送前检查发现："
_FIX_LINKS = "这些链接没有出现在搜索结果或打开过的网页里，可能是凭记忆写的。换成真实来源，或者删掉："
_FIX_PRICES = "这些小节有价格但没有来源链接。补上原样的地址；查不到就标\"未查到\"并去掉数字："
_FIX_TRENDS = "这些趋势说法和列出的数字对不上，或者没写口径。用代码核对后改准确，并写明是总额、占比还是日均："
_RETRY = "改好后再发一次。再次发送时如果还有问题，文件会带着提醒发给用户。"
HOST_NOTES = (REPLY_REMINDER, _NOT_SENT, _FIX_LINKS, _FIX_PRICES, _FIX_TRENDS, _RETRY)

# 趋势说法：“连续 6 个月下滑”“逐月上升”“一路走低”
_UP = "上升|上涨|增长|走高|攀升|提升|走强"
_DOWN = "下滑|下降|下跌|走低|减少|回落|萎缩|走弱|掉到|跌到|跌至|降到|降至|缩水|下行"
_STREAK = (r"连续(?:\s*[\d一二两三四五六七八九十]+\s*个?\s*(?:月|季度?|年|周|天|日))?"
           r"|逐月|逐季|逐年|逐周|逐日|月月|每个?月都|一路|一直")
_CLAIM = re.compile(rf"({_STREAK})[^，,。；;！？\n]{{0,8}}?({_UP}|{_DOWN})")
_ARROW = r"(?:→|->|⇒|➜|➡|⟶)"
_NUM = r"[-−+]?[¥￥$]?\d[\d,]*(?:\.\d+)?\s*(?:万|亿|千)?\s*(?:元|件|%|％)?"
_CHAIN = re.compile(rf"{_NUM}(?:\s*{_ARROW}\s*{_NUM}){{2,}}")
_BASIS = re.compile(r"日均|月均|周均|占比|份额|比重|同比|环比|月度|总额|总量|月总|月销售额|月收入|月营收|月销量|累计|[一-鿿]率")
_UNITS = {"万": 1e4, "亿": 1e8, "千": 1e3}
_CN_NUMBERS = dict(zip("一二两三四五六七八九十", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10)))


@dataclass
class Problems:
    unseen: list[str] = field(default_factory=list)     # 没在任何资料里出现过的地址
    unsourced: list[str] = field(default_factory=list)  # 有价格但没有来源的小节
    trends: list[str] = field(default_factory=list)     # 和数字对不上、或没写口径的趋势说法

    def __bool__(self) -> bool:
        return bool(self.unseen or self.unsourced or self.trends)

    def for_agent(self) -> str:
        lines = [_NOT_SENT]
        if self.unseen:
            lines.append(_FIX_LINKS + "、".join(self.unseen[:_SHOW]))
        if self.unsourced:
            lines.append(_FIX_PRICES + "、".join(self.unsourced[:_SHOW]))
        if self.trends:
            lines.append(_FIX_TRENDS + "；".join(self.trends[:_SHOW]))
        lines.append(_RETRY)
        return "\n".join(lines)

    def for_user(self) -> str:
        parts = []
        if self.unseen:
            parts.append(f"{len(self.unseen)} 个链接没有出现在 Agent 读过的资料里")
        if self.unsourced:
            parts.append(f"{len(self.unsourced)} 个价格小节没有来源")
        if self.trends:
            parts.append(f"{len(self.trends)} 处趋势说法和数字对不上或没写口径")
        return "⚠️ 发送前检查：" + "，".join(parts) + "，请核实后再用。"


def visible_text(name: str, text: str) -> str:
    """HTML 只看读者能看到的文字和链接；脚本、样式、命名空间里的地址不算引用。"""
    if not name.lower().endswith((".html", ".htm")):
        return text
    text = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>", r" \1 ", text, flags=re.I)
    text = re.sub(r"<h[1-6]\b[^>]*>", "\n# ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|table|h[1-6])\s*>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def links(text: str) -> list[str]:
    found = []
    for raw in _URL.findall(text):
        url = normalize(raw.rstrip(".,;:!?"))
        if url and url not in found:
            found.append(url)
    return found


def is_known(db: sqlite3.Connection, url: str) -> bool:
    """见过这个地址，或者见过同一页面带查询参数（跟踪参数）的版本，或者它经批准打开过。"""
    other = url[:-1] if url.endswith("/") else url + "/"
    if db.execute("SELECT 1 FROM seen_url WHERE url IN (?, ?) LIMIT 1", (url, other)).fetchone():
        return True
    if db.execute("SELECT 1 FROM egress_log WHERE url IN (?, ?) AND verdict = 'allow' LIMIT 1",
                  (url, other)).fetchone():
        return True
    if "?" in url:
        return False
    for page in (url, other):
        prefix = page + "?"
        if db.execute("SELECT 1 FROM seen_url WHERE substr(url, 1, ?) = ? LIMIT 1",
                      (len(prefix), prefix)).fetchone():
            return True
    return False


def _sourced(section: str, doc: str) -> bool:
    if _URL.search(section):
        return True
    for number in _REF.findall(section):
        if re.search(rf"(?m)^.*\[\^?{number}\].*https?://", doc):
            return True
    return False


def _amount(raw: str) -> float:
    m = re.search(r"([-−+]?)[¥￥$]?(\d[\d,]*(?:\.\d+)?)\s*(万|亿|千)?", raw)
    if m is None:       # 数字串是按 _NUM 切出来的，每段都有数字；防万一当成对不上
        return float("nan")
    value = float(m[2].replace(",", "")) * _UNITS.get(m[3], 1)
    return -value if m[1] in ("-", "−") else value


def _span(word: str) -> int | None:
    """“连续 3 个月”只核对最后 3 个数；没说几个月就核对整串。"""
    if not word.startswith("连续"):
        return None
    m = re.search(r"\d+|[一二两三四五六七八九十]", word)
    if not m:
        return None
    return int(m[0]) if m[0].isdigit() else _CN_NUMBERS[m[0]]


def _clause(sentence: str, start: int, end: int) -> str:
    left = max((sentence.rfind(c, 0, start) for c in "，,；;：:（("), default=-1)
    rights = [i for i in (sentence.find(c, end) for c in "，,；;）)") if i >= 0]
    return sentence[left + 1:min(rights) if rights else len(sentence)]


def trend_issues(text: str, need_basis: bool) -> list[str]:
    """只核对文字自身：同一句列了数字串，就看它是否真的一路涨或跌。
    need_basis：分析用户数据时，“连续/逐月”这类说法还要写明口径；同一句有数字串佐证的不用。"""
    text = re.sub(r"```.*?```", " ", text, flags=re.S).replace("*", "")
    issues: list[str] = []
    for sentence in re.split(r"[。！？!?\n]+", text):
        chains = list(_CHAIN.finditer(sentence))
        for claim in _CLAIM.finditer(sentence):
            if re.search(r"[不非没未无]", sentence[max(0, claim.start() - 3):claim.start()]):
                continue
            word, verb = claim[1], claim[2]
            problem = ""
            if chains:
                chain = min(chains, key=lambda c: abs(c.start() - claim.start()))
                values = [_amount(v) for v in re.split(_ARROW, chain[0])]
                span = _span(word)
                checked = values[-max(span, 2):] if span else values
                down = re.fullmatch(_DOWN, verb) is not None
                steady = all(b < a if down else b > a for a, b in itertools.pairwise(checked))
                if not steady:
                    problem = f"数字是 {chain[0].strip()}，不是{word}{verb}"
            elif need_basis and word not in ("一路", "一直") \
                    and not _BASIS.search(_clause(sentence, claim.start(), claim.end())):
                problem = "没写是总额、占比还是日均"
            if problem:
                issues.append(f"“{sentence.strip()[:60]}”（{problem}）")
                break
    return list(dict.fromkeys(issues))


def reply_warning(text: str, researched: bool) -> str:
    """回复不退回（退回要再花一轮模型），有问题就在后面附一句提醒用户。"""
    issues = trend_issues(text, not researched)
    if not issues:
        return ""
    lines = "\n".join(f"- {i}" for i in issues[:3])
    return f"⚠️ 发送前检查：{len(issues)} 处趋势说法和数字对不上或没写口径，请以数据为准。\n{lines}"


def check(db: sqlite3.Connection, name: str, blob: bytes, researched: bool) -> Problems:
    """researched：这次运行上过网。没上网的报告（比如分析用户的表格）里的金额不需要来源。"""
    if not name.lower().endswith(SUFFIXES) or len(blob) > MAX_CHECK_BYTES:
        return Problems()
    text = visible_text(name, blob.decode("utf-8", "replace"))
    result = Problems(unseen=[u for u in links(text) if not is_known(db, u)],
                      trends=trend_issues(text, not researched))
    if researched:
        for section in re.split(r"(?m)^(?=#{1,6} )", text):
            if _PRICE.search(section) and not _sourced(section, text):
                title = section.strip().splitlines()[0].lstrip("# ").strip()[:40]
                result.unsourced.append(title or "（开头部分）")
    return result
