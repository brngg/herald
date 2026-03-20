from __future__ import annotations

import unittest

from services.alertmanager_client import (
    AlertmanagerParseError,
    incidents_from_alertmanager_payload,
)


def _sample_payload() -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldFrontendCartProbeFailed",
                    "herald": "true",
                    "incident_class": "bad_config",
                    "instance": "http://frontend.default.svc.cluster.local/cart",
                    "job": "probe/monitoring/herald-frontend-cart",
                    "namespace": "default",
                    "prometheus": "monitoring/monitoring-kube-prometheus-prometheus",
                    "severity": "critical",
                },
                "annotations": {
                    "description": "The synthetic /cart probe is failing.",
                    "summary": "frontend /cart probe is failing",
                },
                "startsAt": "2026-03-20T19:13:49.873Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://monitoring-kube-prometheus-prometheus.monitoring:9090/graph",
                "fingerprint": "7f53ba61768c6182",
            }
        ],
        "groupLabels": {
            "alertname": "HeraldFrontendCartProbeFailed",
            "incident_class": "bad_config",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldFrontendCartProbeFailed",
            "herald": "true",
            "incident_class": "bad_config",
            "instance": "http://frontend.default.svc.cluster.local/cart",
            "job": "probe/monitoring/herald-frontend-cart",
            "namespace": "default",
            "prometheus": "monitoring/monitoring-kube-prometheus-prometheus",
            "severity": "critical",
        },
        "commonAnnotations": {
            "description": "The synthetic /cart probe is failing.",
            "summary": "frontend /cart probe is failing",
        },
        "externalURL": "http://monitoring-kube-prometheus-alertmanager.monitoring:9093",
        "version": "4",
        "groupKey": "{}/{herald=\"true\",namespace=\"default\"}:{alertname=\"HeraldFrontendCartProbeFailed\"}",
        "truncatedAlerts": 0,
    }


class AlertmanagerIngestTest(unittest.TestCase):
    def test_incidents_from_alertmanager_payload_maps_fields(self) -> None:
        incidents = incidents_from_alertmanager_payload(_sample_payload())

        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident.incident_id, "7f53ba61768c6182")
        self.assertEqual(incident.incident_class, "bad_config")
        self.assertEqual(incident.source, "prometheus")
        self.assertEqual(
            incident.raw_context["alert"]["labels"]["alertname"],
            "HeraldFrontendCartProbeFailed",
        )

    def test_incidents_from_alertmanager_payload_requires_alerts(self) -> None:
        with self.assertRaises(AlertmanagerParseError):
            incidents_from_alertmanager_payload({"status": "firing"})


if __name__ == "__main__":
    unittest.main()
