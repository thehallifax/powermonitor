import unittest

from engine.evidence_quality import (
    AdvisorEvidenceSummary,
    EvidenceQuality,
    EvidenceRequirement,
    EvidenceSource,
    cap_confidence_for_evidence,
    evidence_quality_from_requirements,
)


class EvidenceQualityTests(unittest.TestCase):
    def test_caps_confidence_to_available_evidence_quality(self):
        self.assertEqual(cap_confidence_for_evidence("Very High", EvidenceQuality.HIGH), "High")
        self.assertEqual(cap_confidence_for_evidence("High", EvidenceQuality.MEDIUM), "Medium")
        self.assertEqual(cap_confidence_for_evidence("High", EvidenceQuality.LOW), "Low")
        self.assertEqual(cap_confidence_for_evidence("Medium", EvidenceQuality.UNKNOWN), "Low")

    def test_missing_required_evidence_makes_quality_low(self):
        quality = evidence_quality_from_requirements(
            [
                EvidenceRequirement(
                    "Live solar",
                    EvidenceSource.LIVE_TELEMETRY,
                    False,
                    quality=EvidenceQuality.LOW,
                )
            ]
        )

        self.assertEqual(quality, EvidenceQuality.LOW)

    def test_summary_serializes_available_and_missing_evidence(self):
        summary = AdvisorEvidenceSummary(
            required_evidence=[
                EvidenceRequirement(
                    "Tariff",
                    EvidenceSource.USER_CONFIGURATION,
                    True,
                    quality=EvidenceQuality.HIGH,
                )
            ],
            optional_evidence=[
                EvidenceRequirement(
                    "Live solar",
                    EvidenceSource.MISSING,
                    False,
                    required=False,
                    quality=EvidenceQuality.LOW,
                )
            ],
            evidence_quality=EvidenceQuality.LOW,
            confidence="Low",
            reason="Live solar unavailable.",
        )

        data = summary.as_dict()

        self.assertEqual(data["evidence_quality"], "LOW")
        self.assertEqual(data["available_evidence"][0]["source"], "USER_CONFIGURATION")
        self.assertEqual(data["missing_evidence"][0]["name"], "Live solar")


if __name__ == "__main__":
    unittest.main()
