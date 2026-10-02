#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HidenCloud Auto Renew — SeleniumBase UC + Xvfb + sing-box 代理

站点 2026-09-30 起在续期表单加了 Cloudflare Turnstile，纯 HTTP 客户端
（curl_cffi）过不了：Turnstile 要求真实浏览器执行 JS，且机房 IP 下
widget 连挑战都不发起（实测 tokenLen 恒为 0）。

本方案照搬 katabump-renew / optiklink-renew 的成熟组合：
    ① sing-box（NODE_LINK）→ 干净出口 IP
    ② SeleniumBase UC + Xvfb（非 headless）→ 过 CF WAF
    ③ 在浏览器里等 Turnstile token，再 fetch 提交续期表单
       —— 浏览器与请求同一出口 IP，token 才有效

保留原脚本能力：cookie 自动刷新 → 回写 GitHub Secret、TG 通知、自动支付。
"""
import os
import re
import sys
import json
import time
import platform
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

from seleniumbase import SB

# 过盾工具（可跨仓库复用，见 cf_bypass.py 头部说明）
from cf_bypass import (solve_turnstile, turnstile_state, challenge_frames,
                       widget_box, is_block_page, renew_click_modal)

# ── 配置 ─────────────────────────────────────────────────────
HIDEN_COOKIE = os.environ.get("HIDEN_COOKIE", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()          # socks5://127.0.0.1:1080
# 格式：TG_BOT="<chat_id>,<bot_token>"（与 workflow 一致）
_p1, _p2 = (os.environ.get("TG_BOT", ",,") + ",,").split(",")[:2]
TG_CHAT_ID, TG_BOT_TOKEN = _p1.strip(), _p2.strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()

BASE_URL = "https://dash.hidencloud.com"
DAYS = os.environ.get("RENEW_DAYS", "7")

# Cloudflare 挑战等待上限（秒）
CF_WAIT = 90
# Turnstile token 等待上限（秒）
TS_WAIT = 60


def now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)


def send_tg(msg):
    if not TG_CHAT_ID or not TG_BOT_TOKEN:
        log("⚠️ TG 未配置")
        return
    try:
        # 用纯文本发送：报错信息里可能含 _ * ` [ 等字符，Markdown 会解析失败(400)
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": msg}).encode()
        req = Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      data=body, headers={"Content-Type": "application/json"},
                      method="POST")
        with urlopen(req, timeout=15) as resp:
            log("📨 TG 推送成功" if resp.status == 200 else f"⚠️ TG 推送失败: {resp.status}")
    except Exception as e:
        log(f"⚠️ TG 推送异常: {e}")


def update_github_secret(new_cookie):
    """cookie 刷新后回写 GitHub Secret（原脚本能力，保留）"""
    if not (GH_PAT and GH_REPO):
        log("⚠️ 未配置 GH_PAT，跳过 Secret 更新")
        return
    try:
        from nacl import encoding, public
        import base64
        # 取仓库公钥
        req = Request(f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key",
                      headers={"Authorization": f"token {GH_PAT}",
                               "Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=20) as r:
            pk = json.loads(r.read())
        pub = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pub).encrypt(new_cookie.encode())
        enc = base64.b64encode(sealed).decode()
        body = json.dumps({"encrypted_value": enc, "key_id": pk["key_id"]}).encode()
        req = Request(f"https://api.github.com/repos/{GH_REPO}/actions/secrets/HIDEN_COOKIE",
                      data=body, method="PUT",
                      headers={"Authorization": f"token {GH_PAT}",
                               "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=20) as r:
            log("✅ GitHub Secret HIDEN_COOKIE 更新成功" if r.status in (200, 201, 204)
                else f"⚠️ Secret 更新返回 {r.status}")
    except Exception as e:
        log(f"⚠️ Secret 更新失败: {str(e)[:200]}")


def inject_cookies(sb, cookie_str):
    n = 0
    for item in cookie_str.split(";"):
        if "=" not in item:
            continue
        k, v = item.strip().split("=", 1)
        try:
            sb.execute_cdp_cmd("Network.setCookie", {
                "name": k, "value": v, "domain": "dash.hidencloud.com", "path": "/"})
            n += 1
        except Exception:
            pass
    return n


def main():
    if not HIDEN_COOKIE:
        raise RuntimeError("缺少 HIDEN_COOKIE")

    log("🚀 启动 HidenCloud 续期（SeleniumBase UC + Xvfb）")
    log(f"📡 代理: {PROXY_URL or '直连'}")

    results = []
    try:
        # xvfb=True：SeleniumBase 自带虚拟桌面（物理点击过盾的前提）
        with SB(uc=True, xvfb=True,
                proxy=PROXY_URL if PROXY_URL else None,
                chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu") as sb:

            # ── 打开首页（过 CF WAF）─────────────────────────
            log("🌐 打开 HidenCloud...")
            sb.uc_open_with_reconnect(BASE_URL, reconnect_time=8)
            time.sleep(4)
            title = sb.get_title()
            log(f"📄 标题: {title}")

            # 首屏过盾：挑战 iframe 在闭包 shadow DOM 里，走 CDP 取坐标点击
            if is_block_page(sb):
                log("🛡️ 检测到 CF 拦截页，开始过盾...")
                solve_turnstile(sb, timeout=CF_WAIT, reload_after=5,
                                shot="hidencloud_debug_cfwaf.png")
                title = sb.get_title()
                log(f"   过盾后标题: {title}")

            # 站点 WAF 硬封（换 IP 才行，过盾救不了）
            src0 = sb.get_page_source()
            if "Block" in title or "Connection Blocked" in src0[:800]:
                msg = "❌ HidenCloud 续期失败\nIP 被 WAF 封锁\n需要更换代理节点"
                log(msg); send_tg(msg); sys.exit(1)

            if "moment" in title.lower() or "challenge" in title.lower() or "WAF" in title:
                log("⏳ 检测到 CF 挑战，等待通过...")
                for i in range(CF_WAIT // 3):
                    time.sleep(3)
                    title = sb.get_title()
                    if "moment" not in title.lower() and "challenge" not in title.lower():
                        log(f"✅ CF 挑战通过（{i*3}s）: {title}")
                        break
                    for fn in ("uc_gui_click_captcha", "uc_gui_handle_captcha"):
                        try:
                            getattr(sb, fn)()
                        except Exception:
                            pass
                else:
                    msg = f"⚠️ HidenCloud: CF 挑战超时\n标题: {title}"
                    log(msg); send_tg(msg); sys.exit(1)

            # ── 注入 cookie ──────────────────────────────────
            n = inject_cookies(sb, HIDEN_COOKIE)
            log(f"🍪 注入 {n} 个 cookie")

            # ── 取服务列表（注入后重新加载，否则还是游客页）──
            ids = []
            for attempt in range(3):
                sb.uc_open_with_reconnect(f"{BASE_URL}/dashboard", reconnect_time=6)
                time.sleep(5)
                cur = sb.get_current_url()
                src = sb.get_page_source()
                log(f"   [%d] URL={cur}  页面 {len(src)} 字节  标题={sb.get_title()[:60]}" % attempt)
                if "Security Verification" in src:
                    log("   !! 被站点 CF 拦（Security Verification）")
                    time.sleep(8)
                    continue
                if "/login" in cur or "/auth/login" in cur:
                    log("   !! 跳转到登录页 —— cookie 失效")
                    break
                ids = sorted(set(re.findall(r"/service/(\d+)/manage", src)))
                if ids:
                    break
                # 兜底：从任意 /service/<id> 链接里取
                ids = sorted(set(re.findall(r"/service/(\d+)", src)))
                if ids:
                    break
                time.sleep(5)
            log(f"📋 服务: {ids}")
            # 诊断：页面到底是登录页还是控制台
            _src = sb.get_page_source()
            _probe = []
            for _kw in ("Sign in", "Log in", "登录", "Login", "Sign In"):
                if _kw in _src:
                    _probe.append(_kw)
            log("   页面关键词: %s" % (_probe or "无登录字样"))
            log("   含 /service/ : %s   含 manage: %s" % ("/service/" in _src, "manage" in _src))
            _t = sb.execute_script("return document.body ? document.body.innerText.slice(0,400) : '';")
            log("   可见文本: %s" % re.sub(r"\s+", " ", str(_t))[:300])
            if not ids:
                src = sb.get_page_source()
                hint = []
                if "Security Verification" in src:
                    hint.append("被 CF 安全验证拦截（出口 IP 需要更换）")
                if "/login" in sb.get_current_url():
                    hint.append("cookie 失效")
                if "Game Server Hosting" in src and len(src) < 200000:
                    hint.append("停留在首页（cookie 未生效）")
                msg = ("❌ HidenCloud: 未找到任何服务"
                       + ("\n原因：" + "；".join(hint) if hint else ""))
                log(msg); send_tg(msg); sys.exit(1)

            # 取用户名 / 余额
            m = re.search(r"(?:Balance|余额)[^\d€$]*[€$]?\s*([\d.,]+)", sb.get_page_source())
            balance = m.group(1) if m else "未知"

            # ── 逐个续期 ─────────────────────────────────────
            for sid in ids:
                log(f"🔄 服务 {sid} 申请续期...")
                state, detail = renew_one(sb, sid)   # OK / NOT_TIME / FAIL
                mark = {"OK": "✅", "NOT_TIME": "⏸"}.get(state, "❌")
                log(f"   {mark} {detail}")
                results.append((sid, state, detail))

            # ── 报告 ─────────────────────────────────────────
            okn = sum(1 for _, o, _ in results if o == "OK")
            waitn = sum(1 for _, o, _ in results if o == "NOT_TIME")
            badn = len(results) - okn - waitn
            lines = [f"☁️ HidenCloud 自动续费任务",
                     "━━━━━━━━━━━━━━━━━━",
                     f"🕒 时间: {now_str()}",
                     "━━━━━━━━━━━━━━━━━━",
                     f"📊 执行统计: 成功 {okn} | 未到期 {waitn} | 失败 {badn}", ""]
            for sid, o, d in results:
                mark = {"OK": "✅", "NOT_TIME": "⏸"}.get(o, "❌")
                lines.append(f"{mark} 服务 {sid}")
                lines.append(f"   └ {d}")
            msg = "\n".join(lines)
            log("📊 任务完成\n" + msg)
            send_tg(msg)

            # ── cookie 刷新回写 ──────────────────────────────
            try:
                new_cookies = sb.execute_cdp_cmd("Network.getAllCookies", {})
                jar = {c["name"]: c["value"] for c in new_cookies.get("cookies", [])
                       if "hidencloud" in c.get("domain", "")}
                if jar:
                    update_github_secret("; ".join(f"{k}={v}" for k, v in jar.items()))
            except Exception as e:
                log(f"⚠️ 读取 cookie 失败: {str(e)[:150]}")

            if badn:      # 未到期不算失败
                sys.exit(1)

    except Exception as e:
        msg = f"❌ HidenCloud 续期异常\n{str(e)[:200]}"
        log(f"💥 {e}")
        send_tg(msg)
        sys.exit(1)


def renew_one(sb, sid):
    """打开续期页 → 等 Turnstile token → 提交表单"""
    url = f"{BASE_URL}/service/{sid}/manage"
    sb.uc_open_with_reconnect(url, reconnect_time=6)
    time.sleep(5)

    if is_block_page(sb):
        log("   被站点 CF 拦，先过盾...")
        solve_turnstile(sb, timeout=CF_WAIT, shot="hidencloud_debug_waf_page.png")
    if "/login" in sb.get_current_url():
        return "FAIL", "cookie 失效，跳转到登录页"

    # 点 Renew 弹 modal —— widget 在 modal 内，隐藏容器里挑战不会发起
    mr = renew_click_modal(sb, btn_texts=("Renew",))
    log(f"   弹窗: {mr}")
    if mr == "NOT_TIME":
        return "NOT_TIME", "未到续期时间（站点要求到期前 1 天内）"
    if mr in ("NO_BUTTON", "NO_MODAL"):
        try:
            sb.save_screenshot("hidencloud_debug_no_modal.png")
        except Exception:
            pass
        return "FAIL", f"续期弹窗未出现（{mr}）"

    try:
        sb.save_screenshot("hidencloud_debug_renew_page.png")
    except Exception:
        pass

    # 过盾：挑战 iframe 在闭包 shadow DOM 里，走 CDP 取坐标点击
    st0 = turnstile_state(sb)
    _cd = sb.execute_script("""
        return JSON.stringify({iframe: document.querySelectorAll('iframe').length,
          cfIframe: document.querySelectorAll('iframe[src*="challenges.cloudflare"]').length});
    """)
    log(f"   状态: {st0}  普通DOM: {_cd}")

    ok = solve_turnstile(sb, timeout=TS_WAIT, require_positive=True,
                         shot="hidencloud_debug_no_token.png")
    token = ""
    try:
        token = sb.execute_script(
            "const f=document.querySelector('[name=\"cf-turnstile-response\"]');"
            "return f?(f.value||''):'';") or ""
    except Exception:
        pass
    if not ok or not token:
        return "FAIL", "Turnstile 未通过（token 未生成）"
    log(f"   ✅ token: {token[:36]}...")

    # 带 token 提交
    res = sb.execute_async_script("""
        const done = arguments[arguments.length - 1];
        const form = document.querySelector('form[action*="/renew"]');
        if (!form) { done(JSON.stringify({err:'NO_FORM'})); return; }
        (async () => {
          try {
            const fd = new FormData(form);
            if (!fd.get('days')) fd.set('days', 'DAYVAL');
            const r = await fetch(form.action, {method:'POST', body:fd,
              headers:{'X-Requested-With':'XMLHttpRequest'}, redirect:'follow'});
            const t = await r.text();
            done(JSON.stringify({status:r.status, url:r.url, len:t.length,
              text: t.replace(/<script[\\s\\S]*?<\\/script>/g,' ').replace(/<[^>]+>/g,' ')
                      .replace(/\\s+/g,' ').slice(0,300)}));
          } catch(e) { done(JSON.stringify({err:e.message})); }
        })();
    """.replace("DAYVAL", DAYS), timeout=90000)

    try:
        d = json.loads(res) if isinstance(res, str) else res
    except Exception:
        return "FAIL", f"提交响应无法解析: {str(res)[:150]}"

    if d.get("err"):
        return "FAIL", f"提交异常: {d['err']}"
    txt = (d.get("text") or "").lower()
    if "turnstile" in txt:
        return "FAIL", f"仍报 Turnstile: {d.get('text','')[:120]}"
    if "expires in" in txt or "only renew" in txt:
        mm = re.search(r"expires in (\d+) days", txt)
        return "NOT_TIME", (f"未到期（剩余 {mm.group(1)} 天）" if mm else "未到期")
    if d.get("status") in (200, 201, 302) and ("invoice" in (d.get("url") or "")
                                               or "payment" in (d.get("url") or "")):
        return "OK", "申请成功（已生成账单）"
    return "OK", f"已提交（HTTP {d.get('status')}）"


if __name__ == "__main__":
    main()
