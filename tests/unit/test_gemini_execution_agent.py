from __future__ import annotations

import unittest

from schemas.execution import ExecutionDispatch
from services.gemini_execution_agent import (
    ExecutionAgentDecision,
    ExecutionAgentLLM,
    ExecutionTool,
    GeminiExecutionAgent,
)


class _FakeExecutionAgentLLM(ExecutionAgentLLM):
    def __init__(self, decisions: list[ExecutionAgentDecision] | None = None, error: Exception | None = None) -> None:
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


def _dispatch() -> ExecutionDispatch:
    return ExecutionDispatch(
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


def _tools() -> dict[str, ExecutionTool]:
    return {
        "get_deployment_context": ExecutionTool(
            name="get_deployment_context",
            description="describe deployment",
            callable=lambda **_: {
                "status": "succeeded",
                "returncode": 0,
                "stdout": '{"metadata":{"name":"cartservice"}}',
                "stderr": "",
                "command": ["kubectl", "get", "deployment", "cartservice", "-o", "json"],
            },
            mutation=False,
        ),
        "get_rollout_status": ExecutionTool(
            name="get_rollout_status",
            description="rollout status",
            callable=lambda **_: {
                "status": "succeeded",
                "returncode": 0,
                "stdout": 'deployment "cartservice" successfully rolled out',
                "stderr": "",
                "command": ["kubectl", "rollout", "status", "deployment/cartservice"],
            },
            mutation=False,
        ),
        "rollout_undo_deployment": ExecutionTool(
            name="rollout_undo_deployment",
            description="undo rollout",
            callable=lambda **_: {
                "status": "succeeded",
                "returncode": 0,
                "stdout": "deployment.apps/cartservice rolled back\n",
                "stderr": "",
                "command": ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
            },
            mutation=True,
        ),
    }


class GeminiExecutionAgentTest(unittest.TestCase):
    def test_agent_completes_valid_tool_sequence(self) -> None:
        agent = GeminiExecutionAgent(
            llm=_FakeExecutionAgentLLM(
                [
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_deployment_context",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect before acting.",
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
                        summary="Rollback completed successfully.",
                    ),
                ]
            )
        )

        status, summary, command, returncode, stdout, stderr, transcript = agent.run(
            dispatch=_dispatch(),
            tools=_tools(),
        )

        self.assertEqual(status, "succeeded")
        self.assertEqual(summary, "Rollback completed successfully.")
        self.assertEqual(returncode, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(command[0:3], ["kubectl", "rollout", "undo"])
        self.assertEqual(len(transcript), 2)

    def test_agent_rejects_disallowed_tool(self) -> None:
        agent = GeminiExecutionAgent(
            llm=_FakeExecutionAgentLLM(
                [
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="rollout_restart_deployment",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Try restarting instead.",
                    )
                ]
            )
        )

        status, summary, *_ = agent.run(dispatch=_dispatch(), tools=_tools())

        self.assertEqual(status, "failed")
        self.assertIn("disallowed tool", summary)

    def test_agent_fails_when_model_does_not_finish_within_max_steps(self) -> None:
        agent = GeminiExecutionAgent(
            llm=_FakeExecutionAgentLLM(
                [
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_deployment_context",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect step 1.",
                    ),
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_rollout_status",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect step 2.",
                    ),
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_deployment_context",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect step 3.",
                    ),
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_rollout_status",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect step 4.",
                    ),
                ]
            )
        )

        status, summary, *_ = agent.run(dispatch=_dispatch(), tools=_tools())

        self.assertEqual(status, "failed")
        self.assertIn("exceeded max_steps", summary)

    def test_agent_emits_lifecycle_events_without_exposing_reasoning(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        agent = GeminiExecutionAgent(
            llm=_FakeExecutionAgentLLM(
                [
                    ExecutionAgentDecision(
                        decision_type="tool_call",
                        tool_name="get_deployment_context",
                        arguments={"namespace": "default", "deployment": "cartservice"},
                        status="failed",
                        summary="Inspect before acting.",
                    ),
                    ExecutionAgentDecision(
                        decision_type="finish",
                        tool_name="",
                        arguments={},
                        status="failed",
                        summary="Stopping after inspection.",
                    ),
                ]
            )
        )

        agent.run(
            dispatch=_dispatch(),
            tools=_tools(),
            event_logger=lambda event_type, payload: events.append((event_type, dict(payload))),
        )

        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "agent_started",
                "agent_step_started",
                "agent_decision",
                "tool_call_started",
                "tool_call_finished",
                "agent_step_started",
                "agent_decision",
                "agent_finished",
            ],
        )
        self.assertEqual(events[2][1]["tool_name"], "get_deployment_context")
        self.assertNotIn("summary", events[2][1])

    def test_agent_failure_surfaces_model_error(self) -> None:
        agent = GeminiExecutionAgent(llm=_FakeExecutionAgentLLM(error=ValueError("malformed output")))

        with self.assertRaisesRegex(ValueError, "malformed output"):
            agent.llm.decide_next_step(dispatch=_dispatch(), tool_transcript=[])


if __name__ == "__main__":
    unittest.main()
