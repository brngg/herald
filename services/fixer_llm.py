from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from schemas.remediation import ActionTypes, RemediationAction


@dataclass(frozen=True, slots=True)
class FixerLLMResult:
    """LLM-proposed Fixer output after validation."""

    rationale: str
    actions: list[RemediationAction]


class FixerLLM(Protocol):
    """Provider-agnostic interface for the Fixer model.

    This lets us swap OpenAI/Anthropic/local models without changing agent logic.
    """

    def propose(self, *, incident_summary: str, evidence: dict[str, Any]) -> FixerLLMResult: ...


def fixer_output_json_schema() -> dict[str, Any]:
    """JSON schema used for Structured Outputs.

    This schema is intentionally small: actions + a short rationale string.
    """

    action_type_enum = list(get_args(ActionTypes))
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rationale": {
                "type": "string",
                "description": "Short justification for why these actions were selected.",
            },
            "actions": {
                "type": "array",
                "description": "Ranked remediation actions (highest confidence first).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action_id": {"type": "string"},
                        "action_type": {"type": "string", "enum": action_type_enum},
                        "description": {"type": "string"},
                        "confidence_score": {"type": "number"},
                        "blast_radius_score": {"type": "number"},
                        "requires_approval": {"type": "boolean"},
                        "parameters": {"type": "object"},
                    },
                    "required": [
                        "action_id",
                        "action_type",
                        "description",
                        "confidence_score",
                        "blast_radius_score",
                        "requires_approval",
                        "parameters",
                    ],
                },
            },
        },
        "required": ["rationale", "actions"],
    }


def build_fixer_prompts(*, incident_summary: str, evidence: dict[str, Any]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the Fixer LLM."""

    # Keep this prompt deterministic and auditable; avoid embedding secrets.
    system_prompt = (
        "You are the Fixer in a Kubernetes incident-response system called HERALD.\n"
        "Given incident context (summary + evidence), propose bounded, reversible remediation actions.\n"
        "Do not execute anything. Always include requires_approval=true for all actions.\n"
        "Return output strictly in the requested JSON schema."
    )

    # Send only the minimum planning context to hosted models.
    evidence_json = json.dumps(_prompt_safe_evidence(evidence), sort_keys=True)
    user_prompt = (
        "Incident summary:\n"
        f"{incident_summary}\n\n"
        "Evidence (JSON):\n"
        f"{evidence_json}\n\n"
        "Task:\n"
        "- Propose 1-3 remediation actions, ranked by confidence.\n"
        "- Keep actions low blast radius and reversible.\n"
        "- Provide a short rationale."
    )
    return system_prompt, user_prompt


def _prompt_safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Redact evidence down to the minimum useful fields for planning."""

    prompt_evidence: dict[str, Any] = {}
    for key in (
        "incident_class",
        "incident_class_normalized",
        "alertname",
        "namespace",
        "severity",
        "summary",
        "pod",
        "container",
    ):
        value = evidence.get(key)
        if value is not None:
            prompt_evidence[key] = value

    labels = evidence.get("labels")
    if isinstance(labels, dict):
        for key in ("deployment", "app", "service"):
            value = labels.get(key)
            if isinstance(value, str) and value:
                prompt_evidence["deployment_hint"] = value
                break

    return prompt_evidence


def parse_fixer_llm_result(payload: dict[str, Any]) -> FixerLLMResult:
    """Parse and validate an LLM payload into FixerLLMResult.

    This is the safety boundary between untrusted model output and typed actions.
    """

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("LLM payload missing non-empty rationale")

    actions_raw = payload.get("actions")
    if not isinstance(actions_raw, list):
        raise ValueError("LLM payload missing actions list")

    actions: list[RemediationAction] = []
    for idx, item in enumerate(actions_raw):
        if not isinstance(item, dict):
            raise ValueError(f"LLM action at index {idx} must be an object")
        try:
            actions.append(RemediationAction(**_normalize_action_payload(item)))
        except Exception as exc:
            raise ValueError(f"LLM action at index {idx} failed validation: {exc}") from exc

    return FixerLLMResult(rationale=rationale.strip(), actions=actions)


def _normalize_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider-specific action fields into HERALD's canonical contract."""

    normalized = dict(payload)
    parameters = normalized.get("parameters")
    action_type = normalized.get("action_type")

    if isinstance(parameters, dict):
        normalized["parameters"] = _normalize_action_parameters(
            action_type=action_type,
            parameters=parameters,
        )

    return normalized


def _normalize_action_parameters(*, action_type: Any, parameters: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parameters)

    if action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        deployment = normalized.get("deployment")
        if not isinstance(deployment, str) or not deployment:
            alias = normalized.get("deployment_name")
            if isinstance(alias, str) and alias:
                normalized["deployment"] = alias
        normalized.pop("deployment_name", None)

    return normalized
