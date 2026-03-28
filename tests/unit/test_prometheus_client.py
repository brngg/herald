from __future__ import annotations

import unittest

from services.prometheus_client import PrometheusClient


class PrometheusClientTest(unittest.TestCase):
    def test_pre_check_cpu_saturation_reports_ready_to_execute(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 0.08 if "container_cpu_usage_seconds_total" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_cpu_saturation(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "ready_to_execute")
        self.assertEqual(result["cpu_usage"], 0.08)
        self.assertTrue(result["should_execute"])

    def test_post_check_cpu_saturation_reports_recovered(self) -> None:
        values = iter([0.02, 1.0])

        def query_runner(_: str) -> float:
            return next(values)

        client = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_cpu_saturation(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["cpu_usage"], 0.02)
        self.assertEqual(result["ready_count"], 1.0)


if __name__ == "__main__":
    unittest.main()
