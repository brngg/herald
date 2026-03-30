from __future__ import annotations

import os
from dataclasses import dataclass
import time
from typing import Any, Callable


PrometheusQueryRunner = Callable[[str], float | None]
PrometheusRangeQueryRunner = Callable[[str, str, str, str], list[dict[str, Any]] | None]
SleepFn = Callable[[float], None]


@dataclass(slots=True)
class PrometheusClient:
    base_url: str | None = None
    query_runner: PrometheusQueryRunner | None = None
    range_query_runner: PrometheusRangeQueryRunner | None = None
    timeout_seconds: float = 10.0
    pre_check_retry_attempts: int = 3
    pre_check_retry_sleep_seconds: float = 2.0
    pre_check_lookback_window: str = "2m"
    cpu_check_rate_window: str = "1m"
    network_partition_rate_window: str = "5m"
    network_partition_receive_threshold: float = 100.0
    post_check_retry_attempts: int = 6
    post_check_retry_sleep_seconds: float = 5.0
    sleep_fn: SleepFn = time.sleep

    def pre_check_crashloop(self, *, namespace: str, deployment: str) -> dict[str, object]:
        crashloop_query = _recent_crashloop_query(
            namespace=namespace,
            deployment=deployment,
            lookback_window=self.pre_check_lookback_window,
        )
        crashloop_count = 0.0
        attempts = max(1, self.pre_check_retry_attempts)

        for attempt in range(1, attempts + 1):
            crashloop_count = self._query_value(crashloop_query) or 0.0
            if crashloop_count > 0:
                break
            if attempt < attempts:
                self.sleep_fn(self.pre_check_retry_sleep_seconds)

        return {
            "status": "ready_to_execute" if crashloop_count > 0 else "not_firing",
            "namespace": namespace,
            "deployment": deployment,
            "crashloop_count": crashloop_count,
            "query": crashloop_query,
            "should_execute": crashloop_count > 0,
            "attempts": attempts,
        }

    def post_check_crashloop(self, *, namespace: str, deployment: str) -> dict[str, object]:
        crashloop_query = _crashloop_query(namespace=namespace, deployment=deployment)
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        crashloop_count = 0.0
        ready_count = 0.0
        attempts = max(1, self.post_check_retry_attempts)
        recovered = False

        for attempt in range(1, attempts + 1):
            crashloop_count = self._query_value(crashloop_query) or 0.0
            ready_count = self._query_value(ready_query) or 0.0
            recovered = crashloop_count == 0 and ready_count > 0
            if recovered:
                break
            if attempt < attempts:
                self.sleep_fn(self.post_check_retry_sleep_seconds)

        return {
            "status": "recovered" if recovered else "unrecovered",
            "namespace": namespace,
            "deployment": deployment,
            "crashloop_count": crashloop_count,
            "ready_count": ready_count,
            "attempts": attempts,
            "queries": {
                "crashloop": crashloop_query,
                "ready": ready_query,
            },
        }

    def pre_check_cpu_saturation(self, *, namespace: str, deployment: str) -> dict[str, object]:
        cpu_query = _frontend_cpu_query(
            namespace=namespace,
            deployment=deployment,
            rate_window=self.cpu_check_rate_window,
        )
        cpu_usage = 0.0
        attempts = max(1, self.pre_check_retry_attempts)

        for attempt in range(1, attempts + 1):
            cpu_usage = self._query_value(cpu_query) or 0.0
            if cpu_usage > 0.05:
                break
            if attempt < attempts:
                self.sleep_fn(self.pre_check_retry_sleep_seconds)

        return {
            "status": "ready_to_execute" if cpu_usage > 0.05 else "not_firing",
            "namespace": namespace,
            "deployment": deployment,
            "cpu_usage": cpu_usage,
            "query": cpu_query,
            "should_execute": cpu_usage > 0.05,
            "attempts": attempts,
        }

    def post_check_cpu_saturation(self, *, namespace: str, deployment: str) -> dict[str, object]:
        cpu_query = _frontend_cpu_query(
            namespace=namespace,
            deployment=deployment,
            rate_window=self.cpu_check_rate_window,
        )
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        cpu_usage = 0.0
        ready_count = 0.0
        attempts = max(1, self.post_check_retry_attempts)
        recovered = False

        for attempt in range(1, attempts + 1):
            cpu_usage = self._query_value(cpu_query) or 0.0
            ready_count = self._query_value(ready_query) or 0.0
            recovered = cpu_usage <= 0.05 and ready_count > 0
            if recovered:
                break
            if attempt < attempts:
                self.sleep_fn(self.post_check_retry_sleep_seconds)

        return {
            "status": "recovered" if recovered else "unrecovered",
            "namespace": namespace,
            "deployment": deployment,
            "cpu_usage": cpu_usage,
            "ready_count": ready_count,
            "attempts": attempts,
            "queries": {
                "cpu": cpu_query,
                "ready": ready_query,
            },
        }

    def pre_check_bad_config(self, *, namespace: str, deployment: str) -> dict[str, object]:
        probe_query = _frontend_cart_probe_query(namespace=namespace)
        probe_success: float | None = None
        attempts = max(1, self.pre_check_retry_attempts)
        saw_probe_data = False
        saw_missing_probe_telemetry = False
        observed_failure = False

        for attempt in range(1, attempts + 1):
            probe_value = self._query_value(probe_query)
            if probe_value is None:
                saw_missing_probe_telemetry = True
                if attempt < attempts:
                    self.sleep_fn(self.pre_check_retry_sleep_seconds)
                continue
            saw_probe_data = True
            probe_success = probe_value
            if probe_success == 0:
                observed_failure = True
                break
            if attempt < attempts:
                self.sleep_fn(self.pre_check_retry_sleep_seconds)

        return {
            "status": "ready_to_execute" if observed_failure else ("not_firing" if saw_probe_data else "unknown"),
            "namespace": namespace,
            "deployment": deployment,
            "probe_success": probe_success,
            "query": probe_query,
            "missing_probe_telemetry": saw_missing_probe_telemetry,
            "should_execute": observed_failure,
            "attempts": attempts,
        }

    def post_check_bad_config(self, *, namespace: str, deployment: str) -> dict[str, object]:
        probe_query = _frontend_cart_probe_query(namespace=namespace)
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        probe_success: float | None = None
        ready_count = 0.0
        attempts = max(1, self.post_check_retry_attempts)
        recovered = False
        saw_probe_data = False
        saw_missing_probe_telemetry = False

        for attempt in range(1, attempts + 1):
            probe_value = self._query_value(probe_query)
            ready_count = self._query_value(ready_query) or 0.0
            if probe_value is None:
                saw_missing_probe_telemetry = True
                if attempt < attempts:
                    self.sleep_fn(self.post_check_retry_sleep_seconds)
                continue
            saw_probe_data = True
            probe_success = probe_value
            recovered = probe_success > 0 and ready_count > 0
            if recovered:
                break
            if attempt < attempts:
                self.sleep_fn(self.post_check_retry_sleep_seconds)

        return {
            "status": "recovered" if recovered else ("unrecovered" if saw_probe_data else "unknown"),
            "namespace": namespace,
            "deployment": deployment,
            "probe_success": probe_success,
            "ready_count": ready_count,
            "attempts": attempts,
            "missing_probe_telemetry": saw_missing_probe_telemetry,
            "queries": {
                "probe_success": probe_query,
                "ready": ready_query,
            },
        }

    def pre_check_network_partition(self, *, namespace: str, deployment: str) -> dict[str, object]:
        network_query = _cartservice_network_receive_query(
            namespace=namespace,
            deployment=deployment,
            rate_window=self.network_partition_rate_window,
        )
        receive_rate: float | None = None
        attempts = max(1, self.pre_check_retry_attempts)
        saw_network_data = False
        saw_missing_network_telemetry = False
        observed_partition = False

        for attempt in range(1, attempts + 1):
            receive_value = self._query_value(network_query)
            if receive_value is None:
                saw_missing_network_telemetry = True
                if attempt < attempts:
                    self.sleep_fn(self.pre_check_retry_sleep_seconds)
                continue
            saw_network_data = True
            receive_rate = receive_value
            if receive_rate < self.network_partition_receive_threshold:
                observed_partition = True
                break
            if attempt < attempts:
                self.sleep_fn(self.pre_check_retry_sleep_seconds)

        return {
            "status": "ready_to_execute" if observed_partition else ("not_firing" if saw_network_data else "unknown"),
            "namespace": namespace,
            "deployment": deployment,
            "network_receive_rate": receive_rate,
            "query": network_query,
            "missing_network_telemetry": saw_missing_network_telemetry,
            "should_execute": observed_partition,
            "attempts": attempts,
        }

    def post_check_network_partition(self, *, namespace: str, deployment: str) -> dict[str, object]:
        network_query = _cartservice_network_receive_query(
            namespace=namespace,
            deployment=deployment,
            rate_window=self.network_partition_rate_window,
        )
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        receive_rate: float | None = None
        ready_count = 0.0
        attempts = max(1, self.post_check_retry_attempts)
        recovered = False
        saw_network_data = False
        saw_missing_network_telemetry = False

        for attempt in range(1, attempts + 1):
            receive_value = self._query_value(network_query)
            ready_count = self._query_value(ready_query) or 0.0
            if receive_value is None:
                saw_missing_network_telemetry = True
                if attempt < attempts:
                    self.sleep_fn(self.post_check_retry_sleep_seconds)
                continue
            saw_network_data = True
            receive_rate = receive_value
            recovered = receive_rate >= self.network_partition_receive_threshold and ready_count > 0
            if recovered:
                break
            if attempt < attempts:
                self.sleep_fn(self.post_check_retry_sleep_seconds)

        return {
            "status": "recovered" if recovered else ("unrecovered" if saw_network_data else "unknown"),
            "namespace": namespace,
            "deployment": deployment,
            "network_receive_rate": receive_rate,
            "ready_count": ready_count,
            "attempts": attempts,
            "missing_network_telemetry": saw_missing_network_telemetry,
            "queries": {
                "network_receive_rate": network_query,
                "ready": ready_query,
            },
        }

    def _query_value(self, query: str) -> float | None:
        runner = self.query_runner
        if runner is not None:
            value = runner(query)
            return None if value is None else float(value)

        base_url = self.base_url or os.environ.get("PROMETHEUS_BASE_URL")
        if not base_url:
            raise EnvironmentError("PROMETHEUS_BASE_URL is required for Prometheus queries.")

        import httpx

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/v1/query",
                params={"query": query},
            )
            response.raise_for_status()
            payload = response.json()

        return _parse_query_value(payload)

    def query(self, query: str) -> dict[str, object]:
        try:
            value = self._query_value(query)
        except Exception as exc:
            return {
                "status": "failed",
                "query": query,
                "error": str(exc),
            }
        return {
            "status": "succeeded",
            "query": query,
            "value": value,
        }

    def range_query(
        self,
        *,
        query: str,
        start: str,
        end: str,
        step: str,
    ) -> dict[str, object]:
        runner = self.range_query_runner
        if runner is not None:
            try:
                samples = runner(query, start, end, step)
            except Exception as exc:
                return {
                    "status": "failed",
                    "query": query,
                    "start": start,
                    "end": end,
                    "step": step,
                    "error": str(exc),
                }
            return {
                "status": "succeeded",
                "query": query,
                "start": start,
                "end": end,
                "step": step,
                "samples": list(samples or []),
            }

        base_url = self.base_url or os.environ.get("PROMETHEUS_BASE_URL")
        if not base_url:
            return {
                "status": "failed",
                "query": query,
                "start": start,
                "end": end,
                "step": step,
                "error": "PROMETHEUS_BASE_URL is required for Prometheus range queries.",
            }

        import httpx

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(
                    f"{base_url.rstrip('/')}/api/v1/query_range",
                    params={
                        "query": query,
                        "start": start,
                        "end": end,
                        "step": step,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            return {
                "status": "succeeded",
                "query": query,
                "start": start,
                "end": end,
                "step": step,
                "samples": _parse_range_query_samples(payload),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "query": query,
                "start": start,
                "end": end,
                "step": step,
                "error": str(exc),
            }

    def raw_metric_snapshot(self, queries: dict[str, str]) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for name, query in queries.items():
            snapshot[name] = self.query(query)
        return snapshot


def _crashloop_query(*, namespace: str, deployment: str) -> str:
    return (
        "sum(kube_pod_container_status_waiting_reason"
        f'{{namespace="{namespace}",reason="CrashLoopBackOff",pod=~"{deployment}-.*"}})'
    )


def _recent_crashloop_query(*, namespace: str, deployment: str, lookback_window: str) -> str:
    return (
        "sum(max_over_time(kube_pod_container_status_waiting_reason"
        f'{{namespace="{namespace}",reason="CrashLoopBackOff",pod=~"{deployment}-.*"}}[{lookback_window}]))'
    )


def _ready_query(*, namespace: str, deployment: str) -> str:
    return (
        "sum(kube_pod_status_ready"
        f'{{namespace="{namespace}",condition="true",pod=~"{deployment}-.*"}})'
    )


def _frontend_cpu_query(*, namespace: str, deployment: str, rate_window: str) -> str:
    return (
        "sum(rate(container_cpu_usage_seconds_total"
        f'{{namespace="{namespace}",pod=~"{deployment}.*",container="server"}}[{rate_window}]))'
    )


def _frontend_cart_probe_query(*, namespace: str) -> str:
    return f'probe_success{{instance="http://frontend.{namespace}.svc.cluster.local/cart"}}'


def _cartservice_network_receive_query(*, namespace: str, deployment: str, rate_window: str) -> str:
    return (
        "sum(rate(container_network_receive_bytes_total"
        f'{{namespace="{namespace}",pod=~"{deployment}.*",interface="eth0"}}[{rate_window}]))'
    )


def _parse_query_value(payload: dict[str, object]) -> float | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Prometheus payload missing data")

    result = data.get("result")
    if not isinstance(result, list):
        raise ValueError("Prometheus payload missing result list")
    if not result:
        return None

    first = result[0]
    if not isinstance(first, dict):
        raise ValueError("Prometheus result item must be an object")

    value = first.get("value")
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Prometheus result item missing value pair")

    sample = value[1]
    if not isinstance(sample, str):
        raise ValueError("Prometheus sample value must be a string")

    return float(sample)


def _parse_range_query_samples(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Prometheus payload missing data")

    result = data.get("result")
    if not isinstance(result, list):
        raise ValueError("Prometheus payload missing result list")

    samples: list[dict[str, object]] = []
    for series in result:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric")
        values = series.get("values")
        if not isinstance(metric, dict) or not isinstance(values, list):
            continue
        samples.append(
            {
                "metric": dict(metric),
                "values": list(values),
            }
        )
    return samples
