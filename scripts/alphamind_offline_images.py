#!/usr/bin/env python3
"""Export, verify, and load the digest-pinned AlphaMind delivery images."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

PINNED_IMAGES = (
    "wechatopenai/weknora-app@sha256:748097ccb5e9a66d46cfb94de9cd200de5a65dbd2ad7de917a5db0ffd4e56681",
    "wechatopenai/weknora-ui@sha256:da9a3a0a939ddd1ed6f4b510b74a0562a21b898439f907cd6ee529a3680283d8",
    "wechatopenai/weknora-docreader@sha256:7f09b7581150cec73a206460c859abfd3671ae13697450d6b533db5484c1276c",
    "paradedb/paradedb@sha256:af585013f97f622715de01e48d00558f7edf17055d7b40deafc9f98ca8d99a56",
    "redis@sha256:c9d92d840fd011c908f040592857c724ae6d877f2aba5c40ad963276507386b2",
    "qdrant/qdrant@sha256:dab6de32f7b2cc599985a7c764db3e8b062f70508fb85ca074aa856f829bf335",
    "neo4j@sha256:155c8aad10d5c838bc3bbc476c0418779086547822acb214ec5e3d49ba336907",
    "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e",
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


class OfflineBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    errors: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(runner: Runner, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return runner(
        list(argv), text=True, capture_output=True, encoding="utf-8",
        errors="replace", shell=False,
    )


def export_bundle(output_dir: Path | str, *, runner: Runner = subprocess.run) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    for image in PINNED_IMAGES:
        checked = _run(runner, ["docker", "image", "inspect", image])
        if checked.returncode:
            raise OfflineBundleError(f"required image unavailable: {image}")
    archive = output_dir / "images.tar"
    saved = _run(runner, ["docker", "save", "-o", str(archive.resolve()), *PINNED_IMAGES])
    if saved.returncode or not archive.is_file():
        raise OfflineBundleError("docker save failed")
    manifest = {
        "schema_version": "alphamind.offline-images.v1",
        "images": list(PINNED_IMAGES),
        "archive": {
            "file": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def verify_bundle(output_dir: Path | str) -> VerificationResult:
    output_dir = Path(output_dir)
    errors: list[str] = []
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return VerificationResult(False, (f"manifest unreadable: {exc}",))
    if manifest.get("schema_version") != "alphamind.offline-images.v1":
        errors.append("unsupported schema_version")
    if manifest.get("images") != list(PINNED_IMAGES):
        errors.append("image list does not match pinned delivery images")
    archive_meta = manifest.get("archive")
    if not isinstance(archive_meta, dict) or archive_meta.get("file") != "images.tar":
        errors.append("invalid archive metadata")
        return VerificationResult(False, tuple(errors))
    archive = output_dir / "images.tar"
    if not archive.is_file():
        errors.append("images.tar missing")
    else:
        if archive_meta.get("size") != archive.stat().st_size:
            errors.append("archive size mismatch")
        if archive_meta.get("sha256") != sha256_file(archive):
            errors.append("archive sha256 mismatch")
    return VerificationResult(not errors, tuple(errors))


def load_bundle(output_dir: Path | str, *, runner: Runner = subprocess.run) -> None:
    output_dir = Path(output_dir)
    verified = verify_bundle(output_dir)
    if not verified.ok:
        raise OfflineBundleError("; ".join(verified.errors))
    loaded = _run(runner, ["docker", "load", "-i", str((output_dir / "images.tar").resolve())])
    if loaded.returncode:
        raise OfflineBundleError("docker load failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "verify", "load"):
        child = sub.add_parser(name)
        child.add_argument("bundle_dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            export_bundle(args.bundle_dir)
        elif args.command == "verify":
            result = verify_bundle(args.bundle_dir)
            if not result.ok:
                raise OfflineBundleError("; ".join(result.errors))
        else:
            load_bundle(args.bundle_dir)
    except OfflineBundleError as exc:
        print(f"offline bundle FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"offline bundle {args.command} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
