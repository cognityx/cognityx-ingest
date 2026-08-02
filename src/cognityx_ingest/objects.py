"""Deterministic figures and footnotes from normalized parser observations."""

from __future__ import annotations

from dataclasses import dataclass
import re

from cognityx_ingest.models import Block, DocumentObject, PageRecord, Relation, Section
from cognityx_ingest.parser import ExtractedBlock, ExtractedObject, ExtractedPage


_FIGURE_CAPTION = re.compile(r"^Figure\s+(?P<number>\d+(?:-\d+)*)\.\s+(?P<title>\S.*)$")
_FOOTNOTE = re.compile(
    r"^Footnote\s+(?P<marker>\d+)\s*[—-]\s*(?P<text>\S.*)$",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ObservedFigure:
    number: str
    caption: str
    page_index: int
    image: ExtractedObject
    caption_block: ExtractedBlock
    source_backends: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedFootnote:
    marker: str
    text: str
    page_index: int
    note_block: ExtractedBlock
    source_backends: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObjectObservations:
    figures: tuple[ObservedFigure, ...] = ()
    footnotes: tuple[ObservedFootnote, ...] = ()


def detect_object_observations(
    pages: tuple[ExtractedPage, ...], source_backend: str
) -> ObjectObservations:
    figures: list[ObservedFigure] = []
    footnotes: list[ObservedFootnote] = []
    for page in pages:
        content = tuple(
            block
            for block in page.blocks
            if block.block_type not in {"page_header", "page_footer"}
        )
        captions = tuple(
            (block, match)
            for block in content
            if (match := _FIGURE_CAPTION.fullmatch(block.text.strip())) is not None
        )
        for image in page.objects:
            if image.object_type != "figure" or image.bbox is None:
                continue
            candidates = tuple(
                (block, match)
                for block, match in captions
                if block.bbox is not None and block.bbox[1] >= image.bbox[1]
            )
            if not candidates:
                continue
            caption_block, match = min(
                candidates,
                key=lambda item: abs((item[0].bbox or image.bbox)[1] - image.bbox[3]),
            )
            figures.append(
                ObservedFigure(
                    number=match.group("number"),
                    caption=caption_block.text.strip(),
                    page_index=page.physical_page_index,
                    image=image,
                    caption_block=caption_block,
                    source_backends=tuple(
                        sorted(
                            set(image.source_backends or (source_backend,))
                            | set(caption_block.source_backends or (source_backend,))
                        )
                    ),
                )
            )
        for block in content:
            match = _FOOTNOTE.fullmatch(block.text.strip())
            if match is None:
                continue
            footnotes.append(
                ObservedFootnote(
                    marker=match.group("marker"),
                    text=_first_sentence(match.group("text")),
                    page_index=page.physical_page_index,
                    note_block=block,
                    source_backends=(block.source_backends or (source_backend,)),
                )
            )
    return ObjectObservations(tuple(figures), tuple(footnotes))


def build_owned_objects(
    document_id: str,
    observations: ObjectObservations,
    pages: tuple[PageRecord, ...],
    blocks: tuple[Block, ...],
    sections: tuple[Section, ...],
) -> tuple[tuple[DocumentObject, ...], tuple[Relation, ...]]:
    page_by_index = {page.physical_page_index: page for page in pages}
    blocks_by_id = {block.block_id: block for block in blocks}
    objects: list[DocumentObject] = []
    relations: list[Relation] = []
    for observed in observations.figures:
        page = page_by_index[observed.page_index]
        page_blocks = tuple(blocks_by_id[item] for item in page.block_ids)
        image_block = next(
            item
            for item in page_blocks
            if item.block_type == "figure"
            and item.text == f"Figure {observed.number} image"
        )
        caption_block = next(
            item
            for item in page_blocks
            if item.block_type == "caption" and item.text == observed.caption
        )
        owner = _owner_section(caption_block.block_id, sections)
        object_id = f"{document_id}:figure:{observed.number}"
        objects.append(
            DocumentObject(
                object_id=object_id,
                object_type="figure",
                page_id=page.page_id,
                page_ids=(page.page_id,),
                owner_section_id=owner.section_id,
                caption=observed.caption,
                source_anchor_ids=(image_block.block_id, caption_block.block_id),
                parser_source_anchor_ids=(
                    observed.image.object_id,
                    observed.caption_block.block_id,
                ),
                caption_anchor_id=caption_block.block_id,
                image_anchor_id=image_block.block_id,
                bbox=observed.image.bbox,
                source_backends=observed.source_backends,
                fact_sources={
                    "structure": tuple(
                        {
                            "backend": backend,
                            "method": "deterministic_figure_ownership",
                            "confidence": 1.0,
                        }
                        for backend in observed.source_backends
                    )
                },
                method="deterministic_figure_ownership",
                confidence=1.0,
            )
        )
        relations.append(
            Relation(
                relation_id=f"{object_id}:relation:caption",
                source_anchor_id=caption_block.block_id,
                target_anchor_id=object_id,
                relation_type="caption_of",
                status="resolved",
                method="deterministic_layout_ownership",
                confidence=1.0,
                source_backends=observed.source_backends,
                fact_sources={
                    "source": tuple(
                        {
                            "backend": backend,
                            "method": "deterministic_layout_ownership",
                            "confidence": 1.0,
                        }
                        for backend in observed.source_backends
                    )
                },
            )
        )
    for observed in observations.footnotes:
        page = page_by_index[observed.page_index]
        content = tuple(
            blocks_by_id[item]
            for item in page.block_ids
            if blocks_by_id[item].block_type not in {"page_header", "page_footer"}
        )
        note_position, note_block = next(
            (index, item)
            for index, item in enumerate(content)
            if item.block_type == "footnote"
            and item.text.startswith(f"Footnote {observed.marker}")
        )
        marker_block = next(
            item
            for item in reversed(content[:note_position])
            if item.block_type not in {"heading", "caption", "figure", "table"}
        )
        owner = _owner_section(note_block.block_id, sections)
        object_id = f"{document_id}:footnote:{observed.marker}"
        objects.append(
            DocumentObject(
                object_id=object_id,
                object_type="footnote",
                page_id=page.page_id,
                page_ids=(page.page_id,),
                owner_section_id=owner.section_id,
                text=observed.text,
                marker=observed.marker,
                source_anchor_ids=(marker_block.block_id, note_block.block_id),
                parser_source_anchor_ids=(observed.note_block.block_id,),
                marker_anchor_id=marker_block.block_id,
                note_anchor_id=note_block.block_id,
                bbox=note_block.bbox,
                source_backends=observed.source_backends,
                fact_sources={
                    "structure": tuple(
                        {
                            "backend": backend,
                            "method": "deterministic_footnote_ownership",
                            "confidence": 1.0,
                        }
                        for backend in observed.source_backends
                    )
                },
                method="deterministic_footnote_ownership",
                confidence=1.0,
            )
        )
        relations.append(
            Relation(
                relation_id=f"{object_id}:relation:marker",
                source_anchor_id=marker_block.block_id,
                target_anchor_id=object_id,
                relation_type="footnote_marker",
                status="resolved",
                method="deterministic_marker_adjacency",
                confidence=1.0,
                source_backends=observed.source_backends,
                fact_sources={
                    "source": tuple(
                        {
                            "backend": backend,
                            "method": "deterministic_marker_adjacency",
                            "confidence": 1.0,
                        }
                        for backend in observed.source_backends
                    )
                },
            )
        )
    return tuple(objects), tuple(relations)


def _owner_section(anchor_id: str, sections: tuple[Section, ...]) -> Section:
    return max(
        (section for section in sections if anchor_id in section.block_ids),
        key=lambda section: section.level or 0,
    )


def _first_sentence(text: str) -> str:
    match = re.match(r".*?[.!?](?=\s|$)", " ".join(text.split()))
    return match.group(0) if match is not None else " ".join(text.split())
