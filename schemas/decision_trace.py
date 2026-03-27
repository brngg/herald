from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

HumanApproval = Literal["approved", "rejected", "n/a"]
JudgeVerdict = Literal["pass", "fail", "n/a"]
TraceNodeName = Literal[
    "fixer",
    "judge",
    "hitl_gate",
    "human_approval",
    "pre_check",
    "execution_worker",
    "rollout_wait",
    "post_check",
    "rollback",
    "finalization",
]
FinalState = Literal[
    "pending_approval",
    "executing",
    "recovered",
    "unrecovered",
    "escalated",
    "rolled_back",
    "rejected",
]

VALID_HUMAN_APPROVALS: tuple[HumanApproval, ...] = ("approved", "rejected", "n/a")
VALID_JUDGE_VERDICTS: tuple[JudgeVerdict, ...] = ("pass", "fail", "n/a")
VALID_TRACE_NODE_NAMES: tuple[TraceNodeName, ...] = (
    "fixer",
    "judge",
    "hitl_gate",
    "human_approval",
    "pre_check",
    "execution_worker",
    "rollout_wait",
    "post_check",
    "rollback",
    "finalization",
)
VALID_FINAL_STATES: tuple[FinalState, ...] = (
    "pending_approval",
    "executing",
    "recovered",
    "unrecovered",
    "escalated",
    "rolled_back",
    "rejected",
)

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
    final_state: FinalState
    node_runs_by_node: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    latest_run_id_by_node: dict[str, str] = field(default_factory=dict)

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
        if self.final_state not in VALID_FINAL_STATES:
            raise ValueError(f"unsupported final_state: {self.final_state}")
        _validate_node_runs_by_node(self.node_runs_by_node)
        _validate_latest_run_ids(self.node_runs_by_node, self.latest_run_id_by_node)


def _validate_node_runs_by_node(node_runs_by_node: dict[str, dict[str, dict[str, Any]]]) -> None:
    if not isinstance(node_runs_by_node, dict):
        raise TypeError("node_runs_by_node must be a dict")

    seen_sequences: set[int] = set()
    for node_name, runs in node_runs_by_node.items():
        if node_name not in VALID_TRACE_NODE_NAMES:
            raise ValueError(f"unsupported trace node_name: {node_name}")
        if not isinstance(runs, dict):
            raise TypeError("node_runs_by_node values must be dicts")
        last_attempt = 0
        ordered_runs = sorted(runs.values(), key=_run_sequence)
        for run_id, run in runs.items():
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id keys must be non-empty strings")
            _validate_node_run(node_name=node_name, run=run)
        for run in ordered_runs:
            sequence = int(run["sequence"])
            attempt = int(run["attempt"])
            if sequence in seen_sequences:
                raise ValueError(f"duplicate DecisionTrace sequence: {sequence}")
            seen_sequences.add(sequence)
            if attempt <= last_attempt:
                raise ValueError(f"non-monotonic attempt for node {node_name}")
            last_attempt = attempt


def _validate_node_run(*, node_name: str, run: dict[str, Any]) -> None:
    if not isinstance(run, dict):
        raise TypeError("node run values must be dicts")
    required_fields = (
        "run_id",
        "node_name",
        "sequence",
        "attempt",
        "started_at",
        "finished_at",
        "status",
        "summary",
        "input_summary",
        "output_summary",
        "artifact_refs",
    )
    for field_name in required_fields:
        if field_name not in run:
            raise ValueError(f"node run missing required field: {field_name}")

    if run["node_name"] != node_name:
        raise ValueError(f"node run node_name mismatch for {node_name}")
    if not isinstance(run["run_id"], str) or not run["run_id"]:
        raise TypeError("node run run_id must be a non-empty str")
    if not isinstance(run["sequence"], int) or run["sequence"] <= 0:
        raise TypeError("node run sequence must be a positive int")
    if not isinstance(run["attempt"], int) or run["attempt"] <= 0:
        raise TypeError("node run attempt must be a positive int")
    if not isinstance(run["started_at"], str) or not run["started_at"]:
        raise TypeError("node run started_at must be a non-empty str")
    if not isinstance(run["finished_at"], str) or not run["finished_at"]:
        raise TypeError("node run finished_at must be a non-empty str")
    if not isinstance(run["status"], str) or not run["status"]:
        raise TypeError("node run status must be a non-empty str")
    if not isinstance(run["summary"], str) or not run["summary"]:
        raise TypeError("node run summary must be a non-empty str")
    if "llm_explanation" in run:
        llm_explanation = run["llm_explanation"]
        if llm_explanation is not None and (not isinstance(llm_explanation, str) or not llm_explanation):
            raise TypeError("node run llm_explanation must be a non-empty str or None")
    if not isinstance(run["input_summary"], dict):
        raise TypeError("node run input_summary must be a dict")
    if not isinstance(run["output_summary"], dict):
        raise TypeError("node run output_summary must be a dict")
    if not isinstance(run["artifact_refs"], list):
        raise TypeError("node run artifact_refs must be a list")
    for ref in run["artifact_refs"]:
        if not isinstance(ref, str):
            raise TypeError("node run artifact_refs must contain only strings")


def _validate_latest_run_ids(
    node_runs_by_node: dict[str, dict[str, dict[str, Any]]],
    latest_run_id_by_node: dict[str, str],
) -> None:
    if not isinstance(latest_run_id_by_node, dict):
        raise TypeError("latest_run_id_by_node must be a dict")
    if not node_runs_by_node and latest_run_id_by_node:
        raise ValueError("latest_run_id_by_node must be empty when node_runs_by_node is empty")

    for node_name, runs in node_runs_by_node.items():
        latest_run_id = latest_run_id_by_node.get(node_name)
        if latest_run_id is None:
            raise ValueError(f"missing latest run id for node {node_name}")
        if latest_run_id not in runs:
            raise ValueError(f"latest run id {latest_run_id!r} does not exist for node {node_name}")

    for node_name in latest_run_id_by_node:
        if node_name not in node_runs_by_node:
            raise ValueError(f"latest_run_id_by_node references unknown node {node_name}")


def _run_sequence(run: dict[str, Any]) -> int:
    return int(run["sequence"])
