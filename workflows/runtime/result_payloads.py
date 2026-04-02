from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from typing import Any

from schemas.decision_trace import DecisionTrace
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from services.normalization.incident import normalize_incident_class
from services.runtime.decision_trace import derive_trace_timeline
from workflows.hitl_gate import HITLDecision
from workflows.runtime.approval_adapters import serialize_remediation_action


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def build_result(
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
    engine_mode = str(fixer_state.get("_engine_mode", "v1"))
    if engine_mode == "v1":
        hitl_payload = {
            "routing_decision": hitl_decision.routing_decision,
            "requires_approval": hitl_decision.requires_approval,
            "recommended_action": hitl_decision.recommended_action,
            "candidate_actions": hitl_decision.candidate_actions,
        }
    else:
        hitl_payload = {
            "routing_decision": hitl_decision.routing_decision,
            "requires_approval": hitl_decision.requires_approval,
            "recommended_candidate": hitl_decision.recommended_candidate,
            "candidate_options": hitl_decision.candidate_options,
        }
    return {
        "incident": incident,
        "engine_mode": engine_mode,
        "observation_bundle": observation_bundle,
        "reasoner_state": reasoner_state,
        "critic_state": critic_state,
        "synthesizer_state": synthesizer_state,
        "verifier_state": verifier_state,
        "replanner_state": replanner_state,
        "fixer_state": fixer_state,
        "judge_state": judge_state,
        "hitl_decision": hitl_payload,
        "decision_trace": decision_trace,
        "decision_trace_timeline": derive_trace_timeline(decision_trace),
    }


def attach_v2_shadow_fixer_plan(
    trace: DecisionTrace,
    *,
    incident: Any,
    engine_mode: str,
    observation_bundle: ObservationBundle | None,
    observation_run: dict[str, Any] | None,
    reasoner_state: dict[str, Any] | None,
    critic_state: dict[str, Any] | None,
    synthesizer_state: dict[str, Any] | None,
    verifier_state: dict[str, Any] | None,
    replanner_state: dict[str, Any] | None,
) -> DecisionTrace:
    fixer_plan = dict(trace.fixer_plan)
    fixer_plan["v2_shadow"] = build_v2_shadow_payload(
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


def build_v2_shadow_payload(
    *,
    incident: Any,
    engine_mode: str,
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
        "observation_summary": shadow_observation_summary(
            incident=incident,
            observation_bundle=observation_bundle,
            observation_run=observation_run,
        ),
        "reasoner_output": to_jsonable((reasoner_state or {}).get("reasoner_output")),
        "mapped_v1_candidates": [
            serialize_remediation_action(action)
            for action in list((reasoner_state or {}).get("mapped_v1_candidates", []))
            if isinstance(action, RemediationAction)
        ],
        "critic_output": to_jsonable((critic_state or {}).get("critic_output")),
        "policy_summary": to_jsonable((critic_state or {}).get("policy_summary", {})),
        "synthesis_output": to_jsonable((synthesizer_state or {}).get("synthesis_output")),
        "synthesized_v1_dispatches": to_jsonable(list((synthesizer_state or {}).get("synthesized_v1_dispatches", []))),
        "verification_plan": to_jsonable((verifier_state or {}).get("verification_plan")),
        "verification_result_v2": to_jsonable((verifier_state or {}).get("verification_result_v2")),
        "replan_output": to_jsonable((replanner_state or {}).get("replan_output")),
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


def shadow_observation_summary(
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
