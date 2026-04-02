from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from schemas.execution import VALID_EXECUTION_ACTION_TYPES
from schemas.execution_plan import SynthesisOutput
from schemas.intents import OperationIntent
from schemas.observations import ObservationBundle
from schemas.remediation import RemediationAction
from schemas.intents import ResourceTarget
from schemas.verification import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationPlan,
    VerificationResultV2,
)
from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import PrometheusClient


def build_shadow_verification_plan(
    approved_action: RemediationAction,
    synthesis_output: SynthesisOutput | None,
    observation_bundle: ObservationBundle | None,
) -> VerificationPlan | None:
    if approved_action.action_type not in VALID_EXECUTION_ACTION_TYPES:
        return None
    if approved_action.action_type == "escalate":
        return None

    matched_plan = _match_synthesized_plan(approved_action, synthesis_output)
    target_namespace = str(approved_action.parameters.get("namespace", ""))
    target_name = _action_target_name(approved_action)
    if not target_namespace or not target_name:
        return None

    incident_class_hint = observation_bundle.incident_class_hint if observation_bundle is not None else None
    desired_replicas = approved_action.parameters.get("replicas")
    if not isinstance(desired_replicas, int) or isinstance(desired_replicas, bool):
        desired_replicas = None
    verification_checks = _build_checks(
        action_type=approved_action.action_type,
        namespace=target_namespace,
        target_name=target_name,
        incident_class_hint=incident_class_hint,
        synthesized_plan=matched_plan,
        desired_replicas=desired_replicas,
    )
    warnings: list[str] = []
    if synthesis_output is None or matched_plan is None:
        warnings.append("Shadow verification plan fell back to the approved v1 action because no synthesized match was available.")

    return VerificationPlan(
        verification_id=f"verify-{approved_action.action_id}",
        action_id=approved_action.action_id,
        action_type=approved_action.action_type,
        target=_verification_target(approved_action),
        summary=f"Shadow verification plan for {approved_action.action_type} on {target_name}.",
        checks=verification_checks,
        warnings=warnings,
        rollback_warning=None,
    )


