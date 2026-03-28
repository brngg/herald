from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest

from services.alert_inbox import load_inbox_record, store_pending_alerts
from services.execution_worker import ExecutionWorkerClient
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import PrometheusClient
from workflows.operator_inbox import (
    ignore_inbox_artifact,
    run_terminal_inbox_flow,
    run_terminal_inbox_watch,
    start_investigation_for_artifact,
)


def _crashloop_payload(*, fingerprint: str = "crashloop123") -> dict[str, object]:
    return {
        "receiver": "default/herald-webhook-routing/herald-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HeraldCartserviceCrashLoopBackOff",
                    "incident_class": "crashloop",
                    "namespace": "default",
                    "pod": "cartservice-7d6b9f5bb4-abcde",
                    "container": "server",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "cartservice is in CrashLoopBackOff",
                    "description": "Pod cartservice is crash looping.",
                },
                "startsAt": "2026-03-23T20:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": fingerprint,
            }
        ],
        "groupLabels": {
            "alertname": "HeraldCartserviceCrashLoopBackOff",
            "incident_class": "crashloop",
            "namespace": "default",
        },
        "commonLabels": {
            "alertname": "HeraldCartserviceCrashLoopBackOff",
            "incident_class": "crashloop",
            "namespace": "default",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "cartservice is in CrashLoopBackOff",
        },
        "externalURL": "http://alertmanager",
        "version": "4",
        "groupKey": '{}/{namespace="default"}:{alertname="HeraldCartserviceCrashLoopBackOff"}',
        "truncatedAlerts": 0,
    }


def _worker_client(
    *,
    status: str = "succeeded",
    returncode: int = 0,
    stdout: str = "deployment.apps/cartservice rolled back\n",
    stderr: str = "",
) -> ExecutionWorkerClient:
    script = textwrap.dedent(
        f"""
        import json
        import sys

        dispatch = json.load(sys.stdin)
        command = ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"]
        result = {{
            "worker_id": dispatch["worker_id"],
            "action_id": dispatch["action_id"],
            "status": {status!r},
            "started_at": "2026-03-27T03:00:00+00:00",
            "finished_at": "2026-03-27T03:00:05+00:00",
            "command": command,
            "returncode": {returncode},
            "stdout": {stdout!r},
            "stderr": {stderr!r},
            "summary": "Agent completed the approved remediation." if {status!r} == "succeeded" else "Agent failed to complete the approved remediation.",
            "tool_transcript": [{{"step": 1, "tool_name": dispatch["action_type"]}}],
        }}
        print(json.dumps(result))
        """
    ).strip()
    return ExecutionWorkerClient(worker_command_builder=lambda _: [sys.executable, "-c", script])


def _output_collector(lines: list[str]):
    def _collect(message: str = "", *, flush: bool = False) -> None:
        lines.append(message)

    return _collect


def _input_iter(responses: list[str]):
    iterator = iter(responses)

    def _input(_: str = "") -> str:
        return next(iterator)

    return _input


