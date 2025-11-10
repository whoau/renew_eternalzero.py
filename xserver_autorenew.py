# -*- coding: utf-8 -*-
import os
import re
import sys
import time
from pathlib import Path
from typing import List
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

from playwright.sync_api import sync_playwright

# ------------------ Config via ENV ------------------
LOGIN_URL = "https://secure.xserver.ne.jp/xapanel/login/xserver/?request_page=xmgame%2Findex"
GAME_INDEX_URL = "https://secure.xserver.ne.jp/xapanel/xmgame/index"

HEADLESS = os.getenv("HEADLESS", "1") != "0"
EMAIL = os.getenv("XSERVER_EMAIL", "").strip()
PASSWORD = os.getenv("XSERVER_PASSWORD", "").strip()
COOKIE_STR = os.getenv("XSERVER_COOKIE", "").strip()
TARGET_GAME = os.getenv("TARGET_GAME", "").strip()

# 续期“选择的时长”，不是运行间隔（默认 72h，可被 RENEW_HOURS 覆盖）
RENEW_HOURS = int(os.getenv("RENEW_HOURS", "72"))

# 续期间隔限流（默认 60h，不到期就跳过；FORCE_RENEW=1 可强制执行）
RENEW_INTERVAL_HOURS = int(os.getenv("RENEW_INTERVAL_HOURS", "60"))
FORCE_RENEW = os.getenv("FORCE_RENEW", "0") == "1"

# 写入日志 .md 的文件名与时区
RENEW_LOG_MD = os.getenv("RENEW_LOG_MD", "renew_result.md")
LOG_TIMEZONE = os.getenv("LOG_TIMEZONE", "Asia/Tokyo")

DEFAULT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "15000"))
SHORT_TIMEOUT = 4000

# ------------------ Utilities ------------------
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
    candidates = ["ログアウト", "サービス管理", "マイページ", "アカウント", "お知らせ"]
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
        for r in roles:
            try:
                if try_click(page, page.get_by_role(r, name=t, exact=False), timeout=timeout):
                    return True
            except Exception:
                pass
        try:
            if try_click(page, page.get_by_text(t, exact=False), timeout=timeout):
                return True
        except Exception:
            pass
        for sel in [f'a:has-text("{t}")', f'button:has-text("{t}")', f'input[value*="{t}"]', f'label:has-text("{t}")']:
            try:
                if try_click(page, page.locator(sel), timeout=timeout):
                    return True
            except Exception:
                pass
    return False

def goto(page, url: str):
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(500)

# ------------------ Interval Gate (60h default) ------------------
def _parse_ts_from_line(line: str):
    m = re.search(r'(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})\s*(JST|UTC|Z)?', line)
    if not m:
        return None
    ts, tzlabel = m.group(1), (m.group(2) or "JST")
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            break
        except Exception:
            dt = None
    if dt is None:
        return None
    if tzlabel in ("UTC", "Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
        dt = dt.replace(tzinfo=jst)
    return dt.astimezone(timezone.utc)

def get_last_success_utc(filepath=RENEW_LOG_MD):
    p = Path(filepath)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if "成功" in line:
            dt = _parse_ts_from_line(line)
            if dt:
                return dt
    return None

def should_run_interval(filepath=RENEW_LOG_MD, interval_hours=RENEW_INTERVAL_HOURS):
    last = get_last_success_utc(filepath)
    if last is None:
        return True, None
    now = datetime.now(timezone.utc)
    due = last + timedelta(hours=interval_hours)
    return now >= due, due

# ------------------ Logging to .md ------------------
def write_success_md(filepath=RENEW_LOG_MD, tzname=LOG_TIMEZONE):
    tz = None
    try:
        tz = ZoneInfo(tzname) if ZoneInfo else None
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.utcnow()
    suffix = tzname if tz else "UTC"
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} {suffix} 成功\n"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)
    log(f"[write_success_md] {line.strip()} -> {filepath}")

# ------------------ Auth ------------------
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

    goto(page, GAME_INDEX_URL)
    snap(page, "after_cookie_goto_game_index")
    if is_logged_in(page):
        log("Logged in via cookie (game index).")
        return True

    goto(page, LOGIN_URL)
    snap(page, "after_cookie_goto_login")
    if is_logged_in(page):
        log("Logged in via cookie (login URL).")
        return True

    return False

def password_login(page) -> bool:
    if not EMAIL or not PASSWORD:
        return False
    goto(page, LOGIN_URL)
    snap(page, "login_form_loaded")

    # Fill email/ID
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
        for css in [
            'input[type="email"]','input[name*="mail"]','input[id*="mail"]',
            'input[name*="login"]','input[name*="account"]','input[name*="user"]','input[name*="id"]',
            'input[id*="login"]','input[id*="account"]','input[id*="user"]','input[id*="id"]',
        ]:
            try:
                loc = page.locator(css)
                if loc.count() > 0:
                    loc.first.fill(EMAIL, timeout=SHORT_TIMEOUT)
                    filled_email = True
                    break
            except Exception:
                pass

    # Fill password
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
        for css in ['input[type="password"]','input[name*="pass"]','input[id*="pass"]']:
            try:
                loc = page.locator(css)
                if loc.count() > 0:
                    loc.first.fill(PASSWORD, timeout=SHORT_TIMEOUT)
                    filled_pwd = True
                    break
            except Exception:
                pass

    # Submit
    clicked = click_by_text(page, ["ログイン", "ログインする", "サインイン", "ログオン", "ログインへ"])
    if not clicked and filled_pwd:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_login_submit")
    return is_logged_in(page)

