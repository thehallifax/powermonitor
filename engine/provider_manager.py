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
        settings: Any = None,
    ) -> None:
        self.registry = registry
        self.health = health or {}
        self.provider_observations = provider_observations or {}
        self.promotion_summary = promotion_summary or {}
        self.runtime_status = runtime_status or {}
        self.connection_testers = connection_testers or {}
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
            "onboarding_states": [state.value for state in ProviderOnboardingState],
            "actions": ["Configure", "Edit", "Reconnect", "Disconnect", "Test Connection", "Disable", "Enable", "Remove"],
        }

    def test_connection(self, provider_id: str) -> ProviderTestResult:
        provider = self.get(provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_id}")
        tester = self.connection_testers.get(provider_id)
        if tester:
            return self._adapter_test_connection(provider, tester)
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
        status = "success" if success and not warnings else "warning" if success else "failure"
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
                "adapter": "runtime",
            },
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
        missing_required = [
            credential.label for credential in credentials
            if credential.required and not credential.configured
        ]
        lifecycle = ProviderConnectionStatus.CONFIGURED if not missing_required else ProviderConnectionStatus.NOT_CONFIGURED
        notes = (
            "Configuration accepted by Provider Management. Sensitive values are redacted from API responses. Run Test Connection before relying on this provider."
            if not missing_required else
            f"Configuration still needs: {', '.join(missing_required)}."
        )
        return ProviderConfiguration(
            provider_id=provider.provider_id,
            provider_name=provider.name,
            provider_type=provider.provider_type,
            lifecycle_state=lifecycle,
            credentials=credentials,
            editable_fields=editable,
            enabled=True,
            notes=notes,
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
    runtime_status: Optional[dict[str, Any]] = None,
    connection_testers: Optional[dict[str, Any]] = None,
    settings: Any = None,
) -> ProviderManager:
    return ProviderManager(
        provider_registry,
        health=health,
        provider_observations=provider_observations,
        promotion_summary=promotion_summary,
        runtime_status=runtime_status,
        connection_testers=connection_testers,
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
        return "Connection test succeeded. Provider data can be used for PowerMonitor analysis."
    if status == "warning":
        detail = warnings[0] if warnings else "The provider responded, but some checks need attention."
        return f"Connection test completed with a warning: {detail}"
    detail = warnings[0] if warnings else "PowerMonitor could not connect to the provider."
    return f"Connection test failed: {detail}"


def _test_next_step(status: str) -> str:
    if status == "success":
        return "Keep this provider connected and watch the next scheduled sync."
    if status == "warning":
        return "Review the warning, then run Test Connection again after fixing it."
    return "Refresh the provider session or update the required credential, then run Test Connection again."


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
        state_label=state.value.replace("_", " ").title(),
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
        connection_status=_connection_status(state),
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
    if missing_required:
        return ProviderOnboardingState.CONFIGURATION_REQUIRED
    if configuration.lifecycle_state == ProviderConnectionStatus.CONFIGURED and not diagnostics.last_successful_sync:
        return ProviderOnboardingState.READY_TO_CONNECT
    return ProviderOnboardingState.READY_TO_CONNECT


def _required_information(configuration: ProviderConfiguration) -> list[str]:
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


def _connection_status(state: ProviderOnboardingState) -> str:
    return {
        ProviderOnboardingState.NOT_CONFIGURED: "Not configured yet.",
        ProviderOnboardingState.CONFIGURATION_REQUIRED: "Configuration is missing required information.",
        ProviderOnboardingState.READY_TO_CONNECT: "Ready to test before the first sync.",
        ProviderOnboardingState.CONNECTING: "Connection test is running.",
        ProviderOnboardingState.CONNECTED: "Connected and available for analysis.",
        ProviderOnboardingState.SYNCING: "Connected and syncing now.",
        ProviderOnboardingState.WARNING: "Connected, but recent sync health needs review.",
        ProviderOnboardingState.ERROR: "Connection is failing.",
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
    return ["Check the required information", "Reconnect or refresh the provider session", "Run Test Connection", "Review technical diagnostics only if the simple steps do not resolve it"]


def _sync_frequency(provider_type: ProviderType) -> str:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return "Manual import"
    if provider_type == ProviderType.RETAILER:
        return "When billing or tariff settings change"
    return "Collector schedule"


def _next_sync_label(provider_type: ProviderType) -> str:
    if provider_type == ProviderType.GENERIC_IMPORT:
        return "Next manual import"
    if provider_type == ProviderType.RETAILER:
        return "Next settings update"
    return "Next collector run"


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
    if provider_type == ProviderType.HOME_AUTOMATION:
        return {"api_key", "endpoint"}
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
