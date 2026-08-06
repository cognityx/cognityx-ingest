"""Preserve authoritative parser capability evidence without choosing parsers.

Purpose
-------
Parser packages, official documentation, operator policy, and measured outcomes
answer different questions. This module keeps those facts in one parser
capability registry so later code can inspect current evidence without relying on
an LLM's memory. The live registry is the authority because model memory may be
old, incomplete, or unable to distinguish an installed adapter from an
advertised feature.

Registry authority means evidence is explicit, reviewable, and current enough to
be checked. It does not mean every source agrees.

Design principles
-----------------
Every parser record always exposes exactly three source classes.
``parser-discovered`` contains live runtime/package inspection and frozen
official documentation evidence; internet research is only a future mechanism
for refreshing that discovered evidence, never a fourth source class.
``human-guided`` retains approved preferences and restrictions without rewriting
them as parser facts. ``auto-learned`` retains finite measurements without
turning them into undocumented recommendations. Runtime availability and an
officially documented capability remain separate facts, including when they
conflict.

Processing flow
---------------
Strict readers parse frozen JSON into immutable records, preserve supplied
collection order for validation, normalize only the legacy fixture's bounded
``installed`` probe, and serialize deterministic UTF-8 JSON. One exact ordering
fingerprint admits the frozen v3.2 fixture; every other registry must use
canonical lexical order. ``ParserCapabilityRegistry.from_router``
reads a deterministic plugin snapshot, uses ``importlib`` discovery without
importing heavyweight parser packages, overlays only live runtime facts onto an
optional catalog, and preserves documented, human, learned, and conflict
evidence. No document is parsed during this flow.

Primary consumers
-----------------
Audit tools and tests can read the registry now. T04 consumes it later as an
input to adaptive routing. An LLM may propose a plan in that future boundary,
but neither the LLM nor T03 can replace registry facts.

Ownership boundary
------------------
Cognityx Ingest owns adapter registration, capability records, validation, and
the live overlay. Parser projects own their package APIs and official documents.
Operators own human guidance, and benchmark or production systems own measured
evidence. Storage, SDK, CLI, DataForge, and parser model lifecycle remain outside
this module.

Non-goals
---------
T03 does not choose, score, route, invoke, align, fuse, or adjudicate parsers. It
does not change ``ExtractionPolicy``, fetch internet evidence, initialize Docling
or PyMuPDF, parse a source, download a model, persist a database, implement T04,
or modify canonical-content and native-artifact formats.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import importlib.metadata
import importlib.util
import ipaddress
import json
import math
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from cognityx_ingest.parser import (
    DoclingParser,
    ParserPlugin,
    ParserRouter,
    PyMuPDFParser,
    PyPdfExtractor,
)


PARSER_CAPABILITY_REGISTRY_SCHEMA = (
    "cognityx.ingest.parser-capability-registry/v3.2"
)
CAPABILITY_SOURCE_CLASSES: tuple[str, str, str] = (
    "parser-discovered",
    "human-guided",
    "auto-learned",
)

_ALLOWED_ROUTING_MODES: tuple[str, str, str] = (
    "deterministic",
    "hybrid",
    "llm-directed",
)
_CAPABILITY_STATUSES = frozenset(
    {
        "available",
        "declared",
        "declared-when-available",
        "unsupported",
        "not-declared",
        "unavailable",
        "unknown",
    }
)
_ADVERTISED_STATUSES = frozenset(
    {"available", "declared", "declared-when-available"}
)
_BUILTIN_DEPENDENCIES = MappingProxyType(
    {
        PyPdfExtractor: ("pypdf", "pypdf"),
        PyMuPDFParser: ("fitz", "PyMuPDF"),
        DoclingParser: ("docling", "docling"),
    }
)
_FROZEN_LEGACY_REGISTRY_VERSION = "2026-08-06.1"
_FROZEN_LEGACY_ORDER_FINGERPRINT = (
    (
        "docling",
        (
            "docling-doc-model-20260806",
            "docling-chunking-20260806",
            "docling-formats-20260806",
        ),
        (
            "document_hierarchy",
            "tables",
            "pictures",
            "bounding_boxes",
            "provenance",
            "native_chunker_interface",
        ),
        (
            (
                "born-digital structured policy or procedure",
                "preferred-primary",
            ),
            (
                "native PDF links and annotations are mandatory",
                "supplement-with-pymupdf",
            ),
        ),
        (
            ("native-policy-pdf", "structure_recall"),
            ("mixed-scanned-pdf", "structure_recall"),
        ),
        (("document_hierarchy", "declared-but-currently-unavailable"),),
    ),
    (
        "pymupdf",
        ("pymupdf-page-api-20260806",),
        (
            "native_pdf_text",
            "native_links",
            "annotations",
            "page_labels",
            "semantic_document_hierarchy",
        ),
        (
            (
                "native links, annotations, page labels or exact PDF geometry required",
                "preferred-complementary",
            ),
        ),
        (("native-policy-pdf", "native_link_recall"),),
        (),
    ),
    (
        "future-parser",
        (),
        ("paragraph_text", "text_position_selectors", "tables"),
        (("parser-neutral substitution test", "allowed"),),
        (),
        (),
    ),
)


class ParserCapabilityError(Exception):
    """Base failure for parser capability registry operations.

    Responsibility:
        Give callers one stable domain boundary instead of leaking JSON,
        ``importlib``, package metadata, or mapping errors.
    Constructed by:
        Public registry readers, validators, discovery, and lookup methods.
    Used by:
        Ingest composition, audit tools, tests, and future T04 consumers.
    Invariants:
        Diagnostics identify capability records but expose no source payload,
        credential, environment secret, or local filesystem path.
    Lifecycle/persistence:
        Exceptions are transient and never serialized into the registry.
    Thread-safety assumptions:
        Ordinary immutable exception arguments carry no shared mutable state.
    """


class ParserCapabilityValidationError(ParserCapabilityError):
    """Report malformed or internally inconsistent capability evidence.

    Responsibility:
        Distinguish invalid registry data from parser execution and storage errors.
    Constructed by:
        Strict deserialization and aggregate validation.
    Used by:
        Catalog readers, runtime overlay callers, and trust-boundary tests.
    Invariants:
        Invalid records are never returned as a validated registry.
    Lifecycle/persistence:
        Detection is read-only and does not repair frozen evidence in place.
    Thread-safety assumptions:
        The exception has no mutable shared state.
    """


class ParserCapabilityNotFoundError(ParserCapabilityError):
    """Report lookup of a parser ID absent from a validated registry.

    Responsibility:
        Replace raw ``KeyError`` with a caller-facing domain failure.
    Constructed by:
        ``ParserCapabilityRegistry.get``.
    Used by:
        Audit readers and future T04 registry consumers.
    Invariants:
        The message contains only the requested logical parser ID.
    Lifecycle/persistence:
        The transient failure does not mutate or persist registry state.
    Thread-safety assumptions:
        The exception contains immutable diagnostic text.
    """


class ParserCapabilityConflictError(ParserCapabilityValidationError):
    """Report an invalid attempt to collapse incompatible capability evidence.

    Responsibility:
        Preserve documented, runtime, human, and learned facts when a merge
        cannot represent all inputs honestly.
    Constructed by:
        Catalog overlay and conflict-validation helpers.
    Used by:
        Registry builders and future catalog refresh workflows.
    Invariants:
        Conflicting evidence is never silently deleted or converted to a Boolean.
    Lifecycle/persistence:
        The error is transient; supplied immutable catalogs remain unchanged.
    Thread-safety assumptions:
        No mutable registry state is attached to the exception.
    """


@dataclass(frozen=True, slots=True)
class ParserRuntimeProbe:
    """Describe current adapter and optional-package availability separately.

    Responsibility:
        Record registration, importability, installed version, and adapter
        identity without importing or executing the parser.
    Constructed by:
        Runtime discovery or strict catalog deserialization.
    Used by:
        Audit readers, conflict derivation, and future T04 availability checks.
    Invariants:
        Unknown facts use ``None``; registration is never inferred from package
        installation, and version text is present only when observed.
    Lifecycle/persistence:
        Frozen snapshots describe one discovery moment and can be serialized.
    Thread-safety assumptions:
        Immutable scalar fields are safe for concurrent readers.
    """

    plugin_registered: bool | None
    dependency_importable: bool | None
    installed_version: str | None
    adapter_module: str | None
    adapter_class: str | None
    reason: str | None = None

    @property
    def runtime_available(self) -> bool | None:
        """Derive present invocability without making a routing recommendation.

        Registry and audit callers use this read-only fact. A known missing plugin
        or dependency is unavailable, two positive observations are available,
        and incomplete observations remain unknown. The calculation has no side
        effects and raises no parser or import errors.
        """
        if self.plugin_registered is False or self.dependency_importable is False:
            return False
        if self.plugin_registered is True and self.dependency_importable is True:
            return True
        return None


@dataclass(frozen=True, slots=True)
class OfficialDocumentationEvidence:
    """Retain one frozen assertion from an official parser source.

    Responsibility:
        Preserve evidence identity, public URL, retrieval date, and bounded summary
        without performing a network request.
    Constructed by:
        Strict fixture/catalog readers and future explicit refresh workflows.
    Used by:
        Auditors, conflict readers, and later T04 planning inputs.
    Invariants:
        IDs are non-empty, URLs are public HTTP(S), dates are ISO calendar dates,
        and summaries are non-empty.
    Lifecycle/persistence:
        Frozen evidence remains historical even when live runtime facts change.
    Thread-safety assumptions:
        Immutable strings make records safe to share.
    """

    evidence_id: str
    source_url: str
    retrieved_on: str
    summary: str


@dataclass(frozen=True, slots=True)
class CapabilityAssertion:
    """Preserve one named capability with a non-Boolean evidence status.

    Responsibility:
        Keep advertised, available, unavailable, unsupported, and unknown states
        distinguishable instead of reducing them to true or false.
    Constructed by:
        Catalog readers and explicit parser-discovered evidence producers.
    Used by:
        Audit, conflict derivation, and future T04 policy evaluation.
    Invariants:
        Capability names are non-empty and status belongs to the documented
        extensibility gate represented by the current supported status set.
    Lifecycle/persistence:
        Frozen assertions persist with their parser-discovered source record.
    Thread-safety assumptions:
        String-only records are immutable.
    """

    capability: str
    status: str


@dataclass(frozen=True, slots=True)
class ParserDiscoveredCapabilities:
    """Group live runtime and frozen official parser evidence.

    Responsibility:
        Keep package observations, official documents, and declared capability
        statuses together while retaining each fact independently.
    Constructed by:
        Strict catalog readers and ``ParserCapabilityRegistry.from_router``.
    Used by:
        Audit tools, conflict preservation, and future T04 consumers.
    Invariants:
        Evidence and capability identities are unique and deterministically
        ordered; runtime availability does not overwrite documented assertions.
    Lifecycle/persistence:
        Catalog evidence persists while runtime probes can be replaced by a live
        immutable snapshot during overlay.
    Thread-safety assumptions:
        Nested frozen records and tuples are safe for concurrent reads.
    """

    runtime_probe: ParserRuntimeProbe
    official_documentation: tuple[OfficialDocumentationEvidence, ...] = ()
    capabilities: tuple[CapabilityAssertion, ...] = ()


@dataclass(frozen=True, slots=True)
class HumanGuidance:
    """Preserve one approved parser preference or restriction.

    Responsibility:
        Keep a human-authored condition and recommendation separate from parser
        documentation and measured outcomes.
    Constructed by:
        Strict catalog readers or a future governed approval workflow.
    Used by:
        Operators, auditors, and future T04 policy enforcement.
    Invariants:
        Condition and recommendation are non-empty and are never recast as
        parser-discovered facts.
    Lifecycle/persistence:
        Frozen guidance persists until an external governed catalog revision.
    Thread-safety assumptions:
        Immutable strings are safe for concurrent readers.
    """

    condition: str
    recommendation: str


@dataclass(frozen=True, slots=True)
class AutoLearnedMeasurement:
    """Retain one measured parser outcome without assuming a percentage scale.

    Responsibility:
        Associate document class, metric, finite numeric value, and sample count.
    Constructed by:
        Strict catalog readers or future benchmark/feedback producers.
    Used by:
        Auditors and future T04 policy logic after metric-specific interpretation.
    Invariants:
        Names are non-empty, values are finite, and sample counts are nonnegative.
    Lifecycle/persistence:
        Frozen measurements remain tied to their benchmark profile.
    Thread-safety assumptions:
        Immutable scalar fields are safe to share.
    """

    document_class: str
    metric: str
    value: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class AutoLearnedCapabilities:
    """Group measured evidence under one benchmark or feedback profile.

    Responsibility:
        Keep learned measurements isolated from documentation and human policy.
    Constructed by:
        Strict catalog readers and router-only default construction.
    Used by:
        Audit tooling and future metric-aware T04 consumers.
    Invariants:
        Measurement identities are unique and ordered; an empty tuple makes no
        fabricated performance claim.
    Lifecycle/persistence:
        Frozen profile snapshots are replaced only by a later catalog revision.
    Thread-safety assumptions:
        The optional string and tuple of frozen records are immutable.
    """

    benchmark_profile: str | None = None
    measurements: tuple[AutoLearnedMeasurement, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityConflict:
    """Describe advertised capability evidence that runtime cannot currently use.

    Responsibility:
        Preserve both the advertised assertion and runtime observation with an
        explicit resolution label instead of deleting either fact.
    Constructed by:
        Frozen catalogs or the bounded availability-conflict overlay.
    Used by:
        Auditors and future T04 planners that must handle unavailable adapters.
    Invariants:
        Capability and resolution are non-empty; Boolean fields remain factual
        observations rather than parser-selection recommendations.
    Lifecycle/persistence:
        Explicit conflicts persist, while derived conflicts belong to one live
        registry snapshot.
    Thread-safety assumptions:
        Frozen scalar fields are safe for concurrent readers.
    """

    capability: str
    advertised: bool
    runtime_available: bool
    resolution: str


@dataclass(frozen=True, slots=True)
class ParserCapabilityRecord:
    """Aggregate all three evidence classes for one parser identity.

    Responsibility:
        Present discovered facts, human guidance, learned measurements, and
        preserved conflicts without collapsing their provenance.
    Constructed by:
        Strict deserialization or runtime/catalog overlay.
    Used by:
        Registry lookup/list callers, audit tools, and future T04 routing logic.
    Invariants:
        Parser identity and version scope are non-empty, nested collections are
        deterministic, and exactly three source classes are always exposed.
    Lifecycle/persistence:
        Frozen records are serialized as part of a versioned registry snapshot.
    Thread-safety assumptions:
        All fields contain immutable records or tuples.
    """

    parser_id: str
    version_scope: str
    parser_discovered: ParserDiscoveredCapabilities
    human_guided: tuple[HumanGuidance, ...]
    auto_learned: AutoLearnedCapabilities
    conflicts: tuple[CapabilityConflict, ...] = ()

    @property
    def capability_source_classes(self) -> tuple[str, str, str]:
        """Return the exact contractual source classes regardless of empty evidence.

        Registry and future T04 callers use this immutable property to verify the
        evidence boundary. It always returns the module constant in contractual
        order, performs no I/O, and cannot be changed by missing guidance or
        measurements.
        """
        return CAPABILITY_SOURCE_CLASSES


@dataclass(frozen=True, slots=True)
class ParserCapabilityRegistry:
    """Validate, serialize, overlay, and query one capability snapshot.

    Responsibility:
        Hold parser records plus schema/version and permitted future routing-mode
        metadata without implementing routing behavior.
    Constructed by:
        ``from_dict``, ``from_json_bytes``, or ``from_router``.
    Used by:
        Audit tools, tests, and future T04 routing composition.
    Invariants:
        Schema, source classes, routing metadata, parser identities, nested
        evidence, and deterministic ordering all validate before public use.
    Lifecycle/persistence:
        Frozen snapshots serialize deterministically; this class performs no
        storage or network writes.
    Thread-safety assumptions:
        Methods use local indexes and immutable records, making reads safe to share.
    """

    schema: str
    registry_version: str
    parsers: tuple[ParserCapabilityRecord, ...]
    allowed_routing_modes: tuple[str, ...] = _ALLOWED_ROUTING_MODES

    @classmethod
    def from_router(
        cls,
        router: ParserRouter,
        *,
        catalog: ParserCapabilityRegistry | None = None,
    ) -> ParserCapabilityRegistry:
        """Build a live registry without parsing documents or mutating a catalog.

        Ingest composition and audit tools call this with an existing
        ``ParserRouter`` and optional frozen catalog. The algorithm snapshots
        registered plugins, performs bounded import/package metadata probes,
        overlays only runtime facts, preserves all other evidence, adds missing
        availability conflicts, and retains router-only and catalog-only parsers.
        It makes no network request or parser call and is idempotent for unchanged
        runtime metadata. Invalid catalogs raise typed validation failures.
        """
        if not isinstance(router, ParserRouter):
            raise ParserCapabilityValidationError("router must be a ParserRouter")
        if catalog is not None and not isinstance(catalog, ParserCapabilityRegistry):
            raise ParserCapabilityValidationError(
                "catalog must be a ParserCapabilityRegistry or null"
            )
        if catalog is not None:
            catalog.validate()
        plugins = _registered_plugin_index(router)
        catalog_records = (
            {record.parser_id: record for record in catalog.parsers}
            if catalog is not None
            else {}
        )
        parser_ids = sorted(set(plugins) | set(catalog_records))
        records = tuple(
            _overlay_parser_record(
                parser_id,
                plugin=plugins.get(parser_id),
                catalog_record=catalog_records.get(parser_id),
            )
            for parser_id in parser_ids
        )
        registry = cls(
            schema=PARSER_CAPABILITY_REGISTRY_SCHEMA,
            registry_version=(
                catalog.registry_version if catalog is not None else "runtime-v1"
            ),
            parsers=records,
            allowed_routing_modes=(
                catalog.allowed_routing_modes
                if catalog is not None
                else _ALLOWED_ROUTING_MODES
            ),
        )
        registry.validate()
        return registry

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ParserCapabilityRegistry:
        """Parse and validate untrusted fixture or canonical registry data.

        Catalog readers call this at the JSON trust boundary. It checks exact
        field shapes, accepts only the frozen fixture's bounded legacy
        ``installed`` probe or the canonical runtime-probe shape, preserves every
        supplied sequence, and then validates ordering and cross-record
        invariants. No input mapping is modified or silently repaired. Malformed
        data raises ``ParserCapabilityValidationError`` rather than raw mapping or
        JSON errors.
        """
        try:
            _require_fields(
                value,
                {
                    "schema",
                    "registry_version",
                    "allowed_capability_source_classes",
                    "allowed_routing_modes",
                    "parsers",
                },
                "parser capability registry",
            )
            source_classes = _string_tuple(
                value["allowed_capability_source_classes"],
                "allowed_capability_source_classes",
            )
            if source_classes != CAPABILITY_SOURCE_CLASSES:
                raise ParserCapabilityValidationError(
                    "Registry must expose exactly the three capability source classes"
                )
            raw_parsers = _mapping_sequence(value["parsers"], "parsers")
            parsed = tuple(_parse_parser_record(item) for item in raw_parsers)
            _reject_duplicate_values(
                (record.parser_id for record in parsed),
                "parser ID",
            )
            registry = cls(
                schema=_required_text(value["schema"], "schema"),
                registry_version=_required_text(
                    value["registry_version"], "registry_version"
                ),
                parsers=parsed,
                allowed_routing_modes=_string_tuple(
                    value["allowed_routing_modes"], "allowed_routing_modes"
                ),
            )
        except ParserCapabilityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise ParserCapabilityValidationError(
                "Parser capability registry contains malformed values"
            ) from error
        registry.validate()
        return registry

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> ParserCapabilityRegistry:
        """Decode strict UTF-8 JSON and return one validated immutable registry.

        File and artifact readers call this method with untrusted bytes. It decodes
        UTF-8, rejects duplicate object keys at every nesting level, requires a
        JSON object, and delegates nested validation to ``from_dict``. The method
        performs no writes and never includes source payload values in duplicate
        diagnostics. Unicode, JSON, and field errors become typed validation
        failures.
        """
        if not isinstance(payload, bytes):
            raise ParserCapabilityValidationError("Registry payload must be bytes")
        try:
            value = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_strict_json_object
            )
        except ParserCapabilityError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ParserCapabilityValidationError(
                "Registry payload is not valid UTF-8 JSON"
            ) from error
        if not isinstance(value, Mapping):
            raise ParserCapabilityValidationError("Registry JSON must be an object")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic JSON-compatible registry representation.

        Persistence and audit callers use this after validation. It emits the
        exact three source-class keys, canonical runtime-probe fields, validated
        parser/evidence ordering, and routing-mode metadata without producing a
        route. The frozen v3.2 fixture retains its exact admitted legacy ordering.
        The method performs no I/O and repeated calls return equal mappings.
        """
        self.validate()
        return {
            "schema": self.schema,
            "registry_version": self.registry_version,
            "allowed_capability_source_classes": list(CAPABILITY_SOURCE_CLASSES),
            "allowed_routing_modes": list(self.allowed_routing_modes),
            "parsers": [_parser_record_to_dict(item) for item in self.parsers],
        }

    def to_json_bytes(self) -> bytes:
        """Serialize validated registry data to stable compact UTF-8 JSON bytes.

        Catalog persistence and integrity tests call this method. Canonical
        registries sort object keys; the exact frozen legacy snapshot preserves
        its admitted capability-key sequence so reloading cannot invalidate its
        fingerprint. Both paths use compact separators, preserve Unicode, append
        one newline, and produce identical bytes for identical registries.
        """
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=not _has_frozen_legacy_order(self),
            )
            + "\n"
        ).encode("utf-8")

    def validate(self) -> None:
        """Validate schema, identities, evidence, ordering, and metadata.

        Builders and readers call this before lookup or serialization. The
        algorithm verifies exact schema/routing constants, parser and nested IDs,
        runtime facts, public URLs and dates, finite measurements, conflicts, and
        canonical tuple ordering. Validation is read-only and idempotent. Typed
        validation or conflict errors protect callers from malformed catalogs.
        """
        try:
            _validate_registry(self)
        except ParserCapabilityError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise ParserCapabilityValidationError(
                "Parser capability registry contains malformed typed records"
            ) from error

    def get(self, parser_id: str) -> ParserCapabilityRecord:
        """Return one parser record or raise the typed not-found error.

        Audit and future T04 callers provide a logical parser ID. The registry is
        validated, then searched without exposing a mutable index. The method has
        no side effects and returns the same frozen record on repeated calls.
        Empty or unknown IDs raise typed capability errors rather than ``KeyError``.
        """
        self.validate()
        requested = _required_text(parser_id, "parser_id")
        for record in self.parsers:
            if record.parser_id == requested:
                return record
        raise ParserCapabilityNotFoundError(f"Unknown parser capability: {requested}")

    def list(self) -> tuple[ParserCapabilityRecord, ...]:
        """Return all validated parser records as an immutable ordered tuple.

        Registry browsers and future T04 composition call this read-only method.
        It validates the snapshot and returns the existing tuple in parser-ID
        order, performs no copying of evidence payloads, and exposes no mutation
        seam. Invalid direct construction raises a typed validation error.
        """
        self.validate()
        return self.parsers


