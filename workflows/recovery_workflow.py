from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from agents.fixer import run_fixer_pipeline
from agents.critic import run_critic_pipeline
from agents.judge import run_judge_pipeline
from agents.replanner import run_replanner_pipeline
from agents.reasoner import run_reasoner_pipeline
from agents.synthesizer import run_synthesizer_pipeline
from schemas.decision_trace import DecisionTrace
from schemas.execution import ExecutionDispatch, ExecutionResult
from schemas.observations import ObservationBundle, observation_bundle_from_dict
from schemas.remediation import RemediationAction
from schemas.verification import VerificationResultV2
from services.alertmanager_client import incidents_from_alertmanager_payload
from services.capability_catalog import default_capability_catalog
from services.cluster_observer import ClusterObserver
from services.gemini_critic_llm import GeminiCriticLLM
from services.incident_normalization import normalize_incident_class
from services.decision_trace_provenance import append_node_run, derive_trace_timeline, initialize_trace_provenance
from services.execution_worker import ExecutionWorkerClient
from services.gemini_fixer_llm import GeminiFixerLLM
from services.gemini_judge_llm import GeminiJudgeLLM
from services.gemini_reasoner_llm import GeminiReasonerLLM
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import PrometheusClient
from services.verification_engine import build_shadow_verification_plan, run_verification
from workflows.hitl_gate import (
    HITLDecision,
    finalize_decision_trace,
    record_human_approval,
    route_plan,
)

ROLLOUT_WAIT_TIMEOUT_SECONDS = 60
EngineMode = Literal["v1", "v2_shadow", "v2_execute"]
VALID_ENGINE_MODES: tuple[EngineMode, ...] = ("v1", "v2_shadow", "v2_execute")


