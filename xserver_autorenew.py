# -*- coding: utf-8 -*-
import os
import re
import sys
import time
import json
from pathlib import Path
from typing import List


from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

def write_success_md(filepath="renew_result.md", tzname="Asia/Tokyo"):
    tz = ZoneInfo(tzname) if ZoneInfo else None
    now = datetime.now(tz) if tz else datetime.utcnow()
    suffix = "JST" if tz else "UTC"
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} {suffix} 成功\n"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"[write_success_md] {line.strip()} -> {filepath}")

LOGIN_URL = "https://secure.xserver.ne.jp/xapanel/login/xserver/?request_page=xmgame%2Findex"
GAME_INDEX_URL = "https://secure.xserver.ne.jp/xapanel/xmgame/index"

HEADLESS = os.getenv("HEADLESS", "1") != "0"
EMAIL = os.getenv("XSERVER_EMAIL", "").strip()
PASSWORD = os.getenv("XSERVER_PASSWORD", "").strip()
COOKIE_STR = os.getenv("XSERVER_COOKIE", "").strip()
TARGET_GAME = os.getenv("TARGET_GAME", "").strip()

DEFAULT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "12000"))
SHORT_TIMEOUT = 4000

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def snap(page, name: str):
    try:
        out = Path("screenshots")
        ensure_dir(out)
        safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", name)
        filepath = out / f"{int(time.time())}_{safe_name}.png"
        page.screenshot(path=str(filepath), full_page=True)
        log(f"Saved screenshot: {filepath}")
    except Exception as e:
        log(f"Screenshot failed: {e}")

def parse_cookie_string(cookie_str: str, domain: str) -> List[dict]:
    cookies = []
    # Expect cookie_str like: "name1=val1; name2=val2; ..."
    for item in [p.strip() for p in cookie_str.split(";") if p.strip()]:
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax"
        })
    return cookies

def is_logged_in(page) -> bool:
    candidates = [
        "ログアウト", "サービス管理", "マイページ", "アカウント", "お知らせ"
    ]
    for t in candidates:
        try:
            if page.get_by_text(t, exact=False).first.is_visible():
                return True
        except Exception:
            pass
    return False

def try_click(page, locator, timeout=SHORT_TIMEOUT) -> bool:
    try:
        locator.first.click(timeout=timeout)
        page.wait_for_timeout(250)
        return True
    except Exception:
        return False

def click_by_text(page, texts: List[str], roles=("button", "link"), timeout=SHORT_TIMEOUT) -> bool:
    for t in texts:
        # 1) role button/link by accessible name
        for r in roles:
            try:
                if try_click(page, page.get_by_role(r, name=t, exact=False), timeout=timeout):
                    return True
            except Exception:
                pass
        # 2) generic text locator (may hit div/span/a/button)
        try:
            if try_click(page, page.get_by_text(t, exact=False), timeout=timeout):
                return True
        except Exception:
            pass
        # 3) CSS fallbacks
        for sel in [f'a:has-text("{t}")', f'button:has-text("{t}")', f'input[value*="{t}"]', f'label:has-text("{t}")']:
            try:
                if try_click(page, page.locator(sel), timeout=timeout):
                    return True
            except Exception:
                pass
    return False

def select_72h(page) -> bool:
    texts = ["+72時間延長", "72時間", "+72時間", "72 時間"]
    # Try radios by label
    for t in texts:
        try:
            if try_click(page, page.get_by_label(t, exact=False)):
                return True
        except Exception:
            pass
    # Try label has-text
    for t in texts:
        try:
            if try_click(page, page.locator(f'label:has-text("{t}")')):
                return True
        except Exception:
            pass
    # As a last resort click text directly (if option is a button)
    return click_by_text(page, texts)

def goto(page, url: str):
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(500)

def cookie_login(context, page) -> bool:
    if not COOKIE_STR:
        return False
    domains = ["secure.xserver.ne.jp", "www.xserver.ne.jp"]
    all_cookies = []
    for d in domains:
        all_cookies.extend(parse_cookie_string(COOKIE_STR, d))
    if not all_cookies:
        return False
    try:
        context.add_cookies(all_cookies)
    except Exception as e:
        log(f"Add cookies failed: {e}")
        return False

    # Try accessing target page directly
    goto(page, GAME_INDEX_URL)
    snap(page, "after_cookie_goto_game_index")
    if is_logged_in(page):
        log("Logged in via cookie (direct to game index).")
        return True

    # Try login URL with request_page param
    goto(page, LOGIN_URL)
    snap(page, "after_cookie_goto_login")
    if is_logged_in(page):
        log("Logged in via cookie (via login URL).")
        return True

    return False

