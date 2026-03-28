from __future__ import annotations

import subprocess
import unittest

from services.kubernetes_client import KubernetesClient


class KubernetesClientTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
