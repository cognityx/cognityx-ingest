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
