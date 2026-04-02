from __future__ import annotations

import unittest

from schemas.critic import CriticOutput
from schemas.intents import CapabilityCatalog, OperationIntent, ReasonerOutput, ResourceTarget
from schemas.observations import ObservationBundle
from services.llm.tasks.critic_contract import (
    build_critic_prompts,
    critic_output_json_schema,
    parse_critic_llm_result,
)


class CriticLLMTest(unittest.TestCase):
    def test_critic_output_json_schema_includes_candidates(self) -> None:
        schema = critic_output_json_schema()
        self.assertIn("summary", schema["properties"])
        self.assertIn("candidates", schema["properties"])

    def test_build_critic_prompts_uses_compact_observation_summary(self) -> None:
        observations = ObservationBundle(
            incident_id="incident-123",
            incident_class_hint="crashloop",
            namespace_hint="default",
            source="prometheus",
            alert_context={"labels": {"alertname": "HeraldCartserviceCrashLoopBackOff"}, "annotations": {}},
            kubernetes={"pods": {"status": "succeeded"}},
            prometheus={"ready": {"status": "succeeded", "value": 1.0}},
            collected_at="2026-03-29T20:00:00+00:00",
        )
        reasoner_output = ReasonerOutput(
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
        catalog = CapabilityCatalog(version="phase2-shadow-v1", capabilities=[{"operation_family": "rollout.undo_deployment"}])

        system_prompt, user_prompt = build_critic_prompts(
            incident_summary="[critical] crashloop",
            observations=observations,
            reasoner_output=reasoner_output,
            policy_summary={"approved_candidate_count": 1},
            capability_catalog=catalog,
        )

        self.assertIn("HERALD Critic", system_prompt)
        self.assertIn('"incident_id": "incident-123"', user_prompt)
        self.assertIn('"approved_candidate_count": 1', user_prompt)
        self.assertNotIn("cartservice-7d6b9f5bb4-abcde", user_prompt)

    def test_parse_critic_llm_result_returns_typed_output(self) -> None:
        result = parse_critic_llm_result(
            {
                "summary": "Policy validation approved the safe intent.",
                "global_concerns": [],
                "candidates": [],
            }
        )

        self.assertIsInstance(result, CriticOutput)


if __name__ == "__main__":
    unittest.main()
