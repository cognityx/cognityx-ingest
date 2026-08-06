"""Verify the installed v3.2 fixture scaffold.

This script exists for T00 review and future task handoffs. Its core algorithm
checks three immutable boundaries: source-truth hashes from the fixture
manifest, the repository installation manifest, and the reused base PDF plus
tracked design-input ZIP when a repository root is supplied. Reviewers and CI
tests call it from the repository root; production ingest code does not use it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    """Return the SHA-256 digest for one frozen fixture or design-input file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_sha256_manifest(path: Path) -> dict[str, str]:
    """Read a two-column SHA-256 manifest into relative path -> digest records."""
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        sums[rel] = digest
    return sums


def main() -> int:
    """Validate the installed fixture tree and optional repository-level inputs."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parent
    manifest = json.loads((fixture / "fixture_manifest.json").read_text(encoding="utf-8"))

    errors: list[str] = []
    for item in manifest["synthetic_source_truth"]:
        path = fixture / item["path"]
        if not path.is_file():
            errors.append(f"missing delta source: {path}")
        elif sha256(path) != item["sha256"]:
            errors.append(f"delta source hash mismatch: {path}")

    sums = _read_sha256_manifest(fixture / "repo_install_manifest.sha256sums.txt")
    for rel, digest in sums.items():
        path = fixture / rel
        if not path.is_file():
            errors.append(f"missing installed file: {rel}")
        elif sha256(path) != digest:
            errors.append(f"installed file hash mismatch: {rel}")

    if args.repo_root:
        repo = args.repo_root.resolve()
        base = manifest["base_fixture"]
        base_path = repo / base["path"]
        if not base_path.is_file():
            errors.append(f"missing authoritative base fixture: {base_path}")
        elif sha256(base_path) != base["sha256"]:
            errors.append(
                f"authoritative base fixture hash mismatch: {base_path}; "
                f"expected {base['sha256']}, got {sha256(base_path)}"
            )
        design_root = repo / "design_input" / "v3_2"
        zip_path = design_root / "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip"
        sidecar = design_root / "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip.sha256"
        design_doc = design_root / "Cognityx_Ingest_v3_2_Adaptive_Segmentation_Source_Graph_and_Provenance_Address_Plan.docx"
        for path in (zip_path, sidecar, design_doc):
            if not path.is_file():
                errors.append(f"missing tracked design input: {path}")
        if zip_path.is_file() and sidecar.is_file():
            expected = sidecar.read_text(encoding="utf-8").split()[0]
            actual = sha256(zip_path)
            if actual != expected:
                errors.append(
                    f"tracked fixture ZIP hash mismatch: {zip_path}; "
                    f"expected {expected}, got {actual}"
                )

    if errors:
        print("Fixture verification FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Fixture verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
