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
    fixer_llm: Any = None,
    judge_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    incidents = incidents_from_alertmanager_payload(payload)
    if len(incidents) != 1:
        raise ValueError("Crashloop recovery demo expects exactly one incident per payload.")

    incident = incidents[0]
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

    if approve_action_id is None:
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=hitl_decision.decision_trace,
        )

    if hitl_decision.routing_decision == "halt":
        raise ValueError("HITL Gate halted the plan; crashloop execution is not allowed.")

    approved_action = _select_action(hitl_decision, approve_action_id)
    trace = record_human_approval(
        hitl_decision.decision_trace,
        human_approval="approved",
        final_state="executing",
    )

    namespace = str(approved_action.parameters["namespace"])
    deployment = str(approved_action.parameters["deployment"])
    prometheus = prometheus_client or PrometheusClient()
    kubernetes = kubernetes_client or KubernetesClient()
    worker_client = execution_worker_client or ExecutionWorkerClient()

    pre_check = prometheus.pre_check_crashloop(namespace=namespace, deployment=deployment)
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
    if execution_result["status"] == "succeeded":
        execution_result["rollout_status"] = kubernetes.wait_for_rollout_deployment(
            namespace=namespace,
            deployment=deployment,
        )
    post_check = prometheus.post_check_crashloop(namespace=namespace, deployment=deployment)
    post_check = _apply_kubernetes_recovery_fallback(
        post_check=post_check,
        execution_result=execution_result,
        kubernetes=kubernetes,
        namespace=namespace,
        deployment=deployment,
    )
    final_state = "recovered" if post_check["status"] == "recovered" else "unrecovered"
    trace = finalize_decision_trace(
        trace,
        execution_result=execution_result,
        verification_result={"pre_check": pre_check, "post_check": post_check},
        final_state=final_state,
    )
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
    }


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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the crashloop recovery workflow from an Alertmanager payload."
    )
    parser.add_argument("--payload-file", required=True)
    parser.add_argument("--approve-action-id")
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

    fixer_llm = None
    if args.fixer_provider == "gemini":
        fixer_llm = GeminiFixerLLM(model=args.fixer_model)

    judge_llm = None
    if args.judge_provider == "gemini":
        judge_llm = GeminiJudgeLLM(model=args.judge_model)

    prometheus_client = PrometheusClient(base_url=args.prometheus_base_url)
    result = run_crashloop_recovery_from_payload(
        payload,
        approve_action_id=args.approve_action_id,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        prometheus_client=prometheus_client,
    )
    print(json.dumps(_to_jsonable(result), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