def _validate_registry(registry: ParserCapabilityRegistry) -> None:
    """Validate one aggregate while the public method translates raw type errors.

    ``ParserCapabilityRegistry.validate`` delegates here so malformed directly
    constructed records receive the same typed trust-boundary behavior as parsed
    JSON. The helper checks schema, metadata, deterministic order, parser records,
    and globally unique documentation evidence without mutating the snapshot.
    """
    if registry.schema != PARSER_CAPABILITY_REGISTRY_SCHEMA:
        raise ParserCapabilityValidationError(
            f"Unsupported parser capability registry schema: {registry.schema}"
        )
    _required_text(registry.registry_version, "registry_version")
    if registry.allowed_routing_modes != _ALLOWED_ROUTING_MODES:
        raise ParserCapabilityValidationError(
            "Allowed routing modes must be deterministic, hybrid, llm-directed"
        )
    _reject_duplicate_values(
        (record.parser_id for record in registry.parsers), "parser ID"
    )
    frozen_legacy_order = _has_frozen_legacy_order(registry)
    evidence_ids: list[str] = []
    for record in registry.parsers:
        _validate_parser_record(record)
        evidence_ids.extend(
            item.evidence_id
            for item in record.parser_discovered.official_documentation
        )
    _reject_duplicate_values(evidence_ids, "documentation evidence ID")
    if not frozen_legacy_order:
        if registry.parsers != tuple(
            sorted(registry.parsers, key=lambda item: item.parser_id)
        ):
            raise ParserCapabilityValidationError(
                "Parser records are not deterministically ordered"
            )
        for record in registry.parsers:
            _validate_parser_record_order(record)


