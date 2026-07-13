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

from .import_framework import ImportJob, ImportPreview, ImportResult, ImportSource
from .anomaly_engine import AnomalyEngine, HouseholdAnomaly
from .behaviour_engine import BehaviourEngine, HouseholdBehaviour
from .bill_reconciliation_engine import BillReconciliation
from .decision_engine import DecisionEngine
from .models import (
    Account,
    CapabilityStatus,
    DecisionTimeline,
    EngineStatus,
    Evidence,
    Freshness,
    Household,
    HouseholdCapabilitySummary,
    HouseholdCapability,
    HouseholdContext,
    HouseholdMembership,
    TimelineEntry,
)
from .measurement_framework import (
    MeasurementCapability,
    MeasurementProviderRegistry,
    MeasurementType,
    SolarEdgeMeasurementProvider,
    UniversalMeasurement,
)
from .notification_engine import (
    NotificationChannel,
    NotificationDeliveryState,
    NotificationPreferences,
    NotificationPriority,
    NotificationQuietHours,
    NotificationRulePreference,
    NotificationSuppressionReason,
    NotificationType,
)
from .provider_framework import (
    ProviderCapability,
    ProviderFreshness,
    ProviderObservation,
    ProviderObservationQuality,
    ProviderObservationType,
    ProviderType,
)
from .promotion_engine import PromotionEngine, PromotionResult
from .state_change_engine import StateChange, StateChangeEngine

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
        reading_metadata_columns = {
            "provider_id": "ALTER TABLE readings ADD COLUMN provider_id TEXT",
            "source_id": "ALTER TABLE readings ADD COLUMN source_id TEXT",
            "source_resolution": "ALTER TABLE readings ADD COLUMN source_resolution TEXT",
            "source_record_id": "ALTER TABLE readings ADD COLUMN source_record_id TEXT",
            "import_job_id": "ALTER TABLE readings ADD COLUMN import_job_id TEXT",
            "imported_at": "ALTER TABLE readings ADD COLUMN imported_at TEXT",
        }
        for column, statement in reading_metadata_columns.items():
            if column not in present:
                conn.execute(statement)
        energy_columns = [
            column
            for column in ("production_kwh", "consumption_kwh", "solar_to_grid_kwh", "grid_to_home_kwh")
            if column in present
        ]
        if energy_columns:
            energy_where = " OR ".join(f"{column} IS NOT NULL" for column in energy_columns)
            conn.execute(
                f"""
                UPDATE readings
                SET provider_id = COALESCE(NULLIF(provider_id, ''), 'solaredge'),
                    source_id = COALESCE(NULLIF(source_id, ''), 'solaredge_dashboard_power'),
                    source_resolution = COALESCE(NULLIF(source_resolution, ''), 'interval_15m')
                WHERE {energy_where}
                """
            )

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS import_jobs (
                job_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                file_path TEXT,
                preview_json TEXT,
                result_json TEXT,
                message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_import_jobs_household_created
            ON import_jobs(household_id, created_at DESC)
            """
        )
        DataEngine._ensure_provider_observation_table(conn)
        DataEngine._ensure_historical_backfill_tables(conn)
        DataEngine._ensure_promotion_table(conn)
        DataEngine._ensure_household_state_snapshot_table(conn)
        DataEngine._ensure_state_change_table(conn)
        DataEngine._ensure_notification_tables(conn)
        DataEngine._ensure_synergy_bill_tables(conn)
        DataEngine._ensure_bill_reconciliation_table(conn)
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
    def _ensure_provider_observation_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_observations (
                observation_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                provider_id TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                capability TEXT NOT NULL,
                observation_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                value_json TEXT,
                unit TEXT,
                quality TEXT NOT NULL,
                freshness_json TEXT,
                provenance TEXT,
                raw_reference TEXT,
                import_job_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_observations_household_observed
            ON provider_observations(household_id, observed_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_observations_provider_capability
            ON provider_observations(household_id, provider_id, capability)
            """
        )

    @staticmethod
    def _ensure_historical_backfill_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_backfill_jobs (
                job_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                provider_id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                range_preset TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                resolution TEXT NOT NULL,
                quality_tier TEXT NOT NULL,
                data_types_json TEXT NOT NULL,
                progress_json TEXT NOT NULL,
                diagnostics_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historical_backfill_jobs_provider
            ON historical_backfill_jobs(household_id, provider_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historical_backfill_jobs_household_updated
            ON historical_backfill_jobs(household_id, updated_at DESC)
            """
        )

    @staticmethod
    def _ensure_promotion_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promotion_decisions (
                promotion_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                observation_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                capability TEXT NOT NULL,
                observation_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                state TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                conflict_json TEXT,
                value_json TEXT,
                promoted_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_promotion_decisions_household_promoted
            ON promotion_decisions(household_id, promoted_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_promotion_decisions_household_state
            ON promotion_decisions(household_id, state)
            """
        )

    @staticmethod
    def _ensure_household_state_snapshot_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS household_state_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                captured_at TEXT NOT NULL,
                material_hash TEXT NOT NULL,
                tariff_state_json TEXT NOT NULL,
                power_state_json TEXT NOT NULL,
                energy_state_json TEXT NOT NULL,
                solar_state_json TEXT NOT NULL,
                ev_state_json TEXT NOT NULL,
                battery_state_json TEXT NOT NULL,
                billing_state_json TEXT NOT NULL,
                collector_state_json TEXT NOT NULL,
                evidence_state_json TEXT NOT NULL,
                provider_provenance_json TEXT NOT NULL,
                context_summary_json TEXT,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_household_state_snapshots_household_captured
            ON household_state_snapshots(household_id, captured_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_household_state_snapshots_household_hash
            ON household_state_snapshots(household_id, material_hash)
            """
        )

    @staticmethod
    def _ensure_state_change_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_changes (
                change_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                event_key TEXT NOT NULL,
                change_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                previous_snapshot_id TEXT NOT NULL,
                current_snapshot_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_state_changes_household_event_key
            ON state_changes(household_id, event_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_state_changes_household_detected
            ON state_changes(household_id, detected_at DESC)
            """
        )

    @staticmethod
    def _ensure_notification_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_preferences (
                household_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                priority_threshold TEXT NOT NULL,
                quiet_hours_enabled INTEGER NOT NULL,
                quiet_hours_start TEXT NOT NULL,
                quiet_hours_end TEXT NOT NULL,
                quiet_hours_timezone TEXT NOT NULL,
                channel_preference TEXT NOT NULL,
                cooldown_minutes INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_rule_preferences (
                rule_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                notification_type TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                cooldown_minutes INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_rule_household_type
            ON notification_rule_preferences(household_id, notification_type)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_history (
                history_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                stable_key TEXT NOT NULL,
                candidate_id TEXT,
                notification_type TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                suppressed_at TEXT,
                suppression_reason TEXT,
                expires_at TEXT NOT NULL,
                evidence_summary TEXT NOT NULL,
                delivery_state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_history_household_key
            ON notification_history(household_id, stable_key, notification_type)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notification_history_household_created
            ON notification_history(household_id, created_at DESC)
            """
        )

    @staticmethod
    def _ensure_household_scoping(conn: sqlite3.Connection) -> None:
        scoped_tables = (
            "readings",
            "ev_sessions",
            "billing_periods",
            "timeline_entries",
            "import_jobs",
            "provider_observations",
            "historical_backfill_jobs",
            "promotion_decisions",
            "household_state_snapshots",
            "state_changes",
            "notification_rule_preferences",
            "notification_history",
            "synergy_bills",
            "synergy_bill_periods",
            "synergy_bill_line_items",
            "bill_reconciliations",
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_import_jobs_household_created
            ON import_jobs(household_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historical_backfill_jobs_household_updated
            ON historical_backfill_jobs(household_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_synergy_bills_household_issue
            ON synergy_bills(household_id, issue_date DESC)
            """
        )

    @staticmethod
    def _ensure_synergy_bill_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synergy_bills (
                bill_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                retailer TEXT NOT NULL,
                account_last3 TEXT,
                account_hash TEXT,
                invoice_number TEXT NOT NULL,
                issue_date TEXT,
                due_date TEXT,
                bill_total REAL,
                total_amount_owing REAL,
                opening_balance REAL,
                supply_address_hash TEXT,
                nmi_last3 TEXT,
                nmi_hash TEXT,
                next_scheduled_read_date TEXT,
                meter_number_hash TEXT,
                gst REAL,
                confidence TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                totals_json TEXT NOT NULL,
                raw_summary_json TEXT NOT NULL,
                import_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_synergy_bills_household_invoice
            ON synergy_bills(household_id, retailer, invoice_number)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synergy_bill_periods (
                period_id TEXT PRIMARY KEY,
                bill_id TEXT NOT NULL,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                plan_name TEXT,
                start_date TEXT,
                end_date TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_synergy_bill_periods_bill
            ON synergy_bill_periods(household_id, bill_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synergy_bill_line_items (
                line_item_id TEXT PRIMARY KEY,
                bill_id TEXT NOT NULL,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                category TEXT NOT NULL,
                bucket TEXT,
                description TEXT NOT NULL,
                plan_name TEXT,
                period_start TEXT,
                period_end TEXT,
                kwh REAL,
                days INTEGER,
                unit_rate REAL,
                amount REAL,
                raw_text TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_synergy_bill_line_items_bill
            ON synergy_bill_line_items(household_id, bill_id)
            """
        )

    @staticmethod
    def _ensure_bill_reconciliation_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bill_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL DEFAULT 'default_household',
                bill_id TEXT NOT NULL,
                retailer TEXT NOT NULL,
                invoice_number TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                actual_bill_total REAL,
                estimated_bill_total REAL,
                dollar_variance REAL,
                percentage_variance REAL,
                variance_category TEXT NOT NULL,
                confidence TEXT NOT NULL,
                explanation TEXT NOT NULL,
                components_json TEXT NOT NULL,
                data_gaps_json TEXT NOT NULL,
                tariff_mismatches_json TEXT NOT NULL,
                coverage_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_reconciliations_household_bill
            ON bill_reconciliations(household_id, bill_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_bill_reconciliations_household_updated
            ON bill_reconciliations(household_id, updated_at DESC)
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

    def has_usable_solar_readings(self) -> bool:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            row = conn.execute(
                """
                SELECT 1
                FROM readings
                WHERE household_id = ?
                  AND measurement_time IS NOT NULL
                  AND production_kwh IS NOT NULL
                  AND production_kwh > 0
                LIMIT 1
                """,
                (household_id,),
            ).fetchone()
            return row is not None

        return self._with_connection(query)

    def get_measurement_provider_registry(self, start: date, end: date) -> MeasurementProviderRegistry:
        """Return source-agnostic measurement providers for the selected period."""

        rows = self._reading_rows(start, end)
        return MeasurementProviderRegistry([SolarEdgeMeasurementProvider(rows)])

    def get_measurements(
        self,
        start: date,
        end: date,
        measurement_types: Optional[list[MeasurementType]] = None,
    ) -> list[UniversalMeasurement]:
        return self.get_measurement_provider_registry(start, end).measurements(start, end, measurement_types)

    def get_measurement_capabilities(self, start: date, end: date) -> dict[str, list[dict[str, Any]]]:
        return {
            provider_id: [capability.as_dict() for capability in capabilities]
            for provider_id, capabilities in self.get_measurement_provider_registry(start, end).capabilities().items()
        }

    def get_home_capabilities(self, profile, start: date, end: date):
        """Return home capability readiness from profile facts and measurements."""

        from .home_capabilities import detect_home_capabilities

        registry = self.get_measurement_provider_registry(start, end)
        return detect_home_capabilities(profile, registry)

    def _reading_rows(self, start: date, end: date) -> list[dict[str, Any]]:
        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()

        def query(conn):
            present = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}

            def column(name: str, default: str = "0") -> str:
                return name if name in present else f"{default} AS {name}"

            rows = conn.execute(
                f"""
                SELECT
                    measurement_time,
                    tariff_bucket,
                    {column("production_w")},
                    {column("consumption_w")},
                    {column("solar_to_home_w")},
                    {column("solar_to_grid_w")},
                    {column("grid_to_home_w")},
                    production_kwh,
                    {column("consumption_kwh")},
                    solar_to_home_kwh,
                    solar_to_grid_kwh,
                    grid_to_home_kwh
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
                    "production_w": float(row[2] or 0),
                    "consumption_w": float(row[3] or 0),
                    "solar_to_home_w": float(row[4] or 0),
                    "solar_to_grid_w": float(row[5] or 0),
                    "grid_to_home_w": float(row[6] or 0),
                    "production_kwh": float(row[7] or 0),
                    "consumption_kwh": float(row[8] or 0),
                    "solar_to_home_kwh": float(row[9] or 0),
                    "solar_to_grid_kwh": float(row[10] or 0),
                    "grid_to_home_kwh": float(row[11] or 0),
                }
                for row in rows
            ]

        return self._with_connection(query)

    def get_grid_intervals(self, start: date, end: date) -> list[dict[str, Any]]:
        measurements = self.get_measurements(start, end, [MeasurementType.GRID_IMPORT])
        return [
            {
                "measurement_time": measurement.timestamp,
                "tariff_bucket": measurement.attributes.get("tariff_bucket"),
                "grid_to_home_w": float(measurement.attributes.get("power_w") or 0),
                "grid_to_home_kwh": float(measurement.value or 0),
            }
            for measurement in measurements
        ]

    def get_solar_intervals(self, start: date, end: date) -> list[dict[str, Any]]:
        measurements = self.get_measurements(
            start,
            end,
            [MeasurementType.SOLAR_GENERATION, MeasurementType.GRID_EXPORT],
        )
        by_time: dict[str, dict[str, Any]] = {}
        for measurement in measurements:
            row = by_time.setdefault(
                measurement.timestamp,
                {
                    "measurement_time": measurement.timestamp,
                    "production_kwh": 0.0,
                    "solar_to_home_kwh": 0.0,
                    "solar_to_grid_kwh": 0.0,
                },
            )
            if measurement.measurement_type == MeasurementType.SOLAR_GENERATION:
                row["production_kwh"] = float(measurement.value or 0)
                row["solar_to_home_kwh"] = float(measurement.attributes.get("solar_used_kwh") or 0)
            elif measurement.measurement_type == MeasurementType.GRID_EXPORT:
                row["solar_to_grid_kwh"] = float(measurement.value or 0)
        return [by_time[key] for key in sorted(by_time)]

    def get_battery_interval_history(self, start: date, end: date) -> list[dict[str, Any]]:
        """Return interval-grade rows suitable for tariff-aware battery replay."""

        start_text = start.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            present = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
            provider_expr = "COALESCE(NULLIF(provider_id, ''), 'solaredge')" if "provider_id" in present else "'solaredge'"
            source_expr = "COALESCE(NULLIF(source_id, ''), 'solaredge_dashboard_power')" if "source_id" in present else "'solaredge_dashboard_power'"
            resolution_expr = "COALESCE(NULLIF(source_resolution, ''), 'interval_15m')" if "source_resolution" in present else "'interval_15m'"
            consumption_expr = "consumption_kwh" if "consumption_kwh" in present else "0"
            rows = conn.execute(
                f"""
                SELECT
                    measurement_time,
                    tariff_bucket,
                    grid_to_home_kwh,
                    solar_to_grid_kwh,
                    production_kwh,
                    solar_to_home_kwh,
                    {consumption_expr} AS consumption_kwh,
                    {provider_expr} AS provider_id,
                    {source_expr} AS source_id,
                    {resolution_expr} AS source_resolution
                FROM readings
                WHERE household_id = ?
                  AND measurement_time >= ? AND measurement_time < ?
                  AND measurement_time IS NOT NULL
                  AND {resolution_expr} IN ('interval_15m', 'interval_grade')
                ORDER BY measurement_time
                """,
                (household_id, start_text, end_exclusive),
            ).fetchall()
            return [
                {
                    "measurement_time": row[0],
                    "tariff_bucket": row[1],
                    "grid_import_kwh": float(row[2] or 0.0),
                    "solar_export_kwh": float(row[3] or 0.0),
                    "solar_generation_kwh": float(row[4] or 0.0),
                    "solar_to_home_kwh": float(row[5] or 0.0),
                    "household_consumption_kwh": float(row[6] or 0.0),
                    "provider_id": row[7],
                    "source_id": row[8],
                    "source_resolution": row[9],
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
        max_power_source = "provided" if row[9] is not None else "unavailable"
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
            "max_power_source": max_power_source,
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

    def get_household_behaviour(
        self,
        start: date,
        end: date,
        *,
        ev_sources: Optional[list[str]] = None,
    ) -> HouseholdBehaviour:
        ev_sources = ev_sources or ["ocular_export", "home_assistant_ocpp"]
        return BehaviourEngine().build(
            grid_intervals=self.get_grid_intervals(start, end),
            solar_intervals=self.get_solar_intervals(start, end),
            ev_sessions=self.get_ev_sessions(start, end, sources=ev_sources),
            household_id=self._active_household_id(),
        )

    def get_household_anomalies(
        self,
        *,
        household_state: Any,
        context: Any,
        state_changes: list[Any],
        behaviour: Any,
        current_observations: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        anomalies = AnomalyEngine().detect(
            household_state=household_state,
            context=context,
            state_changes=state_changes,
            behaviour=behaviour,
            current_observations=current_observations,
        )
        return [anomaly.as_dict() for anomaly in anomalies]

    def get_household_decisions(
        self,
        *,
        household_state: Any,
        context: Any,
        behaviour: Any = None,
        anomalies: Optional[list[Any]] = None,
        evidence_quality: Any = None,
        notification_readiness: Any = None,
        capability_summary: Any = None,
        financial_metadata: Optional[dict[str, Any]] = None,
        provider_health: Any = None,
        state_changes: Optional[list[Any]] = None,
        now: Any = None,
    ) -> dict[str, Any]:
        engine = DecisionEngine()
        decisions = engine.build(
            household_state=household_state,
            context=context,
            behaviour=behaviour,
            anomalies=anomalies or [],
            evidence_quality=evidence_quality,
            notification_readiness=notification_readiness,
            capability_summary=capability_summary,
            financial_metadata=financial_metadata or {},
            provider_health=provider_health,
            state_changes=state_changes or [],
            now=now,
        )
        return {
            "decisions": [decision.as_dict() for decision in decisions],
            "summary": engine.summary(decisions),
        }

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

    def create_import_job(
        self,
        source: ImportSource,
        filename: str,
        file_path: Path,
        preview: Optional[ImportPreview] = None,
        status: str = "previewed",
        message: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()

        def insert(conn):
            self.validate_connection(conn)
            job = ImportJob.create(household_id, source, filename, status=status)
            if job_id:
                job = ImportJob(
                    job_id=job_id,
                    household_id=job.household_id,
                    source=job.source,
                    filename=job.filename,
                    status=job.status,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    preview=preview,
                )
            conn.execute(
                """
                INSERT INTO import_jobs (
                    job_id,
                    household_id,
                    source_id,
                    source_name,
                    filename,
                    status,
                    file_path,
                    preview_json,
                    message,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    household_id,
                    source.source_id,
                    source.display_name,
                    job.filename,
                    status,
                    str(file_path),
                    json.dumps(preview.as_dict(), sort_keys=True) if preview else None,
                    message,
                    job.created_at,
                    job.updated_at,
                ),
            )
            conn.commit()
            return self.get_import_job(job.job_id)

        return self._with_connection(insert)

    def update_import_job(
        self,
        job_id: str,
        status: str,
        result: Optional[ImportResult] = None,
        message: Optional[str] = None,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()

        def update(conn):
            self.validate_connection(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                UPDATE import_jobs
                SET status = ?,
                    result_json = ?,
                    message = COALESCE(?, message),
                    updated_at = ?
                WHERE household_id = ? AND job_id = ?
                """,
                (
                    status,
                    json.dumps(result.as_dict(), sort_keys=True) if result else None,
                    message,
                    now,
                    household_id,
                    job_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError("Import job not found")
            conn.commit()
            return self.get_import_job(job_id)

        return self._with_connection(update)

    def create_historical_backfill_job(
        self,
        *,
        job_id: str,
        provider_id: str,
        site_id: str,
        range_preset: str,
        start_date: date,
        end_date: date,
        resolution: str,
        quality_tier: str,
        data_types: list[str],
        progress: Optional[dict[str, Any]] = None,
        diagnostics: Optional[dict[str, Any]] = None,
        warnings: Optional[list[str]] = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        household_id = self._active_household_id()

        def insert(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO historical_backfill_jobs (
                    job_id,
                    household_id,
                    provider_id,
                    site_id,
                    range_preset,
                    start_date,
                    end_date,
                    status,
                    resolution,
                    quality_tier,
                    data_types_json,
                    progress_json,
                    diagnostics_json,
                    warnings_json,
                    cancel_requested,
                    created_at,
                    updated_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
                """,
                (
                    job_id,
                    household_id,
                    provider_id,
                    site_id,
                    range_preset,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    status,
                    resolution,
                    quality_tier,
                    json.dumps(data_types, sort_keys=True),
                    json.dumps(progress or {}, sort_keys=True),
                    json.dumps(diagnostics or {}, sort_keys=True),
                    json.dumps(warnings or [], sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
            return self.get_historical_backfill_job(job_id)

        return self._with_connection(insert)

    def update_historical_backfill_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[dict[str, Any]] = None,
        diagnostics: Optional[dict[str, Any]] = None,
        warnings: Optional[list[str]] = None,
        completed: bool = False,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()

        def update(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            existing = self._historical_backfill_row(conn, household_id, job_id)
            if existing is None:
                raise RuntimeError("Historical data job not found")
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE historical_backfill_jobs
                SET status = ?,
                    progress_json = ?,
                    diagnostics_json = ?,
                    warnings_json = ?,
                    updated_at = ?,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE household_id = ? AND job_id = ?
                """,
                (
                    status or existing["status"],
                    json.dumps(progress if progress is not None else existing["progress"], sort_keys=True),
                    json.dumps(diagnostics if diagnostics is not None else existing["diagnostics"], sort_keys=True),
                    json.dumps(warnings if warnings is not None else existing["warnings"], sort_keys=True),
                    now,
                    1 if completed else 0,
                    now,
                    household_id,
                    job_id,
                ),
            )
            conn.commit()
            return self.get_historical_backfill_job(job_id)

        return self._with_connection(update)

    def request_historical_backfill_cancel(self, job_id: str) -> dict[str, Any]:
        household_id = self._active_household_id()

        def update(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                UPDATE historical_backfill_jobs
                SET cancel_requested = 1,
                    status = CASE WHEN status IN ('complete', 'failed', 'cancelled') THEN status ELSE 'cancelling' END,
                    updated_at = ?
                WHERE household_id = ? AND job_id = ?
                """,
                (now, household_id, job_id),
            )
            if cursor.rowcount == 0:
                raise RuntimeError("Historical data job not found")
            conn.commit()
            return self.get_historical_backfill_job(job_id)

        return self._with_connection(update)

    def get_historical_backfill_job(self, job_id: str) -> Optional[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            return self._historical_backfill_row(conn, household_id, job_id)

        return self._with_connection(query)

    def get_historical_backfill_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM historical_backfill_jobs
                WHERE household_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            columns = [column[0] for column in conn.execute("SELECT * FROM historical_backfill_jobs LIMIT 0").description]
            return [self._historical_backfill_dict(dict(zip(columns, row))) for row in rows]

        return self._with_connection(query)

    def get_historical_coverage(self) -> dict[str, Any]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_historical_backfill_tables(conn)
            rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(provider_id, ''), 'solaredge') AS provider_id,
                    COALESCE(NULLIF(source_id, ''), 'solaredge_dashboard_power') AS source_id,
                    COALESCE(NULLIF(source_resolution, ''), 'interval_15m') AS source_resolution,
                    substr(measurement_time, 1, 10) AS reading_date,
                    MIN(measurement_time) AS first_reading,
                    MAX(measurement_time) AS last_reading,
                    COUNT(*) AS interval_count,
                    SUM(CASE WHEN COALESCE(production_kwh, 0) != 0 THEN 1 ELSE 0 END) AS solar_generation_count,
                    SUM(CASE WHEN COALESCE(consumption_kwh, 0) != 0 THEN 1 ELSE 0 END) AS household_consumption_count,
                    SUM(CASE WHEN COALESCE(grid_to_home_kwh, 0) != 0 THEN 1 ELSE 0 END) AS grid_import_count,
                    SUM(CASE WHEN COALESCE(solar_to_grid_kwh, 0) != 0 THEN 1 ELSE 0 END) AS grid_export_count
                FROM readings
                WHERE household_id = ?
                  AND measurement_time IS NOT NULL
                GROUP BY provider_id, source_id, source_resolution, reading_date
                ORDER BY reading_date
                """,
                (household_id,),
            ).fetchall()
            providers: dict[str, Any] = {}
            measurement_totals = {
                "solar_generation": 0,
                "household_consumption": 0,
                "grid_import": 0,
                "grid_export": 0,
            }
            for row in rows:
                provider_id, source_id, resolution, reading_date, first, last, intervals, solar, consumption, grid_import, grid_export = row
                provider = providers.setdefault(
                    provider_id,
                    {
                        "provider_id": provider_id,
                        "sources": {},
                        "days": {},
                        "first_reading": None,
                        "last_reading": None,
                        "quality_tiers": [],
                    },
                )
                source = provider["sources"].setdefault(
                    source_id,
                    {
                        "source_id": source_id,
                        "resolution": resolution,
                        "quality_tier": _quality_tier_for_resolution(resolution),
                        "days": 0,
                        "intervals": 0,
                        "first_reading": None,
                        "last_reading": None,
                    },
                )
                counts = {
                    "solar_generation": int(solar or 0),
                    "household_consumption": int(consumption or 0),
                    "grid_import": int(grid_import or 0),
                    "grid_export": int(grid_export or 0),
                }
                battery_suitable = counts["grid_import"] > 0 and counts["grid_export"] > 0
                provider["days"][reading_date] = {
                    "first": first,
                    "last": last,
                    "intervals": int(intervals or 0),
                    "resolution": resolution,
                    "quality_tier": _quality_tier_for_resolution(resolution),
                    "measurement_counts": counts,
                    "battery_suitable": battery_suitable,
                }
                source["days"] += 1
                source["intervals"] += int(intervals or 0)
                source["first_reading"] = min(filter(None, [source["first_reading"], first]), default=first)
                source["last_reading"] = max(filter(None, [source["last_reading"], last]), default=last)
                provider["first_reading"] = min(filter(None, [provider["first_reading"], first]), default=first)
                provider["last_reading"] = max(filter(None, [provider["last_reading"], last]), default=last)
                tier = _quality_tier_for_resolution(resolution)
                if tier not in provider["quality_tiers"]:
                    provider["quality_tiers"].append(tier)
                for key, value in counts.items():
                    measurement_totals[key] += value
            jobs = self.get_historical_backfill_jobs(limit=10)
            return {
                "providers": providers,
                "measurement_totals": measurement_totals,
                "historical_backfill_jobs": jobs,
                "quality_tiers": {
                    "interval_grade": "Can support tariff-aware analysis and battery dispatch when import/export flows are present.",
                    "daily_aggregate": "Useful for seasonal context, not for replaying battery dispatch.",
                    "monthly_lifetime_aggregate": "Useful for long-term context only, not for tariff-aware battery dispatch.",
                },
            }

        return self._with_connection(query)

    def _historical_backfill_row(self, conn: sqlite3.Connection, household_id: str, job_id: str) -> Optional[dict[str, Any]]:
        cursor = conn.execute(
            """
            SELECT *
            FROM historical_backfill_jobs
            WHERE household_id = ? AND job_id = ?
            """,
            (household_id, job_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [column[0] for column in cursor.description]
        return self._historical_backfill_dict(dict(zip(columns, row)))

    @staticmethod
    def _historical_backfill_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": row.get("job_id"),
            "household_id": row.get("household_id"),
            "provider_id": row.get("provider_id"),
            "site_id": row.get("site_id"),
            "range_preset": row.get("range_preset"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
            "status": row.get("status"),
            "resolution": row.get("resolution"),
            "quality_tier": row.get("quality_tier"),
            "data_types": _json_value(row.get("data_types_json"), []),
            "progress": _json_value(row.get("progress_json"), {}),
            "diagnostics": _json_value(row.get("diagnostics_json"), {}),
            "warnings": _json_value(row.get("warnings_json"), []),
            "cancel_requested": bool(row.get("cancel_requested")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "completed_at": row.get("completed_at"),
        }

    def import_synergy_bill(
        self,
        bill: dict[str, Any],
        import_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()
        retailer = str(bill.get("retailer") or "Synergy")
        invoice_number = str(bill.get("invoice_number") or "").strip()
        if not invoice_number:
            raise ValueError("Synergy bill invoice number is required")
        bill_id = hashlib.sha256(f"{household_id}:{retailer}:{invoice_number}".encode("utf-8")).hexdigest()
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        def last3(value: Any) -> Optional[str]:
            digits = re.sub(r"\D+", "", str(value or ""))
            return digits[-3:] if digits else None

        def value_hash(value: Any) -> Optional[str]:
            normalized = re.sub(r"\s+", "", str(value or ""))
            if not normalized:
                return None
            return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        def insert(conn):
            self.validate_connection(conn)
            DataEngine._ensure_synergy_bill_tables(conn)
            DataEngine._ensure_bill_reconciliation_table(conn)
            existing = conn.execute(
                """
                SELECT bill_id
                FROM synergy_bills
                WHERE household_id = ? AND retailer = ? AND invoice_number = ?
                """,
                (household_id, retailer, invoice_number),
            ).fetchone()
            if existing:
                return {"status": "duplicate", "bill_id": existing[0], "invoice_number": invoice_number}
            redacted_summary = self._redact_synergy_bill_dict(bill)
            conn.execute(
                """
                INSERT INTO synergy_bills (
                    bill_id,
                    household_id,
                    retailer,
                    account_last3,
                    account_hash,
                    invoice_number,
                    issue_date,
                    due_date,
                    bill_total,
                    total_amount_owing,
                    opening_balance,
                    supply_address_hash,
                    nmi_last3,
                    nmi_hash,
                    next_scheduled_read_date,
                    meter_number_hash,
                    gst,
                    confidence,
                    warnings_json,
                    totals_json,
                    raw_summary_json,
                    import_job_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bill_id,
                    household_id,
                    retailer,
                    last3(bill.get("account_number")),
                    value_hash(bill.get("account_number")),
                    invoice_number,
                    bill.get("issue_date"),
                    bill.get("due_date"),
                    bill.get("bill_total"),
                    bill.get("total_amount_owing"),
                    bill.get("opening_balance"),
                    value_hash(bill.get("supply_address")),
                    last3(bill.get("nmi")),
                    value_hash(bill.get("nmi")),
                    bill.get("next_scheduled_read_date"),
                    value_hash(bill.get("meter_number")),
                    bill.get("gst"),
                    str(bill.get("confidence") or "medium"),
                    json.dumps(bill.get("warnings") or [], sort_keys=True),
                    json.dumps(bill.get("totals") or {}, sort_keys=True),
                    json.dumps(redacted_summary, sort_keys=True),
                    import_job_id,
                    now,
                    now,
                ),
            )
            for index, period in enumerate(bill.get("periods") or []):
                period_id = hashlib.sha256(f"{bill_id}:period:{index}".encode("utf-8")).hexdigest()
                conn.execute(
                    """
                    INSERT INTO synergy_bill_periods (
                        period_id, bill_id, household_id, plan_name, start_date, end_date, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        period_id,
                        bill_id,
                        household_id,
                        period.get("plan_name"),
                        period.get("start_date"),
                        period.get("end_date"),
                        now,
                    ),
                )
            for index, item in enumerate(bill.get("line_items") or []):
                line_item_id = hashlib.sha256(f"{bill_id}:line:{index}".encode("utf-8")).hexdigest()
                conn.execute(
                    """
                    INSERT INTO synergy_bill_line_items (
                        line_item_id,
                        bill_id,
                        household_id,
                        category,
                        bucket,
                        description,
                        plan_name,
                        period_start,
                        period_end,
                        kwh,
                        days,
                        unit_rate,
                        amount,
                        raw_text,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        line_item_id,
                        bill_id,
                        household_id,
                        item.get("category"),
                        item.get("bucket"),
                        item.get("description"),
                        item.get("plan_name"),
                        item.get("period_start"),
                        item.get("period_end"),
                        item.get("kwh"),
                        item.get("days"),
                        item.get("unit_rate"),
                        item.get("amount"),
                        item.get("raw_text"),
                        now,
                    ),
                )
            conn.commit()
            return {"status": "inserted", "bill_id": bill_id, "invoice_number": invoice_number}

        return self._with_connection(insert)

    @staticmethod
    def _redact_synergy_bill_dict(bill: dict[str, Any]) -> dict[str, Any]:
        def masked(value: Any) -> Optional[str]:
            digits = re.sub(r"\D+", "", str(value or ""))
            return f"•••{digits[-3:]}" if digits else None

        redacted = dict(bill)
        redacted["account_number"] = masked(bill.get("account_number"))
        redacted["nmi"] = masked(bill.get("nmi"))
        redacted["supply_address"] = None
        redacted["meter_number"] = None
        return redacted

    def get_latest_synergy_bill(self) -> Optional[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_synergy_bill_tables(conn)
            row = conn.execute(
                """
                SELECT
                    bill_id,
                    retailer,
                    account_last3,
                    invoice_number,
                    issue_date,
                    due_date,
                    bill_total,
                    total_amount_owing,
                    opening_balance,
                    nmi_last3,
                    next_scheduled_read_date,
                    gst,
                    confidence,
                    warnings_json,
                    totals_json,
                    import_job_id,
                    created_at
                FROM synergy_bills
                WHERE household_id = ?
                ORDER BY COALESCE(issue_date, created_at) DESC, created_at DESC
                LIMIT 1
                """,
                (household_id,),
            ).fetchone()
            if row is None:
                return None
            bill_id = row[0]
            periods = conn.execute(
                """
                SELECT plan_name, start_date, end_date
                FROM synergy_bill_periods
                WHERE household_id = ? AND bill_id = ?
                ORDER BY start_date
                """,
                (household_id, bill_id),
            ).fetchall()
            items = conn.execute(
                """
                SELECT category, bucket, description, plan_name, period_start, period_end, kwh, days, unit_rate, amount
                FROM synergy_bill_line_items
                WHERE household_id = ? AND bill_id = ?
                ORDER BY rowid
                """,
                (household_id, bill_id),
            ).fetchall()
            return {
                "bill_id": bill_id,
                "retailer": row[1],
                "account_number": f"•••{row[2]}" if row[2] else None,
                "invoice_number": row[3],
                "issue_date": row[4],
                "due_date": row[5],
                "bill_total": row[6],
                "total_amount_owing": row[7],
                "opening_balance": row[8],
                "nmi": f"•••{row[9]}" if row[9] else None,
                "next_scheduled_read_date": row[10],
                "gst": row[11],
                "confidence": row[12],
                "warnings": json.loads(row[13] or "[]"),
                "totals": json.loads(row[14] or "{}"),
                "import_job_id": row[15],
                "created_at": row[16],
                "periods": [
                    {"plan_name": period[0], "start_date": period[1], "end_date": period[2]}
                    for period in periods
                ],
                "line_items": [
                    {
                        "category": item[0],
                        "bucket": item[1],
                        "description": item[2],
                        "plan_name": item[3],
                        "period_start": item[4],
                        "period_end": item[5],
                        "kwh": item[6],
                        "days": item[7],
                        "unit_rate": item[8],
                        "amount": item[9],
                    }
                    for item in items
                ],
            }

        return self._with_connection(query)

    def compare_projected_to_latest_synergy_bill(self, projected_bill: Optional[float]) -> Optional[dict[str, Any]]:
        latest_reconciliation = self.get_latest_bill_reconciliation()
        if latest_reconciliation:
            return latest_reconciliation
        bill = self.get_latest_synergy_bill()
        if not bill:
            return None
        actual = bill.get("bill_total")
        if actual is None or projected_bill is None:
            difference = None
        else:
            difference = float(projected_bill) - float(actual)
        return {
            "latest_bill": bill,
            "projected_bill": projected_bill,
            "actual_bill": actual,
            "difference": difference,
            "status": (
                "projection_higher" if difference is not None and difference > 0.01 else
                "projection_lower" if difference is not None and difference < -0.01 else
                "matched" if difference is not None else
                "comparison_unavailable"
            ),
        }

    def persist_bill_reconciliation(self, reconciliation: BillReconciliation) -> dict[str, Any]:
        household_id = self._active_household_id()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        data = reconciliation.as_dict()
        bill_id = data.get("bill_id")
        if not bill_id:
            raise ValueError("Bill reconciliation requires a bill_id")
        reconciliation_id = hashlib.sha256(f"{household_id}:{bill_id}:reconciliation".encode("utf-8")).hexdigest()
        summary = self._redact_bill_reconciliation_dict(data)

        def upsert(conn):
            self.validate_connection(conn)
            DataEngine._ensure_bill_reconciliation_table(conn)
            conn.execute(
                """
                INSERT INTO bill_reconciliations (
                    reconciliation_id,
                    household_id,
                    bill_id,
                    retailer,
                    invoice_number,
                    period_start,
                    period_end,
                    actual_bill_total,
                    estimated_bill_total,
                    dollar_variance,
                    percentage_variance,
                    variance_category,
                    confidence,
                    explanation,
                    components_json,
                    data_gaps_json,
                    tariff_mismatches_json,
                    coverage_json,
                    summary_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(household_id, bill_id) DO UPDATE SET
                    retailer = excluded.retailer,
                    invoice_number = excluded.invoice_number,
                    period_start = excluded.period_start,
                    period_end = excluded.period_end,
                    actual_bill_total = excluded.actual_bill_total,
                    estimated_bill_total = excluded.estimated_bill_total,
                    dollar_variance = excluded.dollar_variance,
                    percentage_variance = excluded.percentage_variance,
                    variance_category = excluded.variance_category,
                    confidence = excluded.confidence,
                    explanation = excluded.explanation,
                    components_json = excluded.components_json,
                    data_gaps_json = excluded.data_gaps_json,
                    tariff_mismatches_json = excluded.tariff_mismatches_json,
                    coverage_json = excluded.coverage_json,
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                (
                    reconciliation_id,
                    household_id,
                    bill_id,
                    data.get("retailer") or "Synergy",
                    data.get("invoice_number") or "",
                    data.get("period_start"),
                    data.get("period_end"),
                    data.get("actual_bill_total"),
                    data.get("estimated_bill_total"),
                    data.get("dollar_variance"),
                    data.get("percentage_variance"),
                    data.get("variance_category") or "unreliable",
                    data.get("confidence") or "low",
                    data.get("explanation") or "",
                    json.dumps(data.get("components") or [], sort_keys=True),
                    json.dumps(data.get("data_gaps") or [], sort_keys=True),
                    json.dumps(data.get("tariff_mismatches") or [], sort_keys=True),
                    json.dumps(data.get("coverage") or {}, sort_keys=True),
                    json.dumps(summary, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
            return {"status": "saved", "reconciliation_id": reconciliation_id, "bill_id": bill_id}

        return self._with_connection(upsert)

    @staticmethod
    def _redact_bill_reconciliation_dict(reconciliation: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "bill_id",
            "invoice_number",
            "retailer",
            "period_start",
            "period_end",
            "actual_bill_total",
            "estimated_bill_total",
            "dollar_variance",
            "percentage_variance",
            "variance_category",
            "confidence",
            "explanation",
            "components",
            "data_gaps",
            "tariff_mismatches",
            "coverage",
        }
        return {key: reconciliation.get(key) for key in allowed}

    def get_latest_bill_reconciliation(self) -> Optional[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_bill_reconciliation_table(conn)
            row = conn.execute(
                """
                SELECT
                    reconciliation_id,
                    bill_id,
                    retailer,
                    invoice_number,
                    period_start,
                    period_end,
                    actual_bill_total,
                    estimated_bill_total,
                    dollar_variance,
                    percentage_variance,
                    variance_category,
                    confidence,
                    explanation,
                    components_json,
                    data_gaps_json,
                    tariff_mismatches_json,
                    coverage_json,
                    updated_at
                FROM bill_reconciliations
                WHERE household_id = ?
                ORDER BY COALESCE(period_end, updated_at) DESC, updated_at DESC
                LIMIT 1
                """,
                (household_id,),
            ).fetchone()
            if row is None:
                return None
            bill = self.get_latest_synergy_bill()
            result = {
                "reconciliation_id": row[0],
                "bill_id": row[1],
                "retailer": row[2],
                "invoice_number": row[3],
                "period_start": row[4],
                "period_end": row[5],
                "actual_bill_total": row[6],
                "estimated_bill_total": row[7],
                "dollar_variance": row[8],
                "percentage_variance": row[9],
                "variance_category": row[10],
                "confidence": row[11],
                "explanation": row[12],
                "components": json.loads(row[13] or "[]"),
                "data_gaps": json.loads(row[14] or "[]"),
                "tariff_mismatches": json.loads(row[15] or "[]"),
                "coverage": json.loads(row[16] or "{}"),
                "updated_at": row[17],
            }
            result["latest_bill"] = bill if bill and bill.get("bill_id") == row[1] else None
            result["projected_bill"] = row[7]
            result["actual_bill"] = row[6]
            result["difference"] = row[8]
            result["status"] = row[10]
            return result

        return self._with_connection(query)

    def get_bill_reconciliation_summary(self, limit: int = 6) -> dict[str, Any]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            DataEngine._ensure_bill_reconciliation_table(conn)
            rows = conn.execute(
                """
                SELECT invoice_number, period_start, period_end, actual_bill_total, estimated_bill_total,
                       dollar_variance, variance_category, confidence, updated_at
                FROM bill_reconciliations
                WHERE household_id = ?
                ORDER BY COALESCE(period_end, updated_at) DESC, updated_at DESC
                LIMIT ?
                """,
                (household_id, limit),
            ).fetchall()
            history = [
                {
                    "invoice_number": row[0],
                    "period_start": row[1],
                    "period_end": row[2],
                    "actual_bill_total": row[3],
                    "estimated_bill_total": row[4],
                    "dollar_variance": row[5],
                    "variance_category": row[6],
                    "confidence": row[7],
                    "updated_at": row[8],
                }
                for row in rows
            ]
            variances = [abs(float(item["dollar_variance"])) for item in history if item.get("dollar_variance") is not None]
            return {
                "latest": self.get_latest_bill_reconciliation(),
                "history": history,
                "average_absolute_variance": sum(variances) / len(variances) if variances else None,
            }

        return self._with_connection(query)

    def persist_provider_observations(
        self,
        observations: list[ProviderObservation],
    ) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def insert(conn):
            DataEngine._ensure_provider_observation_table(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            rows = []
            for observation in observations:
                provider_id = observation.provider_id or "unknown_provider"
                provider_type = observation.provider_type or ProviderType.GENERIC_IMPORT
                capability = observation.capability
                observation_type = observation.observation_type or _observation_type_for_capability(capability)
                observed_at = observation.observed_at or now
                received_at = observation.received_at or now
                scoped_household_id = observation.household_id or household_id
                if scoped_household_id != household_id:
                    continue
                value_json = json.dumps(observation.value, sort_keys=True) if observation.value is not None else None
                freshness_json = json.dumps(observation.freshness.as_dict(), sort_keys=True) if observation.freshness else None
                key = "|".join(
                    [
                        scoped_household_id,
                        provider_id,
                        capability.value,
                        observation_type.value,
                        observed_at,
                        str(observation.raw_reference or ""),
                        str(observation.import_job_id or ""),
                    ]
                )
                observation_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
                row = (
                    observation_id,
                    scoped_household_id,
                    provider_id,
                    provider_type.value,
                    capability.value,
                    observation_type.value,
                    observed_at,
                    received_at,
                    value_json,
                    observation.unit,
                    observation.quality.value,
                    freshness_json,
                    observation.provenance,
                    observation.raw_reference,
                    observation.import_job_id,
                    now,
                )
                conn.execute(
                    """
                    INSERT INTO provider_observations (
                        observation_id,
                        household_id,
                        provider_id,
                        provider_type,
                        capability,
                        observation_type,
                        observed_at,
                        received_at,
                        value_json,
                        unit,
                        quality,
                        freshness_json,
                        provenance,
                        raw_reference,
                        import_job_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(observation_id) DO UPDATE SET
                        received_at = excluded.received_at,
                        value_json = excluded.value_json,
                        unit = excluded.unit,
                        quality = excluded.quality,
                        freshness_json = excluded.freshness_json,
                        provenance = excluded.provenance,
                        raw_reference = excluded.raw_reference,
                        import_job_id = excluded.import_job_id
                    """,
                    row,
                )
                rows.append(row)
            conn.commit()
            return [
                self._normalise_provider_observation(row)
                for row in rows
            ]

        return self._with_connection(insert)

    def get_provider_observations(
        self,
        *,
        limit: int = 20,
        provider_id: Optional[str] = None,
        capability: Optional[str] = None,
        observation_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_provider_observation_table(conn)
            clauses = ["household_id = ?"]
            values: list[Any] = [household_id]
            if provider_id:
                clauses.append("provider_id = ?")
                values.append(provider_id)
            if capability:
                clauses.append("capability = ?")
                values.append(capability)
            if observation_type:
                clauses.append("observation_type = ?")
                values.append(observation_type)
            rows = conn.execute(
                f"""
                SELECT
                    observation_id,
                    household_id,
                    provider_id,
                    provider_type,
                    capability,
                    observation_type,
                    observed_at,
                    received_at,
                    value_json,
                    unit,
                    quality,
                    freshness_json,
                    provenance,
                    raw_reference,
                    import_job_id,
                    created_at
                FROM provider_observations
                WHERE {' AND '.join(clauses)}
                ORDER BY observed_at DESC, received_at DESC
                LIMIT ?
                """,
                (*values, int(limit)),
            ).fetchall()
            return [self._normalise_provider_observation(row) for row in rows]

        return self._with_connection(query)

    def get_provider_observation_summary(self, limit: int = 20) -> dict[str, Any]:
        observations = self.get_provider_observations(limit=limit)
        by_provider: dict[str, dict[str, Any]] = {}
        for observation in observations:
            provider_id = observation["provider_id"]
            item = by_provider.setdefault(
                provider_id,
                {
                    "provider_id": provider_id,
                    "provider_type": observation["provider_type"],
                    "latest_observed_at": observation["observed_at"],
                    "capabilities": set(),
                    "observation_types": set(),
                    "quality": observation["quality"],
                    "count": 0,
                },
            )
            item["count"] += 1
            item["capabilities"].add(observation["capability"])
            item["observation_types"].add(observation["observation_type"])
            if observation["observed_at"] > item["latest_observed_at"]:
                item["latest_observed_at"] = observation["observed_at"]
                item["quality"] = observation["quality"]
        providers = []
        for item in by_provider.values():
            providers.append(
                {
                    **item,
                    "capabilities": sorted(item["capabilities"]),
                    "observation_types": sorted(item["observation_types"]),
                }
            )
        providers.sort(key=lambda item: item["latest_observed_at"], reverse=True)
        return {
            "count": len(observations),
            "providers": providers,
            "latest": observations[: min(limit, 10)],
        }

    def persist_household_state_snapshot(
        self,
        household_state: Any,
        *,
        context: Optional[Any] = None,
        captured_at: Optional[str] = None,
    ) -> dict[str, Any]:
        household_id = self._active_household_id()
        state = self._redact_household_state_snapshot_payload(
            household_state.as_dict() if hasattr(household_state, "as_dict") else dict(household_state)
        )
        captured = captured_at or state.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds")
        context_summary = self._redact_household_state_snapshot_payload(
            context.as_dict() if hasattr(context, "as_dict") else context
        )
        material_hash = self._household_state_material_hash(state, context_summary)
        snapshot_id = hashlib.sha256(f"{household_id}|{captured}|{material_hash}".encode("utf-8")).hexdigest()[:32]
        provider_provenance = {
            "trusted_provider_data": state.get("evidence", {}).get("trusted_provider_data", []),
            "data_sources_used": state.get("evidence", {}).get("data_sources_used", []),
        }

        def insert(conn):
            DataEngine._ensure_household_state_snapshot_table(conn)
            latest = self._latest_household_state_snapshot_row(conn, household_id)
            if latest and latest[3] == material_hash:
                snapshot = self._normalise_household_state_snapshot(latest)
                snapshot["persisted"] = False
                snapshot["duplicate_suppressed"] = True
                return snapshot
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            row = (
                snapshot_id,
                household_id,
                captured,
                material_hash,
                json.dumps(state.get("tariff", {}), sort_keys=True),
                json.dumps(state.get("power", {}), sort_keys=True),
                json.dumps(state.get("energy", {}), sort_keys=True),
                json.dumps(state.get("solar", {}), sort_keys=True),
                json.dumps(state.get("ev", {}), sort_keys=True),
                json.dumps(state.get("battery", {}), sort_keys=True),
                json.dumps(state.get("billing", {}), sort_keys=True),
                json.dumps(state.get("collector", {}), sort_keys=True),
                json.dumps(state.get("evidence", {}), sort_keys=True),
                json.dumps(provider_provenance, sort_keys=True),
                json.dumps(context_summary, sort_keys=True) if context_summary else None,
                json.dumps(state, sort_keys=True),
                now,
            )
            conn.execute(
                """
                INSERT INTO household_state_snapshots (
                    snapshot_id,
                    household_id,
                    captured_at,
                    material_hash,
                    tariff_state_json,
                    power_state_json,
                    energy_state_json,
                    solar_state_json,
                    ev_state_json,
                    battery_state_json,
                    billing_state_json,
                    collector_state_json,
                    evidence_state_json,
                    provider_provenance_json,
                    context_summary_json,
                    state_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
            snapshot = self._normalise_household_state_snapshot(row)
            snapshot["persisted"] = True
            snapshot["duplicate_suppressed"] = False
            return snapshot

        return self._with_connection(insert)

    def get_latest_household_state_snapshot(self) -> Optional[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_household_state_snapshot_table(conn)
            row = self._latest_household_state_snapshot_row(conn, household_id)
            return self._normalise_household_state_snapshot(row) if row else None

        return self._with_connection(query)

    def get_recent_household_state_snapshots(self, limit: int = 10) -> dict[str, Any]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_household_state_snapshot_table(conn)
            rows = conn.execute(
                """
                SELECT
                    snapshot_id,
                    household_id,
                    captured_at,
                    material_hash,
                    tariff_state_json,
                    power_state_json,
                    energy_state_json,
                    solar_state_json,
                    ev_state_json,
                    battery_state_json,
                    billing_state_json,
                    collector_state_json,
                    evidence_state_json,
                    provider_provenance_json,
                    context_summary_json,
                    state_json,
                    created_at
                FROM household_state_snapshots
                WHERE household_id = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            snapshots = [self._normalise_household_state_snapshot(row) for row in rows]
            return {
                "household_id": household_id,
                "count": len(snapshots),
                "latest_captured_at": snapshots[0]["captured_at"] if snapshots else None,
                "snapshots": snapshots,
                "changes": self._household_state_changes(snapshots),
            }

        return self._with_connection(query)

    def detect_and_persist_state_changes(self, limit: int = 2) -> dict[str, Any]:
        recent = self.get_recent_household_state_snapshots(limit=max(limit, 2))
        snapshots = recent.get("snapshots", [])
        if len(snapshots) < 2:
            return {
                "household_id": self._active_household_id(),
                "count": 0,
                "changes": [],
            }
        current = snapshots[0]
        previous = snapshots[1]
        result = StateChangeEngine().detect(previous, current)
        changes = self.persist_state_changes(result.changes)
        return {
            "household_id": result.household_id,
            "previous_snapshot_id": result.previous_snapshot_id,
            "current_snapshot_id": result.current_snapshot_id,
            "count": len(changes),
            "changes": changes,
        }

    def persist_state_changes(self, changes: list[StateChange]) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def upsert(conn):
            DataEngine._ensure_state_change_table(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            rows = []
            for change in changes:
                if change.household_id != household_id:
                    continue
                evidence_json = json.dumps([item.as_dict() for item in change.evidence], sort_keys=True)
                row = (
                    change.change_id,
                    household_id,
                    change.event_key or change.change_id,
                    change.change_type.value,
                    change.severity.value,
                    change.title,
                    change.summary,
                    change.detected_at,
                    change.previous_snapshot_id,
                    change.current_snapshot_id,
                    evidence_json,
                    now,
                )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO state_changes (
                        change_id,
                        household_id,
                        event_key,
                        change_type,
                        severity,
                        title,
                        summary,
                        detected_at,
                        previous_snapshot_id,
                        current_snapshot_id,
                        evidence_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                if cursor.rowcount:
                    rows.append(row)
            conn.commit()
            return [self._normalise_state_change(row) for row in rows]

        return self._with_connection(upsert)

    def get_recent_state_changes(self, limit: int = 20) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_state_change_table(conn)
            rows = conn.execute(
                """
                SELECT
                    change_id,
                    household_id,
                    event_key,
                    change_type,
                    severity,
                    title,
                    summary,
                    detected_at,
                    previous_snapshot_id,
                    current_snapshot_id,
                    evidence_json,
                    created_at
                FROM state_changes
                WHERE household_id = ?
                ORDER BY detected_at DESC, created_at DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            return [self._normalise_state_change(row) for row in rows]

        return self._with_connection(query)

    def get_state_change_summary(self, limit: int = 20) -> dict[str, Any]:
        changes = self.get_recent_state_changes(limit=limit)
        counts: dict[str, int] = {}
        for change in changes:
            counts[change["change_type"]] = counts.get(change["change_type"], 0) + 1
        return {
            "household_id": self._active_household_id(),
            "count": len(changes),
            "counts": counts,
            "latest": changes[: min(limit, 10)],
        }

    def get_notification_preferences(self) -> NotificationPreferences:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_notification_tables(conn)
            row = conn.execute(
                """
                SELECT
                    household_id,
                    enabled,
                    priority_threshold,
                    quiet_hours_enabled,
                    quiet_hours_start,
                    quiet_hours_end,
                    quiet_hours_timezone,
                    channel_preference,
                    cooldown_minutes,
                    updated_at
                FROM notification_preferences
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            rules = conn.execute(
                """
                SELECT notification_type, enabled, cooldown_minutes
                FROM notification_rule_preferences
                WHERE household_id = ?
                ORDER BY notification_type
                """,
                (household_id,),
            ).fetchall()
            return self._normalise_notification_preferences(row, rules, household_id)

        return self._with_connection(query)

    def persist_notification_preferences(self, preferences: Any) -> dict[str, Any]:
        household_id = self._active_household_id()
        prefs = self._coerce_notification_preferences(preferences, household_id)

        def upsert(conn):
            DataEngine._ensure_notification_tables(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO notification_preferences (
                    household_id,
                    enabled,
                    priority_threshold,
                    quiet_hours_enabled,
                    quiet_hours_start,
                    quiet_hours_end,
                    quiet_hours_timezone,
                    channel_preference,
                    cooldown_minutes,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(household_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    priority_threshold = excluded.priority_threshold,
                    quiet_hours_enabled = excluded.quiet_hours_enabled,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    quiet_hours_timezone = excluded.quiet_hours_timezone,
                    channel_preference = excluded.channel_preference,
                    cooldown_minutes = excluded.cooldown_minutes,
                    updated_at = excluded.updated_at
                """,
                (
                    household_id,
                    1 if prefs.enabled else 0,
                    prefs.priority_threshold.value,
                    1 if prefs.quiet_hours.enabled else 0,
                    prefs.quiet_hours.start,
                    prefs.quiet_hours.end,
                    prefs.quiet_hours.timezone,
                    prefs.channel_preference.value,
                    int(prefs.cooldown_minutes),
                    now,
                ),
            )
            for rule in prefs.rule_preferences:
                rule_id = hashlib.sha256(f"{household_id}|{rule.notification_type.value}".encode("utf-8")).hexdigest()[:32]
                conn.execute(
                    """
                    INSERT INTO notification_rule_preferences (
                        rule_id,
                        household_id,
                        notification_type,
                        enabled,
                        cooldown_minutes,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(household_id, notification_type) DO UPDATE SET
                        enabled = excluded.enabled,
                        cooldown_minutes = excluded.cooldown_minutes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        rule_id,
                        household_id,
                        rule.notification_type.value,
                        1 if rule.enabled else 0,
                        rule.cooldown_minutes,
                        now,
                    ),
                )
            conn.commit()
            return self.get_notification_preferences().as_dict()

        return self._with_connection(upsert)

    def persist_notification_history(
        self,
        candidates: list[Any],
        *,
        delivery_state: Optional[NotificationDeliveryState] = None,
    ) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def upsert(conn):
            DataEngine._ensure_notification_tables(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            rows = []
            for candidate in candidates:
                item = candidate.as_dict() if hasattr(candidate, "as_dict") else dict(candidate)
                if item.get("household_id") != household_id:
                    continue
                notification_type = str(item.get("type") or item.get("notification_type"))
                stable_key = str(item.get("source_event_key") or item.get("candidate_id") or notification_type)
                state = delivery_state or (
                    NotificationDeliveryState.PENDING if item.get("ready") else NotificationDeliveryState.SUPPRESSED
                )
                suppressed_at = now if state == NotificationDeliveryState.SUPPRESSED else None
                sent_at = now if state == NotificationDeliveryState.SENT else None
                suppression_reason = str(item.get("suppression_reason") or NotificationSuppressionReason.NONE.value)
                evidence_summary = self._notification_evidence_summary(item.get("evidence") or [])
                history_id = hashlib.sha256(f"{household_id}|{stable_key}|{notification_type}".encode("utf-8")).hexdigest()[:32]
                row = (
                    history_id,
                    household_id,
                    stable_key,
                    item.get("candidate_id"),
                    notification_type,
                    str(item.get("priority") or NotificationPriority.LOW.value),
                    str(item.get("created_at") or now),
                    sent_at,
                    suppressed_at,
                    suppression_reason,
                    str(item.get("valid_until") or item.get("expires_at") or now),
                    evidence_summary,
                    state.value,
                    now,
                )
                conn.execute(
                    """
                    INSERT INTO notification_history (
                        history_id,
                        household_id,
                        stable_key,
                        candidate_id,
                        notification_type,
                        priority,
                        created_at,
                        sent_at,
                        suppressed_at,
                        suppression_reason,
                        expires_at,
                        evidence_summary,
                        delivery_state,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(household_id, stable_key, notification_type) DO UPDATE SET
                        candidate_id = excluded.candidate_id,
                        priority = excluded.priority,
                        created_at = excluded.created_at,
                        sent_at = COALESCE(excluded.sent_at, notification_history.sent_at),
                        suppressed_at = excluded.suppressed_at,
                        suppression_reason = excluded.suppression_reason,
                        expires_at = excluded.expires_at,
                        evidence_summary = excluded.evidence_summary,
                        delivery_state = CASE
                            WHEN notification_history.delivery_state = 'sent' AND excluded.delivery_state = 'suppressed'
                            THEN notification_history.delivery_state
                            ELSE excluded.delivery_state
                        END,
                        updated_at = excluded.updated_at
                    """,
                    row,
                )
                rows.append(row)
            conn.commit()
            return [self._normalise_notification_history(row) for row in rows]

        return self._with_connection(upsert)

    def get_recent_notification_history(self, limit: int = 20) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_notification_tables(conn)
            rows = conn.execute(
                """
                SELECT
                    history_id,
                    household_id,
                    stable_key,
                    candidate_id,
                    notification_type,
                    priority,
                    created_at,
                    sent_at,
                    suppressed_at,
                    suppression_reason,
                    expires_at,
                    evidence_summary,
                    delivery_state,
                    updated_at
                FROM notification_history
                WHERE household_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            return [self._normalise_notification_history(row) for row in rows]

        return self._with_connection(query)

    def get_notification_history_summary(self, limit: int = 20) -> dict[str, Any]:
        history = self.get_recent_notification_history(limit=limit)
        counts: dict[str, int] = {}
        for item in history:
            state = item["delivery_state"]
            counts[state] = counts.get(state, 0) + 1
        return {
            "household_id": self._active_household_id(),
            "count": len(history),
            "counts": counts,
            "latest": history[: min(limit, 10)],
        }

    def promote_provider_observations(
        self,
        *,
        limit: int = 100,
        tolerance_by_type: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        observations = self.get_provider_observations(limit=limit)
        results = PromotionEngine(tolerance_by_type=tolerance_by_type).evaluate(
            observations,
            household_id=self._active_household_id(),
        )
        self.persist_promotion_results(results)
        return self.get_promotion_summary()

    def persist_promotion_results(self, results: list[PromotionResult]) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def upsert(conn):
            DataEngine._ensure_promotion_table(conn)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            rows = []
            for result in results:
                candidate = result.candidate
                if candidate.household_id != household_id:
                    continue
                promotion_id = hashlib.sha256(
                    f"{household_id}|{candidate.observation_id}".encode("utf-8")
                ).hexdigest()[:32]
                reasons = [reason.as_dict() for reason in result.decision.reasons]
                conflict = result.decision.conflict.as_dict() if result.decision.conflict else None
                row = (
                    promotion_id,
                    household_id,
                    candidate.observation_id,
                    candidate.provider_id,
                    candidate.provider_type,
                    candidate.capability,
                    candidate.observation_type,
                    candidate.observed_at,
                    result.decision.state.value,
                    json.dumps(reasons, sort_keys=True),
                    json.dumps(conflict, sort_keys=True) if conflict else None,
                    json.dumps(candidate.value, sort_keys=True) if candidate.value is not None else None,
                    result.promoted_at,
                    now,
                    now,
                )
                conn.execute(
                    """
                    INSERT INTO promotion_decisions (
                        promotion_id,
                        household_id,
                        observation_id,
                        provider_id,
                        provider_type,
                        capability,
                        observation_type,
                        observed_at,
                        state,
                        reasons_json,
                        conflict_json,
                        value_json,
                        promoted_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(promotion_id) DO UPDATE SET
                        state = excluded.state,
                        reasons_json = excluded.reasons_json,
                        conflict_json = excluded.conflict_json,
                        value_json = excluded.value_json,
                        promoted_at = excluded.promoted_at,
                        updated_at = excluded.updated_at
                    """,
                    row,
                )
                rows.append(row)
            conn.commit()
            return [self._normalise_promotion(row) for row in rows]

        return self._with_connection(upsert)

    def get_latest_promotions(self, limit: int = 20) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_promotion_table(conn)
            rows = conn.execute(
                """
                SELECT
                    promotion_id,
                    household_id,
                    observation_id,
                    provider_id,
                    provider_type,
                    capability,
                    observation_type,
                    observed_at,
                    state,
                    reasons_json,
                    conflict_json,
                    value_json,
                    promoted_at,
                    created_at,
                    updated_at
                FROM promotion_decisions
                WHERE household_id = ?
                ORDER BY promoted_at DESC, observed_at DESC, promotion_id DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            return [self._normalise_promotion(row) for row in rows]

        return self._with_connection(query)

    def get_promotion_summary(self, limit: int = 20) -> dict[str, Any]:
        household_id = self._active_household_id()

        def query(conn):
            DataEngine._ensure_promotion_table(conn)
            state_rows = conn.execute(
                """
                SELECT state, COUNT(*)
                FROM promotion_decisions
                WHERE household_id = ?
                GROUP BY state
                """,
                (household_id,),
            ).fetchall()
            counts = {
                "ACCEPTED": 0,
                "REJECTED": 0,
                "SUPERSEDED": 0,
                "DUPLICATE": 0,
                "CONFLICT": 0,
                "PENDING": 0,
            }
            for state, count in state_rows:
                if state in counts:
                    counts[state] = int(count)
            latest = self.get_latest_promotions(limit=limit)
            conflicts = [item for item in latest if item["state"] == "CONFLICT"]
            return {
                "counts": counts,
                "accepted": counts["ACCEPTED"],
                "rejected": counts["REJECTED"],
                "conflicts": counts["CONFLICT"],
                "latest_promotions": latest,
                "conflict_summary": {
                    "count": counts["CONFLICT"],
                    "latest": conflicts[:10],
                    "repeated_conflicts": counts["CONFLICT"] >= 2,
                },
            }

        return self._with_connection(query)

    def get_import_job(self, job_id: str) -> dict[str, Any]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            row = conn.execute(
                """
                SELECT
                    job_id,
                    household_id,
                    source_id,
                    source_name,
                    filename,
                    status,
                    file_path,
                    preview_json,
                    result_json,
                    message,
                    created_at,
                    updated_at,
                    (
                        SELECT COUNT(*)
                        FROM provider_observations po
                        WHERE po.household_id = import_jobs.household_id
                          AND po.import_job_id = import_jobs.job_id
                    ) AS provider_observation_count
                FROM import_jobs
                WHERE household_id = ? AND job_id = ?
                """,
                (household_id, job_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Import job not found")
            return self._normalise_import_job(row)

        return self._with_connection(query)

    def get_import_history(self, limit: int = 20) -> list[dict[str, Any]]:
        household_id = self._active_household_id()

        def query(conn):
            self.validate_connection(conn)
            rows = conn.execute(
                """
                SELECT
                    job_id,
                    household_id,
                    source_id,
                    source_name,
                    filename,
                    status,
                    file_path,
                    preview_json,
                    result_json,
                    message,
                    created_at,
                    updated_at,
                    (
                        SELECT COUNT(*)
                        FROM provider_observations po
                        WHERE po.household_id = import_jobs.household_id
                          AND po.import_job_id = import_jobs.job_id
                    ) AS provider_observation_count
                FROM import_jobs
                WHERE household_id = ?
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """,
                (household_id, int(limit)),
            ).fetchall()
            return [self._normalise_import_job(row) for row in rows]

        return self._with_connection(query)

    @staticmethod
    def _normalise_import_job(row) -> dict[str, Any]:
        def parse_json(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        return {
            "job_id": row[0],
            "household_id": row[1],
            "source": {
                "source_id": row[2],
                "display_name": row[3],
            },
            "filename": row[4],
            "status": row[5],
            "file_path": row[6],
            "preview": parse_json(row[7]),
            "result": parse_json(row[8]),
            "message": row[9],
            "created_at": row[10],
            "updated_at": row[11],
            "provider_observation_count": int(row[12] or 0) if len(row) > 12 else 0,
        }

    @staticmethod
    def _normalise_provider_observation(row) -> dict[str, Any]:
        def parse_json(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        return {
            "observation_id": row[0],
            "household_id": row[1],
            "provider_id": row[2],
            "provider_type": row[3],
            "capability": row[4],
            "observation_type": row[5],
            "observed_at": row[6],
            "received_at": row[7],
            "value": parse_json(row[8]),
            "unit": row[9],
            "quality": row[10],
            "freshness": parse_json(row[11]),
            "provenance": row[12],
            "raw_reference": row[13],
            "import_job_id": row[14],
            "created_at": row[15],
        }

    @staticmethod
    def _normalise_promotion(row) -> dict[str, Any]:
        def parse_json(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        return {
            "promotion_id": row[0],
            "household_id": row[1],
            "observation_id": row[2],
            "provider_id": row[3],
            "provider_type": row[4],
            "capability": row[5],
            "observation_type": row[6],
            "observed_at": row[7],
            "state": row[8],
            "reasons": parse_json(row[9]) or [],
            "conflict": parse_json(row[10]),
            "value": parse_json(row[11]),
            "promoted_at": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

    @staticmethod
    def _normalise_household_state_snapshot(row) -> dict[str, Any]:
        def parse_json(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        return {
            "snapshot_id": row[0],
            "household_id": row[1],
            "captured_at": row[2],
            "material_hash": row[3],
            "tariff_state": parse_json(row[4]) or {},
            "power_state": parse_json(row[5]) or {},
            "energy_state": parse_json(row[6]) or {},
            "solar_state": parse_json(row[7]) or {},
            "ev_state": parse_json(row[8]) or {},
            "battery_state": parse_json(row[9]) or {},
            "billing_state": parse_json(row[10]) or {},
            "collector_state": parse_json(row[11]) or {},
            "evidence_state": parse_json(row[12]) or {},
            "provider_provenance": parse_json(row[13]) or {},
            "context_summary": parse_json(row[14]),
            "state": parse_json(row[15]) or {},
            "created_at": row[16],
        }

    @staticmethod
    def _normalise_state_change(row) -> dict[str, Any]:
        try:
            evidence = json.loads(row[10]) if row[10] else []
        except json.JSONDecodeError:
            evidence = []
        return {
            "change_id": row[0],
            "household_id": row[1],
            "event_key": row[2],
            "change_type": row[3],
            "severity": row[4],
            "title": row[5],
            "summary": row[6],
            "detected_at": row[7],
            "previous_snapshot_id": row[8],
            "current_snapshot_id": row[9],
            "evidence": evidence,
            "created_at": row[11],
        }

    @staticmethod
    def _normalise_notification_preferences(row, rule_rows, household_id: str) -> NotificationPreferences:
        rules = [
            NotificationRulePreference(
                notification_type=NotificationType(rule[0]),
                enabled=bool(rule[1]),
                cooldown_minutes=rule[2],
            )
            for rule in (rule_rows or [])
        ]
        if not row:
            return NotificationPreferences(
                household_id=household_id,
                enabled=False,
                rule_preferences=rules,
            )
        return NotificationPreferences(
            household_id=row[0],
            enabled=bool(row[1]),
            priority_threshold=NotificationPriority(row[2]),
            quiet_hours=NotificationQuietHours(
                enabled=bool(row[3]),
                start=row[4],
                end=row[5],
                timezone=row[6],
            ),
            channel_preference=NotificationChannel(row[7]),
            cooldown_minutes=int(row[8]),
            rule_preferences=rules,
        )

    @staticmethod
    def _normalise_notification_history(row) -> dict[str, Any]:
        return {
            "history_id": row[0],
            "household_id": row[1],
            "stable_key": row[2],
            "candidate_id": row[3],
            "notification_type": row[4],
            "priority": row[5],
            "created_at": row[6],
            "sent_at": row[7],
            "suppressed_at": row[8],
            "suppression_reason": row[9] or NotificationSuppressionReason.NONE.value,
            "expires_at": row[10],
            "evidence_summary": row[11],
            "delivery_state": row[12],
            "updated_at": row[13],
        }

    @staticmethod
    def _coerce_notification_preferences(value, household_id: str) -> NotificationPreferences:
        if isinstance(value, NotificationPreferences):
            if value.household_id == household_id:
                return value
            return NotificationPreferences(
                household_id=household_id,
                enabled=value.enabled,
                priority_threshold=value.priority_threshold,
                quiet_hours=value.quiet_hours,
                channel_preference=value.channel_preference,
                cooldown_minutes=value.cooldown_minutes,
                rule_preferences=value.rule_preferences,
            )
        data = dict(value or {})
        quiet = data.get("quiet_hours") or {}
        rules = [
            NotificationRulePreference(
                notification_type=NotificationType(rule["notification_type"]),
                enabled=bool(rule.get("enabled", True)),
                cooldown_minutes=rule.get("cooldown_minutes"),
            )
            for rule in data.get("rule_preferences", [])
        ]
        return NotificationPreferences(
            household_id=household_id,
            enabled=bool(data.get("enabled", False)),
            priority_threshold=NotificationPriority(data.get("priority_threshold", NotificationPriority.LOW.value)),
            quiet_hours=NotificationQuietHours(
                enabled=bool(quiet.get("enabled", False)),
                start=str(quiet.get("start", "22:00")),
                end=str(quiet.get("end", "07:00")),
                timezone=str(quiet.get("timezone", "local")),
            ),
            channel_preference=NotificationChannel(data.get("channel_preference", NotificationChannel.LOCAL_UI.value)),
            cooldown_minutes=int(data.get("cooldown_minutes", 120)),
            rule_preferences=rules,
        )

    @staticmethod
    def _notification_evidence_summary(evidence: list[dict[str, Any]]) -> str:
        summaries = []
        for item in evidence[:3]:
            if isinstance(item, dict):
                summary = item.get("summary") or item.get("source")
            else:
                summary = str(item)
            if summary:
                summaries.append(str(summary))
        return "; ".join(summaries) or "No notification evidence supplied."

    @staticmethod
    def _household_state_material_hash(state: dict[str, Any], context_summary: Optional[dict[str, Any]]) -> str:
        material = {
            "state": DataEngine._strip_volatile_state_fields(state),
            "context": DataEngine._strip_volatile_state_fields(context_summary or {}),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _strip_volatile_state_fields(value):
        if isinstance(value, dict):
            return {
                key: DataEngine._strip_volatile_state_fields(item)
                for key, item in value.items()
                if key not in {"generated_at", "captured_at", "created_at", "observed_at"}
            }
        if isinstance(value, list):
            return [DataEngine._strip_volatile_state_fields(item) for item in value]
        return value

    @staticmethod
    def _redact_household_state_snapshot_payload(value):
        sensitive_markers = ("password", "cookie", "api_key", "token", "secret", "authorization")
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if any(marker in str(key).lower() for marker in sensitive_markers):
                    redacted[key] = "REDACTED"
                else:
                    redacted[key] = DataEngine._redact_household_state_snapshot_payload(item)
            return redacted
        if isinstance(value, list):
            return [DataEngine._redact_household_state_snapshot_payload(item) for item in value]
        return value

    @staticmethod
    def _latest_household_state_snapshot_row(conn: sqlite3.Connection, household_id: str):
        return conn.execute(
            """
            SELECT
                snapshot_id,
                household_id,
                captured_at,
                material_hash,
                tariff_state_json,
                power_state_json,
                energy_state_json,
                solar_state_json,
                ev_state_json,
                battery_state_json,
                billing_state_json,
                collector_state_json,
                evidence_state_json,
                provider_provenance_json,
                context_summary_json,
                state_json,
                created_at
            FROM household_state_snapshots
            WHERE household_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (household_id,),
        ).fetchone()

    @staticmethod
    def _household_state_changes(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        changes = []
        for current, previous in zip(snapshots, snapshots[1:]):
            changed = []
            for key in ("tariff_state", "power_state", "solar_state", "ev_state", "battery_state", "billing_state", "collector_state", "evidence_state"):
                if current.get(key) != previous.get(key):
                    changed.append(key.replace("_state", ""))
            if changed:
                changes.append(
                    {
                        "captured_at": current["captured_at"],
                        "changed": changed,
                    }
                )
        return changes

    def persist_timeline_entries(self, entries: list[TimelineEntry]) -> DecisionTimeline:
        household_id = self._active_household_id()

        def upsert(conn):
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            for entry in entries:
                event_key = self._timeline_event_key(entry, household_id)
                if entry.event_type in {"billing_period", "billing_period_created", "billing_period_updated"}:
                    conn.execute(
                        """
                        DELETE FROM timeline_entries
                        WHERE household_id = ?
                          AND timestamp = ?
                          AND event_type IN ('billing_period', 'billing_period_created', 'billing_period_updated')
                          AND event_key != ?
                        """,
                        (household_id, entry.timestamp, event_key),
                    )
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
                        event_key,
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
            seen_billing_periods = set()
            for row in rows:
                try:
                    evidence_sources = json.loads(row[5]) if row[5] else []
                except json.JSONDecodeError:
                    evidence_sources = []
                entry = TimelineEntry(
                    timestamp=row[0],
                    title=row[1],
                    summary=row[2],
                    financial_impact=row[3],
                    confidence=row[4],
                    evidence_sources=list(evidence_sources),
                    event_type=row[6],
                )
                if entry.event_type in {"billing_period", "billing_period_created", "billing_period_updated"}:
                    if entry.timestamp in seen_billing_periods:
                        continue
                    seen_billing_periods.add(entry.timestamp)
                entries.append(entry)
            billing_entries = [
                entry for entry in entries
                if entry.event_type in {"billing_period", "billing_period_created", "billing_period_updated"}
            ]
            if len(billing_entries) > 1:
                latest_billing = max(billing_entries, key=lambda entry: entry.timestamp)
                entries = [
                    entry for entry in entries
                    if entry.event_type not in {"billing_period", "billing_period_created", "billing_period_updated"}
                    or entry is latest_billing
                ]
            return DecisionTimeline(entries=entries)

        return self._with_connection(query)

    @staticmethod
    def _timeline_event_key(entry: TimelineEntry, household_id: str = DEFAULT_HOUSEHOLD_ID) -> str:
        event_type = (
            "billing_period"
            if entry.event_type in {"billing_period", "billing_period_created", "billing_period_updated"}
            else entry.event_type
        )
        title = "" if event_type == "billing_period" else entry.title
        raw = "|".join((household_id, event_type, entry.timestamp, title))
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

    def get_household_capability_summary(
        self,
        today: date,
        settings: Any = None,
    ) -> HouseholdCapabilitySummary:
        household_id = self._active_household_id()
        health = self.health_loader()
        import_rates = getattr(settings, "import_rates", None) if settings is not None else None
        tariff_configured = _tariff_configured(import_rates)

        def query(conn):
            self.validate_connection(conn)
            latest_row = conn.execute(
                """
                SELECT MAX(measurement_time)
                FROM readings
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            latest = latest_row[0] if latest_row else None
            freshness = freshness_from_latest(latest, today)
            reading_row = conn.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(ABS(COALESCE(grid_to_home_kwh, 0))), 0),
                    COALESCE(SUM(ABS(COALESCE(production_kwh, 0))), 0),
                    COALESCE(SUM(ABS(COALESCE(solar_to_home_kwh, 0))), 0),
                    COALESCE(SUM(ABS(COALESCE(solar_to_grid_kwh, 0))), 0),
                    COALESCE(SUM(ABS(COALESCE(import_cost, 0))), 0)
                FROM readings
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            ev_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(energy_kwh), 0)
                FROM ev_sessions
                WHERE household_id = ?
                """,
                (household_id,),
            ).fetchone()
            import_job_count = 0
            if DataEngine._table_exists(conn, "import_jobs"):
                import_job_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM import_jobs
                    WHERE household_id = ?
                    """,
                    (household_id,),
                ).fetchone()[0]
            observation_counts: dict[str, int] = {}
            if DataEngine._table_exists(conn, "provider_observations"):
                observation_rows = conn.execute(
                    """
                    SELECT capability, COUNT(*)
                    FROM provider_observations
                    WHERE household_id = ?
                    GROUP BY capability
                    """,
                    (household_id,),
                ).fetchall()
                observation_counts = {row[0]: int(row[1]) for row in observation_rows}

            measurement_sources = _measurement_capability_sources(SolarEdgeMeasurementProvider([]).capabilities())
            reading_count = int(reading_row[0] or 0)
            grid_kwh = float(reading_row[1] or 0)
            solar_kwh = sum(float(reading_row[index] or 0) for index in (2, 3, 4))
            import_cost = float(reading_row[5] or 0)
            ev_sessions = int(ev_row[0] or 0)
            ev_kwh = float(ev_row[1] or 0)
            collector_available = bool(
                health
                or (self.collect_log_path and self.collect_log_path.exists())
                or reading_count
            )
            capabilities = [
                CapabilityStatus(
                    "solar",
                    "Solar",
                    present=solar_kwh > 0,
                    source=measurement_sources.get(MeasurementType.SOLAR_GENERATION, "provider:solaredge"),
                    confidence="High" if solar_kwh > 0 else "Medium",
                    reason=(
                        f"Detected {solar_kwh:.2f} kWh of solar production/export readings. {_observation_count_note(observation_counts, 'daily_summary')}"
                        if solar_kwh > 0 else
                        "No solar production or export readings found for this household."
                    ),
                    freshness=freshness if solar_kwh > 0 else None,
                ),
                CapabilityStatus(
                    "ev",
                    "EV charging",
                    present=ev_sessions > 0 and ev_kwh > 0,
                    available=True,
                    source="provider:ocular_export",
                    confidence="High" if ev_sessions > 0 and ev_kwh > 0 else "Medium",
                    reason=(
                        f"Detected {ev_sessions} EV charging session rows. {_observation_count_note(observation_counts, 'ev_sessions')}"
                        if ev_sessions > 0 and ev_kwh > 0 else
                        "No persisted EV charging sessions found; Ocular upload can add this data."
                    ),
                ),
                CapabilityStatus(
                    "battery",
                    "Battery",
                    present=False,
                    available=False,
                    source="placeholder",
                    confidence="Low",
                    reason="Battery detection is reserved for a future supported data source.",
                ),
                CapabilityStatus(
                    "grid_smart_meter",
                    "Grid / smart meter",
                    present=reading_count > 0 and grid_kwh >= 0,
                    source=measurement_sources.get(MeasurementType.GRID_IMPORT, "provider:smart_meter_readings"),
                    confidence="High" if reading_count else "Medium",
                    reason=(
                        f"Detected {reading_count} household interval readings. {_observation_count_note(observation_counts, 'interval_history')}"
                        if reading_count else
                        "No household interval readings found."
                    ),
                    freshness=freshness if reading_count else None,
                ),
                CapabilityStatus(
                    "tariff",
                    "Tariff",
                    configured=tariff_configured or import_cost > 0,
                    source="provider:synergy_tariff_config" if tariff_configured else "provider:smart_meter_readings" if import_cost > 0 else None,
                    confidence="High" if tariff_configured else "Medium" if import_cost > 0 else "Low",
                    reason=(
                        "Tariff rates are configured in settings."
                        if tariff_configured else
                        "Import costs exist in readings but configured tariff rates are missing."
                        if import_cost > 0 else
                        "No configured tariff rates or import cost evidence found."
                    ),
                ),
                CapabilityStatus(
                    "imports",
                    "Imports",
                    present=import_job_count > 0,
                    available=True,
                    source="provider:ocular_export",
                    confidence="High",
                    reason=(
                        f"{import_job_count} import job records are available. {_observation_count_note(observation_counts, 'import_only') or _observation_count_note(observation_counts, 'ev_sessions')}"
                        if import_job_count else
                        "Ocular EV charging history upload is available."
                    ),
                ),
                CapabilityStatus(
                    "live_local_collector",
                    "Live/local collector",
                    present=collector_available,
                    source="collector_health",
                    confidence="High" if collector_available else "Low",
                    reason=(
                        "Live data status, logs, or local readings are available."
                        if collector_available else
                        "No local live data status or readings found."
                    ),
                ),
                CapabilityStatus(
                    "data_freshness",
                    "Data freshness",
                    present=freshness.state in {"fresh", "stale"},
                    source="readings",
                    confidence="High" if latest else "Low",
                    reason=(
                        f"Latest reading timestamp is {latest}."
                        if latest else
                        "No latest reading timestamp is available."
                    ),
                    freshness=freshness,
                ),
            ]
            return HouseholdCapabilitySummary(
                household_id=household_id,
                generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                capabilities=capabilities,
            )

        return self._with_connection(query)

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


def _tariff_configured(import_rates: Any) -> bool:
    if not isinstance(import_rates, dict):
        return False
    required = {"overnight", "off_peak", "super_off_peak", "peak"}
    if not required.issubset(import_rates.keys()):
        return False
    try:
        return all(float(import_rates[bucket]) > 0 for bucket in required)
    except (TypeError, ValueError):
        return False


def _observation_type_for_capability(capability: ProviderCapability) -> ProviderObservationType:
    if capability == ProviderCapability.EV_SESSIONS:
        return ProviderObservationType.EV_SESSION
    if capability == ProviderCapability.TARIFF_DATA:
        return ProviderObservationType.TARIFF_CONFIG
    if capability == ProviderCapability.BILLING_DATA:
        return ProviderObservationType.BILLING_PERIOD
    if capability == ProviderCapability.INTERVAL_HISTORY:
        return ProviderObservationType.GRID_IMPORT
    return ProviderObservationType.COLLECTOR_HEALTH


def _observation_count_note(counts: dict[str, int], capability: str) -> str:
    count = int(counts.get(capability, 0))
    if count <= 0:
        return ""
    return f"{count} source reading{'s' if count != 1 else ''} recorded."


def _json_value(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _quality_tier_for_resolution(resolution: Any) -> str:
    value = str(resolution or "").lower()
    if "15" in value or "interval" in value or "quarter" in value:
        return "interval_grade"
    if "daily" in value:
        return "daily_aggregate"
    return "monthly_lifetime_aggregate"


def _measurement_capability_sources(
    capabilities: list[MeasurementCapability],
    provider_id: str = "solaredge",
) -> dict[MeasurementType, str]:
    return {
        capability.measurement_type: f"measurement:{provider_id}:{capability.measurement_type.value}"
        for capability in capabilities
        if capability.available
    }


def freshness_from_latest(latest_measurement: Optional[str], today: date) -> Freshness:
    if not latest_measurement:
        return Freshness(state="unknown")
    try:
        observed = datetime.fromisoformat(latest_measurement)
    except ValueError:
        return Freshness(state="unknown", observed_at=latest_measurement)
    state = "fresh" if observed.date() >= today else "stale"
    return Freshness(state=state, observed_at=latest_measurement)
