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
from .provider_manager import ProviderConnectionStatus, ProviderManager, ProviderSyncState


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
                    self._tariff_task(capability_summary),
                    self._billing_cycle_task(settings),
                    self._goals_task(goals),
                ],
            ),
            SetupSection(
                "energy_providers",
                "Energy Providers",
                self._provider_tasks(capability_summary, provider_registry, provider_management),
            ),
            SetupSection(
                "imports",
                "Imports",
                self._import_tasks(capability_summary, import_sources, provider_registry, provider_management),
            ),
            SetupSection(
                "data_quality",
                "Data Quality",
                [
                    self._collector_task(collector_health),
                    self._freshness_task(capability_summary, now),
                ],
            ),
            SetupSection(
                "recommendations",
                "Recommendations",
                [
                    self._recommendation_centre_task(capability_summary, recommendations),
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

    def _tariff_task(self, capability_summary: HouseholdCapabilitySummary) -> SetupTask:
        tariff = capability_summary.get("tariff")
        ready = bool(tariff and tariff.usable)
        return task(
            "configure_tariff",
            "Configure electricity tariff",
            ready,
            "high",
            "2 minutes",
            "Edit Tariff Plan",
            "#setup-billing",
            tariff.reason if tariff else "Tariff capability is unavailable.",
            "HouseholdCapabilitySummary.tariff",
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
            "Configure billing cycle",
            ready,
            "medium",
            "2 minutes",
            "Edit Billing Cycle",
            "#setup-billing",
            "Billing cycle is configured." if ready else "Set the billing cycle start day used for reports and projections.",
            "settings.billing_cycle",
        )

    def _goals_task(self, goals: list[Any]) -> SetupTask:
        ready = bool(goals)
        return task(
            "configure_household_goals",
            "Configure household goals",
            ready,
            "medium",
            "3 minutes",
            "Review Goals",
            "#home",
            "Goal Engine has active household goals." if ready else "Goal Engine has no active household goals to evaluate.",
            "GoalsEngine",
        )

    def _provider_tasks(
        self,
        capability_summary: HouseholdCapabilitySummary,
        provider_registry: Optional[ProviderRegistry],
        provider_management: Optional[ProviderManager] = None,
    ) -> list[SetupTask]:
        tasks = []
        if self._household_declares("solar"):
            solar = capability_summary.get("solar")
            solar_provider = _first_provider(provider_registry, ProviderCapability.DAILY_SUMMARY)
            managed_provider = _first_managed_provider(provider_management, ProviderCapability.DAILY_SUMMARY)
            rationale = solar.reason if solar else "Solar capability is unavailable."
            source = "HouseholdCapabilitySummary.solar"
            provider_ready = bool(solar and solar.usable)
            action_href = "#system"
            if managed_provider:
                provider_ready = (
                    managed_provider.configuration.lifecycle_state != ProviderConnectionStatus.NOT_CONFIGURED
                    and managed_provider.lifecycle_state in {ProviderConnectionStatus.CONNECTED, ProviderConnectionStatus.CONFIGURED, ProviderConnectionStatus.SYNCING}
                    and managed_provider.sync_state in {ProviderSyncState.SYNCED, ProviderSyncState.UNKNOWN}
                )
                rationale = f"{rationale} Provider: {managed_provider.name} ({managed_provider.lifecycle_state.value})."
                source = "ProviderManagement.solar"
                action_href = f"#system-provider-{managed_provider.provider_id}"
            elif solar_provider:
                rationale = f"{rationale} Provider: {solar_provider.name} ({solar_provider.status.value})."
                source = "ProviderRegistry.solar"
            tasks.append(
                task(
                    "connect_solar_provider",
                    "Connect solar provider",
                    provider_ready,
                    "high",
                    "5 minutes",
                    "Manage Provider",
                    action_href,
                    rationale,
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
                detail = f"{detail} Supported upload paths: {supported}."
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
                detail = f"{detail} Import providers: {', '.join(import_provider_names)}."
            tasks.append(
                task(
                    "upload_ev_charging_history",
                    "Upload EV charging history",
                    bool(ev and ev.usable),
                    "high",
                    "4 minutes",
                    "Upload History",
                    "#imports",
                    detail,
                    "ProviderManagement.imports" if managed_imports else "Import Framework",
                )
            )
        return tasks

    def _collector_task(self, collector_health: dict[str, Any]) -> SetupTask:
        status = str(collector_health.get("status") or "unknown")
        ready = status == "ok"
        return task(
            "collector_offline",
            "Collector offline",
            ready,
            "high",
            "3 minutes",
            "Review Collector Health",
            "#system-collector",
            str(collector_health.get("collector_message") or collector_health.get("message") or status),
            "Collector Health",
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
            "Data older than 24 hours",
            ready,
            "high",
            "2 minutes",
            "Review Data Quality",
            "#system-data-quality",
            data_freshness.reason if data_freshness else "No data freshness capability is available.",
            "DataEngine.freshness",
        )

    def _recommendation_centre_task(
        self,
        capability_summary: HouseholdCapabilitySummary,
        recommendations: list[Any],
    ) -> SetupTask:
        recs = capability_summary.get("recommendations")
        ready = bool(recs and recs.usable) or bool(recommendations)
        return task(
            "explore_recommendation_centre",
            "Explore Recommendation Centre",
            ready,
            "low",
            "2 minutes",
            "Open Recommendation Centre",
            "#recommendations",
            "Recommendation Engine is producing household opportunities." if ready else "Recommendations are not ready yet.",
            "Recommendation Engine",
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
            "Review Home",
            "#home",
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
