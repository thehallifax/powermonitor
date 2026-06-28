#!/usr/bin/env python3
"""Optimisation engine interface.

This module defines the contract boundary for future action optimisation. It
intentionally contains no business logic in Sprint 0.6.1.
"""

from typing import Optional

from .models import Decision, EngineStatus, HouseholdContext


class OptimisationEngine:
    """Placeholder interface for ranking and improving decisions."""

    name = "optimisation_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def optimise(
        self,
        decisions: list[Decision],
        context: dict,
        household_context: Optional[HouseholdContext] = None,
    ) -> list[Decision]:
        self.household_context = household_context or self.household_context
        return decisions
