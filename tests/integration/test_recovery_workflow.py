from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest

from services.execution_worker import ExecutionWorkerClient
from services.kubernetes_client import KubernetesClient
from services.judge_llm import JudgeLLMResult
from services.prometheus_client import PrometheusClient
from workflows.recovery_workflow import run_crashloop_recovery_from_payload


def _crashloop_payload() -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldCartserviceCrashLoopBackOff",
                    "incident_class": "crashloop",
                    "namespace": "default",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                    "container": "server",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "cartservice is in CrashLoopBackOff",
                    "description": "Pod cartservice is crash looping.",
                },
                "startsAt": "2026-03-23T20:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": "crashloop123",
            }
        ],
        "groupLabels": {
            "alertname": "HeraldCartserviceCrashLoopBackOff",
            "incident_class": "crashloop",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldCartserviceCrashLoopBackOff",
            "incident_class": "crashloop",
            "namespace": "default",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "cartservice is in CrashLoopBackOff",
        },
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldCartserviceCrashLoopBackOff"}',
        "truncatedAlerts": 0,
    }


def _worker_client(
    *,
    status: str = "succeeded",
    returncode: int = 0,
    stdout: str = "deployment.apps/cartservice rolled back\n",
    stderr: str = "",
) -> ExecutionWorkerClient:
    script = textwrap.dedent(
        f"""
        import json
        import sys

        dispatch = json.load(sys.stdin)
        result = {{
            "worker_id": dispatch["worker_id"],
            "action_id": dispatch["action_id"],
            "status": {status!r},
            "started_at": "2026-03-27T03:00:00+00:00",
            "finished_at": "2026-03-27T03:00:05+00:00",
            "command": ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
            "returncode": {returncode},
            "stdout": {stdout!r},
            "stderr": {stderr!r},
            "summary": "Agent completed the approved remediation." if {status!r} == "succeeded" else "Agent failed to complete the approved remediation.",
            "tool_transcript": [{{"step": 1, "tool_name": dispatch["action_type"]}}],
        }}
        print(json.dumps(result))
        """
    ).strip()
    return ExecutionWorkerClient(worker_command_builder=lambda _: [sys.executable, "-c", script])


class RecoveryWorkflowIntegrationTest(unittest.TestCase):
    def test_workflow_requires_explicit_approval_before_execution(self) -> None:
        result = run_crashloop_recovery_from_payload(_crashloop_payload())

        hitl = result["hitl_decision"]
        trace = result["decision_trace"]

        self.assertTrue(hitl["requires_approval"])
        self.assertEqual(hitl["routing_decision"], "request_approval_single_action")
        self.assertEqual(trace.human_approval, "n/a")
        self.assertEqual(trace.final_state, "pending_approval")
        self.assertEqual(trace.execution_result, {})
        self.assertEqual(trace.verification_result, {})

    def test_workflow_executes_approved_restart_and_marks_recovered(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        crashloop_queries = iter([1.0, 0.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(query_runner=query_runner)
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client()

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]
        hitl = result["hitl_decision"]

        self.assertEqual(hitl["recommended_action"].action_id, "rollout_undo_cartservice")
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["action_type"], "rollout_undo_deployment")
        self.assertEqual(trace.execution_result["dispatch_status"], "succeeded")
        self.assertTrue(trace.execution_result["worker_id"].startswith("worker-"))
        self.assertEqual(
            trace.execution_result["worker_result"]["action_id"],
            "rollout_undo_cartservice",
        )
        self.assertEqual(
            trace.execution_result["summary"],
            "Agent completed the approved remediation.",
        )
        self.assertEqual(
            trace.execution_result["tool_transcript"][0]["tool_name"],
            "rollout_undo_deployment",
        )
        self.assertEqual(trace.execution_result["rollout_status"]["status"], "succeeded")
        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.verification_result["post_check"]["attempts"], 6)
        self.assertEqual(trace.final_state, "recovered")
        self.assertFalse(trace.rollback_triggered)
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"]],
        )

    def test_workflow_retries_pre_check_before_skipping_execution(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        crashloop_queries = iter([0.0, 1.0, 0.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(
            query_runner=query_runner,
            pre_check_retry_attempts=2,
            pre_check_retry_sleep_seconds=0.0,
            sleep_fn=lambda _: None,
        )
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client()

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["pre_check"]["attempts"], 2)
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertTrue(trace.execution_result["worker_id"].startswith("worker-"))
        self.assertEqual(trace.execution_result["tool_transcript"][0]["tool_name"], "rollout_undo_deployment")
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"]],
        )

    def test_workflow_retries_post_check_until_ready_signal_appears(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        crashloop_queries = iter([1.0, 0.0, 0.0])
        ready_queries = iter([0.0, 1.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=2,
            post_check_retry_sleep_seconds=0.0,
            sleep_fn=lambda _: None,
        )
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client()

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.verification_result["post_check"]["attempts"], 2)
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"]],
        )

    def test_workflow_surfaces_worker_failure_cleanly(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        prometheus = PrometheusClient(
            query_runner=lambda query: 1.0 if "kube_pod_container_status_waiting_reason" in query else 0.0,
            post_check_retry_attempts=1,
            post_check_retry_sleep_seconds=0.0,
            sleep_fn=lambda _: None,
        )
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client(
            status="failed",
            returncode=1,
            stdout="",
            stderr="worker failed",
        )

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["status"], "failed")
        self.assertEqual(trace.execution_result["worker_result"]["stderr"], "worker failed")
        self.assertEqual(
            trace.execution_result["summary"],
            "Agent failed to complete the approved remediation.",
        )
        self.assertNotIn("rollout_status", trace.execution_result)
        self.assertEqual(trace.verification_result["post_check"]["status"], "unrecovered")
        self.assertEqual(trace.final_state, "unrecovered")
        self.assertEqual(commands, [])

    def test_workflow_uses_kubernetes_fallback_when_prometheus_ready_lags(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "rollout", "status"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='deployment "cartservice" successfully rolled out\n',
                    stderr="",
                )
            if command[:3] == ["kubectl", "get", "deployment"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout=(
                        '{"status":{"availableReplicas":1,"readyReplicas":1,"observedGeneration":18}}'
                    ),
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        ready_queries = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=6,
            post_check_retry_sleep_seconds=0.0,
            sleep_fn=lambda _: None,
        )
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client()

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(
            trace.verification_result["post_check"]["kubernetes_fallback"]["is_available"],
            True,
        )
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(
            commands,
            [
                ["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"],
                ["kubectl", "get", "deployment", "cartservice", "-n", "default", "-o", "json"],
            ],
        )

    def test_workflow_refuses_execution_when_hitl_gate_halts(self) -> None:
        class FailingJudgeLLM:
            def evaluate(self, **_: object) -> JudgeLLMResult:
                return JudgeLLMResult(
                    verdict="fail",
                    reason="Judge LLM blocked the plan.",
                )

        with self.assertRaisesRegex(ValueError, "HITL Gate halted"):
            run_crashloop_recovery_from_payload(
                _crashloop_payload(),
                approve_action_id="rollout_undo_cartservice",
                judge_llm=FailingJudgeLLM(),
            )


if __name__ == "__main__":
    unittest.main()
