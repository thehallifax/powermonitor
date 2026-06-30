#!/usr/bin/env python3
"""Shared evidence quality contracts for PowerMonitor advisors."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class EvidenceQuality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceSource(str, Enum):
    LIVE_TELEMETRY = "LIVE_TELEMETRY"
    SMART_METER = "SMART_METER"
    PROVIDER_API = "PROVIDER_API"
    IMPORTED_HISTORY = "IMPORTED_HISTORY"
    DAILY_SUMMARY = "DAILY_SUMMARY"
    ESTIMATED = "ESTIMATED"
    USER_CONFIGURATION = "USER_CONFIGURATION"
    MISSING = "MISSING"


@dataclass(frozen=True)
class EvidenceRequirement:
    """One evidence dependency an advisor considered."""

    name: str
    source: EvidenceSource
    available: bool
    required: bool = True
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        data["quality"] = self.quality.value
        return data


@dataclass(frozen=True)
class AdvisorEvidenceSummary:
    """Evidence-quality summary attached to a shared advisor output."""

    required_evidence: list[EvidenceRequirement] = field(default_factory=list)
    optional_evidence: list[EvidenceRequirement] = field(default_factory=list)
    evidence_quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    confidence: str = "Low"
    reason: str = "Confidence has not been assessed."
    confidence_reason: Optional[str] = None

    @property
    def available_evidence(self) -> list[EvidenceRequirement]:
        return [
            item for item in self.required_evidence + self.optional_evidence
            if item.available
        ]

    @property
    def missing_evidence(self) -> list[EvidenceRequirement]:
        return [
            item for item in self.required_evidence + self.optional_evidence
            if not item.available
        ]

    @property
    def unavailable_evidence(self) -> list[EvidenceRequirement]:
        return self.missing_evidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_evidence": [item.as_dict() for item in self.required_evidence],
            "optional_evidence": [item.as_dict() for item in self.optional_evidence],
            "available_evidence": [item.as_dict() for item in self.available_evidence],
            "missing_evidence": [item.as_dict() for item in self.missing_evidence],
            "unavailable_evidence": [item.as_dict() for item in self.unavailable_evidence],
            "evidence_quality": self.evidence_quality.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "confidence_reason": self.confidence_reason or self.reason,
        }


_CONFIDENCE_RANK = {"Low": 0, "Medium": 1, "High": 2, "Very High": 3}
_CONFIDENCE_BY_RANK = {rank: label for label, rank in _CONFIDENCE_RANK.items()}
_MAX_CONFIDENCE_BY_QUALITY = {
    EvidenceQuality.HIGH: "High",
    EvidenceQuality.MEDIUM: "Medium",
    EvidenceQuality.LOW: "Low",
    EvidenceQuality.UNKNOWN: "Low",
}


def cap_confidence_for_evidence(confidence: str, quality: EvidenceQuality) -> str:
    """Prevent weak evidence from producing stronger advisor confidence."""

    requested = _CONFIDENCE_RANK.get(confidence, 0)
    maximum = _CONFIDENCE_RANK[_MAX_CONFIDENCE_BY_QUALITY[quality]]
    return _CONFIDENCE_BY_RANK[min(requested, maximum)]


def evidence_quality_from_requirements(
    required: list[EvidenceRequirement],
    optional: Optional[list[EvidenceRequirement]] = None,
) -> EvidenceQuality:
    """Conservatively derive overall quality from declared requirements."""

    optional = optional or []
    if not required:
        return EvidenceQuality.UNKNOWN
    if any(item.required and not item.available for item in required):
        return EvidenceQuality.LOW
    qualities = [item.quality for item in required + optional if item.available]
    if not qualities:
        return EvidenceQuality.UNKNOWN
    if EvidenceQuality.LOW in qualities:
        return EvidenceQuality.LOW
    if EvidenceQuality.UNKNOWN in qualities:
        return EvidenceQuality.UNKNOWN
    if EvidenceQuality.MEDIUM in qualities:
        return EvidenceQuality.MEDIUM
    return EvidenceQuality.HIGH
