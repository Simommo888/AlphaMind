#!/usr/bin/env python3
"""
AlphaMind phase1-basic local model API check.

This script verifies the quotation's phase-1 requirement before WeKnora is used:
- Qwen 35B chat model exposes an OpenAI-compatible /v1/chat/completions API.
- Embedding model exposes an OpenAI-compatible /v1/embeddings API and returns the expected dimension.
- Rerank model exposes a compatible rerank-style API.

It intentionally fails on missing/unreachable local endpoints instead of silently
falling back to remote gateways or hash embeddings.
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase1-basic"
LOCAL_HOST_HINTS = ("localhost", "127.0.0.1", "host.docker.internal", "0.0.0.0", "::1")
REMOTE_HOST_HINTS = ("dashscope.aliyuncs.com", "openai.com", "xinshu.ai", "siliconflow", "volces", "bigmodel.cn")


@dataclass
class CheckResult:
    name: str
    status: str  # pass / fail / warn / skip
    detail: str
    elapsed_ms: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check AlphaMind phase1-basic local model APIs.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", help="Print JSON result list.")
    parser.add_argument("--skip-chat", action="store_true", help="Skip chat completion check.")
    parser.add_argument("--skip-embedding", action="store_true", help="Skip embedding check.")
    parser.add_argument("--skip-rerank", action="store_true", help="Skip rerank check.")
    return parser.parse_args()


def parse_env_file(path: Path) -> dict[str, str]:
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


def env_value(env_values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env_values.get(key) or default


def bearer_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def http_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any] | list[Any] | str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                return int(getattr(response, "status", 200)), json.loads(text) if text else {}
            except json.JSONDecodeError:
                return int(getattr(response, "status", 200)), text
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return exc.code, text
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def timed(name: str, func) -> CheckResult:
    start = time.perf_counter()
    try:
        status, detail = func()
        return CheckResult(name, status, detail, int((time.perf_counter() - start) * 1000))
    except Exception as exc:
        return CheckResult(name, "fail", f"{type(exc).__name__}: {exc}", int((time.perf_counter() - start) * 1000))


def endpoint_guard(name: str, url: str) -> CheckResult | None:
    if not url:
        return CheckResult(name, "fail", "missing endpoint URL")
    lowered = url.lower()
    if any(marker in lowered for marker in REMOTE_HOST_HINTS):
        return CheckResult(name, "fail", f"endpoint appears remote, not phase1 local: {url}")
    if not any(marker in lowered for marker in LOCAL_HOST_HINTS):
        return CheckResult(name, "warn", f"endpoint is not obviously local; verify it is a private model service: {url}")
    return None


def join_api(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/") + "/"
    suffix = suffix.lstrip("/")
    return urljoin(base, suffix)


def extract_chat_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    for key in ("content", "text", "response"):
        if isinstance(payload.get(key), str):
            return payload[key]
    return ""


def embedding_dimension(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("embedding"), list):
            return len(first["embedding"])
    if isinstance(payload.get("embedding"), list):
        return len(payload["embedding"])
    return 0


def extract_rerank_count(payload: Any) -> int:
    if isinstance(payload, dict):
        for key in ("results", "data", "scores", "output"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                nested_count = extract_rerank_count(value)
                if nested_count:
                    return nested_count
    if isinstance(payload, list):
        return len(payload)
    return 0


def check_chat(env: dict[str, str], timeout: float) -> CheckResult:
    base_url = env_value(env, "LLM_BASE_URL")
    guard = endpoint_guard("chat_endpoint", base_url)
    if guard and guard.status == "fail":
        return guard
    model = env_value(env, "LLM_MODEL_NAME", "qwen-35b-local")
    api_key = env_value(env, "LLM_API_KEY")
    url = join_api(base_url, "chat/completions")

    def run() -> tuple[str, str]:
        status, payload = http_json(
            url,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是 AlphaMind 第一阶段本地模型连通性检查助手。"},
                    {"role": "user", "content": "请只回复：PHASE1_CHAT_OK"},
                ],
                "temperature": 0,
                "max_tokens": 32,
                "stream": False,
            },
            headers=bearer_headers(api_key),
            timeout=timeout,
        )
        if status < 200 or status >= 300:
            return "fail", f"HTTP {status}: {str(payload)[:300]}"
        text = extract_chat_text(payload)
        if not text:
            return "fail", f"HTTP {status} but no chat text in response"
        detail = f"HTTP {status}; model={model}; response_preview={text[:80]!r}"
        if guard and guard.status == "warn":
            return "warn", guard.detail + "; " + detail
        return "pass", detail

    return timed("chat_completion", run)


def check_embedding(env: dict[str, str], timeout: float) -> CheckResult:
    base_url = env_value(env, "EMBEDDING_BASE_URL")
    guard = endpoint_guard("embedding_endpoint", base_url)
    if guard and guard.status == "fail":
        return guard
    model = env_value(env, "EMBEDDING_MODEL_NAME", "phase1-embedding")
    api_key = env_value(env, "EMBEDDING_API_KEY")
    expected_dim_raw = env_value(env, "EMBEDDING_DIMENSION")
    expected_dim = int(expected_dim_raw) if expected_dim_raw.isdigit() else 0
    url = join_api(base_url, "embeddings")

    def run() -> tuple[str, str]:
        status, payload = http_json(
            url,
            {"model": model, "input": ["AlphaMind 第一阶段向量检查", "基础 RAG 检索"]},
            headers=bearer_headers(api_key),
            timeout=timeout,
        )
        if status < 200 or status >= 300:
            return "fail", f"HTTP {status}: {str(payload)[:300]}"
        actual_dim = embedding_dimension(payload)
        if not actual_dim:
            return "fail", "embedding response did not contain vectors"
        if expected_dim and actual_dim != expected_dim:
            return "fail", f"dimension mismatch: expected {expected_dim}, got {actual_dim}"
        detail = f"HTTP {status}; model={model}; dimension={actual_dim}"
        if guard and guard.status == "warn":
            return "warn", guard.detail + "; " + detail
        return "pass", detail

    return timed("embedding", run)


def check_rerank(env: dict[str, str], timeout: float) -> CheckResult:
    base_url = env_value(env, "RERANK_BASE_URL")
    guard = endpoint_guard("rerank_endpoint", base_url)
    if guard and guard.status == "fail":
        return guard
    model = env_value(env, "RERANK_MODEL_NAME", "phase1-rerank")
    api_key = env_value(env, "RERANK_API_KEY")
    candidate_urls = [base_url.rstrip("/"), join_api(base_url, "rerank")]
    payloads = [
        {
            "model": model,
            "query": "AlphaMind 第一阶段验收口令",
            "documents": [
                "无关文本",
                "AlphaMind 第一阶段验收口令是 BASIC-TXT-CHECKPOINT-7319。",
                "另一个无关段落",
            ],
        },
        {
            "model": model,
            "input": {
                "query": "AlphaMind 第一阶段验收口令",
                "documents": [
                    "无关文本",
                    "AlphaMind 第一阶段验收口令是 BASIC-TXT-CHECKPOINT-7319。",
                    "另一个无关段落",
                ],
            },
        },
    ]

    def run() -> tuple[str, str]:
        errors: list[str] = []
        for url in dict.fromkeys(candidate_urls):
            for payload in payloads:
                try:
                    status, response_payload = http_json(url, payload, headers=bearer_headers(api_key), timeout=timeout)
                except Exception as exc:  # try next compatible shape
                    errors.append(f"{url}: {type(exc).__name__}: {exc}")
                    continue
                if 200 <= status < 300 and extract_rerank_count(response_payload):
                    detail = f"HTTP {status}; model={model}; endpoint={url}; results={extract_rerank_count(response_payload)}"
                    if guard and guard.status == "warn":
                        return "warn", guard.detail + "; " + detail
                    return "pass", detail
                errors.append(f"{url}: HTTP {status}: {str(response_payload)[:180]}")
        return "fail", "; ".join(errors[-4:]) or "no compatible rerank response"

    return timed("rerank", run)


def print_results(results: list[CheckResult]) -> None:
    print("\nAlphaMind phase1-basic model API check")
    print("=" * 90)
    for result in results:
        print(f"{result.status.upper():<5} {result.name:<24} {result.elapsed_ms:>5}ms  {result.detail}")
    print("=" * 90)
    failures = [result for result in results if result.status == "fail"]
    warnings = [result for result in results if result.status == "warn"]
    print(f"failures={len(failures)}, warnings={len(warnings)}, total={len(results)}")


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    env = parse_env_file(env_file)
    results: list[CheckResult] = []

    if not env_file.exists():
        results.append(CheckResult("env_file", "fail", f"missing: {env_file}"))
    else:
        results.append(CheckResult("env_file", "pass", str(env_file)))

    if not args.skip_chat:
        results.append(check_chat(env, args.timeout))
    if not args.skip_embedding:
        results.append(check_embedding(env, args.timeout))
    if not args.skip_rerank:
        results.append(check_rerank(env, args.timeout))

    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_results(results)

    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
