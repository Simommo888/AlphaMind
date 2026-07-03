#!/usr/bin/env python3
"""
Local OpenAI-compatible embeddings proxy for AlphaMind smoke tests.

Use only as a temporary fallback when the configured embedding gateway is
unavailable. It returns deterministic hash-based vectors so the WeKnora parsing,
chunking, Qdrant write, rerank wiring, and GraphRAG pipeline can be validated
end-to-end. It is not a semantic embedding model and must not be used for final
retrieval-quality evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
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


def tokenize(text: str) -> list[str]:
    # Keep Chinese characters and alphanumeric financial tokens. This is simple
    # but stable enough for smoke-testing storage/retrieval plumbing.
    return re.findall(r"[一-鿿]|[A-Za-z0-9_.%+-]+", text.lower())


def hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = tokenize(text)
    if not tokens:
        tokens = [text[:128] or "empty"]
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8", errors="ignore"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        # Slightly weight longer tokens, but cap so boilerplate cannot dominate.
        weight = min(2.0, 1.0 + len(token) / 20.0)
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    return vector


class EmbeddingProxy:
    def __init__(self, env_file: Path, dimensions: int) -> None:
        self.env_file_values = load_env_file(env_file)
        self.model = env_value(self.env_file_values, "EMBEDDING_MODEL_NAME", "alphamind-local-hash-embedding")
        self.api_key = env_value(self.env_file_values, "EMBEDDING_PROXY_API_KEY") or env_value(
            self.env_file_values, "EMBEDDING_API_KEY"
        )
        self.dimensions = dimensions

    def check_auth(self, headers: Any) -> bool:
        if not self.api_key:
            return True
        auth = headers.get("Authorization", "")
        return auth == f"Bearer {self.api_key}"

    def embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        inputs = payload.get("input", "")
        model = payload.get("model") or self.model
        if isinstance(inputs, str):
            texts = [inputs]
        elif isinstance(inputs, list) and all(isinstance(item, str) for item in inputs):
            texts = inputs
        else:
            raise ValueError("input must be a string or list of strings")
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": hash_embedding(text, self.dimensions),
            }
            for index, text in enumerate(texts)
        ]
        approx_tokens = sum(max(1, len(tokenize(text))) for text in texts)
        return {
            "object": "list",
            "model": model,
            "data": data,
            "usage": {"prompt_tokens": approx_tokens, "total_tokens": approx_tokens},
        }


def make_handler(proxy: EmbeddingProxy):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AlphaMindEmbeddingProxy/1.0"

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
            path = self.path.rstrip("/")
            if path in {"/health", "/v1/health"}:
                self.send_json(200, {"status": "ok", "model": proxy.model, "dimensions": proxy.dimensions})
                return
            if path in {"/models", "/v1/models"}:
                self.send_json(200, {"object": "list", "data": [{"id": proxy.model, "object": "model"}]})
                return
            self.send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.rstrip("/")
            if path not in {"/embeddings", "/v1/embeddings"}:
                self.send_json(404, {"error": "not found"})
                return
            if not proxy.check_auth(self.headers):
                self.send_json(401, {"error": {"message": "unauthorized", "type": "auth_error"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(body) if body else {}
                self.send_json(200, proxy.embeddings(payload))
            except ValueError as exc:
                self.send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            except Exception as exc:
                self.send_json(500, {"error": {"message": str(exc), "type": "api_error"}})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind local OpenAI-compatible embeddings proxy.")
    parser.add_argument("--host", default=os.environ.get("EMBEDDING_PROXY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EMBEDDING_PROXY_PORT", "18082")))
    parser.add_argument("--dimensions", type=int, default=int(os.environ.get("EMBEDDING_PROXY_DIMENSIONS", "3072")))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    proxy = EmbeddingProxy(Path(args.env_file), args.dimensions)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(proxy))
    print(f"AlphaMind embedding proxy listening on {args.host}:{args.port}", flush=True)
    print(f"Model: {proxy.model}; dimensions: {proxy.dimensions}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
