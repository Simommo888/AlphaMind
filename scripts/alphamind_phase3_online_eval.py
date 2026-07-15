#!/usr/bin/env python3
"""Strict Phase3 online retrieval evaluation with real model reranking."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alphamind_phase2_quality_eval as base
from alphamind_phase3_acceptance import (
    RerankAudit,
    _post_bearer_json,
    build_chat_completion_payload,
    build_direct_citation_messages,
    build_qwen_reranker,
)

DEFAULT_ENV = PROJECT_ROOT / ".env.phase3-precision"
DEFAULT_QUERIES = PROJECT_ROOT / "dataset" / "phase3_eval_queries.json"
DEFAULT_RETRIEVAL = PROJECT_ROOT / "dataset" / "alphamind_retrieval_config_phase3_precision.json"
DEFAULT_KB_ID = PROJECT_ROOT / "dataset" / "alphamind_phase3_precision_kb_id.txt"
DEFAULT_OUT = PROJECT_ROOT / "dataset" / "phase3_runs" / "phase3_online_eval_latest.json"


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def unload_shared_model(env: dict[str, str], timeout: float) -> dict[str, Any]:
    url = str(env.get("PHASE3_RERANK_BASE_URL") or env.get("RERANK_BASE_URL") or "").strip()
    if "host.docker.internal" in url:
        url = url.replace("host.docker.internal", "localhost")
    if not url:
        raise RuntimeError("shared model base URL is required for GPU orchestration")
    status, payload = _post_bearer_json(
        url.rstrip("/") + "/unload",
        str(env.get("RERANK_API_KEY") or "local-no-auth"),
        timeout,
        {},
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"shared model unload HTTP {status}: {str(payload)[:200]}")
    return {"status": "pass", "endpoint": url.rstrip("/") + "/unload", "http_status": status}


def chat_runtime(env: dict[str, str]) -> str:
    configured = str(env.get("PHASE3_DIRECT_CHAT_RUNTIME") or env.get("PHASE3_CHAT_RUNTIME") or "").strip().lower()
    if configured:
        return configured
    url = str(env.get("PHASE3_DIRECT_CHAT_URL") or env.get("LLM_BASE_URL") or "").strip()
    if "11434" in url:
        return "ollama"
    return "openai"


def unload_chat_model(env: dict[str, str], timeout: float) -> dict[str, Any]:
    runtime = chat_runtime(env)
    model = str(env.get("PHASE3_DIRECT_CHAT_MODEL") or env.get("LLM_MODEL_NAME") or "").strip()
    if not model:
        raise RuntimeError("LLM model is required for GPU orchestration")
    if runtime != "ollama":
        # vLLM/OpenAI-compatible servers have no portable per-model unload API.
        # Customer target hosts run qwen3-14b as a long-lived vLLM service, so the
        # acceptance script records the boundary instead of calling Ollama.
        return {"status": "pass", "runtime": runtime, "model": model, "action": "skip_unload"}
    payload = json.dumps({"model": model, "keep_alive": 0}, ensure_ascii=False).encode("utf-8")
    request = Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        raise RuntimeError(f"Ollama unload HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Ollama unload failed: {exc}") from exc
    if status < 200 or status >= 300:
        raise RuntimeError(f"Ollama unload HTTP {status}")
    return {"status": "pass", "runtime": runtime, "model": model, "http_status": status}


def evaluate_direct_chat(
    *,
    query: dict[str, Any],
    entry: base.GroundTruthEntry,
    candidates: list[dict[str, Any]],
    env: dict[str, str],
    knowledge_to_doc: dict[str, str],
    alias_to_doc: dict[str, str],
    timeout: float,
    include_answer: bool,
) -> dict[str, Any]:
    start = time.perf_counter()
    chat_url = str(env.get("PHASE3_DIRECT_CHAT_URL") or "http://127.0.0.1:8000/v1").strip()
    model = str(env.get("PHASE3_DIRECT_CHAT_MODEL") or env.get("LLM_MODEL_NAME") or "").strip()
    if not model:
        return {"status": "error", "http_status": 0, "error": "direct chat model missing", "answer_preview": ""}
    messages = build_direct_citation_messages(str(query.get("text") or ""), candidates[:3])
    messages[0]["content"] += (
        " 若证据不直接支持问题中的公司、年份或指标，必须回答“未检索到足够证据，不能编造”，"
        "不得进行同业替代、未来预测或补充外部知识。"
    )
    status, payload = base.http_json(
        chat_url,
        "chat/completions",
        api_key=str(env.get("LLM_API_KEY") or "local-no-auth"),
        method="POST",
        payload=build_chat_completion_payload(
            model,
            messages,
            runtime=chat_runtime(env),
            disable_thinking=str(env.get("PHASE3_DISABLE_THINKING", "true")).strip().lower() not in {"0", "false", "no", "off"},
        ),
        timeout=timeout,
    )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
    answer = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
    cited_indexes = sorted({
        int(value) for value in re.findall(r"\[S(\d+)\]", answer)
        if 1 <= int(value) <= min(3, len(candidates))
    })
    doc_ids: list[str] = []
    knowledge_ids: list[str] = []
    raw_candidate_doc_ids: list[str] = []
    for item in candidates[:3]:
        doc_id = base.retrieved_doc_id(item, knowledge_to_doc, alias_to_doc)
        if doc_id and doc_id not in raw_candidate_doc_ids:
            raw_candidate_doc_ids.append(doc_id)
    for index in cited_indexes:
        item = candidates[index - 1]
        doc_id = base.retrieved_doc_id(item, knowledge_to_doc, alias_to_doc)
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
        knowledge_id = str(item.get("knowledge_id") or item.get("knowledgeId") or "")
        if knowledge_id and knowledge_id not in knowledge_ids:
            knowledge_ids.append(knowledge_id)
    refusal_terms = ("未检索到足够证据", "证据不足", "无法确认", "不能编造")
    is_refusal = any(term in answer for term in refusal_terms)
    response_ok = 200 <= status < 300 and bool(answer)
    grounding_ok = entry.is_no_evidence or (bool(cited_indexes) and not is_refusal)
    chat_status = "pass" if response_ok and grounding_ok else "error"
    if not response_ok:
        error = f"direct chat HTTP {status}: {str(payload)[:240]}"
    elif not entry.is_no_evidence and is_refusal:
        error = "positive query returned a no-evidence refusal"
    elif not entry.is_no_evidence and not cited_indexes:
        error = "positive query answer has no valid [S1..S3] citation"
    else:
        error = ""
    result = {
        "status": chat_status,
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "event_count": 1 if answer else 0,
        "event_type_summary": {"direct_chat": 1} if answer else {},
        "reference_count": len(doc_ids),
        "reference_doc_ids": doc_ids,
        "distinct_reference_doc_ids": doc_ids,
        "reference_knowledge_ids": knowledge_ids,
        "raw_reference_doc_candidates": raw_candidate_doc_ids,
        "answer_preview": answer[:500],
        "_answer_for_checks": answer,
        "error": error,
        "mode": "direct_rag_chat",
    }
    if include_answer:
        result["answer"] = answer
    return result


def build_business_search(
    *,
    base_url: str,
    api_key: str,
    kb_id: str,
    retrieval_config: dict[str, Any],
    timeout: float,
    match_count: int,
):
    endpoint = f"api/v1/knowledge-bases/{kb_id}/hybrid-search"

    def search(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = {
            "query_text": query,
            "match_count": match_count,
            "vector_threshold": float(retrieval_config.get("vector_threshold") or 0.0),
            "keyword_threshold": float(retrieval_config.get("keyword_threshold") or 0.0),
        }
        started = time.perf_counter()
        status, response = base.http_json(
            base_url,
            endpoint,
            api_key=api_key,
            method="POST",
            payload=payload,
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if status < 200 or status >= 300:
            raise RuntimeError(f"business retrieval HTTP {status}: {str(response)[:300]}")
        items = base.extract_items(response)
        if not items:
            raise RuntimeError("business retrieval returned no result items")
        return items, {
            "endpoint": base_url.rstrip("/") + "/" + endpoint,
            "method": "POST",
            "http_status": status,
            "elapsed_ms": elapsed_ms,
            "payload_sha256": _payload_sha256(payload),
            "raw_candidate_count": len(items),
        }

    return search


def evaluate_reranked_retrieval(
    *,
    query: dict[str, Any],
    entry: base.GroundTruthEntry,
    search,
    reranker,
    knowledge_to_doc: dict[str, str],
    alias_to_doc: dict[str, str],
    top_k: int,
) -> dict[str, Any]:
    query_text = str(query.get("text") or "").strip()
    started = time.perf_counter()
    try:
        candidates, request_evidence = search(query_text)
        ranked = reranker(query_text, candidates, top_k)
        if not ranked or any("rerank_score" not in item for item in ranked):
            raise RuntimeError("model reranker returned no auditable scores")
    except Exception as exc:
        return {
            "status": "error",
            "http_status": 0,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "metrics": {},
            "retrieved_docs": [],
            "rerank": {"mode": "model_api", "status": "fail"},
            "error": f"{type(exc).__name__}: {exc}",
        }

    retrieved_docs, by_doc_items = base.unique_retrieved_docs(ranked, knowledge_to_doc, alias_to_doc)
    retrieved_ids = [doc.doc_id for doc in retrieved_docs]
    relevant = set(entry.relevant_doc_ids)
    metrics: dict[str, float] = {}
    if entry.scorable_retrieval:
        metrics = {
            "precision@1": base.precision(retrieved_ids, relevant, 1),
            "recall@1": base.recall(retrieved_ids, relevant, 1),
            "precision@3": base.precision(retrieved_ids, relevant, 3),
            "recall@3": base.recall(retrieved_ids, relevant, 3),
            "precision@5": base.precision(retrieved_ids, relevant, 5),
            "recall@5": base.recall(retrieved_ids, relevant, 5),
            "precision@10": base.precision(retrieved_ids, relevant, 10),
            "recall@10": base.recall(retrieved_ids, relevant, 10),
            "reciprocal_rank": base.reciprocal_rank(retrieved_ids, relevant),
            "ndcg@5": base.ndcg(retrieved_ids, relevant, 5),
            "ndcg@10": base.ndcg(retrieved_ids, relevant, 10),
        }
    scores = [float(item["rerank_score"]) for item in ranked]
    models = sorted({str(item.get("rerank_model") or "") for item in ranked if item.get("rerank_model")})
    return {
        "status": "pass",
        "http_status": int(request_evidence.get("http_status") or 0),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "request_evidence": request_evidence,
        "candidate_count": len(candidates),
        "reranked_count": len(ranked),
        "_ranked_candidates": ranked,
        "metrics": metrics,
        "retrieved_docs": [asdict(doc) for doc in retrieved_docs],
        "evidence_coverage": base.must_contain_coverage(entry.evidence, by_doc_items, alias_to_doc),
        "rerank": {
            "mode": "model_api",
            "status": "pass",
            "models": models,
            "score_min": min(scores),
            "score_max": max(scores),
            "scored_candidates": len(scores),
        },
        "error": "",
    }


def assess_gate(
    metrics: dict[str, Any],
    policy: dict[str, Any],
    *,
    rerank_calls: int,
    evaluated_queries: int,
    expected_queries: int,
    query_failures: int = 0,
    ground_truth_status: str = "complete",
) -> dict[str, Any]:
    definitions = (
        ("mean_recall@10", "min_recall_at_10"),
        ("mean_reciprocal_rank", "min_mrr"),
        ("mean_citation_doc_recall", "min_citation_doc_recall"),
        ("no_evidence_refusal_rate", "no_evidence_refusal_rate"),
    )
    checks: dict[str, dict[str, Any]] = {}
    for metric_key, policy_key in definitions:
        actual = metrics.get(metric_key)
        target = policy.get(policy_key)
        status = "pass" if actual is not None and target is not None and float(actual) >= float(target) else "fail"
        checks[metric_key] = {"actual": actual, "minimum": target, "status": status}
    checks["all_queries_executed"] = {
        "actual": evaluated_queries,
        "expected": expected_queries,
        "status": "pass" if expected_queries > 0 and evaluated_queries == expected_queries else "fail",
    }
    checks["real_rerank_api"] = {
        "calls": rerank_calls,
        "expected_minimum": expected_queries,
        "status": "pass" if expected_queries > 0 and rerank_calls >= expected_queries else "fail",
    }
    checks["query_failures"] = {
        "actual": query_failures,
        "maximum": 0,
        "status": "pass" if query_failures == 0 else "fail",
    }
    checks["ground_truth_complete"] = {
        "actual": ground_truth_status,
        "expected": "complete",
        "status": "pass" if ground_truth_status == "complete" else "fail",
    }
    status = "pass" if checks and all(check["status"] == "pass" for check in checks.values()) else "fail"
    return {"status": status, "checks": checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the strict AlphaMind Phase3 fixed online evaluation set.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--retrieval-config", default=str(DEFAULT_RETRIEVAL))
    parser.add_argument("--kb-id-file", default=str(DEFAULT_KB_ID))
    parser.add_argument("--kb-id", default="")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--chat-timeout", type=float, default=300.0)
    parser.add_argument("--match-count", type=int, default=10)
    parser.add_argument("--rerank-batch-size", type=int, default=1)
    parser.add_argument("--include-answer", action="store_true")
    return parser.parse_args()


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    env = base.parse_env_file(Path(args.env_file))
    base_url = base.env_value(env, "WEKNORA_BASE_URL", "http://localhost:8080")
    api_key = base.env_value(env, "WEKNORA_API_KEY")
    kb_id = str(args.kb_id or "").strip()
    if not kb_id and Path(args.kb_id_file).is_file():
        kb_id = Path(args.kb_id_file).read_text(encoding="utf-8").strip()
    if not api_key or not kb_id:
        raise RuntimeError("WEKNORA_API_KEY and KB id are required")

    suite = base.load_json(Path(args.queries))
    queries = [item for item in suite.get("queries", []) if isinstance(item, dict)]
    retrieval_config = base.load_json(Path(args.retrieval_config))
    if not queries:
        raise RuntimeError("fixed evaluation suite is empty")

    knowledge_to_doc: dict[str, str] = {}
    doc_to_knowledge: dict[str, str] = {}
    alias_to_doc: dict[str, str] = {}
    base.merge_kb_knowledge_map_from_api(
        knowledge_to_doc,
        doc_to_knowledge,
        alias_to_doc,
        base_url=base_url,
        api_key=api_key,
        kb_id=kb_id,
        timeout=args.timeout,
    )
    entries, validation = base.load_ground_truth_entries(queries, suite, alias_to_doc)
    entries = {query_id: base.canonicalize_entry(entry, alias_to_doc) for query_id, entry in entries.items()}
    validation = base.validate_ground_truth(queries, entries)

    rerank_env = dict(env)
    configured_rerank_url = base.env_value(env, "PHASE3_RERANK_BASE_URL") or base.env_value(env, "RERANK_BASE_URL")
    if "host.docker.internal" in configured_rerank_url:
        configured_rerank_url = configured_rerank_url.replace("host.docker.internal", "localhost")
    rerank_env["PHASE3_RERANK_BASE_URL"] = configured_rerank_url
    audit = RerankAudit()
    reranker = build_qwen_reranker(
        rerank_env,
        timeout=args.timeout,
        batch_size=args.rerank_batch_size,
        audit=audit,
    )
    search = build_business_search(
        base_url=base_url,
        api_key=api_key,
        kb_id=kb_id,
        retrieval_config=retrieval_config,
        timeout=args.timeout,
        match_count=args.match_count,
    )

    # Run explicit retrieval/chat phases and record model boundaries. Ollama can
    # be unloaded per model; vLLM qwen3-14b is treated as a long-lived service.
    orchestration = [unload_chat_model(env, args.chat_timeout)]
    prefetched: dict[str, tuple[list[dict[str, Any]], dict[str, Any]] | Exception] = {}
    for query in queries:
        query_id = str(query.get("id") or "")
        try:
            prefetched[query_id] = search(str(query.get("text") or ""))
        except Exception as exc:
            prefetched[query_id] = exc

    retrievals: dict[str, dict[str, Any]] = {}
    for query in queries:
        query_id = str(query.get("id") or "")
        entry = entries.get(query_id) or base.GroundTruthEntry(query_id=query_id)
        cached = prefetched[query_id]
        if isinstance(cached, Exception):
            retrievals[query_id] = {
                "status": "error",
                "http_status": 0,
                "elapsed_ms": 0,
                "metrics": {},
                "retrieved_docs": [],
                "rerank": {"mode": "model_api", "status": "fail"},
                "error": f"{type(cached).__name__}: {cached}",
            }
        else:
            retrievals[query_id] = evaluate_reranked_retrieval(
                query=query,
                entry=entry,
                search=lambda _query, value=cached: value,
                reranker=reranker,
                knowledge_to_doc=knowledge_to_doc,
                alias_to_doc=alias_to_doc,
                top_k=args.match_count,
            )

    # Rerank must finish before the shared service is unloaded. On Windows the
    # service may be reclaimed under memory pressure once Ollama starts.
    orchestration.append(unload_shared_model(rerank_env, args.timeout))
    chats: dict[str, dict[str, Any]] = {}
    for query in queries:
        query_id = str(query.get("id") or "")
        entry = entries.get(query_id) or base.GroundTruthEntry(query_id=query_id)
        candidates = retrievals[query_id].get("_ranked_candidates", [])
        chats[query_id] = evaluate_direct_chat(
            query=query,
            entry=entry,
            candidates=candidates,
            env=env,
            knowledge_to_doc=knowledge_to_doc,
            alias_to_doc=alias_to_doc,
            timeout=args.chat_timeout,
            include_answer=args.include_answer,
        )
    orchestration.append(unload_chat_model(env, args.chat_timeout))

    query_results: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query.get("id") or "")
        entry = entries.get(query_id) or base.GroundTruthEntry(query_id=query_id)
        retrieval = dict(retrievals[query_id])
        retrieval.pop("_ranked_candidates", None)
        chat = chats[query_id]
        answer_text = str(chat.pop("_answer_for_checks", "") or chat.get("answer") or chat.get("answer_preview") or "")
        citation = base.evaluate_citation(entry, chat)
        answer = base.evaluate_answer(entry, answer_text)
        no_evidence = base.evaluate_no_evidence(entry, answer)
        status = base.query_status(entry, retrieval, chat, citation, answer, no_evidence)
        query_results.append({
            "query_id": query_id,
            "query": str(query.get("text") or ""),
            "type": str(query.get("type") or ""),
            "ground_truth": {
                "gt_status": entry.gt_status,
                "is_no_evidence": entry.is_no_evidence,
                "relevant_doc_ids": entry.relevant_doc_ids,
                "reference_must_include_doc_ids": entry.reference_must_include_doc_ids,
                "min_distinct_doc_ids": entry.min_distinct_doc_ids,
            },
            "retrieval": retrieval,
            "chat": chat,
            "citation_checks": citation,
            "answer_checks": answer,
            "no_evidence_checks": no_evidence,
            "status": status,
            "warnings": validation.warnings.get(query_id, []),
        })
        print(f"{status.upper():<5} {query_id:<36} retrieval={retrieval.get('status')} chat={chat.get('status')}")

    aggregate = base.aggregate_results(query_results)
    failures = sum(1 for result in query_results if result.get("status") != "pass")
    assessment = assess_gate(
        aggregate,
        suite.get("acceptance_policy", {}),
        rerank_calls=audit.calls,
        evaluated_queries=len(query_results),
        expected_queries=len(queries),
        query_failures=failures,
        ground_truth_status=validation.status,
    )
    return {
        "schema_version": "alphamind.phase3.online-eval.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": assessment["status"],
        "weknora": {"base_url": base_url, "kb_id": kb_id, "api_key_present": bool(api_key)},
        "inputs": {"queries": str(Path(args.queries)), "retrieval_config": str(Path(args.retrieval_config))},
        "settings": {"match_count": args.match_count, "rerank_batch_size": args.rerank_batch_size},
        "gpu_model_orchestration": orchestration,
        "rerank_api_evidence": audit.public_dict(),
        "aggregate_metrics": aggregate,
        "gate_assessment": assessment,
        "ground_truth_validation": asdict(validation),
        "query_results": query_results,
    }


def main() -> int:
    args = parse_args()
    try:
        report = evaluate(args)
    except Exception as exc:
        report = {
            "schema_version": "alphamind.phase3.online-eval.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PHASE3 FIXED ONLINE EVAL: {str(report.get('status')).upper()}")
    print(f"report={out}")
    if report.get("aggregate_metrics"):
        metrics = report["aggregate_metrics"]
        print(
            " ".join(
                f"{key}={float(metrics[key]):.3f}"
                for key in ("mean_recall@10", "mean_reciprocal_rank", "mean_citation_doc_recall", "no_evidence_refusal_rate")
                if metrics.get(key) is not None
            )
        )
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
