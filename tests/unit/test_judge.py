from __future__ import annotations

from datetime import datetime, timezone
import unittest

from agents.judge import run_judge_pipeline
from schemas.incident import Incident
from schemas.remediation import RemediationAction
from services.llm.tasks.judge_contract import JudgeLLMResult


def _crashloop_incident() -> Incident:
    return Incident(
        incident_id="judge-abc123",
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
                },
            }
        },
    )


def _crashloop_evidence() -> dict[str, object]:
    return {
        "incident_class": "crashloop",
        "incident_class_normalized": "crashloop",
        "alertname": "HeraldCartserviceCrashLoopBackOff",
        "namespace": "default",
        "severity": "critical",
        "summary": "cartservice is in CrashLoopBackOff",
        "pod": "cartservice-7d6b9f5bb4-abcde",
        "container": "server",
        "labels": {
            "deployment": "cartservice",
            "namespace": "default",
        },
    }


def _bad_config_evidence() -> dict[str, object]:
    return {
        "incident_class": "bad_config",
        "incident_class_normalized": "bad_config",
        "alertname": "HeraldFrontendCartProbeFailed",
        "namespace": "default",
        "severity": "critical",
        "summary": "frontend /cart probe is failing",
    }


def _cpu_incident() -> Incident:
    return Incident(
        incident_id="judge-cpu123",
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
                "annotations": {
                    "summary": "frontend pod is experiencing high CPU",
                },
            }
        },
    )


def _cpu_evidence() -> dict[str, object]:
    return {
        "incident_class": "cpu_saturation",
        "incident_class_normalized": "cpu_saturation",
        "alertname": "HeraldFrontendHighCPU",
        "namespace": "default",
        "severity": "warning",
        "summary": "frontend pod is experiencing high CPU",
        "pod": "frontend-6f7f7b6c8f-aaaaa",
        "labels": {
            "app": "frontend",
            "namespace": "default",
        },
    }


def _network_partition_incident() -> Incident:
    return Incident(
        incident_id="judge-network123",
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
                "annotations": {
                    "summary": "cartservice network traffic is near zero",
                },
            }
        },
    )


def _network_partition_evidence() -> dict[str, object]:
    return {
        "incident_class": "network_partition",
        "incident_class_normalized": "network_partition",
        "alertname": "HeraldCartserviceDependencyFailure",
        "namespace": "default",
        "severity": "critical",
        "summary": "cartservice network traffic is near zero",
        "pod": "cartservice-7d6b9f5bb4-abcde",
        "labels": {
            "namespace": "default",
            "pod": "cartservice-7d6b9f5bb4-abcde",
        },
    }


def _crashloop_actions() -> list[RemediationAction]:
    return [
        RemediationAction(
            action_id="rollout_undo_cartservice",
            action_type="rollout_undo_deployment",
            description="Roll back cartservice Deployment to the previous ReplicaSet.",
            confidence_score=0.9,
            blast_radius_score=0.3,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        ),
        RemediationAction(
            action_id="restart_cartservice",
            action_type="rollout_restart_deployment",
            description="Restart cartservice Deployment to clear transient crashloop state.",
            confidence_score=0.5,
            blast_radius_score=0.2,
            requires_approval=True,
            parameters={"namespace": "default", "deployment": "cartservice"},
        ),
    ]


