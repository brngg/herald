from __future__ import annotations

import subprocess
import unittest

from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient
from tests.integration.test_recovery_workflow import (
    _bad_config_payload,
    _cpu_payload,
    _crashloop_payload,
    _network_partition_payload,
)
from workflows.recovery_workflow import run_recovery_from_payload


def _shadow_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
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


def _shadow_query_runner(query: str) -> float:
    if "kube_pod_status_ready" in query:
        return 1.0
    if "kube_pod_container_status_waiting_reason" in query:
        return 1.0
    if "container_cpu_usage_seconds_total" in query:
        return 0.08
    if "probe_success" in query:
        return 0.0
    if "container_network_receive_bytes_total" in query:
        return 0.0
    raise AssertionError(f"Unexpected query: {query}")


class ReasonerShadowModeIntegrationTest(unittest.TestCase):
    def test_v2_shadow_emits_intents_and_surfaces_v2_recommended_candidate(self) -> None:
        scenarios = [
            (_crashloop_payload(), "rollout_undo_cartservice"),
            (_cpu_payload(), "delete_frontend_cpu_stresschaos"),
            (_bad_config_payload(), "rollout_undo_frontend_bad_config"),
            (_network_partition_payload(), "delete_frontend_cartservice_network_partition"),
        ]

        for payload, expected_action_id in scenarios:
            with self.subTest(expected_action_id=expected_action_id):
                result = run_recovery_from_payload(
                    payload,
                    engine_mode="v2_shadow",
                    kubernetes_client=KubernetesClient(runner=_shadow_runner),
                    prometheus_client=PrometheusClient(query_runner=_shadow_query_runner),
                )

                trace = result["decision_trace"]
                reasoner_state = result["reasoner_state"]

                self.assertEqual(result["engine_mode"], "v2_shadow")
                self.assertEqual(result["decision_trace_timeline"][0]["node_name"], "observe")
                self.assertEqual(result["decision_trace_timeline"][1]["node_name"], "reason")
                self.assertEqual(result["decision_trace_timeline"][2]["node_name"], "critique")
                self.assertEqual(result["decision_trace_timeline"][3]["node_name"], "synthesize")
                self.assertEqual(reasoner_state["status"], "succeeded")
                self.assertIsNotNone(reasoner_state["reasoner_output"])
                self.assertTrue(reasoner_state["mapped_v1_candidates"])
                self.assertEqual(
                    trace.fixer_plan["v2_shadow"]["status"],
                    "succeeded",
                )
                self.assertGreaterEqual(
                    len(trace.fixer_plan["v2_shadow"]["reasoner_output"]["intents"]),
                    1,
                )
                recommended_candidate = result["hitl_decision"]["recommended_candidate"]
                self.assertIsNotNone(recommended_candidate)
                self.assertEqual(
                    recommended_candidate.legacy_action_hint["action_id"],
                    expected_action_id,
                )
