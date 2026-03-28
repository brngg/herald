from __future__ import annotations

import os
from dataclasses import dataclass
import time
from typing import Callable


PrometheusQueryRunner = Callable[[str], float]
SleepFn = Callable[[float], None]


@dataclass(slots=True)
class PrometheusClient:
    base_url: str | None = None
    query_runner: PrometheusQueryRunner | None = None
    timeout_seconds: float = 10.0
    pre_check_retry_attempts: int = 3
    pre_check_retry_sleep_seconds: float = 2.0
    pre_check_lookback_window: str = "2m"
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
            crashloop_count = self._query(crashloop_query)
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
            crashloop_count = self._query(crashloop_query)
            ready_count = self._query(ready_query)
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
        cpu_query = _frontend_cpu_query(namespace=namespace, deployment=deployment)
        cpu_usage = 0.0
        attempts = max(1, self.pre_check_retry_attempts)

        for attempt in range(1, attempts + 1):
            cpu_usage = self._query(cpu_query)
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
        cpu_query = _frontend_cpu_query(namespace=namespace, deployment=deployment)
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        cpu_usage = 0.0
        ready_count = 0.0
        attempts = max(1, self.post_check_retry_attempts)
        recovered = False

        for attempt in range(1, attempts + 1):
            cpu_usage = self._query(cpu_query)
            ready_count = self._query(ready_query)
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

    def _query(self, query: str) -> float:
        runner = self.query_runner
        if runner is not None:
            return float(runner(query))

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


def _frontend_cpu_query(*, namespace: str, deployment: str) -> str:
    return (
        "sum(rate(container_cpu_usage_seconds_total"
        f'{{namespace="{namespace}",pod=~"{deployment}.*",container="server"}}[5m]))'
    )


def _parse_query_value(payload: dict[str, object]) -> float:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Prometheus payload missing data")

    result = data.get("result")
    if not isinstance(result, list):
        raise ValueError("Prometheus payload missing result list")
    if not result:
        return 0.0

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
