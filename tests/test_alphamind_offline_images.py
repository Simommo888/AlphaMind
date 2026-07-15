import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "alphamind_offline_images.py"
spec = importlib.util.spec_from_file_location("alphamind_offline_images", MODULE)
offline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = offline
spec.loader.exec_module(offline)


class ExportRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:2] == ["docker", "save"]:
            output = Path(argv[argv.index("-o") + 1])
            output.write_bytes(b"pinned-images")
        return subprocess.CompletedProcess(argv, 0, "ok", "")


class OfflineImagesTests(unittest.TestCase):
    def test_all_delivery_images_are_digest_pinned(self):
        self.assertEqual(len(offline.PINNED_IMAGES), 8)
        self.assertTrue(all("@sha256:" in image for image in offline.PINNED_IMAGES))

    def test_export_writes_hashed_manifest_and_argv_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            runner = ExportRunner()
            manifest = offline.export_bundle(output, runner=runner)
            self.assertEqual(manifest["schema_version"], "alphamind.offline-images.v1")
            self.assertEqual(manifest["images"], list(offline.PINNED_IMAGES))
            self.assertEqual(len(manifest["archive"]["sha256"]), 64)
            self.assertTrue((output / "images.tar").is_file())
            self.assertTrue(all(call[1].get("shell") is False for call in runner.calls))
            self.assertTrue(offline.verify_bundle(output).ok)

    def test_load_refuses_tampered_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            offline.export_bundle(output, runner=ExportRunner())
            (output / "images.tar").write_bytes(b"tampered")
            result = offline.verify_bundle(output)
            self.assertFalse(result.ok)
            runner = ExportRunner()
            with self.assertRaises(offline.OfflineBundleError):
                offline.load_bundle(output, runner=runner)
            self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