def _has_frozen_legacy_order(registry: ParserCapabilityRegistry) -> bool:
    """Admit only the complete ordering fingerprint of the frozen v3.2 catalog.

    Validation calls this before enforcing lexical order. The version and every
    ordered identity sequence must match the authoritative fixture exactly; a
    reordered, extended, or partially imitated payload falls through to normal
    canonical validation. Values still receive all ordinary semantic checks.
    """
    if registry.registry_version != _FROZEN_LEGACY_REGISTRY_VERSION:
        return False
    return _registry_order_fingerprint(registry) == (
        _FROZEN_LEGACY_ORDER_FINGERPRINT
    )


def _registry_order_fingerprint(
    registry: ParserCapabilityRegistry,
) -> tuple[object, ...]:
    """Describe every contractually ordered identity without copying evidence.

    The frozen-compatibility check uses this structural tuple rather than source
    payload bytes. It records parser, documentation, capability, guidance,
    measurement, and conflict identities in their supplied order while excluding
    summaries and metric values from diagnostics and comparison messages.
    """
    return tuple(
        (
            record.parser_id,
            tuple(
                item.evidence_id
                for item in record.parser_discovered.official_documentation
            ),
            tuple(
                item.capability for item in record.parser_discovered.capabilities
            ),
            tuple(_guidance_order(item) for item in record.human_guided),
            tuple(
                _measurement_order(item)
                for item in record.auto_learned.measurements
            ),
            tuple(_conflict_order(item) for item in record.conflicts),
        )
        for record in registry.parsers
    )


