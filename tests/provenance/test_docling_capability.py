from __future__ import annotations

from pathlib import Path

import pytest

from cognityx_ingest import DoclingParser


pytest.importorskip(
    "docling", reason="Docling capability suite requires cognityx-ingest[docling]"
)


def test_docling_preserves_required_rich_structure(
    provenance_pdf: Path, ground_truth: dict[str, object]
) -> None:
    extraction = DoclingParser().extract_document(provenance_pdf)
    block_types = {
        block.block_type for page in extraction.pages for block in page.blocks
    }
    object_types = {
        item.object_type for page in extraction.pages for item in page.objects
    }

    assert {"section_header", "text", "list_item", "caption"} <= block_types
    assert {"table", "figure"} <= object_types
    assert extraction.sections
