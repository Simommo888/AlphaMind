#!/usr/bin/env python3
"""
Local /rerank adapter for AlphaMind.

WeKnora's generic reranker calls POST {base_url}/rerank. Some OpenAI-compatible
model gateways only expose /embeddings and do not provide a native /rerank
endpoint. This lightweight proxy bridges that gap by scoring query/document
pairs with the configured embedding model and returning a rerank-compatible
response.

This is a pragmatic fallback for local PoC. For production投研 RAG, prefer a
true cross-encoder / dedicated rerank model and keep retrieval metrics as the
final judge.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_value(env_file_values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env_file_values.get(key) or default


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class RerankProxy:
    def __init__(self, env_file: Path) -> None:
        self.env_file_values = load_env_file(env_file)
        self.embedding_base_url = env_value(self.env_file_values, "EMBEDDING_BASE_URL").rstrip("/")
        self.embedding_api_key = env_value(self.env_file_values, "EMBEDDING_API_KEY")
        self.embedding_model = env_value(self.env_file_values, "EMBEDDING_MODEL_NAME")
        self.chat_base_url = env_value(self.env_file_values, "LLM_BASE_URL").rstrip("/")
        self.chat_api_key = env_value(self.env_file_values, "LLM_API_KEY")
        self.chat_model = env_value(self.env_file_values, "LLM_MODEL_NAME")
        self.proxy_api_key = env_value(self.env_file_values, "RERANK_PROXY_API_KEY")
        self.max_chars = int(env_value(self.env_file_values, "ALPHAMIND_RERANK_MAX_CHARS", "3000"))
        self.fallback_mode = env_value(self.env_file_values, "ALPHAMIND_RERANK_FALLBACK", "keyword").lower()
        if not self.embedding_base_url:
            raise RuntimeError("EMBEDDING_BASE_URL is required")
        if not self.embedding_api_key:
            raise RuntimeError("EMBEDDING_API_KEY is required")
        if not self.embedding_model:
            raise RuntimeError("EMBEDDING_MODEL_NAME is required")

    def check_auth(self, headers: Any) -> bool:
        if not self.proxy_api_key:
            return True
        auth = headers.get("Authorization", "")
        return auth == f"Bearer {self.proxy_api_key}"

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        payload = {
            "model": self.embedding_model,
            "input": [text[: self.max_chars] for text in texts],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.embedding_base_url + "/embeddings",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.embedding_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"embedding API HTTP {exc.code}: {err_body}") from exc
        payload = json.loads(body)
        rows = payload.get("data") or []
        rows = sorted(rows, key=lambda item: item.get("index", 0))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
            raise RuntimeError("embedding API returned unexpected data shape")
        total_tokens = int((payload.get("usage") or {}).get("total_tokens") or 0)
        return vectors, total_tokens

    @staticmethod
    def keyword_tokens(text: str) -> list[str]:
        return re.findall(r"[一-鿿]|[A-Za-z0-9_.]+", text.lower())

    def keyword_results(self, query: str, documents: list[str]) -> list[dict[str, Any]]:
        query_tokens = self.keyword_tokens(query)
        query_set = set(query_tokens)
        results = []
        for index, document in enumerate(documents):
            doc_tokens = self.keyword_tokens(document[: self.max_chars])
            doc_set = set(doc_tokens)
            overlap = len(query_set & doc_set)
            coverage = overlap / max(len(query_set), 1)
            density = overlap / max(len(doc_set), 1)
            exact_bonus = 0.2 if query.lower() in document.lower() else 0.0
            score = min(1.0, coverage * 0.75 + density * 0.25 + exact_bonus)
            results.append(
                {
                    "index": index,
                    "relevance_score": score,
                    "document": {"text": document},
                }
            )
        results.sort(key=lambda item: item["relevance_score"], reverse=True)
        return results

    def rerank(self, request: dict[str, Any]) -> dict[str, Any]:
        query = request.get("query")
        documents = request.get("documents")
        model = request.get("model") or "alphamind-embedding-rerank"
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(documents, list) or not all(isinstance(doc, str) for doc in documents):
            raise ValueError("documents must be a list of strings")
        if not documents:
            return {
                "id": f"alphamind-rerank-{int(time.time() * 1000)}",
                "model": model,
                "usage": {"total_tokens": 0},
                "results": [],
            }

        total_tokens = 0
        try:
            vectors, total_tokens = self.embed([query] + documents)
            query_vector = vectors[0]
            results = []
            for index, vector in enumerate(vectors[1:]):
                score = cosine(query_vector, vector)
                results.append(
                    {
                        "index": index,
                        "relevance_score": score,
                        "document": {"text": documents[index]},
                    }
                )
            results.sort(key=lambda item: item["relevance_score"], reverse=True)
        except Exception as exc:
            if self.fallback_mode != "keyword":
                raise
            print(f"embedding rerank failed, falling back to keyword scoring: {exc}", file=sys.stderr, flush=True)
            results = self.keyword_results(query, documents)
        return {
            "id": f"alphamind-rerank-{int(time.time() * 1000)}",
            "model": model,
            "usage": {"total_tokens": total_tokens},
            "results": results,
        }


def make_handler(proxy: RerankProxy):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AlphaMindRerankProxy/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                self.send_json(200, {"status": "ok", "model": proxy.embedding_model})
                return
            self.send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/rerank":
                self.send_json(404, {"error": "not found"})
                return
            if not proxy.check_auth(self.headers):
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                request = json.loads(body) if body else {}
                response = proxy.rerank(request)
                self.send_json(200, response)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind local /rerank proxy backed by embeddings.")
    parser.add_argument("--host", default=os.environ.get("RERANK_PROXY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RERANK_PROXY_PORT", "18081")))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    proxy = RerankProxy(Path(args.env_file))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(proxy))
    print(f"AlphaMind rerank proxy listening on {args.host}:{args.port}", flush=True)
    print(f"Using embedding model: {proxy.embedding_model}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
