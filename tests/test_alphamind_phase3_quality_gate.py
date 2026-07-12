import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load("alphamind_phase3_quality_gate", "alphamind_phase3_quality_gate.py")
load_test = load("alphamind_phase3_load_test", "alphamind_phase3_load_test.py")


class Phase3QualityGateTests(unittest.TestCase):
    def test_validate_bundle_accepts_repository_phase3_configs(self):
        issues = gate.validate_bundle(
            ROOT / "dataset" / "alphamind_process_config_phase3_precision.json",
            ROOT / "dataset" / "alphamind_retrieval_config_phase3_precision.json",
            ROOT / "dataset" / "alphamind_financial_lexicon_phase3.json",
        )
        self.assertEqual(issues, [])

    def test_validate_bundle_rejects_unbalanced_weights_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            process = {"graph_enabled": True, "extract_config": {"tags": ["KNOWN"], "nodes": [{"name": "Company"}], "relations": [{"node1": "Company", "type": "UNLISTED", "node2": "Company"}]}}
            retrieval = {"fusion": {"weights": {"dense": 1, "sparse": 1}}, "answer_policy": {"financial_metric_fields": ["value"]}}
            lexicon = {"entries": []}
            for name, payload in [("p.json", process), ("r.json", retrieval), ("l.json", lexicon)]:
                (base / name).write_text(json.dumps(payload), encoding="utf-8")
            issues = gate.validate_bundle(base / "p.json", base / "r.json", base / "l.json")
            self.assertTrue(any("weights" in issue for issue in issues))
            self.assertTrue(any("Evidence" in issue for issue in issues))
            self.assertTrue(any("lexicon" in issue for issue in issues))
            self.assertTrue(any("tags" in issue and "UNLISTED" in issue for issue in issues))

    def test_offline_plan_contains_real_pdf_and_tests_but_no_live_checks(self):
        args = argparse.Namespace(
            sample_pdf="sample.pdf", out_dir="out", max_pages=25,
            process_config="p.json", retrieval_config="r.json", lexicon="l.json",
            env_file=".env.phase3-precision", live=False, concurrency=4, requests=20,
            timeout=0,
        )
        names = [step.name for step in gate.build_command_plan(args)]
        self.assertEqual(names, ["config_validation", "phase3_unit_tests", "real_pdf_parse"])

    def test_live_plan_adds_health_and_load_checks(self):
        args = argparse.Namespace(
            sample_pdf="sample.pdf", out_dir="out", max_pages=25,
            process_config="p.json", retrieval_config="r.json", lexicon="l.json",
            env_file=".env.phase3-precision", live=True, concurrency=4, requests=20,
            timeout=0,
        )
        names = [step.name for step in gate.build_command_plan(args)]
        self.assertIn("healthcheck", names)
        self.assertIn("load_test", names)

    def test_percentile_and_summary_are_deterministic(self):
        self.assertEqual(load_test.percentile([10, 20, 30, 40], 95), 40)
        summary = load_test.summarize([10, 20], successes=1, failures=1)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["error_rate"], 0.5)
        self.assertEqual(summary["p95_ms"], 20)


if __name__ == "__main__":
    unittest.main()
