from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from schemas.critic import CriticOutput, CritiqueCandidate, PolicyCheckResult
from schemas.execution_plan import ExecutionPlan, ExecutionPlanStep, SynthesisOutput
from schemas.incident import Incident
from schemas.intents import OperationIntent, ResourceTarget
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from schemas.verification import VerificationCheck, VerificationPlan
from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient
from services.recovery.verification_engine import (
    build_shadow_verification_plan,
    run_verification,
)


def _incident_class_hint() -> ObservationBundle:
    return ObservationBundle(
        incident_id="incident-123",
        incident_class_hint="crashloop",
        namespace_hint="default",
        source="prometheus",
        alert_context={"labels": {"namespace": "default"}},
        kubernetes={},
        prometheus={},
        collected_at="2026-03-29T20:00:00+00:00",
    )


def _approved_action(action_type: str, *, namespace: str = "default", name: str = "cartservice") -> RemediationAction:
    parameters = {"namespace": namespace}
    if action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        parameters["deployment"] = name
    elif action_type == "scale_deployment":
        parameters["deployment"] = name
        parameters["replicas"] = 2
    elif action_type in {"delete_stresschaos", "delete_networkchaos"}:
        parameters["name"] = name
    elif action_type == "escalate":
        parameters["reason"] = "human review"
    return RemediationAction(
        action_id=f"action-{action_type}",
        action_type=action_type,
        description=f"Shadow action for {action_type}.",
        confidence_score=0.9,
        blast_radius_score=0.2,
        requires_approval=True,
        parameters=parameters,
    )


def _synthesis_output(action_type: str, *, namespace: str = "default", name: str = "cartservice") -> SynthesisOutput:
    target_kind = "Deployment"
    operation_family = "rollout.undo_deployment"
    if action_type == "rollout_restart_deployment":
        operation_family = "rollout.restart_deployment"
    elif action_type == "scale_deployment":
        operation_family = "scale.deployment"
    elif action_type == "delete_stresschaos":
        operation_family = "chaos.delete_stresschaos"
        target_kind = "StressChaos"
    elif action_type == "delete_networkchaos":
        operation_family = "chaos.delete_networkchaos"
        target_kind = "NetworkChaos"
    elif action_type == "escalate":
        operation_family = "escalate.human_review"
        target_kind = "Incident"

    plan_steps = []
    if action_type != "escalate":
        plan_steps = [
            ExecutionPlanStep(
                step_id="plan:step-1",
                tool_name=action_type,
                command=["kubectl", action_type.replace("_", " ")],
                expected_effect="Run the bounded remediation step.",
                reversible=True,
                verification_hints={"post_check": "crashloop"},
            )
        ]

    return SynthesisOutput(
        summary="Shadow synthesis output.",
        plans=[
            ExecutionPlan(
                intent_id=f"intent-{action_type}",
                operation_family=operation_family,
                target=ResourceTarget(namespace=namespace, kind=target_kind, name=name if action_type != "escalate" else "incident-1"),
                summary="Shadow execution plan.",
                steps=plan_steps,
                allowed_tool_names=[action_type] if action_type != "escalate" else [],
                blast_radius_score=0.2,
                requires_approval=True,
                rollback_outline={},
            )
        ],
        unsupported_intents=[],
        warnings=[],
    )


