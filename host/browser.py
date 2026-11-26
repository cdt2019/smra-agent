"""浏览器代理人。设计文档第 16 节。

浏览器是另一台机器：它有自己的配置目录（登录态在这里），电脑读不到。电脑通过宿主接口操控它，
只有几个动作：打开地址、取无障碍树、点击节点、在节点里输入、按键、后退、截图。没有"在页面里执行脚本"。

它的流量和电脑一样经过出网代理，只是用浏览器自己的通行证。代理据此认出这是浏览器的请求，
交给裁决者按第 16 节的规则判定。判定看的是点击之后浏览器实际发出的请求，不是"这次点击危不危险"。

要登录时由你接管：宿主给你一个接管页面，你在上面看着截图点击、输入，密码不经过电脑也不经过模型。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .db import new_id, now

if TYPE_CHECKING:
    from .app import App
    from .runs import RunCtx

log = logging.getLogger("browser")

VIEWPORT = {"width": 1280, "height": 800}
MAX_SNAPSHOT_CHARS = 20000
ACTION_TIMEOUT_MS = 10000
TAKEOVER_TTL_S = 30 * 60
_TAKING_OVER = ("用户正在接管浏览器（登录中），接管链接已经再发给用户一遍。"
                "这一轮先结束，用户操作完会有一条 [浏览器] 消息叫醒你。")
PASSWORD_MASK = "******"
LIVE_QUALITY = 50               # 浏览器推来的帧只用来发现变化，不发给手机
LIVE_TIERS = ((2, 78), (1.5, 62), (1, 50))      # 发给手机的图：倍数和 JPEG 质量，网越慢用越靠后的
LIVE_FAST_BPS = 80_000          # 下行高于这个用最清晰的
LIVE_SLOW_BPS = 35_000          # 下行低于这个用最小的图
LIVE_SHARP_BPS = 12_000         # 下行低于这个，最清晰的图要传四秒以上，停手后只补中等清晰度
LIVE_SETTLE_S = 0.6            # 小图发出后画面停这么久，补一张清晰的
LIVE_SETTLE_MAX_S = 3.0         # 慢网下补清晰图最多多等这么久
LIVE_SIGNAL_GAP_S = 0.1        # 浏览器变化信号的最小间隔
LIVE_ACTIVE_S = 1.5             # 你操作后这段时间，再小的变化也立刻推
LIVE_QUIET_S = 2.0              # 没人操作时的细小变化（光标闪烁之类），最多这么久更新一次
LIVE_SAME_SIZE = 0.015          # 两帧 JPEG 大小差这么点，算细小变化

# 无障碍树里这些角色的节点可以操作，给它们编号
INTERACTIVE = {"link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
               "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
               "spinbutton", "treeitem"}
_LINE = re.compile(r'^(\s*)- ([a-z]+)(?: "((?:[^"\\]|\\.)*)")?(.*)$')

ACTIONS = ("open", "snapshot", "click", "type", "press", "back", "screenshot", "login")
_CHROMIUM_ARGS = ["--no-first-run", "--disable-sync", "--disable-background-networking",
                  "--disable-component-update", "--disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable",
                  "--proxy-bypass-list=<-loopback>"]

# 只判断焦点在哪种输入框上，决定接管页面要不要遮住你输入的内容。不读值
_FOCUS_JS = """() => {
  const e = document.activeElement;
  if (!e || e === document.body) return 'none';
  if (e.tagName === 'IFRAME') return 'unknown';
  if (e.tagName === 'INPUT') {
    if (e.type === 'password') return 'password';
    if (['button','checkbox','radio','submit','reset','file','image','range','color'].includes(e.type)) return 'none';
    // 登录表单的账号框：接管页面在这里允许手机的密码管理器填。认不准就当普通输入框，不打扰你
    const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
    if (e.type === 'email' || /\\b(username|email)\\b/.test(ac)) return 'username';
    if (['text','tel',''].includes(e.type) && e.form && e.form.querySelector('input[type=password]')) return 'username';
    return 'text';
  }
  return e.tagName === 'TEXTAREA' || e.isContentEditable ? 'text' : 'none';
}"""

# 选中框的位置，手机上先画一个绿框，不用等整张图传过来。只读位置，不读内容
_FOCUS_BOX_JS = """() => {
  const e = document.activeElement;
  if (!e || e === document.body) return null;
  const r = e.getBoundingClientRect();
  return r.width > 0 && r.height > 0 ? {x: r.left, y: r.top, width: r.width, height: r.height} : null;
}"""

# 页面上还有看得见的密码框，多半还没登录上（只看有没有，不读内容）
_PASSWORD_VISIBLE_JS = """() => [...document.querySelectorAll('input[type=password]')].some(e => {
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
})"""


class BrowserError(RuntimeError):
    """动作做不了。消息原样交给模型。"""


@dataclass
class Ref:
    role: str
    name: str | None
    nth: int            # 有名字：同角色同名字里的第几个；没名字：同角色里的第几个


@dataclass
class Takeover:
    id: str
    key: str            # 接管页面地址里的随机串，只发给你
    session_id: str
    url: str
    reason: str
    started: float = field(default_factory=time.monotonic)


@dataclass
class Blocked:
    method: str
    url: str
    approval_id: str | None


@dataclass
class Denied:
    """出网代理按规则拦下的一个浏览器请求（不问你的那种）。下一次 Agent 用浏览器时告诉它。"""
    run_id: str
    method: str
    url: str
    reason: str
    same_site: bool     # 发往页面所在的网站：多半是页面功能要用的，拦了页面可能不正常
    hint: bool          # 你发来这个网站的网址后就不会再拦（没点名、也没读到疑似注入）


_MAX_DENIED = 100
_SHOW_DENIED = 5


def denial_notes(denied: list[Denied]) -> list[str]:
    """把拦下的请求讲给 Agent：拦了什么、为什么、页面会怎样、别去绕。"""
    same: dict[tuple[str, str], Denied] = {}
    cross = 0
    for d in denied:
        if d.same_site:
            same.setdefault((d.method, d.url), d)
        else:
            cross += 1
    notes = []
    if same:
        lines = [f"这个网站的页面自己发出的 {len(same)} 个请求被安全规则拦下了（不是你的操作出错）："]
        for d in list(same.values())[:_SHOW_DENIED]:
            where = d.url.split("://", 1)[-1]
            lines.append(f"- {d.method} {where[:150]}（{d.reason}）")
        if len(same) > _SHOW_DENIED:
            lines.append(f"- ……还有 {len(same) - _SHOW_DENIED} 个")
        lines.append("页面上依赖这些请求的部分（价格、支付方式、按钮的反应）可能出不来或不正常。"
                     "不要用 fetch、curl 或自己拼接口去绕：不会更快，而且每一步都要用户批准。")
        if any(d.hint for d in same.values()):
            lines.append("如果因此办不成，就停下来告诉用户卡在哪一步，并请用户把这个网站的网址直接发给你："
                         "用户亲手发过网址的网站，同站的请求不再拦。")
        else:
            lines.append("如果因此办不成，就停下来告诉用户卡在哪一步。")
        notes.append("\n".join(lines))
    if cross:
        notes.append(f"另外拦下了 {cross} 个发往别的网站的请求（多半是统计、广告），一般不影响使用。")
    return notes


def number_tree(tree: str) -> tuple[str, dict[str, Ref]]:
    """给无障碍树里可操作的节点编号：行尾加 [ref=eN]，返回编号到定位方式的对照。"""
    refs: dict[str, Ref] = {}
    per_role: dict[str, int] = {}
    per_name: dict[tuple[str, str], int] = {}
    out = []
    for line in tree.splitlines():
        m = _LINE.match(line)
        if not m or m.group(2) not in INTERACTIVE:
            out.append(line)
            continue
        indent, role, raw_name, rest = m.groups()
        name = None
        if raw_name is not None:
            try:
                name = json.loads(f'"{raw_name}"')
            except ValueError:
                name = raw_name
        if name:
            nth = per_name.get((role, name), 0)
            per_name[(role, name)] = nth + 1
        else:
            nth = per_role.get(role, 0)
        per_role[role] = per_role.get(role, 0) + 1
        ref = f"e{len(refs) + 1}"
        refs[ref] = Ref(role, name, nth)
        label = f' "{raw_name}"' if raw_name is not None else ""
        # [ref=..] 放在名字后面、冒号前面，和 Playwright 自己的写法一致
        if rest.startswith(":"):
            out.append(f"{indent}- {role}{label} [ref={ref}]{rest}")
        else:
            out.append(f"{indent}- {role}{label}{rest} [ref={ref}]")
    return "\n".join(out), refs


def form_fields(body: bytes, content_type: str) -> list[tuple[str, str]]:
    """把表单请求体拆成字段给你看。密码类字段只显示被隐藏了。"""
    from urllib.parse import parse_qsl
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return []
    try:
        pairs = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
    except ValueError:
        return []
    return [(k, PASSWORD_MASK if re.search(r"pass|pwd|密码", k, re.I) else v) for k, v in pairs]


class LiveView:
    """画面一变就给接管页面发一张截图，图的大小跟着实际网速走。

    浏览器的 screencast 帧只有一倍像素，这里只拿它当"画面变了"的信号，并且马上确认，
    这样信号不会被慢网络拖住。路上只留一帧，发下一帧时截的总是最新画面；网慢时先发小图，停下来再补清晰的。
    """

    def __init__(self, browser: BrowserAgent, cdp) -> None:
        self.browser = browser
        self.cdp = cdp
        self.closed = False
        self._fresh = asyncio.Event()
        self._frame: str | None = None
        self._last = 0
        self._first = True          # 静止的页面不会产生帧，一开始要主动截一张
        self._held: str | None = None   # 被压住的细小变化，到时间再发
        self._rough = False         # 手机上现在是小图，停下来要补清晰的
        self._last_sent = 0.0
        self._last_took = 0.0       # 上一帧从发出到显示花了多久
        self._size = 0
        self._recent: deque[int] = deque(maxlen=6)
        self._active_until = 0.0
        self._touched = 0.0
        self.after_input = False    # 这一帧是在你最后一次操作做完之后才截的
        self._rate: float | None = None     # 估计的下行字节每秒
        self._rtt: float | None = None
        self._acks: set[asyncio.Task] = set()
        cdp.on("Page.screencastFrame", self._on_frame)

    async def start(self) -> None:
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": LIVE_QUALITY, "maxWidth": 1640, "maxHeight": 2800, "everyNthFrame": 1})

    def _ack_later(self, frame_id: int) -> None:
        task = asyncio.ensure_future(self._ack(frame_id))
        self._acks.add(task)
        task.add_done_callback(self._acks.discard)

    async def _ack(self, frame_id: int) -> None:
        try:
            await asyncio.sleep(LIVE_SIGNAL_GAP_S)      # 限制信号频率，动画页面也不会占满 CPU
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": frame_id})
        except Exception:       # 页面关了或通道断了，发送循环会收场
            pass

    def _quiet_gap(self) -> float:
        return max(LIVE_QUIET_S, self._last_took * 4)   # 网越慢，光标闪烁这类更新越少，别占住线路

    def _on_frame(self, params: dict) -> None:
        self._ack_later(params["sessionId"])
        digest = hash(params["data"])
        if self.closed or digest == self._last:
            return
        size = len(params["data"])
        seen = digest in self._recent               # 光标亮和灭就是来回这几张
        self._recent.append(digest)
        now = time.monotonic()
        if now > self._active_until and self._size and now - self._last_sent < self._quiet_gap() and (
                seen or abs(size - self._size) <= self._size * LIVE_SAME_SIZE):
            # 没人操作时的细小变化：先压住最新的一张，到时间再发，省流量
            self._last, self._held = digest, params["data"]
            return
        self._last, self._frame, self._size = digest, params["data"], size
        self._held = None
        self._fresh.set()

    def touch(self) -> None:
        """你刚操作过，接下来的变化都是你要看的。"""
        self._touched = time.monotonic()
        self._active_until = self._touched + LIVE_ACTIVE_S

    def note_rtt(self, seconds: float) -> None:
        self._rtt = seconds if self._rtt is None else min(self._rtt, seconds)

    def delivered(self, size: int, seconds: float) -> None:
        """一帧从发出到手机显示完花的时间，用来估网速。"""
        self._last_took = seconds
        rate = size / max(0.03, seconds - (self._rtt or 0))
        self._rate = rate if self._rate is None else self._rate * 0.6 + rate * 0.4

    def _tier(self) -> int:
        if self._rate is None or self._rate >= LIVE_FAST_BPS:
            return 0
        return 1 if self._rate >= LIVE_SLOW_BPS else 2

    def _sharp_tier(self) -> int:
        return 0 if self._rate is None or self._rate >= LIVE_SHARP_BPS else 1

    def _settle_wait(self) -> float:
        """清晰图在慢网上要传几秒，多等一下，确认你停手了再发，免得挡住下一次操作的反馈。"""
        return max(LIVE_SETTLE_S, min(LIVE_SETTLE_MAX_S, self._last_took * 1.5))

    async def next(self) -> bytes | None:
        """下一张要发的图。这段时间画面没变就返回 None。"""
        try:
            await asyncio.wait_for(self._fresh.wait(), 0.25 if self._first or self._held or self._rough else 1.0)
        except TimeoutError:
            if self.closed:
                return None
            if self._first:
                self._first = False
                return await self._capture(self._sharp_tier())
            now = time.monotonic()
            if self._held and now - self._last_sent >= self._quiet_gap():
                data, self._held = self._held, None
                return await self._capture(self._sharp_tier(), data)
            if self._rough and now - self._last_sent >= self._settle_wait():
                return await self._capture(self._sharp_tier())
            return None
        self._fresh.clear()
        frame, self._frame = self._frame, None
        if frame is None:
            return None
        self._first = False
        return await self._capture(self._tier(), frame)

    async def _capture(self, tier: int, fallback: str | None = None) -> bytes | None:
        started = self._last_sent = time.monotonic()
        self.after_input = False
        self._rough = tier > self._sharp_tier()
        scale, quality = LIVE_TIERS[tier]
        try:
            shot = await self.browser.takeover_shot(scale, quality)
        except Exception as e:      # 页面正在跳转之类，先用手上这张
            log.info("截图失败：%s", type(e).__name__)
            self._rough = True
            return base64.b64decode(fallback) if fallback else None
        if fallback is None and self._frame is not None:    # 截图期间画面又变了，这张已经旧了
            return None
        self.after_input = started >= self._touched
        return shot

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._fresh.set()
        for task in list(self._acks):
            task.cancel()
        try:
            await self.cdp.send("Page.stopScreencast")
            await self.cdp.detach()
        except Exception:       # 页面已经没了
            pass


class BrowserAgent:
    """宿主侧的浏览器代理人。一个浏览器、一个标签页，同一时刻只有一次运行在用。"""

    def __init__(self, app: App):
        self.app = app
        self.cfg = app.cfg
        self.proxy_token = secrets.token_urlsafe(32)     # 只在宿主和浏览器进程之间，电脑里没有
        self.profile_dir = self.cfg.data_dir / "browser" / "profile"
        # 浏览器放在容器里时，登录状态（Cookie 和本地存储）存在宿主的这个文件里，容器里不留
        self.state_path = self.cfg.data_dir / "browser" / "state.json"
        self.holder_id: str | None = None              # 正在用浏览器的运行
        self.acting = False                              # 正在执行 Agent 的动作（以及动作后的等待）
        self.only_navigating = False                     # 那个动作只是打开或后退
        self.takeover: Takeover | None = None
        self._pw: Any = None
        self._box: Any = None                                 # 浏览器容器（SMRA_BROWSER=docker）
        self._remote: Any = None                         # 连到容器里的浏览器
        self._saving: set[asyncio.Task] = set()
        self._ctx: Any = None
        self._page: Any = None
        self._refs: dict[str, Ref] = {}
        self._blocked: list[Blocked] = []
        self._denied: list[Denied] = []                  # 规则拦下的请求，包括 Agent 两次操作之间页面在后台发的
        self._lock = asyncio.Lock()
        self._phone_view = False                         # 接管时换成了手机尺寸，Agent 下次操作前换回来
        self._phone_cdp: Any = None
        self._live: LiveView | None = None               # 同一时刻只推给一个页面

    # ------------------------------------------------------------ 启动和关闭

    async def _ensure(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._ctx is None:
            from playwright.async_api import async_playwright
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self.cfg.browser == "docker":
                await self._start_in_box()
            else:
                await self._start_local()
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.on("response", self._on_response)
        return self._page

    def _context_options(self, proxy_server: str) -> dict:
        kwargs = dict(
            proxy={"server": proxy_server, "username": "browser", "password": self.proxy_token},
            # 浏览器只和出网代理说话，代理用自己的 CA 解开 HTTPS；上游证书由代理校验
            ignore_https_errors=True,
            viewport=VIEWPORT,
            locale="zh-CN",
        )
        if self.cfg.browser_record_dir:
            Path(self.cfg.browser_record_dir).mkdir(parents=True, exist_ok=True)
            kwargs.update(record_video_dir=self.cfg.browser_record_dir, record_video_size=dict(VIEWPORT))
            log.info("浏览器录像打开，视频在 %s（浏览器关掉时写完）", self.cfg.browser_record_dir)
        return kwargs

    async def _start_local(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        kwargs = dict(self._context_options(f"http://127.0.0.1:{self.app.egress_port}"),
                      headless=self.cfg.browser_headless, args=_CHROMIUM_ARGS)
        if self.cfg.browser_channel and self.cfg.browser_channel != "chromium":
            kwargs["channel"] = self.cfg.browser_channel
        self._ctx = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        log.info("浏览器已启动（%s，配置目录 %s）", self.cfg.browser_channel or "chromium", self.profile_dir)

    async def _start_in_box(self) -> None:
        from .browser_box import BrowserBox
        if self._box is None:
            self._box = BrowserBox(self.app.egress_port)
        endpoint, proxy_server = await self._box.start()
        launch = {"headless": True, "args": _CHROMIUM_ARGS}
        self._remote = await self._pw.chromium.connect(
            endpoint, timeout=60000, headers={"x-playwright-launch-options": json.dumps(launch)})
        self._remote.on("disconnected", self._on_disconnected)
        kwargs = self._context_options(proxy_server)
        kwargs["proxy"]["bypass"] = "<-loopback>"       # 连容器自己的 localhost 也走代理
        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        self._ctx = await self._remote.new_context(**kwargs)
        log.info("浏览器已连上容器（登录状态 %s）", self.state_path)

    def _on_disconnected(self, remote) -> None:
        """容器没了（被删、内存超限）：下次用时重新起一个。"""
        if remote is self._remote:
            log.warning("和浏览器容器的连接断了，下次用时重新起")
            self._remote = self._ctx = self._page = self._phone_cdp = None

    async def save_state(self) -> None:
        """浏览器在容器里时，把登录状态存回宿主。容器随时可以删掉重建，登录不丢。"""
        if self._remote is None or self._ctx is None:
            return
        try:
            state = await self._ctx.storage_state()
        except Exception as e:      # 连接正好断了：下次再存
            log.info("没存上浏览器登录状态：%s", type(e).__name__)
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    async def close(self) -> None:
        try:
            await self.save_state()
            if self._ctx is not None:
                videos = [p.video for p in self._ctx.pages if p.video] if self._remote is not None else []
                await self._ctx.close()
                for n, video in enumerate(videos):     # 容器里录的视频要拷回宿主
                    await video.save_as(Path(self.cfg.browser_record_dir) / f"{secrets.token_hex(6)}-{n}.webm")
            if self._remote is not None:
                await self._remote.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            log.exception("关闭浏览器失败")
        if self._box is not None:
            await self._box.stop()
        self._ctx = self._pw = self._page = self._remote = None

    def _on_response(self, response) -> None:
        if not self.acting or response.headers.get("x-smra-egress") != "blocked":
            return
        self._blocked.append(Blocked(response.request.method, response.url,
                                     response.headers.get("x-smra-approval")))

    # ------------------------------------------------------------ 出网代理用

    def note_denied(self, d: Denied) -> None:
        if len(self._denied) < _MAX_DENIED:
            self._denied.append(d)

    def _take_denied(self, run: RunCtx) -> list[Denied]:
        """取走这次运行的；别的运行留下的直接丢掉，不讲给这次的 Agent 听。"""
        denied, self._denied = self._denied, []
        return [d for d in denied if d.run_id == run.id]

    def current_run(self) -> RunCtx | None:
        """正在用浏览器的那次运行。运行结束后浏览器就没人用了，它自己发的请求一律拦下。"""
        return self.app.runs.by_id(self.holder_id)

    def taking_over(self) -> bool:
        tk = self.takeover
        if tk is None:
            return False
        if time.monotonic() - tk.started > TAKEOVER_TTL_S:
            self._finish_takeover(tk, expired=True)
            return False
        return True

    def page_url(self) -> str:
        return self._page.url if self._page is not None and not self._page.is_closed() else ""

    # ------------------------------------------------------------ Agent 的动作

    async def act(self, run: RunCtx, data: dict) -> dict:
        action = data.get("action")
        if action not in ACTIONS:
            raise BrowserError(f"action 只能是 {' / '.join(ACTIONS)}")
        if self.taking_over():
            tk = self.takeover
            assert tk is not None
            # 用户可能找不到之前那条链接了（比如开了新对话）：再发一遍，并把叫醒消息转到这次的会话
            tk.session_id = run.session_id
            self.app.outbox.enqueue("system", run.session_id, (
                f"浏览器还在等你登录 {urlsplit(tk.url).hostname or tk.url}。\n"
                f"打开接管页面：{self.takeover_link(tk)}\n登录好了点\"登录好了\"；不想登录就在页面上点取消。"),
                f"takeover:{tk.id}:{run.id}")
            run.host_texts.add(_TAKING_OVER)
            raise BrowserError(_TAKING_OVER)
        async with self._lock:
            try:
                return await self._act(run, action, data)
            finally:
                await self.save_state()

    async def _act(self, run: RunCtx, action: str, data: dict) -> dict:
        self.holder_id = run.id
        page = await self._ensure()
        await self._restore_viewport(page)
        if action == "login":
            return await self._request_takeover(run, data)
        if action in ("snapshot", "screenshot"):
            # 上次操作之后页面在后台发的请求被拦了，也要让 Agent 知道
            notes = "".join(n + "\n\n" for n in denial_notes(self._take_denied(run)))
        if action == "snapshot":
            return {"text": notes + await self._snapshot(page)}
        if action == "screenshot":
            shot = await page.screenshot(type="jpeg", quality=60)
            return {"text": f"{notes}{await page.title()} — {page.url}",
                    "image_b64": base64.b64encode(shot).decode("ascii")}
        return await self._do(run, page, action, data)

    def _locate(self, page, ref: str | None):
        target = self._refs.get(str(ref or ""))
        if target is None:
            raise BrowserError(f"没有编号为 {ref} 的节点。页面可能变了，先 snapshot 取一遍最新的。")
        if target.name:
            return page.get_by_role(target.role, name=target.name, exact=True).nth(target.nth)
        return page.get_by_role(target.role).nth(target.nth)

    async def _do(self, run: RunCtx, page, action: str, data: dict) -> dict:
        deferred_before = len(run.deferred)
        self._blocked = []
        self.acting = True
        self.only_navigating = action in ("open", "back")
        notes = []
        try:
            try:
                if action == "open":
                    url = str(data.get("url") or "")
                    if not url.startswith(("http://", "https://")):
                        raise BrowserError("url 要是完整的 http(s) 地址")
                    await page.goto(url, wait_until="commit", timeout=self._settle_ms())
                    notes.append(f"已打开 {url}")
                elif action == "click":
                    await self._locate(page, data.get("ref")).click(timeout=ACTION_TIMEOUT_MS)
                    notes.append(f"已点击 {data.get('ref')}")
                elif action == "type":
                    loc = self._locate(page, data.get("ref"))
                    await loc.fill(str(data.get("text") or ""), timeout=ACTION_TIMEOUT_MS)
                    notes.append(f"已在 {data.get('ref')} 输入")
                    if data.get("submit"):
                        await loc.press("Enter", timeout=ACTION_TIMEOUT_MS)
                        notes.append("并按了回车")
                elif action == "press":
                    key = str(data.get("key") or "")
                    if not re.fullmatch(r"[A-Za-z0-9+]{1,30}", key):
                        raise BrowserError("key 是一个按键名，比如 Enter、Tab、Escape、ArrowDown")
                    await page.keyboard.press(key)
                    notes.append(f"已按 {key}")
                elif action == "back":
                    await page.go_back(wait_until="commit", timeout=self._settle_ms())
                    notes.append("已后退")
            except BrowserError:
                raise
            except Exception as e:      # Playwright 的超时、找不到节点等，原样告诉模型
                notes.append(f"动作没有完成：{type(e).__name__}: {str(e).splitlines()[0][:300]}")
            # 等动作引起的请求落定。要你批准的请求会在代理上挂一会儿，这里一并等着
            try:
                await page.wait_for_load_state("networkidle", timeout=self._settle_ms())
            except Exception:
                pass
        finally:
            self.acting = self.only_navigating = False
        blocked, self._blocked = self._blocked, []
        denied = self._take_denied(run)
        if any(b.url == page.url for b in blocked):
            # 被拦下的是一次页面跳转，浏览器停在了代理的拦截页上：退回操作前的页面，之后好重做
            try:
                await page.go_back(wait_until="load", timeout=ACTION_TIMEOUT_MS)
                notes.append("被拦下的是一次页面跳转，浏览器已退回到操作前的页面。")
            except Exception:
                pass
        new_approvals = run.deferred[deferred_before:]
        explained = {(d.method, d.url) for d in denied}
        for b in blocked:
            if b.approval_id:
                notes.append(f"浏览器发出的 {b.method} {b.url} 需要用户批准（审批号 {b.approval_id}），已经发给用户。")
            elif (b.method, b.url) not in explained:
                notes.append(f"浏览器发出的 {b.method} {b.url} 被出网代理拦下了。")
        notes.extend(denial_notes(denied))
        if new_approvals:
            notes.append("这一轮先结束，不要重试也不要换个办法绕过去；用户处理后你会收到一条 [审批] 消息，"
                         "那时页面可能已经变了，先 snapshot 再重做刚才的操作。")
        text = "\n".join(notes) + "\n\n" + await self._snapshot(page)
        return {"text": text}

    def _settle_ms(self) -> int:
        return int((self.cfg.browser_hold_s + 5) * 1000)

    async def _snapshot(self, page) -> str:
        try:
            tree = await page.locator("body").aria_snapshot(timeout=ACTION_TIMEOUT_MS)
        except Exception as e:
            return f"页面：{page.url}\n（取不到无障碍树：{type(e).__name__}）"
        # 密码框的值会出现在无障碍树里，交给 Agent 之前抹掉
        try:
            secrets_ = await page.locator("input[type=password]").evaluate_all("els => els.map(e => e.value)")
        except Exception:
            secrets_ = []
        for value in secrets_:
            if value:
                tree = tree.replace(value, PASSWORD_MASK)
        numbered, self._refs = number_tree(tree)
        if len(numbered) > MAX_SNAPSHOT_CHARS:
            numbered = numbered[:MAX_SNAPSHOT_CHARS] + "\n……（页面太长，后面截掉了）"
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        return f"页面：{title} — {page.url}\n{numbered}"

    # ------------------------------------------------------------ 你接管

    def takeover_link(self, tk: Takeover) -> str:
        base = self.cfg.takeover_url or f"http://{self.cfg.takeover_bind}:{self.app.takeover_port}"
        return f"{base.rstrip('/')}/takeover/{tk.key}"

    async def _request_takeover(self, run: RunCtx, data: dict) -> dict:
        url = str(data.get("url") or "")
        reason = str(data.get("reason") or "").strip()[:300]
        if not url.startswith(("http://", "https://")):
            raise BrowserError("login 要给出登录页的完整地址（url）")
        if not reason:
            raise BrowserError("login 要写明为什么需要登录（reason），会显示给用户")
        if self.app.takeover_port <= 0 and not self.cfg.takeover_url:
            raise BrowserError("宿主没有开接管页面，用户没法登录")
        tk = Takeover(new_id("tk"), secrets.token_urlsafe(24), run.session_id, url, reason)
        self.takeover = tk
        try:
            # 接管期间浏览器的请求算你的操作；先替你打开登录页
            await self._page.goto(url, wait_until="commit", timeout=self._settle_ms())
        except Exception as e:
            log.info("接管前打开 %s 失败：%s", url, e)
        site = urlsplit(url).hostname or url
        self.app.outbox.enqueue("system", run.session_id, (
            f"Agent 需要你在浏览器里登录 {site}。\n"
            f"Agent 说的原因（不可信，仅供参考）：{reason}\n"
            f"打开接管页面：{self.takeover_link(tk)}\n"
            "在页面上直接点击、输入，密码只经过宿主和浏览器，Agent 看不到。登录好了点\"完成\"。"
            "接管期间浏览器发出的请求都算你的操作。30 分钟内没完成会自动结束。"),
            f"takeover:{tk.id}")
        text = (f"已请用户接管浏览器登录 {site}（{tk.id}）。这一轮到此结束，不要再操作浏览器；"
                "用户登录完会有一条 [浏览器] 消息叫醒你，那时先 snapshot 看看页面。")
        run.host_texts.add(text)
        return {"text": text}

    def drop_takeover(self) -> bool:
        """开新对话时结束没完成的接管，不叫醒旧对话。返回之前有没有接管。"""
        if self.takeover is None:
            return False
        self.takeover = None
        return True

    def takeover_by_key(self, key: str) -> Takeover | None:
        tk = self.takeover
        if tk is None or not secrets.compare_digest(tk.key, key):
            return None
        return tk if self.taking_over() else None

    async def takeover_shot(self, scale: float = 2, quality: int = 78) -> bytes:
        page = await self._ensure()
        if self._phone_view and self._phone_cdp is not None:
            size = page.viewport_size or VIEWPORT
            try:        # 手机屏幕像素密度高，按两倍渲染才不糊
                shot = await self._phone_cdp.send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": quality,
                    "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": scale}})
                return base64.b64decode(shot["data"])
            except Exception as e:
                log.info("两倍截图失败：%s", type(e).__name__)
        return await page.screenshot(type="jpeg", quality=min(quality, 70))

    async def open_live(self) -> LiveView:
        """开始推画面。又打开一个页面时，旧的那个停止推送。"""
        page = await self._ensure()
        if self._live is not None:
            await self._live.close()
        view = LiveView(self, await self._ctx.new_cdp_session(page))
        self._live = view
        try:
            await view.start()
        except Exception:
            await self.close_live(view)
            raise
        return view

    async def close_live(self, view: LiveView) -> None:
        await view.close()
        if self._live is view:
            self._live = None

    async def takeover_state(self, tk: Takeover) -> dict:
        """接管页面顶部要显示的东西。只给主机名（完整地址里可能有登录令牌），不读任何输入框的值。"""
        page = await self._ensure()
        size = page.viewport_size or VIEWPORT
        try:
            focus = await page.evaluate(_FOCUS_JS)
            box = await page.evaluate(_FOCUS_BOX_JS) if focus != "none" else None
        except Exception:       # 页面正在跳转
            focus, box = "none", None
        parts = urlsplit(page.url)
        host, expected = parts.hostname or "", urlsplit(tk.url).hostname or ""
        return {
            "host": host,
            "secure": parts.scheme == "https",
            "offsite": bool(host) and host != expected and not host.endswith(f".{expected}"),
            "focus": focus if focus in ("none", "text", "username", "password", "unknown") else "unknown",
            "focus_box": box,
            "width": size["width"], "height": size["height"],
            "remaining_s": max(0, int(TAKEOVER_TTL_S - (time.monotonic() - tk.started))),
        }

    async def still_on_login(self) -> bool:
        """你点"完成"时页面上是否还有密码框。只是提醒你，判断不了的时候不拦。"""
        page = self._page
        if page is None or page.is_closed():
            return False
        try:
            return bool(await page.evaluate(_PASSWORD_VISIBLE_JS))
        except Exception:       # 页面正在跳转
            return False

    async def _phone_viewport(self, page, width: float, height: float) -> None:
        """远端页面换成手机的宽高，响应式网站会显示手机版。"""
        w, h = max(320, min(int(width), 820)), max(480, min(int(height), 1400))
        await page.set_viewport_size({"width": w, "height": h})
        self._phone_view = True
        if self._phone_cdp is None:
            try:
                self._phone_cdp = await self._ctx.new_cdp_session(page)
            except Exception as e:  # 只影响清晰度
                log.info("打开截图通道失败：%s", type(e).__name__)

    async def _restore_viewport(self, page) -> None:
        if not self._phone_view:
            return
        self._phone_view = False
        cdp, self._phone_cdp = self._phone_cdp, None
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        await page.set_viewport_size(VIEWPORT)

    async def takeover_input(self, data: dict) -> None:
        """你在接管页面上的操作。只有接管页面能调到这里，电脑里没有对应的接口。"""
        page = await self._ensure()
        kind = data.get("type")
        if self._live is not None:
            self._live.touch()
        if kind == "click":
            await page.mouse.click(float(data["x"]), float(data["y"]))
        elif kind == "text":
            await page.keyboard.type(str(data.get("text") or ""))
        elif kind == "key":
            for _ in range(max(1, min(int(data.get("count", 1)), 20))):     # 长按删除时一次连按几下
                await page.keyboard.press(str(data.get("key") or ""))
        elif kind == "scroll":
            if "x" in data and "y" in data:     # 滚轮作用在鼠标所在的位置，页面里可能有单独滚动的区域
                await page.mouse.move(float(data["x"]), float(data["y"]))
            await page.mouse.wheel(float(data.get("dx") or 0), float(data.get("dy") or 0))
        elif kind == "viewport":
            await self._phone_viewport(page, float(data["width"]), float(data["height"]))
        elif kind == "goto":
            await page.goto(str(data.get("url") or ""), wait_until="commit", timeout=30000)
        elif kind == "back":
            await self._takeover_back(page)
        else:
            raise BrowserError("不认识的操作")
        if self._live is not None:      # 跳转可能花了好几秒，从做完算起
            self._live.touch()

    async def _takeover_back(self, page) -> None:
        """后退不能退到浏览器打开前的空白页；已经在空白页上，就回到要登录的页面。"""
        web = ("http://", "https://")
        if not page.url.startswith(web):
            if self.takeover is None:
                raise BrowserError("已经是第一页")
            await page.goto(self.takeover.url, wait_until="commit", timeout=30000)
            return
        cdp = await self._ctx.new_cdp_session(page)
        try:
            history = await cdp.send("Page.getNavigationHistory")
        finally:
            await cdp.detach()
        at = history["currentIndex"]
        if at <= 0 or not history["entries"][at - 1]["url"].startswith(web):
            raise BrowserError("已经是第一页")
        await page.go_back(wait_until="commit", timeout=30000)

    def finish_takeover(self, tk: Takeover, cancelled: bool = False) -> None:
        self._finish_takeover(tk, cancelled=cancelled)

    def _finish_takeover(self, tk: Takeover, cancelled: bool = False, expired: bool = False) -> None:
        if self.takeover is not tk:
            return
        self.takeover = None
        if self._remote is not None:        # 刚登录好，先把登录状态存回宿主
            task = asyncio.get_running_loop().create_task(self.save_state())
            self._saving.add(task)
            task.add_done_callback(self._saving.discard)
        site = urlsplit(tk.url).hostname or tk.url
        if expired:
            text, outcome = f"[浏览器] 用户 30 分钟内没有完成 {site} 的登录，接管已结束。需要的话问问用户。", "expired"
        elif cancelled:
            text, outcome = f"[浏览器] 用户取消了 {site} 的登录。不要再请求登录，换个办法或者问用户。", "cancelled"
        else:
            text, outcome = (f"[浏览器] 用户已经在浏览器里操作完了（登录 {site}）。接着做之前的事；"
                             "先 snapshot 看看页面现在的状态。"), "done"
        self.app.inbox.add("browser", f"{tk.id}:{outcome}", {"text": text, "at": now()}, session_id=tk.session_id)
