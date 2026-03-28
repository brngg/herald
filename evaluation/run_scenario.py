from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from evaluation.metrics import compute_metrics, render_metrics_markdown
from services.execution_worker import ExecutionWorkerClient
from services.judge_llm import JudgeLLMResult
from services.kubernetes_client import KubernetesClient
from services.prometheus_client import PrometheusClient
from workflows.recovery_workflow import run_recovery_from_payload


def run_scenarios(
    *,
    scenario_paths: list[Path],
    runs_per_scenario: int,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_artifacts: list[dict[str, Any]] = []

    for scenario_path in scenario_paths:
        scenario = _load_json_file(scenario_path)
        scenario_id = str(scenario["scenario_id"])
        for run_index in range(1, runs_per_scenario + 1):
            artifact = _run_single_scenario(
                scenario=scenario,
                scenario_path=scenario_path,
                run_index=run_index,
            )
            run_artifacts.append(artifact)
            artifact_path = output_dir / f"{scenario_id}-run-{run_index:02d}.json"
            artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    metrics = compute_metrics(run_artifacts)
    summary = {
        "scenarios": [str(path) for path in scenario_paths],
        "runs_per_scenario": runs_per_scenario,
        "metrics": metrics,
        "artifacts": [artifact["artifact_path"] for artifact in run_artifacts],
    }
    (output_dir / "metrics-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "metrics-summary.md").write_text(render_metrics_markdown(metrics), encoding="utf-8")
    return summary


def _run_single_scenario(
    *,
    scenario: dict[str, Any],
    scenario_path: Path,
    run_index: int,
) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    payload_path = repo_root / str(scenario["payload_file"])
    payload = _load_json_file(payload_path)

    prometheus = _build_prometheus_client(scenario)
    kubernetes = _build_kubernetes_client(scenario)
    worker_client = _build_worker_client(scenario)
    judge_llm = _build_judge_llm(scenario)

    result = run_recovery_from_payload(
        payload,
        approve_action_id=scenario.get("approve_action_id"),
        reject_action_id=scenario.get("reject_action_id"),
        judge_llm=judge_llm,
        kubernetes_client=kubernetes,
        prometheus_client=prometheus,
        execution_worker_client=worker_client,
    )
    jsonable_result = _to_jsonable(result)
    return {
        "scenario_id": scenario["scenario_id"],
        "scenario_path": str(scenario_path),
        "run_index": run_index,
        "expected": scenario["expected"],
        "result": jsonable_result,
        "artifact_path": f"{scenario['scenario_id']}-run-{run_index:02d}.json",
    }


def _build_prometheus_client(scenario: dict[str, Any]) -> PrometheusClient:
    prometheus_config = dict(scenario.get("prometheus", {}))
    crashloop_values = list(prometheus_config.get("crashloop_values", []))
    ready_values = list(prometheus_config.get("ready_values", []))
    cpu_values = list(prometheus_config.get("cpu_values", []))
    default_crashloop = float(prometheus_config.get("default_crashloop", 0.0))
    default_ready = float(prometheus_config.get("default_ready", 0.0))
    default_cpu = float(prometheus_config.get("default_cpu", 0.0))

    def query_runner(query: str) -> float:
        if "kube_pod_container_status_waiting_reason" in query:
            if crashloop_values:
                return float(crashloop_values.pop(0))
            return default_crashloop
        if "container_cpu_usage_seconds_total" in query:
            if cpu_values:
                return float(cpu_values.pop(0))
            return default_cpu
        if "kube_pod_status_ready" in query:
            if ready_values:
                return float(ready_values.pop(0))
            return default_ready
        raise AssertionError(f"Unexpected query: {query}")

    return PrometheusClient(
        query_runner=query_runner,
        pre_check_retry_attempts=int(prometheus_config.get("pre_check_retry_attempts", 1)),
        pre_check_retry_sleep_seconds=0.0,
        post_check_retry_attempts=int(prometheus_config.get("post_check_retry_attempts", 1)),
        post_check_retry_sleep_seconds=0.0,
        sleep_fn=lambda _: None,
    )


def _build_kubernetes_client(scenario: dict[str, Any]) -> KubernetesClient:
    kubernetes_config = dict(scenario.get("kubernetes", {}))
    rollout_status = dict(kubernetes_config.get("rollout_status", _completed_process_payload(0, "ok\n", "")))
    deployment_availability = dict(
        kubernetes_config.get(
            "deployment_availability",
            _completed_process_payload(
                0,
                '{"status":{"availableReplicas":1,"readyReplicas":1,"observedGeneration":1}}',
                "",
            ),
        )
    )
    stresschaos_status = dict(
        kubernetes_config.get(
            "stresschaos",
            _completed_process_payload(0, '{"metadata":{"name":"frontend-cpu-saturation"}}', ""),
        )
    )

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["kubectl", "rollout", "status"]:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=int(rollout_status["returncode"]),
                stdout=str(rollout_status["stdout"]),
                stderr=str(rollout_status["stderr"]),
            )
        if command[:3] == ["kubectl", "get", "deployment"]:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=int(deployment_availability["returncode"]),
                stdout=str(deployment_availability["stdout"]),
                stderr=str(deployment_availability["stderr"]),
            )
        if command[:3] == ["kubectl", "get", "stresschaos"]:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=int(stresschaos_status["returncode"]),
                stdout=str(stresschaos_status["stdout"]),
                stderr=str(stresschaos_status["stderr"]),
            )
        if command[:3] == ["kubectl", "rollout", "undo"]:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="deployment.apps/cartservice rolled back\n",
                stderr="",
            )
        if command[:3] == ["kubectl", "delete", "stresschaos"]:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout='stresschaos.chaos-mesh.org "frontend-cpu-saturation" deleted\n',
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    return KubernetesClient(runner=runner)


