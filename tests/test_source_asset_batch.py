from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

import pytest

from cognityx_ingest import (
    ExecutionContext,
    SourceAssetBatchCancelled,
    SourceAssetBatchResult,
    SourceAssetRegistrationResult,
    SourceAssetRegistry,
)
from cognityx_ingest.cli import main
from cognityx_ingest.control import (
    ControlDecision,
    INGEST_SOURCE_BATCH_CREATE,
)
from cognityx_storage import StorageConfig, StorageRuntime


class RecordingControl:
    def __init__(self) -> None:
        self.authorizations: list[tuple[str, object]] = []
        self.usages = []

    def authorize(self, context, action, resource=None, request=None):
        self.authorizations.append((action, resource))
        return ControlDecision(allowed=True)

    def report_usage(self, context, usage) -> None:
        self.usages.append(usage)


def _execution() -> ExecutionContext:
    return ExecutionContext(
        run_id="run-batch",
        correlation_id="correlation-batch",
        principal_id="alice",
        tenant_id="tenant-a",
    )


def _registry(
    tmp_path: Path, *, control: RecordingControl | None = None
) -> SourceAssetRegistry:
    return SourceAssetRegistry(
        StorageRuntime.from_config(
            StorageConfig.built_in(root=tmp_path / "storage")
        ),
        tmp_path / "catalog.sqlite3",
        control=control,
    )


def _tree(root: Path) -> None:
    (root / "india").mkdir(parents=True)
    (root / "europe").mkdir()
    (root / "empty").mkdir()
    (root / "policy.pdf").write_bytes(b"policy")
    (root / "india/agreement.pdf").write_bytes(b"india")
    (root / "europe/terms.docx").write_bytes(b"europe")


def test_register_path_preserves_single_file_result(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"report")
    registry = _registry(tmp_path)

    result = registry.register_path(
        _execution(), source, bundle="legal", structure="flat", recursive=False
    )

    assert isinstance(result, SourceAssetRegistrationResult)
    assert result.status == "created"


