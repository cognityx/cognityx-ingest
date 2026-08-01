"""Deterministic logical tables assembled from backend-neutral observations."""

from __future__ import annotations

from dataclasses import dataclass
import re

from cognityx_ingest.models import (
    Block,
    DocumentObject,
    PageRecord,
    Section,
    TableCell,
    TablePart,
    TableRow,
)
from cognityx_ingest.parser import ExtractedBlock, ExtractedPage


_TABLE_CAPTION = re.compile(r"^Table\s+(?P<number>\d+(?:-\d+)*)\.\s+\S")
_ROW_START = re.compile(r"^(?P<number>\d{1,4})$")
_RULE_CODE = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9]+)+$")


@dataclass(frozen=True, slots=True)
class ObservedTableRow:
    row_number: int
    cells: tuple[str, ...]
    source_block_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedTablePart:
    page_index: int
    part_number: int
    columns: tuple[str, ...]
    group_text: str
    rows: tuple[ObservedTableRow, ...]
    source_block_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class ObservedLogicalTable:
    number: str
    caption: str
    caption_source_block_id: str
    columns: tuple[str, ...]
    parts: tuple[ObservedTablePart, ...]
    source_backend: str


def detect_logical_tables(
    pages: tuple[ExtractedPage, ...], source_backend: str
) -> tuple[ObservedLogicalTable, ...]:
    """Detect caption-led, structurally continuous tables across adjacent pages."""
    tables: list[ObservedLogicalTable] = []
    for page_position, page in enumerate(pages):
        content = _content_blocks(page)
        for caption_position, caption_block in enumerate(content):
            match = _TABLE_CAPTION.match(caption_block.text.strip())
            if match is None or caption_position + 2 >= len(content):
                continue
            columns = _lines(content[caption_position + 2].text)
            if len(columns) < 2:
                continue
            number = match.group("number")
            group_text = content[caption_position + 1].text.strip()
            first = _part_from_blocks(
                page,
                part_number=1,
                columns=columns,
                group_text=group_text,
                blocks=content[caption_position + 1 :],
            )
            if first is None:
                continue
            parts = [first]
            expected_row = first.rows[-1].row_number + 1
            for next_page in pages[page_position + 1 :]:
                next_content = _content_blocks(next_page)
                if len(next_content) < 3:
                    break
                next_columns = _lines(next_content[1].text)
                if (
                    _normalized(next_content[0].text) != _normalized(group_text)
                    or next_columns != columns
                ):
                    break
                part = _part_from_blocks(
                    next_page,
                    part_number=len(parts) + 1,
                    columns=columns,
                    group_text=group_text,
                    blocks=next_content,
                )
                if part is None or part.rows[0].row_number != expected_row:
                    break
                parts.append(part)
                expected_row = part.rows[-1].row_number + 1
                if any(
                    _normalized(block.text).startswith(
                        _normalized(f"End of Table {number}")
                    )
                    for block in next_content
                ):
                    break
            if len(parts) == 1:
                continue
            rows = tuple(row.row_number for part in parts for row in part.rows)
            if rows != tuple(range(rows[0], rows[-1] + 1)):
                continue
            tables.append(
                ObservedLogicalTable(
                    number=number,
                    caption=_caption_text(caption_block.text),
                    caption_source_block_id=caption_block.block_id,
                    columns=columns,
                    parts=tuple(parts),
                    source_backend=source_backend,
                )
            )
    return tuple(tables)


def build_table_objects(
    document_id: str,
    observations: tuple[ObservedLogicalTable, ...],
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    sections: tuple[Section, ...],
) -> tuple[DocumentObject, ...]:
    """Map observed table parts onto stable canonical anchors."""
    page_by_index = {page.physical_page_index: page for page in pages}
    block_by_id = {block.block_id: block for block in blocks}
    result: list[DocumentObject] = []
    for table in observations:
        canonical_parts: list[TablePart] = []
        canonical_rows: list[TableRow] = []
        part_block_ids: list[str] = []
        for observed_part in table.parts:
            page = page_by_index[observed_part.page_index]
            part_label = f"Table {table.number} part {observed_part.part_number}"
            part_block = next(
                block
                for block_id in page.block_ids
                if (block := block_by_id[block_id]).block_type == "table_part"
                and block.text == part_label
            )
            part_block_ids.append(part_block.block_id)
            rows = tuple(
                _canonical_row(row, table.columns, part_block.block_id)
                for row in observed_part.rows
            )
            canonical_rows.extend(rows)
            group_row = TableRow(
                row_number=None,
                row_type="group",
                text=observed_part.group_text,
                column_span=len(table.columns),
                source_anchor_ids=(part_block.block_id,),
                parser_source_anchor_ids=(observed_part.source_block_ids[0],),
            )
            canonical_parts.append(
                TablePart(
                    part_id=f"{document_id}:table:{table.number}:part:{observed_part.part_number}",
                    page_id=page.page_id,
                    source_block_ids=(part_block.block_id,),
                    parser_source_anchor_ids=observed_part.source_block_ids,
                    row_start=rows[0].row_number or 0,
                    row_end=rows[-1].row_number or 0,
                    repeated_header=observed_part.part_number > 1,
                    merged_group_row=group_row,
                )
            )
        first_page = page_by_index[table.parts[0].page_index]
        caption_block = next(
            block
            for block_id in first_page.block_ids
            if (block := block_by_id[block_id]).block_type == "caption"
            and block.text == table.caption
        )
        owner = max(
            (section for section in sections if caption_block.block_id in section.block_ids),
            key=lambda section: section.level or 0,
        )
        result.append(
            DocumentObject(
                object_id=f"{document_id}:table:{table.number}",
                object_type="table",
                page_id=first_page.page_id,
                page_ids=tuple(part.page_id for part in canonical_parts),
                owner_section_id=owner.section_id,
                caption=table.caption,
                source_anchor_ids=(caption_block.block_id, *part_block_ids),
                caption_anchor_id=caption_block.block_id,
                columns=table.columns,
                rows=tuple(canonical_rows),
                parts=tuple(canonical_parts),
                source_backends=(table.source_backend,),
                bbox=table.parts[0].bbox,
                method="deterministic_table_assembly",
                confidence=1.0,
            )
        )
    return tuple(result)


