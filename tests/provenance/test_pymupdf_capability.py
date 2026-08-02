from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import PyMuPDFParser


fitz = pytest.importorskip(
    "fitz", reason="PyMuPDF capability suite requires cognityx-ingest[pymupdf]"
)


def test_pymupdf_preserves_native_pdf_facts(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyMuPDFParser().extract_document(provenance_pdf)

    assert extraction.backend == "pymupdf"
    assert len(extraction.pages) == 19
    assert sum(len(page.blocks) for page in extraction.pages) > 19
    assert any(page.objects for page in extraction.pages)
    links = [relation for page in extraction.pages for relation in page.relations]
    assert len(links) == 2
    assert any(link.target_text == "https://example.com/lunavane/safety" for link in links)


def test_pymupdf_native_internal_link_matches_frozen_oracle(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyMuPDFParser().extract_document(provenance_pdf)
    page = extraction.pages[14]
    link = next(
        relation
        for relation in page.relations
        if relation.target_anchor == "page:17"
    )
    assert link.bbox is not None

    content_blocks = [
        block
        for block in page.blocks
        if block.block_type not in {"page_header", "page_footer"}
    ]
    source_ordinal, source_block = next(
        (ordinal, block)
        for ordinal, block in enumerate(content_blocks, start=1)
        if block.bbox is not None and _intersects(block.bbox, link.bbox)
    )
    expected = next(
        relation
        for relation in ground_truth["relations"]
        if relation["id"] == "rel-native-appendix-b"
    )

    assert "Appendix B" in source_block.text
    assert expected == {
        "id": "rel-native-appendix-b",
        "literal": "Appendix B",
        "source": f"page-014:block-{source_ordinal:03d}",
        "targets": ["appendix-b"],
        "type": "hyperlink",
        "status": "observed",
        "method": "native_pdf",
    }


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def test_pymupdf_distinguishes_printed_labels_from_native_labels(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyMuPDFParser().extract_document(provenance_pdf)
    assert [page.printed_page_label for page in extraction.pages] == [
        page["printed_label"] for page in ground_truth["pages"]
    ]
    assert [page.page_label for page in extraction.pages] == [
        page["native_pdf_label"] for page in ground_truth["pages"]
    ]


def test_pymupdf_classifies_repeated_headers_and_footers(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyMuPDFParser().extract_document(provenance_pdf)

    for page in extraction.pages:
        headers = [block for block in page.blocks if block.block_type == "page_header"]
        footers = [block for block in page.blocks if block.block_type == "page_footer"]
        assert [block.text for block in headers] == [
            ground_truth["repeated_regions"]["header"]
        ]
        assert len(footers) == 1
        assert footers[0].text.startswith(
            ground_truth["repeated_regions"]["footer_prefix"]
        )
        assert headers[0].method == "deterministic_repeated_margin"
        assert footers[0].confidence == 1.0
        assert headers[0].text not in page.text
        assert footers[0].text not in page.text
