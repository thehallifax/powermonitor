#!/usr/bin/env python3
"""Notification readiness candidates for PowerMonitor.

The Notification Engine prepares local notification candidates only. It does
not send email, SMS, push, webhooks, or any external alert.
"""

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from .evidence_quality import EvidenceQuality
from .state_change_engine import StateChangeType


class NotificationType(str, Enum):
    CHARGE_NOW = "charge_now"
    WAIT_FOR_CHEAPER_WINDOW = "wait_for_cheaper_window"
    PEAK_PERIOD_STARTED = "peak_period_started"
    LOW_COST_WINDOW_STARTED = "low_cost_window_started"
    COLLECTOR_STALE = "collector_stale"
    COLLECTOR_RECOVERED = "collector_recovered"
    PROVIDER_ERROR = "provider_error"
    BILL_TRACKING_HIGH = "bill_tracking_high"
    SOLAR_OPPORTUNITY = "solar_opportunity"
    SETUP_INCOMPLETE = "setup_incomplete"
    GOAL_PROGRESS = "goal_progress"
    EVIDENCE_DEGRADED = "evidence_degraded"


class NotificationPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationChannel(str, Enum):
    LOCAL_UI = "local_ui"
    FUTURE_PUSH = "future_push"
    FUTURE_EMAIL = "future_email"
    FUTURE_WEBHOOK = "future_webhook"


class NotificationSuppressionReason(str, Enum):
    NONE = "none"
    NOTIFICATIONS_DISABLED = "notifications_disabled"
    TYPE_DISABLED = "type_disabled"
    BELOW_PRIORITY_THRESHOLD = "below_priority_threshold"
    QUIET_HOURS = "quiet_hours"
    LOW_EVIDENCE_QUALITY = "low_evidence_quality"
    RECENT_DUPLICATE = "recent_duplicate"
    STALE_RECOMMENDATION = "stale_recommendation"
    CONTEXT_IRRELEVANT = "context_irrelevant"
    SETUP_INCOMPLETE = "setup_incomplete"
    NOT_ACTIONABLE = "not_actionable"
    EXPIRED = "expired"


class NotificationDeliveryState(str, Enum):
    PENDING = "pending"
    SUPPRESSED = "suppressed"
    SENT = "sent"
    EXPIRED = "expired"
    DISABLED = "disabled"


@dataclass(frozen=True)
class NotificationRulePreference:
    notification_type: NotificationType
    enabled: bool = True
    cooldown_minutes: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_type": self.notification_type.value,
            "enabled": self.enabled,
            "cooldown_minutes": self.cooldown_minutes,
        }


@dataclass(frozen=True)
class NotificationQuietHours:
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NotificationPreferences:
    household_id: str = "default_household"
    enabled: bool = True
    priority_threshold: NotificationPriority = NotificationPriority.LOW
    quiet_hours: NotificationQuietHours = field(default_factory=NotificationQuietHours)
    channel_preference: NotificationChannel = NotificationChannel.LOCAL_UI
    cooldown_minutes: int = 120
    rule_preferences: list[NotificationRulePreference] = field(default_factory=list)

    def rule_for(self, notification_type: NotificationType) -> Optional[NotificationRulePreference]:
        for rule in self.rule_preferences:
            if rule.notification_type == notification_type:
                return rule
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "enabled": self.enabled,
            "priority_threshold": self.priority_threshold.value,
            "quiet_hours": self.quiet_hours.as_dict(),
            "channel_preference": self.channel_preference.value,
            "cooldown_minutes": self.cooldown_minutes,
            "rule_preferences": [rule.as_dict() for rule in self.rule_preferences],
        }


@dataclass(frozen=True)
class NotificationEvidence:
    source: str
    summary: str
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    observed_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quality"] = self.quality.value
        return data


