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
from engine.insights_engine import HouseholdInsight
from engine.models import (
    BriefingPriority,
    DailyBriefing,
    DecisionTimeline,
    FinancialImpact,
    Goal,
    GoalProgress,
    GoalStatus,
    SetupChecklist,
    SetupProgress,
    SetupSection,
    SetupTask,
    SetupTaskPriority,
    TimelineEntry,
)
from engine.provider_framework import ProviderCapability
from engine.trends_engine import HouseholdRecord, HouseholdTrend, HouseholdTrendsReport
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

        self.assertIn("Choose an Ocular Excel export or Synergy PDF bill from your phone, tablet, or Mac.", html)
        self.assertIn("First-time import guide", html)
        self.assertIn("Preview before saving", html)
        self.assertIn("Troubleshooting uploads", html)
        self.assertIn("Ocular Excel or Synergy PDF", html)
        self.assertIn("will not auto-refresh", html)
        self.assertIn('class="responsive-table"', html)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", html)
        self.assertIn("application/pdf", html)

    def test_disclosure_panels_can_default_closed_or_open(self):
        closed = report.disclosure_html("Advanced Diagnostics", "Runtime details", "<p>Details</p>")
        opened = report.disclosure_html("Providers", "Connection status", "<p>Providers</p>", open_by_default=True)

        self.assertIn('<details class="disclosure-panel">', closed)
        self.assertNotIn("<details class=\"disclosure-panel\" open>", closed)
        self.assertIn('<details class="disclosure-panel" open>', opened)

    def test_import_data_source_card_uses_upload_actions(self):
        provider = SimpleNamespace(
            provider_id="ocular_export",
            name="Ocular export",
            as_dict=lambda: {
                "provider_id": "ocular_export",
                "provider_type": "ev_charger",
                "lifecycle_state": "CONFIGURED",
                "capability_summary": {"capabilities": ["ev_sessions", "import_only"]},
                "diagnostics": {"last_successful_sync": "2026-07-01T18:20:00+08:00"},
                "health": {"collector_status": "not_configured"},
                "sync_state": "SYNCED",
                "actions": ["configure"],
                "onboarding": {
                    "state": "READY_TO_CONNECT",
                    "state_label": "Historical Data Available",
                    "connected": False,
                    "healthy": True,
                    "data_available": True,
                    "last_successful_sync": "2026-07-01T18:20:00+08:00",
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

        self.assertIn("Your Data Sources", html)
        self.assertIn("EV charging history", html)
        self.assertIn("Status", html)
        self.assertIn("Latest upload available", html)
        self.assertIn("Provides", html)
        self.assertIn("Charging energy", html)
        self.assertIn("Charging costs", html)
        self.assertIn("Upload latest file", html)
        self.assertIn("Replace upload", html)
        self.assertIn("Remove imported history", html)
        self.assertIn("Setup help", html)
        self.assertIn("Advanced Diagnostics", html)
        self.assertNotIn("Connected?", html)
        self.assertNotIn("Provider Health", html)
        self.assertNotIn("Test Connection", html)
        self.assertNotIn("Reconnect", html)
        self.assertNotIn("provider-config-details", html)

    def test_live_data_source_card_uses_connection_actions_not_uploads(self):
        provider = SimpleNamespace(
            provider_id="solaredge",
            name="SolarEdge",
            as_dict=lambda: {
                "provider_id": "solaredge",
                "provider_type": "solar",
                "lifecycle_state": "CONNECTED",
                "capability_summary": {"capabilities": ["interval_history", "daily_summary"]},
                "diagnostics": {"last_successful_sync": "2026-07-02T20:35:00+08:00"},
                "health": {"collector_status": "ok"},
                "sync_state": "SYNCED",
                "actions": ["configure"],
                "configuration": {
                    "credentials": [
                        {"field": "username", "label": "Username/Email", "required": True, "sensitive": True, "configured": True},
                        {"field": "password", "label": "Password", "required": True, "sensitive": True, "configured": True},
                    ],
                    "entity_mapping": None,
                },
                "onboarding": {
                    "state": "CONNECTED",
                    "state_label": "Connected",
                    "connected": True,
                    "healthy": True,
                    "data_available": True,
                    "last_successful_sync": "2026-07-02T20:35:00+08:00",
                    "required_information": ["SolarEdge session"],
                    "why_required": "Solar access lets PowerMonitor explain generation.",
                    "recovery_steps": ["Run Test Connection"],
                    "health_status": "Healthy.",
                    "next_scheduled_sync": "2026-07-02T20:40:00+08:00",
                    "next_step": "No action needed right now.",
                    "technical_details": {"lifecycle_state": "CONNECTED", "sync_state": "SYNCED"},
                },
            },
        )

        html = report.provider_management_html(SimpleNamespace(providers=lambda: [provider]))

        self.assertIn("Solar generation", html)
        self.assertIn("Source", html)
        self.assertIn("SolarEdge", html)
        self.assertIn("Solar export", html)
        self.assertIn("Daily production", html)
        self.assertNotIn(">Connect<", html)
        self.assertIn("Test connection", html)
        self.assertIn("Reconnect", html)
        self.assertIn("Edit credentials", html)
        self.assertIn("Connected account", html)
        self.assertIn("Credentials", html)
        self.assertIn("Hidden for privacy", html)
        self.assertIn("Disconnect", html)
        self.assertIn('class="provider-config-form" data-provider-id="solaredge" method="post" action="/api/providers/solaredge/save"', html)
        self.assertIn('name="provider_id" value="solaredge"', html)
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertIn('name="provider_action" value="test" formaction="/api/providers/solaredge/test"', html)
        self.assertNotIn('action="/setup/billing-cycle"', html)
        self.assertIn("provider-config-form", html)
        self.assertIn("Last successful refresh", html)
        self.assertIn("Next refresh", html)
        self.assertIn("Data available", html)
        self.assertIn("Action needed", html)
        self.assertNotIn("Upload latest file", html)
        self.assertNotIn("Replace upload", html)
        before_advanced = html.split("Advanced Diagnostics", 1)[0]
        self.assertNotIn("Runtime state", before_advanced)
        self.assertNotIn("Sync state", before_advanced)
        self.assertNotIn("Lifecycle", before_advanced)
        self.assertNotIn("Observation count", before_advanced)

        flash_html = report.provider_management_html(
            SimpleNamespace(providers=lambda: [provider]),
            provider_flash={
                "provider_id": "solaredge",
                "action": "save",
                "status": "success",
                "message": "Settings saved. Now test the connection.",
                "next_step": "Run Test Connection before relying on this data source.",
            },
        )
        self.assertIn("provider-flash", flash_html)
        self.assertIn("Settings saved. Now test the connection.", flash_html)

    def test_billing_cycle_form_closes_before_provider_forms_begin(self):
        provider = SimpleNamespace(
            provider_id="solaredge",
            name="SolarEdge",
            as_dict=lambda: {
                "provider_id": "solaredge",
                "provider_type": "solar",
                "lifecycle_state": "CONFIGURED",
                "capability_summary": {"capabilities": ["interval_history", "daily_summary"]},
                "diagnostics": {},
                "health": {"collector_status": "unknown"},
                "sync_state": "NEVER_SYNCED",
                "actions": ["configure"],
                "configuration": {
                    "credentials": [
                        {"field": "username", "label": "Username/Email", "required": True, "sensitive": False, "configured": False},
                        {"field": "password", "label": "Password", "required": True, "sensitive": True, "configured": False},
                    ],
                    "entity_mapping": None,
                },
                "onboarding": {
                    "state": "READY_TO_CONNECT",
                    "state_label": "Waiting For Setup",
                    "connected": False,
                    "healthy": False,
                    "data_available": False,
                    "last_successful_sync": None,
                    "required_information": ["SolarEdge username/email", "SolarEdge password"],
                    "why_required": "Solar access lets PowerMonitor explain generation.",
                    "recovery_steps": ["Run Test Connection"],
                    "technical_details": {},
                },
            },
        )

        html = report.billing_cycle_setup_html(report.load_settings()) + report.provider_management_html(SimpleNamespace(providers=lambda: [provider]))
        billing_open = html.index('action="/setup/billing-cycle"')
        billing_close = html.index("</form>", billing_open)
        provider_form = html.index('class="provider-config-form"')

        self.assertLess(billing_close, provider_form)

    def test_smart_meter_source_is_not_fabricated(self):
        provider = SimpleNamespace(
            provider_id="smart_meter_readings",
            name="Smart meter readings",
            as_dict=lambda: {
                "provider_id": "smart_meter_readings",
                "provider_type": "smart_meter",
                "lifecycle_state": "CONFIGURED",
                "capability_summary": {"capabilities": ["interval_history"]},
                "diagnostics": {"last_successful_sync": "2026-07-02T20:35:00+08:00"},
                "health": {"collector_status": "ok"},
                "sync_state": "SYNCED",
                "actions": [],
                "onboarding": {"connected": True, "data_available": True},
            },
        )

        html = report.provider_management_html(SimpleNamespace(providers=lambda: [provider]))

        self.assertNotIn("Smart meter", html)
        self.assertIn("No household data sources are connected yet", html)

    def test_setup_integrations_group_connected_and_optional_sources(self):
        connected = SimpleNamespace(
            provider_id="solaredge",
            name="SolarEdge",
            as_dict=lambda: {
                "provider_id": "solaredge",
                "provider_type": "solar",
                "lifecycle_state": "CONNECTED",
                "capability_summary": {"capabilities": ["daily_summary"]},
                "diagnostics": {},
                "onboarding": {"connected": True, "data_available": True},
            },
        )
        optional = SimpleNamespace(
            provider_id="tesla",
            name="Tesla",
            as_dict=lambda: {
                "provider_id": "tesla",
                "provider_type": "ev_charger",
                "lifecycle_state": "NOT_CONFIGURED",
                "capability_summary": {"capabilities": ["ev_sessions"]},
                "diagnostics": {},
                "onboarding": {"connected": False, "data_available": False},
            },
        )
        smart_meter = SimpleNamespace(
            provider_id="smart_meter_readings",
            name="Smart meter readings",
            as_dict=lambda: {
                "provider_id": "smart_meter_readings",
                "provider_type": "smart_meter",
                "lifecycle_state": "CONFIGURED",
                "capability_summary": {"capabilities": ["interval_history"]},
                "diagnostics": {},
                "onboarding": {"connected": True, "data_available": True},
            },
        )

        html = report.setup_integrations_html(SimpleNamespace(providers=lambda: [connected, optional, smart_meter]))

        self.assertIn("Connected services", html)
        self.assertIn("Connected", html)
        self.assertIn("SolarEdge", html)
        self.assertIn("Optional", html)
        self.assertIn("Tesla", html)
        self.assertNotIn("Smart meter", html)

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
                retailer_id="synergy",
                state_or_territory="WA",
                current_plan="synergy_ev_add_on",
                tariff_add_on="none",
                solar_setup_choice="solaredge",
                ev_setup_choice="ocular_import",
                battery_setup_choice="none",
            )
        )

        self.assertIn('id="setup-billing"', html)
        self.assertIn('id="setup-billing-cycle"', html)
        self.assertIn('id="setup-tariff"', html)
        self.assertIn('class="catalogue-tariff-previews"', html)
        self.assertIn('data-plan="synergy_ev_add_on"', html)
        self.assertIn("Super off-peak / EV window", html)
        self.assertIn('class="manual-tariff-fields"', html)
        self.assertIn("Where do you live?", html)
        self.assertIn("Do you have solar?", html)
        self.assertIn("Do you have an EV?", html)
        self.assertIn("Do you have a battery?", html)
        self.assertNotIn("Optional add-on", html)
        self.assertNotIn('name="tariff_add_on"', html)

    def test_first_time_setup_uses_homeowner_language(self):
        task = SetupTask(
            task_id="configure_tariff",
            title="Choose your electricity plan",
            status="action_needed",
            priority=SetupTaskPriority("high"),
            estimated_effort="2 minutes",
            action_label="Choose Plan",
            action_href="#setup-tariff",
            rationale="Choose your retailer and electricity plan so PowerMonitor can explain costs.",
            source="settings.tariff",
        )
        setup = SetupChecklist(
            household_id="home",
            generated_at="2026-07-03T09:00:00+08:00",
            progress=SetupProgress(0, 0, 1, [task]),
            sections=[SetupSection("household", "Household", [task])],
        )
        settings = SimpleNamespace(
            billing_period_start_day=6,
            retailer="Synergy",
            retailer_id="synergy",
            state_or_territory="WA",
            current_plan="synergy_ev_add_on",
            tariff_add_on="none",
            supply_charge_per_day=1.292269,
            export_credit_rate=0.02,
            import_rates={"super_off_peak": 0.08, "overnight": 0.19, "off_peak": 0.23, "peak": 0.53},
            solar_setup_choice="solaredge",
            ev_setup_choice="ocular_import",
            battery_setup_choice="manual_later",
        )

        html = report.setup_page_html(setup, settings, provider_management=SimpleNamespace(providers=lambda: []))

        self.assertIn("Where do you live?", html)
        self.assertIn("Who is your electricity retailer?", html)
        self.assertIn("What electricity plan are you on?", html)
        self.assertIn("Do you have solar?", html)
        self.assertIn("Do you have an EV?", html)
        self.assertIn("Do you have a battery?", html)
        self.assertIn("What PowerMonitor can do next", html)
        self.assertIn("Fastest path to a useful first recommendation", html)
        self.assertIn("Connected services", html)
        for blocked in ("Provider Runtime", "Collector", "Observation", "Decision Engine", "Household State", "Promotion", "Lifecycle"):
            self.assertNotIn(blocked, html)

    def test_manual_tariff_form_has_manual_plan_without_catalogue_add_on(self):
        html = report.billing_cycle_setup_html(
            SimpleNamespace(
                billing_period_start_day=6,
                retailer="Manual tariff",
                retailer_id="manual_tariff",
                state_or_territory="WA",
                current_plan="manual_tariff",
                tariff_add_on="none",
                import_rates={
                    "super_off_peak": 0.11,
                    "overnight": 0.22,
                    "off_peak": 0.33,
                    "peak": 0.44,
                },
                supply_charge_per_day=1.10,
                export_credit_rate=0.03,
            )
        )

        self.assertIn('<option value="manual_tariff" selected>Manual tariff</option>', html)
        self.assertIn('class="manual-tariff-fields"', html)
        self.assertIn('name="manual_import_rate_peak"', html)
        self.assertNotIn("Optional add-on", html)

    def test_manual_tariff_visibility_is_controlled_by_selected_plan(self):
        source = (PROJECT_ROOT / "collector" / "report.py").read_text(encoding="utf-8")

        self.assertIn(".manual-tariff-fields {{ display:none;", source)
        self.assertIn('option[value="manual_tariff"]:checked) .manual-tariff-fields', source)
        self.assertIn('option[value="manual_tariff"]:checked) .catalogue-tariff-previews', source)

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

    def test_home_page_prioritises_assistant_summary_and_dedupes_cards(self):
        priority = BriefingPriority(
            advisor_id="charging",
            advisor_type="EV_CHARGING",
            title="Wait for cheaper charging",
            summary="Charging is cheaper later tonight.",
            recommendation="Wait 18 minutes for cheaper charging.",
            status="WAIT",
            confidence="Medium",
            confidence_reason="Some uncertainty remains.",
            freshness="Current household data",
            estimated_financial_impact=FinancialImpact(billing_period=4.5),
            supporting_advisors=["Charging"],
        )
        briefing = DailyBriefing(
            greeting="Good evening.",
            overall_household_status="Tonight, focus on overnight tariffs.",
            overall_confidence="Medium",
            overall_freshness="Current household data",
            top_priorities=[priority],
            positive_outcomes=[],
            items_to_watch=[priority],
            suggested_next_action="Wait 18 minutes for cheaper charging.",
            estimated_savings_opportunity=FinancialImpact(billing_period=4.5),
            advisor_summary=[priority, priority],
            read_time_minutes=1,
        )
        glance = GlanceState(
            status="WAIT",
            tone="wait",
            current_recommendation="Wait 18 minutes, then charge.",
            reason="Wait 18 minutes for cheaper charging.",
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
            confidence="Medium",
            evidence_quality="MEDIUM",
            evidence_reason="Recent readings available.",
        )

        html = report.home_page_html(briefing, glance=glance)

        self.assertIn("Can I charge?", html)
        self.assertIn("Wait if you can.", html)
        self.assertIn("We found one thing worth checking.", html)
        self.assertIn("Everything looks good today.", html)
        self.assertNotIn("Recommendation count", html)
        self.assertNotIn("advisor signals", html)
        self.assertEqual(html.count("Wait for cheaper charging"), 1)
        self.assertEqual(html.count("Primary recommendation"), 1)
        self.assertLessEqual(html.count("Secondary observation"), 2)

    def test_report_shell_reveals_deep_hash_routes(self):
        html = (
            inspect.getsource(report.write_html)
            + inspect.getsource(report.system_page_html)
            + inspect.getsource(report.live_data_help_html)
            + inspect.getsource(report.confidence_section_html)
        )

        self.assertIn("body:has(#setup-tariff:target) #setup", html)
        self.assertIn("body:has(#system-live-data:target) #system", html)
        self.assertIn("body:has(#energy-score-explanation:target) #home", html)
        self.assertIn('id="system-live-data"', html)
        self.assertIn('id="system-data-quality"', html)
        self.assertIn('id="energy-score-explanation"', inspect.getsource(report.energy_score_html))
        self.assertIn("revealHashTarget", html)

    def test_page_heading_includes_home_breadcrumb(self):
        html = report.page_heading_html("Can I trust today's advice?", "Provider status and confidence.")

        self.assertIn('class="page-breadcrumb"', html)
        self.assertIn('href="#home"', html)
        self.assertIn("Can I trust today&#x27;s advice?", html)

    def test_household_insights_home_section_is_limited_to_three(self):
        insights = [
            HouseholdInsight(
                insight_id=f"insight_{index}",
                headline=f"Household pattern {index}",
                explanation="This changed compared with recent household history.",
                comparison_period="Recent days",
                confidence="Medium",
                supporting_values={"Difference": f"{index}%"},
                topic=f"topic_{index}",
                usefulness=10.0,
            )
            for index in range(5)
        ]

        html = report.household_insights_html(insights)

        self.assertIn("Interesting things we noticed", html)
        self.assertIn("Household observations, not action items.", html)
        self.assertEqual(html.count('class="household-insight-card"'), 3)
        self.assertIn("Household pattern 0", html)
        self.assertNotIn("Household pattern 4", html)

    def test_home_trends_records_section_limits_display(self):
        trends = [
            HouseholdTrend(
                trend_id=f"trend_{index}",
                label=f"Trend {index}",
                current_value="10.0 kWh",
                previous_value="8.0 kWh",
                percentage_change=25.0,
                direction="up",
                confidence="Medium",
                explanation="This changed compared with the previous period.",
                topic=f"topic_{index}",
            )
            for index in range(4)
        ]
        records = [
            HouseholdRecord(
                record_id=f"record_{index}",
                label=f"Record {index}",
                value="12.0 kWh",
                date="7 Jun",
                previous_record="10.0 kWh",
                confidence="Medium",
                explanation="This is the strongest record PowerMonitor found.",
                topic=f"record_{index}",
            )
            for index in range(3)
        ]

        html = report.home_trends_records_html(HouseholdTrendsReport(trends, records))

        self.assertIn("Trends and records", html)
        self.assertEqual(html.count('class="household-trend-card"'), 2)
        self.assertEqual(html.count('class="household-record-card"'), 1)
        self.assertIn("Trend 0", html)
        self.assertNotIn("Trend 3", html)
        self.assertIn("record 0", html)
        self.assertNotIn("record 2", html)

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