def run_recovery_from_payload(
    payload: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    engine_mode = _validate_engine_mode(engine_mode)
    if approve_action_id and reject_action_id:
        raise ValueError("Specify either approve_action_id or reject_action_id, not both.")

    incidents = incidents_from_alertmanager_payload(payload)
    if len(incidents) != 1:
        raise ValueError("Recovery workflow expects exactly one incident per payload.")

    incident = incidents[0]
    observation_bundle: ObservationBundle | None = None
    observation_run: dict[str, Any] | None = None
    reasoner_state: dict[str, Any] | None = None
    reasoner_run: dict[str, Any] | None = None
    critic_state: dict[str, Any] | None = None
    critic_run: dict[str, Any] | None = None
    synthesizer_state: dict[str, Any] | None = None
    synthesizer_run: dict[str, Any] | None = None
    verifier_state: dict[str, Any] | None = None
    replanner_state: dict[str, Any] | None = None
    if engine_mode != "v1":
        observation_bundle, observation_run = _collect_observations(
            incident=incident,
            kubernetes_client=kubernetes_client,
            prometheus_client=prometheus_client,
            engine_mode=engine_mode,
        )
        reasoner_state, reasoner_run = _run_shadow_reasoner(
            incident=incident,
            observation_bundle=observation_bundle,
            reasoner_llm=reasoner_llm,
            engine_mode=engine_mode,
        )
        critic_state, critic_run = _run_shadow_critic(
            incident=incident,
            observation_bundle=observation_bundle,
            reasoner_state=reasoner_state,
            critic_llm=critic_llm,
            engine_mode=engine_mode,
        )
        synthesizer_state, synthesizer_run = _run_shadow_synthesizer(
            incident=incident,
            observation_bundle=observation_bundle,
            reasoner_state=reasoner_state,
            critic_state=critic_state,
            engine_mode=engine_mode,
        )
    fixer_state, judge_state, hitl_decision = _plan_recovery(
        incident=incident,
        engine_mode=engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        observation_bundle=observation_bundle,
        observation_run=observation_run,
        reasoner_state=reasoner_state,
        reasoner_run=reasoner_run,
        critic_state=critic_state,
        critic_run=critic_run,
        synthesizer_state=synthesizer_state,
        synthesizer_run=synthesizer_run,
        verifier_state=verifier_state,
        replanner_state=replanner_state,
    )
    fixer_state["_engine_mode"] = engine_mode
    if observation_bundle is not None:
        fixer_state["_observation_bundle"] = observation_bundle
    if reasoner_state is not None:
        fixer_state["_reasoner_state"] = reasoner_state
    if critic_state is not None:
        fixer_state["_critic_state"] = critic_state
    if synthesizer_state is not None:
        fixer_state["_synthesizer_state"] = synthesizer_state
    if verifier_state is not None:
        fixer_state["_verifier_state"] = verifier_state
    if replanner_state is not None:
        fixer_state["_replanner_state"] = replanner_state
    return _continue_recovery(
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


def run_crashloop_recovery_from_payload(
    payload: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    return run_recovery_from_payload(
        payload,
        approve_action_id=approve_action_id,
        reject_action_id=reject_action_id,
        engine_mode=engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        reasoner_llm=reasoner_llm,
        critic_llm=critic_llm,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
        execution_worker_client=execution_worker_client,
    )


def run_recovery_from_saved_plan(
    payload: dict[str, Any],
    saved_result: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    engine_mode: EngineMode | str = "v1",
    critic_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    engine_mode = _validate_engine_mode(str(saved_result.get("engine_mode", engine_mode)))
    if approve_action_id and reject_action_id:
        raise ValueError("Specify either approve_action_id or reject_action_id, not both.")
    if approve_action_id is None and reject_action_id is None:
        raise ValueError("resume-from-file requires --approve-action-id or --reject-action-id.")

    incidents = incidents_from_alertmanager_payload(payload)
    if len(incidents) != 1:
        raise ValueError("Recovery workflow expects exactly one incident per payload.")
    incident = incidents[0]

    fixer_state = _saved_mapping(saved_result.get("fixer_state"), field_name="fixer_state")
    judge_state = _saved_mapping(saved_result.get("judge_state"), field_name="judge_state")
    hitl_decision_payload = _saved_mapping(saved_result.get("hitl_decision"), field_name="hitl_decision")
    decision_trace_payload = _saved_mapping(saved_result.get("decision_trace"), field_name="decision_trace")
    observation_bundle = _saved_observation_bundle(saved_result.get("observation_bundle"))
    reasoner_state = _saved_optional_mapping(saved_result.get("reasoner_state"), field_name="reasoner_state")
    critic_state = _saved_optional_mapping(saved_result.get("critic_state"), field_name="critic_state")
    synthesizer_state = _saved_optional_mapping(
        saved_result.get("synthesizer_state"),
        field_name="synthesizer_state",
    )
    verifier_state = _saved_optional_mapping(saved_result.get("verifier_state"), field_name="verifier_state")
    replanner_state = _saved_optional_mapping(saved_result.get("replanner_state"), field_name="replanner_state")

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
    fixer_state["_engine_mode"] = engine_mode
    if observation_bundle is not None:
        fixer_state["_observation_bundle"] = observation_bundle
    if reasoner_state is not None:
        fixer_state["_reasoner_state"] = reasoner_state
    if critic_state is not None:
        fixer_state["_critic_state"] = critic_state
    if synthesizer_state is not None:
        fixer_state["_synthesizer_state"] = synthesizer_state
    if verifier_state is not None:
        fixer_state["_verifier_state"] = verifier_state
    if replanner_state is not None:
        fixer_state["_replanner_state"] = replanner_state
    return _continue_recovery(
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
    engine_mode: EngineMode | str = "v1",
    critic_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    return run_recovery_from_saved_plan(
        payload,
        saved_result,
        approve_action_id=approve_action_id,
        reject_action_id=reject_action_id,
        engine_mode=engine_mode,
        critic_llm=critic_llm,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
        execution_worker_client=execution_worker_client,
    )


def _plan_recovery(
    *,
    incident: Any,
    engine_mode: EngineMode,
    fixer_llm: Any = None,
    judge_llm: Any = None,
    observation_bundle: ObservationBundle | None = None,
    observation_run: dict[str, Any] | None = None,
    reasoner_state: dict[str, Any] | None = None,
    reasoner_run: dict[str, Any] | None = None,
    critic_state: dict[str, Any] | None = None,
    critic_run: dict[str, Any] | None = None,
    synthesizer_state: dict[str, Any] | None = None,
    synthesizer_run: dict[str, Any] | None = None,
    verifier_state: dict[str, Any] | None = None,
    replanner_state: dict[str, Any] | None = None,
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
    hitl_decision = route_plan(
        incident=incident,
        actions=fixer_state["actions"],
        fixer_rationale=fixer_state.get("fixer_rationale"),
        judge_verdict=judge_state["judge_verdict"],
        judge_reason=judge_state["judge_reason"],
    )
    trace = hitl_decision.decision_trace
    if engine_mode != "v1":
        trace = _attach_v2_shadow_fixer_plan(
            trace,
            incident=incident,
            engine_mode=engine_mode,
            observation_bundle=observation_bundle,
            observation_run=observation_run,
            reasoner_state=reasoner_state,
            critic_state=critic_state,
            synthesizer_state=synthesizer_state,
            verifier_state=verifier_state,
            replanner_state=replanner_state,
        )
    trace = initialize_trace_provenance(trace)
    if observation_run is not None:
        trace = append_node_run(
            trace,
            node_name="observe",
            status=str(observation_run["status"]),
            summary=str(observation_run["summary"]),
            input_summary=dict(observation_run["input_summary"]),
            output_summary=dict(observation_run["output_summary"]),
            artifact_refs=list(observation_run.get("artifact_refs", [])),
        )
    if reasoner_run is not None:
        trace = append_node_run(
            trace,
            node_name="reason",
            status=str(reasoner_run["status"]),
            summary=str(reasoner_run["summary"]),
            llm_explanation=_truncate_text(reasoner_state.get("raw_reasoner_output")) if reasoner_state else None,
            input_summary=dict(reasoner_run["input_summary"]),
            output_summary=dict(reasoner_run["output_summary"]),
            artifact_refs=list(reasoner_run.get("artifact_refs", [])),
        )
    if critic_run is not None:
        trace = append_node_run(
            trace,
            node_name="critique",
            status=str(critic_run["status"]),
            summary=str(critic_run["summary"]),
            llm_explanation=_truncate_text(critic_state.get("raw_critic_output")) if critic_state else None,
            input_summary=dict(critic_run["input_summary"]),
            output_summary=dict(critic_run["output_summary"]),
            artifact_refs=list(critic_run.get("artifact_refs", [])),
        )
    if synthesizer_run is not None:
        trace = append_node_run(
            trace,
            node_name="synthesize",
            status=str(synthesizer_run["status"]),
            summary=str(synthesizer_run["summary"]),
            llm_explanation=_truncate_text((synthesizer_state or {}).get("failure_reason")),
            input_summary=dict(synthesizer_run["input_summary"]),
            output_summary=dict(synthesizer_run["output_summary"]),
            artifact_refs=list(synthesizer_run.get("artifact_refs", [])),
        )
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


def _continue_recovery(
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
    incident_class = normalize_incident_class(str(incident.incident_class))
    if incident_class == "crashloop":
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
    if incident_class == "bad_config":
        return _continue_bad_config_recovery(
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
    if incident_class == "cpu_saturation":
        return _continue_cpu_saturation_recovery(
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
    if incident_class == "network_partition":
        return _continue_network_partition_recovery(
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
    raise ValueError(f"Unsupported incident_class for recovery workflow: {incident.incident_class!r}")


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
    shadow_context = _build_shadow_followup_context(
        incident=incident,
        fixer_state=fixer_state,
        hitl_decision=hitl_decision,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
    )

    def append_finalization(trace: DecisionTrace) -> DecisionTrace:
        return _append_finalization_run(trace, shadow_context=shadow_context)

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
            decision_trace = append_finalization(decision_trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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
        timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
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
            "rollout_status": rollout_status.get("status"),
        },
    )
    rollback_triggered, rollback_result, post_rollback_check = _attempt_bounded_rollback(
        action=approved_action,
        kubernetes=kubernetes,
        prometheus=prometheus,
        namespace=namespace,
        deployment=deployment,
        post_check_fn=prometheus.post_check_crashloop,
        apply_kubernetes_fallback=True,
        failure_reason=(
            "Approved action rollout did not converge."
            if rollout_status["status"] != "succeeded"
            else "Post-check verification did not confirm recovery."
        ),
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
    trace = append_finalization(trace)
    return _build_result(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        decision_trace=trace,
    )


def _continue_bad_config_recovery(
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
    shadow_context = _build_shadow_followup_context(
        incident=incident,
        fixer_state=fixer_state,
        hitl_decision=hitl_decision,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
    )

    def append_finalization(trace: DecisionTrace) -> DecisionTrace:
        return _append_finalization_run(trace, shadow_context=shadow_context)

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
            decision_trace = append_finalization(decision_trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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

    if approved_action.action_type != "rollout_undo_deployment":
        final_state = "escalated"
        execution_result = {
            "status": "not_executed",
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
            "reason": "Approved action does not execute the bounded bad-config rollout rollback step.",
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
        trace = append_finalization(trace)
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

    pre_check = prometheus.pre_check_bad_config(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="pre_check",
        status=str(pre_check["status"]),
        summary="Prometheus pre-check evaluated whether frontend bad-config recovery should execute.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
        },
        output_summary={
            "status": pre_check["status"],
            "probe_success": pre_check["probe_success"],
            "attempts": pre_check["attempts"],
            "should_execute": pre_check["should_execute"],
            "missing_probe_telemetry": pre_check.get("missing_probe_telemetry"),
        },
    )
    if not bool(pre_check["should_execute"]):
        final_state = "recovered" if pre_check["status"] == "not_firing" else "escalated"
        verification_result = {
            "status": pre_check["status"],
            "reason": (
                "Frontend /cart probe was not failing at execution time."
                if pre_check["status"] == "not_firing"
                else "Frontend /cart probe telemetry was unavailable at execution time."
            ),
            "pre_check": pre_check,
        }
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "skipped",
                "reason": "Pre-check determined no bad-config action was necessary.",
            },
            verification_result=verification_result,
            final_state=final_state,
        )
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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
        timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
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
    post_check = prometheus.post_check_bad_config(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="post_check",
        status=str(post_check["status"]),
        summary="Post-check verification evaluated whether frontend bad-config recovery succeeded after execution.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "action_id": approved_action.action_id,
        },
        output_summary={
            "status": post_check["status"],
            "probe_success": post_check.get("probe_success"),
            "ready_count": post_check.get("ready_count"),
            "attempts": post_check.get("attempts"),
            "rollout_status": rollout_status.get("status"),
            "missing_probe_telemetry": post_check.get("missing_probe_telemetry"),
        },
    )
    final_state = "recovered" if post_check["status"] == "recovered" else "escalated"
    verification_result = {"pre_check": pre_check, "post_check": post_check}
    verification_result["recovery_latency_seconds"] = _recovery_latency_seconds(dispatch.requested_at)
    trace = finalize_decision_trace(
        trace,
        execution_result=execution_result,
        verification_result=verification_result,
        final_state=final_state,
        rollback_triggered=False,
    )
    trace = append_finalization(trace)
    return _build_result(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        decision_trace=trace,
    )


def _continue_cpu_saturation_recovery(
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
    shadow_context = _build_shadow_followup_context(
        incident=incident,
        fixer_state=fixer_state,
        hitl_decision=hitl_decision,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
    )

    def append_finalization(trace: DecisionTrace) -> DecisionTrace:
        return _append_finalization_run(trace, shadow_context=shadow_context)

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
            decision_trace = append_finalization(decision_trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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

    if approved_action.action_type != "delete_stresschaos":
        final_state = "escalated"
        execution_result = {
            "status": "not_executed",
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
            "reason": "Approved action does not execute the bounded CPU remediation step.",
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
        trace = append_finalization(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    namespace = str(approved_action.parameters["namespace"])
    deployment = _deployment_for_action(approved_action)
    chaos_name = str(approved_action.parameters["name"])
    prometheus = prometheus_client or PrometheusClient()
    worker_client = execution_worker_client or ExecutionWorkerClient()

    pre_check = prometheus.pre_check_cpu_saturation(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="pre_check",
        status=str(pre_check["status"]),
        summary="Prometheus pre-check evaluated whether frontend CPU saturation recovery should execute.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "chaos_name": chaos_name,
        },
        output_summary={
            "status": pre_check["status"],
            "cpu_usage": pre_check["cpu_usage"],
            "attempts": pre_check["attempts"],
            "should_execute": pre_check["should_execute"],
        },
    )
    if not bool(pre_check["should_execute"]):
        verification_result = {
            "status": "recovered",
            "reason": "Frontend CPU saturation was not firing at execution time.",
            "pre_check": pre_check,
        }
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "skipped",
                "reason": "Pre-check determined no CPU saturation action was necessary.",
            },
            verification_result=verification_result,
            final_state="recovered",
        )
        trace = append_finalization(trace)
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
                    "reason": "Post-check did not run because the execution worker failed before CPU recovery verification.",
                },
            },
            final_state="escalated",
        )
        trace = append_finalization(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    post_check = prometheus.post_check_cpu_saturation(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="post_check",
        status=str(post_check["status"]),
        summary="Post-check verification evaluated whether frontend CPU saturation recovered after execution.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "chaos_name": chaos_name,
            "action_id": approved_action.action_id,
        },
        output_summary={
            "status": post_check["status"],
            "cpu_usage": post_check.get("cpu_usage"),
            "ready_count": post_check.get("ready_count"),
            "attempts": post_check.get("attempts"),
        },
    )
    final_state = "recovered" if post_check["status"] == "recovered" else "escalated"
    verification_result = {"pre_check": pre_check, "post_check": post_check}
    verification_result["recovery_latency_seconds"] = _recovery_latency_seconds(dispatch.requested_at)
    trace = finalize_decision_trace(
        trace,
        execution_result=execution_result,
        verification_result=verification_result,
        final_state=final_state,
    )
    trace = append_finalization(trace)
    return _build_result(
        incident=incident,
        fixer_state=fixer_state,
        judge_state=judge_state,
        hitl_decision=hitl_decision,
        decision_trace=trace,
    )


def _continue_network_partition_recovery(
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
    shadow_context = _build_shadow_followup_context(
        incident=incident,
        fixer_state=fixer_state,
        hitl_decision=hitl_decision,
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
    )

    def append_finalization(trace: DecisionTrace) -> DecisionTrace:
        return _append_finalization_run(trace, shadow_context=shadow_context)

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
            decision_trace = append_finalization(decision_trace)
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
        trace = append_finalization(trace)
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
        trace = append_finalization(trace)
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

    if approved_action.action_type != "delete_networkchaos":
        final_state = "escalated"
        execution_result = {
            "status": "not_executed",
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
            "reason": "Approved action does not execute the bounded network partition remediation step.",
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
        trace = append_finalization(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    namespace = str(approved_action.parameters["namespace"])
    deployment = _deployment_for_action(approved_action)
    chaos_name = str(approved_action.parameters["name"])
    prometheus = prometheus_client or PrometheusClient()
    worker_client = execution_worker_client or ExecutionWorkerClient()

    pre_check = prometheus.pre_check_network_partition(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="pre_check",
        status=str(pre_check["status"]),
        summary="Prometheus pre-check evaluated whether frontend-to-cartservice partition recovery should execute.",
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "chaos_name": chaos_name,
        },
        output_summary={
            "status": pre_check["status"],
            "network_receive_rate": pre_check["network_receive_rate"],
            "attempts": pre_check["attempts"],
            "should_execute": pre_check["should_execute"],
            "missing_network_telemetry": pre_check.get("missing_network_telemetry"),
        },
    )
    if not bool(pre_check["should_execute"]):
        final_state = "recovered" if pre_check["status"] == "not_firing" else "escalated"
        verification_result = {
            "status": pre_check["status"],
            "reason": (
                "Network-partition signal was not firing at execution time."
                if pre_check["status"] == "not_firing"
                else "Cartservice network telemetry was unavailable at execution time."
            ),
            "pre_check": pre_check,
        }
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "skipped",
                "reason": "Pre-check determined no network-partition action was necessary.",
            },
            verification_result=verification_result,
            final_state=final_state,
        )
        trace = append_finalization(trace)
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
                    "reason": "Post-check did not run because the execution worker failed before network partition verification.",
                },
            },
            final_state="escalated",
        )
        trace = append_finalization(trace)
        return _build_result(
            incident=incident,
            fixer_state=fixer_state,
            judge_state=judge_state,
            hitl_decision=hitl_decision,
            decision_trace=trace,
        )

    post_check = prometheus.post_check_network_partition(namespace=namespace, deployment=deployment)
    trace = append_node_run(
        trace,
        node_name="post_check",
        status=str(post_check["status"]),
        summary=(
            "Post-check verification evaluated whether frontend-to-cartservice "
            "network partition recovered after execution."
        ),
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "chaos_name": chaos_name,
            "action_id": approved_action.action_id,
        },
        output_summary={
            "status": post_check["status"],
            "network_receive_rate": post_check.get("network_receive_rate"),
            "ready_count": post_check.get("ready_count"),
            "attempts": post_check.get("attempts"),
            "missing_network_telemetry": post_check.get("missing_network_telemetry"),
        },
    )
    final_state = "recovered" if post_check["status"] == "recovered" else "escalated"
    verification_result = {"pre_check": pre_check, "post_check": post_check}
    verification_result["recovery_latency_seconds"] = _recovery_latency_seconds(dispatch.requested_at)
    trace = finalize_decision_trace(
        trace,
        execution_result=execution_result,
        verification_result=verification_result,
        final_state=final_state,
    )
    trace = append_finalization(trace)
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
    result: dict[str, object] = {
        "status": worker_result.status,
        "action_id": action.action_id,
        "action_type": action.action_type,
        "namespace": namespace,
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
    if action.action_type in {"delete_stresschaos", "delete_networkchaos"}:
        result["name"] = str(action.parameters["name"])
    else:
        result["deployment"] = str(action.parameters["deployment"])
    return result


def _build_result(
    *,
    incident: Any,
    fixer_state: dict[str, Any],
    judge_state: dict[str, Any],
    hitl_decision: HITLDecision,
    decision_trace: DecisionTrace,
) -> dict[str, Any]:
    observation_bundle = fixer_state.get("_observation_bundle")
    reasoner_state = fixer_state.get("_reasoner_state")
    critic_state = fixer_state.get("_critic_state")
    synthesizer_state = fixer_state.get("_synthesizer_state")
    verifier_state = fixer_state.get("_verifier_state")
    replanner_state = fixer_state.get("_replanner_state")
    return {
        "incident": incident,
        "engine_mode": str(fixer_state.get("_engine_mode", "v1")),
        "observation_bundle": observation_bundle,
        "reasoner_state": reasoner_state,
        "critic_state": critic_state,
        "synthesizer_state": synthesizer_state,
        "verifier_state": verifier_state,
        "replanner_state": replanner_state,
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


def _saved_observation_bundle(value: Any) -> ObservationBundle | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        raise TypeError("saved observation_bundle must be an object")
    return observation_bundle_from_dict(value)


def _saved_optional_mapping(value: Any, *, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
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


def _validate_engine_mode(value: str) -> EngineMode:
    if value not in VALID_ENGINE_MODES:
        raise ValueError(f"Unsupported engine_mode: {value!r}")
    return value


def _collect_observations(
    *,
    incident: Any,
    kubernetes_client: KubernetesClient | None,
    prometheus_client: PrometheusClient | None,
    engine_mode: EngineMode,
) -> tuple[ObservationBundle | None, dict[str, Any]]:
    observer = ClusterObserver(
        kubernetes_client=kubernetes_client,
        prometheus_client=prometheus_client,
    )
    try:
        observation_bundle = observer.collect(incident=incident)
    except Exception as exc:
        return None, {
            "status": "failed",
            "summary": f"Observation step failed in {engine_mode} and HERALD continued on the bounded v1 path.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "engine_mode": engine_mode,
            },
            "output_summary": {
                "error": str(exc),
            },
        }

    return observation_bundle, {
        "status": "succeeded",
        "summary": f"Observation step collected live cluster context for {engine_mode} handoff.",
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "engine_mode": engine_mode,
        },
        "output_summary": {
            "namespace_hint": observation_bundle.namespace_hint,
            "incident_class_hint": observation_bundle.incident_class_hint,
            "kubernetes_sections": sorted(observation_bundle.kubernetes.keys()),
            "prometheus_sections": sorted(observation_bundle.prometheus.keys()),
            "error_count": len(observation_bundle.errors),
        },
    }


def _run_shadow_reasoner(
    *,
    incident: Any,
    observation_bundle: ObservationBundle | None,
    reasoner_llm: Any,
    engine_mode: EngineMode,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if observation_bundle is None:
        failure_reason = "Observation bundle unavailable; skipped shadow reasoning."
        return {
            "incident_summary": "",
            "incident_class_hint": normalize_incident_class(str(incident.incident_class)),
            "reasoner_output": None,
            "mapped_v1_candidates": [],
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }, {
            "status": "failed",
            "summary": f"Reasoner skipped in {engine_mode} because observation data was unavailable.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "engine_mode": engine_mode,
            },
            "output_summary": {
                "intent_count": 0,
                "mapped_candidate_count": 0,
                "error_count": 1,
                "failure_reason": failure_reason,
            },
        }

    try:
        reasoner_state = run_reasoner_pipeline(
            incident,
            observation_bundle,
            llm=reasoner_llm,
            capability_catalog=default_capability_catalog(),
        )
    except Exception as exc:
        failure_reason = f"Reasoner pipeline failed unexpectedly: {exc}"
        reasoner_state = {
            "incident_summary": "",
            "incident_class_hint": observation_bundle.incident_class_hint,
            "reasoner_output": None,
            "mapped_v1_candidates": [],
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }

    reasoner_output = reasoner_state.get("reasoner_output")
    intent_count = len(reasoner_output.intents) if reasoner_output is not None else 0
    mapped_candidates = reasoner_state.get("mapped_v1_candidates", [])
    if reasoner_state.get("status") == "failed":
        summary = f"Reasoner failed in {engine_mode} and HERALD continued on the bounded v1 path."
    else:
        summary = f"Reasoner emitted shadow intents for {engine_mode} handoff."

    output_summary = {
        "intent_count": intent_count,
        "mapped_candidate_count": len(mapped_candidates),
        "error_count": len(reasoner_state.get("errors", [])),
    }
    if reasoner_state.get("failure_reason"):
        output_summary["failure_reason"] = reasoner_state["failure_reason"]

    return reasoner_state, {
        "status": reasoner_state.get("status", "failed"),
        "summary": summary,
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "engine_mode": engine_mode,
        },
        "output_summary": output_summary,
    }


def _run_shadow_critic(
    *,
    incident: Any,
    observation_bundle: ObservationBundle | None,
    reasoner_state: dict[str, Any] | None,
    critic_llm: Any,
    engine_mode: EngineMode,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reasoner_output = (reasoner_state or {}).get("reasoner_output")
    if observation_bundle is None or reasoner_output is None:
        failure_reason = "Reasoner output unavailable; skipped shadow critique."
        return {
            "incident_summary": "",
            "critic_output": None,
            "policy_summary": {},
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }, {
            "status": "failed",
            "summary": f"Critic skipped in {engine_mode} because reasoner output was unavailable.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "engine_mode": engine_mode,
            },
            "output_summary": {
                "candidate_count": 0,
                "approved_candidate_count": 0,
                "escalation_recommended": False,
                "error_count": 1,
                "failure_reason": failure_reason,
            },
        }

    try:
        critic_state = run_critic_pipeline(
            incident,
            observation_bundle,
            reasoner_output,
            llm=critic_llm,
            capability_catalog=default_capability_catalog(),
        )
    except Exception as exc:
        failure_reason = f"Critic pipeline failed unexpectedly: {exc}"
        critic_state = {
            "incident_summary": "",
            "critic_output": None,
            "policy_summary": {},
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }

    critic_output = critic_state.get("critic_output")
    policy_summary = dict(critic_state.get("policy_summary", {}))
    if critic_state.get("status") == "failed":
        summary = f"Critic failed in {engine_mode} and HERALD continued on the bounded v1 path."
    else:
        summary = f"Critic emitted shadow policy analysis for {engine_mode} handoff."

    output_summary = {
        "candidate_count": len(critic_output.candidates) if critic_output is not None else 0,
        "approved_candidate_count": int(policy_summary.get("approved_candidate_count", 0)),
        "escalation_recommended": bool(policy_summary.get("escalation_recommended", False)),
        "error_count": len(critic_state.get("errors", [])),
    }
    if critic_state.get("failure_reason"):
        output_summary["failure_reason"] = critic_state["failure_reason"]

    return critic_state, {
        "status": critic_state.get("status", "failed"),
        "summary": summary,
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "engine_mode": engine_mode,
        },
        "output_summary": output_summary,
    }


def _run_shadow_synthesizer(
    *,
    incident: Any,
    observation_bundle: ObservationBundle | None,
    reasoner_state: dict[str, Any] | None,
    critic_state: dict[str, Any] | None,
    engine_mode: EngineMode,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reasoner_output = (reasoner_state or {}).get("reasoner_output")
    critic_output = (critic_state or {}).get("critic_output")
    if observation_bundle is None or reasoner_output is None:
        failure_reason = "Reasoner output unavailable; skipped shadow synthesis."
        return {
            "incident_summary": "",
            "synthesis_output": None,
            "synthesized_v1_dispatches": [],
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }, {
            "status": "failed",
            "summary": f"Synthesizer skipped in {engine_mode} because reasoner output was unavailable.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "engine_mode": engine_mode,
            },
            "output_summary": {
                "plan_count": 0,
                "dispatch_count": 0,
                "unsupported_intent_count": 0,
                "warning_count": 1,
                "failure_reason": failure_reason,
            },
        }

    try:
        synthesizer_state = run_synthesizer_pipeline(
            incident,
            observation_bundle,
            reasoner_output,
            critic_output,
        )
    except Exception as exc:
        failure_reason = f"Synthesizer pipeline failed unexpectedly: {exc}"
        synthesizer_state = {
            "incident_summary": "",
            "synthesis_output": None,
            "synthesized_v1_dispatches": [],
            "errors": [failure_reason],
            "final": True,
            "status": "failed",
            "failure_reason": failure_reason,
        }

    synthesis_output = synthesizer_state.get("synthesis_output")
    synthesized_v1_dispatches = list(synthesizer_state.get("synthesized_v1_dispatches", []))
    if synthesizer_state.get("status") == "failed":
        summary = f"Synthesizer failed in {engine_mode} and HERALD continued on the bounded v1 path."
    else:
        summary = f"Synthesizer emitted bounded shadow execution plans for {engine_mode} handoff."

    output_summary = {
        "plan_count": len(synthesis_output.plans) if synthesis_output is not None else 0,
        "dispatch_count": len(synthesized_v1_dispatches),
        "unsupported_intent_count": len(synthesis_output.unsupported_intents) if synthesis_output is not None else 0,
        "warning_count": len(synthesis_output.warnings) if synthesis_output is not None else 0,
        "error_count": len(synthesizer_state.get("errors", [])),
    }
    if synthesizer_state.get("failure_reason"):
        output_summary["failure_reason"] = synthesizer_state["failure_reason"]

    return synthesizer_state, {
        "status": synthesizer_state.get("status", "failed"),
        "summary": summary,
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "engine_mode": engine_mode,
        },
        "output_summary": output_summary,
    }


def _attach_v2_shadow_fixer_plan(
    trace: DecisionTrace,
    *,
    incident: Any,
    engine_mode: EngineMode,
    observation_bundle: ObservationBundle | None,
    observation_run: dict[str, Any] | None,
    reasoner_state: dict[str, Any] | None,
    critic_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
    verifier_state: dict[str, Any] | None,
    replanner_state: dict[str, Any] | None,
) -> DecisionTrace:
    fixer_plan = dict(trace.fixer_plan)
    fixer_plan["v2_shadow"] = _build_v2_shadow_payload(
        incident=incident,
        engine_mode=engine_mode,
        observation_bundle=observation_bundle,
        observation_run=observation_run,
        reasoner_state=reasoner_state,
        critic_state=critic_state,
        synthesizer_state=synthesizer_state,
        verifier_state=verifier_state,
        replanner_state=replanner_state,
    )
    return replace(trace, fixer_plan=fixer_plan)


def _build_v2_shadow_payload(
    *,
    incident: Any,
    engine_mode: EngineMode,
    observation_bundle: ObservationBundle | None,
    observation_run: dict[str, Any] | None,
    reasoner_state: dict[str, Any] | None,
    critic_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
    verifier_state: dict[str, Any] | None,
    replanner_state: dict[str, Any] | None,
) -> dict[str, Any]:
    reasoner_status = str((reasoner_state or {}).get("status", "failed"))
    critic_status = str((critic_state or {}).get("status", "failed"))
    synthesis_status = str((synthesizer_state or {}).get("status", "failed"))
    verification_status = str((verifier_state or {}).get("verification_status", "not_run"))
    replanner_status = str((replanner_state or {}).get("status", "not_run"))
    overall_status = (
        "succeeded"
        if reasoner_status == "succeeded"
        and critic_status == "succeeded"
        and synthesis_status == "succeeded"
        and verification_status in {"passed", "unrecovered", "not_run"}
        and replanner_status in {"succeeded", "not_run"}
        else "failed"
    )
    payload = {
        "engine_mode": engine_mode,
        "status": overall_status,
        "reasoner_status": reasoner_status,
        "critic_status": critic_status,
        "synthesis_status": synthesis_status,
        "verification_status": verification_status,
        "replanner_status": replanner_status,
        "observation_summary": _shadow_observation_summary(
            incident=incident,
            observation_bundle=observation_bundle,
            observation_run=observation_run,
        ),
        "reasoner_output": _to_jsonable((reasoner_state or {}).get("reasoner_output")),
        "mapped_v1_candidates": [
            _serialize_remediation_action(action)
            for action in list((reasoner_state or {}).get("mapped_v1_candidates", []))
            if isinstance(action, RemediationAction)
        ],
        "critic_output": _to_jsonable((critic_state or {}).get("critic_output")),
        "policy_summary": _to_jsonable((critic_state or {}).get("policy_summary", {})),
        "synthesis_output": _to_jsonable((synthesizer_state or {}).get("synthesis_output")),
        "synthesized_v1_dispatches": _to_jsonable(list((synthesizer_state or {}).get("synthesized_v1_dispatches", []))),
        "verification_plan": _to_jsonable((verifier_state or {}).get("verification_plan")),
        "verification_result_v2": _to_jsonable((verifier_state or {}).get("verification_result_v2")),
        "replan_output": _to_jsonable((replanner_state or {}).get("replan_output")),
    }
    failure_reason = (reasoner_state or {}).get("failure_reason")
    if isinstance(failure_reason, str) and failure_reason:
        payload["failure_reason"] = failure_reason
    critic_failure_reason = (critic_state or {}).get("failure_reason")
    if isinstance(critic_failure_reason, str) and critic_failure_reason:
        payload["critic_failure_reason"] = critic_failure_reason
    synthesis_failure_reason = (synthesizer_state or {}).get("failure_reason")
    if isinstance(synthesis_failure_reason, str) and synthesis_failure_reason:
        payload["synthesis_failure_reason"] = synthesis_failure_reason
    verification_failure_reason = (
        (verifier_state or {}).get("verification_failure_reason")
        or (verifier_state or {}).get("failure_reason")
    )
    if isinstance(verification_failure_reason, str) and verification_failure_reason:
        payload["verification_failure_reason"] = verification_failure_reason
    replan_failure_reason = (
        (replanner_state or {}).get("replan_failure_reason")
        or (replanner_state or {}).get("failure_reason")
    )
    if isinstance(replan_failure_reason, str) and replan_failure_reason:
        payload["replan_failure_reason"] = replan_failure_reason
    return payload


def _shadow_observation_summary(
    *,
    incident: Any,
    observation_bundle: ObservationBundle | None,
    observation_run: dict[str, Any] | None,
) -> dict[str, Any]:
    if observation_bundle is not None:
        return {
            "incident_id": observation_bundle.incident_id,
            "incident_class_hint": observation_bundle.incident_class_hint,
            "namespace_hint": observation_bundle.namespace_hint,
            "kubernetes_sections": sorted(observation_bundle.kubernetes.keys()),
            "prometheus_sections": sorted(observation_bundle.prometheus.keys()),
            "error_count": len(observation_bundle.errors),
        }

    output_summary = dict((observation_run or {}).get("output_summary", {}))
    return {
        "incident_id": incident.incident_id,
        "incident_class_hint": str(
            output_summary.get("incident_class_hint") or normalize_incident_class(str(incident.incident_class))
        ),
        "namespace_hint": output_summary.get("namespace_hint"),
        "kubernetes_sections": list(output_summary.get("kubernetes_sections", [])),
        "prometheus_sections": list(output_summary.get("prometheus_sections", [])),
        "error_count": int(output_summary.get("error_count", 1 if observation_run else 0)),
    }


def _serialize_remediation_action(action: RemediationAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "description": action.description,
        "confidence_score": action.confidence_score,
        "blast_radius_score": action.blast_radius_score,
        "requires_approval": action.requires_approval,
        "parameters": dict(action.parameters),
    }


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
    target_label = _action_target_label(action)
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
        f"({action.action_type}) for {target_label} in namespace {namespace!r}. "
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
    post_check_fn: Any,
    apply_kubernetes_fallback: bool,
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
        timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
    )
    rollback_rollout_status = rollback_record["rollout_status"]
    if not isinstance(rollback_rollout_status, dict) or rollback_rollout_status.get("status") != "succeeded":
        return True, rollback_record, None

    post_rollback_check = post_check_fn(namespace=namespace, deployment=deployment)
    if apply_kubernetes_fallback:
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


