#!/usr/bin/env python3
from datetime import date, timedelta
from typing import Callable, Optional

from .financial_engine import FinancialEngine
from .models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    FINANCIAL_IMPACT_PERIODS,
    Decision,
    DecisionExplanation,
    FinancialImpact,
    HouseholdContext,
)
from .recommendation_engine import (
    generate_ev_session_recommendations,
    generate_recommendations as generate_shared_recommendations,
    generate_recommendations_from_data_engine as generate_shared_recommendations_from_data_engine,
    recommendations_to_decisions,
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
    if decision.explanation is None:
        decision.explanation = _decision_explanation(decision)
    return decision


def _decision_explanation(decision: Decision) -> DecisionExplanation:
    return DecisionExplanation(
        why_this_matters=_why_this_matters(decision),
        evidence_used=_evidence_used(decision),
        financial_impact=_financial_impact_text(decision),
        confidence_reason=_confidence_reason(decision),
        data_sources_used=list(decision.data_sources_used),
        freshness_completeness_note=_freshness_completeness_note(decision),
    )


def _why_this_matters(decision: Decision) -> str:
    if decision.category == "Bills":
        return "This can affect the current bill and should be reviewed before the billing period closes."
    if decision.category == "Tariffs":
        return "This points to usage that may cost more than it needs to under the current tariff."
    if decision.category == "Solar":
        return "This affects how much solar is used at home instead of exported at a lower credit."
    if decision.category == "EV":
        return "EV charging is a flexible load, so timing can materially change charging cost."
    if decision.category == "Behaviour":
        return "Always-on or overnight load can quietly add cost across the whole billing period."
    if decision.action_type == "maintain":
        return "This is a positive signal worth keeping in place."
    return "This decision highlights an energy pattern that may be worth acting on."


def _evidence_used(decision: Decision) -> list[str]:
    if not decision.evidence:
        return [decision.finding]
    return [
        f"{_evidence_label(key)}: {_evidence_value(value)}"
        for key, value in decision.evidence.items()
    ]


def _evidence_label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _evidence_value(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _financial_impact_text(decision: Decision) -> str:
    labels = {
        "today": "today",
        "billing_period": "this billing period",
        "yearly": "per year",
    }
    for key in FINANCIAL_IMPACT_PERIODS:
        value = decision.financial_impact.get(key)
        if value is not None:
            return f"{_money(float(value))} {labels[key]}"
    return "Not quantified by the current Decision Engine output."


def _confidence_reason(decision: Decision) -> str:
    projection_days = decision.evidence.get("projection_reading_days")
    if projection_days is not None:
        return (
            f"{decision.confidence} confidence because the projection uses "
            f"{projection_days} reading days and the available source metadata."
        )
    if any("Actual" in source or "Ocular" in source for source in decision.data_sources_used):
        return f"{decision.confidence} confidence because actual session/source data is available."
    if "Estimated EV charging" in decision.data_sources_used:
        return f"{decision.confidence} confidence because part of the result uses estimated EV charging."
    return f"{decision.confidence} confidence based on the available interval data and configured assumptions."


def _freshness_completeness_note(decision: Decision) -> Optional[str]:
    notes = []
    reading_days = decision.evidence.get("projection_reading_days")
    projected_days = decision.evidence.get("projected_period_days")
    if reading_days is not None and projected_days is not None:
        notes.append(f"Uses {reading_days} of {projected_days} projected period days.")
    elif reading_days is not None:
        notes.append(f"Uses {reading_days} reading days.")
    if decision.expires_at:
        notes.append(f"Decision expires at {decision.expires_at}.")
    return " ".join(notes) if notes else None


def _timeline_entries(context: dict) -> list:
    timeline = context.get("timeline_history")
    return list(getattr(timeline, "entries", []) or [])


def _timeline_count(context: dict, event_type: str) -> int:
    return sum(1 for entry in _timeline_entries(context) if entry.event_type == event_type)


def _latest_timeline_impact(context: dict, event_type: str) -> Optional[str]:
    matching = [
        entry for entry in _timeline_entries(context)
        if entry.event_type == event_type and entry.financial_impact
    ]
    return matching[-1].financial_impact if matching else None


def _trend_sources(*extra_sources: str) -> list[str]:
    return list(dict.fromkeys(("PowerMonitor timeline history", *extra_sources)))


def _entry_date(entry) -> Optional[date]:
    try:
        return date.fromisoformat(str(entry.timestamp)[:10])
    except ValueError:
        return None


def _trend_window_start(context: dict, window: str) -> date:
    end = _as_date(context["current"]["end"])
    if window == "last_7_days":
        return end - timedelta(days=6)
    if window == "current_billing_period":
        return _as_date(context["current"]["start"])
    return end - timedelta(days=13)


def _window_label(window: str) -> str:
    return {
        "last_7_days": "last 7 days",
        "last_14_days": "last 14 days",
        "current_billing_period": "current billing period",
    }.get(window, window.replace("_", " "))


def _trend_stats(context: dict, matcher, window: str) -> dict:
    end = _as_date(context["current"]["end"])
    start = _trend_window_start(context, window)
    half_life = float(context.get("trend_decay_half_life_days", 14.0))
    matched = [entry for entry in _timeline_entries(context) if matcher(entry)]
    recent = []
    decayed_score = 0.0
    for entry in matched:
        entry_day = _entry_date(entry)
        if not entry_day:
            continue
        age_days = max((end - entry_day).days, 0)
        decayed_score += 0.5 ** (age_days / max(half_life, 1.0))
        if start <= entry_day <= end:
            recent.append(entry)
    return {
        "recent_count": len(recent),
        "historical_count": len(matched),
        "decayed_event_score": round(decayed_score, 2),
        "window": _window_label(window),
    }


def _trend_is_active(stats: dict, thresholds: dict) -> bool:
    trigger = int(thresholds.get("trend_repeated_event_count", 2))
    recent_trigger = int(thresholds.get("trend_recent_event_count", 1))
    return (
        stats["historical_count"] >= trigger
        and stats["recent_count"] >= recent_trigger
        and stats["decayed_event_score"] >= recent_trigger
    )


def _trend_evidence(stats: dict, count_key: str) -> dict:
    return {
        "recent_event_count": stats["recent_count"],
        "historical_event_count": stats["historical_count"],
        "recency_window_used": stats["window"],
        "decayed_event_score": stats["decayed_event_score"],
        count_key: stats["historical_count"],
    }


def _trend_explanation(
    why_now: str,
    confidence_reason: str,
    financial_impact: str,
    evidence: dict,
    sources: list[str],
) -> DecisionExplanation:
    return DecisionExplanation(
        why_this_matters=why_now,
        evidence_used=[
            f"{_evidence_label(key)}: {_evidence_value(value)}"
            for key, value in evidence.items()
        ],
        financial_impact=financial_impact,
        confidence_reason=confidence_reason,
        data_sources_used=sources,
        freshness_completeness_note=(
            f"Trend analysis used the {evidence['recency_window_used']} window "
            f"and a decayed event score of {evidence['decayed_event_score']}."
        ),
    )


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


def repeated_peak_usage_trend(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "high_peak_usage",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    latest_impact = _latest_timeline_impact(context, "high_peak_usage")
    finding = (
        f"High peak usage has appeared {stats['recent_count']} times in the "
        f"{stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Tariff settings")
    evidence = {
        **_trend_evidence(stats, "high_peak_usage_events"),
        "latest_recorded_impact": latest_impact or "not quantified",
    }
    confidence_reason = (
        f"Medium confidence because at least one high-peak event is recent and "
        f"{stats['historical_count']} matching events exist in persisted history."
    )
    return Decision(
        id="trend_repeated_peak_usage",
        title="Peak usage is becoming a pattern",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Review flexible loads that repeatedly land in Peak periods and prioritise shifting them to lower-cost windows.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Tariffs",
        action_type="trend_review",
        explanation=_trend_explanation(
            "This matters now because a recent high-peak event confirms the pattern is still active.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def bill_trajectory_trend(context: dict, thresholds: dict) -> Optional[Decision]:
    improved_stats = _trend_stats(
        context,
        lambda entry: entry.title == "Bill tracking improved",
        "current_billing_period",
    )
    worsened_stats = _trend_stats(
        context,
        lambda entry: entry.title == "Bill tracking worsened",
        "current_billing_period",
    )
    improved = improved_stats["historical_count"]
    worsened = worsened_stats["historical_count"]
    if improved == worsened:
        return None
    direction = "improving" if improved > worsened else "worsening"
    dominant_stats = improved_stats if direction == "improving" else worsened_stats
    if not _trend_is_active(dominant_stats, thresholds):
        return None
    finding = (
        f"Bill trajectory is {direction}: {dominant_stats['recent_count']} "
        f"{direction} events are in the {dominant_stats['window']}, with "
        f"{improved} improved and {worsened} worsened events recorded historically."
    )
    sources = _trend_sources("Period comparison", "FinancialEngine report output")
    evidence = {
        **_trend_evidence(dominant_stats, f"bill_tracking_{direction}_events"),
        "bill_tracking_improved_events": improved,
        "bill_tracking_worsened_events": worsened,
    }
    confidence_reason = (
        f"Medium confidence because the {direction} bill trend has recent "
        f"evidence in the {dominant_stats['window']} and more matching events than the opposite direction."
    )
    return Decision(
        id=f"trend_bill_trajectory_{direction}",
        title=f"Bill trajectory is {direction}",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation=(
            "Keep reinforcing the behaviours behind the improvement."
            if direction == "improving"
            else "Review recent high-cost days and recurring recommendations before this trend continues."
        ),
        confidence="Medium",
        priority="Low" if direction == "improving" else "Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Bills",
        action_type="trend_review",
        explanation=_trend_explanation(
            f"This matters now because the bill trajectory has recent {direction} evidence in the current billing period.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def recurring_ev_charging_win(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "ev_cheapest_tariff",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"EV charging has landed in the cheapest tariff window {stats['recent_count']} "
        f"times in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Actual EV session data", "Tariff settings")
    evidence = _trend_evidence(stats, "ev_cheapest_tariff_events")
    confidence_reason = (
        "High confidence because recurring low-cost EV charging is present in recent "
        "timeline history and comes from actual session/tariff evidence."
    )
    return Decision(
        id="trend_recurring_ev_charging_win",
        title="EV charging win is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Keep the EV schedule aligned with the cheapest tariff window and watch for any Peak-session regressions.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="EV",
        action_type="maintain",
        explanation=_trend_explanation(
            "This matters now because the low-cost EV charging pattern is still appearing recently, not only in old history.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def recurring_solar_utilisation_win(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "positive_outcome",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"Positive solar/utilisation outcomes have appeared {stats['recent_count']} "
        f"times in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("SolarEdge interval data")
    evidence = _trend_evidence(stats, "positive_outcome_events")
    confidence_reason = (
        "High confidence because the utilisation win appears in recent timeline history "
        "and is backed by interval evidence."
    )
    return Decision(
        id="trend_recurring_solar_utilisation_win",
        title="Solar utilisation win is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Keep flexible loads in solar and low-cost windows so the recurring positive pattern continues.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Solar",
        action_type="maintain",
        explanation=_trend_explanation(
            "This matters now because a positive solar/utilisation pattern is recurring in recent history.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def repeated_estimated_data_warning(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: "Estimated EV charging" in entry.evidence_sources,
        "last_7_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"Estimated/fallback EV data has been used in {stats['recent_count']} "
        f"timeline events in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Estimated EV charging")
    evidence = _trend_evidence(stats, "estimated_ev_timeline_events")
    confidence_reason = (
        "Medium confidence because estimated/fallback data appears in recent timeline "
        "history, but the underlying EV sessions are not fully measured."
    )
    return Decision(
        id="trend_repeated_estimated_data_warning",
        title="Estimated data is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Connect or refresh actual EV session data so future decisions rely less on fallback estimates.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="General",
        action_type="data_quality",
        explanation=_trend_explanation(
            "This matters now because fallback EV data is still being used in the last 7 days.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


RULES: tuple[Rule, ...] = (
    high_cost_alert,
    peak_cost_reduction,
    solar_self_consumption,
    ev_charging_optimisation,
    baseload_monitoring,
    positive_reinforcement,
    repeated_peak_usage_trend,
    bill_trajectory_trend,
    recurring_ev_charging_win,
    recurring_solar_utilisation_win,
    repeated_estimated_data_warning,
)


def generate_decisions(
    context: dict,
    thresholds: dict,
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Decision]:
    recommendations = generate_shared_recommendations(
        context,
        thresholds,
        include_inactive=include_inactive,
        household_context=household_context,
    )
    return [
        _validate_decision(decision)
        for decision in recommendations_to_decisions(
            recommendations,
            expires_at=context.get("projected_period_end"),
        )
    ]


def build_decision_context(
    current_summary: dict,
    settings,
    data_engine,
    previous_summary: Optional[dict] = None,
    household_context: Optional[HouseholdContext] = None,
) -> dict:
    projected_days = current_summary["supply_days"]
    projected_end = _as_date(current_summary["end"])
    start = _as_date(current_summary["start"])
    end = _as_date(current_summary["end"])
    overnight_baseload = data_engine.get_overnight_baseload_watts(start, end)
    timeline_history = data_engine.get_timeline_entries(limit=100)
    if household_context is None and hasattr(data_engine, "get_household_context"):
        household_context = data_engine.get_household_context()

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
        "timeline_history": timeline_history,
        "household_id": household_context.household.household_id if household_context else None,
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
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Decision]:
    recommendations = generate_shared_recommendations_from_data_engine(
        current_summary,
        settings,
        data_engine,
        thresholds,
        previous_summary=previous_summary,
        include_inactive=include_inactive,
        household_context=household_context,
    )
    expires_at = current_summary.get("end")
    return [_validate_decision(decision) for decision in recommendations_to_decisions(recommendations, expires_at=str(expires_at))]


def generate_recommendations(
    context: dict,
    thresholds: dict,
    household_context: Optional[HouseholdContext] = None,
) -> list[dict]:
    return [
        decision.as_dict()
        for decision in generate_decisions(context, thresholds, household_context=household_context)
    ]


def generate_ev_session_decisions(sessions: list[dict], import_rates: dict) -> list[Decision]:
    return [
        _validate_decision(decision)
        for decision in recommendations_to_decisions(
            generate_ev_session_recommendations(sessions, import_rates)
        )
    ]
