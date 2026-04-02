from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, NotRequired, TypedDict

from schemas.incident import Incident
from schemas.intents import CapabilityCatalog, OperationIntent, ReasonerOutput, ResourceTarget
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from services.recovery.capability_catalog import default_capability_catalog
from services.normalization.incident import normalize_incident_class
from services.recovery.intent_compat import intent_to_v1_remediation
from services.llm.tasks.reasoner_contract import ReasonerLLM, ReasonerLLMResult


class ReasonerAgentState(TypedDict):
    incident: Incident
    observations: ObservationBundle
    capability_catalog: CapabilityCatalog
    incident_summary: str
    incident_class_hint: str
    reasoner_output: ReasonerOutput | None
    mapped_v1_candidates: list[RemediationAction]
    errors: list[str]
    final: bool
    status: str

    raw_reasoner_output: NotRequired[str]
    failure_reason: NotRequired[str]


def initial_reasoner_state(
    incident: Incident,
    observations: ObservationBundle,
    *,
    capability_catalog: CapabilityCatalog | None = None,
) -> ReasonerAgentState:
    return {
        "incident": incident,
        "observations": observations,
        "capability_catalog": capability_catalog or default_capability_catalog(),
        "incident_summary": "",
        "incident_class_hint": observations.incident_class_hint,
        "reasoner_output": None,
        "mapped_v1_candidates": [],
        "errors": [],
        "final": False,
        "status": "failed",
    }


def build_incident_summary_node(state: ReasonerAgentState) -> dict[str, Any]:
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
    return {"incident_summary": summary, "incident_class_hint": observations.incident_class_hint}


