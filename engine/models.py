#!/usr/bin/env python3
"""Shared platform contracts for PowerMonitor intelligence engines."""

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any, Optional, Union

CATEGORIES = ("EV", "Solar", "Bills", "Tariffs", "Behaviour", "Battery", "General")
RECOMMENDATION_CATEGORIES = ("billing", "solar", "EV", "tariff", "baseload", "data_quality")
CONFIDENCE_LEVELS = ("Low", "Medium", "High", "Very High")
DIFFICULTY_LEVELS = ("Easy", "Medium", "Hard")
FINANCIAL_IMPACT_PERIODS = ("today", "billing_period", "yearly")
CHARGING_ADVICE_STATUSES = ("CHARGE_NOW", "WAIT", "AVOID", "UNKNOWN")
ADVISOR_STATUSES = CHARGING_ADVICE_STATUSES + (
    "ON_TRACK",
    "SLIGHTLY_HIGH",
    "HIGH",
    "IMPROVING",
)
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
    explanation: Optional["DecisionExplanation"] = None

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


@dataclass
class RecommendationImpact:
    """Estimated recommendation value for the active period and monthly view."""

    period: Optional[float] = None
    monthly: Optional[float] = None
    currency: str = "AUD"
    basis: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecommendationDismissal:
    """Future-compatible state for hiding or suppressing recommendations."""

    state: str = "active"
    dismissible: bool = True
    dismissed: bool = False
    dismissed_at: Optional[str] = None
    dismissed_until: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in {"active", "dismissed", "snoozed_until"}:
            raise ValueError(f"Unsupported recommendation lifecycle state: {self.state}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Recommendation:
    """Presentation-agnostic recommendation emitted by the Recommendation Engine."""

    stable_id: str
    category: str
    title: str
    explanation: str
    estimated_financial_impact: RecommendationImpact
    confidence: str
    difficulty: str
    priority_score: int
    supporting_evidence: dict[str, Any]
    recommended_action: str
    dismissible_state: RecommendationDismissal = field(default_factory=RecommendationDismissal)

    def __post_init__(self) -> None:
        if self.category not in RECOMMENDATION_CATEGORIES:
            raise ValueError(f"Unsupported recommendation category: {self.category}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported recommendation confidence: {self.confidence}")
        if self.difficulty not in DIFFICULTY_LEVELS:
            raise ValueError(f"Unsupported recommendation difficulty: {self.difficulty}")
        self.priority_score = max(0, min(100, int(round(self.priority_score))))

    @property
    def id(self) -> str:
        return self.stable_id

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.stable_id
        return data


@dataclass(frozen=True)
class EvidenceItem:
    """One ordered evidence point used by an advisor."""

    source: str
    observation: str
    impact: str
    confidence: str
    freshness: Optional[str] = None
    financial_contribution: Optional[FinancialImpact] = None

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported evidence confidence: {self.confidence}")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.financial_contribution, FinancialImpact):
            data["financial_contribution"] = self.financial_contribution.as_dict()
        return data


@dataclass(frozen=True)
class EvidenceTrail:
    """Ordered explanation of why an advisor reached its conclusion."""

    items: list[EvidenceItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"items": [item.as_dict() for item in self.items]}


@dataclass(frozen=True)
class Advisor:
    """Shared presentation-agnostic contract for all PowerMonitor advisors."""

    advisor_id: str
    advisor_type: str
    status: str
    headline: str
    summary: str
    recommendation: str
    estimated_financial_impact: Optional[FinancialImpact]
    confidence: str
    confidence_reason: str
    evidence_trail: EvidenceTrail
    freshness: str
    supporting_data_sources: list[str]
    next_review_time: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in ADVISOR_STATUSES:
            raise ValueError(f"Unsupported advisor status: {self.status}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported advisor confidence: {self.confidence}")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.estimated_financial_impact, FinancialImpact):
            data["estimated_financial_impact"] = self.estimated_financial_impact.as_dict()
        if isinstance(self.evidence_trail, EvidenceTrail):
            data["evidence_trail"] = self.evidence_trail.as_dict()
        return data


