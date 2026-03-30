from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.execution import VALID_EXECUTION_TOOL_NAMES
from schemas.intents import OperationFamily, ResourceTarget


@dataclass(slots=True)
class ExecutionPlanStep:
    step_id: str
    tool_name: str
    command: list[str]
    expected_effect: str
    reversible: bool
    verification_hints: dict[str, Any]

    def __post_init__(self) -> None:
        _require_non_empty_string(self.step_id, "step_id")
        _require_non_empty_string(self.tool_name, "tool_name")
        if self.tool_name not in VALID_EXECUTION_TOOL_NAMES:
            raise ValueError(f"unsupported execution tool_name: {self.tool_name}")
        if not isinstance(self.command, list):
            raise TypeError("command must be a list[str]")
        for item in self.command:
            if not isinstance(item, str):
                raise TypeError("command must contain only strings")
        _require_non_empty_string(self.expected_effect, "expected_effect")
        if not isinstance(self.reversible, bool):
            raise TypeError("reversible must be a bool")
        if not isinstance(self.verification_hints, dict):
            raise TypeError("verification_hints must be a dict")


@dataclass(slots=True)
class ExecutionPlan:
    intent_id: str
    operation_family: OperationFamily
    target: ResourceTarget
    summary: str
    steps: list[ExecutionPlanStep] = field(default_factory=list)
    allowed_tool_names: list[str] = field(default_factory=list)
    blast_radius_score: float = 0.0
    requires_approval: bool = True
    rollback_outline: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.intent_id, "intent_id")
        if not isinstance(self.operation_family, str):
            raise TypeError("operation_family must be a str")
        if not isinstance(self.target, ResourceTarget):
            raise TypeError("target must be a ResourceTarget")
        _require_non_empty_string(self.summary, "summary")
        if not isinstance(self.steps, list):
            raise TypeError("steps must be a list[ExecutionPlanStep]")
        for step in self.steps:
            if not isinstance(step, ExecutionPlanStep):
                raise TypeError("steps must contain only ExecutionPlanStep values")
        if not isinstance(self.allowed_tool_names, list):
            raise TypeError("allowed_tool_names must be a list[str]")
        for tool_name in self.allowed_tool_names:
            if not isinstance(tool_name, str):
                raise TypeError("allowed_tool_names must contain only strings")
            if not tool_name:
                raise ValueError("allowed_tool_names must not contain empty strings")
            if tool_name not in VALID_EXECUTION_TOOL_NAMES:
                raise ValueError(f"unsupported execution tool_name: {tool_name}")
        if len(set(self.allowed_tool_names)) != len(self.allowed_tool_names):
            raise ValueError("allowed_tool_names must not contain duplicates")
        if not _is_number(self.blast_radius_score):
            raise TypeError("blast_radius_score must be a float-compatible number")
        if self.blast_radius_score < 0.0 or self.blast_radius_score > 1.0:
            raise ValueError("blast_radius_score must be in the range [0.0, 1.0]")
        if not isinstance(self.requires_approval, bool):
            raise TypeError("requires_approval must be a bool")
        if not isinstance(self.rollback_outline, dict):
            raise TypeError("rollback_outline must be a dict")
        if self.steps and not self.allowed_tool_names:
            raise ValueError("allowed_tool_names must be non-empty when steps are present")
        if not self.steps and self.allowed_tool_names:
            raise ValueError("non-executable plans must not advertise allowed_tool_names")
        if self.steps:
            allowed = set(self.allowed_tool_names)
            for step in self.steps:
                if step.tool_name not in allowed:
                    raise ValueError(
                        f"step tool_name {step.tool_name!r} must be included in allowed_tool_names"
                    )
        self.blast_radius_score = float(self.blast_radius_score)


@dataclass(slots=True)
class SynthesisOutput:
    summary: str
    plans: list[ExecutionPlan] = field(default_factory=list)
    unsupported_intents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.summary, "summary")
        if not isinstance(self.plans, list):
            raise TypeError("plans must be a list[ExecutionPlan]")
        for plan in self.plans:
            if not isinstance(plan, ExecutionPlan):
                raise TypeError("plans must contain only ExecutionPlan values")
        if not isinstance(self.unsupported_intents, list):
            raise TypeError("unsupported_intents must be a list[dict[str, Any]]")
        for intent in self.unsupported_intents:
            if not isinstance(intent, dict):
                raise TypeError("unsupported_intents must contain only dict values")
        if not isinstance(self.warnings, list):
            raise TypeError("warnings must be a list[str]")
        for warning in self.warnings:
            if not isinstance(warning, str):
                raise TypeError("warnings must contain only strings")
            if not warning:
                raise ValueError("warnings must not contain empty strings")


def execution_plan_step_from_dict(payload: dict[str, Any]) -> ExecutionPlanStep:
    if not isinstance(payload, dict):
        raise TypeError("ExecutionPlanStep payload must be a dict")
    return ExecutionPlanStep(
        step_id=str(payload["step_id"]),
        tool_name=payload["tool_name"],
        command=list(payload["command"]),
        expected_effect=str(payload["expected_effect"]),
        reversible=bool(payload["reversible"]),
        verification_hints=dict(payload.get("verification_hints", {})),
    )


def execution_plan_from_dict(payload: dict[str, Any]) -> ExecutionPlan:
    if not isinstance(payload, dict):
        raise TypeError("ExecutionPlan payload must be a dict")
    return ExecutionPlan(
        intent_id=str(payload["intent_id"]),
        operation_family=payload["operation_family"],
        target=_resource_target_from_dict(payload["target"]),
        summary=str(payload["summary"]),
        steps=[execution_plan_step_from_dict(item) for item in list(payload.get("steps", []))],
        allowed_tool_names=list(payload.get("allowed_tool_names", [])),
        blast_radius_score=float(payload.get("blast_radius_score", 0.0)),
        requires_approval=bool(payload.get("requires_approval", True)),
        rollback_outline=dict(payload.get("rollback_outline", {})),
    )


def synthesis_output_from_dict(payload: dict[str, Any]) -> SynthesisOutput:
    if not isinstance(payload, dict):
        raise TypeError("SynthesisOutput payload must be a dict")
    plans_raw = payload.get("plans", [])
    if not isinstance(plans_raw, list):
        raise TypeError("SynthesisOutput plans must be a list")
    unsupported_raw = payload.get("unsupported_intents", [])
    if not isinstance(unsupported_raw, list):
        raise TypeError("SynthesisOutput unsupported_intents must be a list")
    warnings = _string_list(payload.get("warnings"), field_name="warnings")
    return SynthesisOutput(
        summary=str(payload["summary"]),
        plans=[execution_plan_from_dict(item) for item in plans_raw],
        unsupported_intents=[dict(item) for item in unsupported_raw],
        warnings=warnings,
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


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list[str]")
    values = [str(item) for item in value]
    for item in values:
        if not item:
            raise ValueError(f"{field_name} must not contain empty strings")
    return values


def _require_non_empty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
