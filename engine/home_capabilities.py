#!/usr/bin/env python3
"""Home capability detection from declared profile facts and measurements."""

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .home_profile import HomeProfile
from .measurement_framework import (
    MeasurementCapability,
    MeasurementProvider,
    MeasurementProviderRegistry,
    MeasurementQuality,
    MeasurementType,
)

HOME_CAPABILITY_IDS = (
    "billing_analysis",
    "solar_generation_analysis",
    "household_consumption_analysis",
    "grid_import_export_analysis",
    "ev_charging_analysis",
    "battery_analysis",
    "tariff_optimisation_readiness",
    "energy_timeline_readiness",
    "home_intelligence_readiness",
)


@dataclass(frozen=True)
class HomeCapability:
    """One backend capability currently relevant to the home."""

    capability_id: str
    label: str
    enabled: bool
    available: bool
    reason: str
    supporting_data_sources: list[str] = field(default_factory=list)
    confidence: str = "Low"
    data_quality: str = "unknown"

    def __post_init__(self) -> None:
        if self.capability_id not in HOME_CAPABILITY_IDS:
            raise ValueError(f"Unsupported home capability: {self.capability_id}")
        if self.confidence not in {"Low", "Medium", "High"}:
            raise ValueError(f"Unsupported home capability confidence: {self.confidence}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "enabled": self.enabled,
            "available": self.available,
            "reason": self.reason,
            "supporting_data_sources": list(self.supporting_data_sources),
            "confidence": self.confidence,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class HomeCapabilities:
    """Detected backend readiness for a home."""

    capabilities: list[HomeCapability] = field(default_factory=list)

    def get(self, capability_id: str) -> Optional[HomeCapability]:
        for capability in self.capabilities:
            if capability.capability_id == capability_id:
                return capability
        return None

    def available(self) -> list[HomeCapability]:
        return [capability for capability in self.capabilities if capability.available]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [capability.as_dict() for capability in self.capabilities],
            "available": [
                capability.capability_id
                for capability in self.capabilities
                if capability.available
            ],
        }


