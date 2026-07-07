#!/usr/bin/env python3
"""GoodWe local inverter measurement provider."""

from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime
from typing import Any, Callable, Optional

from .measurement_framework import (
    MeasurementCapability,
    MeasurementMetadata,
    MeasurementQuality,
    MeasurementType,
    UniversalMeasurement,
)


DEFAULT_GOODWE_PORT = 8899


class GoodWeApiError(RuntimeError):
    """Raised when local GoodWe runtime data cannot be read."""


class GoodWeMeasurementProvider:
    """Expose local GoodWe inverter runtime data as universal measurements."""

    provider_id = "goodwe"
    provider_name = "GoodWe"

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_GOODWE_PORT,
        timeout_seconds: float = 5.0,
        runtime_reader: Optional[Callable[[], dict[str, Any]]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("GoodWe host is required")
        self.host = str(host).strip()
        self.port = int(port)
        self.timeout_seconds = timeout_seconds
        self.runtime_reader = runtime_reader
        self.now = now or (lambda: datetime.now().astimezone())
        self._runtime_cache: Optional[dict[str, Any]] = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GoodWeMeasurementProvider":
        host = config.get("host") or config.get("endpoint") or config.get("goodwe_host")
        port = int(config.get("port") or config.get("goodwe_port") or DEFAULT_GOODWE_PORT)
        return cls(str(host or ""), port=port)

    def capabilities(self) -> list[MeasurementCapability]:
        try:
            data = self._runtime_data()
            measurements = self._measurements_from_runtime_data(data, self._timestamp(data))
        except GoodWeApiError:
            measurements = []
        return _capabilities_from_measurements(measurements)

    def measurements(self, start: date, end: date) -> list[UniversalMeasurement]:
        data = self._runtime_data()
        timestamp = self._timestamp(data)
        observed_day = _date_from_timestamp(timestamp)
        if observed_day < start or observed_day > end:
            return []
        return self._measurements_from_runtime_data(data, timestamp)

    def _runtime_data(self) -> dict[str, Any]:
        if self._runtime_cache is not None:
            return self._runtime_cache
        try:
            data = self.runtime_reader() if self.runtime_reader else _read_runtime_data(
                self.host,
                self.port,
                self.timeout_seconds,
            )
        except Exception as exc:
            raise GoodWeApiError(str(exc)) from exc
        if not isinstance(data, dict):
            raise GoodWeApiError("GoodWe runtime data was not a dictionary")
        self._runtime_cache = data
        return data

    def _timestamp(self, data: dict[str, Any]) -> str:
        timestamp = _first_value(data, ("timestamp", "time", "datetime", "read_at", "observed_at"))
        if timestamp:
            return str(timestamp)
        return self.now().isoformat(timespec="seconds")

    def _measurements_from_runtime_data(
        self,
        data: dict[str, Any],
        timestamp: str,
    ) -> list[UniversalMeasurement]:
        items: list[UniversalMeasurement] = []
        solar_day = _number(_first_value(data, ("e_day", "eday", "daily_generation", "today_pv_generation")))
        if solar_day is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    solar_day,
                    "kWh",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"goodwe_field": _first_key(data, ("e_day", "eday", "daily_generation", "today_pv_generation")), "measurement_kind": "daily_energy"},
                )
            )

        solar_power = _number(_first_value(data, ("ppv", "pv_power", "p_pv", "total_pv_power", "solar_power")))
        if solar_power is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    max(solar_power, 0.0),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"goodwe_field": _first_key(data, ("ppv", "pv_power", "p_pv", "total_pv_power", "solar_power")), "measurement_kind": "current_power"},
                )
            )

        household_power = _number(_first_value(data, ("house_consumption", "load_power", "p_load", "total_load", "consumption_power")))
        if household_power is not None:
            items.append(
                self._measurement(
                    MeasurementType.HOUSEHOLD_CONSUMPTION,
                    abs(household_power),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"goodwe_field": _first_key(data, ("house_consumption", "load_power", "p_load", "total_load", "consumption_power")), "measurement_kind": "current_power"},
                )
            )

        direct_import = _number(_first_value(data, ("grid_import_power", "p_grid_import", "grid_import", "meter_import_power")))
        direct_export = _number(_first_value(data, ("grid_export_power", "p_grid_export", "grid_export", "meter_export_power")))
        signed_grid = _number(_first_value(data, ("pgrid", "p_grid", "grid_power", "meter_active_power_total", "active_power")))
        if direct_import is not None and direct_import > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    direct_import,
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"goodwe_field": _first_key(data, ("grid_import_power", "p_grid_import", "grid_import", "meter_import_power")), "measurement_kind": "current_power"},
                )
            )
        elif signed_grid is not None and signed_grid > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    signed_grid,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"goodwe_field": _first_key(data, ("pgrid", "p_grid", "grid_power", "meter_active_power_total", "active_power")), "measurement_kind": "current_power", "calculation": "positive_grid_power_as_import"},
                )
            )
        if direct_export is not None and direct_export > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_EXPORT,
                    direct_export,
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"goodwe_field": _first_key(data, ("grid_export_power", "p_grid_export", "grid_export", "meter_export_power")), "measurement_kind": "current_power"},
                )
            )
        elif signed_grid is not None and signed_grid < 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_EXPORT,
                    abs(signed_grid),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"goodwe_field": _first_key(data, ("pgrid", "p_grid", "grid_power", "meter_active_power_total", "active_power")), "measurement_kind": "current_power", "calculation": "negative_grid_power_as_export"},
                )
            )

        battery_power = _number(_first_value(data, ("battery_power", "pbattery", "p_battery", "battery_w")))
        if battery_power is not None and battery_power > 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_DISCHARGE,
                    battery_power,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"goodwe_field": _first_key(data, ("battery_power", "pbattery", "p_battery", "battery_w")), "measurement_kind": "current_power", "calculation": "positive_battery_power_as_discharge"},
                )
            )
        elif battery_power is not None and battery_power < 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_CHARGE,
                    abs(battery_power),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"goodwe_field": _first_key(data, ("battery_power", "pbattery", "p_battery", "battery_w")), "measurement_kind": "current_power", "calculation": "negative_battery_power_as_charge"},
                )
            )
        return items

    def _measurement(
        self,
        measurement_type: MeasurementType,
        value: float,
        unit: str,
        timestamp: str,
        quality: MeasurementQuality,
        attributes: dict[str, Any],
    ) -> UniversalMeasurement:
        metadata = MeasurementMetadata(
            provider_id=self.provider_id,
            source="goodwe_local",
            timestamp=timestamp,
            quality=quality,
            source_record_id=f"{self.provider_id}:{timestamp}:{attributes.get('goodwe_field')}",
        )
        attributes = {"port": self.port, **attributes}
        return UniversalMeasurement(measurement_type, value, unit, metadata, attributes)


