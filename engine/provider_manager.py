#!/usr/bin/env python3
"""Provider Management layer above the Provider Framework."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .provider_framework import Provider, ProviderCapability, ProviderRegistry, ProviderStatus, ProviderType
from .provider_entity_mapping import (
    DEFAULT_PROVIDER_MAPPING_PATH,
    build_home_assistant_entity_mapping_plan,
    clean_entity_mappings,
    load_provider_entity_mappings,
    load_provider_mapping_record,
    save_provider_entity_mappings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDER_STATE_PATH = PROJECT_ROOT / "config" / "provider_state.json"
DEFAULT_SECRETS_PATH = PROJECT_ROOT / "config" / "secrets.json"
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_SOLAREDGE_COOKIE_PATH = PROJECT_ROOT / "collector" / "solaredge_cookie.txt"


class ProviderConnectionStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    CONNECTED = "CONNECTED"
    SYNCING = "SYNCING"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class ProviderOnboardingState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    READY_TO_CONNECT = "READY_TO_CONNECT"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    SYNCING = "SYNCING"
    WARNING = "WARNING"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class ProviderSyncState(str, Enum):
    NEVER_SYNCED = "NEVER_SYNCED"
    SYNCED = "SYNCED"
    SYNCING = "SYNCING"
    STALE = "STALE"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


SENSITIVE_FIELDS = {"username", "password", "cookie", "api_key", "token", "secret"}
SOLAREDGE_PRIMARY_FIELDS = {"username", "password"}
SOLAREDGE_COOKIE_FALLBACK_FIELDS = {"cookie"}


@dataclass(frozen=True)
class ProviderCredential:
    field: str
    label: str
    required: bool = False
    sensitive: bool = True
    configured: bool = False
    value: Optional[str] = None

    def as_dict(self, include_sensitive: bool = False) -> dict[str, Any]:
        data = {
            "field": self.field,
            "label": self.label,
            "required": self.required,
            "sensitive": self.sensitive,
            "configured": self.configured,
        }
        if include_sensitive and not self.sensitive:
            data["value"] = self.value
        elif self.value is not None:
            data["value"] = "REDACTED" if self.sensitive else self.value
        return data


@dataclass(frozen=True)
class ProviderConfiguration:
    provider_id: str
    provider_name: str
    provider_type: ProviderType
    lifecycle_state: ProviderConnectionStatus
    credentials: list[ProviderCredential] = field(default_factory=list)
    editable_fields: list[str] = field(default_factory=list)
    entity_mapping: Optional[dict[str, Any]] = None
    enabled: bool = True
    notes: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "provider_type": self.provider_type.value,
            "lifecycle_state": self.lifecycle_state.value,
            "credentials": [credential.as_dict() for credential in self.credentials],
            "editable_fields": list(self.editable_fields),
            "entity_mapping": self.entity_mapping,
            "enabled": self.enabled,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ProviderHealth:
    status: ProviderConnectionStatus
    collector_status: str = "unknown"
    last_successful_sync: Optional[str] = None
    last_failed_sync: Optional[str] = None
    failure_count: int = 0
    last_error: Optional[str] = None
    freshness: Optional[dict[str, Any]] = None
    entity_mapping_health: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class ProviderDiagnostics:
    provider_id: str
    last_successful_sync: Optional[str] = None
    last_failed_sync: Optional[str] = None
    failure_count: int = 0
    last_error: Optional[str] = None
    freshness: Optional[dict[str, Any]] = None
    observation_count: int = 0
    live_observation_types: list[str] = field(default_factory=list)
    promotion_count: int = 0
    collector_version: str = "PowerMonitor local collector"
    warnings: list[str] = field(default_factory=list)
    entity_mapping: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderCapabilitySummary:
    provider_id: str
    capabilities: list[ProviderCapability] = field(default_factory=list)
    detected: bool = False
    missing: list[ProviderCapability] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capabilities": [capability.value for capability in self.capabilities],
            "detected": self.detected,
            "missing": [capability.value for capability in self.missing],
        }


@dataclass(frozen=True)
class ProviderTestResult:
    provider_id: str
    success: bool
    authentication_status: str
    capability_detection: ProviderCapabilitySummary
    provider_version: str
    available_datasets: list[str]
    warnings: list[str]
    response_time_ms: int
    status: str = "success"
    user_message: str = "Connection test succeeded."
    next_step: str = "No action needed right now."
    technical_details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "success": self.success,
            "authentication_status": self.authentication_status,
            "capability_detection": self.capability_detection.as_dict(),
            "provider_version": self.provider_version,
            "available_datasets": list(self.available_datasets),
            "warnings": list(self.warnings),
            "response_time_ms": self.response_time_ms,
            "status": self.status,
            "user_message": self.user_message,
            "next_step": self.next_step,
            "technical_details": dict(self.technical_details),
        }


@dataclass(frozen=True)
class ProviderActionResult:
    provider_id: str
    action: str
    success: bool
    status: str
    user_message: str
    next_step: str
    technical_details: dict[str, Any] = field(default_factory=dict)
    provider: Optional[dict[str, Any]] = None
    configuration: Optional[dict[str, Any]] = None
    test: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "action": self.action,
            "success": self.success,
            "status": self.status,
            "user_message": self.user_message,
            "next_step": self.next_step,
            "technical_details": dict(self.technical_details),
            "provider": self.provider,
            "configuration": self.configuration,
            "test": self.test,
        }


@dataclass(frozen=True)
class ProviderOnboardingGuide:
    provider_id: str
    state: ProviderOnboardingState
    state_label: str
    connected: bool
    healthy: bool
    data_available: bool
    last_successful_sync: Optional[str]
    next_scheduled_sync: str
    sync_frequency: str
    runtime_state: str
    collector_duration: str
    consecutive_failures: int
    retry_countdown: str
    health_score: int
    health_factors: dict[str, str]
    required_information: list[str]
    why_required: str
    connection_status: str
    health_status: str
    next_step: str
    common_failures: list[str]
    recovery_steps: list[str]
    technical_details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "state": self.state.value,
            "state_label": self.state_label,
            "connected": self.connected,
            "healthy": self.healthy,
            "data_available": self.data_available,
            "last_successful_sync": self.last_successful_sync,
            "next_scheduled_sync": self.next_scheduled_sync,
            "sync_frequency": self.sync_frequency,
            "runtime_state": self.runtime_state,
            "collector_duration": self.collector_duration,
            "consecutive_failures": self.consecutive_failures,
            "retry_countdown": self.retry_countdown,
            "health_score": self.health_score,
            "health_factors": dict(self.health_factors),
            "required_information": list(self.required_information),
            "why_required": self.why_required,
            "connection_status": self.connection_status,
            "health_status": self.health_status,
            "next_step": self.next_step,
            "common_failures": list(self.common_failures),
            "recovery_steps": list(self.recovery_steps),
            "technical_details": dict(self.technical_details),
        }


@dataclass(frozen=True)
class ManagedProvider:
    provider_id: str
    name: str
    provider_type: ProviderType
    lifecycle_state: ProviderConnectionStatus
    configuration: ProviderConfiguration
    health: ProviderHealth
    sync_state: ProviderSyncState
    diagnostics: ProviderDiagnostics
    capability_summary: ProviderCapabilitySummary
    onboarding: ProviderOnboardingGuide
    actions: list[str]
    source: Optional[str] = None
    note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "provider_type": self.provider_type.value,
            "lifecycle_state": self.lifecycle_state.value,
            "configuration": self.configuration.as_dict(),
            "health": self.health.as_dict(),
            "sync_state": self.sync_state.value,
            "diagnostics": self.diagnostics.as_dict(),
            "capability_summary": self.capability_summary.as_dict(),
            "onboarding": self.onboarding.as_dict(),
            "actions": list(self.actions),
            "source": self.source,
            "note": self.note,
        }


class ProviderManager:
    """Build provider lifecycle state without changing provider adapters."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        health: Optional[dict[str, Any]] = None,
        provider_observations: Optional[dict[str, Any]] = None,
        promotion_summary: Optional[dict[str, Any]] = None,
        runtime_status: Optional[dict[str, Any]] = None,
        connection_testers: Optional[dict[str, Any]] = None,
        reconnect_handlers: Optional[dict[str, Any]] = None,
        settings: Any = None,
        provider_entity_mappings: Optional[dict[str, Any]] = None,
        entity_mapping_store_path: Path = DEFAULT_PROVIDER_MAPPING_PATH,
        provider_state_path: Path = DEFAULT_PROVIDER_STATE_PATH,
        secrets_path: Path = DEFAULT_SECRETS_PATH,
        settings_path: Path = DEFAULT_SETTINGS_PATH,
        solaredge_cookie_path: Path = DEFAULT_SOLAREDGE_COOKIE_PATH,
    ) -> None:
        self.registry = registry
        self.health = health or {}
        self.provider_observations = provider_observations or {}
        self.promotion_summary = promotion_summary or {}
        self.runtime_status = runtime_status or {}
        self.connection_testers = connection_testers or {}
        self.reconnect_handlers = reconnect_handlers or {}
        self.settings = settings
        self.provider_entity_mappings = provider_entity_mappings or {}
        self.entity_mapping_store_path = entity_mapping_store_path
        self.provider_state_path = provider_state_path
        self.secrets_path = secrets_path
        self.settings_path = settings_path
        self.solaredge_cookie_path = solaredge_cookie_path
        self.provider_state = _load_json(provider_state_path)

    def providers(self) -> list[ManagedProvider]:
        existing = {provider.provider_id: provider for provider in self.registry.providers}
        providers = [self._managed_provider(provider) for provider in self.registry.providers]
        for provider in _future_provider_catalog():
            if provider.provider_id not in existing:
                providers.append(self._managed_provider(provider))
        return providers

    def get(self, provider_id: str) -> Optional[ManagedProvider]:
        for provider in self.providers():
            if provider.provider_id == provider_id:
                return provider
        return None

    def as_dict(self) -> dict[str, Any]:
        providers = self.providers()
        return {
            "providers": [provider.as_dict() for provider in providers],
            "status": {provider.provider_id: provider.lifecycle_state.value for provider in providers},
            "onboarding_states": [state.value for state in ProviderOnboardingState],
            "actions": ["Configure", "Edit", "Reconnect", "Disconnect", "Test Connection", "Disable", "Enable", "Remove"],
        }

    def test_connection(self, provider_id: str) -> ProviderTestResult:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        tester = self.connection_testers.get(provider_id)
        if tester:
            result = self._adapter_test_connection(provider, tester)
            self._record_connection_test(provider_id, result)
            return result
        success = provider.lifecycle_state in {
            ProviderConnectionStatus.CONNECTED,
            ProviderConnectionStatus.SYNCING,
        }
        warnings = list(provider.diagnostics.warnings)
        if provider.lifecycle_state == ProviderConnectionStatus.NOT_CONFIGURED:
            warnings.append("Provider is not configured.")
        if provider.lifecycle_state in {ProviderConnectionStatus.DEGRADED, ProviderConnectionStatus.ERROR}:
            warnings.append(provider.diagnostics.last_error or "Provider health is degraded.")
        status = "success" if success and not warnings else "warning" if success else "failure"
        return ProviderTestResult(
            provider_id=provider.provider_id,
            success=success,
            authentication_status="authenticated" if success else "not_authenticated",
            capability_detection=provider.capability_summary,
            provider_version="adapter-not-installed" if provider.lifecycle_state == ProviderConnectionStatus.NOT_CONFIGURED else "provider-framework-v1",
            available_datasets=[capability.value for capability in provider.capability_summary.capabilities] if success else [],
            warnings=warnings,
            response_time_ms=0,
            status=status,
            user_message=_test_user_message(status, warnings),
            next_step=_test_next_step(status),
            technical_details={"source": "ProviderManager inferred state"},
        )

    def _adapter_test_connection(self, provider: ManagedProvider, tester: Any) -> ProviderTestResult:
        try:
            raw = tester()
        except Exception as exc:
            raw = {"success": False, "warnings": [str(exc)], "authentication_status": "not_authenticated"}
        success = bool(raw.get("success"))
        warnings = [str(item) for item in raw.get("warnings", []) if item]
        if provider.provider_id == "solaredge" and success:
            data_retrieved = bool(raw.get("data_retrieved")) or int(raw.get("measurements") or 0) > 0
            if not data_retrieved:
                success = False
                raw["authentication_status"] = raw.get("authentication_status") or "authenticated"
                if not warnings:
                    warnings.append("Signed in, but no recent solar data was received.")
                raw["status"] = "warning"
        status = str(raw.get("status") or ("success" if success and not warnings else "warning" if success or str(raw.get("authentication_status")) == "authenticated" else "failure"))
        datasets = raw.get("available_datasets") or []
        return ProviderTestResult(
            provider_id=provider.provider_id,
            success=success,
            authentication_status=str(raw.get("authentication_status") or ("authenticated" if success else "not_authenticated")),
            capability_detection=provider.capability_summary,
            provider_version=str(raw.get("provider_version") or "provider-adapter"),
            available_datasets=[str(item) for item in datasets],
            warnings=warnings,
            response_time_ms=int(raw.get("response_time_ms") or 0),
            status=status,
            user_message=_test_user_message(status, warnings),
            next_step=_test_next_step(status),
            technical_details={
                "http_status": raw.get("http_status"),
                "measurements": raw.get("measurements"),
                "data_retrieved": raw.get("data_retrieved"),
                "sample_date": raw.get("sample_date"),
                "report_regenerated": raw.get("report_regenerated"),
                "adapter": "runtime",
                "entity_mapping": raw.get("entity_mapping"),
            },
        )

    def _record_connection_test(self, provider_id: str, result: ProviderTestResult) -> None:
        record = dict(self.provider_state.get(provider_id, {}))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record["last_tested_at"] = now
        record["last_test_status"] = result.status
        if result.success:
            record["last_successful_test_at"] = now
            record.pop("last_connection_error", None)
            if provider_id == "solaredge":
                data_returned = bool(result.technical_details.get("data_retrieved")) or int(result.technical_details.get("measurements") or 0) > 0
            else:
                data_returned = bool(result.available_datasets) or bool(result.technical_details.get("data_retrieved")) or int(result.technical_details.get("measurements") or 0) > 0
            if data_returned:
                record["last_successful_data_at"] = now
                record["last_successful_sync_at"] = now
        else:
            record["last_failed_test_at"] = now
            record["last_connection_error"] = result.user_message
        self.provider_state[provider_id] = record
        _save_json(self.provider_state_path, self.provider_state)

    def configure(self, provider_id: str, values: dict[str, Any]) -> ProviderConfiguration:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        validation_errors = _validate_configuration_values(provider.provider_type, values)
        if validation_errors:
            raise ValueError("; ".join(validation_errors))
        saved_mapping = None
        if provider_id == "home_assistant":
            mappings_payload = values.get("entity_mappings") or values.get("selected_mappings") or values.get("selected_entity_mappings")
            if values.get("reset_entity_mappings"):
                current_plan = provider.configuration.entity_mapping or {}
                mappings_payload = current_plan.get("recommended_mappings") or {}
            if mappings_payload is not None:
                saved_mapping = save_provider_entity_mappings(
                    provider_id,
                    clean_entity_mappings(mappings_payload),
                    path=self.entity_mapping_store_path,
                )
        stored = self._store_provider_configuration(provider, values)
        credentials = []
        editable = _editable_fields(provider.provider_type, provider.provider_id)
        for field_name in editable:
            value = values.get(field_name)
            configured = _credential_configured(field_name, value, provider.provider_type, stored)
            credentials.append(
                ProviderCredential(
                    field=field_name,
                    label=_credential_label(provider.provider_type, provider.provider_id, field_name),
                    required=field_name in _required_fields(provider.provider_type, provider.provider_id),
                    sensitive=field_name in SENSITIVE_FIELDS,
                    configured=configured,
                    value=str(value) if value is not None else None,
                )
            )
        missing_required = _missing_required_labels(provider.provider_type, provider.provider_id, stored)
        lifecycle = ProviderConnectionStatus.CONFIGURED if not missing_required else ProviderConnectionStatus.NOT_CONFIGURED
        notes = (
            "Configuration accepted by Provider Management. Sensitive values are redacted from API responses. Run Test Connection before relying on this provider."
            if not missing_required else
            f"Configuration still needs: {', '.join(missing_required)}."
        )
        if saved_mapping is not None:
            notes = "Home Assistant entity mappings saved. Run Test Connection to confirm the selected entities still look valid."
        return ProviderConfiguration(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=lifecycle,
            credentials=credentials,
            editable_fields=editable,
            entity_mapping=self._entity_mapping_for_provider(provider.provider_id),
            enabled=True,
            notes=notes,
        )

    def action(self, provider_id: str, action: str, values: Optional[dict[str, Any]] = None) -> ProviderActionResult:
        normalized = action.strip().lower().replace(" ", "_")
        values = values or {}
        if normalized in {"configure", "edit", "save"}:
            configuration = self.configure(provider_id, values)
            provider = self.get(provider_id)
            return ProviderActionResult(
                provider_id=provider_id,
                action=normalized,
                success=configuration.lifecycle_state != ProviderConnectionStatus.NOT_CONFIGURED,
                status="success" if configuration.lifecycle_state != ProviderConnectionStatus.NOT_CONFIGURED else "warning",
                user_message=(
                    "Settings saved successfully."
                    if configuration.lifecycle_state != ProviderConnectionStatus.NOT_CONFIGURED
                    else configuration.notes or "Unable to save settings because required information is missing."
                ),
                next_step="Run Test Connection before relying on this provider.",
                configuration=configuration.as_dict(),
                provider=provider.as_dict() if provider else None,
                technical_details={"stored_fields": _stored_field_summary(self.provider_state.get(provider_id, {}))},
            )
        if normalized == "test":
            result = self.test_connection(provider_id)
            refreshed = self.get(provider_id)
            return ProviderActionResult(
                provider_id=provider_id,
                action=normalized,
                success=result.success,
                status=result.status,
                user_message=result.user_message,
                next_step=result.next_step,
                test=result.as_dict(),
                provider=refreshed.as_dict() if refreshed else None,
                technical_details=result.technical_details,
            )
        if normalized == "reset":
            provider = self.get(provider_id)
            if provider is None:
                raise ValueError(f"Unknown provider: {provider_id}")
            if provider_id == "home_assistant":
                current_plan = provider.configuration.entity_mapping or {}
                save_provider_entity_mappings(
                    provider_id,
                    clean_entity_mappings(current_plan.get("recommended_mappings") or {}),
                    path=self.entity_mapping_store_path,
                )
            return self._state_action(provider_id, "reset", enabled=True, removed=False, message="Provider settings were reset to recommended local defaults.")
        if normalized == "reconnect":
            reconnecter = self.reconnect_handlers.get(provider_id)
            if reconnecter:
                provider = self.get(provider_id)
                if provider is None:
                    raise ValueError(f"Unknown provider: {provider_id}")
                test_result = self._adapter_test_connection(provider, reconnecter)
                self._record_connection_test(provider_id, test_result)
            else:
                test_result = self.test_connection(provider_id)
            status = "success" if test_result.success else test_result.status
            refreshed = self.get(provider_id)
            return ProviderActionResult(
                provider_id=provider_id,
                action=normalized,
                success=test_result.success,
                status=status,
                user_message=test_result.user_message,
                next_step=(
                    "Dashboard regenerated with the latest available SolarEdge data."
                    if test_result.success and test_result.technical_details.get("report_regenerated")
                    else test_result.next_step
                ),
                test=test_result.as_dict(),
                provider=refreshed.as_dict() if refreshed else None,
                technical_details={"reconnect": "test_connection", **test_result.technical_details},
            )
        if normalized == "disable":
            return self._state_action(provider_id, normalized, enabled=False, removed=False, message="Provider disabled. PowerMonitor will keep existing data but will not rely on new provider syncs.")
        if normalized == "enable":
            return self._state_action(provider_id, normalized, enabled=True, removed=False, message="Provider enabled. Run Test Connection before relying on new data.")
        if normalized == "remove":
            return self._state_action(provider_id, normalized, enabled=False, removed=True, clear=True, message="Provider configuration removed from local Provider Management.")
        raise ValueError(f"Unsupported provider action: {action}")

    def transition(self, provider_id: str, action: str) -> ManagedProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        normalized = action.strip().lower().replace(" ", "_")
        allowed = {item.lower().replace(" ", "_") for item in provider.actions}
        if normalized not in allowed:
            raise ValueError(f"Unsupported provider action: {action}")
        return provider

    def _state_action(
        self,
        provider_id: str,
        action: str,
        *,
        enabled: bool,
        removed: bool,
        message: str,
        clear: bool = False,
    ) -> ProviderActionResult:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        if clear:
            self._clear_provider_configuration(provider_id)
        record = {} if clear else dict(self.provider_state.get(provider_id, {}))
        record["enabled"] = enabled
        record["removed"] = removed
        record["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.provider_state[provider_id] = record
        _save_json(self.provider_state_path, self.provider_state)
        refreshed = self.get(provider_id)
        return ProviderActionResult(
            provider_id=provider_id,
            action=action,
            success=True,
            status="success",
            user_message=message,
            next_step="Run Test Connection after changing provider state." if enabled else "Enable this provider when you want PowerMonitor to use it again.",
            provider=refreshed.as_dict() if refreshed else None,
            technical_details={"enabled": enabled, "removed": removed},
        )

    def _clear_provider_configuration(self, provider_id: str) -> None:
        if provider_id == "home_assistant":
            secrets = _load_json(self.secrets_path)
            for key in ("home_assistant_token", "ha_token"):
                secrets.pop(key, None)
            _save_json(self.secrets_path, secrets)
            save_provider_entity_mappings(provider_id, {}, path=self.entity_mapping_store_path)
        elif provider_id == "solaredge":
            secrets = _load_json(self.secrets_path)
            for key in ("solaredge_username", "solaredge_password"):
                secrets.pop(key, None)
            _save_json(self.secrets_path, secrets)
            self.solaredge_cookie_path.unlink(missing_ok=True)

    def _store_provider_configuration(self, provider: ManagedProvider, values: dict[str, Any]) -> dict[str, Any]:
        record = dict(self.provider_state.get(provider.provider_id, {}))
        configured_fields = set(record.get("configured_fields") or [])
        public_values = dict(record.get("public_values") or {})
        changed = False
        for field_name in _editable_fields(provider.provider_type, provider.provider_id):
            if field_name not in values:
                continue
            raw = values.get(field_name)
            if raw is None or str(raw) == "":
                continue
            configured_fields.add(field_name)
            if field_name not in SENSITIVE_FIELDS:
                public_values[field_name] = str(raw)
            self._persist_provider_value(provider.provider_id, provider.provider_type, field_name, str(raw))
            changed = True
        if not changed:
            return record
        record.update(
            {
                "enabled": True,
                "removed": False,
                "configured_fields": sorted(configured_fields),
                "public_values": public_values,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        self.provider_state[provider.provider_id] = record
        _save_json(self.provider_state_path, self.provider_state)
        return record

    def _persist_provider_value(self, provider_id: str, provider_type: ProviderType, field_name: str, value: str) -> None:
        if provider_id == "home_assistant":
            if field_name == "endpoint":
                settings = _load_json(self.settings_path)
                settings["home_assistant_url"] = value.rstrip("/")
                _save_json(self.settings_path, settings)
            elif field_name == "api_key":
                secrets = _load_json(self.secrets_path)
                secrets["home_assistant_token"] = value
                _save_json(self.secrets_path, secrets)
        elif provider_id == "solaredge":
            if field_name == "cookie":
                self.solaredge_cookie_path.parent.mkdir(parents=True, exist_ok=True)
                self.solaredge_cookie_path.write_text(value.strip() + "\n", encoding="utf-8")
            elif field_name in {"username", "password"}:
                secrets = _load_json(self.secrets_path)
                secrets[f"solaredge_{field_name}"] = value
                _save_json(self.secrets_path, secrets)

    def _managed_provider(self, provider: Provider) -> ManagedProvider:
        state_record = self.provider_state.get(provider.provider_id, {})
        lifecycle = _lifecycle_from_provider(provider, self.health, state_record)
        diagnostics = self._diagnostics(provider, lifecycle)
        capability_summary = ProviderCapabilitySummary(
            provider_id=provider.provider_id,
            capabilities=list(provider.capabilities),
            detected=provider.available,
            missing=[] if provider.available else list(provider.capabilities),
        )
        configuration = ProviderConfiguration(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=lifecycle,
            credentials=_credential_templates(provider.provider_type, lifecycle, state_record, provider.provider_id),
            editable_fields=_editable_fields(provider.provider_type, provider.provider_id),
            entity_mapping=self._entity_mapping_for_provider(provider.provider_id),
            enabled=lifecycle != ProviderConnectionStatus.DISABLED,
            notes=provider.note,
        )
        health = ProviderHealth(
            status=lifecycle,
            collector_status=str(self.health.get("status") or provider.status.value),
            last_successful_sync=diagnostics.last_successful_sync,
            last_failed_sync=diagnostics.last_failed_sync,
            failure_count=diagnostics.failure_count,
            last_error=diagnostics.last_error,
            freshness=diagnostics.freshness,
            entity_mapping_health=list((diagnostics.entity_mapping or {}).get("diagnostics") or []),
        )
        sync_state = _sync_state(lifecycle, diagnostics)
        runtime = _runtime_for_provider(self.runtime_status, provider.provider_id)
        onboarding = _onboarding_guide(provider, configuration, health, diagnostics, capability_summary, sync_state, runtime)
        return ManagedProvider(
            provider_id=provider.provider_id,
            name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=lifecycle,
            configuration=configuration,
            health=health,
            sync_state=sync_state,
            diagnostics=diagnostics,
            capability_summary=capability_summary,
            onboarding=onboarding,
            actions=_actions_for(lifecycle),
            source=provider.source,
            note=provider.note,
        )

    def _diagnostics(self, provider: Provider, lifecycle: ProviderConnectionStatus) -> ProviderDiagnostics:
        state_record = self.provider_state.get(provider.provider_id, {})
        freshness = provider.freshness.as_dict() if provider.freshness else None
        observation_count = _observation_count(self.provider_observations, provider.provider_id)
        live_observation_types = _live_observation_types(self.provider_observations, provider.provider_id)
        promotion_count = _promotion_count(self.promotion_summary, provider.provider_id)
        last_error = None
        warnings = []
        if lifecycle in {ProviderConnectionStatus.DEGRADED, ProviderConnectionStatus.ERROR}:
            last_error = str(state_record.get("last_connection_error") or self.health.get("collector_message") or self.health.get("failure_type") or provider.note or "Provider unavailable")
            warnings.append(last_error)
        if lifecycle == ProviderConnectionStatus.NOT_CONFIGURED:
            warnings.append("Provider configuration is missing.")
        return ProviderDiagnostics(
            provider_id=provider.provider_id,
            last_successful_sync=self.health.get("last_successful_collection") or self.health.get("latest_measurement_time"),
            last_failed_sync=self.health.get("last_collection_failure"),
            failure_count=int(self.health.get("failure_count") or 0),
            last_error=last_error,
            freshness=freshness,
            observation_count=observation_count,
            live_observation_types=live_observation_types,
            promotion_count=promotion_count,
            collector_version=str(self.health.get("collector_version") or "PowerMonitor local collector"),
            warnings=warnings,
            entity_mapping=self._entity_mapping_for_provider(provider.provider_id),
        )

    def _entity_mapping_for_provider(self, provider_id: str) -> Optional[dict[str, Any]]:
        if provider_id != "home_assistant":
            return None
        supplied = self.provider_entity_mappings.get(provider_id)
        if isinstance(supplied, dict) and supplied.get("selected_mappings") is not None:
            selected = clean_entity_mappings(supplied.get("selected_mappings"))
            updated_at = str(supplied.get("updated_at") or "") or None
        elif isinstance(supplied, dict):
            selected = clean_entity_mappings(supplied)
            updated_at = None
        else:
            record = load_provider_mapping_record(provider_id, path=self.entity_mapping_store_path)
            selected = load_provider_entity_mappings(provider_id, path=self.entity_mapping_store_path)
            updated_at = str(record.get("updated_at") or "") or None
        mapping = build_home_assistant_entity_mapping_plan([], selected, updated_at=updated_at).as_dict()
        _attach_last_successful_observations(mapping, self.provider_observations, provider_id)
        return mapping


def build_provider_management(
    *,
    provider_registry: ProviderRegistry,
    health: Optional[dict[str, Any]] = None,
    provider_observations: Optional[dict[str, Any]] = None,
    promotion_summary: Optional[dict[str, Any]] = None,
    runtime_status: Optional[dict[str, Any]] = None,
    connection_testers: Optional[dict[str, Any]] = None,
    reconnect_handlers: Optional[dict[str, Any]] = None,
    settings: Any = None,
    provider_entity_mappings: Optional[dict[str, Any]] = None,
    entity_mapping_store_path: Path = DEFAULT_PROVIDER_MAPPING_PATH,
    provider_state_path: Path = DEFAULT_PROVIDER_STATE_PATH,
    secrets_path: Path = DEFAULT_SECRETS_PATH,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    solaredge_cookie_path: Path = DEFAULT_SOLAREDGE_COOKIE_PATH,
) -> ProviderManager:
    return ProviderManager(
        provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        runtime_status=runtime_status,
        connection_testers=connection_testers,
        reconnect_handlers=reconnect_handlers,
        settings=settings,
        provider_entity_mappings=provider_entity_mappings,
        entity_mapping_store_path=entity_mapping_store_path,
        provider_state_path=provider_state_path,
        secrets_path=secrets_path,
        settings_path=settings_path,
        solaredge_cookie_path=solaredge_cookie_path,
    )


def _lifecycle_from_provider(provider: Provider, health: dict[str, Any], state_record: Optional[dict[str, Any]] = None) -> ProviderConnectionStatus:
    state_record = state_record or {}
    if state_record.get("removed"):
        return ProviderConnectionStatus.NOT_CONFIGURED
    if state_record.get("enabled") is False:
        return ProviderConnectionStatus.DISABLED
    if provider.status == ProviderStatus.MISSING:
        return ProviderConnectionStatus.NOT_CONFIGURED
    if _is_non_live_provider(provider):
        return ProviderConnectionStatus.CONFIGURED
    if provider.status == ProviderStatus.DEGRADED:
        return ProviderConnectionStatus.DEGRADED
    if state_record.get("last_connection_error"):
        return ProviderConnectionStatus.ERROR
    configured = _provider_has_configuration(provider, state_record)
    validated = bool(state_record.get("last_successful_test_at"))
    data_retrieved = bool(state_record.get("last_successful_data_at") or state_record.get("last_successful_sync_at"))
    if configured and validated and data_retrieved:
        if str(health.get("status") or "").lower() not in {"", "ok"}:
            return ProviderConnectionStatus.DEGRADED
        return ProviderConnectionStatus.CONNECTED
    if configured:
        return ProviderConnectionStatus.CONFIGURED
    if str(health.get("status") or "").lower() not in {"", "ok"}:
        return ProviderConnectionStatus.ERROR
    return ProviderConnectionStatus.NOT_CONFIGURED


def _is_non_live_provider(provider: Provider) -> bool:
    return (
        provider.provider_type in {ProviderType.RETAILER, ProviderType.GENERIC_IMPORT}
        or ProviderCapability.IMPORT_ONLY in provider.capabilities
        or provider.provider_id == "ocular_export"
    )


def _provider_has_configuration(provider: Provider, state_record: dict[str, Any]) -> bool:
    configured_fields = set(state_record.get("configured_fields") or [])
    if provider.provider_id == "solaredge":
        return _solaredge_configuration_ready(configured_fields)
    required = _required_fields(provider.provider_type, provider.provider_id)
    if required and required.issubset(configured_fields):
        return True
    if not required and provider.status in {ProviderStatus.CONFIGURED, ProviderStatus.AVAILABLE, ProviderStatus.SUPPORTED}:
        return True
    return bool(state_record.get("updated_at") and not required)


def _sync_state(lifecycle: ProviderConnectionStatus, diagnostics: ProviderDiagnostics) -> ProviderSyncState:
    if lifecycle == ProviderConnectionStatus.SYNCING:
        return ProviderSyncState.SYNCING
    if lifecycle in {ProviderConnectionStatus.ERROR, ProviderConnectionStatus.DEGRADED}:
        return ProviderSyncState.FAILED
    if diagnostics.last_successful_sync:
        return ProviderSyncState.SYNCED
    if lifecycle == ProviderConnectionStatus.NOT_CONFIGURED:
        return ProviderSyncState.NEVER_SYNCED
    return ProviderSyncState.UNKNOWN


def _runtime_for_provider(runtime_status: dict[str, Any], provider_id: str) -> dict[str, Any]:
    providers = runtime_status.get("providers") if isinstance(runtime_status, dict) else None
    if isinstance(providers, list):
        for provider in providers:
            if isinstance(provider, dict) and provider.get("provider_id") == provider_id:
                return provider.get("health") if isinstance(provider.get("health"), dict) else provider
    provider = runtime_status.get(provider_id) if isinstance(runtime_status, dict) else None
    return provider if isinstance(provider, dict) else {}


def _runtime_value(runtime: dict[str, Any], key: str) -> Any:
    if not runtime:
        return None
    if key in runtime:
        return runtime.get(key)
    health = runtime.get("health")
    if isinstance(health, dict):
        return health.get(key)
    return None


def _actions_for(lifecycle: ProviderConnectionStatus) -> list[str]:
    if lifecycle == ProviderConnectionStatus.NOT_CONFIGURED:
        return ["Configure", "Test Connection"]
    if lifecycle == ProviderConnectionStatus.DISABLED:
        return ["Enable", "Remove"]
    actions = ["Configure", "Edit", "Reconnect", "Disconnect", "Test Connection", "Disable", "Remove"]
    if lifecycle in {ProviderConnectionStatus.DEGRADED, ProviderConnectionStatus.ERROR}:
        actions.append("Diagnostics")
    else:
        actions.append("Diagnostics")
    return actions


def _test_user_message(status: str, warnings: list[str]) -> str:
    if status == "success":
        return "Connected successfully."
    if status == "warning":
        detail = warnings[0] if warnings else "The provider responded, but some checks need attention."
        if "signed in" in detail.lower() and "no recent solar data" in detail.lower():
            return "Signed in, but no recent solar data was received."
        return f"Connected with a warning: {detail}"
    detail = warnings[0] if warnings else "PowerMonitor could not connect to the provider."
    lowered = detail.lower()
    if "credential" in lowered and any(word in lowered for word in ("missing", "not configured", "unavailable")):
        return f"Credentials missing. {detail}"
    if "cookie" in lowered and any(word in lowered for word in ("expired", "403", "unauthor", "forbidden")):
        return f"Cookie expired. {detail}"
    if any(word in lowered for word in ("login", "password", "credential", "unauthor", "403", "forbidden")):
        return f"Login failed. {detail}"
    if any(word in lowered for word in ("network", "timeout", "unreachable", "connection refused", "dns")):
        return f"Network unavailable. {detail}"
    return f"Unable to connect. {detail}"


def _test_next_step(status: str) -> str:
    if status == "success":
        return "Keep this provider connected and watch the next scheduled sync."
    if status == "warning":
        return "Check the provider has recent data, then run Test Connection again."
    return "Update the saved username/password or refresh the provider session, then run Test Connection again."


def _duration_label(value: Any) -> str:
    if value in {None, ""}:
        return "Not recorded"
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if milliseconds >= 1000:
        return f"{milliseconds / 1000:.1f}s"
    return f"{milliseconds:.0f}ms"


def _retry_countdown(runtime: dict[str, Any]) -> str:
    state = str(_runtime_value(runtime, "state") or "")
    next_run = _runtime_value(runtime, "next_run_at")
    if state == "BACKING_OFF" and next_run:
        return f"Retry scheduled for {next_run}"
    return "No retry scheduled"


def _health_score(
    state: ProviderOnboardingState,
    health: ProviderHealth,
    diagnostics: ProviderDiagnostics,
    capability_summary: ProviderCapabilitySummary,
    runtime: dict[str, Any],
) -> tuple[int, dict[str, str]]:
    factors = {
        "authentication": "ok" if state in {ProviderOnboardingState.CONNECTED, ProviderOnboardingState.SYNCING, ProviderOnboardingState.READY_TO_CONNECT} else "needs_attention",
        "freshness": "ok" if diagnostics.last_successful_sync or (diagnostics.freshness or {}).get("state") in {"fresh", "ok"} else "unknown",
        "runtime_success": "ok" if str(_runtime_value(runtime, "state") or "") in {"", "IDLE", "SUCCESS"} else "needs_attention",
        "observation_quality": "ok" if capability_summary.detected or diagnostics.observation_count else "unknown",
        "collector_health": "ok" if str(health.collector_status or "").lower() in {"ok", "available", "configured"} else "needs_attention",
    }
    if diagnostics.entity_mapping:
        mapping_status = str(((diagnostics.entity_mapping.get("validation") or {}).get("status")) or "unknown")
        factors["entity_mapping"] = "ok" if mapping_status == "ok" else "needs_attention" if mapping_status in {"warning", "error"} else "unknown"
    score = 100
    for value in factors.values():
        if value == "needs_attention":
            score -= 22
        elif value == "unknown":
            score -= 8
    if state == ProviderOnboardingState.ERROR:
        score = min(score, 35)
    if state == ProviderOnboardingState.WARNING:
        score = min(score, 70)
    if state in {ProviderOnboardingState.NOT_CONFIGURED, ProviderOnboardingState.CONFIGURATION_REQUIRED}:
        score = min(score, 45)
    return max(0, min(100, score)), factors


def _onboarding_guide(
    provider: Provider,
    configuration: ProviderConfiguration,
    health: ProviderHealth,
    diagnostics: ProviderDiagnostics,
    capability_summary: ProviderCapabilitySummary,
    sync_state: ProviderSyncState,
    runtime: dict[str, Any],
) -> ProviderOnboardingGuide:
    state = _onboarding_state(configuration, health, diagnostics, sync_state)
    connected = state in {ProviderOnboardingState.CONNECTED, ProviderOnboardingState.SYNCING}
    healthy = state in {ProviderOnboardingState.CONNECTED, ProviderOnboardingState.SYNCING, ProviderOnboardingState.READY_TO_CONNECT}
    data_available = capability_summary.detected or diagnostics.observation_count > 0 or bool(diagnostics.last_successful_sync)
    required = _required_information(configuration)
    health_score, health_factors = _health_score(state, health, diagnostics, capability_summary, runtime)
    return ProviderOnboardingGuide(
        provider_id=provider.provider_id,
        state=state,
        state_label=_state_label(provider, state, data_available),
        connected=connected,
        healthy=healthy,
        data_available=data_available,
        last_successful_sync=diagnostics.last_successful_sync,
        next_scheduled_sync=_runtime_value(runtime, "next_run_at") or _next_sync_label(provider.provider_type),
        sync_frequency=_sync_frequency(provider.provider_type),
        runtime_state=_runtime_value(runtime, "state") or "Not running",
        collector_duration=_duration_label(_runtime_value(runtime, "execution_duration_ms")),
        consecutive_failures=int(_runtime_value(runtime, "consecutive_failures") or diagnostics.failure_count or 0),
        retry_countdown=_retry_countdown(runtime),
        health_score=health_score,
        health_factors=health_factors,
        required_information=required,
        why_required=_why_required(provider.provider_type),
        connection_status=_connection_status(provider, state, data_available),
        health_status=_health_status(state, health, diagnostics),
        next_step=_next_step(state, provider.name),
        common_failures=_common_failures(provider.provider_type),
        recovery_steps=_recovery_steps(provider.provider_type),
        technical_details={
            "lifecycle_state": configuration.lifecycle_state.value,
            "sync_state": sync_state.value,
            "collector_status": health.collector_status,
            "failure_count": diagnostics.failure_count,
            "last_error": diagnostics.last_error,
            "runtime": runtime,
            "source": provider.source,
        },
    )


def _onboarding_state(
    configuration: ProviderConfiguration,
    health: ProviderHealth,
    diagnostics: ProviderDiagnostics,
    sync_state: ProviderSyncState,
) -> ProviderOnboardingState:
    if not configuration.enabled or configuration.lifecycle_state == ProviderConnectionStatus.DISABLED:
        return ProviderOnboardingState.DISABLED
    if configuration.lifecycle_state == ProviderConnectionStatus.SYNCING or sync_state == ProviderSyncState.SYNCING:
        return ProviderOnboardingState.SYNCING
    if configuration.lifecycle_state == ProviderConnectionStatus.ERROR:
        return ProviderOnboardingState.ERROR
    if configuration.lifecycle_state == ProviderConnectionStatus.DEGRADED or sync_state in {ProviderSyncState.FAILED, ProviderSyncState.STALE}:
        return ProviderOnboardingState.WARNING
    if configuration.lifecycle_state == ProviderConnectionStatus.NOT_CONFIGURED:
        return ProviderOnboardingState.NOT_CONFIGURED
    if configuration.lifecycle_state == ProviderConnectionStatus.CONNECTED:
        return ProviderOnboardingState.CONNECTED
    missing_required = [
        credential for credential in configuration.credentials
        if credential.required and not credential.configured
    ]
    if configuration.provider_id == "solaredge" and configuration.lifecycle_state == ProviderConnectionStatus.CONFIGURED:
        missing_required = []
    if missing_required:
        return ProviderOnboardingState.CONFIGURATION_REQUIRED
    if configuration.lifecycle_state == ProviderConnectionStatus.CONFIGURED and not diagnostics.last_successful_sync:
        return ProviderOnboardingState.READY_TO_CONNECT
    return ProviderOnboardingState.READY_TO_CONNECT


def _required_information(configuration: ProviderConfiguration) -> list[str]:
    if configuration.provider_id == "solaredge":
        configured = {credential.field for credential in configuration.credentials if credential.configured}
        if _solaredge_configuration_ready(configured):
            return ["SolarEdge username/email and password", "Cookie only if you use the advanced fallback"]
        return ["SolarEdge username/email", "SolarEdge password"]
    required = [credential.label for credential in configuration.credentials if credential.required]
    if required:
        return required
    if configuration.provider_type == ProviderType.GENERIC_IMPORT:
        return ["A supported export file when you import history"]
    if configuration.provider_type == ProviderType.RETAILER:
        return ["Tariff or billing plan details"]
    return ["Provider account details", "Provider endpoint if you use a local adapter"]


def _why_required(provider_type: ProviderType) -> str:
    reasons = {
        ProviderType.SOLAR: "Solar access lets PowerMonitor explain generation, export credit, and self-consumption against your bill.",
        ProviderType.EV_CHARGER: "EV session data separates real charging cost from estimates so advice can use actual household charging.",
        ProviderType.RETAILER: "Retailer and tariff details turn usage into bill impact and plan comparisons.",
        ProviderType.SMART_METER: "Meter readings provide the interval history needed for cost, freshness, and confidence checks.",
        ProviderType.HOME_AUTOMATION: "Home automation data can provide local EV or live-power observations without controlling devices.",
        ProviderType.GENERIC_IMPORT: "Import files add history without requiring a live provider session.",
    }
    return reasons.get(provider_type, "Provider details let PowerMonitor test access and explain which data is safe to use.")


def _state_label(provider: Provider, state: ProviderOnboardingState, data_available: bool) -> str:
    if _is_non_live_provider(provider) and data_available:
        return "Historical Data Available"
    if _is_non_live_provider(provider) and state == ProviderOnboardingState.READY_TO_CONNECT:
        return "Configured"
    return {
        ProviderOnboardingState.NOT_CONFIGURED: "Not Configured",
        ProviderOnboardingState.CONFIGURATION_REQUIRED: "Waiting For Setup",
        ProviderOnboardingState.READY_TO_CONNECT: "Waiting For Setup",
        ProviderOnboardingState.CONNECTING: "Checking Connection",
        ProviderOnboardingState.CONNECTED: "Connected",
        ProviderOnboardingState.SYNCING: "Updating",
        ProviderOnboardingState.WARNING: "Needs Attention",
        ProviderOnboardingState.ERROR: "Unable To Connect",
        ProviderOnboardingState.DISABLED: "Disabled",
    }[state]


def _connection_status(provider: Provider, state: ProviderOnboardingState, data_available: bool) -> str:
    if _is_non_live_provider(provider):
        if data_available:
            return "Historical data available. Live updates unavailable."
        if state == ProviderOnboardingState.NOT_CONFIGURED:
            return "Waiting for setup."
        return "Configured. This source does not provide live updates."
    return {
        ProviderOnboardingState.NOT_CONFIGURED: "Not configured.",
        ProviderOnboardingState.CONFIGURATION_REQUIRED: "Configuration is missing required information.",
        ProviderOnboardingState.READY_TO_CONNECT: "Waiting for setup. Run Test Connection before relying on this provider.",
        ProviderOnboardingState.CONNECTING: "Checking connection.",
        ProviderOnboardingState.CONNECTED: "Connected. Live updates are available.",
        ProviderOnboardingState.SYNCING: "Connected and updating now.",
        ProviderOnboardingState.WARNING: "Needs attention. Recent updates could not be verified.",
        ProviderOnboardingState.ERROR: "Unable to connect.",
        ProviderOnboardingState.DISABLED: "Provider is disabled.",
    }[state]


def _health_status(state: ProviderOnboardingState, health: ProviderHealth, diagnostics: ProviderDiagnostics) -> str:
    if state in {ProviderOnboardingState.CONNECTED, ProviderOnboardingState.SYNCING}:
        return "Healthy."
    if state == ProviderOnboardingState.READY_TO_CONNECT:
        return "Ready for a connection test."
    if state in {ProviderOnboardingState.WARNING, ProviderOnboardingState.ERROR}:
        return diagnostics.last_error or health.collector_status or "Needs attention."
    if state == ProviderOnboardingState.DISABLED:
        return "Disabled by setup."
    return "Waiting for setup."


def _next_step(state: ProviderOnboardingState, provider_name: str) -> str:
    return {
        ProviderOnboardingState.NOT_CONFIGURED: f"Select {provider_name}, review the required details, then configure it.",
        ProviderOnboardingState.CONFIGURATION_REQUIRED: "Add the missing required details, then test the connection.",
        ProviderOnboardingState.READY_TO_CONNECT: "Run Test Connection and fix any warnings before relying on this data.",
        ProviderOnboardingState.CONNECTING: "Wait for the connection test to finish.",
        ProviderOnboardingState.CONNECTED: "No action needed right now.",
        ProviderOnboardingState.SYNCING: "No action needed while sync is running.",
        ProviderOnboardingState.WARNING: "Open recovery steps and check the last sync details.",
        ProviderOnboardingState.ERROR: "Follow the recovery steps, then reconnect and test again.",
        ProviderOnboardingState.DISABLED: "Enable this provider if you want PowerMonitor to use it.",
    }[state]


def _common_failures(provider_type: ProviderType) -> list[str]:
    common = ["Expired session or credential", "Provider service unavailable", "No recent data returned"]
    if provider_type == ProviderType.GENERIC_IMPORT:
        return ["Wrong export file type", "Export missing required columns", "Duplicate or incomplete history"]
    if provider_type == ProviderType.RETAILER:
        return ["Missing tariff values", "Billing dates not configured", "Plan details changed"]
    if provider_type == ProviderType.SOLAR:
        return common + ["Solar portal session expired"]
    if provider_type == ProviderType.EV_CHARGER:
        return common + ["Charging session export is incomplete"]
    return common


def _recovery_steps(provider_type: ProviderType) -> list[str]:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return ["Export the file again from the provider", "Preview the upload before saving", "Check validation messages for missing columns or dates"]
    if provider_type == ProviderType.RETAILER:
        return ["Confirm tariff rates and billing dates", "Update changed plan details", "Run the report again after saving"]
    if provider_type == ProviderType.SOLAR:
        return ["Update the SolarEdge username/email and password", "Run Test Connection", "Use the manual cookie fallback only if automatic sign-in is unavailable", "Review technical diagnostics only if the simple steps do not resolve it"]
    return ["Check the required information", "Reconnect or refresh the provider session", "Run Test Connection", "Review technical diagnostics only if the simple steps do not resolve it"]


def _sync_frequency(provider_type: ProviderType) -> str:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return "Manual import"
    if provider_type == ProviderType.RETAILER:
        return "When billing or tariff settings change"
    return "Refresh schedule"


def _next_sync_label(provider_type: ProviderType) -> str:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return "Next manual import"
    if provider_type == ProviderType.RETAILER:
        return "Next settings update"
    return "Next data refresh"


def _editable_fields(provider_type: ProviderType, provider_id: Optional[str] = None) -> list[str]:
    if provider_id == "ocular_export":
        return []
    fields = {
        ProviderType.SOLAR: ["username", "password", "cookie", "endpoint"],
        ProviderType.EV_CHARGER: ["username", "password", "api_key", "endpoint"],
        ProviderType.RETAILER: ["api_key", "endpoint"],
        ProviderType.BATTERY: ["api_key", "endpoint"],
        ProviderType.SMART_METER: ["username", "password", "api_key", "endpoint"],
        ProviderType.WEATHER: ["api_key", "endpoint"],
        ProviderType.HOME_AUTOMATION: ["api_key", "endpoint"],
        ProviderType.GENERIC_IMPORT: ["endpoint"],
    }
    return fields.get(provider_type, ["api_key", "endpoint"])


def _required_fields(provider_type: ProviderType, provider_id: Optional[str] = None) -> set[str]:
    if provider_id == "ocular_export":
        return set()
    if provider_type == ProviderType.GENERIC_IMPORT:
        return set()
    if provider_type == ProviderType.SOLAR:
        return {"username", "password"}
    if provider_type == ProviderType.RETAILER:
        return set()
    if provider_type == ProviderType.HOME_AUTOMATION:
        return {"api_key", "endpoint"}
    return {"endpoint"}


def _credential_templates(
    provider_type: ProviderType,
    lifecycle: ProviderConnectionStatus,
    state_record: Optional[dict[str, Any]] = None,
    provider_id: Optional[str] = None,
) -> list[ProviderCredential]:
    state_record = state_record or {}
    configured_fields = set(state_record.get("configured_fields") or [])
    public_values = state_record.get("public_values") if isinstance(state_record.get("public_values"), dict) else {}
    return [
        ProviderCredential(
            field=field_name,
            label=_credential_label(provider_type, provider_id, field_name),
            required=field_name in _required_fields(provider_type, provider_id),
            sensitive=field_name in SENSITIVE_FIELDS,
            configured=field_name in configured_fields,
            value=public_values.get(field_name) if field_name not in SENSITIVE_FIELDS else None,
        )
        for field_name in _editable_fields(provider_type, provider_id)
    ]


def _credential_label(provider_type: ProviderType, provider_id: Optional[str], field_name: str) -> str:
    if provider_id == "solaredge":
        labels = {
            "username": "Username/Email",
            "password": "Password",
            "cookie": "Cookie (advanced fallback)",
            "endpoint": "Endpoint",
        }
        return labels.get(field_name, field_name.replace("_", " ").title())
    return field_name.replace("_", " ").title()


def _missing_required_labels(provider_type: ProviderType, provider_id: Optional[str], state_record: dict[str, Any]) -> list[str]:
    configured_fields = set(state_record.get("configured_fields") or [])
    if provider_id == "solaredge":
        if _solaredge_configuration_ready(configured_fields):
            return []
        labels = []
        if "username" not in configured_fields:
            labels.append("Username/Email")
        if "password" not in configured_fields:
            labels.append("Password")
        return labels
    return [
        _credential_label(provider_type, provider_id, field_name)
        for field_name in _required_fields(provider_type, provider_id)
        if field_name not in configured_fields
    ]


def _solaredge_configuration_ready(configured_fields: set[str]) -> bool:
    return (
        SOLAREDGE_PRIMARY_FIELDS.issubset(configured_fields)
        or SOLAREDGE_COOKIE_FALLBACK_FIELDS.issubset(configured_fields)
    )


def _future_provider_catalog() -> list[Provider]:
    return [
        Provider("tesla", "Tesla", ProviderType.EV_CHARGER, [ProviderCapability.EV_SESSIONS, ProviderCapability.BATTERY_STATE], note="Future EV, battery, and charger provider."),
        Provider("amber", "Amber", ProviderType.RETAILER, [ProviderCapability.TARIFF_DATA, ProviderCapability.FORECAST], note="Future retailer and tariff provider."),
        Provider("fronius", "Fronius", ProviderType.SOLAR, [ProviderCapability.LIVE_POWER, ProviderCapability.INTERVAL_HISTORY], note="Future solar inverter provider."),
        Provider("enphase", "Enphase", ProviderType.SOLAR, [ProviderCapability.LIVE_POWER, ProviderCapability.INTERVAL_HISTORY, ProviderCapability.BATTERY_STATE], note="Future solar and battery provider."),
    ]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _validate_configuration_values(provider_type: ProviderType, values: dict[str, Any]) -> list[str]:
    errors = []
    for field_name, raw in values.items():
        value = str(raw or "").strip()
        if not value:
            continue
        if field_name == "endpoint":
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append("Endpoint must be a valid http or https URL.")
        if field_name == "api_key" and provider_type == ProviderType.HOME_AUTOMATION and len(value) < 20:
            errors.append("Home Assistant token looks too short. Paste a long-lived access token.")
    return errors


def _credential_configured(field_name: str, value: Any, provider_type: ProviderType, state_record: dict[str, Any]) -> bool:
    if value is not None and str(value) != "":
        return True
    if field_name in set(state_record.get("configured_fields") or []):
        return True
    public_values = state_record.get("public_values") if isinstance(state_record.get("public_values"), dict) else {}
    return field_name in public_values


def _stored_field_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "configured_fields": list(record.get("configured_fields") or []),
        "enabled": bool(record.get("enabled", True)),
        "removed": bool(record.get("removed", False)),
        "updated_at": record.get("updated_at"),
    }


def _observation_count(summary: dict[str, Any], provider_id: str) -> int:
    count = 0
    for observation in summary.get("latest", []) if isinstance(summary, dict) else []:
        if observation.get("provider_id") == provider_id:
            count += 1
    return count


def _promotion_count(summary: dict[str, Any], provider_id: str) -> int:
    count = 0
    for promotion in summary.get("latest_promotions", []) if isinstance(summary, dict) else []:
        if promotion.get("provider_id") == provider_id:
            count += 1
    return count


def _live_observation_types(summary: dict[str, Any], provider_id: str) -> list[str]:
    live_types = {
        "live_power",
        "grid_live_power",
        "solar_live_power",
        "battery_state",
        "battery_soc",
        "battery_power",
        "ev_charger_live_power",
        "ev_connected_state",
        "ev_charging_state",
        "home_live_power",
    }
    found = set()
    for provider in summary.get("providers", []) if isinstance(summary, dict) else []:
        if provider.get("provider_id") == provider_id:
            found.update(item for item in provider.get("observation_types", []) if item in live_types)
    for observation in summary.get("latest", []) if isinstance(summary, dict) else []:
        if observation.get("provider_id") == provider_id and observation.get("observation_type") in live_types:
            found.add(observation["observation_type"])
    return sorted(found)


def _attach_last_successful_observations(mapping: dict[str, Any], summary: dict[str, Any], provider_id: str) -> None:
    latest_by_reference = {}
    for observation in summary.get("latest", []) if isinstance(summary, dict) else []:
        if observation.get("provider_id") != provider_id:
            continue
        reference = str(observation.get("raw_reference") or observation.get("name") or "")
        if not reference:
            continue
        latest_by_reference.setdefault(reference, observation.get("observed_at"))
    for diagnostic in mapping.get("diagnostics", []) if isinstance(mapping, dict) else []:
        reference = str(diagnostic.get("selected_entity") or "")
        if reference in latest_by_reference:
            diagnostic["last_successful_observation"] = latest_by_reference[reference]