class VerificationEngineTest(unittest.TestCase):
    def test_build_shadow_verification_plan_prefers_synthesized_match(self) -> None:
        approved_action = _approved_action("rollout_undo_deployment")
        plan = build_shadow_verification_plan(
            approved_action,
            _synthesis_output("rollout_undo_deployment"),
            _incident_class_hint(),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.action_id, approved_action.action_id)
        self.assertEqual(
            [check.check_type for check in plan.checks],
            [
                "kubernetes_rollout_status",
                "prometheus_readiness_positive",
                "prometheus_crashloop_zero",
            ],
        )

    def test_build_shadow_verification_plan_falls_back_when_no_match_exists(self) -> None:
        approved_action = _approved_action("delete_stresschaos", name="frontend-cpu-saturation")
        plan = build_shadow_verification_plan(
            approved_action,
            _synthesis_output("delete_networkchaos"),
            _incident_class_hint(),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.warnings)
        self.assertEqual(plan.checks[0].check_type, "kubernetes_resource_absent")

    def test_build_shadow_verification_plan_returns_none_for_escalate(self) -> None:
        self.assertIsNone(build_shadow_verification_plan(_approved_action("escalate"), None, _incident_class_hint()))

    def test_run_verification_only_treats_not_found_as_resource_absent(self) -> None:
        plan = VerificationPlan(
            verification_id="verify-resource-error",
            action_id="action-resource-error",
            action_type="delete_stresschaos",
            target=ResourceTarget(namespace="default", kind="StressChaos", name="stresschaos-1"),
            summary="Verify resource absence handles lookup errors safely.",
            checks=[
                VerificationCheck(
                    check_id="check-resource-error",
                    check_type="kubernetes_resource_absent",
                    summary="Resource absent",
                    parameters={"namespace": "default", "kind": "StressChaos", "name": "stresschaos-1"},
                )
            ],
        )

        kubernetes = Mock(spec=KubernetesClient)
        kubernetes.get_stresschaos.return_value = {
            "returncode": 1,
            "stdout": "",
            "stderr": "Forbidden",
        }
        prometheus = Mock(spec=PrometheusClient)
        prometheus.network_partition_receive_threshold = 100.0

        result = run_verification(plan, prometheus=prometheus, kubernetes=kubernetes)

        self.assertEqual(result.status, "unrecovered")
        self.assertFalse(result.check_results[0].passed)

    def test_run_verification_reuses_one_prometheus_post_check_per_plan(self) -> None:
        plan = VerificationPlan(
            verification_id="verify-shared-post-check",
            action_id="action-shared-post-check",
            action_type="rollout_undo_deployment",
            target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
            summary="Verify one shared post-check payload.",
            checks=[
                VerificationCheck(
                    check_id="check-readiness",
                    check_type="prometheus_readiness_positive",
                    summary="Readiness positive",
                    parameters={
                        "namespace": "default",
                        "deployment": "cartservice",
                        "incident_class_hint": "crashloop",
                    },
                ),
                VerificationCheck(
                    check_id="check-crashloop",
                    check_type="prometheus_crashloop_zero",
                    summary="Crashloop zero",
                    parameters={"namespace": "default", "deployment": "cartservice"},
                ),
            ],
        )

        kubernetes = Mock(spec=KubernetesClient)
        prometheus = Mock(spec=PrometheusClient)
        prometheus.network_partition_receive_threshold = 100.0
        prometheus.post_check_crashloop.return_value = {
            "ready_count": 1.0,
            "crashloop_count": 0.0,
        }

        result = run_verification(plan, prometheus=prometheus, kubernetes=kubernetes)

        self.assertEqual(result.status, "passed")
        self.assertEqual(prometheus.post_check_crashloop.call_count, 1)
        self.assertTrue(all(check_result.passed for check_result in result.check_results))

    def test_run_verification_covers_all_bounded_check_types(self) -> None:
        cases = [
            (
                "kubernetes_rollout_status",
                VerificationPlan(
                    verification_id="verify-1",
                    action_id="action-1",
                    action_type="rollout_undo_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    summary="Verify rollout status.",
                    checks=[
                        VerificationCheck(
                            check_id="check-1",
                            check_type="kubernetes_rollout_status",
                            summary="Rollout status",
                            parameters={"namespace": "default", "deployment": "cartservice", "timeout_seconds": 1},
                        )
                    ],
                    warnings=[],
                ),
                {"wait_for_rollout_deployment": {"status": "succeeded"}},
                {},
            ),
            (
                "kubernetes_resource_absent",
                VerificationPlan(
                    verification_id="verify-2",
                    action_id="action-2",
                    action_type="delete_stresschaos",
                    target=ResourceTarget(namespace="default", kind="StressChaos", name="stresschaos-1"),
                    summary="Verify resource absent.",
                    checks=[
                        VerificationCheck(
                            check_id="check-2",
                            check_type="kubernetes_resource_absent",
                            summary="Resource absent",
                            parameters={"namespace": "default", "kind": "StressChaos", "name": "stresschaos-1"},
                        )
                    ],
                    warnings=[],
                ),
                {"get_stresschaos": {"returncode": 1, "stdout": "", "stderr": "Not Found"}},
                {},
            ),
            (
                "prometheus_readiness_positive",
                VerificationPlan(
                    verification_id="verify-3",
                    action_id="action-3",
                    action_type="rollout_undo_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    summary="Verify readiness.",
                    checks=[
                        VerificationCheck(
                            check_id="check-3",
                            check_type="prometheus_readiness_positive",
                            summary="Readiness positive",
                            parameters={"namespace": "default", "deployment": "cartservice"},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_crashloop": {"ready_count": 1.0, "crashloop_count": 0.0}},
            ),
            (
                "prometheus_ready_count_at_least",
                VerificationPlan(
                    verification_id="verify-3b",
                    action_id="action-3b",
                    action_type="scale_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="frontend"),
                    summary="Verify scaled readiness target.",
                    checks=[
                        VerificationCheck(
                            check_id="check-3b",
                            check_type="prometheus_ready_count_at_least",
                            summary="Ready count target",
                            parameters={"namespace": "default", "deployment": "frontend", "min_ready_count": 2},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_deployment_readiness_target": {"ready_count": 2.0, "min_ready_count": 2}},
            ),
            (
                "prometheus_crashloop_zero",
                VerificationPlan(
                    verification_id="verify-4",
                    action_id="action-4",
                    action_type="rollout_restart_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="cartservice"),
                    summary="Verify crashloop zero.",
                    checks=[
                        VerificationCheck(
                            check_id="check-4",
                            check_type="prometheus_crashloop_zero",
                            summary="Crashloop zero",
                            parameters={"namespace": "default", "deployment": "cartservice"},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_crashloop": {"ready_count": 1.0, "crashloop_count": 0.0}},
            ),
            (
                "prometheus_probe_positive",
                VerificationPlan(
                    verification_id="verify-5",
                    action_id="action-5",
                    action_type="rollout_undo_deployment",
                    target=ResourceTarget(namespace="default", kind="Deployment", name="frontend"),
                    summary="Verify probe positive.",
                    checks=[
                        VerificationCheck(
                            check_id="check-5",
                            check_type="prometheus_probe_positive",
                            summary="Probe positive",
                            parameters={"namespace": "default", "deployment": "frontend"},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_bad_config": {"probe_success": 1.0, "ready_count": 1.0}},
            ),
            (
                "prometheus_cpu_below_threshold",
                VerificationPlan(
                    verification_id="verify-6",
                    action_id="action-6",
                    action_type="delete_stresschaos",
                    target=ResourceTarget(namespace="default", kind="StressChaos", name="stresschaos-1"),
                    summary="Verify cpu below threshold.",
                    checks=[
                        VerificationCheck(
                            check_id="check-6",
                            check_type="prometheus_cpu_below_threshold",
                            summary="CPU below threshold",
                            parameters={"namespace": "default", "deployment": "frontend"},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_cpu_saturation": {"cpu_usage": 0.02, "ready_count": 1.0}},
            ),
            (
                "prometheus_network_receive_above_threshold",
                VerificationPlan(
                    verification_id="verify-7",
                    action_id="action-7",
                    action_type="delete_networkchaos",
                    target=ResourceTarget(namespace="default", kind="NetworkChaos", name="partition-1"),
                    summary="Verify network receive above threshold.",
                    checks=[
                        VerificationCheck(
                            check_id="check-7",
                            check_type="prometheus_network_receive_above_threshold",
                            summary="Network above threshold",
                            parameters={"namespace": "default", "deployment": "cartservice", "threshold": 100.0},
                        )
                    ],
                    warnings=[],
                ),
                {},
                {"post_check_network_partition": {"network_receive_rate": 150.0, "ready_count": 1.0}},
            ),
        ]

        for check_type, plan, kubernetes_attrs, prometheus_attrs in cases:
            with self.subTest(check_type=check_type):
                kubernetes = Mock(spec=KubernetesClient)
                prometheus = Mock(spec=PrometheusClient)
                prometheus.network_partition_receive_threshold = 100.0
                for name, value in kubernetes_attrs.items():
                    getattr(kubernetes, name).return_value = value
                for name, value in prometheus_attrs.items():
                    getattr(prometheus, name).return_value = value

                result = run_verification(plan, prometheus=prometheus, kubernetes=kubernetes)

                self.assertEqual(result.status, "passed")
                self.assertTrue(result.check_results[0].passed)

    def test_build_shadow_verification_plan_supports_scale_deployment(self) -> None:
        approved_action = _approved_action("scale_deployment", name="frontend")
        plan = build_shadow_verification_plan(
            approved_action,
            _synthesis_output("scale_deployment", name="frontend"),
            _incident_class_hint(),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            [check.check_type for check in plan.checks],
            [
                "kubernetes_rollout_status",
                "prometheus_ready_count_at_least",
            ],
        )


if __name__ == "__main__":
    unittest.main()
