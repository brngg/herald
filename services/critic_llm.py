from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol

from schemas.critic import CriticOutput, critic_output_from_dict
from schemas.intents import CapabilityCatalog, ReasonerOutput
from schemas.observations import ObservationBundle


@dataclass(frozen=True, slots=True)
class CriticLLMResult:
    output: CriticOutput
    raw_response_text: str


class CriticLLM(Protocol):
    def critique(
        self,
        *,
        incident_summary: str,
        observations: ObservationBundle,
        reasoner_output: ReasonerOutput,
        policy_summary: dict[str, Any],
        capability_catalog: CapabilityCatalog,
    ) -> CriticLLMResult: ...


def critic_output_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "global_concerns": {
                "type": "array",
                "items": {"type": "string"},
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "intent_id": {"type": "string"},
                        "approved_for_consideration": {"type": "boolean"},
                        "concerns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "policy_checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "policy_name": {"type": "string"},
                                    "passed": {"type": "boolean"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["policy_name", "passed", "reason"],
                            },
                        },
                        "recommended_rank": {"type": "integer"},
                        "requires_escalation": {"type": "boolean"},
                    },
                    "required": [
                        "intent_id",
                        "approved_for_consideration",
                        "concerns",
                        "policy_checks",
                        "recommended_rank",
                        "requires_escalation",
                    ],
                },
            },
        },
        "required": ["summary", "global_concerns", "candidates"],
    }


def build_critic_prompts(
    *,
    incident_summary: str,
    observations: ObservationBundle,
    reasoner_output: ReasonerOutput,
    policy_summary: dict[str, Any],
    capability_catalog: CapabilityCatalog,
) -> tuple[str, str]:
    system_prompt = (
        "You are the HERALD Critic.\n"
        "Evaluate the Reasoner's shadow intents using principle-based safety analysis.\n"
        "Return a concise critique, explicit candidate concerns, and candidate-level policy assessment.\n"
        "Do not control execution, routing, or approval. This is shadow-only analysis.\n"
        "Return output strictly in the requested JSON schema."
    )
    user_payload = {
        "incident_summary": incident_summary,
        "observations": _prompt_safe_observations(observations),
        "reasoner_output": _to_jsonable(reasoner_output),
        "policy_summary": dict(policy_summary),
        "capability_catalog": _to_jsonable(capability_catalog),
    }
    user_prompt = (
        "Critique the Reasoner output using the compact observation and policy summary below.\n\n"
        f"{json.dumps(user_payload, sort_keys=True)}"
    )
    return system_prompt, user_prompt


def parse_critic_llm_result(payload: dict[str, Any]) -> CriticOutput:
    if not isinstance(payload, dict):
        raise TypeError("Critic LLM payload must be a dict")
    return critic_output_from_dict(payload)


def _prompt_safe_observations(observations: ObservationBundle) -> dict[str, Any]:
    return {
        "incident_id": observations.incident_id,
        "incident_class_hint": observations.incident_class_hint,
        "namespace_hint": observations.namespace_hint,
        "alert_context": {
            "labels": dict(observations.alert_context.get("labels", {})),
            "annotations": dict(observations.alert_context.get("annotations", {})),
        },
        "kubernetes_sections": sorted(observations.kubernetes.keys()),
        "prometheus_sections": sorted(observations.prometheus.keys()),
        "errors": list(observations.errors),
    }


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
