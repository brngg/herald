from __future__ import annotations

import argparse
import json
import os
import socket
from dataclasses import asdict, is_dataclass
from pathlib import Path
from time import sleep
from typing import Any
from uuid import uuid4

from services.alert_inbox import (
    InboxArtifactRecord,
    claim_inbox_record,
    list_actionable_inbox_records,
    load_inbox_record,
    load_workflow_artifact,
    save_workflow_artifact,
    update_inbox_record,
)
from services.gemini_fixer_llm import GeminiFixerLLM
from services.gemini_critic_llm import GeminiCriticLLM
from services.gemini_judge_llm import GeminiJudgeLLM
from services.gemini_reasoner_llm import GeminiReasonerLLM
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import PrometheusClient
from services.execution_worker import ExecutionWorkerClient
from workflows.recovery_workflow import (
    EngineMode,
    VALID_ENGINE_MODES,
    _continue_with_interactive_hitl,
    run_recovery_from_payload,
)


def ignore_inbox_artifact(artifact_dir: str) -> InboxArtifactRecord:
    return update_inbox_record(
        artifact_dir,
        status="ignored",
        gate0_decision="ignore",
        claimed_by=None,
        claimed_at=None,
        completed_at=_utc_now(),
        expected_statuses=("pending_investigation",),
    )


def start_investigation_for_artifact(
    artifact_dir: str,
    *,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    prometheus_client: PrometheusClient | None = None,
    kubernetes_client: KubernetesClient | None = None,
) -> tuple[InboxArtifactRecord, dict[str, Any]]:
    record = update_inbox_record(
        artifact_dir,
        status="planning_started",
        gate0_decision="investigate",
        expected_statuses=("pending_investigation",),
    )
    planning_result = run_recovery_from_payload(
        record.raw_payload,
        engine_mode=engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        reasoner_llm=reasoner_llm,
        critic_llm=critic_llm,
        prometheus_client=prometheus_client,
        kubernetes_client=kubernetes_client,
    )
    first_pass_path = save_workflow_artifact(
        artifact_dir,
        file_name="first-pass.json",
        payload=planning_result,
    )
    status = (
        "pending_execution_approval"
        if planning_result["decision_trace"].final_state == "pending_approval"
        else "completed"
    )
    updated = update_inbox_record(
        artifact_dir,
        status=status,
        gate0_decision="investigate",
        first_pass_artifact=str(first_pass_path),
        claimed_by=None,
        claimed_at=None,
        completed_at=_utc_now() if status == "completed" else None,
        expected_statuses=("planning_started",),
    )
    return updated, planning_result


