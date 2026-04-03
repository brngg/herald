from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from schemas.decision_trace import DecisionTrace
from schemas.observations import ObservationBundle, observation_bundle_from_dict
from workflows.hitl_gate import HITLDecision
from workflows.runtime.approval_adapters import (
    approval_candidate_from_saved,
    remediation_action_from_saved,
    upgrade_legacy_action_to_candidate,
)


def saved_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError(f"saved {field_name} must be an object")
    return value


def saved_observation_bundle(value: Any) -> ObservationBundle | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError("saved observation_bundle must be an object")
    return observation_bundle_from_dict(value)


def saved_optional_mapping(value: Any, *, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError(f"saved {field_name} must be an object")
    return value


def decision_trace_from_saved(value: dict[str, Any]) -> DecisionTrace:
    return DecisionTrace(
        incident_id=str(value["incident_id"]),
        fixer_plan=dict(value["fixer_plan"]),
        judge_verdict=value["judge_verdict"],
        judge_reason=str(value["judge_reason"]),
        routing_decision=str(value["routing_decision"]),
        human_approval=value["human_approval"],
        execution_result=dict(value["execution_result"]),
        verification_result=dict(value["verification_result"]),
        rollback_triggered=bool(value["rollback_triggered"]),
        final_state=value["final_state"],
        node_runs_by_node=dict(value.get("node_runs_by_node", {})),
        latest_run_id_by_node=dict(value.get("latest_run_id_by_node", {})),
    )


def hitl_decision_from_saved(
    value: dict[str, Any],
    *,
    decision_trace_payload: dict[str, Any],
) -> HITLDecision:
    decision_trace = decision_trace_from_saved(decision_trace_payload)
    if "recommended_candidate" in value or "candidate_options" in value:
        return HITLDecision(
            routing_decision=str(value["routing_decision"]),
            requires_approval=bool(value["requires_approval"]),
            recommended_candidate=approval_candidate_from_saved(value.get("recommended_candidate")),
            candidate_options=[
                approval_candidate_from_saved(candidate_payload)
                for candidate_payload in list(value.get("candidate_options", []))
            ],
            decision_trace=decision_trace,
        )

    recommended_action = remediation_action_from_saved(value.get("recommended_action"))
    candidate_actions = [
        remediation_action_from_saved(action_payload)
        for action_payload in list(value.get("candidate_actions", []))
    ]
    candidate_options = [
        upgrade_legacy_action_to_candidate(action)
        for action in candidate_actions
    ]
    recommended_candidate = None
    if recommended_action is not None:
        recommended_candidate = upgrade_legacy_action_to_candidate(recommended_action)
    return HITLDecision(
        routing_decision=str(value["routing_decision"]),
        requires_approval=bool(value["requires_approval"]),
        recommended_candidate=recommended_candidate,
        candidate_options=candidate_options,
        decision_trace=decision_trace,
    )
