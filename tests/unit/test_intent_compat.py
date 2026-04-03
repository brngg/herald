from __future__ import annotations

import unittest

from schemas.intents import OperationIntent, ResourceTarget
from services.recovery.intent_compat import intent_to_v1_remediation


class IntentCompatTest(unittest.TestCase):
    def test_maps_rollout_undo_to_v1_remediation(self) -> None:
        action = intent_to_v1_remediation(
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
                verification_hints={},
                rollback_hints={},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_id, "rollout_undo_cartservice")
        self.assertEqual(action.action_type, "rollout_undo_deployment")

    def test_maps_networkchaos_delete_to_v1_remediation(self) -> None:
        action = intent_to_v1_remediation(
            OperationIntent(
                intent_id="intent-2",
                intent="Delete the network chaos object.",
                operation_family="chaos.delete_networkchaos",
                target=ResourceTarget(
                    namespace="default",
                    kind="NetworkChaos",
                    name="frontend-to-cartservice-partition",
                ),
                arguments={},
                reversible=True,
                confidence_score=0.88,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_type, "delete_networkchaos")
        self.assertEqual(action.parameters["name"], "frontend-to-cartservice-partition")

    def test_maps_scale_intent_to_v1_remediation(self) -> None:
        action = intent_to_v1_remediation(
            OperationIntent(
                intent_id="intent-3",
                intent="Scale frontend to two replicas.",
                operation_family="scale.deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="frontend"),
                arguments={"replicas": 2},
                reversible=True,
                confidence_score=0.7,
                blast_radius_score=0.25,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_type, "scale_deployment")
        self.assertEqual(action.parameters["replicas"], 2)

    def test_maps_delete_pod_intent_to_v1_remediation(self) -> None:
        action = intent_to_v1_remediation(
            OperationIntent(
                intent_id="intent-4",
                intent="Delete an isolated unhealthy cartservice Pod.",
                operation_family="pod.delete_stateless_pod",
                target=ResourceTarget(namespace="default", kind="Pod", name="cartservice-abcde"),
                arguments={"deployment": "cartservice", "stateless_workload": True},
                reversible=True,
                confidence_score=0.64,
                blast_radius_score=0.18,
                requires_approval=True,
                verification_hints={"deployment": "cartservice", "min_ready_count": 1},
                rollback_hints={},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_type, "delete_pod")
        self.assertEqual(action.parameters["pod"], "cartservice-abcde")
        self.assertEqual(action.parameters["deployment"], "cartservice")


if __name__ == "__main__":
    unittest.main()
