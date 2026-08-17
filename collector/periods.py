#!/usr/bin/env python3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from typing import Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.billing_period_engine import resolve_billing_cycle


SUPPORTED_PERIODS = (
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "year_to_date",
    "last_calendar_year",
    "current_billing_period",
    "previous_billing_period",
    "custom",
)
LOCAL_TIMEZONE = ZoneInfo("Australia/Perth")


@dataclass(frozen=True)
class Period:
    kind: str
    label: str
    start: date
    end: date
    expected_end: Optional[date] = None
    source: Optional[str] = None

    def metadata(self) -> dict:
        return {
            "expected_end": (self.expected_end or self.end).isoformat(),
            "source": self.source,
        }


def _month_before(day: date) -> date:
    return day.replace(day=1) - timedelta(days=1)


def _billing_fallback(today: date, start_day: int) -> tuple[date, date]:
    if today.day >= start_day:
        return today.replace(day=start_day), today
    prior = _month_before(today)
    return prior.replace(day=start_day), today


def resolve_period(
    kind: str,
    settings,
    today: Optional[date] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Period:
    current = today or datetime.now(LOCAL_TIMEZONE).date()
    if kind not in SUPPORTED_PERIODS:
        raise ValueError(f"Unsupported period: {kind}")

    if kind == "today":
        return Period(kind, "Today", current, current)
    if kind == "yesterday":
        day = current - timedelta(days=1)
        return Period(kind, "Yesterday", day, day)
    if kind == "last_7_days":
        return Period(kind, "Last 7 days", current - timedelta(days=6), current)
    if kind == "last_30_days":
        return Period(kind, "Last 30 days", current - timedelta(days=29), current)
    if kind == "this_week":
        return Period(kind, "This week", current - timedelta(days=current.weekday()), current)
    if kind == "last_week":
        this_week = current - timedelta(days=current.weekday())
        return Period(kind, "Last week", this_week - timedelta(days=7), this_week - timedelta(days=1))
    if kind == "this_month":
        return Period(kind, "This month", current.replace(day=1), current)
    if kind == "last_month":
        month_end = _month_before(current)
        return Period(kind, "Last month", month_end.replace(day=1), month_end)
    if kind == "year_to_date":
        return Period(kind, "Year to date", current.replace(month=1, day=1), current)
    if kind == "last_calendar_year":
        year = current.year - 1
        return Period(kind, "Last calendar year", date(year, 1, 1), date(year, 12, 31))
    if kind == "current_billing_period":
        cycle = resolve_billing_cycle(settings, current)
        return Period(kind, "Current billing period", cycle.start, cycle.data_through, cycle.expected_end, cycle.source)
    if kind == "previous_billing_period":
        cycle = resolve_billing_cycle(settings, current)
        return Period(kind, "Previous billing period", cycle.previous_start, cycle.previous_end, cycle.previous_end, cycle.source)

    if not start or not end:
        raise ValueError("Custom periods require start and end dates")
    try:
        custom_start = date.fromisoformat(start)
        custom_end = date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("Custom period dates must use YYYY-MM-DD") from exc
    if custom_start > custom_end:
        raise ValueError("Custom period start cannot be after end")
    return Period(kind, "Custom period", custom_start, custom_end)


def previous_equivalent(period: Period) -> Period:
    days = (period.end - period.start).days + 1
    end = period.start - timedelta(days=1)
    return Period("custom", "Previous equivalent period", end - timedelta(days=days - 1), end)
