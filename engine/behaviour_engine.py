#!/usr/bin/env python3
"""Long-term household behaviour profiling for PowerMonitor.

The Behaviour Engine answers "how does this household normally behave?" from
historical platform data. It is descriptive only and does not recommend,
optimise, or predict future behaviour.
"""

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from statistics import mean, median
from typing import Any, Optional

from .models import EngineStatus, HouseholdContext


class BehaviourConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BaseloadProfile:
    overnight_baseload_watts: Optional[float]
    weekday_baseload_watts: Optional[float]
    weekend_baseload_watts: Optional[float]
    normal_variance_watts: Optional[float]
    confidence: BehaviourConfidence

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = self.confidence.value
        return data


@dataclass(frozen=True)
class DailyUsageProfile:
    typical_wake_time: Optional[str]
    morning_activity_kwh: Optional[float]
    evening_peak_time: Optional[str]
    evening_peak_kwh: Optional[float]
    overnight_idle_kwh: Optional[float]
    weekday_daily_usage_kwh: Optional[float]
    weekend_daily_usage_kwh: Optional[float]
    confidence: BehaviourConfidence

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = self.confidence.value
        return data


@dataclass(frozen=True)
class EVBehaviourProfile:
    preferred_charging_days: list[str]
    preferred_charging_window: Optional[str]
    average_charging_start_time: Optional[str]
    average_charging_finish_time: Optional[str]
    charging_consistency: str
    session_count: int
    confidence: BehaviourConfidence

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = self.confidence.value
        return data


@dataclass(frozen=True)
class SolarUsageProfile:
    export_tendency: str
    self_consumption_tendency: str
    solar_utilisation_tendency: str
    sunny_day_export_pct: Optional[float]
    cloudy_day_self_consumption_pct: Optional[float]
    confidence: BehaviourConfidence

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = self.confidence.value
        return data


@dataclass(frozen=True)
class HouseholdBehaviour:
    household_id: str
    generated_at: str
    sample_start: Optional[str]
    sample_end: Optional[str]
    sample_days: int
    baseload: BaseloadProfile
    daily_usage: DailyUsageProfile
    ev: EVBehaviourProfile
    solar: SolarUsageProfile
    confidence: BehaviourConfidence
    data_sources_used: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "generated_at": self.generated_at,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "sample_days": self.sample_days,
            "baseload": self.baseload.as_dict(),
            "daily_usage": self.daily_usage.as_dict(),
            "ev": self.ev.as_dict(),
            "solar": self.solar.as_dict(),
            "confidence": self.confidence.value,
            "data_sources_used": list(self.data_sources_used),
        }


