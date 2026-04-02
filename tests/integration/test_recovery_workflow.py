from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from unittest.mock import patch

from services.infra.kubernetes.execution_worker import ExecutionWorkerClient
from services.infra.kubernetes.client import KubernetesClient
from services.llm.tasks.judge_contract import JudgeLLMResult
from services.observability.prometheus import PrometheusClient
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
        deployment = dispatch["parameters"].get("deployment", "cartservice")
        namespace = dispatch["parameters"].get("namespace", "default")
        command = ["kubectl", "rollout", "undo", f"deployment/{{deployment}}", "-n", namespace]
        if dispatch["action_type"] == "rollout_restart_deployment":
            command = ["kubectl", "rollout", "restart", f"deployment/{{deployment}}", "-n", namespace]
        elif dispatch["action_type"] == "delete_stresschaos":
            command = ["kubectl", "delete", "stresschaos", dispatch["parameters"]["name"], "-n", dispatch["parameters"]["namespace"]]
        elif dispatch["action_type"] == "delete_networkchaos":
            command = ["kubectl", "delete", "networkchaos", dispatch["parameters"]["name"], "-n", dispatch["parameters"]["namespace"]]
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


def _bad_config_payload() -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldFrontendCartProbeFailed",
                    "incident_class": "bad_config",
                    "instance": "http://frontend.default.svc.cluster.local/cart",
                    "job": "probe/monitoring/herald-frontend-cart",
                    "namespace": "default",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "frontend /cart probe is failing",
                    "description": "The synthetic /cart probe is failing.",
                },
                "startsAt": "2026-03-23T20:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": "badconfig123",
            }
        ],
        "groupLabels": {
            "alertname": "HeraldFrontendCartProbeFailed",
            "incident_class": "bad_config",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldFrontendCartProbeFailed",
            "incident_class": "bad_config",
            "instance": "http://frontend.default.svc.cluster.local/cart",
            "job": "probe/monitoring/herald-frontend-cart",
            "namespace": "default",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "frontend /cart probe is failing",
        },
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldFrontendCartProbeFailed"}',
        "truncatedAlerts": 0,
    }


def _network_partition_payload() -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldCartserviceDependencyFailure",
                    "incident_class": "network_partition",
                    "namespace": "default",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "cartservice network traffic is near zero",
                    "description": "Cartservice is receiving near-zero network traffic.",
                },
                "startsAt": "2026-03-23T20:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": "network123",
            }
        ],
        "groupLabels": {
            "alertname": "HeraldCartserviceDependencyFailure",
            "incident_class": "network_partition",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldCartserviceDependencyFailure",
            "incident_class": "network_partition",
            "namespace": "default",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "cartservice network traffic is near zero",
        },
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldCartserviceDependencyFailure"}',
        "truncatedAlerts": 0,
    }


def _rollout_status_command(deployment: str, namespace: str = "default") -> list[str]:
    return [
        "kubectl",
        "rollout",
        "status",
        f"deployment/{deployment}",
        "-n",
        namespace,
        "--timeout=60s",
    ]


def _deployment_get_command(deployment: str, namespace: str = "default") -> list[str]:
    return [
        "kubectl",
        "get",
        "deployment",
        deployment,
        "-n",
        namespace,
        "-o",
        "json",
    ]


def _assert_rollout_status_count(
    testcase: unittest.TestCase,
    commands: list[list[str]],
    *,
    deployment: str,
    count: int,
    namespace: str = "default",
) -> None:
    testcase.assertEqual(commands.count(_rollout_status_command(deployment, namespace)), count)


