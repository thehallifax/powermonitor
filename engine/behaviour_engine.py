#!/usr/bin/env python3
"""Behaviour engine interface.

This module defines the contract boundary for future household behaviour
analysis. It intentionally contains no business logic in Sprint 0.6.1.
"""

from .models import EngineStatus, Insight


class BehaviourEngine:
    """Placeholder interface for behaviour insights."""

    name = "behaviour_engine"

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def evaluate(self, context: dict) -> list[Insight]:
        return []
