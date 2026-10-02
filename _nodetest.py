#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点 / 出口 IP 过 Cloudflare 能力测试（只读）

登录页 /auth/login 有两道门，本次全程观察：
  ① CF 中间页 "Just a moment..."（托管挑战，需浏览器自己解开）
  ② 解开后页面上的 Turnstile widget（过了才显示账号密码框）

只观察，不登录、不提交任何表单。
"""
import os
import sys
import time
import urllib.request

from seleniumbase import SB

from cf_bypass import (solve_turnstile, turnstile_state, challenge_frames,
                       is_block_page, widget_box)

LOGIN_URL = "https://dash.hidencloud.com/auth/login"
HOME_URL = "https://dash.hidencloud.com"
PROXY = os.environ.get("PROXY_URL", "").strip()
LABEL = os.environ.get("TEST_LABEL", "test")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def egress_ip():
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip",
              "https://ipinfo.io/ip"):
        try:
            return urllib.request.urlopen(u, timeout=15).read().decode().strip()
        except Exception:
            continue
    return "获取失败"


def ip_info(ip):
    try:
        d = urllib.request.urlopen(
            "https://ipinfo.io/%s/json" % ip, timeout=15).read().decode()
        import json as j
        o = j.loads(d)
        return "ASN=%s org=%s country=%s" % (
            o.get("org", "?"), o.get("org", "?"), o.get("country", "?"))
    except Exception:
        return "查询失败"


def main():
    log("=" * 58)
    log("测试: %s" % LABEL)
    log("代理: %s" % (PROXY or "直连"))
    ip = egress_ip()
    log("出口 IP: %s" % ip)
    log("IP 归属: %s" % ip_info(ip))
    log("=" * 58)

    with SB(uc=True, xvfb=True, proxy=PROXY or None,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu") as sb:
        # 先从首页进（不受托管挑战保护），再跳登录页
        log("→ 打开首页 ...")
        sb.uc_open_with_reconnect(HOME_URL, reconnect_time=8)
        time.sleep(5)
        log("  标题: %s" % sb.get_title())

        log("→ 打开登录页 ...")
        try:
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
        except Exception as e:
            log("  打开异常: %s" % str(e)[:120])
        time.sleep(5)
        log("  标题: %s" % sb.get_title())
        log("  被CF中间页拦: %s" % is_block_page(sb))

        # ① 等 CF 中间页自己解开（最多 150 秒）
        t0 = time.time()
        cleared = False
        while time.time() - t0 < 150:
            if not is_block_page(sb):
                cleared = True
                break
            # 手动触发 UC 的过盾（对托管挑战也有效）
            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass
            time.sleep(10)
            log("   等待中 %ds  标题=%s" % (int(time.time() - t0), sb.get_title()))

        log("① CF 中间页: %s（耗时 %ds）"
            % ("✅ 已通过" if cleared else "❌ 未通过", int(time.time() - t0)))
        log("  当前 URL: %s" % sb.get_current_url())
        log("  当前标题: %s" % sb.get_title())

        if not cleared:
            try:
                sb.save_screenshot("nodetest_%s_cfstuck.png" % LABEL)
            except Exception:
                pass
            log("=" * 58)
            log("结论[%s]: ❌ 连 CF 中间页都过不去 → 该出口不可用" % LABEL)
            log("=" * 58)
            sys.exit(1)

        # ② 页面上的 Turnstile widget（过了才显示账号密码框）
        time.sleep(5)
        st = turnstile_state(sb)
        wb = widget_box(sb)
        log("② widget 状态: %s" % st)
        log("   cf frames: %s" % challenge_frames(sb))
        log("   widget 框: %s" % wb)

        def pwd_visible(_=None):
            try:
                return sb.execute_script(
                    "const p=document.querySelector('input[type=password]');"
                    "if(!p) return false;"
                    "const r=p.getBoundingClientRect();"
                    "return r.width>0 && r.height>0;")
            except Exception:
                return False

        log("   密码框可见: %s" % pwd_visible())

        ok = False
        if st.get("widget"):
            log("🛡️ 开始过 Turnstile ...")
            ok = solve_turnstile(sb, timeout=90, require_positive=True,
                                 shot="nodetest_%s_widget_fail.png" % LABEL)
        elif pwd_visible():
            log("ℹ️ 无 widget 但密码框已显示 → 视为已过")
            ok = True

        tok = ""
        try:
            tok = sb.execute_script(
                "const f=document.querySelector('[name=\"cf-turnstile-response\"]');"
                "return f?(f.value||''):'';") or ""
        except Exception:
            pass
        pv = pwd_visible()
        log("   token 长度: %d" % len(tok))
        log("   密码框可见: %s" % pv)
        try:
            sb.save_screenshot("nodetest_%s_final.png" % LABEL)
        except Exception:
            pass

        passed = bool((ok and len(tok) > 20) or pv)
        log("=" * 58)
        log("结论[%s]: %s（中间页✅ / widget=%s / token=%d / 密码框=%s）"
            % (LABEL, "✅ 该出口可过验证" if passed else "❌ 该出口过不去",
               st.get("widget"), len(tok), pv))
        log("=" * 58)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
