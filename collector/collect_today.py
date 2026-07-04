#!/usr/bin/env python3
import json
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector_health import (
    maybe_send_failure_notification,
    record_failure,
    record_report_failure,
    record_report_success,
    record_success,
)
from engine.provider_framework import (
    ProviderCapability,
    ProviderFreshness,
    ProviderObservation,
    ProviderObservationQuality,
    ProviderObservationType,
    ProviderType,
)
from engine.provider_runtime import ProviderAuthentication, ProviderSchedule

COOKIE_PATH = PROJECT_ROOT / "collector" / "solaredge_cookie.txt"
OUTFILE = PROJECT_ROOT / "data" / "latest.json"
TEMP_OUTFILE = OUTFILE.with_suffix(".json.tmp")
LOG_PATH = PROJECT_ROOT / "logs" / "collect.log"
SITE_ID = os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID", "4457399")
REFRESH_SCRIPT = PROJECT_ROOT / "collector" / "refresh_solaredge_session.py"


class AuthenticationFailure(RuntimeError):
    pass


class ReportGenerationFailure(RuntimeError):
    pass


class CollectionBackfillFailure(RuntimeError):
    pass


class SessionRefreshFailure(RuntimeError):
    pass


class CredentialsMissing(SessionRefreshFailure):
    pass


class SignInFailed(SessionRefreshFailure):
    pass


class SolarEdgeUnavailable(SessionRefreshFailure):
    pass


class NoRecentSolarData(RuntimeError):
    pass


def log_solaredge_action(action: str, **details) -> None:
    safe_details = {
        str(key): redact_sensitive(str(value))
        for key, value in details.items()
        if value is not None
    }
    logging.info("SolarEdge provider action=%s details=%s", action, safe_details)


