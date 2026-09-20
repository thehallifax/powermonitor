import sqlite3
import unittest
from datetime import date

from engine.data_engine import DataEngine
from engine.household_timeline import HouseholdTariffResolver, HouseholdTimeline, HouseholdTimelineEvent
from engine.period_summary import PeriodSummaryService
from engine.tariff_versioning import TariffResolver, TariffVersion, TariffVersionStore


def tariff(version, effective_from, *, effective_to=None, supply=1.0, peak=0.5, overnight=0.1, off_peak=0.3):
    return TariffVersion.create(
        provider="Synergy",
        tariff_name="Synergy EV Add On",
        version=version,
        effective_from=date.fromisoformat(effective_from),
        effective_to=date.fromisoformat(effective_to) if effective_to else None,
        supply_charge_per_day=supply,
        usage_rates={
            "peak": peak,
            "overnight": overnight,
            "off_peak": off_peak,
            "super_off_peak": 0.2,
        },
        export_rates={"standard": 0.02},
        tariff_windows=("23:00-06:00", "09:00-15:00"),
        source_identifier="test_fixture",
        created_at="2026-01-01T00:00:00+00:00",
        gst_included=True,
    )


class PeriodSummaryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE readings (
                household_id TEXT DEFAULT 'default_household',
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
        DataEngine(connection=self.connection).validate_database()
        self.v1 = tariff("v1", "2026-01-01", effective_to="2026-01-01")
        self.v2 = tariff("v2", "2026-01-02", effective_to="2026-01-02", supply=2.0, peak=0.8, overnight=0.2, off_peak=0.4)
        self.v3 = tariff("v3", "2026-01-04", supply=3.0, peak=0.9, overnight=0.3, off_peak=0.5)
        events = [
            HouseholdTimelineEvent.tariff_period(self.v1, effective_from=date(2026, 1, 1), effective_to=date(2026, 1, 1), event_id="v1"),
            HouseholdTimelineEvent.tariff_period(self.v2, effective_from=date(2026, 1, 2), effective_to=date(2026, 1, 2), event_id="v2"),
            HouseholdTimelineEvent.tariff_period(self.v3, effective_from=date(2026, 1, 4), event_id="v3"),
        ]
        timeline = HouseholdTimeline(events=events)
        fallback = TariffResolver(TariffVersionStore([self.v1, self.v2, self.v3]), default_provider="Synergy", default_tariff_name="Synergy EV Add On")
        self.resolver = HouseholdTariffResolver(timeline, fallback)

    def tearDown(self):
        self.connection.close()

    def add_reading(self, timestamp, kwh, bucket):
        self.connection.execute(
            """
            INSERT INTO readings (
                household_id, measurement_time, tariff_bucket, production_kwh,
                solar_to_home_kwh, solar_to_grid_kwh, grid_to_home_w,
                grid_to_home_kwh, import_cost
            ) VALUES ('default_household', ?, ?, 0, 0, 0, ?, ?, 999)
            """,
            (timestamp, bucket, kwh * 4 * 1000, kwh),
        )

    def add_session(self, transaction_id, start, end, energy):
        self.connection.execute(
            """
            INSERT INTO ev_sessions (
                household_id, transaction_id, start_time, end_time,
                energy_kwh, source, cost_estimate
            ) VALUES ('default_household', ?, ?, ?, ?, 'ocular_export', 999)
            """,
            (transaction_id, start, end, energy),
        )

    def service(self):
        self.connection.commit()
        return PeriodSummaryService(connection=self.connection, tariff_resolver=self.resolver)

    def test_household_period_cost_supply_and_daily_aggregation(self):
        self.add_reading("2026-01-01T23:45:00", 1.0, "peak")
        self.add_reading("2026-01-02T00:00:00", 2.0, "overnight")

        summary = self.service().get_period_summary(date(2026, 1, 1), date(2026, 1, 2))

        self.assertEqual(summary["household"]["grid_import_kwh"], 3.0)
        self.assertAlmostEqual(summary["household"]["cost"], 3.9)
        self.assertEqual([day["date"] for day in summary["household"]["daily"]], ["2026-01-01", "2026-01-02"])
        self.assertAlmostEqual(summary["household"]["daily"][0]["cost"], 1.5)
        self.assertAlmostEqual(summary["household"]["daily"][1]["cost"], 2.4)
        self.assertEqual([item["version"] for item in summary["tariff"]["versions_used"]], ["v1", "v2"])

    def test_ev_is_repriced_across_windows_and_tariff_versions(self):
        self.add_session("cross", "2026-01-01T22:00:00", "2026-01-02T02:00:00", 8.0)

        summary = self.service().get_period_summary(date(2026, 1, 1), date(2026, 1, 2))

        # 2 kWh at v1 off-peak, 2 kWh at v1 overnight, and 4 kWh at v2 overnight.
        self.assertAlmostEqual(summary["ev"]["energy_kwh"], 8.0)
        self.assertAlmostEqual(summary["ev"]["cost"], 1.6)
        self.assertEqual(summary["ev"]["sessions"][0]["partial_allocation"], False)

    def test_partial_session_is_proportionally_allocated(self):
        self.add_session("partial", "2026-01-01T23:00:00", "2026-01-02T02:00:00", 6.0)

        summary = self.service().get_period_summary("2026-01-02T00:00:00", "2026-01-02T01:00:00")

        self.assertAlmostEqual(summary["ev"]["energy_kwh"], 2.0)
        self.assertAlmostEqual(summary["ev"]["cost"], 0.4)
        self.assertTrue(summary["ev"]["sessions"][0]["partial_allocation"])

    def test_percentage_zero_import_and_missing_household_data(self):
        self.add_session("zero", "2026-01-01T10:00:00", "2026-01-01T11:00:00", 2.0)
        empty = self.service().get_period_summary(date(2026, 1, 1), date(2026, 1, 1))
        self.assertIsNone(empty["ev"]["percentage_of_household"])
        self.assertIsNone(empty["household"]["cost"])

        self.add_reading("2026-01-01T10:00:00", 0.0, "off_peak")
        zero = self.service().get_period_summary(date(2026, 1, 1), date(2026, 1, 1))
        self.assertIsNone(zero["ev"]["percentage_of_household"])
        self.assertTrue(any("zero" in warning for warning in zero["tariff"]["warnings"]))

    def test_missing_tariff_keeps_energy_but_withholds_pricing(self):
        self.add_reading("2026-01-03T12:00:00", 4.0, "off_peak")
        self.add_session("gap", "2026-01-03T12:00:00", "2026-01-03T13:00:00", 3.0)

        summary = self.service().get_period_summary(date(2026, 1, 3), date(2026, 1, 3))

        self.assertEqual(summary["household"]["grid_import_kwh"], 4.0)
        self.assertIsNone(summary["household"]["cost"])
        self.assertEqual(summary["ev"]["energy_kwh"], 3.0)
        self.assertIsNone(summary["ev"]["cost"])
        self.assertFalse(summary["tariff"]["coverage_complete"])
        self.assertTrue(any("does not cover" in warning for warning in summary["tariff"]["warnings"]))

    def test_month_boundary_is_returned_as_separate_daily_rows(self):
        self.add_reading("2026-01-31T23:45:00", 1.0, "overnight")
        self.add_reading("2026-02-01T00:00:00", 1.5, "overnight")
        v_jan = tariff("jan", "2026-01-31", effective_to="2026-01-31")
        v_feb = tariff("feb", "2026-02-01", supply=2.0)
        timeline = HouseholdTimeline(events=[
            HouseholdTimelineEvent.tariff_period(v_jan, effective_from=date(2026, 1, 31), effective_to=date(2026, 1, 31), event_id="jan"),
            HouseholdTimelineEvent.tariff_period(v_feb, effective_from=date(2026, 2, 1), event_id="feb"),
        ])
        self.resolver = HouseholdTariffResolver(
            timeline,
            TariffResolver(TariffVersionStore([v_jan, v_feb]), default_provider="Synergy", default_tariff_name="Synergy EV Add On"),
        )
        summary = self.service().get_period_summary(date(2026, 1, 31), date(2026, 2, 1))

        self.assertEqual(summary["household"]["grid_import_kwh"], 2.5)
        self.assertEqual([row["date"] for row in summary["household"]["daily"]], ["2026-01-31", "2026-02-01"])


if __name__ == "__main__":
    unittest.main()
