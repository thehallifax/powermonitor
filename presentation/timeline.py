#!/usr/bin/env python3
"""Timeline presentation model for existing report output."""

from datetime import date
from typing import Any, Iterable, Optional

from engine.models import Decision, DecisionTimeline, TimelineEntry


def build_decision_timeline(
    period_summary: Any,
    decisions: Iterable[Decision],
    comparison: Optional[dict[str, Any]] = None,
    include_billing_period: bool = True,
    household_decisions: Optional[Iterable[Any]] = None,
) -> DecisionTimeline:
    """Build a chronological memory from existing summaries and decisions."""

    entries = [_billing_period_entry(period_summary)] if include_billing_period else []
    bill_entry = _bill_tracking_entry(comparison)
    if bill_entry:
        entries.append(bill_entry)

    entries.extend(_decision_entry(period_summary, decision) for decision in decisions)
    entries.extend(_household_decision_entry(period_summary, decision) for decision in (household_decisions or []))
    return DecisionTimeline(
        entries=sorted(entries, key=lambda entry: (entry.timestamp, entry.title))
    )


def _billing_period_entry(period_summary: Any) -> TimelineEntry:
    start = _value(period_summary, "start")
    end = _value(period_summary, "end")
    has_readings = int(_value(period_summary, "intervals") or 0) > 0 if _has_value(period_summary, "intervals") else False
    title = "Billing period updated" if has_readings else "Billing period created"
    summary = (
        f"Updated billing-period tracking for {_display_date(start)} to {_display_date(end)} with available readings."
        if has_readings
        else f"Created billing-period tracking for {_display_date(start)} to {_display_date(end)}."
    )
    return TimelineEntry(
        timestamp=_iso_date(start),
        title=title,
        summary=summary,
        financial_impact=None,
        confidence="High",
        evidence_sources=["Billing period settings"],
        event_type="billing_period_updated" if has_readings else "billing_period_created",
    )


def _bill_tracking_entry(comparison: Optional[dict[str, Any]]) -> Optional[TimelineEntry]:
    if not comparison:
        return None
    delta = comparison.get("deltas", {}).get("net_cost")
    period = comparison.get("period_a", {})
    if delta is None:
        return None
    direction = "improved" if delta < 0 else "worsened" if delta > 0 else "held steady"
    return TimelineEntry(
        timestamp=str(period.get("end") or period.get("start") or ""),
        title=f"Bill tracking {direction}",
        summary=(
            "Current period net cost is being compared with the previous "
            "equivalent period using existing report deltas."
        ),
        financial_impact=f"{float(delta):+,.2f} AUD vs comparison period",
        confidence="Medium",
        evidence_sources=["Period comparison", "Bill estimate"],
        event_type="bill_tracking",
    )


def _decision_entry(period_summary: Any, decision: Decision) -> TimelineEntry:
    return TimelineEntry(
        timestamp=decision.expires_at or _iso_date(_value(period_summary, "end")),
        title=_timeline_title(decision),
        summary=decision.finding,
        financial_impact=_decision_financial_impact(decision),
        confidence=decision.confidence,
        evidence_sources=list(decision.data_sources_used),
        event_type=_timeline_event_type(decision),
    )


def _timeline_title(decision: Decision) -> str:
    titles = {
        "ev_session_low_cost_window": "EV charged in cheapest tariff",
        "ev_charging_optimisation": "EV charging reviewed",
        "peak_cost_reduction": "High peak usage",
        "high_cost_alert": "High bill projection",
        "solar_self_consumption": "Solar utilisation opportunity",
        "positive_reinforcement": "Excellent utilisation signal",
    }
    return titles.get(decision.id, decision.title)


def _timeline_event_type(decision: Decision) -> str:
    event_types = {
        "ev_session_low_cost_window": "ev_cheapest_tariff",
        "ev_charging_optimisation": "ev_charging_reviewed",
        "peak_cost_reduction": "high_peak_usage",
        "high_cost_alert": "high_bill_projection",
        "solar_self_consumption": "solar_utilisation",
        "positive_reinforcement": "positive_outcome",
    }
    return event_types.get(decision.id, f"decision_{decision.action_type}")


def _decision_financial_impact(decision: Decision) -> Optional[str]:
    explanation = decision.explanation
    if explanation and not explanation.financial_impact.lower().startswith(("not quantified", "unable to estimate")):
        return explanation.financial_impact
    for key, label in (
        ("today", "today"),
        ("billing_period", "this billing period"),
        ("yearly", "per year"),
    ):
        value = decision.financial_impact.get(key)
        if value is not None:
            return f"{float(value):+,.2f} AUD {label}"
    return None


def _household_decision_entry(period_summary: Any, decision: Any) -> TimelineEntry:
    decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "HOUSEHOLD_DECISION")
    reasons = list(_get(decision, "reasons") or [])
    summary = _get(reasons[0], "summary") if reasons else decision_type.replace("_", " ").title()
    return TimelineEntry(
        timestamp=str(_get(decision, "created_at") or _iso_date(_value(period_summary, "end"))),
        title=f"Recommendation: {decision_type.replace('_', ' ').title()}",
        summary=str(summary),
        financial_impact=_household_decision_financial_impact(decision),
        confidence=_display_confidence(_get(decision, "confidence")),
        evidence_sources=list(_get(decision, "data_sources_used") or ["Household data"]),
        event_type=f"household_decision_{decision_type.lower()}",
        decision_id=_get(decision, "decision_id"),
        decision_type=decision_type,
    )


def _household_decision_financial_impact(decision: Any) -> Optional[str]:
    impact = _get(decision, "financial_impact")
    if not isinstance(impact, dict):
        return None
    for key, label in (
        ("today", "today"),
        ("billing_period", "this billing period"),
        ("yearly", "per year"),
    ):
        value = impact.get(key)
        if value is not None:
            return f"{float(value):+,.2f} AUD {label}"
    return None


def _display_confidence(value: Any) -> str:
    text = str(value or "UNKNOWN").upper()
    return {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "UNKNOWN": "Low"}.get(text, "Low")


def _get(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    value = getattr(source, name, default)
    return getattr(value, "value", value)


def _value(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name)


def _has_value(source: Any, name: str) -> bool:
    if isinstance(source, dict):
        return name in source
    return hasattr(source, name)


def _iso_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _display_date(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)