# ------------------ Navigation & Action ------------------
def navigate_to_game_management(page) -> bool:
    # サービス管理
    if not click_by_text(page, ["サービス管理", "サービス", "管理"]):
        log("Could not find サービス管理, going directly to game index.")
        goto(page, GAME_INDEX_URL)
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_service_mgmt")

    # XServerGAMEs
    click_by_text(page, ["XServerGAMEs", "XServerGAMES", "XServerGAME", "Xserverゲーム", "XserverGAMEs", "XSERVER GAME", "GAMEs"])
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_xservergames")

    # ゲーム管理
    click_by_text(page, ["ゲーム管理", "ゲーム", "管理"])
    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_game_management")
    return True

def click_upgrade_or_extend(page) -> bool:
    if TARGET_GAME:
        log(f"Trying to select target game: {TARGET_GAME}")
        try:
            container = page.locator(f'text={TARGET_GAME}').first
            if container.count() > 0:
                for up_text in ["アップグレード・期限延長", "期限延長", "アップグレード"]:
                    # Try within nearest row/card
                    try:
                        parent = container.locator('xpath=ancestor::*[self::tr or self::*[@role="row"] or contains(@class,"card")][1]')
                        if try_click(page, parent.locator(f'text={up_text}')):
                            return True
                    except Exception:
                        pass
                    if try_click(page, container.locator(f'text={up_text}')):
                        return True
        except Exception:
            pass

    ok = click_by_text(page, ["アップグレード・期限延長", "期限延長", "アップグレード"])
    if ok:
        snap(page, "after_click_upgrade_extend")
    else:
        log("Could not find アップグレード・期限延長 on current page.")
    return ok

def select_hours(page, hours: int) -> bool:
    hours_str = str(hours)
    texts = [
        f"+{hours_str}時間延長", f"＋{hours_str}時間延長",
        f"{hours_str}時間延長",
        f"+{hours_str}時間", f"＋{hours_str}時間",
        f"{hours_str}時間", f"{hours_str} 時間",
    ]
    for t in texts:
        try:
            if try_click(page, page.get_by_label(t, exact=False)):
                return True
        except Exception:
            pass
    for t in texts:
        try:
            if try_click(page, page.get_by_role("radio", name=t, exact=False)):
                return True
        except Exception:
            pass
    for t in texts:
        try:
            if try_click(page, page.locator(f'label:has-text("{t}")')):
                return True
        except Exception:
            pass
    for sel in [
        f'input[type="radio"][value="{hours_str}"]',
        f'input[type="radio"][value*="{hours_str}"]',
        f'input[value="{hours_str}"]',
        f'input[value*="{hours_str}"]',
    ]:
        try:
            if try_click(page, page.locator(sel)):
                return True
        except Exception:
            pass
    return click_by_text(page, texts)

def do_extend_hours(page, hours: int) -> bool:
    # 入口按钮
    click_by_text(page, ["期限を延長する", "延長する"])

    if not select_hours(page, hours):
        log(f"Could not select +{hours}時間 option. It may be unavailable or UI changed.")
        snap(page, f"failed_select_{hours}h")
    else:
        snap(page, f"selected_{hours}h")

    if not click_by_text(page, ["確認画面に進む", "確認へ進む", "確認"]):
        log("Could not find 確認画面に進む (maybe already on confirm).")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass
        snap(page, "after_go_confirm")

    if not click_by_text(page, ["期限を延長する", "延長する", "実行する"]):
        log("Could not find the final 期限を延長する button.")
        snap(page, "failed_final_extend_click")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_extend_submit")

    for t in ["延長", "完了", "処理が完了", "更新されました", "受け付けました"]:
        try:
            if page.get_by_text(t, exact=False).first.is_visible():
                log("Extension likely succeeded.")
                return True
        except Exception:
            pass
    log("Did not detect a success message; treating as success but please review screenshots.")
    return True

# ------------------ Main ------------------
def main():
    # Interval gate (skip if not due)
    if not FORCE_RENEW:
        ok, due = should_run_interval(RENEW_LOG_MD, RENEW_INTERVAL_HOURS)
        if not ok:
            last = get_last_success_utc(RENEW_LOG_MD)
            log(f"Not due yet. Last success (UTC): {last.isoformat() if last else 'N/A'}, Next due (UTC): {due.isoformat() if due else 'N/A'}")
            sys.exit(0)
        else:
            log("Interval due or first run. Proceeding...")
    else:
        log("FORCE_RENEW=1, skipping interval check.")

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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
            log("Login failed. Check credentials/cookie.")
            context.close()
            browser.close()
            sys.exit(2)

        log("Navigating to Game Management...")
        navigate_to_game_management(page)

        log("Opening upgrade/extend page...")
        if not click_upgrade_or_extend(page):
            log("Could not open upgrade/extend page. Exiting.")
            snap(page, "open_upgrade_extend_failed")
            context.close()
            browser.close()
            sys.exit(3)

        log(f"Performing +{RENEW_HOURS}h extension...")
        success = do_extend_hours(page, RENEW_HOURS)

        if success:
            write_success_md(RENEW_LOG_MD, LOG_TIMEZONE)
            log("All steps completed.")
            rc = 0
        else:
            log("Extension step reported failure.")
            rc = 4

        context.close()
        browser.close()
        sys.exit(rc)

if __name__ == "__main__":
    main()
