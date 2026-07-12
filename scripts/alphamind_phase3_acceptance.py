#!/usr/bin/env python3
"""AlphaMind Phase3 end-to-end: preprocess PDF, create KB, upload, retrieve and cite."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from alphamind_bulk_ingest import build_metadata, load_json, upload_file
from alphamind_phase2_acceptance import (
    chunk_check,
    collect_reference_doc_ids,
    create_session,
    ensure_kb,
    env_value,
    extract_items,
    http_json,
    http_sse,
    parse_env_file,
    poll_knowledge_completed,
)
from alphamind_phase3_pdf import parse_pdf

DEFAULT_ENV = PROJECT_ROOT / ".env.phase3-precision"
DEFAULT_KB_CONFIG = PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase3-precision.json"
DEFAULT_PROCESS = PROJECT_ROOT / "dataset" / "alphamind_process_config_phase3_precision.json"
DEFAULT_RETRIEVAL = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase3_precision.json"
DEFAULT_KB_ID = PROJECT_ROOT / "dataset" / "alphamind_phase3_precision_kb_id.txt"
DEFAULT_OUT = PROJECT_ROOT / "dataset" / "phase3_runs"


@dataclass
class Step:
    name: str
    status: str
    detail: str
    elapsed_ms: int = 0


def strict_status(statuses: list[str]) -> str:
    return "pass" if statuses and all(status == "pass" for status in statuses) else "fail"


def timed(name: str, operation) -> Step:
    started = time.perf_counter()
    try:
        status, detail = operation()
    except Exception as exc:
        status, detail = "fail", f"{type(exc).__name__}: {exc}"
    return Step(name, status, str(detail), int((time.perf_counter() - started) * 1000))


def build_phase3_metadata(md_path: Path, source_pdf: Path, root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = build_metadata(md_path, root, doc_id_mode="stem-ext", channel="alphamind-phase3-precision-ingest")
    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    metadata.update(
        {
            "doc_id": str(manifest.get("source_doc_id") or metadata["doc_id"]),
            "title": source_pdf.stem,
            "source_type": "financial_report",
            "source_pdf_path": str(source_pdf.resolve()),
            "source_pdf_sha256": str(manifest.get("source_sha256") or ""),
            "source_pdf_pages": int(manifest.get("page_count") or 0),
            "phase3_parser": str(manifest.get("parser") or ""),
            "numeric_preservation_ratio": float(quality.get("numeric_preservation_ratio") or 0.0),
            "tables_after_stitching": int(quality.get("tables_after_stitching") or 0),
            "cross_page_tables": int(quality.get("cross_page_tables") or 0),
            "footnotes_detected": int(quality.get("footnotes_detected") or 0),
            "ingest_channel": "alphamind-phase3-precision-ingest",
        }
    )
    return metadata


def find_existing_knowledge(base_url: str, api_key: str, kb_id: str, doc_id: str, timeout: float) -> str:
    page = 1
    while page <= 20:
        status, payload = http_json(base_url, f"api/v1/knowledge-bases/{kb_id}/knowledge", api_key=api_key, query={"page": page, "page_size": 100}, timeout=timeout)
        if status < 200 or status >= 300:
            return ""
        items = extract_items(payload)
        for item in items:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if str(metadata.get("doc_id") or item.get("doc_id") or "") == doc_id:
                return str(item.get("id") or item.get("knowledge_id") or "")
        if len(items) < 100:
            break
        page += 1
    return ""


def phase3_chat_check(base_url: str, api_key: str, kb_id: str, query: str, timeout: float, required_terms: list[str] | None = None) -> tuple[str, str]:
    session_id = create_session(base_url, api_key, timeout)
    status, events, raw = http_sse(
        base_url,
        f"api/v1/knowledge-chat/{session_id}",
        api_key=api_key,
        payload={"query": query, "knowledge_base_ids": [kb_id], "disable_title": True, "enable_memory": False, "channel": "api"},
        timeout=max(timeout, 180.0),
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {raw[:240]}"
    references = [event for event in events if event.get("response_type") == "references" or event.get("knowledge_references")]
    doc_ids = collect_reference_doc_ids(references)
    answer = "".join(str(event.get("content", "")) for event in events if event.get("response_type") == "answer")
    if not answer.strip():
        return "fail", "chat returned no answer"
    if not references or not doc_ids:
        return "fail", f"chat answer lacks traceable references; answer_preview={answer[:160]!r}"
    missing = missing_answer_terms(answer, required_terms or [])
    if missing:
        return "fail", f"chat answer missing required terms {missing}; answer_preview={answer[:200]!r}"
    return "pass", f"references={len(references)} distinct_doc_ids={len(doc_ids)} answer_preview={answer[:160]!r}"


def phase3_hybrid_search(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    query: str,
    retrieval_config: dict[str, Any],
    timeout: float,
) -> tuple[str, str, list[dict[str, Any]]]:
    payload = {
        "query_text": query,
        "match_count": int(retrieval_config.get("rerank_top_k") or 10),
        "vector_threshold": float(retrieval_config.get("vector_threshold") or 0.0),
        "keyword_threshold": float(retrieval_config.get("keyword_threshold") or 0.0),
    }
    status, response = http_json(
        base_url,
        f"api/v1/knowledge-bases/{kb_id}/hybrid-search",
        api_key=api_key,
        method="POST",
        payload=payload,
        timeout=max(timeout, 180.0),
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {str(response)[:240]}", []
    results = [item for item in extract_items(response) if isinstance(item, dict)]
    if not results:
        return "fail", "no search results", []
    return "pass", f"{len(results)} results; top={str(results[0].get('content', ''))[:120]!r}", results


def build_direct_citation_messages(query: str, results: list[dict[str, Any]], *, max_sources: int = 3) -> list[dict[str, str]]:
    source_blocks: list[str] = []
    for index, item in enumerate(results[:max_sources], start=1):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        doc_id = str(metadata.get("doc_id") or item.get("doc_id") or item.get("knowledge_id") or "unknown")
        content = str(item.get("content") or "").strip()[:2000]
        page = metadata.get("page") or metadata.get("page_number") or item.get("page") or item.get("page_number")
        if not page:
            page_match = re.search(r"\bpage=(?:&quot;|[\"'])(\d+)", content)
            page = page_match.group(1) if page_match else "unknown"
        source_blocks.append(f"[S{index}] doc_id={doc_id} page={page}\n{content}")
    prompt = (
        "请只根据下列检索证据回答问题。每个事实后必须使用对应的 [S数字] 引用；"
        "证据不足时明确说明，不得补充外部事实或编造数值。若问题询问每股或每10股金额，"
        "必须抽取与该单位直接对应的金额，不得把总股本、基数或合计数误当答案。\n\n"
        + "\n\n".join(source_blocks)
        + f"\n\n问题：{query}"
    )
    return [
        {"role": "system", "content": "你是金融证据核验助手。严格引用提供的来源编号。"},
        {"role": "user", "content": prompt},
    ]


def has_valid_source_citation(answer: str, source_count: int) -> bool:
    cited = {int(value) for value in re.findall(r"\[S(\d+)\]", str(answer or ""))}
    return bool(cited) and all(1 <= value <= source_count for value in cited)


def missing_answer_terms(answer: str, required_terms: list[str]) -> list[str]:
    return [str(term) for term in required_terms if str(term) and str(term) not in str(answer or "")]


def extractive_citation_answer(results: list[dict[str, Any]], required_terms: list[str], *, max_sources: int = 10) -> str:
    terms = [str(term) for term in required_terms if str(term)]
    if not terms:
        return ""
    for index, item in enumerate(results[:max_sources], start=1):
        content = str(item.get("content") or "")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line and all(term in line for term in terms):
                return f"{line} [S{index}]"
    return ""


def _post_bearer_json(url: str, token: str, timeout: float) -> tuple[int, Any]:
    request = Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw[:240]}
        return int(exc.code), payload
    except (URLError, TimeoutError) as exc:
        return 0, {"error": str(exc)}


def direct_citation_chat_check(
    *,
    query: str,
    results: list[dict[str, Any]],
    chat_url: str,
    chat_model: str,
    embedding_unload_url: str,
    timeout: float,
    required_terms: list[str] | None = None,
) -> tuple[str, str]:
    if not results:
        return "fail", "direct citation chat requires hybrid search results"
    extractive = extractive_citation_answer(results, required_terms or [])
    if extractive:
        return "pass", f"mode=extractive sources={min(10, len(results))} answer={extractive!r}"
    if embedding_unload_url:
        status, payload = _post_bearer_json(
            embedding_unload_url.rstrip("/") + "/unload",
            "local-no-auth",
            max(timeout, 60.0),
        )
        if status < 200 or status >= 300:
            return "fail", f"embedding unload HTTP {status}: {str(payload)[:160]}"
    messages = build_direct_citation_messages(query, results)
    status, payload = http_json(
        chat_url,
        "chat/completions",
        api_key="local-no-auth",
        method="POST",
        payload={"model": chat_model, "messages": messages, "stream": False, "think": False},
        timeout=max(timeout, 300.0),
    )
    if status < 200 or status >= 300:
        return "fail", f"direct chat HTTP {status}: {str(payload)[:240]}"
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
    answer = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
    source_count = min(3, len(results))
    if not answer:
        return "fail", "direct chat returned no answer"
    if not has_valid_source_citation(answer, source_count):
        return "fail", f"answer lacks a valid [S1..S{source_count}] citation; preview={answer[:200]!r}"
    missing = missing_answer_terms(answer, required_terms or [])
    if missing:
        return "fail", f"answer missing required terms {missing}; preview={answer[:240]!r}"
    return "pass", f"sources={source_count} answer={answer!r}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind Phase3 end-to-end acceptance.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--kb-config", default=str(DEFAULT_KB_CONFIG))
    parser.add_argument("--process-config", default=str(DEFAULT_PROCESS))
    parser.add_argument("--retrieval-config", default=str(DEFAULT_RETRIEVAL))
    parser.add_argument("--kb-id-file", default=str(DEFAULT_KB_ID))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--query", default="该公司2024年营业收入、扣非净利润和ROE分别是多少？请给出页码与原文证据。")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--poll-timeout", type=float, default=1800.0)
    parser.add_argument("--skip-chat", action="store_true")
    parser.add_argument("--direct-chat-url", default="", help="Optional OpenAI-compatible direct citation fallback, e.g. http://localhost:11434/v1.")
    parser.add_argument("--direct-chat-model", default="")
    parser.add_argument("--embedding-unload-url", default="", help="Optional shared embedding service base URL; POST /unload is called before direct chat.")
    parser.add_argument("--answer-must-contain", action="append", default=[], help="Repeatable hard assertion for citation answer content.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_pdf = Path(args.pdf).resolve()
    env = parse_env_file(Path(args.env_file))
    base_url = env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")
    api_key = env_value(env, "WEKNORA_API_KEY")
    if not source_pdf.is_file() or not api_key:
        print("source PDF or WEKNORA_API_KEY missing", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    pdf_out = out_dir / "pdf"
    pdf_out.mkdir(parents=True, exist_ok=True)
    steps: list[Step] = []

    markdown, manifest = parse_pdf(source_pdf, max_pages=args.max_pages)
    safe_stem = source_pdf.stem[:100]
    md_path = pdf_out / f"{safe_stem}.phase3.md"
    manifest_path = pdf_out / f"{safe_stem}.phase3.manifest.json"
    md_path.write_text(markdown, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quality = manifest["quality"]
    pdf_ok = quality["numeric_preservation_ratio"] >= 0.98 and quality["tables_after_stitching"] > 0 and quality["pages_with_text"] > 0
    steps.append(Step("complex_pdf", "pass" if pdf_ok else "fail", json.dumps(quality, ensure_ascii=False)))

    kb_id, kb_steps = ensure_kb(base_url=base_url, api_key=api_key, kb_config=load_json(args.kb_config), kb_id_file=Path(args.kb_id_file), timeout=args.timeout)
    steps.extend(Step(step.name, step.status, step.detail, step.elapsed_ms) for step in kb_steps)
    if not kb_id:
        return _finish(steps, out_dir, source_pdf)

    metadata = build_phase3_metadata(md_path, source_pdf, pdf_out, manifest)
    knowledge_id = find_existing_knowledge(base_url, api_key, kb_id, metadata["doc_id"], args.timeout)
    if knowledge_id:
        steps.append(Step("upload_or_reuse", "pass", f"reuse knowledge {knowledge_id}"))
    else:
        started = time.perf_counter()
        upload = upload_file(base_url=base_url, api_key=api_key, kb_id=kb_id, path=md_path, metadata=metadata, process_config=load_json(args.process_config), channel="alphamind-phase3-precision-ingest")
        steps.append(Step("upload_or_reuse", "pass" if upload.status == "success" and upload.knowledge_id else "fail", f"HTTP {upload.http_status}: {upload.message[:240]}", int((time.perf_counter() - started) * 1000)))
        knowledge_id = upload.knowledge_id
    if not knowledge_id:
        return _finish(steps, out_dir, source_pdf)

    steps.append(timed("parse_completion", lambda: poll_knowledge_completed(base_url=base_url, api_key=api_key, knowledge_id=knowledge_id, timeout=args.timeout, poll_seconds=args.poll_seconds, poll_timeout=args.poll_timeout)))
    steps.append(timed("chunk_lineage", lambda: chunk_check(base_url=base_url, api_key=api_key, knowledge_id=knowledge_id, metadata=metadata, timeout=args.timeout)))
    retrieval_config = load_json(args.retrieval_config)
    started = time.perf_counter()
    try:
        search_status, search_detail, search_results = phase3_hybrid_search(
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            query=args.query,
            retrieval_config=retrieval_config,
            timeout=args.timeout,
        )
    except Exception as exc:
        search_status, search_detail, search_results = "fail", f"{type(exc).__name__}: {exc}", []
    steps.append(Step("hybrid_search", search_status, search_detail, int((time.perf_counter() - started) * 1000)))
    if search_results:
        (out_dir / "phase3_hybrid_results_latest.json").write_text(
            json.dumps(search_results, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    direct_chat_url = args.direct_chat_url or env.get("PHASE3_DIRECT_CHAT_URL", "")
    direct_chat_model = args.direct_chat_model or env.get("PHASE3_DIRECT_CHAT_MODEL", "") or env.get("LLM_MODEL_NAME", "")
    embedding_unload_url = args.embedding_unload_url or env.get("PHASE3_EMBEDDING_UNLOAD_URL", "")
    if args.skip_chat:
        steps.append(Step("citation_chat", "skipped", "skipped by explicit --skip-chat; strict acceptance remains FAIL"))
    elif direct_chat_url:
        steps.append(timed("citation_chat", lambda: direct_citation_chat_check(
            query=args.query,
            results=search_results,
            chat_url=direct_chat_url,
            chat_model=direct_chat_model,
            embedding_unload_url=embedding_unload_url,
            timeout=args.timeout,
            required_terms=args.answer_must_contain,
        )))
    else:
        steps.append(timed("citation_chat", lambda: phase3_chat_check(base_url, api_key, kb_id, args.query, args.timeout, args.answer_must_contain)))
    return _finish(steps, out_dir, source_pdf, kb_id=kb_id, knowledge_id=knowledge_id)


def _finish(steps: list[Step], out_dir: Path, source_pdf: Path, *, kb_id: str = "", knowledge_id: str = "") -> int:
    status = strict_status([step.status for step in steps])
    report = {"schema_version": "alphamind.phase3.acceptance.v1", "status": status, "source_pdf": str(source_pdf), "kb_id": kb_id, "knowledge_id": knowledge_id, "steps": [asdict(step) for step in steps]}
    path = out_dir / "phase3_acceptance_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for step in steps:
        print(f"{step.status.upper():<5} {step.name:<22} {step.detail[:180]}")
    print(f"PHASE3 END-TO-END ACCEPTANCE: {status.upper()}")
    print(f"report={path}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
