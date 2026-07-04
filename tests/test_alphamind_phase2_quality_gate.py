import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alphamind_phase2_quality_gate as gate


class Phase2QualityGateTests(unittest.TestCase):
    def test_build_command_plan_contains_phase2_strict_steps(self):
        args = SimpleNamespace(
            env_file=".env.phase2-advanced",
            out_dir="dataset/phase2_runs",
            chat_timeout=180.0,
            skip_healthcheck=False,
            skip_tests=False,
        )

        plan = gate.build_command_plan(args)

        self.assertEqual(
            [step.name for step in plan],
            ["healthcheck", "validate_only", "retrieval_eval", "chat_eval", "unit_tests"],
        )
        commands = {step.name: step.command for step in plan}
        self.assertIn("alphamind_healthcheck.py", commands["healthcheck"])
        self.assertIn("--phase phase2-advanced", commands["healthcheck"])
        self.assertIn("--strict", commands["validate_only"])
        self.assertIn("--mode retrieval", commands["retrieval_eval"])
        self.assertIn("phase2_retrieval_eval_latest.json", commands["retrieval_eval"])
        self.assertIn("--mode chat", commands["chat_eval"])
        self.assertIn("--chat-timeout 180", commands["chat_eval"])
        self.assertIn("-m unittest discover", commands["unit_tests"])
        self.assertIn("test_alphamind_phase2_quality*.py", commands["unit_tests"])
        self.assertNotIn("pytest", commands["unit_tests"])

    def test_classify_model_account_error_detects_arrearage(self):
        text = "EmbedBatch API error: Access denied code=Arrearage"

        classification = gate.classify_failure(text)

        self.assertEqual(classification, "model_account_error")

    def test_run_quality_gate_stops_after_first_failure(self):
        plan = [
            gate.CommandSpec("first", "cmd-one", timeout=1),
            gate.CommandSpec("second", "cmd-two", timeout=1),
        ]

        with patch.object(gate, "run_command", return_value=gate.StepResult("first", "fail", 1, 7, "boom", "cmd-one")) as run:
            result = gate.run_quality_gate(plan, keep_going=False)

        self.assertEqual(result.status, "fail")
        self.assertEqual([step.name for step in result.steps], ["first"])
        run.assert_called_once()

    def test_run_quality_gate_can_continue_and_preserve_failure(self):
        plan = [
            gate.CommandSpec("first", "cmd-one", timeout=1),
            gate.CommandSpec("second", "cmd-two", timeout=1),
        ]
        results = [
            gate.StepResult("first", "fail", 1, 7, "boom", "cmd-one"),
            gate.StepResult("second", "pass", 0, 9, "ok", "cmd-two"),
        ]

        with patch.object(gate, "run_command", side_effect=results):
            result = gate.run_quality_gate(plan, keep_going=True)

        self.assertEqual(result.status, "fail")
        self.assertEqual([step.name for step in result.steps], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
