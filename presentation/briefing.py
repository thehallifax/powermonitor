#!/usr/bin/env python3
"""Daily Briefing presentation model for Decision Engine output."""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable, Optional

from engine.models import Decision, DecisionExplanation


PRIORITY_RANK = {
    "high": 0,
    "medium": 1,
    "low": 2,
}


@dataclass(frozen=True)
class BriefingExplanation:
    """Presentation-ready explanation for a briefing item."""

    why_this_matters: str
    evidence_used: list[str]
    financial_impact: str
    confidence_reason: str
    data_sources_used: list[str]
    freshness_completeness_note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BriefingItem:
    """A presentation-ready briefing entry derived from one decision."""

    title: str
    body: str
    confidence: str
    category: str
    priority: str
    severity: str
    financial_impact: str
    data_sources_used: list[str]
    decision_id: Optional[str] = None
    explanation: Optional[BriefingExplanation] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DailyBriefing:
    """The Home-page narrative assembled from existing Decision objects."""

    greeting: str
    headline: str
    introduction: str
    todays_priority: Optional[BriefingItem]
    todays_win: Optional[BriefingItem]
    opportunities: list[BriefingItem]
    read_time_minutes: int
    confidence: str
    data_sources_used: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "greeting": self.greeting,
            "headline": self.headline,
            "introduction": self.introduction,
            "todays_priority": (
                self.todays_priority.as_dict() if self.todays_priority else None
            ),
            "todays_win": self.todays_win.as_dict() if self.todays_win else None,
            "opportunities": [item.as_dict() for item in self.opportunities],
            "read_time_minutes": self.read_time_minutes,
            "confidence": self.confidence,
            "data_sources_used": self.data_sources_used,
        }


def build_daily_briefing(
    decisions: Iterable[Decision],
    briefing_date: Optional[date] = None,
) -> DailyBriefing:
    """Build a calm Home briefing from Decision Engine output."""

    decision_list = list(decisions)
    sorted_decisions = sorted(
        decision_list,
        key=lambda item: (PRIORITY_RANK.get(item.priority.lower(), 1), item.title),
    )
    priority = _select_priority(sorted_decisions)
    win = _select_win(decision_list, priority)
    opportunities = _select_opportunities(sorted_decisions, priority, win)

    greeting = _greeting(briefing_date or date.today())
    headline = _headline(priority, win)
    introduction = _introduction(priority, win, opportunities)
    confidence = _overall_confidence(decision_list)
    data_sources = _data_sources(decision_list)

    briefing = DailyBriefing(
        greeting=greeting,
        headline=headline,
        introduction=introduction,
        todays_priority=_briefing_item(priority) if priority else None,
        todays_win=_briefing_item(win) if win else None,
        opportunities=[_briefing_item(item) for item in opportunities],
        read_time_minutes=1,
        confidence=confidence,
        data_sources_used=data_sources,
    )
    return DailyBriefing(
        **{
            **briefing.as_dict(),
            "todays_priority": briefing.todays_priority,
            "todays_win": briefing.todays_win,
            "opportunities": briefing.opportunities,
            "read_time_minutes": _read_time_minutes(briefing),
        }
    )


def _select_priority(decisions: list[Decision]) -> Optional[Decision]:
    return next(
        (item for item in decisions if item.action_type != "maintain"),
        decisions[0] if decisions else None,
    )


def _select_win(
    decisions: list[Decision],
    priority: Optional[Decision],
) -> Optional[Decision]:
    candidates = [
        item
        for item in decisions
        if item is not priority
        and (item.action_type == "maintain" or item.id == "positive_reinforcement")
    ]
    if candidates:
        return sorted(
            candidates,
            key=lambda item: (item.id != "positive_reinforcement", item.title),
        )[0]
    return next((item for item in decisions if item is not priority and item.priority == "Low"), None)