@dataclass(frozen=True)
class AdvisorCollection:
    """Aggregates active advisors in deterministic priority order."""

    advisors: list[Advisor] = field(default_factory=list)

    def active(self) -> list[Advisor]:
        return sorted(self.advisors, key=_advisor_sort_key)

    def as_dict(self) -> dict[str, Any]:
        ordered = self.active()
        return {
            "advisors": [advisor.as_dict() for advisor in ordered],
            "count": len(ordered),
        }


@dataclass
class ChargingAdvice:
    """Presentation-agnostic answer to whether EV charging should happen now."""

    status: str
    headline: str
    explanation: str
    estimated_financial_benefit: Optional[FinancialImpact]
    confidence: str
    supporting_evidence: dict[str, Any]
    next_recommended_charging_window: Optional[str]
    data_freshness: str
    current_window: str
    next_window: str
    next_window_start: Optional[str]
    tariff_now_label: str
    tariff_now_rate: Optional[float]
    tariff_next_label: Optional[str]
    tariff_next_rate: Optional[float]
    per_kwh_difference: Optional[float]
    charger_data_status: str
    ev_session_freshness: str
    solar_data_status: str
    confidence_reason: str
    evidence_trail: EvidenceTrail
    advisor: Advisor

    def __post_init__(self) -> None:
        if self.status not in CHARGING_ADVICE_STATUSES:
            raise ValueError(f"Unsupported charging advice status: {self.status}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported charging advice confidence: {self.confidence}")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.estimated_financial_benefit, FinancialImpact):
            data["estimated_financial_benefit"] = self.estimated_financial_benefit.as_dict()
        return data


def _advisor_sort_key(advisor: Advisor) -> tuple[int, float, int, str]:
    status_rank = {
        "AVOID": 0,
        "HIGH": 0,
        "CHARGE_NOW": 1,
        "SLIGHTLY_HIGH": 1,
        "IMPROVING": 2,
        "ON_TRACK": 3,
        "WAIT": 2,
        "UNKNOWN": 4,
    }.get(advisor.status, 4)
    impact = 0.0
    if advisor.estimated_financial_impact:
        values = [
            value
            for value in advisor.estimated_financial_impact.periods_dict().values()
            if value is not None
        ]
        impact = max((abs(float(value)) for value in values), default=0.0)
    confidence_rank = {"Very High": 0, "High": 1, "Medium": 2, "Low": 3}.get(advisor.confidence, 4)
    return (status_rank, -impact, confidence_rank, advisor.advisor_id)


@dataclass(frozen=True)
class DecisionExplanation:
    """Explainability payload owned by the Decision Engine."""

    why_this_matters: str
    evidence_used: list[str]
    financial_impact: str
    confidence_reason: str
    data_sources_used: list[str]
    freshness_completeness_note: Optional[str] = None


@dataclass(frozen=True)
class TimelineEntry:
    """A human-readable event in the household decision timeline."""

    timestamp: str
    title: str
    summary: str
    financial_impact: Optional[str]
    confidence: str
    evidence_sources: list[str]
    event_type: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionTimeline:
    """Chronological memory of significant household decisions and outcomes."""

    entries: list[TimelineEntry] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"entries": [entry.as_dict() for entry in self.entries]}


@dataclass(frozen=True)
class EnergyScore:
    """Daily household energy decision health score."""

    score: int
    label: str
    summary: str
    top_positive_factors: list[str]
    top_negative_factors: list[str]
    potential_score: int
    confidence: str
    data_sources_used: list[str]
    explanation: DecisionExplanation

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_api_safe(value: Any) -> Any:
    """Return a JSON-safe representation of platform and presentation models."""

    if hasattr(value, "as_dict") and callable(value.as_dict):
        return to_api_safe(value.as_dict())
    if is_dataclass(value):
        return to_api_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_api_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_api_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
