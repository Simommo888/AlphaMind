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
        self.assertIn("SUPPORTED_BY", plan.cypher)

    def test_unknown_intent_fails_closed(self):
        with self.assertRaises(ValueError):
            graph.build_query_plan("arbitrary_cypher", company_a="甲公司")

    def test_empty_company_fails_closed(self):
        with self.assertRaises(ValueError):
            graph.build_query_plan("institution_executive_network", company_a="")


if __name__ == "__main__":
    unittest.main()
