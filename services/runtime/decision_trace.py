from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from schemas.decision_trace import DecisionTrace, TraceNodeName


def initialize_trace_provenance(trace: DecisionTrace) -> DecisionTrace:
    return replace(
        trace,
        node_runs_by_node=_clone_node_runs(trace.node_runs_by_node),
        latest_run_id_by_node=dict(trace.latest_run_id_by_node),
    )


def append_node_run(
    trace: DecisionTrace,
    *,
    node_name: TraceNodeName,
    status: str,
    summary: str,
    input_summary: dict[str, Any],
    output_summary: dict[str, Any],
    llm_explanation: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    artifact_refs: list[str] | None = None,
) -> DecisionTrace:
    normalized = initialize_trace_provenance(trace)
    node_runs_by_node = _clone_node_runs(normalized.node_runs_by_node)
    latest_run_id_by_node = dict(normalized.latest_run_id_by_node)

    node_runs = dict(node_runs_by_node.get(node_name, {}))
    sequence = _next_sequence(node_runs_by_node)
    attempt = _next_attempt(node_runs)
    run_id = f"{node_name}:{sequence:04d}"
    timestamp = _utc_now()
    run = {
        "run_id": run_id,
        "node_name": node_name,
        "sequence": sequence,
        "attempt": attempt,
        "started_at": started_at or timestamp,
        "finished_at": finished_at or timestamp,
        "status": status,
        "summary": summary,
        "input_summary": deepcopy(input_summary),
        "output_summary": deepcopy(output_summary),
        "artifact_refs": list(artifact_refs or []),
    }
    if llm_explanation is not None:
        run["llm_explanation"] = str(llm_explanation)
    node_runs[run_id] = run
    node_runs_by_node[node_name] = node_runs
    latest_run_id_by_node[node_name] = run_id

    return replace(
        normalized,
        node_runs_by_node=node_runs_by_node,
        latest_run_id_by_node=latest_run_id_by_node,
    )


def get_latest_node_run(trace: DecisionTrace, node_name: str) -> dict[str, Any] | None:
    latest_run_id = trace.latest_run_id_by_node.get(node_name)
    if latest_run_id is None:
        return None
    node_runs = trace.node_runs_by_node.get(node_name, {})
    latest_run = node_runs.get(latest_run_id)
    return deepcopy(latest_run) if latest_run is not None else None


def derive_trace_timeline(trace: DecisionTrace) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for node_runs in trace.node_runs_by_node.values():
        for run in node_runs.values():
            runs.append(
                {
                    "sequence": run["sequence"],
                    "run_id": run["run_id"],
                    "node_name": run["node_name"],
                    "attempt": run["attempt"],
                    "status": run["status"],
                    "summary": run["summary"],
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                }
            )
    return sorted(runs, key=lambda run: (int(run["sequence"]), str(run["run_id"])))


def _next_sequence(node_runs_by_node: dict[str, dict[str, dict[str, Any]]]) -> int:
    max_sequence = 0
    for node_runs in node_runs_by_node.values():
        for run in node_runs.values():
            max_sequence = max(max_sequence, int(run["sequence"]))
    return max_sequence + 1


def _next_attempt(node_runs: dict[str, dict[str, Any]]) -> int:
    max_attempt = 0
    for run in node_runs.values():
        max_attempt = max(max_attempt, int(run["attempt"]))
    return max_attempt + 1


def _clone_node_runs(
    node_runs_by_node: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        node_name: {run_id: deepcopy(run) for run_id, run in runs.items()}
        for node_name, runs in node_runs_by_node.items()
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
