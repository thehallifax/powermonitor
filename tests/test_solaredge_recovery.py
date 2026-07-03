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
