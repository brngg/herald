from __future__ import annotations

import subprocess
import unittest

from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient
from workflows.recovery_workflow import run_crashloop_recovery_from_payload


def _common_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
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


def _common_query_runner(_: str) -> float:
    return 1.0


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
                "annotations": {"summary": "cartservice is in CrashLoopBackOff"},
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
        "commonAnnotations": {"summary": "cartservice is in CrashLoopBackOff"},
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldCartserviceCrashLoopBackOff"}',
        "truncatedAlerts": 0,
    }


def _cpu_payload() -> dict[str, object]:
    payload = _crashloop_payload()
    payload["alerts"] = [
        {
            "status": "firing",
            "labels": {
                "alertname": "HeraldFrontendHighCPU",
                "incident_class": "cpu_saturation",
                "namespace": "default",
                "pod": "frontend-6f7f7b6c8f-aaaaa",
                "severity": "warning",
            },
            "annotations": {"summary": "frontend pod is experiencing high CPU"},
            "startsAt": "2026-03-23T20:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus/graph",
            "fingerprint": "cpu123",
        }
    ]
    payload["groupLabels"] = {"alertname": "HeraldFrontendHighCPU", "incident_class": "cpu_saturation", "namespace": "default"}
    payload["commonLabels"] = {
        "alertname": "HeraldFrontendHighCPU",
        "incident_class": "cpu_saturation",
        "namespace": "default",
        "severity": "warning",
    }
    payload["commonAnnotations"] = {"summary": "frontend pod is experiencing high CPU"}
    payload["groupKey"] = '{}/{namespace="default"}:{alertname="HeraldFrontendHighCPU"}'
    return payload


def _bad_config_payload() -> dict[str, object]:
    payload = _crashloop_payload()
    payload["alerts"] = [
        {
            "status": "firing",
            "labels": {
                "alertname": "HeraldFrontendCartProbeFailed",
                "incident_class": "bad_config",
                "namespace": "default",
                "pod": "frontend-6f7f7b6c8f-aaaaa",
                "severity": "critical",
            },
            "annotations": {"summary": "frontend /cart probe is failing"},
            "startsAt": "2026-03-23T20:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus/graph",
            "fingerprint": "badcfg123",
        }
    ]
    payload["groupLabels"] = {"alertname": "HeraldFrontendCartProbeFailed", "incident_class": "bad_config", "namespace": "default"}
    payload["commonLabels"] = {
        "alertname": "HeraldFrontendCartProbeFailed",
        "incident_class": "bad_config",
        "namespace": "default",
        "severity": "critical",
    }
    payload["commonAnnotations"] = {"summary": "frontend /cart probe is failing"}
    payload["groupKey"] = '{}/{namespace="default"}:{alertname="HeraldFrontendCartProbeFailed"}'
    return payload


def _network_payload() -> dict[str, object]:
    payload = _crashloop_payload()
    payload["alerts"] = [
        {
            "status": "firing",
            "labels": {
                "alertname": "HeraldCartserviceDependencyFailure",
                "incident_class": "network_partition",
                "namespace": "default",
                "pod": "cartservice-7d6b9f5bb4-abcde",
                "severity": "critical",
            },
            "annotations": {"summary": "cartservice network traffic is near zero"},
            "startsAt": "2026-03-23T20:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus/graph",
            "fingerprint": "network123",
        }
    ]
    payload["groupLabels"] = {"alertname": "HeraldCartserviceDependencyFailure", "incident_class": "network_partition", "namespace": "default"}
    payload["commonLabels"] = {
        "alertname": "HeraldCartserviceDependencyFailure",
        "incident_class": "network_partition",
        "namespace": "default",
        "severity": "critical",
    }
    payload["commonAnnotations"] = {"summary": "cartservice network traffic is near zero"}
    payload["groupKey"] = '{}/{namespace="default"}:{alertname="HeraldCartserviceDependencyFailure"}'
    return payload


class CriticShadowModeIntegrationTest(unittest.TestCase):
    def test_shadow_critic_runs_for_all_supported_slices(self) -> None:
        cases = [
            ("crashloop", _crashloop_payload(), "rollout_undo_cartservice"),
            ("cpu_saturation", _cpu_payload(), "delete_frontend_cpu_stresschaos"),
            ("bad_config", _bad_config_payload(), "rollout_undo_frontend_bad_config"),
            ("network_partition", _network_payload(), "delete_frontend_cartservice_network_partition"),
        ]

        for incident_class, payload, expected_action_id in cases:
            with self.subTest(incident_class=incident_class):
                result = run_crashloop_recovery_from_payload(
                    payload,
                    engine_mode="v2_shadow",
                    kubernetes_client=KubernetesClient(runner=_common_runner),
                    prometheus_client=PrometheusClient(query_runner=_common_query_runner),
                )

                self.assertEqual(result["engine_mode"], "v2_shadow")
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
                self.assertIn("critic_output", result["decision_trace"].fixer_plan["v2_shadow"])
                self.assertIn("policy_summary", result["decision_trace"].fixer_plan["v2_shadow"])
                self.assertIn("synthesis_output", result["decision_trace"].fixer_plan["v2_shadow"])
                self.assertGreaterEqual(
                    result["decision_trace"].fixer_plan["v2_shadow"]["policy_summary"]["approved_candidate_count"],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
