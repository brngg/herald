from __future__ import annotations

import unittest

from schemas.execution_plan import (
    ExecutionPlan,
    ExecutionPlanStep,
    SynthesisOutput,
    execution_plan_from_dict,
    synthesis_output_from_dict,
)
from schemas.intents import ResourceTarget


class ExecutionPlanSchemaTest(unittest.TestCase):
    def test_synthesis_output_from_dict_accepts_valid_payload(self) -> None:
        output = synthesis_output_from_dict(
            {
                "summary": "Compiled one bounded execution plan.",
                "plans": [
                    {
                        "intent_id": "intent-1",
                        "operation_family": "rollout.undo_deployment",
                        "target": {
                            "namespace": "default",
                            "kind": "Deployment",
                            "name": "cartservice",
                            "selector": None,
                        },
                        "summary": "Shadow execution plan to roll back Deployment cartservice in namespace default.",
                        "steps": [
                            {
                                "step_id": "intent-1:step-1",
                                "tool_name": "rollout_undo_deployment",
                                "command": [
                                    "kubectl",
                                    "rollout",
                                    "undo",
                                    "deployment/cartservice",
                                    "-n",
                                    "default",
                                ],
                                "expected_effect": "Undo the approved Deployment rollout.",
                                "reversible": True,
                                "verification_hints": {"post_check": "crashloop"},
                            }
                        ],
                        "allowed_tool_names": [
                            "get_deployment_context",
                            "get_rollout_status",
                            "rollout_undo_deployment",
                        ],
                        "blast_radius_score": 0.3,
                        "requires_approval": True,
                        "rollback_outline": {"preferred_rollback": "rollout.undo_deployment"},
                    }
                ],
                "unsupported_intents": [],
                "warnings": ["shadow-only"],
            }
        )

        self.assertIsInstance(output, SynthesisOutput)
        self.assertEqual(output.plans[0].steps[0].command[2], "undo")

    def test_execution_plan_rejects_steps_outside_allowed_tool_names(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionPlan(
                intent_id="intent-1",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                summary="Shadow execution plan",
                steps=[
                    ExecutionPlanStep(
                        step_id="intent-1:step-1",
                        tool_name="rollout_undo_deployment",
                        command=["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
                        expected_effect="Undo the approved Deployment rollout.",
                        reversible=True,
                        verification_hints={},
                    )
                ],
                allowed_tool_names=["get_deployment_context"],
                blast_radius_score=0.3,
                requires_approval=True,
                rollback_outline={},
            )

    def test_execution_plan_from_dict_accepts_zero_step_escalation(self) -> None:
        plan = execution_plan_from_dict(
            {
                "intent_id": "intent-2",
                "operation_family": "escalate.human_review",
                "target": {"namespace": "default", "kind": "Incident", "name": "incident-123", "selector": None},
                "summary": "Non-executable shadow plan for human review: Escalate.",
                "steps": [],
                "allowed_tool_names": [],
                "blast_radius_score": 0.0,
                "requires_approval": True,
                "rollback_outline": {},
            }
        )

        self.assertEqual(plan.steps, [])
        self.assertEqual(plan.allowed_tool_names, [])


if __name__ == "__main__":
    unittest.main()
