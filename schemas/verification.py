from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from schemas.intents import ResourceTarget


VerificationCheckType = Literal[
    "kubernetes_rollout_status",
    "kubernetes_resource_absent",
    "prometheus_readiness_positive",
    "prometheus_crashloop_zero",
    "prometheus_probe_positive",
    "prometheus_cpu_below_threshold",
    "prometheus_network_receive_above_threshold",
]
VerificationStatusV2 = Literal["passed", "unrecovered", "not_run"]


@dataclass(slots=True)
class VerificationCheck:
    check_id: str
    check_type: VerificationCheckType
    summary: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty_string(self.check_id, "check_id")
        if self.check_type not in VALID_VERIFICATION_CHECK_TYPES:
            raise ValueError(f"unsupported check_type: {self.check_type}")
        _require_non_empty_string(self.summary, "summary")
        if not isinstance(self.parameters, dict):
            raise TypeError("parameters must be a dict")


@dataclass(slots=True)
class VerificationPlan:
    verification_id: str
    action_id: str
    action_type: str
    target: ResourceTarget
    summary: str
    checks: list[VerificationCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rollback_warning: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.verification_id, "verification_id")
        _require_non_empty_string(self.action_id, "action_id")
        _require_non_empty_string(self.action_type, "action_type")
        if not isinstance(self.target, ResourceTarget):
            raise TypeError("target must be a ResourceTarget")
        _require_non_empty_string(self.summary, "summary")
        if not isinstance(self.checks, list):
            raise TypeError("checks must be a list[VerificationCheck]")
        for check in self.checks:
            if not isinstance(check, VerificationCheck):
                raise TypeError("checks must contain only VerificationCheck values")
        if not isinstance(self.warnings, list):
            raise TypeError("warnings must be a list[str]")
        for warning in self.warnings:
            if not isinstance(warning, str):
                raise TypeError("warnings must contain only strings")
            if not warning:
                raise ValueError("warnings must not contain empty strings")
        if self.rollback_warning is not None and not isinstance(self.rollback_warning, str):
            raise TypeError("rollback_warning must be a str or None")


@dataclass(slots=True)
class VerificationCheckResult:
    check_id: str
    check_type: VerificationCheckType
    passed: bool
    reason: str
    observed_value: Any
    expected_value: Any

    def __post_init__(self) -> None:
        _require_non_empty_string(self.check_id, "check_id")
        if self.check_type not in VALID_VERIFICATION_CHECK_TYPES:
            raise ValueError(f"unsupported check_type: {self.check_type}")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a bool")
        _require_non_empty_string(self.reason, "reason")


@dataclass(slots=True)
class VerificationResultV2:
    verification_id: str
    status: VerificationStatusV2
    summary: str
    plan: VerificationPlan | None
    check_results: list[VerificationCheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.verification_id, "verification_id")
        if self.status not in VALID_VERIFICATION_STATUSES_V2:
            raise ValueError(f"unsupported verification status: {self.status}")
        _require_non_empty_string(self.summary, "summary")
        if self.plan is not None and not isinstance(self.plan, VerificationPlan):
            raise TypeError("plan must be a VerificationPlan or None")
        if not isinstance(self.check_results, list):
            raise TypeError("check_results must be a list[VerificationCheckResult]")
        for check_result in self.check_results:
            if not isinstance(check_result, VerificationCheckResult):
                raise TypeError("check_results must contain only VerificationCheckResult values")
        if not isinstance(self.warnings, list):
            raise TypeError("warnings must be a list[str]")
        for warning in self.warnings:
            if not isinstance(warning, str):
                raise TypeError("warnings must contain only strings")
            if not warning:
                raise ValueError("warnings must not contain empty strings")
        if self.failure_reason is not None and not isinstance(self.failure_reason, str):
            raise TypeError("failure_reason must be a str or None")


VALID_VERIFICATION_CHECK_TYPES: tuple[VerificationCheckType, ...] = (
    "kubernetes_rollout_status",
    "kubernetes_resource_absent",
    "prometheus_readiness_positive",
    "prometheus_crashloop_zero",
    "prometheus_probe_positive",
    "prometheus_cpu_below_threshold",
    "prometheus_network_receive_above_threshold",
)
VALID_VERIFICATION_STATUSES_V2: tuple[VerificationStatusV2, ...] = (
    "passed",
    "unrecovered",
    "not_run",
)


def verification_plan_from_dict(payload: dict[str, Any]) -> VerificationPlan:
    if not isinstance(payload, dict):
        raise TypeError("VerificationPlan payload must be a dict")
    checks_raw = payload.get("checks", [])
    if not isinstance(checks_raw, list):
        raise TypeError("VerificationPlan checks must be a list")
    warnings = _string_list(payload.get("warnings"), field_name="warnings")
    rollback_warning = payload.get("rollback_warning")
    if rollback_warning is not None:
        rollback_warning = str(rollback_warning)
    return VerificationPlan(
        verification_id=str(payload["verification_id"]),
        action_id=str(payload["action_id"]),
        action_type=str(payload["action_type"]),
        target=_resource_target_from_dict(payload["target"]),
        summary=str(payload["summary"]),
        checks=[verification_check_from_dict(item) for item in checks_raw],
        warnings=warnings,
        rollback_warning=rollback_warning,
    )


def verification_result_from_dict(payload: dict[str, Any]) -> VerificationResultV2:
    if not isinstance(payload, dict):
        raise TypeError("VerificationResultV2 payload must be a dict")
    check_results_raw = payload.get("check_results", [])
    if not isinstance(check_results_raw, list):
        raise TypeError("VerificationResultV2 check_results must be a list")
    warnings = _string_list(payload.get("warnings"), field_name="warnings")
    plan_raw = payload.get("plan")
    plan = verification_plan_from_dict(plan_raw) if isinstance(plan_raw, dict) else None
    failure_reason = payload.get("failure_reason")
    if failure_reason is not None:
        failure_reason = str(failure_reason)
    return VerificationResultV2(
        verification_id=str(payload["verification_id"]),
        status=payload["status"],
        summary=str(payload["summary"]),
        plan=plan,
        check_results=[verification_check_result_from_dict(item) for item in check_results_raw],
        warnings=warnings,
        failure_reason=failure_reason,
    )


def verification_check_from_dict(payload: dict[str, Any]) -> VerificationCheck:
    if not isinstance(payload, dict):
        raise TypeError("VerificationCheck payload must be a dict")
    return VerificationCheck(
        check_id=str(payload["check_id"]),
        check_type=payload["check_type"],
        summary=str(payload["summary"]),
        parameters=dict(payload.get("parameters", {})),
    )


def verification_check_result_from_dict(payload: dict[str, Any]) -> VerificationCheckResult:
    if not isinstance(payload, dict):
        raise TypeError("VerificationCheckResult payload must be a dict")
    passed = payload["passed"]
    if not isinstance(passed, bool):
        raise TypeError("VerificationCheckResult passed must be a bool")
    return VerificationCheckResult(
        check_id=str(payload["check_id"]),
        check_type=payload["check_type"],
        passed=passed,
        reason=str(payload["reason"]),
        observed_value=payload.get("observed_value"),
        expected_value=payload.get("expected_value"),
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
    result = [str(item) for item in value]
    for item in result:
        if not item:
            raise ValueError(f"{field_name} must not contain empty strings")
    return result


def _require_non_empty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
