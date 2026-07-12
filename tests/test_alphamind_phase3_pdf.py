import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_phase3_pdf.py"
spec = importlib.util.spec_from_file_location("alphamind_phase3_pdf", MODULE_PATH)
pdf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pdf
spec.loader.exec_module(pdf)


class Phase3PdfTests(unittest.TestCase):
    def test_stitches_consecutive_tables_and_removes_repeated_header(self):
        fragments = [
            pdf.TableFragment(page=10, rows=[["项目", "2024年", "2023年"], ["营业收入", "1,200", "900"]]),
            pdf.TableFragment(page=11, rows=[["项目", "2024年", "2023年"], ["净利润", "120", "80"]]),
        ]
        tables = pdf.stitch_table_fragments(fragments)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].pages, [10, 11])
        self.assertEqual(tables[0].rows, [["项目", "2024年", "2023年"], ["营业收入", "1,200", "900"], ["净利润", "120", "80"]])

    def test_does_not_stitch_non_consecutive_or_different_headers(self):
        fragments = [
            pdf.TableFragment(page=1, rows=[["项目", "金额"], ["收入", "10"]]),
            pdf.TableFragment(page=3, rows=[["股东", "持股"], ["甲", "20%"]]),
        ]
        self.assertEqual(len(pdf.stitch_table_fragments(fragments)), 2)

    def test_extracts_dense_financial_footnotes_with_page_lineage(self):
        text = "财务报表\n注：本期金额已按新会计准则重述。\n（1）不含税金额。\n普通正文"
        notes = pdf.extract_footnotes(text, page=8)
        self.assertEqual([n.page for n in notes], [8, 8])
        self.assertIn("会计准则", notes[0].text)
        self.assertIn("不含税", notes[1].text)

    def test_numeric_preservation_normalizes_commas_percent_and_parentheses(self):
        source = "营业收入 1,234.50，毛利率 18.2%，亏损（35.00）"
        output = "|营业收入|1234.50|\n|毛利率|18.2%|\n|亏损|(35.00)|"
        self.assertEqual(pdf.numeric_preservation_ratio(source, output), 1.0)

    def test_markdown_contains_table_and_footnote_lineage(self):
        table = pdf.StitchedTable(table_id="table-0001", pages=[2, 3], rows=[["项目", "金额"], ["收入", "100"]])
        note = pdf.Footnote(page=3, text="注：单位为万元。")
        rendered = pdf.render_markdown("doc-1", "示例年报", [table], [note])
        self.assertIn('source_doc_id="doc-1"', rendered)
        self.assertIn('pages="2-3"', rendered)
        self.assertIn("| 项目 | 金额 |", rendered)
        self.assertIn('page="3"', rendered)


if __name__ == "__main__":
    unittest.main()
