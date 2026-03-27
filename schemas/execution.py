from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ExecutionActionType = Literal[
    "rollout_undo_deployment",
    "rollout_restart_deployment",
]
ExecutionStatus = Literal["succeeded", "failed"]
ExecutionToolName = Literal[
    "get_deployment_context",
    "get_rollout_status",
    "rollout_undo_deployment",
    "rollout_restart_deployment",
]

VALID_EXECUTION_ACTION_TYPES: tuple[ExecutionActionType, ...] = (
    "rollout_undo_deployment",
    "rollout_restart_deployment",
)
VALID_EXECUTION_STATUSES: tuple[ExecutionStatus, ...] = ("succeeded", "failed")
VALID_EXECUTION_TOOL_NAMES: tuple[ExecutionToolName, ...] = (
    "get_deployment_context",
    "get_rollout_status",
    "rollout_undo_deployment",
    "rollout_restart_deployment",
)


@dataclass(slots=True)
class ExecutionDispatch:
    incident_id: str
    action_id: str
    action_type: ExecutionActionType
    parameters: dict[str, Any]
    worker_id: str
    requested_at: str
    allowed_tool_names: list[ExecutionToolName]
    max_steps: int

    def __post_init__(self) -> None:
        _require_non_empty_string(self.incident_id, "incident_id")
        _require_non_empty_string(self.action_id, "action_id")
        if self.action_type not in VALID_EXECUTION_ACTION_TYPES:
            raise ValueError(f"unsupported execution action_type: {self.action_type}")
        if not isinstance(self.parameters, dict):
            raise TypeError("parameters must be a dict")
        _require_non_empty_string(self.worker_id, "worker_id")
        _require_non_empty_string(self.requested_at, "requested_at")
        _require_rollout_parameters(self.parameters, self.action_type)
        _require_allowed_tool_names(self.allowed_tool_names, self.action_type)
        if not isinstance(self.max_steps, int):
            raise TypeError("max_steps must be an int")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")


@dataclass(slots=True)
class ExecutionResult:
    worker_id: str
    action_id: str
    status: ExecutionStatus
    started_at: str
    finished_at: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    summary: str
    tool_transcript: list[dict[str, Any]]

    def __post_init__(self) -> None:
        _require_non_empty_string(self.worker_id, "worker_id")
        _require_non_empty_string(self.action_id, "action_id")
        if self.status not in VALID_EXECUTION_STATUSES:
            raise ValueError(f"unsupported execution status: {self.status}")
        _require_non_empty_string(self.started_at, "started_at")
        _require_non_empty_string(self.finished_at, "finished_at")
        if not isinstance(self.command, list):
            raise TypeError("command must be a list[str]")
        for item in self.command:
            if not isinstance(item, str):
                raise TypeError("command must contain only strings")
        if not isinstance(self.returncode, int):
            raise TypeError("returncode must be an int")
        if not isinstance(self.stdout, str):
            raise TypeError("stdout must be a str")
        if not isinstance(self.stderr, str):
            raise TypeError("stderr must be a str")
        _require_non_empty_string(self.summary, "summary")
        if not isinstance(self.tool_transcript, list):
            raise TypeError("tool_transcript must be a list[dict]")
        for item in self.tool_transcript:
            if not isinstance(item, dict):
                raise TypeError("tool_transcript must contain only dict items")


def execution_dispatch_from_dict(payload: dict[str, Any]) -> ExecutionDispatch:
    return ExecutionDispatch(
        incident_id=str(payload["incident_id"]),
        action_id=str(payload["action_id"]),
        action_type=payload["action_type"],
        parameters=dict(payload["parameters"]),
        worker_id=str(payload["worker_id"]),
        requested_at=str(payload["requested_at"]),
        allowed_tool_names=list(payload["allowed_tool_names"]),
        max_steps=int(payload["max_steps"]),
    )


def execution_result_from_dict(payload: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        worker_id=str(payload["worker_id"]),
        action_id=str(payload["action_id"]),
        status=payload["status"],
        started_at=str(payload["started_at"]),
        finished_at=str(payload["finished_at"]),
        command=list(payload["command"]),
        returncode=int(payload["returncode"]),
        stdout=str(payload["stdout"]),
        stderr=str(payload["stderr"]),
        summary=str(payload["summary"]),
        tool_transcript=list(payload["tool_transcript"]),
    )


def _require_non_empty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


def _require_rollout_parameters(
    parameters: dict[str, Any],
    action_type: ExecutionActionType,
) -> None:
    for key in ("namespace", "deployment"):
        value = parameters.get(key)
        if not isinstance(value, str):
            raise TypeError(f"{action_type} parameters must include string {key!r}")
        if not value:
            raise ValueError(f"{action_type} parameters must include non-empty {key!r}")


def _require_allowed_tool_names(
    allowed_tool_names: list[ExecutionToolName],
    action_type: ExecutionActionType,
) -> None:
    if not isinstance(allowed_tool_names, list):
        raise TypeError("allowed_tool_names must be a list[str]")
    if not allowed_tool_names:
        raise ValueError("allowed_tool_names must be non-empty")

    seen: set[str] = set()
    for tool_name in allowed_tool_names:
        if tool_name not in VALID_EXECUTION_TOOL_NAMES:
            raise ValueError(f"unsupported execution tool: {tool_name}")
        if tool_name in seen:
            raise ValueError(f"duplicate execution tool: {tool_name}")
        seen.add(tool_name)

    allowed_for_action = _allowed_tools_for_action(action_type)
    if not set(allowed_tool_names).issubset(allowed_for_action):
        raise ValueError(
            f"{action_type} allowed_tool_names must be a subset of {sorted(allowed_for_action)}"
        )

    if action_type not in allowed_tool_names:
        raise ValueError(f"{action_type} dispatch must include the approved execution tool")


def _allowed_tools_for_action(action_type: ExecutionActionType) -> set[str]:
    readonly_tools = {"get_deployment_context", "get_rollout_status"}
    return readonly_tools | {action_type}
