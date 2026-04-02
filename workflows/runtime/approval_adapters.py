from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from schemas.approval import ApprovalCandidate, approval_candidate_from_dict
from schemas.execution_plan import ExecutionPlan, ExecutionPlanStep
from schemas.intents import ResourceTarget
from schemas.remediation import RemediationAction
from services.recovery.kubectl_compiler import compile_v1_dispatch_preview
from workflows.hitl_gate import HITLDecision


def approval_candidate_from_saved(value: Any) -> ApprovalCandidate | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError("saved approval candidate must be an object")
    return approval_candidate_from_dict(value)


def remediation_action_from_saved(value: Any) -> RemediationAction | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError("saved remediation action must be an object")
    return RemediationAction(
        action_id=str(value["action_id"]),
        action_type=value["action_type"],
        description=str(value["description"]),
        confidence_score=float(value["confidence_score"]),
        blast_radius_score=float(value["blast_radius_score"]),
        requires_approval=bool(value["requires_approval"]),
        parameters=dict(value["parameters"]),
    )


def serialize_remediation_action(action: RemediationAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "description": action.description,
        "confidence_score": action.confidence_score,
        "blast_radius_score": action.blast_radius_score,
        "requires_approval": action.requires_approval,
        "parameters": dict(action.parameters),
    }


def deployment_for_action(action: RemediationAction) -> str:
    if "deployment" in action.parameters:
        return str(action.parameters["deployment"])
    if action.action_type == "delete_networkchaos":
        return "cartservice"
    return "frontend"


def action_target_label(action: RemediationAction) -> str:
    if action.action_type == "delete_stresschaos":
        return f"StressChaos {str(action.parameters['name'])!r}"
    if action.action_type == "delete_networkchaos":
        return f"NetworkChaos {str(action.parameters['name'])!r}"
    if action.action_type == "scale_deployment":
        return (
            f"deployment {deployment_for_action(action)!r} "
            f"-> replicas {int(action.parameters.get('replicas', 1) or 1)}"
        )
    return f"deployment {deployment_for_action(action)!r}"


def dispatch_parameters_match_action(
    *,
    dispatch_parameters: dict[str, Any],
    action: RemediationAction,
) -> bool:
    if action.action_type == "scale_deployment":
        expected = {
            "namespace": str(action.parameters["namespace"]),
            "deployment": str(action.parameters["deployment"]),
            "replicas": int(action.parameters["replicas"]),
        }
        return dispatch_parameters == expected

    if action.action_type in {"delete_stresschaos", "delete_networkchaos"}:
        expected = {
            "namespace": str(action.parameters["namespace"]),
            "name": str(action.parameters["name"]),
        }
        return dispatch_parameters == expected

    expected = {
        "namespace": str(action.parameters["namespace"]),
        "deployment": str(action.parameters["deployment"]),
    }
    return dispatch_parameters == expected


def candidate_display_labels(plan: ExecutionPlan) -> list[str]:
    labels = [plan.operation_family]
    if plan.target.namespace:
        labels.append(plan.target.namespace)
    if plan.target.name:
        labels.append(plan.target.name)
    return labels


def legacy_action_hint_for_plan(
    plan: ExecutionPlan,
    mapped_v1_candidates: list[Any],
) -> dict[str, Any] | None:
    dispatch_preview = compile_v1_dispatch_preview(plan)
    for value in mapped_v1_candidates:
        if not isinstance(value, RemediationAction):
            continue
        if value.action_type not in (
            "rollout_undo_deployment",
            "rollout_restart_deployment",
            "scale_deployment",
            "delete_stresschaos",
            "delete_networkchaos",
        ):
            continue
        if dispatch_preview.get("action_type") != value.action_type:
            continue
        if not dispatch_parameters_match_action(
            dispatch_parameters=dict(dispatch_preview.get("parameters", {})),
            action=value,
        ):
            continue
        return serialize_remediation_action(value)
    if not bool(dispatch_preview.get("executable")):
        return None
    return {
        "action_id": plan.intent_id,
        "action_type": dispatch_preview["action_type"],
        "description": plan.summary,
        "confidence_score": plan.blast_radius_score,
        "blast_radius_score": plan.blast_radius_score,
        "requires_approval": plan.requires_approval,
        "parameters": dict(dispatch_preview["parameters"]),
    }


