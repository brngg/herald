from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class KubernetesClient:
    runner: CommandRunner | None = None

    def get_resource_json(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            kind,
            name,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        payload = _parse_json_output(completed.stdout)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
            "resource": payload,
        }

    def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str | None = None,
    ) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-o",
            "json",
        ]
        if label_selector:
            command.extend(["-l", label_selector])
        completed = self._run(command)
        payload = _parse_json_output(completed.stdout)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "label_selector": label_selector,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
            "resource": payload,
        }

    def get_events(self, *, namespace: str) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            "events",
            "-n",
            namespace,
            "--sort-by=.lastTimestamp",
            "-o",
            "json",
        ]
        completed = self._run(command)
        payload = _parse_json_output(completed.stdout)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
            "resource": payload,
        }

    def get_pod_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: int = 100,
    ) -> dict[str, Any]:
        command = [
            "kubectl",
            "logs",
            pod,
            "-n",
            namespace,
            f"--tail={tail_lines}",
        ]
        if container:
            command.extend(["-c", container])
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

    def get_rollout_history(self, *, namespace: str, deployment: str) -> dict[str, Any]:
        command = [
            "kubectl",
            "rollout",
            "history",
            f"deployment/{deployment}",
            "-n",
            namespace,
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "deployment": deployment,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

    def list_replica_sets(
        self,
        *,
        namespace: str,
        label_selector: str | None = None,
    ) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            "replicasets",
            "-n",
            namespace,
            "-o",
            "json",
        ]
        if label_selector:
            command.extend(["-l", label_selector])
        completed = self._run(command)
        payload = _parse_json_output(completed.stdout)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "label_selector": label_selector,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
            "resource": payload,
        }

    def get_service_endpoints(self, *, namespace: str, service: str) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            "endpoints",
            service,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        payload = _parse_json_output(completed.stdout)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "service": service,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
            "resource": payload,
        }

    def get_deployment_availability(self, *, namespace: str, deployment: str) -> dict[str, Any]:
        command = [
            "kubectl",
            "get",
            "deployment",
            deployment,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        available_replicas = 0
        ready_replicas = 0
        observed_generation = 0

        if completed.returncode == 0 and completed.stdout:
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                status = payload.get("status")
                if isinstance(status, dict):
                    available_replicas = _as_non_negative_int(status.get("availableReplicas"))
                    ready_replicas = _as_non_negative_int(status.get("readyReplicas"))
                    observed_generation = _as_non_negative_int(status.get("observedGeneration"))

        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "namespace": namespace,
            "deployment": deployment,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "available_replicas": available_replicas,
            "ready_replicas": ready_replicas,
            "observed_generation": observed_generation,
            "is_available": completed.returncode == 0 and available_replicas > 0 and ready_replicas > 0,
        }

    def get_deployment_context(self, *, namespace: str, deployment: str) -> dict[str, object]:
        command = [
            "kubectl",
            "get",
            "deployment",
            deployment,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

    def get_stresschaos(self, *, namespace: str, name: str) -> dict[str, object]:
        command = [
            "kubectl",
            "get",
            "stresschaos",
            name,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

    def get_networkchaos(self, *, namespace: str, name: str) -> dict[str, object]:
        command = [
            "kubectl",
            "get",
            "networkchaos",
            name,
            "-n",
            namespace,
            "-o",
            "json",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

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

    def scale_deployment(self, *, namespace: str, deployment: str, replicas: int) -> dict[str, object]:
        command = [
            "kubectl",
            "scale",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--replicas={replicas}",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "action_type": "scale_deployment",
            "namespace": namespace,
            "deployment": deployment,
            "replicas": replicas,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def delete_stresschaos(self, *, namespace: str, name: str) -> dict[str, object]:
        command = [
            "kubectl",
            "delete",
            "stresschaos",
            name,
            "-n",
            namespace,
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "action_type": "delete_stresschaos",
            "namespace": namespace,
            "name": name,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def delete_networkchaos(self, *, namespace: str, name: str) -> dict[str, object]:
        command = [
            "kubectl",
            "delete",
            "networkchaos",
            name,
            "-n",
            namespace,
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "action_type": "delete_networkchaos",
            "namespace": namespace,
            "name": name,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def get_rollout_status(
        self,
        *,
        namespace: str,
        deployment: str,
        timeout_seconds: int = 5,
    ) -> dict[str, object]:
        command = [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output": completed.stdout,
        }

    def wait_for_rollout_deployment(
        self,
        *,
        namespace: str,
        deployment: str,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        command = [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ]
        completed = self._run(command)
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
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


def _as_non_negative_int(value: Any) -> int:
    if isinstance(value, int):
        return max(0, value)
    return 0


def _parse_json_output(stdout: str) -> dict[str, Any] | None:
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
