#!/usr/bin/env python3
"""Canonical billing-cycle resolution for reporting and projections."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional


@dataclass(frozen=True)
class BillingCycle:
    start: date
    data_through: date
    expected_end: date
    previous_start: date
    previous_end: date
    anchor_start: date
    anchor_end: Optional[date]
    cadence_days: Optional[int]
    cadence: str
    source: str
    boundary: str
    anchor_source: str
    warnings: tuple[str, ...] = ()

    @property
    def expected_days(self) -> int:
        return (self.expected_end - self.start).days + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_billing_period_start": self.start.isoformat(),
            "data_through": self.data_through.isoformat(),
            "expected_billing_period_end": self.expected_end.isoformat(),
            "previous_billing_period_start": self.previous_start.isoformat(),
            "previous_billing_period_end": self.previous_end.isoformat(),
            "anchor_period": {
                "start": self.anchor_start.isoformat(),
                "end": self.anchor_end.isoformat() if self.anchor_end else None,
                "source": self.anchor_source,
            },
            "configured_cadence": self.cadence,
            "cadence_days": self.cadence_days,
            "period_source": self.source,
            "current_boundary": self.boundary,
            "current_boundary_predicted": self.boundary == "predicted",
            "expected_days": self.expected_days,
            "warnings": list(self.warnings),
        }


def resolve_billing_cycle(settings: Any, today: date) -> BillingCycle:
    """Resolve the active local billing cycle, advancing past elapsed cycles."""
    configured_start = _as_date(_value(settings, "current_billing_period_start"))
    configured_end = _as_date(_value(settings, "current_billing_period_end"))
    previous_start = _as_date(_value(settings, "previous_billing_period_start"))
    previous_end = _as_date(_value(settings, "previous_billing_period_end"))
    start_day = _valid_start_day(_value(settings, "billing_period_start_day", 1))
    warnings = []

    if configured_start:
        anchor_start = configured_start
        anchor_end = configured_end
        anchor_source = "configured"
    elif previous_end:
        anchor_start = previous_end + timedelta(days=1)
        anchor_end = None
        anchor_source = "actual_historical_bill"
    else:
        anchor_start = _monthly_start(today, start_day)
        anchor_end = None
        anchor_source = "billing_cycle_configuration"

    cadence_days = _cadence_days(configured_start, configured_end, previous_start, previous_end)
    if cadence_days:
        start, expected_end, advances = _advance_fixed(anchor_start, cadence_days, today)
        previous_cycle_start = start - timedelta(days=cadence_days)
        previous_cycle_end = start - timedelta(days=1)
        cadence = f"{cadence_days} days"
    else:
        start, expected_end, advances = _advance_monthly(anchor_start, start_day, today)
        previous_cycle_end = start - timedelta(days=1)
        previous_cycle_start = _monthly_start(previous_cycle_end, start_day)
        cadence = f"monthly on day {start_day}"

    if advances == 0 and previous_start and previous_end:
        previous_cycle_start = previous_start
        previous_cycle_end = previous_end
    boundary = "predicted" if advances > 0 or not configured_start else "configured"
    source = "predicted_from_anchor" if boundary == "predicted" else anchor_source
    if today > expected_end:
        warnings.append("Current billing period did not advance beyond its expected end.")
    if previous_start and previous_end and previous_start > previous_end:
        warnings.append("Configured historical billing period is invalid.")
    if previous_end and configured_start and previous_end >= configured_start:
        warnings.append("Authoritative billing periods overlap.")

    return BillingCycle(
        start=start,
        data_through=today,
        expected_end=expected_end,
        previous_start=previous_cycle_start,
        previous_end=previous_cycle_end,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        cadence_days=cadence_days,
        cadence=cadence,
        source=source,
        boundary=boundary,
        anchor_source=anchor_source,
        warnings=tuple(warnings),
    )


def _cadence_days(
    current_start: Optional[date],
    current_end: Optional[date],
    previous_start: Optional[date],
    previous_end: Optional[date],
) -> Optional[int]:
    if current_start and current_end and current_end >= current_start:
        return (current_end - current_start).days + 1
    if previous_start and previous_end and previous_end >= previous_start:
        return (previous_end - previous_start).days + 1
    return None


def _advance_fixed(anchor_start: date, cadence_days: int, today: date) -> tuple[date, date, int]:
    if today < anchor_start:
        raise ValueError("Billing-cycle anchor cannot be in the future")
    advances = (today - anchor_start).days // cadence_days
    start = anchor_start + timedelta(days=advances * cadence_days)
    return start, start + timedelta(days=cadence_days - 1), advances


def _advance_monthly(anchor_start: date, start_day: int, today: date) -> tuple[date, date, int]:
    if today < anchor_start:
        raise ValueError("Billing-cycle anchor cannot be in the future")
    start = anchor_start
    advances = 0
    while True:
        next_start = _add_month(start, start_day)
        if today < next_start:
            return start, next_start - timedelta(days=1), advances
        start = next_start
        advances += 1


def _monthly_start(day: date, start_day: int) -> date:
    candidate = date(day.year, day.month, min(start_day, calendar.monthrange(day.year, day.month)[1]))
    if day >= candidate:
        return candidate
    previous_month = day.replace(day=1) - timedelta(days=1)
    return date(previous_month.year, previous_month.month, min(start_day, previous_month.day))


def _add_month(day: date, start_day: int) -> date:
    year = day.year + (1 if day.month == 12 else 0)
    month = 1 if day.month == 12 else day.month + 1
    return date(year, month, min(start_day, calendar.monthrange(year, month)[1]))


def _valid_start_day(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Billing-cycle cadence is missing or invalid") from exc
    if not 1 <= result <= 28:
        raise ValueError("Billing-cycle start day must be from 1 to 28")
    return result


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _as_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
