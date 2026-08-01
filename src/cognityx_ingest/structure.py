"""Deterministic canonical structure built from backend-neutral observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from cognityx_ingest.models import Block, Evidence, PageRecord, Section


_NUMBERED_HEADING = re.compile(
    r"^(?P<number>(?:\d+|[A-Z])(?:\.\d+)*)\.\s+(?P<title>\S.*)$"
)
_APPENDIX_HEADING = re.compile(
    r"^Appendix\s+(?P<number>[A-Z])\.\s+(?P<title>\S.*)$"
)
_CALLOUT = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9./]+)+(?:\s|$)")
_BULLET_BOUNDARY = re.compile(r"(?:^|\n)•\s*\n")
_FALSE_CONTINUATION = re.compile(
    r"(?<=[.!?])\s+(?P<control>The next page starts (?:a )?new section.*)$",
    re.DOTALL,
)


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
    text: str, observed_type: str
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
    false_control = _FALSE_CONTINUATION.search(text)
    if false_control is not None:
        content = text[: false_control.start("control")].strip()
        control = false_control.group("control").strip()
        return (
            CanonicalBlockFragment(
                content,
                canonical_block_type(content, observed_type),
            ),
            CanonicalBlockFragment(
                control,
                "page_break_control",
                method="deterministic_page_break_control",
            ),
        )
    return (CanonicalBlockFragment(text, block_type),)


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
    return _apply_continuation_status(
        tuple(result), pages, content_by_page, blocks_by_id, evidence_by_page
    )


def _apply_continuation_status(
    sections: tuple[Section, ...],
    pages: tuple[PageRecord, ...],
    content_by_page: dict[str, tuple[str, ...]],
    blocks_by_id: dict[str, Block],
    evidence_by_page: dict[int, Evidence],
) -> tuple[Section, ...]:
    """Extend only the deepest active section when top-page content flows onward."""
    updated = list(sections)
    for page_index, page in enumerate(pages[:-1]):
        content_ids = content_by_page[page.page_id]
        if not content_ids:
            continue
        active = [
            (index, section)
            for index, section in enumerate(updated)
            if section.page_ids == (page.page_id,)
            and section.end_block_id == content_ids[-1]
        ]
        if not active:
            continue
        section_index, section = max(active, key=lambda item: item[1].level or 0)
        next_page = pages[page_index + 1]
        next_content = content_by_page[next_page.page_id]
        first_heading = next(
            (
                position
                for position, block_id in enumerate(next_content)
                if blocks_by_id[block_id].block_type == "heading"
            ),
            len(next_content),
        )
        leading_content = next_content[:first_heading]
        begins_at_top = bool(
            leading_content
            and next_page.height
            and blocks_by_id[leading_content[0]].bbox
            and blocks_by_id[leading_content[0]].bbox[1] <= next_page.height * 0.2
        )
        method = "deterministic_heading_content_flow"
        if leading_content and begins_at_top:
            next_evidence = evidence_by_page.get(next_page.physical_page_index)
            updated[section_index] = replace(
                section,
                page_ids=(*section.page_ids, next_page.page_id),
                block_ids=(*section.block_ids, *leading_content),
                evidence_ids=(
                    (*section.evidence_ids, next_evidence.evidence_id)
                    if next_evidence
                    else section.evidence_ids
                ),
                end_block_id=leading_content[-1],
                continuation_status="deterministic_true",
                continuation_method=method,
                continuation_confidence=1.0,
                continues_from=section.end_block_id,
                continues_to=leading_content[0],
            )
        else:
            updated[section_index] = replace(
                section,
                continuation_status="deterministic_false",
                continuation_method=method,
                continuation_confidence=1.0,
                continues_from=None,
                continues_to=None,
            )
    return tuple(updated)
