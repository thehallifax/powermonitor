#!/usr/bin/env python3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.decision_engine import Decision, generate_decisions, generate_ev_session_decisions
from engine.models import Recommendation
from engine.recommendation_engine import (
    RecommendationEngine,
    generate_ev_session_recommendations,
    generate_recommendations,
    generate_recommendations_from_data_engine,
)

# Deprecated compatibility exports.
# PB-026: user-facing action guidance must use
# DecisionEngine -> DecisionPipeline -> RecommendationPresentation. This module
# remains for older imports and should be removed once those callers migrate.

__all__ = [
    "Decision",
    "Recommendation",
    "RecommendationEngine",
    "generate_decisions",
    "generate_ev_session_decisions",
    "generate_ev_session_recommendations",
    "generate_recommendations",
    "generate_recommendations_from_data_engine",
]
