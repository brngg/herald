from __future__ import annotations

import unittest

from schemas.intents import CapabilityCatalog
from schemas.observations import ObservationBundle
from services.llm.tasks.reasoner_contract import (
    build_reasoner_prompts,
    reasoner_output_json_schema,
)


class ReasonerLLMTest(unittest.TestCase):
    def test_reasoner_output_json_schema_includes_operation_family_enum(self) -> None:
        schema = reasoner_output_json_schema()
        intent_item = schema["properties"]["intents"]["items"]
        self.assertIn("operation_family", intent_item["properties"])
        self.assertIn("rollout.undo_deployment", intent_item["properties"]["operation_family"]["enum"])

    def test_build_reasoner_prompts_uses_compact_observation_summary(self) -> None:
        observations = ObservationBundle(
            incident_id="incident-123",
            incident_class_hint="crashloop",
            namespace_hint="default",
            source="prometheus",
            alert_context={
                "labels": {"alertname": "HeraldCartserviceCrashLoopBackOff", "severity": "critical"},
                "annotations": {"summary": "cartservice is crash looping"},
            },
            kubernetes={
                "pod_logs": {"status": "succeeded", "output": "Authorization: secret-token\npassword=hunter2"},
                "deployment": {"status": "succeeded"},
                "deployment_summary": {
                    "config_map_refs": ["cartservice-config"],
                    "secret_refs": ["cartservice-secret"],
                    "command_overrides": [["/herald-intentional-crash"]],
                },
                "pod_status_summary": {
                    "waiting_reasons": {"RunContainerError": 1},
                    "termination_reasons": {"StartError": 1},
                    "restart_total": 4,
                },
                "event_summary": {
                    "warning_count": 1,
                    "recent_warnings": [{"reason": "Failed"}],
                },
            },
            prometheus={
                "ready": {"status": "succeeded", "value": 1.0},
                "incident_signal": {"status": "succeeded", "value": 2.0},
            },
            collected_at="2026-03-29T20:00:00+00:00",
            errors=[],
        )
        catalog = CapabilityCatalog(version="phase2-shadow-v1", capabilities=[{"operation_family": "rollout.undo_deployment"}])

        system_prompt, user_prompt = build_reasoner_prompts(
            incident_summary="[critical] crashloop",
            observations=observations,
            incident_class_hint="crashloop",
            capability_catalog=catalog,
        )

        self.assertIn("HERALD Reasoner", system_prompt)
        self.assertIn('"incident_id": "incident-123"', user_prompt)
        self.assertIn('"kubernetes_sections": ["deployment", "deployment_summary", "event_summary", "pod_logs", "pod_status_summary"]', user_prompt)
        self.assertIn('"waiting_reasons": {"RunContainerError": 1}', user_prompt)
        self.assertIn('"config_map_refs": ["cartservice-config"]', user_prompt)
        self.assertIn('"value": 2.0', user_prompt)
        self.assertNotIn("hunter2", user_prompt)
        self.assertNotIn("secret-token", user_prompt)


if __name__ == "__main__":
    unittest.main()
