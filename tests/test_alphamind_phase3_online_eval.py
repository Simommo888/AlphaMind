import importlib.util
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_phase3_online_eval.py"
spec = importlib.util.spec_from_file_location("alphamind_phase3_online_eval", MODULE_PATH)
online_eval = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = online_eval
spec.loader.exec_module(online_eval)


class Phase3OnlineEvalTests(unittest.TestCase):
    def test_vllm_runtime_skips_ollama_unload_boundary(self):
        result = online_eval.unload_chat_model(
            {
                "PHASE3_DIRECT_CHAT_RUNTIME": "vllm",
                "PHASE3_DIRECT_CHAT_MODEL": "qwen3-14b",
                "PHASE3_DIRECT_CHAT_URL": "http://localhost:8000/v1",
            },
            timeout=1,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["action"], "skip_unload")
        self.assertEqual(result["model"], "qwen3-14b")

    def test_retrieval_metrics_use_real_model_reranked_order(self):
        entry = online_eval.base.GroundTruthEntry(
            query_id="q1",
            gt_status="complete",
            relevant_doc_ids=["doc-a"],
            reference_must_include_doc_ids=["doc-a"],
        )
        raw = [
            {"id": "b", "content": "noise", "metadata": {"doc_id": "doc-b"}},
            {"id": "a", "content": "evidence", "metadata": {"doc_id": "doc-a"}},
        ]
        calls = []

        def search(_query):
            return raw, {"http_status": 200, "elapsed_ms": 5, "payload_sha256": "abc"}

        def rerank(query, candidates, top_k):
            calls.append((query, len(candidates), top_k))
            return [dict(candidates[1], rerank_score=0.9), dict(candidates[0], rerank_score=0.1)]

        result = online_eval.evaluate_reranked_retrieval(
            query={"id": "q1", "text": "target"},
            entry=entry,
            search=search,
            reranker=rerank,
            knowledge_to_doc={},
            alias_to_doc={},
            top_k=10,
        )
        self.assertEqual(calls, [("target", 2, 10)])
        self.assertEqual(result["metrics"]["recall@10"], 1.0)
        self.assertEqual(result["metrics"]["reciprocal_rank"], 1.0)
        self.assertEqual(result["retrieved_docs"][0]["doc_id"], "doc-a")
        self.assertEqual(result["rerank"]["mode"], "model_api")

    def test_direct_chat_maps_only_actual_source_citations(self):
        entry = online_eval.base.GroundTruthEntry(query_id="q1", gt_status="complete", relevant_doc_ids=["doc-b"])
        candidates = [
            {"content": "source a", "metadata": {"doc_id": "doc-a"}},
            {"content": "source b", "metadata": {"doc_id": "doc-b"}},
        ]
        response = {"choices": [{"message": {"content": "结论来自第二个来源 [S2]"}}]}
        with patch.object(online_eval.base, "http_json", return_value=(200, response)):
            result = online_eval.evaluate_direct_chat(
                query={"id": "q1", "text": "问题"}, entry=entry, candidates=candidates,
                env={"PHASE3_DIRECT_CHAT_MODEL": "model"}, knowledge_to_doc={}, alias_to_doc={},
                timeout=10, include_answer=False,
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reference_doc_ids"], ["doc-b"])

    def test_positive_direct_chat_rejects_uncited_refusal(self):
        entry = online_eval.base.GroundTruthEntry(query_id="q1", gt_status="complete", relevant_doc_ids=["doc-a"])
        response = {"choices": [{"message": {"content": "未检索到足够证据，不能编造"}}]}
        with patch.object(online_eval.base, "http_json", return_value=(200, response)):
            result = online_eval.evaluate_direct_chat(
                query={"id": "q1", "text": "问题"}, entry=entry,
                candidates=[{"content": "source", "metadata": {"doc_id": "doc-a"}}],
                env={"PHASE3_DIRECT_CHAT_MODEL": "model"}, knowledge_to_doc={}, alias_to_doc={},
                timeout=10, include_answer=False,
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reference_doc_ids"], [])

    def test_gate_is_fail_closed_for_missing_or_below_threshold_metrics(self):
        policy = {
            "min_recall_at_10": 0.9,
            "min_mrr": 0.9,
            "min_citation_doc_recall": 0.95,
            "no_evidence_refusal_rate": 1.0,
        }
        passing = {
            "mean_recall@10": 1.0,
            "mean_reciprocal_rank": 1.0,
            "mean_citation_doc_recall": 1.0,
            "no_evidence_refusal_rate": 1.0,
        }
        assessment = online_eval.assess_gate(passing, policy, rerank_calls=15, evaluated_queries=15, expected_queries=15)
        self.assertEqual(assessment["status"], "pass")

        missing = dict(passing)
        missing.pop("mean_reciprocal_rank")
        self.assertEqual(
            online_eval.assess_gate(missing, policy, rerank_calls=15, evaluated_queries=15, expected_queries=15)["status"],
            "fail",
        )
        below = dict(passing, **{"mean_citation_doc_recall": 0.5})
        self.assertEqual(
            online_eval.assess_gate(below, policy, rerank_calls=15, evaluated_queries=15, expected_queries=15)["status"],
            "fail",
        )
        self.assertEqual(
            online_eval.assess_gate(passing, policy, rerank_calls=0, evaluated_queries=15, expected_queries=15)["status"],
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
