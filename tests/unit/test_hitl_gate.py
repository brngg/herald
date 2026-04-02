from __future__ import annotations

from datetime import datetime, timezone
import unittest

from schemas.approval import ApprovalCandidate
from schemas.incident import Incident
from schemas.execution_plan import ExecutionPlan, ExecutionPlanStep
from schemas.intents import ResourceTarget
from schemas.remediation import RemediationAction
from workflows.hitl_gate import (
    finalize_decision_trace,
    record_human_approval,
    route_candidates,
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


def _candidates() -> list[ApprovalCandidate]:
    return [
        ApprovalCandidate(
            candidate_id="candidate-undo-cartservice",
            summary="Undo the cartservice rollout with an exact execution plan.",
            confidence_score=0.92,
            blast_radius_score=0.3,
            requires_approval=True,
            execution_plan=ExecutionPlan(
                intent_id="candidate-undo-cartservice",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                summary="Undo the cartservice rollout with an exact execution plan.",
                steps=[
                    ExecutionPlanStep(
                        step_id="undo-step",
                        tool_name="rollout_undo_deployment",
                        command=["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
                        expected_effect="Restore the previous cartservice ReplicaSet.",
                        reversible=True,
                        verification_hints={"pre_check": "crashloop", "post_check": "crashloop"},
                    )
                ],
                allowed_tool_names=["rollout_undo_deployment"],
                blast_radius_score=0.3,
                requires_approval=True,
                rollback_outline={},
            ),
            display_labels=["rollout.undo_deployment", "default", "cartservice"],
            legacy_action_hint={"action_id": "undo-cartservice", "action_type": "rollout_undo_deployment"},
        ),
        ApprovalCandidate(
            candidate_id="candidate-escalate-cartservice",
            summary="Escalate the incident for human investigation.",
            confidence_score=0.2,
            blast_radius_score=0.0,
            requires_approval=True,
            execution_plan=ExecutionPlan(
                intent_id="candidate-escalate-cartservice",
                operation_family="escalate.human_review",
                target=ResourceTarget(namespace="default", kind="Incident", name="trace-123"),
                summary="Escalate the incident for human investigation.",
                steps=[],
                allowed_tool_names=[],
                blast_radius_score=0.0,
                requires_approval=True,
                rollback_outline={},
            ),
            display_labels=["escalate.human_review", "trace-123"],
            legacy_action_hint=None,
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

    def test_route_candidates_surfaces_single_recommended_candidate(self) -> None:
        decision = route_candidates(
            incident=_incident(),
            candidates=_candidates(),
            planner_summary="Undo is the best exact execution plan.",
            judge_verdict="pass",
            judge_reason="Critic approved the bounded exact plan.",
        )

        self.assertEqual(decision.routing_decision, "request_approval_single_action")
        self.assertTrue(decision.requires_approval)
        self.assertIsNotNone(decision.recommended_candidate)
        self.assertEqual(decision.recommended_candidate.candidate_id, "candidate-undo-cartservice")
        self.assertEqual(decision.candidate_options[0].candidate_id, "candidate-undo-cartservice")
        self.assertNotIn("actions", decision.decision_trace.fixer_plan)
        self.assertIn("candidate_options", decision.decision_trace.fixer_plan)
        self.assertEqual(
            decision.decision_trace.fixer_plan["candidate_options"][0]["candidate_id"],
            "candidate-undo-cartservice",
        )

    def test_route_candidates_uses_ranked_options_for_non_executable_escalation(self) -> None:
        escalation_only = [_candidates()[1]]

        decision = route_candidates(
            incident=_incident(),
            candidates=escalation_only,
            planner_summary="Only escalation is safe.",
            judge_verdict="pass",
            judge_reason="Critic blocked executable recovery plans.",
        )

        self.assertEqual(decision.routing_decision, "request_approval_ranked_options")
        self.assertIsNotNone(decision.recommended_candidate)
        self.assertFalse(decision.recommended_candidate.execution_plan.steps)

    def test_route_crashloop_plan_halts_when_judge_fails(self) -> None:
        decision = route_crashloop_plan(
            incident=_incident(),
            actions=_actions(),
            fixer_rationale="Undo is the highest-confidence bounded action.",
            judge_verdict="fail",
            judge_reason="Judge blocked the plan.",
        )

        self.assertEqual(decision.routing_decision, "halt")
        self.assertFalse(decision.requires_approval)
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