def continue_execution_approval_for_artifact(
    artifact_dir: str,
    *,
    planning_result: dict[str, Any] | None = None,
    prometheus_client: PrometheusClient | None = None,
    kubernetes_client: KubernetesClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
    input_fn: Any = input,
    output_fn: Any = print,
) -> tuple[InboxArtifactRecord, dict[str, Any]]:
    record = load_inbox_record(artifact_dir)
    if planning_result is None:
        if record.first_pass_artifact is None:
            raise ValueError("Inbox artifact does not include a saved first-pass workflow artifact.")
        planning_result = load_workflow_artifact(record.first_pass_artifact)

    final_result = _continue_with_interactive_hitl(
        payload=record.raw_payload,
        planning_result=planning_result,
        prometheus_client=prometheus_client or PrometheusClient(),
        kubernetes_client=kubernetes_client,
        execution_worker_client=execution_worker_client,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    final_path = save_workflow_artifact(
        artifact_dir,
        file_name="final-result.json",
        payload=final_result,
    )
    updated = update_inbox_record(
        artifact_dir,
        status="completed",
        gate0_decision="investigate",
        first_pass_artifact=record.first_pass_artifact or str(Path(record.artifact_dir) / "first-pass.json"),
        final_result_artifact=str(final_path),
        claimed_by=None,
        claimed_at=None,
        completed_at=_utc_now(),
        expected_statuses=("pending_execution_approval",),
    )
    return updated, final_result


def run_terminal_inbox_flow(
    *,
    inbox_root: str | None = None,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    prometheus_client: PrometheusClient | None = None,
    kubernetes_client: KubernetesClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
    claimer_id: str | None = None,
    claim_timeout_seconds: float = 300.0,
    input_fn: Any = input,
    output_fn: Any = print,
) -> dict[str, Any] | None:
    claimer = claimer_id or _default_claimer_id()
    actionable = list_actionable_inbox_records(
        inbox_root=inbox_root,
        claimer_id=claimer,
        reclaim_after_seconds=claim_timeout_seconds,
    )
    if not actionable:
        output_fn("No actionable alerts are waiting in the terminal inbox.", flush=True)
        return None

    output_fn("Actionable alerts:", flush=True)
    _print_record_list(actionable, output_fn=output_fn)

    selected_index = _prompt_number(
        prompt="Select an alert by number: ",
        maximum=len(actionable),
        input_fn=input_fn,
        output_fn=output_fn,
    )
    selected = actionable[selected_index - 1]
    claimed = claim_inbox_record(
        selected.artifact_dir,
        claimer_id=claimer,
        reclaim_after_seconds=claim_timeout_seconds,
    )
    if claimed is None:
        output_fn("That alert is no longer actionable. Try running the inbox command again.", flush=True)
        return None
    return _handle_claimed_record(
        claimed,
        engine_mode=engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        reasoner_llm=reasoner_llm,
        critic_llm=critic_llm,
        prometheus_client=prometheus_client,
        kubernetes_client=kubernetes_client,
        execution_worker_client=execution_worker_client,
        input_fn=input_fn,
        output_fn=output_fn,
    )


def run_terminal_inbox_watch(
    *,
    inbox_root: str | None = None,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    prometheus_client: PrometheusClient | None = None,
    kubernetes_client: KubernetesClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
    claimer_id: str | None = None,
    claim_timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 1.0,
    max_processed: int | None = None,
    max_polls_without_work: int | None = None,
    input_fn: Any = input,
    output_fn: Any = print,
    sleep_fn: Any = sleep,
) -> list[dict[str, Any]]:
    claimer = claimer_id or _default_claimer_id()
    output_fn(
        f"Watching HERALD terminal inbox as {claimer}. Press Ctrl-C to stop.",
        flush=True,
    )
    processed: list[dict[str, Any]] = []
    idle_polls = 0

    while True:
        claimed = _claim_next_actionable_record(
            inbox_root=inbox_root,
            claimer_id=claimer,
            claim_timeout_seconds=claim_timeout_seconds,
        )
        if claimed is None:
            idle_polls += 1
            if max_polls_without_work is not None and idle_polls >= max_polls_without_work:
                return processed
            sleep_fn(poll_interval_seconds)
            continue

        idle_polls = 0
        try:
            processed.append(
                _handle_claimed_record(
                    claimed,
                    engine_mode=engine_mode,
                    fixer_llm=fixer_llm,
                    judge_llm=judge_llm,
                    reasoner_llm=reasoner_llm,
                    critic_llm=critic_llm,
                    prometheus_client=prometheus_client,
                    kubernetes_client=kubernetes_client,
                    execution_worker_client=execution_worker_client,
                    input_fn=input_fn,
                    output_fn=output_fn,
                )
            )
            output_fn("Watcher returning to idle; waiting for new alerts...", flush=True)
        except Exception as exc:
            output_fn(
                f"Watcher handled alert {claimed.incident_id} with an error and will keep running: {exc}",
                flush=True,
            )
            sleep_fn(poll_interval_seconds)
            continue
        if max_processed is not None and len(processed) >= max_processed:
            return processed


def _handle_claimed_record(
    record: InboxArtifactRecord,
    *,
    engine_mode: EngineMode | str = "v1",
    fixer_llm: Any = None,
    judge_llm: Any = None,
    reasoner_llm: Any = None,
    critic_llm: Any = None,
    prometheus_client: PrometheusClient | None = None,
    kubernetes_client: KubernetesClient | None = None,
    execution_worker_client: ExecutionWorkerClient | None = None,
    input_fn: Any = input,
    output_fn: Any = print,
) -> dict[str, Any]:
    output_fn(_record_summary(record), flush=True)
    if record.status == "pending_execution_approval":
        output_fn("Planning complete. Enter 1 to approve execution or 2 to reject.", flush=True)
        final_record, final_result = continue_execution_approval_for_artifact(
            record.artifact_dir,
            prometheus_client=prometheus_client,
            kubernetes_client=kubernetes_client,
            execution_worker_client=execution_worker_client,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        return {
            "gate0_decision": "investigate",
            "artifact": final_record,
            "planning_result": (
                load_workflow_artifact(final_record.first_pass_artifact)
                if final_record.first_pass_artifact is not None
                else None
            ),
            "final_result": final_result,
        }

    output_fn("New alert received. Enter 1 to investigate or 2 to ignore.", flush=True)
    choice = _prompt_exact({"1", "2"}, input_fn=input_fn, output_fn=output_fn)

    if choice == "2":
        updated = ignore_inbox_artifact(record.artifact_dir)
        return {"gate0_decision": "ignore", "artifact": updated}

    updated, planning_result = start_investigation_for_artifact(
        record.artifact_dir,
        engine_mode=engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        reasoner_llm=reasoner_llm,
        critic_llm=critic_llm,
        prometheus_client=prometheus_client,
        kubernetes_client=kubernetes_client,
    )
    result: dict[str, Any] = {
        "gate0_decision": "investigate",
        "artifact": updated,
        "planning_result": planning_result,
    }
    if updated.status == "pending_execution_approval":
        output_fn("Planning complete. Enter 1 to approve execution or 2 to reject.", flush=True)
        final_record, final_result = continue_execution_approval_for_artifact(
            record.artifact_dir,
            planning_result=planning_result,
            prometheus_client=prometheus_client,
            kubernetes_client=kubernetes_client,
            execution_worker_client=execution_worker_client,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        result["artifact"] = final_record
        result["final_result"] = final_result
    return result


def _claim_next_actionable_record(
    *,
    inbox_root: str | None = None,
    claimer_id: str,
    claim_timeout_seconds: float,
) -> InboxArtifactRecord | None:
    for record in list_actionable_inbox_records(
        inbox_root=inbox_root,
        claimer_id=claimer_id,
        reclaim_after_seconds=claim_timeout_seconds,
    ):
        claimed = claim_inbox_record(
            record.artifact_dir,
            claimer_id=claimer_id,
            reclaim_after_seconds=claim_timeout_seconds,
        )
        if claimed is not None:
            return claimed
    return None


def _print_record_list(records: list[InboxArtifactRecord], *, output_fn: Any) -> None:
    for index, record in enumerate(records, start=1):
        metadata = record.incident_metadata
        output_fn(
            (
                f"{index}. [{record.status}] {record.incident_id} "
                f"({record.incident_class}) ns={metadata.get('namespace')} "
                f"summary={metadata.get('summary')!r}"
            ),
            flush=True,
        )


def _record_summary(record: InboxArtifactRecord) -> str:
    metadata = record.incident_metadata
    return (
        f"Alert {record.incident_id} ({record.incident_class}) "
        f"ns={metadata.get('namespace')} summary={metadata.get('summary')!r}"
    )


def _default_claimer_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


def _prompt_number(
    *,
    prompt: str,
    maximum: int,
    input_fn: Any,
    output_fn: Any,
) -> int:
    while True:
        response = str(input_fn(prompt)).strip()
        if response.isdigit():
            value = int(response)
            if 1 <= value <= maximum:
                return value
        output_fn(f"Enter a number between 1 and {maximum}.", flush=True)


def _prompt_exact(
    valid_choices: set[str],
    *,
    input_fn: Any,
    output_fn: Any,
) -> str:
    while True:
        response = str(input_fn("> ")).strip()
        if response in valid_choices:
            return response
        output_fn(f"Enter one of: {', '.join(sorted(valid_choices))}.", flush=True)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the HERALD terminal inbox flow for Gate 0 investigation."
    )
    parser.add_argument("--inbox-root")
    parser.add_argument("--prometheus-base-url")
    parser.add_argument(
        "--fixer-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--judge-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--reasoner-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument(
        "--critic-provider",
        choices=("heuristic", "gemini"),
        default="heuristic",
    )
    parser.add_argument("--fixer-model", default="gemini-2.5-flash")
    parser.add_argument("--judge-model", default="gemini-2.5-flash")
    parser.add_argument("--reasoner-model", default="gemini-2.5-flash")
    parser.add_argument("--critic-model", default="gemini-2.5-flash")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--claim-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--engine-mode",
        choices=VALID_ENGINE_MODES,
        default="v1",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    fixer_llm = GeminiFixerLLM(model=args.fixer_model) if args.fixer_provider == "gemini" else None
    judge_llm = GeminiJudgeLLM(model=args.judge_model) if args.judge_provider == "gemini" else None
    reasoner_llm = (
        GeminiReasonerLLM(model=args.reasoner_model)
        if args.reasoner_provider == "gemini"
        else None
    )
    critic_llm = GeminiCriticLLM(model=args.critic_model) if args.critic_provider == "gemini" else None
    prometheus_client = PrometheusClient(base_url=args.prometheus_base_url)
    if args.watch:
        try:
            run_terminal_inbox_watch(
                inbox_root=args.inbox_root,
                engine_mode=args.engine_mode,
                fixer_llm=fixer_llm,
                judge_llm=judge_llm,
                reasoner_llm=reasoner_llm,
                critic_llm=critic_llm,
                prometheus_client=prometheus_client,
                poll_interval_seconds=args.poll_interval_seconds,
                claim_timeout_seconds=args.claim_timeout_seconds,
            )
        except KeyboardInterrupt:
            return 130
        return 0

    result = run_terminal_inbox_flow(
        inbox_root=args.inbox_root,
        engine_mode=args.engine_mode,
        fixer_llm=fixer_llm,
        judge_llm=judge_llm,
        reasoner_llm=reasoner_llm,
        critic_llm=critic_llm,
        prometheus_client=prometheus_client,
        claim_timeout_seconds=args.claim_timeout_seconds,
    )
    print(json.dumps(_to_jsonable(result), default=str, indent=2))
    return 0


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
