from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ActionTypes = Literal[
    "rollout_undo_deployment",
    "rollout_restart_deployment",
    "delete_stresschaos",
    "set_deployment_env_var",
    "apply_k8s_manifest",
    "scale_deployment",
    "escalate",
    "do_nothing",
]

VALID_ACTION_TYPES: tuple[ActionTypes, ...] = (
    "rollout_undo_deployment",
    "rollout_restart_deployment",
    "delete_stresschaos",
    "set_deployment_env_var",
    "apply_k8s_manifest",
    "scale_deployment",
    "escalate",
    "do_nothing",
)

@dataclass(slots=True)
class RemediationAction:
    action_id: str
    action_type: ActionTypes
    description: str
    confidence_score: float
    blast_radius_score: float
    requires_approval: bool
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str):
            raise TypeError("action_id must be a str")
        if not self.action_id:
            raise ValueError("action_id must be non-empty")
        if self.action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"unsupported action_type: {self.action_type}")
        if not isinstance(self.description, str):
            raise TypeError("description must be str")
        if not self.description:
            raise ValueError("description must be non-empty")
        if not _is_number(self.confidence_score):
            raise TypeError("confidence_score must be a float-compatible number")
        if self.confidence_score < 0.0 or self.confidence_score > 1.0:
            raise ValueError("confidence_score must be in the range [0.0, 1.0]")
        if not _is_number(self.blast_radius_score):
            raise TypeError("blast_radius_score must be a float-compatible number")
        if self.blast_radius_score < 0.0 or self.blast_radius_score > 1.0:
            raise ValueError("blast_radius_score must be in the range [0.0, 1.0]")
        if not isinstance(self.requires_approval, bool):
            raise TypeError("requires_approval must be a bool")
        if not isinstance(self.parameters, dict):
            raise TypeError("parameters must be a dict")
        self.confidence_score = float(self.confidence_score)
        self.blast_radius_score = float(self.blast_radius_score)
        self._validate_action_parameters()

    def _validate_action_parameters(self) -> None:
        if self.action_type in ("rollout_undo_deployment", "rollout_restart_deployment"):
            _require_non_empty_string(self.parameters, "namespace", self.action_type)
            _require_non_empty_string(self.parameters, "deployment", self.action_type)
            return

        if self.action_type == "delete_stresschaos":
            _require_non_empty_string(self.parameters, "namespace", self.action_type)
            _require_non_empty_string(self.parameters, "name", self.action_type)
            return

        if self.action_type == "escalate":
            _require_non_empty_string(self.parameters, "reason", self.action_type)
            return


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_non_empty_string(
    parameters: dict[str, Any],
    key: str,
    action_type: ActionTypes,
) -> None:
    value = parameters.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{action_type} parameters must include string {key!r}")
    if not value:
        raise ValueError(f"{action_type} parameters must include non-empty {key!r}")