def _read_runtime_data(host: str, port: int, timeout_seconds: float) -> dict[str, Any]:
    try:
        goodwe = importlib.import_module("goodwe")
    except ImportError as exc:
        raise GoodWeApiError("Install the optional goodwe package to read GoodWe local runtime data.") from exc

    async def read() -> dict[str, Any]:
        inverter = await goodwe.connect(host, port=port, timeout=timeout_seconds)
        data = await inverter.read_runtime_data()
        return dict(data)

    return asyncio.run(read())


def _capabilities_from_measurements(measurements: list[UniversalMeasurement]) -> list[MeasurementCapability]:
    capabilities: dict[tuple[MeasurementType, str], MeasurementCapability] = {}
    for measurement in measurements:
        key = (measurement.measurement_type, measurement.unit)
        existing = capabilities.get(key)
        quality = measurement.quality
        if existing and existing.quality == MeasurementQuality.MEASURED:
            quality = existing.quality
        capabilities[key] = MeasurementCapability(
            measurement.measurement_type,
            measurement.unit,
            quality,
            note=f"Available from GoodWe local runtime field {measurement.attributes.get('goodwe_field')}.",
        )
    return sorted(capabilities.values(), key=lambda capability: (capability.measurement_type.value, capability.unit))


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        if key in data and data.get(key) is not None:
            return key
    return None


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    key = _first_key(data, keys)
    return data.get(key) if key else None


def _date_from_timestamp(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).date()


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
