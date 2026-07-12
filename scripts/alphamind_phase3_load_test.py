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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


def request_once(url: str, *, method: str, payload: bytes | None, headers: dict[str, str], timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    request = Request(url, data=payload, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(1024)
            status = int(getattr(response, "status", 200))
            return 200 <= status < 300, (time.perf_counter() - start) * 1000, f"HTTP {status}"
    except HTTPError as exc:
        return False, (time.perf_counter() - start) * 1000, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded HTTP load probe against AlphaMind.")
    parser.add_argument("--url", default="http://localhost:8080/health")
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--payload", default="", help="Inline JSON or @path/to/file.json")
    parser.add_argument("--api-key", default=os.environ.get("WEKNORA_API_KEY", ""), help="Defaults to WEKNORA_API_KEY; prefer the environment to avoid process-list leakage.")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--out", default="dataset/phase3_runs/phase3_load_latest.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.requests <= 0 or args.concurrency <= 0:
        raise SystemExit("requests and concurrency must be positive")
    raw_payload = args.payload
    if raw_payload.startswith("@"):
        raw_payload = Path(raw_payload[1:]).read_text(encoding="utf-8")
    payload = raw_payload.encode("utf-8") if raw_payload else None
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key
    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(request_once, args.url, method=args.method, payload=payload, headers=headers, timeout=args.timeout) for _ in range(args.requests)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    latencies = [latency for _, latency, _ in results]
    successes = sum(1 for ok, _, _ in results if ok)
    failures = len(results) - successes
    report = summarize(latencies, successes=successes, failures=failures, elapsed_seconds=elapsed)
    report.update({"url": args.url, "method": args.method, "concurrency": args.concurrency, "failure_samples": [detail for ok, _, detail in results if not ok][:10]})
    report["status"] = "pass" if report["error_rate"] <= args.max_error_rate and report["p95_ms"] <= args.max_p95_ms else "fail"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