class OperatorInboxIntegrationTest(unittest.TestCase):
    def test_multi_alert_artifacts_store_single_incident_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = _crashloop_payload()
            payload["alerts"].append(
                {
                    **payload["alerts"][0],
                    "fingerprint": "crashloop456",
                }
            )

            records = store_pending_alerts(payload, inbox_root=tmpdir)

            self.assertEqual(len(records), 2)
            first_result = start_investigation_for_artifact(records[0].artifact_dir)[1]
            second_result = start_investigation_for_artifact(records[1].artifact_dir)[1]
            self.assertEqual(first_result["incident"].incident_id, "crashloop123")
            self.assertEqual(second_result["incident"].incident_id, "crashloop456")

    def test_ignore_path_marks_artifact_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            record = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]

            updated = ignore_inbox_artifact(record.artifact_dir)

            reloaded = load_inbox_record(record.artifact_dir)
            self.assertEqual(updated.status, "ignored")
            self.assertEqual(reloaded.status, "ignored")
            self.assertEqual(reloaded.gate0_decision, "ignore")

    def test_investigate_path_creates_first_pass_artifact_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            record = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]

            updated, planning_result = start_investigation_for_artifact(record.artifact_dir)

            reloaded = load_inbox_record(record.artifact_dir)
            self.assertEqual(updated.status, "pending_execution_approval")
            self.assertEqual(reloaded.status, "pending_execution_approval")
            self.assertIsNotNone(reloaded.first_pass_artifact)
            self.assertIsNone(reloaded.final_result_artifact)
            self.assertEqual(planning_result["decision_trace"].final_state, "pending_approval")
            self.assertEqual(planning_result["decision_trace"].execution_result, {})

    def test_pending_alert_investigate_then_approve_or_reject_execution(self) -> None:
        for approval_choice, expected_approval, expected_final_state in (
            ("1", "approved", "recovered"),
            ("2", "rejected", "rejected"),
        ):
            with self.subTest(approval_choice=approval_choice):
                with tempfile.TemporaryDirectory() as tmpdir:
                    store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)
                    output_lines: list[str] = []

                    crashloop_queries = iter([1.0, 0.0])

                    def query_runner(query: str) -> float:
                        if "kube_pod_container_status_waiting_reason" in query:
                            return next(crashloop_queries)
                        if "kube_pod_status_ready" in query:
                            return 1.0
                        raise AssertionError(f"Unexpected query: {query}")

                    prometheus = PrometheusClient(query_runner=query_runner)
                    commands: list[list[str]] = []

                    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                        commands.append(list(command))
                        return subprocess.CompletedProcess(
                            args=list(command),
                            returncode=0,
                            stdout="ok",
                            stderr="",
                        )

                    result = run_terminal_inbox_flow(
                        inbox_root=tmpdir,
                        prometheus_client=prometheus,
                        kubernetes_client=KubernetesClient(runner=runner),
                        execution_worker_client=_worker_client(),
                        input_fn=_input_iter(["1", "1", approval_choice]),
                        output_fn=_output_collector(output_lines),
                    )

                    self.assertIsNotNone(result)
                    artifact = load_inbox_record(result["artifact"].artifact_dir)
                    self.assertEqual(artifact.status, "completed")
                    self.assertIsNotNone(artifact.first_pass_artifact)
                    self.assertIsNotNone(artifact.final_result_artifact)

                    final_result = result["final_result"]
                    self.assertEqual(
                        final_result["decision_trace"].human_approval,
                        expected_approval,
                    )
                    self.assertEqual(
                        final_result["decision_trace"].final_state,
                        expected_final_state,
                    )
                    if approval_choice == "1":
                        self.assertEqual(
                            final_result["decision_trace"].execution_result["status"],
                            "succeeded",
                        )
                        self.assertTrue(commands)
                    else:
                        self.assertEqual(
                            final_result["decision_trace"].execution_result["status"],
                            "not_executed",
                        )
                        self.assertEqual(commands, [])

    def test_watch_mode_processes_new_pending_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)
            output_lines: list[str] = []

            crashloop_queries = iter([1.0, 0.0])

            def query_runner(query: str) -> float:
                if "kube_pod_container_status_waiting_reason" in query:
                    return next(crashloop_queries)
                if "kube_pod_status_ready" in query:
                    return 1.0
                raise AssertionError(f"Unexpected query: {query}")

            processed = run_terminal_inbox_watch(
                inbox_root=tmpdir,
                prometheus_client=PrometheusClient(query_runner=query_runner),
                kubernetes_client=KubernetesClient(
                    runner=lambda command: subprocess.CompletedProcess(
                        args=list(command),
                        returncode=0,
                        stdout="ok",
                        stderr="",
                    )
                ),
                execution_worker_client=_worker_client(),
                input_fn=_input_iter(["1", "1"]),
                output_fn=_output_collector(output_lines),
                poll_interval_seconds=0.0,
                max_processed=1,
            )

            self.assertEqual(len(processed), 1)
            self.assertEqual(
                processed[0]["final_result"]["decision_trace"].final_state,
                "recovered",
            )
            self.assertTrue(any("Watching HERALD terminal inbox" in line for line in output_lines))
            self.assertTrue(any("New alert received." in line for line in output_lines))

    def test_watch_mode_prioritizes_pending_execution_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            resumable = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]
            waiting = store_pending_alerts(
                _crashloop_payload(fingerprint="crashloop999"),
                inbox_root=tmpdir,
            )[0]
            updated, _planning_result = start_investigation_for_artifact(resumable.artifact_dir)
            self.assertEqual(updated.status, "pending_execution_approval")

            processed = run_terminal_inbox_watch(
                inbox_root=tmpdir,
                prometheus_client=PrometheusClient(query_runner=lambda _: 0.0),
                input_fn=_input_iter(["2"]),
                output_fn=_output_collector([]),
                poll_interval_seconds=0.0,
                max_processed=1,
            )

            self.assertEqual(len(processed), 1)
            resumed_record = load_inbox_record(resumable.artifact_dir)
            waiting_record = load_inbox_record(waiting.artifact_dir)
            self.assertEqual(resumed_record.status, "completed")
            self.assertEqual(waiting_record.status, "pending_investigation")

    def test_watch_mode_keeps_running_after_record_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            broken = store_pending_alerts(_crashloop_payload(), inbox_root=tmpdir)[0]
            good = store_pending_alerts(
                _crashloop_payload(fingerprint="crashloop789"),
                inbox_root=tmpdir,
            )[0]

            start_investigation_for_artifact(broken.artifact_dir)
            broken_record = load_inbox_record(broken.artifact_dir)
            self.assertEqual(broken_record.status, "pending_execution_approval")
            self.assertIsNotNone(broken_record.first_pass_artifact)
            # Simulate a corrupted resumable artifact so Gate 1 raises.
            with open(broken_record.first_pass_artifact, "w", encoding="utf-8") as handle:
                handle.write("{}\n")

            output_lines: list[str] = []
            crashloop_queries = iter([1.0, 0.0])

            def query_runner(query: str) -> float:
                if "kube_pod_container_status_waiting_reason" in query:
                    return next(crashloop_queries)
                if "kube_pod_status_ready" in query:
                    return 1.0
                raise AssertionError(f"Unexpected query: {query}")

            processed = run_terminal_inbox_watch(
                inbox_root=tmpdir,
                prometheus_client=PrometheusClient(query_runner=query_runner),
                kubernetes_client=KubernetesClient(
                    runner=lambda command: subprocess.CompletedProcess(
                        args=list(command),
                        returncode=0,
                        stdout="ok",
                        stderr="",
                    )
                ),
                execution_worker_client=_worker_client(),
                input_fn=_input_iter(["1", "1"]),
                output_fn=_output_collector(output_lines),
                poll_interval_seconds=0.0,
                max_processed=1,
            )

            self.assertEqual(len(processed), 1)
            good_record = load_inbox_record(good.artifact_dir)
            self.assertEqual(good_record.status, "completed")
            self.assertTrue(
                any("will keep running" in line for line in output_lines),
                msg=output_lines,
            )


if __name__ == "__main__":
    unittest.main()
