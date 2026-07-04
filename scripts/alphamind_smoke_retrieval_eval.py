#!/usr/bin/env python3
"""
Offline AlphaMind smoke retrieval evaluator.

This intentionally uses only the Python standard library. It validates that the
smoke query set and ground-truth file are aligned with the local parsed corpus
before running heavier WeKnora / rerank experiments.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERIES = PROJECT_ROOT / "dataset" / "alphamind_eval_queries.json"
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "dataset" / "alphamind_ground_truth_template.json"
DEFAULT_CORPUS_ROOT = PROJECT_ROOT / "数据" / "数据库" / "Documents"
DEFAULT_OUT = PROJECT_ROOT / "dataset" / "alphamind_smoke_retrieval_eval_latest.json"
DEFAULT_EXTENSIONS = (".txt", ".csv")

ASCII_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*%?", re.IGNORECASE)
CJK_RE = re.compile(r"[一-鿿]+")
SPACE_RE = re.compile(r"\s+")


@dataclass
class Document:
    doc_id: str
    path: str
    text: str
    tokens: Counter[str]


@dataclass
class QueryResult:
    query_id: str
    query: str
    relevant_count: int
    retrieved_count: int
    metrics: dict[str, float]
    retrieved_docs: list[tuple[str, float]]
    relevant_docs: list[str]
    must_contain_coverage: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AlphaMind smoke retrieval against local parsed docs.")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    parser.add_argument("--corpus-root", default=str(DEFAULT_CORPUS_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--extensions", default=",".join(DEFAULT_EXTENSIONS), help="Comma-separated extensions to index.")
    parser.add_argument("--no-filter-expansion", action="store_true", help="Do not append expected_filters to query text.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON result to stdout.")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_text(text: str) -> str:
    return SPACE_RE.sub(" ", text.lower()).strip()


def cjk_ngrams(segment: str) -> list[str]:
    if len(segment) <= 1:
        return [segment] if segment else []
    tokens: list[str] = []
    for n in (2, 3, 4):
        if len(segment) >= n:
            tokens.extend(segment[i : i + n] for i in range(len(segment) - n + 1))
    return tokens


def tokenize(text: str) -> list[str]:
    normalized = normalize_text(text)
    tokens = [m.group(0).lower() for m in ASCII_RE.finditer(normalized)]
    for match in CJK_RE.finditer(normalized):
        tokens.extend(cjk_ngrams(match.group(0)))
    return tokens


def flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(flatten_values(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(flatten_values(item))
        return result
    return [str(value)]


def iter_corpus_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
    return sorted(files, key=lambda p: p.name)


def read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def build_documents(root: Path, extensions: tuple[str, ...]) -> list[Document]:
    documents: list[Document] = []
    for path in iter_corpus_files(root, extensions):
        doc_id = path.stem
        text = read_text(path)
        # Title carries broker/company/report metadata that OCR text often loses.
        weighted_text = "\n".join([doc_id] * 4 + [text])
        documents.append(Document(doc_id=doc_id, path=str(path), text=f"{doc_id}\n{text}", tokens=Counter(tokenize(weighted_text))))
    return documents


def idf_by_token(documents: list[Document]) -> dict[str, float]:
    doc_freq: Counter[str] = Counter()
    for doc in documents:
        doc_freq.update(doc.tokens.keys())
    total_docs = max(len(documents), 1)
    return {token: math.log((total_docs + 1) / (df + 0.5)) + 1.0 for token, df in doc_freq.items()}


def query_tokens(query: dict[str, Any], *, use_filter_expansion: bool) -> Counter[str]:
    pieces = [str(query.get("text", ""))]
    if use_filter_expansion:
        pieces.extend(flatten_values(query.get("expected_filters", {})))
    return Counter(tokenize(" ".join(pieces)))


def score_documents(query_tf: Counter[str], documents: list[Document], idf: dict[str, float]) -> list[tuple[Document, float]]:
    scored: list[tuple[Document, float]] = []
    for doc in documents:
        score = 0.0
        doc_len = max(sum(doc.tokens.values()), 1)
        for token, q_count in query_tf.items():
            tf = doc.tokens.get(token, 0)
            if not tf:
                continue
            score += (1.0 + math.log(q_count)) * (1.0 + math.log(tf)) * idf.get(token, 1.0)
        # Light length normalization to avoid long reports dominating solely by size.
        score = score / math.sqrt(doc_len / 1000.0)
        if score > 0:
            scored.append((doc, score))
    scored.sort(key=lambda item: (-item[1], item[0].doc_id))
    return scored


def precision(retrieved: list[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = retrieved[:k]
    return sum(1 for doc_id in top if doc_id in relevant) / k


def recall(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = retrieved[:k]
    return sum(1 for doc_id in top if doc_id in relevant) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for index, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / index
    return 0.0


def ndcg(retrieved: list[str], relevant: set[str], k: int) -> float:
    dcg = 0.0
    for index, doc_id in enumerate(retrieved[:k], start=1):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(index + 1)
    ideal_hits = min(len(relevant), k)
    if ideal_hits == 0:
        return 0.0
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / ideal


def must_contain_coverage(entries: list[dict[str, Any]], by_doc_id: dict[str, Document]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in entries:
        doc_id = str(entry.get("doc_id", ""))
        doc = by_doc_id.get(doc_id)
        expected = [str(item) for item in entry.get("must_contain", [])]
        if not doc:
            result[doc_id] = {"status": "missing_doc", "matched": [], "missing": expected}
            continue
        normalized_doc = normalize_text(doc.text)
        matched = [item for item in expected if normalize_text(item) in normalized_doc]
        missing = [item for item in expected if item not in matched]
        result[doc_id] = {
            "status": "pass" if not missing else "warn",
            "matched": matched,
            "missing": missing,
            "coverage": len(matched) / len(expected) if expected else 1.0,
        }
    return result


def evaluate(
    queries_payload: dict[str, Any],
    ground_truth_payload: dict[str, Any],
    documents: list[Document],
    *,
    top_k: int,
    use_filter_expansion: bool,
) -> dict[str, Any]:
    idf = idf_by_token(documents)
    by_doc_id = {doc.doc_id: doc for doc in documents}
    ground_truth = ground_truth_payload.get("ground_truth", {})
    query_results: list[QueryResult] = []

    for query in queries_payload.get("queries", []):
        query_id = str(query.get("id", ""))
        relevant_entries = ground_truth.get(query_id, [])
        relevant_docs = [str(entry.get("doc_id", "")) for entry in relevant_entries if entry.get("doc_id")]
        relevant_set = set(relevant_docs)
        q_tf = query_tokens(query, use_filter_expansion=use_filter_expansion)
        scored = score_documents(q_tf, documents, idf)[:top_k]
        retrieved_ids = [doc.doc_id for doc, _score in scored]
        metrics = {
            "precision@1": precision(retrieved_ids, relevant_set, 1),
            "recall@1": recall(retrieved_ids, relevant_set, 1),
            "precision@3": precision(retrieved_ids, relevant_set, 3),
            "recall@3": recall(retrieved_ids, relevant_set, 3),
            "precision@5": precision(retrieved_ids, relevant_set, 5),
            "recall@5": recall(retrieved_ids, relevant_set, 5),
            "precision@10": precision(retrieved_ids, relevant_set, 10),
            "recall@10": recall(retrieved_ids, relevant_set, 10),
            "reciprocal_rank": reciprocal_rank(retrieved_ids, relevant_set),
            "ndcg@5": ndcg(retrieved_ids, relevant_set, 5),
            "ndcg@10": ndcg(retrieved_ids, relevant_set, 10),
        }
        query_results.append(
            QueryResult(
                query_id=query_id,
                query=str(query.get("text", "")),
                relevant_count=len(relevant_set),
                retrieved_count=len(scored),
                metrics=metrics,
                retrieved_docs=[(doc.doc_id, round(score, 6)) for doc, score in scored],
                relevant_docs=relevant_docs,
                must_contain_coverage=must_contain_coverage(relevant_entries, by_doc_id),
            )
        )

    aggregate: dict[str, float] = {}
    metric_names = [
        "precision@1",
        "recall@1",
        "precision@3",
        "recall@3",
        "precision@5",
        "recall@5",
        "precision@10",
        "recall@10",
        "reciprocal_rank",
        "ndcg@5",
        "ndcg@10",
    ]
    for name in metric_names:
        values = [result.metrics[name] for result in query_results]
        aggregate[f"mean_{name}"] = sum(values) / len(values) if values else 0.0

    missing_ground_truth = [result.query_id for result in query_results if not result.relevant_docs]
    missing_docs = sorted({doc_id for result in query_results for doc_id in result.relevant_docs if doc_id not in by_doc_id})
    missing_must_contain = {
        result.query_id: {
            doc_id: coverage["missing"]
            for doc_id, coverage in result.must_contain_coverage.items()
            if coverage.get("missing")
        }
        for result in query_results
    }
    missing_must_contain = {query_id: docs for query_id, docs in missing_must_contain.items() if docs}
    validation_status = "pass" if not missing_ground_truth and not missing_docs and not missing_must_contain else "fail"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "root": str(DEFAULT_CORPUS_ROOT),
            "document_count": len(documents),
            "doc_ids": [doc.doc_id for doc in documents],
        },
        "settings": {
            "top_k": top_k,
            "use_filter_expansion": use_filter_expansion,
            "tokenizer": "ascii tokens + CJK 2/3/4-grams; title weighted x4",
        },
        "aggregate_metrics": aggregate,
        "query_results": [asdict(result) for result in query_results],
        "validation": {
            "missing_ground_truth": missing_ground_truth,
            "missing_docs": missing_docs,
            "missing_must_contain": missing_must_contain,
            "status": validation_status,
        },
    }


def main() -> int:
    args = parse_args()
    queries_path = Path(args.queries)
    ground_truth_path = Path(args.ground_truth)
    corpus_root = Path(args.corpus_root)
    out_path = Path(args.out)
    extensions = tuple(ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}" for ext in args.extensions.split(",") if ext.strip())

    documents = build_documents(corpus_root, extensions)
    if not documents:
        print(f"No corpus files found in {corpus_root} for extensions {extensions}", file=sys.stderr)
        return 2

    result = evaluate(
        load_json(queries_path),
        load_json(ground_truth_path),
        documents,
        top_k=args.top_k,
        use_filter_expansion=not args.no_filter_expansion,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        metrics = result["aggregate_metrics"]
        validation = result["validation"]
        print("AlphaMind smoke retrieval eval")
        print("=" * 80)
        print(f"corpus_docs={result['corpus']['document_count']} validation={validation['status']} out={out_path}")
        print(
            "Precision@1={:.3f} Precision@5={:.3f} Recall@5={:.3f} Recall@10={:.3f} MRR={:.3f} NDCG@5={:.3f}".format(
                metrics["mean_precision@1"],
                metrics["mean_precision@5"],
                metrics["mean_recall@5"],
                metrics["mean_recall@10"],
                metrics["mean_reciprocal_rank"],
                metrics["mean_ndcg@5"],
            )
        )
        if validation["missing_ground_truth"]:
            print(f"missing_ground_truth={validation['missing_ground_truth']}")
        if validation["missing_docs"]:
            print(f"missing_docs={validation['missing_docs']}")
        if validation["missing_must_contain"]:
            print(f"missing_must_contain={validation['missing_must_contain']}")
        for item in result["query_results"]:
            top = item["retrieved_docs"][0][0] if item["retrieved_docs"] else "NONE"
            print(f"{item['query_id']}: p@1={item['metrics']['precision@1']:.0f} rr={item['metrics']['reciprocal_rank']:.3f} top={top}")
    return 0 if result["validation"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
