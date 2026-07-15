#!/usr/bin/env python3
"""Fail-closed AlphaMind Phase3 offline/live quality gate."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESS = PROJECT_ROOT / "dataset" / "alphamind_process_config_phase3_precision.json"
DEFAULT_RETRIEVAL = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase3_precision.json"
DEFAULT_LEXICON = PROJECT_ROOT / "dataset" / "alphamind_financial_lexicon_phase3.json"
DEFAULT_EVAL_QUERIES = PROJECT_ROOT / "dataset" / "phase3_eval_queries.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "dataset" / "phase3_runs"
DEFAULT_REPORT = DEFAULT_OUT_DIR / "phase3_quality_gate_latest.json"


@dataclass
class CommandSpec:
    name: str
    argv: list[str]
    timeout: int = 600


@dataclass
class StepResult:
    name: str
    status: str
    exit_code: int
    elapsed_ms: int
    output: str


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_bundle(process_path: Path, retrieval_path: Path, lexicon_path: Path) -> list[str]:
    issues: list[str] = []
    for label, path in (("process", process_path), ("retrieval", retrieval_path), ("lexicon", lexicon_path)):
        if not path.is_file():
            issues.append(f"missing {label} config: {path}")
    if issues:
        return issues
    try:
        process = _load_json(process_path)
        retrieval = _load_json(retrieval_path)
        lexicon = _load_json(lexicon_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid JSON: {exc}"]
    nodes = process.get("extract_config", {}).get("nodes", [])
    node_names = {node.get("name") for node in nodes if isinstance(node, dict)}
    required_nodes = {"Company", "Organization", "Institution", "Executive", "Report", "FinancialMetric", "Evidence"}
    missing_nodes = sorted(required_nodes - node_names)
    if missing_nodes:
        issues.append(f"missing Phase3 graph nodes including Evidence: {missing_nodes}")
    if not process.get("graph_enabled") or not process.get("extract_config", {}).get("enabled"):
        issues.append("Phase3 Graph RAG extraction must be enabled")
    extract_config = process.get("extract_config", {})
    relation_types = {
        str(relation.get("type") or "")
        for relation in extract_config.get("relations", [])
        if isinstance(relation, dict) and relation.get("type")
    }
    tags = {str(tag) for tag in extract_config.get("tags", []) if str(tag)}
    unlisted_relation_types = sorted(relation_types - tags)
    if unlisted_relation_types:
        issues.append(f"extract_config tags missing relation types: {unlisted_relation_types}")
    weights = retrieval.get("fusion", {}).get("weights", {})
    try:
        total_weight = sum(float(value) for value in weights.values())
    except (TypeError, ValueError):
        total_weight = -1
    if set(weights) != {"dense", "sparse", "graph"} or abs(total_weight - 1.0) > 1e-9:
        issues.append("fusion weights must contain dense/sparse/graph and sum to 1.0")
    fields = set(retrieval.get("answer_policy", {}).get("financial_metric_fields", []))
    required_fields = {"period", "value", "unit", "source_doc_id", "source_chunk_id", "page"}
    if not required_fields.issubset(fields):
        issues.append(f"answer policy missing evidence fields: {sorted(required_fields - fields)}")
    entries = lexicon.get("entries") if isinstance(lexicon, dict) else None
    if not isinstance(entries, list) or len(entries) < 10:
        issues.append("financial lexicon must contain at least 10 entries")
    else:
        canonicals = [str(entry.get("canonical") or "") for entry in entries if isinstance(entry, dict)]
        if len(canonicals) != len(set(canonicals)) or any(not value for value in canonicals):
            issues.append("financial lexicon canonical terms must be non-empty and unique")
    return issues


def validate_eval_suite(path: Path) -> list[str]:
    if not path.is_file():
        return [f"missing Phase3 eval suite: {path}"]
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid Phase3 eval suite: {exc}"]
    queries = payload.get("queries", []) if isinstance(payload, dict) else []
    live_gate = payload.get("live_gate", {}) if isinstance(payload, dict) else {}
    issues: list[str] = []
    if len(queries) < 15:
        issues.append("Phase3 eval suite must contain at least 15 fixed queries")
    if not live_gate.get("text") or not live_gate.get("answer_must_contain"):
        issues.append("Phase3 eval suite live_gate requires text and answer_must_contain")
    ids = [str(item.get("id") or "") for item in queries if isinstance(item, dict)]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        issues.append("Phase3 eval query IDs must be non-empty and unique")
    unlabeled: list[str] = []
    incomplete_no_evidence: list[str] = []
    for item in queries:
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("id") or "<missing>")
        is_no_evidence = str(item.get("gt_status") or "") == "no_evidence" or str(item.get("expected_behavior") or "") == "no_evidence"
        relevant = item.get("relevant_doc_ids")
        if is_no_evidence:
            refusal_terms = item.get("answer_must_contain_any") or item.get("answer_must_contain")
            if not isinstance(refusal_terms, list) or not refusal_terms:
                incomplete_no_evidence.append(query_id)
        elif not isinstance(relevant, list) or not relevant:
            unlabeled.append(query_id)
    if unlabeled:
        issues.append(f"Phase3 eval ground truth missing relevant_doc_ids: {unlabeled}")
    if incomplete_no_evidence:
        issues.append(f"Phase3 no-evidence ground truth missing refusal terms: {incomplete_no_evidence}")
    return issues


def load_live_gate(path: Path) -> dict[str, Any]:
    issues = validate_eval_suite(path)
    if issues:
        raise ValueError("; ".join(issues))
    payload = _load_json(path)
    return dict(payload["live_gate"])


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_command_plan(args: argparse.Namespace) -> list[CommandSpec]:
    python = sys.executable
    eval_path = Path(getattr(args, "eval_queries", DEFAULT_EVAL_QUERIES))
    live_gate = load_live_gate(eval_path)
    assertion_args = [
        value
        for term in live_gate.get("answer_must_contain", [])
        for value in ("--answer-must-contain", str(term))
    ]
    plan = [
        CommandSpec("config_validation", [python, "scripts/alphamind_phase3_quality_gate.py", "--validate-only", "--process-config", str(args.process_config), "--retrieval-config", str(args.retrieval_config), "--lexicon", str(args.lexicon), "--eval-queries", str(eval_path)]),
        CommandSpec("phase3_unit_tests", [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_alphamind_phase3*.py", "-v"]),
        CommandSpec("real_pdf_parse", [python, "scripts/alphamind_phase3_pdf.py", str(args.sample_pdf), "--out-dir", str(Path(args.out_dir) / "pdf"), "--max-pages", str(args.max_pages), "--strict", "--require-table", "--min-numeric-preservation", "0.98"], 1200),
    ]
    if args.live:
        plan.append(CommandSpec("healthcheck", [python, "scripts/alphamind_healthcheck.py", "--phase", "phase3-precision", "--env-file", str(args.env_file), "--require-qdrant", "--require-neo4j", "--require-minio"], 600))
        plan.append(CommandSpec(
            "phase3_retrieval_e2e",
            [
                python,
                "scripts/alphamind_phase3_acceptance.py",
                "--pdf",
                str(args.sample_pdf),
                "--env-file",
                str(args.env_file),
                "--process-config",
                str(args.process_config),
                "--retrieval-config",
                str(args.retrieval_config),
                "--max-pages",
                str(args.max_pages),
                "--query",
                str(live_gate["text"]),
                *assertion_args,
            ],
            1800,
        ))
        plan.append(CommandSpec(
            "fixed_retrieval_eval",
            [
                python,
                "scripts/alphamind_phase3_online_eval.py",
                "--env-file",
                str(args.env_file),
                "--queries",
                str(eval_path),
                "--retrieval-config",
                str(args.retrieval_config),
                "--out",
                str(Path(args.out_dir) / "phase3_online_eval_latest.json"),
                "--rerank-batch-size",
                "10",
            ],
            3600,
        ))
        env = parse_env_file(Path(args.env_file))
        base_url = os.environ.get("WEKNORA_BASE_URL") or env.get("WEKNORA_BASE_URL") or "http://localhost:8080"
        kb_id_file = Path(getattr(args, "kb_id_file", PROJECT_ROOT / "dataset" / "alphamind_phase3_precision_kb_id.txt"))
        kb_id = kb_id_file.read_text(encoding="utf-8").strip() if kb_id_file.is_file() else ""
        load_payload = json.dumps(
            {
                "query_text": str(live_gate["text"]),
                "match_count": 10,
                "vector_threshold": 0.1,
                "keyword_threshold": 0.18,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        load_argv = [
            python,
            "scripts/alphamind_phase3_load_test.py",
            "--url",
            base_url.rstrip("/") + f"/api/v1/knowledge-bases/{kb_id}/hybrid-search",
            "--method",
            "POST",
            "--payload",
            load_payload,
            "--env-file",
            str(args.env_file),
            "--expect-json-items",
            "--requests",
            str(args.requests),
            "--warmup-requests",
            "1",
            "--concurrency",
            str(args.concurrency),
            "--out",
            str(Path(args.out_dir) / "phase3_load_latest.json"),
        ]
        plan.append(CommandSpec("load_test", load_argv, 1800))
        # Business POST load must run while the embedding service is resident.
        # The fixed eval intentionally unloads it before local LLM chat on 8GB GPUs.
        load_spec = plan.pop()
        fixed_index = next(i for i, step in enumerate(plan) if step.name == "fixed_retrieval_eval")
        plan.insert(fixed_index, load_spec)
    if getattr(args, "timeout", 0):
        for step in plan:
            step.timeout = int(args.timeout)
    return plan


def run_step(spec: CommandSpec) -> StepResult:
    start = time.perf_counter()
    try:
        completed = subprocess.run(spec.argv, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=spec.timeout)
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return StepResult(spec.name, "pass" if completed.returncode == 0 else "fail", completed.returncode, int((time.perf_counter() - start) * 1000), output)
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(value) for value in (exc.stdout, exc.stderr, f"timeout after {spec.timeout}s") if value)
        return StepResult(spec.name, "fail", 124, int((time.perf_counter() - start) * 1000), output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind Phase3 precision quality gate.")
    parser.add_argument("--process-config", default=str(DEFAULT_PROCESS))
    parser.add_argument("--retrieval-config", default=str(DEFAULT_RETRIEVAL))
    parser.add_argument("--lexicon", default=str(DEFAULT_LEXICON))
    parser.add_argument("--eval-queries", default=str(DEFAULT_EVAL_QUERIES))
    parser.add_argument("--sample-pdf", default="")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env.phase3-precision"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = validate_bundle(Path(args.process_config), Path(args.retrieval_config), Path(args.lexicon))
    issues.extend(validate_eval_suite(Path(args.eval_queries)))
    if args.validate_only:
        print(json.dumps({"status": "pass" if not issues else "fail", "issues": issues}, ensure_ascii=False, indent=2))
        return 0 if not issues else 1
    if not args.sample_pdf:
        print("--sample-pdf is required for the real PDF gate", file=sys.stderr)
        return 2
    steps: list[StepResult] = []
    started = time.perf_counter()
    for spec in build_command_plan(args):
        result = run_step(spec)
        steps.append(result)
        print(f"{result.status.upper():<5} {result.name:<22} {result.elapsed_ms:>8}ms")
        if result.status != "pass" and not args.keep_going:
            break
    status = "pass" if steps and all(step.status == "pass" for step in steps) else "fail"
    report = {"schema_version": "alphamind.phase3.gate.v1", "status": status, "mode": "live" if args.live else "offline", "elapsed_ms": int((time.perf_counter() - started) * 1000), "steps": [asdict(step) for step in steps]}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PHASE3 QUALITY GATE: {status.upper()}")
    print(f"report={report_path}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
