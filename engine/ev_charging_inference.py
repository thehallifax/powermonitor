#!/usr/bin/env python3
"""Infer active EV charging intervals from charger connection sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
DEFAULT_CHARGER_POWER_KW = 6.9
DEFAULT_SCHEDULE_START = "23:00"
DEFAULT_SCHEDULE_END = "06:00"
DEFAULT_IMPORT_RATES = {
    "super_off_peak": 0.086151,
    "overnight": 0.193841,
    "off_peak": 0.236916,
    "peak": 0.538446,
}


@dataclass(frozen=True)
class EVChargingInferenceConfig:
    charger_power_kw: float = DEFAULT_CHARGER_POWER_KW
    scheduled_start: time = time(23, 0)
    scheduled_end: time = time(6, 0)
    import_rates: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_IMPORT_RATES))
    source: str = "defaults"

    @classmethod
    def from_settings(cls, settings: Any = None, *, settings_path: Optional[Path] = None) -> "EVChargingInferenceConfig":
        payload = _settings_payload(settings)
        if settings_path and settings_path.exists():
            payload = {**_load_json(settings_path), **payload}

        profile_value = payload.get("home_profile")
        if isinstance(profile_value, dict):
            profile = profile_value
        else:
            profile = _settings_payload(profile_value) if profile_value is not None else {}
        charger_power = _first_float(
            payload,
            profile,
            keys=(
                "ev_charger_power_kw",
                "charger_power_kw",
                "ev_configured_charge_power_kw",
                "configured_charge_power_kw",
            ),
            default=DEFAULT_CHARGER_POWER_KW,
        )
        scheduled_start = _first_time(
            payload,
            profile,
            keys=(
                "ev_charging_schedule_start",
                "ev_scheduled_charging_start",
                "ev_charging_start_time",
                "ev_schedule_start_time",
                "scheduled_charging_start",
            ),
            default=DEFAULT_SCHEDULE_START,
        )
        scheduled_end = _first_time(
            payload,
            profile,
            keys=(
                "ev_charging_schedule_end",
                "ev_scheduled_charging_end",
                "ev_charging_end_time",
                "ev_schedule_end_time",
                "scheduled_charging_end",
            ),
            default=DEFAULT_SCHEDULE_END,
        )
        rates = payload.get("import_rates") if isinstance(payload.get("import_rates"), dict) else {}
        import_rates = {
            bucket: float(rates.get(bucket, DEFAULT_IMPORT_RATES[bucket]))
            for bucket in BUCKET_ORDER
        }
        source = "settings" if payload else "defaults"
        return cls(
            charger_power_kw=max(float(charger_power), 0.001),
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            import_rates=import_rates,
            source=source,
        )

    def note(self) -> str:
        return (
            f"Estimated from configured {self.charger_power_kw:g} kW charger rate "
            f"and charging schedule."
        )


@dataclass(frozen=True)
class EVActiveChargingEstimate:
    connected_at: Optional[datetime]
    disconnected_at: Optional[datetime]
    connected_duration_minutes: Optional[float]
    active_start: Optional[datetime]
    active_finish: Optional[datetime]
    active_duration_minutes: Optional[float]
    estimated_active_duration_hours: Optional[float]
    charger_power_kw: float
    schedule_start: str
    schedule_end: str
    inferred: bool
    diagnostics: list[str] = field(default_factory=list)

    def as_notes(self) -> dict[str, Any]:
        return {
            "connected_at": self.connected_at.isoformat(timespec="seconds") if self.connected_at else None,
            "disconnected_at": self.disconnected_at.isoformat(timespec="seconds") if self.disconnected_at else None,
            "connected_duration_minutes": self.connected_duration_minutes,
            "estimated_active_start": self.active_start.isoformat(timespec="seconds") if self.active_start else None,
            "estimated_active_finish": self.active_finish.isoformat(timespec="seconds") if self.active_finish else None,
            "estimated_active_duration_minutes": self.active_duration_minutes,
            "estimated_active_duration_hours": self.estimated_active_duration_hours,
            "configured_charger_power_kw": self.charger_power_kw,
            "configured_schedule_start": self.schedule_start,
            "configured_schedule_end": self.schedule_end,
            "active_interval_inferred": self.inferred,
            "inference_note": (
                f"Estimated from configured {self.charger_power_kw:g} kW charger rate and charging schedule."
                if self.inferred else ""
            ),
            "inference_diagnostics": list(self.diagnostics),
        }


def infer_active_charging_interval(
    connected_at: Optional[datetime],
    disconnected_at: Optional[datetime],
    energy_kwh: float,
    config: EVChargingInferenceConfig,
    connected_duration_minutes: Optional[float] = None,
) -> EVActiveChargingEstimate:
    diagnostics: list[str] = []
    schedule_start = config.scheduled_start.strftime("%H:%M")
    schedule_end = config.scheduled_end.strftime("%H:%M")
    if connected_at and disconnected_at and connected_duration_minutes is None:
        connected_duration_minutes = max((disconnected_at - connected_at).total_seconds() / 60, 0.0)
    if not connected_at:
        diagnostics.append("missing_connection_timestamp")
    if not disconnected_at:
        diagnostics.append("missing_disconnection_timestamp")
    if energy_kwh <= 0:
        diagnostics.append("missing_or_zero_energy")
    if not connected_at or not disconnected_at or energy_kwh <= 0:
        return EVActiveChargingEstimate(
            connected_at,
            disconnected_at,
            connected_duration_minutes,
            None,
            None,
            None,
            None,
            config.charger_power_kw,
            schedule_start,
            schedule_end,
            False,
            diagnostics,
        )

    estimated_hours = float(energy_kwh) / config.charger_power_kw
    estimated_duration = timedelta(hours=estimated_hours)
    active_start = first_scheduled_start_at_or_after_connection(connected_at, config)
    if disconnected_at < connected_at:
        diagnostics.append("disconnected_before_connected")
        active_start = connected_at
        active_finish = connected_at
    else:
        if active_start > disconnected_at:
            diagnostics.append("scheduled_start_after_disconnection")
            active_start = connected_at
        active_start = max(active_start, connected_at)
        active_finish = active_start + estimated_duration
        if active_finish > disconnected_at:
            diagnostics.append("active_duration_clamped_to_connected_interval")
            connected_span = disconnected_at - connected_at
            if estimated_duration > connected_span:
                active_start = connected_at
            else:
                active_start = max(connected_at, disconnected_at - estimated_duration)
            active_finish = disconnected_at

    active_minutes = max((active_finish - active_start).total_seconds() / 60, 0.0)
    return EVActiveChargingEstimate(
        connected_at,
        disconnected_at,
        connected_duration_minutes,
        active_start,
        active_finish,
        active_minutes,
        estimated_hours,
        config.charger_power_kw,
        schedule_start,
        schedule_end,
        True,
        diagnostics,
    )


def first_scheduled_start_at_or_after_connection(
    connected_at: datetime,
    config: EVChargingInferenceConfig,
) -> datetime:
    if _in_window(connected_at.timetz().replace(tzinfo=None), config.scheduled_start, config.scheduled_end):
        return connected_at
    same_day = datetime.combine(connected_at.date(), config.scheduled_start, tzinfo=connected_at.tzinfo)
    if same_day >= connected_at:
        return same_day
    return same_day + timedelta(days=1)


def split_energy_by_tariff(
    start: Optional[datetime],
    finish: Optional[datetime],
    energy_kwh: float,
    import_rates: dict[str, float],
) -> tuple[float, dict[str, dict[str, float]]]:
    breakdown = {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in BUCKET_ORDER}
    if not start or not finish or finish <= start or energy_kwh <= 0:
        return 0.0, breakdown
    total_seconds = (finish - start).total_seconds()
    cursor = start
    while cursor < finish:
        boundary = min(_next_bucket_boundary(cursor), finish)
        bucket = bucket_for_time(cursor)
        share = (boundary - cursor).total_seconds() / total_seconds
        bucket_kwh = energy_kwh * share
        bucket_cost = bucket_kwh * float(import_rates.get(bucket, DEFAULT_IMPORT_RATES[bucket]))
        breakdown[bucket]["kwh"] += bucket_kwh
        breakdown[bucket]["cost"] += bucket_cost
        cursor = boundary
    return sum(values["cost"] for values in breakdown.values()), breakdown


def bucket_for_time(dt: datetime) -> str:
    hour = dt.hour
    if hour >= 23 or hour < 6:
        return "overnight"
    if 6 <= hour < 9:
        return "off_peak"
    if 9 <= hour < 15:
        return "super_off_peak"
    if 15 <= hour < 21:
        return "peak"
    return "off_peak"


def _next_bucket_boundary(dt: datetime) -> datetime:
    day = dt.date()
    boundaries = [
        datetime.combine(day, time(6, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(9, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(15, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(21, 0), tzinfo=dt.tzinfo),
        datetime.combine(day, time(23, 0), tzinfo=dt.tzinfo),
        datetime.combine(day + timedelta(days=1), time(6, 0), tzinfo=dt.tzinfo),
    ]
    return min(boundary for boundary in boundaries if boundary > dt)


def _in_window(value: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _settings_payload(settings: Any = None) -> dict[str, Any]:
    if settings is None:
        return {}
    if isinstance(settings, dict):
        return dict(settings)
    payload: dict[str, Any] = {}
    for key in dir(settings):
        if key.startswith("_"):
            continue
        try:
            value = getattr(settings, key)
        except Exception:
            continue
        if callable(value):
            continue
        payload[key] = value
    return payload


def _first_float(*payloads: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            try:
                if value not in (None, ""):
                    return float(value)
            except (TypeError, ValueError):
                continue
    return default


def _first_time(*payloads: dict[str, Any], keys: tuple[str, ...], default: str) -> time:
    for payload in payloads:
        for key in keys:
            parsed = _parse_time(payload.get(key))
            if parsed is not None:
                return parsed
    return _parse_time(default) or time(23, 0)


def _parse_time(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None
