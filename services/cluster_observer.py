from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from schemas.incident import Incident
from schemas.observations import ObservationBundle
from services.incident_normalization import normalize_incident_class
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import (
    PrometheusClient,
    _cartservice_network_receive_query,
    _crashloop_query,
    _frontend_cart_probe_query,
    _frontend_cpu_query,
    _ready_query,
)


class ClusterObserver:
    def __init__(
        self,
        *,
        kubernetes_client: KubernetesClient | None = None,
        prometheus_client: PrometheusClient | None = None,
    ) -> None:
        self.kubernetes_client = kubernetes_client or KubernetesClient()
        self.prometheus_client = prometheus_client or PrometheusClient()

    def collect(
        self,
        *,
        incident: Incident,
        namespace_hint: str | None = None,
    ) -> ObservationBundle:
        alert = incident.raw_context.get("alert", {})
        labels = _optional_dict(alert.get("labels"))
        annotations = _optional_dict(alert.get("annotations"))
        namespace = namespace_hint or _optional_string(labels.get("namespace"))
        deployment_hint = _infer_deployment_from_labels(labels)
        service_hint = _infer_service_from_labels(labels, deployment_hint=deployment_hint)
        pod = _optional_string(labels.get("pod"))
        container = _optional_string(labels.get("container"))
        incident_class_hint = normalize_incident_class(incident.incident_class)

        errors: list[str] = []
        kubernetes: dict[str, Any] = {}
        prometheus: dict[str, Any] = {}

        if namespace is not None:
            kubernetes["pods"] = self._safe_kubernetes_call(
                errors,
                "list_pods",
                lambda: self.kubernetes_client.list_pods(
                    namespace=namespace,
                    label_selector=(f"app={deployment_hint}" if deployment_hint else None),
                ),
            )
            kubernetes["events"] = self._safe_kubernetes_call(
                errors,
                "get_events",
                lambda: self.kubernetes_client.get_events(namespace=namespace),
            )

        if namespace is not None and deployment_hint is not None:
            kubernetes["deployment"] = self._safe_kubernetes_call(
                errors,
                "get_resource_json",
                lambda: self.kubernetes_client.get_resource_json(
                    namespace=namespace,
                    kind="deployment",
                    name=deployment_hint,
                ),
            )
            kubernetes["rollout_history"] = self._safe_kubernetes_call(
                errors,
                "get_rollout_history",
                lambda: self.kubernetes_client.get_rollout_history(
                    namespace=namespace,
                    deployment=deployment_hint,
                ),
            )

        if namespace is not None and service_hint is not None:
            kubernetes["service_endpoints"] = self._safe_kubernetes_call(
                errors,
                "get_service_endpoints",
                lambda: self.kubernetes_client.get_service_endpoints(
                    namespace=namespace,
                    service=service_hint,
                ),
            )

        if namespace is not None and pod is not None:
            pod_logs = self._safe_kubernetes_call(
                errors,
                "get_pod_logs",
                lambda: self.kubernetes_client.get_pod_logs(
                    namespace=namespace,
                    pod=pod,
                    container=container,
                ),
            )
            kubernetes["pod_logs"] = _redact_log_result(pod_logs)

        for query_name, query in _observation_queries(
            incident_class_hint=incident_class_hint,
            namespace=namespace,
            deployment_hint=deployment_hint,
        ).items():
            prometheus[query_name] = self.prometheus_client.query(query)
            if prometheus[query_name]["status"] != "succeeded":
                errors.append(
                    f"prometheus.{query_name} failed: {prometheus[query_name].get('error', 'unknown error')}"
                )

        return ObservationBundle(
            incident_id=incident.incident_id,
            incident_class_hint=incident_class_hint,
            namespace_hint=namespace,
            source=incident.source,
            alert_context={
                "labels": labels,
                "annotations": annotations,
                "deployment_hint": deployment_hint,
                "service_hint": service_hint,
                "pod": pod,
                "container": container,
            },
            kubernetes=kubernetes,
            prometheus=prometheus,
            collected_at=_utc_now(),
            errors=errors,
        )

    def _safe_kubernetes_call(
        self,
        errors: list[str],
        operation_name: str,
        operation: Any,
    ) -> dict[str, Any]:
        try:
            result = operation()
        except Exception as exc:
            errors.append(f"kubernetes.{operation_name} failed: {exc}")
            return {"status": "failed", "error": str(exc)}
        if not isinstance(result, dict):
            errors.append(f"kubernetes.{operation_name} returned a non-dict result")
            return {"status": "failed", "error": "non-dict result"}
        if result.get("status") != "succeeded":
            error_text = str(result.get("stderr") or result.get("error") or "unknown error")
            errors.append(f"kubernetes.{operation_name} failed: {error_text}")
        return result


def _observation_queries(
    *,
    incident_class_hint: str,
    namespace: str | None,
    deployment_hint: str | None,
) -> dict[str, str]:
    if namespace is None:
        return {}

    queries: dict[str, str] = {}
    if deployment_hint is not None:
        queries["ready"] = _ready_query(namespace=namespace, deployment=deployment_hint)

    if incident_class_hint == "crashloop" and deployment_hint is not None:
        queries["incident_signal"] = _crashloop_query(namespace=namespace, deployment=deployment_hint)
    elif incident_class_hint == "cpu_saturation" and deployment_hint is not None:
        queries["incident_signal"] = _frontend_cpu_query(
            namespace=namespace,
            deployment=deployment_hint,
            rate_window="1m",
        )
    elif incident_class_hint == "bad_config":
        queries["incident_signal"] = _frontend_cart_probe_query(namespace=namespace)
    elif incident_class_hint == "network_partition" and deployment_hint is not None:
        queries["incident_signal"] = _cartservice_network_receive_query(
            namespace=namespace,
            deployment=deployment_hint,
            rate_window="5m",
        )

    return queries


def _infer_deployment_from_labels(labels: dict[str, Any]) -> str | None:
    for key in ("deployment", "app", "service"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value

    pod = labels.get("pod")
    if isinstance(pod, str) and pod:
        return pod.split("-", 1)[0]
    return None


def _infer_service_from_labels(
    labels: dict[str, Any],
    *,
    deployment_hint: str | None,
) -> str | None:
    for key in ("service", "app", "deployment"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value
    return deployment_hint


def _optional_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _redact_log_result(result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output")
    if not isinstance(output, str):
        return result

    redacted_output = output
    for pattern in (
        r"(?i)(authorization\s*[:=]\s*)\S+",
        r"(?i)(token\s*[:=]\s*)\S+",
        r"(?i)(password\s*[:=]\s*)\S+",
        r"(?i)(secret\s*[:=]\s*)\S+",
        r"(?i)(api[_-]?key\s*[:=]\s*)\S+",
    ):
        redacted_output = re.sub(pattern, r"\1<redacted>", redacted_output)

    truncated_output = redacted_output[:4000]
    if len(redacted_output) > 4000:
        truncated_output += "...<truncated>"

    return replace_output(result, truncated_output)


def replace_output(result: dict[str, Any], output: str) -> dict[str, Any]:
    updated = dict(result)
    updated["output"] = output
    return updated


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
