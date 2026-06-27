#!/usr/bin/env python3
"""Financial impact engine interface.

This module defines the contract boundary for future tariff-aware financial
analysis. It intentionally contains no business logic in Sprint 0.6.1.
"""

from .models import EngineStatus, FinancialImpact


class FinancialEngine:
    """Placeholder interface for financial impact calculations."""

    name = "financial_engine"

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def evaluate(self, context: dict) -> list[FinancialImpact]:
        return []
