#!/usr/bin/env python3
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any, Callable, Optional

from .financial_engine import FinancialEngine
from .models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    FINANCIAL_IMPACT_PERIODS,
    Decision,
    DecisionExplanation,
    FinancialImpact,
    HouseholdContext,
)
from .recommendation_engine import (
    generate_ev_session_recommendations,
    generate_recommendations as generate_shared_recommendations,
    generate_recommendations_from_data_engine as generate_shared_recommendations_from_data_engine,
    recommendations_to_decisions,
)

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
BUCKET_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}


class DecisionType(str, Enum):
    CHARGE_NOW = "CHARGE_NOW"
    WAIT_FOR_CHEAPER_WINDOW = "WAIT_FOR_CHEAPER_WINDOW"
    WAIT_FOR_SOLAR = "WAIT_FOR_SOLAR"
    NO_ACTION = "NO_ACTION"
    REVIEW_PROVIDER = "REVIEW_PROVIDER"
    REVIEW_SETUP = "REVIEW_SETUP"
    REVIEW_BILLING = "REVIEW_BILLING"
    MONITOR_USAGE = "MONITOR_USAGE"
    INVESTIGATE_ANOMALY = "INVESTIGATE_ANOMALY"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class DecisionPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class DecisionStatus(str, Enum):
    ACTIVE = "active"
    SUPPRESSED = "suppressed"
    MONITORING = "monitoring"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class DecisionEvidence:
    source: str
    signal: str
    value: Any = None
    quality: DecisionConfidence = DecisionConfidence.UNKNOWN
    observed_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "signal": self.signal,
            "value": self.value,
            "quality": self.quality.value,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class DecisionReason:
    code: str
    summary: str
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionOutcome:
    status: str
    expected_effect: str
    financial_impact: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HouseholdDecision:
    decision_id: str
    household_id: str
    decision_type: DecisionType
    priority: DecisionPriority
    confidence: DecisionConfidence
    status: DecisionStatus
    created_at: str
    reasons: list[DecisionReason] = field(default_factory=list)
    evidence: list[DecisionEvidence] = field(default_factory=list)
    outcome: Optional[DecisionOutcome] = None
    financial_impact: Optional[dict[str, Any]] = None
    data_sources_used: list[str] = field(default_factory=list)
    valid_until: Optional[str] = None
    related_anomaly_ids: list[str] = field(default_factory=list)
    related_state_change_ids: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    available_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)

    @property
    def type(self) -> str:
        return self.decision_type.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "household_id": self.household_id,
            "type": self.decision_type.value,
            "decision_type": self.decision_type.value,
            "priority": self.priority.value,
            "confidence": self.confidence.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "reasons": [item.as_dict() for item in self.reasons],
            "evidence": [item.as_dict() for item in self.evidence],
            "outcome": self.outcome.as_dict() if self.outcome else None,
            "financial_impact": self.financial_impact,
            "data_sources_used": list(self.data_sources_used),
            "related_anomaly_ids": list(self.related_anomaly_ids),
            "related_state_change_ids": list(self.related_state_change_ids),
            "required_capabilities": list(self.required_capabilities),
            "available_capabilities": list(self.available_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
        }


