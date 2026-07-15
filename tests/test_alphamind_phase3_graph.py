import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_phase3_graph.py"
spec = importlib.util.spec_from_file_location("alphamind_phase3_graph", MODULE_PATH)
graph = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = graph
spec.loader.exec_module(graph)


class Phase3GraphTests(unittest.TestCase):
    def test_supply_chain_overlap_is_parameterized_and_evidence_bearing(self):
        plan = graph.build_query_plan("supply_chain_overlap", company_a="甲公司", company_b="乙公司")
        self.assertIn("$company_a", plan.cypher)
        self.assertIn("$company_b", plan.cypher)
        self.assertNotIn("甲公司", plan.cypher)
        self.assertNotIn("乙公司", plan.cypher)
        self.assertEqual(plan.params["company_a"], "甲公司")
        self.assertIn("source_doc_id", plan.cypher)
        self.assertIn("ac.source_doc_id AS customer_source_doc_id", plan.cypher)
        self.assertIn("bs.source_doc_id AS supplier_source_doc_id", plan.cypher)
        self.assertIn("ac.source_doc_id IS NOT NULL", plan.cypher)
        self.assertIn("bs.source_doc_id IS NOT NULL", plan.cypher)
        self.assertIn("HAS_CUSTOMER", plan.cypher)
        self.assertIn("HAS_SUPPLIER", plan.cypher)
        self.assertIn("SUPPLIES_TO", plan.cypher)
        self.assertIn("AlphaMindEntity", plan.cypher)
        self.assertIn("customer_evidence_quote", plan.cypher)
        self.assertIn("supplier_evidence_quote", plan.cypher)
        self.assertIn("semantic_source", plan.cypher)
        self.assertEqual(plan.params["relation_graph_source"], "alphamind_relation_graph")
        self.assertNotIn("alphamind_relation_graph", plan.cypher)
        self.assertGreaterEqual(plan.hops, 3)
        payload = graph.build_neo4j_payload(plan)
        statement = payload["statements"][0]
        self.assertEqual(statement["parameters"]["company_a"], "甲公司")
        self.assertNotIn("甲公司", statement["statement"])
        self.assertEqual(statement["resultDataContents"], ["row"])

    def test_metric_compare_requires_metric_and_periods(self):
        with self.assertRaises(ValueError):
            graph.build_query_plan("metric_period_compare", company_a="甲公司")
        plan = graph.build_query_plan("metric_period_compare", company_a="甲公司", metric="ROE", periods=["2023A", "2024A"])
        self.assertEqual(plan.params["periods"], ["2023A", "2024A"])
        self.assertNotIn("ROE", plan.cypher)

    def test_institution_executive_path_is_supported(self):
        plan = graph.build_query_plan("institution_executive_network", company_a="甲公司")
        self.assertIn("Executive", plan.cypher)
        self.assertIn("Institution", plan.cypher)
        self.assertIn("ce.source_doc_id AS executive_source_doc_id", plan.cypher)
        self.assertIn("ie.source_doc_id AS institution_source_doc_id", plan.cypher)
        self.assertIn("ce.source_doc_id IS NOT NULL", plan.cypher)
        self.assertIn("ie.source_doc_id IS NOT NULL", plan.cypher)

    def test_graph_chunk_rank_plan_is_parameterized_and_scoped(self):
        plan = graph.build_graph_chunk_rank_plan("knowledge-id", ["ROE", "净资产收益率"], limit=20)
        self.assertIn("$knowledge_id", plan.cypher)
        self.assertIn("$terms", plan.cypher)
        self.assertNotIn("knowledge-id", plan.cypher)
        self.assertEqual(plan.params["knowledge_id"], "knowledge-id")
        self.assertEqual(plan.params["terms"], ["ROE", "净资产收益率"])

    def test_unknown_intent_fails_closed(self):
        with self.assertRaises(ValueError):
            graph.build_query_plan("arbitrary_cypher", company_a="甲公司")

    def test_empty_company_fails_closed(self):
        with self.assertRaises(ValueError):
            graph.build_query_plan("institution_executive_network", company_a="")


if __name__ == "__main__":
    unittest.main()
