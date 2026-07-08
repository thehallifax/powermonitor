import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.historical_trends_engine import HistoricalTrendsEngine
from engine.models import DailyBriefing, FinancialImpact


def history(days, *, solar=True, exports=True, cost_step=1.0, grid_step=1.0):
    start = date(2026, 6, 1)
    rows = []
    for index in range(days):
        solar_value = 8.0 + index * 0.2 if solar else 0.0
        export_value = 2.0 + index * 0.05 if exports else 0.0
        rows.append(
            {
                "day": start + timedelta(days=index),
                "net_cost": 10.0 + index * cost_step,
                "grid_import_kwh": 18.0 + index * grid_step,
                "solar_generated_kwh": solar_value,
                "solar_exported_kwh": export_value,
            }
        )
    return rows


def build_trends(rows, as_of=None):
    as_of = as_of or date(2026, 7, 1)
    return HistoricalTrendsEngine().build(rows, as_of=as_of)


def empty_briefing():
    return DailyBriefing(
        greeting="Good morning.",
        overall_household_status="Everything looks normal.",
        overall_confidence="Medium",
        overall_freshness="Recent household data",
        top_priorities=[],
        positive_outcomes=[],
        items_to_watch=[],
        suggested_next_action="Everything looks normal.",
        estimated_savings_opportunity=FinancialImpact(),
        advisor_summary=[],
        read_time_minutes=1,
    )


class HistoricalTrendsHomeTests(unittest.TestCase):
    def test_enough_history_renders_compact_cards(self):
        html = report.historical_trends_home_html(build_trends(history(30)))

        self.assertIn("Historical trends", html)
        self.assertIn("Yesterday cost", html)
        self.assertIn("Grid import", html)
        self.assertIn("Solar generation", html)
        self.assertLessEqual(html.count("home-historical-trend-card"), 6)

    def test_insufficient_history_hides_section(self):
        html = report.historical_trends_home_html(build_trends(history(2)))

        self.assertEqual(html, "")

    def test_no_solar_hides_solar_cards(self):
        html = report.historical_trends_home_html(build_trends(history(30, solar=False, exports=False)))

        self.assertIn("Yesterday cost", html)
        self.assertIn("Grid import", html)
        self.assertNotIn("Solar generation", html)
        self.assertNotIn("Solar used at home", html)

    def test_no_exports_hides_export_card(self):
        html = report.historical_trends_home_html(build_trends(history(30, exports=False)))

        self.assertIn("Solar generation", html)
        self.assertNotIn("Solar exports", html)

    def test_partial_history_shows_week_comparisons_not_month_comparisons(self):
        rows = history(7)
        html = report.historical_trends_home_html(build_trends(rows, as_of=date(2026, 6, 8)))

        self.assertIn("7-day average", html)
        self.assertNotIn("30-day average", html)

    def test_direction_indicators_are_homeowner_friendly(self):
        improving = report.historical_trends_home_html(
            build_trends(history(7, cost_step=-1.0, grid_step=-1.0), as_of=date(2026, 6, 8))
        )
        worsening = report.historical_trends_home_html(
            build_trends(history(7, cost_step=1.0, grid_step=1.0), as_of=date(2026, 6, 8))
        )
        unchanged = report.historical_trends_home_html(
            build_trends(history(7, cost_step=0.0, grid_step=0.0), as_of=date(2026, 6, 8))
        )

        self.assertIn("▲ Improving", improving)
        self.assertIn("▼ Worsening", worsening)
        self.assertIn("→ Unchanged", unchanged)

    def test_percentage_formatting_uses_yesterday_against_average(self):
        rows = [
            {"day": date(2026, 6, day), "net_cost": 10.0, "grid_import_kwh": 10.0}
            for day in range(1, 7)
        ]
        rows.append({"day": date(2026, 6, 7), "net_cost": 12.0, "grid_import_kwh": 8.0})

        html = report.historical_trends_home_html(build_trends(rows, as_of=date(2026, 6, 8)))

        self.assertIn("17% above your 7-day average", html)
        self.assertIn("18% below your 7-day average", html)

    def test_home_places_historical_trends_after_bill_forecast_before_quick_links(self):
        trends = build_trends(history(30))
        html = report.home_page_html(empty_briefing(), historical_trends=trends)

        bill_index = html.index("Bill forecast")
        trends_index = html.index("Historical trends")
        links_index = html.index("Quick links")
        self.assertLess(bill_index, trends_index)
        self.assertLess(trends_index, links_index)


if __name__ == "__main__":
    unittest.main()
