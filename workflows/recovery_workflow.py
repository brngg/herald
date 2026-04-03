from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from agents.critic import run_critic_pipeline
from agents.replanner import run_replanner_pipeline
from agents.reasoner import run_reasoner_pipeline
from agents.synthesizer import run_synthesizer_pipeline
from schemas.approval import ApprovalCandidate
from schemas.decision_trace import DecisionTrace
from schemas.execution import ExecutionResult
from schemas.execution_plan import ExecutionPlan
from schemas.intents import ResourceTarget
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from schemas.verification import VerificationResultV2
from services.alerts.alertmanager import incidents_from_alertmanager_payload
from services.recovery.capability_catalog import default_capability_catalog
from services.observability.cluster_observer import ClusterObserver
from services.llm.tasks.critic import GeminiCriticLLM
from services.normalization.incident import normalize_incident_class
from services.runtime.decision_trace import append_node_run, initialize_trace_provenance
from services.infra.kubernetes.execution_worker import ExecutionWorkerClient
from services.llm.tasks.reasoner import GeminiReasonerLLM
from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient
from services.recovery.verification_engine import build_shadow_verification_plan, run_verification
from workflows.hitl_gate import (
    HITLDecision,
    finalize_decision_trace,
    record_human_approval,
    route_candidates,
    route_plan,
)
from workflows.runtime.approval_adapters import (
    action_target_label as _action_target_label,
    approval_candidate_from_saved as _approval_candidate_from_saved,
    candidate_action_type as _candidate_action_type,
    candidate_check_hint as _candidate_check_hint,
    candidate_deployment as _candidate_deployment,
    candidate_display_labels as _candidate_display_labels,
    candidate_namespace as _candidate_namespace,
    candidate_target_name as _candidate_target_name,
    deployment_for_action as _deployment_for_action,
    dispatch_parameters_for_candidate as _dispatch_parameters_for_candidate,
    dispatch_parameters_match_action as _dispatch_parameters_match_action,
    execution_plan_from_legacy_action as _execution_plan_from_legacy_action,
    legacy_action_for_candidate as _legacy_action_for_candidate,
    legacy_action_hint_for_plan as _legacy_action_hint_for_plan,
    remediation_action_from_saved as _remediation_action_from_saved,
    select_candidate as _select_candidate,
    serialize_remediation_action as _serialize_remediation_action,
    upgrade_candidate_to_action_fallback as _upgrade_candidate_to_action_fallback,
    upgrade_legacy_action_to_candidate as _upgrade_legacy_action_to_candidate,
)
from workflows.runtime.saved_state import (
    hitl_decision_from_saved as _hitl_decision_from_saved,
    saved_mapping as _saved_mapping,
    saved_observation_bundle as _saved_observation_bundle,
    saved_optional_mapping as _saved_optional_mapping,
)
from workflows.runtime.result_payloads import (
    attach_v2_shadow_fixer_plan as _attach_v2_shadow_fixer_plan,
    build_result as _build_result,
    to_jsonable as _to_jsonable,
)
from workflows.runtime.execution_runtime import (
    append_rollback_run as _append_rollback_run,
    apply_kubernetes_recovery_fallback as _apply_kubernetes_recovery_fallback,
    attempt_bounded_rollback as _attempt_bounded_rollback,
    build_execution_dispatch_for_candidate as _build_execution_dispatch_for_candidate,
    build_execution_dispatch_for_mode as _build_execution_dispatch_for_mode,
    build_execution_result as _build_execution_result,
    build_execution_result_for_candidate as _build_execution_result_for_candidate,
    post_check_function_for_hint as _post_check_function_for_hint,
    post_check_observed_fields as _post_check_observed_fields,
    post_check_summary_for_hint as _post_check_summary_for_hint,
    pre_check_observed_fields as _pre_check_observed_fields,
    pre_check_skip_reason as _pre_check_skip_reason,
    pre_check_summary_for_hint as _pre_check_summary_for_hint,
    recovery_latency_seconds as _recovery_latency_seconds,
    run_post_check_for_candidate as _run_post_check_for_candidate,
    run_pre_check_for_candidate as _run_pre_check_for_candidate,
    wait_for_deployment_availability as _wait_for_deployment_availability,
)

