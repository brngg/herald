from __future__ import annotations

import unittest

from evaluation.metrics import compute_metrics


class MetricsTest(unittest.TestCase):
    def test_compute_metrics_aggregates_recommendation_and_verification_rates(self) -> None:
        run_artifacts = [
            {
                "expected": {
                    "top_action_id": "rollout_undo_cartservice",
                    "requires_approval": True,
                    "final_state": "recovered",
                },
                "result": {
                    "hitl_decision": {
                        "requires_approval": True,
                        "candidate_actions": [
                            {"action_id": "rollout_undo_cartservice"},
                            {"action_id": "restart_cartservice"},
                        ],
                    },
                    "decision_trace": {
                        "incident_id": "incident-1",
                        "fixer_plan": {"actions": []},
                        "judge_verdict": "pass",
                        "judge_reason": "ok",
                        "routing_decision": "request_approval_single_action",
                        "human_approval": "approved",
                        "execution_result": {"status": "succeeded"},
                        "verification_result": {"recovery_latency_seconds": 4.0},
                        "rollback_triggered": False,
                        "final_state": "recovered",
                    },
                },
            },
            {
                "expected": {
                    "top_action_id": "rollout_undo_cartservice",
                    "requires_approval": True,
                    "final_state": "escalated",
                },
                "result": {
                    "hitl_decision": {
                        "requires_approval": True,
                        "candidate_actions": [
                            {"action_id": "restart_cartservice"},
                            {"action_id": "rollout_undo_cartservice"},
                        ],
                    },
                    "decision_trace": {
                        "incident_id": "incident-2",
                        "fixer_plan": {"actions": []},
                        "judge_verdict": "pass",
                        "judge_reason": "ok",
                        "routing_decision": "request_approval_ranked_options",
                        "human_approval": "approved",
                        "execution_result": {"status": "failed"},
                        "verification_result": {},
                        "rollback_triggered": False,
                        "final_state": "escalated",
                    },
                },
            },
        ]

        metrics = compute_metrics(run_artifacts)

        self.assertEqual(metrics["total_runs"], 2)
        self.assertEqual(metrics["recommendation_top1_rate"], 0.5)
        self.assertEqual(metrics["recommendation_top2_rate"], 1.0)
        self.assertEqual(metrics["approval_policy_correct_rate"], 1.0)
        self.assertEqual(metrics["execution_success_rate"], 0.5)
        self.assertEqual(metrics["verification_correct_rate"], 1.0)
        self.assertEqual(metrics["false_recovery_rate"], 0.0)
        self.assertEqual(metrics["decision_trace_coverage_rate"], 1.0)
        self.assertEqual(metrics["median_recovery_latency_seconds"], 4.0)


if __name__ == "__main__":
    unittest.main()
