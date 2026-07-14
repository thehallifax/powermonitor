#!/usr/bin/env python3
import html
import json
import logging
import math
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from collector_health import public_health
from performance import (
    BoundedHtmlCache,
    current_context,
    file_fingerprint,
    increment as perf_increment,
    latest_diagnostics,
    memoize as perf_memoize,
    performance_context,
    record_timing as perf_record_timing,
    settings_fingerprint,
    stage as perf_stage,
    timer_start as perf_timer_start,
)
from periods import LOCAL_TIMEZONE, Period, previous_equivalent, resolve_period

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.briefing_engine import BriefingEngine
from engine.advisor_presenter import attach_decision_to_advisors, explain_household_decisions
from engine.bill_reconciliation_engine import BillReconciliationEngine
from engine.charging_advisor import ChargingAdvisor
from engine.context_engine import ContextEngine
from engine.cost_advisor import CostAdvisor
from engine.data_engine import DataEngine
from engine.decision_engine import (
    Decision,
    generate_decisions_from_data_engine,
)
from engine.decision_pipeline import build_decision_pipeline_output
from engine.efficiency_advisor import EfficiencyAdvisor
from engine.energy_timeline import EnergyTimeline, EnergyTimelineEngine
from engine.financial_engine import FinancialEngine
from engine.goals_engine import GoalsEngine
from engine.historical_trends_engine import (
    HistoricalTrendMetric,
    HistoricalTrends,
    HistoricalTrendsEngine,
    IMPROVING,
    UNCHANGED,
    WORSENING,
)
from engine.historical_backfill import (
    RANGE_PRESETS,
    build_backfill_preview,
    seasonal_history_summary,
    solaredge_capability_audit,
)
from engine.home_profile import DEFAULT_HOME_PROFILE_PATH, HomeProfile, load_home_profile
from engine.household_timeline import (
    DEFAULT_HOUSEHOLD_TIMELINE_PATH,
    HouseholdTimeline,
    HouseholdTimelineError,
    HouseholdTimelineEvent,
    HouseholdTimelineStore,
    household_tariff_resolver,
    load_household_timeline,
    tariff_version_for_plan,
    tariff_version_from_catalog_or_manual,
    timeline_summary_sentence,
)
from engine.household_state_engine import HouseholdStateEngine
from engine.insights_engine import HouseholdInsight, HouseholdInsightsEngine
from engine.measurement_framework import MeasurementType, measurement_totals
from engine.trends_engine import HouseholdTrendsReport, TrendsEngine
from engine.models import (
    Account,
    Advisor,
    AdvisorCollection,
    BriefingPriority,
    ChargingAdvice,
    DailyBriefing,
    DecisionTimeline,
    EnergyScore,
    GoalProgress,
    Household,
    HouseholdCapability,
    HouseholdContext,
    HouseholdMembership,
    SetupChecklist,
    SetupSection,
    SetupTask,
    TimelineEntry,
)
from engine.notification_engine import NotificationEngine
from engine.provider_framework import ProviderCapability, ProviderRegistry, build_provider_registry
from engine.provider_manager import ProviderManager, build_provider_management
from engine.provider_entity_mapping import load_provider_mapping_record
from engine.provider_runtime import ProviderRuntime
from engine.recommendation_adapter import RecommendationPresentation
from engine.runtime_diagnostics import runtime_diagnostics
from engine.solar_advisor import SolarAdvisor
from engine.setup_engine import SetupEngine
from engine.tariff_catalog import (
    DEFAULT_EXPORT_CREDIT_RATE,
    DEFAULT_PLAN,
    DEFAULT_RETAILER,
    DEFAULT_STATE_OR_TERRITORY,
    DEFAULT_SUPPLY_CHARGE_PER_DAY,
    MANUAL_TARIFF_ID,
    MANUAL_TARIFF_LABEL,
    NO_ADD_ON,
    STATE_OPTIONS,
    SUPPORTED_PLANS,
    apply_structured_tariff,
    comparison_rates_for_plan,
    plan_label,
    supported_plan_options,
)
from engine.tariff_versioning import TariffResolver, TariffVersion, resolver_for_settings
from engine.tariff_importing import (
    TariffImportManager,
    TariffProviderRegistry,
    TariffUpdateRepository,
    SynergyTariffProvider,
    usage_profile_from_summary,
)
from presentation.briefing import BriefingItem
from presentation.energy_score import build_energy_score
from presentation.glance import GlanceState, build_glance_state
from presentation.timeline import build_decision_timeline

DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
REPORT_PATH = PROJECT_ROOT / "reports" / "index.html"
LOG_PATH = PROJECT_ROOT / "logs" / "report.log"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_SETTINGS_PATH = SETTINGS_PATH
HOME_PROFILE_PATH = DEFAULT_HOME_PROFILE_PATH
HOUSEHOLD_TIMELINE_PATH = DEFAULT_HOUSEHOLD_TIMELINE_PATH
COLLECT_LOG_PATH = PROJECT_ROOT / "logs" / "collect.log"
DEMO_MODE = False
DEMO_WEATHER_PATH: Optional[Path] = None
DEMO_RECOMMENDATIONS_PATH: Optional[Path] = None
LIVE_HTML_CACHE = BoundedHtmlCache(max_entries=8)

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
BUCKET_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}
AU_SEASON_LABELS_FOR_REPORT = {
    "summer": "Summer",
    "autumn": "Autumn",
    "winter": "Winter",
    "spring": "Spring",
}


def data_engine(conn: Optional[sqlite3.Connection] = None) -> DataEngine:
    return DataEngine(
        db_path=DB_PATH,
        connection=conn,
        collect_log_path=COLLECT_LOG_PATH,
        report_path=REPORT_PATH,
        health_loader=public_health,
    )


def performance_diagnostics() -> dict[str, Any]:
    context = current_context()
    return context.as_dict() if context is not None else latest_diagnostics()


def clear_report_cache() -> None:
    LIVE_HTML_CACHE.clear()


def report_cache_signature(settings: "Settings") -> dict[str, Any]:
    return perf_memoize("report_signature", "current", lambda: {
        "database": database_content_signature(),
        "settings": file_fingerprint(SETTINGS_PATH),
        "timeline": file_fingerprint(household_timeline_path()),
        "settings_hash": settings_fingerprint(settings),
    })


def database_content_signature() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"missing": True}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            household_row = conn.execute(
                """
                SELECT household_id
                FROM households
                WHERE is_default = 1
                ORDER BY household_id
                LIMIT 1
                """
            ).fetchone()
            household_id = household_row[0] if household_row else "default_household"
            readings = conn.execute(
                """
                SELECT COUNT(*), MIN(measurement_time), MAX(measurement_time)
                FROM readings
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            ev_sessions = conn.execute(
                """
                SELECT COUNT(*), MIN(start_time), MAX(start_time), MAX(COALESCE(last_seen, end_time, start_time))
                FROM ev_sessions
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            return {
                "readings": {
                    "count": int((readings or [0])[0] or 0),
                    "first": (readings or [None, None, None])[1],
                    "latest": (readings or [None, None, None])[2],
                },
                "ev_sessions": {
                    "count": int((ev_sessions or [0])[0] or 0),
                    "first": (ev_sessions or [None, None, None, None])[1],
                    "latest": (ev_sessions or [None, None, None, None])[2],
                    "last_seen": (ev_sessions or [None, None, None, None])[3],
                },
            }
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        return file_fingerprint(DB_PATH)


def period_payload(period: Period) -> dict[str, str]:
    return {
        "kind": period.kind,
        "label": period.label,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
    }


def request_cache_key(name: str, settings: "Settings", *parts: Any) -> dict[str, Any]:
    return {
        "name": name,
        "parts": parts,
        "signature": report_cache_signature(settings),
    }


def load_measurements(conn: sqlite3.Connection, start: date, end: date, settings: "Settings"):
    key = request_cache_key("measurements", settings, start.isoformat(), end.isoformat())

    def loader():
        measurements = data_engine(conn).get_measurements(start, end)
        perf_increment(
            "interval_rows_processed",
            sum(1 for item in measurements if item.measurement_type == MeasurementType.GRID_IMPORT),
        )
        return measurements

    with perf_stage("reading load"):
        return perf_memoize("measurements", key, loader)


def tariff_resolution_for_day(resolver: TariffResolver, day: date):
    key = {"day": day.isoformat(), "resolver": id(resolver)}

    def loader():
        if hasattr(resolver, "resolve_with_context"):
            return resolver.resolve_with_context(day)
        return None

    return perf_memoize("tariff_resolution", key, loader)


def tariff_for_resolved_day(resolver: TariffResolver, day: date):
    resolution = tariff_resolution_for_day(resolver, day)
    tariff = resolution.tariff if resolution is not None else resolver.resolve(day)
    return resolution, tariff


def tariff_updates_path() -> Path:
    if SETTINGS_PATH != DEFAULT_SETTINGS_PATH:
        return SETTINGS_PATH.parent / "tariff_updates.json"
    return PROJECT_ROOT / "data" / "runtime" / "tariff_updates.json"


def tariff_update_repository() -> TariffUpdateRepository:
    path = tariff_updates_path()
    legacy_path = SETTINGS_PATH.parent / "tariff_updates.json"
    if legacy_path.exists() and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
    return TariffUpdateRepository(path)


def historical_coverage_data(today: Optional[date] = None) -> dict[str, Any]:
    current_day = today or local_today()
    try:
        coverage = data_engine().get_historical_coverage()
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        coverage = {
            "providers": {},
            "measurement_totals": {},
            "historical_backfill_jobs": [],
            "quality_tiers": {},
        }
    return {
        "historical_coverage": coverage,
        "seasonal_history": seasonal_history_summary(coverage, today=current_day),
        "historical_backfill_jobs": coverage.get("historical_backfill_jobs", []),
        "solaredge_capability_audit": solaredge_capability_audit(current_day),
    }


def activate_demo_mode(today: Optional[date] = None):
    """Switch report paths to deterministic fictional demo data."""

    global DB_PATH, REPORT_PATH, SETTINGS_PATH, HOME_PROFILE_PATH, HOUSEHOLD_TIMELINE_PATH, COLLECT_LOG_PATH
    global DEMO_MODE, DEMO_WEATHER_PATH, DEMO_RECOMMENDATIONS_PATH

    from demo_mode import ensure_demo_environment
    import collector_health

    env = ensure_demo_environment(PROJECT_ROOT, today=today)
    DB_PATH = env.db_path
    REPORT_PATH = env.report_path
    SETTINGS_PATH = env.settings_path
    HOME_PROFILE_PATH = env.home_profile_path
    HOUSEHOLD_TIMELINE_PATH = env.settings_path.parent / "household_timeline.json"
    COLLECT_LOG_PATH = env.collect_log_path
    collector_health.STATE_PATH = env.collector_health_path
    collector_health.SETTINGS_PATH = env.settings_path
    DEMO_WEATHER_PATH = env.weather_path
    DEMO_RECOMMENDATIONS_PATH = env.recommendations_path
    DEMO_MODE = True
    return env


def configure_demo_mode(argv: Optional[list[str]] = None) -> bool:
    from demo_mode import demo_requested

    if not DEMO_MODE and demo_requested(argv):
        activate_demo_mode()
    return DEMO_MODE


def demo_metadata() -> dict:
    if not DEMO_MODE:
        return {}
    payload = {"enabled": True}
    if DEMO_WEATHER_PATH and DEMO_WEATHER_PATH.exists():
        payload["weather"] = json.loads(DEMO_WEATHER_PATH.read_text(encoding="utf-8"))
    if DEMO_RECOMMENDATIONS_PATH and DEMO_RECOMMENDATIONS_PATH.exists():
        payload["seed_recommendations"] = json.loads(DEMO_RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
    return payload


def financial_engine(household_context: Optional[HouseholdContext] = None) -> FinancialEngine:
    return FinancialEngine(household_context=household_context)


def household_timeline_path() -> Path:
    if HOUSEHOLD_TIMELINE_PATH == DEFAULT_HOUSEHOLD_TIMELINE_PATH and SETTINGS_PATH != DEFAULT_SETTINGS_PATH:
        return SETTINGS_PATH.parent / DEFAULT_HOUSEHOLD_TIMELINE_PATH.name
    return HOUSEHOLD_TIMELINE_PATH


def household_timeline(settings: Optional[Any] = None) -> HouseholdTimeline:
    return load_household_timeline(household_timeline_path(), settings or load_settings())


def household_timeline_store() -> HouseholdTimelineStore:
    path = household_timeline_path()
    audit_path = path.with_name("household_timeline_audit.jsonl")
    return HouseholdTimelineStore(path, audit_path)


def tariff_resolver(settings: Any) -> TariffResolver:
    return household_tariff_resolver(settings, household_timeline(settings))


def tariff_for_date(settings: Any, when: date) -> TariffVersion:
    return tariff_resolver(settings).resolve(when)


def tariff_metadata(settings: Any, when: date) -> dict[str, Any]:
    return tariff_for_date(settings, when).public_metadata()


def tariff_import_manager(settings: Any, repository: Optional[TariffUpdateRepository] = None) -> TariffImportManager:
    registry = TariffProviderRegistry([SynergyTariffProvider()])
    return TariffImportManager(
        registry=registry,
        active_store=tariff_resolver(settings).store,
        repository=repository or tariff_update_repository(),
    )


def check_tariff_updates(settings: Any, summary: Optional[Any] = None) -> dict[str, Any]:
    profile = usage_profile_from_summary(summary) if summary is not None else {}
    return tariff_import_manager(settings).check_for_updates("Synergy", usage_profile=profile)


def tariff_update_overview(settings: Any, summary: Optional[Any] = None) -> dict[str, Any]:
    repository = tariff_update_repository()
    active_tariff = tariff_for_date(settings, local_today())
    pending = repository.pending_updates()
    usage_profile = usage_profile_from_summary(summary) if summary is not None else {}
    for update in pending:
        if update.get("financial_preview", {}).get("available"):
            continue
        try:
            current = TariffVersion.from_dict(update["current_version"])
            pending_version = TariffVersion.from_dict(update["pending_version"])
            from engine.tariff_importing import financial_impact_preview

            update["financial_preview"] = financial_impact_preview(current, pending_version, usage_profile)
        except (KeyError, TypeError, ValueError):
            pass
    return {
        "current_active_tariff": active_tariff.as_dict(),
        "pending": pending,
        "diagnostics": repository.diagnostics(),
        "approved_versions": repository.approved_version_dicts(),
    }


def human_date(value: date) -> str:
    return f"{value.day} {value.strftime('%B %Y')}"


def charging_advisor(household_context: Optional[HouseholdContext] = None) -> ChargingAdvisor:
    return ChargingAdvisor(household_context=household_context)


def cost_advisor(household_context: Optional[HouseholdContext] = None) -> CostAdvisor:
    return CostAdvisor(household_context=household_context)


def solar_advisor(household_context: Optional[HouseholdContext] = None) -> SolarAdvisor:
    return SolarAdvisor(household_context=household_context)


def efficiency_advisor(household_context: Optional[HouseholdContext] = None) -> EfficiencyAdvisor:
    return EfficiencyAdvisor(household_context=household_context)


def briefing_engine(household_context: Optional[HouseholdContext] = None) -> BriefingEngine:
    return BriefingEngine(household_context=household_context)


def goals_engine(household_context: Optional[HouseholdContext] = None) -> GoalsEngine:
    return GoalsEngine(household_context=household_context)


def setup_engine(household_context: HouseholdContext) -> SetupEngine:
    return SetupEngine(household_context)


def setup_import_sources():
    from import_ocular_export import OcularEVImporter
    from import_synergy_bill import SynergyBillImporter

    return [OcularEVImporter(DB_PATH).source, SynergyBillImporter(DB_PATH).source]


def default_household_context() -> HouseholdContext:
    return HouseholdContext(
        account=Account("local_account", "Local account", "local"),
        household=Household(
            "default_household",
            "Default household",
            str(LOCAL_TIMEZONE),
            capabilities=[
                HouseholdCapability("solar", source="default"),
                HouseholdCapability("ev", source="default"),
                HouseholdCapability("tariff", source="default"),
                HouseholdCapability("billing", source="default"),
                HouseholdCapability("baseload", source="default"),
                HouseholdCapability("recommendations", source="default"),
            ],
            is_default=True,
        ),
        membership=HouseholdMembership("local_account", "default_household", "owner"),
    )


def report_household_context(conn: Optional[sqlite3.Connection] = None) -> HouseholdContext:
    try:
        return data_engine(conn).get_household_context()
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        return default_household_context()


EV_BUCKET_ORDER = ("super_off_peak", "overnight", "off_peak", "peak")
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}
DEFAULT_SETTINGS = {
    "state_or_territory": DEFAULT_STATE_OR_TERRITORY,
    "tariff_setup_mode": "catalog",
    "tariff_add_on": NO_ADD_ON,
    "supply_charge_per_day": DEFAULT_SUPPLY_CHARGE_PER_DAY,
    "import_rates": {
        "super_off_peak": 0.086151,
        "overnight": 0.193841,
        "off_peak": 0.236916,
        "peak": 0.538446,
    },
    "export_credit_rate": DEFAULT_EXPORT_CREDIT_RATE,
    "ev_baseline_watts": 250.0,
    "ev_data_source": "estimated",
    "home_assistant_url": "http://192.168.4.96:8123",
    "solar_setup_choice": "none",
    "ev_setup_choice": "none",
    "battery_setup_choice": "none",
    "battery_installed_costs": {
        "5": 6500.0,
        "10": 9500.0,
        "15": 12500.0,
        "20": 15500.0,
    },
    "battery_round_trip_efficiency": 0.90,
    "battery_life_years": 15,
    "battery_annual_degradation_pct": 2.0,
    "battery_electricity_inflation_pct": 3.0,
    "battery_discount_rate_pct": 0.0,
    "battery_government_rebate": 0.0,
    "billing_period_start_day": 1,
    "current_billing_period_start": None,
    "current_billing_period_end": None,
    "previous_billing_period_start": None,
    "previous_billing_period_end": None,
    "billing_cycle_configured": False,
    "retailer": "Synergy",
    "retailer_id": DEFAULT_RETAILER,
    "current_plan": DEFAULT_PLAN,
    "comparison_plan": "previous_flat_or_home_plan",
    "comparison_plan_rates": {
        "flat_import_rate": 0.30,
        "supply_charge_per_day": DEFAULT_SUPPLY_CHARGE_PER_DAY,
        "export_credit_rate": DEFAULT_EXPORT_CREDIT_RATE,
    },
    "petrol_price_per_litre": 1.80,
    "previous_car_l_per_100km": 8.0,
    "ev_kwh_per_100km": None,
    "ev_odometer_km": None,
    "car_comparison_enabled": False,
    "recommendation_thresholds": {
        "peak_cost_share_pct": 25.0,
        "peak_shift_kwh_per_day": 5.0,
        "solar_self_consumption_pct": 60.0,
        "ev_detected_min_kwh": 0.5,
        "baseload_excess_pct": 20.0,
        "high_cost_threshold": 300.0,
        "positive_peak_share_pct": 20.0,
        "positive_self_consumption_pct": 70.0,
    },
}


@dataclass
class Settings:
    state_or_territory: str
    tariff_setup_mode: str
    tariff_add_on: str
    supply_charge_per_day: float
    import_rates: dict
    export_credit_rate: float
    ev_baseline_watts: float
    ev_data_source: str
    home_assistant_url: str
    solar_setup_choice: str
    ev_setup_choice: str
    battery_setup_choice: str
    battery_installed_costs: dict
    battery_round_trip_efficiency: float
    battery_life_years: int
    battery_annual_degradation_pct: float
    battery_electricity_inflation_pct: float
    battery_discount_rate_pct: float
    battery_government_rebate: float
    billing_period_start_day: int
    current_billing_period_start: Optional[date]
    current_billing_period_end: Optional[date]
    previous_billing_period_start: Optional[date]
    previous_billing_period_end: Optional[date]
    billing_cycle_configured: bool
    retailer: str
    retailer_id: str
    current_plan: str
    comparison_plan: str
    comparison_plan_rates: dict
    petrol_price_per_litre: float
    previous_car_l_per_100km: float
    ev_kwh_per_100km: Optional[float]
    ev_odometer_km: Optional[float]
    car_comparison_enabled: bool
    recommendation_thresholds: dict
    home_profile: HomeProfile


@dataclass
class Summary:
    start: date
    end: date
    supply_days: int
    intervals: int
    reading_days: int
    grid_import_kwh: float
    import_cost: float
    supply_cost: float
    total_cost: float
    export_credit: float
    net_cost: float
    solar_generated_kwh: float
    solar_used_kwh: float
    solar_exported_kwh: float
    self_consumption_pct: float
    solar_export_pct: float
    peak_cost_share_pct: float
    ev_charging_kwh: float
    ev_charging_cost: float
    ev_charging_source: str
    actual_ev_charging_kwh: float
    actual_ev_charging_cost: float
    actual_ev_tariff_breakdown: dict
    actual_ev_cost_partial: bool
    ev_session_analytics: dict
    average_daily_grid_import_kwh: float
    average_daily_solar_generation_kwh: float
    average_daily_net_cost: float
    buckets: dict
    tariff_resolution: Optional[dict] = None


@dataclass
class DailyTrend:
    day: date
    grid_import_kwh: float
    solar_generated_kwh: float
    solar_exported_kwh: float
    import_cost: float
    export_credit: float
    net_cost: float
    ev_charging_kwh: float
    ev_charging_source: str


def health_data(conn: sqlite3.Connection, today: date, settings: Settings) -> dict:
    engine = data_engine(conn)
    return engine.get_connector_status(
        today,
        bill_period=engine.get_bill_period(today, settings),
    )


def summary_to_dict(summary: Summary) -> dict:
    result = asdict(summary)
    result["start"] = summary.start.isoformat()
    result["end"] = summary.end.isoformat()
    return result


def trend_to_dict(trend: DailyTrend) -> dict:
    result = asdict(trend)
    result["day"] = trend.day.isoformat()
    return result


def load_energy_timeline_readings(conn: sqlite3.Connection, day: date) -> list[dict]:
    engine = data_engine(conn)
    grid_rows = {row["measurement_time"]: row for row in engine.get_grid_intervals(day, day)}
    solar_rows = {row["measurement_time"]: row for row in engine.get_solar_intervals(day, day)}
    timestamps = sorted(set(grid_rows) | set(solar_rows))
    return [
        {
            "measurement_time": timestamp,
            "tariff_bucket": grid_rows.get(timestamp, {}).get("tariff_bucket"),
            "production_kwh": float(solar_rows.get(timestamp, {}).get("production_kwh") or 0.0),
            "solar_to_home_kwh": float(solar_rows.get(timestamp, {}).get("solar_to_home_kwh") or 0.0),
            "solar_to_grid_kwh": float(solar_rows.get(timestamp, {}).get("solar_to_grid_kwh") or 0.0),
            "grid_to_home_w": float(grid_rows.get(timestamp, {}).get("grid_to_home_w") or 0.0),
            "grid_to_home_kwh": float(grid_rows.get(timestamp, {}).get("grid_to_home_kwh") or 0.0),
        }
        for timestamp in timestamps
    ]


def build_energy_timeline(conn: sqlite3.Connection, day: date, settings: Settings, now: Optional[datetime] = None) -> EnergyTimeline:
    readings = load_energy_timeline_readings(conn, day)
    sessions = [session for session in load_actual_ev_sessions(conn, day, day, settings) if completed_ev_session(session)]
    return EnergyTimelineEngine().build(
        day=day,
        readings=readings,
        ev_sessions=sessions,
        settings=settings,
        now=now or datetime.now(LOCAL_TIMEZONE),
    )


def local_today() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def runtime_check_data() -> dict:
    try:
        host, port = web_bind_for_runtime_check()
    except Exception:
        host, port = None, None
    try:
        settings = load_settings()
        active_tariff = tariff_metadata(settings, local_today())
    except Exception:
        active_tariff = None
    try:
        health_summary = public_health()
    except Exception:
        health_summary = {}
    return runtime_diagnostics(
        project_root=PROJECT_ROOT,
        database_path=DB_PATH,
        report_path=REPORT_PATH,
        settings_path=SETTINGS_PATH,
        active_tariff_version=active_tariff,
        health_summary=health_summary,
        web_host=host,
        web_port=port,
        working_directory=Path.cwd(),
    )


def web_bind_for_runtime_check() -> tuple[str, int]:
    config = {}
    if SETTINGS_PATH.exists():
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            web_server = raw.get("web_server") if isinstance(raw.get("web_server"), dict) else {}
            config = {
                "host": web_server.get("host") or raw.get("web_host"),
                "port": web_server.get("port") or raw.get("web_port"),
            }
    host = os.environ.get("POWMONITOR_HOST") or config.get("host") or "0.0.0.0"
    raw_port = os.environ.get("POWMONITOR_PORT") or config.get("port") or 8765
    return str(host), int(raw_port)


def latest_bill_period_bounds(bill: dict) -> tuple[Optional[date], Optional[date]]:
    starts = []
    ends = []
    for period in bill.get("periods") or []:
        try:
            if period.get("start_date"):
                starts.append(date.fromisoformat(str(period["start_date"])))
            if period.get("end_date"):
                ends.append(date.fromisoformat(str(period["end_date"])))
        except ValueError:
            continue
    return (min(starts) if starts else None, max(ends) if ends else None)


def reconcile_latest_synergy_bill(
    conn: sqlite3.Connection,
    engine: DataEngine,
    settings: Settings,
    household_context: HouseholdContext,
) -> Optional[dict]:
    try:
        bill = engine.get_latest_synergy_bill()
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        return None
    if not bill:
        return None
    period_start, period_end = latest_bill_period_bounds(bill)
    if period_start is None or period_end is None:
        return None
    try:
        estimate = load_summary(conn, period_start, period_end, settings)
        reconciliation = BillReconciliationEngine(financial_engine(household_context)).reconcile(
            bill,
            estimate,
            settings,
        )
        engine.persist_bill_reconciliation(reconciliation)
        return engine.get_latest_bill_reconciliation()
    except (RuntimeError, sqlite3.Error, ValueError):
        return None


def dashboard_data(
    today: Optional[date] = None,
    period_kind: str = "current_billing_period",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    current_day = today or local_today()
    settings = load_settings()
    selected = resolve_period(period_kind, settings, current_day, start, end)
    if current_context() is None:
        with performance_context(period_payload(selected)):
            return dashboard_data(current_day, period_kind, start, end)
    billing = resolve_period("current_billing_period", settings, current_day)
    compared = (
        resolve_period("previous_billing_period", settings, current_day)
        if selected.kind == "current_billing_period"
        else previous_equivalent(selected)
    )
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    with perf_stage("database connection/setup"):
        conn_context = sqlite3.connect(DB_PATH)
    with conn_context as conn:
        validate_database(conn)
        context_engine = data_engine(conn)
        household_context = context_engine.get_household_context()
        today_summary = load_summary(conn, current_day, current_day, settings)
        period_summary = load_summary(conn, selected.start, selected.end, settings)
        battery_intervals = battery_dispatch_intervals(context_engine, selected.start, selected.end, settings)
        seasonal_battery_intervals = battery_historical_dispatch_intervals(
            context_engine,
            selected.end - timedelta(days=364),
            selected.end,
            settings,
        )
        billing_summary = (
            period_summary
            if selected.start == billing.start and selected.end == billing.end
            else load_summary(conn, billing.start, billing.end, settings)
        )
        compared_summary = load_summary(conn, compared.start, compared.end, settings)
        trends = load_daily_trends(conn, selected.start, selected.end, settings)
        compared_trends = load_daily_trends(conn, compared.start, compared.end, settings)
        energy_timeline = build_energy_timeline(conn, current_day, settings, now=datetime.now(LOCAL_TIMEZONE))
        with perf_stage("provider/health diagnostics"):
            health = health_data(conn, current_day, settings)
        capability_summary = context_engine.get_household_capability_summary(current_day, settings)
        all_recommendations = load_recommendations(conn, period_summary, settings, household_context)
        recommendations = active_recommendations(all_recommendations)
        hidden = hidden_recommendations(all_recommendations)
        recommendation_lifecycle_counts = context_engine.get_recommendation_lifecycle_counts()
        behaviour_start = current_day - timedelta(days=89)
        household_behaviour = context_engine.get_household_behaviour(
            behaviour_start,
            current_day,
            ev_sources=actual_ev_sources(settings),
        )
        comparison = compare_summaries(selected, period_summary, compared, compared_summary)
        overnight_baseload_watts = context_engine.get_overnight_baseload_watts(selected.start, selected.end)
        with perf_stage("provider/health diagnostics"):
            provider_observations = context_engine.get_provider_observation_summary()
            promotion_summary = context_engine.promote_provider_observations()
        actual_bill_comparison = reconcile_latest_synergy_bill(conn, context_engine, settings, household_context)
        bill_reconciliation_summary = context_engine.get_bill_reconciliation_summary()
    scenarios = scenario_analysis(
        period_summary,
        settings,
        battery_intervals=battery_intervals,
        seasonal_battery_intervals=seasonal_battery_intervals,
    )
    completeness = data_completeness(period_summary, settings)
    plan = plan_comparison(period_summary, settings)
    energy_score = build_energy_score(
        period_summary,
        recommendations,
        completeness,
        plan,
    )
    glance_projected, glance_end, glance_basis, _glance_impact = financial_engine(household_context).project_bill(
        billing_summary,
        settings,
    )
    comparison_previous_summary = comparison.get("summary_b") if comparison else None
    bill_change = financial_engine(household_context).explain_bill_change(
        billing_summary,
        comparison_previous_summary,
        settings,
        current_bill=glance_projected,
        previous_bill=(
            comparison_previous_summary.get("net_cost")
            if isinstance(comparison_previous_summary, dict)
            else getattr(comparison_previous_summary, "net_cost", None)
            if comparison_previous_summary is not None
            else None
        ),
        projected=True,
    )
    if actual_bill_comparison is None:
        try:
            actual_bill_comparison = data_engine().compare_projected_to_latest_synergy_bill(glance_projected)
            bill_reconciliation_summary = data_engine().get_bill_reconciliation_summary()
        except (FileNotFoundError, RuntimeError, sqlite3.Error):
            actual_bill_comparison = None
            bill_reconciliation_summary = {"latest": None, "history": [], "average_absolute_variance": None}
    generated_at = datetime.now().astimezone()
    runtime_check = runtime_check_data()
    import_sources = setup_import_sources()
    provider_registry = build_provider_registry(
        settings=settings,
        health=health,
        capability_summary=capability_summary,
        import_sources=import_sources,
    )
    provider_management = build_provider_management(
        provider_registry=provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        settings=settings,
    )
    provider_runtime = provider_runtime_data()
    household_state = HouseholdStateEngine().build(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=settings,
        health=health,
        capability_summary=capability_summary,
        provider_management=provider_management,
        provider_runtime=provider_runtime,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        household_id=household_context.household.household_id,
        now=generated_at,
    )
    household_current_context = ContextEngine().build(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=settings,
        health=health,
        now=generated_at,
        household_state=household_state,
    )
    snapshot_engine = data_engine()
    state_snapshot = snapshot_engine.persist_household_state_snapshot(
        household_state,
        context=household_current_context,
    )
    latest_state_snapshot = snapshot_engine.get_latest_household_state_snapshot()
    recent_state_snapshots = snapshot_engine.get_recent_household_state_snapshots(limit=5)
    state_change_detection = snapshot_engine.detect_and_persist_state_changes()
    state_change_summary = snapshot_engine.get_state_change_summary()
    recent_state_changes = snapshot_engine.get_recent_state_changes(limit=10)
    notification_preferences = snapshot_engine.get_notification_preferences()
    notification_history = snapshot_engine.get_recent_notification_history(limit=50)
    behaviour_end = current_day
    household_behaviour = snapshot_engine.get_household_behaviour(
        behaviour_end - timedelta(days=89),
        behaviour_end,
        ev_sources=actual_ev_sources(settings),
    )
    household_anomalies = snapshot_engine.get_household_anomalies(
        household_state=household_state,
        context=household_current_context,
        state_changes=recent_state_changes,
        behaviour=household_behaviour,
        current_observations={
            "overnight_baseload_watts": overnight_baseload_watts,
            "current_daily_cost": period_summary.average_daily_net_cost,
            "normal_daily_cost": compared_summary.average_daily_net_cost,
        },
    )
    household_decision_output = snapshot_engine.get_household_decisions(
        household_state=household_state,
        context=household_current_context,
        behaviour=household_behaviour,
        anomalies=household_anomalies,
        capability_summary=capability_summary,
        financial_metadata={
            "projected_bill": glance_projected,
            "high_cost_threshold": settings.recommendation_thresholds.get("high_cost_threshold"),
        },
        provider_health=provider_management,
        state_changes=recent_state_changes,
        now=generated_at,
    )
    household_decisions = household_decision_output["decisions"]
    decision_summary = household_decision_output["summary"]
    timeline_candidates = build_decision_timeline(
        period_summary,
        recommendations,
        comparison,
        include_billing_period=selected.kind == "current_billing_period",
        household_decisions=household_decisions,
    )
    timeline = snapshot_engine.persist_timeline_entries(timeline_candidates.entries)
    charging_advice = build_charging_advice(
        today_summary,
        billing_summary,
        settings,
        health,
        recommendations,
        glance_projected,
        generated_at,
        household_context,
        household_current_context,
    )
    cost = build_cost_advisor(
        period_summary,
        compared_summary,
        settings,
        health,
        recommendations,
        timeline,
        comparison,
        generated_at,
        household_context,
        household_current_context,
    )
    solar = build_solar_advisor(
        period_summary,
        settings,
        health,
        recommendations,
        timeline,
        generated_at,
        household_context,
        household_current_context,
    )
    efficiency = build_efficiency_advisor(
        period_summary,
        compared_summary,
        settings,
        health,
        recommendations,
        timeline,
        comparison,
        trends,
        overnight_baseload_watts,
        generated_at,
        household_context,
        household_current_context,
    )
    advisors = AdvisorCollection(
        attach_decision_to_advisors([charging_advice.advisor, cost, solar, efficiency], household_decisions),
        household=household_context.household,
        capability_summary=capability_summary,
    )
    decision_advisors = explain_household_decisions(household_decisions, existing_advisors=advisors.active())
    advisor_explanations = AdvisorCollection(
        decision_advisors,
        household=household_context.household,
        capability_summary=capability_summary,
    )
    decision_pipeline = build_decision_pipeline_output(
        household_decisions,
        advisor_explanations=advisor_explanations.active(),
        household_state=household_state,
        context=household_current_context,
        behaviour=household_behaviour,
    )
    recommendation_presentations = decision_pipeline.recommendations
    household_insights = HouseholdInsightsEngine().build(
        trends=trends,
        current_summary=period_summary,
        billing_summary=billing_summary,
        comparison=comparison,
        projected_bill=glance_projected,
        recommendations=recommendation_presentations,
    )
    household_trends = TrendsEngine().build(
        current_summary=period_summary,
        previous_summary=comparison_previous_summary,
        current_trends=trends,
        previous_trends=compared_trends,
        comparison_label=str(comparison.get("period_b", {}).get("label") or "previous period"),
        insights=household_insights,
        recommendations=recommendation_presentations,
    )
    briefing = briefing_engine(household_context).build(
        advisor_explanations,
        current_day,
        context=household_current_context,
        recommendation_presentations=recommendation_presentations,
    )
    goals = goals_engine(household_context).evaluate(advisors)
    setup_checklist = setup_engine(household_context).build_checklist(
        capability_summary,
        collector_health=health,
        import_sources=import_sources,
        advisors=advisors,
        goals=goals,
        recommendations=recommendations,
        settings=settings,
        provider_registry=provider_registry,
        provider_management=provider_management,
        now=generated_at,
    )
    notification_readiness = NotificationEngine().build(
        state_changes=recent_state_changes,
        household_state_snapshot=latest_state_snapshot,
        context=household_current_context,
        household_decisions=household_decisions,
        recommendation_presentations=recommendation_presentations,
        advisors=None,
        setup=setup_checklist,
        preferences=notification_preferences,
        history=notification_history,
        now=generated_at,
    )
    notification_readiness_summary = notification_readiness.as_dict()
    snapshot_engine.persist_notification_history(notification_readiness_summary["candidates"])
    notification_history_summary = snapshot_engine.get_notification_history_summary()
    display_timeline = timeline_with_state_changes(timeline, recent_state_changes)
    tariff_updates = tariff_update_overview(settings, billing_summary)
    household_timeline_payload = household_timeline(settings).public_dict(today=current_day)
    historical_data = historical_coverage_data(current_day)
    glance_recommendation = next(
        (item for item in recommendation_presentations if item.category.value == "ev"),
        recommendation_presentations[0] if recommendation_presentations else None,
    )
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "household": household_context.household.as_dict(),
        "account": household_context.account.as_dict(),
        "household_membership": household_context.membership.as_dict(),
        "capability_summary": capability_summary.as_dict(),
        "provider_registry": provider_registry.as_dict(),
        "provider_status": provider_registry.status_by_provider(),
        "provider_management": provider_management.as_dict(),
        "providers": provider_management.as_dict(),
        "provider_runtime": provider_runtime.as_dict(),
        "runtime": provider_runtime.as_dict(),
        "runtime_check": runtime_check,
        "household_state": household_state.as_dict(),
        "household_behaviour": household_behaviour.as_dict(),
        "household_anomalies": household_anomalies,
        "household_decisions": household_decisions,
        "decision_summary": decision_summary,
        "latest_household_state_snapshot": latest_state_snapshot,
        "recent_household_state_snapshots": recent_state_snapshots,
        "household_state_snapshot": state_snapshot,
        "state_change_detection": state_change_detection,
        "state_change_summary": state_change_summary,
        "recent_state_changes": recent_state_changes,
        "notification_preferences": notification_preferences.as_dict(),
        "notification_history": notification_history_summary,
        "notification_candidates": notification_readiness_summary["candidates"],
        "notification_readiness_summary": notification_readiness_summary,
        "provider_observations": provider_observations,
        "promotion_summary": promotion_summary,
        "conflict_summary": promotion_summary["conflict_summary"],
        "latest_promotions": promotion_summary["latest_promotions"],
        "context": household_current_context.as_dict(),
        "ev_data_source": settings.ev_data_source,
        "today": summary_to_dict(today_summary),
        "period": {"kind": selected.kind, "label": selected.label, "start": selected.start.isoformat(), "end": selected.end.isoformat()},
        "summary": summary_to_dict(period_summary),
        "current_billing_period": summary_to_dict(billing_summary),
        "actual_ev_charging_kwh": period_summary.actual_ev_charging_kwh,
        "actual_ev_charging_cost": period_summary.actual_ev_charging_cost,
        "actual_ev_tariff_breakdown": period_summary.actual_ev_tariff_breakdown,
        "actual_ev_cost_partial": period_summary.actual_ev_cost_partial,
        "ev_session_analytics": period_summary.ev_session_analytics,
        "daily_trends": [trend_to_dict(trend) for trend in trends],
        "household_insights": [insight.as_dict() for insight in household_insights],
        "energy_timeline": energy_timeline.as_dict(),
        "household_trends": household_trends.as_dict(),
        "tariff_scenario": tariff_scenario(period_summary, settings),
        "plan_comparison": plan,
        "current_tariff": tariff_metadata(settings, selected.end),
        "tariff_updates": tariff_updates,
        "household_timeline": household_timeline_payload,
        "tariff_resolution": period_summary.tariff_resolution or {},
        "historical_coverage": historical_data["historical_coverage"],
        "seasonal_history": historical_data["seasonal_history"],
        "historical_backfill_jobs": historical_data["historical_backfill_jobs"],
        "solaredge_capability_audit": historical_data["solaredge_capability_audit"],
        "bill_change_explanation": bill_change,
        "actual_bill_comparison": actual_bill_comparison,
        "bill_reconciliation": actual_bill_comparison,
        "bill_reconciliation_summary": bill_reconciliation_summary,
        "latest_synergy_bill": actual_bill_comparison.get("latest_bill") if actual_bill_comparison else None,
        "scenario_analysis": scenarios,
        "billing_scenarios": scenarios["scenarios"],
        "battery_recommendation": scenarios.get("battery_recommendation"),
        "battery_comparison": scenarios["battery_comparison"],
        "battery_investment": scenarios["battery_investment"],
        "timeline": display_timeline.as_dict(),
        "comparison": comparison,
        "advisors": advisors.as_dict(),
        "advisor_explanations": advisor_explanations.as_dict(),
        "daily_briefing": briefing.as_dict(),
        "goals": [goal.as_dict() for goal in goals],
        "setup_progress": setup_checklist.progress.as_dict(),
        "setup_tasks": [task.as_dict() for task in setup_checklist.tasks],
        "setup_sections": [section.as_dict() for section in setup_checklist.sections],
        "setup_checklist": setup_checklist.as_dict(),
        "charging_advice": charging_advice.as_dict(),
        "glance": build_glance_state(
            billing_summary=billing_summary,
            charging_advice=charging_advice,
            energy_score=energy_score,
            projected_bill=glance_projected,
            bill_projection_basis=glance_basis,
            now=generated_at,
            context=household_current_context,
            recommendation_presentation=glance_recommendation,
        ).as_dict(),
        "ev_source_counts": ev_source_counts(),
        "data_completeness": completeness,
        "energy_score": energy_score.as_dict(),
        "car_comparison": car_comparison(settings),
        "recommendations": [item.as_dict() for item in recommendation_presentations],
        "hidden_recommendations": [],
        "recommendation_lifecycle_counts": recommendation_lifecycle_counts,
        "health": health,
        "performance_diagnostics": performance_diagnostics(),
        **({"demo": demo_metadata()} if DEMO_MODE else {}),
    }


def provider_management_data() -> ProviderManager:
    from collect_today import SolarEdgeAdapter
    from home_assistant_provider import HomeAssistantAdapter

    settings = load_settings()
    current_day = local_today()
    with sqlite3.connect(DB_PATH) as conn:
        validate_database(conn)
        engine = data_engine(conn)
        capability_summary = engine.get_household_capability_summary(current_day, settings)
        health = health_data(conn, current_day, settings)
        provider_observations = engine.get_provider_observation_summary()
        promotion_summary = engine.promote_provider_observations()
    provider_registry = build_provider_registry(
        settings=settings,
        health=health,
        capability_summary=capability_summary,
        import_sources=setup_import_sources(),
    )
    solaredge_adapter = SolarEdgeAdapter()
    home_assistant_record = load_provider_mapping_record("home_assistant")
    home_assistant_adapter = HomeAssistantAdapter(entity_mappings=home_assistant_record.get("selected_mappings") if isinstance(home_assistant_record, dict) else None)
    runtime = ProviderRuntime([solaredge_adapter, home_assistant_adapter])
    return build_provider_management(
        provider_registry=provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        runtime_status=runtime.as_dict(),
        connection_testers={
            "solaredge": solaredge_adapter.test_connection,
            "home_assistant": home_assistant_adapter.test_connection,
        },
        reconnect_handlers={
            "solaredge": solaredge_adapter.reconnect,
        },
        provider_entity_mappings={"home_assistant": home_assistant_record},
        settings=settings,
    )


def provider_runtime_data(data_engine_instance=None) -> ProviderRuntime:
    from collect_today import SolarEdgeAdapter
    from home_assistant_provider import HomeAssistantAdapter

    home_assistant_record = load_provider_mapping_record("home_assistant")
    return ProviderRuntime(
        [SolarEdgeAdapter(), HomeAssistantAdapter(entity_mappings=home_assistant_record.get("selected_mappings") if isinstance(home_assistant_record, dict) else None)],
        data_engine=data_engine_instance,
    )


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_settings() -> Settings:
    configure_demo_mode()
    raw = DEFAULT_SETTINGS
    if SETTINGS_PATH.exists():
        with SETTINGS_PATH.open("r", encoding="utf-8") as settings_file:
            loaded = json.load(settings_file)
        if not isinstance(loaded, dict):
            raise ValueError(f"Settings must be a JSON object: {SETTINGS_PATH}")
        raw = {**DEFAULT_SETTINGS, **loaded}
        raw["import_rates"] = {**DEFAULT_SETTINGS["import_rates"], **loaded.get("import_rates", {})}
        raw["recommendation_thresholds"] = {
            **DEFAULT_SETTINGS["recommendation_thresholds"],
            **loaded.get("recommendation_thresholds", {}),
        }
        raw["comparison_plan_rates"] = {
            **DEFAULT_SETTINGS["comparison_plan_rates"],
            **loaded.get("comparison_plan_rates", {}),
        }
        raw["battery_installed_costs"] = {
            **DEFAULT_SETTINGS["battery_installed_costs"],
            **loaded.get("battery_installed_costs", {}),
        }
    raw = apply_structured_tariff(raw)
    if raw.get("current_plan") == "synergy_ev_add_on":
        raw["comparison_plan_rates"] = {
            **raw.get("comparison_plan_rates", {}),
            **comparison_rates_for_plan("previous_flat_or_home_plan"),
        }

    rates = {bucket: float(raw["import_rates"][bucket]) for bucket in BUCKET_ORDER}

    def optional_date(name: str) -> Optional[date]:
        value = raw.get(name)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be an ISO date in YYYY-MM-DD format")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO date in YYYY-MM-DD format") from exc

    settings = Settings(
        state_or_territory=str(raw.get("state_or_territory") or DEFAULT_STATE_OR_TERRITORY),
        tariff_setup_mode=str(raw.get("tariff_setup_mode") or "catalog"),
        tariff_add_on=str(raw.get("tariff_add_on") or NO_ADD_ON),
        supply_charge_per_day=float(raw["supply_charge_per_day"]),
        import_rates=rates,
        export_credit_rate=float(raw["export_credit_rate"]),
        ev_baseline_watts=float(raw["ev_baseline_watts"]),
        ev_data_source=str(raw["ev_data_source"]),
        home_assistant_url=str(raw["home_assistant_url"]),
        solar_setup_choice=str(raw.get("solar_setup_choice") or "none"),
        ev_setup_choice=str(raw.get("ev_setup_choice") or "none"),
        battery_setup_choice=str(raw.get("battery_setup_choice") or "none"),
        battery_installed_costs={
            str(key): float(value)
            for key, value in raw.get("battery_installed_costs", {}).items()
        },
        battery_round_trip_efficiency=float(raw.get("battery_round_trip_efficiency", 0.90)),
        battery_life_years=int(raw.get("battery_life_years", 15)),
        battery_annual_degradation_pct=float(raw.get("battery_annual_degradation_pct", 2.0)),
        battery_electricity_inflation_pct=float(raw.get("battery_electricity_inflation_pct", 3.0)),
        battery_discount_rate_pct=float(raw.get("battery_discount_rate_pct", 0.0)),
        battery_government_rebate=float(raw.get("battery_government_rebate", 0.0) or 0.0),
        billing_period_start_day=int(raw["billing_period_start_day"]),
        current_billing_period_start=optional_date("current_billing_period_start"),
        current_billing_period_end=optional_date("current_billing_period_end"),
        previous_billing_period_start=optional_date("previous_billing_period_start"),
        previous_billing_period_end=optional_date("previous_billing_period_end"),
        billing_cycle_configured=bool(raw.get("billing_cycle_configured")),
        retailer=str(raw.get("retailer") or ""),
        retailer_id=str(raw.get("retailer_id") or raw.get("retailer") or ""),
        current_plan=str(raw["current_plan"]),
        comparison_plan=str(raw["comparison_plan"]),
        comparison_plan_rates={
            key: float(value) for key, value in raw["comparison_plan_rates"].items()
        },
        petrol_price_per_litre=float(raw["petrol_price_per_litre"]),
        previous_car_l_per_100km=float(raw["previous_car_l_per_100km"]),
        ev_kwh_per_100km=float(raw["ev_kwh_per_100km"]) if raw.get("ev_kwh_per_100km") is not None else None,
        ev_odometer_km=float(raw["ev_odometer_km"]) if raw.get("ev_odometer_km") is not None else None,
        car_comparison_enabled=bool(raw["car_comparison_enabled"]),
        recommendation_thresholds={
            key: float(value) for key, value in raw["recommendation_thresholds"].items()
        },
        home_profile=load_home_profile(HOME_PROFILE_PATH, settings=raw),
    )
    try:
        settings.approved_tariff_versions = tariff_update_repository().approved_version_dicts()
    except (OSError, ValueError, json.JSONDecodeError):
        settings.approved_tariff_versions = []
    if settings.supply_charge_per_day < 0 or settings.export_credit_rate < 0:
        raise ValueError("Supply charge and export credit rate cannot be negative")
    if settings.ev_baseline_watts < 0 or any(rate < 0 for rate in settings.import_rates.values()):
        raise ValueError("Import rates and EV baseline cannot be negative")
    if settings.ev_data_source not in {"estimated", "ocular_csv", "ocpp", "home_assistant_ocpp", "ocular_export"}:
        raise ValueError("ev_data_source must be estimated, ocular_csv, ocpp, home_assistant_ocpp, or ocular_export")
    if not 1 <= settings.billing_period_start_day <= 28:
        raise ValueError("billing_period_start_day must be from 1 to 28")
    if bool(settings.previous_billing_period_start) != bool(settings.previous_billing_period_end):
        raise ValueError("Previous billing period start and end must both be set")
    if (
        settings.current_billing_period_start
        and settings.current_billing_period_end
        and settings.current_billing_period_start > settings.current_billing_period_end
    ):
        raise ValueError("Current billing period start cannot be after its end")
    if (
        settings.previous_billing_period_start
        and settings.previous_billing_period_start > settings.previous_billing_period_end
    ):
        raise ValueError("Previous billing period start cannot be after its end")
    if any(value < 0 for value in settings.recommendation_thresholds.values()):
        raise ValueError("Recommendation thresholds cannot be negative")
    if any(value < 0 for value in settings.comparison_plan_rates.values()):
        raise ValueError("Comparison plan rates cannot be negative")
    if any(value < 0 for value in settings.battery_installed_costs.values()):
        raise ValueError("Battery installed costs cannot be negative")
    if settings.battery_life_years < 1:
        raise ValueError("Battery life must be at least 1 year")
    if settings.battery_round_trip_efficiency <= 0:
        raise ValueError("Battery round-trip efficiency must be positive")
    if settings.battery_government_rebate < 0:
        raise ValueError("Battery government rebate cannot be negative")
    return settings


def billing_period(today: date, settings: Settings) -> tuple[date, date]:
    period = resolve_period("current_billing_period", settings, today)
    return period.start, period.end


def backfill_ev_tariff_columns(conn: sqlite3.Connection) -> None:
    DataEngine._backfill_ev_tariff_columns(conn)


def validate_database(conn: sqlite3.Connection) -> None:
    DataEngine.validate_connection(conn)


def cost_sql(settings: Settings) -> tuple[str, list]:
    parts = []
    params = []
    for bucket in BUCKET_ORDER:
        parts.append("WHEN tariff_bucket = ? THEN COALESCE(grid_to_home_kwh, 0) * ?")
        params.extend((bucket, settings.import_rates[bucket]))
    return "CASE " + " ".join(parts) + " ELSE 0 END", params


def empty_ev_breakdown() -> dict:
    return {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in BUCKET_ORDER}


def actual_ev_sources(settings: Settings) -> list[str]:
    if settings.ev_data_source in {"ocular_export", "home_assistant_ocpp"}:
        return ["ocular_export", "home_assistant_ocpp"]
    return []


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time_from_minutes(minutes: Optional[float]) -> Optional[str]:
    if minutes is None:
        return None
    rounded = int(round(minutes)) % (24 * 60)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def average_clock_time(datetimes: list[datetime]) -> Optional[str]:
    if not datetimes:
        return None
    angles = [
        ((value.hour * 60 + value.minute + value.second / 60) / (24 * 60)) * 2 * math.pi
        for value in datetimes
    ]
    sin_sum = sum(math.sin(angle) for angle in angles)
    cos_sum = sum(math.cos(angle) for angle in angles)
    if abs(sin_sum) < 1e-9 and abs(cos_sum) < 1e-9:
        return None
    mean_angle = math.atan2(sin_sum, cos_sum)
    if mean_angle < 0:
        mean_angle += 2 * math.pi
    return format_time_from_minutes(mean_angle / (2 * math.pi) * 24 * 60)


def format_duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "n/a"
    total = max(int(round(minutes)), 0)
    hours, remainder = divmod(total, 60)
    return f"{hours}h {remainder:02d}m"


def session_brief(session: dict) -> Optional[dict]:
    if not session:
        return None
    return {
        "transaction_id": session.get("transaction_id"),
        "date": session.get("date"),
        "start_time": session.get("start_time_display"),
        "end_time": session.get("end_time_display"),
        "duration_minutes": session.get("duration_minutes"),
        "energy_kwh": session.get("energy_kwh"),
        "cost": session.get("cost"),
        "cost_per_kwh": session.get("cost_per_kwh"),
        "dominant_tariff": session.get("dominant_tariff"),
        "max_power_kw": session.get("max_power_kw"),
        "max_power_source": session.get("max_power_source"),
    }


def load_actual_ev_sessions(
    conn: sqlite3.Connection, start: date, end: date, settings: Settings
) -> list[dict]:
    sources = actual_ev_sources(settings)
    key = request_cache_key("ev_sessions", settings, start.isoformat(), end.isoformat(), tuple(sources))

    def loader():
        with perf_stage("EV session/history loading"):
            sessions = data_engine(conn).get_ev_sessions(start, end, sources=sources)
        perf_increment("ev_sessions_loaded", len(sessions))
        return sessions

    return perf_memoize("ev_sessions", key, loader)


def completed_ev_session(session: dict) -> bool:
    return float(session.get("energy_kwh") or 0) > 0 and bool(session.get("end_time"))


def load_completed_ev_sessions(
    conn: sqlite3.Connection, start: date, end: date, settings: Settings
) -> list[dict]:
    return [
        session
        for session in load_actual_ev_sessions(conn, start, end, settings)
        if completed_ev_session(session)
    ]


def daily_ev_history_from_sessions(sessions: list[dict]) -> dict[str, dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}
    for session in sessions:
        day_text = str(session.get("date") or session.get("start_time") or "")[:10]
        if not day_text:
            continue
        row = daily.setdefault(
            day_text,
            {
                "energy_kwh": 0.0,
                "cost": 0.0,
                "source": session.get("source") or "unknown",
                "sessions": 0,
            },
        )
        row["energy_kwh"] += float(session.get("energy_kwh") or 0.0)
        row["cost"] += float(session.get("cost") or 0.0)
        row["sessions"] += 1
        if session.get("source") == "ocular_export":
            row["source"] = "ocular_export"
    return daily


def load_daily_ev_history(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    settings: Settings,
    estimated_by_day: Optional[dict[str, float]] = None,
) -> dict[str, dict[str, Any]]:
    with perf_stage("EV session/history loading"):
        if actual_ev_sources(settings):
            return daily_ev_history_from_sessions(load_completed_ev_sessions(conn, start, end, settings))
        resolver = tariff_resolver(settings)
        return {
            day_text: {
                "energy_kwh": float(value or 0.0),
                "cost": financial_engine().cost_of_ev_session(
                    float(value or 0.0),
                    tariff_for_resolved_day(resolver, date.fromisoformat(day_text))[1].usage_rate("overnight"),
                ),
                "source": "estimated",
                "sessions": 0,
            }
            for day_text, value in (estimated_by_day or {}).items()
        }


def ev_session_analytics(
    sessions: list[dict],
    settings: Settings,
    ignored_incomplete_sessions: int = 0,
) -> dict:
    if not sessions:
        return {
            "available": False,
            "overview": {},
            "tariff_usage": empty_ev_breakdown(),
            "insights": {},
            "recommendations": [],
            "sessions": [],
            "ignored_incomplete_sessions": ignored_incomplete_sessions,
            "ignored_incomplete_message": (
                "Ocular may only export completed sessions, so today's active charge may appear after the next export."
                if ignored_incomplete_sessions else ""
            ),
        }

    total_kwh = sum(session["energy_kwh"] for session in sessions)
    total_cost = sum(session["cost"] for session in sessions)
    tariff_usage = empty_ev_breakdown()
    for session in sessions:
        for bucket in BUCKET_ORDER:
            tariff_usage[bucket]["kwh"] += session["tariff_breakdown"][bucket]["kwh"]
            tariff_usage[bucket]["cost"] += session["tariff_breakdown"][bucket]["cost"]
    for bucket in BUCKET_ORDER:
        tariff_usage[bucket]["pct"] = tariff_usage[bucket]["kwh"] / total_kwh * 100 if total_kwh else 0.0
        tariff_usage[bucket]["label"] = EV_BUCKET_LABELS.get(bucket, bucket)

    starts = [parse_iso_datetime(session["start_time"]) for session in sessions]
    finishes = [parse_iso_datetime(session["end_time"]) for session in sessions]
    starts = [value for value in starts if value]
    finishes = [value for value in finishes if value]
    cheapest = min(sessions, key=lambda session: session["cost"]) if sessions else None
    expensive = max(sessions, key=lambda session: session["cost"]) if sessions else None
    duration_sessions = [session for session in sessions if session.get("duration_minutes") is not None]
    power_sessions = [
        session for session in sessions
        if session.get("max_power_kw") is not None and session.get("max_power_source") == "provided"
    ]
    longest = max(duration_sessions, key=lambda session: session["duration_minutes"]) if duration_sessions else None
    highest_power = max(power_sessions, key=lambda session: session["max_power_kw"]) if power_sessions else None

    history = sorted(sessions, key=lambda session: session.get("start_time") or "", reverse=True)
    return {
        "available": True,
        "overview": {
            "total_kwh": total_kwh,
            "total_cost": total_cost,
            "average_session_cost": total_cost / len(sessions),
            "average_session_kwh": total_kwh / len(sessions),
            "average_effective_cost_per_kwh": total_cost / total_kwh if total_kwh else 0.0,
        },
        "tariff_usage": tariff_usage,
        "insights": {
            "cheapest_session": session_brief(cheapest),
            "most_expensive_session": session_brief(expensive),
            "longest_session": session_brief(longest),
            "highest_power_session": session_brief(highest_power),
            "session_count": len(sessions),
            "charging_days": len({session["date"] for session in sessions if session["date"]}),
            "average_start_time": average_clock_time(starts),
            "average_finish_time": average_clock_time(finishes),
            "average_duration_minutes": (
                sum(session["duration_minutes"] for session in duration_sessions) / len(duration_sessions)
                if duration_sessions else None
            ),
            "duration_sample_count": len(duration_sessions),
            "highest_power_available": bool(highest_power),
        },
        "recommendations": [],
        "sessions": history,
        "ignored_incomplete_sessions": ignored_incomplete_sessions,
        "ignored_incomplete_message": (
            f"{ignored_incomplete_sessions} zero-kWh or incomplete charger session"
            f"{'' if ignored_incomplete_sessions == 1 else 's'} ignored. Ocular may only export completed sessions."
            if ignored_incomplete_sessions else ""
        ),
    }


def load_summary(conn: sqlite3.Connection, start: date, end: date, settings: Settings) -> Summary:
    key = request_cache_key("summary", settings, start.isoformat(), end.isoformat())
    return perf_memoize("summary", key, lambda: _timed_load_summary(conn, start, end, settings))


def _timed_load_summary(conn: sqlite3.Connection, start: date, end: date, settings: Settings) -> Summary:
    with perf_stage("mixed-tariff cost calculation"):
        return _load_summary_uncached(conn, start, end, settings)


def _load_summary_uncached(conn: sqlite3.Connection, start: date, end: date, settings: Settings) -> Summary:
    measurements = load_measurements(conn, start, end, settings)
    resolver = tariff_resolver(settings)
    totals = measurement_totals(measurements)
    interval_timestamps = {
        measurement.timestamp
        for measurement in measurements
        if measurement.measurement_type == MeasurementType.GRID_IMPORT
    }
    buckets = {
        bucket: {"kwh": 0.0, "cost": 0.0}
        for bucket in BUCKET_ORDER
    }
    estimated_ev_charging = 0.0
    finances = financial_engine()
    export_credit = 0.0
    tariff_periods_used: dict[str, dict[str, Any]] = {}
    unresolved_intervals = 0
    fallback_intervals = 0
    for measurement in measurements:
        measurement_day = date.fromisoformat(measurement.timestamp[:10])
        with perf_stage("tariff resolution"):
            resolution, tariff = tariff_for_resolved_day(resolver, measurement_day)
        if measurement.measurement_type == MeasurementType.GRID_EXPORT:
            export_credit += finances.calculate_export_credit(float(measurement.value or 0.0), tariff.primary_export_rate)
            continue
        if measurement.measurement_type != MeasurementType.GRID_IMPORT:
            continue
        resolution_key = (
            resolution.event_id or tariff.version_id
            if resolution is not None
            else tariff.version_id
        )
        period_entry = tariff_periods_used.setdefault(
            resolution_key,
            {
                "tariff": tariff.public_metadata(),
                "source": resolution.source if resolution is not None else "tariff_version",
                "event_id": resolution.event_id if resolution is not None else None,
                "interval_count": 0,
                "import_cost": 0.0,
                "export_credit": 0.0,
                "supply_cost": 0.0,
            },
        )
        period_entry["interval_count"] += 1
        if resolution is not None and resolution.warning:
            unresolved_intervals += 1
        if resolution is not None and resolution.source != "household_timeline":
            fallback_intervals += 1
        bucket = str(measurement.attributes.get("tariff_bucket") or "")
        kwh = float(measurement.value or 0.0)
        buckets.setdefault(bucket, {"kwh": 0.0, "cost": 0.0})
        buckets[bucket]["kwh"] += kwh
        interval_cost = kwh * tariff.usage_rate(bucket)
        buckets[bucket]["cost"] += interval_cost
        period_entry["import_cost"] += interval_cost
        if bucket == "overnight":
            power_w = float(measurement.attributes.get("power_w") or 0.0)
            interval_minutes = float(measurement.attributes.get("interval_minutes") or 15)
            estimated_ev_charging += max(power_w - settings.ev_baseline_watts, 0) * interval_minutes / 60 / 1000

    intervals = len(interval_timestamps)
    reading_days = len({timestamp[:10] for timestamp in interval_timestamps})
    grid_import = totals[MeasurementType.GRID_IMPORT]
    import_cost = sum(bucket["cost"] for bucket in buckets.values())
    generated = totals[MeasurementType.SOLAR_GENERATION]
    used = sum(
        float(measurement.attributes.get("solar_used_kwh") or 0.0)
        for measurement in measurements
        if measurement.measurement_type == MeasurementType.SOLAR_GENERATION
    )
    exported = totals[MeasurementType.GRID_EXPORT]
    actual_ev_charging = None
    actual_ev_cost = None
    actual_ev_breakdown = empty_ev_breakdown()
    actual_ev_partial = False
    actual_source = "estimated"
    raw_actual_sessions = load_actual_ev_sessions(conn, start, end, settings)
    actual_sessions = load_completed_ev_sessions(conn, start, end, settings)
    ignored_incomplete_sessions = len(raw_actual_sessions) - len(actual_sessions)
    analytics = ev_session_analytics(actual_sessions, settings, ignored_incomplete_sessions)
    if actual_sessions:
        actual_source = actual_sessions[0]["source"]
        actual_ev_charging = sum(session["energy_kwh"] for session in actual_sessions)
        actual_ev_cost = sum(session["cost"] for session in actual_sessions)
        actual_ev_partial = any(session["cost_partial"] for session in actual_sessions)
        for session in actual_sessions:
            for bucket in BUCKET_ORDER:
                actual_ev_breakdown[bucket]["kwh"] += session["tariff_breakdown"][bucket]["kwh"]
                actual_ev_breakdown[bucket]["cost"] += session["tariff_breakdown"][bucket]["cost"]
    if actual_ev_charging is not None and actual_ev_charging <= 0:
        actual_ev_charging = None
        actual_ev_cost = None
    supply_days = (end - start).days + 1
    supply_cost = 0.0
    for offset in range(supply_days):
        day = start + timedelta(days=offset)
        with perf_stage("tariff resolution"):
            resolution, tariff = tariff_for_resolved_day(resolver, day)
        supply_cost += tariff.supply_charge_per_day
        key = resolution.event_id or tariff.version_id if resolution is not None else tariff.version_id
        entry = tariff_periods_used.setdefault(
            key,
            {
                "tariff": tariff.public_metadata(),
                "source": resolution.source if resolution is not None else "tariff_version",
                "event_id": resolution.event_id if resolution is not None else None,
                "interval_count": 0,
                "import_cost": 0.0,
                "export_credit": 0.0,
                "supply_cost": 0.0,
            },
        )
        entry["supply_cost"] += tariff.supply_charge_per_day
    for measurement in measurements:
        if measurement.measurement_type != MeasurementType.GRID_EXPORT:
            continue
        measurement_day = date.fromisoformat(measurement.timestamp[:10])
        with perf_stage("tariff resolution"):
            resolution, tariff = tariff_for_resolved_day(resolver, measurement_day)
        key = resolution.event_id or tariff.version_id if resolution is not None else tariff.version_id
        if key in tariff_periods_used:
            tariff_periods_used[key]["export_credit"] += finances.calculate_export_credit(float(measurement.value or 0.0), tariff.primary_export_rate)
    total_cost = float(import_cost) + supply_cost
    net_cost = finances.calculate_net_bill(float(import_cost), supply_cost, export_credit)
    self_consumption = used / generated * 100 if generated else 0.0
    export_pct = exported / generated * 100 if generated else 0.0
    peak_cost = buckets["peak"]["cost"]
    peak_cost_share = peak_cost / import_cost * 100 if import_cost else 0.0
    observed_days = max(int(reading_days), 1)
    return Summary(
        start=start,
        end=end,
        supply_days=supply_days,
        intervals=int(intervals),
        reading_days=int(reading_days),
        grid_import_kwh=float(grid_import),
        import_cost=float(import_cost),
        supply_cost=supply_cost,
        total_cost=total_cost,
        export_credit=export_credit,
        net_cost=net_cost,
        solar_generated_kwh=float(generated),
        solar_used_kwh=float(used),
        solar_exported_kwh=float(exported),
        self_consumption_pct=self_consumption,
        solar_export_pct=export_pct,
        peak_cost_share_pct=peak_cost_share,
        ev_charging_kwh=(
            actual_ev_charging
            if actual_ev_charging is not None
            else float(estimated_ev_charging)
        ),
        ev_charging_cost=(
            actual_ev_cost
            if actual_ev_cost is not None
            else finances.cost_of_ev_session(
                float(estimated_ev_charging),
                tariff_for_resolved_day(resolver, end)[1].usage_rate("overnight"),
            )
        ),
        ev_charging_source=(
            actual_source if actual_ev_charging is not None else "estimated"
        ),
        actual_ev_charging_kwh=float(actual_ev_charging or 0),
        actual_ev_charging_cost=float(actual_ev_cost or 0),
        actual_ev_tariff_breakdown=actual_ev_breakdown,
        actual_ev_cost_partial=actual_ev_partial,
        ev_session_analytics=analytics,
        average_daily_grid_import_kwh=float(grid_import) / observed_days,
        average_daily_solar_generation_kwh=float(generated) / observed_days,
        average_daily_net_cost=net_cost / supply_days,
        buckets=buckets,
        tariff_resolution={
            "periods_used": list(tariff_periods_used.values()),
            "interval_counts": {key: value["interval_count"] for key, value in tariff_periods_used.items()},
            "unresolved_intervals": unresolved_intervals,
            "fallback_intervals": fallback_intervals,
            "confidence": "high" if unresolved_intervals == 0 else "medium",
            "warnings": household_timeline(settings).warnings(),
        },
    )


def battery_dispatch_intervals(engine: DataEngine, start: date, end: date, settings: Settings) -> list[dict[str, Any]]:
    key = request_cache_key("battery_current_intervals", settings, start.isoformat(), end.isoformat())
    return perf_memoize("battery_current_intervals", key, lambda: _battery_dispatch_intervals_uncached(engine, start, end, settings))


def _battery_dispatch_intervals_uncached(engine: DataEngine, start: date, end: date, settings: Settings) -> list[dict[str, Any]]:
    resolver = tariff_resolver(settings)
    intervals = []
    with perf_stage("battery current-period interval load"):
        rows = engine.get_battery_interval_history(start, end)
    perf_increment("battery_current_interval_rows", len(rows))
    for row in rows:
        timestamp = str(row.get("measurement_time") or "")
        if not timestamp:
            continue
        bucket = str(row.get("tariff_bucket") or "")
        with perf_stage("tariff resolution"):
            resolution, tariff = tariff_for_resolved_day(resolver, date.fromisoformat(timestamp[:10]))
        intervals.append(
            {
            "measurement_time": timestamp,
            "tariff_bucket": bucket,
            "import_rate": tariff.usage_rate(bucket),
            "grid_import_kwh": float(row.get("grid_import_kwh") or 0.0),
            "solar_export_kwh": float(row.get("solar_export_kwh") or 0.0),
            "tariff_version_id": tariff.version_id,
            "tariff_effective_from": tariff.effective_from.isoformat(),
            "tariff_name": tariff.tariff_name,
            "tariff_source": resolution.source if resolution is not None else "tariff_version",
            "tariff_warning": resolution.warning if resolution is not None else None,
        }
        )
    return intervals


def battery_historical_dispatch_intervals(engine: DataEngine, start: date, end: date, settings: Settings) -> list[dict[str, Any]]:
    key = request_cache_key("battery_seasonal_intervals", settings, start.isoformat(), end.isoformat())
    return perf_memoize("battery_seasonal_intervals", key, lambda: _battery_historical_dispatch_intervals_uncached(engine, start, end, settings))


def _battery_historical_dispatch_intervals_uncached(engine: DataEngine, start: date, end: date, settings: Settings) -> list[dict[str, Any]]:
    resolver = tariff_resolver(settings)
    intervals = []
    with perf_stage("seasonal history loading"):
        rows = engine.get_battery_interval_history(start, end)
    perf_increment("battery_seasonal_interval_rows", len(rows))
    for row in rows:
        timestamp = str(row.get("measurement_time") or "")
        if not timestamp:
            continue
        bucket = str(row.get("tariff_bucket") or "")
        with perf_stage("tariff resolution"):
            resolution, tariff = tariff_for_resolved_day(resolver, date.fromisoformat(timestamp[:10]))
        intervals.append(
            {
                **row,
                "measurement_time": timestamp,
                "tariff_bucket": bucket,
                "import_rate": tariff.usage_rate(bucket),
                "grid_import_kwh": float(row.get("grid_import_kwh") or 0.0),
                "solar_export_kwh": float(row.get("solar_export_kwh") or 0.0),
                "tariff_version_id": tariff.version_id,
                "tariff_effective_from": tariff.effective_from.isoformat(),
                "tariff_name": tariff.tariff_name,
                "tariff_source": resolution.source if resolution is not None else "tariff_version",
                "tariff_warning": resolution.warning if resolution is not None else None,
            }
        )
    return intervals


def tariff_scenario(summary: Summary, settings: Settings) -> dict:
    resolver = tariff_resolver(settings)
    comparison_tariff = resolver.resolve(
        summary.end,
        provider=str(getattr(settings, "retailer", "") or "Synergy"),
        tariff_name=plan_label(settings.comparison_plan),
    )
    rates = settings.comparison_plan_rates
    finances = financial_engine()
    comparison_import = finances.calculate_import_cost(summary.grid_import_kwh, comparison_tariff.usage_rate("peak") or rates["flat_import_rate"])
    comparison_supply = summary.supply_days * comparison_tariff.supply_charge_per_day
    comparison_credit = finances.calculate_export_credit(summary.solar_exported_kwh, comparison_tariff.primary_export_rate)
    comparison_net = finances.calculate_net_bill(comparison_import, comparison_supply, comparison_credit)
    return {
        "current_plan": settings.current_plan,
        "comparison_plan": settings.comparison_plan,
        "current_plan_net_cost": summary.net_cost,
        "comparison_plan_net_cost": comparison_net,
        "difference": summary.net_cost - comparison_net,
        "estimated_savings": comparison_net - summary.net_cost,
        "comparison_tariff_version": comparison_tariff.public_metadata(),
    }


def ev_cost_under_rate(summary: Summary, rate: float) -> float:
    return financial_engine().cost_of_ev_session(summary.ev_charging_kwh, rate)


def weighted_average_import_rate(summary: Summary) -> float:
    return financial_engine().weighted_average_import_rate(summary)


def plan_comparison(summary: Summary, settings: Settings) -> dict:
    resolver = tariff_resolver(settings)
    current_tariff = resolver.resolve(summary.end)
    comparison_tariff = resolver.resolve(
        summary.end,
        provider=str(getattr(settings, "retailer", "") or "Synergy"),
        tariff_name=plan_label(settings.comparison_plan),
    )
    return financial_engine().compare_tariff_plans(
        summary,
        settings.current_plan,
        settings.comparison_plan,
        {
            "flat_import_rate": settings.comparison_plan_rates["flat_import_rate"],
            "supply_charge_per_day": settings.comparison_plan_rates.get(
                "supply_charge_per_day",
                settings.supply_charge_per_day,
            ),
            "export_credit_rate": settings.comparison_plan_rates.get(
                "export_credit_rate",
                settings.export_credit_rate,
            ),
        },
        current_display_name=plan_label(settings.current_plan),
        comparison_display_name=plan_label(settings.comparison_plan),
        current_tariff_version=current_tariff,
        comparison_tariff_version=comparison_tariff,
    )


def scenario_confidence(summary: Summary, needs_actual_ev: bool = False) -> str:
    coverage = summary.reading_days / summary.supply_days if summary.supply_days else 0.0
    if needs_actual_ev and summary.ev_charging_source != "ocular_export":
        return "medium" if coverage >= 0.5 else "low"
    if coverage >= 0.8:
        return "high"
    if coverage >= 0.35:
        return "medium"
    return "low"


def scenario_card(
    scenario_id: str,
    title: str,
    estimated_bill: float,
    actual_bill: float,
    confidence: str,
    description: str,
) -> dict:
    difference = estimated_bill - actual_bill
    return {
        "id": scenario_id,
        "title": title,
        "estimated_bill": estimated_bill,
        "difference_vs_actual": difference,
        "savings": max(-difference, 0.0),
        "loss": max(difference, 0.0),
        "confidence": confidence,
        "description": description,
    }


def scenario_analysis(
    summary: Summary,
    settings: Settings,
    battery_intervals: Optional[list[dict[str, Any]]] = None,
    seasonal_battery_intervals: Optional[list[dict[str, Any]]] = None,
) -> dict:
    key = request_cache_key(
        "scenario_analysis",
        settings,
        summary.start.isoformat(),
        summary.end.isoformat(),
        len(battery_intervals or []),
        len(seasonal_battery_intervals or []),
        summary.net_cost,
        summary.grid_import_kwh,
        summary.solar_exported_kwh,
    )
    return perf_memoize(
        "scenario_analysis",
        key,
        lambda: _scenario_analysis_uncached(summary, settings, battery_intervals, seasonal_battery_intervals),
    )


def _scenario_analysis_uncached(
    summary: Summary,
    settings: Settings,
    battery_intervals: Optional[list[dict[str, Any]]] = None,
    seasonal_battery_intervals: Optional[list[dict[str, Any]]] = None,
) -> dict:
    actual_bill = summary.net_cost
    ev_kwh = summary.ev_charging_kwh
    ev_actual_cost = summary.ev_charging_cost
    current_tariff = tariff_for_date(settings, summary.end)
    current_rates = current_tariff.usage_rate_map
    cheapest_bucket = min(BUCKET_ORDER, key=lambda bucket: current_rates.get(bucket, 0))
    peak_bucket = max(BUCKET_ORDER, key=lambda bucket: current_rates.get(bucket, 0))

    scenarios = []
    for scenario_id, title, bucket in (
        ("ev_all_super_off_peak", "What if every EV session occurred in the 9am-3pm EV window?", "super_off_peak"),
        ("ev_all_overnight", "What if every EV session occurred overnight?", "overnight"),
        ("ev_all_peak", "What if every EV session occurred during Peak?", peak_bucket),
    ):
        scenario_ev_cost = ev_kwh * current_rates[bucket]
        scenarios.append(
            scenario_card(
                scenario_id,
                title,
                actual_bill - ev_actual_cost + scenario_ev_cost,
                actual_bill,
                scenario_confidence(summary, True),
                f"Reprices {kwh(ev_kwh)} of EV charging at {BUCKET_LABELS.get(bucket, bucket)} rates.",
            )
        )

    scenarios.append(
        scenario_card(
            "no_ev_charging",
            "What if no EV charging occurred this billing period?",
            actual_bill - ev_actual_cost,
            actual_bill,
            scenario_confidence(summary, True),
            f"Removes {kwh(ev_kwh)} of EV charging cost from this period.",
        )
    )

    average_import_rate = weighted_average_import_rate(summary)
    self_consumed_kwh = min(summary.solar_exported_kwh, summary.grid_import_kwh)
    avoided_import_cost = self_consumed_kwh * average_import_rate
    lost_export_credit = summary.solar_exported_kwh * current_tariff.primary_export_rate
    scenarios.append(
        scenario_card(
            "solar_export_self_consumed",
            "What if all exported solar had instead been self-consumed?",
            actual_bill - avoided_import_cost + lost_export_credit,
            actual_bill,
            scenario_confidence(summary),
            f"Models {kwh(self_consumed_kwh)} of exported solar offsetting grid imports, with export credit removed.",
        )
    )

    battery_stage = (
        "annual/seasonal battery simulation"
        if seasonal_battery_intervals
        else "battery current-period simulation"
    )
    with perf_stage(battery_stage):
        perf_increment("battery_simulations_executed")
        battery_comparison = financial_engine().compare_home_battery_sizes(
            battery_intervals or [],
            settings,
            baseline_bill=actual_bill,
            baseline_grid_import_kwh=summary.grid_import_kwh,
            baseline_solar_exported_kwh=summary.solar_exported_kwh,
            tariff_version=current_tariff,
            seasonal_intervals=seasonal_battery_intervals or [],
        )
    battery_recommendation = battery_comparison.get("battery_recommendation") or {}
    with perf_stage("battery investment calculation"):
        battery_investment = battery_investment_analysis(battery_comparison, settings)

    ev_window_cost = ev_kwh * current_rates[cheapest_bucket]
    ev_window_savings = max(ev_actual_cost - ev_window_cost, 0.0)
    tariff_usage = summary.ev_session_analytics.get("tariff_usage", {}) if summary.ev_session_analytics else {}
    cheapest_pct = float(tariff_usage.get(cheapest_bucket, {}).get("pct") or 0.0)
    peak_sessions = [
        session for session in summary.ev_session_analytics.get("sessions", [])
        if session.get("tariff_breakdown", {}).get(peak_bucket, {}).get("cost", 0) > 0
    ] if summary.ev_session_analytics else []
    insights = []
    if ev_kwh > 0:
        insights.append(
            f"If every EV session had been shifted into the EV solar window you would have saved approximately {money(ev_window_savings)}."
        )
    if cheapest_pct:
        insights.append(
            f"You've already charged {cheapest_pct:.0f}% of energy in the cheapest tariff."
        )
    if peak_sessions:
        count_word = "one charging session" if len(peak_sessions) == 1 else f"{len(peak_sessions)} charging sessions"
        insights.append(f"Only {count_word} incurred Peak charges.")

    return {
        "actual_bill": actual_bill,
        "scenarios": scenarios,
        "ev_insights": insights,
        "battery_comparison": battery_comparison,
        "battery_recommendation": battery_recommendation,
        "battery_investment": battery_investment,
    }


def battery_investment_analysis(comparison: Optional[dict], settings: Settings) -> dict[str, Any]:
    assumptions = battery_investment_assumptions(settings)
    if not comparison or not comparison.get("available"):
        return {
            "available": False,
            "recommended_size": None,
            "options": [],
            "confidence": "Low",
            "confidence_reason": "Battery investment analysis needs the existing battery sizing simulation first.",
            "assumptions": assumptions,
        }

    battery_recommendation = comparison.get("battery_recommendation") or {}
    recommended_size = float(battery_recommendation.get("recommended_size_kwh") or (comparison.get("recommended") or {}).get("capacity_kwh") or 0.0)
    source_options = battery_investment_source_options(comparison)
    options = [
        battery_investment_option(option, assumptions, comparison)
        for option in source_options
        if float(option.get("capacity_kwh") or 0.0) > 0
    ]
    selected = next(
        (
            option for option in options
            if abs(float(option["capacity_kwh"]) - recommended_size) < 1e-9
        ),
        options[0] if options else None,
    )
    if not selected:
        return {
            "available": False,
            "recommended_size": None,
            "options": [],
            "confidence": "Low",
            "confidence_reason": "No simulated battery size was available for investment analysis.",
            "assumptions": assumptions,
        }
    selected = {**selected, "recommended": True}
    options = [
        {**option, "recommended": abs(float(option["capacity_kwh"]) - recommended_size) < 1e-9}
        for option in options
    ]
    summary = battery_investment_summary(selected, assumptions)
    return {
        "available": True,
        "recommended_size": selected["capacity_kwh"],
        "recommendation_source": str(battery_recommendation.get("source") or "current_period"),
        "interval_days": int(battery_recommendation.get("interval_days") or 0),
        "seasonal_confidence": str(((comparison.get("seasonal_model") or {}).get("confidence_model") or {}).get("level") or ""),
        "installed_cost": selected["installed_cost"],
        "rebate": selected["rebate"],
        "net_cost": selected["net_cost"],
        "annual_saving": selected["annual_saving"],
        "simple_payback_years": selected["simple_payback_years"],
        "ten_year_estimated_savings": selected["ten_year_estimated_savings"],
        "fifteen_year_estimated_savings": selected["fifteen_year_estimated_savings"],
        "ten_year_net": selected["ten_year_net"],
        "fifteen_year_net": selected["fifteen_year_net"],
        "confidence": selected["confidence"],
        "confidence_reason": selected["confidence_reason"],
        "overall_recommendation": selected["overall_recommendation"],
        "summary": summary,
        "options": options,
        "assumptions": assumptions,
    }


def battery_investment_assumptions(settings: Settings) -> dict[str, Any]:
    efficiency = float(getattr(settings, "battery_round_trip_efficiency", 0.90) or 0.90)
    if efficiency > 1.0:
        efficiency = efficiency / 100.0
    return {
        "installed_costs": {
            str(key): float(value)
            for key, value in getattr(settings, "battery_installed_costs", DEFAULT_SETTINGS["battery_installed_costs"]).items()
        },
        "round_trip_efficiency_pct": efficiency * 100,
        "battery_life_years": int(getattr(settings, "battery_life_years", 15) or 15),
        "annual_degradation_pct": float(getattr(settings, "battery_annual_degradation_pct", 2.0) or 0.0),
        "electricity_inflation_pct": float(getattr(settings, "battery_electricity_inflation_pct", 3.0) or 0.0),
        "discount_rate_pct": float(getattr(settings, "battery_discount_rate_pct", 0.0) or 0.0),
        "government_rebate": float(getattr(settings, "battery_government_rebate", 0.0) or 0.0),
    }


def battery_investment_source_options(comparison: dict) -> list[dict[str, Any]]:
    recommendation = comparison.get("battery_recommendation") or {}
    seasonal = comparison.get("seasonal_model") or {}
    annual_options = seasonal.get("annual_options") or []
    if recommendation.get("source") == "annual_historical" and annual_options:
        return annual_options
    grid_options = ((comparison.get("strategies") or {}).get("solar_plus_grid_arbitrage") or {}).get("options") or []
    return grid_options or comparison.get("options", []) or []


def battery_investment_option(option: dict[str, Any], assumptions: dict[str, Any], comparison: dict[str, Any]) -> dict[str, Any]:
    capacity = float(option.get("capacity_kwh") or 0.0)
    installed_cost = battery_installed_cost(capacity, assumptions)
    rebate = min(float(assumptions["government_rebate"] or 0.0), installed_cost)
    net_cost = max(installed_cost - rebate, 0.0)
    annual_saving = max(float(option.get("annualised_savings") or 0.0), 0.0)
    simple_payback = net_cost / annual_saving if annual_saving > 0 else None
    ten_year_savings = battery_projected_savings(
        annual_saving,
        10,
        assumptions["annual_degradation_pct"],
        assumptions["electricity_inflation_pct"],
        assumptions["discount_rate_pct"],
    )
    fifteen_year_savings = battery_projected_savings(
        annual_saving,
        15,
        assumptions["annual_degradation_pct"],
        assumptions["electricity_inflation_pct"],
        assumptions["discount_rate_pct"],
    )
    confidence, confidence_reason = battery_investment_confidence(comparison)
    recommendation = battery_investment_recommendation(simple_payback, net_cost, ten_year_savings, fifteen_year_savings, assumptions)
    return {
        "capacity_kwh": capacity,
        "label": option.get("label") or f"{capacity:g} kWh usable",
        "installed_cost": installed_cost,
        "rebate": rebate,
        "net_cost": net_cost,
        "annual_saving": annual_saving,
        "simple_payback_years": simple_payback,
        "ten_year_estimated_savings": ten_year_savings,
        "fifteen_year_estimated_savings": fifteen_year_savings,
        "ten_year_net": ten_year_savings - net_cost,
        "fifteen_year_net": fifteen_year_savings - net_cost,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "overall_recommendation": recommendation,
    }


def battery_installed_cost(capacity: float, assumptions: dict[str, Any]) -> float:
    costs = assumptions.get("installed_costs") or {}
    exact = costs.get(f"{capacity:g}")
    if exact is None and capacity.is_integer():
        exact = costs.get(str(int(capacity)))
    if exact is None:
        exact = costs.get(str(capacity))
    if exact is not None:
        return float(exact)
    numeric_costs = sorted((float(size), float(cost)) for size, cost in costs.items())
    if not numeric_costs:
        return 0.0
    return min(numeric_costs, key=lambda item: abs(item[0] - capacity))[1]


def battery_projected_savings(
    annual_saving: float,
    years: int,
    degradation_pct: float,
    inflation_pct: float,
    discount_rate_pct: float,
) -> float:
    total = 0.0
    degradation = 1 - max(float(degradation_pct), 0.0) / 100
    inflation = 1 + float(inflation_pct) / 100
    discount = 1 + max(float(discount_rate_pct), 0.0) / 100
    for year in range(years):
        value = float(annual_saving) * (degradation ** year) * (inflation ** year)
        total += value / (discount ** year)
    return total


def battery_investment_confidence(comparison: dict[str, Any]) -> tuple[str, str]:
    seasonal_confidence = ((comparison.get("seasonal_model") or {}).get("confidence_model") or {})
    if (comparison.get("seasonal_model") or {}).get("available") and seasonal_confidence:
        return str(seasonal_confidence.get("level") or "Low"), str(seasonal_confidence.get("reason") or "")
    data_quality = comparison.get("data_quality") or {}
    days = int(data_quality.get("days") or 0)
    if days >= 30:
        return "High", f"Based on {days} days of interval history."
    if days >= 7:
        return "Medium", f"Based on {days} days of interval history."
    if days > 0:
        return "Low", f"Based on only {days} day{'s' if days != 1 else ''} of interval history."
    return "Low", "No interval history was available for the investment projection."


def battery_investment_recommendation(
    simple_payback: Optional[float],
    net_cost: float,
    ten_year_savings: float,
    fifteen_year_savings: float,
    assumptions: dict[str, Any],
) -> str:
    if simple_payback is None or net_cost <= 0:
        return "Not recommended"
    battery_life = float(assumptions["battery_life_years"])
    ten_year_net = ten_year_savings - net_cost
    fifteen_year_net = fifteen_year_savings - net_cost
    if simple_payback > battery_life:
        return "Not recommended"
    if simple_payback <= 7 and ten_year_net > 0 and fifteen_year_net > 0:
        return "★★★★★ Excellent investment"
    if simple_payback <= 10 and fifteen_year_net > 0:
        return "★★★★ Good investment"
    if fifteen_year_net >= 0:
        return "★★★ Borderline"
    return "★★ Poor investment"


def battery_investment_summary(selected: dict[str, Any], assumptions: dict[str, Any]) -> str:
    payback = selected.get("simple_payback_years")
    battery_life = int(assumptions["battery_life_years"])
    if payback is None:
        return (
            f"Based on your household, a {selected['capacity_kwh']:g} kWh battery does not show measurable annual savings yet. "
            "A purchase is not recommended from the available data."
        )
    if payback > battery_life:
        return (
            f"Based on your household, a {selected['capacity_kwh']:g} kWh battery is expected to save approximately "
            f"{money(selected['annual_saving'])} per year. Assuming an installed cost of {money(selected['installed_cost'])}, "
            f"the estimated simple payback is approximately {payback:.1f} years, which is longer than the assumed "
            f"{battery_life}-year battery life."
        )
    return (
        f"Based on your household, a {selected['capacity_kwh']:g} kWh battery is expected to save approximately "
        f"{money(selected['annual_saving'])} per year. Assuming an installed cost of {money(selected['installed_cost'])}, "
        f"your estimated simple payback is approximately {payback:.1f} years. After 10 years you are projected to be "
        f"{'ahead' if selected['ten_year_net'] >= 0 else 'behind'} by approximately {money(abs(selected['ten_year_net']))}."
    )


def car_comparison(settings: Settings) -> dict:
    available = bool(
        settings.car_comparison_enabled
        and settings.ev_odometer_km is not None
        and settings.ev_kwh_per_100km is not None
    )
    result = {
        "enabled": settings.car_comparison_enabled,
        "available": available,
        "message": "EV fuel savings require distance or charger session data.",
    }
    if available:
        distance = settings.ev_odometer_km
        petrol_cost = distance / 100 * settings.previous_car_l_per_100km * settings.petrol_price_per_litre
        result.update({"distance_km": distance, "estimated_previous_car_fuel_cost": petrol_cost})
    return result


def compare_summaries(period_a: Period, summary_a: Summary, period_b: Period, summary_b: Summary) -> dict:
    fields = {
        "net_cost": summary_a.net_cost - summary_b.net_cost,
        "grid_import_kwh": summary_a.grid_import_kwh - summary_b.grid_import_kwh,
        "solar_generated_kwh": summary_a.solar_generated_kwh - summary_b.solar_generated_kwh,
        "peak_cost": summary_a.buckets["peak"]["cost"] - summary_b.buckets["peak"]["cost"],
        "self_consumption_pct": summary_a.self_consumption_pct - summary_b.self_consumption_pct,
        "ev_charging_kwh": summary_a.ev_charging_kwh - summary_b.ev_charging_kwh,
    }
    return {
        "period_a": {"kind": period_a.kind, "label": period_a.label, "start": period_a.start.isoformat(), "end": period_a.end.isoformat()},
        "period_b": {"kind": period_b.kind, "label": period_b.label, "start": period_b.start.isoformat(), "end": period_b.end.isoformat()},
        "summary_a": summary_to_dict(summary_a),
        "summary_b": summary_to_dict(summary_b),
        "deltas": fields,
    }


def comparison_data(period_a: Period, period_b: Period, settings: Settings) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        validate_database(conn)
        return compare_summaries(
            period_a,
            load_summary(conn, period_a.start, period_a.end, settings),
            period_b,
            load_summary(conn, period_b.start, period_b.end, settings),
        )


def load_daily_trends(
    conn: sqlite3.Connection, start: date, end: date, settings: Settings
) -> list[DailyTrend]:
    key = request_cache_key("daily_trends", settings, start.isoformat(), end.isoformat())
    return perf_memoize("daily_trends", key, lambda: _timed_load_daily_trends(conn, start, end, settings))


def _timed_load_daily_trends(
    conn: sqlite3.Connection, start: date, end: date, settings: Settings
) -> list[DailyTrend]:
    with perf_stage("daily aggregation"):
        return _load_daily_trends_uncached(conn, start, end, settings)


def _load_daily_trends_uncached(
    conn: sqlite3.Connection, start: date, end: date, settings: Settings
) -> list[DailyTrend]:
    measurements = load_measurements(conn, start, end, settings)
    resolver = tariff_resolver(settings)
    rows: dict[str, dict[str, float]] = {}
    for measurement in measurements:
        day_text = measurement.timestamp[:10]
        row = rows.setdefault(
            day_text,
            {
                "grid_import": 0.0,
                "generated": 0.0,
                "exported": 0.0,
                "import_cost": 0.0,
                "ev_charging": 0.0,
            },
        )
        with perf_stage("tariff resolution"):
            _resolution, tariff = tariff_for_resolved_day(resolver, date.fromisoformat(day_text))
        if measurement.measurement_type == MeasurementType.GRID_IMPORT:
            kwh = float(measurement.value or 0.0)
            bucket = str(measurement.attributes.get("tariff_bucket") or "")
            row["grid_import"] += kwh
            row["import_cost"] += kwh * tariff.usage_rate(bucket)
            if bucket == "overnight":
                power_w = float(measurement.attributes.get("power_w") or 0.0)
                interval_minutes = float(measurement.attributes.get("interval_minutes") or 15)
                row["ev_charging"] += max(power_w - settings.ev_baseline_watts, 0) * interval_minutes / 60 / 1000
        elif measurement.measurement_type == MeasurementType.SOLAR_GENERATION:
            row["generated"] += float(measurement.value or 0.0)
        elif measurement.measurement_type == MeasurementType.GRID_EXPORT:
            row["exported"] += float(measurement.value or 0.0)

    trends = []
    estimated_ev_by_day = {day_text: float(row["ev_charging"]) for day_text, row in rows.items()}
    daily_ev_history = load_daily_ev_history(conn, start, end, settings, estimated_ev_by_day)
    configured_actual_sources = actual_ev_sources(settings)
    for day_text in sorted(rows):
        row = rows[day_text]
        ev_history = daily_ev_history.get(day_text)
        ev_value = float((ev_history or {}).get("energy_kwh") or 0.0)
        ev_source = str(
            (ev_history or {}).get("source")
            or (configured_actual_sources[0] if configured_actual_sources else "estimated")
        )
        with perf_stage("tariff resolution"):
            _resolution, tariff = tariff_for_resolved_day(resolver, date.fromisoformat(day_text))
        export_credit = float(row["exported"]) * tariff.primary_export_rate
        trends.append(
            DailyTrend(
                day=date.fromisoformat(day_text),
                grid_import_kwh=float(row["grid_import"]),
                solar_generated_kwh=float(row["generated"]),
                solar_exported_kwh=float(row["exported"]),
                import_cost=float(row["import_cost"]),
                export_credit=export_credit,
                net_cost=float(row["import_cost"]) + tariff.supply_charge_per_day - export_credit,
                ev_charging_kwh=ev_value,
                ev_charging_source=ev_source,
            )
        )
    return trends


def build_historical_trends(conn: sqlite3.Connection, today: date, settings: Settings) -> HistoricalTrends:
    history_start = today - timedelta(days=30)
    history_end = today - timedelta(days=1)
    history = load_daily_trends(conn, history_start, history_end, settings)
    return HistoricalTrendsEngine().build(history, as_of=today)


def load_recommendations(
    conn: sqlite3.Connection,
    current_summary: Summary,
    settings: Settings,
    household_context: Optional[HouseholdContext] = None,
) -> list[Decision]:
    engine = data_engine(conn)
    household_context = household_context or engine.get_household_context()
    previous_summary = None
    if settings.previous_billing_period_start and settings.previous_billing_period_end:
        candidate = load_summary(
            conn,
            settings.previous_billing_period_start,
            settings.previous_billing_period_end,
            settings,
        )
        if candidate.supply_days and candidate.reading_days / candidate.supply_days >= 0.5:
            previous_summary = summary_to_dict(candidate)

    return generate_decisions_from_data_engine(
        summary_to_dict(current_summary),
        settings,
        engine,
        settings.recommendation_thresholds,
        previous_summary=previous_summary,
        include_inactive=True,
        household_context=household_context,
    )


def build_charging_advice(
    today_summary: Summary,
    billing_summary: Summary,
    settings: Settings,
    health: dict,
    recommendations: list[Decision],
    projected_bill: Optional[float],
    now: datetime,
    household_context: Optional[HouseholdContext] = None,
    context=None,
) -> ChargingAdvice:
    return charging_advisor(household_context).advise(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=settings,
        health=health,
        recommendations=recommendations,
        projected_bill=projected_bill,
        now=now,
        context=context,
    )


def build_cost_advisor(
    current_summary: Summary,
    previous_summary: Optional[Summary],
    settings: Settings,
    health: dict,
    recommendations: list[Decision],
    timeline: Optional[DecisionTimeline],
    comparison: Optional[dict],
    now: datetime,
    household_context: Optional[HouseholdContext] = None,
    context=None,
) -> Advisor:
    return cost_advisor(household_context).advise(
        current_summary=current_summary,
        previous_summary=previous_summary,
        settings=settings,
        recommendations=recommendations,
        timeline=timeline,
        health=health,
        comparison=comparison,
        now=now,
        context=context,
    )


def build_solar_advisor(
    summary: Summary,
    settings: Settings,
    health: dict,
    recommendations: list[Decision],
    timeline: Optional[DecisionTimeline],
    now: datetime,
    household_context: Optional[HouseholdContext] = None,
    context=None,
) -> Advisor:
    return solar_advisor(household_context).advise(
        summary=summary,
        settings=settings,
        recommendations=recommendations,
        timeline=timeline,
        health=health,
        now=now,
        context=context,
    )


def build_efficiency_advisor(
    summary: Summary,
    previous_summary: Optional[Summary],
    settings: Settings,
    health: dict,
    recommendations: list[Decision],
    timeline: Optional[DecisionTimeline],
    comparison: Optional[dict],
    trends: list[DailyTrend],
    overnight_baseload_watts: float,
    now: datetime,
    household_context: Optional[HouseholdContext] = None,
    context=None,
) -> Advisor:
    return efficiency_advisor(household_context).advise(
        summary=summary,
        previous_summary=previous_summary,
        settings=settings,
        recommendations=recommendations,
        timeline=timeline,
        health=health,
        overnight_baseload_watts=overnight_baseload_watts,
        comparison=comparison,
        trends=trends,
        now=now,
        context=context,
    )


def recommendation_lifecycle_state(decision: Decision) -> str:
    if is_recommendation_presentation(decision):
        return str(recommendation_presentation_value(decision, "status") or "active")
    return str(decision.evidence.get("lifecycle_state") or "active")


def active_recommendations(recommendations: list[Decision]) -> list[Decision]:
    return [
        decision for decision in recommendations
        if recommendation_lifecycle_state(decision) == "active"
    ]


def hidden_recommendations(recommendations: list[Decision]) -> list[Decision]:
    return [
        decision for decision in recommendations
        if recommendation_lifecycle_state(decision) in {"dismissed", "snoozed_until"}
    ]


def money(value: float) -> str:
    return f"${value:,.2f}"


def kwh(value: float) -> str:
    return f"{value:,.2f} kWh"


def signed_money(value: float) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{money(abs(value))}"


def signed_kwh(value: float) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{kwh(abs(value))}"


EV_ESTIMATE_CAVEAT = (
    "This estimate only covers overnight charging above the configured baseload. "
    "Daytime EV charging is not included unless charger session data is connected."
)


def ev_energy_label(settings: Settings, summary: Optional[Summary] = None) -> str:
    source = summary.ev_charging_source if summary else settings.ev_data_source
    return (
        "Actual EV charging"
        if source in {"home_assistant_ocpp", "ocular_export"}
        else "Estimated overnight EV charging"
    )


def ev_cost_label(settings: Settings, summary: Optional[Summary] = None) -> str:
    source = summary.ev_charging_source if summary else settings.ev_data_source
    return (
        "Calculated charging cost"
        if source == "home_assistant_ocpp"
        else "Calculated session cost"
        if source == "ocular_export"
        else "Estimated overnight EV charging cost"
    )


def ev_note(settings: Settings, summary: Optional[Summary] = None) -> str:
    source = summary.ev_charging_source if summary else settings.ev_data_source
    if source == "home_assistant_ocpp":
        return "Home Assistant OCPP charger sessions"
    if source == "ocular_export":
        return "Ocular charging history export"
    return f"Above {settings.ev_baseline_watts:.0f} W overnight baseline"


def print_summary(title: str, summary: Summary, settings: Settings) -> None:
    print(title)
    print(f"  Period:                       {summary.start} to {summary.end}")
    print(f"  Grid import:                  {kwh(summary.grid_import_kwh)}")
    for bucket in BUCKET_ORDER:
        values = summary.buckets[bucket]
        print(
            f"    {BUCKET_LABELS[bucket]:24} "
            f"{kwh(values['kwh']):>12}  {money(values['cost']):>9}"
        )
    print(f"  Import cost:                  {money(summary.import_cost)}")
    print(
        f"  Daily supply charge:          {money(settings.supply_charge_per_day)}/day "
        f"x {summary.supply_days} = {money(summary.supply_cost)}"
    )
    print(f"  Total estimated cost:         {money(summary.total_cost)}")
    print(f"  Export credit:                -{money(summary.export_credit)}")
    print(f"  Net estimated cost:           {money(summary.net_cost)}")
    print(f"  Solar generated:              {kwh(summary.solar_generated_kwh)}")
    print(f"  Solar used directly:          {kwh(summary.solar_used_kwh)}")
    print(f"  Solar exported:               {kwh(summary.solar_exported_kwh)}")
    print(f"  Solar self-consumption:       {summary.self_consumption_pct:.1f}%")
    print(f"  Solar export percentage:      {summary.solar_export_pct:.1f}%")
    print(f"  Peak cost share:              {summary.peak_cost_share_pct:.1f}%")
    print(f"  {ev_energy_label(settings, summary) + ':':31}{kwh(summary.ev_charging_kwh):>13}")
    print(f"  {ev_cost_label(settings, summary) + ':':31}{money(summary.ev_charging_cost)}")
    if summary.ev_charging_source == "estimated":
        print(f"  EV estimate caveat:            {EV_ESTIMATE_CAVEAT}")
    print(f"  Average daily grid import:     {kwh(summary.average_daily_grid_import_kwh)}")
    print(f"  Average daily solar generated: {kwh(summary.average_daily_solar_generation_kwh)}")
    print(f"  Average daily net cost:        {money(summary.average_daily_net_cost)}")
    if not summary.intervals:
        print("  Note: no readings found for this period")


def metric(label: str, value: str, note: str = "", tone: str = "") -> str:
    note_html = f"<small>{html.escape(note)}</small>" if note else ""
    return (
        f'<div class="metric {html.escape(tone)}">'
        f"<span>{html.escape(label)}</span><strong>{html.escape(value)}</strong>{note_html}"
        "</div>"
    )


def bar_chart(summary: Summary, value_key: str, title: str, value_type: str) -> str:
    maximum = max((summary.buckets[bucket][value_key] for bucket in BUCKET_ORDER), default=0)
    rows = []
    for bucket in BUCKET_ORDER:
        value = summary.buckets[bucket][value_key]
        width = value / maximum * 100 if maximum else 0
        display_value = money(value) if value_type == "money" else kwh(value)
        peak_class = " peak" if bucket == "peak" else ""
        peak_badge = '<span class="peak-badge">Highest rate</span>' if bucket == "peak" else ""
        rows.append(
            f"""
            <div class="bar-row{peak_class}">
              <div class="bar-label"><span>{html.escape(BUCKET_LABELS[bucket])}</span>{peak_badge}</div>
              <div class="bar-track" role="img" aria-label="{html.escape(BUCKET_LABELS[bucket])}: {html.escape(display_value)}">
                <span class="bar-fill" style="width:{width:.1f}%"></span>
              </div>
              <strong>{html.escape(display_value)}</strong>
            </div>
            """
        )
    return f"""
    <div class="chart-card">
      <div class="chart-heading"><h3>{html.escape(title)}</h3><span>Synergy EV tariff</span></div>
      <div class="bar-chart">{''.join(rows)}</div>
    </div>
    """


def actual_ev_message(summary: Summary) -> str:
    breakdown = summary.actual_ev_tariff_breakdown
    source_label = (
        "Ocular charging history"
        if summary.ev_charging_source == "ocular_export"
        else "Home Assistant OCPP"
    )
    if not breakdown:
        return f"Actual charging has been imported from {source_label}."
    dominant = max(EV_BUCKET_ORDER, key=lambda bucket: breakdown[bucket]["kwh"])
    total = sum(breakdown[bucket]["kwh"] for bucket in EV_BUCKET_ORDER)
    if total <= 0:
        return f"Actual charging has been imported from {source_label}."
    share = breakdown[dominant]["kwh"] / total
    if share < 0.5:
        return "Charging was split across tariff windows."
    if dominant == "super_off_peak":
        return "Charging was mostly in the cheapest 9am-3pm EV window."
    if dominant == "overnight":
        return "Charging was mostly overnight."
    return f"Charging was mostly during {EV_BUCKET_LABELS[dominant]}."


def actual_ev_card_html(summary: Summary, settings: Settings) -> str:
    if summary.ev_charging_source not in {"home_assistant_ocpp", "ocular_export"}:
        return ""
    cheapest_cost = summary.actual_ev_charging_kwh * settings.import_rates["super_off_peak"]
    peak_cost = summary.actual_ev_charging_kwh * settings.import_rates["peak"]
    savings_vs_peak = max(peak_cost - summary.actual_ev_charging_cost, 0.0)
    maximum = max(
        (summary.actual_ev_tariff_breakdown[bucket]["kwh"] for bucket in EV_BUCKET_ORDER),
        default=0,
    )
    rows = []
    for bucket in EV_BUCKET_ORDER:
        values = summary.actual_ev_tariff_breakdown[bucket]
        width = values["kwh"] / maximum * 100 if maximum else 0
        rows.append(
            f"""
            <div class="actual-ev-breakdown-row {' peak' if bucket == 'peak' else ''}">
              <div><span>{html.escape(EV_BUCKET_LABELS[bucket])}</span><strong>{html.escape(kwh(values['kwh']))}</strong></div>
              <div class="actual-ev-track" role="img" aria-label="{html.escape(EV_BUCKET_LABELS[bucket])}: {kwh(values['kwh'])}, {money(values['cost'])}">
                <span style="width:{width:.1f}%"></span>
              </div>
              <strong>{html.escape(money(values['cost']))}</strong>
            </div>
            """
        )
    partial = (
        '<div class="actual-ev-partial"><strong>Partial session cost</strong><span>PowerMonitor started observing this transaction after energy had already accumulated, so earlier energy is not back-costed.</span></div>'
        if summary.actual_ev_cost_partial
        else ""
    )
    return f"""
    <section class="actual-ev-card">
      <div class="actual-ev-head">
        <div><span>{html.escape(ev_note(settings, summary))}</span><h3>Actual EV charging</h3></div>
        <p>{html.escape(actual_ev_message(summary))}</p>
      </div>
      <div class="actual-ev-metrics">
        <div><span>Actual EV charging</span><strong>{html.escape(kwh(summary.actual_ev_charging_kwh))}</strong></div>
        <div><span>{html.escape(ev_cost_label(settings, summary))}</span><strong>{html.escape(money(summary.actual_ev_charging_cost))}</strong></div>
        <div><span>Cheapest equivalent / Peak comparison</span><strong>{html.escape(money(cheapest_cost))} / {html.escape(money(peak_cost))}</strong></div>
        <div><span>Estimated savings vs Peak</span><strong>{html.escape(money(savings_vs_peak))}</strong></div>
      </div>
      {partial}
      <div class="actual-ev-breakdown">
        <div class="chart-heading"><h3>Tariff bucket breakdown</h3><span>Imported session energy</span></div>
        {''.join(rows)}
      </div>
    </section>
    """


def cost_per_kwh(value: float) -> str:
    return f"${value:.3f}/kWh"


def compact_session_label(session: Optional[dict]) -> tuple[str, str]:
    if not session:
        return "n/a", "No session data"
    date_text = session.get("date") or "unknown date"
    start_text = session.get("start_time") or "n/a"
    end_text = session.get("end_time") or "n/a"
    return (
        f"{date_text} {start_text}-{end_text}",
        f"{kwh(float(session.get('energy_kwh') or 0))}, {money(float(session.get('cost') or 0))}",
    )


def ev_session_analytics_html(summary: Summary, settings: Settings) -> str:
    analytics = summary.ev_session_analytics
    if not analytics.get("available"):
        return ""
    overview = analytics["overview"]
    insights = analytics["insights"]
    tariff_usage = analytics["tariff_usage"]
    sessions = analytics["sessions"]
    total_kwh = overview["total_kwh"]

    overview_cards = "".join(
        [
            metric("Total EV kWh this period", kwh(overview["total_kwh"]), ev_note(settings, summary), "primary"),
            metric("Total EV cost", money(overview["total_cost"]), ev_cost_label(settings, summary), "cost"),
            metric("Average session cost", money(overview["average_session_cost"]), "Across actual charger sessions", "ev"),
            metric("Average session size", kwh(overview["average_session_kwh"]), "Mean imported session energy", "ev"),
            metric("Average effective $/kWh", cost_per_kwh(overview["average_effective_cost_per_kwh"]), "Total cost divided by EV kWh", "cost"),
        ]
    )

    tariff_rows = []
    for bucket in EV_BUCKET_ORDER:
        values = tariff_usage.get(bucket, {"kwh": 0.0, "cost": 0.0, "pct": 0.0})
        width = float(values.get("pct") or 0)
        tariff_rows.append(
            f"""
            <div class="ev-tariff-row {' peak' if bucket == 'peak' else ''}">
              <div><span>{html.escape(EV_BUCKET_LABELS.get(bucket, bucket))}</span><strong>{kwh(float(values.get('kwh') or 0))} / {width:.1f}%</strong></div>
              <div class="actual-ev-track" role="img" aria-label="{html.escape(EV_BUCKET_LABELS.get(bucket, bucket))}: {width:.1f}%">
                <span style="width:{width:.1f}%"></span>
              </div>
              <strong>{money(float(values.get('cost') or 0))}</strong>
            </div>
            """
        )

    cheapest_title, cheapest_detail = compact_session_label(insights.get("cheapest_session"))
    expensive_title, expensive_detail = compact_session_label(insights.get("most_expensive_session"))
    longest = insights.get("longest_session") or {}
    highest = insights.get("highest_power_session") or {}
    insight_cards = "".join(
        [
            metric("Cheapest session", cheapest_title, cheapest_detail, "good"),
            metric("Most expensive session", expensive_title, expensive_detail, "bad" if (insights.get("most_expensive_session") or {}).get("cost", 0) else "neutral"),
            metric("Longest session", compact_session_label(longest)[0], format_duration(longest.get("duration_minutes")) if longest else "No session data", "ev"),
            metric(
                "Highest power session",
                compact_session_label(highest)[0],
                f"{float(highest.get('max_power_kw') or 0):.2f} kW from charger data" if highest else "Unavailable from imported sessions",
                "ev",
            ),
            metric("Average duration", format_duration(insights.get("average_duration_minutes")), f"{insights.get('duration_sample_count') or 0} sessions with reliable duration", "ev"),
            metric("Number of sessions", str(insights["session_count"]), "Imported charger sessions", "ev"),
            metric("Charging days", str(insights["charging_days"]), "Unique days with charging", "ev"),
            metric("Average start time", insights.get("average_start_time") or "n/a", "Circular average across imported sessions", "ev"),
            metric("Average finish time", insights.get("average_finish_time") or "n/a", "Circular average across imported sessions", "ev"),
        ]
    )

    recommendation_html = ""
    if analytics["recommendations"]:
        recommendation_html = f"""
        <div class="ev-session-recommendations">
          <div class="chart-heading"><h3>EV charging recommendations</h3><span>Session based</span></div>
          <ul>{"".join(f"<li>{html.escape(item.recommendation)}</li>" for item in analytics["recommendations"])}</ul>
        </div>
        """

    history_rows = []
    for session in sessions:
        date_text = session.get("date") or "n/a"
        try:
            date_text = date.fromisoformat(date_text).strftime("%d %b")
        except ValueError:
            pass
        dominant = session.get("dominant_tariff_label") or "n/a"
        history_rows.append(
            f"""
            <tr>
              <td>{html.escape(date_text)}</td>
              <td>{html.escape(str(session.get("start_time_display") or "n/a"))}</td>
              <td>{html.escape(str(session.get("end_time_display") or "n/a"))}</td>
              <td>{html.escape(str(session.get("duration_display") or "n/a"))}</td>
              <td>{kwh(float(session.get("energy_kwh") or 0))}</td>
              <td>{money(float(session.get("cost") or 0))}</td>
              <td>{html.escape(dominant)}</td>
              <td>{cost_per_kwh(float(session.get("cost_per_kwh") or 0))}</td>
            </tr>
            """
        )

    ignored_notice = (
        f'<p class="notice">{html.escape(str(analytics.get("ignored_incomplete_message") or ""))}</p>'
        if analytics.get("ignored_incomplete_message") else ""
    )
    return f"""
    <section class="report-section ev-session-section">
      <div class="section-title">
        <div><span class="eyebrow">EV charging</span><h2>Actual charging sessions</h2></div>
        <p>{len(sessions)} sessions <span>·</span> {kwh(total_kwh)}</p>
      </div>
      {ignored_notice}
      <div class="metrics ev-overview-metrics">{overview_cards}</div>
      <div class="ev-session-grid">
        <div class="ev-session-card">
          <div class="chart-heading"><h3>Tariff usage</h3><span>Stored session allocation</span></div>
          <div class="ev-tariff-list">{''.join(tariff_rows)}</div>
        </div>
        <div class="ev-session-card">
          <div class="chart-heading"><h3>Insights</h3><span>Session summary</span></div>
          <div class="metrics ev-insight-metrics">{insight_cards}</div>
        </div>
      </div>
      {recommendation_html}
      <div class="ev-session-history">
        <div class="chart-heading"><h3>Session history</h3><span>Latest first</span></div>
        <div class="ev-session-table-wrap">
          <table>
            <thead><tr><th>Date</th><th>Start</th><th>Finish</th><th>Duration</th><th>kWh</th><th>Cost</th><th>Dominant tariff</th><th>Cost/kWh</th></tr></thead>
            <tbody>{''.join(history_rows)}</tbody>
          </table>
        </div>
      </div>
    </section>
    """


def ev_tariff_breakdown_html(summary: Summary) -> str:
    return ""


def summary_html(title: str, summary: Summary, settings: Settings) -> str:
    peak_cost = summary.buckets["peak"]["cost"]
    no_data = '<p class="notice">No readings found for this period.</p>' if not summary.intervals else ""
    headline_cards = "".join(
        [
            metric("Net estimated cost", money(summary.net_cost), "Current billing period estimate", "primary"),
            metric("Average daily cost", money(summary.average_daily_net_cost), f"Across {summary.supply_days} elapsed days", "cost"),
            metric("Peak usage cost", money(peak_cost), f"{summary.peak_cost_share_pct:.1f}% of import cost", "bad" if peak_cost else "grid"),
            metric("Solar export credit", f"-{money(summary.export_credit)}", f"{kwh(summary.solar_exported_kwh)} exported", "credit"),
        ]
    )
    detail_cards = "".join(
        [
            metric("Total before export credit", money(summary.total_cost), "Import plus daily supply", "cost"),
            metric("Grid import", kwh(summary.grid_import_kwh), "Energy drawn from the grid", "grid"),
            metric("Import cost", money(summary.import_cost), "Usage charges by tariff", "cost"),
            metric(
                "Supply charge",
                money(summary.supply_cost),
                f"{money(settings.supply_charge_per_day)}/day x {summary.supply_days}",
                "supply",
            ),
            metric("Solar generated", kwh(summary.solar_generated_kwh), "Total rooftop production", "solar"),
            metric("Solar used directly", kwh(summary.solar_used_kwh), "Production consumed at home", "solar"),
            metric("Solar exported", kwh(summary.solar_exported_kwh), "Sent back to the grid", "export"),
            metric("Solar self-consumption", f"{summary.self_consumption_pct:.1f}%", "Solar kept and used at home", "solar"),
            metric("Solar export percentage", f"{summary.solar_export_pct:.1f}%", "Share of generation exported", "export"),
            metric(
                ev_energy_label(settings, summary),
                kwh(summary.ev_charging_kwh),
                ev_note(settings, summary),
                "ev",
            ) if summary.ev_charging_source not in {"home_assistant_ocpp", "ocular_export"} else "",
            metric(ev_cost_label(settings, summary), money(summary.ev_charging_cost), ev_note(settings, summary), "ev") if summary.ev_charging_source not in {"home_assistant_ocpp", "ocular_export"} else "",
        ]
    )
    insights = "".join(
        [
            f"""
            <div class="insight peak-insight">
              <span class="insight-kicker">Peak exposure</span>
              <strong>{summary.peak_cost_share_pct:.1f}% of import cost</strong>
              <p>{money(peak_cost)} was spent during the expensive Peak window.</p>
            </div>
            """,
            f"""
            <div class="insight solar-insight">
              <span class="insight-kicker">Solar performance</span>
              <strong>{summary.self_consumption_pct:.1f}% self-consumed</strong>
              <p>{kwh(summary.solar_used_kwh)} of generated solar was used directly.</p>
            </div>
            """,
            f"""
            <div class="insight ev-insight">
              <span class="insight-kicker">{html.escape(ev_energy_label(settings, summary))}</span>
              <strong>{kwh(summary.ev_charging_kwh)}</strong>
              <p>{html.escape(ev_note(settings, summary))}, costing about {money(summary.ev_charging_cost)}.</p>
            </div>
            """,
        ]
    )
    return f"""
    <section class="report-section bills-detail-section">
      <div class="section-title">
        <div>
          <span class="eyebrow">Why is it that amount?</span>
          <h2>{html.escape(title)}</h2>
        </div>
        <p>{summary.start.strftime("%d %b %Y")} <span>to</span> {summary.end.strftime("%d %b %Y")}</p>
      </div>
      {no_data}
      <div class="metrics bills-breakdown-grid">{headline_cards}</div>
      <p class="scenario-note">Start here: the bill estimate, daily run-rate, peak exposure, and solar credit usually explain the household story fastest.</p>
      <div class="insights">{insights}</div>
    </section>
    """


def advanced_bill_details_html(summary: Summary, settings: Settings) -> str:
    detail_cards = "".join(
        [
            metric("Total before export credit", money(summary.total_cost), "Import plus daily supply", "cost"),
            metric("Grid import", kwh(summary.grid_import_kwh), "Energy drawn from the grid", "grid"),
            metric("Import cost", money(summary.import_cost), "Usage charges by tariff", "cost"),
            metric(
                "Supply charge",
                money(summary.supply_cost),
                f"{money(settings.supply_charge_per_day)}/day x {summary.supply_days}",
                "supply",
            ),
            metric("Solar generated", kwh(summary.solar_generated_kwh), "Total rooftop production", "solar"),
            metric("Solar used directly", kwh(summary.solar_used_kwh), "Production consumed at home", "solar"),
            metric("Solar exported", kwh(summary.solar_exported_kwh), "Sent back to the grid", "export"),
            metric("Solar self-consumption", f"{summary.self_consumption_pct:.1f}%", "Solar kept and used at home", "solar"),
            metric("Solar export percentage", f"{summary.solar_export_pct:.1f}%", "Share of generation exported", "export"),
            metric(
                ev_energy_label(settings, summary),
                kwh(summary.ev_charging_kwh),
                ev_note(settings, summary),
                "ev",
            ) if summary.ev_charging_source not in {"home_assistant_ocpp", "ocular_export"} else "",
            metric(ev_cost_label(settings, summary), money(summary.ev_charging_cost), ev_note(settings, summary), "ev") if summary.ev_charging_source not in {"home_assistant_ocpp", "ocular_export"} else "",
        ]
    )
    return f"""
    <section class="report-section compact-section bills-detail-section bills-advanced-section">
      <div class="section-title"><div><span class="eyebrow">Advanced bill details</span><h2>Tariff and calculation details</h2></div><p>Available when you want the deeper breakdown</p></div>
      {disclosure_html("Detailed bill calculations", "Tariff, solar, EV, and average daily components", f'''
      <div class="metrics bills-detail-calculation-grid">{detail_cards}</div>
      {actual_ev_card_html(summary, settings)}
      ''')}
      {disclosure_html("Tariff charts", "Grid import and cost by tariff bucket", f'''
      <div class="charts">
        {bar_chart(summary, "kwh", "Grid import by tariff bucket", "kwh")}
        {bar_chart(summary, "cost", "Import cost by tariff bucket", "money")}
        {ev_tariff_breakdown_html(summary)}
      </div>
      ''')}
    </section>
    """


def display_timestamp(value: Optional[str]) -> str:
    if not value:
        return "Unavailable"
    try:
        return datetime.fromisoformat(value.replace(",", ".")).astimezone().strftime("%d %b %Y, %H:%M %Z")
    except ValueError:
        return value


def friendly_label(value: object) -> str:
    return str(value or "Unknown").replace("_", " ").replace("-", " ").title()


def page_heading_html(title: str, description: str, parent: str = "Home") -> str:
    return f"""
    <div class="page-heading">
      <div class="page-breadcrumb"><a href="#home">{html.escape(parent)}</a><span>/</span><strong>Current page</strong></div>
      <h2>{html.escape(title)}</h2>
      <p>{html.escape(description)}</p>
    </div>
    """


def disclosure_html(title: str, summary: str, body: str, *, open_by_default: bool = False, css_class: str = "") -> str:
    open_attr = " open" if open_by_default else ""
    class_attr = f"disclosure-panel {html.escape(css_class)}".rstrip()
    return f"""
    <details class="{class_attr}"{open_attr}>
      <summary><span>{html.escape(title)}</span><small>{html.escape(summary)}</small></summary>
      <div class="disclosure-body">{body}</div>
    </details>
    """


def recommendation_confidence_note(decision) -> str:
    confidence = recommendation_confidence(decision)
    if is_recommendation_presentation(decision):
        rationale = recommendation_presentation_value(decision, "rationale", "why_we_think_this")
        if rationale:
            return trust_wording(rationale)
    explanation = getattr(decision, "explanation", None)
    if explanation and getattr(explanation, "confidence_reason", None):
        return trust_wording(explanation.confidence_reason)
    if str(confidence).lower() == "high":
        return "The available data is strong enough to act on this now."
    if str(confidence).lower() == "medium":
        return "Some uncertainty remains; this is based on available readings and tariff timing."
    return "Low confidence: we can't verify enough live household data, so review before acting."


def recommendations_html(recommendations: list) -> str:
    if not recommendations:
        return ""
    cards = []
    for decision in recommendations:
        cards.append(
            f"""
            <article class="recommendation-card {html.escape(decision.severity)}">
              <span class="recommendation-level">{html.escape(recommendation_confidence(decision))} confidence</span>
              <h3>{html.escape(recommendation_title(decision))}</h3>
              <div class="decision-lines">
                <p><strong>Finding</strong>{html.escape(decision.summary if is_recommendation_presentation(decision) else decision.finding)}</p>
                <p><strong>Financial impact</strong>{html.escape(recommendation_impact(decision))}</p>
                <p><strong>Recommendation</strong>{html.escape(recommendation_action(decision))}</p>
              </div>
              {"" if is_recommendation_presentation(decision) else decision_explanation_html(decision.explanation)}
            </article>
            """
        )
    return f"""
    <section class="report-section recommendation-section">
      <div class="section-title">
        <div><span class="eyebrow">Actionable insights</span><h2>Recommendations</h2></div>
        <p>{len(recommendations)} relevant now</p>
      </div>
      <div class="recommendation-grid">{''.join(cards)}</div>
    </section>
    """


def best_next_action_html(
    *,
    title: str,
    action: str,
    reason: str,
    impact: str = "",
    confidence: str = "",
) -> str:
    detail_rows = "".join(
        row
        for row in (
            f"<div><span>Why this matters</span><strong>{html.escape(trust_wording(reason))}</strong></div>" if reason else "",
            f"<div><span>Financial impact</span><strong>{html.escape(trust_wording(impact))}</strong></div>" if impact else "",
            f"<div><span>Confidence</span><strong>{html.escape(confidence)}</strong></div>" if confidence else "",
        )
        if row
    )
    return f"""
    <section class="report-section compact-section best-next-action-section">
      <div class="section-title"><div><span class="eyebrow">Best Next Action</span><h2>{html.escape(title)}</h2></div></div>
      <div class="decision-grid">
        <div><span>What should I do?</span><strong>{html.escape(trust_wording(action))}</strong></div>
        {detail_rows}
      </div>
    </section>
    """


def recommendation_best_next_action_html(recommendations: list, empty_action: str, empty_reason: str) -> str:
    if recommendations:
        item = sorted(recommendations, key=recommendation_sort_key)[0]
        return best_next_action_html(
            title=recommendation_title(item),
            action=recommendation_action(item),
            reason=recommendation_why(item),
            impact=recommendation_impact(item),
            confidence=f"{recommendation_confidence(item)} confidence",
        )
    return best_next_action_html(
        title="Nothing specific needs action right now.",
        action=empty_action,
        reason=empty_reason,
        confidence="PowerMonitor will update this when household data supports a specific recommendation.",
    )


def hero_html(
    period_summary: Summary,
    generated_at: str,
    health: dict,
    recommendations: list[Decision],
    period_label: str = "Current billing period",
) -> str:
    status_class = "healthy" if health["status"] == "ok" else "warning"
    status_text = collector_status_text(health)
    days_since_success = health.get("days_since_last_successful_collection")
    days_since_text = (
        f"{days_since_success:.2f} days" if days_since_success is not None else "Unavailable"
    )
    report_age = health.get("report_age_minutes")
    report_age_text = f"{report_age:.1f} minutes" if report_age is not None else "Unavailable"
    high_cost = next(
        (item for item in recommendations if item.id == "high_cost_alert"),
        None,
    )
    hero_class = " high-cost" if high_cost and high_cost.severity == "red" else ""
    alert_html = ""
    if high_cost:
        projected = high_cost.evidence["projected_net_cost"]
        confidence = high_cost.confidence
        alert_html = (
            f'<div class="hero-alert {html.escape(high_cost.severity)}">'
            f"<span>{html.escape(confidence.title())}-confidence cost projection</span>"
            f"<strong>Projected period cost {money(projected)}</strong></div>"
        )
    return f"""
    <header class="hero{hero_class}">
      <div class="hero-top">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
          <div><span>Local energy dashboard</span><h1>PowerMonitor</h1></div>
        </div>
        <div class="generated"><span>Last generated</span><strong>{html.escape(generated_at)}</strong></div>
      </div>
      <div class="hero-summary">
        <div>
          <span class="hero-label">{html.escape(period_label)} net estimated cost</span>
          <strong>{money(period_summary.net_cost)}</strong>
          <p>{period_summary.start.strftime("%d %b")} to {period_summary.end.strftime("%d %b %Y")}</p>
        </div>
        <div class="hero-note">
          <span>Estimate</span>
          <p>{money(period_summary.total_cost)} before an estimated {money(period_summary.export_credit)} solar export credit.</p>
        </div>
      </div>
      {alert_html}
      <div class="status-strip">
        <div><span>Last generated</span><strong>{html.escape(generated_at)}</strong></div>
        <div><span>Latest SolarEdge reading</span><strong>{html.escape(display_timestamp(health["latest_measurement_time"]))}</strong></div>
        <div><span>Live data status</span><strong class="status-value {status_class}">{html.escape(status_text)}</strong></div>
        <div><span>Since successful refresh</span><strong>{html.escape(days_since_text)}</strong></div>
        <div><span>Report age</span><strong>{html.escape(report_age_text)}</strong></div>
      </div>
    </header>
    """


def trend_bar(value: float, maximum: float, css_class: str, label: str) -> str:
    width = value / maximum * 100 if maximum else 0
    return (
        f'<span class="trend-track" role="img" aria-label="{html.escape(label)}">'
        f'<i class="{html.escape(css_class)}" style="width:{width:.1f}%"></i></span>'
    )


def trends_html(trends: list[DailyTrend], settings: Settings) -> str:
    if not trends:
        return """
        <section class="report-section trend-section">
          <div class="section-title"><div><span class="eyebrow">Daily analytics</span><h2>Billing period trends</h2></div></div>
          <p class="notice">No daily trend data is available for this billing period.</p>
        </section>
        """

    max_cost = max(max(day.import_cost, day.net_cost, day.export_credit) for day in trends)
    max_energy = max(max(day.grid_import_kwh, day.solar_generated_kwh) for day in trends)
    max_ev = max(day.ev_charging_kwh for day in trends)
    trend_ev_source = (
        "ocular_export"
        if any(day.ev_charging_source == "ocular_export" for day in trends)
        else "home_assistant_ocpp"
        if any(day.ev_charging_source == "home_assistant_ocpp" for day in trends)
        else "estimated"
    )
    trend_ev_label = (
        "Actual EV charging"
        if trend_ev_source in {"home_assistant_ocpp", "ocular_export"}
        else "Estimated overnight EV charging"
    )
    trend_ev_note = (
        "Home Assistant OCPP charger sessions"
        if trend_ev_source == "home_assistant_ocpp"
        else "Ocular charging history export"
        if trend_ev_source == "ocular_export"
        else f"{settings.ev_baseline_watts:.0f} W baseline method"
    )
    cost_rows = []
    energy_rows = []
    ev_rows = []
    for day in trends:
        label = day.day.strftime("%d %b")
        cost_rows.append(
            f"""
            <div class="trend-row cost-trend">
              <strong>{label}</strong>
              <div class="trend-bars">
                {trend_bar(day.import_cost, max_cost, "import-bar", f"{label} import cost {money(day.import_cost)}")}
                {trend_bar(day.net_cost, max_cost, "net-bar", f"{label} net cost {money(day.net_cost)}")}
                {trend_bar(day.export_credit, max_cost, "credit-bar", f"{label} export credit {money(day.export_credit)}")}
              </div>
              <span>{money(day.net_cost)}</span>
            </div>
            """
        )
        energy_rows.append(
            f"""
            <div class="trend-row energy-trend">
              <strong>{label}</strong>
              <div class="trend-bars">
                {trend_bar(day.grid_import_kwh, max_energy, "grid-bar", f"{label} grid import {kwh(day.grid_import_kwh)}")}
                {trend_bar(day.solar_generated_kwh, max_energy, "solar-bar", f"{label} solar generation {kwh(day.solar_generated_kwh)}")}
              </div>
              <span>{day.grid_import_kwh:.1f} / {day.solar_generated_kwh:.1f}</span>
            </div>
            """
        )
        ev_rows.append(
            f"""
            <div class="trend-row ev-trend">
              <strong>{label}</strong>
              <div class="trend-bars">
                {trend_bar(day.ev_charging_kwh, max_ev, "ev-bar", f"{label} {trend_ev_label.lower()} {kwh(day.ev_charging_kwh)}")}
              </div>
              <span>{day.ev_charging_kwh:.1f} kWh</span>
            </div>
            """
        )
    return f"""
    <section class="report-section trend-section">
      <div class="section-title">
        <div><span class="eyebrow">Daily analytics</span><h2>Billing period trends</h2></div>
        <p>{trends[0].day.strftime("%d %b %Y")} <span>to</span> {trends[-1].day.strftime("%d %b %Y")}</p>
      </div>
      <div class="trend-grid">
        <div class="trend-card">
          <div class="chart-heading"><h3>Daily cost trend</h3><span class="trend-legend"><i class="import-dot"></i>Import <i class="net-dot"></i>Net <i class="credit-dot"></i>Credit</span></div>
          <div class="trend-list">{''.join(cost_rows)}</div>
        </div>
        <div class="trend-card">
          <div class="chart-heading"><h3>Grid import vs solar generation</h3><span class="trend-legend"><i class="grid-dot"></i>Grid <i class="solar-dot"></i>Solar</span></div>
          <div class="trend-list">{''.join(energy_rows)}</div>
        </div>
        <div class="trend-card">
          <div class="chart-heading"><h3>{html.escape(trend_ev_label)}</h3><span>{html.escape(trend_ev_note)}</span></div>
          <div class="trend-list">{''.join(ev_rows)}</div>
        </div>
      </div>
    </section>
    """


def history_page_html(
    trends: list[DailyTrend],
    settings: Settings,
    insights: list[HouseholdInsight],
    energy_timeline: Optional[EnergyTimeline],
    display_timeline: DecisionTimeline,
    historical_trends: Optional[HistoricalTrends] = None,
) -> str:
    return f"""
    {page_heading_html("History", "The household story over time, including what happened recently, what looked unusual, and what it meant.")}
    {history_story_section_html(trends, settings, insights, historical_trends)}
    {energy_timeline_history_html(energy_timeline)}
    {history_comparison_summary_html(historical_trends, insights)}
    {household_insights_html(insights)}
    {trends_html(trends, settings)}
    <div id="timeline">
      {page_heading_html("Household History", "The household events, decisions, outcomes, and confidence changes worth remembering.", parent="History")}
      {timeline_html(display_timeline)}
    </div>
    """


def history_page_html(
    trends: list[DailyTrend],
    settings: Settings,
    insights: list[HouseholdInsight],
    energy_timeline: Optional[EnergyTimeline],
    display_timeline: DecisionTimeline,
    historical_trends: Optional[HistoricalTrends] = None,
) -> str:
    return f"""
    {page_heading_html("History", "The household story over time, including what changed, what looked unusual, and what it meant.")}
    {history_story_section_html(trends, settings, insights, historical_trends)}
    {energy_timeline_history_html(energy_timeline)}
    {history_comparison_summary_html(historical_trends, insights)}
    {household_insights_html(insights)}
    {trends_html(trends, settings)}
    <div id="timeline">
      {page_heading_html("Household History", "The household events, decisions, outcomes, and confidence changes worth remembering.", parent="History")}
      {timeline_html(display_timeline)}
    </div>
    """


def ev_accuracy_html(settings: Settings) -> str:
    if settings.ev_data_source == "ocular_export":
        return """
        <section class="ev-caveat" role="note">
          <div><span>EV data source</span><strong>Ocular charging history exports</strong></div>
          <p>Actual charger session energy is imported from Ocular Excel history exports. Re-importing all history is safe and updates existing sessions. If no Ocular sessions exist for a period, PowerMonitor falls back to estimated overnight EV charging above the configured baseload; daytime EV charging is not included unless charger session data is connected.</p>
        </section>
        """
    if settings.ev_data_source == "home_assistant_ocpp":
        return """
        <section class="ev-caveat" role="note">
          <div><span>EV data source</span><strong>Home Assistant OCPP sessions</strong></div>
          <p>Actual charger session energy is imported from Home Assistant OCPP sensors. The overnight grid-import estimate is used only when actual session data is unavailable.</p>
        </section>
        """
    if settings.ev_data_source != "estimated":
        return ""
    return f"""
    <section class="ev-caveat" role="note">
      <div><span>EV data source</span><strong>Estimated overnight grid import</strong></div>
      <p>{html.escape(EV_ESTIMATE_CAVEAT)}</p>
    </section>
    """


def authentication_alert_html(health: dict) -> str:
    status = str(health.get("status") or "").lower()
    failure_type = str(health.get("failure_type") or "").lower()
    auth_markers = ("session expired", "cookie expired", "credentials missing", "sign-in failed")
    if not any(marker in status for marker in auth_markers) and failure_type != "authentication failure":
        return ""
    last_success = display_timestamp(health.get("last_successful_collection"))
    title = "SolarEdge sign-in needs attention"
    if "credentials missing" in status:
        title = "SolarEdge credentials are missing"
    elif "session expired" in status or "cookie expired" in status:
        title = "SolarEdge session expired"
    return f"""
    <section class="auth-alert" role="alert">
      <div class="auth-alert-heading">
        <div><span>Live data needs attention</span><h2>{html.escape(title)}</h2></div>
        <strong>Last successful update: {html.escape(last_success)}</strong>
      </div>
      <ol>
        <li>Open Provider Management and update the SolarEdge username/email and password.</li>
        <li>Run Test Connection, then run <code>python3 collector/collect_today.py</code>.</li>
        <li>Use the manual cookie fallback only if automatic sign-in is unavailable.</li>
      </ol>
      <p>PowerMonitor never displays or logs SolarEdge usernames, passwords, cookies, tokens, or authorization headers.</p>
    </section>
    """


def period_controls_html(selected: Period, period_flash: Optional[dict] = None) -> str:
    choices = (
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("last_7_days", "Last 7 days"),
        ("last_30_days", "Last 30 days"),
        ("this_month", "This month"),
        ("last_month", "Last month"),
        ("year_to_date", "Year to date"),
        ("last_calendar_year", "Last calendar year"),
        ("current_billing_period", "Current billing period"),
        ("previous_billing_period", "Previous billing period"),
        ("custom", "Custom"),
    )
    options = "".join(
        f'<option value="{kind}"{" selected" if selected.kind == kind else ""}>{label}</option>'
        for kind, label in choices
    )
    flash_html = period_flash_html(period_flash)
    return f"""
    <section class="period-controls">
      {flash_html}
      <form method="get" action="/">
        <label><span>Dashboard period</span><select name="period">{options}</select></label>
        <label><span>Custom start</span><input type="date" name="start" value="{selected.start.isoformat()}"></label>
        <label><span>Custom end</span><input type="date" name="end" value="{selected.end.isoformat()}"></label>
        <button type="submit">Apply period</button>
      </form>
      <p>Showing <strong>{html.escape(selected.label)}</strong>: {selected.start.strftime("%d %b %Y")} to {selected.end.strftime("%d %b %Y")}</p>
    </section>
    """


def period_flash_html(flash: Optional[dict]) -> str:
    if not flash:
        return ""
    status = str(flash.get("status") or "warning").lower()
    message = str(flash.get("message") or "Dashboard period updated.")
    detail = str(flash.get("detail") or "")
    detail_html = f"<small>{html.escape(detail)}</small>" if detail else ""
    return f"""
    <div class="provider-action-result visible {html.escape(status)} period-flash" role="status">
      <span>Dashboard period</span>
      <strong>{html.escape(message)}</strong>
      {detail_html}
    </div>
    """


def nav_html() -> str:
    pages = (
        ("home", "Home"),
        ("bills", "Bills"),
        ("solar", "Solar"),
        ("ev", "EV"),
        ("whatif", "Decisions"),
        ("analytics", "History"),
        ("system", "Settings"),
    )
    return (
        '<div class="mobile-shortcuts"><a href="#home">Home</a><a href="#bills">Bills</a><a href="#whatif">Decisions</a><a href="#system">Settings</a></div>'
        '<nav class="top-nav" aria-label="Dashboard pages">'
        + "".join(f'<a href="#{page_id}">{html.escape(label)}</a>' for page_id, label in pages)
        + "</nav>"
    )


def projected_bill(summary: Summary, settings: Settings) -> tuple[float, date, str]:
    projected, end, basis, _impact = financial_engine().project_bill(summary, settings)
    return projected, end, basis


def glance_page_html(glance: GlanceState) -> str:
    score = str(glance.energy_score) if glance.energy_score is not None else "N/A"
    return f"""
    <section class="glance-display glance-{html.escape(glance.tone)}" aria-label="Glance Mode">
      <a class="glance-return" href="#home">Return Home</a>
      <div class="glance-topline">
        <div>
          <span>Glance Mode</span>
          <h2>Can I charge right now?</h2>
        </div>
        <div class="glance-freshness">
          <span>Freshness</span>
          <strong>{html.escape(glance.freshness)}</strong>
        </div>
      </div>
      <div class="glance-status">{html.escape(glance.status)}</div>
      <p class="glance-reason">{html.escape(glance.reason)}</p>
      <div class="glance-grid">
        <div class="glance-panel wide">
          <span>Current recommendation</span>
          <strong>{html.escape(glance.current_recommendation)}</strong>
        </div>
        <div class="glance-panel">
          <span>Confidence</span>
          <strong>{html.escape(glance.evidence_quality.title())}</strong>
          <small>{html.escape(trust_wording(glance.evidence_reason))}</small>
        </div>
        <div class="glance-panel">
          <span>Current tariff/window</span>
          <strong>{html.escape(glance.current_tariff)}</strong>
        </div>
        <div class="glance-panel">
          <span>Next cheaper window</span>
          <strong>{html.escape(glance.next_window)}</strong>
        </div>
        <div class="glance-panel">
          <span>Lowest-cost window</span>
          <strong>{html.escape(glance.lowest_cost_window)}</strong>
        </div>
        <div class="glance-panel score">
          <span>Energy Score</span>
          <strong>{html.escape(score)}</strong>
          <small>{html.escape(glance.energy_score_label)}</small>
        </div>
        <div class="glance-panel bill">
          <span>Current / projected bill</span>
          <strong>{money(glance.bill_current)} / {money(glance.bill_projected)}</strong>
          <small>{html.escape(glance.bill_projection_basis)}</small>
        </div>
        <div class="glance-panel solar">
          <span>Solar/export status</span>
          <strong>{html.escape(trust_wording(glance.solar_status))}</strong>
        </div>
        <div class="glance-panel">
          <span>Last updated</span>
          <strong>{html.escape(display_timestamp(glance.last_updated))}</strong>
        </div>
      </div>
    </section>
    """


def confidence_label(summary: Summary) -> tuple[str, str]:
    if not summary.intervals or not summary.reading_days:
        return "Low", "We can't verify live conditions right now."
    if summary.reading_days / max(summary.supply_days, 1) < 0.5:
        return "Low", "Some readings are missing, so confidence is reduced."
    if summary.ev_charging_source == "ocular_export":
        return "High", "Live readings plus charger session history"
    if summary.ev_charging_source == "estimated":
        return "Medium", "Uses tariff schedule and estimated EV charging, not charger session data"
    if summary.ev_charging_source == "home_assistant_ocpp":
        return "Medium", "Uses household readings plus Home Assistant charger snapshots"
    return "Low", "Limited source coverage"


def collector_status_text(health: dict) -> str:
    status = str(health.get("status") or "unknown")
    if status in {"SolarEdge session expired", "Cookie expired", "Sign-in failed"}:
        return "Live data sign-in needs attention"
    if status == "Credentials missing":
        return "SolarEdge credentials missing"
    if status == "SolarEdge unavailable":
        return "SolarEdge unavailable"
    return status.title()


def collector_status_explanation(health: dict) -> str:
    status = str(health.get("status") or "").lower()
    message = str(health.get("collector_message") or health.get("failure_type") or "")
    if "credentials missing" in status:
        return "SolarEdge needs saved username/email and password before live solar updates can recover automatically."
    if "sign-in failed" in status:
        return "SolarEdge sign-in failed. Update the saved credentials, then test the connection."
    if "cookie expired" in status or "session expired" in status:
        return "SolarEdge live updates need a fresh sign-in. PowerMonitor can still use recent household history cautiously."
    if "unavailable" in status:
        return "SolarEdge could not be reached. Recent household history may still be used while live solar is unavailable."
    return trust_wording(message or "Live updates could not be verified.")


def customer_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "AdvisorCollection": "recommendations",
        "DecisionEngine": "today's recommendation",
        "StateChangeEngine": "household changes",
        "DataEngine": "household data",
        "FinancialEngine": "bill estimate",
        "Recommendation Engine": "recommendations",
        "RecommendationPresentation": "recommendation",
        "Household Decision": "recommendation",
        "HouseholdDecision": "recommendation",
        "Household State": "household status",
        "Provider Health": "source health",
        "Household State Engine": "household status",
        "Collector health": "live data",
        "Provider observations": "source readings",
        "Provider Observation": "source reading",
        "Provider Observation pipeline": "source reading history",
        "Provider Runtime": "data connection status",
        "Provider Management": "data source setup",
        "provider session": "source sign-in",
        "Provider": "Data source",
        "provider": "source",
        "Runtime": "data connection",
        "Collector": "live data",
        "collector": "live data",
        "SolarEdge session expired": "We can't verify live conditions right now",
        "session expired": "live data sign-in needs attention",
        "Investigate anomaly": "Potential issue detected",
        "Investigate unusual household behaviour": "Potential issue detected",
        "Investigate unusual behaviour": "Potential issue detected",
        "active anomaly": "potential issue",
        "Evidence quality": "Confidence",
        "evidence quality": "confidence",
        "Not quantified": "Unable to estimate",
        "not quantified": "unable to estimate",
    }
    for internal, friendly in replacements.items():
        text = text.replace(internal, friendly)
    return text


def trust_wording(value: Any) -> str:
    text = customer_label(value)
    replacements = {
        "Charging advice needs fresher data": "We can't verify live conditions right now",
        "Avoid charging now": "Peak tariff now",
        "Avoid flexible loads": "Use tariff timing before starting flexible loads",
        "Low confidence because comparison data is unavailable": "Low confidence because some comparison data is missing",
        "Low confidence because solar interval evidence is unavailable": "Low confidence because live solar readings are unavailable",
        "Low confidence because overnight baseload data is unavailable": "Low confidence because overnight usage readings are unavailable",
        "available source data is stale": "live household readings are not fresh",
        "available source data is missing": "live household readings are missing",
        "available household data": "the household data PowerMonitor can verify",
        "available interval data": "available household readings",
        "interval data": "household readings",
        "evidence trail": "supporting data",
        "Evidence trail": "Supporting data",
        "anomaly": "potential issue",
        "Anomaly": "Potential issue",
        "HA OCPP": "Home Assistant charger",
    }
    for internal, friendly in replacements.items():
        text = text.replace(internal, friendly)
    return text


def customer_source(value: Any) -> str:
    text = customer_label(value)
    source_replacements = {
        "Data completeness summary": "Available household information",
        "Data source coverage": "Connected household sources",
        "Decision Engine": "PowerMonitor",
        "Recommendation Engine": "PowerMonitor",
        "Household State": "Household information",
        "Provider Observation": "Household update",
        "Provider Observations": "Household updates",
    }
    for internal, friendly in source_replacements.items():
        text = text.replace(internal, friendly)
    raw = text.strip()
    text = raw.replace("_", " ").strip()
    if not text:
        return "Household data"
    if "_" in raw or text.isupper():
        text = text.lower().title()
    text = text.replace("Ev ", "EV ").replace(" Ev", " EV")
    return text[:1].upper() + text[1:]


def unable_to_estimate() -> str:
    return "Unable to estimate"


def is_recommendation_presentation(item) -> bool:
    if isinstance(item, RecommendationPresentation):
        return True
    if isinstance(item, dict):
        return bool(item.get("recommendation_id") and item.get("recommended_action"))
    return bool(
        hasattr(item, "recommendation_id")
        and hasattr(item, "recommended_action")
        and hasattr(item, "rationale")
    )


def has_recommendation_value(value: Any) -> bool:
    return value is not None and value != ""


def recommendation_presentation_value(item, field: str, *fallback_fields: str) -> Optional[Any]:
    fields = (field, *fallback_fields)
    for name in fields:
        if isinstance(item, dict) and has_recommendation_value(item.get(name)):
            return item.get(name)
        if hasattr(item, name):
            value = getattr(item, name)
            if has_recommendation_value(value):
                return value
    if hasattr(item, "as_dict"):
        try:
            data = item.as_dict()
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            for name in fields:
                if has_recommendation_value(data.get(name)):
                    return data.get(name)
    containers = []
    for container_name in ("reason", "rationale", "explanation", "evidence", "decision", "advisor_explanation"):
        if isinstance(item, dict):
            container = item.get(container_name)
        else:
            container = getattr(item, container_name, None)
        if isinstance(container, dict):
            containers.append(container)
    for container in containers:
        for name in fields:
            if has_recommendation_value(container.get(name)):
                return container.get(name)
    return None


def recommendation_financial_impact_value(impact: Any, field: str) -> Optional[Any]:
    if not impact:
        return None
    if isinstance(impact, dict):
        return impact.get(field)
    return getattr(impact, field, None)


def recommendation_severity(decision) -> str:
    if is_recommendation_presentation(decision):
        urgency = recommendation_presentation_value(decision, "urgency") or {}
        if isinstance(urgency, dict):
            value = str(urgency.get("value") or "")
        else:
            value = str(getattr(urgency, "value", "") or "")
        return {
            "low": "green",
            "medium": "amber",
            "high": "red",
            "critical": "red",
        }.get(value.lower(), "amber")
    return decision.severity


def recommendation_confidence(decision) -> str:
    if is_recommendation_presentation(decision):
        return str(recommendation_presentation_value(decision, "confidence") or "Medium")
    return decision.confidence.title()


def recommendation_impact(decision) -> str:
    if is_recommendation_presentation(decision):
        impact = recommendation_presentation_value(decision, "financial_impact")
        if not impact:
            return unable_to_estimate()
        for value in (
            recommendation_financial_impact_value(impact, "today"),
            recommendation_financial_impact_value(impact, "billing_period"),
            recommendation_financial_impact_value(impact, "yearly"),
        ):
            if value is not None:
                return money(float(value))
        return unable_to_estimate()
    for key in ("today", "billing_period", "yearly"):
        value = decision.financial_impact.get(key)
        if value is not None:
            try:
                return money(float(value))
            except (TypeError, ValueError):
                return str(value)
    for key in (
        "estimated_savings",
        "savings",
        "savings_if_shifted",
        "monthly_savings_estimate",
        "estimated_period_savings",
        "estimated_savings_vs_peak",
    ):
        if key in decision.evidence:
            try:
                return money(float(decision.evidence[key]))
            except (TypeError, ValueError):
                return str(decision.evidence[key])
    for key in ("projected_net_cost", "current_estimate", "actual_cost"):
        if key in decision.evidence:
            try:
                return money(float(decision.evidence[key]))
            except (TypeError, ValueError):
                return str(decision.evidence[key])
    return unable_to_estimate()


def recommendation_monthly_impact(decision) -> str:
    if is_recommendation_presentation(decision):
        impact = recommendation_presentation_value(decision, "financial_impact")
        yearly = recommendation_financial_impact_value(impact, "yearly")
        if yearly is not None:
            return money(float(yearly) / 12)
        return unable_to_estimate()
    value = decision.evidence.get("estimated_monthly_impact")
    if value is None:
        return unable_to_estimate()
    try:
        return money(float(value))
    except (TypeError, ValueError):
        return str(value)


def recommendation_priority_score(decision) -> int:
    if is_recommendation_presentation(decision):
        try:
            return int(recommendation_presentation_value(decision, "display_priority", "priority_score") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(round(float(decision.evidence.get("priority_score", 0))))
    except (TypeError, ValueError):
        return {"High": 67, "Medium": 34, "Low": 0}.get(decision.priority, 0)


def recommendation_category(decision) -> str:
    if is_recommendation_presentation(decision):
        category = recommendation_presentation_value(decision, "category") or {}
        if isinstance(category, dict):
            return str(category.get("label") or category.get("value") or "Household")
        return str(getattr(category, "label", None) or getattr(category, "value", None) or "Household")
    return str(decision.evidence.get("recommendation_category") or decision.category).replace("_", " ")


def recommendation_difficulty(decision) -> str:
    if is_recommendation_presentation(decision):
        urgency = recommendation_presentation_value(decision, "urgency") or {}
        if isinstance(urgency, dict):
            label = str(urgency.get("label") or urgency.get("value") or "Medium")
        else:
            label = str(getattr(urgency, "label", None) or getattr(urgency, "value", None) or "Medium")
        return f"{label} urgency"
    return str(decision.evidence.get("difficulty") or "Medium").title()


def recommendation_title(decision) -> str:
    if is_recommendation_presentation(decision):
        return trust_wording(recommendation_presentation_value(decision, "title") or "Recommendation")
    return trust_wording(decision.title)


def recommendation_action(decision) -> str:
    action = (
        recommendation_presentation_value(decision, "recommended_action", "action")
        if is_recommendation_presentation(decision)
        else decision.recommendation
    )
    return trust_wording(action)


def recommendation_why(decision) -> str:
    if is_recommendation_presentation(decision):
        return trust_wording(
            recommendation_presentation_value(decision, "rationale", "why_we_think_this", "summary")
            or "This recommendation is based on the household information PowerMonitor can verify."
        )
    return (
        trust_wording(decision.explanation.why_this_matters)
        if decision.explanation
        else "This recommendation is based on the household data PowerMonitor can verify."
    )


def recommendation_what_happened(decision) -> str:
    if is_recommendation_presentation(decision):
        return trust_wording(
            recommendation_presentation_value(decision, "what_happened", "summary", "finding")
            or "PowerMonitor found something in the household information it can verify."
        )
    return trust_wording(decision.summary if hasattr(decision, "summary") else decision.finding)


def recommendation_why_we_think_this(decision) -> str:
    if is_recommendation_presentation(decision):
        return trust_wording(
            recommendation_presentation_value(decision, "why_we_think_this", "rationale", "summary")
            or "The available household information supports this recommendation, but detail is limited."
        )
    return recommendation_why(decision)


def recommendation_what_to_check(decision) -> str:
    if is_recommendation_presentation(decision):
        return trust_wording(
            recommendation_presentation_value(decision, "what_to_check", "recommended_action", "action")
            or "Review the related household data source and recent readings."
        )
    return trust_wording(recommendation_action(decision))


def recommendation_if_ignored(decision) -> str:
    if is_recommendation_presentation(decision):
        fallback = recommendation_presentation_value(decision, "if_ignored")
        if fallback:
            return trust_wording(fallback)
        impact = recommendation_impact(decision)
        if impact != unable_to_estimate():
            return f"The pattern may continue and could affect costs by around {impact}."
        return "The pattern may continue, but PowerMonitor cannot estimate the impact yet."
    impact = recommendation_impact(decision)
    if impact != unable_to_estimate():
        return f"The pattern may continue and could affect costs by around {impact}."
    return "The pattern may continue, but PowerMonitor cannot estimate the impact yet."


def recommendation_evidence_html(decision) -> str:
    if is_recommendation_presentation(decision):
        evidence = recommendation_presentation_value(decision, "evidence") or []
        if not evidence:
            return '<p class="notice">No supporting evidence is available for this recommendation.</p>'
        rows = []
        for item in evidence:
            signal = item.get("signal") if isinstance(item, dict) else getattr(item, "signal", None)
            value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
            source = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
            quality = item.get("quality") if isinstance(item, dict) else getattr(item, "quality", None)
            rows.append(
                f"<li><strong>{html.escape(customer_source(signal))}</strong><span>{html.escape(trust_wording(value))}</span><small>{html.escape(customer_source(source))} - {html.escape(customer_label(quality).title())}</small></li>"
            )
        return f'<ul class="recommendation-evidence">{"".join(rows)}</ul>'
    hidden_keys = {
        "priority_score",
        "recommendation_category",
        "estimated_monthly_impact",
        "difficulty",
        "confidence_reason",
        "lifecycle_state",
        "lifecycle_snoozed_until",
        "lifecycle_reason",
    }
    rows = []
    for key, value in decision.evidence.items():
        if key in hidden_keys or value is None:
            continue
        label = key.replace("_", " ").capitalize()
        rows.append(
            f"<li><strong>{html.escape(label)}</strong><span>{html.escape(trust_wording(value))}</span></li>"
        )
    if not rows:
        return '<p class="notice">No supporting evidence is available for this recommendation.</p>'
    return f'<ul class="recommendation-evidence">{"".join(rows)}</ul>'


def decision_type_for_recommendation(decision: Decision) -> list[str]:
    if decision.category == "EV":
        return ["CHARGE_NOW", "WAIT_FOR_CHEAPER_WINDOW", "WAIT_FOR_SOLAR"]
    if decision.category == "Bills":
        return ["REVIEW_BILLING"]
    if decision.category == "Solar":
        return ["WAIT_FOR_SOLAR", "MONITOR_USAGE"]
    if decision.category in {"Tariffs", "Behaviour"}:
        return ["MONITOR_USAGE", "WAIT_FOR_CHEAPER_WINDOW", "INVESTIGATE_ANOMALY"]
    if decision.action_type == "data_quality":
        return ["DATA_INSUFFICIENT", "REVIEW_PROVIDER"]
    return ["NO_ACTION", "MONITOR_USAGE"]


def matching_household_decision(decision, household_decisions: Optional[list[dict]]) -> Optional[dict]:
    if is_recommendation_presentation(decision):
        payload = recommendation_presentation_value(decision, "decision")
        return payload if isinstance(payload, dict) else None
    if not household_decisions:
        return None
    preferred = decision_type_for_recommendation(decision)
    for decision_type in preferred:
        for household_decision in household_decisions:
            if household_decision.get("type") == decision_type or household_decision.get("decision_type") == decision_type:
                return household_decision
    return household_decisions[0] if household_decisions else None


def recommendation_decision_provenance_html(decision, household_decisions: Optional[list[dict]]) -> str:
    household_decision = matching_household_decision(decision, household_decisions)
    if not household_decision:
        return '<p><strong>Recommendation</strong>No matching recommendation is ready yet.</p>'
    reasons = household_decision.get("reasons") or []
    reason = reasons[0].get("summary") if reasons and isinstance(reasons[0], dict) else "Recommendation reason"
    return f"""
      <p><strong>Recommendation</strong>{html.escape(customer_source(household_decision.get("type") or household_decision.get("decision_type") or "Recommendation"))}</p>
      <p><strong>Why this matters</strong>{html.escape(customer_label(reason))}</p>
    """


def recommendation_with_decision_dict(decision, household_decisions: Optional[list[dict]]) -> dict:
    if is_recommendation_presentation(decision):
        if hasattr(decision, "as_dict"):
            data = decision.as_dict()
        elif isinstance(decision, dict):
            data = dict(decision)
        else:
            data = dict(getattr(decision, "__dict__", {}))
        data["household_decision_id"] = recommendation_presentation_value(decision, "decision_id")
        return data
    data = decision.as_dict()
    household_decision = matching_household_decision(decision, household_decisions)
    data["decision"] = household_decision
    data["household_decision_id"] = household_decision.get("decision_id") if household_decision else None
    return data


def recommendation_controls_html(decision, hidden: bool = False) -> str:
    stable_id = html.escape(str(recommendation_presentation_value(decision, "id", "recommendation_id") if is_recommendation_presentation(decision) else decision.id))
    if hidden:
        return f"""
        <form class="recommendation-controls" method="post" action="/recommendations/lifecycle">
          <input type="hidden" name="stable_id" value="{stable_id}">
          <button type="submit" name="action" value="restore">Restore</button>
        </form>
        """
    return f"""
    <div class="recommendation-controls">
      <form method="post" action="/recommendations/lifecycle">
        <input type="hidden" name="stable_id" value="{stable_id}">
        <button type="submit" name="action" value="dismiss">Dismiss</button>
      </form>
      <form method="post" action="/recommendations/lifecycle">
        <input type="hidden" name="stable_id" value="{stable_id}">
        <button type="submit" name="action" value="snooze_1d">Snooze 1 day</button>
        <button type="submit" name="action" value="snooze_7d">7 days</button>
        <button type="submit" name="action" value="snooze_next_billing">Next bill</button>
      </form>
    </div>
    """


def recommendation_sort_key(decision) -> tuple[int, str]:
    return (-recommendation_priority_score(decision), recommendation_title(decision))


def recommendation_centre_html(
    recommendations: list[Decision],
    lifecycle_counts: Optional[dict[str, int]] = None,
    hidden: Optional[list[Decision]] = None,
    advisors: Optional[list[Advisor]] = None,
    goals: Optional[list[GoalProgress]] = None,
    household_decisions: Optional[list[dict]] = None,
) -> str:
    ranked = sorted(recommendations, key=recommendation_sort_key)
    hidden = sorted(hidden or [], key=recommendation_sort_key)
    advisors = advisors or []
    goals = goals or []
    lifecycle_counts = lifecycle_counts or {"dismissed": 0, "snoozed_until": 0}
    inactive_count = int(lifecycle_counts.get("dismissed", 0)) + int(lifecycle_counts.get("snoozed_until", 0))
    lifecycle_note = (
        f'<p class="notice">{inactive_count} hidden: '
        f'{int(lifecycle_counts.get("dismissed", 0))} dismissed, '
        f'{int(lifecycle_counts.get("snoozed_until", 0))} snoozed.</p>'
        if inactive_count
        else ""
    )
    cards = []
    for decision in ranked:
        why = recommendation_why(decision)
        confidence_note = recommendation_confidence_note(decision)
        goal_links = recommendation_goal_links_html(decision, goals)
        cards.append(
            f"""
            <article class="recommendation-card recommendation-centre-card {html.escape(recommendation_severity(decision))}">
              <div class="recommendation-centre-heading">
                <span class="recommendation-level">{html.escape(recommendation_confidence(decision))} confidence</span>
                <div><small>Priority score</small><strong>{recommendation_priority_score(decision)}</strong></div>
              </div>
              <h3>{html.escape(recommendation_title(decision))}</h3>
              <div class="recommendation-centre-meta">
                <span>{html.escape(recommendation_category(decision).title())}</span>
                <span>{html.escape(recommendation_difficulty(decision))} difficulty</span>
              </div>
              <div class="recommendation-metrics">
                <span><small>Period impact</small><strong>{html.escape(recommendation_impact(decision))}</strong></span>
                <span><small>Monthly impact</small><strong>{html.escape(recommendation_monthly_impact(decision))}</strong></span>
              </div>
              <div class="decision-lines">
                <p><strong>What happened?</strong>{html.escape(recommendation_what_happened(decision))}</p>
                <p><strong>Why? Why do we think this?</strong>{html.escape(recommendation_why_we_think_this(decision))}</p>
                <p class="primary-action"><strong>What should I check?</strong>{html.escape(recommendation_what_to_check(decision))}</p>
                <p><strong>What happens if I ignore it?</strong>{html.escape(recommendation_if_ignored(decision))}</p>
                <p><strong>Confidence</strong>{html.escape(confidence_note)}</p>
                {recommendation_decision_provenance_html(decision, household_decisions)}
                {goal_links}
              </div>
              <div class="recommendation-centre-evidence">
                <strong>Supporting evidence</strong>
                {recommendation_evidence_html(decision)}
              </div>
              {recommendation_controls_html(decision)}
            </article>
            """
        )
    hidden_cards = []
    for decision in hidden:
        state = recommendation_lifecycle_state(decision).replace("_", " ")
        evidence = recommendation_presentation_value(decision, "evidence") if is_recommendation_presentation(decision) else decision.evidence
        snoozed_until = evidence.get("lifecycle_snoozed_until") if isinstance(evidence, dict) else None
        state_text = f"{state}: {snoozed_until}" if snoozed_until else state
        hidden_cards.append(
            f"""
            <article class="recommendation-card recommendation-centre-card {html.escape(recommendation_severity(decision))}">
              <div class="recommendation-centre-heading">
                <span class="recommendation-level">{html.escape(state_text.title())}</span>
                <div><small>Priority score</small><strong>{recommendation_priority_score(decision)}</strong></div>
              </div>
              <h3>{html.escape(recommendation_title(decision))}</h3>
              <div class="recommendation-centre-meta">
                <span>{html.escape(recommendation_category(decision).title())}</span>
                <span>{html.escape(recommendation_difficulty(decision))} difficulty</span>
              </div>
              <div class="decision-lines">
                <p class="primary-action"><strong>Recommended action</strong>{html.escape(recommendation_action(decision))}</p>
              </div>
              {recommendation_controls_html(decision, hidden=True)}
            </article>
            """
        )
    hidden_section = (
        f"""
        <section class="report-section recommendation-centre-section">
          <div class="section-title">
            <div><span class="eyebrow">Dismissed / Snoozed</span><h2>Hidden recommendations</h2></div>
            <p>{len(hidden)} hidden</p>
          </div>
          <div class="recommendation-grid">{''.join(hidden_cards)}</div>
        </section>
        """
        if hidden_cards
        else ""
    )
    active_notice = (
        '<p class="notice">Nothing needs a decision right now. PowerMonitor will surface a recommendation here when the data points to a useful action.</p>'
        if not cards
        else ""
    )
    return f"""
    <section class="report-section recommendation-centre-section">
      <div class="section-title">
        <div><span class="eyebrow">Recommendation centre</span><h2>What should I do next?</h2></div>
        <p>{len(ranked)} active</p>
      </div>
      {lifecycle_note}
      {active_notice}
      <div class="recommendation-grid">{''.join(cards)}</div>
    </section>
    {hidden_section}
    """


def recommendation_goal_links_html(decision: Decision, goals: list[GoalProgress]) -> str:
    if not goals:
        return ""
    category = recommendation_category(decision).replace("_", " ").lower()
    titles = [
        goal.goal.title
        for goal in goals
        if any(item.replace("_", " ").lower() == category for item in goal.goal.recommendation_categories)
    ]
    if not titles:
        return ""
    return f"<p><strong>Contributes to goals</strong>{html.escape(', '.join(titles))}</p>"


def advisor_centre_html(advisors: list[Advisor]) -> str:
    if not advisors:
        return ""
    cards = []
    for advisor in advisors:
        evidence_summary = getattr(advisor, "evidence_summary", None)
        evidence_quality = evidence_summary.evidence_quality.value.title() if evidence_summary else "Unknown"
        missing = ", ".join(item.name for item in evidence_summary.missing_evidence) if evidence_summary else ""
        missing_text = trust_wording(missing or "No required data gaps declared.")
        confidence_reduced = trust_wording(evidence_summary.confidence_reason if evidence_summary else advisor.confidence_reason)
        cards.append(
            f"""
            <article class="recommendation-card recommendation-centre-card">
              <div class="recommendation-centre-heading">
                <span class="recommendation-level">{html.escape(advisor.confidence)} confidence</span>
                <div><small>Status</small><strong>{html.escape(advisor.status.replace("_", " "))}</strong></div>
              </div>
              <h3>{html.escape(trust_wording(advisor.headline))}</h3>
              <div class="recommendation-centre-meta">
                <span>{html.escape(advisor.advisor_type.replace("_", " ").title())}</span>
                <span>{html.escape(advisor.freshness)}</span>
              </div>
              <div class="decision-lines">
                <p class="primary-action"><strong>Recommended action</strong>{html.escape(trust_wording(advisor.recommendation))}</p>
                <p><strong>Why?</strong>{html.escape(trust_wording(advisor.confidence_reason))}</p>
                <p><strong>Confidence</strong>{html.escape(evidence_quality)}</p>
                <p><strong>Still missing</strong>{html.escape(missing_text)}</p>
                <p><strong>Confidence note</strong>{html.escape(confidence_reduced)}</p>
              </div>
            </article>
            """
        )
    return f"""
    <section class="report-section recommendation-centre-section">
      <div class="section-title">
        <div><span class="eyebrow">Supporting evidence</span><h2>Background checks</h2></div>
        <p>{len(advisors)} checks</p>
      </div>
      <p class="scenario-note">These checks explain confidence and missing context behind the recommendations above.</p>
      <div class="recommendation-grid">{''.join(cards)}</div>
    </section>
    """


def top_actions_html(recommendations: list) -> str:
    items = recommendations[:3]
    if not items:
        return """
        <section class="report-section compact-section">
          <div class="section-title"><div><span class="eyebrow">Top actions</span><h2>Nothing urgent</h2></div></div>
          <p class="notice">No high-priority recommendations are active for this period.</p>
        </section>
        """
    cards = []
    for item in items:
        cards.append(
            f"""
            <article class="action-card {html.escape(item.severity)}">
              <span>{html.escape(recommendation_confidence(item))} confidence</span>
              <h3>{html.escape(recommendation_title(item))}</h3>
              <p><strong>Finding</strong>{html.escape(item.summary if is_recommendation_presentation(item) else item.finding)}</p>
              <p><strong>Financial impact</strong>{html.escape(recommendation_impact(item))}</p>
              <p><strong>Recommendation</strong>{html.escape(recommendation_action(item))}</p>
              {"" if is_recommendation_presentation(item) else decision_explanation_html(item.explanation)}
            </article>
            """
        )
    return f"""
    <section id="top-actions" class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Top actions</span><h2>Next best moves</h2></div><p>Top {len(items)}</p></div>
      <div class="action-grid">{''.join(cards)}</div>
    </section>
    """


def timeline_event_icon(event_type: str) -> str:
    value = str(event_type or "").lower()
    if "bill" in value or "cost" in value:
        return "$"
    if "solar" in value:
        return "S"
    if "charging" in value or "ev" in value:
        return "EV"
    if "provider" in value or "data" in value or "system" in value:
        return "D"
    if "recommend" in value or "decision" in value:
        return "!"
    return "*"


def timeline_event_datetime(timestamp: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def timeline_event_group(timestamp: str) -> str:
    parsed = timeline_event_datetime(timestamp)
    if parsed is not None:
        return parsed.strftime("%A, %d %b")
    return "Recent events"


def timeline_group_summary(entries: list[TimelineEntry]) -> str:
    titles = " ".join(f"{entry.title} {entry.event_type}" for entry in entries[:3]).lower()
    if any(token in titles for token in ("solar", "export")):
        return "solar stood out"
    if any(token in titles for token in ("ev", "charging")):
        return "EV charging stood out"
    if any(token in titles for token in ("bill", "cost", "peak", "tariff")):
        return "higher-cost activity stood out"
    if any(token in titles for token in ("decision", "recommend", "state")):
        return "important household changes"
    return "worth a closer look"


def timeline_group_heading(day_label: str, entries: list[TimelineEntry]) -> str:
    if not entries:
        return day_label
    parsed = timeline_event_datetime(entries[0].timestamp)
    if parsed is None:
        return day_label
    local_day = parsed.date()
    today = local_today()
    if local_day == today:
        return "Today - still in progress"
    if local_day == today - timedelta(days=1):
        return f"Yesterday - {timeline_group_summary(entries)}"
    return f"{parsed.strftime('%A')} - {timeline_group_summary(entries)}"


def timeline_time_label(timestamp: str) -> str:
    parsed = timeline_event_datetime(timestamp)
    if parsed is not None:
        return parsed.strftime("%H:%M")
    return str(timestamp or "Time unknown")


def timeline_grouped_entries(entries: list[TimelineEntry]) -> list[tuple[str, list[TimelineEntry]]]:
    groups: list[tuple[str, list[TimelineEntry]]] = []
    current_key: Optional[str] = None
    current_entries: list[TimelineEntry] = []
    for entry in entries:
        key = timeline_event_group(entry.timestamp)
        if key != current_key:
            if current_entries:
                groups.append((current_key or "Recent events", current_entries))
            current_key = key
            current_entries = [entry]
        else:
            current_entries.append(entry)
    if current_entries:
        groups.append((current_key or "Recent events", current_entries))
    return groups


def timeline_html(timeline: DecisionTimeline) -> str:
    household_events = household_timeline_history_events_html()
    if not timeline.entries and not household_events:
        return """
        <section class="report-section timeline-section">
          <div class="section-title"><div><span class="eyebrow">Household timeline</span><h2>No important changes recorded yet.</h2></div></div>
          <p class="notice">Once PowerMonitor has enough history, this page will show the decisions, bill changes, and household events worth remembering.</p>
        </section>
        """
    rows = []
    for day_label, day_entries in timeline_grouped_entries(timeline.entries):
        rows.append(f'<div class="timeline-day">{html.escape(timeline_group_heading(day_label, day_entries))}</div>')
        for entry in day_entries:
            impact = (
                f'<small>{html.escape(entry.financial_impact)}</small>'
                if entry.financial_impact
                else "<small>Financial impact unavailable</small>"
            )
            sources = ", ".join(customer_source(source) for source in entry.evidence_sources) if entry.evidence_sources else "No sources"
            kind = customer_label(friendly_label(entry.event_type))
            rows.append(
                f"""
                <article class="timeline-entry">
                  <div class="timeline-stamp"><span aria-hidden="true">{html.escape(timeline_event_icon(entry.event_type))}</span><time>{html.escape(timeline_time_label(entry.timestamp))}</time></div>
                  <div>
                    <span>{html.escape(entry.confidence)} confidence</span>
                    <h3>{html.escape(entry.title)}</h3>
                    <p>{html.escape(entry.summary)}</p>
                    <div class="timeline-meta">
                      {impact}
                      <small>{html.escape(kind)}</small>
                      <small>Sources: {html.escape(sources)}</small>
                    </div>
                  </div>
                </article>
                """
            )
    return f"""
    <section class="report-section timeline-section">
      <div class="section-title">
        <div><span class="eyebrow">Household timeline</span><h2>What changed, and why it mattered</h2></div>
        <p>{len(timeline.entries)} events</p>
      </div>
      <p class="scenario-note">Start here to see the household events and recommendations PowerMonitor considered worth remembering.</p>
      {household_events}
      <div class="timeline-list">{''.join(rows)}</div>
    </section>
    """


def household_timeline_history_events_html() -> str:
    try:
        timeline = household_timeline()
    except (HouseholdTimelineError, ValueError, OSError):
        return ""
    events = [event for event in timeline.events if event.active]
    if not events:
        return ""
    rows = "".join(household_timeline_event_readonly_html(event) for event in sorted(events, key=lambda item: item.effective_from, reverse=True))
    return f'<div class="timeline-list household-history-list">{rows}</div>'


def household_timeline_event_readonly_html(event: HouseholdTimelineEvent) -> str:
    summary = (
        "Current household tariff"
        if event.event_type == "tariff_period" and event.effective_to is None
        else "Historical household tariff"
        if event.event_type == "tariff_period"
        else event.event_type.replace("_", " ").title()
    )
    date_text = f"From {display_date(event.effective_from)}" if event.effective_to is None else f"{display_date(event.effective_from)} to {display_date(event.effective_to)}"
    return f"""
    <article class="timeline-entry compact">
      <div class="timeline-stamp"><span aria-hidden="true">T</span><time>{html.escape(date_text)}</time></div>
      <div>
        <span>{html.escape(event.confidence)} confidence</span>
        <h3>{html.escape(event.title)}</h3>
        <p>{html.escape(summary)}{(' · ' + html.escape(event.notes)) if event.notes else ''}</p>
        <div class="timeline-meta"><small>Source: {html.escape(event.source.replace('_', ' '))}</small></div>
      </div>
    </article>
    """


def tariff_context_statement_html(tariff_resolution: Optional[dict], label: str = "Tariff history") -> str:
    if not tariff_resolution:
        return ""
    periods = tariff_resolution.get("periods_used") or []
    warnings = tariff_resolution.get("warnings") or []
    if len(periods) <= 1 and not warnings:
        return ""
    sentence = timeline_summary_sentence(tariff_resolution) or "Tariff history is incomplete for this period."
    warning = " ".join(str(item) for item in warnings[:2])
    warning_html = f"<p class='scenario-note'>{html.escape(warning)}</p>" if warning else ""
    return f"""
    <section class="report-section compact-section tariff-context-note">
      <div class="section-title"><div><span class="eyebrow">{html.escape(label)}</span><h2>{html.escape(sentence)}</h2></div><p>{html.escape(str(tariff_resolution.get("confidence") or "medium").title())} confidence</p></div>
      {warning_html}
    </section>
    """


def briefing_card_html(
    item: BriefingItem,
    eyebrow: str,
    heading: Optional[str] = None,
) -> str:
    explanation = decision_explanation_html(item.explanation)
    return f"""
    <article class="briefing-card {html.escape(item.severity)}">
      <div class="briefing-card-heading">
        <span>{html.escape(eyebrow)}</span>
        <strong>{html.escape(item.confidence)} confidence</strong>
      </div>
      <h3>{html.escape(trust_wording(heading or item.title))}</h3>
      <p>{html.escape(trust_wording(item.body))}</p>
      <div class="briefing-meta">
        <small>{html.escape(item.category)}</small>
        <small>{html.escape(item.financial_impact)}</small>
      </div>
      {explanation}
    </article>
    """


def decision_explanation_html(explanation) -> str:
    if not explanation:
        return ""
    evidence_items = "".join(
        f"<li>{html.escape(trust_wording(evidence))}</li>"
        for evidence in explanation.evidence_used
    )
    sources = (
        ", ".join(customer_source(source) for source in explanation.data_sources_used)
        if explanation.data_sources_used
        else "No decision sources"
    )
    freshness = (
        f"""
        <div>
          <span>Freshness/completeness</span>
          <p>{html.escape(explanation.freshness_completeness_note)}</p>
        </div>
        """
        if explanation.freshness_completeness_note
        else ""
    )
    return f"""
      <details class="decision-explanation">
        <summary>Why?</summary>
        <div class="decision-explanation-grid">
          <div>
            <span>Why this matters</span>
            <p>{html.escape(trust_wording(explanation.why_this_matters))}</p>
          </div>
          <div>
            <span>Financial impact</span>
            <p>{html.escape(trust_wording(explanation.financial_impact))}</p>
          </div>
          <div>
            <span>Confidence reason</span>
            <p>{html.escape(trust_wording(explanation.confidence_reason))}</p>
          </div>
          {freshness}
          <div>
            <span>Evidence used</span>
            <ul>{evidence_items}</ul>
          </div>
          <div>
            <span>Data sources used</span>
            <p>{html.escape(sources)}</p>
          </div>
        </div>
      </details>
    """


def briefing_opportunities_html(items: list[BriefingItem]) -> str:
    if not items:
        return """
        <section class="briefing-block">
          <div class="section-title"><div><span class="eyebrow">Opportunities</span><h2>Nothing else needs attention</h2></div></div>
          <p class="notice">No additional opportunities are active for this briefing.</p>
        </section>
        """
    cards = "".join(
        briefing_card_html(item, "Opportunity")
        for item in items
    )
    return f"""
    <section class="briefing-block">
      <div class="section-title"><div><span class="eyebrow">Opportunities</span><h2>Worth a quick look</h2></div><p>Up to three</p></div>
      <div class="briefing-grid">{cards}</div>
    </section>
    """


def daily_briefing_html(
    briefing: DailyBriefing,
    goals: Optional[list[GoalProgress]] = None,
    glance: Optional[GlanceState] = None,
    insights: Optional[list[HouseholdInsight]] = None,
    trends_report: Optional[HouseholdTrendsReport] = None,
    health: Optional[dict] = None,
    energy_timeline: Optional[EnergyTimeline] = None,
) -> str:
    action_items = dedupe_briefing_items([*briefing.top_priorities, *briefing.items_to_watch])
    positive_items = dedupe_briefing_items(briefing.positive_outcomes)
    primary_items = action_items[:1]
    secondary_items = dedupe_briefing_items([*action_items[1:], *positive_items])[:2]
    return f"""
    <section class="report-section briefing-section">
      {home_assistant_hero_html(briefing)}
      {home_snapshot_html(briefing, glance, health)}
      {home_yesterday_summary_html(trends_report)}
      {home_best_next_action_html(primary_items, secondary_items)}
    </section>
    """


def home_snapshot_html(briefing: DailyBriefing, glance: Optional[GlanceState], health: Optional[dict] = None) -> str:
    return f"""
      <section class="briefing-block home-snapshot-block">
        <div class="section-title"><div><span class="eyebrow">Today's Snapshot</span><h2>Am I doing okay today?</h2></div><p>Three quick checks</p></div>
        {home_status_strip_html(briefing, glance, health)}
      </section>
    """


def home_yesterday_summary_html(report: Optional[HouseholdTrendsReport]) -> str:
    comparison_note = "Yesterday comparison is not available yet."
    detail = "History already holds the longer household record and recent changes."
    if report and (report.trends or report.records):
        detail = "History has recent trends and records while yesterday-specific comparison is still being added."
    return f"""
      <section class="briefing-block home-yesterday-block">
        <div class="section-title"><div><span class="eyebrow">Compared with Yesterday</span><h2>{html.escape(comparison_note)}</h2></div></div>
        <div class="home-inline-summary">
          <p>{html.escape(detail)}</p>
          <a class="button-link" href="#analytics">View History</a>
        </div>
      </section>
    """


def home_best_next_action_html(primary_items: list[BriefingPriority], secondary_items: list[BriefingPriority]) -> str:
    lead = primary_items[0] if primary_items else secondary_items[0] if secondary_items else None
    if not lead:
        return """
      <section class="briefing-block home-action-block">
        <div class="section-title"><div><span class="eyebrow">Best Next Action</span><h2>Nothing important needs action.</h2></div></div>
        <div class="home-inline-summary">
          <p>Everything looks normal right now. Open Decisions when you want the full recommendation centre and scenario cards.</p>
          <a class="button-link" href="#whatif">View Decisions</a>
        </div>
      </section>
        """
    secondary_summary = ""
    if secondary_items:
        labels = "; ".join(trust_wording(item.title) for item in secondary_items[:2])
        secondary_summary = f"<p><strong>Also watching</strong>{html.escape(labels)}</p>"
    action_text = trust_wording(lead.recommendation or lead.summary or lead.title)
    severity = briefing_priority_severity(lead.status)
    return f"""
      <section class="briefing-block home-action-block">
        <div class="section-title"><div><span class="eyebrow">Best Next Action</span><h2>{html.escape(trust_wording(lead.title))}</h2></div><p>{html.escape(lead.confidence)} confidence</p></div>
        <article class="briefing-card {html.escape(severity)} home-action-summary-card">
          <div class="briefing-card-heading">
            <span>{html.escape(lead.advisor_type.replace("_", " ").title())}</span>
            <strong>{html.escape(lead.confidence)} confidence</strong>
          </div>
          <p class="primary-action">{html.escape(action_text)}</p>
          <div class="decision-lines">
            <p><strong>Why this matters</strong>{html.escape(trust_wording(lead.summary))}</p>
            {secondary_summary}
          </div>
          <div class="provider-action-row">
            <a class="button-link" href="#whatif">View Decisions</a>
          </div>
        </article>
      </section>
    """


def history_story_metric(
    trends: Optional[HistoricalTrends],
    group_name: str,
    metric_id: str,
) -> Optional[HistoricalTrendMetric]:
    if not trends:
        return None
    group = getattr(trends, group_name, None)
    if not group or not getattr(group, "available", False):
        return None
    metric = group.metrics.get(metric_id)
    if not historical_trend_metric_ready(metric):
        return None
    return metric


def history_story_headline(latest: DailyTrend, historical_trends: Optional[HistoricalTrends]) -> str:
    metric = history_story_metric(historical_trends, "cost", "rolling_7_day_average_cost")
    if metric and metric.comparison_value and metric.value:
        delta = float(metric.comparison_value) - float(metric.value)
        pct = abs(delta / float(metric.value)) * 100 if float(metric.value) else 0.0
        if pct >= 10:
            return f"Yesterday was {'more' if delta > 0 else 'less'} expensive than usual."
        return "Yesterday looked close to your usual daily cost."
    return f"{latest.day.strftime('%A')} shows the latest household pattern."


def history_story_comparison_sentence(historical_trends: Optional[HistoricalTrends]) -> str:
    cost_metric = history_story_metric(historical_trends, "cost", "rolling_7_day_average_cost")
    grid_metric = history_story_metric(historical_trends, "grid", "rolling_7_day_average_import")
    solar_metric = history_story_metric(historical_trends, "solar", "rolling_7_day_average_generation")
    parts = []
    if cost_metric and cost_metric.comparison_value and cost_metric.value:
        delta = float(cost_metric.comparison_value) - float(cost_metric.value)
        if abs(delta) >= 0.5:
            parts.append(
                f"Daily cost was {abs(delta / float(cost_metric.value)) * 100:.0f}% {'above' if delta > 0 else 'below'} your recent average."
            )
    if grid_metric and grid_metric.comparison_value and grid_metric.value:
        delta = float(grid_metric.comparison_value) - float(grid_metric.value)
        if abs(delta) >= 0.5:
            parts.append(f"Grid import was {'higher' if delta > 0 else 'lower'} than usual.")
    if solar_metric and solar_metric.comparison_value and solar_metric.value:
        delta = float(solar_metric.comparison_value) - float(solar_metric.value)
        if abs(delta) >= 0.5:
            parts.append(f"Solar generation was {'stronger' if delta > 0 else 'weaker'} than usual.")
    return " ".join(parts) or "Recent household history is available, and the latest day looks close to normal."


def history_story_detail(
    latest: DailyTrend,
    settings: Settings,
    insights: list[HouseholdInsight],
    historical_trends: Optional[HistoricalTrends],
) -> str:
    parts = [history_story_comparison_sentence(historical_trends)]
    if float(latest.solar_generated_kwh or 0.0) > 0:
        parts.append(f"Solar generated {kwh(latest.solar_generated_kwh)} and exported {kwh(latest.solar_exported_kwh)}.")
    if float(latest.ev_charging_kwh or 0.0) > 0:
        parts.append(f"{ev_energy_label(settings)} was {kwh(latest.ev_charging_kwh)}.")
    if insights:
        parts.append(trust_wording(insights[0].headline))
    return " ".join(part for part in parts if part).strip()


def history_story_metrics_html(latest: DailyTrend, settings: Settings) -> str:
    cards = [
        metric("Daily cost", money(latest.net_cost), latest.day.strftime("%A"), "primary"),
        metric("Grid import", kwh(latest.grid_import_kwh), "Electricity bought from the grid", "grid"),
    ]
    if float(latest.solar_generated_kwh or 0.0) > 0:
        cards.append(metric("Solar generation", kwh(latest.solar_generated_kwh), "Rooftop production", "solar"))
    if float(latest.ev_charging_kwh or 0.0) > 0:
        cards.append(metric(ev_energy_label(settings), kwh(latest.ev_charging_kwh), "Latest day", "ev"))
    if float(latest.solar_exported_kwh or 0.0) > 0:
        cards.append(metric("Export", kwh(latest.solar_exported_kwh), "Sent back to the grid", "export"))
    return "".join(cards)


def history_story_section_html(
    trends: list[DailyTrend],
    settings: Settings,
    insights: list[HouseholdInsight],
    historical_trends: Optional[HistoricalTrends] = None,
) -> str:
    latest = trends[-1] if trends else None
    if latest is None:
        return """
        <section class="report-section compact-section history-story-section">
          <div class="section-title"><div><span class="eyebrow">Recent household story</span><h2>No recent household story is ready yet.</h2></div></div>
          <p class="notice">Once PowerMonitor has enough daily history, this page will summarise what happened recently and how it compared with normal.</p>
        </section>
        """
    return f"""
    <section class="report-section compact-section history-story-section">
      <div class="section-title"><div><span class="eyebrow">Recent household story</span><h2>{html.escape(history_story_headline(latest, historical_trends))}</h2></div><p>{html.escape(latest.day.strftime('%A, %d %b %Y'))}</p></div>
      <p class="scenario-note">{html.escape(history_story_detail(latest, settings, insights, historical_trends))}</p>
      <div class="metrics history-story-metrics">{history_story_metrics_html(latest, settings)}</div>
    </section>
    """


def history_comparison_cards(trends: Optional[HistoricalTrends]) -> list[str]:
    specs = [
        ("cost", "rolling_7_day_average_cost", "Daily cost", "7-day average", False, "Shows whether the latest day cost more or less than usual."),
        ("grid", "rolling_7_day_average_import", "Grid import", "7-day average", False, "Shows whether the household relied on the grid more than usual."),
        ("solar", "rolling_7_day_average_generation", "Solar generation", "7-day average", True, "Shows whether rooftop solar did more or less work than usual."),
        ("exports", "rolling_7_day_average_exports", "Solar exports", "7-day average", True, "Shows whether more spare solar was sent to the grid."),
    ]
    cards = []
    for group_name, metric_id, title, comparison_label, higher_is_better, explanation in specs:
        metric_value = history_story_metric(trends, group_name, metric_id)
        if metric_value:
            cards.append(
                historical_trend_card_html(
                    metric_value,
                    title=title,
                    comparison_label=comparison_label,
                    higher_is_better=higher_is_better,
                    explanation=explanation,
                )
            )
    return cards


def history_comparison_summary_html(
    historical_trends: Optional[HistoricalTrends],
    insights: list[HouseholdInsight],
    limit: int = 3,
) -> str:
    cards = history_comparison_cards(historical_trends)[:limit]
    if not cards and not insights:
        return ""
    insight_note = (
        f"<p class=\"scenario-note\">{html.escape(trust_wording(insights[0].explanation))}</p>"
        if insights
        else ""
    )
    return f"""
    <section class="report-section compact-section history-comparison-section">
      <div class="section-title"><div><span class="eyebrow">Compared with usual</span><h2>Was the latest day normal?</h2></div><p>{len(cards)} comparison{'s' if len(cards) != 1 else ''}</p></div>
      <div class="home-historical-trend-grid history-comparison-grid">{''.join(cards)}</div>
      {insight_note}
    </section>
    """


def energy_timeline_home_html(timeline: Optional[EnergyTimeline], limit: int = 6) -> str:
    if not timeline or not timeline.events:
        return """
      <section class="briefing-block energy-timeline-block">
        <div class="section-title"><div><span class="eyebrow">Today so far</span><h2>No energy events detected yet.</h2></div></div>
        <p class="notice">PowerMonitor will show a daily event timeline here once today's readings contain enough activity.</p>
      </section>
        """
    visible = timeline.events[:limit]
    hidden = timeline.events[limit:]
    cards = "".join(energy_timeline_event_card(event) for event in visible)
    hidden_rows = "".join(
        f"<li><time>{html.escape(event.local_time)}</time> {html.escape(event.title)}</li>"
        for event in hidden
    )
    more = (
        f"""
        <details class="energy-timeline-more">
          <summary>Show {len(hidden)} more event{'s' if len(hidden) != 1 else ''}</summary>
          <ul>{hidden_rows}</ul>
        </details>
        """
        if hidden else ""
    )
    progress = "Still in progress" if timeline.is_in_progress else "Complete day"
    return f"""
      <section class="briefing-block energy-timeline-block">
        <div class="section-title"><div><span class="eyebrow">What happened today?</span><h2>Energy timeline</h2></div><p>{html.escape(progress)}</p></div>
        <p class="scenario-note">{html.escape(timeline.summary)}</p>
        <div class="energy-timeline-list">{cards}</div>
        {more}
      </section>
    """


def energy_timeline_history_html(timeline: Optional[EnergyTimeline], limit: int = 8) -> str:
    if not timeline:
        return ""
    events = sorted(
        timeline.events,
        key=lambda event: str(getattr(event, "sort_time", "") or getattr(event, "local_time", "")),
        reverse=True,
    )[:limit]
    rows = "".join(
        f"""
        <article class="home-timeline-row history-preview-row">
          <div class="timeline-stamp"><span aria-hidden="true">{html.escape(str(getattr(event, "icon", "") or "•"))}</span><time>{html.escape(str(getattr(event, "local_time", "")))}</time></div>
          <div>
            <span>{html.escape(str(getattr(event, "confidence", "") or "Current").title())} confidence</span>
            <strong>{html.escape(str(getattr(event, "title", "")))}</strong>
            <p>{html.escape(str(getattr(event, "explanation", "")))}</p>
          </div>
        </article>
        """
        for event in events
    )
    progress = "Still in progress" if timeline.is_in_progress else "Latest completed day"
    return f"""
    <section class="report-section compact-section energy-timeline-history">
      <div class="section-title"><div><span class="eyebrow">Recent activity</span><h2>What happened recently?</h2></div><p>{html.escape(progress)}</p></div>
      <p class="scenario-note">{html.escape(timeline.summary)}</p>
      <div class="home-timeline-list history-timeline-preview">{rows}</div>
    </section>
    """


def energy_timeline_event_card(event) -> str:
    values = "".join(
        f"<small>{html.escape(str(label))}: {html.escape(str(value))}</small>"
        for label, value in (getattr(event, "supporting_values", {}) or {}).items()
    )
    confidence = getattr(event, "confidence", "")
    confidence_html = f"<small>{html.escape(str(confidence).title())} confidence</small>" if confidence else ""
    return f"""
      <article class="energy-timeline-card">
        <time>{html.escape(str(getattr(event, "local_time", "")))}</time>
        <div>
          <h3><span>{html.escape(str(getattr(event, "icon", "") or "•"))}</span>{html.escape(str(getattr(event, "title", "")))}</h3>
          <p>{html.escape(str(getattr(event, "explanation", "")))}</p>
          <div class="briefing-meta">{confidence_html}{values}</div>
        </div>
      </article>
    """


def home_trends_records_html(report: Optional[HouseholdTrendsReport]) -> str:
    trends = list(report.trends[:2]) if report else []
    records = list(report.records[:1]) if report else []
    if not trends and not records:
        return """
      <section class="briefing-block household-trends-block">
        <div class="section-title"><div><span class="eyebrow">Trends and records</span><h2>No long-term change stood out yet.</h2></div></div>
        <p class="notice">PowerMonitor will show trends and personal records here once there is enough comparison history.</p>
      </section>
        """
    trend_cards = []
    for trend in trends:
        change = (
            f"{abs(trend.percentage_change):.0f}% {trend.direction}"
            if trend.percentage_change is not None and trend.direction != "flat"
            else "steady"
        )
        trend_cards.append(
            f"""
            <article class="household-trend-card">
              <span>Trend</span>
              <h3>{html.escape(trend.label)} is {html.escape(change)}.</h3>
              <p>{html.escape(trend.explanation)}</p>
              <div class="briefing-meta">
                <small>Now: {html.escape(trend.current_value)}</small>
                <small>Before: {html.escape(trend.previous_value)}</small>
                <small>{html.escape(trend.confidence)} confidence</small>
              </div>
            </article>
            """
        )
    record_cards = []
    for record in records:
        previous = f"Previous record: {record.previous_record}" if record.previous_record else "First comparable record"
        record_cards.append(
            f"""
            <article class="household-record-card">
              <span>Record</span>
              <h3>{html.escape(record.date)} was your {html.escape(record.label.lower())}.</h3>
              <p>{html.escape(record.explanation)}</p>
              <div class="briefing-meta">
                <small>Value: {html.escape(record.value)}</small>
                <small>{html.escape(previous)}</small>
                <small>{html.escape(record.confidence)} confidence</small>
              </div>
            </article>
            """
        )
    count_label = ", ".join(
        part for part in (
            f"{len(trends)} trend{'s' if len(trends) != 1 else ''}" if trends else "",
            f"{len(records)} record{'s' if len(records) != 1 else ''}" if records else "",
        )
        if part
    )
    return f"""
      <section class="briefing-block household-trends-block">
        <div class="section-title"><div><span class="eyebrow">Trends and records</span><h2>Longer-term household context.</h2></div><p>{html.escape(count_label)}</p></div>
        <div class="briefing-grid">{''.join(trend_cards + record_cards)}</div>
      </section>
    """


def household_insights_html(insights: list[HouseholdInsight]) -> str:
    if not insights:
        return """
      <section class="briefing-block household-insights-block">
        <div class="section-title"><div><span class="eyebrow">Interesting things we noticed</span><h2>No unusual household pattern stood out.</h2></div></div>
        <p class="notice">PowerMonitor will show up to three observations here when recent household history looks meaningfully different from normal.</p>
      </section>
        """
    cards = []
    for insight in insights[:3]:
        values = "".join(
            f"<small>{html.escape(label)}: {html.escape(value)}</small>"
            for label, value in insight.supporting_values.items()
        )
        cards.append(
            f"""
            <article class="household-insight-card">
              <div class="briefing-card-heading">
                <span>{html.escape(insight.confidence)} confidence</span>
                <strong>{html.escape(insight.comparison_period)}</strong>
              </div>
              <h3>{html.escape(insight.headline)}</h3>
              <p>{html.escape(insight.explanation)}</p>
              <div class="briefing-meta">{values}</div>
            </article>
            """
        )
    return f"""
      <section class="briefing-block household-insights-block">
        <div class="section-title"><div><span class="eyebrow">Interesting things we noticed</span><h2>Household observations, not action items.</h2></div><p>Up to three</p></div>
        <div class="briefing-grid">{''.join(cards)}</div>
      </section>
    """


def home_assistant_hero_html(briefing: DailyBriefing) -> str:
    headline = trust_wording(briefing.suggested_next_action)
    if not briefing.top_priorities and not briefing.items_to_watch:
        headline = "Everything looks normal."
    return f"""
      <div class="briefing-hero home-assistant-hero">
        <div>
          <span class="eyebrow">Home</span>
          <h2>{html.escape(headline)}</h2>
          <p>{html.escape(trust_wording(briefing.overall_household_status))}</p>
        </div>
        <div class="briefing-readtime">
          <span>{html.escape(briefing.overall_confidence)} confidence</span>
          <strong>{html.escape(home_attention_summary(briefing))}</strong>
        </div>
      </div>
    """


def home_status_strip_html(briefing: DailyBriefing, glance: Optional[GlanceState], health: Optional[dict] = None) -> str:
    charge_title, charge_body, charge_tone = home_charge_summary(glance)
    spend_title, spend_body, spend_tone = home_spending_summary(briefing)
    health_title, health_body, health_tone = home_health_summary(briefing, glance, health)
    return f"""
      <div class="home-status-grid">
        {home_status_tile_html("Can I charge?", charge_title, charge_body, charge_tone)}
        {home_status_tile_html("Am I spending more than normal?", spend_title, spend_body, spend_tone)}
        {home_status_tile_html("Is everything healthy?", health_title, health_body, health_tone)}
      </div>
    """


def home_status_tile_html(question: str, title: str, body: str, tone: str) -> str:
    return f"""
      <article class="home-status-tile {html.escape(tone)}">
        <span>{html.escape(question)}</span>
        <h3>{html.escape(title)}</h3>
        <p>{html.escape(body)}</p>
      </article>
    """


def home_charge_summary(glance: Optional[GlanceState]) -> tuple[str, str, str]:
    if not glance:
        return "Use tariff timing.", "Live charging guidance is not available on this report.", "amber"
    if glance.status == "CHARGE NOW":
        return "Charge now.", trust_wording(glance.reason), "green"
    if glance.status == "WAIT":
        return "Wait if you can.", trust_wording(glance.reason), "amber"
    if glance.status == "AVOID":
        return "Use a cheaper window.", trust_wording(glance.reason), "amber"
    return "Use tariff timing only.", trust_wording(glance.reason), "amber"


def home_spending_summary(briefing: DailyBriefing) -> tuple[str, str, str]:
    impact = briefing.estimated_savings_opportunity
    value = None
    if impact:
        value = impact.billing_period if impact.billing_period is not None else impact.today
    if value and float(value) > 0:
        return "We found a possible saving.", f"{money(float(value))} may be worth reviewing this billing period.", "amber"
    if str(briefing.overall_confidence).lower() == "low":
        return "Spending needs fresher data.", "PowerMonitor cannot verify enough household data for a confident spending check.", "amber"
    return "Spending looks normal.", "No unusual bill pressure is showing in the current briefing.", "green"


def home_health_summary(briefing: DailyBriefing, glance: Optional[GlanceState], health: Optional[dict] = None) -> tuple[str, str, str]:
    if health and str(health.get("status") or "").lower() not in {"", "ok"}:
        return "Live data needs attention.", collector_status_explanation(health), "amber"
    if glance and glance.status in {"LIVE DATA UNAVAILABLE", "UNABLE TO VERIFY LIVE CONDITIONS"}:
        return "Live data needs attention.", trust_wording(glance.reason), "amber"
    if glance and glance.status == "NO DATA AVAILABLE":
        return "Waiting for household data.", trust_wording(glance.reason), "amber"
    if str(briefing.overall_confidence).lower() == "low":
        return "Confidence is reduced.", "Some readings are missing, so PowerMonitor is being cautious.", "amber"
    return "Everything looks healthy.", "Current household data is good enough for today's guidance.", "green"


def home_attention_summary(briefing: DailyBriefing) -> str:
    count = len(dedupe_briefing_items([*briefing.top_priorities, *briefing.items_to_watch]))
    if count == 0:
        return "Looks normal"
    if count == 1:
        return "One thing to check"
    return "A few things to check"


def home_action_section_html(items: list[BriefingPriority]) -> str:
    if not items:
        return """
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">What should I do?</span><h2>Nothing important needs action.</h2></div></div>
        <p class="notice">Everything looks normal. PowerMonitor will call out a specific action when the data supports one.</p>
      </section>
        """
    cards = "".join(briefing_priority_card_html(item, "Primary recommendation") for item in items[:1])
    return f"""
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">What should I do?</span><h2>{html.escape(home_action_heading(items))}</h2></div></div>
        <div class="briefing-grid">{cards}</div>
      </section>
    """


def home_action_heading(items: list[BriefingPriority]) -> str:
    return "We found one thing worth checking." if len(items) == 1 else "We found a few things worth checking."


def home_positive_section_html(items: list[BriefingPriority]) -> str:
    if not items:
        return """
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">Good signs</span><h2>Everything looks good today.</h2></div></div>
        <p class="notice">No positive signal needs a deep dive. The household picture looks steady.</p>
      </section>
        """
    cards = "".join(briefing_priority_card_html(item, "Good sign") for item in items[:2])
    return f"""
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">Good signs</span><h2>{html.escape(home_positive_heading(items))}</h2></div></div>
        <div class="briefing-grid two-up">{cards}</div>
      </section>
    """


def home_secondary_section_html(items: list[BriefingPriority]) -> str:
    if not items:
        return """
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">Secondary observations</span><h2>Everything looks good today.</h2></div></div>
        <p class="notice">No secondary observation needs attention. PowerMonitor will show up to two when something is worth checking.</p>
      </section>
        """
    cards = "".join(briefing_priority_card_html(item, "Secondary observation") for item in items[:2])
    return f"""
      <section class="briefing-block">
        <div class="section-title"><div><span class="eyebrow">Secondary observations</span><h2>{html.escape(home_secondary_heading(items))}</h2></div><p>Up to two</p></div>
        <div class="briefing-grid two-up">{cards}</div>
      </section>
    """


def home_secondary_heading(items: list[BriefingPriority]) -> str:
    if len(items) == 1:
        return "One other thing is worth knowing."
    return "Two other things are worth knowing."


def home_positive_heading(items: list[BriefingPriority]) -> str:
    if len(items) == 1:
        return positive_signal_text(items[0])
    return "A few household patterns look good today."


def positive_signal_text(item: BriefingPriority) -> str:
    text = f"{item.title} {item.summary}".lower()
    if "solar" in text:
        return "Solar production is tracking normally."
    if "charg" in text or "ev" in text:
        return "Charging behaviour matches your usual pattern."
    return "Everything looks good today."


def home_change_note(items: list[BriefingPriority]) -> tuple[str, str]:
    watch_items = [item for item in items if item.status in {"WATCH", "UNKNOWN", "WAIT"}]
    if not watch_items:
        return "No important changes.", "Nothing new needs attention in this briefing."
    item = watch_items[0]
    return "One thing changed.", trust_wording(item.summary or item.title)


def dedupe_briefing_items(items: list[BriefingPriority]) -> list[BriefingPriority]:
    deduped: list[BriefingPriority] = []
    seen: set[str] = set()
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", f"{item.title} {item.recommendation}".lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def goals_progress_html(goals: list[GoalProgress], limit: Optional[int] = None) -> str:
    if not goals:
        return ""
    visible_goals = goals[:limit] if limit else goals
    cards = []
    for goal in visible_goals:
        progress = f"{goal.progress_pct}%" if goal.progress_pct is not None else "Unknown"
        cards.append(
            f"""
            <article class="briefing-card goal-progress-card {html.escape(goal_status_tone(goal))}">
              <div class="goal-progress-topline">
                <strong>{html.escape(progress)}</strong>
                <span>{html.escape(goal.status.label)}</span>
              </div>
              <div class="briefing-card-heading">
                <span>{html.escape(goal.goal.outcome)}</span>
                <strong>{html.escape(goal.confidence)} confidence</strong>
              </div>
              <h3>{html.escape(trust_wording(goal.goal.title))}</h3>
              <p>{html.escape(trust_wording(goal.summary))}</p>
            </article>
            """
        )
    return f"""
    <section id="home-goals" class="briefing-block">
      <div class="section-title"><div><span class="eyebrow">Today's Progress</span><h2>{len(goals)} active goal{'s' if len(goals) != 1 else ''}</h2></div><p>{'Top priorities only' if limit and len(goals) > limit else 'Current household goals'}</p></div>
      <div class="briefing-grid">{''.join(cards)}</div>
    </section>
    """


def goal_status_tone(goal: GoalProgress) -> str:
    if goal.status.state in {"AHEAD", "ON_TRACK"}:
        return "green"
    if goal.status.state == "NEEDS_ATTENTION":
        return "red"
    return "amber"


def briefing_priority_grid_html(
    items: list[BriefingPriority],
    title: str,
    empty_title: str,
    empty_body: str,
) -> str:
    if not items:
        return f"""
        <section class="briefing-block">
          <div class="section-title"><div><span class="eyebrow">{html.escape(title)}</span><h2>{html.escape(empty_title)}</h2></div></div>
          <p class="notice">{html.escape(empty_body)}</p>
        </section>
        """
    cards = "".join(briefing_priority_card_html(item, title) for item in items)
    return f"""
    <section class="briefing-block">
      <div class="section-title"><div><span class="eyebrow">{html.escape(title)}</span><h2>{len(items)} recommendation{'s' if len(items) != 1 else ''}</h2></div></div>
      <div class="briefing-grid">{cards}</div>
    </section>
    """


def briefing_priority_card_html(item: BriefingPriority, eyebrow: str) -> str:
    support = ""
    if item.supporting_advisors:
        support = f"<small>Why this matters: {html.escape(', '.join(customer_label(source) for source in item.supporting_advisors))}</small>"
    return f"""
    <article class="briefing-card {html.escape(briefing_priority_severity(item.status))}">
      <div class="briefing-card-heading">
        <span>{html.escape(eyebrow)}</span>
        <strong>{html.escape(item.confidence)} confidence</strong>
      </div>
      <h3>{html.escape(trust_wording(item.title))}</h3>
      <p>{html.escape(trust_wording(item.summary))}</p>
      <div class="briefing-meta">
        <small>{html.escape(item.advisor_type.replace("_", " ").title())}</small>
        <small>{html.escape(briefing_priority_impact(item))}</small>
        {support}
      </div>
    </article>
    """


def briefing_priority_severity(status: str) -> str:
    if status in {"HIGH", "AVOID"}:
        return "red"
    if status in {"SLIGHTLY_HIGH", "WAIT", "UNKNOWN"}:
        return "amber"
    return "green"


def briefing_priority_impact(item: BriefingPriority) -> str:
    impact = item.estimated_financial_impact
    if not impact:
        return unable_to_estimate()
    if impact.billing_period is not None:
        return money(impact.billing_period)
    if impact.today is not None:
        return f"{money(impact.today)} today"
    if impact.yearly is not None:
        return f"{money(impact.yearly)} yearly"
    return unable_to_estimate()


def energy_score_html(score: Optional[EnergyScore]) -> str:
    if not score:
        return ""
    positives = "".join(
        f"<li>{html.escape(item)}</li>" for item in score.top_positive_factors
    ) or "<li>No positive factors are available yet.</li>"
    negatives = "".join(
        f"<li>{html.escape(item)}</li>" for item in score.top_negative_factors
    ) or "<li>No major negative factors are active.</li>"
    breakdown = energy_score_breakdown_html(score)
    return f"""
    <article id="energy-score-explanation" class="energy-score-card">
      <div class="energy-score-main">
        <span>Energy Score</span>
        <strong>{score.score}</strong>
        <small>{html.escape(score.label)} confidence: {html.escape(score.confidence)}</small>
      </div>
      <div class="energy-score-detail">
        <h3>{html.escape(score.summary)}</h3>
        <p>Potential score: <strong>{score.potential_score}</strong></p>
        <section class="energy-score-explain">
          <h4>Why is my score what it is?</h4>
          <p>{html.escape(trust_wording(score.explanation.why_this_matters) if score.explanation else "PowerMonitor uses the household information it already has to explain what is helping and what is holding the score back.")}</p>
          {breakdown}
        </section>
        <div class="energy-score-factors">
          <div><span>Positive factors</span><ul>{positives}</ul></div>
          <div><span>Negative factors</span><ul>{negatives}</ul></div>
        </div>
        {decision_explanation_html(score.explanation)}
      </div>
    </article>
    """


def energy_score_breakdown_html(score: EnergyScore) -> str:
    factors = [*score.top_positive_factors, *score.top_negative_factors]
    lower_factors = [item.lower() for item in factors]

    def factor_for(keywords: tuple[str, ...], fallback: str) -> str:
        for original, lowered in zip(factors, lower_factors):
            if any(keyword in lowered for keyword in keywords):
                return trust_wording(original)
        return fallback

    rows = [
        ("Cost", factor_for(("cost", "bill", "tariff", "plan"), "Cost impact is based on the current tariff and bill signals PowerMonitor can verify.")),
        ("Solar", factor_for(("solar", "export", "self-consumption", "self consumption"), "Solar impact depends on production and how much of it is used at home.")),
        ("Peak usage", factor_for(("peak", "high cost"), "Peak usage affects the score when household electricity is used during expensive windows.")),
        ("Overnight usage", factor_for(("overnight", "baseload", "night"), "Overnight usage affects the score when the home draws more power than its usual idle pattern.")),
        ("Household behaviour", factor_for(("behaviour", "behavior", "charging", "ev", "trend"), "Household behaviour reflects recurring charging, usage, and data patterns already visible to PowerMonitor.")),
    ]
    return "<div class=\"energy-score-breakdown\">" + "".join(
        f"<div><span>{html.escape(label)}</span><p>{html.escape(detail)}</p></div>"
        for label, detail in rows
    ) + "</div>"


def setup_progress_card_html(setup: Optional[SetupChecklist]) -> str:
    if not setup:
        return ""
    progress = setup.progress
    if progress.complete:
        return """
        <section class="setup-progress-card complete">
          <div><span>First-time setup</span><h3>PowerMonitor is ready to help.</h3><p>You can add optional services later for more detailed advice.</p></div>
          <a href="#setup">Review settings</a>
        </section>
        """
    remaining = "".join(
        f"<li>{html.escape(task.title)}</li>"
        for task in progress.remaining_tasks[:3]
    )
    fastest = first_time_task_phrase(progress.remaining_tasks[0].title) if progress.remaining_tasks else "add recent readings or import history"
    return f"""
    <section class="setup-progress-card">
      <div>
        <span>First-time setup</span>
        <h3>PowerMonitor can start, but advice will improve with one more step.</h3>
        <p>Can explain now: electricity plan, tariff timing, and bill estimates when data is available.</p>
        <p>Needs next:</p>
        <ul>{remaining}</ul>
        <p>Fastest path to a useful first recommendation: {html.escape(fastest)}.</p>
      </div>
      <a href="#setup">Continue setup</a>
    </section>
    """


def setup_task_html(task: SetupTask) -> str:
    tone = "good" if task.status == "complete" else "neutral" if task.status == "future" else "bad"
    status = task.status.replace("_", " ").title()
    return f"""
    <article class="setup-task {tone}">
      <div class="setup-task-main">
        <span>{html.escape(status)}</span>
        <h3>{html.escape(task.title)}</h3>
        <p>{html.escape(trust_wording(task.rationale))}</p>
      </div>
      <div class="setup-task-meta">
        <div><span>Priority</span><strong>{html.escape(task.priority.value.title())}</strong></div>
        <div><span>Estimated effort</span><strong>{html.escape(task.estimated_effort)}</strong></div>
        <a href="{html.escape(task.action_href)}">{html.escape(task.action_label)}</a>
      </div>
    </article>
    """


def setup_section_html(section: SetupSection) -> str:
    tasks = "".join(setup_task_html(task) for task in section.tasks) or '<p class="notice">No setup tasks for this section.</p>'
    title = setup_section_title(section)
    return f"""
    <section class="report-section setup-section">
      <div class="section-title">
        <div><span class="eyebrow">{html.escape(title)}</span><h2>{html.escape(setup_section_summary(section))}</h2></div>
        <p>{html.escape(setup_section_note(section))}</p>
      </div>
      <div class="setup-task-list">{tasks}</div>
    </section>
    """


def setup_section_title(section: SetupSection) -> str:
    return {
        "household": "Your household",
        "energy_providers": "Connected services",
        "imports": "Imported history",
        "data_quality": "Advice confidence",
    }.get(section.section_id, section.title)


def setup_section_summary(section: SetupSection) -> str:
    if section.completion_pct == 100:
        return "Ready"
    if section.section_id == "data_quality":
        return "Confidence checks"
    return "Still useful"


def setup_section_note(section: SetupSection) -> str:
    if section.completion_pct == 100:
        return "This part is ready."
    return "You can keep using PowerMonitor while this improves."


def setup_integrations_html(provider_management: Optional[ProviderManager]) -> str:
    if not provider_management:
        return ""
    connected = []
    optional = []
    for provider in provider_management.providers():
        data = provider.as_dict()
        onboarding = data.get("onboarding") or {}
        provider_id = str(data.get("provider_id") or provider.provider_id)
        if provider_id == "smart_meter_readings":
            continue
        name = _source_display_name(provider_id, data)
        if _should_show_data_source(provider_id, data, onboarding):
            connected.append(name)
        elif _is_future_optional_source(provider_id, data, onboarding):
            optional.append(name)
        elif provider_id == "home_assistant" and not _provider_is_configured_or_available(data, onboarding):
            optional.append(name)
    connected_rows = "".join(f"<li>{html.escape(name)}</li>" for name in sorted(set(connected))) or "<li>No integrations connected yet.</li>"
    optional_rows = "".join(f"<li>{html.escape(name)}</li>" for name in sorted(set(optional))) or "<li>No optional integrations listed yet.</li>"
    return f"""
    <section class="report-section compact-section">
      <div class="section-title">
        <div><span class="eyebrow">Connected services</span><h2>Connect only what your household uses</h2></div>
        <p>PowerMonitor can work with imports, live services, and local setup choices.</p>
      </div>
      <div class="provider-guidance-grid integration-summary-grid">
        <div><span>Connected</span><ul>{connected_rows}</ul></div>
        <div><span>Optional</span><ul>{optional_rows}</ul></div>
      </div>
    </section>
    """


def setup_page_html(
    setup: Optional[SetupChecklist],
    settings: Optional[Settings] = None,
    promotion_summary: Optional[dict] = None,
    notification_preferences: Optional[dict] = None,
    provider_management: Optional[ProviderManager] = None,
    tariff_updates: Optional[dict] = None,
    tariff_update_flash: Optional[dict] = None,
) -> str:
    if not setup:
        return '<section class="report-section"><p class="notice">Setup guidance is unavailable.</p></section>'
    sections = "".join(setup_section_html(section) for section in setup.sections)
    conflict = (promotion_summary or {}).get("conflict_summary", {})
    conflict_notice = ""
    if conflict.get("repeated_conflicts"):
        conflict_notice = """
        <section class="setup-progress-card">
        <div><span>Data connection</span><h3>Two sources disagree.</h3><p>Review provider configuration before relying on this data.</p></div>
          <a href="#system">Review Settings</a>
        </section>
        """
    notification_notice = ""
    if notification_preferences and not notification_preferences.get("enabled"):
        notification_notice = """
        <section class="setup-progress-card">
          <div><span>Optional</span><h3>Notifications are disabled.</h3><p>Notification readiness will still be evaluated locally, but no notification type is enabled.</p></div>
          <a href="#system">Review Notifications</a>
        </section>
        """
    return f"""
    {conflict_notice}
    {notification_notice}
    {first_time_intro_html(setup)}
      {billing_cycle_setup_html(settings)}
      {tariff_updates_html(tariff_updates, tariff_update_flash)}
      {household_timeline_html(settings)}
      {first_time_finish_html(setup, settings)}
    {setup_integrations_html(provider_management)}
    <section class="report-section compact-section">
      <div class="section-title">
        <div><span class="eyebrow">Setup</span><h2>{setup.progress.completion_pct}% complete</h2></div>
        <p>{first_time_progress_summary(setup)}</p>
      </div>
      <div class="setup-meter"><span style="width:{setup.progress.completion_pct}%"></span></div>
    </section>
    {sections}
    """


def household_timeline_html(settings: Optional[Settings]) -> str:
    if not settings:
        return ""
    try:
        timeline = household_timeline(settings)
    except (HouseholdTimelineError, ValueError, OSError):
        return ""
    current_day = local_today()
    events = sorted(
        [event for event in timeline.events if event.active],
        key=lambda item: (item.effective_to is None and item.event_type == "tariff_period", item.effective_from),
        reverse=True,
    )
    rows = "".join(household_timeline_event_html(event, timeline, settings) for event in events) or '<p class="notice">No household history events have been added yet.</p>'
    gaps = timeline.tariff_gaps()
    gap_notice = ""
    if gaps:
        gap_rows = "".join(
            f"<li>{html.escape(household_timeline_gap_label(start, end))}</li>"
            for start, end in gaps
        )
        gap_notice = f"""
        <div class="provider-action-result warning visible">
          <span>Timeline gap</span>
          <strong>Some historical tariff dates are unknown.</strong>
          <small>PowerMonitor will use fallback rates for affected analysis and lower confidence.</small>
          <ul>{gap_rows}</ul>
        </div>
        """
    active_tariff = next((event for event in timeline.tariff_periods() if event.effective_to is None), None)
    suggested_previous_end = active_tariff.effective_from - timedelta(days=1) if active_tariff else None
    suggested_text = ""
    suggested_end_value = ""
    if active_tariff and suggested_previous_end:
        suggested_text = (
            f"<p class=\"timeline-helper\">Current tariff starts {html.escape(display_date(active_tariff.effective_from))}. "
            f"Suggested previous tariff end: {html.escape(display_date(suggested_previous_end))}.</p>"
        )
        suggested_end_value = suggested_previous_end.isoformat()
    confirm_timeline = ""
    if active_tariff and active_tariff.source.startswith("inferred"):
        confirm_timeline = f"""
        <form class="timeline-confirm-form" method="post" action="/household-timeline/confirm">
          <input type="hidden" name="event_id" value="{html.escape(active_tariff.event_id)}">
          <button type="submit">Confirm timeline</button>
        </form>
        """
    return f"""
    <section id="household-timeline" class="report-section setup-section household-timeline-section">
      <div class="section-title">
        <div><span class="eyebrow">Settings</span><h2>Home energy history</h2></div>
        <p>Tell PowerMonitor when plans or household conditions changed so historical analysis uses the right context.</p>
      </div>
      {gap_notice}
      {confirm_timeline}
      <div class="household-timeline-list">{rows}</div>
      <details class="battery-calculation-details household-timeline-editor">
        <summary>Add previous tariff</summary>
        {suggested_text}
        <form class="setup-config-form household-timeline-form" method="post" action="/household-timeline/add-tariff">
          <label><span>Provider</span><input name="provider" value="{html.escape(str(settings.retailer or 'Synergy'))}"></label>
          <label><span>Tariff plan</span><select name="plan_id">{household_timeline_plan_options()}</select></label>
          <label><span>Effective from</span><input type="date" name="effective_from" required></label>
          <label><span>Effective to</span><input type="date" name="effective_to" value="{html.escape(suggested_end_value)}"></label>
          <label><span>Manual tariff name</span><input name="manual_tariff_name" placeholder="Only for manual tariffs"></label>
          <label><span>Daily supply charge</span><input type="number" step="0.000001" min="0" name="supply_charge_per_day" value="{settings.supply_charge_per_day:.6f}"></label>
          <label><span>Export credit</span><input type="number" step="0.000001" min="0" name="export_credit_rate" value="{settings.export_credit_rate:.6f}"></label>
          <label><span>Flat/import rate</span><input type="number" step="0.000001" min="0" name="flat_import_rate" value="{settings.import_rates.get('peak', 0.0):.6f}"></label>
          <label class="timeline-notes"><span>Notes</span><input name="notes" placeholder="Optional"></label>
          <button type="submit">Add previous tariff</button>
        </form>
      </details>
      <details class="battery-calculation-details">
        <summary>Add household event</summary>
        <form class="setup-config-form household-timeline-form" method="post" action="/household-timeline/add-event">
          <label><span>Event type</span><select name="event_type">
            <option value="solar_installation">Solar installation</option>
            <option value="battery_installation">Battery installation</option>
            <option value="ev_added">EV added</option>
            <option value="ev_removed">EV removed</option>
            <option value="ev_charger_installed">EV charger installed</option>
            <option value="major_appliance_change">Major appliance change</option>
            <option value="occupancy_change">Occupancy change</option>
            <option value="custom_note">Custom note</option>
          </select></label>
          <label><span>Title</span><input name="title" required></label>
          <label><span>Effective from</span><input type="date" name="effective_from" required></label>
          <label><span>Effective to</span><input type="date" name="effective_to"></label>
          <label class="timeline-notes"><span>Notes</span><input name="notes"></label>
          <button type="submit">Add household event</button>
        </form>
      </details>
      <details class="battery-calculation-details">
        <summary>View diagnostics</summary>
        <pre>{html.escape(json.dumps(timeline.public_dict(today=local_today()), indent=2, sort_keys=True))}</pre>
      </details>
    </section>
    """


def household_timeline_event_html(event: HouseholdTimelineEvent, timeline: HouseholdTimeline, settings: Settings) -> str:
    current = event.event_type == "tariff_period" and event.effective_to is None
    status = "Current tariff" if current else "Previous tariff" if event.event_type == "tariff_period" else event.event_type.replace("_", " ").title()
    date_text = (
        f"From {display_date(event.effective_from)}"
        if event.effective_to is None
        else f"{display_date(event.effective_from)} to {display_date(event.effective_to)}"
    )
    metadata = event.metadata.get("tariff") if isinstance(event.metadata, dict) else {}
    diagnostics = ""
    source_label = "Current settings" if event.source.startswith("inferred") else event.source.replace("_", " ").title()
    if isinstance(metadata, dict):
        diagnostics = f"""
        <details class="timeline-diagnostics">
          <summary>Diagnostics</summary>
          <pre>{html.escape(json.dumps(event.public_dict(), indent=2, sort_keys=True))}</pre>
        </details>
        """
    disabled_confirm = "Disable this tariff period? PowerMonitor may use fallback rates for the affected historical dates."
    delete_confirm = "Deleting this tariff period may cause PowerMonitor to use fallback rates for the affected dates."
    edit_label = "Edit details" if current else "Edit previous tariff" if event.event_type == "tariff_period" else "Edit"
    edit_form = household_timeline_edit_form(event, settings, current=current)
    change_form = ""
    if current:
        tomorrow = local_today() + timedelta(days=1)
        change_form = f"""
        <details class="timeline-action-panel">
          <summary>Change tariff</summary>
          <p class="timeline-review">Choose the new tariff and start date. PowerMonitor will end {html.escape(event.title)} on the previous local calendar day and create one new current tariff.</p>
          <form class="setup-config-form household-timeline-form" method="post" action="/household-timeline/replace-current">
            <input type="hidden" name="event_id" value="{html.escape(event.event_id)}">
            <label><span>New tariff</span><select name="plan_id">{household_timeline_plan_options()}</select></label>
            <label><span>New tariff starts</span><input type="date" name="effective_from" value="{tomorrow.isoformat()}" required></label>
            <label><span>Provider</span><input name="provider" value="{html.escape(str(settings.retailer or 'Synergy'))}"></label>
            <label><span>Manual tariff name</span><input name="manual_tariff_name" placeholder="Only for manual tariffs"></label>
            <label><span>Daily supply charge</span><input type="number" step="0.000001" min="0" name="supply_charge_per_day" value="{settings.supply_charge_per_day:.6f}"></label>
            <label><span>Export credit</span><input type="number" step="0.000001" min="0" name="export_credit_rate" value="{settings.export_credit_rate:.6f}"></label>
            <label><span>Flat/import rate</span><input type="number" step="0.000001" min="0" name="flat_import_rate" value="{settings.import_rates.get('peak', 0.0):.6f}"></label>
            <label class="timeline-notes"><span>Notes</span><input name="notes" placeholder="Optional"></label>
            <div class="timeline-review">
              <strong>Review before saving</strong>
              <span>You are changing from {html.escape(event.title)}. The existing tariff will close the day before the new tariff starts. Historical calculations from that date will use the new tariff snapshot.</span>
            </div>
            <button type="submit" data-confirm="Change tariff? PowerMonitor will close the current tariff period and create a new current tariff. Historical calculations from the change date will use the new tariff.">Change tariff</button>
          </form>
        </details>
        """
    card_class = "timeline-entry compact household-timeline-event"
    if current:
        card_class += " current-tariff-card"
    elif event.event_type == "tariff_period":
        card_class += " previous-tariff-card"
    return f"""
    <article class="{card_class}">
      <div class="timeline-stamp"><span>{html.escape(status[:1])}</span><time>{html.escape('Current' if current else 'Previous' if event.event_type == 'tariff_period' else status)}</time></div>
      <div class="timeline-card-body">
        <span>{html.escape(status)}</span>
        <h3>{html.escape(event.title)}</h3>
        <p>{html.escape(date_text)}</p>
        {f'<p class="timeline-note">{html.escape(event.notes)}</p>' if event.notes and not event.source.startswith("inferred") else ''}
        <small class="timeline-source">{html.escape(source_label)}</small>
        <div class="household-timeline-actions">
          <details class="timeline-action-panel">
            <summary>{html.escape(edit_label)}</summary>
            {edit_form}
          </details>
          {change_form}
          <form method="post" action="/household-timeline/disable">
            <input type="hidden" name="event_id" value="{html.escape(event.event_id)}">
            <button type="submit" data-confirm="{html.escape(disabled_confirm)}">Disable</button>
          </form>
          <form method="post" action="/household-timeline/delete">
            <input type="hidden" name="event_id" value="{html.escape(event.event_id)}">
            <button type="submit" data-confirm="{html.escape(delete_confirm)}">Delete</button>
          </form>
        </div>
        {diagnostics}
      </div>
    </article>
    """


def household_timeline_edit_form(event: HouseholdTimelineEvent, settings: Settings, *, current: bool) -> str:
    metadata = event.metadata.get("tariff") if isinstance(event.metadata, dict) else {}
    manual = isinstance(metadata, dict) and str(metadata.get("source_identifier") or "").startswith("manual")
    note_value = "Created from current tariff settings." if event.source.startswith("inferred") else event.notes or ""
    plan_fields = ""
    if event.event_type == "tariff_period" and not current:
        plan_fields = f"""
          <label><span>Tariff plan</span><select name="plan_id">{household_timeline_plan_options(event.title)}</select></label>
          <label><span>Provider</span><input name="provider" value="{html.escape(str(metadata.get('provider') if isinstance(metadata, dict) else settings.retailer or 'Synergy'))}"></label>
          <label><span>Manual tariff name</span><input name="manual_tariff_name" value="{html.escape(event.title if manual else '')}" placeholder="Only for manual tariffs"></label>
          <label><span>Daily supply charge</span><input type="number" step="0.000001" min="0" name="supply_charge_per_day" value="{float(metadata.get('supply_charge_per_day', settings.supply_charge_per_day)) if isinstance(metadata, dict) else settings.supply_charge_per_day:.6f}"></label>
          <label><span>Export credit</span><input type="number" step="0.000001" min="0" name="export_credit_rate" value="{float((metadata.get('export_rates') or {}).get('standard', settings.export_credit_rate)) if isinstance(metadata, dict) else settings.export_credit_rate:.6f}"></label>
          <label><span>Flat/import rate</span><input type="number" step="0.000001" min="0" name="flat_import_rate" value="{float((metadata.get('usage_rates') or {}).get('peak', settings.import_rates.get('peak', 0.0))) if isinstance(metadata, dict) else settings.import_rates.get('peak', 0.0):.6f}"></label>
        """
    end_field = "" if current else f'<label><span>Effective to</span><input type="date" name="effective_to" value="{event.effective_to.isoformat() if event.effective_to else ""}" required></label>'
    confidence_selected = lambda value: " selected" if event.confidence == value else ""
    return f"""
        <form class="setup-config-form household-timeline-form" method="post" action="/household-timeline/edit">
          <input type="hidden" name="event_id" value="{html.escape(event.event_id)}">
          <input type="hidden" name="current_tariff" value="{'1' if current else '0'}">
          {plan_fields}
          <label><span>Effective from</span><input type="date" name="effective_from" value="{event.effective_from.isoformat()}" required></label>
          {end_field}
          <label><span>Confidence</span><select name="confidence">
            <option value="high"{confidence_selected("high")}>High</option>
            <option value="medium"{confidence_selected("medium")}>Medium</option>
            <option value="low"{confidence_selected("low")}>Low</option>
          </select></label>
          <label class="timeline-notes"><span>Notes</span><input name="notes" value="{html.escape(note_value)}"></label>
          <button type="submit" data-confirm="Update this tariff period? PowerMonitor will validate the dates before saving.">Save changes</button>
        </form>
    """


def household_timeline_plan_options(selected_label: Optional[str] = None) -> str:
    options = []
    for plan_id, plan in SUPPORTED_PLANS.items():
        selected = " selected" if selected_label and plan.label == selected_label else ""
        options.append(f'<option value="{html.escape(plan_id)}"{selected}>{html.escape(plan.label)}</option>')
    options.append(f'<option value="{MANUAL_TARIFF_ID}">Manual historical tariff</option>')
    return "".join(options)


def household_timeline_gap_label(start: date, end: Optional[date]) -> str:
    if start == date.min:
        return f"Before {display_date(end)}"
    if end is None:
        return f"From {display_date(start)}"
    return f"{display_date(start)} to {display_date(end)}"


def display_date(value: Optional[date]) -> str:
    if value is None:
        return "current"
    return value.strftime("%-d %B %Y") if hasattr(value, "strftime") else str(value)


def first_time_intro_html(setup: SetupChecklist) -> str:
    return f"""
    <section class="report-section compact-section first-time-intro">
      <div class="section-title"><div><span class="eyebrow">First-time setup</span><h2>Start with the few answers PowerMonitor needs.</h2></div><p>{html.escape(first_time_progress_summary(setup))}</p></div>
      <div class="onboarding-steps first-time-path">
        <div><span>1</span><strong>Location</strong><p>Country and state decide which retailers and plans are shown.</p></div>
        <div><span>2</span><strong>Electricity plan</strong><p>Choose your retailer and plan, or use manual tariff rates.</p></div>
        <div><span>3</span><strong>Optional services</strong><p>Add solar, EV charging, or battery history now, or connect them later.</p></div>
      </div>
    </section>
    """


def first_time_progress_summary(setup: SetupChecklist) -> str:
    if setup.progress.complete:
        return "PowerMonitor has enough setup to start explaining your household."
    remaining = [task.title for task in setup.progress.remaining_tasks[:2]]
    if not remaining:
        return "PowerMonitor can start now. Optional details can improve advice later."
    return f"Next: {', '.join(remaining)}."


def billing_cycle_setup_html(settings: Optional[Settings]) -> str:
    if not settings:
        return ""
    state_or_territory = getattr(settings, "state_or_territory", DEFAULT_STATE_OR_TERRITORY)
    retailer_id = getattr(settings, "retailer_id", DEFAULT_RETAILER)
    current_plan = getattr(settings, "current_plan", DEFAULT_PLAN)
    supply_charge = getattr(settings, "supply_charge_per_day", DEFAULT_SETTINGS["supply_charge_per_day"])
    export_credit_rate = getattr(settings, "export_credit_rate", DEFAULT_SETTINGS["export_credit_rate"])
    country_options = '<option value="AU" selected>Australia</option>'
    state_options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if state_or_territory == value else ""}>{html.escape(label)}</option>'
        for value, label in STATE_OPTIONS
    )
    retailer_options = "".join(
        [
            f'<option value="{DEFAULT_RETAILER}"{" selected" if retailer_id == DEFAULT_RETAILER or getattr(settings, "retailer", "") == "Synergy" else ""}>Synergy</option>',
            f'<option value="{MANUAL_TARIFF_ID}"{" selected" if current_plan == MANUAL_TARIFF_ID else ""}>{MANUAL_TARIFF_LABEL}</option>',
        ]
    )
    plan_options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if current_plan == value else ""}>{html.escape(label)}</option>'
        for value, label in supported_plan_options()
    ) + f'<option value="{MANUAL_TARIFF_ID}"{" selected" if current_plan == MANUAL_TARIFF_ID else ""}>{MANUAL_TARIFF_LABEL}</option>'
    rates = getattr(settings, "import_rates", DEFAULT_SETTINGS["import_rates"])
    solar_choice = getattr(settings, "solar_setup_choice", "none")
    ev_choice = getattr(settings, "ev_setup_choice", "none")
    battery_choice = getattr(settings, "battery_setup_choice", "none")
    battery_costs = getattr(settings, "battery_installed_costs", DEFAULT_SETTINGS["battery_installed_costs"])
    battery_efficiency = float(getattr(settings, "battery_round_trip_efficiency", 0.90) or 0.90)
    battery_efficiency_pct = battery_efficiency * 100 if battery_efficiency <= 1.0 else battery_efficiency
    battery_life_years = int(getattr(settings, "battery_life_years", 15) or 15)
    battery_degradation = float(getattr(settings, "battery_annual_degradation_pct", 2.0) or 0.0)
    battery_inflation = float(getattr(settings, "battery_electricity_inflation_pct", 3.0) or 0.0)
    battery_discount = float(getattr(settings, "battery_discount_rate_pct", 0.0) or 0.0)
    battery_rebate = float(getattr(settings, "battery_government_rebate", 0.0) or 0.0)
    previews = tariff_catalog_previews_html()
    return f"""
    <section id="setup-billing" class="report-section setup-section">
      <div class="section-title">
        <div><span class="eyebrow">First-time setup</span><h2>Tell PowerMonitor about your household</h2></div>
        <p>Answer what you know now. Anything optional can be added later.</p>
      </div>
      <form class="setup-config-form" method="post" action="/setup/billing-cycle">
        <div class="first-time-question">
          <span>1</span>
          <div>
            <h3>Where do you live?</h3>
            <div class="setup-form-row">
              <label><span>Country</span><select name="country">{country_options}</select></label>
              <label><span>State / territory</span><select name="state_or_territory">{state_options}</select></label>
            </div>
          </div>
        </div>
        <div class="first-time-question">
          <span>2</span>
          <div>
            <h3>Who is your electricity retailer?</h3>
            <label><span>Retailer</span><select name="retailer">{retailer_options}</select></label>
            <p>Choose Manual tariff if your retailer is not listed yet.</p>
          </div>
        </div>
        <div class="first-time-question">
          <span>3</span>
          <div>
            <h3>What electricity plan are you on?</h3>
            <label id="setup-tariff"><span>Electricity plan</span><select name="current_plan">{plan_options}</select></label>
          </div>
        </div>
        <div class="first-time-question">
          <span>4</span>
          <div>
            <h3>When does your bill usually start?</h3>
            <label id="setup-billing-cycle"><span>Billing cycle start day</span><input type="number" name="billing_period_start_day" min="1" max="28" value="{settings.billing_period_start_day}" required></label>
          </div>
        </div>
        <div class="first-time-question">
          <span>5</span>
          <div>
            <h3>Do you have solar?</h3>
            {setup_choice_select("solar_setup_choice", solar_choice, (("none", "No"), ("solaredge", "SolarEdge"), ("home_assistant", "Home Assistant"), ("manual_later", "Other / manual later")))}
          </div>
        </div>
        <div class="first-time-question">
          <span>6</span>
          <div>
            <h3>Do you have an EV?</h3>
            {setup_choice_select("ev_setup_choice", ev_choice, (("none", "No"), ("ocular_import", "Ocular import"), ("home_assistant", "Home Assistant"), ("manual_later", "Other / manual later")))}
          </div>
        </div>
        <div class="first-time-question">
          <span>7</span>
          <div>
            <h3>Do you have a battery?</h3>
            {setup_choice_select("battery_setup_choice", battery_choice, (("none", "No"), ("home_assistant", "Home Assistant"), ("manual_later", "Other / manual later")))}
          </div>
        </div>
        <div class="catalogue-tariff-previews">
          {previews}
        </div>
        <fieldset class="manual-tariff-fields">
          <legend>Battery investment assumptions</legend>
          <p class="notice">Used only for battery purchase projections. Battery dispatch still uses the existing simulation.</p>
          <label><span>5 kWh installed cost</span><input type="number" step="1" min="0" name="battery_installed_cost_5" value="{float(battery_costs.get("5", battery_costs.get(5, 6500.0))):.0f}"></label>
          <label><span>10 kWh installed cost</span><input type="number" step="1" min="0" name="battery_installed_cost_10" value="{float(battery_costs.get("10", battery_costs.get(10, 9500.0))):.0f}"></label>
          <label><span>15 kWh installed cost</span><input type="number" step="1" min="0" name="battery_installed_cost_15" value="{float(battery_costs.get("15", battery_costs.get(15, 12500.0))):.0f}"></label>
          <label><span>20 kWh installed cost</span><input type="number" step="1" min="0" name="battery_installed_cost_20" value="{float(battery_costs.get("20", battery_costs.get(20, 15500.0))):.0f}"></label>
          <label><span>Round-trip efficiency %</span><input type="number" step="0.1" min="0" max="100" name="battery_round_trip_efficiency_pct" value="{battery_efficiency_pct:.1f}"></label>
          <label><span>Battery life years</span><input type="number" step="1" min="1" name="battery_life_years" value="{battery_life_years}"></label>
          <label><span>Annual degradation %</span><input type="number" step="0.1" min="0" name="battery_annual_degradation_pct" value="{battery_degradation:.1f}"></label>
          <label><span>Electricity inflation %</span><input type="number" step="0.1" name="battery_electricity_inflation_pct" value="{battery_inflation:.1f}"></label>
          <label><span>Discount rate %</span><input type="number" step="0.1" min="0" name="battery_discount_rate_pct" value="{battery_discount:.1f}"></label>
          <label><span>Government rebate</span><input type="number" step="1" min="0" name="battery_government_rebate" value="{battery_rebate:.0f}"></label>
        </fieldset>
        <fieldset class="manual-tariff-fields">
          <legend>Manual tariff</legend>
          <p class="notice">If your retailer or plan is not listed, choose Manual tariff.</p>
          <label><span>Daily supply charge</span><input type="number" step="0.000001" min="0" name="manual_supply_charge_per_day" value="{supply_charge:.6f}"></label>
          <label><span>Export credit rate</span><input type="number" step="0.000001" min="0" name="manual_export_credit_rate" value="{export_credit_rate:.6f}"></label>
          <label><span>Super off peak import rate</span><input type="number" step="0.000001" min="0" name="manual_import_rate_super_off_peak" value="{rates.get("super_off_peak", 0):.6f}"></label>
          <label><span>Overnight import rate</span><input type="number" step="0.000001" min="0" name="manual_import_rate_overnight" value="{rates.get("overnight", 0):.6f}"></label>
          <label><span>Off peak import rate</span><input type="number" step="0.000001" min="0" name="manual_import_rate_off_peak" value="{rates.get("off_peak", 0):.6f}"></label>
          <label><span>Peak import rate</span><input type="number" step="0.000001" min="0" name="manual_import_rate_peak" value="{rates.get("peak", 0):.6f}"></label>
        </fieldset>
        <button type="submit">Finish setup</button>
      </form>
      <p class="notice">Saving this gives PowerMonitor enough local setup to explain bills and tariff timing. Optional services can be connected later.</p>
    </section>
    """


def tariff_updates_html(tariff_updates: Optional[dict], tariff_update_flash: Optional[dict] = None) -> str:
    if tariff_updates is None:
        return ""
    current = tariff_updates.get("current_active_tariff") or {}
    pending = tariff_updates.get("pending") or []
    diagnostics_by_provider = tariff_updates.get("diagnostics") or {}
    pending_html = "".join(tariff_update_card_html(update) for update in pending)
    diagnostics_html = "".join(
        tariff_update_diagnostics_html(provider, diagnostics, current, pending[0] if pending else None)
        for provider, diagnostics in diagnostics_by_provider.items()
    ) or '<p class="scenario-note">No tariff import checks have run yet.</p>'
    last_checked = _tariff_last_checked(diagnostics_by_provider)
    current_status = "Update ready for review" if pending else "Current"
    no_update = not pending
    flash_html = tariff_update_flash_html(tariff_update_flash)
    body = (
        tariff_no_update_state_html(current, last_checked, current_status)
        if no_update else
        pending_html
    )
    return f"""
    <section id="tariff-updates" class="report-section setup-section tariff-updates-section">
      <div class="section-title">
        <div><span class="eyebrow">Settings</span><h2>Tariff updates</h2></div>
        <p>Review retailer tariff changes before they affect new calculations.</p>
      </div>
      {flash_html}
      {body}
      <details class="battery-calculation-details tariff-diagnostics-disclosure">
        <summary>View diagnostics</summary>
        {diagnostics_html}
      </details>
    </section>
    """


def tariff_update_card_html(update: dict) -> str:
    pending_version = update.get("pending_version") or {}
    current_version = update.get("current_version") or {}
    preview = update.get("financial_preview") or {}
    update_id = html.escape(str(update.get("update_id") or ""))
    impact = tariff_impact_summary(preview, current_version, pending_version)
    comparison_rows = tariff_changed_fields_html(update, current_version, pending_version)
    raw_rows = "".join(
        f"""
        <tr class="{html.escape(tariff_change_tone(change))}">
          <td>{html.escape(str(change.get("label") or ""))}</td>
          <td>{html.escape(str(change.get("before") or ""))}</td>
          <td>{html.escape(str(change.get("after") or ""))}</td>
          <td>{html.escape(tariff_change_text(change))}</td>
        </tr>
        """
        for change in update.get("diff") or []
    ) or '<tr><td colspan="4">Checksum changed with no rate-level difference.</td></tr>'
    return f"""
    <article class="system-card tariff-update-card tariff-update-review">
      <div class="tariff-update-hero">
        <div>
          <span>Update detected</span>
          <h3>{html.escape(str(update.get("provider") or pending_version.get("provider") or "Provider"))} has published updated rates for your plan.</h3>
          <p>{html.escape(impact["summary_sentence"])}</p>
        </div>
        <strong>{html.escape(impact["annual_label"])}</strong>
      </div>
      <div class="tariff-update-summary-list">
        <div><span>Provider</span><strong>{html.escape(str(update.get("provider") or pending_version.get("provider") or "Unknown"))}</strong></div>
        <div><span>Tariff name</span><strong>{html.escape(str(pending_version.get("tariff_name") or update.get("tariff_name") or "Unknown"))}</strong></div>
        <div><span>Date detected</span><strong>{html.escape(display_timestamp(update.get("date_detected")))}</strong></div>
        <div><span>Effective date</span><strong>{html.escape(str(update.get("effective_date") or pending_version.get("effective_from") or "Unknown"))}</strong></div>
        <div><span>Current version</span><strong>{html.escape(str(current_version.get("version") or "Unknown"))}</strong></div>
        <div><span>Proposed version</span><strong>{html.escape(str(pending_version.get("version") or "Unknown"))}</strong></div>
      </div>
      <div class="tariff-impact-card">
        <div><span>Estimated annual bill change</span><strong>{html.escape(impact["annual_label"])}</strong></div>
        <div><span>Estimated monthly bill change</span><strong>{html.escape(impact["monthly_label"])}</strong></div>
        <div><span>Current plan</span><strong>{html.escape(impact["plan_label"])}</strong></div>
        <div><span>Battery recommendation</span><strong>{html.escape(impact["battery_label"])}</strong></div>
        <div><span>EV charging guidance</span><strong>{html.escape(impact["ev_label"])}</strong></div>
        <div><span>Confidence</span><strong>{html.escape(impact["confidence"])}</strong></div>
        <div><span>Usage period used</span><strong>{html.escape(impact["basis"])}</strong></div>
      </div>
      <div class="tariff-comparison-list">
        <div class="tariff-comparison-header"><span>Review changes</span><strong>Changed fields only</strong></div>
        {comparison_rows}
      </div>
      <div class="tariff-decision-actions">
        <form method="post" action="/tariff-updates/approve">
          <input type="hidden" name="update_id" value="{update_id}">
          <p>New calculations from the effective date will use the approved version. Historical periods will keep the tariff that applied at the time. Approval does not rewrite previous reports.</p>
          <button type="submit" data-confirm="Approve this tariff update? New calculations from the effective date will use the approved version. Historical reports will not be rewritten.">Approve tariff update</button>
        </form>
        <form method="post" action="/tariff-updates/dismiss">
          <input type="hidden" name="update_id" value="{update_id}">
          <p>The pending version will be archived. Your current active tariff remains unchanged.</p>
          <button type="submit" data-confirm="Dismiss this tariff update? The pending version will be archived and the current active tariff will remain unchanged.">Dismiss update</button>
        </form>
      </div>
      <details class="evidence-trail tariff-raw-changes"><summary>View raw changes</summary><div class="tariff-raw-table"><table class="responsive-table"><thead><tr><th>Item</th><th>Current</th><th>New</th><th>Change</th></tr></thead><tbody>{raw_rows}</tbody></table></div><pre>{html.escape(json.dumps(update, indent=2, sort_keys=True))}</pre></details>
    </article>
    """


def tariff_update_diagnostics_html(provider: str, diagnostics: dict, current: dict, pending_update: Optional[dict]) -> str:
    pending = (pending_update or {}).get("pending_version") or {}
    groups = [
        ("Provider check", [("Provider", diagnostics.get("provider") or provider), ("Current status", diagnostics.get("import_status"))]),
        ("Active version", [("Tariff", current.get("tariff_name")), ("Version", current.get("version")), ("Effective date", current.get("effective_from"))]),
        ("Pending version", [("Tariff", pending.get("tariff_name") or "None pending"), ("Version", pending.get("version")), ("Effective date", pending.get("effective_from"))]),
        ("Source and retrieval", [("Source URL", diagnostics.get("source_url")), ("Current checksum", diagnostics.get("current_checksum")), ("Pending checksum", diagnostics.get("pending_checksum"))]),
        ("Last result", [("Last successful tariff check", diagnostics.get("last_successful_check")), ("Last error", diagnostics.get("last_error") or "None")]),
        ("Next scheduled check", [("Next scheduled check", diagnostics.get("next_scheduled_check"))]),
    ]
    group_html = "".join(
        f"""
        <div class="tariff-diagnostics-group">
          <span>{html.escape(title)}</span>
          <div class="config-grid">{"".join(f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or 'n/a'))}</strong></div>" for label, value in rows)}</div>
        </div>
        """
        for title, rows in groups
    )
    return f"""
    <div class="tariff-diagnostics">{group_html}</div>
    """


def tariff_no_update_state_html(current: dict, last_checked: str, current_status: str) -> str:
    provider = str(current.get("provider") or "Your retailer")
    tariff_name = str(current.get("tariff_name") or "Current tariff")
    return f"""
    <div class="system-card tariff-current-card">
      <div class="chart-heading"><h3>{html.escape(tariff_name)} is current.</h3><span>Current active tariff</span></div>
      <p>Your {html.escape(provider)} {html.escape(tariff_name)} tariff is current. PowerMonitor will keep historical versions separate so past periods continue to use the tariff that applied at the time.</p>
      <div class="tariff-update-summary-list">
        <div><span>Current tariff name</span><strong>{html.escape(tariff_name)}</strong></div>
        <div><span>Provider</span><strong>{html.escape(provider)}</strong></div>
        <div><span>Effective date</span><strong>{html.escape(str(current.get("effective_from") or "Unknown"))}</strong></div>
        <div><span>Last checked</span><strong>{html.escape(last_checked or "Not checked yet")}</strong></div>
        <div><span>Current status</span><strong>{html.escape(current_status)}</strong></div>
      </div>
      <form method="post" action="/tariff-updates/check" class="inline-action-form tariff-primary-action">
        <button type="submit">Check for tariff updates</button>
      </form>
    </div>
    """


def tariff_update_flash_html(flash: Optional[dict]) -> str:
    if not flash:
        return ""
    status = str(flash.get("status") or "success")
    action = str(flash.get("action") or "")
    if action == "approve":
        title = "Tariff update approved."
        message = "Future calculations from the effective date will use the approved tariff version. Historical periods were not rewritten."
    elif action == "dismiss":
        title = "Tariff update dismissed."
        message = "The pending tariff version was archived. The current active tariff remains unchanged."
    elif action == "check":
        title = "Tariff check complete."
        message = str(flash.get("message") or "PowerMonitor checked for retailer tariff updates.")
    else:
        title = "Tariff update action complete."
        message = str(flash.get("message") or "Review the tariff update status below.")
    return f"""
    <div class="provider-action-result visible {html.escape(status)} provider-flash tariff-update-flash" role="status">
      <span>{html.escape(friendly_label(action or "tariff update"))}</span>
      <strong>{html.escape(title)}</strong>
      <small>{html.escape(message)}</small>
    </div>
    """


def _tariff_last_checked(diagnostics_by_provider: dict) -> str:
    values = [
        diagnostics.get("last_successful_check")
        for diagnostics in diagnostics_by_provider.values()
        if isinstance(diagnostics, dict) and diagnostics.get("last_successful_check")
    ]
    return display_timestamp(max(values)) if values else "Not checked yet"


def tariff_changed_fields_html(update: dict, current_version: dict, pending_version: dict) -> str:
    changes = list(update.get("diff") or [])
    current_effective = str(current_version.get("effective_from") or "")
    pending_effective = str(update.get("effective_date") or pending_version.get("effective_from") or "")
    if current_effective and pending_effective and current_effective != pending_effective:
        changes.append(
            {
                "field": "effective_date",
                "label": "Effective date",
                "before": current_effective,
                "after": pending_effective,
                "summary": f"Effective date: {current_effective} -> {pending_effective}",
            }
        )
    rows = []
    for change in changes:
        tone = tariff_change_tone(change)
        rows.append(
            f"""
            <div class="tariff-comparison-row {html.escape(tone)}">
              <span>{html.escape(tariff_change_label(change))}</span>
              <div><small>Current</small><strong>{html.escape(str(change.get("before") or ""))}</strong></div>
              <div><small>New</small><strong>{html.escape(str(change.get("after") or ""))}</strong></div>
              <div><small>Change</small><strong>{html.escape(tariff_change_text(change))}</strong></div>
            </div>
            """
        )
    return "".join(rows) or '<p class="notice">No rate-level fields changed.</p>'


def tariff_change_label(change: dict) -> str:
    field = str(change.get("field") or "")
    label = str(change.get("label") or field)
    labels = {
        "supply_charge": "Daily supply charge",
        "usage:peak": "Peak rate",
        "usage:off_peak": "Off-peak rate",
        "usage:overnight": "Overnight rate",
        "usage:super_off_peak": "Super off-peak rate",
        "export:standard": "Export credit",
        "tariff_windows": "Tariff windows",
        "effective_date": "Effective date",
    }
    return labels.get(field, label)


def tariff_change_tone(change: dict) -> str:
    field = str(change.get("field") or "")
    before = _tariff_number(change.get("before"))
    after = _tariff_number(change.get("after"))
    if field in {"tariff_windows", "effective_date", "gst_status", "currency", "checksum"} or before is None or after is None:
        return "neutral"
    delta = after - before
    if abs(delta) < 1e-9:
        return "neutral"
    if field.startswith("export:"):
        return "beneficial" if delta > 0 else "adverse"
    return "adverse" if delta > 0 else "beneficial"


def tariff_change_text(change: dict) -> str:
    field = str(change.get("field") or "")
    before = _tariff_number(change.get("before"))
    after = _tariff_number(change.get("after"))
    if field in {"tariff_windows", "effective_date"}:
        return "Changed"
    if before is None or after is None:
        return "Changed"
    delta = after - before
    if abs(delta) < 1e-9:
        return "No material change"
    unit = " c/kWh" if "c" in f"{change.get('before')} {change.get('after')}" else "/day" if "$" in f"{change.get('before')} {change.get('after')}" else ""
    sign = "+" if delta > 0 else "-"
    text = f"{sign}{abs(delta):.2f}{unit}"
    tone = tariff_change_tone(change)
    if tone == "beneficial":
        text += " (beneficial)"
    elif tone == "adverse":
        text += " (cost increase)" if delta > 0 and not field.startswith("export:") else " (less favourable)"
    return text


def _tariff_number(value: Any) -> Optional[float]:
    text = str(value or "").replace("$", "").replace("c/kWh", "").replace("c", "").replace("/kWh", "").replace("/day", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def tariff_impact_summary(preview: dict, current_version: dict, pending_version: dict) -> dict[str, str]:
    annual = preview.get("annual_bill_difference")
    available = bool(preview.get("available")) and annual is not None
    if available:
        annual_value = float(annual)
        monthly_value = annual_value / 12
        direction = "increase" if annual_value > 0 else "decrease" if annual_value < 0 else "not materially change"
        annual_label = f"{'+' if annual_value > 0 else '-' if annual_value < 0 else ''}{money(abs(annual_value))}/year" if annual_value else "$0/year"
        monthly_label = f"{'+' if monthly_value > 0 else '-' if monthly_value < 0 else ''}{money(abs(monthly_value))}/month" if monthly_value else "$0/month"
        summary_sentence = f"Based on your recent usage, this change may {direction} your electricity costs by about {money(abs(annual_value))} per year."
        confidence = "Medium"
    else:
        annual_label = "Unavailable"
        monthly_label = "Unavailable"
        summary_sentence = str(preview.get("basis") or "PowerMonitor needs recent usage before estimating household impact.")
        confidence = "Low"
    basis = str(preview.get("basis") or "Recent household usage unavailable.")
    battery_raw = str(preview.get("battery_recommendation_impact") or "")
    battery_label = (
        "The recommended battery size is unchanged."
        if "minimal" in battery_raw.lower() or "unchanged" in battery_raw.lower()
        else "Battery recommendation may change; review the battery scenario after approval."
        if battery_raw else
        "Battery recommendation impact is unavailable."
    )
    plan_label_text = (
        "Your current plan still appears to be the better option."
        if available and abs(float(annual or 0.0)) < 1.0
        else "Plan comparisons should be reviewed after approval."
        if available else
        "Plan comparison impact is unavailable."
    )
    ev_label = tariff_ev_guidance_label(pending_version)
    return {
        "annual_label": annual_label,
        "monthly_label": monthly_label,
        "summary_sentence": summary_sentence,
        "battery_label": battery_label,
        "plan_label": plan_label_text,
        "ev_label": ev_label,
        "confidence": confidence,
        "basis": basis,
    }


def tariff_ev_guidance_label(pending_version: dict) -> str:
    rates = dict(pending_version.get("usage_rates") or {})
    if not rates:
        return "EV charging guidance is unavailable until tariff rates are known."
    cheapest = min(rates, key=lambda bucket: float(rates.get(bucket) or 0.0))
    if cheapest == "super_off_peak":
        return "Super off-peak charging remains the cheapest window."
    return f"{cheapest.replace('_', ' ').title()} appears to be the cheapest charging window."


def setup_choice_select(name: str, selected: str, options: tuple[tuple[str, str], ...]) -> str:
    choices = "".join(
        f'<option value="{html.escape(value)}"{" selected" if selected == value else ""}>{html.escape(label)}</option>'
        for value, label in options
    )
    return f'<label><span>Your answer</span><select name="{html.escape(name)}">{choices}</select></label>'


def first_time_finish_html(setup: SetupChecklist, settings: Optional[Settings]) -> str:
    can_explain, optional, later = first_time_finish_lists(setup, settings)
    return f"""
    <section class="report-section compact-section first-time-finish">
      <div class="section-title"><div><span class="eyebrow">Finish setup</span><h2>What PowerMonitor can do next</h2></div><p>Useful now, better as you add history.</p></div>
      <div class="provider-guidance-grid first-time-finish-grid">
        <div><span>Can explain now</span><ul>{list_items_plain(can_explain)}</ul></div>
        <div><span>Optional</span><ul>{list_items_plain(optional)}</ul></div>
        <div><span>Connect later</span><ul>{list_items_plain(later)}</ul></div>
      </div>
      <p class="notice">{html.escape(first_recommendation_next_step(setup, settings))}</p>
    </section>
    """


def first_time_finish_lists(setup: SetupChecklist, settings: Optional[Settings]) -> tuple[list[str], list[str], list[str]]:
    can_explain = ["Your electricity plan and tariff timing", "Projected bill once readings or imports are available"]
    optional = ["Solar details", "EV charging history", "Battery behaviour"]
    later = []
    if settings:
        if settings.solar_setup_choice in {"solaredge", "home_assistant"}:
            can_explain.append("Solar production after the service is connected")
            optional.remove("Solar details")
        elif settings.solar_setup_choice == "manual_later":
            optional.remove("Solar details")
            later.append("Solar details")
        if settings.ev_setup_choice in {"ocular_import", "home_assistant"}:
            can_explain.append("EV charging once history is imported or connected")
            optional.remove("EV charging history")
        elif settings.ev_setup_choice == "manual_later":
            optional.remove("EV charging history")
            later.append("EV charging history")
        if settings.battery_setup_choice == "home_assistant":
            can_explain.append("Battery behaviour once connected")
            optional.remove("Battery behaviour")
        elif settings.battery_setup_choice == "manual_later":
            optional.remove("Battery behaviour")
            later.append("Battery behaviour")
    if not later:
        later.append("Extra services only if you want more detail")
    return can_explain, optional or ["Nothing required right now"], later


def list_items_plain(items: list[str]) -> str:
    return "".join(f"<li>{html.escape(item)}</li>" for item in items)


def first_recommendation_next_step(setup: SetupChecklist, settings: Optional[Settings]) -> str:
    if settings and settings.ev_setup_choice == "ocular_import":
        return "Fastest path to a useful first recommendation: upload an Ocular charging export in Settings."
    if settings and settings.solar_setup_choice in {"solaredge", "home_assistant"}:
        return "Fastest path to a useful first recommendation: connect your solar service or add recent readings."
    if setup.progress.remaining_tasks:
        return f"Fastest path to a useful first recommendation: {first_time_task_phrase(setup.progress.remaining_tasks[0].title)}."
    return "PowerMonitor can start with tariff timing now. Add readings or imports when you want more confident advice."


def first_time_task_phrase(title: str) -> str:
    replacements = {
        "Add EV charging history": "add EV charging history",
        "Choose your electricity plan": "choose your electricity plan",
        "Set your billing cycle": "set your billing cycle",
        "Add solar data": "add solar data",
        "Refresh household data": "refresh household data",
        "Live updates unavailable": "review live data",
    }
    return replacements.get(str(title), str(title).lower())


def tariff_catalog_previews_html() -> str:
    def rate(value: float) -> str:
        return f"${value:.4f}/kWh"

    cards = []
    for plan_id, plan in SUPPORTED_PLANS.items():
        windows = "".join(f"<li>{html.escape(window)}</li>" for window in plan.tariff_windows)
        rows = [
            ("Daily supply charge", f"${plan.supply_charge_per_day:.4f}/day"),
            ("Export credit", rate(plan.export_credit_rate)),
            ("Peak rate", rate(plan.import_rates["peak"])),
            ("Off-peak rate", rate(plan.import_rates["off_peak"])),
            ("Overnight rate", rate(plan.import_rates["overnight"])),
            ("Super off-peak / EV window", rate(plan.import_rates["super_off_peak"])),
        ]
        cards.append(
            f"""
            <article class="tariff-preview-card" data-plan="{html.escape(plan_id)}">
              <div><span>Catalogue tariff</span><h3>{html.escape(plan.label)}</h3></div>
              <dl>{"".join(f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>" for label, value in rows)}</dl>
              <div class="tariff-window-list"><span>Tariff windows</span><ul>{windows}</ul></div>
            </article>
            """
        )
    return "".join(cards)


def home_page_html(
    briefing: DailyBriefing,
    goals: Optional[list[GoalProgress]] = None,
    setup: Optional[SetupChecklist] = None,
    glance: Optional[GlanceState] = None,
    bill_change: Optional[dict] = None,
    insights: Optional[list[HouseholdInsight]] = None,
    trends_report: Optional[HouseholdTrendsReport] = None,
    health: Optional[dict] = None,
    energy_timeline: Optional[EnergyTimeline] = None,
    recommendations: Optional[list[Decision]] = None,
    timeline: Optional[DecisionTimeline] = None,
    billing_summary: Optional[Summary] = None,
    historical_trends: Optional[HistoricalTrends] = None,
    billing_period_end: Optional[date] = None,
) -> str:
    return f"""
    <section class="report-section home-dashboard">
      {home_glance_card_html(briefing, glance, health)}
      {home_status_cards_section_html(briefing, glance, health)}
      {home_best_next_action_compact_html(briefing)}
      {home_today_timeline_html(energy_timeline)}
      {home_bill_forecast_compact_html(glance, billing_summary, bill_change, billing_period_end)}
      {historical_trends_home_html(historical_trends)}
      {home_quick_links_html()}
    </section>
    """


def home_glance_card_html(briefing: DailyBriefing, glance: Optional[GlanceState], health: Optional[dict] = None) -> str:
    status_title, status_body, _tone = home_health_summary(briefing, glance, health)
    headline = trust_wording(briefing.suggested_next_action) if briefing.suggested_next_action else "Everything looks normal."
    if not briefing.top_priorities and not briefing.items_to_watch:
        headline = "Everything looks normal."
    return f"""
    <section class="home-dashboard-block home-glance-block">
      <div class="section-title">
        <div><span class="eyebrow">Today at a glance</span><h2>{html.escape(headline)}</h2></div>
        <p>{html.escape(briefing.overall_confidence)} confidence</p>
      </div>
      <div class="home-glance-card">
        <div>
          <strong>{html.escape(home_attention_summary(briefing))}</strong>
          <p>{html.escape(trust_wording(briefing.overall_household_status))}</p>
        </div>
        <div class="home-glance-meta">
          <span>Status</span>
          <strong>{html.escape(status_title)}</strong>
          <small>{html.escape(status_body)}</small>
        </div>
      </div>
    </section>
    """


def home_status_cards_section_html(briefing: DailyBriefing, glance: Optional[GlanceState], health: Optional[dict] = None) -> str:
    return f"""
    <section class="home-dashboard-block home-status-block">
      <div class="home-status-grid">
        {home_status_tile_html("Can I charge?", *home_charge_summary(glance))}
        {home_status_tile_html("Is spending normal?", *home_spending_summary(briefing))}
        {home_status_tile_html("Is everything healthy?", *home_health_summary(briefing, glance, health))}
      </div>
    </section>
    """


def home_best_next_action_compact_html(briefing: DailyBriefing) -> str:
    action_items = dedupe_briefing_items([*briefing.top_priorities, *briefing.items_to_watch])
    lead = action_items[0] if action_items else None
    if not lead:
        return """
        <section class="home-dashboard-block home-action-block">
          <div class="section-title"><div><span class="eyebrow">Best next action</span><h2>Nothing important needs action.</h2></div></div>
          <div class="home-inline-summary">
            <p>Everything looks normal right now. Decisions keeps the full recommendation centre when you want to explore options.</p>
            <a class="button-link" href="#whatif">View decisions</a>
          </div>
        </section>
        """
    action_text = trust_wording(lead.recommendation or lead.summary or lead.title)
    return f"""
    <section class="home-dashboard-block home-action-block">
      <div class="section-title"><div><span class="eyebrow">Best next action</span><h2>{html.escape(trust_wording(lead.title))}</h2></div><p>{html.escape(lead.confidence)} confidence</p></div>
      <div class="home-inline-summary home-action-summary">
        <div>
          <strong>{html.escape(action_text)}</strong>
          <p>{html.escape(trust_wording(lead.summary))}</p>
        </div>
        <a class="button-link" href="#whatif">View decisions</a>
      </div>
    </section>
    """


def home_today_timeline_html(timeline: Optional[EnergyTimeline], limit: int = 3) -> str:
    if not timeline or not timeline.events:
        return """
        <section class="home-dashboard-block home-timeline-block">
          <div class="section-title"><div><span class="eyebrow">Today's timeline</span><h2>No energy events are ready yet.</h2></div></div>
          <div class="home-inline-summary">
            <p>History keeps the full event list once more household activity is available.</p>
            <a class="button-link" href="#timeline">View full history</a>
          </div>
        </section>
        """
    ordered_events = sorted(
        timeline.events,
        key=lambda event: str(getattr(event, "sort_time", "") or getattr(event, "local_time", "")),
        reverse=True,
    )
    rows = []
    for event in ordered_events[:limit]:
        rows.append(
            f"""
            <article class="home-timeline-row">
              <div class="timeline-stamp"><span aria-hidden="true">{html.escape(str(getattr(event, "icon", "") or "•"))}</span><time>{html.escape(str(getattr(event, "local_time", "")))}</time></div>
              <div>
                <span>{html.escape(str(getattr(event, "confidence", "") or "Current").title())} confidence</span>
                <strong>{html.escape(str(getattr(event, "title", "")))}</strong>
                <p>{html.escape(str(getattr(event, "explanation", "")))}</p>
              </div>
            </article>
            """
        )
    return f"""
    <section class="home-dashboard-block home-timeline-block">
      <div class="section-title"><div><span class="eyebrow">Today's timeline</span><h2>{html.escape(timeline.summary)}</h2></div><p>Showing latest {min(len(ordered_events), limit)} events</p></div>
      <div class="home-timeline-list">{''.join(rows)}</div>
      <div class="provider-action-row"><a class="button-link" href="#timeline">View full history</a></div>
    </section>
    """


def forecast_basis_homeowner_text(basis: str) -> str:
    normalized = str(basis or "").strip().lower()
    if normalized == "previous period length":
        return "Forecast based on your previous billing period."
    return "Projection assumes the rest of this bill follows your recent usage pattern."


def home_bill_forecast_compact_html(
    glance: Optional[GlanceState],
    summary: Optional[Summary] = None,
    bill_change: Optional[dict] = None,
    billing_period_end: Optional[date] = None,
) -> str:
    if not glance:
        return """
        <section class="home-dashboard-block home-bill-block">
          <div class="section-title"><div><span class="eyebrow">Bill forecast</span><h2>Bill forecast is not available yet.</h2></div></div>
          <div class="home-inline-summary">
            <p>Bills keeps the full financial explanation, tariff comparison, and reconciliation when enough data is ready.</p>
            <a class="button-link" href="#bills">View bills</a>
          </div>
        </section>
        """
    current = float(getattr(glance, "bill_current", 0.0) or 0.0)
    projected = float(getattr(glance, "bill_projected", 0.0) or 0.0)
    basis = getattr(glance, "bill_projection_basis", "Current run-rate")
    forecast_note = forecast_basis_homeowner_text(str(basis))
    progress_copy = "Billing period progress unavailable."
    if summary is not None:
        current_days = max(int(getattr(summary, "supply_days", 0) or 0), 0)
        period_end = billing_period_end or getattr(summary, "end", None)
        total_days = None
        if isinstance(period_end, date):
            total_days = max((period_end - summary.start).days + 1, current_days)
        if total_days:
            progress_copy = f"{current_days} of {total_days} days"
        elif current_days:
            progress_copy = f"{current_days} days tracked"

    difference = None
    previous_bill = None
    confidence = str(getattr(glance, "confidence", "") or "Medium").title()
    if isinstance(bill_change, dict):
        difference = bill_change.get("difference")
        previous_bill = bill_change.get("previous_bill")
        confidence = str(bill_change.get("confidence") or confidence).title()

    if isinstance(difference, (int, float)):
        difference_value = float(difference)
        if abs(difference_value) < 0.005:
            delta_value = "No change"
            delta_note = "In line with your previous completed bill."
            delta_tone = "neutral"
        else:
            delta_value = f"{'▲' if difference_value > 0 else '▼'} {money(abs(difference_value))}"
            delta_note = (
                "Higher than your previous completed bill."
                if difference_value > 0
                else "Lower than your previous completed bill."
            )
            delta_tone = "up" if difference_value > 0 else "down"
    else:
        delta_value = "Not ready yet"
        delta_note = "PowerMonitor needs a previous completed bill for comparison."
        delta_tone = "neutral"

    previous_bill_note = (
        f"Previous completed bill {money(float(previous_bill))}."
        if isinstance(previous_bill, (int, float))
        else delta_note
    )
    return f"""
    <section class="home-dashboard-block home-bill-block">
      <div class="section-title"><div><span class="eyebrow">Bill forecast</span><h2>Where is this billing period heading?</h2></div></div>
      <div class="home-forecast-summary">
        <div class="home-forecast-main">
          <span>Current spend</span>
          <strong>{html.escape(money(current))}</strong>
          <small>{html.escape(progress_copy)}</small>
        </div>
        <div class="home-forecast-grid">
          <div class="home-forecast-detail">
            <span>Projected bill</span>
            <strong>{html.escape(money(projected))}</strong>
            <small>{html.escape(forecast_note)}</small>
          </div>
          <div class="home-forecast-detail home-forecast-delta home-forecast-delta-{html.escape(delta_tone)}">
            <span>Compared with last bill</span>
            <strong>{html.escape(delta_value)}</strong>
            <small>{html.escape(previous_bill_note)}</small>
          </div>
          <div class="home-forecast-detail">
            <span>Forecast confidence</span>
            <strong>{html.escape(confidence)}</strong>
            <small>{html.escape(forecast_note)}</small>
          </div>
        </div>
      </div>
      <div class="home-inline-summary">
        <p>{html.escape(forecast_note)}</p>
        <a class="button-link" href="#bills">View bills</a>
      </div>
    </section>
    """


def historical_trends_home_html(trends: Optional[HistoricalTrends], limit: int = 6) -> str:
    cards = historical_trend_home_cards(trends)[:limit]
    if not cards:
        return ""
    return f"""
    <section class="home-dashboard-block home-historical-trends-block">
      <div class="section-title"><div><span class="eyebrow">Historical trends</span><h2>How yesterday compared with normal.</h2></div><p>{len(cards)} comparisons</p></div>
      <div class="home-historical-trend-grid">{''.join(cards)}</div>
    </section>
    """


def historical_trend_home_cards(trends: Optional[HistoricalTrends]) -> list[str]:
    if not trends:
        return []
    specs = [
        ("cost", "rolling_7_day_average_cost", "Yesterday cost", "7-day average", False, "Lower household cost than normal."),
        ("cost", "rolling_30_day_average_cost", "Yesterday cost", "30-day average", False, "Shows whether yesterday was cheaper than your recent month."),
        ("grid", "rolling_7_day_average_import", "Grid import", "7-day average", False, "Less electricity purchased from the grid than normal."),
        ("grid", "rolling_30_day_average_import", "Grid import", "30-day average", False, "Shows whether grid use is moving away from your recent month."),
        ("solar", "rolling_7_day_average_generation", "Solar generation", "7-day average", True, "Your solar production was better than usual."),
        ("exports", "rolling_7_day_average_exports", "Solar exports", "7-day average", True, "Shows whether more solar was sent to the grid than usual."),
        ("self_consumption", "rolling_average_self_consumption", "Solar used at home", "recent average", True, "More self-consumption means more solar stayed in the home."),
    ]
    cards = []
    for domain_name, metric_id, title, comparison_label, higher_is_better, explanation in specs:
        group = getattr(trends, domain_name, None)
        if not group or not getattr(group, "available", False):
            continue
        metric = group.metrics.get(metric_id)
        if not historical_trend_metric_ready(metric):
            continue
        cards.append(
            historical_trend_card_html(
                metric,
                title=title,
                comparison_label=comparison_label,
                higher_is_better=higher_is_better,
                explanation=explanation,
            )
        )
    return cards


def historical_trend_metric_ready(metric: Optional[HistoricalTrendMetric]) -> bool:
    if not metric or metric.value is None or metric.comparison_value is None:
        return False
    required_days = 10 if "30" in metric.metric_id or metric.metric_id == "rolling_average_self_consumption" else 3
    return int(metric.sample_days or 0) >= required_days


def historical_trend_card_html(
    metric: HistoricalTrendMetric,
    *,
    title: str,
    comparison_label: str,
    higher_is_better: bool,
    explanation: str,
) -> str:
    yesterday = float(metric.comparison_value or 0.0)
    comparison = float(metric.value or 0.0)
    delta = yesterday - comparison
    pct = delta / comparison * 100 if comparison else 0.0
    direction = historical_home_direction(delta, higher_is_better)
    relation = "above" if delta > 0 else "below" if delta < 0 else "in line with"
    pct_text = "in line with" if abs(pct) < 0.5 else f"{abs(pct):.0f}% {relation}"
    return f"""
      <article class="home-historical-trend-card {html.escape(direction.lower())}">
        <div class="home-historical-trend-head">
          <span>{html.escape(title)}</span>
          <strong>{html.escape(historical_direction_indicator(direction))}</strong>
        </div>
        <div class="home-historical-trend-value">{html.escape(format_historical_value(yesterday, metric.unit))}</div>
        <p>{html.escape(pct_text)} your {html.escape(comparison_label)}</p>
        <div class="home-historical-trend-meta">
          <span>Yesterday: {html.escape(format_historical_value(yesterday, metric.unit))}</span>
          <span>{html.escape(comparison_label.title())}: {html.escape(format_historical_value(comparison, metric.unit))}</span>
        </div>
        <small>{html.escape(explanation)}</small>
      </article>
    """


def historical_home_direction(delta: float, higher_is_better: bool) -> str:
    if abs(delta) < 0.000001:
        return UNCHANGED
    improved = delta > 0 if higher_is_better else delta < 0
    return IMPROVING if improved else WORSENING


def historical_direction_indicator(direction: str) -> str:
    if direction == IMPROVING:
        return "▲ Improving"
    if direction == WORSENING:
        return "▼ Worsening"
    return "→ Unchanged"


def format_historical_value(value: float, unit: str) -> str:
    if unit == "$":
        return money(value)
    if unit == "%":
        return f"{value:.0f}%"
    return kwh(value)


def home_quick_links_html() -> str:
    links = (
        ("Bills", "#bills", "Bill explanation and tariff comparison"),
        ("Solar", "#solar", "Generation, exports, and solar performance"),
        ("EV", "#ev", "Charging sessions, costs, and EV insights"),
        ("Decisions", "#whatif", "Scenarios, recommendations, and decision support"),
        ("History", "#analytics", "Timeline and longer-term household history"),
        ("Settings", "#system", "Setup, imports, providers, and diagnostics"),
    )
    cards = "".join(
        f"""
        <a class="home-page-link" href="{href}">
          <span>{html.escape(label)}</span>
          <strong>{html.escape(description)}</strong>
        </a>
        """
        for label, href, description in links
    )
    return f"""
    <section class="home-dashboard-block home-links-block">
      <div class="section-title"><div><span class="eyebrow">Quick links</span><h2>Open the page that owns the detail.</h2></div></div>
      <div class="home-page-link-grid">{cards}</div>
    </section>
    """


def advisor_home_html(advisor: Advisor) -> str:
    tone = {
        "IMPROVING": "green",
        "ON_TRACK": "green",
        "SLIGHTLY_HIGH": "amber",
        "HIGH": "red",
        "UNKNOWN": "amber",
    }.get(advisor.status, "amber")
    impact = advisor.estimated_financial_impact
    impact_text = unable_to_estimate()
    if impact and impact.billing_period is not None:
        impact_text = money(impact.billing_period)
    evidence_summary = getattr(advisor, "evidence_summary", None)
    evidence_quality = evidence_summary.evidence_quality.value.title() if evidence_summary else "Unknown"
    evidence_reason = evidence_summary.reason if evidence_summary else "Evidence summary unavailable."
    return f"""
    <section class="source-status {tone}">
      <div><span>{html.escape(customer_label(advisor.advisor_type.replace("_", " ").title()))} Recommendation</span><h2>{html.escape(advisor.headline)}</h2></div>
      <p>{html.escape(advisor.summary)}</p>
      <div class="metrics">
        {metric("Status", advisor.status.replace("_", " "), "Current recommendation", "cost")}
        {metric("Projection impact", impact_text, customer_label(impact.basis) if impact and impact.basis else "Bill estimate", "cost")}
        {metric("Confidence", advisor.confidence, advisor.confidence_reason, "primary")}
        {metric("Evidence", evidence_quality, evidence_reason, "primary")}
        {metric("Next review", advisor.next_review_time or "Unavailable", advisor.freshness, "solar")}
      </div>
      {evidence_trail_html(advisor)}
    </section>
    """


def charging_advice_home_html(charging_advice: ChargingAdvice) -> str:
    advisor = charging_advice.advisor
    tone = {
        "CHARGE_NOW": "green",
        "WAIT": "amber",
        "AVOID": "red",
        "UNKNOWN": "amber",
    }[advisor.status]
    benefit = advisor.estimated_financial_impact
    benefit_text = unable_to_estimate()
    if benefit and benefit.today is not None:
        benefit_text = f"{money(benefit.today)} per kWh"
    rate_text = (
        f"{money(charging_advice.current_window_rate)} / {money(charging_advice.next_lower_cost_window_rate)}"
        if charging_advice.current_window_rate is not None and charging_advice.next_lower_cost_window_rate is not None
        else "Unavailable"
    )
    difference_text = (
        f"{money(charging_advice.per_kwh_difference)} per kWh"
        if charging_advice.per_kwh_difference is not None
        else "Unavailable"
    )
    reasoning = evidence_trail_html(advisor)
    evidence_summary = getattr(advisor, "evidence_summary", None)
    evidence_quality = evidence_summary.evidence_quality.value.title() if evidence_summary else "Unknown"
    evidence_reason = evidence_summary.reason if evidence_summary else "Evidence summary unavailable."
    return f"""
    <section class="source-status {tone}">
      <div><span>Charging Advisor</span><h2>{html.escape(advisor.headline)}</h2></div>
      <p>{html.escape(advisor.summary)}</p>
      <div class="metrics">
        {metric("Status", advisor.status.replace("_", " "), "Shared Advisor", "ev")}
        {metric("Tariff now / next cheaper", rate_text, f"{charging_advice.current_window_label} to {charging_advice.next_lower_cost_window_label}", "cost")}
        {metric("Difference", difference_text, "Current tariff minus next window tariff", "cost")}
        {metric("Benefit", benefit_text, benefit.basis if benefit and benefit.basis else "Measured where tariff difference exists", "cost")}
        {metric("Confidence", advisor.confidence, advisor.confidence_reason, "primary")}
        {metric("Evidence", evidence_quality, evidence_reason, "primary")}
        {metric("Next cheaper window", charging_advice.next_lower_cost_window or "Unavailable", charging_advice.next_lower_cost_window_start or "Configured tariff schedule", "solar")}
        {metric("Lowest-cost window", charging_advice.lowest_cost_window or "Unavailable", charging_advice.lowest_cost_window_start or "Configured tariff schedule", "solar")}
        {metric("EV data", charging_advice.charger_data_status.replace("_", " "), charging_advice.ev_session_freshness, "ev")}
        {metric("Solar data", charging_advice.solar_data_status, "Daily summary only; no live surplus assumption", "solar")}
      </div>
      {reasoning}
    </section>
    """


def evidence_trail_html(advisor: Advisor) -> str:
    items = getattr(advisor.evidence_trail, "items", [])
    if not items:
        return ""
    rows = []
    for item in items:
        contribution = ""
        if item.financial_contribution and item.financial_contribution.today is not None:
            contribution = f"<small>{html.escape(money(item.financial_contribution.today))} per kWh</small>"
        rows.append(
            f"""
            <li>
              <span>{html.escape(item.source)}</span>
              <strong>{html.escape(item.observation)}</strong>
              <p>{html.escape(item.impact)}</p>
              <small>{html.escape(item.confidence)} confidence · {html.escape(item.freshness or "Freshness unavailable")}</small>
              {contribution}
            </li>
            """
        )
    return f"""
      <details class="evidence-trail">
        <summary>Why?</summary>
        <ol>{''.join(rows)}</ol>
      </details>
    """


def bill_attribution_html(summary: Summary, plan: dict, settings: Settings) -> str:
    current = plan["current_plan"]
    rows = [
        bills_summary_row_html("Grid energy", money(summary.import_cost), kwh(summary.grid_import_kwh), "grid"),
        bills_summary_row_html("Household usage cost", money(current["household_usage_cost"]), "Grid cost excluding EV charging", "cost"),
        bills_summary_row_html("Peak usage cost", money(summary.buckets["peak"]["cost"]), f"{summary.peak_cost_share_pct:.1f}% of import cost", "bad"),
        bills_summary_row_html("Supply charge", money(summary.supply_cost), f"{money(summary.supply_cost / summary.supply_days)}/day", "supply"),
        bills_summary_row_html("EV charging cost", money(summary.ev_charging_cost), ev_cost_label(settings, summary), "ev"),
        bills_summary_row_html("Export credit", f"-{money(summary.export_credit)}", kwh(summary.solar_exported_kwh), "credit"),
        bills_summary_row_html("Plan savings/loss", signed_money(-plan["difference"]), plan["recommendation"], "good" if plan["savings"] > 0 else "bad" if plan["savings"] < 0 else "neutral"),
    ]
    return f"""
    <section class="report-section compact-section bills-detail-section">
      <div class="section-title"><div><span class="eyebrow">Bill attribution</span><h2>What is driving the bill?</h2></div><p>Biggest bill components first</p></div>
      <p class="scenario-note">This splits the estimated bill into the parts a household can usually act on: grid energy, fixed supply charge, solar export credit, EV charging, and peak usage.</p>
      <div class="bills-summary-list bills-readable-list">{''.join(rows)}</div>
    </section>
    """


def actual_synergy_bill_html(comparison: Optional[dict], settings: Settings) -> str:
    if not comparison:
        return f"""
        <section class="report-section compact-section">
          <div class="section-title"><div><span class="eyebrow">Bill reconciliation</span><h2>How close was PowerMonitor?</h2></div><p>Retailer reconciliation</p></div>
          <p class="notice">No Synergy PDF bill has been imported yet. Upload one in Settings to compare PowerMonitor's projection with the actual retailer bill.</p>
        </section>
        """
    bill = comparison.get("latest_bill") or {}
    actual_total = comparison.get("actual_bill_total", comparison.get("actual_bill"))
    estimated_total = comparison.get("estimated_bill_total", comparison.get("projected_bill"))
    difference = comparison.get("dollar_variance", comparison.get("difference"))
    difference_text = signed_money(float(difference)) if difference is not None else "Not available"
    category = str(comparison.get("variance_category") or comparison.get("status") or "comparison unavailable").replace("_", " ")
    confidence = str(comparison.get("confidence") or bill.get("confidence") or "low").title()
    explanation = str(comparison.get("explanation") or "PowerMonitor compared the imported bill with its estimate for the same period.")
    periods = bill.get("periods") or []
    period_rows = "".join(
        f"""
        <div class="completeness-row neutral">
          <span>{html.escape(str(period.get("plan_name") or "Synergy plan"))}</span>
          <strong>{html.escape(str(period.get("start_date") or ""))} to {html.escape(str(period.get("end_date") or ""))}</strong>
          <small>Plan period detected on the bill.</small>
        </div>
        """
        for period in periods
    ) or '<p class="notice">No plan period was detected on the imported bill.</p>'
    bucket_rows = bill_bucket_rows_html(bill, settings)
    components = comparison.get("components") or []
    component_rows = "".join(
        f"""
        <div class="bill-change-row {'bad' if float(component.get('variance') or 0.0) > 0 else 'good' if float(component.get('variance') or 0.0) < 0 else 'neutral'}">
          <div><span>{html.escape(str(component.get('category') or '').replace('_', ' ').title())}</span><strong>{html.escape(str(component.get('label') or 'Bill component'))}</strong><p>{html.escape(str(component.get('explanation') or ''))}</p></div>
          <strong>{signed_money(float(component.get('variance') or 0.0))}</strong>
        </div>
        """
        for component in components
    ) or '<p class="notice">No detailed variance breakdown is available yet.</p>'
    data_gaps = comparison.get("data_gaps") or []
    tariff_mismatches = comparison.get("tariff_mismatches") or []
    warnings = list(bill.get("warnings") or []) + list(data_gaps)
    warning_html = "".join(f"<li>{html.escape(str(warning))}</li>" for warning in warnings) or "<li>No data gaps affecting this comparison were found.</li>"
    mismatch_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in tariff_mismatches)
    tariff_action = (
        f"""
        <div class="notice">
          <strong>Review tariff settings.</strong>
          <p>The retailer bill contains rates that differ from the configured tariff. Check the Billing and Tariff setup section so future estimates use the right plan.</p>
          <ul>{mismatch_html}</ul>
        </div>
        """
        if tariff_mismatches else ""
    )
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Bill reconciliation</span><h2>How close was PowerMonitor?</h2></div><p>{html.escape(confidence)} confidence</p></div>
      <p class="scenario-note">{html.escape(explanation)}</p>
      <div class="metrics">
        {metric("Invoice", str(comparison.get("invoice_number") or bill.get("invoice_number") or "n/a"), f"Account {str(bill.get('account_number') or 'masked')}", "primary")}
        {metric("Actual bill", money(float(actual_total or 0.0)), "Synergy bill total", "cost")}
        {metric("PowerMonitor estimate", money(float(estimated_total or 0.0)), "Same billing period", "supply")}
        {metric("Difference", difference_text, f"{category.title()} accuracy", "bad" if difference is not None and float(difference) > 0 else "good" if difference is not None and float(difference) < 0 else "neutral")}
      </div>
      <div class="bill-change-grid">
        <div class="bill-change-card main"><h3>What explains the gap?</h3>{component_rows}</div>
        <div class="bill-change-card"><h3>Data gaps affecting accuracy</h3><ul class="bill-change-list">{warning_html}</ul>{tariff_action}</div>
      </div>
      <div class="system-grid">
        <div class="system-card"><div class="chart-heading"><h3>Billing periods</h3><span>Plan changes supported</span></div><div class="completeness-grid">{period_rows}</div></div>
        <div class="system-card"><div class="chart-heading"><h3>Tariff rates detected</h3><span>Bill line items</span></div><div class="completeness-grid">{bucket_rows}</div></div>
      </div>
      <details class="evidence-trail"><summary>Advanced reconciliation details</summary><pre>{html.escape(json.dumps({key: comparison.get(key) for key in ('coverage', 'variance_category', 'confidence')}, indent=2, sort_keys=True))}</pre></details>
    </section>
    """


def bill_bucket_rows_html(bill: dict, settings: Settings) -> str:
    totals = bill.get("totals") or {}
    buckets = totals.get("usage_by_bucket") or {}
    configured = {
        "overnight": settings.import_rates.get("overnight"),
        "off_peak": settings.import_rates.get("off_peak"),
        "super_off_peak": settings.import_rates.get("super_off_peak"),
        "peak": settings.import_rates.get("peak"),
    }
    rows = []
    for bucket, values in buckets.items():
        rate = values.get("unit_rate") if isinstance(values, dict) else None
        kwh_value = values.get("kwh") if isinstance(values, dict) else None
        configured_rate = configured.get(bucket)
        mismatch = (
            configured_rate is not None and rate is not None and abs(float(configured_rate) - float(rate)) > 0.0005
        )
        rows.append(
            f"""
            <div class="completeness-row {'bad' if mismatch else 'good'}">
              <span>{html.escape(str(bucket).replace("_", " ").title())}</span>
              <strong>{html.escape(f"{float(kwh_value or 0):.2f} kWh @ ${float(rate or 0):.6f}")}</strong>
              <small>{html.escape("Configured tariff differs from bill rate." if mismatch else "Matches configured rate or no configured comparison.")}</small>
            </div>
            """
        )
    return "".join(rows) or '<p class="notice">No usage bucket rates were extracted.</p>'


def bill_change_explanation_html(explanation: Optional[dict], compact: bool = False) -> str:
    if not explanation:
        return ""
    if not explanation.get("available"):
        return f"""
        <section class="report-section compact-section bill-change-section">
          <div class="section-title"><div><span class="eyebrow">Bill explanation</span><h2>Why has my bill changed?</h2></div><p>Comparison unavailable</p></div>
          <p class="notice">{html.escape(str(explanation.get("narrative") or explanation.get("reason") or "Historical comparison is unavailable."))}</p>
        </section>
        """

    contributors = explanation.get("contributors") or []
    positive = explanation.get("positive_changes") or []
    negative = explanation.get("negative_changes") or []
    ev = explanation.get("ev_explanation")
    contributor_rows = "".join(bill_change_contributor_html(item) for item in contributors[:5])
    if not contributor_rows:
        contributor_rows = '<p class="notice">No major bill drivers could be separated from the available data.</p>'
    positive_rows = bill_change_small_list_html(positive[:3], "Money saved", "good")
    negative_rows = bill_change_small_list_html(negative[:3], "Extra cost", "bad")
    ev_html = ev_bill_explanation_html(ev) if ev else ""
    metrics = "".join(
        [
            metric("Current bill", money(float(explanation["current_bill"])), "Projected current billing period" if compact else "Current billing period", "primary"),
            metric("Previous bill", money(float(explanation["previous_bill"])), "Previous completed billing period", "cost"),
            metric("Difference", signed_money(float(explanation["difference"])), f"{explanation.get('confidence', 'Medium')} confidence", "bad" if float(explanation["difference"]) > 0 else "good" if float(explanation["difference"]) < 0 else "neutral"),
        ]
    )
    return f"""
    <section class="report-section compact-section bill-change-section bills-detail-section">
      <div class="section-title"><div><span class="eyebrow">What changed?</span><h2>Why has my bill changed?</h2></div><p>Largest contributors first</p></div>
      <p class="scenario-note">{html.escape(str(explanation.get("narrative") or ""))}</p>
      <div class="metrics bill-change-metrics">{metrics}</div>
      <div class="bill-change-grid bill-change-grid-story">
        <div class="bill-change-card main bill-change-card-readable"><h3>Largest contributors</h3>{contributor_rows}</div>
        <div class="bill-change-card bill-change-card-summary">
          <h3>Change summary</h3>
          <div class="bill-change-summary-grid">
          <div class="bill-change-summary-block">
            <span>Reduced cost</span>
            {positive_rows}
          </div>
          <div class="bill-change-summary-block">
            <span>Added cost</span>
            {negative_rows}
          </div>
          </div>
        </div>
      </div>
      {ev_html}
      <p class="scenario-note">Contributions are estimates from available billing components and may not add exactly to the bill difference when projections, partial data, or overlapping EV and tariff effects are involved.</p>
    </section>
    """


def bill_change_contributor_html(item: dict) -> str:
    impact = float(item.get("impact") or 0.0)
    tone = "bad" if impact > 0 else "good" if impact < 0 else "neutral"
    return f"""
    <div class="bill-change-row {tone}">
      <div class="bill-change-copy"><strong>{html.escape(str(item.get("label") or "Bill change"))}</strong><p>{html.escape(str(item.get("detail") or ""))}</p></div>
      <strong class="bill-change-amount">{signed_money(impact)}</strong>
    </div>
    """


def bill_change_small_list_html(items: list[dict], empty_label: str, tone: str = "neutral") -> str:
    if not items:
        return f'<p class="notice">No clear {html.escape(empty_label.lower())} found in the available comparison.</p>'
    return "<ul class=\"bill-change-list\">" + "".join(
        f"<li class=\"{html.escape(tone)}\"><span>{html.escape(str(item.get('label') or 'Change'))}</span><strong>{signed_money(float(item.get('impact') or 0.0))}</strong></li>"
        for item in items
    ) + "</ul>"


def ev_bill_explanation_html(ev: dict) -> str:
    petrol = ev.get("petrol_equivalent_cost")
    petrol_text = money(float(petrol)) if petrol is not None else "Not configured"
    rows = [
        bills_summary_row_html("Total EV energy", kwh(float(ev.get("total_energy_kwh") or 0.0)), "Actual charger data", "ev"),
        bills_summary_row_html("Charging cost", money(float(ev.get("charging_cost") or 0.0)), "Approximate electricity cost", "cost"),
        bills_summary_row_html("If all Peak", money(float(ev.get("cost_if_all_peak") or 0.0)), "Same energy at Peak rate", "bad"),
        bills_summary_row_html("Off-peak saving", money(float(ev.get("savings_vs_peak") or 0.0)), "Compared with all-Peak charging", "good"),
        bills_summary_row_html("Petrol equivalent", petrol_text, "Uses configured vehicle efficiency and fuel price", "supply"),
    ]
    return f"""
    <div class="bill-change-ev">
      <div class="chart-heading"><h3>EV charging explanation</h3><span>{html.escape(str(ev.get("source") or "charger data").replace("_", " "))}</span></div>
      <div class="bills-summary-list bills-summary-list-compact bills-readable-list bills-readable-list-compact">{''.join(rows)}</div>
    </div>
    """


def financial_highlights_html(summary: Summary, projected_bill_value: float, plan: dict, recommendations: list) -> str:
    saving_text = (
        f"{money(plan['savings'])} ahead on the current plan"
        if plan.get("savings", 0) > 0
        else f"{money(abs(plan.get('savings', 0)))} possible plan gap"
        if plan.get("savings", 0) < 0
        else "No plan gap detected"
    )
    return f"""
    <section class="report-section compact-section bills-overview-section">
      <div class="section-title"><div><span class="eyebrow">How much is my bill?</span><h2>Current bill summary</h2></div><p>{summary.start.strftime('%d %b')} to {summary.end.strftime('%d %b %Y')}</p></div>
      <div class="bills-overview-grid">
        <div class="bills-overview-hero">
          <span>Projected bill</span>
          <strong>{money(projected_bill_value)}</strong>
          <p>Projection assumes the rest of this bill follows your recent usage pattern.</p>
          <div class="bills-overview-subline">
            <div>
              <span>Current spend</span>
              <strong>{money(summary.net_cost)}</strong>
            </div>
            <small>{summary.supply_days} days so far</small>
          </div>
        </div>
        <div class="bills-overview-side-summary">
          <div class="bills-summary-row neutral">
            <div class="bills-summary-copy"><span>Savings opportunity</span><small>{html.escape(plan["recommendation"])}</small></div>
            <strong>{html.escape(saving_text)}</strong>
          </div>
          <div class="bills-summary-row bad">
            <div class="bills-summary-copy"><span>Peak usage cost</span><small>{summary.peak_cost_share_pct:.1f}% of import cost</small></div>
            <strong>{money(summary.buckets["peak"]["cost"])}</strong>
          </div>
        </div>
      </div>
    </section>
    """


def bills_page_html(
    summary: Summary,
    projected_bill_value: float,
    plan: dict,
    recommendations: list,
    actual_bill_comparison: Optional[dict],
    bill_change: Optional[dict],
    comparison: Optional[dict],
    settings: Settings,
) -> str:
    comparison_html_block = comparison_html(comparison) if comparison else ""
    actions_html = bills_actions_html(recommendations, plan)
    advanced_details_html = advanced_bill_details_html(summary, settings)
    tariff_context = tariff_context_statement_html(summary.tariff_resolution, "Bills")
    return f"""
    {page_heading_html("Where did my money go?", "Bill attribution, tariff exposure, export credits, and plan comparison.")}
    {tariff_context}
    {financial_highlights_html(summary, projected_bill_value, plan, recommendations)}
    {summary_html("Current billing period breakdown", summary, settings)}
    {bill_change_explanation_html(bill_change)}
    {bill_attribution_html(summary, plan, settings)}
    {comparison_html_block}
    {actual_synergy_bill_html(actual_bill_comparison, settings)}
    <div class="bills-layout-grid bills-layout-grid--actions">
      {plan_comparison_html(plan, car_comparison(settings))}
      {actions_html}
    </div>
    {advanced_details_html}
    """


def timeline_with_state_changes(timeline: DecisionTimeline, state_changes: list[dict]) -> DecisionTimeline:
    entries = list(timeline.entries)
    existing = {(entry.timestamp, entry.event_type, entry.title) for entry in entries}
    for change in state_changes[:10]:
        entry = TimelineEntry(
            timestamp=str(change.get("detected_at") or ""),
            title=str(change.get("title") or "Household state changed"),
            summary=str(change.get("summary") or ""),
            financial_impact=None,
            confidence=str(change.get("severity") or "info").title(),
            evidence_sources=["Household State snapshots"],
            event_type=f"state_change:{change.get('change_type') or 'unknown'}",
        )
        key = (entry.timestamp, entry.event_type, entry.title)
        if key not in existing:
            entries.append(entry)
            existing.add(key)
    entries.sort(key=lambda item: item.timestamp, reverse=True)
    return DecisionTimeline(entries)


def solar_page_html(summary: Summary, recommendations: list) -> str:
    solar_recs = [
        item for item in recommendations
        if recommendation_category(item).lower() == "solar"
        or "solar" in recommendation_title(item).lower()
    ]
    cards = "".join(
        [
            metric("Solar generated", kwh(summary.solar_generated_kwh), "Total rooftop production", "primary"),
            metric("Solar used directly", kwh(summary.solar_used_kwh), "Consumed at home", "solar"),
            metric("Solar exported", kwh(summary.solar_exported_kwh), "Sent to grid", "export"),
            metric("Self-consumption", f"{summary.self_consumption_pct:.1f}%", "Solar kept at home", "solar"),
            metric("Export percentage", f"{summary.solar_export_pct:.1f}%", "Generation exported", "export"),
        ]
    )
    rec_html = solar_insights_html(solar_recs)
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Solar detail</span><h2>Generation and self-consumption</h2></div><p>Supporting metrics</p></div>
      <div class="metrics">{cards}</div>
    </section>
    {rec_html}
    """


def solar_insights_html(recommendations: list[Decision]) -> str:
    if not recommendations:
        return """
        <section class="report-section compact-section">
          <div class="section-title"><div><span class="eyebrow">Solar insights</span><h2>No solar-specific insight needs attention.</h2></div></div>
          <p class="notice">Generation, self-consumption, and export metrics still appear above for this period.</p>
        </section>
        """
    items = "".join(
        f"""
        <article class="action-card {html.escape(recommendation_severity(item))}">
          <span>{html.escape(recommendation_confidence(item))} confidence</span>
          <h3>{html.escape(recommendation_title(item))}</h3>
          <p><strong>Why this matters</strong>{html.escape(recommendation_why(item))}</p>
        </article>
        """
        for item in sorted(recommendations, key=recommendation_sort_key)[:2]
    )
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Solar insights</span><h2>What stands out in this period?</h2></div><p>Top {min(len(recommendations), 2)}</p></div>
      <div class="action-grid">{items}</div>
    </section>
    """


def ev_source_status_html(summary: Summary, settings: Settings) -> str:
    source = summary.ev_charging_source
    configured = settings.ev_data_source
    ignored = int((summary.ev_session_analytics or {}).get("ignored_incomplete_sessions") or 0)
    if source == "ocular_export":
        title = "Using Ocular charging history"
        body = "Complete Ocular sessions are available for this period. Costs are calculated from exact session times and delivered energy."
        tone = "green"
    elif source == "home_assistant_ocpp":
        title = "Using Home Assistant OCPP"
        body = "OCPP charger snapshots are available for this period. Ocular export support remains enabled when imported sessions are present."
        tone = "amber"
    else:
        title = "Using estimated EV fallback"
        body = (
            "No completed charger sessions imported for today yet. "
            if summary.start == summary.end == local_today()
            else "No completed Ocular or HA OCPP sessions were found for this period. "
        )
        body += (
            f"PowerMonitor is estimating overnight EV charging above {settings.ev_baseline_watts:.0f} W. "
            "Ocular may only export completed sessions, so today's active charge may appear after the next export. "
            "Import Ocular history with: python3 collector/import_ocular_export.py /path/to/export.xlsx"
        )
        if ignored:
            body += f" {ignored} zero-kWh or incomplete imported session row was ignored."
        tone = "red"
    return f"""
    <section class="source-status {tone}">
      <div><span>Actual EV source status</span><h2>{html.escape(title)}</h2></div>
      <p>{html.escape(body)} Configured source: <strong>{html.escape(configured)}</strong>.</p>
    </section>
    """


def ev_page_html(summary: Summary, settings: Settings) -> str:
    warning = ""
    if not summary.ev_session_analytics.get("available"):
        warning_title = (
            "No completed charger sessions imported for today yet"
            if summary.start == summary.end == local_today()
            else "Actual session data unavailable"
        )
        warning_body = (
            "Ocular may only export completed sessions, so today's active charge may appear after the next export. PowerMonitor is using the overnight fallback estimate where applicable."
            if summary.start == summary.end == local_today()
            else EV_ESTIMATE_CAVEAT
        )
        warning = f"""
        <section class="auth-alert">
          <div class="auth-alert-heading"><div><span>EV data warning</span><h2>{html.escape(warning_title)}</h2></div></div>
          <p>{html.escape(warning_body)}</p>
          <ol>
            <li>Export all charging history from the Ocular app.</li>
            <li>Run <code>python3 collector/import_ocular_export.py /path/to/export.xlsx</code>.</li>
            <li>Run <code>python3 collector/report.py</code>.</li>
          </ol>
        </section>
        """
    return f"""
    {ev_source_status_html(summary, settings)}
    {actual_ev_card_html(summary, settings)}
    {ev_session_analytics_html(summary, settings)}
    {warning}
    """


def ev_source_counts() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        engine = data_engine()
        engine.validate_database()
        return engine.get_ev_source_counts()
    except sqlite3.Error:
        return []


def data_completeness(summary: Summary, settings: Settings) -> dict:
    confidence, confidence_note = confidence_label(summary)
    solar_present = summary.solar_generated_kwh > 0 or summary.solar_used_kwh > 0 or summary.solar_exported_kwh > 0
    grid_present = summary.intervals > 0
    ev_actual_present = summary.ev_charging_source in {"ocular_export", "home_assistant_ocpp"}
    billing_configured = bool(settings.current_billing_period_start)
    items = [
        {
            "name": "Solar data",
            "status": "present" if solar_present else "missing",
            "detail": kwh(summary.solar_generated_kwh) if solar_present else "No solar production/export values in selected period",
        },
        {
            "name": "Grid data",
            "status": "present" if grid_present else "missing",
            "detail": f"{summary.intervals} intervals, {kwh(summary.grid_import_kwh)} imported" if grid_present else "No interval readings in selected period",
        },
        {
            "name": "EV actual sessions",
            "status": "present" if ev_actual_present else "missing",
            "detail": ev_note(settings, summary) if ev_actual_present else "Using estimated overnight EV fallback",
        },
        {
            "name": "Billing period",
            "status": "configured" if billing_configured else "missing",
            "detail": settings.current_billing_period_start.isoformat() if billing_configured else "Using calendar fallback",
        },
    ]
    return {
        "confidence": confidence,
        "confidence_note": confidence_note,
        "items": items,
    }


def current_knowledge_items(summary: Summary, settings: Settings) -> list[dict[str, str]]:
    completeness = data_completeness(summary, settings)
    by_name = {str(item["name"]): item for item in completeness["items"]}
    solar_known = by_name.get("Solar data", {}).get("status") == "present"
    grid_known = by_name.get("Grid data", {}).get("status") == "present"
    ev_known = by_name.get("EV actual sessions", {}).get("status") == "present"
    billing_known = by_name.get("Billing period", {}).get("status") == "configured"
    tariff_known = bool(getattr(settings, "import_rates", None) or getattr(settings, "current_plan", None))
    return [
        {
            "name": "Solar generation",
            "known": "yes" if solar_known else "no",
            "effect": "Helps explain solar use, exports, and whether daytime advice can be trusted.",
        },
        {
            "name": "Grid usage",
            "known": "yes" if grid_known else "no",
            "effect": "Helps explain household consumption, peak exposure, and bill movement.",
        },
        {
            "name": "EV charging",
            "known": "yes" if ev_known else "no",
            "effect": "Helps separate car charging from normal household usage and estimate charging costs.",
        },
        {
            "name": "Billing cycle",
            "known": "yes" if billing_known else "no",
            "effect": "Helps compare today against the right point in the current bill.",
        },
        {
            "name": "Tariff",
            "known": "yes" if tariff_known else "no",
            "effect": "Helps identify expensive and cheaper electricity windows.",
        },
        {
            "name": "Weather",
            "known": "no",
            "effect": "Would help explain unusual solar production on cloudy or sunny days.",
        },
        {
            "name": "Battery",
            "known": "no",
            "effect": "Would help explain stored energy, discharge, and backup behaviour.",
        },
        {
            "name": "Live inverter telemetry",
            "known": "no",
            "effect": "Would help verify right-now solar surplus instead of relying on recent history.",
        },
    ]


def data_completeness_html(summary: Summary, settings: Settings) -> str:
    completeness = data_completeness(summary, settings)
    rows = []
    for item in current_knowledge_items(summary, settings):
        known = item["known"] == "yes"
        tone = "good" if known else "neutral"
        status = "Known" if known else "Not set up"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{'✔' if known else '○'} {html.escape(item["name"])}</span>
              <strong>{html.escape(status)}</strong>
              <small>{html.escape(item["effect"])}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>What PowerMonitor currently knows</h3><span>{html.escape(homeowner_confidence_label(completeness["confidence"]))}</span></div>
      <p class="scenario-note">{html.escape(completeness["confidence_note"])}</p>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def homeowner_confidence_label(confidence: str) -> str:
    if str(confidence).lower() == "high":
        return "High confidence"
    if str(confidence).lower() == "medium":
        return "Medium confidence"
    return "Limited confidence"


def capability_summary_html(capability_summary) -> str:
    if not capability_summary:
        return ""
    rows = []
    for capability in capability_summary.capabilities:
        state = "Ready" if capability.enabled else "Not detected"
        rows.append(
            f"""
            <div class="completeness-row {'good' if capability.enabled else 'bad'}">
              <span>{html.escape(capability.label)}</span>
              <strong>{html.escape(state)}</strong>
              <small>{html.escape(capability.reason)}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Capability summary</h3><span>Household scoped</span></div>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def provider_registry_html(provider_registry: Optional[ProviderRegistry]) -> str:
    if not provider_registry:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Available providers</h3><span>Not loaded</span></div>
      <p class="notice">Provider guidance is not available in this report. Check Settings again after the dashboard refreshes.</p>
    </div>
    """
    rows = []
    for provider in provider_registry.providers:
        capabilities = ", ".join(capability.value.replace("_", " ") for capability in provider.capabilities)
        rows.append(
            f"""
            <div id="system-provider-registry-{html.escape(provider.provider_id)}" class="completeness-row {'good' if provider.available else 'bad'}">
              <span>{html.escape(provider.name)}</span>
              <strong>{html.escape(provider.status.value.title())}</strong>
              <small>{html.escape(provider.provider_type.value.replace("_", " ").title())}: {html.escape(capabilities or "No capabilities")}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Provider registry</h3><span>{len(provider_registry.providers)} providers</span></div>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def _provider_onboarding_fallback(data: dict) -> dict:
    lifecycle = str(data.get("lifecycle_state") or "NOT_CONFIGURED")
    diagnostics = data.get("diagnostics", {}) or {}
    capabilities = data.get("capability_summary", {}) or {}
    healthy = lifecycle in {"CONNECTED", "SYNCING", "CONFIGURED"}
    return {
        "state": lifecycle,
        "state_label": friendly_label(lifecycle),
        "connected": healthy,
        "healthy": healthy,
        "data_available": bool(capabilities.get("detected") or diagnostics.get("observation_count") or diagnostics.get("last_successful_sync")),
        "last_successful_sync": diagnostics.get("last_successful_sync"),
        "next_scheduled_sync": "Next data refresh",
        "sync_frequency": "Refresh schedule",
        "required_information": ["Provider configuration details"],
        "why_required": "Provider details let PowerMonitor check access and explain which data is safe to use.",
        "connection_status": "Connected and available for analysis." if healthy else "Waiting for setup.",
        "health_status": trust_wording(friendly_label((data.get("health") or {}).get("collector_status") or "unknown")),
        "next_step": "No action needed right now." if healthy else "Reconnect live data when convenient. Until then PowerMonitor uses tariff timing and stored readings.",
        "common_failures": ["Live data sign-in needs attention", "Provider service unavailable", "No recent data returned"],
        "recovery_steps": ["Check the required information", "Reconnect live data", "Run Test Connection"],
        "technical_details": {
            "lifecycle_state": lifecycle,
            "sync_state": data.get("sync_state"),
            "live_data_status": (data.get("health") or {}).get("collector_status"),
        },
    }


def _list_items(items: list[str]) -> str:
    return "".join(f"<li>{html.escape(trust_wording(item))}</li>" for item in items)


def _technical_detail_rows(details: dict) -> str:
    return "".join(
        f"<div><span>{html.escape(friendly_label(str(key)))}</span><strong>{html.escape(trust_wording(value if value is not None else 'None'))}</strong></div>"
        for key, value in details.items()
    )


def _provider_entity_mapping_html(mapping: Optional[dict]) -> str:
    if not mapping:
        return ""
    validation = mapping.get("validation") or {}
    diagnostics = mapping.get("diagnostics") or []
    targets = {
        str(target.get("target_id")): target
        for target in mapping.get("targets", [])
        if isinstance(target, dict)
    }
    selected = mapping.get("selected_mappings") or {}
    recommended = mapping.get("recommended_mappings") or {}
    if not selected and not recommended:
        return """
          <p class="notice">Run Test Connection to discover Home Assistant entities and generate recommended mappings.</p>
        """
    rows = []
    for target_id, entity_id in selected.items():
        target = targets.get(str(target_id), {})
        rows.append(
            f"""
            <div>
              <span>{html.escape(str(target.get("label") or friendly_label(str(target_id))))}</span>
              <strong>{html.escape(str(entity_id))}</strong>
              <small>Status: {html.escape(str((validation.get("target_status") or {}).get(target_id, "selected")).replace("_", " ").title())}</small>
            </div>
            """
        )
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        rows.append(
            f"""
            <div>
              <span>{html.escape(str(diagnostic.get("target_label") or diagnostic.get("target_id") or "Entity"))}</span>
              <strong>{html.escape(str(diagnostic.get("selected_entity") or "Not selected"))}</strong>
              <small>{html.escape(str(diagnostic.get("availability") or "unknown").title())}; state {html.escape(str(diagnostic.get("entity_state") or "unknown"))}; unit {html.escape(str(diagnostic.get("unit") or "none"))}; last update {html.escape(str(diagnostic.get("last_update_time") or "unknown"))}.</small>
            </div>
            """
        )
    warning_rows = _list_items([str(item) for item in validation.get("warnings", [])])
    return f"""
      <div class="provider-guidance-grid">
        <div><span>Validation</span><strong>{html.escape(str(validation.get("status") or "unknown").title())}</strong></div>
        <div><span>Reset option</span><strong>Use recommended mappings</strong></div>
      </div>
      <div class="config-grid">{''.join(rows) or '<p class="notice">No entities selected yet.</p>'}</div>
      {f'<ul>{warning_rows}</ul>' if warning_rows else ''}
    """


def _provider_action_confirm(action: str, provider_name: str) -> str:
    messages = {
        "reconnect": f"Reconnect {provider_name} now? PowerMonitor will test the saved connection details.",
        "disable": f"Disable {provider_name}? Existing data stays available, but PowerMonitor will stop relying on new refreshes from this provider.",
        "enable": f"Enable {provider_name}? Run Test Connection afterwards before relying on new data.",
        "remove": f"Remove {provider_name} from Provider Management? Saved local connection details for this provider will be cleared where possible.",
        "reset": f"Reset {provider_name} to recommended local defaults?",
    }
    return messages.get(action, "")


def _provider_configuration_form_html(data: dict, actions: Optional[list[tuple[str, str, str]]] = None, *, hide_credentials: bool = False) -> str:
    provider_id = str(data.get("provider_id") or "")
    provider_action_base = f"/api/providers/{html.escape(provider_id)}"
    provider_name = _source_display_name(provider_id, data)
    configuration = data.get("configuration") or {}
    credentials = configuration.get("credentials") or []
    fields = []
    for credential in credentials:
        if not isinstance(credential, dict):
            continue
        field = str(credential.get("field") or "")
        if not field:
            continue
        label = str(credential.get("label") or friendly_label(field))
        sensitive = bool(credential.get("sensitive"))
        required = bool(credential.get("required"))
        configured = bool(credential.get("configured"))
        value = "" if sensitive else str(credential.get("value") or "")
        placeholder = "Already saved; paste a new value to replace it" if sensitive and configured else "Required" if required else "Optional"
        input_type = "password" if sensitive else "url" if field == "endpoint" else "text"
        fields.append(
            f"""
            <label>
              <span>{html.escape(label)}{' *' if required else ''}</span>
              <input type="{input_type}" name="{html.escape(field)}" value="{html.escape(value)}" placeholder="{html.escape(placeholder)}" autocomplete="off">
              <small>{html.escape('Configured' if configured else 'Not configured')}</small>
            </label>
            """
        )
    mapping = configuration.get("entity_mapping") or {}
    mapping_fields = []
    for target in mapping.get("targets", []):
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("target_id") or "")
        if not target_id:
            continue
        selected = str((mapping.get("selected_mappings") or {}).get(target_id) or "")
        recommended = str((mapping.get("recommended_mappings") or {}).get(target_id) or "")
        mapping_fields.append(
            f"""
            <label>
              <span>{html.escape(str(target.get("label") or friendly_label(target_id)))}</span>
              <input type="text" name="entity_mappings.{html.escape(target_id)}" value="{html.escape(selected)}" placeholder="{html.escape(recommended or 'Run Test Connection for recommendations')}">
              <small>{html.escape(str(target.get("category") or '').replace('_', ' ').title())}</small>
            </label>
            """
        )
    mapping_html = ""
    if mapping_fields:
        mapping_html = f"""
          <div class="provider-form-section">
            <span>Home Assistant entity mappings</span>
            <div class="provider-form-grid">{''.join(mapping_fields)}</div>
          </div>
        """
    action_specs = actions or [
        ("Save", "save", "submit"),
        ("Test Connection", "test", "button"),
        ("Reconnect", "reconnect", "button"),
        ("Reset", "reset", "button"),
        ("Disable", "disable", "button"),
        ("Enable", "enable", "button"),
        ("Remove", "remove", "button"),
    ]
    action_buttons = []
    for label, action, button_type in action_specs:
        confirm = _provider_action_confirm(action, provider_name)
        confirm_attr = f' data-confirm="{html.escape(confirm)}"' if confirm else ""
        action_buttons.append(
            f'<button type="submit" name="provider_action" value="{html.escape(action)}" formaction="{provider_action_base}/{html.escape(action)}" formmethod="post" data-provider-action="{html.escape(action)}"{confirm_attr}>{html.escape(label)}</button>'
        )
    action_buttons_html = "".join(action_buttons)
    fields_html = ''.join(fields) or '<p class="notice">No editable connection fields for this provider.</p>'
    if hide_credentials:
        connection_details = f"""
        <div class="provider-account-summary">
          <div><span>Connected account</span><strong>Saved locally</strong></div>
          <div><span>Credentials</span><strong>Hidden for privacy</strong></div>
        </div>
        <details class="credential-editor">
          <summary>Edit credentials</summary>
          <div class="provider-form-grid">{fields_html}</div>
          <div class="provider-action-row credential-save-row">
            <button type="submit" name="provider_action" value="save" formaction="{provider_action_base}/save" formmethod="post" data-provider-action="save">Save credentials</button>
          </div>
        </details>
        """
    else:
        connection_details = f"""
        <div class="provider-form-section">
          <span>Connection details</span>
          <div class="provider-form-grid">{fields_html}</div>
        </div>
        """
    return f"""
      <form class="provider-config-form" data-provider-id="{html.escape(provider_id)}" method="post" action="{provider_action_base}/save">
        <input type="hidden" name="provider_id" value="{html.escape(provider_id)}">
        {connection_details}
        {mapping_html}
        <div class="provider-action-row">
          {action_buttons_html}
        </div>
        <div class="provider-action-result" aria-live="polite"></div>
      </form>
    """


def _provider_is_configured_or_available(data: dict, onboarding: dict) -> bool:
    state = str(data.get("lifecycle_state") or "")
    return bool(
        onboarding.get("connected")
        or onboarding.get("data_available")
        or state in {"CONFIGURED", "CONNECTED", "SYNCING", "DEGRADED", "ERROR", "DISABLED"}
    )


def _is_future_optional_source(provider_id: str, data: dict, onboarding: dict) -> bool:
    state = str(data.get("lifecycle_state") or "")
    return state == "NOT_CONFIGURED" and provider_id in {"tesla", "amber", "fronius", "enphase"}


def _should_show_data_source(provider_id: str, data: dict, onboarding: dict) -> bool:
    if provider_id == "smart_meter_readings":
        return False
    if _is_future_optional_source(provider_id, data, onboarding):
        return False
    return _provider_is_configured_or_available(data, onboarding)


def _should_show_provider_management_card(provider_id: str, data: dict, onboarding: dict) -> bool:
    if provider_id == "smart_meter_readings":
        return _provider_is_configured_or_available(data, onboarding)
    if _provider_is_configured_or_available(data, onboarding):
        return True
    if provider_id in {"solaredge", "ocular_export", "synergy_tariff_config", "home_assistant"}:
        return True
    if _is_future_optional_source(provider_id, data, onboarding):
        return True
    return False


def _source_kind(provider_id: str, data: dict) -> str:
    provider_type = str(data.get("provider_type") or "")
    capabilities = set((data.get("capability_summary") or {}).get("capabilities") or [])
    if provider_type == "retailer" or provider_id == "synergy_tariff_config":
        return "configuration"
    if "import_only" in capabilities or provider_id == "ocular_export":
        return "import"
    return "live"


def _data_source_title(provider_id: str, data: dict) -> str:
    if provider_id == "solaredge":
        return "Solar generation"
    if provider_id == "ocular_export":
        return "EV charging history"
    if provider_id == "synergy_tariff_config":
        return "Electricity tariff"
    if provider_id == "home_assistant":
        return "Home energy sensors"
    if provider_id == "home_assistant_ocpp":
        return "EV charger"
    name = str(data.get("name") or provider_id).replace("_", " ").title()
    return name


def _source_display_name(provider_id: str, data: dict) -> str:
    if provider_id == "solaredge":
        return "SolarEdge"
    if provider_id == "ocular_export":
        return "Ocular"
    if provider_id == "synergy_tariff_config":
        return "Synergy"
    if provider_id in {"home_assistant", "home_assistant_ocpp"}:
        return "Home Assistant"
    if provider_id == "fronius":
        return "Fronius"
    if provider_id == "enphase":
        return "Enphase"
    if provider_id == "tesla":
        return "Tesla"
    if provider_id == "amber":
        return "Amber"
    return str(data.get("name") or provider_id).replace("_", " ").title()


def _data_source_provides(provider_id: str, data: dict) -> list[str]:
    if provider_id == "solaredge":
        return ["Solar generation", "Solar export", "Daily production"]
    if provider_id == "ocular_export":
        return ["EV charging history", "Charging energy", "Charging costs"]
    if provider_id == "synergy_tariff_config":
        return ["Electricity tariff", "Billing cycle", "Plan comparison"]
    if provider_id in {"home_assistant", "home_assistant_ocpp"}:
        return ["Local energy sensors", "EV charger status", "Household readings"]
    provider_type = str(data.get("provider_type") or "")
    if provider_type == "solar":
        return ["Solar generation", "Solar export", "Daily production"]
    if provider_type == "ev_charger":
        return ["EV charging history", "Charging energy", "Charging costs"]
    if provider_type == "retailer":
        return ["Electricity tariff", "Billing details", "Plan comparison"]
    return ["Household energy information"]


def _data_source_status(kind: str, data: dict, onboarding: dict) -> str:
    state = str(data.get("lifecycle_state") or onboarding.get("state") or "")
    health_status = str(onboarding.get("health_status") or (data.get("health") or {}).get("last_error") or "")
    if kind == "configuration":
        return "Connected" if state in {"CONFIGURED", "CONNECTED", "SYNCING"} else "Needs attention"
    if kind == "import":
        return "Latest upload available" if onboarding.get("data_available") else "Not connected"
    if onboarding.get("connected"):
        return "Connected"
    if "signed in" in health_status.lower() and "no recent solar data" in health_status.lower():
        return "Signed in, but no data received"
    if onboarding.get("data_available") and state in {"CONFIGURED", "NOT_CONFIGURED"}:
        return "Historical data only"
    if state in {"ERROR", "DEGRADED"}:
        return "Needs attention"
    if state in {"CONFIGURED"}:
        return "Waiting for setup"
    return "Not connected"


def _provider_management_status(status: str, data: dict) -> str:
    state = str(data.get("lifecycle_state") or "")
    if status in {"Connected", "Latest upload available", "Historical data only"}:
        return "Connected"
    if status in {"Needs attention", "Signed in, but no data received"}:
        return "Warning"
    if state in {"DEGRADED", "ERROR"}:
        return "Warning"
    return "Disconnected"


def _provider_connection_type(kind: str, data: dict) -> str:
    provider_id = str(data.get("provider_id") or "")
    if kind == "import":
        return "File Import"
    if kind == "configuration" or provider_id == "synergy_tariff_config":
        return "Manual"
    return "Live API"


def _provider_version_label(data: dict) -> str:
    diagnostics = data.get("diagnostics") or {}
    version = diagnostics.get("collector_version")
    if version:
        return str(version)
    if str(data.get("lifecycle_state") or "") == "NOT_CONFIGURED":
        return "Not installed"
    return "Provider framework v1"


def _provider_last_error_label(data: dict, onboarding: dict) -> str:
    diagnostics = data.get("diagnostics") or {}
    health = data.get("health") or {}
    error = diagnostics.get("last_error") or health.get("last_error")
    if error:
        return trust_wording(error)
    health_status = str(onboarding.get("health_status") or "")
    if "error" in health_status.lower() or "attention" in health_status.lower() or "failed" in health_status.lower():
        return trust_wording(health_status)
    return "No recent error"


def _data_source_tone(status: str) -> str:
    if status in {"Connected", "Latest upload available"}:
        return "good"
    if status in {"Needs attention", "Signed in, but no data received"}:
        return "bad"
    return "neutral"


def _format_source_timestamp(value: Any, fallback: str = "Not available yet") -> str:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        local = parsed.astimezone(LOCAL_TIMEZONE)
        return local.strftime("%-d %b, %-I:%M %p")
    except (TypeError, ValueError):
        return str(value)


def _provider_data_available_label(provider_id: str, data: dict, onboarding: dict) -> str:
    freshness = ((data.get("diagnostics") or {}).get("freshness") or {})
    latest = freshness.get("observed_at") or onboarding.get("last_successful_sync")
    if provider_id == "solaredge" and latest:
        return f"Solar history available. Latest solar reading: {_format_source_timestamp(latest)}."
    if onboarding.get("data_available"):
        return "Household history available."
    return "No usable live data received yet."


def _provider_next_refresh_label(kind: str, onboarding: dict) -> str:
    if kind == "import":
        return "Manual upload"
    return _format_source_timestamp(onboarding.get("next_scheduled_sync"), str(onboarding.get("next_scheduled_sync") or "Next scheduled refresh"))


def _provider_has_enough_configuration(data: dict) -> bool:
    credentials = ((data.get("configuration") or {}).get("credentials") or [])
    configured = {str(item.get("field")) for item in credentials if isinstance(item, dict) and item.get("configured")}
    provider_id = str(data.get("provider_id") or "")
    if provider_id == "solaredge":
        return {"username", "password"}.issubset(configured) or "cookie" in configured
    return bool(configured)


def _data_source_actions_html(kind: str, data: dict, status: str) -> str:
    provider_id = str(data.get("provider_id") or "")
    diagnostics_link = f'<a class="button-link muted" href="/api/providers/{html.escape(provider_id)}/diagnostics">View diagnostics</a>'
    historical_link = (
        '<a class="button-link" href="#historical-coverage">Historical data</a>'
        if provider_id == "solaredge" else ""
    )
    if kind == "import":
        return f"""
          <div class="provider-action-row">
            <a class="button-link" href="#imports">Upload latest file</a>
            <a class="button-link" href="#imports">Replace upload</a>
            <a class="button-link muted" href="#imports">Remove imported history</a>
            {diagnostics_link}
          </div>
        """
    if kind == "configuration":
        return f"""
          <div class="provider-action-row">
            <a class="button-link" href="#setup-billing">Edit configuration</a>
            {diagnostics_link}
          </div>
        """
    if str(data.get("lifecycle_state") or "") == "DISABLED":
        return _provider_configuration_form_html(
            data,
            [("Enable", "enable", "submit"), ("Remove", "remove", "submit")],
            hide_credentials=True,
        ) + f'<div class="provider-action-row provider-diagnostics-row">{diagnostics_link}</div>'
    if status == "Connected":
        live_actions = [("Test connection", "test", "submit"), ("Reconnect", "reconnect", "submit"), ("Disable", "disable", "submit"), ("Remove", "remove", "submit")]
        return _provider_configuration_form_html(data, live_actions, hide_credentials=True) + f'<div class="provider-action-row provider-diagnostics-row">{historical_link}{diagnostics_link}</div>'
    live_actions = [("Add provider", "save", "submit")]
    if _provider_has_enough_configuration(data):
        live_actions.append(("Test connection", "test", "submit"))
    if status in {"Needs attention", "Signed in, but no data received", "Historical data only", "Waiting for setup"}:
        live_actions.append(("Reconnect", "reconnect", "submit"))
        live_actions.append(("Disable", "disable", "submit"))
    live_actions.append(("Remove", "remove", "submit"))
    return _provider_configuration_form_html(data, live_actions) + f'<div class="provider-action-row provider-diagnostics-row">{historical_link}{diagnostics_link}</div>'


def _provider_flash_html(provider_id: str, provider_flash: Optional[dict]) -> str:
    if not provider_flash or str(provider_flash.get("provider_id") or "") != provider_id:
        return ""
    status = str(provider_flash.get("status") or "success").lower()
    if status not in {"success", "warning", "failure"}:
        status = "warning"
    action = friendly_label(str(provider_flash.get("action") or "provider action"))
    message = str(provider_flash.get("message") or "Provider action completed.")
    next_step = str(provider_flash.get("next_step") or "Review provider status.")
    return f"""
      <div class="provider-action-result visible {html.escape(status)} provider-flash" role="status">
        <span>{html.escape(action)}</span>
        <strong>{html.escape(message)}</strong>
        <small>{html.escape(next_step)}</small>
      </div>
    """


def _data_source_card_html(provider, data: dict, onboarding: dict, provider_flash: Optional[dict] = None) -> str:
    provider_id = str(data.get("provider_id") or provider.provider_id)
    kind = _source_kind(provider_id, data)
    status = _data_source_status(kind, data, onboarding)
    provides = "".join(f"<li>{html.escape(item)}</li>" for item in _data_source_provides(provider_id, data))
    last_updated = _format_source_timestamp(onboarding.get("last_successful_sync"), "Not updated yet")
    if kind == "configuration":
        last_updated = "Configured" if status == "Connected" else "Needs setup"
    elif kind == "import" and status == "Latest upload available":
        last_updated = _format_source_timestamp(onboarding.get("last_successful_sync"), "Uploaded previously")
    next_refresh = _provider_next_refresh_label(kind, onboarding)
    data_available = _provider_data_available_label(provider_id, data, onboarding)
    action_needed = str(onboarding.get("next_step") or "No action needed right now.")
    management_status = _provider_management_status(status, data)
    connection_type = _provider_connection_type(kind, data)
    provider_version = _provider_version_label(data)
    last_error = _provider_last_error_label(data, onboarding)
    setup_body = f"""
      <div class="provider-guidance-grid">
        <div><span>What this needs</span><strong>{html.escape(', '.join(onboarding.get("required_information") or ["Connection details"]))}</strong></div>
        <div><span>Why it matters</span><strong>{html.escape(trust_wording(onboarding.get("why_required") or ""))}</strong></div>
        <div><span>If something looks wrong</span><ul>{_list_items(onboarding.get("recovery_steps") or [])}</ul></div>
      </div>
    """
    mapping_body = _provider_entity_mapping_html((data.get("configuration") or {}).get("entity_mapping") or (data.get("diagnostics") or {}).get("entity_mapping"))
    technical_body = f"""
      <div class="config-grid">{_technical_detail_rows(onboarding.get("technical_details") or {})}</div>
      <div class="config-grid">
        <div><span>Lifecycle</span><strong>{html.escape(str(data.get("lifecycle_state") or "unknown"))}</strong></div>
        <div><span>Sync state</span><strong>{html.escape(str(data.get("sync_state") or "unknown"))}</strong></div>
        <div><span>Runtime state</span><strong>{html.escape(friendly_label(str(onboarding.get("runtime_state") or "Unknown")))}</strong></div>
        <div><span>Observation count</span><strong>{html.escape(str((data.get("diagnostics") or {}).get("observation_count", 0)))}</strong></div>
        <div><span>Promotion count</span><strong>{html.escape(str((data.get("diagnostics") or {}).get("promotion_count", 0)))}</strong></div>
      </div>
    """
    return f"""
    <div id="system-provider-{html.escape(provider_id)}" class="provider-status-card {_data_source_tone(status)} data-source-card">
      <div class="provider-status-head">
        <span>{html.escape(_data_source_title(provider_id, data))}</span>
        <strong>{html.escape(management_status)}</strong>
      </div>
      <p>{html.escape(trust_wording(onboarding.get("why_required") or data.get("note") or ""))}</p>
      <div class="provider-status-grid data-source-grid">
        <div><span>Source</span><strong>{html.escape(_source_display_name(provider_id, data))}</strong></div>
        <div><span>Status</span><strong>{html.escape(management_status)}</strong></div>
        <div><span>Connection type</span><strong>{html.escape(connection_type)}</strong></div>
        <div><span>Last successful refresh</span><strong>{html.escape(last_updated)}</strong></div>
        <div><span>Next scheduled refresh</span><strong>{html.escape(next_refresh)}</strong></div>
        <div><span>Last error</span><strong>{html.escape(last_error)}</strong></div>
        <div><span>Provider version</span><strong>{html.escape(provider_version)}</strong></div>
        <div><span>Data available</span><strong>{html.escape(data_available)}</strong></div>
        <div><span>Action needed</span><strong>{html.escape(trust_wording(action_needed))}</strong></div>
      </div>
      {_provider_flash_html(provider_id, provider_flash)}
      <div class="source-provides"><span>Provides</span><ul>{provides}</ul></div>
      {_data_source_actions_html(kind, data, status)}
      {disclosure_html("Setup help", "What this source needs and why", setup_body, css_class="provider-details")}
      {disclosure_html("Entity mapping", "Selected entities, recommendations, validation, and availability", mapping_body, css_class="provider-details") if mapping_body and kind == "live" else ""}
      {disclosure_html("Advanced Diagnostics", "Technical source details for troubleshooting", technical_body, css_class="provider-details advanced-diagnostics")}
    </div>
    """


def provider_management_html(provider_management: Optional[ProviderManager], provider_flash: Optional[dict] = None) -> str:
    if not provider_management:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Provider Management</h3><span>Unavailable</span></div>
      <p class="notice">PowerMonitor cannot load provider guidance right now. Existing readings can still be shown.</p>
    </div>
    """
    rows = []
    for provider in provider_management.providers():
        data = provider.as_dict()
        onboarding = data.get("onboarding") or _provider_onboarding_fallback(data)
        if not _should_show_provider_management_card(str(data.get("provider_id") or provider.provider_id), data, onboarding):
            continue
        rows.append(_data_source_card_html(provider, data, onboarding, provider_flash))
    connected = 0
    warnings = 0
    for provider in provider_management.providers():
        data = provider.as_dict()
        onboarding = data.get("onboarding") or _provider_onboarding_fallback(data)
        if not _should_show_provider_management_card(str(data.get("provider_id") or provider.provider_id), data, onboarding):
            continue
        status = _provider_management_status(_data_source_status(_source_kind(str(data.get("provider_id") or provider.provider_id), data), data, onboarding), data)
        if status == "Connected":
            connected += 1
        elif status == "Warning":
            warnings += 1
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Provider Management</h3><span>{connected} connected / {warnings} warnings</span></div>
      <p class="scenario-note">Manage household integrations here without editing configuration files. Add providers, test connections, reconnect, disable, remove, or inspect diagnostics from the same provider-agnostic workflow.</p>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No household data sources are connected yet.</p>'}</div>
    </div>
    """


def _parse_iso_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def historical_backfill_job_snapshot(job: dict, *, now: Optional[datetime] = None) -> dict:
    progress = job.get("progress") or {}
    completed_days = list(progress.get("completed_days") or [])
    failed_days = list(progress.get("failed_days") or [])
    total_days = max(int(progress.get("chunks_total") or 0), 0)
    completed_count = max(int(progress.get("chunks_completed") or 0), 0)
    remaining_days = max(total_days - completed_count, 0)
    percentage_complete = min(100.0, (completed_count / total_days) * 100.0) if total_days else 0.0
    created_at = _parse_iso_datetime(job.get("created_at"))
    updated_at = _parse_iso_datetime(job.get("updated_at"))
    now_dt = now or datetime.now().astimezone()
    estimated_finish_time = None
    if (
        updated_at
        and created_at
        and updated_at >= created_at
        and completed_count > 0
        and remaining_days > 0
        and str(job.get("status") or "").lower() in {"queued", "running", "retrying", "cancelling"}
    ):
        elapsed = (updated_at - created_at).total_seconds()
        if elapsed > 0:
            seconds_per_day = elapsed / completed_count
            if seconds_per_day > 0:
                eta = updated_at + timedelta(seconds=seconds_per_day * remaining_days)
                if eta >= now_dt:
                    estimated_finish_time = eta.strftime("%d %b %Y, %H:%M")
    status = str(job.get("status") or "queued")
    terminal = status in {"complete", "cancelled", "failed"}
    provider_id = str(job.get("provider_id") or "solaredge")
    provider_label = {"solaredge": "SolarEdge"}.get(provider_id, provider_id.replace("_", " ").title())
    data_received = int(progress.get("data_received") or 0)
    imported_or_updated = int(progress.get("imported_or_updated") or 0)
    duplicates_skipped = int(progress.get("duplicates_skipped") or 0)
    completed_at = _parse_iso_datetime(job.get("completed_at"))
    summary_message = ""
    if status == "complete":
        summary_message = "Historical import complete"
    elif status == "failed":
        summary_message = "Historical import failed"
    elif status == "cancelled":
        summary_message = "Historical import cancelled"
    return {
        "job_id": str(job.get("job_id") or ""),
        "provider_id": provider_id,
        "provider": provider_label,
        "status": status,
        "status_text": status.replace("_", " ").title(),
        "start_date": str(job.get("start_date") or ""),
        "end_date": str(job.get("end_date") or ""),
        "requested_range": {
            "start": str(job.get("start_date") or ""),
            "end": str(job.get("end_date") or ""),
        },
        "current_day": progress.get("current_day"),
        "chunks_completed": completed_count,
        "chunks_total": total_days,
        "days_completed": completed_count,
        "total_days": total_days,
        "days_remaining": remaining_days,
        "percentage": round(percentage_complete, 1),
        "percentage_complete": round(percentage_complete, 1),
        "percentage_label": f"{percentage_complete:.0f}% complete" if total_days else "0% complete",
        "days_progress_label": f"{completed_count} / {total_days} days checked" if total_days else "No days queued yet",
        "data_received": data_received,
        "imported_or_updated": imported_or_updated,
        "readings_added": imported_or_updated,
        "duplicates_skipped": duplicates_skipped,
        "failed_day_count": len(failed_days),
        "failed_days_count": len(failed_days),
        "last_successful_day": completed_days[-1] if completed_days else None,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
        "started_time": created_at.strftime("%d %b %Y, %H:%M") if created_at else None,
        "updated_time": updated_at.strftime("%d %b %Y, %H:%M") if updated_at else None,
        "completed_time": completed_at.strftime("%d %b %Y, %H:%M") if completed_at else None,
        "estimated_finish": estimated_finish_time,
        "estimated_finish_time": estimated_finish_time,
        "cancel_requested": bool(job.get("cancel_requested")),
        "resumable": status in {"failed", "cancelled"},
        "cancellable": status in {"queued", "running", "retrying", "cancelling"},
        "terminal": terminal,
        "summary_message": summary_message,
    }


def historical_backfill_job_card_html(job: dict) -> str:
    snapshot = historical_backfill_job_snapshot(job)
    tone = "good" if snapshot["status"] == "complete" else "bad" if snapshot["status"] == "failed" else "neutral"
    current_day = (
        f"Current day: {snapshot['current_day']}"
        if snapshot["current_day"]
        else f"Last successful day: {snapshot['last_successful_day']}"
        if snapshot["last_successful_day"]
        else "Waiting for the next chunk."
    )
    retry_html = (
        f"<small>Failed or retried days: {snapshot['failed_days_count']}</small>"
        if snapshot["failed_days_count"]
        else ""
    )
    updated_html = f"<small>Updated {html.escape(str(snapshot['updated_time']))}</small>" if snapshot["updated_time"] else ""
    eta_html = (
        f"<small>Estimated finish: {html.escape(str(snapshot['estimated_finish_time']))}</small>"
        if snapshot["estimated_finish_time"]
        else ""
    )
    resume_action = (
        f'<form method="post" action="/api/historical-backfill/resume"><input type="hidden" name="job_id" value="{html.escape(snapshot["job_id"])}"><button type="submit">Resume</button></form>'
        if snapshot["resumable"]
        else ""
    )
    cancel_action = (
        f'<form method="post" action="/api/historical-backfill/cancel"><input type="hidden" name="job_id" value="{html.escape(snapshot["job_id"])}"><button type="submit">Cancel</button></form>'
        if snapshot["cancellable"]
        else ""
    )
    return f"""
        <div class="historical-job-card completeness-row {tone}" data-job-id="{html.escape(snapshot['job_id'])}" data-job-status="{html.escape(snapshot['status'])}" data-poll-active="{str(snapshot['status'] in {'queued', 'running', 'retrying', 'cancelling'}).lower()}">
          <div class="historical-job-header">
            <span>{html.escape(snapshot["provider"])} - {html.escape(snapshot["status_text"])}</span>
            <strong>{html.escape(snapshot["requested_range"]["start"])} to {html.escape(snapshot["requested_range"]["end"])}</strong>
          </div>
          <div class="historical-job-progress">
            <div class="historical-job-bar" aria-hidden="true"><span style="width:{snapshot['percentage_complete']:.1f}%"></span></div>
            <strong>{html.escape(snapshot["percentage_label"])}</strong>
          </div>
          <div class="historical-job-meta">
            <small>{html.escape(snapshot["days_progress_label"])}</small>
            <small>{html.escape(current_day)}</small>
            {retry_html}
            {updated_html}
            {eta_html}
          </div>
          <div class="provider-action-row">
            {resume_action}
            {cancel_action}
            <a class="button-link" href="/api/historical-backfill/diagnostics?job_id={html.escape(snapshot['job_id'])}">View diagnostics</a>
          </div>
        </div>
    """


def historical_coverage_html(data: Optional[dict], historical_backfill_flash: Optional[dict] = None) -> str:
    data = data or historical_coverage_data()
    coverage = data.get("historical_coverage") or {}
    seasonal = data.get("seasonal_history") or {}
    audit = data.get("solaredge_capability_audit") or {}
    providers = coverage.get("providers") or {}
    solaredge = providers.get("solaredge") or {}
    provider_sources = solaredge.get("sources") or {}
    source_rows = "".join(
        f"""
        <div class="completeness-row good">
          <span>{html.escape(str(source.get("resolution") or "interval data").replace("_", " "))}</span>
          <strong>{html.escape(str(source.get("days") or 0))} days available</strong>
          <small>{html.escape(str(source.get("first_reading") or "No first reading"))} to {html.escape(str(source.get("last_reading") or "No last reading"))}</small>
        </div>
        """
        for source in provider_sources.values()
    )
    if not source_rows:
        source_rows = '<p class="notice">No SolarEdge historical interval readings are stored yet.</p>'

    jobs = data.get("historical_backfill_jobs") or []
    job_rows = "".join(historical_backfill_job_card_html(job) for job in jobs[:5])
    if not job_rows:
        job_rows = '<p class="notice">No historical data backfill jobs have run yet.</p>'
    poll_active = any(str(job.get("status") or "").lower() in {"queued", "running", "retrying", "cancelling"} for job in jobs[:5])
    flash_html = historical_backfill_flash_html(historical_backfill_flash)

    range_options = "".join(
        f'<option value="{html.escape(key)}"{" selected" if key == "previous_summer" else ""}>{html.escape(label)}</option>'
        for key, label in RANGE_PRESETS.items()
    )
    seasonal_range = seasonal.get("range") or {}
    existing = seasonal.get("existing_coverage") or {}
    gaps = seasonal.get("gaps") or {}
    warning_rows = "".join(f"<li>{html.escape(str(item))}</li>" for item in seasonal.get("warnings") or [])
    quality_rows = "".join(
        f"<li><strong>{html.escape(key.replace('_', ' ').title())}:</strong> {html.escape(str(value))}</li>"
        for key, value in (coverage.get("quality_tiers") or {}).items()
    )
    return f"""
    <section id="historical-coverage" class="report-section">
      {page_heading_html("Historical data", "See which past readings can support seasonal context and battery modelling.", parent="Settings")}
      {flash_html}
      <div id="historical-seasonal-readiness" class="system-card">
        <div class="chart-heading"><h3>Previous summer readiness</h3><span>{html.escape(str(seasonal_range.get("start") or ""))} to {html.escape(str(seasonal_range.get("end") or ""))}</span></div>
        <div class="config-grid">
          <div><span>Days with readings</span><strong>{html.escape(str(existing.get("days_with_any_reading", 0)))} / {html.escape(str(gaps.get("days_in_range", seasonal_range.get("days", 0))))}</strong></div>
          <div><span>Battery-ready days</span><strong>{html.escape(str(existing.get("battery_suitable_days", 0)))}</strong></div>
          <div><span>Resolution</span><strong>{html.escape(str(seasonal.get("resolution") or "interval_15m").replace("_", " "))}</strong></div>
          <div><span>Confidence</span><strong>{html.escape(str(seasonal.get("quality_tier") or "interval_grade").replace("_", " ").title())}</strong></div>
        </div>
        <p class="scenario-note">Battery modelling only replays days with interval import/export readings. Daily, monthly, or lifetime totals are useful context, but they are not enough for tariff-aware battery dispatch.</p>
      </div>
      <div class="system-card">
        <div class="chart-heading"><h3>Start historical import</h3><span>SolarEdge</span></div>
        <form class="setup-config-form" method="post" action="/api/historical-backfill/start">
          <input type="hidden" name="provider_id" value="solaredge">
          <label><span>Date range</span><select name="range_preset">{range_options}</select></label>
          <label><span>Custom start</span><input type="date" name="start"></label>
          <label><span>Custom end</span><input type="date" name="end"></label>
          <button type="submit">Start import</button>
        </form>
        <p class="scenario-note">Previous summer is the Australian summer from 1 December to 31 March most recently completed. Custom ranges are also supported.</p>
      </div>
      <div id="historical-stored-coverage" class="system-card">
        <div class="chart-heading"><h3>Stored coverage</h3><span>By source</span></div>
        <div class="completeness-grid historical-coverage-sources">{source_rows}</div>
      </div>
      <div class="system-card">
        <div class="chart-heading"><h3>Historical import jobs</h3><span>Progress and resume status</span></div>
        <div id="historical-backfill-jobs" class="completeness-grid historical-job-list" data-endpoint="/api/historical-backfill/status" data-poll-active="{str(poll_active).lower()}" data-poll-interval-ms="5000">{job_rows}</div>
      </div>
      {disclosure_html("SolarEdge historical capability", "Resolution, limits, and battery suitability", f'<div class="config-grid"><div><span>Source</span><strong>{html.escape(str(audit.get("source") or "SolarEdge"))}</strong></div><div><span>Chunk size</span><strong>{html.escape(str(audit.get("chunking") or "One day"))}</strong></div><div><span>Recent reliable window</span><strong>{html.escape(str((audit.get("reliable_recent_window") or {}).get("start") or ""))} to {html.escape(str((audit.get("reliable_recent_window") or {}).get("end") or ""))}</strong></div><div><span>Battery suitability</span><strong>{html.escape(str(audit.get("battery_dispatch_suitability") or ""))}</strong></div></div><ul class="guidance-list">{warning_rows}</ul><ul class="guidance-list">{quality_rows}</ul>', css_class="provider-details")}
    </section>
    """


def historical_backfill_flash_html(flash: Optional[dict]) -> str:
    if not flash:
        return ""
    status = str(flash.get("status") or "success").lower()
    action = str(flash.get("action") or "historical data")
    message = str(flash.get("message") or "Historical data action completed.")
    job_id = str(flash.get("job_id") or "")
    selected_range = str(flash.get("range") or "")
    next_step = str(flash.get("next_step") or "Review the job status below.")
    detail_parts = []
    if job_id:
        detail_parts.append(f"Job {job_id}")
    if selected_range:
        detail_parts.append(selected_range)
    detail = " · ".join(detail_parts)
    detail_html = f"<small>{html.escape(detail)}</small>" if detail else ""
    return f"""
    <div class="provider-action-result visible {html.escape(status)} provider-flash historical-backfill-flash" role="status">
      <strong>{html.escape(action.replace('_', ' ').title())}</strong>
      <span>{html.escape(message)}</span>
      {detail_html}
      <small>{html.escape(next_step)}</small>
    </div>
    """


def provider_runtime_html(provider_runtime: Optional[ProviderRuntime]) -> str:
    if not provider_runtime:
        return ""
    data = provider_runtime.as_dict()
    rows = []
    for provider in data.get("providers", []):
        health = provider.get("health", {})
        rows.append(
            f"""
            <div class="completeness-row {'good' if health.get('state') in {'IDLE', 'SUCCESS'} else 'bad'}">
              <span>{html.escape(str(provider.get("provider_id") or "provider"))}</span>
              <strong>{html.escape(str(health.get("state") or "UNKNOWN").replace("_", " ").title())}</strong>
              <small>Schedule: {html.escape(str(provider.get("schedule") or "manual"))}. Last refresh: {html.escape(str(health.get("last_run") or "Never"))}. Next: {html.escape(str(health.get("next_run_at") or "Manual"))}.</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Data connection status</h3><span>{html.escape(str(data.get("scheduler_status") or "idle").title())}</span></div>
      <div class="config-grid">
        <div><span>Connection health</span><strong>{html.escape(str(data.get("runtime_health") or "unknown").title())}</strong></div>
        <div><span>Active providers</span><strong>{len(data.get("active_providers", []))}</strong></div>
      </div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No data connections are registered.</p>'}</div>
    </div>
    """


def provider_observations_html(summary: Optional[dict]) -> str:
    if not summary:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Recent household updates</h3><span>Not available yet</span></div>
      <p class="notice">No recent update history is available yet. Existing bill, tariff, or import information can still support cautious guidance.</p>
    </div>
    """
    rows = []
    for observation in summary.get("latest", [])[:8]:
        provider = friendly_update_provider(observation)
        description = friendly_update_description(observation)
        relative_time = relative_update_time(str(observation.get("observed_at") or observation.get("received_at") or ""))
        rows.append(
            f"""
            <div class="completeness-row {'good' if observation.get('quality') == 'high' else 'neutral'}">
              <span>{html.escape(relative_time)}</span>
              <strong>{html.escape(provider)}</strong>
              <small>{html.escape(description)}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Recent household updates</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">Legacy readings or EV sessions may exist before source reading history. New imports and live data refreshes will populate readings as they run.</p>'}</div>
    </div>
    """


def provider_observations_diagnostics_html(summary: Optional[dict]) -> str:
    if not summary:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Source reading details</h3><span>Not available yet</span></div>
      <p class="notice">No source reading summary is available for troubleshooting.</p>
    </div>
    """
    rows = []
    for observation in summary.get("latest", [])[:8]:
        rows.append(
            f"""
            <div class="completeness-row {'good' if observation.get('quality') == 'high' else 'bad'}">
              <span>{html.escape(str(observation.get("provider_id") or "Unknown provider"))}</span>
              <strong>{html.escape(str(observation.get("observation_type") or "Observation").replace("_", " ").title())}</strong>
              <small>{html.escape(str(observation.get("capability") or ""))} - {html.escape(str(observation.get("observed_at") or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Source reading details</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No source readings recorded yet.</p>'}</div>
    </div>
    """


def friendly_update_provider(observation: dict) -> str:
    provider_id = str(observation.get("provider_id") or "").lower()
    provider_names = {
        "solaredge": "SolarEdge",
        "ocular_export": "Ocular",
        "home_assistant_ocpp": "Home Assistant",
        "home_assistant": "Home Assistant",
        "synergy_tariff_config": "Electricity tariff",
        "smart_meter_readings": "Smart Meter",
    }
    return provider_names.get(provider_id, customer_source(provider_id) if provider_id else "Household data")


def friendly_update_description(observation: dict) -> str:
    text = " ".join(
        str(observation.get(key) or "")
        for key in ("observation_type", "capability")
    ).lower()
    if "solar" in text:
        return "Solar production updated"
    if "grid" in text or "interval" in text or "import" in text:
        return "Electricity usage updated"
    if "ev" in text or "session" in text or "charging" in text:
        return "EV charging imported"
    if "tariff" in text:
        return "Tariff information updated"
    if "billing" in text:
        return "Billing cycle updated"
    if "collector" in text or "health" in text:
        return "Live data status updated"
    return "Household information updated"


def relative_update_time(value: str) -> str:
    if not value:
        return "Recently"
    try:
        observed = datetime.fromisoformat(value.replace(",", ".")).astimezone(LOCAL_TIMEZONE)
    except ValueError:
        return display_timestamp(value)
    today = datetime.now(LOCAL_TIMEZONE).date()
    days = (today - observed.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if 1 < days < 7:
        return f"{days} days ago"
    return observed.strftime("%d %b")


def system_trust_summary_html(provider_management: Optional[ProviderManager], summary: Summary) -> str:
    checklist = []
    limitations = []
    if provider_management:
        for provider in provider_management.providers():
            data = provider.as_dict()
            onboarding = data.get("onboarding") or {}
            provider_id = str(data.get("provider_id") or provider.provider_id)
            if provider_id == "smart_meter_readings":
                continue
            kind = _source_kind(provider_id, data)
            status = _data_source_status(kind, data, onboarding)
            if _should_show_data_source(provider_id, data, onboarding):
                label = _source_display_name(provider_id, data)
                detail = "Configured" if kind == "configuration" else _format_source_timestamp(onboarding.get("last_successful_sync"), "Available")
                prefix = "✓" if status in {"Connected", "Latest upload available"} else "!"
                checklist.append((prefix, label, detail, status))
            elif _is_future_optional_source(provider_id, data, onboarding):
                checklist.append(("○", _source_display_name(provider_id, data), "Not connected (optional)", "Optional"))
            if kind == "import" and _should_show_data_source(provider_id, data, onboarding) and not onboarding.get("connected"):
                limitations.append(f"{_source_display_name(provider_id, data)} is historical upload data, not a live service.")
            elif not _is_future_optional_source(provider_id, data, onboarding) and data.get("lifecycle_state") in {"NOT_CONFIGURED", "ERROR", "DEGRADED"}:
                limitations.append(f"{_source_display_name(provider_id, data)} needs attention.")
    if not checklist and int(getattr(summary, "intervals", 0) or 0) > 0:
        checklist.append(("✓", "Recent household information", "Available", "Connected"))
    source_rows = "".join(
        f"""
        <li><strong>{html.escape(prefix)} {html.escape(label)}</strong><span>{html.escape(detail)}</span></li>
        """
        for prefix, label, detail, status in checklist
    ) or "<li><strong>! No household data</strong><span>Add a data source or upload history.</span></li>"
    limitation_rows = _list_items(limitations[:3])
    confidence_label = "High confidence" if checklist and not limitations else "Limited confidence"
    confidence_copy = (
        "Today's advice is based on household data that is available and recent enough for practical guidance."
        if confidence_label == "High confidence"
        else "Some live readings are missing or need attention, so PowerMonitor is using the best recent household information available."
    )
    return f"""
    <div class="system-card trust-summary-card">
      <div class="chart-heading"><h3>Today's advice uses</h3><span>{html.escape(confidence_label)}</span></div>
      <p>{html.escape(confidence_copy)}</p>
      <div class="provider-guidance-grid trust-checklist">
        <div><span>Information PowerMonitor can use</span><ul>{source_rows}</ul></div>
        <div><span>Limits to keep in mind</span>{f'<ul>{limitation_rows}</ul>' if limitation_rows else '<strong>No major source limitations detected.</strong>'}</div>
      </div>
    </div>
    """


def system_action_needed_html(setup: Optional[SetupChecklist], provider_management: Optional[ProviderManager]) -> str:
    actions = []
    if setup and setup.progress.remaining_tasks:
        for task in setup.progress.remaining_tasks[:2]:
            actions.append((task.title, task.rationale, task.action_href, task.action_label))
    if provider_management:
        for provider in provider_management.providers():
            data = provider.as_dict()
            onboarding = data.get("onboarding") or {}
            provider_id = str(data.get("provider_id") or provider.provider_id)
            if provider_id == "smart_meter_readings" or _is_future_optional_source(provider_id, data, onboarding):
                continue
            status = _data_source_status(_source_kind(provider_id, data), data, onboarding)
            if status == "Needs attention":
                actions.append((_source_display_name(provider_id, data), "This data source needs a quick check before advice can use it confidently.", f"#system-provider-{provider_id}", "Review source"))
    if not actions:
        return """
        <div class="system-card">
          <div class="chart-heading"><h3>Is there anything I need to do?</h3><span>Nothing urgent</span></div>
          <p class="scenario-note">No required setup or data-source fixes are active right now.</p>
        </div>
        """
    rows = "".join(
        f"""
        <div class="completeness-row bad">
          <span>{html.escape(title)}</span>
          <strong>{html.escape(action_label)}</strong>
          <small>{html.escape(trust_wording(detail))}</small>
          <a href="{html.escape(href)}">{html.escape(action_label)}</a>
        </div>
        """
        for title, detail, href, action_label in actions[:3]
    )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Is there anything I need to do?</h3><span>{len(actions)} item{'s' if len(actions) != 1 else ''}</span></div>
      <div class="completeness-grid">{rows}</div>
    </div>
    """


def promotion_summary_html(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    counts = summary.get("counts", {})
    rows = []
    for item in summary.get("latest_promotions", [])[:8]:
        state = str(item.get("state") or "PENDING")
        tone = "good" if state == "ACCEPTED" else "bad" if state in {"REJECTED", "CONFLICT"} else "neutral"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(str(item.get("provider_id") or "Unknown provider"))}</span>
              <strong>{html.escape(state.replace("_", " ").title())}</strong>
              <small>{html.escape(str(item.get("observation_type") or ""))} - {html.escape(str(item.get("observed_at") or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Latest trusted readings</h3><span>{int(counts.get("ACCEPTED") or 0)} accepted / {int(counts.get("REJECTED") or 0)} rejected / {int(counts.get("CONFLICT") or 0)} conflicts</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No source readings have been trusted yet.</p>'}</div>
    </div>
    """


def context_summary_html(context) -> str:
    if not context:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Context summary</h3><span>Unavailable</span></div>
      <p class="notice">Household context could not be inferred for this report.</p>
    </div>
    """
    data = context.as_dict() if hasattr(context, "as_dict") else dict(context)
    rows = {
        "Time": data.get("time"),
        "Solar": data.get("solar"),
        "Tariff": data.get("tariff"),
        "Charging": data.get("charging"),
        "Billing": data.get("billing"),
        "Evidence": data.get("evidence"),
        "Confidence": data.get("confidence"),
    }
    row_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or 'Unknown').replace('_', ' ').title())}</strong></div>"
        for label, value in rows.items()
    )
    sources = ", ".join(str(source) for source in data.get("data_sources_used", [])) or "No sources available"
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Context summary</h3><span>{html.escape(str(data.get('current_tariff_label') or 'Current state'))}</span></div>
      <div class="config-grid">{row_html}</div>
      <p class="notice">{html.escape(str(data.get("confidence_reason") or ""))}</p>
      <p class="small">Sources: {html.escape(sources)}</p>
    </div>
    """


def household_state_html(household_state, latest_snapshot=None, recent_snapshots=None) -> str:
    if not household_state:
        return ""
    data = household_state.as_dict() if hasattr(household_state, "as_dict") else dict(household_state)
    rows = {
        "Current tariff": data.get("tariff", {}).get("active_label"),
        "Import/export": data.get("power", {}).get("import_export_state"),
        "Grid live power": _live_power_label(data.get("power", {}).get("current_grid_power_w")),
        "Home live power": _live_power_label(data.get("power", {}).get("current_home_power_w")),
        "Solar": "Producing" if data.get("solar", {}).get("producing") else "Not producing" if data.get("solar", {}).get("producing") is False else "Unknown",
        "Solar live power": _live_power_label(data.get("solar", {}).get("current_production_w")),
        "EV": data.get("ev", {}).get("charging_source") or "Unknown",
        "EV charging power": _live_power_label(data.get("ev", {}).get("charging_power_w")),
        "Battery": data.get("battery", {}).get("state"),
        "Battery SOC": _percentage_label(data.get("battery", {}).get("charge_pct")),
        "Battery power": _live_power_label(data.get("battery", {}).get("power_w")),
        "Billing day": data.get("billing", {}).get("day_of_period"),
        "Live data": data.get("collector", {}).get("status"),
        "Evidence": data.get("evidence", {}).get("confidence"),
    }
    row_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or 'Unknown').replace('_', ' ').title())}</strong></div>"
        for label, value in rows.items()
    )
    sources = ", ".join(data.get("evidence", {}).get("data_sources_used", [])) or "No sources available"
    latest_text = latest_snapshot.get("captured_at") if latest_snapshot else data.get("generated_at")
    changes = []
    if recent_snapshots:
        for change in recent_snapshots.get("changes", [])[:4]:
            changes.append(
                f"<li>{html.escape(str(change.get('captured_at') or 'Unknown'))}: {html.escape(', '.join(change.get('changed', [])) or 'state changed')}</li>"
            )
    changes_html = f"<ul>{''.join(changes)}</ul>" if changes else '<p class="small">No recent material state changes recorded.</p>'
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Household status</h3><span>{html.escape(str(data.get("summary", {}).get("current_status") or "Current facts").title())}</span></div>
      <div class="config-grid">{row_html}</div>
      <p class="small">Latest snapshot: {html.escape(str(latest_text or "Not captured"))}</p>
      {changes_html}
      <p class="small">Sources: {html.escape(sources)}</p>
    </div>
    """


def _live_power_label(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return f"{float(value):.0f} W"
    except (TypeError, ValueError):
        return str(value)


def _percentage_label(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return str(value)


def household_behaviour_html(behaviour: Optional[dict]) -> str:
    if not behaviour:
        return ""
    baseload = behaviour.get("baseload") or {}
    daily = behaviour.get("daily_usage") or {}
    ev = behaviour.get("ev") or {}
    solar = behaviour.get("solar") or {}
    overnight = baseload.get("overnight_baseload_watts")
    rows = {
        "Overnight baseload": f"{float(overnight):.0f} W" if overnight is not None else "Unknown",
        "Average wake time": daily.get("typical_wake_time") or "Unknown",
        "EV charging": ev.get("preferred_charging_window") or "Unknown",
        "Solar export tendency": solar.get("export_tendency") or "Unknown",
        "Confidence": behaviour.get("confidence") or "Unknown",
    }
    row_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value).replace('_', ' ').title())}</strong></div>"
        for label, value in rows.items()
    )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Household Behaviour</h3><span>{int(behaviour.get("sample_days") or 0)} learned days</span></div>
      <div class="config-grid">{row_html}</div>
    </div>
    """


def household_anomalies_html(anomalies: Optional[list[dict]]) -> str:
    if not anomalies:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Unusual changes</h3><span>0 active</span></div>
      <p class="notice">No unusual household changes are active.</p>
    </div>
    """
    rows = []
    for anomaly in anomalies[:8]:
        severity = str(anomaly.get("severity") or "info")
        confidence = str(anomaly.get("confidence") or "UNKNOWN")
        tone = "bad" if severity in {"warning", "critical"} else "neutral" if severity == "watch" else "good"
        evidence = anomaly.get("evidence") or []
        evidence_summary = evidence[0].get("summary") if evidence and isinstance(evidence[0], dict) else anomaly.get("summary")
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(str(anomaly.get("title") or "Household anomaly"))}</span>
              <strong>{html.escape(severity.title())} / {html.escape(confidence.title())}</strong>
              <small>{html.escape(str(anomaly.get("first_detected") or ""))} - {html.escape(str(evidence_summary or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Unusual changes</h3><span>{len(anomalies)} active</span></div>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def household_decisions_html(
    decisions: Optional[list[dict]],
    summary: Optional[dict],
) -> str:
    if not decisions:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Recommendation summary</h3><span>0 active</span></div>
      <p class="notice">No active recommendations are ready right now.</p>
    </div>
    """
    rows = []
    for decision in decisions[:8]:
        priority = str(decision.get("priority") or "medium")
        confidence = str(decision.get("confidence") or "UNKNOWN")
        tone = "bad" if priority in {"high", "critical"} else "neutral" if priority == "medium" else "good"
        reasons = decision.get("reasons") or []
        reason = reasons[0].get("summary") if reasons and isinstance(reasons[0], dict) else decision.get("type")
        evidence = decision.get("evidence") or []
        evidence_summary = evidence[0].get("signal") if evidence and isinstance(evidence[0], dict) else "supporting evidence"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(customer_source(decision.get("type") or "Recommendation"))}</span>
              <strong>{html.escape(priority.title())} / {html.escape(confidence.title())}</strong>
              <small>{html.escape(customer_label(reason or ""))} Why this matters: {html.escape(customer_source(evidence_summary))}</small>
            </div>
            """
        )
    active_count = int((summary or {}).get("active_count") or len(decisions))
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Recommendation summary</h3><span>{active_count} active</span></div>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def decision_provenance_html(
    decisions: Optional[list[dict]],
    advisors: Optional[list[Advisor]],
) -> str:
    if not decisions:
        return ""
    advisors = advisors or []
    advisor_by_decision = {
        advisor.decision_id: advisor
        for advisor in advisors
        if advisor.decision_id
    }
    rows = []
    for decision in decisions[:6]:
        decision_id = decision.get("decision_id")
        advisor = advisor_by_decision.get(decision_id)
        evidence = decision.get("evidence") or []
        evidence_text = evidence[0].get("source") if evidence and isinstance(evidence[0], dict) else "Household data"
        advisor_text = advisor.headline if advisor else "No explanation linked"
        rows.append(
            f"""
            <div class="completeness-row neutral">
              <span>{html.escape(customer_source(decision.get("type") or "Recommendation"))}</span>
              <strong>{html.escape(advisor_text)}</strong>
              <small>Source: {html.escape(customer_source(evidence_text))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Recommendation trail</h3><span>Recommendation to explanation to source</span></div>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


def state_changes_html(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    rows = []
    for change in summary.get("latest", [])[:8]:
        severity = str(change.get("severity") or "info")
        tone = "bad" if severity in {"warning", "critical"} else "good" if severity == "opportunity" else "neutral"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(str(change.get("title") or "State changed"))}</span>
              <strong>{html.escape(severity.title())}</strong>
              <small>{html.escape(str(change.get("summary") or ""))} - {html.escape(str(change.get("detected_at") or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Recent household changes</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No material household state changes recorded yet.</p>'}</div>
    </div>
    """


def notification_readiness_html(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    rows = []
    for candidate in summary.get("candidates", [])[:8]:
        ready = bool(candidate.get("ready"))
        priority = str(candidate.get("priority") or "low")
        tone = "bad" if priority in {"high", "critical"} and ready else "good" if ready else "neutral"
        status = "Ready" if ready else str(candidate.get("suppression_reason") or "suppressed").replace("_", " ").title()
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(str(candidate.get("title") or "Notification candidate"))}</span>
              <strong>{html.escape(status)}</strong>
              <small>{html.escape(str(candidate.get("message") or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Notification checks</h3><span>{int(summary.get("ready_count") or 0)} ready / {int(summary.get("count") or 0)} candidates</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No notification candidates are ready.</p>'}</div>
    </div>
    """


def notification_preferences_html(preferences: Optional[dict]) -> str:
    if not preferences:
        return ""
    quiet = preferences.get("quiet_hours") or {}
    rows = {
        "Notifications": "Enabled" if preferences.get("enabled") else "Disabled",
        "Priority threshold": preferences.get("priority_threshold"),
        "Quiet hours": f"{quiet.get('start')} to {quiet.get('end')}" if quiet.get("enabled") else "Off",
        "Channel": preferences.get("channel_preference"),
        "Cooldown": f"{preferences.get('cooldown_minutes')} minutes",
        "Type rules": len(preferences.get("rule_preferences") or []),
    }
    row_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or 'Unknown').replace('_', ' ').title())}</strong></div>"
        for label, value in rows.items()
    )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Notification settings</h3><span>Local only</span></div>
      <div class="config-grid">{row_html}</div>
    </div>
    """


def notification_history_html(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    rows = []
    for item in summary.get("latest", [])[:6]:
        state = str(item.get("delivery_state") or "pending")
        tone = "bad" if state == "suppressed" else "good" if state == "sent" else "neutral"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(str(item.get("notification_type") or "notification").replace('_', ' ').title())}</span>
              <strong>{html.escape(state.title())}</strong>
              <small>{html.escape(str(item.get("suppression_reason") or "none").replace('_', ' ').title())} - {html.escape(str(item.get("updated_at") or item.get("created_at") or ""))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Notification history</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No notification history recorded yet.</p>'}</div>
    </div>
    """


def confidence_section_html(
    summary: Summary,
    settings: Settings,
    provider_observations: Optional[dict],
) -> str:
    confidence, confidence_note = confidence_label(summary)
    missing = [item for item in current_knowledge_items(summary, settings) if item["known"] != "yes"]
    missing_rows = "".join(
        f"""
        <div class="completeness-row neutral">
          <span>{html.escape(item["name"])}</span>
          <strong>Not available</strong>
          <small>{html.escape(item["effect"])}</small>
        </div>
        """
        for item in missing[:5]
    )
    missing_html = missing_rows or '<p class="notice">Nothing important is missing for today&apos;s advice.</p>'
    effect = (
        "PowerMonitor can make reliable recommendations today using the available household information."
        if str(confidence).lower() == "high"
        else "PowerMonitor can still provide guidance, but it will avoid strong recommendations where live or recent information is missing."
    )
    return f"""
    <div id="system-data-quality" class="trust-answer-card">
      <div class="chart-heading"><h3>Can I trust today's advice?</h3><span>{html.escape(homeowner_confidence_label(confidence))}</span></div>
      <p class="system-answer-lede">Today's advice is: <strong>{html.escape(homeowner_confidence_label(confidence))}</strong>.</p>
      <p>{html.escape(trust_wording(confidence_note))} {html.escape(effect)}</p>
      <div class="system-overview-grid">
        {data_completeness_html(summary, settings)}
        <div class="system-card">
          <div class="chart-heading"><h3>What information is missing?</h3><span>Effect on advice</span></div>
          <div class="completeness-grid">{missing_html}</div>
        </div>
      </div>
      {provider_observations_html(provider_observations)}
    </div>
    """


def live_data_help_html(health: dict, summary: Summary) -> str:
    status = str(health.get("status") or "unknown")
    live_ok = status == "ok"
    latest = health.get("last_successful_collection") or health.get("latest_measurement_time")
    live_title = "Live data is working." if live_ok else "Live updates are not available right now."
    unavailable = "Nothing important is unavailable right now." if live_ok else "Right-now readings from live data sources cannot be verified."
    why = (
        "The latest live refresh completed successfully."
        if live_ok
        else collector_status_explanation(health)
    )
    if summary.intervals:
        effect = "Recent household information is still available, so PowerMonitor can provide cautious guidance instead of treating everything as unavailable."
    else:
        effect = "PowerMonitor has too little usable household information, so recommendations will be limited until data is refreshed or imported."
    action = "No action is needed right now." if live_ok else "Reconnect the affected live service, then run Test Connection or refresh the report."
    return f"""
    <div id="system-live-data" class="trust-answer-card">
      <div class="chart-heading"><h3>Why isn't live data working?</h3><span>{html.escape("Working" if live_ok else "Needs attention")}</span></div>
      <p class="system-answer-lede">{html.escape(live_title)}</p>
      <div class="provider-guidance-grid">
        <div><span>What is unavailable?</span><strong>{html.escape(unavailable)}</strong></div>
        <div><span>Why?</span><strong>{html.escape(why)}</strong></div>
        <div><span>How does it affect advice?</span><strong>{html.escape(effect)}</strong></div>
        <div><span>Suggested action</span><strong>{html.escape(action)}</strong></div>
      </div>
      <p class="scenario-note">Last successful update: {html.escape(display_timestamp(latest))}.</p>
    </div>
    """


def runtime_check_html(runtime_check: Optional[dict]) -> str:
    if not runtime_check:
        return """
        <div class="system-card">
          <div class="chart-heading"><h3>Local environment details</h3><span>Unavailable</span></div>
          <p class="notice">Local environment details are not available for this report.</p>
        </div>
        """
    tariff = runtime_check.get("active_tariff_version") or {}
    health_summary = runtime_check.get("runtime_health_summary") or {}
    rows = [
        ("Git commit", runtime_check.get("git_commit")),
        ("Python version", runtime_check.get("python_version")),
        ("Python executable", runtime_check.get("executable")),
        ("Working directory", runtime_check.get("working_directory")),
        ("Project .venv", "Yes" if runtime_check.get("running_in_project_venv") else "No"),
        ("Playwright", "Available" if runtime_check.get("playwright_available") else "Missing"),
        ("Playwright Chromium", "Available" if runtime_check.get("playwright_chromium_available") else "Missing"),
        ("pypdf", "Available" if runtime_check.get("pypdf_available") else "Missing"),
        ("pdftotext", "Available" if runtime_check.get("pdftotext_available") else "Not found"),
        ("Project root", runtime_check.get("project_root")),
        ("Report path", runtime_check.get("report_path")),
        ("Report generated", runtime_check.get("report_generation_timestamp")),
        ("Database", runtime_check.get("database_path")),
        ("Settings/config", runtime_check.get("settings_path")),
        ("Web bind", runtime_check.get("web_bind_address") or f"{runtime_check.get('web_host') or 'unknown'}:{runtime_check.get('web_port') or 'unknown'}"),
        ("Active tariff version", tariff.get("version_id") or tariff.get("version") or tariff.get("tariff_name")),
        ("Runtime health", health_summary.get("status") or health_summary.get("failure_type") or "n/a"),
    ]
    row_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or 'n/a'))}</strong></div>"
        for label, value in rows
    )
    launch_labels = runtime_check.get("active_launch_agent_labels") or []
    launch_html = "".join(f"<li>{html.escape(str(label))}</li>" for label in launch_labels) or "<li>No active PowerMonitor LaunchAgents detected.</li>"
    warning_rows = "".join(f"<li>{html.escape(str(item))}</li>" for item in runtime_check.get("warnings") or [])
    warnings = f"<ul>{warning_rows}</ul>" if warning_rows else "<p>No runtime setup warnings detected.</p>"
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Runtime diagnostics</h3><span>Canonical paths</span></div>
      <div class="config-grid">{row_html}</div>
      <details class="evidence-trail"><summary>Active LaunchAgents</summary><ul>{launch_html}</ul></details>
      <details class="evidence-trail"><summary>Runtime setup warnings</summary>{warnings}</details>
    </div>
    """


def system_page_html(
    health: dict,
    settings: Settings,
    summary: Summary,
    capability_summary=None,
    setup: Optional[SetupChecklist] = None,
    provider_registry: Optional[ProviderRegistry] = None,
    provider_management: Optional[ProviderManager] = None,
    provider_runtime: Optional[ProviderRuntime] = None,
    provider_observations: Optional[dict] = None,
    promotion_summary: Optional[dict] = None,
    context=None,
    household_state=None,
    household_behaviour: Optional[dict] = None,
    household_anomalies: Optional[list[dict]] = None,
    household_decisions: Optional[list[dict]] = None,
    decision_summary: Optional[dict] = None,
    advisor_explanations: Optional[list[Advisor]] = None,
    latest_household_state_snapshot=None,
    recent_household_state_snapshots=None,
    state_change_summary: Optional[dict] = None,
    notification_readiness_summary: Optional[dict] = None,
    notification_preferences: Optional[dict] = None,
    notification_history: Optional[dict] = None,
    provider_flash: Optional[dict] = None,
    runtime_check: Optional[dict] = None,
    energy_timeline: Optional[EnergyTimeline] = None,
    battery_comparison: Optional[dict] = None,
) -> str:
    counts = ev_source_counts()
    count_rows = "".join(
        f"<tr><td>{html.escape(item['source'])}</td><td>{item['sessions']}</td><td>{kwh(item['energy_kwh'])}</td><td>{money(item['cost'])}</td></tr>"
        for item in counts
    ) or '<tr><td colspan="4">No EV sessions table rows found.</td></tr>'
    config_rows = {
        "ev_data_source": settings.ev_data_source,
        "billing_period_start": settings.current_billing_period_start.isoformat() if settings.current_billing_period_start else "fallback",
        "billing_period_end": settings.current_billing_period_end.isoformat() if settings.current_billing_period_end else "estimated",
        "supply_charge_per_day": money(settings.supply_charge_per_day),
        "export_credit_rate": money(settings.export_credit_rate),
        "comparison_flat_rate": money(settings.comparison_plan_rates["flat_import_rate"]),
    }
    config_html = "".join(f"<div><span>{html.escape(key)}</span><strong>{html.escape(str(value))}</strong></div>" for key, value in config_rows.items())
    collector_body = f"""
      <div class="metrics">
        {metric("Live data status", collector_status_text(health), "Current live data state", "good" if health.get("status") == "ok" else "bad")}
        {metric("Last successful refresh", display_timestamp(health.get("last_successful_collection")), "Most recent successful data refresh", "grid")}
        {metric("Last failure", display_timestamp(health.get("last_collection_failure")), str(health.get("collector_message") or health.get("failure_type") or "No recent failure recorded"), "bad" if health.get("status") != "ok" and health.get("last_collection_failure") else "grid")}
        {metric("Report age", f"{float(health.get('report_age_minutes') or 0):.1f} minutes", "Freshness of generated dashboard", "grid")}
        {metric("Latest reading", display_timestamp(health.get("latest_measurement_time")), "Latest household reading", "grid")}
        {metric("Setup completion", f"{setup.progress.completion_pct}%" if setup else "Unavailable", "Setup readiness", "good" if setup and setup.progress.complete else "grid")}
      </div>
    """
    advanced_body = f"""
      <div class="system-card">
        <div class="chart-heading"><h3>Live data diagnostics</h3><span>Raw status</span></div>
        {collector_body}
      </div>
      {provider_observations_diagnostics_html(provider_observations)}
      {runtime_check_html(runtime_check)}
      {battery_dispatch_diagnostics_html(battery_comparison)}
      {provider_runtime_html(provider_runtime)}
      {provider_registry_html(provider_registry)}
      {household_state_html(household_state, latest_household_state_snapshot, recent_household_state_snapshots)}
      {household_behaviour_html(household_behaviour)}
      {household_anomalies_html(household_anomalies)}
      {decision_provenance_html(household_decisions, advisor_explanations)}
      {state_changes_html(state_change_summary)}
      {notification_preferences_html(notification_preferences)}
      {notification_readiness_html(notification_readiness_summary)}
      {notification_history_html(notification_history)}
      {promotion_summary_html(promotion_summary)}
      {context_summary_html(context)}
      {capability_summary_html(capability_summary)}
      <div class="system-card">
        <div class="chart-heading"><h3>EV source counts</h3><span>Grouped by source</span></div>
        <table><thead><tr><th>Source</th><th>Sessions</th><th>kWh</th><th>Cost</th></tr></thead><tbody>{count_rows}</tbody></table>
      </div>
      <div class="system-card">
        <div class="chart-heading"><h3>Config summary</h3><span>Privacy safe</span></div>
        <div class="config-grid">{config_html}</div>
      </div>
      <div class="system-card">
        <div class="chart-heading"><h3>Import instructions</h3><span>No secrets displayed</span></div>
        <p>Ocular: <code>python3 collector/import_ocular_export.py /path/to/export.xlsx</code></p>
        <p>SolarEdge: refresh <code>collector/solaredge_cookie.txt</code> only when the session warning appears. Cookie contents are never displayed here.</p>
      </div>
    """
    return f"""
    <section id="system-collector" class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Settings</span><h2>Can I trust today's advice?</h2></div><p>Start with whether the current guidance is based on enough household information.</p></div>
      {system_trust_summary_html(provider_management, summary)}
      {system_action_needed_html(setup, provider_management)}
      {confidence_section_html(summary, settings, provider_observations)}
      {disclosure_html("Provider Management", "Connect, test, reconnect, disable, remove, and inspect household integrations", f'''
        {provider_management_html(provider_management, provider_flash=provider_flash)}
      ''', open_by_default=True, css_class="system-group")}
      {disclosure_html("Live data help", "What is unavailable, why, and what to do next", live_data_help_html(health, summary), css_class="system-group")}
      {disclosure_html("Recommendations", "Active household recommendations and confidence", f'''
        {household_decisions_html(household_decisions, decision_summary)}
      ''', css_class="system-group")}
      {disclosure_html("Advanced Diagnostics", "Data connections, behaviour, recommendation trail, notifications, and local configuration", advanced_body, css_class="system-group advanced-diagnostics")}
    </section>
    """


def battery_dispatch_diagnostics_html(comparison: Optional[dict]) -> str:
    if not comparison or not comparison.get("diagnostics"):
        return ""
    diagnostics = comparison.get("diagnostics") or {}
    strategy_options = (diagnostics.get("strategies") or {}).get("solar_plus_grid_arbitrage")
    strategy_label = "Solar plus cheap-grid charging" if strategy_options else "Solar-only charging"
    options = strategy_options or diagnostics.get("options") or []
    recommended_capacity = float((comparison.get("recommended") or {}).get("capacity_kwh") or 0.0)
    selected = next(
        (option for option in options if abs(float(option.get("capacity_kwh") or 0.0) - recommended_capacity) < 1e-9),
        options[0] if options else None,
    )
    if not selected:
        return ""
    summary = selected.get("summary") or {}
    totals = summary.get("totals") or {}
    warnings = summary.get("warnings") or []
    warning_html = (
        f"""<div class="notice warning"><strong>Dispatch warnings</strong><ul>{"".join(f"<li>{html.escape(str(item))}</li>" for item in warnings)}</ul></div>"""
        if warnings
        else '<p class="scenario-note">No battery dispatch invariant warnings for the selected size.</p>'
    )
    rows = []
    for row in selected.get("daily_rows") or []:
        rows.append(
            f"""
            <tr class="{'warning' if row.get('energy_conservation_check') != 'pass' else ''}">
              <td>{html.escape(str(row.get("date") or ""))}</td>
              <td>{kwh(float(row.get("starting_state_of_charge_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("verified_solar_export_available_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("solar_charged_kwh", row.get("charged_into_battery_kwh")) or 0.0))}</td>
              <td>{kwh(float(row.get("grid_energy_used_to_charge_battery_kwh") or 0.0))}</td>
              <td>{money(float(row.get("grid_charge_rate") or 0.0))}</td>
              <td>{kwh(float(row.get("charging_losses_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("rejected_solar_because_battery_full_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("peak_import_before_battery_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("other_non_cheapest_import_before_battery_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("cheapest_window_import_before_battery_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("battery_discharge_into_peak_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("battery_discharge_into_other_non_cheapest_kwh") or 0.0))}</td>
              <td>{money(float(row.get("avoided_cost") or 0.0))}</td>
              <td>{kwh(float(row.get("total_battery_discharge_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("ending_state_of_charge_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("remaining_peak_import_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("remaining_other_non_cheapest_import_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("remaining_cheapest_window_import_kwh") or 0.0))}</td>
              <td>{kwh(float(row.get("remaining_solar_export_kwh") or 0.0))}</td>
              <td>{money(float(row.get("net_daily_saving_after_charging_cost", row.get("estimated_savings")) or 0.0))}</td>
              <td>{html.escape(str(row.get("energy_conservation_check") or ""))}</td>
            </tr>
            """
        )
    return f"""
    <div class="system-card battery-diagnostics">
      <div class="chart-heading"><h3>Battery dispatch diagnostics</h3><span>{html.escape(strategy_label)} · {html.escape(str(selected.get("label") or ""))}</span></div>
      <p>{html.escape(str(diagnostics.get("methodology") or ""))}</p>
      <div class="config-grid">
        {battery_metric("Solar export", kwh(float(totals.get("verified_solar_export_available_kwh") or 0.0)), "Verified available")}
        {battery_metric("Solar charged", kwh(float(totals.get("solar_charged_kwh", totals.get("charged_into_battery_kwh")) or 0.0)), "Verified export into battery")}
        {battery_metric("Grid charged", kwh(float(totals.get("grid_energy_used_to_charge_battery_kwh") or 0.0)), "Cheapest tariff input")}
        {battery_metric("Charging losses", kwh(float(totals.get("charging_losses_kwh") or 0.0)), "Round-trip efficiency")}
        {battery_metric("Discharged", kwh(float(totals.get("total_battery_discharge_kwh") or 0.0)), "From battery")}
        {battery_metric("Rejected solar", kwh(float(totals.get("rejected_solar_because_battery_full_kwh") or 0.0)), "Battery full")}
        {battery_metric("Savings", money(float(totals.get("estimated_savings") or 0.0)), "Daily rows total")}
        {battery_metric("Check", str(summary.get("energy_conservation_check") or "unknown"), "Invariant result")}
      </div>
      {warning_html}
      <div class="battery-diagnostics-table">
        <table class="responsive-table">
          <thead><tr><th>Date</th><th>Start SOC</th><th>Solar export</th><th>Solar charged</th><th>Grid charged</th><th>Grid rate</th><th>Losses</th><th>Rejected</th><th>Peak before</th><th>Other before</th><th>Cheapest before</th><th>Peak discharge</th><th>Other discharge</th><th>Avoided cost</th><th>Total discharge</th><th>End SOC</th><th>Peak left</th><th>Other left</th><th>Cheapest left</th><th>Export left</th><th>Net saving</th><th>Check</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>
    """
def imports_page_html(
    history: Optional[list[dict]] = None,
    capability_summary=None,
    setup: Optional[SetupChecklist] = None,
    provider_registry: Optional[ProviderRegistry] = None,
    provider_management: Optional[ProviderManager] = None,
    provider_observations: Optional[dict] = None,
) -> str:
    try:
        history = history if history is not None else data_engine().get_import_history()
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        history = []

    if history:
        rows = "".join(
            f"""
            <tr>
              <td>{html.escape(str(item.get("created_at") or ""))}</td>
              <td>{html.escape(str(item.get("source", {}).get("display_name") or item.get("source", {}).get("source_id") or "Unknown"))}</td>
              <td>{html.escape(str(item.get("filename") or ""))}</td>
              <td>{html.escape(str(item.get("status") or ""))}</td>
              <td>{html.escape(str(item.get("message") or ""))}</td>
              <td>{int(item.get("provider_observation_count") or 0)}</td>
            </tr>
            """
            for item in history
        )
    else:
        rows = '<tr><td colspan="6">No import history yet.</td></tr>'

    capability_note = ""
    if capability_summary:
        ev = capability_summary.get("ev")
        imports = capability_summary.get("imports")
        capability_note = f"""
        <div class="config-grid">
          <div><span>Upload can add</span><strong>{html.escape(ev.label if ev else "EV charging")}</strong></div>
          <div><span>Upload support</span><strong>{html.escape("Available" if imports and imports.available else "Unavailable")}</strong></div>
        </div>
        """
    setup_tasks = []
    if setup:
        setup_tasks = [
            task for section in setup.sections
            if section.section_id == "imports"
            for task in section.tasks
        ]
    setup_task_rows = "".join(
        f"""
        <div class="completeness-row {'good' if task.status == 'complete' else 'bad'}">
          <span>{html.escape(task.title)}</span>
          <strong>{html.escape(task.status.replace("_", " ").title())}</strong>
          <small>{html.escape(task.rationale)}</small>
        </div>
        """
        for task in setup_tasks
        if task.status != "future"
    )
    setup_task_html_block = (
        f'<div class="system-card"><div class="chart-heading"><h3>Upload setup tasks</h3><span>Setup guidance</span></div><div class="completeness-grid">{setup_task_rows}</div></div>'
        if setup_task_rows else ""
    )
    import_providers = (
        [provider for provider in provider_management.providers() if ProviderCapability.IMPORT_ONLY in provider.capability_summary.capabilities]
        if provider_management else
        provider_registry.providers_with_capability(ProviderCapability.IMPORT_ONLY)
        if provider_registry else []
    )
    provider_rows = "".join(
        f"""
        <div class="completeness-row {'good' if getattr(provider, 'lifecycle_state', None) and provider.lifecycle_state.value != 'NOT_CONFIGURED' else 'bad'}">
          <span>{html.escape(provider.name)}</span>
          <strong>{html.escape(getattr(getattr(provider, 'lifecycle_state', None), 'value', getattr(getattr(provider, 'status', None), 'value', 'unknown')).replace("_", " ").title())}</strong>
          <small>{html.escape(provider.note or 'Import support')} <a href="#system-provider-{html.escape(provider.provider_id)}">Open data source setup</a></small>
        </div>
        """
        for provider in import_providers
    )
    observation_rows = ""
    if provider_observations:
        observation_rows = "".join(
            f"""
            <div class="completeness-row neutral">
              <span>{html.escape(str(item.get("provider_id") or "Unknown source"))}</span>
              <strong>{html.escape(str(item.get("observation_type") or "Reading").replace("_", " ").title())}</strong>
              <small>{html.escape(str(item.get("capability") or ""))}</small>
            </div>
            """
            for item in provider_observations.get("latest", [])[:6]
        )
    provider_html = (
        f'<div class="system-card"><div class="chart-heading"><h3>Supported uploads</h3><span>Import sources</span></div><div class="completeness-grid">{provider_rows}</div></div>'
        if provider_rows else ""
    )
    observation_html = (
        f'<div class="system-card"><div class="chart-heading"><h3>Recent source readings</h3><span>Latest uploaded or live readings</span></div><div class="completeness-grid">{observation_rows}</div></div>'
        if observation_rows else ""
    )
    import_guidance = """
      <div class="onboarding-steps">
        <div><span>1</span><strong>Choose source export</strong><p>PowerMonitor supports Ocular EV charging history Excel exports and Synergy PDF bills.</p></div>
        <div><span>2</span><strong>Preview before saving</strong><p>The preview checks rows, bill totals, billing periods, energy totals, and validation issues before anything is imported.</p></div>
        <div><span>3</span><strong>Import updates the dashboard</strong><p>Accepted sessions and bills are matched to your household and used in bills, EV analytics, and confidence messaging.</p></div>
      </div>
    """
    troubleshooting = """
      <ul class="guidance-list">
        <li>Use the original `.xlsx` or `.xlsm` export from Ocular, or the original Synergy bill PDF.</li>
        <li>If preview shows validation issues, export the source file again and retry before committing.</li>
        <li>Re-importing history is safe: existing sessions are updated and duplicate bills are ignored.</li>
        <li>The main dashboard will not auto-refresh while you are choosing or uploading a file.</li>
      </ul>
    """
    if setup_tasks:
        next_task = next((task for task in setup_tasks if task.status != "complete" and task.status != "future"), None)
    else:
        next_task = None
    if next_task:
        import_action = best_next_action_html(
            title=next_task.title,
            action=next_task.action_label,
            reason=next_task.rationale,
            confidence="Setup guidance",
        )
    elif history:
        import_action = best_next_action_html(
            title="Recent imports are available.",
            action="Review the latest import status below, then return to Home for updated guidance.",
            reason="Successful imports improve bill explanations, EV charging costs, and confidence messaging.",
            confidence="Import history found",
        )
    else:
        import_action = best_next_action_html(
            title="Start with the file that answers your next question.",
            action="Upload an Ocular export for EV charging costs, or a Synergy bill PDF to compare PowerMonitor with the retailer bill.",
            reason="Imports are optional, but they make recommendations more specific when live data is limited.",
            confidence="No import history yet",
        )

    return f"""
    {import_action}
    <section class="report-section import-panel">
      <div class="section-title"><div><span class="eyebrow">Imports</span><h2>Upload household data</h2></div><p>Choose an Ocular Excel export or Synergy PDF bill from your phone, tablet, or Mac.</p></div>
      {disclosure_html("First-time import guide", "What to upload and what happens next", import_guidance, open_by_default=True, css_class="onboarding-guide")}
      {capability_note}
      {provider_html}
      {observation_html}
      {setup_task_html_block}
      <form class="import-form" method="post" action="/imports/preview" enctype="multipart/form-data">
        <label><span>Ocular Excel or Synergy PDF</span><input type="file" name="import_file" accept=".xlsx,.xlsm,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel.sheet.macroEnabled.12" required></label>
        <button type="submit">Preview import</button>
      </form>
      <p class="notice">The dashboard will not auto-refresh while you are choosing or uploading a file.</p>
      {disclosure_html("Troubleshooting uploads", "Formats, validation, and safe re-imports", troubleshooting, css_class="onboarding-guide")}
    </section>
    <section class="report-section import-panel compact-section">
      <div class="section-title"><div><span class="eyebrow">History</span><h2>Recent import jobs</h2></div><p>Use this only to confirm whether an upload worked.</p></div>
      <table class="responsive-table">
        <thead><tr><th>Created</th><th>Source</th><th>File</th><th>Status</th><th>Message</th><th>Readings</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def comparison_html(comparison: dict) -> str:
    deltas = comparison["deltas"]
    label = (
        "Compared with previous billing period"
        if comparison["period_b"]["kind"] == "previous_billing_period"
        else "Compared with selected period"
    )
    rows = "".join(
        [
            comparison_summary_row_html("Net cost", deltas["net_cost"], signed_money, lower_is_better=True),
            comparison_summary_row_html("Grid import", deltas["grid_import_kwh"], signed_kwh, lower_is_better=True),
            comparison_summary_row_html("Solar generation", deltas["solar_generated_kwh"], signed_kwh),
            comparison_summary_row_html("Peak cost", deltas["peak_cost"], signed_money, lower_is_better=True),
            comparison_summary_row_html("Self-consumption", deltas["self_consumption_pct"], lambda v: f"{v:+.1f}%"),
            comparison_summary_row_html("EV charging", deltas["ev_charging_kwh"], signed_kwh, lower_is_better=True),
        ]
    )
    return f"""
    <section class="report-section compact-section comparison-section bills-detail-section">
      <div class="section-title"><div><span class="eyebrow">Period comparison</span><h2>{html.escape(label)}</h2></div>
      <p>{html.escape(comparison["period_b"]["label"])}: {comparison["period_b"]["start"]} to {comparison["period_b"]["end"]}</p></div>
      <div class="comparison-summary-list">{rows}</div>
    </section>
    """


def plan_comparison_html(comparison: dict, car: dict) -> str:
    current = comparison["current_plan"]
    flat = comparison["comparison_plan"]
    savings = comparison["savings"]
    conclusion = (
        f"You saved {money(savings)} this period."
        if savings > 0
        else f"The Flat plan would have been cheaper by {money(-savings)} this period."
        if savings < 0
        else "Both plans are about even for this period."
    )
    detail_rows = "".join(
        [
            bills_summary_row_html("EV charging cost", f"{money(current['ev_charging_cost'])} / {money(flat['ev_charging_cost'])}", "Current plan / flat plan", "ev"),
            bills_summary_row_html("Household usage cost", f"{money(current['household_usage_cost'])} / {money(flat['household_usage_cost'])}", "Current plan / flat plan", "cost"),
            bills_summary_row_html("Solar export value difference", signed_money(comparison["solar_export_value_difference"]), "Difference between plan models", "credit"),
        ]
    )
    return f"""
    <section class="report-section compact-section plan-comparison-section bills-detail-section">
      <div class="section-title"><div><span class="eyebrow">Plan comparison</span><h2>Could a different plan reduce this bill?</h2></div><p>Same measured usage, configured rates</p></div>
      <div class="bills-plan-grid">
        <div class="bills-plan-hero">
          <span>Current plan</span>
          <strong>{money(current["net_bill"])}</strong>
          <small>{html.escape(current["display_name"])}</small>
        </div>
        <div class="bills-plan-hero secondary">
          <span>Flat plan</span>
          <strong>{money(flat["net_bill"])}</strong>
          <small>{html.escape(flat["display_name"])}</small>
        </div>
        <div class="bills-plan-outcome">
          <span>Difference</span>
          <strong>{signed_money(comparison["difference"])}</strong>
          <p>{html.escape(conclusion)}</p>
        </div>
      </div>
      <div class="bills-summary-list bills-summary-list-compact">{detail_rows}</div>
      <p class="scenario-note">{html.escape(comparison["recommendation"])} These are tariff-model estimates, not actual bill reconciliation.</p>
      <p class="scenario-note">{html.escape(car["message"])}</p>
    </section>
    """


def bills_summary_row_html(label: str, value: str, note: str = "", tone: str = "") -> str:
    note_html = f"<small>{html.escape(note)}</small>" if note else ""
    tone_class = f" {html.escape(tone)}" if tone else ""
    return f"""
    <div class="bills-summary-row{tone_class}">
      <div class="bills-summary-copy"><span>{html.escape(label)}</span>{note_html}</div>
      <strong>{html.escape(value)}</strong>
    </div>
    """


def comparison_indicator(value: float) -> str:
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "→"


def comparison_summary_row_html(title: str, value: float, formatter, lower_is_better: bool = False) -> str:
    tone = "neutral"
    if value:
        improved = value < 0 if lower_is_better else value > 0
        tone = "good" if improved else "bad"
    display = formatter(value)
    return f"""
    <div class="comparison-summary-row {tone}">
      <span>{html.escape(title)}</span>
      <strong>{html.escape(f"{comparison_indicator(value)} {display}")}</strong>
    </div>
    """


def bills_actions_html(recommendations: list, plan: dict) -> str:
    ranked = sorted(recommendations, key=recommendation_sort_key)
    items = ranked[:3]
    if not items:
        return f"""
        <section class="report-section compact-section bills-detail-section bills-actions-section">
          <div class="section-title"><div><span class="eyebrow">Next steps</span><h2>What can I do about it?</h2></div><p>No high-priority savings action</p></div>
          <p class="notice">{html.escape(plan.get("recommendation") or "PowerMonitor will show a cost-saving action here when the household data points to one.")}</p>
        </section>
        """
    cards = "".join(
        f"""
        <article class="action-card {html.escape(recommendation_severity(item))}">
          <span>{html.escape(recommendation_confidence(item))} confidence</span>
          <h3>{html.escape(trust_wording(recommendation_action(item)))}</h3>
          <p><strong>Why this helps</strong>{html.escape(trust_wording(recommendation_why(item)))}</p>
          <p><strong>Expected impact</strong>{html.escape(trust_wording(recommendation_impact(item)))}</p>
        </article>
        """
        for item in items
    )
    return f"""
    <section class="report-section compact-section bills-detail-section bills-actions-section">
      <div class="section-title"><div><span class="eyebrow">Next steps</span><h2>What can I do about it?</h2></div><p>Highest-impact actions first</p></div>
      <div class="action-grid">{cards}</div>
    </section>
    """


def battery_comparison_html(comparison: Optional[dict]) -> str:
    if not comparison:
        return ""
    baseline = comparison.get("baseline", {})
    options = comparison.get("options", [])
    strategies = comparison.get("strategies") or {}
    grid_options = (strategies.get("solar_plus_grid_arbitrage") or {}).get("options") or []
    grid_by_capacity = {float(option.get("capacity_kwh") or 0.0): option for option in grid_options}
    recommended = comparison.get("recommended", {})
    primary = comparison.get("battery_recommendation") or {}
    data_quality = comparison.get("data_quality", {})
    if not comparison.get("available"):
        reason = data_quality.get("note") or "Battery modelling is unavailable for this period."
        return f"""
        <div class="scenario-insights">
          <div class="chart-heading"><h3>Home battery sizing</h3><span>Actual interval model</span></div>
          <p>{html.escape(str(reason))}</p>
        </div>
        """

    recommended_capacity = float(recommended.get("capacity_kwh") or 0.0)
    tariff_name = str(comparison.get("tariff_name") or comparison.get("tariff_metadata", {}).get("tariff_name") or "selected tariff")
    effective_text = str(comparison.get("tariff_effective_from") or comparison.get("tariff_metadata", {}).get("effective_from") or "")
    try:
        effective_label = human_date(date.fromisoformat(effective_text))
    except ValueError:
        effective_label = effective_text
    tariff_note = (
        f"Calculated using {tariff_name} tariff effective from {effective_label}."
        if effective_label
        else f"Calculated using {tariff_name} tariff."
    )
    primary_source = str(primary.get("source") or "current_period")
    current_label = "Best for selected period" if primary_source == "annual_historical" else "Recommended"
    rows = [battery_strategy_row_html(baseline, None, recommended_capacity, baseline=True, recommended_label=current_label)]
    for option in options:
        rows.append(battery_strategy_row_html(option, grid_by_capacity.get(float(option.get("capacity_kwh") or 0.0)), recommended_capacity, recommended_label=current_label))
    opportunity = comparison.get("opportunity_metrics", {}) or {}
    grid_strategy = strategies.get("solar_plus_grid_arbitrage") or {}
    grid_recommended = grid_strategy.get("recommended") or {}
    matching_grid = grid_by_capacity.get(recommended_capacity) or (grid_options[0] if grid_options else {})
    additional_grid = float(matching_grid.get("additional_savings_from_grid_charging") or 0.0)
    grid_energy = float(matching_grid.get("grid_energy_used_to_charge_battery_kwh") or 0.0)
    grid_delivered = float(matching_grid.get("grid_charged_delivered_kwh") or 0.0)
    cheapest_bucket = str((comparison.get("tariff_context") or {}).get("cheapest_bucket") or "the cheapest window").replace("_", " ")
    cheapest_rate = float((comparison.get("tariff_context") or {}).get("cheapest_rate") or 0.0)
    efficiency = float((comparison.get("assumptions") or {}).get("round_trip_efficiency_pct") or 0.0)
    if grid_energy > 0 and additional_grid > 0:
        grid_explanation = (
            f"Charging a {recommended_capacity:g} kWh battery during the {cheapest_bucket} window at {money(cheapest_rate)}/kWh "
            f"adds about {money(additional_grid)} this period after charging losses, based on {kwh(grid_energy)} of grid energy "
            f"delivering {kwh(grid_delivered)} from the battery."
        )
    else:
        grid_explanation = (
            "Cheap-grid charging was modelled, but this period does not show a material extra benefit beyond the solar-only battery case."
        )
    tariff_history_note = battery_tariff_history_note_html(comparison)

    return f"""
    <div class="scenario-insights battery-comparison">
      <div class="chart-heading"><h3>Home battery sizing</h3><span>What battery should I buy?</span></div>
      {tariff_history_note}
      {battery_primary_recommendation_html(comparison, tariff_name)}
      {battery_why_this_size_html(comparison)}
      {battery_tradeoff_cards_html(comparison)}
      {battery_annual_sizing_table_html(comparison)}
      {battery_accordion_html("Current billing-period evidence", "Selected-period context", battery_current_period_result_html(rows, recommended, grid_recommended, grid_explanation, opportunity, primary_source, comparison))}
      {battery_accordion_html("Seasonal evidence", "Historical seasonal context", battery_seasonal_model_html(comparison.get("seasonal_model"), comparison) or '<p class="scenario-note">Seasonal battery modelling needs interval-grade historical import/export data.</p>')}
      {battery_accordion_html("Methodology", "Data, tariffs, dispatch assumptions and limitations", battery_methodology_html(comparison, grid_explanation, tariff_note, efficiency))}
    </div>
    """


def battery_tariff_history_note_html(comparison: dict) -> str:
    periods = comparison.get("tariff_periods_used") or []
    warning = str(comparison.get("tariff_history_warning") or "")
    if len(periods) <= 1 and not warning:
        return ""
    names = ", ".join(str(item.get("tariff_name") or "tariff") for item in periods[:3])
    text = warning or f"Annual battery modelling includes historical {names} pricing."
    return f'<p class="scenario-note tariff-context-inline">{html.escape(text)}</p>'


def battery_accordion_html(title: str, summary: str, body: str) -> str:
    return f"""
      <details class="battery-accordion">
        <summary><span>{html.escape(title)}</span><small>{html.escape(summary)}</small></summary>
        <div class="battery-accordion-body">{body}</div>
      </details>
    """


def battery_primary_recommendation_html(comparison: dict, tariff_name: str) -> str:
    primary = comparison.get("battery_recommendation") or {}
    size = float(primary.get("recommended_size_kwh") or 0.0)
    confidence = str(primary.get("confidence") or "Low")
    interval_days = int(primary.get("interval_days") or 0)
    seasons = primary.get("seasons_included") or []
    source = str(primary.get("source") or "current_period")
    periods = primary.get("tariff_periods_used") or comparison.get("tariff_periods_used") or []
    tariff_basis = "Historical tariff changes included" if len(periods) > 1 else "Current tariff period"
    season_basis = (
        "All 4 seasons"
        if len(seasons) == 4
        else f"{len(seasons)} season{'s' if len(seasons) != 1 else ''}"
        if seasons
        else "Selected period only"
    )
    basis = (
        f"Based on {interval_days} days of interval history across {season_basis.lower()}."
        if source == "annual_historical"
        else "Based on the selected billing period because annual interval history is not yet strong enough."
    )
    coverage = f"{interval_days} interval days, {season_basis}" if source == "annual_historical" else "Selected billing period"
    warning_text = " ".join(str(item) for item in primary.get("warnings") or [])
    confidence_note = (
        "PowerMonitor has enough annual history for this recommendation."
        if source == "annual_historical"
        else "Annual history is not yet strong enough, so this uses the selected period."
    )
    based_items = [
        f"{interval_days} interval days" if source == "annual_historical" else "Selected billing-period interval data",
        season_basis,
        tariff_basis,
        "Current and previous household tariff periods" if len(periods) > 1 else f"Current tariff: {tariff_name}",
    ]
    return f"""
      <div class="battery-primary-summary">
        <div class="chart-heading"><h3>Recommended battery</h3><span>{html.escape('Annual history' if source == 'annual_historical' else 'Current period')}</span></div>
        <div class="battery-metrics battery-investment-cards">
          {battery_metric("Recommended size", f"{size:g} kWh usable", basis)}
          {battery_metric("Estimated annual bill reduction", money(float(primary.get("annual_saving") or 0.0)), "From the selected recommendation source")}
          {battery_metric("Confidence", confidence, confidence_note)}
          {battery_metric("Historical coverage", coverage, "Data behind the primary recommendation")}
          {battery_metric("Current tariff", tariff_name, "Tariff calculations are unchanged")}
        </div>
        <div class="battery-based-on">
          <strong>Based on</strong>
          <ul>{"".join(f"<li>{html.escape(item)}</li>" for item in based_items if item)}</ul>
          <a href="#battery-investment">See estimated cost and payback</a>
        </div>
      </div>
    """


def battery_recommendation_context(comparison: dict) -> dict[str, Any]:
    options = sorted(
        [option for option in battery_investment_source_options(comparison) if float(option.get("capacity_kwh") or 0.0) > 0],
        key=lambda option: float(option.get("capacity_kwh") or 0.0),
    )
    primary = comparison.get("battery_recommendation") or {}
    recommended_size = float(primary.get("recommended_size_kwh") or (comparison.get("recommended") or {}).get("capacity_kwh") or 0.0)
    recommended = next(
        (option for option in options if abs(float(option.get("capacity_kwh") or 0.0) - recommended_size) < 1e-9),
        options[0] if options else {},
    )
    index = options.index(recommended) if recommended in options else -1
    smaller = options[index - 1] if index > 0 else None
    larger = options[index + 1] if 0 <= index < len(options) - 1 else None
    recommended_saving = float(recommended.get("annualised_savings") or 0.0)
    smaller_saving = float((smaller or {}).get("annualised_savings") or 0.0)
    larger_saving = float((larger or {}).get("annualised_savings") or 0.0)
    return {
        "options": options,
        "recommended": recommended,
        "recommended_size": recommended_size,
        "recommended_saving": recommended_saving,
        "smaller": smaller,
        "larger": larger,
        "smaller_delta": max(recommended_saving - smaller_saving, 0.0) if smaller else None,
        "larger_delta": max(larger_saving - recommended_saving, 0.0) if larger else None,
    }


def battery_why_this_size_html(comparison: dict) -> str:
    context = battery_recommendation_context(comparison)
    recommended = context["recommended"]
    if not recommended:
        return ""
    recommended_size = float(context["recommended_size"] or 0.0)
    smaller = context["smaller"]
    larger = context["larger"]
    parts = []
    if smaller:
        parts.append(
            f"{float(smaller.get('capacity_kwh') or 0.0):g} kWh captures much of the available value, "
            f"but {recommended_size:g} kWh adds about {money(float(context['smaller_delta'] or 0.0))} per year."
        )
    else:
        parts.append(f"{recommended_size:g} kWh is the smallest tested size with a measurable household benefit.")
    if larger:
        parts.append(
            f"Moving to {float(larger.get('capacity_kwh') or 0.0):g} kWh adds about {money(float(context['larger_delta'] or 0.0))} per year, "
            f"so {recommended_size:g} kWh is the best overall fit."
        )
    else:
        parts.append("Larger tested sizes did not provide a better overall fit.")
    return f"""
      <div class="battery-why-size">
        <h4>Why this size?</h4>
        <p>{html.escape(" ".join(parts))}</p>
      </div>
    """


def battery_tradeoff_cards_html(comparison: dict) -> str:
    context = battery_recommendation_context(comparison)
    smaller = context["smaller"]
    larger = context["larger"]
    if not smaller and not larger:
        return ""
    smaller_html = (
        battery_tradeoff_card_html(
            "Smaller option",
            smaller,
            f"Saves approximately {money(float(context['smaller_delta'] or 0.0))} less per year",
            "Lower upfront cost. May suit a tighter budget.",
        )
        if smaller
        else ""
    )
    larger_signal = str((larger or {}).get("recommendation_signal") or "Limited extra value")
    larger_html = (
        battery_tradeoff_card_html(
            "Larger option",
            larger,
            f"Adds approximately {money(float(context['larger_delta'] or 0.0))} per year",
            f"{homeowner_battery_signal(larger_signal)}. Longer payback likely.",
        )
        if larger
        else ""
    )
    return f'<div class="battery-tradeoff-grid">{smaller_html}{larger_html}</div>'


def battery_tradeoff_card_html(title: str, option: Optional[dict], headline: str, detail: str) -> str:
    if not option:
        return ""
    return f"""
      <div class="battery-tradeoff-card">
        <span>{html.escape(title)}</span>
        <strong>{float(option.get("capacity_kwh") or 0.0):g} kWh usable</strong>
        <p>{html.escape(headline)}</p>
        <small>{html.escape(detail)}</small>
      </div>
    """


def homeowner_battery_signal(signal: str) -> str:
    cleaned = str(signal or "").strip()
    mapping = {
        "Good value": "Strong value",
        "Optional tariff benefit": "Strong value",
        "Limited additional benefit": "Limited extra value",
        "Likely oversized for this usage pattern": "Likely oversized",
        "Recommended": "Recommended",
    }
    return mapping.get(cleaned, cleaned or "Option")


def battery_annual_sizing_table_html(comparison: dict) -> str:
    model = comparison.get("seasonal_model") or {}
    options = battery_investment_source_options(comparison)
    if not options:
        return ""
    primary = comparison.get("battery_recommendation") or {}
    recommended_size = float(primary.get("recommended_size_kwh") or (comparison.get("recommended") or {}).get("capacity_kwh") or 0.0)
    recommended_option = next((option for option in options if abs(float(option.get("capacity_kwh") or 0.0) - recommended_size) < 1e-9), {})
    recommended_saving = float(recommended_option.get("annualised_savings") or 0.0)
    rows = ""
    for index, option in enumerate(options):
        capacity = float(option.get("capacity_kwh") or 0.0)
        saving = float(option.get("annualised_savings") or 0.0)
        extra = float(option.get("marginal_savings") or 0.0)
        versus_recommended = saving - recommended_saving
        is_recommended = abs(capacity - recommended_size) < 1e-9
        if is_recommended:
            signal = "Recommended"
        elif index == 0:
            signal = "Entry option"
        elif capacity < recommended_size:
            signal = "Strong value"
        else:
            signal = homeowner_battery_signal(str(option.get("recommendation_signal") or "Limited extra value"))
        rows += f"""
        <tr>
          <td class="battery-size-cell"><strong>{html.escape(str(option.get("label") or f"{capacity:g} kWh usable"))}</strong>{'<small>Best overall fit</small>' if is_recommended else ''}</td>
          <td class="battery-money-cell">{money(saving)}</td>
          <td class="battery-money-cell">{money(extra)}</td>
          <td class="battery-money-cell">{signed_money(versus_recommended)}</td>
          <td class="battery-recommendation-cell"><span class="battery-signal {'recommended' if is_recommended else 'neutral'}">{html.escape(signal)}</span></td>
        </tr>
        """
    return f"""
      <div class="battery-table-wrap">
        <table class="responsive-table battery-homeowner-table battery-annual-table">
          <thead><tr><th>Battery</th><th>Annual bill reduction</th><th>Extra benefit versus previous size</th><th>Benefit versus recommended</th><th>Recommendation</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p class="scenario-note">Annual values combine included seasons using transparent seasonal weighting. They are not selected-period bill savings.</p>
    """


def battery_current_period_result_html(rows: list[str], recommended: dict, grid_recommended: dict, grid_explanation: str, opportunity: dict, primary_source: str, comparison: Optional[dict] = None) -> str:
    comparison = comparison or {}
    title = "Current billing-period result" if primary_source == "annual_historical" else "Current billing-period sizing"
    lead = (
        str(recommended.get("summary") or "Battery sizing uses actual imports, exports, and tariff rates for this period.")
        if primary_source == "current_period"
        else str(recommended.get("summary") or "The selected billing period remains useful supporting context, but it is not the primary household recommendation.")
    )
    period_dates = battery_current_period_dates(comparison)
    primary = comparison.get("battery_recommendation") or {}
    annual_size = float(primary.get("recommended_size_kwh") or 0.0)
    current_size = float(primary.get("current_period_size_kwh") or recommended.get("capacity_kwh") or 0.0)
    difference_note = ""
    if primary_source == "annual_historical" and annual_size and current_size and abs(annual_size - current_size) > 1e-9:
        seasonal = comparison.get("seasonal_model") or {}
        difference_note = (
            f"This billing period favours {current_size:g} kWh because the selected-period export and import pattern is narrower. "
            f"The annual recommendation remains {annual_size:g} kWh because broader seasonal history shows greater storage opportunity."
        )
    return f"""
      <div class="battery-current-period-result">
        <div class="chart-heading"><h3>{html.escape(title)}</h3><span>{html.escape('Supporting context' if primary_source == 'annual_historical' else 'Primary source')}</span></div>
        {f'<p class="scenario-note">Selected period: {html.escape(period_dates)}</p>' if period_dates else ''}
        <p class="battery-conclusion">{html.escape(lead)}</p>
        {f'<p class="scenario-note">{html.escape(difference_note)}</p>' if difference_note else ''}
        <p class="battery-conclusion secondary">{html.escape(str(grid_recommended.get("summary") or grid_explanation))}</p>
        <div class="battery-metrics">
          {battery_metric("Solar export available", kwh(float(opportunity.get("average_daily_solar_export_available_kwh") or 0.0)), "Average per day for charging")}
          {battery_metric("Non-cheapest import", kwh(float(opportunity.get("average_daily_non_cheapest_grid_import_kwh") or 0.0)), "Average daily import a battery can economically offset")}
          {battery_metric("Battery charged", kwh(float(opportunity.get("average_daily_battery_energy_charged_kwh") or 0.0)), "Average daily charge at selected-period size")}
          {battery_metric("Battery discharged", kwh(float(opportunity.get("average_daily_battery_energy_discharged_kwh") or 0.0)), "Average daily discharge at selected-period size")}
          {battery_metric("Utilisation", f"{float(opportunity.get('average_battery_utilisation_pct') or 0.0):.0f}%", "Average capacity used each day")}
          {battery_metric("Full / depleted days", f"{int(opportunity.get('days_battery_reached_full_charge') or 0)} / {int(opportunity.get('days_battery_fully_depleted') or 0)}", "Reached full charge / fully discharged")}
          {battery_metric("Solar export left", kwh(float(opportunity.get("remaining_solar_export_kwh") or 0.0)), "After selected-period battery model")}
          {battery_metric("Next-size gain", money(float(opportunity.get("marginal_savings_to_next_capacity") or 0.0)), "Extra period saving from the next size")}
        </div>
        <div class="battery-table-wrap">
          <table class="responsive-table battery-homeowner-table">
            <thead>
              <tr>
                <th>Battery</th>
                <th>Period solar only</th>
                <th>Period solar + grid</th>
                <th>Extra period saving</th>
                <th>Period bill</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </div>
    """


def battery_current_period_dates(comparison: dict) -> str:
    days = []
    diagnostics = (comparison.get("diagnostics") or {}).get("options") or []
    for option in diagnostics:
        for row in option.get("daily_rows") or []:
            day_value = row.get("day") or row.get("date")
            if day_value:
                days.append(str(day_value))
    if not days:
        return ""
    first = min(days)
    last = max(days)
    if first == last:
        return first
    return f"{first} to {last}"


def battery_strategy_row_html(solar_option: dict, grid_option: Optional[dict], recommended_capacity: float, baseline: bool = False, recommended_label: str = "Recommended") -> str:
    capacity = float(solar_option.get("capacity_kwh") or 0.0)
    is_recommended = not baseline and abs(capacity - recommended_capacity) < 1e-9
    signal = str(solar_option.get("recommendation_signal") or ("Current baseline" if baseline else "Limited additional benefit"))
    if is_recommended:
        signal = recommended_label
    capacity_label = "No battery" if baseline else str(solar_option.get("label") or f"{capacity:g} kWh usable")
    grid_option = grid_option or {}
    solar_saving = float(solar_option.get("period_savings") or 0.0)
    grid_saving = float(grid_option.get("period_savings", solar_saving) or 0.0)
    additional = float(grid_option.get("additional_savings_from_grid_charging") or 0.0)
    estimated_bill = float(grid_option.get("estimated_bill", solar_option.get("estimated_bill", 0.0)) or 0.0)
    signal_class = "recommended" if is_recommended and recommended_label == "Recommended" else "neutral"
    row_class = "recommended" if is_recommended and recommended_label == "Recommended" else "baseline" if baseline else ""
    return f"""
    <tr class="{row_class}">
      <td class="battery-size-cell"><strong>{html.escape(capacity_label)}</strong></td>
      <td class="battery-money-cell">{money(solar_saving)}</td>
      <td class="battery-money-cell">{money(grid_saving)}</td>
      <td class="battery-money-cell">{money(additional)}</td>
      <td class="battery-money-cell">{money(estimated_bill)}</td>
      <td class="battery-recommendation-cell"><span class="battery-signal {signal_class}">{html.escape(signal)}</span></td>
    </tr>
    """


def battery_seasonal_model_html(model: Optional[dict], comparison: Optional[dict] = None) -> str:
    if not model:
        return ""
    if not model.get("available"):
        return f"""
        <div class="battery-seasonal-summary">
          <div class="chart-heading"><h3>Seasonal battery model</h3><span>Historical intervals</span></div>
          <p class="scenario-note">{html.escape(str(model.get("summary") or "Seasonal battery modelling needs interval-grade historical import/export data."))}</p>
        </div>
        """
    confidence = model.get("confidence_model") or {}
    primary_size = float(((comparison or {}).get("battery_recommendation") or {}).get("recommended_size_kwh") or (model.get("annual_recommendation") or {}).get("capacity_kwh") or 0.0)
    rows = "".join(
        f"""
        <tr>
          <td><strong>{html.escape(str(season.get("label") or season.get("season") or ""))}</strong></td>
          <td>{int(season.get("interval_days") or 0)} days</td>
          <td>{kwh(float(season.get("average_solar_generation_kwh") or 0.0))}/day</td>
          <td>{kwh(float(season.get("average_import_kwh") or 0.0))}/day</td>
          <td>{kwh(float(season.get("average_export_kwh") or 0.0))}/day</td>
          <td>{money(float(season.get("estimated_battery_saving") or 0.0))}</td>
          <td>{html.escape(seasonal_fit_label(season, primary_size))}</td>
        </tr>
        """
        for season in model.get("seasons", [])
    )
    diagnostics = model.get("diagnostics") or {}
    weights = model.get("annual_weighting") or {}
    weight_text = ", ".join(
        f"{AU_SEASON_LABELS_FOR_REPORT.get(season, season.title())} {float(weight) * 100:.0f}%"
        for season, weight in weights.items()
    )
    included_text = ", ".join(AU_SEASON_LABELS_FOR_REPORT.get(str(season), str(season).title()) for season in weights) or "none"
    excluded = model.get("excluded_periods") or []
    excluded_text = "; ".join(
        f"{AU_SEASON_LABELS_FOR_REPORT.get(str(item.get('season')), str(item.get('season')).title())}: {item.get('reason')}"
        for item in excluded
    ) or "none"
    interval_days = int(diagnostics.get("seasonal_interval_days") or confidence.get("interval_days") or 0)
    mismatch = model.get("recommendation_mismatch") or {}
    mismatch_note = (
        f'<p class="scenario-note">{html.escape(str(mismatch.get("reason") or ""))}</p>'
        if mismatch.get("exists")
        else ""
    )
    return f"""
    <div class="battery-seasonal-summary">
      <div class="chart-heading"><h3>Seasonal battery model</h3><span>{html.escape(str(confidence.get("level") or "Low"))} confidence</span></div>
      <div class="battery-seasonal-table-wrap">
        <table class="responsive-table battery-homeowner-table battery-seasonal-table">
          <thead><tr><th>Season</th><th>Interval days</th><th>Average solar</th><th>Average import</th><th>Average export</th><th>Annual-equivalent battery benefit</th><th>Seasonal fit</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p class="battery-conclusion secondary">{html.escape(str(model.get("summary") or ""))}</p>
      {mismatch_note}
      <p class="scenario-note">Annual weighting: {html.escape(weight_text or "unavailable")}. Annual recommendation combines all included seasons using transparent seasonal weighting. Included seasons: {html.escape(included_text)}. Excluded seasons: {html.escape(excluded_text)}. Total interval days: {interval_days}. Coverage: {float(diagnostics.get("coverage_percentage") or 0.0):.0f}% across stored interval history. {html.escape(str(confidence.get("reason") or ""))}</p>
      <details class="battery-calculation-details">
        <summary>Seasonal diagnostics</summary>
        <pre>{html.escape(json.dumps({"coverage_by_season": diagnostics.get("coverage_by_season"), "annual_weighting": diagnostics.get("annual_weighting"), "excluded_periods": diagnostics.get("excluded_periods"), "confidence": confidence}, indent=2, sort_keys=True))}</pre>
      </details>
    </div>
    """


def seasonal_fit_label(season: dict, primary_size: float) -> str:
    size = float(season.get("recommended_battery_kwh") or 0.0)
    if size <= 0:
        return "No clear fit"
    if primary_size and size > primary_size:
        return f"Higher {str(season.get('label') or 'seasonal').lower()} opportunity"
    if primary_size and size < primary_size:
        return f"{size:g} kWh fit"
    return f"{size:g} kWh fit"


def battery_investment_html(investment: Optional[dict]) -> str:
    if not investment or not investment.get("available"):
        return ""
    assumptions = investment.get("assumptions") or {}
    payback = investment.get("simple_payback_years")
    payback_label = f"{float(payback):.1f} years" if payback is not None else "Not recovered"
    ten_year_net = float(investment.get("ten_year_net") or 0.0)
    fifteen_year_net = float(investment.get("fifteen_year_net") or 0.0)
    assumption_sentence = (
        f"Projection assumes {int(assumptions.get('battery_life_years') or 15)}-year battery life, "
        f"{float(assumptions.get('annual_degradation_pct') or 0.0):g}% annual degradation, "
        f"{float(assumptions.get('electricity_inflation_pct') or 0.0):g}% electricity price growth "
        "and current usage patterns."
    )
    rebate = float(investment.get("rebate") or 0.0)
    cost_note = f"Installed cost {money(float(investment.get('installed_cost') or 0.0))}"
    if rebate > 0:
        cost_note += f" less {money(rebate)} rebate"
    details_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(str(option.get("label") or ""))}</td>
          <td>{money(float(option.get("installed_cost") or 0.0))}</td>
          <td>{money(float(option.get("rebate") or 0.0))}</td>
          <td>{money(float(option.get("net_cost") or 0.0))}</td>
          <td>{money(float(option.get("annual_saving") or 0.0))}</td>
          <td>{f"{float(option['simple_payback_years']):.1f} years" if option.get("simple_payback_years") is not None else "Not recovered"}</td>
          <td>{money(float(option.get("ten_year_estimated_savings") or 0.0))}</td>
          <td>{signed_money(float(option.get("ten_year_net") or 0.0))}</td>
          <td>{money(float(option.get("fifteen_year_estimated_savings") or 0.0))}</td>
          <td>{signed_money(float(option.get("fifteen_year_net") or 0.0))}</td>
        </tr>
        """
        for option in investment.get("options", [])
    )
    return f"""
    <div class="scenario-insights battery-investment" id="battery-investment">
      <div class="chart-heading"><h3>Battery investment</h3><span>Purchase economics</span></div>
      <p class="battery-conclusion">{html.escape(str(investment.get("summary") or ""))}</p>
      <div class="battery-metrics battery-investment-cards">
        {battery_metric("Estimated investment", money(float(investment.get("net_cost") or 0.0)), cost_note)}
        {battery_metric("Estimated annual saving", money(float(investment.get("annual_saving") or 0.0)), "From existing battery simulation")}
        {battery_metric("Simple payback", payback_label, str(investment.get("confidence_reason") or ""))}
        {battery_metric("10-year outcome", signed_money(ten_year_net), "Net benefit after purchase cost")}
        {battery_metric("15-year outcome", signed_money(fifteen_year_net), "Net benefit after purchase cost")}
        {battery_metric("Overall recommendation", str(investment.get("overall_recommendation") or "Not recommended"), f"{investment.get('confidence', 'Low')} confidence")}
      </div>
      <p class="scenario-note">{html.escape(assumption_sentence)}</p>
      <details class="battery-calculation-details">
        <summary>How was this calculated?</summary>
        <p>Annual savings come from the existing simulated battery savings. The investment projection applies the configured installed cost, rebate, annual degradation, electricity price inflation and discount rate without rerunning battery dispatch.</p>
        <table class="responsive-table battery-investment-table">
          <thead><tr><th>Battery</th><th>Installed cost</th><th>Rebate</th><th>Net price</th><th>Annual saving</th><th>Payback</th><th>10-year savings</th><th>10-year net</th><th>15-year savings</th><th>15-year net</th></tr></thead>
          <tbody>{details_rows}</tbody>
        </table>
      </details>
    </div>
    """


def battery_metric(label: str, value: str, note: str) -> str:
    return f"""
    <div>
      <span>{html.escape(label)}</span>
      <strong>{html.escape(value)}</strong>
      <small>{html.escape(note)}</small>
    </div>
    """


def battery_calculation_details_html(comparison: dict) -> str:
    baseline = comparison.get("baseline", {})
    options = comparison.get("options", [])
    grid_options = ((comparison.get("strategies") or {}).get("solar_plus_grid_arbitrage") or {}).get("options") or []
    grid_by_capacity = {float(option.get("capacity_kwh") or 0.0): option for option in grid_options}
    rows = [
        f"""
        <tr>
          <td>No battery</td><td>Baseline</td><td>{kwh(0.0)}</td><td>{kwh(0.0)}</td><td>{kwh(0.0)}</td>
          <td>{kwh(float(baseline.get("remaining_grid_import_kwh") or 0.0))}</td>
          <td>{kwh(float(baseline.get("remaining_solar_export_kwh") or 0.0))}</td>
          <td>{money(0.0)}</td><td>{money(0.0)}</td><td>{money(0.0)}</td>
        </tr>
        """
    ]
    for option in options:
        grid = grid_by_capacity.get(float(option.get("capacity_kwh") or 0.0), {})
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(option.get("label") or ""))}</td>
              <td>Solar only</td>
              <td>{kwh(float(option.get("peak_import_avoided_kwh") or 0.0))}</td>
              <td>{kwh(float(option.get("other_import_avoided_kwh") or 0.0))}</td>
              <td>{kwh(0.0)}</td>
              <td>{kwh(float(option.get("remaining_grid_import_kwh") or 0.0))}</td>
              <td>{kwh(float(option.get("remaining_solar_export_kwh") or 0.0))}</td>
              <td>{money(float(option.get("marginal_savings_to_next_capacity") or 0.0))}</td>
              <td>{money(float(option.get("average_cost_per_delivered_battery_kwh") or 0.0))}</td>
              <td>{money(float(option.get("average_avoided_grid_rate_per_discharged_kwh") or 0.0))}</td>
            </tr>
            <tr>
              <td>{html.escape(str(option.get("label") or ""))}</td>
              <td>Solar + cheap grid</td>
              <td>{kwh(float(grid.get("peak_import_avoided_kwh") or 0.0))}</td>
              <td>{kwh(float(grid.get("other_import_avoided_kwh") or 0.0))}</td>
              <td>{kwh(float(grid.get("grid_energy_used_to_charge_battery_kwh") or 0.0))} input / {kwh(float(grid.get("charging_losses_kwh") or 0.0))} losses</td>
              <td>{kwh(float(grid.get("remaining_grid_import_kwh") or 0.0))}</td>
              <td>{kwh(float(grid.get("remaining_solar_export_kwh") or 0.0))}</td>
              <td>{money(float(grid.get("marginal_savings_to_next_capacity") or 0.0))}</td>
              <td>{money(float(grid.get("average_cost_per_delivered_battery_kwh") or 0.0))}</td>
              <td>{money(float(grid.get("average_avoided_grid_rate_per_discharged_kwh") or 0.0))}</td>
            </tr>
            """
        )
    return f"""
    <div class="battery-calculation-details">
      <h4>Detailed dispatch table</h4>
      <div class="battery-diagnostics-table battery-methodology-table">
        <table class="responsive-table">
        <thead><tr><th>Option</th><th>Strategy</th><th>Peak avoided</th><th>Other avoided</th><th>Grid charge / losses</th><th>Import left</th><th>Export left</th><th>Next-size gain</th><th>Battery kWh cost</th><th>Avoided rate</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      </div>
    </div>
    """


def battery_methodology_html(comparison: dict, grid_explanation: str, tariff_note: str, efficiency: float) -> str:
    assumptions = comparison.get("assumptions") or {}
    periods = comparison.get("tariff_periods_used") or []
    period_rows = "".join(
        f"<li>{html.escape(str(item.get('tariff_name') or 'Unknown tariff'))}: {int(item.get('interval_count') or 0)} intervals</li>"
        for item in periods
    )
    return f"""
      <div class="battery-methodology">
        <h4>Data used</h4>
        <p>{html.escape(str(comparison.get("methodology") or "Battery modelling uses interval import and export readings."))}</p>
        <h4>Tariffs used</h4>
        <p>{html.escape(tariff_note)}</p>
        {f'<ul class="guidance-list">{period_rows}</ul>' if period_rows else ''}
        <h4>Battery dispatch assumptions</h4>
        <p>Each local day starts empty and resets at day end. Solar-only modelling charges only from verified excess solar export. Usable sizes modelled: {html.escape(", ".join(f"{float(value):g} kWh" for value in assumptions.get("usable_capacities_kwh", [])))}.</p>
        <h4>Grid charging assumptions</h4>
        <p>{html.escape(grid_explanation)} Cheap-grid battery charging is only modelled during periods where the active tariff includes a suitable cheap window.</p>
        <h4>Limitations</h4>
        <p>Equipment cost, degradation and battery life are handled in Battery investment. Actual inverter limits, backup reserve settings and manufacturer behaviour may reduce savings. Round-trip efficiency is modelled at {efficiency:.0f}% for grid-charged energy.</p>
        {battery_calculation_details_html(comparison)}
      </div>
    """


def scenario_analysis_html(analysis: dict) -> str:
    scenarios = analysis.get("scenarios", [])
    if not scenarios and not analysis.get("battery_comparison"):
        return ""
    cards = []
    for scenario in scenarios:
        difference = float(scenario["difference_vs_actual"])
        tone = "good" if difference < 0 else "bad" if difference > 0 else "neutral"
        impact = (
            f"Saves {money(-difference)}"
            if difference < 0
            else f"Costs {money(difference)} more"
            if difference > 0
            else "No change"
        )
        cards.append(
            f"""
            <article class="scenario-card {tone}">
              <span>{html.escape(str(scenario["confidence"]).title())} confidence</span>
              <h3>{html.escape(scenario["title"])}</h3>
              <strong>{money(float(scenario["estimated_bill"]))}</strong>
              <p>{html.escape(impact)} vs actual. {html.escape(scenario["description"])}</p>
            </article>
            """
        )
    insights = analysis.get("ev_insights", [])
    insights_html = ""
    if insights:
        insights_html = f"""
        <div class="scenario-insights">
          <div class="chart-heading"><h3>EV scenario insights</h3><span>Charging behaviour</span></div>
          <ul>{"".join(f"<li>{html.escape(item)}</li>" for item in insights)}</ul>
        </div>
        """
    return f"""
    <section class="report-section scenario-section">
      <div class="section-title"><div><span class="eyebrow">Decisions</span><h2>Scenario analysis</h2></div><p>Estimated bill compared with actual</p></div>
      <div class="scenario-grid">{''.join(cards)}</div>
      {battery_comparison_html(analysis.get("battery_comparison"))}
      {battery_investment_html(analysis.get("battery_investment"))}
      {insights_html}
    </section>
    """


def whatif_page_html(
    analysis: dict,
    plan: dict,
    recommendations: list,
    goals: Optional[list[GoalProgress]] = None,
) -> str:
    savings = plan["savings"]
    meaning = (
        f"The EV Add On plan is ahead by {money(savings)} for this period."
        if savings > 0
        else f"The Flat/Home plan is ahead by {money(-savings)} for this period."
        if savings < 0
        else "The current and comparison plans are roughly even this period."
    )
    shared_recommendation = next(
        (
            item for item in recommendations
            if recommendation_category(item) in {"EV Charging", "Tariffs", "Bills", "Billing"}
        ),
        recommendations[0] if recommendations else None,
    )
    best_move = recommendation_action(shared_recommendation) if shared_recommendation else "No active recommendation for this scenario."
    return f"""
    {scenario_analysis_html(analysis)}
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Decision assistant</span><h2>What this means</h2></div></div>
      <div class="decision-grid">
        <div><span>Flat-rate vs EV Add On scenario</span><strong>{html.escape(meaning)}</strong></div>
        <div><span>Best next move</span><strong>{html.escape(best_move)}</strong></div>
      </div>
    </section>
    {goals_progress_html(goals or [])}
    """


def write_html(
    today_summary: Summary,
    period_summary: Summary,
    trends: list[DailyTrend],
    settings: Settings,
    health: dict,
    recommendations: list[Decision],
    selected_period: Optional[Period] = None,
    comparison: Optional[dict] = None,
    output_path: Optional[Path] = None,
    timeline: Optional[DecisionTimeline] = None,
    recommendation_lifecycle_counts: Optional[dict[str, int]] = None,
    hidden: Optional[list[Decision]] = None,
    billing_summary: Optional[Summary] = None,
    glance_only: bool = False,
    overnight_baseload_watts: Optional[float] = None,
    provider_flash: Optional[dict] = None,
    tariff_update_flash: Optional[dict] = None,
    historical_backfill_flash: Optional[dict] = None,
    period_flash: Optional[dict] = None,
    runtime_check: Optional[dict] = None,
    energy_timeline: Optional[EnergyTimeline] = None,
) -> str:
    selected_period = selected_period or Period(
        "current_billing_period", "Current billing period", period_summary.start, period_summary.end
    )
    if current_context() is None:
        with performance_context(period_payload(selected_period)):
            return write_html(
                today_summary,
                period_summary,
                trends,
                settings,
                health,
                recommendations,
                selected_period,
                comparison,
                output_path=output_path,
                timeline=timeline,
                recommendation_lifecycle_counts=recommendation_lifecycle_counts,
                hidden=hidden,
                billing_summary=billing_summary,
                glance_only=glance_only,
                overnight_baseload_watts=overnight_baseload_watts,
                provider_flash=provider_flash,
                tariff_update_flash=tariff_update_flash,
                historical_backfill_flash=historical_backfill_flash,
                period_flash=period_flash,
                runtime_check=runtime_check,
                energy_timeline=energy_timeline,
            )
    plan = plan_comparison(period_summary, settings)
    try:
        engine = data_engine()
        battery_intervals = battery_dispatch_intervals(
            engine,
            selected_period.start,
            selected_period.end,
            settings,
        )
        seasonal_battery_intervals = battery_historical_dispatch_intervals(
            engine,
            selected_period.end - timedelta(days=364),
            selected_period.end,
            settings,
        )
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        battery_intervals = []
        seasonal_battery_intervals = []
    scenarios = scenario_analysis(
        period_summary,
        settings,
        battery_intervals=battery_intervals,
        seasonal_battery_intervals=seasonal_battery_intervals,
    )
    completeness = data_completeness(period_summary, settings)
    energy_score = build_energy_score(period_summary, recommendations, completeness, plan)
    timeline = timeline or build_decision_timeline(
        period_summary,
        recommendations,
        comparison,
        include_billing_period=selected_period.kind == "current_billing_period",
    )
    billing_summary = billing_summary or period_summary
    runtime_check = runtime_check or runtime_check_data()
    household_context = report_household_context()
    try:
        html_data_engine = data_engine()
        capability_summary = html_data_engine.get_household_capability_summary(local_today(), settings)
        provider_observations = html_data_engine.get_provider_observation_summary()
        promotion_summary = html_data_engine.promote_provider_observations()
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        capability_summary = None
        provider_observations = None
        promotion_summary = None
    compared_trends: list[DailyTrend] = []
    if comparison:
        try:
            period_b = comparison.get("period_b") or {}
            previous_start = date.fromisoformat(str(period_b.get("start")))
            previous_end = date.fromisoformat(str(period_b.get("end")))
            with sqlite3.connect(DB_PATH) as conn:
                validate_database(conn)
                compared_trends = load_daily_trends(conn, previous_start, previous_end, settings)
        except (TypeError, ValueError, FileNotFoundError, RuntimeError, sqlite3.Error):
            compared_trends = []
    glance_projected, glance_end, glance_basis, _glance_impact = financial_engine(household_context).project_bill(
        billing_summary,
        settings,
    )
    comparison_previous_summary = comparison.get("summary_b") if comparison else None
    bill_change = financial_engine(household_context).explain_bill_change(
        billing_summary,
        comparison_previous_summary,
        settings,
        current_bill=glance_projected,
        previous_bill=(
            comparison_previous_summary.get("net_cost")
            if isinstance(comparison_previous_summary, dict)
            else getattr(comparison_previous_summary, "net_cost", None)
            if comparison_previous_summary is not None
            else None
        ),
        projected=True,
    )
    try:
        actual_bill_comparison = data_engine().compare_projected_to_latest_synergy_bill(glance_projected)
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        actual_bill_comparison = None
    generated_dt = datetime.now().astimezone()
    generated_at = generated_dt.strftime("%d %b %Y, %H:%M %Z")
    if energy_timeline is None:
        try:
            with sqlite3.connect(DB_PATH) as timeline_conn:
                validate_database(timeline_conn)
                energy_timeline = build_energy_timeline(timeline_conn, local_today(), settings, now=generated_dt)
        except (FileNotFoundError, RuntimeError, sqlite3.Error):
            energy_timeline = EnergyTimelineEngine().build(day=local_today(), readings=[], settings=settings, now=generated_dt)
    try:
        with sqlite3.connect(DB_PATH) as historical_conn:
            validate_database(historical_conn)
            historical_trends = build_historical_trends(historical_conn, local_today(), settings)
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        historical_trends = HistoricalTrendsEngine().build([], as_of=local_today())
    overnight_baseload_watts = float(overnight_baseload_watts or 0.0)
    import_sources = setup_import_sources()
    provider_registry = build_provider_registry(
        settings=settings,
        health=health,
        capability_summary=capability_summary,
        import_sources=import_sources,
    )
    provider_management = build_provider_management(
        provider_registry=provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        settings=settings,
    )
    with perf_stage("provider/health diagnostics"):
        provider_runtime = provider_runtime_data()
    household_state = HouseholdStateEngine().build(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=settings,
        health=health,
        capability_summary=capability_summary,
        provider_management=provider_management,
        provider_runtime=provider_runtime,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        household_id=household_context.household.household_id,
        now=generated_dt,
    )
    household_current_context = ContextEngine().build(
        today_summary=today_summary,
        billing_summary=billing_summary,
        settings=settings,
        health=health,
        now=generated_dt,
        household_state=household_state,
    )
    snapshot_engine = data_engine()
    state_snapshot = snapshot_engine.persist_household_state_snapshot(
        household_state,
        context=household_current_context,
    )
    latest_state_snapshot = snapshot_engine.get_latest_household_state_snapshot()
    recent_state_snapshots = snapshot_engine.get_recent_household_state_snapshots(limit=5)
    state_change_detection = snapshot_engine.detect_and_persist_state_changes()
    state_change_summary = snapshot_engine.get_state_change_summary()
    recent_state_changes = snapshot_engine.get_recent_state_changes(limit=10)
    notification_preferences = snapshot_engine.get_notification_preferences()
    notification_history = snapshot_engine.get_recent_notification_history(limit=50)
    behaviour_end = selected_period.end
    household_behaviour = snapshot_engine.get_household_behaviour(
        behaviour_end - timedelta(days=89),
        behaviour_end,
        ev_sources=actual_ev_sources(settings),
    )
    anomaly_baseline = comparison.get("summary_b") if comparison else None
    household_anomalies = snapshot_engine.get_household_anomalies(
        household_state=household_state,
        context=household_current_context,
        state_changes=recent_state_changes,
        behaviour=household_behaviour,
        current_observations={
            "overnight_baseload_watts": overnight_baseload_watts,
            "current_daily_cost": period_summary.average_daily_net_cost,
            "normal_daily_cost": (
                anomaly_baseline.get("average_daily_net_cost")
                if isinstance(anomaly_baseline, dict)
                else anomaly_baseline.average_daily_net_cost
                if anomaly_baseline else None
            ),
        },
    )
    household_decision_output = snapshot_engine.get_household_decisions(
        household_state=household_state,
        context=household_current_context,
        behaviour=household_behaviour,
        anomalies=household_anomalies,
        capability_summary=capability_summary,
        financial_metadata={
            "projected_bill": glance_projected,
            "high_cost_threshold": settings.recommendation_thresholds.get("high_cost_threshold"),
        },
        provider_health=provider_management,
        state_changes=recent_state_changes,
        now=generated_dt,
    )
    household_decisions = household_decision_output["decisions"]
    decision_summary = household_decision_output["summary"]
    timeline = build_decision_timeline(
        period_summary,
        recommendations,
        comparison,
        include_billing_period=selected_period.kind == "current_billing_period",
        household_decisions=household_decisions,
    )
    charging_advice = build_charging_advice(
        today_summary,
        billing_summary,
        settings,
        health,
        recommendations,
        glance_projected,
        generated_dt,
        household_context,
        household_current_context,
    )
    previous_summary = comparison.get("summary_b") if comparison else None
    cost = build_cost_advisor(
        period_summary,
        previous_summary,
        settings,
        health,
        recommendations,
        timeline,
        comparison,
        generated_dt,
        household_context,
        household_current_context,
    )
    solar = build_solar_advisor(
        period_summary,
        settings,
        health,
        recommendations,
        timeline,
        generated_dt,
        household_context,
        household_current_context,
    )
    efficiency = build_efficiency_advisor(
        period_summary,
        previous_summary,
        settings,
        health,
        recommendations,
        timeline,
        comparison,
        trends,
        overnight_baseload_watts,
        generated_dt,
        household_context,
        household_current_context,
    )
    advisors = AdvisorCollection(
        attach_decision_to_advisors([charging_advice.advisor, cost, solar, efficiency], household_decisions),
        household=household_context.household,
        capability_summary=capability_summary,
    )
    decision_advisors = explain_household_decisions(household_decisions, existing_advisors=advisors.active())
    advisor_explanations = AdvisorCollection(
        decision_advisors,
        household=household_context.household,
        capability_summary=capability_summary,
    )
    decision_pipeline = build_decision_pipeline_output(
        household_decisions,
        advisor_explanations=advisor_explanations.active(),
        household_state=household_state,
        context=household_current_context,
        behaviour=household_behaviour,
    )
    recommendation_presentations = decision_pipeline.recommendations
    household_insights = HouseholdInsightsEngine().build(
        trends=trends,
        current_summary=period_summary,
        billing_summary=billing_summary,
        comparison=comparison,
        projected_bill=glance_projected,
        recommendations=recommendation_presentations,
    )
    household_trends = TrendsEngine().build(
        current_summary=period_summary,
        previous_summary=comparison_previous_summary,
        current_trends=trends,
        previous_trends=compared_trends,
        comparison_label=str((comparison or {}).get("period_b", {}).get("label") or "previous period"),
        insights=household_insights,
        recommendations=recommendation_presentations,
    )
    briefing = briefing_engine(household_context).build(
        advisor_explanations,
        local_today(),
        context=household_current_context,
        recommendation_presentations=recommendation_presentations,
    )
    goals = goals_engine(household_context).evaluate(advisors)
    setup_checklist = setup_engine(household_context).build_checklist(
        capability_summary,
        collector_health=health,
        import_sources=import_sources,
        advisors=advisors,
        goals=goals,
        recommendations=recommendations,
        settings=settings,
        provider_registry=provider_registry,
        provider_management=provider_management,
        solar_data_available=data_engine(conn).has_usable_solar_readings(),
        now=generated_dt,
    )
    notification_readiness = NotificationEngine().build(
        state_changes=recent_state_changes,
        household_state_snapshot=latest_state_snapshot,
        context=household_current_context,
        household_decisions=household_decisions,
        recommendation_presentations=recommendation_presentations,
        advisors=None,
        setup=setup_checklist,
        preferences=notification_preferences,
        history=notification_history,
        now=generated_dt,
    )
    notification_readiness_summary = notification_readiness.as_dict()
    snapshot_engine.persist_notification_history(notification_readiness_summary["candidates"])
    notification_history_summary = snapshot_engine.get_notification_history_summary()
    tariff_updates = tariff_update_overview(settings, billing_summary)
    historical_data = historical_coverage_data(selected_period.end)
    glance_recommendation = next(
        (item for item in recommendation_presentations if item.category.value == "ev"),
        recommendation_presentations[0] if recommendation_presentations else None,
    )
    glance = build_glance_state(
        billing_summary=billing_summary,
        charging_advice=charging_advice,
        energy_score=energy_score,
        projected_bill=glance_projected,
        bill_projection_basis=glance_basis,
        now=generated_dt,
        context=household_current_context,
        recommendation_presentation=glance_recommendation,
    )
    display_timeline = timeline_with_state_changes(timeline, recent_state_changes)
    render_started = perf_timer_start()
    body_class = ' class="glance-only"' if glance_only else ""
    chrome_html = "" if glance_only else f"""
  {hero_html(period_summary, generated_at, health, recommendations, selected_period.label)}
  {authentication_alert_html(health)}
  {period_controls_html(selected_period, period_flash)}
  {nav_html()}
"""
    pages_html = (
        f"""<section id="glance" class="page glance-page">
    {glance_page_html(glance)}
  </section>"""
        if glance_only
        else f"""
  <section id="glance" class="page glance-page">
    {glance_page_html(glance)}
  </section>
  <section id="home" class="page">
    {home_page_html(briefing, goals, setup_checklist, glance, bill_change, household_insights, household_trends, health, energy_timeline, recommendation_presentations, display_timeline, billing_summary, historical_trends, glance_end)}
  </section>
  <section id="bills" class="page">
    {bills_page_html(period_summary, glance_projected, plan, recommendation_presentations, actual_bill_comparison, bill_change, comparison, settings)}
  </section>
  <section id="solar" class="page">
    {page_heading_html("How well is my solar performing?", "Generation, self-consumption, exports, and lost opportunity.")}
    {solar_page_html(period_summary, recommendation_presentations)}
  </section>
  <section id="ev" class="page">
    {page_heading_html("What did my vehicle cost to charge?", "Actual charging cost, tariff split, and session history.")}
    {ev_page_html(period_summary, settings)}
  </section>
  <section id="whatif" class="page">
    {page_heading_html("Decisions", "Simple scenario cards for tariff, charging, solar, and battery choices.")}
    {whatif_page_html(scenarios, plan, recommendation_presentations, goals)}
    <div id="recommendations">
      {page_heading_html("Recommendation Centre", "The most useful household actions, with plain-language reasons and confidence.", parent="Decisions")}
      {recommendation_centre_html(recommendation_presentations, recommendation_lifecycle_counts, [], [], goals, household_decisions)}
      {advisor_centre_html(advisor_explanations.active())}
    </div>
  </section>
  <section id="analytics" class="page">
    {history_page_html(trends, settings, household_insights, energy_timeline, display_timeline, historical_trends)}
  </section>
  <section id="system" class="page">
    {page_heading_html("Settings", "Setup, imports, provider status, confidence, and privacy-safe diagnostics.")}
    <div id="setup">
    {setup_page_html(setup_checklist, settings, promotion_summary, notification_preferences.as_dict(), provider_management, tariff_updates, tariff_update_flash)}
    </div>
    <div id="imports">
      {page_heading_html("Upload household data", "Preview and import supported local household exports.", parent="Settings")}
      {imports_page_html(capability_summary=capability_summary, setup=setup_checklist, provider_registry=provider_registry, provider_management=provider_management, provider_observations=provider_observations)}
    </div>
    {historical_coverage_html(historical_data, historical_backfill_flash)}
    {system_page_html(health, settings, period_summary, capability_summary, setup_checklist, provider_registry, provider_management, provider_runtime, provider_observations, promotion_summary, household_current_context, household_state, household_behaviour.as_dict(), household_anomalies, household_decisions, decision_summary, advisor_explanations.active(), latest_state_snapshot, recent_state_snapshots, state_change_summary, notification_readiness_summary, notification_preferences.as_dict(), notification_history_summary, provider_flash, runtime_check, battery_comparison=scenarios.get("battery_comparison"))}
  </section>
"""
    )
    footer_html = "" if glance_only else "<footer>PowerMonitor local report. Values are estimates based on available 15-minute readings.</footer>"
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PowerMonitor Dashboard</title>
  <style>
    :root {{
      color-scheme:light; --ink:#17221f; --muted:#687873; --line:#dce7e2; --paper:#fff;
      --wash:#eef4f1; --deep:#102a24; --deep-2:#174b3d; --green:#1c9b6c; --green-soft:#e7f7ef;
      --sun:#f2b84b; --sun-soft:#fff5da; --red:#dc604e; --red-soft:#fff0ed; --blue:#4a86a8;
      --blue-soft:#eaf4f8; --shadow:0 16px 44px rgba(29,55,47,.08);
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--wash); color:var(--ink); font:15px/1.5 -apple-system,
      BlinkMacSystemFont, "Segoe UI", sans-serif; -webkit-font-smoothing:antialiased; overflow-x:hidden; }}
    a,button,input,select {{ touch-action:manipulation; }}
    input,select,button {{ max-width:100%; }}
    main {{ width:min(1180px, calc(100% - 36px)); margin:24px auto 64px; }}
    .hero {{ position:relative; overflow:hidden; padding:30px; color:#fff; border-radius:26px;
      background:linear-gradient(135deg,var(--deep) 0%,var(--deep-2) 100%); box-shadow:0 24px 60px rgba(16,42,36,.24); }}
    .hero::after {{ content:""; position:absolute; width:380px; height:380px; right:-130px; top:-220px;
      border:70px solid rgba(255,255,255,.055); border-radius:50%; }}
    .hero.high-cost {{ background:linear-gradient(135deg,#45251f 0%,#7a382d 100%); }}
    .hero-alert {{ position:relative; z-index:1; display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:18px; padding:11px 14px; color:#ffe8e3; background:rgba(255,120,96,.15); border:1px solid rgba(255,190,180,.2); border-radius:12px; }}
    .hero-alert.amber {{ color:#fff1c7; background:rgba(242,184,75,.14); border-color:rgba(255,223,153,.22); }}
    .hero-alert span {{ font-size:9px; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }} .hero-alert strong {{ font-size:12px; }}
    .hero-top,.hero-summary,.section-title,.chart-heading,.bar-label {{ display:flex; align-items:center; justify-content:space-between; gap:18px; }}
    .brand {{ display:flex; align-items:center; gap:13px; }}
    .brand-mark {{ display:flex; align-items:end; gap:3px; width:42px; height:42px; padding:10px; background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.15); border-radius:13px; }}
    .brand-mark i {{ display:block; width:5px; border-radius:5px; background:#64ddb0; }}
    .brand-mark i:nth-child(1) {{ height:10px; }} .brand-mark i:nth-child(2) {{ height:18px; }} .brand-mark i:nth-child(3) {{ height:14px; }}
    .brand span,.generated span,.hero-label {{ display:block; color:rgba(255,255,255,.62); font-size:11px; font-weight:700; letter-spacing:.09em; text-transform:uppercase; }}
    h1 {{ margin:0; font-size:24px; letter-spacing:-.035em; }}
    .generated {{ position:relative; z-index:1; text-align:right; }} .generated strong {{ font-size:13px; }}
    .hero-summary {{ position:relative; z-index:1; align-items:end; margin-top:54px; }}
    .hero-summary strong {{ display:block; margin:6px 0 0; font-size:clamp(48px,8vw,76px); line-height:1; letter-spacing:-.07em; }}
    .hero-summary p {{ margin:8px 0 0; color:rgba(255,255,255,.68); }}
    .hero-note {{ max-width:340px; padding:16px 18px; background:rgba(255,255,255,.09); border:1px solid rgba(255,255,255,.12); border-radius:16px; }}
    .hero-note span {{ padding:3px 8px; color:#b7f0d7; background:rgba(56,199,144,.15); border-radius:99px; font-size:10px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
    .hero-note p {{ margin:8px 0 0; font-size:12px; line-height:1.45; }}
    .status-strip {{ position:relative; z-index:1; display:grid; grid-template-columns:repeat(5,1fr); gap:1px; margin-top:24px; overflow:hidden; background:rgba(255,255,255,.13); border:1px solid rgba(255,255,255,.12); border-radius:15px; }}
    .status-strip div {{ padding:12px 14px; background:rgba(255,255,255,.065); }} .status-strip span {{ display:block; color:rgba(255,255,255,.55); font-size:9px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
    .status-strip strong {{ display:block; margin-top:3px; font-size:11px; }} .status-value::before {{ content:""; display:inline-block; width:6px; height:6px; margin-right:6px; border-radius:50%; background:#f2b84b; }}
    .status-value.healthy::before {{ background:#64ddb0; }}
    .top-nav {{ position:sticky; top:10px; z-index:10; display:flex; flex-wrap:wrap; gap:8px; margin:18px 0; padding:10px; background:rgba(255,255,255,.86); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); backdrop-filter:blur(14px); }}
    .top-nav a {{ padding:9px 12px; color:var(--deep-2); text-decoration:none; background:#f7faf9; border:1px solid var(--line); border-radius:999px; font-size:12px; font-weight:750; }}
    .top-nav a:hover {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); }}
    .mobile-shortcuts {{ display:none; }}
    .page {{ display:none; }} #home {{ display:block; }} .page:target {{ display:block; }}
    body:has(.page:target) #home,
    body:has(#recommendations:target) #home,
    body:has(#timeline:target) #home,
    body:has(#battery-investment:target) #home,
    body:has(#setup:target) #home,
    body:has(#imports:target) #home,
    body:has(#setup-billing:target) #home,
    body:has(#setup-billing-cycle:target) #home,
    body:has(#setup-tariff:target) #home,
    body:has(#system-collector:target) #home,
    body:has(#system-live-data:target) #home,
    body:has(#system-data-quality:target) #home,
    body:has(#home-goals:target) #home,
    body:has(#energy-score-explanation:target) #home,
    body:has([id^="system-provider-"]:target) #home {{ display:none; }}
    body:has(#home:target) #home,
    body:has(#energy-score-explanation:target) #home,
    body:has(#home-goals:target) #whatif,
    body:has(#recommendations:target) #whatif,
    body:has(#battery-investment:target) #whatif,
    body:has(#timeline:target) #analytics,
    body:has(#setup:target) #system,
    body:has(#imports:target) #system,
    body:has(#setup-billing:target) #system,
    body:has(#setup-billing-cycle:target) #system,
    body:has(#setup-tariff:target) #system,
    body:has(#system-collector:target) #system,
    body:has(#system-live-data:target) #system,
    body:has(#system-data-quality:target) #system,
    body:has([id^="system-provider-"]:target) #system {{ display:block; }}
    .page-heading {{ margin-top:18px; padding:18px 22px; background:#fff; border:1px solid var(--line); border-radius:20px; box-shadow:var(--shadow); }}
    .page-heading h2 {{ margin:0; }} .page-heading p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }}
    .page-breadcrumb {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; color:var(--muted); font-size:11px; font-weight:800; }}
    .page-breadcrumb a {{ color:var(--deep-2); text-decoration:none; }} .page-breadcrumb strong {{ color:var(--ink); }}
    .report-section {{ margin-top:24px; padding:28px; background:rgba(255,255,255,.82); border:1px solid rgba(215,228,222,.9); border-radius:24px; box-shadow:var(--shadow); }}
    .compact-section {{ margin-top:16px; }}
    .auth-alert {{ margin-top:18px; padding:20px 22px; color:#812f25; background:linear-gradient(135deg,#fff3ef,#ffe6df); border:1px solid #efbcb2; border-left:6px solid var(--red); border-radius:18px; box-shadow:var(--shadow); }}
    .auth-alert-heading {{ display:flex; align-items:center; justify-content:space-between; gap:18px; }} .auth-alert-heading span {{ font-size:9px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .auth-alert-heading h2 {{ margin:2px 0 0; color:#8f3327; }} .auth-alert-heading strong {{ font-size:11px; }}
    .auth-alert ol {{ margin:14px 0 0; padding-left:20px; color:#713d36; font-size:12px; }} .auth-alert li + li {{ margin-top:5px; }}
    .auth-alert code {{ padding:2px 5px; color:#71352d; background:rgba(255,255,255,.65); border-radius:5px; }} .auth-alert p {{ margin:12px 0 0; font-size:10px; font-weight:700; }}
    .period-controls {{ margin-top:18px; padding:16px 18px; background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); }}
    .period-controls form {{ display:grid; grid-template-columns:1.4fr 1fr 1fr auto; gap:10px; align-items:end; }}
    .period-controls label span {{ display:block; margin-bottom:4px; color:var(--muted); font-size:9px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }}
    .period-controls select,.period-controls input,.period-controls button {{ width:100%; min-height:39px; padding:8px 10px; color:var(--ink); background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font:inherit; }}
    .period-controls button {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); font-weight:750; cursor:pointer; }}
    .period-controls p,.scenario-note {{ margin:10px 0 0; color:var(--muted); font-size:11px; }}
    .period-flash {{ margin-bottom:12px; }}
    .disclosure-panel {{ margin-top:14px; background:#fff; border:1px solid var(--line); border-radius:16px; overflow:hidden; }}
    .disclosure-panel summary {{ position:relative; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:15px 17px; cursor:pointer; list-style:none; }}
    .disclosure-panel summary::-webkit-details-marker {{ display:none; }}
    .disclosure-panel summary span {{ color:var(--ink); font-size:13px; font-weight:850; }}
    .disclosure-panel summary small {{ color:var(--muted); font-size:11px; text-align:right; }}
    .disclosure-panel summary::after {{ content:"+"; display:grid; place-items:center; flex:0 0 24px; height:24px; color:var(--deep-2); background:#eef5f2; border-radius:50%; font-weight:900; }}
    .disclosure-panel[open] summary::after {{ content:"-"; }}
    .disclosure-body {{ padding:0 17px 17px; }}
    .onboarding-steps {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:14px; }}
    .onboarding-steps div {{ padding:15px; background:#f8fbf9; border:1px solid var(--line); border-radius:14px; }}
    .onboarding-steps span {{ display:grid; place-items:center; width:28px; height:28px; color:#fff; background:var(--deep-2); border-radius:50%; font-size:12px; font-weight:900; }}
    .onboarding-steps strong {{ display:block; margin-top:10px; font-size:14px; }}
    .onboarding-steps p,.guidance-list {{ margin:6px 0 0; color:var(--muted); font-size:12px; }}
    .guidance-list {{ display:grid; gap:7px; padding-left:18px; }}
    .section-title {{ margin-bottom:20px; }} .eyebrow {{ color:var(--green); font-size:10px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }}
    h2 {{ margin:2px 0 0; font-size:28px; letter-spacing:-.045em; }} .section-title p {{ margin:0; color:var(--muted); font-size:13px; }}
    .section-title p span {{ margin:0 5px; color:#a0ada9; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
    .metric {{ position:relative; min-height:128px; padding:17px; overflow:hidden; background:var(--paper); border:1px solid var(--line); border-radius:16px; }}
    .metric::before {{ content:""; position:absolute; width:7px; height:7px; right:15px; top:15px; background:#aab8b3; border-radius:50%; box-shadow:0 0 0 5px #f1f5f3; }}
    .metric span,.metric small {{ display:block; color:var(--muted); }} .metric span {{ max-width:80%; font-size:12px; font-weight:650; }}
    .metric strong {{ display:block; margin-top:20px; overflow-wrap:anywhere; font-size:24px; line-height:1.05; letter-spacing:-.045em; }}
    .metric small {{ margin-top:7px; font-size:10px; line-height:1.35; }}
    .metric.primary {{ grid-column:span 2; color:#fff; background:linear-gradient(135deg,#166449,#248b67); border-color:transparent; }}
    .metric.primary span,.metric.primary small {{ color:rgba(255,255,255,.7); }} .metric.primary strong {{ font-size:38px; }}
    .metric.primary::before {{ background:#6fe0b5; box-shadow:0 0 0 5px rgba(255,255,255,.11); }}
    .metric.solar::before {{ background:var(--sun); box-shadow:0 0 0 5px var(--sun-soft); }}
    .metric.grid::before,.metric.ev::before {{ background:var(--blue); box-shadow:0 0 0 5px var(--blue-soft); }}
    .metric.cost::before,.metric.supply::before {{ background:var(--green); box-shadow:0 0 0 5px var(--green-soft); }}
    .metric.export::before,.metric.credit::before {{ background:#8faaa0; }}
    .metric.good::before {{ background:var(--green); box-shadow:0 0 0 5px var(--green-soft); }} .metric.bad::before {{ background:var(--red); box-shadow:0 0 0 5px var(--red-soft); }}
    .charts {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:14px; }}
    .ev-breakdown-card {{ grid-column:1 / -1; }}
    .chart-card {{ padding:20px; background:var(--paper); border:1px solid var(--line); border-radius:18px; }}
    .chart-heading {{ margin-bottom:18px; }} h3 {{ margin:0; font-size:15px; letter-spacing:-.02em; }} .chart-heading span {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.08em; }}
    .bar-chart {{ display:grid; gap:15px; }} .bar-row {{ display:grid; grid-template-columns:130px 1fr 75px; align-items:center; gap:12px; }}
    .bar-label {{ justify-content:flex-start; gap:6px; color:var(--muted); font-size:11px; font-weight:650; }}
    .peak-badge {{ padding:2px 5px; color:var(--red); background:var(--red-soft); border-radius:99px; font-size:7px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; }}
    .bar-track {{ height:9px; overflow:hidden; background:#eef3f1; border-radius:99px; }} .bar-fill {{ display:block; min-width:2px; height:100%; background:linear-gradient(90deg,#48b78d,var(--green)); border-radius:inherit; }}
    .bar-row.peak .bar-label,.bar-row.peak strong {{ color:var(--red); }} .bar-row.peak .bar-fill {{ background:linear-gradient(90deg,#ed9a8b,var(--red)); }}
    .bar-row strong {{ text-align:right; font-size:11px; font-variant-numeric:tabular-nums; }}
    .actual-ev-card {{ margin-top:14px; padding:22px; color:#12382c; background:linear-gradient(135deg,#f1fbf6,#dff5ea); border:1px solid #bee7d1; border-radius:22px; box-shadow:0 14px 34px rgba(36,139,103,.10); }}
    .actual-ev-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:16px; }}
    .actual-ev-head span {{ display:block; color:#2b8b68; font-size:9px; font-weight:850; letter-spacing:.12em; text-transform:uppercase; }}
    .actual-ev-head h3 {{ margin:2px 0 0; font-size:24px; letter-spacing:-.04em; }}
    .actual-ev-head p {{ max-width:430px; margin:0; color:#3d735f; font-size:12px; font-weight:650; }}
    .actual-ev-metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
    .actual-ev-metrics div {{ min-height:96px; padding:14px; background:rgba(255,255,255,.72); border:1px solid rgba(65,151,115,.20); border-radius:15px; }}
    .actual-ev-metrics span {{ display:block; color:#517c6a; font-size:10px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; }}
    .actual-ev-metrics strong {{ display:block; margin-top:13px; color:#12382c; font-size:22px; line-height:1.05; letter-spacing:-.04em; }}
    .actual-ev-breakdown {{ margin-top:16px; padding:16px; background:rgba(255,255,255,.58); border:1px solid rgba(65,151,115,.18); border-radius:16px; }}
    .actual-ev-breakdown-row {{ display:grid; grid-template-columns:210px 1fr 72px; align-items:center; gap:12px; margin-top:11px; }}
    .actual-ev-breakdown-row div:first-child span,.actual-ev-breakdown-row div:first-child strong {{ display:block; }}
    .actual-ev-breakdown-row div:first-child span {{ color:#476f60; font-size:10px; font-weight:750; }}
    .actual-ev-breakdown-row div:first-child strong,.actual-ev-breakdown-row > strong {{ font-size:11px; font-variant-numeric:tabular-nums; }}
    .actual-ev-breakdown-row > strong {{ text-align:right; color:#315d4c; }}
    .actual-ev-track {{ height:10px; overflow:hidden; background:#dbeee5; border-radius:99px; }}
    .actual-ev-track span {{ display:block; min-width:2px; height:100%; background:linear-gradient(90deg,#3bb77e,#1c9b6c); border-radius:inherit; }}
    .actual-ev-breakdown-row.peak .actual-ev-track span {{ background:linear-gradient(90deg,#ed9a8b,var(--red)); }}
    .actual-ev-partial {{ display:flex; align-items:center; gap:10px; margin-top:12px; padding:10px 12px; color:#7b5515; background:#fff3cf; border:1px solid #eed99d; border-radius:12px; }}
    .actual-ev-partial strong {{ font-size:11px; text-transform:uppercase; letter-spacing:.07em; }} .actual-ev-partial span {{ font-size:11px; }}
    .ev-session-section {{ background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(246,252,249,.9)); }}
    .ev-overview-metrics {{ margin-bottom:14px; }}
    .ev-session-grid {{ display:grid; grid-template-columns:.9fr 1.1fr; gap:14px; margin-top:14px; align-items:start; }}
    .ev-session-card,.ev-session-recommendations,.ev-session-history {{ padding:18px; background:var(--paper); border:1px solid var(--line); border-radius:18px; }}
    .ev-tariff-list {{ display:grid; gap:10px; }}
    .ev-tariff-row {{ display:grid; grid-template-columns:minmax(190px,220px) 1fr 72px; align-items:center; gap:10px; }}
    .ev-tariff-row div:first-child span,.ev-tariff-row div:first-child strong {{ display:block; }}
    .ev-tariff-row div:first-child span {{ color:var(--muted); font-size:11px; font-weight:750; line-height:1.35; }}
    .ev-tariff-row div:first-child strong,.ev-tariff-row > strong {{ font-size:12px; font-variant-numeric:tabular-nums; }}
    .ev-tariff-row > strong {{ text-align:right; color:#315d4c; }}
    .ev-tariff-row.peak .actual-ev-track span {{ background:linear-gradient(90deg,#ed9a8b,var(--red)); }}
    .ev-insight-metrics {{ grid-template-columns:repeat(2,1fr); }}
    .ev-insight-metrics .metric {{ display:grid; align-content:start; gap:8px; min-height:0; padding:16px 18px; }}
    .ev-insight-metrics .metric span {{ font-size:11px; line-height:1.35; }}
    .ev-insight-metrics .metric strong {{ margin-top:2px; font-size:22px; line-height:1.15; letter-spacing:-.025em; }}
    .ev-insight-metrics .metric small {{ margin-top:0; max-width:none; color:#5f6b67; font-size:12px; line-height:1.45; }}
    .ev-session-recommendations {{ margin-top:14px; background:#fffaf0; border-color:#eeddb0; }}
    .ev-session-recommendations ul {{ margin:0; padding-left:18px; color:#74571a; font-size:12px; }}
    .ev-session-recommendations li + li {{ margin-top:6px; }}
    .ev-session-history {{ margin-top:14px; }}
    .ev-session-table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    .ev-session-history table {{ width:100%; border-collapse:collapse; min-width:760px; }}
    .ev-session-history th,.ev-session-history td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; font-size:11px; font-variant-numeric:tabular-nums; }}
    .ev-session-history th {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .ev-session-history td:nth-child(5),.ev-session-history td:nth-child(6),.ev-session-history td:nth-child(8) {{ text-align:right; }}
    .insights {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:14px; }}
    .insight {{ padding:17px; border:1px solid var(--line); border-radius:16px; background:#f7faf9; }}
    .insight-kicker {{ display:block; margin-bottom:8px; color:var(--muted); font-size:9px; font-weight:800; letter-spacing:.11em; text-transform:uppercase; }}
    .insight strong {{ display:block; font-size:17px; letter-spacing:-.025em; }} .insight p {{ margin:5px 0 0; color:var(--muted); font-size:11px; line-height:1.45; }}
    .peak-insight {{ background:var(--red-soft); border-color:#f6d7d1; }} .peak-insight .insight-kicker,.peak-insight strong {{ color:#ad4436; }}
    .solar-insight {{ background:var(--sun-soft); border-color:#f1dfb4; }} .solar-insight .insight-kicker,.solar-insight strong {{ color:#8b6519; }}
    .ev-insight {{ background:var(--blue-soft); border-color:#d5e7ed; }} .ev-insight .insight-kicker,.ev-insight strong {{ color:#376d88; }}
    .trend-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    .trend-card {{ padding:20px; background:var(--paper); border:1px solid var(--line); border-radius:18px; }}
    .trend-card:last-child {{ grid-column:1 / -1; }}
    .trend-list {{ display:grid; gap:12px; }} .trend-row {{ display:grid; grid-template-columns:52px 1fr 78px; align-items:center; gap:10px; }}
    .trend-row > strong,.trend-row > span {{ font-size:10px; font-variant-numeric:tabular-nums; }} .trend-row > span {{ color:var(--muted); text-align:right; }}
    .trend-bars {{ display:grid; gap:3px; }} .trend-track {{ display:block; height:5px; overflow:hidden; background:#edf3f0; border-radius:99px; }}
    .trend-track i {{ display:block; min-width:2px; height:100%; border-radius:inherit; }}
    .import-bar {{ background:#80a195; }} .net-bar {{ background:var(--deep-2); }} .credit-bar {{ background:#79cba8; }}
    .grid-bar {{ background:var(--blue); }} .solar-bar {{ background:var(--sun); }} .ev-bar {{ background:linear-gradient(90deg,#77aac2,var(--blue)); }}
    .trend-legend {{ display:flex; align-items:center; gap:5px; }} .trend-legend i {{ width:6px; height:6px; border-radius:50%; }}
    .import-dot {{ background:#80a195; }} .net-dot {{ background:var(--deep-2); }} .credit-dot {{ background:#79cba8; }}
    .grid-dot {{ background:var(--blue); }} .solar-dot {{ background:var(--sun); }}
    .recommendation-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
    .recommendation-card {{ position:relative; padding:20px; border:1px solid var(--line); border-radius:18px; background:var(--paper); }}
    .recommendation-card h3 {{ margin:10px 0 5px; font-size:18px; }} .recommendation-card p {{ margin:0; color:var(--muted); font-size:12px; }}
    .recommendation-level {{ padding:3px 7px; border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .recommendation-action {{ margin-top:14px; padding:11px 12px; border-radius:10px; font-size:11px; font-weight:650; }}
    .recommendation-card.green {{ border-color:#cce9dc; background:#f3fbf7; }} .recommendation-card.green .recommendation-level,.recommendation-card.green .recommendation-action {{ color:#226b50; background:#dff4e9; }}
    .recommendation-card.amber {{ border-color:#eeddb0; background:#fffaf0; }} .recommendation-card.amber .recommendation-level,.recommendation-card.amber .recommendation-action {{ color:#835f15; background:#fff0c8; }}
    .recommendation-card.red {{ border-color:#f0cec8; background:#fff5f3; }} .recommendation-card.red .recommendation-level,.recommendation-card.red .recommendation-action {{ color:#a74334; background:#ffe3de; }}
    .recommendation-metrics {{ display:grid; grid-template-columns:repeat(2,1fr); gap:7px; margin-top:13px; }} .recommendation-metrics span {{ padding:8px; background:rgba(255,255,255,.65); border:1px solid rgba(0,0,0,.05); border-radius:9px; }}
    .recommendation-metrics small,.recommendation-metrics strong {{ display:block; }} .recommendation-metrics small {{ color:var(--muted); font-size:8px; text-transform:capitalize; }} .recommendation-metrics strong {{ margin-top:2px; font-size:11px; }}
    .recommendation-centre-heading,.recommendation-centre-meta {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
    .recommendation-centre-heading div {{ text-align:right; }}
    .recommendation-centre-heading small {{ display:block; color:var(--muted); font-size:8px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .recommendation-centre-heading strong {{ display:block; font-size:28px; line-height:1; letter-spacing:-.05em; }}
    .recommendation-centre-meta {{ justify-content:flex-start; flex-wrap:wrap; margin-top:8px; }}
    .recommendation-centre-meta span {{ padding:4px 8px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .recommendation-centre-evidence {{ margin-top:14px; padding-top:12px; border-top:1px solid rgba(0,0,0,.08); }}
    .recommendation-centre-evidence > strong {{ display:block; margin-bottom:8px; font-size:9px; font-weight:850; letter-spacing:.09em; text-transform:uppercase; }}
    .recommendation-evidence {{ display:grid; gap:6px; margin:0; padding:0; list-style:none; }}
    .recommendation-evidence li {{ display:grid; grid-template-columns:145px 1fr; gap:8px; color:var(--muted); font-size:11px; }}
    .recommendation-evidence strong {{ color:var(--ink); font-size:10px; text-transform:capitalize; }}
    .recommendation-controls {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; padding-top:12px; border-top:1px solid rgba(0,0,0,.08); }}
    .recommendation-controls form {{ display:flex; flex-wrap:wrap; gap:8px; margin:0; }}
    .recommendation-controls button {{ min-height:30px; padding:6px 9px; color:var(--deep-2); background:#f8fbf9; border:1px solid var(--line); border-radius:9px; font:inherit; font-size:11px; font-weight:750; cursor:pointer; }}
    .recommendation-controls button:hover {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); }}
    .recommendation-centre-section {{ display:grid; gap:14px; }}
    .recommendation-centre-section .section-title {{ margin:0; }}
    .recommendation-centre-section .scenario-note {{ margin:0; }}
    .recommendation-centre-section + .recommendation-centre-section {{ margin-top:16px; }}
    .recommendation-centre-section:last-child .recommendation-card {{ padding:16px; background:#f8fbf9; }}
    .recommendation-centre-section:last-child .recommendation-centre-heading strong {{ font-size:18px; letter-spacing:-.025em; }}
    .action-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }}
    .action-card {{ padding:17px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .action-card span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .action-card h3 {{ margin:11px 0 5px; font-size:16px; }} .action-card p {{ margin:0; color:var(--muted); font-size:12px; }}
    .action-card.green {{ background:#f3fbf7; border-color:#cce9dc; }} .action-card.amber {{ background:#fffaf0; border-color:#eeddb0; }} .action-card.red {{ background:#fff5f3; border-color:#f0cec8; }}
    .briefing-section,.home-dashboard {{ display:grid; gap:16px; }}
    .briefing-hero {{ display:flex; align-items:flex-start; justify-content:space-between; gap:28px; padding:26px; color:#fff; background:linear-gradient(135deg,#111816,#285140); border-radius:24px; }}
    .home-assistant-hero {{ min-height:170px; align-items:center; }}
    .briefing-hero h2 {{ max-width:760px; color:#fff; font-size:34px; }}
    .briefing-hero p {{ max-width:700px; margin:8px 0 0; color:rgba(255,255,255,.74); }}
    .briefing-readtime {{ flex:0 0 150px; padding:14px; text-align:right; background:rgba(255,255,255,.1); border-radius:16px; }}
    .briefing-readtime span,.briefing-card-heading span,.briefing-meta small,.briefing-sources {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .briefing-readtime span {{ color:rgba(255,255,255,.64); }}
    .briefing-readtime strong {{ display:block; margin-top:5px; font-size:20px; letter-spacing:-.035em; }}
    .briefing-headline {{ margin:0; color:#2f453e; font-size:20px; font-weight:750; letter-spacing:-.025em; }}
    .home-status-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
    .home-status-tile {{ min-height:122px; padding:16px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .home-status-tile span {{ display:block; color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .home-status-tile h3 {{ margin:14px 0 6px; font-size:20px; letter-spacing:-.035em; }}
    .home-status-tile p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.4; }}
    .home-status-tile.green {{ background:#f3fbf7; border-color:#cce9dc; }}
    .home-status-tile.amber {{ background:#fffaf0; border-color:#eeddb0; }}
    .home-status-tile.red {{ background:#fff5f3; border-color:#f0cec8; }}
    .home-dashboard-block {{ display:grid; gap:12px; }}
    .home-glance-card {{ display:grid; grid-template-columns:minmax(0,1.4fr) minmax(240px,.8fr); gap:12px; padding:18px; color:#12382c; background:linear-gradient(135deg,#f1fbf6,#dff5ea); border:1px solid #bee7d1; border-radius:18px; }}
    .home-glance-card strong {{ display:block; font-size:22px; letter-spacing:-.03em; }}
    .home-glance-card p {{ margin:6px 0 0; color:#3d735f; font-size:13px; line-height:1.45; }}
    .home-glance-meta {{ padding:14px; background:rgba(255,255,255,.72); border:1px solid rgba(65,151,115,.18); border-radius:14px; }}
    .home-glance-meta span,.home-glance-meta strong,.home-glance-meta small {{ display:block; }}
    .home-glance-meta span {{ color:#517c6a; font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .home-glance-meta strong {{ margin-top:8px; font-size:18px; }}
    .home-glance-meta small {{ margin-top:6px; color:#517c6a; font-size:11px; line-height:1.4; }}
    .home-inline-summary {{ display:flex; align-items:center; justify-content:space-between; gap:14px; padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .home-inline-summary p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .home-inline-summary strong {{ display:block; font-size:15px; letter-spacing:-.02em; }}
    .home-action-summary {{ align-items:flex-start; }}
    .home-action-summary-card .primary-action {{ margin-top:14px; }}
    .home-action-summary-card .decision-lines {{ margin-top:12px; }}
    .home-timeline-list {{ display:grid; gap:10px; }}
    .home-timeline-row {{ display:grid; grid-template-columns:84px 1fr; gap:10px; padding:10px 12px; background:#fff; border:1px solid var(--line); border-radius:14px; }}
    .home-timeline-row .timeline-stamp {{ align-items:center; gap:6px; }}
    .home-timeline-row .timeline-stamp span {{ min-width:22px; height:22px; font-size:9px; }}
    .home-timeline-row .timeline-stamp time {{ color:#8a9692; font-size:10px; font-weight:750; letter-spacing:.03em; }}
    .home-timeline-row > div:last-child > span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .home-timeline-row strong {{ display:block; margin-top:6px; font-size:15px; letter-spacing:-.02em; }}
    .home-timeline-row p {{ margin:3px 0 0; color:var(--muted); font-size:11px; line-height:1.35; }}
    .history-story-section .metrics {{ margin-top:14px; }}
    .history-comparison-grid {{ margin-top:14px; }}
    .history-timeline-preview .home-timeline-row {{ background:#f8fbf9; }}
    .history-preview-row strong {{ margin-top:4px; }}
    .home-change-block .notice {{ margin:0; }}
    .energy-timeline-list {{ display:grid; grid-template-columns:1fr; gap:10px; }}
    .energy-timeline-card {{ display:grid; grid-template-columns:64px 1fr; gap:14px; align-items:start; padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .energy-timeline-card time {{ padding-top:3px; color:var(--ink); font-size:17px; font-weight:850; letter-spacing:0; font-variant-numeric:tabular-nums; }}
    .energy-timeline-card h3 {{ display:flex; gap:9px; align-items:center; margin:0 0 5px; font-size:17px; letter-spacing:0; }}
    .energy-timeline-card h3 span {{ width:22px; flex:0 0 22px; text-align:center; }}
    .energy-timeline-card p {{ margin:0; color:var(--muted); font-size:13px; line-height:1.45; }}
    .energy-timeline-card .briefing-meta {{ margin-top:8px; justify-content:flex-start; flex-wrap:wrap; }}
    .energy-timeline-more {{ margin-top:10px; }}
    .energy-timeline-more summary {{ cursor:pointer; color:var(--accent); font-weight:800; }}
    .energy-score-card {{ display:grid; grid-template-columns:180px 1fr; gap:18px; padding:20px; background:#fff; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .energy-score-main {{ display:grid; align-content:center; min-height:150px; padding:18px; color:#fff; background:linear-gradient(135deg,#111816,#2d6b52); border-radius:18px; }}
    .energy-score-main span,.energy-score-main small,.energy-score-factors span {{ font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .energy-score-main span,.energy-score-main small {{ color:rgba(255,255,255,.68); }}
    .energy-score-main strong {{ display:block; margin:8px 0; font-size:58px; line-height:1; letter-spacing:-.06em; }}
    .energy-score-detail h3 {{ margin:0; font-size:18px; letter-spacing:-.03em; }}
    .energy-score-detail p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }}
    .energy-score-explain {{ margin-top:13px; padding:13px; background:#f8fbf9; border:1px solid var(--line); border-radius:13px; }}
    .energy-score-explain h4 {{ margin:0; font-size:14px; }}
    .energy-score-breakdown {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:12px; }}
    .energy-score-breakdown div {{ padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .energy-score-breakdown span {{ display:block; color:var(--ink); font-size:10px; font-weight:850; text-transform:uppercase; }}
    .energy-score-breakdown p {{ margin-top:5px; font-size:12px; }}
    .energy-score-factors {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:13px; }}
    .energy-score-factors div {{ padding:12px; background:#f8fbf9; border:1px solid var(--line); border-radius:13px; }}
    .energy-score-factors span {{ color:var(--ink); }}
    .energy-score-factors ul {{ margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:12px; line-height:1.4; }}
    .briefing-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
    .briefing-grid.two-up {{ grid-template-columns:1fr 1fr; }}
    .briefing-card {{ padding:20px; background:#fff; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .goal-progress-card {{ display:grid; gap:10px; min-height:0; padding:16px; align-content:start; }}
    .goal-progress-card .briefing-card-heading {{ align-items:center; }}
    .goal-progress-card h3 {{ margin:0; font-size:17px; line-height:1.25; letter-spacing:-.02em; }}
    .goal-progress-card p {{ font-size:12px; line-height:1.42; }}
    .goal-progress-topline {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }}
    .goal-progress-topline strong {{ color:var(--ink); font-size:28px; line-height:1; letter-spacing:-.045em; font-variant-numeric:tabular-nums; }}
    .goal-progress-topline span {{ flex:0 1 auto; padding:4px 8px; color:#315f74; background:var(--blue-soft); border-radius:999px; font-size:9px; font-weight:850; letter-spacing:.06em; text-transform:uppercase; text-align:right; }}
    .household-insight-card {{ padding:20px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .household-trend-card,.household-record-card {{ padding:20px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .household-record-card {{ background:#f8fbf9; border-color:#cce9dc; }}
    .household-trend-card > span,.household-record-card > span {{ display:block; color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .briefing-card-heading,.briefing-meta {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .briefing-card-heading strong {{ padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .briefing-card h3,.household-insight-card h3,.household-trend-card h3,.household-record-card h3 {{ margin:13px 0 7px; font-size:20px; letter-spacing:-.035em; }}
    .briefing-card p,.household-insight-card p,.household-trend-card p,.household-record-card p {{ margin:0; color:var(--muted); font-size:13px; line-height:1.5; }}
    .briefing-meta {{ margin-top:16px; padding-top:12px; border-top:1px solid var(--line); }}
    .household-insight-card .briefing-meta,.household-trend-card .briefing-meta,.household-record-card .briefing-meta {{ align-items:flex-start; flex-wrap:wrap; justify-content:flex-start; }}
    .decision-explanation {{ margin-top:14px; padding-top:12px; border-top:1px solid rgba(0,0,0,.08); }}
    .decision-explanation summary {{ cursor:pointer; color:var(--ink); font-size:12px; font-weight:850; }}
    .decision-explanation-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:11px; margin-top:12px; }}
    .decision-explanation-grid div {{ min-width:0; }}
    .decision-explanation-grid span {{ display:block; margin-bottom:3px; color:var(--ink); font-size:8px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .decision-explanation-grid p,.decision-explanation-grid li {{ color:var(--muted); font-size:12px; line-height:1.4; }}
    .decision-explanation-grid ul {{ margin:0; padding-left:18px; }}
    .evidence-trail {{ margin-top:14px; padding-top:12px; border-top:1px solid rgba(0,0,0,.08); }}
    .evidence-trail summary {{ cursor:pointer; color:var(--ink); font-size:12px; font-weight:850; }}
    .evidence-trail ol {{ display:grid; gap:10px; margin:12px 0 0; padding-left:20px; }}
    .evidence-trail li {{ color:var(--muted); font-size:12px; line-height:1.4; }}
    .evidence-trail li span,.evidence-trail li strong,.evidence-trail li small {{ display:block; }}
    .evidence-trail li span {{ color:var(--ink); font-size:8px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .evidence-trail li strong {{ margin-top:2px; color:var(--ink); font-size:13px; }}
    .evidence-trail li p {{ margin:3px 0; }}
    .briefing-card.green {{ background:#f3fbf7; }} .briefing-card.amber {{ background:#fffaf0; }} .briefing-card.red {{ background:#fff5f3; }}
    .briefing-block {{ display:grid; gap:14px; }}
    .briefing-block .section-title {{ margin:0; }}
    .briefing-sources {{ margin:0; color:#8a9692; }}
    .plan-detail-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:14px; }}
    .plan-detail-grid div {{ padding:13px; background:#f8fbf9; border:1px solid var(--line); border-radius:13px; }}
    .plan-detail-grid span,.plan-detail-grid strong {{ display:block; }} .plan-detail-grid span {{ color:var(--muted); font-size:10px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; }}
    .plan-detail-grid strong {{ margin-top:6px; font-size:16px; letter-spacing:-.025em; }}
    .bills-summary-list {{ display:grid; gap:10px; margin-top:14px; }}
    .bills-summary-list-compact {{ margin-top:12px; }}
    .bills-readable-list {{ max-width:min(100%, 840px); }}
    .bills-readable-list-compact {{ max-width:min(100%, 760px); }}
    .bills-summary-row {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px; align-items:center; padding:12px 14px; background:#fff; border:1px solid var(--line); border-radius:14px; }}
    .bills-summary-copy span,.bills-summary-copy small {{ display:block; }}
    .bills-summary-copy span {{ color:var(--ink); font-size:14px; font-weight:750; }}
    .bills-summary-copy small {{ margin-top:3px; color:var(--muted); font-size:11px; line-height:1.4; }}
    .bills-summary-row strong {{ text-align:right; white-space:nowrap; font-size:20px; letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
    .bills-summary-row.good strong {{ color:var(--green); }}
    .bills-summary-row.bad strong {{ color:var(--red); }}
    .bills-summary-row.credit strong {{ color:#2d7a63; }}
    .comparison-summary-list {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; margin-top:14px; }}
    .comparison-summary-row {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:14px; }}
    .comparison-summary-row span {{ color:var(--ink); font-size:13px; font-weight:750; }}
    .comparison-summary-row strong {{ white-space:nowrap; font-size:20px; letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
    .comparison-summary-row.good strong {{ color:var(--green); }}
    .comparison-summary-row.bad strong {{ color:var(--red); }}
    .bills-plan-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:14px; }}
    .bills-plan-hero,.bills-plan-outcome {{ display:grid; gap:6px; padding:16px 18px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .bills-plan-hero span,.bills-plan-outcome span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .bills-plan-hero strong,.bills-plan-outcome strong {{ font-size:30px; line-height:1; letter-spacing:-.05em; font-variant-numeric:tabular-nums; white-space:nowrap; }}
    .bills-plan-hero small {{ color:var(--muted); font-size:12px; line-height:1.4; }}
    .bills-plan-hero.secondary {{ background:#f8fbf9; }}
    .bills-plan-outcome p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.4; }}
    .scenario-section {{ display:grid; gap:14px; }}
    .scenario-section .section-title {{ margin:0; }}
    .scenario-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
    .scenario-card {{ min-height:154px; padding:16px; background:#fff; border:1px solid var(--line); border-radius:17px; }}
    .scenario-card span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .scenario-card h3 {{ margin:10px 0 0; font-size:15px; line-height:1.25; }} .scenario-card strong {{ display:block; margin-top:13px; font-size:24px; letter-spacing:-.04em; }}
    .scenario-card p {{ margin:7px 0 0; color:var(--muted); font-size:11px; line-height:1.4; }}
    .scenario-card.good {{ border-color:#cce9dc; background:#f4fbf7; }} .scenario-card.good strong {{ color:#226b50; }}
    .scenario-card.bad {{ border-color:#f0cec8; background:#fff5f3; }} .scenario-card.bad strong {{ color:#a74334; }}
    .scenario-insights {{ margin-top:14px; padding:18px; background:#fffaf0; border:1px solid #eeddb0; border-radius:17px; }}
    .scenario-insights ul {{ margin:0; padding-left:18px; color:#74571a; font-size:12px; }} .scenario-insights li + li {{ margin-top:6px; }}
    .battery-comparison {{ display:grid; gap:14px; }}
    .battery-conclusion {{ max-width:920px; color:#4c3f1f; font-size:14px; line-height:1.55; }}
    .battery-primary-summary,.battery-why-size,.battery-tradeoff-card {{ padding:16px; background:#fff; border:1px solid #eeddb0; border-radius:14px; }}
    .battery-primary-summary {{ display:grid; gap:12px; }}
    .battery-primary-summary .battery-metrics {{ margin:0; }}
    .battery-based-on {{ display:grid; gap:8px; padding:12px 14px; background:#fffaf0; border:1px solid #eeddb0; border-radius:12px; }}
    .battery-based-on strong,.battery-why-size h4,.battery-methodology h4 {{ margin:0; color:#302611; font-size:13px; letter-spacing:-.01em; }}
    .battery-based-on ul {{ display:grid; gap:4px; margin:0; padding-left:18px; color:#5f6b67; font-size:12px; line-height:1.4; }}
    .battery-based-on a {{ justify-self:start; color:#226b50; font-size:12px; font-weight:850; text-decoration:none; }}
    .battery-based-on a:focus-visible,.battery-accordion summary:focus-visible,.battery-calculation-details summary:focus-visible {{ outline:3px solid #80bfa3; outline-offset:3px; border-radius:10px; }}
    .battery-why-size {{ display:grid; gap:6px; }}
    .battery-why-size p {{ margin:0; color:#4c3f1f; font-size:14px; line-height:1.55; }}
    .battery-tradeoff-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
    .battery-tradeoff-card {{ display:grid; gap:6px; align-content:start; }}
    .battery-tradeoff-card span {{ color:#74571a; font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .battery-tradeoff-card strong {{ color:#1f2a26; font-size:18px; line-height:1.2; letter-spacing:-.025em; }}
    .battery-tradeoff-card p,.battery-tradeoff-card small {{ margin:0; color:#5f6b67; font-size:12px; line-height:1.4; }}
    .battery-metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .battery-metrics div,.battery-diagnostics .config-grid div {{ padding:12px; background:#fff; border:1px solid #eeddb0; border-radius:12px; }}
    .battery-metrics span,.battery-metrics strong,.battery-metrics small,.battery-diagnostics .config-grid span,.battery-diagnostics .config-grid strong,.battery-diagnostics .config-grid small {{ display:block; }}
    .battery-metrics span,.battery-diagnostics .config-grid span {{ color:#74571a; font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .battery-metrics strong,.battery-diagnostics .config-grid strong {{ margin-top:5px; font-size:18px; letter-spacing:-.025em; white-space:nowrap; }}
    .battery-metrics small,.battery-diagnostics .config-grid small {{ margin-top:4px; color:var(--muted); font-size:11px; line-height:1.35; }}
    .battery-homeowner-table {{ width:100%; table-layout:fixed; border-collapse:collapse; background:#fff; }}
    .battery-homeowner-table th,.battery-homeowner-table td {{ padding:15px 18px; border-bottom:1px solid rgba(31,42,38,.11); vertical-align:middle; }}
    .battery-homeowner-table thead th {{ border-bottom:2px solid rgba(31,42,38,.18); }}
    .battery-homeowner-table th {{ color:#4f5b57; font-size:11px; font-weight:850; letter-spacing:.045em; line-height:1.25; text-transform:uppercase; background:#fff; }}
    .battery-homeowner-table th + th,.battery-homeowner-table td + td {{ border-left:1px solid rgba(31,42,38,.06); }}
    .battery-homeowner-table tbody tr:nth-child(even) td {{ background:rgba(31,42,38,.018); }}
    .battery-homeowner-table th:nth-child(1) {{ width:18%; text-align:left; }}
    .battery-homeowner-table th:nth-child(2),
    .battery-homeowner-table th:nth-child(3),
    .battery-homeowner-table th:nth-child(4),
    .battery-homeowner-table th:nth-child(5) {{ width:13%; text-align:right; }}
    .battery-homeowner-table th:nth-child(6) {{ width:30%; }}
    .battery-homeowner-table td {{ white-space:normal; font-size:13px; line-height:1.4; }}
    .battery-homeowner-table .battery-money-cell {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
    .battery-homeowner-table .battery-size-cell {{ width:18%; min-width:132px; text-align:left; }}
    .battery-homeowner-table .battery-size-cell strong {{ display:block; font-size:14px; font-weight:750; color:#1f2a26; white-space:nowrap; }}
    .battery-homeowner-table .battery-recommendation-cell {{ min-width:220px; }}
    .battery-homeowner-table tr.recommended td {{ font-weight:650; }}
    .battery-homeowner-table tr.baseline td {{ color:#58625e; }}
    .battery-table-wrap,.battery-seasonal-table-wrap {{ width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    .battery-table-wrap .battery-homeowner-table,.battery-seasonal-table-wrap .battery-homeowner-table {{ min-width:760px; }}
    .battery-annual-table {{ margin-top:2px; }}
    .battery-annual-table .battery-size-cell small {{ display:block; margin-top:3px; color:#226b50; font-size:10px; font-weight:850; }}
    .battery-annual-table th:nth-child(1),.battery-annual-table td:nth-child(1),
    .battery-seasonal-table th:nth-child(1),.battery-seasonal-table td:nth-child(1) {{ width:22%; text-align:left; }}
    .battery-annual-table th:nth-child(2),.battery-annual-table th:nth-child(3),.battery-annual-table th:nth-child(4),
    .battery-annual-table td:nth-child(2),.battery-annual-table td:nth-child(3),.battery-annual-table td:nth-child(4),
    .battery-seasonal-table th:nth-child(n+2),.battery-seasonal-table td:nth-child(n+2) {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .battery-annual-table th:nth-child(5),.battery-annual-table td:nth-child(5) {{ width:26%; text-align:left; }}
    .battery-seasonal-table {{ min-width:980px; table-layout:auto; }}
    .battery-seasonal-table th:nth-child(1),.battery-seasonal-table td:nth-child(1) {{ min-width:120px; }}
    .battery-seasonal-table th:nth-child(6),.battery-seasonal-table td:nth-child(6) {{ min-width:170px; }}
    .battery-seasonal-table th:nth-child(7),.battery-seasonal-table td:nth-child(7) {{ min-width:170px; text-align:left; }}
    .battery-seasonal-table th,.battery-seasonal-table td {{ white-space:nowrap; }}
    .battery-current-period-result {{ display:grid; gap:10px; }}
    .battery-signal {{ display:inline-block; max-width:100%; padding:4px 9px; border:1px solid rgba(31,42,38,.12); border-radius:999px; font-size:10px; font-weight:850; line-height:1.2; white-space:nowrap; }}
    .battery-signal.neutral {{ color:#4f5b57; background:#f5f7f6; }}
    .battery-signal.recommended {{ color:#226b50; background:#e7f6ee; border-color:#b9dfcc; }}
    .battery-accordion {{ overflow:hidden; background:#fff; border:1px solid #eeddb0; border-radius:14px; }}
    .battery-accordion summary {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; cursor:pointer; list-style:none; }}
    .battery-accordion summary::-webkit-details-marker {{ display:none; }}
    .battery-accordion summary span {{ color:#302611; font-size:15px; font-weight:850; letter-spacing:-.015em; }}
    .battery-accordion summary small {{ color:#74571a; font-size:11px; font-weight:750; text-align:right; }}
    .battery-accordion summary::after {{ content:"+"; display:grid; place-items:center; flex:0 0 24px; height:24px; color:#74571a; background:#fff7df; border-radius:999px; font-weight:900; }}
    .battery-accordion[open] summary::after {{ content:"-"; }}
    .battery-accordion-body {{ padding:0 16px 16px; border-top:1px solid rgba(238,221,176,.7); }}
    .battery-calculation-details {{ margin-top:12px; padding:12px; background:#fff; border:1px solid #eeddb0; border-radius:12px; }}
    .battery-calculation-details summary {{ cursor:pointer; font-size:12px; font-weight:850; }}
    .battery-methodology {{ display:grid; gap:10px; }}
    .battery-methodology p {{ margin:0; color:#5f6b67; font-size:12px; line-height:1.5; }}
    .battery-diagnostics-table {{ overflow-x:auto; margin-top:12px; }}
    .battery-diagnostics-table table {{ min-width:1700px; border-collapse:collapse; background:#fff; }}
    .battery-diagnostics-table th,.battery-diagnostics-table td {{ padding:11px 14px; border-bottom:1px solid rgba(31,42,38,.10); background:transparent; white-space:nowrap; }}
    .battery-diagnostics-table thead th {{ border-bottom:2px solid rgba(31,42,38,.18); color:#4f5b57; font-size:10px; font-weight:850; letter-spacing:.04em; text-transform:uppercase; background:#fff; }}
    .battery-diagnostics-table tbody tr:nth-child(even) td {{ background:rgba(31,42,38,.018); }}
    .battery-diagnostics-table td:not(:first-child) {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .battery-diagnostics-table td:last-child {{ text-align:left; }}
    .battery-methodology-table table {{ min-width:1040px; }}
    .battery-methodology-table th,.battery-methodology-table td {{ max-width:150px; white-space:normal; overflow-wrap:anywhere; }}
    .timeline-list {{ display:grid; gap:12px; }}
    .timeline-day {{ margin:8px 0 0; color:var(--muted); font-size:11px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .timeline-entry {{ display:grid; grid-template-columns:120px 1fr; gap:16px; padding:18px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .timeline-entry.compact {{ grid-template-columns:96px 1fr; gap:12px; padding:14px; }}
    .timeline-stamp {{ display:flex; align-items:center; gap:8px; }}
    .timeline-stamp span {{ display:inline-grid; place-items:center; min-width:28px; height:28px; padding:0; color:#fff; background:var(--deep-2); border-radius:50%; font-size:10px; font-weight:900; letter-spacing:0; }}
    .timeline-entry time {{ color:var(--muted); font-size:11px; font-weight:850; letter-spacing:.06em; text-transform:uppercase; }}
    .timeline-entry > div:last-child > span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .timeline-entry h3 {{ margin:9px 0 4px; font-size:18px; letter-spacing:-.025em; }}
    .timeline-entry p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .timeline-meta {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
    .timeline-meta small {{ padding:6px 8px; color:var(--muted); background:#f8fbf9; border:1px solid var(--line); border-radius:9px; font-size:10px; }}
    .household-timeline-list,.household-history-list {{ display:grid; gap:12px; margin-bottom:14px; }}
    .current-tariff-card {{ border-color:#bfe7d1; background:#f3fbf7; }}
    .previous-tariff-card {{ background:#fbfdfc; }}
    .timeline-card-body {{ min-width:0; }}
    .timeline-note {{ margin-top:5px !important; }}
    .timeline-source {{ display:block; margin-top:7px; color:#6e7a76; font-size:11px; }}
    .timeline-helper,.timeline-review {{ margin:0 0 12px; padding:11px 12px; color:#48645b; background:#f8fbf9; border:1px solid var(--line); border-radius:12px; font-size:12px; line-height:1.45; }}
    .timeline-review strong,.timeline-review span {{ display:block; }}
    .timeline-review span {{ margin-top:3px; color:#5f6b67; }}
    .timeline-confirm-form {{ margin:0 0 12px; }}
    .timeline-confirm-form button {{ min-height:36px; padding:8px 12px; color:#174b3d; background:#e7f7ef; border:1px solid #bfe7d1; border-radius:10px; font:inherit; font-size:12px; font-weight:800; cursor:pointer; }}
    .timeline-action-panel {{ display:inline-block; margin:0; }}
    .timeline-action-panel summary {{ min-height:36px; display:inline-flex; align-items:center; padding:7px 10px; color:#174b3d; background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font-size:12px; font-weight:800; cursor:pointer; }}
    .timeline-action-panel[open] {{ display:block; width:100%; margin-top:8px; }}
    .timeline-action-panel[open] summary {{ margin-bottom:10px; }}
    .timeline-diagnostics {{ margin-top:10px; }}
    .timeline-diagnostics summary {{ color:var(--muted); font-size:11px; font-weight:800; cursor:pointer; }}
    .timeline-diagnostics pre {{ max-height:260px; overflow:auto; padding:12px; background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font-size:10px; }}
    .household-timeline-actions {{ display:flex; flex-wrap:wrap; gap:8px; align-items:flex-start; margin-top:12px; }}
    .household-timeline-actions form {{ display:flex; flex-wrap:wrap; gap:8px; margin:0; }}
    .household-timeline-actions > form button,.household-timeline-form button {{ min-height:36px; padding:8px 12px; color:#174b3d; background:#fff; border:1px solid var(--line); border-radius:10px; font:inherit; font-size:12px; font-weight:800; cursor:pointer; }}
    .household-timeline-actions > form:last-child button {{ color:#8f3327; background:#fff5f3; border-color:#f0cec8; }}
    .household-timeline-actions input[type="date"] {{ min-height:38px; padding:8px 10px; border:1px solid var(--line); border-radius:9px; font:inherit; }}
    .household-timeline-form .timeline-notes {{ grid-column:span 2; }}
    .tariff-context-note {{ background:#f8fbf9; }}
    .tariff-context-inline {{ margin:0 0 12px; padding:10px 12px; background:#f8fbf9; border:1px solid var(--line); border-radius:12px; }}
    .decision-grid,.system-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    .decision-grid div,.system-card {{ padding:18px; background:#fff; border:1px solid var(--line); border-radius:17px; }}
    .decision-grid span,.decision-grid strong,.config-grid span,.config-grid strong {{ display:block; }}
    .decision-grid span,.config-grid span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .decision-grid strong {{ margin-top:7px; font-size:18px; letter-spacing:-.02em; }}
    .source-status {{ margin-top:18px; padding:20px 22px; border-radius:18px; border:1px solid var(--line); box-shadow:var(--shadow); }}
    .source-status span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }} .source-status h2 {{ margin:2px 0 0; }}
    .source-status p {{ margin:8px 0 0; color:var(--muted); font-size:12px; }}
    .source-status.green {{ background:#f3fbf7; border-color:#cce9dc; }} .source-status.amber {{ background:#fffaf0; border-color:#eeddb0; }} .source-status.red {{ background:#fff5f3; border-color:#f0cec8; }}
    .system-grid {{ margin-top:14px; }}
    .system-card {{ margin-top:14px; overflow-x:auto; overflow-wrap:anywhere; -webkit-overflow-scrolling:touch; }}
    .system-card table {{ width:100%; border-collapse:collapse; }} .system-card th,.system-card td {{ padding:9px 8px; border-bottom:1px solid var(--line); text-align:left; font-size:11px; }}
    .system-card th {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .system-overview-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    .system-group .disclosure-body {{ padding-top:1px; }}
    .provider-status-card {{ margin-top:10px; padding:16px; background:#f8fbf9; border:1px solid var(--line); border-radius:14px; }}
    .provider-status-card.good {{ background:#f3fbf7; border-color:#cce9dc; }} .provider-status-card.bad {{ background:#fff5f3; border-color:#f0cec8; }} .provider-status-card.neutral {{ background:#fffaf0; border-color:#eeddb0; }}
    .provider-status-card:target,.deep-link-target:not(.page) {{ outline:3px solid rgba(31,107,80,.32); scroll-margin-top:24px; }}
    .provider-status-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .provider-status-head span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-status-head strong {{ font-size:15px; }}
    .provider-status-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:10px; }}
    .provider-status-grid div {{ padding:10px; background:rgba(255,255,255,.72); border-radius:10px; }}
    .provider-status-grid span,.provider-status-grid strong {{ display:block; }}
    .provider-status-grid span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-status-grid strong {{ margin-top:3px; font-size:12px; line-height:1.3; }}
    .provider-status-card p {{ margin:9px 0 0; color:var(--muted); font-size:12px; }}
    .data-source-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }}
    .source-provides {{ margin-top:12px; padding:10px; background:rgba(255,255,255,.72); border-radius:10px; }}
    .source-provides span {{ display:block; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .source-provides ul {{ margin:6px 0 0; padding-left:17px; color:var(--ink); font-size:12px; line-height:1.45; }}
    .provider-details {{ margin-top:10px; background:rgba(255,255,255,.68); }}
    .provider-guidance-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    .provider-guidance-grid div {{ padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .provider-guidance-grid span {{ display:block; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-guidance-grid strong {{ display:block; margin-top:4px; font-size:12px; line-height:1.35; }}
    .provider-guidance-grid ul {{ margin:6px 0 0; padding-left:17px; color:var(--muted); font-size:12px; line-height:1.4; }}
    .trust-checklist li {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:center; }}
    .trust-checklist li span {{ text-align:right; text-transform:none; letter-spacing:0; font-size:11px; font-weight:650; }}
    .provider-config-form {{ display:grid; gap:12px; margin-top:12px; padding:12px; background:rgba(255,255,255,.78); border:1px solid rgba(210,220,214,.7); border-radius:12px; }}
    .provider-account-summary {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    .provider-account-summary div {{ padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .provider-account-summary span,.provider-account-summary strong {{ display:block; }}
    .provider-account-summary span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-account-summary strong {{ margin-top:4px; font-size:12px; }}
    .credential-editor {{ padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .credential-editor summary {{ cursor:pointer; color:var(--deep); font-size:12px; font-weight:800; }}
    .credential-editor .provider-form-grid {{ margin-top:10px; }}
    .credential-save-row {{ margin-top:10px; }}
    .provider-form-section > span {{ display:block; margin:0 0 8px; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-form-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    .provider-form-grid label {{ display:block; padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .provider-form-grid label span,.provider-form-grid label small {{ display:block; }}
    .provider-form-grid label span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-form-grid label small {{ margin-top:4px; color:var(--muted); font-size:10px; }}
    .provider-form-grid input {{ width:100%; min-height:38px; margin-top:5px; padding:8px 10px; color:var(--ink); background:#f8fbf9; border:1px solid var(--line); border-radius:9px; font:inherit; }}
    .provider-action-row {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .provider-action-row button {{ min-height:38px; padding:8px 12px; color:#fff; background:var(--deep-2); border:1px solid var(--deep-2); border-radius:10px; font:inherit; font-size:12px; font-weight:750; cursor:pointer; }}
    .provider-action-row .button-link {{ display:inline-flex; align-items:center; justify-content:center; min-height:36px; padding:8px 11px; color:#fff; background:var(--deep-2); border:1px solid var(--deep-2); border-radius:10px; font-size:12px; font-weight:750; text-decoration:none; }}
    .provider-action-row .button-link.muted {{ color:#74571a; background:#fffaf0; border-color:#eeddb0; }}
    .home-inline-summary .button-link {{ flex:0 0 auto; display:inline-flex; align-items:center; justify-content:center; min-height:36px; padding:8px 11px; color:#fff; background:var(--deep-2); border:1px solid var(--deep-2); border-radius:10px; font-size:12px; font-weight:750; text-decoration:none; }}
    .bills-layout-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr)); gap:14px; align-items:start; margin-top:14px; }}
    .bills-layout-grid > .report-section {{ margin-top:0; }}
    .bills-layout-grid--actions {{ grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr)); }}
    .bills-layout-grid--changes {{ grid-template-columns:minmax(0,1fr); }}
    .bills-detail-section .section-title {{ margin-bottom:16px; }}
    .bills-overview-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr)); gap:14px; }}
    .bills-overview-hero {{ display:grid; gap:12px; padding:22px; background:#fff; border:1px solid var(--line); border-radius:22px; }}
    .bills-overview-hero > span,.bills-overview-subline span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .bills-overview-hero > strong {{ font-size:48px; line-height:.92; letter-spacing:-.055em; font-variant-numeric:tabular-nums; }}
    .bills-overview-hero > p {{ margin:0; color:var(--muted); font-size:13px; line-height:1.45; max-width:34ch; }}
    .bills-overview-subline {{ display:flex; align-items:flex-end; justify-content:space-between; gap:12px; padding-top:12px; border-top:1px solid var(--line); }}
    .bills-overview-subline > div {{ display:grid; gap:4px; }}
    .bills-overview-subline strong {{ font-size:22px; letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
    .bills-overview-subline small {{ color:var(--muted); font-size:12px; line-height:1.4; }}
    .bills-overview-side-summary {{ display:grid; gap:10px; align-content:start; }}
    .bills-breakdown-grid {{ grid-template-columns:minmax(260px,1.4fr) repeat(auto-fit,minmax(220px,1fr)); align-items:stretch; }}
    .bills-breakdown-grid .metric.primary {{ grid-row:span 2; min-height:0; }}
    .bills-detail-calculation-grid {{ grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); align-items:stretch; }}
    .bills-breakdown-grid .metric,.bills-detail-calculation-grid .metric {{ min-height:0; }}
    .bills-breakdown-grid .metric strong,.bills-detail-calculation-grid .metric strong {{ white-space:nowrap; overflow-wrap:normal; word-break:normal; font-variant-numeric:tabular-nums; }}
    .bills-breakdown-grid .metric small,.bills-detail-calculation-grid .metric small {{ max-width:none; }}
    .home-forecast-summary {{ display:grid; grid-template-columns:minmax(0,1.15fr) minmax(260px,.85fr); gap:12px; }}
    .home-forecast-main {{ display:grid; align-content:start; padding:18px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .home-forecast-main span,.home-forecast-main strong,.home-forecast-main small {{ display:block; }}
    .home-forecast-main span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .home-forecast-main strong {{ margin-top:12px; font-size:42px; line-height:.95; letter-spacing:-.05em; font-variant-numeric:tabular-nums; }}
    .home-forecast-main small {{ margin-top:8px; color:var(--muted); font-size:12px; line-height:1.4; }}
    .home-forecast-grid {{ display:grid; gap:10px; }}
    .home-forecast-detail {{ display:grid; gap:4px; padding:14px 16px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .home-forecast-detail span,.home-forecast-detail strong,.home-forecast-detail small {{ display:block; }}
    .home-forecast-detail span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .home-forecast-detail strong {{ font-size:20px; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }}
    .home-forecast-detail small {{ color:var(--muted); font-size:12px; line-height:1.35; }}
    .home-forecast-delta-up strong {{ color:var(--warn); }}
    .home-forecast-delta-down strong {{ color:var(--good); }}
    .home-forecast-delta-neutral strong {{ color:var(--text); }}
    .home-historical-trend-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .home-historical-trend-card {{ display:grid; gap:8px; min-height:154px; padding:14px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .home-historical-trend-card.improving {{ background:#f3fbf7; border-color:#cce9dc; }}
    .home-historical-trend-card.worsening {{ background:#fff5f3; border-color:#f0cec8; }}
    .home-historical-trend-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
    .home-historical-trend-head span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .home-historical-trend-head strong {{ flex:0 0 auto; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:999px; font-size:8px; font-weight:850; letter-spacing:.06em; text-transform:uppercase; }}
    .home-historical-trend-card.improving .home-historical-trend-head strong {{ color:#226b50; background:#dff4e9; }}
    .home-historical-trend-card.worsening .home-historical-trend-head strong {{ color:#a74334; background:#ffe3de; }}
    .home-historical-trend-value {{ font-size:30px; line-height:1; font-weight:850; letter-spacing:-.045em; font-variant-numeric:tabular-nums; }}
    .home-historical-trend-card p {{ margin:0; color:var(--ink); font-size:13px; line-height:1.35; }}
    .home-historical-trend-meta {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .home-historical-trend-meta span {{ padding:4px 7px; color:var(--muted); background:rgba(255,255,255,.7); border:1px solid rgba(0,0,0,.04); border-radius:999px; font-size:9px; font-weight:750; }}
    .home-historical-trend-card small {{ color:var(--muted); font-size:11px; line-height:1.35; }}
    .home-page-link-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .home-page-link {{ display:grid; gap:6px; padding:14px; color:inherit; text-decoration:none; background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .home-page-link span {{ color:var(--muted); font-size:11px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; }}
    .home-page-link strong {{ font-size:13px; line-height:1.4; }}
    .provider-action-row button[data-provider-action="remove"],.provider-action-row button[data-provider-action="disable"] {{ color:#8f3327; background:#fff5f3; border-color:#f0cec8; }}
    .provider-action-row button[data-provider-action="reset"] {{ color:#74571a; background:#fffaf0; border-color:#eeddb0; }}
    .provider-action-result {{ display:none; padding:11px; background:#f8fbf9; border:1px solid var(--line); border-radius:12px; }}
    .provider-flash {{ margin-top:12px; }}
    .provider-action-result.visible {{ display:block; }}
    .provider-action-result.success {{ background:#f3fbf7; border-color:#cce9dc; }} .provider-action-result.warning {{ background:#fffaf0; border-color:#eeddb0; }} .provider-action-result.failure {{ background:#fff5f3; border-color:#f0cec8; }}
    .provider-action-result strong,.provider-action-result span,.provider-action-result small {{ display:block; }}
    .provider-action-result span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-action-result strong {{ margin-top:3px; font-size:13px; }} .provider-action-result small {{ margin-top:4px; color:var(--muted); font-size:11px; }}
    .provider-action-result pre {{ max-height:220px; overflow:auto; margin:9px 0 0; padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; font-size:10px; white-space:pre-wrap; }}
    .tariff-update-hero {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px; align-items:start; padding:18px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .tariff-update-hero span,.tariff-update-summary-list span,.tariff-impact-card span,.tariff-comparison-header span,.tariff-comparison-row small,.tariff-diagnostics-group > span {{ display:block; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .tariff-update-hero h3 {{ margin:5px 0 0; font-size:22px; line-height:1.15; letter-spacing:-.02em; }}
    .tariff-update-hero p {{ margin:8px 0 0; color:var(--ink); font-size:13px; line-height:1.45; }}
    .tariff-update-hero > strong {{ white-space:nowrap; font-size:30px; line-height:1; font-variant-numeric:tabular-nums; }}
    .tariff-update-summary-list,.tariff-impact-card {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr)); gap:10px; margin-top:12px; }}
    .tariff-update-summary-list div,.tariff-impact-card div {{ padding:12px; background:#fff; border:1px solid var(--line); border-radius:12px; }}
    .tariff-update-summary-list strong,.tariff-impact-card strong {{ display:block; margin-top:4px; font-size:13px; line-height:1.35; }}
    .tariff-comparison-list {{ display:grid; gap:8px; margin-top:14px; }}
    .tariff-comparison-header {{ display:flex; justify-content:space-between; gap:10px; align-items:end; padding:0 2px; }}
    .tariff-comparison-header strong {{ font-size:12px; color:var(--muted); }}
    .tariff-comparison-row {{ display:grid; grid-template-columns:minmax(160px,1.2fr) repeat(3,minmax(120px,1fr)); gap:10px; align-items:center; padding:12px; background:#fff; border:1px solid var(--line); border-left-width:5px; border-radius:12px; }}
    .tariff-comparison-row > span {{ font-weight:850; }}
    .tariff-comparison-row strong {{ display:block; margin-top:3px; font-size:13px; font-variant-numeric:tabular-nums; }}
    .tariff-comparison-row.beneficial {{ border-left-color:#2f8f68; background:#f3fbf7; }}
    .tariff-comparison-row.adverse {{ border-left-color:#b84a3a; background:#fff5f3; }}
    .tariff-comparison-row.neutral {{ border-left-color:#c49a3a; background:#fffaf0; }}
    .tariff-decision-actions {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:14px; }}
    .tariff-decision-actions form {{ display:grid; gap:10px; align-content:start; padding:12px; background:#fff; border:1px solid var(--line); border-radius:12px; }}
    .tariff-decision-actions p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .tariff-decision-actions button {{ min-height:42px; padding:10px 12px; color:#fff; background:var(--deep-2); border:1px solid var(--deep-2); border-radius:10px; font:inherit; font-size:13px; font-weight:800; cursor:pointer; }}
    .tariff-decision-actions form:nth-child(2) button {{ color:#74571a; background:#fffaf0; border-color:#eeddb0; }}
    .tariff-raw-table {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    .tariff-raw-changes pre {{ max-height:260px; overflow:auto; }}
    .tariff-diagnostics {{ display:grid; gap:12px; }}
    .tariff-diagnostics-group {{ display:grid; gap:8px; }}
    .tariff-current-card > p {{ color:var(--ink); }}
    .import-panel {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    .import-panel table {{ width:100%; border-collapse:collapse; }}
    .import-panel th,.import-panel td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; font-size:12px; }}
    .import-panel th {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .import-form {{ display:grid; grid-template-columns:1fr auto; gap:12px; align-items:end; }}
    .import-form label span {{ display:block; margin-bottom:4px; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .import-form input,.import-form button {{ width:100%; min-height:42px; padding:9px 11px; color:var(--ink); background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font:inherit; }}
    .import-form input[type="file"] {{ min-height:48px; }}
    .import-form button {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); font-weight:750; cursor:pointer; }}
    .config-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }} .config-grid div {{ padding:11px; background:#f8fbf9; border:1px solid var(--line); border-radius:12px; }}
    .completeness-grid {{ display:grid; gap:9px; }}
    .completeness-row {{ display:grid; grid-template-columns:1fr auto; gap:4px 10px; padding:11px; background:#f8fbf9; border:1px solid var(--line); border-radius:12px; }}
    .completeness-row span,.completeness-row strong,.completeness-row small {{ display:block; }}
    .completeness-row span {{ color:var(--muted); font-size:10px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; }}
    .completeness-row strong {{ font-size:12px; }} .completeness-row small {{ grid-column:1 / -1; color:var(--muted); font-size:11px; }}
    .completeness-row.good {{ border-color:#cce9dc; background:#f3fbf7; }} .completeness-row.good strong {{ color:#226b50; }}
    .completeness-row.bad {{ border-color:#f0cec8; background:#fff5f3; }} .completeness-row.bad strong {{ color:#a74334; }}
    .historical-job-list {{ gap:12px; }}
    .historical-job-card {{ grid-template-columns:1fr; gap:10px; }}
    .historical-job-header,.historical-job-progress {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .historical-job-header strong,.historical-job-progress strong {{ font-size:12px; }}
    .historical-job-progress strong {{ white-space:nowrap; }}
    .historical-job-bar {{ flex:1 1 auto; height:10px; overflow:hidden; background:#dbe7e2; border-radius:999px; }}
    .historical-job-bar span {{ display:block; height:100%; min-width:6px; background:linear-gradient(90deg,#4a86a8,#1c9b6c); border-radius:inherit; }}
    .historical-job-card.bad .historical-job-bar span {{ background:linear-gradient(90deg,#e58a7b,#dc604e); }}
    .historical-job-meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:6px 10px; }}
    .historical-job-meta small {{ grid-column:auto; }}
    .decision-lines {{ display:grid; gap:12px; margin-top:14px; }}
    .decision-lines p,.action-card p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .decision-lines strong,.action-card p strong {{ display:block; margin-bottom:2px; color:var(--ink); font-size:9px; font-weight:850; letter-spacing:.09em; text-transform:uppercase; }}
    .decision-lines .primary-action {{ padding:11px 12px; color:var(--ink); background:rgba(255,255,255,.72); border:1px solid rgba(0,0,0,.06); border-radius:12px; font-size:13px; }}
    .glance-page {{ min-height:calc(100vh - 180px); }}
    .glance-display {{ min-height:calc(100vh - 220px); display:grid; align-content:space-between; gap:clamp(18px,3vh,34px); padding:clamp(26px,5vw,62px); color:#f8fffc; background:#101418; border-radius:32px; box-shadow:0 28px 80px rgba(16,20,24,.22); }}
    .glance-return {{ justify-self:start; align-self:start; padding:10px 13px; color:#101418; background:#fff; border-radius:10px; text-decoration:none; font-size:12px; font-weight:850; }}
    .glance-only {{ min-height:100vh; overflow:hidden; background:#101418; }}
    .glance-only main {{ width:100vw; min-height:100vh; margin:0; }}
    .glance-only .page {{ display:block; }}
    .glance-only .glance-page {{ min-height:100vh; }}
    .glance-only .glance-display {{ min-height:100vh; border-radius:0; box-shadow:none; }}
    body:has(#glance:target) .hero,body:has(#glance:target) .period-controls,body:has(#glance:target) .top-nav,body:has(#glance:target) footer {{ display:none; }}
    body:has(#glance:target) main {{ width:100vw; min-height:100vh; margin:0; }}
    body:has(#glance:target) .glance-page {{ min-height:100vh; }}
    body:has(#glance:target) .glance-display {{ min-height:100vh; border-radius:0; box-shadow:none; }}
    .glance-charge {{ background:linear-gradient(135deg,#08231b,#126145 56%,#1e8d63); }}
    .glance-wait {{ background:linear-gradient(135deg,#12171c,#1f4961 58%,#326e8a); }}
    .glance-avoid {{ background:linear-gradient(135deg,#221312,#64291f 58%,#9a392c); }}
    .glance-unavailable {{ background:linear-gradient(135deg,#171717,#3b3b3b 58%,#5a4c33); }}
    .glance-topline {{ display:flex; align-items:flex-start; justify-content:space-between; gap:28px; }}
    .glance-topline span,.glance-panel span,.glance-freshness span {{ display:block; color:rgba(255,255,255,.64); font-size:clamp(12px,1.2vw,18px); font-weight:850; letter-spacing:.11em; text-transform:uppercase; }}
    .glance-topline h2 {{ margin:6px 0 0; color:#fff; font-size:clamp(28px,3.2vw,58px); line-height:1; letter-spacing:0; }}
    .glance-freshness {{ min-width:220px; padding:18px 20px; text-align:right; background:rgba(255,255,255,.1); border:1px solid rgba(255,255,255,.12); border-radius:20px; }}
    .glance-freshness strong {{ display:block; margin-top:5px; font-size:clamp(22px,2vw,34px); line-height:1.05; }}
    .glance-status {{ font-size:clamp(74px,13vw,190px); font-weight:900; line-height:.86; letter-spacing:0; text-wrap:balance; }}
    .glance-reason {{ margin:0; color:rgba(255,255,255,.82); font-size:clamp(28px,4vw,68px); font-weight:800; line-height:1.05; letter-spacing:0; }}
    .glance-grid {{ display:grid; grid-template-columns:1.2fr 1fr 1fr; gap:clamp(12px,1.5vw,22px); }}
    .glance-panel {{ min-height:clamp(116px,15vh,190px); padding:clamp(18px,2vw,30px); background:rgba(255,255,255,.1); border:1px solid rgba(255,255,255,.12); border-radius:22px; backdrop-filter:blur(18px); }}
    .glance-panel.wide {{ grid-column:span 2; }}
    .glance-panel strong {{ display:block; margin-top:12px; color:#fff; font-size:clamp(24px,3vw,50px); line-height:1.08; letter-spacing:0; }}
    .glance-panel small {{ display:block; margin-top:8px; color:rgba(255,255,255,.66); font-size:clamp(13px,1.2vw,18px); font-weight:750; }}
    .glance-panel.score strong {{ font-size:clamp(54px,6vw,92px); line-height:.9; }}
    :root {{ --ink:#1d1d1f; --muted:#6e6e73; --line:#e7e7ec; --paper:#fff; --wash:#f5f5f7; --deep:#101418; --deep-2:#24312c; --shadow:0 18px 55px rgba(0,0,0,.06); }}
    body {{ background:radial-gradient(circle at top left,#ffffff 0,#f5f5f7 34%,#eef3f0 100%); }}
    main {{ width:min(1200px, calc(100% - 48px)); margin:28px auto 72px; }}
    .hero {{ padding:28px; border-radius:32px; background:linear-gradient(135deg,#111816,#24312c); box-shadow:0 28px 80px rgba(16,20,24,.18); }}
    .hero-summary {{ margin-top:36px; }} .hero-summary strong {{ font-size:clamp(52px,7vw,82px); }}
    .hero-note,.status-strip div {{ backdrop-filter:blur(18px); }}
    .top-nav {{ top:14px; justify-content:center; gap:6px; margin:18px 0 20px; padding:8px; border-radius:999px; background:rgba(255,255,255,.78); }}
    .top-nav a {{ padding:10px 14px; color:#5f6b67; background:transparent; border-color:transparent; transition:all .15s ease; white-space:nowrap; }}
    .top-nav a:focus-visible,.mobile-shortcuts a:focus-visible {{ outline:3px solid #80bfa3; outline-offset:3px; border-radius:999px; }}
    .top-nav a[href="#home"] {{ color:#fff; background:var(--deep); }}
    body:has(.page:target) .top-nav a[href="#home"] {{ color:#5f6b67; background:transparent; }}
    body:has(#home:target) .top-nav a[href="#home"],
    body:has(#glance:target) .top-nav a[href="#home"],
    body:has(#bills:target) .top-nav a[href="#bills"],
    body:has(#solar:target) .top-nav a[href="#solar"],
    body:has(#ev:target) .top-nav a[href="#ev"],
    body:has(#whatif:target) .top-nav a[href="#whatif"],
    body:has(#recommendations:target) .top-nav a[href="#whatif"],
    body:has(#battery-investment:target) .top-nav a[href="#whatif"],
    body:has(#timeline:target) .top-nav a[href="#analytics"],
    body:has(#analytics:target) .top-nav a[href="#analytics"],
    body:has(#imports:target) .top-nav a[href="#system"],
    body:has(#system:target) .top-nav a[href="#system"],
    body:has(#system-collector:target) .top-nav a[href="#system"],
    body:has(#system-live-data:target) .top-nav a[href="#system"],
    body:has(#system-data-quality:target) .top-nav a[href="#system"],
    body:has([id^="system-provider-"]:target) .top-nav a[href="#system"],
    body:has(#setup:target) .top-nav a[href="#system"],
    body:has(#setup-billing:target) .top-nav a[href="#system"],
    body:has(#setup-billing-cycle:target) .top-nav a[href="#system"],
    body:has(#setup-tariff:target) .top-nav a[href="#system"],
    body:has(#energy-score-explanation:target) .top-nav a[href="#home"],
    body:has(#home-goals:target) .top-nav a[href="#whatif"] {{ color:#fff; background:var(--deep); border-color:var(--deep); }}
    .page-heading {{ padding:24px 28px; border:0; border-radius:28px; box-shadow:var(--shadow); }}
    .page-heading h2 {{ font-size:34px; letter-spacing:-.055em; }} .page-heading p {{ max-width:620px; font-size:14px; }}
    .report-section {{ margin-top:18px; padding:30px; border:0; border-radius:28px; background:rgba(255,255,255,.88); box-shadow:var(--shadow); }}
    .metrics {{ gap:14px; }} .metric {{ min-height:144px; padding:20px; border:0; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .metric span {{ font-size:12px; font-weight:750; }} .metric strong {{ margin-top:24px; font-size:30px; }} .metric small {{ max-width:94%; font-size:11px; }}
    .metric.primary {{ background:linear-gradient(135deg,#111816,#1f6b50); }} .metric.primary strong {{ font-size:46px; }}
    .home-metrics .metric {{ min-height:154px; }} #home .compact-section {{ margin-top:14px; }}
    .chart-card,.trend-card,.timeline-entry,.ev-session-card,.ev-session-recommendations,.ev-session-history,.system-card,.decision-grid div,.scenario-card,.action-card,.recommendation-card,.setup-task {{ border:0; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .setup-progress-card {{ display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:14px; padding:20px 22px; background:#fff; border-radius:24px; box-shadow:var(--shadow); }}
    .setup-progress-card span,.setup-task-main span,.setup-task-meta span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .setup-progress-card h3 {{ margin:3px 0 0; font-size:24px; letter-spacing:-.04em; }}
    .setup-progress-card p {{ margin:8px 0 0; color:var(--muted); font-size:12px; }}
    .setup-progress-card ul {{ margin:4px 0 0; padding-left:18px; color:var(--muted); font-size:12px; }}
    .setup-progress-card a,.setup-task-meta a {{ padding:10px 12px; color:#fff; background:var(--deep-2); border-radius:10px; text-decoration:none; font-size:12px; font-weight:750; }}
    .setup-progress-card.complete {{ color:#226b50; background:#f3fbf7; }}
    .setup-meter {{ height:12px; overflow:hidden; background:#eef3f0; border-radius:999px; }}
    .setup-meter span {{ display:block; height:100%; min-width:2px; background:linear-gradient(90deg,#48b78d,var(--green)); border-radius:inherit; }}
    .setup-task-list {{ display:grid; gap:12px; }}
    .setup-task {{ display:grid; grid-template-columns:1fr 300px; gap:16px; padding:18px; background:#fff; }}
    .setup-task.good {{ background:#f3fbf7; }} .setup-task.bad {{ background:#fff5f3; }} .setup-task.neutral {{ background:#f8fbf9; }}
    .setup-task h3 {{ margin:6px 0 0; font-size:18px; letter-spacing:-.025em; }}
    .setup-task p {{ margin:6px 0 0; color:var(--muted); font-size:12px; }}
    .setup-task-meta {{ display:grid; grid-template-columns:1fr; gap:8px; align-content:start; }}
    .setup-task-meta div {{ padding:10px; background:rgba(255,255,255,.72); border-radius:11px; }}
    .setup-task-meta strong {{ display:block; margin-top:3px; font-size:13px; }}
    .setup-config-form {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; align-items:end; }}
    .first-time-question {{ grid-column:1/-1; display:grid; grid-template-columns:44px 1fr; gap:14px; align-items:start; padding:16px; background:#fff; border:1px solid var(--line); border-radius:14px; }}
    .first-time-question > span {{ display:grid; place-items:center; width:32px; height:32px; color:#fff; background:var(--deep-2); border-radius:50%; font-size:13px; font-weight:900; }}
    .first-time-question h3 {{ margin:0 0 10px; font-size:18px; letter-spacing:-.025em; }}
    .first-time-question p {{ margin:8px 0 0; color:var(--muted); font-size:12px; }}
    .setup-form-row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .setup-config-form label span {{ display:block; margin-bottom:5px; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .setup-config-form input,.setup-config-form select,.setup-config-form button {{ width:100%; min-height:42px; padding:9px 11px; color:var(--ink); background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font:inherit; }}
    .setup-config-form button {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); font-weight:750; cursor:pointer; }}
    .catalogue-tariff-previews,.manual-tariff-fields {{ grid-column:1/-1; }}
    .manual-tariff-fields {{ display:none; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:0; padding:14px; border:1px solid var(--line); border-radius:12px; background:#fff; }}
    .manual-tariff-fields legend {{ padding:0 6px; color:var(--ink); font-size:12px; font-weight:850; }}
    .manual-tariff-fields .notice {{ grid-column:1/-1; margin:0; }}
    .tariff-preview-card {{ display:none; padding:14px; background:#fff; border:1px solid var(--line); border-radius:12px; }}
    .tariff-preview-card h3 {{ margin:2px 0 10px; }}
    .tariff-preview-card span,.tariff-window-list span {{ display:block; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .tariff-preview-card dl {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:0; }}
    .tariff-preview-card dl div {{ padding:9px; background:#f8fbf9; border:1px solid var(--line); border-radius:10px; }}
    .tariff-preview-card dt {{ color:var(--muted); font-size:10px; font-weight:750; }}
    .tariff-preview-card dd {{ margin:3px 0 0; color:var(--ink); font-size:13px; font-weight:850; }}
    .tariff-window-list {{ margin-top:10px; }}
    .tariff-window-list ul {{ margin:6px 0 0 18px; padding:0; color:var(--muted); font-size:12px; }}
    .bill-change-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr)); gap:12px; margin-top:14px; }}
    .bill-change-card,.bill-change-ev {{ padding:14px; background:#fff; border:1px solid var(--line); border-radius:12px; }}
    .bill-change-card-readable {{ max-width:min(100%, 860px); }}
    .bill-change-card h3 {{ margin:0 0 10px; font-size:14px; }}
    .bill-change-grid-story .bill-change-card.main {{ grid-column:1 / -1; }}
    .bill-change-card-summary {{ display:grid; gap:14px; }}
    .bill-change-summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
    .bill-change-summary-block {{ display:grid; gap:8px; }}
    .bill-change-summary-block > span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.09em; text-transform:uppercase; }}
    .bill-change-row {{ display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:16px; padding:14px 0; border-top:1px solid var(--line); }}
    .bill-change-row:first-of-type {{ border-top:0; padding-top:0; }}
    .bill-change-copy strong {{ display:block; font-size:15px; line-height:1.3; }}
    .bill-change-copy {{ max-width:60ch; }}
    .bill-change-list span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .bill-change-row p {{ margin:4px 0 0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .bill-change-amount,.bill-change-list strong {{ min-width:84px; text-align:right; font-variant-numeric:tabular-nums; }}
    .bill-change-row.good > .bill-change-amount,.bill-change-list li.good strong {{ color:var(--green); }}
    .bill-change-row.bad > .bill-change-amount,.bill-change-list li.bad strong {{ color:var(--red); }}
    .bill-change-row.neutral > .bill-change-amount,.bill-change-list li.neutral strong {{ color:var(--ink); }}
    .bill-change-list {{ display:grid; gap:8px; margin:0; padding:0; list-style:none; }}
    .bill-change-list li {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; padding:9px 10px; background:#f8fbf9; border:1px solid var(--line); border-radius:10px; }}
    .bill-change-ev {{ margin-top:12px; }}
    .bills-actions-section .action-grid {{ grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }}
    .bills-actions-section .action-card {{ display:grid; align-content:start; min-height:0; }}
    .bills-advanced-section details + details {{ margin-top:12px; }}
    .setup-config-form:has(select[name="current_plan"] option[value="synergy_home_plan"]:checked) .tariff-preview-card[data-plan="synergy_home_plan"],
    .setup-config-form:has(select[name="current_plan"] option[value="synergy_ev_add_on"]:checked) .tariff-preview-card[data-plan="synergy_ev_add_on"],
    .setup-config-form:has(select[name="current_plan"] option[value="previous_flat_or_home_plan"]:checked) .tariff-preview-card[data-plan="previous_flat_or_home_plan"] {{ display:block; }}
    .setup-config-form:has(select[name="current_plan"] option[value="manual_tariff"]:checked) .catalogue-tariff-previews {{ display:none; }}
    .setup-config-form:has(select[name="current_plan"] option[value="manual_tariff"]:checked) .manual-tariff-fields {{ display:grid; }}
    .action-grid {{ gap:14px; }} .action-card {{ padding:20px; }}
    .action-card h3,.recommendation-card h3 {{ font-size:19px; letter-spacing:-.03em; }}
    .scenario-card {{ min-height:210px; padding:22px; }} .scenario-card strong {{ font-size:32px; }}
    .source-status {{ border:0; border-radius:24px; }}
    code {{ padding:2px 5px; background:#eef4f1; border-radius:6px; }}
    .notice {{ padding:10px 12px; border-radius:10px; background:var(--sun-soft); color:#765b20; }}
    .ev-caveat {{ display:flex; align-items:center; justify-content:space-between; gap:24px; margin-top:24px; padding:16px 18px; color:#315f74; background:var(--blue-soft); border:1px solid #d5e7ed; border-radius:16px; }}
    .ev-caveat div {{ min-width:220px; }} .ev-caveat span,.ev-caveat strong {{ display:block; }} .ev-caveat span {{ font-size:9px; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }} .ev-caveat strong {{ margin-top:3px; font-size:13px; }}
    .ev-caveat p {{ margin:0; color:#53798a; font-size:11px; }}
    footer {{ padding:24px 0 0; color:var(--muted); text-align:center; font-size:10px; }}
    @media (max-width:900px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }} .charts,.trend-grid,.recommendation-grid,.scenario-grid,.plan-detail-grid,.bill-change-grid,.action-grid,.briefing-grid,.briefing-grid.two-up,.home-status-grid,.energy-score-card,.energy-score-factors,.energy-score-breakdown,.decision-explanation-grid,.decision-grid,.system-grid,.system-overview-grid,.config-grid,.import-form,.setup-task,.setup-config-form,.setup-form-row,.manual-tariff-fields,.tariff-preview-card dl,.onboarding-steps,.provider-status-grid,.provider-guidance-grid,.provider-account-summary,.home-page-link-grid,.home-forecast-grid,.home-forecast-summary,.home-historical-trend-grid,.home-glance-card,.home-timeline-row,.bills-layout-grid,.bills-overview-grid,.comparison-summary-list,.bills-plan-grid,.battery-metrics,.battery-tradeoff-grid,.tariff-update-hero,.tariff-decision-actions {{ grid-template-columns:1fr; }} .household-timeline-form .timeline-notes {{ grid-column:auto; }} .tariff-update-hero > strong {{ white-space:normal; }} .tariff-comparison-row {{ grid-template-columns:1fr; }} .trend-card:last-child {{ grid-column:auto; }} .period-controls form {{ grid-template-columns:1fr 1fr; }} .top-nav {{ justify-content:flex-start; overflow-x:auto; flex-wrap:nowrap; -webkit-overflow-scrolling:touch; }} }}
    @media (max-width:620px) {{
      body {{ font-size:14px; }} main {{ width:min(100% - 16px,1180px); margin:8px auto 36px; padding-bottom:60px; }} .hero,.report-section {{ padding:18px; border-radius:18px; }}
      .mobile-shortcuts {{ position:sticky; top:0; z-index:20; display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin:8px 0 10px; padding:7px; background:rgba(255,255,255,.92); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); backdrop-filter:blur(14px); }}
      .mobile-shortcuts a {{ min-width:0; padding:9px 7px; color:#fff; background:var(--deep); border-radius:10px; text-align:center; text-decoration:none; font-size:11px; font-weight:800; }}
      .top-nav {{ position:relative; top:auto; margin:10px 0 14px; border-radius:16px; }}
      .top-nav a {{ flex:0 0 auto; min-height:38px; padding:9px 12px; }}
      .hero-top,.hero-summary,.section-title {{ align-items:flex-start; flex-direction:column; }} .generated {{ text-align:left; }}
      .hero-summary {{ margin-top:30px; }} .hero-summary strong {{ font-size:44px; letter-spacing:-.04em; }} .hero-note {{ max-width:none; }} .metrics,.insights {{ grid-template-columns:1fr; }}
      .page-heading {{ padding:18px; border-radius:18px; }} .page-heading h2 {{ font-size:26px; letter-spacing:-.03em; }}
      .section-title h2 {{ font-size:24px; letter-spacing:-.03em; }} .section-title p {{ font-size:12px; }}
      .metric {{ min-height:auto; padding:16px; }} .metric span {{ max-width:none; }} .metric strong,.metric.primary strong {{ margin-top:16px; font-size:26px; letter-spacing:-.03em; }}
      .status-strip {{ grid-template-columns:1fr; }}
      .period-controls {{ padding:14px; }} .period-controls form {{ grid-template-columns:1fr; }} .period-controls button,.setup-config-form button,.import-form button {{ min-height:46px; }}
      .first-time-question {{ grid-template-columns:1fr; gap:10px; padding:14px; }}
      .disclosure-panel summary {{ align-items:flex-start; flex-direction:column; }} .disclosure-panel summary small {{ text-align:left; }} .disclosure-panel summary::after {{ position:absolute; right:17px; }}
      .battery-accordion summary {{ align-items:flex-start; flex-direction:column; padding-right:48px; position:relative; }}
      .battery-accordion summary small {{ text-align:left; }}
      .battery-accordion summary::after {{ position:absolute; right:16px; top:14px; }}
      .battery-table-wrap .battery-homeowner-table,.battery-seasonal-table-wrap .battery-homeowner-table {{ min-width:720px; }}
      .actual-ev-head {{ flex-direction:column; }} .actual-ev-metrics {{ grid-template-columns:1fr 1fr; }} .actual-ev-breakdown-row {{ grid-template-columns:1fr; gap:6px; }}
      .ev-session-grid,.ev-insight-metrics {{ grid-template-columns:1fr; }} .ev-tariff-row {{ grid-template-columns:1fr; gap:6px; }}
      .timeline-entry {{ grid-template-columns:1fr; gap:8px; padding:15px; }} .timeline-stamp {{ justify-content:flex-start; }} .timeline-meta small {{ width:100%; }}
      .hero-alert {{ align-items:flex-start; flex-direction:column; }}
      .auth-alert-heading {{ align-items:flex-start; flex-direction:column; }}
      .ev-caveat {{ align-items:flex-start; flex-direction:column; gap:8px; }}
      .briefing-hero {{ flex-direction:column; padding:20px; }} .home-assistant-hero {{ min-height:0; }} .briefing-readtime {{ flex:0 0 auto; width:100%; text-align:left; }}
      .home-inline-summary {{ align-items:flex-start; flex-direction:column; }}
      .home-status-tile {{ min-height:auto; padding:16px; }} .home-status-tile h3 {{ margin-top:16px; font-size:20px; }}
      .briefing-grid,.briefing-grid.two-up {{ grid-template-columns:1fr; }}
      .metric.primary {{ grid-column:auto; }} .bar-row {{ grid-template-columns:105px 1fr 66px; gap:8px; }} .peak-badge {{ display:none; }}
      .bar-label {{ min-width:0; }} .bar-label span {{ overflow-wrap:anywhere; }}
      .chart-card {{ padding:16px; }} .chart-heading {{ align-items:flex-start; flex-direction:column; gap:2px; }}
      .trend-card {{ padding:16px; }} .trend-row {{ grid-template-columns:48px 1fr 68px; gap:7px; }}
      .glance-display {{ gap:18px; padding:22px; }}
      .glance-topline {{ flex-direction:column; }}
      .glance-freshness {{ width:100%; text-align:left; }}
      .glance-grid {{ grid-template-columns:1fr; }}
      .glance-panel.wide {{ grid-column:auto; }}
      .glance-status {{ font-size:54px; line-height:.9; overflow-wrap:anywhere; }}
      .glance-reason {{ font-size:25px; }}
      .glance-panel strong {{ font-size:24px; }}
      .glance-panel.score strong {{ font-size:54px; }}
      .setup-progress-card {{ align-items:flex-start; flex-direction:column; }} .setup-progress-card a,.setup-task-meta a {{ width:100%; text-align:center; }}
      .provider-form-grid {{ grid-template-columns:1fr; }} .provider-action-row button {{ flex:1 1 140px; }}
      .completeness-row {{ grid-template-columns:1fr; }} .completeness-row strong {{ overflow-wrap:anywhere; }}
      .system-card table,.import-panel table {{ min-width:640px; }}
    }}
    @media print {{
      @page {{ size:A4; margin:10mm; }} * {{ print-color-adjust:exact; -webkit-print-color-adjust:exact; }}
      body {{ background:#fff; font-size:11px; }} main {{ width:100%; margin:0; }} .hero {{ padding:20px; border-radius:16px; box-shadow:none; }}
      .top-nav,.period-controls {{ display:none; }} .page {{ display:block !important; }} .page-heading {{ box-shadow:none; break-before:page; }}
      .hero-summary {{ margin-top:25px; }} .hero-summary strong {{ font-size:44px; }} .report-section {{ break-inside:avoid; margin-top:12px; padding:16px; border-radius:14px; box-shadow:none; }}
      .metrics {{ grid-template-columns:repeat(4,1fr); gap:7px; }} .metric {{ min-height:90px; padding:11px; }} .metric strong {{ margin-top:12px; font-size:17px; }}
      .metric.primary strong {{ font-size:25px; }} .charts {{ gap:8px; }} .chart-card {{ padding:12px; }} .bar-chart {{ gap:8px; }}
      .bar-row {{ grid-template-columns:95px 1fr 55px; gap:7px; }} .insights {{ gap:7px; }} .insight {{ padding:11px; }} footer {{ padding-top:10px; }}
      .trend-grid {{ grid-template-columns:1fr 1fr; gap:8px; }} .trend-card {{ padding:12px; }} .trend-card:last-child {{ grid-column:1 / -1; }}
      .trend-list {{ gap:7px; }} .trend-row {{ grid-template-columns:42px 1fr 55px; gap:5px; }}
      .recommendation-grid {{ grid-template-columns:1fr 1fr; gap:8px; }} .recommendation-card {{ padding:12px; break-inside:avoid; }}
      .scenario-grid {{ grid-template-columns:1fr 1fr; gap:8px; }} .scenario-card,.scenario-insights {{ padding:12px; break-inside:avoid; }}
      .timeline-entry {{ padding:12px; break-inside:avoid; }}
      .ev-session-grid {{ grid-template-columns:1fr; gap:8px; }} .ev-session-card,.ev-session-recommendations,.ev-session-history {{ padding:12px; break-inside:avoid; }}
      .ev-session-history th,.ev-session-history td {{ padding:6px; font-size:9px; }}
      .auth-alert {{ break-inside:avoid; }}
    }}
  </style>
</head>
<body{body_class}><main>
  {chrome_html}
  {pages_html}
  {footer_html}
</main>
{"<script src='/assets/dashboard.js' defer></script>" if not glance_only else ""}
</body></html>
"""
    perf_record_timing("page-section rendering", render_started)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
    return document


def build_live_html(
    period_kind: str = "current_billing_period",
    start: Optional[str] = None,
    end: Optional[str] = None,
    glance_only: bool = False,
    provider_flash: Optional[dict] = None,
    tariff_update_flash: Optional[dict] = None,
    historical_backfill_flash: Optional[dict] = None,
    period_flash: Optional[dict] = None,
) -> str:
    configure_demo_mode()
    settings = load_settings()
    current_day = local_today()
    selected = resolve_period(period_kind, settings, current_day, start, end)
    if current_context() is None:
        with performance_context(period_payload(selected)):
            return build_live_html(
                period_kind,
                start=start,
                end=end,
                glance_only=glance_only,
                provider_flash=provider_flash,
                tariff_update_flash=tariff_update_flash,
                historical_backfill_flash=historical_backfill_flash,
                period_flash=period_flash,
            )
    cache_allowed = not any(
        (provider_flash, tariff_update_flash, historical_backfill_flash, period_flash)
    )
    cache_key = json.dumps(
        {
            "period": period_payload(selected),
            "glance_only": bool(glance_only),
            "signature": report_cache_signature(settings),
        },
        default=str,
        sort_keys=True,
    )
    if cache_allowed:
        cached = LIVE_HTML_CACHE.get(cache_key)
        if cached is not None:
            perf_increment("html_cache_hits")
            return cached
        perf_increment("html_cache_misses")
    billing = resolve_period("current_billing_period", settings, current_day)
    compared = (
        resolve_period("previous_billing_period", settings, current_day)
        if selected.kind == "current_billing_period"
        else previous_equivalent(selected)
    )
    with perf_stage("database connection/setup"):
        conn_context = sqlite3.connect(DB_PATH)
    with conn_context as conn:
        validate_database(conn)
        today_summary = load_summary(conn, current_day, current_day, settings)
        selected_summary = load_summary(conn, selected.start, selected.end, settings)
        billing_summary = (
            selected_summary
            if selected.start == billing.start and selected.end == billing.end
            else load_summary(conn, billing.start, billing.end, settings)
        )
        compared_summary = load_summary(conn, compared.start, compared.end, settings)
        trends = load_daily_trends(conn, selected.start, selected.end, settings)
        with perf_stage("provider/health diagnostics"):
            health = health_data(conn, current_day, settings)
        energy_timeline = build_energy_timeline(conn, current_day, settings, now=datetime.now(LOCAL_TIMEZONE))
        all_recommendations = load_recommendations(conn, selected_summary, settings)
        recommendations = active_recommendations(all_recommendations)
        hidden = hidden_recommendations(all_recommendations)
        recommendation_lifecycle_counts = data_engine(conn).get_recommendation_lifecycle_counts()
        comparison = compare_summaries(selected, selected_summary, compared, compared_summary)
        timeline_candidates = build_decision_timeline(
            selected_summary,
            recommendations,
            comparison,
            include_billing_period=selected.kind == "current_billing_period",
        )
        timeline = data_engine(conn).persist_timeline_entries(timeline_candidates.entries)
        overnight_baseload_watts = data_engine(conn).get_overnight_baseload_watts(selected.start, selected.end)
    runtime_check = runtime_check_data()
    html_document = write_html(
        today_summary,
        selected_summary,
        trends,
        settings,
        health,
        recommendations,
        selected,
        comparison,
        output_path=None,
        timeline=timeline,
        recommendation_lifecycle_counts=recommendation_lifecycle_counts,
        hidden=hidden,
        billing_summary=billing_summary,
        glance_only=glance_only,
        overnight_baseload_watts=overnight_baseload_watts,
        provider_flash=provider_flash,
        tariff_update_flash=tariff_update_flash,
        historical_backfill_flash=historical_backfill_flash,
        period_flash=period_flash,
        runtime_check=runtime_check,
        energy_timeline=energy_timeline,
    )
    if cache_allowed:
        LIVE_HTML_CACHE.set(cache_key, html_document)
    return html_document


def generate_report(output_path: Optional[Path] = None) -> str:
    settings = load_settings()
    current_day = local_today()
    period_start, period_end = billing_period(current_day, settings)
    selected = Period("current_billing_period", "Current billing period", period_start, period_end)
    if current_context() is None:
        with performance_context(period_payload(selected)):
            return generate_report(output_path)
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    with perf_stage("database connection/setup"):
        conn_context = sqlite3.connect(DB_PATH)
    with conn_context as conn:
        validate_database(conn)
        today_summary = load_summary(conn, current_day, current_day, settings)
        period_summary = load_summary(conn, period_start, period_end, settings)
        trends = load_daily_trends(conn, period_start, period_end, settings)
        with perf_stage("provider/health diagnostics"):
            health = health_data(conn, current_day, settings)
        all_recommendations = load_recommendations(conn, period_summary, settings)
        recommendations = active_recommendations(all_recommendations)
        hidden = hidden_recommendations(all_recommendations)
        recommendation_lifecycle_counts = data_engine(conn).get_recommendation_lifecycle_counts()
        previous = resolve_period("previous_billing_period", settings, current_day)
        previous_summary = load_summary(conn, previous.start, previous.end, settings)
        comparison = compare_summaries(
            selected,
            period_summary,
            previous,
            previous_summary,
        )
        timeline_candidates = build_decision_timeline(
            period_summary,
            recommendations,
            comparison,
            include_billing_period=True,
        )
        timeline = data_engine(conn).persist_timeline_entries(timeline_candidates.entries)
        overnight_baseload_watts = data_engine(conn).get_overnight_baseload_watts(period_start, period_end)

    health["last_successful_report_generation"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    health["report_age_minutes"] = 0.0
    print_summary("Today summary", today_summary, settings)
    print()
    print_summary("Current billing period summary", period_summary, settings)
    return write_html(
        today_summary,
        period_summary,
        trends,
        settings,
        health,
        recommendations,
        Period("current_billing_period", "Current billing period", period_start, period_end),
        comparison,
        timeline=timeline,
        recommendation_lifecycle_counts=recommendation_lifecycle_counts,
        hidden=hidden,
        overnight_baseload_watts=overnight_baseload_watts,
        output_path=output_path,
    )


def main() -> None:
    configure_demo_mode()
    setup_logging()
    generate_report(REPORT_PATH)
    logging.info(
        "Generated report at %s",
        REPORT_PATH,
    )
    print()
    print(f"HTML report: {REPORT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Report generation failed")
        raise