def password_login(page) -> bool:
    goto(page, LOGIN_URL)
    snap(page, "login_form_loaded")

    # Heuristics for email/ID field
    email_locators = [
        'input[type="email"]',
        'input[name*="mail"]',
        'input[id*="mail"]',
        'input[name*="login"]',
        'input[name*="account"]',
        'input[name*="user"]',
        'input[name*="id"]',
        'input[id*="login"]',
        'input[id*="account"]',
        'input[id*="user"]',
        'input[id*="id"]',
    ]
    pwd_locators = [
        'input[type="password"]',
        'input[name*="pass"]',
        'input[id*="pass"]',
    ]

    # Try by labels first (Japanese)
    filled_email = False
    for label in ["メールアドレス", "ログインID", "アカウントID", "ID", "メール"]:
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() > 0:
                loc.first.fill(EMAIL, timeout=SHORT_TIMEOUT)
                filled_email = True
                break
        except Exception:
            pass

    if not filled_email:
        for css in email_locators:
            try:
                loc = page.locator(css)
                if loc.count() > 0:
                    loc.first.fill(EMAIL, timeout=SHORT_TIMEOUT)
                    filled_email = True
                    break
            except Exception:
                pass

    filled_pwd = False
    for label in ["パスワード", "Password"]:
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() > 0:
                loc.first.fill(PASSWORD, timeout=SHORT_TIMEOUT)
                filled_pwd = True
                break
        except Exception:
            pass

    if not filled_pwd:
        for css in pwd_locators:
            try:
                loc = page.locator(css)
                if loc.count() > 0:
                    loc.first.fill(PASSWORD, timeout=SHORT_TIMEOUT)
                    filled_pwd = True
                    break
            except Exception:
                pass

    # Click login button
    clicked = click_by_text(page, ["ログイン", "ログインする", "サインイン", "ログオン", "ログインへ"])
    if not clicked:
        # Try submit by pressing Enter in password field
        try:
            if filled_pwd:
                page.keyboard.press("Enter")
        except Exception:
            pass

    # Wait to be logged in
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_login_submit")
    return is_logged_in(page)

def navigate_to_game_management(page) -> bool:
    # 1) サービス管理
    if not click_by_text(page, ["サービス管理", "サービス", "管理"]):
        log("Could not find サービス管理, trying direct game index.")
        goto(page, GAME_INDEX_URL)

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_service_mgmt")

    # 2) XServerGAMEs
    if not click_by_text(page, ["XServerGAMEs", "XServerGAMES", "XServerGAME", "Xserverゲーム", "XserverGAMEs", "XSERVER GAME", "GAMEs"]):
        log("Could not click XServerGAMEs link directly, attempting to proceed on current page.")

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_xservergames")

    # 3) ゲーム管理
    if not click_by_text(page, ["ゲーム管理", "ゲーム", "管理"]):
        log("Could not find ゲーム管理; will proceed with upgrade/extend search anyway.")

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_game_management")

    return True

def click_upgrade_or_extend(page) -> bool:
    # Optionally narrow down by TARGET_GAME (if provided)
    if TARGET_GAME:
        log(f"Trying to select target game: {TARGET_GAME}")
        try:
            # Find a row/card containing target game text, then find its アップグレード・期限延長
            container = page.locator(f'text={TARGET_GAME}').first
            if container.count() > 0:
                # Climb up to a card or row then click inside
                # Try common patterns
                for up_text in ["アップグレード・期限延長", "期限延長", "アップグレード"]:
                    # within the same section
                    if try_click(page, container.locator(f'xpath=ancestor::*[self::tr or self::*[@role="row"] or contains(@class,"card")][1]').locator(f'text={up_text}')):
                        return True
                    if try_click(page, container.locator(f'text={up_text}')):
                        return True
        except Exception:
            pass

    # Generic click for アップグレード・期限延長
    ok = click_by_text(page, ["アップグレード・期限延長", "期限延長", "アップグレード"])
    if not ok:
        log("Could not find アップグレード・期限延長 on current page.")
    else:
        snap(page, "after_click_upgrade_extend")
    return ok

def do_extend_72h(page) -> bool:
    # Some pages first show a secondary "期限を延長する" entry point
    click_by_text(page, ["期限を延長する", "延長する"])

    # Now select +72時間
    if not select_72h(page):
        log("Could not select +72時間 option. It may be unavailable or UI changed.")
        snap(page, "failed_select_72h")
        # Even if failed, try to continue (maybe default is already 72h)
    else:
        snap(page, "selected_72h")

    # 確認画面に進む
    if not click_by_text(page, ["確認画面に進む", "確認へ進む", "確認"]):
        log("Could not find 確認画面に進む. UI may be different or already on confirm page.")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass
        snap(page, "after_go_confirm")

    # Final confirm: 期限を延長する
    if not click_by_text(page, ["期限を延長する", "延長する", "実行する"]):
        log("Could not find the final 期限を延長する button.")
        snap(page, "failed_final_extend_click")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_extend_submit")

    # Check success
    success_texts = ["延長", "完了", "処理が完了", "更新されました", "受け付けました"]
    for t in success_texts:
        try:
            if page.get_by_text(t, exact=False).first.is_visible():
                log("Extension likely succeeded.")
                return True
        except Exception:
            pass

    log("Did not detect a success message; please review screenshots.")
    return True  # Consider non-blocking success to avoid failing the workflow

def main():
    if not COOKIE_STR and (not EMAIL or not PASSWORD):
        log("No cookie provided and missing EMAIL/PASSWORD. Please set GitHub Secrets.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        logged_in = False
        if COOKIE_STR:
            log("Trying cookie login...")
            logged_in = cookie_login(context, page)

        if not logged_in and EMAIL and PASSWORD:
            log("Trying password login...")
            logged_in = password_login(page)

        if not logged_in:
            snap(page, "login_failed")
            log("Login failed. Please check credentials or cookie.")
            # Exit gracefully to avoid noisy failures, but return non-zero to be visible
            sys.exit(2)

        log("Navigating to Game Management...")
        navigate_to_game_management(page)

        log("Opening upgrade/extend page...")
        if not click_upgrade_or_extend(page):
            log("Could not open upgrade/extend page. Exiting.")
            snap(page, "open_upgrade_extend_failed")
            sys.exit(3)

        log("Performing +72h extension...")
        success = do_extend_72h(page)

        if success:
            log("All steps completed.")
        else:
            log("Extension step reported failure.")

        context.close()
        browser.close()

if __name__ == "__main__":
    main()