def _append_finalization_run(
    trace: DecisionTrace,
    *,
    shadow_context: dict[str, Any] | None = None,
) -> DecisionTrace:
    trace = _append_shadow_followup_runs(trace, shadow_context=shadow_context)
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


def _build_shadow_followup_context(
    *,
    incident: Any,
    fixer_state: dict[str, Any],
    hitl_decision: HITLDecision,
    kubernetes_client: KubernetesClient | None,
    prometheus_client: PrometheusClient | None,
) -> dict[str, Any] | None:
    if str(fixer_state.get("_engine_mode", "v1")) != "v2_shadow":
        return None
    return {
        "incident": incident,
        "fixer_state": fixer_state,
        "hitl_decision": hitl_decision,
        "engine_mode": str(fixer_state.get("_engine_mode", "v2_shadow")),
        "kubernetes_client": kubernetes_client,
        "prometheus_client": prometheus_client,
    }


def _append_shadow_followup_runs(
    trace: DecisionTrace,
    *,
    shadow_context: dict[str, Any] | None,
) -> DecisionTrace:
    if shadow_context is None:
        return trace

    verifier_state, verify_run = _run_shadow_verifier(
        trace=trace,
        incident=shadow_context["incident"],
        fixer_state=shadow_context["fixer_state"],
        hitl_decision=shadow_context["hitl_decision"],
        prometheus_client=shadow_context.get("prometheus_client"),
        kubernetes_client=shadow_context.get("kubernetes_client"),
    )
    shadow_context["fixer_state"]["_verifier_state"] = verifier_state
    trace = append_node_run(
        trace,
        node_name="verify",
        status=str(verify_run["status"]),
        summary=str(verify_run["summary"]),
        input_summary=dict(verify_run["input_summary"]),
        output_summary=dict(verify_run["output_summary"]),
        artifact_refs=list(verify_run.get("artifact_refs", [])),
    )
    replanner_state, replan_run = _run_shadow_replanner(
        trace=trace,
        incident=shadow_context["incident"],
        fixer_state=shadow_context["fixer_state"],
        hitl_decision=shadow_context["hitl_decision"],
    )
    shadow_context["fixer_state"]["_replanner_state"] = replanner_state
    trace = _attach_v2_shadow_fixer_plan(
        trace,
        incident=shadow_context["incident"],
        engine_mode=str(shadow_context.get("engine_mode", "v2_shadow")),
        observation_bundle=shadow_context["fixer_state"].get("_observation_bundle"),
        observation_run=None,
        reasoner_state=shadow_context["fixer_state"].get("_reasoner_state"),
        critic_state=shadow_context["fixer_state"].get("_critic_state"),
        synthesizer_state=shadow_context["fixer_state"].get("_synthesizer_state"),
        verifier_state=verifier_state,
        replanner_state=replanner_state,
    )
    if replan_run is not None:
        trace = append_node_run(
            trace,
            node_name="replan",
            status=str(replan_run["status"]),
            summary=str(replan_run["summary"]),
            input_summary=dict(replan_run["input_summary"]),
            output_summary=dict(replan_run["output_summary"]),
            artifact_refs=list(replan_run.get("artifact_refs", [])),
        )
    return trace


