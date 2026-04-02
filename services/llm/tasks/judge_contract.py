from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from schemas.remediation import RemediationAction


JudgeOutcome = Literal["pass", "fail"]
VALID_JUDGE_OUTCOMES: tuple[JudgeOutcome, ...] = ("pass", "fail")


@dataclass(frozen=True, slots=True)
class JudgeLLMResult:
    """Judge output after validation."""

    verdict: JudgeOutcome
    reason: str

    def __post_init__(self) -> None:
        if self.verdict not in VALID_JUDGE_OUTCOMES:
            raise ValueError(f"unsupported judge verdict: {self.verdict}")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a str")
        if not self.reason.strip():
            raise ValueError("reason must be non-empty")


class JudgeLLM(Protocol):
    def evaluate(
        self,
        *,
        incident_summary: str,
        evidence: dict[str, Any],
        actions: list[RemediationAction],
        fixer_rationale: str | None,
    ) -> JudgeLLMResult: ...


def judge_output_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": list(VALID_JUDGE_OUTCOMES)},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
    }


def build_judge_prompts(
    *,
    incident_summary: str,
    evidence: dict[str, Any],
    actions: list[RemediationAction],
    fixer_rationale: str | None,
) -> tuple[str, str]:
    system_prompt = (
        "You are the Judge in a Kubernetes incident-response system called HERALD.\n"
        "Evaluate the Fixer plan for the current supported incident slice.\n"
        "Return pass only if the proposed plan is bounded, approval-gated, and reasonable.\n"
        "Return output strictly in the requested JSON schema."
    )

    user_payload = {
        "incident_summary": incident_summary,
        "evidence": _prompt_safe_evidence(evidence),
        "actions": [
            {
                "action_id": action.action_id,
                "action_type": action.action_type,
                "description": action.description,
                "confidence_score": action.confidence_score,
                "blast_radius_score": action.blast_radius_score,
                "requires_approval": action.requires_approval,
                "parameters": action.parameters,
            }
            for action in actions
        ],
        "fixer_rationale": fixer_rationale or "",
    }
    user_prompt = (
        "Judge the following Fixer plan for the current incident slice.\n\n"
        f"{json.dumps(user_payload, sort_keys=True)}"
    )
    return system_prompt, user_prompt


def parse_judge_llm_result(payload: dict[str, Any]) -> JudgeLLMResult:
    if not isinstance(payload, dict):
        raise TypeError("Judge LLM payload must be a dict")

    verdict = payload.get("verdict")
    reason = payload.get("reason")
    return JudgeLLMResult(verdict=verdict, reason=reason)  # type: ignore[arg-type]


def _prompt_safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
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
        "deployment_hint",
    ):
        value = evidence.get(key)
        if value is not None:
            prompt_evidence[key] = value

    labels = evidence.get("labels")
    if isinstance(labels, dict):
        for key in ("deployment", "app", "service"):
            value = labels.get(key)
            if isinstance(value, str) and value and "deployment_hint" not in prompt_evidence:
                prompt_evidence["deployment_hint"] = value
                break

    return prompt_evidence
