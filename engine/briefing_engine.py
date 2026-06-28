#!/usr/bin/env python3
"""Advisor orchestration for the PowerMonitor daily briefing."""

from dataclasses import dataclass
from datetime import date
import re
from typing import Optional

from .models import (
    Advisor,
    AdvisorCollection,
    BriefingPriority,
    BriefingSection,
    DailyBriefing,
    FinancialImpact,
    HouseholdContext,
)


class BriefingEngine:
    """Build a concise daily briefing from active advisors only."""

    name = "briefing_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def build(
        self,
        advisors: AdvisorCollection,
        briefing_date: Optional[date] = None,
        household_context: Optional[HouseholdContext] = None,
    ) -> DailyBriefing:
        active_household = household_context or self.household_context
        if active_household and advisors.household is None:
            advisors = AdvisorCollection(
                advisors.advisors,
                household=active_household.household,
                capability_summary=advisors.capability_summary,
            )
        ordered = advisors.active()
        priorities = _orchestrated_section(ordered, "priority")[:1]
        used_advisor_ids = _supporting_ids(priorities)
        positives = _orchestrated_section(ordered, "positive", used_advisor_ids)[:1]
        used_advisor_ids.update(_supporting_ids(positives))
        watch_items = _orchestrated_section(ordered, "watch", used_advisor_ids)[:1]
        summary = [_briefing_priority(item) for item in ordered]
        sections = [
            BriefingSection("Today's Best Action", "top_priorities", priorities),
            BriefingSection("You're doing well", "positive_outcomes", positives),
            BriefingSection("Watch This", "items_to_watch", watch_items),
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


@dataclass(frozen=True)
class _AdvisorCluster:
    theme: str
    advisors: list[Advisor]
    section_type: str


PRIORITY_STATUSES = {
    "AVOID",
    "HIGH",
    "INEFFICIENT",
    "CHARGE_NOW",
    "SLIGHTLY_HIGH",
    "MISSED_OPPORTUNITY",
}
POSITIVE_STATUSES = {"IMPROVING", "ON_TRACK", "EXCELLENT", "GOOD"}
WATCH_STATUSES = {"WAIT", "WATCH", "OPPORTUNITY", "UNKNOWN"}


def _orchestrated_section(
    advisors: list[Advisor],
    section_type: str,
    exclude_ids: Optional[set[str]] = None,
) -> list[BriefingPriority]:
    exclude_ids = exclude_ids or set()
    section_advisors = [
        advisor for advisor in advisors
        if _section_for_status(advisor.status) == section_type and advisor.advisor_id not in exclude_ids
    ]
    clusters = _cluster_advisors(section_advisors, section_type, advisors, exclude_ids)
    ordered = sorted(clusters, key=_cluster_sort_key)
    return [_cluster_priority(cluster) for cluster in ordered]


def _section_for_status(status: str) -> Optional[str]:
    if status in PRIORITY_STATUSES:
        return "priority"
    if status in POSITIVE_STATUSES:
        return "positive"
    if status in WATCH_STATUSES:
        return "watch"
    return None


def _cluster_advisors(
    advisors: list[Advisor],
    section_type: str,
    all_advisors: list[Advisor],
    exclude_ids: set[str],
) -> list[_AdvisorCluster]:
    by_theme: dict[str, list[Advisor]] = {}
    for advisor in advisors:
        by_theme.setdefault(_advisor_theme(advisor), []).append(advisor)
    if section_type == "priority":
        for theme, items in list(by_theme.items()):
            item_ids = {item.advisor_id for item in items}
            supporters = [
                advisor for advisor in all_advisors
                if advisor.advisor_id not in item_ids
                and advisor.advisor_id not in exclude_ids
                and _advisor_theme(advisor) == theme
            ]
            items.extend(supporters)
    return [
        _AdvisorCluster(theme=theme, advisors=items, section_type=section_type)
        for theme, items in by_theme.items()
    ]


def _advisor_theme(advisor: Advisor) -> str:
    text = " ".join([advisor.advisor_type, advisor.headline, advisor.summary, advisor.recommendation]).lower()
    if advisor.advisor_type == "EV_CHARGING" or re.search(r"\bev\b", text) or "charg" in text:
        return "ev_charging"
    if advisor.advisor_type == "SOLAR" or "solar" in text or "export" in text:
        return "solar_flexibility"
    if advisor.advisor_type == "EFFICIENCY" and any(word in text for word in ("daytime", "flexible", "import")):
        return "solar_flexibility"
    if advisor.advisor_type == "EFFICIENCY" or "baseload" in text or "efficient" in text:
        return "efficiency"
    if advisor.advisor_type == "COST" or "bill" in text or "cost" in text or "tariff" in text:
        return "cost_control"
    return advisor.advisor_type.lower()


def _cluster_sort_key(cluster: _AdvisorCluster) -> tuple[float, str]:
    score = sum(_advisor_score(advisor) for advisor in cluster.advisors)
    if len(cluster.advisors) > 1:
        score += 18.0
    return (-score, cluster.theme)


def _advisor_score(advisor: Advisor) -> float:
    urgency = {
        "AVOID": 40.0,
        "HIGH": 40.0,
        "INEFFICIENT": 38.0,
        "CHARGE_NOW": 35.0,
        "SLIGHTLY_HIGH": 30.0,
        "MISSED_OPPORTUNITY": 30.0,
        "WATCH": 22.0,
        "OPPORTUNITY": 20.0,
        "WAIT": 18.0,
        "UNKNOWN": 8.0,
        "IMPROVING": 16.0,
        "ON_TRACK": 15.0,
        "GOOD": 14.0,
        "EXCELLENT": 14.0,
    }.get(advisor.status, 10.0)
    confidence = {"Very High": 18.0, "High": 14.0, "Medium": 8.0, "Low": 2.0}.get(advisor.confidence, 2.0)
    impact = min(_impact_value(advisor.estimated_financial_impact), 50.0)
    effort = _effort_score(advisor.recommendation)
    return urgency + confidence + impact + effort


def _effort_score(recommendation: str) -> float:
    text = recommendation.lower()
    if any(word in text for word in ("keep", "stay the course", "review again")):
        return 4.0
    if any(word in text for word in ("review", "watch", "check")):
        return 8.0
    if any(word in text for word in ("shift", "move", "charge", "prioritise", "run during")):
        return 12.0
    return 6.0


def _cluster_priority(cluster: _AdvisorCluster) -> BriefingPriority:
    primary = sorted(cluster.advisors, key=lambda item: _advisor_score(item), reverse=True)[0]
    if len(cluster.advisors) == 1:
        return _briefing_priority(primary)

    support = [_advisor_label(item) for item in cluster.advisors]
    impact = _combined_impact(cluster.advisors)
    confidence = _combined_confidence(cluster.advisors)
    freshness = _combined_freshness(cluster.advisors)
    title = _cluster_title(cluster, primary)
    recommendation = _cluster_recommendation(cluster, primary)
    summary = _cluster_summary(cluster, primary, support)
    return BriefingPriority(
        advisor_id="+".join(sorted(item.advisor_id for item in cluster.advisors)),
        advisor_type="HOUSEHOLD_ACTION",
        title=title,
        summary=summary,
        recommendation=recommendation,
        status=primary.status,
        confidence=confidence,
        confidence_reason=_combined_confidence_reason(cluster.advisors, confidence),
        freshness=freshness,
        estimated_financial_impact=impact,
        supporting_advisors=support,
    )


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
        supporting_advisors=[_advisor_label(advisor)],
    )


