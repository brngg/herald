from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class KubernetesClient:
    runner: CommandRunner | None = None

    def rollout_undo_deployment(self, *, namespace: str, deployment: str) -> dict[str, object]:
        command = [
            "kubectl",
            "rollout",
            "undo",
            f"deployment/{deployment}",
            "-n",
            namespace,
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "action_type": "rollout_undo_deployment",
            "namespace": namespace,
            "deployment": deployment,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def rollout_restart_deployment(self, *, namespace: str, deployment: str) -> dict[str, object]:
        command = [
            "kubectl",
            "rollout",
            "restart",
            f"deployment/{deployment}",
            "-n",
            namespace,
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "action_type": "rollout_restart_deployment",
            "namespace": namespace,
            "deployment": deployment,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        runner = self.runner or _default_runner
        return runner(command)


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )
