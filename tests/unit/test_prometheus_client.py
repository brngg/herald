from __future__ import annotations

import unittest

from services.observability.prometheus import PrometheusClient


class PrometheusClientTest(unittest.TestCase):
    def test_query_returns_successful_snapshot_value(self) -> None:
        client = PrometheusClient(query_runner=lambda _: 3.5)

        result = client.query("up")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["value"], 3.5)

    def test_range_query_uses_injected_runner(self) -> None:
        client = PrometheusClient(
            range_query_runner=lambda query, start, end, step: [
                {
                    "metric": {"__name__": query, "step": step},
                    "values": [[start, "1"], [end, "2"]],
                }
            ]
        )

        result = client.range_query(
            query="up",
            start="2026-03-29T20:00:00+00:00",
            end="2026-03-29T20:05:00+00:00",
            step="30s",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["samples"][0]["metric"]["step"], "30s")

    def test_raw_metric_snapshot_collects_each_query(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 1.0 if query == "up" else 0.0,
        )

        snapshot = client.raw_metric_snapshot({"ready": "up", "incident_signal": "probe_success"})

        self.assertEqual(snapshot["ready"]["status"], "succeeded")
        self.assertEqual(snapshot["ready"]["value"], 1.0)
        self.assertEqual(snapshot["incident_signal"]["value"], 0.0)

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

    def test_pre_check_deployment_readiness_shortfall_reports_ready_to_execute(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 0.0 if "kube_pod_status_ready" in query else 1.0,
            pre_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.pre_check_deployment_readiness_shortfall(
            namespace="default",
            deployment="frontend",
            min_ready_count=2,
        )

        self.assertEqual(result["status"], "ready_to_execute")
        self.assertEqual(result["ready_count"], 0.0)
        self.assertEqual(result["min_ready_count"], 2)
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
        self.assertIn("[1m]", result["queries"]["cpu"])

    def test_post_check_deployment_readiness_target_reports_recovered(self) -> None:
        client = PrometheusClient(
            query_runner=lambda query: 2.0 if "kube_pod_status_ready" in query else 0.0,
            post_check_retry_attempts=1,
            sleep_fn=lambda _: None,
        )

        result = client.post_check_deployment_readiness_target(
            namespace="default",
            deployment="frontend",
            min_ready_count=2,
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["ready_count"], 2.0)
        self.assertEqual(result["min_ready_count"], 2)

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
