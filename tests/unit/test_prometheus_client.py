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
        self.assertIn("[1m]", result["query"])

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
        self.assertIn("[1m]", result["queries"]["cpu"])

    def test_cpu_query_window_is_configurable(self) -> None:
        seen_queries: list[str] = []

        def query_runner(query: str) -> float:
            seen_queries.append(query)
            return 0.08

        client = PrometheusClient(
            query_runner=query_runner,
            pre_check_retry_attempts=1,
            cpu_check_rate_window="2m",
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_cpu_saturation(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "ready_to_execute")
        self.assertTrue(seen_queries)
        self.assertIn("[2m]", seen_queries[0])

    def test_pre_check_bad_config_reports_ready_to_execute_when_probe_is_failing(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 0.0 if "probe_success" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_bad_config(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "ready_to_execute")
        self.assertEqual(result["probe_success"], 0.0)
        self.assertTrue(result["should_execute"])
        self.assertIn('probe_success{instance="http://frontend.default.svc.cluster.local/cart"}', result["query"])

    def test_pre_check_bad_config_treats_missing_probe_series_as_unknown(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: None if "probe_success" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_bad_config(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["probe_success"])
        self.assertFalse(result["should_execute"])
        self.assertTrue(result["missing_probe_telemetry"])

    def test_post_check_bad_config_reports_recovered(self) -> None:
        values = iter([1.0, 1.0])

        def query_runner(_: str) -> float:
            return next(values)

        client = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_bad_config(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["probe_success"], 1.0)
        self.assertEqual(result["ready_count"], 1.0)
        self.assertIn('probe_success{instance="http://frontend.default.svc.cluster.local/cart"}', result["queries"]["probe_success"])

    def test_post_check_bad_config_treats_missing_probe_series_as_unknown(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: None if "probe_success" in query else 1.0,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_bad_config(namespace="default", deployment="frontend")

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["probe_success"])
        self.assertTrue(result["missing_probe_telemetry"])
        self.assertEqual(result["ready_count"], 1.0)

    def test_pre_check_network_partition_reports_ready_to_execute_when_receive_rate_is_low(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 0.0 if "container_network_receive_bytes_total" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_network_partition(namespace="default", deployment="cartservice")

        self.assertEqual(result["status"], "ready_to_execute")
        self.assertEqual(result["network_receive_rate"], 0.0)
        self.assertTrue(result["should_execute"])
        self.assertIn("container_network_receive_bytes_total", result["query"])
        self.assertIn("[5m]", result["query"])

    def test_pre_check_network_partition_treats_missing_series_as_unknown(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: None if "container_network_receive_bytes_total" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_network_partition(namespace="default", deployment="cartservice")

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["network_receive_rate"])
        self.assertFalse(result["should_execute"])
        self.assertTrue(result["missing_network_telemetry"])

    def test_post_check_network_partition_reports_recovered(self) -> None:
        values = iter([150.0, 1.0])

        def query_runner(_: str) -> float:
            return next(values)

        client = PrometheusClient(
            query_runner=query_runner,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_network_partition(namespace="default", deployment="cartservice")

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["network_receive_rate"], 150.0)
        self.assertEqual(result["ready_count"], 1.0)
        self.assertIn("container_network_receive_bytes_total", result["queries"]["network_receive_rate"])

    def test_post_check_network_partition_treats_missing_series_as_unknown(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: None if "container_network_receive_bytes_total" in query else 1.0,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_network_partition(namespace="default", deployment="cartservice")

        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["network_receive_rate"])
        self.assertTrue(result["missing_network_telemetry"])
        self.assertEqual(result["ready_count"], 1.0)


if __name__ == "__main__":
    unittest.main()
