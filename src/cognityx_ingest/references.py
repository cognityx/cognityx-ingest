"""Deterministic references resolved over canonical document structure."""

from __future__ import annotations

import hashlib
import re

from .models import Block, PageRecord, Relation, Section, UnresolvedItem
from .parser import ExtractionResult


_PLURAL_SECTION = re.compile(
    r"\bSections\s+(?P<first>\d+(?:\.\d+)*)(?P<rest>(?:\s*(?:,|and)\s*\d+(?:\.\d+)*)+)"
)
_SECTION_NUMBER = re.compile(r"\bSection(?!s)\s+(?P<number>\d+(?:\.\d+)*)")
_SECTION_IN_REST = re.compile(r"\d+(?:\.\d+)*")
_APPENDIX = re.compile(r"\bAppendix\s+(?P<number>[A-Z])\b")
_PRINTED_PAGE = re.compile(
    r"\bprinted page\s+(?P<label>[A-Z]-\d+|[ivxlcdm]+|\d+)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\]\[()]+")
_DOCUMENT_VERSION = re.compile(
    r"(?P<literal>[A-Z][A-Za-z0-9 &'’-]+(?:Handbook|Manual|Policy),\s*"
    r"Version\s+\d+(?:\.\d+)*)"
)
_CONTENT_TYPES = frozenset({"paragraph", "list", "table", "hyperlink", "url"})


def build_reference_provenance(
    document_id: str,
    extraction: ExtractionResult,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    sections: tuple[Section, ...],
) -> tuple[tuple[Relation, ...], tuple[UnresolvedItem, ...], frozenset[str]]:
    """Detect exact references and map them to stable canonical anchors."""
    section_by_number = {
        section.number.casefold(): section
        for section in sections
        if section.number is not None
    }
    page_by_label = {
        page.printed_page_label.casefold(): page
        for page in pages
        if page.printed_page_label
    }
    page_by_index = {page.physical_page_index: page for page in pages}
    blocks_by_page: dict[str, list[Block]] = {}
    for block in blocks:
        blocks_by_page.setdefault(block.page_id, []).append(block)

    relations: list[Relation] = []
    unresolved: list[UnresolvedItem] = []
    seen: set[tuple[str, str, str, str | None, str]] = set()

    def emit(
        source: Block,
        literal: str,
        target: str,
        relation_type: str,
        method: str,
        status: str = "resolved",
        confidence: float = 1.0,
    ) -> None:
        key = (source.block_id, literal, relation_type, target, method)
        if key in seen:
            return
        seen.add(key)
        relations.append(
            Relation(
                relation_id=_stable_id(document_id, *key),
                source_anchor_id=source.block_id,
                target_anchor_id=target,
                relation_type=relation_type,
                status=status,
                target_text=literal,
                method=method,
                confidence=confidence,
            )
        )

    for block in blocks:
        if block.block_type not in _CONTENT_TYPES:
            continue
        text = " ".join(block.text.split())
        for match in _PLURAL_SECTION.finditer(text):
            literal = match.group(0)
            numbers = (
                match.group("first"),
                *_SECTION_IN_REST.findall(match.group("rest")),
            )
            for number in numbers:
                section = section_by_number.get(number.casefold())
                if section is not None:
                    emit(
                        block,
                        literal,
                        section.section_id,
                        "references",
                        "deterministic_exact",
                    )
        for match in _SECTION_NUMBER.finditer(text):
            section = section_by_number.get(match.group("number").casefold())
            if section is not None:
                emit(
                    block,
                    match.group(0),
                    section.section_id,
                    "references",
                    "deterministic_exact",
                )
        for match in _APPENDIX.finditer(text):
            section = section_by_number.get(match.group("number").casefold())
            if section is not None:
                emit(
                    block,
                    match.group(0),
                    section.section_id,
                    "references",
                    "deterministic_exact",
                )
        for match in _PRINTED_PAGE.finditer(text):
            page = page_by_label.get(match.group("label").casefold())
            if page is not None:
                emit(
                    block,
                    match.group(0),
                    page.page_id,
                    "page_reference",
                    "deterministic_printed_label",
                )
        for match in _URL.finditer(text):
            literal = match.group(0).rstrip(".,;:")
            emit(block, literal, literal, "url", "deterministic_url")
        for match in _DOCUMENT_VERSION.finditer(text):
            literal = match.group("literal")
            task_id = _stable_id(
                document_id,
                block.block_id,
                literal,
                "cross_document_reference",
                None,
                "deterministic_absent_target",
            )
            if any(item.task_id == task_id for item in unresolved):
                continue
            unresolved.append(
                UnresolvedItem(
                    task_id=task_id,
                    source_anchor_id=block.block_id,
                    relation_type="cross_document_reference",
                    target_text=literal,
                    reason="document_not_in_corpus",
                    status="unresolved",
                    method="deterministic_absent_target",
                    confidence=1.0,
                )
            )

    handled_parser_relations: set[str] = set()
    for extracted_page in extraction.pages:
        page = page_by_index.get(extracted_page.physical_page_index)
        if page is None:
            continue
        page_blocks = blocks_by_page.get(page.page_id, ())
        for observed in extracted_page.relations:
            source = _source_block(page_blocks, observed.bbox)
            if source is None:
                continue
            if observed.target_text and observed.target_text.startswith(
                ("http://", "https://")
            ):
                literal = _visible_link_text(source.text, observed.target_text)
                emit(
                    source,
                    literal,
                    observed.target_text,
                    "hyperlink",
                    "native_pdf",
                    status="observed",
                    confidence=observed.confidence or 1.0,
                )
                handled_parser_relations.add(observed.relation_id)
                continue
            target_index = _target_page_index(observed.target_anchor)
            target_page = (
                page_by_index.get(target_index) if target_index is not None else None
            )
            target_section = (
                _section_starting_on(target_page.page_id, sections)
                if target_page is not None
                else None
            )
            if target_section is None:
                continue
            literal = _section_literal(target_section)
            if literal.casefold() not in source.text.casefold():
                continue
            emit(
                source,
                literal,
                target_section.section_id,
                "hyperlink",
                "native_pdf",
                status="observed",
                confidence=observed.confidence or 1.0,
            )
            handled_parser_relations.add(observed.relation_id)

    return tuple(relations), tuple(unresolved), frozenset(handled_parser_relations)