def table_source_groups(
    observations: tuple[ObservedLogicalTable, ...],
) -> tuple[
    dict[str, tuple[ObservedLogicalTable, ObservedTablePart]],
    dict[str, ObservedLogicalTable],
]:
    parts: dict[str, tuple[ObservedLogicalTable, ObservedTablePart]] = {}
    captions: dict[str, ObservedLogicalTable] = {}
    for table in observations:
        captions[table.caption_source_block_id] = table
        for part in table.parts:
            for block_id in part.source_block_ids:
                parts[block_id] = (table, part)
    return parts, captions


def _part_from_blocks(
    page: ExtractedPage,
    *,
    part_number: int,
    columns: tuple[str, ...],
    group_text: str,
    blocks: tuple[ExtractedBlock, ...],
) -> ObservedTablePart | None:
    if len(blocks) < 3 or _lines(blocks[1].text) != columns:
        return None
    rows: list[ObservedTableRow] = []
    source_blocks = [blocks[0].block_id, blocks[1].block_id]
    position = 2
    while position < len(blocks):
        block = blocks[position]
        lines = _lines(block.text)
        if not lines or _ROW_START.fullmatch(lines[0]) is None:
            break
        row_sources = [block.block_id]
        code = lines[-1] if _RULE_CODE.fullmatch(lines[-1]) else None
        if code is not None:
            lines = lines[:-1]
        elif position + 1 < len(blocks):
            candidate = blocks[position + 1]
            if _RULE_CODE.fullmatch(candidate.text.strip()) and _overlaps_y(block, candidate):
                code = candidate.text.strip()
                row_sources.append(candidate.block_id)
                position += 1
        if code is None or len(lines) < 4:
            return None
        row_number = int(lines[0])
        rows.append(
            ObservedTableRow(
                row_number=row_number,
                cells=(lines[0], lines[1], lines[2], " ".join(lines[3:]), code),
                source_block_ids=tuple(row_sources),
            )
        )
        source_blocks.extend(row_sources)
        position += 1
    if not rows:
        return None
    return ObservedTablePart(
        page_index=page.physical_page_index,
        part_number=part_number,
        columns=columns,
        group_text=group_text,
        rows=tuple(rows),
        source_block_ids=tuple(source_blocks),
        bbox=_union_bbox(tuple(block for block in blocks if block.block_id in source_blocks)),
    )


def _canonical_row(
    row: ObservedTableRow, columns: tuple[str, ...], part_block_id: str
) -> TableRow:
    cells = tuple(
        TableCell(
            column_index=index,
            column_name=column,
            text=text,
            source_anchor_ids=(part_block_id,),
            parser_source_anchor_ids=row.source_block_ids,
        )
        for index, (column, text) in enumerate(zip(columns, row.cells, strict=True))
    )
    return TableRow(
        row_number=row.row_number,
        row_type="data",
        cells=cells,
        source_anchor_ids=(part_block_id,),
        parser_source_anchor_ids=row.source_block_ids,
    )


def _content_blocks(page: ExtractedPage) -> tuple[ExtractedBlock, ...]:
    return tuple(
        block
        for block in page.blocks
        if block.block_type not in {"page_header", "page_footer"}
    )


def _caption_text(text: str) -> str:
    return re.split(r"\s+[—-]\s+", text.strip(), maxsplit=1)[0]


def _lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _normalized(text: str) -> str:
    return " ".join(text.replace("—", "-").replace("–", "-").split()).casefold()


def _overlaps_y(first: ExtractedBlock, second: ExtractedBlock) -> bool:
    if first.bbox is None or second.bbox is None:
        return False
    return min(first.bbox[3], second.bbox[3]) >= max(first.bbox[1], second.bbox[1])


def _union_bbox(
    blocks: tuple[ExtractedBlock, ...],
) -> tuple[float, float, float, float] | None:
    boxes = tuple(block.bbox for block in blocks if block.bbox is not None)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
