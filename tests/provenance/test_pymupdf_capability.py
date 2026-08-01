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


@pytest.mark.xfail(
    strict=True,
    reason="GAP-RICH-STRUCTURE: docs/provenance-gap-report.md#gap-rich-structure",
)
def test_pymupdf_distinguishes_printed_labels_from_native_labels(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyMuPDFParser().extract_document(provenance_pdf)
    assert [page.printed_page_label for page in extraction.pages] == [
        page["printed_label"] for page in ground_truth["pages"]
    ]
