"""Base fixture integrity checks for the v3.2 focused scaffold.

These tests protect the frozen base PDF and installed delta fixture from
accidental regeneration. They use only hashes and README contract examples,
which keeps T00 independent of optional parser packages.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    """Compute a streaming SHA-256 digest for fixture files."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_manifest_matches_frozen_fixture_tree(v3_2_fixture_root: Path) -> None:
    """Validate the source-truth hashes declared by the fixture manifest."""
    manifest = json.loads((v3_2_fixture_root / "fixture_manifest.json").read_text())
    assert manifest["schema"] == "cognityx.ingest.fixture-manifest/v3.2"
    for item in manifest["synthetic_source_truth"]:
        assert _sha256(v3_2_fixture_root / item["path"]) == item["sha256"]


def test_base_fixture_is_reused_without_duplication(provenance_fixture_root: Path) -> None:
    """Assert T00 reuses the existing provenance v1 PDF bytes."""
    pdf = provenance_fixture_root / "main_policy_v2.pdf"
    assert pdf.is_file()
    assert _sha256(pdf) == "73a2dc18cc0ed79419a2208db93cc151e0a1fe092c96ed4322e449207f22630c"


def test_frozen_inputs_keep_normal_cli_examples_stable(v3_2_fixture_root: Path) -> None:
    """Assert the fixture README keeps the normal user workflow visible."""
    readme = (v3_2_fixture_root / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "cogni ingest document.pdf",
        "cogni ingest --asset src-...",
        "cogni ingest --bundle research/reports",
        "cogni job watch job-...",
        "cogni document show doc-...",
        "cogni artifact read doc-... provenance",
    ):
        assert phrase in readme
