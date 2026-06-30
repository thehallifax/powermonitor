#!/usr/bin/env python3
import html
import json
import logging
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from collector_health import public_health
from periods import LOCAL_TIMEZONE, Period, previous_equivalent, resolve_period

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.briefing_engine import BriefingEngine
from engine.advisor_presenter import attach_decision_to_advisors, explain_household_decisions
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
from engine.financial_engine import FinancialEngine
from engine.goals_engine import GoalsEngine
from engine.household_state_engine import HouseholdStateEngine
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
from engine.provider_runtime import ProviderRuntime
from engine.recommendation_adapter import RecommendationPresentation
from engine.solar_advisor import SolarAdvisor
from engine.setup_engine import SetupEngine
from presentation.briefing import BriefingItem
from presentation.energy_score import build_energy_score
from presentation.glance import GlanceState, build_glance_state
from presentation.timeline import build_decision_timeline

DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
REPORT_PATH = PROJECT_ROOT / "reports" / "index.html"
LOG_PATH = PROJECT_ROOT / "logs" / "report.log"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
COLLECT_LOG_PATH = PROJECT_ROOT / "logs" / "collect.log"

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
BUCKET_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}


def data_engine(conn: Optional[sqlite3.Connection] = None) -> DataEngine:
    return DataEngine(
        db_path=DB_PATH,
        connection=conn,
        collect_log_path=COLLECT_LOG_PATH,
        report_path=REPORT_PATH,
        health_loader=public_health,
    )


def financial_engine(household_context: Optional[HouseholdContext] = None) -> FinancialEngine:
    return FinancialEngine(household_context=household_context)


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

    return [OcularEVImporter(DB_PATH).source]


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
    "supply_charge_per_day": 1.292269,
    "import_rates": {
        "super_off_peak": 0.086151,
        "overnight": 0.193841,
        "off_peak": 0.236916,
        "peak": 0.538446,
    },
    "export_credit_rate": 0.02,
    "ev_baseline_watts": 250.0,
    "ev_data_source": "estimated",
    "home_assistant_url": "http://192.168.4.96:8123",
    "billing_period_start_day": 1,
    "current_billing_period_start": None,
    "current_billing_period_end": None,
    "previous_billing_period_start": None,
    "previous_billing_period_end": None,
    "billing_cycle_configured": False,
    "retailer": "",
    "current_plan": "synergy_ev_add_on",
    "comparison_plan": "previous_flat_or_home_plan",
    "comparison_plan_rates": {
        "flat_import_rate": 0.30,
        "supply_charge_per_day": 1.292269,
        "export_credit_rate": 0.02,
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
    supply_charge_per_day: float
    import_rates: dict
    export_credit_rate: float
    ev_baseline_watts: float
    ev_data_source: str
    home_assistant_url: str
    billing_period_start_day: int
    current_billing_period_start: Optional[date]
    current_billing_period_end: Optional[date]
    previous_billing_period_start: Optional[date]
    previous_billing_period_end: Optional[date]
    billing_cycle_configured: bool
    retailer: str
    current_plan: str
    comparison_plan: str
    comparison_plan_rates: dict
    petrol_price_per_litre: float
    previous_car_l_per_100km: float
    ev_kwh_per_100km: Optional[float]
    ev_odometer_km: Optional[float]
    car_comparison_enabled: bool
    recommendation_thresholds: dict


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


def local_today() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def dashboard_data(
    today: Optional[date] = None,
    period_kind: str = "current_billing_period",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    current_day = today or local_today()
    settings = load_settings()
    selected = resolve_period(period_kind, settings, current_day, start, end)
    billing = resolve_period("current_billing_period", settings, current_day)
    compared = (
        resolve_period("previous_billing_period", settings, current_day)
        if selected.kind == "current_billing_period"
        else previous_equivalent(selected)
    )
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    with sqlite3.connect(DB_PATH) as conn:
        validate_database(conn)
        context_engine = data_engine(conn)
        household_context = context_engine.get_household_context()
        today_summary = load_summary(conn, current_day, current_day, settings)
        period_summary = load_summary(conn, selected.start, selected.end, settings)
        billing_summary = (
            period_summary
            if selected.start == billing.start and selected.end == billing.end
            else load_summary(conn, billing.start, billing.end, settings)
        )
        compared_summary = load_summary(conn, compared.start, compared.end, settings)
        trends = load_daily_trends(conn, selected.start, selected.end, settings)
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
        provider_observations = context_engine.get_provider_observation_summary()
        promotion_summary = context_engine.promote_provider_observations()
    scenarios = scenario_analysis(period_summary, settings)
    completeness = data_completeness(period_summary, settings)
    plan = plan_comparison(period_summary, settings)
    energy_score = build_energy_score(
        period_summary,
        recommendations,
        completeness,
        plan,
    )
    glance_projected, _glance_end, glance_basis, _glance_impact = financial_engine(household_context).project_bill(
        billing_summary,
        settings,
    )
    generated_at = datetime.now().astimezone()
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
        "tariff_scenario": tariff_scenario(period_summary, settings),
        "plan_comparison": plan,
        "scenario_analysis": scenarios,
        "billing_scenarios": scenarios["scenarios"],
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
    home_assistant_adapter = HomeAssistantAdapter()
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
        settings=settings,
    )


def provider_runtime_data(data_engine_instance=None) -> ProviderRuntime:
    from collect_today import SolarEdgeAdapter
    from home_assistant_provider import HomeAssistantAdapter

    return ProviderRuntime([SolarEdgeAdapter(), HomeAssistantAdapter()], data_engine=data_engine_instance)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_settings() -> Settings:
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
        supply_charge_per_day=float(raw["supply_charge_per_day"]),
        import_rates=rates,
        export_credit_rate=float(raw["export_credit_rate"]),
        ev_baseline_watts=float(raw["ev_baseline_watts"]),
        ev_data_source=str(raw["ev_data_source"]),
        home_assistant_url=str(raw["home_assistant_url"]),
        billing_period_start_day=int(raw["billing_period_start_day"]),
        current_billing_period_start=optional_date("current_billing_period_start"),
        current_billing_period_end=optional_date("current_billing_period_end"),
        previous_billing_period_start=optional_date("previous_billing_period_start"),
        previous_billing_period_end=optional_date("previous_billing_period_end"),
        billing_cycle_configured=bool(raw.get("billing_cycle_configured")),
        retailer=str(raw.get("retailer") or ""),
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
    )
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
    return data_engine(conn).get_ev_sessions(start, end, sources=sources)


def ev_session_analytics(sessions: list[dict], settings: Settings) -> dict:
    if not sessions:
        return {
            "available": False,
            "overview": {},
            "tariff_usage": empty_ev_breakdown(),
            "insights": {},
            "recommendations": [],
            "sessions": [],
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
    }


def load_summary(conn: sqlite3.Connection, start: date, end: date, settings: Settings) -> Summary:
    start_text = start.isoformat()
    end_exclusive = (end + timedelta(days=1)).isoformat()
    import_cost_expression, cost_params = cost_sql(settings)
    household_id = data_engine(conn)._active_household_id()
    row = conn.execute(
        f"""
        SELECT
            COUNT(*),
            COUNT(DISTINCT substr(measurement_time, 1, 10)),
            COALESCE(SUM(grid_to_home_kwh), 0),
            COALESCE(SUM({import_cost_expression}), 0),
            COALESCE(SUM(production_kwh), 0),
            COALESCE(SUM(solar_to_home_kwh), 0),
            COALESCE(SUM(solar_to_grid_kwh), 0),
            COALESCE(SUM(
                CASE WHEN tariff_bucket = 'overnight'
                     THEN MAX(COALESCE(grid_to_home_w, 0) - ?, 0) * 0.25 / 1000.0
                     ELSE 0 END
            ), 0)
        FROM readings
        WHERE household_id = ?
          AND measurement_time >= ? AND measurement_time < ?
        """,
        (*cost_params, settings.ev_baseline_watts, household_id, start_text, end_exclusive),
    ).fetchone()

    buckets = {
        bucket: {"kwh": 0.0, "cost": 0.0}
        for bucket in BUCKET_ORDER
    }
    for bucket, kwh in conn.execute(
        """
        SELECT tariff_bucket, COALESCE(SUM(grid_to_home_kwh), 0)
        FROM readings
        WHERE household_id = ?
          AND measurement_time >= ? AND measurement_time < ?
        GROUP BY tariff_bucket
        """,
        (household_id, start_text, end_exclusive),
    ):
        buckets.setdefault(bucket, {"kwh": 0.0, "cost": 0.0})
        buckets[bucket] = {
            "kwh": float(kwh),
            "cost": float(kwh) * settings.import_rates.get(bucket, 0),
        }

    intervals, reading_days, grid_import, import_cost, generated, used, exported, estimated_ev_charging = row
    actual_ev_charging = None
    actual_ev_cost = None
    actual_ev_breakdown = empty_ev_breakdown()
    actual_ev_partial = False
    actual_source = "estimated"
    actual_sessions = load_actual_ev_sessions(conn, start, end, settings)
    analytics = ev_session_analytics(actual_sessions, settings)
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
    finances = financial_engine()
    supply_cost = supply_days * settings.supply_charge_per_day
    export_credit = finances.calculate_export_credit(float(exported), settings.export_credit_rate)
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
            else finances.cost_of_ev_session(float(estimated_ev_charging), settings.import_rates["overnight"])
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
    )


def tariff_scenario(summary: Summary, settings: Settings) -> dict:
    rates = settings.comparison_plan_rates
    finances = financial_engine()
    comparison_import = finances.calculate_import_cost(summary.grid_import_kwh, rates["flat_import_rate"])
    comparison_supply = summary.supply_days * rates["supply_charge_per_day"]
    comparison_credit = finances.calculate_export_credit(summary.solar_exported_kwh, rates["export_credit_rate"])
    comparison_net = finances.calculate_net_bill(comparison_import, comparison_supply, comparison_credit)
    return {
        "current_plan": settings.current_plan,
        "comparison_plan": settings.comparison_plan,
        "current_plan_net_cost": summary.net_cost,
        "comparison_plan_net_cost": comparison_net,
        "difference": summary.net_cost - comparison_net,
        "estimated_savings": comparison_net - summary.net_cost,
    }


def ev_cost_under_rate(summary: Summary, rate: float) -> float:
    return financial_engine().cost_of_ev_session(summary.ev_charging_kwh, rate)


