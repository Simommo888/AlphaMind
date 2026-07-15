import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_delivery.py"
spec = importlib.util.spec_from_file_location("alphamind_delivery", MODULE_PATH)
delivery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = delivery
spec.loader.exec_module(delivery)


class RecordingRunner:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class SequenceRunner:
    def __init__(self, returncodes):
        self.calls = []
        self.returncodes = iter(returncodes)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code = next(self.returncodes)
        return subprocess.CompletedProcess(argv, code, "", "missing" if code else "")


class AlphaMindDeliveryTests(unittest.TestCase):
    def test_compose_command_uses_three_layers_and_required_profiles_as_argv(self):
        self.assertEqual(
            delivery.compose_command("up", "-d"),
            [
                "docker", "compose", "--env-file", ".env.phase3-precision",
                "-f", "docker-compose.yml",
                "-f", "docker-compose.phase2-advanced.yml",
                "-f", "docker-compose.phase3-precision.yml",
                "-f", "docker-compose.delivery.yml",
                "--profile", "qdrant", "--profile", "neo4j", "--profile", "minio",
                "up", "-d",
            ],
        )

    def test_insecure_secret_check_rejects_empty_and_documented_placeholders(self):
        env = {
            "DB_PASSWORD": "change_me",
            "REDIS_PASSWORD": "strong-redis-password",
            "TENANT_AES_KEY": "copy_from_existing_phase2_env",
            "SYSTEM_AES_KEY": "",
            "JWT_SECRET": "real-jwt-secret-that-is-at-least-32-characters",
            "MINIO_SECRET_ACCESS_KEY": "minioadmin",
            "NEO4J_PASSWORD": "password",
        }
        self.assertEqual(
            delivery.insecure_secret_keys(env),
            ["DB_PASSWORD", "TENANT_AES_KEY", "SYSTEM_AES_KEY", "MINIO_SECRET_ACCESS_KEY", "NEO4J_PASSWORD"],
        )

    def test_weak_short_secrets_are_rejected(self):
        strong = "A" * 40
        env = {key: strong for key in delivery.SECRET_KEYS}
        env["DB_PASSWORD"] = "short"
        env["JWT_SECRET"] = "tiny"
        self.assertEqual(delivery.insecure_secret_keys(env), ["DB_PASSWORD", "JWT_SECRET"])

    def test_persistent_volume_filter_keeps_only_delivery_data_mounts(self):
        mounts = [
            ("WeKnora-postgres", "postgres-data", "/var/lib/postgresql/data"),
            ("WeKnora-qdrant", "qdrant_data", "/qdrant/storage"),
            ("WeKnora-neo4j", "neo4j-data", "/data"),
            ("WeKnora-minio", "minio_data", "/data"),
            ("WeKnora-app", "data-files", "/data/files"),
            ("WeKnora-redis", "redis-data", "/data"),
            ("WeKnora-docreader", "docreader-tmp", "/tmp/docreader"),
            ("WeKnora-app", "logs", "/app/logs"),
        ]
        selected = delivery.select_persistent_volumes(mounts)
        self.assertEqual([item.volume for item in selected], [
            "postgres-data", "qdrant_data", "neo4j-data", "minio_data", "data-files",
        ])
        self.assertEqual([item.destination for item in selected], [
            "/var/lib/postgresql/data", "/qdrant/storage", "/data", "/data", "/data/files",
        ])

    def test_backup_uses_pinned_available_helper_image(self):
        self.assertEqual(
            delivery.BACKUP_IMAGE,
            "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
        )

    def test_parse_inspect_mounts_and_accept_compose_prefixed_volume_names(self):
        payload = json.dumps([
            {"Type": "volume", "Name": "alphamind_postgres-data", "Destination": "/var/lib/postgresql/data"},
            {"Type": "bind", "Source": "config", "Destination": "/config"},
        ])
        self.assertEqual(
            delivery.parse_inspect_mounts("WeKnora-postgres", payload),
            [("WeKnora-postgres", "alphamind_postgres-data", "/var/lib/postgresql/data")],
        )

    def test_backup_manifest_verification_accepts_discovered_prefixed_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-prefixed"
            backup_dir.mkdir()
            artifacts = []
            for target in delivery.PERSISTENT_TARGETS:
                archive = backup_dir / f"{target.key}.tar.gz"
                archive.write_bytes(target.key.encode())
                artifacts.append({
                    "key": target.key,
                    "container": target.container,
                    "volume": f"alphamind_{target.volume}",
                    "destination": target.destination,
                    "archive": archive.name,
                    "sha256": hashlib.sha256(target.key.encode()).hexdigest(),
                    "size": len(target.key.encode()),
                })
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
            )
            self.assertTrue(delivery.verify_backup(backup_dir).ok)

    def test_backup_restart_failure_overrides_success(self):
        self.assertEqual(delivery.final_backup_status(0, 1), 1)
        self.assertEqual(delivery.final_backup_status(2, 0), 2)
        self.assertEqual(delivery.final_backup_status(0, 0), 0)

    def test_backup_verification_rejects_archive_name_not_bound_to_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-renamed"
            backup_dir.mkdir()
            artifacts = []
            for target in delivery.PERSISTENT_TARGETS:
                archive = backup_dir / f"{target.key}.verified"
                archive.write_bytes(target.key.encode())
                artifacts.append({
                    "key": target.key, "container": target.container,
                    "volume": target.volume, "destination": target.destination,
                    "archive": archive.name,
                    "sha256": hashlib.sha256(target.key.encode()).hexdigest(),
                    "size": len(target.key.encode()),
                })
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
            )
            result = delivery.verify_backup(backup_dir)
            self.assertFalse(result.ok)
            self.assertTrue(any("archive name" in error for error in result.errors))

    def test_default_backup_directory_is_outside_repository(self):
        target = delivery.default_backup_directory(ROOT, "20260712T000000Z")
        self.assertEqual(target, ROOT.parent / "AlphaMind-backups" / "alphamind-20260712T000000Z")
        self.assertNotEqual(target.parent, ROOT / "backups")

    def test_backup_manifest_verification_checks_sha256_and_required_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-001"
            backup_dir.mkdir()
            artifacts = []
            for target in delivery.PERSISTENT_TARGETS:
                archive = backup_dir / f"{target.key}.tar.gz"
                payload = f"archive:{target.key}".encode()
                archive.write_bytes(payload)
                artifacts.append({
                    "key": target.key,
                    "container": target.container,
                    "volume": target.volume,
                    "destination": target.destination,
                    "archive": archive.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                })
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
            )

            result = delivery.verify_backup(backup_dir)
            self.assertTrue(result.ok)
            self.assertEqual(result.checked, 5)

            (backup_dir / "qdrant.tar.gz").write_bytes(b"tampered")
            result = delivery.verify_backup(backup_dir)
            self.assertFalse(result.ok)
            self.assertTrue(any("sha256" in error for error in result.errors))

    def test_backup_manifest_verification_fails_closed_on_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-002"
            backup_dir.mkdir()
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": []}), encoding="utf-8"
            )
            result = delivery.verify_backup(backup_dir)
            self.assertFalse(result.ok)
            self.assertTrue(any("missing" in error for error in result.errors))

    def test_restore_requires_exact_backup_directory_confirmation_and_is_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-003"
            backup_dir.mkdir()
            runner = RecordingRunner()

            with self.assertRaises(delivery.RestoreRefused):
                delivery.restore_backup(backup_dir, confirm_restore=None, runner=runner)
            with self.assertRaises(delivery.RestoreRefused):
                delivery.restore_backup(backup_dir, confirm_restore="wrong-name", runner=runner)
            self.assertEqual(runner.calls, [])

    def test_restore_validates_backup_before_returning_non_executing_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-004"
            backup_dir.mkdir()
            artifacts = []
            for target in delivery.PERSISTENT_TARGETS:
                archive = backup_dir / f"{target.key}.tar.gz"
                archive.write_bytes(target.key.encode())
                artifacts.append({
                    "key": target.key,
                    "container": target.container,
                    "volume": target.volume,
                    "destination": target.destination,
                    "archive": archive.name,
                    "sha256": hashlib.sha256(target.key.encode()).hexdigest(),
                    "size": len(target.key.encode()),
                })
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
            )
            runner = RecordingRunner()
            plan = delivery.restore_backup(
                backup_dir, confirm_restore=backup_dir.name, runner=runner
            )
            self.assertTrue(plan.dry_run)
            self.assertEqual(len(plan.commands), 5)
            self.assertTrue(all(isinstance(arg, str) for command in plan.commands for arg in command))
            self.assertEqual(runner.calls, [])

    def test_restore_execute_staging_requires_new_volumes_and_runs_verified_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup-stage"
            backup_dir.mkdir()
            artifacts = []
            for target in delivery.PERSISTENT_TARGETS:
                archive = backup_dir / f"{target.key}.tar.gz"
                archive.write_bytes(target.key.encode())
                artifacts.append({
                    "key": target.key, "container": target.container,
                    "volume": target.volume, "destination": target.destination,
                    "archive": archive.name,
                    "sha256": hashlib.sha256(target.key.encode()).hexdigest(),
                    "size": len(target.key.encode()),
                })
            (backup_dir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "artifacts": artifacts}), encoding="utf-8"
            )
            runner = SequenceRunner([1, 0] * 5)
            plan = delivery.restore_backup(
                backup_dir,
                confirm_restore=backup_dir.name,
                execute_staging=True,
                runner=runner,
            )
            self.assertFalse(plan.dry_run)
            self.assertEqual(len(runner.calls), 10)
            self.assertTrue(all(call[1].get("shell") is False for call in runner.calls))

    def test_restore_validation_uses_isolated_project_and_overlay(self):
        command = delivery.restore_compose_command("alphamind-20260712T000000Z", "up", "-d", "app")
        self.assertIn("docker-compose.restore-validation.yml", command)
        self.assertIn("alphamind-restore-verify-alphamind-20260712t000000z", command)
        self.assertEqual(command[-3:], ["up", "-d", "app"])

    def test_validate_restore_checks_staging_volumes_and_isolated_app(self):
        runner = RecordingRunner()
        controller = delivery.DeliveryController(ROOT, runner=runner)
        self.assertEqual(controller.validate_restore("alphamind-20260712T000000Z", app_port=28080), 0)
        commands = [call[0] for call in runner.calls]
        self.assertEqual(sum(command[:3] == ["docker", "volume", "inspect"] for command in commands), 5)
        self.assertTrue(any("docker-compose.restore-validation.yml" in command for command in commands))
        self.assertTrue(any(command and command[0] == "curl" for command in commands))
        self.assertTrue(any(command[-2:] == ["down", "-v"] for command in commands))

    def test_controller_injects_runner_and_never_uses_shell_commands(self):
        runner = RecordingRunner()
        controller = delivery.DeliveryController(ROOT, runner=runner)
        self.assertEqual(controller.start(), 0)
        self.assertEqual(controller.stop(), 0)
        self.assertEqual(controller.status(), 0)
        self.assertEqual(controller.logs(["app"], tail=25), 0)

        commands = [call[0] for call in runner.calls]
        self.assertEqual(commands[0], delivery.compose_command("up", "-d"))
        self.assertTrue(any("alphamind_healthcheck.py" in arg for arg in commands[1]))
        self.assertEqual(commands[2], delivery.compose_command("stop"))
        self.assertNotIn("-v", commands[2])
        self.assertEqual(commands[3], delivery.compose_command("ps"))
        self.assertEqual(commands[4], delivery.compose_command("logs", "--tail", "25", "app"))
        self.assertTrue(all(isinstance(command, list) for command in commands))
        self.assertTrue(all(call[1].get("shell") is not True for call in runner.calls))
        expected_env = delivery.parse_env_file(ROOT / delivery.DEFAULT_ENV_FILE)
        self.assertEqual(runner.calls[0][1]["env"]["NEO4J_PASSWORD"], expected_env["NEO4J_PASSWORD"])
        self.assertEqual(runner.calls[0][1]["env"]["JWT_SECRET"], expected_env["JWT_SECRET"])

    def test_acceptance_runs_full_tests_graph_import_and_live_quality_gate(self):
        runner = RecordingRunner()
        controller = delivery.DeliveryController(ROOT, runner=runner)
        self.assertEqual(controller.acceptance(sample_pdf=Path("sample.pdf"), max_pages=30), 0)
        commands = [call[0] for call in runner.calls]
        self.assertIn([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_alphamind*.py"], commands)
        graph_commands = [command for command in commands if any("alphamind_relation_graph.py" in arg for arg in command)]
        self.assertEqual(len(graph_commands), 1)
        self.assertIn("--import-neo4j", graph_commands[0])
        gate_commands = [command for command in commands if any("alphamind_phase3_quality_gate.py" in arg for arg in command)]
        self.assertEqual(len(gate_commands), 1)
        self.assertIn("--live", gate_commands[0])
        self.assertEqual(gate_commands[0][gate_commands[0].index("--concurrency") + 1], "4")
        self.assertTrue(any(str(arg).endswith("sample.pdf") for arg in gate_commands[0]))

    def test_parser_exposes_all_delivery_commands_and_restore_confirmation(self):
        parser = delivery.build_parser()
        commands = [
            ["preflight"], ["start"], ["stop"], ["status"], ["logs"],
            ["backup"], ["verify-backup", "backup-dir"],
            ["restore", "backup-dir", "--confirm-restore", "backup-dir", "--execute-staging"],
            ["validate-restore", "alphamind-20260712T000000Z"],
            ["acceptance"],
        ]
        self.assertEqual([parser.parse_args(argv).command for argv in commands], [
            "preflight", "start", "stop", "status", "logs", "backup",
            "verify-backup", "restore", "validate-restore", "acceptance",
        ])
        restore_args = parser.parse_args(commands[7])
        self.assertEqual(restore_args.confirm_restore, "backup-dir")
        self.assertTrue(restore_args.execute_staging)


if __name__ == "__main__":
    unittest.main()