class DecisionEngine:
    """Single authority for normalized household decisions.

    The engine consumes existing platform state and emits decision contracts.
    It does not render wording, HTML, or UI-specific copy.
    """

    def build(
        self,
        *,
        household_state: Any = None,
        context: Any = None,
        behaviour: Any = None,
        anomalies: Optional[list[Any]] = None,
        evidence_quality: Any = None,
        notification_readiness: Any = None,
        capability_summary: Any = None,
        financial_metadata: Optional[dict[str, Any]] = None,
        provider_health: Any = None,
        state_changes: Optional[list[Any]] = None,
        now: Optional[Any] = None,
    ) -> list[HouseholdDecision]:
        del behaviour, notification_readiness
        created_at = _iso_now(now, household_state, context)
        household_id = str(_get(household_state, "household_id") or _get(context, "household_id") or "default_household")
        capabilities = _capability_map(capability_summary)
        evidence_confidence = _decision_confidence(
            evidence_quality
            or _get(_get(household_state, "evidence"), "confidence")
            or _get(context, "confidence")
        )
        decisions: list[HouseholdDecision] = []

        self._setup_decisions(decisions, household_id, created_at, capabilities, evidence_confidence)
        self._provider_decisions(decisions, household_id, created_at, provider_health, household_state, evidence_confidence)
        self._data_quality_decisions(decisions, household_id, created_at, household_state, context, evidence_confidence)
        self._anomaly_decisions(decisions, household_id, created_at, anomalies or [], evidence_confidence)
        self._charging_decision(decisions, household_id, created_at, household_state, context, capabilities, financial_metadata or {}, evidence_confidence)
        self._billing_decision(decisions, household_id, created_at, household_state, context, financial_metadata or {}, evidence_confidence)
        self._state_change_decisions(decisions, household_id, created_at, state_changes or [], evidence_confidence)

        if not decisions:
            decisions.append(self._decision(
                household_id,
                DecisionType.NO_ACTION,
                DecisionPriority.LOW,
                evidence_confidence,
                created_at,
                [DecisionReason("no_active_action", "Current platform signals do not require an immediate household action.")],
                [DecisionEvidence("HouseholdState", "current_status", _get(_get(household_state, "summary"), "current_status"), evidence_confidence, created_at)],
                outcome=DecisionOutcome("monitor", "Continue monitoring current household state."),
                data_sources=_data_sources_from_state(household_state, context),
            ))
        return sorted(decisions, key=_household_decision_sort_key)

    def summary(self, decisions: list[HouseholdDecision]) -> dict[str, Any]:
        counts_by_type: dict[str, int] = {}
        counts_by_priority: dict[str, int] = {}
        counts_by_status: dict[str, int] = {}
        for decision in decisions:
            counts_by_type[decision.decision_type.value] = counts_by_type.get(decision.decision_type.value, 0) + 1
            counts_by_priority[decision.priority.value] = counts_by_priority.get(decision.priority.value, 0) + 1
            counts_by_status[decision.status.value] = counts_by_status.get(decision.status.value, 0) + 1
        primary = decisions[0].as_dict() if decisions else None
        return {
            "count": len(decisions),
            "active_count": len([item for item in decisions if item.status == DecisionStatus.ACTIVE]),
            "counts_by_type": counts_by_type,
            "counts_by_priority": counts_by_priority,
            "counts_by_status": counts_by_status,
            "primary_decision": primary,
        }

    def _setup_decisions(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        capabilities: dict[str, Any],
        evidence_confidence: DecisionConfidence,
    ) -> None:
        required = ["grid_smart_meter", "tariff"]
        missing = [capability for capability in required if not _capability_usable(capabilities.get(capability))]
        if not missing:
            return
        available = [capability for capability in required if capability not in missing]
        decisions.append(self._decision(
            household_id,
            DecisionType.REVIEW_SETUP,
            DecisionPriority.HIGH,
            _cap_confidence(evidence_confidence, DecisionConfidence.MEDIUM),
            created_at,
            [DecisionReason("required_capability_missing", f"Required capability missing: {', '.join(missing)}.")],
            [
                DecisionEvidence("CapabilitySummary", capability, _capability_state(capabilities.get(capability)), evidence_confidence, created_at)
                for capability in required
            ],
            outcome=DecisionOutcome("setup_required", "Complete required platform setup before relying on household decisions."),
            data_sources=["Capability Summary"],
            required_capabilities=required,
            available_capabilities=available,
            missing_capabilities=missing,
        ))

    def _provider_decisions(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        provider_health: Any,
        household_state: Any,
        evidence_confidence: DecisionConfidence,
    ) -> None:
        degraded = _provider_degraded(provider_health)
        collector = _get(household_state, "collector")
        collector_healthy = _get(collector, "healthy")
        if not degraded and collector_healthy is not False:
            return
        reasons = []
        evidence = []
        if degraded:
            reasons.append(DecisionReason("provider_degraded", "One or more providers report degraded health."))
            evidence.append(DecisionEvidence("ProviderHealth", "provider_health", "degraded", evidence_confidence, created_at))
        if collector_healthy is False:
            reasons.append(DecisionReason("collector_unhealthy", "Collector health is not currently good."))
            evidence.append(DecisionEvidence("HouseholdState", "collector_status", _get(collector, "status"), evidence_confidence, created_at))
        decisions.append(self._decision(
            household_id,
            DecisionType.REVIEW_PROVIDER,
            DecisionPriority.HIGH if collector_healthy is False else DecisionPriority.MEDIUM,
            _cap_confidence(evidence_confidence, DecisionConfidence.MEDIUM),
            created_at,
            reasons,
            evidence,
            outcome=DecisionOutcome("provider_review", "Provider or collector health should be reviewed before acting on dependent advice."),
            data_sources=["Provider Health", "Household State"],
        ))

    def _data_quality_decisions(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        household_state: Any,
        context: Any,
        evidence_confidence: DecisionConfidence,
    ) -> None:
        if evidence_confidence not in {DecisionConfidence.LOW, DecisionConfidence.UNKNOWN}:
            return
        evidence_state = _get(household_state, "evidence")
        decisions.append(self._decision(
            household_id,
            DecisionType.DATA_INSUFFICIENT,
            DecisionPriority.MEDIUM,
            evidence_confidence,
            created_at,
            [DecisionReason("evidence_insufficient", "Available evidence is not strong enough for high-confidence household action.")],
            [
                DecisionEvidence("HouseholdState", "evidence_quality", _get(evidence_state, "quality"), evidence_confidence, created_at),
                DecisionEvidence("HouseholdContext", "context_evidence", _get(context, "evidence"), evidence_confidence, created_at),
            ],
            outcome=DecisionOutcome("do_not_escalate", "Avoid high-confidence action decisions until evidence improves."),
            data_sources=_data_sources_from_state(household_state, context),
        ))

    def _anomaly_decisions(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        anomalies: list[Any],
        evidence_confidence: DecisionConfidence,
    ) -> None:
        active = [
            anomaly for anomaly in anomalies
            if str(_get(anomaly, "severity") or "").lower() in {"warning", "critical", "watch"}
        ]
        if not active:
            return
        top = sorted(active, key=lambda item: _anomaly_rank(_get(item, "severity")), reverse=True)[0]
        anomaly_confidence = _decision_confidence(_get(top, "confidence") or evidence_confidence)
        decisions.append(self._decision(
            household_id,
            DecisionType.INVESTIGATE_ANOMALY,
            DecisionPriority.HIGH if str(_get(top, "severity")).lower() in {"warning", "critical"} else DecisionPriority.MEDIUM,
            _cap_confidence(evidence_confidence, anomaly_confidence),
            created_at,
            [DecisionReason("household_anomaly_active", str(_get(top, "summary") or _get(top, "title") or "A household anomaly is active."))],
            [DecisionEvidence("AnomalyEngine", str(_get(top, "category") or "anomaly"), _get(top, "title"), anomaly_confidence, _get(top, "first_detected") or created_at)],
            outcome=DecisionOutcome("investigate", "Investigate the active anomaly using its evidence trail."),
            data_sources=list(_get(top, "data_sources_used") or ["Anomaly Engine"]),
            related_anomaly_ids=[str(_get(top, "anomaly_id") or "")],
        ))

    def _charging_decision(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        household_state: Any,
        context: Any,
        capabilities: dict[str, Any],
        financial_metadata: dict[str, Any],
        evidence_confidence: DecisionConfidence,
    ) -> None:
        if not _capability_usable(capabilities.get("ev")):
            return
        tariff = str(_get(context, "tariff") or "")
        charging = str(_get(context, "charging") or "")
        solar = str(_get(context, "solar") or "")
        current_rate = _get(_get(household_state, "tariff"), "active_rate")
        financial_impact = _financial_impact_dict(financial_metadata.get("charging"))
        evidence = [
            DecisionEvidence("HouseholdContext", "tariff", tariff, evidence_confidence, created_at),
            DecisionEvidence("HouseholdContext", "charging", charging, evidence_confidence, created_at),
            DecisionEvidence("HouseholdState", "active_rate", current_rate, evidence_confidence, created_at),
        ]
        if evidence_confidence in {DecisionConfidence.LOW, DecisionConfidence.UNKNOWN}:
            return
        if charging == "CHEAP_WINDOW_ACTIVE" or tariff == "LOWEST_COST":
            decisions.append(self._decision(
                household_id,
                DecisionType.CHARGE_NOW,
                DecisionPriority.HIGH,
                evidence_confidence,
                created_at,
                [DecisionReason("cheap_window_active", "The current tariff context is the lowest-cost charging window.")],
                evidence,
                outcome=DecisionOutcome("charge_now", "Charging now aligns with current tariff context.", financial_impact),
                financial_impact=financial_impact,
                data_sources=_data_sources_from_state(household_state, context, "Capability Summary"),
                required_capabilities=["ev", "tariff"],
                available_capabilities=["ev", "tariff"],
            ))
        elif tariff in {"HIGH_COST", "HIGHEST_COST"}:
            decisions.append(self._decision(
                household_id,
                DecisionType.WAIT_FOR_CHEAPER_WINDOW,
                DecisionPriority.HIGH if tariff == "HIGHEST_COST" else DecisionPriority.MEDIUM,
                evidence_confidence,
                created_at,
                [DecisionReason("current_tariff_high", "The current tariff context is high-cost and a cheaper window is known.")],
                evidence + [DecisionEvidence("HouseholdState", "next_lower_cost_window", _get(_get(household_state, "tariff"), "next_lower_cost_window"), evidence_confidence, created_at)],
                outcome=DecisionOutcome("wait", "Wait for the next lower-cost tariff window.", financial_impact),
                financial_impact=financial_impact,
                data_sources=_data_sources_from_state(household_state, context, "Capability Summary"),
                required_capabilities=["ev", "tariff"],
                available_capabilities=["ev", "tariff"],
            ))
        elif solar == "SOLAR_SURPLUS_POSSIBLE":
            decisions.append(self._decision(
                household_id,
                DecisionType.WAIT_FOR_SOLAR,
                DecisionPriority.MEDIUM,
                evidence_confidence,
                created_at,
                [DecisionReason("solar_surplus_possible", "Solar context suggests surplus may support flexible charging.")],
                evidence + [DecisionEvidence("HouseholdContext", "solar", solar, evidence_confidence, created_at)],
                outcome=DecisionOutcome("wait_for_solar", "Wait for solar-supported charging evidence."),
                data_sources=_data_sources_from_state(household_state, context, "Capability Summary"),
                required_capabilities=["ev", "solar"],
                available_capabilities=["ev", "solar"],
            ))

    def _billing_decision(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        household_state: Any,
        context: Any,
        financial_metadata: dict[str, Any],
        evidence_confidence: DecisionConfidence,
    ) -> None:
        billing = str(_get(context, "billing") or "")
        projected = financial_metadata.get("projected_bill")
        if billing not in {"HIGH_SPEND", "BILL_END_APPROACHING"} and not _high_projected_bill(financial_metadata):
            return
        priority = DecisionPriority.HIGH if billing == "HIGH_SPEND" or _high_projected_bill(financial_metadata) else DecisionPriority.MEDIUM
        decisions.append(self._decision(
            household_id,
            DecisionType.REVIEW_BILLING,
            priority,
            evidence_confidence,
            created_at,
            [DecisionReason("billing_context_review", f"Billing context is {billing or 'available for review'}.")],
            [
                DecisionEvidence("HouseholdContext", "billing", billing, evidence_confidence, created_at),
                DecisionEvidence("FinancialEngine", "projected_bill", projected, evidence_confidence, created_at),
            ],
            outcome=DecisionOutcome("review_billing", "Review bill trajectory and tariff exposure.", _financial_impact_dict(financial_metadata.get("billing"))),
            financial_impact=_financial_impact_dict(financial_metadata.get("billing")),
            data_sources=_data_sources_from_state(household_state, context, "FinancialEngine"),
        ))

    def _state_change_decisions(
        self,
        decisions: list[HouseholdDecision],
        household_id: str,
        created_at: str,
        state_changes: list[Any],
        evidence_confidence: DecisionConfidence,
    ) -> None:
        peak_changes = [
            change for change in state_changes
            if str(_get(change, "change_type") or "") in {"entered_peak", "tariff_window_changed"}
        ]
        if not peak_changes:
            return
        latest = peak_changes[0]
        decisions.append(self._decision(
            household_id,
            DecisionType.MONITOR_USAGE,
            DecisionPriority.MEDIUM,
            evidence_confidence,
            created_at,
            [DecisionReason("state_change_monitoring", str(_get(latest, "summary") or "A relevant household state change occurred."))],
            [DecisionEvidence("StateChangeEngine", str(_get(latest, "change_type") or "state_change"), _get(latest, "title"), evidence_confidence, _get(latest, "detected_at") or created_at)],
            outcome=DecisionOutcome("monitor", "Monitor usage while this state change is active."),
            data_sources=["StateChangeEngine"],
            related_state_change_ids=[str(_get(latest, "change_id") or "")],
        ))

    def _decision(
        self,
        household_id: str,
        decision_type: DecisionType,
        priority: DecisionPriority,
        confidence: DecisionConfidence,
        created_at: str,
        reasons: list[DecisionReason],
        evidence: list[DecisionEvidence],
        *,
        outcome: Optional[DecisionOutcome] = None,
        financial_impact: Optional[dict[str, Any]] = None,
        data_sources: Optional[list[str]] = None,
        related_anomaly_ids: Optional[list[str]] = None,
        related_state_change_ids: Optional[list[str]] = None,
        required_capabilities: Optional[list[str]] = None,
        available_capabilities: Optional[list[str]] = None,
        missing_capabilities: Optional[list[str]] = None,
        status: DecisionStatus = DecisionStatus.ACTIVE,
    ) -> HouseholdDecision:
        reason_key = "|".join(reason.code for reason in reasons) or "none"
        evidence_key = "|".join(str(item.value) for item in evidence[:3])
        decision_id = hashlib.sha256(f"{household_id}|{decision_type.value}|{reason_key}|{evidence_key}".encode("utf-8")).hexdigest()[:24]
        return HouseholdDecision(
            decision_id=decision_id,
            household_id=household_id,
            decision_type=decision_type,
            priority=priority,
            confidence=confidence,
            status=status,
            created_at=created_at,
            reasons=reasons,
            evidence=evidence,
            outcome=outcome,
            financial_impact=financial_impact,
            data_sources_used=list(dict.fromkeys(data_sources or [])),
            related_anomaly_ids=[item for item in (related_anomaly_ids or []) if item],
            related_state_change_ids=[item for item in (related_state_change_ids or []) if item],
            required_capabilities=required_capabilities or [],
            available_capabilities=available_capabilities or [],
            missing_capabilities=missing_capabilities or [],
        )


