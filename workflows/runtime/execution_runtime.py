from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from schemas.approval import ApprovalCandidate
from schemas.decision_trace import DecisionTrace
from schemas.execution import ExecutionDispatch, ExecutionResult
from schemas.execution_plan import ExecutionPlan
from schemas.remediation import RemediationAction
from services.runtime.decision_trace import append_node_run
from services.recovery.kubectl_compiler import compile_v1_dispatch_preview
from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient
from workflows.runtime.approval_adapters import (
    candidate_check_hint,
    candidate_namespace,
    dispatch_parameters_match_action,
    legacy_action_for_candidate,
)
from workflows.runtime.result_payloads import to_jsonable


def run_pre_check_for_candidate(
    candidate: ApprovalCandidate,
    *,
    prometheus: PrometheusClient,
    namespace: str,
    deployment: str,
) -> dict[str, object]:
    check_hint = candidate_check_hint(candidate)
    min_ready_count = 1
    if candidate.execution_plan.steps:
        hinted = candidate.execution_plan.steps[0].verification_hints.get("min_ready_count")
        if isinstance(hinted, int) and not isinstance(hinted, bool):
            min_ready_count = hinted
    if check_hint == "crashloop":
        return prometheus.pre_check_crashloop(namespace=namespace, deployment=deployment)
    if check_hint == "cpu_saturation":
        return prometheus.pre_check_cpu_saturation(namespace=namespace, deployment=deployment)
    if check_hint == "bad_config":
        return prometheus.pre_check_bad_config(namespace=namespace, deployment=deployment)
    if check_hint == "network_partition":
        return prometheus.pre_check_network_partition(namespace=namespace, deployment=deployment)
    if check_hint == "deployment_readiness_shortfall":
        return prometheus.pre_check_deployment_readiness_shortfall(
            namespace=namespace,
            deployment=deployment,
            min_ready_count=min_ready_count,
        )
    return {"status": "unknown", "should_execute": False, "attempts": 0}


def run_post_check_for_candidate(
    candidate: ApprovalCandidate,
    *,
    prometheus: PrometheusClient,
    namespace: str,
    deployment: str,
) -> dict[str, object]:
    check_hint = candidate_check_hint(candidate)
    min_ready_count = 1
    if candidate.execution_plan.steps:
        hinted = candidate.execution_plan.steps[0].verification_hints.get("min_ready_count")
        if isinstance(hinted, int) and not isinstance(hinted, bool):
            min_ready_count = hinted
    if check_hint == "crashloop":
        return prometheus.post_check_crashloop(namespace=namespace, deployment=deployment)
    if check_hint == "cpu_saturation":
        return prometheus.post_check_cpu_saturation(namespace=namespace, deployment=deployment)
    if check_hint == "bad_config":
        return prometheus.post_check_bad_config(namespace=namespace, deployment=deployment)
    if check_hint == "network_partition":
        return prometheus.post_check_network_partition(namespace=namespace, deployment=deployment)
    if check_hint == "deployment_readiness_shortfall":
        return prometheus.post_check_deployment_readiness_target(
            namespace=namespace,
            deployment=deployment,
            min_ready_count=min_ready_count,
        )
    return {"status": "unknown", "attempts": 0}


def post_check_function_for_hint(check_hint: str, prometheus: PrometheusClient) -> Any:
    if check_hint == "crashloop":
        return prometheus.post_check_crashloop
    if check_hint == "cpu_saturation":
        return prometheus.post_check_cpu_saturation
    if check_hint == "bad_config":
        return prometheus.post_check_bad_config
    if check_hint == "network_partition":
        return prometheus.post_check_network_partition
    if check_hint == "deployment_readiness_shortfall":
        return lambda *, namespace, deployment: prometheus.post_check_deployment_readiness_target(
            namespace=namespace,
            deployment=deployment,
            min_ready_count=1,
        )
    raise ValueError(f"Unsupported post_check hint for rollback: {check_hint!r}")


def pre_check_summary_for_hint(check_hint: str) -> str:
    if check_hint == "crashloop":
        return "Prometheus pre-check evaluated whether crashloop recovery should execute."
    if check_hint == "cpu_saturation":
        return "Prometheus pre-check evaluated whether CPU saturation recovery should execute."
    if check_hint == "bad_config":
        return "Prometheus pre-check evaluated whether bad-config recovery should execute."
    if check_hint == "network_partition":
        return "Prometheus pre-check evaluated whether network-partition recovery should execute."
    if check_hint == "deployment_readiness_shortfall":
        return "Prometheus pre-check evaluated whether deployment readiness remained below the approved bounded target."
    return "Prometheus pre-check evaluated whether the approved plan should execute."


