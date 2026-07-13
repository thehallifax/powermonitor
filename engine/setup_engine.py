#!/usr/bin/env python3
"""Continuous household setup guidance for PowerMonitor."""

from datetime import datetime, timedelta
from typing import Any, Optional

from .import_framework import ImportSource
from .models import (
    AdvisorCollection,
    HouseholdCapabilitySummary,
    HouseholdContext,
    SetupChecklist,
    SetupProgress,
    SetupSection,
    SetupTask,
    SetupTaskPriority,
)
from .provider_framework import ProviderCapability, ProviderRegistry
from .provider_manager import ManagedProvider, ProviderConnectionStatus, ProviderManager


class SetupEngine:
    """Derive setup readiness from existing household and engine state."""

    name = "setup_engine"

    def __init__(self, household_context: HouseholdContext) -> None:
        self.household_context = household_context

    def build_checklist(
        self,
        capability_summary: HouseholdCapabilitySummary,
        *,
        collector_health: Optional[dict[str, Any]] = None,
        import_sources: Optional[list[ImportSource]] = None,
        advisors: Optional[AdvisorCollection] = None,
        goals: Optional[list[Any]] = None,
        recommendations: Optional[list[Any]] = None,
        settings: Optional[Any] = None,
        provider_registry: Optional[ProviderRegistry] = None,
        provider_management: Optional[ProviderManager] = None,
        solar_data_available: Optional[bool] = None,
        now: Optional[datetime] = None,
    ) -> SetupChecklist:
        now = now or datetime.now().astimezone()
        collector_health = collector_health or {}
        import_sources = import_sources or []
        recommendations = recommendations or []
        goals = goals or []

        sections = [
            SetupSection(
                "household",
                "Household",
                [
                    self._tariff_task(capability_summary, settings),
                    self._billing_cycle_task(settings),
                    self._goals_task(goals),
                ],
            ),
            SetupSection(
                "energy_providers",
                "Energy Providers",
                self._provider_tasks(capability_summary, provider_registry, provider_management, solar_data_available),
            ),
            SetupSection(
                "imports",
                "Imports",
                self._import_tasks(capability_summary, import_sources, provider_registry, provider_management),
            ),
            SetupSection(
                "data_quality",
                "Advice Readiness",
                [
                    self._collector_task(collector_health),
                    self._freshness_task(capability_summary, now),
                    self._energy_score_task(capability_summary),
                ],
            ),
        ]
        sections = [
            SetupSection(section.section_id, section.title, [task for task in section.tasks if task])
            for section in sections
        ]
        progress = self._progress(sections)
        return SetupChecklist(
            household_id=self.household_context.household.household_id,
            generated_at=now.isoformat(timespec="seconds"),
            progress=progress,
            sections=sections,
        )

    def _progress(self, sections: list[SetupSection]) -> SetupProgress:
        tasks = [task for section in sections for task in section.tasks if task.status != "future"]
        completed = [task for task in tasks if task.status == "complete"]
        remaining = sorted(
            [task for task in tasks if task.status == "action_needed"],
            key=lambda task: (_priority_rank(task.priority.value), task.title),
        )
        completion_pct = 100 if not tasks else int(round((len(completed) / len(tasks)) * 100))
        return SetupProgress(
            completion_pct=completion_pct,
            completed_tasks=len(completed),
            total_tasks=len(tasks),
            remaining_tasks=remaining,
        )

    def _tariff_task(self, capability_summary: HouseholdCapabilitySummary, settings: Any = None) -> SetupTask:
        tariff = capability_summary.get("tariff")
        setup_mode = str(getattr(settings, "tariff_setup_mode", "") or "")
        plan = str(getattr(settings, "current_plan", "") or "")
        saved = bool(getattr(settings, "billing_cycle_configured", False))
        ready = bool(tariff and tariff.usable) or bool(saved and setup_mode in {"catalog", "manual"} and plan)
        return task(
            "configure_tariff",
            "Choose your electricity plan",
            ready,
            "high",
            "2 minutes",
            "Choose Plan",
            "#setup-tariff",
            "Your electricity plan is saved." if ready else "Choose your retailer and electricity plan so PowerMonitor can explain costs.",
            "settings.tariff" if ready and not (tariff and tariff.usable) else "HouseholdCapabilitySummary.tariff",
        )

    def _billing_cycle_task(self, settings: Any) -> SetupTask:
        ready = bool(
            getattr(settings, "current_billing_period_start", None)
            and getattr(settings, "current_billing_period_end", None)
        ) or bool(getattr(settings, "billing_cycle_configured", False)) or bool(
            getattr(settings, "billing_period_start_day", None)
        )
        return task(
            "configure_billing_cycle",
            "Set your billing cycle",
            ready,
            "medium",
            "2 minutes",
            "Set Billing Cycle",
            "#setup-billing",
            "Your billing cycle is saved." if ready else "Set the day your bill usually starts so projections line up with your bill.",
            "settings.billing_cycle",
        )

    def _goals_task(self, goals: list[Any]) -> SetupTask:
        ready = bool(goals)
        return task(
            "configure_household_goals",
            "Choose household goals",
            ready,
            "medium",
            "3 minutes",
            "Review Goals",
            "#home-goals",
            "Household goals are ready." if ready else "Optional: add goals later if you want PowerMonitor to track a target.",
            "GoalsEngine",
        )

    def _provider_tasks(
        self,
        capability_summary: HouseholdCapabilitySummary,
        provider_registry: Optional[ProviderRegistry],
        provider_management: Optional[ProviderManager] = None,
        solar_data_available: Optional[bool] = None,
    ) -> list[SetupTask]:
        tasks = []
        if self._household_declares("solar"):
            solar = capability_summary.get("solar")
            solar_provider = _first_provider(provider_registry, ProviderCapability.DAILY_SUMMARY)
            managed_provider = _first_managed_provider(provider_management, ProviderCapability.DAILY_SUMMARY)
            rationale = solar.reason if solar else "Solar capability is unavailable."
            source = "HouseholdCapabilitySummary.solar"
            verified_solar = bool(solar_data_available) if solar_data_available is not None else bool(solar and solar.usable)
            action_href = "#system"
            action_label = "Add Solar"
            if managed_provider:
                configured = _provider_configuration_ready(managed_provider)
                if verified_solar:
                    rationale = f"Verified normalized solar generation readings are available from {managed_provider.name}."
                    action_label = "Review Solar Data"
                elif not configured:
                    rationale = f"{managed_provider.name} is selected, but connection details are missing."
                    action_label = f"Connect {managed_provider.name}"
                elif managed_provider.lifecycle_state in {ProviderConnectionStatus.ERROR, ProviderConnectionStatus.DEGRADED}:
                    rationale = f"{managed_provider.name} needs attention, and no usable solar generation readings are stored yet."
                    action_label = "Review Provider"
                else:
                    rationale = f"{managed_provider.name} is configured, but PowerMonitor has not collected usable solar generation readings yet."
                    action_label = "Collect Solar Data"
                source = "ProviderManagement.solar"
                action_href = f"#system-provider-{managed_provider.provider_id}"
            elif solar_provider:
                if verified_solar:
                    rationale = "Verified normalized solar generation readings are available."
                    action_label = "Review Solar Data"
                else:
                    rationale = f"{solar_provider.name} can add solar production and export history."
                source = "ProviderRegistry.solar"
                action_href = f"#system-provider-registry-{solar_provider.provider_id}"
            elif verified_solar:
                rationale = "Verified normalized solar generation readings are available."
                action_label = "Review Solar Data"
            tasks.append(
                task(
                    "connect_solar_provider",
                    "Add solar data",
                    verified_solar,
                    "high",
                    "5 minutes",
                    action_label,
                    action_href,
                    rationale if solar_provider or managed_provider or verified_solar else "Add solar data now, or skip it and add it later.",
                    source,
                )
            )
        return tasks

    def _import_tasks(
        self,
        capability_summary: HouseholdCapabilitySummary,
        import_sources: list[ImportSource],
        provider_registry: Optional[ProviderRegistry],
        provider_management: Optional[ProviderManager] = None,
    ) -> list[SetupTask]:
        tasks = []
        if self._household_declares("ev"):
            ev = capability_summary.get("ev")
            supported = ", ".join(
                sorted({extension for source in import_sources for extension in source.supported_extensions})
            )
            detail = ev.reason if ev else "EV charging capability is unavailable."
            if supported:
                detail = f"{detail} Supported files: {supported}."
            import_provider_names = [
                provider.name
                for provider in (provider_registry.providers_with_capability(ProviderCapability.IMPORT_ONLY) if provider_registry else [])
            ]
            managed_imports = [
                provider for provider in (provider_management.providers() if provider_management else [])
                if ProviderCapability.IMPORT_ONLY in provider.capability_summary.capabilities
            ]
            if managed_imports:
                import_provider_names = [provider.name for provider in managed_imports]
            if import_provider_names:
                detail = f"{detail} Available upload option: {', '.join(import_provider_names)}."
            tasks.append(
                task(
                    "upload_ev_charging_history",
                    "Add EV charging history",
                    bool(ev and ev.usable),
                    "high",
                    "4 minutes",
                    "Add EV History",
                    "#imports",
                    detail if supported or import_provider_names or (ev and ev.usable) else "Import EV charging history now, or skip it and add it later.",
                    "ProviderManagement.imports" if managed_imports else "Import Framework",
                )
            )
        return tasks

    def _collector_task(self, collector_health: dict[str, Any]) -> SetupTask:
        status = str(collector_health.get("status") or "unknown")
        ready = status == "ok"
        return task(
            "collector_offline",
            "Live updates unavailable",
            ready,
            "high",
            "3 minutes",
            "Review Live Data",
            "#system-live-data",
            "Live updates are working." if ready else "PowerMonitor can still use saved readings and imports, but right-now checks are limited.",
            "Data connection",
        )

    def _freshness_task(
        self,
        capability_summary: HouseholdCapabilitySummary,
        now: datetime,
    ) -> SetupTask:
        data_freshness = capability_summary.get("data_freshness")
        observed_at = data_freshness.freshness.observed_at if data_freshness and data_freshness.freshness else None
        latest = _parse_datetime(observed_at)
        ready = bool(latest and now - latest <= timedelta(hours=24))
        return task(
            "data_older_than_24_hours",
            "Refresh household data",
            ready,
            "high",
            "2 minutes",
            "Can I trust today's advice?",
            "#system-data-quality",
            "Household data is recent." if ready else "Recent household data helps PowerMonitor give confident advice.",
            "DataEngine.freshness",
        )

    def _energy_score_task(self, capability_summary: HouseholdCapabilitySummary) -> SetupTask:
        freshness = capability_summary.get("data_freshness")
        ready = bool(freshness and freshness.usable)
        return task(
            "review_energy_score",
            "Review Energy Score",
            ready,
            "low",
            "1 minute",
            "Why is my score what it is?",
            "#energy-score-explanation",
            freshness.reason if freshness else "Energy Score needs household readings.",
            "Energy Score",
        )

    def _household_declares(self, capability_id: str) -> bool:
        for capability in self.household_context.household.capabilities:
            if capability.capability_id == capability_id and capability.enabled:
                return True
        return False


