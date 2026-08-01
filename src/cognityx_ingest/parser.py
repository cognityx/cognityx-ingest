"""Backend-neutral PDF extraction plugins and selection policies."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from pypdf import PdfReader


class UnsupportedInputError(ValueError):
    """Raised when a requested source is not an ingestible PDF."""


class ParserUnavailableError(RuntimeError):
    """Raised when an optional parser plugin is not installed."""


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    block_id: str
    text: str
    reading_order: int
    block_type: str = "text"
    bbox: tuple[float, float, float, float] | None = None
    method: str = "parser"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ExtractedObject:
    object_id: str
    object_type: str
    page_index: int
    caption: str | None = None
    text: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    method: str = "parser"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ExtractedSection:
    section_id: str
    title: str
    start_page_index: int
    end_page_index: int
    method: str = "parser"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ExtractedRelation:
    relation_id: str
    source_anchor: str
    target_anchor: str | None
    relation_type: str
    target_text: str | None = None
    status: str = "unresolved"
    method: str = "parser"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """One physical PDF page with optional native structure."""

    page_number: int
    text: str
    page_index: int | None = None
    page_label: str | None = None
    printed_page_label: str | None = None
    width: float | None = None
    height: float | None = None
    blocks: tuple[ExtractedBlock, ...] = ()
    objects: tuple[ExtractedObject, ...] = ()
    relations: tuple[ExtractedRelation, ...] = ()

    @property
    def physical_page_index(self) -> int:
        return self.page_index if self.page_index is not None else self.page_number - 1


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Normalized parser output without backend-specific public classes."""

    pages: tuple[ExtractedPage, ...]
    backend: str
    backend_version: str | None = None
    sections: tuple[ExtractedSection, ...] = ()
    raw_artifact: bytes | None = None
    considered_backends: tuple[str, ...] = ()
    selected_reason: str = "configured"
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class PdfExtractor(Protocol):
    def extract(self, path: Path) -> tuple[ExtractedPage, ...]: ...


class ParserPlugin(Protocol):
    name: str

    def extract_document(self, path: Path) -> ExtractionResult: ...


class ParserSelector(Protocol):
    def select(
        self, path: Path, candidates: tuple[str, ...], facts: Mapping[str, Any]
    ) -> tuple[str, str]: ...


class PyPdfExtractor:
    """Baseline text extraction using pypdf."""

    name = "basic"

    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        return self.extract_document(path).pages

    def extract_document(self, path: Path) -> ExtractionResult:
        _require_pdf(path)
        try:
            reader = PdfReader(path)
            pages = tuple(
                ExtractedPage(
                    page_number=index,
                    page_index=index - 1,
                    page_label=_pypdf_page_label(reader, index - 1),
                    text=(page.extract_text() or "").strip(),
                )
                for index, page in enumerate(reader.pages, start=1)
            )
            return ExtractionResult(pages=pages, backend=self.name)
        except Exception as error:
            raise UnsupportedInputError(f"Could not parse PDF {path}: {error}") from error


class PyMuPDFParser:
    """PDF-native labels, outlines, links, annotations, blocks, and objects."""

    name = "pymupdf"

    def extract_document(self, path: Path) -> ExtractionResult:
        _require_pdf(path)
        try:
            import fitz
        except ImportError as exc:
            raise ParserUnavailableError(
                "PyMuPDF parsing requires the cognityx-ingest[pymupdf] extra."
            ) from exc
        try:
            document = fitz.open(path)
            native_labels = PdfReader(path).page_labels
            pages: list[ExtractedPage] = []
            outline = document.get_toc(simple=False)
            raw: dict[str, Any] = {"outline": outline, "pages": []}
            for index, page in enumerate(document):
                label = page.get_label() or native_labels[index] or None
                blocks = tuple(
                    ExtractedBlock(
                        block_id=f"page:{index}:block:{order}",
                        text=str(item[4]).strip(),
                        reading_order=order,
                        bbox=tuple(float(value) for value in item[:4]),
                    )
                    for order, item in enumerate(page.get_text("blocks"), start=1)
                    if str(item[4]).strip()
                )
                relations = tuple(
                    ExtractedRelation(
                        relation_id=f"page:{index}:link:{order}",
                        source_anchor=f"page:{index}",
                        target_anchor=(
                            f"page:{int(link['page'])}"
                            if isinstance(link.get("page"), int) and link["page"] >= 0
                            else None
                        ),
                        target_text=link.get("uri"),
                        relation_type="link",
                        status=("resolved" if link.get("page", -1) >= 0 else "unresolved"),
                        method="native",
                        confidence=1.0,
                    )
                    for order, link in enumerate(page.get_links(), start=1)
                )
                images = tuple(
                    ExtractedObject(
                        object_id=f"page:{index}:figure:{order}",
                        object_type="figure",
                        page_index=index,
                        method="native",
                        confidence=1.0,
                    )
                    for order, _ in enumerate(page.get_images(full=True), start=1)
                )
                annotations = tuple(
                    ExtractedObject(
                        object_id=f"page:{index}:annotation:{order}",
                        object_type="annotation",
                        page_index=index,
                        text=str(annotation.info.get("content") or "") or None,
                        bbox=tuple(float(value) for value in annotation.rect),
                        method="native",
                        confidence=1.0,
                    )
                    for order, annotation in enumerate(page.annots() or (), start=1)
                )
                pages.append(
                    ExtractedPage(
                        page_number=index + 1,
                        page_index=index,
                        page_label=label,
                        text=page.get_text("text").strip(),
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        blocks=blocks,
                        objects=(*images, *annotations),
                        relations=relations,
                    )
                )
                raw["pages"].append(
                    {"index": index, "label": label, "links": page.get_links()}
                )
            return ExtractionResult(
                pages=_classify_repeated_page_regions(tuple(pages)),
                backend=self.name,
                backend_version=getattr(fitz, "VersionBind", None),
                sections=_outline_sections(outline, len(pages)),
                raw_artifact=json.dumps(raw, sort_keys=True, default=str).encode(),
            )
        except ParserUnavailableError:
            raise
        except Exception as error:
            raise UnsupportedInputError(f"Could not parse PDF {path}: {error}") from error


