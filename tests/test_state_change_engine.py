import unittest

from engine.state_change_engine import StateChangeEngine, StateChangeType


def snapshot(
    snapshot_id,
    *,
    tariff_bucket="off_peak",
    solar_producing=False,
    import_export_state="importing",
    collector_healthy=True,
    billing_start="2026-06-01",
    billing_day=10,
    supply_days=30,
    evidence_confidence="HIGH",
    providers=None,
):
    return {
        "snapshot_id": snapshot_id,
        "household_id": "default_household",
        "captured_at": f"2026-06-27T12:0{snapshot_id[-1]}:00+08:00",
        "tariff_state": {"active_bucket": tariff_bucket},
        "solar_state": {"producing": solar_producing},
        "power_state": {"import_export_state": import_export_state},
        "collector_state": {"healthy": collector_healthy},
        "billing_state": {
            "period_start": billing_start,
            "day_of_period": billing_day,
            "supply_days": supply_days,
        },
        "evidence_state": {"confidence": evidence_confidence},
        "provider_provenance": {"trusted_provider_data": ["solaredge"] if providers is None else providers},
    }


class StateChangeEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = StateChangeEngine()

    def change_types(self, previous, current):
        return {change.change_type for change in self.engine.detect(previous, current).changes}

    def test_detects_tariff_transition_and_peak_entry(self):
        changes = self.change_types(
            snapshot("snap1", tariff_bucket="off_peak"),
            snapshot("snap2", tariff_bucket="peak"),
        )

        self.assertIn(StateChangeType.TARIFF_WINDOW_CHANGED, changes)
        self.assertIn(StateChangeType.ENTERED_PEAK, changes)

    def test_detects_peak_exit_and_low_cost_entry(self):
        changes = self.change_types(
            snapshot("snap1", tariff_bucket="peak"),
            snapshot("snap2", tariff_bucket="super_off_peak"),
        )

        self.assertIn(StateChangeType.EXITED_PEAK, changes)
        self.assertIn(StateChangeType.ENTERED_LOW_COST_WINDOW, changes)

    def test_detects_solar_and_power_direction_changes(self):
        changes = self.change_types(
            snapshot("snap1", solar_producing=False, import_export_state="importing"),
            snapshot("snap2", solar_producing=True, import_export_state="exporting"),
        )

        self.assertIn(StateChangeType.SOLAR_STARTED, changes)
        self.assertIn(StateChangeType.IMPORT_EXPORT_DIRECTION_CHANGED, changes)

    def test_detects_collector_stale_and_recovery(self):
        stale = self.change_types(
            snapshot("snap1", collector_healthy=True),
            snapshot("snap2", collector_healthy=False),
        )
        recovered = self.change_types(
            snapshot("snap1", collector_healthy=False),
            snapshot("snap2", collector_healthy=True),
        )

        self.assertIn(StateChangeType.COLLECTOR_WENT_STALE, stale)
        self.assertIn(StateChangeType.COLLECTOR_RECOVERED, recovered)

    def test_detects_evidence_degradation_and_recovery(self):
        degraded = self.change_types(
            snapshot("snap1", evidence_confidence="HIGH"),
            snapshot("snap2", evidence_confidence="LOW"),
        )
        recovered = self.change_types(
            snapshot("snap1", evidence_confidence="LOW"),
            snapshot("snap2", evidence_confidence="MEDIUM"),
        )

        self.assertIn(StateChangeType.EVIDENCE_QUALITY_DEGRADED, degraded)
        self.assertIn(StateChangeType.EVIDENCE_QUALITY_RECOVERED, recovered)

    def test_detects_billing_period_events_and_provider_changes(self):
        changes = self.change_types(
            snapshot("snap1", billing_start="2026-06-01", billing_day=25, providers=["solaredge"]),
            snapshot("snap2", billing_start="2026-07-01", billing_day=28, providers=[]),
        )

        self.assertIn(StateChangeType.BILLING_PERIOD_STARTED, changes)
        self.assertIn(StateChangeType.BILLING_PERIOD_NEAR_END, changes)
        self.assertIn(StateChangeType.PROVIDER_DEGRADED, changes)

    def test_unchanged_snapshots_do_not_emit_noisy_events(self):
        previous = snapshot("snap1")
        current = snapshot("snap2")

        result = self.engine.detect(previous, current)

        self.assertEqual(result.changes, [])


if __name__ == "__main__":
    unittest.main()
