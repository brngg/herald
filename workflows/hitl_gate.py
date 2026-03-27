from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from schemas.decision_trace import DecisionTrace, FinalState, HumanApproval
from schemas.incident import Incident
from schemas.remediation import RemediationAction


CONFIDENCE_PROMOTE_THRESHOLD = 0.85
CONFIDENCE_HIDE_THRESHOLD = 0.30
BLAST_RADIUS_WARN_THRESHOLD = 0.5
BLAST_RADIUS_BLOCK_THRESHOLD = 0.8


@dataclass(slots=True)
class HITLDecision:
    routing_decision: str
    requires_approval: bool
    recommended_action: RemediationAction | None
    candidate_actions: list[RemediationAction]
    decision_trace: DecisionTrace


def route_crashloop_plan(
    *,
    incident: Incident,
    actions: list[RemediationAction],
    fixer_rationale: str | None,
    judge_verdict: str,
    judge_reason: str,
) -> HITLDecision:
    ranked_actions = _rank_actions(actions)
    recommended_action = ranked_actions[0] if ranked_actions else None

    if judge_verdict != "pass":
        routing_decision = "halt"
    elif not ranked_actions:
        routing_decision = "halt"
    elif recommended_action.blast_radius_score >= BLAST_RADIUS_BLOCK_THRESHOLD:
        routing_decision = "request_approval_ranked_options"
    elif (
        recommended_action.confidence_score >= CONFIDENCE_PROMOTE_THRESHOLD
        and recommended_action.blast_radius_score < BLAST_RADIUS_WARN_THRESHOLD
    ):
        routing_decision = "request_approval_single_action"
    elif recommended_action.confidence_score < CONFIDENCE_HIDE_THRESHOLD:
        routing_decision = "request_approval_ranked_options"
    else:
        routing_decision = "request_approval_ranked_options"

    trace = DecisionTrace(
        incident_id=incident.incident_id,
        fixer_plan=_serialize_fixer_plan(ranked_actions, fixer_rationale),
        judge_verdict=judge_verdict,
        judge_reason=judge_reason,
        routing_decision=routing_decision,
        human_approval="n/a",
        execution_result={},
        verification_result={},
        rollback_triggered=False,
        final_state="pending_approval",
    )

    return HITLDecision(
        routing_decision=routing_decision,
        requires_approval=routing_decision != "halt" and bool(ranked_actions),
        recommended_action=recommended_action,
        candidate_actions=ranked_actions,
        decision_trace=trace,
    )


def record_human_approval(
    trace: DecisionTrace,
    *,
    human_approval: HumanApproval,
    final_state: FinalState | None = None,
) -> DecisionTrace:
    return replace(
        trace,
        human_approval=human_approval,
        final_state=final_state or trace.final_state,
    )


def finalize_decision_trace(
    trace: DecisionTrace,
    *,
    execution_result: dict[str, Any],
    verification_result: dict[str, Any],
    final_state: FinalState,
    rollback_triggered: bool = False,
) -> DecisionTrace:
    return replace(
        trace,
        execution_result=execution_result,
        verification_result=verification_result,
        rollback_triggered=rollback_triggered,
        final_state=final_state,
    )


def _rank_actions(actions: list[RemediationAction]) -> list[RemediationAction]:
    return sorted(
        actions,
        key=lambda action: (-action.confidence_score, action.blast_radius_score, action.action_id),
    )


def _serialize_fixer_plan(
    actions: list[RemediationAction],
    fixer_rationale: str | None,
) -> dict[str, Any]:
    return {
        "actions": [
            {
                "action_id": action.action_id,
                "action_type": action.action_type,
                "description": action.description,
                "confidence_score": action.confidence_score,
                "blast_radius_score": action.blast_radius_score,
                "requires_approval": action.requires_approval,
                "parameters": action.parameters,
            }
            for action in actions
        ],
        "fixer_rationale": fixer_rationale or "",
    }
