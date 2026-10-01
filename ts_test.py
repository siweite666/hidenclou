# -*- coding: utf-8 -*-
"""在 CI 里实测：SeleniumBase UC + Xvfb 能否过 HidenCloud 的 Turnstile

只做只读探测：
  1. 浏览器加载续期页 → 看 widget 有没有出 token
  2. 有 token 就带着它 POST /service/221433/renew（这是真提交，会真续期）
     —— 未到期的话服务端会回 "only renew"，那也是成功的证明
"""
import os, re, json, time, platform

if platform.system().lower() == "linux":
    from pyvirtualdisplay import Display
    _d = Display(visible=False, size=(1920, 1080))
    _d.start()
    os.environ["DISPLAY"] = _d.new_display_var

from seleniumbase import SB

COOKIE = os.environ.get("HIDEN_COOKIE", "")
SID = "221433"
BASE = "https://dash.hidencloud.com"


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def main():
    with SB(uc=True, headless=False,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu") as sb:
        # ① 先访问一次域，再注入 cookie
        log("打开首页...")
        sb.uc_open_with_reconnect(BASE, reconnect_time=8)
        time.sleep(4)
        log("标题: %s" % sb.get_title())

        ok = sb.execute_script("return document.readyState")
        log("readyState=%s" % ok)

        # 注入 cookie
        n = 0
        for item in COOKIE.split(";"):
            if "=" not in item:
                continue
            k, v = item.strip().split("=", 1)
            try:
                sb.execute_cdp_cmd("Network.setCookie", {
                    "name": k, "value": v, "domain": "dash.hidencloud.com", "path": "/"})
                n += 1
            except Exception as e:
                log("  cookie %s 注入失败: %s" % (k, str(e)[:80]))
        log("注入 %d 个 cookie" % n)

        # ② 打开续期页
        url = "%s/service/%s/manage" % (BASE, SID)
        log("打开 %s" % url)
        sb.uc_open_with_reconnect(url, reconnect_time=8)
        time.sleep(6)
        log("标题: %s" % sb.get_title())
        log("URL: %s" % sb.get_current_url())

        src = sb.get_page_source()
        log("页面 %d 字节" % len(src))
        if "Security Verification" in src:
            log("!! 被站点 CF 拦（Security Verification）")
            return
        if "login" in sb.get_current_url():
            log("!! 未登录（cookie 失效）")
            return

        # ③ 找 widget 状态
        info = sb.execute_script("""
            const w = document.querySelector('.cf-turnstile');
            const f = document.querySelector('[name="cf-turnstile-response"]');
            return JSON.stringify({
              widget: !!w,
              sitekey: w ? w.getAttribute('data-sitekey') : null,
              iframe: !!document.querySelector('iframe[src*="challenges.cloudflare"]'),
              field: !!f,
              tokenLen: f ? (f.value || '').length : 0,
              tokenHead: f && f.value ? f.value.slice(0,24) : null,
              form: !!document.querySelector('form[action*="/renew"]')
            });
        """)
        log("widget 状态: %s" % info)

        # ④ 等 token 出现（最多 60s）
        token = ""
        for i in range(20):
            token = sb.execute_script("""
                const f = document.querySelector('[name="cf-turnstile-response"]');
                return f ? (f.value || '') : '';
            """)
            if token:
                log("✅ 拿到 token（第 %ds）: %s..." % (i * 3, token[:40]))
                break
            time.sleep(3)
        if not token:
            log("❌ 60s 内没拿到 token —— Turnstile 没通过")
            sb.execute_script("""
                const w=document.querySelector('.cf-turnstile');
                if(w) w.scrollIntoView({block:'center'});
            """)
            time.sleep(3)
            sb.save_screenshot("/tmp/ts.png")
            log("已存截图 /tmp/ts.png")
            return

        # ⑤ 带 token 真提交（读回表单字段，保证和站点一致）
        res = sb.execute_async_script("""
            const done = arguments[arguments.length - 1];
            const form = document.querySelector('form[action*="/renew"]');
            if (!form) { done("NO_FORM"); return; }
            (async () => {
              try {
                const fd = new FormData(form);
                const r = await fetch(form.action, {
                  method: 'POST', body: fd, headers: {'X-Requested-With':'XMLHttpRequest'},
                  redirect: 'follow'
                });
                const t = await r.text();
                done(JSON.stringify({status: r.status, url: r.url, len: t.length,
                                     snip: t.replace(/<[^>]+>/g,' ').replace(/\\s+/g,' ').slice(0,300)}));
              } catch (e) { done("EXC " + e.message); }
            })();
        """, timeout=90000)
        log("提交结果: %s" % str(res)[:600])


if __name__ == "__main__":
    main()
