from __future__ import annotations

import unittest

from schemas.critic import CriticOutput, CritiqueCandidate, PolicyCheckResult
from schemas.intents import OperationIntent, ReasonerOutput, ResourceTarget
from services.recovery.intent_synthesizer import (
    compile_shadow_dispatches,
    synthesize_execution_plans,
)


class IntentSynthesizerTest(unittest.TestCase):
    def test_preserves_critic_rank_order_and_compiles_dispatches(self) -> None:
        reasoner_output = ReasonerOutput(
            diagnosis_summary="cartservice is crash looping",
            likely_causes=["bad rollout"],
            missing_information=[],
            intents=[
                OperationIntent(
                    intent_id="intent-restart",
                    intent="Restart cartservice.",
                    operation_family="rollout.restart_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    arguments={},
                    reversible=True,
                    confidence_score=0.5,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    verification_hints={},
                    rollback_hints={},
                ),
                OperationIntent(
                    intent_id="intent-undo",
                    intent="Roll back cartservice.",
                    operation_family="rollout.undo_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    arguments={},
                    reversible=True,
                    confidence_score=0.9,
                    blast_radius_score=0.3,
                    requires_approval=True,
                    verification_hints={},
                    rollback_hints={},
                ),
            ],
        )
        critic_output = CriticOutput(
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
                ),
                CritiqueCandidate(
                    intent_id="intent-restart",
                    approved_for_consideration=True,
                    concerns=[],
                    policy_checks=[
                        PolicyCheckResult(policy_name="requires_approval", passed=True, reason="ok"),
                    ],
                    recommended_rank=2,
                    requires_escalation=False,
                ),
            ],
        )

        synthesis_output = synthesize_execution_plans(reasoner_output, critic_output)
        dispatches = compile_shadow_dispatches(synthesis_output)

        self.assertEqual(synthesis_output.plans[0].intent_id, "intent-undo")
        self.assertEqual(dispatches[0]["action_type"], "rollout_undo_deployment")
        self.assertEqual(dispatches[0]["command"][2], "undo")

    def test_marks_escalation_plan_non_executable(self) -> None:
        reasoner_output = ReasonerOutput(
            diagnosis_summary="escalate",
            likely_causes=["unknown"],
            missing_information=[],
            intents=[
                OperationIntent(
                    intent_id="intent-escalate",
                    intent="Escalate to a human.",
                    operation_family="escalate.human_review",
                    target=ResourceTarget(namespace="default", kind="Incident", name="incident-123"),
                    arguments={"reason": "Need human review."},
                    reversible=True,
                    confidence_score=0.2,
                    blast_radius_score=0.0,
                    requires_approval=True,
                    verification_hints={},
                    rollback_hints={},
                )
            ],
        )

        synthesis_output = synthesize_execution_plans(reasoner_output, None)

        self.assertEqual(synthesis_output.plans[0].steps, [])
        self.assertIn("Human review intent is explicitly non-executable.", synthesis_output.warnings)


if __name__ == "__main__":
    unittest.main()
