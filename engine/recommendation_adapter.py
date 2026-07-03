#!/usr/bin/env python3
"""Presentation adapter from HouseholdDecision to user recommendations."""

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .models import FinancialImpact


@dataclass(frozen=True)
class RecommendationPriority:
    value: str
    label: str
    rank: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationCategory:
    value: str
    label: str
    icon: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationReason:
    code: str
    summary: str
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationEvidence:
    source: str
    signal: str
    value: Any = None
    quality: str = "UNKNOWN"
    observed_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationPresentation:
    recommendation_id: str
    decision_id: str
    household_id: str
    title: str
    summary: str
    recommended_action: str
    rationale: str
    what_happened: str
    why_we_think_this: str
    what_to_check: str
    if_ignored: str
    confidence: str
    evidence: list[RecommendationEvidence]
    urgency: RecommendationPriority
    category: RecommendationCategory
    icon: str
    display_priority: int
    reasons: list[RecommendationReason] = field(default_factory=list)
    financial_impact: Optional[FinancialImpact] = None
    decision: Optional[dict[str, Any]] = None
    advisor_explanation: Optional[dict[str, Any]] = None
    state_change_ids: list[str] = field(default_factory=list)
    household_state: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    behaviour: Optional[dict[str, Any]] = None
    data_sources_used: list[str] = field(default_factory=list)
    status: str = "active"
    valid_until: Optional[str] = None

    @property
    def id(self) -> str:
        return self.recommendation_id

    @property
    def priority_score(self) -> int:
        return self.display_priority

    @property
    def severity(self) -> str:
        return {
            "low": "green",
            "medium": "amber",
            "high": "red",
            "critical": "red",
        }.get(self.urgency.value, "amber")

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "id": self.recommendation_id,
            "decision_id": self.decision_id,
            "household_id": self.household_id,
            "title": self.title,
            "summary": self.summary,
            "recommended_action": self.recommended_action,
            "action": self.recommended_action,
            "rationale": self.rationale,
            "what_happened": self.what_happened,
            "why_we_think_this": self.why_we_think_this,
            "what_to_check": self.what_to_check,
            "if_ignored": self.if_ignored,
            "confidence": self.confidence,
            "evidence": [item.as_dict() for item in self.evidence],
            "urgency": self.urgency.as_dict(),
            "priority": self.urgency.value,
            "category": self.category.as_dict(),
            "category_label": self.category.label,
            "icon": self.icon,
            "display_priority": self.display_priority,
            "priority_score": self.display_priority,
            "reasons": [item.as_dict() for item in self.reasons],
            "financial_impact": self.financial_impact.as_dict() if self.financial_impact else None,
            "decision": self.decision,
            "advisor_explanation": self.advisor_explanation,
            "state_change_ids": list(self.state_change_ids),
            "household_state": self.household_state,
            "context": self.context,
            "behaviour": self.behaviour,
            "data_sources_used": list(self.data_sources_used),
            "status": self.status,
            "valid_until": self.valid_until,
            "severity": self.severity,
        }


