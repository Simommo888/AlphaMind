#!/usr/bin/env python3
from __future__ import annotations

import csv
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dataset" / "cleanup_plans"
OUT.mkdir(parents=True, exist_ok=True)

QUERY = r"""
COPY (
SELECT kb.id AS kb_id, kb.name AS kb_name, kb.deleted_at AS kb_deleted_at,
       k.id AS knowledge_id, k.title, k.file_name, k.file_type, k.parse_status,
       k.metadata->>'doc_id' AS doc_id,
       k.metadata->>'file_path' AS metadata_file_path,
       k.metadata->>'ingest_channel' AS ingest_channel,
       k.created_at
FROM knowledge_bases kb
LEFT JOIN knowledges k ON k.knowledge_base_id=kb.id
ORDER BY kb.created_at, k.created_at
) TO STDOUT WITH CSV HEADER
"""


def main() -> int:
    res = subprocess.run(
        ["docker", "exec", "WeKnora-postgres", "psql", "-U", "postgres", "-d", "WeKnora", "-c", QUERY],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode:
        print(res.stderr)
        return res.returncode

    (OUT / "knowledges_inventory.csv").write_text(res.stdout, encoding="utf-8")
    rows = list(csv.DictReader(res.stdout.splitlines()))
    test: list[dict[str, str]] = []
    real: list[dict[str, str]] = []

    for row in rows:
        if not row.get("knowledge_id"):
            continue
        file_path = (row.get("metadata_file_path") or "").replace("\\", "/").lower()
        file_name = row.get("file_name") or ""
        kb_name = row.get("kb_name") or ""
        channel = row.get("ingest_channel") or ""
        kb_deleted = bool(row.get("kb_deleted_at"))
        reasons: list[str] = []

        if kb_deleted:
            reasons.append("deleted_kb")
        if "Phase1 Basic" in kb_name:
            reasons.append("phase1_basic_sample_kb")
        if file_name.startswith("sample_") or "phase1_samples" in file_path or "/testdata/" in file_path:
            reasons.append("sample_or_test_file")
        if channel in {"alphamind-phase2-acceptance", "alphamind-phase2-quality-gt"}:
            reasons.append(channel)

        if reasons:
            row["delete_reason"] = ";".join(reasons)
            test.append(row)
        else:
            real.append(row)

    if rows:
        test_fields = list(rows[0].keys()) + ["delete_reason"]
        with (OUT / "test_knowledge_candidates.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=test_fields)
            writer.writeheader()
            writer.writerows(test)
        with (OUT / "real_knowledge_keep.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(real)

    print(f"inventory={OUT / 'knowledges_inventory.csv'}")
    print(f"test_candidates={len(test)} file={OUT / 'test_knowledge_candidates.csv'}")
    for row in test:
        print(row["kb_name"], row["knowledge_id"], row["file_name"], row["delete_reason"])
    print(f"real_keep={len(real)} file={OUT / 'real_knowledge_keep.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