Rule = Callable[[dict, dict], Optional[Decision]]

financial_engine = FinancialEngine()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    value = getattr(obj, key, default)
    if isinstance(value, Enum):
        return value.value
    return value


def _iso_now(now: Optional[Any], household_state: Any, context: Any) -> str:
    if now is not None:
        return now.isoformat(timespec="seconds") if hasattr(now, "isoformat") else str(now)
    return str(_get(household_state, "generated_at") or _get(context, "observed_at") or date.today().isoformat())


def _decision_confidence(value: Any) -> DecisionConfidence:
    if isinstance(value, DecisionConfidence):
        return value
    text = str(value or "").upper()
    if text in {"VERY HIGH", "VERY_HIGH", "HIGH"}:
        return DecisionConfidence.HIGH
    if text == "MEDIUM":
        return DecisionConfidence.MEDIUM
    if text == "LOW":
        return DecisionConfidence.LOW
    return DecisionConfidence.UNKNOWN


def _cap_confidence(*values: DecisionConfidence) -> DecisionConfidence:
    order = {
        DecisionConfidence.UNKNOWN: 0,
        DecisionConfidence.LOW: 1,
        DecisionConfidence.MEDIUM: 2,
        DecisionConfidence.HIGH: 3,
    }
    return min(values, key=lambda item: order[item]) if values else DecisionConfidence.UNKNOWN