def post_check_summary_for_hint(check_hint: str) -> str:
    if check_hint == "crashloop":
        return "Post-check verification evaluated whether crashloop recovery succeeded."
    if check_hint == "cpu_saturation":
        return "Post-check verification evaluated whether CPU saturation recovery succeeded."
    if check_hint == "bad_config":
        return "Post-check verification evaluated whether bad-config recovery succeeded."
    if check_hint == "network_partition":
        return "Post-check verification evaluated whether network-partition recovery succeeded."
    if check_hint == "deployment_readiness_shortfall":
        return "Post-check verification evaluated whether deployment readiness reached the approved bounded target."
    return "Post-check verification evaluated whether the approved plan recovered the incident."


def pre_check_skip_reason(pre_check: dict[str, object]) -> str:
    status = str(pre_check.get("status") or "")
    if status == "not_firing":
        return "The incident signal was not firing at execution time."
    if status == "unknown":
        return "Required telemetry was unavailable at execution time."
    return "Pre-check determined that the approved plan should not execute."


def pre_check_observed_fields(pre_check: dict[str, object]) -> dict[str, object]:
    observed: dict[str, object] = {}
    for key in (
        "crashloop_count",
        "cpu_usage",
        "probe_success",
        "network_receive_rate",
        "ready_count",
        "min_ready_count",
        "missing_probe_telemetry",
        "missing_network_telemetry",
    ):
        if key in pre_check:
            observed[key] = pre_check[key]
    return observed


def post_check_observed_fields(post_check: dict[str, object]) -> dict[str, object]:
    observed: dict[str, object] = {}
    for key in (
        "crashloop_count",
        "cpu_usage",
        "probe_success",
        "network_receive_rate",
        "ready_count",
        "min_ready_count",
        "missing_probe_telemetry",
        "missing_network_telemetry",
        "reason",
    ):
        if key in post_check:
            observed[key] = post_check[key]
    return observed


def build_execution_dispatch_for_candidate(
    *,
    incident_id: str,
    candidate: ApprovalCandidate,
) -> tuple[ExecutionDispatch, dict[str, Any]]:
    dispatch_preview = compile_v1_dispatch_preview(candidate.execution_plan)
    if not bool(dispatch_preview.get("executable")):
        raise ValueError(f"Candidate {candidate.candidate_id!r} was not executable")
    return ExecutionDispatch(
        incident_id=incident_id,
        action_id=str(candidate.legacy_action_hint.get("action_id") if candidate.legacy_action_hint else candidate.candidate_id),
        action_type=dispatch_preview["action_type"],
        parameters=dict(dispatch_preview["parameters"]),
        worker_id=f"worker-{uuid4()}",
        requested_at=_utc_now(),
        allowed_tool_names=list(candidate.execution_plan.allowed_tool_names),
        max_steps=max(5, len(candidate.execution_plan.steps) + 2),
    ), {
        "dispatch_source": "v2_execution_plan",
        "candidate_id": candidate.candidate_id,
        "execution_plan": to_jsonable(candidate.execution_plan),
        "legacy_action_hint": dict(candidate.legacy_action_hint) if candidate.legacy_action_hint else None,
    }


def build_execution_result_for_candidate(
    *,
    candidate: ApprovalCandidate,
    dispatch: ExecutionDispatch,
    worker_result: ExecutionResult,
    dispatch_metadata: dict[str, Any] | None = None,
) -> dict[str, object]:
    namespace = candidate_namespace(candidate)
    result: dict[str, object] = {
        "status": worker_result.status,
        "candidate_id": candidate.candidate_id,
        "intent_id": candidate.execution_plan.intent_id,
        "action_id": dispatch.action_id,
        "action_type": dispatch.action_type,
        "operation_family": candidate.execution_plan.operation_family,
        "namespace": namespace,
        "worker_id": dispatch.worker_id,
        "dispatch_status": "succeeded",
        "dispatch": to_jsonable(dispatch),
        "worker_result": to_jsonable(worker_result),
        "command": worker_result.command,
        "returncode": worker_result.returncode,
        "stdout": worker_result.stdout,
        "stderr": worker_result.stderr,
        "summary": worker_result.summary,
        "tool_transcript": worker_result.tool_transcript,
        "execution_plan": to_jsonable(candidate.execution_plan),
    }
    metadata = dict(dispatch_metadata or {})
    if isinstance(metadata.get("dispatch_source"), str):
        result["dispatch_source"] = metadata["dispatch_source"]
    if candidate.legacy_action_hint is not None:
        result["legacy_action_hint"] = dict(candidate.legacy_action_hint)
    if dispatch.action_type in {"delete_stresschaos", "delete_networkchaos"}:
        result["name"] = str(dispatch.parameters["name"])
    elif dispatch.action_type == "delete_pod":
        result["pod"] = str(dispatch.parameters["pod"])
        deployment = dispatch.parameters.get("deployment")
        if isinstance(deployment, str) and deployment:
            result["deployment"] = deployment
    else:
        result["deployment"] = str(dispatch.parameters["deployment"])
    if dispatch.action_type == "scale_deployment":
        result["replicas"] = int(dispatch.parameters["replicas"])
    return result


