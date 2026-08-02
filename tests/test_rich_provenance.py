from __future__ import annotations

import json
from pathlib import Path

from cognityx_ingest import (
    BoundedInferenceResolver,
    CanonicalDocument,
    EnrichmentIdentity,
    ExtractedBlock,
    ExtractedObject,
    ExtractedPage,
    ExtractedRelation,
    ExtractionPolicy,
    ExtractionResult,
    InferenceResolutionConfig,
    InferenceTarget,
    IngestService,
    ParserRouter,
    SourceAssetRegistry,
)
from cognityx_resource import ExecutionContext
from cognityx_storage import LocalStorageBackend, StorageClient, StorageConfig, StorageRuntime


class RichParser:
    name = "rich-test"

    def extract_document(self, path: Path) -> ExtractionResult:
        return ExtractionResult(
            backend=self.name,
            backend_version="1",
            considered_backends=("basic", "rich-test"),
            selected_reason="test policy",
            raw_artifact=b'{"raw":true}',
            pages=(
                ExtractedPage(
                    1,
                    "First page",
                    page_index=0,
                    page_label="i",
                    printed_page_label="1",
                    blocks=(ExtractedBlock("b1", "First page", 1),),
                    objects=(ExtractedObject("f1", "figure", 0, caption="Diagram"),),
                    relations=(
                        ExtractedRelation(
                            "r1",
                            "page:0",
                            None,
                            "references",
                            target_text="the next page",
                        ),
                    ),
                ),
                ExtractedPage(2, "Second page", page_index=1, page_label="ii"),
            ),
        )


class InferenceClient:
    def __init__(self, target_anchor: str) -> None:
        self.target_anchor = target_anchor
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": "request-1",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "target_anchor_id": self.target_anchor,
                                "relation_type": "references",
                                "confidence": 0.8,
                                "reason": "explicit continuation wording",
                            }
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 20},
            "cognityx": {"timings": {"latency_seconds": 0.1}},
        }


def _components(tmp_path: Path, client: InferenceClient):
    runtime = StorageRuntime.from_config(StorageConfig.built_in(root=tmp_path / "runtime"))
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    resolver = BoundedInferenceResolver(
        InferenceResolutionConfig(
            targets=(InferenceTarget(model="approved/model"),), max_calls=1
        ),
        client=client,
    )
    return IngestService(
        storage, extractor=RichParser(), resolver=resolver, registry=registry
    ), registry, storage


def test_rich_provenance_and_validated_inference_are_immutable(tmp_path: Path) -> None:
    run_id = "run-rich"
    expected_target = "placeholder"
    client = InferenceClient(expected_target)
    service, registry, storage = _components(tmp_path, client)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    context = ExecutionContext(run_id=run_id, correlation_id="corr", principal_id="alex")
    registered = registry.register_asset(context, source, bundle="legal/hr")

    document_id = f"pdf-{registered.sha256[:12]}-{registered.asset_id[4:12]}-" + __import__("hashlib").sha256(run_id.encode()).hexdigest()[:8]
    client.target_anchor = f"{document_id}:page-index:1"
    result = service.ingest_asset(registered.asset_id, registry, context)
    document = result.document

    assert document.schema_version == "cognityx.ingest.document/v2"
    assert [page.physical_page_index for page in document.pages] == [0, 1]
    assert document.pages[0].pdf_page_label == "i"
    assert document.sections[0].page_ids == tuple(page.page_id for page in document.pages)
    assert document.objects[0].object_type == "figure"
    assert document.relations[0].status == "inferred"
    assert document.relations[0].target_anchor_id == document.pages[1].page_id
    assert document.decisions[-1].request_id == "request-1"
    assert document.decisions[-1].configuration_hash
    assert not document.unresolved
    assert client.calls[0]["provider"] == "local"
    assert client.calls[0]["model"] == "approved/model"
    assert client.calls[0]["response_format"] == {"type": "json_object"}

    provenance = json.load(storage.open(result.provenance_key))
    manifest = json.load(storage.open(result.manifest_key))
    assert provenance["source_asset"]["blob_sha256"] == registered.sha256
    assert provenance["parser"]["selected"] == "rich-test"
    assert manifest["artifacts"]["provenance"]["uri"].startswith("storage://")
    assert result.raw_parser_key and storage.exists(result.raw_parser_key)


def test_invalid_inference_anchor_remains_unresolved(tmp_path: Path) -> None:
    client = InferenceClient("invented-anchor")
    service, registry, _ = _components(tmp_path, client)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    context = ExecutionContext(run_id="run-invalid", correlation_id="corr", principal_id="alex")
    registered = registry.register_asset(context, source)

    result = service.ingest_asset(registered.asset_id, registry, context)

    assert not result.document.relations
    assert result.document.unresolved[0].reason == "target_anchor_not_found"
    assert result.document.decisions[-1].status == "rejected"


class Plugin:
    def __init__(self, name: str, richness: int) -> None:
        self.name = name
        self.richness = richness

    def extract_document(self, path: Path) -> ExtractionResult:
        blocks = tuple(ExtractedBlock(str(i), "x", i) for i in range(self.richness))
        return ExtractionResult((ExtractedPage(1, "x", blocks=blocks),), self.name)


