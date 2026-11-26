"""接管页面：你在这里看着浏览器的画面，直接点击、输入，用来登录。手机和电脑都能用。

单独的端口，和电脑能连的宿主接口分开。地址里带一个只发给你的随机串。
密码从这里直接进浏览器，不经过电脑，也不经过模型。
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import web

from . import activity
from .browser import BrowserError

if TYPE_CHECKING:
    from .app import App
    from .browser import BrowserAgent, Takeover

log = logging.getLogger("takeover")

LIVE_MIN_GAP_S = 0.07       # 最多约 14 帧每秒，别占满 CPU
LIVE_ACK_TIMEOUT_S = 20     # 慢网上一帧要传好几秒，等够了再发下一帧，否则帧会在线路里排队
LIVE_PING_EVERY_S = 5       # 空闲时量一次来回延迟，用来估网速
LIVE_ENDED, LIVE_REPLACED, LIVE_FAILED = 4404, 4409, 4500   # 页面据此决定重连还是回到轮询

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f5f4ef" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#131412" media="(prefers-color-scheme: dark)">
<meta name="robots" content="noindex,nofollow">
<title>接管浏览器</title>
<style nonce="__NONCE__">
:root{--bg:#f5f4ef;--surface:#fffefb;--raised:#ecebe4;--line:#dfddd3;--ink:#1d1c18;--soft:#6d6a60;--accent:#1d6b52;--accent-ink:#fff;
 --accent-tint:#e1eee8;--warn:#8a5208;--warn-tint:#fbefd6;--danger:#a83a26;--shadow:0 1px 2px #29261a14,0 6px 20px #29261a12;--ease:cubic-bezier(.2,.8,.2,1)}
@media (prefers-color-scheme:dark){:root{--bg:#131412;--surface:#1d1e1b;--raised:#2a2b27;--line:#33342f;--ink:#f1f0ea;--soft:#a3a297;--accent:#4cc29a;--accent-ink:#06261b;
 --accent-tint:#16352a;--warn:#f1c063;--warn-tint:#3a2b10;--danger:#f0806b;--shadow:0 1px 2px #0006,0 8px 24px #0005}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
html,body{margin:0;height:100%;overflow:hidden;overscroll-behavior:none;background:var(--bg);color:var(--ink);
 font:15px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none;display:block}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;touch-action:manipulation;
 transition:transform .12s var(--ease),background-color .15s,opacity .15s}
button:active{transform:scale(.96)}
button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#app{position:fixed;left:0;right:0;top:var(--app-top,0);height:var(--app-h,100dvh);display:flex;flex-direction:column;max-width:760px;margin:auto}

header{display:flex;align-items:center;gap:2px;padding:calc(4px + env(safe-area-inset-top)) 6px 4px 14px}
#site{flex:1;min-width:0;display:flex;align-items:center;gap:10px}
#badge{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;flex:none;background:var(--accent-tint);color:var(--accent);transition:background-color .2s,color .2s}
#badge svg{width:18px;height:18px}
#site.insecure #badge{background:var(--warn-tint);color:var(--danger)}
#site-text{min-width:0}
#host{display:block;font-size:15.5px;font-weight:650;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#meta{display:flex;gap:6px;color:var(--soft);font-size:12px;line-height:1.35;white-space:nowrap}
#site.insecure #sec{color:var(--danger);font-weight:600}
#clock{font-variant-numeric:tabular-nums}
#clock.low{color:var(--warn);font-weight:600}
.icon{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;color:var(--soft)}
.icon:hover{background:var(--raised)}

#offsite{display:none;align-items:flex-start;gap:8px;margin:2px 8px 6px;padding:9px 12px;border-radius:12px;background:var(--warn-tint);color:var(--warn);font-size:13px;line-height:1.4}
#offsite.on{display:flex}
#offsite svg{width:17px;height:17px;margin-top:1px}

#frame{flex:1;min-height:0;padding:2px 8px 8px}
#stage{position:relative;width:100%;height:100%;overflow:hidden;border-radius:16px;background:var(--surface);box-shadow:0 0 0 1px var(--line),var(--shadow);touch-action:none;user-select:none;-webkit-user-select:none}
#shot{position:absolute;left:0;top:0;transform-origin:0 0;display:block;max-width:none;-webkit-user-drag:none;opacity:0;transition:opacity .2s}
#shot.ready{opacity:1}
#ring{position:absolute;box-sizing:border-box;border:2px solid var(--accent);border-radius:6px;pointer-events:none}
#ring[hidden]{display:none}
#loading{position:absolute;inset:0;padding:26px 20px;display:grid;align-content:start;gap:12px}
#loading i{display:block;height:13px;border-radius:7px;background:linear-gradient(100deg,var(--raised) 35%,var(--bg) 50%,var(--raised) 65%);background-size:220% 100%;animation:sheen 1.3s linear infinite}
#loading i:nth-child(1){width:42%;height:24px}#loading i:nth-child(2){width:88%}#loading i:nth-child(3){width:70%}
@keyframes sheen{to{background-position:-220% 0}}
.ripple{position:absolute;width:40px;height:40px;margin:-20px;border-radius:50%;background:color-mix(in srgb,var(--accent) 22%,transparent);box-shadow:inset 0 0 0 1.5px var(--accent);pointer-events:none;animation:ripple .42s var(--ease) forwards}
@keyframes ripple{from{transform:scale(.35);opacity:1}to{transform:scale(1.1);opacity:0}}
#fit{display:none;position:absolute;right:10px;top:10px;height:34px;padding:0 12px;border-radius:17px;font-size:13px;background:var(--surface);box-shadow:0 0 0 1px var(--line),var(--shadow)}
#fit.on{display:block}
#toast{position:absolute;left:50%;bottom:12px;display:flex;align-items:center;gap:6px;max-width:calc(100% - 24px);padding:7px 13px;border-radius:18px;background:var(--ink);color:var(--bg);font-size:13px;white-space:nowrap;opacity:0;transform:translate(-50%,6px);transition:opacity .16s,transform .2s var(--ease);pointer-events:none}
#toast.on{opacity:1;transform:translate(-50%,0)}

#dock{display:grid;gap:8px;padding:0 10px calc(8px + env(safe-area-inset-bottom))}
#hint{display:flex;align-items:center;gap:7px;padding:0 4px;min-height:18px;color:var(--soft);font-size:12.5px}
#hint::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--line);flex:none;transition:background-color .15s}
#hint[data-state=text]::before{background:var(--accent)}
#hint[data-state=password]{color:var(--accent);font-weight:600}
#hint[data-state=password]::before,#hint[data-state=unknown]::before{background:var(--accent)}
form{display:flex;gap:8px;margin:0}
#field{flex:1;min-width:0;position:relative;display:flex;align-items:center;height:48px;border-radius:15px;background:var(--surface);box-shadow:inset 0 0 0 1px var(--line);transition:box-shadow .15s}
#field:focus-within{box-shadow:inset 0 0 0 1.5px var(--accent)}
#text{flex:1;min-width:0;height:100%;padding:0 6px 0 15px;border:0;outline:0;background:none;color:var(--ink);font-size:16px}
#text::placeholder{color:var(--soft);opacity:.8}
#text::-ms-reveal,#text::-ms-clear{display:none}
#reveal{width:44px;height:44px;display:grid;place-items:center;color:var(--soft);border-radius:12px;flex:none}
#reveal .eye-off{display:none}#reveal[aria-pressed=true] .eye{display:none}#reveal[aria-pressed=true] .eye-off{display:block}
#send{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;flex:none;background:var(--ink);color:var(--bg)}
#keys{display:flex;gap:8px}
#seg{flex:1;min-width:0;display:grid;grid-template-columns:repeat(4,1fr);height:50px;padding:3px;border-radius:15px;background:var(--raised)}
#seg button{min-height:44px;border-radius:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;font-size:11px;color:var(--soft)}
#seg button svg{width:17px;height:17px}
#seg button:hover{background:var(--surface);color:var(--ink)}
#done{flex:none;height:50px;padding:0 20px;display:flex;align-items:center;gap:6px;border-radius:15px;background:var(--accent);color:var(--accent-ink);font-weight:650}
#done svg{width:18px;height:18px;stroke-width:2.2}
#app.compact #hint{display:none}
#app.compact #seg{height:44px}#app.compact #seg button{min-height:38px}#app.compact #seg span{display:none}
#app.compact #done{height:44px}

dialog{width:100%;max-width:480px;margin:auto auto 0;padding:8px 20px calc(18px + env(safe-area-inset-bottom));border:0;border-radius:22px 22px 0 0;background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}
dialog[open]{animation:rise .24s var(--ease)}
dialog::backdrop{background:#0d0d0b73}
@keyframes rise{from{transform:translateY(24px);opacity:.4}to{transform:none;opacity:1}}
.grab{width:36px;height:4px;margin:0 auto 16px;border-radius:2px;background:var(--line)}
dialog h2{margin:0 0 6px;font-size:18px;font-weight:650;letter-spacing:-.01em}
dialog p{margin:0;color:var(--soft);font-size:14px}
dl{margin:16px 0 0;display:grid;gap:12px}
dt{color:var(--soft);font-size:12px;margin-bottom:2px}
dd{margin:0;font-size:14px;overflow-wrap:anywhere}
.actions{display:grid;gap:8px;margin-top:20px}
.actions button{height:48px;border-radius:14px;font-weight:600;background:var(--raised)}
.actions .go{background:var(--accent);color:var(--accent-ink)}
.actions .bad{background:none;color:var(--danger)}

#end{display:none;position:fixed;inset:0;z-index:5;flex-direction:column;align-items:center;justify-content:center;padding:32px;text-align:center;background:var(--bg)}
#end.on{display:flex;animation:fade .25s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
#end-mark{width:64px;height:64px;margin-bottom:20px;border-radius:21px;display:grid;place-items:center;background:var(--accent-tint);color:var(--accent)}
#end-mark.off{background:var(--raised);color:var(--soft)}
#end-mark svg{width:30px;height:30px;stroke-width:2}
#end-title{font-size:21px;font-weight:650;letter-spacing:-.01em}
#end-sub{margin-top:6px;color:var(--soft);font-size:14px;max-width:280px}
@media (prefers-reduced-motion:reduce){*,::before,::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style></head><body>
<svg hidden><defs>
 <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></symbol>
 <symbol id="i-open" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8 11V8a4 4 0 0 1 7.5-2"/></symbol>
 <symbol id="i-check" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></symbol>
</defs></svg>
<div id="app">
<header>
 <div id="site">
  <span id="badge" aria-hidden="true"><svg><use id="badge-icon" href="#i-lock"/></svg></span>
  <div id="site-text"><span id="host">正在连接…</span><span id="meta"><span id="sec"></span><span id="clock"></span></span></div>
 </div>
 <button id="info" class="icon" aria-label="查看这次登录的说明"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 11.2v4.6M12 8.2v.1"/></svg></button>
 <button id="cancel" class="icon" aria-label="取消登录"><svg><use href="#i-x"/></svg></button>
</header>
<div id="offsite" role="alert"><svg viewBox="0 0 24 24"><path d="M12 4 3 19.5h18L12 4Z"/><path d="M12 10v4.5M12 17v.1"/></svg><span id="offsite-text"></span></div>
<main id="frame">
 <div id="stage" aria-label="远端浏览器画面，点按操作，滑动翻页，双指缩放">
  <div id="loading" aria-label="正在加载画面"><i></i><i></i><i></i></div>
  <img id="shot" alt="远端浏览器画面" draggable="false">
  <div id="ring" hidden></div>
  <button id="fit">还原大小</button>
  <div id="toast" role="status"></div>
 </div>
</main>
<div id="dock">
 <div id="hint" data-state="none"><span id="hint-text">点一下画面中的输入框</span></div>
 <form id="form" autocomplete="off">
  <div id="field">
   <input id="text" type="text" placeholder="输入内容" aria-label="要输入到远端页面的内容" autocomplete="off"
    autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="send" data-1p-ignore data-lpignore="true">
   <button id="reveal" type="button" aria-pressed="false" aria-label="显示内容" hidden>
    <svg class="eye" viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>
    <svg class="eye-off" viewBox="0 0 24 24"><path d="M4 4l16 16M9.8 5.8A9 9 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16 16 0 0 1-3 3.8M6.3 7.5A15.600 15.600 0 0 0 2.500 12S6 18.500 12 18.500a9 9 0 0 0 4-.9"/></svg>
   </button>
  </div>
  <button id="send" type="submit" aria-label="输入到页面"><svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
 </form>
 <div id="keys">
  <div id="seg" role="group" aria-label="页面操作">
   <button id="back" type="button"><svg viewBox="0 0 24 24"><path d="M19 12H5M11 6l-6 6 6 6"/></svg><span>后退</span></button>
   <button id="del" type="button" aria-label="删除页面输入框里的一个字符，长按连续删除"><svg viewBox="0 0 24 24"><path d="M9 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6-7 6-7ZM12 9.500l5 5M17 9.500l-5 5"/></svg><span>删除</span></button>
   <button id="tab" type="button"><svg viewBox="0 0 24 24"><path d="M4 12h13M12 7l5 5-5 5M20.500 6v12"/></svg><span>下一项</span></button>
   <button id="enter" type="button"><svg viewBox="0 0 24 24"><path d="M19 6v5a3 3 0 0 1-3 3H5M9.500 9.500 5 14l4.500 4.500"/></svg><span>回车</span></button>
  </div>
  <button id="done" type="button"><svg><use href="#i-check"/></svg>登录好了</button>
 </div>
</div>
</div>

<dialog id="why">
 <div class="grab"></div>
 <h2>这次登录</h2>
 <p>下面的说明由 Agent 填写，未经核实，请以画面中的网站为准。</p>
 <dl><div><dt>要登录的地址</dt><dd>__URL__</dd></div><div><dt>Agent 给出的原因</dt><dd>__REASON__</dd></div>
 <div><dt>你的密码</dt><dd>只发给这个浏览器，不经过 Agent 和模型。</dd></div></dl>
 <div class="actions"><button id="why-close" type="button">知道了</button></div>
</dialog>
<dialog id="ask">
 <div class="grab"></div>
 <h2>取消这次登录？</h2>
 <p>Agent 会收到“用户取消了”，这个链接随即失效。</p>
 <div class="actions"><button id="keep" type="button">继续登录</button><button id="really" class="bad" type="button">取消登录</button></div>
</dialog>
<dialog id="unsure">
 <div class="grab"></div>
 <h2>还没登录上</h2>
 <p>页面还停在登录页。填好邮箱和密码后，还要点画面里网站的“登录”按钮提交；页面跳走了再点“登录好了”。如果已经提交过，看看画面里有没有报错。</p>
 <div class="actions"><button id="submit-login" class="go" type="button">提交登录（在页面上按回车）</button><button id="stay" type="button">回去看看</button><button id="anyway" type="button">仍然交还给 Agent</button></div>
</dialog>
<div id="end" role="status"><div id="end-mark"><svg><use id="end-icon" href="#i-check"/></svg></div><div id="end-title"></div><div id="end-sub"></div></div>

<script nonce="__NONCE__">
const base = location.pathname, $ = id => document.getElementById(id);
const img = $('shot'), stage = $('stage'), text = $('text');
let vw = 0, vh = 0, view = {s: 1, x: 0, y: 0, w: 0, h: 0}, ended = false, shotBusy = false, shotUrl = '', revealed = false, lastFocus = '', deadline = 0, risky = true;
let live = null, liveOn = false, liveStopped = false, liveRetry = 0, frameNo = 0;

function toast(message){ const t = $('toast'); t.textContent = message; t.classList.add('on');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove('on'), 1600); }
function finish(title, sub, ok){
  if (ended) return; ended = true; text.value = ''; stopLive();
  for (const d of document.querySelectorAll('dialog')) if (d.open) d.close();
  $('end-title').textContent = title; $('end-sub').textContent = sub;
  $('end-icon').setAttribute('href', ok ? '#i-check' : '#i-x'); $('end-mark').classList.toggle('off', !ok);
  $('end').classList.add('on');
}
const gone = () => finish('这次接管已结束', '链接已失效，可以关掉这个页面了。', false);

async function post(path, body){
  const isInput = path === '/input' && body && body.type !== 'viewport';
  if (isInput) inputStart = Date.now();
  try {
    const r = await fetch(base + path, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body || {})});
    if (isInput) inputEnd = Date.now();
    if (r.status === 404) { gone(); return false; }
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) { toast(r.status === 400 && data.error ? data.error : '操作没成功，看一下画面再试'); return false; }
    return data.ok === undefined ? true : data;
  } catch (e) { toast('网络不通，请重试'); return false; }
}
function soon(){ [120, 500, 1100].forEach(ms => setTimeout(refresh, ms)); }

function layout(){
  if (!img.naturalWidth) return;
  const W = stage.clientWidth, H = stage.clientHeight, fit = Math.min(W / img.naturalWidth, H / img.naturalHeight);
  view.w = img.naturalWidth * fit; view.h = img.naturalHeight * fit;
  img.style.width = view.w + 'px'; img.style.height = view.h + 'px';
  place();
}
// 远端选中框的位置几乎不占流量，先画一个绿框占位；操作后的真实画面一到就撤掉，不和页面自己的选中框叠在一起
let ringBox = null, ringKey = '', nextIsFresh = false, inputStart = 0, inputEnd = 0, freshAt = 0;
function pictureCaughtUp(){ freshAt = Date.now(); ringBox = null; drawRing(); }
function noteFocusBox(box){
  const key = box ? JSON.stringify(box) : '';
  if (key === ringKey) return;
  ringKey = key;
  ringBox = box && freshAt < inputStart ? box : null;   // 画面已经赶上了就不用再画
  drawRing();
}
function drawRing(){
  const ring = $('ring');
  if (!ringBox || !img.naturalWidth || !vw) { ring.hidden = true; return; }
  const k = view.s * (view.w / vw);
  ring.style.left = `${view.x + ringBox.x * k - 2}px`;
  ring.style.top = `${view.y + ringBox.y * k - 2}px`;
  ring.style.width = `${ringBox.width * k + 4}px`;
  ring.style.height = `${ringBox.height * k + 4}px`;
  ring.hidden = false;
}
function place(){
  const W = stage.clientWidth, H = stage.clientHeight, w = view.w * view.s, h = view.h * view.s;
  view.x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, view.x));
  view.y = h <= H ? 0 : Math.min(0, Math.max(H - h, view.y));
  img.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.s})`;
  $('fit').classList.toggle('on', view.s > 1.01);
  drawRing();
}
function toPage(cx, cy){ const r = img.getBoundingClientRect();
  return {x: (cx - r.left) * vw / r.width, y: (cy - r.top) * vh / r.height, inside: cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom}; }

async function showFrame(blob){
  const url = URL.createObjectURL(blob), next = new Image();
  try {
    next.src = url; await next.decode();
    const ratio = img.naturalWidth ? next.naturalWidth / next.naturalHeight - img.naturalWidth / img.naturalHeight : 1;
    img.src = url; await img.decode();
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = url; frameNo++; $('loading').hidden = true; img.classList.add('ready');
    if (Math.abs(ratio) > .001 || !view.w) layout();
  } catch (e) { URL.revokeObjectURL(url); }
}
async function refresh(){
  if (liveOn) return;                               // 推流通着就不用再拉截图
  if (shotBusy) { refresh.again = true; return; }   // 正在取的这张可能早于刚才的操作，取完再来一张
  if (ended || document.hidden) return;
  shotBusy = true; refresh.again = false;
  const before = frameNo, askedAt = Date.now();
  try {
    const r = await fetch(base + '/shot', {cache: 'no-store'});
    if (r.status === 404) return gone();
    if (!r.ok) return;
    const blob = await r.blob();
    if (frameNo === before) {                       // 等它的时候推流已经送来更新的画面，这张就丢掉
      await showFrame(blob);
      if (inputEnd && askedAt >= inputEnd) pictureCaughtUp();
    }
  } catch (e) {} finally { shotBusy = false; if (refresh.again) setTimeout(refresh, 0); }
}
function connectLive(){
  if (live || liveStopped || ended || document.hidden || !window.WebSocket) return;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + base + '/live');
  ws.binaryType = 'blob'; live = ws;
  ws.onmessage = async e => {
    if (typeof e.data === 'string') {
      if (e.data === 'p') ws.send('q');             // 服务器量来回延迟
      else if (e.data === 'a') nextIsFresh = true;  // 下一帧是操作之后截的
      return;
    }
    const fresh = nextIsFresh; nextIsFresh = false;
    await showFrame(e.data);
    if (fresh) pictureCaughtUp();
    if (live === ws) { liveOn = true; liveRetry = 0; }
    if (ws.readyState === 1) ws.send('ok');
  };
  ws.onclose = e => {
    if (live !== ws) return;
    live = null; liveOn = false;
    if (e.code === 4404) { state(); return; }       // 接管结束，由 state 收到 404 显示结束页
    if (e.code === 4409) { liveStopped = true; return; }   // 别的页面接手了，这边退回拉截图
    if (!ended && !document.hidden) setTimeout(connectLive, Math.min(5000, 500 * 2 ** liveRetry++));
  };
}
function stopLive(){ const ws = live; live = null; liveOn = false; if (ws) ws.close(); }
// 远端焦点在登录表单的账号框、密码框上时，允许手机的密码管理器填这个输入框。
// 密码管理器只认得接管页面的地址，认不出远端是哪个网站，防不了钓鱼：页面跳到别的网站或没加密时一律关掉
function allowAutofill(focus){
  const kind = risky ? '' : focus === 'password' ? 'current-password' : focus === 'username' ? 'username' : '';
  text.setAttribute('autocomplete', kind || 'off');
  text.name = kind ? (kind === 'username' ? 'username' : 'password') : 'remote-input';
  $('form').setAttribute('autocomplete', kind ? 'on' : 'off');
  if (kind) { text.removeAttribute('data-1p-ignore'); text.removeAttribute('data-lpignore'); }
  else { text.setAttribute('data-1p-ignore', ''); text.setAttribute('data-lpignore', 'true'); }
  return Boolean(kind);
}
function applyFocus(focus){
  const hidden = focus === 'password' || focus === 'unknown';
  if (focus !== lastFocus) { lastFocus = focus; revealed = false; }
  text.type = hidden && !revealed ? 'password' : 'text';
  const autofill = allowAutofill(focus);
  const reveal = $('reveal');
  reveal.hidden = !hidden;
  reveal.setAttribute('aria-pressed', String(revealed));
  reveal.setAttribute('aria-label', revealed ? '隐藏内容' : '显示内容');
  text.placeholder = focus === 'password' ? '输入密码' : '输入内容';
  $('hint').dataset.state = focus;
  $('hint-text').textContent = focus === 'password'
      ? (autofill ? '密码只发给浏览器，Agent 看不到；可以用手机的密码管理器填' : '密码只发给浏览器，Agent 看不到')
    : focus === 'unknown' ? '嵌入页面里的输入框，内容已遮住'
    : focus === 'username' ? (autofill ? '已选中账号输入框，可以用手机的密码管理器填' : '已选中账号输入框')
    : focus === 'text' ? '已选中输入框' : '点一下画面中的输入框';
}
function tick(){
  if (!deadline || ended) return;
  const left = Math.max(0, Math.round((deadline - Date.now()) / 1000)), clock = $('clock');
  clock.textContent = `剩余 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
  clock.classList.toggle('low', left < 300);
}
async function state(){
  if (ended || document.hidden) return;
  try {
    const r = await fetch(base + '/state', {cache: 'no-store'});
    if (r.status === 404) return gone();
    if (!r.ok) return;
    const s = await r.json();
    vw = s.width; vh = s.height; deadline = Date.now() + s.remaining_s * 1000; tick();
    $('host').textContent = s.host || '正在打开…';
    $('sec').textContent = s.secure ? '加密连接 ·' : '未加密，不要输入密码 ·';
    $('site').classList.toggle('insecure', !s.secure);
    $('badge-icon').setAttribute('href', s.secure ? '#i-lock' : '#i-open');
    $('offsite').classList.toggle('on', s.offsite);
    $('offsite-text').textContent = s.offsite ? `页面已跳到 ${s.host}，不是原来要登录的网站。确认可信再输入密码。` : '';
    risky = Boolean(s.offsite || !s.secure);
    applyFocus(s.focus);
    noteFocusBox(s.focus_box || null);
  } catch (e) {}
}
async function syncViewport(){
  if (!(matchMedia('(pointer:coarse)').matches || innerWidth <= 820)) return;
  await post('/input', {type: 'viewport', width: stage.clientWidth, height: stage.clientHeight});
  view.s = 1; await state(); await refresh(); layout();
}

// 手势：点按＝点击，单指滑动＝翻页（放大后是移动画面），双指＝缩放
const pts = new Map(); let gesture = null, pendingY = 0, pendingX = 0, scrollAt = null, scrollTimer = 0;
function flushScroll(){
  scrollTimer = 0;
  if (!scrollAt || (!pendingX && !pendingY)) return;
  const dx = pendingX, dy = pendingY; pendingX = pendingY = 0;
  post('/input', {type: 'scroll', x: scrollAt.x, y: scrollAt.y, dx, dy}).then(() => { state(); soon(); });
}
function queueScroll(dx, dy, at){ ringBox = null; drawRing(); pendingX += dx; pendingY += dy; scrollAt = at; if (!scrollTimer) scrollTimer = setTimeout(flushScroll, 90); }
stage.addEventListener('pointerdown', e => {
  if (e.target === $('fit')) return;
  stage.setPointerCapture(e.pointerId);
  pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (pts.size === 1) gesture = {type: 'tap', sx: e.clientX, sy: e.clientY, lx: e.clientX, ly: e.clientY, at: Date.now()};
  if (pts.size === 2) { const [a, b] = [...pts.values()], r = stage.getBoundingClientRect();
    gesture = {type: 'pinch', d: Math.hypot(a.x - b.x, a.y - b.y) || 1, s: view.s,
      cx: ((a.x + b.x) / 2 - r.left - view.x) / view.s, cy: ((a.y + b.y) / 2 - r.top - view.y) / view.s}; }
});
stage.addEventListener('pointermove', e => {
  if (!pts.has(e.pointerId) || !gesture) return;
  pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (gesture.type === 'pinch' && pts.size >= 2) {
    const [a, b] = [...pts.values()], r = stage.getBoundingClientRect();
    view.s = Math.min(4, Math.max(1, gesture.s * Math.hypot(a.x - b.x, a.y - b.y) / gesture.d));
    view.x = (a.x + b.x) / 2 - r.left - gesture.cx * view.s; view.y = (a.y + b.y) / 2 - r.top - gesture.cy * view.s;
    place(); return;
  }
  if (gesture.type === 'tap' && Math.hypot(e.clientX - gesture.sx, e.clientY - gesture.sy) > 8) gesture.type = 'drag';
  if (gesture.type !== 'drag') return;
  const mx = e.clientX - gesture.lx, my = e.clientY - gesture.ly; gesture.lx = e.clientX; gesture.ly = e.clientY;
  if (view.s > 1.01) { view.x += mx; view.y += my; place(); return; }
  const rect = img.getBoundingClientRect(), k = vh / rect.height, at = toPage(gesture.sx, gesture.sy);
  queueScroll(-mx * k, -my * k, {x: at.x, y: at.y});
});
function pointerEnd(e){
  if (!pts.delete(e.pointerId)) return;
  if (gesture && gesture.type === 'tap' && e.type === 'pointerup' && Date.now() - gesture.at < 700) {
    const at = toPage(e.clientX, e.clientY);
    if (at.inside && vw) {
      const r = stage.getBoundingClientRect(), dot = document.createElement('div');
      dot.className = 'ripple'; dot.style.left = e.clientX - r.left + 'px'; dot.style.top = e.clientY - r.top + 'px';
      stage.appendChild(dot); setTimeout(() => dot.remove(), 460);
      post('/input', {type: 'click', x: at.x, y: at.y}).then(() => { state(); soon(); setTimeout(state, 350); });
    }
  }
  if (pts.size === 0) gesture = null; else if (gesture && gesture.type === 'pinch') gesture = {type: 'done'};
}
stage.addEventListener('pointerup', pointerEnd);
stage.addEventListener('pointercancel', pointerEnd);
stage.addEventListener('wheel', e => { e.preventDefault(); const at = toPage(e.clientX, e.clientY); queueScroll(e.deltaX, e.deltaY, {x: at.x, y: at.y}); }, {passive: false});
$('fit').onclick = () => { view.s = 1; place(); };

// 手机键盘可能还把最后几个字留在候选（组字）状态，这时读 value 会少字：等组字结束再读
let composing = false;
text.addEventListener('compositionstart', () => { composing = true; });
text.addEventListener('compositionend', () => { composing = false; });
const settled = () => !composing ? Promise.resolve() : new Promise(done => {
  const timer = setTimeout(done, 500);
  text.addEventListener('compositionend', () => { clearTimeout(timer); setTimeout(done, 0); }, {once: true});
});
$('form').addEventListener('submit', async e => {
  e.preventDefault();
  await settled();
  const value = text.value;
  if (!value) { toast('还没有输入内容'); text.focus(); return; }
  const secret = text.type === 'password' || lastFocus === 'password' || lastFocus === 'unknown';
  text.value = ''; toast('正在输入…');
  // 不是密码的话把输进去的内容回显一下，少了字一眼能看出来
  if (await post('/input', {type: 'text', text: value})) { toast(secret ? '已输入到页面' : `已输入：${value}`); soon(); }
  else text.value = value;
});
// 删除页面里已输入的字符：连按的次数攒起来发，路上只有一个请求，顺序不会乱
let delPending = 0, delBusy = false, delTimer = 0, delAt = 0;
async function pumpDelete(){
  if (delBusy) return;
  delBusy = true;
  try {
    while (delPending > 0) {
      const count = Math.min(delPending, 20); delPending -= count;
      if (!await post('/input', {type: 'key', key: 'Backspace', count})) { delPending = 0; break; }
    }
  } finally { delBusy = false; soon(); }
}
function deleteOne(){ delAt = Date.now(); delPending++; pumpDelete(); }
function stopDelete(){ clearTimeout(delTimer); clearInterval(delTimer); delTimer = 0; }
$('del').addEventListener('pointerdown', e => {
  e.preventDefault();       // 不抢走输入框的焦点，手机键盘不会收起
  deleteOne(); stopDelete();
  delTimer = setTimeout(() => { delTimer = setInterval(deleteOne, 90); }, 420);
});
for (const name of ['pointerup', 'pointercancel', 'pointerleave']) $('del').addEventListener(name, stopDelete);
$('del').addEventListener('click', e => { if (e.detail === 0) deleteOne(); });     // 键盘或读屏触发的点击
// 下方输入框是空的时候，手机键盘上的退格就是删页面里的字
text.addEventListener('keydown', e => {
  if ((e.key === 'Backspace' || e.keyCode === 8) && !text.value) { e.preventDefault(); deleteOne(); }
});
text.addEventListener('beforeinput', e => {
  if (e.inputType === 'deleteContentBackward' && !text.value && Date.now() - delAt > 60) { e.preventDefault(); deleteOne(); }
});
$('reveal').onclick = () => { revealed = !revealed; applyFocus(lastFocus); text.focus(); };
$('enter').onclick = () => post('/input', {type: 'key', key: 'Enter'}).then(() => { soon(); setTimeout(state, 700); });
$('tab').onclick = () => post('/input', {type: 'key', key: 'Tab'}).then(() => { state(); soon(); });
$('back').onclick = () => post('/input', {type: 'back'}).then(() => { state(); soon(); });
// "完成"只是把浏览器交还给 Agent，不代表登录成功；页面上还有密码框时先问一句
const handBack = () => finish('已交还给 Agent', 'Agent 会先看一眼页面再接着做；没登录上的话它会告诉你。可以关掉这个页面了。', true);
$('done').onclick = async () => {
  const r = await post('/done');
  if (r && r.confirm === 'still_login') $('unsure').showModal();
  else if (r) handBack();
};
$('stay').onclick = () => $('unsure').close();
// 光标还在密码框里时，回车就是提交登录表单
$('submit-login').onclick = async () => {
  $('unsure').close();
  if (await post('/input', {type: 'key', key: 'Enter'})) { toast('已提交，等页面跳走再点“登录好了”'); soon(); setTimeout(state, 700); }
};
$('anyway').onclick = async () => { if (await post('/done', {force: true})) handBack(); };
$('info').onclick = () => $('why').showModal();
$('why-close').onclick = () => $('why').close();
$('cancel').onclick = () => $('ask').showModal();
$('keep').onclick = () => $('ask').close();
$('really').onclick = async () => { if (await post('/cancel')) finish('已取消登录', 'Agent 已收到通知，可以关掉这个页面了。', false); };
for (const d of document.querySelectorAll('dialog')) d.addEventListener('click', e => { if (e.target === d) d.close(); });

function fitScreen(){
  const v = window.visualViewport, root = document.documentElement.style, height = v ? v.height : innerHeight;
  root.setProperty('--app-h', height + 'px');
  root.setProperty('--app-top', (v ? v.offsetTop : 0) + 'px');
  $('app').classList.toggle('compact', height < 560);
  layout();
}
if (window.visualViewport) { visualViewport.addEventListener('resize', fitScreen); visualViewport.addEventListener('scroll', fitScreen); }
addEventListener('resize', fitScreen);
addEventListener('orientationchange', () => setTimeout(() => { fitScreen(); syncViewport(); }, 350));
document.addEventListener('visibilitychange', () => {   // 切到后台就停推，省手机流量和服务器 CPU
  if (document.hidden) stopLive(); else { state(); refresh(); connectLive(); }
});
fitScreen();
state().then(syncViewport).then(refresh).then(connectLive);
setInterval(refresh, 1500); setInterval(state, 2000); setInterval(tick, 1000);
</script></body></html>"""


