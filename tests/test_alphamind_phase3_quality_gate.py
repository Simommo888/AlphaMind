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

    def test_repository_graph_config_supports_query_relation_types_and_edge_evidence(self):
        process = json.loads((ROOT / "dataset" / "alphamind_process_config_phase3_precision.json").read_text(encoding="utf-8"))
        relations = process["extract_config"]["relations"]
        by_type = {relation["type"]: relation for relation in relations}
        for relation_type in ("HAS_CUSTOMER", "HAS_SUPPLIER", "HAS_EXECUTIVE", "AFFILIATED_WITH", "APPOINTED", "NOMINATED"):
            self.assertIn(relation_type, by_type)
            self.assertTrue({"source_doc_id", "source_chunk_id", "page"}.issubset(set(by_type[relation_type].get("attributes", []))))

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
        plan = gate.build_command_plan(args)
        names = [step.name for step in plan]
        self.assertIn("healthcheck", names)
        self.assertIn("phase3_retrieval_e2e", names)
        self.assertIn("fixed_retrieval_eval", names)
        online_argv = next(step.argv for step in plan if step.name == "fixed_retrieval_eval")
        self.assertEqual(online_argv[online_argv.index("--rerank-batch-size") + 1], "10")
        self.assertIn("load_test", names)
        self.assertLess(names.index("load_test"), names.index("fixed_retrieval_eval"))
        load_argv = next(step.argv for step in plan if step.name == "load_test")
        self.assertIn("POST", load_argv)
        self.assertTrue(any("/hybrid-search" in value for value in load_argv))
        self.assertIn("--payload", load_argv)
        self.assertIn("--expect-json-items", load_argv)
        self.assertEqual(load_argv[load_argv.index("--warmup-requests") + 1], "1")
        self.assertFalse(any(value.endswith("/health") for value in load_argv))

    def test_eval_suite_rejects_structural_only_queries_without_scoring_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(json.dumps({
                "live_gate": {"text": "q", "answer_must_contain": ["x"]},
                "queries": [{"id": f"q{i}", "text": "q"} for i in range(15)],
            }), encoding="utf-8")
            issues = gate.validate_eval_suite(path)
        self.assertTrue(any("ground truth" in issue for issue in issues))

    def test_load_probe_resolves_env_file_key_and_requires_non_empty_json_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "phase3.env"
            env_file.write_text("WEKNORA_API_KEY=file-key\n", encoding="utf-8")
            self.assertEqual(load_test.resolve_api_key("", env_file, {"WEKNORA_API_KEY": "stale-key"}), "file-key")
        self.assertTrue(load_test.json_has_items({"data": [{"id": "chunk"}]}))
        self.assertTrue(load_test.json_has_items({"data": {"items": [{"id": "chunk"}]}}))
        self.assertFalse(load_test.json_has_items({"data": {"items": []}}))

    def test_percentile_and_summary_are_deterministic(self):
        self.assertEqual(load_test.percentile([10, 20, 30, 40], 95), 40)
        summary = load_test.summarize([10, 20], successes=1, failures=1)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["error_rate"], 0.5)
        self.assertEqual(summary["p95_ms"], 20)


if __name__ == "__main__":
    unittest.main()
