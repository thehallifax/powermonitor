import subprocess
import sys
import unittest
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


class SolarEdgeRecoveryTests(unittest.TestCase):
    def test_expired_cookie_triggers_refresh_and_collection_retry(self):
        attempts = []

        def collect_recent():
            attempts.append("collect")
            if len(attempts) == 1:
                raise collect_today.AuthenticationFailure("SolarEdge returned HTTP 403")

        with patch.object(collect_today, "setup_logging"), \
            patch.object(collect_today, "collect_recent", side_effect=collect_recent), \
            patch.object(collect_today, "refresh_solaredge_session") as refresh:
            collect_today.main()

        self.assertEqual(attempts, ["collect", "collect"])
        refresh.assert_called_once()

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

    def test_safe_error_message_redacts_credentials(self):
        message = collect_today.safe_error_message(
            RuntimeError("login failed for user@example.com with password=redaction-fixture-password Cookie: SESSION=redaction-fixture-cookie")
        )

        self.assertIn("REDACTED_EMAIL", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn("redaction-fixture-password", message)
        self.assertNotIn("redaction-fixture-cookie", message)

    def test_solaredge_test_connection_refreshes_session_before_retry(self):
        with TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "solaredge_cookie.txt"

            def refresh():
                cookie_path.write_text("SESSION=fresh\n", encoding="utf-8")

            with patch.object(collect_today, "COOKIE_PATH", cookie_path), \
                patch.object(collect_today, "refresh_solaredge_session", side_effect=refresh) as refresh_mock, \
                patch.object(collect_today, "fetch_solaredge", return_value="200") as fetch_mock, \
                patch.object(collect_today, "validate_response", return_value=24):
                result = collect_today.SolarEdgeAdapter().test_connection()

        self.assertTrue(result["success"])
        refresh_mock.assert_called_once()
        self.assertEqual(fetch_mock.call_args.args[0], "SESSION=fresh")


if __name__ == "__main__":
    unittest.main()
