#!/usr/bin/env python3
"""Canonical HouseholdDecision to RecommendationPresentation pipeline."""

from dataclasses import dataclass, field
from typing import Any, Optional

from .recommendation_adapter import RecommendationAdapter, RecommendationPresentation


@dataclass(frozen=True)
class DecisionPipelineOutput:
    """Canonical user-action output for presentation, notifications, and APIs."""

    household_decisions: list[Any] = field(default_factory=list)
    recommendations: list[RecommendationPresentation] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "household_decisions": [
                item.as_dict() if hasattr(item, "as_dict") else dict(item)
                for item in self.household_decisions
            ],
            "recommendations": [item.as_dict() for item in self.recommendations],
        }


class DecisionPipeline:
    """Single platform-owned path from decisions to user-facing actions."""

    name = "decision_pipeline"

    def __init__(self, adapter: Optional[RecommendationAdapter] = None) -> None:
        self.adapter = adapter or RecommendationAdapter()

    def present(
        self,
        household_decisions: list[Any],
        *,
        advisor_explanations: Optional[list[Any]] = None,
        household_state: Optional[Any] = None,
        context: Optional[Any] = None,
        behaviour: Optional[Any] = None,
    ) -> DecisionPipelineOutput:
        recommendations = self.adapter.adapt(
            household_decisions,
            advisor_explanations=advisor_explanations,
            household_state=household_state,
            context=context,
            behaviour=behaviour,
        )
        return DecisionPipelineOutput(
            household_decisions=list(household_decisions),
            recommendations=recommendations,
        )


def build_decision_pipeline_output(
    household_decisions: list[Any],
    *,
    advisor_explanations: Optional[list[Any]] = None,
    household_state: Optional[Any] = None,
    context: Optional[Any] = None,
    behaviour: Optional[Any] = None,
) -> DecisionPipelineOutput:
    return DecisionPipeline().present(
        household_decisions,
        advisor_explanations=advisor_explanations,
        household_state=household_state,
        context=context,
        behaviour=behaviour,
    )
