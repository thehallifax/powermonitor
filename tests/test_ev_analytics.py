import sys
import unittest
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report


class Settings:
    supply_charge_per_day = 1.0
    export_credit_rate = 0.02
    ev_baseline_watts = 0.0
    ev_data_source = "ocular_export"
    import_rates = {
        "super_off_peak": 0.08,
        "overnight": 0.19,
        "off_peak": 0.23,
        "peak": 0.54,
    }


def session(
    transaction_id,
    start_time,
    end_time,
    duration,
    *,
    energy=10.0,
    cost=2.0,
    max_power=None,
    max_power_source="unavailable",
):
    breakdown = {bucket: {"kwh": 0.0, "cost": 0.0} for bucket in report.BUCKET_ORDER}
    breakdown["overnight"] = {"kwh": energy, "cost": cost}
    return {
        "transaction_id": transaction_id,
        "date": start_time[:10],
        "start_time": start_time,
        "end_time": end_time,
        "start_time_display": start_time[11:16],
        "end_time_display": end_time[11:16],
        "duration_minutes": duration,
        "duration_display": report.format_duration(duration),
        "energy_kwh": energy,
        "cost": cost,
        "cost_per_kwh": cost / energy,
        "source": "ocular_export",
        "cost_partial": False,
        "max_power_kw": max_power,
        "max_power_source": max_power_source,
        "dominant_tariff": "overnight",
        "dominant_tariff_label": "11pm-6am overnight",
        "tariff_breakdown": breakdown,
    }


