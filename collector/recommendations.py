#!/usr/bin/env python3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.decision_engine import (
    Decision,
    generate_decisions,
    generate_ev_session_decisions,
    generate_recommendations,
)

Recommendation = Decision

__all__ = [
    "Decision",
    "Recommendation",
    "generate_decisions",
    "generate_ev_session_decisions",
    "generate_recommendations",
]
