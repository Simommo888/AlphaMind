#!/usr/bin/env python3
"""
Online AlphaMind smoke retrieval evaluator for WeKnora.

Reuses dataset/alphamind_eval_queries.json and
dataset/alphamind_ground_truth_template.json, calls WeKnora hybrid-search, and
computes document-level retrieval metrics. It records current online quality as
a baseline; low metrics are reported for tuning rather than hidden.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase2-advanced"
DEFAULT_QUERIES = PROJECT_ROOT / "dataset" / "alphamind_eval_queries.json"
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "dataset" / "alphamind_ground_truth_template.json"
DEFAULT_RETRIEVAL_CONFIG = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase2_advanced.json"
DEFAULT_KB_ID_FILE = PROJECT_ROOT / "dataset" / "alphamind_phase2_advanced_kb_id.txt"
DEFAULT_PHASE2_RUN_DIR = PROJECT_ROOT / "dataset" / "phase2_runs"
DEFAULT_OUT = PROJECT_ROOT / "dataset" / "alphamind_online_retrieval_eval_latest.json"


@dataclass
class RetrievedDoc:
    doc_id: str
    knowledge_id: str
    score: float
    first_rank: int
    chunk_count: int
    title: str


@dataclass
class QueryResult:
    query_id: str
    query: str
    sent_query: str
    relevant_count: int
    retrieved_count: int
    chunk_count: int
    http_status: int
    elapsed_ms: int
    metrics: dict[str, float]
    retrieved_docs: list[dict[str, Any]]
    relevant_docs: list[str]
    relevant_knowledge_ids: list[str]
    must_contain_coverage: dict[str, Any]
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AlphaMind smoke queries against WeKnora hybrid search.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--base-url", default="", help="Override WEKNORA_BASE_URL from env file.")
    parser.add_argument("--api-key", default="", help="Override WEKNORA_API_KEY from env file.")
    parser.add_argument("--kb-id", default="", help="Override KB id. Defaults to --kb-id-file.")
    parser.add_argument("--kb-id-file", default=str(DEFAULT_KB_ID_FILE))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    parser.add_argument("--retrieval-config", default=str(DEFAULT_RETRIEVAL_CONFIG))
    parser.add_argument("--acceptance-report", default="", help="Phase2 acceptance JSON used to map knowledge_id -> doc_id. Defaults to latest report.")
    parser.add_argument("--ingest-report", action="append", default=[], help="CSV ingest report(s) to merge into knowledge_id -> doc_id mapping. Defaults to phase2_runs/alphamind_ingest_*.csv.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--match-count", type=int, default=0, help="Override hybrid-search match_count. Defaults to retrieval_config.rerank_top_k.")
    parser.add_argument("--vector-threshold", type=float, default=None)
    parser.add_argument("--keyword-threshold", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-filter-expansion", action="store_true", help="Send only query text, without expected_filters expansion.")
    parser.add_argument("--skip-context-enrichment", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print full JSON result to stdout.")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_value(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


def flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(flatten_values(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(flatten_values(item))
        return result
    return [str(value)]


def expanded_query(query: dict[str, Any], *, use_filter_expansion: bool) -> str:
    text = str(query.get("text", "")).strip()
    if not use_filter_expansion:
        return text
    extras = [item for item in flatten_values(query.get("expected_filters", {})) if item and item.lower() != "true"]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in extras:
        if item not in seen and item not in text:
            deduped.append(item)
            seen.add(item)
    return " ".join([text] + deduped).strip()


def headers(api_key: str) -> dict[str, str]:
    result = {"Content-Type": "application/json"}
    if api_key:
        result["X-API-Key"] = api_key
    return result


def http_json(
    base_url: str,
    path: str,
    *,
    api_key: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if query:
        compact_query = {k: v for k, v in query.items() if v is not None and v != ""}
        if compact_query:
            url += "?" + urlencode(compact_query)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method)
    for key, value in headers(api_key).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                return int(getattr(response, "status", 200)), json.loads(text) if text else {}
            except json.JSONDecodeError:
                return int(getattr(response, "status", 200)), text
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return exc.code, text
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def extract_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def extract_items(payload: Any) -> list[dict[str, Any]]:
    data = extract_data(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "list", "data", "records", "knowledges", "chunks"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def latest_acceptance_report(run_dir: Path) -> Path:
    files = sorted(run_dir.glob("phase2_acceptance_*.json"), key=lambda p: p.name)
    if not files:
        return Path("")
    return files[-1]


def doc_stem_from_path(value: str) -> str:
    if not value:
        return ""
    return Path(value).stem


def add_mapping(knowledge_to_doc: dict[str, str], doc_to_knowledge: dict[str, str], knowledge_id: str, file_path: str, relative_path: str = "") -> None:
    doc_id = doc_stem_from_path(file_path or relative_path)
    if knowledge_id and doc_id:
        knowledge_to_doc[knowledge_id] = doc_id
        doc_to_knowledge[doc_id] = knowledge_id


def ingest_reports_from_dir(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("alphamind_ingest_*.csv"), key=lambda p: p.name)


def build_knowledge_map(acceptance_report: Path, ingest_reports: list[Path]) -> tuple[dict[str, str], dict[str, str]]:
    knowledge_to_doc: dict[str, str] = {}
    doc_to_knowledge: dict[str, str] = {}
    if acceptance_report and acceptance_report.exists():
        payload = load_json(acceptance_report)
        for upload in payload.get("uploads", []):
            if not isinstance(upload, dict):
                continue
            add_mapping(
                knowledge_to_doc,
                doc_to_knowledge,
                str(upload.get("knowledge_id") or ""),
                str(upload.get("file_path") or ""),
                str(upload.get("relative_path") or ""),
            )
    for report in ingest_reports:
        if not report.exists():
            continue
        with report.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("status") not in {"success", "duplicate"}:
                    continue
                add_mapping(
                    knowledge_to_doc,
                    doc_to_knowledge,
                    row.get("knowledge_id", ""),
                    row.get("file_path", ""),
                    row.get("relative_path", ""),
                )
    return knowledge_to_doc, doc_to_knowledge


def item_title(item: dict[str, Any]) -> str:
    return str(item.get("knowledge_title") or item.get("knowledge_filename") or item.get("title") or "")


def item_content(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("content") or ""),
        str(item.get("matched_content") or ""),
        item_title(item),
    ]
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        parts.extend(str(metadata.get(key) or "") for key in ("doc_id", "company", "ticker", "broker", "title"))
    return "\n".join(part for part in parts if part)


def retrieved_doc_id(item: dict[str, Any], knowledge_to_doc: dict[str, str]) -> str:
    knowledge_id = str(item.get("knowledge_id") or "")
    if knowledge_id in knowledge_to_doc:
        return knowledge_to_doc[knowledge_id]
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        file_path = str(metadata.get("file_path") or "")
        if file_path:
            return doc_stem_from_path(file_path)
    title = item_title(item)
    return doc_stem_from_path(title)


def unique_retrieved_docs(items: list[dict[str, Any]], knowledge_to_doc: dict[str, str]) -> tuple[list[RetrievedDoc], dict[str, list[dict[str, Any]]]]:
    by_doc: dict[str, list[dict[str, Any]]] = {}
    ordered: list[RetrievedDoc] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        doc_id = retrieved_doc_id(item, knowledge_to_doc)
        if not doc_id:
            continue
        by_doc.setdefault(doc_id, []).append(item)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        ordered.append(
            RetrievedDoc(
                doc_id=doc_id,
                knowledge_id=str(item.get("knowledge_id") or ""),
                score=score,
                first_rank=index,
                chunk_count=1,
                title=item_title(item),
            )
        )
    for position, doc in enumerate(ordered):
        doc.chunk_count = len(by_doc.get(doc.doc_id, []))
        ordered[position] = doc
    return ordered, by_doc


def precision(retrieved: list[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    return sum(1 for doc_id in retrieved[:k] if doc_id in relevant) / k


def recall(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return sum(1 for doc_id in retrieved[:k] if doc_id in relevant) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for index, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / index
    return 0.0


def ndcg(retrieved: list[str], relevant: set[str], k: int) -> float:
    dcg = 0.0
    for index, doc_id in enumerate(retrieved[:k], start=1):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(relevant), k)
    if ideal_hits == 0:
        return 0.0
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / ideal


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def must_contain_coverage(entries: list[dict[str, Any]], by_doc_items: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    for entry in entries:
        doc_id = str(entry.get("doc_id") or "")
        expected = [str(item) for item in entry.get("must_contain", [])]
        items = by_doc_items.get(doc_id, [])
        if not items:
            coverage[doc_id] = {"status": "not_retrieved", "matched": [], "missing": expected, "coverage": 0.0}
            continue
        combined = normalize_text("\n".join(item_content(item) for item in items))
        matched = [item for item in expected if normalize_text(item) in combined]
        missing = [item for item in expected if item not in matched]
        coverage[doc_id] = {
            "status": "pass" if not missing else "warn",
            "matched": matched,
            "missing": missing,
            "coverage": len(matched) / len(expected) if expected else 1.0,
        }
    return coverage


def evaluate_query(
    *,
    query: dict[str, Any],
    relevant_entries: list[dict[str, Any]],
    base_url: str,
    api_key: str,
    kb_id: str,
    retrieval_config: dict[str, Any],
    knowledge_to_doc: dict[str, str],
    doc_to_knowledge: dict[str, str],
    timeout: float,
    match_count: int,
    vector_threshold: float,
    keyword_threshold: float,
    use_filter_expansion: bool,
    skip_context_enrichment: bool,
) -> QueryResult:
    query_text = str(query.get("text") or "")
    sent_query = expanded_query(query, use_filter_expansion=use_filter_expansion)
    payload: dict[str, Any] = {
        "query_text": sent_query,
        "match_count": match_count,
        "vector_threshold": vector_threshold,
        "keyword_threshold": keyword_threshold,
    }
    if skip_context_enrichment:
        payload["skip_context_enrichment"] = True
    start = time.perf_counter()
    try:
        status, response = http_json(
            base_url,
            f"api/v1/knowledge-bases/{kb_id}/hybrid-search",
            api_key=api_key,
            method="POST",
            payload=payload,
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if status < 200 or status >= 300:
            return QueryResult(
                query_id=str(query.get("id") or ""),
                query=query_text,
                sent_query=sent_query,
                relevant_count=len(relevant_entries),
                retrieved_count=0,
                chunk_count=0,
                http_status=status,
                elapsed_ms=elapsed_ms,
                metrics={},
                retrieved_docs=[],
                relevant_docs=[str(entry.get("doc_id") or "") for entry in relevant_entries],
                relevant_knowledge_ids=[],
                must_contain_coverage={},
                error=str(response)[:500],
            )
        items = extract_items(response)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return QueryResult(
            query_id=str(query.get("id") or ""),
            query=query_text,
            sent_query=sent_query,
            relevant_count=len(relevant_entries),
            retrieved_count=0,
            chunk_count=0,
            http_status=0,
            elapsed_ms=elapsed_ms,
            metrics={},
            retrieved_docs=[],
            relevant_docs=[str(entry.get("doc_id") or "") for entry in relevant_entries],
            relevant_knowledge_ids=[],
            must_contain_coverage={},
            error=f"{type(exc).__name__}: {exc}",
        )

    retrieved_docs, by_doc_items = unique_retrieved_docs(items, knowledge_to_doc)
    retrieved_ids = [doc.doc_id for doc in retrieved_docs]
    relevant_docs = [str(entry.get("doc_id") or "") for entry in relevant_entries if entry.get("doc_id")]
    relevant_set = set(relevant_docs)
    metrics = {
        "precision@1": precision(retrieved_ids, relevant_set, 1),
        "recall@1": recall(retrieved_ids, relevant_set, 1),
        "precision@3": precision(retrieved_ids, relevant_set, 3),
        "recall@3": recall(retrieved_ids, relevant_set, 3),
        "precision@5": precision(retrieved_ids, relevant_set, 5),
        "recall@5": recall(retrieved_ids, relevant_set, 5),
        "precision@10": precision(retrieved_ids, relevant_set, 10),
        "recall@10": recall(retrieved_ids, relevant_set, 10),
        "reciprocal_rank": reciprocal_rank(retrieved_ids, relevant_set),
        "ndcg@5": ndcg(retrieved_ids, relevant_set, 5),
        "ndcg@10": ndcg(retrieved_ids, relevant_set, 10),
    }
    return QueryResult(
        query_id=str(query.get("id") or ""),
        query=query_text,
        sent_query=sent_query,
        relevant_count=len(relevant_set),
        retrieved_count=len(retrieved_docs),
        chunk_count=len(items),
        http_status=status,
        elapsed_ms=elapsed_ms,
        metrics=metrics,
        retrieved_docs=[asdict(doc) for doc in retrieved_docs],
        relevant_docs=relevant_docs,
        relevant_knowledge_ids=[doc_to_knowledge.get(doc_id, "") for doc_id in relevant_docs],
        must_contain_coverage=must_contain_coverage(relevant_entries, by_doc_items),
    )


def target_assessment(metrics: dict[str, float], targets: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "precision@5": {
            "actual": metrics.get("mean_precision@5", 0.0),
            "target": float(targets.get("precision_at_5_target") or 0.0),
        },
        "recall@10": {
            "actual": metrics.get("mean_recall@10", 0.0),
            "target": float(targets.get("recall_at_10_target") or 0.0),
        },
    }
    for item in checks.values():
        item["status"] = "pass" if item["actual"] >= item["target"] else "warn"
    return {"status": "pass" if all(item["status"] == "pass" for item in checks.values()) else "warn", "checks": checks}


def aggregate_results(results: list[QueryResult]) -> dict[str, float]:
    metric_names = [
        "precision@1",
        "recall@1",
        "precision@3",
        "recall@3",
        "precision@5",
        "recall@5",
        "precision@10",
        "recall@10",
        "reciprocal_rank",
        "ndcg@5",
        "ndcg@10",
    ]
    aggregate: dict[str, float] = {}
    valid = [result for result in results if result.metrics]
    for name in metric_names:
        values = [result.metrics[name] for result in valid]
        aggregate[f"mean_{name}"] = sum(values) / len(values) if values else 0.0
    aggregate["mean_elapsed_ms"] = sum(result.elapsed_ms for result in results) / len(results) if results else 0.0
    return aggregate


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    env = parse_env_file(Path(args.env_file))
    base_url = args.base_url or env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")
    api_key = args.api_key or env_value(env, "WEKNORA_API_KEY", "")
    kb_id = args.kb_id or Path(args.kb_id_file).read_text(encoding="utf-8").strip()
    queries_payload = load_json(Path(args.queries))
    ground_truth_payload = load_json(Path(args.ground_truth))
    retrieval_config = load_json(Path(args.retrieval_config))
    acceptance_report = Path(args.acceptance_report) if args.acceptance_report else latest_acceptance_report(DEFAULT_PHASE2_RUN_DIR)
    ingest_reports = [Path(path) for path in args.ingest_report] if args.ingest_report else ingest_reports_from_dir(DEFAULT_PHASE2_RUN_DIR)
    knowledge_to_doc, doc_to_knowledge = build_knowledge_map(acceptance_report, ingest_reports)

    match_count = args.match_count or int(retrieval_config.get("rerank_top_k") or 10)
    vector_threshold = args.vector_threshold if args.vector_threshold is not None else float(retrieval_config.get("vector_threshold") or 0.0)
    keyword_threshold = args.keyword_threshold if args.keyword_threshold is not None else float(retrieval_config.get("keyword_threshold") or 0.0)

    ground_truth = ground_truth_payload.get("ground_truth", {})
    results = [
        evaluate_query(
            query=query,
            relevant_entries=ground_truth.get(str(query.get("id") or ""), []),
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            retrieval_config=retrieval_config,
            knowledge_to_doc=knowledge_to_doc,
            doc_to_knowledge=doc_to_knowledge,
            timeout=args.timeout,
            match_count=match_count,
            vector_threshold=vector_threshold,
            keyword_threshold=keyword_threshold,
            use_filter_expansion=not args.no_filter_expansion,
            skip_context_enrichment=args.skip_context_enrichment,
        )
        for query in queries_payload.get("queries", [])
    ]
    aggregate = aggregate_results(results)
    missing_ground_truth = [result.query_id for result in results if not result.relevant_docs]
    missing_mapping = sorted({doc_id for result in results for doc_id in result.relevant_docs if doc_id not in doc_to_knowledge})
    http_errors = {result.query_id: result.error for result in results if result.error}
    missing_must_contain = {
        result.query_id: {
            doc_id: coverage["missing"]
            for doc_id, coverage in result.must_contain_coverage.items()
            if coverage.get("missing")
        }
        for result in results
    }
    missing_must_contain = {query_id: docs for query_id, docs in missing_must_contain.items() if docs}
    target_status = target_assessment(aggregate, ground_truth_payload.get("metrics", {}))
    setup_status = "pass" if not missing_ground_truth and not missing_mapping and not http_errors else "fail"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weknora": {
            "base_url": base_url,
            "kb_id": kb_id,
            "api_key_present": bool(api_key),
        },
        "inputs": {
            "queries": str(Path(args.queries)),
            "ground_truth": str(Path(args.ground_truth)),
            "retrieval_config": str(Path(args.retrieval_config)),
            "acceptance_report": str(acceptance_report) if acceptance_report else "",
            "ingest_reports": [str(path) for path in ingest_reports],
            "knowledge_map_size": len(knowledge_to_doc),
        },
        "settings": {
            "match_count": match_count,
            "vector_threshold": vector_threshold,
            "keyword_threshold": keyword_threshold,
            "use_filter_expansion": not args.no_filter_expansion,
            "skip_context_enrichment": args.skip_context_enrichment,
            "metric_level": "document-level unique knowledge order, mapped via phase2 acceptance report",
        },
        "aggregate_metrics": aggregate,
        "target_assessment": target_status,
        "query_results": [asdict(result) for result in results],
        "validation": {
            "setup_status": setup_status,
            "quality_status": target_status["status"],
            "missing_ground_truth": missing_ground_truth,
            "missing_mapping": missing_mapping,
            "http_errors": http_errors,
            "missing_must_contain": missing_must_contain,
        },
    }


def main() -> int:
    args = parse_args()
    result = evaluate(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        metrics = result["aggregate_metrics"]
        validation = result["validation"]
        print("AlphaMind online WeKnora retrieval eval")
        print("=" * 80)
        print(
            f"setup={validation['setup_status']} quality={validation['quality_status']} "
            f"kb={result['weknora']['kb_id']} out={out_path}"
        )
        print(
            "Precision@1={:.3f} Precision@5={:.3f} Recall@5={:.3f} Recall@10={:.3f} MRR={:.3f} NDCG@5={:.3f} mean_latency_ms={:.0f}".format(
                metrics["mean_precision@1"],
                metrics["mean_precision@5"],
                metrics["mean_recall@5"],
                metrics["mean_recall@10"],
                metrics["mean_reciprocal_rank"],
                metrics["mean_ndcg@5"],
                metrics["mean_elapsed_ms"],
            )
        )
        if validation["http_errors"]:
            print(f"http_errors={validation['http_errors']}")
        if validation["missing_mapping"]:
            print(f"missing_mapping={validation['missing_mapping']}")
        if validation["missing_must_contain"]:
            print(f"missing_must_contain={validation['missing_must_contain']}")
        for item in result["query_results"]:
            top = item["retrieved_docs"][0]["doc_id"] if item["retrieved_docs"] else "NONE"
            print(
                f"{item['query_id']}: p@1={item['metrics'].get('precision@1', 0):.0f} "
                f"rr={item['metrics'].get('reciprocal_rank', 0):.3f} chunks={item['chunk_count']} top={top}"
            )
    return 0 if result["validation"]["setup_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
