from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from evaluation.run_scenario import run_scenarios


class RunScenarioIntegrationTest(unittest.TestCase):
    def test_run_scenarios_writes_artifacts_and_metrics(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        scenario_paths = [
            repo_root / "evaluation" / "scenarios" / "crashloop_recovered.json",
            repo_root / "evaluation" / "scenarios" / "crashloop_worker_failure.json",
            repo_root / "evaluation" / "scenarios" / "frontend_cpu_recovered.json",
            repo_root / "evaluation" / "scenarios" / "frontend_bad_config_recovered.json",
            repo_root / "evaluation" / "scenarios" / "frontend_bad_config_rejected.json",
            repo_root / "evaluation" / "scenarios" / "frontend_bad_config_worker_failure.json",
            repo_root / "evaluation" / "scenarios" / "frontend_cartservice_network_partition_recovered.json",
            repo_root / "evaluation" / "scenarios" / "frontend_cartservice_network_partition_rejected.json",
            repo_root / "evaluation" / "scenarios" / "frontend_cartservice_network_partition_worker_failure.json",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            summary = run_scenarios(
                scenario_paths=scenario_paths,
                runs_per_scenario=1,
                output_dir=Path(temp_dir),
            )

            self.assertEqual(summary["runs_per_scenario"], 1)
            self.assertEqual(summary["metrics"]["total_runs"], 9)
            self.assertTrue((Path(temp_dir) / "crashloop_recovered-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "crashloop_worker_failure-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_cpu_recovered-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_bad_config_recovered-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_bad_config_rejected-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_bad_config_worker_failure-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_cartservice_network_partition_recovered-run-01.json").exists())
            self.assertTrue((Path(temp_dir) / "frontend_cartservice_network_partition_rejected-run-01.json").exists())
            self.assertTrue(
                (Path(temp_dir) / "frontend_cartservice_network_partition_worker_failure-run-01.json").exists()
            )
            self.assertTrue((Path(temp_dir) / "metrics-summary.json").exists())
            self.assertTrue((Path(temp_dir) / "metrics-summary.md").exists())
            with (Path(temp_dir) / "crashloop_recovered-run-01.json").open("r", encoding="utf-8") as handle:
                artifact = json.load(handle)
            self.assertEqual(artifact["result"]["engine_mode"], "v2_execute")
            self.assertIn("decision_trace_timeline", artifact["result"])
            self.assertEqual(artifact["result"]["decision_trace_timeline"][0]["node_name"], "observe")


if __name__ == "__main__":
    unittest.main()
