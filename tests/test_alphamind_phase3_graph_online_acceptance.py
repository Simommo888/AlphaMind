import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "alphamind_graph_online_acceptance.py"


def load_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("alphamind_graph_online_acceptance", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


COLUMNS = [
    "company_a",
    "company_b",
    "shared_counterparty",
    "customer_rank",
    "supplier_rank",
    "customer_source_doc_id",
    "customer_source_chunk_id",
    "customer_page",
    "customer_evidence_quote",
    "supplier_source_doc_id",
    "supplier_source_chunk_id",
    "supplier_page",
    "supplier_evidence_quote",
    "semantic_source",
]

TRACEABLE_ROW = [
    "三安光电",
    "中芯国际",
    "中微公司",
    None,
    None,
    "local-md:first",
    "local-line:first",
    0,
    "三安光电向中微公司供应设备。",
    "local-md:second",
    "local-line:second",
    0,
    "中微公司向中芯国际供应设备。",
    "relation_graph_bridge",
]


class AlphaMindGraphOnlineAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.acceptance = load_module()

    def test_traceable_non_empty_supply_chain_result_passes(self):
        report = self.acceptance.evaluate_result(
            {"intent": "supply_chain_overlap", "columns": COLUMNS, "rows": [TRACEABLE_ROW]},
            company_a="三安光电",
            company_b="中芯国际",
            generated_at="2026-07-12T00:00:00Z",
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["row_count"], 1)
        self.assertTrue(report["checks"]["non_empty"])
        self.assertTrue(report["checks"]["all_rows_traceable"])
        self.assertEqual(report["evidence_rows"][0]["shared_counterparty"], "中微公司")
        self.assertEqual(report["evidence_rows"][0]["customer_page"], 0)

    def test_empty_or_incomplete_supply_chain_result_fails_closed(self):
        empty = self.acceptance.evaluate_result(
            {"intent": "supply_chain_overlap", "columns": COLUMNS, "rows": []},
            company_a="甲",
            company_b="乙",
        )
        incomplete_row = list(TRACEABLE_ROW)
        incomplete_row[COLUMNS.index("supplier_evidence_quote")] = ""
        incomplete = self.acceptance.evaluate_result(
            {"intent": "supply_chain_overlap", "columns": COLUMNS, "rows": [incomplete_row]},
            company_a="甲",
            company_b="乙",
        )

        self.assertEqual(empty["status"], "fail")
        self.assertFalse(empty["checks"]["non_empty"])
        self.assertEqual(incomplete["status"], "fail")
        self.assertFalse(incomplete["checks"]["all_rows_traceable"])

    def test_run_acceptance_writes_report_without_credentials(self):
        calls = []

        def fake_executor(plan, **kwargs):
            calls.append((plan, kwargs))
            return {"intent": plan.intent, "columns": COLUMNS, "rows": [TRACEABLE_ROW]}

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "acceptance.json"
            report = self.acceptance.run_acceptance(
                company_a="三安光电",
                company_b="中芯国际",
                report_path=report_path,
                neo4j_url="http://localhost:17474",
                username="neo4j",
                password="do-not-write",
                database="neo4j",
                executor=fake_executor,
            )
            saved = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "pass")
        self.assertEqual(saved["status"], "pass")
        self.assertEqual(calls[0][0].params["company_a"], "三安光电")
        self.assertEqual(calls[0][1]["password"], "do-not-write")
        self.assertNotIn("do-not-write", json.dumps(saved, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
