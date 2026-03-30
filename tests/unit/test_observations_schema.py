from __future__ import annotations

import unittest

from schemas.observations import ObservationBundle, observation_bundle_from_dict


class ObservationBundleSchemaTest(unittest.TestCase):
    def test_observation_bundle_from_dict_accepts_valid_payload(self) -> None:
        bundle = observation_bundle_from_dict(
            {
                "incident_id": "incident-123",
                "incident_class_hint": "crashloop",
                "namespace_hint": "default",
                "source": "prometheus",
                "alert_context": {"labels": {"namespace": "default"}},
                "kubernetes": {"pods": {"status": "succeeded"}},
                "prometheus": {"ready": {"status": "succeeded", "value": 1.0}},
                "collected_at": "2026-03-29T20:00:00+00:00",
                "errors": [],
            }
        )

        self.assertIsInstance(bundle, ObservationBundle)
        self.assertEqual(bundle.incident_id, "incident-123")
        self.assertEqual(bundle.namespace_hint, "default")
        self.assertEqual(bundle.prometheus["ready"]["value"], 1.0)

    def test_observation_bundle_rejects_empty_incident_id(self) -> None:
        with self.assertRaises(ValueError):
            ObservationBundle(
                incident_id="",
                incident_class_hint="crashloop",
                namespace_hint="default",
                source="prometheus",
                alert_context={},
                kubernetes={},
                prometheus={},
                collected_at="2026-03-29T20:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