SECRET_PATTERNS = (
    (re.compile(r"(Cookie:\s*)[^\n\r]+", re.IGNORECASE), r"\1REDACTED"),
    (re.compile(r"(Authorization:\s*)[^\n\r]+", re.IGNORECASE), r"\1REDACTED"),
    (re.compile(r"[A-Za-z0-9_.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "REDACTED_EMAIL"),
    (re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE), r"\1REDACTED"),
    (re.compile(r"(token\s*[=:]\s*)\S+", re.IGNORECASE), r"\1REDACTED"),
)


class SolarEdgeAdapter:
    """Reference provider adapter for the existing SolarEdge collector."""

    provider_id = "solaredge"
    schedule = ProviderSchedule.EVERY_5_MINUTES

    def authenticate(self) -> ProviderAuthentication:
        try:
            read_session_header()
        except AuthenticationFailure as exc:
            return ProviderAuthentication(
                authenticated=False,
                refresh_required=True,
                method="cookie",
                message=safe_error_message(exc),
            )
        return ProviderAuthentication(authenticated=True, method="cookie")

    def refresh_session(self) -> ProviderAuthentication:
        try:
            refresh_solaredge_session()
            read_session_header()
        except (SessionRefreshFailure, AuthenticationFailure) as exc:
            return ProviderAuthentication(
                authenticated=False,
                refresh_required=True,
                method="cookie",
                message=safe_error_message(exc),
            )
        return ProviderAuthentication(authenticated=True, method="cookie")

    def test_connection(self) -> dict:
        output_path = TEMP_OUTFILE.with_name("solaredge-runtime-test.json.tmp")
        output_path.unlink(missing_ok=True)
        started = datetime.now().astimezone()
        sample_day = None
        auth_path = None
        try:
            status, measurements, sample_day, auth_path = fetch_usable_sample(output_path)
            success = True
            auth_status = "authenticated"
            warning = None
        except NoRecentSolarData as exc:
            status = "no_recent_data"
            measurements = 0
            success = False
            auth_status = "authenticated"
            warning = safe_error_message(exc)
        except Exception as exc:
            status = "failed"
            measurements = 0
            success = False
            auth_status = "not_authenticated"
            warning = safe_error_message(exc)
        finally:
            output_path.unlink(missing_ok=True)
        duration_ms = int((datetime.now().astimezone() - started).total_seconds() * 1000)
        return {
            "success": success,
            "authentication_status": auth_status,
            "provider_version": "solaredge-monitoring-local",
            "available_datasets": ["interval_history", "daily_summary"] if success else [],
            "warnings": [warning] if warning else [],
            "response_time_ms": duration_ms,
            "measurements": measurements,
            "http_status": status,
            "sample_date": sample_day.isoformat() if sample_day else None,
            "data_retrieved": bool(measurements),
            "auth_path": auth_path,
            "data_fetch_attempted": True,
        }

    def reconnect(self) -> dict:
        started = datetime.now().astimezone()
        summary = {}
        try:
            summary = collect_recent(action="reconnect")
            success = True
            warning = None
            status = "collection_complete"
        except CredentialsMissing as exc:
            success = False
            warning = f"Credentials missing. {safe_error_message(exc)}"
            status = "credentials_missing"
        except SignInFailed as exc:
            success = False
            warning = f"Login failed. {safe_error_message(exc)}"
            status = "login_failed"
        except SolarEdgeUnavailable as exc:
            success = False
            warning = f"Session refresh failed. {safe_error_message(exc)}"
            status = "session_refresh_failed"
        except SessionRefreshFailure as exc:
            success = False
            warning = f"Session refresh failed. {safe_error_message(exc)}"
            status = "session_refresh_failed"
        except NoRecentSolarData as exc:
            success = False
            warning = f"Data retrieval failed. {safe_error_message(exc)}"
            status = "data_retrieval_failed"
        except CollectionBackfillFailure as exc:
            success = False
            warning = f"Data retrieval failed. {safe_error_message(exc)}"
            status = "data_retrieval_failed"
        except ReportGenerationFailure as exc:
            success = False
            warning = f"Report regeneration failed. {safe_error_message(exc)}"
            status = "report_regeneration_failed"
        except subprocess.SubprocessError as exc:
            success = False
            warning = f"Import failed. {safe_error_message(exc)}"
            status = "import_failed"
        except Exception as exc:
            success = False
            warning = safe_error_message(exc)
            status = "failed"
        duration_ms = int((datetime.now().astimezone() - started).total_seconds() * 1000)
        measurements = int(summary.get("measurements") or 0)
        imported = int(summary.get("imported") or 0)
        return {
            "success": success,
            "authentication_status": "authenticated" if success else "not_authenticated",
            "provider_version": "solaredge-monitoring-local",
            "available_datasets": ["interval_history", "daily_summary"] if success else [],
            "warnings": [warning] if warning else [],
            "response_time_ms": duration_ms,
            "measurements": measurements,
            "imported": imported,
            "imported_or_updated": int(summary.get("imported_or_updated") or imported),
            "days_checked": int(summary.get("days_checked") or 0),
            "days_fetched": int(summary.get("days_fetched") or 0),
            "requested_start_date": summary.get("requested_start_date"),
            "requested_end_date": summary.get("requested_end_date"),
            "duplicates_skipped": int(summary.get("duplicates_skipped") or 0),
            "data_received": int(summary.get("data_received") or 0),
            "http_status": status,
            "data_retrieved": success and measurements > 0,
            "report_regenerated": bool(summary.get("report_regenerated")),
            "auth_path": ", ".join(summary.get("auth_paths") or []),
            "data_fetch_attempted": True,
            "import_attempted": True,
        }

    def discover_capabilities(self) -> list[ProviderCapability]:
        return [ProviderCapability.INTERVAL_HISTORY, ProviderCapability.DAILY_SUMMARY]

    def collect_observations(self) -> list[ProviderObservation]:
        collect_recent()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        return [
            ProviderObservation(
                capability=ProviderCapability.DAILY_SUMMARY,
                name="solaredge_collection_complete",
                value={"status": "collection_complete"},
                provider_id=self.provider_id,
                provider_type=ProviderType.SOLAR,
                observation_type=ProviderObservationType.COLLECTOR_HEALTH,
                observed_at=now,
                freshness=ProviderFreshness(state="fresh", observed_at=now, age_minutes=0.0),
                quality=ProviderObservationQuality.HIGH,
                provenance="SolarEdgeAdapter",
            )
        ]

    def collect_live_state(self) -> dict:
        return {"live_power": None, "message": "Live state is not implemented for the legacy SolarEdge endpoint."}

    def collect_diagnostics(self) -> dict:
        return {
            "cookie_configured": COOKIE_PATH.exists() and bool(COOKIE_PATH.read_text(encoding="utf-8").strip()),
            "site_id_configured": bool(SITE_ID),
            "schedule": self.schedule.value,
            "collector": "collector/collect_today.py",
        }


def safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        http_status = (exc.stdout or "").strip()
        if http_status:
            return f"command failed with HTTP/status {http_status}"
        return f"command failed with exit code {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "command timed out"
    return redact_sensitive(str(exc))


def redact_sensitive(message: str) -> str:
    redacted = str(message or "")
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_collection_backfill_days() -> int:
    settings_path = PROJECT_ROOT / "config" / "settings.json"
    default = 4
    if not settings_path.exists():
        return default
    try:
        with settings_path.open("r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read config/settings.json; using default backfill window")
        return default
    try:
        days = int(settings.get("collection_backfill_days", default))
    except (TypeError, ValueError):
        logging.warning("Invalid collection_backfill_days; using default backfill window")
        days = default
    return max(4, min(days, 7))


def validate_response(path: Path, expected_day: date) -> int:
    try:
        with path.open("r", encoding="utf-8") as response:
            data = json.load(response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
        if any(marker in preview for marker in ("login", "sign in", "unauthorized", "forbidden")):
            raise AuthenticationFailure("SolarEdge authentication appears to have expired") from exc
        raise ValueError("SolarEdge response was not valid JSON") from exc

    if not isinstance(data, dict):
        raise ValueError("SolarEdge response must be a JSON object")

    measurements = data.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise ValueError("SolarEdge response has no measurements")

    matching_day = 0
    usable_measurements = 0
    for index, measurement in enumerate(measurements):
        if not isinstance(measurement, dict):
            raise ValueError(f"SolarEdge measurement {index} is not an object")

        timestamp = measurement.get("measurementTime")
        if not isinstance(timestamp, str):
            raise ValueError(f"SolarEdge measurement {index} has no valid measurementTime")
        try:
            measurement_time = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError(f"SolarEdge measurement {index} has an invalid measurementTime") from exc

        if measurement_time.date() == expected_day:
            matching_day += 1
        if any(measurement.get(key) is not None for key in ("production", "consumption", "import", "export")):
            usable_measurements += 1

    if not matching_day:
        raise ValueError(f"SolarEdge response has no measurements for {expected_day}")
    if not usable_measurements:
        raise ValueError("SolarEdge response contains no usable power values")
    return len(measurements)


def fetch_with_session_recovery(current_day: date, output_path: Path) -> tuple[str, int, str]:
    auth_path = "existing_session"
    try:
        session_header = read_session_header()
        status = fetch_solaredge(session_header, current_day, output_path)
        measurements = validate_response(output_path, current_day)
        return status, measurements, auth_path
    except AuthenticationFailure as exc:
        log_solaredge_action(
            "fetch",
            stage="authentication_recovery",
            auth_path="saved_credentials",
            data_fetch_attempted=True,
            result=safe_error_message(exc),
        )
        output_path.unlink(missing_ok=True)

    refresh_solaredge_session()
    session_header = read_session_header()
    auth_path = "credential_refresh"
    status = fetch_solaredge(session_header, current_day, output_path)
    measurements = validate_response(output_path, current_day)
    return status, measurements, auth_path


def fetch_usable_sample(output_path: Path, days: int = 7) -> tuple[str, int, date, str]:
    current_day = date.today()
    last_error = "No recent SolarEdge data was received."
    for offset in range(max(1, days)):
        sample_day = current_day - timedelta(days=offset)
        output_path.unlink(missing_ok=True)
        try:
            status, measurements, auth_path = fetch_with_session_recovery(sample_day, output_path)
            log_solaredge_action(
                "test_connection",
                auth_path=auth_path,
                data_fetch_attempted=True,
                result="usable_sample",
                measurements=measurements,
                sample_date=sample_day,
            )
            return status, measurements, sample_day, auth_path
        except AuthenticationFailure:
            raise
        except SessionRefreshFailure:
            raise
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            last_error = safe_error_message(exc)
            continue
        finally:
            output_path.unlink(missing_ok=True)
    raise NoRecentSolarData(f"Signed in, but no recent solar data was received. {last_error}")


def build_url(today: str) -> str:
    return (
        f"https://monitoring.solaredge.com/services/dashboard/power/sites/{SITE_ID}"
        f"?chart-time-unit=quarter-hours"
        f"&start-date={today}"
        f"&end-date={today}"
        "&measurement-types="
        "production%2Cconsumption%2C"
        "consumption-distribution-without-storage%2C"
        "production-distribution-without-storage%2C"
        "storage-charge-level"
    )


def fetch_solaredge(session_header: str, current_day: date, output_path: Path) -> str:
    curl_command = [
        "curl",
        "--http1.1",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--write-out",
        "%{http_code}",
        "-L",
        "--connect-timeout",
        "20",
        "--max-time",
        "90",
        "-H",
        "Accept: application/json, */*",
        "-H",
        "Referer: https://monitoring.solaredge.com/one",
        "-H",
        "User-Agent: Mozilla/5.0",
        "-H",
        "Cookie" + ": " + session_header,
        build_url(current_day.isoformat()),
        "-o",
        str(output_path),
    ]
    try:
        downloaded = subprocess.run(
            curl_command,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        http_status = (exc.stdout or "").strip()
        if http_status in ("401", "403"):
            raise AuthenticationFailure(f"SolarEdge returned HTTP {http_status}") from exc
        status_message = f"SolarEdge request failed for {current_day}: {safe_error_message(exc)}"
        raise RuntimeError(status_message) from exc
    http_status = downloaded.stdout.strip()
    if http_status in ("401", "403"):
        raise AuthenticationFailure(f"SolarEdge returned HTTP {http_status}")
    return http_status


def import_day(json_path: Path) -> tuple[int, str]:
    imported = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "collector" / "import_solaredge.py"), str(json_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    importer_message = imported.stdout.strip()
    match = re.search(r"Imported\s+(\d+)\s+readings", importer_message)
    imported_count = int(match.group(1)) if match else 0
    return imported_count, importer_message


def parse_import_summary(importer_message: str) -> dict[str, int]:
    summary = {
        "data_received": 0,
        "imported_or_updated": 0,
        "duplicates_skipped": 0,
        "observations_promoted": 0,
    }
    patterns = {
        "data_received": r"data received\s+(\d+)",
        "imported_or_updated": r"records imported\s+(\d+)",
        "duplicates_skipped": r"duplicates ignored\s+(\d+)",
        "observations_promoted": r"observations promoted\s+(\d+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, importer_message or "", re.IGNORECASE)
        if match:
            summary[key] = int(match.group(1))
    if not summary["imported_or_updated"]:
        match = re.search(r"Imported\s+(\d+)\s+readings", importer_message or "", re.IGNORECASE)
        if match:
            summary["imported_or_updated"] = int(match.group(1))
    return summary


def generate_report() -> None:
    try:
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "collector" / "report.py")],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.SubprocessError as exc:
        message = "Collection and import succeeded, but dashboard report generation failed"
        record_report_failure(message)
        logging.error("%s", message)
        raise ReportGenerationFailure(message) from exc
    record_report_success()
    logging.info("Dashboard report generation succeeded")


def read_session_header() -> str:
    if not COOKIE_PATH.exists():
        raise AuthenticationFailure("SolarEdge cookie is missing")

    session_header = COOKIE_PATH.read_text(encoding="utf-8").strip()
    if not session_header:
        raise AuthenticationFailure("SolarEdge cookie is empty")
    return session_header


def refresh_solaredge_session() -> None:
    if not REFRESH_SCRIPT.exists():
        raise SessionRefreshFailure("SolarEdge refresh script is missing")
    logging.info("Attempting automated SolarEdge session refresh")
    try:
        refreshed = subprocess.run(
            [sys.executable, str(REFRESH_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        logging.error("Automated SolarEdge session refresh timed out")
        raise SolarEdgeUnavailable("Network unavailable. SolarEdge sign-in timed out.") from exc
    except subprocess.SubprocessError as exc:
        output = redact_sensitive(" ".join([str(getattr(exc, "stdout", "") or ""), str(getattr(exc, "stderr", "") or "")]).strip())
        lowered = output.lower()
        logging.error("Automated SolarEdge session refresh failed: %s", output or "no output")
        if "credentials are not configured" in lowered or "credentials missing" in lowered or "not configured" in lowered:
            raise CredentialsMissing(
                "Credentials missing. Add SolarEdge username/email and password, or refresh the cookie manually."
            ) from exc
        if any(marker in lowered for marker in ("login failed", "sign-in failed", "login fields", "no solaredge cookies", "authentication")):
            raise SignInFailed("Sign-in failed. Check the saved SolarEdge username/email and password.") from exc
        if any(marker in lowered for marker in ("network", "timeout", "unavailable", "dns", "connection")):
            raise SolarEdgeUnavailable("SolarEdge unavailable. Check the network and try again.") from exc
        raise SessionRefreshFailure("Automated SolarEdge session refresh failed") from exc
    safe_output = redact_sensitive(" ".join(refreshed.stdout.split()))
    if safe_output:
        logging.info("Automated SolarEdge session refresh result: %s", safe_output)


def collect_recent(action: str = "scheduled_collection") -> dict:
    current_day = date.today()
    backfill_days = load_collection_backfill_days()
    start_day = current_day - timedelta(days=backfill_days - 1)
    days_to_fetch = [start_day + timedelta(days=offset) for offset in range(backfill_days)]
    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    TEMP_OUTFILE.unlink(missing_ok=True)

    logging.info(
        "Starting SolarEdge recent collection for %s to %s (%d days)",
        start_day,
        current_day,
        backfill_days,
    )

    succeeded = 0
    failed = 0
    total_measurements = 0
    total_imported = 0
    total_received = 0
    total_duplicates = 0
    failures = []
    auth_paths = set()
    day_summaries = []

    for collection_day in days_to_fetch:
        temp_outfile = OUTFILE.with_name(f"latest-{collection_day.isoformat()}.json.tmp")
        temp_outfile.unlink(missing_ok=True)
        log_solaredge_action(action, stage="fetch", data_fetch_attempted=True, day=collection_day)
        try:
            _status, measurement_count, auth_path = fetch_with_session_recovery(collection_day, temp_outfile)
            auth_paths.add(auth_path)
            log_solaredge_action(
                action,
                stage="import",
                auth_path=auth_path,
                data_fetch_attempted=True,
                import_attempted=True,
                requested_date_range=f"{start_day} to {current_day}",
                measurements=measurement_count,
                day=collection_day,
            )
            imported_count, importer_message = import_day(temp_outfile)
            import_summary = parse_import_summary(importer_message)
            temp_outfile.replace(OUTFILE)
        except AuthenticationFailure:
            temp_outfile.unlink(missing_ok=True)
            raise
        except SessionRefreshFailure:
            temp_outfile.unlink(missing_ok=True)
            raise
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
            failed += 1
            safe_message = safe_error_message(exc)
            failures.append(f"{collection_day}: {safe_message}")
            logging.error("Collection failed for %s; continuing: %s", collection_day, safe_message)
            temp_outfile.unlink(missing_ok=True)
            continue

        succeeded += 1
        total_measurements += measurement_count
        imported_or_updated = import_summary["imported_or_updated"] or imported_count
        total_imported += imported_or_updated
        total_received += import_summary["data_received"] or measurement_count
        total_duplicates += import_summary["duplicates_skipped"]
        day_summaries.append(
            {
                "date": collection_day.isoformat(),
                "measurements_returned": measurement_count,
                "data_received": import_summary["data_received"] or measurement_count,
                "imported_or_updated": imported_or_updated,
                "duplicates_skipped": import_summary["duplicates_skipped"],
            }
        )
        logging.info(
            "Collection succeeded for %s with %d measurements, %d imported/updated, %d duplicates skipped; importer: %s",
            collection_day,
            measurement_count,
            imported_or_updated,
            import_summary["duplicates_skipped"],
            importer_message or "no output",
        )
        if importer_message:
            print(importer_message)

    logging.info(
        "SolarEdge recent collection finished: range %s to %s; %d/%d days succeeded, %d failed, %d measurements validated, %d readings imported/updated, %d duplicates skipped",
        start_day,
        current_day,
        succeeded,
        backfill_days,
        failed,
        total_measurements,
        total_imported,
        total_duplicates,
    )

    if not succeeded:
        detail = "; ".join(failures[:3]) if failures else "No days imported"
        raise CollectionBackfillFailure(
            f"SolarEdge recent collection failed for all {backfill_days} days. {detail}"
        )

    message = None
    if failed:
        message = f"Partial collection: {failed}/{backfill_days} days failed; {succeeded}/{backfill_days} days imported"
    record_success(total_measurements, message)
    print(
        f"Collection complete: requested {start_day} to {current_day}; "
        f"fetched {succeeded}/{backfill_days} days; "
        f"validated {total_measurements} measurements; "
        f"imported/updated {total_imported}; "
        f"skipped duplicates {total_duplicates}"
    )
    if failed:
        print(f"Partial collection: {failed}/{backfill_days} days failed; see {LOG_PATH}")

    generate_report()
    print("Dashboard report regenerated")
    log_solaredge_action(
        action,
        stage="complete",
        auth_path=",".join(sorted(auth_paths)),
        data_fetch_attempted=True,
        import_attempted=True,
        report_regeneration_attempted=True,
        result="success",
        requested_date_range=f"{start_day} to {current_day}",
        days_checked=backfill_days,
        days_fetched=succeeded,
        measurements=total_measurements,
        imported_or_updated=total_imported,
        duplicates_skipped=total_duplicates,
    )
    return {
        "succeeded": succeeded,
        "failed": failed,
        "days_checked": backfill_days,
        "days_fetched": succeeded,
        "requested_start_date": start_day.isoformat(),
        "requested_end_date": current_day.isoformat(),
        "measurements": total_measurements,
        "imported": total_imported,
        "imported_or_updated": total_imported,
        "data_received": total_received,
        "duplicates_skipped": total_duplicates,
        "day_summaries": day_summaries,
        "auth_paths": sorted(auth_paths),
        "report_regenerated": True,
    }


def main() -> None:
    setup_logging()
    collect_recent(action="scheduled_collection")


def handle_failure(failure_type: str, message: str) -> None:
    safe_message = redact_sensitive(message)
    state = record_failure(failure_type, safe_message)
    logging.error("Collection failed [%s]: %s", failure_type, safe_message)
    try:
        if maybe_send_failure_notification(state):
            logging.info("Sent collector failure notification")
    except Exception as exc:
        logging.error("Failed to send collector failure notification: %s", exc)
    print(f"Collection failed; see {LOG_PATH}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except CredentialsMissing as exc:
        handle_failure("Credentials missing", str(exc))
        raise SystemExit(1) from None
    except SignInFailed as exc:
        handle_failure("Sign-in failed", str(exc))
        raise SystemExit(1) from None
    except SolarEdgeUnavailable as exc:
        handle_failure("SolarEdge unavailable", str(exc))
        raise SystemExit(1) from None
    except AuthenticationFailure as exc:
        handle_failure("Cookie expired", str(exc))
        raise SystemExit(1) from None
    except SessionRefreshFailure as exc:
        handle_failure("SolarEdge unavailable", str(exc))
        raise SystemExit(1) from None
    except ReportGenerationFailure as exc:
        print(f"{exc}; see {LOG_PATH}", file=sys.stderr)
        raise SystemExit(1) from None
    except CollectionBackfillFailure as exc:
        handle_failure("collector failure", str(exc))
        raise SystemExit(1) from None
    except subprocess.SubprocessError:
        handle_failure("collector failure", "A collection command failed")
        raise SystemExit(1) from None
    except Exception as exc:
        handle_failure("collector failure", str(exc))
        raise SystemExit(1) from None