def _capability_map(capability_summary: Any) -> dict[str, Any]:
    capabilities = _get(capability_summary, "capabilities") or []
    result = {}
    for capability in capabilities:
        capability_id = _get(capability, "capability_id")
        if capability_id:
            result[str(capability_id)] = capability
    return result


def _capability_usable(capability: Any) -> bool:
    if capability is None:
        return False
    usable = _get(capability, "usable")
    if usable is not None:
        return bool(usable)
    return bool(_get(capability, "present") or _get(capability, "configured") or _get(capability, "available"))


def _capability_state(capability: Any) -> str:
    if capability is None:
        return "missing"
    if _capability_usable(capability):
        return "usable"
    if _get(capability, "configured"):
        return "configured"
    if _get(capability, "available"):
        return "available"
    return "missing"


def _provider_degraded(provider_health: Any) -> bool:
    if provider_health is None:
        return False
    data = provider_health.as_dict() if hasattr(provider_health, "as_dict") else provider_health
    providers = _get(data, "providers") or []
    if isinstance(data, dict) and not providers:
        providers = data.get("provider_status") or data.get("latest") or []
    for provider in providers:
        state = str(_get(provider, "lifecycle_state") or _get(provider, "connection_status") or _get(provider, "status") or "").upper()
        if state in {"DEGRADED", "ERROR", "FAILED"}:
            return True
    return False


def _data_sources_from_state(household_state: Any, context: Any, *extra: str) -> list[str]:
    evidence = _get(household_state, "evidence")
    sources = list(_get(evidence, "data_sources_used") or [])
    sources.extend(_get(context, "data_sources_used") or [])
    sources.extend(extra)
    return list(dict.fromkeys(str(source) for source in sources if source))


def _financial_impact_dict(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, FinancialImpact):
        return value.as_dict()
    if isinstance(value, dict):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return None


def _high_projected_bill(financial_metadata: dict[str, Any]) -> bool:
    projected = financial_metadata.get("projected_bill")
    threshold = financial_metadata.get("high_cost_threshold")
    try:
        return projected is not None and threshold is not None and float(projected) > float(threshold)
    except (TypeError, ValueError):
        return False


def _anomaly_rank(severity: Any) -> int:
    return {"critical": 4, "warning": 3, "watch": 2, "info": 1}.get(str(severity or "").lower(), 0)


def _household_decision_sort_key(decision: HouseholdDecision) -> tuple[int, int, str]:
    priority = {
        DecisionPriority.CRITICAL: 0,
        DecisionPriority.HIGH: 1,
        DecisionPriority.MEDIUM: 2,
        DecisionPriority.LOW: 3,
    }.get(decision.priority, 4)
    confidence = {
        DecisionConfidence.HIGH: 0,
        DecisionConfidence.MEDIUM: 1,
        DecisionConfidence.LOW: 2,
        DecisionConfidence.UNKNOWN: 3,
    }.get(decision.confidence, 4)
    return (priority, confidence, decision.decision_type.value)


def _impact(
    today: Optional[float] = None,
    billing_period: Optional[float] = None,
    yearly: Optional[float] = None,
) -> FinancialImpact:
    return FinancialImpact(
        today=today,
        billing_period=billing_period,
        yearly=yearly,
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _data_sources(context: dict, *extra_sources: str) -> list[str]:
    sources = ["SolarEdge interval data"]
    ev_source = context.get("ev_data_source")
    if ev_source == "home_assistant_ocpp":
        sources.append("HA OCPP session snapshots")
    elif ev_source == "ocular_export":
        sources.append("Ocular EV export")
    elif ev_source:
        sources.append("Estimated EV charging")
    sources.extend(extra_sources)
    return list(dict.fromkeys(sources))


def _validate_decision(decision: Decision) -> Decision:
    if decision.category not in CATEGORIES:
        raise ValueError(f"Unsupported decision category: {decision.category}")
    if decision.confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"Unsupported decision confidence: {decision.confidence}")
    for period in FINANCIAL_IMPACT_PERIODS:
        decision.financial_impact.setdefault(period, None)
    if decision.explanation is None:
        decision.explanation = _decision_explanation(decision)
    return decision


