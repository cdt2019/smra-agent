"""测试网店（格物仓）的登录页。

只在测试里、假域名 shop.example 上出现。输入框是真的，接管页面要真的点、真的输入。
"""
from __future__ import annotations

_MARK = '<span class="mark">' + "".join(
    f'<i{" class=on" if i in (0, 3, 4, 6, 7, 8) else ""}></i>' for i in range(9)) + "</span>"

CSS = """
:root{--bg:#fcfcfc;--surface:#fff;--surface-2:#f6f6f6;--border:#e6e6e6;--border-strong:#d6d6d6;--text:#1c1c1c;--text-2:#6b6b6b;--text-3:#8f8f8f;
 --primary:#26c77d;--primary-hover:#1fb66f;--primary-soft:#eaf8f0;--primary-ink:#0b7a47;--on-primary:#052e1a;--danger-ink:#b42318}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.au-main{min-height:100vh;display:flex;flex-direction:column;padding:12px 16px 20px}
.au-top{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--text-2)}
.mark{display:grid;grid-template-columns:repeat(3,7px);gap:2px}
.mark i{width:7px;height:7px;border-radius:2px;background:var(--border-strong)}.mark i.on{background:var(--primary)}
.au-form{width:100%;max-width:400px;margin:0 auto;padding:20px 0 28px;display:flex;flex-direction:column;gap:18px}
h1{margin:0;font-size:24px;font-weight:800;letter-spacing:-.01em}
.lead{margin:-12px 0 0;color:var(--text-2)}
.seg{display:flex;padding:2px;background:var(--surface-2);border-radius:6px}
.seg a{flex:1;height:36px;display:flex;align-items:center;justify-content:center;border:1px solid transparent;border-radius:5px;color:var(--text-2)}
.seg a.on{background:var(--surface);border-color:var(--border);color:var(--text);font-weight:600}
.fd{display:flex;flex-direction:column;gap:6px;font-weight:600}
.fd small{display:flex;justify-content:space-between;font-size:14px}
.fd small a{font-size:12.5px;font-weight:600;color:var(--primary-ink)}
.fd input{width:100%;height:42px;padding:0 12px;border:1px solid var(--border-strong);border-radius:6px;background:var(--surface);color:var(--text);font-family:inherit;font-size:14.5px;font-weight:400;outline:0}
.fd input:focus{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)}
.fd input.bad{border-color:var(--danger-ink)}
.err{font-size:12.5px;font-weight:400;color:var(--danger-ink)}
.btn{height:40px;display:flex;align-items:center;justify-content:center;border:1px solid var(--border-strong);border-radius:6px;background:var(--surface);color:var(--text);font-family:inherit;font-size:14px;font-weight:600;cursor:pointer}
.btn.pri{height:44px;background:var(--primary);border-color:var(--primary-hover);color:var(--on-primary)}
.or{display:flex;align-items:center;gap:12px;font-size:12.5px;color:var(--text-3)}
.or::before,.or::after{content:"";flex:1;height:1px;background:var(--border)}
.au-ft{margin-top:auto;display:flex;flex-wrap:wrap;justify-content:center;gap:4px 16px;font-size:12.5px;color:var(--text-3)}
"""


def login_body(error: bool = False) -> str:
    bad, message = (" class=bad", '<span class="err">邮箱或密码不正确</span>') if error else ("", "")
    return f"""<div class="au-main">
<div class="au-top">{_MARK}<a href="/">← 返回商店</a></div>
<form class="au-form" action="/login" method="post">
<h1>登录格物仓</h1><p class="lead">看订单、售后和钱包</p>
<div class="seg"><a class="on" href="/login">登录</a><a href="/login">注册</a></div>
<label class="fd"><small>邮箱</small><input id="user" name="user" inputmode="email" autocomplete="username" autocapitalize="off"></label>
<label class="fd"><small>密码<a href="/login">忘记密码？</a></small><input id="pw"{bad} name="password" type="password" autocomplete="current-password">{message}</label>
<button class="btn pri">登录</button>
<div class="or">或者</div><a class="btn" href="/orders">不登录，用下单邮箱查订单</a>
</form>
<div class="au-ft"><span>© 2026 格物仓 GRIDVAULT</span><a href="/">服务条款</a><a href="/">隐私政策</a><a href="/">帮助中心</a></div>
</div>"""
