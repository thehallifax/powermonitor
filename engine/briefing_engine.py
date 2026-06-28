#!/usr/bin/env python3
"""Advisor orchestration for the PowerMonitor daily briefing."""

from datetime import date
from typing import Optional

from .models import (
    Advisor,
    AdvisorCollection,
    BriefingPriority,
    BriefingSection,
    DailyBriefing,
    FinancialImpact,
)


class BriefingEngine:
    """Build a concise daily briefing from active advisors only."""

    name = "briefing_engine"

    def build(
        self,
        advisors: AdvisorCollection,
        briefing_date: Optional[date] = None,
    ) -> DailyBriefing:
        ordered = advisors.active()
        priorities = [_briefing_priority(item) for item in _priority_advisors(ordered)[:3]]
        positives = [_briefing_priority(item) for item in _positive_advisors(ordered)[:2]]
        watch_items = [_briefing_priority(item) for item in _watch_advisors(ordered)[:2]]
        summary = [_briefing_priority(item) for item in ordered]
        sections = [
            BriefingSection("Top priorities", "top_priorities", priorities),
            BriefingSection("You're doing well", "positive_outcomes", positives),
            BriefingSection("Items to watch", "items_to_watch", watch_items),
        ]
        return DailyBriefing(
            greeting=_greeting(briefing_date or date.today()),
            overall_household_status=_overall_status(ordered),
            overall_confidence=_overall_confidence(ordered),
            overall_freshness=_overall_freshness(ordered),
            top_priorities=priorities,
            positive_outcomes=positives,
            items_to_watch=watch_items,
            suggested_next_action=_suggested_action(priorities, watch_items, positives),
            estimated_savings_opportunity=_estimated_savings(ordered),
            advisor_summary=summary,
            read_time_minutes=_read_time_minutes(priorities, positives, watch_items),
            sections=sections,
        )


def _priority_advisors(advisors: list[Advisor]) -> list[Advisor]:
    return [
        item for item in advisors
        if item.status in {"AVOID", "HIGH", "CHARGE_NOW", "SLIGHTLY_HIGH"}
    ]


def _positive_advisors(advisors: list[Advisor]) -> list[Advisor]:
    return [
        item for item in advisors
        if item.status in {"IMPROVING", "ON_TRACK"}
    ]


def _watch_advisors(advisors: list[Advisor]) -> list[Advisor]:
    return [
        item for item in advisors
        if item.status in {"WAIT", "UNKNOWN"}
    ]


def _briefing_priority(advisor: Advisor) -> BriefingPriority:
    return BriefingPriority(
        advisor_id=advisor.advisor_id,
        advisor_type=advisor.advisor_type,
        title=advisor.headline,
        summary=advisor.summary,
        recommendation=advisor.recommendation,
        status=advisor.status,
        confidence=advisor.confidence,
        confidence_reason=advisor.confidence_reason,
        freshness=advisor.freshness,
        estimated_financial_impact=advisor.estimated_financial_impact,
    )


def _overall_status(advisors: list[Advisor]) -> str:
    if not advisors:
        return "No active advisor signals are available today."
    statuses = {item.status for item in advisors}
    if statuses & {"AVOID", "HIGH"}:
        return "There is one household item worth acting on first."
    if statuses & {"CHARGE_NOW", "SLIGHTLY_HIGH"}:
        return "There is a timely opportunity to review today."
    if statuses & {"IMPROVING", "ON_TRACK"}:
        return "The household is broadly on track today."
    return "PowerMonitor needs fresher data for a confident briefing."


def _overall_confidence(advisors: list[Advisor]) -> str:
    if not advisors:
        return "Low"
    rank = {"Low": 0, "Medium": 1, "High": 2, "Very High": 3}
    return min((item.confidence for item in advisors), key=lambda value: rank.get(value, 0))


def _overall_freshness(advisors: list[Advisor]) -> str:
    if not advisors:
        return "Unknown"
    fresh = [item.freshness for item in advisors if item.freshness]
    return fresh[0] if len(set(fresh)) == 1 else "Mixed freshness"


def _suggested_action(
    priorities: list[BriefingPriority],
    watch_items: list[BriefingPriority],
    positives: list[BriefingPriority],
) -> str:
    if priorities:
        return priorities[0].recommendation
    if watch_items:
        return watch_items[0].recommendation
    if positives:
        return positives[0].recommendation
    return "No action is needed until more advisor data is available."


def _estimated_savings(advisors: list[Advisor]) -> Optional[FinancialImpact]:
    measurable = []
    for advisor in advisors:
        impact = advisor.estimated_financial_impact
        if not impact:
            continue
        values = impact.periods_dict()
        if any(value is not None for value in values.values()):
            measurable.append(impact)
    if not measurable:
        return None
    today = sum(float(item.today or 0.0) for item in measurable)
    billing = sum(float(item.billing_period or 0.0) for item in measurable)
    yearly = sum(float(item.yearly or 0.0) for item in measurable)
    return FinancialImpact(
        today=today if today else None,
        billing_period=billing if billing else None,
        yearly=yearly if yearly else None,
        basis="Sum of measurable active advisor financial impacts",
    )


def _read_time_minutes(
    priorities: list[BriefingPriority],
    positives: list[BriefingPriority],
    watch_items: list[BriefingPriority],
) -> int:
    item_count = len(priorities) + len(positives) + len(watch_items)
    return max(1, min(3, 1 + item_count // 4))


def _greeting(briefing_date: date) -> str:
    return f"Good morning. Here is your {briefing_date.strftime('%A')} PowerMonitor briefing."
