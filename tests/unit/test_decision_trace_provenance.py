from __future__ import annotations

import unittest

from schemas.decision_trace import DecisionTrace
from services.runtime.decision_trace import (
    append_node_run,
    derive_trace_timeline,
    get_latest_node_run,
    initialize_trace_provenance,
)


def _trace() -> DecisionTrace:
    return DecisionTrace(
        incident_id="incident-123",
        fixer_plan={"actions": []},
        judge_verdict="n/a",
        judge_reason="pending",
        routing_decision="halt",
        human_approval="n/a",
        execution_result={},
        verification_result={},
        rollback_triggered=False,
        final_state="pending_approval",
    )


class DecisionTraceProvenanceTest(unittest.TestCase):
    def test_initialize_trace_provenance_preserves_empty_defaults(self) -> None:
        trace = initialize_trace_provenance(_trace())

        self.assertEqual(trace.node_runs_by_node, {})
        self.assertEqual(trace.latest_run_id_by_node, {})

    def test_append_node_run_creates_run_id_and_increments_sequence(self) -> None:
        trace = append_node_run(
            _trace(),
            node_name="fixer",
            status="succeeded",
            summary="Fixer ran.",
            llm_explanation="Rollback is the highest-confidence bounded action.",
            input_summary={"incident_id": "incident-123"},
            output_summary={"action_ids": ["undo"]},
        )
        trace = append_node_run(
            trace,
            node_name="judge",
            status="pass",
            summary="Judge ran.",
            input_summary={"action_ids": ["undo"]},
            output_summary={"judge_verdict": "pass"},
        )

        fixer_run = trace.node_runs_by_node["fixer"]["fixer:0001"]
        judge_run = trace.node_runs_by_node["judge"]["judge:0002"]
        self.assertEqual(fixer_run["sequence"], 1)
        self.assertEqual(
            fixer_run["llm_explanation"],
            "Rollback is the highest-confidence bounded action.",
        )
        self.assertEqual(judge_run["sequence"], 2)
        self.assertEqual(trace.latest_run_id_by_node["judge"], "judge:0002")

    def test_append_node_run_increments_attempt_per_node(self) -> None:
        trace = append_node_run(
            _trace(),
            node_name="fixer",
            status="succeeded",
            summary="Fixer run 1.",
            input_summary={},
            output_summary={},
        )
        trace = append_node_run(
            trace,
            node_name="fixer",
            status="succeeded",
            summary="Fixer run 2.",
            input_summary={},
            output_summary={},
        )

        first_run = trace.node_runs_by_node["fixer"]["fixer:0001"]
        second_run = trace.node_runs_by_node["fixer"]["fixer:0002"]
        self.assertEqual(first_run["attempt"], 1)
        self.assertEqual(second_run["attempt"], 2)

    def test_get_latest_node_run_returns_newest_run(self) -> None:
        trace = append_node_run(
            _trace(),
            node_name="fixer",
            status="succeeded",
            summary="Fixer run 1.",
            input_summary={},
            output_summary={"run": 1},
        )
        trace = append_node_run(
            trace,
            node_name="fixer",
            status="succeeded",
            summary="Fixer run 2.",
            input_summary={},
            output_summary={"run": 2},
        )

        latest = get_latest_node_run(trace, "fixer")

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["run_id"], "fixer:0002")
        self.assertEqual(latest["output_summary"]["run"], 2)

    def test_derive_trace_timeline_orders_runs_globally(self) -> None:
        trace = append_node_run(
            _trace(),
            node_name="judge",
            status="pass",
            summary="Judge ran.",
            input_summary={},
            output_summary={},
        )
        trace = append_node_run(
            trace,
            node_name="fixer",
            status="succeeded",
            summary="Fixer reran.",
            input_summary={},
            output_summary={},
        )

        timeline = derive_trace_timeline(trace)

        self.assertEqual([item["sequence"] for item in timeline], [1, 2])
        self.assertEqual([item["run_id"] for item in timeline], ["judge:0001", "fixer:0002"])


if __name__ == "__main__":
    unittest.main()
