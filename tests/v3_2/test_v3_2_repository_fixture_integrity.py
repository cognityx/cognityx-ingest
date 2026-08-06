"""Repository fixture integrity checks for v3.2 T00.

The installed fixture has a repository manifest that differs from the original
ZIP manifest. These tests ensure the installed tree, reused base PDF, and
tracked design inputs can be verified in a fresh checkout. They are used by
reviewers and future agents before starting T01.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    """Compute SHA-256 for frozen binary and text fixture inputs."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_repository_install_manifest_covers_installed_files(v3_2_fixture_root: Path) -> None:
    """Validate every file listed in the repository installation manifest."""
    manifest = (
        v3_2_fixture_root / "repo_install_manifest.sha256sums.txt"
    ).read_text(encoding="utf-8").splitlines()
    assert manifest
    for line in manifest:
        digest, rel = line.split("  ", 1)
        assert _sha256(v3_2_fixture_root / rel) == digest


def test_fixture_verifier_script_passes_from_repo_root() -> None:
    """Execute the repository verifier exactly as requested by the T00 prompt."""
    completed = subprocess.run(
        [
            sys.executable,
            "tests/fixtures/v3_2_focused/verify_fixture_pack.py",
            "--repo-root",
            ".",
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Fixture verification PASSED" in completed.stdout


def test_zip_checksum_is_preserved_separately(design_input_v3_2_root: Path) -> None:
    """Confirm the tracked ZIP still matches its tracked SHA-256 sidecar."""
    sha_file = design_input_v3_2_root / "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip.sha256"
    zip_file = design_input_v3_2_root / "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip"
    assert sha_file.read_text(encoding="utf-8").split()[0] == _sha256(zip_file)


def test_design_inputs_are_tracked_verbatim(design_input_v3_2_root: Path) -> None:
    """Ensure only the three frozen design inputs are tracked under design_input."""
    expected = {
        "Cognityx_Ingest_v3_2_Adaptive_Segmentation_Source_Graph_and_Provenance_Address_Plan.docx",
        "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip",
        "Cognityx_Ingest_v3_2_Focused_Fixture_Pack.zip.sha256",
    }
    assert expected == {p.name for p in design_input_v3_2_root.iterdir() if p.is_file()}
