#!/usr/bin/env python3
"""
Diagnose the local AlphaMind / WeKnora Docker stack.

The script is intentionally read-only by default. It checks:
- Docker CLI availability and daemon connectivity
- Docker Compose config rendering
- Container status for key WeKnora services
- Port connectivity for app/frontend/qdrant/neo4j/minio/langfuse
- Recent logs for common error keywords

Use --include-logs to print short log tails. Use --start-command to print the
recommended compose up command, but the script does not start containers.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.alphamind.yml"]
DEFAULT_PROFILES = ["qdrant", "neo4j", "minio", "langfuse"]
DEFAULT_KEY_SERVICES = [
    "frontend",
    "app",
    "docreader",
    "postgres",
    "redis",
    "qdrant",
    "neo4j",
    "minio",
    "langfuse-web",
    "langfuse-worker",
    "langfuse-clickhouse",
]
DEFAULT_PORTS = [
    ("frontend", "127.0.0.1", 8088),
    ("app", "127.0.0.1", 8080),
    ("qdrant_rest", "127.0.0.1", 6333),
    ("neo4j_http", "127.0.0.1", 7474),
    ("neo4j_bolt", "127.0.0.1", 7687),
    ("minio_s3", "127.0.0.1", 9000),
    ("minio_console", "127.0.0.1", 9001),
    ("langfuse", "127.0.0.1", 3000),
]
PHASE1_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.phase1-basic.yml"]
PHASE1_PROFILES = ["qdrant"]
PHASE1_KEY_SERVICES = ["frontend", "app", "docreader", "postgres", "redis", "qdrant"]
PHASE1_PORTS = [
    ("frontend", "127.0.0.1", 8088),
    ("app", "127.0.0.1", 8080),
    ("qdrant_rest", "127.0.0.1", 6333),
]
PHASE2_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.phase2-advanced.yml"]
PHASE2_PROFILES = ["qdrant", "neo4j", "minio"]
PHASE2_KEY_SERVICES = ["frontend", "app", "docreader", "postgres", "redis", "qdrant", "neo4j", "minio"]
PHASE2_PORTS = [
    ("frontend", "127.0.0.1", 8088),
    ("app", "127.0.0.1", 8080),
    ("qdrant_rest", "127.0.0.1", 6333),
    ("neo4j_http", "127.0.0.1", 7474),
    ("neo4j_bolt", "127.0.0.1", 7687),
    ("minio_s3", "127.0.0.1", 9000),
    ("minio_console", "127.0.0.1", 9001),
]
ERROR_KEYWORDS = (
    "error",
    "fatal",
    "panic",
    "exception",
    "failed",
    "refused",
    "timeout",
    "authentication failed",
    "permission denied",
    "no such host",
    "connection reset",
)


@dataclass
class DiagnosticResult:
    name: str
    status: str  # pass / warn / fail / info
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose AlphaMind Docker stack readiness.")
    parser.add_argument("--phase", choices=("alphamind", "phase1-basic", "phase2-advanced"), default="alphamind", help="Select compose/profile/service set.")
    parser.add_argument("--project-dir", default=str(PROJECT_ROOT))
    parser.add_argument("--include-logs", action="store_true", help="Print recent logs for key services.")
    parser.add_argument("--log-lines", type=int, default=80)
    parser.add_argument("--json", action="store_true", help="Print JSON result list.")
    parser.add_argument("--start-command", action="store_true", help="Print recommended compose up command.")
    parser.add_argument("--timeout", type=float, default=2.0, help="Port connect timeout in seconds.")
    return parser.parse_args()


def run_command(command: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return 124, str(stdout).strip(), str(stderr).strip() or f"timeout after {timeout}s"


def phase_settings(phase: str) -> tuple[list[str], list[str], list[str], list[tuple[str, str, int]]]:
    if phase == "phase1-basic":
        return PHASE1_COMPOSE_FILES, PHASE1_PROFILES, PHASE1_KEY_SERVICES, PHASE1_PORTS
    if phase == "phase2-advanced":
        return PHASE2_COMPOSE_FILES, PHASE2_PROFILES, PHASE2_KEY_SERVICES, PHASE2_PORTS
    return DEFAULT_COMPOSE_FILES, DEFAULT_PROFILES, DEFAULT_KEY_SERVICES, DEFAULT_PORTS


def compose_command(extra: Iterable[str], *, compose_files: list[str], profiles: list[str]) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in compose_files:
        command.extend(["-f", compose_file])
    for profile in profiles:
        command.extend(["--profile", profile])
    command.extend(extra)
    return command


def check_command(name: str, command: list[str], cwd: Path, timeout: int = 30) -> DiagnosticResult:
    code, stdout, stderr = run_command(command, cwd, timeout=timeout)
    if code == 0:
        first_line = stdout.splitlines()[0] if stdout else "ok"
        return DiagnosticResult(name, "pass", first_line[:300])
    return DiagnosticResult(name, "fail", (stderr or stdout or f"exit {code}")[:500])


def get_compose_ps(cwd: Path, *, compose_files: list[str], profiles: list[str]) -> tuple[DiagnosticResult, list[dict[str, object]]]:
    command = compose_command(["ps", "--format", "json"], compose_files=compose_files, profiles=profiles)
    code, stdout, stderr = run_command(command, cwd, timeout=30)
    if code != 0:
        return DiagnosticResult("compose_ps", "fail", (stderr or stdout or f"exit {code}")[:500]), []

    rows: list[dict[str, object]] = []
    if not stdout:
        return DiagnosticResult("compose_ps", "warn", "no containers returned"), []

    # docker compose may return either a JSON array or one JSON object per line depending on version.
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            rows = [row for row in parsed if isinstance(row, dict)]
        elif isinstance(parsed, dict):
            rows = [parsed]
    except json.JSONDecodeError:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                pass

    if not rows:
        return DiagnosticResult("compose_ps", "warn", "could not parse compose ps JSON output"), []
    return DiagnosticResult("compose_ps", "pass", f"{len(rows)} containers listed"), rows


def service_name(row: dict[str, object]) -> str:
    for key in ("Service", "Name"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def service_state(row: dict[str, object]) -> str:
    for key in ("State", "Status"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def summarize_services(rows: list[dict[str, object]], *, key_services: list[str]) -> list[DiagnosticResult]:
    by_service = {service_name(row): row for row in rows}
    results = []
    for service in key_services:
        row = by_service.get(service)
        if not row:
            # Langfuse optional unless profile is running; still useful to warn.
            status = "warn" if service.startswith("langfuse") else "fail"
            results.append(DiagnosticResult(f"service_{service}", status, "not found in compose ps"))
            continue
        state = service_state(row).lower()
        if "running" in state or state == "running":
            results.append(DiagnosticResult(f"service_{service}", "pass", service_state(row)))
        elif "exited" in state or "dead" in state:
            results.append(DiagnosticResult(f"service_{service}", "fail", service_state(row)))
        else:
            results.append(DiagnosticResult(f"service_{service}", "warn", service_state(row)))
    return results


def check_port(name: str, host: str, port: int, timeout: float) -> DiagnosticResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return DiagnosticResult(f"port_{name}", "pass", f"{host}:{port} reachable")
    except OSError as exc:
        return DiagnosticResult(f"port_{name}", "warn", f"{host}:{port} not reachable: {exc}")


def inspect_logs(cwd: Path, service: str, lines: int, *, compose_files: list[str], profiles: list[str]) -> tuple[DiagnosticResult, str]:
    code, stdout, stderr = run_command(compose_command(["logs", service, f"--tail={lines}"], compose_files=compose_files, profiles=profiles), cwd, timeout=20)
    if code != 0:
        return DiagnosticResult(f"logs_{service}", "warn", (stderr or stdout or f"exit {code}")[:500]), ""
    lowered = stdout.lower()
    hits = sorted({keyword for keyword in ERROR_KEYWORDS if keyword in lowered})
    if hits:
        return DiagnosticResult(f"logs_{service}", "warn", f"keywords: {', '.join(hits)}"), stdout
    return DiagnosticResult(f"logs_{service}", "pass", "no common error keywords in recent logs"), stdout


def print_results(results: list[DiagnosticResult]) -> None:
    print("\nAlphaMind Docker stack diagnosis")
    print("=" * 90)
    for result in results:
        print(f"{result.status.upper():<5} {result.name:<36} {result.detail}")
    print("=" * 90)
    failures = [result for result in results if result.status == "fail"]
    warnings = [result for result in results if result.status == "warn"]
    print(f"failures={len(failures)}, warnings={len(warnings)}, total={len(results)}")


def recommended_start_command(project_dir: Path, phase: str) -> str:
    if phase == "phase1-basic":
        return (
            f"cd {project_dir}\n"
            "copy .env.phase1-basic.example .env.phase1-basic  # first run only; then edit local model endpoints\n"
            "docker compose --env-file .env.phase1-basic -f docker-compose.yml -f docker-compose.phase1-basic.yml `\n"
            "  --profile qdrant up -d"
        )
    if phase == "phase2-advanced":
        return (
            f"cd {project_dir}\n"
            "copy .env.phase2-advanced.example .env.phase2-advanced  # first run only; then edit local model endpoints and API key\n"
            "docker compose --env-file .env.phase2-advanced -f docker-compose.yml -f docker-compose.phase2-advanced.yml `\n"
            "  --profile qdrant --profile neo4j --profile minio up -d"
        )
    return (
        f"cd {project_dir}\n"
        "docker compose -f docker-compose.yml -f docker-compose.alphamind.yml `\n"
        "  --profile qdrant --profile neo4j --profile minio --profile langfuse up -d"
    )


def main() -> int:
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()
    compose_files, profiles, key_services, ports = phase_settings(args.phase)
    results: list[DiagnosticResult] = []

    results.append(DiagnosticResult("project_dir", "pass" if project_dir.exists() else "fail", str(project_dir)))
    for compose_file in compose_files:
        path = project_dir / compose_file
        results.append(DiagnosticResult(f"file_{compose_file}", "pass" if path.exists() else "fail", str(path)))
    if args.phase == "phase1-basic":
        env_path = project_dir / ".env.phase1-basic"
        example_path = project_dir / ".env.phase1-basic.example"
        if env_path.exists():
            results.append(DiagnosticResult("file_.env.phase1-basic", "pass", str(env_path)))
        elif example_path.exists():
            # Let compose config render before the user has copied the real env file.
            os.environ.setdefault("PHASE1_ENV_FILE", ".env.phase1-basic.example")
            results.append(DiagnosticResult("file_.env.phase1-basic", "warn", f"missing {env_path}; using {example_path} for config validation"))
        else:
            results.append(DiagnosticResult("file_.env.phase1-basic", "fail", f"missing {env_path} and {example_path}"))
    elif args.phase == "phase2-advanced":
        env_path = project_dir / ".env.phase2-advanced"
        example_path = project_dir / ".env.phase2-advanced.example"
        if env_path.exists():
            results.append(DiagnosticResult("file_.env.phase2-advanced", "pass", str(env_path)))
        elif example_path.exists():
            # Let compose config render before the user has copied the real env file.
            os.environ.setdefault("PHASE2_ENV_FILE", ".env.phase2-advanced.example")
            results.append(DiagnosticResult("file_.env.phase2-advanced", "warn", f"missing {env_path}; using {example_path} for config validation"))
        else:
            results.append(DiagnosticResult("file_.env.phase2-advanced", "fail", f"missing {env_path} and {example_path}"))

    results.append(check_command("docker_version", ["docker", "--version"], project_dir, timeout=10))
    results.append(check_command("docker_daemon", ["docker", "info", "--format", "{{.ServerVersion}}"], project_dir, timeout=10))
    results.append(check_command("compose_config", compose_command(["config", "--quiet"], compose_files=compose_files, profiles=profiles), project_dir, timeout=30))

    ps_result, rows = get_compose_ps(project_dir, compose_files=compose_files, profiles=profiles)
    results.append(ps_result)
    if rows:
        results.extend(summarize_services(rows, key_services=key_services))

    for name, host, port in ports:
        results.append(check_port(name, host, port, args.timeout))

    log_output: list[tuple[str, str]] = []
    for service in key_services:
        result, output = inspect_logs(project_dir, service, args.log_lines, compose_files=compose_files, profiles=profiles)
        results.append(result)
        if args.include_logs and output:
            log_output.append((service, output))

    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_results(results)
        if args.start_command:
            print("\nRecommended start command:")
            print(recommended_start_command(project_dir, args.phase))
        if log_output:
            print("\nRecent logs")
            print("=" * 90)
            for service, output in log_output:
                print(f"\n--- {service} ---")
                print(output[-4000:])

    # Fail only for hard failures in docker/compose/project files and core services.
    hard_fail_prefixes = (
        "project_dir",
        "file_",
        "docker_",
        "compose_config",
        "service_app",
        "service_docreader",
        "service_postgres",
        "service_redis",
    )
    hard_failures = [
        result for result in results
        if result.status == "fail" and result.name.startswith(hard_fail_prefixes)
    ]
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
