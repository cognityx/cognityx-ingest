from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest.cli import main


def _output(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def test_lifecycle_cli_requires_confirmation_and_emits_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "asset.txt"
    source.write_bytes(b"asset")
    root = tmp_path / "storage"
    assert main(["assets", "add", str(source), "--storage-root", str(root)]) == 0
    created = _output(capsys)

    with pytest.raises(ValueError, match="requires --yes"):
        main(
            [
                "assets",
                "delete",
                created["asset_id"],
                "--storage-root",
                str(root),
            ]
        )
    assert capsys.readouterr().out == ""

    assert (
        main(
            [
                "assets",
                "delete",
                created["asset_id"],
                "--yes",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    deleted = _output(capsys)
    assert deleted["status"] == "deleted"

    assert (
        main(
            [
                "assets",
                "deleted",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    assert _output(capsys)[0]["asset_id"] == created["asset_id"]

    assert (
        main(
            [
                "cleanup",
                "blobs",
                "--dry-run",
                "--older-than",
                "7d",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    assert "deletion_candidates" in _output(capsys)
