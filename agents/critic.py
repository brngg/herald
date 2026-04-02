from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, NotRequired, TypedDict

from schemas.critic import CriticOutput, CritiqueCandidate, PolicyCheckResult
from schemas.incident import Incident
from schemas.intents import CapabilityCatalog, ReasonerOutput
from schemas.observations import ObservationBundle
from services.recovery.capability_catalog import default_capability_catalog
from services.llm.tasks.critic_contract import CriticLLM, CriticLLMResult
from services.recovery.policy_validator import (
    PolicyValidationResult,
    summarize_policy_validation,
    validate_shadow_intent_policies,
)


class CriticAgentState(TypedDict):
    incident: Incident
    observations: ObservationBundle
    reasoner_output: ReasonerOutput
    capability_catalog: CapabilityCatalog
    incident_summary: str
    critic_output: CriticOutput | None
    policy_summary: dict[str, Any]
    errors: list[str]
    final: bool
    status: str

    raw_critic_output: NotRequired[str]
    failure_reason: NotRequired[str]


def initial_critic_state(
    incident: Incident,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    *,
    capability_catalog: CapabilityCatalog | None = None,
) -> CriticAgentState:
    return {
        "incident": incident,
        "observations": observations,
        "reasoner_output": reasoner_output,
        "capability_catalog": capability_catalog or default_capability_catalog(),
        "incident_summary": "",
        "critic_output": None,
        "policy_summary": {},
        "errors": [],
        "final": False,
        "status": "failed",
    }


def build_incident_summary_node(state: CriticAgentState) -> dict[str, Any]:
    observations = state["observations"]
    labels = observations.alert_context.get("labels", {})
    annotations = observations.alert_context.get("annotations", {})

    severity = labels.get("severity") or "unknown_severity"
    alertname = labels.get("alertname") or observations.incident_class_hint
    namespace = observations.namespace_hint or "unknown_ns"
    summary_text = annotations.get("summary") or annotations.get("description") or ""

    summary = (
        f"[{severity}] {alertname} ({observations.incident_class_hint}) "
        f"ns={namespace} - {summary_text}"
    ).strip()
    return {"incident_summary": summary}


def heuristic_critic_node(state: CriticAgentState) -> dict[str, Any]:
    policy_validation = validate_shadow_intent_policies(state["reasoner_output"].intents)
    summary, global_concerns = _build_heuristic_summary(policy_validation)
    critic_output = _critic_output_from_policy_validation(
        summary=summary,
        global_concerns=global_concerns,
        policy_validation=policy_validation,
    )
    return {
        "critic_output": critic_output,
        "policy_summary": summarize_policy_validation(policy_validation),
        "status": "succeeded",
    }


def make_llm_critique_node(llm: CriticLLM) -> Any:
    def _node(state: CriticAgentState) -> dict[str, Any]:
        errors = list(state.get("errors", []))
        incident_summary = state.get("incident_summary", "")
        observations = state["observations"]
        reasoner_output = state["reasoner_output"]
        capability_catalog = state["capability_catalog"]
        try:
            result: CriticLLMResult = llm.critique(
                incident_summary=incident_summary,
                observations=observations,
                reasoner_output=reasoner_output,
                policy_summary=summarize_policy_validation(
                    validate_shadow_intent_policies(reasoner_output.intents)
                ),
                capability_catalog=capability_catalog,
            )
            policy_validation = validate_shadow_intent_policies(reasoner_output.intents)
            return {
                "critic_output": _merge_critic_output(result.output, policy_validation),
                "policy_summary": summarize_policy_validation(policy_validation),
                "raw_critic_output": result.raw_response_text,
                "errors": errors,
                "status": "succeeded",
            }
        except Exception as exc:
            errors.append(f"Critic LLM failed; falling back to heuristic: {exc}")
            fallback = heuristic_critic_node(state)
            merged_errors = list(fallback.get("errors", []))
            return {
                "critic_output": fallback["critic_output"],
                "policy_summary": fallback["policy_summary"],
                "errors": errors + merged_errors,
                "status": fallback["status"],
            }

    return _node


def finalize_critic_node(state: CriticAgentState) -> dict[str, Any]:
    critic_output = state.get("critic_output")
    if critic_output is None:
        failure_reason = str(state.get("failure_reason") or "Critic did not produce shadow policy analysis.")
        return {
            "status": "failed",
            "failure_reason": failure_reason,
            "final": True,
        }
    return {
        "status": state.get("status", "succeeded"),
        "final": True,
    }


