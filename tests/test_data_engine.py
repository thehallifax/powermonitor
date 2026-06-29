import sqlite3
import unittest
from dataclasses import dataclass
from datetime import date

from engine.data_engine import DataEngine
from engine.decision_engine import generate_decisions_from_data_engine
from engine.models import TimelineEntry
from engine.provider_framework import (
    ProviderCapability,
    ProviderFreshness,
    ProviderObservation,
    ProviderObservationQuality,
    ProviderObservationType,
    ProviderType,
)


@dataclass
class Settings:
    billing_period_start_day: int = 6
    current_billing_period_start: date = date(2026, 6, 6)
    current_billing_period_end = None
    previous_billing_period_start = None
    previous_billing_period_end = None
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }
    ev_baseline_watts = 250.0
    recommendation_thresholds = {
        "peak_cost_share_pct": 25.0,
        "peak_shift_kwh_per_day": 5.0,
        "solar_self_consumption_pct": 60.0,
        "ev_detected_min_kwh": 0.5,
        "baseload_excess_pct": 20.0,
        "high_cost_threshold": 300.0,
        "positive_peak_share_pct": 20.0,
        "positive_self_consumption_pct": 70.0,
    }


class DataEngineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE readings (
                measurement_time TEXT,
                tariff_bucket TEXT,
                production_kwh REAL,
                solar_to_home_kwh REAL,
                solar_to_grid_kwh REAL,
                grid_to_home_w REAL,
                grid_to_home_kwh REAL,
                import_cost REAL
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO readings (
                measurement_time, tariff_bucket, production_kwh,
                solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_w,
                grid_to_home_kwh, import_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-06-27T00:15:00", "overnight", 0.0, 0.0, 0.0, 320.0, 0.08, 0.02),
                ("2026-06-27T00:30:00", "overnight", 0.0, 0.0, 0.0, 280.0, 0.07, 0.02),
                ("2026-06-27T12:00:00", "super_off_peak", 1.4, 0.8, 0.6, 90.0, 0.03, 0.01),
            ],
        )
        self.engine = DataEngine(connection=self.conn, health_loader=lambda: {"status": "ok"})
        self.engine.validate_database()
        self.conn.execute(
            """
            INSERT INTO ev_sessions (
                start_time, end_time, energy_kwh, source, cost_estimate,
                transaction_id, super_off_peak_kwh, super_off_peak_cost,
                overnight_kwh, overnight_cost, off_peak_kwh, off_peak_cost,
                peak_kwh, peak_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-27T10:00:00",
                "2026-06-27T11:00:00",
                7.5,
                "ocular_export",
                0.65,
                "abc123",
                7.5,
                0.65,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
        )

    def tearDown(self):
        self.conn.close()

    def test_get_bill_period_uses_configured_start(self):
        period = self.engine.get_bill_period(date(2026, 6, 27), Settings())

        self.assertEqual(period.start, date(2026, 6, 6))
        self.assertEqual(period.end, date(2026, 6, 27))
        self.assertEqual(period.as_dict()["kind"], "current_billing_period")

    def test_get_intervals_and_current_summary_do_not_calculate_money(self):
        grid = self.engine.get_grid_intervals(date(2026, 6, 27), date(2026, 6, 27))
        solar = self.engine.get_solar_intervals(date(2026, 6, 27), date(2026, 6, 27))
        summary = self.engine.get_current_bill_summary(date(2026, 6, 27), date(2026, 6, 27))

        self.assertEqual(len(grid), 3)
        self.assertEqual(len(solar), 3)
        self.assertAlmostEqual(summary["grid_import_kwh"], 0.18)
        self.assertNotIn("net_cost", summary)

    def test_get_ev_sessions_normalises_tariff_breakdown(self):
        sessions = self.engine.get_ev_sessions(
            date(2026, 6, 27),
            date(2026, 6, 27),
            sources=["ocular_export"],
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["transaction_id"], "abc123")
        self.assertEqual(sessions[0]["dominant_tariff"], "super_off_peak")
        self.assertAlmostEqual(sessions[0]["tariff_breakdown"]["super_off_peak"]["kwh"], 7.5)

    def test_completeness_and_snapshot_use_shared_status_models(self):
        completeness = self.engine.get_data_completeness(
            {
                "solar_generated_kwh": 1.4,
                "solar_used_kwh": 0.8,
                "solar_exported_kwh": 0.6,
                "intervals": 3,
                "ev_charging_source": "ocular_export",
            },
            billing_configured=True,
            confidence="High",
            confidence_note="Complete test data",
        )
        snapshot = self.engine.get_latest_household_snapshot(date(2026, 6, 27))

        self.assertEqual(completeness.confidence, "High")
        self.assertEqual(snapshot.freshness.state, "fresh")
        self.assertEqual(snapshot.connector_status.state, "ready")

    def test_source_counts_are_normalised(self):
        counts = self.engine.get_ev_source_counts()

        self.assertEqual(counts[0]["source"], "ocular_export")
        self.assertEqual(counts[0]["sessions"], 1)

    def test_provider_observations_are_persisted_and_summarised(self):
        observations = self.engine.persist_provider_observations(
            [
                ProviderObservation(
                    ProviderCapability.EV_SESSIONS,
                    "ev_session",
                    value={"transaction_id": "abc123", "energy_kwh": 7.5},
                    unit="session",
                    provider_id="ocular_export",
                    provider_type=ProviderType.EV_CHARGER,
                    observation_type=ProviderObservationType.EV_SESSION,
                    observed_at="2026-06-27T11:00:00",
                    quality=ProviderObservationQuality.HIGH,
                    freshness=ProviderFreshness(state="fresh", observed_at="2026-06-27T11:00:00"),
                    provenance="test",
                    raw_reference="abc123",
                    import_job_id="job_1",
                )
            ]
        )
        stored = self.engine.get_provider_observations()
        summary = self.engine.get_provider_observation_summary()

        self.assertEqual(len(observations), 1)
        self.assertEqual(stored[0]["provider_id"], "ocular_export")
        self.assertEqual(stored[0]["observation_type"], "ev_session")
        self.assertEqual(stored[0]["value"]["energy_kwh"], 7.5)
        self.assertEqual(summary["providers"][0]["capabilities"], ["ev_sessions"])

    def test_provider_observations_are_household_scoped(self):
        from engine.models import Account, Household, HouseholdContext, HouseholdMembership

        household_b = HouseholdContext(
            Account("acct_b", "Account B", "local"),
            Household("household_b", "Household B", "Australia/Perth"),
            HouseholdMembership("acct_b", "household_b", "owner"),
        )
        engine_b = DataEngine(connection=self.conn, household_context=household_b)
        self.engine.persist_provider_observations(
            [
                ProviderObservation(
                    ProviderCapability.IMPORT_ONLY,
                    "collector_health",
                    value={"status": "ok"},
                    provider_id="collector",
                    provider_type=ProviderType.GENERIC_IMPORT,
                    observation_type=ProviderObservationType.COLLECTOR_HEALTH,
                    observed_at="2026-06-27T12:00:00",
                    quality=ProviderObservationQuality.MEDIUM,
                )
            ]
        )

        self.assertEqual(len(self.engine.get_provider_observations()), 1)
        self.assertEqual(engine_b.get_provider_observations(), [])

    def test_provider_observations_are_promoted_to_trusted_read_models(self):
        self.engine.persist_provider_observations(
            [
                ProviderObservation(
                    ProviderCapability.EV_SESSIONS,
                    "ev_session",
                    value={"transaction_id": "abc123", "energy_kwh": 7.5},
                    provider_id="ocular_export",
                    provider_type=ProviderType.EV_CHARGER,
                    observation_type=ProviderObservationType.EV_SESSION,
                    observed_at="2026-06-27T11:00:00",
                    quality=ProviderObservationQuality.HIGH,
                    raw_reference="abc123",
                )
            ]
        )

        summary = self.engine.promote_provider_observations()
        latest = self.engine.get_latest_promotions()

        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(latest[0]["state"], "ACCEPTED")
        self.assertEqual(latest[0]["observation_type"], "ev_session")

    def test_promotion_conflicts_are_summarised(self):
        self.engine.persist_provider_observations(
            [
                ProviderObservation(
                    ProviderCapability.DAILY_SUMMARY,
                    "grid_import",
                    value=10.0,
                    provider_id="meter_a",
                    provider_type=ProviderType.SMART_METER,
                    observation_type=ProviderObservationType.GRID_IMPORT,
                    observed_at="2026-06-27T12:00:00",
                    quality=ProviderObservationQuality.HIGH,
                    raw_reference="reading",
                ),
                ProviderObservation(
                    ProviderCapability.DAILY_SUMMARY,
                    "grid_import",
                    value=11.0,
                    provider_id="meter_b",
                    provider_type=ProviderType.SMART_METER,
                    observation_type=ProviderObservationType.GRID_IMPORT,
                    observed_at="2026-06-27T12:00:00",
                    quality=ProviderObservationQuality.HIGH,
                    raw_reference="reading",
                ),
            ]
        )

        summary = self.engine.promote_provider_observations(tolerance_by_type={"grid_import": 0.1})

        self.assertEqual(summary["conflicts"], 1)
        self.assertEqual(summary["conflict_summary"]["count"], 1)

    def test_promotions_are_household_scoped(self):
        from engine.models import Account, Household, HouseholdContext, HouseholdMembership

        household_b = HouseholdContext(
            Account("acct_b", "Account B", "local"),
            Household("household_b", "Household B", "Australia/Perth"),
            HouseholdMembership("acct_b", "household_b", "owner"),
        )
        engine_b = DataEngine(connection=self.conn, household_context=household_b)
        self.engine.persist_provider_observations(
            [
                ProviderObservation(
                    ProviderCapability.EV_SESSIONS,
                    "ev_session",
                    value={"energy_kwh": 7.5},
                    provider_id="ocular_export",
                    provider_type=ProviderType.EV_CHARGER,
                    observation_type=ProviderObservationType.EV_SESSION,
                    observed_at="2026-06-27T11:00:00",
                    quality=ProviderObservationQuality.HIGH,
                )
            ]
        )
        self.engine.promote_provider_observations()

        self.assertEqual(self.engine.get_promotion_summary()["accepted"], 1)
        self.assertEqual(engine_b.get_promotion_summary()["accepted"], 0)

    def test_timeline_entries_are_persisted_idempotently(self):
        entry = TimelineEntry(
            timestamp="2026-06-27",
            title="High peak usage",
            summary="Peak usage was high.",
            financial_impact="$4.20 this billing period",
            confidence="Medium",
            evidence_sources=["SolarEdge interval data", "Tariff settings"],
            event_type="high_peak_usage",
        )

        first = self.engine.persist_timeline_entries([entry])
        updated = TimelineEntry(
            timestamp=entry.timestamp,
            title=entry.title,
            summary="Peak usage was still high.",
            financial_impact=entry.financial_impact,
            confidence=entry.confidence,
            evidence_sources=entry.evidence_sources,
            event_type=entry.event_type,
        )
        second = self.engine.persist_timeline_entries([updated])

        self.assertEqual(len(first.entries), 1)
        self.assertEqual(len(second.entries), 1)
        self.assertEqual(second.entries[0].summary, "Peak usage was still high.")
        self.assertEqual(second.entries[0].event_type, "high_peak_usage")

    def test_billing_period_timeline_events_update_without_duplicates(self):
        created = TimelineEntry(
            timestamp="2026-06-06",
            title="Billing period created",
            summary="Created billing-period tracking.",
            financial_impact=None,
            confidence="High",
            evidence_sources=["Billing period settings"],
            event_type="billing_period_created",
        )
        updated = TimelineEntry(
            timestamp="2026-06-06",
            title="Billing period updated",
            summary="Updated billing-period tracking with readings.",
            financial_impact=None,
            confidence="High",
            evidence_sources=["Billing period settings"],
            event_type="billing_period_updated",
        )

        self.engine.persist_timeline_entries([created])
        timeline = self.engine.persist_timeline_entries([updated])
        billing_entries = [
            entry for entry in timeline.entries
            if entry.event_type in {"billing_period_created", "billing_period_updated"}
        ]

        self.assertEqual(len(billing_entries), 1)
        self.assertEqual(billing_entries[0].title, "Billing period updated")

    def test_timeline_readback_hides_older_billing_period_system_noise(self):
        self.engine.persist_timeline_entries(
            [
                TimelineEntry(
                    timestamp="2026-01-01",
                    title="Billing period updated",
                    summary="Old non-current billing row.",
                    financial_impact=None,
                    confidence="High",
                    evidence_sources=["Billing period settings"],
                    event_type="billing_period_updated",
                ),
                TimelineEntry(
                    timestamp="2026-06-06",
                    title="Billing period updated",
                    summary="Current billing row.",
                    financial_impact=None,
                    confidence="High",
                    evidence_sources=["Billing period settings"],
                    event_type="billing_period_updated",
                ),
            ]
        )

        timeline = self.engine.get_timeline_entries()
        billing_entries = [
            entry for entry in timeline.entries
            if entry.event_type == "billing_period_updated"
        ]

        self.assertEqual(len(billing_entries), 1)
        self.assertEqual(billing_entries[0].timestamp, "2026-06-06")

    def test_timeline_entries_can_be_queried_by_period(self):
        entries = [
            TimelineEntry(
                timestamp="2026-06-01",
                title="Old event",
                summary="Outside selected period.",
                financial_impact=None,
                confidence="High",
                evidence_sources=["test"],
                event_type="billing_period",
            ),
            TimelineEntry(
                timestamp="2026-06-27",
                title="Current event",
                summary="Inside selected period.",
                financial_impact=None,
                confidence="High",
                evidence_sources=["test"],
                event_type="bill_tracking",
            ),
        ]

        self.engine.persist_timeline_entries(entries)
        timeline = self.engine.get_timeline_entries(
            date(2026, 6, 20),
            date(2026, 6, 30),
        )

        self.assertEqual([entry.title for entry in timeline.entries], ["Current event"])

    def test_recommendation_lifecycle_is_persisted_and_counted(self):
        dismissed = self.engine.set_recommendation_lifecycle(
            "peak_cost_reduction",
            "dismissed",
            reason="Not relevant this week",
        )
        snoozed = self.engine.set_recommendation_lifecycle(
            "solar_self_consumption",
            "snoozed_until",
            snoozed_until="2999-01-01",
        )

        lifecycle = self.engine.get_recommendation_lifecycle()
        counts = self.engine.get_recommendation_lifecycle_counts()

        self.assertEqual(dismissed["state"], "dismissed")
        self.assertEqual(snoozed["snoozed_until"], "2999-01-01")
        self.assertEqual(lifecycle["peak_cost_reduction"]["reason"], "Not relevant this week")
        self.assertEqual(counts["dismissed"], 1)
        self.assertEqual(counts["snoozed_until"], 1)

    def test_recommendation_lifecycle_validates_state(self):
        with self.assertRaises(ValueError):
            self.engine.set_recommendation_lifecycle("peak_cost_reduction", "hidden")

        with self.assertRaises(ValueError):
            self.engine.set_recommendation_lifecycle("peak_cost_reduction", "snoozed_until")

    def test_decision_engine_consumes_data_engine_for_baseload(self):
        settings = Settings()
        summary = {
            "start": date(2026, 6, 27),
            "end": date(2026, 6, 27),
            "supply_days": 1,
            "reading_days": 1,
            "peak_cost_share_pct": 0.0,
            "buckets": {"peak": {"kwh": 0.0, "cost": 0.0}},
            "self_consumption_pct": 100.0,
            "solar_export_pct": 0.0,
            "ev_charging_kwh": 0.0,
            "ev_charging_cost": 0.0,
            "ev_charging_source": "estimated",
            "net_cost": 1.0,
            "average_daily_net_cost": 1.0,
        }

        decisions = generate_decisions_from_data_engine(
            summary,
            settings,
            self.engine,
            {**settings.recommendation_thresholds, "baseload_excess_pct": 10.0},
        )

        self.assertIn("baseload_monitoring", [decision.id for decision in decisions])
        baseload = next(decision for decision in decisions if decision.id == "baseload_monitoring")
        self.assertIsNotNone(baseload.explanation)
        self.assertEqual(
            baseload.explanation.data_sources_used,
            ["SolarEdge interval data", "Estimated EV charging"],
        )
        self.assertIn("overnight load", baseload.explanation.why_this_matters)


if __name__ == "__main__":
    unittest.main()