class BehaviourEngine:
    """Builds descriptive long-term behaviour from normalized household data."""

    name = "behaviour_engine"

    def __init__(self, household_context: Optional[HouseholdContext] = None) -> None:
        self.household_context = household_context

    def status(self) -> EngineStatus:
        return EngineStatus(name=self.name)

    def evaluate(
        self,
        _context: dict[str, Any],
        household_context: Optional[HouseholdContext] = None,
    ) -> list[Any]:
        """Compatibility hook for engine smoke tests; behaviour uses build()."""

        self.household_context = household_context or self.household_context
        return []

    def build(
        self,
        *,
        grid_intervals: list[dict[str, Any]],
        solar_intervals: list[dict[str, Any]],
        ev_sessions: list[dict[str, Any]],
        household_id: str = "default_household",
        generated_at: Optional[datetime] = None,
    ) -> HouseholdBehaviour:
        generated = generated_at or datetime.now().astimezone()
        dated_rows = [(parse_time(row.get("measurement_time")), row) for row in grid_intervals]
        dated_rows = [(dt, row) for dt, row in dated_rows if dt]
        sample_dates = sorted({dt.date().isoformat() for dt, _row in dated_rows})
        confidence = confidence_for_days(len(sample_dates))
        return HouseholdBehaviour(
            household_id=household_id,
            generated_at=generated.isoformat(timespec="seconds"),
            sample_start=sample_dates[0] if sample_dates else None,
            sample_end=sample_dates[-1] if sample_dates else None,
            sample_days=len(sample_dates),
            baseload=self._baseload(dated_rows, confidence),
            daily_usage=self._daily_usage(dated_rows, confidence),
            ev=self._ev_behaviour(ev_sessions),
            solar=self._solar_behaviour(solar_intervals),
            confidence=confidence,
            data_sources_used=self._sources(grid_intervals, solar_intervals, ev_sessions),
        )

    def _baseload(self, rows: list[tuple[datetime, dict[str, Any]]], confidence: BehaviourConfidence) -> BaseloadProfile:
        overnight = [
            float(row.get("grid_to_home_w") or 0)
            for dt, row in rows
            if dt.hour < 5 and float(row.get("grid_to_home_w") or 0) > 0
        ]
        weekday = [
            float(row.get("grid_to_home_w") or 0)
            for dt, row in rows
            if dt.weekday() < 5 and dt.hour < 5 and float(row.get("grid_to_home_w") or 0) > 0
        ]
        weekend = [
            float(row.get("grid_to_home_w") or 0)
            for dt, row in rows
            if dt.weekday() >= 5 and dt.hour < 5 and float(row.get("grid_to_home_w") or 0) > 0
        ]
        return BaseloadProfile(
            overnight_baseload_watts=round(median(overnight), 1) if overnight else None,
            weekday_baseload_watts=round(median(weekday), 1) if weekday else None,
            weekend_baseload_watts=round(median(weekend), 1) if weekend else None,
            normal_variance_watts=round(stddev(overnight), 1) if len(overnight) >= 2 else None,
            confidence=confidence if overnight else BehaviourConfidence.UNKNOWN,
        )

    def _daily_usage(self, rows: list[tuple[datetime, dict[str, Any]]], confidence: BehaviourConfidence) -> DailyUsageProfile:
        if not rows:
            return DailyUsageProfile(None, None, None, None, None, None, None, BehaviourConfidence.UNKNOWN)
        by_hour = hourly_kwh(rows)
        by_day = daily_kwh(rows)
        overnight_avg = mean([value for hour, value in by_hour.items() if 0 <= hour < 5] or [0.0])
        wake_candidates = [
            hour for hour in range(6, 11)
            if by_hour.get(hour, 0.0) > max(overnight_avg * 2.0, overnight_avg + 0.2)
        ]
        evening_hour = max(range(16, 23), key=lambda hour: by_hour.get(hour, 0.0))
        weekday_days = [value for day, value in by_day.items() if datetime.fromisoformat(day).weekday() < 5]
        weekend_days = [value for day, value in by_day.items() if datetime.fromisoformat(day).weekday() >= 5]
        return DailyUsageProfile(
            typical_wake_time=f"{wake_candidates[0]:02d}:00" if wake_candidates else None,
            morning_activity_kwh=round(sum(by_hour.get(hour, 0.0) for hour in range(6, 11)), 2),
            evening_peak_time=f"{evening_hour:02d}:00",
            evening_peak_kwh=round(by_hour.get(evening_hour, 0.0), 2),
            overnight_idle_kwh=round(sum(by_hour.get(hour, 0.0) for hour in range(0, 5)), 2),
            weekday_daily_usage_kwh=round(mean(weekday_days), 2) if weekday_days else None,
            weekend_daily_usage_kwh=round(mean(weekend_days), 2) if weekend_days else None,
            confidence=confidence,
        )

    def _ev_behaviour(self, sessions: list[dict[str, Any]]) -> EVBehaviourProfile:
        if not sessions:
            return EVBehaviourProfile([], None, None, None, "unknown", 0, BehaviourConfidence.UNKNOWN)
        starts = [parse_time(session.get("start_time")) for session in sessions]
        finishes = [parse_time(session.get("end_time")) for session in sessions]
        starts = [item for item in starts if item]
        finishes = [item for item in finishes if item]
        day_counts: dict[str, int] = {}
        for start in starts:
            label = start.strftime("%A")
            day_counts[label] = day_counts.get(label, 0) + 1
        preferred_days = [
            day for day, count in sorted(day_counts.items(), key=lambda item: (-item[1], item[0]))
            if count == max(day_counts.values())
        ]
        avg_start = circular_average_time(starts)
        avg_finish = circular_average_time(finishes)
        hour_counts: dict[int, int] = {}
        for start in starts:
            hour_counts[start.hour] = hour_counts.get(start.hour, 0) + 1
        peak_hour = max(hour_counts, key=hour_counts.get) if hour_counts else None
        consistency_ratio = max(hour_counts.values()) / len(starts) if starts and hour_counts else 0.0
        confidence = BehaviourConfidence.HIGH if len(sessions) >= 8 else BehaviourConfidence.MEDIUM if len(sessions) >= 3 else BehaviourConfidence.LOW
        return EVBehaviourProfile(
            preferred_charging_days=preferred_days,
            preferred_charging_window=f"{peak_hour:02d}:00-{(peak_hour + 1) % 24:02d}:00" if peak_hour is not None else None,
            average_charging_start_time=avg_start,
            average_charging_finish_time=avg_finish,
            charging_consistency=consistency_label(consistency_ratio),
            session_count=len(sessions),
            confidence=confidence,
        )

    def _solar_behaviour(self, rows: list[dict[str, Any]]) -> SolarUsageProfile:
        by_day: dict[str, dict[str, float]] = {}
        for row in rows:
            dt = parse_time(row.get("measurement_time"))
            if not dt:
                continue
            day = dt.date().isoformat()
            item = by_day.setdefault(day, {"generated": 0.0, "used": 0.0, "exported": 0.0})
            item["generated"] += float(row.get("production_kwh") or 0)
            item["used"] += float(row.get("solar_to_home_kwh") or 0)
            item["exported"] += float(row.get("solar_to_grid_kwh") or 0)
        days = [item for item in by_day.values() if item["generated"] > 0]
        if not days:
            return SolarUsageProfile("unknown", "unknown", "unknown", None, None, BehaviourConfidence.UNKNOWN)
        export_pcts = [item["exported"] / item["generated"] * 100 for item in days]
        self_pcts = [item["used"] / item["generated"] * 100 for item in days]
        generation = [item["generated"] for item in days]
        sunny_cutoff = median(generation)
        sunny = [item for item in days if item["generated"] >= sunny_cutoff]
        cloudy = [item for item in days if item["generated"] < sunny_cutoff]
        confidence = confidence_for_days(len(days))
        avg_export = mean(export_pcts)
        avg_self = mean(self_pcts)
        return SolarUsageProfile(
            export_tendency=tendency(avg_export, 20, 45, "low", "moderate", "high"),
            self_consumption_tendency=tendency(avg_self, 45, 70, "low", "moderate", "high"),
            solar_utilisation_tendency=tendency(avg_self, 50, 75, "limited", "steady", "strong"),
            sunny_day_export_pct=round(mean([item["exported"] / item["generated"] * 100 for item in sunny]), 1) if sunny else None,
            cloudy_day_self_consumption_pct=round(mean([item["used"] / item["generated"] * 100 for item in cloudy]), 1) if cloudy else None,
            confidence=confidence,
        )

    def _sources(self, grid_intervals: list[dict[str, Any]], solar_intervals: list[dict[str, Any]], ev_sessions: list[dict[str, Any]]) -> list[str]:
        sources = []
        if grid_intervals:
            sources.append("Household interval readings")
        if solar_intervals:
            sources.append("Solar interval readings")
        if ev_sessions:
            sources.append("EV charging sessions")
        return sources


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def confidence_for_days(days: int) -> BehaviourConfidence:
    if days >= 21:
        return BehaviourConfidence.HIGH
    if days >= 7:
        return BehaviourConfidence.MEDIUM
    if days > 0:
        return BehaviourConfidence.LOW
    return BehaviourConfidence.UNKNOWN


