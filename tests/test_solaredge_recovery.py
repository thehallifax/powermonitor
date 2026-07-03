import subprocess
import sys
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import collect_today
import refresh_solaredge_session


class FakeLoginElement:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def is_visible(self, timeout=None):
        return True

    def is_enabled(self, timeout=None):
        return True

    def fill(self, value):
        if "password" in self.selector:
            self.page.password_filled = True
        else:
            self.page.username_filled = True

    def click(self):
        self.page.clicks.append(self.selector)
        if self.page.username_filled and not self.page.password_available:
            self.page.password_available = True
            self.page.available_selectors.add("input[type='password']")
            self.page.available_selectors.add("button:has-text('Sign in')")
        elif self.page.password_filled:
            self.page.submitted = True

    def press(self, key):
        self.page.pressed.append(key)
        if self.page.password_filled:
            self.page.submitted = True


class FakeLoginLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        if hasattr(self.page, "selector_count"):
            return self.page.selector_count(self.selector)
        if self.selector in self.page.available_selectors:
            return 1
        if self.selector.startswith("text=/") and self.page.challenge:
            return 1
        return 0

    def first(self):
        return FakeLoginElement(self.page, self.selector)

    def nth(self, index):
        return self.first()


class FakeLoginPage:
    def __init__(self, selectors=None, challenge=False):
        self.available_selectors = set(selectors or [])
        self.challenge = challenge
        self.password_available = "input[type='password']" in self.available_selectors
        self.username_filled = False
        self.password_filled = False
        self.submitted = False
        self.clicks = []
        self.pressed = []
        self.url = "https://monitoring.solaredge.com/solaredge-web/p/login"

    def goto(self, url, wait_until=None):
        self.url = url

    def wait_for_load_state(self, state, timeout=None):
        return None

    def wait_for_selector(self, selector, timeout=None):
        return None

    def wait_for_timeout(self, timeout):
        return None

    def locator(self, selector):
        return FakeLoginLocator(self, selector)

    def title(self):
        return "SolarEdge Login"

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"redacted screenshot placeholder")


class FakeInitialLoginPage(FakeLoginPage):
    def __init__(self):
        super().__init__(
            selectors={
                "div#se-signing-mfe",
                "#se-signing-mfe",
                "#se-signing-mfe button:has-text('Log in')",
                '#se-signing-mfe button:has-text("Log in")',
                "button[type='button']:has-text('Log in')",
                'button[type="button"]:has-text("Log in")',
                "button:has-text('Log in')",
            }
        )
        self.url = "https://monitoring.solaredge.com/mfe/auth/"

    def goto(self, url, wait_until=None):
        self.url = url


class FakeInitialLoginElement(FakeLoginElement):
    def click(self):
        self.page.clicks.append(self.selector)
        if "Log in" in self.selector:
            self.page.url = "https://monitoring.solaredge.com/solaredge-web/p/login"
            self.page.available_selectors.difference_update(refresh_solaredge_session.INITIAL_LOGIN_SELECTORS)
            self.page.available_selectors.add("input[type='email']")
            self.page.available_selectors.add("input[type='password']")
            self.page.available_selectors.add("button:has-text('Sign in')")
            self.page.password_available = True
        else:
            super().click()


class FakeInitialLoginLocator(FakeLoginLocator):
    def first(self):
        return FakeInitialLoginElement(self.page, self.selector)

    def nth(self, index):
        return self.first()


FakeInitialLoginPage.locator = lambda self, selector: FakeInitialLoginLocator(self, selector)


class FakeHiddenInitialLoginElement(FakeInitialLoginElement):
    def is_visible(self, timeout=None):
        return False


class FakeHiddenInitialLoginLocator(FakeInitialLoginLocator):
    def first(self):
        return FakeHiddenInitialLoginElement(self.page, self.selector)

    def nth(self, index):
        return self.first()


class FakeHiddenInitialLoginPage(FakeInitialLoginPage):
    pass


FakeHiddenInitialLoginPage.locator = lambda self, selector: FakeHiddenInitialLoginLocator(self, selector)


