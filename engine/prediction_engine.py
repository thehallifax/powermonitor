#!/usr/bin/env python3
"""Prediction engine interface.

This module defines the contract boundary for future forecasts. It
intentionally contains no business logic in Sprint 0.6.1.
"""

from .models import EngineStatus, Forecast


class PredictionEngine:
    """Placeholder interface for forecasts."""

    name = "prediction_engine"

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def forecast(self, context: dict) -> list[Forecast]:
        return []
