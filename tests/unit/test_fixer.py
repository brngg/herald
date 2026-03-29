from __future__ import annotations

from datetime import datetime, timezone
import unittest

from agents.fixer import (
    _serialize_results_for_output,
    build_incident_summary_node,
    extract_evidence_node,
    finalize_plan_node,
    initial_fixer_state,
    propose_actions_node,
    run_fixer_for_alertmanager_payload,
    run_fixer_pipeline,
)
from schemas.incident import Incident
from services.fixer_llm import FixerLLMResult
from tests.unit.test_alertmanager_ingest import _sample_payload


def _crashloop_incident() -> Incident:
    return Incident(
        incident_id="abc123",
        incident_class="crashloop",
        detected_at=datetime.now(tz=timezone.utc),
        source="prometheus",
        raw_context={
            "alert": {
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
                    "description": "Pod cartservice-... is crash looping in namespace default.",
                },
            }
        },
    )


def _cpu_incident() -> Incident:
    return Incident(
        incident_id="cpu123",
        incident_class="cpu_saturation",
        detected_at=datetime.now(tz=timezone.utc),
        source="prometheus",
        raw_context={
            "alert": {
                "labels": {
                    "alertname": "HeraldFrontendHighCPU",
                    "incident_class": "cpu_saturation",
                    "namespace": "default",
                    "pod": "frontend-6f7f7b6c8f-aaaaa",
                    "severity": "warning",
                },
                "annotations": {"summary": "frontend pod is experiencing high CPU"},
            }
        },
    )


def _bad_config_incident() -> Incident:
    return Incident(
        incident_id="badcfg123",
        incident_class="bad_config",
        detected_at=datetime.now(tz=timezone.utc),
        source="prometheus",
        raw_context={
            "alert": {
                "labels": {
                    "alertname": "HeraldFrontendCartProbeFailed",
                    "incident_class": "bad_config",
                    "namespace": "default",
                    "pod": "frontend-6f7f7b6c8f-aaaaa",
                    "severity": "critical",
                },
                "annotations": {"summary": "frontend /cart probe is failing"},
            }
        },
    )


def _network_partition_incident() -> Incident:
    return Incident(
        incident_id="network123",
        incident_class="network_partition",
        detected_at=datetime.now(tz=timezone.utc),
        source="prometheus",
        raw_context={
            "alert": {
                "labels": {
                    "alertname": "HeraldCartserviceDependencyFailure",
                    "incident_class": "network_partition",
                    "namespace": "default",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                    "severity": "critical",
                },
                "annotations": {"summary": "cartservice network traffic is near zero"},
            }
        },
    )


class FixerTest(unittest.TestCase):
    def test_crashloop_proposes_actions_for_incident_class_crashloop(self) -> None:
        state = initial_fixer_state(_crashloop_incident())
        state.update(extract_evidence_node(state))
        state.update(build_incident_summary_node(state))
        state.update(propose_actions_node(state))
        state.update(finalize_plan_node(state))

        self.assertGreaterEqual(len(state["actions"]), 1)
        self.assertIn("rollout_undo_deployment", state["raw_plan"])
        # Keep operator-facing strings ASCII-friendly.
        self.assertNotIn("—", state["incident_summary"])

    def test_unsupported_incident_class_is_surfaced_in_raw_plan(self) -> None:
        state = initial_fixer_state(_cpu_incident())
        state.update(extract_evidence_node(state))
        state.update(build_incident_summary_node(state))
        state.update(propose_actions_node(state))
        state.update(finalize_plan_node(state))

        self.assertEqual(len(state["actions"]), 2)
        self.assertEqual(state["actions"][0].action_type, "delete_stresschaos")
        self.assertEqual(state["actions"][0].parameters["name"], "frontend-cpu-saturation")
        self.assertEqual(state["actions"][1].action_type, "escalate")
        self.assertNotIn("Errors:", state["raw_plan"])

    def test_run_fixer_for_alertmanager_payload_ingests_and_runs(self) -> None:
        results = run_fixer_for_alertmanager_payload(_sample_payload())

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["incident"].incident_class, "bad_config")
        self.assertEqual(results[0]["actions"][0].action_type, "rollout_undo_deployment")
        self.assertEqual(results[0]["actions"][0].parameters["deployment"], "frontend")
        self.assertEqual(results[0]["actions"][1].action_type, "escalate")

    def test_bad_config_proposes_frontend_rollback_actions(self) -> None:
        state = initial_fixer_state(_bad_config_incident())
        state.update(extract_evidence_node(state))
        state.update(build_incident_summary_node(state))
        state.update(propose_actions_node(state))
        state.update(finalize_plan_node(state))

        self.assertEqual(len(state["actions"]), 2)
        self.assertEqual(state["actions"][0].action_id, "rollout_undo_frontend_bad_config")
        self.assertEqual(state["actions"][0].action_type, "rollout_undo_deployment")
        self.assertEqual(state["actions"][0].parameters["deployment"], "frontend")
        self.assertEqual(state["actions"][1].action_type, "escalate")
        self.assertIn("rollout_undo_frontend_bad_config", state["raw_plan"])

    def test_network_partition_proposes_networkchaos_delete_actions(self) -> None:
        state = initial_fixer_state(_network_partition_incident())
        state.update(extract_evidence_node(state))
        state.update(build_incident_summary_node(state))
        state.update(propose_actions_node(state))
        state.update(finalize_plan_node(state))

        self.assertEqual(len(state["actions"]), 2)
        self.assertEqual(state["actions"][0].action_id, "delete_frontend_cartservice_network_partition")
        self.assertEqual(state["actions"][0].action_type, "delete_networkchaos")
        self.assertEqual(state["actions"][0].parameters["name"], "frontend-to-cartservice-partition")
        self.assertEqual(state["actions"][0].parameters["deployment"], "cartservice")
        self.assertEqual(state["actions"][1].action_type, "escalate")
        self.assertIn("delete_frontend_cartservice_network_partition", state["raw_plan"])

    def test_run_fixer_pipeline_uses_injected_llm(self) -> None:
        class StubLLM:
            def propose(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
            ) -> FixerLLMResult:
                self.seen_summary = incident_summary
                self.seen_evidence = evidence
                return FixerLLMResult(
                    rationale="Structured output from test LLM.",
                    actions=[],
                )

        llm = StubLLM()
        state = run_fixer_pipeline(_crashloop_incident(), llm=llm)

        self.assertTrue(llm.seen_summary)
        self.assertEqual(str(llm.seen_evidence["incident_class"]), "crashloop")
        self.assertIn("Rationale:", state["raw_plan"])

    def test_cli_output_excludes_raw_context_by_default(self) -> None:
        state = run_fixer_pipeline(_crashloop_incident())

        output = _serialize_results_for_output([state], include_debug_context=False)

        self.assertNotIn("raw_context", output[0]["incident"])
        self.assertNotIn("evidence", output[0])

    def test_cli_output_can_include_debug_context_explicitly(self) -> None:
        state = run_fixer_pipeline(_crashloop_incident())

        output = _serialize_results_for_output([state], include_debug_context=True)

        self.assertIn("raw_context", output[0]["incident"])
        self.assertIn("evidence", output[0])


if __name__ == "__main__":
    unittest.main()
