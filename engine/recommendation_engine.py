#!/usr/bin/env python3
"""Deprecated recommendation compatibility for older PowerMonitor outputs.

PB-026 retirement note:
    User-facing action guidance must flow through DecisionEngine ->
    HouseholdDecision -> DecisionPipeline -> RecommendationPresentation.
    This module remains only for compatibility tests and legacy import paths.
    Do not call it from report/API/presentation code for user actions.
"""

from datetime import date, datetime, timedelta
from typing import Any, Optional

from .financial_engine import FinancialEngine
from .billing_period_engine import resolve_billing_cycle
from .models import (
    Decision,
    DecisionExplanation,
    FinancialImpact,
    HouseholdContext,
    Recommendation,
    RecommendationDismissal,
    RecommendationImpact,
)

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
BUCKET_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}

CATEGORY_TO_DECISION = {
    "billing": "Bills",
    "solar": "Solar",
    "EV": "EV",
    "tariff": "Tariffs",
    "baseload": "Behaviour",
    "data_quality": "General",
}
ACTION_TYPE_BY_ID = {
    "high_cost_alert": "review",
    "peak_cost_reduction": "load_shift",
    "solar_self_consumption": "load_shift",
    "ev_charging_optimisation": "tariff_window",
    "baseload_monitoring": "investigate",
    "positive_reinforcement": "maintain",
    "ev_session_low_cost_window": "maintain",
    "ev_session_peak_shift": "load_shift",
    "data_quality_actual_ev_sessions": "data_quality",
    "data_quality_tariff_setup": "data_quality",
    "trend_repeated_peak_usage": "trend_review",
    "trend_bill_trajectory_improving": "trend_review",
    "trend_bill_trajectory_worsening": "trend_review",
    "trend_recurring_ev_charging_win": "maintain",
    "trend_recurring_solar_utilisation_win": "maintain",
    "trend_repeated_estimated_data_warning": "data_quality",
}
PRIORITY_LABELS = (
    (67, "High"),
    (34, "Medium"),
    (0, "Low"),
)