def _overlay_parser_record(
    parser_id: str,
    *,
    plugin: ParserPlugin | None,
    catalog_record: ParserCapabilityRecord | None,
) -> ParserCapabilityRecord:
    """Overlay only runtime facts while preserving every non-runtime source fact.

    ``from_router`` calls this pure helper once per unioned parser ID. Router
    adapters receive a live bounded probe; catalog-only parsers become explicitly
    unregistered with unknown dependency facts. Documentation, human guidance,
    measurements, and explicit conflicts are reused unchanged. Derived conflicts
    are added deterministically and never delete an existing conflict.
    """
    runtime_probe = (
        _probe_plugin(plugin)
        if plugin is not None
        else ParserRuntimeProbe(
            plugin_registered=False,
            dependency_importable=None,
            installed_version=None,
            adapter_module=None,
            adapter_class=None,
            reason="Parser exists only in the supplied catalog.",
        )
    )
    if catalog_record is None:
        discovered = ParserDiscoveredCapabilities(runtime_probe=runtime_probe)
        record = ParserCapabilityRecord(
            parser_id=parser_id,
            version_scope=runtime_probe.installed_version or "unversioned",
            parser_discovered=discovered,
            human_guided=(),
            auto_learned=AutoLearnedCapabilities(),
        )
    else:
        record = replace(
            catalog_record,
            parser_discovered=replace(
                catalog_record.parser_discovered,
                runtime_probe=runtime_probe,
            ),
        )
    return _canonicalize_parser_record(
        replace(record, conflicts=_preserve_availability_conflicts(record))
    )