def run_critic_pipeline(
    incident: Incident,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    llm: CriticLLM | None = None,
    *,
    capability_catalog: CapabilityCatalog | None = None,
) -> CriticAgentState:
    state = initial_critic_state(
        incident,
        observations,
        reasoner_output,
        capability_catalog=capability_catalog,
    )
    state.update(build_incident_summary_node(state))
    policy_validation = validate_shadow_intent_policies(reasoner_output.intents)
    if llm is None:
        state.update(heuristic_critic_node(state))
    else:
        state.update(make_llm_critique_node(llm)(state))
    if state.get("critic_output") is None:
        state["critic_output"] = _critic_output_from_policy_validation(
            summary=_default_summary(policy_validation),
            global_concerns=_default_global_concerns(policy_validation),
            policy_validation=policy_validation,
        )
        state["policy_summary"] = summarize_policy_validation(policy_validation)
        state["status"] = "succeeded"
    state.update(finalize_critic_node(state))
    return state


def serialize_critic_state(state: CriticAgentState) -> dict[str, Any]:
    return {key: _to_jsonable(value) for key, value in state.items()}


def _merge_critic_output(
    output: CriticOutput,
    policy_validation: list[PolicyValidationResult],
) -> CriticOutput:
    llm_by_intent_id = {candidate.intent_id: candidate for candidate in output.candidates}
    merged_candidates: list[CritiqueCandidate] = []
    for index, policy in enumerate(policy_validation, start=1):
        candidate = llm_by_intent_id.get(policy.intent_id)
        if candidate is None:
            merged_candidates.append(
                CritiqueCandidate(
                    intent_id=policy.intent_id,
                    approved_for_consideration=policy.approved_for_consideration,
                    concerns=list(policy.concerns),
                    policy_checks=list(_policy_checks_from_validation(policy)),
                    recommended_rank=index,
                    requires_escalation=policy.requires_escalation,
                )
            )
            continue
        merged_candidates.append(
            CritiqueCandidate(
                intent_id=policy.intent_id,
                approved_for_consideration=policy.approved_for_consideration,
                concerns=_dedupe_strings(list(candidate.concerns) + list(policy.concerns)),
                policy_checks=list(_policy_checks_from_validation(policy)),
                recommended_rank=index,
                requires_escalation=policy.requires_escalation,
            )
        )

    merged_candidates.sort(key=lambda candidate: (candidate.recommended_rank, candidate.intent_id))
    for index, candidate in enumerate(merged_candidates, start=1):
        candidate.recommended_rank = index  # type: ignore[misc]
    return CriticOutput(
        summary=output.summary,
        global_concerns=_dedupe_strings(list(output.global_concerns)),
        candidates=merged_candidates,
    )


def _critic_output_from_policy_validation(
    *,
    summary: str,
    global_concerns: list[str],
    policy_validation: list[PolicyValidationResult],
) -> CriticOutput:
    candidates: list[CritiqueCandidate] = []
    for index, validation in enumerate(policy_validation, start=1):
        candidates.append(
            CritiqueCandidate(
                intent_id=validation.intent_id,
                approved_for_consideration=validation.approved_for_consideration,
                concerns=list(validation.concerns),
                policy_checks=list(_policy_checks_from_validation(validation)),
                recommended_rank=index,
                requires_escalation=validation.requires_escalation,
            )
        )
    return CriticOutput(
        summary=summary,
        global_concerns=_dedupe_strings(global_concerns),
        candidates=candidates,
    )


def _policy_checks_from_validation(validation: PolicyValidationResult) -> list[PolicyCheckResult]:
    return [
        PolicyCheckResult(
            policy_name=check.policy_name,
            passed=check.passed,
            reason=check.reason,
        )
        for check in validation.policy_checks
    ]


def _build_heuristic_summary(
    policy_validation: list[PolicyValidationResult],
) -> tuple[str, list[str]]:
    approved_count = sum(1 for result in policy_validation if result.approved_for_consideration)
    escalate_count = sum(1 for result in policy_validation if result.requires_escalation)
    summary = (
        f"Policy validation approved {approved_count} candidate(s) and flagged {escalate_count} for escalation."
    )
    return summary, _default_global_concerns(policy_validation)


def _default_summary(policy_validation: list[PolicyValidationResult]) -> str:
    approved_count = sum(1 for result in policy_validation if result.approved_for_consideration)
    return f"Policy validation reviewed {len(policy_validation)} intent(s) and approved {approved_count} for consideration."


def _default_global_concerns(policy_validation: list[PolicyValidationResult]) -> list[str]:
    concerns: list[str] = []
    if not policy_validation:
        concerns.append("Reasoner emitted no intents to critique.")
        return concerns
    if any(result.requires_escalation for result in policy_validation):
        concerns.append("One or more intents should be escalated or blocked by policy.")
    if any(not result.approved_for_consideration for result in policy_validation):
        concerns.append("At least one intent failed deterministic policy validation.")
    return concerns


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
