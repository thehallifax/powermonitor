#!/usr/bin/env python3
"""Home Assistant provider adapter for the Provider Runtime."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
DEFAULT_HOME_ASSISTANT_URL = "http://192.168.4.96:8123"

from engine.provider_framework import (
    ProviderCapability,
    ProviderFreshness,
    ProviderObservation,
    ProviderObservationQuality,
    ProviderObservationType,
    ProviderType,
)
from engine.provider_runtime import ProviderAuthentication, ProviderSchedule


class HomeAssistantAPIError(RuntimeError):
    pass


class HomeAssistantAdapter:
    """ProviderRuntime adapter for Home Assistant's REST API."""

    provider_id = "home_assistant"
    schedule = ProviderSchedule.EVERY_5_MINUTES

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        request_json: Optional[Callable[[str, str, str], Any]] = None,
    ) -> None:
        self.base_url = (base_url or home_assistant_url()).rstrip("/")
        self.token = token if token is not None else home_assistant_token()
        self._request_json = request_json
        self._last_entities: list[dict[str, Any]] = []
        self._last_version: Optional[str] = None

    def authenticate(self) -> ProviderAuthentication:
        if not self.base_url:
            return ProviderAuthentication(False, refresh_required=True, method="long_lived_access_token", message="Home Assistant URL is not configured")
        if not self.token:
            return ProviderAuthentication(False, refresh_required=True, method="long_lived_access_token", message="Home Assistant long-lived access token is not configured")
        return ProviderAuthentication(True, method="long_lived_access_token")

    def refresh_session(self) -> ProviderAuthentication:
        return self.authenticate()

    def test_connection(self) -> dict[str, Any]:
        started = monotonic()
        warnings: list[str] = []
        try:
            auth = self.authenticate()
            if not auth.authenticated:
                raise PermissionError(auth.message or "Home Assistant authentication is not configured")
            config = self._get("api/config")
            states = self._states()
            version = str(config.get("version") or "unknown") if isinstance(config, dict) else "unknown"
            capabilities = discover_capabilities(states)
            self._last_entities = states
            self._last_version = version
            success = True
            authentication_status = "authenticated"
        except Exception as exc:
            success = False
            authentication_status = "not_authenticated"
            version = "unknown"
            capabilities = []
            states = []
            warnings.append(home_assistant_error_message(exc))
        return {
            "success": success,
            "authentication_status": authentication_status,
            "provider_version": f"home-assistant-{version}",
            "available_datasets": [capability.value for capability in capabilities],
            "warnings": warnings,
            "response_time_ms": int((monotonic() - started) * 1000),
            "http_status": "ok" if success else "failed",
            "measurements": len(states),
            "version": version,
        }

    def discover_capabilities(self) -> list[ProviderCapability]:
        if self._last_entities:
            return discover_capabilities(self._last_entities)
        if self.authenticate().authenticated:
            return supported_capabilities()
        return []

    def collect_observations(self) -> list[ProviderObservation]:
        auth = self.authenticate()
        if not auth.authenticated:
            raise PermissionError(auth.message or "Home Assistant authentication is not configured")
        states = self._states()
        self._last_entities = states
        return observations_from_entities(states)

    def collect_live_state(self) -> dict[str, Any]:
        return {
            "entity_count": len(self._last_entities),
            "capabilities": [capability.value for capability in self.discover_capabilities()],
        }

    def collect_diagnostics(self) -> dict[str, Any]:
        return {
            "url_configured": bool(self.base_url),
            "token_configured": bool(self.token),
            "entity_count": len(self._last_entities),
            "version": self._last_version,
            "schedule": self.schedule.value,
        }

    def _states(self) -> list[dict[str, Any]]:
        payload = self._get("api/states")
        if not isinstance(payload, list):
            raise HomeAssistantAPIError("Home Assistant returned an invalid states response")
        return [item for item in payload if isinstance(item, dict)]

    def _get(self, path: str) -> Any:
        if self._request_json:
            return self._request_json(self.base_url, self.token, path)
        url = urljoin(self.base_url + "/", path)
        request = Request(url, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PermissionError(f"Home Assistant returned HTTP {exc.code}") from exc
            raise HomeAssistantAPIError(f"Home Assistant returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise HomeAssistantAPIError("Home Assistant network failure") from exc


def home_assistant_url() -> str:
    settings = load_json(SETTINGS_PATH)
    return str(settings.get("home_assistant_url") or DEFAULT_HOME_ASSISTANT_URL)


def home_assistant_token() -> str:
    for name in ("HOME_ASSISTANT_TOKEN", "HA_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    secrets = load_json(SECRETS_PATH)
    return str(secrets.get("home_assistant_token") or secrets.get("ha_token") or "")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_capabilities(entities: list[dict[str, Any]]) -> list[ProviderCapability]:
    capabilities: set[ProviderCapability] = set()
    for entity in entities:
        mapped = capability_for_entity(entity)
        if mapped:
            capabilities.add(mapped)
    return sorted(capabilities, key=lambda capability: capability.value)


def supported_capabilities() -> list[ProviderCapability]:
    return [
        ProviderCapability.BATTERY_STATE,
        ProviderCapability.DAILY_SUMMARY,
        ProviderCapability.EV_SESSIONS,
        ProviderCapability.INTERVAL_HISTORY,
        ProviderCapability.LIVE_POWER,
    ]


def observations_from_entities(entities: list[dict[str, Any]]) -> list[ProviderObservation]:
    observations = []
    received_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for entity in entities:
        capability = capability_for_entity(entity)
        if not capability:
            continue
        observation = observation_from_entity(entity, capability, received_at)
        if observation:
            observations.append(observation)
    return observations


def capability_for_entity(entity: dict[str, Any]) -> Optional[ProviderCapability]:
    entity_id = str(entity.get("entity_id") or "").lower()
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    unit = str(attributes.get("unit_of_measurement") or "").lower()
    device_class = str(attributes.get("device_class") or "").lower()
    friendly = str(attributes.get("friendly_name") or entity_id).lower()
    haystack = f"{entity_id} {friendly}"
    if "battery" in haystack and ("%" in unit or device_class == "battery"):
        return ProviderCapability.BATTERY_STATE
    if "ev" in haystack or "charger" in haystack or "ocpp" in haystack:
        if device_class in {"power", "energy"} or unit in {"w", "kw", "wh", "kwh"}:
            return ProviderCapability.EV_SESSIONS
    if "solar" in haystack or "pv" in haystack or "production" in haystack:
        if device_class in {"power", "energy"} or unit in {"w", "kw", "wh", "kwh"}:
            return ProviderCapability.DAILY_SUMMARY
    if device_class == "power" or unit in {"w", "kw"}:
        return ProviderCapability.LIVE_POWER
    if device_class == "energy" or unit in {"wh", "kwh"}:
        return ProviderCapability.INTERVAL_HISTORY
    return None


def observation_from_entity(entity: dict[str, Any], capability: ProviderCapability, received_at: str) -> Optional[ProviderObservation]:
    value = parse_numeric(entity.get("state"))
    if value is None:
        return None
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    entity_id = str(entity.get("entity_id") or "unknown_entity")
    observed_at = str(entity.get("last_updated") or entity.get("last_changed") or received_at).replace("Z", "+00:00")
    observation_type = {
        ProviderCapability.DAILY_SUMMARY: ProviderObservationType.SOLAR_GENERATION,
        ProviderCapability.INTERVAL_HISTORY: ProviderObservationType.GRID_IMPORT,
        ProviderCapability.EV_SESSIONS: ProviderObservationType.EV_SESSION,
        ProviderCapability.BATTERY_STATE: ProviderObservationType.COLLECTOR_HEALTH,
        ProviderCapability.LIVE_POWER: ProviderObservationType.COLLECTOR_HEALTH,
    }.get(capability, ProviderObservationType.COLLECTOR_HEALTH)
    return ProviderObservation(
        capability=capability,
        name=entity_id,
        value=value,
        unit=str(attributes.get("unit_of_measurement") or "") or None,
        provider_id="home_assistant",
        provider_type=ProviderType.HOME_AUTOMATION,
        observation_type=observation_type,
        observed_at=observed_at,
        received_at=received_at,
        freshness=ProviderFreshness(state="fresh", observed_at=observed_at, age_minutes=None),
        quality=ProviderObservationQuality.MEDIUM,
        provenance="HomeAssistantAdapter",
        raw_reference=entity_id,
        note=str(attributes.get("friendly_name") or entity_id),
    )


def parse_numeric(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if text.lower() in {"", "unknown", "unavailable", "none"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def home_assistant_error_message(exc: BaseException) -> str:
    message = str(exc)
    if isinstance(exc, PermissionError):
        return message or "Home Assistant authentication failed"
    if isinstance(exc, HomeAssistantAPIError):
        return message
    return message or "Home Assistant connection failed"
