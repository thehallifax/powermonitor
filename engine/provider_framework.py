#!/usr/bin/env python3
"""Provider metadata framework for PowerMonitor integrations."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional, Union


class ProviderType(str, Enum):
    SOLAR = "solar"
    EV_CHARGER = "ev_charger"
    RETAILER = "retailer"
    BATTERY = "battery"
    SMART_METER = "smart_meter"
    WEATHER = "weather"
    HOME_AUTOMATION = "home_automation"
    GENERIC_IMPORT = "generic_import"


class ProviderCapability(str, Enum):
    LIVE_POWER = "live_power"
    INTERVAL_HISTORY = "interval_history"
    DAILY_SUMMARY = "daily_summary"
    BILLING_DATA = "billing_data"
    TARIFF_DATA = "tariff_data"
    EV_SESSIONS = "ev_sessions"
    BATTERY_STATE = "battery_state"
    FORECAST = "forecast"
    IMPORT_ONLY = "import_only"


class ProviderStatus(str, Enum):
    AVAILABLE = "available"
    CONFIGURED = "configured"
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    MISSING = "missing"
    FUTURE = "future"


class ProviderObservationQuality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ProviderObservationType(str, Enum):
    SOLAR_GENERATION = "solar_generation"
    GRID_IMPORT = "grid_import"
    SOLAR_EXPORT = "solar_export"
    EV_SESSION = "ev_session"
    TARIFF_CONFIG = "tariff_config"
    BILLING_PERIOD = "billing_period"
    COLLECTOR_HEALTH = "collector_health"
    LIVE_POWER = "live_power"
    GRID_LIVE_POWER = "grid_live_power"
    SOLAR_LIVE_POWER = "solar_live_power"
    BATTERY_STATE = "battery_state"
    BATTERY_SOC = "battery_soc"
    BATTERY_POWER = "battery_power"
    EV_CHARGER_LIVE_POWER = "ev_charger_live_power"
    EV_CONNECTED_STATE = "ev_connected_state"
    EV_CHARGING_STATE = "ev_charging_state"
    HOME_LIVE_POWER = "home_live_power"


@dataclass(frozen=True)
class ProviderFreshness:
    state: str = "unknown"
    observed_at: Optional[str] = None
    age_minutes: Optional[float] = None
    note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderObservation:
    capability: ProviderCapability
    name: str
    value: Any = None
    unit: Optional[str] = None
    provider_id: Optional[str] = None
    provider_type: Optional[ProviderType] = None
    household_id: Optional[str] = None
    observation_type: Optional[ProviderObservationType] = None
    observed_at: Optional[str] = None
    received_at: Optional[str] = None
    freshness: Optional[ProviderFreshness] = None
    quality: ProviderObservationQuality = ProviderObservationQuality.UNKNOWN
    provenance: Optional[str] = None
    raw_reference: Optional[str] = None
    import_job_id: Optional[str] = None
    note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type.value if self.provider_type else None,
            "household_id": self.household_id,
            "capability": self.capability.value,
            "observation_type": self.observation_type.value if self.observation_type else None,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "freshness": self.freshness.as_dict() if self.freshness else None,
            "quality": self.quality.value,
            "provenance": self.provenance,
            "raw_reference": self.raw_reference,
            "import_job_id": self.import_job_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class Provider:
    provider_id: str
    name: str
    provider_type: ProviderType
    capabilities: list[ProviderCapability] = field(default_factory=list)
    status: ProviderStatus = ProviderStatus.MISSING
    freshness: Optional[ProviderFreshness] = None
    observations: list[ProviderObservation] = field(default_factory=list)
    source: Optional[str] = None
    note: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status in {
            ProviderStatus.AVAILABLE,
            ProviderStatus.CONFIGURED,
            ProviderStatus.SUPPORTED,
        }

    def supports(self, capability: Union[ProviderCapability, str]) -> bool:
        value = capability if isinstance(capability, ProviderCapability) else ProviderCapability(capability)
        return value in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "provider_type": self.provider_type.value,
            "capabilities": [capability.value for capability in self.capabilities],
            "status": self.status.value,
            "available": self.available,
            "freshness": self.freshness.as_dict() if self.freshness else None,
            "observations": [observation.as_dict() for observation in self.observations],
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class ProviderRegistry:
    providers: list[Provider] = field(default_factory=list)

    def list_available(self) -> list[Provider]:
        return [provider for provider in self.providers if provider.available]

    def providers_with_capability(self, capability: Union[ProviderCapability, str]) -> list[Provider]:
        value = capability if isinstance(capability, ProviderCapability) else ProviderCapability(capability)
        return [provider for provider in self.providers if value in provider.capabilities]

    def has_capability(self, capability: Union[ProviderCapability, str]) -> bool:
        return any(provider.available for provider in self.providers_with_capability(capability))

    def get(self, provider_id: str) -> Optional[Provider]:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        return None

    def status_by_provider(self) -> dict[str, str]:
        return {provider.provider_id: provider.status.value for provider in self.providers}

    def as_dict(self) -> dict[str, Any]:
        available = self.list_available()
        capabilities = sorted({
            capability.value
            for provider in available
            for capability in provider.capabilities
        })
        return {
            "providers": [provider.as_dict() for provider in self.providers],
            "available_provider_ids": [provider.provider_id for provider in available],
            "capabilities": capabilities,
            "status": self.status_by_provider(),
        }


def build_provider_registry(
    *,
    settings: Any = None,
    health: Optional[dict[str, Any]] = None,
    capability_summary: Any = None,
    import_sources: Optional[list[Any]] = None,
) -> ProviderRegistry:
    """Register current conceptual providers without creating new integrations."""

    health = health or {}
    import_sources = import_sources or []
    solar = _capability(capability_summary, "solar")
    ev = _capability(capability_summary, "ev")
    tariff = _capability(capability_summary, "tariff")
    grid = _capability(capability_summary, "grid_smart_meter")
    freshness_capability = _capability(capability_summary, "data_freshness")
    freshness = _provider_freshness(freshness_capability, health)
    tariff_configured = bool(tariff and getattr(tariff, "usable", False))
    solar_present = bool(solar and getattr(solar, "usable", False))
    ev_present = bool(ev and getattr(ev, "usable", False))
    grid_present = bool(grid and getattr(grid, "usable", False))
    ev_source = str(getattr(settings, "ev_data_source", "") or "")
    home_assistant_configured = bool(str(getattr(settings, "home_assistant_url", "") or "").strip())
    has_ocular_import = any(str(getattr(source, "source_id", "")) == "ocular_ev_export" or "ocular" in str(getattr(source, "source_id", "")) for source in import_sources)

    providers = [
        Provider(
            provider_id="solaredge",
            name="SolarEdge",
            provider_type=ProviderType.SOLAR,
            capabilities=[
                ProviderCapability.INTERVAL_HISTORY,
                ProviderCapability.DAILY_SUMMARY,
            ],
            status=_status_from_capability(solar_present, health),
            freshness=freshness if solar_present else None,
            observations=[
                ProviderObservation(
                    ProviderCapability.DAILY_SUMMARY,
                    "generation",
                    freshness=freshness,
                    quality=ProviderObservationQuality.HIGH if solar_present else ProviderObservationQuality.LOW,
                    note="Solar generation/import/export are normalized from readings.",
                )
            ],
            source="readings",
            note=solar.reason if solar else "Solar provider not detected in household readings.",
        ),
        Provider(
            provider_id="smart_meter_readings",
            name="Smart meter readings",
            provider_type=ProviderType.SMART_METER,
            capabilities=[
                ProviderCapability.INTERVAL_HISTORY,
                ProviderCapability.DAILY_SUMMARY,
            ],
            status=ProviderStatus.AVAILABLE if grid_present else ProviderStatus.MISSING,
            freshness=freshness if grid_present else None,
            observations=[
                ProviderObservation(
                    ProviderCapability.INTERVAL_HISTORY,
                    "import_export_interval_history",
                    freshness=freshness,
                    quality=ProviderObservationQuality.HIGH if grid_present else ProviderObservationQuality.LOW,
                    note="Grid import/export interval data is normalized by DataEngine.",
                )
            ],
            source="readings",
            note=grid.reason if grid else "Smart meter readings are not detected.",
        ),
        Provider(
            provider_id="ocular_export",
            name="Ocular export",
            provider_type=ProviderType.EV_CHARGER,
            capabilities=[
                ProviderCapability.EV_SESSIONS,
                ProviderCapability.IMPORT_ONLY,
            ],
            status=ProviderStatus.AVAILABLE if ev_present else ProviderStatus.SUPPORTED if has_ocular_import or import_sources else ProviderStatus.MISSING,
            observations=[
                ProviderObservation(
                    ProviderCapability.EV_SESSIONS,
                    "sessions",
                    quality=ProviderObservationQuality.HIGH if ev_present else ProviderObservationQuality.MEDIUM,
                    note="EV sessions are available when imported from Ocular workbook exports.",
                )
            ],
            source="import_framework",
            note=ev.reason if ev else "Ocular EV charging history upload is supported.",
        ),
        Provider(
            provider_id="synergy_tariff_config",
            name="Synergy tariff/config",
            provider_type=ProviderType.RETAILER,
            capabilities=[
                ProviderCapability.TARIFF_DATA,
                ProviderCapability.BILLING_DATA,
            ],
            status=ProviderStatus.CONFIGURED if tariff_configured else ProviderStatus.MISSING,
            observations=[
                ProviderObservation(
                    ProviderCapability.TARIFF_DATA,
                    "tariff_rules",
                    quality=ProviderObservationQuality.HIGH if tariff_configured else ProviderObservationQuality.LOW,
                    note="Tariff rules, supply charge, and export credit come from local configuration.",
                )
            ],
            source="settings",
            note=tariff.reason if tariff else "Retailer tariff configuration is missing.",
        ),
    ]
    if home_assistant_configured:
        providers.append(
            Provider(
                provider_id="home_assistant",
                name="Home Assistant",
                provider_type=ProviderType.HOME_AUTOMATION,
                capabilities=[
                    ProviderCapability.LIVE_POWER,
                    ProviderCapability.INTERVAL_HISTORY,
                    ProviderCapability.DAILY_SUMMARY,
                    ProviderCapability.BATTERY_STATE,
                    ProviderCapability.EV_SESSIONS,
                ],
                status=ProviderStatus.CONFIGURED,
                observations=[
                    ProviderObservation(
                        ProviderCapability.LIVE_POWER,
                        "home_assistant_entities",
                        quality=ProviderObservationQuality.MEDIUM,
                        note="Home Assistant entities are discovered through the data connection and turned into source readings.",
                    )
                ],
                source="home_assistant",
                note="Home Assistant URL is configured. Add a long-lived access token, then run Test Connection.",
            )
        )
    if ev_source == "home_assistant_ocpp":
        providers.append(
            Provider(
                provider_id="home_assistant_ocpp",
                name="Home Assistant OCPP",
                provider_type=ProviderType.HOME_AUTOMATION,
                capabilities=[
                    ProviderCapability.EV_SESSIONS,
                    ProviderCapability.LIVE_POWER,
                ],
                status=ProviderStatus.AVAILABLE if ev_present else ProviderStatus.CONFIGURED,
                freshness=freshness if ev_present else None,
                observations=[
                    ProviderObservation(
                        ProviderCapability.EV_SESSIONS,
                        "sessions",
                        freshness=freshness if ev_present else None,
                        quality=ProviderObservationQuality.HIGH if ev_present else ProviderObservationQuality.MEDIUM,
                        note="Home Assistant OCPP can provide EV session data when configured.",
                    )
                ],
                source="home_assistant_ocpp",
                note="Configured EV data source is Home Assistant OCPP.",
            )
        )
    return ProviderRegistry(providers)


def _capability(summary: Any, capability_id: str) -> Any:
    if summary is None:
        return None
    if hasattr(summary, "get"):
        return summary.get(capability_id)
    return None


def _provider_freshness(capability: Any, health: dict[str, Any]) -> ProviderFreshness:
    freshness = getattr(capability, "freshness", None)
    if freshness:
        return ProviderFreshness(
            state=getattr(freshness, "state", "unknown"),
            observed_at=getattr(freshness, "observed_at", None),
            age_minutes=getattr(freshness, "age_minutes", None),
            note=getattr(capability, "reason", None),
        )
    return ProviderFreshness(
        state="unknown" if not health else str(health.get("status") or "unknown"),
        observed_at=health.get("latest_measurement_time"),
        age_minutes=health.get("report_age_minutes"),
        note=str(health.get("collector_message") or health.get("message") or ""),
    )


def _status_from_capability(present: bool, health: dict[str, Any]) -> ProviderStatus:
    if not present:
        return ProviderStatus.MISSING
    status = str(health.get("status") or "ok").lower()
    if status == "ok":
        return ProviderStatus.AVAILABLE
    return ProviderStatus.DEGRADED
