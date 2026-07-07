#!/usr/bin/env python3
"""Enphase Envoy local measurement provider."""

from __future__ import annotations

import json
import ssl
from datetime import date, datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .measurement_framework import (
    MeasurementCapability,
    MeasurementMetadata,
    MeasurementQuality,
    MeasurementType,
    UniversalMeasurement,
)


PRODUCTION_ENDPOINT = "/production.json"
METER_READINGS_ENDPOINT = "/ivp/meters/readings"


class EnphaseApiError(RuntimeError):
    """Raised when local Enphase Envoy data cannot be read."""


class EnphaseMeasurementProvider:
    """Expose local Enphase Envoy data as universal measurements."""

    provider_id = "enphase"
    provider_name = "Enphase"

    def __init__(
        self,
        host: str,
        *,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_https: bool = True,
        timeout_seconds: float = 5.0,
        http_get: Optional[Callable[[str, dict[str, str]], dict[str, Any]]] = None,
        runtime_reader: Optional[Callable[[], dict[str, Any]]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("Enphase Envoy host is required")
        self.base_url = _normalise_base_url(host, use_https)
        self.token = token
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get
        self.runtime_reader = runtime_reader
        self.now = now or (lambda: datetime.now().astimezone())
        self._runtime_cache: Optional[dict[str, Any]] = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "EnphaseMeasurementProvider":
        host = config.get("host") or config.get("endpoint") or config.get("enphase_host")
        token = config.get("token") or config.get("access_token") or config.get("enphase_token")
        username = config.get("username") or config.get("enphase_username")
        password = config.get("password") or config.get("enphase_password")
        use_https = bool(config.get("use_https", True))
        return cls(str(host or ""), token=token, username=username, password=password, use_https=use_https)

    def capabilities(self) -> list[MeasurementCapability]:
        try:
            data = self._runtime_data()
            measurements = self._measurements_from_runtime_data(data, self._timestamp(data))
        except EnphaseApiError:
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
            data = self.runtime_reader() if self.runtime_reader else self._fetch_local_payloads()
        except Exception as exc:
            raise EnphaseApiError(str(exc)) from exc
        if not isinstance(data, dict):
            raise EnphaseApiError("Enphase Envoy runtime data was not a dictionary")
        self._runtime_cache = data
        return data

    def _fetch_local_payloads(self) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "PowerMonitor/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payloads: dict[str, Any] = {}
        for key, endpoint in (("production_json", PRODUCTION_ENDPOINT), ("meter_readings", METER_READINGS_ENDPOINT)):
            url = urljoin(self.base_url, endpoint.lstrip("/"))
            try:
                payloads[key] = self.http_get(url, headers) if self.http_get else _http_get_json(url, headers, self.timeout_seconds)
            except Exception as exc:
                payloads[f"{key}_error"] = str(exc)
        if "production_json" not in payloads and "meter_readings" not in payloads:
            error = payloads.get("production_json_error") or payloads.get("meter_readings_error") or "No local Enphase payloads returned."
            raise EnphaseApiError(str(error))
        return payloads

    def _timestamp(self, data: dict[str, Any]) -> str:
        timestamp = _first_value(data, ("timestamp", "time", "datetime", "read_at", "observed_at"))
        if timestamp:
            return _timestamp_text(timestamp)
        production = data.get("production_json") if isinstance(data.get("production_json"), dict) else data
        timestamp = _first_value(production if isinstance(production, dict) else {}, ("timestamp", "time", "last_report_at"))
        if timestamp:
            return _timestamp_text(timestamp)
        readings = data.get("meter_readings")
        if isinstance(readings, list):
            for reading in readings:
                if isinstance(reading, dict) and reading.get("timestamp"):
                    return _timestamp_text(reading["timestamp"])
        return self.now().isoformat(timespec="seconds")

    def _measurements_from_runtime_data(
        self,
        data: dict[str, Any],
        timestamp: str,
    ) -> list[UniversalMeasurement]:
        items: list[UniversalMeasurement] = []
        production_payload = data.get("production_json") if "production_json" in data else data
        if isinstance(production_payload, dict):
            items.extend(self._measurements_from_production_json(production_payload, timestamp))
        meter_payload = data.get("meter_readings")
        if isinstance(meter_payload, list):
            existing = {(item.measurement_type, item.unit) for item in items}
            for item in self._measurements_from_meter_readings(meter_payload, timestamp):
                if (item.measurement_type, item.unit) not in existing:
                    items.append(item)
                    existing.add((item.measurement_type, item.unit))
        return items

    def _measurements_from_production_json(self, payload: dict[str, Any], timestamp: str) -> list[UniversalMeasurement]:
        items: list[UniversalMeasurement] = []
        production = _first_section(payload.get("production"), ("eim", "inverters", "microinverters"))
        if production:
            solar_today = _number(_first_value(production, ("whToday", "wh_today", "wattHoursToday")))
            if solar_today is not None:
                items.append(
                    self._measurement(
                        MeasurementType.SOLAR_GENERATION,
                        solar_today / 1000.0,
                        "kWh",
                        timestamp,
                        MeasurementQuality.MEASURED,
                        {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "production", "enphase_field": "whToday", "measurement_kind": "daily_energy", "source_unit": "Wh"},
                    )
                )
            solar_power = _number(_first_value(production, ("wNow", "wattsNow", "activePower")))
            if solar_power is not None:
                items.append(
                    self._measurement(
                        MeasurementType.SOLAR_GENERATION,
                        max(solar_power, 0.0),
                        "W",
                        timestamp,
                        MeasurementQuality.MEASURED,
                        {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "production", "enphase_field": "wNow", "measurement_kind": "current_power"},
                    )
                )

        total_consumption = _consumption_section(payload, "total-consumption")
        if total_consumption:
            consumption_power = _number(_first_value(total_consumption, ("wNow", "wattsNow", "activePower")))
            if consumption_power is not None:
                items.append(
                    self._measurement(
                        MeasurementType.HOUSEHOLD_CONSUMPTION,
                        abs(consumption_power),
                        "W",
                        timestamp,
                        MeasurementQuality.MEASURED,
                        {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "consumption.total-consumption", "enphase_field": "wNow", "measurement_kind": "current_power"},
                    )
                )

        net_consumption = _consumption_section(payload, "net-consumption")
        net_power = _number(_first_value(net_consumption or {}, ("wNow", "wattsNow", "activePower")))
        if net_power is not None and net_power > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    net_power,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "consumption.net-consumption", "enphase_field": "wNow", "measurement_kind": "current_power", "calculation": "positive_net_consumption_as_import"},
                )
            )
        elif net_power is not None and net_power < 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_EXPORT,
                    abs(net_power),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "consumption.net-consumption", "enphase_field": "wNow", "measurement_kind": "current_power", "calculation": "negative_net_consumption_as_export"},
                )
            )

        storage = _first_section(payload.get("storage"), ("acb", "ensemble", "storage"))
        battery_power = _number(_first_value(storage or {}, ("wNow", "wattsNow", "activePower")))
        if battery_power is not None and battery_power > 0:
            items.append(
                self._measurement(
                    MeasurementType.BATTERY_DISCHARGE,
                    battery_power,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "storage", "enphase_field": "wNow", "measurement_kind": "current_power", "calculation": "positive_storage_power_as_discharge"},
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
                    {"endpoint": PRODUCTION_ENDPOINT, "enphase_section": "storage", "enphase_field": "wNow", "measurement_kind": "current_power", "calculation": "negative_storage_power_as_charge"},
                )
            )
        return items

    def _measurements_from_meter_readings(
        self,
        readings: list[dict[str, Any]],
        timestamp: str,
    ) -> list[UniversalMeasurement]:
        items: list[UniversalMeasurement] = []
        production = _meter_reading(readings, "production")
        if production:
            solar_power = _number(_first_value(production, ("activePower", "active_power")))
            if solar_power is not None:
                items.append(
                    self._measurement(
                        MeasurementType.SOLAR_GENERATION,
                        max(solar_power, 0.0),
                        "W",
                        timestamp,
                        MeasurementQuality.MEASURED,
                        {"endpoint": METER_READINGS_ENDPOINT, "enphase_section": "meter.production", "enphase_field": "activePower", "measurement_kind": "current_power"},
                    )
                )
        total_consumption = _meter_reading(readings, "total-consumption")
        if total_consumption:
            consumption_power = _number(_first_value(total_consumption, ("activePower", "active_power")))
            if consumption_power is not None:
                items.append(
                    self._measurement(
                        MeasurementType.HOUSEHOLD_CONSUMPTION,
                        abs(consumption_power),
                        "W",
                        timestamp,
                        MeasurementQuality.MEASURED,
                        {"endpoint": METER_READINGS_ENDPOINT, "enphase_section": "meter.total-consumption", "enphase_field": "activePower", "measurement_kind": "current_power"},
                    )
                )
        net = _meter_reading(readings, "net-consumption")
        net_power = _number(_first_value(net or {}, ("activePower", "active_power")))
        if net_power is not None and net_power < 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    abs(net_power),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"endpoint": METER_READINGS_ENDPOINT, "enphase_section": "meter.net-consumption", "enphase_field": "activePower", "measurement_kind": "current_power", "calculation": "negative_meter_net_power_as_import"},
                )
            )
        elif net_power is not None and net_power > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_EXPORT,
                    net_power,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {"endpoint": METER_READINGS_ENDPOINT, "enphase_section": "meter.net-consumption", "enphase_field": "activePower", "measurement_kind": "current_power", "calculation": "positive_meter_net_power_as_export"},
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
            source="enphase_envoy_local",
            timestamp=timestamp,
            quality=quality,
            source_record_id=f"{self.provider_id}:{timestamp}:{attributes.get('endpoint')}:{attributes.get('enphase_section')}",
        )
        return UniversalMeasurement(measurement_type, value, unit, metadata, attributes)


