#!/usr/bin/env python3
"""Cross-platform delivery controller for the AlphaMind Phase3 stack.

All external commands are represented as argv lists and execution is routed
through an injectable runner. The restore command defaults to a dry-run plan and can explicitly execute only
into new staging volumes after verification. Active volumes are never deleted
or overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ".env.phase3-precision"
DEFAULT_SAMPLE_PDF = Path("数据/0710/半导体券商研究报告/产业链/A股/卓胜微_300782/卓胜微_300782_2024年报.pdf")
DEFAULT_RELATION_SOURCE = Path("数据/0710/企业md")
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.phase2-advanced.yml",
    "docker-compose.phase3-precision.yml",
    "docker-compose.delivery.yml",
)
COMPOSE_PROFILES = ("qdrant", "neo4j", "minio")
BACKUP_IMAGE = "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"

SECRET_KEYS = (
    "DB_PASSWORD",
    "REDIS_PASSWORD",
    "TENANT_AES_KEY",
    "SYSTEM_AES_KEY",
    "JWT_SECRET",
    "MINIO_SECRET_ACCESS_KEY",
    "NEO4J_PASSWORD",
)
SECRET_MIN_LENGTHS = {
    "DB_PASSWORD": 16,
    "REDIS_PASSWORD": 16,
    "TENANT_AES_KEY": 32,
    "SYSTEM_AES_KEY": 32,
    "JWT_SECRET": 32,
    "MINIO_SECRET_ACCESS_KEY": 16,
    "NEO4J_PASSWORD": 16,
}
INSECURE_SECRET_VALUES = {
    "",
    "change_me",
    "changeme",
    "password",
    "minioadmin",
    "copy_from_existing_phase2_env",
}


@dataclass(frozen=True)
class VolumeTarget:
    key: str
    container: str
    volume: str
    destination: str


PERSISTENT_TARGETS = (
    VolumeTarget("postgres", "WeKnora-postgres", "postgres-data", "/var/lib/postgresql/data"),
    VolumeTarget("qdrant", "WeKnora-qdrant", "qdrant_data", "/qdrant/storage"),
    VolumeTarget("neo4j", "WeKnora-neo4j", "neo4j-data", "/data"),
    VolumeTarget("minio", "WeKnora-minio", "minio_data", "/data"),
    VolumeTarget("data-files", "WeKnora-app", "data-files", "/data/files"),
)


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    checked: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RestorePlan:
    backup_dir: Path
    dry_run: bool
    commands: tuple[list[str], ...]


class RestoreRefused(RuntimeError):
    """Raised when restore safety checks fail closed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def default_backup_directory(project_dir: Path | str, stamp: str) -> Path:
    return Path(project_dir).resolve().parent / "AlphaMind-backups" / f"alphamind-{stamp}"


def compose_command(*arguments: str, env_file: str = DEFAULT_ENV_FILE) -> list[str]:
    command = ["docker", "compose", "--env-file", env_file]
    for compose_file in COMPOSE_FILES:
        command.extend(("-f", compose_file))
    for profile in COMPOSE_PROFILES:
        command.extend(("--profile", profile))
    command.extend(arguments)
    return command


def restore_compose_command(
    backup_name: str,
    *arguments: str,
    env_file: str = DEFAULT_ENV_FILE,
) -> list[str]:
    if not re.fullmatch(r"alphamind-[A-Za-z0-9_.-]+", backup_name):
        raise ValueError("invalid restore backup name")
    project_name = f"alphamind-restore-verify-{backup_name}".lower()
    command = [
        "docker", "compose", "-p", project_name,
        "--env-file", env_file,
    ]
    for compose_file in (*COMPOSE_FILES, "docker-compose.restore-validation.yml"):
        command.extend(("-f", compose_file))
    for profile in COMPOSE_PROFILES:
        command.extend(("--profile", profile))
    command.extend(arguments)
    return command


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def insecure_secret_keys(env: Mapping[str, str]) -> list[str]:
    insecure = []
    for key in SECRET_KEYS:
        value = env.get(key, "").strip().lower()
        if (
            value in INSECURE_SECRET_VALUES
            or value.startswith("copy_from_")
            or len(value) < SECRET_MIN_LENGTHS[key]
        ):
            insecure.append(key)
    return insecure


