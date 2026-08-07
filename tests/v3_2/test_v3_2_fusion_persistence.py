"""Additive T05 artifact persistence and cleanup tests through IngestService."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from cognityx_ingest import (
    CanonicalContentArtifact,
    ExtractedBlock,
    ExtractedPage,
    ExtractionResult,
    IngestManager,
    IngestResult,
    IngestService,
    ParserFusionArtifact,
    ParserFusionValidationError,
    ParserObservationSet,
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
    fused = _fused_result()
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


def _fused_result() -> ExtractionResult:
    """Build deterministic completed compare output for persistence tests."""
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
    return _fuse_results(results, ("docling", "pymupdf"))


def test_ingest_service_persists_bound_observations_and_fusion_json(tmp_path: Path) -> None:
    """Write both exact validated v3.2 aggregates outside the native store."""
    result, storage, _context = _ingest_fused(tmp_path)
    expected_fusion = (
        f"ingest/documents/{result.document.document_id}/parser/fusion-decisions.json"
    )
    expected_observations = (
        f"ingest/documents/{result.document.document_id}/parser/observations.json"
    )
    assert result.fusion_artifact_key == expected_fusion
    assert result.observation_artifact_key == expected_observations
    observation_payload = storage.open(expected_observations).read()
    fusion_payload = storage.open(expected_fusion).read()
    observation_set = ParserObservationSet.from_json_bytes(observation_payload)
    artifact = ParserFusionArtifact.from_json_bytes(fusion_payload)
    artifact.validate_against_observation_set(observation_set)
    assert observation_set.to_json_bytes() == observation_payload
    assert artifact.to_json_bytes() == fusion_payload
    assert storage.stat(expected_observations).media_type == "application/json"
    assert storage.stat(expected_fusion).media_type == "application/json"
    assert artifact.observation_set_sha256 == hashlib.sha256(
        observation_payload
    ).hexdigest()
    assert all(
        observation_set.get(observation_id)
        for decision in artifact.fact_decisions
        for observation_id in decision.observation_ids
    )
    assert all("fusion-decisions" not in key for key in result.raw_parser_keys)
    assert all("observations" not in key for key in result.raw_parser_keys)


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
    observation_reference = manifest["artifacts"]["parser_observations"]
    assert observation_reference == {
        "artifact_id": f"art-{result.document.document_id}-parser_observations",
        "uri": storage.uri(result.observation_artifact_key),
    }
    observation_summary = provenance["parser"]["observations"]
    assert set(observation_summary) == {
        "observation_schema",
        "observation_set_id",
        "observation_artifact_uri",
        "sha256",
        "observation_count",
        "parser_ids",
    }
    assert observation_summary["observation_artifact_uri"] == storage.uri(
        result.observation_artifact_key
    )
    assert observation_summary["parser_ids"] == ["docling", "pymupdf"]
    assert "Shared exact text" not in json.dumps(observation_summary)
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
    assert next(
        item
        for item in result.artifacts
        if item.artifact_id == f"art-{result.document.document_id}-parser_observations"
    ).uri == storage.uri(result.observation_artifact_key)
    expected_bytes = sum(
        storage.stat(key).size_bytes
        for key in (
            result.document_key,
            result.evidence_key,
            result.manifest_key,
            result.provenance_key,
            result.canonical_content_key,
                result.observation_artifact_key,
                result.fusion_artifact_key,
                result.source_graph_key,
                result.provenance_addresses_key,
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
    observations = ParserObservationSet.from_json_bytes(
        storage.open(result.observation_artifact_key).read()
    )
    fusion = ParserFusionArtifact.from_json_bytes(
        storage.open(result.fusion_artifact_key).read()
    )
    decisions = {item.decision_id: item for item in fusion.fact_decisions}
    for source in sources:
        observation = observations.get(source["observation_id"])
        decision = decisions[source["decision_id"]]
        assert observation.source_region.source_region_id == "page:0"
        assert source["observation_id"] in decision.observation_ids


def test_document_cleanup_removes_both_document_local_t05_artifacts(tmp_path: Path) -> None:
    """Use existing recursive document deletion rather than a T07 purge API."""
    result, storage, context = _ingest_fused(tmp_path)
    assert storage.exists(result.observation_artifact_key)
    assert storage.exists(result.fusion_artifact_key)
    IngestManager(storage, JobRepository()).delete_document(
        context, result.document.document_id
    )
    assert not storage.exists(result.observation_artifact_key)
    assert not storage.exists(result.fusion_artifact_key)


def test_conflict_and_unresolved_ids_resolve_after_exact_reload(tmp_path: Path) -> None:
    """Resolve every retained decision reference through durable observations."""
    results = (
        ExtractionResult(
            pages=(
                ExtractedPage(
                    1,
                    "Docling page",
                    page_index=0,
                    blocks=(
                        ExtractedBlock(
                            "block-a",
                            "same block",
                            reading_order=1,
                            bbox=(0.0, 0.0, 100.0, 40.0),
                        ),
                    ),
                ),
            ),
            backend="docling",
            raw_artifact=b'{"native":"docling"}',
        ),
        ExtractionResult(
            pages=(
                ExtractedPage(
                    1,
                    "PyMuPDF page",
                    page_index=0,
                    blocks=(
                        ExtractedBlock(
                            "block-a",
                            "same block",
                            reading_order=2,
                            bbox=(0.0, 0.0, 100.0, 40.0),
                        ),
                    ),
                ),
            ),
            backend="pymupdf",
            raw_artifact=b'{"native":"pymupdf"}',
        ),
    )
    fused = _fuse_results(results, ("docling", "pymupdf"))
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime-conflict")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "artifacts-conflict")
    ).for_shared_data()
    source = tmp_path / "conflict.pdf"
    source.write_bytes(b"conflict source")
    result = IngestService(
        storage, extractor=_FusedFixtureParser(fused), registry=registry
    ).ingest(
        source,
        context=ExecutionContext(
            run_id="run-conflict-reload",
            correlation_id="cor-conflict-reload",
            principal_id="fixture-test",
        ),
        registry=registry,
    )
    observations = ParserObservationSet.from_json_bytes(
        storage.open(result.observation_artifact_key).read()
    )
    fusion = ParserFusionArtifact.from_json_bytes(
        storage.open(result.fusion_artifact_key).read()
    )
    assert {item.state for item in fusion.fact_decisions} >= {"conflict", "unresolved"}
    assert all(
        observations.get(observation_id)
        for decision in fusion.fact_decisions
        if decision.state in {"conflict", "unresolved"}
        for observation_id in decision.observation_ids
    )


def test_ingest_validates_both_public_artifacts_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a same-ID observation-byte change before any artifact storage call."""
    fused = _fused_result()
    value = json.loads(fused.observation_artifact)
    value["observations"][0]["confidence"] = 0.375
    tampered = replace(
        fused,
        observation_artifact=json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode(),
    )
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime-tampered")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(
        LocalStorageBackend(tmp_path / "artifacts-tampered")
    ).for_shared_data()

    def forbidden_write(*_args, **_kwargs):
        """Fail if persistence starts before observation/fusion validation."""
        raise AssertionError("artifact write occurred before T05 validation")

    monkeypatch.setattr(storage, "put_bytes", forbidden_write)
    source = tmp_path / "tampered.pdf"
    source.write_bytes(b"tampered source")
    with pytest.raises(ParserFusionValidationError, match="SHA-256"):
        IngestService(
            storage, extractor=_FusedFixtureParser(tampered), registry=registry
        ).ingest(
            source,
            context=ExecutionContext(
                run_id="run-tampered",
                correlation_id="cor-tampered",
                principal_id="fixture-test",
            ),
            registry=registry,
        )
