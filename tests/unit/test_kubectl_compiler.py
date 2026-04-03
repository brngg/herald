from __future__ import annotations

import unittest

from schemas.intents import OperationIntent, ResourceTarget
from services.recovery.kubectl_compiler import (
    compile_execution_plan,
    compile_v1_dispatch_preview,
)


class KubectlCompilerTest(unittest.TestCase):
    def test_compiles_rollout_undo_intent_to_exact_command(self) -> None:
        plan = compile_execution_plan(
            OperationIntent(
                intent_id="intent-1",
                intent="Roll back cartservice.",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                arguments={},
                reversible=True,
                confidence_score=0.9,
                blast_radius_score=0.3,
                requires_approval=True,
                verification_hints={"post_check": "crashloop"},
                rollback_hints={"preferred_rollback": "rollout.undo_deployment"},
            )
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.steps[0].command,
            ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
        )
        self.assertEqual(
            compile_v1_dispatch_preview(plan)["allowed_tool_names"],
            ["get_deployment_context", "get_rollout_status", "rollout_undo_deployment"],
        )

    def test_compiles_human_review_as_non_executable_plan(self) -> None:
        plan = compile_execution_plan(
            OperationIntent(
                intent_id="intent-2",
                intent="Escalate the incident.",
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
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.steps, [])
        self.assertEqual(compile_v1_dispatch_preview(plan)["executable"], False)

    def test_compiles_scale_intent_to_bounded_command(self) -> None:
        plan = compile_execution_plan(
            OperationIntent(
                intent_id="intent-scale",
                intent="Scale frontend to two replicas.",
                operation_family="scale.deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="frontend"),
                arguments={"replicas": 2},
                reversible=True,
                confidence_score=0.72,
                blast_radius_score=0.25,
                requires_approval=True,
                verification_hints={"post_check": "deployment_readiness_shortfall"},
                rollback_hints={},
            )
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.steps[0].command,
            ["kubectl", "scale", "deployment/frontend", "-n", "default", "--replicas=2"],
        )
        preview = compile_v1_dispatch_preview(plan)
        self.assertEqual(preview["action_type"], "scale_deployment")
        self.assertEqual(preview["parameters"]["replicas"], 2)

    def test_compiles_delete_pod_intent_to_exact_command(self) -> None:
        plan = compile_execution_plan(
            OperationIntent(
                intent_id="intent-delete-pod",
                intent="Delete the unhealthy cartservice Pod.",
                operation_family="pod.delete_stateless_pod",
                target=ResourceTarget(namespace="default", kind="Pod", name="cartservice-abcde"),
                arguments={"deployment": "cartservice", "stateless_workload": True},
                reversible=True,
                confidence_score=0.6,
                blast_radius_score=0.18,
                requires_approval=True,
                verification_hints={
                    "post_check": "deployment_readiness_shortfall",
                    "deployment": "cartservice",
                    "min_ready_count": 1,
                },
                rollback_hints={},
            )
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.steps[0].command,
            ["kubectl", "delete", "pod", "cartservice-abcde", "-n", "default"],
        )
        preview = compile_v1_dispatch_preview(plan)
        self.assertEqual(preview["action_type"], "delete_pod")
        self.assertEqual(preview["parameters"]["pod"], "cartservice-abcde")
        self.assertEqual(preview["parameters"]["deployment"], "cartservice")


if __name__ == "__main__":
    unittest.main()
