from __future__ import annotations

import tempfile
import unittest

from services.alerts.inbox import list_inbox_records, load_inbox_record

try:
    from fastapi.testclient import TestClient
    from services.alerts.inbox_service import create_app
except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent import
    TestClient = None
    create_app = None
    _FASTAPI_IMPORT_ERROR = exc
else:
    _FASTAPI_IMPORT_ERROR = None


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


@unittest.skipIf(_FASTAPI_IMPORT_ERROR is not None, f"fastapi unavailable: {_FASTAPI_IMPORT_ERROR}")
class AlertInboxServiceTest(unittest.TestCase):
    def test_webhook_intake_stores_pending_alert_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(create_app(inbox_root=tmpdir))

            response = client.post("/alerts", json=_crashloop_payload())

            self.assertEqual(response.status_code, 202)
            body = response.json()
            self.assertEqual(body["status"], "accepted")
            self.assertEqual(body["stored"], 1)

            records = list_inbox_records(inbox_root=tmpdir)
            self.assertEqual(len(records), 1)
            record = load_inbox_record(records[0].artifact_dir)
            self.assertEqual(record.status, "pending_investigation")
            self.assertEqual(record.incident_id, "crashloop123")
            self.assertEqual(record.incident_metadata["namespace"], "default")
            self.assertEqual(
                record.raw_payload["alerts"][0]["labels"]["incident_class"],
                "crashloop",
            )


if __name__ == "__main__":
    unittest.main()
