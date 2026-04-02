from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from schemas.execution_plan import ExecutionPlan, ExecutionPlanStep
from schemas.intents import OperationIntent

SAFE_SCALE_REPLICA_MIN = 1
SAFE_SCALE_REPLICA_MAX = 3


def compile_execution_plan(intent: OperationIntent) -> ExecutionPlan | None:
    action_type, command, allowed_tool_names, expected_effect, summary = _compile_command(intent)
    if action_type is None:
        if intent.operation_family == "escalate.human_review":
            return _build_escalation_plan(intent)
        return None

    verification_hints = dict(intent.verification_hints)
    if intent.operation_family == "scale.deployment":
        replicas = intent.arguments.get("replicas")
        if isinstance(replicas, int) and not isinstance(replicas, bool):
            verification_hints.setdefault("target_replicas", replicas)
            verification_hints.setdefault("min_ready_count", replicas)

    step = ExecutionPlanStep(
        step_id=f"{intent.intent_id}:step-1",
        tool_name=action_type,
        command=command,
        expected_effect=expected_effect,
        reversible=intent.reversible,
        verification_hints=verification_hints,
    )
    return ExecutionPlan(
        intent_id=intent.intent_id,
        operation_family=intent.operation_family,
        target=intent.target,
        summary=summary,
        steps=[step],
        allowed_tool_names=allowed_tool_names,
        blast_radius_score=intent.blast_radius_score,
        requires_approval=intent.requires_approval,
        rollback_outline=dict(intent.rollback_hints),
    )


def compile_v1_dispatch_preview(plan: ExecutionPlan) -> dict[str, Any]:
    if not plan.steps:
        return {
            "intent_id": plan.intent_id,
            "operation_family": plan.operation_family,
            "action_type": "escalate",
            "parameters": _escalation_parameters(plan),
            "allowed_tool_names": [],
            "command": [],
            "executable": False,
            "blast_radius_score": plan.blast_radius_score,
            "requires_approval": plan.requires_approval,
            "summary": plan.summary,
        }

    action_type = plan.steps[0].tool_name
    return {
        "intent_id": plan.intent_id,
        "operation_family": plan.operation_family,
        "action_type": action_type,
        "parameters": _dispatch_parameters(plan),
        "allowed_tool_names": list(plan.allowed_tool_names),
        "command": list(plan.steps[0].command),
        "executable": True,
        "blast_radius_score": plan.blast_radius_score,
        "requires_approval": plan.requires_approval,
        "summary": plan.summary,
    }


def compile_intent_summary(intent: OperationIntent) -> str:
    plan = compile_execution_plan(intent)
    if plan is None:
        return f"Unsupported intent {intent.intent_id} ({intent.operation_family})."
    return plan.summary


def _compile_command(
    intent: OperationIntent,
) -> tuple[str | None, list[str], list[str], str, str]:
    namespace = intent.target.namespace
    name = intent.target.name
    if intent.operation_family == "rollout.undo_deployment":
        if namespace is None or name is None:
            return None, [], [], "", ""
        command = [
            "kubectl",
            "rollout",
            "undo",
            f"deployment/{name}",
            "-n",
            namespace,
        ]
        return (
            "rollout_undo_deployment",
            command,
            ["get_deployment_context", "get_rollout_status", "rollout_undo_deployment"],
            "Undo the approved Deployment rollout.",
            f"Shadow execution plan to roll back Deployment {name} in namespace {namespace}.",
        )

    if intent.operation_family == "rollout.restart_deployment":
        if namespace is None or name is None:
            return None, [], [], "", ""
        command = [
            "kubectl",
            "rollout",
            "restart",
            f"deployment/{name}",
            "-n",
            namespace,
        ]
        return (
            "rollout_restart_deployment",
            command,
            ["get_deployment_context", "get_rollout_status", "rollout_restart_deployment"],
            "Restart the approved Deployment rollout.",
            f"Shadow execution plan to restart Deployment {name} in namespace {namespace}.",
        )

    if intent.operation_family == "scale.deployment":
        if namespace is None or name is None:
            return None, [], [], "", ""
        replicas = intent.arguments.get("replicas")
        if not isinstance(replicas, int) or isinstance(replicas, bool):
            return None, [], [], "", ""
        if replicas < SAFE_SCALE_REPLICA_MIN or replicas > SAFE_SCALE_REPLICA_MAX:
            return None, [], [], "", ""
        command = [
            "kubectl",
            "scale",
            f"deployment/{name}",
            "-n",
            namespace,
            f"--replicas={replicas}",
        ]
        return (
            "scale_deployment",
            command,
            ["get_deployment_context", "get_rollout_status", "scale_deployment"],
            "Scale the approved Deployment to the bounded replica target.",
            (
                f"Shadow execution plan to scale Deployment {name} in namespace {namespace} "
                f"to {replicas} replicas."
            ),
        )

    if intent.operation_family == "chaos.delete_stresschaos":
        if namespace is None or name is None:
            return None, [], [], "", ""
        command = [
            "kubectl",
            "delete",
            "stresschaos",
            name,
            "-n",
            namespace,
        ]
        return (
            "delete_stresschaos",
            command,
            ["get_stresschaos", "delete_stresschaos"],
            "Delete the approved StressChaos object.",
            f"Shadow execution plan to delete StressChaos {name} in namespace {namespace}.",
        )

    if intent.operation_family == "chaos.delete_networkchaos":
        if namespace is None or name is None:
            return None, [], [], "", ""
        command = [
            "kubectl",
            "delete",
            "networkchaos",
            name,
            "-n",
            namespace,
        ]
        return (
            "delete_networkchaos",
            command,
            ["get_networkchaos", "delete_networkchaos"],
            "Delete the approved NetworkChaos object.",
            f"Shadow execution plan to delete NetworkChaos {name} in namespace {namespace}.",
        )

    return None, [], [], "", ""


def _build_escalation_plan(intent: OperationIntent) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id=intent.intent_id,
        operation_family=intent.operation_family,
        target=intent.target,
        summary=f"Non-executable shadow plan for human review: {intent.intent}.",
        steps=[],
        allowed_tool_names=[],
        blast_radius_score=intent.blast_radius_score,
        requires_approval=intent.requires_approval,
        rollback_outline=dict(intent.rollback_hints),
    )


def _dispatch_parameters(plan: ExecutionPlan) -> dict[str, Any]:
    target = plan.target
    parameters: dict[str, Any] = {}
    if target.namespace is not None:
        parameters["namespace"] = target.namespace
    if target.name is not None:
        if plan.operation_family in {"chaos.delete_stresschaos", "chaos.delete_networkchaos"}:
            parameters["name"] = target.name
        else:
            parameters["deployment"] = target.name
    if plan.operation_family == "scale.deployment" and plan.steps:
        replicas = plan.steps[0].verification_hints.get("target_replicas")
        if isinstance(replicas, int) and not isinstance(replicas, bool):
            parameters["replicas"] = replicas
        else:
            replicas = plan.steps[0].verification_hints.get("min_ready_count")
            if isinstance(replicas, int) and not isinstance(replicas, bool):
                parameters["replicas"] = replicas
    return parameters


def _escalation_parameters(plan: ExecutionPlan) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if plan.target.namespace is not None:
        parameters["namespace"] = plan.target.namespace
    if plan.target.name is not None:
        parameters["name"] = plan.target.name
    return parameters


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
