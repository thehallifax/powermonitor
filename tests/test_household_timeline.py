import json
import io
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
import web
from engine.financial_engine import FinancialEngine
from engine.household_timeline import (
    HouseholdTimeline,
    HouseholdTimelineError,
    HouseholdTimelineEvent,
    HouseholdTimelineStore,
    household_tariff_resolver,
    migrate_timeline_from_settings,
    tariff_version_for_plan,
    tariff_version_from_catalog_or_manual,
)


def settings():
    return SimpleNamespace(
        current_plan="synergy_ev_add_on",
        retailer="Synergy",
        retailer_id="synergy",
        supply_charge_per_day=1.20,
        export_credit_rate=0.02,
        ev_baseline_watts=0.0,
        ev_data_source="estimated",
        import_rates={"super_off_peak": 0.08, "overnight": 0.19, "off_peak": 0.24, "peak": 0.54},
        comparison_plan="previous_flat_or_home_plan",
        comparison_plan_rates={"flat_import_rate": 0.30, "supply_charge_per_day": 1.20, "export_credit_rate": 0.02},
    )


def mixed_timeline():
    flat = tariff_version_for_plan("synergy_home_plan", date(2026, 4, 1), date(2026, 4, 30))
    ev = tariff_version_for_plan("synergy_ev_add_on", date(2026, 5, 1), None)
    return HouseholdTimeline(
        events=[
            HouseholdTimelineEvent.tariff_period(flat, effective_from=date(2026, 4, 1), effective_to=date(2026, 4, 30), event_id="flat"),
            HouseholdTimelineEvent.tariff_period(ev, effective_from=date(2026, 5, 1), event_id="ev"),
        ]
    )