_PAGE_LABEL_SUFFIX = re.compile(
    r"(?i)(?:front\s+matter|printed\s+page|page)\s+"
    r"(?P<label>(?:[ivxlcdm]+|\d+|[a-z]+-\d+))\s*$"
)


def normalize_repeated_region_text(text: str) -> str:
    """Normalize variable visible page labels when comparing margin text."""
    collapsed = " ".join(text.split())
    return _PAGE_LABEL_SUFFIX.sub("<page-label>", collapsed)


def _visible_page_label(text: str) -> str | None:
    match = _PAGE_LABEL_SUFFIX.search(" ".join(text.split()))
    return match.group("label") if match else None


def _classify_repeated_page_regions(
    pages: tuple[ExtractedPage, ...],
) -> tuple[ExtractedPage, ...]:
    """Classify recurring positioned margin blocks without document-specific text."""
    candidates: list[tuple[int, str, ExtractedBlock, str]] = []
    for page_index, page in enumerate(pages):
        if not page.height:
            continue
        for block in page.blocks:
            if block.bbox is None:
                continue
            if block.bbox[1] <= page.height * 0.12:
                region_type = "page_header"
            elif block.bbox[3] >= page.height * 0.88:
                region_type = "page_footer"
            else:
                continue
            candidates.append(
                (
                    page_index,
                    region_type,
                    block,
                    normalize_repeated_region_text(block.text),
                )
            )

    counts = Counter((region_type, normalized) for _, region_type, _, normalized in candidates)
    minimum_occurrences = max(2, math.ceil(len(pages) * 0.25))
    repeated = {
        key for key, count in counts.items() if count >= minimum_occurrences
    }
    classified: dict[int, dict[str, str]] = {}
    for page_index, region_type, block, normalized in candidates:
        if (region_type, normalized) in repeated:
            classified.setdefault(page_index, {})[block.block_id] = region_type

    result: list[ExtractedPage] = []
    for page_index, page in enumerate(pages):
        page_regions = classified.get(page_index, {})
        blocks = tuple(
            replace(
                block,
                block_type=page_regions.get(block.block_id, block.block_type),
                method=(
                    "deterministic_repeated_margin"
                    if block.block_id in page_regions
                    else block.method
                ),
                confidence=(1.0 if block.block_id in page_regions else block.confidence),
            )
            for block in page.blocks
        )
        content_text = "\n".join(
            block.text for block in blocks if block.block_type not in {"page_header", "page_footer"}
        ).strip()
        footer_labels = [
            _visible_page_label(block.text)
            for block in blocks
            if block.block_type == "page_footer"
        ]
        result.append(
            replace(
                page,
                text=content_text or page.text,
                printed_page_label=next((label for label in footer_labels if label), None),
                blocks=blocks,
            )
        )
    return tuple(result)


