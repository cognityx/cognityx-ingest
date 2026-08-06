from __future__ import annotations

from cognityx_ingest.cli import main


def test_cli_ingest_paths_remain_supported(tmp_path, provenance_pdf):
    storage_root = tmp_path / "storage"
    assert main(["ingest", str(provenance_pdf), "--storage-root", str(storage_root)]) == 0


def test_cli_compatibility_aliases_remain_supported(tmp_path, provenance_pdf):
    storage_root = tmp_path / "storage"
    assert main(["sources", "add", str(provenance_pdf), "--storage-root", str(storage_root)]) == 0