def _build_worker_client(scenario: dict[str, Any]) -> ExecutionWorkerClient:
    worker_result = dict(
        scenario.get(
            "worker_result",
            {
                "status": "succeeded",
                "returncode": 0,
                "stdout": "deployment.apps/cartservice rolled back\n",
                "stderr": "",
                "summary": "Agent completed the approved remediation.",
            },
        )
    )

    script = f"""
import json
import sys

dispatch = json.load(sys.stdin)
command = ["kubectl", "rollout", "undo", "deployment/cartservice", "-n", "default"]
if dispatch["action_type"] == "rollout_restart_deployment":
    command = ["kubectl", "rollout", "restart", "deployment/cartservice", "-n", "default"]
if dispatch["action_type"] == "delete_stresschaos":
    command = ["kubectl", "delete", "stresschaos", dispatch["parameters"]["name"], "-n", dispatch["parameters"]["namespace"]]
result = {{
    "worker_id": dispatch["worker_id"],
    "action_id": dispatch["action_id"],
    "status": {worker_result['status']!r},
    "started_at": "2026-03-27T03:00:00+00:00",
    "finished_at": "2026-03-27T03:00:05+00:00",
    "command": command,
    "returncode": {int(worker_result['returncode'])},
    "stdout": {str(worker_result['stdout'])!r},
    "stderr": {str(worker_result['stderr'])!r},
    "summary": {str(worker_result.get('summary', 'Agent completed the approved remediation.'))!r},
    "tool_transcript": [{{"step": 1, "tool_name": dispatch["action_type"]}}],
}}
print(json.dumps(result))
"""

    return ExecutionWorkerClient(
        worker_command_builder=lambda _: [sys.executable, "-c", script.strip()],
    )


def _build_judge_llm(scenario: dict[str, Any]) -> object | None:
    judge_verdict = scenario.get("judge_verdict")
    if judge_verdict != "fail":
        return None

    class FailingJudgeLLM:
        def evaluate(self, **_: object) -> JudgeLLMResult:
            return JudgeLLMResult(
                verdict="fail",
                reason=str(scenario.get("judge_reason", "Scenario forced judge halt.")),
            )

    return FailingJudgeLLM()


def _completed_process_payload(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    return {"returncode": returncode, "stdout": stdout, "stderr": stderr}


def _load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object in {path}")
    return payload


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _default_output_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "evaluation" / "results" / "latest"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay HERALD scenarios and compute evaluation metrics.")
    parser.add_argument("--scenario", action="append", required=True, help="Path to a scenario JSON file.")
    parser.add_argument("--runs", type=int, default=1, help="Number of times to replay each scenario.")
    parser.add_argument("--output-dir", default=str(_default_output_dir()))
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    scenario_paths = [Path(item).resolve() for item in args.scenario]
    summary = run_scenarios(
        scenario_paths=scenario_paths,
        runs_per_scenario=args.runs,
        output_dir=Path(args.output_dir).resolve(),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
