#!/usr/bin/env python3
import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "data" / "collector_health.json"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"

DEFAULT_EMAIL_SETTINGS = {
    "enabled": False,
    "failure_after_hours": 24,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password_env": "POWERMONITOR_SMTP_PASSWORD",
    "from_address": "",
    "to_address": "",
    "use_starttls": True,
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def record_success(measurement_count: int, message: Optional[str] = None) -> dict:
    timestamp = now_iso()
    state = load_state()
    state.update(
        {
            "status": "ok",
            "failure_type": None,
            "message": message,
            "last_attempt_at": timestamp,
            "last_success_at": timestamp,
            "last_failure_at": None,
            "failure_started_at": None,
            "consecutive_failures": 0,
            "notification_sent_at": None,
            "last_measurement_count": measurement_count,
        }
    )
    write_state(state)
    return state


def record_report_success() -> dict:
    timestamp = now_iso()
    state = load_state()
    if state.get("status") == "report generation failure":
        state.update({"status": "ok", "failure_type": None, "message": None})
    state.update(
        {
            "last_successful_report_generation": timestamp,
            "last_report_failure_at": None,
        }
    )
    write_state(state)
    return state


def record_report_failure(message: str) -> dict:
    timestamp = now_iso()
    state = load_state()
    state.update(
        {
            "status": "report generation failure",
            "failure_type": "report generation failure",
            "message": message,
            "last_report_failure_at": timestamp,
        }
    )
    write_state(state)
    return state


def record_failure(failure_type: str, message: str) -> dict:
    timestamp = now_iso()
    state = load_state()
    failure_started_at = state.get("failure_started_at")
    if state.get("status") == "ok" or not failure_started_at:
        failure_started_at = timestamp
    state.update(
        {
            "status": failure_type,
            "failure_type": failure_type,
            "message": message,
            "last_attempt_at": timestamp,
            "last_failure_at": timestamp,
            "failure_started_at": failure_started_at,
            "consecutive_failures": int(state.get("consecutive_failures", 0)) + 1,
        }
    )
    write_state(state)
    return state


def days_since(timestamp: Optional[str]) -> Optional[float]:
    if not timestamp:
        return None
    try:
        elapsed = datetime.now().astimezone() - datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return max(elapsed.total_seconds() / 86400, 0.0)


def public_health(state: Optional[dict] = None) -> dict:
    state = state or load_state()
    return {
        "status": state.get("status"),
        "failure_type": state.get("failure_type"),
        "message": state.get("message"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_success_at": state.get("last_success_at"),
        "last_failure_at": state.get("last_failure_at"),
        "last_successful_report_generation": state.get("last_successful_report_generation"),
        "last_report_failure_at": state.get("last_report_failure_at"),
        "failure_started_at": state.get("failure_started_at"),
        "consecutive_failures": int(state.get("consecutive_failures", 0)),
        "notification_sent_at": state.get("notification_sent_at"),
        "days_since_last_successful_collection": days_since(state.get("last_success_at")),
    }


def load_email_settings() -> dict:
    settings = dict(DEFAULT_EMAIL_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return settings
        configured = raw.get("failure_email_notification", {})
        if isinstance(configured, dict):
            settings.update(configured)
    return settings


def maybe_send_failure_notification(state: dict) -> bool:
    settings = load_email_settings()
    if not settings.get("enabled") or state.get("notification_sent_at"):
        return False

    failure_age = days_since(state.get("failure_started_at"))
    if failure_age is None or failure_age * 24 <= float(settings["failure_after_hours"]):
        return False

    required = ("smtp_host", "from_address", "to_address")
    if any(not settings.get(key) for key in required):
        return False

    message = EmailMessage()
    message["Subject"] = "PowerMonitor collector failure"
    message["From"] = settings["from_address"]
    message["To"] = settings["to_address"]
    message.set_content(
        "PowerMonitor collection has failed for more than "
        f"{settings['failure_after_hours']} hours.\n\n"
        f"Status: {state.get('status')}\n"
        f"Failure started: {state.get('failure_started_at')}\n"
        f"Last successful collection: {state.get('last_success_at') or 'unavailable'}\n"
        f"Consecutive failures: {state.get('consecutive_failures', 0)}\n"
    )

    password = os.environ.get(str(settings.get("smtp_password_env", "")), "")
    with smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=30) as smtp:
        if settings.get("use_starttls", True):
            smtp.starttls()
        if settings.get("smtp_username"):
            smtp.login(settings["smtp_username"], password)
        smtp.send_message(message)

    state["notification_sent_at"] = now_iso()
    write_state(state)
    return True
