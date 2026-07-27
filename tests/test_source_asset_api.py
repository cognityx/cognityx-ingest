from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from cognityx_ingest import (
    DocBundle,
    ExecutionContext,
    RegisteredSource,
    SourceAsset,
    SourceAssetContext,
    SourceAssetLocation,
    SourceAssetRegistrationResult,
    SourceAssetRegistry,
    SourceBundle,
    SourceContext,
    SourceLocation,
    SourceRegistrationResult,
    SourceRegistry,
)
from cognityx_ingest.cli import main
from cognityx_ingest.parser import PyPdfExtractor
from cognityx_storage import BlobRef, StorageConfig, StorageRuntime


def _context() -> ExecutionContext:
    return ExecutionContext(
        run_id="run",
        correlation_id="correlation",
        principal_id="alice",
        tenant_id="tenant-a",
    )


def _registry(root: Path) -> SourceAssetRegistry:
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
    return SourceAssetRegistry(
        runtime, root / ".cognityx-ingest" / "source_catalog.sqlite3"
    )


def test_canonical_models_and_registry_are_compatibility_aliases() -> None:
    assert SourceRegistry is SourceAssetRegistry
    assert SourceBundle is DocBundle
    assert RegisteredSource is SourceAsset
    assert SourceContext is SourceAssetContext
    assert SourceLocation is SourceAssetLocation
    assert SourceRegistrationResult is SourceAssetRegistrationResult


def test_canonical_and_compatibility_methods_share_one_implementation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "interview.mp3"
    source.write_bytes(b"audio bytes")
    registry = _registry(root)

    canonical = registry.register_asset(
        _context(), source, bundle="research/interviews"
    )
    compatible = registry.register_file(
        _context(), source, bundle="research/interviews"
    )
    asset = registry.show_asset(_context(), canonical.asset_id)
    old_asset = registry.show_source(_context(), canonical.source_id)
    bundle = registry.resolve_doc_bundle(
        _context(), "research/interviews", create=False
    )

    assert canonical.asset_id == canonical.source_id
    assert compatible.asset_id == canonical.asset_id
    assert compatible.status == "already_registered"
    assert asset is not old_asset
    assert asset == old_asset
    assert registry.list_assets(_context()) == registry.list_sources(_context())
    assert registry.list_doc_bundles(_context()) == registry.list_bundles(
        _context()
    )
    assert asset.ref.resource_type == "source_asset"
    assert asset.ref.resource_id == asset.asset_id
    assert asset.ref.context_id == asset.context_id
    assert bundle.ref.resource_type == "doc_bundle"
    assert bundle.ref.resource_id == bundle.bundle_id
    assert bundle.ref.context_id == bundle.context_id


def test_existing_catalog_ids_schema_blobref_and_bytes_remain_stable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "existing.bin"
    content = b"existing stable content"
    source.write_bytes(content)
    old_registry = SourceRegistry(
        StorageRuntime.from_config(StorageConfig.built_in(root=root)),
        root / ".cognityx-ingest/source_catalog.sqlite3",
    )
    result = old_registry.register_file(_context(), source, bundle="existing")
    catalog = root / ".cognityx-ingest/source_catalog.sqlite3"

    before = _catalog_contract(catalog, result.source_id)
    registry = _registry(root)
    asset = registry.show_asset(_context(), result.source_id)
    bundle = registry.resolve_doc_bundle(_context(), "existing", create=False)
    after = _catalog_contract(catalog, result.source_id)

    assert before == after
    assert asset.asset_id == result.source_id
    assert asset.source_id == result.source_id
    assert asset.asset_id.startswith("src-")
    assert bundle.bundle_id == result.bundle_id
    with registry.open_asset(_context(), asset.asset_id) as opened:
        assert opened.read() == content
    blob_ref = BlobRef.from_dict(json.loads(after["blob_ref_json"]))
    assert blob_ref.blob_id == asset.blob_id


@pytest.mark.parametrize(
    ("filename", "content", "media_prefix"),
    [
        ("sample.pdf", b"%PDF-sample", "application/"),
        ("image.png", b"png-sample", "image/"),
        ("audio.mp3", b"mp3-sample", "audio/"),
        ("video.mp4", b"mp4-sample", "video/"),
        ("data.csv", b"name,value\none,1\n", "text/"),
        ("archive.zip", b"PK-zip-sample", "application/"),
    ],
)
def test_source_asset_registration_accepts_any_digital_file_without_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: bytes,
    media_prefix: str,
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / filename
    source.write_bytes(content)
    registry = _registry(root)

    def parsing_is_not_registration(*args, **kwargs):
        raise AssertionError("SourceAsset registration must not invoke a parser")

    monkeypatch.setattr(PyPdfExtractor, "extract", parsing_is_not_registration)
    result = registry.register_asset(
        _context(), source, bundle="multiformat"
    )
    asset = registry.show_asset(_context(), result.asset_id)

    assert asset.original_filename == filename
    assert asset.size_bytes == len(content)
    assert len(asset.sha256) == 64
    assert asset.media_type.startswith(media_prefix)
    with registry.open_asset(_context(), asset.asset_id) as opened:
        assert opened.read() == content


def test_canonical_and_compatibility_cli_share_catalog_and_separate_warnings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "asset.csv"
    source.write_bytes(b"a,b\n1,2\n")

    assert (
        main(
            [
                "assets",
                "add",
                str(source),
                "--bundle",
                "research/data",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    canonical_output = capsys.readouterr()
    created = json.loads(canonical_output.out)
    assert canonical_output.err == ""
    assert created["asset_id"].startswith("src-")
    assert "source_id" not in created

    assert (
        main(
            [
                "sources",
                "show",
                created["asset_id"],
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    compatibility_output = capsys.readouterr()
    shown = json.loads(compatibility_output.out)
    assert shown["source_id"] == created["asset_id"]
    assert "asset_id" not in shown
    assert "'sources' is retained for compatibility" in compatibility_output.err

    assert (
        main(
            [
                "doc-bundles",
                "list",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    canonical_bundles = json.loads(capsys.readouterr().out)
    assert any(item["path"] == "research/data" for item in canonical_bundles)

    assert (
        main(
            [
                "bundles",
                "list",
                "--storage-root",
                str(root),
            ]
        )
        == 0
    )
    compatibility_bundles = capsys.readouterr()
    assert json.loads(compatibility_bundles.out) == canonical_bundles
    assert (
        "'bundles' is retained for compatibility"
        in compatibility_bundles.err
    )


def _catalog_contract(catalog: Path, source_id: str) -> dict[str, object]:
    with sqlite3.connect(catalog) as db:
        source_columns = tuple(
            row[1] for row in db.execute("PRAGMA table_info(sources)")
        )
        tables = tuple(
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        row = db.execute(
            "SELECT source_id,context_id,bundle_id,blob_id,blob_ref_json "
            "FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
    return {
        "tables": tables,
        "source_columns": source_columns,
        "source_id": row[0],
        "context_id": row[1],
        "bundle_id": row[2],
        "blob_id": row[3],
        "blob_ref_json": row[4],
    }
