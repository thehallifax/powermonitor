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
from engine.provider_entity_mapping import (
    DEFAULT_PROVIDER_MAPPING_PATH,
    HOME_ASSISTANT_MAPPING_TARGETS,
    build_home_assistant_entity_mapping_plan,
    clean_entity_mappings,
    load_provider_entity_mappings,
    load_provider_mapping_record,
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
        entity_mappings: Optional[dict[str, str]] = None,
        request_json: Optional[Callable[[str, str, str], Any]] = None,
    ) -> None:
        self.base_url = (base_url or home_assistant_url()).rstrip("/")
        self.token = token if token is not None else home_assistant_token()
        self.entity_mappings = (
            clean_entity_mappings(entity_mappings)
            if entity_mappings is not None else
            load_provider_entity_mappings(self.provider_id, path=DEFAULT_PROVIDER_MAPPING_PATH)
        )
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
            mapping_record = load_provider_mapping_record(self.provider_id, path=DEFAULT_PROVIDER_MAPPING_PATH)
            mapping_plan = build_home_assistant_entity_mapping_plan(
                states,
                self.entity_mappings,
                updated_at=str(mapping_record.get("updated_at") or "") or None,
            )
            warnings.extend(mapping_plan.validation.warnings)
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
            "entity_mapping": mapping_plan.as_dict() if success else {},
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
        return observations_from_entities(states, self.entity_mappings)

    def collect_live_state(self) -> dict[str, Any]:
        return {
            "entity_count": len(self._last_entities),
            "capabilities": [capability.value for capability in self.discover_capabilities()],
        }

    def collect_diagnostics(self) -> dict[str, Any]:
        mapping_record = load_provider_mapping_record(self.provider_id, path=DEFAULT_PROVIDER_MAPPING_PATH)
        mapping_plan = build_home_assistant_entity_mapping_plan(
            self._last_entities,
            self.entity_mappings,
            updated_at=str(mapping_record.get("updated_at") or "") or None,
        )
        return {
            "url_configured": bool(self.base_url),
            "token_configured": bool(self.token),
            "entity_count": len(self._last_entities),
            "version": self._last_version,
            "schedule": self.schedule.value,
            "entity_mapping": mapping_plan.as_dict(),
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


def observations_from_entities(entities: list[dict[str, Any]], entity_mappings: Optional[dict[str, str]] = None) -> list[ProviderObservation]:
    observations = []
    received_at = datetime.now().astimezone().isoformat(timespec="seconds")
    selected = clean_entity_mappings(entity_mappings)
    if selected:
        by_entity = {str(entity.get("entity_id") or ""): entity for entity in entities}
        targets = {target.target_id: target for target in HOME_ASSISTANT_MAPPING_TARGETS}
        mapping_plan = build_home_assistant_entity_mapping_plan(entities, selected)
        valid_targets = {
            target_id
            for target_id, status in mapping_plan.validation.target_status.items()
            if status == "configured"
        }
        for target_id, entity_id in selected.items():
            target = targets.get(target_id)
            entity = by_entity.get(entity_id)
            if not target or not entity or target_id not in valid_targets:
                continue
            observation = observation_from_entity(
                entity,
                target.capability,
                received_at,
                observation_type=target.observation_type,
            )
            if observation:
                observations.append(observation)
        return observations
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
        if any(word in haystack for word in ("connected", "plug", "connector", "charging")) and unit == "":
            return ProviderCapability.EV_SESSIONS
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


def observation_from_entity(
    entity: dict[str, Any],
    capability: ProviderCapability,
    received_at: str,
    *,
    observation_type: Optional[ProviderObservationType] = None,
) -> Optional[ProviderObservation]:
    observation_type = observation_type or observation_type_for_entity(entity, capability)
    value = parse_value(entity.get("state"), observation_type)
    if value is None:
        return None
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    entity_id = str(entity.get("entity_id") or "unknown_entity")
    observed_at = str(entity.get("last_updated") or entity.get("last_changed") or received_at).replace("Z", "+00:00")
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


def observation_type_for_entity(entity: dict[str, Any], capability: ProviderCapability) -> ProviderObservationType:
    entity_id = str(entity.get("entity_id") or "").lower()
    attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    unit = str(attributes.get("unit_of_measurement") or "").lower()
    device_class = str(attributes.get("device_class") or "").lower()
    friendly = str(attributes.get("friendly_name") or entity_id).lower()
    haystack = f"{entity_id} {friendly}"
    if capability == ProviderCapability.BATTERY_STATE:
        if "%" in unit or device_class == "battery" or "soc" in haystack or "state of charge" in haystack:
            return ProviderObservationType.BATTERY_SOC
        return ProviderObservationType.BATTERY_POWER
    if capability == ProviderCapability.EV_SESSIONS:
        if any(word in haystack for word in ("connected", "plug", "connector")) and unit == "":
            return ProviderObservationType.EV_CONNECTED_STATE
        if "charging" in haystack and unit == "":
            return ProviderObservationType.EV_CHARGING_STATE
        return ProviderObservationType.EV_CHARGER_LIVE_POWER
    if capability == ProviderCapability.DAILY_SUMMARY:
        return ProviderObservationType.SOLAR_LIVE_POWER if unit in {"w", "kw"} or device_class == "power" else ProviderObservationType.SOLAR_GENERATION
    if capability == ProviderCapability.INTERVAL_HISTORY:
        return ProviderObservationType.GRID_LIVE_POWER if unit in {"w", "kw"} or device_class == "power" else ProviderObservationType.GRID_IMPORT
    if capability == ProviderCapability.LIVE_POWER:
        if "grid" in haystack:
            return ProviderObservationType.GRID_LIVE_POWER
        return ProviderObservationType.HOME_LIVE_POWER
    return ProviderObservationType.LIVE_POWER


def parse_numeric(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if text.lower() in {"", "unknown", "unavailable", "none"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_value(value: Any, observation_type: ProviderObservationType) -> Any:
    if observation_type in {ProviderObservationType.EV_CONNECTED_STATE, ProviderObservationType.EV_CHARGING_STATE, ProviderObservationType.BATTERY_STATE}:
        text = str(value).strip().lower()
        if text in {"on", "true", "connected", "charging", "1"}:
            return True
        if text in {"off", "false", "disconnected", "idle", "not_charging", "0"}:
            return False
        return text or None
    return parse_numeric(value)


def home_assistant_error_message(exc: BaseException) -> str:
    message = str(exc)
    if isinstance(exc, PermissionError):
        return message or "Home Assistant authentication failed"
    if isinstance(exc, HomeAssistantAPIError):
        return message
    return message or "Home Assistant connection failed"