class FakeSitePage(FakeLoginPage):
    def __init__(self, site_id="4457399", direct_navigation=True, has_site_row=True):
        super().__init__(selectors={"text=/\\bSites\\b/i", "text=/Site ID/i"})
        self.site_id = site_id
        self.direct_navigation = direct_navigation
        self.has_site_row = has_site_row
        self.dashboard_opened = False
        self.url = "https://monitoring.solaredge.com/solaredge-web/p/sites"

    def goto(self, url, wait_until=None):
        self.url = url
        if not self.direct_navigation and self.site_id in url and ("dashboard" in url or "/site/" in url or f"/sites/{self.site_id}" in url):
            self.url = "https://monitoring.solaredge.com/solaredge-web/p/sites"
            return
        if self.direct_navigation and self.site_id in url and ("dashboard" in url or "/site/" in url or f"/sites/{self.site_id}" in url):
            self.dashboard_opened = True
            self.available_selectors.add("text=/energy produced/i")

    def selector_count(self, selector):
        if self.dashboard_opened and selector in refresh_solaredge_session.DASHBOARD_INDICATOR_SELECTORS:
            return 1
        if self.site_id in selector and self.has_site_row:
            return 1
        if selector in refresh_solaredge_session.FIRST_SITE_SELECTORS and self.has_site_row:
            return 1
        if selector in {"text=/\\bSites\\b/i", "text=/Site ID/i", "table:has-text('Site ID')"} and not self.dashboard_opened:
            return 1
        return 0


class FakeSiteElement(FakeLoginElement):
    def click(self):
        super().click()
        if isinstance(self.page, FakeSitePage):
            self.page.dashboard_opened = True
            self.page.url = f"https://monitoring.solaredge.com/solaredge-web/p/site/{self.page.site_id}/#/dashboard"
            self.page.available_selectors.add("text=/energy produced/i")


class FakeSiteLocator(FakeLoginLocator):
    def first(self):
        return FakeSiteElement(self.page, self.selector)


FakeSitePage.locator = lambda self, selector: FakeSiteLocator(self, selector)