def allowed_tool_names_for_action(action_type: str) -> list[str]:
    if action_type == "rollout_undo_deployment":
        return [
            "get_deployment_context",
            "get_rollout_status",
            "rollout_undo_deployment",
        ]
    if action_type == "rollout_restart_deployment":
        return [
            "get_deployment_context",
            "get_rollout_status",
            "rollout_restart_deployment",
        ]
    if action_type == "scale_deployment":
        return [
            "get_deployment_context",
            "get_rollout_status",
            "scale_deployment",
        ]
    if action_type == "delete_pod":
        return [
            "get_pod_context",
            "delete_pod",
        ]
    if action_type == "delete_stresschaos":
        return [
            "get_stresschaos",
            "delete_stresschaos",
        ]
    if action_type == "delete_networkchaos":
        return [
            "get_networkchaos",
            "delete_networkchaos",
        ]
    raise ValueError(f"Unsupported recovery execution action: {action_type}")


def build_execution_dispatch(*, incident_id: str, action: RemediationAction) -> ExecutionDispatch:
    return ExecutionDispatch(
        incident_id=incident_id,
        action_id=action.action_id,
        action_type=action.action_type,
        parameters=action.parameters,
        worker_id=f"worker-{uuid4()}",
        requested_at=_utc_now(),
        allowed_tool_names=allowed_tool_names_for_action(action.action_type),
        max_steps=5,
    )


def build_execution_dispatch_for_mode(
    *,
    incident_id: str,
    action: RemediationAction,
    fixer_state: dict[str, Any],
) -> tuple[ExecutionDispatch, dict[str, Any]]:
    engine_mode = str(fixer_state.get("_engine_mode", "v1"))
    if engine_mode != "v2_execute":
        return build_execution_dispatch(incident_id=incident_id, action=action), {
            "dispatch_source": "v1_action_mapping",
        }

    synthesized_plan, fallback_reason = select_phase6_execution_plan(
        action=action,
        fixer_state=fixer_state,
    )
    if synthesized_plan is None:
        return build_execution_dispatch(incident_id=incident_id, action=action), {
            "dispatch_source": "v1_fallback_missing_v2_plan",
            "dispatch_fallback_reason": fallback_reason or "Synthesized execution plan was unavailable.",
        }

    dispatch_preview = compile_v1_dispatch_preview(synthesized_plan)
    if not bool(dispatch_preview.get("executable")):
        return build_execution_dispatch(incident_id=incident_id, action=action), {
            "dispatch_source": "v1_fallback_invalid_v2_plan",
            "dispatch_fallback_reason": "Synthesized execution plan was non-executable for the Phase 6 pilot.",
        }

    return ExecutionDispatch(
        incident_id=incident_id,
        action_id=action.action_id,
        action_type=dispatch_preview["action_type"],
        parameters=dict(dispatch_preview["parameters"]),
        worker_id=f"worker-{uuid4()}",
        requested_at=_utc_now(),
        allowed_tool_names=list(synthesized_plan.allowed_tool_names),
        max_steps=max(5, len(synthesized_plan.steps) + 2),
    ), {
        "dispatch_source": "v2_execute_synthesized_plan",
        "synthesized_intent_id": synthesized_plan.intent_id,
        "execution_plan": to_jsonable(synthesized_plan),
    }


