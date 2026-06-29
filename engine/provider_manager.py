#!/usr/bin/env python3
"""Provider Management layer above the Provider Framework."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from .provider_framework import Provider, ProviderCapability, ProviderRegistry, ProviderStatus, ProviderType


class ProviderConnectionStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    CONNECTED = "CONNECTED"
    SYNCING = "SYNCING"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class ProviderSyncState(str, Enum):
    NEVER_SYNCED = "NEVER_SYNCED"
    SYNCED = "SYNCED"
    SYNCING = "SYNCING"
    STALE = "STALE"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


SENSITIVE_FIELDS = {"password", "cookie", "api_key", "token", "secret"}


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
    promotion_count: int = 0
    collector_version: str = "PowerMonitor local collector"
    warnings: list[str] = field(default_factory=list)

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
        settings: Any = None,
    ) -> None:
        self.registry = registry
        self.health = health or {}
        self.provider_observations = provider_observations or {}
        self.promotion_summary = promotion_summary or {}
        self.settings = settings

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
            "actions": ["Configure", "Edit", "Reconnect", "Disconnect", "Test Connection", "Disable", "Enable", "Remove"],
        }

    def test_connection(self, provider_id: str) -> ProviderTestResult:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        success = provider.lifecycle_state in {
            ProviderConnectionStatus.CONNECTED,
            ProviderConnectionStatus.SYNCING,
            ProviderConnectionStatus.CONFIGURED,
        }
        warnings = list(provider.diagnostics.warnings)
        if provider.lifecycle_state == ProviderConnectionStatus.NOT_CONFIGURED:
            warnings.append("Provider is not configured.")
        if provider.lifecycle_state in {ProviderConnectionStatus.DEGRADED, ProviderConnectionStatus.ERROR}:
            warnings.append(provider.diagnostics.last_error or "Provider health is degraded.")
        return ProviderTestResult(
            provider_id=provider.provider_id,
            success=success,
            authentication_status="authenticated" if success else "not_authenticated",
            capability_detection=provider.capability_summary,
            provider_version="adapter-not-installed" if provider.lifecycle_state == ProviderConnectionStatus.NOT_CONFIGURED else "provider-framework-v1",
            available_datasets=[capability.value for capability in provider.capability_summary.capabilities] if success else [],
            warnings=warnings,
            response_time_ms=0,
        )

    def configure(self, provider_id: str, values: dict[str, Any]) -> ProviderConfiguration:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        credentials = []
        editable = _editable_fields(provider.provider_type)
        for field_name in editable:
            value = values.get(field_name)
            credentials.append(
                ProviderCredential(
                    field=field_name,
                    label=field_name.replace("_", " ").title(),
                    required=field_name in _required_fields(provider.provider_type),
                    sensitive=field_name in SENSITIVE_FIELDS,
                    configured=value is not None and str(value) != "",
                    value=str(value) if value is not None else None,
                )
            )
        return ProviderConfiguration(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=ProviderConnectionStatus.CONFIGURED,
            credentials=credentials,
            editable_fields=editable,
            enabled=True,
            notes="Configuration accepted by Provider Management. Sensitive values are redacted from API responses.",
        )

    def transition(self, provider_id: str, action: str) -> ManagedProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        normalized = action.strip().lower().replace(" ", "_")
        allowed = {item.lower().replace(" ", "_") for item in provider.actions}
        if normalized not in allowed:
            raise ValueError(f"Unsupported provider action: {action}")
        return provider

    def _managed_provider(self, provider: Provider) -> ManagedProvider:
        lifecycle = _lifecycle_from_provider(provider, self.health)
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
            credentials=_credential_templates(provider.provider_type, lifecycle),
            editable_fields=_editable_fields(provider.provider_type),
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
        )
        return ManagedProvider(
            provider_id=provider.provider_id,
            name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=lifecycle,
            configuration=configuration,
            health=health,
            sync_state=_sync_state(lifecycle, diagnostics),
            diagnostics=diagnostics,
            capability_summary=capability_summary,
            actions=_actions_for(lifecycle),
            source=provider.source,
            note=provider.note,
        )

    def _diagnostics(self, provider: Provider, lifecycle: ProviderConnectionStatus) -> ProviderDiagnostics:
        freshness = provider.freshness.as_dict() if provider.freshness else None
        observation_count = _observation_count(self.provider_observations, provider.provider_id)
        promotion_count = _promotion_count(self.promotion_summary, provider.provider_id)
        last_error = None
        warnings = []
        if lifecycle in {ProviderConnectionStatus.DEGRADED, ProviderConnectionStatus.ERROR}:
            last_error = str(self.health.get("collector_message") or self.health.get("failure_type") or provider.note or "Provider unavailable")
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
            promotion_count=promotion_count,
            collector_version=str(self.health.get("collector_version") or "PowerMonitor local collector"),
            warnings=warnings,
        )


def build_provider_management(
    *,
    provider_registry: ProviderRegistry,
    health: Optional[dict[str, Any]] = None,
    provider_observations: Optional[dict[str, Any]] = None,
    promotion_summary: Optional[dict[str, Any]] = None,
    settings: Any = None,
) -> ProviderManager:
    return ProviderManager(
        provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        settings=settings,
    )


def _lifecycle_from_provider(provider: Provider, health: dict[str, Any]) -> ProviderConnectionStatus:
    if provider.status == ProviderStatus.MISSING:
        return ProviderConnectionStatus.NOT_CONFIGURED
    if provider.status == ProviderStatus.DEGRADED:
        return ProviderConnectionStatus.DEGRADED
    if provider.status == ProviderStatus.CONFIGURED:
        return ProviderConnectionStatus.CONFIGURED
    if provider.status in {ProviderStatus.AVAILABLE, ProviderStatus.SUPPORTED}:
        return ProviderConnectionStatus.CONNECTED
    if str(health.get("status") or "").lower() not in {"", "ok"}:
        return ProviderConnectionStatus.ERROR
    return ProviderConnectionStatus.NOT_CONFIGURED


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


def _editable_fields(provider_type: ProviderType) -> list[str]:
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


def _required_fields(provider_type: ProviderType) -> set[str]:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return set()
    if provider_type == ProviderType.SOLAR:
        return {"cookie"}
    if provider_type == ProviderType.RETAILER:
        return set()
    return {"endpoint"}


def _credential_templates(provider_type: ProviderType, lifecycle: ProviderConnectionStatus) -> list[ProviderCredential]:
    configured = lifecycle != ProviderConnectionStatus.NOT_CONFIGURED
    return [
        ProviderCredential(
            field=field_name,
            label=field_name.replace("_", " ").title(),
            required=field_name in _required_fields(provider_type),
            sensitive=field_name in SENSITIVE_FIELDS,
            configured=configured and field_name not in SENSITIVE_FIELDS,
        )
        for field_name in _editable_fields(provider_type)
    ]


def _future_provider_catalog() -> list[Provider]:
    return [
        Provider("tesla", "Tesla", ProviderType.EV_CHARGER, [ProviderCapability.EV_SESSIONS, ProviderCapability.BATTERY_STATE], note="Future EV, battery, and charger provider."),
        Provider("amber", "Amber", ProviderType.RETAILER, [ProviderCapability.TARIFF_DATA, ProviderCapability.FORECAST], note="Future retailer and tariff provider."),
        Provider("fronius", "Fronius", ProviderType.SOLAR, [ProviderCapability.LIVE_POWER, ProviderCapability.INTERVAL_HISTORY], note="Future solar inverter provider."),
        Provider("enphase", "Enphase", ProviderType.SOLAR, [ProviderCapability.LIVE_POWER, ProviderCapability.INTERVAL_HISTORY, ProviderCapability.BATTERY_STATE], note="Future solar and battery provider."),
    ]


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
