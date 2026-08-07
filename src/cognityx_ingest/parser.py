"""Execute backend-neutral PDF parsers and preserve compatibility result shapes.

This module owns parser adapters, existing selection policies, and the stable
``ExtractionResult`` consumed by ingest callers. T05 alignment, fusion, and
adjudication live in ``parser_fusion`` rather than expanding this execution
module. Compare mode delegates completed parser results to that service, then
receives a compatibility projection plus additive observation and decision
artifacts. Compatibility fact sources retain exact parser-local page, block,
object, and relation identities before T05 enriches them with observation and
decision IDs. Repeated source values are therefore traced to their actual parser
occurrence instead of an arbitrary value-hash match. Canonical builders, audit
tools, and later T06 and T08 work consume those references; this module still
does not adjudicate evidence or create future segmentation and graph APIs. The
separation keeps routing and parser execution distinct from evidence decisions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
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
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )


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
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )


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
    bbox: tuple[float, float, float, float] | None = None
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )


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
    source_backends: tuple[str, ...] = ()
    fact_sources: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict, hash=False
    )

    @property
    def physical_page_index(self) -> int:
        return self.page_index if self.page_index is not None else self.page_number - 1


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Carry normalized parser output without backend-private public classes.

    Parser adapters construct this record and ``ParserRouter`` returns it to
    ``IngestService`` and direct Python callers. Its fields preserve the existing
    normalized page model, native raw artifacts, selection diagnostics, and
    considered backends. T05 adds exact observation and fusion artifacts as
    optional bytes so every old constructor remains valid. The observation bytes
    preserve parser evidence; the fusion bytes preserve decisions that reference
    that evidence. The record is frozen, retains no open parser resources,
    performs no work itself, and is safe for concurrent reads when nested
    caller-supplied mappings are treated as immutable.
    """

    pages: tuple[ExtractedPage, ...]
    backend: str
    backend_version: str | None = None
    sections: tuple[ExtractedSection, ...] = ()
    raw_artifact: bytes | None = None
    raw_artifacts: Mapping[str, bytes] = field(default_factory=dict)
    considered_backends: tuple[str, ...] = ()
    selected_reason: str = "configured"
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    observation_artifact: bytes | None = None
    fusion_artifact: bytes | None = None


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
                        bbox=(
                            tuple(float(value) for value in link["from"])
                            if link.get("from") is not None
                            else None
                        ),
                    )
                    for order, link in enumerate(page.get_links(), start=1)
                )
                images = tuple(
                    ExtractedObject(
                        object_id=f"page:{index}:figure:{order}",
                        object_type="figure",
                        page_index=index,
                        bbox=_image_bbox(page, image),
                        method="native",
                        confidence=1.0,
                    )
                    for order, image in enumerate(page.get_images(full=True), start=1)
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
                text=content_text if blocks else page.text,
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
    """Apply one bounded selection policy while keeping output fixed.

    The ingest composition root constructs this router from parser plugins and an
    ``ExtractionPolicy``. Existing callers use it to execute fixed, rule,
    fallback, compare, or agent policies. T03 capability discovery may inspect a
    deterministic immutable snapshot of registered plugins, but it cannot mutate
    the registry or select and execute a parser through that inspection seam.
    """

    def __init__(
        self,
        plugins: Sequence[ParserPlugin] | None = None,
        *,
        policy: ExtractionPolicy | None = None,
        selector: ParserSelector | None = None,
    ) -> None:
        """Snapshot adapters for introspection while preserving routing behavior.

        The composition root supplies lightweight parser adapters and an optional
        execution policy. The immutable snapshot retains every supplied adapter so
        capability discovery can validate identities before dictionary indexing
        hides duplicates; the existing mapping remains the execution seam used by
        normal ingest callers. Construction does not execute a parser.
        """
        available = tuple(
            plugins or (PyPdfExtractor(), PyMuPDFParser(), DoclingParser())
        )
        self._registered_plugin_snapshot = available
        self._plugins = {item.name: item for item in available}
        self.policy = policy or ExtractionPolicy()
        self.selector = selector

    def registered_plugins(self) -> tuple[ParserPlugin, ...]:
        """Return registered adapters in parser-ID order without executing them.

        Capability-registry construction calls this read-only method before any
        document is parsed. It snapshots the private plugin mapping as a tuple,
        preserving plugin objects for bounded class/package inspection while
        exposing neither the mutable dictionary nor a mutation API. Repeated
        calls are side-effect free and deterministically ordered.
        """
        snapshot = self._registered_plugin_snapshot
        names = tuple(getattr(plugin, "name", None) for plugin in snapshot)
        if all(isinstance(name, str) for name in names):
            return tuple(
                plugin
                for _, plugin in sorted(
                    zip(names, snapshot, strict=True), key=lambda item: item[0]
                )
            )
        return snapshot

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
            return _fuse_results(results, candidates)
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
    """Copy selection metadata while preserving every parser and T05 artifact.

    Fixed, rule, fallback, and agent execution paths call this pure helper after
    a parser returns. It changes only the considered candidates and reason,
    performs no parser or external call, and retains both optional observation
    and fusion bytes so composition wrappers cannot accidentally break their
    integrity binding.
    """
    return ExtractionResult(
        pages=result.pages,
        backend=result.backend,
        backend_version=result.backend_version,
        sections=result.sections,
        raw_artifact=result.raw_artifact,
        raw_artifacts=result.raw_artifacts,
        considered_backends=candidates,
        selected_reason=reason,
        diagnostics=result.diagnostics,
        observation_artifact=result.observation_artifact,
        fusion_artifact=result.fusion_artifact,
    )


def _fuse_results(
    results: Sequence[ExtractionResult], candidates: tuple[str, ...]
) -> ExtractionResult:
    """Delegate completed compare results to the explicit T05 production service.

    ``ParserRouter(mode="compare")`` is the only production caller. The local
    import avoids a module cycle because ``parser_fusion`` consumes immutable
    extraction records from this module. T05 performs no parser execution,
    provider, network, or LLM call; it returns a deterministic compatibility
    result carrying the authoritative v3.2 artifact. Typed T05 failures propagate
    unchanged and no persistence occurs here.
    """
    from cognityx_ingest.parser_fusion import ParserFusionService

    return ParserFusionService().fuse_extraction_results(
        results, candidates
    ).extraction_result


def _legacy_compatibility_projection(
    results: Sequence[ExtractionResult], candidates: tuple[str, ...]
) -> ExtractionResult:
    """Project T05 inputs into the established one-value extraction field shape.

    ``ParserFusionService`` calls this only after it has created explicit
    observation and adjudication records. The historical deterministic logic is
    retained so existing compare-mode consumers do not change shape or selected
    values. Its choices are compatibility projections, not evidence acceptance;
    the T05 artifact and diagnostics remain authoritative. The pure algorithm is
    order-independent and performs no parser, network, provider, or LLM call.
    """
    ordered = tuple(sorted(results, key=lambda item: item.backend))
    pages_by_index: dict[int, list[tuple[str, ExtractedPage]]] = {}
    for result in ordered:
        for page in result.pages:
            pages_by_index.setdefault(page.physical_page_index, []).append(
                (result.backend, page)
            )

    conflicts: list[dict[str, Any]] = []
    pages = tuple(
        _fuse_page(page_index, tuple(values), conflicts)
        for page_index, values in sorted(pages_by_index.items())
    )
    raw_artifacts = {
        result.backend: result.raw_artifact
        for result in ordered
        if result.raw_artifact is not None
    }
    source_backends = tuple(result.backend for result in ordered)
    versions = {
        result.backend: result.backend_version
        for result in ordered
        if result.backend_version is not None
    }
    diagnostics = {
        "fusion": "canonical_multi_source",
        "source_backends": list(source_backends),
        "backend_versions": versions,
        "conflicts": conflicts,
    }
    return ExtractionResult(
        pages=pages,
        backend="fusion",
        sections=_fuse_sections(ordered),
        raw_artifact=json.dumps(
            {
                "schema": "cognityx.ingest.parser-fusion/v1",
                "source_backends": source_backends,
                "backend_versions": versions,
                "conflicts": conflicts,
            },
            sort_keys=True,
        ).encode(),
        raw_artifacts=raw_artifacts,
        considered_backends=tuple(sorted(candidates)),
        selected_reason="canonical_fact_level_fusion",
        diagnostics=diagnostics,
    )


def _fuse_page(
    page_index: int,
    observed: tuple[tuple[str, ExtractedPage], ...],
    conflicts: list[dict[str, Any]],
) -> ExtractedPage:
    """Project one physical page while retaining exact parser source identity.

    The legacy compare projection calls this after parser execution. It applies
    existing value selection unchanged, composes deterministic blocks, objects,
    and relations, and adds page-region identity to each selected fact source.
    T05 enrichment and canonical audit consumers use those additive locators;
    no persistence, parser execution, or source-text duplication occurs here.
    """
    source_backends = tuple(sorted(backend for backend, _page in observed))
    selected: dict[str, Any] = {}
    fact_sources: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for field_name in (
        "text",
        "page_label",
        "printed_page_label",
        "width",
        "height",
    ):
        value, sources = _select_page_fact(
            page_index, field_name, observed, conflicts
        )
        selected[field_name] = value
        if sources:
            fact_sources[field_name] = tuple(
                {
                    "backend": backend,
                    "method": "parser_page_fact",
                    "confidence": 1.0,
                    "source_region_id": f"page:{page_index}",
                    "occurrence_index": 1,
                }
                for backend in sources
            )
    return ExtractedPage(
        page_number=page_index + 1,
        page_index=page_index,
        text=selected["text"] or "",
        page_label=selected["page_label"],
        printed_page_label=selected["printed_page_label"],
        width=selected["width"],
        height=selected["height"],
        blocks=_fuse_blocks(page_index, observed, conflicts),
        objects=_fuse_objects(page_index, observed, conflicts),
        relations=_fuse_relations(page_index, observed, conflicts),
        source_backends=source_backends,
        fact_sources=fact_sources,
    )


def _select_page_fact(
    page_index: int,
    field_name: str,
    observed: tuple[tuple[str, ExtractedPage], ...],
    conflicts: list[dict[str, Any]],
) -> tuple[Any, tuple[str, ...]]:
    values = [
        (backend, getattr(page, field_name))
        for backend, page in observed
        if getattr(page, field_name) not in {None, ""}
    ]
    if not values:
        return None, ()
    chosen_backend, chosen = min(
        values,
        key=lambda item: (_backend_rank(item[0], field_name), str(item[1])),
    )
    distinct = {str(value) for _backend, value in values}
    if len(distinct) > 1:
        conflicts.append(
            {
                "page_index": page_index,
                "fact": field_name,
                "observations": [
                    {"backend": backend, "value": value}
                    for backend, value in sorted(values, key=lambda item: item[0])
                ],
                "selected_backend": chosen_backend,
                "resolution": "deterministic_backend_precedence",
            }
        )
    sources = tuple(sorted(backend for backend, value in values if value == chosen))
    return chosen, sources


def _fuse_blocks(
    page_index: int,
    observed: tuple[tuple[str, ExtractedPage], ...],
    conflicts: list[dict[str, Any]],
) -> tuple[ExtractedBlock, ...]:
    """Project legacy blocks with deterministic duplicate-occurrence provenance.

    Compare mode calls this for one page. Blocks are normalized into stable
    parser-local order before duplicate text occurrences are grouped, then the
    established text, type, geometry, and reading-order choices are preserved.
    Each fact source carries its original block anchor and T05 region ID so
    enrichment can bind exactly. The pure result serves existing callers and
    later canonical audit references without creating T06 segmentation views.
    """
    grouped: dict[tuple[str, int], list[tuple[str, ExtractedBlock]]] = {}
    occurrences: Counter[tuple[str, str]] = Counter()
    for backend, page in observed:
        for block in sorted(
            page.blocks, key=lambda item: (item.block_id, item.reading_order)
        ):
            normalized_text = _normalized_fact_text(block.text)
            occurrences[(backend, normalized_text)] += 1
            grouped.setdefault(
                (normalized_text, occurrences[(backend, normalized_text)]), []
            ).append(
                (backend, block)
            )
    fused: list[ExtractedBlock] = []
    for (normalized_text, occurrence), values in sorted(grouped.items()):
        _text_backend, text = min(
            ((backend, block.text) for backend, block in values),
            key=lambda item: (_backend_rank(item[0], "text"), item[1]),
        )
        type_backend, block_type = min(
            ((backend, block.block_type) for backend, block in values),
            key=lambda item: (_block_type_rank(item[1]), item[0], item[1]),
        )
        _bbox_backend, bbox = min(
            (
                (backend, block.bbox)
                for backend, block in values
                if block.bbox is not None
            ),
            key=lambda item: (_backend_rank(item[0], "bbox"), item[1]),
            default=("", None),
        )
        source_backends = tuple(sorted({backend for backend, _block in values}))
        if len({block.block_type for _backend, block in values}) > 1:
            conflicts.append(
                {
                    "page_index": page_index,
                    "fact": "block_type",
                    "anchor_key": f"{normalized_text}:{occurrence}",
                    "observations": [
                        {"backend": backend, "value": block.block_type}
                        for backend, block in sorted(values, key=lambda item: item[0])
                    ],
                    "selected_backend": type_backend,
                    "resolution": "deterministic_semantic_type_precedence",
                }
            )
        confidence_values = [
            block.confidence
            for _backend, block in values
            if block.confidence is not None
        ]
        fused.append(
            ExtractedBlock(
                block_id=(
                    f"fusion:{page_index}:block:"
                    f"{hashlib.sha256(f'{normalized_text}:{occurrence}'.encode()).hexdigest()[:16]}"
                ),
                text=text,
                reading_order=min(block.reading_order for _backend, block in values),
                block_type=block_type,
                bbox=bbox,
                method=(
                    "canonical_parser_fusion"
                    if len(source_backends) > 1
                    else values[0][1].method
                ),
                confidence=max(confidence_values) if confidence_values else None,
                source_backends=source_backends,
                fact_sources={
                    "text": _block_fact_sources(
                        values, "text", text, page_index, occurrence
                    ),
                    "block_type": _block_fact_sources(
                        values, "block_type", block_type, page_index, occurrence
                    ),
                    "bbox": _block_fact_sources(
                        values, "bbox", bbox, page_index, occurrence
                    ),
                },
            )
        )
    ordered = sorted(
        fused,
        key=lambda block: (
            block.bbox[1] if block.bbox is not None else float("inf"),
            block.bbox[0] if block.bbox is not None else float("inf"),
            block.reading_order,
            block.block_id,
        ),
    )
    return tuple(replace(block, reading_order=index) for index, block in enumerate(ordered, 1))


def _fuse_objects(
    page_index: int,
    observed: tuple[tuple[str, ExtractedPage], ...],
    conflicts: list[dict[str, Any]],
) -> tuple[ExtractedObject, ...]:
    """Project legacy objects and retain each parser-local object occurrence.

    The compatibility page builder calls this after parser execution. It sorts
    parser objects by stable IDs, applies the existing object and geometry
    choices, and records bounded region, anchor, and occurrence metadata for
    every source. T05 and audit consumers use those identities; object text and
    captions are not copied into provenance metadata and no T08 graph is built.
    """
    grouped: dict[tuple[str, str, int], list[tuple[str, ExtractedObject]]] = {}
    occurrences: Counter[tuple[str, str, str]] = Counter()
    for backend, page in observed:
        for item in sorted(page.objects, key=lambda value: value.object_id):
            identity_text = _normalized_fact_text(item.caption or item.text or "")
            occurrence_key = (backend, item.object_type, identity_text)
            occurrences[occurrence_key] += 1
            grouped.setdefault(
                (item.object_type, identity_text, occurrences[occurrence_key]), []
            ).append(
                (backend, item)
            )
    result: list[ExtractedObject] = []
    for (object_type, identity_text, occurrence), values in sorted(grouped.items()):
        preferred_backend, preferred = min(
            values, key=lambda item: (_backend_rank(item[0], "object"), item[1].object_id)
        )
        source_backends = tuple(sorted({backend for backend, _item in values}))
        result.append(
            ExtractedObject(
                object_id=(
                    f"fusion:{page_index}:{object_type}:"
                    f"{hashlib.sha256(f'{identity_text}:{occurrence}'.encode()).hexdigest()[:16]}"
                ),
                object_type=object_type,
                page_index=page_index,
                caption=next(
                    (item.caption for _backend, item in values if item.caption), None
                ),
                text=next((item.text for _backend, item in values if item.text), None),
                bbox=next(
                    (
                        item.bbox
                        for backend, item in sorted(
                            values,
                            key=lambda value: _backend_rank(value[0], "bbox"),
                        )
                        if item.bbox is not None
                    ),
                    None,
                ),
                method=(
                    "canonical_parser_fusion"
                    if len(source_backends) > 1
                    else preferred.method
                ),
                confidence=max(
                    (item.confidence for _backend, item in values if item.confidence is not None),
                    default=None,
                ),
                source_backends=source_backends,
                fact_sources={
                    "identity": _object_fact_sources(
                        values, page_index, occurrence
                    ),
                    "selected": (
                        _source_detail(
                            preferred_backend,
                            preferred,
                            source_region_id=_parser_source_region_id(
                                "object",
                                preferred_backend,
                                page_index,
                                preferred.object_id,
                            ),
                            source_anchor=preferred.object_id,
                            occurrence_index=occurrence,
                        ),
                    ),
                },
            )
        )
    return tuple(result)


def _fuse_relations(
    page_index: int,
    observed: tuple[tuple[str, ExtractedPage], ...],
    conflicts: list[dict[str, Any]],
) -> tuple[ExtractedRelation, ...]:
    """Project legacy relations with exact record and endpoint provenance.

    The compatibility page builder calls this for completed parser relations.
    Stable relation-ID ordering makes duplicate occurrence assignment independent
    of input order, while established relation field choices remain unchanged.
    Additive metadata distinguishes the parser relation record from its source
    endpoint anchor for T05 audit consumers; T08 target resolution remains out of
    scope and target text is never copied into fact-source metadata.
    """
    grouped: dict[
        tuple[str, str, str, int], list[tuple[str, ExtractedRelation]]
    ] = {}
    occurrences: Counter[tuple[str, str, str, str]] = Counter()
    for backend, page in observed:
        for item in sorted(page.relations, key=lambda value: value.relation_id):
            identity = (
                item.relation_type,
                item.target_anchor or "",
                item.target_text or "",
            )
            occurrence_key = (backend, *identity)
            occurrences[occurrence_key] += 1
            grouped.setdefault((*identity, occurrences[occurrence_key]), []).append(
                (backend, item)
            )
    result: list[ExtractedRelation] = []
    for key, values in sorted(grouped.items()):
        occurrence = key[-1]
        preferred_backend, preferred = min(
            values,
            key=lambda item: (_backend_rank(item[0], "relation"), item[1].relation_id),
        )
        source_backends = tuple(sorted({backend for backend, _item in values}))
        result.append(
            ExtractedRelation(
                relation_id=(
                    f"fusion:{page_index}:relation:"
                    f"{hashlib.sha256(repr(key).encode()).hexdigest()[:16]}"
                ),
                source_anchor=preferred.source_anchor,
                target_anchor=preferred.target_anchor,
                relation_type=preferred.relation_type,
                target_text=preferred.target_text,
                status=preferred.status,
                method=(
                    "canonical_parser_fusion"
                    if len(source_backends) > 1
                    else preferred.method
                ),
                confidence=max(
                    (item.confidence for _backend, item in values if item.confidence is not None),
                    default=None,
                ),
                bbox=next(
                    (
                        item.bbox
                        for backend, item in sorted(
                            values,
                            key=lambda value: _backend_rank(value[0], "bbox"),
                        )
                        if item.bbox is not None
                    ),
                    None,
                ),
                source_backends=source_backends,
                fact_sources={
                    "identity": _relation_fact_sources(
                        values, page_index, occurrence
                    ),
                    "selected": (
                        _source_detail(
                            preferred_backend,
                            preferred,
                            source_region_id=(
                                f"relation:{preferred_backend}:"
                                f"{preferred.relation_id}"
                            ),
                            source_anchor=preferred.source_anchor,
                            occurrence_index=occurrence,
                            parser_relation_id=preferred.relation_id,
                        ),
                    ),
                },
            )
        )
    return tuple(result)


def _fuse_sections(
    results: tuple[ExtractionResult, ...]
) -> tuple[ExtractedSection, ...]:
    values: dict[tuple[str, int, int], list[tuple[str, ExtractedSection]]] = {}
    for result in results:
        for section in result.sections:
            key = (
                _normalized_fact_text(section.title),
                section.start_page_index,
                section.end_page_index,
            )
            values.setdefault(key, []).append((result.backend, section))
    return tuple(
        ExtractedSection(
            section_id=(
                "fusion:section:"
                f"{hashlib.sha256(repr(key).encode()).hexdigest()[:16]}"
            ),
            title=min(item.title for _backend, item in group),
            start_page_index=key[1],
            end_page_index=key[2],
            method="canonical_parser_fusion",
            confidence=max(
                (item.confidence for _backend, item in group if item.confidence is not None),
                default=None,
            ),
        )
        for key, group in sorted(values.items())
    )


def _backend_rank(backend: str, fact: str) -> tuple[int, str]:
    if fact in {"page_label", "printed_page_label", "width", "height", "bbox", "relation"}:
        preferred = {"pymupdf": 0, "docling": 1, "basic": 99}
    elif fact in {"object"}:
        preferred = {"docling": 0, "pymupdf": 1, "basic": 99}
    else:
        preferred = {"pymupdf": 0, "docling": 1, "basic": 99}
    return preferred.get(backend, 50), backend


def _block_type_rank(value: str) -> tuple[int, str]:
    priority = {
        "title": 0,
        "section_header": 1,
        "heading": 1,
        "caption": 2,
        "table": 2,
        "figure": 2,
        "list_item": 3,
        "list": 3,
        "text": 99,
    }
    return priority.get(value, 50), value


def _normalized_fact_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _source_detail(
    backend: str,
    item: Any,
    *,
    source_region_id: str,
    source_anchor: str,
    occurrence_index: int,
    parser_relation_id: str | None = None,
) -> Mapping[str, Any]:
    """Describe one exact parser-local occurrence for T05 compatibility.

    The legacy projection helpers call this after choosing a parser item. The
    metadata preserves the old backend, method, and confidence values while
    adding the region, anchor, and occurrence needed to bind that choice to one
    observation. Relation IDs are retained separately because a relation's
    source anchor identifies its endpoint rather than the relation record.
    Canonical-content and audit consumers use the later enriched observation and
    decision IDs; no parser source text is copied here.
    """
    detail = {
        "backend": backend,
        "method": item.method,
        "confidence": item.confidence,
        "source_region_id": source_region_id,
        "source_anchor": source_anchor,
        "occurrence_index": occurrence_index,
    }
    if parser_relation_id is not None:
        detail["parser_relation_id"] = parser_relation_id
    return detail


def _parser_source_region_id(
    kind: str, backend: str, page_index: int, parser_anchor: str
) -> str:
    """Return the stable T05 region ID for one parser-local block or object.

    Compatibility projection and observation adaptation share this hashing
    algorithm so a projected fact can name the exact region that produced it.
    Hashing bounds parser-owned identifiers without treating them as paths. The
    helper is deterministic, performs no I/O, and is consumed only inside the
    parser-to-T05 boundary; relation regions retain their established format.
    """
    digest = hashlib.sha256(parser_anchor.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{backend}:{page_index}:{digest}"


def _block_fact_sources(
    values: list[tuple[str, ExtractedBlock]],
    field_name: str,
    selected: Any,
    page_index: int,
    occurrence_index: int,
) -> tuple[Mapping[str, Any], ...]:
    """Retain exact parser block identities supporting one projected fact.

    The legacy block fuser calls this for text, type, and geometry. It filters
    only items equal to the selected legacy value, then records the parser block
    anchor, its shared T05 source-region ID, and deterministic duplicate-text
    occurrence. Existing compatibility values are not reselected or copied.
    """
    return tuple(
        _source_detail(
            backend,
            block,
            source_region_id=_parser_source_region_id(
                "block", backend, page_index, block.block_id
            ),
            source_anchor=block.block_id,
            occurrence_index=occurrence_index,
        )
        for backend, block in sorted(values, key=lambda item: item[0])
        if getattr(block, field_name) == selected
    )


def _object_fact_sources(
    values: list[tuple[str, ExtractedObject]],
    page_index: int,
    occurrence_index: int,
) -> tuple[Mapping[str, Any], ...]:
    """Retain every parser-local object identity in one compatibility group.

    Object fusion calls this for its identity projection. Each additive source
    names the exact parser object region and occurrence while preserving the old
    source metadata. T05 enrichment and canonical audit consumers use these
    locators without embedding captions, object text, or other source values.
    """
    return tuple(
        _source_detail(
            backend,
            item,
            source_region_id=_parser_source_region_id(
                "object", backend, page_index, item.object_id
            ),
            source_anchor=item.object_id,
            occurrence_index=occurrence_index,
        )
        for backend, item in sorted(values, key=lambda value: value[0])
    )


def _relation_fact_sources(
    values: list[tuple[str, ExtractedRelation]],
    page_index: int,
    occurrence_index: int,
) -> tuple[Mapping[str, Any], ...]:
    """Retain exact parser relation records and their endpoint anchors.

    Relation fusion calls this for its compatibility identity group. The stable
    source-region ID binds the relation observation, while ``source_anchor``
    continues to identify the relation endpoint and ``parser_relation_id``
    distinguishes parser-local relation records. No target text is duplicated.
    """
    return tuple(
        _source_detail(
            backend,
            item,
            source_region_id=f"relation:{backend}:{item.relation_id}",
            source_anchor=item.source_anchor,
            occurrence_index=occurrence_index,
            parser_relation_id=item.relation_id,
        )
        for backend, item in sorted(values, key=lambda value: value[0])
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


def _image_bbox(page: Any, image: Any) -> tuple[float, float, float, float] | None:
    rectangles = tuple(page.get_image_rects(image))
    if not rectangles:
        return None
    return (
        min(float(item.x0) for item in rectangles),
        min(float(item.y0) for item in rectangles),
        max(float(item.x1) for item in rectangles),
        max(float(item.y1) for item in rectangles),
    )


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
