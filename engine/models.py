#!/usr/bin/env python3
"""Shared platform contracts for PowerMonitor intelligence engines."""

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union

CATEGORIES = ("EV", "Solar", "Bills", "Tariffs", "Behaviour", "Battery", "General")
CONFIDENCE_LEVELS = ("Low", "Medium", "High", "Very High")
FINANCIAL_IMPACT_PERIODS = ("today", "billing_period", "yearly")
ENGINE_STATES = ("idle", "ready", "running", "degraded", "failed")
FRESHNESS_STATES = ("fresh", "stale", "expired", "unknown")


@dataclass
class FinancialImpact:
    """Estimated monetary impact over standard PowerMonitor periods."""

    today: Optional[float] = None
    billing_period: Optional[float] = None
    yearly: Optional[float] = None
    currency: str = "AUD"
    basis: Optional[str] = None

    def items(self):
        return self.periods_dict().items()

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        value = getattr(self, key)
        if value is None:
            setattr(self, key, default)
            return default
        return value

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def periods_dict(self) -> dict[str, Optional[float]]:
        return {
            "today": self.today,
            "billing_period": self.billing_period,
            "yearly": self.yearly,
        }


@dataclass(frozen=True)
class Evidence:
    """A traceable measurement, threshold, comparison, or source fact."""

    name: str
    value: Any
    unit: Optional[str] = None
    period: Optional[str] = None
    source: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class Confidence:
    """Confidence level plus the reason behind it."""

    level: str
    reason: Optional[str] = None
    source: Optional[str] = None

    def __post_init__(self) -> None:
        if self.level not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported confidence level: {self.level}")


@dataclass(frozen=True)
class Freshness:
    """Recency and completeness state for source data or engine output."""

    state: str = "unknown"
    observed_at: Optional[str] = None
    expires_at: Optional[str] = None
    age_minutes: Optional[float] = None

    def __post_init__(self) -> None:
        if self.state not in FRESHNESS_STATES:
            raise ValueError(f"Unsupported freshness state: {self.state}")


@dataclass
class Insight:
    """Source-agnostic intelligence output before it becomes a decision."""

    id: str
    title: str
    summary: str
    evidence: list[Evidence] = field(default_factory=list)
    confidence: Optional[Confidence] = None
    freshness: Optional[Freshness] = None
    source_engine: Optional[str] = None


@dataclass
class Forecast:
    """Projected value with optional uncertainty and supporting evidence."""

    id: str
    title: str
    value: float
    unit: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    evidence: list[Evidence] = field(default_factory=list)
    confidence: Optional[Confidence] = None


@dataclass(frozen=True)
class EngineStatus:
    """Health and readiness information reported by an intelligence engine."""

    name: str
    state: str = "ready"
    message: Optional[str] = None
    freshness: Optional[Freshness] = None

    def __post_init__(self) -> None:
        if self.state not in ENGINE_STATES:
            raise ValueError(f"Unsupported engine state: {self.state}")


@dataclass
class Decision:
    """Actionable recommendation emitted by the Decision Engine."""

    id: str
    title: str
    summary: str
    finding: str
    evidence: dict[str, Any]
    financial_impact: Union[FinancialImpact, dict[str, Any]]
    recommendation: str
    confidence: str
    priority: str
    expires_at: Optional[str]
    data_sources_used: list[str]
    category: str
    action_type: str

    @property
    def severity(self) -> str:
        return {
            "low": "green",
            "medium": "amber",
            "high": "red",
        }.get(self.priority.lower(), "amber")

    @property
    def metrics(self) -> dict[str, Any]:
        metrics = dict(self.evidence)
        impact = (
            self.financial_impact.periods_dict()
            if isinstance(self.financial_impact, FinancialImpact)
            else self.financial_impact
        )
        metrics.update(
            {
                key: value
                for key, value in impact.items()
                if value is not None
            }
        )
        return metrics

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.financial_impact, FinancialImpact):
            data["financial_impact"] = self.financial_impact.periods_dict()
        data["severity"] = self.severity
        data["metrics"] = self.metrics
        data["action"] = self.recommendation
        return data
