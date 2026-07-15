#!/usr/bin/env python3
"""
AlphaMind phase2 quality evaluator.

Runs Phase2 seed queries against an existing WeKnora KB and measures quality above
basic system acceptance: retrieval recall, knowledge-chat references, citation doc
coverage, answer constraints, and no-evidence guardrails.

Uses only the Python standard library so it can run in the same environment as the
existing AlphaMind acceptance/evaluation scripts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase2-advanced"
DEFAULT_QUERIES = PROJECT_ROOT / "dataset" / "phase2_eval_queries.json"
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "dataset" / "phase2_ground_truth.json"
DEFAULT_RETRIEVAL_CONFIG = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase2_advanced.json"
DEFAULT_KB_ID_FILE = PROJECT_ROOT / "dataset" / "alphamind_phase2_advanced_kb_id.txt"
DEFAULT_PHASE2_RUN_DIR = PROJECT_ROOT / "dataset" / "phase2_runs"
DEFAULT_OUT = DEFAULT_PHASE2_RUN_DIR / "phase2_quality_eval_latest.json"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


RETRIEVAL_METRICS = (
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
)

NO_EVIDENCE_DEFAULT_REFUSAL_TERMS = [
    "未检索到足够证据",
    "没有足够证据",
    "知识库中未找到",
    "无法根据现有资料",
    "不能编造",
]

NO_EVIDENCE_DEFAULT_FORBIDDEN_PATTERNS = [
    r"目标价[^。\n]{0,30}\d+(?:\.\d+)?\s*元",
    r"EPS[^。\n]{0,30}\d+(?:\.\d+)?",
    r"每股收益[^。\n]{0,30}\d+(?:\.\d+)?",
]


@dataclass
class RetrievedDoc:
    doc_id: str
    knowledge_id: str
    score: float
    first_rank: int
    chunk_count: int
    title: str


@dataclass
class GroundTruthEntry:
    query_id: str
    gt_status: str = "placeholder"  # complete / partial / placeholder / no_evidence
    expected_behavior: str = "evidence_required"
    relevant_doc_ids: list[str] = field(default_factory=list)
    reference_must_include_doc_ids: list[str] = field(default_factory=list)
    min_distinct_doc_ids: int = 1
    requires_references: bool = True
    answer_must_contain: list[str] = field(default_factory=list)
    answer_must_contain_any: list[str] = field(default_factory=list)
    answer_must_not_contain: list[str] = field(default_factory=list)
    answer_must_not_match: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    @property
    def is_no_evidence(self) -> bool:
        return self.gt_status == "no_evidence" or self.expected_behavior == "no_evidence"

    @property
    def is_placeholder(self) -> bool:
        return self.gt_status == "placeholder"

    @property
    def is_partial(self) -> bool:
        return self.gt_status == "partial"

    @property
    def scorable_retrieval(self) -> bool:
        return bool(self.relevant_doc_ids) and not self.is_no_evidence and not self.is_placeholder

    @property
    def scorable_citation(self) -> bool:
        return bool(self.reference_must_include_doc_ids) and not self.is_no_evidence and not self.is_placeholder


@dataclass
class GroundTruthValidation:
    status: str
    missing_ground_truth: list[str]
    placeholder_queries: list[str]
    partial_queries: list[str]
    regex_errors: dict[str, list[str]]
    warnings: dict[str, list[str]]
    action_items: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AlphaMind Phase2 answer quality against WeKnora.")
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
    parser.add_argument("--mode", choices=("all", "retrieval", "chat"), default="all")
    parser.add_argument("--validate-only", action="store_true", help="Validate query/ground-truth schema without calling WeKnora or writing an output report.")
    parser.add_argument("--match-count", type=int, default=0, help="Override hybrid-search match_count. Defaults to retrieval_config.rerank_top_k.")
    parser.add_argument("--vector-threshold", type=float, default=None)
    parser.add_argument("--keyword-threshold", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument("--no-filter-expansion", action="store_true", help="Send only query text, without expected_filters expansion.")
    parser.add_argument("--skip-context-enrichment", action="store_true")
    parser.add_argument("--include-answer", action="store_true", help="Include full answer text in query_results instead of only answer_preview.")
    parser.add_argument("--strict", action="store_true", help="Treat placeholder/partial ground truth as a failing status.")
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


QUERY_EXPANSION_FIELD_ALLOWLIST = {"company", "ticker", "broker", "industry", "topic", "source_type", "report_type"}


def expanded_query(query: dict[str, Any], *, use_filter_expansion: bool) -> str:
    text = str(query.get("text", "")).strip()
    if not use_filter_expansion:
        return text
    filters = query.get("expected_filters", {})
    if not isinstance(filters, dict):
        return text
    extras: list[str] = []
    for key in QUERY_EXPANSION_FIELD_ALLOWLIST:
        extras.extend(item for item in flatten_values(filters.get(key)) if item and item.lower() != "true")
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


def http_sse(
    base_url: str,
    path: str,
    *,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, list[dict[str, Any]], str]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST")
    for key, value in headers(api_key).items():
        request.add_header(key, value)
    events: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                raw_parts.append(line)
                if line.startswith("data:"):
                    data = line[5:].strip()
                    try:
                        event = json.loads(data)
                        if isinstance(event, dict):
                            events.append(event)
                    except json.JSONDecodeError:
                        pass
            return status, events, "\n".join(raw_parts[-80:])
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, events, text
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


def extract_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "knowledge_id", "knowledgeId", "session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = extract_id(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = extract_id(value)
            if found:
                return found
    return ""


def latest_acceptance_report(run_dir: Path) -> Path:
    files = sorted(run_dir.glob("phase2_acceptance_*.json"), key=lambda p: p.name)
    if not files:
        return Path("")
    return files[-1]


def doc_stem_from_path(value: str) -> str:
    if not value:
        return ""
    cleaned = value.replace("\\", "/")
    if cleaned.startswith("minio://"):
        cleaned = cleaned.rsplit("/", 1)[-1]
    return Path(cleaned).stem


def normalize_identifier(value: str) -> str:
    return "".join(str(value or "").lower().split())


def doc_id_without_hash(value: str) -> str:
    value = str(value or "").strip()
    match = re.match(r"^(?P<prefix>.+)_[0-9a-f]{12}$", value, flags=re.IGNORECASE)
    return match.group("prefix") if match else value


def add_alias(alias_to_doc: dict[str, str], alias: str, doc_id: str) -> None:
    alias = str(alias or "").strip()
    doc_id = str(doc_id or "").strip()
    if not alias or not doc_id:
        return
    alias_to_doc[alias] = doc_id
    alias_to_doc[normalize_identifier(alias)] = doc_id
    stable_alias = doc_id_without_hash(alias)
    if stable_alias and stable_alias != alias:
        alias_to_doc[stable_alias] = doc_id
        alias_to_doc[normalize_identifier(stable_alias)] = doc_id


def canonical_doc_id(value: str, alias_to_doc: dict[str, str]) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    stable_value = doc_id_without_hash(value)
    if stable_value and stable_value != value:
        stable_direct = alias_to_doc.get(stable_value) or alias_to_doc.get(normalize_identifier(stable_value))
        if stable_direct:
            return stable_direct
    direct = alias_to_doc.get(value) or alias_to_doc.get(normalize_identifier(value))
    if direct:
        return direct
    stem = doc_stem_from_path(value)
    if stem and stem != value:
        stable_stem = doc_id_without_hash(stem)
        return (
            alias_to_doc.get(stable_stem)
            or alias_to_doc.get(normalize_identifier(stable_stem))
            or alias_to_doc.get(stem)
            or alias_to_doc.get(normalize_identifier(stem))
            or stem
        )
    return value


def add_mapping(
    knowledge_to_doc: dict[str, str],
    doc_to_knowledge: dict[str, str],
    alias_to_doc: dict[str, str],
    knowledge_id: str,
    *,
    doc_id: str = "",
    file_path: str = "",
    relative_path: str = "",
    title: str = "",
) -> None:
    canonical = doc_id or doc_stem_from_path(file_path or relative_path or title)
    if not canonical:
        return
    if knowledge_id:
        knowledge_to_doc[knowledge_id] = canonical
        doc_to_knowledge[canonical] = knowledge_id
    add_alias(alias_to_doc, canonical, canonical)
    for alias in (file_path, relative_path, title, doc_stem_from_path(file_path), doc_stem_from_path(relative_path), doc_stem_from_path(title)):
        add_alias(alias_to_doc, alias, canonical)


def parse_metadata_json(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def ingest_reports_from_dir(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("alphamind_ingest_*.csv"), key=lambda p: p.name)


def build_knowledge_map(run_dir: Path, acceptance_report: Path, ingest_reports: list[Path]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    knowledge_to_doc: dict[str, str] = {}
    doc_to_knowledge: dict[str, str] = {}
    alias_to_doc: dict[str, str] = {}
    if acceptance_report and acceptance_report.exists():
        payload = load_json(acceptance_report)
        for upload in payload.get("uploads", []):
            if not isinstance(upload, dict):
                continue
            add_mapping(
                knowledge_to_doc,
                doc_to_knowledge,
                alias_to_doc,
                str(upload.get("knowledge_id") or ""),
                doc_id=str(upload.get("doc_id") or ""),
                file_path=str(upload.get("file_path") or ""),
                relative_path=str(upload.get("relative_path") or ""),
            )
    for report in ingest_reports:
        if not report.exists():
            continue
        with report.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("status") not in {"success", "duplicate"}:
                    continue
                metadata = parse_metadata_json(row.get("metadata_json", ""))
                add_mapping(
                    knowledge_to_doc,
                    doc_to_knowledge,
                    alias_to_doc,
                    row.get("knowledge_id", ""),
                    doc_id=row.get("doc_id", "") or str(metadata.get("doc_id") or ""),
                    file_path=row.get("file_path", "") or str(metadata.get("file_path") or ""),
                    relative_path=row.get("relative_path", "") or str(metadata.get("relative_path") or ""),
                    title=str(metadata.get("title") or ""),
                )
    return knowledge_to_doc, doc_to_knowledge, alias_to_doc


def merge_kb_knowledge_map_from_payload(
    knowledge_to_doc: dict[str, str],
    doc_to_knowledge: dict[str, str],
    alias_to_doc: dict[str, str],
    payload: Any,
) -> int:
    count = 0
    for item in extract_items(payload):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        add_mapping(
            knowledge_to_doc,
            doc_to_knowledge,
            alias_to_doc,
            str(item.get("id") or item.get("knowledge_id") or item.get("knowledgeId") or ""),
            doc_id=str(metadata.get("doc_id") or item.get("doc_id") or ""),
            file_path=str(metadata.get("file_path") or item.get("file_path") or ""),
            relative_path=str(metadata.get("relative_path") or item.get("relative_path") or ""),
            title=str(item.get("title") or item.get("file_name") or metadata.get("title") or ""),
        )
        count += 1
    return count


def merge_kb_knowledge_map_from_api(
    knowledge_to_doc: dict[str, str],
    doc_to_knowledge: dict[str, str],
    alias_to_doc: dict[str, str],
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    timeout: float,
) -> int:
    if not kb_id:
        return 0
    total = 0
    page = 1
    page_size = 100
    while True:
        status, payload = http_json(
            base_url,
            f"api/v1/knowledge-bases/{kb_id}/knowledge",
            api_key=api_key,
            query={"page": page, "page_size": page_size},
            timeout=timeout,
        )
        if status < 200 or status >= 300:
            break
        items = extract_items(payload)
        if not items:
            break
        total += merge_kb_knowledge_map_from_payload(knowledge_to_doc, doc_to_knowledge, alias_to_doc, payload)
        if len(items) < page_size:
            break
        page += 1
    return total


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


def retrieved_doc_id(item: dict[str, Any], knowledge_to_doc: dict[str, str], alias_to_doc: dict[str, str]) -> str:
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for key in ("doc_id", "document_id", "documentId"):
            value = str(metadata.get(key) or "")
            if value:
                return canonical_doc_id(value, alias_to_doc)
    knowledge_id = str(item.get("knowledge_id") or item.get("knowledgeId") or "")
    if knowledge_id in knowledge_to_doc:
        return knowledge_to_doc[knowledge_id]
    if isinstance(metadata, dict):
        for key in ("file_path", "relative_path", "title"):
            value = str(metadata.get(key) or "")
            if value:
                return canonical_doc_id(doc_stem_from_path(value), alias_to_doc)
    return canonical_doc_id(doc_stem_from_path(item_title(item)), alias_to_doc)


def unique_retrieved_docs(
    items: list[dict[str, Any]],
    knowledge_to_doc: dict[str, str],
    alias_to_doc: dict[str, str],
) -> tuple[list[RetrievedDoc], dict[str, list[dict[str, Any]]]]:
    by_doc: dict[str, list[dict[str, Any]]] = {}
    ordered: list[RetrievedDoc] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        doc_id = retrieved_doc_id(item, knowledge_to_doc, alias_to_doc)
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
                knowledge_id=str(item.get("knowledge_id") or item.get("knowledgeId") or ""),
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
    return "".join(str(value or "").lower().split())


def contains_text(haystack: str, needle: str) -> bool:
    return bool(needle) and normalize_text(needle) in normalize_text(haystack)


def must_contain_coverage(entries: list[dict[str, Any]], by_doc_items: dict[str, list[dict[str, Any]]], alias_to_doc: dict[str, str]) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    for entry in entries:
        doc_id = canonical_doc_id(str(entry.get("doc_id") or ""), alias_to_doc)
        expected = [str(item) for item in entry.get("must_contain", [])]
        items = by_doc_items.get(doc_id, [])
        if not items:
            coverage[doc_id] = {"status": "not_retrieved", "matched": [], "missing": expected, "coverage": 0.0}
            continue
        combined = "\n".join(item_content(item) for item in items)
        matched = [item for item in expected if contains_text(combined, item)]
        missing = [item for item in expected if item not in matched]
        coverage[doc_id] = {
            "status": "pass" if not missing else "warn",
            "matched": matched,
            "missing": missing,
            "coverage": len(matched) / len(expected) if expected else 1.0,
        }
    return coverage


def bool_from_query_filter(query: dict[str, Any], key: str, default: bool = True) -> bool:
    filters = query.get("expected_filters", {})
    if isinstance(filters, dict) and key in filters:
        return bool(filters.get(key))
    return default


def int_from_query_filter(query: dict[str, Any], key: str, default: int) -> int:
    filters = query.get("expected_filters", {})
    if isinstance(filters, dict) and key in filters:
        try:
            return int(filters.get(key))
        except (TypeError, ValueError):
            return default
    return default


def infer_expected_behavior(query: dict[str, Any], raw: dict[str, Any] | None = None) -> str:
    if raw and raw.get("expected_behavior"):
        return str(raw.get("expected_behavior"))
    if query.get("expected_behavior"):
        behavior = str(query.get("expected_behavior"))
        if "没有足够证据" in behavior or "不能编造" in behavior:
            return "no_evidence"
        return behavior
    query_type = str(query.get("type") or "")
    if "no_evidence" in query_type:
        return "no_evidence"
    if "cross" in query_type or "graph_seeded" in query_type:
        return "cross_document"
    if "table" in query_type:
        return "table_required"
    return "evidence_required"


def normalize_evidence(entries: Any, alias_to_doc: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        doc_id = canonical_doc_id(str(entry.get("doc_id") or ""), alias_to_doc)
        result.append(
            {
                "doc_id": doc_id,
                "chunk_ids": [str(item) for item in entry.get("chunk_ids", [])] if isinstance(entry.get("chunk_ids"), list) else [],
                "must_contain": [str(item) for item in entry.get("must_contain", [])] if isinstance(entry.get("must_contain"), list) else [],
            }
        )
    return result


def entry_from_rich_object(query: dict[str, Any], query_id: str, raw: dict[str, Any], alias_to_doc: dict[str, str]) -> GroundTruthEntry:
    behavior = infer_expected_behavior(query, raw)
    relevant_doc_ids = [canonical_doc_id(str(item), alias_to_doc) for item in raw.get("relevant_doc_ids", []) if str(item)]
    reference_docs = [canonical_doc_id(str(item), alias_to_doc) for item in raw.get("reference_must_include_doc_ids", []) if str(item)]
    evidence = normalize_evidence(raw.get("evidence", []), alias_to_doc)
    if not evidence and isinstance(raw.get("relevant"), list):
        evidence = normalize_evidence(raw.get("relevant", []), alias_to_doc)
    if not relevant_doc_ids:
        relevant_doc_ids = [str(item.get("doc_id")) for item in evidence if item.get("doc_id")]
    if not reference_docs and not raw.get("reference_must_include_doc_ids"):
        reference_docs = list(relevant_doc_ids)
    is_no_evidence = behavior == "no_evidence" or str(raw.get("gt_status") or "") == "no_evidence"
    gt_status = str(raw.get("gt_status") or "").strip()
    if not gt_status:
        if is_no_evidence:
            gt_status = "no_evidence"
        elif relevant_doc_ids:
            gt_status = "complete" if raw.get("evidence") else "partial"
        else:
            gt_status = "placeholder"
    answer_must_not_match = [str(item) for item in raw.get("answer_must_not_match", [])] if isinstance(raw.get("answer_must_not_match"), list) else []
    if raw.get("must_not_contain_fabricated_numbers") and not answer_must_not_match:
        answer_must_not_match = list(NO_EVIDENCE_DEFAULT_FORBIDDEN_PATTERNS)
    answer_must_contain = raw.get("answer_must_contain", raw.get("must_contain", []))
    return GroundTruthEntry(
        query_id=query_id,
        gt_status=gt_status,
        expected_behavior="no_evidence" if is_no_evidence else behavior,
        relevant_doc_ids=relevant_doc_ids,
        reference_must_include_doc_ids=reference_docs,
        min_distinct_doc_ids=int(raw.get("min_distinct_doc_ids") or int_from_query_filter(query, "min_distinct_doc_ids", 0 if is_no_evidence else 1)),
        requires_references=bool(raw.get("requires_references", bool_from_query_filter(query, "requires_references", not is_no_evidence))),
        answer_must_contain=[str(item) for item in answer_must_contain] if isinstance(answer_must_contain, list) else [],
        answer_must_contain_any=[str(item) for item in raw.get("answer_must_contain_any", [])] if isinstance(raw.get("answer_must_contain_any"), list) else [],
        answer_must_not_contain=[str(item) for item in raw.get("answer_must_not_contain", [])] if isinstance(raw.get("answer_must_not_contain"), list) else [],
        answer_must_not_match=answer_must_not_match,
        evidence=evidence,
        notes=str(raw.get("notes") or ""),
    )


def entry_from_legacy_list(query: dict[str, Any], query_id: str, entries: list[Any], alias_to_doc: dict[str, str]) -> GroundTruthEntry:
    evidence = normalize_evidence(entries, alias_to_doc)
    relevant_doc_ids = [str(item.get("doc_id")) for item in evidence if item.get("doc_id")]
    behavior = infer_expected_behavior(query)
    return GroundTruthEntry(
        query_id=query_id,
        gt_status="complete" if relevant_doc_ids else "placeholder",
        expected_behavior=behavior,
        relevant_doc_ids=relevant_doc_ids,
        reference_must_include_doc_ids=list(relevant_doc_ids),
        min_distinct_doc_ids=int_from_query_filter(query, "min_distinct_doc_ids", 1),
        requires_references=bool_from_query_filter(query, "requires_references", True),
        answer_must_contain=sorted({term for entry in evidence for term in entry.get("must_contain", [])}),
        evidence=evidence,
    )


def load_ground_truth_entries(
    queries: list[dict[str, Any]],
    ground_truth_payload: dict[str, Any],
    alias_to_doc: dict[str, str] | None = None,
) -> tuple[dict[str, GroundTruthEntry], GroundTruthValidation]:
    alias_to_doc = alias_to_doc or {}
    query_by_id = {str(query.get("id") or ""): query for query in queries if query.get("id")}
    entries: dict[str, GroundTruthEntry] = {}

    gt_map = ground_truth_payload.get("ground_truth") if isinstance(ground_truth_payload, dict) else None
    if isinstance(gt_map, dict):
        for query_id, raw in gt_map.items():
            query = query_by_id.get(str(query_id), {"id": query_id})
            if isinstance(raw, list):
                entries[str(query_id)] = entry_from_legacy_list(query, str(query_id), raw, alias_to_doc)
            elif isinstance(raw, dict):
                entries[str(query_id)] = entry_from_rich_object(query, str(query_id), raw, alias_to_doc)

    current_queries = ground_truth_payload.get("queries") if isinstance(ground_truth_payload, dict) else None
    if isinstance(current_queries, list):
        for raw in current_queries:
            if not isinstance(raw, dict):
                continue
            query_id = str(raw.get("id") or "")
            if not query_id or query_id in entries:
                continue
            query = query_by_id.get(query_id, {"id": query_id})
            entries[query_id] = entry_from_rich_object(query, query_id, raw, alias_to_doc)

    validation = validate_ground_truth(queries, entries)
    return entries, validation


def validate_ground_truth(queries: list[dict[str, Any]], entries: dict[str, GroundTruthEntry]) -> GroundTruthValidation:
    missing: list[str] = []
    placeholders: list[str] = []
    partials: list[str] = []
    regex_errors: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}
    action_items: list[str] = []

    for query in queries:
        query_id = str(query.get("id") or "")
        if not query_id:
            continue
        entry = entries.get(query_id)
        if not entry:
            missing.append(query_id)
            action_items.append(f"补齐 {query_id} 的 ground_truth 条目。")
            continue
        query_warnings: list[str] = []
        if entry.is_placeholder:
            placeholders.append(query_id)
            query_warnings.append("ground truth 仍为 placeholder；retrieval/citation 硬指标将跳过。")
            action_items.append(f"补齐 {query_id} 的 relevant_doc_ids/reference_must_include_doc_ids/evidence。")
        if entry.is_partial:
            partials.append(query_id)
            query_warnings.append("ground truth 为 partial；可运行但质量门禁降级为 warning。")
        if not entry.is_no_evidence and not entry.relevant_doc_ids:
            query_warnings.append("非 no-evidence query 缺 relevant_doc_ids。")
        if entry.is_no_evidence and not (entry.answer_must_contain or entry.answer_must_contain_any):
            query_warnings.append("no-evidence query 缺拒答短语检查。")
            action_items.append(f"为 {query_id} 增加 answer_must_contain_any 拒答短语。")
        if entry.min_distinct_doc_ids > len(entry.reference_must_include_doc_ids) and entry.reference_must_include_doc_ids:
            query_warnings.append("min_distinct_doc_ids 大于 reference_must_include_doc_ids 数量。")
        bad_patterns: list[str] = []
        for pattern in entry.answer_must_not_match:
            try:
                re.compile(pattern)
            except re.error as exc:
                bad_patterns.append(f"{pattern}: {exc}")
        if bad_patterns:
            regex_errors[query_id] = bad_patterns
        if query_warnings:
            warnings[query_id] = query_warnings

    if regex_errors:
        status = "fail"
    elif missing or placeholders:
        status = "placeholder"
    elif partials:
        status = "partial"
    else:
        status = "complete"
    return GroundTruthValidation(status, missing, placeholders, partials, regex_errors, warnings, action_items)


def canonicalize_entry(entry: GroundTruthEntry, alias_to_doc: dict[str, str]) -> GroundTruthEntry:
    entry.relevant_doc_ids = [canonical_doc_id(doc_id, alias_to_doc) for doc_id in entry.relevant_doc_ids]
    entry.reference_must_include_doc_ids = [canonical_doc_id(doc_id, alias_to_doc) for doc_id in entry.reference_must_include_doc_ids]
    for evidence in entry.evidence:
        evidence["doc_id"] = canonical_doc_id(str(evidence.get("doc_id") or ""), alias_to_doc)
    return entry


def create_session(base_url: str, api_key: str, timeout: float) -> str:
    status, payload = http_json(
        base_url,
        "api/v1/sessions",
        api_key=api_key,
        method="POST",
        payload={"title": "AlphaMind phase2 quality eval", "description": "phase2 quality automated evaluation"},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"create session HTTP {status}: {str(payload)[:200]}")
    session_id = extract_id(payload)
    if not session_id:
        raise RuntimeError("create session response did not include session id")
    return session_id


def collect_reference_ids(value: Any, knowledge_to_doc: dict[str, str], alias_to_doc: dict[str, str]) -> dict[str, list[str]]:
    doc_ids: set[str] = set()
    knowledge_ids: set[str] = set()
    raw_doc_candidates: set[str] = set()

    def add_doc_candidate(candidate: str) -> None:
        candidate = str(candidate or "").strip()
        if not candidate:
            return
        raw_doc_candidates.add(candidate)
        doc_ids.add(canonical_doc_id(candidate, alias_to_doc))

    def walk(item: Any, key_hint: str = "") -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                lowered = key.lower()
                if lowered in {"doc_id", "docid", "document_id", "documentid"} and isinstance(value, str):
                    add_doc_candidate(value)
                elif lowered in {"knowledge_id", "knowledgeid"} and isinstance(value, str):
                    knowledge_ids.add(value)
                    if value in knowledge_to_doc:
                        doc_ids.add(knowledge_to_doc[value])
                elif lowered in {"file_path", "filepath", "relative_path", "filename", "file_name", "title", "knowledge_filename", "knowledge_title"} and isinstance(value, str):
                    stem = doc_stem_from_path(value)
                    if stem:
                        add_doc_candidate(stem)
                else:
                    walk(value, lowered)
        elif isinstance(item, list):
            for child in item:
                walk(child, key_hint)

    walk(value)
    return {
        "doc_ids": sorted(doc_id for doc_id in doc_ids if doc_id),
        "knowledge_ids": sorted(knowledge_ids),
        "raw_doc_candidates": sorted(raw_doc_candidates),
    }


def event_type_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("response_type") or event.get("type") or event.get("event") or "unknown")
        summary[event_type] = summary.get(event_type, 0) + 1
    return summary


def answer_from_events(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in events:
        if event.get("response_type") == "answer":
            for key in ("content", "delta", "text"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
    if parts:
        return "".join(parts)
    for event in events:
        for key in ("content", "delta", "text"):
            value = event.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "".join(parts)


def evaluate_retrieval_query(
    *,
    query: dict[str, Any],
    entry: GroundTruthEntry,
    base_url: str,
    api_key: str,
    kb_id: str,
    knowledge_to_doc: dict[str, str],
    alias_to_doc: dict[str, str],
    timeout: float,
    match_count: int,
    vector_threshold: float,
    keyword_threshold: float,
    use_filter_expansion: bool,
    skip_context_enrichment: bool,
) -> dict[str, Any]:
    query_id = str(query.get("id") or "")
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
            return {
                "status": "error",
                "http_status": status,
                "elapsed_ms": elapsed_ms,
                "sent_query": sent_query,
                "metrics": {},
                "retrieved_docs": [],
                "evidence_coverage": {},
                "error": str(response)[:500],
            }
        items = extract_items(response)
    except Exception as exc:
        return {
            "status": "error",
            "http_status": 0,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "sent_query": sent_query,
            "metrics": {},
            "retrieved_docs": [],
            "evidence_coverage": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    retrieved_docs, by_doc_items = unique_retrieved_docs(items, knowledge_to_doc, alias_to_doc)
    retrieved_ids = [doc.doc_id for doc in retrieved_docs]
    relevant_set = set(entry.relevant_doc_ids)
    metrics: dict[str, float] = {}
    if entry.scorable_retrieval:
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
    return {
        "status": "pass" if items else "warn",
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "sent_query": sent_query,
        "chunk_count": len(items),
        "retrieved_count": len(retrieved_docs),
        "relevant_count": len(entry.relevant_doc_ids),
        "metrics": metrics,
        "retrieved_docs": [asdict(doc) for doc in retrieved_docs],
        "evidence_coverage": must_contain_coverage(entry.evidence, by_doc_items, alias_to_doc),
        "error": "" if items else "no search results",
        "metric_note": "skipped: placeholder/no-evidence/no relevant_doc_ids" if not metrics else "document-level unique knowledge order",
    }


def evaluate_chat_query(
    *,
    query: dict[str, Any],
    entry: GroundTruthEntry,
    base_url: str,
    api_key: str,
    kb_id: str,
    knowledge_to_doc: dict[str, str],
    alias_to_doc: dict[str, str],
    timeout: float,
    include_answer: bool,
) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        session_id = create_session(base_url, api_key, timeout)
        status, events, raw = http_sse(
            base_url,
            f"api/v1/knowledge-chat/{session_id}",
            api_key=api_key,
            payload={
                "query": str(query.get("text") or ""),
                "knowledge_base_ids": [kb_id],
                "disable_title": True,
                "enable_memory": False,
                "channel": "api",
            },
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if status < 200 or status >= 300:
            return {
                "status": "error",
                "http_status": status,
                "elapsed_ms": elapsed_ms,
                "event_count": len(events),
                "event_type_summary": event_type_summary(events),
                "reference_count": 0,
                "reference_doc_ids": [],
                "distinct_reference_doc_ids": [],
                "answer_preview": "",
                "error": raw[:500],
            }
        answer = answer_from_events(events)
        references = [event for event in events if event.get("response_type") == "references" or event.get("knowledge_references")]
        reference_ids = collect_reference_ids(references, knowledge_to_doc, alias_to_doc)
        distinct_doc_ids = reference_ids["doc_ids"]
        result = {
            "status": "pass" if events else "error",
            "http_status": status,
            "elapsed_ms": elapsed_ms,
            "event_count": len(events),
            "event_type_summary": event_type_summary(events),
            "reference_count": len(references),
            "reference_doc_ids": distinct_doc_ids,
            "distinct_reference_doc_ids": distinct_doc_ids,
            "reference_knowledge_ids": reference_ids["knowledge_ids"],
            "raw_reference_doc_candidates": reference_ids["raw_doc_candidates"],
            "answer_preview": answer[:500],
            "_answer_for_checks": answer,
            "error": "" if events else f"no SSE events parsed; raw={raw[:240]}",
        }
        if include_answer:
            result["answer"] = answer
        return result
    except Exception as exc:
        return {
            "status": "error",
            "http_status": 0,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "event_count": 0,
            "event_type_summary": {},
            "reference_count": 0,
            "reference_doc_ids": [],
            "distinct_reference_doc_ids": [],
            "answer_preview": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def evaluate_citation(entry: GroundTruthEntry, chat_result: dict[str, Any]) -> dict[str, Any]:
    cited = set(chat_result.get("distinct_reference_doc_ids") or [])
    required = set(entry.reference_must_include_doc_ids)
    matched = sorted(required & cited)
    missing = sorted(required - cited)
    extra = sorted(cited - required)
    recall_value = len(matched) / len(required) if required else None
    precision_value = len(matched) / len(cited) if cited else (None if not required else 0.0)
    distinct_pass = len(cited) >= entry.min_distinct_doc_ids
    if not entry.requires_references and not required:
        status = "skipped"
    elif entry.scorable_citation:
        status = "pass" if not missing and distinct_pass else "fail"
    else:
        status = "skipped"
    return {
        "status": status,
        "required_doc_ids": sorted(required),
        "cited_doc_ids": sorted(cited),
        "matched_doc_ids": matched,
        "missing_doc_ids": missing,
        "extra_doc_ids": extra,
        "citation_doc_recall": recall_value,
        "citation_doc_precision": precision_value,
        "min_distinct_doc_ids": entry.min_distinct_doc_ids,
        "min_distinct_doc_ids_pass": distinct_pass,
    }


def evaluate_answer(entry: GroundTruthEntry, answer: str) -> dict[str, Any]:
    required = entry.answer_must_contain
    required_any = entry.answer_must_contain_any
    matched = [term for term in required if contains_text(answer, term)]
    missing = [term for term in required if term not in matched]
    any_matched = [term for term in required_any if contains_text(answer, term)]
    forbidden_terms = [term for term in entry.answer_must_not_contain if contains_text(answer, term)]
    forbidden_patterns: list[str] = []
    for pattern in entry.answer_must_not_match:
        try:
            if re.search(pattern, answer, flags=re.IGNORECASE):
                forbidden_patterns.append(pattern)
        except re.error:
            forbidden_patterns.append(f"INVALID_REGEX:{pattern}")
    must_contain_pass = not missing
    must_contain_any_pass = True if not required_any else bool(any_matched)
    must_not_pass = not forbidden_terms and not forbidden_patterns
    status = "pass" if must_contain_pass and must_contain_any_pass and must_not_pass else "fail"
    if not required and not required_any and not entry.answer_must_not_contain and not entry.answer_must_not_match:
        status = "skipped"
    return {
        "status": status,
        "matched_must_contain": matched,
        "missing_must_contain": missing,
        "matched_must_contain_any": any_matched,
        "must_contain_any_candidates": required_any,
        "forbidden_term_violations": forbidden_terms,
        "forbidden_pattern_violations": forbidden_patterns,
        "must_contain_pass": must_contain_pass,
        "must_contain_any_pass": must_contain_any_pass,
        "must_not_pass": must_not_pass,
    }


def evaluate_no_evidence(entry: GroundTruthEntry, answer_checks: dict[str, Any]) -> dict[str, Any]:
    if not entry.is_no_evidence:
        return {"status": "skipped"}
    refusal_pass = bool(answer_checks.get("matched_must_contain_any")) or bool(answer_checks.get("matched_must_contain"))
    violations = list(answer_checks.get("forbidden_term_violations") or []) + list(answer_checks.get("forbidden_pattern_violations") or [])
    status = "pass" if refusal_pass and not violations else "fail"
    return {
        "status": status,
        "refusal_pass": refusal_pass,
        "fabrication_violations": violations,
    }


def query_status(entry: GroundTruthEntry, retrieval_result: dict[str, Any] | None, chat_result: dict[str, Any] | None, citation: dict[str, Any], answer: dict[str, Any], no_evidence: dict[str, Any]) -> str:
    if retrieval_result and retrieval_result.get("status") == "error":
        return "fail"
    if chat_result and chat_result.get("status") == "error":
        return "fail"
    if entry.is_placeholder or entry.is_partial:
        return "warn"
    if no_evidence.get("status") == "fail":
        return "fail"
    if citation.get("status") == "fail":
        return "fail"
    if answer.get("status") == "fail":
        return "fail"
    return "pass"


def aggregate_results(query_results: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    retrieval_results = [result.get("retrieval") or {} for result in query_results]
    metric_results = [result for result in retrieval_results if result.get("metrics")]
    for name in RETRIEVAL_METRICS:
        values = [float(result["metrics"][name]) for result in metric_results if name in result.get("metrics", {})]
        aggregate[f"mean_{name}"] = sum(values) / len(values) if values else None
    retrieval_elapsed = [float(result.get("elapsed_ms") or 0.0) for result in retrieval_results if result]
    aggregate["mean_retrieval_elapsed_ms"] = sum(retrieval_elapsed) / len(retrieval_elapsed) if retrieval_elapsed else None

    chat_results = [result.get("chat") or {} for result in query_results if result.get("chat")]
    aggregate["chat_success_rate"] = sum(1 for result in chat_results if result.get("status") == "pass") / len(chat_results) if chat_results else None
    evidence_chat_results = [result for result in query_results if not (result.get("ground_truth") or {}).get("is_no_evidence") and result.get("chat")]
    aggregate["chat_reference_rate"] = (
        sum(1 for result in evidence_chat_results if (result.get("chat") or {}).get("reference_count", 0) > 0) / len(evidence_chat_results)
        if evidence_chat_results
        else None
    )
    distinct_counts = [len((result.get("chat") or {}).get("distinct_reference_doc_ids") or []) for result in chat_results]
    aggregate["mean_distinct_reference_doc_ids"] = sum(distinct_counts) / len(distinct_counts) if distinct_counts else None

    citation_checks = [result.get("citation_checks") or {} for result in query_results]
    citation_recalls = [float(check["citation_doc_recall"]) for check in citation_checks if check.get("citation_doc_recall") is not None]
    citation_precisions = [float(check["citation_doc_precision"]) for check in citation_checks if check.get("citation_doc_precision") is not None]
    distinct_checks = [check for check in citation_checks if check.get("status") in {"pass", "fail"}]
    aggregate["mean_citation_doc_recall"] = sum(citation_recalls) / len(citation_recalls) if citation_recalls else None
    aggregate["mean_citation_doc_precision"] = sum(citation_precisions) / len(citation_precisions) if citation_precisions else None
    aggregate["min_distinct_doc_ids_pass_rate"] = (
        sum(1 for check in distinct_checks if check.get("min_distinct_doc_ids_pass")) / len(distinct_checks) if distinct_checks else None
    )

    answer_checks = [result.get("answer_checks") or {} for result in query_results]
    must_checks = [check for check in answer_checks if check.get("status") in {"pass", "fail"}]
    aggregate["answer_must_contain_pass_rate"] = sum(1 for check in must_checks if check.get("must_contain_pass") and check.get("must_contain_any_pass")) / len(must_checks) if must_checks else None
    aggregate["answer_must_not_violation_rate"] = sum(1 for check in must_checks if not check.get("must_not_pass")) / len(must_checks) if must_checks else None

    no_evidence_checks = [
        result.get("no_evidence_checks") or {}
        for result in query_results
        if (result.get("ground_truth") or {}).get("is_no_evidence")
        and (result.get("no_evidence_checks") or {}).get("status") in {"pass", "fail"}
    ]
    aggregate["no_evidence_refusal_rate"] = sum(1 for check in no_evidence_checks if check.get("refusal_pass")) / len(no_evidence_checks) if no_evidence_checks else None
    aggregate["no_evidence_fabrication_violation_rate"] = sum(1 for check in no_evidence_checks if check.get("fabrication_violations")) / len(no_evidence_checks) if no_evidence_checks else None
    return aggregate


def target_assessment(metrics: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    def add_check(name: str, metric_key: str, target_key: str, default: float, *, higher_is_better: bool = True) -> None:
        actual = metrics.get(metric_key)
        target = float(targets.get(target_key, default))
        if actual is None:
            status = "skip"
        elif higher_is_better:
            status = "pass" if float(actual) >= target else "warn"
        else:
            status = "pass" if float(actual) <= target else "warn"
        checks[name] = {"actual": actual, "target": target, "status": status}

    add_check("recall@10", "mean_recall@10", "recall_at_10_target", 0.8)
    add_check("citation_doc_recall", "mean_citation_doc_recall", "citation_doc_recall_target", float(targets.get("citation_accuracy_target", 0.85)))
    add_check("chat_reference_rate", "chat_reference_rate", "chat_reference_rate_target", 0.9)
    add_check("answer_must_contain", "answer_must_contain_pass_rate", "answer_must_contain_rate_target", 0.8)
    add_check("no_evidence_refusal", "no_evidence_refusal_rate", "no_evidence_refusal_rate_target", 1.0)
    add_check("no_evidence_fabrication", "no_evidence_fabrication_violation_rate", "hallucination_violation_rate_max", 0.0, higher_is_better=False)
    active = [item for item in checks.values() if item["status"] != "skip"]
    status = "pass" if active and all(item["status"] == "pass" for item in active) else "warn"
    if not active:
        status = "skip"
    return {"status": status, "checks": checks}


def validation_summary(
    ground_truth_validation: GroundTruthValidation,
    query_results: list[dict[str, Any]],
    target_status: dict[str, Any],
    *,
    strict: bool,
    validate_only: bool,
) -> dict[str, Any]:
    def has_transport_error(value: dict[str, Any] | None) -> bool:
        if not isinstance(value, dict):
            return False
        if value.get("status") == "error":
            return True
        try:
            http_status = int(value.get("http_status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        return bool(http_status and (http_status < 200 or http_status >= 300))

    http_errors = {
        result["query_id"]: {
            key: value.get("error")
            for key, value in (("retrieval", result.get("retrieval")), ("chat", result.get("chat")))
            if has_transport_error(value) and value.get("error")
        }
        for result in query_results
    }
    http_errors = {query_id: errors for query_id, errors in http_errors.items() if errors}
    hard_query_ids = {result["query_id"] for result in query_results if result.get("status") == "fail"}
    citation_failures = {
        result["query_id"]: result.get("citation_checks")
        for result in query_results
        if result["query_id"] in hard_query_ids and (result.get("citation_checks") or {}).get("status") == "fail"
    }
    answer_failures = {
        result["query_id"]: result.get("answer_checks")
        for result in query_results
        if result["query_id"] in hard_query_ids and (result.get("answer_checks") or {}).get("status") == "fail"
    }
    no_evidence_violations = {
        result["query_id"]: result.get("no_evidence_checks")
        for result in query_results
        if result["query_id"] in hard_query_ids and (result.get("no_evidence_checks") or {}).get("status") == "fail"
    }
    setup_status = "pass" if validate_only or not http_errors else "fail"
    hard_quality_failures = bool(citation_failures or answer_failures or no_evidence_violations)
    if setup_status == "fail" or ground_truth_validation.regex_errors:
        quality_status = "fail"
    elif strict and ground_truth_validation.status in {"placeholder", "partial"}:
        quality_status = "fail"
    elif hard_quality_failures:
        quality_status = "fail"
    elif ground_truth_validation.status in {"placeholder", "partial"} or target_status.get("status") == "warn":
        quality_status = "warn"
    else:
        quality_status = "pass"
    return {
        "setup_status": setup_status,
        "ground_truth_status": ground_truth_validation.status,
        "quality_status": quality_status,
        "missing_ground_truth": ground_truth_validation.missing_ground_truth,
        "placeholder_queries": ground_truth_validation.placeholder_queries,
        "partial_queries": ground_truth_validation.partial_queries,
        "regex_errors": ground_truth_validation.regex_errors,
        "ground_truth_warnings": ground_truth_validation.warnings,
        "http_errors": http_errors,
        "citation_failures": citation_failures,
        "answer_failures": answer_failures,
        "no_evidence_violations": no_evidence_violations,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    env = parse_env_file(Path(args.env_file))
    base_url = args.base_url or env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")
    api_key = args.api_key or env_value(env, "WEKNORA_API_KEY", "")
    kb_id = args.kb_id
    if not kb_id and Path(args.kb_id_file).exists():
        kb_id = Path(args.kb_id_file).read_text(encoding="utf-8").strip()

    queries_payload = load_json(Path(args.queries))
    ground_truth_payload = load_json(Path(args.ground_truth))
    queries = [query for query in queries_payload.get("queries", []) if isinstance(query, dict)]
    retrieval_config = load_json(Path(args.retrieval_config)) if Path(args.retrieval_config).exists() else {}

    acceptance_report = Path(args.acceptance_report) if args.acceptance_report else latest_acceptance_report(DEFAULT_PHASE2_RUN_DIR)
    ingest_reports = [Path(path) for path in args.ingest_report] if args.ingest_report else ingest_reports_from_dir(DEFAULT_PHASE2_RUN_DIR)
    knowledge_to_doc, doc_to_knowledge, alias_to_doc = build_knowledge_map(DEFAULT_PHASE2_RUN_DIR, acceptance_report, ingest_reports)
    if kb_id:
        merge_kb_knowledge_map_from_api(
            knowledge_to_doc,
            doc_to_knowledge,
            alias_to_doc,
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            timeout=args.timeout,
        )
    gt_entries, gt_validation = load_ground_truth_entries(queries, ground_truth_payload, alias_to_doc)
    gt_entries = {query_id: canonicalize_entry(entry, alias_to_doc) for query_id, entry in gt_entries.items()}
    gt_validation = validate_ground_truth(queries, gt_entries)

    match_count = args.match_count or int(retrieval_config.get("rerank_top_k") or 10)
    vector_threshold = args.vector_threshold if args.vector_threshold is not None else float(retrieval_config.get("vector_threshold") or 0.0)
    keyword_threshold = args.keyword_threshold if args.keyword_threshold is not None else float(retrieval_config.get("keyword_threshold") or 0.0)
    use_filter_expansion = not args.no_filter_expansion

    query_results: list[dict[str, Any]] = []
    if not args.validate_only:
        if not kb_id:
            raise RuntimeError("KB id is required. Provide --kb-id or --kb-id-file.")
        for query in queries:
            query_id = str(query.get("id") or "")
            entry = gt_entries.get(query_id) or GroundTruthEntry(query_id=query_id)
            retrieval_result: dict[str, Any] | None = None
            chat_result: dict[str, Any] | None = None
            if args.mode in {"all", "retrieval"}:
                retrieval_result = evaluate_retrieval_query(
                    query=query,
                    entry=entry,
                    base_url=base_url,
                    api_key=api_key,
                    kb_id=kb_id,
                    knowledge_to_doc=knowledge_to_doc,
                    alias_to_doc=alias_to_doc,
                    timeout=args.timeout,
                    match_count=match_count,
                    vector_threshold=vector_threshold,
                    keyword_threshold=keyword_threshold,
                    use_filter_expansion=use_filter_expansion,
                    skip_context_enrichment=args.skip_context_enrichment,
                )
            if args.mode in {"all", "chat"}:
                chat_result = evaluate_chat_query(
                    query=query,
                    entry=entry,
                    base_url=base_url,
                    api_key=api_key,
                    kb_id=kb_id,
                    knowledge_to_doc=knowledge_to_doc,
                    alias_to_doc=alias_to_doc,
                    timeout=args.chat_timeout,
                    include_answer=args.include_answer,
                )
            answer_text = str((chat_result or {}).get("_answer_for_checks") or (chat_result or {}).get("answer") or (chat_result or {}).get("answer_preview") or "")
            if chat_result and "_answer_for_checks" in chat_result:
                chat_result = dict(chat_result)
                chat_result.pop("_answer_for_checks", None)
            citation_checks = evaluate_citation(entry, chat_result or {}) if chat_result else {"status": "skipped"}
            answer_checks = evaluate_answer(entry, answer_text) if chat_result else {"status": "skipped"}
            no_evidence_checks = evaluate_no_evidence(entry, answer_checks) if chat_result else {"status": "skipped"}
            query_results.append(
                {
                    "query_id": query_id,
                    "query": str(query.get("text") or ""),
                    "type": str(query.get("type") or ""),
                    "expected_behavior": entry.expected_behavior,
                    "ground_truth": {
                        "gt_status": entry.gt_status,
                        "is_no_evidence": entry.is_no_evidence,
                        "relevant_doc_ids": entry.relevant_doc_ids,
                        "reference_must_include_doc_ids": entry.reference_must_include_doc_ids,
                        "min_distinct_doc_ids": entry.min_distinct_doc_ids,
                        "requires_references": entry.requires_references,
                    },
                    "retrieval": retrieval_result,
                    "chat": chat_result,
                    "citation_checks": citation_checks,
                    "answer_checks": answer_checks,
                    "no_evidence_checks": no_evidence_checks,
                    "status": query_status(entry, retrieval_result, chat_result, citation_checks, answer_checks, no_evidence_checks),
                    "warnings": gt_validation.warnings.get(query_id, []),
                }
            )

    aggregate = aggregate_results(query_results)
    target_status = target_assessment(aggregate, ground_truth_payload.get("metrics", {}) if isinstance(ground_truth_payload, dict) else {})
    validation = validation_summary(gt_validation, query_results, target_status, strict=args.strict, validate_only=args.validate_only)
    action_items = sorted(set(gt_validation.action_items))
    if validation["citation_failures"]:
        action_items.append("检查 citation_failures 中的 query：若检索已命中但引用缺失，下一步调 knowledge-chat prompt/reference policy。")
    if validation["no_evidence_violations"]:
        action_items.append("no-evidence guardrail 未通过：下一步改服务端拒答 prompt/answer policy，再复跑 evaluator。")

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
            "mode": args.mode,
            "validate_only": args.validate_only,
            "match_count": match_count,
            "vector_threshold": vector_threshold,
            "keyword_threshold": keyword_threshold,
            "use_filter_expansion": use_filter_expansion,
            "skip_context_enrichment": args.skip_context_enrichment,
            "include_answer": args.include_answer,
            "strict": args.strict,
        },
        "aggregate_metrics": aggregate,
        "target_assessment": target_status,
        "query_results": query_results,
        "validation": validation,
        "action_items": action_items,
    }


def print_summary(result: dict[str, Any], out_path: Path | None) -> None:
    validation = result["validation"]
    metrics = result["aggregate_metrics"]
    print("AlphaMind Phase2 quality eval")
    print("=" * 80)
    print(
        f"setup={validation['setup_status']} ground_truth={validation['ground_truth_status']} "
        f"quality={validation['quality_status']} mode={result['settings']['mode']} kb={result['weknora']['kb_id']}"
    )
    if out_path:
        print(f"out={out_path}")
    metric_line = []
    for key in ("mean_recall@10", "mean_reciprocal_rank", "chat_reference_rate", "mean_citation_doc_recall", "answer_must_contain_pass_rate", "no_evidence_refusal_rate"):
        value = metrics.get(key)
        if value is not None:
            metric_line.append(f"{key}={value:.3f}")
    if metric_line:
        print(" ".join(metric_line))
    if result.get("action_items"):
        print("Action items:")
        for item in result["action_items"][:10]:
            print(f"- {item}")


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        result = evaluate(args)
    except Exception as exc:
        print(f"AlphaMind Phase2 quality eval failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out_path: Path | None = None
    if not args.validate_only:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result, out_path)

    validation = result["validation"]
    if validation["setup_status"] == "fail" or validation["quality_status"] == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