def select_persistent_volumes(
    mounts: Iterable[tuple[str, str, str]],
) -> list[VolumeTarget]:
    """Select only the five allow-listed container destination mounts.

    Input tuples are ``(container, volume, destination)``. Matching both the
    container and destination prevents Redis, logs, and docreader temporary
    data from entering a delivery backup even when a volume has a similar name.
    """

    by_mount = {(target.container, target.destination): target for target in PERSISTENT_TARGETS}
    selected: dict[str, VolumeTarget] = {}
    for container, volume, destination in mounts:
        target = by_mount.get((container, destination))
        if target is not None:
            selected[target.key] = replace(target, volume=volume)
    return [selected[target.key] for target in PERSISTENT_TARGETS if target.key in selected]


def parse_inspect_mounts(container: str, payload: str) -> list[tuple[str, str, str]]:
    try:
        mounts = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid docker inspect mounts for {container}: {exc}") from exc
    if not isinstance(mounts, list):
        raise ValueError(f"invalid docker inspect mounts for {container}: expected list")
    result: list[tuple[str, str, str]] = []
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "volume":
            continue
        name = mount.get("Name")
        destination = mount.get("Destination")
        if isinstance(name, str) and name and isinstance(destination, str) and destination:
            result.append((container, name, destination))
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_archive_path(backup_dir: Path, archive_name: object) -> Path | None:
    if not isinstance(archive_name, str):
        return None
    archive = Path(archive_name)
    if archive.is_absolute() or len(archive.parts) != 1 or archive.name != archive_name:
        return None
    return backup_dir / archive


