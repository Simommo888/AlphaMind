import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alphamind_healthcheck as healthcheck


class AlphaMindHealthcheckEnvResolutionTests(unittest.TestCase):
    def test_phase3_precision_profile_is_accepted(self):
        with patch.object(sys, "argv", ["alphamind_healthcheck.py", "--phase", "phase3-precision"]):
            self.assertEqual(healthcheck.parse_args().phase, "phase3-precision")

    def test_phase2_defaults_use_env_file_urls_and_ports_when_cli_overrides_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env.phase2-advanced"
            env_file.write_text(
                "\n".join(
                    [
                        "WEKNORA_BASE_URL=http://localhost:18080",
                        "WEKNORA_API_KEY=sk-test",
                        "QDRANT_REST_URL=http://localhost:16333",
                        "QDRANT_HOST=qdrant-alt",
                        "QDRANT_INTERNAL_URL=http://qdrant-alt:6333",
                        "WEKNORA_APP_CONTAINER=WeKnora-app-alt",
                        "NEO4J_HTTP_URL=http://localhost:17474",
                        "NEO4J_USERNAME=neo4j",
                        "NEO4J_PASSWORD=alphamind-test-password",
                        "NEO4J_DATABASE=neo4j",
                        "MINIO_PUBLIC_URL=http://localhost:19000",
                        "LANGFUSE_PUBLIC_URL=http://localhost:13000",
                        "RETRIEVE_DRIVER=qdrant",
                        "STORAGE_TYPE=minio",
                        "NEO4J_ENABLE=true",
                        "LLM_MODEL_NAME=qwen-test",
                        "EMBEDDING_MODEL_NAME=embedding-test",
                        "RERANK_MODEL_NAME=rerank-test",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            captured = {}

            def fake_json_http_check(name, url, **kwargs):
                captured.setdefault("json", {})[name] = url
                return healthcheck.CheckResult(name, "pass", kwargs.get("required", False), "ok")

            def fake_auth_api(name, url, **kwargs):
                captured.setdefault("auth", {})[name] = url
                return healthcheck.CheckResult(name, "pass", False, "ok")

            def fake_qdrant(url, **kwargs):
                captured["qdrant_url"] = url
                return healthcheck.CheckResult("qdrant_collections", "pass", kwargs.get("required", False), "ok")

            def fake_qdrant_internal(app_container, internal_url, **kwargs):
                captured["app_container"] = app_container
                captured["qdrant_internal_url"] = internal_url
                return healthcheck.CheckResult("qdrant_internal", "pass", kwargs.get("required", False), "ok")

            def fake_neo4j(url, **kwargs):
                captured["neo4j_url"] = url
                captured["neo4j_user"] = kwargs.get("username")
                captured["neo4j_password"] = kwargs.get("password")
                captured["neo4j_database"] = kwargs.get("database")
                return healthcheck.CheckResult("neo4j_http", "pass", kwargs.get("required", False), "ok")

            def fake_minio(url, **kwargs):
                captured["minio_url"] = url
                return healthcheck.CheckResult("minio_live", "pass", kwargs.get("required", False), "ok")

            def fake_langfuse(url, **kwargs):
                captured["langfuse_url"] = url
                return healthcheck.CheckResult("langfuse_http", "pass", kwargs.get("required", False), "ok")

            argv = [
                "alphamind_healthcheck.py",
                "--phase",
                "phase2-advanced",
                "--env-file",
                str(env_file),
            ]
            with patch.dict(
                os.environ,
                {"NEO4J_USERNAME": "stale-user", "NEO4J_PASSWORD": "stale-password", "NEO4J_DATABASE": "stale-db"},
                clear=True,
            ), patch.object(sys, "argv", argv), \
                patch.object(healthcheck, "json_http_check", side_effect=fake_json_http_check), \
                patch.object(healthcheck, "check_auth_api", side_effect=fake_auth_api), \
                patch.object(healthcheck, "check_qdrant", side_effect=fake_qdrant), \
                patch.object(healthcheck, "check_qdrant_internal", side_effect=fake_qdrant_internal), \
                patch.object(healthcheck, "check_neo4j", side_effect=fake_neo4j), \
                patch.object(healthcheck, "check_minio", side_effect=fake_minio), \
                patch.object(healthcheck, "check_langfuse", side_effect=fake_langfuse), \
                patch.object(healthcheck, "print_table"):
                exit_code = healthcheck.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["json"]["weknora_health"], "http://localhost:18080/health")
            self.assertEqual(captured["auth"]["weknora_system_info"], "http://localhost:18080/api/v1/system/info")
            self.assertEqual(captured["auth"]["weknora_parser_engines"], "http://localhost:18080/api/v1/system/parser-engines")
            self.assertEqual(captured["auth"]["weknora_storage_status"], "http://localhost:18080/api/v1/system/storage-engine-status")
            self.assertEqual(captured["qdrant_url"], "http://localhost:16333")
            self.assertEqual(captured["app_container"], "WeKnora-app-alt")
            self.assertEqual(captured["qdrant_internal_url"], "http://qdrant-alt:6333")
            self.assertEqual(captured["neo4j_url"], "http://localhost:17474")
            self.assertEqual(captured["neo4j_user"], "neo4j")
            self.assertEqual(captured["neo4j_password"], "alphamind-test-password")
            self.assertEqual(captured["neo4j_database"], "neo4j")
            self.assertEqual(captured["minio_url"], "http://localhost:19000")
            self.assertEqual(captured["langfuse_url"], "http://localhost:13000")

    def test_phase2_defaults_build_localhost_urls_from_env_file_ports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env.phase2-advanced"
            env_file.write_text(
                "\n".join(
                    [
                        "WEKNORA_BASE_URL=http://localhost:18080",
                        "WEKNORA_API_KEY=sk-test",
                        "QDRANT_REST_PORT=16333",
                        "QDRANT_HOST=qdrant",
                        "NEO4J_HTTP_PORT=17474",
                        "NEO4J_USERNAME=neo4j",
                        "NEO4J_PASSWORD=alphamind-test-password",
                        "MINIO_PORT=19000",
                        "LANGFUSE_PORT=13000",
                        "RETRIEVE_DRIVER=qdrant",
                        "STORAGE_TYPE=minio",
                        "NEO4J_ENABLE=true",
                        "LLM_MODEL_NAME=qwen-test",
                        "EMBEDDING_MODEL_NAME=embedding-test",
                        "RERANK_MODEL_NAME=rerank-test",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_qdrant_from_port(url, **kwargs):
                captured["qdrant_url"] = url
                return healthcheck.CheckResult("qdrant_collections", "pass", kwargs.get("required", False), "ok")

            def fake_neo4j_from_port(url, **kwargs):
                captured["neo4j_url"] = url
                return healthcheck.CheckResult("neo4j_http", "pass", kwargs.get("required", False), "ok")

            def fake_minio_from_port(url, **kwargs):
                captured["minio_url"] = url
                return healthcheck.CheckResult("minio_live", "pass", kwargs.get("required", False), "ok")

            def fake_langfuse_from_port(url, **kwargs):
                captured["langfuse_url"] = url
                return healthcheck.CheckResult("langfuse_http", "pass", kwargs.get("required", False), "ok")

            with patch.dict(os.environ, {}, clear=True), patch.object(
                sys,
                "argv",
                ["alphamind_healthcheck.py", "--phase", "phase2-advanced", "--env-file", str(env_file)],
            ), patch.object(healthcheck, "json_http_check", return_value=healthcheck.CheckResult("weknora_health", "pass", True, "ok")), patch.object(
                healthcheck,
                "check_auth_api",
                return_value=healthcheck.CheckResult("auth", "pass", False, "ok"),
            ), patch.object(
                healthcheck,
                "check_qdrant",
                side_effect=fake_qdrant_from_port,
            ), patch.object(
                healthcheck,
                "check_qdrant_internal",
                return_value=healthcheck.CheckResult("qdrant_internal", "pass", True, "ok"),
            ), patch.object(
                healthcheck,
                "check_neo4j",
                side_effect=fake_neo4j_from_port,
            ), patch.object(
                healthcheck,
                "check_minio",
                side_effect=fake_minio_from_port,
            ), patch.object(
                healthcheck,
                "check_langfuse",
                side_effect=fake_langfuse_from_port,
            ), patch.object(healthcheck, "print_table"):
                exit_code = healthcheck.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["qdrant_url"], "http://localhost:16333")
            self.assertEqual(captured["neo4j_url"], "http://localhost:17474")
            self.assertEqual(captured["minio_url"], "http://localhost:19000")
            self.assertEqual(captured["langfuse_url"], "http://localhost:13000")

    def test_phase3_check_models_runs_local_model_api_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env.phase3-precision"
            env_file.write_text(
                "\n".join([
                    "WEKNORA_BASE_URL=http://localhost:18080",
                    "RETRIEVE_DRIVER=none",
                    "STORAGE_TYPE=local",
                    "NEO4J_ENABLE=false",
                    "LLM_MODEL_NAME=qwen3-14b",
                    "LLM_BASE_URL=http://host.docker.internal:8000/v1",
                    "EMBEDDING_MODEL_NAME=embedding-test",
                    "RERANK_MODEL_NAME=rerank-test",
                ]) + "\n",
                encoding="utf-8",
            )
            completed = __import__("subprocess").CompletedProcess(["probe"], 0, "ok", "")
            with patch.object(
                sys,
                "argv",
                [
                    "alphamind_healthcheck.py",
                    "--phase",
                    "phase3-precision",
                    "--env-file",
                    str(env_file),
                    "--check-models",
                ],
            ), patch.object(healthcheck, "json_http_check", return_value=healthcheck.CheckResult("weknora_health", "pass", True, "ok")), patch.object(
                healthcheck, "check_auth_api", return_value=healthcheck.CheckResult("auth", "skip", False, "skip")
            ), patch.object(healthcheck, "check_qdrant", return_value=healthcheck.CheckResult("qdrant", "pass", False, "ok")), patch.object(
                healthcheck, "check_qdrant_internal", return_value=healthcheck.CheckResult("qdrant_internal", "pass", True, "ok")
            ), patch.object(healthcheck, "check_neo4j", return_value=healthcheck.CheckResult("neo4j", "pass", True, "ok")), patch.object(
                healthcheck, "check_minio", return_value=healthcheck.CheckResult("minio", "pass", True, "ok")
            ), patch.object(healthcheck, "check_langfuse", return_value=healthcheck.CheckResult("langfuse", "skip", False, "skip")), patch.object(
                healthcheck.subprocess, "run", return_value=completed
            ) as run, patch.object(healthcheck, "print_table"):
                self.assertEqual(healthcheck.main(), 0)

            argv = run.call_args.args[0]
            self.assertIn("alphamind_phase1_model_check.py", " ".join(argv))
            self.assertIn(str(env_file), argv)


if __name__ == "__main__":
    unittest.main()
