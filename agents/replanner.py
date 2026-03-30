from __future__ import annotations

from typing import Any, TypedDict

from schemas.decision_trace import DecisionTrace
from schemas.intents import OperationIntent, ReasonerOutput
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from schemas.replan import ReplanOutput


class ReplannerAgentState(TypedDict):
    replan_output: ReplanOutput | None
    status: str
    errors: list[str]
    final: bool
    failure_reason: str | None


def run_replanner_pipeline(
    *,
    incident: Any,
    observations: ObservationBundle | None,
    reasoner_output: ReasonerOutput | None,
    verifier_state: dict[str, Any] | None,
    trace: DecisionTrace,
    approved_action: RemediationAction | None,
) -> dict[str, Any]:
    verification_status = str((verifier_state or {}).get("verification_status", "not_run"))
    if verification_status != "unrecovered":
        return {
            "replan_output": None,
            "status": "not_run",
            "errors": [],
            "final": True,
            "failure_reason": None,
        }

    if approved_action is None:
        failure_reason = "Approved action was unavailable for shadow replanning."
        return {
            "replan_output": None,
            "status": "failed",
            "errors": [failure_reason],
            "final": True,
            "failure_reason": failure_reason,
        }

    if reasoner_output is None:
        failure_reason = "Reasoner output was unavailable for shadow replanning."
        return {
            "replan_output": None,
            "status": "failed",
            "errors": [failure_reason],
            "final": True,
            "failure_reason": failure_reason,
        }

    if trace.rollback_triggered or trace.final_state == "rolled_back":
        output = ReplanOutput(
            decision="escalate",
            rationale=(
                "A bounded rollback already ran after failed verification, so HERALD should escalate "
                "instead of proposing another automated mutation from the same evidence."
            ),
            intents=[],
            stop_reason="rollback_already_triggered",
        )
        return {
            "replan_output": output,
            "status": "succeeded",
            "errors": [],
            "final": True,
            "failure_reason": None,
        }

    alternative_intents = [
        intent
        for intent in reasoner_output.intents
        if _operation_family_for_action(approved_action) != intent.operation_family
    ]
    if not alternative_intents:
        output = ReplanOutput(
            decision="escalate",
            rationale=(
                "Shadow verification did not confirm recovery, and no bounded alternative intent remained "
                "after excluding the already executed action."
            ),
            intents=[],
            stop_reason="no_alternative_intents",
        )
        return {
            "replan_output": output,
            "status": "succeeded",
            "errors": [],
            "final": True,
            "failure_reason": None,
        }

    next_intent = alternative_intents[0]
    if next_intent.operation_family == "escalate.human_review":
        output = ReplanOutput(
            decision="escalate",
            rationale=(
                "Shadow verification remained unrecovered and the only bounded next step is human review."
            ),
            intents=[next_intent],
            stop_reason="human_review_only",
        )
    else:
        namespace = observations.namespace_hint if observations is not None else next_intent.target.namespace
        output = ReplanOutput(
            decision="propose_new_intent",
            rationale=(
                "Shadow verification remained unrecovered, so HERALD proposes the next bounded alternative "
                f"for namespace {namespace!r} instead of repeating the executed action."
            ),
            intents=[next_intent],
            stop_reason=None,
        )
    return {
        "replan_output": output,
        "status": "succeeded",
        "errors": [],
        "final": True,
        "failure_reason": None,
    }


def _operation_family_for_action(action: RemediationAction) -> str:
    if action.action_type == "rollout_undo_deployment":
        return "rollout.undo_deployment"
    if action.action_type == "rollout_restart_deployment":
        return "rollout.restart_deployment"
    if action.action_type == "delete_stresschaos":
        return "chaos.delete_stresschaos"
    if action.action_type == "delete_networkchaos":
        return "chaos.delete_networkchaos"
    return "escalate.human_review"
