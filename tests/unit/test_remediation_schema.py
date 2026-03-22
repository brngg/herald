from __future__ import annotations

import unittest

from schemas.remediation import RemediationAction


class RemediationActionSchemaTest(unittest.TestCase):
    def test_accepts_valid_payload(self) -> None:
        action = RemediationAction(
            action_id="rollback-cartservice",
            action_type="rollout_undo_deployment",
            description="Undo the bad cartservice rollout",
            confidence_score=0.95,
            blast_radius_score=0.2,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        )

        self.assertEqual(action.action_id, "rollback-cartservice")
        self.assertEqual(action.confidence_score, 0.95)
        self.assertEqual(action.blast_radius_score, 0.2)

    def test_accepts_integer_scores_and_normalizes_to_float(self) -> None:
        action = RemediationAction(
            action_id="do-nothing",
            action_type="do_nothing",
            description="Take no action and escalate",
            confidence_score=1,
            blast_radius_score=0,
            requires_approval=True,
            parameters={},
        )

        self.assertEqual(action.confidence_score, 1.0)
        self.assertEqual(action.blast_radius_score, 0.0)

    def test_rejects_invalid_action_type(self) -> None:
        with self.assertRaises(ValueError):
            RemediationAction(
                action_id="bad-action",
                action_type="delete_namespace",  # type: ignore[arg-type]
                description="Invalid action type",
                confidence_score=0.5,
                blast_radius_score=0.5,
                requires_approval=True,
                parameters={},
            )

    def test_rejects_out_of_range_confidence(self) -> None:
        with self.assertRaises(ValueError):
            RemediationAction(
                action_id="bad-confidence",
                action_type="escalate",
                description="Confidence is too high",
                confidence_score=1.2,
                blast_radius_score=0.1,
                requires_approval=True,
                parameters={},
            )

    def test_rejects_non_bool_requires_approval(self) -> None:
        with self.assertRaises(TypeError):
            RemediationAction(
                action_id="bad-bool",
                action_type="escalate",
                description="requires_approval must be bool",
                confidence_score=0.4,
                blast_radius_score=0.1,
                requires_approval="yes",  # type: ignore[arg-type]
                parameters={},
            )


if __name__ == "__main__":
    unittest.main()
