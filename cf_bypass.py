# -*- coding: utf-8 -*-
"""Cloudflare Turnstile 过盾工具（SeleniumBase 版，可跨仓库复用）

来源与实测要点
──────────────
参考 eooce/Auto-Renew-HidenCloud app.py L111-190 的实测结论，加上本机对
dash.hidencloud.com 的验证：

1. **挑战 iframe 在闭包 shadow DOM 里**
   `document.querySelectorAll('iframe')` 和 Selenium 的 `find_elements` 都找不到，
   返回 `iframe: false`。但 CDP 的 `Page.getFrameTree` 能看到（浏览器层 frame 树），
   再用 `DOM.getFrameOwner` + `DOM.getBoxModel` 取坐标。

2. **token 输入框在 light DOM**
   `input[name="cf-turnstile-response"]` 可直接 querySelector 到，
   容器约 300x65 —— 可用作兜底定位。

3. **点击要走 CDP 底层事件**
   `Input.dispatchMouseEvent` 产生的 `isTrusted=true`，
   并带拟人多步移动轨迹（起点偏移、随机步数、微小抖动）。

4. **display:none 下挑战不发起**
   站点自己的注释写明：widget 在隐藏容器里时 challenge 不跑，
   `getBoundingClientRect()` 返回 `0x0x0x0`、token 输入框恒空。
   → 用 `widget_box()` 判断容器是否可见，不可见时先想办法显示它。

用法
────
    from cf_bypass import solve_turnstile, turnstile_state, challenge_frames

    with SB(uc=True, xvfb=True) as sb:
        sb.uc_open_with_reconnect(url, reconnect_time=8)
        ok = solve_turnstile(sb, timeout=90)
"""
import random
import time

# Turnstile token 输入框（light DOM，可兜底定位）
TOKEN_INPUT_SEL = ('input[name="cf-turnstile-response"], '
                   'textarea[name="cf-turnstile-response"]')
FRAME_URL_MARKER = "challenges.cloudflare.com"

# 检测 token 是否生成（solved 判定阈值 20 字符，避免空值/半截值误判）
STATE_JS = """
() => {
    try {
        let total = 0, solved = 0;
        document.querySelectorAll(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
        ).forEach(n => {
            total += 1;
            if (n.value && n.value.length > 20) solved += 1;
        });
        const w = document.querySelector('.cf-turnstile');
        let rect = null;
        if (w) rect = w.getBoundingClientRect();
        return {total: total, solved: solved,
                widget: !!w,
                wrect: rect ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null};
    } catch (e) { return {total: 0, solved: 0, widget: false, wrect: null}; }
}
"""

_CDP = {}


def _log(m):
    print("[cf] %s" % m, flush=True)


def _session(sb):
    """CDP 会话与 driver 一一对应，缓存复用"""
    key = id(sb.driver)
    s = _CDP.get(key)
    if s is None:
        try:
            s = sb.driver.execute_cdp_cmd
            _CDP[key] = s
        except Exception as e:
            _log("创建 CDP 会话失败: %s" % e)
            return None
    return s


def _cdp(sb, method, **params):
    fn = _session(sb)
    if fn is None:
        return None
    try:
        return fn(method, params)
    except Exception as e:
        _log("CDP %s 失败: %s" % (method, str(e)[:120]))
        return None


def turnstile_state(sb):
    """返回 {total, solved, widget, wrect}"""
    try:
        st = sb.execute_script("return " + STATE_JS.strip()[5:])  # 去掉 () =>
    except Exception:
        st = None
    if not isinstance(st, dict):
        return {"total": 0, "solved": 0, "widget": False, "wrect": None}
    return st


def challenge_frames(sb):
    """列所有 URL 含 challenges.cloudflare.com 的 frameId（含 shadow DOM 内的）

    关键：Page.getFrameTree 走浏览器层，不受 shadow DOM 遮蔽。
    """
    tree = _cdp(sb, "Page.getFrameTree")
    if not tree:
        return []
    out = []

    def walk(node):
        f = node.get("frame", {})
        fid, url = f.get("id"), f.get("url", "")
        if FRAME_URL_MARKER in url:
            out.append(fid)
        for ch in node.get("childFrames", []) or []:
            walk(ch)

    walk(tree.get("frameTree", {}))
    return out


