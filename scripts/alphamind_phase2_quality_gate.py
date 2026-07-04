#!/usr/bin/env python3
"""
Run the AlphaMind Phase2 strict quality gate end to end.

The gate wraps the Phase2 healthcheck, ground-truth validation, strict retrieval
quality evaluation, strict chat quality evaluation, and evaluator unit tests into
one reproducible command. It never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.phase2-advanced"
DEFAULT_OUT_DIR = PROJECT_ROOT / "dataset" / "phase2_runs"
DEFAULT_REPORT = DEFAULT_OUT_DIR / "phase2_quality_gate_latest.json"

MODEL_ACCOUNT_ERROR_TERMS = (
    "arrearage",
    "access denied",
    "account is in good standing",
    "insufficient_quota",
    "quota",
    "billing",
    "欠费",
    "额度",
)
NETWORK_ERROR_TERMS = (
    "connection refused",
    "connection reset",
    "temporary failure in name resolution",
    "no such host",
    "timeout",
    "timed out",
)


@dataclass
class CommandSpec:
    name: str
    command: str
    timeout: int = 600
    argv: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    name: str
    status: str
    exit_code: int
    elapsed_ms: int
    output: str
    command: str
    failure_class: str = ""


@dataclass
class GateResult:
    status: str
    steps: list[StepResult]
    elapsed_ms: int
    report_path: str = ""


def display_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv).replace(shlex.quote(sys.executable), "python", 1)


def timeout_for(name: str, args: argparse.Namespace) -> int:
    explicit = getattr(args, "timeout", 0) or 0
    if explicit:
        return int(explicit)
    if name == "chat_eval":
        return max(300, int(float(args.chat_timeout) + 120))
    if name in {"retrieval_eval", "healthcheck"}:
        return 600
    return 300


def build_command_plan(args: argparse.Namespace) -> list[CommandSpec]:
    env_file = str(args.env_file)
    out_dir = str(args.out_dir)
    chat_timeout = str(int(args.chat_timeout) if float(args.chat_timeout).is_integer() else args.chat_timeout)

    plan: list[CommandSpec] = []

    if not args.skip_healthcheck:
        argv = [
            sys.executable,
            "scripts/alphamind_healthcheck.py",
            "--phase",
            "phase2-advanced",
            "--env-file",
            env_file,
            "--require-qdrant",
            "--require-neo4j",
            "--require-minio",
        ]
        plan.append(CommandSpec("healthcheck", display_command(argv), timeout_for("healthcheck", args), argv))

    argv = [
        sys.executable,
        "scripts/alphamind_phase2_quality_eval.py",
        "--validate-only",
        "--strict",
        "--env-file",
        env_file,
    ]
    plan.append(CommandSpec("validate_only", display_command(argv), timeout_for("validate_only", args), argv))

    argv = [
        sys.executable,
        "scripts/alphamind_phase2_quality_eval.py",
        "--mode",
        "retrieval",
        "--strict",
        "--env-file",
        env_file,
        "--out",
        str(Path(out_dir) / "phase2_retrieval_eval_latest.json"),
    ]
    plan.append(CommandSpec("retrieval_eval", display_command(argv), timeout_for("retrieval_eval", args), argv))

    argv = [
        sys.executable,
        "scripts/alphamind_phase2_quality_eval.py",
        "--mode",
        "chat",
        "--strict",
        "--env-file",
        env_file,
        "--chat-timeout",
        chat_timeout,
        "--out",
        str(Path(out_dir) / "phase2_chat_eval_latest.json"),
    ]
    plan.append(CommandSpec("chat_eval", display_command(argv), timeout_for("chat_eval", args), argv))

    if not args.skip_tests:
        argv = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_alphamind_phase2_quality*.py",
            "-v",
        ]
        plan.append(CommandSpec("unit_tests", display_command(argv), timeout_for("unit_tests", args), argv))
    return plan


def classify_failure(output: str) -> str:
    lowered = output.lower()
    if any(term in lowered for term in MODEL_ACCOUNT_ERROR_TERMS):
        return "model_account_error"
    if any(term in lowered for term in NETWORK_ERROR_TERMS):
        return "network_or_service_error"
    if "assert" in lowered or "failed" in lowered or "traceback" in lowered:
        return "test_or_quality_failure"
    return "command_failure"


def run_command(spec: CommandSpec, cwd: Path = PROJECT_ROOT) -> StepResult:
    start = time.perf_counter()
    argv = spec.argv or shlex.split(spec.command)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=spec.timeout,
        )
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        status = "pass" if completed.returncode == 0 else "fail"
        failure_class = "" if status == "pass" else classify_failure(output)
        return StepResult(
            spec.name,
            status,
            int(completed.returncode),
            int((time.perf_counter() - start) * 1000),
            output,
            spec.command,
            failure_class,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in [exc.stdout or "", exc.stderr or "", f"timeout after {spec.timeout}s"] if part)
        return StepResult(
            spec.name,
            "fail",
            124,
            int((time.perf_counter() - start) * 1000),
            output,
            spec.command,
            "network_or_service_error" if "timeout" in output.lower() else "command_timeout",
        )


def run_quality_gate(plan: list[CommandSpec], *, keep_going: bool = False, cwd: Path = PROJECT_ROOT) -> GateResult:
    start = time.perf_counter()
    steps: list[StepResult] = []
    for spec in plan:
        result = run_command(spec, cwd=cwd)
        steps.append(result)
        if result.status != "pass" and not keep_going:
            break
    status = "pass" if steps and all(step.status == "pass" for step in steps) else "fail"
    return GateResult(status, steps, int((time.perf_counter() - start) * 1000))


def write_report(result: GateResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    payload["report_path"] = str(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result.report_path = str(path)


def print_summary(result: GateResult) -> None:
    print("AlphaMind Phase2 quality gate")
    print("=" * 80)
    for step in result.steps:
        detail = step.output.splitlines()[-1] if step.output else ""
        suffix = f" [{step.failure_class}]" if step.failure_class else ""
        print(f"{step.status.upper():<5} {step.name:<18} {step.elapsed_ms:>7}ms exit={step.exit_code}{suffix}  {detail[:220]}")
    print("=" * 80)
    print(f"PHASE2 QUALITY GATE: {result.status.upper()}")
    if result.report_path:
        print(f"report={result.report_path}")
    failures = [step for step in result.steps if step.status != "pass"]
    if failures:
        first = failures[0]
        print(f"first_failure={first.name} class={first.failure_class or 'unknown'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AlphaMind Phase2 strict quality gate.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument("--timeout", type=int, default=0, help="Override per-step timeout in seconds.")
    parser.add_argument("--skip-healthcheck", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--keep-going", action="store_true", help="Run all steps even after a failure.")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = build_command_plan(args)
    result = run_quality_gate(plan, keep_going=args.keep_going)
    write_report(result, Path(args.report))
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print_summary(result)
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
