from __future__ import annotations

from pathlib import Path

from cognityx_ingest import (
    ExtractedBlock,
    ExtractedPage,
    ExtractionResult,
    IngestService,
    SourceAssetRegistry,
)
from cognityx_ingest.parser import _classify_repeated_page_regions
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


class RepeatedRegionsOnlyParser:
    name = "repeated-regions-only"

    def __init__(self, pages: tuple[ExtractedPage, ...]) -> None:
        self.pages = pages

    def extract_document(self, path: Path) -> ExtractionResult:
        return ExtractionResult(pages=self.pages, backend=self.name)


def test_repeated_regions_only_page_has_empty_content_and_evidence(
    tmp_path: Path,
) -> None:
    pages = _classify_repeated_page_regions(
        tuple(
            ExtractedPage(
                page_number=index,
                page_index=index - 1,
                text=f"Policy header\nControlled fixture | Page {label}",
                width=100,
                height=100,
                blocks=(
                    ExtractedBlock(
                        f"header-{index}", "Policy header", 1, bbox=(0, 0, 100, 10)
                    ),
                    ExtractedBlock(
                        f"footer-{index}",
                        f"Controlled fixture | Page {label}",
                        2,
                        bbox=(0, 90, 100, 100),
                    ),
                ),
            )
            for index, label in enumerate(("i", "ii"), start=1)
        )
    )

    assert [page.printed_page_label for page in pages] == ["i", "ii"]
    assert all(page.text == "" for page in pages)
    assert all(
        [block.block_type for block in page.blocks] == ["page_header", "page_footer"]
        for page in pages
    )
    unstructured = _classify_repeated_page_regions(
        (ExtractedPage(page_number=1, text="Unstructured fallback"),)
    )
    assert unstructured[0].text == "Unstructured fallback"

    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic parser input")
    result = IngestService(
        storage, extractor=RepeatedRegionsOnlyParser(pages), registry=registry
    ).ingest(
        source,
        context=ExecutionContext(
            run_id="run-repeated-regions-only",
            correlation_id="correlation-repeated-regions-only",
            principal_id="parser-test",
        ),
        registry=registry,
    )

    assert all(item.text == "" for item in result.evidence)
    repeated_block_ids = {
        block.block_id
        for block in result.document.blocks
        if block.block_type in {"page_header", "page_footer"}
    }
    assert repeated_block_ids
    assert all(
        repeated_block_ids.isdisjoint(section.block_ids)
        for section in result.document.sections
    )