def _select_opportunities(
    decisions: list[Decision],
    priority: Optional[Decision],
    win: Optional[Decision],
) -> list[Decision]:
    excluded = {id(item) for item in (priority, win) if item}
    return [item for item in decisions if id(item) not in excluded][:3]


def _greeting(briefing_date: date) -> str:
    weekday = briefing_date.strftime("%A")
    return f"Good morning. Here is your {weekday} energy briefing."


def _headline(
    priority: Optional[Decision],
    win: Optional[Decision],
) -> str:
    if priority:
        return f"Today's priority is {priority.title.lower()}."
    if win:
        return "There is no urgent action today."
    return "No active decisions need attention today."


def _introduction(
    priority: Optional[Decision],
    win: Optional[Decision],
    opportunities: list[Decision],
) -> str:
    if priority and win:
        return (
            "One item is worth acting on first, and there is also a useful "
            "positive signal to keep in place."
        )
    if priority:
        return "Start with the priority below, then review the smaller opportunities."
    if opportunities:
        return "Nothing looks urgent, but there are a few useful checks available."
    return "The available decisions do not indicate anything that needs action right now."


def _briefing_item(decision: Decision) -> BriefingItem:
    return BriefingItem(
        decision_id=decision.id,
        title=decision.title,
        body=_natural_body(decision),
        confidence=decision.confidence,
        category=decision.category,
        priority=decision.priority,
        severity=decision.severity,
        financial_impact=_financial_impact_text(decision),
        data_sources_used=list(decision.data_sources_used),
        explanation=_briefing_explanation(decision),
    )


def _natural_body(decision: Decision) -> str:
    return (
        f"{decision.finding} {decision.recommendation} "
        f"Confidence is {decision.confidence.lower()}."
    )


def _financial_impact_text(decision: Decision) -> str:
    impact = decision.financial_impact
    for key, label in (
        ("today", "today"),
        ("billing_period", "this billing period"),
        ("yearly", "per year"),
    ):
        value = impact.get(key)
        if value is not None:
            return f"${float(value):,.2f} {label}"
    return "Not quantified"


def _briefing_explanation(decision: Decision) -> BriefingExplanation:
    explanation = decision.explanation or _fallback_decision_explanation(decision)
    return BriefingExplanation(
        why_this_matters=explanation.why_this_matters,
        evidence_used=list(explanation.evidence_used),
        financial_impact=explanation.financial_impact,
        confidence_reason=explanation.confidence_reason,
        data_sources_used=list(explanation.data_sources_used),
        freshness_completeness_note=explanation.freshness_completeness_note,
    )


def _fallback_decision_explanation(decision: Decision) -> DecisionExplanation:
    return DecisionExplanation(
        why_this_matters=decision.finding,
        evidence_used=[
            f"{key.replace('_', ' ').capitalize()}: {value}"
            for key, value in decision.evidence.items()
        ] or [decision.finding],
        financial_impact=_financial_impact_text(decision),
        confidence_reason=f"{decision.confidence} confidence from Decision Engine output.",
        data_sources_used=list(decision.data_sources_used),
        freshness_completeness_note=(
            f"Decision expires at {decision.expires_at}." if decision.expires_at else None
        ),
    )


def _overall_confidence(decisions: list[Decision]) -> str:
    levels = [item.confidence for item in decisions]
    if "Low" in levels:
        return "Low"
    if "Medium" in levels:
        return "Medium"
    if levels:
        return "High"
    return "Unknown"


def _data_sources(decisions: list[Decision]) -> list[str]:
    sources = []
    for decision in decisions:
        sources.extend(decision.data_sources_used)
    return list(dict.fromkeys(sources))


def _read_time_minutes(briefing: DailyBriefing) -> int:
    words = " ".join(
        [
            briefing.greeting,
            briefing.headline,
            briefing.introduction,
            briefing.todays_priority.body if briefing.todays_priority else "",
            briefing.todays_win.body if briefing.todays_win else "",
            " ".join(item.body for item in briefing.opportunities),
        ]
    ).split()
    return max(1, round(len(words) / 180))
