#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto renew on https://gpanel.eternalzero.cloud
- Login strategy: AUTO (default) = try COOKIE first, fallback to EMAIL if cookie fails.
- Opens target server (default "null's Test Server") and clicks "ADD 5H" if available.
- Headless friendly for GitHub Actions. Saves screenshots for debugging.

Env vars:
  FG_LOGIN_METHOD    : "AUTO" (default), "COOKIE" or "EMAIL"
  FG_COOKIE          : Cookie header string, e.g. "cf_clearance=...; pterodactyl_session=..." (COOKIE mode)
  FG_COOKIE_DOMAIN   : Cookie domain (default: gpanel.eternalzero.cloud) [通常无需设置，代码用 url 注入]
  FG_EMAIL           : Email/username for login (EMAIL mode or fallback)
  FG_PASSWORD        : Password for login (EMAIL mode or fallback)
  FG_SERVER_KEYWORD  : Keyword for server card/title (default: "null's Test Server")
  FG_BASE_URL        : Base URL (default: https://gpanel.eternalzero.cloud)
  HEADLESS           : "true"/"false" (default: true)
  TIMEOUT_MS         : Default timeout (ms, default: 30000)

Outputs:
  - screenshots/*.png for debugging
"""

import os
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def log(msg: str):
    now = datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] {msg}", flush=True)


def ensure_dir(p: str):
    Path(p).mkdir(parents=True, exist_ok=True)


def parse_cookie_pairs(cookie_string: str):
    """
    Parse a raw Cookie header value into (name, value) pairs.
    - Accepts input like "Cookie: a=1; b=2" or "a=1; b=2"
    - Skips attributes like path/domain/expires/httponly/secure/samesite
    """
    s = (cookie_string or "").strip()
    if not s:
        return []
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    skip = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly"}
    pairs = []
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        n = name.strip()
        if not n or n.lower() in skip:
            continue
        pairs.append((n, value.strip()))
    return pairs


def first_visible_locator(page, selectors, timeout=1000):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def wait_text(page, pattern: str, timeout=5000):
    try:
        page.locator(
            'xpath=//*[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "{}")]'
            .format(pattern.upper())
        ).first.wait_for(state="visible", timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False


def login_with_cookie(context, page, base_url: str, cookie_string: str, default_timeout: int):
    pairs = parse_cookie_pairs(cookie_string)
    if not pairs:
        raise RuntimeError("FG_COOKIE parsed 0 valid pairs. Provide 'name=value; name2=value2' (no Path/Domain/Expires).")

    # url 注入，避免域名/属性不匹配导致 Invalid cookie fields
    cookies = [{"name": n, "value": v, "url": base_url} for n, v in pairs]
    context.add_cookies(cookies)

    page.goto(base_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    if any(k in page.url for k in ["/auth/login", "/login"]):
        raise RuntimeError("Cookie login failed (redirected to login).")
    if first_visible_locator(page, ['input[type="password"]', 'input[name="password"]'], timeout=1000):
        raise RuntimeError("Cookie login failed (password field visible).")

    log("Cookie login succeeded.")
    return True


def navigate_to_login(page, base_url: str, default_timeout: int):
    candidates = [
        base_url.rstrip("/") + "/auth/login",
        base_url.rstrip("/") + "/login",
        base_url,
    ]
    for url in candidates:
        log(f"Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=default_timeout)
        except PlaywrightTimeoutError:
            pass
        if first_visible_locator(page, [
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[id*="email" i]',
            'input[id*="username" i]',
            'input[placeholder*="mail" i]',
            'input[placeholder*="用户名"]',
            'input[placeholder*="邮箱"]',
        ], timeout=1500):
            return True
    return False


def login_with_email(page, base_url: str, email: str, password: str, default_timeout: int):
    if not email or not password:
        raise RuntimeError("FG_EMAIL or FG_PASSWORD is empty for EMAIL login")

    if not navigate_to_login(page, base_url, default_timeout):
        page.screenshot(path="screenshots/no_login_form.png", full_page=True)
        raise RuntimeError("Cannot find login form at known paths.")

    user_loc = first_visible_locator(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[id*="email" i]',
        'input[id*="username" i]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="用户名"]',
        'input[placeholder*="邮箱"]',
    ], timeout=5000)
    if not user_loc:
        raise RuntimeError("Email/Username input not found on login page.")
    user_loc.fill(email)

    pwd_loc = first_visible_locator(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[id*="password" i]',
        'input[placeholder*="密码"]',
    ], timeout=5000)
    if not pwd_loc:
        raise RuntimeError("Password input not found on login page.")
    pwd_loc.fill(password)

    submit = first_visible_locator(page, [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("登录")',
        'button:has-text("登入")',
    ], timeout=2000)
    if submit:
        submit.click()
    else:
        pwd_loc.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    if any(k in page.url for k in ["/auth/login", "/login"]):
        raise RuntimeError("Email login failed or blocked (still on login).")
    log("Email login succeeded.")
    return True


def attempt_login(context, page, base_url: str, login_method: str,
                  cookie_string: str, email: str, password: str, default_timeout: int):
    method = (login_method or "AUTO").strip().upper()
    has_cookie = bool((cookie_string or "").strip())
    has_email = bool(email) and bool(password)

    log(f"Login method: {method} (has_cookie={has_cookie}, has_email={has_email})")

    last_err = None
    if method == "EMAIL":
        if not has_email:
            raise RuntimeError("EMAIL login selected but FG_EMAIL/FG_PASSWORD not provided.")
        return login_with_email(page, base_url, email, password, default_timeout)

    if method in ("COOKIE", "AUTO"):
        if has_cookie:
            try:
                return login_with_cookie(context, page, base_url, cookie_string, default_timeout)
            except Exception as e:
                last_err = e
                log(f"Cookie login failed: {e}")

        if has_email:
            log("Falling back to EMAIL login...")
            return login_with_email(page, base_url, email, password, default_timeout)

        if method == "COOKIE":
            raise RuntimeError(f"COOKIE login failed and no EMAIL credentials provided. Error: {last_err}")

    raise RuntimeError("No valid credentials provided (neither FG_COOKIE nor FG_EMAIL/FG_PASSWORD).")


def is_on_server_detail(page):
    return any([
        wait_text(page, "Renew Server", timeout=800),
        wait_text(page, "Expires:", timeout=800),
        wait_text(page, "ADD 5H", timeout=800),
    ])


def go_to_server_detail(page, server_keyword: str, default_timeout: int, screenshots_dir: str):
    if is_on_server_detail(page):
        log("Already on server detail page.")
        return

    clicked = False
    if server_keyword:
        log(f"Trying to open server by keyword: {server_keyword}")
        candidates = [
            page.get_by_role("link", name=re.compile(server_keyword, re.I)).first,
            page.get_by_role("button", name=re.compile(server_keyword, re.I)).first,
            page.locator(f"a:has-text('{server_keyword}')").first,
            page.locator(f"text={server_keyword}").first,
            page.locator(
                'xpath=//a[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "{}")]'
                .format(server_keyword.upper())
            ).first,
        ]
        for cand in candidates:
            try:
                cand.wait_for(state="visible", timeout=2000)
                cand.click()
                clicked = True
                break
            except Exception:
                continue
    if not clicked:
        log("No FG_SERVER_KEYWORD matched, clicking the first likely server link.")
        loc = first_visible_locator(page, [
            'a[href*="/server"]',
            'a[href*="/servers"]',
            'a[href*="server"]',
            'a:has-text("Server")',
        ], timeout=2000)
        if loc:
            loc.click()
        else:
            loc2 = first_visible_locator(page, ['a', 'button'], timeout=2000)
            if loc2:
                loc2.click()

    try:
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    if not is_on_server_detail(page):
        page.screenshot(path=f"{screenshots_dir}/server_open_failed.png", full_page=True)
        raise RuntimeError("Failed to open server detail page (no 'Renew Server/Expires/ADD 5H').")

    log("Opened server detail page.")


def get_status_text(page):
    try:
        loc = page.locator(
            'xpath=//*[contains(translate(., "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "RENEW SERVER") '
            ' or contains(translate(., "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "EXPIRES:") '
            ' or contains(translate(., "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "RENEWALS:") '
            ' or contains(translate(., "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "COOLDOWN:")]'
        ).first
        loc.wait_for(state="visible", timeout=1500)
        txt = loc.evaluate("el => el.closest('section,div,li,dd,dt,article,aside')?.innerText || el.innerText")
        return re.sub(r"\s+", " ", (txt or "")).strip()
    except Exception:
        return ""


def click_add_5h(page, default_timeout: int, screenshots_dir: str):
    selectors = [
        'xpath=//*[self::button or self::a][contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ADD 5H")]',
        'xpath=//*[self::button or self::a][contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ADD 5 H")]',
    ]
    btn = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=2000)
            btn = loc
            break
        except Exception:
            continue

    before = get_status_text(page)
    if before:
        log(f"Before: {before}")

    if not btn:
        log("No visible 'ADD 5H' button found. Maybe on cooldown or not eligible.")
        page.screenshot(path=f"{screenshots_dir}/no_add_button.png", full_page=True)
        return False

    try:
        enabled = btn.is_enabled()
    except Exception:
        enabled = True

    if not enabled:
        log("'ADD 5H' button is disabled. Skipping.")
        page.screenshot(path=f"{screenshots_dir}/add_button_disabled.png", full_page=True)
        return False

    log("Clicking 'ADD 5H' ...")
    btn.click()

    try:
        page.wait_for_timeout(2500)
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    after = get_status_text(page)
    if after:
        log(f"After:  {after}")

    page.screenshot(path=f"{screenshots_dir}/after_click.png", full_page=True)
    log("Click done, screenshot saved.")
    return True


def main():
    base_url = os.getenv("FG_BASE_URL", "https://gpanel.eternalzero.cloud").rstrip("/")
    login_method = (os.getenv("FG_LOGIN_METHOD") or "AUTO").strip().upper()
    cookie_string = os.getenv("FG_COOKIE", "")
    _cookie_domain = os.getenv("FG_COOKIE_DOMAIN", "gpanel.eternalzero.cloud")
    email = os.getenv("FG_EMAIL", "")
    password = os.getenv("FG_PASSWORD", "")
    server_keyword = os.getenv("FG_SERVER_KEYWORD", "null's Test Server").strip()
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    default_timeout = int(os.getenv("TIMEOUT_MS", "30000"))

    screenshots_dir = "screenshots"
    ensure_dir(screenshots_dir)

    log(f"Starting. Method={login_method}, Headless={headless}, Base={base_url}, Keyword={server_keyword or '(first server)'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
        context.set_default_timeout(default_timeout)
        page = context.new_page()

        try:
            attempt_login(context, page, base_url, login_method, cookie_string, email, password, default_timeout)

            try:
                page.wait_for_load_state("networkidle", timeout=default_timeout)
            except PlaywrightTimeoutError:
                pass

            go_to_server_detail(page, server_keyword, default_timeout, "screenshots")
            success = click_add_5h(page, default_timeout, "screenshots")

            if success:
                log("Success: attempted to add 5 hours.")
            else:
                log("No action performed: button not available/disabled.")

            page.screenshot(path=f"screenshots/final.png", full_page=True)
        except Exception as e:
            log(f"ERROR: {e}")
            try:
                page.screenshot(path=f"screenshots/error.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
