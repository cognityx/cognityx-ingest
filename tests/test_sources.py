from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cognityx_ingest.control import ControlDecision
from cognityx_ingest.models import ExecutionContext
from cognityx_ingest.sources import SourceRegistry
from cognityx_storage import LocalStorageBackend, StorageClient


def _context(*, principal: str | None = "alice", tenant: str | None = "tenant-a", system: bool = False) -> ExecutionContext:
    return ExecutionContext(
        run_id="run", correlation_id="correlation", principal_id=principal,
        tenant_id=tenant, context_type="system" if system else "user",
        scopes={"service": "policy-sync"} if system else {"region": "eu"},
    )


def _registry(tmp_path: Path, control=None) -> SourceRegistry:
    storage = StorageClient(LocalStorageBackend(tmp_path / "storage")).for_shared_data()
    return SourceRegistry(storage, tmp_path / "storage" / ".cognityx-ingest" / "source_catalog.sqlite3", control=control)


def test_context_is_canonical_and_system_context_is_distinct(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    first = registry.resolve_context(_context())
    equivalent = registry.resolve_context(_context())
    different = registry.resolve_context(_context(tenant="tenant-b"))
    system = registry.resolve_context(_context(principal=None, tenant=None, system=True))

    assert first.context_id == equivalent.context_id
    assert first.context_id != different.context_id
    assert system.context_type == "system"
    assert system.context_id != first.context_id


def test_bundle_tree_is_lazy_stable_and_context_scoped(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    default = registry.resolve_bundle(_context())
    nested = registry.resolve_bundle(_context(), "phd/rag")
    again = registry.resolve_bundle(_context(), "phd/rag")
    other = registry.resolve_bundle(_context(tenant="tenant-b"), "phd/rag")

    assert default.name == "default"
    assert nested.path == "phd/rag"
    assert nested.bundle_id == again.bundle_id
    assert nested.bundle_id != other.bundle_id


def test_source_registration_deduplicates_by_bundle_and_reuses_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    one = tmp_path / "one.txt"
    two = tmp_path / "renamed.md"
    one.write_bytes(b"same immutable bytes")
    two.write_bytes(b"same immutable bytes")

    first = registry.register_file(_context(), one)
    duplicate = registry.register_file(_context(), two)
    other_bundle = registry.register_file(_context(), two, bundle="phd/rag")

    assert first.status == "created"
    assert duplicate.status == "already_registered"
    assert duplicate.source_id == first.source_id
    assert other_bundle.status == "created"
    assert other_bundle.source_id != first.source_id
    assert other_bundle.sha256 == first.sha256
    with registry.open(_context(), other_bundle.source_id) as stored:
        assert stored.read() == b"same immutable bytes"
    blobs = list((tmp_path / "storage" / "shared" / "blob-domains").rglob("*"))
    assert len([path for path in blobs if path.is_file()]) == 1


def test_restart_and_context_scoped_lookup_preserve_catalog_without_path_access(tmp_path: Path) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(b"report")
    first_registry = _registry(tmp_path)
    result = first_registry.register_file(_context(), source)
    restarted = _registry(tmp_path)

    assert restarted.show_source(_context(), result.source_id).source_id == result.source_id
    with pytest.raises(KeyError):
        restarted.show_source(_context(tenant="tenant-b"), result.source_id)


def test_two_simultaneous_registrations_create_one_logical_source(tmp_path: Path) -> None:
    source = tmp_path / "parallel.txt"
    source.write_bytes(b"parallel bytes")
    registry = _registry(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: registry.register_file(_context(), source), range(2)))

    assert {result.status for result in results} == {"created", "already_registered"}
    assert len({result.source_id for result in results}) == 1
    assert len(registry.list_sources(_context())) == 1


def test_source_actions_use_existing_control_seam(tmp_path: Path) -> None:
    class RecordingControl:
        def __init__(self) -> None:
            self.actions: list[str] = []

        def authorize(self, context, action, resource=None, request=None):
            self.actions.append(action)
            return ControlDecision(allowed=True)

        def report_usage(self, context, usage):
            return None

    control = RecordingControl()
    registry = _registry(tmp_path, control)
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")
    result = registry.register_file(_context(), source)
    registry.list_bundles(_context())
    registry.list_sources(_context())
    registry.show_source(_context(), result.source_id)

    assert {"ingest.bundle.create", "ingest.bundle.read", "ingest.source.create", "ingest.source.list", "ingest.source.read"} <= set(control.actions)