def verify_backup(backup_dir: Path | str) -> VerificationResult:
    backup_dir = Path(backup_dir)
    errors: list[str] = []
    manifest_path = backup_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return VerificationResult(False, 0, (f"manifest unreadable: {exc}",))

    if manifest.get("schema_version") != 1:
        errors.append("unsupported manifest schema_version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return VerificationResult(False, 0, tuple(errors + ["manifest artifacts must be a list"]))

    expected = {target.key: target for target in PERSISTENT_TARGETS}
    seen: set[str] = set()
    checked = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("invalid artifact entry")
            continue
        key = artifact.get("key")
        if key not in expected:
            errors.append(f"unexpected backup target: {key!r}")
            continue
        if key in seen:
            errors.append(f"duplicate backup target: {key}")
            continue
        seen.add(key)
        checked += 1
        target = expected[key]
        for field in ("container", "destination"):
            if artifact.get(field) != getattr(target, field):
                errors.append(f"{key}: {field} does not match allow-list")
        volume = artifact.get("volume")
        if not isinstance(volume, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", volume):
            errors.append(f"{key}: invalid Docker volume name")

        archive_name = artifact.get("archive")
        expected_archive_name = f"{key}.tar.gz"
        if archive_name != expected_archive_name:
            errors.append(f"{key}: archive name must be {expected_archive_name}")
            continue
        archive_path = _safe_archive_path(backup_dir, archive_name)
        if archive_path is None:
            errors.append(f"{key}: unsafe archive path")
            continue
        if not archive_path.is_file():
            errors.append(f"{key}: archive missing")
            continue
        actual_size = archive_path.stat().st_size
        if artifact.get("size") != actual_size:
            errors.append(f"{key}: size mismatch")
        expected_hash = artifact.get("sha256")
        if not isinstance(expected_hash, str) or sha256_file(archive_path) != expected_hash.lower():
            errors.append(f"{key}: sha256 mismatch")

    missing = [target.key for target in PERSISTENT_TARGETS if target.key not in seen]
    if missing:
        errors.append("missing backup targets: " + ", ".join(missing))
    return VerificationResult(not errors, checked, tuple(errors))


def build_backup_commands(
    backup_dir: Path | str,
    targets: Sequence[VolumeTarget] = PERSISTENT_TARGETS,
) -> list[list[str]]:
    host_backup = str(Path(backup_dir).resolve())
    commands = []
    for target in targets:
        commands.append([
            "docker", "run", "--rm",
            "--volumes-from", target.container,
            "-v", f"{host_backup}:/backup",
            BACKUP_IMAGE,
            "tar", "-czf", f"/backup/{target.key}.tar.gz",
            "-C", target.destination, ".",
        ])
    return commands


def _staging_volume_name(backup_name: str, key: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", backup_name).strip("-.") or "backup"
    return f"alphamind-restore-{safe_name}-{key}"


def build_restore_commands(backup_dir: Path | str) -> list[list[str]]:
    backup_dir = Path(backup_dir)
    host_backup = str(backup_dir.resolve())
    commands = []
    for target in PERSISTENT_TARGETS:
        staging_volume = _staging_volume_name(backup_dir.name, target.key)
        commands.append([
            "docker", "run", "--rm",
            "-v", f"{staging_volume}:/restore",
            "-v", f"{host_backup}:/backup:ro",
            BACKUP_IMAGE,
            "tar", "-xzf", f"/backup/{target.key}.tar.gz",
            "-C", "/restore",
        ])
    return commands


def restore_backup(
    backup_dir: Path | str,
    *,
    confirm_restore: str | None,
    execute_staging: bool = False,
    runner: Runner = subprocess.run,
) -> RestorePlan:
    """Validate a backup and optionally restore only into brand-new staging volumes."""

    backup_dir = Path(backup_dir)
    if not confirm_restore or confirm_restore != backup_dir.name:
        raise RestoreRefused(
            f"restore confirmation must exactly equal backup directory name: {backup_dir.name}"
        )
    result = verify_backup(backup_dir)
    if not result.ok:
        raise RestoreRefused("backup verification failed: " + "; ".join(result.errors))
    commands = tuple(build_restore_commands(backup_dir))
    if not execute_staging:
        return RestorePlan(backup_dir.resolve(), True, commands)

    run_kwargs = {
        "cwd": str(backup_dir.resolve().parent),
        "text": True,
        "capture_output": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    for command in commands:
        staging_volume = command[4].split(":", 1)[0]
        inspected = runner(["docker", "volume", "inspect", staging_volume], **run_kwargs)
        detail = (inspected.stderr or inspected.stdout or "").lower()
        if inspected.returncode == 0:
            raise RestoreRefused(f"staging volume already exists: {staging_volume}")
        if inspected.returncode != 1 or not any(token in detail for token in ("no such volume", "missing")):
            raise RestoreRefused(f"cannot verify staging volume absence: {staging_volume}")
        restored = runner(command, **run_kwargs)
        if restored.returncode:
            detail = (restored.stderr or restored.stdout or "restore command failed").strip()
            raise RestoreRefused(f"staging restore failed for {staging_volume}: {detail}")
    return RestorePlan(backup_dir.resolve(), False, commands)


def final_backup_status(backup_status: int, restart_status: int) -> int:
    return int(backup_status) if backup_status else int(restart_status)


class DeliveryController:
    def __init__(
        self,
        project_dir: Path | str = PROJECT_ROOT,
        *,
        runner: Runner = subprocess.run,
        env_file: str = DEFAULT_ENV_FILE,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.runner = runner
        self.env_file = env_file

    def _run(
        self,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        env_path = self.project_dir / self.env_file
        if env_path.is_file():
            command_env.update(parse_env_file(env_path))
        if extra_env:
            command_env.update(extra_env)
        return self.runner(
            list(command),
            cwd=str(self.project_dir),
            text=True,
            capture_output=capture_output,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=command_env,
        )

    def _compose(self, *arguments: str, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        return self._run(
            compose_command(*arguments, env_file=self.env_file),
            capture_output=capture_output,
        )

    @staticmethod
    def _exit_code(completed: subprocess.CompletedProcess[str]) -> int:
        return int(completed.returncode)

    def preflight(self) -> int:
        missing = [name for name in (*COMPOSE_FILES, self.env_file) if not (self.project_dir / name).is_file()]
        if missing:
            print("preflight FAIL: missing files: " + ", ".join(missing), file=sys.stderr)
            return 1
        try:
            env = parse_env_file(self.project_dir / self.env_file)
        except OSError as exc:
            print(f"preflight FAIL: cannot read env file: {exc}", file=sys.stderr)
            return 1
        insecure = insecure_secret_keys(env)
        if insecure:
            print("preflight FAIL: insecure or placeholder secrets: " + ", ".join(insecure), file=sys.stderr)
            return 1
        for command in (["docker", "version"], compose_command("config", "--quiet", env_file=self.env_file)):
            completed = self._run(command, capture_output=True)
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "command failed").strip()
                print(f"preflight FAIL: {detail}", file=sys.stderr)
                return int(completed.returncode) or 1
        print("preflight PASS")
        return 0

    def _readiness(self) -> int:
        command = [
            sys.executable,
            str(self.project_dir / "scripts" / "alphamind_healthcheck.py"),
            "--phase", "phase3-precision",
            "--env-file", str(self.project_dir / self.env_file),
            "--require-qdrant", "--require-neo4j", "--require-minio",
        ]
        return self._exit_code(self._run(command))

    def start(self) -> int:
        started = self._compose("up", "-d")
        if started.returncode:
            return int(started.returncode) or 1
        return self._readiness()

    def stop(self) -> int:
        return self._exit_code(self._compose("stop"))

    def status(self) -> int:
        return self._exit_code(self._compose("ps"))

    def logs(self, services: Sequence[str] = (), *, tail: int = 200) -> int:
        return self._exit_code(self._compose("logs", "--tail", str(tail), *services))

    def validate_restore(self, backup_name: str, *, app_port: int = 28080) -> int:
        try:
            restore_compose_command(backup_name, "config", "--quiet", env_file=self.env_file)
        except ValueError as exc:
            print(f"restore validation REFUSED: {exc}", file=sys.stderr)
            return 1
        for target in PERSISTENT_TARGETS:
            volume = _staging_volume_name(backup_name, target.key)
            inspected = self._run(["docker", "volume", "inspect", volume], capture_output=True)
            if inspected.returncode:
                print(f"restore validation FAIL: staging volume missing: {volume}", file=sys.stderr)
                return 1
        extra_env = {"RESTORE_BACKUP_NAME": backup_name, "RESTORE_APP_PORT": str(app_port)}
        started = self._run(
            restore_compose_command(backup_name, "up", "-d", "app", env_file=self.env_file),
            extra_env=extra_env,
        )
        if started.returncode:
            self._run(
                restore_compose_command(backup_name, "down", "-v", env_file=self.env_file),
                extra_env=extra_env,
            )
            return int(started.returncode) or 1
        health = self._run([
            "curl", "-fsS", "--retry", "30", "--retry-delay", "2", "--retry-all-errors",
            "--max-time", "10", f"http://127.0.0.1:{app_port}/health",
        ])
        stopped = self._run(
            restore_compose_command(backup_name, "down", "-v", env_file=self.env_file),
            extra_env=extra_env,
        )
        if health.returncode:
            return int(health.returncode) or 1
        if stopped.returncode:
            return int(stopped.returncode) or 1
        print("isolated restore validation PASS")
        return 0

    def discover_persistent_volumes(self) -> list[VolumeTarget]:
        mounts: list[tuple[str, str, str]] = []
        for target in PERSISTENT_TARGETS:
            completed = self._run(
                ["docker", "inspect", target.container, "--format", "{{json .Mounts}}"],
                capture_output=True,
            )
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "docker inspect failed").strip()
                raise RuntimeError(f"cannot inspect {target.container}: {detail}")
            mounts.extend(parse_inspect_mounts(target.container, completed.stdout or ""))
        selected = select_persistent_volumes(mounts)
        if len(selected) != len(PERSISTENT_TARGETS):
            found = {target.key for target in selected}
            missing = [target.key for target in PERSISTENT_TARGETS if target.key not in found]
            raise RuntimeError("missing persistent volumes: " + ", ".join(missing))
        return selected

    def _archive_targets(self, backup_dir: Path, targets: Sequence[VolumeTarget]) -> int:
        artifacts = []
        for target, command in zip(targets, build_backup_commands(backup_dir, targets)):
            completed = self._run(command)
            if completed.returncode:
                return int(completed.returncode) or 1
            archive = backup_dir / f"{target.key}.tar.gz"
            if not archive.is_file():
                print(f"backup FAIL: expected archive was not created: {archive}", file=sys.stderr)
                return 1
            artifacts.append({
                **asdict(target),
                "archive": archive.name,
                "sha256": sha256_file(archive),
                "size": archive.stat().st_size,
            })
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": artifacts,
        }
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = verify_backup(backup_dir)
        if not result.ok:
            print("backup FAIL: " + "; ".join(result.errors), file=sys.stderr)
            return 1
        print(str(backup_dir))
        return 0

    def backup(self, output_dir: Path | str) -> int:
        try:
            targets = self.discover_persistent_volumes()
        except (RuntimeError, ValueError) as exc:
            print(f"backup FAIL: {exc}", file=sys.stderr)
            return 1
        backup_dir = Path(output_dir)
        if not backup_dir.is_absolute():
            backup_dir = self.project_dir / backup_dir
        backup_dir.mkdir(parents=True, exist_ok=False)

        stopped = self._compose("stop")
        if stopped.returncode:
            return int(stopped.returncode) or 1
        backup_status = 1
        restart_status = 0
        try:
            backup_status = self._archive_targets(backup_dir, targets)
        finally:
            restarted = self._compose("start")
            restart_status = int(restarted.returncode) or 0
            if not restart_status:
                restart_status = self._readiness()
            if restart_status:
                print("backup FAIL: stack restart/readiness failed", file=sys.stderr)
        return final_backup_status(backup_status, restart_status)

    def acceptance(self, *, sample_pdf: Path = DEFAULT_SAMPLE_PDF, max_pages: int = 30) -> int:
        if self.preflight() != 0:
            return 1
        sample_path = sample_pdf if sample_pdf.is_absolute() else self.project_dir / sample_pdf
        commands = [
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_alphamind*.py"],
            [
                sys.executable,
                str(self.project_dir / "scripts" / "alphamind_relation_graph.py"),
                "--source-root", str(self.project_dir / DEFAULT_RELATION_SOURCE),
                "--output", str(self.project_dir / "dataset" / "alphamind_relation_graph.json"),
                "--report", str(self.project_dir / "dataset" / "alphamind_relation_graph_report.json"),
                "--import-neo4j", "--env-file", str(self.project_dir / self.env_file),
            ],
            [
                sys.executable,
                str(self.project_dir / "scripts" / "alphamind_phase3_quality_gate.py"),
                "--sample-pdf", str(sample_path),
                "--max-pages", str(max_pages),
                "--env-file", str(self.project_dir / self.env_file),
                "--live", "--concurrency", "4", "--requests", "40", "--keep-going",
            ],
        ]
        for command in commands:
            completed = self._run(command)
            if completed.returncode:
                return int(completed.returncode) or 1
        print("delivery acceptance PASS")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AlphaMind delivery controller")
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")

    logs = subparsers.add_parser("logs")
    logs.add_argument("services", nargs="*")
    logs.add_argument("--tail", type=int, default=200)

    backup = subparsers.add_parser("backup")
    backup.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="New backup directory (default: ../AlphaMind-backups/alphamind-<UTC timestamp>)",
    )

    verify = subparsers.add_parser("verify-backup")
    verify.add_argument("backup_dir", type=Path)

    restore = subparsers.add_parser("restore")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--confirm-restore", required=True)
    restore.add_argument(
        "--execute-staging",
        action="store_true",
        help="Execute verified archives into brand-new staging volumes only",
    )

    validate_restore = subparsers.add_parser("validate-restore")
    validate_restore.add_argument("backup_name")
    validate_restore.add_argument("--app-port", type=int, default=28080)

    acceptance = subparsers.add_parser("acceptance")
    acceptance.add_argument("--sample-pdf", type=Path, default=DEFAULT_SAMPLE_PDF)
    acceptance.add_argument("--max-pages", type=int, default=30)
    return parser


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    args = build_parser().parse_args(argv)
    controller = DeliveryController(args.project_dir, runner=runner, env_file=args.env_file)
    if args.command == "preflight":
        return controller.preflight()
    if args.command == "start":
        return controller.start()
    if args.command == "stop":
        return controller.stop()
    if args.command == "status":
        return controller.status()
    if args.command == "logs":
        return controller.logs(args.services, tail=args.tail)
    if args.command == "backup":
        output_dir = args.output_dir
        if output_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_dir = default_backup_directory(args.project_dir, stamp)
        return controller.backup(output_dir)
    if args.command == "verify-backup":
        result = verify_backup(args.backup_dir)
        if result.ok:
            print(f"backup PASS: {result.checked} artifacts")
            return 0
        print("backup FAIL: " + "; ".join(result.errors), file=sys.stderr)
        return 1
    if args.command == "restore":
        try:
            plan = restore_backup(
                args.backup_dir,
                confirm_restore=args.confirm_restore,
                execute_staging=args.execute_staging,
                runner=runner,
            )
        except RestoreRefused as exc:
            print(f"restore REFUSED: {exc}", file=sys.stderr)
            return 1
        if plan.dry_run:
            print("restore dry-run PASS; active data is unchanged")
            for command in plan.commands:
                print(json.dumps(command, ensure_ascii=False))
        else:
            print("staging restore PASS; active data is unchanged")
        return 0
    if args.command == "validate-restore":
        return controller.validate_restore(args.backup_name, app_port=args.app_port)
    if args.command == "acceptance":
        return controller.acceptance(sample_pdf=args.sample_pdf, max_pages=args.max_pages)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
