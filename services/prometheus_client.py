from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable


PrometheusQueryRunner = Callable[[str], float]


@dataclass(slots=True)
class PrometheusClient:
    base_url: str | None = None
    query_runner: PrometheusQueryRunner | None = None
    timeout_seconds: float = 10.0

    def pre_check_crashloop(self, *, namespace: str, deployment: str) -> dict[str, object]:
        crashloop_query = _crashloop_query(namespace=namespace, deployment=deployment)
        crashloop_count = self._query(crashloop_query)
        return {
            "status": "ready_to_execute" if crashloop_count > 0 else "not_firing",
            "namespace": namespace,
            "deployment": deployment,
            "crashloop_count": crashloop_count,
            "query": crashloop_query,
            "should_execute": crashloop_count > 0,
        }

    def post_check_crashloop(self, *, namespace: str, deployment: str) -> dict[str, object]:
        crashloop_query = _crashloop_query(namespace=namespace, deployment=deployment)
        ready_query = _ready_query(namespace=namespace, deployment=deployment)
        crashloop_count = self._query(crashloop_query)
        ready_count = self._query(ready_query)
        recovered = crashloop_count == 0 and ready_count > 0
        return {
            "status": "recovered" if recovered else "unrecovered",
            "namespace": namespace,
            "deployment": deployment,
            "crashloop_count": crashloop_count,
            "ready_count": ready_count,
            "queries": {
                "crashloop": crashloop_query,
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


def _ready_query(*, namespace: str, deployment: str) -> str:
    return (
        "sum(kube_pod_status_ready"
        f'{{namespace="{namespace}",condition="true",pod=~"{deployment}-.*"}})'
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
