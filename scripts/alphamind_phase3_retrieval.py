#!/usr/bin/env python3
"""Financial lexicon query expansion and weighted RRF fusion for AlphaMind Phase3."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_lexicon(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("lexicon must be a list or an object containing entries")
    return [entry for entry in entries if isinstance(entry, dict)]


def expand_query(query: str, lexicon: list[dict[str, Any]], max_expansions: int = 8) -> list[str]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    if max_expansions < 0:
        raise ValueError("max_expansions must be non-negative")
    lowered = query.casefold()
    raw_matches: list[tuple[int, int, int, dict[str, Any]]] = []
    for entry in lexicon:
        canonical = str(entry.get("canonical") or "").strip()
        terms = [str(term).strip() for term in entry.get("terms", []) if str(term).strip()]
        occurrences: list[tuple[int, int, int]] = []
        for candidate in [canonical, *terms]:
            if not candidate:
                continue
            start = lowered.find(candidate.casefold())
            if start >= 0:
                occurrences.append((len(candidate), start, start + len(candidate)))
        if occurrences:
            length, start, end = max(occurrences)
            raw_matches.append((length, start, end, entry))
    # Prefer the most specific financial phrase. For example, a query containing
    # “扣非净利润” must not also trigger the broader “净利润” entry merely
    # because the latter is a substring of the former.
    matches = [
        (length, entry)
        for length, start, end, entry in raw_matches
        if not any(
            other_length > length and other_start <= start and end <= other_end
            for other_length, other_start, other_end, _ in raw_matches
        )
    ]
    matches.sort(key=lambda item: (-item[0], str(item[1].get("canonical") or "")))
    result = [query]
    seen = {query.casefold()}
    for _, entry in matches:
        candidates = [str(entry.get("canonical") or "").strip(), *[str(term).strip() for term in entry.get("terms", [])]]
        for candidate in candidates:
            key = candidate.casefold()
            if candidate and key not in seen:
                result.append(candidate)
                seen.add(key)
                if len(result) - 1 >= max_expansions:
                    return result
    return result


def weighted_rrf(
    channels: dict[str, list[dict[str, Any]]],
    weights: dict[str, float],
    *,
    k: int = 60,
    id_key: str = "id",
) -> list[dict[str, Any]]:
    if k <= 0:
        raise ValueError("k must be positive")
    if any(float(weight) < 0 for weight in weights.values()):
        raise ValueError("weights must be non-negative")
    scores: dict[str, float] = {}
    representatives: dict[str, dict[str, Any]] = {}
    memberships: dict[str, list[str]] = {}
    for channel, items in channels.items():
        weight = float(weights.get(channel, 0.0))
        if weight <= 0:
            continue
        channel_seen: set[str] = set()
        for rank, item in enumerate(items, 1):
            identifier = str(item.get(id_key) or item.get("doc_id") or item.get("chunk_id") or "").strip()
            if not identifier or identifier in channel_seen:
                continue
            channel_seen.add(identifier)
            scores[identifier] = scores.get(identifier, 0.0) + weight / (k + rank)
            representatives.setdefault(identifier, dict(item))
            memberships.setdefault(identifier, []).append(channel)
    result = []
    for identifier, score in scores.items():
        item = representatives[identifier]
        item[id_key] = identifier
        item["fusion_score"] = score
        item["fusion_channels"] = memberships[identifier]
        result.append(item)
    return sorted(result, key=lambda item: (-float(item["fusion_score"]), str(item[id_key])))


def _normalize_items(items: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        identifier = str(item.get(id_key) or item.get("id") or item.get("chunk_id") or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        item[id_key] = identifier
        item.setdefault("id", identifier)
        item.setdefault("chunk_id", identifier)
        normalized.append(item)
    return normalized


def financial_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int,
    *,
    exact_terms: list[str],
    exact_boost: float,
    graph_boost: float,
) -> list[dict[str, Any]]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    terms = [str(term).casefold() for term in exact_terms if str(term).strip()]
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        content = str(item.get("content") or "").casefold()
        term_hits = sum(1 for term in terms if term in content)
        score = float(item.get("fusion_score") or 0.0)
        score += float(exact_boost) * term_hits
        if "graph" in item.get("fusion_channels", []):
            score += float(graph_boost)
        item["rerank_score"] = score
        item["rerank_mode"] = "financial_exact_graph"
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-float(item["rerank_score"]), str(item.get("id") or item.get("chunk_id") or "")))[:top_k]


def execute_retrieval(
    query: str,
    config: dict[str, Any],
    lexicon: list[dict[str, Any]],
    search_channel,
    graph_ranker,
    reranker,
) -> list[dict[str, Any]]:
    """Execute expansion, distinct dense/sparse retrieval, graph ranking, weighted RRF and rerank."""
    expansion_cfg = config.get("query_expansion", {})
    expanded = (
        expand_query(query, lexicon, int(expansion_cfg.get("max_expansions", 8)))
        if expansion_cfg.get("enabled", False)
        else [str(query).strip()]
    )
    fusion = config.get("fusion", {})
    weights = {str(key): float(value) for key, value in fusion.get("weights", {}).items()}
    if set(weights) != {"dense", "sparse", "graph"}:
        raise ValueError("retrieval requires dense, sparse and graph fusion weights")
    id_key = str(fusion.get("deduplicate_by") or "chunk_id")
    dense = _normalize_items(search_channel(query, "dense"), id_key)
    sparse_raw: list[dict[str, Any]] = []
    for expanded_query in expanded:
        sparse_raw.extend(search_channel(expanded_query, "sparse"))
    sparse = _normalize_items(sparse_raw, id_key)
    preliminary = weighted_rrf(
        {"dense": dense, "sparse": sparse},
        {"dense": weights["dense"], "sparse": weights["sparse"]},
        k=int(fusion.get("rrf_k", 60)),
        id_key=id_key,
    )
    if weights["graph"] > 0 and graph_ranker is None:
        raise ValueError("graph fusion weight is positive but graph_ranker is unavailable")
    graph = _normalize_items(graph_ranker(expanded, preliminary) if graph_ranker else [], id_key)
    fused = weighted_rrf(
        {"dense": dense, "sparse": sparse, "graph": graph},
        weights,
        k=int(fusion.get("rrf_k", 60)),
        id_key=id_key,
    )
    rerank_cfg = config.get("rerank", {})
    if rerank_cfg.get("require_source_lineage"):
        fused = [
            item for item in fused
            if isinstance(item.get("metadata"), dict) and item["metadata"].get("doc_id")
        ]
    if rerank_cfg.get("enabled"):
        if reranker is None:
            raise ValueError("rerank is enabled but reranker is unavailable")
        fused = reranker(query, fused, int(rerank_cfg.get("top_k") or config.get("rerank_top_k") or 30))
    return fused[: int(rerank_cfg.get("top_k") or config.get("rerank_top_k") or len(fused) or 1)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expand a financial query using the AlphaMind Phase3 lexicon.")
    parser.add_argument("query")
    parser.add_argument("--lexicon", default="dataset/alphamind_financial_lexicon_phase3.json")
    parser.add_argument("--max-expansions", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expanded = expand_query(args.query, load_lexicon(args.lexicon), args.max_expansions)
    print(json.dumps({"query": args.query, "expanded": expanded}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