def select_candidate(hitl_decision: HITLDecision, candidate_id: str) -> ApprovalCandidate:
    for candidate in hitl_decision.candidate_options:
        if candidate.candidate_id == candidate_id:
            return candidate
        legacy_action_hint = candidate.legacy_action_hint or {}
        if str(legacy_action_hint.get("action_id") or "") == candidate_id:
            return candidate
    raise ValueError(f"Approved candidate_id {candidate_id!r} is not available in the HITL decision.")


def legacy_action_for_candidate(candidate: ApprovalCandidate) -> RemediationAction | None:
    legacy_action_hint = candidate.legacy_action_hint
    if legacy_action_hint is None:
        return None
    return remediation_action_from_saved(legacy_action_hint)


def upgrade_candidate_to_action_fallback(candidate: ApprovalCandidate) -> RemediationAction:
    legacy_action = legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return legacy_action
    parameters = dispatch_parameters_for_candidate(candidate)
    return RemediationAction(
        action_id=candidate.candidate_id,
        action_type=candidate_action_type(candidate),
        description=candidate.summary,
        confidence_score=candidate.confidence_score,
        blast_radius_score=candidate.blast_radius_score,
        requires_approval=candidate.requires_approval,
        parameters=parameters,
    )


def upgrade_legacy_action_to_candidate(action: RemediationAction) -> ApprovalCandidate:
    plan = execution_plan_from_legacy_action(action)
    return ApprovalCandidate(
        candidate_id=action.action_id,
        summary=action.description,
        confidence_score=action.confidence_score,
        blast_radius_score=action.blast_radius_score,
        requires_approval=action.requires_approval,
        execution_plan=plan,
        display_labels=[action.action_type, action_target_label(action)],
        legacy_action_hint=serialize_remediation_action(action),
    )


