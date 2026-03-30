from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from agents.synthesizer import run_synthesizer_pipeline
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
                intent_id="intent-undo",
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


def _critic_output() -> CriticOutput:
    return CriticOutput(
        summary="safe",
        global_concerns=[],
        candidates=[
            CritiqueCandidate(
                intent_id="intent-undo",
                approved_for_consideration=True,
                concerns=[],
                policy_checks=[
                    PolicyCheckResult(policy_name="requires_approval", passed=True, reason="ok"),
                ],
                recommended_rank=1,
                requires_escalation=False,
            )
        ],
    )


class SynthesizerPipelineTest(unittest.TestCase):
    def test_pipeline_returns_shadow_dispatches(self) -> None:
        state = run_synthesizer_pipeline(_incident(), _observations(), _reasoner_output(), _critic_output())

        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(state["synthesized_v1_dispatches"])
        self.assertEqual(state["synthesis_output"].plans[0].steps[0].tool_name, "rollout_undo_deployment")

    def test_pipeline_falls_back_to_failure_state_when_compiler_raises(self) -> None:
        with patch("agents.synthesizer.synthesize_execution_plans", side_effect=RuntimeError("boom")):
            state = run_synthesizer_pipeline(_incident(), _observations(), _reasoner_output(), _critic_output())

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["errors"])
        self.assertIsNotNone(state["synthesis_output"])
        self.assertEqual(state["synthesized_v1_dispatches"], [])


if __name__ == "__main__":
    unittest.main()
