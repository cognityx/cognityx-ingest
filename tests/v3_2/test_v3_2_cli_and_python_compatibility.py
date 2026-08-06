"""CLI and Python compatibility checks for the v3.2 scaffold.

T00 must preserve the ordinary user workflow while adding future-facing tests.
These checks call the current compatibility CLI commands through the production
entrypoint so T10 can evolve the SDK/CLI later without breaking today's path.
"""

from __future__ import annotations

from cognityx_ingest.cli import main


def test_cli_ingest_paths_remain_supported(tmp_path, provenance_pdf):
    """Verify path ingestion still works through the compatibility CLI."""
    storage_root = tmp_path / "storage"
    assert main(["ingest", str(provenance_pdf), "--storage-root", str(storage_root)]) == 0


def test_cli_compatibility_aliases_remain_supported(tmp_path, provenance_pdf):
    """Verify legacy source aliases still register the reused base PDF."""
    storage_root = tmp_path / "storage"
    assert main(["sources", "add", str(provenance_pdf), "--storage-root", str(storage_root)]) == 0
