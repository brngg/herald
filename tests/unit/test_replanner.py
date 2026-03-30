from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agents.replanner import run_replanner_pipeline
from schemas.decision_trace import DecisionTrace
from schemas.incident import Incident
from schemas.intents import OperationIntent, ReasonerOutput, ResourceTarget
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from schemas.replan import ReplanOutput, replan_output_from_dict


def _incident() -> Incident:
    return Incident(
        incident_id="incident-123",
        incident_class="crashloop",
        detected_at=datetime(2026, 3, 29, 20, 0, tzinfo=UTC),
        source="prometheus",
        raw_context={"labels": {"namespace": "default"}},
    )


def _observations() -> ObservationBundle:
    return ObservationBundle(
        incident_id="incident-123",
        incident_class_hint="crashloop",
        namespace_hint="default",
        source="prometheus",
        alert_context={"labels": {"namespace": "default"}},
        kubernetes={},
        prometheus={},
        collected_at="2026-03-29T20:00:00+00:00",
    )


def _reasoner_output() -> ReasonerOutput:
    return ReasonerOutput(
        diagnosis_summary="cartservice is crash looping",
        likely_causes=["bad rollout"],
        missing_information=[],
        intents=[
            OperationIntent(
                intent_id="undo",
                intent="Undo the rollout",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                arguments={},
                reversible=True,
                confidence_score=0.9,
                blast_radius_score=0.3,
                requires_approval=True,
                verification_hints={"post_check": "crashloop"},
                rollback_hints={},
            ),
            OperationIntent(
                intent_id="restart",
                intent="Restart the deployment",
                operation_family="rollout.restart_deployment",
                target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                arguments={},
                reversible=True,
                confidence_score=0.5,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={"post_check": "crashloop"},
                rollback_hints={},
            ),
        ],
    )


def _approved_action(action_type: str) -> RemediationAction:
    parameters = {"namespace": "default", "deployment": "cartservice"}
    return RemediationAction(
        action_id=f"action-{action_type}",
        action_type=action_type,
        description=action_type,
        confidence_score=0.9,
        blast_radius_score=0.3,
        requires_approval=True,
        parameters=parameters,
    )


def _trace(*, final_state: str = "escalated", rollback_triggered: bool = False) -> DecisionTrace:
    return DecisionTrace(
        incident_id="incident-123",
        fixer_plan={"actions": [], "fixer_rationale": ""},
        judge_verdict="pass",
        judge_reason="ok",
        routing_decision="approve_required",
        human_approval="approved",
        execution_result={"status": "succeeded", "action_id": "action-rollout_undo_deployment"},
        verification_result={"post_check": {"status": "unrecovered"}},
        rollback_triggered=rollback_triggered,
        final_state=final_state,  # type: ignore[arg-type]
    )


class ReplanSchemaTest(unittest.TestCase):
    def test_replan_output_from_dict_round_trips(self) -> None:
        payload = {
            "decision": "propose_new_intent",
            "rationale": "Try the alternate bounded action.",
            "intents": [
                {
                    "intent_id": "restart",
                    "intent": "Restart the deployment",
                    "operation_family": "rollout.restart_deployment",
                    "target": {"namespace": "default", "kind": "Deployment", "name": "cartservice"},
                    "arguments": {},
                    "reversible": True,
                    "confidence_score": 0.5,
                    "blast_radius_score": 0.2,
                    "requires_approval": True,
                    "verification_hints": {"post_check": "crashloop"},
                    "rollback_hints": {},
                }
            ],
        }

        output = replan_output_from_dict(payload)

        self.assertIsInstance(output, ReplanOutput)
        self.assertEqual(output.decision, "propose_new_intent")
        self.assertEqual(output.intents[0].intent_id, "restart")


class ReplannerTest(unittest.TestCase):
    def test_replanner_is_not_run_when_verification_passed(self) -> None:
        state = run_replanner_pipeline(
            incident=_incident(),
            observations=_observations(),
            reasoner_output=_reasoner_output(),
            verifier_state={"verification_status": "passed"},
            trace=_trace(),
            approved_action=_approved_action("rollout_undo_deployment"),
        )

        self.assertEqual(state["status"], "not_run")
        self.assertIsNone(state["replan_output"])

    def test_replanner_proposes_alternative_intent_after_unrecovered_verification(self) -> None:
        state = run_replanner_pipeline(
            incident=_incident(),
            observations=_observations(),
            reasoner_output=_reasoner_output(),
            verifier_state={"verification_status": "unrecovered"},
            trace=_trace(),
            approved_action=_approved_action("rollout_undo_deployment"),
        )

        self.assertEqual(state["status"], "succeeded")
        assert state["replan_output"] is not None
        self.assertEqual(state["replan_output"].decision, "propose_new_intent")
        self.assertEqual(state["replan_output"].intents[0].intent_id, "restart")

    def test_replanner_escalates_after_bounded_rollback(self) -> None:
        state = run_replanner_pipeline(
            incident=_incident(),
            observations=_observations(),
            reasoner_output=_reasoner_output(),
            verifier_state={"verification_status": "unrecovered"},
            trace=_trace(final_state="rolled_back", rollback_triggered=True),
            approved_action=_approved_action("rollout_restart_deployment"),
        )

        self.assertEqual(state["status"], "succeeded")
        assert state["replan_output"] is not None
        self.assertEqual(state["replan_output"].decision, "escalate")
        self.assertEqual(state["replan_output"].stop_reason, "rollback_already_triggered")


if __name__ == "__main__":
    unittest.main()
