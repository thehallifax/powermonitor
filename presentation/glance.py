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
    energy_score: Optional[int]
    energy_score_label: str
    bill_current: float
    bill_projected: float
    bill_projection_basis: str
    solar_status: str
    freshness: str
    last_updated: str
    confidence: str

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
) -> GlanceState:
    bucket = str(charging_advice.supporting_evidence.get("current_tariff_bucket") or current_tariff_bucket(now))
    status = {
        "CHARGE_NOW": "CHARGE NOW",
        "WAIT": "WAIT",
        "AVOID": "AVOID",
        "UNKNOWN": "DATA UNAVAILABLE",
    }[charging_advice.status]
    tone = {
        "CHARGE_NOW": "charge",
        "WAIT": "wait",
        "AVOID": "avoid",
        "UNKNOWN": "unavailable",
    }[charging_advice.status]

    return GlanceState(
        status=status,
        tone=tone,
        current_recommendation=charging_advice.headline,
        reason=charging_advice.explanation,
        current_tariff=charging_advice.tariff_now_label or TARIFF_LABELS[bucket],
        current_tariff_bucket=bucket,
        next_window=charging_advice.next_window or next_better_window(now, bucket),
        energy_score=energy_score.score if energy_score else None,
        energy_score_label=energy_score.label if energy_score else "Unavailable",
        bill_current=float(getattr(billing_summary, "net_cost", 0.0) or 0.0),
        bill_projected=float(projected_bill),
        bill_projection_basis=bill_projection_basis,
        solar_status=str(charging_advice.supporting_evidence.get("solar_status") or "Solar unavailable"),
        freshness=charging_advice.data_freshness,
        last_updated=now.astimezone().isoformat(timespec="seconds"),
        confidence=charging_advice.confidence,
    )
