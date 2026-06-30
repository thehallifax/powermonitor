import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = PROJECT_ROOT / "collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report
from engine.models import DecisionTimeline, Goal, GoalProgress, GoalStatus, TimelineEntry
from engine.provider_framework import ProviderCapability
from presentation.glance import GlanceState


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
        self.assertIn("First-time import guide", html)
        self.assertIn("Preview before saving", html)
        self.assertIn("Troubleshooting uploads", html)
        self.assertIn("Ocular Excel file", html)
        self.assertIn("will not auto-refresh", html)
        self.assertIn('class="responsive-table"', html)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", html)

    def test_disclosure_panels_can_default_closed_or_open(self):
        closed = report.disclosure_html("Advanced Diagnostics", "Runtime details", "<p>Details</p>")
        opened = report.disclosure_html("Providers", "Connection status", "<p>Providers</p>", open_by_default=True)

        self.assertIn('<details class="disclosure-panel">', closed)
        self.assertNotIn("<details class=\"disclosure-panel\" open>", closed)
        self.assertIn('<details class="disclosure-panel" open>', opened)

    def test_provider_cards_answer_connection_health_update_and_next_step(self):
        provider = SimpleNamespace(
            provider_id="ocular_export",
            name="Ocular export",
            as_dict=lambda: {
                "lifecycle_state": "NOT_CONFIGURED",
                "capability_summary": {"capabilities": ["ev_sessions"]},
                "diagnostics": {"last_successful_sync": None},
                "health": {"collector_status": "not_configured"},
                "sync_state": "NEVER_SYNCED",
                "actions": ["configure"],
                "onboarding": {
                    "state": "NOT_CONFIGURED",
                    "state_label": "Not Configured",
                    "connected": False,
                    "healthy": False,
                    "data_available": False,
                    "last_successful_sync": None,
                    "next_scheduled_sync": "Next manual import",
                    "sync_frequency": "Manual import",
                    "required_information": ["A supported export file when you import history"],
                    "why_required": "Import files add history without requiring a live provider session.",
                    "connection_status": "Not configured yet.",
                    "health_status": "Waiting for setup.",
                    "next_step": "Select Ocular export, review the required details, then configure it.",
                    "common_failures": ["Wrong export file type"],
                    "recovery_steps": ["Export the file again from the provider"],
                    "technical_details": {"lifecycle_state": "NOT_CONFIGURED"},
                },
            },
        )
        manager = SimpleNamespace(providers=lambda: [provider])

        html = report.provider_management_html(manager)

        self.assertIn("Connected?", html)
        self.assertIn("Healthy?", html)
        self.assertIn("Last updated", html)
        self.assertIn("Data available?", html)
        self.assertIn("Sync frequency", html)
        self.assertIn("Next scheduled sync", html)
        self.assertIn("What should I do next?", html)
        self.assertIn("Setup and recovery", html)
        self.assertIn("Technical diagnostics", html)
        self.assertIn("Not yet", html)
        self.assertIn("provider-config-details", html)
        self.assertIn("<details class=\"disclosure-panel provider-details provider-config-details\" open>", html)

    def test_import_provider_cards_deep_link_to_provider_setup(self):
        provider = SimpleNamespace(
            provider_id="ocular_export",
            name="Ocular export",
            lifecycle_state=SimpleNamespace(value="NOT_CONFIGURED"),
            capability_summary=SimpleNamespace(capabilities=[ProviderCapability.IMPORT_ONLY]),
            note="Ocular EV charging history upload is supported.",
        )
        manager = SimpleNamespace(providers=lambda: [provider])

        html = report.imports_page_html(provider_management=manager)

        self.assertIn('href="#system-provider-ocular_export"', html)

    def test_setup_configuration_sections_have_deep_link_targets(self):
        html = report.billing_cycle_setup_html(
            SimpleNamespace(
                billing_period_start_day=6,
                retailer="Synergy",
                current_plan="synergy_ev_add_on",
            )
        )

        self.assertIn('id="setup-billing"', html)
        self.assertIn('id="setup-billing-cycle"', html)
        self.assertIn('id="setup-tariff"', html)

    def test_goals_and_glance_have_direct_action_targets(self):
        goal = GoalProgress(
            goal=Goal(
                goal_id="bill_target",
                title="Keep the bill predictable",
                outcome="Bill control",
                advisor_types=["COST"],
                recommendation_categories=["Bills"],
            ),
            progress_pct=50,
            status=GoalStatus("ON_TRACK", "On track", "Progress is on track."),
            confidence="High",
            summary="You are tracking close to the target.",
            supporting_advisors=["cost_advisor"],
            supporting_evidence=["Bill estimate"],
            recommended_action="Keep monitoring.",
        )
        goals_html = report.goals_progress_html([goal])
        glance_html = report.glance_page_html(
            GlanceState(
                status="WAIT",
                tone="wait",
                current_recommendation="Wait for the next lower-cost tariff window.",
                reason="A cheaper window is coming soon.",
                current_tariff="Peak",
                current_tariff_bucket="peak",
                next_window="9pm",
                lowest_cost_window="11pm",
                energy_score=82,
                energy_score_label="Good",
                bill_current=120.0,
                bill_projected=180.0,
                bill_projection_basis="Current run-rate",
                solar_status="Solar unavailable",
                freshness="Fresh",
                last_updated="2026-06-30T09:15:00+08:00",
                confidence="High",
                evidence_quality="HIGH",
                evidence_reason="Recent readings available.",
            )
        )

        self.assertIn('id="home-goals"', goals_html)
        self.assertIn('class="glance-return"', glance_html)
        self.assertIn("Return Home", glance_html)
        self.assertIn('href="#home"', glance_html)

    def test_report_shell_reveals_deep_hash_routes(self):
        html = inspect.getsource(report.write_html) + inspect.getsource(report.system_page_html)

        self.assertIn("body:has(#setup-tariff:target) #setup", html)
        self.assertIn("body:has(#system-live-data:target) #system", html)
        self.assertIn('id="system-live-data"', html)
        self.assertIn('id="system-data-quality"', html)
        self.assertIn("revealHashTarget", html)

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
