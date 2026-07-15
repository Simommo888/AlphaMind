import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_extractive_citation_keeps_embedding_resident_when_no_llm_is_called(self):
        results = [{"content": "每10股派发现金红利1.02元", "metadata": {"doc_id": "doc"}}]
        with patch.object(acceptance, "_post_bearer_json", return_value=(200, {"status": "ok"})) as post:
            status, _ = acceptance.direct_citation_chat_check(
                query="q", results=results, chat_url="http://chat", chat_model="m",
                embedding_unload_url="http://embedding", timeout=1,
                required_terms=["1.02", "现金红利"],
            )
        self.assertEqual(status, "pass")
        post.assert_not_called()

    def test_vllm_direct_chat_payload_disables_qwen3_thinking_without_ollama_field(self):
        payload = acceptance.build_chat_completion_payload(
            "qwen3-14b",
            [{"role": "user", "content": "问题"}],
            runtime="vllm",
        )
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("think", payload)
        self.assertEqual(
            acceptance.build_chat_completion_payload("qwen3:14b", [], runtime="ollama")["think"],
            False,
        )

    def test_qwen_reranker_batches_candidates_to_avoid_gpu_oom(self):
        candidates = [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}]
        responses = [
            (200, {"results": [{"index": 0, "relevance_score": 0.2}]}),
            (200, {"results": [{"index": 0, "relevance_score": 0.9}]}),
        ]
        with patch.object(acceptance, "_post_bearer_json", side_effect=responses) as post:
            result = acceptance.qwen_rerank_candidates(
                "q", candidates, 2, base_url="http://rerank", api_key="key", timeout=1, batch_size=1,
            )
        self.assertEqual(post.call_count, 2)
        self.assertEqual([item["id"] for item in result], ["b", "a"])

    def test_online_reranker_requires_real_api_and_records_audit_evidence(self):
        env = {
            "RERANK_BASE_URL": "http://rerank:8010",
            "RERANK_API_KEY": "secret",
            "RERANK_MODEL_NAME": "qwen3-reranker-4b-local",
        }
        audit = acceptance.RerankAudit()
        with patch.object(
            acceptance,
            "qwen_rerank_candidates",
            return_value=[{"id": "b", "content": "B", "rerank_score": 0.9}],
        ) as call:
            reranker = acceptance.build_qwen_reranker(env, timeout=3, batch_size=2, audit=audit)
            ranked = reranker("q", [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}], 1)
        self.assertEqual(ranked[0]["rerank_mode"], "model_api")
        self.assertEqual(ranked[0]["rerank_model"], "qwen3-reranker-4b-local")
        self.assertEqual(audit.calls, 1)
        self.assertEqual(audit.candidates, 2)
        self.assertEqual(audit.returned, 1)
        self.assertEqual(audit.endpoint, "http://rerank:8010/rerank")
        self.assertNotIn("secret", json.dumps(audit.public_dict(), ensure_ascii=False))
        call.assert_called_once()

        with self.assertRaisesRegex(RuntimeError, "RERANK_BASE_URL"):
            acceptance.build_qwen_reranker({"RERANK_MODEL_NAME": "model"}, timeout=1, batch_size=1)

    def test_main_stops_before_remote_side_effects_when_pdf_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "bad.pdf"
            env = root / "test.env"
            pdf.write_bytes(b"%PDF-test")
            env.write_text("WEKNORA_API_KEY=test-key\n", encoding="utf-8")
            manifest = {
                "source_doc_id": "doc-bad",
                "source_sha256": "hash",
                "page_count": 1,
                "parser": "test",
                "quality": {"numeric_preservation_ratio": 0.5, "tables_after_stitching": 0, "pages_with_text": 1},
            }
            argv = ["phase3", "--pdf", str(pdf), "--env-file", str(env), "--out-dir", str(root / "out")]
            with patch.object(sys, "argv", argv), patch.object(acceptance, "parse_pdf", return_value=("bad", manifest)), patch.object(acceptance, "ensure_kb") as ensure:
                self.assertEqual(acceptance.main(), 1)
            ensure.assert_not_called()

    def test_main_stops_after_parse_failure_before_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "report.pdf"
            env = root / "test.env"
            pdf.write_bytes(b"%PDF-test")
            env.write_text("WEKNORA_API_KEY=test-key\n", encoding="utf-8")
            manifest = {
                "source_doc_id": "doc-current",
                "source_sha256": "hash",
                "page_count": 1,
                "parser": "test",
                "quality": {"numeric_preservation_ratio": 1.0, "tables_after_stitching": 1, "pages_with_text": 1},
            }
            argv = ["phase3", "--pdf", str(pdf), "--env-file", str(env), "--out-dir", str(root / "out")]
            with patch.object(sys, "argv", argv), patch.object(acceptance, "parse_pdf", return_value=("ok", manifest)), patch.object(acceptance, "ensure_kb", return_value=("kb", [])), patch.object(acceptance, "find_existing_knowledge", return_value="knowledge"), patch.object(acceptance, "poll_knowledge_completed", return_value=("fail", "parse failed")), patch.object(acceptance, "phase3_hybrid_search") as search:
                self.assertEqual(acceptance.main(), 1)
            search.assert_not_called()

    def test_hybrid_and_chat_require_current_document_id(self):
        wrong_result = {"id": "chunk", "content": "other", "metadata": {"doc_id": "doc-other"}}
        with patch.object(acceptance, "http_json", return_value=(200, {"data": [wrong_result]})):
            status, detail, _ = acceptance.phase3_hybrid_search(
                base_url="http://example", api_key="key", kb_id="kb", query="q",
                retrieval_config={
                    "query_expansion": {"enabled": False},
                    "fusion": {"weights": {"dense": 0.5, "sparse": 0.5, "graph": 0.0}, "deduplicate_by": "chunk_id"},
                    "rerank": {"enabled": False, "top_k": 10},
                },
                timeout=1, expected_doc_id="doc-current", lexicon=[],
            )
        self.assertEqual(status, "fail")
        self.assertIn("doc-current", detail)

        ranked_results = [
            {"id": "wrong", "content": "other", "metadata": {"doc_id": "doc-other"}},
            {"id": "right", "content": "target", "metadata": {"doc_id": "doc-current"}},
        ]
        threshold_config = {
            "query_expansion": {"enabled": False},
            "fusion": {"weights": {"dense": 0.5, "sparse": 0.5, "graph": 0.0}, "deduplicate_by": "chunk_id"},
            "rerank": {"enabled": False, "top_k": 10},
            "quality_thresholds": {"mean_recall_at_10": 0.9, "mean_reciprocal_rank": 0.9},
        }
        with patch.object(acceptance, "http_json", return_value=(200, {"data": ranked_results})):
            status, detail, _ = acceptance.phase3_hybrid_search(
                base_url="http://example", api_key="key", kb_id="kb", query="q",
                retrieval_config=threshold_config, timeout=1, expected_doc_id="doc-current", lexicon=[],
            )
        self.assertEqual(status, "fail")
        self.assertIn("MRR", detail)

        events = [
            {"response_type": "references", "knowledge_references": [{"metadata": {"doc_id": "doc-other"}}]},
            {"response_type": "answer", "content": "answer"},
        ]
        with patch.object(acceptance, "create_session", return_value="session"), patch.object(acceptance, "http_sse", return_value=(200, events, "")):
            status, detail = acceptance.phase3_chat_check("http://example", "key", "kb", "q", 1, expected_doc_id="doc-current")
        self.assertEqual(status, "fail")
        self.assertIn("doc-current", detail)

    def test_strict_status_requires_every_step_to_pass(self):
        self.assertEqual(acceptance.strict_status(["pass", "pass"]), "pass")
        self.assertEqual(acceptance.strict_status(["pass", "warn"]), "fail")
        self.assertEqual(acceptance.strict_status(["pass", "skipped"]), "fail")
        self.assertEqual(acceptance.strict_status([]), "fail")


if __name__ == "__main__":
    unittest.main()
