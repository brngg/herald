from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from schemas.approval import ApprovalCandidate
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
    decision_trace: DecisionTrace
    recommended_action: RemediationAction | None = None
    candidate_actions: list[RemediationAction] = field(default_factory=list)
    recommended_candidate: ApprovalCandidate | None = None
    candidate_options: list[ApprovalCandidate] = field(default_factory=list)


def route_plan(
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


def route_candidates(
    *,
    incident: Incident,
    candidates: list[ApprovalCandidate],
    planner_summary: str | None,
    judge_verdict: str,
    judge_reason: str,
) -> HITLDecision:
    ranked_candidates = _rank_candidates(candidates)
    recommended_candidate = ranked_candidates[0] if ranked_candidates else None

    if judge_verdict != "pass":
        routing_decision = "halt"
    elif not ranked_candidates:
        routing_decision = "halt"
    elif recommended_candidate.blast_radius_score >= BLAST_RADIUS_BLOCK_THRESHOLD:
        routing_decision = "request_approval_ranked_options"
    elif not recommended_candidate.execution_plan.steps:
        routing_decision = "request_approval_ranked_options"
    elif (
        recommended_candidate.confidence_score >= CONFIDENCE_PROMOTE_THRESHOLD
        and recommended_candidate.blast_radius_score < BLAST_RADIUS_WARN_THRESHOLD
    ):
        routing_decision = "request_approval_single_action"
    elif recommended_candidate.confidence_score < CONFIDENCE_HIDE_THRESHOLD:
        routing_decision = "request_approval_ranked_options"
    else:
        routing_decision = "request_approval_ranked_options"

    trace = DecisionTrace(
        incident_id=incident.incident_id,
        fixer_plan=_serialize_candidate_plan(ranked_candidates, planner_summary),
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
        requires_approval=routing_decision != "halt" and bool(ranked_candidates),
        recommended_candidate=recommended_candidate,
        candidate_options=ranked_candidates,
        decision_trace=trace,
    )


def route_crashloop_plan(
    *,
    incident: Incident,
    actions: list[RemediationAction],
    fixer_rationale: str | None,
    judge_verdict: str,
    judge_reason: str,
) -> HITLDecision:
    return route_plan(
        incident=incident,
        actions=actions,
        fixer_rationale=fixer_rationale,
        judge_verdict=judge_verdict,
        judge_reason=judge_reason,
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


def _rank_candidates(candidates: list[ApprovalCandidate]) -> list[ApprovalCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.confidence_score,
            candidate.blast_radius_score,
            candidate.candidate_id,
        ),
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


def _serialize_candidate_plan(
    candidates: list[ApprovalCandidate],
    planner_summary: str | None,
) -> dict[str, Any]:
    return {
        "candidate_options": [
            {
                "candidate_id": candidate.candidate_id,
                "summary": candidate.summary,
                "confidence_score": candidate.confidence_score,
                "blast_radius_score": candidate.blast_radius_score,
                "requires_approval": candidate.requires_approval,
                "execution_plan": {
                    "intent_id": candidate.execution_plan.intent_id,
                    "operation_family": candidate.execution_plan.operation_family,
                    "target": {
                        "namespace": candidate.execution_plan.target.namespace,
                        "kind": candidate.execution_plan.target.kind,
                        "name": candidate.execution_plan.target.name,
                        "selector": candidate.execution_plan.target.selector,
                    },
                    "summary": candidate.execution_plan.summary,
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "tool_name": step.tool_name,
                            "command": list(step.command),
                            "expected_effect": step.expected_effect,
                            "reversible": step.reversible,
                            "verification_hints": dict(step.verification_hints),
                        }
                        for step in candidate.execution_plan.steps
                    ],
                    "allowed_tool_names": list(candidate.execution_plan.allowed_tool_names),
                    "blast_radius_score": candidate.execution_plan.blast_radius_score,
                    "requires_approval": candidate.execution_plan.requires_approval,
                    "rollback_outline": dict(candidate.execution_plan.rollback_outline),
                },
                "display_labels": list(candidate.display_labels),
                "legacy_action_hint": dict(candidate.legacy_action_hint) if candidate.legacy_action_hint else None,
            }
            for candidate in candidates
        ],
        "planner_summary": planner_summary or "",
    }
