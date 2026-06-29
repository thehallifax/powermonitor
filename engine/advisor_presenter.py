#!/usr/bin/env python3
"""Presentation adapters from Household Decisions to advisor explanations."""

from typing import Any, Optional

from .models import Advisor, EvidenceItem, EvidenceTrail, FinancialImpact


ADVISOR_TYPES_BY_DECISION = {
    "CHARGE_NOW": "EV_CHARGING",
    "WAIT_FOR_CHEAPER_WINDOW": "EV_CHARGING",
    "WAIT_FOR_SOLAR": "EV_CHARGING",
    "REVIEW_BILLING": "COST",
    "MONITOR_USAGE": "EFFICIENCY",
    "INVESTIGATE_ANOMALY": "EFFICIENCY",
    "REVIEW_PROVIDER": "EFFICIENCY",
    "REVIEW_SETUP": "EFFICIENCY",
    "DATA_INSUFFICIENT": "EFFICIENCY",
    "NO_ACTION": "EFFICIENCY",
}

STATUS_BY_DECISION = {
    "CHARGE_NOW": "CHARGE_NOW",
    "WAIT_FOR_CHEAPER_WINDOW": "WAIT",
    "WAIT_FOR_SOLAR": "WAIT",
    "REVIEW_BILLING": "WATCH",
    "MONITOR_USAGE": "WATCH",
    "INVESTIGATE_ANOMALY": "WATCH",
    "REVIEW_PROVIDER": "UNKNOWN",
    "REVIEW_SETUP": "UNKNOWN",
    "DATA_INSUFFICIENT": "UNKNOWN",
    "NO_ACTION": "ON_TRACK",
}

HEADLINES_BY_DECISION = {
    "CHARGE_NOW": "Charging window is favourable",
    "WAIT_FOR_CHEAPER_WINDOW": "Wait for a cheaper charging window",
    "WAIT_FOR_SOLAR": "Wait for stronger solar evidence",
    "REVIEW_BILLING": "Review current bill trajectory",
    "MONITOR_USAGE": "Monitor current household usage",
    "INVESTIGATE_ANOMALY": "Investigate unusual household behaviour",
    "REVIEW_PROVIDER": "Review provider health",
    "REVIEW_SETUP": "Finish required setup",
    "DATA_INSUFFICIENT": "Evidence is not strong enough yet",
    "NO_ACTION": "No immediate household action",
}


def explain_household_decisions(
    decisions: list[Any],
    *,
    existing_advisors: Optional[list[Advisor]] = None,
) -> list[Advisor]:
    """Create advisor explanation cards from HouseholdDecision objects.

    Existing advisors are used only as wording fallbacks where they already
    explain the same advisor domain. The chosen action, priority, confidence,
    and evidence remain owned by the HouseholdDecision.
    """

    existing_by_type = {
        advisor.advisor_type: advisor
        for advisor in existing_advisors or []
    }
    advisors = []
    for decision in decisions:
        advisor_type = ADVISOR_TYPES_BY_DECISION.get(_get(decision, "decision_type") or _get(decision, "type"), "EFFICIENCY")
        fallback = existing_by_type.get(advisor_type)
        advisors.append(_advisor_from_decision(decision, advisor_type, fallback))
    return advisors


def attach_decision_to_advisors(advisors: list[Advisor], decisions: list[Any]) -> list[Advisor]:
    """Attach nearest HouseholdDecision references to existing advisor cards."""

    by_type = _decisions_by_advisor_type(decisions)
    migrated = []
    for advisor in advisors:
        decision = _best_decision_for_advisor(advisor, by_type)
        if not decision:
            migrated.append(advisor)
            continue
        migrated.append(_copy_advisor_with_decision(advisor, decision))
    return migrated


