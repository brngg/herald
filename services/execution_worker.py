from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from schemas.execution import (
    ExecutionDispatch,
    ExecutionResult,
    ExecutionToolName,
    execution_dispatch_from_dict,
    execution_result_from_dict,
)
from services.gemini_execution_agent import (
    ExecutionAgentLLM,
    ExecutionTool,
    GeminiExecutionAgent,
    GeminiExecutionAgentLLM,
)
from services.kubernetes_client import KubernetesClient


WorkerCommandBuilder = Callable[[ExecutionDispatch], Sequence[str]]


@dataclass(slots=True)
class ExecutionWorkerHandle:
    worker_id: str
    dispatch: ExecutionDispatch
    command: list[str]
    process: subprocess.Popen[str]
    stderr_lines: list[str]
    stderr_thread: threading.Thread | None


@dataclass(slots=True)
class ExecutionWorkerClient:
    worker_command_builder: WorkerCommandBuilder | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    event_stream: TextIO | None = None

    def dispatch_execution_worker(self, dispatch: ExecutionDispatch) -> ExecutionWorkerHandle:
        command = list((self.worker_command_builder or _default_worker_command_builder)(dispatch))
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.cwd or _default_worker_cwd(),
            env=_merged_env(self.env),
        )
        stderr_lines: list[str] = []
        stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            stderr_thread = threading.Thread(
                target=_relay_worker_stderr,
                args=(process.stderr, stderr_lines, self.event_stream or sys.stderr),
                daemon=True,
            )
            stderr_thread.start()
        if process.stdin is None:
            process.kill()
            raise RuntimeError("Execution worker did not expose stdin.")
        process.stdin.write(json.dumps(asdict(dispatch)))
        process.stdin.close()
        return ExecutionWorkerHandle(
            worker_id=dispatch.worker_id,
            dispatch=dispatch,
            command=command,
            process=process,
            stderr_lines=stderr_lines,
            stderr_thread=stderr_thread,
        )

    def collect_execution_result(self, handle: ExecutionWorkerHandle) -> ExecutionResult:
        if handle.process.stdout is None:
            raise RuntimeError("Execution worker did not expose stdout/stderr.")
        stdout = handle.process.stdout.read()
        handle.process.stdout.close()
        handle.process.wait()
        if handle.stderr_thread is not None:
            handle.stderr_thread.join(timeout=1.0)
        stderr = "".join(handle.stderr_lines)
        if stdout.strip():
            try:
                payload = json.loads(stdout)
                if isinstance(payload, dict):
                    return execution_result_from_dict(payload)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
        return _build_failed_result(
            dispatch=handle.dispatch,
            summary="Execution worker did not return a valid JSON result.",
            stderr=stderr or stdout or "Execution worker did not return a valid JSON result.",
            command=handle.command,
            returncode=handle.process.returncode,
            tool_transcript=[],
        )


def execute_dispatch(
    dispatch: ExecutionDispatch,
    *,
    kubernetes_client: KubernetesClient | None = None,
    llm: ExecutionAgentLLM | None = None,
) -> ExecutionResult:
    started_at = _utc_now()
    kubernetes = kubernetes_client or KubernetesClient()
    agent = GeminiExecutionAgent(llm=llm or _default_execution_llm())
    tools = _build_execution_tools(kubernetes)
    _emit_worker_event(
        "worker_spawned",
        {
            "worker_id": dispatch.worker_id,
            "action_id": dispatch.action_id,
            "action_type": dispatch.action_type,
            "pid": os.getpid(),
        },
    )

    try:
        status, summary, command, returncode, stdout, stderr, tool_transcript = agent.run(
            dispatch=dispatch,
            tools=tools,
            event_logger=_emit_worker_event,
        )
    except Exception as exc:
        _emit_worker_event(
            "worker_exited",
            {
                "worker_id": dispatch.worker_id,
                "action_id": dispatch.action_id,
                "status": "failed",
            },
        )
        return _build_failed_result(
            dispatch=dispatch,
            summary=f"Gemini execution agent failed before completing the approved action: {exc}",
            stderr=str(exc),
            command=[],
            returncode=1,
            tool_transcript=[],
            started_at=started_at,
        )

    result = ExecutionResult(
        worker_id=dispatch.worker_id,
        action_id=dispatch.action_id,
        status=status,
        started_at=started_at,
        finished_at=_utc_now(),
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        summary=summary,
        tool_transcript=tool_transcript,
    )
    _emit_worker_event(
        "worker_exited",
        {
            "worker_id": dispatch.worker_id,
            "action_id": dispatch.action_id,
            "status": result.status,
        },
    )
    return result


