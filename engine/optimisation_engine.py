#!/usr/bin/env python3
"""Optimisation engine interface.

This module defines the contract boundary for future action optimisation. It
intentionally contains no business logic in Sprint 0.6.1.
"""

from .models import Decision, EngineStatus


class OptimisationEngine:
    """Placeholder interface for ranking and improving decisions."""

    name = "optimisation_engine"

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def optimise(self, decisions: list[Decision], context: dict) -> list[Decision]:
        return decisions