def hourly_kwh(rows: list[tuple[datetime, dict[str, Any]]]) -> dict[int, float]:
    totals: dict[int, float] = {}
    counts: dict[int, set[str]] = {}
    for dt, row in rows:
        totals[dt.hour] = totals.get(dt.hour, 0.0) + float(row.get("grid_to_home_kwh") or 0)
        counts.setdefault(dt.hour, set()).add(dt.date().isoformat())
    return {
        hour: total / max(len(counts.get(hour, [])), 1)
        for hour, total in totals.items()
    }


def daily_kwh(rows: list[tuple[datetime, dict[str, Any]]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for dt, row in rows:
        day = dt.date().isoformat()
        totals[day] = totals.get(day, 0.0) + float(row.get("grid_to_home_kwh") or 0)
    return totals


def circular_average_time(values: list[datetime]) -> Optional[str]:
    if not values:
        return None
    angles = []
    for value in values:
        minutes = value.hour * 60 + value.minute
        angles.append(minutes / (24 * 60) * 2 * math.pi)
    sin_sum = sum(math.sin(angle) for angle in angles)
    cos_sum = sum(math.cos(angle) for angle in angles)
    if sin_sum == 0 and cos_sum == 0:
        return None
    angle = math.atan2(sin_sum / len(angles), cos_sum / len(angles))
    if angle < 0:
        angle += 2 * math.pi
    minutes = int(round(angle / (2 * math.pi) * 24 * 60)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def consistency_label(ratio: float) -> str:
    if ratio >= 0.7:
        return "high"
    if ratio >= 0.4:
        return "moderate"
    return "low"


def tendency(value: float, low_cutoff: float, high_cutoff: float, low: str, mid: str, high: str) -> str:
    if value < low_cutoff:
        return low
    if value < high_cutoff:
        return mid
    return high


def stddev(values: list[float]) -> float:
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / len(values))