def weighted_average_import_rate(summary: Summary) -> float:
    return financial_engine().weighted_average_import_rate(summary)


def plan_comparison(summary: Summary, settings: Settings) -> dict:
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


def scenario_analysis(summary: Summary, settings: Settings) -> dict:
    actual_bill = summary.net_cost
    ev_kwh = summary.ev_charging_kwh
    ev_actual_cost = summary.ev_charging_cost
    cheapest_bucket = min(BUCKET_ORDER, key=lambda bucket: settings.import_rates.get(bucket, 0))
    peak_bucket = max(BUCKET_ORDER, key=lambda bucket: settings.import_rates.get(bucket, 0))

    scenarios = []
    for scenario_id, title, bucket in (
        ("ev_all_super_off_peak", "What if every EV session occurred in the 9am-3pm EV window?", "super_off_peak"),
        ("ev_all_overnight", "What if every EV session occurred overnight?", "overnight"),
        ("ev_all_peak", "What if every EV session occurred during Peak?", peak_bucket),
    ):
        scenario_ev_cost = ev_kwh * settings.import_rates[bucket]
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
    lost_export_credit = summary.solar_exported_kwh * settings.export_credit_rate
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

    battery_shift_kwh = min(10.0, summary.buckets.get("peak", {}).get("kwh", 0.0))
    battery_savings = battery_shift_kwh * settings.import_rates[peak_bucket] - battery_shift_kwh * settings.export_credit_rate
    scenarios.append(
        scenario_card(
            "battery_shift_peak_imports",
            "What if a 10 kWh home battery shifted Peak imports to solar?",
            actual_bill - max(battery_savings, 0.0),
            actual_bill,
            scenario_confidence(summary),
            f"Models up to {kwh(battery_shift_kwh)} of Peak imports avoided by stored solar.",
        )
    )

    ev_window_cost = ev_kwh * settings.import_rates[cheapest_bucket]
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
    }


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
    import_cost_expression, cost_params = cost_sql(settings)
    household_id = data_engine(conn)._active_household_id()
    rows = conn.execute(
        f"""
        SELECT
            substr(measurement_time, 1, 10) AS day,
            COALESCE(SUM(grid_to_home_kwh), 0),
            COALESCE(SUM(production_kwh), 0),
            COALESCE(SUM(solar_to_grid_kwh), 0),
            COALESCE(SUM({import_cost_expression}), 0),
            COALESCE(SUM(
                CASE WHEN tariff_bucket = 'overnight'
                     THEN MAX(COALESCE(grid_to_home_w, 0) - ?, 0) * 0.25 / 1000.0
                     ELSE 0 END
            ), 0)
        FROM readings
        WHERE household_id = ?
          AND measurement_time >= ? AND measurement_time < ?
        GROUP BY day
        ORDER BY day
        """,
        (*cost_params, settings.ev_baseline_watts, household_id, start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()
    trends = []
    actual_ev_totals = data_engine(conn).get_daily_ev_session_totals(
        start,
        end,
        sources=actual_ev_sources(settings),
    )
    for day_text, grid_import, generated, exported, import_cost, ev_charging in rows:
        ev_value = float(ev_charging)
        ev_source = "estimated"
        actual_ev = actual_ev_totals.get(day_text)
        if actual_ev and float(actual_ev.get("energy_kwh") or 0) > 0:
            ev_value = float(actual_ev["energy_kwh"])
            ev_source = actual_ev["source"]
        export_credit = float(exported) * settings.export_credit_rate
        trends.append(
            DailyTrend(
                day=date.fromisoformat(day_text),
                grid_import_kwh=float(grid_import),
                solar_generated_kwh=float(generated),
                solar_exported_kwh=float(exported),
                import_cost=float(import_cost),
                export_credit=export_credit,
                net_cost=float(import_cost) + settings.supply_charge_per_day - export_credit,
                ev_charging_kwh=ev_value,
                ev_charging_source=ev_source,
            )
        )
    return trends


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

    return f"""
    <section class="report-section ev-session-section">
      <div class="section-title">
        <div><span class="eyebrow">EV charging</span><h2>Actual charging sessions</h2></div>
        <p>{len(sessions)} sessions <span>·</span> {kwh(total_kwh)}</p>
      </div>
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
    <section class="report-section">
      <div class="section-title">
        <div>
          <span class="eyebrow">Energy snapshot</span>
          <h2>{html.escape(title)}</h2>
        </div>
        <p>{summary.start.strftime("%d %b %Y")} <span>to</span> {summary.end.strftime("%d %b %Y")}</p>
      </div>
      {no_data}
      <div class="metrics">{headline_cards}</div>
      <p class="scenario-note">Start here: the bill estimate, daily run-rate, peak exposure, and solar credit usually explain the household story fastest.</p>
      <div class="insights">{insights}</div>
      {disclosure_html("Detailed bill calculations", "Tariff, solar, EV, and average daily components", f'''
      <div class="metrics">{detail_cards}</div>
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
      <div class="page-breadcrumb"><a href="#home">{html.escape(parent)}</a><span>/</span><strong>{html.escape(title)}</strong></div>
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
        rationale = getattr(decision, "rationale", None)
        if rationale:
            return rationale
    explanation = getattr(decision, "explanation", None)
    if explanation and getattr(explanation, "confidence_reason", None):
        return explanation.confidence_reason
    if str(confidence).lower() == "high":
        return "The available data is strong enough to act on this now."
    if str(confidence).lower() == "medium":
        return "Useful signal, with some uncertainty in the supporting data."
    return "PowerMonitor has limited evidence, so treat this as a prompt to review."


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


def hero_html(
    period_summary: Summary,
    generated_at: str,
    health: dict,
    recommendations: list[Decision],
    period_label: str = "Current billing period",
) -> str:
    status_class = "healthy" if health["status"] == "ok" else "warning"
    status_text = (
        "SolarEdge session expired"
        if health["status"] == "SolarEdge session expired"
        else health["status"].title()
    )
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
        <div><span>Collector status</span><strong class="status-value {status_class}">{html.escape(status_text)}</strong></div>
        <div><span>Since successful collection</span><strong>{html.escape(days_since_text)}</strong></div>
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
    if "session expired" not in status and failure_type != "authentication failure":
        return ""
    last_success = display_timestamp(health.get("last_successful_collection"))
    return f"""
    <section class="auth-alert" role="alert">
      <div class="auth-alert-heading">
        <div><span>Collector action required</span><h2>SolarEdge login expired</h2></div>
        <strong>Last successful collection: {html.escape(last_success)}</strong>
      </div>
      <ol>
        <li>Sign in to the SolarEdge monitoring website in your browser.</li>
        <li>Copy the fresh request cookie into <code>collector/solaredge_cookie.txt</code>.</li>
        <li>Run <code>python3 collector/test_cookie.py</code>, then <code>python3 collector/collect_today.py</code>.</li>
      </ol>
      <p>Keep the cookie private. PowerMonitor never displays or logs its value.</p>
    </section>
    """


def period_controls_html(selected: Period) -> str:
    choices = (
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("last_7_days", "Last 7 days"),
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
    return f"""
    <section class="period-controls">
      <form method="get" action="/">
        <label><span>Dashboard period</span><select name="period">{options}</select></label>
        <label><span>Custom start</span><input type="date" name="start" value="{selected.start.isoformat()}"></label>
        <label><span>Custom end</span><input type="date" name="end" value="{selected.end.isoformat()}"></label>
        <button type="submit">Apply period</button>
      </form>
      <p>Showing <strong>{html.escape(selected.label)}</strong>: {selected.start.strftime("%d %b %Y")} to {selected.end.strftime("%d %b %Y")}</p>
    </section>
    """


def nav_html() -> str:
    pages = (
        ("home", "Home"),
        ("setup", "Setup"),
        ("glance", "Glance"),
        ("recommendations", "Recommendations"),
        ("bills", "Bills & Tariffs"),
        ("solar", "Solar"),
        ("ev", "EV Charging"),
        ("whatif", "What If?"),
        ("timeline", "Timeline"),
        ("analytics", "Analytics"),
        ("imports", "Imports"),
        ("system", "System"),
    )
    return (
        '<div class="mobile-shortcuts"><a href="#home">Home</a><a href="#glance">Glance</a><a href="#setup">Setup</a><a href="#system">System</a></div>'
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
          <span>Evidence quality</span>
          <strong>{html.escape(glance.evidence_quality.title())}</strong>
          <small>{html.escape(glance.evidence_reason)}</small>
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
          <strong>{html.escape(glance.solar_status)}</strong>
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
        return "Low", "missing interval data"
    if summary.reading_days / max(summary.supply_days, 1) < 0.5:
        return "Low", "partial SolarEdge interval data"
    if summary.ev_charging_source == "ocular_export":
        return "High", "SolarEdge interval data + actual Ocular sessions"
    if summary.ev_charging_source == "estimated":
        return "Medium", "SolarEdge interval data + estimated EV charging"
    if summary.ev_charging_source == "home_assistant_ocpp":
        return "Medium", "SolarEdge interval data + HA OCPP session snapshots"
    return "Low", "limited source coverage"


def collector_status_text(health: dict) -> str:
    return (
        "SolarEdge session expired"
        if health.get("status") == "SolarEdge session expired"
        else str(health.get("status") or "unknown").title()
    )


def is_recommendation_presentation(item) -> bool:
    return isinstance(item, RecommendationPresentation)


def recommendation_confidence(decision) -> str:
    if is_recommendation_presentation(decision):
        return decision.confidence
    return decision.confidence.title()


def recommendation_impact(decision) -> str:
    if is_recommendation_presentation(decision):
        impact = decision.financial_impact
        if not impact:
            return "Not quantified"
        for value in (impact.today, impact.billing_period, impact.yearly):
            if value is not None:
                return money(float(value))
        return "Not quantified"
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
    return "Not quantified"


def recommendation_monthly_impact(decision) -> str:
    if is_recommendation_presentation(decision):
        impact = decision.financial_impact
        if impact and impact.yearly is not None:
            return money(float(impact.yearly) / 12)
        return "Not quantified"
    value = decision.evidence.get("estimated_monthly_impact")
    if value is None:
        return "Not quantified"
    try:
        return money(float(value))
    except (TypeError, ValueError):
        return str(value)


def recommendation_priority_score(decision) -> int:
    if is_recommendation_presentation(decision):
        return int(decision.display_priority)
    try:
        return int(round(float(decision.evidence.get("priority_score", 0))))
    except (TypeError, ValueError):
        return {"High": 67, "Medium": 34, "Low": 0}.get(decision.priority, 0)


def recommendation_category(decision) -> str:
    if is_recommendation_presentation(decision):
        return decision.category.label
    return str(decision.evidence.get("recommendation_category") or decision.category).replace("_", " ")


def recommendation_difficulty(decision) -> str:
    if is_recommendation_presentation(decision):
        return f"{decision.urgency.label} urgency"
    return str(decision.evidence.get("difficulty") or "Medium").title()


def recommendation_title(decision) -> str:
    return decision.title


def recommendation_action(decision) -> str:
    return decision.recommended_action if is_recommendation_presentation(decision) else decision.recommendation


def recommendation_why(decision) -> str:
    if is_recommendation_presentation(decision):
        return decision.rationale
    return (
        decision.explanation.why_this_matters
        if decision.explanation
        else "This recommendation is based on available Recommendation Engine evidence."
    )


def recommendation_evidence_html(decision) -> str:
    if is_recommendation_presentation(decision):
        if not decision.evidence:
            return '<p class="notice">No supporting evidence is available for this recommendation.</p>'
        rows = [
            f"<li><strong>{html.escape(item.signal.replace('_', ' ').capitalize())}</strong><span>{html.escape(str(item.value))}</span><small>{html.escape(item.source)} - {html.escape(item.quality)}</small></li>"
            for item in decision.evidence
        ]
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
            f"<li><strong>{html.escape(label)}</strong><span>{html.escape(str(value))}</span></li>"
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
        return decision.decision
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
        return '<p><strong>Household decision</strong>No matching HouseholdDecision emitted yet.</p>'
    reasons = household_decision.get("reasons") or []
    reason = reasons[0].get("summary") if reasons and isinstance(reasons[0], dict) else "Decision Engine reasoning"
    return f"""
      <p><strong>Household decision</strong>{html.escape(str(household_decision.get("type") or household_decision.get("decision_type") or "Decision").replace("_", " ").title())}</p>
      <p><strong>Decision evidence</strong>{html.escape(str(reason))}</p>
    """


def recommendation_with_decision_dict(decision, household_decisions: Optional[list[dict]]) -> dict:
    if is_recommendation_presentation(decision):
        data = decision.as_dict()
        data["household_decision_id"] = decision.decision_id
        return data
    data = decision.as_dict()
    household_decision = matching_household_decision(decision, household_decisions)
    data["decision"] = household_decision
    data["household_decision_id"] = household_decision.get("decision_id") if household_decision else None
    return data


def recommendation_controls_html(decision, hidden: bool = False) -> str:
    stable_id = html.escape(decision.id)
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


def recommendation_centre_html(
    recommendations: list[Decision],
    lifecycle_counts: Optional[dict[str, int]] = None,
    hidden: Optional[list[Decision]] = None,
    advisors: Optional[list[Advisor]] = None,
    goals: Optional[list[GoalProgress]] = None,
    household_decisions: Optional[list[dict]] = None,
) -> str:
    ranked = sorted(recommendations, key=lambda item: (-recommendation_priority_score(item), item.title))
    hidden = sorted(hidden or [], key=lambda item: (-recommendation_priority_score(item), item.title))
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
            <article class="recommendation-card recommendation-centre-card {html.escape(decision.severity)}">
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
                <p class="primary-action"><strong>Recommended action</strong>{html.escape(recommendation_action(decision))}</p>
                <p><strong>Why?</strong>{html.escape(why)}</p>
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
        snoozed_until = decision.evidence.get("lifecycle_snoozed_until")
        state_text = f"{state}: {snoozed_until}" if snoozed_until else state
        hidden_cards.append(
            f"""
            <article class="recommendation-card recommendation-centre-card {html.escape(decision.severity)}">
              <div class="recommendation-centre-heading">
                <span class="recommendation-level">{html.escape(state_text.title())}</span>
                <div><small>Priority score</small><strong>{recommendation_priority_score(decision)}</strong></div>
              </div>
              <h3>{html.escape(decision.title)}</h3>
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
    {advisor_centre_html(advisors)}
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
        missing_text = missing or "No required evidence gaps declared."
        confidence_reduced = evidence_summary.confidence_reason if evidence_summary else advisor.confidence_reason
        cards.append(
            f"""
            <article class="recommendation-card recommendation-centre-card">
              <div class="recommendation-centre-heading">
                <span class="recommendation-level">{html.escape(advisor.confidence)} confidence</span>
                <div><small>Status</small><strong>{html.escape(advisor.status.replace("_", " "))}</strong></div>
              </div>
              <h3>{html.escape(advisor.headline)}</h3>
              <div class="recommendation-centre-meta">
                <span>{html.escape(advisor.advisor_type.replace("_", " ").title())}</span>
                <span>{html.escape(advisor.freshness)}</span>
              </div>
              <div class="decision-lines">
                <p class="primary-action"><strong>Recommended action</strong>{html.escape(advisor.recommendation)}</p>
                <p><strong>Why?</strong>{html.escape(advisor.confidence_reason)}</p>
                <p><strong>Evidence quality</strong>{html.escape(evidence_quality)}</p>
                <p><strong>Still missing</strong>{html.escape(missing_text)}</p>
                <p><strong>Confidence note</strong>{html.escape(confidence_reduced)}</p>
              </div>
            </article>
            """
        )
    return f"""
    <section class="report-section recommendation-centre-section">
      <div class="section-title">
        <div><span class="eyebrow">Advisors</span><h2>Background checks</h2></div>
        <p>{len(advisors)} active</p>
      </div>
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
    <section id="system-collector" class="report-section compact-section">
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


def timeline_event_group(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%A, %d %b")
    except ValueError:
        return "Recent events"


def timeline_time_label(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M")
    except ValueError:
        return str(timestamp or "Time unknown")


def timeline_html(timeline: DecisionTimeline) -> str:
    if not timeline.entries:
        return """
        <section class="report-section timeline-section">
          <div class="section-title"><div><span class="eyebrow">Decision timeline</span><h2>No timeline yet</h2></div></div>
          <p class="notice">PowerMonitor has not recorded any timeline events for this period.</p>
        </section>
        """
    rows = []
    current_group = None
    for entry in timeline.entries:
        group = timeline_event_group(entry.timestamp)
        if group != current_group:
            current_group = group
            rows.append(f'<div class="timeline-day">{html.escape(group)}</div>')
        impact = (
            f'<small>{html.escape(entry.financial_impact)}</small>'
            if entry.financial_impact
            else "<small>Financial impact not quantified</small>"
        )
        sources = ", ".join(entry.evidence_sources) if entry.evidence_sources else "No evidence sources"
        kind = friendly_label(entry.event_type)
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
        <div><span class="eyebrow">Decision timeline</span><h2>Household memory</h2></div>
        <p>{len(timeline.entries)} events</p>
      </div>
      <p class="scenario-note">Grouped by day so important household changes are easier to scan.</p>
      <div class="timeline-list">{''.join(rows)}</div>
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
      <h3>{html.escape(heading or item.title)}</h3>
      <p>{html.escape(item.body)}</p>
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
        f"<li>{html.escape(evidence)}</li>"
        for evidence in explanation.evidence_used
    )
    sources = (
        ", ".join(explanation.data_sources_used)
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
            <p>{html.escape(explanation.why_this_matters)}</p>
          </div>
          <div>
            <span>Financial impact</span>
            <p>{html.escape(explanation.financial_impact)}</p>
          </div>
          <div>
            <span>Confidence reason</span>
            <p>{html.escape(explanation.confidence_reason)}</p>
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
          <p class="notice">No additional Decision Engine opportunities are active for this briefing.</p>
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


def daily_briefing_html(briefing: DailyBriefing, goals: Optional[list[GoalProgress]] = None) -> str:
    priorities = briefing_priority_grid_html(briefing.top_priorities, "Today's Best Action", "No immediate action", "Advisor Engine has not found an urgent household priority today.")
    positives = briefing_priority_grid_html(briefing.positive_outcomes, "You're Doing Well", "No positive signal yet", "Advisor Engine does not have enough positive advisor output to call out a win today.")
    watch = briefing_priority_grid_html(briefing.items_to_watch, "Watch This", "Nothing else to watch", "No lower-priority advisor watch items are active today.")
    goal_progress = goals_progress_html(goals or [])
    savings = briefing.estimated_savings_opportunity
    savings_text = "Not quantified"
    if savings:
        value = savings.billing_period if savings.billing_period is not None else savings.today
        if value is not None:
            savings_text = money(value)
    return f"""
    <section class="report-section briefing-section">
      <div class="briefing-hero">
        <div>
          <span class="eyebrow">Daily briefing</span>
          <h2>{html.escape(briefing.greeting)}</h2>
          <p>{html.escape(briefing.overall_household_status)}</p>
        </div>
        <div class="briefing-readtime">
          <span>{html.escape(briefing.overall_confidence)} confidence</span>
          <strong>{briefing.read_time_minutes} min read</strong>
        </div>
      </div>
      <p class="briefing-headline">{html.escape(briefing.suggested_next_action)}</p>
      <div class="metrics">
        {metric("Overall freshness", briefing.overall_freshness, "AdvisorCollection", "primary")}
        {metric("Savings opportunity", savings_text, savings.basis if savings and savings.basis else "Measured advisor impacts", "cost")}
        {metric("Advisor count", str(len(briefing.advisor_summary)), "Active advisor summary", "ev")}
        {metric("Read time", f"{briefing.read_time_minutes} min", "Briefing Engine estimate", "solar")}
      </div>
      {goal_progress}
      {priorities}
      {positives}
      {watch}
    </section>
    """


def goals_progress_html(goals: list[GoalProgress]) -> str:
    if not goals:
        return ""
    cards = []
    for goal in goals:
        progress = f"{goal.progress_pct}%" if goal.progress_pct is not None else "Unknown"
        cards.append(
            f"""
            <article class="briefing-card {html.escape(goal_status_tone(goal))}">
              <div class="briefing-card-heading">
                <span>{html.escape(goal.goal.outcome)}</span>
                <strong>{html.escape(goal.confidence)} confidence</strong>
              </div>
              <h3>{html.escape(goal.goal.title)}</h3>
              <p>{html.escape(goal.summary)}</p>
              <div class="briefing-meta">
                <small>{html.escape(progress)}</small>
                <small>{html.escape(goal.status.label)}</small>
              </div>
            </article>
            """
        )
    return f"""
    <section class="briefing-block">
      <div class="section-title"><div><span class="eyebrow">Today's Progress</span><h2>{len(goals)} active goal{'s' if len(goals) != 1 else ''}</h2></div></div>
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
      <div class="section-title"><div><span class="eyebrow">{html.escape(title)}</span><h2>{len(items)} advisor signal{'s' if len(items) != 1 else ''}</h2></div></div>
      <div class="briefing-grid">{cards}</div>
    </section>
    """


def briefing_priority_card_html(item: BriefingPriority, eyebrow: str) -> str:
    support = ""
    if item.supporting_advisors:
        support = f"<small>Supported by {html.escape(', '.join(item.supporting_advisors))}</small>"
    return f"""
    <article class="briefing-card {html.escape(briefing_priority_severity(item.status))}">
      <div class="briefing-card-heading">
        <span>{html.escape(eyebrow)}</span>
        <strong>{html.escape(item.confidence)} confidence</strong>
      </div>
      <h3>{html.escape(item.title)}</h3>
      <p>{html.escape(item.summary)}</p>
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
        return "Not quantified"
    if impact.billing_period is not None:
        return money(impact.billing_period)
    if impact.today is not None:
        return f"{money(impact.today)} today"
    if impact.yearly is not None:
        return f"{money(impact.yearly)} yearly"
    return "Not quantified"


def energy_score_html(score: Optional[EnergyScore]) -> str:
    if not score:
        return ""
    positives = "".join(
        f"<li>{html.escape(item)}</li>" for item in score.top_positive_factors
    ) or "<li>No positive factors are available yet.</li>"
    negatives = "".join(
        f"<li>{html.escape(item)}</li>" for item in score.top_negative_factors
    ) or "<li>No major negative factors are active.</li>"
    return f"""
    <article class="energy-score-card">
      <div class="energy-score-main">
        <span>Energy Score</span>
        <strong>{score.score}</strong>
        <small>{html.escape(score.label)} confidence: {html.escape(score.confidence)}</small>
      </div>
      <div class="energy-score-detail">
        <h3>{html.escape(score.summary)}</h3>
        <p>Potential score: <strong>{score.potential_score}</strong></p>
        <div class="energy-score-factors">
          <div><span>Positive factors</span><ul>{positives}</ul></div>
          <div><span>Negative factors</span><ul>{negatives}</ul></div>
        </div>
        {decision_explanation_html(score.explanation)}
      </div>
    </article>
    """


def setup_progress_card_html(setup: Optional[SetupChecklist]) -> str:
    if not setup:
        return ""
    progress = setup.progress
    if progress.complete:
        return """
        <section class="setup-progress-card complete">
          <div><span>Setup</span><h3>Your household is fully configured.</h3></div>
        </section>
        """
    remaining = "".join(
        f"<li>{html.escape(task.title)}</li>"
        for task in progress.remaining_tasks[:3]
    )
    return f"""
    <section class="setup-progress-card">
      <div>
        <span>Setup</span>
        <h3>{progress.completion_pct}% complete</h3>
        <p>Remaining:</p>
        <ul>{remaining}</ul>
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
        <p>{html.escape(task.rationale)}</p>
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
    return f"""
    <section class="report-section setup-section">
      <div class="section-title">
        <div><span class="eyebrow">{html.escape(section.title)}</span><h2>{section.completion_pct}% ready</h2></div>
        <p>{section.completed_tasks} of {section.total_tasks} active tasks complete</p>
      </div>
      <div class="setup-task-list">{tasks}</div>
    </section>
    """


def setup_page_html(
    setup: Optional[SetupChecklist],
    settings: Optional[Settings] = None,
    promotion_summary: Optional[dict] = None,
    notification_preferences: Optional[dict] = None,
) -> str:
    if not setup:
        return '<section class="report-section"><p class="notice">Setup guidance is unavailable.</p></section>'
    sections = "".join(setup_section_html(section) for section in setup.sections)
    conflict = (promotion_summary or {}).get("conflict_summary", {})
    conflict_notice = ""
    if conflict.get("repeated_conflicts"):
        conflict_notice = """
        <section class="setup-progress-card">
          <div><span>Provider configuration</span><h3>Provider observations are conflicting.</h3><p>Review provider configuration before relying on promoted data.</p></div>
          <a href="#system">Review System</a>
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
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Guided setup</span><h2>Start with these basics</h2></div><p>Most households only need billing, tariff, and one data source to begin.</p></div>
      <div class="onboarding-steps">
        <div><span>1</span><strong>Set billing and tariff</strong><p>Use the Billing Cycle panel below for the start day, retailer, and plan.</p></div>
        <div><span>2</span><strong>Add household data</strong><p>Use Imports for Ocular EV history, or Providers for live/local data when available.</p></div>
        <div><span>3</span><strong>Review confidence</strong><p>Only visit System when Setup points to a data-quality or collector issue.</p></div>
      </div>
    </section>
    {billing_cycle_setup_html(settings)}
    <section class="report-section compact-section">
      <div class="section-title">
        <div><span class="eyebrow">Setup</span><h2>{setup.progress.completion_pct}% complete</h2></div>
        <p>{setup.progress.completed_tasks} of {setup.progress.total_tasks} active tasks complete</p>
      </div>
      <div class="setup-meter"><span style="width:{setup.progress.completion_pct}%"></span></div>
    </section>
    {sections}
    """


def billing_cycle_setup_html(settings: Optional[Settings]) -> str:
    if not settings:
        return ""
    plan_options = (
        ("synergy_ev_add_on", "Synergy EV Add On"),
        ("synergy_home_plan", "Synergy Home Plan"),
        ("previous_flat_or_home_plan", "Previous Flat/Home Plan"),
    )
    options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if settings.current_plan == value else ""}>{html.escape(label)}</option>'
        for value, label in plan_options
    )
    retailer = html.escape(settings.retailer)
    return f"""
    <section id="setup-billing" class="report-section setup-section">
      <div class="section-title">
        <div><span class="eyebrow">Setup</span><h2>Billing Cycle</h2></div>
        <p>Used by bills, projections, comparisons, and setup readiness</p>
      </div>
      <form class="setup-config-form" method="post" action="/setup/billing-cycle">
        <label><span>Billing cycle start day</span><input type="number" name="billing_period_start_day" min="1" max="28" value="{settings.billing_period_start_day}" required></label>
        <label><span>Retailer</span><input type="text" name="retailer" value="{retailer}" placeholder="Optional"></label>
        <label><span>Tariff plan</span><select name="current_plan">{options}</select></label>
        <button type="submit">Save Billing Cycle</button>
      </form>
      <p class="notice">Saving this completes the Billing Cycle setup task immediately. Current billing-period date overrides can still be kept in <code>config/settings.json</code> for exact bill matching.</p>
    </section>
    """


def home_page_html(
    briefing: DailyBriefing,
    goals: Optional[list[GoalProgress]] = None,
    setup: Optional[SetupChecklist] = None,
) -> str:
    return setup_progress_card_html(setup) + daily_briefing_html(briefing, goals or [])


def advisor_home_html(advisor: Advisor) -> str:
    tone = {
        "IMPROVING": "green",
        "ON_TRACK": "green",
        "SLIGHTLY_HIGH": "amber",
        "HIGH": "red",
        "UNKNOWN": "amber",
    }.get(advisor.status, "amber")
    impact = advisor.estimated_financial_impact
    impact_text = "Not quantified"
    if impact and impact.billing_period is not None:
        impact_text = money(impact.billing_period)
    evidence_summary = getattr(advisor, "evidence_summary", None)
    evidence_quality = evidence_summary.evidence_quality.value.title() if evidence_summary else "Unknown"
    evidence_reason = evidence_summary.reason if evidence_summary else "Evidence summary unavailable."
    return f"""
    <section class="source-status {tone}">
      <div><span>{html.escape(advisor.advisor_type.replace("_", " ").title())} Advisor</span><h2>{html.escape(advisor.headline)}</h2></div>
      <p>{html.escape(advisor.summary)}</p>
      <div class="metrics">
        {metric("Status", advisor.status.replace("_", " "), "Shared Advisor", "cost")}
        {metric("Projection impact", impact_text, impact.basis if impact and impact.basis else "Financial Engine", "cost")}
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
    benefit_text = "Not quantified"
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
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Bill attribution</span><h2>What is driving the bill?</h2></div><p>Biggest bill components first</p></div>
      <p class="scenario-note">This splits the estimated bill into the parts a household can usually act on: grid energy, fixed supply charge, solar export credit, EV charging, and peak usage.</p>
      <div class="metrics">
        {metric("Grid energy", money(summary.import_cost), kwh(summary.grid_import_kwh), "grid")}
        {metric("Supply charge", money(summary.supply_cost), f"{money(summary.supply_cost / summary.supply_days)}/day", "supply")}
        {metric("Export credit", f"-{money(summary.export_credit)}", kwh(summary.solar_exported_kwh), "credit")}
        {metric("EV charging cost", money(summary.ev_charging_cost), ev_cost_label(settings, summary), "ev")}
        {metric("Household usage cost", money(current["household_usage_cost"]), "Grid cost excluding EV charging", "cost")}
        {metric("Peak usage cost", money(summary.buckets["peak"]["cost"]), f"{summary.peak_cost_share_pct:.1f}% of import cost", "bad")}
        {metric("Plan savings/loss", signed_money(-plan["difference"]), plan["recommendation"], "good" if plan["savings"] > 0 else "bad" if plan["savings"] < 0 else "neutral")}
      </div>
    </section>
    """


def financial_highlights_html(summary: Summary, projected_bill_value: float, plan: dict, recommendations: list) -> str:
    saving_text = (
        f"{money(plan['savings'])} ahead on the current plan"
        if plan.get("savings", 0) > 0
        else f"{money(abs(plan.get('savings', 0)))} possible plan gap"
        if plan.get("savings", 0) < 0
        else "No plan gap detected"
    )
    top_action = recommendation_action(recommendations[0]) if recommendations else "No savings recommendation is active right now."
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Financial overview</span><h2>Start here</h2></div><p>Current bill, projection, and next opportunity</p></div>
      <div class="metrics">
        {metric("Current billing period", money(summary.net_cost), f"{summary.start.strftime('%d %b')} to {summary.end.strftime('%d %b %Y')}", "primary")}
        {metric("Projected bill", money(projected_bill_value), "Based on current period run-rate", "cost")}
        {metric("Savings opportunity", saving_text, plan["recommendation"], "good" if plan.get("savings", 0) > 0 else "neutral")}
        {metric("Next useful action", top_action, "From active recommendations", "solar")}
      </div>
    </section>
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
    rec_html = recommendations_html(solar_recs) if solar_recs else '<p class="notice">No solar-specific recommendation is active for this period.</p>'
    return f"""
    <section class="report-section compact-section">
      <div class="section-title"><div><span class="eyebrow">Solar</span><h2>Generation and self-consumption</h2></div></div>
      <div class="metrics">{cards}</div>
    </section>
    {rec_html}
    """


def ev_source_status_html(summary: Summary, settings: Settings) -> str:
    source = summary.ev_charging_source
    configured = settings.ev_data_source
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
            f"No actual Ocular or HA OCPP sessions were found for this period. "
            f"PowerMonitor is estimating overnight EV charging above {settings.ev_baseline_watts:.0f} W. "
            "Import Ocular history with: python3 collector/import_ocular_export.py /path/to/export.xlsx"
        )
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
        warning = f"""
        <section class="auth-alert">
          <div class="auth-alert-heading"><div><span>EV data warning</span><h2>Actual session data unavailable</h2></div></div>
          <p>{html.escape(EV_ESTIMATE_CAVEAT)}</p>
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


def data_completeness_html(summary: Summary, settings: Settings) -> str:
    completeness = data_completeness(summary, settings)
    rows = []
    for item in completeness["items"]:
        status = item["status"]
        tone = "good" if status in {"present", "configured"} else "bad"
        rows.append(
            f"""
            <div class="completeness-row {tone}">
              <span>{html.escape(item["name"])}</span>
              <strong>{html.escape(status.title())}</strong>
              <small>{html.escape(item["detail"])}</small>
            </div>
            """
        )
    return f"""
    <div id="system-data-quality" class="system-card">
      <div class="chart-heading"><h3>Data completeness</h3><span>{html.escape(completeness["confidence"])} confidence</span></div>
      <p class="scenario-note">{html.escape(completeness["confidence_note"])}</p>
      <div class="completeness-grid">{''.join(rows)}</div>
    </div>
    """


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
      <p class="notice">Provider guidance is not available in this report. Check System again after the dashboard refreshes.</p>
    </div>
    """
    rows = []
    for provider in provider_registry.providers:
        capabilities = ", ".join(capability.value.replace("_", " ") for capability in provider.capabilities)
        rows.append(
            f"""
            <div class="completeness-row {'good' if provider.available else 'bad'}">
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
        "next_scheduled_sync": "Next collector run",
        "sync_frequency": "Collector schedule",
        "required_information": ["Provider configuration details"],
        "why_required": "Provider details let PowerMonitor test access and explain which data is safe to use.",
        "connection_status": "Connected and available for analysis." if healthy else "Waiting for setup.",
        "health_status": friendly_label((data.get("health") or {}).get("collector_status") or "unknown"),
        "next_step": "No action needed right now." if healthy else "Open this provider's setup details and complete the connection.",
        "common_failures": ["Expired session or credential", "Provider service unavailable", "No recent data returned"],
        "recovery_steps": ["Check the required information", "Reconnect or refresh the provider session", "Run Test Connection"],
        "technical_details": {
            "lifecycle_state": lifecycle,
            "sync_state": data.get("sync_state"),
            "collector_status": (data.get("health") or {}).get("collector_status"),
        },
    }


def _list_items(items: list[str]) -> str:
    return "".join(f"<li>{html.escape(str(item))}</li>" for item in items)


def _technical_detail_rows(details: dict) -> str:
    return "".join(
        f"<div><span>{html.escape(friendly_label(str(key)))}</span><strong>{html.escape(str(value if value is not None else 'None'))}</strong></div>"
        for key, value in details.items()
    )


def provider_management_html(provider_management: Optional[ProviderManager]) -> str:
    if not provider_management:
        return """
    <div class="system-card">
      <div class="chart-heading"><h3>Provider connections</h3><span>No connection manager</span></div>
      <p class="notice">Connection status is unavailable. Existing readings can still be shown, but provider setup guidance may be incomplete.</p>
    </div>
    """
    rows = []
    for provider in provider_management.providers():
        data = provider.as_dict()
        onboarding = data.get("onboarding") or _provider_onboarding_fallback(data)
        capabilities = ", ".join(
            capability.replace("_", " ").title()
            for capability in data["capability_summary"].get("capabilities", [])
        ) or "No capabilities"
        state = str(onboarding.get("state") or data["lifecycle_state"])
        tone = "good" if onboarding.get("connected") and onboarding.get("healthy") else "bad" if state in {"ERROR", "WARNING", "NOT_CONFIGURED", "CONFIGURATION_REQUIRED"} else "neutral"
        last_sync = onboarding.get("last_successful_sync") or "Never synced"
        data_available = "Yes" if onboarding.get("data_available") else "Not yet"
        actions = " ".join(
            f'<span class="chip">{html.escape(friendly_label(action))}</span>'
            for action in data.get("actions", [])[:4]
        )
        setup_body = f"""
          <div class="provider-guidance-grid">
            <div><span>Required information</span><strong>{html.escape(', '.join(onboarding.get("required_information") or ["Provider details"]))}</strong></div>
            <div><span>Why it is required</span><strong>{html.escape(str(onboarding.get("why_required") or ""))}</strong></div>
            <div><span>Common failures</span><ul>{_list_items(onboarding.get("common_failures") or [])}</ul></div>
            <div><span>Recovery steps</span><ul>{_list_items(onboarding.get("recovery_steps") or [])}</ul></div>
          </div>
        """
        technical_body = f"""
          <div class="config-grid">{_technical_detail_rows(onboarding.get("technical_details") or {})}</div>
          <div class="config-grid">
            <div><span>Lifecycle</span><strong>{html.escape(str(data.get("lifecycle_state") or "unknown"))}</strong></div>
            <div><span>Sync state</span><strong>{html.escape(str(data.get("sync_state") or "unknown"))}</strong></div>
            <div><span>Observation count</span><strong>{html.escape(str((data.get("diagnostics") or {}).get("observation_count", 0)))}</strong></div>
            <div><span>Promotion count</span><strong>{html.escape(str((data.get("diagnostics") or {}).get("promotion_count", 0)))}</strong></div>
          </div>
        """
        rows.append(
            f"""
            <div id="system-provider-{html.escape(provider.provider_id)}" class="provider-status-card {tone}">
              <div class="provider-status-head">
                <span>{html.escape(provider.name)}</span>
                <strong>{html.escape(str(onboarding.get("state_label") or friendly_label(state)))}</strong>
              </div>
              <div class="provider-status-grid">
                <div><span>Connected?</span><strong>{html.escape('Yes' if onboarding.get('connected') else 'Not yet')}</strong></div>
                <div><span>Healthy?</span><strong>{html.escape(str(onboarding.get("health_status") or "Unknown"))}</strong></div>
                <div><span>Last updated</span><strong>{html.escape(str(last_sync))}</strong></div>
                <div><span>Data available?</span><strong>{html.escape(data_available)}</strong></div>
                <div><span>Sync frequency</span><strong>{html.escape(str(onboarding.get("sync_frequency") or "Unknown"))}</strong></div>
                <div><span>Next scheduled sync</span><strong>{html.escape(str(onboarding.get("next_scheduled_sync") or "Unknown"))}</strong></div>
                <div><span>Runtime state</span><strong>{html.escape(friendly_label(str(onboarding.get("runtime_state") or "Unknown")))}</strong></div>
                <div><span>Collector duration</span><strong>{html.escape(str(onboarding.get("collector_duration") or "Not recorded"))}</strong></div>
                <div><span>Consecutive failures</span><strong>{html.escape(str(onboarding.get("consecutive_failures", 0)))}</strong></div>
                <div><span>Retry countdown</span><strong>{html.escape(str(onboarding.get("retry_countdown") or "No retry scheduled"))}</strong></div>
                <div><span>Provider health</span><strong>{html.escape(str(onboarding.get("health_score", "Unknown")))}%</strong></div>
                <div><span>Connection status</span><strong>{html.escape(str(onboarding.get("connection_status") or "Unknown"))}</strong></div>
                <div><span>What should I do next?</span><strong>{html.escape(str(onboarding.get("next_step") or "Review provider setup."))}</strong></div>
              </div>
              <p>Provides: {html.escape(capabilities)}.</p>
              <small>{actions}</small>
              {disclosure_html("Setup and recovery", "What is needed, why, and how to fix common failures", setup_body, css_class="provider-details")}
              {disclosure_html("Technical diagnostics", "Lifecycle, sync, observations, and collector details", technical_body, css_class="provider-details")}
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Provider connections</h3><span>{len(rows)} providers</span></div>
      <p class="scenario-note">Select a provider, check what information is required, test the connection, then use the status and sync details to decide what to do next.</p>
      <div class="completeness-grid">{''.join(rows)}</div>
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
              <small>Schedule: {html.escape(str(provider.get("schedule") or "manual"))}. Last execution: {html.escape(str(health.get("last_run") or "Never"))}. Next: {html.escape(str(health.get("next_run_at") or "Manual"))}.</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Provider Runtime</h3><span>{html.escape(str(data.get("scheduler_status") or "idle").title())}</span></div>
      <div class="config-grid">
        <div><span>Runtime health</span><strong>{html.escape(str(data.get("runtime_health") or "unknown").title())}</strong></div>
        <div><span>Active providers</span><strong>{len(data.get("active_providers", []))}</strong></div>
      </div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No runtime adapters are registered.</p>'}</div>
    </div>
    """


def provider_observations_html(summary: Optional[dict]) -> str:
    if not summary:
        return """
    <div id="system-data-quality" class="system-card">
      <div class="chart-heading"><h3>Provider observations</h3><span>Not available yet</span></div>
      <p class="notice">Older readings may exist before provider observations were introduced. New imports and collectors will fill this in as they run.</p>
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
    <div id="system-data-quality" class="system-card">
      <div class="chart-heading"><h3>Provider observations</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">Legacy readings or EV sessions may exist before the Provider Observation pipeline. New imports and collectors will populate observations as they run.</p>'}</div>
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
      <div class="chart-heading"><h3>Latest promotions</h3><span>{int(counts.get("ACCEPTED") or 0)} accepted / {int(counts.get("REJECTED") or 0)} rejected / {int(counts.get("CONFLICT") or 0)} conflicts</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No provider observations have been promoted yet.</p>'}</div>
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
        "Collector": data.get("collector", {}).get("status"),
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
      <div class="chart-heading"><h3>Household State</h3><span>{html.escape(str(data.get("summary", {}).get("current_status") or "Current facts").title())}</span></div>
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
      <div class="chart-heading"><h3>Household Anomalies</h3><span>0 active</span></div>
      <p class="notice">No behaviour-aware household anomalies are active.</p>
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
      <div class="chart-heading"><h3>Household Anomalies</h3><span>{len(anomalies)} active</span></div>
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
      <div class="chart-heading"><h3>Decision Summary</h3><span>0 active</span></div>
      <p class="notice">No active household decisions are currently emitted.</p>
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
              <span>{html.escape(str(decision.get("type") or "Decision").replace("_", " ").title())}</span>
              <strong>{html.escape(priority.title())} / {html.escape(confidence.title())}</strong>
              <small>{html.escape(str(reason or ""))} Evidence: {html.escape(str(evidence_summary))}</small>
            </div>
            """
        )
    active_count = int((summary or {}).get("active_count") or len(decisions))
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Decision Summary</h3><span>{active_count} active</span></div>
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
        evidence_text = evidence[0].get("source") if evidence and isinstance(evidence[0], dict) else "DecisionEngine"
        advisor_text = advisor.headline if advisor else "No advisor explanation linked"
        rows.append(
            f"""
            <div class="completeness-row neutral">
              <span>{html.escape(str(decision.get("type") or "Decision").replace("_", " ").title())}</span>
              <strong>{html.escape(advisor_text)}</strong>
              <small>Evidence: {html.escape(str(evidence_text))}</small>
            </div>
            """
        )
    return f"""
    <div class="system-card">
      <div class="chart-heading"><h3>Decision Provenance</h3><span>Decision to advisor to evidence</span></div>
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
      <div class="chart-heading"><h3>State Changes</h3><span>{int(summary.get("count") or 0)} recent</span></div>
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
      <div class="chart-heading"><h3>Notification Readiness</h3><span>{int(summary.get("ready_count") or 0)} ready / {int(summary.get("count") or 0)} candidates</span></div>
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
      <div class="chart-heading"><h3>Notification Preferences</h3><span>Local only</span></div>
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
      <div class="chart-heading"><h3>Notification History</h3><span>{int(summary.get("count") or 0)} recent</span></div>
      <div class="completeness-grid">{''.join(rows) or '<p class="notice">No notification history recorded yet.</p>'}</div>
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
        {metric("Collector status", collector_status_text(health), "Current collector state", "good" if health.get("status") == "ok" else "bad")}
        {metric("Last successful collection", display_timestamp(health.get("last_successful_collection")), "Most recent successful collector run", "grid")}
        {metric("Last failure", display_timestamp(health.get("last_collection_failure")), str(health.get("collector_message") or health.get("failure_type") or "No recent failure recorded"), "bad" if health.get("status") != "ok" and health.get("last_collection_failure") else "grid")}
        {metric("Report age", f"{float(health.get('report_age_minutes') or 0):.1f} minutes", "Freshness of generated dashboard", "grid")}
        {metric("Latest reading", display_timestamp(health.get("latest_measurement_time")), "Latest SolarEdge interval", "grid")}
        {metric("Setup completion", f"{setup.progress.completion_pct}%" if setup else "Unavailable", "Continuous Setup Engine readiness", "good" if setup and setup.progress.complete else "grid")}
      </div>
    """
    advanced_body = f"""
      {provider_runtime_html(provider_runtime)}
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
      <div class="section-title"><div><span class="eyebrow">System</span><h2>Can I trust the data?</h2></div><p>Start with household status. Open deeper checks only when something looks wrong.</p></div>
      <div class="system-overview-grid">
        {household_state_html(household_state, latest_household_state_snapshot, recent_household_state_snapshots)}
        {data_completeness_html(summary, settings)}
      </div>
      {disclosure_html("Providers", "Connection status and next steps", f'''
        {provider_management_html(provider_management)}
        {provider_registry_html(provider_registry)}
      ''', open_by_default=True, css_class="system-group")}
      {disclosure_html("Collector Health", "Collection recency, failures, and report freshness", collector_body, open_by_default=True, css_class="system-group")}
      {disclosure_html("Decisions", "Active household decisions and confidence", f'''
        {household_decisions_html(household_decisions, decision_summary)}
      ''', css_class="system-group")}
      {disclosure_html("Evidence", "Provider observations and data confidence", f'''
        {provider_observations_html(provider_observations)}
        {data_completeness_html(summary, settings)}
      ''', css_class="system-group")}
      {disclosure_html("Advanced Diagnostics", "Runtime, behaviour, provenance, notifications, and raw configuration", advanced_body, css_class="system-group advanced-diagnostics")}
    </section>
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
          <div><span>Import framework</span><strong>{html.escape("Available" if imports and imports.available else "Unavailable")}</strong></div>
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
        f'<div class="system-card"><div class="chart-heading"><h3>Upload setup tasks</h3><span>Setup Engine</span></div><div class="completeness-grid">{setup_task_rows}</div></div>'
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
          <small>{html.escape(provider.note or 'Import provider metadata')} <a href="#system-provider-{html.escape(provider.provider_id)}">Open provider setup</a></small>
        </div>
        """
        for provider in import_providers
    )
    observation_rows = ""
    if provider_observations:
        observation_rows = "".join(
            f"""
            <div class="completeness-row neutral">
              <span>{html.escape(str(item.get("provider_id") or "Unknown provider"))}</span>
              <strong>{html.escape(str(item.get("observation_type") or "Observation").replace("_", " ").title())}</strong>
              <small>{html.escape(str(item.get("capability") or ""))}</small>
            </div>
            """
            for item in provider_observations.get("latest", [])[:6]
        )
    provider_html = (
        f'<div class="system-card"><div class="chart-heading"><h3>Supported import providers</h3><span>Provider Management</span></div><div class="completeness-grid">{provider_rows}</div></div>'
        if provider_rows else ""
    )
    observation_html = (
        f'<div class="system-card"><div class="chart-heading"><h3>Observation providers</h3><span>Latest provider-created observations</span></div><div class="completeness-grid">{observation_rows}</div></div>'
        if observation_rows else ""
    )
    import_guidance = """
      <div class="onboarding-steps">
        <div><span>1</span><strong>Choose provider export</strong><p>PowerMonitor currently supports Ocular EV charging history Excel exports.</p></div>
        <div><span>2</span><strong>Preview before saving</strong><p>The preview checks rows, dates, energy totals, and validation issues before anything is imported.</p></div>
        <div><span>3</span><strong>Import updates the dashboard</strong><p>Accepted sessions are matched to your household and used in bills, EV analytics, and confidence messaging.</p></div>
      </div>
    """
    troubleshooting = """
      <ul class="guidance-list">
        <li>Use the original `.xlsx` or `.xlsm` export from Ocular where possible.</li>
        <li>If preview shows validation issues, export the charging history again and retry before committing.</li>
        <li>Re-importing history is safe: existing sessions are updated rather than duplicated.</li>
        <li>The main dashboard will not auto-refresh while you are choosing or uploading a file.</li>
      </ul>
    """

    return f"""
    <section class="report-section import-panel">
      <div class="section-title"><div><span class="eyebrow">Imports</span><h2>Upload household data</h2></div><p>Choose an Ocular Excel export from your phone, tablet, or Mac.</p></div>
      {disclosure_html("First-time import guide", "What to upload and what happens next", import_guidance, open_by_default=True, css_class="onboarding-guide")}
      {capability_note}
      {provider_html}
      {observation_html}
      {setup_task_html_block}
      <form class="import-form" method="post" action="/imports/preview" enctype="multipart/form-data">
        <label><span>Ocular Excel file</span><input type="file" name="import_file" accept=".xlsx,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel.sheet.macroEnabled.12" required></label>
        <button type="submit">Preview import</button>
      </form>
      <p class="notice">The dashboard will not auto-refresh while you are choosing or uploading a file.</p>
      {disclosure_html("Troubleshooting uploads", "Formats, validation, and safe re-imports", troubleshooting, css_class="onboarding-guide")}
    </section>
    <section class="report-section import-panel compact-section">
      <div class="section-title"><div><span class="eyebrow">History</span><h2>Recent import jobs</h2></div></div>
      <table class="responsive-table">
        <thead><tr><th>Created</th><th>Source</th><th>File</th><th>Status</th><th>Message</th><th>Observations</th></tr></thead>
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

    def delta_card(title: str, value: float, formatter, lower_is_better: bool = False) -> str:
        tone = "neutral"
        if value:
            positive = value < 0 if lower_is_better else value > 0
            tone = "good" if positive else "bad"
        return metric(title, formatter(value), label, tone)

    cards = "".join(
        [
            delta_card("Net cost change", deltas["net_cost"], signed_money, True),
            delta_card("Grid import change", deltas["grid_import_kwh"], signed_kwh, True),
            delta_card("Solar generation change", deltas["solar_generated_kwh"], signed_kwh),
            delta_card("Peak cost change", deltas["peak_cost"], signed_money, True),
            delta_card("Solar self-consumption change", deltas["self_consumption_pct"], lambda v: f"{v:+.1f} pp" if v else "0.0 pp"),
            delta_card("EV charging change", deltas["ev_charging_kwh"], signed_kwh, True),
        ]
    )
    return f"""
    <section class="report-section comparison-section">
      <div class="section-title"><div><span class="eyebrow">Period comparison</span><h2>{html.escape(label)}</h2></div>
      <p>{html.escape(comparison["period_b"]["label"])}: {comparison["period_b"]["start"]} to {comparison["period_b"]["end"]}</p></div>
      <div class="metrics comparison-metrics">{cards}</div>
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
    detail_rows = f"""
      <div><span>EV charging cost</span><strong>{money(current["ev_charging_cost"])} / {money(flat["ev_charging_cost"])}</strong></div>
      <div><span>Household usage cost</span><strong>{money(current["household_usage_cost"])} / {money(flat["household_usage_cost"])}</strong></div>
      <div><span>Solar export value difference</span><strong>{signed_money(comparison["solar_export_value_difference"])}</strong></div>
    """
    return f"""
    <section class="report-section plan-comparison-section">
      <div class="section-title"><div><span class="eyebrow">Plan comparison</span><h2>Synergy EV Add On vs Flat plan</h2></div><p>Same measured usage, configured rates</p></div>
      <div class="metrics">
        {metric("Current plan", money(current["net_bill"]), current["display_name"], "primary")}
        {metric("Flat plan", money(flat["net_bill"]), flat["display_name"], "cost")}
        {metric("Difference", signed_money(comparison["difference"]), conclusion, "good" if savings > 0 else "bad" if savings < 0 else "neutral")}
      </div>
      <div class="plan-detail-grid">{detail_rows}</div>
      <p class="scenario-note">{html.escape(comparison["recommendation"])} These are tariff-model estimates, not actual bill reconciliation.</p>
      <p class="scenario-note">{html.escape(car["message"])}</p>
    </section>
    """


