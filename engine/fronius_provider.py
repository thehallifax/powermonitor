#!/usr/bin/env python3
"""Fronius Solar API measurement provider."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
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


POWER_FLOW_ENDPOINT = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"


class FroniusApiError(RuntimeError):
    """Raised when a Fronius Solar API response cannot be used."""


class FroniusMeasurementProvider:
    """Expose local Fronius Solar API values as universal measurements."""

    provider_id = "fronius"
    provider_name = "Fronius"

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: float = 5.0,
        http_get: Optional[Callable[[str], dict[str, Any]]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not str(host or "").strip():
            raise ValueError("Fronius host is required")
        self.base_url = _normalise_base_url(host)
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get
        self.now = now or (lambda: datetime.now().astimezone())

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "FroniusMeasurementProvider":
        host = config.get("host") or config.get("endpoint") or config.get("fronius_host")
        return cls(str(host or ""))

    def capabilities(self) -> list[MeasurementCapability]:
        return [
            MeasurementCapability(
                MeasurementType.SOLAR_GENERATION,
                "kWh",
                MeasurementQuality.MEASURED,
                note="Daily solar energy from Fronius Site.E_Day when available.",
            ),
            MeasurementCapability(
                MeasurementType.SOLAR_GENERATION,
                "W",
                MeasurementQuality.MEASURED,
                note="Current PV power from Fronius Site.P_PV when available.",
            ),
            MeasurementCapability(
                MeasurementType.HOUSEHOLD_CONSUMPTION,
                "W",
                MeasurementQuality.MEASURED,
                note="Current household load from Fronius Site.P_Load when available.",
            ),
            MeasurementCapability(
                MeasurementType.GRID_IMPORT,
                "W",
                MeasurementQuality.CALCULATED,
                note="Current import split from positive Fronius Site.P_Grid values.",
            ),
            MeasurementCapability(
                MeasurementType.GRID_EXPORT,
                "W",
                MeasurementQuality.CALCULATED,
                note="Current export split from negative Fronius Site.P_Grid values.",
            ),
        ]

    def measurements(self, start: date, end: date) -> list[UniversalMeasurement]:
        payload = self._fetch_power_flow()
        timestamp = _timestamp(payload, self.now())
        observed_day = _date_from_timestamp(timestamp)
        if observed_day < start or observed_day > end:
            return []
        site = _site_data(payload)
        return self._measurements_from_site(site, timestamp)

    def _fetch_power_flow(self) -> dict[str, Any]:
        url = urljoin(self.base_url, POWER_FLOW_ENDPOINT)
        payload = self.http_get(url) if self.http_get else _http_get_json(url, self.timeout_seconds)
        status = ((payload.get("Head") or {}).get("Status") or {}) if isinstance(payload, dict) else {}
        code = status.get("Code", 0)
        if code not in (0, "0", None):
            reason = status.get("Reason") or status.get("UserMessage") or "Fronius Solar API returned an error"
            raise FroniusApiError(str(reason))
        return payload

    def _measurements_from_site(self, site: dict[str, Any], timestamp: str) -> list[UniversalMeasurement]:
        items: list[UniversalMeasurement] = []
        solar_day_wh = _number(site.get("E_Day"))
        if solar_day_wh is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    solar_day_wh / 1000.0,
                    "kWh",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {
                        "endpoint": POWER_FLOW_ENDPOINT,
                        "fronius_field": "Site.E_Day",
                        "measurement_kind": "daily_energy",
                        "source_unit": "Wh",
                    },
                )
            )

        solar_power_w = _number(site.get("P_PV"))
        if solar_power_w is not None:
            items.append(
                self._measurement(
                    MeasurementType.SOLAR_GENERATION,
                    max(solar_power_w, 0.0),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {
                        "endpoint": POWER_FLOW_ENDPOINT,
                        "fronius_field": "Site.P_PV",
                        "measurement_kind": "current_power",
                    },
                )
            )

        load_power_w = _number(site.get("P_Load"))
        if load_power_w is not None:
            items.append(
                self._measurement(
                    MeasurementType.HOUSEHOLD_CONSUMPTION,
                    abs(load_power_w),
                    "W",
                    timestamp,
                    MeasurementQuality.MEASURED,
                    {
                        "endpoint": POWER_FLOW_ENDPOINT,
                        "fronius_field": "Site.P_Load",
                        "measurement_kind": "current_power",
                    },
                )
            )

        grid_power_w = _number(site.get("P_Grid"))
        if grid_power_w is not None and grid_power_w > 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_IMPORT,
                    grid_power_w,
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {
                        "endpoint": POWER_FLOW_ENDPOINT,
                        "fronius_field": "Site.P_Grid",
                        "measurement_kind": "current_power",
                        "calculation": "positive_grid_power_as_import",
                    },
                )
            )
        elif grid_power_w is not None and grid_power_w < 0:
            items.append(
                self._measurement(
                    MeasurementType.GRID_EXPORT,
                    abs(grid_power_w),
                    "W",
                    timestamp,
                    MeasurementQuality.CALCULATED,
                    {
                        "endpoint": POWER_FLOW_ENDPOINT,
                        "fronius_field": "Site.P_Grid",
                        "measurement_kind": "current_power",
                        "calculation": "negative_grid_power_as_export",
                    },
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
            source="fronius_solar_api",
            timestamp=timestamp,
            quality=quality,
            source_record_id=f"{self.provider_id}:{timestamp}:{attributes.get('fronius_field')}",
        )
        return UniversalMeasurement(measurement_type, value, unit, metadata, attributes)


def _http_get_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "PowerMonitor/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalise_base_url(host: str) -> str:
    value = str(host).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return f"{value}/"


def _site_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = ((payload.get("Body") or {}).get("Data") or {}) if isinstance(payload, dict) else {}
    site = data.get("Site") or {}
    return site if isinstance(site, dict) else {}


def _timestamp(payload: dict[str, Any], fallback: datetime) -> str:
    timestamp = ((payload.get("Head") or {}).get("Timestamp")) if isinstance(payload, dict) else None
    if timestamp:
        return str(timestamp)
    return fallback.isoformat(timespec="seconds")


def _date_from_timestamp(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).date()


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