def select_phase6_execution_plan(
    *,
    action: RemediationAction,
    fixer_state: dict[str, Any],
) -> tuple[ExecutionPlan | None, str | None]:
    synthesizer_state = fixer_state.get("_synthesizer_state") or {}
    synthesis_output = synthesizer_state.get("synthesis_output")
    if synthesis_output is None:
        return None, "Synthesizer output was unavailable during v2_execute dispatch selection."

    matching_plans: list[ExecutionPlan] = []
    for plan in list(getattr(synthesis_output, "plans", [])):
        if not isinstance(plan, ExecutionPlan):
            continue
        if not plan.steps or len(plan.steps) != 1:
            continue
        if not plan.requires_approval or plan.blast_radius_score >= 0.8:
            continue
        dispatch_preview = compile_v1_dispatch_preview(plan)
        if not bool(dispatch_preview.get("executable")):
            continue
        if dispatch_preview.get("action_type") != action.action_type:
            continue
        if not dispatch_parameters_match_action(
            dispatch_parameters=dict(dispatch_preview.get("parameters", {})),
            action=action,
        ):
            continue
        if plan.steps[0].tool_name != action.action_type:
            continue
        matching_plans.append(plan)

    if not matching_plans:
        return None, (
            "No synthesized single-step execution plan matched the approved action for the Phase 6 pilot."
        )
    return matching_plans[0], None


def build_execution_result(
    *,
    action: RemediationAction,
    dispatch: ExecutionDispatch,
    worker_result: ExecutionResult,
    dispatch_metadata: dict[str, Any] | None = None,
) -> dict[str, object]:
    namespace = str(action.parameters["namespace"])
    result: dict[str, object] = {
        "status": worker_result.status,
        "action_id": action.action_id,
        "action_type": action.action_type,
        "namespace": namespace,
        "worker_id": dispatch.worker_id,
        "dispatch_status": "succeeded",
        "dispatch": to_jsonable(dispatch),
        "worker_result": to_jsonable(worker_result),
        "command": worker_result.command,
        "returncode": worker_result.returncode,
        "stdout": worker_result.stdout,
        "stderr": worker_result.stderr,
        "summary": worker_result.summary,
        "tool_transcript": worker_result.tool_transcript,
    }
    metadata = dict(dispatch_metadata or {})
    dispatch_source = metadata.get("dispatch_source")
    if isinstance(dispatch_source, str) and dispatch_source:
        result["dispatch_source"] = dispatch_source
    dispatch_fallback_reason = metadata.get("dispatch_fallback_reason")
    if isinstance(dispatch_fallback_reason, str) and dispatch_fallback_reason:
        result["dispatch_fallback_reason"] = dispatch_fallback_reason
    synthesized_intent_id = metadata.get("synthesized_intent_id")
    if isinstance(synthesized_intent_id, str) and synthesized_intent_id:
        result["synthesized_intent_id"] = synthesized_intent_id
    execution_plan = metadata.get("execution_plan")
    if isinstance(execution_plan, dict):
        result["execution_plan"] = execution_plan
    if action.action_type in {"delete_stresschaos", "delete_networkchaos"}:
        result["name"] = str(action.parameters["name"])
    elif action.action_type == "delete_pod":
        result["pod"] = str(action.parameters["pod"])
        deployment = action.parameters.get("deployment")
        if isinstance(deployment, str) and deployment:
            result["deployment"] = deployment
    else:
        result["deployment"] = str(action.parameters["deployment"])
    if action.action_type == "scale_deployment":
        result["replicas"] = int(action.parameters["replicas"])
    return result


def apply_kubernetes_recovery_fallback(
    *,
    post_check: dict[str, object],
    execution_result: dict[str, object],
    kubernetes: KubernetesClient,
    namespace: str,
    deployment: str,
) -> dict[str, object]:
    rollout_status = execution_result.get("rollout_status")
    if not isinstance(rollout_status, dict):
        return post_check
    if rollout_status.get("status") != "succeeded":
        return post_check
    if post_check.get("status") == "recovered":
        return post_check
    if post_check.get("crashloop_count") != 0.0:
        return post_check
    if post_check.get("ready_count") not in (0.0, 0):
        return post_check

    cached_availability = execution_result.get("deployment_availability")
    if isinstance(cached_availability, dict):
        kubernetes_fallback = dict(cached_availability)
    else:
        kubernetes_fallback = kubernetes.get_deployment_availability(
            namespace=namespace,
            deployment=deployment,
        )
    updated_post_check = dict(post_check)
    updated_post_check["kubernetes_fallback"] = kubernetes_fallback
    if kubernetes_fallback.get("is_available") is True:
        updated_post_check["status"] = "recovered"
        updated_post_check["reason"] = (
            "Prometheus readiness lagged after rollout, but Kubernetes reported the deployment as available."
        )
    return updated_post_check


