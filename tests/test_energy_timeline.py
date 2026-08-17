import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.data_engine import DataEngine
from engine.energy_timeline import EnergyTimeline, EnergyTimelineEngine, EnergyTimelineEvent


def settings(**overrides):
    data = {
        "ev_baseline_watts": 500.0,
        "ev_data_source": "estimated",
        "import_rates": {
            "super_off_peak": 0.08,
            "overnight": 0.18,
            "off_peak": 0.23,
            "peak": 0.54,
        },
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def reading(
    time_text,
    *,
    bucket="off_peak",
    production=0.0,
    solar_home=0.0,
    export=0.0,
    grid_w=300.0,
    grid_kwh=0.05,
):
    return {
        "measurement_time": time_text,
        "tariff_bucket": bucket,
        "production_kwh": production,
        "solar_to_home_kwh": solar_home,
        "solar_to_grid_kwh": export,
        "grid_to_home_w": grid_w,
        "grid_to_home_kwh": grid_kwh,
    }


class EnergyTimelineEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = EnergyTimelineEngine()
        self.day = date(2026, 7, 7)

    def test_solar_start_end_and_peak_production_detection(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[
                reading("2026-07-07T06:30:00+08:00"),
                reading("2026-07-07T06:45:00+08:00", production=0.05, solar_home=0.04),
                reading("2026-07-07T12:00:00+08:00", production=1.25, solar_home=0.6, export=0.4),
                reading("2026-07-07T17:45:00+08:00", production=0.03, solar_home=0.02),
            ],
            settings=settings(),
            now=datetime(2026, 7, 7, 13, 0),
        )
        events = {event.event_type: event for event in timeline.events}

        self.assertEqual(events["solar_generation_started"].local_time, "06:45")
        self.assertEqual(events["peak_solar_production"].local_time, "12:00")
        self.assertEqual(events["solar_generation_ended"].local_time, "18:00")
        self.assertTrue(timeline.is_in_progress)

    def test_export_start_end_detection(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[
                reading("2026-07-07T09:00:00+08:00", production=0.3, solar_home=0.2),
                reading("2026-07-07T09:15:00+08:00", production=0.6, solar_home=0.2, export=0.25),
                reading("2026-07-07T10:00:00+08:00", production=0.5, solar_home=0.25, export=0.15),
            ],
            settings=settings(),
        )
        events = {event.event_type: event for event in timeline.events}

        self.assertEqual(events["export_to_grid_started"].local_time, "09:15")
        self.assertEqual(events["export_to_grid_ended"].local_time, "10:15")

    def test_tariff_event_detection(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[
                reading("2026-07-07T15:00:00+08:00", bucket="super_off_peak"),
                reading("2026-07-07T17:00:00+08:00", bucket="peak"),
            ],
            settings=settings(),
        )
        event_types = [event.event_type for event in timeline.events]

        self.assertIn("super_off_peak_tariff_started", event_types)
        self.assertIn("peak_tariff_started", event_types)

    def test_ev_actual_session_event(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[reading("2026-07-07T20:00:00+08:00", bucket="off_peak")],
            ev_sessions=[
                {
                    "start_time": "2026-07-07T21:10:00+08:00",
                    "end_time": "2026-07-07T23:30:00+08:00",
                    "energy_kwh": 18.5,
                    "source": "ocular_export",
                }
            ],
            settings=settings(ev_data_source="ocular_export"),
        )
        events = {event.event_type: event for event in timeline.events}

        self.assertEqual(events["ev_charging_session_started"].local_time, "21:10")
        self.assertEqual(events["ev_charging_ended"].local_time, "23:30")
        self.assertEqual(events["ev_charging_session_started"].confidence, "high")

    def test_ev_fallback_event(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[
                reading("2026-07-07T23:00:00+08:00", bucket="overnight", grid_w=4200.0, grid_kwh=1.05),
            ],
            settings=settings(ev_baseline_watts=500.0),
        )
        events = {event.event_type: event for event in timeline.events}

        self.assertEqual(events["ev_charging_likely_started"].local_time, "23:00")
        self.assertEqual(events["ev_charging_likely_started"].confidence, "low")

    def test_missing_data_produces_no_false_events(self):
        timeline = self.engine.build(day=self.day, readings=[], settings=settings())

        self.assertEqual(timeline.events, [])
        self.assertIn("No daily energy events", timeline.summary)

    def test_highest_usage_lowest_and_most_expensive_events(self):
        timeline = self.engine.build(
            day=self.day,
            readings=[
                reading("2026-07-07T10:00:00+08:00", bucket="super_off_peak", grid_kwh=0.2, grid_w=800),
                reading("2026-07-07T18:00:00+08:00", bucket="peak", grid_kwh=1.2, grid_w=4800),
            ],
            settings=settings(),
        )
        event_types = [event.event_type for event in timeline.events]

        self.assertIn("highest_usage_interval", event_types)
        self.assertIn("lowest_cost_period", event_types)
        self.assertIn("most_expensive_period", event_types)


class EnergyTimelinePresentationTests(unittest.TestCase):
    def test_home_presentation_caps_events(self):
        events = [
            EnergyTimelineEvent(
                event_type=f"event_{index}",
                local_time=f"0{index}:00",
                title=f"Event {index}",
                explanation="Something happened.",
                confidence="medium",
                sort_time=f"2026-07-07T0{index}:00:00",
            )
            for index in range(8)
        ]
        html = report.energy_timeline_home_html(
            EnergyTimeline(date="2026-07-07", is_in_progress=True, events=events, summary="Today is still in progress."),
            limit=6,
        )

        self.assertEqual(html.count('class="energy-timeline-card"'), 6)
        self.assertIn("Show 2 more events", html)
        self.assertIn("Energy timeline", html)

    def test_dashboard_summary_includes_energy_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "power.sqlite"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                create_readings(conn)
                insert_reading(conn, "2026-07-07T12:00:00", "super_off_peak", 1.0, 0.5, 0.2, 500.0, 0.1, "default_household")
                DataEngine(connection=conn).validate_database()
            with patch.object(report, "DB_PATH", db_path), patch.object(report, "REPORT_PATH", Path(temporary) / "index.html"):
                payload = report.dashboard_data(today=date(2026, 7, 7))

        self.assertIn("energy_timeline", payload)
        self.assertTrue(payload["energy_timeline"]["events"])


def create_readings(conn):
    conn.execute(
        """
        CREATE TABLE readings (
            measurement_time TEXT,
            tariff_bucket TEXT,
            production_kwh REAL,
            solar_to_home_kwh REAL,
            solar_to_grid_kwh REAL,
            grid_to_home_w REAL,
            grid_to_home_kwh REAL,
            import_cost REAL,
            household_id TEXT DEFAULT 'default_household'
        )
        """
    )


def insert_reading(conn, measurement_time, bucket, production, solar_home, export, grid_w, grid_kwh, household_id):
    conn.execute(
        """
        INSERT INTO readings (
            measurement_time, tariff_bucket, production_kwh, solar_to_home_kwh,
            solar_to_grid_kwh, grid_to_home_w, grid_to_home_kwh, import_cost, household_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (measurement_time, bucket, production, solar_home, export, grid_w, grid_kwh, 0.0, household_id),
    )


if __name__ == "__main__":
    unittest.main()
