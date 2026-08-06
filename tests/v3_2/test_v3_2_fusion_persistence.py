"""Additive T05 artifact persistence and cleanup tests through IngestService."""

from __future__ import annotations

import json
from pathlib import Path

from cognityx_ingest import (
    CanonicalContentArtifact,
    ExtractedPage,
    ExtractionResult,
    IngestManager,
    IngestResult,
    IngestService,
    ParserFusionArtifact,
    SourceAssetRegistry,
)
from cognityx_ingest.parser import _fuse_results
from cognityx_jobs import JobRepository
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


class _FusedFixtureParser:
    """Return one completed fused result so persistence tests execute no parser."""

    name = "fusion"

    def __init__(self, result: ExtractionResult) -> None:
        """Retain exact immutable compatibility and decision artifact bytes."""
        self._result = result

    def extract_document(self, _path: Path) -> ExtractionResult:
        """Return the completed result idempotently without reading or writing."""
        return self._result


def _ingest_fused(tmp_path: Path) -> tuple[IngestResult, StorageClient, ExecutionContext]:
    """Run the real persistence composition with deterministic completed results."""
    results = (
        ExtractionResult(
            pages=(ExtractedPage(1, "Shared exact text", page_index=0),),
            backend="docling",
            backend_version="1.0",
            raw_artifact=b'{"native":"docling"}',
        ),
        ExtractionResult(
            pages=(ExtractedPage(1, "Shared exact text", page_index=0),),
            backend="pymupdf",
            backend_version="2.0",
            raw_artifact=b'{"native":"pymupdf"}',
        ),
    )
    fused = _fuse_results(results, ("docling", "pymupdf"))
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-fusion-persistence",
        correlation_id="cor-fusion-persistence",
        principal_id="fixture-test",
    )
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"fixture source bytes")
    result = IngestService(
        storage, extractor=_FusedFixtureParser(fused), registry=registry
    ).ingest(source, context=context, registry=registry)
    return result, storage, context


def test_ingest_service_persists_additive_fusion_decisions_json(tmp_path: Path) -> None:
    """Write exact validated v3.2 bytes outside NativeArtifactStore ownership."""
    result, storage, _context = _ingest_fused(tmp_path)
    expected = (
        f"ingest/documents/{result.document.document_id}/parser/fusion-decisions.json"
    )
    assert result.fusion_artifact_key == expected
    payload = storage.open(expected).read()
    artifact = ParserFusionArtifact.from_json_bytes(payload)
    assert artifact.to_json_bytes() == payload
    assert storage.stat(expected).media_type == "application/json"
    assert all("fusion-decisions" not in key for key in result.raw_parser_keys)


def test_manifest_provenance_and_usage_reference_fusion_additively(tmp_path: Path) -> None:
    """Expose stable IDs, summary counts, and complete output-byte accounting."""
    result, storage, _context = _ingest_fused(tmp_path)
    manifest = json.load(storage.open(result.manifest_key))
    provenance = json.load(storage.open(result.provenance_key))
    reference = manifest["artifacts"]["parser_fusion_decisions"]
    assert reference == {
        "artifact_id": f"art-{result.document.document_id}-parser_fusion_decisions",
        "uri": storage.uri(result.fusion_artifact_key),
    }
    fusion = provenance["parser"]["fusion"]
    assert fusion["fusion_schema"] == "cognityx.ingest.parser-fusion/v3.2"
    assert fusion["fusion_artifact_uri"] == storage.uri(result.fusion_artifact_key)
    assert fusion["source_backends"] == ["docling", "pymupdf"]
    assert fusion["state_counts"]["agreement"] >= 1
    assert "observations" not in fusion
    assert next(
        item
        for item in result.artifacts
        if item.artifact_id == f"art-{result.document.document_id}-parser_fusion_decisions"
    ).uri == storage.uri(result.fusion_artifact_key)
    expected_bytes = sum(
        storage.stat(key).size_bytes
        for key in (
            result.document_key,
            result.evidence_key,
            result.manifest_key,
            result.provenance_key,
            result.canonical_content_key,
            result.fusion_artifact_key,
        )
    )
    assert result.usage.output_bytes == expected_bytes


def test_raw_native_and_canonical_artifacts_remain_readable(tmp_path: Path) -> None:
    """Keep v1 fusion, contributor raw bytes, T01 descriptors, and T02 readable."""
    result, storage, _context = _ingest_fused(tmp_path)
    assert len(result.raw_parser_keys) == 3
    assert {key.rsplit("/", 1)[-1] for key in result.raw_parser_keys} == {
        "docling.json",
        "pymupdf.json",
        "fusion.json",
    }
    assert json.load(storage.open(result.raw_parser_key))["schema"] == (
        "cognityx.ingest.parser-fusion/v1"
    )
    provenance = json.load(storage.open(result.provenance_key))
    assert all(
        item["descriptor_uri"].startswith("storage://")
        for item in provenance["parser"]["raw_artifacts"]
    )
    CanonicalContentArtifact.from_dict(
        json.load(storage.open(result.canonical_content_key))
    )


def test_canonical_fact_sources_retain_observation_and_adjudication_ids(
    tmp_path: Path,
) -> None:
    """Carry typed T05 references into T02 nodes without copying decision values."""
    result, storage, _context = _ingest_fused(tmp_path)
    canonical = json.load(storage.open(result.canonical_content_key))
    sources = canonical["content_nodes"][0]["fact_sources"]
    assert len(sources) == 2
    assert all(
        {
            "fact",
            "parser_id",
            "method",
            "observation_id",
            "decision_id",
            "adjudication_state",
            "accepted",
            "rejected",
            "gold_eligible",
            "confidence",
        }
        == set(item)
        for item in sources
    )
    assert {item["parser_id"] for item in sources} == {"docling", "pymupdf"}
    assert {item["adjudication_state"] for item in sources} == {"agreement"}
    assert all(item["accepted"] is True for item in sources)
    assert "value" not in json.dumps(sources)


def test_document_cleanup_removes_document_local_fusion_artifact(tmp_path: Path) -> None:
    """Use existing recursive document deletion rather than a T07 purge API."""
    result, storage, context = _ingest_fused(tmp_path)
    assert storage.exists(result.fusion_artifact_key)
    IngestManager(storage, JobRepository()).delete_document(
        context, result.document.document_id
    )
    assert not storage.exists(result.fusion_artifact_key)