def _run_shadow_verifier(
    *,
    trace: DecisionTrace,
    incident: Any,
    fixer_state: dict[str, Any],
    hitl_decision: HITLDecision,
    prometheus_client: PrometheusClient | None,
    kubernetes_client: KubernetesClient | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    execution_result = dict(trace.execution_result)
    verification_result = dict(trace.verification_result)
    approved_action = _approved_action_for_execution(trace, hitl_decision)
    if approved_action is None:
        failure_reason = "No approved action was available for shadow verification."
        verifier_state = {
            "verification_plan": None,
            "verification_result_v2": None,
            "verification_status": "not_run",
            "errors": [failure_reason],
            "final": True,
            "status": "not_run",
            "failure_reason": failure_reason,
        }
        return verifier_state, {
            "status": "not_run",
            "summary": "Shadow verification was not run because no approved action could be resolved.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "action_id": execution_result.get("action_id"),
                "action_type": execution_result.get("action_type"),
            },
            "output_summary": {
                "verification_status": "not_run",
                "check_count": 0,
                "passed_check_count": 0,
                "warning_count": 0,
                "failure_reason": failure_reason,
            },
        }

    execution_status = str(execution_result.get("status", "not_run"))
    if execution_status != "succeeded" or verification_result.get("post_check", {}).get("status") == "not_run":
        reason = "Shadow verification did not run because the v1 action did not reach a real post-check."
        if trace.final_state in {"pending_approval", "rejected", "escalated"} or execution_status in {"failed", "not_executed", "halted", "skipped"}:
            reason = "Shadow verification did not run because the approved action did not execute to completion."
        verifier_state = {
            "verification_plan": None,
            "verification_result_v2": None,
            "verification_status": "not_run",
            "errors": [],
            "final": True,
            "status": "not_run",
        }
        return verifier_state, {
            "status": "not_run",
            "summary": reason,
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "action_id": approved_action.action_id,
                "action_type": approved_action.action_type,
            },
            "output_summary": {
                "verification_status": "not_run",
                "check_count": 0,
                "passed_check_count": 0,
                "warning_count": 0,
            },
        }

    synthesis_state = fixer_state.get("_synthesizer_state") or {}
    synthesis_output = synthesis_state.get("synthesis_output")
    observation_bundle = fixer_state.get("_observation_bundle")
    verification_plan = build_shadow_verification_plan(
        approved_action=approved_action,
        synthesis_output=synthesis_output,
        observation_bundle=observation_bundle,
    )
    if verification_plan is None:
        failure_reason = "Shadow verification could not build a runnable verification plan."
        verifier_state = {
            "verification_plan": None,
            "verification_result_v2": None,
            "verification_status": "not_run",
            "errors": [failure_reason],
            "final": True,
            "status": "not_run",
            "failure_reason": failure_reason,
        }
        return verifier_state, {
            "status": "not_run",
            "summary": failure_reason,
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "action_id": approved_action.action_id,
                "action_type": approved_action.action_type,
            },
            "output_summary": {
                "verification_status": "not_run",
                "check_count": 0,
                "passed_check_count": 0,
                "warning_count": 0,
                "failure_reason": failure_reason,
            },
        }

    try:
        verification_result_v2 = run_verification(
            verification_plan,
            prometheus=prometheus_client or PrometheusClient(),
            kubernetes=kubernetes_client or KubernetesClient(),
        )
    except Exception as exc:
        failure_reason = f"Shadow verification failed unexpectedly: {exc}"
        verification_result_v2 = VerificationResultV2(
            verification_id=verification_plan.verification_id,
            status="not_run",
            summary="Shadow verification failed to execute.",
            plan=verification_plan,
            check_results=[],
            warnings=[failure_reason],
            failure_reason=failure_reason,
        )
        verifier_state = {
            "verification_plan": verification_plan,
            "verification_result_v2": verification_result_v2,
            "verification_status": "not_run",
            "errors": [failure_reason],
            "final": True,
            "status": "not_run",
            "failure_reason": failure_reason,
        }
        return verifier_state, {
            "status": "not_run",
            "summary": "Shadow verification failed to execute.",
            "input_summary": {
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
                "action_id": approved_action.action_id,
                "action_type": approved_action.action_type,
            },
            "output_summary": {
                "verification_status": "not_run",
                "check_count": 0,
                "passed_check_count": 0,
                "warning_count": 1,
                "failure_reason": failure_reason,
            },
        }

    if trace.rollback_triggered or trace.final_state == "rolled_back":
        verification_result_v2 = replace(
            verification_result_v2,
            status="unrecovered",
            warnings=_dedupe_strings(
                list(verification_result_v2.warnings)
                + ["Rollback verification remains v1-only until Phase 5B."]
            ),
        )

    verifier_state = {
        "verification_plan": verification_plan,
        "verification_result_v2": verification_result_v2,
        "verification_status": verification_result_v2.status,
        "errors": [],
        "final": True,
        "status": verification_result_v2.status,
    }
    if verification_result_v2.failure_reason:
        verifier_state["failure_reason"] = verification_result_v2.failure_reason

    output_summary: dict[str, Any] = {
        "verification_status": verification_result_v2.status,
        "check_count": len(verification_result_v2.check_results),
        "passed_check_count": sum(1 for check_result in verification_result_v2.check_results if check_result.passed),
        "warning_count": len(verification_result_v2.warnings),
    }
    if verification_result_v2.failure_reason:
        output_summary["failure_reason"] = verification_result_v2.failure_reason

    return verifier_state, {
        "status": verification_result_v2.status,
        "summary": "Shadow verification evaluated the approved v1 action outcome.",
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "action_id": approved_action.action_id,
            "action_type": approved_action.action_type,
        },
        "output_summary": output_summary,
    }


