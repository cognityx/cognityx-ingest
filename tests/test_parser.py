from __future__ import annotations

from cognityx_ingest import Evidence, ExtractedBlock, ExtractedPage, ExtractionResult
from cognityx_ingest.parser import _classify_repeated_page_regions
from cognityx_ingest.service import _canonical_sections, _canonical_structure


def test_repeated_regions_only_page_has_empty_content_and_evidence() -> None:
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

    extraction = ExtractionResult(pages=pages, backend="test")
    page_records, blocks, _objects = _canonical_structure("document", extraction)
    evidence = tuple(
        Evidence(
            evidence_id=f"evidence-{page.page_number}",
            document_id="document",
            page_number=page.page_number,
            text=page.text,
            char_start=0,
            char_end=len(page.text),
            physical_page_index=page.physical_page_index,
        )
        for page in pages
    )
    sections = _canonical_sections(
        "document", "title", extraction, page_records, blocks, evidence
    )

    assert all(item.text == "" for item in evidence)
    assert all(section.block_ids == () for section in sections)
