import inspect
import sys
import unittest
from datetime import date
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
from engine.energy_timeline import EnergyTimeline, EnergyTimelineEvent
from presentation.glance import GlanceState


class MobilePresentationTests(unittest.TestCase):
    def test_navigation_includes_mobile_dashboard_shortcuts(self):
        html = report.nav_html()

        self.assertIn('class="mobile-shortcuts"', html)
        self.assertIn('href="#home"', html)
        self.assertIn('href="#bills"', html)
        self.assertIn('href="#whatif"', html)
        self.assertIn('href="#system"', html)
        self.assertIn(">Home<", html)
        self.assertIn(">Bills<", html)
        self.assertIn(">Solar<", html)
        self.assertIn(">EV<", html)
        self.assertIn(">Decisions<", html)
        self.assertIn(">History<", html)
        self.assertIn(">Settings<", html)
        self.assertNotIn(">Glance<", html)
        self.assertNotIn(">Setup<", html)
        self.assertNotIn(">Recommendations<", html)
        self.assertNotIn(">Timeline<", html)
        self.assertNotIn(">Imports<", html)
        self.assertNotIn(">System<", html)
        self.assertLessEqual(html.count("<a href=") - 4, 7)
        self.assertEqual(html.count(">Home<"), 2)
        self.assertEqual(html.count(">Bills<"), 2)
        self.assertEqual(html.count(">Solar<"), 1)
        self.assertEqual(html.count(">EV<"), 1)
        self.assertEqual(html.count(">Decisions<"), 2)
        self.assertEqual(html.count(">History<"), 1)
        self.assertEqual(html.count(">Settings<"), 2)

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
        billing_summary = report.Summary(
            start=date(2026, 6, 1),
            end=date(2026, 7, 2),
            supply_days=32,
            intervals=0,
            reading_days=30,
            grid_import_kwh=420.0,
            import_cost=96.0,
            supply_cost=24.0,
            total_cost=120.0,
            export_credit=6.0,
            net_cost=114.0,
            solar_generated_kwh=0.0,
            solar_used_kwh=0.0,
            solar_exported_kwh=0.0,
            self_consumption_pct=0.0,
            solar_export_pct=0.0,
            peak_cost_share_pct=0.0,
            ev_charging_kwh=0.0,
            ev_charging_cost=0.0,
            ev_charging_source="estimated",
            actual_ev_charging_kwh=0.0,
            actual_ev_charging_cost=0.0,
            actual_ev_tariff_breakdown={},
            actual_ev_cost_partial=False,
            ev_session_analytics={},
            average_daily_grid_import_kwh=0.0,
            average_daily_solar_generation_kwh=0.0,
            average_daily_net_cost=0.0,
            buckets={
                "overnight": {"kwh": 0.0, "cost": 0.0},
                "off_peak": {"kwh": 0.0, "cost": 0.0},
                "super_off_peak": {"kwh": 0.0, "cost": 0.0},
                "peak": {"kwh": 0.0, "cost": 0.0},
            },
        )
        bill_change = {"difference": 12.5, "previous_bill": 167.5, "confidence": "Medium"}

        html = report.home_page_html(
            briefing,
            glance=glance,
            bill_change=bill_change,
            billing_summary=billing_summary,
            billing_period_end=date(2026, 8, 1),
        )

        self.assertIn("Can I charge?", html)
        self.assertIn("Wait if you can.", html)
        self.assertIn("Today at a glance", html)
        self.assertIn("Is spending normal?", html)
        self.assertIn("Is everything healthy?", html)
        self.assertIn("Best next action", html)
        self.assertIn("Today's timeline", html)
        self.assertIn("Bill forecast", html)
        self.assertIn("Quick links", html)
        self.assertNotIn("Recommendation count", html)
        self.assertNotIn("advisor signals", html)
        self.assertNotIn("Household observations", html)
        self.assertNotIn("Longer-term household context", html)
        self.assertNotIn("Secondary observations", html)
        self.assertNotIn("Trends and records", html)
        self.assertNotIn("Interesting things we noticed", html)
        self.assertNotIn("Background checks", html)
        self.assertNotIn("Recommendation Centre", html)
        self.assertNotIn("Household History", html)
        self.assertNotIn("Recommendation Preview", html)
        self.assertNotIn("recommendation-card", html)
        self.assertNotIn('class="bill-change-section"', html)
        self.assertNotIn("Why has my bill changed?", html)
        self.assertNotIn("What changed, and why it mattered", html)
        self.assertNotIn("Today's Progress", html)
        self.assertNotIn("Show 7 more events", html)
        self.assertNotIn("previous period length", html)
        self.assertNotIn("Supply charges", html)
        self.assertNotIn("Energy charges", html)
        self.assertNotIn("Export credits", html)
        self.assertNotIn('id="home-goals"', html)
        self.assertEqual(html.count("Wait for cheaper charging"), 1)
        self.assertIn("Current spend", html)
        self.assertIn("32 of 62 days", html)
        self.assertIn("Compared with last bill", html)
        self.assertIn("▲ $12.50", html)
        self.assertIn("Forecast confidence", html)
        self.assertIn("View decisions", html)
        self.assertIn("View full history", html)
        self.assertIn("View bills", html)
        self.assertIn("Projection assumes the rest of this bill follows your recent usage pattern.", html)
        self.assertLessEqual(html.count('class="home-timeline-row"'), 3)

    def test_bills_summary_keeps_detailed_bill_components(self):
        settings = report.load_settings()
        summary = report.Summary(
            start=date(2026, 6, 1),
            end=date(2026, 7, 31),
            supply_days=61,
            intervals=1,
            reading_days=61,
            grid_import_kwh=512.0,
            import_cost=140.0,
            supply_cost=38.0,
            total_cost=178.0,
            export_credit=14.0,
            net_cost=164.0,
            solar_generated_kwh=320.0,
            solar_used_kwh=210.0,
            solar_exported_kwh=110.0,
            self_consumption_pct=65.6,
            solar_export_pct=34.4,
            peak_cost_share_pct=28.0,
            ev_charging_kwh=0.0,
            ev_charging_cost=0.0,
            ev_charging_source="estimated",
            actual_ev_charging_kwh=0.0,
            actual_ev_charging_cost=0.0,
            actual_ev_tariff_breakdown={},
            actual_ev_cost_partial=False,
            ev_session_analytics={},
            average_daily_grid_import_kwh=8.4,
            average_daily_solar_generation_kwh=5.2,
            average_daily_net_cost=2.69,
            buckets={
                "overnight": {"kwh": 120.0, "cost": 22.0},
                "off_peak": {"kwh": 98.0, "cost": 18.0},
                "super_off_peak": {"kwh": 55.0, "cost": 8.0},
                "peak": {"kwh": 239.0, "cost": 39.2},
            },
        )

        html = report.summary_html("Current billing period", summary, settings)

        self.assertIn("Import cost", html)
        self.assertIn("Supply charge", html)
        self.assertIn("Solar export credit", html)

    def test_bills_page_follows_financial_flow_and_ends_with_actions(self):
        settings = report.load_settings()
        summary = report.Summary(
            start=date(2026, 6, 1),
            end=date(2026, 7, 31),
            supply_days=61,
            intervals=1,
            reading_days=61,
            grid_import_kwh=512.0,
            import_cost=140.0,
            supply_cost=38.0,
            total_cost=178.0,
            export_credit=14.0,
            net_cost=164.0,
            solar_generated_kwh=320.0,
            solar_used_kwh=210.0,
            solar_exported_kwh=110.0,
            self_consumption_pct=65.6,
            solar_export_pct=34.4,
            peak_cost_share_pct=28.0,
            ev_charging_kwh=24.0,
            ev_charging_cost=7.5,
            ev_charging_source="estimated",
            actual_ev_charging_kwh=0.0,
            actual_ev_charging_cost=0.0,
            actual_ev_tariff_breakdown={},
            actual_ev_cost_partial=False,
            ev_session_analytics={},
            average_daily_grid_import_kwh=8.4,
            average_daily_solar_generation_kwh=5.2,
            average_daily_net_cost=2.69,
            buckets={
                "overnight": {"kwh": 120.0, "cost": 22.0},
                "off_peak": {"kwh": 98.0, "cost": 18.0},
                "super_off_peak": {"kwh": 55.0, "cost": 8.0},
                "peak": {"kwh": 239.0, "cost": 39.2},
            },
        )
        plan = {
            "savings": 18.0,
            "difference": -18.0,
            "recommendation": "Shift more usage into lower-cost hours and keep reviewing the EV Add On plan.",
            "solar_export_value_difference": 4.5,
            "current_plan": {
                "net_bill": 164.0,
                "display_name": "Synergy EV Add On",
                "ev_charging_cost": 7.5,
                "household_usage_cost": 132.5,
            },
            "comparison_plan": {
                "net_bill": 182.0,
                "display_name": "Synergy Home Plan",
                "ev_charging_cost": 19.0,
                "household_usage_cost": 141.0,
            },
        }
        recommendations = [
            {
                "recommendation_id": "bill-1",
                "title": "Move charging later",
                "recommended_action": "Shift EV charging after 9 pm.",
                "rationale": "Most of the recent extra cost came from higher evening demand.",
                "confidence": "High",
                "urgency": {"value": "high"},
                "category": {"value": "bills", "label": "Bills"},
                "financial_impact": {"billing_period": 14.0},
            },
            {
                "recommendation_id": "bill-2",
                "title": "Trim cooling",
                "recommended_action": "Reduce evening air conditioning by one hour.",
                "rationale": "Peak usage is driving a larger share of the bill this period.",
                "confidence": "Medium",
                "urgency": {"value": "medium"},
                "category": {"value": "efficiency", "label": "Efficiency"},
                "financial_impact": {"billing_period": 8.0},
            },
        ]
        bill_change = {
            "available": True,
            "narrative": "Your bill is projected to be higher because peak usage and overnight charging increased.",
            "current_bill": 182.0,
            "previous_bill": 164.0,
            "difference": 18.0,
            "confidence": "High",
            "contributors": [
                {"label": "Peak usage increased", "detail": "More usage landed in the expensive evening window.", "impact": 11.0},
                {"label": "Overnight charging increased", "detail": "More EV charging happened this period.", "impact": 7.0},
            ],
            "positive_changes": [
                {"label": "Solar export credit improved", "impact": -3.5},
            ],
            "negative_changes": [
                {"label": "General household consumption increased", "impact": 9.5},
            ],
        }
        comparison = {
            "period_b": {
                "kind": "previous_billing_period",
                "label": "Previous billing period",
                "start": "2026-04-01",
                "end": "2026-05-31",
            },
            "deltas": {
                "net_cost": 18.0,
                "grid_import_kwh": 35.0,
                "solar_generated_kwh": -12.0,
                "peak_cost": 9.5,
                "self_consumption_pct": -2.1,
                "ev_charging_kwh": 10.0,
            },
        }

        html = report.bills_page_html(
            summary,
            182.0,
            plan,
            recommendations,
            None,
            bill_change,
            comparison,
            settings,
        )

        self.assertLess(html.index("How much is my bill?"), html.index("Why is it that amount?"))
        self.assertLess(html.index("Why is it that amount?"), html.index("Why has my bill changed?"))
        self.assertLess(html.index("Why has my bill changed?"), html.index("What can I do about it?"))
        self.assertIn("Projected bill", html)
        self.assertIn("$182.00", html)
        self.assertIn("Largest contributors", html)
        self.assertIn("Current billing period breakdown", html)
        self.assertIn("Detailed bill calculations", html)
        self.assertIn("bills-breakdown-grid", html)
        self.assertIn("bills-detail-calculation-grid", html)
        self.assertIn("Grid energy", html)
        self.assertIn("Household usage cost", html)
        self.assertIn("Peak usage cost", html)
        self.assertIn("Compared with previous billing period", html)
        self.assertIn("▲ +$18.00", html)
        self.assertIn("▲ +35.00 kWh", html)
        self.assertIn("▼ -12.00 kWh", html)
        self.assertIn("▼ -2.1%", html)
        self.assertIn("Shift EV charging after 9 pm.", html)
        self.assertIn("Reduce evening air conditioning by one hour.", html)

    def test_home_timeline_caps_rows_at_three(self):
        timeline = EnergyTimeline(
            date="2026-07-08",
            is_in_progress=True,
            summary="Three key events are enough for the dashboard.",
            events=[
                EnergyTimelineEvent(
                    event_type=f"event_{index}",
                    local_time=f"{index:02d}:00",
                    title=f"Event {index}",
                    explanation="Something happened.",
                    confidence="medium",
                    sort_time=f"2026-07-08T{index:02d}:00:00",
                )
                for index in range(5)
            ],
        )

        html = report.home_today_timeline_html(timeline)

        self.assertEqual(html.count('class="home-timeline-row"'), 3)
        self.assertIn("Showing latest 3 events", html)
        self.assertNotIn("Latest 3", html)

    def test_home_timeline_orders_rows_newest_first(self):
        timeline = EnergyTimeline(
            date="2026-07-08",
            is_in_progress=True,
            summary="Recent household activity.",
            events=[
                EnergyTimelineEvent(
                    event_type="solar_start",
                    local_time="08:15",
                    title="Solar started",
                    explanation="Morning solar production began.",
                    confidence="high",
                    sort_time="2026-07-08T08:15:00",
                ),
                EnergyTimelineEvent(
                    event_type="peak_usage",
                    local_time="18:45",
                    title="Peak usage interval",
                    explanation="Evening demand increased.",
                    confidence="medium",
                    sort_time="2026-07-08T18:45:00",
                ),
                EnergyTimelineEvent(
                    event_type="export_start",
                    local_time="11:30",
                    title="Export started",
                    explanation="Solar export began.",
                    confidence="high",
                    sort_time="2026-07-08T11:30:00",
                ),
            ],
        )

        html = report.home_today_timeline_html(timeline)

        peak_index = html.index("Peak usage interval")
        export_index = html.index("Export started")
        solar_index = html.index("Solar started")
        self.assertLess(peak_index, export_index)
        self.assertLess(export_index, solar_index)

    def test_bill_contributor_rows_do_not_render_category_labels(self):
        html = report.bill_change_explanation_html(
            {
                "available": True,
                "narrative": "A few billing components changed this period.",
                "current_bill": 210.0,
                "previous_bill": 180.0,
                "difference": 30.0,
                "confidence": "High",
                "contributors": [
                    {"category": "household", "label": "General household consumption increased", "detail": "Higher everyday usage added cost.", "impact": 12.0},
                    {"category": "overnight", "label": "Overnight charging increased", "detail": "More charging happened overnight.", "impact": 9.0},
                    {"category": "off_peak", "label": "Off-peak usage increased", "detail": "Off-peak usage was higher than last period.", "impact": 4.0},
                    {"category": "solar", "label": "Solar export credit fell", "detail": "Less export credit reduced the offset.", "impact": 3.0},
                    {"category": "peak", "label": "Peak usage increased", "detail": "Peak imports were more expensive.", "impact": 2.0},
                ],
                "positive_changes": [],
                "negative_changes": [],
            }
        )

        self.assertIn("Largest contributors", html)
        self.assertIn("General household consumption increased", html)
        self.assertIn("Overnight charging increased", html)
        self.assertNotIn("HOUSEHOLDGeneral", html)
        self.assertNotIn("HouseholdGeneral", html)
        self.assertNotIn("OVERNIGHTOvernight", html)
        self.assertNotIn("OvernightOvernight", html)
        self.assertNotIn("OFF PEAKOff-peak", html)
        self.assertNotIn("SOLARSolar", html)
        self.assertNotIn("PEAKPeak", html)

    def test_report_shell_reveals_deep_hash_routes(self):
        html = (
            inspect.getsource(report.write_html)
            + inspect.getsource(report.system_page_html)
            + inspect.getsource(report.live_data_help_html)
            + inspect.getsource(report.confidence_section_html)
        )

        self.assertIn("body:has(#setup-tariff:target) #system", html)
        self.assertIn("body:has(#system-live-data:target) #system", html)
        self.assertIn("body:has(#recommendations:target) #whatif", html)
        self.assertIn("body:has(#home-goals:target) #whatif", html)
        self.assertIn("body:has(#timeline:target) #analytics", html)
        self.assertIn("body:has(#imports:target) #system", html)
        self.assertIn("body:has(#energy-score-explanation:target) #home", html)
        self.assertIn(".page {{ display:none; }}", html)
        self.assertIn("#home {{ display:block; }}", html)
        self.assertIn(".page:target {{ display:block; }}", html)
        self.assertIn('id="system-live-data"', html)
        self.assertIn('id="system-data-quality"', html)
        self.assertIn('id="energy-score-explanation"', inspect.getsource(report.energy_score_html))
        self.assertIn("revealHashTarget", html)

    def test_report_shell_assigns_full_sections_to_owner_pages(self):
        source = inspect.getsource(report.write_html)
        home_block = source[source.index('<section id="home"'):source.index('<section id="bills"')]
        decisions_block = source[source.index('<section id="whatif"'):source.index('<section id="analytics"')]
        history_block = source[source.index('<section id="analytics"'):source.index('<section id="system"')]
        history_helper = inspect.getsource(report.history_page_html)

        self.assertEqual(source.count('page_heading_html("Recommendation Centre"'), 1)
        self.assertEqual(source.count("advisor_centre_html(advisor_explanations.active())"), 1)
        self.assertEqual(source.count("recommendation_centre_html("), 1)
        self.assertEqual(history_helper.count('page_heading_html("Household History"'), 1)
        self.assertEqual(history_helper.count("timeline_html(display_timeline)"), 1)

        self.assertIn("recommendation_presentations, display_timeline", home_block)
        self.assertNotIn("Background checks", home_block)
        self.assertNotIn("Recommendation Centre", home_block)
        self.assertNotIn("Household History", home_block)
        self.assertNotIn("What changed, and why it mattered", home_block)
        self.assertNotIn('id="recommendations"', home_block)
        self.assertNotIn('id="timeline"', home_block)
        self.assertNotIn("goals_progress_html", home_block)
        self.assertIn('id="recommendations"', decisions_block)
        self.assertIn("advisor_centre_html(advisor_explanations.active())", decisions_block)
        self.assertIn("recommendation_centre_html", decisions_block)
        self.assertIn("goals_progress_html(goals or [])", inspect.getsource(report.whatif_page_html))
        self.assertIn("history_page_html(", history_block)
        self.assertIn('id="timeline"', history_helper)
        self.assertIn("timeline_html(display_timeline)", history_helper)
        self.assertIn("household_insights_html(insights)", history_helper)
        self.assertIn("energy_timeline_history_html(energy_timeline)", history_helper)
        self.assertIn("history_story_section_html", history_helper)
        self.assertIn("What changed, and why it mattered", inspect.getsource(report.timeline_html))
        self.assertNotIn("comparison_html(comparison)", history_helper)

    def test_history_page_starts_with_homeowner_story_and_keeps_full_history(self):
        trends = [
            report.DailyTrend(
                day=date(2026, 7, 6),
                grid_import_kwh=14.0,
                solar_generated_kwh=8.0,
                solar_exported_kwh=2.0,
                import_cost=5.0,
                export_credit=0.4,
                net_cost=6.2,
                ev_charging_kwh=0.0,
                ev_charging_source="estimated",
            ),
            report.DailyTrend(
                day=date(2026, 7, 7),
                grid_import_kwh=18.0,
                solar_generated_kwh=10.0,
                solar_exported_kwh=3.0,
                import_cost=7.0,
                export_credit=0.6,
                net_cost=8.9,
                ev_charging_kwh=5.5,
                ev_charging_source="estimated",
            ),
        ]
        historical = report.HistoricalTrendsEngine().build(
            [
                {"day": date(2026, 7, 1), "net_cost": 6.0, "grid_import_kwh": 13.0, "solar_generated_kwh": 8.0, "solar_exported_kwh": 2.0},
                {"day": date(2026, 7, 2), "net_cost": 6.2, "grid_import_kwh": 13.5, "solar_generated_kwh": 8.5, "solar_exported_kwh": 2.2},
                {"day": date(2026, 7, 3), "net_cost": 6.4, "grid_import_kwh": 14.0, "solar_generated_kwh": 9.0, "solar_exported_kwh": 2.4},
                {"day": date(2026, 7, 4), "net_cost": 6.3, "grid_import_kwh": 14.2, "solar_generated_kwh": 9.1, "solar_exported_kwh": 2.1},
                {"day": date(2026, 7, 5), "net_cost": 6.1, "grid_import_kwh": 13.8, "solar_generated_kwh": 8.8, "solar_exported_kwh": 2.0},
                {"day": date(2026, 7, 6), "net_cost": 6.2, "grid_import_kwh": 14.0, "solar_generated_kwh": 8.0, "solar_exported_kwh": 2.0},
                {"day": date(2026, 7, 7), "net_cost": 8.9, "grid_import_kwh": 18.0, "solar_generated_kwh": 10.0, "solar_exported_kwh": 3.0},
            ],
            as_of=date(2026, 7, 8),
        )
        insights = [
            HouseholdInsight(
                insight_id="solar_shift",
                headline="Solar covered more of the household load than normal.",
                explanation="The latest solar day kept more generation at home than recent days usually do.",
                comparison_period="Recent solar days",
                confidence="Medium",
                supporting_values={"Latest": "10.0 kWh", "Average": "8.9 kWh"},
                topic="solar",
                usefulness=10.0,
            )
        ]
        energy_timeline = EnergyTimeline(
            date="2026-07-08",
            is_in_progress=True,
            summary="A few notable events shaped the latest day.",
            events=[
                EnergyTimelineEvent(
                    event_type="peak_usage_interval",
                    local_time="19:30",
                    title="Peak usage interval",
                    explanation="Grid import increased during the evening.",
                    confidence="medium",
                    sort_time="2026-07-08T19:30:00",
                ),
                EnergyTimelineEvent(
                    event_type="solar_started",
                    local_time="08:10",
                    title="Solar started",
                    explanation="Rooftop solar began covering part of the household load.",
                    confidence="high",
                    sort_time="2026-07-08T08:10:00",
                ),
            ],
        )
        timeline = DecisionTimeline(
            entries=[
                TimelineEntry(
                    timestamp="2026-07-08T19:30:00+08:00",
                    title="Peak usage interval",
                    summary="Grid import increased during the evening.",
                    financial_impact="$2.10 extra",
                    confidence="Medium",
                    evidence_sources=["Household data"],
                    event_type="billing_cost",
                ),
                TimelineEntry(
                    timestamp="2026-07-07T12:10:00+08:00",
                    title="Solar covered more load",
                    summary="Solar covered more of the household load than normal.",
                    financial_impact="$1.20 saved",
                    confidence="High",
                    evidence_sources=["Household data"],
                    event_type="solar_event",
                ),
            ]
        )

        html = report.history_page_html(
            trends,
            report.load_settings(),
            insights,
            energy_timeline,
            timeline,
            historical,
        )

        self.assertIn("Recent household story", html)
        self.assertIn("What happened recently?", html)
        self.assertIn("Was the latest day normal?", html)
        self.assertIn("Daily cost", html)
        self.assertIn("Grid import", html)
        self.assertIn("Solar generation", html)
        self.assertIn("Estimated overnight EV charging", html)
        self.assertIn("Household History", html)
        self.assertIn("What changed, and why it mattered", html)
        self.assertLess(html.index("Recent household story"), html.index("Household History"))
        self.assertIn("Yesterday -", html)

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

        self.assertIn("Tuesday - higher-cost activity stood out", html)
        self.assertIn('class="timeline-stamp"', html)
        self.assertIn("Billing Period Updated", html)
        self.assertNotIn("billing_period_updated", html)


if __name__ == "__main__":
    unittest.main()
