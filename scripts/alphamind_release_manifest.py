#!/usr/bin/env python3
"""Generate and fail-closed verify the AlphaMind Phase3 delivery manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

SCHEMA_VERSION = "alphamind.delivery.v2"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = "delivery/manifest.json"
DEFAULT_ACCEPTANCE = "dataset/phase3_runs/phase3_acceptance_latest.json"
REQUIRED_RELEASE_EVIDENCE = (
    "dataset/phase3_runs/phase3_acceptance_latest.json",
    "dataset/phase3_runs/phase3_quality_gate_latest.json",
    "dataset/phase3_runs/phase3_online_eval_latest.json",
    "dataset/phase3_runs/phase3_load_latest.json",
    "dataset/alphamind_graph_online_acceptance.json",
    "dataset/delivery/restore_validation_latest.json",
    "dataset/delivery/offline_images_report.json",
    "dataset/delivery/alphamind_delivery_acceptance_latest.json",
)


@dataclass(frozen=True)
class ArtifactSpec:
    path: str
    role: str
    aggregate_group: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    checked_files: int
    errors: tuple[str, ...]


_FIXED_ARTIFACTS = (
    ArtifactSpec(".env.phase3-precision.example", "configuration_example", "configuration"),
    ArtifactSpec("docker-compose.yml", "compose_base", "configuration"),
    ArtifactSpec("docker-compose.phase2-advanced.yml", "compose_phase2", "configuration"),
    ArtifactSpec("docker-compose.phase3-precision.yml", "compose_phase3", "configuration"),
    ArtifactSpec("docker-compose.delivery.yml", "compose_delivery", "configuration"),
    ArtifactSpec("docker-compose.restore-validation.yml", "compose_restore_validation", "configuration"),
    ArtifactSpec("config/builtin_models.phase3-precision.yaml", "model_configuration", "configuration"),
    ArtifactSpec("dataset/alphamind_process_config_phase3_precision.json", "process_configuration", "configuration"),
    ArtifactSpec("dataset/alphamind_retrieval_config_phase3_precision.json", "retrieval_configuration", "configuration"),
    ArtifactSpec("docs/ALPHAMIND_KB_CONFIG.phase3-precision.json", "knowledge_base_configuration", "configuration"),
    ArtifactSpec("dataset/alphamind_financial_lexicon_phase3.json", "financial_lexicon", "dataset"),
    ArtifactSpec("dataset/phase3_eval_queries.json", "evaluation_dataset", "dataset"),
    ArtifactSpec("dataset/alphamind_relation_graph.json", "relation_dataset"),
    ArtifactSpec("dataset/alphamind_relation_graph_report.json", "relation_evidence", "acceptance"),
    ArtifactSpec("dataset/alphamind_graph_online_acceptance.json", "graph_acceptance_evidence", "acceptance"),
    ArtifactSpec("dataset/phase3_runs/phase3_acceptance_latest.json", "acceptance_evidence", "acceptance"),
    ArtifactSpec("dataset/phase3_runs/phase3_quality_gate_latest.json", "quality_gate_evidence", "acceptance"),
    ArtifactSpec("dataset/phase3_runs/phase3_online_eval_latest.json", "online_eval_evidence", "acceptance"),
    ArtifactSpec("dataset/phase3_runs/phase3_business_load_latest.json", "business_load_evidence", "acceptance"),
    ArtifactSpec("dataset/phase3_runs/phase3_load_latest.json", "quality_gate_load_evidence", "acceptance"),
    ArtifactSpec("dataset/delivery/restore_validation_latest.json", "restore_validation_evidence", "acceptance"),
    ArtifactSpec("dataset/delivery/offline_images_report.json", "offline_delivery_evidence", "acceptance"),
    ArtifactSpec("dataset/delivery/alphamind_delivery_acceptance_latest.json", "delivery_acceptance_evidence", "acceptance"),
    ArtifactSpec("dataset/delivery/source_spec/金融系统报价单_终版.phase3.md", "source_specification"),
    ArtifactSpec("dataset/delivery/source_spec/金融系统报价单_终版.phase3.manifest.json", "source_specification_manifest"),
    ArtifactSpec("scripts/alphamind_bulk_ingest.py", "operator_script"),
    ArtifactSpec("scripts/alphamind_healthcheck.py", "operator_script"),
    ArtifactSpec("scripts/alphamind_delivery.py", "operator_script"),
    ArtifactSpec("scripts/alphamind_release_manifest.py", "reproducibility_script"),
    ArtifactSpec("scripts/alphamind_offline_images.py", "offline_delivery_script"),
    ArtifactSpec("scripts/alphamind_phase3_pdf.py", "pipeline_script"),
    ArtifactSpec("scripts/alphamind_phase3_retrieval.py", "pipeline_script"),
    ArtifactSpec("scripts/alphamind_phase3_graph.py", "pipeline_script"),
    ArtifactSpec("scripts/alphamind_relation_graph.py", "pipeline_script"),
    ArtifactSpec("scripts/alphamind_graph_online_acceptance.py", "acceptance_script"),
    ArtifactSpec("scripts/alphamind_phase3_online_eval.py", "acceptance_script"),
    ArtifactSpec("scripts/alphamind_phase3_acceptance.py", "acceptance_script"),
    ArtifactSpec("scripts/alphamind_phase3_quality_gate.py", "acceptance_script"),
    ArtifactSpec("scripts/alphamind_phase3_load_test.py", "acceptance_script"),
    ArtifactSpec("delivery/requirements-phase3.txt", "python_dependency_lock"),
    ArtifactSpec("go.mod", "go_dependency_manifest"),
    ArtifactSpec("go.sum", "go_dependency_lock"),
    ArtifactSpec("frontend/package.json", "frontend_dependency_manifest"),
    ArtifactSpec("frontend/package-lock.json", "frontend_dependency_lock"),
    ArtifactSpec("delivery/README.md", "delivery_documentation"),
    ArtifactSpec("delivery/CUSTOMER_ACCEPTANCE_CHECKLIST.md", "delivery_documentation"),
    ArtifactSpec("delivery/DEVIATION_REGISTER.md", "delivery_documentation"),
    ArtifactSpec("deploy/systemd/alphamind-qwen3.service", "linux_service_definition"),
    ArtifactSpec("deploy/systemd/qwen3.env.example", "linux_configuration_example"),
    ArtifactSpec("docs/ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_DELIVERY_HANDOFF.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_BACKUP_RESTORE.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_OFFLINE_DELIVERY.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_UBUNTU24_DEPLOYMENT.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_PHASE3_PRECISION_RUNBOOK.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_QWEN3_14B_VLLM.md", "model_runtime_documentation", "configuration"),
    ArtifactSpec("docs/ALPHAMIND_PHASE3_ACCEPTANCE_REPORT.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_RELATION_GRAPH_NEO4J.md", "delivery_documentation"),
    ArtifactSpec("docs/ALPHAMIND_KNOWLEDGE_GRAPH_SCHEMA_PHASE3.md", "delivery_documentation"),
)


def artifact_specs(root: Path) -> tuple[ArtifactSpec, ...]:
    tests = tuple(
        ArtifactSpec(path.relative_to(root).as_posix(), "automated_test")
        for path in sorted((root / "tests").glob("test_alphamind*.py"))
    )
    return tuple(sorted((*_FIXED_ARTIFACTS, *tests), key=lambda item: item.path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_record(root: Path, spec: ArtifactSpec) -> dict[str, object]:
    path = root / spec.path
    if not path.is_file():
        raise FileNotFoundError(f"required delivery artifact missing: {spec.path}")
    record: dict[str, object] = {
        "path": spec.path,
        "role": spec.role,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if spec.aggregate_group:
        record["aggregate_group"] = spec.aggregate_group
    return record


def _aggregate_sha256(records: Iterable[dict[str, object]], group: str) -> str:
    selected = sorted(
        (record for record in records if record.get("aggregate_group") == group),
        key=lambda record: str(record["path"]),
    )
    digest = hashlib.sha256()
    for record in selected:
        digest.update(f'{record["path"]}\0{record["sha256"]}\n'.encode("utf-8"))
    return digest.hexdigest()


def _run_git(root: Path, *args: str, text: bool = True) -> str | bytes:
    return subprocess.check_output(["git", *args], cwd=root, text=text, stderr=subprocess.STDOUT)


def _git_dirty_entries(root: Path, output_path: str) -> list[str]:
    raw = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", text=False)
    assert isinstance(raw, bytes)
    output = PurePosixPath(output_path).as_posix()
    entries: list[str] = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        entry = chunk.decode("utf-8", errors="surrogateescape")
        path = entry[3:].replace("\\", "/") if len(entry) >= 4 else entry
        if path == output:
            continue
        entries.append(entry)
    return sorted(entries)


def _git_state(root: Path, output_path: str) -> dict[str, object]:
    commit = str(_run_git(root, "rev-parse", "HEAD")).strip()
    branch = str(_run_git(root, "branch", "--show-current")).strip()
    entries = _git_dirty_entries(root, output_path)
    paths_digest = hashlib.sha256("\n".join(entries).encode("utf-8", errors="surrogateescape")).hexdigest()
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(entries),
        "dirty_entry_count": len(entries),
        "dirty_entries_sha256": paths_digest,
    }


def _acceptance(root: Path, relative_path: str) -> dict[str, object]:
    path = root / relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    checksum = sha256_file(path)
    supplied_run_id = payload.get("run_id")
    run_id = supplied_run_id if isinstance(supplied_run_id, str) and supplied_run_id else f"phase3-acceptance-{checksum[:16]}"
    return {
        "run_id": run_id,
        "run_id_source": "report.run_id" if supplied_run_id else "sha256-derived",
        "report": relative_path,
        "report_sha256": checksum,
        "status": payload.get("status", "unknown"),
    }


def _dependencies() -> dict[str, object]:
    return {
        "tools": {
            "python": ">=3.10",
            "git": ">=2.30",
            "docker_engine": ">=24",
            "docker_compose": ">=2.24",
        },
        "python_lock": "delivery/requirements-phase3.txt",
        "go_lock": ["go.mod", "go.sum"],
        "frontend_lock": ["frontend/package.json", "frontend/package-lock.json"],
        "services": ["PostgreSQL 17/ParadeDB 0.22.2", "Redis 7", "Qdrant 1.16.2", "Neo4j 2025.10.1", "MinIO RELEASE.2025-09-07T16-13-09Z"],
        "model_endpoints": ["OpenAI-compatible LLM", "OpenAI-compatible embedding", "generic reranker"],
    }


def _release_evidence(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    reports: list[dict[str, str]] = []
    all_pass = True
    for relative in paths:
        status = "missing"
        try:
            payload = json.loads((root / relative).read_text(encoding="utf-8"))
            status = str(payload.get("status") or "unknown").strip().lower()
        except (OSError, json.JSONDecodeError):
            status = "unreadable"
        if status != "pass":
            all_pass = False
        reports.append({"path": relative, "status": status})
    return {"status": "pass" if all_pass else "fail", "reports": reports}


def build_manifest(
    root: Path,
    *,
    specs: tuple[ArtifactSpec, ...] | None = None,
    acceptance_path: str = DEFAULT_ACCEPTANCE,
    output_path: str = DEFAULT_OUTPUT,
) -> dict[str, object]:
    root = root.resolve()
    selected_specs = specs if specs is not None else artifact_specs(root)
    records = [_file_record(root, item) for item in selected_specs]
    acceptance = _acceptance(root, acceptance_path)
    evidence_paths = REQUIRED_RELEASE_EVIDENCE if specs is None else (acceptance_path,)
    release_evidence = _release_evidence(root, evidence_paths)
    ready = acceptance.get("status") == "pass" and release_evidence.get("status") == "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "delivery_tier": "phase3-precision",
        "status": "ready_with_conditions" if ready else "blocked",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "scripts/alphamind_release_manifest.py",
        "source_spec": "dataset/delivery/source_spec/金融系统报价单_终版.phase3.md",
        "git": _git_state(root, output_path),
        "hashes": {
            "algorithm": "sha256",
            "file_encoding": "raw-bytes",
            "aggregate_encoding": "sorted path + NUL + file sha256 + LF",
            "configuration_sha256": _aggregate_sha256(records, "configuration"),
            "dataset_sha256": _aggregate_sha256(records, "dataset"),
            "acceptance_evidence_sha256": _aggregate_sha256(records, "acceptance"),
        },
        "acceptance": acceptance,
        "release_evidence": release_evidence,
        "dependencies": _dependencies(),
        "runtime": {
            "frontend_url": "http://localhost:18088",
            "backend_url": "http://localhost:18080",
            "qdrant_url": "http://localhost:16333",
            "neo4j_browser_url": "http://localhost:17474",
            "neo4j_bolt_url": "neo4j://localhost:17687",
            "minio_url": "http://localhost:19000",
            "minio_console_url": "http://localhost:19001",
        },
        "models": {
            "llm": "qwen3-14b",
            "llm_runtime": "vLLM OpenAI-compatible API",
            "llm_base_url": "http://host.docker.internal:8000/v1",
            "embedding": "qwen3-embedding-4b-local",
            "reranker": "qwen3-reranker-0.6b-local",
            "status": "target_qwen3_14b_vllm_configured_customer_host_validation_required",
            "quoted_exact_models": "customer_substituted_35b_to_qwen3_14b",
        },
        "inherited_requirements": {
            "cuda_tuning": {
                "status": "CONDITIONAL",
                "evidence": "Current Windows 10 + Docker Desktop 8GB GPU baseline is validated; customer Ubuntu/CUDA target tuning is not yet executed.",
                "customer_action": "Provide target Ubuntu 24.04 GPU host, driver/CUDA matrix, then rerun acceptance.",
            },
            "customer_data_redaction": {
                "status": "CONDITIONAL",
                "evidence": "No customer private corpus is included in this delivery inventory; redaction of future enterprise/WeCom material is not yet verifiable.",
                "customer_action": "Redact core commercial and personal information before transfer and record corpus hash after approval.",
            },
        },
        "conditional_requirements": [
            "Ubuntu 24.04 target-host and CUDA tuning validation",
            "Customer vLLM qwen3-14b target-host validation",
            "Exact quoted embedding 8B / reranker 0.6 model validation on target hardware",
            "MinerU/OpenDataLab-compatible OCR endpoint for scanned PDFs",
            "Customer-private-corpus redaction approval and final Recall/MRR/citation benchmark",
        ],
        "files": records,
    }


def generate_manifest(
    root: Path,
    output: Path,
    *,
    specs: tuple[ArtifactSpec, ...] | None = None,
    acceptance_path: str = DEFAULT_ACCEPTANCE,
) -> dict[str, object]:
    root = root.resolve()
    output = output if output.is_absolute() else root / output
    output_relative = output.resolve().relative_to(root).as_posix()
    manifest = build_manifest(root, specs=specs, acceptance_path=acceptance_path, output_path=output_relative)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, output)
    return manifest


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def verify_manifest(
    root: Path,
    manifest_path: Path,
    *,
    specs: tuple[ArtifactSpec, ...] | None = None,
) -> VerificationResult:
    root = root.resolve()
    manifest_path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    errors: list[str] = []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VerificationResult(False, 0, (f"manifest unreadable: {exc}",))
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    selected_specs = specs if specs is not None else artifact_specs(root)
    expected = {item.path: item for item in selected_specs}
    raw_records = payload.get("files")
    if not isinstance(raw_records, list):
        return VerificationResult(False, 0, tuple((*errors, "files must be a list")))
    records: dict[str, dict[str, object]] = {}
    for raw in raw_records:
        if not isinstance(raw, dict):
            errors.append("file record must be an object")
            continue
        relative = raw.get("path")
        if not _safe_relative_path(relative):
            errors.append(f"unsafe path in manifest: {relative!r}")
            continue
        assert isinstance(relative, str)
        if relative in records:
            errors.append(f"duplicate file record: {relative}")
            continue
        records[relative] = raw
    if set(records) != set(expected):
        missing = sorted(set(expected) - set(records))
        extra = sorted(set(records) - set(expected))
        errors.append(f"inventory mismatch: missing={missing}, extra={extra}")
    current_records: list[dict[str, object]] = []
    checked = 0
    for relative, spec in expected.items():
        record = records.get(relative)
        if record is None:
            continue
        path = root / relative
        if not path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        checked += 1
        actual_hash = sha256_file(path)
        actual_size = path.stat().st_size
        if record.get("sha256") != actual_hash:
            errors.append(f"sha256 mismatch: {relative}")
        if record.get("size") != actual_size:
            errors.append(f"size mismatch: {relative}")
        if record.get("role") != spec.role:
            errors.append(f"role mismatch: {relative}")
        current: dict[str, object] = {"path": relative, "sha256": actual_hash}
        if spec.aggregate_group:
            current["aggregate_group"] = spec.aggregate_group
        current_records.append(current)
    hashes = payload.get("hashes") if isinstance(payload.get("hashes"), dict) else {}
    for group, key in (("configuration", "configuration_sha256"), ("dataset", "dataset_sha256"), ("acceptance", "acceptance_evidence_sha256")):
        actual = _aggregate_sha256(current_records, group)
        if hashes.get(key) != actual:
            errors.append(f"aggregate sha256 mismatch: {key}")
    acceptance = payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else {}
    report = acceptance.get("report")
    if not _safe_relative_path(report):
        errors.append(f"unsafe acceptance report path: {report!r}")
    else:
        try:
            actual_acceptance = _acceptance(root, str(report))
            for key in ("run_id", "report_sha256", "status"):
                if acceptance.get(key) != actual_acceptance.get(key):
                    errors.append(f"acceptance {key} mismatch")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"acceptance report unreadable: {exc}")
    evidence_paths = REQUIRED_RELEASE_EVIDENCE if specs is None else (str(report),)
    expected_evidence = _release_evidence(root, evidence_paths)
    if payload.get("release_evidence") != expected_evidence:
        errors.append("release evidence status mismatch")
    expected_status = "ready_with_conditions" if acceptance.get("status") == "pass" and expected_evidence.get("status") == "pass" else "blocked"
    if payload.get("status") != expected_status:
        errors.append(f"manifest status mismatch: expected {expected_status}")
    if expected_status != "ready_with_conditions":
        errors.append("release evidence is not fully PASS")
    try:
        output_relative = manifest_path.resolve().relative_to(root).as_posix()
        actual_git = _git_state(root, output_relative)
        if actual_git.get("dirty"):
            errors.append("git worktree has uncommitted changes")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        errors.append(f"git state unavailable: {exc}")
    return VerificationResult(not errors, checked, tuple(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "verify"))
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    if args.command == "generate":
        try:
            manifest = generate_manifest(root, output)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
            print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        ready = manifest.get("status") == "ready_with_conditions"
        print(json.dumps({"status": "pass" if ready else "fail", "manifest": str(output), "files": len(manifest["files"]), "run_id": manifest["acceptance"]["run_id"], "release_status": manifest.get("status")}, ensure_ascii=False, indent=2))
        return 0 if ready else 1
    result = verify_manifest(root, output)
    print(json.dumps({"status": "pass" if result.ok else "fail", "manifest": str(output), "checked_files": result.checked_files, "errors": result.errors}, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
