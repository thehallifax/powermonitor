#!/usr/bin/env python3
"""Universal measurement contracts for source-agnostic household analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Optional, Protocol


class MeasurementType(str, Enum):
    SOLAR_GENERATION = "solar_generation"
    HOUSEHOLD_CONSUMPTION = "household_consumption"
    GRID_IMPORT = "grid_import"
    GRID_EXPORT = "grid_export"
    BATTERY_CHARGE = "battery_charge"
    BATTERY_DISCHARGE = "battery_discharge"
    TARIFF_DATA = "tariff_data"


class MeasurementQuality(str, Enum):
    MEASURED = "measured"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MeasurementMetadata:
    provider_id: str
    source: str
    timestamp: str
    quality: MeasurementQuality = MeasurementQuality.UNKNOWN
    source_record_id: Optional[str] = None
    imported_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quality"] = self.quality.value
        return data


@dataclass(frozen=True)
class MeasurementCapability:
    measurement_type: MeasurementType
    unit: str
    quality: MeasurementQuality = MeasurementQuality.UNKNOWN
    available: bool = True
    note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_type": self.measurement_type.value,
            "unit": self.unit,
            "quality": self.quality.value,
            "available": self.available,
            "note": self.note,
        }


@dataclass(frozen=True)
class UniversalMeasurement:
    measurement_type: MeasurementType
    value: float
    unit: str
    metadata: MeasurementMetadata
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def timestamp(self) -> str:
        return self.metadata.timestamp

    @property
    def provider_id(self) -> str:
        return self.metadata.provider_id

    @property
    def quality(self) -> MeasurementQuality:
        return self.metadata.quality

    def as_dict(self) -> dict[str, Any]:
        return {
            "measurement_type": self.measurement_type.value,
            "value": self.value,
            "unit": self.unit,
            "metadata": self.metadata.as_dict(),
            "attributes": dict(self.attributes),
        }


class MeasurementProvider(Protocol):
    """Minimal provider contract for source-agnostic measurements.

    Providers should keep source-specific API, file, or database handling inside
    their own implementation and expose only typed capabilities and normalized
    measurements to downstream analytics.
    """

    provider_id: str
    provider_name: str

    def capabilities(self) -> list[MeasurementCapability]:
        ...

    def measurements(self, start: date, end: date) -> list[UniversalMeasurement]:
        ...


@dataclass(frozen=True)
class MeasurementProviderRegistry:
    providers: list[MeasurementProvider] = field(default_factory=list)

    def __post_init__(self) -> None:
        provider_ids = [provider.provider_id for provider in self.providers]
        duplicates = sorted({provider_id for provider_id in provider_ids if provider_ids.count(provider_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate measurement provider id: {', '.join(duplicates)}")

    def register(self, provider: MeasurementProvider) -> "MeasurementProviderRegistry":
        if provider.provider_id in self.provider_ids():
            raise ValueError(f"Duplicate measurement provider id: {provider.provider_id}")
        return MeasurementProviderRegistry([*self.providers, provider])

    def get(self, provider_id: str) -> Optional[MeasurementProvider]:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        return None

    def provider_ids(self) -> list[str]:
        return [provider.provider_id for provider in self.providers]

    def capabilities(self) -> dict[str, list[MeasurementCapability]]:
        return {provider.provider_id: provider.capabilities() for provider in self.providers}

    def supported_measurement_types(self) -> list[MeasurementType]:
        supported = {
            capability.measurement_type
            for provider in self.providers
            for capability in provider.capabilities()
            if capability.available
        }
        return sorted(supported, key=lambda measurement_type: measurement_type.value)

    def missing_capabilities(self, required: Iterable[MeasurementType]) -> list[MeasurementType]:
        supported = set(self.supported_measurement_types())
        missing = {measurement_type for measurement_type in required if measurement_type not in supported}
        return sorted(missing, key=lambda measurement_type: measurement_type.value)

    def supports(self, measurement_type: MeasurementType) -> bool:
        return any(
            capability.available and capability.measurement_type == measurement_type
            for provider in self.providers
            for capability in provider.capabilities()
        )

    def providers_for(self, measurement_type: MeasurementType) -> list[MeasurementProvider]:
        return [
            provider
            for provider in self.providers
            if any(
                capability.available and capability.measurement_type == measurement_type
                for capability in provider.capabilities()
            )
        ]

    def measurements(
        self,
        start: date,
        end: date,
        measurement_types: Optional[Iterable[MeasurementType]] = None,
    ) -> list[UniversalMeasurement]:
        selected = set(measurement_types or [])
        items = [
            measurement
            for provider in self.providers
            for measurement in provider.measurements(start, end)
            if not selected or measurement.measurement_type in selected
        ]
        return sorted(items, key=lambda item: (item.timestamp, item.provider_id, item.measurement_type.value))

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_ids": self.provider_ids(),
            "supported_measurement_types": [
                measurement_type.value for measurement_type in self.supported_measurement_types()
            ],
            "capabilities": {
                provider_id: [capability.as_dict() for capability in capabilities]
                for provider_id, capabilities in self.capabilities().items()
            },
        }


class SolarEdgeMeasurementProvider:
    """Expose normalized SolarEdge-backed readings as universal measurements."""

    provider_id = "solaredge"
    provider_name = "SolarEdge"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def capabilities(self) -> list[MeasurementCapability]:
        return [
            MeasurementCapability(MeasurementType.SOLAR_GENERATION, "kWh", MeasurementQuality.MEASURED),
            MeasurementCapability(MeasurementType.HOUSEHOLD_CONSUMPTION, "kWh", MeasurementQuality.MEASURED),
            MeasurementCapability(MeasurementType.GRID_IMPORT, "kWh", MeasurementQuality.MEASURED),
            MeasurementCapability(MeasurementType.GRID_EXPORT, "kWh", MeasurementQuality.MEASURED),
        ]

    def measurements(self, start: date, end: date) -> list[UniversalMeasurement]:
        start_text = start.isoformat()
        end_text = (end + timedelta(days=1)).isoformat()
        measurements: list[UniversalMeasurement] = []
        for row in self.rows:
            timestamp = str(row.get("measurement_time") or "")
            if timestamp < start_text or timestamp >= end_text:
                continue
            measurements.extend(self._measurements_for_row(row))
        return measurements

    def _measurements_for_row(self, row: dict[str, Any]) -> list[UniversalMeasurement]:
        timestamp = str(row.get("measurement_time") or "")
        metadata = MeasurementMetadata(
            provider_id=self.provider_id,
            source="readings",
            timestamp=timestamp,
            quality=MeasurementQuality.MEASURED,
            source_record_id=timestamp,
        )
        common = {
            "tariff_bucket": row.get("tariff_bucket"),
            "interval_minutes": 15,
        }
        return [
            UniversalMeasurement(
                MeasurementType.SOLAR_GENERATION,
                _float(row.get("production_kwh")),
                "kWh",
                metadata,
                {
                    **common,
                    "power_w": _float(row.get("production_w")),
                    "solar_used_kwh": _float(row.get("solar_to_home_kwh")),
                },
            ),
            UniversalMeasurement(
                MeasurementType.HOUSEHOLD_CONSUMPTION,
                _float(row.get("consumption_kwh")),
                "kWh",
                metadata,
                {**common, "power_w": _float(row.get("consumption_w"))},
            ),
            UniversalMeasurement(
                MeasurementType.GRID_IMPORT,
                _float(row.get("grid_to_home_kwh")),
                "kWh",
                metadata,
                {**common, "power_w": _float(row.get("grid_to_home_w"))},
            ),
            UniversalMeasurement(
                MeasurementType.GRID_EXPORT,
                _float(row.get("solar_to_grid_kwh")),
                "kWh",
                metadata,
                {**common, "power_w": _float(row.get("solar_to_grid_w"))},
            ),
        ]


def measurement_totals(measurements: list[UniversalMeasurement]) -> dict[MeasurementType, float]:
    totals = {measurement_type: 0.0 for measurement_type in MeasurementType}
    for measurement in measurements:
        totals[measurement.measurement_type] += float(measurement.value or 0.0)
    return totals


def measurement_quality_summary(measurements: list[UniversalMeasurement]) -> dict[str, int]:
    summary = {quality.value: 0 for quality in MeasurementQuality}
    for measurement in measurements:
        summary[measurement.quality.value] += 1
    return summary


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