def _supporting_ids(items: list[BriefingPriority]) -> set[str]:
    advisor_ids: set[str] = set()
    for item in items:
        advisor_ids.update(part for part in item.advisor_id.split("+") if part)
    return advisor_ids


def _cluster_title(cluster: _AdvisorCluster, primary: Advisor) -> str:
    if cluster.section_type == "positive":
        return primary.headline
    if cluster.theme == "ev_charging":
        return primary.recommendation
    if cluster.theme == "solar_flexibility":
        return "Shift flexible loads into your solar window."
    if cluster.theme == "efficiency":
        return primary.headline
    if cluster.theme == "cost_control":
        return primary.recommendation
    return primary.headline


def _cluster_recommendation(cluster: _AdvisorCluster, primary: Advisor) -> str:
    if cluster.theme == "solar_flexibility" and cluster.section_type == "priority":
        return "Shift flexible loads into sunny periods where practical."
    return primary.recommendation


def _cluster_summary(cluster: _AdvisorCluster, primary: Advisor, support: list[str]) -> str:
    if cluster.section_type == "positive":
        return f"{primary.summary} Supported by {', '.join(support)}."
    if cluster.theme == "ev_charging":
        return f"{primary.summary} This action is supported by {', '.join(support)}."
    if cluster.theme == "solar_flexibility":
        return f"{primary.summary} Solar and efficiency signals point to the same household action."
    return f"{primary.summary} Supported by {', '.join(support)}."


def _advisor_label(advisor: Advisor) -> str:
    return advisor.advisor_type.replace("_", " ").title()


def _combined_impact(advisors: list[Advisor]) -> Optional[FinancialImpact]:
    impacts = [advisor.estimated_financial_impact for advisor in advisors if advisor.estimated_financial_impact]
    if not impacts:
        return None
    today = sum(float(item.today or 0.0) for item in impacts)
    billing = sum(float(item.billing_period or 0.0) for item in impacts)
    yearly = sum(float(item.yearly or 0.0) for item in impacts)
    return FinancialImpact(
        today=round(today, 2) if today else None,
        billing_period=round(billing, 2) if billing else None,
        yearly=round(yearly, 2) if yearly else None,
        basis="Combined measurable impact from supporting advisors",
    )


def _combined_confidence(advisors: list[Advisor]) -> str:
    rank = {"Low": 0, "Medium": 1, "High": 2, "Very High": 3}
    return min((advisor.confidence for advisor in advisors), key=lambda value: rank.get(value, 0))


def _combined_confidence_reason(advisors: list[Advisor], confidence: str) -> str:
    labels = ", ".join(_advisor_label(advisor) for advisor in advisors)
    return f"{confidence} confidence because the merged household action uses the lowest confidence across: {labels}."


def _combined_freshness(advisors: list[Advisor]) -> str:
    fresh = [advisor.freshness for advisor in advisors if advisor.freshness]
    if not fresh:
        return "Unknown"
    return fresh[0] if len(set(fresh)) == 1 else "Mixed freshness"


def _overall_status(advisors: list[Advisor]) -> str:
    if not advisors:
        return "No active advisor signals are available today."
    statuses = {item.status for item in advisors}
    if statuses & {"AVOID", "HIGH", "INEFFICIENT"}:
        return "There is one household item worth acting on first."
    if statuses & {"CHARGE_NOW", "SLIGHTLY_HIGH", "MISSED_OPPORTUNITY"}:
        return "There is a timely opportunity to review today."
    if statuses & {"IMPROVING", "ON_TRACK", "EXCELLENT", "GOOD"}:
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


def _impact_value(impact: Optional[FinancialImpact]) -> float:
    if not impact:
        return 0.0
    values = [value for value in impact.periods_dict().values() if value is not None]
    return max((abs(float(value)) for value in values), default=0.0)
