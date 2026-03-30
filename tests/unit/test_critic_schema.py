from __future__ import annotations

import unittest

from schemas.critic import CriticOutput, CritiqueCandidate, PolicyCheckResult, critic_output_from_dict


class CriticSchemaTest(unittest.TestCase):
    def test_accepts_valid_payload(self) -> None:
        output = CriticOutput(
            summary="Policy validation approved the safe intent.",
            global_concerns=["One intent needs escalation."],
            candidates=[
                CritiqueCandidate(
                    intent_id="reasoner-rollout-undo-cartservice",
                    approved_for_consideration=True,
                    concerns=["Reversible and namespaced."],
                    policy_checks=[
                        PolicyCheckResult(
                            policy_name="requires_approval_enforced",
                            passed=True,
                            reason="Intent already requires human approval.",
                        )
                    ],
                    recommended_rank=1,
                    requires_escalation=False,
                )
            ],
        )

        self.assertEqual(output.candidates[0].intent_id, "reasoner-rollout-undo-cartservice")

    def test_rejects_empty_summary(self) -> None:
        with self.assertRaises(ValueError):
            CriticOutput(summary="", global_concerns=[], candidates=[])

    def test_parses_untrusted_payload(self) -> None:
        output = critic_output_from_dict(
            {
                "summary": "Policy validation approved one candidate.",
                "global_concerns": ["Escalation is preferred."],
                "candidates": [
                    {
                        "intent_id": "reasoner-escalate-frontend-cpu",
                        "approved_for_consideration": True,
                        "concerns": ["Escalation is appropriate."],
                        "policy_checks": [
                            {
                                "policy_name": "requires_approval_enforced",
                                "passed": True,
                                "reason": "Intent already requires human approval.",
                            }
                        ],
                        "recommended_rank": 1,
                        "requires_escalation": True,
                    }
                ],
            }
        )

        self.assertEqual(output.candidates[0].recommended_rank, 1)
        self.assertTrue(output.candidates[0].requires_escalation)


if __name__ == "__main__":
    unittest.main()
