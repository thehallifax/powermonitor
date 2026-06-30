import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
from engine.models import DecisionTimeline, TimelineEntry


class MobilePresentationTests(unittest.TestCase):
    def test_navigation_includes_mobile_dashboard_shortcuts(self):
        html = report.nav_html()

        self.assertIn('class="mobile-shortcuts"', html)
        self.assertIn('href="#home"', html)
        self.assertIn('href="#glance"', html)
        self.assertIn('href="#setup"', html)

    def test_imports_page_uses_mobile_friendly_upload_copy(self):
        html = report.imports_page_html(history=[])

        self.assertIn("Choose an Ocular Excel export from your phone, tablet, or Mac.", html)
        self.assertIn("Ocular Excel file", html)
        self.assertIn("will not auto-refresh", html)
        self.assertIn('class="responsive-table"', html)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", html)

    def test_page_heading_includes_home_breadcrumb(self):
        html = report.page_heading_html("Can I trust the data?", "Connected sources and confidence.")

        self.assertIn('class="page-breadcrumb"', html)
        self.assertIn('href="#home"', html)
        self.assertIn("Can I trust the data?", html)

    def test_timeline_groups_events_with_readable_icons_and_types(self):
        html = report.timeline_html(
            DecisionTimeline(
                [
                    TimelineEntry(
                        timestamp="2026-06-30T09:15:00+08:00",
                        title="Billing period updated",
                        summary="The current billing period was refreshed.",
                        financial_impact="$12.00",
                        confidence="High",
                        evidence_sources=["Billing settings"],
                        event_type="billing_period_updated",
                    )
                ]
            )
        )

        self.assertIn("Tuesday, 30 Jun", html)
        self.assertIn('class="timeline-stamp"', html)
        self.assertIn("Billing Period Updated", html)
        self.assertNotIn("billing_period_updated", html)


if __name__ == "__main__":
    unittest.main()