def _registered_plugin_index(router: ParserRouter) -> dict[str, ParserPlugin]:
    """Validate a router snapshot before sorting or indexing registered adapters.

    ``from_router`` uses this trust-boundary helper so malformed names, duplicate
    names, and unusable adapter type metadata cannot leak ``TypeError`` or be
    hidden by dictionary overwrite. It reads only registration identity and class
    metadata, never calls extraction, and returns a parser-ID ordered index for
    deterministic runtime discovery.
    """
    try:
        plugins = tuple(router.registered_plugins())
        identified: list[tuple[str, ParserPlugin]] = []
        for plugin in plugins:
            name = _required_text(getattr(plugin, "name", None), "parser plugin name")
            adapter_type = type(plugin)
            _required_text(
                getattr(adapter_type, "__module__", None),
                f"adapter module for parser {name}",
            )
            _required_text(
                getattr(adapter_type, "__qualname__", None),
                f"adapter class for parser {name}",
            )
            identified.append((name, plugin))
    except ParserCapabilityError:
        raise
    except Exception as error:
        raise ParserCapabilityValidationError(
            "Registered parser metadata could not be inspected"
        ) from error
    _reject_duplicate_values((name for name, _ in identified), "parser plugin name")
    return {
        name: plugin
        for name, plugin in sorted(identified, key=lambda item: item[0])
    }


def _canonicalize_parser_record(
    record: ParserCapabilityRecord,
) -> ParserCapabilityRecord:
    """Canonicalize records created by trusted runtime composition only.

    ``from_router`` may overlay a frozen legacy catalog onto current observations.
    Runtime-built output must nevertheless use lexical ordering, so this helper
    sorts each identity-bearing tuple after semantic parsing has already preserved
    and validated the catalog. Untrusted ``from_dict`` input never calls it.
    """
    discovered = replace(
        record.parser_discovered,
        official_documentation=tuple(
            sorted(
                record.parser_discovered.official_documentation,
                key=lambda item: item.evidence_id,
            )
        ),
        capabilities=tuple(
            sorted(
                record.parser_discovered.capabilities,
                key=lambda item: item.capability,
            )
        ),
    )
    learned = replace(
        record.auto_learned,
        measurements=tuple(
            sorted(record.auto_learned.measurements, key=_measurement_order)
        ),
    )
    return replace(
        record,
        parser_discovered=discovered,
        human_guided=tuple(sorted(record.human_guided, key=_guidance_order)),
        auto_learned=learned,
        conflicts=tuple(sorted(record.conflicts, key=_conflict_order)),
    )


def _probe_plugin(plugin: ParserPlugin) -> ParserRuntimeProbe:
    """Inspect one registered adapter without importing or initializing its parser.

    Runtime discovery calls this with an already constructed lightweight adapter.
    Known built-ins map to static import/distribution names and use ``find_spec``
    plus package metadata. Every import or metadata failure becomes an honest
    false/unknown fact and reason. Custom adapters retain registration and class
    identity while dependency/version facts remain unknown rather than guessed.
    """
    adapter_type = type(plugin)
    adapter_module = adapter_type.__module__
    adapter_class = adapter_type.__qualname__
    dependency = _BUILTIN_DEPENDENCIES.get(adapter_type)
    if dependency is None:
        return ParserRuntimeProbe(
            plugin_registered=True,
            dependency_importable=None,
            installed_version=None,
            adapter_module=adapter_module,
            adapter_class=adapter_class,
            reason="Registered custom parser has no declared package metadata.",
        )
    import_module, distribution = dependency
    try:
        importable = importlib.util.find_spec(import_module) is not None
    except (ImportError, AttributeError, ValueError):
        importable = False
    installed_version: str | None = None
    reason: str | None = None
    if importable:
        try:
            installed_version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            reason = "Import module is visible but distribution metadata is absent."
        except Exception:
            reason = "Distribution metadata could not be read."
    else:
        reason = f"Optional dependency {import_module} is not importable."
    return ParserRuntimeProbe(
        plugin_registered=True,
        dependency_importable=importable,
        installed_version=installed_version,
        adapter_module=adapter_module,
        adapter_class=adapter_class,
        reason=reason,
    )


def _preserve_availability_conflicts(
    record: ParserCapabilityRecord,
) -> tuple[CapabilityConflict, ...]:
    """Retain explicit conflicts and add bounded declared/unavailable conflicts.

    Overlay calls this after replacing the runtime probe. If runtime is known
    unavailable, each advertised assertion without an existing capability
    conflict gains one ``declared-but-currently-unavailable`` record. Unknown or
    available runtime does not invent a conflict. Duplicate capabilities with
    incompatible explicit facts raise ``ParserCapabilityConflictError``.
    """
    conflicts = {item.capability: item for item in record.conflicts}
    if len(conflicts) != len(record.conflicts):
        raise ParserCapabilityConflictError(
            f"Duplicate conflicts for parser: {record.parser_id}"
        )
    if record.parser_discovered.runtime_probe.runtime_available is False:
        for assertion in record.parser_discovered.capabilities:
            if assertion.status in _ADVERTISED_STATUSES:
                conflicts.setdefault(
                    assertion.capability,
                    CapabilityConflict(
                        capability=assertion.capability,
                        advertised=True,
                        runtime_available=False,
                        resolution="declared-but-currently-unavailable",
                    ),
                )
    return tuple(sorted(conflicts.values(), key=_conflict_order))


