from __future__ import annotations

import unittest

from schemas.execution import ExecutionDispatch, ExecutionResult


class ExecutionSchemaTest(unittest.TestCase):
    def test_accepts_valid_execution_dispatch(self) -> None:
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
            max_steps=5,
        )

        self.assertEqual(dispatch.worker_id, "worker-123")

    def test_rejects_unsupported_execution_action_type(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionDispatch(
                incident_id="incident-123",
                action_id="scale-cartservice",
                action_type="kubectl_shell",  # type: ignore[arg-type]
                parameters={"namespace": "default", "deployment": "cartservice"},
                worker_id="worker-123",
                requested_at="2026-03-27T03:00:00+00:00",
                allowed_tool_names=["get_deployment_context", "get_rollout_status"],
                max_steps=5,
            )

    def test_rejects_unsupported_tool_name(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionDispatch(
                incident_id="incident-123",
                action_id="rollout_undo_cartservice",
                action_type="rollout_undo_deployment",
                parameters={"namespace": "default", "deployment": "cartservice"},
                worker_id="worker-123",
                requested_at="2026-03-27T03:00:00+00:00",
                allowed_tool_names=["kubectl_shell", "rollout_undo_deployment"],  # type: ignore[list-item]
                max_steps=5,
            )

    def test_rejects_action_tool_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionDispatch(
                incident_id="incident-123",
                action_id="rollout_undo_cartservice",
                action_type="rollout_undo_deployment",
                parameters={"namespace": "default", "deployment": "cartservice"},
                worker_id="worker-123",
                requested_at="2026-03-27T03:00:00+00:00",
                allowed_tool_names=[
                    "get_deployment_context",
                    "get_rollout_status",
                    "rollout_restart_deployment",
                ],
                max_steps=5,
            )

    def test_accepts_delete_stresschaos_dispatch(self) -> None:
        dispatch = ExecutionDispatch(
            incident_id="incident-123",
            action_id="delete_frontend_cpu_stresschaos",
            action_type="delete_stresschaos",
            parameters={"namespace": "default", "name": "frontend-cpu-saturation"},
            worker_id="worker-123",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=[
                "get_stresschaos",
                "delete_stresschaos",
            ],
            max_steps=5,
        )

        self.assertEqual(dispatch.action_type, "delete_stresschaos")

    def test_accepts_scale_deployment_dispatch(self) -> None:
        dispatch = ExecutionDispatch(
            incident_id="incident-123",
            action_id="scale-frontend-2",
            action_type="scale_deployment",
            parameters={"namespace": "default", "deployment": "frontend", "replicas": 2},
            worker_id="worker-123",
            requested_at="2026-03-27T03:00:00+00:00",
            allowed_tool_names=[
                "get_deployment_context",
                "get_rollout_status",
                "scale_deployment",
            ],
            max_steps=5,
        )

        self.assertEqual(dispatch.action_type, "scale_deployment")

    def test_accepts_valid_execution_result(self) -> None:
        result = ExecutionResult(
            worker_id="worker-123",
            action_id="rollout_undo_cartservice",
            status="succeeded",
            started_at="2026-03-27T03:00:00+00:00",
            finished_at="2026-03-27T03:00:05+00:00",
            command=["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
            returncode=0,
            stdout="deployment.apps/cartservice rolled back\n",
            stderr="",
            summary="Executed the approved rollback and observed a clean command result.",
            tool_transcript=[{"step": 1, "tool_name": "rollout_undo_deployment"}],
        )

        self.assertEqual(result.status, "succeeded")

    def test_rejects_non_list_command(self) -> None:
        with self.assertRaises(TypeError):
            ExecutionResult(
                worker_id="worker-123",
                action_id="rollout_undo_cartservice",
                status="failed",
                started_at="2026-03-27T03:00:00+00:00",
                finished_at="2026-03-27T03:00:01+00:00",
                command="kubectl rollout undo",  # type: ignore[arg-type]
                returncode=1,
                stdout="",
                stderr="failed",
                summary="failed",
                tool_transcript=[],
            )

    def test_rejects_non_list_transcript(self) -> None:
        with self.assertRaises(TypeError):
            ExecutionResult(
                worker_id="worker-123",
                action_id="rollout_undo_cartservice",
                status="failed",
                started_at="2026-03-27T03:00:00+00:00",
                finished_at="2026-03-27T03:00:01+00:00",
                command=[],
                returncode=1,
                stdout="",
                stderr="failed",
                summary="failed",
                tool_transcript="not-a-list",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
