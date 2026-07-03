#!/usr/bin/env python3
"""
Bulk ingest AlphaMind research documents into WeKnora.

Features:
- Recursively scans a local corpus directory.
- Optionally prefers original PDF/Office files over derived text siblings.
- Derives stock-research metadata from common Chinese broker-report filenames.
- Generates stable, collision-resistant doc_id values.
- Writes a JSONL manifest before upload for review/audit.
- Uploads files to WeKnora with metadata and process_config.
- Writes CSV reports for success/failure so failed files can be retried.

This script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
    ".html",
    ".csv",
    ".xls",
    ".xlsx",
    ".json",
)
PHASE1_EXTENSIONS = (".md", ".markdown", ".txt", ".html", ".mhtml")
PHASE1_PROCESS_CONFIG = "alphamind_process_config_phase1_basic.json"

ORIGINAL_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
DERIVED_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm"}

BROKER_SUFFIXES = (
    "证券",
    "国际",
    "期货",
    "基金",
    "研究所",
    "研究院",
    "投顾",
)


@dataclass
class UploadResult:
    file_path: str
    relative_path: str
    doc_id: str
    status: str
    http_status: int | str
    knowledge_id: str
    message: str
    metadata_json: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk upload AlphaMind documents to a WeKnora knowledge base."
    )
    parser.add_argument(
        "--phase",
        choices=("research-report", "phase1-basic"),
        default="research-report",
        help="Use phase-specific safe defaults. phase1-basic only allows Markdown/TXT/HTML/MHTML samples.",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Directory to scan, for example D:\\AlphaMind\\数据",
    )
    parser.add_argument(
        "--kb-id",
        required=True,
        help="Target WeKnora knowledge base ID.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WEKNORA_BASE_URL", "http://localhost:8080"),
        help="WeKnora backend base URL, default: %(default)s",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("WEKNORA_API_KEY"),
        help="WeKnora API key. Defaults to WEKNORA_API_KEY env var.",
    )
    parser.add_argument(
        "--process-config",
        default=str(
            Path(__file__).resolve().parents[1]
            / "dataset"
            / "alphamind_process_config_research_report.json"
        ),
        help="Path to process_config JSON template.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "dataset" / "ingest_runs"),
        help="Directory for CSV reports and default manifests.",
    )
    parser.add_argument(
        "--extensions",
        nargs="*",
        default=list(DEFAULT_EXTENSIONS),
        help="File extensions to include. Default covers PDF/Office/text/table formats.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max files to process after filtering. 0 means unlimited.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="Seconds to sleep between uploads to avoid overwhelming docreader.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and write planned metadata, but do not upload.",
    )
    parser.add_argument(
        "--skip-existing-report",
        help="CSV report from a previous run; files with status=success are skipped.",
    )
    parser.add_argument(
        "--channel",
        default="alphamind-bulk-ingest",
        help="Channel metadata sent to WeKnora.",
    )
    parser.add_argument(
        "--prefer-originals",
        action="store_true",
        help=(
            "When an original PDF/Office file and a derived text/html file share the same stem, "
            "upload only the original. Table files are not treated as derived."
        ),
    )
    parser.add_argument(
        "--doc-id-mode",
        choices=("stem", "stem-ext", "hash"),
        default="stem-ext",
        help=(
            "doc_id strategy. 'stem' is legacy and can collide; 'stem-ext' adds extension and "
            "relative-path hash; 'hash' adds content hash and reads every file. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--hash-files",
        action="store_true",
        help="Compute content SHA-256 for manifest/metadata. Implied by --doc-id-mode hash.",
    )
    parser.add_argument(
        "--manifest",
        help="Write planned upload manifest JSONL to this path. Defaults to out-dir timestamp JSONL.",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="Do not write a JSONL manifest.",
    )
    return parser.parse_args()


def load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def normalized_extensions(values: Iterable[str]) -> set[str]:
    result = set()
    for value in values:
        value = value.strip().lower()
        if not value:
            continue
        if not value.startswith("."):
            value = "." + value
        result.add(value)
    return result


def iter_files(root: Path, extensions: set[str]) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def sibling_key(path: Path, root: Path) -> str:
    """Group extracted text siblings with their original document by relative stem."""
    relative = path.relative_to(root)
    return str(relative.with_suffix("")).replace("\\", "/").casefold()


def apply_prefer_originals(files: list[Path], root: Path) -> tuple[list[Path], dict[str, str]]:
    original_keys = {
        sibling_key(path, root)
        for path in files
        if path.suffix.lower() in ORIGINAL_DOCUMENT_EXTENSIONS
    }
    selected: list[Path] = []
    skipped: dict[str, str] = {}
    for path in files:
        suffix = path.suffix.lower()
        if suffix in DERIVED_TEXT_EXTENSIONS and sibling_key(path, root) in original_keys:
            skipped[str(path.resolve())] = "prefer_originals_skipped_derived_text"
            continue
        selected.append(path)
    return selected, skipped


def read_successful_paths(report_path: str | None) -> set[str]:
    if not report_path:
        return set()
    path = Path(report_path)
    if not path.exists():
        return set()
    successful = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") == "success" and row.get("file_path"):
                successful.add(str(Path(row["file_path"]).resolve()))
    return successful


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_text_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def safe_slug(value: str, max_len: int = 120) -> str:
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", value).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug[:max_len] or "document"


def compact_date_to_iso(value: str) -> str:
    """Convert YYMMDD or YYYYMMDD into ISO date when possible."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 6:
        year = int(digits[:2])
        # AlphaMind corpus is modern broker research. Interpret 80-99 as 1900s, otherwise 2000s.
        full_year = 1900 + year if year >= 80 else 2000 + year
        return f"{full_year:04d}-{digits[2:4]}-{digits[4:6]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def first_iso_date(text: str) -> str:
    match = re.search(r"(20\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def trailing_report_date(stem: str) -> str:
    # Common pattern: ...-241116_1 or ...-250806
    matches = re.findall(r"(?:^|[-_])([12]\d{5})(?:_\d+)?$", stem)
    if matches:
        return compact_date_to_iso(matches[-1])
    return ""


def normalize_ticker(raw: str) -> str:
    ticker = raw.strip().upper()
    if re.fullmatch(r"\d{5}\.HK", ticker):
        return ticker
    if re.fullmatch(r"\d{4}\.HK", ticker):
        return ticker.zfill(8)  # 0189.HK -> 00189.HK
    if re.fullmatch(r"\d{6}\.SH|\d{6}\.SZ|\d{6}\.BJ", ticker):
        return ticker
    if re.fullmatch(r"\d{6}", ticker):
        if ticker.startswith(("60", "68", "90")):
            return ticker + ".SH"
        if ticker.startswith(("00", "30", "20")):
            return ticker + ".SZ"
        if ticker.startswith(("43", "83", "87")):
            return ticker + ".BJ"
    return ticker


def exchange_from_ticker(ticker: str) -> str:
    if ticker.endswith(".SH"):
        return "SH"
    if ticker.endswith(".SZ"):
        return "SZ"
    if ticker.endswith(".BJ"):
        return "BJ"
    if ticker.endswith(".HK"):
        return "HK"
    return ""


def extract_ticker(stem: str) -> tuple[str, str]:
    # Handles ASCII and Chinese parentheses: 人福医药(600079), 东岳集团(0189.HK), 福赛科技(301529.SZ)
    match = re.search(
        r"(?P<company>[一-鿿A-Za-z0-9&·]+)[（(](?P<ticker>[0-9]{4,6}(?:\.[A-Z]{2,4})?|[0-9]{5}\.HK)[）)]",
        stem,
        flags=re.IGNORECASE,
    )
    if not match:
        return "", ""
    return match.group("company"), normalize_ticker(match.group("ticker"))


def infer_broker(stem: str) -> str:
    # reading-server-2025-...-report-国金证券-人福医药(...)
    match = re.search(r"report[-_](?P<broker>[^-_]+?)(?:[-_])", stem, re.IGNORECASE)
    if match:
        return match.group("broker")

    # 2025-09-23-长江证券-福赛科技(...)
    match = re.search(r"^20\d{2}[-_.]\d{1,2}[-_.]\d{1,2}[-_](?P<broker>[^-_]+?)(?:[-_])", stem)
    if match:
        return match.group("broker")

    # 2023-08-25_国盛证券_昊华科技...
    match = re.search(r"^20\d{2}[-_.]\d{1,2}[-_.]\d{1,2}[_-](?P<broker>[^_-]+?)[_-]", stem)
    if match:
        return match.group("broker")

    for token in re.split(r"[-_\s]+", stem):
        if any(token.endswith(suffix) for suffix in BROKER_SUFFIXES):
            return token
    return ""


def infer_report_type(stem: str) -> str:
    if "深度" in stem:
        return "深度报告"
    if "首次覆盖" in stem:
        return "首次覆盖"
    if "半年报" in stem or "年报" in stem or "季报" in stem:
        return "业绩点评"
    if "行业" in stem or "专题" in stem or "策略" in stem:
        return "行业研究"
    if "周报" in stem:
        return "周报"
    return "研报"


def infer_source_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".xls", ".xlsx", ".json"}:
        return "financial_or_table_data"
    if "公告" in path.name:
        return "announcement"
    if "年报" in path.name or "季报" in path.name or "半年报" in path.name:
        return "financial_report"
    return "broker_report"