def _parse_parser_record(value: Mapping[str, object]) -> ParserCapabilityRecord:
    """Parse one parser while preserving nested order for strict validation.

    Catalog readers use this helper to construct immutable typed records without
    repairing untrusted input. Ordering is checked later against either canonical
    lexical rules or the one complete frozen-fixture fingerprint.
    """
    _require_fields(
        value,
        {"parser_id", "version_scope", "capability_sources", "conflicts"},
        "parser capability record",
    )
    sources = _mapping(value["capability_sources"], "capability_sources")
    if tuple(sources.keys()) != CAPABILITY_SOURCE_CLASSES and set(sources) != set(
        CAPABILITY_SOURCE_CLASSES
    ):
        raise ParserCapabilityValidationError(
            "Parser record must contain exactly three capability source classes"
        )
    discovered = _parse_discovered(
        _mapping(sources["parser-discovered"], "parser-discovered")
    )
    human_source = _mapping(sources["human-guided"], "human-guided")
    _require_fields(human_source, {"guidance"}, "human-guided source")
    guidance = tuple(
        _parse_human_guidance(item)
        for item in _mapping_sequence(human_source["guidance"], "guidance")
    )
    learned = _parse_auto_learned(
        _mapping(sources["auto-learned"], "auto-learned")
    )
    conflicts = tuple(
        _parse_conflict(item)
        for item in _mapping_sequence(value["conflicts"], "conflicts")
    )
    return ParserCapabilityRecord(
        parser_id=_required_text(value["parser_id"], "parser_id"),
        version_scope=_required_text(value["version_scope"], "version_scope"),
        parser_discovered=discovered,
        human_guided=guidance,
        auto_learned=learned,
        conflicts=conflicts,
    )


def _parse_discovered(value: Mapping[str, object]) -> ParserDiscoveredCapabilities:
    """Parse discovered facts and enforce legacy runtime-field consistency.

    The reader preserves documentation and capability order exactly as supplied.
    A legacy ``runtime_available`` capability is accepted only when it equals the
    value derivable from the runtime probe; unknown or contradictory derivations
    raise a typed conflict instead of creating a second source of truth.
    """
    _require_fields(
        value,
        {"runtime_probe", "official_documentation", "capabilities"},
        "parser-discovered source",
    )
    runtime = _parse_runtime_probe(_mapping(value["runtime_probe"], "runtime_probe"))
    evidence = tuple(
        _parse_documentation(item)
        for item in _mapping_sequence(
            value["official_documentation"], "official_documentation"
        )
    )
    capability_map = _mapping(value["capabilities"], "capabilities")
    assertions: list[CapabilityAssertion] = []
    for capability, status in capability_map.items():
        if capability == "runtime_available":
            if not isinstance(status, bool):
                raise ParserCapabilityValidationError(
                    "runtime_available capability fact must be Boolean"
                )
            if runtime.runtime_available is None:
                raise ParserCapabilityConflictError(
                    "runtime_available capability fact has no derivable runtime value"
                )
            if status is not runtime.runtime_available:
                raise ParserCapabilityConflictError(
                    "runtime_available capability fact conflicts with runtime probe"
                )
            continue
        assertions.append(
            CapabilityAssertion(
                capability=_required_text(capability, "capability"),
                status=_required_text(status, f"capability status for {capability}"),
            )
        )
    return ParserDiscoveredCapabilities(
        runtime_probe=runtime,
        official_documentation=evidence,
        capabilities=tuple(assertions),
    )


def _parse_runtime_probe(value: Mapping[str, object]) -> ParserRuntimeProbe:
    """Parse canonical runtime facts or the frozen fixture's legacy installed fact.

    The authoritative fixture predates the richer T03 runtime shape and stores
    only ``installed`` plus an optional reason. That value maps solely to
    dependency importability; plugin registration remains unknown so the two facts
    are not conflated. Any other partial or mixed shape is rejected.
    """
    legacy_fields = {"installed", "reason"}
    canonical_fields = {
        "plugin_registered",
        "dependency_importable",
        "installed_version",
        "adapter_module",
        "adapter_class",
        "reason",
    }
    if set(value) <= legacy_fields and "installed" in value:
        return ParserRuntimeProbe(
            plugin_registered=None,
            dependency_importable=_required_bool(value["installed"], "installed"),
            installed_version=None,
            adapter_module=None,
            adapter_class=None,
            reason=_optional_text(value.get("reason"), "reason"),
        )
    _require_fields(value, canonical_fields, "runtime probe")
    return ParserRuntimeProbe(
        plugin_registered=_optional_bool(
            value["plugin_registered"], "plugin_registered"
        ),
        dependency_importable=_optional_bool(
            value["dependency_importable"], "dependency_importable"
        ),
        installed_version=_optional_text(
            value["installed_version"], "installed_version"
        ),
        adapter_module=_optional_text(value["adapter_module"], "adapter_module"),
        adapter_class=_optional_text(value["adapter_class"], "adapter_class"),
        reason=_optional_text(value["reason"], "reason"),
    )


def _parse_documentation(
    value: Mapping[str, object],
) -> OfficialDocumentationEvidence:
    """Parse one exact-shape frozen official-documentation record."""
    _require_fields(
        value,
        {"evidence_id", "source_url", "retrieved_on", "summary"},
        "official documentation evidence",
    )
    return OfficialDocumentationEvidence(
        evidence_id=_required_text(value["evidence_id"], "evidence_id"),
        source_url=_required_text(value["source_url"], "source_url"),
        retrieved_on=_required_text(value["retrieved_on"], "retrieved_on"),
        summary=_required_text(value["summary"], "summary"),
    )


def _parse_human_guidance(value: Mapping[str, object]) -> HumanGuidance:
    """Parse one exact-shape human condition and recommendation."""
    _require_fields(value, {"condition", "recommendation"}, "human guidance")
    return HumanGuidance(
        condition=_required_text(value["condition"], "condition"),
        recommendation=_required_text(value["recommendation"], "recommendation"),
    )


def _parse_auto_learned(value: Mapping[str, object]) -> AutoLearnedCapabilities:
    """Parse learned evidence without normalizing its supplied identity order."""
    _require_fields(
        value,
        {"benchmark_profile", "measurements"},
        "auto-learned source",
    )
    measurements = tuple(
        _parse_measurement(item)
        for item in _mapping_sequence(value["measurements"], "measurements")
    )
    return AutoLearnedCapabilities(
        benchmark_profile=_optional_text(
            value["benchmark_profile"], "benchmark_profile"
        ),
        measurements=measurements,
    )


