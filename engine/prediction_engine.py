#!/usr/bin/env python3
"""Prediction engine interface.

This module defines the contract boundary for future forecasts. It
intentionally contains no business logic in Sprint 0.6.1.
"""

from typing import Optional

from .models import EngineStatus, Forecast, HouseholdContext


class PredictionEngine:
    """Placeholder interface for forecasts."""

    name = "prediction_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def forecast(
        self,
        context: dict,
        household_context: Optional[HouseholdContext] = None,
    ) -> list[Forecast]:
        self.household_context = household_context or self.household_context
        return []