def build(app: App) -> web.Application:
    def _browser() -> BrowserAgent:
        """接管页面上的操作都先过了 takeover_of：浏览器关着时那里就 404 了。"""
        assert app.browser is not None
        return app.browser

    sockets: set[web.WebSocketResponse] = set()

    def takeover_of(request: web.Request) -> Takeover:
        tk = _browser().takeover_by_key(request.match_info["key"]) if app.browser else None
        if tk is None:
            raise web.HTTPNotFound(text="这个接管已经结束或不存在。")
        return tk

    def allowed_hosts(request: web.Request) -> set[str]:
        """接管页面自己的地址。经 Tailscale 转发时，浏览器看到的是配置里的公开地址。"""
        hosts = {request.host}
        if app.cfg.takeover_url:
            hosts.add(urlsplit(app.cfg.takeover_url).netloc)
        return hosts

    async def close_sockets(_: web.Application) -> None:
        for ws in list(sockets):
            await ws.close(code=LIVE_ENDED)

    async def page(request: web.Request) -> web.Response:
        tk = takeover_of(request)
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
               "img-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        # 先填 nonce：Agent 写的原因里就算有占位符，也拿不到它
        body = (PAGE.replace("__NONCE__", nonce)
                .replace("__URL__", html.escape(tk.url))
                .replace("__REASON__", html.escape(tk.reason)))
        return web.Response(text=body, content_type="text/html",
                            headers={"cache-control": "no-store", "referrer-policy": "no-referrer",
                                     "content-security-policy": csp, "x-content-type-options": "nosniff"})

    async def shot(request: web.Request) -> web.Response:
        takeover_of(request)
        return web.Response(body=await _browser().takeover_shot(), content_type="image/jpeg",
                            headers={"cache-control": "no-store"})

    async def live(request: web.Request) -> web.StreamResponse:
        """推画面。每发一帧等页面回一句 ok 再发下一帧，手机跟不上就掉帧。"""
        tk = takeover_of(request)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc not in allowed_hosts(request):
            raise web.HTTPForbidden(text="来源不对")
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=64)
        await ws.prepare(request)
        sockets.add(ws)
        shown = asyncio.Event()
        view = None
        pinged_at = 0.0

        async def read() -> None:
            try:
                async for message in ws:
                    if message.type != web.WSMsgType.TEXT:
                        continue
                    if message.data == "ok":
                        shown.set()
                    elif message.data == "q" and pinged_at and view is not None:
                        view.note_rtt(time.monotonic() - pinged_at)
            finally:
                shown.set()

        async def ping() -> None:
            nonlocal pinged_at
            pinged_at = time.monotonic()
            await ws.send_str("p")

        reader = asyncio.ensure_future(read())
        code = LIVE_ENDED
        try:
            view = await _browser().open_live()
            await ping()
            while not ws.closed and not view.closed and _browser().takeover_by_key(tk.key) is tk:
                frame = await view.next()
                if ws.closed or _browser().takeover_by_key(tk.key) is not tk:
                    continue
                if frame is None:
                    if time.monotonic() - pinged_at >= LIVE_PING_EVERY_S:
                        await ping()
                    continue
                shown.clear()
                sent_at = time.monotonic()
                if view.after_input:            # 告诉页面：这张已经是操作后的真实画面，可以撤掉占位的绿框
                    await ws.send_str("a")
                await ws.send_bytes(frame)     # 路上只留一帧，下一帧截的就是那时最新的画面
                give_up = sent_at + LIVE_ACK_TIMEOUT_S
                while (not shown.is_set() and time.monotonic() < give_up and not ws.closed and not view.closed
                       and _browser().takeover_by_key(tk.key) is tk):      # 接管结束要马上收场
                    try:
                        await asyncio.wait_for(shown.wait(), 0.25)
                    except TimeoutError:
                        pass
                view.delivered(len(frame), time.monotonic() - sent_at)
                await asyncio.sleep(LIVE_MIN_GAP_S)
            if view.closed:
                code = LIVE_REPLACED
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as e:      # 页面回到轮询截图，还能继续用
            log.info("画面推送中断：%s", type(e).__name__)
            code = LIVE_FAILED
        finally:
            sockets.discard(ws)
            reader.cancel()
            if view is not None:
                await _browser().close_live(view)
            if not ws.closed:
                await ws.close(code=code)
        return ws

    async def state(request: web.Request) -> web.Response:
        tk = takeover_of(request)
        return web.json_response(await _browser().takeover_state(tk), headers={"cache-control": "no-store"})

    async def input_(request: web.Request) -> web.Response:
        takeover_of(request)
        try:
            await _browser().takeover_input(await request.json())
        except BrowserError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except (KeyError, ValueError, TypeError):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        except Exception as e:      # 页面在跳转之类，下一次截图会显示现状。错误信息里可能带输入内容，不记
            log.info("接管操作失败：%s", type(e).__name__)
            return web.json_response({"ok": False, "error": type(e).__name__})
        return web.json_response({"ok": True})

    async def done(request: web.Request) -> web.Response:
        tk = takeover_of(request)
        try:
            force = bool((await request.json()).get("force"))
        except Exception:
            force = False
        if not force and await _browser().still_on_login():
            # 页面上还有密码框：多半还没点登录，或者登录没成功。先问你一句
            return web.json_response({"ok": True, "confirm": "still_login"})
        _browser().finish_takeover(tk)
        return web.json_response({"ok": True, "done": True})

    async def cancel(request: web.Request) -> web.Response:
        _browser().finish_takeover(takeover_of(request), cancelled=True)
        return web.json_response({"ok": True})

    async def activity_page(request: web.Request) -> web.Response:
        """只读的活动记录。没有脚本；内容全部转义。"""
        days = app.activity_page_days(request.match_info["key"])
        if days is None:
            raise web.HTTPNotFound(text="这个链接已经失效。在聊天里再发一次 /activity。")
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")
        body = activity.html_page(activity.collect(app.db, days), nonce)
        return web.Response(text=body, content_type="text/html",
                            headers={"cache-control": "no-store", "referrer-policy": "no-referrer",
                                     "content-security-policy": csp, "x-content-type-options": "nosniff"})

    web_app = web.Application()
    web_app.on_shutdown.append(close_sockets)
    web_app.router.add_get("/takeover/{key}", page)
    web_app.router.add_get("/takeover/{key}/live", live)
    web_app.router.add_get("/takeover/{key}/shot", shot)
    web_app.router.add_get("/takeover/{key}/state", state)
    web_app.router.add_post("/takeover/{key}/input", input_)
    web_app.router.add_post("/takeover/{key}/done", done)
    web_app.router.add_post("/takeover/{key}/cancel", cancel)
    web_app.router.add_get("/activity/{key}", activity_page)
    return web_app