def _parse_measurement(value: Mapping[str, object]) -> AutoLearnedMeasurement:
    """Parse a finite metric value without imposing an undocumented numeric scale."""
    _require_fields(
        value,
        {"document_class", "metric", "value", "sample_count"},
        "auto-learned measurement",
    )
    numeric = value["value"]
    if isinstance(numeric, bool) or not isinstance(numeric, (int, float)):
        raise ParserCapabilityValidationError("Measurement value must be numeric")
    sample_count = value["sample_count"]
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise ParserCapabilityValidationError("Measurement sample_count must be integer")
    return AutoLearnedMeasurement(
        document_class=_required_text(value["document_class"], "document_class"),
        metric=_required_text(value["metric"], "metric"),
        value=float(numeric),
        sample_count=sample_count,
    )


def _parse_conflict(value: Mapping[str, object]) -> CapabilityConflict:
    """Parse one exact-shape preserved capability/runtime conflict."""
    _require_fields(
        value,
        {"capability", "advertised", "runtime_available", "resolution"},
        "capability conflict",
    )
    return CapabilityConflict(
        capability=_required_text(value["capability"], "conflict capability"),
        advertised=_required_bool(value["advertised"], "advertised"),
        runtime_available=_required_bool(
            value["runtime_available"], "runtime_available"
        ),
        resolution=_required_text(value["resolution"], "resolution"),
    )


def _validate_parser_record(record: ParserCapabilityRecord) -> None:
    """Validate one parser's semantics without collapsing its provenance.

    Aggregate validation calls this before ordering checks so duplicate identities
    and malformed facts receive precise typed failures. The frozen fixture and
    canonical records share every semantic invariant; only their admitted order
    differs.
    """
    _required_text(record.parser_id, "parser_id")
    _required_text(record.version_scope, "version_scope")
    _validate_runtime_probe(record.parser_discovered.runtime_probe)
    documents = record.parser_discovered.official_documentation
    _reject_duplicate_values(
        (item.evidence_id for item in documents), "documentation evidence ID"
    )
    for item in documents:
        _validate_documentation(item)
    assertions = record.parser_discovered.capabilities
    _reject_duplicate_values(
        (item.capability for item in assertions), "capability name"
    )
    for item in assertions:
        _required_text(item.capability, "capability")
        if item.status not in _CAPABILITY_STATUSES:
            raise ParserCapabilityValidationError(
                f"Unsupported capability status: {item.status}"
            )
    for guidance in record.human_guided:
        _required_text(guidance.condition, "condition")
        _required_text(guidance.recommendation, "recommendation")
    learned = record.auto_learned
    if learned.benchmark_profile is not None:
        _required_text(learned.benchmark_profile, "benchmark_profile")
    identities = [
        (item.document_class, item.metric) for item in learned.measurements
    ]
    if len(identities) != len(set(identities)):
        raise ParserCapabilityValidationError(
            f"Duplicate measurement identity for parser: {record.parser_id}"
        )
    for measurement in learned.measurements:
        _validate_measurement(measurement)
    _reject_duplicate_values(
        (item.capability for item in record.conflicts), "conflict capability"
    )
    for conflict in record.conflicts:
        _required_text(conflict.capability, "conflict capability")
        _required_text(conflict.resolution, "conflict resolution")
        if not isinstance(conflict.advertised, bool) or not isinstance(
            conflict.runtime_available, bool
        ):
            raise ParserCapabilityValidationError(
                "Conflict advertised and runtime_available facts must be Boolean"
            )


def _validate_parser_record_order(record: ParserCapabilityRecord) -> None:
    """Require lexical order for every nested identity-bearing collection.

    Aggregate validation invokes this only after all semantic checks and only
    when the complete frozen legacy fingerprint did not match. It compares typed
    tuples without sorting or mutating the caller's registry.
    """
    documents = record.parser_discovered.official_documentation
    if documents != tuple(sorted(documents, key=lambda item: item.evidence_id)):
        raise ParserCapabilityValidationError(
            f"Documentation is not ordered for parser: {record.parser_id}"
        )
    assertions = record.parser_discovered.capabilities
    if assertions != tuple(sorted(assertions, key=lambda item: item.capability)):
        raise ParserCapabilityValidationError(
            f"Capabilities are not ordered for parser: {record.parser_id}"
        )
    if record.human_guided != tuple(
        sorted(record.human_guided, key=_guidance_order)
    ):
        raise ParserCapabilityValidationError(
            f"Human guidance is not ordered for parser: {record.parser_id}"
        )
    measurements = record.auto_learned.measurements
    if measurements != tuple(sorted(measurements, key=_measurement_order)):
        raise ParserCapabilityValidationError(
            f"Measurements are not ordered for parser: {record.parser_id}"
        )
    if record.conflicts != tuple(sorted(record.conflicts, key=_conflict_order)):
        raise ParserCapabilityValidationError(
            f"Conflicts are not ordered for parser: {record.parser_id}"
        )


def _validate_runtime_probe(probe: ParserRuntimeProbe) -> None:
    """Validate runtime facts without converting unknown observations to false."""
    for name, value in (
        ("plugin_registered", probe.plugin_registered),
        ("dependency_importable", probe.dependency_importable),
    ):
        if value is not None and not isinstance(value, bool):
            raise ParserCapabilityValidationError(f"{name} must be Boolean or null")
    for name, value in (
        ("installed_version", probe.installed_version),
        ("adapter_module", probe.adapter_module),
        ("adapter_class", probe.adapter_class),
        ("reason", probe.reason),
    ):
        if value is not None:
            _required_text(value, name)
    if probe.installed_version is not None and any(
        character.isspace() for character in probe.installed_version
    ):
        raise ParserCapabilityValidationError(
            "installed_version must not contain whitespace"
        )


