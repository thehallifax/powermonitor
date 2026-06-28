#!/usr/bin/env python3
"""Household goal progress derived from active advisor outputs."""

from typing import Optional

from .models import Advisor, AdvisorCollection, Goal, GoalProgress, GoalStatus, HouseholdContext


class GoalsEngine:
    """Tracks household goals using AdvisorCollection evidence only."""

    name = "goals_engine"

    def __init__(
        self,
        goals: Optional[list[Goal]] = None,
        household_context: Optional[HouseholdContext] = None,
    ) -> None:
        self.goals = goals or default_goals()
        self.household_context = household_context

    def evaluate(
        self,
        advisors: AdvisorCollection,
        household_context: Optional[HouseholdContext] = None,
    ) -> list[GoalProgress]:
        active_household = household_context or self.household_context
        if active_household and advisors.household is None:
            advisors = AdvisorCollection(
                advisors.advisors,
                household=active_household.household,
                capability_summary=advisors.capability_summary,
            )
        ordered = advisors.active()
        return [
            self._progress_for_goal(goal, ordered)
            for goal in self.goals
            if goal.active
        ]

    def goal_titles_for_recommendation_category(self, category: str) -> list[str]:
        normalized = category.replace("_", " ").lower()
        return [
            goal.title
            for goal in self.goals
            if goal.active and any(item.replace("_", " ").lower() == normalized for item in goal.recommendation_categories)
        ]

    def _progress_for_goal(self, goal: Goal, advisors: list[Advisor]) -> GoalProgress:
        supporting = [
            advisor for advisor in advisors
            if advisor.advisor_type in goal.advisor_types
        ]
        if not supporting:
            return _unknown_progress(goal)

        scored = [
            (_goal_score(goal.goal_id, advisor), advisor)
            for advisor in supporting
        ]
        known = [(score, advisor) for score, advisor in scored if score is not None]
        if not known:
            return _unknown_progress(goal, supporting)

        progress = int(round(sum(score for score, _advisor in known) / len(known)))
        status = _goal_status(progress)
        confidence = _lowest_confidence([advisor for _score, advisor in known])
        lead = sorted(known, key=lambda item: (item[0], item[1].advisor_id))[0][1]
        evidence = _evidence_summary([advisor for _score, advisor in known])
        return GoalProgress(
            goal=goal,
            progress_pct=progress,
            status=status,
            confidence=confidence,
            summary=_goal_summary(goal, progress, lead),
            supporting_advisors=[_advisor_label(advisor) for _score, advisor in known],
            supporting_evidence=evidence,
            recommended_action=lead.recommendation,
        )


def default_goals() -> list[Goal]:
    return [
        Goal(
            goal_id="bill_target",
            title="Bill Target",
            outcome="Stay under billing target",
            advisor_types=["COST"],
            recommendation_categories=["billing", "tariff"],
        ),
        Goal(
            goal_id="peak_reduction",
            title="Peak Reduction",
            outcome="Reduce Peak usage",
            advisor_types=["COST", "EV_CHARGING"],
            recommendation_categories=["billing", "tariff", "EV"],
        ),
        Goal(
            goal_id="solar_self_consumption",
            title="Solar Usage",
            outcome="Increase solar self-consumption",
            advisor_types=["SOLAR"],
            recommendation_categories=["solar"],
        ),
        Goal(
            goal_id="off_peak_ev_charging",
            title="Off-Peak EV Charging",
            outcome="Increase off-peak EV charging",
            advisor_types=["EV_CHARGING"],
            recommendation_categories=["EV", "tariff"],
        ),
        Goal(
            goal_id="overnight_baseload",
            title="Overnight Baseload",
            outcome="Reduce overnight baseload",
            advisor_types=["EFFICIENCY"],
            recommendation_categories=["baseload", "data_quality"],
        ),
    ]


def _goal_score(goal_id: str, advisor: Advisor) -> Optional[int]:
    scoring = {
        "bill_target": {
            "IMPROVING": 100,
            "ON_TRACK": 90,
            "SLIGHTLY_HIGH": 65,
            "HIGH": 35,
            "UNKNOWN": None,
        },
        "peak_reduction": {
            "IMPROVING": 95,
            "ON_TRACK": 85,
            "CHARGE_NOW": 85,
            "WAIT": 75,
            "SLIGHTLY_HIGH": 60,
            "AVOID": 45,
            "HIGH": 35,
            "UNKNOWN": None,
        },
        "solar_self_consumption": {
            "EXCELLENT": 100,
            "GOOD": 85,
            "OPPORTUNITY": 62,
            "MISSED_OPPORTUNITY": 42,
            "UNKNOWN": None,
        },
        "off_peak_ev_charging": {
            "CHARGE_NOW": 92,
            "WAIT": 78,
            "AVOID": 45,
            "UNKNOWN": None,
        },
        "overnight_baseload": {
            "EXCELLENT": 100,
            "GOOD": 85,
            "WATCH": 62,
            "INEFFICIENT": 35,
            "UNKNOWN": None,
        },
    }
    return scoring.get(goal_id, {}).get(advisor.status)


def _goal_status(progress: int) -> GoalStatus:
    if progress >= 95:
        return GoalStatus("AHEAD", "Ahead", "Goal is comfortably ahead based on current advisor evidence.")
    if progress >= 75:
        return GoalStatus("ON_TRACK", "On track", "Goal is tracking well based on current advisor evidence.")
    if progress >= 55:
        return GoalStatus("WATCH", "Watch", "Goal is partly on track but has a visible risk signal.")
    return GoalStatus("NEEDS_ATTENTION", "Needs attention", "Goal is behind based on current advisor evidence.")


def _unknown_progress(goal: Goal, advisors: Optional[list[Advisor]] = None) -> GoalProgress:
    supporting = advisors or []
    return GoalProgress(
        goal=goal,
        progress_pct=None,
        status=GoalStatus("UNKNOWN", "Unknown", "PowerMonitor needs more advisor evidence for this goal."),
        confidence="Low",
        summary=f"{goal.title} needs more advisor evidence.",
        supporting_advisors=[_advisor_label(advisor) for advisor in supporting],
        supporting_evidence=_evidence_summary(supporting),
        recommended_action="Review again after more advisor data is available.",
    )


def _goal_summary(goal: Goal, progress: int, advisor: Advisor) -> str:
    return f"{goal.outcome}: {progress}% progress based on {advisor.advisor_type.replace('_', ' ').title()} Advisor evidence."


def _lowest_confidence(advisors: list[Advisor]) -> str:
    rank = {"Low": 0, "Medium": 1, "High": 2, "Very High": 3}
    return min((advisor.confidence for advisor in advisors), key=lambda value: rank.get(value, 0))


def _evidence_summary(advisors: list[Advisor]) -> list[str]:
    evidence: list[str] = []
    for advisor in advisors:
        if advisor.headline:
            evidence.append(advisor.headline)
        for item in advisor.evidence_trail.items[:2]:
            evidence.append(f"{item.source}: {item.observation}")
    return evidence[:6]


def _advisor_label(advisor: Advisor) -> str:
    return advisor.advisor_type.replace("_", " ").title()