def task(
    task_id: str,
    title: str,
    ready: bool,
    priority: str,
    estimated_effort: str,
    action_label: str,
    action_href: str,
    rationale: str,
    source: str,
) -> SetupTask:
    return SetupTask(
        task_id=task_id,
        title=title,
        status="complete" if ready else "action_needed",
        priority=SetupTaskPriority(priority),
        estimated_effort=estimated_effort,
        action_label=action_label,
        action_href=action_href,
        rationale=rationale,
        source=source,
    )


def _first_provider(
    provider_registry: Optional[ProviderRegistry],
    capability: ProviderCapability,
):
    if not provider_registry:
        return None
    providers = provider_registry.providers_with_capability(capability)
    return providers[0] if providers else None


def _first_managed_provider(
    provider_management: Optional[ProviderManager],
    capability: ProviderCapability,
):
    if not provider_management:
        return None
    providers = [
        provider for provider in provider_management.providers()
        if capability in provider.capability_summary.capabilities
    ]
    return providers[0] if providers else None


def _provider_configuration_ready(provider: ManagedProvider) -> bool:
    credentials = list(provider.configuration.credentials or [])
    required = [credential for credential in credentials if credential.required]
    if required and all(credential.configured for credential in required):
        return True
    if provider.provider_id == "solaredge" and any(
        credential.field == "cookie" and credential.configured for credential in credentials
    ):
        return True
    if required:
        return False
    return provider.configuration.lifecycle_state != ProviderConnectionStatus.NOT_CONFIGURED


def _priority_rank(priority: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(priority, 3)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed
