from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol

from schemas.intents import (
    CapabilityCatalog,
    ReasonerOutput,
    VALID_OPERATION_FAMILIES,
    reasoner_output_from_dict,
)
from schemas.observations import ObservationBundle


@dataclass(frozen=True, slots=True)
class ReasonerLLMResult:
    output: ReasonerOutput
    raw_response_text: str


class ReasonerLLM(Protocol):
    def reason(
        self,
        *,
        incident_summary: str,
        observations: ObservationBundle,
        incident_class_hint: str,
        capability_catalog: CapabilityCatalog,
    ) -> ReasonerLLMResult: ...


def reasoner_output_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "diagnosis_summary": {"type": "string"},
            "likely_causes": {"type": "array", "items": {"type": "string"}},
            "missing_information": {"type": "array", "items": {"type": "string"}},
            "intents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "intent_id": {"type": "string"},
                        "intent": {"type": "string"},
                        "operation_family": {"type": "string", "enum": list(VALID_OPERATION_FAMILIES)},
                        "target": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "namespace": {"type": ["string", "null"]},
                                "kind": {"type": "string"},
                                "name": {"type": ["string", "null"]},
                                "selector": {
                                    "type": ["object", "null"],
                                    "additionalProperties": {"type": "string"},
                                },
                            },
                            "required": ["namespace", "kind", "name", "selector"],
                        },
                        "arguments": {"type": "object"},
                        "reversible": {"type": "boolean"},
                        "confidence_score": {"type": "number"},
                        "blast_radius_score": {"type": "number"},
                        "requires_approval": {"type": "boolean"},
                        "verification_hints": {"type": "object"},
                        "rollback_hints": {"type": "object"},
                    },
                    "required": [
                        "intent_id",
                        "intent",
                        "operation_family",
                        "target",
                        "arguments",
                        "reversible",
                        "confidence_score",
                        "blast_radius_score",
                        "requires_approval",
                        "verification_hints",
                        "rollback_hints",
                    ],
                },
            },
        },
        "required": ["diagnosis_summary", "likely_causes", "missing_information", "intents"],
    }


def build_reasoner_prompts(
    *,
    incident_summary: str,
    observations: ObservationBundle,
    incident_class_hint: str,
    capability_catalog: CapabilityCatalog,
) -> tuple[str, str]:
    system_prompt = (
        "You are the HERALD Reasoner.\n"
        "Diagnose the Kubernetes incident from live observations and emit 1-3 ranked recovery intents.\n"
        "Do not emit kubectl syntax.\n"
        "Use the capability catalog as a description of what the platform can do, not as a fixed menu.\n"
        "Every intent must require human approval.\n"
        "Prefer namespaced, reversible, low-blast-radius operations.\n"
        "Return output strictly in the requested JSON schema."
    )
    user_payload = {
        "incident_summary": incident_summary,
        "incident_class_hint": incident_class_hint,
        "observations": _prompt_safe_observations(observations),
        "capability_catalog": _to_jsonable(capability_catalog),
    }
    user_prompt = (
        "Reason about the current incident using the compact observation summary below.\n\n"
        f"{json.dumps(user_payload, sort_keys=True)}"
    )
    return system_prompt, user_prompt


def parse_reasoner_llm_result(payload: dict[str, Any]) -> ReasonerOutput:
    return reasoner_output_from_dict(payload)


def _prompt_safe_observations(observations: ObservationBundle) -> dict[str, Any]:
    prometheus = observations.prometheus
    return {
        "incident_id": observations.incident_id,
        "incident_class_hint": observations.incident_class_hint,
        "namespace_hint": observations.namespace_hint,
        "alert_context": {
            "labels": dict(observations.alert_context.get("labels", {})),
            "annotations": dict(observations.alert_context.get("annotations", {})),
        },
        "kubernetes_sections": sorted(observations.kubernetes.keys()),
        "kubernetes_summary": {
            "deployment": _section_prompt_summary(observations.kubernetes.get("deployment_summary")),
            "pods": _section_prompt_summary(observations.kubernetes.get("pod_status_summary")),
            "events": _section_prompt_summary(observations.kubernetes.get("event_summary")),
            "endpoints": _section_prompt_summary(observations.kubernetes.get("endpoint_summary")),
            "replica_sets": _section_prompt_summary(observations.kubernetes.get("replica_set_summary")),
            "rollout": _section_prompt_summary(observations.kubernetes.get("rollout_summary")),
        },
        "prometheus_sections": sorted(prometheus.keys()),
        "prometheus_values": {
            "ready": _metric_prompt_summary(prometheus.get("ready")),
            "incident_signal": _metric_prompt_summary(prometheus.get("incident_signal")),
        },
        "errors": list(observations.errors),
    }


def _metric_prompt_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary: dict[str, Any] = {"status": value.get("status")}
    if "value" in value:
        summary["value"] = value.get("value")
    return summary


def _section_prompt_summary(value: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(value, dict):
        return _to_jsonable(value)
    if isinstance(value, list):
        return _to_jsonable(value)
    return None


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