@dataclass(frozen=True)
class NotificationCandidate:
    candidate_id: str
    household_id: str
    notification_type: NotificationType
    title: str
    message: str
    priority: NotificationPriority
    created_at: str
    valid_until: str
    evidence: list[NotificationEvidence] = field(default_factory=list)
    recommended_channel: NotificationChannel = NotificationChannel.LOCAL_UI
    ready: bool = True
    suppression_reason: NotificationSuppressionReason = NotificationSuppressionReason.NONE
    source_event_key: Optional[str] = None

    @property
    def type(self) -> str:
        return self.notification_type.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "household_id": self.household_id,
            "type": self.notification_type.value,
            "title": self.title,
            "message": self.message,
            "priority": self.priority.value,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "evidence": [item.as_dict() for item in self.evidence],
            "recommended_channel": self.recommended_channel.value,
            "ready": self.ready,
            "suppression_reason": self.suppression_reason.value,
            "source_event_key": self.source_event_key,
        }


@dataclass(frozen=True)
class NotificationReadiness:
    household_id: str
    generated_at: str
    candidates: list[NotificationCandidate] = field(default_factory=list)

    @property
    def ready_candidates(self) -> list[NotificationCandidate]:
        return [item for item in self.candidates if item.ready]

    @property
    def suppressed_candidates(self) -> list[NotificationCandidate]:
        return [item for item in self.candidates if not item.ready]

    def as_dict(self) -> dict[str, Any]:
        counts_by_type: dict[str, int] = {}
        counts_by_priority: dict[str, int] = {}
        suppression_counts: dict[str, int] = {}
        for candidate in self.candidates:
            counts_by_type[candidate.notification_type.value] = counts_by_type.get(candidate.notification_type.value, 0) + 1
            counts_by_priority[candidate.priority.value] = counts_by_priority.get(candidate.priority.value, 0) + 1
            if not candidate.ready:
                reason = candidate.suppression_reason.value
                suppression_counts[reason] = suppression_counts.get(reason, 0) + 1
        return {
            "household_id": self.household_id,
            "generated_at": self.generated_at,
            "count": len(self.candidates),
            "ready_count": len(self.ready_candidates),
            "suppressed_count": len(self.suppressed_candidates),
            "counts_by_type": counts_by_type,
            "counts_by_priority": counts_by_priority,
            "suppression_counts": suppression_counts,
            "candidates": [item.as_dict() for item in self.candidates],
        }


@dataclass(frozen=True)
class NotificationHistory:
    history_id: str
    household_id: str
    stable_key: str
    candidate_id: Optional[str]
    notification_type: NotificationType
    priority: NotificationPriority
    created_at: str
    expires_at: str
    delivery_state: NotificationDeliveryState
    evidence_summary: str
    sent_at: Optional[str] = None
    suppressed_at: Optional[str] = None
    suppression_reason: NotificationSuppressionReason = NotificationSuppressionReason.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_id": self.history_id,
            "household_id": self.household_id,
            "stable_key": self.stable_key,
            "candidate_id": self.candidate_id,
            "notification_type": self.notification_type.value,
            "priority": self.priority.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "delivery_state": self.delivery_state.value,
            "evidence_summary": self.evidence_summary,
            "sent_at": self.sent_at,
            "suppressed_at": self.suppressed_at,
            "suppression_reason": self.suppression_reason.value,
        }


