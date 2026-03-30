from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agents.reasoner import run_reasoner_pipeline
from schemas.incident import Incident
from schemas.observations import ObservationBundle
from services.reasoner_llm import ReasonerLLMResult


def _incident(incident_class: str) -> Incident:
    return Incident(
        incident_id=f"{incident_class}-123",
        incident_class=incident_class,
        detected_at=datetime(2026, 3, 29, 20, 0, tzinfo=UTC),
        source="prometheus",
        raw_context={
            "alert": {
                "labels": {
                    "alertname": "HeraldAlert",
                    "namespace": "default",
                    "severity": "critical",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                },
                "annotations": {
                    "summary": "synthetic summary",
                },
            }
        },
    )


def _observations(incident_class_hint: str) -> ObservationBundle:
    return ObservationBundle(
        incident_id=f"{incident_class_hint}-123",
        incident_class_hint=incident_class_hint,
        namespace_hint="default",
        source="prometheus",
        alert_context={
            "labels": {"severity": "critical", "alertname": "HeraldAlert"},
            "annotations": {"summary": "synthetic summary"},
            "deployment_hint": "cartservice" if incident_class_hint == "crashloop" else "frontend",
            "service_hint": "frontend",
            "pod": "cartservice-7d6b9f5bb4-abcde",
            "container": "server",
        },
        kubernetes={"deployment": {"status": "succeeded"}},
        prometheus={
            "ready": {"status": "succeeded", "value": 1.0},
            "incident_signal": {"status": "succeeded", "value": 1.0},
        },
        collected_at="2026-03-29T20:00:00+00:00",
    )


class ReasonerTest(unittest.TestCase):
    def test_heuristic_reasoner_emits_ranked_intents(self) -> None:
        state = run_reasoner_pipeline(_incident("crashloop"), _observations("crashloop"))

        self.assertEqual(state["status"], "succeeded")
        self.assertIsNotNone(state["reasoner_output"])
        assert state["reasoner_output"] is not None
        self.assertGreaterEqual(len(state["reasoner_output"].intents), 1)
        self.assertEqual(state["reasoner_output"].intents[0].operation_family, "rollout.undo_deployment")
        self.assertTrue(state["mapped_v1_candidates"])

    def test_reasoner_llm_failure_falls_back_to_heuristic(self) -> None:
        class FailingLLM:
            def reason(self, **_: object) -> ReasonerLLMResult:
                raise RuntimeError("provider unavailable")

        state = run_reasoner_pipeline(
            _incident("network_partition"),
            _observations("network_partition"),
            llm=FailingLLM(),
        )

        self.assertEqual(state["status"], "succeeded")
        self.assertIn("falling back to heuristic", " ".join(state["errors"]))
        self.assertIsNotNone(state["reasoner_output"])
        assert state["reasoner_output"] is not None
        self.assertEqual(
            state["reasoner_output"].intents[0].operation_family,
            "chaos.delete_networkchaos",
        )


if __name__ == "__main__":
    unittest.main()
