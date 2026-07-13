#!/usr/bin/env python3
import html
import json
import logging
import os
import subprocess
import sqlite3
import sys
import uuid
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import import_ocular_export
import import_synergy_bill
import report
from engine.import_framework import detect_importer
from engine.data_engine import DataEngine
from engine.historical_backfill import build_backfill_preview, job_id_for, resolve_backfill_range
from engine.models import to_api_safe
from engine.runtime_diagnostics import runtime_diagnostics
from engine.tariff_catalog import (
    BUCKET_ORDER,
    DEFAULT_PLAN,
    DEFAULT_RETAILER,
    DEFAULT_STATE_OR_TERRITORY,
    MANUAL_TARIFF_ID,
    apply_structured_tariff,
    comparison_rates_for_plan,
    tariff_plan_for_selection,
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / "logs" / "web.log"
UPLOAD_DIR = PROJECT_ROOT / "data" / "import_uploads"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def live_health() -> dict:
    settings = report.load_settings()
    with sqlite3.connect(report.DB_PATH) as conn:
        report.validate_database(conn)
        return report.health_data(conn, report.local_today(), settings)


def runtime_check() -> dict:
    host, port = resolve_web_bind()
    return runtime_diagnostics(
        project_root=PROJECT_ROOT,
        database_path=report.DB_PATH,
        web_host=host,
        web_port=port,
    )


def api_json_dumps(payload: dict) -> str:
    return json.dumps(to_api_safe(payload), indent=2, sort_keys=True)


def current_report_path() -> Path:
    return report.REPORT_PATH


def regenerate_dashboard_report() -> None:
    report.generate_report(current_report_path())


def resolve_web_bind(settings_path: Path = SETTINGS_PATH) -> tuple[str, int]:
    config = load_web_bind_config(settings_path)
    host = os.environ.get("POWMONITOR_HOST") or config.get("host") or DEFAULT_HOST
    raw_port = os.environ.get("POWMONITOR_PORT") or config.get("port") or DEFAULT_PORT
    return str(host), parse_port(raw_port)


def load_web_bind_config(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    web_server = raw.get("web_server") if isinstance(raw.get("web_server"), dict) else {}
    return {
        "host": web_server.get("host") or raw.get("web_host"),
        "port": web_server.get("port") or raw.get("web_port"),
    }


def normalize_provider_payload(payload: dict) -> dict:
    normalized = {}
    mappings = {}
    for key, value in payload.items():
        key_text = str(key)
        if key_text.startswith("entity_mappings."):
            mappings[key_text.split(".", 1)[1]] = value
        else:
            normalized[key_text] = value
    existing = normalized.get("entity_mappings")
    if isinstance(existing, dict):
        mappings.update(existing)
    if mappings:
        normalized["entity_mappings"] = mappings
    return normalized


def _last_item(value) -> Optional[str]:
    if isinstance(value, list) and value:
        return str(value[-1])
    return None


def provider_flash_from_params(params: dict) -> Optional[dict]:
    provider_id = DashboardHandler.param(params, "provider_action_provider")
    if not provider_id:
        return None
    return {
        "provider_id": provider_id,
        "action": DashboardHandler.param(params, "provider_action", ""),
        "status": DashboardHandler.param(params, "provider_status", ""),
        "message": DashboardHandler.param(params, "provider_message", ""),
        "next_step": DashboardHandler.param(params, "provider_next", ""),
    }


def tariff_update_flash_from_params(params: dict) -> Optional[dict]:
    action = DashboardHandler.param(params, "tariff_update_action")
    if not action:
        return None
    return {
        "action": action,
        "status": DashboardHandler.param(params, "tariff_update_status", "success"),
        "message": DashboardHandler.param(params, "tariff_update_message", ""),
    }


def historical_backfill_flash_from_params(params: dict) -> Optional[dict]:
    action = DashboardHandler.param(params, "historical_backfill_action")
    if not action:
        return None
    return {
        "action": action,
        "status": DashboardHandler.param(params, "historical_backfill_status", "success"),
        "message": DashboardHandler.param(params, "historical_backfill_message", ""),
        "job_id": DashboardHandler.param(params, "historical_backfill_job_id", ""),
        "range": DashboardHandler.param(params, "historical_backfill_range", ""),
        "next_step": DashboardHandler.param(params, "historical_backfill_next", ""),
    }


def tariff_update_redirect(action: str, *, status: str = "success", message: str = "") -> str:
    query = urlencode(
        {
            "tariff_update_action": action,
            "tariff_update_status": status,
            "tariff_update_message": message,
        }
    )
    return f"/?{query}#tariff-updates"


def historical_backfill_redirect(payload: dict, *, action: str, status: str = "success") -> str:
    selected_range = payload.get("range") or {}
    range_label = ""
    if selected_range:
        range_label = f"{selected_range.get('start', '')} to {selected_range.get('end', '')}".strip()
    query = urlencode(
        {
            "historical_backfill_action": action,
            "historical_backfill_status": status,
            "historical_backfill_message": str(payload.get("message") or "Historical data action completed."),
            "historical_backfill_job_id": str(payload.get("job_id") or ""),
            "historical_backfill_range": range_label,
            "historical_backfill_next": str(payload.get("next_step") or "Review the job status below."),
        }
    )
    return f"/?{query}#historical-coverage"


def provider_action_redirect(provider_id: str, action: str, result) -> str:
    message = str(result.user_message or "Provider action completed.")
    next_step = str(result.next_step or "Review provider status.")
    if action in {"save", "configure", "edit"} and result.success:
        message = "Settings saved. Now test the connection."
        next_step = "Run Test Connection before relying on this data source."
    query = urlencode(
        {
            "provider_action_provider": provider_id,
            "provider_action": action,
            "provider_status": str(result.status or ("success" if result.success else "failure")),
            "provider_message": message,
            "provider_next": next_step,
        }
    )
    return f"/?{query}#system-provider-{provider_id}"


def format_money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def parse_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("POWMONITOR_PORT/web_port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("POWMONITOR_PORT/web_port must be between 1 and 65535")
    return port


def update_billing_cycle_settings(
    settings_path: Path,
    *,
    billing_period_start_day,
    state_or_territory: str = DEFAULT_STATE_OR_TERRITORY,
    retailer: str = DEFAULT_RETAILER,
    current_plan: str = DEFAULT_PLAN,
    tariff_add_on: str = "none",
    manual_rates: Optional[dict] = None,
    solar_setup_choice: str = "none",
    ev_setup_choice: str = "none",
    battery_setup_choice: str = "none",
    battery_investment_assumptions: Optional[dict] = None,
) -> dict:
    try:
        start_day = int(billing_period_start_day)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a billing cycle start day from 1 to 28.") from exc
    if not 1 <= start_day <= 28:
        raise ValueError("Choose a billing cycle start day from 1 to 28.")
    state = str(state_or_territory or DEFAULT_STATE_OR_TERRITORY).upper()
    retailer_id = str(retailer or DEFAULT_RETAILER).lower()
    plan = str(current_plan or DEFAULT_PLAN)
    add_on = str(tariff_add_on or "none")

    if settings_path.exists():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Settings file must contain valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("Settings file must contain a JSON object")
    else:
        raw = {}

    raw.update(
        {
            "billing_period_start_day": start_day,
            "state_or_territory": state,
            "retailer": retailer_id,
            "retailer_id": retailer_id,
            "current_plan": plan,
            "tariff_add_on": add_on,
            "solar_setup_choice": _setup_choice(solar_setup_choice, {"none", "solaredge", "home_assistant", "manual_later"}),
            "ev_setup_choice": _setup_choice(ev_setup_choice, {"none", "ocular_import", "home_assistant", "manual_later"}),
            "battery_setup_choice": _setup_choice(battery_setup_choice, {"none", "home_assistant", "manual_later"}),
        }
    )
    if battery_investment_assumptions is not None:
        raw.update(_battery_investment_settings(battery_investment_assumptions))
    if plan == MANUAL_TARIFF_ID or retailer_id == MANUAL_TARIFF_ID:
        raw.update(_manual_tariff_settings(manual_rates or {}))
    else:
        selected = tariff_plan_for_selection(state, retailer_id, plan, add_on)
        if selected is None:
            raise ValueError("If your retailer or plan is not listed, choose Manual tariff.")
        raw = apply_structured_tariff(raw)
        raw["comparison_plan_rates"] = comparison_rates_for_plan("previous_flat_or_home_plan")
    raw["billing_cycle_configured"] = True
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temporary.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(settings_path)
    return raw


def _setup_choice(value: str, allowed: set[str]) -> str:
    choice = str(value or "none")
    return choice if choice in allowed else "none"


def _manual_tariff_settings(values: dict) -> dict:
    try:
        supply = float(values["supply_charge_per_day"])
        export = float(values["export_credit_rate"])
        rates = {bucket: float(values[bucket]) for bucket in BUCKET_ORDER}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Enter all manual tariff rates before saving Manual tariff.") from exc
    if supply < 0 or export < 0 or any(rate < 0 for rate in rates.values()):
        raise ValueError("Manual tariff rates cannot be negative.")
    return {
        "tariff_setup_mode": "manual",
        "current_plan": MANUAL_TARIFF_ID,
        "retailer": "Manual tariff",
        "retailer_id": MANUAL_TARIFF_ID,
        "tariff_add_on": "none",
        "supply_charge_per_day": supply,
        "export_credit_rate": export,
        "import_rates": rates,
    }


def _battery_investment_settings(values: dict) -> dict:
    try:
        costs = {
            "5": float(values["installed_cost_5"]),
            "10": float(values["installed_cost_10"]),
            "15": float(values["installed_cost_15"]),
            "20": float(values["installed_cost_20"]),
        }
        efficiency_pct = float(values["round_trip_efficiency_pct"])
        life_years = int(values["life_years"])
        degradation = float(values["annual_degradation_pct"])
        inflation = float(values["electricity_inflation_pct"])
        discount = float(values["discount_rate_pct"])
        rebate = float(values["government_rebate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Enter valid battery investment assumptions before saving.") from exc
    if any(cost < 0 for cost in costs.values()):
        raise ValueError("Battery installed costs cannot be negative.")
    if efficiency_pct <= 0 or efficiency_pct > 100:
        raise ValueError("Battery round-trip efficiency must be between 0 and 100%.")
    if life_years < 1:
        raise ValueError("Battery life must be at least 1 year.")
    if degradation < 0 or discount < 0 or rebate < 0:
        raise ValueError("Battery degradation, discount rate and rebate cannot be negative.")
    return {
        "battery_installed_costs": costs,
        "battery_round_trip_efficiency": efficiency_pct / 100,
        "battery_life_years": life_years,
        "battery_annual_degradation_pct": degradation,
        "battery_electricity_inflation_pct": inflation,
        "battery_discount_rate_pct": discount,
        "battery_government_rebate": rebate,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PowerMonitor/1.0"

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        path = request.path
        params = parse_qs(request.query)
        try:
            if path == "/":
                self.serve_dashboard(params)
            elif path == "/glance":
                self.serve_glance(params)
            elif path == "/reports/index.html":
                self.serve_static_report()
            elif path == "/api/summary":
                self.send_json(
                    report.dashboard_data(
                        period_kind=self.param(params, "period", "current_billing_period"),
                        start=self.param(params, "start"),
                        end=self.param(params, "end"),
                    )
                )
            elif path in {"/api/setup_progress", "/api/setup_tasks", "/api/setup_sections"}:
                self.send_json(self.setup_api_payload(path, params))
            elif path == "/api/providers":
                self.send_json(self.providers_payload())
            elif path.startswith("/api/providers/"):
                self.send_json(self.provider_payload(path))
            elif path == "/api/runtime":
                self.send_json(report.provider_runtime_data().as_dict())
            elif path == "/api/runtime/providers":
                self.send_json({"providers": report.provider_runtime_data().as_dict()["providers"]})
            elif path.startswith("/api/runtime/provider/"):
                self.send_json(self.runtime_provider_payload(path))
            elif path == "/api/compare":
                self.send_json(self.compare_data(params))
            elif path == "/api/health":
                self.send_json(live_health())
            elif path == "/api/runtime-check":
                self.send_json({"runtime": runtime_check()})
            elif path == "/api/historical-coverage":
                self.send_json(report.historical_coverage_data())
            elif path == "/api/historical-backfill/preview":
                self.send_json(self.historical_backfill_preview(params))
            elif path == "/api/historical-backfill/diagnostics":
                self.send_json(self.historical_backfill_diagnostics(params))
            elif path.startswith("/api/historical-backfill/"):
                self.historical_backfill_method_not_allowed(path)
            else:
                self.send_json({"error": "not found"}, status=404)
        except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
            logging.error("Request failed for %s: %s", path, exc)
            self.send_json({"error": "dashboard data unavailable"}, status=500)

    def do_POST(self) -> None:
        request = urlsplit(self.path)
        try:
            if request.path == "/recommendations/lifecycle":
                self.update_recommendation_lifecycle()
            elif request.path == "/setup/billing-cycle":
                self.update_billing_cycle()
            elif request.path == "/tariff-updates/check":
                self.check_tariff_updates()
            elif request.path == "/tariff-updates/approve":
                self.resolve_tariff_update("approve")
            elif request.path == "/tariff-updates/dismiss":
                self.resolve_tariff_update("dismiss")
            elif request.path == "/imports/preview":
                self.preview_import()
            elif request.path == "/imports/commit":
                self.commit_import()
            elif request.path.startswith("/api/providers/"):
                self.update_provider(request.path)
            elif request.path == "/api/historical-backfill/start":
                self.start_historical_backfill()
            elif request.path == "/api/historical-backfill/cancel":
                self.cancel_historical_backfill()
            elif request.path == "/api/historical-backfill/resume":
                self.resume_historical_backfill()
            else:
                self.send_json({"error": "not found"}, status=404)
        except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
            logging.error("POST failed for %s", request.path)
            if request.path.startswith("/imports/"):
                self.send_import_error("We could not read that file. Please choose a supported Ocular Excel export or Synergy PDF bill and try again.", status=400)
            elif request.path == "/setup/billing-cycle":
                result_html = self.import_result_html(
                    "Billing cycle not saved",
                    str(exc),
                    success=False,
                )
                self.send_html(result_html.encode("utf-8"))
            elif request.path.startswith("/tariff-updates/"):
                self.redirect("/#tariff-updates")
            elif request.path.startswith("/api/historical-backfill/") and not self.provider_request_wants_json():
                action_name = request.path.rsplit("/", 1)[-1]
                payload = {
                    "message": str(exc) if isinstance(exc, ValueError) else "Historical data action failed.",
                    "next_step": "Review the historical data action and try again.",
                }
                self.redirect(historical_backfill_redirect(payload, action=action_name, status="failure"))
            elif request.path.startswith("/api/providers/") and not self.provider_request_wants_json():
                parts = [part for part in request.path.split("/") if part]
                provider_id = parts[2] if len(parts) >= 3 else "provider"
                action_name = parts[3] if len(parts) >= 4 else "update"
                message = f"Unable to save settings. {exc}" if isinstance(exc, ValueError) and action_name in {"configure", "edit", "save"} else "Provider action failed."
                query = urlencode(
                    {
                        "provider_action_provider": provider_id,
                        "provider_action": action_name,
                        "provider_status": "failure",
                        "provider_message": message,
                        "provider_next": "Review the provider form and try again.",
                    }
                )
                self.redirect(f"/?{query}#system-provider-{provider_id}")
            else:
                action_name = request.path.rsplit("/", 1)[-1] if request.path.startswith("/api/providers/") else "update"
                if action_name in {"configure", "edit", "save"}:
                    user_message = f"Unable to save settings. {exc}" if isinstance(exc, ValueError) else "Unable to save settings."
                else:
                    user_message = str(exc) if isinstance(exc, ValueError) else "Request update failed."
                self.send_json(
                    {
                        "action": {
                            "success": False,
                            "status": "failure",
                            "user_message": user_message,
                            "next_step": "Review the highlighted fields and try again.",
                            "technical_details": {"path": request.path},
                        }
                    },
                    status=400 if isinstance(exc, ValueError) else 500,
                )

    @staticmethod
    def param(params: dict, name: str, default=None):
        values = params.get(name)
        return values[0] if values else default

    def compare_data(self, params: dict) -> dict:
        settings = report.load_settings()
        today = report.local_today()
        period_a = report.resolve_period(
            self.param(params, "period_a", "custom" if self.param(params, "start_a") else "current_billing_period"),
            settings,
            today,
            self.param(params, "start_a"),
            self.param(params, "end_a"),
        )
        period_b = report.resolve_period(
            self.param(params, "period_b", "custom" if self.param(params, "start_b") else "previous_billing_period"),
            settings,
            today,
            self.param(params, "start_b"),
            self.param(params, "end_b"),
        )
        return report.comparison_data(period_a, period_b, settings)

    def setup_api_payload(self, path: str, params: dict) -> dict:
        data = report.dashboard_data(
            period_kind=self.param(params, "period", "current_billing_period"),
            start=self.param(params, "start"),
            end=self.param(params, "end"),
        )
        key = path.rsplit("/", 1)[-1]
        return {key: data[key]}

    def providers_payload(self) -> dict:
        return {"providers": report.provider_management_data().as_dict()["providers"]}

    def historical_backfill_preview(self, params: dict) -> dict:
        provider_id = self.param(params, "provider_id", "solaredge")
        range_preset = self.param(params, "range_preset", "previous_summer")
        start = self.param(params, "start")
        end = self.param(params, "end")
        coverage = report.historical_coverage_data()["historical_coverage"]
        return {
            "preview": build_backfill_preview(
                coverage,
                provider_id=provider_id,
                preset=range_preset,
                today=report.local_today(),
                start=start,
                end=end,
            )
        }

    def historical_backfill_diagnostics(self, params: dict) -> dict:
        job_id = self.param(params, "job_id")
        engine = DataEngine(db_path=report.DB_PATH)
        coverage_data = report.historical_coverage_data()
        if job_id:
            job = engine.get_historical_backfill_job(str(job_id))
            if not job:
                raise ValueError("Historical data job not found.")
            return {
                "historical_backfill": {
                    "job": job,
                    "coverage": coverage_data["historical_coverage"],
                    "capability_audit": coverage_data["solaredge_capability_audit"],
                }
            }
        return {
            "historical_backfill": {
                "jobs": coverage_data["historical_backfill_jobs"],
                "coverage": coverage_data["historical_coverage"],
                "seasonal_history": coverage_data["seasonal_history"],
                "capability_audit": coverage_data["solaredge_capability_audit"],
            }
        }

    def historical_backfill_method_not_allowed(self, path: str) -> None:
        action = path.rsplit("/", 1)[-1]
        if self.provider_request_wants_json():
            self.send_json(
                {
                    "error": "method_not_allowed",
                    "message": f"Use POST to {path} for historical backfill {action}.",
                    "allowed_methods": ["POST"],
                },
                status=405,
            )
            return
        self.redirect(
            historical_backfill_redirect(
                {
                    "message": f"Historical data {action} must be submitted with the form button.",
                    "next_step": "Return to Historical data and use the action button.",
                },
                action=action,
                status="failure",
            )
        )

    def start_historical_backfill(self) -> None:
        payload = self.read_request_payload()
        provider_id = str(payload.get("provider_id") or "solaredge")
        if provider_id != "solaredge":
            raise ValueError("Historical import is currently available for SolarEdge only.")
        range_preset = str(payload.get("range_preset") or "previous_summer")
        start = str(payload.get("start") or "") or None
        end = str(payload.get("end") or "") or None
        site_id = str(payload.get("site_id") or os.environ.get("POWERMONITOR_SOLAREDGE_SITE_ID") or "default")
        selected = resolve_backfill_range(range_preset, today=report.local_today(), start=start, end=end)
        job_id = job_id_for(provider_id, site_id, selected)
        command = [
            sys.executable,
            str(PROJECT_ROOT / "collector" / "backfill_solaredge.py"),
            "--range",
            range_preset,
            "--site-id",
            site_id,
            "--job-id",
            job_id,
        ]
        if start:
            command.extend(["--start", start])
        if end:
            command.extend(["--end", end])
        subprocess.Popen(command, cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        response = {
            "success": True,
            "job_id": job_id,
            "provider_id": provider_id,
            "site_id": site_id,
            "requested_range": selected.as_dict(),
            "range": selected.as_dict(),
            "status": "started",
            "chunks_completed": 0,
            "readings_added": 0,
            "duplicates_skipped": 0,
            "failed_days": [],
            "last_successful_day": None,
            "resume_available": True,
            "message": "Historical data import started.",
            "next_step": "Refresh Historical data in a moment to see chunks completed, readings added, duplicates skipped, and failed days.",
        }
        self.send_historical_backfill_response(response, action="start")

    def resume_historical_backfill(self) -> None:
        payload = self.read_request_payload()
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise ValueError("Job id is required.")
        command = [
            sys.executable,
            str(PROJECT_ROOT / "collector" / "backfill_solaredge.py"),
            "--resume",
            "--job-id",
            job_id,
        ]
        subprocess.Popen(command, cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        job = DataEngine(db_path=report.DB_PATH).get_historical_backfill_job(job_id) or {}
        progress = job.get("progress") or {}
        response = {
            "success": True,
            "job_id": job_id,
            "provider_id": job.get("provider_id", "solaredge"),
            "requested_range": {
                "start": job.get("start_date"),
                "end": job.get("end_date"),
                "preset": job.get("range_preset"),
            },
            "range": {
                "start": job.get("start_date"),
                "end": job.get("end_date"),
                "preset": job.get("range_preset"),
            },
            "status": "started",
            "chunks_completed": int(progress.get("chunks_completed") or 0),
            "readings_added": int(progress.get("imported_or_updated") or 0),
            "duplicates_skipped": int(progress.get("duplicates_skipped") or 0),
            "failed_days": list(progress.get("failed_days") or []),
            "last_successful_day": _last_item(progress.get("completed_days")),
            "resume_available": True,
            "message": "Historical data import resumed.",
            "next_step": "Refresh Historical data in a moment to see updated progress.",
        }
        self.send_historical_backfill_response(response, action="resume")

    def cancel_historical_backfill(self) -> None:
        payload = self.read_request_payload()
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise ValueError("Job id is required.")
        job = DataEngine(db_path=report.DB_PATH).request_historical_backfill_cancel(job_id)
        progress = job.get("progress") or {}
        response = {
            "success": True,
            "job_id": job_id,
            "provider_id": job.get("provider_id", "solaredge"),
            "requested_range": {
                "start": job.get("start_date"),
                "end": job.get("end_date"),
                "preset": job.get("range_preset"),
            },
            "range": {
                "start": job.get("start_date"),
                "end": job.get("end_date"),
                "preset": job.get("range_preset"),
            },
            "status": job.get("status"),
            "chunks_completed": int(progress.get("chunks_completed") or 0),
            "readings_added": int(progress.get("imported_or_updated") or 0),
            "duplicates_skipped": int(progress.get("duplicates_skipped") or 0),
            "failed_days": list(progress.get("failed_days") or []),
            "last_successful_day": _last_item(progress.get("completed_days")),
            "resume_available": job.get("status") not in {"complete", "failed"},
            "message": "Cancel requested.",
            "next_step": "The running import will stop at the next safe checkpoint.",
        }
        self.send_historical_backfill_response(response, action="cancel")

    def send_historical_backfill_response(self, payload: dict, *, action: str) -> None:
        regenerate_dashboard_report()
        if self.provider_request_wants_json():
            self.send_json({"historical_backfill": payload})
            return
        self.redirect(historical_backfill_redirect(payload, action=action))

    def provider_payload(self, path: str) -> dict:
        parts = [part for part in path.split("/") if part]
        if len(parts) not in {3, 4}:
            return {"error": "not found"}
        provider_id = parts[2]
        manager = report.provider_management_data()
        provider = manager.get(provider_id)
        if provider is None:
            return {"error": "provider not found"}
        if len(parts) == 4 and parts[3] == "diagnostics":
            return {"diagnostics": provider.diagnostics.as_dict()}
        if len(parts) == 4 and parts[3] == "test":
            return {"test": manager.test_connection(provider_id).as_dict()}
        return {"provider": provider.as_dict()}

    def runtime_provider_payload(self, path: str) -> dict:
        provider_id = path.rsplit("/", 1)[-1]
        runtime = report.provider_runtime_data()
        return {"provider": runtime.provider_status(provider_id)}

    def update_provider(self, path: str) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_json({"error": "not found"}, status=404)
            return
        provider_id = parts[2]
        action = parts[3]
        manager = report.provider_management_data()
        payload = self.read_request_payload()
        if action == "test":
            result = manager.action(provider_id, action)
            logging.info(
                "Provider action POST provider_id=%s action=%s status=%s",
                provider_id,
                action,
                result.status,
            )
            self.send_provider_action_response(provider_id, action, result)
            return
        if action in {"configure", "edit", "save", "reset", "reconnect", "disable", "enable", "remove"}:
            result = manager.action(provider_id, action, payload)
            logging.info(
                "Provider action POST provider_id=%s action=%s status=%s",
                provider_id,
                action,
                result.status,
            )
            self.send_provider_action_response(provider_id, action, result)
            return
        self.send_json({"error": "unsupported provider action"}, status=400)

    def send_provider_action_response(self, provider_id: str, action: str, result) -> None:
        if self.provider_request_wants_json():
            self.send_json({"action": result.as_dict()})
            return
        self.redirect(provider_action_redirect(provider_id, action, result))

    def provider_request_wants_json(self) -> bool:
        content_type = self.headers.get("Content-Type", "")
        accept = self.headers.get("Accept", "")
        requested_with = self.headers.get("X-Requested-With", "")
        if "application/json" in content_type or requested_with.lower() == "xmlhttprequest":
            return True
        return "application/json" in accept and "text/html" not in accept

    def read_request_payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_bytes = self.rfile.read(length)
        if not raw_bytes:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            fields, _files = parse_multipart_form(raw_bytes, content_type)
            return normalize_provider_payload(fields)
        raw_body = raw_bytes.decode("utf-8")
        if "application/json" in content_type:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return normalize_provider_payload(payload)
        return normalize_provider_payload({key: values[-1] for key, values in parse_qs(raw_body).items()})

    def update_recommendation_lifecycle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(raw_body)
        stable_id = self.param(params, "stable_id")
        action = self.param(params, "action")
        if not stable_id or not action:
            raise ValueError("stable_id and action are required")

        settings = report.load_settings()
        state = "active"
        snoozed_until = None
        reason = None
        if action == "dismiss":
            state = "dismissed"
            reason = "Dismissed from Recommendation Centre"
        elif action == "restore":
            state = "active"
            reason = "Restored from Recommendation Centre"
        elif action == "snooze_1d":
            state = "snoozed_until"
            snoozed_until = (report.local_today() + timedelta(days=1)).isoformat()
            reason = "Snoozed for 1 day"
        elif action == "snooze_7d":
            state = "snoozed_until"
            snoozed_until = (report.local_today() + timedelta(days=7)).isoformat()
            reason = "Snoozed for 7 days"
        elif action == "snooze_next_billing":
            state = "snoozed_until"
            snoozed_until = next_billing_snooze_date(settings).isoformat()
            reason = "Snoozed until next billing period"
        else:
            raise ValueError(f"Unsupported lifecycle action: {action}")

        engine = report.data_engine()
        engine.validate_database()
        engine.set_recommendation_lifecycle(
            stable_id,
            state,
            snoozed_until=snoozed_until,
            reason=reason,
        )
        self.redirect("/#recommendations")

    def update_billing_cycle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8"))
        update_billing_cycle_settings(
            SETTINGS_PATH,
            billing_period_start_day=self.param(params, "billing_period_start_day"),
            state_or_territory=self.param(params, "state_or_territory", "WA"),
            retailer=self.param(params, "retailer", ""),
            current_plan=self.param(params, "current_plan", ""),
            tariff_add_on=self.param(params, "tariff_add_on", "none"),
            solar_setup_choice=self.param(params, "solar_setup_choice", "none"),
            ev_setup_choice=self.param(params, "ev_setup_choice", "none"),
            battery_setup_choice=self.param(params, "battery_setup_choice", "none"),
            battery_investment_assumptions={
                "installed_cost_5": self.param(params, "battery_installed_cost_5", "6500"),
                "installed_cost_10": self.param(params, "battery_installed_cost_10", "9500"),
                "installed_cost_15": self.param(params, "battery_installed_cost_15", "12500"),
                "installed_cost_20": self.param(params, "battery_installed_cost_20", "15500"),
                "round_trip_efficiency_pct": self.param(params, "battery_round_trip_efficiency_pct", "90"),
                "life_years": self.param(params, "battery_life_years", "15"),
                "annual_degradation_pct": self.param(params, "battery_annual_degradation_pct", "2"),
                "electricity_inflation_pct": self.param(params, "battery_electricity_inflation_pct", "3"),
                "discount_rate_pct": self.param(params, "battery_discount_rate_pct", "0"),
                "government_rebate": self.param(params, "battery_government_rebate", "0"),
            },
            manual_rates={
                "supply_charge_per_day": self.param(params, "manual_supply_charge_per_day"),
                "export_credit_rate": self.param(params, "manual_export_credit_rate"),
                "super_off_peak": self.param(params, "manual_import_rate_super_off_peak"),
                "overnight": self.param(params, "manual_import_rate_overnight"),
                "off_peak": self.param(params, "manual_import_rate_off_peak"),
                "peak": self.param(params, "manual_import_rate_peak"),
            },
        )
        self.redirect("/#setup")

    def check_tariff_updates(self) -> None:
        settings = report.load_settings()
        current_day = report.local_today()
        period = report.resolve_period("current_billing_period", settings, current_day)
        summary = None
        try:
            with sqlite3.connect(report.DB_PATH) as conn:
                report.validate_database(conn)
                summary = report.load_summary(conn, period.start, period.end, settings)
        except (FileNotFoundError, RuntimeError, sqlite3.Error):
            summary = None
        result = report.check_tariff_updates(settings, summary)
        regenerate_dashboard_report()
        status = str(result.get("status") or "checked")
        message = (
            "A tariff update is ready for review."
            if status == "pending_review"
            else "No tariff update was found."
            if status == "no_change"
            else "PowerMonitor could not complete the tariff check."
            if status == "error"
            else "Tariff check complete."
        )
        self.redirect(tariff_update_redirect("check", status="failure" if status == "error" else "success", message=message))

    def resolve_tariff_update(self, action: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8"))
        update_id = self.param(params, "update_id", "")
        if not update_id:
            raise ValueError("Tariff update id is required.")
        repository = report.tariff_update_repository()
        if action == "approve":
            repository.approve(update_id)
        elif action == "dismiss":
            repository.dismiss(update_id)
        else:
            raise ValueError(f"Unsupported tariff update action: {action}")
        regenerate_dashboard_report()
        message = (
            "Approved. Future calculations from the effective date will use the new version; historical periods were not rewritten."
            if action == "approve"
            else "Dismissed. The pending version was archived and the active tariff was left unchanged."
        )
        self.redirect(tariff_update_redirect(action, message=message))

    def preview_import(self) -> None:
        filename, content = self.read_uploaded_file()
        upload_path = self.write_upload(filename, content)
        importer = detect_importer(upload_path, self.importers())
        if importer is None:
            upload_path.unlink(missing_ok=True)
            self.send_import_error("That file type is not supported yet. Please choose an Ocular Excel export or Synergy PDF bill.", status=400)
            return

        parsed = importer.parse(upload_path)
        preview = importer.preview(parsed)
        if any(issue.severity == "error" for issue in preview.issues):
            upload_path.unlink(missing_ok=True)
            self.send_import_preview(importer.source.display_name, filename, preview, None)
            return

        engine = report.data_engine()
        engine.validate_database()
        job = engine.create_import_job(
            importer.source,
            filename,
            upload_path,
            preview=preview,
            status="previewed",
            message="Ready to import",
        )
        self.send_import_preview(importer.source.display_name, filename, preview, job["job_id"])

    def commit_import(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8"))
        job_id = self.param(params, "job_id")
        if not job_id:
            raise ValueError("job_id is required")

        engine = report.data_engine()
        engine.validate_database()
        job = engine.get_import_job(job_id)
        upload_path = Path(str(job.get("file_path") or ""))
        if not upload_path.exists():
            result_html = self.import_result_html(
                "Import file unavailable",
                "The uploaded file is no longer available. Please upload it again.",
                success=False,
            )
            engine.update_import_job(job_id, "failed", message="Uploaded file unavailable")
            self.send_html(result_html.encode("utf-8"))
            return

        importer = detect_importer(upload_path, self.importers())
        if importer is None:
            engine.update_import_job(job_id, "failed", message="Unsupported import format")
            self.send_import_error("That uploaded file is not a supported Ocular Excel export or Synergy PDF bill. Please upload it again.", status=400)
            return

        parsed = importer.parse(upload_path)
        result = importer.commit(parsed, engine.get_household_context(), job_id=job_id)
        engine.update_import_job(job_id, result.status, result=result, message=result.message)
        result_html = self.import_result_html(
            "Import complete" if result.success else "Import failed",
            result.message or "Import finished.",
            success=result.success,
        )
        self.send_html(result_html.encode("utf-8"))

    def serve_dashboard(self, params: dict) -> None:
        provider_flash = provider_flash_from_params(params)
        tariff_flash = tariff_update_flash_from_params(params)
        historical_backfill_flash = historical_backfill_flash_from_params(params)
        if provider_flash or tariff_flash or historical_backfill_flash:
            content = report.build_live_html(
                provider_flash=provider_flash,
                tariff_update_flash=tariff_flash,
                historical_backfill_flash=historical_backfill_flash,
            ).encode("utf-8")
            self.send_html(content)
            return
        self.serve_static_report()

    def serve_glance(self, params: dict) -> None:
        content = report.build_live_html(
            "current_billing_period",
            self.param(params, "start"),
            self.param(params, "end"),
            glance_only=True,
        ).encode("utf-8")
        self.send_html(content, refresh_seconds=300)

    def serve_static_report(self) -> None:
        report_path = current_report_path()
        if not report_path.exists():
            regenerate_dashboard_report()
        self.send_html(report_path.read_bytes())

    def read_uploaded_file(self) -> tuple[str, bytes]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length)
        fields, files = parse_multipart_form(raw_body, self.headers.get("Content-Type", ""))
        uploaded = files.get("import_file")
        if uploaded is None or not uploaded.get("filename"):
            raise ValueError("Choose an Ocular Excel file or Synergy PDF bill before previewing the import.")
        content = uploaded.get("content") or b""
        if not content:
            raise ValueError("That file is empty. Please choose the export or bill again.")
        return safe_filename(str(uploaded["filename"])), content

    def write_upload(self, filename: str, content: bytes) -> Path:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower()
        path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        path.write_bytes(content)
        return path

    @staticmethod
    def importers():
        return [
            import_ocular_export.OcularEVImporter(report.DB_PATH),
            import_synergy_bill.SynergyBillImporter(report.DB_PATH),
        ]

    def send_import_preview(self, source_name: str, filename: str, preview, job_id: Optional[str]) -> None:
        issues = "".join(
            f"<li>{html.escape(issue.severity.title())}: {html.escape(issue.message)}</li>"
            for issue in preview.issues
        ) or "<li>No validation issues found.</li>"
        if preview.source.source_id == "synergy_bill_pdf":
            samples = "".join(
                f"""
                <tr>
                  <td>{html.escape(str(item.get('invoice_number') or ''))}</td>
                  <td>{html.escape(str(item.get('account_number') or ''))}</td>
                  <td>{html.escape(str(item.get('nmi') or ''))}</td>
                  <td>{html.escape(str(item.get('issue_date') or ''))}</td>
                  <td>{html.escape(format_money(item.get('bill_total')))}</td>
                  <td>{html.escape(format_money(item.get('export_credits')))}</td>
                  <td>{html.escape(str(item.get('confidence') or '').title())}</td>
                </tr>
                """
                for item in preview.sample_records
            ) or '<tr><td colspan="7">No bill details available.</td></tr>'
            sample_heading = "<tr><th>Invoice</th><th>Account</th><th>NMI</th><th>Issue date</th><th>Bill total</th><th>Export credits</th><th>Confidence</th></tr>"
            summary_text = (
                f"{html.escape(filename)}: invoice preview is ready, covering "
                f"{html.escape(str(preview.date_start or 'n/a'))} to {html.escape(str(preview.date_end or 'n/a'))}. "
                f"Detected usage totals {preview.total_energy_kwh:.2f} kWh."
            )
        else:
            samples = "".join(
                f"<tr><td>{html.escape(str(item.get('record_number') or ''))}</td><td>{html.escape(str(item.get('start_time') or ''))}</td><td>{float(item.get('energy_kwh') or 0):.2f} kWh</td></tr>"
                for item in preview.sample_records
            ) or '<tr><td colspan="3">No sample records available.</td></tr>'
            sample_heading = "<tr><th>Record</th><th>Start</th><th>Energy</th></tr>"
            summary_text = (
                f"{html.escape(filename)}: {preview.valid_records} of {preview.total_records} rows are ready to import, "
                f"covering {preview.total_energy_kwh:.2f} kWh from {html.escape(str(preview.date_start or 'n/a'))} "
                f"to {html.escape(str(preview.date_end or 'n/a'))}."
            )
        commit_form = (
            f"""
            <form class="import-form" method="post" action="/imports/commit">
              <input type="hidden" name="job_id" value="{html.escape(job_id)}">
              <button type="submit">Commit import</button>
            </form>
            """
            if job_id
            else '<p class="notice">Resolve validation errors before committing this import.</p>'
        )
        content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Import preview</title>{self.import_page_style()}</head>
<body><main>
  <section class="panel">
    <p class="eyebrow">Import preview</p>
    <h1>{html.escape(source_name)}</h1>
    <p>{summary_text}</p>
    {commit_form}
    <a class="secondary-link" href="/#imports">Back to Dashboard</a>
  </section>
  <section class="panel"><h2>Validation</h2><ul>{issues}</ul></section>
  <section class="panel"><h2>Extracted details</h2><div class="table-scroll"><table><thead>{sample_heading}</thead><tbody>{samples}</tbody></table></div></section>
</main></body></html>"""
        self.send_html(content.encode("utf-8"))

    def send_import_error(self, message: str, status: int = 400) -> None:
        content = self.import_result_html("Import not accepted", message, success=False)
        self.send_response(status)
        encoded = content.encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def import_result_html(self, title: str, message: str, success: bool) -> str:
        tone = "success" if success else "error"
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>{self.import_page_style()}</head>
<body><main><section class="panel {tone}">
  <p class="eyebrow">Imports</p>
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(message)}</p>
  <a class="secondary-link" href="/#imports">Back to Dashboard</a>
</section></main></body></html>"""

    @staticmethod
    def import_page_style() -> str:
        return """
<style>
body{margin:0;background:#f5f5f7;color:#1d1d1f;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:min(920px,calc(100% - 36px));margin:28px auto}
.panel{margin-top:16px;padding:24px;background:#fff;border-radius:18px;box-shadow:0 18px 55px rgba(0,0,0,.06)}
.panel.success{border-left:6px solid #1c9b6c}.panel.error{border-left:6px solid #dc604e}
.eyebrow{margin:0;color:#1c9b6c;font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}
h1,h2{margin:4px 0 8px} p{color:#6e6e73} a,button{display:inline-block;margin-top:10px;padding:9px 12px;color:#fff;background:#24312c;border:0;border-radius:10px;text-decoration:none;font:inherit;font-weight:750;cursor:pointer}
.secondary-link{background:#eef3f0;color:#24312c}.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;min-width:520px;border-collapse:collapse} th,td{padding:9px 8px;border-bottom:1px solid #e7e7ec;text-align:left;font-size:12px} th{color:#6e6e73;font-size:9px;text-transform:uppercase;letter-spacing:.08em}
.notice{padding:10px 12px;color:#765b20;background:#fff5da;border-radius:10px}
@media(max-width:620px){main{width:min(100% - 16px,920px);margin:8px auto 28px}.panel{padding:18px;border-radius:16px}a,button{width:100%;text-align:center}h1{font-size:26px}}
</style>"""

    def send_html(self, content: bytes, refresh_seconds: Optional[int] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if refresh_seconds is not None:
            self.send_header("Refresh", str(int(refresh_seconds)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: int = 200) -> None:
        content = api_json_dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "script-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )

    def log_request(self, code="-", size="-") -> None:
        path = urlsplit(self.path).path
        logging.info("%s %s %s %s", self.client_address[0], self.command, path, code)

    def log_message(self, message: str, *args) -> None:
        return


def next_billing_snooze_date(settings) -> date:
    today = report.local_today()
    if settings.current_billing_period_end:
        return settings.current_billing_period_end + timedelta(days=1)
    if (
        settings.current_billing_period_start
        and settings.previous_billing_period_start
        and settings.previous_billing_period_end
    ):
        days = (
            settings.previous_billing_period_end - settings.previous_billing_period_start
        ).days + 1
        return settings.current_billing_period_start + timedelta(days=days)
    return today + timedelta(days=7)


def safe_filename(value: str) -> str:
    name = Path(value or "upload").name
    cleaned = "".join(char if char.isalnum() or char in {".", "-", "_"} else "_" for char in name)
    return cleaned or "upload"


def parse_multipart_form(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Expected multipart form data.")
    headers = (
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    if not message.is_multipart():
        raise ValueError("Upload form was not multipart.")
    fields: dict[str, str] = {}
    files: dict[str, dict[str, object]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        if filename is not None:
            files[str(name)] = {
                "filename": safe_filename(filename),
                "content": content,
                "content_type": part.get_content_type(),
            }
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[str(name)] = content.decode(charset, errors="replace")
    return fields, files


def main() -> None:
    report.configure_demo_mode(sys.argv)
    global SETTINGS_PATH
    SETTINGS_PATH = report.SETTINGS_PATH
    setup_logging()
    host, port = resolve_web_bind()
    logging.info("Runtime check: %s", json.dumps(runtime_check(), sort_keys=True))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    logging.info("PowerMonitor dashboard server started on http://%s:%d", host, port)
    print(f"PowerMonitor dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logging.info("PowerMonitor dashboard server stopped")


if __name__ == "__main__":
    main()
