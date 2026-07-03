#!/usr/bin/env python3
"""Glance Mode presentation mapping for wall and kiosk displays."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from engine.charging_advisor import TARIFF_LABELS, current_tariff_bucket, next_better_window
from engine.models import ChargingAdvice, EnergyScore


@dataclass(frozen=True)
class GlanceState:
    status: str
    tone: str
    current_recommendation: str
    reason: str
    current_tariff: str
    current_tariff_bucket: str
    next_window: str
    lowest_cost_window: str
    energy_score: Optional[int]
    energy_score_label: str
    bill_current: float
    bill_projected: float
    bill_projection_basis: str
    solar_status: str
    freshness: str
    last_updated: str
    confidence: str
    evidence_quality: str
    evidence_reason: str
    context: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_glance_state(
    *,
    billing_summary: Any,
    charging_advice: ChargingAdvice,
    energy_score: Optional[EnergyScore],
    projected_bill: float,
    bill_projection_basis: str,
    now: datetime,
    context: Optional[Any] = None,
    recommendation_presentation: Optional[Any] = None,
) -> GlanceState:
    advisor = charging_advice.advisor
    bucket = str(charging_advice.supporting_evidence.get("current_tariff_bucket") or current_tariff_bucket(now))
    status = {
        "CHARGE_NOW": "CHARGE NOW",
        "WAIT": "WAIT",
        "AVOID": "AVOID",
        "UNKNOWN": "NO DATA AVAILABLE" if not _has_usable_household_information(
            billing_summary=billing_summary,
            energy_score=energy_score,
            projected_bill=projected_bill,
            recommendation_presentation=recommendation_presentation,
        ) else "LIVE DATA UNAVAILABLE",
    }[advisor.status]
    tone = {
        "CHARGE_NOW": "charge",
        "WAIT": "wait",
        "AVOID": "avoid",
        "UNKNOWN": "unavailable",
    }[advisor.status]
    evidence_summary = getattr(advisor, "evidence_summary", None)
    evidence_quality = evidence_summary.evidence_quality.value if evidence_summary else "UNKNOWN"
    evidence_reason = evidence_summary.reason if evidence_summary else advisor.confidence_reason
    wait_minutes = charging_advice.recommended_wait_minutes
    simple_reason = glance_reason(status, wait_minutes, charging_advice.next_lower_cost_window, advisor.summary)
    simple_recommendation = glance_recommendation(status, wait_minutes, charging_advice.next_lower_cost_window, advisor.headline)
    if recommendation_presentation is not None:
        simple_reason = str(getattr(recommendation_presentation, "rationale", None) or simple_reason)
        simple_recommendation = str(
            getattr(recommendation_presentation, "recommended_action", None)
            or getattr(recommendation_presentation, "title", None)
            or simple_recommendation
        )

    return GlanceState(
        status=status,
        tone=tone,
        current_recommendation=simple_recommendation,
        reason=simple_reason,
        current_tariff=charging_advice.tariff_now_label or TARIFF_LABELS[bucket],
        current_tariff_bucket=bucket,
        next_window=charging_advice.next_lower_cost_window or charging_advice.next_window or next_better_window(now, bucket),
        lowest_cost_window=charging_advice.lowest_cost_window or charging_advice.next_window or next_better_window(now, bucket),
        energy_score=energy_score.score if energy_score else None,
        energy_score_label=energy_score.label if energy_score else "Unavailable",
        bill_current=float(getattr(billing_summary, "net_cost", 0.0) or 0.0),
        bill_projected=float(projected_bill),
        bill_projection_basis=bill_projection_basis,
        solar_status=str(charging_advice.supporting_evidence.get("solar_status") or "Solar unavailable"),
        freshness=advisor.freshness,
        last_updated=now.astimezone().isoformat(timespec="seconds"),
        confidence=advisor.confidence,
        evidence_quality=evidence_quality,
        evidence_reason=evidence_reason,
        context=context.as_dict() if hasattr(context, "as_dict") else context,
    )


def glance_reason(
    status: str,
    wait_minutes: Optional[int],
    next_window: Optional[str],
    fallback: str,
) -> str:
    if status == "WAIT" and wait_minutes is not None and wait_minutes > 0:
        return f"Wait {wait_minutes} minutes for cheaper charging."
    if status == "AVOID" and wait_minutes is not None and wait_minutes > 0:
        return f"Peak tariff now. Cheaper charging starts in {wait_minutes} minutes."
    if status == "CHARGE NOW":
        return "Use this window for charging or flexible loads."
    if status == "LIVE DATA UNAVAILABLE":
        return "Live data unavailable. Using recent household information."
    if status == "NO DATA AVAILABLE":
        return "No usable household data is available yet."
    return next_window or fallback


def glance_recommendation(
    status: str,
    wait_minutes: Optional[int],
    next_window: Optional[str],
    fallback: str,
) -> str:
    if status == "WAIT" and wait_minutes is not None and wait_minutes > 0:
        return f"Wait {wait_minutes} minutes, then charge."
    if status == "AVOID":
        return f"Use {next_window} instead if the load can wait." if next_window else "Use tariff timing before starting flexible loads."
    if status == "CHARGE NOW":
        return "Charge now if the load is flexible."
    if status == "LIVE DATA UNAVAILABLE":
        return "Unable to verify live conditions. Use recent household information with caution."
    if status == "NO DATA AVAILABLE":
        return "Set up a data source before relying on guidance."
    return fallback


def _has_usable_household_information(
    *,
    billing_summary: Any,
    energy_score: Optional[EnergyScore],
    projected_bill: float,
    recommendation_presentation: Optional[Any],
) -> bool:
    if energy_score is not None:
        return True
    if recommendation_presentation is not None:
        return True
    if int(getattr(billing_summary, "intervals", 0) or 0) > 0:
        return True
    if float(getattr(billing_summary, "net_cost", 0.0) or 0.0) > 0:
        return True
    return float(projected_bill or 0.0) > 0
