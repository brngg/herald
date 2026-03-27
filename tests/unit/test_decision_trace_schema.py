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
        self.assertEqual(trace.node_runs_by_node, {})
        self.assertEqual(trace.latest_run_id_by_node, {})

    def test_accepts_valid_multi_node_provenance(self) -> None:
        trace = DecisionTrace(
            incident_id="abc123",
            fixer_plan={"actions": []},
            judge_verdict="pass",
            judge_reason="ok",
            routing_decision="request_approval_single_action",
            human_approval="approved",
            execution_result={},
            verification_result={},
            rollback_triggered=False,
            final_state="recovered",
            node_runs_by_node={
                "fixer": {
                    "fixer:0001": {
                        "run_id": "fixer:0001",
                        "node_name": "fixer",
                        "sequence": 1,
                        "attempt": 1,
                        "started_at": "2026-03-27T03:00:00+00:00",
                        "finished_at": "2026-03-27T03:00:01+00:00",
                        "status": "succeeded",
                        "summary": "Fixer planned.",
                        "llm_explanation": "Roll back to the previous ReplicaSet first.",
                        "input_summary": {},
                        "output_summary": {},
                        "artifact_refs": [],
                    }
                },
                "judge": {
                    "judge:0002": {
                        "run_id": "judge:0002",
                        "node_name": "judge",
                        "sequence": 2,
                        "attempt": 1,
                        "started_at": "2026-03-27T03:00:01+00:00",
                        "finished_at": "2026-03-27T03:00:02+00:00",
                        "status": "pass",
                        "summary": "Judge approved.",
                        "input_summary": {},
                        "output_summary": {},
                        "artifact_refs": [],
                    }
                },
            },
            latest_run_id_by_node={"fixer": "fixer:0001", "judge": "judge:0002"},
        )

        self.assertEqual(trace.latest_run_id_by_node["judge"], "judge:0002")
        self.assertEqual(
            trace.node_runs_by_node["fixer"]["fixer:0001"]["llm_explanation"],
            "Roll back to the previous ReplicaSet first.",
        )

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

    def test_rejects_unsupported_final_state(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="n/a",
                judge_reason="pending",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="done",  # type: ignore[arg-type]
            )

    def test_rejects_missing_latest_run_id(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="pass",
                judge_reason="ok",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="escalated",
                node_runs_by_node={
                    "fixer": {
                        "fixer:0001": {
                            "run_id": "fixer:0001",
                            "node_name": "fixer",
                            "sequence": 1,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:00+00:00",
                            "finished_at": "2026-03-27T03:00:01+00:00",
                            "status": "succeeded",
                            "summary": "Fixer planned.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        }
                    }
                },
                latest_run_id_by_node={},
            )

    def test_rejects_non_string_llm_explanation(self) -> None:
        with self.assertRaises(TypeError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="pass",
                judge_reason="ok",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="escalated",
                node_runs_by_node={
                    "fixer": {
                        "fixer:0001": {
                            "run_id": "fixer:0001",
                            "node_name": "fixer",
                            "sequence": 1,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:00+00:00",
                            "finished_at": "2026-03-27T03:00:01+00:00",
                            "status": "succeeded",
                            "summary": "Fixer planned.",
                            "llm_explanation": 123,  # type: ignore[assignment]
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        }
                    }
                },
                latest_run_id_by_node={"fixer": "fixer:0001"},
            )

    def test_rejects_duplicate_global_sequence(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="pass",
                judge_reason="ok",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="escalated",
                node_runs_by_node={
                    "fixer": {
                        "fixer:0001": {
                            "run_id": "fixer:0001",
                            "node_name": "fixer",
                            "sequence": 1,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:00+00:00",
                            "finished_at": "2026-03-27T03:00:01+00:00",
                            "status": "succeeded",
                            "summary": "Fixer planned.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        }
                    },
                    "judge": {
                        "judge:0001": {
                            "run_id": "judge:0001",
                            "node_name": "judge",
                            "sequence": 1,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:01+00:00",
                            "finished_at": "2026-03-27T03:00:02+00:00",
                            "status": "pass",
                            "summary": "Judge approved.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        }
                    },
                },
                latest_run_id_by_node={"fixer": "fixer:0001", "judge": "judge:0001"},
            )

    def test_rejects_non_monotonic_per_node_attempt(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="pass",
                judge_reason="ok",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="escalated",
                node_runs_by_node={
                    "fixer": {
                        "fixer:0001": {
                            "run_id": "fixer:0001",
                            "node_name": "fixer",
                            "sequence": 1,
                            "attempt": 2,
                            "started_at": "2026-03-27T03:00:00+00:00",
                            "finished_at": "2026-03-27T03:00:01+00:00",
                            "status": "succeeded",
                            "summary": "Fixer planned.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        },
                        "fixer:0002": {
                            "run_id": "fixer:0002",
                            "node_name": "fixer",
                            "sequence": 2,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:01+00:00",
                            "finished_at": "2026-03-27T03:00:02+00:00",
                            "status": "succeeded",
                            "summary": "Fixer replanned.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        },
                    }
                },
                latest_run_id_by_node={"fixer": "fixer:0002"},
            )

    def test_rejects_run_with_mismatched_node_name(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTrace(
                incident_id="abc123",
                fixer_plan={"actions": []},
                judge_verdict="pass",
                judge_reason="ok",
                routing_decision="halt",
                human_approval="n/a",
                execution_result={},
                verification_result={},
                rollback_triggered=False,
                final_state="escalated",
                node_runs_by_node={
                    "fixer": {
                        "fixer:0001": {
                            "run_id": "fixer:0001",
                            "node_name": "judge",
                            "sequence": 1,
                            "attempt": 1,
                            "started_at": "2026-03-27T03:00:00+00:00",
                            "finished_at": "2026-03-27T03:00:01+00:00",
                            "status": "succeeded",
                            "summary": "Fixer planned.",
                            "input_summary": {},
                            "output_summary": {},
                            "artifact_refs": [],
                        }
                    }
                },
                latest_run_id_by_node={"fixer": "fixer:0001"},
            )


if __name__ == "__main__":
    unittest.main()
