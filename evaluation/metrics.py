from __future__ import annotations

from collections.abc import Iterable
from statistics import median
from typing import Any


REQUIRED_DECISION_TRACE_FIELDS = (
    "incident_id",
    "fixer_plan",
    "judge_verdict",
    "judge_reason",
    "routing_decision",
    "human_approval",
    "execution_result",
    "verification_result",
    "rollback_triggered",
    "final_state",
)


def compute_metrics(run_artifacts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    runs = list(run_artifacts)
    total_runs = len(runs)
    if total_runs == 0:
        return {
            "total_runs": 0,
            "recommendation_top1_rate": 0.0,
            "recommendation_top2_rate": 0.0,
            "approval_policy_correct_rate": 0.0,
            "execution_success_rate": 0.0,
            "verification_correct_rate": 0.0,
            "false_recovery_rate": 0.0,
            "decision_trace_coverage_rate": 0.0,
            "median_recovery_latency_seconds": None,
            "p95_recovery_latency_seconds": None,
        }

    top1_hits = 0
    top2_hits = 0
    approval_hits = 0
    verification_hits = 0
    false_recoveries = 0
    trace_coverage_hits = 0
    executed_runs = 0
    successful_executions = 0
    recovery_latencies: list[float] = []

    for artifact in runs:
        result = artifact["result"]
        expected = artifact["expected"]
        recommended_ids = _candidate_action_ids(result)
        expected_action_id = expected.get("top_action_id")
        if isinstance(expected_action_id, str):
            if recommended_ids[:1] == [expected_action_id]:
                top1_hits += 1
            if expected_action_id in recommended_ids[:2]:
                top2_hits += 1

        if result["hitl_decision"]["requires_approval"] == expected.get("requires_approval"):
            approval_hits += 1

        trace = result["decision_trace"]
        if trace["final_state"] == expected.get("final_state"):
            verification_hits += 1
        if trace["final_state"] == "recovered" and expected.get("final_state") != "recovered":
            false_recoveries += 1
        if _has_complete_decision_trace(trace):
            trace_coverage_hits += 1

        execution_status = trace["execution_result"].get("status")
        if execution_status in {"succeeded", "failed"}:
            executed_runs += 1
            if execution_status == "succeeded":
                successful_executions += 1

        latency = trace["verification_result"].get("recovery_latency_seconds")
        if isinstance(latency, (int, float)):
            recovery_latencies.append(float(latency))

    return {
        "total_runs": total_runs,
        "recommendation_top1_rate": top1_hits / total_runs,
        "recommendation_top2_rate": top2_hits / total_runs,
        "approval_policy_correct_rate": approval_hits / total_runs,
        "execution_success_rate": successful_executions / executed_runs if executed_runs else 0.0,
        "verification_correct_rate": verification_hits / total_runs,
        "false_recovery_rate": false_recoveries / total_runs,
        "decision_trace_coverage_rate": trace_coverage_hits / total_runs,
        "median_recovery_latency_seconds": median(recovery_latencies) if recovery_latencies else None,
        "p95_recovery_latency_seconds": _percentile(recovery_latencies, 95),
    }


def render_metrics_markdown(metrics: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# HERALD Evaluation Metrics",
            "",
            f"- Total runs: {metrics['total_runs']}",
            f"- Recommendation top-1 rate: {_format_rate(metrics['recommendation_top1_rate'])}",
            f"- Recommendation top-2 rate: {_format_rate(metrics['recommendation_top2_rate'])}",
            f"- Approval policy correctness: {_format_rate(metrics['approval_policy_correct_rate'])}",
            f"- Execution success rate: {_format_rate(metrics['execution_success_rate'])}",
            f"- Verification correctness: {_format_rate(metrics['verification_correct_rate'])}",
            f"- False recovery rate: {_format_rate(metrics['false_recovery_rate'])}",
            f"- DecisionTrace coverage: {_format_rate(metrics['decision_trace_coverage_rate'])}",
            f"- Median recovery latency (s): {_format_latency(metrics['median_recovery_latency_seconds'])}",
            f"- P95 recovery latency (s): {_format_latency(metrics['p95_recovery_latency_seconds'])}",
        ]
    )


def _candidate_action_ids(result: dict[str, Any]) -> list[str]:
    action_ids: list[str] = []
    hitl_decision = result["hitl_decision"]
    if "candidate_actions" in hitl_decision:
        for action in hitl_decision["candidate_actions"]:
            action_id = action.get("action_id")
            if isinstance(action_id, str):
                action_ids.append(action_id)
        return action_ids

    for candidate in hitl_decision.get("candidate_options", []):
        legacy_action_hint = candidate.get("legacy_action_hint")
        if isinstance(legacy_action_hint, dict):
            action_id = legacy_action_hint.get("action_id")
            if isinstance(action_id, str):
                action_ids.append(action_id)
                continue
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str):
            action_ids.append(candidate_id)
    return action_ids


def _has_complete_decision_trace(trace: dict[str, Any]) -> bool:
    return all(field in trace for field in REQUIRED_DECISION_TRACE_FIELDS)


def _percentile(values: list[float], percentile_rank: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile_rank / 100) * (len(ordered) - 1))))
    return ordered[index]


def _format_rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_latency(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"