def run_verification(
    plan: VerificationPlan,
    *,
    prometheus: PrometheusClient,
    kubernetes: KubernetesClient,
) -> VerificationResultV2:
    check_results: list[VerificationCheckResult] = []
    warnings = list(plan.warnings)
    status = "passed"
    shared_prometheus_post_check = _shared_prometheus_post_check(plan, prometheus=prometheus)

    for check in plan.checks:
        result = _run_check(
            check,
            plan=plan,
            prometheus=prometheus,
            kubernetes=kubernetes,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        check_results.append(result)
        if not result.passed:
            status = "unrecovered"

    if plan.rollback_warning is not None:
        warnings.append(plan.rollback_warning)

    if not plan.checks:
        status = "not_run"
        warnings.append("Verification plan did not include runnable checks.")

    if any(not result.passed for result in check_results):
        status = "unrecovered"

    summary = (
        f"Verification {status} for {plan.action_type} on {plan.target.name or plan.target.kind} "
        f"with {len(check_results)} check(s)."
    )
    return VerificationResultV2(
        verification_id=plan.verification_id,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        plan=plan,
        check_results=check_results,
        warnings=_dedupe_strings(warnings),
    )


def _run_check(
    check: VerificationCheck,
    *,
    plan: VerificationPlan,
    prometheus: PrometheusClient,
    kubernetes: KubernetesClient,
    shared_prometheus_post_check: dict[str, Any] | None = None,
) -> VerificationCheckResult:
    params = dict(check.parameters)
    if check.check_type == "kubernetes_rollout_status":
        timeout_seconds = int(params.get("timeout_seconds", 60))
        rollout_status = kubernetes.wait_for_rollout_deployment(
            namespace=str(params["namespace"]),
            deployment=str(params["deployment"]),
            timeout_seconds=timeout_seconds,
        )
        passed = rollout_status["status"] == "succeeded"
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="Kubernetes rollout reached a succeeded status." if passed else "Kubernetes rollout did not succeed.",
            observed_value=rollout_status["status"],
            expected_value="succeeded",
        )

    if check.check_type == "kubernetes_resource_absent":
        kind = str(params["kind"])
        namespace = str(params["namespace"])
        name = str(params["name"])
        resource_result = _resource_lookup(kubernetes, kind=kind, namespace=namespace, name=name)
        absent = _resource_is_absent(resource_result)
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=absent,
            reason="The Kubernetes resource was absent." if absent else "The Kubernetes resource still existed.",
            observed_value=resource_result,
            expected_value="absent",
        )

    if check.check_type == "prometheus_readiness_positive":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        ready_count = _observed_ready_count(result)
        passed = ready_count > 0
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="Readiness is positive." if passed else "Readiness did not become positive.",
            observed_value=ready_count,
            expected_value="> 0",
        )

    if check.check_type == "prometheus_ready_count_at_least":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        ready_count = _observed_ready_count(result)
        minimum = float(check.parameters.get("min_ready_count", 1.0) or 1.0)
        passed = ready_count >= minimum
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason=(
                "Ready replica count met the minimum target."
                if passed
                else "Ready replica count did not meet the minimum target."
            ),
            observed_value=ready_count,
            expected_value=f">= {minimum}",
        )

    if check.check_type == "prometheus_crashloop_zero":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        crashloop_count = float(result.get("crashloop_count", 0.0) or 0.0)
        passed = crashloop_count == 0.0
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="Crashloop count is zero." if passed else "Crashloop count remains non-zero.",
            observed_value=crashloop_count,
            expected_value=0.0,
        )

    if check.check_type == "prometheus_probe_positive":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        probe_success = _observed_probe_success(result)
        passed = probe_success is not None and probe_success > 0
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="Probe success is positive." if passed else "Probe success is not positive.",
            observed_value=probe_success,
            expected_value="> 0",
        )

    if check.check_type == "prometheus_cpu_below_threshold":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        cpu_usage = float(result.get("cpu_usage", 0.0) or 0.0)
        passed = cpu_usage <= 0.05
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="CPU usage is below threshold." if passed else "CPU usage remains above threshold.",
            observed_value=cpu_usage,
            expected_value="<= 0.05",
        )

    if check.check_type == "prometheus_network_receive_above_threshold":
        result = _prometheus_check_result(
            plan,
            check=check,
            prometheus=prometheus,
            shared_prometheus_post_check=shared_prometheus_post_check,
        )
        network_receive_rate = float(result.get("network_receive_rate", 0.0) or 0.0)
        threshold = float(params.get("threshold", prometheus.network_partition_receive_threshold))
        passed = network_receive_rate >= threshold
        return VerificationCheckResult(
            check_id=check.check_id,
            check_type=check.check_type,
            passed=passed,
            reason="Network receive rate is above threshold." if passed else "Network receive rate remains below threshold.",
            observed_value=network_receive_rate,
            expected_value=f">= {threshold}",
        )

    raise ValueError(f"unsupported verification check_type: {check.check_type}")


