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


def _runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=0,
        stdout='{"items": [], "metadata": {"name": "ok"}}',
        stderr="",
    )


def _query_runner(_: str) -> float:
    return 1.0


class SynthesizerShadowModeIntegrationTest(unittest.TestCase):
    def test_v2_shadow_emits_synthesize_and_bounded_commands(self) -> None:
        cases = [
            (_crashloop_payload(), "rollout_undo_cartservice", "rollout_undo_deployment"),
            (_cpu_payload(), "delete_frontend_cpu_stresschaos", "delete_stresschaos"),
            (_bad_config_payload(), "rollout_undo_frontend_bad_config", "rollout_undo_deployment"),
            (_network_partition_payload(), "delete_frontend_cartservice_network_partition", "delete_networkchaos"),
        ]

        for payload, expected_action_id, expected_action_type in cases:
            with self.subTest(expected_action_id=expected_action_id):
                result = run_recovery_from_payload(
                    payload,
                    engine_mode="v2_shadow",
                    kubernetes_client=KubernetesClient(runner=_runner),
                    prometheus_client=PrometheusClient(query_runner=_query_runner),
                )

                trace = result["decision_trace"]
                shadow = trace.fixer_plan["v2_shadow"]
                self.assertEqual(
                    [entry["node_name"] for entry in result["decision_trace_timeline"][:4]],
                    ["observe", "reason", "critique", "synthesize"],
                )
                recommended_candidate = result["hitl_decision"]["recommended_candidate"]
                self.assertIsNotNone(recommended_candidate)
                self.assertEqual(
                    recommended_candidate.legacy_action_hint["action_id"],
                    expected_action_id,
                )
                self.assertEqual(
                    recommended_candidate.legacy_action_hint["action_type"],
                    expected_action_type,
                )
                self.assertIn("synthesis_output", shadow)
                self.assertIn("synthesized_v1_dispatches", shadow)
                self.assertTrue(shadow["synthesis_output"]["plans"])
                self.assertTrue(
                    any(
                        dispatch["action_type"] == expected_action_type
                        for dispatch in shadow["synthesized_v1_dispatches"]
                    )
                )


if __name__ == "__main__":
    unittest.main()
