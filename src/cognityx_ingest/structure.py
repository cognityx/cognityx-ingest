"""Deterministic canonical structure built from backend-neutral observations."""

from __future__ import annotations

from dataclasses import dataclass
import re

from cognityx_ingest.models import Block, Evidence, PageRecord, Section


_NUMBERED_HEADING = re.compile(
    r"^(?P<number>(?:\d+|[A-Z])(?:\.\d+)*)\.\s+(?P<title>\S.*)$"
)
_APPENDIX_HEADING = re.compile(
    r"^Appendix\s+(?P<number>[A-Z])\.\s+(?P<title>\S.*)$"
)
_CALLOUT = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9./]+)+(?:\s|$)")


@dataclass(frozen=True, slots=True)
class HeadingCandidate:
    number: str
    title: str
    level: int


def normalize_heading_candidate(text: str) -> HeadingCandidate | None:
    """Return a numbered heading without depending on parser-private labels."""
    normalized = text.strip()
    match = _APPENDIX_HEADING.fullmatch(normalized) or _NUMBERED_HEADING.fullmatch(
        normalized
    )
    if match is None:
        return None
    number = match.group("number")
    return HeadingCandidate(
        number=number,
        title=match.group("title"),
        level=number.count(".") + 1,
    )


def canonical_block_type(text: str, observed_type: str) -> str:
    """Normalize parser block labels while preserving deterministic semantics."""
    if observed_type in {"page_header", "page_footer"}:
        return observed_type
    if normalize_heading_candidate(text) is not None:
        return "heading"
    if observed_type in {"table", "figure", "caption", "footnote"}:
        return observed_type
    if observed_type in {"list", "list_item"} or text.lstrip().startswith("•"):
        return "list"
    if _CALLOUT.match(text.strip()):
        return "callout"
    return "paragraph"


def build_same_page_sections(
    document_id: str,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    evidence: tuple[Evidence, ...],
) -> tuple[Section, ...]:
    """Build hierarchy and spans that stop at boundaries observed on one page."""
    blocks_by_id = {block.block_id: block for block in blocks}
    evidence_by_page = {
        item.physical_page_index: item
        for item in evidence
        if item.physical_page_index is not None
    }
    observed: list[tuple[PageRecord, int, Block, HeadingCandidate]] = []
    content_by_page: dict[str, tuple[str, ...]] = {}
    for page in pages:
        content_ids = tuple(
            block_id
            for block_id in page.block_ids
            if blocks_by_id[block_id].block_type
            not in {"page_header", "page_footer"}
        )
        content_by_page[page.page_id] = content_ids
        for position, block_id in enumerate(content_ids):
            block = blocks_by_id[block_id]
            candidate = normalize_heading_candidate(block.text)
            if candidate is not None:
                observed.append((page, position, block, candidate))

    if not observed:
        return ()

    parent_by_block: dict[str, str | None] = {}
    path_by_block: dict[str, tuple[str, ...]] = {}
    section_id_by_block: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for _page, _position, block, candidate in observed:
        while stack and stack[-1][0] >= candidate.level:
            stack.pop()
        parent_block_id = stack[-1][1] if stack else None
        parent_by_block[block.block_id] = (
            section_id_by_block[parent_block_id] if parent_block_id else None
        )
        parent_path = path_by_block[parent_block_id] if parent_block_id else ()
        path_by_block[block.block_id] = (*parent_path, candidate.number)
        section_id_by_block[block.block_id] = (
            f"{document_id}:section:{candidate.number.lower()}"
        )
        stack.append((candidate.level, block.block_id))

    result: list[Section] = []
    for index, (page, position, block, candidate) in enumerate(observed):
        content_ids = content_by_page[page.page_id]
        end_position = len(content_ids) - 1
        later_headings = observed[index + 1 :]
        for next_page, next_position, _next_block, next_candidate in later_headings:
            if next_page.page_id != page.page_id:
                break
            if next_candidate.level <= candidate.level:
                end_position = next_position - 1
                break
        selected_block_ids = content_ids[position : end_position + 1]
        page_evidence = evidence_by_page.get(page.physical_page_index)
        result.append(
            Section(
                section_id=section_id_by_block[block.block_id],
                number=candidate.number,
                title=candidate.title,
                level=candidate.level,
                parent_section_id=parent_by_block[block.block_id],
                path=path_by_block[block.block_id],
                heading_block_id=block.block_id,
                start_block_id=block.block_id,
                end_block_id=selected_block_ids[-1],
                evidence_ids=((page_evidence.evidence_id,) if page_evidence else ()),
                page_ids=(page.page_id,),
                block_ids=selected_block_ids,
                method="deterministic_numbered_heading",
                confidence=1.0,
            )
        )
    return tuple(result)
