#!/usr/bin/env python3
"""Trusted household data access for PowerMonitor intelligence engines.

The Data Engine owns source retrieval, freshness, completeness, and
normalisation. It deliberately does not calculate financial outcomes, generate
recommendations, render HTML, or depend on dashboard/report concepts.
"""

import json
import re
import sqlite3
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    Account,
    DecisionTimeline,
    EngineStatus,
    Evidence,
    Freshness,
    Household,
    HouseholdCapability,
    HouseholdContext,
    HouseholdMembership,
    TimelineEntry,
)

BUCKET_ORDER = ("overnight", "off_peak", "super_off_peak", "peak")
DEFAULT_ACCOUNT_ID = "local_account"
DEFAULT_HOUSEHOLD_ID = "default_household"
DEFAULT_HOUSEHOLD_NAME = "Default household"
DEFAULT_HOUSEHOLD_TIMEZONE = "Australia/Perth"
DEFAULT_HOUSEHOLD_CAPABILITIES = (
    "solar",
    "ev",
    "tariff",
    "billing",
    "baseload",
    "recommendations",
)
EV_BUCKET_LABELS = {
    "super_off_peak": "9am-3pm super off-peak / EV solar window",
    "overnight": "11pm-6am overnight",
    "off_peak": "off-peak",
    "peak": "peak",
}
EV_TARIFF_COLUMN_PAIRS = {
    bucket: (f"{bucket}_kwh", f"{bucket}_cost")
    for bucket in BUCKET_ORDER
}


@dataclass(frozen=True)
class BillPeriod:
    kind: str
    label: str
    start: date
    end: date

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True)
class HouseholdSnapshot:
    generated_at: str
    latest_measurement_time: Optional[str]
    freshness: Freshness
    connector_status: EngineStatus
    bill_period: Optional[BillPeriod] = None
    evidence: list[Evidence] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.bill_period:
            result["bill_period"] = self.bill_period.as_dict()
        return result


