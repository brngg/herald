from __future__ import annotations

import unittest

from schemas.intents import ResourceTarget
from schemas.verification import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationPlan,
    VerificationResultV2,
    verification_plan_from_dict,
    verification_result_from_dict,
)


class VerificationSchemaTest(unittest.TestCase):
    def test_accepts_valid_plan_and_result_payloads(self) -> None:
        plan = VerificationPlan(
            verification_id="verify-action-1",
            action_id="action-1",
            action_type="rollout_undo_deployment",
            target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
            summary="Verify rollout recovery.",
            checks=[
                VerificationCheck(
                    check_id="check-1",
                    check_type="kubernetes_rollout_status",
                    summary="Check rollout status.",
                    parameters={"namespace": "default", "deployment": "cartservice"},
                )
            ],
            warnings=["shadow-only"],
        )
        result = VerificationResultV2(
            verification_id="verify-action-1",
            status="passed",
            summary="Verification passed.",
            plan=plan,
            check_results=[
                VerificationCheckResult(
                    check_id="check-1",
                    check_type="kubernetes_rollout_status",
                    passed=True,
                    reason="Rollout succeeded.",
                    observed_value="succeeded",
                    expected_value="succeeded",
                )
            ],
            warnings=["shadow-only"],
        )

        self.assertEqual(result.plan.action_id, "action-1")

    def test_parses_nested_untrusted_payloads(self) -> None:
        result = verification_result_from_dict(
            {
                "verification_id": "verify-action-1",
                "status": "passed",
                "summary": "Verification passed.",
                "plan": {
                    "verification_id": "verify-action-1",
                    "action_id": "action-1",
                    "action_type": "rollout_undo_deployment",
                    "target": {
                        "namespace": "default",
                        "kind": "Deployment",
                        "name": "cartservice",
                        "selector": None,
                    },
                    "summary": "Verify rollout recovery.",
                    "checks": [],
                    "warnings": [],
                    "rollback_warning": None,
                },
                "check_results": [
                    {
                        "check_id": "check-1",
                        "check_type": "kubernetes_rollout_status",
                        "passed": True,
                        "reason": "Rollout succeeded.",
                        "observed_value": "succeeded",
                        "expected_value": "succeeded",
                    }
                ],
                "warnings": [],
                "failure_reason": None,
            }
        )

        self.assertEqual(result.plan.target.name, "cartservice")
        self.assertTrue(result.check_results[0].passed)

    def test_rejects_non_boolean_passed_flag(self) -> None:
        with self.assertRaises(TypeError):
            verification_result_from_dict(
                {
                    "verification_id": "verify-action-1",
                    "status": "passed",
                    "summary": "Verification passed.",
                    "plan": None,
                    "check_results": [
                        {
                            "check_id": "check-1",
                            "check_type": "kubernetes_rollout_status",
                            "passed": "yes",
                            "reason": "Rollout succeeded.",
                            "observed_value": "succeeded",
                            "expected_value": "succeeded",
                        }
                    ],
                    "warnings": [],
                }
            )

    def test_plan_parser_rejects_missing_checks_shape(self) -> None:
        with self.assertRaises(TypeError):
            verification_plan_from_dict(
                {
                    "verification_id": "verify-action-1",
                    "action_id": "action-1",
                    "action_type": "rollout_undo_deployment",
                    "target": {
                        "namespace": "default",
                        "kind": "Deployment",
                        "name": "cartservice",
                    },
                    "summary": "Verify rollout recovery.",
                    "checks": "not-a-list",
                    "warnings": [],
                }
            )


if __name__ == "__main__":
    unittest.main()