def scenario_analysis_html(analysis: dict) -> str:
    scenarios = analysis.get("scenarios", [])
    if not scenarios:
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
      <div class="section-title"><div><span class="eyebrow">What If?</span><h2>Scenario analysis</h2></div><p>Estimated bill compared with actual</p></div>
      <div class="scenario-grid">{''.join(cards)}</div>
      {insights_html}
    </section>
    """


def whatif_page_html(analysis: dict, plan: dict, recommendations: list) -> str:
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
    output_path: Optional[Path] = REPORT_PATH,
    timeline: Optional[DecisionTimeline] = None,
    recommendation_lifecycle_counts: Optional[dict[str, int]] = None,
    hidden: Optional[list[Decision]] = None,
    billing_summary: Optional[Summary] = None,
    glance_only: bool = False,
    overnight_baseload_watts: Optional[float] = None,
) -> str:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    selected_period = selected_period or Period(
        "current_billing_period", "Current billing period", period_summary.start, period_summary.end
    )
    plan = plan_comparison(period_summary, settings)
    scenarios = scenario_analysis(period_summary, settings)
    completeness = data_completeness(period_summary, settings)
    energy_score = build_energy_score(period_summary, recommendations, completeness, plan)
    timeline = timeline or build_decision_timeline(
        period_summary,
        recommendations,
        comparison,
        include_billing_period=selected_period.kind == "current_billing_period",
    )
    billing_summary = billing_summary or period_summary
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
    glance_projected, _glance_end, glance_basis, _glance_impact = financial_engine(household_context).project_bill(
        billing_summary,
        settings,
    )
    generated_dt = datetime.now().astimezone()
    generated_at = generated_dt.strftime("%d %b %Y, %H:%M %Z")
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
    body_class = ' class="glance-only"' if glance_only else ""
    chrome_html = "" if glance_only else f"""
  {hero_html(period_summary, generated_at, health, recommendations, selected_period.label)}
  {authentication_alert_html(health)}
  {period_controls_html(selected_period)}
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
    {home_page_html(briefing, goals, setup_checklist)}
  </section>
  <section id="setup" class="page">
    {page_heading_html("Finish household setup", "Complete the few configuration items PowerMonitor needs to explain your bill clearly.")}
    {setup_page_html(setup_checklist, settings, promotion_summary, notification_preferences.as_dict())}
  </section>
  <section id="recommendations" class="page">
    {page_heading_html("Recommendation Centre", "The most useful household actions, with plain-language reasons and confidence.")}
    {recommendation_centre_html(recommendation_presentations, recommendation_lifecycle_counts, [], advisor_explanations.active(), goals, household_decisions)}
  </section>
  <section id="bills" class="page">
    {page_heading_html("Where did my money go?", "Bill attribution, tariff exposure, export credits, and plan comparison.")}
    {financial_highlights_html(period_summary, glance_projected, plan, recommendation_presentations)}
    {summary_html(selected_period.label, period_summary, settings)}
    {bill_attribution_html(period_summary, plan, settings)}
    {comparison_html(comparison) if comparison else ""}
    {plan_comparison_html(plan, car_comparison(settings))}
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
    {page_heading_html("What should I change?", "Simple scenario cards for tariff, charging, solar, and battery choices.")}
    {whatif_page_html(scenarios, plan, recommendation_presentations)}
  </section>
  <section id="timeline" class="page">
    {page_heading_html("What has happened over time?", "A household history of decisions, outcomes, confidence, and evidence.")}
    {timeline_html(display_timeline)}
  </section>
  <section id="analytics" class="page">
    {page_heading_html("What patterns exist?", "Daily trends, historical comparison, and larger analytical detail.")}
    {trends_html(trends, settings)}
    {comparison_html(comparison) if comparison else ""}
  </section>
  <section id="imports" class="page">
    {page_heading_html("Upload household data", "Preview and import supported local household exports.")}
    {imports_page_html(capability_summary=capability_summary, setup=setup_checklist, provider_registry=provider_registry, provider_management=provider_management, provider_observations=provider_observations)}
  </section>
  <section id="system" class="page">
    {page_heading_html("Can I trust the data?", "Connected sources, completeness, confidence, collection health, and privacy-safe status.")}
    {system_page_html(health, settings, period_summary, capability_summary, setup_checklist, provider_registry, provider_management, provider_runtime, provider_observations, promotion_summary, household_current_context, household_state, household_behaviour.as_dict(), household_anomalies, household_decisions, decision_summary, advisor_explanations.active(), latest_state_snapshot, recent_state_snapshots, state_change_summary, notification_readiness_summary, notification_preferences.as_dict(), notification_history_summary)}
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
    body:has(.page:target) #home {{ display:none; }} body:has(#home:target) #home {{ display:block; }}
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
    .ev-session-grid {{ display:grid; grid-template-columns:.9fr 1.1fr; gap:14px; margin-top:14px; }}
    .ev-session-card,.ev-session-recommendations,.ev-session-history {{ padding:20px; background:var(--paper); border:1px solid var(--line); border-radius:18px; }}
    .ev-tariff-list {{ display:grid; gap:12px; }}
    .ev-tariff-row {{ display:grid; grid-template-columns:230px 1fr 72px; align-items:center; gap:12px; }}
    .ev-tariff-row div:first-child span,.ev-tariff-row div:first-child strong {{ display:block; }}
    .ev-tariff-row div:first-child span {{ color:var(--muted); font-size:10px; font-weight:750; }}
    .ev-tariff-row div:first-child strong,.ev-tariff-row > strong {{ font-size:11px; font-variant-numeric:tabular-nums; }}
    .ev-tariff-row > strong {{ text-align:right; color:#315d4c; }}
    .ev-tariff-row.peak .actual-ev-track span {{ background:linear-gradient(90deg,#ed9a8b,var(--red)); }}
    .ev-insight-metrics {{ grid-template-columns:repeat(2,1fr); }}
    .ev-insight-metrics .metric {{ min-height:112px; }}
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
    .action-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
    .action-card {{ padding:17px; background:#fff; border:1px solid var(--line); border-radius:16px; }}
    .action-card span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .action-card h3 {{ margin:11px 0 5px; font-size:16px; }} .action-card p {{ margin:0; color:var(--muted); font-size:12px; }}
    .action-card.green {{ background:#f3fbf7; border-color:#cce9dc; }} .action-card.amber {{ background:#fffaf0; border-color:#eeddb0; }} .action-card.red {{ background:#fff5f3; border-color:#f0cec8; }}
    .briefing-section {{ display:grid; gap:18px; }}
    .briefing-hero {{ display:flex; align-items:flex-start; justify-content:space-between; gap:28px; padding:26px; color:#fff; background:linear-gradient(135deg,#111816,#285140); border-radius:24px; }}
    .briefing-hero h2 {{ max-width:760px; color:#fff; font-size:34px; }}
    .briefing-hero p {{ max-width:700px; margin:8px 0 0; color:rgba(255,255,255,.74); }}
    .briefing-readtime {{ flex:0 0 150px; padding:14px; text-align:right; background:rgba(255,255,255,.1); border-radius:16px; }}
    .briefing-readtime span,.briefing-card-heading span,.briefing-meta small,.briefing-sources {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .briefing-readtime span {{ color:rgba(255,255,255,.64); }}
    .briefing-readtime strong {{ display:block; margin-top:5px; font-size:20px; letter-spacing:-.035em; }}
    .briefing-headline {{ margin:0; color:#2f453e; font-size:20px; font-weight:750; letter-spacing:-.025em; }}
    .energy-score-card {{ display:grid; grid-template-columns:180px 1fr; gap:18px; padding:20px; background:#fff; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .energy-score-main {{ display:grid; align-content:center; min-height:150px; padding:18px; color:#fff; background:linear-gradient(135deg,#111816,#2d6b52); border-radius:18px; }}
    .energy-score-main span,.energy-score-main small,.energy-score-factors span {{ font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .energy-score-main span,.energy-score-main small {{ color:rgba(255,255,255,.68); }}
    .energy-score-main strong {{ display:block; margin:8px 0; font-size:58px; line-height:1; letter-spacing:-.06em; }}
    .energy-score-detail h3 {{ margin:0; font-size:18px; letter-spacing:-.03em; }}
    .energy-score-detail p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }}
    .energy-score-factors {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:13px; }}
    .energy-score-factors div {{ padding:12px; background:#f8fbf9; border:1px solid var(--line); border-radius:13px; }}
    .energy-score-factors span {{ color:var(--ink); }}
    .energy-score-factors ul {{ margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:12px; line-height:1.4; }}
    .briefing-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
    .briefing-grid.two-up {{ grid-template-columns:1fr 1fr; }}
    .briefing-card {{ padding:20px; background:#fff; border-radius:22px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.04); }}
    .briefing-card-heading,.briefing-meta {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .briefing-card-heading strong {{ padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .briefing-card h3 {{ margin:13px 0 7px; font-size:20px; letter-spacing:-.035em; }}
    .briefing-card p {{ margin:0; color:var(--muted); font-size:13px; line-height:1.5; }}
    .briefing-meta {{ margin-top:16px; padding-top:12px; border-top:1px solid var(--line); }}
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
    .scenario-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
    .scenario-card {{ min-height:188px; padding:18px; background:#fff; border:1px solid var(--line); border-radius:17px; }}
    .scenario-card span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .scenario-card h3 {{ margin:12px 0 0; font-size:16px; line-height:1.25; }} .scenario-card strong {{ display:block; margin-top:18px; font-size:26px; letter-spacing:-.045em; }}
    .scenario-card p {{ margin:7px 0 0; color:var(--muted); font-size:11px; line-height:1.4; }}
    .scenario-card.good {{ border-color:#cce9dc; background:#f4fbf7; }} .scenario-card.good strong {{ color:#226b50; }}
    .scenario-card.bad {{ border-color:#f0cec8; background:#fff5f3; }} .scenario-card.bad strong {{ color:#a74334; }}
    .scenario-insights {{ margin-top:14px; padding:18px; background:#fffaf0; border:1px solid #eeddb0; border-radius:17px; }}
    .scenario-insights ul {{ margin:0; padding-left:18px; color:#74571a; font-size:12px; }} .scenario-insights li + li {{ margin-top:6px; }}
    .timeline-list {{ display:grid; gap:12px; }}
    .timeline-day {{ margin:8px 0 0; color:var(--muted); font-size:11px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .timeline-entry {{ display:grid; grid-template-columns:120px 1fr; gap:16px; padding:18px; background:#fff; border:1px solid var(--line); border-radius:18px; }}
    .timeline-stamp {{ display:flex; align-items:center; gap:8px; }}
    .timeline-stamp span {{ display:inline-grid; place-items:center; min-width:28px; height:28px; padding:0; color:#fff; background:var(--deep-2); border-radius:50%; font-size:10px; font-weight:900; letter-spacing:0; }}
    .timeline-entry time {{ color:var(--muted); font-size:11px; font-weight:850; letter-spacing:.06em; text-transform:uppercase; }}
    .timeline-entry > div:last-child > span {{ display:inline-block; padding:3px 7px; color:#315f74; background:var(--blue-soft); border-radius:99px; font-size:8px; font-weight:850; letter-spacing:.1em; text-transform:uppercase; }}
    .timeline-entry h3 {{ margin:9px 0 4px; font-size:18px; letter-spacing:-.025em; }}
    .timeline-entry p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .timeline-meta {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
    .timeline-meta small {{ padding:6px 8px; color:var(--muted); background:#f8fbf9; border:1px solid var(--line); border-radius:9px; font-size:10px; }}
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
    .provider-status-card {{ margin-top:10px; padding:14px; background:#f8fbf9; border:1px solid var(--line); border-radius:14px; }}
    .provider-status-card.good {{ background:#f3fbf7; border-color:#cce9dc; }} .provider-status-card.bad {{ background:#fff5f3; border-color:#f0cec8; }} .provider-status-card.neutral {{ background:#fffaf0; border-color:#eeddb0; }}
    .provider-status-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .provider-status-head span {{ color:var(--muted); font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-status-head strong {{ font-size:15px; }}
    .provider-status-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:10px; }}
    .provider-status-grid div {{ padding:10px; background:rgba(255,255,255,.72); border-radius:10px; }}
    .provider-status-grid span,.provider-status-grid strong {{ display:block; }}
    .provider-status-grid span {{ color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-status-grid strong {{ margin-top:3px; font-size:12px; line-height:1.3; }}
    .provider-status-card p {{ margin:9px 0 0; color:var(--muted); font-size:12px; }}
    .provider-details {{ margin-top:10px; background:rgba(255,255,255,.68); }}
    .provider-guidance-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    .provider-guidance-grid div {{ padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px; }}
    .provider-guidance-grid span {{ display:block; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .provider-guidance-grid strong {{ display:block; margin-top:4px; font-size:12px; line-height:1.35; }}
    .provider-guidance-grid ul {{ margin:6px 0 0; padding-left:17px; color:var(--muted); font-size:12px; line-height:1.4; }}
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
    .decision-lines {{ display:grid; gap:12px; margin-top:14px; }}
    .decision-lines p,.action-card p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; }}
    .decision-lines strong,.action-card p strong {{ display:block; margin-bottom:2px; color:var(--ink); font-size:9px; font-weight:850; letter-spacing:.09em; text-transform:uppercase; }}
    .decision-lines .primary-action {{ padding:11px 12px; color:var(--ink); background:rgba(255,255,255,.72); border:1px solid rgba(0,0,0,.06); border-radius:12px; font-size:13px; }}
    .glance-page {{ min-height:calc(100vh - 180px); }}
    .glance-display {{ min-height:calc(100vh - 220px); display:grid; align-content:space-between; gap:clamp(18px,3vh,34px); padding:clamp(26px,5vw,62px); color:#f8fffc; background:#101418; border-radius:32px; box-shadow:0 28px 80px rgba(16,20,24,.22); }}
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
    .top-nav a[href="#home"] {{ color:#fff; background:var(--deep); }}
    body:has(.page:target) .top-nav a[href="#home"] {{ color:#5f6b67; background:transparent; }}
    body:has(#home:target) .top-nav a[href="#home"],
    body:has(#setup:target) .top-nav a[href="#setup"],
    body:has(#glance:target) .top-nav a[href="#glance"],
    body:has(#recommendations:target) .top-nav a[href="#recommendations"],
    body:has(#bills:target) .top-nav a[href="#bills"],
    body:has(#solar:target) .top-nav a[href="#solar"],
    body:has(#ev:target) .top-nav a[href="#ev"],
    body:has(#whatif:target) .top-nav a[href="#whatif"],
    body:has(#timeline:target) .top-nav a[href="#timeline"],
    body:has(#analytics:target) .top-nav a[href="#analytics"],
    body:has(#imports:target) .top-nav a[href="#imports"],
    body:has(#system:target) .top-nav a[href="#system"] {{ color:#fff; background:var(--deep); border-color:var(--deep); }}
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
    .setup-config-form {{ display:grid; grid-template-columns:1fr 1fr 1fr auto; gap:12px; align-items:end; }}
    .setup-config-form label span {{ display:block; margin-bottom:5px; color:var(--muted); font-size:9px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
    .setup-config-form input,.setup-config-form select,.setup-config-form button {{ width:100%; min-height:42px; padding:9px 11px; color:var(--ink); background:#f8fbf9; border:1px solid var(--line); border-radius:10px; font:inherit; }}
    .setup-config-form button {{ color:#fff; background:var(--deep-2); border-color:var(--deep-2); font-weight:750; cursor:pointer; }}
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
    @media (max-width:900px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }} .charts,.trend-grid,.recommendation-grid,.scenario-grid,.plan-detail-grid,.action-grid,.briefing-grid,.briefing-grid.two-up,.energy-score-card,.energy-score-factors,.decision-explanation-grid,.decision-grid,.system-grid,.system-overview-grid,.config-grid,.import-form,.setup-task,.setup-config-form,.onboarding-steps,.provider-status-grid,.provider-guidance-grid {{ grid-template-columns:1fr; }} .trend-card:last-child {{ grid-column:auto; }} .period-controls form {{ grid-template-columns:1fr 1fr; }} .top-nav {{ justify-content:flex-start; overflow-x:auto; flex-wrap:nowrap; -webkit-overflow-scrolling:touch; }} }}
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
      .disclosure-panel summary {{ align-items:flex-start; flex-direction:column; }} .disclosure-panel summary small {{ text-align:left; }} .disclosure-panel summary::after {{ position:absolute; right:17px; }}
      .actual-ev-head {{ flex-direction:column; }} .actual-ev-metrics {{ grid-template-columns:1fr 1fr; }} .actual-ev-breakdown-row {{ grid-template-columns:1fr; gap:6px; }}
      .ev-session-grid,.ev-insight-metrics {{ grid-template-columns:1fr; }} .ev-tariff-row {{ grid-template-columns:1fr; gap:6px; }}
      .timeline-entry {{ grid-template-columns:1fr; gap:8px; padding:15px; }} .timeline-stamp {{ justify-content:flex-start; }} .timeline-meta small {{ width:100%; }}
      .hero-alert {{ align-items:flex-start; flex-direction:column; }}
      .auth-alert-heading {{ align-items:flex-start; flex-direction:column; }}
      .ev-caveat {{ align-items:flex-start; flex-direction:column; gap:8px; }}
      .briefing-hero {{ flex-direction:column; padding:20px; }} .briefing-readtime {{ flex:0 0 auto; width:100%; text-align:left; }}
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
</main></body></html>
"""
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
    return document


def build_live_html(
    period_kind: str = "current_billing_period",
    start: Optional[str] = None,
    end: Optional[str] = None,
    glance_only: bool = False,
) -> str:
    settings = load_settings()
    current_day = local_today()
    selected = resolve_period(period_kind, settings, current_day, start, end)
    billing = resolve_period("current_billing_period", settings, current_day)
    compared = (
        resolve_period("previous_billing_period", settings, current_day)
        if selected.kind == "current_billing_period"
        else previous_equivalent(selected)
    )
    with sqlite3.connect(DB_PATH) as conn:
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
        health = health_data(conn, current_day, settings)
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
    return write_html(
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
    )


def main() -> None:
    setup_logging()
    settings = load_settings()
    current_day = local_today()
    period_start, period_end = billing_period(current_day, settings)
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    with sqlite3.connect(DB_PATH) as conn:
        validate_database(conn)
        today_summary = load_summary(conn, current_day, current_day, settings)
        period_summary = load_summary(conn, period_start, period_end, settings)
        trends = load_daily_trends(conn, period_start, period_end, settings)
        health = health_data(conn, current_day, settings)
        all_recommendations = load_recommendations(conn, period_summary, settings)
        recommendations = active_recommendations(all_recommendations)
        hidden = hidden_recommendations(all_recommendations)
        recommendation_lifecycle_counts = data_engine(conn).get_recommendation_lifecycle_counts()
        previous = resolve_period("previous_billing_period", settings, current_day)
        previous_summary = load_summary(conn, previous.start, previous.end, settings)
        comparison = compare_summaries(
            Period("current_billing_period", "Current billing period", period_start, period_end),
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
    write_html(
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
    )
    logging.info(
        "Generated report for %s and billing period %s to %s at %s",
        current_day,
        period_start,
        period_end,
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
