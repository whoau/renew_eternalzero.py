# auto_start_falix.py
# 说明：
# - 通过 FALIX_COOKIES（GitHub Secrets）传入 Cookie（"name=value; name2=value2"）
# - 默认服务器名：qing；默认控制台URL：https://client.falixnodes.net/server/console
# - 原理：用 Playwright 进入控制台，若看到 Start 按钮可用则点击；若 Stop 按钮可用则判定在线无需操作。

import os
import re
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def parse_cookies(cookie_string: str, domain: str):
    cookies = []
    if not cookie_string:
        return cookies
    for part in cookie_string.split(";"):
        kv = part.strip()
        if not kv or "=" not in kv:
            continue
        name, value = kv.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value,
            "domain": domain,
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        })
    return cookies

def locator_by_names(page, names):
    # 优先通过 aria role 查找按钮；备用用 :has-text
    for name in names:
        try:
            loc = page.get_by_role("button", name=re.compile(name, re.I))
            if loc.count():
                for i in range(min(loc.count(), 3)):
                    btn = loc.nth(i)
                    if btn.is_visible():
                        return btn
        except Exception:
            pass
    for name in names:
        try:
            loc = page.locator(f'button:has-text("{name}")')
            if loc.count():
                for i in range(min(loc.count(), 3)):
                    btn = loc.nth(i)
                    if btn.is_visible():
                        return btn
        except Exception:
            pass
    # 常见 data-action 兜底
    mapping = {"start": ["Start", "启动", "开始"], "stop": ["Stop", "停止"]}
    for action, keys in mapping.items():
        if any(re.search(k, " ".join(names), re.I) for k in keys):
            loc = page.locator(f'[data-action="{action}"]')
            if loc.count():
                return loc.first
    return None

def wait_for_power_controls(page, timeout=30000):
    t0 = time.time()
    while time.time() - t0 < timeout / 1000.0:
        start_btn = locator_by_names(page, ["Start", "启动", "开始"])
        stop_btn = locator_by_names(page, ["Stop", "停止"])
        if (start_btn and start_btn.is_visible()) or (stop_btn and stop_btn.is_visible()):
            return start_btn, stop_btn
        time.sleep(0.3)
    return None, None

def main():
    SERVER_NAME = os.environ.get("SERVER_NAME", "qing")
    CONSOLE_URL = os.environ.get("CONSOLE_URL", "https://client.falixnodes.net/server/console")
    HOME_URL = os.environ.get("HOME_URL", "https://client.falixnodes.net")
    COOKIE_STR = os.environ.get("FALIX_COOKIES", "").strip()

    if not COOKIE_STR:
        log("环境变量 FALIX_COOKIES 为空，请在 GitHub Secrets 中设置。")
        return 2

    cookies = parse_cookies(COOKIE_STR, "client.falixnodes.net")
    if not cookies:
        log("无法解析 FALIX_COOKIES。请使用 'name=value; name2=value2' 格式。")
        return 3

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.add_cookies(cookies)
        page = context.new_page()

        # 进入主页
        try:
            log("打开主页...")
            page.goto(HOME_URL, wait_until="load", timeout=60000)
        except PWTimeout:
            log("主页加载超时，继续尝试。")

        # 粗略判断是否未登录
        try:
            login_links = [
                page.get_by_role("link", name=re.compile("登录|登錄|Log ?in|Sign ?in", re.I)),
                page.get_by_role("button", name=re.compile("登录|登錄|Log ?in|Sign ?in", re.I)),
            ]
            for loc in login_links:
                if loc and loc.count() and loc.first.is_visible():
                    log("疑似未登录，Cookie 可能已失效。")
                    return 4
        except Exception:
            pass

        # 打开控制台页
        try:
            log("打开控制台页面...")
            page.goto(CONSOLE_URL, wait_until="load", timeout=60000)
        except PWTimeout:
            log("控制台加载超时，继续检测。")

        page.wait_for_timeout(2000)

        # 尝试确认服务器名（可选）
        try:
            name_locator = page.get_by_text(SERVER_NAME, exact=False)
            if name_locator.count():
                log(f"检测到服务器名称包含: {SERVER_NAME}")
            else:
                log(f"未在控制台页找到服务器名关键字: {SERVER_NAME}（忽略继续）")
        except Exception:
            pass

        start_btn, stop_btn = wait_for_power_controls(page, timeout=45000)

        if stop_btn and stop_btn.is_enabled():
            log("当前状态：在线（检测到 Stop 按钮）。无需启动。")
            return 0

        if start_btn:
            if start_btn.is_enabled():
                log("当前状态：离线（检测到 Start 按钮）。准备启动...")
                try:
                    start_btn.click()
                    # 等待在线确认
                    t0 = time.time()
                    ok = False
                    while time.time() - t0 < 60:
                        sbtn = locator_by_names(page, ["Stop", "停止"])
                        if sbtn and sbtn.is_visible() and sbtn.is_enabled():
                            ok = True
                            break
                        try:
                            stat = page.get_by_text(re.compile("Online|Running|启动中|正在运行|已启动", re.I))
                            if stat.count() and stat.first.is_visible():
                                ok = True
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                    if ok:
                        log("启动成功或已在运行中。")
                        return 0
                    else:
                        log("已点击启动，但 60s 内未确认在线，可能仍在启动或被限流。")
                        return 0
                except Exception as e:
                    log(f"点击 Start 失败：{e}")
                    return 5
            else:
                log("检测到 Start 按钮但不可用，可能正在启动或页面未就绪。")
                return 0

        log("未找到 Start/Stop 控件。可能页面结构变更或未成功登录。")
        return 6

if __name__ == "__main__":
    sys.exit(main())
