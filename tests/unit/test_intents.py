from __future__ import annotations

import unittest

from schemas.intents import (
    OperationIntent,
    ReasonerOutput,
    ResourceTarget,
    reasoner_output_from_dict,
)


class IntentsSchemaTest(unittest.TestCase):
    def test_reasoner_output_from_dict_accepts_valid_payload(self) -> None:
        output = reasoner_output_from_dict(
            {
                "diagnosis_summary": "cartservice appears crash looping after a bad rollout",
                "likely_causes": ["A bad deployment revision"],
                "missing_information": [],
                "intents": [
                    {
                        "intent_id": "intent-1",
                        "intent": "Roll back cartservice to the previous ReplicaSet.",
                        "operation_family": "rollout.undo_deployment",
                        "target": {
                            "namespace": "default",
                            "kind": "Deployment",
                            "name": "cartservice",
                            "selector": None,
                        },
                        "arguments": {},
                        "reversible": True,
                        "confidence_score": 0.9,
                        "blast_radius_score": 0.3,
                        "requires_approval": True,
                        "verification_hints": {"post_check": "crashloop"},
                        "rollback_hints": {"preferred_rollback": "rollout.undo_deployment"},
                    }
                ],
            }
        )

        self.assertIsInstance(output, ReasonerOutput)
        self.assertEqual(output.intents[0].operation_family, "rollout.undo_deployment")
        self.assertEqual(output.intents[0].target.name, "cartservice")

    def test_operation_intent_rejects_requires_approval_false(self) -> None:
        with self.assertRaises(ValueError):
            OperationIntent(
                intent_id="intent-1",
                intent="Restart cartservice.",
                operation_family="rollout.restart_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                arguments={},
                reversible=True,
                confidence_score=0.5,
                blast_radius_score=0.2,
                requires_approval=False,
                verification_hints={},
                rollback_hints={},
            )

    def test_resource_target_rejects_empty_selector_value(self) -> None:
        with self.assertRaises(TypeError):
            ResourceTarget(
                namespace="default",
                kind="Deployment",
                name="frontend",
                selector={"app": ""},
            )


if __name__ == "__main__":
    unittest.main()