class SolarEdgeRecoveryTests(unittest.TestCase):
    def test_scheduled_collection_uses_shared_recovery_and_imports_readings(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "latest.json"
            imports = []

            def fetch_with_recovery(collection_day, temp_path):
                temp_path.write_text('{"measurements": []}', encoding="utf-8")
                return "200", 24, "credential_refresh"

            def import_day(temp_path):
                imports.append(temp_path.name)
                return 24, "Imported 24 readings"

            with patch.object(collect_today, "OUTFILE", output_path), \
                patch.object(collect_today, "TEMP_OUTFILE", output_path.with_suffix(".json.tmp")), \
                patch.object(collect_today, "load_collection_backfill_days", return_value=1), \
                patch.object(collect_today, "fetch_with_session_recovery", side_effect=fetch_with_recovery) as fetch_mock, \
                patch.object(collect_today, "import_day", side_effect=import_day), \
                patch.object(collect_today, "record_success"), \
                patch.object(collect_today, "generate_report"), \
                patch.object(collect_today, "log_solaredge_action"):
                summary = collect_today.collect_recent(action="scheduled_collection")

        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(imports, [f"latest-{collect_today.date.today().isoformat()}.json.tmp"])
        self.assertEqual(summary["measurements"], 24)
        self.assertEqual(summary["imported"], 24)
        self.assertEqual(summary["auth_paths"], ["credential_refresh"])
        self.assertTrue(summary["report_regenerated"])

    def test_main_runs_scheduled_collection_without_legacy_refresh_wrapper(self):
        with patch.object(collect_today, "setup_logging"), \
            patch.object(collect_today, "collect_recent") as collect_recent, \
            patch.object(collect_today, "refresh_solaredge_session") as refresh:
            collect_today.main()

        collect_recent.assert_called_once_with(action="scheduled_collection")
        refresh.assert_not_called()

    def test_missing_cookie_is_recoverable_authentication_failure(self):
        with TemporaryDirectory() as directory:
            missing_cookie = Path(directory) / "solaredge_cookie.txt"
            with patch.object(collect_today, "COOKIE_PATH", missing_cookie):
                with self.assertRaises(collect_today.AuthenticationFailure) as context:
                    collect_today.read_session_header()

        self.assertIn("cookie is missing", str(context.exception))

    def test_refreshed_cookie_is_saved_without_exposing_value(self):
        with TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "solaredge_cookie.txt"
            with patch.object(refresh_solaredge_session, "COOKIE_PATH", cookie_path):
                refresh_solaredge_session.save_cookie("SESSION=redaction-fixture-cookie")

            self.assertEqual(cookie_path.read_text(encoding="utf-8"), "SESSION=redaction-fixture-cookie\n")
            self.assertEqual(oct(cookie_path.stat().st_mode & 0o777), "0o600")

    def test_failed_refresh_without_credentials_gives_clear_guidance(self):
        error = subprocess.CalledProcessError(
            1,
            ["python3", "collector/refresh_solaredge_session.py"],
            output="refresh failed: SolarEdge credentials are not configured in environment or config/secrets.json",
            stderr="",
        )

        with patch.object(collect_today, "REFRESH_SCRIPT", PROJECT_ROOT / "collector" / "refresh_solaredge_session.py"), \
            patch("collect_today.subprocess.run", side_effect=error), \
            patch.object(collect_today.logging, "error"):
            with self.assertRaises(collect_today.CredentialsMissing) as context:
                collect_today.refresh_solaredge_session()

        self.assertIn("Credentials missing", str(context.exception))
        self.assertNotIn("config/secrets.json", str(context.exception))

    def test_solaredge_login_supports_two_step_username_then_password(self):
        page = FakeLoginPage(selectors={"input[type='email']", "button[type='submit']"})

        refresh_solaredge_session.login(page, "user@example.com", "redaction-fixture-password")

        self.assertTrue(page.username_filled)
        self.assertTrue(page.password_filled)
        self.assertTrue(page.submitted)
        self.assertGreaterEqual(page.clicks.count("button[type='submit']"), 1)

    def test_solaredge_login_clicks_initial_login_button_before_field_search_retry(self):
        page = FakeInitialLoginPage()

        refresh_solaredge_session.login(page, "user@example.com", "redaction-fixture-password")

        self.assertIn("#se-signing-mfe button:has-text('Log in')", page.clicks)
        self.assertTrue(page.username_filled)
        self.assertTrue(page.password_filled)
        self.assertTrue(page.submitted)
        self.assertEqual(page.url, "https://monitoring.solaredge.com/solaredge-web/p/login")

    def test_solaredge_login_candidate_buttons_explain_skip_when_not_clicked(self):
        with TemporaryDirectory() as directory:
            debug_dir = Path(directory) / "debug"
            page = FakeHiddenInitialLoginPage()
            with patch.object(refresh_solaredge_session, "DEBUG_DIR", debug_dir):
                with self.assertRaises(refresh_solaredge_session.RefreshError) as context:
                    refresh_solaredge_session.login(page, "user@example.com", "redaction-fixture-password")

            message = str(context.exception)
            debug_files = list(debug_dir.glob("solaredge-login-*.json"))
            metadata = json.loads(debug_files[0].read_text(encoding="utf-8"))

        self.assertIn("initial_login_button_found=True", message)
        self.assertIn("initial_login_button_clicked=False", message)
        self.assertIn("initial_login_button_visible_candidates=0", message)
        self.assertTrue(metadata["initial_login_button_found"])
        self.assertFalse(metadata["initial_login_button_clicked"])
        self.assertGreater(metadata["initial_login_button_candidates"], 0)
        self.assertEqual(metadata["initial_login_button_visible_candidates"], 0)
        self.assertTrue(any("not_visible" in " ".join(item["skipped"]) for item in metadata["initial_login_candidate_details"]))

    def test_solaredge_login_failure_reports_mfe_and_button_candidate_counts(self):
        with TemporaryDirectory() as directory:
            debug_dir = Path(directory) / "debug"
            page = FakeLoginPage(selectors={"div#se-signing-mfe", "#se-signing-mfe"})
            with patch.object(refresh_solaredge_session, "DEBUG_DIR", debug_dir):
                with self.assertRaises(refresh_solaredge_session.RefreshError) as context:
                    refresh_solaredge_session.login(page, "user@example.com", "redaction-fixture-password")

            message = str(context.exception)
            debug_files = list(debug_dir.glob("solaredge-login-*.json"))
            metadata = json.loads(debug_files[0].read_text(encoding="utf-8"))

        self.assertIn("signing_mfe_found=True", message)
        self.assertIn("initial_login_button_candidates=0", message)
        self.assertTrue(metadata["signing_mfe_found"])
        self.assertEqual(metadata["initial_login_button_candidates"], 0)

    def test_solaredge_login_failure_reports_safe_page_diagnostics(self):
        with TemporaryDirectory() as directory:
            debug_dir = Path(directory) / "debug"
            page = FakeLoginPage(selectors=set(), challenge=True)
            with patch.object(refresh_solaredge_session, "DEBUG_DIR", debug_dir):
                with self.assertRaises(refresh_solaredge_session.RefreshError) as context:
                    refresh_solaredge_session.login(page, "user@example.com", "redaction-fixture-password")

            message = str(context.exception)
            debug_files = list(debug_dir.glob("solaredge-login-*.json"))
            metadata = json.loads(debug_files[0].read_text(encoding="utf-8"))

        self.assertIn("title='SolarEdge Login'", message)
        self.assertIn("hostname='monitoring.solaredge.com'", message)
        self.assertIn("username_field_found=False", message)
        self.assertIn("password_field_found=False", message)
        self.assertIn("challenge_or_sso_likely=True", message)
        self.assertIn("initial_login_button_found=False", message)
        self.assertIn("initial_login_button_clicked=False", message)
        self.assertIn("signing_mfe_found=False", message)
        self.assertIn("initial_login_button_candidates=0", message)
        self.assertIn("field_search_attempts=", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn("redaction-fixture-password", message)
        self.assertEqual(len(debug_files), 1)
        self.assertEqual(metadata["hostname"], "monitoring.solaredge.com")
        self.assertFalse(metadata["username_field_found"])
        self.assertFalse(metadata["password_field_found"])
        self.assertTrue(metadata["challenge_or_sso_likely"])
        self.assertFalse(metadata["initial_login_button_found"])
        self.assertFalse(metadata["initial_login_button_clicked"])
        self.assertFalse(metadata["signing_mfe_found"])
        self.assertEqual(metadata["initial_login_button_candidates"], 0)
        self.assertEqual(metadata["url_before_initial_login_click"], "https://monitoring.solaredge.com/mfe/auth/")
        self.assertEqual(metadata["url_after_initial_login_click"], "https://monitoring.solaredge.com/mfe/auth/")
        self.assertGreaterEqual(metadata["field_search_attempts"], 1)
        self.assertNotIn("user@example.com", json.dumps(metadata))
        self.assertNotIn("redaction-fixture-password", json.dumps(metadata))

    def test_solaredge_post_login_detects_sites_list_and_uses_configured_site_id(self):
        page = FakeSitePage(site_id="4457399", direct_navigation=True)

        metadata = refresh_solaredge_session.open_site_dashboard(page, "4457399")

        self.assertTrue(metadata["sites_list_detected"])
        self.assertTrue(metadata["configured_site_id_used"])
        self.assertFalse(metadata["site_row_clicked"])
        self.assertTrue(metadata["dashboard_loaded"])
        self.assertIn("4457399", page.url)

    def test_solaredge_post_login_clicks_first_site_when_direct_navigation_fails(self):
        page = FakeSitePage(site_id="4457399", direct_navigation=False, has_site_row=True)

        metadata = refresh_solaredge_session.open_site_dashboard(page, "4457399")

        self.assertTrue(metadata["sites_list_detected"])
        self.assertTrue(metadata["configured_site_id_used"])
        self.assertTrue(metadata["site_row_clicked"])
        self.assertTrue(metadata["dashboard_loaded"])
        self.assertIn("#/dashboard", page.url)

    def test_solaredge_post_login_fails_when_no_site_can_be_opened(self):
        with TemporaryDirectory() as directory:
            debug_dir = Path(directory) / "debug"
            page = FakeSitePage(site_id="4457399", direct_navigation=False, has_site_row=False)
            with patch.object(refresh_solaredge_session, "DEBUG_DIR", debug_dir):
                with self.assertRaises(refresh_solaredge_session.RefreshError) as context:
                    refresh_solaredge_session.open_site_dashboard(page, "4457399")

            message = str(context.exception)
            debug_files = list(debug_dir.glob("solaredge-login-*.json"))

        self.assertIn("SolarEdge site dashboard could not be opened", message)
        self.assertIn("sites_list_detected=True", message)
        self.assertIn("site_row_clicked=False", message)
        self.assertIn("dashboard_loaded=False", message)
        self.assertEqual(len(debug_files), 1)

    def test_solaredge_data_endpoint_verification_requires_dashboard_first(self):
        calls = []
        page = FakeSitePage(site_id="4457399", direct_navigation=False, has_site_row=True)

        refresh_solaredge_session.open_site_dashboard(page, "4457399")

        def expect_response(predicate, timeout=None):
            calls.append(("expect_response", page.dashboard_opened))

            class Manager:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return Manager()

        page.expect_response = expect_response
        refresh_solaredge_session.verify_data_endpoint(page, "4457399")

        self.assertEqual(calls, [("expect_response", True)])

    def test_safe_error_message_redacts_credentials(self):
        message = collect_today.safe_error_message(
            RuntimeError("login failed for user@example.com with password=redaction-fixture-password Cookie: SESSION=redaction-fixture-cookie")
        )

        self.assertIn("REDACTED_EMAIL", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn("redaction-fixture-password", message)
        self.assertNotIn("redaction-fixture-cookie", message)

    def test_solaredge_fetch_helper_refreshes_session_before_retry(self):
        with TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "solaredge_cookie.txt"
            output_path = Path(directory) / "latest.json.tmp"

            def refresh():
                cookie_path.write_text("SESSION=fresh\n", encoding="utf-8")

            with patch.object(collect_today, "COOKIE_PATH", cookie_path), \
                patch.object(collect_today, "refresh_solaredge_session", side_effect=refresh) as refresh_mock, \
                patch.object(collect_today, "fetch_solaredge", return_value="200") as fetch_mock, \
                patch.object(collect_today, "validate_response", return_value=24):
                status, measurements, auth_path = collect_today.fetch_with_session_recovery(collect_today.date.today(), output_path)

        self.assertEqual(status, "200")
        self.assertEqual(measurements, 24)
        self.assertEqual(auth_path, "credential_refresh")
        refresh_mock.assert_called_once()
        self.assertEqual(fetch_mock.call_args.args[0], "SESSION=fresh")

    def test_solaredge_test_connection_uses_shared_auth_session_helper(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "solaredge-runtime-test.json.tmp"

            with patch.object(collect_today, "TEMP_OUTFILE", output_path), \
                patch.object(
                    collect_today,
                    "fetch_with_session_recovery",
                    return_value=("200", 24, "existing_session"),
                ) as fetch_helper:
                result = collect_today.SolarEdgeAdapter().test_connection()

        self.assertTrue(result["success"])
        self.assertEqual(result["auth_path"], "existing_session")
        fetch_helper.assert_called_once()

    def test_solaredge_reconnect_uses_collect_recent_without_legacy_refresh(self):
        with patch.object(
            collect_today,
            "collect_recent",
            return_value={
                "succeeded": 1,
                "failed": 0,
                "measurements": 24,
                "imported": 24,
                "auth_paths": ["credential_refresh"],
                "report_regenerated": True,
            },
        ) as collect_recent, \
            patch.object(collect_today, "refresh_solaredge_session") as refresh:
            result = collect_today.SolarEdgeAdapter().reconnect()

        self.assertTrue(result["success"])
        self.assertEqual(result["measurements"], 24)
        self.assertEqual(result["imported"], 24)
        self.assertEqual(result["auth_path"], "credential_refresh")
        collect_recent.assert_called_once_with(action="reconnect")
        refresh.assert_not_called()

    def test_solaredge_reconnect_failure_identifies_stage(self):
        with patch.object(
            collect_today,
            "collect_recent",
            side_effect=collect_today.CollectionBackfillFailure("No usable days imported"),
        ):
            data_failure = collect_today.SolarEdgeAdapter().reconnect()

        with patch.object(
            collect_today,
            "collect_recent",
            side_effect=collect_today.SessionRefreshFailure("Automated SolarEdge session refresh failed"),
        ):
            session_failure = collect_today.SolarEdgeAdapter().reconnect()

        self.assertFalse(data_failure["success"])
        self.assertEqual(data_failure["http_status"], "data_retrieval_failed")
        self.assertIn("Data retrieval failed", data_failure["warnings"][0])
        self.assertFalse(session_failure["success"])
        self.assertEqual(session_failure["http_status"], "session_refresh_failed")
        self.assertIn("Session refresh failed", session_failure["warnings"][0])

    def test_missing_credentials_are_not_downgraded_to_no_recent_data(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "latest.json.tmp"

            with patch.object(
                collect_today,
                "fetch_with_session_recovery",
                side_effect=collect_today.CredentialsMissing("Credentials missing."),
            ):
                with self.assertRaises(collect_today.CredentialsMissing):
                    collect_today.fetch_usable_sample(output_path)

    def test_solaredge_test_connection_warns_when_signed_in_but_no_data(self):
        with TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "solaredge_cookie.txt"
            cookie_path.write_text("SESSION=fresh\n", encoding="utf-8")

            with patch.object(collect_today, "COOKIE_PATH", cookie_path), \
                patch.object(collect_today, "fetch_solaredge", return_value="200"), \
                patch.object(collect_today, "validate_response", side_effect=ValueError("SolarEdge response has no measurements")):
                result = collect_today.SolarEdgeAdapter().test_connection()

        self.assertFalse(result["success"])
        self.assertEqual(result["authentication_status"], "authenticated")
        self.assertEqual(result["http_status"], "no_recent_data")
        self.assertIn("Signed in, but no recent solar data was received", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
