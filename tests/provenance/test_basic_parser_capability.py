from __future__ import annotations

from pathlib import Path

from cognityx_ingest import PyPdfExtractor


def test_basic_parser_reports_exact_pages_text_and_honest_capabilities(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = PyPdfExtractor().extract_document(provenance_pdf)
    expected_pages = ground_truth["pages"]

    assert extraction.backend == "basic"
    assert len(extraction.pages) == ground_truth["document"]["page_count"]
    assert [page.physical_page_index for page in extraction.pages] == list(range(19))
    for expected, actual in zip(expected_pages, extraction.pages, strict=True):
        assert " ".join(expected["canary"].split()) in " ".join(actual.text.split())
        assert actual.page_number == expected["sequence_number"]
    assert not extraction.sections
    assert all(not page.blocks for page in extraction.pages)
    assert all(not page.objects for page in extraction.pages)
    assert all(not page.relations for page in extraction.pages)