class HomeCapabilityDetector:
    """Combine Home Profile facts with measurement-provider capabilities."""

    def detect(
        self,
        profile: Optional[HomeProfile] = None,
        registry: Optional[MeasurementProviderRegistry] = None,
    ) -> HomeCapabilities:
        profile = profile or HomeProfile.default()
        registry = registry or MeasurementProviderRegistry([])
        provider_map = _provider_capability_map(registry)
        capabilities = [
            self._billing_analysis(profile, provider_map),
            self._solar_generation_analysis(profile, provider_map),
            self._household_consumption_analysis(profile, provider_map),
            self._grid_import_export_analysis(provider_map),
            self._ev_charging_analysis(profile),
            self._battery_analysis(profile, provider_map),
            self._tariff_optimisation_readiness(profile, provider_map),
            self._energy_timeline_readiness(provider_map),
        ]
        capabilities.append(self._home_intelligence_readiness(capabilities))
        return HomeCapabilities(capabilities)

    def _billing_analysis(
        self,
        profile: HomeProfile,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        tariff_sources = _sources(provider_map, MeasurementType.TARIFF_DATA)
        profile_sources = _profile_sources(profile, "current_tariff", "electricity_retailer")
        available = bool(tariff_sources or profile.current_tariff or profile.electricity_retailer)
        return HomeCapability(
            "billing_analysis",
            "Billing analysis",
            enabled=True,
            available=available,
            reason=(
                "Tariff or retailer context is available for bill explanation."
                if available else
                "Add retailer or tariff details to the Home Profile."
            ),
            supporting_data_sources=sorted(set(tariff_sources + profile_sources)),
            confidence="High" if tariff_sources else "Medium" if available else "Low",
            data_quality=_quality(provider_map, MeasurementType.TARIFF_DATA, fallback="declared" if available else "unknown"),
        )

    def _solar_generation_analysis(
        self,
        profile: HomeProfile,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        sources = _sources(provider_map, MeasurementType.SOLAR_GENERATION)
        enabled = profile.solar_installed
        available = enabled and bool(sources)
        return HomeCapability(
            "solar_generation_analysis",
            "Solar generation analysis",
            enabled=enabled,
            available=available,
            reason=(
                "Solar generation measurements are available."
                if available else
                "Solar is declared, but no solar generation measurements are available."
                if enabled else
                "Solar is not declared in the Home Profile."
            ),
            supporting_data_sources=sources + (["home_profile:solar_installed"] if enabled else []),
            confidence=_confidence(provider_map, MeasurementType.SOLAR_GENERATION) if available else "Low",
            data_quality=_quality(provider_map, MeasurementType.SOLAR_GENERATION),
        )

    def _household_consumption_analysis(
        self,
        profile: HomeProfile,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        sources = _sources(provider_map, MeasurementType.HOUSEHOLD_CONSUMPTION)
        enabled = profile.household_monitoring_available or bool(sources)
        available = bool(sources)
        return HomeCapability(
            "household_consumption_analysis",
            "Household consumption analysis",
            enabled=enabled,
            available=available,
            reason=(
                "Household consumption measurements are available."
                if available else
                "Household monitoring is declared, but no consumption measurements are available."
                if profile.household_monitoring_available else
                "No household consumption monitoring is declared or measured."
            ),
            supporting_data_sources=sources + (["home_profile:household_monitoring_available"] if profile.household_monitoring_available else []),
            confidence=_confidence(provider_map, MeasurementType.HOUSEHOLD_CONSUMPTION) if available else "Low",
            data_quality=_quality(provider_map, MeasurementType.HOUSEHOLD_CONSUMPTION),
        )

    def _grid_import_export_analysis(
        self,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        import_sources = _sources(provider_map, MeasurementType.GRID_IMPORT)
        export_sources = _sources(provider_map, MeasurementType.GRID_EXPORT)
        available = bool(import_sources and export_sources)
        return HomeCapability(
            "grid_import_export_analysis",
            "Grid import/export analysis",
            enabled=True,
            available=available,
            reason=(
                "Grid import and export measurements are available."
                if available else
                "Grid import and export analysis needs both import and export measurements."
            ),
            supporting_data_sources=sorted(set(import_sources + export_sources)),
            confidence=_combined_confidence(
                _confidence(provider_map, MeasurementType.GRID_IMPORT),
                _confidence(provider_map, MeasurementType.GRID_EXPORT),
            ) if available else "Low",
            data_quality=_combined_quality(
                _quality(provider_map, MeasurementType.GRID_IMPORT),
                _quality(provider_map, MeasurementType.GRID_EXPORT),
            ),
        )

    def _ev_charging_analysis(self, profile: HomeProfile) -> HomeCapability:
        enabled = profile.ev_owned
        available = enabled and bool(profile.charger_type)
        return HomeCapability(
            "ev_charging_analysis",
            "EV charging analysis",
            enabled=enabled,
            available=available,
            reason=(
                "EV ownership and charger type are declared."
                if available else
                "EV is declared, but charger type or charging data is not available yet."
                if enabled else
                "EV ownership is not declared in the Home Profile."
            ),
            supporting_data_sources=_profile_sources(profile, "ev_owned", "charger_type"),
            confidence="Low" if available else "Low",
            data_quality="declared" if available else "unknown",
        )

    def _battery_analysis(
        self,
        profile: HomeProfile,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        charge_sources = _sources(provider_map, MeasurementType.BATTERY_CHARGE)
        discharge_sources = _sources(provider_map, MeasurementType.BATTERY_DISCHARGE)
        enabled = profile.battery_installed
        available = enabled and bool(charge_sources and discharge_sources)
        return HomeCapability(
            "battery_analysis",
            "Battery analysis",
            enabled=enabled,
            available=available,
            reason=(
                "Battery charge and discharge measurements are available."
                if available else
                "Battery is declared, but battery charge/discharge measurements are unavailable."
                if enabled else
                "Battery is not declared in the Home Profile."
            ),
            supporting_data_sources=sorted(set(charge_sources + discharge_sources + (["home_profile:battery_installed"] if enabled else []))),
            confidence=_combined_confidence(
                _confidence(provider_map, MeasurementType.BATTERY_CHARGE),
                _confidence(provider_map, MeasurementType.BATTERY_DISCHARGE),
            ) if available else "Low",
            data_quality=_combined_quality(
                _quality(provider_map, MeasurementType.BATTERY_CHARGE),
                _quality(provider_map, MeasurementType.BATTERY_DISCHARGE),
            ),
        )

    def _tariff_optimisation_readiness(
        self,
        profile: HomeProfile,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        grid_sources = _sources(provider_map, MeasurementType.GRID_IMPORT)
        tariff_sources = _sources(provider_map, MeasurementType.TARIFF_DATA)
        has_tariff = bool(profile.current_tariff or tariff_sources)
        available = bool(has_tariff and grid_sources)
        return HomeCapability(
            "tariff_optimisation_readiness",
            "Tariff optimisation readiness",
            enabled=True,
            available=available,
            reason=(
                "Tariff context and grid import measurements are available."
                if available else
                "Tariff optimisation needs tariff context and grid import measurements."
            ),
            supporting_data_sources=sorted(set(grid_sources + tariff_sources + _profile_sources(profile, "current_tariff"))),
            confidence="High" if tariff_sources and grid_sources else "Medium" if available else "Low",
            data_quality=_combined_quality(
                _quality(provider_map, MeasurementType.GRID_IMPORT),
                _quality(provider_map, MeasurementType.TARIFF_DATA, fallback="declared" if profile.current_tariff else "unknown"),
            ),
        )

    def _energy_timeline_readiness(
        self,
        provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    ) -> HomeCapability:
        timeline_types = [
            MeasurementType.SOLAR_GENERATION,
            MeasurementType.HOUSEHOLD_CONSUMPTION,
            MeasurementType.GRID_IMPORT,
            MeasurementType.GRID_EXPORT,
            MeasurementType.BATTERY_CHARGE,
            MeasurementType.BATTERY_DISCHARGE,
        ]
        sources = sorted(set(source for measurement_type in timeline_types for source in _sources(provider_map, measurement_type)))
        available = bool(sources)
        return HomeCapability(
            "energy_timeline_readiness",
            "Energy timeline readiness",
            enabled=True,
            available=available,
            reason=(
                "At least one time-series energy measurement is available."
                if available else
                "Energy timeline needs at least one measurement provider."
            ),
            supporting_data_sources=sources,
            confidence=_best_confidence([_confidence(provider_map, measurement_type) for measurement_type in timeline_types]) if available else "Low",
            data_quality=_best_quality([_quality(provider_map, measurement_type) for measurement_type in timeline_types]),
        )

    def _home_intelligence_readiness(self, capabilities: list[HomeCapability]) -> HomeCapability:
        by_id = {capability.capability_id: capability for capability in capabilities}
        billing = by_id["billing_analysis"]
        analytical = [
            by_id["solar_generation_analysis"],
            by_id["household_consumption_analysis"],
            by_id["grid_import_export_analysis"],
            by_id["ev_charging_analysis"],
            by_id["battery_analysis"],
        ]
        available = billing.available and any(capability.available for capability in analytical)
        sources = sorted(set(billing.supporting_data_sources + [source for capability in analytical for source in capability.supporting_data_sources]))
        return HomeCapability(
            "home_intelligence_readiness",
            "Home intelligence readiness",
            enabled=True,
            available=available,
            reason=(
                "Billing context and at least one household energy capability are available."
                if available else
                "Home Intelligence needs billing context plus at least one energy capability."
            ),
            supporting_data_sources=sources,
            confidence=_combined_confidence(billing.confidence, _best_confidence([capability.confidence for capability in analytical if capability.available])) if available else "Low",
            data_quality=_best_quality([billing.data_quality] + [capability.data_quality for capability in analytical]),
        )


def detect_home_capabilities(
    profile: Optional[HomeProfile] = None,
    registry: Optional[MeasurementProviderRegistry] = None,
) -> HomeCapabilities:
    return HomeCapabilityDetector().detect(profile, registry)


def _provider_capability_map(
    registry: MeasurementProviderRegistry,
) -> dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]]:
    result: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]] = {}
    for provider in registry.providers:
        for capability in provider.capabilities():
            if not capability.available:
                continue
            result.setdefault(capability.measurement_type, []).append((provider, capability))
    return result


def _sources(
    provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    measurement_type: MeasurementType,
) -> list[str]:
    return sorted(
        {
            f"provider:{provider.provider_id}:{measurement_type.value}"
            for provider, _capability in provider_map.get(measurement_type, [])
        }
    )


def _profile_sources(profile: HomeProfile, *fields: str) -> list[str]:
    sources = []
    for field_name in fields:
        value = getattr(profile, field_name)
        if value not in (None, "", False):
            sources.append(f"home_profile:{field_name}")
    return sources


def _confidence(
    provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    measurement_type: MeasurementType,
) -> str:
    qualities = [capability.quality for _provider, capability in provider_map.get(measurement_type, [])]
    if MeasurementQuality.MEASURED in qualities:
        return "High"
    if MeasurementQuality.CALCULATED in qualities or MeasurementQuality.ESTIMATED in qualities:
        return "Medium"
    return "Low"


def _quality(
    provider_map: dict[MeasurementType, list[tuple[MeasurementProvider, MeasurementCapability]]],
    measurement_type: MeasurementType,
    fallback: str = "unknown",
) -> str:
    qualities = [capability.quality for _provider, capability in provider_map.get(measurement_type, [])]
    if MeasurementQuality.MEASURED in qualities:
        return MeasurementQuality.MEASURED.value
    if MeasurementQuality.CALCULATED in qualities:
        return MeasurementQuality.CALCULATED.value
    if MeasurementQuality.ESTIMATED in qualities:
        return MeasurementQuality.ESTIMATED.value
    return fallback


def _combined_confidence(*values: str) -> str:
    ranks = {"High": 3, "Medium": 2, "Low": 1}
    present = [value for value in values if value]
    if not present:
        return "Low"
    return min(present, key=lambda value: ranks.get(value, 1))


def _best_confidence(values: Iterable[str]) -> str:
    ranks = {"High": 3, "Medium": 2, "Low": 1}
    return max(values, key=lambda value: ranks.get(value, 1), default="Low")


def _combined_quality(*values: str) -> str:
    present = [value for value in values if value != "unknown"]
    if not present:
        return "unknown"
    order = {"measured": 3, "calculated": 2, "estimated": 1, "declared": 1}
    return min(present, key=lambda value: order.get(value, 0))


def _best_quality(values: Iterable[str]) -> str:
    order = {"measured": 4, "calculated": 3, "estimated": 2, "declared": 1, "unknown": 0}
    return max(values, key=lambda value: order.get(value, 0), default="unknown")