def heuristic_reason_node(state: ReasonerAgentState) -> dict[str, Any]:
    observations = state["observations"]
    incident = state["incident"]
    incident_class_hint = normalize_incident_class(state.get("incident_class_hint", observations.incident_class_hint))
    namespace = observations.namespace_hint or "default"
    deployment_hint = _string_or_none(observations.alert_context.get("deployment_hint"))
    service_hint = _string_or_none(observations.alert_context.get("service_hint"))

    if incident_class_hint == "crashloop":
        deployment = deployment_hint or "cartservice"
        intents = [
            OperationIntent(
                intent_id="reasoner-rollout-undo-cartservice",
                intent=f"Roll back Deployment {deployment} to the previous ReplicaSet.",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment),
                arguments={},
                reversible=True,
                confidence_score=0.9,
                blast_radius_score=0.3,
                requires_approval=True,
                verification_hints={"pre_check": "crashloop", "post_check": "crashloop"},
                rollback_hints={"preferred_rollback": "rollout.undo_deployment"},
            ),
            OperationIntent(
                intent_id="reasoner-rollout-restart-cartservice",
                intent=f"Restart Deployment {deployment} to clear transient crashloop state.",
                operation_family="rollout.restart_deployment",
                target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment),
                arguments={},
                reversible=True,
                confidence_score=0.5,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={"pre_check": "crashloop", "post_check": "crashloop"},
                rollback_hints={"preferred_rollback": "rollout.undo_deployment"},
            ),
        ]
        diagnosis_summary = f"{deployment} appears to be crash looping after a recent rollout."
        likely_causes = ["A bad Deployment revision introduced a failing cartservice pod startup path."]
    elif incident_class_hint == "cpu_saturation":
        chaos_name = "frontend-cpu-saturation"
        deployment = deployment_hint or "frontend"
        intents = [
            OperationIntent(
                intent_id="reasoner-delete-frontend-stresschaos",
                intent="Delete the active frontend CPU StressChaos object.",
                operation_family="chaos.delete_stresschaos",
                target=ResourceTarget(namespace=namespace, kind="StressChaos", name=chaos_name),
                arguments={},
                reversible=True,
                confidence_score=0.9,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={"pre_check": "cpu_saturation", "post_check": "cpu_saturation"},
                rollback_hints={},
            ),
            OperationIntent(
                intent_id="reasoner-escalate-frontend-cpu",
                intent=f"Escalate the {deployment} CPU saturation incident for human review.",
                operation_family="escalate.human_review",
                target=ResourceTarget(namespace=namespace, kind="Incident", name=incident.incident_id),
                arguments={"reason": "Bounded CPU remediation may not fully explain the sustained saturation."},
                reversible=True,
                confidence_score=0.35,
                blast_radius_score=0.0,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            ),
        ]
        diagnosis_summary = "Frontend CPU appears to be saturated by an active synthetic chaos workload."
        likely_causes = ["Chaos Mesh CPU stress is causing frontend resource saturation."]
    elif incident_class_hint == "bad_config":
        deployment = deployment_hint or service_hint or "frontend"
        intents = [
            OperationIntent(
                intent_id="reasoner-rollout-undo-frontend",
                intent=f"Roll back Deployment {deployment} to the previous ReplicaSet.",
                operation_family="rollout.undo_deployment",
                target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment),
                arguments={},
                reversible=True,
                confidence_score=0.92,
                blast_radius_score=0.3,
                requires_approval=True,
                verification_hints={"pre_check": "bad_config", "post_check": "bad_config"},
                rollback_hints={"preferred_rollback": "rollout.undo_deployment"},
            ),
            OperationIntent(
                intent_id="reasoner-escalate-frontend-bad-config",
                intent=f"Escalate the {deployment} dependency configuration incident.",
                operation_family="escalate.human_review",
                target=ResourceTarget(namespace=namespace, kind="Incident", name=incident.incident_id),
                arguments={"reason": "Bounded rollback is available, but deeper config inspection may still be required."},
                reversible=True,
                confidence_score=0.2,
                blast_radius_score=0.0,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            ),
        ]
        diagnosis_summary = "Frontend dependency configuration appears unhealthy and likely regressed in a recent rollout."
        likely_causes = ["The frontend /cart dependency path is failing due to bad configuration."]
    elif incident_class_hint == "network_partition":
        chaos_name = "frontend-to-cartservice-partition"
        intents = [
            OperationIntent(
                intent_id="reasoner-delete-networkchaos",
                intent="Delete the active frontend-to-cartservice NetworkChaos object.",
                operation_family="chaos.delete_networkchaos",
                target=ResourceTarget(namespace=namespace, kind="NetworkChaos", name=chaos_name),
                arguments={},
                reversible=True,
                confidence_score=0.88,
                blast_radius_score=0.2,
                requires_approval=True,
                verification_hints={"pre_check": "network_partition", "post_check": "network_partition"},
                rollback_hints={},
            ),
            OperationIntent(
                intent_id="reasoner-escalate-network-partition",
                intent="Escalate the dependency network partition incident for human review.",
                operation_family="escalate.human_review",
                target=ResourceTarget(namespace=namespace, kind="Incident", name=incident.incident_id),
                arguments={"reason": "Dependency communication may be failing for reasons beyond the bounded chaos path."},
                reversible=True,
                confidence_score=0.25,
                blast_radius_score=0.0,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            ),
        ]
        diagnosis_summary = "Cartservice dependency traffic appears near zero and is consistent with an active network partition."
        likely_causes = ["A Chaos Mesh NetworkChaos rule is partitioning frontend from cartservice."]
    else:
        dynamic_scale_output = _dynamic_scale_reasoning(
            observations=observations,
            incident=incident,
            namespace=namespace,
            deployment_hint=deployment_hint or service_hint,
        )
        if dynamic_scale_output is None:
            reasoner_output = ReasonerOutput(
                diagnosis_summary=(
                    "The bounded heuristic Reasoner could not map the current evidence to a safe automated recovery."
                ),
                likely_causes=[
                    "The incident falls outside the current benchmark-shaped heuristic rules.",
                ],
                missing_information=[
                    "A more specific diagnosis or operator review is required before safe execution.",
                ],
                intents=[
                    OperationIntent(
                        intent_id=f"reasoner-escalate-{incident.incident_id}",
                        intent="Escalate the incident to a human operator for deeper investigation.",
                        operation_family="escalate.human_review",
                        target=ResourceTarget(namespace=namespace, kind="Incident", name=incident.incident_id),
                        arguments={"reason": "Bounded heuristic recovery is not clearly justified by the current evidence."},
                        reversible=True,
                        confidence_score=0.2,
                        blast_radius_score=0.0,
                        requires_approval=True,
                        verification_hints={},
                        rollback_hints={},
                    )
                ],
            )
            return {
                "reasoner_output": reasoner_output,
                "mapped_v1_candidates": _mapped_v1_candidates(reasoner_output.intents),
                "status": "succeeded",
            }
        return {
            "reasoner_output": dynamic_scale_output,
            "mapped_v1_candidates": _mapped_v1_candidates(dynamic_scale_output.intents),
            "status": "succeeded",
        }

    reasoner_output = ReasonerOutput(
        diagnosis_summary=diagnosis_summary,
        likely_causes=likely_causes,
        missing_information=[],
        intents=intents,
    )
    return {
        "reasoner_output": reasoner_output,
        "mapped_v1_candidates": _mapped_v1_candidates(reasoner_output.intents),
        "status": "succeeded",
    }


