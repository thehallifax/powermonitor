#!/usr/bin/env python3
"""Glance Mode presentation mapping for wall and kiosk displays."""

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any, Optional

from engine.models import Decision, EnergyScore

TARIFF_LABELS = {
    "overnight": "Overnight",
    "off_peak": "Off peak",
    "super_off_peak": "Super off peak",
    "peak": "Peak",
}


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
    today_summary: Any,
    billing_summary: Any,
    recommendations: list[Decision],
    energy_score: Optional[EnergyScore],
    health: dict[str, Any],
    projected_bill: float,
    bill_projection_basis: str,
    now: datetime,
) -> GlanceState:
    bucket = current_tariff_bucket(now)
    available, freshness, confidence = data_is_available(today_summary, billing_summary, health)
    top_decision = current_decision(recommendations)
    solar_status, has_solar_surplus = solar_signal(today_summary)

    if not available:
        status = "DATA UNAVAILABLE"
        tone = "unavailable"
        reason = "Low confidence / missing data"
    elif bucket == "super_off_peak":
        status = "CHARGE NOW"
        tone = "charge"
        reason = "Super off-peak window"
    elif has_solar_surplus:
        status = "CHARGE NOW"
        tone = "charge"
        reason = "Solar surplus"
    elif bucket == "peak":
        status = "AVOID"
        tone = "avoid"
        reason = "Peak period"
    else:
        status = "WAIT"
        tone = "wait"
        reason = "A cheaper window is coming"

    return GlanceState(
        status=status,
        tone=tone,
        current_recommendation=(
            top_decision.recommendation
            if top_decision
            else "No active Decision Engine recommendation right now."
        ),
        reason=reason,
        current_tariff=TARIFF_LABELS[bucket],
        current_tariff_bucket=bucket,
        next_window=next_better_window(now, bucket),
        energy_score=energy_score.score if energy_score else None,
        energy_score_label=energy_score.label if energy_score else "Unavailable",
        bill_current=float(getattr(billing_summary, "net_cost", 0.0) or 0.0),
        bill_projected=float(projected_bill),
        bill_projection_basis=bill_projection_basis,
        solar_status=solar_status,
        freshness=freshness,
        last_updated=now.astimezone().isoformat(timespec="seconds"),
        confidence=confidence,
    )


def current_decision(recommendations: list[Decision]) -> Optional[Decision]:
    actionable = [item for item in recommendations if item.action_type != "maintain"]
    return actionable[0] if actionable else (recommendations[0] if recommendations else None)


def data_is_available(today_summary: Any, billing_summary: Any, health: dict[str, Any]) -> tuple[bool, str, str]:
    status = str(health.get("status") or "").lower()
    intervals = int(getattr(today_summary, "intervals", 0) or 0)
    billing_intervals = int(getattr(billing_summary, "intervals", 0) or 0)
    if status in {"no readings", "invalid reading timestamp", "solaredge session expired"}:
        return False, status or "unavailable", "Low"
    if status not in {"ok"}:
        return False, status or "unavailable", "Low"
    if intervals <= 0 and billing_intervals <= 0:
        return False, "No interval readings available", "Low"
    if intervals <= 0:
        return False, "No readings for today yet", "Low"
    age = health.get("report_age_minutes")
    if age is not None and float(age) > 180:
        return False, f"Report age {float(age):.0f} minutes", "Low"
    return True, "Fresh", "High"


def solar_signal(today_summary: Any) -> tuple[str, bool]:
    generated = float(getattr(today_summary, "solar_generated_kwh", 0.0) or 0.0)
    exported = float(getattr(today_summary, "solar_exported_kwh", 0.0) or 0.0)
    export_pct = float(getattr(today_summary, "solar_export_pct", 0.0) or 0.0)
    if generated <= 0:
        return "Solar unavailable", False
    if exported >= 0.5 and export_pct >= 15:
        return f"Exporting solar: {exported:.1f} kWh today", True
    return f"Solar generating: {generated:.1f} kWh today", False


def current_tariff_bucket(now: datetime) -> str:
    current_time = now.astimezone().time()
    if current_time >= time(23, 0) or current_time < time(6, 0):
        return "overnight"
    if time(6, 0) <= current_time < time(9, 0):
        return "off_peak"
    if time(9, 0) <= current_time < time(15, 0):
        return "super_off_peak"
    if time(15, 0) <= current_time < time(21, 0):
        return "peak"
    return "off_peak"


def next_better_window(now: datetime, current_bucket: str) -> str:
    local_now = now.astimezone()
    if current_bucket == "super_off_peak":
        return "Now until 3:00 PM"

    next_super = local_now.replace(hour=9, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(15, 0):
        next_super += timedelta(days=1)
    elif local_now.time() >= time(9, 0):
        return "Now until 3:00 PM"

    next_overnight = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
    if local_now.time() >= time(23, 0):
        next_overnight += timedelta(days=1)

    target = min(next_super, next_overnight)
    label = "super off-peak" if target == next_super else "overnight"
    return f"{label.title()} from {target.strftime('%-I:%M %p')}"
