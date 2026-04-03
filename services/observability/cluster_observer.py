from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from schemas.incident import Incident
from schemas.observations import ObservationBundle
from services.normalization.incident import normalize_incident_class
from services.infra.kubernetes.client import KubernetesClient
from services.observability.prometheus import (
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
            kubernetes["resource_quotas"] = self._safe_kubernetes_call(
                errors,
                "get_resource_quotas",
                lambda: self.kubernetes_client.get_resource_quotas(namespace=namespace),
            )
            kubernetes["persistent_volume_claims"] = self._safe_kubernetes_call(
                errors,
                "list_persistent_volume_claims",
                lambda: self.kubernetes_client.list_persistent_volume_claims(namespace=namespace),
            )
            kubernetes["horizontal_pod_autoscalers"] = self._safe_kubernetes_call(
                errors,
                "list_horizontal_pod_autoscalers",
                lambda: self.kubernetes_client.list_horizontal_pod_autoscalers(namespace=namespace),
            )
            kubernetes["pod_status_summary"] = _summarize_pods_result(kubernetes.get("pods"))
            kubernetes["event_summary"] = _summarize_events_result(kubernetes.get("events"))
            kubernetes["resource_quota_summary"] = _summarize_resource_quotas_result(
                kubernetes.get("resource_quotas")
            )
            kubernetes["pvc_summary"] = _summarize_persistent_volume_claims_result(
                kubernetes.get("persistent_volume_claims")
            )
            kubernetes["hpa_summary"] = _summarize_horizontal_pod_autoscalers_result(
                kubernetes.get("horizontal_pod_autoscalers")
            )
            node_names = list(kubernetes["pod_status_summary"].get("node_names", []))
            if node_names:
                kubernetes["node_conditions"] = [
                    self._safe_kubernetes_call(
                        errors,
                        "get_node_conditions",
                        lambda node_name=node_name: self.kubernetes_client.get_node_conditions(node=node_name),
                    )
                    for node_name in node_names[:3]
                ]
                kubernetes["node_condition_summary"] = _summarize_node_conditions_results(
                    kubernetes.get("node_conditions")
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
            kubernetes["replica_sets"] = self._safe_kubernetes_call(
                errors,
                "list_replica_sets",
                lambda: self.kubernetes_client.list_replica_sets(
                    namespace=namespace,
                    label_selector=f"app={deployment_hint}",
                ),
            )
            kubernetes["deployment_summary"] = _summarize_deployment_result(kubernetes.get("deployment"))
            kubernetes["replica_set_summary"] = _summarize_replica_sets_result(kubernetes.get("replica_sets"))
            kubernetes["rollout_summary"] = _summarize_rollout_history_result(kubernetes.get("rollout_history"))
            deployment_summary = kubernetes.get("deployment_summary")
            if isinstance(deployment_summary, dict):
                config_refs = list(deployment_summary.get("config_map_refs", []))
                secret_refs = list(deployment_summary.get("secret_refs", []))
                if config_refs:
                    kubernetes["config_maps"] = [
                        self._safe_kubernetes_call(
                            errors,
                            "get_config_map_context",
                            lambda name=name: self.kubernetes_client.get_config_map_context(
                                namespace=namespace,
                                name=name,
                            ),
                        )
                        for name in config_refs[:5]
                    ]
                    kubernetes["config_map_summary"] = _summarize_config_objects(
                        kubernetes.get("config_maps"),
                        kind="ConfigMap",
                    )
                if secret_refs:
                    kubernetes["secrets"] = [
                        self._safe_kubernetes_call(
                            errors,
                            "get_secret_context_metadata",
                            lambda name=name: self.kubernetes_client.get_secret_context_metadata(
                                namespace=namespace,
                                name=name,
                            ),
                        )
                        for name in secret_refs[:5]
                    ]
                    kubernetes["secret_summary"] = _summarize_config_objects(
                        kubernetes.get("secrets"),
                        kind="Secret",
                    )

        if namespace is not None and service_hint is not None:
            kubernetes["service"] = self._safe_kubernetes_call(
                errors,
                "get_service_context",
                lambda: self.kubernetes_client.get_service_context(
                    namespace=namespace,
                    service=service_hint,
                ),
            )
            kubernetes["service_endpoints"] = self._safe_kubernetes_call(
                errors,
                "get_service_endpoints",
                lambda: self.kubernetes_client.get_service_endpoints(
                    namespace=namespace,
                    service=service_hint,
                ),
            )
            kubernetes["endpoint_slices"] = self._safe_kubernetes_call(
                errors,
                "list_endpoint_slices",
                lambda: self.kubernetes_client.list_endpoint_slices(
                    namespace=namespace,
                    service=service_hint,
                ),
            )
            kubernetes["service_summary"] = _summarize_service_result(kubernetes.get("service"))
            kubernetes["endpoint_summary"] = _summarize_service_endpoints_result(
                kubernetes.get("service_endpoints")
            )
            kubernetes["endpoint_slice_summary"] = _summarize_endpoint_slices_result(
                kubernetes.get("endpoint_slices")
            )

        kubernetes["dependency_summary"] = _summarize_dependency_context(
            deployment_summary=kubernetes.get("deployment_summary"),
            service_summary=kubernetes.get("service_summary"),
            endpoint_summary=kubernetes.get("endpoint_summary"),
            endpoint_slice_summary=kubernetes.get("endpoint_slice_summary"),
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


def _resource_items(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    resource = result.get("resource")
    if not isinstance(resource, dict):
        return []
    items = resource.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _resource_dict(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    resource = result.get("resource")
    if not isinstance(resource, dict):
        return {}
    return resource


def _summarize_pods_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    waiting_reasons: dict[str, int] = {}
    termination_reasons: dict[str, int] = {}
    non_ready_pods: list[str] = []
    restart_total = 0
    ready_pod_count = 0
    node_names: set[str] = set()

    for item in items:
        metadata = item.get("metadata")
        spec = item.get("spec")
        status = item.get("status")
        pod_name = metadata.get("name") if isinstance(metadata, dict) else None
        if isinstance(spec, dict):
            node_name = spec.get("nodeName")
            if isinstance(node_name, str) and node_name:
                node_names.add(node_name)
        if not isinstance(status, dict):
            continue
        container_statuses = status.get("containerStatuses")
        pod_ready = False
        if isinstance(container_statuses, list):
            for container_status in container_statuses:
                if not isinstance(container_status, dict):
                    continue
                restart_total += _as_non_negative_int(container_status.get("restartCount"))
                if container_status.get("ready") is True:
                    pod_ready = True
                state = container_status.get("state")
                if isinstance(state, dict):
                    waiting = state.get("waiting")
                    if isinstance(waiting, dict):
                        reason = waiting.get("reason")
                        if isinstance(reason, str) and reason:
                            waiting_reasons[reason] = waiting_reasons.get(reason, 0) + 1
                last_state = container_status.get("lastState")
                if isinstance(last_state, dict):
                    terminated = last_state.get("terminated")
                    if isinstance(terminated, dict):
                        reason = terminated.get("reason")
                        if isinstance(reason, str) and reason:
                            termination_reasons[reason] = termination_reasons.get(reason, 0) + 1
        if pod_ready:
            ready_pod_count += 1
        elif isinstance(pod_name, str) and pod_name:
            non_ready_pods.append(pod_name)

    return {
        "pod_count": len(items),
        "ready_pod_count": ready_pod_count,
        "restart_total": restart_total,
        "waiting_reasons": waiting_reasons,
        "termination_reasons": termination_reasons,
        "non_ready_pods": non_ready_pods[:5],
        "node_names": sorted(node_names),
    }


def _summarize_events_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    warnings: list[dict[str, Any]] = []

    for item in items[-10:]:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() != "warning":
            continue
        involved_object = item.get("involvedObject")
        if not isinstance(involved_object, dict):
            involved_object = {}
        warnings.append(
            {
                "reason": item.get("reason"),
                "message": item.get("message"),
                "kind": involved_object.get("kind"),
                "name": involved_object.get("name"),
                "last_timestamp": item.get("lastTimestamp"),
            }
        )

    return {
        "warning_count": len(warnings),
        "recent_warnings": warnings[-5:],
    }


def _summarize_deployment_result(result: Any) -> dict[str, Any]:
    resource = _resource_dict(result)
    if not resource:
        return {}

    metadata = resource.get("metadata")
    spec = resource.get("spec")
    status = resource.get("status")
    metadata = metadata if isinstance(metadata, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    status = status if isinstance(status, dict) else {}
    template = spec.get("template")
    template = template if isinstance(template, dict) else {}
    pod_spec = template.get("spec")
    pod_spec = pod_spec if isinstance(pod_spec, dict) else {}

    config_map_refs: set[str] = set()
    secret_refs: set[str] = set()
    image_names: list[str] = []
    command_overrides: list[list[str]] = []

    containers = pod_spec.get("containers")
    if isinstance(containers, list):
        for container in containers:
            if not isinstance(container, dict):
                continue
            image = container.get("image")
            if isinstance(image, str) and image:
                image_names.append(image)
            command = container.get("command")
            if isinstance(command, list) and all(isinstance(item, str) for item in command):
                command_overrides.append(list(command))
            _collect_env_references(
                container,
                config_map_refs=config_map_refs,
                secret_refs=secret_refs,
            )

    volumes = pod_spec.get("volumes")
    if isinstance(volumes, list):
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            config_map = volume.get("configMap")
            if isinstance(config_map, dict):
                name = config_map.get("name")
                if isinstance(name, str) and name:
                    config_map_refs.add(name)
            secret = volume.get("secret")
            if isinstance(secret, dict):
                name = secret.get("secretName")
                if isinstance(name, str) and name:
                    secret_refs.add(name)

    conditions: list[dict[str, Any]] = []
    raw_conditions = status.get("conditions")
    if isinstance(raw_conditions, list):
        for condition in raw_conditions:
            if not isinstance(condition, dict):
                continue
            conditions.append(
                {
                    "type": condition.get("type"),
                    "status": condition.get("status"),
                    "reason": condition.get("reason"),
                }
            )

    return {
        "name": metadata.get("name"),
        "generation": metadata.get("generation"),
        "desired_replicas": _as_non_negative_int(spec.get("replicas")),
        "updated_replicas": _as_non_negative_int(status.get("updatedReplicas")),
        "ready_replicas": _as_non_negative_int(status.get("readyReplicas")),
        "available_replicas": _as_non_negative_int(status.get("availableReplicas")),
        "observed_generation": _as_non_negative_int(status.get("observedGeneration")),
        "images": image_names,
        "command_overrides": command_overrides,
        "config_map_refs": sorted(config_map_refs),
        "secret_refs": sorted(secret_refs),
        "conditions": conditions,
    }


def _summarize_replica_sets_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    summaries: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata")
        spec = item.get("spec")
        status = item.get("status")
        metadata = metadata if isinstance(metadata, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        status = status if isinstance(status, dict) else {}
        summaries.append(
            {
                "name": metadata.get("name"),
                "revision": _deployment_revision(metadata.get("annotations")),
                "desired_replicas": _as_non_negative_int(spec.get("replicas")),
                "ready_replicas": _as_non_negative_int(status.get("readyReplicas")),
                "available_replicas": _as_non_negative_int(status.get("availableReplicas")),
            }
        )
    summaries.sort(key=lambda item: item.get("revision", 0), reverse=True)
    return {
        "replica_set_count": len(summaries),
        "recent_replica_sets": summaries[:5],
    }


def _summarize_rollout_history_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    output = result.get("output")
    if not isinstance(output, str):
        return {}
    revisions: list[int] = []
    for token in re.findall(r"#(\d+)", output):
        try:
            revisions.append(int(token))
        except ValueError:
            continue
    return {
        "revision_count": len(revisions),
        "latest_revision": max(revisions) if revisions else None,
        "recent_revisions": revisions[-5:],
    }


def _summarize_service_endpoints_result(result: Any) -> dict[str, Any]:
    resource = _resource_dict(result)
    subsets = resource.get("subsets")
    if not isinstance(subsets, list):
        return {
            "address_count": 0,
            "not_ready_address_count": 0,
            "ports": [],
        }
    address_count = 0
    not_ready_address_count = 0
    ports: list[int] = []
    for subset in subsets:
        if not isinstance(subset, dict):
            continue
        addresses = subset.get("addresses")
        not_ready = subset.get("notReadyAddresses")
        subset_ports = subset.get("ports")
        if isinstance(addresses, list):
            address_count += len(addresses)
        if isinstance(not_ready, list):
            not_ready_address_count += len(not_ready)
        if isinstance(subset_ports, list):
            for port_item in subset_ports:
                if not isinstance(port_item, dict):
                    continue
                port = port_item.get("port")
                if isinstance(port, int):
                    ports.append(port)
    return {
        "address_count": address_count,
        "not_ready_address_count": not_ready_address_count,
        "ports": sorted(set(ports)),
    }


def _summarize_service_result(result: Any) -> dict[str, Any]:
    resource = _resource_dict(result)
    if not resource:
        return {}
    metadata = resource.get("metadata")
    spec = resource.get("spec")
    metadata = metadata if isinstance(metadata, dict) else {}
    spec = spec if isinstance(spec, dict) else {}
    ports = spec.get("ports")
    selector = spec.get("selector")
    summarized_ports: list[dict[str, Any]] = []
    if isinstance(ports, list):
        for item in ports:
            if not isinstance(item, dict):
                continue
            summarized_ports.append(
                {
                    "name": item.get("name"),
                    "port": item.get("port"),
                    "target_port": item.get("targetPort"),
                    "protocol": item.get("protocol"),
                }
            )
    return {
        "name": metadata.get("name"),
        "type": spec.get("type"),
        "selector": dict(selector) if isinstance(selector, dict) else {},
        "ports": summarized_ports,
    }


def _summarize_endpoint_slices_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    ready_endpoint_count = 0
    not_ready_endpoint_count = 0
    endpoint_count = 0
    ports: list[int] = []
    for item in items:
        endpoints = item.get("endpoints")
        if isinstance(endpoints, list):
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    continue
                endpoint_count += 1
                conditions = endpoint.get("conditions")
                conditions = conditions if isinstance(conditions, dict) else {}
                if conditions.get("ready") is False:
                    not_ready_endpoint_count += 1
                else:
                    ready_endpoint_count += 1
        slice_ports = item.get("ports")
        if isinstance(slice_ports, list):
            for port_item in slice_ports:
                if not isinstance(port_item, dict):
                    continue
                port = port_item.get("port")
                if isinstance(port, int):
                    ports.append(port)
    return {
        "endpoint_slice_count": len(items),
        "endpoint_count": endpoint_count,
        "ready_endpoint_count": ready_endpoint_count,
        "not_ready_endpoint_count": not_ready_endpoint_count,
        "ports": sorted(set(ports)),
    }


def _summarize_dependency_context(
    *,
    deployment_summary: Any,
    service_summary: Any,
    endpoint_summary: Any,
    endpoint_slice_summary: Any,
) -> dict[str, Any]:
    deployment_summary = deployment_summary if isinstance(deployment_summary, dict) else {}
    service_summary = service_summary if isinstance(service_summary, dict) else {}
    endpoint_summary = endpoint_summary if isinstance(endpoint_summary, dict) else {}
    endpoint_slice_summary = (
        endpoint_slice_summary if isinstance(endpoint_slice_summary, dict) else {}
    )
    return {
        "config_map_refs": list(deployment_summary.get("config_map_refs", [])),
        "secret_refs": list(deployment_summary.get("secret_refs", [])),
        "service_selector": dict(service_summary.get("selector", {}))
        if isinstance(service_summary.get("selector"), dict)
        else {},
        "service_port_count": len(service_summary.get("ports", []))
        if isinstance(service_summary.get("ports"), list)
        else 0,
        "endpoint_address_count": _as_non_negative_int(endpoint_summary.get("address_count")),
        "endpoint_not_ready_count": _as_non_negative_int(
            endpoint_summary.get("not_ready_address_count")
        ),
        "endpoint_slice_ready_count": _as_non_negative_int(
            endpoint_slice_summary.get("ready_endpoint_count")
        ),
        "endpoint_slice_not_ready_count": _as_non_negative_int(
            endpoint_slice_summary.get("not_ready_endpoint_count")
        ),
    }


def _summarize_config_objects(results: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(results, list):
        return {}
    summaries: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        resource = _resource_dict(result)
        metadata = resource.get("metadata") if isinstance(resource, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        summary = {
            "name": metadata.get("name") or result.get("name"),
            "exists": result.get("status") == "succeeded",
            "resource_version": metadata.get("resourceVersion"),
        }
        if kind == "ConfigMap":
            data = resource.get("data") if isinstance(resource, dict) else {}
            summary["data_key_count"] = len(data) if isinstance(data, dict) else 0
        if kind == "Secret":
            summary["type"] = resource.get("type") if isinstance(resource, dict) else None
            data_keys = resource.get("data_keys") if isinstance(resource, dict) else []
            summary["data_keys"] = list(data_keys) if isinstance(data_keys, list) else []
        summaries.append(summary)
    return {
        "count": len(summaries),
        "objects": summaries[:5],
    }


def _summarize_node_conditions_results(results: Any) -> dict[str, Any]:
    if not isinstance(results, list):
        return {}
    summaries: list[dict[str, Any]] = []
    for result in results:
        resource = _resource_dict(result)
        metadata = resource.get("metadata") if isinstance(resource, dict) else {}
        status = resource.get("status") if isinstance(resource, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        status = status if isinstance(status, dict) else {}
        node_conditions: dict[str, str] = {}
        conditions = status.get("conditions")
        if isinstance(conditions, list):
            for condition in conditions:
                if not isinstance(condition, dict):
                    continue
                condition_type = condition.get("type")
                condition_status = condition.get("status")
                if isinstance(condition_type, str) and isinstance(condition_status, str):
                    node_conditions[condition_type] = condition_status
        summaries.append(
            {
                "node": metadata.get("name") or result.get("node"),
                "conditions": node_conditions,
            }
        )
    return {
        "node_count": len(summaries),
        "nodes": summaries,
    }


def _summarize_horizontal_pod_autoscalers_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    summaries: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata")
        spec = item.get("spec")
        status = item.get("status")
        metadata = metadata if isinstance(metadata, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        status = status if isinstance(status, dict) else {}
        summaries.append(
            {
                "name": metadata.get("name"),
                "min_replicas": _as_non_negative_int(spec.get("minReplicas")),
                "max_replicas": _as_non_negative_int(spec.get("maxReplicas")),
                "current_replicas": _as_non_negative_int(status.get("currentReplicas")),
                "desired_replicas": _as_non_negative_int(status.get("desiredReplicas")),
            }
        )
    return {"hpa_count": len(summaries), "hpas": summaries[:10]}


def _summarize_persistent_volume_claims_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    summaries: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata")
        status = item.get("status")
        spec = item.get("spec")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = status if isinstance(status, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        summaries.append(
            {
                "name": metadata.get("name"),
                "phase": status.get("phase"),
                "storage_class": spec.get("storageClassName"),
            }
        )
    pending = [summary["name"] for summary in summaries if summary.get("phase") != "Bound"]
    return {
        "pvc_count": len(summaries),
        "pending_claim_count": len(pending),
        "pending_claims": pending[:10],
    }


def _summarize_resource_quotas_result(result: Any) -> dict[str, Any]:
    items = _resource_items(result)
    quotas: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata")
        status = item.get("status")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = status if isinstance(status, dict) else {}
        used = status.get("used")
        hard = status.get("hard")
        quotas.append(
            {
                "name": metadata.get("name"),
                "used": dict(used) if isinstance(used, dict) else {},
                "hard": dict(hard) if isinstance(hard, dict) else {},
            }
        )
    return {"quota_count": len(quotas), "quotas": quotas[:5]}


def _collect_env_references(
    container: dict[str, Any],
    *,
    config_map_refs: set[str],
    secret_refs: set[str],
) -> None:
    env_items = container.get("env")
    if isinstance(env_items, list):
        for env_item in env_items:
            if not isinstance(env_item, dict):
                continue
            value_from = env_item.get("valueFrom")
            if not isinstance(value_from, dict):
                continue
            config_map_key_ref = value_from.get("configMapKeyRef")
            if isinstance(config_map_key_ref, dict):
                name = config_map_key_ref.get("name")
                if isinstance(name, str) and name:
                    config_map_refs.add(name)
            secret_key_ref = value_from.get("secretKeyRef")
            if isinstance(secret_key_ref, dict):
                name = secret_key_ref.get("name")
                if isinstance(name, str) and name:
                    secret_refs.add(name)

    env_from_items = container.get("envFrom")
    if isinstance(env_from_items, list):
        for env_from_item in env_from_items:
            if not isinstance(env_from_item, dict):
                continue
            config_map_ref = env_from_item.get("configMapRef")
            if isinstance(config_map_ref, dict):
                name = config_map_ref.get("name")
                if isinstance(name, str) and name:
                    config_map_refs.add(name)
            secret_ref = env_from_item.get("secretRef")
            if isinstance(secret_ref, dict):
                name = secret_ref.get("name")
                if isinstance(name, str) and name:
                    secret_refs.add(name)


def _deployment_revision(annotations: Any) -> int:
    if not isinstance(annotations, dict):
        return 0
    value = annotations.get("deployment.kubernetes.io/revision")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0