def _decision_explanation(decision: Decision) -> DecisionExplanation:
    return DecisionExplanation(
        why_this_matters=_why_this_matters(decision),
        evidence_used=_evidence_used(decision),
        financial_impact=_financial_impact_text(decision),
        confidence_reason=_confidence_reason(decision),
        data_sources_used=list(decision.data_sources_used),
        freshness_completeness_note=_freshness_completeness_note(decision),
    )


def _why_this_matters(decision: Decision) -> str:
    if decision.category == "Bills":
        return "This can affect the current bill and should be reviewed before the billing period closes."
    if decision.category == "Tariffs":
        return "This points to usage that may cost more than it needs to under the current tariff."
    if decision.category == "Solar":
        return "This affects how much solar is used at home instead of exported at a lower credit."
    if decision.category == "EV":
        return "EV charging is a flexible load, so timing can materially change charging cost."
    if decision.category == "Behaviour":
        return "Always-on or overnight load can quietly add cost across the whole billing period."
    if decision.action_type == "maintain":
        return "This is a positive signal worth keeping in place."
    return "This decision highlights an energy pattern that may be worth acting on."


def _evidence_used(decision: Decision) -> list[str]:
    if not decision.evidence:
        return [decision.finding]
    return [
        f"{_evidence_label(key)}: {_evidence_value(value)}"
        for key, value in decision.evidence.items()
    ]


