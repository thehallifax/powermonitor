"""Small, source-backed selected-period summaries for the household utility.

This module is deliberately independent of the dashboard and the legacy
recommendation/decision pipeline.  It reads the existing ``readings`` and
``ev_sessions`` tables, recalculates prices from effective-dated tariff
versions, and returns plain dictionaries suitable for a future presentation
layer.

Date inputs represent inclusive local calendar days.  Datetime inputs
represent a half-open interval ``[start, end)``.  EV session energy is
allocated proportionally by elapsed time when a selected period only contains
part of a session; Ocular session data does not provide interval-level energy.
Supply charge is applied once for each local calendar day touched by the
requested period, including a partially selected day.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .data_engine import DataEngine
from .ev_charging_inference import bucket_for_time
from .financial_engine import FinancialEngine
from .household_timeline import HouseholdTariffResolver


LOCAL_TIMEZONE = ZoneInfo("Australia/Perth")
OCULAR_SOURCES = ("ocular_export", "ocular_csv")


def _as_datetime(value: date | datetime | str, *, end: bool = False) -> tuple[datetime, bool]:
    date_only = isinstance(value, date) and not isinstance(value, datetime)
    if isinstance(value, datetime):
        parsed = value
    elif date_only:
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("Period values must be ISO dates or datetimes") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        parsed = parsed.astimezone(LOCAL_TIMEZONE)
    if end and date_only:
        parsed += timedelta(days=1)
    return parsed, date_only


def _iso(value: datetime) -> str:
    return value.astimezone(LOCAL_TIMEZONE).isoformat(timespec="seconds")


def _unique_warning(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


class PeriodSummaryService:
    """Calculate retained household and EV facts for one selected period."""

    def __init__(
        self,
        *,
        connection: Optional[sqlite3.Connection] = None,
        db_path: Optional[str] = None,
        data_engine: Optional[DataEngine] = None,
        tariff_resolver: HouseholdTariffResolver,
    ) -> None:
        if data_engine is not None:
            self.data_engine = data_engine
        else:
            self.data_engine = DataEngine(
                connection=connection,
                db_path=db_path,
            )
        self.tariff_resolver = tariff_resolver
        self.finances = FinancialEngine()

    def get_period_summary(
        self,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> dict[str, Any]:
        period_start, start_was_date = _as_datetime(start)
        period_end, end_was_date = _as_datetime(end, end=True)
        if period_end <= period_start:
            raise ValueError("Period end must be after period start")

        warnings: list[str] = []
        tariff_state: dict[str, Any] = {
            "versions_used": [],
            "coverage_complete": True,
            "warnings": warnings,
        }
        reading_rows = self._readings(period_start, period_end)
        household_daily = self._household_daily(reading_rows, tariff_state)
        household_kwh = sum(float(row["grid_import_kwh"]) for row in reading_rows)
        household_cost = self._sum_costs(household_daily)
        if not reading_rows:
            _unique_warning(warnings, "No household readings were found in the selected period.")
            household_cost = None

        supply_cost = self._supply_cost(period_start, period_end, tariff_state)
        if supply_cost is None:
            household_cost = None
        elif household_cost is not None:
            household_cost += supply_cost
            for day in household_daily:
                if day["cost"] is not None:
                    day["cost"] += self._day_supply_cost(day["date"], tariff_state)

        sessions = self._ev_sessions(period_start, period_end)
        ev_sessions: list[dict[str, Any]] = []
        ev_kwh = 0.0
        ev_cost = 0.0
        ev_cost_complete = True
        for session in sessions:
            result = self._price_session(session, period_start, period_end, tariff_state)
            if result is None:
                continue
            ev_sessions.append(result)
            ev_kwh += result["energy_kwh"]
            if result["cost"] is None:
                ev_cost_complete = False
            else:
                ev_cost += result["cost"]
        if not ev_cost_complete:
            ev_cost_value: Optional[float] = None
        else:
            ev_cost_value = ev_cost

        percentage = None
        if reading_rows and household_kwh > 0 and ev_sessions:
            percentage = ev_kwh / household_kwh * 100.0
        elif ev_sessions and household_kwh <= 0:
            _unique_warning(warnings, "EV percentage is unavailable because household import is zero.")
        elif ev_sessions and not reading_rows:
            _unique_warning(warnings, "EV percentage is unavailable because household readings are missing.")

        if not ev_sessions:
            ev_kwh = 0.0
            ev_cost_value = 0.0 if not any("EV" in warning for warning in warnings) else None

        tariff_state["coverage_complete"] = not any(
            warning.startswith(("Tariff history", "No tariff version", "Invalid tariff"))
            for warning in warnings
        )
        return {
            "period": {
                "start": _iso(period_start),
                "end": _iso(period_end),
                "start_is_date": start_was_date,
                "end_is_date": end_was_date,
            },
            "household": {
                "grid_import_kwh": household_kwh,
                "cost": household_cost,
                "daily": household_daily,
            },
            "ev": {
                "energy_kwh": ev_kwh,
                "cost": ev_cost_value,
                "percentage_of_household": percentage,
                "sessions": ev_sessions,
            },
            "tariff": tariff_state,
        }

    def _readings(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        def query(conn: sqlite3.Connection):
            household_id = self.data_engine._active_household_id()
            rows = conn.execute(
                """
                SELECT measurement_time, tariff_bucket, grid_to_home_kwh,
                       grid_to_home_w, provider_id, source_id, import_cost
                FROM readings
                WHERE household_id = ?
                  AND measurement_time >= ? AND measurement_time < ?
                ORDER BY measurement_time
                """,
                (household_id, start.date().isoformat(), (end.date() + timedelta(days=1)).isoformat()),
            ).fetchall()
            result = []
            for row in rows:
                timestamp = self._parse_timestamp(row[0])
                if start <= timestamp < end:
                    result.append({
                        "measurement_time": timestamp,
                        "tariff_bucket": row[1],
                        "grid_import_kwh": float(row[2] or 0.0),
                        "grid_to_home_w": float(row[3] or 0.0),
                        "provider_id": row[4],
                        "source_id": row[5],
                        "import_cost": row[6],
                    })
            return result

        return self.data_engine._with_connection(query)

    def _ev_sessions(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        def query(conn: sqlite3.Connection):
            household_id = self.data_engine._active_household_id()
            rows = conn.execute(
                """
                SELECT transaction_id, start_time, end_time, duration_minutes,
                       energy_kwh, source, status
                FROM ev_sessions
                WHERE household_id = ? AND source IN (?, ?)
                  AND start_time < ?
                  AND COALESCE(end_time, start_time) >= ?
                ORDER BY start_time
                """,
                (
                    household_id,
                    *OCULAR_SOURCES,
                    (end.date() + timedelta(days=1)).isoformat(),
                    (start.date() - timedelta(days=1)).isoformat(),
                ),
            ).fetchall()
            sessions = []
            for row in rows:
                session_start = self._parse_timestamp(row[1])
                session_end = self._parse_timestamp(row[2]) if row[2] else None
                if session_end is None or session_end <= session_start:
                    continue
                if session_start < end and session_end > start:
                    sessions.append({
                        "transaction_id": row[0],
                        "start_time": session_start,
                        "end_time": session_end,
                        "duration_minutes": float(row[3] or (session_end - session_start).total_seconds() / 60),
                        "energy_kwh": float(row[4] or 0.0),
                        "source": row[5],
                        "status": row[6],
                    })
            return sessions

        return self.data_engine._with_connection(query)

    def _household_daily(self, rows: list[dict[str, Any]], tariff_state: dict[str, Any]) -> list[dict[str, Any]]:
        by_day: dict[date, dict[str, Any]] = {}
        for row in rows:
            day = row["measurement_time"].date()
            entry = by_day.setdefault(day, {"date": day.isoformat(), "grid_import_kwh": 0.0, "cost": 0.0, "_priced": True})
            entry["grid_import_kwh"] += row["grid_import_kwh"]
            tariff = self._tariff_for(day, tariff_state)
            if tariff is None:
                entry["_priced"] = False
                continue
            bucket = row["tariff_bucket"] or bucket_for_time(row["measurement_time"])
            entry["cost"] += self.finances.calculate_import_cost(row["grid_import_kwh"], tariff.usage_rate(bucket))
        result = []
        for day in sorted(by_day):
            entry = by_day[day]
            result.append({
                "date": entry["date"],
                "grid_import_kwh": entry["grid_import_kwh"],
                "cost": entry["cost"] if entry["_priced"] else None,
            })
        return result

    def _sum_costs(self, daily: list[dict[str, Any]]) -> Optional[float]:
        if not daily or any(item["cost"] is None for item in daily):
            return None
        return sum(float(item["cost"]) for item in daily)

    def _supply_cost(self, start: datetime, end: datetime, tariff_state: dict[str, Any]) -> Optional[float]:
        total = 0.0
        day = start.date()
        while day <= (end - timedelta(microseconds=1)).date():
            cost = self._day_supply_cost(day, tariff_state)
            if cost is None:
                return None
            total += cost
            day += timedelta(days=1)
        return total

    def _day_supply_cost(self, day_text: str | date, tariff_state: dict[str, Any]) -> float:
        day = day_text if isinstance(day_text, date) else date.fromisoformat(day_text)
        tariff = self._tariff_for(day, tariff_state)
        return float(tariff.supply_charge_per_day) if tariff is not None else 0.0

    def _price_session(
        self,
        session: dict[str, Any],
        period_start: datetime,
        period_end: datetime,
        tariff_state: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        overlap_start = max(session["start_time"], period_start)
        overlap_end = min(session["end_time"], period_end)
        if overlap_end <= overlap_start or session["energy_kwh"] <= 0:
            return None
        full_seconds = (session["end_time"] - session["start_time"]).total_seconds()
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        energy = session["energy_kwh"] * overlap_seconds / full_seconds
        cost: Optional[float] = 0.0
        cursor = overlap_start
        while cursor < overlap_end:
            boundary = min(self._next_tariff_boundary(cursor), overlap_end)
            tariff = self._tariff_for(cursor.date(), tariff_state)
            if tariff is None:
                cost = None
            elif cost is not None:
                segment_energy = energy * (boundary - cursor).total_seconds() / overlap_seconds
                cost += self.finances.cost_of_ev_session(segment_energy, tariff.usage_rate(bucket_for_time(cursor)))
            cursor = boundary
        return {
            "transaction_id": session["transaction_id"],
            "source": session["source"],
            "start_time": _iso(session["start_time"]),
            "end_time": _iso(session["end_time"]),
            "duration_minutes": session["duration_minutes"],
            "energy_kwh": energy,
            "cost": cost,
            "partial_allocation": overlap_start != session["start_time"] or overlap_end != session["end_time"],
        }

    @staticmethod
    def _next_tariff_boundary(value: datetime) -> datetime:
        boundaries = [
            datetime.combine(value.date() + timedelta(days=1), time.min, tzinfo=value.tzinfo),
            datetime.combine(value.date(), time(6), tzinfo=value.tzinfo),
            datetime.combine(value.date(), time(9), tzinfo=value.tzinfo),
            datetime.combine(value.date(), time(15), tzinfo=value.tzinfo),
            datetime.combine(value.date(), time(21), tzinfo=value.tzinfo),
            datetime.combine(value.date(), time(23), tzinfo=value.tzinfo),
        ]
        return min(boundary for boundary in boundaries if boundary > value)

    def _tariff_for(self, day: date, tariff_state: dict[str, Any]):
        period = self.tariff_resolver.timeline.active_tariff_period(day)
        if period is None:
            _unique_warning(tariff_state["warnings"], f"Tariff history does not cover {day.isoformat()}.")
            return None
        tariff = period.tariff_version()
        if not any(item.get("version_id") == tariff.version_id for item in tariff_state["versions_used"]):
            tariff_state["versions_used"].append(tariff.public_metadata())
        return tariff

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(LOCAL_TIMEZONE)


def get_period_summary(
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    connection: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
    data_engine: Optional[DataEngine] = None,
    tariff_resolver: HouseholdTariffResolver,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`PeriodSummaryService`."""

    return PeriodSummaryService(
        connection=connection,
        db_path=db_path,
        data_engine=data_engine,
        tariff_resolver=tariff_resolver,
    ).get_period_summary(start, end)