class RecommendationAdapter:
    """Convert HouseholdDecision objects into presentation recommendations."""

    def adapt(
        self,
        decisions: list[Any],
        *,
        advisor_explanations: Optional[list[Any]] = None,
        household_state: Optional[Any] = None,
        context: Optional[Any] = None,
        behaviour: Optional[Any] = None,
    ) -> list[RecommendationPresentation]:
        advisor_by_decision = {
            str(_get(advisor, "decision_id")): advisor
            for advisor in advisor_explanations or []
            if _get(advisor, "decision_id")
        }
        actionable_decisions = [
            decision for decision in decisions
            if str(_get(decision, "decision_type") or _get(decision, "type") or "") != "NO_ACTION"
        ]
        presentations = [
            self.adapt_one(
                decision,
                advisor_explanation=advisor_by_decision.get(str(_get(decision, "decision_id"))),
                household_state=household_state,
                context=context,
                behaviour=behaviour,
            )
            for decision in actionable_decisions
        ]
        return sorted(presentations, key=lambda item: (-item.display_priority, item.title))

    def adapt_one(
        self,
        decision: Any,
        *,
        advisor_explanation: Optional[Any] = None,
        household_state: Optional[Any] = None,
        context: Optional[Any] = None,
        behaviour: Optional[Any] = None,
    ) -> RecommendationPresentation:
        decision_type = str(_get(decision, "decision_type") or _get(decision, "type") or "NO_ACTION")
        decision_id = str(_get(decision, "decision_id") or decision_type.lower())
        reasons = [_reason(item) for item in list(_get(decision, "reasons") or [])]
        evidence = [_evidence(item) for item in list(_get(decision, "evidence") or [])]
        primary_reason = reasons[0].summary if reasons else ""
        outcome = _get(decision, "outcome") or {}
        action = _plain_wording(str(_get(outcome, "expected_effect") or _default_action(decision_type)))
        title = _title(decision_type)
        summary = _plain_wording(primary_reason or action)
        rationale = _plain_wording(" ".join(item.summary for item in reasons).strip() or summary)
        impact = _financial_impact(_get(decision, "financial_impact"))
        what_happened = _what_happened(decision_type, decision, summary, household_state, behaviour)
        why_we_think_this = _why_we_think_this(decision_type, evidence, rationale, household_state, context, behaviour)
        what_to_check = _what_to_check(decision_type, household_state, context, behaviour)
        if_ignored = _if_ignored(decision_type, impact)
        urgency = _priority(_get(decision, "priority"))
        category = _category(decision_type)
        confidence = _display_confidence(_get(decision, "confidence"))
        decision_payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
        advisor_payload = advisor_explanation.as_dict() if hasattr(advisor_explanation, "as_dict") else advisor_explanation
        return RecommendationPresentation(
            recommendation_id=f"decision:{decision_id}",
            decision_id=decision_id,
            household_id=str(_get(decision, "household_id") or "default_household"),
            title=title,
            summary=summary,
            recommended_action=action,
            rationale=rationale,
            what_happened=what_happened,
            why_we_think_this=why_we_think_this,
            what_to_check=what_to_check,
            if_ignored=if_ignored,
            confidence=confidence,
            evidence=evidence,
            urgency=urgency,
            category=category,
            icon=category.icon,
            display_priority=_display_priority(decision_type, urgency, confidence, impact),
            reasons=reasons,
            financial_impact=impact,
            decision=decision_payload,
            advisor_explanation=advisor_payload,
            state_change_ids=list(_get(decision, "related_state_change_ids") or []),
            household_state=household_state.as_dict() if hasattr(household_state, "as_dict") else household_state,
            context=context.as_dict() if hasattr(context, "as_dict") else context,
            behaviour=behaviour.as_dict() if hasattr(behaviour, "as_dict") else behaviour,
            data_sources_used=list(_get(decision, "data_sources_used") or ["Household data"]),
            status=str(_get(decision, "status") or "active"),
            valid_until=_get(decision, "valid_until"),
        )


def adapt_recommendations(
    decisions: list[Any],
    *,
    advisor_explanations: Optional[list[Any]] = None,
    household_state: Optional[Any] = None,
    context: Optional[Any] = None,
    behaviour: Optional[Any] = None,
) -> list[RecommendationPresentation]:
    return RecommendationAdapter().adapt(
        decisions,
        advisor_explanations=advisor_explanations,
        household_state=household_state,
        context=context,
        behaviour=behaviour,
    )


def _reason(value: Any) -> RecommendationReason:
    return RecommendationReason(
        code=str(_get(value, "code") or "decision_reason"),
        summary=str(_get(value, "summary") or ""),
        weight=float(_get(value, "weight") or 1.0),
    )


def _evidence(value: Any) -> RecommendationEvidence:
    return RecommendationEvidence(
        source=str(_get(value, "source") or "Household data"),
        signal=str(_get(value, "signal") or "decision"),
        value=_get(value, "value"),
        quality=str(_get(value, "quality") or "UNKNOWN"),
        observed_at=_get(value, "observed_at"),
    )


def _priority(value: Any) -> RecommendationPriority:
    text = str(value or "low").lower()
    rank = {"critical": 100, "high": 80, "medium": 55, "low": 25}.get(text, 25)
    return RecommendationPriority(text, text.title(), rank)


def _category(decision_type: str) -> RecommendationCategory:
    value, label, icon = {
        "CHARGE_NOW": ("ev", "EV Charging", "plug-zap"),
        "WAIT_FOR_CHEAPER_WINDOW": ("ev", "EV Charging", "clock"),
        "WAIT_FOR_SOLAR": ("solar", "Solar", "sun"),
        "REVIEW_BILLING": ("billing", "Bills", "receipt"),
        "MONITOR_USAGE": ("monitoring", "Monitoring", "activity"),
        "INVESTIGATE_ANOMALY": ("monitoring", "Monitoring", "search"),
        "REVIEW_PROVIDER": ("monitoring", "Monitoring", "wifi-alert"),
        "REVIEW_SETUP": ("setup", "Setup", "settings"),
        "DATA_INSUFFICIENT": ("data_quality", "Data Quality", "database"),
        "NO_ACTION": ("general", "General", "check-circle"),
    }.get(decision_type, ("general", "General", "circle"))
    return RecommendationCategory(value, label, icon)


