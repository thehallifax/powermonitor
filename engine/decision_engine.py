#!/usr/bin/env python3
from datetime import date, timedelta
from typing import Callable, Optional

from .financial_engine import FinancialEngine
from .models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    FINANCIAL_IMPACT_PERIODS,
    Decision,
    FinancialImpact,
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


Rule = Callable[[dict, dict], Optional[Decision]]

financial_engine = FinancialEngine()


def _impact(
    today: Optional[float] = None,
    billing_period: Optional[float] = None,
    yearly: Optional[float] = None,
) -> FinancialImpact:
    return FinancialImpact(
        today=today,
        billing_period=billing_period,
        yearly=yearly,
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _data_sources(context: dict, *extra_sources: str) -> list[str]:
    sources = ["SolarEdge interval data"]
    ev_source = context.get("ev_data_source")
    if ev_source == "home_assistant_ocpp":
        sources.append("HA OCPP session snapshots")
    elif ev_source == "ocular_export":
        sources.append("Ocular EV export")
    elif ev_source:
        sources.append("Estimated EV charging")
    sources.extend(extra_sources)
    return list(dict.fromkeys(sources))


def _validate_decision(decision: Decision) -> Decision:
    if decision.category not in CATEGORIES:
        raise ValueError(f"Unsupported decision category: {decision.category}")
    if decision.confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"Unsupported decision confidence: {decision.confidence}")
    for period in FINANCIAL_IMPACT_PERIODS:
        decision.financial_impact.setdefault(period, None)
    return decision