def test_preserve_structure_and_omitted_bundle_root(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    _tree(root)
    registry = _registry(tmp_path)

    result = registry.register_path(_execution(), root)

    assert isinstance(result, SourceAssetBatchResult)
    assert result.root_bundle_path == "contracts"
    assert result.created_count == 3
    assert [item.relative_path for item in result.items] == [
        "europe/terms.docx",
        "india/agreement.pdf",
        "policy.pdf",
    ]
    assert {item.bundle_path for item in result.items} == {
        "contracts",
        "contracts/europe",
        "contracts/india",
    }
    paths = {item.path for item in registry.list_doc_bundles(_execution())}
    assert "contracts/empty" not in paths


def test_explicit_bundle_does_not_repeat_root_and_flattening_works(
    tmp_path: Path,
) -> None:
    root = tmp_path / "contracts"
    _tree(root)
    registry = _registry(tmp_path)

    result = registry.register_path(
        _execution(), root, bundle=r"legal\active", structure="flat"
    )

    assert result.root_bundle_path == "legal/active"
    assert {item.bundle_path for item in result.items} == {"legal/active"}
    assert all("contracts" not in item.bundle_path for item in result.items)


def test_non_recursive_ignores_nested_files(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    _tree(root)

    result = _registry(tmp_path).register_path(
        _execution(), root, recursive=False
    )

    assert result.files_discovered == 1
    assert result.items[0].relative_path == "policy.pdf"


def test_unicode_symlinks_special_entries_and_storage_are_skipped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "源"
    (root / "भारत").mkdir(parents=True)
    (root / "भारत/契約.txt").write_text("content", encoding="utf-8")
    (root / "file-link").symlink_to(root / "भारत/契約.txt")
    (root / "dir-link").symlink_to(root / "भारत", target_is_directory=True)
    os.mkfifo(root / "named-pipe")
    storage = root / "generated-storage"
    storage.mkdir()
    (storage / "generated.bin").write_bytes(b"generated")
    registry = SourceAssetRegistry(
        StorageRuntime.from_config(StorageConfig.built_in(root=storage)),
        tmp_path / "catalog.sqlite3",
    )

    result = registry.register_path(_execution(), root)

    assert result.files_discovered == 1
    assert result.items[-1].relative_path == "भारत/契約.txt"
    skipped = {
        item.relative_path: item.error_category
        for item in result.items
        if item.status == "skipped"
    }
    assert skipped == {
        "dir-link": "symlink",
        "file-link": "symlink",
        "generated-storage": "cognityx_storage_root",
        "named-pipe": "special_entry",
    }


def test_partial_failure_rerun_restore_and_usage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    (root / "b.txt").write_bytes(b"b")
    control = RecordingControl()
    registry = _registry(tmp_path, control=control)
    original = registry.register_asset

    def fail_one(execution, file, *, bundle=None):
        if Path(file).name == "a.txt":
            raise PermissionError("secret absolute path must not escape")
        return original(execution, file, bundle=bundle)

    monkeypatch.setattr(registry, "register_asset", fail_one)
    partial = registry.register_path(_execution(), root)

    assert partial.failed_count == 1
    assert partial.created_count == 1
    failed = next(item for item in partial.items if item.status == "failed")
    assert failed.relative_path == "a.txt"
    assert "secret" not in failed.error_message

    monkeypatch.setattr(registry, "register_asset", original)
    rerun = registry.register_path(_execution(), root)
    assert rerun.created_count == 1
    assert rerun.already_registered_count == 1
    created = next(item for item in rerun.items if item.relative_path == "a.txt")
    registry.delete_asset(_execution(), created.asset_id)
    restored = registry.register_path(_execution(), root)
    assert restored.restored_count == 1
    assert restored.already_registered_count == 1

    assert any(
        action == INGEST_SOURCE_BATCH_CREATE
        for action, _ in control.authorizations
    )
    metrics = control.usages[-1].metrics
    assert metrics["files_discovered"] == 2
    assert metrics["files_processed"] == 2
    assert metrics["assets_restored"] == 1


def test_same_names_work_across_preserved_and_flat_bundles(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir()
    (root / "one/same.txt").write_bytes(b"one")
    (root / "two/same.txt").write_bytes(b"two")
    registry = _registry(tmp_path)

    preserved = registry.register_path(
        _execution(), root, bundle="preserved", structure="preserve"
    )
    flattened = registry.register_path(
        _execution(), root, bundle="flat", structure="flat"
    )

    assert preserved.created_count == 2
    assert {item.bundle_path for item in preserved.items} == {
        "preserved/one",
        "preserved/two",
    }
    assert flattened.created_count == 2
    assert {item.bundle_path for item in flattened.items} == {"flat"}


@pytest.mark.parametrize("structure", ["unknown", "", "PRESERVE"])
def test_invalid_structure_is_rejected(
    tmp_path: Path, structure: str
) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    with pytest.raises(ValueError, match="structure"):
        _registry(tmp_path).register_path(
            _execution(), root, structure=structure
        )


@pytest.mark.parametrize("bundle", ["/absolute", "../escape", "a/../b", "C:\\root"])
def test_generated_bundle_paths_are_safe(tmp_path: Path, bundle: str) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    with pytest.raises(ValueError, match="Bundle path"):
        _registry(tmp_path).register_path(
            _execution(), root, bundle=bundle
        )


def test_progress_order_cancellation_and_safe_json(tmp_path: Path) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    (root / "b.txt").write_bytes(b"b")
    events: list[dict] = []

    def cancelled() -> bool:
        return any(item["event"] == "file_completed" for item in events)

    with pytest.raises(SourceAssetBatchCancelled) as captured:
        _registry(tmp_path).register_path(
            _execution(),
            root,
            progress=events.append,
            cancellation_requested=cancelled,
        )

    assert [item["event"] for item in events] == [
        "scan_started",
        "scan_completed",
        "file_started",
        "file_completed",
    ]
    assert captured.value.result is not None
    assert captured.value.result.files_processed == 1
    assert str(tmp_path) not in json.dumps(asdict(captured.value.result))


def test_synthetic_tree_is_lexical_and_complete(tmp_path: Path) -> None:
    root = tmp_path / "many"
    for index in range(125):
        directory = root / f"group-{index % 5}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"file-{index:03d}.bin").write_bytes(str(index).encode())

    result = _registry(tmp_path).register_path(_execution(), root)

    relative_paths = [item.relative_path for item in result.items]
    assert result.files_discovered == 125
    assert result.files_processed == 125
    assert relative_paths == sorted(relative_paths)


def test_component_cli_directory_output_is_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "contracts"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")

    assert (
        main(
            [
                "assets",
                "add",
                str(root),
                "--bundle",
                "legal",
                "--structure",
                "flat",
                "--no-recursive",
                "--storage-root",
                str(tmp_path / "storage"),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.err == ""
    assert payload["root_bundle_path"] == "legal"
    assert payload["items"][0]["asset_id"].startswith("src-")
    assert str(root) not in output.out
