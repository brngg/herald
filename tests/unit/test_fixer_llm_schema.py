from __future__ import annotations

import unittest

from services.llm.tasks.fixer_contract import build_fixer_prompts, parse_fixer_llm_result


class FixerLLMSchemaTest(unittest.TestCase):
    def test_parse_fixer_llm_result_accepts_valid_payload(self) -> None:
        result = parse_fixer_llm_result(
            {
                "rationale": "Roll back to last known good deployment; restart as fallback.",
                "actions": [
                    {
                        "action_id": "rollout_undo_cartservice",
                        "action_type": "rollout_undo_deployment",
                        "description": "Roll back cartservice Deployment to previous ReplicaSet.",
                        "confidence_score": 0.9,
                        "blast_radius_score": 0.3,
                        "requires_approval": True,
                        "parameters": {"namespace": "default", "deployment": "cartservice"},
                    }
                ],
            }
        )

        self.assertEqual(result.actions[0].action_id, "rollout_undo_cartservice")
        self.assertTrue(result.rationale)

    def test_parse_fixer_llm_result_rejects_missing_rationale(self) -> None:
        with self.assertRaises(ValueError):
            parse_fixer_llm_result({"actions": []})

    def test_parse_fixer_llm_result_normalizes_deployment_name_alias(self) -> None:
        result = parse_fixer_llm_result(
            {
                "rationale": "Undo or restart the Deployment.",
                "actions": [
                    {
                        "action_id": "undo_cartservice_deployment",
                        "action_type": "rollout_undo_deployment",
                        "description": "Undo the cartservice deployment.",
                        "confidence_score": 0.85,
                        "blast_radius_score": 0.3,
                        "requires_approval": True,
                        "parameters": {
                            "namespace": "default",
                            "deployment_name": "cartservice",
                        },
                    }
                ],
            }
        )

        self.assertEqual(result.actions[0].parameters["deployment"], "cartservice")
        self.assertNotIn("deployment_name", result.actions[0].parameters)

    def test_parse_fixer_llm_result_keeps_canonical_deployment_field(self) -> None:
        result = parse_fixer_llm_result(
            {
                "rationale": "Restart the Deployment.",
                "actions": [
                    {
                        "action_id": "restart_cartservice_deployment",
                        "action_type": "rollout_restart_deployment",
                        "description": "Restart the cartservice deployment.",
                        "confidence_score": 0.75,
                        "blast_radius_score": 0.2,
                        "requires_approval": True,
                        "parameters": {
                            "namespace": "default",
                            "deployment": "cartservice",
                        },
                    }
                ],
            }
        )

        self.assertEqual(result.actions[0].parameters["deployment"], "cartservice")

    def test_build_fixer_prompts_redacts_full_labels_and_annotations(self) -> None:
        _, user_prompt = build_fixer_prompts(
            incident_summary="[critical] crashloop",
            evidence={
                "incident_class": "crashloop",
                "incident_class_normalized": "crashloop",
                "alertname": "HeraldCartserviceCrashLoopBackOff",
                "namespace": "default",
                "severity": "critical",
                "summary": "cartservice is in CrashLoopBackOff",
                "pod": "cartservice-abc",
                "container": "server",
                "labels": {
                    "deployment": "cartservice",
                    "generatorURL": "http://internal-prometheus/graph",
                },
                "annotations": {
                    "runbook_url": "https://internal/runbook",
                },
            },
        )

        self.assertIn('"deployment_hint": "cartservice"', user_prompt)
        self.assertNotIn("generatorURL", user_prompt)
        self.assertNotIn("runbook_url", user_prompt)


if __name__ == "__main__":
    unittest.main()
