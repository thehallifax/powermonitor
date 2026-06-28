#!/usr/bin/env python3
"""Behaviour engine interface.

This module defines the contract boundary for future household behaviour
analysis. It intentionally contains no business logic in Sprint 0.6.1.
"""

from typing import Optional

from .models import EngineStatus, HouseholdContext, Insight


class BehaviourEngine:
    """Placeholder interface for behaviour insights."""

    name = "behaviour_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def evaluate(
        self,
        context: dict,
        household_context: Optional[HouseholdContext] = None,
    ) -> list[Insight]:
        self.household_context = household_context or self.household_context
        return []