def _validate_documentation(item: OfficialDocumentationEvidence) -> None:
    """Require public HTTP(S) evidence URLs and real ISO retrieval dates."""
    _required_text(item.evidence_id, "evidence_id")
    _required_text(item.summary, "summary")
    source_url = _required_text(item.source_url, "source_url")
    retrieved_on = _required_text(item.retrieved_on, "retrieved_on")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ParserCapabilityValidationError(
            f"Official evidence URL is not public HTTP(S): {item.evidence_id}"
        )
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ParserCapabilityValidationError(
            f"Official evidence URL is local: {item.evidence_id}"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ParserCapabilityValidationError(
            f"Official evidence URL is local: {item.evidence_id}"
        )
    try:
        date.fromisoformat(retrieved_on)
    except ValueError as error:
        raise ParserCapabilityValidationError(
            f"Official evidence date is invalid: {item.evidence_id}"
        ) from error


def _validate_measurement(item: AutoLearnedMeasurement) -> None:
    """Require finite measured values and nonnegative integer sample counts."""
    _required_text(item.document_class, "document_class")
    _required_text(item.metric, "metric")
    if isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
        raise ParserCapabilityValidationError("Measurement value must be numeric")
    if not math.isfinite(item.value):
        raise ParserCapabilityValidationError("Measurement value must be finite")
    if isinstance(item.sample_count, bool) or not isinstance(item.sample_count, int):
        raise ParserCapabilityValidationError(
            "Measurement sample_count must be integer"
        )
    if item.sample_count < 0:
        raise ParserCapabilityValidationError(
            "Measurement sample_count must be nonnegative"
        )


def _parser_record_to_dict(item: ParserCapabilityRecord) -> dict[str, object]:
    """Serialize one parser while keeping all three source classes explicit."""
    return {
        "parser_id": item.parser_id,
        "version_scope": item.version_scope,
        "capability_sources": {
            "parser-discovered": _discovered_to_dict(item.parser_discovered),
            "human-guided": {
                "guidance": [
                    {
                        "condition": guidance.condition,
                        "recommendation": guidance.recommendation,
                    }
                    for guidance in item.human_guided
                ]
            },
            "auto-learned": {
                "benchmark_profile": item.auto_learned.benchmark_profile,
                "measurements": [
                    {
                        "document_class": measurement.document_class,
                        "metric": measurement.metric,
                        "value": measurement.value,
                        "sample_count": measurement.sample_count,
                    }
                    for measurement in item.auto_learned.measurements
                ],
            },
        },
        "conflicts": [
            {
                "capability": conflict.capability,
                "advertised": conflict.advertised,
                "runtime_available": conflict.runtime_available,
                "resolution": conflict.resolution,
            }
            for conflict in item.conflicts
        ],
    }


def _discovered_to_dict(item: ParserDiscoveredCapabilities) -> dict[str, object]:
    """Serialize live runtime separately from frozen official assertions."""
    probe = item.runtime_probe
    capabilities: dict[str, object] = {
        assertion.capability: assertion.status for assertion in item.capabilities
    }
    if probe.runtime_available is not None:
        capabilities["runtime_available"] = probe.runtime_available
    return {
        "runtime_probe": {
            "plugin_registered": probe.plugin_registered,
            "dependency_importable": probe.dependency_importable,
            "installed_version": probe.installed_version,
            "adapter_module": probe.adapter_module,
            "adapter_class": probe.adapter_class,
            "reason": probe.reason,
        },
        "official_documentation": [
            {
                "evidence_id": evidence.evidence_id,
                "source_url": evidence.source_url,
                "retrieved_on": evidence.retrieved_on,
                "summary": evidence.summary,
            }
            for evidence in item.official_documentation
        ],
        "capabilities": capabilities,
    }


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one decoded JSON object while rejecting duplicate keys immediately.

    ``json.loads`` invokes this hook for every nested object. It keeps ordinary
    insertion order, reports only a bounded key identifier, and never includes
    source values or the containing payload in diagnostics. Reusing a field name
    in a different object remains valid because each object has its own call.
    """
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            bounded_key = key[:64]
            raise ParserCapabilityValidationError(
                f"Duplicate JSON object key: {bounded_key}"
            )
        value[key] = item
    return value


def _require_fields(
    value: Mapping[str, object], expected: set[str], context: str
) -> None:
    """Enforce exact object shape so unsupported evidence cannot hide in JSON."""
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ParserCapabilityValidationError(
            f"Invalid {context} fields: expected {sorted(expected)}"
        )


def _mapping(value: object, context: str) -> Mapping[str, object]:
    """Return a string-key mapping or raise a typed trust-boundary error."""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ParserCapabilityValidationError(f"{context} must be an object")
    return value


def _mapping_sequence(value: object, context: str) -> tuple[Mapping[str, object], ...]:
    """Return an immutable mapping sequence without accepting strings or objects."""
    if not isinstance(value, list):
        raise ParserCapabilityValidationError(f"{context} must be an array")
    return tuple(_mapping(item, context) for item in value)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    """Parse an exact ordered string list into an immutable tuple."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ParserCapabilityValidationError(f"{context} must be a string array")
    return tuple(value)


def _required_text(value: object, context: str) -> str:
    """Require non-empty, already-trimmed text at the JSON and object boundary."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ParserCapabilityValidationError(f"{context} must be non-empty text")
    return value


def _optional_text(value: object, context: str) -> str | None:
    """Accept null or delegate non-empty text validation without coercion."""
    if value is None:
        return None
    return _required_text(value, context)


def _required_bool(value: object, context: str) -> bool:
    """Require a real Boolean rather than accepting integer coercion."""
    if not isinstance(value, bool):
        raise ParserCapabilityValidationError(f"{context} must be Boolean")
    return value


def _optional_bool(value: object, context: str) -> bool | None:
    """Accept null or a real Boolean while preserving unknown runtime facts."""
    if value is None:
        return None
    return _required_bool(value, context)


def _reject_duplicate_values(values: Iterable[object], context: str) -> None:
    """Reject duplicate logical identities before indexes could overwrite facts."""
    observed = list(values)
    if len(observed) != len(set(observed)):
        raise ParserCapabilityValidationError(f"Duplicate {context}")


def _guidance_order(item: HumanGuidance) -> tuple[str, str]:
    """Return the canonical human-guidance sort key."""
    return (item.condition, item.recommendation)


def _measurement_order(item: AutoLearnedMeasurement) -> tuple[str, str]:
    """Return the canonical learned-measurement identity order."""
    return (item.document_class, item.metric)


def _conflict_order(item: CapabilityConflict) -> tuple[str, str]:
    """Return the canonical conflict order without erasing resolution detail."""
    return (item.capability, item.resolution)


__all__ = [
    "AutoLearnedCapabilities",
    "AutoLearnedMeasurement",
    "CAPABILITY_SOURCE_CLASSES",
    "CapabilityAssertion",
    "CapabilityConflict",
    "HumanGuidance",
    "OfficialDocumentationEvidence",
    "PARSER_CAPABILITY_REGISTRY_SCHEMA",
    "ParserCapabilityConflictError",
    "ParserCapabilityError",
    "ParserCapabilityNotFoundError",
    "ParserCapabilityRecord",
    "ParserCapabilityRegistry",
    "ParserCapabilityValidationError",
    "ParserDiscoveredCapabilities",
    "ParserRuntimeProbe",
]
