"""Prove the v3.2 DataForge producer contract without running DataForge.

The frozen paragraph and composite records remain exact downstream oracles.
Production-facing checks additionally run ``IngestService`` and the T08 public
graph/address readers. They verify that a run advertises only reference bundles
for successful documents and that those references agree with provenance. Ingest
does not generate questions, answers, claims, or Knowledge Units in this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

from cognityx_ingest.models import ExecutionContext
from cognityx_ingest.parser import ExtractedPage, UnsupportedInputError
from cognityx_ingest.service import IngestService
from cognityx_ingest.source_assets import SourceAssetRegistry
from cognityx_ingest.source_graph import (
    ProvenanceAddressCatalog,
    ProvenanceAddressResolver,
    SourceGraph,
)
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


class _DeterministicParser:
    """Provide bounded parser-neutral text for producer integration tests.

    ``IngestService`` constructs and calls this test double through its existing
    extraction protocol. The algorithm maps file bytes containing ``bad`` to the
    normal unsupported-input failure and every other file to one stable page.
    It performs only the requested local read, has no retained state or network
    side effect, and is safe for sequential test use; production parser behavior
    and parser-native contracts are not altered or imitated.
    """

    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        """Return one deterministic page or the typed controlled failure.

        The service calls this once per registered asset. Input is a test path;
        output ordering is fixed and no writes occur. A ``bad`` marker raises
        ``UnsupportedInputError`` so partial-success manifest behavior can be
        tested without broad exception injection.
        """
        if b"bad" in path.read_bytes():
            raise UnsupportedInputError("controlled fixture failure")
        return (ExtractedPage(page_number=1, text="Stable source paragraph."),)


def _producer_components(
    root: Path,
) -> tuple[IngestService, SourceAssetRegistry, StorageClient, ExecutionContext]:
    """Create isolated production composition for run-manifest assertions.

    Tests call this material helper with a temporary root. It constructs the
    normal registry, Storage client, service, and immutable execution context;
    no global configuration is changed. The caller owns the temporary lifecycle,
    and independent invocations cannot share mutable state or physical paths.
    """
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=root))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(root)).for_shared_data()
    context = ExecutionContext(
        run_id="run-t09-producer",
        correlation_id="cor-t09-producer",
        principal_id="t09-test",
    )
    return (
        IngestService(storage, extractor=_DeterministicParser(), registry=registry),
        registry,
        storage,
        context,
    )


def test_dataforge_paragraph_qa_contract(v3_2_fixture_root):
    """Validate the paragraph Q/A handoff fixture shape and support address."""
    qa = json.loads(
        (v3_2_fixture_root / "dataforge" / "paragraph_qa_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert qa["schema"] == "cognityx.dataforge.paragraph-qa-handoff/v1"
    assert qa["input"]["segmentation_view_id"] == "view-paragraph-v1"
    assert qa["expected_output"]["support_address_ids"] == ["addr-strong-pol-p2"]
    assert qa["expected_output"]["must_not_store_independent_source_copy"] is True


def test_dataforge_composite_ku_contract(v3_2_fixture_root):
    """Validate the composite Knowledge Unit fixture excludes ambiguous support."""
    ku = json.loads(
        (v3_2_fixture_root / "dataforge" / "composite_ku_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert ku["schema"] == "cognityx.dataforge.composite-ku-handoff/v1"
    assert ku["seed"] == {"division_id": "div-policy-4.2"}
    assert "rel-ambiguous-example" in ku["excluded_relation_ids"]
    assert ku["expected_knowledge_unit"]["gold_support_contains_only_validated_relations"] is True


def test_frozen_t08_support_is_exact_and_ambiguous_relation_is_not_gold(
    v3_2_fixture_root,
):
    """Exercise public T08 readers and resolver over the exact frozen proof."""
    graph = SourceGraph.from_json_bytes(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_bytes(),
        compact_fixture=True,
    )
    catalog = ProvenanceAddressCatalog.from_json_bytes(
        (v3_2_fixture_root / "expected" / "provenance_addresses.json").read_bytes(),
        compact_fixture=True,
    )
    resolution = ProvenanceAddressResolver(graph, catalog).resolve(
        "addr-strong-pol-p2"
    )

    assert graph.graph_revision == "sg-rev-001"
    assert resolution.status == "exact"
    assert resolution.target is not None
    assert resolution.target.node_id == "pol-p2"
    assert [item.relation_id for item in graph.outgoing("pol-p1")] == []
    assert [
        item.relation_id for item in graph.outgoing("pol-p1", gold_only=False)
    ] == ["rel-ambiguous-example"]


def test_run_manifest_publishes_ordered_reference_only_bundles(
    tmp_path: Path,
) -> None:
    """Match each successful producer ref to its persisted provenance v2 URIs."""
    service, registry, storage, context = _producer_components(tmp_path / "storage")
    paths = (tmp_path / "second.pdf", tmp_path / "first.pdf")
    for path in paths:
        path.write_bytes(path.name.encode("utf-8"))
    assets = tuple(registry.register_asset(context, path) for path in paths)

    run = service.ingest_assets(
        tuple(item.asset_id for item in assets),
        registry,
        context,
        submitted_input={"type": "t09-test"},
    )
    manifest = json.load(storage.open(run.run_manifest_key))
    refs = manifest["dataforge_source_refs"]

    assert [item["document_id"] for item in refs] == manifest["document_ids"]
    assert len(refs) == len(run.results) == 2
    assert manifest["evidence_refs"]
    assert manifest["provenance_refs"]
    assert manifest["document_manifest_refs"]
    for result, ref in zip(run.results, refs, strict=True):
        provenance = json.load(storage.open(result.provenance_key))
        assert ref == {
            "document_id": result.document.document_id,
            "provenance_uri": provenance["artifact_uris"]["provenance"],
            "canonical_content_uri": provenance["artifact_uris"]["canonical_content"],
            "source_graph_uri": provenance["artifact_uris"]["source_graph"],
            "provenance_addresses_uri": provenance["artifact_uris"][
                "provenance_addresses"
            ],
        }
        assert all(
            value.startswith("storage://")
            for key, value in ref.items()
            if key.endswith("_uri")
        )
        assert set(ref) == {
            "document_id",
            "provenance_uri",
            "canonical_content_uri",
            "source_graph_uri",
            "provenance_addresses_uri",
        }


def test_failed_document_has_no_dataforge_source_ref(tmp_path: Path) -> None:
    """Keep partial failure auditable without fabricating T08 artifact bundles."""
    service, registry, storage, context = _producer_components(tmp_path / "storage")
    good = tmp_path / "good.pdf"
    bad = tmp_path / "bad.pdf"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    assets = (
        registry.register_asset(context, good),
        registry.register_asset(context, bad),
    )

    run = service.ingest_assets(
        tuple(item.asset_id for item in assets),
        registry,
        context,
        submitted_input={"type": "t09-partial-test"},
    )
    manifest = json.load(storage.open(run.run_manifest_key))

    assert len(manifest["dataforge_source_refs"]) == 1
    assert manifest["dataforge_source_refs"][0]["document_id"] == (
        run.results[0].document.document_id
    )
    assert manifest["failed_files"][0]["asset_id"] == assets[1].asset_id
