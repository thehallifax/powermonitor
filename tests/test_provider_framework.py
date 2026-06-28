import inspect
import unittest
from dataclasses import dataclass

from engine.charging_advisor import ChargingAdvisor
from engine.models import CapabilityStatus, Freshness, HouseholdCapabilitySummary
from engine.provider_framework import (
    ProviderCapability,
    ProviderRegistry,
    ProviderStatus,
    ProviderType,
    build_provider_registry,
)


@dataclass
class Settings:
    ev_data_source: str = "estimated"


class ProviderFrameworkTests(unittest.TestCase):
    def test_registry_lists_conceptual_current_providers_and_capabilities(self):
        registry = build_provider_registry(
            settings=Settings(),
            health={"status": "ok", "report_age_minutes": 5},
            capability_summary=capability_summary(solar=True, ev=True, tariff=True),
            import_sources=[source("ocular_ev_export", "Ocular EV export")],
        )

        self.assertIsInstance(registry, ProviderRegistry)
        self.assertEqual(registry.get("solaredge").provider_type, ProviderType.SOLAR)
        self.assertTrue(registry.has_capability(ProviderCapability.TARIFF_DATA))
        self.assertTrue(registry.has_capability(ProviderCapability.EV_SESSIONS))
        self.assertIn("synergy_tariff_config", registry.status_by_provider())

    def test_home_assistant_ocpp_provider_registers_only_when_configured(self):
        registry = build_provider_registry(
            settings=Settings(ev_data_source="home_assistant_ocpp"),
            health={"status": "ok"},
            capability_summary=capability_summary(solar=False, ev=True, tariff=True),
            import_sources=[],
        )

        provider = registry.get("home_assistant_ocpp")

        self.assertIsNotNone(provider)
        self.assertEqual(provider.status, ProviderStatus.AVAILABLE)
        self.assertTrue(provider.supports(ProviderCapability.LIVE_POWER))

    def test_registry_serializes_for_api(self):
        data = build_provider_registry(
            settings=Settings(),
            health={},
            capability_summary=capability_summary(solar=False, ev=False, tariff=False),
            import_sources=[],
        ).as_dict()

        self.assertIn("providers", data)
        self.assertIn("status", data)
        self.assertTrue(all("provider_id" in provider for provider in data["providers"]))

    def test_advisors_remain_provider_agnostic(self):
        signature = inspect.signature(ChargingAdvisor.advise)

        self.assertNotIn("provider_registry", signature.parameters)
        self.assertNotIn("provider", signature.parameters)


def capability_summary(*, solar: bool, ev: bool, tariff: bool) -> HouseholdCapabilitySummary:
    freshness = Freshness(state="fresh", observed_at="2026-06-28T08:00:00+08:00")
    return HouseholdCapabilitySummary(
        household_id="household",
        generated_at="2026-06-28T09:00:00+08:00",
        capabilities=[
            CapabilityStatus("solar", "Solar", present=solar, source="provider:solaredge", reason="Solar state", freshness=freshness if solar else None),
            CapabilityStatus("ev", "EV charging", present=ev, available=True, source="provider:ocular_export", reason="EV state"),
            CapabilityStatus("grid_smart_meter", "Grid / smart meter", present=True, source="provider:smart_meter_readings", reason="Grid state", freshness=freshness),
            CapabilityStatus("tariff", "Tariff", configured=tariff, source="provider:synergy_tariff_config", reason="Tariff state"),
            CapabilityStatus("imports", "Imports", available=True, source="provider:ocular_export", reason="Imports supported"),
            CapabilityStatus("data_freshness", "Data freshness", present=True, source="readings", reason="Freshness state", freshness=freshness),
        ],
    )


@dataclass(frozen=True)
class source:
    source_id: str
    display_name: str
    supported_extensions: tuple[str, ...] = (".xlsx",)


if __name__ == "__main__":
    unittest.main()
