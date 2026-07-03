#!/usr/bin/env python3
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COOKIE_PATH = PROJECT_ROOT / "collector" / "solaredge_cookie.txt"
SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
DEBUG_DIR = PROJECT_ROOT / "logs" / "debug"
SITE_ID = os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID", "4457399")
LOGIN_URL = "https://monitoring.solaredge.com/solaredge-web/p/login"

USERNAME_SELECTORS = (
    "input[type='email']",
    "input[name='username']",
    "input[name='j_username']",
    "input[name='email']",
    "input[name='loginfmt']",
    "input[name='identifier']",
    "input[id*='user' i]",
    "input[id*='email' i]",
    "input[id*='login' i]",
    "input[name*='user' i]",
    "input[name*='email' i]",
    "input[autocomplete='username']",
    "input[autocomplete='email']",
    "input[placeholder*='email' i]",
    "input[placeholder*='username' i]",
)

PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[name='j_password']",
    "input[id*='password' i]",
    "input[name*='password' i]",
    "input[autocomplete='current-password']",
)

CONTINUE_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Proceed')",
    "input[value*='Continue' i]",
    "input[value*='Next' i]",
)

SIGN_IN_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Login')",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
    "button:has-text('Submit')",
    "input[value*='Login' i]",
    "input[value*='Log in' i]",
    "input[value*='Sign in' i]",
)

CHALLENGE_SELECTORS = (
    "iframe[src*='captcha' i]",
    "iframe[src*='recaptcha' i]",
    "iframe[src*='challenge' i]",
    "text=/captcha|challenge|verification|verify you|single sign-on|sso/i",
)


class RefreshError(RuntimeError):
    pass


def load_secret(name: str, aliases: tuple[str, ...] = ()) -> str:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value:
            return value

    if SECRETS_PATH.exists():
        with SECRETS_PATH.open("r", encoding="utf-8") as secrets_file:
            secrets = json.load(secrets_file)
        if isinstance(secrets, dict):
            for key in (name.lower(), *[alias.lower() for alias in aliases]):
                value = secrets.get(key)
                if value:
                    return str(value)
    return ""


def find_first(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count():
                first = locator.first()
                try:
                    if hasattr(first, "is_visible") and not first.is_visible(timeout=1000):
                        continue
                except TypeError:
                    if hasattr(first, "is_visible") and not first.is_visible():
                        continue
                except Exception:
                    pass
                return first
        except Exception:
            continue
    return None


def selector_found(page, selectors: tuple[str, ...]) -> bool:
    return find_first(page, selectors) is not None


def click_first(page, selectors: tuple[str, ...]) -> bool:
    target = find_first(page, selectors)
    if target is None:
        return False
    target.click()
    wait_for_page_settle(page)
    return True


def wait_for_page_settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        page.wait_for_selector("input, button", timeout=10000)
    except Exception:
        pass


def page_hostname(page) -> str:
    try:
        hostname = urlparse(str(page.url or "")).hostname
    except Exception:
        hostname = None
    return hostname or "unknown"


def safe_page_title(page) -> str:
    try:
        title = str(page.title() or "").strip()
    except Exception:
        title = ""
    return title or "untitled"


def challenge_likely(page) -> bool:
    if "captcha" in safe_page_title(page).lower():
        return True
    for selector in CHALLENGE_SELECTORS:
        try:
            if page.locator(selector).count():
                return True
        except Exception:
            continue
    return False


def save_login_debug(page, reason: str, allow_screenshot: bool) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_path = DEBUG_DIR / f"solaredge-login-{timestamp}"
    metadata = {
        "reason": reason,
        "title": safe_page_title(page),
        "url": str(getattr(page, "url", "") or ""),
        "hostname": page_hostname(page),
        "username_field_found": selector_found(page, USERNAME_SELECTORS),
        "password_field_found": selector_found(page, PASSWORD_SELECTORS),
        "challenge_or_sso_likely": challenge_likely(page),
    }
    metadata_path = base_path.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        metadata_path.chmod(0o600)
    except OSError:
        pass
    if allow_screenshot:
        screenshot_path = base_path.with_suffix(".png")
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot_path.chmod(0o600)
        except Exception:
            pass
    return metadata_path


def login_failure_message(page, reason: str, *, username_found: bool, password_found: bool, allow_screenshot: bool) -> str:
    debug_path = save_login_debug(page, reason, allow_screenshot=allow_screenshot)
    return (
        f"SolarEdge login fields were not found. "
        f"title={safe_page_title(page)!r}; "
        f"hostname={page_hostname(page)!r}; "
        f"username_field_found={username_found}; "
        f"password_field_found={password_found}; "
        f"challenge_or_sso_likely={challenge_likely(page)}; "
        f"debug={debug_path}"
    )


def login(page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    wait_for_page_settle(page)

    username_field = find_first(page, USERNAME_SELECTORS)
    password_field = find_first(page, PASSWORD_SELECTORS)
    if username_field is None:
        raise RefreshError(
            login_failure_message(
                page,
                "username field missing",
                username_found=False,
                password_found=password_field is not None,
                allow_screenshot=True,
            )
        )

    username_field.fill(username)
    if password_field is None:
        click_first(page, CONTINUE_SELECTORS)
        password_field = find_first(page, PASSWORD_SELECTORS)
    if password_field is None:
        raise RefreshError(
            login_failure_message(
                page,
                "password field missing after username step",
                username_found=True,
                password_found=False,
                allow_screenshot=False,
            )
        )

    password_field.fill(password)

    submit = find_first(page, SIGN_IN_SELECTORS)
    if submit is None:
        password_field.press("Enter")
    else:
        submit.click()

    wait_for_page_settle(page)


def cookie_header(cookies: list[dict]) -> str:
    relevant = []
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if value and "solaredge.com" in domain and re.match(r"^[A-Za-z0-9_.-]+$", name):
            relevant.append(f"{name}={value}")
    if not relevant:
        raise RefreshError("No SolarEdge cookies were captured")
    return "; ".join(sorted(relevant))


def save_cookie(header: str) -> None:
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = COOKIE_PATH.with_suffix(".txt.tmp")
    temporary.write_text(header + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(COOKIE_PATH)


def refresh() -> None:
    username = load_secret("SOLAREDGE_USERNAME", ("solaredge_username", "username"))
    password = load_secret("SOLAREDGE_PASSWORD", ("solaredge_password", "password"))
    if not username or not password:
        raise RefreshError(
            "SolarEdge credentials are not configured in environment or config/secrets.json"
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RefreshError("Playwright is not installed for this Python environment") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            login(page, username, password)
            today = date.today().isoformat()
            power_url = (
                f"https://monitoring.solaredge.com/services/dashboard/power/sites/{SITE_ID}"
                "?chart-time-unit=quarter-hours"
                f"&start-date={today}"
                f"&end-date={today}"
                "&measurement-types=production%2Cconsumption%2C"
                "consumption-distribution-without-storage%2C"
                "production-distribution-without-storage%2Cstorage-charge-level"
            )

            with page.expect_response(
                lambda response: "/services/dashboard/power/sites/" in response.url
                and response.status == 200,
                timeout=60000,
            ):
                page.goto(power_url, wait_until="domcontentloaded")

            save_cookie(cookie_header(context.cookies()))
        finally:
            browser.close()


def main() -> int:
    try:
        refresh()
    except RefreshError as exc:
        print(f"refresh failed: {exc}")
        return 1
    except Exception:
        print("refresh failed: browser automation error")
        return 1
    print("refresh ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
