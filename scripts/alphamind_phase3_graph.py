#!/usr/bin/env python3
"""Fail-closed, parameterized multi-hop graph query plans for AlphaMind Phase3."""
from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class QueryPlan:
    intent: str
    hops: int
    cypher: str
    params: dict[str, Any]
    evidence_required: tuple[str, ...] = ("source_doc_id", "source_chunk_id", "page")


def build_neo4j_payload(plan: QueryPlan) -> dict[str, Any]:
    """Build Neo4j transactional HTTP payload without interpolating user values."""
    return {
        "statements": [
            {
                "statement": plan.cypher,
                "parameters": dict(plan.params),
                "resultDataContents": ["row"],
            }
        ]
    }


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('\"').strip("'")
    return values


def execute_plan(
    plan: QueryPlan,
    *,
    neo4j_url: str,
    username: str,
    password: str,
    database: str = "neo4j",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Execute an allow-listed plan through Neo4j's parameterized transaction API."""
    if not neo4j_url or not username or not password:
        raise ValueError("neo4j_url, username and password are required")
    endpoint = neo4j_url.rstrip("/") + f"/db/{quote(database, safe='')}/tx/commit"
    body = json.dumps(build_neo4j_payload(plan), ensure_ascii=False).encode("utf-8")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Neo4j transaction request failed: {exc}") from exc
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        safe_errors = [
            {"code": item.get("code", ""), "message": item.get("message", "")}
            for item in errors
            if isinstance(item, dict)
        ]
        raise RuntimeError(f"Neo4j query failed: {safe_errors}")
    results = payload.get("results", []) if isinstance(payload, dict) else []
    first = results[0] if results else {}
    return {
        "intent": plan.intent,
        "columns": first.get("columns", []),
        "rows": [item.get("row", []) for item in first.get("data", []) if isinstance(item, dict)],
    }


def _required(value: str, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def build_graph_chunk_rank_plan(knowledge_id: str, terms: list[str], *, limit: int = 200) -> QueryPlan:
    knowledge_id = _required(knowledge_id, "knowledge_id")
    clean_terms = [str(term).strip() for term in terms if str(term).strip()]
    if not clean_terms:
        raise ValueError("at least one graph ranking term is required")
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    cypher = """
MATCH (n)
WHERE n.kg = $knowledge_id AND size(coalesce(n.chunks, [])) > 0
WITH n, size([term IN $terms WHERE toLower(coalesce(n.name, '')) CONTAINS toLower(term)]) AS term_hits
UNWIND n.chunks AS chunk_id
WITH chunk_id, max(term_hits) AS term_hits, count(*) AS graph_hits
RETURN chunk_id, term_hits, graph_hits
ORDER BY term_hits DESC, graph_hits DESC, chunk_id
LIMIT $limit
""".strip()
    return QueryPlan("graph_chunk_rank", 1, cypher, {"knowledge_id": knowledge_id, "terms": clean_terms, "limit": limit}, ())


def build_query_plan(
    intent: str,
    *,
    company_a: str,
    company_b: str = "",
    metric: str = "",
    periods: list[str] | None = None,
    limit: int = 50,
) -> QueryPlan:
    company_a = _required(company_a, "company_a")
    if limit <= 0 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if intent == "supply_chain_overlap":
        company_b = _required(company_b, "company_b")
        # Preserve the Phase3-native HAS_CUSTOMER/HAS_SUPPLIER model while
        # bridging the deterministic AlphaMindEntity graph's canonical
        # supplier -> customer direction (SUPPLIES_TO). Both branches expose
        # the same evidence-bearing result contract.
        cypher = """
CALL {
  MATCH (a:Company), (b:Company)
  WHERE (a.name = $company_a OR a.ticker = $company_a OR $company_a IN coalesce(a.aliases, []))
    AND (b.name = $company_b OR b.ticker = $company_b OR $company_b IN coalesce(b.aliases, []))
  MATCH (a)-[ac:HAS_CUSTOMER]->(party:Organization)<-[bs:HAS_SUPPLIER]-(b)
  WHERE ac.source_doc_id IS NOT NULL AND ac.source_chunk_id IS NOT NULL AND ac.page IS NOT NULL
    AND bs.source_doc_id IS NOT NULL AND bs.source_chunk_id IS NOT NULL AND bs.page IS NOT NULL
    AND coalesce(ac.evidence_quote, ac.quote, '') <> ''
    AND coalesce(bs.evidence_quote, bs.quote, '') <> ''
  RETURN a.name AS company_a, b.name AS company_b, party.name AS shared_counterparty,
         ac.rank AS customer_rank, bs.rank AS supplier_rank,
         ac.source_doc_id AS customer_source_doc_id,
         ac.source_chunk_id AS customer_source_chunk_id,
         ac.page AS customer_page,
         coalesce(ac.evidence_quote, ac.quote) AS customer_evidence_quote,
         bs.source_doc_id AS supplier_source_doc_id,
         bs.source_chunk_id AS supplier_source_chunk_id,
         bs.page AS supplier_page,
         coalesce(bs.evidence_quote, bs.quote) AS supplier_evidence_quote,
         'phase3_native' AS semantic_source
  UNION
  MATCH (a:AlphaMindEntity:Company), (b:AlphaMindEntity:Company)
  WHERE (a.name = $company_a OR a.ticker = $company_a OR $company_a IN coalesce(a.aliases, []))
    AND (b.name = $company_b OR b.ticker = $company_b OR $company_b IN coalesce(b.aliases, []))
    AND a.graph_source = $relation_graph_source AND b.graph_source = $relation_graph_source
  MATCH (a)-[ac:SUPPLIES_TO]->(party:AlphaMindEntity:Company)-[bs:SUPPLIES_TO]->(b)
  WHERE party.graph_source = $relation_graph_source
    AND ac.graph_source = $relation_graph_source AND bs.graph_source = $relation_graph_source
    AND ac.source_doc_id IS NOT NULL AND ac.source_chunk_id IS NOT NULL AND ac.page IS NOT NULL
    AND bs.source_doc_id IS NOT NULL AND bs.source_chunk_id IS NOT NULL AND bs.page IS NOT NULL
    AND coalesce(ac.evidence_quote, '') <> '' AND coalesce(bs.evidence_quote, '') <> ''
  RETURN a.name AS company_a, b.name AS company_b, party.name AS shared_counterparty,
         null AS customer_rank, null AS supplier_rank,
         ac.source_doc_id AS customer_source_doc_id,
         ac.source_chunk_id AS customer_source_chunk_id,
         ac.page AS customer_page,
         ac.evidence_quote AS customer_evidence_quote,
         bs.source_doc_id AS supplier_source_doc_id,
         bs.source_chunk_id AS supplier_source_chunk_id,
         bs.page AS supplier_page,
         bs.evidence_quote AS supplier_evidence_quote,
         'relation_graph_bridge' AS semantic_source
}
RETURN company_a, company_b, shared_counterparty,
       customer_rank, supplier_rank,
       customer_source_doc_id, customer_source_chunk_id, customer_page, customer_evidence_quote,
       supplier_source_doc_id, supplier_source_chunk_id, supplier_page, supplier_evidence_quote,
       semantic_source
ORDER BY coalesce(customer_rank, 999), coalesce(supplier_rank, 999), shared_counterparty
LIMIT $limit
""".strip()
        return QueryPlan(
            intent,
            4,
            cypher,
            {
                "company_a": company_a,
                "company_b": company_b,
                "relation_graph_source": "alphamind_relation_graph",
                "limit": limit,
            },
        )
    if intent == "institution_executive_network":
        cypher = """
MATCH (c:Company)
WHERE c.name = $company_a OR c.ticker = $company_a OR $company_a IN coalesce(c.aliases, [])
MATCH (c)-[ce:HAS_EXECUTIVE]->(person:Executive)<-[ie:APPOINTED|NOMINATED|AFFILIATED_WITH]-(institution:Institution)
WHERE ce.source_doc_id IS NOT NULL AND ce.source_chunk_id IS NOT NULL AND ce.page IS NOT NULL
  AND ie.source_doc_id IS NOT NULL AND ie.source_chunk_id IS NOT NULL AND ie.page IS NOT NULL
RETURN c.name AS company, person.name AS executive, institution.name AS institution,
       ce.source_doc_id AS executive_source_doc_id,
       ce.source_chunk_id AS executive_source_chunk_id,
       ce.page AS executive_page,
       ie.source_doc_id AS institution_source_doc_id,
       ie.source_chunk_id AS institution_source_chunk_id,
       ie.page AS institution_page
LIMIT $limit
""".strip()
        return QueryPlan(intent, 3, cypher, {"company_a": company_a, "limit": limit})
    if intent == "metric_period_compare":
        metric = _required(metric, "metric")
        clean_periods = [str(period).strip() for period in (periods or []) if str(period).strip()]
        if len(clean_periods) < 2:
            raise ValueError("at least two periods are required")
        cypher = """
MATCH (c:Company)<-[:ABOUT_COMPANY]-(m:FinancialMetric)<-[:HAS_METRIC]-(r:Report)
WHERE (c.name = $company_a OR c.ticker = $company_a OR $company_a IN coalesce(c.aliases, []))
  AND m.name = $metric AND m.period IN $periods
RETURN c.name AS company, m.name AS metric, m.period AS period, m.value AS value,
       m.unit AS unit, r.doc_id AS source_doc_id, m.source_chunk_id AS source_chunk_id, m.page AS page
ORDER BY m.period, r.publish_date DESC
LIMIT $limit
""".strip()
        return QueryPlan(intent, 3, cypher, {"company_a": company_a, "metric": metric, "periods": clean_periods, "limit": limit})
    raise ValueError(f"unsupported intent: {intent}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or execute a safe AlphaMind Phase3 multi-hop Cypher plan.")
    parser.add_argument("intent", choices=("supply_chain_overlap", "institution_executive_network", "metric_period_compare"))
    parser.add_argument("--company-a", required=True)
    parser.add_argument("--company-b", default="")
    parser.add_argument("--metric", default="")
    parser.add_argument("--period", action="append", default=[])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--execute", action="store_true", help="Execute the allow-listed parameterized plan against Neo4j.")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[1] / ".env.phase3-precision"))
    parser.add_argument("--neo4j-url", default="")
    parser.add_argument("--neo4j-user", default="")
    parser.add_argument("--neo4j-password", default="")
    parser.add_argument("--neo4j-database", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = build_query_plan(args.intent, company_a=args.company_a, company_b=args.company_b, metric=args.metric, periods=args.period, limit=args.limit)
    if not args.execute:
        print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
        return 0
    env = parse_env_file(Path(args.env_file))
    result = execute_plan(
        plan,
        neo4j_url=args.neo4j_url or os.environ.get("NEO4J_HTTP_URL") or env.get("NEO4J_HTTP_URL") or f"http://localhost:{env.get('NEO4J_HTTP_PORT', '7474')}",
        username=args.neo4j_user or os.environ.get("NEO4J_USERNAME") or env.get("NEO4J_USERNAME", "neo4j"),
        password=args.neo4j_password or os.environ.get("NEO4J_PASSWORD") or env.get("NEO4J_PASSWORD", ""),
        database=args.neo4j_database or os.environ.get("NEO4J_DATABASE") or env.get("NEO4J_DATABASE", "neo4j"),
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
