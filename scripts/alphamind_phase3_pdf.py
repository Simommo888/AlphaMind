#!/usr/bin/env python3
"""AlphaMind Phase3 complex financial PDF -> lineage-rich Markdown pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass
class TableFragment:
    page: int
    rows: list[list[str]]
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class StitchedTable:
    table_id: str
    pages: list[int]
    rows: list[list[str]]
    bboxes: list[tuple[float, float, float, float] | None] = field(default_factory=list)


@dataclass
class Footnote:
    page: int
    text: str


def clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def clean_rows(rows: list[list[Any]]) -> list[list[str]]:
    cleaned = [[clean_cell(cell) for cell in row] for row in rows if row]
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    return [row + [""] * (width - len(row)) for row in cleaned]


def _header_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    scores = [SequenceMatcher(None, clean_cell(a).casefold(), clean_cell(b).casefold()).ratio() for a, b in zip(left, right)]
    return sum(scores) / len(scores)


def stitch_table_fragments(fragments: list[TableFragment], threshold: float = 0.78) -> list[StitchedTable]:
    stitched: list[StitchedTable] = []
    for fragment in fragments:
        rows = clean_rows(fragment.rows)
        if not rows:
            continue
        if stitched:
            current = stitched[-1]
            consecutive = fragment.page == current.pages[-1] + 1
            same_width = len(rows[0]) == len(current.rows[0])
            similarity = _header_similarity(current.rows[0], rows[0]) if same_width else 0.0
            if consecutive and same_width and similarity >= threshold:
                current.pages.append(fragment.page)
                current.bboxes.append(fragment.bbox)
                current.rows.extend(rows[1:] if similarity >= threshold else rows)
                continue
        stitched.append(StitchedTable(f"table-{len(stitched)+1:04d}", [fragment.page], rows, [fragment.bbox]))
    return stitched


_FOOTNOTE_RE = re.compile(r"^(?:注\s*[:：]|[（(]?\d{1,3}[）)]\s*[、.．:]?|[*＊※])\s*\S+")


def extract_footnotes(text: str, page: int) -> list[Footnote]:
    notes: list[Footnote] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and _FOOTNOTE_RE.match(line):
            notes.append(Footnote(page, line))
    return notes


def _numeric_tokens(text: str) -> Counter[str]:
    normalized = text.replace("（", "(").replace("）", ")").replace("，", " ")
    normalized = re.sub(r"(?<=\d),(?=\d)", "", normalized)
    tokens = []
    for match in re.finditer(r"\(?-?\d+(?:\.\d+)?%?\)?", normalized):
        token = match.group(0)
        tokens.append(token)
    return Counter(tokens)


def numeric_preservation_ratio(source: str, output: str) -> float:
    expected = _numeric_tokens(source)
    if not expected:
        return 1.0
    actual = _numeric_tokens(output)
    preserved = sum(min(count, actual.get(token, 0)) for token, count in expected.items())
    return preserved / sum(expected.values())


def _escape_md(cell: str) -> str:
    return clean_cell(cell).replace("|", "\\|").replace("\n", "<br>")


def table_to_markdown(table: StitchedTable) -> str:
    rows = clean_rows(table.rows)
    if not rows:
        return ""
    width = len(rows[0])
    lines = ["| " + " | ".join(_escape_md(cell) for cell in rows[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    lines.extend("| " + " | ".join(_escape_md(cell) for cell in row) + " |" for row in rows[1:])
    return "\n".join(lines)


def render_markdown(
    doc_id: str,
    title: str,
    tables: list[StitchedTable],
    footnotes: list[Footnote],
    page_texts: list[tuple[int, str]] | None = None,
) -> str:
    parts = [f"# {title}", "", f'<!-- alphamind source_doc_id="{doc_id}" parser="phase3-pymupdf-layout" -->', ""]
    for page, text in page_texts or []:
        parts.extend([f'<!-- page source_doc_id="{doc_id}" page="{page}" -->', f"## 第 {page} 页", "", text.strip(), ""])
    if tables:
        parts.extend(["# 结构化表格", ""])
    for table in tables:
        page_range = str(table.pages[0]) if len(table.pages) == 1 else f"{table.pages[0]}-{table.pages[-1]}"
        parts.extend([
            f'<!-- table source_doc_id="{doc_id}" table_id="{table.table_id}" pages="{page_range}" -->',
            f"## 表格 {table.table_id}（页 {page_range}）",
            "",
            table_to_markdown(table),
            "",
        ])
    if footnotes:
        parts.extend(["# 财务脚注", ""])
    for index, note in enumerate(footnotes, 1):
        parts.extend([
            f'<!-- footnote source_doc_id="{doc_id}" footnote_id="footnote-{index:04d}" page="{note.page}" -->',
            f"- [第 {note.page} 页] {note.text}",
            "",
        ])
    return "\n".join(parts).rstrip() + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _doc_id(path: Path, digest: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", path.stem).strip("_")[:100] or "document"
    return f"alphamind_phase3_{slug}_{digest[:12]}"


def parse_pdf(path: Path, *, max_pages: int = 0) -> tuple[str, dict[str, Any]]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required: pip install pymupdf") from exc
    digest = _sha256(path)
    doc_id = _doc_id(path, digest)
    fragments: list[TableFragment] = []
    footnotes: list[Footnote] = []
    page_texts: list[tuple[int, str]] = []
    source_texts: list[str] = []
    table_errors: list[dict[str, Any]] = []
    document = fitz.open(path)
    page_count = len(document)
    selected_count = min(page_count, max_pages) if max_pages > 0 else page_count
    for page_index in range(selected_count):
        page = document[page_index]
        page_number = page_index + 1
        text = page.get_text("text", sort=True)
        source_texts.append(text)
        page_texts.append((page_number, text))
        footnotes.extend(extract_footnotes(text, page_number))
        try:
            finder = getattr(page, "find_tables", None)
            tables = finder().tables if finder else []
            for table in tables:
                bbox = tuple(float(v) for v in table.bbox) if getattr(table, "bbox", None) else None
                rows = clean_rows(table.extract())
                if rows:
                    fragments.append(TableFragment(page_number, rows, bbox))
        except Exception as exc:
            table_errors.append({"page": page_number, "error": f"{type(exc).__name__}: {exc}"})
    tables = stitch_table_fragments(fragments)
    markdown = render_markdown(doc_id, path.stem, tables, footnotes, page_texts)
    source = "\n".join(source_texts)
    quality = {
        "numeric_preservation_ratio": round(numeric_preservation_ratio(source, markdown), 6),
        "pages_with_text": sum(1 for _, text in page_texts if text.strip()),
        "pages_processed": selected_count,
        "tables_detected": len(fragments),
        "tables_after_stitching": len(tables),
        "cross_page_tables": sum(1 for table in tables if len(table.pages) > 1),
        "footnotes_detected": len(footnotes),
        "table_detection_errors": len(table_errors),
    }
    manifest = {
        "schema_version": "alphamind.phase3.pdf.v1",
        "source_path": str(path.resolve()),
        "source_sha256": digest,
        "source_doc_id": doc_id,
        "parser": "phase3-pymupdf-layout",
        "page_count": page_count,
        "quality": quality,
        "tables": [asdict(table) for table in tables],
        "footnotes": [asdict(note) for note in footnotes],
        "errors": table_errors,
    }
    return markdown, manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a complex financial PDF to lineage-rich Markdown.")
    parser.add_argument("pdf")
    parser.add_argument("--out-dir", default="dataset/phase3_runs/pdf")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--min-numeric-preservation", type=float, default=0.98)
    parser.add_argument("--require-table", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.pdf)
    if not source.is_file():
        print(f"PDF not found: {source}", file=sys.stderr)
        return 2
    markdown, manifest = parse_pdf(source, max_pages=args.max_pages)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", source.stem).strip("_")[:100]
    md_path = out_dir / f"{stem}.phase3.md"
    manifest_path = out_dir / f"{stem}.phase3.manifest.json"
    md_path.write_text(markdown, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quality = manifest["quality"]
    failures = []
    if quality["pages_processed"] <= 0 or quality["pages_with_text"] <= 0:
        failures.append("no readable pages")
    if quality["numeric_preservation_ratio"] < args.min_numeric_preservation:
        failures.append(f"numeric preservation {quality['numeric_preservation_ratio']:.3f} < {args.min_numeric_preservation:.3f}")
    if args.require_table and quality["tables_after_stitching"] <= 0:
        failures.append("no tables detected")
    print(json.dumps({"markdown": str(md_path), "manifest": str(manifest_path), "quality": quality, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