def frame_box(sb, frame_id):
    """取 frame 元素的位置（DOM.getFrameOwner → DOM.getBoxModel）"""
    owner = _cdp(sb, "DOM.getFrameOwner", frameId=frame_id)
    if not owner or "backendNodeId" not in owner:
        return None
    box = _cdp(sb, "DOM.getBoxModel", backendNodeId=owner["backendNodeId"])
    if not box:
        return None
    q = box.get("model", {}).get("content")   # [x1,y1,x2,y2,x3,y3,x4,y4]
    if not q or len(q) < 8:
        return None
    xs, ys = q[0::2], q[1::2]
    return {"x": min(xs), "y": min(ys),
            "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


def widget_box(sb):
    """取 token 输入框容器的位置（light DOM 兜底定位）"""
    try:
        return sb.execute_script("""
            const n = document.querySelector('input[name="cf-turnstile-response"],'
                      + 'textarea[name="cf-turnstile-response"]');
            if (!n) return null;
            let el = n, best = null;
            for (let i = 0; i < 4 && el; i++) {
                const r = el.getBoundingClientRect();
                if (r.width > 40 && r.height > 20) { best = r; break; }
                el = el.parentElement;
            }
            if (!best) { const r = n.getBoundingClientRect();
                         best = r.width > 0 ? r : null; }
            if (!best) return null;
            return {x: best.x, y: best.y, w: best.width, h: best.height};
        """)
    except Exception:
        return None


def cdp_click_at(sb, x, y):
    """CDP 底层鼠标点击（isTrusted=true），带拟人多步移动轨迹"""
    sx = x - random.uniform(50, 110)
    sy = y - random.uniform(35, 75)
    if _cdp(sb, "Input.dispatchMouseEvent", type="mouseMoved", x=sx, y=sy) is None:
        return False
    steps = random.randint(8, 14)
    for i in range(1, steps + 1):
        ix = sx + (x - sx) * i / steps + random.uniform(-1.5, 1.5)
        iy = sy + (y - sy) * i / steps + random.uniform(-1.5, 1.5)
        _cdp(sb, "Input.dispatchMouseEvent", type="mouseMoved", x=ix, y=iy)
        time.sleep(random.uniform(0.01, 0.035))
    time.sleep(random.uniform(0.10, 0.25))
    _cdp(sb, "Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
         button="left", buttons=1, clickCount=1)
    time.sleep(random.uniform(0.05, 0.12))
    _cdp(sb, "Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
         button="left", clickCount=1)
    return True


def is_block_page(sb):
    """判断当前是否被站点 CF 拦住（安全验证页）"""
    try:
        src = sb.get_page_source()[:3000]
        title = sb.get_title()
    except Exception:
        return False
    return ("Security Verification" in src
            or "Just a moment" in title
            or "Connection Blocked" in src)


def solve_turnstile(sb, timeout=90, success_check=None, reload_after=0,
                    require_positive=False, shot=None):
    """处理 Turnstile。返回 True=已通过（或 require_positive=False 时无 widget）

    timeout         最多等多少秒
    success_check   额外的成功判据（callable(sb) -> bool）
    reload_after    等待 N 秒仍无 token 时刷新页面重试
    require_positive True 时必须有 token 才算成功
    shot            超时截图路径
    """
    t0 = time.time()
    if reload_after:
        time.sleep(reload_after)

    clicked = set()
    tried_gui = False
    last_click = 0.0

    while time.time() - t0 < timeout:
        st = turnstile_state(sb)
        if st.get("solved", 0) > 0:
            _log("✅ Turnstile 通过（token 已生成 %s/%s）" % (st["solved"], st["total"]))
            return True
        if success_check is not None:
            try:
                if success_check(sb):
                    _log("✅ 自定义判据已满足")
                    return True
            except Exception:
                pass

        if not st.get("widget"):
            if not require_positive and time.time() - t0 > 8:
                _log("ℹ️ 页面无 Turnstile widget，跳过")
                return True
            time.sleep(1.5)
            continue

        # 容器不可见（display:none）→ 挑战不会发起，等它被显示出来
        wr = st.get("wrect") or {}
        if not wr or wr.get("w", 0) < 10 or wr.get("h", 0) < 10:
            time.sleep(2)
            continue

        # 每 12 秒尝试一次点击（避免高频无效点击）
        if time.time() - last_click < 12:
            time.sleep(1.5)
            continue
        last_click = time.time()

        # ① 优先：shadow DOM 里的挑战 iframe → CDP 取坐标点击
        done = False
        for fid in challenge_frames(sb):
            if fid in clicked:
                continue
            bx = frame_box(sb, fid)
            if not bx or bx["w"] < 10:
                continue
            # 复选框在 widget 左侧：x+30，垂直居中
            cx = bx["x"] + min(30, bx["w"] * 0.2)
            cy = bx["y"] + bx["h"] / 2
            _log("🖱️ CDP 点击挑战框 (%.0f, %.0f) size=%.0fx%.0f"
                 % (cx, cy, bx["w"], bx["h"]))
            if cdp_click_at(sb, cx, cy):
                clicked.add(fid)
                done = True
                break
            time.sleep(0.5)
        if done:
            continue

        # ② 兜底：light DOM 容器 → 点同样的相对位置
        wb = widget_box(sb)
        if wb and wb["w"] > 10:
            cx = wb["x"] + min(30, wb["w"] * 0.2)
            cy = wb["y"] + wb["h"] / 2
            _log("🖱️ CDP 点击 widget 容器 (%.0f, %.0f)" % (cx, cy))
            cdp_click_at(sb, cx, cy)
            continue

        # ③ 最后：SeleniumBase 自带的物理点击（pyautogui）
        if not tried_gui:
            tried_gui = True
            try:
                _log("🖱️ 尝试 uc_gui_click_captcha()")
                sb.uc_gui_click_captcha()
            except Exception as e:
                _log("uc_gui_click_captcha 跳过: %s" % str(e)[:100])

    _log("❌ Turnstile 在 %ds 内未通过" % timeout)
    if shot:
        try:
            sb.save_screenshot(shot)
            _log("已存截图 %s" % shot)
        except Exception:
            pass
    return False


def renew_click_modal(sb, btn_texts=("Renew",), modal_marker=None,
                      restricted_markers=("Renewal Restricted", "can only renew"),
                      tries=6):
    """点续期按钮弹 modal。

    返回 "OK" | "NOT_TIME" | "NO_MODAL" | "NO_BUTTON"
    NOT_TIME：站点规则不允许（未到期），不是故障 —— 别当失败处理。
    """
    js = """
    (texts) => {
        const bs = [...document.querySelectorAll('button, a')];
        const b = bs.find(x => texts.some(t =>
            (x.textContent || '').trim().toLowerCase().includes(t.toLowerCase())));
        if (!b) return 'NO_BUTTON';
        b.scrollIntoView({block: 'center'});
        b.click();
        return 'CLICKED';
    }
    """
    for i in range(tries):
        r = None
        try:
            r = sb.execute_script(js, list(btn_texts))
        except Exception:
            pass
        if r == "NO_BUTTON":
            return "NO_BUTTON"
        time.sleep(2)

        # 未到期弹窗
        try:
            body = sb.execute_script(
                "return document.body ? document.body.innerText : ''") or ""
        except Exception:
            body = ""
        if any(m.lower() in body.lower() for m in restricted_markers):
            _log("⚠️ 站点规则：未到续期时间（Renewal Restricted）")
            return "NOT_TIME"

        # modal 是否出现
        if modal_marker:
            try:
                if sb.execute_script(
                        "return document.querySelector(arguments[0]) !== null",
                        modal_marker):
                    return "OK"
            except Exception:
                pass
        # 或者 Turnstile widget 已可见
        st = turnstile_state(sb)
        wr = st.get("wrect") or {}
        if st.get("widget") and wr.get("w", 0) > 10 and wr.get("h", 0) > 10:
            return "OK"
        time.sleep(2)
    return "NO_MODAL"
