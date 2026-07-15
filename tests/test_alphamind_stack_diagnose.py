import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alphamind_stack_diagnose as diagnose


class AlphaMindStackDiagnoseEnvResolutionTests(unittest.TestCase):
    def test_phase2_diagnose_loads_env_file_for_compose_and_port_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (project / "docker-compose.phase2-advanced.yml").write_text("services: {}\n", encoding="utf-8")
            (project / ".env.phase2-advanced").write_text(
                "\n".join(
                    [
                        "FRONTEND_PORT=18088",
                        "APP_PORT=18080",
                        "QDRANT_REST_PORT=16333",
                        "NEO4J_HTTP_PORT=17474",
                        "NEO4J_BOLT_PORT=17687",
                        "MINIO_PORT=19000",
                        "MINIO_CONSOLE_PORT=19001",
                        "PHASE2_ENV_FILE=.env.phase2-advanced",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            captured = {"commands": [], "ports": [], "env_seen": {}}

            def fake_check_command(name, command, cwd, timeout=30):
                captured["commands"].append(command)
                if name == "compose_config":
                    captured["env_seen"] = {
                        "PHASE2_ENV_FILE": os.environ.get("PHASE2_ENV_FILE"),
                        "FRONTEND_PORT": os.environ.get("FRONTEND_PORT"),
                    }
                return diagnose.DiagnosticResult(name, "pass", "ok")

            def fake_get_compose_ps(cwd, *, compose_files, profiles):
                return diagnose.DiagnosticResult("compose_ps", "warn", "no containers returned"), []

            def fake_check_port(name, host, port, timeout):
                captured["ports"].append((name, host, port))
                return diagnose.DiagnosticResult(f"port_{name}", "pass", f"{host}:{port} reachable")

            def fake_inspect_logs(cwd, service, lines, *, compose_files, profiles):
                return diagnose.DiagnosticResult(f"logs_{service}", "pass", "ok"), ""

            argv = ["alphamind_stack_diagnose.py", "--phase", "phase2-advanced", "--project-dir", str(project)]
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", argv), \
                patch.object(diagnose, "check_command", side_effect=fake_check_command), \
                patch.object(diagnose, "get_compose_ps", side_effect=fake_get_compose_ps), \
                patch.object(diagnose, "check_port", side_effect=fake_check_port), \
                patch.object(diagnose, "inspect_logs", side_effect=fake_inspect_logs), \
                patch.object(diagnose, "print_results"):
                exit_code = diagnose.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["env_seen"]["PHASE2_ENV_FILE"], ".env.phase2-advanced")
            self.assertEqual(captured["env_seen"]["FRONTEND_PORT"], "18088")
            compose_commands = [cmd for cmd in captured["commands"] if "compose" in cmd]
            self.assertTrue(compose_commands)
            self.assertEqual(
                captured["ports"],
                [
                    ("frontend", "127.0.0.1", 18088),
                    ("app", "127.0.0.1", 18080),
                    ("qdrant_rest", "127.0.0.1", 16333),
                    ("neo4j_http", "127.0.0.1", 17474),
                    ("neo4j_bolt", "127.0.0.1", 17687),
                    ("minio_s3", "127.0.0.1", 19000),
                    ("minio_console", "127.0.0.1", 19001),
                ],
            )


if __name__ == "__main__":
    unittest.main()
