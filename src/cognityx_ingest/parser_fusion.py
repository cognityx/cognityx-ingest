"""Build auditable parser observations, alignment, fusion, and adjudication.

Purpose
-------
Multiple parsers can describe the same document differently. This module keeps
those descriptions as separate observations, determines which source regions
they describe, classifies how their facts relate, and records an explicit
adjudication decision. T04 routing decides which parsers should run; T05 begins
only after existing parser execution has produced ``ExtractionResult`` values.

Design principles
-----------------
Evidence is retained before a compatibility value is selected. Alignment uses
source-native identity, selectors, spans, geometry, or an exact text digest; it
does not use semantic similarity. Fusion distinguishes agreement,
complementary facts, conflict, and unresolved evidence. Confidence is evidence,
not an automatic winner rule. Persisted policies are bounded data rather than
callables or Python expressions. Every public aggregate is immutable and every
serialized collection has a stable logical order.

Alignment aggregates all facts sharing an original source-region ID before it
compares regions across parsers. One parser block's text, block type, box, and
reading order therefore begin in one region even when no second parser exists.
Stronger accepted identity or anchor evidence suppresses weaker ambiguous
geometry candidates for that region; the weaker edge remains as superseded audit
evidence instead of changing the accepted group's state.

Processing flow
---------------
``ParserFusionService`` adapts parser results into observations, aligns them to
source regions, groups facts, applies fact-specific policies, creates one v3.2
fusion artifact, and finally asks the legacy parser layer for an
``ExtractionResult`` compatibility projection. That projection exists because
older callers require one value in fields such as page text or bounding box; it
is not evidence acceptance. A conflict or unresolved decision remains visible
and ineligible for gold support even when the old shape needs a projected value.

The compatibility result carries two deterministic public byte aggregates.
``ParserObservationSet`` becomes durable ``observations.json`` evidence, while
``ParserFusionArtifact`` becomes ``fusion-decisions.json`` and binds the exact
observation bytes with both an observation-set ID and SHA-256. Ingest reloads
and validates both before writing either processing artifact.

Primary consumers
-----------------
``ParserRouter(mode="compare")`` uses the service through a thin wrapper.
``IngestService`` persists the resulting v3.2 artifact. Canonical-content and
audit readers consume observation and decision identities. T06 can later build
segmentation views from canonical IDs and spans without treating one parser's
split or merged blocks as authoritative source text.

Alignment, fusion, and adjudication are intentionally separate. Alignment asks
only whether observations point at the same source region. Fusion then asks
whether fact values agree, conflict, or complement each other. Adjudication is
the final policy step that accepts, rejects, or preserves uncertainty. Keeping
these questions separate lets operators inspect why two parser records were
grouped even when no fact can safely be accepted. It also lets downstream users
distinguish a compatibility field needed by old code from reviewed evidence.

Adjudication policies retain their complete data records in the fusion artifact,
not only their IDs. Exact agreement, complementary retention, conflict
preservation, explicit reviewed values, required review, and segmentation
variant preservation are separate executable strategies. A bounded fact-family
vocabulary supports reviewed defaults, while an exact fact policy takes
precedence. Validation replays these retained policies and rejects changed
strategies, preferred values, resolutions, or arbitrary replacement identities.
For explicit-value policy, preferred values are an ordered reviewed priority:
only the first listed value present in the observations is accepted. No policy
can discard an observation.

Compatibility fact sources retain parser-local source-region and anchor identity
before enrichment. The enrichment stage first uses that exact occurrence
identity and uses value hashing only as a uniquely identifying fallback. If two
observations remain possible, fusion fails rather than selecting an arbitrary
stable ID. This lets canonical fact sources, audit tools, and later T06 and T08
consumers point to the occurrence that actually produced the legacy projection.

The fusion processing activity has exactly three fields: activity ID, canonical
bounding-box threshold, and deterministic method. It contributes to fusion
identity and is separately cross-validated against the observation set. This
prevents recomputing a self-consistent fusion ID around an activity record that
belongs to another observation process.

Missing confidence stays absent. Page records have no native page-level
confidence field, so page observations use ``None`` instead of an invented zero
or one. Existing compatibility metadata can retain historical confidence where
required, but it is not reinterpreted as T05 observed evidence.

Ownership boundary
------------------
Ingest owns parser observations and processing decisions. T01 remains the owner
of parser-native payload storage, T03 owns capability evidence, and T04 owns
routing plans. This module performs no parser execution, provider call, network
request, or LLM call. It references native artifact IDs only when a caller
actually supplies them and never copies parser-native payloads.
``IngestService`` persists observations and decisions directly rather than using
``NativeArtifactStore``. T07 remains the owner of later retention and purge
policy; normal document-prefix deletion is the only cleanup behavior in T05.

Non-goals
---------
T05 does not materialize T06 segmentation views, implement the T08 Source Graph
or provenance-address resolver, generate DataForge records, provide query-time
retrieval, or change SDK and CLI behavior. Split and merged segmentation remains
as conflicting observations. Ambiguous and unresolved facts are never gold
support, and no compatibility projection changes that rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import re
from typing import Mapping, Sequence, TypeAlias

from cognityx_ingest.parser import (
    ExtractedBlock,
    ExtractedObject,
    ExtractedPage,
    ExtractedRelation,
    ExtractedSection,
    ExtractionResult,
    _parser_source_region_id,
)


PARSER_OBSERVATION_SET_SCHEMA = "cognityx.ingest.parser-observation-set/v3.2"
PARSER_FUSION_ARTIFACT_SCHEMA = "cognityx.ingest.parser-fusion/v3.2"
FUSION_STATES = ("agreement", "complementary", "conflict", "unresolved")
EPISTEMIC_STATES = (
    "observed",
    "deterministic",
    "parser-inferred",
    "model-inferred",
    "human-validated",
    "ambiguous",
    "contradicted",
    "unresolved",
)
ALIGNMENT_STATUSES = (
    "exact",
    "accepted-candidate",
    "ambiguous",
    "rejected",
    "superseded",
)
FACT_FAMILIES = (
    "textual",
    "geometry",
    "segmentation",
    "structure",
    "relation",
    "object",
)

_FACT_FAMILY_MEMBERS: Mapping[str, frozenset[str]] = {
    "textual": frozenset({"text", "caption", "object_text", "title", "target_text"}),
    "geometry": frozenset({"bbox", "image_bbox", "width", "height", "page_range"}),
    "segmentation": frozenset({"blocks", "segmentation"}),
    "structure": frozenset({"block_type", "reading_order", "page_label", "printed_page_label"}),
    "relation": frozenset({"source_anchor", "target_anchor", "relation_type", "relation_status"}),
    "object": frozenset({"object_type"}),
}

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_PARSER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FACT_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_UNSAFE_GOLD_STATES = {"ambiguous", "contradicted", "unresolved"}
_PROCESSING_ACTIVITY_KEYS = (
    "activity_id",
    "bbox_iou_threshold",
    "method",
)
_PROCESSING_ACTIVITY_METHOD = "deterministic-parser-fusion"
_PROCESSING_ACTIVITY_FALLBACK_ID = "activity-parser-fusion"


class ParserFusionError(Exception):
    """Base typed failure for the T05 processing boundary.

    Responsibility:
        Give callers one safe category for observation, alignment, fusion,
        adjudication, and compatibility failures.
    Constructed by:
        Public constructors, readers, validators, and service methods.
    Used by:
        Parser composition, persistence, API callers, and audit tooling.
    Main algorithm:
        Subclasses classify a failed stage without exposing parser values.
    Invariants:
        Messages contain field or identity context, never source values or paths.
    Lifecycle/persistence:
        Exceptions are transient and are not serialized into fusion artifacts.
    Side effects:
        Raising the error stops the current T05 operation before persistence.
    Typed failures:
        This is the root of every public T05 failure.
    Trust boundary:
        It replaces raw JSON, key, type, and parser-value exceptions.
    Thread-safety assumptions:
        Exception instances have no shared mutable state.
    """


class ParserObservationValidationError(ParserFusionError):
    """Report an invalid observation value, region, observation, or set.

    Responsibility:
        Reject malformed or untrusted observation input before alignment.
    Constructed by:
        Observation records and strict JSON readers.
    Used by:
        Parser adapters, services, tests, and artifact readers.
    Main algorithm:
        Validate bounded identifiers, finite numbers, hashes, references, and
        deterministic order.
    Invariants:
        The rejected source value is never included in the error text.
    Lifecycle/persistence:
        The exception is not persisted.
    Side effects:
        None beyond aborting validation.
    Typed failures:
        It replaces ``TypeError``, ``KeyError``, and JSON decoder failures.
    Trust boundary:
        All observation JSON is untrusted until these checks pass.
    Thread-safety assumptions:
        Instances are independent and immutable in normal use.
    """


class ParserAlignmentError(ParserFusionError):
    """Report a failure while aligning observations to source regions.

    Responsibility:
        Keep alignment failures distinct from later fact adjudication failures.
    Constructed by:
        The deterministic alignment stage.
    Used by:
        ``ParserFusionService`` callers and diagnostics.
    Main algorithm:
        Reject inconsistent references or invalid geometry before grouping.
    Invariants:
        No semantic or parser-native value is exposed in the message.
    Lifecycle/persistence:
        Failed alignment produces no fusion artifact.
    Side effects:
        None.
    Typed failures:
        Wraps bounded alignment validation problems.
    Trust boundary:
        Alignment evidence is not authoritative until validated.
    Thread-safety assumptions:
        The error carries no shared mutable state.
    """


class ParserFusionValidationError(ParserFusionError):
    """Report an invalid fusion artifact or cross-record reference.

    Responsibility:
        Protect readers from malformed decisions and nondeterministic artifacts.
    Constructed by:
        Fusion records, deserializers, and aggregate validation.
    Used by:
        Persistence, audit readers, tests, and future consumers.
    Main algorithm:
        Check schemas, identities, references, ordering, and state invariants.
    Invariants:
        Messages identify the contract rule without copying evidence values.
    Lifecycle/persistence:
        Invalid artifacts are rejected and never accepted for persistence.
    Side effects:
        None.
    Typed failures:
        Replaces raw JSON and lookup errors at the artifact boundary.
    Trust boundary:
        Persisted bytes remain untrusted until validation succeeds.
    Thread-safety assumptions:
        No mutable shared state is used.
    """


class ParserAdjudicationError(ParserFusionError):
    """Report an invalid or insufficient fact-specific adjudication policy.

    Responsibility:
        Prevent implicit winner selection when a reviewed policy is required.
    Constructed by:
        Policy validation and decision derivation.
    Used by:
        Fusion orchestration and policy authors.
    Main algorithm:
        Enforce bounded strategies and explicit conflict acceptance.
    Invariants:
        Confidence alone never causes this stage to accept a value.
    Lifecycle/persistence:
        A failed policy produces no persisted decision.
    Side effects:
        None.
    Typed failures:
        Identifies policy and adjudication contract failures.
    Trust boundary:
        Persisted policy data is untrusted until validated.
    Thread-safety assumptions:
        No shared mutable policy registry is used.
    """


class ParserFusionCompatibilityError(ParserFusionError):
    """Report inability to create the existing extraction compatibility shape.

    Responsibility:
        Separate compatibility projection failures from evidence decisions.
    Constructed by:
        The final projection stage.
    Used by:
        ``ParserRouter(mode="compare")`` and existing ingest composition.
    Main algorithm:
        Preserve the old result shape, bind each selected fact to one exact
        parser occurrence, and reject ambiguous provenance before attaching the
        authoritative artifact.
    Invariants:
        Projection never changes a conflict or unresolved state into acceptance.
    Lifecycle/persistence:
        No compatibility result is returned after failure.
    Side effects:
        None.
    Typed failures:
        Wraps projection-only contract failures.
    Trust boundary:
        Legacy fields are convenience values, not T05 authority.
    Thread-safety assumptions:
        Projection is pure over immutable input records.
    """


class ParserFusionUnresolvedError(ParserFusionError):
    """Report that a caller required resolution but T05 retained uncertainty.

    Responsibility:
        Let strict consumers refuse unresolved or ambiguous decisions explicitly.
    Constructed by:
        Future strict decision lookup paths.
    Used by:
        Audit, publication, and gold-evidence consumers.
    Main algorithm:
        Preserve uncertainty rather than inventing an accepted observation.
    Invariants:
        It is never raised merely because confidence is absent.
    Lifecycle/persistence:
        Existing unresolved decisions remain in their artifact.
    Side effects:
        None.
    Typed failures:
        A bounded unresolved-state signal.
    Trust boundary:
        Downstream publication must opt into unresolved handling.
    Thread-safety assumptions:
        Instances contain no mutable shared state.
    """


@dataclass(frozen=True, slots=True)
class ObservationValue:
    """Hold one exact JSON-safe parser value in canonical immutable bytes.

    Responsibility:
        Preserve strings exactly while giving every supported value one stable
        JSON representation and SHA-256 identity.
    Constructed by:
        Parser adapters, fixture adapters, and strict deserialization.
    Used by:
        Observations, equality checks, policy matching, and artifact writers.
    Main algorithm:
        Recursively validate JSON types, reject non-finite numbers, sort mapping
        keys, and serialize without insignificant whitespace.
    Invariants:
        ``sha256`` always identifies the exact bytes returned by
        ``to_json_bytes``; mutable caller collections are never retained.
    Lifecycle/persistence:
        Canonical bytes may be embedded as a normal JSON value in an observation.
    Side effects:
        Construction and reads are side-effect free.
    Typed failures:
        Invalid values raise ``ParserObservationValidationError`` without value
        disclosure.
    Trust boundary:
        Caller values and JSON bytes are untrusted until canonicalized.
    Thread-safety assumptions:
        Immutable bytes are safe for concurrent reads.
    """

    _json_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        """Validate direct construction through the same strict public boundary."""
        canonical = _canonical_value_bytes(_strict_json_loads(self._json_bytes, observation=True))
        if canonical != self._json_bytes:
            raise ParserObservationValidationError(
                "Observation value bytes are not in canonical JSON form."
            )

    @classmethod
    def from_value(cls, value: JSONValue) -> "ObservationValue":
        """Canonicalize a parser value without retaining caller-owned collections.

        Parser adapters call this once per fact. The recursive validation and
        deterministic JSON serialization are pure and idempotent; no parser,
        provider, network, or LLM call can occur. Unsupported or non-finite input
        raises ``ParserObservationValidationError`` and has no retention effect.
        """
        return cls(_canonical_value_bytes(value))

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ObservationValue":
        """Read untrusted JSON bytes and return their canonical immutable value.

        Artifact readers call this method. Duplicate keys and non-finite numbers
        are rejected before canonicalization. The operation is deterministic,
        side-effect free, and performs no external calls.
        """
        return cls.from_value(_strict_json_loads(payload, observation=True))

    def to_value(self) -> JSONValue:
        """Return a detached JSON-safe copy for serializers and compatibility code.

        The result may contain new lists or dictionaries, but mutating them cannot
        change this record. Repeated calls return equivalent values in stable key
        order and perform no external calls.
        """
        value = _strict_json_loads(self._json_bytes, observation=True)
        return value  # type: ignore[return-value]

    def to_json_bytes(self) -> bytes:
        """Return the exact deterministic bytes used for hashing and equality."""
        return self._json_bytes

    @property
    def sha256(self) -> str:
        """Return SHA-256 over the exact canonical bytes without side effects."""
        return hashlib.sha256(self._json_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationSourceRegion:
    """Identify where one parser fact came from without copying source text.

    Responsibility:
        Carry source-native location evidence used by alignment.
    Constructed by:
        Parser adapters or validated observation readers.
    Used by:
        Alignment, decision grouping, canonical provenance, and future T06 views.
    Main algorithm:
        Validate IDs, pages, spans, ordered finite geometry, and optional digest.
    Invariants:
        At least one locator exists and no field contains copied source text.
    Lifecycle/persistence:
        The immutable record is serialized inside an observation set.
    Side effects:
        None.
    Typed failures:
        Invalid locators raise ``ParserObservationValidationError``.
    Trust boundary:
        Locators are evidence and do not become authority merely by existing.
    Thread-safety assumptions:
        Scalars and tuples are safe for concurrent reads.
    """

    source_region_id: str
    resource_id: str | None = None
    physical_page_index: int | None = None
    presentation_unit_id: str | None = None
    source_anchor: str | None = None
    selector_ids: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    text_span_sha256: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed location evidence at immutable construction time."""
        _require_id(self.source_region_id, "source_region_id", observation=True)
        for name, value in (
            ("resource_id", self.resource_id),
            ("presentation_unit_id", self.presentation_unit_id),
            ("source_anchor", self.source_anchor),
        ):
            if value is not None:
                _require_id(value, name, observation=True)
        if tuple(sorted(set(self.selector_ids))) != self.selector_ids:
            raise ParserObservationValidationError(
                "selector_ids must be unique and deterministically ordered."
            )
        for selector_id in self.selector_ids:
            _require_id(selector_id, "selector_id", observation=True)
        if self.physical_page_index is not None and self.physical_page_index < 0:
            raise ParserObservationValidationError(
                "physical_page_index must be nonnegative."
            )
        if (self.char_start is None) != (self.char_end is None):
            raise ParserObservationValidationError(
                "Character spans require both char_start and char_end."
            )
        if self.char_start is not None:
            if self.char_start < 0 or self.char_end is None or self.char_end < self.char_start:
                raise ParserObservationValidationError("Character span is invalid.")
        if self.bbox is not None:
            _validate_bbox(self.bbox, ParserObservationValidationError)
        if self.text_span_sha256 is not None and not _is_sha256(self.text_span_sha256):
            raise ParserObservationValidationError("text_span_sha256 must be SHA-256.")

    def to_dict(self) -> dict[str, object]:
        """Serialize location evidence in deterministic field form without text."""
        return {
            "source_region_id": self.source_region_id,
            "resource_id": self.resource_id,
            "physical_page_index": self.physical_page_index,
            "presentation_unit_id": self.presentation_unit_id,
            "source_anchor": self.source_anchor,
            "selector_ids": list(self.selector_ids),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "text_span_sha256": self.text_span_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ObservationSourceRegion":
        """Validate one untrusted region mapping with no external side effects."""
        _require_fields(
            value,
            required={"source_region_id"},
            optional={
                "resource_id", "physical_page_index", "presentation_unit_id",
                "source_anchor", "selector_ids", "char_start", "char_end", "bbox",
                "text_span_sha256",
            },
            observation=True,
        )
        bbox_value = value.get("bbox")
        bbox = None if bbox_value is None else _number_tuple(bbox_value, 4, observation=True)
        return cls(
            source_region_id=_text_field(value, "source_region_id", observation=True),
            resource_id=_optional_text(value.get("resource_id"), "resource_id", observation=True),
            physical_page_index=_optional_int(value.get("physical_page_index"), "physical_page_index", observation=True),
            presentation_unit_id=_optional_text(value.get("presentation_unit_id"), "presentation_unit_id", observation=True),
            source_anchor=_optional_text(value.get("source_anchor"), "source_anchor", observation=True),
            selector_ids=_string_tuple(value.get("selector_ids", ()), "selector_ids", observation=True),
            char_start=_optional_int(value.get("char_start"), "char_start", observation=True),
            char_end=_optional_int(value.get("char_end"), "char_end", observation=True),
            bbox=bbox,  # type: ignore[arg-type]
            text_span_sha256=_optional_text(value.get("text_span_sha256"), "text_span_sha256", observation=True),
        )


@dataclass(frozen=True, slots=True)
class ParserObservation:
    """Record one parser's exact value for one fact at one source region.

    Responsibility:
        Preserve parser identity, source location, value identity, method, and
        epistemic state before any fusion choice.
    Constructed by:
        ``ParserFusionService`` adapters or strict artifact readers.
    Used by:
        Alignment, adjudication, audit tools, and canonical fact provenance.
    Main algorithm:
        Bind a stable ID to parser, region, fact, value hash, and occurrence.
    Invariants:
        ``value_sha256`` matches the exact value bytes and identities are bounded.
    Lifecycle/persistence:
        Observations persist inside document-local ``parser/observations.json``;
        fusion decisions reference their stable IDs rather than copying values.
    Side effects:
        None.
    Typed failures:
        Invalid records raise ``ParserObservationValidationError``.
    Trust boundary:
        The value is evidence, not canonical authoritative text.
    Thread-safety assumptions:
        The frozen record and nested immutable value are safe for concurrent reads.
    """

    observation_id: str
    parser_id: str
    parser_version: str | None
    source_region: ObservationSourceRegion
    fact: str
    value: ObservationValue
    value_sha256: str
    confidence: float | None = None
    method: str = "parser"
    native_artifact_id: str | None = None
    native_pointer: str | None = None
    epistemic_state: str = "observed"
    occurrence_index: int = 1

    def __post_init__(self) -> None:
        """Validate identity, hash, confidence, provenance, and epistemic state."""
        _require_id(self.observation_id, "observation_id", observation=True)
        if not _PARSER_ID_PATTERN.fullmatch(self.parser_id):
            raise ParserObservationValidationError("parser_id is malformed.")
        if self.parser_version is not None:
            _bounded_text(self.parser_version, "parser_version", observation=True)
        if not _FACT_PATTERN.fullmatch(self.fact):
            raise ParserObservationValidationError("fact is malformed.")
        if self.value_sha256 != self.value.sha256:
            raise ParserObservationValidationError("Observation value SHA-256 mismatch.")
        _optional_unit_interval(self.confidence, "confidence", observation=True)
        _bounded_text(self.method, "method", observation=True)
        if self.native_artifact_id is not None:
            _require_id(self.native_artifact_id, "native_artifact_id", observation=True)
        if self.native_pointer is not None:
            _bounded_text(self.native_pointer, "native_pointer", observation=True, limit=1024)
            if self.native_artifact_id is None:
                raise ParserObservationValidationError(
                    "native_pointer requires native_artifact_id."
                )
        if self.epistemic_state not in EPISTEMIC_STATES:
            raise ParserObservationValidationError("epistemic_state is unsupported.")
        if isinstance(self.occurrence_index, bool) or self.occurrence_index < 1:
            raise ParserObservationValidationError("occurrence_index must be positive.")
        if self.observation_id != _parser_observation_id(
            self.parser_id,
            self.parser_version,
            self.source_region,
            self.fact,
            self.value_sha256,
            self.occurrence_index,
        ):
            raise ParserObservationValidationError(
                "observation_id does not match the canonical observation identity."
            )

    @classmethod
    def create(
        cls,
        *,
        parser_id: str,
        parser_version: str | None,
        source_region: ObservationSourceRegion,
        fact: str,
        value: ObservationValue | JSONValue,
        confidence: float | None = None,
        method: str = "parser",
        native_artifact_id: str | None = None,
        native_pointer: str | None = None,
        epistemic_state: str = "observed",
        occurrence_index: int = 1,
    ) -> "ParserObservation":
        """Create a stable observation from parser-adapter facts.

        The adapter supplies identity and source evidence after parser execution.
        This pure method canonicalizes the value, hashes a stable logical payload,
        and returns an immutable record. Input order, network access, providers,
        LLMs, and retention are irrelevant. Invalid input raises
        ``ParserObservationValidationError``.
        """
        wrapped = value if isinstance(value, ObservationValue) else ObservationValue.from_value(value)
        observation_id = _parser_observation_id(
            parser_id,
            parser_version,
            source_region,
            fact,
            wrapped.sha256,
            occurrence_index,
        )
        return cls(
            observation_id=observation_id,
            parser_id=parser_id,
            parser_version=parser_version,
            source_region=source_region,
            fact=fact,
            value=wrapped,
            value_sha256=wrapped.sha256,
            confidence=confidence,
            method=method,
            native_artifact_id=native_artifact_id,
            native_pointer=native_pointer,
            epistemic_state=epistemic_state,
            occurrence_index=occurrence_index,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize the observation deterministically without native payload bytes."""
        return {
            "observation_id": self.observation_id,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "source_region": self.source_region.to_dict(),
            "fact": self.fact,
            "value": self.value.to_value(),
            "value_sha256": self.value_sha256,
            "confidence": self.confidence,
            "method": self.method,
            "native_artifact_id": self.native_artifact_id,
            "native_pointer": self.native_pointer,
            "epistemic_state": self.epistemic_state,
            "occurrence_index": self.occurrence_index,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ParserObservation":
        """Read one observation mapping and reject unsupported fields safely."""
        required = {
            "observation_id", "parser_id", "parser_version", "source_region",
            "fact", "value", "value_sha256", "confidence", "method",
            "native_artifact_id", "native_pointer", "epistemic_state",
            "occurrence_index",
        }
        _require_fields(value, required=required, optional=set(), observation=True)
        region = value["source_region"]
        if not isinstance(region, Mapping):
            raise ParserObservationValidationError("source_region must be an object.")
        return cls(
            observation_id=_text_field(value, "observation_id", observation=True),
            parser_id=_text_field(value, "parser_id", observation=True),
            parser_version=_optional_text(value["parser_version"], "parser_version", observation=True),
            source_region=ObservationSourceRegion.from_dict(region),
            fact=_text_field(value, "fact", observation=True),
            value=ObservationValue.from_value(value["value"]),  # type: ignore[arg-type]
            value_sha256=_text_field(value, "value_sha256", observation=True),
            confidence=_optional_float(value["confidence"], "confidence", observation=True),
            method=_text_field(value, "method", observation=True),
            native_artifact_id=_optional_text(value["native_artifact_id"], "native_artifact_id", observation=True),
            native_pointer=_optional_text(value["native_pointer"], "native_pointer", observation=True),
            epistemic_state=_text_field(value, "epistemic_state", observation=True),
            occurrence_index=_int_field(value, "occurrence_index", observation=True),
        )


@dataclass(frozen=True, slots=True)
class ParserObservationSet:
    """Aggregate validated parser observations for one fusion operation.

    Responsibility:
        Provide one deterministic lookup and serialization boundary before
        alignment.
    Constructed by:
        ``ParserFusionService.build_observation_set`` or strict readers.
    Used by:
        Alignment, fusion, persistence tests, and audit consumers.
    Main algorithm:
        Sort observations by stable ID and verify parser and identity indexes.
    Invariants:
        Schema is exact; IDs and parser/fact/occurrence identities are unique.
    Lifecycle/persistence:
        Exact deterministic bytes persist as ``parser/observations.json``. The
        fusion artifact binds those bytes by set ID and SHA-256.
    Side effects:
        Lookup and serialization are pure.
    Typed failures:
        Invalid sets raise ``ParserObservationValidationError``.
    Trust boundary:
        JSON input is untrusted until ``validate`` succeeds.
    Thread-safety assumptions:
        Frozen tuples support concurrent reads.
    """

    schema: str
    observation_set_id: str
    source_document_id: str | None
    parser_ids: tuple[str, ...]
    observations: tuple[ParserObservation, ...]
    processing_activity_id: str | None = None

    def __post_init__(self) -> None:
        """Validate direct construction so invalid aggregates cannot circulate."""
        self.validate()

    @classmethod
    def create(
        cls,
        observations: Sequence[ParserObservation],
        *,
        source_document_id: str | None = None,
        processing_activity_id: str | None = None,
    ) -> "ParserObservationSet":
        """Build one order-independent immutable observation aggregate.

        Fusion orchestration calls this after adaptation. Sorting and identity
        hashing are deterministic and side-effect free; no external calls occur.
        Invalid observations raise ``ParserObservationValidationError``.
        """
        ordered = tuple(sorted(observations, key=lambda item: item.observation_id))
        parser_ids = tuple(sorted({item.parser_id for item in ordered}))
        return cls(
            schema=PARSER_OBSERVATION_SET_SCHEMA,
            observation_set_id=_parser_observation_set_id(
                source_document_id,
                ordered,
                processing_activity_id,
            ),
            source_document_id=source_document_id,
            parser_ids=parser_ids,
            observations=ordered,
            processing_activity_id=processing_activity_id,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ParserObservationSet":
        """Parse and validate an untrusted observation-set mapping strictly."""
        required = {
            "schema", "observation_set_id", "source_document_id", "parser_ids",
            "observations", "processing_activity_id",
        }
        _require_fields(value, required=required, optional=set(), observation=True)
        raw_observations = value["observations"]
        if not isinstance(raw_observations, list):
            raise ParserObservationValidationError("observations must be an array.")
        observations = []
        for item in raw_observations:
            if not isinstance(item, Mapping):
                raise ParserObservationValidationError("Each observation must be an object.")
            observations.append(ParserObservation.from_dict(item))
        return cls(
            schema=_text_field(value, "schema", observation=True),
            observation_set_id=_text_field(value, "observation_set_id", observation=True),
            source_document_id=_optional_text(value["source_document_id"], "source_document_id", observation=True),
            parser_ids=_string_tuple(value["parser_ids"], "parser_ids", observation=True),
            observations=tuple(observations),
            processing_activity_id=_optional_text(value["processing_activity_id"], "processing_activity_id", observation=True),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ParserObservationSet":
        """Read strict JSON bytes with duplicate-key rejection and no side effects."""
        value = _strict_json_loads(payload, observation=True)
        if not isinstance(value, Mapping):
            raise ParserObservationValidationError("Observation set must be a JSON object.")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic JSON-safe observation-set representation."""
        return {
            "schema": self.schema,
            "observation_set_id": self.observation_set_id,
            "source_document_id": self.source_document_id,
            "parser_ids": list(self.parser_ids),
            "observations": [item.to_dict() for item in self.observations],
            "processing_activity_id": self.processing_activity_id,
        }

    def to_json_bytes(self) -> bytes:
        """Serialize stable bytes without parser, network, provider, or LLM calls."""
        return _canonical_json_bytes(self.to_dict())

    def validate(self) -> None:
        """Validate schema, ordering, uniqueness, parser membership, and identity.

        Constructors and readers call this idempotent operation. It mutates
        nothing and raises ``ParserObservationValidationError`` for malformed
        references or nondeterministic order.
        """
        if self.schema != PARSER_OBSERVATION_SET_SCHEMA:
            raise ParserObservationValidationError("Observation-set schema is unsupported.")
        _require_id(self.observation_set_id, "observation_set_id", observation=True)
        if self.source_document_id is not None:
            _require_id(self.source_document_id, "source_document_id", observation=True)
        if self.processing_activity_id is not None:
            _require_id(self.processing_activity_id, "processing_activity_id", observation=True)
        if tuple(sorted(set(self.parser_ids))) != self.parser_ids:
            raise ParserObservationValidationError("parser_ids must be unique and ordered.")
        if tuple(sorted(self.observations, key=lambda item: item.observation_id)) != self.observations:
            raise ParserObservationValidationError("observations must be deterministically ordered.")
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ParserObservationValidationError("Duplicate observation_id detected.")
        if tuple(sorted({item.parser_id for item in self.observations})) != self.parser_ids:
            raise ParserObservationValidationError("parser_ids do not match observations.")
        expected_id = _parser_observation_set_id(
            self.source_document_id,
            self.observations,
            self.processing_activity_id,
        )
        if self.observation_set_id != expected_id:
            raise ParserObservationValidationError(
                "observation_set_id does not match the canonical set identity."
            )
        identities = [
            (
                item.parser_id,
                item.source_region.source_region_id,
                item.fact,
                item.occurrence_index,
            )
            for item in self.observations
        ]
        if len(identities) != len(set(identities)):
            raise ParserObservationValidationError(
                "Duplicate parser, region, fact, and occurrence identity detected."
            )

    def get(self, observation_id: str) -> ParserObservation:
        """Return one observation by ID or raise a safe typed lookup failure."""
        for item in self.observations:
            if item.observation_id == observation_id:
                return item
        raise ParserObservationValidationError("Observation ID is not present.")

    def observations_for_region(self, source_region_id: str) -> tuple[ParserObservation, ...]:
        """Return region observations in stable ID order without side effects."""
        return tuple(
            item for item in self.observations
            if item.source_region.source_region_id == source_region_id
        )

    def observations_for_fact(self, fact: str) -> tuple[ParserObservation, ...]:
        """Return fact observations in stable ID order without side effects."""
        return tuple(item for item in self.observations if item.fact == fact)


@dataclass(frozen=True, slots=True)
class AlignmentEvidence:
    """Describe why two observations may refer to the same source region.

    Responsibility:
        Preserve exact, candidate, ambiguous, and rejected alignment evidence.
    Constructed by:
        ``ParserFusionService.align``.
    Used by:
        Aligned groups, artifact readers, and audit reviewers.
    Main algorithm:
        Record one region-level matching rule and optional score. Observation
        endpoints are deterministic representatives of the two source regions.
    Invariants:
        IDs are ordered, scores are finite unit values, and status is bounded.
    Lifecycle/persistence:
        Stored in the v3.2 fusion artifact.
    Side effects:
        None.
    Typed failures:
        Invalid evidence raises ``ParserAlignmentError``.
    Trust boundary:
        A score remains evidence rather than authority.
    Thread-safety assumptions:
        Frozen scalar and tuple fields are safe for concurrent reads.
    """

    alignment_id: str
    left_observation_id: str
    right_observation_id: str
    alignment_method: str
    left_source_region_id: str = "region-unknown"
    right_source_region_id: str = "region-unknown"
    alignment_score: float | None = None
    supporting_selector_ids: tuple[str, ...] = ()
    status: str = "rejected"

    def __post_init__(self) -> None:
        """Validate alignment identity, ordering, score, selectors, and status."""
        _require_id(self.alignment_id, "alignment_id", alignment=True)
        _require_id(self.left_observation_id, "left_observation_id", alignment=True)
        _require_id(self.right_observation_id, "right_observation_id", alignment=True)
        _require_id(self.left_source_region_id, "left_source_region_id", alignment=True)
        _require_id(self.right_source_region_id, "right_source_region_id", alignment=True)
        if self.left_observation_id >= self.right_observation_id:
            raise ParserAlignmentError("Alignment observation IDs must be ordered.")
        _bounded_text(self.alignment_method, "alignment_method", alignment=True)
        _optional_unit_interval(self.alignment_score, "alignment_score", alignment=True)
        if tuple(sorted(set(self.supporting_selector_ids))) != self.supporting_selector_ids:
            raise ParserAlignmentError("Alignment selector IDs must be unique and ordered.")
        if self.status not in ALIGNMENT_STATUSES:
            raise ParserAlignmentError("Alignment status is unsupported.")
        expected_id = _alignment_evidence_id(
            self.left_observation_id,
            self.right_observation_id,
            self.left_source_region_id,
            self.right_source_region_id,
            self.alignment_method,
            self.alignment_score,
            self.supporting_selector_ids,
            self.status,
        )
        if self.alignment_id != expected_id:
            raise ParserAlignmentError(
                "alignment_id does not match the canonical evidence identity."
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize alignment evidence in stable JSON-safe form."""
        return {
            "alignment_id": self.alignment_id,
            "left_observation_id": self.left_observation_id,
            "right_observation_id": self.right_observation_id,
            "left_source_region_id": self.left_source_region_id,
            "right_source_region_id": self.right_source_region_id,
            "alignment_method": self.alignment_method,
            "alignment_score": self.alignment_score,
            "supporting_selector_ids": list(self.supporting_selector_ids),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class AlignedObservationGroup:
    """Collect observations joined by accepted source-region evidence.

    Responsibility:
        Give fact fusion one explicit region grouping without semantic merging.
    Constructed by:
        The deterministic alignment stage.
    Used by:
        Fact and region decision derivation.
    Main algorithm:
        Build stable connected components from exact or accepted-candidate edges.
    Invariants:
        Observation and parser IDs are unique and ordered; ambiguous evidence is
        retained rather than greedily connected.
    Lifecycle/persistence:
        Stored in the v3.2 fusion artifact.
    Side effects:
        None.
    Typed failures:
        Invalid groups raise ``ParserAlignmentError``.
    Trust boundary:
        Membership means source alignment, not fact agreement.
    Thread-safety assumptions:
        Frozen tuples are safe for concurrent reads.
    """

    alignment_group_id: str
    source_region_id: str
    source_region_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    parser_ids: tuple[str, ...]
    alignment_evidence_ids: tuple[str, ...]
    alignment_status: str

    def __post_init__(self) -> None:
        """Validate deterministic group identity and bounded alignment status."""
        _require_id(self.alignment_group_id, "alignment_group_id", alignment=True)
        _require_id(self.source_region_id, "source_region_id", alignment=True)
        for label, values in (
            ("observation_ids", self.observation_ids),
            ("source_region_ids", self.source_region_ids),
            ("parser_ids", self.parser_ids),
            ("alignment_evidence_ids", self.alignment_evidence_ids),
        ):
            if tuple(sorted(set(values))) != values:
                raise ParserAlignmentError(f"{label} must be unique and ordered.")
        if not self.observation_ids:
            raise ParserAlignmentError("Aligned group requires an observation.")
        if not self.source_region_ids:
            raise ParserAlignmentError("Aligned group requires a source region.")
        if self.alignment_status not in {"exact", "accepted-candidate", "ambiguous"}:
            raise ParserAlignmentError("Aligned group status is unsupported.")
        expected_id = _aligned_group_id(
            self.source_region_id,
            self.source_region_ids,
            self.observation_ids,
            self.parser_ids,
            self.alignment_evidence_ids,
            self.alignment_status,
        )
        if self.alignment_group_id != expected_id:
            raise ParserAlignmentError(
                "alignment_group_id does not match the canonical group identity."
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize the group without copying any observation value."""
        return {
            "alignment_group_id": self.alignment_group_id,
            "source_region_id": self.source_region_id,
            "source_region_ids": list(self.source_region_ids),
            "observation_ids": list(self.observation_ids),
            "parser_ids": list(self.parser_ids),
            "alignment_evidence_ids": list(self.alignment_evidence_ids),
            "alignment_status": self.alignment_status,
        }


@dataclass(frozen=True, slots=True)
class FactAdjudicationPolicy:
    """Declare a bounded deterministic strategy for one fact or fact family.

    Responsibility:
        Replace implicit global backend precedence with reviewable fact policy.
    Constructed by:
        Ingest composition, reviewed defaults, or strict readers.
    Used by:
        Fact adjudication after alignment and value comparison.
    Main algorithm:
        Match a fact, apply one named strategy, and for explicit preference
        select the first present value in reviewed tuple order while retaining
        all observations.
    Invariants:
        Strategies are bounded data; preferred values are nonempty and hash
        unique only for explicit preference; no executable expression exists.
    Lifecycle/persistence:
        Applied policy records persist inside the fusion artifact so strict
        readers can replay decisions without executable policy code.
    Side effects:
        None.
    Typed failures:
        Invalid policy records raise ``ParserAdjudicationError``.
    Trust boundary:
        Policy values are untrusted until converted to ``ObservationValue``.
    Thread-safety assumptions:
        Frozen policy values are safe for concurrent reads.
    """

    policy_id: str
    fact: str | None = None
    fact_family: str | None = None
    strategy: str = "preserve-conflict"
    preferred_values: tuple[ObservationValue, ...] = ()
    resolution_code: str = "unresolved"
    retain_all_observations: bool = True
    gold_eligible_on_accept: bool = False

    def __post_init__(self) -> None:
        """Validate bounded policy ownership and ordered preference semantics.

        Policy authors and strict readers use the same invariant boundary. The
        algorithm preserves preferred-value tuple order, requires at least one
        hash-unique value for ``prefer-explicit-value``, and rejects preferences
        for strategies where they have no defined effect. Validation is pure and
        raises ``ParserAdjudicationError`` before a policy can reach replay.
        """
        _require_id(self.policy_id, "policy_id", adjudication=True)
        if (self.fact is None) == (self.fact_family is None):
            raise ParserAdjudicationError("Policy requires exactly one fact target.")
        target = self.fact if self.fact is not None else self.fact_family
        if target is None or not _FACT_PATTERN.fullmatch(target):
            raise ParserAdjudicationError("Policy fact target is malformed.")
        if self.fact_family is not None and self.fact_family not in FACT_FAMILIES:
            raise ParserAdjudicationError("Policy fact family is unsupported.")
        if self.strategy not in {
            "exact-agreement", "retain-complementary", "preserve-conflict",
            "prefer-explicit-value", "require-review",
            "preserve-segmentation-variants",
        }:
            raise ParserAdjudicationError("Policy strategy is unsupported.")
        if not all(isinstance(item, ObservationValue) for item in self.preferred_values):
            raise ParserAdjudicationError("preferred_values must be immutable observation values.")
        preferred_hashes = tuple(item.sha256 for item in self.preferred_values)
        if self.strategy == "prefer-explicit-value":
            if not preferred_hashes:
                raise ParserAdjudicationError(
                    "prefer-explicit-value requires at least one preferred value."
                )
            if len(set(preferred_hashes)) != len(preferred_hashes):
                raise ParserAdjudicationError(
                    "preferred_values must have unique canonical SHA-256 values."
                )
        elif preferred_hashes:
            raise ParserAdjudicationError(
                "preferred_values are valid only for prefer-explicit-value."
            )
        if not self.retain_all_observations:
            raise ParserAdjudicationError(
                "v3.2 policies must retain every parser observation."
            )
        _bounded_text(self.resolution_code, "resolution_code", adjudication=True)

    def to_dict(self) -> dict[str, object]:
        """Serialize policy data while preserving reviewed priority order.

        Artifact writers call this pure method after validation. It emits no
        executable behavior, retains ``preferred_values`` in contractual tuple
        order, and returns detached JSON-safe values for strict replay consumers.
        """
        return {
            "policy_id": self.policy_id,
            "fact": self.fact,
            "fact_family": self.fact_family,
            "strategy": self.strategy,
            "preferred_values": [item.to_value() for item in self.preferred_values],
            "resolution_code": self.resolution_code,
            "retain_all_observations": self.retain_all_observations,
            "gold_eligible_on_accept": self.gold_eligible_on_accept,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FactAdjudicationPolicy":
        """Read one bounded data-only policy for persistence replay.

        Fusion artifact readers call this strict constructor. It accepts only the
        declared scalar fields and canonical preferred values, preserving their
        supplied priority order without sorting or deduplication. It performs no
        code evaluation and raises ``ParserAdjudicationError`` for unsupported
        policy ownership or behavior.
        """
        required = {
            "policy_id", "fact", "fact_family", "strategy", "preferred_values",
            "resolution_code", "retain_all_observations", "gold_eligible_on_accept",
        }
        if set(value) != required:
            raise ParserAdjudicationError("Policy fields do not match the required schema.")
        preferred = value["preferred_values"]
        if not isinstance(preferred, list):
            raise ParserAdjudicationError("preferred_values must be an array.")
        retain = value["retain_all_observations"]
        gold = value["gold_eligible_on_accept"]
        if not isinstance(retain, bool) or not isinstance(gold, bool):
            raise ParserAdjudicationError("Policy flags must be booleans.")
        policy_id = value["policy_id"]
        strategy = value["strategy"]
        resolution = value["resolution_code"]
        fact = value["fact"]
        family = value["fact_family"]
        if not all(isinstance(item, str) for item in (policy_id, strategy, resolution)):
            raise ParserAdjudicationError("Policy identifiers must be strings.")
        if fact is not None and not isinstance(fact, str):
            raise ParserAdjudicationError("Policy fact must be null or a string.")
        if family is not None and not isinstance(family, str):
            raise ParserAdjudicationError("Policy fact_family must be null or a string.")
        return cls(
            policy_id=policy_id,
            fact=fact,
            fact_family=family,
            strategy=strategy,
            preferred_values=tuple(ObservationValue.from_value(item) for item in preferred),
            resolution_code=resolution,
            retain_all_observations=retain,
            gold_eligible_on_accept=gold,
        )


@dataclass(frozen=True, slots=True)
class FactFusionDecision:
    """Record one fact's state, accepted evidence, and retained disagreement.

    Responsibility:
        Make agreement, complementarity, conflict, or uncertainty explicit.
    Constructed by:
        Fact fusion and policy adjudication.
    Used by:
        Compatibility projection, canonical provenance, audit, and gold filtering.
    Main algorithm:
        Compare canonical value bytes, apply a fact policy, and retain every
        supporting observation ID.
    Invariants:
        Accepted and rejected IDs never overlap; unresolved has no accepted ID;
        agreement has at least two equivalent observations.
    Lifecycle/persistence:
        Stored in the v3.2 fusion artifact.
    Side effects:
        None.
    Typed failures:
        Invalid decisions raise ``ParserFusionValidationError``.
    Trust boundary:
        ``gold_eligible`` is explicit and never inferred from confidence alone.
    Thread-safety assumptions:
        Frozen tuples support concurrent reads.
    """

    decision_id: str
    source_region_id: str
    fact: str
    state: str
    observation_ids: tuple[str, ...]
    accepted_observation_ids: tuple[str, ...]
    rejected_observation_ids: tuple[str, ...]
    resolution: str
    required_action: str | None = None
    gold_eligible: bool = False
    policy_id: str | None = None

    def __post_init__(self) -> None:
        """Validate ordering, overlap, state-specific acceptance, and policy use."""
        _require_id(self.decision_id, "decision_id", fusion=True)
        _require_id(self.source_region_id, "source_region_id", fusion=True)
        if not _FACT_PATTERN.fullmatch(self.fact):
            raise ParserFusionValidationError("Decision fact is malformed.")
        if self.state not in FUSION_STATES:
            raise ParserFusionValidationError("Fusion state is unsupported.")
        for label, values in (
            ("observation_ids", self.observation_ids),
            ("accepted_observation_ids", self.accepted_observation_ids),
            ("rejected_observation_ids", self.rejected_observation_ids),
        ):
            if tuple(sorted(set(values))) != values:
                raise ParserFusionValidationError(f"{label} must be unique and ordered.")
        if not self.observation_ids:
            raise ParserFusionValidationError("Fact decision requires observations.")
        if not set(self.accepted_observation_ids) <= set(self.observation_ids):
            raise ParserFusionValidationError("Accepted observations are outside the decision.")
        if not set(self.rejected_observation_ids) <= set(self.observation_ids):
            raise ParserFusionValidationError("Rejected observations are outside the decision.")
        if set(self.accepted_observation_ids) & set(self.rejected_observation_ids):
            raise ParserFusionValidationError("Accepted and rejected observations overlap.")
        if set(self.accepted_observation_ids) | set(self.rejected_observation_ids) != set(
            self.observation_ids
        ):
            raise ParserFusionValidationError(
                "Accepted and rejected observations must classify every observation."
            )
        if self.state == "unresolved" and self.accepted_observation_ids:
            raise ParserFusionValidationError("Unresolved decisions cannot accept observations.")
        if self.state == "agreement" and len(self.accepted_observation_ids) < 2:
            raise ParserFusionValidationError("Agreement requires at least two observations.")
        if self.state == "conflict" and self.accepted_observation_ids and self.policy_id is None:
            raise ParserFusionValidationError("Conflict acceptance requires an explicit policy.")
        if self.state == "unresolved" and self.gold_eligible:
            raise ParserFusionValidationError("Unresolved decisions cannot be gold eligible.")
        _bounded_text(self.resolution, "resolution", fusion=True)
        if self.required_action is not None:
            _bounded_text(self.required_action, "required_action", fusion=True)
        if self.policy_id is not None:
            _require_id(self.policy_id, "policy_id", fusion=True)
        expected_id = _fact_decision_id(
            self.source_region_id,
            self.fact,
            self.state,
            self.observation_ids,
            self.accepted_observation_ids,
            self.rejected_observation_ids,
            self.resolution,
            self.required_action,
            self.gold_eligible,
            self.policy_id,
        )
        if self.decision_id != expected_id:
            raise ParserFusionValidationError(
                "decision_id does not match the canonical fact decision identity."
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize the decision by observation references rather than values."""
        return {
            "decision_id": self.decision_id,
            "source_region_id": self.source_region_id,
            "fact": self.fact,
            "state": self.state,
            "observation_ids": list(self.observation_ids),
            "accepted_observation_ids": list(self.accepted_observation_ids),
            "rejected_observation_ids": list(self.rejected_observation_ids),
            "resolution": self.resolution,
            "required_action": self.required_action,
            "gold_eligible": self.gold_eligible,
            "policy_id": self.policy_id,
        }


@dataclass(frozen=True, slots=True)
class RegionFusionDecision:
    """Summarize all aligned fact decisions for one source region.

    Responsibility:
        Give consumers one explicit region state without hiding fact conflicts.
    Constructed by:
        Region state derivation after every fact decision exists.
    Used by:
        Audit summaries, state counts, and downstream eligibility filters.
    Main algorithm:
        Apply unresolved, conflict, complementary, then agreement precedence.
    Invariants:
        IDs and parsers are ordered; unresolved or conflict regions are not gold.
    Lifecycle/persistence:
        Stored in the v3.2 fusion artifact.
    Side effects:
        None.
    Typed failures:
        Invalid summaries raise ``ParserFusionValidationError``.
    Trust boundary:
        This is a summary of decisions, not a replacement for them.
    Thread-safety assumptions:
        Frozen tuples are safe for concurrent reads.
    """

    region_decision_id: str
    source_region_id: str
    alignment_group_ids: tuple[str, ...]
    fact_decision_ids: tuple[str, ...]
    state: str
    source_parsers: tuple[str, ...]
    gold_eligible: bool

    def __post_init__(self) -> None:
        """Validate summary state, identities, ordering, and gold safety."""
        _require_id(self.region_decision_id, "region_decision_id", fusion=True)
        _require_id(self.source_region_id, "source_region_id", fusion=True)
        for label, values in (
            ("alignment_group_ids", self.alignment_group_ids),
            ("fact_decision_ids", self.fact_decision_ids),
            ("source_parsers", self.source_parsers),
        ):
            if tuple(sorted(set(values))) != values:
                raise ParserFusionValidationError(f"{label} must be unique and ordered.")
        if self.state not in FUSION_STATES:
            raise ParserFusionValidationError("Region fusion state is unsupported.")
        if self.state in {"conflict", "unresolved"} and self.gold_eligible:
            raise ParserFusionValidationError("Conflict or unresolved region cannot be gold eligible.")
        expected_id = _region_decision_id(
            self.source_region_id,
            self.alignment_group_ids,
            self.fact_decision_ids,
            self.state,
            self.source_parsers,
            self.gold_eligible,
        )
        if self.region_decision_id != expected_id:
            raise ParserFusionValidationError(
                "region_decision_id does not match the canonical region decision identity."
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize the region summary in deterministic ID order."""
        return {
            "region_decision_id": self.region_decision_id,
            "source_region_id": self.source_region_id,
            "alignment_group_ids": list(self.alignment_group_ids),
            "fact_decision_ids": list(self.fact_decision_ids),
            "state": self.state,
            "source_parsers": list(self.source_parsers),
            "gold_eligible": self.gold_eligible,
        }


@dataclass(frozen=True, slots=True)
class ParserFusionArtifact:
    """Persist the complete auditable T05 decision graph without copied values.

    Responsibility:
        Bind alignment evidence, groups, fact decisions, region summaries, and
        complete replayable policies to the exact bytes of one observation set.
    Constructed by:
        ``ParserFusionService.fuse`` or strict deserialization.
    Used by:
        ``IngestService``, audit readers, canonical provenance, and future T06.
    Main algorithm:
        Validate all cross-record references and serialize stable JSON bytes.
    Invariants:
        Schema and ordering are exact; decisions reference observations rather
        than repeating accepted text; state counts match fact decisions; the
        three-field processing activity is canonical and contributes to fusion
        identity before cross-validation against its observation set.
    Lifecycle/persistence:
        Stored as additive document-local ``fusion-decisions.json`` beside the
        separately durable and SHA-bound ``observations.json`` artifact.
    Side effects:
        Validation, lookups, and serialization are side-effect free.
    Typed failures:
        Invalid bytes or references raise ``ParserFusionValidationError``.
    Trust boundary:
        Persisted JSON is untrusted until ``from_json_bytes`` validates it.
    Thread-safety assumptions:
        The frozen aggregate is safe for concurrent reads.
    """

    schema: str
    fusion_id: str
    observation_set_id: str
    observation_set_sha256: str
    source_backends: tuple[str, ...]
    backend_versions: tuple[tuple[str, str], ...]
    alignment_evidence: tuple[AlignmentEvidence, ...]
    aligned_groups: tuple[AlignedObservationGroup, ...]
    fact_decisions: tuple[FactFusionDecision, ...]
    region_decisions: tuple[RegionFusionDecision, ...]
    adjudication_policies: tuple[FactAdjudicationPolicy, ...]
    policy_ids: tuple[str, ...]
    processing_activity: tuple[tuple[str, str], ...]
    state_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        """Validate direct construction and all cross-record invariants."""
        self.validate()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ParserFusionArtifact":
        """Parse one strict JSON-safe artifact mapping and validate every record.

        Persistence and audit readers call this trust boundary. It converts all
        nested records, wraps invalid retained policy semantics as
        ``ParserFusionValidationError``, and then invokes complete aggregate
        validation. The method performs no parser execution or I/O and returns a
        frozen artifact only when every persisted field is trustworthy.
        """
        required = {
            "schema", "fusion_id", "observation_set_id", "observation_set_sha256", "source_backends",
            "backend_versions", "alignment_evidence", "aligned_groups",
            "fact_decisions", "region_decisions", "adjudication_policies", "policy_ids",
            "processing_activity", "state_counts",
        }
        _require_fields(value, required=required, optional=set(), fusion=True)
        try:
            policies = tuple(
                FactAdjudicationPolicy.from_dict(item)
                for item in _object_array(
                    value["adjudication_policies"], "adjudication_policies"
                )
            )
        except ParserAdjudicationError as exc:
            raise ParserFusionValidationError(
                "Retained adjudication policy semantics are invalid."
            ) from exc
        return cls(
            schema=_text_field(value, "schema", fusion=True),
            fusion_id=_text_field(value, "fusion_id", fusion=True),
            observation_set_id=_text_field(value, "observation_set_id", fusion=True),
            observation_set_sha256=_text_field(value, "observation_set_sha256", fusion=True),
            source_backends=_string_tuple(value["source_backends"], "source_backends", fusion=True),
            backend_versions=_string_mapping_tuple(value["backend_versions"], "backend_versions"),
            alignment_evidence=tuple(
                _alignment_from_dict(item) for item in _object_array(value["alignment_evidence"], "alignment_evidence")
            ),
            aligned_groups=tuple(
                _group_from_dict(item) for item in _object_array(value["aligned_groups"], "aligned_groups")
            ),
            fact_decisions=tuple(
                _fact_decision_from_dict(item) for item in _object_array(value["fact_decisions"], "fact_decisions")
            ),
            region_decisions=tuple(
                _region_decision_from_dict(item) for item in _object_array(value["region_decisions"], "region_decisions")
            ),
            adjudication_policies=policies,
            policy_ids=_string_tuple(value["policy_ids"], "policy_ids", fusion=True),
            processing_activity=_string_mapping_tuple(value["processing_activity"], "processing_activity"),
            state_counts=_count_mapping_tuple(value["state_counts"]),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ParserFusionArtifact":
        """Read duplicate-safe JSON bytes without parser or external calls."""
        value = _strict_json_loads(payload, observation=False)
        if not isinstance(value, Mapping):
            raise ParserFusionValidationError("Fusion artifact must be a JSON object.")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic value-free decision representation."""
        return {
            "schema": self.schema,
            "fusion_id": self.fusion_id,
            "observation_set_id": self.observation_set_id,
            "observation_set_sha256": self.observation_set_sha256,
            "source_backends": list(self.source_backends),
            "backend_versions": dict(self.backend_versions),
            "alignment_evidence": [item.to_dict() for item in self.alignment_evidence],
            "aligned_groups": [item.to_dict() for item in self.aligned_groups],
            "fact_decisions": [item.to_dict() for item in self.fact_decisions],
            "region_decisions": [item.to_dict() for item in self.region_decisions],
            "adjudication_policies": [item.to_dict() for item in self.adjudication_policies],
            "policy_ids": list(self.policy_ids),
            "processing_activity": dict(self.processing_activity),
            "state_counts": dict(self.state_counts),
        }

    def to_json_bytes(self) -> bytes:
        """Return exact stable artifact bytes suitable for immutable persistence."""
        return _canonical_json_bytes(self.to_dict())

    def validate(self) -> None:
        """Validate schema, deterministic ordering, identities, and references.

        Construction and untrusted reads call this idempotent pure method. It
        requires the exact three-field processing-activity shape and canonical
        threshold before recomputing every stable identity and cross-reference.
        It performs no parser, network, provider, or LLM operation and raises
        only ``ParserFusionValidationError`` for aggregate contract failures.
        """
        if self.schema != PARSER_FUSION_ARTIFACT_SCHEMA:
            raise ParserFusionValidationError("Fusion artifact schema is unsupported.")
        _require_id(self.fusion_id, "fusion_id", fusion=True)
        _require_id(self.observation_set_id, "observation_set_id", fusion=True)
        if not _is_sha256(self.observation_set_sha256):
            raise ParserFusionValidationError("observation_set_sha256 must be SHA-256.")
        _validate_ordered_unique(self.source_backends, "source_backends")
        _validate_ordered_unique(self.policy_ids, "policy_ids")
        _validate_pair_order(self.backend_versions, "backend_versions")
        _validate_pair_order(self.processing_activity, "processing_activity")
        if tuple(
            key for key, _value in self.processing_activity
        ) != _PROCESSING_ACTIVITY_KEYS:
            raise ParserFusionValidationError(
                "processing_activity must contain exactly activity_id, "
                "bbox_iou_threshold, and method."
            )
        activity = dict(self.processing_activity)
        _require_id(
            activity["activity_id"],
            "processing_activity.activity_id",
            fusion=True,
        )
        if activity["method"] != _PROCESSING_ACTIVITY_METHOD:
            raise ParserFusionValidationError(
                "Fusion processing method is unsupported."
            )
        _processing_activity_threshold(activity)
        if tuple(name for name, _count in self.state_counts) != FUSION_STATES:
            raise ParserFusionValidationError("state_counts must use contractual state order.")
        if any(isinstance(count, bool) or count < 0 for _name, count in self.state_counts):
            raise ParserFusionValidationError("state_counts values must be nonnegative integers.")
        _validate_record_order(self.alignment_evidence, "alignment_id", "alignment_evidence")
        _validate_record_order(self.aligned_groups, "alignment_group_id", "aligned_groups")
        _validate_record_order(self.fact_decisions, "decision_id", "fact_decisions")
        _validate_record_order(self.region_decisions, "region_decision_id", "region_decisions")
        _validate_record_order(self.adjudication_policies, "policy_id", "adjudication_policies")
        if self.policy_ids != tuple(item.policy_id for item in self.adjudication_policies):
            raise ParserFusionValidationError(
                "policy_ids do not match retained adjudication policies."
            )
        evidence_ids = {item.alignment_id for item in self.alignment_evidence}
        group_ids = {item.alignment_group_id for item in self.aligned_groups}
        decision_ids = {item.decision_id for item in self.fact_decisions}
        grouped_observation_ids = [
            observation_id
            for group in self.aligned_groups
            for observation_id in group.observation_ids
        ]
        if len(grouped_observation_ids) != len(set(grouped_observation_ids)):
            raise ParserFusionValidationError(
                "An observation appears in more than one aligned group."
            )
        grouped_observation_set = set(grouped_observation_ids)
        grouped_source_region_ids = [
            source_region_id
            for group in self.aligned_groups
            for source_region_id in group.source_region_ids
        ]
        if len(grouped_source_region_ids) != len(set(grouped_source_region_ids)):
            raise ParserFusionValidationError(
                "An original source region appears in more than one aligned group."
            )
        for item in self.alignment_evidence:
            if {
                item.left_observation_id,
                item.right_observation_id,
            } - grouped_observation_set:
                raise ParserFusionValidationError(
                    "Alignment evidence references an observation outside every group."
                )
        for group in self.aligned_groups:
            if not set(group.alignment_evidence_ids) <= evidence_ids:
                raise ParserFusionValidationError("Aligned group references missing evidence.")
            for evidence_id in group.alignment_evidence_ids:
                item = next(
                    value for value in self.alignment_evidence
                    if value.alignment_id == evidence_id
                )
                if not {
                    item.left_source_region_id,
                    item.right_source_region_id,
                } & set(group.source_region_ids):
                    raise ParserFusionValidationError(
                        "Aligned group references evidence outside its source regions."
                    )
        if len({item.source_region_id for item in self.region_decisions}) != len(
            self.region_decisions
        ):
            raise ParserFusionValidationError("Duplicate region decision summary detected.")
        if {item.source_region_id for item in self.region_decisions} != {
            item.source_region_id for item in self.aligned_groups
        }:
            raise ParserFusionValidationError(
                "Region decisions must summarize every aligned group exactly once."
            )
        for region in self.region_decisions:
            if not set(region.alignment_group_ids) <= group_ids:
                raise ParserFusionValidationError("Region decision references missing group.")
            if not set(region.fact_decision_ids) <= decision_ids:
                raise ParserFusionValidationError("Region decision references missing fact decision.")
        for decision in self.fact_decisions:
            if decision.policy_id is not None and decision.policy_id not in self.policy_ids:
                raise ParserFusionValidationError("Decision references missing policy.")
            matching_groups = tuple(
                group
                for group in self.aligned_groups
                if group.source_region_id == decision.source_region_id
            )
            if not matching_groups or not set(decision.observation_ids) <= {
                observation_id
                for group in matching_groups
                for observation_id in group.observation_ids
            }:
                raise ParserFusionValidationError(
                    "Decision observations are outside their aligned region."
                )
        try:
            _policy_index(self.adjudication_policies)
        except ParserAdjudicationError as exc:
            raise ParserFusionValidationError(
                "Retained adjudication policy ownership is invalid."
            ) from exc
        expected_counts = tuple(
            (state, sum(item.state == state for item in self.fact_decisions))
            for state in FUSION_STATES
        )
        if self.state_counts != expected_counts:
            raise ParserFusionValidationError("state_counts do not match fact decisions.")
        expected_fusion_id = _parser_fusion_id(
            self.observation_set_id,
            self.observation_set_sha256,
            self.source_backends,
            self.backend_versions,
            self.alignment_evidence,
            self.aligned_groups,
            self.fact_decisions,
            self.region_decisions,
            self.adjudication_policies,
            self.processing_activity,
            self.state_counts,
        )
        if self.fusion_id != expected_fusion_id:
            raise ParserFusionValidationError(
                "fusion_id does not match the canonical fusion identity."
            )

    def validate_against_observation_set(
        self, observation_set: ParserObservationSet
    ) -> None:
        """Replay alignment and policy semantics against exact observation bytes.

        ``IngestService`` and strict readers call this before trusting or writing
        either artifact. Validation binds both the set ID and SHA-256, recomputes
        parser/version indexes, requires the exact three-field processing activity
        ID to equal the observation set ID or documented fallback, reruns
        deterministic region alignment using the canonical persisted threshold,
        reapplies retained data-only policies, and requires every group, fact
        decision, and region summary to match exactly. Fusion-ID recomputation
        cannot bypass the separate activity check. The pure replay performs no
        parser, provider, network, or LLM call.
        """
        self.validate()
        observation_set.validate()
        if self.observation_set_id != observation_set.observation_set_id:
            raise ParserFusionValidationError(
                "Fusion artifact references another observation set."
            )
        observation_sha256 = hashlib.sha256(
            observation_set.to_json_bytes()
        ).hexdigest()
        if self.observation_set_sha256 != observation_sha256:
            raise ParserFusionValidationError(
                "Fusion artifact observation-set SHA-256 does not match."
            )
        if self.source_backends != observation_set.parser_ids:
            raise ParserFusionValidationError(
                "Fusion source_backends do not match the observation set."
            )
        expected_versions = tuple(sorted({
            (item.parser_id, item.parser_version)
            for item in observation_set.observations
            if item.parser_version is not None
        }))
        if self.backend_versions != expected_versions:
            raise ParserFusionValidationError(
                "Fusion backend_versions do not match the observation set."
            )
        expected_ids = {item.observation_id for item in observation_set.observations}
        grouped_ids = {
            observation_id
            for group in self.aligned_groups
            for observation_id in group.observation_ids
        }
        if grouped_ids != expected_ids:
            raise ParserFusionValidationError(
                "Aligned groups do not cover the observation set exactly once."
            )
        index = {item.observation_id: item for item in observation_set.observations}
        region_ids_by_observation = {
            item.observation_id: item.source_region.source_region_id
            for item in observation_set.observations
        }
        for evidence in self.alignment_evidence:
            if evidence.left_observation_id not in index or evidence.right_observation_id not in index:
                raise ParserFusionValidationError(
                    "Alignment evidence references a missing observation."
                )
            if (
                region_ids_by_observation[evidence.left_observation_id]
                != evidence.left_source_region_id
                or region_ids_by_observation[evidence.right_observation_id]
                != evidence.right_source_region_id
            ):
                raise ParserFusionValidationError(
                    "Alignment representative does not belong to its source region."
                )
        for group in self.aligned_groups:
            actual_region_ids = tuple(sorted({
                region_ids_by_observation[item] for item in group.observation_ids
            }))
            actual_parsers = tuple(sorted({index[item].parser_id for item in group.observation_ids}))
            if group.source_region_ids != actual_region_ids:
                raise ParserFusionValidationError(
                    "Aligned group source regions do not match its observations."
                )
            if group.parser_ids != actual_parsers:
                raise ParserFusionValidationError(
                    "Aligned group parser IDs do not match its observations."
                )
        for decision in self.fact_decisions:
            observations = tuple(index[item] for item in decision.observation_ids)
            if any(item.fact != decision.fact for item in observations):
                raise ParserFusionValidationError(
                    "Fact decision references another fact."
                )
            if decision.state == "agreement" and len(
                {item.value_sha256 for item in observations}
            ) != 1:
                raise ParserFusionValidationError(
                    "Agreement observations do not have equivalent values."
                )
            if decision.state == "agreement" and len(
                {item.parser_id for item in observations}
            ) < 2:
                raise ParserFusionValidationError(
                    "Agreement requires observations from distinct parsers."
                )
            if decision.gold_eligible and any(
                item.epistemic_state in _UNSAFE_GOLD_STATES
                for item in observations
            ):
                raise ParserFusionValidationError(
                    "Unsafe observation state cannot be gold eligible."
                )
        activity = dict(self.processing_activity)
        expected_activity_id = (
            observation_set.processing_activity_id
            or _PROCESSING_ACTIVITY_FALLBACK_ID
        )
        if activity["activity_id"] != expected_activity_id:
            raise ParserFusionValidationError(
                "Fusion processing activity does not match the observation set."
            )
        threshold = _processing_activity_threshold(activity)
        expected_evidence, expected_groups = ParserFusionService(
            bbox_iou_threshold=threshold
        ).align(observation_set)
        if self.alignment_evidence != expected_evidence:
            raise ParserFusionValidationError(
                "Alignment evidence does not replay from the observation set."
            )
        if self.aligned_groups != expected_groups:
            raise ParserFusionValidationError(
                "Aligned groups do not replay from the observation set."
            )
        policy_index = _policy_index(self.adjudication_policies)
        expected_fact_decisions = _build_fact_decisions(
            observation_set,
            expected_groups,
            policy_index,
        )
        if self.fact_decisions != expected_fact_decisions:
            raise ParserFusionValidationError(
                "Fact decisions do not replay from retained policies."
            )
        expected_region_decisions = _build_region_decisions(
            observation_set,
            expected_groups,
            expected_fact_decisions,
        )
        if self.region_decisions != expected_region_decisions:
            raise ParserFusionValidationError(
                "Region decisions do not replay from retained policies."
            )

    def decision(self, decision_id: str) -> FactFusionDecision:
        """Return one fact decision or raise a typed missing-reference failure."""
        for item in self.fact_decisions:
            if item.decision_id == decision_id:
                return item
        raise ParserFusionValidationError("Fusion decision ID is not present.")

    def decisions_for_region(self, source_region_id: str) -> tuple[FactFusionDecision, ...]:
        """Return region fact decisions in stable ID order without side effects."""
        return tuple(item for item in self.fact_decisions if item.source_region_id == source_region_id)

    def accepted_observation_ids(self) -> tuple[str, ...]:
        """Return every explicitly accepted observation ID in stable order."""
        return tuple(sorted({
            observation_id
            for decision in self.fact_decisions
            for observation_id in decision.accepted_observation_ids
        }))

    def gold_eligible_decisions(self) -> tuple[FactFusionDecision, ...]:
        """Return only explicitly gold-eligible decisions in stable artifact order."""
        return tuple(item for item in self.fact_decisions if item.gold_eligible)


@dataclass(frozen=True, slots=True)
class FusionOutcome:
    """Return observations, authority artifact, and legacy extraction projection.

    Responsibility:
        Keep the auditable authority and compatibility result together.
    Constructed by:
        ``ParserFusionService.fuse_extraction_results``.
    Used by:
        The parser compatibility wrapper and tests.
    Main algorithm:
        Aggregate three already validated immutable results.
    Invariants:
        The extraction result carries exact bytes for both public aggregates, and
        the fusion artifact binds the observation bytes by ID and SHA-256.
    Lifecycle/persistence:
        ``IngestService`` persists only the relevant result and artifact outputs.
    Side effects:
        None after construction.
    Typed failures:
        Invalid composition raises ``ParserFusionCompatibilityError``.
    Trust boundary:
        The fusion artifact, not the compatibility projection, owns decision state.
    Thread-safety assumptions:
        The frozen aggregate is safe for concurrent reads.
    """

    observation_set: ParserObservationSet
    fusion_artifact: ParserFusionArtifact
    extraction_result: ExtractionResult

    def __post_init__(self) -> None:
        """Ensure compatibility transport carries both exact bound aggregates."""
        observation_bytes = self.observation_set.to_json_bytes()
        if self.extraction_result.observation_artifact != observation_bytes:
            raise ParserFusionCompatibilityError(
                "Compatibility result does not carry the observation-set bytes."
            )
        if self.extraction_result.fusion_artifact != self.fusion_artifact.to_json_bytes():
            raise ParserFusionCompatibilityError(
                "Compatibility result does not carry the fusion artifact bytes."
            )
        self.fusion_artifact.validate_against_observation_set(self.observation_set)


class ParserFusionService:
    """Orchestrate deterministic observation alignment, fusion, and adjudication.

    Responsibility:
        Provide the production T05 seam used after existing parser execution.
    Constructed by:
        The parser compatibility wrapper or direct Python composition.
    Used by:
        ``ParserRouter(mode="compare")``, tests, and future ingest orchestration.
    Main algorithm:
        Adapt results, aggregate same-region facts, align source regions by
        evidence priority, apply replayable bounded policies, summarize regions,
        and create a marked compatibility projection carrying both artifacts.
    Invariants:
        Equivalent inputs in any sequence order produce identical IDs, decisions,
        artifact bytes, and compatibility results.
    Lifecycle/persistence:
        The service is stateless; ``IngestService`` owns persistence.
    Side effects:
        None. It does not execute parsers or call network, providers, or LLMs.
    Typed failures:
        Public stage-specific ``ParserFusionError`` subclasses are raised.
    Trust boundary:
        Parser values are validated as untrusted JSON-safe evidence.
    Thread-safety assumptions:
        Stateless methods and immutable inputs support concurrent calls.
    """

    def __init__(self, *, bbox_iou_threshold: float = 0.8) -> None:
        """Configure the finite unique-mutual-best geometry threshold.

        Composition code constructs the service once. The threshold must be a
        finite unit value. Construction is side-effect free and no external call
        can occur; invalid input raises ``ParserAlignmentError``.
        """
        _optional_unit_interval(bbox_iou_threshold, "bbox_iou_threshold", alignment=True)
        self._bbox_iou_threshold = bbox_iou_threshold

    def build_observation_set(
        self,
        results: Sequence[ExtractionResult],
        *,
        source_document_id: str | None = None,
    ) -> ParserObservationSet:
        """Adapt completed extraction results without losing parser identity.

        The compare wrapper calls this only after parser execution. Results are
        sorted by parser ID, then pages and native record IDs; page, block,
        object, relation, and section facts become immutable observations. No
        parser, network, provider, or LLM call occurs. The operation is
        deterministic and has no retention side effect. Malformed or duplicate
        parser input raises ``ParserObservationValidationError``.
        """
        ordered_results = tuple(sorted(results, key=lambda item: item.backend))
        parser_ids = tuple(item.backend for item in ordered_results)
        if len(parser_ids) != len(set(parser_ids)):
            raise ParserObservationValidationError("Duplicate parser result identity detected.")
        observations: list[ParserObservation] = []
        for result in ordered_results:
            observations.extend(_adapt_result(result, source_document_id))
        activity_seed = {
            "source_document_id": source_document_id,
            "parsers": list(parser_ids),
            "observation_ids": sorted(item.observation_id for item in observations),
        }
        activity_id = f"activity-parser-fusion-{_sha256_json(activity_seed)[:24]}"
        return ParserObservationSet.create(
            observations,
            source_document_id=source_document_id,
            processing_activity_id=activity_id,
        )

    def align(
        self,
        observation_set: ParserObservationSet,
    ) -> tuple[tuple[AlignmentEvidence, ...], tuple[AlignedObservationGroup, ...]]:
        """Align observations using source-native evidence in bounded priority order.

        Fusion callers may inspect this stage independently. Observations first
        aggregate by original source-region ID, including one-parser regions.
        Exact region, anchor, selector, span, unique mutual-best IoU, and exact
        digest evidence are then considered in that order. Stronger accepted
        edges supersede weaker candidates; genuine ambiguity remains visible and
        is not greedily joined. The pure operation is order-independent and performs no
        parser, network, provider, or LLM call. Invalid references raise
        ``ParserAlignmentError`` and nothing is persisted.
        """
        observation_set.validate()
        regions = _build_source_region_aggregates(observation_set.observations)
        evidence = _build_alignment_evidence(regions, self._bbox_iou_threshold)
        groups = _build_alignment_groups(regions, evidence)
        return evidence, groups

    def fuse(
        self,
        observation_set: ParserObservationSet,
        *,
        policies: tuple[FactAdjudicationPolicy, ...] | None = None,
    ) -> ParserFusionArtifact:
        """Create explicit fact and region decisions from validated observations.

        The orchestration layer calls this after adaptation, or callers can pass
        a validated set directly. It invokes the separate alignment stage,
        selects bounded fact-specific policies, compares canonical value bytes,
        preserves disagreement, and emits deterministic artifact bytes. No
        external execution occurs and no data is persisted here. Invalid policy
        or cross-record state raises a typed T05 error.
        """
        alignment_evidence, groups = self.align(observation_set)
        policy_records = tuple(sorted(policies or _default_policies(), key=lambda item: item.policy_id))
        selected_policies = _policy_index(policy_records)
        fact_decisions = _build_fact_decisions(observation_set, groups, selected_policies)
        region_decisions = _build_region_decisions(
            observation_set, groups, fact_decisions
        )
        policy_ids = tuple(sorted({
            item.policy_id for item in fact_decisions if item.policy_id is not None
        }))
        applied_policies = tuple(
            item for item in policy_records if item.policy_id in policy_ids
        )
        source_backends = observation_set.parser_ids
        versions = tuple(sorted({
            (item.parser_id, item.parser_version)
            for item in observation_set.observations
            if item.parser_version is not None
        }))
        observation_bytes = observation_set.to_json_bytes()
        observation_sha256 = hashlib.sha256(observation_bytes).hexdigest()
        activity_id = (
            observation_set.processing_activity_id
            or _PROCESSING_ACTIVITY_FALLBACK_ID
        )
        processing_activity = (
            ("activity_id", activity_id),
            ("bbox_iou_threshold", format(self._bbox_iou_threshold, ".17g")),
            ("method", _PROCESSING_ACTIVITY_METHOD),
        )
        state_counts = tuple(
            (state, sum(item.state == state for item in fact_decisions))
            for state in FUSION_STATES
        )
        fusion_id = _parser_fusion_id(
            observation_set.observation_set_id,
            observation_sha256,
            source_backends,
            versions,  # type: ignore[arg-type]
            alignment_evidence,
            groups,
            fact_decisions,
            region_decisions,
            applied_policies,
            processing_activity,
            state_counts,
        )
        artifact = ParserFusionArtifact(
            schema=PARSER_FUSION_ARTIFACT_SCHEMA,
            fusion_id=fusion_id,
            observation_set_id=observation_set.observation_set_id,
            observation_set_sha256=observation_sha256,
            source_backends=source_backends,
            backend_versions=versions,  # type: ignore[arg-type]
            alignment_evidence=alignment_evidence,
            aligned_groups=groups,
            fact_decisions=fact_decisions,
            region_decisions=region_decisions,
            adjudication_policies=applied_policies,
            policy_ids=policy_ids,
            processing_activity=processing_activity,
            state_counts=state_counts,
        )
        artifact.validate_against_observation_set(observation_set)
        return artifact

    def fuse_extraction_results(
        self,
        results: Sequence[ExtractionResult],
        candidates: tuple[str, ...],
        *,
        policies: tuple[FactAdjudicationPolicy, ...] | None = None,
        source_document_id: str | None = None,
    ) -> FusionOutcome:
        """Run all T05 stages and return an additive legacy-compatible result.

        ``parser._fuse_results`` calls this after existing compare-mode parser
        execution. The method builds observations, aligns, adjudicates, delegates
        only the old field-shape projection to parser compatibility code, and
        attaches exact v3.2 bytes plus audit diagnostics. Input sequence order is
        normalized. No parser, provider, network, or LLM call occurs, and
        persistence remains owned by ``IngestService``. Stage failures use typed
        T05 errors.
        """
        observation_set = self.build_observation_set(
            results, source_document_id=source_document_id
        )
        artifact = self.fuse(observation_set, policies=policies)
        try:
            from cognityx_ingest.parser import _legacy_compatibility_projection

            projected = _legacy_compatibility_projection(results, candidates)
        except ParserFusionError:
            raise
        except Exception as exc:
            raise ParserFusionCompatibilityError(
                "Legacy extraction compatibility projection failed."
            ) from exc
        projected = _enrich_compatibility_fact_sources(
            projected, observation_set, artifact
        )
        summary = {
            "schema": artifact.schema,
            "fusion_id": artifact.fusion_id,
            "observation_set_id": artifact.observation_set_id,
            "state_counts": dict(artifact.state_counts),
            "conflict_count": dict(artifact.state_counts)["conflict"],
            "unresolved_count": dict(artifact.state_counts)["unresolved"],
            "compatibility_projection": True,
            "gold_eligible": False,
            "decisions": [
                {
                    "decision_id": item.decision_id,
                    "source_region_id": item.source_region_id,
                    "fact": item.fact,
                    "adjudication_state": item.state,
                    "supporting_observation_ids": list(item.observation_ids),
                    "accepted_observation_ids": list(item.accepted_observation_ids),
                    "rejected_observation_ids": list(item.rejected_observation_ids),
                    "gold_eligible": item.gold_eligible,
                }
                for item in artifact.fact_decisions
            ],
        }
        diagnostics = dict(projected.diagnostics)
        diagnostics["t05_fusion"] = summary
        extraction_result = replace(
            projected,
            diagnostics=diagnostics,
            observation_artifact=observation_set.to_json_bytes(),
            fusion_artifact=artifact.to_json_bytes(),
        )
        return FusionOutcome(observation_set, artifact, extraction_result)


def _enrich_compatibility_fact_sources(
    projected: ExtractionResult,
    observation_set: ParserObservationSet,
    artifact: ParserFusionArtifact,
) -> ExtractionResult:
    """Attach T05 IDs and state to legacy fact sources without copying values.

    Compatibility projection calls this after adjudication. It matches existing
    selected page and block sources to exact parser-local source regions or
    anchors before considering occurrence and value identity, then adds decision
    metadata. The algorithm does not change selected values or execute external
    work. Unmatched legacy metadata remains intact; ambiguous matches raise a
    typed compatibility error rather than choosing an arbitrary observation.
    """
    decision_by_observation = {
        observation_id: decision
        for decision in artifact.fact_decisions
        for observation_id in decision.observation_ids
    }
    pages = tuple(
        _enrich_compatibility_page(
            page, observation_set.observations, decision_by_observation
        )
        for page in projected.pages
    )
    return replace(projected, pages=pages)


def _enrich_compatibility_page(
    page: ExtractedPage,
    observations: tuple[ParserObservation, ...],
    decision_by_observation: Mapping[str, FactFusionDecision],
) -> ExtractedPage:
    """Enrich one projected page and its blocks without changing legacy values.

    Compatibility projection calls this for each physical page. It delegates
    exact occurrence lookup for page and block fact sources, returns immutable
    replacements, and leaves object, relation, and selected field values intact.
    Ambiguity propagates as ``ParserFusionCompatibilityError`` before persistence.
    """
    page_sources = {
        fact: _enrich_source_details(
            sources,
            fact,
            getattr(page, fact),
            page.physical_page_index,
            observations,
            decision_by_observation,
        )
        for fact, sources in page.fact_sources.items()
    }
    blocks = tuple(
        replace(
            block,
            fact_sources={
                fact: _enrich_source_details(
                    sources,
                    fact,
                    getattr(block, fact),
                    page.physical_page_index,
                    observations,
                    decision_by_observation,
                    bbox=block.bbox,
                )
                for fact, sources in block.fact_sources.items()
            },
        )
        for block in page.blocks
    )
    return replace(page, fact_sources=page_sources, blocks=blocks)


def _enrich_source_details(
    sources: tuple[Mapping[str, object], ...],
    fact: str,
    selected_value: object,
    page_index: int,
    observations: tuple[ParserObservation, ...],
    decision_by_observation: Mapping[str, FactFusionDecision],
    *,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Bind selected legacy sources to one exact T05 observation occurrence.

    The compatibility layer calls this after it has selected an unchanged legacy
    value. For each parser source, the algorithm prefers exact source-region ID,
    then parser-local anchor, then page/bbox/occurrence identity. The historical
    parser/fact/value/page/bbox match is allowed only when it leaves one candidate.
    Zero matches preserve the original source; multiple matches raise
    ``ParserFusionCompatibilityError``. The result adds only IDs and decision
    state, never source text, for canonical, audit, T06, and T08 consumers.
    """
    try:
        selected_hash = ObservationValue.from_value(selected_value).sha256  # type: ignore[arg-type]
    except ParserObservationValidationError:
        return sources
    enriched: list[Mapping[str, object]] = []
    for source in sources:
        observation = _select_compatibility_observation(
            source,
            fact,
            selected_hash,
            page_index,
            bbox,
            observations,
        )
        if observation is None:
            enriched.append(source)
            continue
        decision = decision_by_observation[observation.observation_id]
        accepted = observation.observation_id in decision.accepted_observation_ids
        rejected = observation.observation_id in decision.rejected_observation_ids
        enriched.append(
            {
                **source,
                "observation_id": observation.observation_id,
                "decision_id": decision.decision_id,
                "adjudication_state": decision.state,
                "accepted": accepted,
                "rejected": rejected,
                "compatibility_projection": (
                    decision.state in {"conflict", "unresolved"} and not accepted
                ),
                "gold_eligible": decision.gold_eligible and accepted,
            }
        )
    return tuple(enriched)


def _select_compatibility_observation(
    source: Mapping[str, object],
    fact: str,
    selected_hash: str,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
    observations: tuple[ParserObservation, ...],
) -> ParserObservation | None:
    """Select one parser observation using strongest available occurrence identity.

    ``_enrich_source_details`` calls this pure matcher for one compatibility
    source. It progressively narrows parser/fact/value candidates by exact region,
    anchor, and occurrence-aware page/geometry. A legacy value-only fallback is
    accepted only when unique. Malformed identity metadata or any remaining
    ambiguity raises ``ParserFusionCompatibilityError`` without exposing values.
    """
    parser_id = source.get("backend") or source.get("parser_id")
    candidates = tuple(
        item
        for item in observations
        if item.parser_id == parser_id
        and item.fact == fact
        and item.value_sha256 == selected_hash
    )

    source_region_id = source.get("source_region_id")
    if source_region_id is not None:
        if not isinstance(source_region_id, str):
            raise ParserFusionCompatibilityError(
                "Compatibility source_region_id must be a string."
            )
        candidates = tuple(
            item
            for item in candidates
            if item.source_region.source_region_id == source_region_id
        )
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

    source_anchor = source.get("source_anchor")
    if source_anchor is not None:
        if not isinstance(source_anchor, str):
            raise ParserFusionCompatibilityError(
                "Compatibility source_anchor must be a string."
            )
        candidates = tuple(
            item
            for item in candidates
            if item.source_region.source_anchor == source_anchor
        )
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

    occurrence_index = source.get("occurrence_index")
    if occurrence_index is not None and (
        isinstance(occurrence_index, bool)
        or not isinstance(occurrence_index, int)
        or occurrence_index < 1
    ):
        raise ParserFusionCompatibilityError(
            "Compatibility occurrence_index must be a positive integer."
        )
    identity_candidates = tuple(
        item
        for item in candidates
        if item.source_region.physical_page_index == page_index
        and (
            bbox is None
            or item.source_region.bbox is None
            or item.source_region.bbox == bbox
        )
        and (
            occurrence_index is None
            or item.occurrence_index == occurrence_index
        )
    )
    if len(identity_candidates) == 1:
        return identity_candidates[0]
    if len(identity_candidates) > 1:
        raise ParserFusionCompatibilityError(
            "Compatibility source matches multiple parser observations."
        )
    if occurrence_index is not None:
        return None

    fallback_candidates = tuple(
        item
        for item in observations
        if item.parser_id == parser_id
        and item.fact == fact
        and item.value_sha256 == selected_hash
        and item.source_region.physical_page_index == page_index
        and (
            bbox is None
            or item.source_region.bbox is None
            or item.source_region.bbox == bbox
        )
    )
    if len(fallback_candidates) == 1:
        return fallback_candidates[0]
    if len(fallback_candidates) > 1:
        raise ParserFusionCompatibilityError(
            "Compatibility value fallback matches multiple parser observations."
        )
    return None


def _canonical_value_bytes(value: object) -> bytes:
    """Canonicalize JSON evidence while retaining exact strings and no mutables."""
    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ParserObservationValidationError("Observation value is not JSON-safe.") from exc


def _validate_json_value(value: object) -> None:
    """Reject non-JSON, non-finite, non-string-key, or mutable custom values safely."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ParserObservationValidationError("Observation number must be finite.")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ParserObservationValidationError("Observation object keys must be strings.")
            _bounded_text(key, "observation object key", observation=True, allow_empty=True)
            _validate_json_value(item)
        return
    raise ParserObservationValidationError("Observation value uses an unsupported type.")


def _strict_json_loads(payload: bytes, *, observation: bool) -> object:
    """Decode strict UTF-8 JSON, rejecting duplicate keys and non-finite constants."""
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Build one object only when every untrusted key is unique."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise error_type("Duplicate JSON object key detected.")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        """Reject JSON extensions for NaN and infinity without exposing values."""
        raise error_type("JSON numbers must be finite.")

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ParserFusionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise error_type("Payload is not valid strict UTF-8 JSON.") from exc


def _canonical_json_bytes(value: object) -> bytes:
    """Serialize validated contract records with stable keys and compact spacing."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ParserFusionValidationError("Fusion record is not JSON-safe.") from exc


def _processing_activity_threshold(activity: Mapping[str, str]) -> float:
    """Validate and return the exact canonical fusion alignment threshold.

    Artifact construction and replay share this pure boundary. It accepts only a
    finite unit-interval number whose text is exactly Python's stable ``.17g``
    representation, preventing equivalent-looking but noncanonical activity
    records from acquiring distinct fusion identities. Invalid persisted data
    raises ``ParserFusionValidationError`` before alignment replay.
    """
    try:
        raw_threshold = activity["bbox_iou_threshold"]
        threshold = float(raw_threshold)
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserFusionValidationError(
            "Fusion bbox threshold is missing or invalid."
        ) from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ParserFusionValidationError(
            "Fusion bbox threshold is out of range."
        )
    if raw_threshold != format(threshold, ".17g"):
        raise ParserFusionValidationError(
            "Fusion bbox threshold is not in canonical form."
        )
    return threshold


def _sha256_json(value: object) -> str:
    """Hash stable JSON identities so IDs never depend on caller sequence order."""
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _parser_observation_id(
    parser_id: str,
    parser_version: str | None,
    source_region: ObservationSourceRegion,
    fact: str,
    value_sha256: str,
    occurrence_index: int,
) -> str:
    """Recompute one observation ID from its complete canonical identity."""
    identity = {
        "parser_id": parser_id,
        "parser_version": parser_version,
        "source_region": source_region.to_dict(),
        "fact": fact,
        "value_sha256": value_sha256,
        "occurrence_index": occurrence_index,
    }
    return f"obs-{_sha256_json(identity)[:32]}"


def _parser_observation_set_id(
    source_document_id: str | None,
    observations: tuple[ParserObservation, ...],
    processing_activity_id: str | None,
) -> str:
    """Recompute the set ID from ordered observations and processing context."""
    identity = {
        "source_document_id": source_document_id,
        "observation_ids": [item.observation_id for item in observations],
        "processing_activity_id": processing_activity_id,
    }
    return f"obset-{_sha256_json(identity)[:32]}"


def _alignment_evidence_id(
    left_observation_id: str,
    right_observation_id: str,
    left_source_region_id: str,
    right_source_region_id: str,
    method: str,
    score: float | None,
    selector_ids: tuple[str, ...],
    status: str,
) -> str:
    """Recompute a region edge ID including evidence and disposition."""
    identity = {
        "left_observation_id": left_observation_id,
        "right_observation_id": right_observation_id,
        "left_source_region_id": left_source_region_id,
        "right_source_region_id": right_source_region_id,
        "method": method,
        "score": score,
        "selector_ids": list(selector_ids),
        "status": status,
    }
    return f"align-{_sha256_json(identity)[:32]}"


def _aligned_group_id(
    source_region_id: str,
    source_region_ids: tuple[str, ...],
    observation_ids: tuple[str, ...],
    parser_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
    status: str,
) -> str:
    """Recompute one final region-group ID from all public membership fields."""
    identity = {
        "source_region_id": source_region_id,
        "source_region_ids": list(source_region_ids),
        "observation_ids": list(observation_ids),
        "parser_ids": list(parser_ids),
        "alignment_evidence_ids": list(evidence_ids),
        "alignment_status": status,
    }
    return f"group-{_sha256_json(identity)[:32]}"


def _fact_decision_id(
    source_region_id: str,
    fact: str,
    state: str,
    observation_ids: tuple[str, ...],
    accepted_ids: tuple[str, ...],
    rejected_ids: tuple[str, ...],
    resolution: str,
    required_action: str | None,
    gold_eligible: bool,
    policy_id: str | None,
) -> str:
    """Recompute one adjudication decision ID from its full public meaning."""
    identity = {
        "source_region_id": source_region_id,
        "fact": fact,
        "state": state,
        "observation_ids": list(observation_ids),
        "accepted_observation_ids": list(accepted_ids),
        "rejected_observation_ids": list(rejected_ids),
        "resolution": resolution,
        "required_action": required_action,
        "gold_eligible": gold_eligible,
        "policy_id": policy_id,
    }
    return f"decision-{_sha256_json(identity)[:32]}"


def _region_decision_id(
    source_region_id: str,
    alignment_group_ids: tuple[str, ...],
    fact_decision_ids: tuple[str, ...],
    state: str,
    source_parsers: tuple[str, ...],
    gold_eligible: bool,
) -> str:
    """Recompute one region summary ID from groups, facts, and state."""
    identity = {
        "source_region_id": source_region_id,
        "alignment_group_ids": list(alignment_group_ids),
        "fact_decision_ids": list(fact_decision_ids),
        "state": state,
        "source_parsers": list(source_parsers),
        "gold_eligible": gold_eligible,
    }
    return f"region-decision-{_sha256_json(identity)[:32]}"


def _parser_fusion_id(
    observation_set_id: str,
    observation_set_sha256: str,
    source_backends: tuple[str, ...],
    backend_versions: tuple[tuple[str, str], ...],
    alignment_evidence: tuple[AlignmentEvidence, ...],
    aligned_groups: tuple[AlignedObservationGroup, ...],
    fact_decisions: tuple[FactFusionDecision, ...],
    region_decisions: tuple[RegionFusionDecision, ...],
    policies: tuple[FactAdjudicationPolicy, ...],
    processing_activity: tuple[tuple[str, str], ...],
    state_counts: tuple[tuple[str, int], ...],
) -> str:
    """Recompute the fusion ID from every persisted semantic component."""
    identity = {
        "observation_set_id": observation_set_id,
        "observation_set_sha256": observation_set_sha256,
        "source_backends": list(source_backends),
        "backend_versions": dict(backend_versions),
        "alignment_evidence": [item.to_dict() for item in alignment_evidence],
        "aligned_groups": [item.to_dict() for item in aligned_groups],
        "fact_decisions": [item.to_dict() for item in fact_decisions],
        "region_decisions": [item.to_dict() for item in region_decisions],
        "adjudication_policies": [item.to_dict() for item in policies],
        "processing_activity": dict(processing_activity),
        "state_counts": dict(state_counts),
    }
    return f"fusion-{_sha256_json(identity)[:32]}"


def _adapt_result(result: ExtractionResult, source_document_id: str | None) -> tuple[ParserObservation, ...]:
    """Adapt one parser result into stable page, block, object, relation, and section facts."""
    if not _PARSER_ID_PATTERN.fullmatch(result.backend):
        raise ParserObservationValidationError("Parser result backend is malformed.")
    observations: list[ParserObservation] = []
    resource_id = source_document_id
    pages = sorted(result.pages, key=lambda item: (item.physical_page_index, item.page_number))
    for page in pages:
        page_index = page.physical_page_index
        page_region = ObservationSourceRegion(
            source_region_id=f"page:{page_index}",
            resource_id=resource_id,
            physical_page_index=page_index,
            presentation_unit_id=f"page:{page_index}",
        )
        for fact, value in (
            ("text", page.text),
            ("page_label", page.page_label),
            ("printed_page_label", page.printed_page_label),
            ("width", page.width),
            ("height", page.height),
        ):
            if value is not None and value != "":
                observations.append(
                    _make_observation(
                        result,
                        page_region,
                        fact,
                        value,
                        "parser_page_fact",
                        None,
                    )
                )
        for block in sorted(page.blocks, key=lambda item: (item.block_id, item.reading_order)):
            region = _block_region(result.backend, page_index, block, resource_id)
            for fact, value in (
                ("text", block.text), ("block_type", block.block_type),
                ("bbox", block.bbox), ("reading_order", block.reading_order),
            ):
                if value is not None:
                    observations.append(_make_observation(result, region, fact, value, block.method, block.confidence))
        for item in sorted(page.objects, key=lambda value: value.object_id):
            region = _object_region(result.backend, page_index, item, resource_id)
            for fact, value in (
                ("object_type", item.object_type), ("caption", item.caption),
                ("object_text", item.text), ("bbox", item.bbox),
            ):
                if value is not None and value != "":
                    observations.append(_make_observation(result, region, fact, value, item.method, item.confidence))
        for item in sorted(page.relations, key=lambda value: value.relation_id):
            region = ObservationSourceRegion(
                source_region_id=f"relation:{result.backend}:{item.relation_id}",
                resource_id=resource_id,
                physical_page_index=page_index,
                source_anchor=item.source_anchor,
                bbox=item.bbox,
            )
            for fact, value in (
                ("source_anchor", item.source_anchor), ("target_anchor", item.target_anchor),
                ("relation_type", item.relation_type), ("target_text", item.target_text),
                ("relation_status", item.status),
            ):
                if value is not None and value != "":
                    state = item.status if item.status in EPISTEMIC_STATES else "observed"
                    observations.append(_make_observation(result, region, fact, value, item.method, item.confidence, state))
    for section in sorted(result.sections, key=lambda item: item.section_id):
        region = ObservationSourceRegion(
            source_region_id=f"section:{result.backend}:{section.section_id}",
            resource_id=resource_id,
            physical_page_index=section.start_page_index,
            source_anchor=section.section_id,
        )
        for fact, value in (
            ("title", section.title),
            ("page_range", [section.start_page_index, section.end_page_index]),
        ):
            observations.append(_make_observation(result, region, fact, value, section.method, section.confidence))
    return tuple(observations)


def _make_observation(
    result: ExtractionResult,
    region: ObservationSourceRegion,
    fact: str,
    value: object,
    method: str,
    confidence: float | None,
    epistemic_state: str = "observed",
) -> ParserObservation:
    """Construct one adapter observation without fabricating native artifact links."""
    return ParserObservation.create(
        parser_id=result.backend,
        parser_version=result.backend_version,
        source_region=region,
        fact=fact,
        value=value,  # type: ignore[arg-type]
        confidence=confidence,
        method=method,
        epistemic_state=epistemic_state,
    )


def _block_region(
    parser_id: str,
    page_index: int,
    block: ExtractedBlock,
    resource_id: str | None,
) -> ObservationSourceRegion:
    """Build block location evidence shared with compatibility projection.

    The extraction-result adapter calls this for every parser block. It uses the
    parser module's bounded region-ID algorithm so compatibility metadata names
    the same occurrence, retains the parser block ID as an anchor, and stores
    only a text digest rather than copied text. Alignment and canonical audit
    consumers use the resulting immutable region; T06 view creation remains out
    of scope.
    """
    digest = hashlib.sha256(block.text.encode("utf-8")).hexdigest() if block.text else None
    return ObservationSourceRegion(
        source_region_id=_parser_source_region_id(
            "block", parser_id, page_index, block.block_id
        ),
        resource_id=resource_id,
        physical_page_index=page_index,
        source_anchor=block.block_id,
        bbox=block.bbox,
        text_span_sha256=digest,
    )


def _object_region(
    parser_id: str,
    page_index: int,
    item: ExtractedObject,
    resource_id: str | None,
) -> ObservationSourceRegion:
    """Build object location evidence shared with compatibility projection.

    The adapter calls this for each normalized object. It binds the parser-local
    object ID to the same bounded source-region identity emitted by legacy
    compatibility metadata, while retaining optional page geometry. T05
    alignment and audit consumers use the locator without copying caption or
    object text; T08 graph materialization remains a later responsibility.
    """
    return ObservationSourceRegion(
        source_region_id=_parser_source_region_id(
            "object", parser_id, page_index, item.object_id
        ),
        resource_id=resource_id,
        physical_page_index=page_index,
        source_anchor=item.object_id,
        bbox=item.bbox,
    )

@dataclass(frozen=True, slots=True)
class _SourceRegionAggregate:
    """Hold every fact emitted for one original parser source region.

    Why it exists:
        Alignment is about source regions, not individual fact values. Grouping
        first prevents text, type, geometry, and reading-order facts from one
        block from competing as separate geometry candidates.
    Core algorithm:
        Collect observations sharing ``source_region_id``, verify that their
        non-null location evidence does not contradict, and expose one stable
        representative observation for compatibility alignment endpoints.
    Design principle:
        Preserve every observation while comparing location only once per region.
    Used by:
        The private deterministic alignment stage. It is never persisted as a
        third artifact or exposed as a T06 segmentation API.
    """

    source_region_id: str
    source_region: ObservationSourceRegion
    observation_ids: tuple[str, ...]
    parser_ids: tuple[str, ...]
    representative_observation_id: str
    representative_observation_ids: tuple[str, ...]
    text_digest_occurrences: tuple[tuple[str, int], ...]


def _build_source_region_aggregates(
    observations: tuple[ParserObservation, ...],
) -> tuple[_SourceRegionAggregate, ...]:
    """Aggregate same-region facts and reject contradictory location evidence.

    ``ParserFusionService.align`` calls this before cross-parser matching. The
    operation groups even a single parser's facts, validates all location fields,
    and returns deterministic immutable aggregates without copying source text.
    """
    grouped: dict[str, list[ParserObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.source_region.source_region_id, []).append(
            observation
        )
    aggregates: list[_SourceRegionAggregate] = []
    for source_region_id, values in sorted(grouped.items()):
        ordered = tuple(sorted(values, key=lambda item: item.observation_id))
        source_region = _compatible_region_location(source_region_id, ordered)
        text_occurrences = tuple(sorted({
            (_normalized_text_digest(value), item.occurrence_index)
            for item in ordered
            if item.fact in {"text", "blocks", "segmentation"}
            and isinstance((value := item.value.to_value()), str)
        }))
        aggregates.append(
            _SourceRegionAggregate(
                source_region_id=source_region_id,
                source_region=source_region,
                observation_ids=tuple(item.observation_id for item in ordered),
                parser_ids=tuple(sorted({item.parser_id for item in ordered})),
                representative_observation_id=ordered[0].observation_id,
                representative_observation_ids=tuple(
                    min(
                        item.observation_id
                        for item in ordered
                        if item.parser_id == parser_id
                    )
                    for parser_id in sorted({item.parser_id for item in ordered})
                ),
                text_digest_occurrences=text_occurrences,
            )
        )
    return tuple(aggregates)


def _compatible_region_location(
    source_region_id: str,
    observations: tuple[ParserObservation, ...],
) -> ObservationSourceRegion:
    """Merge non-conflicting location fields for one original source region.

    Parser adapters may repeat the same region record for several facts. A field
    may be absent on some facts, but two different non-null values would make the
    region unsafe to align and therefore raise ``ParserAlignmentError``.
    """
    def one_value(name: str) -> object:
        """Return one non-null location value or reject contradictory evidence."""
        found = {
            getattr(item.source_region, name)
            for item in observations
            if getattr(item.source_region, name) is not None
        }
        if len(found) > 1:
            raise ParserAlignmentError(
                f"Source-region observations disagree about {name}."
            )
        return next(iter(found), None)

    selector_ids = tuple(sorted({
        selector
        for item in observations
        for selector in item.source_region.selector_ids
    }))
    return ObservationSourceRegion(
        source_region_id=source_region_id,
        resource_id=one_value("resource_id"),  # type: ignore[arg-type]
        physical_page_index=one_value("physical_page_index"),  # type: ignore[arg-type]
        presentation_unit_id=one_value("presentation_unit_id"),  # type: ignore[arg-type]
        source_anchor=one_value("source_anchor"),  # type: ignore[arg-type]
        selector_ids=selector_ids,
        char_start=one_value("char_start"),  # type: ignore[arg-type]
        char_end=one_value("char_end"),  # type: ignore[arg-type]
        bbox=one_value("bbox"),  # type: ignore[arg-type]
        text_span_sha256=one_value("text_span_sha256"),  # type: ignore[arg-type]
    )


def _build_alignment_evidence(
    regions: tuple[_SourceRegionAggregate, ...], threshold: float
) -> tuple[AlignmentEvidence, ...]:
    """Compare region aggregates once and enforce stronger-evidence precedence.

    Exact anchor, selector, and span matches are accepted before geometry. Any
    lower-priority bbox candidate touching an exactly matched region is retained
    as ``superseded`` audit evidence and cannot make that accepted region
    ambiguous. Remaining geometry uses unique mutual-best IoU; exact normalized
    text plus occurrence is considered only when stronger location is absent.
    """
    exact: list[AlignmentEvidence] = [
        _internal_region_alignment_record(region)
        for region in regions
        if len(region.representative_observation_ids) >= 2
    ]
    bbox_candidates: list[tuple[_SourceRegionAggregate, _SourceRegionAggregate]] = []
    digest: list[AlignmentEvidence] = []
    for index, left in enumerate(regions):
        for right in regions[index + 1:]:
            if set(left.parser_ids) & set(right.parser_ids):
                continue
            direct = _direct_region_alignment(left, right)
            if direct is not None:
                method, selectors = direct
                exact.append(
                    _alignment_record(left, right, method, 1.0, selectors, "exact")
                )
            elif (
                _same_region_page(left, right)
                and left.source_region.bbox is not None
                and right.source_region.bbox is not None
            ):
                bbox_candidates.append((left, right))
            elif _digest_region_alignment(left, right):
                digest.append(
                    _alignment_record(
                        left,
                        right,
                        "exact-text-digest-occurrence",
                        1.0,
                        (),
                        "exact",
                    )
                )
    locked = {
        region_id
        for item in exact
        for region_id in (item.left_source_region_id, item.right_source_region_id)
    }
    bbox_evidence = _mutual_best_region_bbox(bbox_candidates, threshold, locked)
    return tuple(
        sorted((*exact, *bbox_evidence, *digest), key=lambda item: item.alignment_id)
    )


def _internal_region_alignment_record(
    region: _SourceRegionAggregate,
) -> AlignmentEvidence:
    """Record explicit identity when multiple parsers already share one region ID."""
    first, second = sorted(region.representative_observation_ids)[:2]
    alignment_id = _alignment_evidence_id(
        first,
        second,
        region.source_region_id,
        region.source_region_id,
        "exact-source-region-id",
        1.0,
        (),
        "exact",
    )
    return AlignmentEvidence(
        alignment_id=alignment_id,
        left_observation_id=first,
        right_observation_id=second,
        left_source_region_id=region.source_region_id,
        right_source_region_id=region.source_region_id,
        alignment_method="exact-source-region-id",
        alignment_score=1.0,
        status="exact",
    )


def _direct_region_alignment(
    left: _SourceRegionAggregate,
    right: _SourceRegionAggregate,
) -> tuple[str, tuple[str, ...]] | None:
    """Return the strongest exact source-native evidence for two regions."""
    a, b = left.source_region, right.source_region
    if a.source_region_id == b.source_region_id:
        return "exact-source-region-id", tuple(
            sorted(set(a.selector_ids) & set(b.selector_ids))
        )
    if (
        a.resource_id is not None
        and a.resource_id == b.resource_id
        and a.physical_page_index == b.physical_page_index
        and a.source_anchor is not None
        and a.source_anchor == b.source_anchor
    ):
        return "exact-resource-page-anchor", ()
    shared_selectors = tuple(sorted(set(a.selector_ids) & set(b.selector_ids)))
    if shared_selectors:
        return "exact-selector", shared_selectors
    if (
        a.resource_id == b.resource_id
        and a.physical_page_index == b.physical_page_index
        and a.char_start is not None
        and b.char_start is not None
        and a.char_start == b.char_start
        and a.char_end == b.char_end
    ):
        return "exact-character-span", ()
    return None


def _digest_region_alignment(
    left: _SourceRegionAggregate,
    right: _SourceRegionAggregate,
) -> bool:
    """Match exact normalized text occurrences only without stronger location."""
    return bool(
        left.text_digest_occurrences
        and left.text_digest_occurrences == right.text_digest_occurrences
        and not _has_strong_location(left.source_region)
        and not _has_strong_location(right.source_region)
    )


def _has_strong_location(region: ObservationSourceRegion) -> bool:
    """Identify evidence that must take precedence over text-digest fallback."""
    return bool(
        region.resource_id
        or region.physical_page_index is not None
        or region.presentation_unit_id
        or region.source_anchor
        or region.selector_ids
        or region.char_start is not None
        or region.bbox is not None
    )


def _normalized_text_digest(value: str) -> str:
    """Hash whitespace-collapsed casefolded text only for bounded alignment."""
    normalized = " ".join(value.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _same_region_page(
    left: _SourceRegionAggregate,
    right: _SourceRegionAggregate,
) -> bool:
    """Require equal resource and physical page before region geometry comparison."""
    a, b = left.source_region, right.source_region
    return (
        a.physical_page_index is not None
        and a.physical_page_index == b.physical_page_index
        and a.resource_id == b.resource_id
    )


def _bbox_iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    """Return intersection-over-union without averaging incompatible boundaries."""
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return 0.0 if union <= 0 else intersection / union


def _mutual_best_region_bbox(
    candidates: list[tuple[_SourceRegionAggregate, _SourceRegionAggregate]],
    threshold: float,
    locked_regions: set[str],
) -> tuple[AlignmentEvidence, ...]:
    """Accept unique mutual-best region IoU and preserve every rejected candidate."""
    scored = [
        (left, right, _bbox_iou(left.source_region.bbox, right.source_region.bbox))  # type: ignore[arg-type]
        for left, right in candidates
    ]
    best: dict[tuple[str, str], float] = {}
    for left, right, score in scored:
        right_parser_key = "|".join(right.parser_ids)
        left_parser_key = "|".join(left.parser_ids)
        best[(left.source_region_id, right_parser_key)] = max(
            score, best.get((left.source_region_id, right_parser_key), -1.0)
        )
        best[(right.source_region_id, left_parser_key)] = max(
            score, best.get((right.source_region_id, left_parser_key), -1.0)
        )
    peer_counts: dict[tuple[str, str], int] = {}
    for left, right, score in scored:
        right_parser_key = "|".join(right.parser_ids)
        left_parser_key = "|".join(left.parser_ids)
        if math.isclose(score, best[(left.source_region_id, right_parser_key)]):
            key = (left.source_region_id, right_parser_key)
            peer_counts[key] = peer_counts.get(key, 0) + 1
        if math.isclose(score, best[(right.source_region_id, left_parser_key)]):
            key = (right.source_region_id, left_parser_key)
            peer_counts[key] = peer_counts.get(key, 0) + 1
    result: list[AlignmentEvidence] = []
    for left, right, score in scored:
        right_parser_key = "|".join(right.parser_ids)
        left_parser_key = "|".join(left.parser_ids)
        left_best = best[(left.source_region_id, right_parser_key)]
        right_best = best[(right.source_region_id, left_parser_key)]
        peers_left = peer_counts[(left.source_region_id, right_parser_key)]
        peers_right = peer_counts[(right.source_region_id, left_parser_key)]
        if left.source_region_id in locked_regions or right.source_region_id in locked_regions:
            status = "superseded"
        elif score < threshold:
            status = "rejected"
        elif math.isclose(score, left_best) and math.isclose(score, right_best) and peers_left == peers_right == 1:
            status = "accepted-candidate"
        else:
            status = "ambiguous"
        result.append(_alignment_record(left, right, "bbox-iou-mutual-best", score, (), status))
    return tuple(result)


def _alignment_record(
    left: _SourceRegionAggregate,
    right: _SourceRegionAggregate,
    method: str,
    score: float,
    selectors: tuple[str, ...],
    status: str,
) -> AlignmentEvidence:
    """Create a stable region edge with representative observation endpoints."""
    ordered = sorted(
        (
            (left.representative_observation_id, left.source_region_id),
            (right.representative_observation_id, right.source_region_id),
        )
    )
    (first, first_region), (second, second_region) = ordered
    selector_ids = tuple(sorted(set(selectors)))
    return AlignmentEvidence(
        alignment_id=_alignment_evidence_id(
            first,
            second,
            first_region,
            second_region,
            method,
            score,
            selector_ids,
            status,
        ),
        left_observation_id=first,
        right_observation_id=second,
        left_source_region_id=first_region,
        right_source_region_id=second_region,
        alignment_method=method,
        alignment_score=score,
        supporting_selector_ids=selector_ids,
        status=status,
    )


def _build_alignment_groups(
    regions: tuple[_SourceRegionAggregate, ...],
    evidence: tuple[AlignmentEvidence, ...],
) -> tuple[AlignedObservationGroup, ...]:
    """Build region components while retaining but not applying uncertain edges."""
    parent = {item.source_region_id: item.source_region_id for item in regions}

    def find(value: str) -> str:
        """Resolve the deterministic root of one accepted alignment component."""
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        """Join accepted endpoints under the lexically smallest stable root."""
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for item in evidence:
        if item.status in {"exact", "accepted-candidate"}:
            union(item.left_source_region_id, item.right_source_region_id)
    components: dict[str, list[str]] = {}
    for source_region_id in sorted(parent):
        components.setdefault(find(source_region_id), []).append(source_region_id)
    region_index = {item.source_region_id: item for item in regions}
    groups: list[AlignedObservationGroup] = []
    for region_ids in components.values():
        ordered_region_ids = tuple(sorted(region_ids))
        ordered_ids = tuple(sorted({
            observation_id
            for region_id in ordered_region_ids
            for observation_id in region_index[region_id].observation_ids
        }))
        related = tuple(sorted(
            item.alignment_id for item in evidence
            if item.left_source_region_id in ordered_region_ids
            or item.right_source_region_id in ordered_region_ids
        ))
        statuses = {
            item.status for item in evidence
            if item.alignment_id in related
            and item.status not in {"rejected", "superseded"}
        }
        status = "ambiguous" if "ambiguous" in statuses else (
            "accepted-candidate" if "accepted-candidate" in statuses else "exact"
        )
        source_region_id = (
            ordered_region_ids[0]
            if len(ordered_region_ids) == 1
            else f"region-aligned-{_sha256_json(ordered_region_ids)[:24]}"
        )
        parser_ids = tuple(sorted({
            parser_id
            for region_id in ordered_region_ids
            for parser_id in region_index[region_id].parser_ids
        }))
        groups.append(AlignedObservationGroup(
            alignment_group_id=_aligned_group_id(
                source_region_id,
                ordered_region_ids,
                ordered_ids,
                parser_ids,
                related,
                status,
            ),
            source_region_id=source_region_id,
            source_region_ids=ordered_region_ids,
            observation_ids=ordered_ids,
            parser_ids=parser_ids,
            alignment_evidence_ids=related,
            alignment_status=status,
        ))
    return tuple(sorted(groups, key=lambda item: item.alignment_group_id))


def _default_policies() -> tuple[FactAdjudicationPolicy, ...]:
    """Return reviewed fact-specific policies for the bounded focused contract."""
    return (
        FactAdjudicationPolicy("policy-bbox", fact="bbox", strategy="preserve-conflict", resolution_code="fact-specific-policy"),
        FactAdjudicationPolicy("policy-blocks", fact="blocks", strategy="preserve-segmentation-variants", resolution_code="align-by-source-span-then-preserve-both-segmentation-observations"),
        FactAdjudicationPolicy("policy-native-link-target", fact="native_link_target", strategy="retain-complementary", resolution_code="retain-complementary-fact", gold_eligible_on_accept=True),
        FactAdjudicationPolicy("policy-object-type", fact="object_type", strategy="prefer-explicit-value", preferred_values=(ObservationValue.from_value("table"),), resolution_code="richer-validated-structure", gold_eligible_on_accept=False),
        FactAdjudicationPolicy("policy-owner-division", fact="owner_division", strategy="retain-complementary", resolution_code="retain-complementary-fact", gold_eligible_on_accept=True),
        FactAdjudicationPolicy("policy-reading-order", fact="reading_order", strategy="require-review", resolution_code="unresolved-reading-order"),
        FactAdjudicationPolicy("policy-segmentation", fact="segmentation", strategy="preserve-segmentation-variants", resolution_code="align-by-source-span-then-preserve-both-segmentation-observations"),
        FactAdjudicationPolicy("policy-text", fact="text", strategy="exact-agreement", resolution_code="exact-typed-value"),
    )


def _policy_index(policies: tuple[FactAdjudicationPolicy, ...]) -> dict[str, FactAdjudicationPolicy]:
    """Index exact and bounded-family policy ownership without ambiguity."""
    result: dict[str, FactAdjudicationPolicy] = {}
    ids: set[str] = set()
    for policy in policies:
        if policy.policy_id in ids:
            raise ParserAdjudicationError("Duplicate policy_id detected.")
        ids.add(policy.policy_id)
        target_type = "fact" if policy.fact is not None else "family"
        target = policy.fact or policy.fact_family
        key = f"{target_type}:{target}"
        if key in result:
            raise ParserAdjudicationError("Multiple policies target the same ownership scope.")
        result[key] = policy
    return result


def _fact_family(fact: str) -> str | None:
    """Return the one reviewed family containing a fact, if any.

    Policy dispatch uses this bounded vocabulary rather than prefixes or fuzzy
    matching. A fact can belong to at most one family, making ownership stable
    for persisted replay.
    """
    matches = tuple(
        family for family, members in _FACT_FAMILY_MEMBERS.items() if fact in members
    )
    if len(matches) > 1:
        raise ParserAdjudicationError("Fact belongs to multiple policy families.")
    return matches[0] if matches else None


def _policy_for_fact(
    fact: str,
    policies: Mapping[str, FactAdjudicationPolicy],
) -> FactAdjudicationPolicy | None:
    """Select an exact policy before a reviewed family policy."""
    exact = policies.get(f"fact:{fact}")
    if exact is not None:
        return exact
    family = _fact_family(fact)
    return policies.get(f"family:{family}") if family is not None else None


def _build_fact_decisions(
    observation_set: ParserObservationSet,
    groups: tuple[AlignedObservationGroup, ...],
    policies: Mapping[str, FactAdjudicationPolicy],
) -> tuple[FactFusionDecision, ...]:
    """Compare canonical values per aligned region and apply explicit fact policy."""
    observation_index = {item.observation_id: item for item in observation_set.observations}
    decisions: list[FactFusionDecision] = []
    for group in groups:
        grouped: dict[str, list[ParserObservation]] = {}
        for observation_id in group.observation_ids:
            item = observation_index[observation_id]
            grouped.setdefault(item.fact, []).append(item)
        for fact, values in sorted(grouped.items()):
            decisions.append(_adjudicate_fact(
                group.source_region_id,
                fact,
                tuple(sorted(values, key=lambda item: item.observation_id)),
                group.alignment_status,
                _policy_for_fact(fact, policies),
            ))
    return tuple(sorted(decisions, key=lambda item: item.decision_id))


def _adjudicate_fact(
    source_region_id: str,
    fact: str,
    observations: tuple[ParserObservation, ...],
    alignment_status: str,
    policy: FactAdjudicationPolicy | None,
) -> FactFusionDecision:
    """Execute one bounded policy strategy without discarding observations.

    Fact fusion calls this pure dispatcher after region alignment. Agreement
    requires equivalent bytes from at least two distinct parser IDs. Every
    strategy classifies all observations as accepted or rejected, never chooses
    by confidence, and preserves conflict or unresolved state when review is
    required. Explicit-value policy walks reviewed preferences in tuple order,
    chooses only the first observed value, and accepts every observation with
    that one hash so contradictory preferred values cannot both be accepted.
    """
    ids = tuple(item.observation_id for item in observations)
    values = {item.value_sha256 for item in observations}
    parser_ids = {item.parser_id for item in observations}
    unsafe = any(item.epistemic_state in _UNSAFE_GOLD_STATES for item in observations)
    policy_id = policy.policy_id if policy is not None else None
    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ids
    required_action: str | None = None
    resolution = policy.resolution_code if policy is not None else "preserve-unreviewed-conflict"
    gold = False
    if alignment_status == "ambiguous":
        state = "unresolved"
        required_action = "selective-review-or-third-parser"
        resolution = "ambiguous-source-alignment"
    elif len(parser_ids) >= 2 and len(values) == 1:
        state = "agreement"
        accepted = ids
        rejected = ()
        resolution = "exact-typed-value"
        gold = not unsafe
    elif len(values) == 1:
        state = "complementary"
        accepted = ids
        rejected = ()
        resolution = policy.resolution_code if policy is not None else "single-observed-fact"
        gold = not unsafe and (policy.gold_eligible_on_accept if policy is not None else False)
    else:
        state = "conflict"
        strategy = policy.strategy if policy is not None else "preserve-conflict"
        if strategy == "prefer-explicit-value":
            observed_hashes = {item.value_sha256 for item in observations}
            chosen_hash = next(
                (
                    item.sha256
                    for item in policy.preferred_values
                    if item.sha256 in observed_hashes
                ),
                None,
            ) if policy is not None else None
            accepted = tuple(
                item.observation_id
                for item in observations
                if item.value_sha256 == chosen_hash
            )
            rejected = tuple(item for item in ids if item not in set(accepted))
        elif strategy == "require-review":
            state = "unresolved"
            required_action = "selective-review-or-third-parser"
        elif strategy in {
            "exact-agreement",
            "retain-complementary",
            "preserve-conflict",
            "preserve-segmentation-variants",
        }:
            accepted = ()
            rejected = ids
        if policy is None:
            resolution = "preserve-unreviewed-conflict"
    accepted = tuple(sorted(accepted))
    rejected = tuple(sorted(rejected))
    gold_eligible = gold and state not in {"conflict", "unresolved"}
    return FactFusionDecision(
        decision_id=_fact_decision_id(
            source_region_id,
            fact,
            state,
            ids,
            accepted,
            rejected,
            resolution,
            required_action,
            gold_eligible,
            policy_id,
        ),
        source_region_id=source_region_id,
        fact=fact,
        state=state,
        observation_ids=ids,
        accepted_observation_ids=accepted,
        rejected_observation_ids=rejected,
        resolution=resolution,
        required_action=required_action,
        gold_eligible=gold_eligible,
        policy_id=policy_id,
    )


def _build_region_decisions(
    observation_set: ParserObservationSet,
    groups: tuple[AlignedObservationGroup, ...],
    fact_decisions: tuple[FactFusionDecision, ...],
) -> tuple[RegionFusionDecision, ...]:
    """Apply explicit unresolved, conflict, complementary, agreement precedence."""
    observation_index = {item.observation_id: item for item in observation_set.observations}
    result: list[RegionFusionDecision] = []
    for group in groups:
        decisions = tuple(item for item in fact_decisions if item.source_region_id == group.source_region_id)
        states = {item.state for item in decisions}
        parsers = tuple(sorted({
            observation_index[observation_id].parser_id
            for decision in decisions for observation_id in decision.observation_ids
        }))
        if group.alignment_status == "ambiguous" or "unresolved" in states:
            state = "unresolved"
        elif "conflict" in states:
            state = "conflict"
        elif "complementary" in states:
            state = "complementary"
        else:
            state = "agreement"
        group_ids = (group.alignment_group_id,)
        fact_ids = tuple(sorted(item.decision_id for item in decisions))
        gold_eligible = state not in {"conflict", "unresolved"} and all(
            item.gold_eligible for item in decisions
        )
        result.append(RegionFusionDecision(
            region_decision_id=_region_decision_id(
                group.source_region_id,
                group_ids,
                fact_ids,
                state,
                parsers,
                gold_eligible,
            ),
            source_region_id=group.source_region_id,
            alignment_group_ids=group_ids,
            fact_decision_ids=fact_ids,
            state=state,
            source_parsers=parsers,
            gold_eligible=gold_eligible,
        ))
    ordered = tuple(sorted(result, key=lambda item: item.region_decision_id))
    if len({item.source_region_id for item in ordered}) != len(ordered):
        raise ParserFusionValidationError("Duplicate source-region summary detected.")
    return ordered


def _alignment_from_dict(value: Mapping[str, object]) -> AlignmentEvidence:
    """Parse one strict alignment record for artifact deserialization."""
    required = {
        "alignment_id", "left_observation_id", "right_observation_id",
        "left_source_region_id", "right_source_region_id", "alignment_method",
        "alignment_score", "supporting_selector_ids", "status",
    }
    _require_fields(value, required=required, optional=set(), fusion=True)
    return AlignmentEvidence(
        alignment_id=_text_field(value, "alignment_id", fusion=True),
        left_observation_id=_text_field(value, "left_observation_id", fusion=True),
        right_observation_id=_text_field(value, "right_observation_id", fusion=True),
        left_source_region_id=_text_field(value, "left_source_region_id", fusion=True),
        right_source_region_id=_text_field(value, "right_source_region_id", fusion=True),
        alignment_method=_text_field(value, "alignment_method", fusion=True),
        alignment_score=_optional_float(value["alignment_score"], "alignment_score", fusion=True),
        supporting_selector_ids=_string_tuple(value["supporting_selector_ids"], "supporting_selector_ids", fusion=True),
        status=_text_field(value, "status", fusion=True),
    )


def _group_from_dict(value: Mapping[str, object]) -> AlignedObservationGroup:
    """Parse one strict aligned group without copying observation values."""
    required = {
        "alignment_group_id", "source_region_id", "source_region_ids",
        "observation_ids", "parser_ids", "alignment_evidence_ids",
        "alignment_status",
    }
    _require_fields(value, required=required, optional=set(), fusion=True)
    return AlignedObservationGroup(
        alignment_group_id=_text_field(value, "alignment_group_id", fusion=True),
        source_region_id=_text_field(value, "source_region_id", fusion=True),
        source_region_ids=_string_tuple(value["source_region_ids"], "source_region_ids", fusion=True),
        observation_ids=_string_tuple(value["observation_ids"], "observation_ids", fusion=True),
        parser_ids=_string_tuple(value["parser_ids"], "parser_ids", fusion=True),
        alignment_evidence_ids=_string_tuple(value["alignment_evidence_ids"], "alignment_evidence_ids", fusion=True),
        alignment_status=_text_field(value, "alignment_status", fusion=True),
    )


def _fact_decision_from_dict(value: Mapping[str, object]) -> FactFusionDecision:
    """Parse one strict fact decision and enforce its state invariants."""
    required = {"decision_id", "source_region_id", "fact", "state", "observation_ids", "accepted_observation_ids", "rejected_observation_ids", "resolution", "required_action", "gold_eligible", "policy_id"}
    _require_fields(value, required=required, optional=set(), fusion=True)
    return FactFusionDecision(
        decision_id=_text_field(value, "decision_id", fusion=True),
        source_region_id=_text_field(value, "source_region_id", fusion=True),
        fact=_text_field(value, "fact", fusion=True),
        state=_text_field(value, "state", fusion=True),
        observation_ids=_string_tuple(value["observation_ids"], "observation_ids", fusion=True),
        accepted_observation_ids=_string_tuple(value["accepted_observation_ids"], "accepted_observation_ids", fusion=True),
        rejected_observation_ids=_string_tuple(value["rejected_observation_ids"], "rejected_observation_ids", fusion=True),
        resolution=_text_field(value, "resolution", fusion=True),
        required_action=_optional_text(value["required_action"], "required_action", fusion=True),
        gold_eligible=_bool_field(value, "gold_eligible"),
        policy_id=_optional_text(value["policy_id"], "policy_id", fusion=True),
    )


def _region_decision_from_dict(value: Mapping[str, object]) -> RegionFusionDecision:
    """Parse one strict region summary and enforce gold-state safety."""
    required = {"region_decision_id", "source_region_id", "alignment_group_ids", "fact_decision_ids", "state", "source_parsers", "gold_eligible"}
    _require_fields(value, required=required, optional=set(), fusion=True)
    return RegionFusionDecision(
        region_decision_id=_text_field(value, "region_decision_id", fusion=True),
        source_region_id=_text_field(value, "source_region_id", fusion=True),
        alignment_group_ids=_string_tuple(value["alignment_group_ids"], "alignment_group_ids", fusion=True),
        fact_decision_ids=_string_tuple(value["fact_decision_ids"], "fact_decision_ids", fusion=True),
        state=_text_field(value, "state", fusion=True),
        source_parsers=_string_tuple(value["source_parsers"], "source_parsers", fusion=True),
        gold_eligible=_bool_field(value, "gold_eligible"),
    )


def _object_array(value: object, field_name: str) -> tuple[Mapping[str, object], ...]:
    """Require an array of JSON objects for strict artifact record parsing."""
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ParserFusionValidationError(f"{field_name} must be an array of objects.")
    return tuple(value)  # type: ignore[return-value]


def _string_mapping_tuple(value: object, field_name: str) -> tuple[tuple[str, str], ...]:
    """Freeze a string mapping into deterministic key order for immutable records."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ParserFusionValidationError(f"{field_name} must be a string mapping.")
    return tuple(sorted(value.items()))  # type: ignore[arg-type,return-value]


def _count_mapping_tuple(value: object) -> tuple[tuple[str, int], ...]:
    """Freeze exact contractual state counts in their declared state order."""
    if not isinstance(value, Mapping) or set(value) != set(FUSION_STATES):
        raise ParserFusionValidationError("state_counts must contain every fusion state.")
    result = []
    for state in FUSION_STATES:
        count = value[state]
        if isinstance(count, bool) or not isinstance(count, int):
            raise ParserFusionValidationError("state_counts must contain integer values.")
        result.append((state, count))
    return tuple(result)


def _validate_record_order(records: tuple[object, ...], attribute: str, label: str) -> None:
    """Reject duplicate or nondeterministically ordered aggregate record IDs."""
    identities = tuple(getattr(item, attribute) for item in records)
    if identities != tuple(sorted(set(identities))):
        raise ParserFusionValidationError(f"{label} must be unique and ordered.")


def _validate_ordered_unique(values: tuple[str, ...], label: str) -> None:
    """Require stable unique strings in immutable aggregate arrays."""
    if values != tuple(sorted(set(values))):
        raise ParserFusionValidationError(f"{label} must be unique and ordered.")


def _validate_pair_order(values: tuple[tuple[str, str], ...], label: str) -> None:
    """Require immutable key-value tuples to be unique and key ordered."""
    if values != tuple(sorted(values)) or len({key for key, _value in values}) != len(values):
        raise ParserFusionValidationError(f"{label} must have unique ordered keys.")


def _require_fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
    observation: bool = False,
    fusion: bool = False,
) -> None:
    """Reject missing and unsupported JSON fields without exposing evidence values."""
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    actual = set(value)
    if not required <= actual or not actual <= required | optional:
        raise error_type("Record fields do not match the required schema.")


def _require_id(
    value: str,
    field_name: str,
    *,
    observation: bool = False,
    alignment: bool = False,
    adjudication: bool = False,
    fusion: bool = False,
) -> None:
    """Validate one bounded identifier and select the caller's typed stage error."""
    if _ID_PATTERN.fullmatch(value):
        return
    error_type = _error_type(observation, alignment, adjudication, fusion)
    raise error_type(f"{field_name} is malformed.")


def _bounded_text(
    value: str,
    field_name: str,
    *,
    observation: bool = False,
    alignment: bool = False,
    adjudication: bool = False,
    fusion: bool = False,
    limit: int = 256,
    allow_empty: bool = False,
) -> None:
    """Validate text length and control characters without echoing source content."""
    error_type = _error_type(observation, alignment, adjudication, fusion)
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise error_type(f"{field_name} is invalid.")


def _error_type(
    observation: bool, alignment: bool, adjudication: bool, fusion: bool
) -> type[ParserFusionError]:
    """Map validation context to one public typed T05 failure class."""
    if observation:
        return ParserObservationValidationError
    if alignment:
        return ParserAlignmentError
    if adjudication:
        return ParserAdjudicationError
    return ParserFusionValidationError


def _validate_bbox(
    bbox: tuple[float, float, float, float],
    error_type: type[ParserFusionError],
) -> None:
    """Require four finite ordered coordinates without normalizing or averaging."""
    if len(bbox) != 4 or not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in bbox):
        raise error_type("bbox must contain four finite numbers.")
    if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        raise error_type("bbox coordinates must be ordered.")


def _optional_unit_interval(
    value: float | None,
    field_name: str,
    *,
    observation: bool = False,
    alignment: bool = False,
) -> None:
    """Validate optional finite confidence or alignment evidence in the unit range."""
    if value is None:
        return
    error_type = ParserObservationValidationError if observation else ParserAlignmentError
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise error_type(f"{field_name} must be a finite value from zero to one.")


def _number_tuple(value: object, length: int, *, observation: bool) -> tuple[float, ...]:
    """Parse a fixed finite numeric array without allowing booleans or raw mutables."""
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    if not isinstance(value, list) or len(value) != length:
        raise error_type("Numeric array has an invalid shape.")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value):
        raise error_type("Numeric array values must be finite.")
    return tuple(float(item) for item in value)


def _string_tuple(
    value: object, field_name: str, *, observation: bool = False, fusion: bool = False
) -> tuple[str, ...]:
    """Freeze a JSON string array without retaining caller-owned mutable storage."""
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise error_type(f"{field_name} must be a string array.")
    return tuple(value)


def _text_field(
    value: Mapping[str, object], field_name: str, *, observation: bool = False, fusion: bool = False
) -> str:
    """Read one required string field without leaking its untrusted value."""
    item = value.get(field_name)
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    if not isinstance(item, str):
        raise error_type(f"{field_name} must be a string.")
    return item


def _optional_text(
    value: object, field_name: str, *, observation: bool = False, fusion: bool = False
) -> str | None:
    """Read one optional bounded string field through the stage's typed error."""
    if value is None:
        return None
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be null or a string.")
    return value


def _optional_int(value: object, field_name: str, *, observation: bool) -> int | None:
    """Read one optional integer without accepting booleans as numeric evidence."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParserObservationValidationError(f"{field_name} must be null or an integer.")
    return value


def _optional_float(
    value: object, field_name: str, *, observation: bool = False, fusion: bool = False
) -> float | None:
    """Read one optional finite number through a typed stage boundary."""
    if value is None:
        return None
    error_type = ParserObservationValidationError if observation else ParserFusionValidationError
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise error_type(f"{field_name} must be null or a finite number.")
    return float(value)


def _int_field(value: Mapping[str, object], field_name: str, *, observation: bool) -> int:
    """Read one required integer field without boolean coercion."""
    item = value.get(field_name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ParserObservationValidationError(f"{field_name} must be an integer.")
    return item


def _bool_field(value: Mapping[str, object], field_name: str) -> bool:
    """Read one required strict boolean field for artifact deserialization."""
    item = value.get(field_name)
    if not isinstance(item, bool):
        raise ParserFusionValidationError(f"{field_name} must be a boolean.")
    return item


def _is_sha256(value: str) -> bool:
    """Return whether text is one lowercase or uppercase hexadecimal SHA-256."""
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


__all__ = [
    "ALIGNMENT_STATUSES",
    "EPISTEMIC_STATES",
    "FUSION_STATES",
    "PARSER_FUSION_ARTIFACT_SCHEMA",
    "PARSER_OBSERVATION_SET_SCHEMA",
    "AlignedObservationGroup",
    "AlignmentEvidence",
    "FactAdjudicationPolicy",
    "FactFusionDecision",
    "FusionOutcome",
    "ObservationSourceRegion",
    "ObservationValue",
    "ParserAdjudicationError",
    "ParserAlignmentError",
    "ParserFusionArtifact",
    "ParserFusionCompatibilityError",
    "ParserFusionError",
    "ParserFusionService",
    "ParserFusionUnresolvedError",
    "ParserFusionValidationError",
    "ParserObservation",
    "ParserObservationSet",
    "ParserObservationValidationError",
    "RegionFusionDecision",
]