def _advisor_from_decision(decision: Any, advisor_type: str, fallback: Optional[Advisor]) -> Advisor:
    decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "NO_ACTION")
    confidence = _display_confidence(_get(decision, "confidence"))
    impact = _financial_impact(_get(decision, "financial_impact"))
    reasons = list(_get(decision, "reasons") or [])
    primary_reason = _get(reasons[0], "summary") if reasons else None
    outcome = _get(decision, "outcome")
    recommendation = _get(outcome, "expected_effect") or primary_reason or "Continue monitoring."
    headline = HEADLINES_BY_DECISION.get(decision_type) or (fallback.headline if fallback else decision_type.replace("_", " ").title())
    status = STATUS_BY_DECISION.get(decision_type, "WATCH")
    evidence = [
        EvidenceItem(
            source=str(_get(item, "source") or "DecisionEngine"),
            observation=f"{_get(item, 'signal') or 'decision'}: {_get(item, 'value')}",
            impact=str(primary_reason or recommendation),
            confidence=_display_confidence(_get(item, "quality") or confidence),
            freshness=_get(item, "observed_at"),
        )
        for item in list(_get(decision, "evidence") or [])[:5]
    ]
    if not evidence:
        evidence = [EvidenceItem("DecisionEngine", decision_type, recommendation, confidence)]
    decision_payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
    return Advisor(
        advisor_id=f"decision_{str(_get(decision, 'decision_id') or decision_type).lower()}",
        advisor_type=advisor_type,
        status=status,
        headline=headline,
        summary=primary_reason or recommendation,
        recommendation=recommendation,
        estimated_financial_impact=impact,
        confidence=confidence,
        confidence_reason=_confidence_reason(decision, primary_reason),
        evidence_trail=EvidenceTrail(evidence),
        freshness="DecisionEngine",
        supporting_data_sources=list(_get(decision, "data_sources_used") or ["DecisionEngine"]),
        next_review_time=_get(decision, "valid_until"),
        decision_id=_get(decision, "decision_id"),
        decision_type=decision_type,
        decision=decision_payload,
    )


def _copy_advisor_with_decision(advisor: Advisor, decision: Any) -> Advisor:
    decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "")
    confidence = _display_confidence(_get(decision, "confidence") or advisor.confidence)
    decision_payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
    return Advisor(
        advisor_id=advisor.advisor_id,
        advisor_type=advisor.advisor_type,
        status=STATUS_BY_DECISION.get(decision_type, advisor.status),
        headline=advisor.headline,
        summary=advisor.summary,
        recommendation=advisor.recommendation,
        estimated_financial_impact=advisor.estimated_financial_impact,
        confidence=confidence,
        confidence_reason=_confidence_reason(decision, advisor.confidence_reason),
        evidence_trail=advisor.evidence_trail,
        freshness=advisor.freshness,
        supporting_data_sources=advisor.supporting_data_sources,
        next_review_time=advisor.next_review_time,
        evidence_summary=advisor.evidence_summary,
        decision_id=_get(decision, "decision_id"),
        decision_type=decision_type,
        decision=decision_payload,
    )


def _decisions_by_advisor_type(decisions: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for decision in decisions:
        decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "")
        advisor_type = ADVISOR_TYPES_BY_DECISION.get(decision_type, "EFFICIENCY")
        result.setdefault(advisor_type, []).append(decision)
    return result


def _best_decision_for_advisor(advisor: Advisor, by_type: dict[str, list[Any]]) -> Optional[Any]:
    candidates = by_type.get(advisor.advisor_type, [])
    if not candidates:
        return None
    return sorted(candidates, key=_decision_sort_key)[0]


def _decision_sort_key(decision: Any) -> tuple[int, int, str]:
    priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(_get(decision, "priority") or "").lower(), 4)
    confidence = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}.get(str(_get(decision, "confidence") or ""), 4)
    return (priority, confidence, str(_get(decision, "decision_id") or ""))


def _financial_impact(value: Any) -> Optional[FinancialImpact]:
    if isinstance(value, FinancialImpact):
        return value
    if not isinstance(value, dict):
        return None
    return FinancialImpact(
        today=value.get("today"),
        billing_period=value.get("billing_period"),
        yearly=value.get("yearly"),
        currency=value.get("currency", "AUD"),
        basis=value.get("basis"),
    )


def _display_confidence(value: Any) -> str:
    text = str(value or "UNKNOWN").replace("_", " ").title()
    return "High" if text == "High" else "Medium" if text == "Medium" else "Low" if text == "Low" else "Low"


def _confidence_reason(decision: Any, fallback: Optional[str]) -> str:
    decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "decision")
    confidence = _display_confidence(_get(decision, "confidence"))
    return f"{confidence} confidence from Decision Engine {decision_type} reasoning. {fallback or ''}".strip()


def _get(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    value = getattr(source, key, default)
    return getattr(value, "value", value)
