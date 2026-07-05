#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "dataset" / "cleanup_plans"
BACKUP_DIR = ROOT / "dataset" / "cleanup_backups" / time.strftime("%Y%m%d-%H%M%S")
TEST_CANDIDATES = PLAN_DIR / "test_knowledge_candidates.csv"


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(cmd, cwd=ROOT, text=True, input=input_text, capture_output=True, encoding="utf-8", errors="replace")
    if check and res.returncode:
        print("COMMAND FAILED:", " ".join(cmd), file=sys.stderr)
        print(res.stdout, file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        raise SystemExit(res.returncode)
    return res


def psql(sql: str, *, check: bool = True) -> str:
    res = run(["docker", "exec", "-i", "WeKnora-postgres", "psql", "-U", "postgres", "-d", "WeKnora", "-v", "ON_ERROR_STOP=1", "-P", "pager=off"], input_text=sql, check=check)
    return res.stdout


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (ROOT / ".env.phase2-advanced").read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def neo4j_commit(statement: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    env = load_env()
    password = env.get("NEO4J_PASSWORD", "")
    token = base64.b64encode(("neo4j:" + password).encode()).decode()
    body = json.dumps({"statements": [{"statement": statement, "parameters": parameters or {}}]}).encode()
    req = urllib.request.Request(
        "http://localhost:7474/db/neo4j/tx/commit",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data


def qdrant_delete(collection: str, knowledge_ids: list[str], kb_ids: list[str]) -> dict[str, Any]:
    should = []
    for kid in knowledge_ids:
        should.append({"key": "knowledge_id", "match": {"value": kid}})
    for kb in kb_ids:
        should.append({"key": "knowledge_base_id", "match": {"value": kb}})
    body = json.dumps({"filter": {"should": should}}).encode()
    req = urllib.request.Request(
        f"http://localhost:6333/collections/{collection}/points/delete?wait=true",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def qdrant_count(collection: str, knowledge_ids: list[str], kb_ids: list[str]) -> int:
    should = []
    for kid in knowledge_ids:
        should.append({"key": "knowledge_id", "match": {"value": kid}})
    for kb in kb_ids:
        should.append({"key": "knowledge_base_id", "match": {"value": kb}})
    body = json.dumps({"exact": True, "filter": {"should": should}}).encode()
    req = urllib.request.Request(
        f"http://localhost:6333/collections/{collection}/points/count",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return int(json.load(resp)["result"]["count"])


def read_candidates() -> tuple[list[str], list[str], list[str]]:
    if not TEST_CANDIDATES.exists():
        raise SystemExit(f"Missing {TEST_CANDIDATES}; run scripts/alphamind_cleanup_plan.py first")
    knowledge_ids: list[str] = []
    kb_ids: list[str] = []
    file_names: list[str] = []
    with TEST_CANDIDATES.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("knowledge_id"):
                knowledge_ids.append(row["knowledge_id"])
            if row.get("kb_id"):
                kb_ids.append(row["kb_id"])
            if row.get("file_name"):
                file_names.append(row["file_name"])
    return sorted(set(knowledge_ids)), sorted(set(kb_ids)), file_names


def sql_array(values: list[str]) -> str:
    return "ARRAY[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "]::varchar[]"


def backup(knowledge_ids: list[str], kb_ids: list[str]) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEST_CANDIDATES, BACKUP_DIR / "test_knowledge_candidates.csv")
    shutil.copy2(PLAN_DIR / "knowledges_inventory.csv", BACKUP_DIR / "knowledges_inventory.csv")
    (BACKUP_DIR / "ids.json").write_text(json.dumps({"knowledge_ids": knowledge_ids, "kb_ids": kb_ids}, ensure_ascii=False, indent=2), encoding="utf-8")

    kid_arr = sql_array(knowledge_ids)
    kb_arr = sql_array(kb_ids)
    backup_sql = f"""
\\copy (SELECT * FROM knowledge_bases WHERE id = ANY({kb_arr})) TO '/tmp/alphamind_cleanup_knowledge_bases.csv' WITH CSV HEADER;
\\copy (SELECT * FROM knowledges WHERE id = ANY({kid_arr}) OR knowledge_base_id = ANY({kb_arr})) TO '/tmp/alphamind_cleanup_knowledges.csv' WITH CSV HEADER;
\\copy (SELECT * FROM chunks WHERE knowledge_id = ANY({kid_arr}) OR knowledge_base_id = ANY({kb_arr})) TO '/tmp/alphamind_cleanup_chunks.csv' WITH CSV HEADER;
\\copy (SELECT * FROM knowledge_processing_spans WHERE knowledge_id = ANY({kid_arr})) TO '/tmp/alphamind_cleanup_spans.csv' WITH CSV HEADER;
"""
    psql(backup_sql)
    for name in ["knowledge_bases", "knowledges", "chunks", "spans"]:
        cp = run(["docker", "cp", f"WeKnora-postgres:/tmp/alphamind_cleanup_{name}.csv", str(BACKUP_DIR / f"{name}.csv")], check=False)
        if cp.returncode:
            print(cp.stderr.strip())

    # Back up local test/sample files by moving them into cleanup backup; this removes them from active project paths.
    for rel in ["dataset/phase1_samples", "dataset/samples", "testdata"]:
        src = ROOT / rel
        if src.exists():
            dst = BACKUP_DIR / "local_files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))


def delete_records(knowledge_ids: list[str], kb_ids: list[str]) -> None:
    kid_arr = sql_array(knowledge_ids)
    kb_arr = sql_array(kb_ids)
    sql = f"""
BEGIN;
DELETE FROM chunks WHERE knowledge_id = ANY({kid_arr}) OR knowledge_base_id = ANY({kb_arr});
DELETE FROM knowledge_processing_spans WHERE knowledge_id = ANY({kid_arr});
DELETE FROM knowledge_tag_relations WHERE knowledge_id = ANY({kid_arr});
DELETE FROM user_kb_pins WHERE kb_id = ANY({kb_arr});
DELETE FROM kb_shares WHERE knowledge_base_id = ANY({kb_arr});
DELETE FROM knowledges WHERE id = ANY({kid_arr}) OR knowledge_base_id = ANY({kb_arr});
DELETE FROM knowledge_bases WHERE id = ANY({kb_arr}) AND id NOT IN (SELECT DISTINCT knowledge_base_id FROM knowledges);
COMMIT;
"""
    print(psql(sql))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually delete test/sample data")
    args = parser.parse_args()

    knowledge_ids, kb_ids, _ = read_candidates()
    print(f"test knowledge_ids={len(knowledge_ids)} kb_ids={len(kb_ids)}")

    collections = ["alphamind_research_embeddings_1024", "alphamind_research_embeddings_3072"]
    for collection in collections:
        try:
            print(f"qdrant_before[{collection}]={qdrant_count(collection, knowledge_ids, kb_ids)}")
        except Exception as exc:
            print(f"qdrant_count_failed[{collection}]={exc}")

    neo_before = neo4j_commit(
        "MATCH (n) WHERE n.kg IN $kids RETURN count(n) AS nodes",
        {"kids": knowledge_ids},
    )["results"][0]["data"][0]["row"][0]
    print(f"neo4j_nodes_before={neo_before}")

    if not args.execute:
        print("dry-run only; pass --execute to delete")
        return 0

    backup(knowledge_ids, kb_ids)
    print(f"backup_dir={BACKUP_DIR}")

    for collection in collections:
        try:
            print(f"qdrant_delete[{collection}]={qdrant_delete(collection, knowledge_ids, kb_ids)}")
        except Exception as exc:
            print(f"qdrant_delete_failed[{collection}]={exc}")

    neo4j_commit("MATCH (n) WHERE n.kg IN $kids DETACH DELETE n", {"kids": knowledge_ids})
    delete_records(knowledge_ids, kb_ids)

    for collection in collections:
        try:
            print(f"qdrant_after[{collection}]={qdrant_count(collection, knowledge_ids, kb_ids)}")
        except Exception as exc:
            print(f"qdrant_count_after_failed[{collection}]={exc}")
    neo_after = neo4j_commit(
        "MATCH (n) WHERE n.kg IN $kids RETURN count(n) AS nodes",
        {"kids": knowledge_ids},
    )["results"][0]["data"][0]["row"][0]
    print(f"neo4j_nodes_after={neo_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
