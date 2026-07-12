#!/usr/bin/env python3
"""
AlphaMind pre-ingestion health check for WeKnora.

Checks local config files and service endpoints before running bulk ingestion:
- WeKnora app /health
- authenticated WeKnora system APIs when WEKNORA_API_KEY or --api-key is provided
- Qdrant REST
- Neo4j HTTP transactional endpoint when credentials are available
- MinIO health endpoint
- Langfuse HTTP health/root endpoint

The script never prints secret values. It only reports whether sensitive variables are present.
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
PHASE1_ENV_FILE = PROJECT_ROOT / ".env.phase1-basic"
PHASE2_ENV_FILE = PROJECT_ROOT / ".env.phase2-advanced"
PHASE3_ENV_FILE = PROJECT_ROOT / ".env.phase3-precision"


@dataclass
class CheckResult:
    name: str
    status: str  # pass / fail / warn / skip
    required: bool
    detail: str
    elapsed_ms: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check AlphaMind/WeKnora services before ingestion.")
    parser.add_argument("--phase", choices=("alphamind", "phase1-basic", "phase2-advanced", "phase3-precision"), default="alphamind", help="Select dependency profile.")
    parser.add_argument("--base-url", default="", help="Override WEKNORA_BASE_URL from env file.")
    parser.add_argument("--api-key", default=os.environ.get("WEKNORA_API_KEY"))
    parser.add_argument("--qdrant-url", default="", help="Override QDRANT_REST_URL / QDRANT_REST_PORT from env file.")
    parser.add_argument("--app-container", default="", help="Override WEKNORA_APP_CONTAINER from env file.")
    parser.add_argument("--qdrant-internal-url", default="", help="Override QDRANT_INTERNAL_URL from env file.")
    parser.add_argument("--neo4j-url", default="", help="Override NEO4J_HTTP_URL / NEO4J_HTTP_PORT from env file.")
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USERNAME"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--minio-url", default="", help="Override MINIO_PUBLIC_URL / MINIO_PORT from env file.")
    parser.add_argument("--langfuse-url", default="", help="Override LANGFUSE_PUBLIC_URL / LANGFUSE_PORT from env file.")
    parser.add_argument("--env-file", default="", help="Optional .env file for non-secret readiness checks.")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--require-qdrant", action="store_true", help="Fail if Qdrant is unreachable.")
    parser.add_argument("--require-neo4j", action="store_true", help="Fail if Neo4j is unreachable.")
    parser.add_argument("--require-minio", action="store_true", help="Fail if MinIO is unreachable.")
    parser.add_argument("--require-langfuse", action="store_true", help="Fail if Langfuse is unreachable.")
    parser.add_argument("--check-models", action="store_true", help="Run phase1 local model API checks as part of healthcheck.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a table.")
    return parser.parse_args()


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def env_get(env_file_values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env_file_values.get(key) or default


def localhost_url_from_port(env_file_values: dict[str, str], port_key: str, default_port: str) -> str:
    return f"http://localhost:{env_get(env_file_values, port_key, default_port)}"


def bool_env(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def timed(name: str, required: bool, func) -> CheckResult:
    start = time.perf_counter()
    try:
        result = func()
        if isinstance(result, CheckResult):
            result.elapsed_ms = int((time.perf_counter() - start) * 1000)
            return result
        status, detail = result
        return CheckResult(name, status, required, detail, int((time.perf_counter() - start) * 1000))
    except Exception as exc:
        return CheckResult(name, "fail" if required else "warn", required, f"{type(exc).__name__}: {exc}", int((time.perf_counter() - start) * 1000))


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, str]:
    request = Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def json_http_check(
    name: str,
    url: str,
    *,
    required: bool,
    timeout: float,
    headers: dict[str, str] | None = None,
    expect_success_field: bool = False,
    detail_keys: list[str] | None = None,
) -> CheckResult:
    def run() -> tuple[str, str]:
        status, body = http_request(url, headers=headers, timeout=timeout)
        if status < 200 or status >= 300:
            return ("fail" if required else "warn", f"HTTP {status}: {body[:200]}")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return "pass", f"HTTP {status} non-JSON response"
        if expect_success_field and payload.get("success") is not True and payload.get("code") != 0:
            return ("fail" if required else "warn", f"success/code field not OK: {body[:200]}")
        if detail_keys and isinstance(payload.get("data"), dict):
            data = payload["data"]
            details = []
            for key in detail_keys:
                if key in data:
                    details.append(f"{key}={data[key]}")
            if details:
                return "pass", "; ".join(details)
        if "status" in payload:
            return "pass", f"status={payload['status']}"
        if "success" in payload:
            return "pass", f"success={payload['success']}"
        return "pass", f"HTTP {status} JSON OK"

    return timed(name, required, run)


def file_check(path: Path, name: str, required: bool = True) -> CheckResult:
    def run() -> tuple[str, str]:
        if path.exists():
            return "pass", str(path)
        return ("fail" if required else "warn", f"missing: {path}")

    return timed(name, required, run)


def api_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def check_auth_api(name: str, url: str, *, api_key: str | None, timeout: float, detail_keys: list[str] | None = None) -> CheckResult:
    if not api_key:
        return CheckResult(name, "skip", False, "WEKNORA_API_KEY/--api-key not provided; authenticated API check skipped")
    return json_http_check(
        name,
        url,
        required=False,
        timeout=timeout,
        headers=api_headers(api_key),
        expect_success_field=True,
        detail_keys=detail_keys,
    )


def check_qdrant(url: str, *, required: bool, timeout: float) -> CheckResult:
    return json_http_check(
        "qdrant_collections",
        urljoin(url.rstrip("/") + "/", "collections"),
        required=required,
        timeout=timeout,
    )


def check_qdrant_internal(app_container: str, internal_url: str, *, required: bool, timeout: float) -> CheckResult:
    def run() -> tuple[str, str]:
        command = [
            "docker",
            "exec",
            app_container,
            "curl",
            "-sf",
            urljoin(internal_url.rstrip("/") + "/", "collections"),
        ]
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout + 5,
        )
        if completed.returncode == 0:
            return "pass", "app-container internal collections OK"
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}")[:300]
        return ("fail" if required else "warn", detail)

    return timed("qdrant_internal", required, run)


def check_minio(url: str, *, required: bool, timeout: float) -> CheckResult:
    def run() -> tuple[str, str]:
        status, body = http_request(urljoin(url.rstrip("/") + "/", "minio/health/live"), timeout=timeout)
        if 200 <= status < 300:
            return "pass", "live"
        return ("fail" if required else "warn", f"HTTP {status}: {body[:200]}")

    return timed("minio_live", required, run)


def check_langfuse(url: str, *, required: bool, timeout: float) -> CheckResult:
    def run() -> tuple[str, str]:
        health_url = urljoin(url.rstrip("/") + "/", "api/public/health")
        status, body = http_request(health_url, timeout=timeout)
        if 200 <= status < 300:
            return "pass", f"health HTTP {status}"
        root_status, root_body = http_request(url.rstrip("/") + "/", timeout=timeout)
        if 200 <= root_status < 400:
            return "pass", f"root HTTP {root_status}; health HTTP {status}"
        return ("fail" if required else "warn", f"health HTTP {status}; root HTTP {root_status}: {(body or root_body)[:200]}")

    return timed("langfuse_http", required, run)


def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def check_neo4j(
    url: str,
    *,
    username: str | None,
    password: str | None,
    database: str,
    required: bool,
    timeout: float,
) -> CheckResult:
    def run() -> tuple[str, str]:
        if username and password:
            endpoint = urljoin(url.rstrip("/") + "/", f"db/{database}/tx/commit")
            body = json.dumps({"statements": [{"statement": "RETURN 1 AS ok"}]}).encode("utf-8")
            status, response_body = http_request(
                endpoint,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": basic_auth_header(username, password),
                },
                body=body,
                timeout=timeout,
            )
            if 200 <= status < 300:
                try:
                    payload = json.loads(response_body)
                    errors = payload.get("errors") or []
                    if errors:
                        return ("fail" if required else "warn", f"Neo4j errors: {errors[:1]}")
                except json.JSONDecodeError:
                    pass
                return "pass", "transaction endpoint OK"
            return ("fail" if required else "warn", f"HTTP {status}: {response_body[:200]}")

        status, body = http_request(url.rstrip("/") + "/", timeout=timeout)
        if 200 <= status < 400 or status == 401:
            return "pass", f"HTTP endpoint reachable ({status}); credentials not provided for query"
        return ("fail" if required else "warn", f"HTTP {status}: {body[:200]}")

    return timed("neo4j_http", required, run)


def env_presence_check(env_values: dict[str, str], key: str, required: bool = False) -> CheckResult:
    def run() -> tuple[str, str]:
        value = env_get(env_values, key)
        if value:
            return "pass", f"{key}=present"
        return ("fail" if required else "warn", f"{key}=missing")

    return timed(f"env_{key}", required, run)


def check_phase1_models(env_file: Path, *, timeout: float, required: bool) -> CheckResult:
    def run() -> tuple[str, str]:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "alphamind_phase1_model_check.py"),
            "--env-file",
            str(env_file),
            "--timeout",
            str(timeout),
            "--json",
        ]
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(30, int(timeout * 4)),
        )
        if completed.returncode == 0:
            return "pass", "local model APIs OK"
        detail = (completed.stdout or completed.stderr or f"exit {completed.returncode}")[:800]
        return ("fail" if required else "warn", detail)

    return timed("phase1_model_apis", required, run)


def print_table(results: list[CheckResult]) -> None:
    status_icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}
    print("\nAlphaMind health check")
    print("=" * 80)
    for result in results:
        required = "required" if result.required else "optional"
        print(f"{status_icon.get(result.status, result.status):<5} {result.name:<32} {required:<8} {result.elapsed_ms:>5}ms  {result.detail}")
    print("=" * 80)
    failures = [r for r in results if r.required and r.status == "fail"]
    warnings = [r for r in results if r.status == "warn"]
    print(f"required failures={len(failures)}, warnings={len(warnings)}, total={len(results)}")


def main() -> int:
    args = parse_args()
    if args.env_file:
        env_file = Path(args.env_file)
    elif args.phase == "phase1-basic":
        env_file = PHASE1_ENV_FILE
    elif args.phase == "phase2-advanced":
        env_file = PHASE2_ENV_FILE
    elif args.phase == "phase3-precision":
        env_file = PHASE3_ENV_FILE
    else:
        env_file = DEFAULT_ENV_FILE
    env_values = parse_env_file(env_file)

    api_key = args.api_key or env_get(env_values, "WEKNORA_API_KEY")
    base_url = args.base_url or env_get(env_values, "WEKNORA_BASE_URL", "http://localhost:8080")
    qdrant_url = args.qdrant_url or env_get(env_values, "QDRANT_REST_URL") or localhost_url_from_port(env_values, "QDRANT_REST_PORT", "6333")
    app_container = args.app_container or env_get(env_values, "WEKNORA_APP_CONTAINER", "WeKnora-app")
    qdrant_internal_url = args.qdrant_internal_url or env_get(env_values, "QDRANT_INTERNAL_URL") or f"http://{env_get(env_values, 'QDRANT_HOST', 'qdrant')}:6333"
    neo4j_url = args.neo4j_url or env_get(env_values, "NEO4J_HTTP_URL") or localhost_url_from_port(env_values, "NEO4J_HTTP_PORT", "7474")
    neo4j_user = args.neo4j_user or env_get(env_values, "NEO4J_USERNAME", "neo4j")
    neo4j_password = args.neo4j_password or env_get(env_values, "NEO4J_PASSWORD")
    minio_url = args.minio_url or env_get(env_values, "MINIO_PUBLIC_URL") or localhost_url_from_port(env_values, "MINIO_PORT", "9000")
    langfuse_url = args.langfuse_url or env_get(env_values, "LANGFUSE_PUBLIC_URL") or localhost_url_from_port(env_values, "LANGFUSE_PORT", "3000")

    retrieve_driver = env_get(env_values, "RETRIEVE_DRIVER", "")
    storage_type = env_get(env_values, "STORAGE_TYPE", "")
    neo4j_enabled = bool_env(env_get(env_values, "NEO4J_ENABLE", "")) or bool_env(env_get(env_values, "ENABLE_GRAPH_RAG", ""))
    phase1 = args.phase == "phase1-basic"
    phase2 = args.phase == "phase2-advanced"
    phase3 = args.phase == "phase3-precision"

    require_qdrant = args.require_qdrant or retrieve_driver == "qdrant" or phase2 or phase3
    require_minio = False if phase1 else (args.require_minio or storage_type == "minio" or phase2 or phase3)
    require_neo4j = False if phase1 else (args.require_neo4j or neo4j_enabled or phase2 or phase3)

    results: list[CheckResult] = []
    results.append(file_check(env_file, "env_file", required=phase1 or phase2 or phase3))
    if phase1:
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_process_config_phase1_basic.json", "phase1_process_config"))
        results.append(file_check(PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase1-basic.json", "phase1_kb_config"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_bulk_ingest.py", "bulk_ingest_script"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_phase1_model_check.py", "phase1_model_check"))
    elif phase2:
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_process_config_phase2_advanced.json", "phase2_process_config"))
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase2_advanced.json", "phase2_retrieval_config"))
        results.append(file_check(PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase2-advanced.json", "phase2_kb_config"))
        results.append(file_check(PROJECT_ROOT / "docker-compose.phase2-advanced.yml", "phase2_compose_overlay"))
        results.append(file_check(PROJECT_ROOT / "config" / "builtin_models.phase2-advanced.yaml", "phase2_builtin_models"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_bulk_ingest.py", "bulk_ingest_script"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_phase2_acceptance.py", "phase2_acceptance_script", required=False))
    elif phase3:
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_process_config_phase3_precision.json", "phase3_process_config"))
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase3_precision.json", "phase3_retrieval_config"))
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_financial_lexicon_phase3.json", "phase3_financial_lexicon"))
        results.append(file_check(PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.phase3-precision.json", "phase3_kb_config"))
        results.append(file_check(PROJECT_ROOT / "docker-compose.phase3-precision.yml", "phase3_compose_overlay"))
        results.append(file_check(PROJECT_ROOT / "config" / "builtin_models.phase3-precision.yaml", "phase3_builtin_models"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_bulk_ingest.py", "bulk_ingest_script"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_phase3_pdf.py", "phase3_pdf_pipeline"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_phase3_acceptance.py", "phase3_acceptance_script"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_phase3_quality_gate.py", "phase3_quality_gate"))
    else:
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_process_config_research_report.json", "process_config"))
        results.append(file_check(PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_recommended.json", "retrieval_config"))
        results.append(file_check(PROJECT_ROOT / "docs" / "ALPHAMIND_KB_CONFIG.json", "kb_config"))
        results.append(file_check(PROJECT_ROOT / "scripts" / "alphamind_bulk_ingest.py", "bulk_ingest_script"))

    results.append(env_presence_check(env_values, "WEKNORA_API_KEY", required=False))
    results.append(env_presence_check(env_values, "LLM_MODEL_NAME", required=phase1 or phase2 or phase3))
    results.append(env_presence_check(env_values, "EMBEDDING_MODEL_NAME", required=phase1 or phase2 or phase3))
    results.append(env_presence_check(env_values, "RERANK_MODEL_NAME", required=phase1 or phase2 or phase3))

    results.append(
        json_http_check(
            "weknora_health",
            urljoin(base_url.rstrip("/") + "/", "health"),
            required=True,
            timeout=args.timeout,
        )
    )
    results.append(
        check_auth_api(
            "weknora_system_info",
            urljoin(base_url.rstrip("/") + "/", "api/v1/system/info"),
            api_key=api_key,
            timeout=args.timeout,
            detail_keys=["version", "vector_store_engine", "graph_database_engine", "minio_enabled"],
        )
    )
    results.append(
        check_auth_api(
            "weknora_parser_engines",
            urljoin(base_url.rstrip("/") + "/", "api/v1/system/parser-engines"),
            api_key=api_key,
            timeout=args.timeout,
        )
    )
    results.append(
        check_auth_api(
            "weknora_storage_status",
            urljoin(base_url.rstrip("/") + "/", "api/v1/system/storage-engine-status"),
            api_key=api_key,
            timeout=args.timeout,
        )
    )

    qdrant_host_check = check_qdrant(qdrant_url, required=False, timeout=args.timeout)
    results.append(qdrant_host_check)
    # If Qdrant is intentionally not published to the host, validate it from the app container over the compose network.
    results.append(check_qdrant_internal(app_container, qdrant_internal_url, required=require_qdrant, timeout=args.timeout))
    if phase1:
        results.append(CheckResult("neo4j_http", "skip", False, "phase1-basic does not require Neo4j/GraphRAG"))
        results.append(CheckResult("minio_live", "skip", False, "phase1-basic uses local storage"))
        results.append(CheckResult("langfuse_http", "skip", False, "phase1-basic does not require Langfuse"))
    else:
        results.append(check_neo4j(neo4j_url, username=neo4j_user, password=neo4j_password, database=args.neo4j_database, required=require_neo4j, timeout=args.timeout))
        results.append(check_minio(minio_url, required=require_minio, timeout=args.timeout))
        results.append(check_langfuse(langfuse_url, required=args.require_langfuse, timeout=args.timeout))

    if phase1 and args.check_models:
        results.append(check_phase1_models(env_file, timeout=args.timeout, required=True))

    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_table(results)

    required_failures = [result for result in results if result.required and result.status == "fail"]
    return 1 if required_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