def main() -> int:
    raw_payload = sys.stdin.read()
    if not raw_payload.strip():
        raise ValueError("Execution worker requires an ExecutionDispatch JSON payload on stdin.")

    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise TypeError("ExecutionDispatch payload must be an object.")

    try:
        dispatch = execution_dispatch_from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ExecutionDispatch payload: {exc}") from exc

    result = execute_dispatch(dispatch)
    print(json.dumps(asdict(result)))
    return 0 if result.status == "succeeded" else 1


def _build_execution_tools(kubernetes: KubernetesClient) -> dict[ExecutionToolName, ExecutionTool]:
    return {
        "get_deployment_context": ExecutionTool(
            name="get_deployment_context",
            description="Read the current Kubernetes Deployment JSON for the approved workload.",
            callable=kubernetes.get_deployment_context,
            mutation=False,
        ),
        "get_rollout_status": ExecutionTool(
            name="get_rollout_status",
            description="Read the current rollout status for the approved workload with a short timeout.",
            callable=kubernetes.get_rollout_status,
            mutation=False,
        ),
        "rollout_undo_deployment": ExecutionTool(
            name="rollout_undo_deployment",
            description="Undo the approved Deployment rollout.",
            callable=kubernetes.rollout_undo_deployment,
            mutation=True,
        ),
        "rollout_restart_deployment": ExecutionTool(
            name="rollout_restart_deployment",
            description="Restart the approved Deployment rollout.",
            callable=kubernetes.rollout_restart_deployment,
            mutation=True,
        ),
    }


def _default_execution_llm() -> GeminiExecutionAgentLLM:
    return GeminiExecutionAgentLLM(model=os.environ.get("GEMINI_EXECUTION_MODEL", "gemini-2.5-flash"))


def _default_worker_command_builder(_: ExecutionDispatch) -> Sequence[str]:
    return [sys.executable, "-m", "services.execution_worker"]


def _default_worker_cwd() -> str:
    return str(Path(__file__).resolve().parents[1])


def _merged_env(extra_env: Mapping[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return env


def _build_failed_result(
    *,
    dispatch: ExecutionDispatch,
    summary: str,
    stderr: str,
    command: Sequence[str],
    returncode: int,
    tool_transcript: list[dict[str, object]],
    started_at: str | None = None,
) -> ExecutionResult:
    timestamp = started_at or _utc_now()
    return ExecutionResult(
        worker_id=dispatch.worker_id,
        action_id=dispatch.action_id,
        status="failed",
        started_at=timestamp,
        finished_at=_utc_now(),
        command=list(command),
        returncode=returncode,
        stdout="",
        stderr=stderr,
        summary=summary,
        tool_transcript=tool_transcript,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _relay_worker_stderr(source: TextIO, sink_lines: list[str], sink: TextIO) -> None:
    try:
        for line in source:
            sink_lines.append(line)
            sink.write(line)
            sink.flush()
    finally:
        source.close()


def _emit_worker_event(event_type: str, payload: dict[str, object]) -> None:
    worker_id = str(payload.get("worker_id", "unknown-worker"))
    action_id = str(payload.get("action_id", "unknown-action"))
    message = _format_worker_event_message(event_type, payload)
    sys.stderr.write(f"[HERALD {worker_id}] {message} action_id={action_id}\n")
    sys.stderr.flush()


def _format_worker_event_message(event_type: str, payload: dict[str, object]) -> str:
    if event_type == "worker_spawned":
        return (
            "spawned Gemini execution agent "
            f"pid={payload.get('pid')} action_type={payload.get('action_type')}"
        )
    if event_type == "agent_started":
        allowed_tools = payload.get("allowed_tool_names", [])
        return f"agent started allowed_tools={allowed_tools} max_steps={payload.get('max_steps')}"
    if event_type == "agent_step_started":
        return f"step {payload.get('step')} deciding next tool"
    if event_type == "agent_decision":
        decision_type = payload.get("decision_type")
        tool_name = payload.get("tool_name")
        if decision_type == "finish":
            return f"step {payload.get('step')} returned finish"
        return f"step {payload.get('step')} requested tool={tool_name}"
    if event_type == "tool_call_started":
        return f"step {payload.get('step')} running tool={payload.get('tool_name')}"
    if event_type == "tool_call_finished":
        return (
            f"step {payload.get('step')} completed tool={payload.get('tool_name')} "
            f"status={payload.get('status')}"
        )
    if event_type == "tool_call_failed":
        return f"step {payload.get('step')} failed tool={payload.get('tool_name')}"
    if event_type == "agent_finished":
        return f"agent finished status={payload.get('status')}"
    if event_type == "worker_exited":
        return f"worker exited status={payload.get('status')}"
    return f"{event_type} payload={payload}"


if __name__ == "__main__":
    raise SystemExit(main())
