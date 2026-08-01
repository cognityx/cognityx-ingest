from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import IngestService, PyPdfExtractor, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


@pytest.mark.xfail(
    strict=True,
    reason="GAP-DETERMINISTIC-RELATIONS: docs/provenance-gap-report.md#gap-deterministic-relations",
)
@pytest.mark.parametrize(
    "relation_id",
    [
        "rel-exact-7.2",
        "rel-plural-7.2-11.3",
        "rel-appendix-b",
        "rel-page-a-1",
        "rel-plain-url",
        "rel-missing-handbook",
    ],
)
def test_expected_relation_is_detected_without_model(
    relation_id: str,
    ground_truth: dict[str, object],
    provenance_pdf: Path,
    tmp_path: Path,
) -> None:
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id=f"run-relation-{relation_id}",
        correlation_id=f"correlation-{relation_id}",
        principal_id="relation-test",
    )
    result = IngestService(
        storage, extractor=PyPdfExtractor(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)
    canonical_relations = (*result.document.relations, *result.document.unresolved)
    expected = next(
        item for item in ground_truth["relations"] if item["id"] == relation_id
    )
    assert any(item.target_text == expected["literal"] for item in canonical_relations)


def test_ground_truth_separates_resolved_ambiguous_and_unresolved(
    ground_truth: dict[str, object]
) -> None:
    statuses = {item["status"] for item in ground_truth["relations"]}
    assert {"observed", "resolved", "rejected", "ambiguous", "unresolved"} <= statuses
    missing = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-missing-handbook"
    )
    ambiguous = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-ambiguous-travel"
    )
    assert missing["targets"] == []
    assert len(ambiguous["targets"]) == 2
