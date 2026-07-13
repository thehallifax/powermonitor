#!/usr/bin/env python3
"""Historical data coverage and backfill planning.

This module describes historical import capability without changing the
calculation engines. Provider-specific fetching remains in collectors; DataEngine
owns persistence and coverage queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional


RANGE_PRESETS = {
    "previous_summer": "Previous summer",
    "last_90_days": "Last 90 days",
    "last_12_months": "Last 12 months",
    "previous_calendar_year": "Previous calendar year",
    "custom": "Custom date range",
    "lifetime": "Lifetime available history",
}

SOLAREDGE_DATA_TYPES = (
    "solar_generation",
    "household_consumption",
    "grid_import",
    "grid_export",
)


@dataclass(frozen=True)
class BackfillRange:
    preset: str
    label: str
    start: date
    end: date

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": (self.end - self.start).days + 1,
        }


def most_recent_completed_au_summer(today: date) -> tuple[date, date]:
    """Return 1 Dec to 31 Mar for the most recently completed AU summer."""

    current_year_summer_end = date(today.year, 3, 31)
    if today > current_year_summer_end:
        return date(today.year - 1, 12, 1), current_year_summer_end
    return date(today.year - 2, 12, 1), date(today.year - 1, 3, 31)


def resolve_backfill_range(
    preset: str,
    *,
    today: Optional[date] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> BackfillRange:
    current_day = today or date.today()
    preset = preset if preset in RANGE_PRESETS else "previous_summer"
    if preset == "previous_summer":
        start_day, end_day = most_recent_completed_au_summer(current_day)
    elif preset == "last_90_days":
        end_day = current_day
        start_day = current_day - timedelta(days=89)
    elif preset == "last_12_months":
        end_day = current_day
        start_day = current_day - timedelta(days=365)
    elif preset == "previous_calendar_year":
        start_day = date(current_day.year - 1, 1, 1)
        end_day = date(current_day.year - 1, 12, 31)
    elif preset == "custom":
        if not start or not end:
            raise ValueError("Custom historical range requires start and end dates.")
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
    else:
        # The local SolarEdge dashboard endpoint does not expose a trustworthy
        # start-of-history marker. Use the widest explicit planning range while
        # making source limits visible in the preview.
        end_day = current_day
        start_day = current_day - timedelta(days=3650)
    if end_day < start_day:
        raise ValueError("Historical range end date must be on or after start date.")
    return BackfillRange(preset, RANGE_PRESETS[preset], start_day, end_day)


def solaredge_capability_audit(today: Optional[date] = None) -> dict[str, Any]:
    current_day = today or date.today()
    reliable_start = current_day - timedelta(days=6)
    return {
        "provider_id": "solaredge",
        "provider_name": "SolarEdge",
        "source": "SolarEdge monitoring dashboard power endpoint",
        "available_data": [
            "Solar production",
            "Household consumption",
            "Grid import",
            "Grid export",
            "Solar-to-home flow",
            "Solar-to-grid flow",
        ],
        "resolution": "15-minute interval readings when the dashboard endpoint returns quarter-hour power data.",
        "quality_tier": "interval_grade",
        "timezone": "Stored in the household timezone after import.",
        "cumulative_or_interval": "Power values are converted into interval kWh for each 15-minute reading.",
        "known_limits": [
            "The existing scheduled collector deliberately uses a 4-7 day recent window because that endpoint is reliable for recent quarter-hour data.",
            "Older day requests are attempted one day at a time and recorded as skipped when SolarEdge does not return usable interval data.",
            "Monthly or lifetime SolarEdge totals are not treated as sufficient for tariff-aware battery dispatch.",
        ],
        "battery_dispatch_suitability": "Suitable only when interval solar export, grid import, and tariff bucket data exist for the period.",
        "reliable_recent_window": {
            "start": reliable_start.isoformat(),
            "end": current_day.isoformat(),
            "days": 7,
        },
        "supported_ranges": list(RANGE_PRESETS.keys()),
        "chunking": "One day per request.",
    }


def build_backfill_preview(
    coverage: dict[str, Any],
    *,
    provider_id: str = "solaredge",
    preset: str = "previous_summer",
    today: Optional[date] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict[str, Any]:
    selected = resolve_backfill_range(preset, today=today, start=start, end=end)
    audit = solaredge_capability_audit(today)
    existing = coverage_for_range(coverage, provider_id, selected.start, selected.end)
    days = selected.as_dict()["days"]
    return {
        "provider_id": provider_id,
        "site_id": "default",
        "range": selected.as_dict(),
        "data_types": list(SOLAREDGE_DATA_TYPES),
        "resolution": "interval_15m",
        "quality_tier": "interval_grade",
        "battery_suitability": {
            "suitable": existing.get("battery_suitable_days", 0) >= days,
            "label": "Suitable for battery modelling only on days with interval import/export readings.",
        },
        "existing_coverage": existing,
        "gaps": {
            "days_missing": max(days - int(existing.get("days_with_any_reading") or 0), 0),
            "days_in_range": days,
        },
        "capability_audit": audit,
        "actions": ["preview", "start", "resume", "cancel", "view_diagnostics"],
        "warnings": audit["known_limits"] if provider_id == "solaredge" else ["Provider is not implemented for historical backfill yet."],
    }


def coverage_for_range(coverage: dict[str, Any], provider_id: str, start: date, end: date) -> dict[str, Any]:
    by_provider = coverage.get("providers", {}).get(provider_id, {})
    days = by_provider.get("days", {})
    in_range = {
        day: value
        for day, value in days.items()
        if start.isoformat() <= day <= end.isoformat()
    }
    battery_days = sum(1 for value in in_range.values() if value.get("battery_suitable"))
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_with_any_reading": len(in_range),
        "battery_suitable_days": battery_days,
        "first_reading": min((value.get("first") for value in in_range.values() if value.get("first")), default=None),
        "last_reading": max((value.get("last") for value in in_range.values() if value.get("last")), default=None),
    }


def seasonal_history_summary(coverage: dict[str, Any], *, today: Optional[date] = None) -> dict[str, Any]:
    start, end = most_recent_completed_au_summer(today or date.today())
    return build_backfill_preview(
        coverage,
        provider_id="solaredge",
        preset="previous_summer",
        today=today,
        start=start.isoformat(),
        end=end.isoformat(),
    )


def job_id_for(provider_id: str, site_id: str, selected: BackfillRange, created_at: Optional[str] = None) -> str:
    import hashlib

    created = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
    key = "|".join([provider_id, site_id, selected.preset, selected.start.isoformat(), selected.end.isoformat(), created])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