@dataclass(frozen=True)
class DataCompleteness:
    confidence: str
    confidence_note: str
    items: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DataEngine:
    """SQLite-backed source of truth for normalized household data."""

    name = "data_engine"

    def __init__(
        self,
        db_path: Optional[Path] = None,
        connection: Optional[sqlite3.Connection] = None,
        collect_log_path: Optional[Path] = None,
        report_path: Optional[Path] = None,
        health_loader: Optional[Callable[[], dict]] = None,
        household_context: Optional[HouseholdContext] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else None
        self.connection = connection
        self.collect_log_path = Path(collect_log_path) if collect_log_path else None
        self.report_path = Path(report_path) if report_path else None
        self.health_loader = health_loader or (lambda: {})
        self.household_context = household_context

    def status(self) -> EngineStatus:
        try:
            self.validate_database()
        except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
            return EngineStatus(name=self.name, state="failed", message=str(exc))
        return EngineStatus(name=self.name)

    def _with_connection(self, callback):
        if self.connection is not None:
            return callback(self.connection)
        if not self.db_path:
            raise FileNotFoundError("No database path configured")
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        with sqlite3.connect(self.db_path) as conn:
            return callback(conn)

    def validate_database(self) -> None:
        return self._with_connection(self.validate_connection)

    def get_household_context(self, household_id: Optional[str] = None) -> HouseholdContext:
        """Return household context, falling back to the default single-household mode."""

        if self.household_context and (
            household_id is None
            or self.household_context.household.household_id == household_id
        ):
            return self.household_context

        def query(conn):
            self.validate_connection(conn)
            selected_id = household_id or DEFAULT_HOUSEHOLD_ID
            household_row = conn.execute(
                """
                SELECT household_id, name, timezone, is_default
                FROM households
                WHERE household_id = ?
                """,
                (selected_id,),
            ).fetchone()
            if household_row is None and household_id is None:
                household_row = conn.execute(
                    """
                    SELECT household_id, name, timezone, is_default
                    FROM households
                    WHERE is_default = 1
                    ORDER BY household_id
                    LIMIT 1
                    """
                ).fetchone()
            if household_row is None:
                raise RuntimeError(f"Household not found: {selected_id}")

            account_row = conn.execute(
                """
                SELECT a.account_id, a.display_name, a.account_type, m.role
                FROM household_memberships m
                JOIN accounts a ON a.account_id = m.account_id
                WHERE m.household_id = ?
                ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END, a.account_id
                LIMIT 1
                """,
                (household_row[0],),
            ).fetchone()
            if account_row is None:
                account_row = (DEFAULT_ACCOUNT_ID, "Local account", "local", "owner")
            capability_rows = conn.execute(
                """
                SELECT capability_id, enabled, source
                FROM household_capabilities
                WHERE household_id = ?
                ORDER BY capability_id
                """,
                (household_row[0],),
            ).fetchall()
            account = Account(
                account_id=account_row[0],
                display_name=account_row[1],
                account_type=account_row[2],
            )
            household = Household(
                household_id=household_row[0],
                name=household_row[1],
                timezone=household_row[2],
                is_default=bool(household_row[3]),
                capabilities=[
                    HouseholdCapability(
                        capability_id=row[0],
                        enabled=bool(row[1]),
                        source=row[2],
                    )
                    for row in capability_rows
                ],
            )
            return HouseholdContext(
                account=account,
                household=household,
                membership=HouseholdMembership(
                    account_id=account.account_id,
                    household_id=household.household_id,
                    role=account_row[3],
                ),
            )

        context = self._with_connection(query)
        if household_id is None:
            self.household_context = context
        return context

    def _active_household_id(self) -> str:
        if self.household_context:
            return self.household_context.household.household_id
        return DEFAULT_HOUSEHOLD_ID

    @staticmethod
    def validate_connection(conn: sqlite3.Connection) -> None:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'readings'"
        ).fetchone()
        if not table:
            raise RuntimeError("No readings table found")

        required = {
            "measurement_time",
            "tariff_bucket",
            "production_kwh",
            "solar_to_home_kwh",
            "solar_to_grid_kwh",
            "grid_to_home_w",
            "grid_to_home_kwh",
            "import_cost",
        }
        present = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"Readings table is missing columns: {', '.join(missing)}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ev_sessions (
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                energy_kwh REAL NOT NULL,
                source TEXT NOT NULL,
                cost_estimate REAL
            )
            """
        )
        present_ev_columns = {row[1] for row in conn.execute("PRAGMA table_info(ev_sessions)")}
        ev_column_sql = {
            "transaction_id": "ALTER TABLE ev_sessions ADD COLUMN transaction_id TEXT",
            "last_seen": "ALTER TABLE ev_sessions ADD COLUMN last_seen TEXT",
            "max_power_kw": "ALTER TABLE ev_sessions ADD COLUMN max_power_kw REAL",
            "status": "ALTER TABLE ev_sessions ADD COLUMN status TEXT",
            "tariff_breakdown_json": "ALTER TABLE ev_sessions ADD COLUMN tariff_breakdown_json TEXT",
            "first_seen_energy_kwh": "ALTER TABLE ev_sessions ADD COLUMN first_seen_energy_kwh REAL",
            "last_energy_kwh": "ALTER TABLE ev_sessions ADD COLUMN last_energy_kwh REAL",
            "duration_minutes": "ALTER TABLE ev_sessions ADD COLUMN duration_minutes REAL",
            "cost_partial": "ALTER TABLE ev_sessions ADD COLUMN cost_partial INTEGER DEFAULT 0",
            "start_source": "ALTER TABLE ev_sessions ADD COLUMN start_source TEXT",
            "stop_source": "ALTER TABLE ev_sessions ADD COLUMN stop_source TEXT",
            "notes_json": "ALTER TABLE ev_sessions ADD COLUMN notes_json TEXT",
        }
        for bucket, (kwh_column, cost_column) in EV_TARIFF_COLUMN_PAIRS.items():
            ev_column_sql[kwh_column] = f"ALTER TABLE ev_sessions ADD COLUMN {kwh_column} REAL DEFAULT 0"
            ev_column_sql[cost_column] = f"ALTER TABLE ev_sessions ADD COLUMN {cost_column} REAL DEFAULT 0"
        for column, statement in ev_column_sql.items():
            if column not in present_ev_columns:
                conn.execute(statement)
        DataEngine._backfill_ev_tariff_columns(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ev_sessions_transaction_id
            ON ev_sessions(transaction_id)
            WHERE transaction_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_periods (
                name TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                actual_bill_amount REAL,
                plan_name TEXT,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_entries (
                event_key TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                financial_impact TEXT,
                confidence TEXT NOT NULL,
                evidence_sources_json TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timeline_entries_timestamp
            ON timeline_entries(timestamp)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recommendation_lifecycle (
                stable_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'active',
                snoozed_until TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recommendation_lifecycle_state
            ON recommendation_lifecycle(state)
            """
        )
        DataEngine._ensure_household_tables(conn)
        DataEngine._ensure_household_scoping(conn)
        conn.commit()

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
        )

    @staticmethod
    def _ensure_household_scoping(conn: sqlite3.Connection) -> None:
        scoped_tables = (
            "readings",
            "ev_sessions",
            "billing_periods",
            "timeline_entries",
        )
        for table in scoped_tables:
            if DataEngine._table_exists(conn, table):
                DataEngine._ensure_household_column(conn, table)
                DataEngine._ensure_default_household_trigger(conn, table)

        DataEngine._migrate_recommendation_lifecycle_table(conn)
        DataEngine._ensure_default_household_trigger(conn, "recommendation_lifecycle")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_readings_household_time
            ON readings(household_id, measurement_time)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ev_sessions_household_start
            ON ev_sessions(household_id, start_time)
            """
        )
        conn.execute("DROP INDEX IF EXISTS idx_ev_sessions_transaction_id")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ev_sessions_household_transaction_id
            ON ev_sessions(household_id, transaction_id)
            WHERE transaction_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timeline_entries_household_timestamp
            ON timeline_entries(household_id, timestamp)
            """
        )

    @staticmethod
    def _ensure_household_column(conn: sqlite3.Connection, table: str) -> None:
        columns = DataEngine._table_columns(conn, table)
        if "household_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN household_id TEXT")
        conn.execute(
            f"""
            UPDATE {table}
            SET household_id = ?
            WHERE household_id IS NULL OR household_id = ''
            """,
            (DEFAULT_HOUSEHOLD_ID,),
        )

    @staticmethod
    def _ensure_default_household_trigger(conn: sqlite3.Connection, table: str) -> None:
        trigger_name = f"trg_{table}_default_household_id"
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_name}
            AFTER INSERT ON {table}
            FOR EACH ROW
            WHEN NEW.household_id IS NULL OR NEW.household_id = ''
            BEGIN
                UPDATE {table}
                SET household_id = '{DEFAULT_HOUSEHOLD_ID}'
                WHERE rowid = NEW.rowid;
            END
            """
        )

    @staticmethod
    def _migrate_recommendation_lifecycle_table(conn: sqlite3.Connection) -> None:
        if not DataEngine._table_exists(conn, "recommendation_lifecycle"):
            conn.execute(
                """
                CREATE TABLE recommendation_lifecycle (
                    household_id TEXT NOT NULL DEFAULT 'default_household',
                    stable_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    snoozed_until TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (household_id, stable_id)
                )
                """
            )
        else:
            columns = DataEngine._table_columns(conn, "recommendation_lifecycle")
            pk_columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(recommendation_lifecycle)")
                if row[5]
            ]
            if "household_id" not in columns or pk_columns == ["stable_id"]:
                conn.execute("ALTER TABLE recommendation_lifecycle RENAME TO recommendation_lifecycle_legacy")
                conn.execute(
                    """
                    CREATE TABLE recommendation_lifecycle (
                        household_id TEXT NOT NULL DEFAULT 'default_household',
                        stable_id TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'active',
                        snoozed_until TEXT,
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (household_id, stable_id)
                    )
                    """
                )
                legacy_columns = DataEngine._table_columns(conn, "recommendation_lifecycle_legacy")
                household_expr = "household_id" if "household_id" in legacy_columns else f"'{DEFAULT_HOUSEHOLD_ID}'"
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO recommendation_lifecycle (
                        household_id,
                        stable_id,
                        state,
                        snoozed_until,
                        reason,
                        created_at,
                        updated_at
                    )
                    SELECT
                        COALESCE(NULLIF({household_expr}, ''), ?),
                        stable_id,
                        state,
                        snoozed_until,
                        reason,
                        created_at,
                        updated_at
                    FROM recommendation_lifecycle_legacy
                    """,
                    (DEFAULT_HOUSEHOLD_ID,),
                )
                conn.execute("DROP TABLE recommendation_lifecycle_legacy")
            else:
                DataEngine._ensure_household_column(conn, "recommendation_lifecycle")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recommendation_lifecycle_household_state
            ON recommendation_lifecycle(household_id, state)
            """
        )

    @staticmethod
    def _ensure_household_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'local'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS households (
                household_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                timezone TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS household_capabilities (
                household_id TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                source TEXT,
                PRIMARY KEY (household_id, capability_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS household_memberships (
                account_id TEXT NOT NULL,
                household_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                PRIMARY KEY (account_id, household_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts (account_id, display_name, account_type)
            VALUES (?, ?, ?)
            """,
            (DEFAULT_ACCOUNT_ID, "Local account", "local"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO households (household_id, name, timezone, is_default)
            VALUES (?, ?, ?, 1)
            """,
            (DEFAULT_HOUSEHOLD_ID, DEFAULT_HOUSEHOLD_NAME, DEFAULT_HOUSEHOLD_TIMEZONE),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO household_memberships (account_id, household_id, role)
            VALUES (?, ?, ?)
            """,
            (DEFAULT_ACCOUNT_ID, DEFAULT_HOUSEHOLD_ID, "owner"),
        )
        for capability in DEFAULT_HOUSEHOLD_CAPABILITIES:
            conn.execute(
                """
                INSERT OR IGNORE INTO household_capabilities (
                    household_id,
                    capability_id,
                    enabled,
                    source
                ) VALUES (?, ?, 1, ?)
                """,
                (DEFAULT_HOUSEHOLD_ID, capability, "default"),
            )

    @staticmethod
    def _backfill_ev_tariff_columns(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT rowid, tariff_breakdown_json
            FROM ev_sessions
            WHERE tariff_breakdown_json IS NOT NULL
              AND (
                COALESCE(super_off_peak_kwh, 0) + COALESCE(overnight_kwh, 0) +
                COALESCE(off_peak_kwh, 0) + COALESCE(peak_kwh, 0)
              ) = 0
            """
        ).fetchall()
        for rowid, raw_breakdown in rows:
            try:
                parsed = json.loads(raw_breakdown) if raw_breakdown else {}
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            values = []
            assignments = []
            for bucket in BUCKET_ORDER:
                bucket_values = parsed.get(bucket, {})
                if not isinstance(bucket_values, dict):
                    bucket_values = {}
                kwh_column, cost_column = EV_TARIFF_COLUMN_PAIRS[bucket]
                assignments.extend((f"{kwh_column} = ?", f"{cost_column} = ?"))
                values.extend((
                    float(bucket_values.get("kwh") or 0),
                    float(bucket_values.get("cost") or 0),
                ))
            conn.execute(
                f"UPDATE ev_sessions SET {', '.join(assignments)} WHERE rowid = ?",
                (*values, rowid),
            )

    def get_bill_period(self, today: date, settings: Any, kind: str = "current_billing_period") -> BillPeriod:
        if kind == "current_billing_period":
            start_day = int(settings.billing_period_start_day)
            period_start = today.replace(day=start_day) if today.day >= start_day else (
                today.replace(day=1) - timedelta(days=1)
            ).replace(day=start_day)
            period_end = today
            if getattr(settings, "current_billing_period_start", None):
                period_start = settings.current_billing_period_start
            return BillPeriod(kind, "Current billing period", period_start, period_end)
        if kind == "previous_billing_period":
            if getattr(settings, "previous_billing_period_start", None) and getattr(settings, "previous_billing_period_end", None):
                return BillPeriod(
                    kind,
                    "Previous billing period",
                    settings.previous_billing_period_start,
                    settings.previous_billing_period_end,
                )
            current = self.get_bill_period(today, settings)
            days = (current.end - current.start).days + 1
            end = current.start - timedelta(days=1)
            return BillPeriod(kind, "Previous billing period", end - timedelta(days=days - 1), end)
        raise ValueError(f"Unsupported bill period: {kind}")

    def get_current_bill_summary(self, start: date, end: date) -> dict[str, Any]:
        rows = self.get_grid_intervals(start, end)
        solar_rows = self.get_solar_intervals(start, end)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "intervals": len(rows),
            "reading_days": len({row["measurement_time"][:10] for row in rows}),
            "grid_import_kwh": sum(row["grid_to_home_kwh"] for row in rows),
            "solar_generated_kwh": sum(row["production_kwh"] for row in solar_rows),
            "solar_exported_kwh": sum(row["solar_to_grid_kwh"] for row in solar_rows),
        }

    def get_grid_intervals(self, start: date, end: date) -> list[dict[str, Any]]:
        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()

        def query(conn):
            rows = conn.execute(
                """
                SELECT measurement_time, tariff_bucket, grid_to_home_w, grid_to_home_kwh
                FROM readings
                WHERE household_id = ?
                  AND measurement_time >= ? AND measurement_time < ?
                ORDER BY measurement_time
                """,
                (household_id, start_text, end_exclusive),
            ).fetchall()
            return [
                {
                    "measurement_time": row[0],
                    "tariff_bucket": row[1],
                    "grid_to_home_w": float(row[2] or 0),
                    "grid_to_home_kwh": float(row[3] or 0),
                }
                for row in rows
            ]

        return self._with_connection(query)

    def get_solar_intervals(self, start: date, end: date) -> list[dict[str, Any]]:
        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()

        def query(conn):
            rows = conn.execute(
                """
                SELECT measurement_time, production_kwh, solar_to_home_kwh, solar_to_grid_kwh
                FROM readings
                WHERE household_id = ?
                  AND measurement_time >= ? AND measurement_time < ?
                ORDER BY measurement_time
                """,
                (household_id, start_text, end_exclusive),
            ).fetchall()
            return [
                {
                    "measurement_time": row[0],
                    "production_kwh": float(row[1] or 0),
                    "solar_to_home_kwh": float(row[2] or 0),
                    "solar_to_grid_kwh": float(row[3] or 0),
                }
                for row in rows
            ]

        return self._with_connection(query)

    def get_ev_sessions(
        self,
        start: date,
        end: date,
        sources: Optional[list[str]] = None,
        prefer_source: Optional[str] = "ocular_export",
    ) -> list[dict[str, Any]]:
        if not sources:
            return []
        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()
        tariff_columns = [
            column
            for bucket in BUCKET_ORDER
            for column in EV_TARIFF_COLUMN_PAIRS[bucket]
        ]

        def query(conn):
            rows = conn.execute(
                """
                SELECT
                    transaction_id,
                    start_time,
                    end_time,
                    COALESCE(last_seen, end_time, start_time) AS last_seen_value,
                    energy_kwh,
                    cost_estimate,
                    cost_partial,
                    source,
                    duration_minutes,
                    max_power_kw,
                    tariff_breakdown_json,
                    {}
                FROM ev_sessions
                WHERE household_id = ?
                  AND source IN ({})
                  AND COALESCE(last_seen, end_time, start_time) >= ?
                  AND start_time < ?
                ORDER BY start_time
                """.format(
                    ", ".join(tariff_columns),
                    ",".join("?" for _ in sources),
                ),
                (household_id, *sources, start_text, end_exclusive),
            ).fetchall()
            if rows and prefer_source and any(row[7] == prefer_source for row in rows):
                rows = [row for row in rows if row[7] == prefer_source]
            return [self._normalise_ev_session(row) for row in rows]

        return self._with_connection(query)

    @staticmethod
    def _normalise_ev_session(row) -> dict[str, Any]:
        breakdown = {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in BUCKET_ORDER}
        stored_total = 0.0
        column_offset = 11
        for index, bucket in enumerate(BUCKET_ORDER):
            kwh_value = float(row[column_offset + index * 2] or 0)
            cost_value = float(row[column_offset + index * 2 + 1] or 0)
            breakdown[bucket] = {"kwh": kwh_value, "cost": cost_value}
            stored_total += abs(kwh_value) + abs(cost_value)
        if stored_total <= 0:
            try:
                parsed = json.loads(row[10]) if row[10] else {}
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                for bucket in BUCKET_ORDER:
                    values = parsed.get(bucket, {})
                    if isinstance(values, dict):
                        breakdown[bucket] = {
                            "kwh": float(values.get("kwh") or 0),
                            "cost": float(values.get("cost") or 0),
                        }

        energy = float(row[4] or 0)
        cost = sum(values["cost"] for values in breakdown.values())
        if cost <= 0 and row[5] is not None:
            cost = float(row[5] or 0)
        start_dt = parse_iso_datetime(row[1])
        end_dt = parse_iso_datetime(row[2]) or parse_iso_datetime(row[3])
        duration = float(row[8]) if row[8] is not None else None
        if duration is None and start_dt and end_dt:
            duration = max((end_dt - start_dt).total_seconds() / 60, 0.0)
        max_power = float(row[9]) if row[9] is not None else None
        if max_power is None and duration and duration > 0:
            max_power = energy / (duration / 60)
        dominant = max(BUCKET_ORDER, key=lambda bucket: breakdown[bucket]["kwh"]) if energy > 0 else None
        return {
            "transaction_id": row[0],
            "start_time": row[1],
            "end_time": row[2],
            "date": start_dt.date().isoformat() if start_dt else None,
            "start_time_display": start_dt.strftime("%H:%M") if start_dt else "n/a",
            "end_time_display": end_dt.strftime("%H:%M") if end_dt else "n/a",
            "duration_minutes": duration,
            "duration_display": format_duration(duration),
            "energy_kwh": energy,
            "cost": cost,
            "cost_per_kwh": cost / energy if energy else 0.0,
            "source": row[7],
            "cost_partial": bool(row[6]),
            "max_power_kw": max_power,
            "dominant_tariff": dominant,
            "dominant_tariff_label": EV_BUCKET_LABELS.get(dominant, dominant or "n/a"),
            "tariff_breakdown": breakdown,
        }

    def get_overnight_baseload_watts(self, start: date, end: date) -> float:
        values = [
            row["grid_to_home_w"]
            for row in self.get_grid_intervals(start, end)
            if row["tariff_bucket"] == "overnight"
        ]
        if not values:
            return 0.0
        ordered = sorted(values)
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[midpoint]
        return (ordered[midpoint - 1] + ordered[midpoint]) / 2

    def get_ev_source_counts(self) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            rows = conn.execute(
                """
                SELECT source, COUNT(*), COALESCE(SUM(energy_kwh), 0), COALESCE(SUM(cost_estimate), 0)
                FROM ev_sessions
                WHERE household_id = ?
                GROUP BY source
                ORDER BY source
                """,
                (household_id,),
            ).fetchall()
            return [
                {"source": row[0] or "unknown", "sessions": int(row[1]), "energy_kwh": float(row[2] or 0), "cost": float(row[3] or 0)}
                for row in rows
            ]

        return self._with_connection(query)

    def get_daily_ev_session_totals(
        self,
        start: date,
        end: date,
        sources: Optional[list[str]] = None,
        prefer_source: Optional[str] = "ocular_export",
    ) -> dict[str, dict[str, Any]]:
        if not sources:
            return {}
        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()

        def query(conn):
            rows = conn.execute(
                """
                SELECT
                    date(start_time) AS session_day,
                    source,
                    COUNT(*),
                    COALESCE(SUM(energy_kwh), 0)
                FROM ev_sessions
                WHERE household_id = ?
                  AND source IN ({})
                  AND start_time >= ?
                  AND start_time < ?
                GROUP BY session_day, source
                ORDER BY session_day, source
                """.format(",".join("?" for _ in sources)),
                (household_id, *sources, start_text, end_exclusive),
            ).fetchall()
            by_day: dict[str, list[tuple[str, int, float]]] = {}
            for day_text, source, sessions, energy_kwh in rows:
                by_day.setdefault(day_text, []).append(
                    (source, int(sessions), float(energy_kwh or 0))
                )

            totals = {}
            for day_text, candidates in by_day.items():
                chosen = None
                if prefer_source:
                    chosen = next(
                        (candidate for candidate in candidates if candidate[0] == prefer_source),
                        None,
                    )
                if chosen is None:
                    chosen = candidates[0]
                totals[day_text] = {
                    "source": chosen[0],
                    "sessions": chosen[1],
                    "energy_kwh": chosen[2],
                }
            return totals

        return self._with_connection(query)

    def set_recommendation_lifecycle(
        self,
        stable_id: str,
        state: str,
        snoozed_until: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        if state not in {"active", "dismissed", "snoozed_until"}:
            raise ValueError(f"Unsupported recommendation lifecycle state: {state}")
        if state == "snoozed_until" and not snoozed_until:
            raise ValueError("snoozed_until is required for snoozed recommendations")
        if state != "snoozed_until":
            snoozed_until = None
        household_id = self._active_household_id()

        def upsert(conn):
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO recommendation_lifecycle (
                    household_id,
                    stable_id,
                    state,
                    snoozed_until,
                    reason,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(household_id, stable_id) DO UPDATE SET
                    state = excluded.state,
                    snoozed_until = excluded.snoozed_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (household_id, stable_id, state, snoozed_until, reason, now, now),
            )
            conn.commit()
            return self.get_recommendation_lifecycle([stable_id]).get(stable_id, {})

        return self._with_connection(upsert)

    def get_recommendation_lifecycle(
        self,
        stable_ids: Optional[list[str]] = None,
    ) -> dict[str, dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            if stable_ids:
                rows = conn.execute(
                    """
                    SELECT stable_id, state, snoozed_until, reason, updated_at
                    FROM recommendation_lifecycle
                    WHERE household_id = ?
                      AND stable_id IN ({})
                    """.format(",".join("?" for _ in stable_ids)),
                    (household_id, *stable_ids),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT stable_id, state, snoozed_until, reason, updated_at
                    FROM recommendation_lifecycle
                    WHERE household_id = ?
                    """,
                    (household_id,),
                ).fetchall()
            return {
                row[0]: {
                    "stable_id": row[0],
                    "state": row[1],
                    "snoozed_until": row[2],
                    "reason": row[3],
                    "updated_at": row[4],
                }
                for row in rows
            }

        return self._with_connection(query)

    def get_recommendation_lifecycle_counts(self) -> dict[str, int]:
        household_id = self._active_household_id()

        def query(conn):
            rows = conn.execute(
                """
                SELECT state, COUNT(*)
                FROM recommendation_lifecycle
                WHERE household_id = ?
                GROUP BY state
                """,
                (household_id,),
            ).fetchall()
            counts = {"active": 0, "dismissed": 0, "snoozed_until": 0}
            for state, count in rows:
                if state in counts:
                    counts[state] = int(count)
            return counts

        return self._with_connection(query)

    def persist_timeline_entries(self, entries: list[TimelineEntry]) -> DecisionTimeline:
        household_id = self._active_household_id()

        def upsert(conn):
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            for entry in entries:
                conn.execute(
                    """
                    INSERT INTO timeline_entries (
                        household_id,
                        event_key,
                        timestamp,
                        title,
                        summary,
                        financial_impact,
                        confidence,
                        evidence_sources_json,
                        event_type,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        household_id = excluded.household_id,
                        timestamp = excluded.timestamp,
                        title = excluded.title,
                        summary = excluded.summary,
                        financial_impact = excluded.financial_impact,
                        confidence = excluded.confidence,
                        evidence_sources_json = excluded.evidence_sources_json,
                        event_type = excluded.event_type,
                        updated_at = excluded.updated_at
                    """,
                    (
                        household_id,
                        self._timeline_event_key(entry, household_id),
                        entry.timestamp,
                        entry.title,
                        entry.summary,
                        entry.financial_impact,
                        entry.confidence,
                        json.dumps(entry.evidence_sources, sort_keys=True),
                        entry.event_type,
                        now,
                        now,
                    ),
                )
            conn.commit()
            return self.get_timeline_entries()

        return self._with_connection(upsert)

    def get_timeline_entries(
        self,
        start: Optional[date] = None,
        end: Optional[date] = None,
        limit: int = 100,
    ) -> DecisionTimeline:
        start_text = start.isoformat() if start else None
        end_text = end.isoformat() if end else None
        household_id = self._active_household_id()

        def query(conn):
            conditions = ["household_id = ?"]
            params: list[Any] = [household_id]
            if start_text:
                conditions.append("timestamp >= ?")
                params.append(start_text)
            if end_text:
                conditions.append("timestamp <= ?")
                params.append(end_text)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = conn.execute(
                f"""
                SELECT
                    timestamp,
                    title,
                    summary,
                    financial_impact,
                    confidence,
                    evidence_sources_json,
                    event_type
                FROM timeline_entries
                {where}
                ORDER BY timestamp, title
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            entries = []
            for row in rows:
                try:
                    evidence_sources = json.loads(row[5]) if row[5] else []
                except json.JSONDecodeError:
                    evidence_sources = []
                entries.append(
                    TimelineEntry(
                        timestamp=row[0],
                        title=row[1],
                        summary=row[2],
                        financial_impact=row[3],
                        confidence=row[4],
                        evidence_sources=list(evidence_sources),
                        event_type=row[6],
                    )
                )
            return DecisionTimeline(entries=entries)

        return self._with_connection(query)

    @staticmethod
    def _timeline_event_key(entry: TimelineEntry, household_id: str = DEFAULT_HOUSEHOLD_ID) -> str:
        raw = "|".join((household_id, entry.event_type, entry.timestamp, entry.title))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_data_completeness(
        self,
        summary: dict[str, Any],
        billing_configured: bool,
        confidence: str,
        confidence_note: str,
    ) -> DataCompleteness:
        solar_present = any(float(summary.get(key) or 0) > 0 for key in ("solar_generated_kwh", "solar_used_kwh", "solar_exported_kwh"))
        grid_present = int(summary.get("intervals") or 0) > 0
        ev_actual_present = summary.get("ev_charging_source") in {"ocular_export", "home_assistant_ocpp"}
        return DataCompleteness(
            confidence=confidence,
            confidence_note=confidence_note,
            items=[
                {"name": "Solar data", "status": "present" if solar_present else "missing"},
                {"name": "Grid data", "status": "present" if grid_present else "missing"},
                {"name": "EV actual sessions", "status": "present" if ev_actual_present else "missing"},
                {"name": "Billing period", "status": "configured" if billing_configured else "missing"},
            ],
        )

    def get_connector_status(
        self,
        today: date,
        bill_period: Optional[BillPeriod] = None,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()
        latest_measurement = self._latest_measurement_time()
        latest_collect, collect_status = self._latest_collect_log_entry()
        collector_health = self.health_loader()
        report_timestamp = collector_health.get("last_successful_report_generation")
        report_age_minutes = None
        if self.report_path and self.report_path.exists():
            report_modified = datetime.fromtimestamp(self.report_path.stat().st_mtime).astimezone()
            try:
                recorded_report = datetime.fromisoformat(report_timestamp) if report_timestamp else None
            except ValueError:
                recorded_report = None
            if not recorded_report or report_modified > recorded_report:
                report_timestamp = report_modified.isoformat(timespec="seconds")
            report_age_minutes = max(
                (datetime.now().astimezone() - report_modified).total_seconds() / 60,
                0.0,
            )

        status = collector_health.get("status") or collect_status or "collect log unavailable"
        if not latest_measurement:
            if status == "ok":
                status = "no readings"
        else:
            try:
                if status == "ok" and datetime.fromisoformat(latest_measurement).date() < today:
                    status = "stale"
            except ValueError:
                status = "invalid reading timestamp"

        result = {
            "household_id": household_id,
            "status": status,
            "failure_type": collector_health.get("failure_type"),
            "collector_message": collector_health.get("message"),
            "last_successful_collection": collector_health.get("last_success_at"),
            "last_collection_attempt": collector_health.get("last_attempt_at"),
            "last_collection_failure": collector_health.get("last_failure_at"),
            "last_successful_report_generation": report_timestamp,
            "last_report_generation_failure": collector_health.get("last_report_failure_at"),
            "report_age_minutes": report_age_minutes,
            "consecutive_failures": collector_health.get("consecutive_failures", 0),
            "days_since_last_successful_collection": collector_health.get(
                "days_since_last_successful_collection"
            ),
            "failure_notification_sent_at": collector_health.get("notification_sent_at"),
            "latest_measurement_time": latest_measurement,
            "latest_collect_log_timestamp": latest_collect,
        }
        if bill_period:
            result["current_billing_period_start"] = bill_period.start.isoformat()
            result["current_billing_period_end"] = bill_period.end.isoformat()
        return result

    def get_latest_household_snapshot(
        self,
        today: date,
        bill_period: Optional[BillPeriod] = None,
    ) -> HouseholdSnapshot:
        latest = self._latest_measurement_time()
        status = self.get_connector_status(today, bill_period)
        freshness = freshness_from_latest(latest, today)
        return HouseholdSnapshot(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            latest_measurement_time=latest,
            freshness=freshness,
            connector_status=EngineStatus(
                name="collector",
                state="ready" if status.get("status") == "ok" else "degraded",
                message=status.get("status"),
                freshness=freshness,
            ),
            bill_period=bill_period,
            evidence=[
                Evidence("latest_measurement_time", latest),
                Evidence("collector_status", status.get("status")),
            ],
        )

    def _latest_measurement_time(self) -> Optional[str]:
        household_id = self._active_household_id()

        def query(conn):
            row = conn.execute(
                """
                SELECT MAX(measurement_time)
                FROM readings
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            return row[0] if row else None

        return self._with_connection(query)

    def _latest_collect_log_entry(self) -> tuple[Optional[str], Optional[str]]:
        if not self.collect_log_path or not self.collect_log_path.exists():
            return None, None
        timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)")
        latest_timestamp = None
        latest_status = None
        with self.collect_log_path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                match = timestamp_pattern.match(line)
                if match:
                    latest_timestamp = match.group(1)
                    if " ERROR " in line:
                        latest_status = "error"
                    elif "Collection succeeded" in line:
                        latest_status = "ok"
                    elif "Starting SolarEdge collection" in line:
                        latest_status = "collecting"
        return latest_timestamp, latest_status


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "n/a"
    total = max(int(round(minutes)), 0)
    hours, remainder = divmod(total, 60)
    return f"{hours}h {remainder:02d}m"


def freshness_from_latest(latest_measurement: Optional[str], today: date) -> Freshness:
    if not latest_measurement:
        return Freshness(state="unknown")
    try:
        observed = datetime.fromisoformat(latest_measurement)
    except ValueError:
        return Freshness(state="unknown", observed_at=latest_measurement)
    state = "fresh" if observed.date() >= today else "stale"
    return Freshness(state=state, observed_at=latest_measurement)
