from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any

from schemas.incident import Incident
from services.observability.cluster_observer import ClusterObserver


class _FakeKubernetesClient:
    def list_pods(self, *, namespace: str, label_selector: str | None = None) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "label_selector": label_selector,
            "resource": {
                "items": [
                    {
                        "metadata": {"name": "cartservice-abcde"},
                        "spec": {"nodeName": "minikube"},
                        "status": {
                            "containerStatuses": [
                                {
                                    "ready": False,
                                    "restartCount": 4,
                                    "state": {"waiting": {"reason": "RunContainerError"}},
                                    "lastState": {"terminated": {"reason": "StartError"}},
                                }
                            ]
                        },
                    }
                ]
            },
        }

    def get_events(self, *, namespace: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "resource": {
                "items": [
                    {
                        "type": "Warning",
                        "reason": "Failed",
                        "message": "Container failed to start.",
                        "lastTimestamp": "2026-03-29T20:00:05Z",
                        "involvedObject": {"kind": "Pod", "name": "cartservice-abcde"},
                    }
                ]
            },
        }

    def get_resource_json(self, *, namespace: str, kind: str, name: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "resource": {
                "metadata": {"name": name, "generation": 7},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "image": "example.com/cartservice:v2",
                                    "command": ["/herald-intentional-crash"],
                                    "env": [
                                        {
                                            "name": "CONFIG_VALUE",
                                            "valueFrom": {
                                                "configMapKeyRef": {"name": "cartservice-config"},
                                            },
                                        }
                                    ],
                                    "envFrom": [
                                        {"secretRef": {"name": "cartservice-secret"}},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"secret": {"secretName": "cartservice-volume-secret"}},
                            ],
                        }
                    },
                },
                "status": {
                    "updatedReplicas": 1,
                    "readyReplicas": 0,
                    "availableReplicas": 0,
                    "observedGeneration": 7,
                    "conditions": [
                        {
                            "type": "Available",
                            "status": "False",
                            "reason": "MinimumReplicasUnavailable",
                        }
                    ],
                },
            },
        }

    def get_rollout_history(self, *, namespace: str, deployment: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "deployment": deployment,
            "output": "deployment.apps/cartservice with revision #3\n",
        }

    def get_service_endpoints(self, *, namespace: str, service: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "service": service,
            "resource": {
                "subsets": [
                    {
                        "addresses": [{"ip": "10.0.0.5"}],
                        "notReadyAddresses": [{"ip": "10.0.0.6"}],
                        "ports": [{"port": 7070}],
                    }
                ]
            },
        }

    def get_service_context(self, *, namespace: str, service: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "service": service,
            "resource": {
                "metadata": {"name": service},
                "spec": {
                    "type": "ClusterIP",
                    "selector": {"app": "cartservice"},
                    "ports": [
                        {"name": "grpc", "port": 7070, "targetPort": 7070, "protocol": "TCP"}
                    ],
                },
            },
        }

    def list_endpoint_slices(self, *, namespace: str, service: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "service": service,
            "resource": {
                "items": [
                    {
                        "endpoints": [
                            {"conditions": {"ready": True}},
                            {"conditions": {"ready": False}},
                        ],
                        "ports": [{"port": 7070}],
                    }
                ]
            },
        }

    def list_replica_sets(self, *, namespace: str, label_selector: str | None = None) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "label_selector": label_selector,
            "resource": {
                "items": [
                    {
                        "metadata": {
                            "name": "cartservice-abc123",
                            "annotations": {"deployment.kubernetes.io/revision": "6"},
                        },
                        "spec": {"replicas": 0},
                        "status": {"readyReplicas": 0, "availableReplicas": 0},
                    },
                    {
                        "metadata": {
                            "name": "cartservice-def456",
                            "annotations": {"deployment.kubernetes.io/revision": "7"},
                        },
                        "spec": {"replicas": 1},
                        "status": {"readyReplicas": 0, "availableReplicas": 0},
                    },
                ]
            },
        }

    def get_config_map_context(self, *, namespace: str, name: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "name": name,
            "resource": {
                "metadata": {"name": name},
                "data": {"CONFIG_VALUE": "example"},
            },
        }

    def get_secret_context_metadata(self, *, namespace: str, name: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "name": name,
            "resource": {
                "metadata": {"name": name},
                "type": "Opaque",
                "data_keys": ["token"],
            },
        }

    def get_resource_quotas(self, *, namespace: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "resource": {
                "items": [
                    {
                        "metadata": {"name": "default-quota"},
                        "status": {
                            "hard": {"limits.cpu": "4"},
                            "used": {"limits.cpu": "1"},
                        },
                    }
                ]
            },
        }

    def list_persistent_volume_claims(self, *, namespace: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "resource": {
                "items": [
                    {
                        "metadata": {"name": "cartservice-pvc"},
                        "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
                    }
                ]
            },
        }

    def list_horizontal_pod_autoscalers(self, *, namespace: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "namespace": namespace,
            "resource": {
                "items": [
                    {
                        "metadata": {"name": "cartservice"},
                        "status": {"currentReplicas": 1, "desiredReplicas": 1},
                    }
                ]
            },
        }

    def get_node_conditions(self, *, node: str) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "node": node,
            "resource": {
                "metadata": {"name": node},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "False"},
                    ]
                },
            },
        }

    def get_pod_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: int = 100,
    ) -> dict[str, Any]:
        del tail_lines
        return {
            "status": "succeeded",
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "output": "Authorization: bearer-token\npassword=hunter2\nhealthy\n",
        }