def peak_cost_reduction(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    trigger = thresholds["peak_cost_share_pct"]
    if summary["peak_cost_share_pct"] <= trigger:
        return None
    shift_kwh = thresholds["peak_shift_kwh_per_day"]
    shiftable_kwh = min(summary["buckets"]["peak"]["kwh"], shift_kwh * summary["reading_days"])
    savings = financial_engine.calculate_load_shift_savings(
        shiftable_kwh,
        context["import_rates"]["peak"],
        context["import_rates"]["super_off_peak"],
    )
    impact = financial_engine.financial_impact(billing_period=savings)
    finding = (
        f"Peak usage accounts for {summary['peak_cost_share_pct']:.1f}% of import cost "
        f"and currently costs ${summary['buckets']['peak']['cost']:.2f}."
    )
    return Decision(
        id="peak_cost_reduction",
        title="Reduce Peak-period imports",
        summary=finding,
        finding=finding,
        evidence={
            "peak_cost": summary["buckets"]["peak"]["cost"],
            "peak_cost_share_pct": summary["peak_cost_share_pct"],
            "shift_kwh_per_day": shift_kwh,
            "estimated_period_savings": savings,
        },
        financial_impact=impact,
        recommendation=f"Moving up to {shift_kwh:.1f} kWh/day from Peak to 9am-3pm could save about ${savings:.2f} this period.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context, "Tariff settings"),
        category="Tariffs",
        action_type="load_shift",
    )


def solar_self_consumption(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    if summary["self_consumption_pct"] >= thresholds["solar_self_consumption_pct"]:
        return None
    finding = (
        f"Solar self-consumption is {summary['self_consumption_pct']:.1f}% and "
        f"{summary['solar_export_pct']:.1f}% of generation is exported."
    )
    return Decision(
        id="solar_self_consumption",
        title="Use more solar at home",
        summary=finding,
        finding=finding,
        evidence={
            "self_consumption_pct": summary["self_consumption_pct"],
            "solar_export_pct": summary["solar_export_pct"],
        },
        financial_impact=_impact(),
        recommendation="Shift flexible loads such as appliances, hot water, or EV charging into the 9am-3pm solar window.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="Solar",
        action_type="load_shift",
    )


def ev_charging_optimisation(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    if summary["ev_charging_kwh"] <= thresholds["ev_detected_min_kwh"]:
        return None
    actual = context.get("ev_data_source") in {"home_assistant_ocpp", "ocular_export"}
    energy_label = "Actual EV charging" if actual else "Estimated overnight EV charging"
    peak_cost = financial_engine.cost_of_ev_session(
        summary["ev_charging_kwh"],
        context["import_rates"]["peak"],
    )
    observed_cost = summary["ev_charging_cost"]
    savings_vs_peak = financial_engine.calculate_cost_difference(peak_cost, observed_cost)
    if actual:
        breakdown = summary.get("actual_ev_tariff_breakdown") or {}
        dominant = max(
            ("super_off_peak", "overnight", "off_peak", "peak"),
            key=lambda bucket: (breakdown.get(bucket) or {}).get("kwh", 0),
        )
        total_kwh = sum((breakdown.get(bucket) or {}).get("kwh", 0) for bucket in breakdown)
        if total_kwh <= 0:
            window_message = "Charging was imported from actual EV session data."
        elif (breakdown.get(dominant) or {}).get("kwh", 0) / total_kwh < 0.5:
            window_message = "Charging was split across tariff windows."
        elif dominant == "super_off_peak":
            window_message = "Charging was mostly in the cheapest 9am-3pm EV window."
        elif dominant == "overnight":
            window_message = "Charging was mostly overnight."
        else:
            window_message = "Charging was mostly outside the cheapest EV windows."
        finding = (
            f"{energy_label} is {summary['ev_charging_kwh']:.2f} kWh with an observed "
            f"OCPP cost estimate of ${observed_cost:.2f}."
        )
        recommendation = (
            f"{window_message} The same charging at the Peak rate would cost about "
            f"${peak_cost:.2f}."
        )
    else:
        finding = (
            f"{energy_label} is {summary['ev_charging_kwh']:.2f} kWh, costing about "
            f"${observed_cost:.2f} at the overnight rate."
        )
        recommendation = (
            f"The same estimated charging at the Peak rate would cost about ${peak_cost:.2f}. "
            "Daytime EV charging is not included without charger session data."
        )
    return Decision(
        id="ev_charging_optimisation",
        title=energy_label,
        summary=finding,
        finding=finding,
        evidence={
            "estimated_ev_kwh": summary["ev_charging_kwh"],
            "observed_or_estimated_cost": observed_cost,
            "estimated_peak_cost": peak_cost,
            "estimated_savings_vs_peak": savings_vs_peak,
        },
        financial_impact=financial_engine.financial_impact(billing_period=savings_vs_peak),
        recommendation=recommendation,
        confidence="Medium",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context, "Tariff settings"),
        category="EV",
        action_type="tariff_window",
    )


def baseload_monitoring(context: dict, thresholds: dict) -> Optional[Decision]:
    current_baseload = context["overnight_baseload_watts"]
    expected = context["ev_baseline_watts"]
    trigger = expected * (1 + thresholds["baseload_excess_pct"] / 100)
    if current_baseload <= trigger:
        return None
    finding = (
        f"Typical overnight baseload is about {current_baseload:.0f} W versus the "
        f"configured {expected:.0f} W baseline."
    )
    return Decision(
        id="baseload_monitoring",
        title="Review overnight baseload",
        summary=finding,
        finding=finding,
        evidence={
            "current_baseload_watts": current_baseload,
            "expected_baseline_watts": expected,
            "excess_pct": (current_baseload / expected - 1) * 100 if expected else 0,
        },
        financial_impact=_impact(),
        recommendation="Check always-on devices, heating, hot water, networking equipment, and standby loads.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="Behaviour",
        action_type="investigate",
    )


def high_cost_alert(context: dict, thresholds: dict) -> Optional[Decision]:
    projected = context["projected_net_cost"]
    threshold = thresholds["high_cost_threshold"]
    if projected <= threshold:
        return None
    reading_days = context["current"]["reading_days"]
    if reading_days < 10:
        confidence = "Low"
    elif reading_days <= 20:
        confidence = "Medium"
    else:
        confidence = "High"
    actual_cost = context["current"]["net_cost"]
    priority = "High" if actual_cost > threshold or confidence != "Low" else "Medium"
    finding = (
        f"Actual net cost so far is ${actual_cost:.2f}; the projected cost by "
        f"{context['projected_period_end']} is ${projected:.2f}. "
        f"This is a {confidence.lower()}-confidence projection."
    )
    return Decision(
        id="high_cost_alert",
        title="Projected billing cost is high",
        summary=finding,
        finding=finding,
        evidence={
            "actual_cost_so_far": actual_cost,
            "projected_net_cost": projected,
            "projected_period_end": context["projected_period_end"],
            "projection_confidence": confidence.lower(),
            "projection_reading_days": reading_days,
            "configured_threshold": threshold,
            "projected_period_days": context["projected_period_days"],
        },
        financial_impact=context.get("projected_bill_impact") or _impact(billing_period=projected),
        recommendation=(
            f"Projection is based on {reading_days} days of data. "
            "Prioritise Peak-period load shifting and review high-import days before the next meter read."
        ),
        confidence=confidence,
        priority=priority,
        expires_at=context["projected_period_end"],
        data_sources_used=_data_sources(context, "Billing period settings"),
        category="Bills",
        action_type="review",
    )


def positive_reinforcement(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    wins = []
    if summary["peak_cost_share_pct"] < thresholds["positive_peak_share_pct"]:
        wins.append(f"Peak share is only {summary['peak_cost_share_pct']:.1f}%")
    if summary["self_consumption_pct"] > thresholds["positive_self_consumption_pct"]:
        wins.append(f"solar self-consumption is {summary['self_consumption_pct']:.1f}%")
    previous = context.get("previous")
    if previous and summary["average_daily_net_cost"] < previous["average_daily_net_cost"]:
        wins.append("average daily net cost is lower than the previous billing period")
    if not wins:
        return None
    finding = "; ".join(wins).capitalize() + "."
    return Decision(
        id="positive_reinforcement",
        title="You're doing well",
        summary=finding,
        finding=finding,
        evidence={"positive_signals": len(wins)},
        financial_impact=_impact(),
        recommendation="Keep the current habits going and continue using the low-cost and solar windows.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="General",
        action_type="maintain",
    )


RULES: tuple[Rule, ...] = (
    high_cost_alert,
    peak_cost_reduction,
    solar_self_consumption,
    ev_charging_optimisation,
    baseload_monitoring,
    positive_reinforcement,
)


def generate_decisions(context: dict, thresholds: dict) -> list[Decision]:
    decisions = []
    for rule in RULES:
        decision = rule(context, thresholds)
        if decision:
            decisions.append(_validate_decision(decision))
    return decisions


def build_decision_context(
    current_summary: dict,
    settings,
    data_engine,
    previous_summary: Optional[dict] = None,
) -> dict:
    projected_days = current_summary["supply_days"]
    projected_end = _as_date(current_summary["end"])
    start = _as_date(current_summary["start"])
    end = _as_date(current_summary["end"])
    overnight_baseload = data_engine.get_overnight_baseload_watts(start, end)

    if settings.current_billing_period_start and settings.current_billing_period_end:
        projected_end = settings.current_billing_period_end
        projected_days = (projected_end - settings.current_billing_period_start).days + 1
    elif (
        settings.current_billing_period_start
        and settings.previous_billing_period_start
        and settings.previous_billing_period_end
    ):
        projected_days = (
            settings.previous_billing_period_end - settings.previous_billing_period_start
        ).days + 1
        projected_end = settings.current_billing_period_start + timedelta(days=projected_days - 1)

    projected_bill = financial_engine.project_bill(current_summary, settings)
    return {
        "current": current_summary,
        "previous": previous_summary,
        "import_rates": settings.import_rates,
        "ev_data_source": current_summary["ev_charging_source"],
        "ev_baseline_watts": settings.ev_baseline_watts,
        "overnight_baseload_watts": overnight_baseload,
        "projected_period_days": projected_days,
        "projected_period_end": projected_end.isoformat(),
        "projected_net_cost": projected_bill[0],
        "projected_bill_impact": projected_bill[3],
    }


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def generate_decisions_from_data_engine(
    current_summary: dict,
    settings,
    data_engine,
    thresholds: dict,
    previous_summary: Optional[dict] = None,
) -> list[Decision]:
    context = build_decision_context(
        current_summary,
        settings,
        data_engine,
        previous_summary=previous_summary,
    )
    return generate_decisions(context, thresholds)


def generate_recommendations(context: dict, thresholds: dict) -> list[dict]:
    return [decision.as_dict() for decision in generate_decisions(context, thresholds)]


def generate_ev_session_decisions(sessions: list[dict], import_rates: dict) -> list[Decision]:
    if not sessions:
        return []

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

    decisions = []
    cheapest_bucket = min(BUCKET_ORDER, key=lambda bucket: import_rates.get(bucket, 0))
    peak_bucket = max(BUCKET_ORDER, key=lambda bucket: import_rates.get(bucket, 0))
    cheap_pct = tariff_usage[cheapest_bucket]["pct"]
    if cheap_pct >= 90:
        recommendation = (
            f"{cheap_pct:.0f}% of charging occurred during "
            f"{EV_BUCKET_LABELS[cheapest_bucket]}."
        )
        decisions.append(
            Decision(
                id="ev_session_low_cost_window",
                title="EV charging is mostly in the low-cost window",
                summary=recommendation,
                finding=recommendation,
                evidence={
                    "cheap_window_pct": cheap_pct,
                    "cheap_window_kwh": tariff_usage[cheapest_bucket]["kwh"],
                    "total_session_kwh": total_kwh,
                },
                financial_impact=_impact(),
                recommendation=recommendation,
                confidence="High",
                priority="Low",
                expires_at=None,
                data_sources_used=["Actual EV session data", "Tariff settings"],
                category="EV",
                action_type="maintain",
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
        savings = max(
            financial_engine.calculate_load_shift_savings(
                peak_kwh,
                import_rates[peak_bucket],
                import_rates[cheapest_bucket],
            ),
            0.0,
        )
        recommendation = (
            f"One session incurred {BUCKET_LABELS.get(peak_bucket, peak_bucket)} charges "
            f"costing {_money(peak_cost)}. Shifting that energy to "
            f"{EV_BUCKET_LABELS[cheapest_bucket]} would have saved about {_money(savings)}."
        )
        decisions.append(
            Decision(
                id="ev_session_peak_shift",
                title="Shift Peak EV session energy",
                summary=recommendation,
                finding=(
                    f"One EV session used {peak_kwh:.2f} kWh in the "
                    f"{BUCKET_LABELS.get(peak_bucket, peak_bucket)} bucket."
                ),
                evidence={
                    "peak_session_cost": peak_cost,
                    "peak_session_kwh": peak_kwh,
                    "savings_if_shifted": savings,
                },
                financial_impact=financial_engine.financial_impact(billing_period=savings),
                recommendation=recommendation,
                confidence="High",
                priority="Medium",
                expires_at=None,
                data_sources_used=["Actual EV session data", "Tariff settings"],
                category="EV",
                action_type="load_shift",
            )
        )

    return [_validate_decision(decision) for decision in decisions]