def test_parser_policies_keep_one_normalized_contract(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    plugins = (Plugin("basic", 0), Plugin("rich", 2))

    fixed = ParserRouter(plugins, policy=ExtractionPolicy("fixed", ("basic", "rich")))
    compared = ParserRouter(plugins, policy=ExtractionPolicy("compare", ("basic", "rich")))

    assert fixed.extract_document(source).backend == "basic"
    assert compared.extract_document(source).backend == "fusion"
    assert compared.extract_document(source).selected_reason == "canonical_fact_level_fusion"


class FactPlugin:
    def __init__(self, result: ExtractionResult) -> None:
        self.name = result.backend
        self.result = result

    def extract_document(self, path: Path) -> ExtractionResult:
        return self.result


def test_compare_fuses_facts_independently_of_backend_order(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    basic = ExtractionResult(
        pages=(ExtractedPage(1, "Basic fallback", page_index=0),),
        backend="basic",
        raw_artifact=b'{"basic":true}',
    )
    pymupdf = ExtractionResult(
        pages=(
            ExtractedPage(
                1,
                "Shared heading\nNative body",
                page_index=0,
                page_label="i",
                printed_page_label="1",
                width=600.0,
                height=800.0,
                blocks=(
                    ExtractedBlock(
                        "pymu-heading",
                        "Shared heading",
                        1,
                        block_type="text",
                        bbox=(10.0, 10.0, 200.0, 30.0),
                        method="native_layout",
                        confidence=1.0,
                    ),
                ),
                relations=(
                    ExtractedRelation(
                        "native-link",
                        "page:0",
                        None,
                        "link",
                        target_text="https://example.test",
                        status="observed",
                        method="native_pdf",
                        confidence=1.0,
                    ),
                ),
            ),
        ),
        backend="pymupdf",
        raw_artifact=b'{"pymupdf":true}',
    )
    docling = ExtractionResult(
        pages=(
            ExtractedPage(
                1,
                "Shared heading\nDocling body",
                page_index=0,
                blocks=(
                    ExtractedBlock(
                        "docling-heading",
                        "Shared heading",
                        1,
                        block_type="section_header",
                        bbox=(10.0, 10.0, 200.0, 30.0),
                        method="docling_structure",
                        confidence=0.9,
                    ),
                    ExtractedBlock(
                        "docling-body", "Docling body", 2, block_type="text"
                    ),
                ),
                objects=(
                    ExtractedObject(
                        "docling-table",
                        "table",
                        0,
                        caption="Table A",
                        method="docling_structure",
                        confidence=0.9,
                    ),
                ),
            ),
        ),
        backend="docling",
        raw_artifact=b'{"docling":true}',
    )
    plugins = tuple(FactPlugin(item) for item in (basic, pymupdf, docling))

    first = ParserRouter(
        plugins,
        policy=ExtractionPolicy("compare", ("basic", "pymupdf", "docling")),
    ).extract_document(source)
    second = ParserRouter(
        tuple(reversed(plugins)),
        policy=ExtractionPolicy("compare", ("docling", "pymupdf", "basic")),
    ).extract_document(source)

    assert first == second
    assert first.backend == "fusion"
    assert first.pages[0].text != "Basic fallback"
    assert first.pages[0].page_label == "i"
    assert first.pages[0].source_backends == ("basic", "docling", "pymupdf")
    heading = next(block for block in first.pages[0].blocks if block.text == "Shared heading")
    assert heading.block_type == "section_header"
    assert heading.source_backends == ("docling", "pymupdf")
    assert any(item.object_type == "table" for item in first.pages[0].objects)
    assert any(item.target_text == "https://example.test" for item in first.pages[0].relations)
    assert first.raw_artifacts.keys() == {"basic", "docling", "pymupdf"}
    assert first.diagnostics["conflicts"]

    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "fusion-runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    context = ExecutionContext(
        run_id="run-fusion-order",
        correlation_id="correlation-fusion-order",
        principal_id="fusion-test",
    )
    asset = registry.register_asset(context, source)
    stored: list[dict[str, object]] = []
    for position, extraction in enumerate((first, second), start=1):
        storage = StorageClient(
            LocalStorageBackend(tmp_path / f"fusion-artifacts-{position}")
        ).for_shared_data()
        result = IngestService(
            storage,
            extractor=FactPlugin(extraction),
            registry=registry,
        ).ingest_asset(asset.asset_id, registry, context)
        stored.append(json.load(storage.open(result.provenance_key)))
        assert len(result.raw_parser_keys) == 4
        assert all(storage.exists(key) for key in result.raw_parser_keys)

    assert stored[0] == stored[1]
    assert stored[0]["parser"]["diagnostics"]["conflicts"]
    assert {item["backend"] for item in stored[0]["parser"]["raw_artifacts"]} == {
        "basic",
        "docling",
        "fusion",
        "pymupdf",
    }


def test_v1_document_reader_and_enrichment_identity_are_stable() -> None:
    legacy = CanonicalDocument.from_dict(
        {
            "document_id": "pdf-old",
            "schema_version": "cognityx.ingest.document/v1",
            "source": {
                "source_id": "src-old",
                "filename": "old.pdf",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "storage_key": "sourceasset://src-old",
                "media_type": "application/pdf",
            },
            "title": "Old",
            "sections": [],
        }
    )
    first = EnrichmentIdentity.create(
        source_content_hash="a" * 64,
        source_anchor_ids=("b", "a"),
        representation_type="embedding",
        generation_method="model",
        model_version="v1",
        configuration={"dimensions": 3},
    )
    second = EnrichmentIdentity.create(
        source_content_hash="a" * 64,
        source_anchor_ids=("a", "b"),
        representation_type="embedding",
        generation_method="model",
        model_version="v1",
        configuration={"dimensions": 3},
    )

    assert legacy.pages == ()
    assert first.enrichment_id == second.enrichment_id
