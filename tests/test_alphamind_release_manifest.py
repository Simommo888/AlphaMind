import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "alphamind_release_manifest.py"
spec = importlib.util.spec_from_file_location("alphamind_release_manifest", MODULE_PATH)
release_manifest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release_manifest
spec.loader.exec_module(release_manifest)


class AlphaMindReleaseManifestTests(unittest.TestCase):
    def init_repo(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    def fixture(self, root: Path):
        files = {
            "config/runtime.json": b'{"mode":"test"}\n',
            "dataset/eval.json": b'{"queries":["q1"]}\n',
            "dataset/acceptance.json": b'{"status":"pass"}\n',
            "tests/test_release.py": b"def test_placeholder(): pass\n",
        }
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        specs = (
            release_manifest.ArtifactSpec("config/runtime.json", "configuration", "configuration"),
            release_manifest.ArtifactSpec("dataset/eval.json", "evaluation_dataset", "dataset"),
            release_manifest.ArtifactSpec("dataset/acceptance.json", "acceptance_evidence", "acceptance"),
            release_manifest.ArtifactSpec("tests/test_release.py", "automated_test"),
        )
        return files, specs

    def test_default_inventory_contains_every_final_review_p1_artifact_and_all_tests(self):
        specs = release_manifest.artifact_specs(ROOT)
        paths = {item.path for item in specs}
        required = {
            ".env.phase3-precision.example",
            "docker-compose.yml",
            "docker-compose.phase2-advanced.yml",
            "docker-compose.phase3-precision.yml",
            "config/builtin_models.phase3-precision.yaml",
            "dataset/alphamind_process_config_phase3_precision.json",
            "dataset/alphamind_retrieval_config_phase3_precision.json",
            "dataset/alphamind_financial_lexicon_phase3.json",
            "dataset/phase3_eval_queries.json",
            "docs/ALPHAMIND_KB_CONFIG.phase3-precision.json",
            "scripts/alphamind_bulk_ingest.py",
            "scripts/alphamind_healthcheck.py",
            "delivery/requirements-phase3.txt",
        }
        self.assertTrue(required <= paths, required - paths)
        expected_tests = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests").glob("test_alphamind*.py")
        }
        self.assertEqual({item.path for item in specs if item.role == "automated_test"}, expected_tests)

    def test_build_manifest_records_full_hashes_git_state_aggregate_hashes_run_id_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, specs = self.fixture(root)
            commit = self.init_repo(root)

            manifest = release_manifest.build_manifest(
                root,
                specs=specs,
                acceptance_path="dataset/acceptance.json",
                output_path="delivery/manifest.json",
            )

            records = {item["path"]: item for item in manifest["files"]}
            self.assertEqual(records["config/runtime.json"]["sha256"], hashlib.sha256(files["config/runtime.json"]).hexdigest())
            self.assertEqual(records["dataset/eval.json"]["size"], len(files["dataset/eval.json"]))
            self.assertEqual(manifest["git"]["commit"], commit)
            self.assertFalse(manifest["git"]["dirty"])
            self.assertRegex(manifest["hashes"]["configuration_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["hashes"]["dataset_sha256"], r"^[0-9a-f]{64}$")
            acceptance_sha = hashlib.sha256(files["dataset/acceptance.json"]).hexdigest()
            self.assertEqual(manifest["acceptance"]["run_id"], f"phase3-acceptance-{acceptance_sha[:16]}")
            self.assertEqual(manifest["acceptance"]["status"], "pass")
            self.assertIn("python", manifest["dependencies"]["tools"])
            self.assertIn("cuda_tuning", manifest["inherited_requirements"])
            self.assertIn("customer_data_redaction", manifest["inherited_requirements"])

    def test_generate_then_verify_passes_and_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, specs = self.fixture(root)
            self.init_repo(root)
            output = root / "delivery" / "manifest.json"

            release_manifest.generate_manifest(
                root,
                output,
                specs=specs,
                acceptance_path="dataset/acceptance.json",
            )
            result = release_manifest.verify_manifest(root, output, specs=specs)
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.checked_files, 4)

            (root / "dataset" / "eval.json").write_text('{"queries":["tampered"]}\n', encoding="utf-8")
            result = release_manifest.verify_manifest(root, output, specs=specs)
            self.assertFalse(result.ok)
            self.assertTrue(any("sha256" in error for error in result.errors))

    def test_verify_rejects_path_traversal_and_incomplete_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, specs = self.fixture(root)
            self.init_repo(root)
            output = root / "delivery" / "manifest.json"
            release_manifest.generate_manifest(root, output, specs=specs, acceptance_path="dataset/acceptance.json")
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["files"] = payload["files"][1:]
            payload["files"][0]["path"] = "../outside"
            output.write_text(json.dumps(payload), encoding="utf-8")

            result = release_manifest.verify_manifest(root, output, specs=specs)
            self.assertFalse(result.ok)
            self.assertTrue(any("unsafe path" in error for error in result.errors))
            self.assertTrue(any("inventory" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
