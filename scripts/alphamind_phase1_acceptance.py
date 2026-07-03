#!/usr/bin/env python3
"""
AlphaMind phase1-basic end-to-end acceptance.

Validates the quotation's phase-1 scope only:
- WeKnora app health
- phase1 knowledge base creation/reuse
- Markdown/TXT/MHTML upload with graph/question generation disabled
- parse completion
- text chunks
- basic hybrid search
- basic knowledge chat with references

It intentionally ignores existing PDF/GraphRAG KBs and does not upload PDFs.
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase1-basic"
DEFAULT_KB_CONFIG = PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase1-basic.json"
DEFAULT_PROCESS_CONFIG = PROJECT_ROOT / "dataset" / "alphamind_process_config_phase1_basic.json"
DEFAULT_KB_ID_FILE = PROJECT_ROOT / "dataset" / "alphamind_phase1_basic_kb_id.txt"
DEFAULT_SAMPLE_DIR = PROJECT_ROOT / "dataset" / "phase1_samples"
DEFAULT_RUN_DIR = PROJECT_ROOT / "dataset" / "phase1_runs"
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".mhtml"}
EXPECTED_FACTS = {
    "sample_facts.md": "PHASE1-RAG-BASIC-2026",
    "sample_policy.txt": "BASIC-TXT-CHECKPOINT-7319",
    "sample_web_archive.mhtml": "MHTML-ARCHIVE-CHECK-9527",
}


@dataclass
class StepResult:
    name: str
    status: str  # pass / fail / warn / skip
    detail: str
    elapsed_ms: int = 0


@dataclass
class UploadRecord:
    file_path: str
    knowledge_id: str
    status: str
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind phase1-basic acceptance against WeKnora.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--base-url", default="", help="Override WEKNORA_BASE_URL, default from env file.")
    parser.add_argument("--api-key", default="", help="Override WEKNORA_API_KEY, default from env file.")
    parser.add_argument("--kb-config", default=str(DEFAULT_KB_CONFIG))
    parser.add_argument("--process-config", default=str(DEFAULT_PROCESS_CONFIG))
    parser.add_argument("--kb-id-file", default=str(DEFAULT_KB_ID_FILE))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--skip-chat", action="store_true", help="Skip knowledge-chat step; search/chunk checks still run.")
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


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


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
            return status, events, "\n".join(raw_parts[-50:])
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
        for key in ("items", "list", "data", "records", "knowledges"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def encode_multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----AlphaMindPhase1{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode("utf-8"))
    chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_file(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    file_path: Path,
    process_config: dict[str, Any],
    timeout: float,
) -> tuple[int, Any]:
    metadata = {
        "doc_id": f"phase1_{file_path.stem}_{uuid.uuid4().hex[:8]}",
        "title": file_path.stem,
        "source_type": "phase1_basic_sample",
        "relative_path": file_path.name,
        "file_suffix": file_path.suffix.lower(),
        "language": "zh-CN",
        "ingest_channel": "alphamind-phase1-acceptance",
    }
    fields = {
        "fileName": file_path.name,
        "metadata": json.dumps({k: str(v) for k, v in metadata.items()}, ensure_ascii=False, separators=(",", ":")),
        "process_config": json.dumps(process_config, ensure_ascii=False, separators=(",", ":")),
        "channel": "alphamind-phase1-acceptance",
    }
    body, content_type = encode_multipart(fields, "file", file_path)
    url = urljoin(base_url.rstrip("/") + "/", f"api/v1/knowledge-bases/{kb_id}/knowledge/file")
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urlopen(request, timeout=max(timeout, 60.0)) as response:
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


def sample_files(sample_dir: Path) -> list[Path]:
    files = sorted(path for path in sample_dir.iterdir() if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES)
    return [path for path in files if path.name in EXPECTED_FACTS]


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
        return "warn", "no existing phase1 KB found"
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
                    return "fail", "parse_status=failed"
                last_payload = f"parse_status={parse_status}"
        time.sleep(poll_seconds)
    return "fail", f"timeout waiting for completed; last_status={last_status}; {last_payload}"


def chunk_check(
    *,
    base_url: str,
    api_key: str,
    knowledge_id: str,
    expected_fact: str,
    timeout: float,
) -> tuple[str, str]:
    status, payload = http_json(
        base_url,
        f"api/v1/chunks/{knowledge_id}",
        api_key=api_key,
        query={"page": 1, "page_size": 20},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {str(payload)[:200]}"
    chunks = extract_items(payload)
    if not chunks:
        return "fail", "no chunks returned"
    contents = "\n".join(str(chunk.get("content", "")) for chunk in chunks)
    if expected_fact not in contents:
        return "warn", f"{len(chunks)} chunks returned but expected fact not in first page"
    return "pass", f"{len(chunks)} chunks returned; expected fact found"


def hybrid_search_check(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    expected_fact: str,
    timeout: float,
) -> tuple[str, str]:
    status, payload = http_json(
        base_url,
        f"api/v1/knowledge-bases/{kb_id}/hybrid-search",
        api_key=api_key,
        method="POST",
        payload={"query_text": expected_fact, "match_count": 5, "vector_threshold": 0.0, "keyword_threshold": 0.0},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {str(payload)[:240]}"
    results = extract_items(payload)
    if not results:
        return "fail", "no search results"
    top_content = str(results[0].get("content", ""))
    if expected_fact not in "\n".join(str(item.get("content", "")) for item in results):
        return "warn", f"{len(results)} results, expected fact not found in returned contents; top={top_content[:100]!r}"
    return "pass", f"{len(results)} results; expected fact found"


def create_session(base_url: str, api_key: str, timeout: float) -> str:
    status, payload = http_json(
        base_url,
        "api/v1/sessions",
        api_key=api_key,
        method="POST",
        payload={"title": "AlphaMind phase1 acceptance", "description": "phase1-basic automated acceptance"},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"create session HTTP {status}: {str(payload)[:200]}")
    session_id = extract_id(payload)
    if not session_id:
        raise RuntimeError("create session response did not include session id")
    return session_id


def chat_check(*, base_url: str, api_key: str, kb_id: str, expected_fact: str, timeout: float) -> tuple[str, str]:
    session_id = create_session(base_url, api_key, timeout)
    status, events, raw = http_sse(
        base_url,
        f"api/v1/knowledge-chat/{session_id}",
        api_key=api_key,
        payload={
            "query": f"请回答第一阶段验收口令：{expected_fact}",
            "knowledge_base_ids": [kb_id],
            "disable_title": True,
            "enable_memory": False,
            "channel": "api",
        },
        timeout=max(timeout, 90.0),
    )
    if status < 200 or status >= 300:
        return "fail", f"HTTP {status}: {raw[:240]}"
    answer = "".join(str(event.get("content", "")) for event in events if event.get("response_type") == "answer")
    references = [event for event in events if event.get("response_type") == "references" or event.get("knowledge_references")]
    if not events:
        return "fail", f"no SSE events parsed; raw={raw[:240]}"
    if not references:
        return "warn", f"chat returned {len(events)} events but no references; answer_preview={answer[:120]!r}"
    if expected_fact not in answer and expected_fact not in raw:
        return "warn", f"references returned but expected fact not visible in answer/raw; answer_preview={answer[:120]!r}"
    return "pass", f"events={len(events)} references={len(references)} answer_preview={answer[:120]!r}"


def write_reports(out_dir: Path, steps: list[StepResult], uploads: list[UploadRecord]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"phase1_acceptance_{stamp}.json"
    csv_path = out_dir / f"phase1_uploads_{stamp}.csv"
    json_path.write_text(
        json.dumps({"steps": [asdict(step) for step in steps], "uploads": [asdict(upload) for upload in uploads]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file_path", "knowledge_id", "status", "detail"])
        writer.writeheader()
        for upload in uploads:
            writer.writerow(asdict(upload))
    return json_path, csv_path


def print_summary(steps: list[StepResult], uploads: list[UploadRecord], json_path: Path, csv_path: Path) -> None:
    print("\nAlphaMind phase1-basic acceptance")
    print("=" * 100)
    for step in steps:
        print(f"{step.status.upper():<5} {step.name:<34} {step.elapsed_ms:>6}ms  {step.detail}")
    if uploads:
        print("\nUploads")
        for upload in uploads:
            print(f"{upload.status.upper():<5} {Path(upload.file_path).name:<34} {upload.knowledge_id:<36} {upload.detail}")
    print("=" * 100)
    print(f"Report JSON: {json_path}")
    print(f"Upload CSV:  {csv_path}")
    failures = [step for step in steps if step.status == "fail"]
    warnings = [step for step in steps if step.status == "warn"]
    print(f"failures={len(failures)}, warnings={len(warnings)}, steps={len(steps)}, uploads={len(uploads)}")


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    env = parse_env_file(env_file)
    base_url = (args.base_url or env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")).rstrip("/")
    api_key = args.api_key or env_value(env, "WEKNORA_API_KEY")
    kb_config_path = Path(args.kb_config)
    process_config_path = Path(args.process_config)
    kb_id_file = Path(args.kb_id_file)
    sample_dir = Path(args.sample_dir)
    out_dir = Path(args.out_dir)

    steps: list[StepResult] = []
    uploads: list[UploadRecord] = []

    def check_files() -> tuple[str, str]:
        missing = [str(path) for path in (env_file, kb_config_path, process_config_path, sample_dir) if not path.exists()]
        if missing:
            return "fail", "missing: " + "; ".join(missing)
        files = sample_files(sample_dir)
        if len(files) < 3:
            return "fail", f"expected 3 phase1 sample files, found {len(files)}"
        bad = [str(path) for path in files if path.suffix.lower() not in ALLOWED_SUFFIXES]
        if bad:
            return "fail", "disallowed sample suffix: " + "; ".join(bad)
        return "pass", f"sample_files={len(files)}"
    steps.append(timed("local_files", check_files))
    if steps[-1].status == "fail":
        json_path, csv_path = write_reports(out_dir, steps, uploads)
        if args.json:
            print(json.dumps({"steps": [asdict(step) for step in steps], "uploads": []}, ensure_ascii=False, indent=2))
        else:
            print_summary(steps, uploads, json_path, csv_path)
        return 1

    kb_config = load_json(kb_config_path)
    process_config = load_json(process_config_path)

    steps.append(timed("weknora_health", lambda: ("pass", "ok") if 200 <= http_json(base_url, "health", api_key=api_key, timeout=args.timeout)[0] < 300 else ("fail", "health endpoint failed")))
    if not api_key:
        steps.append(StepResult("api_key", "fail", "WEKNORA_API_KEY is required for acceptance"))
    if any(step.status == "fail" for step in steps):
        json_path, csv_path = write_reports(out_dir, steps, uploads)
        if args.json:
            print(json.dumps({"steps": [asdict(step) for step in steps], "uploads": []}, ensure_ascii=False, indent=2))
        else:
            print_summary(steps, uploads, json_path, csv_path)
        return 1

    kb_id, kb_steps = ensure_kb(base_url=base_url, api_key=api_key, kb_config=kb_config, kb_id_file=kb_id_file, timeout=args.timeout)
    steps.extend(kb_steps)
    if not kb_id:
        json_path, csv_path = write_reports(out_dir, steps, uploads)
        if args.json:
            print(json.dumps({"steps": [asdict(step) for step in steps], "uploads": []}, ensure_ascii=False, indent=2))
        else:
            print_summary(steps, uploads, json_path, csv_path)
        return 1

    for file_path in sample_files(sample_dir):
        expected_fact = EXPECTED_FACTS[file_path.name]
        def do_upload(path: Path = file_path) -> tuple[str, str]:
            status, payload = upload_file(
                base_url=base_url,
                api_key=api_key,
                kb_id=kb_id,
                file_path=path,
                process_config=process_config,
                timeout=args.timeout,
            )
            if status < 200 or status >= 300:
                uploads.append(UploadRecord(str(path), "", "error", f"HTTP {status}: {str(payload)[:180]}"))
                return "fail", f"{path.name}: HTTP {status}: {str(payload)[:200]}"
            knowledge_id = extract_id(payload)
            if not knowledge_id:
                uploads.append(UploadRecord(str(path), "", "error", "response missing knowledge id"))
                return "fail", f"{path.name}: response missing knowledge id"
            uploads.append(UploadRecord(str(path), knowledge_id, "success", "uploaded"))
            return "pass", f"{path.name}: {knowledge_id}"
        upload_step = timed(f"upload_{file_path.stem}", do_upload)
        steps.append(upload_step)
        if upload_step.status != "pass":
            continue
        knowledge_id = uploads[-1].knowledge_id
        steps.append(timed(f"parse_{file_path.stem}", lambda kid=knowledge_id: poll_knowledge_completed(
            base_url=base_url,
            api_key=api_key,
            knowledge_id=kid,
            timeout=args.timeout,
            poll_seconds=args.poll_seconds,
            poll_timeout=args.poll_timeout,
        )))
        if steps[-1].status != "pass":
            continue
        steps.append(timed(f"chunks_{file_path.stem}", lambda kid=knowledge_id, fact=expected_fact: chunk_check(
            base_url=base_url,
            api_key=api_key,
            knowledge_id=kid,
            expected_fact=fact,
            timeout=args.timeout,
        )))
        steps.append(timed(f"search_{file_path.stem}", lambda fact=expected_fact: hybrid_search_check(
            base_url=base_url,
            api_key=api_key,
            kb_id=kb_id,
            expected_fact=fact,
            timeout=args.timeout,
        )))

    if not args.skip_chat:
        first_fact = EXPECTED_FACTS["sample_facts.md"]
        steps.append(timed("knowledge_chat", lambda: chat_check(base_url=base_url, api_key=api_key, kb_id=kb_id, expected_fact=first_fact, timeout=args.timeout)))
    else:
        steps.append(StepResult("knowledge_chat", "skip", "--skip-chat provided"))

    json_path, csv_path = write_reports(out_dir, steps, uploads)
    if args.json:
        print(json.dumps({"steps": [asdict(step) for step in steps], "uploads": [asdict(upload) for upload in uploads], "report_json": str(json_path), "upload_csv": str(csv_path)}, ensure_ascii=False, indent=2))
    else:
        print_summary(steps, uploads, json_path, csv_path)

    return 1 if any(step.status == "fail" for step in steps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