class NotificationEngine:
    """Converts platform intelligence outputs into notification candidates."""

    health_notification_types = {
        NotificationType.COLLECTOR_STALE,
        NotificationType.COLLECTOR_RECOVERED,
        NotificationType.PROVIDER_ERROR,
        NotificationType.EVIDENCE_DEGRADED,
    }

    def build(
        self,
        *,
        state_changes: Optional[list[Any]] = None,
        household_state_snapshot: Optional[Any] = None,
        context: Optional[Any] = None,
        household_decisions: Optional[list[Any]] = None,
        recommendation_presentations: Optional[list[Any]] = None,
        advisors: Optional[Any] = None,
        setup: Optional[Any] = None,
        evidence_quality: Optional[Any] = None,
        preferences: Optional[NotificationPreferences] = None,
        history: Optional[list[Any]] = None,
        recently_notified_event_keys: Optional[set[str]] = None,
        allow_legacy_advisor_notifications: bool = True,
        now: Optional[datetime] = None,
    ) -> NotificationReadiness:
        created = now or datetime.now().astimezone()
        household_id = self._household_id(household_state_snapshot, context, advisors, setup)
        preferences = preferences or NotificationPreferences(household_id=household_id)
        quality = self._evidence_quality(evidence_quality, context, advisors, household_state_snapshot)
        recent_keys = set(recently_notified_event_keys or set())
        recent_keys.update(self._recent_history_keys(history or [], preferences, created))
        candidates: list[NotificationCandidate] = []
        seen_keys: set[str] = set()

        for change in state_changes or []:
            candidate = self._candidate_from_state_change(change, household_id, created, quality, context, advisors, setup, preferences, recent_keys)
            self._append_candidate(candidates, seen_keys, candidate)

        for candidate in self._candidates_from_decisions(household_decisions, household_id, created, quality, context, setup, advisors, preferences, recent_keys):
            self._append_candidate(candidates, seen_keys, candidate)

        if not household_decisions:
            for candidate in self._candidates_from_presentations(recommendation_presentations, household_id, created, quality, context, setup, advisors, preferences, recent_keys):
                self._append_candidate(candidates, seen_keys, candidate)

        if not household_decisions and not recommendation_presentations and allow_legacy_advisor_notifications:
            for candidate in self._candidates_from_advisors(advisors, household_id, created, quality, context, setup, preferences, recent_keys):
                self._append_candidate(candidates, seen_keys, candidate)

        setup_candidate = self._candidate_from_setup(setup, household_id, created, quality, preferences, recent_keys)
        self._append_candidate(candidates, seen_keys, setup_candidate)

        candidates.sort(key=self._candidate_sort_key)
        return NotificationReadiness(
            household_id=household_id,
            generated_at=created.isoformat(timespec="seconds"),
            candidates=candidates,
        )

    def _candidate_from_state_change(
        self,
        change: Any,
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        context: Optional[Any],
        advisors: Optional[Any],
        setup: Optional[Any],
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> Optional[NotificationCandidate]:
        change_type = str(self._get(change, "change_type") or "")
        event_key = str(self._get(change, "event_key") or self._get(change, "change_id") or change_type)
        title = str(self._get(change, "title") or "Household state changed")
        summary = str(self._get(change, "summary") or "")
        detected_at = self._get(change, "detected_at")
        evidence = [
            NotificationEvidence(
                source="StateChangeEngine",
                summary=summary or title,
                quality=quality,
                observed_at=str(detected_at) if detected_at else None,
            )
        ]

        mapping = {
            StateChangeType.ENTERED_PEAK.value: (NotificationType.PEAK_PERIOD_STARTED, NotificationPriority.MEDIUM, "Peak period started"),
            StateChangeType.ENTERED_LOW_COST_WINDOW.value: (NotificationType.LOW_COST_WINDOW_STARTED, NotificationPriority.MEDIUM, "Low-cost charging window started"),
            StateChangeType.COLLECTOR_WENT_STALE.value: (NotificationType.COLLECTOR_STALE, NotificationPriority.CRITICAL, "Collector data may be stale"),
            StateChangeType.COLLECTOR_RECOVERED.value: (NotificationType.COLLECTOR_RECOVERED, NotificationPriority.LOW, "Collector recovered"),
            StateChangeType.PROVIDER_DEGRADED.value: (NotificationType.PROVIDER_ERROR, NotificationPriority.HIGH, "Provider data degraded"),
            StateChangeType.EVIDENCE_QUALITY_DEGRADED.value: (NotificationType.EVIDENCE_DEGRADED, NotificationPriority.HIGH, "Evidence quality degraded"),
        }
        if change_type not in mapping:
            return None
        notification_type, priority, fallback_title = mapping[change_type]
        candidate = self._candidate(
            household_id,
            notification_type,
            title if title != "Household state changed" else fallback_title,
            summary or fallback_title,
            priority,
            created,
            evidence,
            event_key,
        )
        reason = self._suppression_reason(candidate, quality, context, setup, advisors, preferences, recent_keys)
        return self._with_suppression(candidate, reason)

    def _candidates_from_decisions(
        self,
        household_decisions: Optional[list[Any]],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
        advisors: Optional[Any],
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> list[NotificationCandidate]:
        candidates = []
        for decision in household_decisions or []:
            decision_type = str(self._get(decision, "decision_type") or self._get(decision, "type") or "")
            status = str(self._get(decision, "status") or "active")
            notification_type = self._notification_type_for_decision_type(decision_type)
            if status != "active" or not notification_type:
                continue
            default_priority = self._default_priority_for_decision_type(decision_type)
            priority = self._priority_from_decision(decision, default_priority)
            reason = self._decision_reason(decision)
            evidence = [
                NotificationEvidence(
                    source="DecisionEngine",
                    summary=reason,
                    quality=self._decision_quality(decision) or quality,
                    observed_at=self._get(decision, "created_at"),
                )
            ]
            candidate = self._candidate(
                household_id,
                notification_type,
                decision_type.replace("_", " ").title(),
                reason,
                priority,
                created,
                evidence,
                f"decision:{self._get(decision, 'decision_id') or decision_type}",
                expires_at=self._get(decision, "valid_until"),
            )
            suppression = self._suppression_reason(candidate, quality, context, setup, advisors, preferences, recent_keys)
            candidates.append(self._with_suppression(candidate, suppression))
        return candidates

    def _notification_type_for_decision_type(self, decision_type: str) -> Optional[NotificationType]:
        return {
            "CHARGE_NOW": NotificationType.CHARGE_NOW,
            "WAIT_FOR_CHEAPER_WINDOW": NotificationType.WAIT_FOR_CHEAPER_WINDOW,
            "WAIT_FOR_SOLAR": NotificationType.SOLAR_OPPORTUNITY,
            "REVIEW_PROVIDER": NotificationType.PROVIDER_ERROR,
            "REVIEW_SETUP": NotificationType.SETUP_INCOMPLETE,
            "REVIEW_BILLING": NotificationType.BILL_TRACKING_HIGH,
            "MONITOR_USAGE": NotificationType.PEAK_PERIOD_STARTED,
            "INVESTIGATE_ANOMALY": NotificationType.EVIDENCE_DEGRADED,
            "DATA_INSUFFICIENT": NotificationType.EVIDENCE_DEGRADED,
        }.get(decision_type)

    def _default_priority_for_decision_type(self, decision_type: str) -> NotificationPriority:
        return {
            "CHARGE_NOW": NotificationPriority.HIGH,
            "WAIT_FOR_CHEAPER_WINDOW": NotificationPriority.MEDIUM,
            "WAIT_FOR_SOLAR": NotificationPriority.MEDIUM,
            "REVIEW_PROVIDER": NotificationPriority.HIGH,
            "REVIEW_SETUP": NotificationPriority.LOW,
            "REVIEW_BILLING": NotificationPriority.HIGH,
            "MONITOR_USAGE": NotificationPriority.MEDIUM,
            "INVESTIGATE_ANOMALY": NotificationPriority.MEDIUM,
            "DATA_INSUFFICIENT": NotificationPriority.HIGH,
        }.get(decision_type, NotificationPriority.LOW)

    def _candidates_from_presentations(
        self,
        recommendation_presentations: Optional[list[Any]],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
        advisors: Optional[Any],
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> list[NotificationCandidate]:
        candidates = []
        for presentation in recommendation_presentations or []:
            decision = self._get(presentation, "decision") or {}
            decision_type = str(self._get(decision, "decision_type") or self._get(decision, "type") or "")
            decision_id = str(self._get(presentation, "decision_id") or self._get(decision, "decision_id") or decision_type)
            if not decision_type or str(self._get(presentation, "status") or "active") != "active":
                continue
            notification_type = self._notification_type_for_decision_type(decision_type)
            if not notification_type:
                continue
            priority = self._priority_from_presentation(presentation)
            message = str(
                self._get(presentation, "recommended_action")
                or self._get(presentation, "rationale")
                or self._get(presentation, "title")
                or decision_type
            )
            evidence = [
                NotificationEvidence(
                    source="RecommendationPresentation",
                    summary=str(self._get(presentation, "rationale") or message),
                    quality=self._presentation_quality(presentation) or quality,
                    observed_at=self._get(decision, "created_at"),
                )
            ]
            candidate = self._candidate(
                household_id,
                notification_type,
                str(self._get(presentation, "title") or decision_type.replace("_", " ").title()),
                message,
                priority,
                created,
                evidence,
                f"decision:{decision_id}",
                expires_at=self._get(presentation, "valid_until") or self._get(decision, "valid_until"),
            )
            suppression = self._suppression_reason(candidate, quality, context, setup, advisors, preferences, recent_keys)
            candidates.append(self._with_suppression(candidate, suppression))
        return candidates

    def _candidates_from_advisors(
        self,
        advisors: Optional[Any],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> list[NotificationCandidate]:
        candidates = []
        for advisor in self._active_advisors(advisors):
            advisor_id = str(self._get(advisor, "advisor_id") or self._get(advisor, "id") or self._get(advisor, "advisor_type") or "advisor")
            status = str(self._get(advisor, "status") or "").upper()
            advisor_type = str(self._get(advisor, "advisor_type") or "").upper()
            headline = str(self._get(advisor, "headline") or self._get(advisor, "title") or advisor_id)
            message = str(self._get(advisor, "recommendation") or self._get(advisor, "summary") or headline)
            confidence = str(self._get(advisor, "confidence") or "")
            evidence = [
                NotificationEvidence(
                    source=f"Advisor:{advisor_type or advisor_id}",
                    summary=f"{headline} ({confidence or 'unknown'} confidence)",
                    quality=self._advisor_quality(advisor) or quality,
                    observed_at=self._get(advisor, "next_review_time"),
                )
            ]
            notification_type = None
            priority = NotificationPriority.MEDIUM
            if advisor_type == "EV_CHARGING" and status == "CHARGE_NOW":
                notification_type = NotificationType.CHARGE_NOW
                priority = NotificationPriority.HIGH
            elif advisor_type == "EV_CHARGING" and status == "WAIT":
                notification_type = NotificationType.WAIT_FOR_CHEAPER_WINDOW
            elif advisor_type == "COST" and status in {"HIGH", "WATCH"}:
                notification_type = NotificationType.BILL_TRACKING_HIGH
                priority = NotificationPriority.HIGH if status == "HIGH" else NotificationPriority.MEDIUM
            elif advisor_type == "SOLAR" and status in {"OPPORTUNITY", "MISSED_OPPORTUNITY"}:
                notification_type = NotificationType.SOLAR_OPPORTUNITY
            elif status in {"IMPROVING", "EXCELLENT"}:
                notification_type = NotificationType.GOAL_PROGRESS
                priority = NotificationPriority.LOW
            if not notification_type:
                continue
            candidate = self._candidate(
                household_id,
                notification_type,
                headline,
                message,
                priority,
                created,
                evidence,
                f"advisor:{advisor_id}:{status}",
                expires_at=self._get(advisor, "next_review_time"),
            )
            reason = self._suppression_reason(candidate, quality, context, setup, advisors, preferences, recent_keys)
            candidates.append(self._with_suppression(candidate, reason))
        return candidates

    def _candidate_from_setup(
        self,
        setup: Optional[Any],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> Optional[NotificationCandidate]:
        progress = self._get(setup, "progress")
        completion_pct = self._get(progress, "completion_pct")
        remaining = list(self._get(progress, "remaining_tasks") or [])
        if completion_pct is None or int(completion_pct) >= 100 or not remaining:
            return None
        first_task = remaining[0]
        title = "Setup is incomplete"
        message = f"{self._get(first_task, 'title') or 'A setup task'} is still needed before some advice is fully reliable."
        candidate = self._candidate(
            household_id,
            NotificationType.SETUP_INCOMPLETE,
            title,
            message,
            NotificationPriority.LOW,
            created,
            [NotificationEvidence("SetupEngine", f"Setup completion is {int(completion_pct)}%.", quality)],
            "setup:incomplete",
            ttl=timedelta(hours=12),
        )
        reason = self._preference_suppression(candidate, preferences, recent_keys)
        return self._with_suppression(candidate, reason)

    def _suppression_reason(
        self,
        candidate: NotificationCandidate,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
        advisors: Optional[Any],
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> NotificationSuppressionReason:
        now = self._parse_datetime(candidate.created_at)
        valid_until = self._parse_datetime(candidate.valid_until)
        if now.tzinfo and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=now.tzinfo)
        if valid_until <= now:
            return NotificationSuppressionReason.EXPIRED
        preference_reason = self._preference_suppression(candidate, preferences, recent_keys)
        if preference_reason != NotificationSuppressionReason.NONE:
            return preference_reason
        if candidate.source_event_key in recent_keys:
            return NotificationSuppressionReason.RECENT_DUPLICATE
        if quality in {EvidenceQuality.LOW, EvidenceQuality.UNKNOWN} and candidate.notification_type not in self.health_notification_types:
            return NotificationSuppressionReason.LOW_EVIDENCE_QUALITY
        if candidate.notification_type == NotificationType.SOLAR_OPPORTUNITY and str(self._context_value(context, "solar")) == "SOLAR_IMPOSSIBLE":
            return NotificationSuppressionReason.CONTEXT_IRRELEVANT
        if candidate.notification_type == NotificationType.LOW_COST_WINDOW_STARTED and not self._has_ev_capability(advisors):
            return NotificationSuppressionReason.NOT_ACTIONABLE
        if candidate.notification_type == NotificationType.PEAK_PERIOD_STARTED and str(self._context_value(context, "tariff")) not in {"HIGH_COST", "HIGHEST_COST"}:
            return NotificationSuppressionReason.CONTEXT_IRRELEVANT
        if candidate.notification_type == NotificationType.COLLECTOR_STALE and not self._collector_stale_beyond_threshold(candidate):
            return NotificationSuppressionReason.STALE_RECOMMENDATION
        if candidate.notification_type not in self.health_notification_types and self._setup_reliability_low(setup):
            return NotificationSuppressionReason.SETUP_INCOMPLETE
        return NotificationSuppressionReason.NONE

    def _candidate(
        self,
        household_id: str,
        notification_type: NotificationType,
        title: str,
        message: str,
        priority: NotificationPriority,
        created: datetime,
        evidence: list[NotificationEvidence],
        source_event_key: str,
        *,
        recommended_channel: NotificationChannel = NotificationChannel.LOCAL_UI,
        ttl: timedelta = timedelta(hours=1),
        expires_at: Optional[str] = None,
    ) -> NotificationCandidate:
        valid_until = expires_at or (created + ttl).isoformat(timespec="seconds")
        candidate_id = hashlib.sha256(f"{household_id}|{notification_type.value}|{source_event_key}|{valid_until}".encode("utf-8")).hexdigest()[:32]
        return NotificationCandidate(
            candidate_id=candidate_id,
            household_id=household_id,
            notification_type=notification_type,
            title=title,
            message=message,
            priority=priority,
            created_at=created.isoformat(timespec="seconds"),
            valid_until=valid_until,
            evidence=evidence,
            recommended_channel=recommended_channel,
            source_event_key=source_event_key,
        )

    def _with_suppression(
        self,
        candidate: NotificationCandidate,
        reason: NotificationSuppressionReason,
    ) -> NotificationCandidate:
        if reason == NotificationSuppressionReason.NONE:
            return candidate
        return NotificationCandidate(
            candidate_id=candidate.candidate_id,
            household_id=candidate.household_id,
            notification_type=candidate.notification_type,
            title=candidate.title,
            message=candidate.message,
            priority=candidate.priority,
            created_at=candidate.created_at,
            valid_until=candidate.valid_until,
            evidence=candidate.evidence,
            recommended_channel=candidate.recommended_channel,
            ready=False,
            suppression_reason=reason,
            source_event_key=candidate.source_event_key,
        )

    def _append_candidate(
        self,
        candidates: list[NotificationCandidate],
        seen_keys: set[str],
        candidate: Optional[NotificationCandidate],
    ) -> None:
        if not candidate:
            return
        key = candidate.source_event_key or candidate.candidate_id
        if key in seen_keys:
            candidates.append(self._with_suppression(candidate, NotificationSuppressionReason.RECENT_DUPLICATE))
            return
        seen_keys.add(key)
        candidates.append(candidate)

    def _evidence_quality(
        self,
        evidence_quality: Optional[Any],
        context: Optional[Any],
        advisors: Optional[Any],
        household_state_snapshot: Optional[Any],
    ) -> EvidenceQuality:
        if isinstance(evidence_quality, EvidenceQuality):
            return evidence_quality
        if evidence_quality:
            try:
                return EvidenceQuality(str(evidence_quality))
            except ValueError:
                pass
        context_confidence = str(self._context_value(context, "confidence") or "")
        if context_confidence in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
            return EvidenceQuality(context_confidence)
        snapshot_confidence = str(self._get(self._get(household_state_snapshot, "evidence_state") or self._get(household_state_snapshot, "evidence"), "confidence") or "")
        if snapshot_confidence in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
            return EvidenceQuality(snapshot_confidence)
        qualities = [self._advisor_quality(advisor) for advisor in self._active_advisors(advisors)]
        qualities = [quality for quality in qualities if quality]
        if EvidenceQuality.LOW in qualities:
            return EvidenceQuality.LOW
        if EvidenceQuality.UNKNOWN in qualities:
            return EvidenceQuality.UNKNOWN
        if EvidenceQuality.MEDIUM in qualities:
            return EvidenceQuality.MEDIUM
        if EvidenceQuality.HIGH in qualities:
            return EvidenceQuality.HIGH
        return EvidenceQuality.UNKNOWN

    def _advisor_quality(self, advisor: Any) -> Optional[EvidenceQuality]:
        summary = self._get(advisor, "evidence_summary")
        quality = self._get(summary, "evidence_quality") or self._get(advisor, "evidence_quality")
        if isinstance(quality, EvidenceQuality):
            return quality
        if quality:
            try:
                return EvidenceQuality(str(quality))
            except ValueError:
                return None
        return None

    def _decision_quality(self, decision: Any) -> Optional[EvidenceQuality]:
        confidence = str(self._get(decision, "confidence") or "")
        if confidence in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
            return EvidenceQuality(confidence)
        return None

    def _priority_from_decision(
        self,
        decision: Any,
        default: NotificationPriority,
    ) -> NotificationPriority:
        priority = str(self._get(decision, "priority") or "").lower()
        return {
            "low": NotificationPriority.LOW,
            "medium": NotificationPriority.MEDIUM,
            "high": NotificationPriority.HIGH,
            "critical": NotificationPriority.CRITICAL,
        }.get(priority, default)

    def _priority_from_presentation(self, presentation: Any) -> NotificationPriority:
        urgency = self._get(presentation, "urgency")
        value = str(self._get(urgency, "value") or self._get(presentation, "priority") or "").lower()
        return {
            "low": NotificationPriority.LOW,
            "medium": NotificationPriority.MEDIUM,
            "high": NotificationPriority.HIGH,
            "critical": NotificationPriority.CRITICAL,
        }.get(value, NotificationPriority.LOW)

    def _presentation_quality(self, presentation: Any) -> Optional[EvidenceQuality]:
        confidence = str(self._get(presentation, "confidence") or "").upper().replace(" ", "_")
        if confidence == "VERY_HIGH":
            confidence = "HIGH"
        if confidence in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
            return EvidenceQuality(confidence)
        return None

    def _decision_reason(self, decision: Any) -> str:
        reasons = list(self._get(decision, "reasons") or [])
        if reasons:
            return str(self._get(reasons[0], "summary") or self._get(reasons[0], "code") or "Decision Engine output.")
        outcome = self._get(decision, "outcome")
        if outcome:
            return str(self._get(outcome, "expected_effect") or self._get(outcome, "status") or "Decision Engine output.")
        return "Decision Engine output."

    def _active_advisors(self, advisors: Optional[Any]) -> list[Any]:
        if advisors is None:
            return []
        if hasattr(advisors, "active") and callable(advisors.active):
            return list(advisors.active())
        if isinstance(advisors, dict):
            return list(advisors.get("advisors", []))
        return list(advisors or [])

    def _household_id(self, household_state_snapshot: Any, context: Any, advisors: Any, setup: Any) -> str:
        household_id = self._get(household_state_snapshot, "household_id") or self._get(setup, "household_id")
        if household_id:
            return str(household_id)
        household = self._get(advisors, "household")
        return str(self._get(household, "household_id") or "default_household")

    def _context_value(self, context: Any, key: str) -> Any:
        value = self._get(context, key)
        return getattr(value, "value", value)

    def _get(self, source: Any, key: str, default: Any = None) -> Any:
        if source is None:
            return default
        if isinstance(source, dict):
            return source.get(key, default)
        return getattr(source, key, default)

    def _has_ev_capability(self, advisors: Optional[Any]) -> bool:
        for advisor in self._active_advisors(advisors):
            if str(self._get(advisor, "advisor_type") or "").upper() == "EV_CHARGING":
                return True
        capability_summary = self._get(advisors, "capability_summary")
        if capability_summary and hasattr(capability_summary, "get"):
            ev = capability_summary.get("ev")
            return bool(self._get(ev, "usable"))
        return False

    def _setup_reliability_low(self, setup: Optional[Any]) -> bool:
        progress = self._get(setup, "progress")
        completion_pct = self._get(progress, "completion_pct")
        if completion_pct is None:
            return False
        return int(completion_pct) < 50

    def _collector_stale_beyond_threshold(self, candidate: NotificationCandidate) -> bool:
        return bool(candidate.evidence)

    def _preference_suppression(
        self,
        candidate: NotificationCandidate,
        preferences: NotificationPreferences,
        recent_keys: set[str],
    ) -> NotificationSuppressionReason:
        if not preferences.enabled:
            return NotificationSuppressionReason.NOTIFICATIONS_DISABLED
        rule = preferences.rule_for(candidate.notification_type)
        if rule and not rule.enabled:
            return NotificationSuppressionReason.TYPE_DISABLED
        if self._priority_rank(candidate.priority) < self._priority_rank(preferences.priority_threshold):
            return NotificationSuppressionReason.BELOW_PRIORITY_THRESHOLD
        if self._quiet_hours_active(preferences.quiet_hours, candidate.created_at) and candidate.priority != NotificationPriority.CRITICAL:
            return NotificationSuppressionReason.QUIET_HOURS
        if candidate.source_event_key in recent_keys:
            return NotificationSuppressionReason.RECENT_DUPLICATE
        return NotificationSuppressionReason.NONE

    def _recent_history_keys(
        self,
        history: list[Any],
        preferences: NotificationPreferences,
        now: datetime,
    ) -> set[str]:
        keys = set()
        for item in history:
            state = str(self._get(item, "delivery_state") or "").lower()
            if state != NotificationDeliveryState.SENT.value:
                continue
            sent_at = self._get(item, "sent_at") or self._get(item, "created_at")
            if not sent_at:
                continue
            sent = self._parse_datetime(str(sent_at))
            if now.tzinfo and sent.tzinfo is None:
                sent = sent.replace(tzinfo=now.tzinfo)
            notification_type = self._notification_type(self._get(item, "notification_type"))
            cooldown = preferences.cooldown_minutes
            if notification_type:
                rule = preferences.rule_for(notification_type)
                if rule and rule.cooldown_minutes is not None:
                    cooldown = rule.cooldown_minutes
            if now - sent <= timedelta(minutes=max(int(cooldown or 0), 0)):
                key = self._get(item, "stable_key") or self._get(item, "source_event_key") or self._get(item, "candidate_id")
                if key:
                    keys.add(str(key))
        return keys

    def _quiet_hours_active(self, quiet_hours: NotificationQuietHours, created_at: str) -> bool:
        if not quiet_hours.enabled:
            return False
        current = self._parse_datetime(created_at).time()
        start = self._parse_time(quiet_hours.start)
        end = self._parse_time(quiet_hours.end)
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def _priority_rank(self, priority: NotificationPriority) -> int:
        return {
            NotificationPriority.LOW: 0,
            NotificationPriority.MEDIUM: 1,
            NotificationPriority.HIGH: 2,
            NotificationPriority.CRITICAL: 3,
        }[priority]

    def _notification_type(self, value: Any) -> Optional[NotificationType]:
        if isinstance(value, NotificationType):
            return value
        if value:
            try:
                return NotificationType(str(value))
            except ValueError:
                return None
        return None

    def _candidate_sort_key(self, candidate: NotificationCandidate) -> tuple[int, str, str]:
        priority_rank = {
            NotificationPriority.CRITICAL: 0,
            NotificationPriority.HIGH: 1,
            NotificationPriority.MEDIUM: 2,
            NotificationPriority.LOW: 3,
        }
        ready_rank = 0 if candidate.ready else 1
        return (ready_rank, priority_rank[candidate.priority], candidate.title)

    def _parse_datetime(self, value: str) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _parse_time(self, value: str):
        hour, minute = str(value or "00:00").split(":")[:2]
        return datetime.strptime(f"{int(hour):02d}:{int(minute):02d}", "%H:%M").time()
