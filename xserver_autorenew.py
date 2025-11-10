# -*- coding: utf-8 -*-
import os
import re
import sys
import time
from pathlib import Path
from typing import List
from datetime import datetime

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

# 续期“选择的时长”（与运行频率无关）
RENEW_HOURS = int(os.getenv("RENEW_HOURS", "72"))

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
    for t in ["ログアウト", "マイページ", "アカウント", "お知らせ"]:
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
    # 勾选“同意/確認/承諾”等复选框，避免提交按钮被禁用
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

    # 提交
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

# ------------------ Navigation ------------------
UPGRADE_TEXTS = [
    "アップグレード・期限延長", "アップグレード/期限延長", "アップグレード ・ 期限延長",
    "期限延長", "期限を延長する", "更新", "更新手続き",
    "プラン変更・期限延長", "プラン変更"
]
DETAIL_TEXTS = ["詳細", "管理", "設定", "ゲーム詳細", "サービス詳細", "契約情報", "メニュー"]
CONTRACT_TEXTS = ["契約", "契約情報", "料金", "お支払い", "支払い", "請求", "更新", "延長", "プラン変更"]

def ensure_on_game_index(page):
    goto(page, GAME_INDEX_URL)
    snap(page, "on_game_index")

def open_game_management(page) -> bool:
    # 按行点击右侧“ゲーム管理”按钮（表格里的蓝色按钮）
    def click_row_btn(row) -> bool:
        for sel in [
            'button:has-text("ゲーム管理")',
            '[role="button"]:has-text("ゲーム管理")',
            'a:has-text("ゲーム管理")',
            ':is(button,a,div,span)[class*="btn"]:has-text("ゲーム管理")',
            ':is(button,a,div,span):has-text("ゲーム管理")',
        ]:
            try:
                loc = row.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return try_click(page, loc.first, timeout=1500)
            except Exception:
                pass
        return False

    # 先用 TARGET_GAME 锁定行（例如“waters”）
    target_row = None
    if TARGET_GAME:
        try:
            tbody_rows = page.locator("tbody tr").filter(has_text=TARGET_GAME)
            if tbody_rows.count() > 0:
                target_row = tbody_rows.first
            else:
                any_rows = page.locator("tr").filter(has_text=TARGET_GAME)
                if any_rows.count() > 0:
                    target_row = any_rows.first
        except Exception:
            target_row = None

    if target_row:
        if click_row_btn(target_row):
            snap(page, "clicked_row_game_management_target")
            try:
                page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
            except Exception:
                pass
            return True

    # 没指定或没命中：点第一个“ゲーム管理”
    for sel in [
        'tbody tr:has(button:has-text("ゲーム管理")) >> button:has-text("ゲーム管理")',
        'tbody tr:has([role="button"]:has-text("ゲーム管理")) >> [role="button"]:has-text("ゲーム管理")',
        'tbody tr:has(a:has-text("ゲーム管理")) >> a:has-text("ゲーム管理")',
        'button:has-text("ゲーム管理")',
        '[role="button"]:has-text("ゲーム管理")',
        'a:has-text("ゲーム管理")',
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                if try_click(page, loc.first, timeout=1500):
                    snap(page, "clicked_row_game_management_first")
                    try:
                        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass

    # 遍历前几行兜底
    try:
        rows = page.locator("tbody tr")
        cnt = rows.count()
        n = min(cnt if cnt else 0, 10)
        for i in range(n):
            row = rows.nth(i)
            if click_row_btn(row):
                snap(page, f"clicked_row_game_management_index_{i}")
                try:
                    page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
                except Exception:
                    pass
                return True
    except Exception:
        pass

    snap(page, "game_management_not_found")
    log("Row-level 'ゲーム管理' button was not found.")
    return False

def open_game_detail(page) -> bool:
    # 如需从“詳細/管理/設定”进入（备用路径）
    try:
        if TARGET_GAME:
            container = page.locator(f'text={TARGET_GAME}').first
            if container and container.count() > 0:
                parent = container.locator('xpath=ancestor::*[self::tr or contains(@class,"card") or contains(@class,"item")][1]')
                for t in DETAIL_TEXTS:
                    if try_click(page, parent.locator(f'text={t}')) or try_click(page, container.locator(f'text={t}')):
                        return True
        if click_text_global(page, DETAIL_TEXTS):
            return True
    except Exception:
        pass
    return False

def click_upgrade_or_extend(page) -> bool:
    # 在“ゲーム管理”页直接找入口
    if click_text_global(page, UPGRADE_TEXTS):
        snap(page, "after_click_upgrade_extend")
        return True

    # 兜底：进入“詳細/管理/設定”或“契約/料金/更新”再找
    log("Upgrade/extend not found on game-management page. Trying detail/billing...")
    if open_game_detail(page):
        try:
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass
        snap(page, "after_open_detail")
        if click_text_global(page, UPGRADE_TEXTS):
            snap(page, "after_click_upgrade_extend_from_detail")
            return True
        if click_text_global(page, ["契約", "契約情報", "料金", "お支払い", "支払い", "請求", "更新", "延長", "プラン変更"]):
            try:
                page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
            except Exception:
                pass
            snap(page, "after_open_contract_or_billing")
            if click_text_global(page, UPGRADE_TEXTS):
                snap(page, "after_click_upgrade_extend_from_contract")
                return True

    snap(page, "open_upgrade_extend_failed")
    dump_html(page, "open_upgrade_extend_failed")
    return False

# ------------------ Extend ------------------
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
    return click_text_global(page, texts)

def do_extend_hours(page, hours: int) -> bool:
    # 你的最新流程：先在“アップグレード・期限延長”页底部点击“期限を延長する”，再选择时长
    scroll_to_bottom(page)
    if not click_text_global(page, ["期限を延長する", "延長する"]):
        log("Entry button '期限を延長する' not found at bottom; trying anyway.")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass
        snap(page, "after_click_entry_extend")

    if not select_hours(page, hours):
        log(f"Could not select +{hours}時間 option. It may be unavailable or UI changed.")
        snap(page, f"failed_select_{hours}h")
    else:
        snap(page, f"selected_{hours}h")

    accept_required_checks(page)

    # 確認画面に進む
    confirm_texts = [
        "確認画面に進む", "確認へ進む", "確認画面へ", "確認画面へ進む",
        "申込内容を確認", "申し込み内容を確認", "申込み内容を確認",
        "確認する", "次へ", "次に進む", "進む"
    ]
    if not click_text_global(page, confirm_texts):
        log("Could not find a confirm-step button; maybe already on confirm page.")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass
        snap(page, "after_go_confirm")

    scroll_to_bottom(page)
    accept_required_checks(page)

    # 最终提交
    final_texts = [
        "期限を延長する", "延長する", "実行する",
        "延長を確定する", "確定する",
        "申込みを確定する", "お申し込みを確定する",
        "申込を確定する", "お申込みを確定する"
    ]
    if not click_text_global(page, final_texts):
        if not click_submit_fallback(page):
            log("Could not find the final submit button.")
            snap(page, "failed_final_extend_click")
            return False

    try:
        page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass
    snap(page, "after_extend_submit")

    for t in ["延長", "完了", "処理が完了", "更新されました", "受け付けました", "受付しました", "手続きが完了"]:
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

        # 登录：先 Cookie，后账号密码
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

        # 登录后直接到 xmgame/index
        ensure_on_game_index(page)

        # ゲーム管理（表格行内按钮）
        log("Opening ゲーム管理 (row button)...")
        if not open_game_management(page):
            log("Could not open ゲーム管理 (row). Exiting.")
            dump_html(page, "game_management_not_found")
            context.close()
            browser.close()
            sys.exit(3)

        # アップグレード・期限延長
        log("Opening アップグレード・期限延長...")
        if not click_upgrade_or_extend(page):
            log("Could not open upgrade/extend page. Exiting.")
            context.close()
            browser.close()
            sys.exit(3)

        # 执行续期
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
