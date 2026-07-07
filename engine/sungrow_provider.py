#!/usr/bin/env python3
"""Sungrow local inverter measurement provider."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Optional

from .measurement_framework import (
    MeasurementCapability,
    MeasurementMetadata,
    MeasurementQuality,
    MeasurementType,
    UniversalMeasurement,
)


DEFAULT_SUNGROW_PORT = 502
DEFAULT_SUNGROW_UNIT_ID = 1


class SungrowApiError(RuntimeError):
    """Raised when local Sungrow runtime data cannot be read."""


class SungrowMeasurementProvider:
    """Expose local Sungrow inverter runtime data as universal measurements."""

    provider_id = "sungrow"
    provider_name = "Sungrow"

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_SUNGROW_PORT,
        protocol: str = "modbus_tcp",
        unit_id: int = DEFAULT_SUNGROW_UNIT_ID,
        timeout_seconds: float = 5.0,
        register_map: Optional[dict[str, Any]] = None,
        runtime_reader: Optional[Callable[[], dict[str, Any]]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("Sungrow host is required")
        self.host = str(host).strip()
        self.port = int(port)
        self.protocol = str(protocol or "modbus_tcp")
        self.unit_id = int(unit_id)
        self.timeout_seconds = timeout_seconds
        self.register_map = register_map or {}
        self.runtime_reader = runtime_reader
        self.now = now or (lambda: datetime.now().astimezone())
        self._runtime_cache: Optional[dict[str, Any]] = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SungrowMeasurementProvider":
        host = config.get("host") or config.get("endpoint") or config.get("sungrow_host")
        port = int(config.get("port") or config.get("sungrow_port") or DEFAULT_SUNGROW_PORT)
        protocol = str(config.get("protocol") or config.get("sungrow_protocol") or "modbus_tcp")
        unit_id = int(config.get("unit_id") or config.get("slave_id") or config.get("sungrow_unit_id") or DEFAULT_SUNGROW_UNIT_ID)
        register_map = config.get("register_map") if isinstance(config.get("register_map"), dict) else None
        return cls(str(host or ""), port=port, protocol=protocol, unit_id=unit_id, register_map=register_map)

    def capabilities(self) -> list[MeasurementCapability]:
        try:
            data = self._runtime_data()
            measurements = self._measurements_from_runtime_data(data, self._timestamp(data))
        except SungrowApiError:
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
            if self.runtime_reader:
                data = self.runtime_reader()
            else:
                data = _read_runtime_data(
                    self.host,
                    self.port,
                    self.protocol,
                    self.unit_id,
                    self.timeout_seconds,
                    self.register_map,
                )
        except Exception as exc:
            raise SungrowApiError(str(exc)) from exc
        if not isinstance(data, dict):
            raise SungrowApiError("Sungrow runtime data was not a dictionary")
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
        solar_day = _number(_first_value(data, ("daily_pv_generation", "e_day", "today_generation", "pv_energy_today")))
        if solar_day is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    solar_day,
                    "kWh",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("daily_pv_generation", "e_day", "today_generation", "pv_energy_today")), "measurement_kind": "daily_energy"},
                )
            )

        solar_power = _number(_first_value(data, ("total_dc_power", "pv_power", "total_pv_power", "p_pv", "solar_power")))
        if solar_power is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    max(solar_power, 0.0),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("total_dc_power", "pv_power", "total_pv_power", "p_pv", "solar_power")), "measurement_kind": "current_power"},
                )
            )

        household_power = _number(_first_value(data, ("load_power", "household_power", "p_load", "total_load", "consumption_power")))
        if household_power is not None:
            items.append(
                self._measurement(
                    MeasurementType.HOUSEHOLD_CONSUMPTION,
                    abs(household_power),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("load_power", "household_power", "p_load", "total_load", "consumption_power")), "measurement_kind": "current_power"},
                )
            )

        direct_import = _number(_first_value(data, ("grid_import_power", "import_power", "p_grid_import", "meter_import_power")))
        direct_export = _number(_first_value(data, ("grid_export_power", "export_power", "p_grid_export", "meter_export_power")))
        signed_grid = _number(_first_value(data, ("meter_active_power", "p_grid", "grid_power", "total_active_power", "active_power")))
        if direct_import is not None and direct_import > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    direct_import,
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("grid_import_power", "import_power", "p_grid_import", "meter_import_power")), "measurement_kind": "current_power"},
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
                    {"sungrow_field": _first_key(data, ("meter_active_power", "p_grid", "grid_power", "total_active_power", "active_power")), "measurement_kind": "current_power", "calculation": "positive_grid_power_as_import"},
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
                    {"sungrow_field": _first_key(data, ("grid_export_power", "export_power", "p_grid_export", "meter_export_power")), "measurement_kind": "current_power"},
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
                    {"sungrow_field": _first_key(data, ("meter_active_power", "p_grid", "grid_power", "total_active_power", "active_power")), "measurement_kind": "current_power", "calculation": "negative_grid_power_as_export"},
                )
            )

        direct_charge = _number(_first_value(data, ("battery_charge_power", "p_battery_charge", "battery_charging_power")))
        direct_discharge = _number(_first_value(data, ("battery_discharge_power", "p_battery_discharge", "battery_discharging_power")))
        signed_battery = _number(_first_value(data, ("battery_power", "p_battery", "pbattery")))
        if direct_charge is not None and direct_charge > 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_CHARGE,
                    direct_charge,
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("battery_charge_power", "p_battery_charge", "battery_charging_power")), "measurement_kind": "current_power"},
                )
            )
        elif signed_battery is not None and signed_battery < 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_CHARGE,
                    abs(signed_battery),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"sungrow_field": _first_key(data, ("battery_power", "p_battery", "pbattery")), "measurement_kind": "current_power", "calculation": "negative_battery_power_as_charge"},
                )
            )
        if direct_discharge is not None and direct_discharge > 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_DISCHARGE,
                    direct_discharge,
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {"sungrow_field": _first_key(data, ("battery_discharge_power", "p_battery_discharge", "battery_discharging_power")), "measurement_kind": "current_power"},
                )
            )
        elif signed_battery is not None and signed_battery > 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_DISCHARGE,
                    signed_battery,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"sungrow_field": _first_key(data, ("battery_power", "p_battery", "pbattery")), "measurement_kind": "current_power", "calculation": "positive_battery_power_as_discharge"},
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
            source="sungrow_local",
            timestamp=timestamp,
            quality=quality,
            source_record_id=f"{self.provider_id}:{timestamp}:{attributes.get('sungrow_field')}",
        )
        attributes = {"port": self.port, "protocol": self.protocol, "unit_id": self.unit_id, **attributes}
        return UniversalMeasurement(measurement_type, value, unit, metadata, attributes)


def _read_runtime_data(
    host: str,
    port: int,
    protocol: str,
    unit_id: int,
    timeout_seconds: float,
    register_map: dict[str, Any],
) -> dict[str, Any]:
    if protocol != "modbus_tcp":
        raise SungrowApiError(f"Unsupported Sungrow local protocol: {protocol}")
    if not register_map:
        raise SungrowApiError("Sungrow local Modbus requires a register_map or injected runtime_reader.")
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError as exc:
        raise SungrowApiError("Install the optional pymodbus package to read Sungrow local Modbus data.") from exc

    client = ModbusTcpClient(host, port=port, timeout=timeout_seconds)
    if not client.connect():
        raise SungrowApiError("Unable to connect to Sungrow local Modbus endpoint.")
    try:
        data: dict[str, Any] = {}
        for field_name, spec in register_map.items():
            register = _register_spec(spec)
            response = client.read_input_registers(register["address"], register["count"], slave=unit_id)
            if response.isError():
                raise SungrowApiError(f"Unable to read Sungrow register for {field_name}.")
            data[field_name] = _decode_registers(response.registers, register["scale"], register["signed"])
        return data
    finally:
        client.close()


def _register_spec(spec: Any) -> dict[str, Any]:
    if isinstance(spec, int):
        return {"address": spec, "count": 1, "scale": 1.0, "signed": False}
    if not isinstance(spec, dict) or "address" not in spec:
        raise SungrowApiError("Invalid Sungrow register map entry.")
    return {
        "address": int(spec["address"]),
        "count": int(spec.get("count") or 1),
        "scale": float(spec.get("scale") or 1.0),
        "signed": bool(spec.get("signed")),
    }


def _decode_registers(registers: list[int], scale: float, signed: bool) -> float:
    raw = 0
    for register in registers:
        raw = (raw << 16) + int(register)
    if signed and registers and raw >= 1 << (16 * len(registers) - 1):
        raw -= 1 << (16 * len(registers))
    return raw * scale


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
            note=f"Available from Sungrow local field {measurement.attributes.get('sungrow_field')}.",
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