class _FakePrometheusClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, query: str) -> dict[str, object]:
        self.queries.append(query)
        value = 1.0
        if "CrashLoopBackOff" in query:
            value = 2.0
        return {
            "status": "succeeded",
            "query": query,
            "value": value,
        }


class ClusterObserverTest(unittest.TestCase):
    def test_collects_live_context_and_redacts_logs(self) -> None:
        prometheus = _FakePrometheusClient()
        observer = ClusterObserver(
            kubernetes_client=_FakeKubernetesClient(),
            prometheus_client=prometheus,
        )
        incident = Incident(
            incident_id="incident-123",
            incident_class="crashloop",
            detected_at=datetime(2026, 3, 29, 20, 0, tzinfo=UTC),
            source="prometheus",
            raw_context={
                "alert": {
                    "labels": {
                        "namespace": "default",
                        "pod": "cartservice-7d6b9f5bb4-abcde",
                        "container": "server",
                    },
                    "annotations": {"summary": "cartservice is crash looping"},
                }
            },
        )

        bundle = observer.collect(incident=incident)

        self.assertEqual(bundle.incident_id, "incident-123")
        self.assertEqual(bundle.incident_class_hint, "crashloop")
        self.assertEqual(bundle.namespace_hint, "default")
        self.assertIn("deployment", bundle.kubernetes)
        self.assertIn("deployment_summary", bundle.kubernetes)
        self.assertIn("pod_logs", bundle.kubernetes)
        self.assertIn("pod_status_summary", bundle.kubernetes)
        self.assertIn("event_summary", bundle.kubernetes)
        self.assertIn("endpoint_summary", bundle.kubernetes)
        self.assertIn("service_summary", bundle.kubernetes)
        self.assertIn("endpoint_slice_summary", bundle.kubernetes)
        self.assertIn("dependency_summary", bundle.kubernetes)
        self.assertIn("config_map_summary", bundle.kubernetes)
        self.assertIn("secret_summary", bundle.kubernetes)
        self.assertIn("resource_quota_summary", bundle.kubernetes)
        self.assertIn("pvc_summary", bundle.kubernetes)
        self.assertIn("hpa_summary", bundle.kubernetes)
        self.assertIn("node_condition_summary", bundle.kubernetes)
        self.assertIn("replica_set_summary", bundle.kubernetes)
        self.assertIn("rollout_summary", bundle.kubernetes)
        self.assertIn("ready", bundle.prometheus)
        self.assertIn("incident_signal", bundle.prometheus)
        self.assertEqual(bundle.errors, [])
        self.assertIn("<redacted>", bundle.kubernetes["pod_logs"]["output"])
        self.assertNotIn("hunter2", bundle.kubernetes["pod_logs"]["output"])
        self.assertEqual(bundle.kubernetes["pod_status_summary"]["waiting_reasons"]["RunContainerError"], 1)
        self.assertEqual(bundle.kubernetes["pod_status_summary"]["termination_reasons"]["StartError"], 1)
        self.assertIn("cartservice-config", bundle.kubernetes["deployment_summary"]["config_map_refs"])
        self.assertIn("cartservice-secret", bundle.kubernetes["deployment_summary"]["secret_refs"])
        self.assertIn("cartservice-volume-secret", bundle.kubernetes["deployment_summary"]["secret_refs"])
        self.assertEqual(bundle.kubernetes["endpoint_summary"]["address_count"], 1)
        self.assertEqual(bundle.kubernetes["endpoint_summary"]["not_ready_address_count"], 1)
        self.assertEqual(bundle.kubernetes["service_summary"]["selector"]["app"], "cartservice")
        self.assertEqual(bundle.kubernetes["endpoint_slice_summary"]["ready_endpoint_count"], 1)
        self.assertEqual(bundle.kubernetes["endpoint_slice_summary"]["not_ready_endpoint_count"], 1)
        self.assertEqual(bundle.kubernetes["dependency_summary"]["service_port_count"], 1)
        self.assertEqual(bundle.kubernetes["dependency_summary"]["endpoint_slice_ready_count"], 1)
        self.assertEqual(bundle.kubernetes["config_map_summary"]["count"], 1)
        self.assertEqual(bundle.kubernetes["secret_summary"]["count"], 2)
        self.assertEqual(bundle.kubernetes["resource_quota_summary"]["quota_count"], 1)
        self.assertEqual(bundle.kubernetes["pvc_summary"]["pvc_count"], 1)
        self.assertEqual(bundle.kubernetes["hpa_summary"]["hpa_count"], 1)
        self.assertEqual(bundle.kubernetes["node_condition_summary"]["node_count"], 1)
        self.assertEqual(bundle.kubernetes["replica_set_summary"]["recent_replica_sets"][0]["revision"], 7)
        self.assertEqual(bundle.kubernetes["rollout_summary"]["latest_revision"], 3)
        self.assertEqual(bundle.kubernetes["event_summary"]["warning_count"], 1)
        self.assertTrue(prometheus.queries)


if __name__ == "__main__":
    unittest.main()
