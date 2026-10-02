#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点 / 出口 IP 过 Turnstile 能力测试（只读）

背景：续期表单的 Turnstile 只在「到期前 1 天」的窗口里才渲染，平时测不到。
但登录页 /auth/login 有**常驻** Turnstile（过了才显示账号密码框），
因此可以随时验证「这个出口 IP 能不能过 Turnstile」。

本脚本不登录、不提交任何表单，只观察 widget 是否出现、能否出 token。
"""
import os
import sys
import time
import urllib.request

from seleniumbase import SB

from cf_bypass import (solve_turnstile, turnstile_state, challenge_frames,
                       is_block_page, widget_box)

LOGIN_URL = "https://dash.hidencloud.com/auth/login"
PROXY = os.environ.get("PROXY_URL", "").strip()
TARGET = os.environ.get("TEST_URL", LOGIN_URL)
LABEL = os.environ.get("TEST_LABEL", "test")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def egress_ip():
    """出口 IP（走代理时用它确认真的是节点在出网）"""
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip",
              "https://ipinfo.io/ip"):
        try:
            r = urllib.request.urlopen(u, timeout=15)
            return r.read().decode().strip()
        except Exception:
            continue
    return "获取失败"


def main():
    log("=" * 56)
    log("测试: %s" % LABEL)
    log("代理: %s" % (PROXY or "直连"))
    log("目标: %s" % TARGET)
    log("出口 IP: %s" % egress_ip())
    log("=" * 56)

    with SB(uc=True, xvfb=True, proxy=PROXY or None,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu") as sb:
        sb.uc_open_with_reconnect(TARGET, reconnect_time=8)
        time.sleep(6)

        url = sb.get_current_url()
        log("URL: %s" % url)
        log("标题: %s" % sb.get_title())
        log("被CF拦: %s" % is_block_page(sb))

        # Turnstile widget 是否存在
        st = turnstile_state(sb)
        log("widget: %s" % st)
        log("cf frames: %s" % challenge_frames(sb))
        wb = widget_box(sb)
        log("widget 框: %s" % wb)

        # 账号密码框是否已显示（= Turnstile 已过的旁证）
        def pwd_visible(_):
            try:
                return sb.execute_script(
                    "const p=document.querySelector('input[type=password]');"
                    "if(!p) return false;"
                    "const r=p.getBoundingClientRect();"
                    "return r.width>0 && r.height>0;")
            except Exception:
                return False

        log("密码框可见(过盾前): %s" % pwd_visible(sb))

        if not st.get("widget"):
            log("⚠️ 未检测到 Turnstile widget —— 该页当前形态测不出结论")
            try:
                sb.save_screenshot("nodetest_%s_nowidget.png" % LABEL)
            except Exception:
                pass
            sys.exit(2)

        log("🛡️ 开始过盾 ...")
        ok = solve_turnstile(sb, timeout=90, require_positive=True,
                             shot="nodetest_%s_fail.png" % LABEL)

        tok = ""
        try:
            tok = sb.execute_script(
                "const f=document.querySelector('[name=\"cf-turnstile-response\"]');"
                "return f?(f.value||''):'';") or ""
        except Exception:
            pass

        pv = pwd_visible(sb)
        log("token 长度: %d" % len(tok))
        log("密码框可见(过盾后): %s" % pv)
        try:
            sb.save_screenshot("nodetest_%s_final.png" % LABEL)
        except Exception:
            pass

        passed = bool(ok and len(tok) > 20)
        log("=" * 56)
        log("结论[%s]: %s" % (LABEL, "✅ 过盾成功" if passed else "❌ 过盾失败"))
        log("=" * 56)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
