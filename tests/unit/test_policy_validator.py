from __future__ import annotations

import unittest

from schemas.intents import OperationIntent, ResourceTarget
from services.recovery.policy_validator import validate_shadow_intent_policies


class PolicyValidatorTest(unittest.TestCase):
    def test_prefers_low_blast_radius_reversible_namespaced_intent(self) -> None:
        intent = OperationIntent(
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

        results = validate_shadow_intent_policies([intent])

        self.assertEqual(results[0].intent_id, intent.intent_id)
        self.assertTrue(results[0].approved_for_consideration)
        self.assertFalse(results[0].requires_escalation)

    def test_blocks_high_blast_radius_intent(self) -> None:
        intent = OperationIntent(
            intent_id="reasoner-delete-networkchaos",
            intent="Delete the active NetworkChaos object.",
            operation_family="chaos.delete_networkchaos",
            target=ResourceTarget(namespace="default", kind="NetworkChaos", name="frontend-to-cartservice"),
            arguments={},
            reversible=True,
            confidence_score=0.9,
            blast_radius_score=0.95,
            requires_approval=True,
            verification_hints={},
            rollback_hints={},
        )

        results = validate_shadow_intent_policies([intent])

        self.assertFalse(results[0].approved_for_consideration)
        self.assertTrue(results[0].requires_escalation)
        self.assertTrue(
            any(
                check.policy_name == "blast_radius_below_block_threshold" and not check.passed
                for check in results[0].policy_checks
            )
        )

    def test_boosts_escalation_intent_when_other_intents_are_unsafe(self) -> None:
        unsafe = OperationIntent(
            intent_id="reasoner-delete-networkchaos",
            intent="Delete the active NetworkChaos object.",
            operation_family="chaos.delete_networkchaos",
            target=ResourceTarget(namespace="default", kind="NetworkChaos", name="frontend-to-cartservice"),
            arguments={},
            reversible=True,
            confidence_score=0.9,
            blast_radius_score=0.95,
            requires_approval=True,
            verification_hints={},
            rollback_hints={},
        )
        escalate = OperationIntent(
            intent_id="reasoner-escalate-network-partition",
            intent="Escalate the incident to a human operator.",
            operation_family="escalate.human_review",
            target=ResourceTarget(namespace="default", kind="Incident", name="incident-123"),
            arguments={"reason": "The blast radius is too high."},
            reversible=True,
            confidence_score=0.2,
            blast_radius_score=0.0,
            requires_approval=True,
            verification_hints={},
            rollback_hints={},
        )

        results = validate_shadow_intent_policies([unsafe, escalate])

        self.assertEqual(results[0].intent_id, "reasoner-escalate-network-partition")
        self.assertTrue(results[0].approved_for_consideration)
        self.assertTrue(results[0].requires_escalation)


if __name__ == "__main__":
    unittest.main()