def _http_get_json(url: str, headers: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout_seconds, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalise_base_url(host: str, use_https: bool) -> str:
    value = str(host).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        scheme = "https" if use_https else "http"
        value = f"{scheme}://{value}"
    return f"{value}/"


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
            note=f"Available from Enphase Envoy local {measurement.attributes.get('endpoint')}.",
        )
    return sorted(capabilities.values(), key=lambda capability: (capability.measurement_type.value, capability.unit))


def _first_section(value: Any, preferred_types: tuple[str, ...]) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    for preferred_type in preferred_types:
        for item in value:
            if isinstance(item, dict) and str(item.get("type") or item.get("measurementType") or "").lower() == preferred_type:
                return item
    return next((item for item in value if isinstance(item, dict)), None)


def _consumption_section(payload: dict[str, Any], measurement_type: str) -> Optional[dict[str, Any]]:
    consumption = payload.get("consumption")
    if isinstance(consumption, dict):
        return consumption if str(consumption.get("measurementType") or "").lower() in {"", measurement_type} else None
    if isinstance(consumption, list):
        for item in consumption:
            if isinstance(item, dict) and str(item.get("measurementType") or item.get("type") or "").lower() == measurement_type:
                return item
    return None


def _meter_reading(readings: list[dict[str, Any]], measurement_type: str) -> Optional[dict[str, Any]]:
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        kind = str(reading.get("measurementType") or reading.get("type") or "").lower()
        if kind == measurement_type:
            return reading
    return None


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        if key in data and data.get(key) is not None:
            return key
    return None


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    key = _first_key(data, keys)
    return data.get(key) if key else None


def _timestamp_text(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    return str(value)


def _date_from_timestamp(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).date()


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
