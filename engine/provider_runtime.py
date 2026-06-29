#!/usr/bin/env python3
"""Provider Adapter Runtime for executing provider adapters consistently."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from time import monotonic
from typing import Any, Optional, Protocol

from .provider_framework import ProviderCapability, ProviderObservation


class ProviderExecutionState(str, Enum):
    IDLE = "IDLE"
    DUE = "DUE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BACKING_OFF = "BACKING_OFF"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    DISABLED = "DISABLED"


class ProviderSchedule(str, Enum):
    MANUAL_ONLY = "MANUAL_ONLY"
    EVERY_5_MINUTES = "EVERY_5_MINUTES"
    HOURLY = "HOURLY"
    DAILY = "DAILY"

    @property
    def interval(self) -> Optional[timedelta]:
        return {
            ProviderSchedule.MANUAL_ONLY: None,
            ProviderSchedule.EVERY_5_MINUTES: timedelta(minutes=5),
            ProviderSchedule.HOURLY: timedelta(hours=1),
            ProviderSchedule.DAILY: timedelta(days=1),
        }[self]


@dataclass(frozen=True)
class ProviderAuthentication:
    authenticated: bool = False
    expires_at: Optional[str] = None
    refresh_required: bool = False
    method: str = "unknown"
    message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderExecutionMetrics:
    started_at: str
    finished_at: str
    duration_ms: int
    observations_collected: int = 0
    observations_submitted: int = 0
    retry_count: int = 0
    response_time_ms: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderExecutionResult:
    provider_id: str
    state: ProviderExecutionState
    success: bool
    observations: list[ProviderObservation] = field(default_factory=list)
    metrics: Optional[ProviderExecutionMetrics] = None
    authentication: Optional[ProviderAuthentication] = None
    capabilities: list[ProviderCapability] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None
    next_run_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "state": self.state.value,
            "success": self.success,
            "observations": [observation.as_dict() for observation in self.observations],
            "metrics": self.metrics.as_dict() if self.metrics else None,
            "authentication": self.authentication.as_dict() if self.authentication else None,
            "capabilities": [capability.value for capability in self.capabilities],
            "diagnostics": dict(self.diagnostics),
            "message": self.message,
            "next_run_at": self.next_run_at,
        }


@dataclass(frozen=True)
class ProviderRuntimeHealth:
    provider_id: str
    state: ProviderExecutionState = ProviderExecutionState.IDLE
    schedule: ProviderSchedule = ProviderSchedule.MANUAL_ONLY
    last_run: Optional[str] = None
    last_successful_run: Optional[str] = None
    last_failed_run: Optional[str] = None
    consecutive_failures: int = 0
    average_runtime_ms: Optional[float] = None
    execution_duration_ms: Optional[int] = None
    provider_freshness: Optional[str] = None
    next_run_at: Optional[str] = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["schedule"] = self.schedule.value
        return data


class ProviderAdapter(Protocol):
    provider_id: str
    schedule: ProviderSchedule

    def authenticate(self) -> ProviderAuthentication:
        ...

    def refresh_session(self) -> ProviderAuthentication:
        ...

    def test_connection(self) -> dict[str, Any]:
        ...

    def discover_capabilities(self) -> list[ProviderCapability]:
        ...

    def collect_observations(self) -> list[ProviderObservation]:
        ...

    def collect_live_state(self) -> dict[str, Any]:
        ...

    def collect_diagnostics(self) -> dict[str, Any]:
        ...


class ProviderRuntime:
    """Executes adapters and owns retries, timing, backoff, and submission."""

    def __init__(
        self,
        adapters: list[ProviderAdapter],
        *,
        data_engine: Any = None,
        max_retries: int = 1,
        base_backoff_seconds: int = 60,
        now: Optional[datetime] = None,
    ) -> None:
        self.adapters = {adapter.provider_id: adapter for adapter in adapters}
        self.data_engine = data_engine
        self.max_retries = max(0, int(max_retries))
        self.base_backoff_seconds = max(1, int(base_backoff_seconds))
        self.health: dict[str, ProviderRuntimeHealth] = {
            adapter.provider_id: ProviderRuntimeHealth(
                provider_id=adapter.provider_id,
                schedule=adapter.schedule,
                next_run_at=self._next_run(adapter.schedule, now or datetime.now().astimezone()),
            )
            for adapter in adapters
        }

    def as_dict(self) -> dict[str, Any]:
        active = list(self.adapters)
        failures = sum(health.consecutive_failures for health in self.health.values())
        scheduler_state = "active" if active else "idle"
        return {
            "runtime_health": "degraded" if failures else "healthy",
            "scheduler_status": scheduler_state,
            "active_providers": active,
            "providers": [self.provider_status(provider_id) for provider_id in active],
            "diagnostics": {
                "provider_count": len(active),
                "consecutive_failures": failures,
                "max_retries": self.max_retries,
                "base_backoff_seconds": self.base_backoff_seconds,
            },
        }

    def provider_status(self, provider_id: str) -> dict[str, Any]:
        adapter = self._adapter(provider_id)
        health = self.health[provider_id]
        return {
            "provider_id": provider_id,
            "schedule": adapter.schedule.value,
            "health": health.as_dict(),
            "capabilities": [capability.value for capability in adapter.discover_capabilities()],
        }

    def run_due(self, now: Optional[datetime] = None) -> list[ProviderExecutionResult]:
        current = now or datetime.now().astimezone()
        results = []
        for adapter in self.adapters.values():
            if self.is_due(adapter.provider_id, current):
                results.append(self.run_provider(adapter.provider_id, now=current))
        return results

    def is_due(self, provider_id: str, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now().astimezone()
        adapter = self._adapter(provider_id)
        if adapter.schedule == ProviderSchedule.MANUAL_ONLY:
            return False
        health = self.health[provider_id]
        if health.state == ProviderExecutionState.BACKING_OFF and health.next_run_at:
            return _parse_datetime(health.next_run_at) <= current
        if health.next_run_at:
            return _parse_datetime(health.next_run_at) <= current
        return True

    def run_provider(self, provider_id: str, *, now: Optional[datetime] = None) -> ProviderExecutionResult:
        adapter = self._adapter(provider_id)
        started_at = now or datetime.now().astimezone()
        self._set_state(provider_id, ProviderExecutionState.RUNNING, started_at)
        start_clock = monotonic()
        retry_count = 0
        auth = adapter.authenticate()
        if auth.refresh_required or not auth.authenticated:
            auth = adapter.refresh_session()
        while True:
            try:
                if not auth.authenticated:
                    raise PermissionError(auth.message or "Provider authentication failed")
                observations = adapter.collect_observations()
                diagnostics = adapter.collect_diagnostics()
                submitted = self._submit_observations(observations)
                finished_at = datetime.now().astimezone()
                metrics = ProviderExecutionMetrics(
                    started_at=started_at.isoformat(timespec="seconds"),
                    finished_at=finished_at.isoformat(timespec="seconds"),
                    duration_ms=int((monotonic() - start_clock) * 1000),
                    observations_collected=len(observations),
                    observations_submitted=submitted,
                    retry_count=retry_count,
                )
                result = ProviderExecutionResult(
                    provider_id=provider_id,
                    state=ProviderExecutionState.SUCCESS,
                    success=True,
                    observations=observations,
                    metrics=metrics,
                    authentication=auth,
                    capabilities=adapter.discover_capabilities(),
                    diagnostics=diagnostics,
                    message="Provider execution succeeded.",
                    next_run_at=self._next_run(adapter.schedule, finished_at),
                )
                self._record_success(provider_id, result)
                return result
            except Exception as exc:
                if retry_count < self.max_retries:
                    retry_count += 1
                    if isinstance(exc, PermissionError):
                        auth = adapter.refresh_session()
                    continue
                finished_at = datetime.now().astimezone()
                metrics = ProviderExecutionMetrics(
                    started_at=started_at.isoformat(timespec="seconds"),
                    finished_at=finished_at.isoformat(timespec="seconds"),
                    duration_ms=int((monotonic() - start_clock) * 1000),
                    retry_count=retry_count,
                )
                result = ProviderExecutionResult(
                    provider_id=provider_id,
                    state=ProviderExecutionState.FAILED,
                    success=False,
                    metrics=metrics,
                    authentication=auth,
                    capabilities=adapter.discover_capabilities(),
                    diagnostics=_safe_diagnostics(adapter),
                    message=str(exc),
                    next_run_at=self._backoff(provider_id, finished_at),
                )
                self._record_failure(provider_id, result)
                return result

    def _adapter(self, provider_id: str) -> ProviderAdapter:
        adapter = self.adapters.get(provider_id)
        if adapter is None:
            raise ValueError(f"Unknown runtime provider: {provider_id}")
        return adapter

    def _submit_observations(self, observations: list[ProviderObservation]) -> int:
        if not observations:
            return 0
        if self.data_engine is None:
            return 0
        persisted = self.data_engine.persist_provider_observations(observations)
        return len(persisted)

    def _set_state(self, provider_id: str, state: ProviderExecutionState, now: datetime) -> None:
        current = self.health[provider_id]
        self.health[provider_id] = ProviderRuntimeHealth(
            provider_id=provider_id,
            state=state,
            schedule=current.schedule,
            last_run=now.isoformat(timespec="seconds"),
            last_successful_run=current.last_successful_run,
            last_failed_run=current.last_failed_run,
            consecutive_failures=current.consecutive_failures,
            average_runtime_ms=current.average_runtime_ms,
            execution_duration_ms=current.execution_duration_ms,
            provider_freshness=current.provider_freshness,
            next_run_at=current.next_run_at,
            diagnostics=current.diagnostics,
        )

    def _record_success(self, provider_id: str, result: ProviderExecutionResult) -> None:
        current = self.health[provider_id]
        duration = result.metrics.duration_ms if result.metrics else None
        if current.average_runtime_ms is None:
            average = float(duration or 0)
        else:
            average = (current.average_runtime_ms + float(duration or 0)) / 2
        self.health[provider_id] = ProviderRuntimeHealth(
            provider_id=provider_id,
            state=ProviderExecutionState.SUCCESS,
            schedule=current.schedule,
            last_run=result.metrics.finished_at if result.metrics else current.last_run,
            last_successful_run=result.metrics.finished_at if result.metrics else current.last_run,
            last_failed_run=current.last_failed_run,
            consecutive_failures=0,
            average_runtime_ms=round(average, 2),
            execution_duration_ms=duration,
            provider_freshness=result.metrics.finished_at if result.metrics else None,
            next_run_at=result.next_run_at,
            diagnostics=result.diagnostics,
        )

    def _record_failure(self, provider_id: str, result: ProviderExecutionResult) -> None:
        current = self.health[provider_id]
        failures = current.consecutive_failures + 1
        self.health[provider_id] = ProviderRuntimeHealth(
            provider_id=provider_id,
            state=ProviderExecutionState.BACKING_OFF,
            schedule=current.schedule,
            last_run=result.metrics.finished_at if result.metrics else current.last_run,
            last_successful_run=current.last_successful_run,
            last_failed_run=result.metrics.finished_at if result.metrics else current.last_run,
            consecutive_failures=failures,
            average_runtime_ms=current.average_runtime_ms,
            execution_duration_ms=result.metrics.duration_ms if result.metrics else None,
            provider_freshness=current.provider_freshness,
            next_run_at=result.next_run_at,
            diagnostics={"last_error": result.message},
        )

    def _next_run(self, schedule: ProviderSchedule, now: datetime) -> Optional[str]:
        interval = schedule.interval
        if interval is None:
            return None
        return (now + interval).isoformat(timespec="seconds")

    def _backoff(self, provider_id: str, now: datetime) -> str:
        failures = self.health[provider_id].consecutive_failures + 1
        delay = self.base_backoff_seconds * (2 ** max(0, failures - 1))
        return (now + timedelta(seconds=delay)).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _safe_diagnostics(adapter: ProviderAdapter) -> dict[str, Any]:
    try:
        return adapter.collect_diagnostics()
    except Exception:
        return {}