class RecommendationEngine:
    """Deprecated deterministic recommendation generator.

    Compatibility only. New product surfaces must use DecisionPipeline.
    """

    name = "recommendation_engine"

    def __init__(
        self,
        financial_engine: Optional[FinancialEngine] = None,
        household_context: Optional[HouseholdContext] = None,
    ) -> None:
        self.household_context = household_context
        self.financial_engine = financial_engine or FinancialEngine()

    def generate(
        self,
        observations: dict[str, Any],
        thresholds: dict[str, Any],
        include_inactive: bool = False,
        household_context: Optional[HouseholdContext] = None,
    ) -> list[Recommendation]:
        active_household = household_context or self.household_context
        if active_household:
            observations.setdefault("household_id", active_household.household.household_id)
        recommendations = [
            item
            for item in (
                self._high_cost_alert(observations, thresholds),
                self._peak_cost_reduction(observations, thresholds),
                self._solar_self_consumption(observations, thresholds),
                self._ev_charging_optimisation(observations, thresholds),
                self._baseload_monitoring(observations, thresholds),
                self._positive_reinforcement(observations, thresholds),
                self._data_quality_actual_ev_sessions(observations, thresholds),
                self._data_quality_tariff_setup(observations, thresholds),
            )
            if item is not None
        ]
        recommendations.extend(self._trend_recommendations(observations, thresholds))
        return self.apply_lifecycle(
            self.rank(recommendations),
            observations.get("recommendation_lifecycle", {}),
            include_inactive=include_inactive,
        )

    def apply_lifecycle(
        self,
        recommendations: list[Recommendation],
        lifecycle: dict[str, dict[str, Any]],
        include_inactive: bool = False,
    ) -> list[Recommendation]:
        now = datetime.now().astimezone()
        active = []
        for recommendation in recommendations:
            state = lifecycle.get(recommendation.stable_id, {})
            lifecycle_state = state.get("state") or "active"
            snoozed_until = state.get("snoozed_until")
            is_snoozed = lifecycle_state == "snoozed_until" and _is_future(snoozed_until, now)
            is_dismissed = lifecycle_state == "dismissed"
            recommendation.dismissible_state = RecommendationDismissal(
                state=lifecycle_state if is_dismissed or is_snoozed else "active",
                dismissed=is_dismissed,
                dismissed_until=snoozed_until if is_snoozed else None,
                reason=state.get("reason"),
            )
            if include_inactive or not (is_dismissed or is_snoozed):
                active.append(recommendation)
        return active

    def rank(self, recommendations: list[Recommendation]) -> list[Recommendation]:
        for recommendation in recommendations:
            recommendation.priority_score = self.score(recommendation)
        return sorted(
            recommendations,
            key=lambda item: (-item.priority_score, item.stable_id),
        )

    def score(self, recommendation: Recommendation) -> int:
        evidence = recommendation.supporting_evidence
        impact = recommendation.estimated_financial_impact
        period_impact = abs(float(impact.period or 0.0))
        monthly_impact = abs(float(impact.monthly or 0.0))
        impact_score = min(35, int(max(period_impact, monthly_impact) * 1.5))

        confidence_score = {"High": 22, "Medium": 14, "Low": 6, "Very High": 26}[recommendation.confidence]
        difficulty_score = {"Easy": 18, "Medium": 10, "Hard": 4}[recommendation.difficulty]
        category_score = {
            "billing": 16,
            "tariff": 14,
            "EV": 12,
            "solar": 10,
            "baseload": 10,
            "data_quality": 13,
        }[recommendation.category]
        evidence_score = min(12, len([value for value in evidence.values() if value not in (None, "", [])]) * 2)

        if recommendation.recommended_action.lower().startswith("keep "):
            category_score = max(4, category_score - 8)
            impact_score = min(impact_score, 8)

        return max(0, min(100, impact_score + confidence_score + difficulty_score + category_score + evidence_score))

    def _high_cost_alert(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        projected = float(context["projected_net_cost"])
        threshold = float(thresholds["high_cost_threshold"])
        if projected <= threshold:
            return None
        summary = context["current"]
        reading_days = int(summary["reading_days"])
        confidence = "Low" if reading_days < 10 else "Medium" if reading_days <= 20 else "High"
        actual_cost = float(summary["net_cost"])
        explanation = (
            f"Actual net cost so far is ${actual_cost:.2f}; the projected cost by "
            f"{context['projected_period_end']} is ${projected:.2f}. "
            f"This is a {confidence.lower()}-confidence projection."
        )
        return self._recommendation(
            stable_id="high_cost_alert",
            category="billing",
            title="Projected billing cost is high",
            explanation=explanation,
            period_impact=projected,
            monthly_impact=self._monthly(projected, context["projected_period_days"]),
            confidence=confidence,
            difficulty="Medium",
            evidence={
                "actual_cost_so_far": actual_cost,
                "projected_net_cost": projected,
                "projected_period_end": context["projected_period_end"],
                "projection_reading_days": reading_days,
                "configured_threshold": threshold,
                "projected_period_days": context["projected_period_days"],
            },
            action=(
                f"Projection is based on {reading_days} days of data. "
                "Prioritise Peak-period load shifting and review high-import days before the next meter read."
            ),
            basis="projected current bill",
        )

    def _peak_cost_reduction(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        summary = context["current"]
        trigger = float(thresholds["peak_cost_share_pct"])
        if float(summary["peak_cost_share_pct"]) <= trigger:
            return None
        shift_kwh = float(thresholds["peak_shift_kwh_per_day"])
        shiftable_kwh = min(float(summary["buckets"]["peak"]["kwh"]), shift_kwh * float(summary["reading_days"]))
        savings = self.financial_engine.calculate_load_shift_savings(
            shiftable_kwh,
            context["import_rates"]["peak"],
            context["import_rates"]["super_off_peak"],
        )
        explanation = (
            f"Peak usage accounts for {summary['peak_cost_share_pct']:.1f}% of import cost "
            f"and currently costs ${summary['buckets']['peak']['cost']:.2f}."
        )
        return self._recommendation(
            stable_id="peak_cost_reduction",
            category="tariff",
            title="Reduce Peak-period imports",
            explanation=explanation,
            period_impact=savings,
            monthly_impact=self._monthly(savings, summary["supply_days"]),
            confidence="Medium",
            difficulty="Medium",
            evidence={
                "peak_cost": summary["buckets"]["peak"]["cost"],
                "peak_cost_share_pct": summary["peak_cost_share_pct"],
                "shift_kwh_per_day": shift_kwh,
                "estimated_period_savings": savings,
            },
            action=f"Moving up to {shift_kwh:.1f} kWh/day from Peak to 9am-3pm could save about ${savings:.2f} this period.",
            basis="measured peak imports and configured tariff rates",
        )

    def _solar_self_consumption(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        summary = context["current"]
        if float(summary["self_consumption_pct"]) >= float(thresholds["solar_self_consumption_pct"]):
            return None
        explanation = (
            f"Solar self-consumption is {summary['self_consumption_pct']:.1f}% and "
            f"{summary['solar_export_pct']:.1f}% of generation is exported."
        )
        return self._recommendation(
            stable_id="solar_self_consumption",
            category="solar",
            title="Use more solar at home",
            explanation=explanation,
            period_impact=None,
            monthly_impact=None,
            confidence="Medium",
            difficulty="Medium",
            evidence={
                "self_consumption_pct": summary["self_consumption_pct"],
                "solar_export_pct": summary["solar_export_pct"],
            },
            action="Shift flexible loads into the 9am-3pm solar window where practical.",
            basis="measured solar generation, export, and self-consumption",
        )

    def _ev_charging_optimisation(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        summary = context["current"]
        if float(summary["ev_charging_kwh"]) <= float(thresholds["ev_detected_min_kwh"]):
            return None
        actual = context.get("ev_data_source") in {"home_assistant_ocpp", "ocular_export"}
        energy_label = "Actual EV charging" if actual else "Estimated overnight EV charging"
        peak_cost = self.financial_engine.cost_of_ev_session(summary["ev_charging_kwh"], context["import_rates"]["peak"])
        observed_cost = float(summary["ev_charging_cost"])
        savings_vs_peak = max(peak_cost - observed_cost, 0.0)
        explanation = (
            f"{energy_label} is {summary['ev_charging_kwh']:.2f} kWh, costing about "
            f"${observed_cost:.2f}."
        )
        action = (
            f"The same charging at the Peak rate would cost about ${peak_cost:.2f}."
            if actual
            else f"The same estimated charging at the Peak rate would cost about ${peak_cost:.2f}. "
            "Daytime EV charging is not included without charger session data."
        )
        return self._recommendation(
            stable_id="ev_charging_optimisation",
            category="EV",
            title=energy_label,
            explanation=explanation,
            period_impact=savings_vs_peak,
            monthly_impact=self._monthly(savings_vs_peak, summary["supply_days"]),
            confidence="Medium" if actual else "Low",
            difficulty="Easy",
            evidence={
                "estimated_ev_kwh": summary["ev_charging_kwh"],
                "observed_or_estimated_cost": observed_cost,
                "estimated_peak_cost": peak_cost,
                "estimated_savings_vs_peak": savings_vs_peak,
                "actual_session_data_available": actual,
            },
            action=action,
            basis="measured or estimated EV charging and configured tariff rates",
        )

    def _baseload_monitoring(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        current_baseload = float(context["overnight_baseload_watts"])
        expected = float(context["ev_baseline_watts"])
        trigger = expected * (1 + float(thresholds["baseload_excess_pct"]) / 100)
        if current_baseload <= trigger:
            return None
        explanation = (
            f"Typical overnight baseload is about {current_baseload:.0f} W versus the "
            f"configured {expected:.0f} W baseline."
        )
        return self._recommendation(
            stable_id="baseload_monitoring",
            category="baseload",
            title="Review overnight baseload",
            explanation=explanation,
            period_impact=None,
            monthly_impact=None,
            confidence="Medium",
            difficulty="Medium",
            evidence={
                "current_baseload_watts": current_baseload,
                "expected_baseline_watts": expected,
                "excess_pct": (current_baseload / expected - 1) * 100 if expected else 0,
            },
            action="Review measured overnight load patterns and reduce avoidable always-on consumption where practical.",
            basis="overnight interval readings and configured baseload threshold",
        )

    def _positive_reinforcement(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        summary = context["current"]
        wins = []
        if float(summary["peak_cost_share_pct"]) < float(thresholds["positive_peak_share_pct"]):
            wins.append(f"Peak share is only {summary['peak_cost_share_pct']:.1f}%")
        if float(summary["self_consumption_pct"]) > float(thresholds["positive_self_consumption_pct"]):
            wins.append(f"solar self-consumption is {summary['self_consumption_pct']:.1f}%")
        previous = context.get("previous")
        if previous and summary["average_daily_net_cost"] < previous["average_daily_net_cost"]:
            wins.append("average daily net cost is lower than the previous billing period")
        if not wins:
            return None
        explanation = "; ".join(wins).capitalize() + "."
        return self._recommendation(
            stable_id="positive_reinforcement",
            category="billing",
            title="You're doing well",
            explanation=explanation,
            period_impact=None,
            monthly_impact=None,
            confidence="High",
            difficulty="Easy",
            evidence={"positive_signals": len(wins)},
            action="Keep the current habits going and continue using the low-cost and solar windows.",
            basis="measured tariff, solar, and period comparison signals",
        )

    def _data_quality_actual_ev_sessions(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        summary = context["current"]
        if context.get("ev_data_source") in {"home_assistant_ocpp", "ocular_export"}:
            return None
        if float(summary.get("ev_charging_kwh") or 0.0) <= float(thresholds["ev_detected_min_kwh"]):
            return None
        explanation = "EV charging is currently based on the overnight fallback estimate rather than actual charger sessions."
        return self._recommendation(
            stable_id="data_quality_actual_ev_sessions",
            category="data_quality",
            title="Connect actual EV session data",
            explanation=explanation,
            period_impact=None,
            monthly_impact=None,
            confidence="High",
            difficulty="Medium",
            evidence={
                "ev_charging_source": context.get("ev_data_source"),
                "estimated_ev_kwh": summary.get("ev_charging_kwh"),
            },
            action="Import or refresh actual EV session data so future EV recommendations use measured charging sessions.",
            basis="data source coverage",
        )

    def _data_quality_tariff_setup(self, context: dict[str, Any], thresholds: dict[str, Any]) -> Optional[Recommendation]:
        capability_summary = context.get("capability_summary") or {}
        tariff = _capability(capability_summary, "tariff")
        if tariff and tariff.get("usable"):
            return None
        if not tariff and context.get("import_rates"):
            return None
        return self._recommendation(
            stable_id="data_quality_tariff_setup",
            category="data_quality",
            title="Complete tariff setup",
            explanation="Tariff rates are not configured, so cost and charging recommendations may be unavailable.",
            period_impact=None,
            monthly_impact=None,
            confidence="High",
            difficulty="Easy",
            evidence={
                "capability": "tariff",
                "reason": tariff.get("reason") if tariff else "Tariff capability was not detected.",
            },
            action="Add the household tariff rates before relying on cost, plan, or charging recommendations.",
            basis="household capability detection",
        )

    def _trend_recommendations(self, context: dict[str, Any], thresholds: dict[str, Any]) -> list[Recommendation]:
        entries = list(getattr(context.get("timeline_history"), "entries", []) or [])
        if not entries:
            return []
        current_end = _as_date(context["current"]["end"])
        return [
            item
            for item in (
                self._repeated_peak_trend(context, thresholds, entries, current_end),
                self._bill_trajectory_trend(context, thresholds, entries, current_end),
                self._recurring_ev_win(context, thresholds, entries, current_end),
                self._recurring_solar_win(context, thresholds, entries, current_end),
                self._repeated_estimated_data(context, thresholds, entries, current_end),
            )
            if item is not None
        ]

    def _repeated_peak_trend(self, context, thresholds, entries, current_end):
        stats = _trend_stats(context, entries, current_end, lambda entry: entry.event_type == "high_peak_usage", "last_14_days")
        if not _trend_is_active(stats, thresholds):
            return None
        return self._trend_recommendation(
            "trend_repeated_peak_usage",
            "tariff",
            "Peak usage is becoming a pattern",
            f"High peak usage has appeared {stats['recent_count']} times in the {stats['window']} and {stats['historical_count']} times historically.",
            "Medium",
            "Medium",
            _trend_evidence(stats, "high_peak_usage_events"),
            "Review flexible loads that repeatedly land in Peak periods and prioritise shifting them to lower-cost windows.",
        )

    def _bill_trajectory_trend(self, context, thresholds, entries, current_end):
        improved = _trend_stats(context, entries, current_end, lambda entry: entry.title == "Bill tracking improved", "current_billing_period")
        worsened = _trend_stats(context, entries, current_end, lambda entry: entry.title == "Bill tracking worsened", "current_billing_period")
        if improved["historical_count"] == worsened["historical_count"]:
            return None
        direction = "improving" if improved["historical_count"] > worsened["historical_count"] else "worsening"
        stats = improved if direction == "improving" else worsened
        if not _trend_is_active(stats, thresholds):
            return None
        return self._trend_recommendation(
            f"trend_bill_trajectory_{direction}",
            "billing",
            f"Bill trajectory is {direction}",
            f"Bill trajectory is {direction}: {stats['recent_count']} {direction} events are in the {stats['window']}, with {improved['historical_count']} improved and {worsened['historical_count']} worsened events recorded historically.",
            "Medium",
            "Easy",
            {**_trend_evidence(stats, f"bill_tracking_{direction}_events"), "bill_tracking_improved_events": improved["historical_count"], "bill_tracking_worsened_events": worsened["historical_count"]},
            "Keep reinforcing the behaviours behind the improvement." if direction == "improving" else "Review recent high-cost days and recurring recommendations before this trend continues.",
        )

    def _recurring_ev_win(self, context, thresholds, entries, current_end):
        stats = _trend_stats(context, entries, current_end, lambda entry: entry.event_type == "ev_cheapest_tariff", "last_14_days")
        if not _trend_is_active(stats, thresholds):
            return None
        return self._trend_recommendation(
            "trend_recurring_ev_charging_win",
            "EV",
            "EV charging win is recurring",
            f"EV charging has landed in the cheapest tariff window {stats['recent_count']} times in the {stats['window']} and {stats['historical_count']} times historically.",
            "High",
            "Easy",
            _trend_evidence(stats, "ev_cheapest_tariff_events"),
            "Keep the EV schedule aligned with the cheapest tariff window and watch for any Peak-session regressions.",
        )

    def _recurring_solar_win(self, context, thresholds, entries, current_end):
        stats = _trend_stats(context, entries, current_end, lambda entry: entry.event_type == "positive_outcome", "last_14_days")
        if not _trend_is_active(stats, thresholds):
            return None
        return self._trend_recommendation(
            "trend_recurring_solar_utilisation_win",
            "solar",
            "Solar utilisation win is recurring",
            f"Positive solar/utilisation outcomes have appeared {stats['recent_count']} times in the {stats['window']} and {stats['historical_count']} times historically.",
            "High",
            "Easy",
            _trend_evidence(stats, "positive_outcome_events"),
            "Keep flexible loads in solar and low-cost windows so the recurring positive pattern continues.",
        )

    def _repeated_estimated_data(self, context, thresholds, entries, current_end):
        stats = _trend_stats(context, entries, current_end, lambda entry: "Estimated EV charging" in entry.evidence_sources, "last_7_days")
        if not _trend_is_active(stats, thresholds):
            return None
        return self._trend_recommendation(
            "trend_repeated_estimated_data_warning",
            "data_quality",
            "Estimated data is recurring",
            f"Estimated/fallback EV data has been used in {stats['recent_count']} timeline events in the {stats['window']} and {stats['historical_count']} times historically.",
            "Medium",
            "Medium",
            _trend_evidence(stats, "estimated_ev_timeline_events"),
            "Connect or refresh actual EV session data so future decisions rely less on fallback estimates.",
        )

    def _trend_recommendation(self, stable_id, category, title, explanation, confidence, difficulty, evidence, action):
        return self._recommendation(
            stable_id=stable_id,
            category=category,
            title=title,
            explanation=explanation,
            period_impact=None,
            monthly_impact=None,
            confidence=confidence,
            difficulty=difficulty,
            evidence={
                **evidence,
                "source_type": "timeline_history",
                "confidence_reason": f"{confidence} confidence because the pattern is present in recent timeline history.",
            },
            action=action,
            basis="PowerMonitor timeline history",
        )

    def _recommendation(
        self,
        stable_id: str,
        category: str,
        title: str,
        explanation: str,
        period_impact: Optional[float],
        monthly_impact: Optional[float],
        confidence: str,
        difficulty: str,
        evidence: dict[str, Any],
        action: str,
        basis: str,
    ) -> Recommendation:
        return Recommendation(
            stable_id=stable_id,
            category=category,
            title=title,
            explanation=explanation,
            estimated_financial_impact=RecommendationImpact(
                period=period_impact,
                monthly=monthly_impact,
                basis=basis,
            ),
            confidence=confidence,
            difficulty=difficulty,
            priority_score=0,
            supporting_evidence=evidence,
            recommended_action=action,
            dismissible_state=RecommendationDismissal(),
        )

    def _monthly(self, value: Optional[float], days: int) -> Optional[float]:
        if value is None:
            return None
        return self.financial_engine.projected_monthly_cost(value, int(days or 0))


def generate_recommendations(
    context: dict[str, Any],
    thresholds: dict[str, Any],
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Recommendation]:
    """Deprecated compatibility wrapper. Use DecisionPipeline for user actions."""
    return RecommendationEngine(household_context=household_context).generate(
        context,
        thresholds,
        include_inactive=include_inactive,
    )


def generate_recommendations_from_data_engine(
    current_summary: dict[str, Any],
    settings,
    data_engine,
    thresholds: dict[str, Any],
    previous_summary: Optional[dict[str, Any]] = None,
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Recommendation]:
    """Deprecated compatibility wrapper. Use DataEngine.get_household_decisions."""
    context = build_recommendation_context(
        current_summary,
        settings,
        data_engine,
        previous_summary=previous_summary,
        household_context=household_context,
    )
    return generate_recommendations(
        context,
        thresholds,
        include_inactive=include_inactive,
        household_context=household_context,
    )


def build_recommendation_context(
    current_summary: dict[str, Any],
    settings,
    data_engine,
    previous_summary: Optional[dict[str, Any]] = None,
    household_context: Optional[HouseholdContext] = None,
) -> dict[str, Any]:
    financial_engine = FinancialEngine()
    projected_days = current_summary["supply_days"]
    projected_end = _as_date(current_summary["end"])
    start = _as_date(current_summary["start"])
    end = _as_date(current_summary["end"])
    overnight_baseload = data_engine.get_overnight_baseload_watts(start, end)
    timeline_history = data_engine.get_timeline_entries(limit=100)
    recommendation_lifecycle = (
        data_engine.get_recommendation_lifecycle()
        if hasattr(data_engine, "get_recommendation_lifecycle")
        else {}
    )
    if household_context is None and hasattr(data_engine, "get_household_context"):
        household_context = data_engine.get_household_context()

    try:
        cycle = resolve_billing_cycle(settings, end)
        if start == cycle.start:
            projected_end = cycle.expected_end
            projected_days = cycle.expected_days
    except (TypeError, ValueError, AttributeError):
        pass

    projected_bill = financial_engine.project_bill(current_summary, settings)
    return {
        "current": current_summary,
        "previous": previous_summary,
        "import_rates": settings.import_rates,
        "ev_data_source": current_summary["ev_charging_source"],
        "ev_baseline_watts": settings.ev_baseline_watts,
        "overnight_baseload_watts": overnight_baseload,
        "timeline_history": timeline_history,
        "recommendation_lifecycle": recommendation_lifecycle,
        "capability_summary": (
            data_engine.get_household_capability_summary(end, settings).as_dict()
            if hasattr(data_engine, "get_household_capability_summary")
            else {}
        ),
        "household_id": household_context.household.household_id if household_context else None,
        "projected_period_days": projected_days,
        "projected_period_end": projected_end.isoformat(),
        "projected_net_cost": projected_bill[0],
        "projected_bill_impact": projected_bill[3],
    }


def _capability(summary: dict[str, Any], capability_id: str) -> dict[str, Any]:
    for capability in summary.get("capabilities", []):
        if capability.get("capability_id") == capability_id:
            return capability
    return {}


def generate_ev_session_recommendations(sessions: list[dict[str, Any]], import_rates: dict[str, float]) -> list[Recommendation]:
    """Deprecated compatibility wrapper for legacy EV-session recommendation tests."""
    if not sessions:
        return []
    engine = RecommendationEngine()
    total_kwh = sum(session["energy_kwh"] for session in sessions)
    tariff_usage = {
        bucket: {
            "kwh": sum(session["tariff_breakdown"][bucket]["kwh"] for session in sessions),
            "cost": sum(session["tariff_breakdown"][bucket]["cost"] for session in sessions),
        }
        for bucket in BUCKET_ORDER
    }
    for bucket in BUCKET_ORDER:
        tariff_usage[bucket]["pct"] = tariff_usage[bucket]["kwh"] / total_kwh * 100 if total_kwh else 0.0
    cheapest_bucket = min(BUCKET_ORDER, key=lambda bucket: import_rates.get(bucket, 0))
    peak_bucket = max(BUCKET_ORDER, key=lambda bucket: import_rates.get(bucket, 0))
    recommendations = []
    cheap_pct = tariff_usage[cheapest_bucket]["pct"]
    if cheap_pct >= 90:
        text = f"{cheap_pct:.0f}% of charging occurred during {EV_BUCKET_LABELS[cheapest_bucket]}."
        recommendations.append(
            engine._recommendation(
                "ev_session_low_cost_window",
                "EV",
                "EV charging is mostly in the low-cost window",
                text,
                None,
                None,
                "High",
                "Easy",
                {
                    "cheap_window_pct": cheap_pct,
                    "cheap_window_kwh": tariff_usage[cheapest_bucket]["kwh"],
                    "total_session_kwh": total_kwh,
                },
                text,
                "actual EV session data",
            )
        )
    peak_sessions = [
        session for session in sessions
        if session["tariff_breakdown"][peak_bucket]["cost"] > 0
    ]
    if peak_sessions:
        target = max(peak_sessions, key=lambda session: session["tariff_breakdown"][peak_bucket]["cost"])
        peak_cost = target["tariff_breakdown"][peak_bucket]["cost"]
        peak_kwh = target["tariff_breakdown"][peak_bucket]["kwh"]
        savings = max(engine.financial_engine.calculate_load_shift_savings(peak_kwh, import_rates[peak_bucket], import_rates[cheapest_bucket]), 0.0)
        text = (
            f"One session incurred {BUCKET_LABELS.get(peak_bucket, peak_bucket)} charges "
            f"costing ${peak_cost:,.2f}. Shifting that energy to "
            f"{EV_BUCKET_LABELS[cheapest_bucket]} would have saved about ${savings:,.2f}."
        )
        recommendations.append(
            engine._recommendation(
                "ev_session_peak_shift",
                "EV",
                "Shift Peak EV session energy",
                f"One EV session used {peak_kwh:.2f} kWh in the {BUCKET_LABELS.get(peak_bucket, peak_bucket)} bucket.",
                savings,
                None,
                "High",
                "Easy",
                {
                    "peak_session_cost": peak_cost,
                    "peak_session_kwh": peak_kwh,
                    "savings_if_shifted": savings,
                },
                text,
                "actual EV session data and tariff rates",
            )
        )
    return engine.rank(recommendations)


def recommendation_to_decision(recommendation: Recommendation, expires_at: Optional[str] = None) -> Decision:
    impact = FinancialImpact(
        billing_period=recommendation.estimated_financial_impact.period,
        basis=recommendation.estimated_financial_impact.basis,
    )
    evidence = {
        **dict(recommendation.supporting_evidence),
        "priority_score": recommendation.priority_score,
        "recommendation_category": recommendation.category,
        "estimated_monthly_impact": recommendation.estimated_financial_impact.monthly,
        "difficulty": recommendation.difficulty,
        "lifecycle_state": recommendation.dismissible_state.state,
        "lifecycle_snoozed_until": recommendation.dismissible_state.dismissed_until,
        "lifecycle_reason": recommendation.dismissible_state.reason,
    }
    decision = Decision(
        id=recommendation.stable_id,
        title=recommendation.title,
        summary=recommendation.explanation,
        finding=recommendation.explanation,
        evidence=evidence,
        financial_impact=impact,
        recommendation=recommendation.recommended_action,
        confidence=recommendation.confidence,
        priority=_priority_label(recommendation.priority_score),
        expires_at=expires_at,
        data_sources_used=_data_sources_for(recommendation),
        category=CATEGORY_TO_DECISION[recommendation.category],
        action_type=ACTION_TYPE_BY_ID.get(recommendation.stable_id, recommendation.category),
        explanation=_decision_explanation(recommendation),
    )
    return decision


def recommendations_to_decisions(recommendations: list[Recommendation], expires_at: Optional[str] = None) -> list[Decision]:
    return [recommendation_to_decision(item, expires_at=expires_at) for item in recommendations]


def _priority_label(score: int) -> str:
    for minimum, label in PRIORITY_LABELS:
        if score >= minimum:
            return label
    return "Low"


def _data_sources_for(recommendation: Recommendation) -> list[str]:
    if recommendation.stable_id == "baseload_monitoring":
        return ["SolarEdge interval data", "Estimated EV charging"]
    if recommendation.stable_id == "trend_repeated_estimated_data_warning":
        return ["PowerMonitor timeline history", "Estimated EV charging"]
    if recommendation.stable_id.startswith("trend_"):
        sources = ["PowerMonitor timeline history"]
        if recommendation.category == "tariff":
            sources.append("Tariff settings")
        elif recommendation.category == "EV":
            sources.extend(["Actual EV session data", "Tariff settings"])
        elif recommendation.category == "solar":
            sources.append("SolarEdge interval data")
        elif recommendation.category == "billing":
            sources.extend(["Period comparison", "FinancialEngine report output"])
        return list(dict.fromkeys(sources))
    basis = recommendation.estimated_financial_impact.basis or "Recommendation Engine observations"
    sources = [basis]
    if recommendation.category in {"billing", "solar", "tariff", "baseload"}:
        sources.append("SolarEdge interval data")
    if recommendation.category == "EV":
        sources.append("EV session or EV estimate data")
    if recommendation.category == "data_quality":
        sources.append("Data completeness summary")
    return list(dict.fromkeys(sources))


def _decision_explanation(recommendation: Recommendation) -> DecisionExplanation:
    why_this_matters = recommendation.explanation
    if recommendation.stable_id == "baseload_monitoring":
        why_this_matters = "overnight load can quietly add cost across the whole billing period."
    elif recommendation.stable_id == "trend_repeated_peak_usage":
        why_this_matters = "This matters now because a recent high-peak event confirms the pattern is still active."
    elif recommendation.stable_id == "trend_recurring_ev_charging_win":
        why_this_matters = "This matters now because the low-cost EV charging pattern is still appearing recently, not only in old history."
    elif recommendation.stable_id == "trend_recurring_solar_utilisation_win":
        why_this_matters = "This matters now because a positive solar/utilisation pattern is recurring in recent history."
    elif recommendation.stable_id == "trend_repeated_estimated_data_warning":
        why_this_matters = "This matters now because fallback EV data is still being used in the last 7 days."
    return DecisionExplanation(
        why_this_matters=why_this_matters,
        evidence_used=[
            f"{key.replace('_', ' ').capitalize()}: {value}"
            for key, value in recommendation.supporting_evidence.items()
        ],
        financial_impact=_impact_text(recommendation),
        confidence_reason=(
            f"{recommendation.confidence} confidence based on measurable evidence "
            f"from {recommendation.estimated_financial_impact.basis or 'available observations'}."
        ),
        data_sources_used=_data_sources_for(recommendation),
        freshness_completeness_note=None,
    )


def _impact_text(recommendation: Recommendation) -> str:
    impact = recommendation.estimated_financial_impact
    if impact.period is not None:
        return f"${float(impact.period):,.2f} this period"
    if impact.monthly is not None:
        return f"${float(impact.monthly):,.2f} per month"
    return "Unable to estimate from the current household data."


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _is_future(value: Optional[str], now: datetime) -> bool:
    if not value:
        return False
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.max.time())
        except ValueError:
            return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed > now


def _entry_date(entry) -> Optional[date]:
    try:
        return date.fromisoformat(str(entry.timestamp)[:10])
    except ValueError:
        return None


def _trend_window_start(context: dict[str, Any], end: date, window: str) -> date:
    if window == "last_7_days":
        return end - timedelta(days=6)
    if window == "current_billing_period":
        return _as_date(context["current"]["start"])
    return end - timedelta(days=13)


def _trend_stats(context, entries, end, matcher, window):
    start = _trend_window_start(context, end, window)
    matched = [entry for entry in entries if matcher(entry)]
    recent = []
    decayed_score = 0.0
    for entry in matched:
        entry_day = _entry_date(entry)
        if not entry_day:
            continue
        age_days = max((end - entry_day).days, 0)
        decayed_score += 0.5 ** (age_days / 14.0)
        if start <= entry_day <= end:
            recent.append(entry)
    return {
        "recent_count": len(recent),
        "historical_count": len(matched),
        "decayed_event_score": round(decayed_score, 2),
        "window": {"last_7_days": "last 7 days", "last_14_days": "last 14 days", "current_billing_period": "current billing period"}[window],
    }


def _trend_is_active(stats: dict[str, Any], thresholds: dict[str, Any]) -> bool:
    trigger = int(thresholds.get("trend_repeated_event_count", 2))
    recent_trigger = int(thresholds.get("trend_recent_event_count", 1))
    return (
        stats["historical_count"] >= trigger
        and stats["recent_count"] >= recent_trigger
        and stats["decayed_event_score"] >= recent_trigger
    )


def _trend_evidence(stats: dict[str, Any], count_key: str) -> dict[str, Any]:
    return {
        "recent_event_count": stats["recent_count"],
        "historical_event_count": stats["historical_count"],
        "recency_window_used": stats["window"],
        "decayed_event_score": stats["decayed_event_score"],
        count_key: stats["historical_count"],
    }
