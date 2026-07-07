#!/usr/bin/env python3
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COOKIE_PATH = PROJECT_ROOT / "collector" / "solaredge_cookie.txt"
SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
DEBUG_DIR = PROJECT_ROOT / "logs" / "debug"
SITE_ID = os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID", "4457399")
LOGIN_URL = "https://monitoring.solaredge.com/mfe/auth/"

USERNAME_SELECTORS = (
    "input[name='username'][type='email']",
    'input[name="username"][type="email"]',
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
    "input[name='password'][type='password']",
    'input[name="password"][type="password"]',
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
    "form:has(input[name='password']) button:has-text('Sign in')",
    'form:has(input[name="password"]) button:has-text("Sign in")',
    "form:has(input[type='password']) button:has-text('Sign in')",
    'form:has(input[type="password"]) button:has-text("Sign in")',
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

LOGIN_SOLAREDGE_USERNAME_SELECTORS = (
    "input[name='username'][type='email']",
    'input[name="username"][type="email"]',
    "input[type='email'][name='username']",
    'input[type="email"][name="username"]',
    "input[type='email'][placeholder='name@host.com']",
    'input[type="email"][placeholder="name@host.com"]',
)

LOGIN_SOLAREDGE_PASSWORD_SELECTORS = (
    "input[name='password'][type='password']",
    'input[name="password"][type="password"]',
    "input[type='password'][name='password']",
    'input[type="password"][name="password"]',
    "input[type='password'][placeholder='Enter password']",
    'input[type="password"][placeholder="Enter password"]',
)

LOGIN_SOLAREDGE_SIGN_IN_SELECTORS = (
    "form:has(input[name='password'][type='password']) button:has-text('Sign in')",
    'form:has(input[name="password"][type="password"]) button:has-text("Sign in")',
    "form:has(input[name='password']) button:has-text('Sign in')",
    'form:has(input[name="password"]) button:has-text("Sign in")',
)

INITIAL_LOGIN_SELECTORS = (
    "#se-signing-mfe button:has-text('Log in')",
    '#se-signing-mfe button:has-text("Log in")',
    'button:has-text("Log in")',
    "button:has-text('Log in')",
    "button[type='button']:has-text('Log in')",
    'button[type="button"]:has-text("Log in")',
    'text="Log in"',
    "role=button[name=/log in/i]",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
    "a:has-text('Log in')",
    "a:has-text('Login')",
    "a:has-text('Sign in')",
    "input[value*='Log in' i]",
    "input[value*='Login' i]",
    "[role='button']:has-text('Log in')",
)

CHALLENGE_SELECTORS = (
    "iframe[src*='captcha' i]",
    "iframe[src*='recaptcha' i]",
    "iframe[src*='challenge' i]",
    "text=/captcha|challenge|verification|verify you|single sign-on|sso/i",
)

SITES_LIST_SELECTORS = (
    "text=/\\bSites\\b/i",
    "text=/Site ID/i",
    "table:has-text('Site ID')",
    "[data-testid*='site' i]",
    "a[href*='/site/']",
    "a[href*='siteId=']",
)

FIRST_SITE_SELECTORS = (
    "a[href*='/site/']",
    "a[href*='siteId=']",
    "tr:has(a[href*='/site/'])",
    "tr:has-text('Site ID')",
    "[data-testid*='site' i]",
)

DASHBOARD_INDICATOR_SELECTORS = (
    "text=/energy produced/i",
    "text=/power flow/i",
    "text=/production/i",
    "text=/consumption/i",
    "text=/dashboard/i",
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


def selector_count(page, selectors: tuple[str, ...]) -> int:
    total = 0
    for selector in selectors:
        try:
            total += int(page.locator(selector).count())
        except Exception:
            continue
    return total


def visible_selector_count(page, selectors: tuple[str, ...]) -> int:
    total = 0
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = int(locator.count())
        except Exception:
            continue
        for index in range(count):
            try:
                if element_visible(locator_item(locator, index)):
                    total += 1
            except Exception:
                continue
    return total


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


def is_solaredge_login_host(page) -> bool:
    return page_hostname(page).lower() == "login.solaredge.com"


def username_selectors_for_page(page) -> tuple[str, ...]:
    if is_solaredge_login_host(page):
        return LOGIN_SOLAREDGE_USERNAME_SELECTORS
    return USERNAME_SELECTORS


def password_selectors_for_page(page) -> tuple[str, ...]:
    if is_solaredge_login_host(page):
        return LOGIN_SOLAREDGE_PASSWORD_SELECTORS
    return PASSWORD_SELECTORS


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


def route_path(page) -> str:
    try:
        parsed = urlparse(str(page.url or ""))
    except Exception:
        return "unknown"
    route = parsed.path or "/"
    if parsed.fragment:
        route = f"{route}#{parsed.fragment}"
    return route


def save_login_debug(page, reason: str, allow_screenshot: bool, extra: Optional[dict] = None) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_path = DEBUG_DIR / f"solaredge-login-{timestamp}"
    metadata = {
        "reason": reason,
        "title": safe_page_title(page),
        "url": str(getattr(page, "url", "") or ""),
        "hostname": page_hostname(page),
        "route_path": route_path(page),
        "username_field_found": selector_found(page, username_selectors_for_page(page)),
        "password_field_found": selector_found(page, password_selectors_for_page(page)),
        "challenge_or_sso_likely": challenge_likely(page),
    }
    if extra:
        metadata.update({str(key): value for key, value in extra.items()})
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


def login_failure_message(
    page,
    reason: str,
    *,
    username_found: bool,
    password_found: bool,
    allow_screenshot: bool,
    extra: Optional[dict] = None,
) -> str:
    debug_path = save_login_debug(page, reason, allow_screenshot=allow_screenshot, extra=extra)
    extra = extra or {}
    return (
        f"SolarEdge login fields were not found. "
        f"title={safe_page_title(page)!r}; "
        f"hostname={page_hostname(page)!r}; "
        f"username_field_found={username_found}; "
        f"password_field_found={password_found}; "
        f"challenge_or_sso_likely={challenge_likely(page)}; "
        f"initial_login_button_found={bool(extra.get('initial_login_button_found'))}; "
        f"initial_login_button_clicked={bool(extra.get('initial_login_button_clicked'))}; "
        f"signing_mfe_found={bool(extra.get('signing_mfe_found'))}; "
        f"initial_login_button_candidates={int(extra.get('initial_login_button_candidates') or 0)}; "
        f"initial_login_button_visible_candidates={int(extra.get('initial_login_button_visible_candidates') or 0)}; "
        f"initial_login_clicked_selector={extra.get('initial_login_clicked_selector') or ''}; "
        f"explicit_login_host_path_used={bool(extra.get('explicit_login_host_path_used'))}; "
        f"explicit_username_wait_success={bool(extra.get('explicit_username_wait_success'))}; "
        f"explicit_password_wait_success={bool(extra.get('explicit_password_wait_success'))}; "
        f"explicit_fields_filled={bool(extra.get('explicit_fields_filled'))}; "
        f"submit_attempted={bool(extra.get('submit_attempted'))}; "
        f"visible_email_input_count={int(extra.get('visible_email_input_count') or 0)}; "
        f"visible_password_input_count={int(extra.get('visible_password_input_count') or 0)}; "
        f"sign_in_button_count={int(extra.get('sign_in_button_count') or 0)}; "
        f"clicked_sign_in_selector={extra.get('clicked_sign_in_selector') or ''}; "
        f"field_search_attempts={int(extra.get('field_search_attempts') or 0)}; "
        f"debug={debug_path}"
    )


def sign_in_submit_failure_message(page, metadata: dict) -> str:
    debug_path = save_login_debug(page, "sign-in did not continue", allow_screenshot=True, extra=metadata)
    return (
        "SolarEdge sign-in failed or did not continue after credentials were submitted. "
        f"title={safe_page_title(page)!r}; "
        f"hostname={page_hostname(page)!r}; "
        f"password_form_found={bool(metadata.get('password_form_found'))}; "
        f"account_sign_in_button_found={bool(metadata.get('account_sign_in_button_found'))}; "
        f"account_sign_in_button_clicked={bool(metadata.get('account_sign_in_button_clicked'))}; "
        f"enter_submit_used={bool(metadata.get('enter_submit_used'))}; "
        f"url_before_submit={metadata.get('url_before_submit') or ''}; "
        f"url_after_submit={metadata.get('url_after_submit') or ''}; "
        f"debug={debug_path}"
    )


def sign_in_button_failure_message(page, metadata: dict) -> str:
    debug_path = save_login_debug(page, "sign-in button could not be submitted", allow_screenshot=True, extra=metadata)
    return (
        "SolarEdge sign-in button could not be submitted. "
        f"title={safe_page_title(page)!r}; "
        f"hostname={page_hostname(page)!r}; "
        f"password_form_found={bool(metadata.get('password_form_found'))}; "
        f"account_sign_in_button_found={bool(metadata.get('account_sign_in_button_found'))}; "
        f"account_sign_in_button_clicked={bool(metadata.get('account_sign_in_button_clicked'))}; "
        f"enter_submit_used={bool(metadata.get('enter_submit_used'))}; "
        f"url_before_submit={metadata.get('url_before_submit') or ''}; "
        f"url_after_submit={metadata.get('url_after_submit') or ''}; "
        f"debug={debug_path}"
    )


def wait_for_login_fields(page, attempts: int = 6) -> tuple[object, object, int]:
    username_field = None
    password_field = None
    for attempt in range(1, max(1, attempts) + 1):
        username_field = find_first(page, username_selectors_for_page(page))
        password_field = find_first(page, password_selectors_for_page(page))
        if username_field is not None or password_field is not None:
            return username_field, password_field, attempt
        try:
            page.wait_for_timeout(1000)
        except Exception:
            wait_for_page_settle(page)
    return username_field, password_field, max(1, attempts)


def wait_for_initial_login_shell(page) -> bool:
    try:
        page.wait_for_selector("div#se-signing-mfe", timeout=15000)
    except Exception:
        pass
    return selector_found(page, ("div#se-signing-mfe", "#se-signing-mfe"))


def locator_item(locator, index: int):
    try:
        return locator.nth(index)
    except Exception:
        if index == 0:
            return locator.first()
        raise


def element_visible(element) -> bool:
    try:
        return bool(element.is_visible(timeout=1000))
    except TypeError:
        try:
            return bool(element.is_visible())
        except Exception:
            return False
    except Exception:
        return False


def element_enabled(element) -> bool:
    try:
        return bool(element.is_enabled(timeout=1000))
    except TypeError:
        try:
            return bool(element.is_enabled())
        except Exception:
            return True
    except AttributeError:
        return True
    except Exception:
        return False


def click_initial_login_button(page) -> tuple[object, dict]:
    details = []
    total_candidates = 0
    visible_candidates = 0
    for selector in INITIAL_LOGIN_SELECTORS:
        selector_detail = {
            "selector": selector,
            "candidate_count": 0,
            "visible_count": 0,
            "skipped": [],
        }
        try:
            locator = page.locator(selector)
            count = int(locator.count())
        except Exception as exc:
            selector_detail["skipped"].append(f"selector_error:{type(exc).__name__}")
            details.append(selector_detail)
            continue
        selector_detail["candidate_count"] = count
        total_candidates += count
        for index in range(count):
            try:
                candidate = locator_item(locator, index)
            except Exception as exc:
                selector_detail["skipped"].append(f"{index}:locator_error:{type(exc).__name__}")
                continue
            if not element_visible(candidate):
                selector_detail["skipped"].append(f"{index}:not_visible")
                continue
            selector_detail["visible_count"] += 1
            visible_candidates += 1
            if not element_enabled(candidate):
                selector_detail["skipped"].append(f"{index}:not_enabled")
                continue
            candidate.click()
            return candidate, {
                "initial_login_button_found": True,
                "initial_login_button_clicked": True,
                "initial_login_button_candidates": total_candidates,
                "initial_login_button_visible_candidates": visible_candidates,
                "initial_login_clicked_selector": selector,
                "initial_login_candidate_details": details + [selector_detail],
            }
        details.append(selector_detail)
    return None, {
        "initial_login_button_found": total_candidates > 0,
        "initial_login_button_clicked": False,
        "initial_login_button_candidates": total_candidates,
        "initial_login_button_visible_candidates": visible_candidates,
        "initial_login_clicked_selector": "",
        "initial_login_candidate_details": details,
    }


def wait_after_initial_login_click(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_selector(
            ", ".join(LOGIN_SOLAREDGE_USERNAME_SELECTORS + LOGIN_SOLAREDGE_PASSWORD_SELECTORS + USERNAME_SELECTORS + PASSWORD_SELECTORS),
            timeout=15000,
        )
    except Exception:
        wait_for_page_settle(page)


def find_sign_in_button(page):
    if is_solaredge_login_host(page):
        scoped = find_first(page, LOGIN_SOLAREDGE_SIGN_IN_SELECTORS)
        if scoped is not None:
            return scoped
    return find_first(page, SIGN_IN_SELECTORS)


def first_visible_enabled(locator):
    try:
        count = int(locator.count())
    except Exception:
        return None, 0
    for index in range(count):
        try:
            candidate = locator_item(locator, index)
        except Exception:
            continue
        if element_visible(candidate) and element_enabled(candidate):
            return candidate, count
    return None, count


def click_button_inside(scope, selector: str):
    try:
        locator = scope.locator(selector)
    except Exception:
        return None, 0
    button, count = first_visible_enabled(locator)
    if button is not None:
        button.click()
    return button, count


def password_form(password_field):
    try:
        form = password_field.locator("xpath=ancestor::form[1]")
        if int(form.count()):
            return form
    except Exception:
        return None
    return None


def password_container(password_field):
    for selector in (
        "xpath=ancestor::*[self::div or self::section or self::main][1]",
        "xpath=ancestor::div[1]",
    ):
        try:
            container = password_field.locator(selector)
            if int(container.count()):
                return container
        except Exception:
            continue
    return None


def submit_account_sign_in(page, password_field, metadata: dict) -> None:
    metadata["submit_attempted"] = False
    metadata["password_form_found"] = False
    metadata["account_sign_in_button_found"] = False
    metadata["account_sign_in_button_clicked"] = False
    metadata["enter_submit_used"] = False
    metadata["url_before_submit"] = str(getattr(page, "url", "") or "")
    metadata["url_after_submit"] = metadata["url_before_submit"]

    form = password_form(password_field)
    if form is not None:
        metadata["password_form_found"] = True
        button, count = click_button_inside(form, "button, input[type='submit'], [role='button']")
        metadata["sign_in_button_count"] = count
        if button is not None:
            metadata["submit_attempted"] = True
            metadata["account_sign_in_button_found"] = True
            metadata["account_sign_in_button_clicked"] = True
            metadata["clicked_sign_in_selector"] = "password_form button"
            wait_after_sign_in_submit(page)
            metadata["url_after_submit"] = str(getattr(page, "url", "") or "")
            return

    container = password_container(password_field)
    if container is not None:
        button, count = click_button_inside(
            container,
            "button:has-text('Sign in'), button:has-text('Sign In'), [role='button']:has-text('Sign in')",
        )
        metadata["sign_in_button_count"] = max(int(metadata.get("sign_in_button_count") or 0), count)
        if button is not None:
            metadata["submit_attempted"] = True
            metadata["account_sign_in_button_found"] = True
            metadata["account_sign_in_button_clicked"] = True
            metadata["clicked_sign_in_selector"] = "password_container Sign in"
            wait_after_sign_in_submit(page)
            metadata["url_after_submit"] = str(getattr(page, "url", "") or "")
            return

    try:
        password_field.press("Enter")
        metadata["submit_attempted"] = True
        metadata["enter_submit_used"] = True
        metadata["clicked_sign_in_selector"] = "password_field Enter"
        wait_after_sign_in_submit(page)
        metadata["url_after_submit"] = str(getattr(page, "url", "") or "")
        if not is_solaredge_login_host(page):
            return
    except Exception:
        pass

    try:
        page.keyboard.press("Enter")
        metadata["submit_attempted"] = True
        metadata["enter_submit_used"] = True
        metadata["clicked_sign_in_selector"] = "page.keyboard Enter"
        wait_after_sign_in_submit(page)
        metadata["url_after_submit"] = str(getattr(page, "url", "") or "")
    except Exception:
        pass


def wait_after_sign_in_submit(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_function("window.location.hostname !== 'login.solaredge.com'", timeout=15000)
    except Exception:
        wait_for_page_settle(page)


def click_existing_account_sign_in(page) -> tuple[object, str]:
    for selector in LOGIN_SOLAREDGE_SIGN_IN_SELECTORS:
        try:
            locator = page.locator(selector)
            count = int(locator.count())
        except Exception:
            continue
        for index in range(count):
            try:
                candidate = locator_item(locator, index)
            except Exception:
                continue
            if not element_visible(candidate) or not element_enabled(candidate):
                continue
            candidate.click()
            return candidate, selector
    return None, ""


def explicit_login_solaredge(page, username: str, password: str) -> tuple[bool, dict]:
    metadata = {
        "explicit_login_host_path_used": is_solaredge_login_host(page),
        "explicit_username_wait_success": False,
        "explicit_password_wait_success": False,
        "visible_email_input_count": 0,
        "visible_password_input_count": 0,
        "sign_in_button_count": 0,
        "clicked_sign_in_selector": "",
        "explicit_fields_filled": False,
        "submit_attempted": False,
        "password_form_found": False,
        "account_sign_in_button_found": False,
        "account_sign_in_button_clicked": False,
        "enter_submit_used": False,
        "url_before_submit": "",
        "url_after_submit": "",
    }
    if not metadata["explicit_login_host_path_used"]:
        return False, metadata

    try:
        username_field = page.wait_for_selector('input[name="username"][type="email"]', state="visible", timeout=30000)
        metadata["explicit_username_wait_success"] = True
    except Exception:
        metadata["visible_email_input_count"] = visible_selector_count(page, LOGIN_SOLAREDGE_USERNAME_SELECTORS)
        return False, metadata

    try:
        password_field = page.wait_for_selector('input[name="password"][type="password"]', state="visible", timeout=30000)
        metadata["explicit_password_wait_success"] = True
    except Exception:
        metadata["visible_email_input_count"] = visible_selector_count(page, LOGIN_SOLAREDGE_USERNAME_SELECTORS)
        metadata["visible_password_input_count"] = visible_selector_count(page, LOGIN_SOLAREDGE_PASSWORD_SELECTORS)
        return False, metadata

    metadata["visible_email_input_count"] = visible_selector_count(page, LOGIN_SOLAREDGE_USERNAME_SELECTORS)
    metadata["visible_password_input_count"] = visible_selector_count(page, LOGIN_SOLAREDGE_PASSWORD_SELECTORS)
    if username_field is None or password_field is None:
        return False, metadata

    try:
        username_field.fill(username)
        password_field.fill(password)
        metadata["explicit_fields_filled"] = True
    except Exception:
        return False, metadata
    submit_account_sign_in(page, password_field, metadata)
    if not metadata["submit_attempted"]:
        raise RefreshError(sign_in_button_failure_message(page, metadata))
    if is_solaredge_login_host(page):
        raise RefreshError(sign_in_submit_failure_message(page, metadata))
    return True, metadata


def sites_list_detected(page) -> bool:
    route = route_path(page).lower()
    if "site" in route and "dashboard" not in route:
        return True
    return selector_found(page, SITES_LIST_SELECTORS)


def dashboard_loaded(page, site_id: str = SITE_ID) -> bool:
    route = route_path(page).lower()
    current_url = str(getattr(page, "url", "") or "").lower()
    if "dashboard" in route and (not site_id or site_id.lower() in current_url or "site" in route):
        return True
    return selector_found(page, DASHBOARD_INDICATOR_SELECTORS)


def dashboard_urls(site_id: str) -> tuple[str, ...]:
    return (
        f"https://monitoring.solaredge.com/solaredge-web/p/site/{site_id}/#/dashboard",
        f"https://monitoring.solaredge.com/solaredge-web/p/site/{site_id}",
        f"https://monitoring.solaredge.com/solaredge-web/p/sites/{site_id}",
        f"https://monitoring.solaredge.com/solaredge-web/p/dashboard?siteId={site_id}",
    )


def configured_site_selectors(site_id: str) -> tuple[str, ...]:
    return (
        f"a[href*='{site_id}']",
        f"tr:has-text('{site_id}')",
        f"button:has-text('{site_id}')",
        f"text='{site_id}'",
    )


def click_site(page, site_id: str = "") -> bool:
    if site_id and click_first(page, configured_site_selectors(site_id)):
        return True
    return click_first(page, FIRST_SITE_SELECTORS)


def save_navigation_debug(page, reason: str, metadata: dict) -> Path:
    safe_metadata = {
        "sites_list_detected": bool(metadata.get("sites_list_detected")),
        "site_row_clicked": bool(metadata.get("site_row_clicked")),
        "dashboard_loaded": bool(metadata.get("dashboard_loaded")),
        "configured_site_id_used": bool(metadata.get("configured_site_id_used")),
    }
    return save_login_debug(page, reason, allow_screenshot=True, extra=safe_metadata)


def site_navigation_failure_message(page, reason: str, metadata: dict) -> str:
    debug_path = save_navigation_debug(page, reason, metadata)
    return (
        f"SolarEdge site dashboard could not be opened. "
        f"title={safe_page_title(page)!r}; "
        f"hostname={page_hostname(page)!r}; "
        f"route_path={route_path(page)!r}; "
        f"sites_list_detected={bool(metadata.get('sites_list_detected'))}; "
        f"site_row_clicked={bool(metadata.get('site_row_clicked'))}; "
        f"dashboard_loaded={bool(metadata.get('dashboard_loaded'))}; "
        f"configured_site_id_used={bool(metadata.get('configured_site_id_used'))}; "
        f"debug={debug_path}"
    )


def open_site_dashboard(page, site_id: str = SITE_ID) -> dict:
    wait_for_page_settle(page)
    metadata = {
        "sites_list_detected": sites_list_detected(page),
        "site_row_clicked": False,
        "dashboard_loaded": dashboard_loaded(page, site_id),
        "configured_site_id_used": False,
    }
    if metadata["dashboard_loaded"]:
        return metadata

    if site_id:
        for url in dashboard_urls(site_id):
            metadata["configured_site_id_used"] = True
            page.goto(url, wait_until="domcontentloaded")
            wait_for_page_settle(page)
            metadata["sites_list_detected"] = metadata["sites_list_detected"] or sites_list_detected(page)
            metadata["dashboard_loaded"] = dashboard_loaded(page, site_id)
            if metadata["dashboard_loaded"]:
                return metadata

    metadata["sites_list_detected"] = metadata["sites_list_detected"] or sites_list_detected(page)
    metadata["site_row_clicked"] = click_site(page, site_id)
    metadata["dashboard_loaded"] = dashboard_loaded(page, site_id)
    if metadata["dashboard_loaded"]:
        return metadata

    raise RefreshError(site_navigation_failure_message(page, "site dashboard not loaded", metadata))


def power_url(day: Optional[date] = None, site_id: str = SITE_ID) -> str:
    current_day = (day or date.today()).isoformat()
    return (
        f"https://monitoring.solaredge.com/services/dashboard/power/sites/{site_id}"
        "?chart-time-unit=quarter-hours"
        f"&start-date={current_day}"
        f"&end-date={current_day}"
        "&measurement-types=production%2Cconsumption%2C"
        "consumption-distribution-without-storage%2C"
        "production-distribution-without-storage%2Cstorage-charge-level"
    )


def verify_data_endpoint(page, site_id: str = SITE_ID) -> None:
    with page.expect_response(
        lambda response: f"/services/dashboard/power/sites/{site_id}" in response.url
        and response.status == 200,
        timeout=60000,
    ):
        page.goto(power_url(site_id=site_id), wait_until="domcontentloaded")


def login(page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    wait_for_page_settle(page)

    explicit_success, explicit_metadata = explicit_login_solaredge(page, username, password)
    if explicit_success:
        return

    url_before_initial_click = str(getattr(page, "url", "") or "")
    initial_button = None
    initial_clicked = False
    signing_mfe_found = wait_for_initial_login_shell(page)
    initial_button_metadata = {
        "initial_login_button_found": False,
        "initial_login_button_clicked": False,
        "initial_login_button_candidates": 0,
        "initial_login_button_visible_candidates": 0,
        "initial_login_clicked_selector": "",
        "initial_login_candidate_details": [],
    }
    username_field, password_field, field_search_attempts = wait_for_login_fields(page)
    if username_field is None and password_field is None:
        initial_button, initial_button_metadata = click_initial_login_button(page)
        if initial_button_metadata["initial_login_button_clicked"]:
            initial_clicked = True
            wait_after_initial_login_click(page)
            explicit_success, explicit_retry_metadata = explicit_login_solaredge(page, username, password)
            explicit_metadata.update({key: value for key, value in explicit_retry_metadata.items() if value not in (False, 0, "")})
            if explicit_success:
                return
            username_field, password_field, retry_attempts = wait_for_login_fields(page)
            field_search_attempts += retry_attempts
    url_after_initial_click = str(getattr(page, "url", "") or "")
    login_metadata = {
        "initial_login_button_found": initial_button_metadata["initial_login_button_found"],
        "initial_login_button_clicked": initial_clicked,
        "signing_mfe_found": signing_mfe_found,
        "url_before_initial_login_click": url_before_initial_click,
        "url_after_initial_login_click": url_after_initial_click,
        "field_search_attempts": field_search_attempts,
        **initial_button_metadata,
        **explicit_metadata,
    }
    if username_field is None:
        raise RefreshError(
            login_failure_message(
                page,
                "username field missing",
                username_found=False,
                password_found=password_field is not None,
                allow_screenshot=True,
                extra=login_metadata,
            )
        )

    username_field.fill(username)
    if password_field is None:
        click_first(page, CONTINUE_SELECTORS)
        password_field = find_first(page, password_selectors_for_page(page))
    if password_field is None:
        raise RefreshError(
            login_failure_message(
                page,
                "password field missing after username step",
                username_found=True,
                password_found=False,
                allow_screenshot=False,
                extra=login_metadata,
            )
        )

    password_field.fill(password)

    submit = find_sign_in_button(page)
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
        raise RefreshError(
            "Automatic SolarEdge sign-in is unavailable because Playwright is not installed in the active Python environment."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            login(page, username, password)
            open_site_dashboard(page, SITE_ID)
            verify_data_endpoint(page, SITE_ID)
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
