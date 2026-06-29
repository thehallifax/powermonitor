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
    LOW_EVIDENCE_QUALITY = "low_evidence_quality"
    RECENT_DUPLICATE = "recent_duplicate"
    STALE_RECOMMENDATION = "stale_recommendation"
    CONTEXT_IRRELEVANT = "context_irrelevant"
    SETUP_INCOMPLETE = "setup_incomplete"
    NOT_ACTIONABLE = "not_actionable"
    EXPIRED = "expired"


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
        advisors: Optional[Any] = None,
        setup: Optional[Any] = None,
        evidence_quality: Optional[Any] = None,
        recently_notified_event_keys: Optional[set[str]] = None,
        now: Optional[datetime] = None,
    ) -> NotificationReadiness:
        created = now or datetime.now().astimezone()
        household_id = self._household_id(household_state_snapshot, context, advisors, setup)
        quality = self._evidence_quality(evidence_quality, context, advisors, household_state_snapshot)
        recent_keys = recently_notified_event_keys or set()
        candidates: list[NotificationCandidate] = []
        seen_keys: set[str] = set()

        for change in state_changes or []:
            candidate = self._candidate_from_state_change(change, household_id, created, quality, context, advisors, setup, recent_keys)
            self._append_candidate(candidates, seen_keys, candidate)

        for candidate in self._candidates_from_advisors(advisors, household_id, created, quality, context, setup, recent_keys):
            self._append_candidate(candidates, seen_keys, candidate)

        setup_candidate = self._candidate_from_setup(setup, household_id, created, quality, recent_keys)
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
        reason = self._suppression_reason(candidate, quality, context, setup, advisors, recent_keys)
        return self._with_suppression(candidate, reason)

    def _candidates_from_advisors(
        self,
        advisors: Optional[Any],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
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
            reason = self._suppression_reason(candidate, quality, context, setup, advisors, recent_keys)
            candidates.append(self._with_suppression(candidate, reason))
        return candidates

    def _candidate_from_setup(
        self,
        setup: Optional[Any],
        household_id: str,
        created: datetime,
        quality: EvidenceQuality,
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
        reason = NotificationSuppressionReason.RECENT_DUPLICATE if candidate.source_event_key in recent_keys else NotificationSuppressionReason.NONE
        return self._with_suppression(candidate, reason)

    def _suppression_reason(
        self,
        candidate: NotificationCandidate,
        quality: EvidenceQuality,
        context: Optional[Any],
        setup: Optional[Any],
        advisors: Optional[Any],
        recent_keys: set[str],
    ) -> NotificationSuppressionReason:
        now = self._parse_datetime(candidate.created_at)
        valid_until = self._parse_datetime(candidate.valid_until)
        if now.tzinfo and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=now.tzinfo)
        if valid_until <= now:
            return NotificationSuppressionReason.EXPIRED
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
