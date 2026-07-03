#!/usr/bin/env python3
"""Compatibility wrappers from Household Decisions to advisor explanations."""

from typing import Any, Optional

from .models import Advisor, EvidenceItem, EvidenceTrail
from .recommendation_adapter import RecommendationPresentation, adapt_recommendations


ADVISOR_TYPES_BY_CATEGORY = {
    "ev": "EV_CHARGING",
    "billing": "COST",
    "solar": "SOLAR",
    "monitoring": "EFFICIENCY",
    "anomaly": "EFFICIENCY",
    "setup": "EFFICIENCY",
    "data_quality": "EFFICIENCY",
    "general": "EFFICIENCY",
}

STATUS_BY_PRIORITY = {
    "critical": "WATCH",
    "high": "WATCH",
    "medium": "UNKNOWN",
    "low": "ON_TRACK",
}
STATUS_BY_CATEGORY = {
    "billing": "WATCH",
    "solar": "WAIT",
    "anomaly": "WATCH",
}
STATUS_BY_DECISION_TYPE = {
    "CHARGE_NOW": "CHARGE_NOW",
    "WAIT_FOR_CHEAPER_WINDOW": "WAIT",
    "WAIT_FOR_SOLAR": "WAIT",
    "NO_ACTION": "ON_TRACK",
}


def explain_household_decisions(
    decisions: list[Any],
    *,
    existing_advisors: Optional[list[Advisor]] = None,
) -> list[Advisor]:
    """Create advisor explanation cards from adapter recommendations."""

    presentations = adapt_recommendations(
        decisions,
        advisor_explanations=existing_advisors,
    )
    return [_advisor_from_presentation(item) for item in presentations]


def attach_decision_to_advisors(advisors: list[Advisor], decisions: list[Any]) -> list[Advisor]:
    """Attach nearest HouseholdDecision references to existing advisor cards.

    Deprecated compatibility wrapper: advisor cards may explain evidence, but
    user action text must come from DecisionPipeline/RecommendationAdapter.
    Remove once native advisor cards are fully replaced by advisor explanations.
    """

    presentations = adapt_recommendations(decisions, advisor_explanations=advisors)
    by_advisor_type: dict[str, list[RecommendationPresentation]] = {}
    for presentation in presentations:
        advisor_type = ADVISOR_TYPES_BY_CATEGORY.get(presentation.category.value, "EFFICIENCY")
        by_advisor_type.setdefault(advisor_type, []).append(presentation)

    migrated = []
    for advisor in advisors:
        candidates = by_advisor_type.get(advisor.advisor_type, [])
        if not candidates:
            migrated.append(_copy_advisor_without_action(advisor))
            continue
        presentation = sorted(candidates, key=lambda item: -item.display_priority)[0]
        migrated.append(_copy_advisor_with_presentation(advisor, presentation))
    return migrated


def _advisor_from_presentation(presentation: RecommendationPresentation) -> Advisor:
    advisor_type = ADVISOR_TYPES_BY_CATEGORY.get(presentation.category.value, "EFFICIENCY")
    decision_type = (presentation.decision or {}).get("decision_type")
    status = STATUS_BY_CATEGORY.get(
        presentation.category.value,
        STATUS_BY_PRIORITY.get(presentation.urgency.value, "WATCH"),
    )
    status = STATUS_BY_DECISION_TYPE.get(decision_type, status)
    return Advisor(
        advisor_id=f"decision_{presentation.decision_id.lower()}",
        advisor_type=advisor_type,
        status=status,
        headline=presentation.title,
        summary=presentation.summary,
        recommendation=presentation.recommended_action,
        estimated_financial_impact=presentation.financial_impact,
        confidence=presentation.confidence,
        confidence_reason=_confidence_reason(presentation.confidence),
        evidence_trail=EvidenceTrail([
            EvidenceItem(
                source=item.source,
                observation=f"{item.signal}: {item.value}",
                impact=presentation.rationale,
                confidence=presentation.confidence,
                freshness=item.observed_at,
            )
            for item in presentation.evidence[:5]
        ] or [EvidenceItem("Household data", presentation.title, presentation.rationale, presentation.confidence)]),
        freshness="Current household data",
        supporting_data_sources=list(presentation.data_sources_used),
        next_review_time=presentation.valid_until,
        decision_id=presentation.decision_id,
        decision_type=decision_type,
        decision=presentation.decision,
    )


def _copy_advisor_with_presentation(advisor: Advisor, presentation: RecommendationPresentation) -> Advisor:
    return Advisor(
        advisor_id=advisor.advisor_id,
        advisor_type=advisor.advisor_type,
        status=advisor.status,
        headline=advisor.headline,
        summary=advisor.summary,
        recommendation=presentation.recommended_action,
        estimated_financial_impact=advisor.estimated_financial_impact,
        confidence=presentation.confidence,
        confidence_reason=_confidence_reason(presentation.confidence),
        evidence_trail=advisor.evidence_trail,
        freshness=advisor.freshness,
        supporting_data_sources=advisor.supporting_data_sources,
        next_review_time=advisor.next_review_time,
        evidence_summary=advisor.evidence_summary,
        decision_id=presentation.decision_id,
        decision_type=(presentation.decision or {}).get("decision_type"),
        decision=presentation.decision,
    )


def _copy_advisor_without_action(advisor: Advisor) -> Advisor:
    return Advisor(
        advisor_id=advisor.advisor_id,
        advisor_type=advisor.advisor_type,
        status=advisor.status,
        headline=advisor.headline,
        summary=advisor.summary,
        recommendation="Advisor evidence only; use Decision Pipeline recommendations for actions.",
        estimated_financial_impact=advisor.estimated_financial_impact,
        confidence=advisor.confidence,
        confidence_reason=advisor.confidence_reason,
        evidence_trail=advisor.evidence_trail,
        freshness=advisor.freshness,
        supporting_data_sources=advisor.supporting_data_sources,
        next_review_time=advisor.next_review_time,
        evidence_summary=advisor.evidence_summary,
        decision_id=advisor.decision_id,
        decision_type=advisor.decision_type,
        decision=advisor.decision,
    )


def _confidence_reason(confidence: str) -> str:
    if str(confidence).lower() == "high":
        return "High confidence from the household data PowerMonitor can verify."
    if str(confidence).lower() == "medium":
        return "Some uncertainty remains; this is based on available readings and tariff timing."
    return "Low confidence: PowerMonitor cannot verify enough live household data yet."