class EvAnalyticsTests(unittest.TestCase):
    def test_average_clock_time_wraps_around_midnight(self):
        analytics = report.ev_session_analytics(
            [
                session("a", "2026-06-27T23:40:00+08:00", "2026-06-28T23:50:00+08:00", 10),
                session("b", "2026-06-28T00:10:00+08:00", "2026-06-28T00:20:00+08:00", 10),
            ],
            Settings(),
        )

        self.assertEqual(analytics["insights"]["average_start_time"], "23:55")
        self.assertEqual(analytics["insights"]["average_finish_time"], "00:05")

    def test_highest_power_requires_source_supplied_power(self):
        analytics = report.ev_session_analytics(
            [
                session("a", "2026-06-27T10:00:00+08:00", "2026-06-27T12:00:00+08:00", 120, energy=22),
                session("b", "2026-06-27T13:00:00+08:00", "2026-06-27T13:30:00+08:00", 30, energy=6),
            ],
            Settings(),
        )

        self.assertIsNone(analytics["insights"]["highest_power_session"])
        self.assertFalse(analytics["insights"]["highest_power_available"])

    def test_longest_and_average_duration_use_reliable_duration_values(self):
        analytics = report.ev_session_analytics(
            [
                session("short", "2026-06-27T10:00:00+08:00", "2026-06-27T11:00:00+08:00", 60),
                session("long", "2026-06-27T12:00:00+08:00", "2026-06-27T15:00:00+08:00", 180, max_power=7.2, max_power_source="provided"),
            ],
            Settings(),
        )

        self.assertEqual(analytics["insights"]["longest_session"]["transaction_id"], "long")
        self.assertEqual(analytics["insights"]["average_duration_minutes"], 120)
        self.assertEqual(analytics["insights"]["highest_power_session"]["max_power_kw"], 7.2)

    def test_completed_historical_ocular_session_counts_as_actual(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn, "2026-07-04T23:30:00", grid_to_home_w=5000.0)
            create_ev_sessions(conn)
            insert_ev_session(conn, "hist-1", "2026-07-04T20:00:00", "2026-07-04T22:00:00", 18.0)
            report.data_engine(conn).validate_database()

            summary = report.load_summary(conn, date(2026, 7, 4), date(2026, 7, 4), Settings())

        self.assertEqual(summary.ev_charging_source, "ocular_export")
        self.assertAlmostEqual(summary.actual_ev_charging_kwh, 18.0)
        self.assertTrue(summary.ev_session_analytics["available"])

    def test_same_day_zero_ocular_session_is_ignored_for_actual_totals(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn, "2026-07-05T23:30:00", grid_to_home_w=5000.0)
            create_ev_sessions(conn)
            insert_ev_session(conn, "today-zero", "2026-07-05T10:00:00", "2026-07-05T10:00:00", 0.0)
            report.data_engine(conn).validate_database()

            summary = report.load_summary(conn, date(2026, 7, 5), date(2026, 7, 5), Settings())

        self.assertEqual(summary.ev_charging_source, "estimated")
        self.assertEqual(summary.actual_ev_charging_kwh, 0.0)
        self.assertFalse(summary.ev_session_analytics["available"])
        self.assertEqual(summary.ev_session_analytics["ignored_incomplete_sessions"], 1)

    def test_fallback_and_actual_labels_are_not_shown_together_for_today_zero_session(self):
        today = report.local_today()
        day_text = today.isoformat()
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn, f"{day_text}T23:30:00", grid_to_home_w=5000.0)
            create_ev_sessions(conn)
            insert_ev_session(conn, "today-zero", f"{day_text}T10:00:00", f"{day_text}T10:00:00", 0.0)
            report.data_engine(conn).validate_database()
            summary = report.load_summary(conn, today, today, Settings())

        html = report.ev_page_html(summary, Settings())

        self.assertIn("Using estimated EV fallback", html)
        self.assertIn("No completed charger sessions imported for today yet", html)
        self.assertNotIn("<h2>Actual charging sessions</h2>", html)
        self.assertNotIn("<h3>Actual EV charging</h3>", html)

    def test_mixed_period_uses_completed_actuals_and_mentions_ignored_sessions(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_readings(conn, "2026-07-04T23:30:00", grid_to_home_w=5000.0)
            create_readings(conn, "2026-07-05T23:30:00", grid_to_home_w=5000.0)
            create_ev_sessions(conn)
            insert_ev_session(conn, "hist-1", "2026-07-04T20:00:00", "2026-07-04T22:00:00", 18.0)
            insert_ev_session(conn, "today-zero", "2026-07-05T10:00:00", "2026-07-05T10:00:00", 0.0)
            report.data_engine(conn).validate_database()

            summary = report.load_summary(conn, date(2026, 7, 4), date(2026, 7, 5), Settings())

        html = report.ev_page_html(summary, Settings())

        self.assertEqual(summary.ev_charging_source, "ocular_export")
        self.assertAlmostEqual(summary.actual_ev_charging_kwh, 18.0)
        self.assertEqual(summary.ev_session_analytics["ignored_incomplete_sessions"], 1)
        self.assertIn("Actual charging sessions", html)
        self.assertIn("zero-kWh or incomplete charger session ignored", html)

    def test_history_actual_ev_charging_uses_same_completed_sessions_as_ev_page(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_ev_sessions(conn)
            for day in range(10, 15):
                create_readings(conn, f"2026-07-{day:02d}T23:30:00", grid_to_home_w=5000.0)
            insert_ev_session(conn, "jul-10", "2026-07-10T20:00:00", "2026-07-10T22:00:00", 18.0)
            report.data_engine(conn).validate_database()

            summary = report.load_summary(conn, date(2026, 7, 10), date(2026, 7, 14), Settings())
            trends = report.load_daily_trends(conn, date(2026, 7, 10), date(2026, 7, 14), Settings())

        session_dates = [session["date"] for session in summary.ev_session_analytics["sessions"]]
        trend_dates = [
            trend.day.isoformat()
            for trend in trends
            if trend.ev_charging_source in {"ocular_export", "home_assistant_ocpp"} and trend.ev_charging_kwh > 0
        ]

        self.assertEqual(session_dates, ["2026-07-10"])
        self.assertEqual(trend_dates, ["2026-07-10"])
        self.assertAlmostEqual(
            sum(session["energy_kwh"] for session in summary.ev_session_analytics["sessions"]),
            sum(trend.ev_charging_kwh for trend in trends if trend.ev_charging_kwh > 0),
        )
        self.assertEqual([trend.ev_charging_kwh for trend in trends[1:]], [0.0, 0.0, 0.0, 0.0])

    def test_historical_session_import_updates_ev_page_and_history_dates_totals_and_ordering(self):
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            create_ev_sessions(conn)
            for day in range(10, 15):
                create_readings(conn, f"2026-07-{day:02d}T23:30:00", grid_to_home_w=5000.0)
                insert_ev_session(
                    conn,
                    f"jul-{day}",
                    f"2026-07-{day:02d}T20:00:00",
                    f"2026-07-{day:02d}T22:00:00",
                    float(day),
                )
            report.data_engine(conn).validate_database()

            summary = report.load_summary(conn, date(2026, 7, 10), date(2026, 7, 14), Settings())
            trends = report.load_daily_trends(conn, date(2026, 7, 10), date(2026, 7, 14), Settings())

        sessions = summary.ev_session_analytics["sessions"]
        session_dates_latest_first = [session["date"] for session in sessions]
        trend_dates_chronological = [
            trend.day.isoformat()
            for trend in trends
            if trend.ev_charging_source == "ocular_export" and trend.ev_charging_kwh > 0
        ]

        self.assertEqual(session_dates_latest_first, ["2026-07-14", "2026-07-13", "2026-07-12", "2026-07-11", "2026-07-10"])
        self.assertEqual(trend_dates_chronological, ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13", "2026-07-14"])
        self.assertEqual(list(reversed(trend_dates_chronological)), session_dates_latest_first)
        self.assertAlmostEqual(
            sum(session["energy_kwh"] for session in sessions),
            sum(trend.ev_charging_kwh for trend in trends),
        )


if __name__ == "__main__":
    unittest.main()


def create_readings(conn, measurement_time: str, grid_to_home_w: float = 0.0) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
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
    conn.execute(
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
        ("default_household", measurement_time, "overnight", 0.0, 0.0, 0.0, grid_to_home_w, grid_to_home_w * 0.25 / 1000.0, 0.0),
    )


def create_ev_sessions(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_sessions (
            household_id TEXT DEFAULT 'default_household',
            start_time TEXT NOT NULL,
            end_time TEXT,
            last_seen TEXT,
            energy_kwh REAL NOT NULL,
            source TEXT NOT NULL,
            cost_estimate REAL,
            transaction_id TEXT,
            cost_partial INTEGER DEFAULT 0,
            duration_minutes REAL,
            overnight_kwh REAL DEFAULT 0,
            overnight_cost REAL DEFAULT 0,
            off_peak_kwh REAL DEFAULT 0,
            off_peak_cost REAL DEFAULT 0,
            super_off_peak_kwh REAL DEFAULT 0,
            super_off_peak_cost REAL DEFAULT 0,
            peak_kwh REAL DEFAULT 0,
            peak_cost REAL DEFAULT 0
        )
        """
    )


def insert_ev_session(conn, transaction_id: str, start_time: str, end_time: str, energy_kwh: float) -> None:
    conn.execute(
        """
        INSERT INTO ev_sessions (
            household_id,
            start_time,
            end_time,
            last_seen,
            energy_kwh,
            source,
            cost_estimate,
            transaction_id,
            duration_minutes,
            overnight_kwh,
            overnight_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "default_household",
            start_time,
            end_time,
            end_time,
            energy_kwh,
            "ocular_export",
            energy_kwh * Settings.import_rates["overnight"],
            transaction_id,
            120.0 if energy_kwh else 0.0,
            energy_kwh,
            energy_kwh * Settings.import_rates["overnight"],
        ),
    )
