from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

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


@dataclass(slots=True)
class ExecutionWorkerClient:
    worker_command_builder: WorkerCommandBuilder | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None

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
        )

    def collect_execution_result(self, handle: ExecutionWorkerHandle) -> ExecutionResult:
        if handle.process.stdout is None or handle.process.stderr is None:
            raise RuntimeError("Execution worker did not expose stdout/stderr.")
        stdout = handle.process.stdout.read()
        stderr = handle.process.stderr.read()
        handle.process.stdout.close()
        handle.process.stderr.close()
        handle.process.wait()
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

    try:
        status, summary, command, returncode, stdout, stderr, tool_transcript = agent.run(
            dispatch=dispatch,
            tools=tools,
        )
    except Exception as exc:
        return _build_failed_result(
            dispatch=dispatch,
            summary=f"Gemini execution agent failed before completing the approved action: {exc}",
            stderr=str(exc),
            command=[],
            returncode=1,
            tool_transcript=[],
            started_at=started_at,
        )

    return ExecutionResult(
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


if __name__ == "__main__":
    raise SystemExit(main())
