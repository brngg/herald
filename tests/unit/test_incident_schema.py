from __future__ import annotations

from datetime import datetime, timezone
import unittest

from schemas.incident import Incident


class IncidentSchemaTest(unittest.TestCase):
    def test_incident_accepts_valid_payload(self) -> None:
        incident = Incident(
            incident_id="abc123",
            incident_class="bad_config",
            detected_at=datetime.now(tz=timezone.utc),
            source="prometheus",
            raw_context={"status": "firing"},
        )

        self.assertEqual(incident.incident_id, "abc123")
        self.assertEqual(incident.source, "prometheus")

    def test_incident_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            Incident(
                incident_id="abc123",
                incident_class="bad_config",
                detected_at=datetime.now(),
                source="prometheus",
                raw_context={},
            )


if __name__ == "__main__":
    unittest.main()
