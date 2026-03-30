from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PolicyCheckResult:
    policy_name: str
    passed: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_name, str):
            raise TypeError("policy_name must be a str")
        if not self.policy_name:
            raise ValueError("policy_name must be non-empty")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a bool")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a str")
        if not self.reason:
            raise ValueError("reason must be non-empty")


@dataclass(slots=True)
class CritiqueCandidate:
    intent_id: str
    approved_for_consideration: bool
    concerns: list[str] = field(default_factory=list)
    policy_checks: list[PolicyCheckResult] = field(default_factory=list)
    recommended_rank: int = 1
    requires_escalation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str):
            raise TypeError("intent_id must be a str")
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if not isinstance(self.approved_for_consideration, bool):
            raise TypeError("approved_for_consideration must be a bool")
        if not isinstance(self.concerns, list):
            raise TypeError("concerns must be a list[str]")
        for concern in self.concerns:
            if not isinstance(concern, str):
                raise TypeError("concerns must contain only strings")
            if not concern:
                raise ValueError("concerns must not contain empty strings")
        if not isinstance(self.policy_checks, list):
            raise TypeError("policy_checks must be a list[PolicyCheckResult]")
        for policy_check in self.policy_checks:
            if not isinstance(policy_check, PolicyCheckResult):
                raise TypeError("policy_checks must contain only PolicyCheckResult values")
        if not isinstance(self.recommended_rank, int):
            raise TypeError("recommended_rank must be an int")
        if self.recommended_rank <= 0:
            raise ValueError("recommended_rank must be positive")
        if not isinstance(self.requires_escalation, bool):
            raise TypeError("requires_escalation must be a bool")


@dataclass(slots=True)
class CriticOutput:
    summary: str
    global_concerns: list[str] = field(default_factory=list)
    candidates: list[CritiqueCandidate] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str):
            raise TypeError("summary must be a str")
        if not self.summary:
            raise ValueError("summary must be non-empty")
        if not isinstance(self.global_concerns, list):
            raise TypeError("global_concerns must be a list[str]")
        for concern in self.global_concerns:
            if not isinstance(concern, str):
                raise TypeError("global_concerns must contain only strings")
            if not concern:
                raise ValueError("global_concerns must not contain empty strings")
        if not isinstance(self.candidates, list):
            raise TypeError("candidates must be a list[CritiqueCandidate]")
        for candidate in self.candidates:
            if not isinstance(candidate, CritiqueCandidate):
                raise TypeError("candidates must contain only CritiqueCandidate values")


def critic_output_from_dict(payload: dict[str, Any]) -> CriticOutput:
    if not isinstance(payload, dict):
        raise TypeError("CriticOutput payload must be a dict")

    global_concerns = _string_list(payload.get("global_concerns", []), field_name="global_concerns")
    candidates_raw = payload.get("candidates", [])
    if not isinstance(candidates_raw, list):
        raise TypeError("candidates must be a list")

    candidates = [_critique_candidate_from_dict(candidate) for candidate in candidates_raw]
    return CriticOutput(
        summary=str(payload["summary"]),
        global_concerns=global_concerns,
        candidates=candidates,
    )


def _critique_candidate_from_dict(payload: dict[str, Any]) -> CritiqueCandidate:
    if not isinstance(payload, dict):
        raise TypeError("CritiqueCandidate payload must be a dict")

    policy_checks_raw = payload.get("policy_checks", [])
    if not isinstance(policy_checks_raw, list):
        raise TypeError("policy_checks must be a list")

    return CritiqueCandidate(
        intent_id=str(payload["intent_id"]),
        approved_for_consideration=payload["approved_for_consideration"],
        concerns=_string_list(payload.get("concerns", []), field_name="concerns"),
        policy_checks=[_policy_check_from_dict(item) for item in policy_checks_raw],
        recommended_rank=int(payload.get("recommended_rank", 1)),
        requires_escalation=payload["requires_escalation"],
    )


def _policy_check_from_dict(payload: dict[str, Any]) -> PolicyCheckResult:
    if not isinstance(payload, dict):
        raise TypeError("PolicyCheckResult payload must be a dict")
    return PolicyCheckResult(
        policy_name=str(payload["policy_name"]),
        passed=payload["passed"],
        reason=str(payload["reason"]),
    )


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list[str]")
    strings = [str(item) for item in value]
    for item in strings:
        if not item:
            raise ValueError(f"{field_name} must not contain empty strings")
    return strings
