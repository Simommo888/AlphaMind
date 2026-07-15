#!/usr/bin/env python3
"""Run fail-closed online acceptance for an evidence-bearing quote business path."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import alphamind_phase3_graph as phase3_graph

REQUIRED_RESULT_COLUMNS = (
    "company_a",
    "company_b",
    "shared_counterparty",
    "customer_source_doc_id",
    "customer_source_chunk_id",
    "customer_page",
    "customer_evidence_quote",
    "supplier_source_doc_id",
    "supplier_source_chunk_id",
    "supplier_page",
    "supplier_evidence_quote",
    "semantic_source",
)
REQUIRED_TRACE_FIELDS = (
    "customer_source_doc_id",
    "customer_source_chunk_id",
    "customer_page",
    "customer_evidence_quote",
    "supplier_source_doc_id",
    "supplier_source_chunk_id",
    "supplier_page",
    "supplier_evidence_quote",
)
ALLOWED_SEMANTIC_SOURCES = {"phase3_native", "relation_graph_bridge"}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_value(value: Any) -> bool:
    """Treat page zero as present while rejecting null and blank evidence."""
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def evaluate_result(
    result: dict[str, Any],
    *,
    company_a: str,
    company_b: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Validate non-empty topology and complete evidence for every returned row."""
    columns = result.get("columns") if isinstance(result, dict) else None
    rows = result.get("rows") if isinstance(result, dict) else None
    valid_columns = isinstance(columns, list) and len(columns) == len(set(columns))
    valid_rows = isinstance(rows, list)
    column_set = set(columns) if valid_columns else set()
    columns_complete = set(REQUIRED_RESULT_COLUMNS).issubset(column_set)

    evidence_rows: list[dict[str, Any]] = []
    row_shapes_valid = True
    if valid_columns and valid_rows:
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                row_shapes_valid = False
                continue
            evidence_rows.append(dict(zip(columns, row)))
    else:
        row_shapes_valid = False

    all_rows_traceable = bool(evidence_rows) and columns_complete and row_shapes_valid and all(
        all(_has_value(row.get(field)) for field in REQUIRED_TRACE_FIELDS)
        for row in evidence_rows
    )
    allowed_semantic_sources = bool(evidence_rows) and all(
        row.get("semantic_source") in ALLOWED_SEMANTIC_SOURCES for row in evidence_rows
    )
    checks = {
        "result_shape_valid": bool(valid_columns and valid_rows and row_shapes_valid),
        "required_columns_present": columns_complete,
        "non_empty": bool(evidence_rows),
        "all_rows_traceable": all_rows_traceable,
        "allowed_semantic_sources": allowed_semantic_sources,
    }
    issues = [name for name, passed in checks.items() if not passed]
    return {
        "status": "pass" if not issues else "fail",
        "generated_at": generated_at or _utc_timestamp(),
        "intent": "supply_chain_overlap",
        "query_parameters": {"company_a": company_a, "company_b": company_b},
        "row_count": len(evidence_rows),
        "checks": checks,
        "issues": issues,
        "evidence_rows": evidence_rows,
    }


def run_acceptance(
    *,
    company_a: str,
    company_b: str,
    report_path: Path | str,
    neo4j_url: str,
    username: str,
    password: str,
    database: str = "neo4j",
    timeout: float = 30.0,
    limit: int = 10,
    executor: Callable[..., dict[str, Any]] = phase3_graph.execute_plan,
) -> dict[str, Any]:
    """Execute the exact controlled plan, validate it, and persist a credential-free report."""
    plan = phase3_graph.build_query_plan(
        "supply_chain_overlap",
        company_a=company_a,
        company_b=company_b,
        limit=limit,
    )
    result = executor(
        plan,
        neo4j_url=neo4j_url,
        username=username,
        password=password,
        database=database,
        timeout=timeout,
    )
    report = evaluate_result(result, company_a=company_a, company_b=company_b)
    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a non-empty, evidence-traceable AlphaMind supply-chain multi-hop path in live Neo4j."
    )
    parser.add_argument("--company-a", default="中芯国际")
    parser.add_argument("--company-b", default="乐鑫科技")
    parser.add_argument("--report", default="dataset/alphamind_graph_online_acceptance.json")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[1] / ".env.phase3-precision"))
    parser.add_argument("--neo4j-url", default="")
    parser.add_argument("--neo4j-user", default="")
    parser.add_argument("--neo4j-password", default="")
    parser.add_argument("--neo4j-database", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = phase3_graph.parse_env_file(Path(args.env_file))
    report = run_acceptance(
        company_a=args.company_a,
        company_b=args.company_b,
        report_path=args.report,
        neo4j_url=args.neo4j_url
        or os.environ.get("NEO4J_HTTP_URL")
        or env.get("NEO4J_HTTP_URL")
        or f"http://localhost:{env.get('NEO4J_HTTP_PORT', '7474')}",
        username=args.neo4j_user
        or os.environ.get("NEO4J_USERNAME")
        or env.get("NEO4J_USERNAME", "neo4j"),
        password=args.neo4j_password
        or os.environ.get("NEO4J_PASSWORD")
        or env.get("NEO4J_PASSWORD", ""),
        database=args.neo4j_database
        or os.environ.get("NEO4J_DATABASE")
        or env.get("NEO4J_DATABASE", "neo4j"),
        timeout=args.timeout,
        limit=args.limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
