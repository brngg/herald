from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agents.fixer import run_fixer_pipeline
from agents.judge import run_judge_pipeline
from schemas.decision_trace import DecisionTrace
from schemas.execution import ExecutionDispatch, ExecutionResult
from schemas.remediation import RemediationAction
from services.alertmanager_client import incidents_from_alertmanager_payload
from services.decision_trace_provenance import append_node_run, derive_trace_timeline, initialize_trace_provenance
from services.execution_worker import ExecutionWorkerClient
from services.gemini_fixer_llm import GeminiFixerLLM
from services.gemini_judge_llm import GeminiJudgeLLM
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import PrometheusClient
from workflows.hitl_gate import (
    HITLDecision,
    finalize_decision_trace,
    record_human_approval,
    route_crashloop_plan,
)


def run_crashloop_recovery_from_payload(
    payload: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    fixer_llm: Any = None,
    judge_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    if approve_action_id and reject_action_id:
        raise ValueError("Specify either approve_action_id or reject_action_id, not both.")

    incidents = incidents_from_alertmanager_payload(payload)
    if len(incidents) != 1:
        raise ValueError("Crashloop recovery demo expects exactly one incident per payload.")

    incident = incidents[0]
    fixer_state, judge_state, hitl_decision = _plan_crashloop_recovery(
        incident=incident,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
    )
    return _continue_crashloop_recovery(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        approve_action_id=approve_action_id,
        reject_action_id=reject_action_id,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
        execution_worker_client=execution_worker_client,
    )


def run_crashloop_recovery_from_saved_plan(
    payload: dict[str, Any],
    saved_result: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    if approve_action_id and reject_action_id:
        raise ValueError("Specify either approve_action_id or reject_action_id, not both.")
    if approve_action_id is None and reject_action_id is None:
        raise ValueError("resume-from-file requires --approve-action-id or --reject-action-id.")

    incidents = incidents_from_alertmanager_payload(payload)
    if len(incidents) != 1:
        raise ValueError("Crashloop recovery demo expects exactly one incident per payload.")
    incident = incidents[0]

    fixer_state = _saved_mapping(saved_result.get("fixer_state"), field_name="fixer_state")
    judge_state = _saved_mapping(saved_result.get("judge_state"), field_name="judge_state")
    hitl_decision_payload = _saved_mapping(saved_result.get("hitl_decision"), field_name="hitl_decision")
    decision_trace_payload = _saved_mapping(saved_result.get("decision_trace"), field_name="decision_trace")

    hitl_decision = HITLDecision(
        routing_decision=str(hitl_decision_payload["routing_decision"]),
        requires_approval=bool(hitl_decision_payload["requires_approval"]),
        recommended_action=_remediation_action_from_saved(hitl_decision_payload.get("recommended_action")),
        candidate_actions=[
            _remediation_action_from_saved(action_payload)
            for action_payload in list(hitl_decision_payload.get("candidate_actions", []))
        ],
        decision_trace=_decision_trace_from_saved(decision_trace_payload),
    )
    return _continue_crashloop_recovery(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        approve_action_id=approve_action_id,
        reject_action_id=reject_action_id,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
        execution_worker_client=execution_worker_client,
    )


def _plan_crashloop_recovery(
    *,
    incident: Any,
    fixer_llm: Any = None,
    judge_llm: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], HITLDecision]:
    fixer_state = run_fixer_pipeline(incident, llm=fixer_llm)
    judge_state = run_judge_pipeline(
        incident=incident,
        evidence=fixer_state["evidence"],
        incident_summary=fixer_state["incident_summary"],
        actions=fixer_state["actions"],
        fixer_rationale=fixer_state.get("fixer_rationale"),
        llm=judge_llm,
    )
    hitl_decision = route_crashloop_plan(
        incident=incident,
        actions=fixer_state["actions"],
        fixer_rationale=fixer_state.get("fixer_rationale"),
        judge_verdict=judge_state["judge_verdict"],
        judge_reason=judge_state["judge_reason"],
    )
    trace = initialize_trace_provenance(hitl_decision.decision_trace)
    trace = append_node_run(
        trace,
        node_name="fixer",
        status="succeeded",
        summary="Fixer generated a bounded remediation plan.",
        llm_explanation=_truncate_text(fixer_state.get("fixer_rationale")),
        input_summary={
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
        },
        output_summary={
            "incident_summary": fixer_state["incident_summary"],
            "ranked_action_ids": [action.action_id for action in fixer_state["actions"]],
            "fixer_rationale": _truncate_text(fixer_state.get("fixer_rationale") or ""),
        },
    )
    trace = append_node_run(
        trace,
        node_name="judge",
        status=str(judge_state["judge_verdict"]),
        summary="Judge evaluated the Fixer plan against the safety rubric.",
        llm_explanation=_truncate_text(judge_state.get("judge_llm_reason")),
        input_summary={
            "incident_id": incident.incident_id,
            "candidate_action_ids": [action.action_id for action in fixer_state["actions"]],
        },
        output_summary={
            "judge_verdict": judge_state["judge_verdict"],
            "judge_reason": judge_state["judge_reason"],
        },
    )
    trace = append_node_run(
        trace,
        node_name="hitl_gate",
        status="succeeded",
        summary="HITL Gate routed the plan according to confidence, blast radius, and Judge verdict.",
        input_summary={
            "judge_verdict": judge_state["judge_verdict"],
            "candidate_action_ids": [action.action_id for action in hitl_decision.candidate_actions],
        },
        output_summary={
            "routing_decision": hitl_decision.routing_decision,
            "requires_approval": hitl_decision.requires_approval,
            "recommended_action_id": (
                hitl_decision.recommended_action.action_id if hitl_decision.recommended_action else None
            ),
            "candidate_action_ids": [action.action_id for action in hitl_decision.candidate_actions],
        },
    )
    hitl_decision = HITLDecision(
        routing_decision=hitl_decision.routing_decision,
        requires_approval=hitl_decision.requires_approval,
        recommended_action=hitl_decision.recommended_action,
        candidate_actions=hitl_decision.candidate_actions,
        decision_trace=trace,
    )
    return fixer_state, judge_state, hitl_decision


def _continue_crashloop_recovery(
    *,
    incident: Any,
    fixer_state: dict[str, Any],
    judge_state: dict[str, Any],
    hitl_decision: HITLDecision,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:

    if approve_action_id is None and reject_action_id is None:
        decision_trace = hitl_decision.decision_trace
        if hitl_decision.routing_decision == "halt":
            decision_trace = finalize_decision_trace(
                decision_trace,
                execution_result={
                    "status": "halted",
                    "reason": "HITL Gate escalated the plan before execution.",
                },
                verification_result={
                    "status": "not_run",
                    "reason": "Execution did not start because the HITL Gate halted the plan.",
                },
                final_state="escalated",
            )
            decision_trace = _append_finalization_run(decision_trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=decision_trace,
        )

    if hitl_decision.routing_decision == "halt":
        trace = finalize_decision_trace(
            hitl_decision.decision_trace,
            execution_result={
                "status": "halted",
                "reason": "HITL Gate escalated the plan before execution.",
            },
            verification_result={
                "status": "not_run",
                "reason": "Execution did not start because the HITL Gate halted the plan.",
            },
            final_state="escalated",
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    if reject_action_id is not None:
        rejected_action = _select_action(hitl_decision, reject_action_id)
        trace = record_human_approval(
            hitl_decision.decision_trace,
            human_approval="rejected",
            final_state="rejected",
        )
        trace = append_node_run(
            trace,
            node_name="human_approval",
            status="rejected",
            summary="Human operator rejected the proposed remediation action.",
            input_summary={
                "action_id": rejected_action.action_id,
                "action_type": rejected_action.action_type,
            },
            output_summary={
                "human_approval": "rejected",
                "selected_action_id": rejected_action.action_id,
            },
        )
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "not_executed",
                "action_id": rejected_action.action_id,
                "action_type": rejected_action.action_type,
                "reason": "Human rejected the proposed remediation action.",
            },
            verification_result={
                "status": "not_run",
                "reason": "Execution was skipped because the human operator rejected the proposed action.",
            },
            final_state="rejected",
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    approved_action = _select_action(hitl_decision, approve_action_id)
    trace = record_human_approval(
        hitl_decision.decision_trace,
        human_approval="approved",
        final_state="executing",
    )
    trace = append_node_run(
        trace,
        node_name="human_approval",
        status="approved",
        summary="Human operator approved the proposed remediation action.",
        input_summary={
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
        },
        output_summary={
            "human_approval": "approved",
            "selected_action_id": approved_action.action_id,
        },
    )

    if approved_action.action_type not in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        final_state = "escalated"
        execution_result = {
            "status": "not_executed",
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
            "reason": "Approved action does not execute an automated bounded rollout step.",
        }
        verification_result = {
            "status": "not_run",
            "reason": "No automated execution was attempted because the approved action was non-executable.",
        }
        trace = finalize_decision_trace(
            trace,
            execution_result=execution_result,
            verification_result=verification_result,
            final_state=final_state,
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    namespace = str(approved_action.parameters["namespace"])
    deployment = str(approved_action.parameters["deployment"])
    prometheus = prometheus_client or PrometheusClient()
    kubernetes = kubernetes_client or KubernetesClient()
    worker_client = execution_worker_client or ExecutionWorkerClient()

    pre_check = prometheus.pre_check_crashloop(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="pre_check",
        status=str(pre_check["status"]),
        summary="Prometheus pre-check evaluated whether crashloop recovery should execute.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
        },
        output_summary={
            "status": pre_check["status"],
            "crashloop_count": pre_check["crashloop_count"],
            "attempts": pre_check["attempts"],
            "should_execute": pre_check["should_execute"],
        },
    )
    if not bool(pre_check["should_execute"]):
        verification_result = {
            "status": "recovered",
            "reason": "Crashloop was not firing at execution time.",
            "pre_check": pre_check,
        }
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "skipped",
                "reason": "Pre-check determined no crashloop action was necessary.",
            },
            verification_result=verification_result,
            final_state="recovered",
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    dispatch = _build_execution_dispatch(incident_id=incident.incident_id, action=approved_action)
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result(
        action=approved_action,
        dispatch=dispatch,
        worker_result=worker_result,
    )
    trace = append_node_run(
        trace,
        node_name="execution_worker",
        status=str(worker_result.status),
        summary=_execution_worker_summary(worker_result.status),
        llm_explanation=_execution_worker_llm_explanation(
            action=approved_action,
            worker_result=worker_result,
        ),
        input_summary={
            "worker_id": dispatch.worker_id,
            "action_id": dispatch.action_id,
            "action_type": dispatch.action_type,
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "tool_names": [entry.get("tool_name") for entry in worker_result.tool_transcript],
        },
    )
    if execution_result["status"] != "succeeded":
        trace = finalize_decision_trace(
            trace,
            execution_result=execution_result,
            verification_result={
                "pre_check": pre_check,
                "post_check": {
                    "status": "not_run",
                    "reason": "Post-check did not run because the execution worker failed before rollout verification.",
                },
            },
            final_state="escalated",
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    execution_result["rollout_status"] = kubernetes.wait_for_rollout_deployment(
        namespace=namespace,
        deployment=deployment,
    )
    rollout_status = execution_result["rollout_status"]
    trace = append_node_run(
        trace,
        node_name="rollout_wait",
        status=str(rollout_status["status"]),
        summary="Kubernetes rollout status was checked after the approved remediation executed.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "action_id": approved_action.action_id,
        },
        output_summary={
            "status": rollout_status["status"],
            "returncode": rollout_status["returncode"],
        },
    )
    if rollout_status["status"] != "succeeded":
        rollback_triggered, rollback_result, post_rollback_check = _attempt_bounded_rollback(
            action=approved_action,
            kubernetes=kubernetes,
            prometheus=prometheus,
            namespace=namespace,
            deployment=deployment,
            failure_reason="Approved action rollout did not converge.",
        )
        if rollback_result is not None:
            execution_result["rollback"] = rollback_result
            trace = _append_rollback_run(
                trace,
                approved_action=approved_action,
                rollback_result=rollback_result,
                post_rollback_check=post_rollback_check,
            )
        verification_result = {
            "pre_check": pre_check,
            "post_check": {
                "status": "not_run",
                "reason": "Post-check did not run because rollout convergence failed after execution.",
            },
        }
        if post_rollback_check is not None:
            verification_result["post_rollback_check"] = post_rollback_check
        final_state = "rolled_back" if post_rollback_check and post_rollback_check["status"] == "recovered" else "escalated"
        verification_result["recovery_latency_seconds"] = _recovery_latency_seconds(dispatch.requested_at)
        trace = finalize_decision_trace(
            trace,
            execution_result=execution_result,
            verification_result=verification_result,
            final_state=final_state,
            rollback_triggered=rollback_triggered,
        )
        trace = _append_finalization_run(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    post_check = prometheus.post_check_crashloop(namespace=namespace, deployment=deployment)
    post_check = _apply_kubernetes_recovery_fallback(
        post_check=post_check,
        execution_result=execution_result,
        kubernetes=kubernetes,
        namespace=namespace,
        deployment=deployment,
    )
    trace = append_node_run(
        trace,
        node_name="post_check",
        status=str(post_check["status"]),
        summary="Post-check verification evaluated whether recovery succeeded after execution.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "action_id": approved_action.action_id,
        },
        output_summary={
            "status": post_check["status"],
            "crashloop_count": post_check.get("crashloop_count"),
            "ready_count": post_check.get("ready_count"),
            "attempts": post_check.get("attempts"),
        },
    )
    rollback_triggered, rollback_result, post_rollback_check = _attempt_bounded_rollback(
        action=approved_action,
        kubernetes=kubernetes,
        prometheus=prometheus,
        namespace=namespace,
        deployment=deployment,
        failure_reason="Post-check verification did not confirm recovery.",
    ) if post_check["status"] != "recovered" else (False, None, None)
    if rollback_result is not None:
        execution_result["rollback"] = rollback_result
        trace = _append_rollback_run(
            trace,
            approved_action=approved_action,
            rollback_result=rollback_result,
            post_rollback_check=post_rollback_check,
        )
    final_state = "recovered"
    if post_check["status"] != "recovered":
        if post_rollback_check is not None and post_rollback_check["status"] == "recovered":
            final_state = "rolled_back"
        else:
            final_state = "escalated"
    verification_result = {"pre_check": pre_check, "post_check": post_check}
    if post_rollback_check is not None:
        verification_result["post_rollback_check"] = post_rollback_check
    verification_result["recovery_latency_seconds"] = _recovery_latency_seconds(dispatch.requested_at)
    trace = finalize_decision_trace(
        trace,
        execution_result=execution_result,
        verification_result=verification_result,
        final_state=final_state,
        rollback_triggered=rollback_triggered,
    )
    trace = _append_finalization_run(trace)
    return _build_result(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        decision_trace=trace,
    )


def _select_action(hitl_decision: HITLDecision, action_id: str) -> RemediationAction:
    for action in hitl_decision.candidate_actions:
        if action.action_id == action_id:
            return action
    raise ValueError(f"Approved action_id {action_id!r} is not available in the HITL decision.")


def _build_execution_dispatch(*, incident_id: str, action: RemediationAction) -> ExecutionDispatch:
    return ExecutionDispatch(
        incident_id=incident_id,
        action_id=action.action_id,
        action_type=action.action_type,
        parameters=action.parameters,
        worker_id=f"worker-{uuid4()}",
        requested_at=_utc_now(),
        allowed_tool_names=_allowed_tool_names_for_action(action.action_type),
        max_steps=5,
    )


def _build_execution_result(
    *,
    action: RemediationAction,
    dispatch: ExecutionDispatch,
    worker_result: ExecutionResult,
) -> dict[str, object]:
    namespace = str(action.parameters["namespace"])
    deployment = str(action.parameters["deployment"])
    return {
        "status": worker_result.status,
        "action_id": action.action_id,
        "action_type": action.action_type,
        "namespace": namespace,
        "deployment": deployment,
        "worker_id": dispatch.worker_id,
        "dispatch_status": "succeeded",
        "dispatch": _to_jsonable(dispatch),
        "worker_result": _to_jsonable(worker_result),
        "command": worker_result.command,
        "returncode": worker_result.returncode,
        "stdout": worker_result.stdout,
        "stderr": worker_result.stderr,
        "summary": worker_result.summary,
        "tool_transcript": worker_result.tool_transcript,
    }


def _build_result(
    *,
    incident: Any,
    fixer_state: dict[str, Any],
    judge_state: dict[str, Any],
    hitl_decision: HITLDecision,
    decision_trace: DecisionTrace,
) -> dict[str, Any]:
    return {
        "incident": incident,
        "fixer_state": fixer_state,
        "judge_state": judge_state,
        "hitl_decision": {
            "routing_decision": hitl_decision.routing_decision,
            "requires_approval": hitl_decision.requires_approval,
            "recommended_action": hitl_decision.recommended_action,
            "candidate_actions": hitl_decision.candidate_actions,
        },
        "decision_trace": decision_trace,
        "decision_trace_timeline": derive_trace_timeline(decision_trace),
    }


def _saved_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError(f"saved {field_name} must be an object")
    return value


def _remediation_action_from_saved(value: Any) -> RemediationAction | None:
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


def _decision_trace_from_saved(value: dict[str, Any]) -> DecisionTrace:
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


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _allowed_tool_names_for_action(action_type: str) -> list[str]:
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
    raise ValueError(f"Unsupported crashloop execution action: {action_type}")


def _execution_worker_summary(status: str) -> str:
    if status == "succeeded":
        return "Execution worker completed the approved remediation action."
    return "Execution worker failed while attempting the approved remediation action."


def _execution_worker_llm_explanation(
    *,
    action: RemediationAction,
    worker_result: ExecutionResult,
) -> str | None:
    narrative = _truncate_text(worker_result.summary, limit=300)

    namespace = str(action.parameters["namespace"])
    deployment = str(action.parameters["deployment"])
    tool_names = [
        str(entry.get("tool_name"))
        for entry in worker_result.tool_transcript
        if isinstance(entry.get("tool_name"), str)
    ]
    tool_clause = (
        f"It used the bounded tools {', '.join(tool_names)} before finishing."
        if tool_names
        else "It did not report any tool invocations before finishing."
    )
    command_text = " ".join(worker_result.command) if worker_result.command else "no command"
    outcome_clause = (
        f"The approved action succeeded with return code {worker_result.returncode}."
        if worker_result.status == "succeeded"
        else f"The approved action failed with return code {worker_result.returncode}."
    )

    stdout = _truncate_text(worker_result.stdout, limit=200)
    stderr = _truncate_text(worker_result.stderr, limit=200)
    io_clause = ""
    if stdout:
        io_clause += f" stdout={stdout!r}."
    if stderr:
        io_clause += f" stderr={stderr!r}."

    base_explanation = (
        f"The Gemini execution agent handled approved action {action.action_id!r} "
        f"({action.action_type}) for deployment {deployment!r} in namespace {namespace!r}. "
        f"{tool_clause} It executed {command_text!r}. {outcome_clause}{io_clause}"
    )
    if narrative:
        lead = narrative
        if lead[-1] not in ".!?":
            lead += "."
        return f"{lead} {base_explanation}"
    return base_explanation


def _apply_kubernetes_recovery_fallback(
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


def _attempt_bounded_rollback(
    *,
    action: RemediationAction,
    kubernetes: KubernetesClient,
    prometheus: PrometheusClient,
    namespace: str,
    deployment: str,
    failure_reason: str,
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
    )
    rollback_rollout_status = rollback_record["rollout_status"]
    if not isinstance(rollback_rollout_status, dict) or rollback_rollout_status.get("status") != "succeeded":
        return True, rollback_record, None

    post_rollback_check = prometheus.post_check_crashloop(namespace=namespace, deployment=deployment)
    post_rollback_check = _apply_kubernetes_recovery_fallback(
        post_check=post_rollback_check,
        execution_result={"rollout_status": rollback_rollout_status},
        kubernetes=kubernetes,
        namespace=namespace,
        deployment=deployment,
    )
    return True, rollback_record, post_rollback_check


def _recovery_latency_seconds(requested_at: str) -> float:
    started = datetime.fromisoformat(requested_at)
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


def _append_rollback_run(
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


def _append_finalization_run(trace: DecisionTrace) -> DecisionTrace:
    return append_node_run(
        trace,
        node_name="finalization",
        status=str(trace.final_state),
        summary="DecisionTrace was finalized with the latest workflow state.",
        input_summary={
            "human_approval": trace.human_approval,
            "routing_decision": trace.routing_decision,
        },
        output_summary={
            "final_state": trace.final_state,
            "rollback_triggered": trace.rollback_triggered,
        },
    )


def _truncate_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "...<truncated>"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the crashloop recovery workflow from an Alertmanager payload."
    )
    parser.add_argument("--payload-file", required=True)
    parser.add_argument("--resume-from-file")
    parser.add_argument("--approve-action-id")
    parser.add_argument("--reject-action-id")
    parser.add_argument(
        "--interactive-hitl",
        action="store_true",
        help="Run planning first, then prompt in the terminal: 1 = approve recommended action, 2 = reject.",
    )
    parser.add_argument(
        "--fixer-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--judge-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument("--fixer-model", default="gemini-2.5-flash")
    parser.add_argument("--judge-model", default="gemini-2.5-flash")
    parser.add_argument("--prometheus-base-url")
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    with open(args.payload_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("payload JSON must be an object")

    prometheus_client = PrometheusClient(base_url=args.prometheus_base_url)
    if args.interactive_hitl and args.resume_from_file:
        raise ValueError("--interactive-hitl cannot be combined with --resume-from-file.")
    if args.interactive_hitl and (args.approve_action_id or args.reject_action_id):
        raise ValueError("--interactive-hitl cannot be combined with explicit approve/reject action ids.")
    if args.resume_from_file:
        with open(args.resume_from_file, "r", encoding="utf-8") as handle:
            saved_result = json.load(handle)
        if not isinstance(saved_result, dict):
            raise TypeError("resume-from-file JSON must be an object")
        result = run_crashloop_recovery_from_saved_plan(
            payload,
            saved_result,
            approve_action_id=args.approve_action_id,
            reject_action_id=args.reject_action_id,
            prometheus_client=prometheus_client,
        )
    else:
        fixer_llm = None
        if args.fixer_provider == "gemini":
            fixer_llm = GeminiFixerLLM(model=args.fixer_model)

        judge_llm = None
        if args.judge_provider == "gemini":
            judge_llm = GeminiJudgeLLM(model=args.judge_model)

        if args.interactive_hitl:
            planning_result = run_crashloop_recovery_from_payload(
                payload,
                fixer_llm=fixer_llm,
                judge_llm=judge_llm,
                prometheus_client=prometheus_client,
            )
            result = _continue_with_interactive_hitl(
                payload=payload,
                planning_result=planning_result,
                prometheus_client=prometheus_client,
            )
        else:
            result = run_crashloop_recovery_from_payload(
                payload,
                approve_action_id=args.approve_action_id,
                reject_action_id=args.reject_action_id,
                fixer_llm=fixer_llm,
                judge_llm=judge_llm,
                prometheus_client=prometheus_client,
            )
    print(json.dumps(_to_jsonable(result), default=str, indent=2))
    return 0


def _continue_with_interactive_hitl(
    *,
    payload: dict[str, Any],
    planning_result: dict[str, Any],
    prometheus_client: PrometheusClient,
    kubernetes_client: KubernetesClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
    input_fn: Any = input,
) -> dict[str, Any]:
    hitl_decision = planning_result["hitl_decision"]
    recommended_action = hitl_decision["recommended_action"]
    if recommended_action is None:
        return planning_result

    action_id = recommended_action.action_id
    action_type = recommended_action.action_type
    print(
        f"HITL Gate: recommended action {action_id} ({action_type}).",
        flush=True,
    )
    print("Enter 1 to approve or 2 to reject.", flush=True)
    user_choice = str(input_fn("> ")).strip()

    if user_choice == "1":
        return run_crashloop_recovery_from_saved_plan(
            payload,
            planning_result,
            approve_action_id=action_id,
            kubernetes_client=kubernetes_client,
            prometheus_client=prometheus_client,
            execution_worker_client=execution_worker_client,
        )
    if user_choice == "2":
        return run_crashloop_recovery_from_saved_plan(
            payload,
            planning_result,
            reject_action_id=action_id,
            kubernetes_client=kubernetes_client,
            prometheus_client=prometheus_client,
            execution_worker_client=execution_worker_client,
        )
    return planning_result


if __name__ == "__main__":
    raise SystemExit(main())
