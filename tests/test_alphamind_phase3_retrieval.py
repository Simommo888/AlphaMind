import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_phase3_retrieval.py"
spec = importlib.util.spec_from_file_location("alphamind_phase3_retrieval", MODULE_PATH)
retrieval = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = retrieval
spec.loader.exec_module(retrieval)


class Phase3RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.lexicon = [
            {"canonical": "net_profit_attributable", "terms": ["归母净利润", "净利润"]},
            {"canonical": "net_profit_attributable_excl_non_recurring", "terms": ["扣非净利润", "扣除非经常性损益后的净利润"]},
            {"canonical": "ROE", "terms": ["ROE", "净资产收益率"]},
            {"canonical": "private_placement_discount", "terms": ["定增倒挂", "定向增发价格倒挂"]},
        ]

    def test_expands_financial_aliases_with_deduplication_and_limit(self):
        expanded = retrieval.expand_query("这家公司扣非净利润和ROE如何？", self.lexicon, max_expansions=8)
        self.assertEqual(expanded[0], "这家公司扣非净利润和ROE如何？")
        self.assertIn("net_profit_attributable_excl_non_recurring", expanded)
        self.assertNotIn("net_profit_attributable", expanded)
        self.assertIn("净资产收益率", expanded)
        self.assertEqual(len(expanded), len(set(expanded)))
        self.assertLessEqual(len(expanded), 9)

    def test_unknown_query_is_not_polluted(self):
        self.assertEqual(retrieval.expand_query("天气如何", self.lexicon), ["天气如何"])

    def test_weighted_rrf_fuses_dense_sparse_graph_and_deduplicates(self):
        channels = {
            "dense": [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}],
            "sparse": [{"id": "b", "content": "B"}, {"id": "c", "content": "C"}],
            "graph": [{"id": "b", "content": "B"}],
        }
        fused = retrieval.weighted_rrf(channels, {"dense": 0.5, "sparse": 0.3, "graph": 0.2}, k=10)
        self.assertEqual([item["id"] for item in fused], ["b", "a", "c"])
        self.assertEqual(set(fused[0]["fusion_channels"]), {"dense", "sparse", "graph"})
        self.assertGreater(fused[0]["fusion_score"], fused[1]["fusion_score"])

    def test_financial_rerank_boosts_exact_terms_and_graph_hits(self):
        candidates = [
            {"id": "a", "content": "普通描述", "fusion_score": 0.1, "fusion_channels": ["dense"]},
            {"id": "b", "content": "每10股派发现金红利1.02元", "fusion_score": 0.09, "fusion_channels": ["dense", "graph"]},
        ]
        ranked = retrieval.financial_rerank(
            "现金红利是多少", candidates, 2,
            exact_terms=["现金红利", "每10股"], exact_boost=0.2, graph_boost=0.15,
        )
        self.assertEqual([item["id"] for item in ranked], ["b", "a"])
        self.assertGreater(ranked[0]["rerank_score"], ranked[1]["rerank_score"])

    def test_execute_retrieval_runs_expansion_three_channels_and_rerank(self):
        calls = []

        def search(query, channel):
            calls.append((query, channel))
            if channel == "dense":
                return [{"id": "a", "content": "dense"}, {"id": "b", "content": "shared"}]
            return [{"id": "b", "content": "shared"}, {"id": "c", "content": "sparse"}]

        def graph_rank(terms, candidates):
            self.assertIn("ROE", terms)
            return [next(item for item in candidates if item["id"] == "b")]

        def rerank(query, candidates, top_k):
            self.assertEqual(query, "ROE如何")
            return list(reversed(candidates[:top_k]))

        config = {
            "query_expansion": {"enabled": True, "max_expansions": 4},
            "fusion": {"weights": {"dense": 0.5, "sparse": 0.3, "graph": 0.2}, "rrf_k": 10, "deduplicate_by": "chunk_id"},
            "rerank": {"enabled": True, "top_k": 3, "require_source_lineage": False},
        }
        results = retrieval.execute_retrieval("ROE如何", config, self.lexicon, search, graph_rank, rerank)
        self.assertTrue(any(channel == "dense" for _, channel in calls))
        self.assertTrue(any(channel == "sparse" for _, channel in calls))
        self.assertEqual(set(next(item for item in results if item["id"] == "b")["fusion_channels"]), {"dense", "sparse", "graph"})
        self.assertEqual(len(results), 3)

    def test_execute_retrieval_fails_closed_without_required_graph_or_reranker(self):
        config = {
            "query_expansion": {"enabled": False},
            "fusion": {"weights": {"dense": 0.5, "sparse": 0.3, "graph": 0.2}, "rrf_k": 10},
            "rerank": {"enabled": True, "top_k": 3},
        }
        search = lambda query, channel: [{"id": "a", "content": "x"}]
        with self.assertRaises(ValueError):
            retrieval.execute_retrieval("q", config, self.lexicon, search, None, None)

    def test_rrf_rejects_negative_weights(self):
        with self.assertRaises(ValueError):
            retrieval.weighted_rrf({"dense": []}, {"dense": -1})


if __name__ == "__main__":
    unittest.main()
