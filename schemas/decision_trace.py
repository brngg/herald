from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

HumanApproval = Literal["approved", "rejected", "n/a"]
JudgeVerdict = Literal["pass", "fail", "n/a"]

VALID_HUMAN_APPROVALS: tuple[HumanApproval, ...] = ("approved", "rejected", "n/a")
VALID_JUDGE_VERDICTS: tuple[JudgeVerdict, ...] = ("pass", "fail", "n/a")

@dataclass(slots=True)
class DecisionTrace:
    incident_id: str
    fixer_plan: dict[str, Any]  # or str for v0
    judge_verdict: JudgeVerdict
    judge_reason: str
    routing_decision: str
    human_approval: HumanApproval
    execution_result: dict[str, Any]  # or str for v0
    verification_result: dict[str, Any]  # or str for v0
    rollback_triggered: bool
    final_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, str):
            raise TypeError("incident_id must be a str")
        if not self.incident_id:
            raise ValueError("incident_id must be non-empty")
        if not isinstance(self.fixer_plan, dict):
            raise TypeError("fixer_plan must be a dict")
        if self.judge_verdict not in VALID_JUDGE_VERDICTS:
            raise ValueError(f"unsupported judge_verdict: {self.judge_verdict}")
        if not isinstance(self.judge_reason, str):
            raise TypeError("judge_reason must be a str")
        if not self.judge_reason:
            raise ValueError("judge_reason must be non-empty")
        if not isinstance(self.routing_decision, str):
            raise TypeError("routing_decision must be a str")
        if not self.routing_decision:
            raise ValueError("routing_decision must be non-empty")
        if self.human_approval not in VALID_HUMAN_APPROVALS:
            raise ValueError(f"unsupported human_approval: {self.human_approval}")
        if not isinstance(self.execution_result, dict):
            raise TypeError("execution_result must be a dict")
        if not isinstance(self.verification_result, dict):
            raise TypeError("verification_result must be a dict")
        if not isinstance(self.rollback_triggered, bool):
            raise TypeError("rollback_triggered must be a bool")
        if not isinstance(self.final_state, str):
            raise TypeError("final_state must be a str")
        if not self.final_state:
            raise ValueError("final_state must be non-empty")