def make_doc_id(path: Path, root: Path, mode: str, content_hash: str = "") -> str:
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    stem_slug = safe_slug(path.stem)
    suffix_slug = path.suffix.lower().lstrip(".") or "file"

    if mode == "stem":
        return f"alphamind_{stem_slug}"
    if mode == "hash":
        if not content_hash:
            content_hash = file_sha256(path)
        return f"alphamind_{stem_slug}_{suffix_slug}_{content_hash[:16]}"

    # Default: stable for a given corpus path and collision-resistant for PDF/TXT siblings.
    rel_hash = short_text_hash(relative_path)
    return f"alphamind_{stem_slug}_{suffix_slug}_{rel_hash}"


def build_metadata(
    path: Path,
    root: Path,
    *,
    doc_id_mode: str,
    channel: str,
    content_hash: str = "",
) -> dict[str, Any]:
    stem = path.stem
    company, ticker = extract_ticker(stem)
    broker = infer_broker(stem)
    report_date = trailing_report_date(stem) or first_iso_date(stem)
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    doc_id = make_doc_id(path, root, doc_id_mode, content_hash)

    metadata: dict[str, Any] = {
        "doc_id": doc_id,
        "source_type": infer_source_type(path),
        "title": stem,
        "company": company,
        "ticker": ticker,
        "exchange": exchange_from_ticker(ticker),
        "industry": "",
        "broker": broker,
        "analyst": [],
        "publish_date": report_date,
        "report_type": infer_report_type(stem),
        "rating": "",
        "target_price": None,
        "language": "zh-CN",
        "file_path": str(path),
        "relative_path": relative_path,
        "file_suffix": path.suffix.lower(),
        "file_size_bytes": path.stat().st_size,
        "content_sha256": content_hash,
        "ingest_channel": channel,
    }
    return metadata