def _assert_deployment_probe_count(
    testcase: unittest.TestCase,
    commands: list[list[str]],
    *,
    deployment: str,
    count: int,
    namespace: str = "default",
) -> None:
    testcase.assertEqual(commands.count(_deployment_get_command(deployment, namespace)), count)


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

    def test_v2_shadow_collects_observations_before_v1_planning(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "logs", "cartservice-7d6b9f5bb4-abcde"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="Authorization: bearer-token\nhealthy\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "history"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice with revision #3\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return 1.0
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_shadow",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(query_runner=query_runner),
        )

        self.assertEqual(result["engine_mode"], "v2_shadow")
        self.assertIsNotNone(result["observation_bundle"])
        self.assertIsNotNone(result["reasoner_state"])
        self.assertEqual(result["decision_trace_timeline"][0]["node_name"], "observe")
        self.assertEqual(result["decision_trace_timeline"][1]["node_name"], "reason")
        self.assertEqual(result["decision_trace_timeline"][2]["node_name"], "critique")
        self.assertEqual(result["decision_trace_timeline"][3]["node_name"], "synthesize")
        self.assertIn("observe", result["decision_trace"].node_runs_by_node)
        self.assertIn("reason", result["decision_trace"].node_runs_by_node)
        self.assertIn("critique", result["decision_trace"].node_runs_by_node)
        self.assertIn("synthesize", result["decision_trace"].node_runs_by_node)
        self.assertIn("deployment", result["observation_bundle"].kubernetes)
        self.assertIn("incident_signal", result["observation_bundle"].prometheus)
        self.assertIn("v2_shadow", result["decision_trace"].fixer_plan)
        self.assertEqual(result["decision_trace"].fixer_plan["v2_shadow"]["status"], "succeeded")
        self.assertTrue(result["decision_trace"].fixer_plan["v2_shadow"]["mapped_v1_candidates"])
        self.assertIn("critic_output", result["decision_trace"].fixer_plan["v2_shadow"])
        self.assertIn("policy_summary", result["decision_trace"].fixer_plan["v2_shadow"])
        self.assertIn("synthesis_output", result["decision_trace"].fixer_plan["v2_shadow"])
        self.assertIn("synthesized_v1_dispatches", result["decision_trace"].fixer_plan["v2_shadow"])
        self.assertTrue(commands)

    def test_v2_shadow_runs_verifier_for_all_benchmark_slices(self) -> None:
        cases = [
            (
                "crashloop",
                _crashloop_payload(),
                "rollout_undo_cartservice",
                {
                    "kube_pod_container_status_waiting_reason": [1.0, 1.0, 0.0, 0.0, 0.0],
                    "kube_pod_status_ready": [1.0, 1.0, 1.0, 1.0],
                },
                {},
            ),
            (
                "cpu_saturation",
                _cpu_payload(),
                "delete_frontend_cpu_stresschaos",
                {
                    "container_cpu_usage_seconds_total": [0.08, 0.08, 0.02, 0.02, 0.02],
                    "kube_pod_status_ready": [1.0, 1.0, 1.0, 1.0],
                },
                {},
            ),
            (
                "bad_config",
                _bad_config_payload(),
                "rollout_undo_frontend_bad_config",
                {
                    "probe_success": [0.0, 0.0, 1.0, 1.0, 1.0],
                    "kube_pod_status_ready": [1.0, 1.0, 1.0, 1.0],
                },
                {},
            ),
            (
                "network_partition",
                _network_partition_payload(),
                "delete_frontend_cartservice_network_partition",
                {
                    "container_network_receive_bytes_total": [0.0, 0.0, 150.0, 150.0, 150.0],
                    "kube_pod_status_ready": [1.0, 1.0, 1.0, 1.0],
                },
                {
                    "stdout": '{"items": [], "metadata": {"name": "ok"}}',
                },
            ),
        ]

        for incident_class, payload, approve_action_id, query_sequences, runner_kwargs in cases:
            with self.subTest(incident_class=incident_class):
                query_iters = {needle: iter(values) for needle, values in query_sequences.items()}

                def query_runner(query: str) -> float:
                    for needle, iterator in query_iters.items():
                        if needle in query:
                            return next(iterator)
                    raise AssertionError(f"Unexpected query: {query}")

                commands: list[list[str]] = []

                def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                    commands.append(list(command))
                    if tuple(command[:3]) in {
                        ("kubectl", "get", "stresschaos"),
                        ("kubectl", "get", "networkchaos"),
                    }:
                        return subprocess.CompletedProcess(
                            args=list(command),
                            returncode=1,
                            stdout="",
                            stderr="Not Found",
                        )
                    if command[:3] == ["kubectl", "get", "deployment"]:
                        return subprocess.CompletedProcess(
                            args=list(command),
                            returncode=0,
                            stdout='{"status": {"availableReplicas": 1, "readyReplicas": 1, "observedGeneration": 1}}',
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        args=list(command),
                        returncode=runner_kwargs.get("returncode", 0),
                        stdout=str(runner_kwargs.get("stdout", "ok")),
                        stderr=str(runner_kwargs.get("stderr", "")),
                    )

                result = run_recovery_from_payload(
                    payload,
                    engine_mode="v2_shadow",
                    approve_action_id=approve_action_id,
                    kubernetes_client=KubernetesClient(runner=runner),
                    prometheus_client=PrometheusClient(query_runner=query_runner),
                    execution_worker_client=_worker_client(
                        stdout=str(runner_kwargs.get("worker_stdout", "approved remediation completed\n")),
                    ),
                )

                trace = result["decision_trace"]
                shadow = trace.fixer_plan["v2_shadow"]

                self.assertEqual(result["engine_mode"], "v2_shadow")
                self.assertEqual(result["verifier_state"]["verification_status"], "passed")
                self.assertEqual(result["replanner_state"]["status"], "not_run")
                self.assertEqual(shadow["verification_status"], "passed")
                self.assertEqual(shadow["replanner_status"], "not_run")
                self.assertIn("verification_plan", shadow)
                self.assertIn("verification_result_v2", shadow)
                self.assertEqual(result["decision_trace_timeline"][-2]["node_name"], "verify")
                self.assertEqual(result["decision_trace_timeline"][-1]["node_name"], "finalization")
                self.assertIn("verify", trace.node_runs_by_node)
                self.assertNotIn("replan", trace.node_runs_by_node)
                self.assertTrue(commands)

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

        crashloop_queries = iter([1.0, 0.0, 0.0, 0.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        prometheus = PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None)
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
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=4)

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
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=4)

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

        crashloop_queries = iter([1.0, 0.0, 0.0, 0.0])

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
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(),
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(trace.node_runs_by_node["fixer"]["fixer:0001"]["attempt"], 1)
        self.assertEqual(trace.node_runs_by_node["judge"]["judge:0002"]["attempt"], 1)
        self.assertEqual(result["decision_trace_timeline"][0]["node_name"], "fixer")
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=4)

    def test_resume_from_saved_plan_preserves_shadow_engine_mode(self) -> None:
        def plan_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[:3] == ["kubectl", "logs", "cartservice-7d6b9f5bb4-abcde"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="healthy\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "history"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice with revision #3\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )

        planning_result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_shadow",
            kubernetes_client=KubernetesClient(runner=plan_runner),
            prometheus_client=PrometheusClient(query_runner=lambda _: 1.0),
        )

        crashloop_queries = iter([1.0, 0.0, 0.0, 0.0])

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
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )
            ),
            prometheus_client=PrometheusClient(query_runner=query_runner),
            execution_worker_client=_worker_client(),
        )

        self.assertEqual(result["engine_mode"], "v2_shadow")
        self.assertEqual(result["decision_trace_timeline"][0]["node_name"], "observe")
        self.assertEqual(result["decision_trace_timeline"][1]["node_name"], "reason")
        self.assertEqual(result["decision_trace_timeline"][2]["node_name"], "critique")
        self.assertEqual(result["decision_trace_timeline"][3]["node_name"], "synthesize")
        self.assertEqual(result["decision_trace_timeline"][-2]["node_name"], "verify")
        self.assertEqual(result["decision_trace_timeline"][-1]["node_name"], "finalization")
        self.assertIn("v2_shadow", result["decision_trace"].fixer_plan)
        self.assertIsNotNone(result["synthesizer_state"])
        self.assertIsNotNone(result["verifier_state"])
        self.assertIsNotNone(result["replanner_state"])
        self.assertEqual(result["verifier_state"]["verification_status"], "passed")
        self.assertEqual(result["replanner_state"]["status"], "not_run")
        self.assertEqual(result["decision_trace"].fixer_plan["v2_shadow"]["verification_status"], "passed")
        self.assertEqual(result["decision_trace"].fixer_plan["v2_shadow"]["replanner_status"], "not_run")

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
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            kubernetes_client=KubernetesClient(runner=runner),
            execution_worker_client=_worker_client(),
            input_fn=lambda _: "1",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.final_state, "recovered")
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=4)

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
        escalate_candidate = {
            "candidate_id": escalate_action.action_id,
            "summary": escalate_action.description,
            "confidence_score": escalate_action.confidence_score,
            "blast_radius_score": escalate_action.blast_radius_score,
            "requires_approval": escalate_action.requires_approval,
            "execution_plan": {
                "intent_id": escalate_action.action_id,
                "operation_family": "escalate.human_review",
                "target": {
                    "namespace": "default",
                    "kind": "Incident",
                    "name": "crashloop123",
                    "selector": None,
                },
                "summary": escalate_action.description,
                "steps": [],
                "allowed_tool_names": [],
                "blast_radius_score": escalate_action.blast_radius_score,
                "requires_approval": escalate_action.requires_approval,
                "rollback_outline": {},
            },
            "display_labels": ["escalate.human_review", "crashloop123"],
            "legacy_action_hint": {
                "action_id": escalate_action.action_id,
                "action_type": escalate_action.action_type,
                "description": escalate_action.description,
                "confidence_score": escalate_action.confidence_score,
                "blast_radius_score": escalate_action.blast_radius_score,
                "requires_approval": escalate_action.requires_approval,
                "parameters": escalate_action.parameters,
            },
        }
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
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=4)

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
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=1)

    def test_workflow_can_recover_even_if_rollout_wait_times_out(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "rollout", "status"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=1,
                    stdout="",
                    stderr="timed out waiting for the condition",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 0.0])
        ready_queries = iter([1.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(
                query_runner=query_runner,
                post_check_retry_attempts=1,
                post_check_retry_sleep_seconds=0.0,
                sleep_fn=lambda _: None,
            ),
            execution_worker_client=_worker_client(),
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.execution_result["rollout_status"]["status"], "failed")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertFalse(trace.rollback_triggered)
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=1)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=0)

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
                from services.llm.tasks.fixer_contract import FixerLLMResult
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
            if command[:3] == ["kubectl", "get", "deployment"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='{"status": {"availableReplicas": 0, "readyReplicas": 0, "observedGeneration": 1}}',
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 1.0, 0.0, 0.0])
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
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=2)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=8)
        self.assertEqual(
            commands.count(["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"]),
            1,
        )

    def test_v2_shadow_records_unrecovered_verification_after_bounded_rollback(self) -> None:
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
            if command[:3] == ["kubectl", "get", "deployment"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='{"status": {"availableReplicas": 0, "readyReplicas": 0, "observedGeneration": 1}}',
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
        ready_queries = iter([1.0, 0.0, 1.0, 1.0, 1.0])

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
            engine_mode="v2_shadow",
            approve_action_id="restart_cartservice",
            kubernetes_client=kubernetes,
            prometheus_client=prometheus,
            execution_worker_client=worker_client,
        )

        trace = result["decision_trace"]
        shadow = trace.fixer_plan["v2_shadow"]

        self.assertTrue(trace.rollback_triggered)
        self.assertEqual(trace.final_state, "rolled_back")
        self.assertEqual(trace.verification_result["post_check"]["status"], "unrecovered")
        self.assertEqual(result["verifier_state"]["verification_status"], "unrecovered")
        self.assertEqual(result["replanner_state"]["status"], "succeeded")
        self.assertEqual(result["replanner_state"]["replan_output"].decision, "escalate")
        self.assertEqual(shadow["verification_status"], "unrecovered")
        self.assertEqual(shadow["replanner_status"], "succeeded")
        self.assertEqual(shadow["replan_output"]["decision"], "escalate")
        self.assertIn("Rollback verification remains v1-only until Phase 5B.", shadow["verification_result_v2"]["warnings"])
        self.assertEqual(result["decision_trace_timeline"][-3]["node_name"], "verify")
        self.assertEqual(result["decision_trace_timeline"][-2]["node_name"], "replan")
        self.assertEqual(result["decision_trace_timeline"][-1]["node_name"], "finalization")
        self.assertIn(["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"], commands)
        _assert_rollout_status_count(self, commands, deployment="cartservice", count=3)
        _assert_deployment_probe_count(self, commands, deployment="cartservice", count=9)

    def test_v2_shadow_replanner_proposes_alternative_intent_after_unrecovered_undo(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_shadow",
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )
            ),
            prometheus_client=PrometheusClient(query_runner=lambda _: 1.0),
        )

        crashloop_queries = iter([1.0, 1.0, 1.0])
        ready_queries = iter([0.0, 0.0])

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        result = run_crashloop_recovery_from_saved_plan(
            _crashloop_payload(),
            planning_result,
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='deployment "cartservice" successfully rolled out\n',
                    stderr="",
                )
            ),
            prometheus_client=PrometheusClient(
                query_runner=query_runner,
                post_check_retry_attempts=1,
                post_check_retry_sleep_seconds=0.0,
                sleep_fn=lambda _: None,
            ),
            execution_worker_client=_worker_client(),
        )

        trace = result["decision_trace"]
        shadow = trace.fixer_plan["v2_shadow"]

        self.assertFalse(trace.rollback_triggered)
        self.assertEqual(trace.final_state, "escalated")
        self.assertEqual(result["verifier_state"]["verification_status"], "unrecovered")
        self.assertEqual(result["replanner_state"]["status"], "succeeded")
        self.assertEqual(result["replanner_state"]["replan_output"].decision, "propose_new_intent")
        self.assertEqual(
            result["replanner_state"]["replan_output"].intents[0].intent_id,
            "reasoner-rollout-restart-cartservice",
        )
        self.assertEqual(shadow["verification_status"], "unrecovered")
        self.assertEqual(shadow["replanner_status"], "succeeded")
        self.assertEqual(shadow["replan_output"]["decision"], "propose_new_intent")
        self.assertEqual(result["decision_trace_timeline"][-3]["node_name"], "verify")
        self.assertEqual(result["decision_trace_timeline"][-2]["node_name"], "replan")
        self.assertEqual(result["decision_trace_timeline"][-1]["node_name"], "finalization")

    def test_v2_shadow_marks_verification_not_run_for_escalate_and_worker_failure(self) -> None:
        planning_result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_shadow",
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )
            ),
            prometheus_client=PrometheusClient(query_runner=lambda query: 1.0 if "kube_pod_container_status_waiting_reason" in query else 1.0),
        )

        escalate_action = RemediationAction(
            action_id="escalate-cartservice-crashloop",
            action_type="escalate",
            description="Escalate to a human operator for deeper investigation.",
            confidence_score=0.9,
            blast_radius_score=0.1,
            requires_approval=True,
            parameters={"reason": "Automated remediation should not proceed."},
        )
        escalate_candidate = {
            "candidate_id": escalate_action.action_id,
            "summary": escalate_action.description,
            "confidence_score": escalate_action.confidence_score,
            "blast_radius_score": escalate_action.blast_radius_score,
            "requires_approval": escalate_action.requires_approval,
            "execution_plan": {
                "intent_id": escalate_action.action_id,
                "operation_family": "escalate.human_review",
                "target": {
                    "namespace": "default",
                    "kind": "Incident",
                    "name": "crashloop123",
                    "selector": None,
                },
                "summary": escalate_action.description,
                "steps": [],
                "allowed_tool_names": [],
                "blast_radius_score": escalate_action.blast_radius_score,
                "requires_approval": escalate_action.requires_approval,
                "rollback_outline": {},
            },
            "display_labels": ["escalate.human_review", "crashloop123"],
            "legacy_action_hint": {
                "action_id": escalate_action.action_id,
                "action_type": escalate_action.action_type,
                "description": escalate_action.description,
                "confidence_score": escalate_action.confidence_score,
                "blast_radius_score": escalate_action.blast_radius_score,
                "requires_approval": escalate_action.requires_approval,
                "parameters": escalate_action.parameters,
            },
        }
        planning_result["hitl_decision"]["recommended_candidate"] = escalate_candidate
        planning_result["hitl_decision"]["candidate_options"] = [escalate_candidate]
        planning_result["decision_trace"] = replace(
            planning_result["decision_trace"],
            fixer_plan={
                "candidate_options": [escalate_candidate],
                "planner_summary": "",
                "v2_shadow": planning_result["decision_trace"].fixer_plan["v2_shadow"],
            },
        )

        escalate_result = _continue_with_interactive_hitl(
            payload=_crashloop_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(query_runner=lambda query: 1.0 if "kube_pod_container_status_ready" in query else 1.0),
            input_fn=lambda _: "1",
        )

        self.assertEqual(escalate_result["decision_trace"].final_state, "escalated")
        self.assertEqual(escalate_result["verifier_state"]["verification_status"], "not_run")
        self.assertEqual(escalate_result["replanner_state"]["status"], "not_run")
        self.assertEqual(escalate_result["decision_trace"].fixer_plan["v2_shadow"]["verification_status"], "not_run")
        self.assertEqual(escalate_result["decision_trace"].fixer_plan["v2_shadow"]["replanner_status"], "not_run")

        worker_failure_result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_shadow",
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='deployment "cartservice" successfully rolled out\n',
                    stderr="",
                )
            ),
            prometheus_client=PrometheusClient(
                query_runner=lambda query: 1.0 if "kube_pod_container_status_waiting_reason" in query else 1.0,
            ),
            execution_worker_client=_worker_client(
                status="failed",
                returncode=1,
                stdout="",
                stderr="worker failed",
            ),
        )

        self.assertEqual(worker_failure_result["decision_trace"].final_state, "escalated")
        self.assertEqual(worker_failure_result["verifier_state"]["verification_status"], "not_run")
        self.assertEqual(worker_failure_result["replanner_state"]["status"], "not_run")
        self.assertEqual(worker_failure_result["decision_trace"].fixer_plan["v2_shadow"]["verification_status"], "not_run")
        self.assertEqual(worker_failure_result["decision_trace"].fixer_plan["v2_shadow"]["replanner_status"], "not_run")

    def test_v2_shadow_surfaces_verification_failure_reason_in_trace_payload(self) -> None:
        with patch(
            "workflows.recovery_workflow.run_verification",
            side_effect=RuntimeError("shadow verification exploded"),
        ):
            result = run_crashloop_recovery_from_payload(
                _crashloop_payload(),
                engine_mode="v2_shadow",
                approve_action_id="rollout_undo_cartservice",
                kubernetes_client=KubernetesClient(
                    runner=lambda command: subprocess.CompletedProcess(
                        args=list(command),
                        returncode=0,
                        stdout='deployment "cartservice" successfully rolled out\n',
                        stderr="",
                    )
                ),
                prometheus_client=PrometheusClient(
                    query_runner=lambda query: 1.0,
                ),
                execution_worker_client=_worker_client(),
            )

        shadow = result["decision_trace"].fixer_plan["v2_shadow"]
        self.assertEqual(result["verifier_state"]["verification_status"], "not_run")
        self.assertIn("shadow verification exploded", result["verifier_state"]["failure_reason"])
        self.assertIn("shadow verification exploded", shadow["verification_failure_reason"])

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

    def test_v2_execute_cpu_pilot_uses_synthesized_dispatch(self) -> None:
        cpu_queries = iter([0.08, 0.08, 0.02])

        def query_runner(query: str) -> float:
            if "container_cpu_usage_seconds_total" in query:
                return next(cpu_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        kubernetes = KubernetesClient(
            runner=lambda command: subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )
        )
        result = run_recovery_from_payload(
            _cpu_payload(),
            engine_mode="v2_execute",
            approve_action_id="delete_frontend_cpu_stresschaos",
            kubernetes_client=kubernetes,
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(result["engine_mode"], "v2_execute")
        self.assertIn("recommended_candidate", result["hitl_decision"])
        self.assertNotIn("recommended_action", result["hitl_decision"])
        self.assertEqual(
            [item["node_name"] for item in result["decision_trace_timeline"][:4]],
            ["observe", "reason", "critique", "synthesize"],
        )
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["candidate_id"], "reasoner-delete-frontend-stresschaos")
        self.assertEqual(trace.execution_result["intent_id"], "reasoner-delete-frontend-stresschaos")
        self.assertEqual(trace.execution_result["execution_plan"]["operation_family"], "chaos.delete_stresschaos")
        self.assertEqual(
            trace.execution_result["dispatch"]["allowed_tool_names"],
            ["get_stresschaos", "delete_stresschaos"],
        )
        self.assertEqual(trace.execution_result["dispatch"]["parameters"]["name"], "frontend-cpu-saturation")
        self.assertEqual(trace.execution_result["tool_transcript"][0]["tool_name"], "delete_stresschaos")
        self.assertEqual(trace.final_state, "recovered")
        self.assertNotIn("verify", trace.node_runs_by_node)
        self.assertNotIn("replan", trace.node_runs_by_node)

    def test_v2_execute_crashloop_restart_uses_exact_plan_and_rolls_back(self) -> None:
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
            if command[:3] == ["kubectl", "get", "deployment"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='{"status": {"availableReplicas": 0, "readyReplicas": 0, "observedGeneration": 1}}',
                    stderr="",
                )
            if command[:3] == ["kubectl", "logs", "cartservice-7d6b9f5bb4-abcde"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="Authorization: bearer-token\nhealthy\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "history"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice with revision #3\n",
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {command}")

        crashloop_queries = iter([1.0, 1.0, 0.0, 0.0])
        ready_queries = iter([1.0, 0.0, 1.0])

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
        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_execute",
            approve_action_id="restart_cartservice",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=prometheus,
            execution_worker_client=_worker_client(stdout="deployment.apps/cartservice restarted\n"),
        )

        trace = result["decision_trace"]

        self.assertEqual(result["engine_mode"], "v2_execute")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["candidate_id"], "reasoner-rollout-restart-cartservice")
        self.assertEqual(trace.execution_result["action_type"], "rollout_restart_deployment")
        self.assertTrue(trace.rollback_triggered)
        self.assertEqual(trace.execution_result["rollback"]["status"], "succeeded")
        self.assertEqual(trace.verification_result["post_check"]["status"], "unrecovered")
        self.assertEqual(trace.verification_result["post_rollback_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "rolled_back")
        self.assertIn(
            ["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=60s"],
            commands,
        )
        self.assertIn(
            ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"],
            commands,
        )

    def test_v2_execute_network_partition_pilot_uses_synthesized_dispatch(self) -> None:
        network_queries = iter([0.0, 0.0, 150.0])

        def query_runner(query: str) -> float:
            if "container_network_receive_bytes_total" in query:
                return next(network_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        kubernetes = KubernetesClient(
            runner=lambda command: subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )
        )
        result = run_recovery_from_payload(
            _network_partition_payload(),
            engine_mode="v2_execute",
            approve_action_id="delete_frontend_cartservice_network_partition",
            kubernetes_client=kubernetes,
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout='networkchaos.chaos-mesh.org "frontend-to-cartservice-partition" deleted\n',
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(result["engine_mode"], "v2_execute")
        self.assertEqual(
            [item["node_name"] for item in result["decision_trace_timeline"][:4]],
            ["observe", "reason", "critique", "synthesize"],
        )
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["candidate_id"], "reasoner-delete-networkchaos")
        self.assertEqual(trace.execution_result["execution_plan"]["operation_family"], "chaos.delete_networkchaos")
        self.assertEqual(
            trace.execution_result["dispatch"]["allowed_tool_names"],
            ["get_networkchaos", "delete_networkchaos"],
        )
        self.assertEqual(
            trace.execution_result["dispatch"]["parameters"]["name"],
            "frontend-to-cartservice-partition",
        )
        self.assertEqual(trace.execution_result["tool_transcript"][0]["tool_name"], "delete_networkchaos")
        self.assertEqual(trace.final_state, "recovered")
        self.assertNotIn("verify", trace.node_runs_by_node)
        self.assertNotIn("replan", trace.node_runs_by_node)

    def test_v2_execute_bad_config_pilot_uses_synthesized_dispatch(self) -> None:
        probe_queries = iter([0.0, 0.0, 1.0])
        commands: list[list[str]] = []

        def query_runner(query: str) -> float:
            if "probe_success" in query:
                return next(probe_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        result = run_recovery_from_payload(
            _bad_config_payload(),
            engine_mode="v2_execute",
            approve_action_id="rollout_undo_frontend_bad_config",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/frontend rolled back\n",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(result["engine_mode"], "v2_execute")
        self.assertEqual(
            [item["node_name"] for item in result["decision_trace_timeline"][:4]],
            ["observe", "reason", "critique", "synthesize"],
        )
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["candidate_id"], "reasoner-rollout-undo-frontend")
        self.assertEqual(trace.execution_result["execution_plan"]["operation_family"], "rollout.undo_deployment")
        self.assertEqual(
            trace.execution_result["dispatch"]["allowed_tool_names"],
            ["get_deployment_context", "get_rollout_status", "rollout_undo_deployment"],
        )
        self.assertEqual(trace.execution_result["dispatch"]["parameters"]["deployment"], "frontend")
        self.assertEqual(trace.execution_result["tool_transcript"][0]["tool_name"], "rollout_undo_deployment")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertNotIn("verify", trace.node_runs_by_node)
        self.assertNotIn("replan", trace.node_runs_by_node)
        self.assertIn(
            ["kubectl", "rollout", "status", "deployment/frontend", "-n", "default", "--timeout=60s"],
            commands,
        )

    def test_v2_execute_crashloop_undo_pilot_uses_synthesized_dispatch(self) -> None:
        crashloop_queries = iter([1.0, 1.0, 0.0, 0.0, 0.0])
        commands: list[list[str]] = []

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "logs", "cartservice-7d6b9f5bb4-abcde"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="Authorization: bearer-token\nhealthy\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "history"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice with revision #3\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "status"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout='deployment "cartservice" successfully rolled out\n',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_execute",
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/cartservice rolled back\n",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(result["engine_mode"], "v2_execute")
        self.assertEqual(
            [item["node_name"] for item in result["decision_trace_timeline"][:4]],
            ["observe", "reason", "critique", "synthesize"],
        )
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["candidate_id"], "reasoner-rollout-undo-cartservice")
        self.assertEqual(trace.execution_result["execution_plan"]["operation_family"], "rollout.undo_deployment")
        self.assertEqual(
            trace.execution_result["dispatch"]["allowed_tool_names"],
            ["get_deployment_context", "get_rollout_status", "rollout_undo_deployment"],
        )
        self.assertEqual(trace.execution_result["dispatch"]["parameters"]["deployment"], "cartservice")
        self.assertEqual(trace.execution_result["tool_transcript"][0]["tool_name"], "rollout_undo_deployment")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertNotIn("verify", trace.node_runs_by_node)
        self.assertNotIn("replan", trace.node_runs_by_node)
        self.assertIn(
            ["kubectl", "rollout", "status", "deployment/cartservice", "-n", "default", "--timeout=60s"],
            commands,
        )

    def test_v2_execute_crashloop_undo_waits_for_kubernetes_availability_grace(self) -> None:
        crashloop_queries = iter([1.0, 1.0, 0.0])
        ready_queries = iter([1.0, 0.0])
        deployment_statuses = iter(
            [
                '{"status": {"availableReplicas": 0, "readyReplicas": 0, "observedGeneration": 67}}',
                '{"status": {"availableReplicas": 0, "readyReplicas": 0, "observedGeneration": 67}}',
                '{"status": {"availableReplicas": 1, "readyReplicas": 1, "observedGeneration": 67}}',
            ]
        )
        commands: list[list[str]] = []

        def query_runner(query: str) -> float:
            if "kube_pod_container_status_waiting_reason" in query:
                return next(crashloop_queries)
            if "kube_pod_status_ready" in query:
                return next(ready_queries)
            raise AssertionError(f"Unexpected query: {query}")

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            if command[:3] == ["kubectl", "logs", "cartservice-7d6b9f5bb4-abcde"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="Authorization: bearer-token\nhealthy\n",
                    stderr="",
                )
            if command[:3] == ["kubectl", "rollout", "history"]:
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="deployment.apps/cartservice with revision #3\n",
                    stderr="",
                )
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
                    stdout=next(deployment_statuses),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"items": [], "metadata": {"name": "ok"}}',
                stderr="",
            )

        result = run_crashloop_recovery_from_payload(
            _crashloop_payload(),
            engine_mode="v2_execute",
            approve_action_id="rollout_undo_cartservice",
            kubernetes_client=KubernetesClient(runner=runner),
            prometheus_client=PrometheusClient(
                query_runner=query_runner,
                post_check_retry_attempts=1,
                post_check_retry_sleep_seconds=0.0,
                sleep_fn=lambda _: None,
            ),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/cartservice rolled back\n",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["dispatch_source"], "v2_execution_plan")
        self.assertEqual(trace.execution_result["deployment_availability"]["availability_status"], "available")
        self.assertGreaterEqual(trace.execution_result["deployment_availability"]["attempts"], 2)
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(
            trace.verification_result["post_check"]["kubernetes_fallback"]["is_available"],
            True,
        )
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(
            len([command for command in commands if command[:3] == ["kubectl", "get", "deployment"]]),
            3,
        )

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

    def test_bad_config_workflow_requires_explicit_approval_before_execution(self) -> None:
        result = run_recovery_from_payload(_bad_config_payload())

        hitl = result["hitl_decision"]
        trace = result["decision_trace"]

        self.assertTrue(hitl["requires_approval"])
        self.assertEqual(hitl["routing_decision"], "request_approval_single_action")
        self.assertEqual(hitl["recommended_action"].action_id, "rollout_undo_frontend_bad_config")
        self.assertEqual(trace.human_approval, "n/a")
        self.assertEqual(trace.final_state, "pending_approval")
        self.assertEqual(set(trace.node_runs_by_node.keys()), {"fixer", "judge", "hitl_gate"})

    def test_bad_config_workflow_executes_approved_rollback_and_marks_recovered(self) -> None:
        probe_queries = iter([0.0, 1.0])
        commands: list[list[str]] = []

        def query_runner(query: str) -> float:
            if "probe_success" in query:
                return next(probe_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        result = run_recovery_from_payload(
            _bad_config_payload(),
            approve_action_id="rollout_undo_frontend_bad_config",
            prometheus_client=PrometheusClient(query_runner=query_runner, sleep_fn=lambda _: None),
            kubernetes_client=KubernetesClient(runner=runner),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/frontend rolled back\n",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["action_type"], "rollout_undo_deployment")
        self.assertEqual(trace.execution_result["deployment"], "frontend")
        self.assertEqual(trace.execution_result["rollout_status"]["status"], "succeeded")
        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertIn("execution_worker", trace.node_runs_by_node)
        self.assertIn("rollout_wait", trace.node_runs_by_node)
        self.assertIn(
            ["kubectl", "rollout", "status", "deployment/frontend", "-n", "default", "--timeout=60s"],
            commands,
        )
        self.assertEqual(
            len([command for command in commands if command[:3] == ["kubectl", "get", "deployment"]]),
            4,
        )

    def test_bad_config_workflow_skips_execution_when_probe_telemetry_is_missing(self) -> None:
        commands: list[list[str]] = []

        def query_runner(query: str) -> float | None:
            if "probe_success" in query:
                return None
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        result = run_recovery_from_payload(
            _bad_config_payload(),
            approve_action_id="rollout_undo_frontend_bad_config",
            prometheus_client=PrometheusClient(query_runner=query_runner),
            kubernetes_client=KubernetesClient(runner=runner),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/frontend rolled back\n",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.verification_result["pre_check"]["status"], "unknown")
        self.assertFalse(trace.verification_result["pre_check"]["should_execute"])
        self.assertEqual(trace.execution_result["status"], "skipped")
        self.assertEqual(trace.final_state, "escalated")
        self.assertEqual(commands, [])

    def test_bad_config_workflow_marks_rejected_when_human_rejects_action(self) -> None:
        result = run_recovery_from_payload(
            _bad_config_payload(),
            reject_action_id="rollout_undo_frontend_bad_config",
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "rejected")
        self.assertEqual(trace.execution_result["status"], "not_executed")
        self.assertEqual(trace.final_state, "rejected")

    def test_bad_config_workflow_can_resume_from_saved_first_pass(self) -> None:
        planning_result = run_recovery_from_payload(_bad_config_payload())

        probe_queries = iter([0.0, 1.0])

        def query_runner(query: str) -> float:
            if "probe_success" in query:
                return next(probe_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        result = run_recovery_from_saved_plan(
            _bad_config_payload(),
            planning_result,
            approve_action_id="rollout_undo_frontend_bad_config",
            prometheus_client=PrometheusClient(query_runner=query_runner),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/frontend rolled back\n",
            ),
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.final_state, "recovered")
        self.assertEqual(trace.node_runs_by_node["fixer"]["fixer:0001"]["attempt"], 1)
        self.assertEqual(trace.node_runs_by_node["judge"]["judge:0002"]["attempt"], 1)

    def test_bad_config_workflow_surfaces_worker_failure_cleanly(self) -> None:
        result = run_recovery_from_payload(
            _bad_config_payload(),
            approve_action_id="rollout_undo_frontend_bad_config",
            prometheus_client=PrometheusClient(query_runner=lambda _: 0.0, sleep_fn=lambda _: None),
            execution_worker_client=_worker_client(
                status="failed",
                returncode=1,
                stdout="",
                stderr="worker failed",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["status"], "failed")
        self.assertEqual(trace.verification_result["post_check"]["status"], "not_run")
        self.assertEqual(trace.final_state, "escalated")

    def test_bad_config_workflow_interactive_hitl_approves_recommended_action(self) -> None:
        planning_result = run_recovery_from_payload(_bad_config_payload())

        probe_queries = iter([0.0, 1.0])

        def query_runner(query: str) -> float:
            if "probe_success" in query:
                return next(probe_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        result = _continue_with_interactive_hitl(
            payload=_bad_config_payload(),
            planning_result=planning_result,
            prometheus_client=PrometheusClient(query_runner=query_runner),
            kubernetes_client=KubernetesClient(
                runner=lambda command: subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )
            ),
            execution_worker_client=_worker_client(
                stdout="deployment.apps/frontend rolled back\n",
            ),
            input_fn=lambda _: "1",
        )

        trace = result["decision_trace"]
        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.final_state, "recovered")

    def test_network_partition_workflow_requires_explicit_approval_before_execution(self) -> None:
        result = run_recovery_from_payload(_network_partition_payload())

        hitl = result["hitl_decision"]
        trace = result["decision_trace"]

        self.assertTrue(hitl["requires_approval"])
        self.assertEqual(hitl["routing_decision"], "request_approval_single_action")
        self.assertEqual(hitl["recommended_action"].action_id, "delete_frontend_cartservice_network_partition")
        self.assertEqual(trace.human_approval, "n/a")
        self.assertEqual(trace.final_state, "pending_approval")
        self.assertEqual(set(trace.node_runs_by_node.keys()), {"fixer", "judge", "hitl_gate"})

    def test_network_partition_workflow_executes_approved_delete_and_marks_recovered(self) -> None:
        network_queries = iter([0.0, 150.0])

        def query_runner(query: str) -> float:
            if "container_network_receive_bytes_total" in query:
                return next(network_queries)
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        result = run_recovery_from_payload(
            _network_partition_payload(),
            approve_action_id="delete_frontend_cartservice_network_partition",
            prometheus_client=PrometheusClient(query_runner=query_runner),
            execution_worker_client=_worker_client(
                stdout='networkchaos.chaos-mesh.org "frontend-to-cartservice-partition" deleted\n',
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "approved")
        self.assertEqual(trace.execution_result["status"], "succeeded")
        self.assertEqual(trace.execution_result["action_type"], "delete_networkchaos")
        self.assertEqual(trace.execution_result["name"], "frontend-to-cartservice-partition")
        self.assertEqual(trace.verification_result["pre_check"]["status"], "ready_to_execute")
        self.assertEqual(trace.verification_result["post_check"]["status"], "recovered")
        self.assertEqual(trace.final_state, "recovered")
        self.assertIn("execution_worker", trace.node_runs_by_node)
        self.assertIn("post_check", trace.node_runs_by_node)

    def test_network_partition_workflow_skips_execution_when_network_telemetry_is_missing(self) -> None:
        def query_runner(query: str) -> float | None:
            if "container_network_receive_bytes_total" in query:
                return None
            if "kube_pod_status_ready" in query:
                return 1.0
            raise AssertionError(f"Unexpected query: {query}")

        result = run_recovery_from_payload(
            _network_partition_payload(),
            approve_action_id="delete_frontend_cartservice_network_partition",
            prometheus_client=PrometheusClient(query_runner=query_runner),
            execution_worker_client=_worker_client(
                stdout='networkchaos.chaos-mesh.org "frontend-to-cartservice-partition" deleted\n',
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.verification_result["pre_check"]["status"], "unknown")
        self.assertFalse(trace.verification_result["pre_check"]["should_execute"])
        self.assertEqual(trace.execution_result["status"], "skipped")
        self.assertEqual(trace.final_state, "escalated")

    def test_network_partition_workflow_marks_rejected_when_human_rejects_action(self) -> None:
        result = run_recovery_from_payload(
            _network_partition_payload(),
            reject_action_id="delete_frontend_cartservice_network_partition",
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.human_approval, "rejected")
        self.assertEqual(trace.execution_result["status"], "not_executed")
        self.assertEqual(trace.final_state, "rejected")

    def test_network_partition_workflow_surfaces_worker_failure_cleanly(self) -> None:
        result = run_recovery_from_payload(
            _network_partition_payload(),
            approve_action_id="delete_frontend_cartservice_network_partition",
            prometheus_client=PrometheusClient(
                query_runner=lambda query: 0.0 if "container_network_receive_bytes_total" in query else 1.0,
                sleep_fn=lambda _: None,
            ),
            execution_worker_client=_worker_client(
                status="failed",
                returncode=1,
                stdout="",
                stderr="worker failed",
            ),
        )

        trace = result["decision_trace"]

        self.assertEqual(trace.execution_result["status"], "failed")
        self.assertEqual(trace.verification_result["post_check"]["status"], "not_run")
        self.assertEqual(trace.final_state, "escalated")


if __name__ == "__main__":
    unittest.main()