def _title(decision_type: str) -> str:
    return {
        "CHARGE_NOW": "Charging window is favourable",
        "WAIT_FOR_CHEAPER_WINDOW": "Wait for a cheaper charging window",
        "WAIT_FOR_SOLAR": "Wait for stronger solar evidence",
        "REVIEW_BILLING": "Review current bill trajectory",
        "MONITOR_USAGE": "Monitor household usage",
        "INVESTIGATE_ANOMALY": "Potential issue detected",
        "REVIEW_PROVIDER": "Review provider health",
        "REVIEW_SETUP": "Finish required setup",
        "DATA_INSUFFICIENT": "Improve data quality",
        "NO_ACTION": "No immediate household action",
    }.get(decision_type, decision_type.replace("_", " ").title())


def _default_action(decision_type: str) -> str:
    return {
        "DATA_INSUFFICIENT": "We don't yet have enough information to make a recommendation.",
        "NO_ACTION": "Continue monitoring current household state.",
        "MONITOR_USAGE": "Keep monitoring this pattern before taking action.",
        "INVESTIGATE_ANOMALY": "Review the supporting data before taking action.",
    }.get(decision_type, "Review this household decision.")


def _what_happened(
    decision_type: str,
    decision: Any,
    summary: str,
    household_state: Optional[Any],
    behaviour: Optional[Any],
) -> str:
    if decision_type == "DATA_INSUFFICIENT":
        return "PowerMonitor does not yet have enough verified household data to make a useful recommendation."
    if decision_type == "NO_ACTION":
        return "Nothing in the current household data needs action right now."
    if decision_type == "REVIEW_BILLING":
        projected = _money_value(_get(_get(decision, "financial_impact") or {}, "billing_period"))
        if projected:
            return f"Your bill projection is worth checking. Estimated impact: {projected}."
        return "Your bill trajectory is worth checking against your usual spending pattern."
    if decision_type == "INVESTIGATE_ANOMALY":
        baseload = _nested_number(behaviour, "baseload", "overnight_baseload_watts")
        if baseload:
            return f"Your overnight electricity use averages about {baseload:.0f} W, and the latest pattern looks different from normal."
        return "PowerMonitor found a household pattern that looks different from your normal behaviour."
    if decision_type in {"CHARGE_NOW", "WAIT_FOR_CHEAPER_WINDOW", "WAIT_FOR_SOLAR"}:
        tariff = _nested_value(household_state, "tariff", "active_label")
        if tariff:
            return f"The current tariff window is {tariff}, so charging timing matters."
        return "The current charging decision depends on tariff timing and available household evidence."
    if decision_type == "REVIEW_PROVIDER":
        return "One of the data sources PowerMonitor depends on may need attention."
    if decision_type == "REVIEW_SETUP":
        return "Some setup is still missing, so PowerMonitor cannot explain every household pattern yet."
    return summary


def _why_we_think_this(
    decision_type: str,
    evidence: list[RecommendationEvidence],
    rationale: str,
    household_state: Optional[Any],
    context: Optional[Any],
    behaviour: Optional[Any],
) -> str:
    if decision_type == "DATA_INSUFFICIENT":
        return "The available readings are missing, stale, estimated, or incomplete."
    if decision_type == "INVESTIGATE_ANOMALY":
        baseline = _nested_number(behaviour, "baseload", "overnight_baseload_watts")
        variance = _nested_number(behaviour, "baseload", "normal_variance_watts")
        if baseline and variance:
            return f"This is being compared with your learned overnight baseline of about {baseline:.0f} W, allowing for normal variation of about {variance:.0f} W."
    if decision_type == "REVIEW_BILLING":
        billing = _get(context, "billing")
        if billing:
            return f"The billing context is {str(billing).replace('_', ' ').lower()}, based on the current bill period and available readings."
    if evidence:
        first = evidence[0]
        value = first.value if first.value is not None else first.signal
        return f"PowerMonitor is using {first.source} evidence: {value}."
    return rationale


def _what_to_check(
    decision_type: str,
    household_state: Optional[Any],
    context: Optional[Any],
    behaviour: Optional[Any],
) -> str:
    del household_state, context
    if decision_type == "DATA_INSUFFICIENT":
        return "Check live data collection, provider sign-in, and whether enough days of readings have been collected."
    if decision_type == "INVESTIGATE_ANOMALY":
        return "Check pool pump schedules, hot water, heating or cooling, fridges/freezers, networking gear, and appliances left running overnight."
    if decision_type == "REVIEW_BILLING":
        return "Check recent high-cost days, peak-period use, EV charging sessions, and whether this bill period has unusual usage."
    if decision_type == "WAIT_FOR_CHEAPER_WINDOW":
        return "Check whether EV charging, hot water, dishwasher, washing machine, or other flexible loads can wait."
    if decision_type == "WAIT_FOR_SOLAR":
        return "Check live solar production before starting flexible daytime loads."
    if decision_type == "CHARGE_NOW":
        return "Check whether the EV or another flexible load is ready to use this low-cost window."
    if decision_type == "REVIEW_PROVIDER":
        return "Check the provider connection, last successful sync, and whether credentials need refreshing."
    if decision_type == "REVIEW_SETUP":
        return "Check the setup tasks PowerMonitor marks as incomplete."
    if decision_type == "NO_ACTION":
        return "Nothing specific. Keep collecting data so PowerMonitor can spot changes."
    preferred = _nested_value(behaviour, "ev", "preferred_charging_window")
    if preferred:
        return f"Check whether today's behaviour differs from your usual {preferred} pattern."
    return "Check the supporting evidence and recent household activity."