class HouseholdTimelineTests(unittest.TestCase):
    def test_one_open_current_tariff_migrates_from_settings(self):
        timeline = migrate_timeline_from_settings(settings())

        periods = timeline.tariff_periods()

        self.assertEqual(len(periods), 1)
        self.assertIsNone(periods[0].effective_to)
        self.assertEqual(periods[0].event_type, "tariff_period")
        self.assertEqual(periods[0].source, "inferred_current_settings")

    def test_flat_then_ev_tariff_resolution_uses_timeline_precedence(self):
        resolver = household_tariff_resolver(settings(), mixed_timeline())

        april = resolver.resolve_with_context(date(2026, 4, 30))
        may = resolver.resolve_with_context(date(2026, 5, 1))

        self.assertEqual(april.tariff.tariff_name, "Synergy Home Plan")
        self.assertEqual(may.tariff.tariff_name, "Synergy EV Add On")
        self.assertEqual(april.source, "household_timeline")
        self.assertEqual(may.source, "household_timeline")

    def test_overlapping_tariff_periods_are_rejected_and_adjacent_allowed(self):
        flat = tariff_version_for_plan("synergy_home_plan", date(2026, 4, 1), date(2026, 4, 30))
        ev = tariff_version_for_plan("synergy_ev_add_on", date(2026, 4, 30), None)

        with self.assertRaisesRegex(HouseholdTimelineError, "overlap"):
            HouseholdTimeline(
                events=[
                    HouseholdTimelineEvent.tariff_period(flat, effective_from=date(2026, 4, 1), effective_to=date(2026, 4, 30)),
                    HouseholdTimelineEvent.tariff_period(ev, effective_from=date(2026, 4, 30)),
                ]
            )

        adjacent = mixed_timeline()
        self.assertEqual(len(adjacent.tariff_periods()), 2)

    def test_open_ended_period_followed_by_another_period_is_rejected(self):
        flat = tariff_version_for_plan("synergy_home_plan", date(2026, 4, 1), None)
        ev = tariff_version_for_plan("synergy_ev_add_on", date(2026, 5, 1), None)

        with self.assertRaisesRegex(HouseholdTimelineError, "Open-ended"):
            HouseholdTimeline(
                events=[
                    HouseholdTimelineEvent.tariff_period(flat, effective_from=date(2026, 4, 1)),
                    HouseholdTimelineEvent.tariff_period(ev, effective_from=date(2026, 5, 1)),
                ]
            )

    def test_gaps_and_fallback_warnings_are_reported(self):
        flat = tariff_version_for_plan("synergy_home_plan", date(2026, 4, 1), date(2026, 4, 15))
        ev = tariff_version_for_plan("synergy_ev_add_on", date(2026, 5, 1), None)
        timeline = HouseholdTimeline(
            events=[
                HouseholdTimelineEvent.tariff_period(flat, effective_from=date(2026, 4, 1), effective_to=date(2026, 4, 15)),
                HouseholdTimelineEvent.tariff_period(ev, effective_from=date(2026, 5, 1)),
            ]
        )

        resolved = household_tariff_resolver(settings(), timeline).resolve_with_context(date(2026, 4, 20))

        self.assertIn("missing from 2026-04-16 to 2026-04-30", " ".join(timeline.warnings()))
        self.assertEqual(resolved.source, "tariff_version")
        self.assertIn("fallback", resolved.warning)

    def test_manual_historical_tariff_and_catalogue_historical_tariff_are_stored(self):
        manual = tariff_version_from_catalog_or_manual(
            provider="Synergy",
            tariff_name="Synergy flat/home plan",
            version="manual-2026-04",
            effective_from=date(2026, 4, 1),
            effective_to=date(2026, 4, 30),
            supply_charge_per_day=1.1,
            usage_rates={"super_off_peak": 0.30, "overnight": 0.30, "off_peak": 0.30, "peak": 0.30},
            export_credit_rate=0.02,
        )
        catalog = tariff_version_for_plan("synergy_ev_add_on", date(2026, 5, 1), None)

        timeline = HouseholdTimeline(
            events=[
                HouseholdTimelineEvent.tariff_period(manual, effective_from=date(2026, 4, 1), effective_to=date(2026, 4, 30)),
                HouseholdTimelineEvent.tariff_period(catalog, effective_from=date(2026, 5, 1)),
            ]
        )

        self.assertEqual(timeline.tariff_periods()[0].tariff_version().usage_rate("peak"), 0.30)
        self.assertEqual(timeline.tariff_periods()[1].tariff_version().tariff_name, "Synergy EV Add On")

    def test_store_preserves_audit_record_for_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "household_timeline.json"
            audit = Path(temp_dir) / "household_timeline_audit.jsonl"
            store = HouseholdTimelineStore(path, audit)
            timeline = mixed_timeline()

            store.save(timeline, action="test_save", previous=HouseholdTimeline(), reason="unit test")

            saved = json.loads(path.read_text(encoding="utf-8"))
            audit_record = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(len(saved["events"]), 2)
            self.assertEqual(audit_record["action"], "test_save")
            self.assertEqual(audit_record["reason"], "unit test")
            self.assertEqual(audit_record["affected_event_ids"], ["ev", "flat"])
            self.assertEqual(audit_record["source"], "settings")

    def test_interval_cost_before_and_after_tariff_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            timeline_path = Path(temp_dir) / "household_timeline.json"
            timeline_path.write_text(json.dumps(mixed_timeline().as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", timeline_path):
                with closing(sqlite3.connect(":memory:")) as conn, conn:
                    create_readings(conn)
                    report.data_engine(conn).validate_database()
                    summary = report.load_summary(conn, date(2026, 4, 30), date(2026, 5, 1), settings())

        flat = mixed_timeline().tariff_periods()[0].tariff_version()
        ev = mixed_timeline().tariff_periods()[1].tariff_version()
        self.assertAlmostEqual(summary.import_cost, flat.usage_rate("peak") + ev.usage_rate("peak"))
        self.assertAlmostEqual(summary.supply_cost, flat.supply_charge_per_day + ev.supply_charge_per_day)
        self.assertAlmostEqual(summary.export_credit, flat.primary_export_rate + ev.primary_export_rate)
        self.assertEqual(len(summary.tariff_resolution["periods_used"]), 2)

    def test_flat_rate_period_does_not_use_ev_cheap_grid_arbitrage(self):
        intervals = [
            {"measurement_time": "2026-04-30T12:00:00", "tariff_bucket": "super_off_peak", "import_rate": 0.30, "grid_import_kwh": 0.0, "solar_export_kwh": 0.0, "tariff_name": "Flat"},
            {"measurement_time": "2026-04-30T18:00:00", "tariff_bucket": "peak", "import_rate": 0.30, "grid_import_kwh": 5.0, "solar_export_kwh": 0.0, "tariff_name": "Flat"},
            {"measurement_time": "2026-05-01T12:00:00", "tariff_bucket": "super_off_peak", "import_rate": 0.08, "grid_import_kwh": 0.0, "solar_export_kwh": 0.0, "tariff_name": "EV Add On"},
            {"measurement_time": "2026-05-01T18:00:00", "tariff_bucket": "peak", "import_rate": 0.54, "grid_import_kwh": 5.0, "solar_export_kwh": 0.0, "tariff_name": "EV Add On"},
        ]

        comparison = FinancialEngine().compare_home_battery_sizes(
            intervals,
            settings(),
            baseline_bill=10.0,
            baseline_grid_import_kwh=10.0,
            baseline_solar_exported_kwh=0.0,
            capacities_kwh=(5.0,),
            tariff_version=None,
            include_seasonal=False,
        )

        daily = comparison["strategies"]["solar_plus_grid_arbitrage"]["options"][0]["daily_diagnostics"]
        by_day = {item["date"]: item for item in daily}
        self.assertAlmostEqual(by_day["2026-04-30"]["grid_energy_used_to_charge_battery_kwh"], 0.0)
        self.assertGreater(by_day["2026-05-01"]["grid_energy_used_to_charge_battery_kwh"], 0.0)
        self.assertEqual(len(comparison["tariff_periods_used"]), 2)

    def test_settings_timeline_presentation_is_read_only_until_action_selected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            timeline_path = Path(temp_dir) / "household_timeline.json"
            timeline_path.write_text(json.dumps(mixed_timeline().as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", timeline_path):
                markup = report.household_timeline_html(settings())

        self.assertIn("Home energy history", markup)
        self.assertIn("Current tariff", markup)
        self.assertIn("Previous tariff", markup)
        self.assertIn("Edit details", markup)
        self.assertIn("Change tariff", markup)
        self.assertIn("Edit previous tariff", markup)
        self.assertNotIn("End current period", markup)
        self.assertNotIn("Edit event", markup)
        self.assertIn("Add previous tariff", markup)
        self.assertIn("value=\"2026-04-30\"", markup)
        self.assertIn("Deleting this tariff period may cause PowerMonitor to use fallback rates", markup)
        self.assertIn("data-confirm", markup)

    def test_inferred_timeline_wording_is_neutral_but_diagnostics_keep_provenance(self):
        markup = report.household_timeline_html(settings())
        primary_markup = markup.split("Diagnostics", 1)[0]

        self.assertIn("Current settings", markup)
        self.assertNotIn("Created from the current configured tariff because no household tariff timeline exists yet.", primary_markup)
        self.assertIn("Created from current tariff settings.", primary_markup)
        self.assertIn("inferred_current_settings", markup)
        self.assertIn("Confirm timeline", markup)

    def test_edit_current_tariff_start_date_and_overlap_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "household_timeline.json"
            audit = Path(temp_dir) / "household_timeline_audit.jsonl"
            path.write_text(json.dumps(mixed_timeline().as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", path), patch.object(web, "regenerate_dashboard_report", lambda: None):
                run_timeline_post(
                    "edit",
                    {
                        "event_id": "ev",
                        "current_tariff": "1",
                        "effective_from": "2026-05-02",
                        "confidence": "high",
                        "notes": "Corrected current start",
                    },
                )
                updated = HouseholdTimelineStore(path, audit).load(settings())
                current = updated.get_event("ev")
                self.assertEqual(current.effective_from, date(2026, 5, 2))
                self.assertIsNone(current.effective_to)
                self.assertEqual(current.notes, "Corrected current start")

                with self.assertRaisesRegex(HouseholdTimelineError, "overlap"):
                    run_timeline_post(
                        "edit",
                        {
                            "event_id": "ev",
                            "current_tariff": "1",
                            "effective_from": "2026-04-30",
                            "confidence": "high",
                        },
                    )

    def test_replace_current_tariff_closes_old_period_and_creates_one_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "household_timeline.json"
            audit = Path(temp_dir) / "household_timeline_audit.jsonl"
            path.write_text(json.dumps(mixed_timeline().as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", path), patch.object(web, "regenerate_dashboard_report", lambda: None):
                run_timeline_post(
                    "replace-current",
                    {
                        "event_id": "ev",
                        "plan_id": "synergy_home_plan",
                        "effective_from": "2026-08-01",
                        "provider": "Synergy",
                        "notes": "Changed plan",
                    },
                )

            updated = HouseholdTimelineStore(path, audit).load(settings())
            old = updated.get_event("ev")
            open_periods = [event for event in updated.tariff_periods() if event.effective_to is None]
            self.assertEqual(old.effective_to, date(2026, 7, 31))
            self.assertEqual(len(open_periods), 1)
            self.assertEqual(open_periods[0].title, "Synergy Home Plan")
            audit_actions = [json.loads(line)["action"] for line in audit.read_text(encoding="utf-8").splitlines()]
            self.assertIn("replace_current_tariff", audit_actions)

    def test_previous_tariff_edit_replaces_stable_tariff_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "household_timeline.json"
            audit = Path(temp_dir) / "household_timeline_audit.jsonl"
            path.write_text(json.dumps(mixed_timeline().as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", path), patch.object(web, "regenerate_dashboard_report", lambda: None):
                run_timeline_post(
                    "edit",
                    {
                        "event_id": "flat",
                        "current_tariff": "0",
                        "plan_id": "synergy_ev_add_on",
                        "provider": "Synergy",
                        "effective_from": "2026-04-01",
                        "effective_to": "2026-04-30",
                        "confidence": "high",
                        "notes": "Corrected previous plan",
                    },
                )

            updated = HouseholdTimelineStore(path, audit).load(settings())
            previous = updated.get_event("flat")
            self.assertEqual(previous.title, "Synergy EV Add On")
            self.assertEqual(previous.tariff_version().tariff_name, "Synergy EV Add On")
            self.assertEqual(previous.notes, "Corrected previous plan")

    def test_confirm_inferred_timeline_removes_unresolved_primary_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "household_timeline.json"
            audit = Path(temp_dir) / "household_timeline_audit.jsonl"
            timeline = migrate_timeline_from_settings(settings())
            path.write_text(json.dumps(timeline.as_dict()), encoding="utf-8")
            with patch.object(report, "HOUSEHOLD_TIMELINE_PATH", path), patch.object(web, "regenerate_dashboard_report", lambda: None):
                run_timeline_post("confirm", {"event_id": "current_tariff"})

            confirmed = HouseholdTimelineStore(path, audit).load(settings()).get_event("current_tariff")
            self.assertEqual(confirmed.source, "manual_settings")
            self.assertEqual(confirmed.notes, "Created from current tariff settings.")

    def test_api_safe_summary_payload_includes_tariff_resolution(self):
        summary = report.Summary(
            start=date(2026, 4, 30),
            end=date(2026, 5, 1),
            supply_days=2,
            intervals=2,
            reading_days=2,
            grid_import_kwh=2.0,
            import_cost=1.0,
            supply_cost=2.0,
            total_cost=3.0,
            export_credit=0.1,
            net_cost=2.9,
            solar_generated_kwh=0.0,
            solar_used_kwh=0.0,
            solar_exported_kwh=0.0,
            self_consumption_pct=0.0,
            solar_export_pct=0.0,
            peak_cost_share_pct=100.0,
            ev_charging_kwh=0.0,
            ev_charging_cost=0.0,
            ev_charging_source="estimated",
            actual_ev_charging_kwh=0.0,
            actual_ev_charging_cost=0.0,
            actual_ev_tariff_breakdown={},
            actual_ev_cost_partial=False,
            ev_session_analytics={},
            average_daily_grid_import_kwh=1.0,
            average_daily_solar_generation_kwh=0.0,
            average_daily_net_cost=1.45,
            buckets={},
            tariff_resolution={"periods_used": [{"tariff": {"tariff_name": "Flat"}}], "fallback_intervals": 0},
        )

        payload = report.summary_to_dict(summary)

        self.assertIn("tariff_resolution", payload)
        self.assertEqual(payload["tariff_resolution"]["periods_used"][0]["tariff"]["tariff_name"], "Flat")


def run_timeline_post(action: str, params: dict[str, str]) -> None:
    body = urlencode(params).encode("utf-8")
    handler = web.DashboardHandler.__new__(web.DashboardHandler)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.redirect = lambda _location: None
    handler.update_household_timeline(f"/household-timeline/{action}")


def create_readings(conn) -> None:
    conn.execute(
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
    conn.executemany(
        """
        INSERT INTO readings (
            household_id,
            measurement_time,
            tariff_bucket,
            production_kwh,
            solar_to_home_kwh,
            solar_to_grid_kwh,
            grid_to_home_w,
            grid_to_home_kwh,
            import_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("default_household", "2026-04-30T18:00:00", "peak", 0.0, 0.0, 1.0, 4000.0, 1.0, 0.0),
            ("default_household", "2026-05-01T18:00:00", "peak", 0.0, 0.0, 1.0, 4000.0, 1.0, 0.0),
        ],
    )


if __name__ == "__main__":
    unittest.main()