def _stable_id(document_id: str, *parts: object) -> str:
    value = "\x1f".join("" if item is None else str(item) for item in parts)
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{document_id}:relation:reference:{digest}"


def _source_block(
    blocks: list[Block] | tuple[Block, ...],
    bbox: tuple[float, float, float, float] | None,
) -> Block | None:
    if bbox is None:
        return None
    candidates = [
        block
        for block in blocks
        if block.bbox is not None and _intersects(block.bbox, bbox)
    ]
    return min(candidates, key=lambda item: _area(item.bbox), default=None)


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def _area(bbox: tuple[float, float, float, float] | None) -> float:
    if bbox is None:
        return float("inf")
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _visible_link_text(block_text: str, uri: str) -> str:
    text = " ".join(block_text.split())
    if uri in text:
        return uri
    return text.rsplit(": ", maxsplit=1)[-1]


def _target_page_index(anchor: str | None) -> int | None:
    if not anchor or not anchor.startswith("page:"):
        return None
    try:
        return int(anchor.removeprefix("page:"))
    except ValueError:
        return None


def _section_starting_on(page_id: str, sections: tuple[Section, ...]) -> Section | None:
    candidates = [
        section
        for section in sections
        if section.heading_block_id
        and section.heading_block_id.startswith(f"{page_id}:block:")
    ]
    return min(
        candidates,
        key=lambda item: (item.level or 0, item.heading_block_id or ""),
        default=None,
    )


def _section_literal(section: Section) -> str:
    number = section.number or ""
    return (
        f"Appendix {number}"
        if len(number) == 1 and number.isalpha()
        else f"Section {number}"
    )