def make_llm_reason_node(llm: ReasonerLLM) -> Any:
    def _node(state: ReasonerAgentState) -> dict[str, Any]:
        errors = list(state.get("errors", []))
        incident_summary = state.get("incident_summary", "")
        observations = state["observations"]
        incident_class_hint = state["incident_class_hint"]
        capability_catalog = state["capability_catalog"]
        try:
            result: ReasonerLLMResult = llm.reason(
                incident_summary=incident_summary,
                observations=observations,
                incident_class_hint=incident_class_hint,
                capability_catalog=capability_catalog,
            )
            return {
                "reasoner_output": result.output,
                "mapped_v1_candidates": _mapped_v1_candidates(result.output.intents),
                "raw_reasoner_output": result.raw_response_text,
                "errors": errors,
                "status": "succeeded",
            }
        except Exception as exc:
            errors.append(f"Reasoner LLM failed; falling back to heuristic: {exc}")
            fallback = heuristic_reason_node(state)
            merged_errors = list(fallback.get("errors", []))
            return {
                "reasoner_output": fallback["reasoner_output"],
                "mapped_v1_candidates": fallback["mapped_v1_candidates"],
                "errors": errors + merged_errors,
                "status": fallback["status"],
            }

    return _node


def finalize_reasoner_node(state: ReasonerAgentState) -> dict[str, Any]:
    reasoner_output = state.get("reasoner_output")
    if reasoner_output is None:
        failure_reason = str(state.get("failure_reason") or "Reasoner did not produce shadow intents.")
        return {
            "status": "failed",
            "failure_reason": failure_reason,
            "final": True,
        }
    return {
        "status": state.get("status", "succeeded"),
        "final": True,
    }


def run_reasoner_pipeline(
    incident: Incident,
    observations: ObservationBundle,
    llm: ReasonerLLM | None = None,
    *,
    capability_catalog: CapabilityCatalog | None = None,
) -> ReasonerAgentState:
    state = initial_reasoner_state(
        incident,
        observations,
        capability_catalog=capability_catalog,
    )
    state.update(build_incident_summary_node(state))
    if llm is None:
        state.update(heuristic_reason_node(state))
    else:
        state.update(make_llm_reason_node(llm)(state))
    state.update(finalize_reasoner_node(state))
    return state


def serialize_reasoner_state(state: ReasonerAgentState) -> dict[str, Any]:
    return {
        key: _to_jsonable(value)
        for key, value in state.items()
    }


def _mapped_v1_candidates(intents: list[OperationIntent]) -> list[RemediationAction]:
    candidates: list[RemediationAction] = []
    for intent in intents:
        mapped = intent_to_v1_remediation(intent)
        if mapped is not None:
            candidates.append(mapped)
    return candidates


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _dynamic_scale_reasoning(
    *,
    observations: ObservationBundle,
    incident: Incident,
    namespace: str,
    deployment_hint: str | None,
) -> ReasonerOutput | None:
    deployment_summary = observations.kubernetes.get("deployment_summary")
    if not isinstance(deployment_summary, dict):
        return None

    deployment_name = deployment_hint or _string_or_none(deployment_summary.get("name"))
    if deployment_name is None:
        return None

    desired_replicas = _non_negative_int(deployment_summary.get("desired_replicas"))
    ready_replicas = _non_negative_int(deployment_summary.get("ready_replicas"))
    available_replicas = _non_negative_int(deployment_summary.get("available_replicas"))
    current_ready = max(ready_replicas, available_replicas)

    if desired_replicas > 0:
        return None

    target_replicas = 1
    return ReasonerOutput(
        diagnosis_summary=(
            f"Deployment {deployment_name} appears scaled below a safe ready minimum and likely needs bounded capacity restored."
        ),
        likely_causes=[
            f"Deployment {deployment_name} currently reports desired_replicas={desired_replicas} and ready_replicas={current_ready}.",
        ],
        missing_information=[],
        intents=[
            OperationIntent(
                intent_id=f"reasoner-scale-{deployment_name}-{target_replicas}",
                intent=f"Scale Deployment {deployment_name} back to {target_replicas} replica.",
                operation_family="scale.deployment",
                target=ResourceTarget(namespace=namespace, kind="Deployment", name=deployment_name),
                arguments={"replicas": target_replicas},
                reversible=True,
                confidence_score=0.78,
                blast_radius_score=0.25,
                requires_approval=True,
                verification_hints={
                    "pre_check": "deployment_readiness_shortfall",
                    "post_check": "deployment_readiness_shortfall",
                    "target_replicas": target_replicas,
                    "min_ready_count": target_replicas,
                },
                rollback_hints={"previous_replicas": desired_replicas},
            ),
            OperationIntent(
                intent_id=f"reasoner-escalate-{deployment_name}-scale",
                intent=f"Escalate deployment readiness shortfall on {deployment_name} for human review.",
                operation_family="escalate.human_review",
                target=ResourceTarget(namespace=namespace, kind="Incident", name=incident.incident_id),
                arguments={"reason": "Bounded scaling may restore safe availability, but the root cause of the replica shortfall is still unknown."},
                reversible=True,
                confidence_score=0.3,
                blast_radius_score=0.0,
                requires_approval=True,
                verification_hints={},
                rollback_hints={},
            ),
        ],
    )


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
