from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from schemas.intents import OperationIntent, ResourceTarget


ReplanDecision = Literal["propose_new_intent", "escalate", "no_action"]
VALID_REPLAN_DECISIONS: tuple[ReplanDecision, ...] = (
    "propose_new_intent",
    "escalate",
    "no_action",
)


@dataclass(slots=True)
class ReplanOutput:
    decision: ReplanDecision
    rationale: str
    intents: list[OperationIntent] = field(default_factory=list)
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in VALID_REPLAN_DECISIONS:
            raise ValueError(f"unsupported replan decision: {self.decision}")
        _require_non_empty_string(self.rationale, "rationale")
        if not isinstance(self.intents, list):
            raise TypeError("intents must be a list[OperationIntent]")
        for intent in self.intents:
            if not isinstance(intent, OperationIntent):
                raise TypeError("intents must contain only OperationIntent values")
        if self.stop_reason is not None and not isinstance(self.stop_reason, str):
            raise TypeError("stop_reason must be a str or None")


def replan_output_from_dict(payload: dict[str, Any]) -> ReplanOutput:
    if not isinstance(payload, dict):
        raise TypeError("ReplanOutput payload must be a dict")
    intents_raw = payload.get("intents", [])
    if not isinstance(intents_raw, list):
        raise TypeError("ReplanOutput intents must be a list")
    stop_reason = payload.get("stop_reason")
    if stop_reason is not None:
        stop_reason = str(stop_reason)
    return ReplanOutput(
        decision=payload["decision"],
        rationale=str(payload["rationale"]),
        intents=[_operation_intent_from_dict(item) for item in intents_raw],
        stop_reason=stop_reason,
    )


def _operation_intent_from_dict(payload: dict[str, Any]) -> OperationIntent:
    if not isinstance(payload, dict):
        raise TypeError("OperationIntent payload must be a dict")
    return OperationIntent(
        intent_id=str(payload["intent_id"]),
        intent=str(payload["intent"]),
        operation_family=payload["operation_family"],
        target=_resource_target_from_dict(payload["target"]),
        arguments=dict(payload.get("arguments", {})),
        reversible=bool(payload["reversible"]),
        confidence_score=float(payload["confidence_score"]),
        blast_radius_score=float(payload["blast_radius_score"]),
        requires_approval=bool(payload["requires_approval"]),
        verification_hints=dict(payload.get("verification_hints", {})),
        rollback_hints=dict(payload.get("rollback_hints", {})),
    )


def _resource_target_from_dict(payload: dict[str, Any]) -> ResourceTarget:
    if not isinstance(payload, dict):
        raise TypeError("ResourceTarget payload must be a dict")
    namespace = payload.get("namespace")
    if namespace is not None:
        namespace = str(namespace)
    name = payload.get("name")
    if name is not None:
        name = str(name)
    selector_raw = payload.get("selector")
    selector: dict[str, str] | None = None
    if selector_raw is not None:
        if not isinstance(selector_raw, dict):
            raise TypeError("selector must be a dict[str, str] when provided")
        selector = {str(key): str(value) for key, value in selector_raw.items()}
    return ResourceTarget(
        namespace=namespace,
        kind=str(payload["kind"]),
        name=name,
        selector=selector,
    )


def _require_non_empty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