def _if_ignored(decision_type: str, impact: Optional[FinancialImpact]) -> str:
    impact_text = _impact_text(impact)
    if decision_type == "DATA_INSUFFICIENT":
        return "PowerMonitor may stay cautious and avoid making confident recommendations."
    if decision_type == "NO_ACTION":
        return "Nothing needs action now; PowerMonitor will keep watching for changes."
    if decision_type == "REVIEW_BILLING":
        return f"The bill may keep tracking above normal{impact_text}." if impact_text else "The bill may keep tracking above normal without an early warning."
    if decision_type in {"WAIT_FOR_CHEAPER_WINDOW", "CHARGE_NOW", "WAIT_FOR_SOLAR"}:
        return f"Flexible loads may cost more than necessary{impact_text}." if impact_text else "Flexible loads may cost more than necessary."
    if decision_type == "INVESTIGATE_ANOMALY":
        return f"An unusual load could keep adding cost{impact_text}." if impact_text else "An unusual load could keep adding cost or hide a device problem."
    if decision_type in {"REVIEW_PROVIDER", "REVIEW_SETUP"}:
        return "Future recommendations may be delayed, less specific, or suppressed until the data improves."
    return f"The pattern may continue{impact_text}." if impact_text else "The pattern may continue."


def _plain_wording(value: str) -> str:
    replacements = {
        "Investigate the active anomaly using its evidence trail.": "Review the supporting data before taking action.",
        "Investigate the active anomaly": "Review the potential issue",
        "evidence trail": "supporting data",
        "anomaly": "potential issue",
        "Anomaly": "Potential issue",
    }
    text = str(value or "")
    for technical, plain in replacements.items():
        text = text.replace(technical, plain)
    return text


def _impact_text(impact: Optional[FinancialImpact]) -> str:
    if not impact:
        return ""
    for value, suffix in (
        (impact.today, " today"),
        (impact.billing_period, " this billing period"),
        (impact.yearly, " per year"),
    ):
        if value is not None:
            return f" (around ${float(value):,.2f}{suffix})"
    return ""


def _money_value(value: Any) -> Optional[str]:
    try:
        if value is None:
            return None
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return None


def _nested_value(source: Any, *keys: str) -> Any:
    current = source
    for key in keys:
        current = _get(current, key)
        if current is None:
            return None
    return current


def _nested_number(source: Any, *keys: str) -> Optional[float]:
    value = _nested_value(source, *keys)
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_priority(decision_type: str, urgency: RecommendationPriority, confidence: str, impact: Optional[FinancialImpact]) -> int:
    confidence_score = {"Very High": 18, "High": 14, "Medium": 8, "Low": 2}.get(confidence, 2)
    impact_score = 0
    if impact:
        impact_score = min(30, int(abs(float(impact.billing_period or impact.today or impact.yearly or 0))))
    type_bonus = {
        "INVESTIGATE_ANOMALY": 15,
        "CHARGE_NOW": 12,
        "WAIT_FOR_CHEAPER_WINDOW": 10,
        "REVIEW_BILLING": 8,
        "NO_ACTION": -20,
    }.get(decision_type, 0)
    return max(0, min(100, urgency.rank + confidence_score + impact_score + type_bonus))


def _financial_impact(value: Any) -> Optional[FinancialImpact]:
    if isinstance(value, FinancialImpact):
        return value
    if not isinstance(value, dict):
        return None
    return FinancialImpact(
        today=value.get("today"),
        billing_period=value.get("billing_period"),
        yearly=value.get("yearly"),
        currency=value.get("currency", "AUD"),
        basis=value.get("basis"),
    )


def _display_confidence(value: Any) -> str:
    text = str(value or "UNKNOWN").replace("_", " ").title()
    return "Very High" if text == "Very High" else "High" if text == "High" else "Medium" if text == "Medium" else "Low"


def _get(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    value = getattr(source, key, default)
    return getattr(value, "value", value)