ROLLOUT_WAIT_TIMEOUT_SECONDS = 60
ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS = 4
ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS = 5.0
EngineMode = Literal["v2_shadow", "v2_execute"]
LEGACY_ENGINE_MODE = "v1"
VALID_ENGINE_MODES: tuple[EngineMode, ...] = ("v2_shadow", "v2_execute")
DEFAULT_ENGINE_MODE: EngineMode = "v2_execute"


def run_recovery_from_payload(
    payload: dict[str, Any],
    *,
    approve_action_id: str | None = None,
    reject_action_id: str | None = None,
    engine_mode: EngineMode | str = DEFAULT_ENGINE_MODE,
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
    engine_mode: EngineMode | str = DEFAULT_ENGINE_MODE,
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
    engine_mode: EngineMode | str = DEFAULT_ENGINE_MODE,
    critic_llm: Any = None,
    kubernetes_client: KubernetesClient | None = None,
    prometheus_client: PrometheusClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
) -> dict[str, Any]:
    engine_mode = _validate_engine_mode(str(saved_result.get("engine_mode", engine_mode)), allow_legacy=True)
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

    hitl_decision = _hitl_decision_from_saved(
        hitl_decision_payload,
        decision_trace_payload=decision_trace_payload,
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
    engine_mode: EngineMode | str = DEFAULT_ENGINE_MODE,
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
    if engine_mode != "v1":
        fixer_state = _build_v2_compat_fixer_state(
            incident=incident,
            reasoner_state=reasoner_state,
            synthesizer_state=synthesizer_state,
        )
        candidate_options = _build_v2_candidate_options(
            incident=incident,
            fixer_state=fixer_state,
            reasoner_state=reasoner_state,
            critic_state=critic_state,
            synthesizer_state=synthesizer_state,
        )
        judge_state = _build_v2_judge_state(
            incident=incident,
            candidate_options=candidate_options,
            critic_state=critic_state,
            synthesizer_state=synthesizer_state,
        )
        hitl_decision = route_candidates(
            incident=incident,
            candidates=candidate_options,
            planner_summary=(synthesizer_state or {}).get("synthesis_output").summary
            if (synthesizer_state or {}).get("synthesis_output") is not None
            else (reasoner_state or {}).get("incident_summary"),
            judge_verdict=judge_state["judge_verdict"],
            judge_reason=judge_state["judge_reason"],
        )
        trace = hitl_decision.decision_trace
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
            summary="Legacy Fixer output was retained only as a compatibility hint while v2 planning remained authoritative.",
            llm_explanation=_truncate_text(fixer_state.get("fixer_rationale")),
            input_summary={
                "incident_id": incident.incident_id,
                "incident_class": incident.incident_class,
            },
            output_summary={
                "legacy_action_ids": [action.action_id for action in fixer_state["actions"]],
                "legacy_action_count": len(fixer_state["actions"]),
            },
        )
        trace = append_node_run(
            trace,
            node_name="judge",
            status=str(judge_state["judge_verdict"]),
            summary="Critic and deterministic policy validation evaluated the exact execution-plan candidates.",
            llm_explanation=_truncate_text(judge_state.get("judge_llm_reason")),
            input_summary={
                "incident_id": incident.incident_id,
                "candidate_ids": [candidate.candidate_id for candidate in hitl_decision.candidate_options],
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
            summary="HITL Gate ranked exact execution-plan candidates for approval.",
            input_summary={
                "judge_verdict": judge_state["judge_verdict"],
                "candidate_ids": [candidate.candidate_id for candidate in hitl_decision.candidate_options],
            },
            output_summary={
                "routing_decision": hitl_decision.routing_decision,
                "requires_approval": hitl_decision.requires_approval,
                "recommended_candidate_id": (
                    hitl_decision.recommended_candidate.candidate_id
                    if hitl_decision.recommended_candidate
                    else None
                ),
                "candidate_ids": [candidate.candidate_id for candidate in hitl_decision.candidate_options],
            },
        )
        hitl_decision = HITLDecision(
            routing_decision=hitl_decision.routing_decision,
            requires_approval=hitl_decision.requires_approval,
            recommended_candidate=hitl_decision.recommended_candidate,
            candidate_options=hitl_decision.candidate_options,
            decision_trace=trace,
        )
        return fixer_state, judge_state, hitl_decision

    from agents.fixer import run_fixer_pipeline
    from agents.judge import run_judge_pipeline

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
            "candidate_ids": [candidate.candidate_id for candidate in hitl_decision.candidate_options],
        },
        output_summary={
            "routing_decision": hitl_decision.routing_decision,
            "requires_approval": hitl_decision.requires_approval,
            "recommended_candidate_id": (
                hitl_decision.recommended_candidate.candidate_id
                if hitl_decision.recommended_candidate
                else None
            ),
            "candidate_ids": [candidate.candidate_id for candidate in hitl_decision.candidate_options],
        },
    )
    hitl_decision = HITLDecision(
        routing_decision=hitl_decision.routing_decision,
        requires_approval=hitl_decision.requires_approval,
        recommended_candidate=hitl_decision.recommended_candidate,
        candidate_options=hitl_decision.candidate_options,
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
    if str(fixer_state.get("_engine_mode", "v1")) != "v1":
        return _continue_v2_recovery(
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


def _continue_v2_recovery(
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
                    "reason": "HITL Gate escalated the exact execution-plan candidates before execution.",
                },
                verification_result={
                    "status": "not_run",
                    "reason": "Execution did not start because the HITL Gate halted the v2 plan.",
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
                "reason": "HITL Gate escalated the exact execution-plan candidates before execution.",
            },
            verification_result={
                "status": "not_run",
                "reason": "Execution did not start because the HITL Gate halted the v2 plan.",
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
        rejected_candidate = _select_candidate(hitl_decision, reject_action_id)
        trace = record_human_approval(
            hitl_decision.decision_trace,
            human_approval="rejected",
            final_state="rejected",
        )
        trace = append_node_run(
            trace,
            node_name="human_approval",
            status="rejected",
            summary="Human operator rejected the proposed exact execution plan.",
            input_summary={
                "candidate_id": rejected_candidate.candidate_id,
                "operation_family": rejected_candidate.execution_plan.operation_family,
            },
            output_summary={
                "human_approval": "rejected",
                "selected_candidate_id": rejected_candidate.candidate_id,
            },
        )
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "not_executed",
                "candidate_id": rejected_candidate.candidate_id,
                "operation_family": rejected_candidate.execution_plan.operation_family,
                "reason": "Human rejected the proposed exact execution plan.",
            },
            verification_result={
                "status": "not_run",
                "reason": "Execution was skipped because the human operator rejected the proposed plan.",
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

    approved_candidate = _select_candidate(hitl_decision, approve_action_id)
    approved_action = _legacy_action_for_candidate(approved_candidate)
    trace = record_human_approval(
        hitl_decision.decision_trace,
        human_approval="approved",
        final_state="executing",
    )
    trace = append_node_run(
        trace,
        node_name="human_approval",
        status="approved",
        summary="Human operator approved the exact execution plan.",
        input_summary={
            "candidate_id": approved_candidate.candidate_id,
            "operation_family": approved_candidate.execution_plan.operation_family,
        },
        output_summary={
            "human_approval": "approved",
            "selected_candidate_id": approved_candidate.candidate_id,
        },
    )

    if not approved_candidate.execution_plan.steps:
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "not_executed",
                "candidate_id": approved_candidate.candidate_id,
                "operation_family": approved_candidate.execution_plan.operation_family,
                "reason": "Approved candidate was non-executable and recommended escalation instead.",
            },
            verification_result={
                "status": "not_run",
                "reason": "No automated execution was attempted because the approved candidate was non-executable.",
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

    prometheus = prometheus_client or PrometheusClient()
    kubernetes = kubernetes_client or KubernetesClient()
    worker_client = execution_worker_client or ExecutionWorkerClient()
    namespace = _candidate_namespace(approved_candidate)
    deployment = _candidate_deployment(approved_candidate)
    target_name = _candidate_target_name(approved_candidate)
    check_hint = _candidate_check_hint(approved_candidate)

    pre_check = _run_pre_check_for_candidate(
        approved_candidate,
        prometheus=prometheus,
        namespace=namespace,
        deployment=deployment,
    )
    trace = append_node_run(
        trace,
        node_name="pre_check",
        status=str(pre_check["status"]),
        summary=_pre_check_summary_for_hint(check_hint),
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "target_name": target_name,
            "candidate_id": approved_candidate.candidate_id,
        },
        output_summary={
            "status": pre_check.get("status"),
            "attempts": pre_check.get("attempts"),
            "should_execute": pre_check.get("should_execute"),
            **_pre_check_observed_fields(pre_check),
        },
    )
    if not bool(pre_check.get("should_execute")):
        final_state = "recovered" if pre_check.get("status") == "not_firing" else "escalated"
        trace = finalize_decision_trace(
            trace,
            execution_result={
                "status": "skipped",
                "candidate_id": approved_candidate.candidate_id,
                "operation_family": approved_candidate.execution_plan.operation_family,
                "reason": _pre_check_skip_reason(pre_check),
            },
            verification_result={
                "status": pre_check.get("status", "not_run"),
                "reason": _pre_check_skip_reason(pre_check),
                "pre_check": pre_check,
            },
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

    dispatch, dispatch_metadata = _build_execution_dispatch_for_candidate(
        incident_id=incident.incident_id,
        candidate=approved_candidate,
    )
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result_for_candidate(
        candidate=approved_candidate,
        dispatch=dispatch,
        worker_result=worker_result,
        dispatch_metadata=dispatch_metadata,
    )
    trace = append_node_run(
        trace,
        node_name="execution_worker",
        status=str(worker_result.status),
        summary=_execution_worker_summary(worker_result.status),
        llm_explanation=_execution_worker_candidate_explanation(
            candidate=approved_candidate,
            worker_result=worker_result,
        ),
        input_summary={
            "worker_id": dispatch.worker_id,
            "candidate_id": approved_candidate.candidate_id,
            "action_type": dispatch.action_type,
            "dispatch_source": dispatch_metadata["dispatch_source"],
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "dispatch_source": dispatch_metadata["dispatch_source"],
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
                    "reason": "Post-check did not run because the execution worker failed before verification.",
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

    if dispatch.action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        execution_result["rollout_status"] = kubernetes.wait_for_rollout_deployment(
            namespace=namespace,
            deployment=deployment,
            timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
        )
        rollout_status = execution_result["rollout_status"]
        if rollout_status.get("status") == "succeeded":
            execution_result["deployment_availability"] = _wait_for_deployment_availability(
                kubernetes=kubernetes,
                namespace=namespace,
                deployment=deployment,
                sleep_fn=prometheus.sleep_fn,
                attempts=ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS,
                sleep_seconds=ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS,
            )
        trace = append_node_run(
            trace,
            node_name="rollout_wait",
            status=str(rollout_status["status"]),
            summary="Kubernetes rollout status was checked after the approved exact execution plan ran.",
            input_summary={
                "namespace": namespace,
                "deployment": deployment,
                "candidate_id": approved_candidate.candidate_id,
            },
            output_summary={
                "status": rollout_status["status"],
                "returncode": rollout_status["returncode"],
                "availability_status": (
                    execution_result.get("deployment_availability", {}).get("availability_status")
                    if isinstance(execution_result.get("deployment_availability"), dict)
                    else None
                ),
                "availability_attempts": (
                    execution_result.get("deployment_availability", {}).get("attempts")
                    if isinstance(execution_result.get("deployment_availability"), dict)
                    else None
                ),
            },
        )

    post_check = _run_post_check_for_candidate(
        approved_candidate,
        prometheus=prometheus,
        namespace=namespace,
        deployment=deployment,
    )
    if check_hint == "crashloop":
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
        summary=_post_check_summary_for_hint(check_hint),
        input_summary={
            "namespace": namespace,
            "deployment": deployment,
            "target_name": target_name,
            "candidate_id": approved_candidate.candidate_id,
        },
        output_summary={
            "status": post_check.get("status"),
            "attempts": post_check.get("attempts"),
            **_post_check_observed_fields(post_check),
        },
    )

    rollback_triggered = False
    rollback_result: dict[str, object] | None = None
    post_rollback_check: dict[str, object] | None = None
    if approved_action is not None and post_check.get("status") != "recovered":
        rollback_triggered, rollback_result, post_rollback_check = _attempt_bounded_rollback(
            action=approved_action,
            kubernetes=kubernetes,
            prometheus=prometheus,
            namespace=namespace,
            deployment=deployment,
            post_check_fn=_post_check_function_for_hint(check_hint, prometheus),
            apply_kubernetes_fallback_to_post_check=check_hint == "crashloop",
            failure_reason="Post-check verification did not confirm recovery after the approved exact execution plan.",
            rollout_wait_timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
            rollout_availability_grace_attempts=ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS,
            rollout_availability_grace_sleep_seconds=ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS,
        )

    verification_result: dict[str, object] = {
        "pre_check": pre_check,
        "post_check": post_check,
        "recovery_latency_seconds": _recovery_latency_seconds(dispatch.requested_at),
    }
    final_state = "recovered" if post_check.get("status") == "recovered" else "escalated"
    if rollback_triggered and rollback_result is not None:
        execution_result["rollback"] = rollback_result
        trace = _append_rollback_run(
            trace,
            approved_action=approved_action or _upgrade_candidate_to_action_fallback(approved_candidate),
            rollback_result=rollback_result,
            post_rollback_check=post_rollback_check,
        )
        if post_rollback_check is not None:
            verification_result["post_rollback_check"] = post_rollback_check
        if post_rollback_check is not None and post_rollback_check.get("status") == "recovered":
            final_state = "rolled_back"
        else:
            final_state = "escalated"

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

    dispatch, dispatch_metadata = _build_execution_dispatch_for_mode(
        incident_id=incident.incident_id,
        action=approved_action,
        fixer_state=fixer_state,
    )
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result(
        action=approved_action,
        dispatch=dispatch,
        worker_result=worker_result,
        dispatch_metadata=dispatch_metadata,
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
            "dispatch_source": dispatch_metadata["dispatch_source"],
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "dispatch_source": dispatch_metadata["dispatch_source"],
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
    if rollout_status.get("status") == "succeeded":
        execution_result["deployment_availability"] = _wait_for_deployment_availability(
            kubernetes=kubernetes,
            namespace=namespace,
            deployment=deployment,
            sleep_fn=prometheus.sleep_fn,
            attempts=ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS,
            sleep_seconds=ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS,
        )
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
            "availability_status": (
                execution_result.get("deployment_availability", {}).get("availability_status")
                if isinstance(execution_result.get("deployment_availability"), dict)
                else None
            ),
            "availability_attempts": (
                execution_result.get("deployment_availability", {}).get("attempts")
                if isinstance(execution_result.get("deployment_availability"), dict)
                else None
            ),
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
        apply_kubernetes_fallback_to_post_check=True,
        failure_reason=(
            "Approved action rollout did not converge."
            if rollout_status["status"] != "succeeded"
            else "Post-check verification did not confirm recovery."
        ),
        rollout_wait_timeout_seconds=ROLLOUT_WAIT_TIMEOUT_SECONDS,
        rollout_availability_grace_attempts=ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS,
        rollout_availability_grace_sleep_seconds=ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS,
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

    dispatch, dispatch_metadata = _build_execution_dispatch_for_mode(
        incident_id=incident.incident_id,
        action=approved_action,
        fixer_state=fixer_state,
    )
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result(
        action=approved_action,
        dispatch=dispatch,
        worker_result=worker_result,
        dispatch_metadata=dispatch_metadata,
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
            "dispatch_source": dispatch_metadata["dispatch_source"],
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "dispatch_source": dispatch_metadata["dispatch_source"],
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
    if rollout_status.get("status") == "succeeded":
        execution_result["deployment_availability"] = _wait_for_deployment_availability(
            kubernetes=kubernetes,
            namespace=namespace,
            deployment=deployment,
            sleep_fn=prometheus.sleep_fn,
            attempts=ROLLOUT_AVAILABILITY_GRACE_ATTEMPTS,
            sleep_seconds=ROLLOUT_AVAILABILITY_GRACE_SLEEP_SECONDS,
        )
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
            "availability_status": (
                execution_result.get("deployment_availability", {}).get("availability_status")
                if isinstance(execution_result.get("deployment_availability"), dict)
                else None
            ),
            "availability_attempts": (
                execution_result.get("deployment_availability", {}).get("attempts")
                if isinstance(execution_result.get("deployment_availability"), dict)
                else None
            ),
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

    dispatch, dispatch_metadata = _build_execution_dispatch_for_mode(
        incident_id=incident.incident_id,
        action=approved_action,
        fixer_state=fixer_state,
    )
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result(
        action=approved_action,
        dispatch=dispatch,
        worker_result=worker_result,
        dispatch_metadata=dispatch_metadata,
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
            "dispatch_source": dispatch_metadata["dispatch_source"],
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "dispatch_source": dispatch_metadata["dispatch_source"],
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

    dispatch, dispatch_metadata = _build_execution_dispatch_for_mode(
        incident_id=incident.incident_id,
        action=approved_action,
        fixer_state=fixer_state,
    )
    worker_handle = worker_client.dispatch_execution_worker(dispatch)
    worker_result = worker_client.collect_execution_result(worker_handle)
    execution_result = _build_execution_result(
        action=approved_action,
        dispatch=dispatch,
        worker_result=worker_result,
        dispatch_metadata=dispatch_metadata,
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
            "dispatch_source": dispatch_metadata["dispatch_source"],
        },
        output_summary={
            "worker_id": worker_result.worker_id,
            "status": worker_result.status,
            "action_id": worker_result.action_id,
            "returncode": worker_result.returncode,
            "dispatch_source": dispatch_metadata["dispatch_source"],
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


def _build_v2_candidate_options(
    *,
    incident: Any,
    fixer_state: dict[str, Any],
    reasoner_state: dict[str, Any] | None,
    critic_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
) -> list[ApprovalCandidate]:
    synthesis_output = (synthesizer_state or {}).get("synthesis_output")
    reasoner_output = (reasoner_state or {}).get("reasoner_output")
    if synthesis_output is None or reasoner_output is None:
        return [
            _fallback_escalation_candidate(
                incident=incident,
                summary=(
                    (synthesizer_state or {}).get("failure_reason")
                    or (reasoner_state or {}).get("failure_reason")
                    or "v2 planning did not produce executable candidates."
                ),
            )
        ]

    intents_by_id = {intent.intent_id: intent for intent in list(getattr(reasoner_output, "intents", []))}
    critic_candidates = {
        candidate.intent_id: candidate
        for candidate in list(getattr((critic_state or {}).get("critic_output"), "candidates", []))
    }
    mapped_v1_candidates = list((reasoner_state or {}).get("mapped_v1_candidates", []))
    candidates: list[ApprovalCandidate] = []
    for plan in list(getattr(synthesis_output, "plans", [])):
        intent = intents_by_id.get(plan.intent_id)
        critic_candidate = critic_candidates.get(plan.intent_id)
        confidence_score = float(getattr(intent, "confidence_score", 0.0) or 0.0)
        display_labels = _candidate_display_labels(plan)
        legacy_action_hint = _legacy_action_hint_for_plan(plan, mapped_v1_candidates)
        executable_plan = plan
        if (
            not plan.steps
            or bool(critic_candidate is not None and (not critic_candidate.approved_for_consideration or critic_candidate.requires_escalation))
            or plan.blast_radius_score >= 0.8
        ):
            executable_plan = _non_executable_plan(
                plan,
                summary=f"Escalation-only candidate: {plan.summary}",
            )
        candidates.append(
            ApprovalCandidate(
                candidate_id=plan.intent_id,
                summary=executable_plan.summary,
                confidence_score=confidence_score,
                blast_radius_score=plan.blast_radius_score,
                requires_approval=plan.requires_approval,
                execution_plan=executable_plan,
                display_labels=display_labels,
                legacy_action_hint=legacy_action_hint,
            )
        )

    if not candidates:
        candidates.append(
            _fallback_escalation_candidate(
                incident=incident,
                summary="v2 planning produced no candidate execution plans.",
            )
        )
    return candidates


def _build_v2_compat_fixer_state(
    *,
    incident: Any,
    reasoner_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
) -> dict[str, Any]:
    reasoner_output = (reasoner_state or {}).get("reasoner_output")
    incident_summary = str(
        (reasoner_state or {}).get("incident_summary")
        or getattr(reasoner_output, "diagnosis_summary", "")
        or ""
    )
    synthesis_output = (synthesizer_state or {}).get("synthesis_output")
    fixer_rationale = str(
        getattr(synthesis_output, "summary", "")
        or (reasoner_state or {}).get("failure_reason")
        or "v2 planning produced capability-driven recovery candidates."
    )
    return {
        "incident_id": incident.incident_id,
        "incident_summary": incident_summary,
        "actions": list((reasoner_state or {}).get("mapped_v1_candidates", [])),
        "evidence": [],
        "fixer_rationale": fixer_rationale,
        "status": "succeeded" if reasoner_output is not None else "failed",
    }


def _build_v2_judge_state(
    *,
    incident: Any,
    candidate_options: list[ApprovalCandidate],
    critic_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
) -> dict[str, Any]:
    critic_output = (critic_state or {}).get("critic_output")
    judge_reason = ""
    if critic_output is not None:
        judge_reason = str(getattr(critic_output, "summary", ""))
    if not judge_reason:
        judge_reason = (
            str((synthesizer_state or {}).get("failure_reason") or "")
            or "v2 planning produced exact execution-plan candidates."
        )
    if candidate_options:
        return {
            "judge_verdict": "pass",
            "judge_reason": judge_reason,
            "judge_llm_reason": judge_reason,
            "incident_id": incident.incident_id,
        }
    return {
        "judge_verdict": "fail",
        "judge_reason": "v2 planning produced no approval candidates.",
        "judge_llm_reason": "v2 planning produced no approval candidates.",
        "incident_id": incident.incident_id,
    }


def _fallback_escalation_candidate(*, incident: Any, summary: str) -> ApprovalCandidate:
    plan = ExecutionPlan(
        intent_id=f"fallback-escalate-{incident.incident_id}",
        operation_family="escalate.human_review",
        target=ResourceTarget(namespace="default", kind="Incident", name=incident.incident_id),
        summary=f"Escalation-only candidate: {summary}",
        steps=[],
        allowed_tool_names=[],
        blast_radius_score=0.0,
        requires_approval=True,
        rollback_outline={},
    )
    return ApprovalCandidate(
        candidate_id=plan.intent_id,
        summary=plan.summary,
        confidence_score=0.0,
        blast_radius_score=0.0,
        requires_approval=True,
        execution_plan=plan,
        display_labels=["escalate.human_review", incident.incident_id],
        legacy_action_hint=None,
    )


def _non_executable_plan(plan: ExecutionPlan, *, summary: str) -> ExecutionPlan:
    return ExecutionPlan(
        intent_id=plan.intent_id,
        operation_family=plan.operation_family,
        target=plan.target,
        summary=summary,
        steps=[],
        allowed_tool_names=[],
        blast_radius_score=plan.blast_radius_score,
        requires_approval=plan.requires_approval,
        rollback_outline=dict(plan.rollback_outline),
    )


def _execution_worker_candidate_explanation(
    *,
    candidate: ApprovalCandidate,
    worker_result: ExecutionResult,
) -> str | None:
    legacy_action = _legacy_action_for_candidate(candidate)
    if legacy_action is not None:
        return _execution_worker_llm_explanation(action=legacy_action, worker_result=worker_result)

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
        f"The approved plan succeeded with return code {worker_result.returncode}."
        if worker_result.status == "succeeded"
        else f"The approved plan failed with return code {worker_result.returncode}."
    )
    base_explanation = (
        f"The Gemini execution agent handled approved candidate {candidate.candidate_id!r} "
        f"({candidate.execution_plan.operation_family}). {tool_clause} "
        f"It executed {command_text!r}. {outcome_clause}"
    )
    narrative = _truncate_text(worker_result.summary, limit=300)
    if narrative:
        if narrative[-1] not in ".!?":
            narrative += "."
        return f"{narrative} {base_explanation}"
    return base_explanation


def _select_action(hitl_decision: HITLDecision, action_id: str) -> RemediationAction:
    for candidate in hitl_decision.candidate_options:
        if candidate.candidate_id == action_id:
            legacy_action = _legacy_action_for_candidate(candidate)
            if legacy_action is not None:
                return legacy_action
            return _upgrade_candidate_to_action_fallback(candidate)
    raise ValueError(f"Approved action_id {action_id!r} is not available in the HITL decision.")


def _validate_engine_mode(value: str, *, allow_legacy: bool = False) -> EngineMode | str:
    if value in VALID_ENGINE_MODES:
        return value  # type: ignore[return-value]
    if allow_legacy and value == LEGACY_ENGINE_MODE:
        return value
    if allow_legacy:
        raise ValueError(f"Unsupported engine_mode in saved artifact: {value!r}")
    raise ValueError(f"Unsupported engine_mode: {value!r}")


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
    candidate_id = str(trace.execution_result.get("candidate_id") or "")
    if hitl_decision.candidate_options:
        for candidate in hitl_decision.candidate_options:
            if candidate_id and candidate.candidate_id == candidate_id:
                return _legacy_action_for_candidate(candidate)
            legacy_action = _legacy_action_for_candidate(candidate)
            if legacy_action is not None and action_id and legacy_action.action_id == action_id:
                return legacy_action
        return None

    if not action_id:
        return None
    for candidate in hitl_decision.candidate_options:
        if candidate.candidate_id == action_id:
            legacy_action = _legacy_action_for_candidate(candidate)
            if legacy_action is not None:
                return legacy_action
            return _upgrade_candidate_to_action_fallback(candidate)
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
        help="Run planning first, then prompt in the terminal: 1 = approve recommended candidate, 2 = reject.",
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
        default=DEFAULT_ENGINE_MODE,
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
            from services.llm.tasks.fixer import GeminiFixerLLM
            fixer_llm = GeminiFixerLLM(model=args.fixer_model)

        judge_llm = None
        if args.judge_provider == "gemini":
            from services.llm.tasks.judge import GeminiJudgeLLM
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
    recommended_candidate = hitl_decision.get("recommended_candidate")
    if recommended_candidate is not None:
        if isinstance(recommended_candidate, dict):
            selection_id = str(recommended_candidate["candidate_id"])
            summary = str(recommended_candidate["summary"])
        else:
            selection_id = recommended_candidate.candidate_id
            summary = recommended_candidate.summary
        output_fn(
            f"HITL Gate: recommended candidate {selection_id} - {summary}",
            flush=True,
        )
        output_fn("Enter 1 to approve or 2 to reject.", flush=True)
        while True:
            user_choice = str(input_fn("> ")).strip()
            if user_choice == "1":
                return run_recovery_from_saved_plan(
                    payload,
                    planning_result,
                    approve_action_id=selection_id,
                    engine_mode=str(planning_result.get("engine_mode", DEFAULT_ENGINE_MODE)),
                    kubernetes_client=kubernetes_client,
                    prometheus_client=prometheus_client,
                    execution_worker_client=execution_worker_client,
                )
            if user_choice == "2":
                return run_recovery_from_saved_plan(
                    payload,
                    planning_result,
                    reject_action_id=selection_id,
                    engine_mode=str(planning_result.get("engine_mode", DEFAULT_ENGINE_MODE)),
                    kubernetes_client=kubernetes_client,
                    prometheus_client=prometheus_client,
                    execution_worker_client=execution_worker_client,
                )
            output_fn("Enter 1 to approve or 2 to reject.", flush=True)

    recommended_candidate = hitl_decision.get("recommended_candidate")
    if recommended_candidate is None:
        return planning_result

    if isinstance(recommended_candidate, dict):
        selection_id = str(recommended_candidate["candidate_id"])
        summary = str(recommended_candidate["summary"])
    else:
        selection_id = recommended_candidate.candidate_id
        summary = recommended_candidate.summary
    output_fn(
        f"HITL Gate: recommended candidate {selection_id} - {summary}",
        flush=True,
    )
    output_fn("Enter 1 to approve or 2 to reject.", flush=True)
    while True:
        user_choice = str(input_fn("> ")).strip()
        if user_choice == "1":
            return run_recovery_from_saved_plan(
                payload,
                planning_result,
                approve_action_id=selection_id,
                engine_mode=str(planning_result.get("engine_mode", DEFAULT_ENGINE_MODE)),
                kubernetes_client=kubernetes_client,
                prometheus_client=prometheus_client,
                execution_worker_client=execution_worker_client,
            )
        if user_choice == "2":
            return run_recovery_from_saved_plan(
                payload,
                planning_result,
                reject_action_id=selection_id,
                engine_mode=str(planning_result.get("engine_mode", DEFAULT_ENGINE_MODE)),
                kubernetes_client=kubernetes_client,
                prometheus_client=prometheus_client,
                execution_worker_client=execution_worker_client,
            )
        output_fn("Enter 1 to approve or 2 to reject.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
