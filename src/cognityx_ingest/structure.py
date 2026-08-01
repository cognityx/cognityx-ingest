"""Deterministic canonical structure built from backend-neutral observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from cognityx_ingest.models import Block, Evidence, PageRecord, Relation, Section
from cognityx_ingest.parser import ExtractedPage


_NUMBERED_HEADING = re.compile(
    r"^(?P<number>(?:\d+|[A-Z])(?:\.\d+)*)\.\s+(?P<title>\S.*)$"
)
_APPENDIX_HEADING = re.compile(
    r"^Appendix\s+(?P<number>[A-Z])\.\s+(?P<title>\S.*)$"
)
_CALLOUT = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9./]+)+(?:\s|$)")
_BULLET_BOUNDARY = re.compile(r"(?:^|\n)•\s*\n")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=\S)")
_EARLY_PAGE_END_RATIO = 0.3
_CONTINUATION_METHOD = "deterministic_heading_content_layout_flow"


@dataclass(frozen=True, slots=True)
class HeadingCandidate:
    number: str
    title: str
    level: int


@dataclass(frozen=True, slots=True)
class CanonicalBlockFragment:
    text: str
    block_type: str
    method: str = "deterministic_block_type"
    confidence: float = 1.0


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


def canonical_block_fragments(
    text: str,
    observed_type: str,
    *,
    split_terminal_sentence: bool = False,
) -> tuple[CanonicalBlockFragment, ...]:
    """Split parser-observed groups only at deterministic semantic boundaries."""
    block_type = canonical_block_type(text, observed_type)
    if block_type in {"page_header", "page_footer"}:
        return (CanonicalBlockFragment(text, block_type),)
    if block_type == "list":
        items = tuple(
            item.strip()
            for item in _BULLET_BOUNDARY.split(text)
            if item.strip()
        )
        if len(items) > 1:
            return tuple(
                CanonicalBlockFragment(
                    item,
                    "list_item",
                    method="deterministic_list_item_split",
                )
                for item in items
            )
    sentence_boundaries = tuple(_SENTENCE_BOUNDARY.finditer(text))
    if split_terminal_sentence and sentence_boundaries:
        boundary = sentence_boundaries[-1]
        content = text[: boundary.start()].strip()
        terminal = text[boundary.end() :].strip()
        return (
            CanonicalBlockFragment(
                content,
                canonical_block_type(content, observed_type),
                method="deterministic_page_boundary_split",
            ),
            CanonicalBlockFragment(
                terminal,
                canonical_block_type(terminal, observed_type),
                method="deterministic_page_boundary_split",
            ),
        )
    return (CanonicalBlockFragment(text, block_type),)


def terminal_sentence_split_block_ids(
    pages: tuple[ExtractedPage, ...],
) -> frozenset[str]:
    """Find parser blocks needing a terminal anchor at an early page boundary."""
    selected: set[str] = set()
    for page, next_page in zip(pages, pages[1:], strict=False):
        content = tuple(
            block
            for block in page.blocks
            if block.block_type not in {"page_header", "page_footer"}
        )
        next_content = tuple(
            block
            for block in next_page.blocks
            if block.block_type not in {"page_header", "page_footer"}
        )
        if not content or not next_content:
            continue
        terminal = content[-1]
        ends_early = bool(
            page.height
            and terminal.bbox
            and terminal.bbox[3] <= page.height * _EARLY_PAGE_END_RATIO
        )
        if (
            ends_early
            and normalize_heading_candidate(next_content[0].text) is not None
            and _SENTENCE_BOUNDARY.search(terminal.text)
        ):
            selected.add(terminal.block_id)
    return frozenset(selected)


def build_sections(
    document_id: str,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    evidence: tuple[Evidence, ...],
) -> tuple[Section, ...]:
    """Build deterministic hierarchy, spans, and validated continuation status."""
    blocks_by_id = {block.block_id: block for block in blocks}
    evidence_by_page = {
        item.physical_page_index: item
        for item in evidence
        if item.physical_page_index is not None
    }
    observed: list[tuple[int, PageRecord, Block, HeadingCandidate]] = []
    content_by_page: dict[str, tuple[str, ...]] = {}
    content_stream: list[tuple[PageRecord, Block]] = []
    for page in pages:
        content_ids = tuple(
            block_id
            for block_id in page.block_ids
            if blocks_by_id[block_id].block_type
            not in {"page_header", "page_footer"}
        )
        content_by_page[page.page_id] = content_ids
        for block_id in content_ids:
            block = blocks_by_id[block_id]
            stream_position = len(content_stream)
            content_stream.append((page, block))
            candidate = normalize_heading_candidate(block.text)
            if candidate is not None:
                observed.append((stream_position, page, block, candidate))

    if not observed:
        return ()

    parent_by_block: dict[str, str | None] = {}
    path_by_block: dict[str, tuple[str, ...]] = {}
    section_id_by_block: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for _position, _page, block, candidate in observed:
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
    for index, (position, page, block, candidate) in enumerate(observed):
        end_position = len(content_stream)
        for next_position, _next_page, _next_block, next_candidate in observed[index + 1 :]:
            if next_candidate.level <= candidate.level:
                end_position = next_position
                break
        selected = content_stream[position:end_position]
        selected_block_ids = tuple(item.block_id for _item_page, item in selected)
        selected_pages = tuple(dict.fromkeys(item.page_id for item, _block in selected))
        selected_evidence = tuple(
            evidence_by_page[item.physical_page_index].evidence_id
            for item in dict.fromkeys(item for item, _block in selected)
            if item.physical_page_index in evidence_by_page
        )
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
                evidence_ids=selected_evidence,
                page_ids=selected_pages,
                block_ids=selected_block_ids,
                method="deterministic_numbered_heading",
                confidence=1.0,
            )
        )
    return _apply_continuation_status(
        tuple(result), pages, content_by_page, blocks_by_id
    )


def _apply_continuation_status(
    sections: tuple[Section, ...],
    pages: tuple[PageRecord, ...],
    content_by_page: dict[str, tuple[str, ...]],
    blocks_by_id: dict[str, Block],
) -> tuple[Section, ...]:
    """Record true spans and explicit peer-or-higher boundary rejection."""
    page_position = {page.page_id: index for index, page in enumerate(pages)}
    updated: list[Section] = []
    for section in sections:
        if len(section.page_ids) > 1:
            source, target = _first_page_transition(section.block_ids, blocks_by_id)
            updated.append(
                replace(
                    section,
                    continuation_status="deterministic_true",
                    continuation_method=_CONTINUATION_METHOD,
                    continuation_confidence=1.0,
                    continues_from=source,
                    continues_to=target,
                )
            )
            continue
        page_id = section.page_ids[0]
        page_index = page_position[page_id]
        page_content = content_by_page[page_id]
        next_content = (
            content_by_page[pages[page_index + 1].page_id]
            if page_index + 1 < len(pages)
            else ()
        )
        next_heading = (
            normalize_heading_candidate(blocks_by_id[next_content[0]].text)
            if next_content
            else None
        )
        rejected = bool(
            page_content
            and section.end_block_id == page_content[-1]
            and next_heading
            and section.level
            and next_heading.level <= section.level
        )
        updated.append(
            replace(
                section,
                continuation_status=("deterministic_false" if rejected else None),
                continuation_method=(_CONTINUATION_METHOD if rejected else None),
                continuation_confidence=(1.0 if rejected else None),
                continues_from=None,
                continues_to=None,
            )
        )
    return tuple(updated)


def build_continuation_relations(
    document_id: str,
    sections: tuple[Section, ...],
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
) -> tuple[Relation, ...]:
    """Emit one canonical relation for the deepest section at each page boundary."""
    blocks_by_id = {block.block_id: block for block in blocks}
    content_by_page = {
        page.page_id: tuple(
            block_id
            for block_id in page.block_ids
            if blocks_by_id[block_id].block_type
            not in {"page_header", "page_footer"}
        )
        for page in pages
    }
    relations: list[Relation] = []
    for page, next_page in zip(pages, pages[1:], strict=False):
        content = content_by_page[page.page_id]
        next_content = content_by_page[next_page.page_id]
        if not content or not next_content:
            continue
        source = content[-1]
        target = next_content[0]
        crossing = [
            section
            for section in sections
            if source in section.block_ids and target in section.block_ids
        ]
        if (
            crossing
            and blocks_by_id[target].block_type != "heading"
            and _is_top_page_block(next_page, blocks_by_id[target])
        ):
            relations.append(
                Relation(
                    relation_id=(
                        f"{document_id}:relation:continuation:"
                        f"{page.physical_page_index}:resolved"
                    ),
                    source_anchor_id=source,
                    target_anchor_id=target,
                    relation_type="continues_on",
                    status="resolved",
                    method=_CONTINUATION_METHOD,
                    confidence=1.0,
                )
            )
            continue
        next_heading = normalize_heading_candidate(blocks_by_id[target].text)
        rejected = [
            section
            for section in sections
            if section.end_block_id == source
            and next_heading
            and section.level
            and next_heading.level <= section.level
        ]
        if rejected and blocks_by_id[source].method == "deterministic_page_boundary_split":
            relations.append(
                Relation(
                    relation_id=(
                        f"{document_id}:relation:continuation:"
                        f"{page.physical_page_index}:rejected"
                    ),
                    source_anchor_id=source,
                    target_anchor_id=None,
                    relation_type="continues_on",
                    status="rejected",
                    method=_CONTINUATION_METHOD,
                    confidence=1.0,
                    reason="next_page_starts_with_peer_or_higher_heading",
                )
            )
    return tuple(relations)


def _first_page_transition(
    block_ids: tuple[str, ...], blocks_by_id: dict[str, Block]
) -> tuple[str, str]:
    for source, target in zip(block_ids, block_ids[1:], strict=False):
        if blocks_by_id[source].page_id != blocks_by_id[target].page_id:
            return source, target
    raise ValueError("A multi-page section must contain a page transition")


def _is_top_page_block(page: PageRecord, block: Block) -> bool:
    return bool(page.height and block.bbox and block.bbox[1] <= page.height * 0.2)
