from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
import unittest

from schemas.execution import ExecutionDispatch
from services.infra.kubernetes.execution_worker import (
    ExecutionWorkerClient,
    _default_worker_cwd,
    _merged_env,
    execute_dispatch,
)
from services.gemini_execution_agent import ExecutionAgentDecision, ExecutionAgentLLM
from services.infra.kubernetes.client import KubernetesClient


class _FakeExecutionAgentLLM(ExecutionAgentLLM):
    def __init__(
        self,
        decisions: list[ExecutionAgentDecision] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._decisions = iter(decisions or [])
        self._error = error

    def decide_next_step(
        self,
        *,
        dispatch: ExecutionDispatch,
        tool_transcript: list[dict[str, object]],
    ) -> ExecutionAgentDecision:
        if self._error is not None:
            raise self._error
        return next(self._decisions)


class ExecutionWorkerTest(unittest.TestCase):
    def test_worker_subprocess_defaults_include_repo_root(self) -> None:
        cwd = _default_worker_cwd()
        env = _merged_env(None)

        self.assertTrue(cwd.endswith("/Users/bcheng/Projects/herald"))
        pythonpath = env.get("PYTHONPATH", "")
        self.assertTrue(pythonpath)
        self.assertEqual(pythonpath.split(os.pathsep)[0], cwd)

    def test_execute_dispatch_runs_gemini_agent_loop_for_bounded_action(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="deployment.apps/cartservice rolled back\n",
                stderr="",
            )

        dispatch = ExecutionDispatch(
            incident_id="incident-123",
            action_id="rollout_undo_cartservice",
            action_type="rollout_undo_deployment",
            parameters={"namespace": "default", "deployment": "cartservice"},
            worker_id="worker-123",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=[
                "get_deployment_context",
                "get_rollout_status",
                "rollout_undo_deployment",
            ],
            max_steps=4,
        )
        llm = _FakeExecutionAgentLLM(
            [
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="get_deployment_context",
                    arguments={"namespace": "default", "deployment": "cartservice"},
                    status="failed",
                    summary="Inspect the deployment before acting.",
                ),
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="rollout_undo_deployment",
                    arguments={"namespace": "default", "deployment": "cartservice"},
                    status="failed",
                    summary="Execute the approved rollback.",
                ),
                ExecutionAgentDecision(
                    decision_type="finish",
                    tool_name="",
                    arguments={},
                    status="succeeded",
                    summary="Executed the approved rollback successfully.",
                ),
            ]
        )

        result = execute_dispatch(
            dispatch,
            kubernetes_client=KubernetesClient(runner=runner),
            llm=llm,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.summary, "Executed the approved rollback successfully.")
        self.assertEqual(
            result.command,
            ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
        )
        self.assertEqual(result.tool_transcript[0]["tool_name"], "get_deployment_context")
        self.assertEqual(result.tool_transcript[1]["tool_name"], "rollout_undo_deployment")
        self.assertEqual(
            commands,
            [
                ["kubectl", "get", "deployment", "cartservice", "-n", "default", "-o", "json"],
                ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
            ],
        )

    def test_dispatch_and_collect_use_subprocess_worker_contract(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import sys

            dispatch = json.load(sys.stdin)
            sys.stderr.write("[HERALD %s] spawned Gemini execution agent pid=999 action_type=%s action_id=%s\\n" % (
                dispatch["worker_id"],
                dispatch["action_type"],
                dispatch["action_id"],
            ))
            sys.stderr.flush()
            result = {
                "worker_id": dispatch["worker_id"],
                "action_id": dispatch["action_id"],
                "status": "succeeded",
                "started_at": "2026-03-27T03:00:00+00:00",
                "finished_at": "2026-03-27T03:00:05+00:00",
                "command": ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "summary": "Agent executed the approved rollback.",
                "tool_transcript": [{"step": 1, "tool_name": "rollout_undo_deployment"}],
            }
            print(json.dumps(result))
            """
        ).strip()

        event_stream = io.StringIO()
        client = ExecutionWorkerClient(
            worker_command_builder=lambda _: [sys.executable, "-c", script],
            event_stream=event_stream,
        )
        dispatch = ExecutionDispatch(
            incident_id="incident-123",
            action_id="rollout_undo_cartservice",
            action_type="rollout_undo_deployment",
            parameters={"namespace": "default", "deployment": "cartservice"},
            worker_id="worker-123",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=[
                "get_deployment_context",
                "get_rollout_status",
                "rollout_undo_deployment",
            ],
            max_steps=4,
        )

        handle = client.dispatch_execution_worker(dispatch)
        result = client.collect_execution_result(handle)

        self.assertEqual(handle.worker_id, "worker-123")
        self.assertEqual(result.worker_id, "worker-123")
        self.assertEqual(result.action_id, "rollout_undo_cartservice")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.summary, "Agent executed the approved rollback.")
        self.assertIn("spawned Gemini execution agent", event_stream.getvalue())

    def test_execute_dispatch_surfaces_model_failure_as_structured_result(self) -> None:
        dispatch = ExecutionDispatch(
            incident_id="incident-123",
            action_id="rollout_undo_cartservice",
            action_type="rollout_undo_deployment",
            parameters={"namespace": "default", "deployment": "cartservice"},
            worker_id="worker-123",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=[
                "get_deployment_context",
                "get_rollout_status",
                "rollout_undo_deployment",
            ],
            max_steps=4,
        )

        result = execute_dispatch(
            dispatch,
            kubernetes_client=KubernetesClient(),
            llm=_FakeExecutionAgentLLM(error=ValueError("malformed Gemini output")),
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("malformed Gemini output", result.summary)
        self.assertEqual(result.tool_transcript, [])

    def test_execute_dispatch_supports_delete_stresschaos(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "get", "stresschaos"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='{"metadata":{"name":"frontend-cpu-saturation"}}',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
                stderr="",
            )

        dispatch = ExecutionDispatch(
            incident_id="incident-456",
            action_id="delete_frontend_cpu_stresschaos",
            action_type="delete_stresschaos",
            parameters={"namespace": "default", "name": "frontend-cpu-saturation"},
            worker_id="worker-456",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=["get_stresschaos", "delete_stresschaos"],
            max_steps=3,
        )
        llm = _FakeExecutionAgentLLM(
            [
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="get_stresschaos",
                    arguments={"namespace": "default", "name": "frontend-cpu-saturation"},
                    status="failed",
                    summary="Inspect the active chaos object before deletion.",
                ),
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="delete_stresschaos",
                    arguments={"namespace": "default", "name": "frontend-cpu-saturation"},
                    status="failed",
                    summary="Delete the approved CPU saturation chaos object.",
                ),
                ExecutionAgentDecision(
                    decision_type="finish",
                    tool_name="",
                    arguments={},
                    status="succeeded",
                    summary="Deleted the active CPU saturation chaos object successfully.",
                ),
            ]
        )

        result = execute_dispatch(
            dispatch,
            kubernetes_client=KubernetesClient(runner=runner),
            llm=llm,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.tool_transcript[0]["tool_name"], "get_stresschaos")
        self.assertEqual(result.tool_transcript[1]["tool_name"], "delete_stresschaos")
        self.assertEqual(
            commands,
            [
                ["kubectl", "get", "stresschaos", "frontend-cpu-saturation", "-n", "default", "-o", "json"],
                ["kubectl", "delete", "stresschaos", "frontend-cpu-saturation", "-n", "default"],
            ],
        )

    def test_execute_dispatch_supports_scale_deployment(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "get", "deployment"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='{"metadata":{"name":"frontend"}}',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='deployment.apps/frontend scaled\n',
                stderr="",
            )

        dispatch = ExecutionDispatch(
            incident_id="incident-789",
            action_id="scale_frontend_2",
            action_type="scale_deployment",
            parameters={"namespace": "default", "deployment": "frontend", "replicas": 2},
            worker_id="worker-789",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=["get_deployment_context", "get_rollout_status", "scale_deployment"],
            max_steps=3,
        )
        llm = _FakeExecutionAgentLLM(
            [
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="get_deployment_context",
                    arguments={"namespace": "default", "deployment": "frontend"},
                    status="failed",
                    summary="Inspect the deployment before scaling.",
                ),
                ExecutionAgentDecision(
                    decision_type="tool_call",
                    tool_name="scale_deployment",
                    arguments={"replicas": 99},
                    status="failed",
                    summary="Scale the approved deployment to the bounded replica target.",
                ),
                ExecutionAgentDecision(
                    decision_type="finish",
                    tool_name="",
                    arguments={},
                    status="succeeded",
                    summary="Scaled the approved deployment successfully.",
                ),
            ]
        )

        result = execute_dispatch(
            dispatch,
            kubernetes_client=KubernetesClient(runner=runner),
            llm=llm,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.tool_transcript[1]["tool_name"], "scale_deployment")
        self.assertEqual(
            commands,
            [
                ["kubectl", "get", "deployment", "frontend", "-n", "default", "-o", "json"],
                ["kubectl", "scale", "deployment/frontend", "-n", "default", "--replicas=2"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