def metadata_for_api(metadata: dict[str, Any]) -> dict[str, str]:
    """WeKnora's upload API expects metadata as map[string]string."""
    result: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, str):
            result[key] = value
        elif isinstance(value, (list, dict)):
            result[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            result[key] = str(value)
    return result


def encode_multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----AlphaMindBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    filename = file_path.name
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))

    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def extract_knowledge_id(response_body: str) -> str:
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("id", "knowledge_id", "knowledgeId"):
                if key in value and isinstance(value[key], str):
                    return value[key]
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(payload)


def upload_file(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    path: Path,
    metadata: dict[str, Any],
    process_config: dict[str, Any],
    channel: str,
) -> UploadResult:
    relative_path = metadata["relative_path"]
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    api_metadata_json = json.dumps(metadata_for_api(metadata), ensure_ascii=False, separators=(",", ":"))
    process_config_json = json.dumps(process_config, ensure_ascii=False, separators=(",", ":"))

    endpoint = urljoin(base_url.rstrip("/") + "/", f"api/v1/knowledge-bases/{kb_id}/knowledge/file")
    body, content_type = encode_multipart(
        {
            "fileName": relative_path,
            "metadata": api_metadata_json,
            "process_config": process_config_json,
            "channel": channel,
        },
        "file",
        path,
    )

    request = Request(endpoint, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    request.add_header("X-API-Key", api_key)

    try:
        with urlopen(request, timeout=300) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            return UploadResult(
                file_path=str(path),
                relative_path=relative_path,
                doc_id=metadata["doc_id"],
                status="success" if 200 <= int(status) < 300 else "error",
                http_status=status,
                knowledge_id=extract_knowledge_id(response_body),
                message=response_body[:1000],
                metadata_json=metadata_json,
            )
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return UploadResult(
            file_path=str(path),
            relative_path=relative_path,
            doc_id=metadata["doc_id"],
            status="error",
            http_status=exc.code,
            knowledge_id="",
            message=response_body[:1000],
            metadata_json=metadata_json,
        )
    except URLError as exc:
        return UploadResult(
            file_path=str(path),
            relative_path=relative_path,
            doc_id=metadata["doc_id"],
            status="error",
            http_status="url_error",
            knowledge_id="",
            message=str(exc.reason),
            metadata_json=metadata_json,
        )
    except Exception as exc:  # Keep batch going and report the file-level failure.
        return UploadResult(
            file_path=str(path),
            relative_path=relative_path,
            doc_id=metadata["doc_id"],
            status="error",
            http_status="exception",
            knowledge_id="",
            message=f"{type(exc).__name__}: {exc}",
            metadata_json=metadata_json,
        )


def write_report(path: Path, results: list[UploadResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file_path",
                "relative_path",
                "doc_id",
                "status",
                "http_status",
                "knowledge_id",
                "message",
                "metadata_json",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def write_manifest(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def build_metadata_records(
    files: list[Path],
    root: Path,
    *,
    doc_id_mode: str,
    channel: str,
    hash_files: bool,
) -> list[tuple[Path, dict[str, Any]]]:
    records = []
    for path in files:
        content_hash = file_sha256(path) if hash_files or doc_id_mode == "hash" else ""
        metadata = build_metadata(
            path,
            root,
            doc_id_mode=doc_id_mode,
            channel=channel,
            content_hash=content_hash,
        )
        records.append((path, metadata))
    return records


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Root directory does not exist: {root}", file=sys.stderr)
        return 2

    if args.phase == "phase1-basic":
        default_research_process_config = str(PROJECT_ROOT / "dataset" / "alphamind_process_config_research_report.json")
        if str(Path(args.process_config)) == default_research_process_config:
            args.process_config = str(PROJECT_ROOT / "dataset" / PHASE1_PROCESS_CONFIG)
        if tuple(args.extensions) == DEFAULT_EXTENSIONS:
            args.extensions = list(PHASE1_EXTENSIONS)
        args.prefer_originals = False
        if args.channel == "alphamind-bulk-ingest":
            args.channel = "alphamind-phase1-basic-ingest"

    if not args.dry_run and not args.api_key:
        print("Missing --api-key or WEKNORA_API_KEY for non-dry-run upload.", file=sys.stderr)
        return 2

    process_config = load_json(args.process_config)
    extensions = normalized_extensions(args.extensions)
    if args.phase == "phase1-basic":
        disallowed = sorted(extension for extension in extensions if extension not in set(PHASE1_EXTENSIONS))
        if disallowed:
            print(
                "phase1-basic only allows Markdown/TXT/HTML/MHTML extensions; disallowed: "
                + ", ".join(disallowed),
                file=sys.stderr,
            )
            return 2
    files = iter_files(root, extensions)

    skipped_reasons: dict[str, str] = {}
    skipped_successes = read_successful_paths(args.skip_existing_report)
    if skipped_successes:
        before = len(files)
        files = [path for path in files if str(path.resolve()) not in skipped_successes]
        for skipped in skipped_successes:
            skipped_reasons[skipped] = "skip_existing_success"
        print(f"Skipped existing successes: {before - len(files)}")

    if args.prefer_originals:
        files, prefer_skipped = apply_prefer_originals(files, root)
        skipped_reasons.update(prefer_skipped)
        print(f"Skipped derived text siblings because --prefer-originals is set: {len(prefer_skipped)}")

    if args.limit > 0:
        files = files[: args.limit]

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir)
    report_path = out_dir / f"alphamind_ingest_{timestamp}.csv"
    manifest_path = None
    if not args.no_manifest:
        manifest_path = Path(args.manifest) if args.manifest else out_dir / f"alphamind_manifest_{timestamp}.jsonl"

    print(f"Root: {root}")
    print(f"Matched files after filtering: {len(files)}")
    print(f"Process config: {args.process_config}")
    print(f"Report: {report_path}")
    if manifest_path:
        print(f"Manifest: {manifest_path}")

    metadata_records = build_metadata_records(
        files,
        root,
        doc_id_mode=args.doc_id_mode,
        channel=args.channel,
        hash_files=args.hash_files,
    )

    manifest_records = [
        {
            "action": "upload" if not args.dry_run else "dry_run",
            "kb_id": args.kb_id,
            "base_url": args.base_url,
            "process_config_path": str(Path(args.process_config).resolve()),
            "metadata": metadata,
        }
        for _, metadata in metadata_records
    ]
    if manifest_path:
        write_manifest(manifest_path, manifest_records)

    results: list[UploadResult] = []
    for index, (path, metadata) in enumerate(metadata_records, start=1):
        relative_path = metadata["relative_path"]
        print(f"[{index}/{len(metadata_records)}] {relative_path}")

        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if args.dry_run:
            results.append(
                UploadResult(
                    file_path=str(path),
                    relative_path=relative_path,
                    doc_id=metadata["doc_id"],
                    status="dry_run",
                    http_status="dry_run",
                    knowledge_id="",
                    message="planned only",
                    metadata_json=metadata_json,
                )
            )
            continue

        result = upload_file(
            base_url=args.base_url,
            api_key=args.api_key,
            kb_id=args.kb_id,
            path=path,
            metadata=metadata,
            process_config=process_config,
            channel=args.channel,
        )
        results.append(result)
        print(f"    -> {result.status} ({result.http_status}) {result.knowledge_id}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    write_report(report_path, results)

    success_count = sum(1 for result in results if result.status == "success")
    dry_run_count = sum(1 for result in results if result.status == "dry_run")
    error_count = sum(1 for result in results if result.status == "error")
    print(f"Done. success={success_count}, dry_run={dry_run_count}, error={error_count}, skipped={len(skipped_reasons)}")
    print(f"CSV report: {report_path}")
    if manifest_path:
        print(f"JSONL manifest: {manifest_path}")

    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
