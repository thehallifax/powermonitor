import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
for path in (PROJECT_ROOT, COLLECTOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import report
from engine.historical_comparison import build_historical_comparison, resolve_historical_comparison


def period(start: date = date(2026, 7, 1), end: date = date(2026, 7, 10), kind: str = "custom"):
    label = "Current billing period" if kind == "current_billing_period" else "Custom period"
    return SimpleNamespace(kind=kind, label=label, start=start, end=end)


def settings():
    return SimpleNamespace(
        current_billing_period_start=date(2026, 7, 6),
        previous_billing_period_start=date(2026, 6, 6),
        previous_billing_period_end=date(2026, 7, 5),
    )


def summary(
    *,
    net_cost: float,
    import_cost: float,
    supply_cost: float,
    export_credit: float,
    grid_import_kwh: float,
    supply_days: int = 10,
    intervals: int = 960,
    reading_days: int = 10,
    solar_generated_kwh: float = 100.0,
    solar_used_kwh: float = 40.0,
    solar_exported_kwh: float = 60.0,
    ev_charging_kwh: float = 20.0,
    ev_charging_cost: float = 4.0,
    ev_charging_source: str = "ocular_export",
    tariff_resolution: Optional[dict] = None,
):
    average_daily_net_cost = net_cost / supply_days if supply_days else 0.0
    return report.Summary(
        start=date(2026, 7, 1),
        end=date(2026, 7, supply_days),
        net_cost=net_cost,
        import_cost=import_cost,
        supply_cost=supply_cost,
        total_cost=import_cost + supply_cost,
        export_credit=export_credit,
        grid_import_kwh=grid_import_kwh,
        supply_days=supply_days,
        intervals=intervals,
        reading_days=reading_days,
        solar_generated_kwh=solar_generated_kwh,
        solar_used_kwh=solar_used_kwh,
        solar_exported_kwh=solar_exported_kwh,
        self_consumption_pct=(solar_used_kwh / solar_generated_kwh * 100 if solar_generated_kwh else 0.0),
        solar_export_pct=(solar_exported_kwh / solar_generated_kwh * 100 if solar_generated_kwh else 0.0),
        peak_cost_share_pct=(import_cost * 0.55 / import_cost * 100 if import_cost else 0.0),
        ev_charging_kwh=ev_charging_kwh,
        ev_charging_cost=ev_charging_cost,
        ev_charging_source=ev_charging_source,
        actual_ev_charging_kwh=ev_charging_kwh,
        actual_ev_charging_cost=ev_charging_cost,
        actual_ev_tariff_breakdown={},
        actual_ev_cost_partial=False,
        ev_session_analytics={},
        average_daily_net_cost=average_daily_net_cost,
        average_daily_grid_import_kwh=grid_import_kwh / supply_days if supply_days else 0.0,
        average_daily_solar_generation_kwh=solar_generated_kwh / supply_days if supply_days else 0.0,
        buckets={
            "peak": {"kwh": grid_import_kwh * 0.45, "cost": import_cost * 0.55},
            "off_peak": {"kwh": grid_import_kwh * 0.35, "cost": import_cost * 0.30},
            "super_off_peak": {"kwh": grid_import_kwh * 0.20, "cost": import_cost * 0.15},
        },
        tariff_resolution=tariff_resolution or {
            "periods_used": [
                {
                    "tariff": {
                        "provider": "Synergy",
                        "tariff_name": "Synergy EV Add On",
                        "version": "2026-07-01",
                    },
                    "source": "household_timeline",
                    "interval_count": intervals,
                }
            ],
            "unresolved_intervals": 0,
            "fallback_intervals": 0,
            "confidence": "high",
            "warnings": [],
        },
    )


class HistoricalComparisonTests(unittest.TestCase):
    def test_resolves_current_and_previous_billing_periods(self):
        resolved = resolve_historical_comparison(
            "previous_billing_period",
            period(date(2026, 7, 6), date(2026, 7, 14), kind="current_billing_period"),
            today=date(2026, 7, 14),
            settings=settings(),
        )

        self.assertEqual(resolved["primary_period"]["start"], "2026-07-06")
        self.assertEqual(resolved["comparison_period"]["start"], "2026-06-06")
        self.assertEqual(resolved["comparison_period"]["end"], "2026-07-05")
        self.assertIn("partial", " ".join(resolved["warnings"]).lower())

    def test_default_previous_billing_comparison_is_safe_for_non_current_periods(self):
        resolved = resolve_historical_comparison(
            "previous_billing_period",
            period(date(2026, 6, 1), date(2026, 6, 7), kind="last_7_days"),
            today=date(2026, 7, 14),
            settings=settings(),
        )

        self.assertEqual(resolved["primary_period"]["start"], "2026-06-01")
        self.assertEqual(resolved["primary_period"]["end"], "2026-06-07")
        self.assertEqual(resolved["comparison_period"]["start"], "2026-05-25")
        self.assertEqual(resolved["comparison_period"]["end"], "2026-05-31")

    def test_resolves_calendar_and_rolling_comparison_types(self):
        today = date(2026, 7, 14)
        cases = {
            "same_month_last_year": ("2026-07-01", "2025-07-01", "2025-07-14"),
            "previous_year_to_date": ("2026-01-01", "2025-01-01", "2025-07-14"),
            "previous_rolling_12_months": ("2025-07-15", "2024-07-15", "2025-07-14"),
            "previous_equivalent_period": ("2026-07-01", "2026-06-21", "2026-06-30"),
            "same_period_last_year": ("2026-07-01", "2025-07-01", "2025-07-10"),
        }

        for kind, (primary_start, comparison_start, comparison_end) in cases.items():
            with self.subTest(kind=kind):
                resolved = resolve_historical_comparison(kind, period(), today=today, settings=settings())
                self.assertEqual(resolved["primary_period"]["start"], primary_start)
                self.assertEqual(resolved["comparison_period"]["start"], comparison_start)
                self.assertEqual(resolved["comparison_period"]["end"], comparison_end)

    def test_metrics_changes_handle_zero_baseline_safely(self):
        resolved = resolve_historical_comparison(
            "previous_equivalent_period",
            period(),
            today=date(2026, 7, 14),
            settings=settings(),
        )
        payload = build_historical_comparison(
            resolved,
            summary(net_cost=50, import_cost=40, supply_cost=12, export_credit=2, grid_import_kwh=100),
            summary(net_cost=0, import_cost=0, supply_cost=0, export_credit=0, grid_import_kwh=0),
        )

        self.assertIsNone(payload["changes"]["net_estimated_cost"]["percent"])
        self.assertIn("historical_comparison", {"historical_comparison": payload})
        for key in ("comparison_type", "primary_period", "comparison_period", "metrics", "changes", "attribution", "tariffs_used", "confidence", "warnings"):
            self.assertIn(key, payload)

    def test_attribution_reconciles_and_ranks_largest_drivers(self):
        resolved = resolve_historical_comparison(
            "previous_equivalent_period",
            period(),
            today=date(2026, 7, 14),
            settings=settings(),
        )
        payload = build_historical_comparison(
            resolved,
            summary(net_cost=160, import_cost=120, supply_cost=18, export_credit=8, grid_import_kwh=240, ev_charging_cost=30),
            summary(net_cost=100, import_cost=80, supply_cost=12, export_credit=12, grid_import_kwh=200, ev_charging_cost=5),
        )
        amounts = [abs(item["amount"]) for item in payload["attribution"]["components"]]

        self.assertTrue(payload["attribution"]["reconciles"])
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        self.assertIn("EV charging change", [item["label"] for item in payload["attribution"]["components"]])

    def test_mixed_tariffs_and_gaps_are_exposed(self):
        mixed_tariffs = {
            "periods_used": [
                {"tariff": {"tariff_name": "Previous Flat/Home Plan"}, "source": "timeline", "interval_count": 480},
                {"tariff": {"tariff_name": "Synergy EV Add On"}, "source": "timeline", "interval_count": 460},
            ],
            "unresolved_intervals": 20,
            "fallback_intervals": 10,
            "confidence": "medium",
            "warnings": ["20 intervals had no resolved tariff."],
        }
        resolved = resolve_historical_comparison(
            "previous_equivalent_period",
            period(),
            today=date(2026, 7, 14),
            settings=settings(),
        )
        payload = build_historical_comparison(
            resolved,
            summary(net_cost=120, import_cost=90, supply_cost=15, export_credit=5, grid_import_kwh=180, tariff_resolution=mixed_tariffs),
            summary(net_cost=100, import_cost=80, supply_cost=12, export_credit=8, grid_import_kwh=160),
        )

        primary_tariffs = payload["tariffs_used"]["primary_period"]
        self.assertTrue(primary_tariffs["tariff_changes"])
        self.assertEqual(primary_tariffs["unresolved_tariff_gaps"], 20)
        self.assertEqual(primary_tariffs["fallback_tariff_usage"], 10)
        self.assertIn("unresolved tariff gaps", " ".join(payload["confidence"]["warnings"]))

    def test_confidence_reflects_missing_solar_ev_and_period_length_mismatch(self):
        resolved = resolve_historical_comparison(
            "previous_billing_period",
            period(date(2026, 7, 6), date(2026, 7, 14), kind="current_billing_period"),
            today=date(2026, 7, 14),
            settings=settings(),
        )
        payload = build_historical_comparison(
            resolved,
            summary(net_cost=70, import_cost=60, supply_cost=12, export_credit=2, grid_import_kwh=120, intervals=200, reading_days=3, solar_generated_kwh=0, ev_charging_source="estimated"),
            summary(net_cost=65, import_cost=55, supply_cost=12, export_credit=2, grid_import_kwh=100, supply_days=30, intervals=500, reading_days=10, solar_generated_kwh=0, ev_charging_source="estimated"),
        )

        self.assertIn(payload["confidence"]["level"], {"Low", "Insufficient data"})
        warning_text = " ".join(payload["confidence"]["warnings"]).lower()
        self.assertIn("interval coverage", warning_text)
        self.assertIn("solar", warning_text)
        self.assertIn("different lengths", warning_text)

    def test_presentation_retains_selector_and_uses_mobile_safe_table(self):
        selected = report.Period("custom", "Custom", date(2026, 7, 1), date(2026, 7, 10))
        controls = report.period_controls_html(selected, comparison_type="same_period_last_year")
        resolved = resolve_historical_comparison(
            "same_period_last_year",
            period(),
            today=date(2026, 7, 14),
            settings=settings(),
        )
        payload = build_historical_comparison(
            resolved,
            summary(net_cost=120, import_cost=90, supply_cost=15, export_credit=5, grid_import_kwh=180),
            summary(net_cost=100, import_cost=80, supply_cost=12, export_credit=8, grid_import_kwh=160),
        )
        html = report.historical_comparison_html(payload)

        self.assertIn('name="comparison_type"', controls)
        self.assertIn('value="same_period_last_year" selected', controls)
        self.assertIn("historical-comparison-table-wrap", html)
        self.assertIn("responsive-table historical-comparison-table", html)
        self.assertIn("Technical reconciliation", html)

    def test_existing_bill_comparison_contract_is_unchanged(self):
        current = summary(net_cost=120, import_cost=90, supply_cost=15, export_credit=5, grid_import_kwh=180)
        previous = summary(net_cost=100, import_cost=80, supply_cost=12, export_credit=8, grid_import_kwh=160)
        payload = report.compare_summaries(
            report.Period("custom", "Custom", date(2026, 7, 1), date(2026, 7, 10)),
            current,
            report.Period("previous_equivalent_period", "Previous equivalent", date(2026, 6, 21), date(2026, 6, 30)),
            previous,
        )

        self.assertEqual(set(payload), {"period_a", "period_b", "summary_a", "summary_b", "deltas"})


if __name__ == "__main__":
    unittest.main()
