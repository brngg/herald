from __future__ import annotations

import unittest

from schemas.decision_trace import DecisionTrace


class DecisionTraceSchemaTest(unittest.TestCase):
    def test_accepts_valid_payload(self) -> None:
        trace = DecisionTrace(
            incident_id="abc123",
            fixer_plan={"actions": []},
            judge_verdict="n/a",
            judge_reason="not evaluated yet",
            routing_decision="halt",
            human_approval="n/a",
            execution_result={},
            verification_result={},
            rollback_triggered=False,
            final_state="unrecovered",
        )

        self.assertEqual(trace.incident_id, "abc123")

    def test_rejects_invalid_judge_verdict(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="maybe",  # type: ignore[arg-type]
                judge_reason="unclear",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="unrecovered",
            )

    def test_rejects_invalid_human_approval(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="n/a",
                judge_reason="pending",
                routing_decision="halt",
                human_approval="pending",  # type: ignore[arg-type]
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="unrecovered",
            )

    def test_rejects_non_string_routing_decision(self) -> None:
        with self.assertRaises(TypeError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="n/a",
                judge_reason="pending",
                routing_decision=123,  # type: ignore[arg-type]
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="unrecovered",
            )

    def test_rejects_non_bool_rollback_triggered(self) -> None:
        with self.assertRaises(TypeError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="n/a",
                judge_reason="pending",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered="false",  # type: ignore[arg-type]
                final_state="unrecovered",
            )


if __name__ == "__main__":
    unittest.main()