def _build_checks(
    *,
    action_type: str,
    namespace: str,
    target_name: str,
    incident_class_hint: str | None,
    synthesized_plan: Any | None,
    desired_replicas: int | None,
) -> list[VerificationCheck]:
    checks: list[VerificationCheck] = []
    verification_hints = {}
    if synthesized_plan is not None and getattr(synthesized_plan, "steps", None):
        verification_hints = dict(synthesized_plan.steps[0].verification_hints)

    if action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        checks.append(
            VerificationCheck(
                check_id="kubernetes-rollout-status",
                check_type="kubernetes_rollout_status",
                summary="Verify the rollout reached a succeeded status.",
                parameters={
                    "namespace": namespace,
                    "deployment": target_name,
                    "timeout_seconds": 60,
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-readiness-positive",
                check_type="prometheus_readiness_positive",
                summary="Verify readiness became positive after the rollout.",
                parameters={
                    "namespace": namespace,
                    "deployment": target_name,
                    "incident_class_hint": incident_class_hint,
                },
            )
        )
        post_check = str(verification_hints.get("post_check") or incident_class_hint or "")
        if post_check == "crashloop":
            checks.append(
                VerificationCheck(
                    check_id="prometheus-crashloop-zero",
                    check_type="prometheus_crashloop_zero",
                    summary="Verify crashloop count dropped to zero.",
                    parameters={
                        "namespace": namespace,
                        "deployment": target_name,
                    },
                )
            )
        if post_check == "bad_config":
            checks.append(
                VerificationCheck(
                    check_id="prometheus-probe-positive",
                    check_type="prometheus_probe_positive",
                    summary="Verify the frontend probe became positive.",
                    parameters={
                        "namespace": namespace,
                        "deployment": target_name,
                    },
                )
            )
        return checks

    if action_type == "scale_deployment":
        min_ready_count = desired_replicas or 1
        if synthesized_plan is not None and getattr(synthesized_plan, "steps", None):
            hinted = synthesized_plan.steps[0].verification_hints.get("min_ready_count")
            if isinstance(hinted, int) and not isinstance(hinted, bool):
                min_ready_count = hinted
        checks.append(
            VerificationCheck(
                check_id="kubernetes-rollout-status",
                check_type="kubernetes_rollout_status",
                summary="Verify the scaled rollout reached a succeeded status.",
                parameters={
                    "namespace": namespace,
                    "deployment": target_name,
                    "timeout_seconds": 60,
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-ready-count-at-least",
                check_type="prometheus_ready_count_at_least",
                summary="Verify ready replica count reached the approved bounded target.",
                parameters={
                    "namespace": namespace,
                    "deployment": target_name,
                    "min_ready_count": min_ready_count,
                },
            )
        )
        return checks

    if action_type == "delete_stresschaos":
        checks.append(
            VerificationCheck(
                check_id="kubernetes-resource-absent",
                check_type="kubernetes_resource_absent",
                summary="Verify the StressChaos resource is absent.",
                parameters={
                    "namespace": namespace,
                    "kind": "StressChaos",
                    "name": target_name,
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-readiness-positive",
                check_type="prometheus_readiness_positive",
                summary="Verify readiness became positive after deleting StressChaos.",
                parameters={
                    "namespace": namespace,
                    "deployment": _default_deployment_for_class(incident_class_hint),
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-cpu-below-threshold",
                check_type="prometheus_cpu_below_threshold",
                summary="Verify CPU usage dropped below the threshold.",
                parameters={
                    "namespace": namespace,
                    "deployment": _default_deployment_for_class(incident_class_hint),
                },
            )
        )
        return checks

    if action_type == "delete_networkchaos":
        checks.append(
            VerificationCheck(
                check_id="kubernetes-resource-absent",
                check_type="kubernetes_resource_absent",
                summary="Verify the NetworkChaos resource is absent.",
                parameters={
                    "namespace": namespace,
                    "kind": "NetworkChaos",
                    "name": target_name,
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-readiness-positive",
                check_type="prometheus_readiness_positive",
                summary="Verify readiness became positive after deleting NetworkChaos.",
                parameters={
                    "namespace": namespace,
                    "deployment": _default_deployment_for_class(incident_class_hint),
                },
            )
        )
        checks.append(
            VerificationCheck(
                check_id="prometheus-network-receive-above-threshold",
                check_type="prometheus_network_receive_above_threshold",
                summary="Verify network receive rate rose above the partition threshold.",
                parameters={
                    "namespace": namespace,
                    "deployment": _default_deployment_for_class(incident_class_hint),
                },
            )
        )
        return checks

    raise ValueError(f"unsupported action_type for verification plan: {action_type}")


def _match_synthesized_plan(
    approved_action: RemediationAction,
    synthesis_output: SynthesisOutput | None,
) -> Any | None:
    if synthesis_output is None:
        return None
    target_name = _action_target_name(approved_action)
    if not target_name:
        return None
    for plan in synthesis_output.plans:
        if plan.operation_family != _operation_family_for_action(approved_action.action_type):
            continue
        if plan.target.namespace != approved_action.parameters.get("namespace"):
            continue
        if plan.target.name != target_name:
            continue
        return plan
    return None


def _prometheus_post_check(
    plan: VerificationPlan,
    *,
    check: VerificationCheck,
    prometheus: PrometheusClient,
) -> dict[str, Any]:
    namespace = str(plan.target.namespace or "")
    deployment = str(plan.target.name or "")
    if plan.action_type in {"rollout_undo_deployment", "rollout_restart_deployment"}:
        incident_class_hint = str(check.parameters.get("incident_class_hint") or "")
        if check.check_type == "prometheus_probe_positive" or incident_class_hint == "bad_config":
            return prometheus.post_check_bad_config(namespace=namespace, deployment=deployment)
        return prometheus.post_check_crashloop(namespace=namespace, deployment=deployment)
    if plan.action_type == "scale_deployment":
        min_ready_count = int(check.parameters.get("min_ready_count") or 1)
        return prometheus.post_check_deployment_readiness_target(
            namespace=namespace,
            deployment=deployment,
            min_ready_count=min_ready_count,
        )
    if plan.action_type == "delete_stresschaos":
        return prometheus.post_check_cpu_saturation(namespace=namespace, deployment=deployment)
    if plan.action_type == "delete_networkchaos":
        return prometheus.post_check_network_partition(namespace=namespace, deployment=deployment)
    return {}


def _shared_prometheus_post_check(
    plan: VerificationPlan,
    *,
    prometheus: PrometheusClient,
) -> dict[str, Any] | None:
    first_prometheus_check = next(
        (check for check in plan.checks if check.check_type.startswith("prometheus_")),
        None,
    )
    if first_prometheus_check is None:
        return None
    return _prometheus_post_check(plan, check=first_prometheus_check, prometheus=prometheus)


def _prometheus_check_result(
    plan: VerificationPlan,
    *,
    check: VerificationCheck,
    prometheus: PrometheusClient,
    shared_prometheus_post_check: dict[str, Any] | None,
) -> dict[str, Any]:
    if shared_prometheus_post_check is not None:
        return shared_prometheus_post_check
    return _prometheus_post_check(plan, check=check, prometheus=prometheus)


def _resource_lookup(
    kubernetes: KubernetesClient,
    *,
    kind: str,
    namespace: str,
    name: str,
) -> dict[str, Any]:
    if kind == "StressChaos":
        return kubernetes.get_stresschaos(namespace=namespace, name=name)
    if kind == "NetworkChaos":
        return kubernetes.get_networkchaos(namespace=namespace, name=name)
    return kubernetes.get_resource_json(namespace=namespace, kind=kind, name=name)


def _resource_is_absent(result: dict[str, Any]) -> bool:
    stderr = str(result.get("stderr", "")).lower()
    stdout = str(result.get("stdout", "")).lower()
    return "not found" in stderr or "not found" in stdout


def _observed_ready_count(result: dict[str, Any]) -> float:
    if "ready_count" in result:
        return float(result.get("ready_count", 0.0) or 0.0)
    if "ready_replicas" in result:
        return float(result.get("ready_replicas", 0.0) or 0.0)
    if "available_replicas" in result:
        return float(result.get("available_replicas", 0.0) or 0.0)
    return 0.0


def _observed_probe_success(result: dict[str, Any]) -> float | None:
    value = result.get("probe_success")
    if value is None:
        return None
    return float(value)


def _action_target_name(action: RemediationAction) -> str | None:
    if action.action_type in {"rollout_undo_deployment", "rollout_restart_deployment", "scale_deployment"}:
        return str(action.parameters.get("deployment") or "")
    if action.action_type in {"delete_stresschaos", "delete_networkchaos"}:
        return str(action.parameters.get("name") or "")
    return None


def _operation_family_for_action(action_type: str) -> str:
    if action_type == "rollout_undo_deployment":
        return "rollout.undo_deployment"
    if action_type == "rollout_restart_deployment":
        return "rollout.restart_deployment"
    if action_type == "scale_deployment":
        return "scale.deployment"
    if action_type == "delete_stresschaos":
        return "chaos.delete_stresschaos"
    if action_type == "delete_networkchaos":
        return "chaos.delete_networkchaos"
    return "escalate.human_review"


def _verification_target(action: RemediationAction) -> ResourceTarget:
    if action.action_type in {"rollout_undo_deployment", "rollout_restart_deployment", "scale_deployment"}:
        return ResourceTarget(
            namespace=str(action.parameters["namespace"]),
            kind="Deployment",
            name=str(action.parameters["deployment"]),
        )
    if action.action_type == "delete_stresschaos":
        return ResourceTarget(
            namespace=str(action.parameters["namespace"]),
            kind="StressChaos",
            name=str(action.parameters["name"]),
        )
    if action.action_type == "delete_networkchaos":
        return ResourceTarget(
            namespace=str(action.parameters["namespace"]),
            kind="NetworkChaos",
            name=str(action.parameters["name"]),
        )
    return ResourceTarget(namespace=str(action.parameters.get("namespace", "")), kind="Incident", name=None)


def _default_deployment_for_class(incident_class_hint: str | None) -> str:
    if incident_class_hint == "network_partition":
        return "cartservice"
    return "frontend"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