class JudgeTest(unittest.TestCase):
    def test_judge_passes_supported_crashloop_plan(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=_crashloop_actions(),
            fixer_rationale="Rollback or restart the deployment.",
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertTrue(state["final"])
        self.assertNotIn("judge_llm_reason", state)

    def test_judge_fails_when_no_actions_are_proposed(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("no remediation actions", state["judge_reason"])

    def test_judge_fails_high_blast_radius_rollout(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="undo-cartservice",
                    action_type="rollout_undo_deployment",
                    description="Undo cartservice deployment.",
                    confidence_score=0.9,
                    blast_radius_score=0.9,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "cartservice"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("Blast Radius", state["judge_reason"])

    def test_judge_passes_supported_bad_config_plan(self) -> None:
        state = run_judge_pipeline(
            incident=Incident(
                incident_id="judge-bad-config",
                incident_class="bad_config",
                detected_at=datetime.now(tz=timezone.utc),
                source="prometheus",
                raw_context={},
            ),
            evidence=_bad_config_evidence(),
            incident_summary="[critical] bad_config",
            actions=[
                RemediationAction(
                    action_id="rollout_undo_frontend_bad_config",
                    action_type="rollout_undo_deployment",
                    description="Roll back frontend Deployment to the previous ReplicaSet.",
                    confidence_score=0.92,
                    blast_radius_score=0.3,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "frontend"},
                ),
                RemediationAction(
                    action_id="escalate_frontend_bad_config",
                    action_type="escalate",
                    description="Escalate frontend bad-config incident for deeper investigation.",
                    confidence_score=0.2,
                    blast_radius_score=0.0,
                    requires_approval=True,
                    parameters={"reason": "Bounded frontend config remediation did not appear safe or sufficient."},
                ),
            ],
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertIn("Bad-config plan is bounded", state["judge_reason"])

    def test_judge_fails_bad_config_plan_targeting_other_deployment(self) -> None:
        state = run_judge_pipeline(
            incident=Incident(
                incident_id="judge-bad-config",
                incident_class="bad_config",
                detected_at=datetime.now(tz=timezone.utc),
                source="prometheus",
                raw_context={},
            ),
            evidence=_bad_config_evidence(),
            incident_summary="[critical] bad_config",
            actions=[
                RemediationAction(
                    action_id="rollout_undo_cartservice_bad_config",
                    action_type="rollout_undo_deployment",
                    description="Wrongly target cartservice.",
                    confidence_score=0.9,
                    blast_radius_score=0.3,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "cartservice"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("frontend", state["judge_reason"])

    def test_judge_passes_supported_cpu_plan(self) -> None:
        state = run_judge_pipeline(
            incident=_cpu_incident(),
            evidence=_cpu_evidence(),
            incident_summary="[warning] cpu saturation",
            actions=[
                RemediationAction(
                    action_id="delete_frontend_cpu_stresschaos",
                    action_type="delete_stresschaos",
                    description="Delete the active frontend CPU StressChaos object.",
                    confidence_score=0.9,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "default", "name": "frontend-cpu-saturation"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertIn("CPU saturation plan is bounded", state["judge_reason"])

    def test_judge_fails_cpu_plan_targeting_other_stresschaos(self) -> None:
        state = run_judge_pipeline(
            incident=_cpu_incident(),
            evidence=_cpu_evidence(),
            incident_summary="[warning] cpu saturation",
            actions=[
                RemediationAction(
                    action_id="delete_other_stresschaos",
                    action_type="delete_stresschaos",
                    description="Delete some other StressChaos object.",
                    confidence_score=0.7,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "default", "name": "other-chaos"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("frontend-cpu-saturation", state["judge_reason"])

    def test_judge_passes_supported_network_partition_plan(self) -> None:
        state = run_judge_pipeline(
            incident=_network_partition_incident(),
            evidence=_network_partition_evidence(),
            incident_summary="[critical] network partition",
            actions=[
                RemediationAction(
                    action_id="delete_frontend_cartservice_network_partition",
                    action_type="delete_networkchaos",
                    description="Delete the active frontend-to-cartservice NetworkChaos partition object.",
                    confidence_score=0.88,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={
                        "namespace": "default",
                        "name": "frontend-to-cartservice-partition",
                        "deployment": "cartservice",
                    },
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertIn("Network-partition plan is bounded", state["judge_reason"])

    def test_judge_fails_network_partition_plan_targeting_other_networkchaos(self) -> None:
        state = run_judge_pipeline(
            incident=_network_partition_incident(),
            evidence=_network_partition_evidence(),
            incident_summary="[critical] network partition",
            actions=[
                RemediationAction(
                    action_id="delete_wrong_networkchaos",
                    action_type="delete_networkchaos",
                    description="Delete an unrelated NetworkChaos object.",
                    confidence_score=0.8,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "default", "name": "wrong-partition"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("frontend-to-cartservice-partition", state["judge_reason"])

    def test_judge_fails_unsupported_action_type_for_crashloop(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="scale-cartservice",
                    action_type="scale_deployment",
                    description="Scale cartservice deployment.",
                    confidence_score=0.6,
                    blast_radius_score=0.4,
                    requires_approval=True,
                    parameters={},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("unsupported action_type", state["judge_reason"])

    def test_judge_fails_when_action_missing_approval_gate(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="restart-cartservice",
                    action_type="rollout_restart_deployment",
                    description="Restart cartservice deployment.",
                    confidence_score=0.7,
                    blast_radius_score=0.2,
                    requires_approval=False,
                    parameters={"namespace": "default", "deployment": "cartservice"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("requires_approval=true", state["judge_reason"])

    def test_judge_fails_when_rollout_targets_other_deployment(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="restart-checkoutservice",
                    action_type="rollout_restart_deployment",
                    description="Restart checkoutservice deployment.",
                    confidence_score=0.7,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "checkoutservice"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("incident points to", state["judge_reason"])

    def test_judge_fails_when_rollout_targets_other_namespace(self) -> None:
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="restart-cartservice-other-namespace",
                    action_type="rollout_restart_deployment",
                    description="Restart cartservice deployment in another namespace.",
                    confidence_score=0.7,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "payments", "deployment": "cartservice"},
                )
            ],
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("namespace 'payments'", state["judge_reason"])

    def test_judge_uses_injected_llm(self) -> None:
        class StubJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[RemediationAction],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                self.seen_summary = incident_summary
                self.seen_actions = actions
                return JudgeLLMResult(verdict="pass", reason="LLM-approved plan.")

        llm = StubJudgeLLM()
        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=_crashloop_actions(),
            llm=llm,
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertEqual(len(llm.seen_actions), 2)
        self.assertTrue(llm.seen_summary)
        self.assertEqual(state["judge_llm_reason"], "LLM-approved plan.")

    def test_judge_llm_cannot_bypass_heuristic_fail(self) -> None:
        class PermissiveJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[RemediationAction],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                return JudgeLLMResult(verdict="pass", reason="LLM would approve this.")

        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=[
                RemediationAction(
                    action_id="restart-checkoutservice",
                    action_type="rollout_restart_deployment",
                    description="Restart checkoutservice deployment.",
                    confidence_score=0.7,
                    blast_radius_score=0.2,
                    requires_approval=True,
                    parameters={"namespace": "default", "deployment": "checkoutservice"},
                )
            ],
            llm=PermissiveJudgeLLM(),
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertIn("incident points to", state["judge_reason"])

    def test_judge_uses_llm_fail_reason_when_heuristic_passes(self) -> None:
        class ConservativeJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[RemediationAction],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                return JudgeLLMResult(verdict="fail", reason="LLM rejected the plan.")

        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=_crashloop_actions(),
            llm=ConservativeJudgeLLM(),
        )

        self.assertEqual(state["judge_verdict"], "fail")
        self.assertEqual(state["judge_reason"], "LLM rejected the plan.")
        self.assertEqual(state["judge_llm_reason"], "LLM rejected the plan.")

    def test_judge_preserves_llm_reason_when_heuristic_passes(self) -> None:
        class SupportiveJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[RemediationAction],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                return JudgeLLMResult(
                    verdict="pass",
                    reason="Rollback is the best bounded option for this crashloop incident.",
                )

        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=_crashloop_actions(),
            llm=SupportiveJudgeLLM(),
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertEqual(
            state["judge_reason"],
            "Crashloop plan is bounded, reversible, approval-gated, and limited to supported v0 remediation actions.",
        )
        self.assertEqual(
            state["judge_llm_reason"],
            "Rollback is the best bounded option for this crashloop incident.",
        )

    def test_judge_falls_back_when_llm_raises(self) -> None:
        class FailingJudgeLLM:
            def evaluate(
                self,
                *,
                incident_summary: str,
                evidence: dict[str, object],
                actions: list[RemediationAction],
                fixer_rationale: str | None,
            ) -> JudgeLLMResult:
                raise RuntimeError("network timeout")

        state = run_judge_pipeline(
            incident=_crashloop_incident(),
            evidence=_crashloop_evidence(),
            incident_summary="[critical] crashloop",
            actions=_crashloop_actions(),
            llm=FailingJudgeLLM(),
        )

        self.assertEqual(state["judge_verdict"], "pass")
        self.assertIn("falling back to heuristic", state["errors"][0])


if __name__ == "__main__":
    unittest.main()
