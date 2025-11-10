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

# 页面上选择的续期时长（与运行间隔无关）
RENEW_HOURS = int(os.getenv("RENEW_HOURS", "72"))

# 续期间隔限流（默认 60h；FORCE_RENEW=1 可忽略限流）
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

def dump_html(page, name: str):
    try:
        out = Path("pages")
        ensure_dir(out)
        safe = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", name)
        p = out / f"{int(time.time())}_{safe}.html"
        p.write_text(page.content(), encoding="utf-8")
        log(f"Saved page html: {p}")
    except Exception as e:
        log(f"Dump html failed: {e}")

def parse_cookie_string(cookie_str: str, domain: str) -> List[dict]:
    cookies = []
    for item in [p.strip() for p in cookie_str.split(";") if p.strip()]:
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax"
        })
    return cookies

def is_logged_in(page) -> bool:
    candidates = ["ログアウト", "マイページ", "アカウント", "お知らせ"]
    for t in candidates:
        try:
            if page.get_by_text(t, exact=False).first.is_visible():
                return True
        except Exception:
            pass
    return False

def try_click(page_or_frame, locator, timeout=SHORT_TIMEOUT) -> bool:
    try:
        locator.first.click(timeout=timeout)
        page_or_frame.wait_for_timeout(250)
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

def click_text_global(page, texts):
    if click_by_text(page, texts):
        return True
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        try:
            for t in texts:
                try:
                    if try_click(fr, fr.get_by_role("button", name=t, exact=False)):
                        return True
                except Exception:
                    pass
                try:
                    if try_click(fr, fr.get_by_text(t, exact=False)):
                        return True
                except Exception:
                    pass
                for sel in [f'a:has-text("{t}")', f'button:has-text("{t}")', f'input[value*="{t}"]', f'label:has-text("{t}")']:
                    try:
                        if try_click(fr, fr.locator(sel)):
                            return True
                    except Exception:
                        pass
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

def scroll_to_bottom(page):
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass
    page.wait_for_timeout(400)

def accept_required_checks(page):
    keywords = ["同意", "確認", "承諾", "同意します", "確認しました", "規約", "注意事項"]
    for k in keywords:
        try:
            page.locator(f'label:has-text("{k}")').first.click(timeout=800)
        except Exception:
            pass
    try:
        boxes = page.locator('input[type="checkbox"]')
        count = min(boxes.count(), 5)
        clicked = 0
        for i in range(count):
            el = boxes.nth(i)
            try:
                if el.is_visible() and not el.is_checked():
                    el.check(timeout=800)
                    clicked += 1
            except Exception:
                pass
        if clicked:
            log(f"Checked {clicked} agreement checkbox(es).")
    except Exception:
        pass

def click_submit_fallback(page):
    selectors = [
        'button[type="submit"]:not([disabled])',
        'input[type="submit"]:not([disabled])',
        'button:not([disabled]).is-primary, button:not([disabled]).btn-primary, button:not([disabled]).c-btn--primary',
        'a.button--primary, a.btn-primary'
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return try_click(page, loc.first, timeout=1500)
        except Exception:
            pass
    return False

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

    # 尝试直接进入 game index
    goto(page, GAME_INDEX_URL)
    snap(page, "after_cookie_goto_game_index")
    if is_logged_in(page):
        log("Logged in via cookie (game index).")
        return True

    # 再试登录页
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

    # 填邮箱/ID
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

    # 填密码
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
