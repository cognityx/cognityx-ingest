"""End-to-end T07 retention, reuse, reference, purge, and survival tests.

The suite composes the real SQLite SourceAssetRegistry, local Cognityx Storage,
T01 NativeArtifactStore, T02 canonical validation, and T06 verified-view seam.
Physical payload removal appears only in test setup to model a Storage-owned job.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from cognityx_ingest.canonical_content import (
    CanonicalArtifactDescriptor,
    CanonicalContentArtifact,
    NativeBinding,
)
from cognityx_ingest.cleanup import (
    ExtractionRetentionService,
    collect_reference_ids,
)
from cognityx_ingest.models import (
    ExtractionIdentity,
    ExtractionPurgeBlockedError,
    ExtractionPurgeFinalizationError,
    ExtractionRetentionConflictError,
    ExtractionRetentionReferenceError,
    ExtractionRetentionState,
    ExtractionReuseIntegrityError,
)
from cognityx_ingest.native_artifacts import (
    NativeArtifactDescriptor,
    NativeArtifactStore,
)
from cognityx_ingest.parser_fusion import ParserFusionService
from cognityx_ingest.segmentation_views import (
    NodeSpan,
    SegmentationSegment,
    SegmentationViewReferenceError,
    SegmentationViewService,
)
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


@dataclass(frozen=True, slots=True)
class _Harness:
    """Hold real T07 collaborators and one retained artifact for focused tests."""

    execution: ExecutionContext
    registry: SourceAssetRegistry
    storage: StorageClient
    native_store: NativeArtifactStore
    service: ExtractionRetentionService
    identity: ExtractionIdentity
    descriptor: NativeArtifactDescriptor


def _harness(tmp_path: Path, *, references: tuple[str, ...] = ()) -> _Harness:
    """Create and register one integrity-verified extraction in isolated storage."""
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry(
        runtime, tmp_path / "runtime" / "catalog.sqlite3"
    )
    execution = ExecutionContext(
        run_id="run-retention-1",
        correlation_id="cor-retention-1",
        principal_id="retention-test",
        tenant_id="tenant-a",
    )
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "native")
    ).for_shared_data()
    native_store = NativeArtifactStore(storage, execution)
    descriptor = native_store.store(
        artifact_id="art-docling-retained",
        parser_id="docling",
        parser_version="2.5.0",
        payload=b'{"texts":[{"text":"retained native payload"}]}\n',
        media_type="application/json",
        native_pointers=("#/texts/0",),
    )
    identity = ExtractionIdentity.from_configuration(
        source_sha256="1" * 64,
        parser_id="docling",
        parser_version="2.5.0",
        parser_configuration={
            "adapter": "canonical-v3-2",
            "pipeline": "standard",
        },
        model_version="none",
        scope="tenant-a/policy",
    )
    service = ExtractionRetentionService(
        registry=registry,
        native_artifacts=native_store,
        artifact_storage=storage,
    )
    service.register_extraction(
        execution, identity, descriptor, reference_ids=references
    )
    return _Harness(
        execution,
        registry,
        storage,
        native_store,
        service,
        identity,
        descriptor,
    )


def _expired_without_protection(harness: _Harness) -> None:
    """Advance the harness artifact to the sole purge-eligible metadata shape."""
    record = harness.registry.get_extraction_record(
        harness.execution, harness.descriptor.artifact_id
    )
    for reference_id in record.reference_ids:
        harness.service.remove_reference(
            harness.execution, record.artifact_id, reference_id
        )
    harness.service.mark_retention_expired(
        harness.execution, harness.descriptor.artifact_id
    )


def _bound_canonical(
    canonical: CanonicalContentArtifact,
    descriptor: NativeArtifactDescriptor,
) -> CanonicalContentArtifact:
    """Attach one real T01-backed NativeBinding to the frozen canonical artifact."""
    binding = NativeBinding(
        binding_id="bind-retained-docling",
        canonical_id=canonical.content_nodes[0].node_id,
        artifact_id=descriptor.artifact_id,
        native_pointer=descriptor.native_pointers[0],
        binding_role="support",
    )
    generic = CanonicalArtifactDescriptor(
        artifact_id=descriptor.artifact_id,
        role="parser_native",
        uri=descriptor.uri,
        media_type=descriptor.media_type,
        sha256=descriptor.sha256,
        schema_version=None,
    )
    result = replace(
        canonical,
        native_bindings=(binding,),
        artifact_descriptors=(generic,),
    )
    result.validate(native_descriptors={descriptor.artifact_id: descriptor})
    return result


def test_exact_validated_extraction_is_reusable_and_nonexact_is_not(
    tmp_path: Path,
) -> None:
    """Return a verified hit only for all six exact identity components."""
    harness = _harness(tmp_path)
    miss = harness.service.acquire_reusable(
        harness.execution,
        replace(harness.identity, model_version="other-model"),
        "consumer-miss",
    )
    hit = harness.service.acquire_reusable(
        harness.execution, harness.identity, "consumer-hit"
    )
    assert miss.reused is False
    assert miss.reason == "no exact validated extraction"
    assert hit.reused is True
    assert hit.record is not None
    assert hit.record.artifact_sha256 == harness.descriptor.sha256
    assert hit.record.reference_ids == ("consumer-hit",)


def test_expired_and_purged_records_are_not_selected_for_new_reuse(
    tmp_path: Path,
) -> None:
    """Enforce validated-only lookup before and after Storage-owned purge."""
    harness = _harness(tmp_path)
    _expired_without_protection(harness)
    expired = harness.service.acquire_reusable(
        harness.execution, harness.identity, "consumer-expired"
    )
    harness.storage.delete(harness.descriptor.storage_key)
    harness.service.finalize_purge(
        harness.execution, harness.descriptor.artifact_id, "retention-expired"
    )
    purged = harness.service.acquire_reusable(
        harness.execution, harness.identity, "consumer-purged"
    )
    assert expired.reused is False
    assert purged.reused is False


def test_payload_integrity_failure_releases_only_new_acquisition(
    tmp_path: Path,
) -> None:
    """Roll back a new lease but retain a pre-existing consumer reference."""
    harness = _harness(tmp_path, references=("consumer-existing",))
    payload_path = harness.storage.resolve_local_path(
        harness.descriptor.storage_key
    )
    assert payload_path is not None
    payload_path.write_bytes(b"corrupt")
    with pytest.raises(ExtractionReuseIntegrityError):
        harness.service.acquire_reusable(
            harness.execution, harness.identity, "consumer-new"
        )
    record = harness.registry.get_extraction_record(
        harness.execution, harness.descriptor.artifact_id
    )
    assert record.reference_ids == ("consumer-existing",)


def test_reference_ids_are_deduplicated_ordered_and_removed_explicitly(
    tmp_path: Path,
) -> None:
    """Persist stable reference identity and never infer lifecycle removal."""
    harness = _harness(tmp_path, references=("consumer-z", "consumer-a"))
    harness.service.add_reference(
        harness.execution, harness.descriptor.artifact_id, "consumer-a"
    )
    record = harness.service.add_reference(
        harness.execution, harness.descriptor.artifact_id, "consumer-m"
    )
    assert record.reference_ids == ("consumer-a", "consumer-m", "consumer-z")
    after = harness.service.remove_reference(
        harness.execution, harness.descriptor.artifact_id, "consumer-m"
    )
    assert after.reference_ids == ("consumer-a", "consumer-z")


def test_retention_records_are_context_scoped_without_cross_tenant_disclosure(
    tmp_path: Path,
) -> None:
    """Reject another context's artifact ID through the normal missing-record type."""
    harness = _harness(tmp_path)
    other = ExecutionContext(
        run_id="run-retention-other",
        correlation_id="cor-retention-other",
        principal_id="other-user",
        tenant_id="tenant-b",
    )
    with pytest.raises(ExtractionRetentionReferenceError, match="this context"):
        harness.registry.get_extraction_record(
            other, harness.descriptor.artifact_id
        )


