#!/usr/bin/env python3
"""Homeowner-friendly daily energy event timeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class EnergyTimelineEvent:
    event_type: str
    local_time: str
    title: str
    explanation: str
    confidence: str
    supporting_values: dict[str, str] = field(default_factory=dict)
    icon: str = ""
    sort_time: str = field(default="", compare=False)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("sort_time", None)
        return data


@dataclass(frozen=True)
class EnergyTimeline:
    date: str
    is_in_progress: bool
    events: list[EnergyTimelineEvent]
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "is_in_progress": self.is_in_progress,
            "summary": self.summary,
            "events": [event.as_dict() for event in self.events],
        }


class EnergyTimelineEngine:
    """Detect daily household energy events from existing interval data."""

    def build(
        self,
        *,
        day: date,
        readings: Iterable[dict[str, Any]],
        ev_sessions: Iterable[dict[str, Any]] = (),
        settings: Any = None,
        now: Optional[datetime] = None,
    ) -> EnergyTimeline:
        rows = sorted((self._normalise_reading(row) for row in readings), key=lambda row: row["dt"])
        sessions = list(ev_sessions or [])
        events: list[EnergyTimelineEvent] = []
        events.extend(self._solar_events(rows))
        events.extend(self._export_events(rows))
        events.extend(self._solar_powered_events(rows))
        events.extend(self._tariff_events(rows, settings))
        events.extend(self._ev_events(rows, sessions, settings))
        events.extend(self._usage_cost_events(rows, settings))
        deduped = self._dedupe(events)
        deduped.sort(key=lambda event: event.sort_time or event.local_time)
        is_in_progress = bool(now and day == now.date())
        if not deduped:
            summary = "No daily energy events could be detected from the available readings."
        elif is_in_progress:
            summary = "Today is still in progress. These are the events PowerMonitor can already verify."
        else:
            summary = "These are the main household energy events PowerMonitor detected for the day."
        return EnergyTimeline(
            date=day.isoformat(),
            is_in_progress=is_in_progress,
            events=deduped,
            summary=summary,
        )

    def _solar_events(self, rows: list[dict[str, Any]]) -> list[EnergyTimelineEvent]:
        solar_rows = [row for row in rows if row["production_kwh"] > 0.01]
        if not solar_rows:
            return []
        peak = max(solar_rows, key=lambda row: row["production_kwh"])
        return [
            self._event(
                "solar_generation_started",
                solar_rows[0],
                "Solar generation started",
                "Your panels began producing usable solar.",
                "high",
                {"Solar": _kwh(solar_rows[0]["production_kwh"])},
                "☀️",
            ),
            self._event(
                "peak_solar_production",
                peak,
                "Peak solar production",
                "This was the strongest solar interval PowerMonitor saw today.",
                "high",
                {"Solar interval": _kwh(peak["production_kwh"])},
                "☀️",
            ),
            self._event(
                "solar_generation_ended",
                solar_rows[-1],
                "Solar generation ended",
                "Your panels stopped producing usable solar for the day.",
                "medium",
                {"Last solar interval": _kwh(solar_rows[-1]["production_kwh"])},
                "🌙",
                at_end=True,
            ),
        ]

    def _export_events(self, rows: list[dict[str, Any]]) -> list[EnergyTimelineEvent]:
        export_rows = [row for row in rows if row["solar_to_grid_kwh"] > 0.01]
        if not export_rows:
            return []
        return [
            self._event(
                "export_to_grid_started",
                export_rows[0],
                "Exporting to grid",
                "Your home was making more solar than it was using.",
                "high",
                {"Export": _kwh(export_rows[0]["solar_to_grid_kwh"])},
                "🔋",
            ),
            self._event(
                "export_to_grid_ended",
                export_rows[-1],
                "Exporting ended",
                "Solar export stopped, so the home was no longer sending surplus power to the grid.",
                "medium",
                {"Last export interval": _kwh(export_rows[-1]["solar_to_grid_kwh"])},
                "🔋",
                at_end=True,
            ),
        ]

    def _solar_powered_events(self, rows: list[dict[str, Any]]) -> list[EnergyTimelineEvent]:
        for index, row in enumerate(rows):
            if row["solar_to_home_kwh"] > 0.02 and row["solar_to_home_kwh"] >= row["grid_to_home_kwh"]:
                events = [
                    self._event(
                        "home_mostly_solar_powered",
                        row,
                        "Home mostly solar powered",
                        "Most of the home's electricity in this interval came directly from solar.",
                        "medium",
                        {"Solar to home": _kwh(row["solar_to_home_kwh"]), "Grid import": _kwh(row["grid_to_home_kwh"])},
                        "☀️",
                    )
                ]
                resumed = self._first_after(
                    rows[index + 1:],
                    lambda item: item["grid_to_home_kwh"] > item["solar_to_home_kwh"] and item["grid_to_home_kwh"] > 0.02,
                )
                if resumed:
                    events.append(
                        self._event(
                            "grid_import_resumed",
                            resumed,
                            "Grid import resumed",
                            "The home started relying more on grid electricity again.",
                            "medium",
                            {"Grid import": _kwh(resumed["grid_to_home_kwh"])},
                            "⚡",
                        )
                    )
                return events
        return []

    def _tariff_events(self, rows: list[dict[str, Any]], settings: Any) -> list[EnergyTimelineEvent]:
        events = []
        first_by_bucket = {}
        for row in rows:
            bucket = row["tariff_bucket"]
            if bucket and bucket not in first_by_bucket:
                first_by_bucket[bucket] = row
        if "super_off_peak" in first_by_bucket:
            row = first_by_bucket["super_off_peak"]
            events.append(
                self._event(
                    "super_off_peak_tariff_started",
                    row,
                    "Super Off Peak started",
                    "This is usually your cheapest charging window.",
                    "high",
                    {"Tariff": "Super Off Peak"},
                    "🌙",
                )
            )
        if "peak" in first_by_bucket:
            row = first_by_bucket["peak"]
            events.append(
                self._event(
                    "peak_tariff_started",
                    row,
                    "Peak tariff started",
                    "Electricity is usually more expensive in this window.",
                    "high",
                    {"Tariff": "Peak"},
                    "💸",
                )
            )
        cheapest = self._bucket_by_rate(first_by_bucket, settings, cheapest=True)
        if cheapest and cheapest[0] != "super_off_peak":
            events.append(
                self._event(
                    "lowest_cost_period",
                    cheapest[1],
                    "Lowest-cost period started",
                    "This was the cheapest tariff period PowerMonitor saw today.",
                    "medium",
                    {"Tariff": _bucket_label(cheapest[0])},
                    "🌙",
                )
            )
        expensive = self._bucket_by_rate(first_by_bucket, settings, cheapest=False)
        if expensive and expensive[0] != "peak":
            events.append(
                self._event(
                    "most_expensive_period",
                    expensive[1],
                    "Most expensive period started",
                    "This was the highest-cost tariff period PowerMonitor saw today.",
                    "medium",
                    {"Tariff": _bucket_label(expensive[0])},
                    "💸",
                )
            )
        return events

    def _ev_events(self, rows: list[dict[str, Any]], sessions: list[dict[str, Any]], settings: Any) -> list[EnergyTimelineEvent]:
        actual_sessions = [session for session in sessions if _number(session, "energy_kwh") > 0 and _value(session, "start_time")]
        if actual_sessions:
            events = []
            for session in sorted(actual_sessions, key=lambda item: str(_value(item, "start_time") or ""))[:2]:
                start_dt = _parse_datetime(_value(session, "start_time"))
                end_dt = _parse_datetime(_value(session, "end_time"))
                source = str(_value(session, "source") or "charger history").replace("_", " ")
                if start_dt:
                    events.append(
                        self._event_from_dt(
                            "ev_charging_session_started",
                            start_dt,
                            "EV charging started",
                            "A completed charger session was imported for this time.",
                            "high",
                            {"Energy": _kwh(_number(session, "energy_kwh")), "Source": source},
                            "🚗",
                        )
                    )
                if end_dt:
                    events.append(
                        self._event_from_dt(
                            "ev_charging_ended",
                            end_dt,
                            "EV charging ended",
                            "The imported charger session finished.",
                            "high",
                            {"Energy": _kwh(_number(session, "energy_kwh")), "Source": source},
                            "🚗",
                        )
                    )
            return events
        baseline = _number(settings, "ev_baseline_watts")
        threshold = max(baseline + 1000.0, 1500.0)
        for row in rows:
            if row["tariff_bucket"] == "overnight" and row["grid_to_home_w"] >= threshold:
                return [
                    self._event(
                        "ev_charging_likely_started",
                        row,
                        "EV charging likely started",
                        "Overnight usage rose above the normal baseload, so PowerMonitor treated it as likely EV charging.",
                        "low",
                        {"Grid demand": _watts(row["grid_to_home_w"]), "Baseload": _watts(baseline)},
                        "🚗",
                    )
                ]
        return []

    def _usage_cost_events(self, rows: list[dict[str, Any]], settings: Any) -> list[EnergyTimelineEvent]:
        if not rows:
            return []
        highest = max(rows, key=lambda row: row["grid_to_home_kwh"])
        events = []
        if highest["grid_to_home_kwh"] > 0.02:
            events.append(
                self._event(
                    "highest_usage_interval",
                    highest,
                    "Highest usage interval",
                    "This was the interval with the most grid electricity use today.",
                    "high",
                    {"Grid import": _kwh(highest["grid_to_home_kwh"])},
                    "⚡",
                )
            )
        rates = _rates(settings)
        cost_rows = [dict(row, interval_cost=row["grid_to_home_kwh"] * rates.get(row["tariff_bucket"], 0.0)) for row in rows]
        positive_cost_rows = [row for row in cost_rows if row["interval_cost"] > 0.005]
        if positive_cost_rows:
            most = max(positive_cost_rows, key=lambda row: row["interval_cost"])
            events.append(
                self._event(
                    "most_expensive_period",
                    most,
                    "Most expensive interval",
                    "This was the costliest grid-use interval PowerMonitor saw today.",
                    "medium",
                    {"Estimated cost": _money(most["interval_cost"]), "Tariff": _bucket_label(most["tariff_bucket"])},
                    "💸",
                )
            )
            cheapest = min(positive_cost_rows, key=lambda row: row["interval_cost"])
            events.append(
                self._event(
                    "lowest_cost_period",
                    cheapest,
                    "Lowest-cost interval",
                    "This was the lowest-cost grid-use interval PowerMonitor saw today.",
                    "medium",
                    {"Estimated cost": _money(cheapest["interval_cost"]), "Tariff": _bucket_label(cheapest["tariff_bucket"])},
                    "🌙",
                )
            )
        return events

    def _normalise_reading(self, row: dict[str, Any]) -> dict[str, Any]:
        dt = _parse_datetime(_value(row, "measurement_time")) or datetime.combine(date.today(), time.min)
        return {
            "dt": dt,
            "measurement_time": _value(row, "measurement_time"),
            "tariff_bucket": str(_value(row, "tariff_bucket") or ""),
            "production_kwh": _number(row, "production_kwh"),
            "solar_to_home_kwh": _number(row, "solar_to_home_kwh"),
            "solar_to_grid_kwh": _number(row, "solar_to_grid_kwh"),
            "grid_to_home_w": _number(row, "grid_to_home_w"),
            "grid_to_home_kwh": _number(row, "grid_to_home_kwh"),
        }

    def _event(
        self,
        event_type: str,
        row: dict[str, Any],
        title: str,
        explanation: str,
        confidence: str,
        values: dict[str, str],
        icon: str,
        *,
        at_end: bool = False,
    ) -> EnergyTimelineEvent:
        dt = row["dt"] + timedelta(minutes=15) if at_end else row["dt"]
        return self._event_from_dt(event_type, dt, title, explanation, confidence, values, icon)

    def _event_from_dt(
        self,
        event_type: str,
        dt: datetime,
        title: str,
        explanation: str,
        confidence: str,
        values: dict[str, str],
        icon: str,
    ) -> EnergyTimelineEvent:
        local_time = dt.strftime("%H:%M")
        return EnergyTimelineEvent(
            event_type=event_type,
            local_time=local_time,
            title=title,
            explanation=explanation,
            confidence=confidence,
            supporting_values=values,
            icon=icon,
            sort_time=dt.isoformat(),
        )

    def _bucket_by_rate(self, first_by_bucket: dict[str, dict[str, Any]], settings: Any, *, cheapest: bool) -> Optional[tuple[str, dict[str, Any]]]:
        rates = _rates(settings)
        candidates = [(bucket, row, rates.get(bucket)) for bucket, row in first_by_bucket.items() if bucket in rates]
        candidates = [item for item in candidates if item[2] is not None]
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: float(item[2])) if cheapest else max(candidates, key=lambda item: float(item[2]))
        return selected[0], selected[1]

    def _first_after(self, rows: list[dict[str, Any]], predicate) -> Optional[dict[str, Any]]:
        for row in rows:
            if predicate(row):
                return row
        return None

    def _dedupe(self, events: list[EnergyTimelineEvent]) -> list[EnergyTimelineEvent]:
        seen: set[tuple[str, str]] = set()
        result = []
        for event in events:
            key = (event.event_type, event.local_time)
            if key in seen:
                continue
            seen.add(key)
            result.append(event)
        return result


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _number(source: Any, name: str) -> float:
    try:
        return float(_value(source, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rates(settings: Any) -> dict[str, float]:
    raw = _value(settings, "import_rates", {}) or {}
    return {str(key): float(value) for key, value in raw.items()}


def _kwh(value: float) -> str:
    return f"{float(value):.2f} kWh"


def _watts(value: float) -> str:
    return f"{float(value):.0f} W"


def _money(value: float) -> str:
    return f"${float(value):.2f}"


def _bucket_label(bucket: str) -> str:
    return str(bucket or "").replace("_", " ").title()
