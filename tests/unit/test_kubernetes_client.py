from __future__ import annotations

import subprocess
import unittest

from services.infra.kubernetes.client import KubernetesClient


class KubernetesClientTest(unittest.TestCase):
    def test_get_resource_json_parses_kubectl_json_output(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='{"metadata": {"name": "frontend"}}',
                stderr="",
            )

        client = KubernetesClient(runner=runner)

        result = client.get_resource_json(namespace="default", kind="deployment", name="frontend")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["resource"]["metadata"]["name"], "frontend")
        self.assertEqual(
            commands,
            [["kubectl", "get", "deployment", "frontend", "-n", "default", "-o", "json"]],
        )

    def test_get_pod_logs_includes_container_and_tail(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        client = KubernetesClient(runner=runner)

        result = client.get_pod_logs(
            namespace="default",
            pod="frontend-abcde",
            container="server",
            tail_lines=25,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "ok")
        self.assertEqual(
            commands,
            [["kubectl", "logs", "frontend-abcde", "-n", "default", "--tail=25", "-c", "server"]],
        )

    def test_delete_stresschaos_uses_expected_command(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
                stderr="",
            )

        client = KubernetesClient(runner=runner)

        result = client.delete_stresschaos(namespace="default", name="frontend-cpu-saturation")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            commands,
            [["kubectl", "delete", "stresschaos", "frontend-cpu-saturation", "-n", "default"]],
        )

    def test_scale_deployment_uses_expected_command(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='deployment.apps/frontend scaled\n',
                stderr="",
            )

        client = KubernetesClient(runner=runner)

        result = client.scale_deployment(namespace="default", deployment="frontend", replicas=2)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            commands,
            [["kubectl", "scale", "deployment/frontend", "-n", "default", "--replicas=2"]],
        )

    def test_delete_networkchaos_uses_expected_command(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='networkchaos.chaos-mesh.org "frontend-to-cartservice-partition" deleted\n',
                stderr="",
            )

        client = KubernetesClient(runner=runner)

        result = client.delete_networkchaos(
            namespace="default",
            name="frontend-to-cartservice-partition",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            commands,
            [["kubectl", "delete", "networkchaos", "frontend-to-cartservice-partition", "-n", "default"]],
        )


if __name__ == "__main__":
    unittest.main()