def test_concurrent_exact_acquisition_deduplicates_one_reference(
    tmp_path: Path,
) -> None:
    """Serialize racing acquisitions and retain one active reference row."""
    harness = _harness(tmp_path)

    def acquire() -> bool:
        """Acquire the same exact identity from one worker thread."""
        return harness.service.acquire_reusable(
            harness.execution, harness.identity, "consumer-race"
        ).reused

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert all(workers.map(lambda _index: acquire(), range(8)))
    record = harness.registry.get_extraction_record(
        harness.execution, harness.descriptor.artifact_id
    )
    assert record.reference_ids == ("consumer-race",)


def test_native_binding_verified_view_and_external_consumer_block_purge(
    tmp_path: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Collect only explicit references after canonical and T06 ownership proof."""
    harness = _harness(tmp_path)
    canonical = _bound_canonical(frozen_canonical_artifact, harness.descriptor)
    view_service = SegmentationViewService.from_canonical(
        canonical,
        native_descriptors={harness.descriptor.artifact_id: harness.descriptor},
    )
    native_view = view_service.build_parser_native(
        "view-retained-native",
        chunker_id="docling-hierarchical",
        native_artifact_id=harness.descriptor.artifact_id,
        segments=(
            SegmentationSegment(
                segment_id="segment-retained-native",
                native_chunk_pointer="#/chunks/0",
                node_spans=(NodeSpan(canonical.content_nodes[0].node_id),),
            ),
        ),
    )
    view_set = view_service.build_view_set((native_view,))
    references = collect_reference_ids(
        harness.descriptor.artifact_id,
        canonical_content=canonical,
        native_descriptors={harness.descriptor.artifact_id: harness.descriptor},
        segmentation_view_sets=((view_service, view_set),),
        consumer_reference_ids=("consumer-dataforge-1",),
    )
    assert references == (
        "bind-retained-docling",
        "consumer-dataforge-1",
        "view-retained-native",
    )
    for reference_id in references:
        harness.service.add_reference(
            harness.execution, harness.descriptor.artifact_id, reference_id
        )
    harness.service.mark_retention_expired(
        harness.execution, harness.descriptor.artifact_id
    )
    candidate = harness.service.plan_purge(harness.execution).protected[0]
    assert candidate.reason == "active references remain"


def test_foreign_t06_view_set_cannot_create_a_retention_reference(
    tmp_path: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Require production ownership validation rather than value-only T06 checks."""
    harness = _harness(tmp_path)
    first = SegmentationViewService.from_canonical(frozen_canonical_artifact)
    foreign = replace(
        frozen_canonical_artifact,
        document_id="foreign-document",
    )
    second = SegmentationViewService.from_canonical(foreign)
    first_set = first.build_view_set((first.build_paragraph(),))
    with pytest.raises(SegmentationViewReferenceError):
        collect_reference_ids(
            harness.descriptor.artifact_id,
            segmentation_view_sets=((second, first_set),),
        )


def test_purge_eligibility_precedence_and_legal_hold_are_idempotent(
    tmp_path: Path,
) -> None:
    """Derive exact reasons from state, references, and independent hold."""
    harness = _harness(tmp_path, references=("consumer-live",))
    validated = harness.service.plan_purge(harness.execution).protected[0]
    assert validated.reason == "active references remain"
    harness.service.remove_reference(
        harness.execution, harness.descriptor.artifact_id, "consumer-live"
    )
    unexpired = harness.service.plan_purge(harness.execution).protected[0]
    assert unexpired.reason == "retention has not expired"
    first_hold = harness.service.set_legal_hold(
        harness.execution, harness.descriptor.artifact_id, enabled=True
    )
    second_hold = harness.service.set_legal_hold(
        harness.execution, harness.descriptor.artifact_id, enabled=True
    )
    assert first_hold == second_hold
    harness.service.mark_retention_expired(
        harness.execution, harness.descriptor.artifact_id
    )
    held = harness.service.plan_purge(harness.execution).protected[0]
    assert held.reason == "legal hold blocks purge"
    harness.service.set_legal_hold(
        harness.execution, harness.descriptor.artifact_id, enabled=False
    )
    assert harness.service.plan_purge(harness.execution).eligible[0].eligible


@pytest.mark.parametrize("blocker", ("reference", "hold"))
def test_stale_purge_plan_rechecks_new_protection(
    tmp_path: Path, blocker: str
) -> None:
    """Reject advisory eligibility after a reference or legal hold races in."""
    harness = _harness(tmp_path)
    _expired_without_protection(harness)
    assert harness.service.plan_purge(harness.execution).eligible
    if blocker == "reference":
        harness.service.add_reference(
            harness.execution, harness.descriptor.artifact_id, "consumer-late"
        )
    else:
        harness.service.set_legal_hold(
            harness.execution, harness.descriptor.artifact_id, enabled=True
        )
    harness.storage.delete(harness.descriptor.storage_key)
    with pytest.raises(ExtractionPurgeBlockedError):
        harness.service.finalize_purge(
            harness.execution,
            harness.descriptor.artifact_id,
            "retention-expired",
        )


def test_plan_is_metadata_only_and_finalization_requires_payload_absence(
    tmp_path: Path,
) -> None:
    """Keep planning non-destructive and reject finalization while bytes exist."""
    harness = _harness(tmp_path)
    _expired_without_protection(harness)
    plan = harness.service.plan_purge(harness.execution)
    assert plan.eligible[0].artifact_storage_key == harness.descriptor.storage_key
    assert harness.storage.exists(harness.descriptor.storage_key)
    with pytest.raises(ExtractionPurgeFinalizationError, match="still exists"):
        harness.service.finalize_purge(
            harness.execution,
            harness.descriptor.artifact_id,
            "retention-expired",
        )
    assert harness.storage.exists(harness.descriptor.storage_key)


def test_storage_removal_then_finalization_preserves_tombstone_and_descriptor(
    tmp_path: Path,
) -> None:
    """Finalize one-way state only after external removal and retain T01 metadata."""
    harness = _harness(tmp_path)
    _expired_without_protection(harness)
    harness.storage.delete(harness.descriptor.storage_key)
    purged = harness.service.finalize_purge(
        harness.execution,
        harness.descriptor.artifact_id,
        "retention-expired",
    )
    assert purged.state is ExtractionRetentionState.PURGED
    assert purged.tombstone is not None
    assert purged.tombstone.to_dict() == {
        "parser_id": "docling",
        "parser_version": "2.5.0",
        "source_sha256": "1" * 64,
        "artifact_sha256": harness.descriptor.sha256,
        "deletion_reason": "retention-expired",
    }
    assert purged.identity == harness.identity
    assert purged.extraction_identity == harness.identity.digest
    assert harness.native_store.read(harness.descriptor.artifact_id) == harness.descriptor
    with pytest.raises(ExtractionRetentionConflictError):
        harness.service.mark_retention_expired(
            harness.execution, harness.descriptor.artifact_id
        )


def test_same_identity_can_register_a_new_artifact_after_old_tombstone(
    tmp_path: Path,
) -> None:
    """Allow future exact extraction without resurrecting historical metadata."""
    harness = _harness(tmp_path)
    old_artifact_id = harness.descriptor.artifact_id
    _expired_without_protection(harness)
    harness.storage.delete(harness.descriptor.storage_key)
    old_record = harness.service.finalize_purge(
        harness.execution, old_artifact_id, "retention-expired"
    )
    replacement = harness.native_store.store(
        artifact_id="art-docling-replacement",
        parser_id="docling",
        parser_version="2.5.0",
        payload=b'{"texts":[{"text":"retained native payload"}]}\n',
        media_type="application/json",
        native_pointers=("#/texts/0",),
    )
    new_record = harness.service.register_extraction(
        harness.execution, harness.identity, replacement
    )
    assert old_record.state is ExtractionRetentionState.PURGED
    assert old_record.artifact_id == old_artifact_id
    assert new_record.state is ExtractionRetentionState.VALIDATED
    assert new_record.artifact_id == "art-docling-replacement"
    assert new_record.extraction_identity == old_record.extraction_identity
    assert len(harness.registry.list_extraction_records(harness.execution)) == 2


def test_raw_payload_purge_does_not_mutate_canonical_binding_or_t06_records(
    tmp_path: Path,
    frozen_canonical_artifact: CanonicalContentArtifact,
) -> None:
    """Prove canonical text/selectors/bindings and segmentation bytes survive T07."""
    harness = _harness(tmp_path)
    canonical = _bound_canonical(frozen_canonical_artifact, harness.descriptor)
    view_service = SegmentationViewService.from_canonical(
        canonical,
        native_descriptors={harness.descriptor.artifact_id: harness.descriptor},
    )
    view_set = view_service.build_view_set((view_service.build_paragraph(),))
    canonical_before = canonical.to_json_bytes(
        native_descriptors={harness.descriptor.artifact_id: harness.descriptor}
    )
    views_before = view_set.to_json_bytes()
    selectors_before = tuple(
        selector
        for node in canonical.content_nodes
        for selector in node.source_selectors
    )
    bindings_before = canonical.native_bindings
    _expired_without_protection(harness)
    harness.storage.delete(harness.descriptor.storage_key)
    harness.service.finalize_purge(
        harness.execution, harness.descriptor.artifact_id, "retention-expired"
    )
    assert canonical.to_json_bytes(
        native_descriptors={harness.descriptor.artifact_id: harness.descriptor}
    ) == canonical_before
    assert view_set.to_json_bytes() == views_before
    assert canonical.native_bindings == bindings_before
    assert tuple(
        selector
        for node in canonical.content_nodes
        for selector in node.source_selectors
    ) == selectors_before


def test_raw_payload_purge_does_not_mutate_t05_observations_or_fusion(
    tmp_path: Path,
    fusion_cases,
    build_fusion_observation_set,
) -> None:
    """Keep accepted compact evidence independent from parser payload retention."""
    harness = _harness(tmp_path)
    observations = build_fusion_observation_set(fusion_cases[0])
    fusion = ParserFusionService().fuse(observations)
    observations_before = observations.to_json_bytes()
    fusion_before = fusion.to_json_bytes()
    _expired_without_protection(harness)
    harness.storage.delete(harness.descriptor.storage_key)
    harness.service.finalize_purge(
        harness.execution, harness.descriptor.artifact_id, "retention-expired"
    )
    assert observations.to_json_bytes() == observations_before
    assert fusion.to_json_bytes() == fusion_before


def test_t07_cleanup_source_contains_no_physical_deletion_or_parser_execution() -> None:
    """Keep prohibited deletion and parser ownership absent from T07 production code."""
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "cognityx_ingest"
        / "cleanup.py"
    ).read_text(encoding="utf-8")
    retention_source = source.split("class ExtractionRetentionService:", 1)[1]
    assert ".delete(" not in retention_source
    assert ".unlink(" not in retention_source
    assert ".rmtree(" not in retention_source
    assert "from cognityx_ingest.parser import" not in retention_source
