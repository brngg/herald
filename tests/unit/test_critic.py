from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agents.critic import run_critic_pipeline
from schemas.critic import CriticOutput, CritiqueCandidate, PolicyCheckResult
from schemas.incident import Incident
from schemas.intents import OperationIntent, ReasonerOutput, ResourceTarget
from schemas.observations import ObservationBundle


def _incident() -> Incident:
    return Incident(
        incident_id="incident-123",
        incident_class="crashloop",
        detected_at=datetime.now(tz=UTC),
        source="prometheus",
        raw_context={"alert": {"labels": {"namespace": "default"}}},
    )


def _observations() -> ObservationBundle:
    return ObservationBundle(
        incident_id="incident-123",
        incident_class_hint="crashloop",
        namespace_hint="default",
        source="prometheus",
        alert_context={"labels": {"namespace": "default", "alertname": "HeraldCartserviceCrashLoopBackOff"}},
        kubernetes={"pods": {"status": "succeeded"}},
        prometheus={"ready": {"status": "succeeded", "value": 1.0}},
        collected_at="2026-03-29T20:00:00+00:00",
    )


def _reasoner_output() -> ReasonerOutput:
    return ReasonerOutput(
        diagnosis_summary="cartservice is crash looping",
        likely_causes=["bad deployment"],
        missing_information=[],
        intents=[
            OperationIntent(
                intent_id="reasoner-rollout-undo-cartservice",
                intent="Roll back the cartservice Deployment.",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                arguments={},
                reversible=True,
                confidence_score=0.9,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            )
        ],
    )


class CriticPipelineTest(unittest.TestCase):
    def test_heuristic_fallback_produces_policy_summary(self) -> None:
        state = run_critic_pipeline(_incident(), _observations(), _reasoner_output(), llm=None)

        self.assertEqual(state["status"], "succeeded")
        self.assertIsInstance(state["critic_output"], CriticOutput)
        self.assertEqual(state["policy_summary"]["approved_candidate_count"], 1)
        self.assertEqual(state["critic_output"].candidates[0].intent_id, "reasoner-rollout-undo-cartservice")

    def test_provider_failure_falls_back_to_heuristic(self) -> None:
        class FailingLLM:
            def critique(self, **_: object) -> object:
                raise ValueError("boom")

        state = run_critic_pipeline(_incident(), _observations(), _reasoner_output(), llm=FailingLLM())

        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(any("falling back to heuristic" in error for error in state["errors"]))
        self.assertIsNotNone(state["critic_output"])
        self.assertEqual(state["critic_output"].candidates[0].recommended_rank, 1)


if __name__ == "__main__":
    unittest.main()
