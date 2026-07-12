import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_phase3_acceptance.py"
spec = importlib.util.spec_from_file_location("alphamind_phase3_acceptance", MODULE_PATH)
acceptance = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acceptance
spec.loader.exec_module(acceptance)


class Phase3AcceptanceTests(unittest.TestCase):
    def test_custom_smoke_query_is_configurable(self):
        with patch.object(sys, "argv", ["alphamind_phase3_acceptance.py", "--pdf", "sample.pdf", "--query", "自定义问题"]):
            args = acceptance.parse_args()
        self.assertEqual(args.query, "自定义问题")

    def test_direct_citation_prompt_uses_bounded_traceable_sources(self):
        messages = acceptance.build_direct_citation_messages(
            "问题",
            [
                {"content": "证据甲", "metadata": {"doc_id": "doc-a", "page": 2}},
                {"content": "<!-- page source_doc_id=\"doc-b\" page=\"7\" -->\n证据乙", "metadata": {"doc_id": "doc-b"}},
            ],
        )
        prompt = messages[-1]["content"]
        self.assertIn("[S1]", prompt)
        self.assertIn("doc-a", prompt)
        self.assertIn("page=2", prompt)
        self.assertIn("page=7", prompt)
        self.assertTrue(acceptance.has_valid_source_citation("结论 [S1]", 2))
        self.assertFalse(acceptance.has_valid_source_citation("无引用", 2))
        self.assertFalse(acceptance.has_valid_source_citation("伪造 [S3]", 2))
        self.assertEqual(acceptance.missing_answer_terms("每10股1.02元 [S1]", ["1.02", "每10股"]), [])
        self.assertEqual(acceptance.missing_answer_terms("每10股1.02元 [S1]", ["现金红利"]), ["现金红利"])
        extracted = acceptance.extractive_citation_answer(
            [{"content": "其他"}, {"content": "向全体股东每10股派发现金红利1.02元（含税）。\n下一行"}],
            ["1.02", "现金红利"],
        )
        self.assertEqual(extracted, "向全体股东每10股派发现金红利1.02元（含税）。 [S2]")

    def test_build_metadata_preserves_pdf_lineage_and_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md = root / "report.phase3.md"
            pdf = root / "report.pdf"
            md.write_text("# report", encoding="utf-8")
            pdf.write_bytes(b"%PDF-test")
            manifest = {
                "source_doc_id": "doc-phase3",
                "source_sha256": "abc123",
                "page_count": 10,
                "parser": "phase3-pymupdf-layout",
                "quality": {"numeric_preservation_ratio": 1.0, "tables_after_stitching": 4},
            }
            metadata = acceptance.build_phase3_metadata(md, pdf, root, manifest)
            self.assertEqual(metadata["doc_id"], "doc-phase3")
            self.assertEqual(metadata["source_pdf_sha256"], "abc123")
            self.assertEqual(metadata["source_pdf_pages"], 10)
            self.assertEqual(metadata["numeric_preservation_ratio"], 1.0)
            self.assertEqual(metadata["tables_after_stitching"], 4)
            self.assertEqual(metadata["ingest_channel"], "alphamind-phase3-precision-ingest")

    def test_strict_status_requires_every_step_to_pass(self):
        self.assertEqual(acceptance.strict_status(["pass", "pass"]), "pass")
        self.assertEqual(acceptance.strict_status(["pass", "warn"]), "fail")
        self.assertEqual(acceptance.strict_status(["pass", "skipped"]), "fail")
        self.assertEqual(acceptance.strict_status([]), "fail")


if __name__ == "__main__":
    unittest.main()