def execution_plan_from_legacy_action(action: RemediationAction) -> ExecutionPlan:
    namespace = str(action.parameters.get("namespace") or "default")
    if action.action_type == "delete_stresschaos":
        name = str(action.parameters["name"])
        return ExecutionPlan(
            intent_id=action.action_id,
            operation_family="chaos.delete_stresschaos",
            target=ResourceTarget(namespace=namespace, kind="StressChaos", name=name),
            summary=action.description,
            steps=[
                ExecutionPlanStep(
                    step_id=f"{action.action_id}:step-1",
                    tool_name="delete_stresschaos",
                    command=["kubectl", "delete", "stresschaos", name, "-n", namespace],
                    expected_effect="Delete the approved StressChaos object.",
                    reversible=True,
                    verification_hints={"pre_check": "cpu_saturation", "post_check": "cpu_saturation"},
                )
            ],
            allowed_tool_names=["get_stresschaos", "delete_stresschaos"],
            blast_radius_score=action.blast_radius_score,
            requires_approval=action.requires_approval,
            rollback_outline={},
        )
    if action.action_type == "delete_networkchaos":
        name = str(action.parameters["name"])
        return ExecutionPlan(
            intent_id=action.action_id,
            operation_family="chaos.delete_networkchaos",
            target=ResourceTarget(namespace=namespace, kind="NetworkChaos", name=name),
            summary=action.description,
            steps=[
                ExecutionPlanStep(
                    step_id=f"{action.action_id}:step-1",
                    tool_name="delete_networkchaos",
                    command=["kubectl", "delete", "networkchaos", name, "-n", namespace],
                    expected_effect="Delete the approved NetworkChaos object.",
                    reversible=True,
                    verification_hints={"pre_check": "network_partition", "post_check": "network_partition"},
                )
            ],
            allowed_tool_names=["get_networkchaos", "delete_networkchaos"],
            blast_radius_score=action.blast_radius_score,
            requires_approval=action.requires_approval,
            rollback_outline={},
        )
    if action.action_type == "scale_deployment":
        deployment = str(action.parameters["deployment"])
        replicas = int(action.parameters.get("replicas", 1) or 1)
        return ExecutionPlan(
            intent_id=action.action_id,
            operation_family="scale.deployment",
            target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment),
            summary=action.description,
            steps=[
                ExecutionPlanStep(
                    step_id=f"{action.action_id}:step-1",
                    tool_name="scale_deployment",
                    command=[
                        "kubectl",
                        "scale",
                        f"deployment/{deployment}",
                        "-n",
                        namespace,
                        f"--replicas={replicas}",
                    ],
                    expected_effect="Scale the approved Deployment to the bounded replica target.",
                    reversible=True,
                    verification_hints={
                        "pre_check": "deployment_readiness_shortfall",
                        "post_check": "deployment_readiness_shortfall",
                        "target_replicas": replicas,
                        "min_ready_count": replicas,
                    },
                )
            ],
            allowed_tool_names=["get_deployment_context", "get_rollout_status", "scale_deployment"],
            blast_radius_score=action.blast_radius_score,
            requires_approval=action.requires_approval,
            rollback_outline={"previous_replicas_unknown": True},
        )
    if action.action_type == "escalate":
        return ExecutionPlan(
            intent_id=action.action_id,
            operation_family="escalate.human_review",
            target=ResourceTarget(namespace=namespace, kind="Incident", name=action.action_id),
            summary=action.description,
            steps=[],
            allowed_tool_names=[],
            blast_radius_score=action.blast_radius_score,
            requires_approval=action.requires_approval,
            rollback_outline={},
        )

    deployment = str(action.parameters["deployment"])
    operation_family = "rollout.undo_deployment"
    tool_name = "rollout_undo_deployment"
    command = ["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", namespace]
    verification_hints = {"pre_check": "crashloop", "post_check": "crashloop"}
    if action.action_id == "rollout_undo_frontend_bad_config":
        verification_hints = {"pre_check": "bad_config", "post_check": "bad_config"}
    if action.action_type == "rollout_restart_deployment":
        operation_family = "rollout.restart_deployment"
        tool_name = "rollout_restart_deployment"
        command = ["kubectl", "rollout", "restart", f"deployment/{deployment}", "-n", namespace]
    return ExecutionPlan(
        intent_id=action.action_id,
        operation_family=operation_family,
        target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment),
        summary=action.description,
        steps=[
            ExecutionPlanStep(
                step_id=f"{action.action_id}:step-1",
                tool_name=tool_name,
                command=command,
                expected_effect="Execute the approved rollout step.",
                reversible=True,
                verification_hints=verification_hints,
            )
        ],
        allowed_tool_names=["get_deployment_context", "get_rollout_status", tool_name],
        blast_radius_score=action.blast_radius_score,
        requires_approval=action.requires_approval,
        rollback_outline={"preferred_rollback": "rollout.undo_deployment"},
    )


def candidate_action_type(candidate: ApprovalCandidate) -> str:
    if not candidate.execution_plan.steps:
        return "escalate"
    return candidate.execution_plan.steps[0].tool_name


def dispatch_parameters_for_candidate(candidate: ApprovalCandidate) -> dict[str, Any]:
    legacy_action = legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return dict(legacy_action.parameters)
    preview = compile_v1_dispatch_preview(candidate.execution_plan)
    return dict(preview.get("parameters", {}))


def candidate_namespace(candidate: ApprovalCandidate) -> str:
    if candidate.execution_plan.target.namespace:
        return str(candidate.execution_plan.target.namespace)
    legacy_action = legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return str(legacy_action.parameters.get("namespace") or "default")
    return "default"


def candidate_deployment(candidate: ApprovalCandidate) -> str:
    legacy_action = legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return deployment_for_action(legacy_action)
    if candidate.execution_plan.operation_family == "chaos.delete_stresschaos":
        return "frontend"
    if candidate.execution_plan.operation_family == "chaos.delete_networkchaos":
        return "cartservice"
    return str(candidate.execution_plan.target.name or "unknown")


def candidate_target_name(candidate: ApprovalCandidate) -> str:
    if candidate.execution_plan.target.name:
        return str(candidate.execution_plan.target.name)
    legacy_action = legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return action_target_label(legacy_action)
    return candidate.candidate_id


def candidate_check_hint(candidate: ApprovalCandidate) -> str:
    if candidate.execution_plan.steps:
        return str(candidate.execution_plan.steps[0].verification_hints.get("post_check") or "")
    return ""