class DoclingParser:
    """Rich document-structure adapter for the optional Docling backend."""

    name = "docling"

    def extract_document(self, path: Path) -> ExtractionResult:
        _require_pdf(path)
        try:
            from docling.document_converter import DocumentConverter
            from importlib.metadata import version
        except ImportError as exc:
            raise ParserUnavailableError(
                "Docling parsing requires the cognityx-ingest[docling] extra."
            ) from exc
        try:
            converted = DocumentConverter().convert(path)
            document = converted.document
            markdown = document.export_to_markdown()
            payload = document.export_to_dict()
            pages, sections = _normalize_docling(payload, markdown)
            return ExtractionResult(
                pages=pages,
                backend=self.name,
                backend_version=version("docling"),
                sections=sections,
                raw_artifact=json.dumps(payload, sort_keys=True, default=str).encode(),
            )
        except Exception as error:
            raise UnsupportedInputError(f"Could not parse PDF {path}: {error}") from error


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    mode: str = "fixed"
    backends: tuple[str, ...] = ("basic",)

    def __post_init__(self) -> None:
        if self.mode not in {"fixed", "rule", "fallback", "compare", "agent"}:
            raise ValueError(f"Unknown extraction policy mode: {self.mode}")
        if not self.backends:
            raise ValueError("At least one parser backend is required.")


class ParserRouter:
    """Apply one bounded selection policy while keeping output fixed."""

    def __init__(
        self,
        plugins: Sequence[ParserPlugin] | None = None,
        *,
        policy: ExtractionPolicy | None = None,
        selector: ParserSelector | None = None,
    ) -> None:
        available = plugins or (PyPdfExtractor(), PyMuPDFParser(), DoclingParser())
        self._plugins = {item.name: item for item in available}
        self.policy = policy or ExtractionPolicy()
        self.selector = selector

    def extract_document(self, path: Path) -> ExtractionResult:
        candidates = tuple(self.policy.backends)
        if self.policy.mode == "rule":
            selected = "pymupdf" if "pymupdf" in candidates else candidates[0]
            return self._run(selected, path, candidates, "rule_based_selection")
        if self.policy.mode == "agent":
            if self.selector is None:
                raise ValueError("Agent selection requires a bounded parser selector.")
            selected, reason = self.selector.select(
                path,
                candidates,
                {"suffix": path.suffix.lower(), "size_bytes": path.stat().st_size},
            )
            if selected not in candidates:
                raise ValueError("Parser selector returned a backend outside its allowlist.")
            return self._run(selected, path, candidates, reason)
        if self.policy.mode == "compare":
            results = self._available_results(path, candidates)
            if not results:
                raise ParserUnavailableError("No configured parser backend was available.")
            selected = max(results, key=_richness_score)
            return _with_selection(selected, candidates, "highest_normalized_structure_score")
        if self.policy.mode == "fallback":
            errors: list[str] = []
            for name in candidates:
                try:
                    return self._run(name, path, candidates, "first_successful_backend")
                except (ParserUnavailableError, UnsupportedInputError) as exc:
                    errors.append(f"{name}: {exc}")
            raise UnsupportedInputError("All parser backends failed: " + "; ".join(errors))
        return self._run(candidates[0], path, candidates, "fixed_backend")

    def extract(self, path: Path) -> tuple[ExtractedPage, ...]:
        return self.extract_document(path).pages

    def _run(
        self, name: str, path: Path, candidates: tuple[str, ...], reason: str
    ) -> ExtractionResult:
        try:
            result = self._plugins[name].extract_document(path)
        except KeyError as exc:
            raise ValueError(f"Unknown parser backend: {name}") from exc
        return _with_selection(result, candidates, reason)

    def _available_results(
        self, path: Path, candidates: tuple[str, ...]
    ) -> list[ExtractionResult]:
        results: list[ExtractionResult] = []
        for name in candidates:
            try:
                results.append(self._plugins[name].extract_document(path))
            except (ParserUnavailableError, UnsupportedInputError):
                continue
        return results


BasicPdfParser = PyPdfExtractor


def normalize_extraction(extractor: Any, path: Path) -> ExtractionResult:
    """Adapt legacy extractors to the normalized plugin contract."""
    method = getattr(extractor, "extract_document", None)
    if method is not None:
        return method(path)
    pages = tuple(extractor.extract(path))
    return ExtractionResult(
        pages=pages,
        backend=type(extractor).__name__,
        selected_reason="legacy_extractor",
    )


def _with_selection(
    result: ExtractionResult, candidates: tuple[str, ...], reason: str
) -> ExtractionResult:
    return ExtractionResult(
        pages=result.pages,
        backend=result.backend,
        backend_version=result.backend_version,
        sections=result.sections,
        raw_artifact=result.raw_artifact,
        considered_backends=candidates,
        selected_reason=reason,
        diagnostics=result.diagnostics,
    )


def _richness_score(result: ExtractionResult) -> tuple[int, int, int]:
    return (
        sum(len(page.blocks) + len(page.objects) + len(page.relations) for page in result.pages),
        sum(bool(page.page_label) for page in result.pages),
        sum(len(page.text) for page in result.pages),
    )


def _require_pdf(path: Path) -> None:
    if path.suffix.lower() != ".pdf":
        raise UnsupportedInputError(f"Only PDF input is supported: {path}")


