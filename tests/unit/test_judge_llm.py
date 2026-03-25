from __future__ import annotations

import unittest

from schemas.remediation import RemediationAction
from services.judge_llm import (
    JudgeLLMResult,
    build_judge_prompts,
    parse_judge_llm_result,
)


class JudgeLLMResultTest(unittest.TestCase):
    def test_accepts_valid_result(self) -> None:
        result = JudgeLLMResult(verdict="pass", reason="Plan is bounded.")

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.reason, "Plan is bounded.")

    def test_rejects_invalid_verdict(self) -> None:
        with self.assertRaises(ValueError):
            JudgeLLMResult(verdict="maybe", reason="unclear")  # type: ignore[arg-type]

    def test_rejects_empty_reason(self) -> None:
        with self.assertRaises(ValueError):
            JudgeLLMResult(verdict="fail", reason="")

    def test_parse_judge_llm_result_accepts_valid_payload(self) -> None:
        result = parse_judge_llm_result({"verdict": "pass", "reason": "Plan is bounded."})

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.reason, "Plan is bounded.")

    def test_parse_judge_llm_result_rejects_invalid_verdict(self) -> None:
        with self.assertRaises(ValueError):
            parse_judge_llm_result({"verdict": "maybe", "reason": "unclear"})

    def test_build_judge_prompts_includes_actions_and_rationale(self) -> None:
        _, user_prompt = build_judge_prompts(
            incident_summary="[critical] crashloop",
            evidence={
                "incident_class": "crashloop",
                "incident_class_normalized": "crashloop",
                "namespace": "default",
                "pod": "cartservice-abc",
                "labels": {"deployment": "cartservice"},
            },
            actions=[
                RemediationAction(
                    action_id="rollout_undo_cartservice",
                    action_type="rollout_undo_deployment",
                    description="Roll back cartservice Deployment.",
                    confidence_score=0.9,
                    blast_radius_score=0.3,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "cartservice"},
                )
            ],
            fixer_rationale="Rollback the last deployment first.",
        )

        self.assertIn("rollout_undo_cartservice", user_prompt)
        self.assertIn("Rollback the last deployment first.", user_prompt)
        self.assertIn('"deployment_hint": "cartservice"', user_prompt)


if __name__ == "__main__":
    unittest.main()
