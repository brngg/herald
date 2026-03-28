from __future__ import annotations

import tempfile
import unittest

from services.alert_inbox import (
    claim_inbox_record,
    list_actionable_inbox_records,
    load_inbox_record,
    store_pending_alerts,
    update_inbox_record,
)


def _crashloop_payload(*, fingerprint: str = "crashloop123") -> dict[str, object]:
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
                "fingerprint": fingerprint,
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


class AlertInboxTest(unittest.TestCase):
    def test_claim_inbox_record_marks_record_with_claimer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            record = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]

            claimed = claim_inbox_record(record.artifact_dir, claimer_id="watcher-1")

            self.assertIsNotNone(claimed)
            reloaded = load_inbox_record(record.artifact_dir)
            self.assertEqual(reloaded.claimed_by, "watcher-1")
            self.assertIsNotNone(reloaded.claimed_at)

    def test_actionable_records_prioritize_pending_execution_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pending = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]
            resumable = store_pending_alerts(
                _crashloop_payload(fingerprint="crashloop456"),
                inbox_root=tmpdir,
            )[0]
            update_inbox_record(
                resumable.artifact_dir,
                status="pending_execution_approval",
                expected_statuses=("pending_investigation",),
            )

            actionable = list_actionable_inbox_records(inbox_root=tmpdir)

            self.assertEqual(
                [record.incident_id for record in actionable],
                ["crashloop456", "crashloop123"],
            )


if __name__ == "__main__":
    unittest.main()
