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

    def test_rrf_rejects_negative_weights(self):
        with self.assertRaises(ValueError):
            retrieval.weighted_rrf({"dense": []}, {"dense": -1})


if __name__ == "__main__":
    unittest.main()
