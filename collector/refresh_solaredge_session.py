#!/usr/bin/env python3
import json
import os
import re
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COOKIE_PATH = PROJECT_ROOT / "collector" / "solaredge_cookie.txt"
SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
SITE_ID = os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID", "4457399")
LOGIN_URL = "https://monitoring.solaredge.com/solaredge-web/p/login"


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
        locator = page.locator(selector)
        if locator.count():
            return locator.first()
    return None


def login(page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    username_field = find_first(
        page,
        (
            "input[type='email']",
            "input[name='username']",
            "input[name='j_username']",
            "input[id*='user' i]",
            "input[name*='email' i]",
        ),
    )
    password_field = find_first(
        page,
        (
            "input[type='password']",
            "input[name='password']",
            "input[name='j_password']",
            "input[id*='password' i]",
        ),
    )
    if username_field is None or password_field is None:
        raise RefreshError("SolarEdge login fields were not found")

    username_field.fill(username)
    password_field.fill(password)

    submit = find_first(
        page,
        (
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
        ),
    )
    if submit is None:
        password_field.press("Enter")
    else:
        submit.click()

    page.wait_for_load_state("domcontentloaded", timeout=30000)


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
