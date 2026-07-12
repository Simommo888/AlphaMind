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
