from __future__ import annotations

from datetime import datetime, timezone
import unittest

from schemas.incident import Incident
from schemas.remediation import RemediationAction
from workflows.hitl_gate import (
    finalize_decision_trace,
    record_human_approval,
    route_crashloop_plan,
)


def _incident() -> Incident:
    return Incident(
        incident_id="trace-123",
        incident_class="crashloop",
        detected_at=datetime.now(tz=timezone.utc),
        source="prometheus",
        raw_context={
            "alert": {
                "labels": {
                    "alertname": "HeraldCartserviceCrashLoopBackOff",
                    "namespace": "default",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                }
            }
        },
    )


def _actions() -> list[RemediationAction]:
    return [
        RemediationAction(
            action_id="undo-cartservice",
            action_type="rollout_undo_deployment",
            description="Undo the last cartservice rollout.",
            confidence_score=0.92,
            blast_radius_score=0.3,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        ),
        RemediationAction(
            action_id="restart-cartservice",
            action_type="rollout_restart_deployment",
            description="Restart cartservice deployment.",
            confidence_score=0.65,
            blast_radius_score=0.2,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        ),
    ]


class HITLGateTest(unittest.TestCase):
    def test_route_crashloop_plan_surfaces_single_recommended_action(self) -> None:
        decision = route_crashloop_plan(
            incident=_incident(),
            actions=_actions(),
            fixer_rationale="Undo is the highest-confidence bounded action.",
            judge_verdict="pass",
            judge_reason="Plan is bounded and approval-gated.",
        )

        self.assertEqual(decision.routing_decision, "request_approval_single_action")
        self.assertTrue(decision.requires_approval)
        self.assertIsNotNone(decision.recommended_action)
        self.assertEqual(decision.recommended_action.action_id, "undo-cartservice")
        self.assertEqual(decision.candidate_actions[0].action_id, "undo-cartservice")

        trace = decision.decision_trace
        self.assertEqual(trace.incident_id, "trace-123")
        self.assertEqual(trace.judge_verdict, "pass")
        self.assertEqual(trace.routing_decision, "request_approval_single_action")
        self.assertEqual(trace.human_approval, "n/a")
        self.assertEqual(trace.final_state, "pending_approval")
        self.assertIn("actions", trace.fixer_plan)
        self.assertEqual(trace.execution_result, {})
        self.assertEqual(trace.verification_result, {})

    def test_route_crashloop_plan_halts_when_judge_fails(self) -> None:
        decision = route_crashloop_plan(
            incident=_incident(),
            actions=_actions(),
            fixer_rationale="Undo is the highest-confidence bounded action.",
            judge_verdict="fail",
            judge_reason="Judge blocked the plan.",
        )

        self.assertEqual(decision.routing_decision, "halt")
        self.assertTrue(decision.requires_approval)
        self.assertEqual(decision.decision_trace.routing_decision, "halt")
        self.assertEqual(decision.decision_trace.judge_reason, "Judge blocked the plan.")

    def test_decision_trace_can_record_approval_and_finalize(self) -> None:
        decision = route_crashloop_plan(
            incident=_incident(),
            actions=_actions(),
            fixer_rationale="Undo is the highest-confidence bounded action.",
            judge_verdict="pass",
            judge_reason="Plan is bounded and approval-gated.",
        )

        approved = record_human_approval(
            decision.decision_trace,
            human_approval="approved",
            final_state="executing",
        )
        finalized = finalize_decision_trace(
            approved,
            execution_result={"status": "succeeded"},
            verification_result={"status": "recovered"},
            final_state="recovered",
        )

        self.assertEqual(finalized.human_approval, "approved")
        self.assertEqual(finalized.execution_result["status"], "succeeded")
        self.assertEqual(finalized.verification_result["status"], "recovered")
        self.assertEqual(finalized.final_state, "recovered")


if __name__ == "__main__":
    unittest.main()
