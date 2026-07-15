import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_relation_graph.py"


def load_module():
    spec = importlib.util.spec_from_file_location("alphamind_relation_graph", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AlphaMindRelationGraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = load_module()

    def test_classifies_industry_into_sector_and_chain_stage(self):
        self.assertEqual(self.graph.classify_sector("光伏加工设备"), "新能源")
        self.assertEqual(self.graph.classify_chain_stage("光伏加工设备"), "中游制造")
        self.assertEqual(self.graph.classify_sector("半导体材料"), "科技")
        self.assertEqual(self.graph.classify_chain_stage("半导体材料"), "上游资源材料")

    def test_builds_taxonomy_and_customer_relation_with_line_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "企业WPS转换md"
            (root / "光伏加工设备").mkdir(parents=True)
            (root / "光伏电池组件").mkdir(parents=True)
            (root / "光伏加工设备" / "奥特维.md").write_text(
                "# 奥特维\n## 3.1 产品\n### 客户\n公司已与晶科能源建立了良好的业务合作关系。\n",
                encoding="utf-8",
            )
            (root / "光伏电池组件" / "晶科能源.md").write_text("# 晶科能源\n", encoding="utf-8")

            result = self.graph.build_graph([root])

        node_ids = {node["id"] for node in result["nodes"]}
        self.assertIn("sector:新能源", node_ids)
        self.assertIn("industry:光伏加工设备", node_ids)
        self.assertIn("company:奥特维", node_ids)
        self.assertIn("company:晶科能源", node_ids)
        relation = next(edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO")
        self.assertEqual(relation["source"], "company:奥特维")
        self.assertEqual(relation["target"], "company:晶科能源")
        self.assertEqual(relation["evidence"][0]["line"], 4)
        self.assertIn("晶科能源", relation["evidence"][0]["quote"])
        self.assertEqual(self.graph.validate_graph(result), [])

    def test_supplier_relation_has_correct_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "企业md"
            (root / "消费电子").mkdir(parents=True)
            (root / "光伏材料").mkdir(parents=True)
            (root / "消费电子" / "TCL电子.md").write_text(
                "# TCL电子\n### 采购\nTCL中环是TCL电子的主要供应商。\n",
                encoding="utf-8",
            )
            (root / "光伏材料" / "TCL中环.md").write_text("# TCL中环\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO")
        self.assertEqual(relation["source"], "company:TCL中环")
        self.assertEqual(relation["target"], "company:TCL电子")
        self.assertEqual(relation["evidence"][0]["method"], "explicit_supplier")

    def test_company_is_target_supplier_keeps_source_to_target_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "电池").mkdir()
            (root / "汽车").mkdir()
            (root / "电池" / "翔丰华.md").write_text(
                "# 翔丰华\n### 客户\n公司目前是比亚迪等知名公司的供应商。\n",
                encoding="utf-8",
            )
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO")
        self.assertEqual(relation["source"], "company:翔丰华")
        self.assertEqual(relation["target"], "company:比亚迪")

    def test_mixed_supplier_and_customer_sentence_uses_target_local_supplier_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "半导体").mkdir()
            (root / "半导体" / "乐鑫科技.md").write_text(
                "# 乐鑫科技\n公司的供应商主要以台积电为代表，兆易创新为其主要闪存供应商。2）公司采用直销模式，直销客户多为物联网厂商。\n",
                encoding="utf-8",
            )
            (root / "半导体" / "兆易创新.md").write_text("# 兆易创新\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO")
        self.assertEqual(relation["source"], "company:兆易创新")
        self.assertEqual(relation["target"], "company:乐鑫科技")

    def test_product_comparison_does_not_make_application_customer_a_competitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "封装").mkdir()
            (root / "设备" / "中科飞测.md").write_text(
                "# 中科飞测\n检测设备与国际竞品整体性能相当，在华天科技等先进封装厂商产线上实现应用。\n",
                encoding="utf-8",
            )
            (root / "封装" / "华天科技.md").write_text("# 华天科技\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "COMPETES_WITH" for edge in result["edges"]))

    def test_negated_customer_relation_is_not_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "汽车").mkdir()
            (root / "设备" / "甲设备.md").write_text("# 甲设备\n公司与比亚迪不存在客户关系。\n", encoding="utf-8")
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "SUPPLIES_TO" for edge in result["edges"]))

    def test_competitor_statement_under_customer_heading_remains_competition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "汽车").mkdir()
            (root / "设备" / "甲设备.md").write_text(
                "# 甲设备\n### 客户\n公司将比亚迪列为主要竞争对手。\n",
                encoding="utf-8",
            )
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["target"] == "company:比亚迪")
        self.assertEqual(relation["type"], "COMPETES_WITH")

    def test_third_party_company_suffix_is_not_treated_as_document_company_pronoun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "电池").mkdir()
            (root / "汽车").mkdir()
            (root / "电池" / "国轩高科.md").write_text("# 国轩高科\n易事特公司向比亚迪供货。\n", encoding="utf-8")
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "SUPPLIES_TO" for edge in result["edges"]))

    def test_third_party_relationship_is_not_attributed_to_document_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "电池").mkdir()
            (root / "设备").mkdir()
            (root / "电池" / "国轩高科.md").write_text(
                "# 国轩高科\n华为与易事特在多领域展开合作。\n",
                encoding="utf-8",
            )
            (root / "设备" / "易事特.md").write_text("# 易事特\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "PARTNERS_WITH" for edge in result["edges"]))

    def test_control_direction_uses_shareholder_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "油服").mkdir()
            (root / "石油").mkdir()
            (root / "油服" / "中海油服.md").write_text(
                "# 中海油服\n公司控股股东是中国海油，中国海油持有公司50.53%的股权。\n",
                encoding="utf-8",
            )
            (root / "石油" / "中国海油.md").write_text("# 中国海油\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["type"] == "CONTROLS")
        self.assertEqual(relation["source"], "company:中国海油")
        self.assertEqual(relation["target"], "company:中海油服")

    def test_nearest_explicit_relationship_wins_over_unrelated_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "电子").mkdir()
            (root / "平台").mkdir()
            (root / "电子" / "伊戈尔.md").write_text(
                "# 伊戈尔\n有竞争对手在送样，但是公司跟阿里巴巴共同参与了研发。\n",
                encoding="utf-8",
            )
            (root / "平台" / "阿里巴巴.md").write_text("# 阿里巴巴\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["target"] == "company:阿里巴巴")
        self.assertEqual(relation["type"], "PARTNERS_WITH")

    def test_subsidiary_word_does_not_control_unrelated_company_earlier_in_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "设备" / "奥特维.md").write_text(
                "# 奥特维\n晶盛机电生产单晶炉，公司子公司松瓷机电生产加料机。\n",
                encoding="utf-8",
            )
            (root / "设备" / "晶盛机电.md").write_text("# 晶盛机电\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "CONTROLS" for edge in result["edges"]))

    def test_subsidiary_supplier_does_not_control_its_customer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "零部件").mkdir()
            (root / "汽车").mkdir()
            (root / "零部件" / "超捷股份.md").write_text(
                "# 超捷股份\n公司子公司上海易扣为比亚迪提供解决方案。\n",
                encoding="utf-8",
            )
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "CONTROLS" for edge in result["edges"]))

    def test_minority_investment_is_not_mislabeled_as_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "材料").mkdir()
            (root / "材料" / "久立特材.md").write_text(
                "# 久立特材\n公司战略投资永兴材料，持有永兴材料7.15%股权。\n",
                encoding="utf-8",
            )
            (root / "材料" / "永兴材料.md").write_text("# 永兴材料\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "CONTROLS" for edge in result["edges"]))

    def test_nearby_other_controlling_shareholder_does_not_promote_minority_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "材料").mkdir()
            (root / "材料" / "西部超导.md").write_text(
                "# 西部超导\n西北有色金属研究院为公司最大控股股东，实际控制人为陕西省财政厅。截至目前，西北有色金属研究院为公司第一大控股股东，持有公司权益20.96%，上市公司中信金属为第二大控股股东，持有权益11.89%。\n",
                encoding="utf-8",
            )
            (root / "材料" / "中信金属.md").write_text("# 中信金属\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "CONTROLS" for edge in result["edges"]))

    def test_company_name_containing_holding_does_not_imply_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "汽车").mkdir()
            (root / "汽车" / "九号公司.md").write_text(
                "# 九号公司\n公司产品均价高于雅迪控股，但双方只是可比公司。\n",
                encoding="utf-8",
            )
            (root / "汽车" / "雅迪控股.md").write_text("# 雅迪控股\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["type"] == "CONTROLS" for edge in result["edges"]))

    def test_ambiguous_common_noun_company_name_is_not_linked_from_plain_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "机械").mkdir()
            (root / "医疗").mkdir()
            (root / "机械" / "三一国际.md").write_text(
                "# 三一国际\n公司推进新产业合作关系建设。\n",
                encoding="utf-8",
            )
            (root / "医疗" / "新产业.md").write_text("# 新产业\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        self.assertFalse(any(edge["target"] == "company:新产业" for edge in result["edges"]))

    def test_deduplicates_relations_but_preserves_multiple_evidence_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "组件").mkdir()
            (root / "设备" / "甲设备.md").write_text(
                "# 甲设备\n### 客户\n乙组件是公司客户。\n乙组件再次采购公司设备。\n",
                encoding="utf-8",
            )
            (root / "组件" / "乙组件.md").write_text("# 乙组件\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relations = [edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO"]
        self.assertEqual(len(relations), 1)
        self.assertEqual(len(relations[0]["evidence"]), 2)

    def test_does_not_invent_anonymous_or_unknown_company_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "半导体").mkdir()
            (root / "半导体" / "甲公司.md").write_text(
                "# 甲公司\n### 客户\n客户A和神秘科技是主要客户。\n",
                encoding="utf-8",
            )
            result = self.graph.build_graph([root])

        names = {node["name"] for node in result["nodes"] if node["type"] == "Company"}
        self.assertEqual(names, {"甲公司"})
        self.assertFalse(any(edge["type"] == "SUPPLIES_TO" for edge in result["edges"]))

    def test_long_evidence_excerpt_contains_target_and_local_trace_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "汽车").mkdir()
            long_prefix = "背景资料" * 180
            (root / "设备" / "甲设备.md").write_text(
                f"# 甲设备\n### 客户\n{long_prefix}，公司向比亚迪供应设备。\n",
                encoding="utf-8",
            )
            (root / "汽车" / "比亚迪.md").write_text("# 比亚迪\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        relation = next(edge for edge in result["edges"] if edge["type"] == "SUPPLIES_TO")
        evidence = relation["evidence"][0]
        self.assertIn("比亚迪", evidence["quote"])
        self.assertLessEqual(len(evidence["quote"]), 603)
        self.assertTrue(evidence["source_doc_id"].startswith("local-md:"))
        self.assertTrue(evidence["source_chunk_id"].startswith("local-line:"))
        self.assertEqual(evidence["page"], 0)

    def test_taxonomy_nodes_and_edges_include_taxonomy_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "半导体材料").mkdir()
            (root / "半导体材料" / "甲材料.md").write_text("# 甲材料\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        stages = [node for node in result["nodes"] if node["type"] == "ChainStage"]
        taxonomy_edges = [edge for edge in result["edges"] if edge["type"] in {"BELONGS_TO", "PART_OF_SECTOR", "PART_OF_CHAIN", "UPSTREAM_OF"}]
        self.assertTrue(stages)
        self.assertTrue(all(node.get("taxonomy_source") for node in stages))
        self.assertTrue(all(edge["evidence"][0].get("taxonomy_source") for edge in taxonomy_edges))

    def test_neo4j_import_statements_are_parameterized_and_evidence_bearing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "设备").mkdir()
            (root / "组件").mkdir()
            (root / "设备" / "甲设备.md").write_text("# 甲设备\n### 客户\n乙组件是公司客户。\n", encoding="utf-8")
            (root / "组件" / "乙组件.md").write_text("# 乙组件\n", encoding="utf-8")
            result = self.graph.build_graph([root])

        statements = self.graph.build_neo4j_import_statements(result, replace=True)
        self.assertIn("DETACH DELETE", statements[0]["statement"])
        company_statement = next(item for item in statements if ":Company" in item["statement"])
        self.assertIn("$rows", company_statement["statement"])
        self.assertNotIn("甲设备", company_statement["statement"])
        relation_statement = next(item for item in statements if "SUPPLIES_TO" in item["statement"])
        self.assertNotIn("乙组件是公司客户", relation_statement["statement"])
        row = relation_statement["parameters"]["rows"][0]
        self.assertEqual(row["source_file"].endswith("甲设备.md"), True)
        self.assertEqual(row["source_line"], 3)
        self.assertIn("乙组件", row["evidence_quote"])
        self.assertTrue(row["source_doc_id"].startswith("local-md:"))
        self.assertTrue(row["source_chunk_id"].startswith("local-line:"))
        self.assertEqual(row["page"], 0)

    def test_neo4j_import_rejects_unknown_labels_and_relations(self):
        bad_node_graph = {
            "schema_version": "alphamind.relation_graph.v1",
            "nodes": [{"id": "x", "type": "ArbitraryLabel", "name": "x"}],
            "edges": [],
        }
        with self.assertRaises(ValueError):
            self.graph.build_neo4j_import_statements(bad_node_graph)

        bad_edge_graph = {
            "schema_version": "alphamind.relation_graph.v1",
            "nodes": [
                {"id": "a", "type": "Company", "name": "a"},
                {"id": "b", "type": "Company", "name": "b"},
            ],
            "edges": [{"id": "e", "source": "a", "target": "b", "type": "ARBITRARY", "evidence": [{}]}],
        }
        with self.assertRaises(ValueError):
            self.graph.build_neo4j_import_statements(bad_edge_graph)

    def test_import_graph_runs_schema_then_atomic_data_transaction(self):
        graph = {
            "schema_version": "alphamind.relation_graph.v1",
            "nodes": [{"id": "company:a", "type": "Company", "name": "甲公司"}],
            "edges": [],
            "stats": {"nodes": 1, "edges": 0},
        }
        calls = []

        def requester(endpoint, payload, headers, timeout):
            calls.append((endpoint, payload, headers, timeout))
            return {"results": [{} for _ in payload["statements"]], "errors": []}

        imported = self.graph.import_graph(
            graph,
            neo4j_url="http://localhost:7474",
            username="neo4j",
            password="secret",
            requester=requester,
        )
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/db/neo4j/tx/commit"))
        self.assertIn("CREATE CONSTRAINT", calls[0][1]["statements"][0]["statement"])
        self.assertIn("DETACH DELETE", calls[1][1]["statements"][0]["statement"])
        self.assertTrue(calls[0][2]["Authorization"].startswith("Basic "))
        self.assertEqual(imported["status"], "pass")
        self.assertEqual(imported["nodes"], 1)

    def test_import_graph_rejects_malformed_success_response(self):
        graph = {
            "schema_version": "alphamind.relation_graph.v1",
            "nodes": [{"id": "company:a", "type": "Company", "name": "甲公司"}],
            "edges": [],
            "stats": {"nodes": 1, "edges": 0},
        }

        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.graph.import_graph(
                graph,
                neo4j_url="http://localhost:7474",
                username="neo4j",
                password="secret",
                requester=lambda endpoint, payload, headers, timeout: {},
            )

    def test_phase3_config_declares_sector_chain_and_company_relations(self):
        config = json.loads((ROOT / "dataset" / "alphamind_process_config_phase3_precision.json").read_text(encoding="utf-8"))
        extraction = config["extract_config"]
        node_names = {node["name"] for node in extraction["nodes"]}
        relation_types = {relation["type"] for relation in extraction["relations"]}
        relations_by_type = {relation["type"]: relation for relation in extraction["relations"]}
        self.assertTrue({"Sector", "Industry", "ChainStage", "Company"}.issubset(node_names))
        self.assertTrue({
            "BELONGS_TO",
            "PART_OF_SECTOR",
            "PART_OF_CHAIN",
            "UPSTREAM_OF",
            "SUPPLIES_TO",
            "PARTNERS_WITH",
            "CONTROLS",
            "COMPETES_WITH",
        }.issubset(relation_types))
        self.assertTrue({"source_doc_id", "source_chunk_id", "page", "quote", "confidence"}.issubset(
            set(relations_by_type["COMPETES_WITH"].get("attributes", []))
        ))

    def test_cli_can_import_to_neo4j_using_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            out = Path(tmp) / "graph.json"
            report = Path(tmp) / "report.json"
            env_file = Path(tmp) / ".env"
            (root / "半导体").mkdir(parents=True)
            (root / "半导体" / "芯片公司.md").write_text("# 芯片公司\n", encoding="utf-8")
            env_file.write_text(
                "NEO4J_HTTP_URL=http://neo4j.test:7474\nNEO4J_USERNAME=test-user\nNEO4J_PASSWORD=test-password\nNEO4J_DATABASE=neo4j\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_import(graph, **kwargs):
                captured.update(kwargs)
                return {"status": "pass", "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}

            original = self.graph.import_graph
            self.graph.import_graph = fake_import
            try:
                code = self.graph.run_cli([
                    "--source-root", str(root),
                    "--output", str(out),
                    "--report", str(report),
                    "--import-neo4j",
                    "--env-file", str(env_file),
                ])
            finally:
                self.graph.import_graph = original

            quality = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(captured["neo4j_url"], "http://neo4j.test:7474")
        self.assertEqual(captured["username"], "test-user")
        self.assertEqual(captured["password"], "test-password")
        self.assertEqual(quality["neo4j_import"]["status"], "pass")

    def test_cli_writes_graph_and_quality_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            out = Path(tmp) / "graph.json"
            report = Path(tmp) / "report.json"
            (root / "锂电池").mkdir(parents=True)
            (root / "锂电池" / "电池公司.md").write_text("# 电池公司\n", encoding="utf-8")
            code = self.graph.run_cli([
                "--source-root", str(root),
                "--output", str(out),
                "--report", str(report),
            ])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            quality = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], "alphamind.relation_graph.v1")
        self.assertEqual(quality["status"], "pass")
        self.assertEqual(quality["issues"], [])
        self.assertGreaterEqual(quality["stats"]["companies"], 1)


if __name__ == "__main__":
    unittest.main()
