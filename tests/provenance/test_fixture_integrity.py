from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_frozen_fixture_checksums(provenance_fixture_root: Path) -> None:
    checksum_file = provenance_fixture_root / "expected" / "sha256sums.txt"
    entries = [
        line.split(maxsplit=1)
        for line in checksum_file.read_text().splitlines()
        if line.strip()
    ]

    assert entries
    for expected, relative_path in entries:
        assert _sha256(provenance_fixture_root / relative_path) == expected


def test_ground_truth_is_self_consistent(
    ground_truth: dict[str, object], provenance_fixture_root: Path
) -> None:
    pages = ground_truth["pages"]
    assert isinstance(pages, list)
    page_ids = {page["id"] for page in pages}
    anchors = {
        f"{page['id']}:block-{ordinal:03d}"
        for page in pages
        for ordinal, _kind, _text in page["blocks"]
    }
    section_ids = {section["id"] for section in ground_truth["sections"]}
    object_ids = {item["id"] for item in ground_truth["objects"]}
    related_ids = {item["id"] for item in ground_truth["related_documents"]}

    assert [page["physical_index"] for page in pages] == list(range(19))
    assert [page["sequence_number"] for page in pages] == list(range(1, 20))
    for section in ground_truth["sections"]:
        assert section["start"] in anchors
        assert section["end"] in anchors
        assert section["parent"] is None or section["parent"] in section_ids
    for relation in ground_truth["relations"]:
        assert relation["source"] in anchors
        for target in relation["targets"]:
            if ":" not in target and not target.startswith("https://"):
                assert target in section_ids | object_ids | page_ids | related_ids
    for item in ground_truth["objects"]:
        if image := item.get("image"):
            assert (provenance_fixture_root / image).is_file()

    assert ground_truth["dataforge_handoff"]["forbidden_fields"] == [
        "embedding",
        "vector_index",
        "vector_database",
    ]


def test_authoritative_named_docx_copy_is_frozen_without_reconstruction(
    provenance_fixture_root: Path, ground_truth: dict[str, object]
) -> None:
    source = provenance_fixture_root / "main_policy_v2.docx"
    pdf = provenance_fixture_root / "main_policy_v2.pdf"

    assert source.read_bytes() == pdf.read_bytes()
    assert source.read_bytes().startswith(b"%PDF-1.7")
    assert _sha256(source) == ground_truth["document"]["frozen_source_sha256"]
    assert ground_truth["document"]["frozen_source_container"] == "PDF 1.7"
