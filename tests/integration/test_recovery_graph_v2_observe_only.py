from __future__ import annotations

import unittest
from datetime import UTC, datetime

from schemas.incident import Incident
from schemas.observations import ObservationBundle
from workflows.recovery_graph_v2 import run_recovery_graph_v2_observe_only


class _StaticObserver:
    def collect(self, *, incident: Incident, namespace_hint: str | None = None) -> ObservationBundle:
        del namespace_hint
        return ObservationBundle(
            incident_id=incident.incident_id,
            incident_class_hint="crashloop",
            namespace_hint="default",
            source=incident.source,
            alert_context={"labels": {"namespace": "default"}},
            kubernetes={"pods": {"status": "succeeded"}},
            prometheus={"ready": {"status": "succeeded", "value": 1.0}},
            collected_at="2026-03-29T20:00:00+00:00",
        )


class RecoveryGraphV2ObserveOnlyIntegrationTest(unittest.TestCase):
    def test_graph_runs_observe_reason_then_handoff(self) -> None:
        incident = Incident(
            incident_id="incident-123",
            incident_class="crashloop",
            detected_at=datetime(2026, 3, 29, 20, 0, tzinfo=UTC),
            source="prometheus",
            raw_context={"alert": {"labels": {"namespace": "default"}}},
        )

        result = run_recovery_graph_v2_observe_only(incident, observer=_StaticObserver())

        self.assertEqual(result["observation_bundle"].incident_id, "incident-123")
        self.assertEqual(result["reasoner_state"]["status"], "succeeded")
        self.assertEqual(result["critic_state"]["status"], "succeeded")
        self.assertEqual(result["synthesizer_state"]["status"], "succeeded")
        self.assertEqual(result["handoff_summary"]["status"], "handoff_to_v1")
        self.assertEqual(result["handoff_summary"]["incident_class_hint"], "crashloop")
        self.assertGreaterEqual(result["handoff_summary"]["intent_count"], 1)
        self.assertGreaterEqual(result["handoff_summary"]["critic_candidate_count"], 1)
        self.assertGreaterEqual(result["handoff_summary"]["synthesized_plan_count"], 1)


if __name__ == "__main__":
    unittest.main()
