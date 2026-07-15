#!/usr/bin/env python3
"""Bounded concurrent HTTP load probe for AlphaMind Phase3."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_api_key(cli_value: str, env_file: Path, process_env: Mapping[str, str]) -> str:
    return str(cli_value or parse_env_file(env_file).get("WEKNORA_API_KEY") or process_env.get("WEKNORA_API_KEY") or "")


def json_has_items(payload: Any) -> bool:
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict):
        return False
    for key in ("items", "results", "chunks", "data"):
        if key in payload and json_has_items(payload[key]):
            return True
    return False


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((percentile_value / 100.0) * len(ordered)) - 1))
    return ordered[index]


def summarize(latencies_ms: list[float], *, successes: int, failures: int, elapsed_seconds: float = 0.0) -> dict[str, Any]:
    total = successes + failures
    return {
        "requests": total,
        "successes": successes,
        "failures": failures,
        "error_rate": failures / total if total else 0.0,
        "p50_ms": round(percentile(latencies_ms, 50), 3),
        "p95_ms": round(percentile(latencies_ms, 95), 3),
        "p99_ms": round(percentile(latencies_ms, 99), 3),
        "max_ms": round(max(latencies_ms), 3) if latencies_ms else 0.0,
        "throughput_rps": round(total / elapsed_seconds, 3) if elapsed_seconds > 0 else 0.0,
    }


def request_once(
    url: str,
    *,
    method: str,
    payload: bytes | None,
    headers: dict[str, str],
    timeout: float,
    expect_json_items: bool = False,
) -> tuple[bool, float, str]:
    start = time.perf_counter()
    request = Request(url, data=payload, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
            status = int(getattr(response, "status", 200))
            ok = 200 <= status < 300
            if ok and expect_json_items:
                try:
                    decoded = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return False, (time.perf_counter() - start) * 1000, "response is not JSON"
                if not json_has_items(decoded):
                    return False, (time.perf_counter() - start) * 1000, "response JSON contains no result items"
            return ok, (time.perf_counter() - start) * 1000, f"HTTP {status}"
    except HTTPError as exc:
        return False, (time.perf_counter() - start) * 1000, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded HTTP load probe against AlphaMind.")
    parser.add_argument("--url", default="http://localhost:8080/health")
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--payload", default="", help="Inline JSON or @path/to/file.json")
    parser.add_argument("--api-key", default="", help="Explicit API key; prefer --env-file to avoid process-list leakage.")
    parser.add_argument("--env-file", type=Path, default=Path(".env.phase3-precision"))
    parser.add_argument("--expect-json-items", action="store_true", help="Fail 2xx responses that do not contain a non-empty result list.")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--warmup-requests", type=int, default=0, help="Sequential successful probes excluded from measured latency.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--out", default="dataset/phase3_runs/phase3_load_latest.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.requests <= 0 or args.concurrency <= 0 or args.warmup_requests < 0:
        raise SystemExit("requests/concurrency must be positive and warmup-requests non-negative")
    raw_payload = args.payload
    if raw_payload.startswith("@"):
        raw_payload = Path(raw_payload[1:]).read_text(encoding="utf-8")
    payload = raw_payload.encode("utf-8") if raw_payload else None
    api_key = resolve_api_key(args.api_key, args.env_file, os.environ)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    warmup_results = [
        request_once(
            args.url,
            method=args.method,
            payload=payload,
            headers=headers,
            timeout=args.timeout,
            expect_json_items=args.expect_json_items,
        )
        for _ in range(args.warmup_requests)
    ]
    warmup_failures = sum(1 for ok, _, _ in warmup_results if not ok)
    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                request_once,
                args.url,
                method=args.method,
                payload=payload,
                headers=headers,
                timeout=args.timeout,
                expect_json_items=args.expect_json_items,
            )
            for _ in range(args.requests)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    latencies = [latency for _, latency, _ in results]
    successes = sum(1 for ok, _, _ in results if ok)
    failures = len(results) - successes
    report = summarize(latencies, successes=successes, failures=failures, elapsed_seconds=elapsed)
    report.update({
        "url": args.url,
        "method": args.method,
        "concurrency": args.concurrency,
        "warmup_requests": args.warmup_requests,
        "warmup_failures": warmup_failures,
        "warmup_max_ms": round(max((latency for _, latency, _ in warmup_results), default=0.0), 3),
        "failure_samples": [detail for ok, _, detail in results if not ok][:10],
    })
    report["status"] = "pass" if warmup_failures == 0 and report["error_rate"] <= args.max_error_rate and report["p95_ms"] <= args.max_p95_ms else "fail"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
