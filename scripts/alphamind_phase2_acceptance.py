#!/usr/bin/env python3
"""
AlphaMind phase2-advanced end-to-end acceptance.

Validates the quotation's phase-2 scope:
- WeKnora app health
- phase2 knowledge base creation/reuse with MinIO + GraphRAG config
- small-batch PDF/Office/table upload using alphamind_bulk_ingest metadata/upload helpers
- parse completion
- chunks and hybrid search
- knowledge chat with references
- Neo4j graph smoke checks for basic entity/relation materialization

Uses only the Python standard library plus local AlphaMind scripts.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from alphamind_bulk_ingest import (
    DEFAULT_EXTENSIONS,
    UploadResult,
    apply_prefer_originals,
    build_metadata_records,
    iter_files,
    load_json,
    normalized_extensions,
    upload_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase2-advanced"
DEFAULT_KB_CONFIG = PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase2-advanced.json"
DEFAULT_PROCESS_CONFIG = PROJECT_ROOT / "dataset" / "alphamind_process_config_phase2_advanced.json"
DEFAULT_RETRIEVAL_CONFIG = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase2_advanced.json"
DEFAULT_KB_ID_FILE = PROJECT_ROOT / "dataset" / "alphamind_phase2_advanced_kb_id.txt"
DEFAULT_SAMPLE_ROOT = PROJECT_ROOT / "数据" / "Documents(1)"
DEFAULT_TABLE_ROOT = PROJECT_ROOT / "数据" / "数据库" / "Documents"
DEFAULT_RUN_DIR = PROJECT_ROOT / "dataset" / "phase2_runs"

DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx")
TABLE_EXTENSIONS = (".csv", ".xls", ".xlsx", ".json")


@dataclass
class StepResult:
    name: str
    status: str  # pass / fail / warn / skip
    detail: str
    elapsed_ms: int = 0


@dataclass
class UploadRecord:
    file_path: str
    relative_path: str
    doc_id: str
    knowledge_id: str
    status: str
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind phase2-advanced acceptance against WeKnora.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--base-url", default="", help="Override WEKNORA_BASE_URL, default from env file.")
    parser.add_argument("--api-key", default="", help="Override WEKNORA_API_KEY, default from env file.")
    parser.add_argument("--kb-config", default=str(DEFAULT_KB_CONFIG))
    parser.add_argument("--process-config", default=str(DEFAULT_PROCESS_CONFIG))
    parser.add_argument("--retrieval-config", default=str(DEFAULT_RETRIEVAL_CONFIG))
    parser.add_argument("--kb-id-file", default=str(DEFAULT_KB_ID_FILE))
    parser.add_argument("--sample-root", default=str(DEFAULT_SAMPLE_ROOT), help="Root containing phase2 PDF/Office samples.")
    parser.add_argument("--table-root", default=str(DEFAULT_TABLE_ROOT), help="Optional root containing simple table samples.")
    parser.add_argument("--out-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--limit", type=int, default=5, help="Max PDF/Office samples to upload. 0 means unlimited.")
    parser.add_argument("--table-limit", type=int, default=1, help="Max table samples to upload. 0 means none.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    parser.add_argument("--channel", default="alphamind-phase2-acceptance")
    parser.add_argument("--doc-id-mode", choices=("stem", "stem-ext", "hash"), default="stem-ext")
    parser.add_argument("--hash-files", action="store_true")
    parser.add_argument("--skip-chat", action="store_true", help="Skip knowledge-chat step; search/chunk checks still run.")
    parser.add_argument("--skip-graph", action="store_true", help="Skip Neo4j graph smoke checks.")
    parser.add_argument("--neo4j-url", default=os.environ.get("NEO4J_HTTP_URL", ""))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USERNAME", ""))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args()


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


def timed(name: str, func) -> StepResult:
    start = time.perf_counter()
    try:
        status, detail = func()
        return StepResult(name, status, detail, int((time.perf_counter() - start) * 1000))
    except Exception as exc:
        return StepResult(name, "fail", f"{type(exc).__name__}: {exc}", int((time.perf_counter() - start) * 1000))


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
    timeout: float = 20.0,
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


def http_request(
    url: str,
    *,
    method: str = "GET",
    request_headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, str]:
    request = Request(url, data=body, method=method)
    for key, value in (request_headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
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


def ensure_kb(
    *,
    base_url: str,
    api_key: str,
    kb_config: dict[str, Any],
    kb_id_file: Path,
    timeout: float,
) -> tuple[str, list[StepResult]]:
    steps: list[StepResult] = []
    stored = kb_id_file.read_text(encoding="utf-8", errors="ignore").strip() if kb_id_file.exists() else ""
    if stored:
        def check_stored() -> tuple[str, str]:
            status, payload = http_json(base_url, f"api/v1/knowledge-bases/{stored}", api_key=api_key, timeout=timeout)
            if 200 <= status < 300:
                data = extract_data(payload)
                name = data.get("name") if isinstance(data, dict) else ""
                return "pass", f"reuse stored KB {stored} ({name})"
            return "warn", f"stored KB {stored} not reusable: HTTP {status} {str(payload)[:160]}"
        result = timed("kb_reuse", check_stored)
        steps.append(result)
        if result.status == "pass":
            return stored, steps

    def find_existing() -> tuple[str, str]:
        status, payload = http_json(base_url, "api/v1/knowledge-bases", api_key=api_key, timeout=timeout)
        if status < 200 or status >= 300:
            return "warn", f"list KB failed: HTTP {status} {str(payload)[:160]}"
        target_name = kb_config.get("name")
        for item in extract_items(payload):
            if item.get("name") == target_name and isinstance(item.get("id"), str):
                kb_id_file.parent.mkdir(parents=True, exist_ok=True)
                kb_id_file.write_text(item["id"], encoding="utf-8")
                return "pass", f"found existing KB {item['id']}"
        return "warn", "no existing phase2 KB found"
    find_result = timed("kb_find_existing", find_existing)
    steps.append(find_result)
    if find_result.status == "pass":
        kb_id = kb_id_file.read_text(encoding="utf-8").strip()
        return kb_id, steps

    def create() -> tuple[str, str]:
        status, payload = http_json(base_url, "api/v1/knowledge-bases", api_key=api_key, method="POST", payload=kb_config, timeout=timeout)
        if status < 200 or status >= 300:
            return "fail", f"HTTP {status}: {str(payload)[:300]}"
        kb_id = extract_id(payload)
        if not kb_id:
            return "fail", "create KB response did not include id"
        kb_id_file.parent.mkdir(parents=True, exist_ok=True)
        kb_id_file.write_text(kb_id, encoding="utf-8")
        return "pass", f"created KB {kb_id}"
    create_result = timed("kb_create", create)
    steps.append(create_result)
    if create_result.status != "pass":
        return "", steps
    return kb_id_file.read_text(encoding="utf-8").strip(), steps


def select_sample_records(args: argparse.Namespace) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    warnings: list[str] = []
    records: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()

    sample_root = Path(args.sample_root).resolve()
    if sample_root.exists() and sample_root.is_dir():
        doc_files = iter_files(sample_root, normalized_extensions(DOCUMENT_EXTENSIONS))
        doc_files, skipped = apply_prefer_originals(doc_files, sample_root)
        if skipped:
            warnings.append(f"prefer_originals_skipped={len(skipped)}")
        if args.limit > 0:
            doc_files = doc_files[: args.limit]
        for path, metadata in build_metadata_records(
            doc_files,
            sample_root,
            doc_id_mode=args.doc_id_mode,
            channel=args.channel,
            hash_files=args.hash_files,
        ):
            records.append((path, metadata))
            seen.add(str(path.resolve()))
    else:
        warnings.append(f"sample_root_missing={sample_root}")

    table_root = Path(args.table_root).resolve()
    if args.table_limit > 0:
        if table_root.exists() and table_root.is_dir():
            table_files = [path for path in iter_files(table_root, normalized_extensions(TABLE_EXTENSIONS)) if str(path.resolve()) not in seen]
            table_files = table_files[: args.table_limit]
            records.extend(
                build_metadata_records(
                    table_files,
                    table_root,
                    doc_id_mode=args.doc_id_mode,
                    channel=args.channel,
                    hash_files=args.hash_files,
                )
            )
        else:
            warnings.append(f"table_root_missing={table_root}")

    return records, warnings


def poll_knowledge_completed(
    *,
    base_url: str,
    api_key: str,
    knowledge_id: str,
    timeout: float,
    poll_seconds: float,
    poll_timeout: float,
) -> tuple[str, str]:
    deadline = time.time() + poll_timeout
    last_status = ""
    last_payload = ""
    while time.time() < deadline:
        status, payload = http_json(base_url, f"api/v1/knowledge/{knowledge_id}", api_key=api_key, timeout=timeout)
        if status < 200 or status >= 300:
            last_payload = f"HTTP {status}: {str(payload)[:200]}"
        else:
            data = extract_data(payload)
            if isinstance(data, dict):
                parse_status = data.get("parse_status") or data.get("status") or ""
                last_status = str(parse_status)
                if parse_status == "completed":
                    return "pass", "completed"
                if parse_status == "failed":
                    code = data.get("parse_error_code") or data.get("error_code") or ""
                    message = data.get("parse_error_message") or data.get("error_message") or ""
                    return "fail", f"parse_status=failed {code} {message}".strip()
                last_payload = f"parse_status={parse_status}"
        time.sleep(poll_seconds)
    return "fail", f"timeout waiting for completed; last_status={last_status}; {last_payload}"


def chunk_check(
    *,
    base_url: str,
    api_key: str,
    knowledge_id: str,
    metadata: dict[str, Any],
    timeout: float,
) -> tuple[str, str]:
    status, payload = http_json(
        base_url,
        f"api/v1/chunks/{knowledge_id}",
        api_key=api_key,
        query={"page": 1, "page_size": 30},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {str(payload)[:200]}"
    chunks = extract_items(payload)
    if not chunks:
        return "fail", "no chunks returned"
    contents = "\n".join(str(chunk.get("content", "")) for chunk in chunks)
    expected_terms = [
        str(metadata.get("company") or ""),
        str(metadata.get("ticker") or ""),
        str(metadata.get("broker") or ""),
    ]
    expected_terms = [term for term in expected_terms if term]
    if expected_terms and not any(term in contents for term in expected_terms):
        return "warn", f"{len(chunks)} chunks returned but metadata terms not in first page: {expected_terms[:3]}"
    return "pass", f"{len(chunks)} chunks returned"


def search_query_for(metadata: dict[str, Any]) -> str:
    terms = [
        str(metadata.get("company") or ""),
        str(metadata.get("ticker") or ""),
        str(metadata.get("broker") or ""),
    ]
    terms = [term for term in terms if term]
    if terms:
        return " ".join(terms[:3])
    title = str(metadata.get("title") or "")
    return title[:40] or str(metadata.get("doc_id") or "")


def hybrid_search_check(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    query_text: str,
    retrieval_config: dict[str, Any],
    timeout: float,
) -> tuple[str, str]:
    payload = {
        "query_text": query_text,
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
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {str(response)[:240]}"
    results = extract_items(response)
    if not results:
        return "fail", "no search results"
    top_content = str(results[0].get("content", ""))
    return "pass", f"{len(results)} results; query={query_text!r}; top={top_content[:100]!r}"


def create_session(base_url: str, api_key: str, timeout: float) -> str:
    status, payload = http_json(
        base_url,
        "api/v1/sessions",
        api_key=api_key,
        method="POST",
        payload={"title": "AlphaMind phase2 acceptance", "description": "phase2-advanced automated acceptance"},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"create session HTTP {status}: {str(payload)[:200]}")
    session_id = extract_id(payload)
    if not session_id:
        raise RuntimeError("create session response did not include session id")
    return session_id


def collect_reference_doc_ids(value: Any) -> set[str]:
    doc_ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in {"doc_id", "docid", "document_id", "documentid"} and isinstance(item, str) and item:
                doc_ids.add(item)
            elif lowered == "metadata" and isinstance(item, dict):
                doc_ids.update(collect_reference_doc_ids(item))
            else:
                doc_ids.update(collect_reference_doc_ids(item))
    elif isinstance(value, list):
        for item in value:
            doc_ids.update(collect_reference_doc_ids(item))
    return doc_ids


def chat_check(*, base_url: str, api_key: str, kb_id: str, query: str, timeout: float) -> tuple[str, str]:
    session_id = create_session(base_url, api_key, timeout)
    status, events, raw = http_sse(
        base_url,
        f"api/v1/knowledge-chat/{session_id}",
        api_key=api_key,
        payload={
            "query": query,
            "knowledge_base_ids": [kb_id],
            "disable_title": True,
            "enable_memory": False,
            "channel": "api",
        },
        timeout=max(timeout, 180.0),
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {raw[:240]}"
    answer = "".join(str(event.get("content", "")) for event in events if event.get("response_type") == "answer")
    references = [event for event in events if event.get("response_type") == "references" or event.get("knowledge_references")]
    doc_ids = collect_reference_doc_ids(references)
    if not events:
        return "fail", f"no SSE events parsed; raw={raw[:240]}"
    if not references:
        return "warn", f"chat returned {len(events)} events but no references; answer_preview={answer[:160]!r}"
    if len(doc_ids) < 2:
        return "warn", f"references returned but distinct doc_ids={len(doc_ids)}; answer_preview={answer[:160]!r}"
    return "pass", f"events={len(events)} references={len(references)} distinct_doc_ids={len(doc_ids)} answer_preview={answer[:160]!r}"


def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def neo4j_query(
    *,
    url: str,
    username: str,
    password: str,
    database: str,
    statement: str,
    timeout: float,
) -> tuple[str, Any]:
    endpoint = urljoin(url.rstrip("/") + "/", f"db/{database}/tx/commit")
    body = json.dumps({"statements": [{"statement": statement}]}).encode("utf-8")
    status, response_body = http_request(
        endpoint,
        method="POST",
        request_headers={
            "Content-Type": "application/json",
            "Authorization": basic_auth_header(username, password),
        },
        body=body,
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {response_body[:200]}"
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return "fail", f"non-JSON response: {response_body[:200]}"
    errors = payload.get("errors") or []
    if errors:
        return "fail", errors[:1]
    return "pass", payload


def first_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    results = payload.get("results") or []
    if not results:
        return 0
    data = results[0].get("data") or []
    if not data:
        return 0
    row = data[0].get("row") or []
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def graph_smoke_check(
    *,
    neo4j_url: str,
    username: str,
    password: str,
    database: str,
    timeout: float,
) -> tuple[str, str]:
    if not neo4j_url:
        return "fail", "Neo4j URL missing"
    if not username or not password:
        return "fail", "Neo4j username/password missing"

    checks = {
        "nodes": "MATCH (n) RETURN count(n) AS count",
        "relations": "MATCH ()-[r]->() RETURN count(r) AS count",
        "Company": "MATCH (n:Company) RETURN count(n) AS count",
        "Report": "MATCH (n:Report) RETURN count(n) AS count",
        "Broker": "MATCH (n:Broker) RETURN count(n) AS count",
        "COVERS": "MATCH ()-[r:COVERS]->() RETURN count(r) AS count",
        "PUBLISHED_BY": "MATCH ()-[r:PUBLISHED_BY]->() RETURN count(r) AS count",
    }
    counts: dict[str, int] = {}
    failures: list[str] = []
    for name, statement in checks.items():
        status, payload = neo4j_query(
            url=neo4j_url,
            username=username,
            password=password,
            database=database,
            statement=statement,
            timeout=timeout,
        )
        if status != "pass":
            failures.append(f"{name}: {payload}")
            continue
        counts[name] = first_count(payload)
    if failures:
        return "fail", "; ".join(failures[:3])
    if counts.get("nodes", 0) <= 0 or counts.get("relations", 0) <= 0:
        return "fail", f"graph empty: {counts}"
    missing = [name for name in ("Company", "Report", "COVERS") if counts.get(name, 0) <= 0]
    if missing:
        return "warn", f"graph reachable but missing phase2 labels/relations {missing}: {counts}"
    return "pass", json.dumps(counts, ensure_ascii=False, separators=(",", ":"))


def write_reports(out_dir: Path, steps: list[StepResult], uploads: list[UploadRecord]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"phase2_acceptance_{stamp}.json"
    csv_path = out_dir / f"phase2_uploads_{stamp}.csv"
    json_path.write_text(
        json.dumps({"steps": [asdict(step) for step in steps], "uploads": [asdict(upload) for upload in uploads]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file_path", "relative_path", "doc_id", "knowledge_id", "status", "detail"])
        writer.writeheader()
        for upload in uploads:
            writer.writerow(asdict(upload))
    return json_path, csv_path


def print_summary(steps: list[StepResult], uploads: list[UploadRecord], json_path: Path, csv_path: Path) -> None:
    print("\nAlphaMind phase2-advanced acceptance")
    print("=" * 110)
    for step in steps:
        print(f"{step.status.upper():<5} {step.name:<40} {step.elapsed_ms:>6}ms  {step.detail}")
    if uploads:
        print("\nUploads")
        for upload in uploads:
            print(f"{upload.status.upper():<7} {Path(upload.file_path).name:<46} {upload.knowledge_id:<36} {upload.detail}")
    print("=" * 110)
    print(f"Report JSON: {json_path}")
    print(f"Upload CSV:  {csv_path}")
    failures = [step for step in steps if step.status == "fail"]
    warnings = [step for step in steps if step.status == "warn"]
    print(f"failures={len(failures)}, warnings={len(warnings)}, steps={len(steps)}, uploads={len(uploads)}")


def exit_with_reports(args: argparse.Namespace, steps: list[StepResult], uploads: list[UploadRecord]) -> int:
    json_path, csv_path = write_reports(Path(args.out_dir), steps, uploads)
    if args.json:
        print(json.dumps({"steps": [asdict(step) for step in steps], "uploads": [asdict(upload) for upload in uploads]}, ensure_ascii=False, indent=2))
    else:
        print_summary(steps, uploads, json_path, csv_path)
    return 1 if any(step.status == "fail" for step in steps) else 0


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    env = parse_env_file(env_file)
    base_url = (args.base_url or env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")).rstrip("/")
    api_key = args.api_key or env_value(env, "WEKNORA_API_KEY")
    neo4j_url = args.neo4j_url or env_value(env, "NEO4J_HTTP_URL", "http://localhost:7474")
    neo4j_user = args.neo4j_user or env_value(env, "NEO4J_USERNAME", "neo4j")
    neo4j_password = args.neo4j_password or env_value(env, "NEO4J_PASSWORD")
    neo4j_database = args.neo4j_database or env_value(env, "NEO4J_DATABASE", "neo4j")

    kb_config_path = Path(args.kb_config)
    process_config_path = Path(args.process_config)
    retrieval_config_path = Path(args.retrieval_config)
    kb_id_file = Path(args.kb_id_file)

    steps: list[StepResult] = []
    uploads: list[UploadRecord] = []
    upload_results: list[UploadResult] = []

    sample_records, sample_warnings = select_sample_records(args)

    def check_files() -> tuple[str, str]:
        required_paths = [env_file, kb_config_path, process_config_path, retrieval_config_path]
        missing = [str(path) for path in required_paths if not path.exists()]
        if missing:
            return "fail", "missing: " + "; ".join(missing)
        if not sample_records:
            return "fail", "no phase2 samples selected"
        detail = f"sample_records={len(sample_records)}"
        if sample_warnings:
            detail += "; " + "; ".join(sample_warnings)
            return "warn", detail
        return "pass", detail
    steps.append(timed("local_files", check_files))
    if steps[-1].status == "fail":
        return exit_with_reports(args, steps, uploads)

    kb_config = load_json(str(kb_config_path))
    process_config = load_json(str(process_config_path))
    retrieval_config = load_json(str(retrieval_config_path))

    steps.append(timed("weknora_health", lambda: ("pass", "ok") if 200 <= http_json(base_url, "health", api_key=api_key, timeout=args.timeout)[0] < 300 else ("fail", "health endpoint failed")))
    if not api_key:
        steps.append(StepResult("api_key", "fail", "WEKNORA_API_KEY is required for acceptance"))
    if any(step.status == "fail" for step in steps):
        return exit_with_reports(args, steps, uploads)

    kb_id, kb_steps = ensure_kb(base_url=base_url, api_key=api_key, kb_config=kb_config, kb_id_file=kb_id_file, timeout=args.timeout)
    steps.extend(kb_steps)
    if not kb_id:
        return exit_with_reports(args, steps, uploads)

    metadata_by_knowledge_id: dict[str, dict[str, Any]] = {}
    for file_path, metadata in sample_records:
        def do_upload(path: Path = file_path, item_metadata: dict[str, Any] = metadata) -> tuple[str, str]:
            result = upload_file(
                base_url=base_url,
                api_key=api_key,
                kb_id=kb_id,
                path=path,
                metadata=item_metadata,
                process_config=process_config,
                channel=args.channel,
            )
            upload_results.append(result)
            detail = f"HTTP {result.http_status}: {result.message[:180]}"
            uploads.append(
                UploadRecord(
                    file_path=result.file_path,
                    relative_path=result.relative_path,
                    doc_id=result.doc_id,
                    knowledge_id=result.knowledge_id,
                    status=result.status,
                    detail=detail,
                )
            )
            if result.status != "success":
                return "fail", f"{path.name}: {detail}"
            if not result.knowledge_id:
                return "fail", f"{path.name}: response missing knowledge id"
            metadata_by_knowledge_id[result.knowledge_id] = item_metadata
            return "pass", f"{path.name}: {result.knowledge_id}"
        upload_step = timed(f"upload_{file_path.stem[:28]}", do_upload)
        steps.append(upload_step)
        if upload_step.status != "pass":
            continue
        knowledge_id = uploads[-1].knowledge_id
        steps.append(timed(f"parse_{file_path.stem[:29]}", lambda kid=knowledge_id: poll_knowledge_completed(
            base_url=base_url,
            api_key=api_key,
            knowledge_id=kid,
            timeout=args.timeout,
            poll_seconds=args.poll_seconds,
            poll_timeout=args.poll_timeout,
        )))
        if steps[-1].status == "pass":
            steps.append(timed(f"chunks_{file_path.stem[:28]}", lambda kid=knowledge_id, meta=metadata_by_knowledge_id[knowledge_id]: chunk_check(
                base_url=base_url,
                api_key=api_key,
                knowledge_id=kid,
                metadata=meta,
                timeout=args.timeout,
            )))

    successful_uploads = [upload for upload in uploads if upload.status == "success" and upload.knowledge_id]
    if successful_uploads:
        first_meta = metadata_by_knowledge_id.get(successful_uploads[0].knowledge_id, {})
        steps.append(timed("hybrid_search_primary", lambda: hybrid_search_check(
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            query_text=search_query_for(first_meta),
            retrieval_config=retrieval_config,
            timeout=args.timeout,
        )))
    else:
        steps.append(StepResult("hybrid_search_primary", "skip", "no successful uploads"))

    if args.skip_chat:
        steps.append(StepResult("knowledge_chat_cross_doc", "skip", "--skip-chat set"))
    elif len(successful_uploads) >= 2:
        query = (
            "请基于已上传的 AlphaMind 二阶段样本文档，对比这些研报中的公司观点、风险因素或盈利预测差异；"
            "必须逐条列出来源引用，没有引用不要编造。"
        )
        steps.append(timed("knowledge_chat_cross_doc", lambda: chat_check(
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            query=query,
            timeout=args.timeout,
        )))
    else:
        steps.append(StepResult("knowledge_chat_cross_doc", "skip", "need at least 2 successful uploads"))

    if args.skip_graph:
        steps.append(StepResult("neo4j_graph_smoke", "skip", "--skip-graph set"))
    else:
        steps.append(timed("neo4j_graph_smoke", lambda: graph_smoke_check(
            neo4j_url=neo4j_url,
            username=neo4j_user,
            password=neo4j_password,
            database=neo4j_database,
            timeout=args.timeout,
        )))

    return exit_with_reports(args, steps, uploads)


if __name__ == "__main__":
    raise SystemExit(main())
