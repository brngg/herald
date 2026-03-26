from __future__ import annotations

import subprocess
import unittest

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

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
        )

        trace = result["decision_trace"]
        hitl = result["hitl_decision"]

        self.assertEqual(hitl["recommended_action"].action_id, "rollout_undo_cartservice")
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["action_type"], "rollout_undo_deployment")
        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertFalse(trace.rollback_triggered)
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"]],
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
