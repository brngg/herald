from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace

from services.execution_worker import ExecutionWorkerClient
from services.kubernetes_client import KubernetesClient
from services.judge_llm import JudgeLLMResult
from services.prometheus_client import PrometheusClient
from schemas.remediation import RemediationAction
from workflows.recovery_workflow import (
    _continue_with_interactive_hitl,
    run_recovery_from_payload,
    run_recovery_from_saved_plan,
    run_crashloop_recovery_from_payload,
    run_crashloop_recovery_from_saved_plan,
)


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
        command = ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"]
        if dispatch["action_type"] == "rollout_restart_deployment":
            command = ["kubectl", "rollout", "restart", "deployment/cartservice", "-n", "default"]
        elif dispatch["action_type"] == "delete_stresschaos":
            command = ["kubectl", "delete", "stresschaos", dispatch["parameters"]["name"], "-n", dispatch["parameters"]["namespace"]]
        result = {{
            "worker_id": dispatch["worker_id"],
            "action_id": dispatch["action_id"],
            "status": {status!r},
            "started_at": "2026-03-27T03:00:00+00:00",
            "finished_at": "2026-03-27T03:00:05+00:00",
            "command": command,
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


def _cpu_payload() -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldFrontendHighCPU",
                    "incident_class": "cpu_saturation",
                    "namespace": "default",
                    "pod": "frontend-6f7f7b6c8f-aaaaa",
                    "severity": "warning",
                },
                "annotations": {
                    "summary": "frontend pod is experiencing high CPU",
                    "description": "Frontend CPU usage is above the saturation threshold.",
                },
                "startsAt": "2026-03-23T20:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": "cpu123",
            }
        ],
        "groupLabels": {
            "alertname": "HeraldFrontendHighCPU",
            "incident_class": "cpu_saturation",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldFrontendHighCPU",
            "incident_class": "cpu_saturation",
            "namespace": "default",
            "severity": "warning",
        },
        "commonAnnotations": {
            "summary": "frontend pod is experiencing high CPU",
        },
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldFrontendHighCPU"}',
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
        self.assertEqual(set(trace.node_runs_by_node.keys()), {"fixer", "judge", "hitl_gate"})
        self.assertEqual(
            [item["node_name"] for item in result["decision_trace_timeline"]],
            ["fixer", "judge", "hitl_gate"],
        )

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
            trace.node_runs_by_node["execution_worker"][trace.latest_run_id_by_node["execution_worker"]]["summary"],
            "Execution worker completed the approved remediation action.",
        )
        execution_worker_explanation = trace.node_runs_by_node["execution_worker"][
            trace.latest_run_id_by_node["execution_worker"]
        ]["llm_explanation"]
        self.assertIn("Agent completed the approved remediation.", execution_worker_explanation)
        self.assertIn("The Gemini execution agent handled approved action 'rollout_undo_cartservice'", execution_worker_explanation)
        self.assertIn("It used the bounded tools rollout_undo_deployment before finishing.", execution_worker_explanation)
        self.assertIn("The approved action succeeded with return code 0.", execution_worker_explanation)
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
        self.assertTrue(
            {"human_approval", "pre_check", "execution_worker", "rollout_wait", "post_check", "finalization"}.issubset(
                set(trace.node_runs_by_node.keys())
            )
        )
        self.assertEqual(result["decision_trace_timeline"][-1]["node_name"], "finalization")
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

    def test_workflow_can_resume_from_saved_first_pass_without_rerunning_planning(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(_crashloop_payload())

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

        result = run_crashloop_recovery_from_saved_plan(
            _crashloop_payload(),
            planning_result,
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(query_runner=query_runner),
            execution_worker_client=_worker_client(),
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(trace.node_runs_by_node["fixer"]["fixer:0001"]["attempt"], 1)
        self.assertEqual(trace.node_runs_by_node["judge"]["judge:0002"]["attempt"], 1)
        self.assertEqual(result["decision_trace_timeline"][0]["node_name"], "fixer")
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"]],
        )

    def test_interactive_hitl_choice_approves_recommended_action(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(_crashloop_payload())

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

        result = _continue_with_interactive_hitl(
            payload=_crashloop_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(query_runner=query_runner),
            kubernetes_client=KubernetesClient(runner=runner),
            execution_worker_client=_worker_client(),
            input_fn=lambda _: "1",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(
            commands,
            [["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"]],
        )

    def test_interactive_hitl_choice_rejects_recommended_action(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(_crashloop_payload())

        result = _continue_with_interactive_hitl(
            payload=_crashloop_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(),
            input_fn=lambda _: "2",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "rejected")
        self.assertEqual(trace.final_state, "rejected")

    def test_interactive_hitl_approving_escalate_action_finalizes_cleanly(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(_crashloop_payload())
        escalate_action = RemediationAction(
            action_id="escalate-cartservice-crashloop",
            action_type="escalate",
            description="Escalate to a human operator for deeper investigation.",
            confidence_score=0.9,
            blast_radius_score=0.1,
            requires_approval=True,
            parameters={"reason": "Automated remediation should not proceed."},
        )
        planning_result["hitl_decision"]["recommended_action"] = escalate_action
        planning_result["hitl_decision"]["candidate_actions"] = [escalate_action]
        planning_result["decision_trace"] = replace(
            planning_result["decision_trace"],
            fixer_plan={
                "actions": [
                    {
                        "action_id": escalate_action.action_id,
                        "action_type": escalate_action.action_type,
                        "description": escalate_action.description,
                        "confidence_score": escalate_action.confidence_score,
                        "blast_radius_score": escalate_action.blast_radius_score,
                        "requires_approval": escalate_action.requires_approval,
                        "parameters": escalate_action.parameters,
                    }
                ],
                "fixer_rationale": "",
            },
        )

        result = _continue_with_interactive_hitl(
            payload=_crashloop_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(),
            input_fn=lambda _: "1",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "not_executed")
        self.assertEqual(trace.execution_result["action_type"], "escalate")
        self.assertEqual(trace.verification_result["status"], "not_run")
        self.assertEqual(trace.final_state, "escalated")

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
        self.assertEqual(trace.verification_result["post_check"]["status"], "not_run")
        self.assertEqual(trace.final_state, "escalated")
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

    def test_workflow_marks_rejected_when_human_rejects_action(self) -> None:
        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            reject_action_id="rollout_undo_cartservice",
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "rejected")
        self.assertEqual(trace.execution_result["status"], "not_executed")
        self.assertEqual(trace.verification_result["status"], "not_run")
        self.assertEqual(trace.final_state, "rejected")
        self.assertFalse(trace.rollback_triggered)
        self.assertIn("human_approval", trace.node_runs_by_node)
        self.assertEqual(result["decision_trace_timeline"][-1]["status"], "rejected")

    def test_workflow_records_optional_llm_explanations_in_node_runs(self) -> None:
        class StubFixerLLM:
            def propose(self, *, incident_summary: str, evidence: dict[str, object]) -> object:
                from services.fixer_llm import FixerLLMResult
                from schemas.remediation import RemediationAction

                return FixerLLMResult(
                    rationale="Rollback is the safest first move because it is bounded and reversible.",
                    actions=[
                        RemediationAction(
                            action_id="rollout_undo_cartservice",
                            action_type="rollout_undo_deployment",
                            description="Roll back cartservice Deployment to the previous ReplicaSet.",
                            confidence_score=0.9,
                            blast_radius_score=0.3,
                            requires_approval=True,
                            parameters={"namespace": "default", "deployment": "cartservice"},
                        ),
                        RemediationAction(
                            action_id="restart_cartservice",
                            action_type="rollout_restart_deployment",
                            description="Restart cartservice Deployment to clear transient crashloop state.",
                            confidence_score=0.5,
                            blast_radius_score=0.2,
                            requires_approval=True,
                            parameters={"namespace": "default", "deployment": "cartservice"},
                        ),
                    ],
                )

        class StubJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[object],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                return JudgeLLMResult(
                    verdict="pass",
                    reason="The rollback recommendation stays within the approved blast-radius envelope.",
                )

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            fixer_llm=StubFixerLLM(),
            judge_llm=StubJudgeLLM(),
        )

        trace = result["decision_trace"]
        fixer_run = trace.node_runs_by_node["fixer"][trace.latest_run_id_by_node["fixer"]]
        judge_run = trace.node_runs_by_node["judge"][trace.latest_run_id_by_node["judge"]]

        self.assertEqual(
            fixer_run["llm_explanation"],
            "Rollback is the safest first move because it is bounded and reversible.",
        )
        self.assertEqual(
            judge_run["llm_explanation"],
            "The rollback recommendation stays within the approved blast-radius envelope.",
        )

    def test_workflow_triggers_bounded_rollback_after_restart_verification_failure(self) -> None:
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
            if command[:3] == ["kubectl", "rollout", "undo"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice rolled back\n",
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 1.0, 0.0])
        ready_queries = iter([0.0, 1.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=1,
            post_check_retry_sleep_seconds=0.0,
            sleep_fn=lambda _: None,
        )
        kubernetes = KubernetesClient(runner=runner)
        worker_client = _worker_client(stdout="deployment.apps/cartservice restarted\n")

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="restart_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]

        self.assertTrue(trace.rollback_triggered)
        self.assertEqual(trace.execution_result["rollback"]["status"], "succeeded")
        self.assertEqual(trace.execution_result["rollback"]["action_type"], "rollout_undo_deployment")
        self.assertEqual(trace.verification_result["post_check"]["status"], "unrecovered")
        self.assertEqual(trace.verification_result["post_rollback_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "rolled_back")
        self.assertIn("rollback", trace.node_runs_by_node)
        self.assertEqual(result["decision_trace_timeline"][-1]["status"], "rolled_back")
        self.assertEqual(
            commands,
            [
                ["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"],
                ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
                ["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=300s"],
            ],
        )

    def test_workflow_returns_escalated_trace_when_hitl_gate_halts(self) -> None:
        class FailingJudgeLLM:
            def evaluate(self, **_: object) -> JudgeLLMResult:
                return JudgeLLMResult(
                    verdict="fail",
                    reason="Judge LLM blocked the plan.",
                )

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            judge_llm=FailingJudgeLLM(),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.final_state, "escalated")
        self.assertEqual(trace.execution_result["status"], "halted")
        self.assertEqual(trace.verification_result["status"], "not_run")
        self.assertFalse(result["hitl_decision"]["requires_approval"])
        self.assertEqual(result["decision_trace_timeline"][-1]["status"], "escalated")

    def test_cpu_workflow_requires_explicit_approval_before_execution(self) -> None:
        result = run_recovery_from_payload(_cpu_payload())

        hitl = result["hitl_decision"]
        trace = result["decision_trace"]

        self.assertTrue(hitl["requires_approval"])
        self.assertEqual(hitl["routing_decision"], "request_approval_single_action")
        self.assertEqual(hitl["recommended_action"].action_type, "delete_stresschaos")
        self.assertEqual(trace.human_approval, "n/a")
        self.assertEqual(trace.final_state, "pending_approval")
        self.assertEqual(set(trace.node_runs_by_node.keys()), {"fixer", "judge", "hitl_gate"})

    def test_cpu_workflow_executes_approved_delete_and_marks_recovered(self) -> None:
        cpu_queries = iter([0.08, 0.02, 1.0])

        def query_runner(_: str) -> float:
            return next(cpu_queries)

        result = run_recovery_from_payload(
            _cpu_payload(),
            approve_action_id="delete_frontend_cpu_stresschaos",
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["action_type"], "delete_stresschaos")
        self.assertEqual(trace.execution_result["name"], "frontend-cpu-saturation")
        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertNotIn("rollout_wait", trace.node_runs_by_node)
        self.assertIn("execution_worker", trace.node_runs_by_node)

    def test_cpu_workflow_marks_rejected_when_human_rejects_action(self) -> None:
        result = run_recovery_from_payload(
            _cpu_payload(),
            reject_action_id="delete_frontend_cpu_stresschaos",
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "rejected")
        self.assertEqual(trace.execution_result["status"], "not_executed")
        self.assertEqual(trace.final_state, "rejected")

    def test_cpu_workflow_can_resume_from_saved_first_pass(self) -> None:
        planning_result = run_recovery_from_payload(_cpu_payload())
        cpu_queries = iter([0.08, 0.02, 1.0])

        def query_runner(_: str) -> float:
            return next(cpu_queries)

        result = run_recovery_from_saved_plan(
            _cpu_payload(),
            planning_result,
            approve_action_id="delete_frontend_cpu_stresschaos",
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
            ),
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(trace.node_runs_by_node["fixer"]["fixer:0001"]["attempt"], 1)
        self.assertEqual(trace.node_runs_by_node["judge"]["judge:0002"]["attempt"], 1)

    def test_cpu_workflow_surfaces_worker_failure_cleanly(self) -> None:
        result = run_recovery_from_payload(
            _cpu_payload(),
            approve_action_id="delete_frontend_cpu_stresschaos",
            prometheus_client=PrometheusClient(query_runner=lambda _: 0.08, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                status="failed",
                returncode=1,
                stdout="",
                stderr="chaos delete failed",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["status"], "failed")
        self.assertEqual(trace.verification_result["post_check"]["status"], "not_run")
        self.assertEqual(trace.final_state, "escalated")

    def test_cpu_workflow_interactive_hitl_approves_recommended_action(self) -> None:
        planning_result = run_recovery_from_payload(_cpu_payload())
        cpu_queries = iter([0.08, 0.02, 1.0])

        def query_runner(_: str) -> float:
            return next(cpu_queries)

        result = _continue_with_interactive_hitl(
            payload=_cpu_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
            ),
            input_fn=lambda _: "1",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.final_state, "recovered")


if __name__ == "__main__":
    unittest.main()