def _run_shadow_replanner(
    *,
    trace: DecisionTrace,
    incident: Any,
    fixer_state: dict[str, Any],
    hitl_decision: HITLDecision,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    verifier_state = fixer_state.get("_verifier_state")
    approved_action = _approved_action_for_execution(trace, hitl_decision)
    replanner_state = run_replanner_pipeline(
        incident=incident,
        observations=fixer_state.get("_observation_bundle"),
        reasoner_output=(fixer_state.get("_reasoner_state") or {}).get("reasoner_output"),
        verifier_state=verifier_state,
        trace=trace,
        approved_action=approved_action,
    )
    if replanner_state.get("status") == "not_run":
        return replanner_state, None

    replan_output = replanner_state.get("replan_output")
    output_summary = {
        "decision": replan_output.decision if replan_output is not None else None,
        "proposed_intent_ids": [intent.intent_id for intent in replan_output.intents] if replan_output else [],
        "stop_reason": replan_output.stop_reason if replan_output is not None else None,
        "error_count": len(replanner_state.get("errors", [])),
    }
    if replanner_state.get("failure_reason"):
        output_summary["failure_reason"] = replanner_state["failure_reason"]

    summary = "Shadow replanner evaluated the unrecovered verification result."
    if replan_output is not None and replan_output.decision == "propose_new_intent":
        summary = "Shadow replanner proposed a bounded alternative intent after unrecovered verification."
    if replan_output is not None and replan_output.decision == "escalate":
        summary = "Shadow replanner recommended escalation after unrecovered verification."

    return replanner_state, {
        "status": replanner_state.get("status", "failed"),
        "summary": summary,
        "input_summary": {
            "incident_id": incident.incident_id,
            "incident_class": incident.incident_class,
            "verification_status": (verifier_state or {}).get("verification_status"),
            "action_id": approved_action.action_id if approved_action else None,
            "action_type": approved_action.action_type if approved_action else None,
        },
        "output_summary": output_summary,
    }


def _approved_action_for_execution(trace: DecisionTrace, hitl_decision: HITLDecision) -> RemediationAction | None:
    action_id = str(trace.execution_result.get("action_id") or "")
    if not action_id:
        return None
    for action in hitl_decision.candidate_actions:
        if action.action_id == action_id:
            return action
    return None


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _truncate_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "...<truncated>"


def _deployment_for_action(action: RemediationAction) -> str:
    if "deployment" in action.parameters:
        return str(action.parameters["deployment"])
    if action.action_type == "delete_networkchaos":
        return "cartservice"
    return "frontend"


def _action_target_label(action: RemediationAction) -> str:
    if action.action_type == "delete_stresschaos":
        return f"StressChaos {str(action.parameters['name'])!r}"
    if action.action_type == "delete_networkchaos":
        return f"NetworkChaos {str(action.parameters['name'])!r}"
    return f"deployment {_deployment_for_action(action)!r}"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the HERALD recovery workflow from an Alertmanager payload."
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
    parser.add_argument("--reasoner-model", default="gemini-2.5-flash")
    parser.add_argument("--critic-model", default="gemini-2.5-flash")
    parser.add_argument("--prometheus-base-url")
    parser.add_argument(
        "--reasoner-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--critic-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--engine-mode",
        choices=VALID_ENGINE_MODES,
        default="v1",
    )
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
        result = run_recovery_from_saved_plan(
            payload,
            saved_result,
            approve_action_id=args.approve_action_id,
            reject_action_id=args.reject_action_id,
            engine_mode=args.engine_mode,
            critic_llm=None,
            prometheus_client=prometheus_client,
        )
    else:
        fixer_llm = None
        if args.fixer_provider == "gemini":
            fixer_llm = GeminiFixerLLM(model=args.fixer_model)

        judge_llm = None
        if args.judge_provider == "gemini":
            judge_llm = GeminiJudgeLLM(model=args.judge_model)
        reasoner_llm = None
        if args.reasoner_provider == "gemini":
            reasoner_llm = GeminiReasonerLLM(model=args.reasoner_model)
        critic_llm = None
        if args.critic_provider == "gemini":
            critic_llm = GeminiCriticLLM(model=args.critic_model)

        if args.interactive_hitl:
            planning_result = run_recovery_from_payload(
                payload,
                engine_mode=args.engine_mode,
                fixer_llm=fixer_llm,
                judge_llm=judge_llm,
                reasoner_llm=reasoner_llm,
                critic_llm=critic_llm,
                prometheus_client=prometheus_client,
            )
            result = _continue_with_interactive_hitl(
                payload=payload,
                planning_result=planning_result,
                prometheus_client=prometheus_client,
            )
        else:
            result = run_recovery_from_payload(
                payload,
                approve_action_id=args.approve_action_id,
                reject_action_id=args.reject_action_id,
                engine_mode=args.engine_mode,
                fixer_llm=fixer_llm,
                judge_llm=judge_llm,
                reasoner_llm=reasoner_llm,
                critic_llm=critic_llm,
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
    output_fn: Any = print,
) -> dict[str, Any]:
    hitl_decision = planning_result["hitl_decision"]
    recommended_action = hitl_decision["recommended_action"]
    if recommended_action is None:
        return planning_result

    if isinstance(recommended_action, dict):
        action_id = str(recommended_action["action_id"])
        action_type = str(recommended_action["action_type"])
    else:
        action_id = recommended_action.action_id
        action_type = recommended_action.action_type
    output_fn(
        f"HITL Gate: recommended action {action_id} ({action_type}).",
        flush=True,
    )
    output_fn("Enter 1 to approve or 2 to reject.", flush=True)
    while True:
        user_choice = str(input_fn("> ")).strip()
        if user_choice == "1":
            return run_recovery_from_saved_plan(
                payload,
                planning_result,
                approve_action_id=action_id,
                engine_mode=str(planning_result.get("engine_mode", "v1")),
                kubernetes_client=kubernetes_client,
                prometheus_client=prometheus_client,
                execution_worker_client=execution_worker_client,
            )
        if user_choice == "2":
            return run_recovery_from_saved_plan(
                payload,
                planning_result,
                reject_action_id=action_id,
                engine_mode=str(planning_result.get("engine_mode", "v1")),
                kubernetes_client=kubernetes_client,
                prometheus_client=prometheus_client,
                execution_worker_client=execution_worker_client,
            )
        output_fn("Enter 1 to approve or 2 to reject.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