def _pypdf_page_label(reader: PdfReader, index: int) -> str | None:
    try:
        labels = reader.page_labels
        return str(labels[index]) if index < len(labels) else None
    except Exception:
        return None


def _docling_page_text(markdown: str, value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("text", "content", "markdown"):
            if value.get(key):
                return str(value[key])
    return markdown


def _outline_sections(
    outline: Sequence[Any], page_count: int
) -> tuple[ExtractedSection, ...]:
    headings: list[tuple[str, int]] = []
    for item in outline:
        if not isinstance(item, Sequence) or len(item) < 3:
            continue
        try:
            page_index = max(0, int(item[2]) - 1)
        except (TypeError, ValueError):
            continue
        headings.append((str(item[1]), min(page_index, max(0, page_count - 1))))
    return tuple(
        ExtractedSection(
            section_id=f"outline:{index}",
            title=title,
            start_page_index=start,
            end_page_index=(
                headings[index][1] - 1 if index < len(headings) else page_count - 1
            ),
            method="native_outline",
            confidence=1.0,
        )
        for index, (title, start) in enumerate(headings, start=1)
    )


def _normalize_docling(
    payload: Any, markdown: str
) -> tuple[tuple[ExtractedPage, ...], tuple[ExtractedSection, ...]]:
    if not isinstance(payload, Mapping):
        return (ExtractedPage(1, markdown),), ()
    page_values = payload.get("pages")
    page_count = len(page_values) if isinstance(page_values, Mapping) else 1
    text_by_page: dict[int, list[str]] = {index: [] for index in range(page_count)}
    blocks_by_page: dict[int, list[ExtractedBlock]] = {
        index: [] for index in range(page_count)
    }
    objects_by_page: dict[int, list[ExtractedObject]] = {
        index: [] for index in range(page_count)
    }
    headings: list[tuple[str, int]] = []
    for collection, object_type in (
        (payload.get("texts", ()), "text"),
        (payload.get("tables", ()), "table"),
        (payload.get("pictures", ()), "figure"),
    ):
        if not isinstance(collection, Sequence):
            continue
        for item_index, item in enumerate(collection, start=1):
            if not isinstance(item, Mapping):
                continue
            page_index, bbox = _docling_provenance(item, page_count)
            text = str(item.get("text") or item.get("caption") or "").strip()
            label = str(item.get("label") or object_type)
            if object_type == "text":
                if text:
                    text_by_page[page_index].append(text)
                    blocks_by_page[page_index].append(
                        ExtractedBlock(
                            block_id=f"docling:{page_index}:block:{item_index}",
                            text=text,
                            reading_order=len(blocks_by_page[page_index]) + 1,
                            block_type=label,
                            bbox=bbox,
                        )
                    )
                if label in {"title", "section_header", "heading"} and text:
                    headings.append((text, page_index))
            else:
                objects_by_page[page_index].append(
                    ExtractedObject(
                        object_id=f"docling:{page_index}:{object_type}:{item_index}",
                        object_type=object_type,
                        page_index=page_index,
                        caption=text or None,
                        bbox=bbox,
                    )
                )
    pages = tuple(
        ExtractedPage(
            page_number=index + 1,
            page_index=index,
            text="\n".join(text_by_page[index]).strip() or markdown,
            blocks=tuple(blocks_by_page[index]),
            objects=tuple(objects_by_page[index]),
        )
        for index in range(page_count)
    )
    sections = tuple(
        ExtractedSection(
            section_id=f"docling:section:{index}",
            title=title,
            start_page_index=start,
            end_page_index=(
                headings[index][1] - 1 if index < len(headings) else page_count - 1
            ),
            method="docling_heading",
        )
        for index, (title, start) in enumerate(headings, start=1)
    )
    return pages, sections


def _docling_provenance(
    item: Mapping[str, Any], page_count: int
) -> tuple[int, tuple[float, float, float, float] | None]:
    provenance = item.get("prov") or item.get("provenance") or ()
    first = provenance[0] if isinstance(provenance, Sequence) and provenance else {}
    if not isinstance(first, Mapping):
        return 0, None
    try:
        page_index = max(0, int(first.get("page_no", 1)) - 1)
    except (TypeError, ValueError):
        page_index = 0
    page_index = min(page_index, max(0, page_count - 1))
    bbox_value = first.get("bbox")
    if isinstance(bbox_value, Mapping):
        values = [bbox_value.get(key) for key in ("l", "t", "r", "b")]
    elif isinstance(bbox_value, Sequence):
        values = list(bbox_value[:4])
    else:
        values = []
    try:
        bbox = tuple(float(value) for value in values) if len(values) == 4 else None
    except (TypeError, ValueError):
        bbox = None
    return page_index, bbox
