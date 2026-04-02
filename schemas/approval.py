from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.execution_plan import ExecutionPlan, execution_plan_from_dict


@dataclass(slots=True)
class ApprovalCandidate:
    candidate_id: str
    summary: str
    confidence_score: float
    blast_radius_score: float
    requires_approval: bool
    execution_plan: ExecutionPlan
    display_labels: list[str] = field(default_factory=list)
    legacy_action_hint: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.candidate_id, "candidate_id")
        _require_non_empty_string(self.summary, "summary")
        if not _is_number(self.confidence_score):
            raise TypeError("confidence_score must be a float-compatible number")
        if not _is_number(self.blast_radius_score):
            raise TypeError("blast_radius_score must be a float-compatible number")
        self.confidence_score = float(self.confidence_score)
        self.blast_radius_score = float(self.blast_radius_score)
        if self.confidence_score < 0.0 or self.confidence_score > 1.0:
            raise ValueError("confidence_score must be in the range [0.0, 1.0]")
        if self.blast_radius_score < 0.0 or self.blast_radius_score > 1.0:
            raise ValueError("blast_radius_score must be in the range [0.0, 1.0]")
        if not isinstance(self.requires_approval, bool):
            raise TypeError("requires_approval must be a bool")
        if not isinstance(self.execution_plan, ExecutionPlan):
            raise TypeError("execution_plan must be an ExecutionPlan")
        if not isinstance(self.display_labels, list):
            raise TypeError("display_labels must be a list[str]")
        for label in self.display_labels:
            _require_non_empty_string(label, "display_labels item")
        if self.legacy_action_hint is not None and not isinstance(self.legacy_action_hint, dict):
            raise TypeError("legacy_action_hint must be a dict or None")


def approval_candidate_from_dict(payload: dict[str, Any]) -> ApprovalCandidate:
    if not isinstance(payload, dict):
        raise TypeError("ApprovalCandidate payload must be a dict")
    return ApprovalCandidate(
        candidate_id=str(payload["candidate_id"]),
        summary=str(payload["summary"]),
        confidence_score=float(payload["confidence_score"]),
        blast_radius_score=float(payload["blast_radius_score"]),
        requires_approval=bool(payload["requires_approval"]),
        execution_plan=execution_plan_from_dict(dict(payload["execution_plan"])),
        display_labels=[str(item) for item in list(payload.get("display_labels", []))],
        legacy_action_hint=dict(payload["legacy_action_hint"]) if payload.get("legacy_action_hint") is not None else None,
    )


def _require_non_empty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
