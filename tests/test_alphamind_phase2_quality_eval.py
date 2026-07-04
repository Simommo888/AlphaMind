import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alphamind_phase2_quality_eval as evaluator


class Phase2QualityEvalTests(unittest.TestCase):
    def test_loads_current_phase2_placeholder_schema(self):
        queries = [
            {"id": "q1", "text": "问题", "type": "metadata_recall"},
            {"id": "q2", "text": "无证据", "type": "no_evidence_guardrail"},
        ]
        payload = {
            "queries": [
                {"id": "q1", "relevant_doc_ids": [], "must_contain": ["信捷电气"]},
                {
                    "id": "q2",
                    "relevant_doc_ids": [],
                    "must_contain": ["未检索到足够证据"],
                    "must_not_contain_fabricated_numbers": True,
                },
            ]
        }

        entries, validation = evaluator.load_ground_truth_entries(queries, payload, {})

        self.assertEqual(entries["q1"].gt_status, "placeholder")
        self.assertFalse(entries["q1"].scorable_retrieval)
        self.assertTrue(entries["q2"].is_no_evidence)
        self.assertIn("q1", validation.placeholder_queries)
        self.assertNotIn("q2", validation.placeholder_queries)

    def test_loads_legacy_smoke_schema(self):
        queries = [{"id": "q_legacy", "text": "德龙激光 EPS"}]
        payload = {
            "ground_truth": {
                "q_legacy": [
                    {
                        "doc_id": "德龙报告.pdf",
                        "chunk_ids": [],
                        "must_contain": ["德龙激光", "EPS"],
                    }
                ]
            }
        }
        alias_to_doc = {"德龙报告": "doc_delong"}

        entries, validation = evaluator.load_ground_truth_entries(queries, payload, alias_to_doc)

        self.assertEqual(validation.status, "complete")
        self.assertEqual(entries["q_legacy"].relevant_doc_ids, ["doc_delong"])
        self.assertEqual(entries["q_legacy"].answer_must_contain, ["EPS", "德龙激光"])

    def test_collect_reference_ids_maps_knowledge_and_nested_metadata(self):
        references = [
            {
                "response_type": "references",
                "knowledge_references": [
                    {"knowledge_id": "kid-1"},
                    {"metadata": {"doc_id": "raw-doc"}},
                    {"metadata": {"file_path": "D:/AlphaMind/报告A.pdf"}},
                ],
            }
        ]
        knowledge_to_doc = {"kid-1": "doc-from-knowledge"}
        alias_to_doc = {"raw-doc": "doc-from-metadata", "报告A": "doc-from-file"}

        ids = evaluator.collect_reference_ids(references, knowledge_to_doc, alias_to_doc)

        self.assertEqual(
            set(ids["doc_ids"]),
            {"doc-from-knowledge", "doc-from-metadata", "doc-from-file"},
        )
        self.assertEqual(ids["knowledge_ids"], ["kid-1"])

    def test_no_evidence_answer_checks_pass_and_fail(self):
        entry = evaluator.GroundTruthEntry(
            query_id="q_no_evidence",
            gt_status="no_evidence",
            expected_behavior="no_evidence",
            requires_references=False,
            answer_must_contain_any=["未检索到足够证据", "无法根据现有资料"],
            answer_must_not_match=[r"目标价[^。\n]{0,30}\d+(?:\.\d+)?\s*元", r"EPS[^。\n]{0,30}\d+(?:\.\d+)?"],
        )

        passing = evaluator.evaluate_answer(entry, "未检索到足够证据，不能编造目标价或 EPS。")
        passing_no_evidence = evaluator.evaluate_no_evidence(entry, passing)
        self.assertEqual(passing["status"], "pass")
        self.assertEqual(passing_no_evidence["status"], "pass")

        failing = evaluator.evaluate_answer(entry, "可以确定目标价为 12 元，EPS 为 0.50。")
        failing_no_evidence = evaluator.evaluate_no_evidence(entry, failing)
        self.assertEqual(failing["status"], "fail")
        self.assertEqual(failing_no_evidence["status"], "fail")
        self.assertTrue(failing_no_evidence["fabrication_violations"])

    def test_placeholder_metrics_are_excluded_from_aggregate(self):
        query_results = [
            {
                "ground_truth": {"gt_status": "placeholder", "is_no_evidence": False},
                "retrieval": {"metrics": {}},
                "chat": None,
                "citation_checks": {"status": "skipped"},
                "answer_checks": {"status": "skipped"},
            },
            {
                "ground_truth": {"gt_status": "complete", "is_no_evidence": False},
                "retrieval": {"metrics": {"recall@10": 1.0, "precision@1": 1.0}},
                "chat": None,
                "citation_checks": {"status": "skipped"},
                "answer_checks": {"status": "skipped"},
            },
        ]

        aggregate = evaluator.aggregate_results(query_results)

        self.assertEqual(aggregate["mean_recall@10"], 1.0)
        self.assertEqual(aggregate["mean_precision@1"], 1.0)
        self.assertIsNone(aggregate["mean_recall@5"])

    def test_filter_expansion_skips_control_fields(self):
        query = {
            "text": "对比信捷电气的观点",
            "expected_filters": {
                "company": "信捷电气",
                "topic": "超快激光",
                "requires_references": True,
                "requires_metric_fields": ["period", "value", "unit"],
                "min_distinct_doc_ids": 2,
            },
        }

        expanded = evaluator.expanded_query(query, use_filter_expansion=True)

        self.assertIn("超快激光", expanded)
        self.assertNotIn("period", expanded)
        self.assertNotIn("value", expanded)
        self.assertNotIn("unit", expanded)
        self.assertNotIn(" True", expanded)
        self.assertNotRegex(expanded, r"\s2(?:\s|$)")

    def test_retrieval_warn_is_quality_warning_not_setup_failure(self):
        validation = evaluator.GroundTruthValidation(
            status="complete",
            missing_ground_truth=[],
            placeholder_queries=[],
            partial_queries=[],
            regex_errors={},
            warnings={},
            action_items=[],
        )
        query_results = [
            {
                "query_id": "q_no_results",
                "status": "warn",
                "retrieval": {
                    "status": "warn",
                    "http_status": 200,
                    "metrics": {"recall@10": 0.0},
                    "error": "no search results",
                },
                "chat": None,
                "citation_checks": {"status": "skipped"},
                "answer_checks": {"status": "skipped"},
                "no_evidence_checks": {"status": "skipped"},
            }
        ]
        target_status = {"status": "warn", "checks": {}}

        summary = evaluator.validation_summary(
            validation,
            query_results,
            target_status,
            strict=False,
            validate_only=False,
        )

        self.assertEqual(summary["setup_status"], "pass")
        self.assertEqual(summary["quality_status"], "warn")
        self.assertEqual(summary["http_errors"], {})

    def test_soft_query_failures_do_not_force_quality_failure(self):
        validation = evaluator.GroundTruthValidation(
            status="placeholder",
            missing_ground_truth=[],
            placeholder_queries=["q_placeholder"],
            partial_queries=["q_partial"],
            regex_errors={},
            warnings={},
            action_items=[],
        )
        query_results = [
            {
                "query_id": "q_placeholder",
                "status": "warn",
                "retrieval": None,
                "chat": {"status": "pass", "http_status": 200, "error": ""},
                "citation_checks": {"status": "skipped"},
                "answer_checks": {"status": "fail", "missing_must_contain": ["占位词"]},
                "no_evidence_checks": {"status": "skipped"},
            },
            {
                "query_id": "q_partial",
                "status": "warn",
                "retrieval": None,
                "chat": {"status": "pass", "http_status": 200, "error": ""},
                "citation_checks": {"status": "fail", "missing_doc_ids": ["doc"]},
                "answer_checks": {"status": "fail", "missing_must_contain": ["部分词"]},
                "no_evidence_checks": {"status": "skipped"},
            },
        ]
        target_status = {"status": "warn", "checks": {}}

        summary = evaluator.validation_summary(
            validation,
            query_results,
            target_status,
            strict=False,
            validate_only=False,
        )

        self.assertEqual(summary["setup_status"], "pass")
        self.assertEqual(summary["quality_status"], "warn")
        self.assertEqual(summary["citation_failures"], {})
        self.assertEqual(summary["answer_failures"], {})


if __name__ == "__main__":
    unittest.main()