def _evidence_label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _evidence_value(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _financial_impact_text(decision: Decision) -> str:
    labels = {
        "today": "today",
        "billing_period": "this billing period",
        "yearly": "per year",
    }
    for key in FINANCIAL_IMPACT_PERIODS:
        value = decision.financial_impact.get(key)
        if value is not None:
            return f"{_money(float(value))} {labels[key]}"
    return "Not quantified by the current Decision Engine output."


def _confidence_reason(decision: Decision) -> str:
    projection_days = decision.evidence.get("projection_reading_days")
    if projection_days is not None:
        return (
            f"{decision.confidence} confidence because the projection uses "
            f"{projection_days} reading days and the available source metadata."
        )
    if any("Actual" in source or "Ocular" in source for source in decision.data_sources_used):
        return f"{decision.confidence} confidence because actual session/source data is available."
    if "Estimated EV charging" in decision.data_sources_used:
        return f"{decision.confidence} confidence because part of the result uses estimated EV charging."
    return f"{decision.confidence} confidence based on the available interval data and configured assumptions."


def _freshness_completeness_note(decision: Decision) -> Optional[str]:
    notes = []
    reading_days = decision.evidence.get("projection_reading_days")
    projected_days = decision.evidence.get("projected_period_days")
    if reading_days is not None and projected_days is not None:
        notes.append(f"Uses {reading_days} of {projected_days} projected period days.")
    elif reading_days is not None:
        notes.append(f"Uses {reading_days} reading days.")
    if decision.expires_at:
        notes.append(f"Decision expires at {decision.expires_at}.")
    return " ".join(notes) if notes else None


def _timeline_entries(context: dict) -> list:
    timeline = context.get("timeline_history")
    return list(getattr(timeline, "entries", []) or [])


def _timeline_count(context: dict, event_type: str) -> int:
    return sum(1 for entry in _timeline_entries(context) if entry.event_type == event_type)


def _latest_timeline_impact(context: dict, event_type: str) -> Optional[str]:
    matching = [
        entry for entry in _timeline_entries(context)
        if entry.event_type == event_type and entry.financial_impact
    ]
    return matching[-1].financial_impact if matching else None


def _trend_sources(*extra_sources: str) -> list[str]:
    return list(dict.fromkeys(("PowerMonitor timeline history", *extra_sources)))


def _entry_date(entry) -> Optional[date]:
    try:
        return date.fromisoformat(str(entry.timestamp)[:10])
    except ValueError:
        return None


def _trend_window_start(context: dict, window: str) -> date:
    end = _as_date(context["current"]["end"])
    if window == "last_7_days":
        return end - timedelta(days=6)
    if window == "current_billing_period":
        return _as_date(context["current"]["start"])
    return end - timedelta(days=13)


def _window_label(window: str) -> str:
    return {
        "last_7_days": "last 7 days",
        "last_14_days": "last 14 days",
        "current_billing_period": "current billing period",
    }.get(window, window.replace("_", " "))


def _trend_stats(context: dict, matcher, window: str) -> dict:
    end = _as_date(context["current"]["end"])
    start = _trend_window_start(context, window)
    half_life = float(context.get("trend_decay_half_life_days", 14.0))
    matched = [entry for entry in _timeline_entries(context) if matcher(entry)]
    recent = []
    decayed_score = 0.0
    for entry in matched:
        entry_day = _entry_date(entry)
        if not entry_day:
            continue
        age_days = max((end - entry_day).days, 0)
        decayed_score += 0.5 ** (age_days / max(half_life, 1.0))
        if start <= entry_day <= end:
            recent.append(entry)
    return {
        "recent_count": len(recent),
        "historical_count": len(matched),
        "decayed_event_score": round(decayed_score, 2),
        "window": _window_label(window),
    }


def _trend_is_active(stats: dict, thresholds: dict) -> bool:
    trigger = int(thresholds.get("trend_repeated_event_count", 2))
    recent_trigger = int(thresholds.get("trend_recent_event_count", 1))
    return (
        stats["historical_count"] >= trigger
        and stats["recent_count"] >= recent_trigger
        and stats["decayed_event_score"] >= recent_trigger
    )


def _trend_evidence(stats: dict, count_key: str) -> dict:
    return {
        "recent_event_count": stats["recent_count"],
        "historical_event_count": stats["historical_count"],
        "recency_window_used": stats["window"],
        "decayed_event_score": stats["decayed_event_score"],
        count_key: stats["historical_count"],
    }


def _trend_explanation(
    why_now: str,
    confidence_reason: str,
    financial_impact: str,
    evidence: dict,
    sources: list[str],
) -> DecisionExplanation:
    return DecisionExplanation(
        why_this_matters=why_now,
        evidence_used=[
            f"{_evidence_label(key)}: {_evidence_value(value)}"
            for key, value in evidence.items()
        ],
        financial_impact=financial_impact,
        confidence_reason=confidence_reason,
        data_sources_used=sources,
        freshness_completeness_note=(
            f"Trend analysis used the {evidence['recency_window_used']} window "
            f"and a decayed event score of {evidence['decayed_event_score']}."
        ),
    )


def peak_cost_reduction(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    trigger = thresholds["peak_cost_share_pct"]
    if summary["peak_cost_share_pct"] <= trigger:
        return None
    shift_kwh = thresholds["peak_shift_kwh_per_day"]
    shiftable_kwh = min(summary["buckets"]["peak"]["kwh"], shift_kwh * summary["reading_days"])
    savings = financial_engine.calculate_load_shift_savings(
        shiftable_kwh,
        context["import_rates"]["peak"],
        context["import_rates"]["super_off_peak"],
    )
    impact = financial_engine.financial_impact(billing_period=savings)
    finding = (
        f"Peak usage accounts for {summary['peak_cost_share_pct']:.1f}% of import cost "
        f"and currently costs ${summary['buckets']['peak']['cost']:.2f}."
    )
    return Decision(
        id="peak_cost_reduction",
        title="Reduce Peak-period imports",
        summary=finding,
        finding=finding,
        evidence={
            "peak_cost": summary["buckets"]["peak"]["cost"],
            "peak_cost_share_pct": summary["peak_cost_share_pct"],
            "shift_kwh_per_day": shift_kwh,
            "estimated_period_savings": savings,
        },
        financial_impact=impact,
        recommendation=f"Moving up to {shift_kwh:.1f} kWh/day from Peak to 9am-3pm could save about ${savings:.2f} this period.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context, "Tariff settings"),
        category="Tariffs",
        action_type="load_shift",
    )


def solar_self_consumption(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    if summary["self_consumption_pct"] >= thresholds["solar_self_consumption_pct"]:
        return None
    finding = (
        f"Solar self-consumption is {summary['self_consumption_pct']:.1f}% and "
        f"{summary['solar_export_pct']:.1f}% of generation is exported."
    )
    return Decision(
        id="solar_self_consumption",
        title="Use more solar at home",
        summary=finding,
        finding=finding,
        evidence={
            "self_consumption_pct": summary["self_consumption_pct"],
            "solar_export_pct": summary["solar_export_pct"],
        },
        financial_impact=_impact(),
        recommendation="Shift flexible loads such as appliances, hot water, or EV charging into the 9am-3pm solar window.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="Solar",
        action_type="load_shift",
    )


def ev_charging_optimisation(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    if summary["ev_charging_kwh"] <= thresholds["ev_detected_min_kwh"]:
        return None
    actual = context.get("ev_data_source") in {"home_assistant_ocpp", "ocular_export"}
    energy_label = "Actual EV charging" if actual else "Estimated overnight EV charging"
    peak_cost = financial_engine.cost_of_ev_session(
        summary["ev_charging_kwh"],
        context["import_rates"]["peak"],
    )
    observed_cost = summary["ev_charging_cost"]
    savings_vs_peak = financial_engine.calculate_cost_difference(peak_cost, observed_cost)
    if actual:
        breakdown = summary.get("actual_ev_tariff_breakdown") or {}
        dominant = max(
            ("super_off_peak", "overnight", "off_peak", "peak"),
            key=lambda bucket: (breakdown.get(bucket) or {}).get("kwh", 0),
        )
        total_kwh = sum((breakdown.get(bucket) or {}).get("kwh", 0) for bucket in breakdown)
        if total_kwh <= 0:
            window_message = "Charging was imported from actual EV session data."
        elif (breakdown.get(dominant) or {}).get("kwh", 0) / total_kwh < 0.5:
            window_message = "Charging was split across tariff windows."
        elif dominant == "super_off_peak":
            window_message = "Charging was mostly in the cheapest 9am-3pm EV window."
        elif dominant == "overnight":
            window_message = "Charging was mostly overnight."
        else:
            window_message = "Charging was mostly outside the cheapest EV windows."
        finding = (
            f"{energy_label} is {summary['ev_charging_kwh']:.2f} kWh with an observed "
            f"OCPP cost estimate of ${observed_cost:.2f}."
        )
        recommendation = (
            f"{window_message} The same charging at the Peak rate would cost about "
            f"${peak_cost:.2f}."
        )
    else:
        finding = (
            f"{energy_label} is {summary['ev_charging_kwh']:.2f} kWh, costing about "
            f"${observed_cost:.2f} at the overnight rate."
        )
        recommendation = (
            f"The same estimated charging at the Peak rate would cost about ${peak_cost:.2f}. "
            "Daytime EV charging is not included without charger session data."
        )
    return Decision(
        id="ev_charging_optimisation",
        title=energy_label,
        summary=finding,
        finding=finding,
        evidence={
            "estimated_ev_kwh": summary["ev_charging_kwh"],
            "observed_or_estimated_cost": observed_cost,
            "estimated_peak_cost": peak_cost,
            "estimated_savings_vs_peak": savings_vs_peak,
        },
        financial_impact=financial_engine.financial_impact(billing_period=savings_vs_peak),
        recommendation=recommendation,
        confidence="Medium",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context, "Tariff settings"),
        category="EV",
        action_type="tariff_window",
    )


def baseload_monitoring(context: dict, thresholds: dict) -> Optional[Decision]:
    current_baseload = context["overnight_baseload_watts"]
    expected = context["ev_baseline_watts"]
    trigger = expected * (1 + thresholds["baseload_excess_pct"] / 100)
    if current_baseload <= trigger:
        return None
    finding = (
        f"Typical overnight baseload is about {current_baseload:.0f} W versus the "
        f"configured {expected:.0f} W baseline."
    )
    return Decision(
        id="baseload_monitoring",
        title="Review overnight baseload",
        summary=finding,
        finding=finding,
        evidence={
            "current_baseload_watts": current_baseload,
            "expected_baseline_watts": expected,
            "excess_pct": (current_baseload / expected - 1) * 100 if expected else 0,
        },
        financial_impact=_impact(),
        recommendation="Check always-on devices, heating, hot water, networking equipment, and standby loads.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="Behaviour",
        action_type="investigate",
    )


def high_cost_alert(context: dict, thresholds: dict) -> Optional[Decision]:
    projected = context["projected_net_cost"]
    threshold = thresholds["high_cost_threshold"]
    if projected <= threshold:
        return None
    reading_days = context["current"]["reading_days"]
    if reading_days < 10:
        confidence = "Low"
    elif reading_days <= 20:
        confidence = "Medium"
    else:
        confidence = "High"
    actual_cost = context["current"]["net_cost"]
    priority = "High" if actual_cost > threshold or confidence != "Low" else "Medium"
    finding = (
        f"Actual net cost so far is ${actual_cost:.2f}; the projected cost by "
        f"{context['projected_period_end']} is ${projected:.2f}. "
        f"This is a {confidence.lower()}-confidence projection."
    )
    return Decision(
        id="high_cost_alert",
        title="Projected billing cost is high",
        summary=finding,
        finding=finding,
        evidence={
            "actual_cost_so_far": actual_cost,
            "projected_net_cost": projected,
            "projected_period_end": context["projected_period_end"],
            "projection_confidence": confidence.lower(),
            "projection_reading_days": reading_days,
            "configured_threshold": threshold,
            "projected_period_days": context["projected_period_days"],
        },
        financial_impact=context.get("projected_bill_impact") or _impact(billing_period=projected),
        recommendation=(
            f"Projection is based on {reading_days} days of data. "
            "Prioritise Peak-period load shifting and review high-import days before the next meter read."
        ),
        confidence=confidence,
        priority=priority,
        expires_at=context["projected_period_end"],
        data_sources_used=_data_sources(context, "Billing period settings"),
        category="Bills",
        action_type="review",
    )


def positive_reinforcement(context: dict, thresholds: dict) -> Optional[Decision]:
    summary = context["current"]
    wins = []
    if summary["peak_cost_share_pct"] < thresholds["positive_peak_share_pct"]:
        wins.append(f"Peak share is only {summary['peak_cost_share_pct']:.1f}%")
    if summary["self_consumption_pct"] > thresholds["positive_self_consumption_pct"]:
        wins.append(f"solar self-consumption is {summary['self_consumption_pct']:.1f}%")
    previous = context.get("previous")
    if previous and summary["average_daily_net_cost"] < previous["average_daily_net_cost"]:
        wins.append("average daily net cost is lower than the previous billing period")
    if not wins:
        return None
    finding = "; ".join(wins).capitalize() + "."
    return Decision(
        id="positive_reinforcement",
        title="You're doing well",
        summary=finding,
        finding=finding,
        evidence={"positive_signals": len(wins)},
        financial_impact=_impact(),
        recommendation="Keep the current habits going and continue using the low-cost and solar windows.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=_data_sources(context),
        category="General",
        action_type="maintain",
    )


def repeated_peak_usage_trend(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "high_peak_usage",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    latest_impact = _latest_timeline_impact(context, "high_peak_usage")
    finding = (
        f"High peak usage has appeared {stats['recent_count']} times in the "
        f"{stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Tariff settings")
    evidence = {
        **_trend_evidence(stats, "high_peak_usage_events"),
        "latest_recorded_impact": latest_impact or "not quantified",
    }
    confidence_reason = (
        f"Medium confidence because at least one high-peak event is recent and "
        f"{stats['historical_count']} matching events exist in persisted history."
    )
    return Decision(
        id="trend_repeated_peak_usage",
        title="Peak usage is becoming a pattern",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Review flexible loads that repeatedly land in Peak periods and prioritise shifting them to lower-cost windows.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Tariffs",
        action_type="trend_review",
        explanation=_trend_explanation(
            "This matters now because a recent high-peak event confirms the pattern is still active.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def bill_trajectory_trend(context: dict, thresholds: dict) -> Optional[Decision]:
    improved_stats = _trend_stats(
        context,
        lambda entry: entry.title == "Bill tracking improved",
        "current_billing_period",
    )
    worsened_stats = _trend_stats(
        context,
        lambda entry: entry.title == "Bill tracking worsened",
        "current_billing_period",
    )
    improved = improved_stats["historical_count"]
    worsened = worsened_stats["historical_count"]
    if improved == worsened:
        return None
    direction = "improving" if improved > worsened else "worsening"
    dominant_stats = improved_stats if direction == "improving" else worsened_stats
    if not _trend_is_active(dominant_stats, thresholds):
        return None
    finding = (
        f"Bill trajectory is {direction}: {dominant_stats['recent_count']} "
        f"{direction} events are in the {dominant_stats['window']}, with "
        f"{improved} improved and {worsened} worsened events recorded historically."
    )
    sources = _trend_sources("Period comparison", "FinancialEngine report output")
    evidence = {
        **_trend_evidence(dominant_stats, f"bill_tracking_{direction}_events"),
        "bill_tracking_improved_events": improved,
        "bill_tracking_worsened_events": worsened,
    }
    confidence_reason = (
        f"Medium confidence because the {direction} bill trend has recent "
        f"evidence in the {dominant_stats['window']} and more matching events than the opposite direction."
    )
    return Decision(
        id=f"trend_bill_trajectory_{direction}",
        title=f"Bill trajectory is {direction}",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation=(
            "Keep reinforcing the behaviours behind the improvement."
            if direction == "improving"
            else "Review recent high-cost days and recurring recommendations before this trend continues."
        ),
        confidence="Medium",
        priority="Low" if direction == "improving" else "Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Bills",
        action_type="trend_review",
        explanation=_trend_explanation(
            f"This matters now because the bill trajectory has recent {direction} evidence in the current billing period.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def recurring_ev_charging_win(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "ev_cheapest_tariff",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"EV charging has landed in the cheapest tariff window {stats['recent_count']} "
        f"times in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Actual EV session data", "Tariff settings")
    evidence = _trend_evidence(stats, "ev_cheapest_tariff_events")
    confidence_reason = (
        "High confidence because recurring low-cost EV charging is present in recent "
        "timeline history and comes from actual session/tariff evidence."
    )
    return Decision(
        id="trend_recurring_ev_charging_win",
        title="EV charging win is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Keep the EV schedule aligned with the cheapest tariff window and watch for any Peak-session regressions.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="EV",
        action_type="maintain",
        explanation=_trend_explanation(
            "This matters now because the low-cost EV charging pattern is still appearing recently, not only in old history.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def recurring_solar_utilisation_win(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: entry.event_type == "positive_outcome",
        "last_14_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"Positive solar/utilisation outcomes have appeared {stats['recent_count']} "
        f"times in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("SolarEdge interval data")
    evidence = _trend_evidence(stats, "positive_outcome_events")
    confidence_reason = (
        "High confidence because the utilisation win appears in recent timeline history "
        "and is backed by interval evidence."
    )
    return Decision(
        id="trend_recurring_solar_utilisation_win",
        title="Solar utilisation win is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Keep flexible loads in solar and low-cost windows so the recurring positive pattern continues.",
        confidence="High",
        priority="Low",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="Solar",
        action_type="maintain",
        explanation=_trend_explanation(
            "This matters now because a positive solar/utilisation pattern is recurring in recent history.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


def repeated_estimated_data_warning(context: dict, thresholds: dict) -> Optional[Decision]:
    stats = _trend_stats(
        context,
        lambda entry: "Estimated EV charging" in entry.evidence_sources,
        "last_7_days",
    )
    if not _trend_is_active(stats, thresholds):
        return None
    finding = (
        f"Estimated/fallback EV data has been used in {stats['recent_count']} "
        f"timeline events in the {stats['window']} and {stats['historical_count']} times historically."
    )
    sources = _trend_sources("Estimated EV charging")
    evidence = _trend_evidence(stats, "estimated_ev_timeline_events")
    confidence_reason = (
        "Medium confidence because estimated/fallback data appears in recent timeline "
        "history, but the underlying EV sessions are not fully measured."
    )
    return Decision(
        id="trend_repeated_estimated_data_warning",
        title="Estimated data is recurring",
        summary=finding,
        finding=finding,
        evidence={**evidence, "confidence_reason": confidence_reason},
        financial_impact=_impact(),
        recommendation="Connect or refresh actual EV session data so future decisions rely less on fallback estimates.",
        confidence="Medium",
        priority="Medium",
        expires_at=context.get("projected_period_end"),
        data_sources_used=sources,
        category="General",
        action_type="data_quality",
        explanation=_trend_explanation(
            "This matters now because fallback EV data is still being used in the last 7 days.",
            confidence_reason,
            "Not quantified by the current Decision Engine output.",
            evidence,
            sources,
        ),
    )


RULES: tuple[Rule, ...] = (
    high_cost_alert,
    peak_cost_reduction,
    solar_self_consumption,
    ev_charging_optimisation,
    baseload_monitoring,
    positive_reinforcement,
    repeated_peak_usage_trend,
    bill_trajectory_trend,
    recurring_ev_charging_win,
    recurring_solar_utilisation_win,
    repeated_estimated_data_warning,
)


def generate_decisions(
    context: dict,
    thresholds: dict,
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Decision]:
    recommendations = generate_shared_recommendations(
        context,
        thresholds,
        include_inactive=include_inactive,
        household_context=household_context,
    )
    return [
        _validate_decision(decision)
        for decision in recommendations_to_decisions(
            recommendations,
            expires_at=context.get("projected_period_end"),
        )
    ]


def build_decision_context(
    current_summary: dict,
    settings,
    data_engine,
    previous_summary: Optional[dict] = None,
    household_context: Optional[HouseholdContext] = None,
) -> dict:
    projected_days = current_summary["supply_days"]
    projected_end = _as_date(current_summary["end"])
    start = _as_date(current_summary["start"])
    end = _as_date(current_summary["end"])
    overnight_baseload = data_engine.get_overnight_baseload_watts(start, end)
    timeline_history = data_engine.get_timeline_entries(limit=100)
    if household_context is None and hasattr(data_engine, "get_household_context"):
        household_context = data_engine.get_household_context()

    if settings.current_billing_period_start and settings.current_billing_period_end:
        projected_end = settings.current_billing_period_end
        projected_days = (projected_end - settings.current_billing_period_start).days + 1
    elif (
        settings.current_billing_period_start
        and settings.previous_billing_period_start
        and settings.previous_billing_period_end
    ):
        projected_days = (
            settings.previous_billing_period_end - settings.previous_billing_period_start
        ).days + 1
        projected_end = settings.current_billing_period_start + timedelta(days=projected_days - 1)

    projected_bill = financial_engine.project_bill(current_summary, settings)
    return {
        "current": current_summary,
        "previous": previous_summary,
        "import_rates": settings.import_rates,
        "ev_data_source": current_summary["ev_charging_source"],
        "ev_baseline_watts": settings.ev_baseline_watts,
        "overnight_baseload_watts": overnight_baseload,
        "timeline_history": timeline_history,
        "household_id": household_context.household.household_id if household_context else None,
        "projected_period_days": projected_days,
        "projected_period_end": projected_end.isoformat(),
        "projected_net_cost": projected_bill[0],
        "projected_bill_impact": projected_bill[3],
    }


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def generate_decisions_from_data_engine(
    current_summary: dict,
    settings,
    data_engine,
    thresholds: dict,
    previous_summary: Optional[dict] = None,
    include_inactive: bool = False,
    household_context: Optional[HouseholdContext] = None,
) -> list[Decision]:
    recommendations = generate_shared_recommendations_from_data_engine(
        current_summary,
        settings,
        data_engine,
        thresholds,
        previous_summary=previous_summary,
        include_inactive=include_inactive,
        household_context=household_context,
    )
    expires_at = current_summary.get("end")
    return [_validate_decision(decision) for decision in recommendations_to_decisions(recommendations, expires_at=str(expires_at))]


def generate_recommendations(
    context: dict,
    thresholds: dict,
    household_context: Optional[HouseholdContext] = None,
) -> list[dict]:
    return [
        decision.as_dict()
        for decision in generate_decisions(context, thresholds, household_context=household_context)
    ]


def generate_ev_session_decisions(sessions: list[dict], import_rates: dict) -> list[Decision]:
    return [
        _validate_decision(decision)
        for decision in recommendations_to_decisions(
            generate_ev_session_recommendations(sessions, import_rates)
        )
    ]