def wait_for_deployment_availability(
    *,
    kubernetes: KubernetesClient,
    namespace: str,
    deployment: str,
    sleep_fn: Any,
    attempts: int,
    sleep_seconds: float,
) -> dict[str, object]:
    max_attempts = max(1, attempts)
    latest: dict[str, object] = {
        "status": "not_run",
        "namespace": namespace,
        "deployment": deployment,
        "availability_status": "unknown",
        "attempts": 0,
        "is_available": False,
    }

    for attempt in range(1, max_attempts + 1):
        current = kubernetes.get_deployment_availability(
            namespace=namespace,
            deployment=deployment,
        )
        latest = dict(current)
        latest["attempts"] = attempt
        latest["availability_status"] = "available" if current.get("is_available") is True else "unavailable"
        if current.get("is_available") is True:
            break
        if attempt < max_attempts:
            sleep_fn(sleep_seconds)

    return latest


def attempt_bounded_rollback(
    *,
    action: RemediationAction,
    kubernetes: KubernetesClient,
    prometheus: PrometheusClient,
    namespace: str,
    deployment: str,
    post_check_fn: Any,
    apply_kubernetes_fallback_to_post_check: bool,
    failure_reason: str,
    rollout_wait_timeout_seconds: int,
    rollout_availability_grace_attempts: int,
    rollout_availability_grace_sleep_seconds: float,
) -> tuple[bool, dict[str, object] | None, dict[str, object] | None]:
    if action.action_type != "rollout_restart_deployment":
        return False, None, None

    rollback_result = kubernetes.rollout_undo_deployment(
        namespace=namespace,
        deployment=deployment,
    )
    rollback_record: dict[str, object] = {
        "status": rollback_result["status"],
        "reason": failure_reason,
        "action_type": "rollout_undo_deployment",
        "namespace": namespace,
        "deployment": deployment,
        "command": rollback_result["command"],
        "returncode": rollback_result["returncode"],
        "stdout": rollback_result["stdout"],
        "stderr": rollback_result["stderr"],
    }
    if rollback_result["status"] != "succeeded":
        return True, rollback_record, None

    rollback_record["rollout_status"] = kubernetes.wait_for_rollout_deployment(
        namespace=namespace,
        deployment=deployment,
        timeout_seconds=rollout_wait_timeout_seconds,
    )
    rollback_rollout_status = rollback_record["rollout_status"]
    if not isinstance(rollback_rollout_status, dict) or rollback_rollout_status.get("status") != "succeeded":
        return True, rollback_record, None

    rollback_record["deployment_availability"] = wait_for_deployment_availability(
        kubernetes=kubernetes,
        namespace=namespace,
        deployment=deployment,
        sleep_fn=prometheus.sleep_fn,
        attempts=rollout_availability_grace_attempts,
        sleep_seconds=rollout_availability_grace_sleep_seconds,
    )
    post_rollback_check = post_check_fn(namespace=namespace, deployment=deployment)
    if apply_kubernetes_fallback_to_post_check:
        post_rollback_check = apply_kubernetes_recovery_fallback(
            post_check=post_rollback_check,
            execution_result={
                "rollout_status": rollback_rollout_status,
                "deployment_availability": rollback_record["deployment_availability"],
            },
            kubernetes=kubernetes,
            namespace=namespace,
            deployment=deployment,
        )
    return True, rollback_record, post_rollback_check


def recovery_latency_seconds(requested_at: str) -> float:
    started = datetime.fromisoformat(requested_at)
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


def append_rollback_run(
    trace: DecisionTrace,
    *,
    approved_action: RemediationAction,
    rollback_result: dict[str, object],
    post_rollback_check: dict[str, object] | None,
) -> DecisionTrace:
    return append_node_run(
        trace,
        node_name="rollback",
        status=str(rollback_result["status"]),
        summary="HERALD triggered a bounded rollback after the approved action failed to verify cleanly.",
        input_summary={
            "failed_action_id": approved_action.action_id,
            "failed_action_type": approved_action.action_type,
        },
        output_summary={
            "status": rollback_result["status"],
            "action_type": rollback_result["action_type"],
            "returncode": rollback_result["returncode"],
            "post_rollback_status": post_rollback_check["status"] if post_rollback_check else None,
        },
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
