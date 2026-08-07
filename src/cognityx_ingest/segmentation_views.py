"""Build non-copying segmentation views over canonical Cognityx content.

Purpose
-------
A downstream reader often needs to see a document in several useful shapes. A
paragraph question-answering job wants one paragraph at a time, while retrieval
may prefer a sentence window or a small child span whose parent division is
returned. This module represents those alternatives as a segmentation view, a
derived read model made only from canonical IDs, character ranges, and bounded
strategy metadata.

Design principles
-----------------
Canonical text is stored exactly once, in
``CanonicalContentArtifact.content_nodes[*].content.text``. ``NodeSpan`` points
to that text by node ID and an optional half-open character range. Segments do
not contain text, excerpts, normalized copies, embeddings, or source paths.
Several views may overlap and disagree because each is an alternative way to
read the same source; there is deliberately no fused boundary, winning view, or
canonical chunk collection.

The closed value model is a second line of protection. Readers accept only the
known identity, span, scope, pointer, and profile fields for one strategy. A
recursive no-copy check rejects text-like fields and arbitrary payload fields.
No-copy is structural, not lexical: an ID or bounded profile value may
coincidentally equal short source text such as ``"A"`` or ``"Policy"`` without
becoming a copied text field. Strict persisted readers reject duplicate JSON names
and noncanonical array order rather than repairing input, so one logical view has
one deterministic byte representation.

The canonical digest is an immutable identity binding, not metadata a consumer
may rewrite. Value-level ``SegmentationViewSet.validate`` proves internal schema
consistency. ``SegmentationViewService.validate_view_set`` performs the stronger
proof that a production set belongs to the service's exact canonical bytes and
that every declared strategy is semantically valid against that canonical catalog.

The six strategies stay visibly different. Paragraph views preserve individual
paragraph nodes. Direct division views use only a division's directly owned
nodes. Parser-native views retain chunker observations. Sentence-safe fixed-size
views use an injected counter, sentence window views separate seed from context,
and parent-child views separate the retrieval child from its return division.

Parser-native structure is preserved without making a parser's chunk boundary
canonical. A parser-native segment retains a T01 artifact identity and native
pointer together with canonical node spans. It never imports parser-private
classes, reopens the source, reruns a parser, or copies a native chunk payload.

Processing flow
---------------
``SegmentationViewService.from_canonical`` validates one T02 canonical artifact,
hashes its exact deterministic bytes, and builds an in-memory reference catalog.
Strategy builders then create immutable segment references in canonical order.
Strict readers reject malformed or nondeterministically ordered persisted JSON.
At read time, ``resolve_span`` looks up the canonical node and returns the
requested slice without writing the reconstructed text back into a segment.

``from_fixture`` is a bounded test adapter for the compact frozen v3.2 fixture.
It verifies the fixture's exact compact shape and binds it to the SHA-256 of the
unchanged canonical fixture bytes. Production serialization uses an explicit
digest-bound shape so compact and production trust boundaries cannot be mixed.

Primary consumers
-----------------
T06 tests, audit tools, later DataForge paragraph-QA adapters, and retrieval
preparation code consume these records. The normal Ingest parser and CLI do not
need to call this module merely to preserve their current behavior.

Ownership boundary
------------------
T05 hands canonical IDs and parser fact metadata to T06 without being rerun.
T01 continues to own opaque native artifacts and their verified pointers. T06
owns deterministic reference-only view materialization. T07 owns physical
reuse, retention, and purge policy. Retrieval/DataForge owns query-time view
selection, ranking, context assembly, semantic Knowledge Units, and training
records.

Non-goals
---------
This module does not parse documents, align or fuse parser observations, choose
parsers, persist a cache, create a retrieval index, call a network/provider/LLM,
download a tokenizer, generate embeddings, execute query-time routing, or create
``canonical_chunks``. Reconstruction is transient and never becomes another
source-text store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Protocol, TypeAlias

from cognityx_ingest.canonical_content import CanonicalContentArtifact
from cognityx_ingest.native_artifacts import NativeArtifactDescriptor


SEGMENTATION_VIEWS_SCHEMA = "cognityx.ingest.segmentation-views/v3.2"
SEGMENTATION_STRATEGIES = (
    "paragraph",
    "direct-division",
    "parser-native-structure",
    "sentence-safe-fixed-size",
    "sentence-window",
    "parent-child",
)

_STRATEGY_ORDER = {name: index for index, name in enumerate(SEGMENTATION_STRATEGIES)}
_COPY_FIELD_NAMES = {
    "text",
    "content",
    "excerpt",
    "quote",
    "body",
    "source_text",
    "normalized_text",
    "embedding",
}
_PROFILE_FIELDS = {
    "implementation",
    "version",
    "chunker_id",
    "native_artifact_id",
    "max_tokens",
    "tokenizer",
    "context_before",
    "context_after",
}
_FIXTURE_CANONICAL_REFERENCE = "../expected/canonical_content.json"
_FIXTURE_INVARIANT = (
    "No segment stores source text. Text is reconstructed from canonical node IDs "
    "and optional character ranges."
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_NATIVE_POINTER_RE = re.compile(r"^#(?:/(?:[^~/]|~[01])*)*$")

JSONScalar: TypeAlias = str | int | float | bool | None


class SegmentationViewError(Exception):
    """Base typed failure for non-copying segmentation operations.

    Responsibility:
        Give API, audit, and future DataForge callers one domain-level failure
        family instead of leaking JSON, mapping, index, or filesystem errors.
    Constructed by:
        Strict readers, validators, strategy builders, and reconstruction APIs.
    Used by:
        Application composition, tests, and later T07/T08 consumers.
    Main algorithm:
        Subclasses classify validation, reference, strategy, reconstruction, and
        fixture-boundary failures without changing the failed input.
    Invariants:
        Messages contain bounded record identities, never canonical source text,
        parser-native payload bytes, credentials, or raw local paths.
    Lifecycle/persistence:
        Exceptions are transient and are never serialized into view artifacts.
    Side effects and typed failures:
        Raising an instance has no side effects; subclasses are the typed output.
    Trust boundary and thread safety:
        Instances carry immutable diagnostic strings and no shared mutable state.
    """


class SegmentationViewValidationError(SegmentationViewError):
    """Report malformed or internally inconsistent segmentation records.

    Responsibility:
        Protect strict persisted-reader and value-object invariants.
    Constructed by:
        ``from_dict``, ``from_json_bytes``, and ``validate`` methods.
    Used by:
        Fixture readers, production readers, builders, and integrity tests.
    Main algorithm:
        Reject unknown fields, invalid scalar shapes, duplicate identities, and
        nondeterministic ordering before a record can be trusted.
    Invariants:
        Validation never repairs or silently normalizes persisted input.
    Lifecycle/persistence and side effects:
        The error is transient; checking is read-only and idempotent.
    Typed failures:
        More specific reference or strategy subclasses may be raised instead.
    Trust boundary and thread safety:
        Messages are bounded and instances have ordinary immutable semantics.
    """


class SegmentationViewReferenceError(SegmentationViewValidationError):
    """Report a node, division, or native artifact reference that cannot resolve.

    Responsibility:
        Prevent a view from silently binding to missing or different canonical
        content.
    Constructed by:
        Canonical-catalog and native-artifact validation helpers.
    Used by:
        Production builders, compact fixture loading, and reconstruction calls.
    Main algorithm:
        Resolve each ID and range against the service's immutable binding.
    Invariants:
        A validated bound view contains no dangling reference.
    Lifecycle/persistence and side effects:
        Detection is transient, read-only, deterministic, and non-persisted.
    Typed failures:
        It is a specialized ``SegmentationViewValidationError``.
    Trust boundary and thread safety:
        It exposes IDs only, not resolved text or filesystem locations.
    """


class SegmentationStrategyError(SegmentationViewValidationError):
    """Report an unknown strategy or a strategy-specific shape violation.

    Responsibility:
        Keep six alternative segmentation meanings explicit instead of blending
        them into a permissive generic chunk record.
    Constructed by:
        View validation and strategy builder entry points.
    Used by:
        Producers and strict readers before a view is returned.
    Main algorithm:
        Match one frozen strategy vocabulary entry and enforce its required and
        prohibited fields and profile metadata.
    Invariants:
        No strategy can become a canonical or fused chunk boundary.
    Lifecycle/persistence and side effects:
        The transient check performs no I/O or mutation and is idempotent.
    Typed failures:
        Callers may catch this class separately from malformed references.
    Trust boundary and thread safety:
        No source value is copied into its message or retained as state.
    """


class SegmentationReconstructionError(SegmentationViewError):
    """Report a failure while resolving canonical text at read time.

    Responsibility:
        Separate transient reconstruction problems from persisted-shape errors.
    Constructed by:
        ``resolve_span``, segment resolution, and parent-scope resolution.
    Used by:
        Audit and downstream read-only consumers.
    Main algorithm:
        Wrap an impossible or ambiguous lookup after reference validation.
    Invariants:
        The error never contains the reconstructed source text.
    Lifecycle/persistence and side effects:
        It is transient; failed reconstruction writes and caches nothing.
    Typed failures:
        Invalid references normally raise ``SegmentationViewReferenceError``;
        this class covers reconstruction-specific ambiguity.
    Trust boundary and thread safety:
        It carries bounded IDs only and has no mutable shared state.
    """


class SegmentationFixtureError(SegmentationViewError):
    """Report a malformed or unavailable frozen compact fixture.

    Responsibility:
        Keep the test-only filesystem adapter outside the production artifact
        shape and avoid leaking operating-system paths.
    Constructed by:
        ``SegmentationViewService.from_fixture`` and bounded fixture readers.
    Used by:
        Focused v3.2 tests and fixture-integrity diagnostics.
    Main algorithm:
        Read only the named frozen files, reject unexpected references, and wrap
        JSON or filesystem implementation exceptions.
    Invariants:
        Production callers never need this adapter and fixture bytes are not
        rewritten.
    Lifecycle/persistence and side effects:
        Loading is read-only, repeatable, and stores no error record.
    Typed failures:
        It wraps local ``OSError`` and malformed fixture JSON failures.
    Trust boundary and thread safety:
        Messages name logical fixture roles, never raw local paths or source text.
    """


class TokenCounter(Protocol):
    """Define the narrow injected boundary for sentence-safe token budgets.

    Responsibility:
        Let application composition supply deterministic token counting without
        making T06 download a tokenizer or depend on an inference provider.
    Constructed by:
        The embedding application or a focused deterministic test double.
    Used by:
        ``build_sentence_safe_fixed_size`` only while deriving references.
    Main algorithm:
        Count tokens in one transient canonical sentence slice and return a
        non-negative integer.
    Invariants:
        The counter is not serialized and cannot place text in a segment.
    Lifecycle/persistence and side effects:
        T06 retains the counter only in memory. Implementations should be local
        and deterministic; T06 performs no network/provider/LLM call itself.
    Typed failures:
        Invalid counts become ``SegmentationStrategyError``.
    Trust boundary and thread safety:
        Concurrency properties belong to the injected implementation.
    """

    def count_tokens(self, text: str) -> int:
        """Count one transient string without retaining it.

        ``SegmentationViewService`` calls this during fixed-size generation. The
        implementation returns an integer token count; repeated calls for the
        same text must be deterministic. T06 performs no I/O, parsing, network,
        provider, or LLM call around it and never persists the argument. Counter
        implementation failures propagate, while invalid returned values become
        ``SegmentationStrategyError``.
        """
        ...


@dataclass(frozen=True, slots=True)
class SegmentationProfile:
    """Hold deterministic strategy metadata without accepting arbitrary payloads.

    Responsibility:
        Preserve fixture and production strategy identity such as tokenizer,
        chunker, version, and bounded window settings.
    Constructed by:
        Strict view readers and service strategy builders.
    Used by:
        View validation, serialization, and cache identity calculation.
    Main algorithm:
        Freeze allowed scalar key/value pairs in lexical key order.
    Invariants:
        Keys come from a bounded metadata vocabulary, values are finite JSON
        scalars, and no source text field or parser-native payload is accepted.
    Lifecycle/persistence:
        The immutable profile is serialized inside its view when non-empty.
    Side effects and typed failures:
        Construction and serialization are pure; malformed input raises typed
        validation errors.
    Trust boundary and thread safety:
        Frozen tuples make instances safe for concurrent readers.
    """

    values: tuple[tuple[str, JSONScalar], ...]

    def __post_init__(self) -> None:
        """Enforce canonical profile identity for every construction path.

        Direct Python callers and ``from_dict`` both converge here. The algorithm
        requires an immutable tuple of two-item tuples, checks the bounded field
        vocabulary, rejects duplicate or non-lexical keys, and validates each
        finite JSON scalar without converting it. This makes ``to_dict`` and cache
        identity injective for supported profiles: two distinct in-memory inputs
        cannot silently collapse to one mapping. The check is deterministic,
        performs no parser/network/provider/LLM call, retains no source payload,
        mutates nothing, and raises ``SegmentationViewValidationError`` at the
        untrusted construction boundary. Frozen tuple state remains thread-safe.
        """
        if not isinstance(self.values, tuple):
            raise SegmentationViewValidationError(
                "Segmentation profile values must be an immutable tuple"
            )
        seen: set[str] = set()
        previous: str | None = None
        for pair in self.values:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise SegmentationViewValidationError(
                    "Segmentation profile entries must be immutable key-value pairs"
                )
            key, value = pair
            if not isinstance(key, str) or key not in _PROFILE_FIELDS:
                raise SegmentationViewValidationError(
                    "Segmentation profile contains an unsupported field"
                )
            if key in seen:
                raise SegmentationViewValidationError(
                    "Segmentation profile contains a duplicate field"
                )
            if previous is not None and key < previous:
                raise SegmentationViewValidationError(
                    "Segmentation profile fields are not in canonical order"
                )
            _json_scalar(value, f"profile.{key}")
            seen.add(key)
            previous = key

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SegmentationProfile":
        """Validate and freeze untrusted profile metadata.

        Strict readers and builders call this method. It requires allowed string
        keys, finite scalar values, and lexical key order in the in-memory value;
        the returned profile is deterministic and immutable. It performs no I/O,
        parser, network, provider, or LLM call, retains no source text, and raises
        ``SegmentationViewValidationError`` for malformed values.
        """
        pairs: list[tuple[str, JSONScalar]] = []
        for key, item in value.items():
            if key not in _PROFILE_FIELDS:
                raise SegmentationViewValidationError(
                    "Segmentation profile contains an unsupported field"
                )
            pairs.append((key, _json_scalar(item, f"profile.{key}")))
        pairs.sort(key=lambda pair: pair[0])
        return cls(values=tuple(pairs))

    def to_dict(self) -> dict[str, JSONScalar]:
        """Return profile metadata in deterministic lexical order.

        Serializers and cache identity calculation call this pure method. The
        result contains only bounded scalar metadata, is identical across
        repeated calls, performs no I/O or external call, retains no source text,
        and raises no new failure after construction.
        """
        return {key: value for key, value in self.values}

    def get(self, key: str) -> JSONScalar:
        """Return one optional metadata value without exposing mutable state.

        Strategy validators call this deterministic lookup. It performs no I/O,
        parser, network, provider, or LLM call, has no side effects, retains no
        source text, and returns ``None`` when the key is absent.
        """
        return dict(self.values).get(key)


@dataclass(frozen=True, slots=True)
class NodeSpan:
    """Reference all or part of one canonical ContentNode without copying text.

    Responsibility:
        Be the core non-copying primitive shared by all segmentation strategies.
    Constructed by:
        Strict JSON readers and production strategy builders.
    Used by:
        Segments, reference validation, reconstruction, DataForge, and retrieval
        preparation.
    Main algorithm:
        Represent either a whole node or ``[char_start, char_end)`` within it.
    Invariants:
        ``node_id`` is required; range endpoints occur together, start is
        non-negative, end is greater than start, and a bound end fits the node.
        The record has no text, source path, selector, excerpt, or embedding.
    Lifecycle/persistence:
        The immutable reference may be serialized; resolved text is transient.
    Side effects and typed failures:
        Validation is pure and raises typed validation/reference errors.
    Trust boundary and thread safety:
        Frozen scalar fields are safe to share between concurrent readers.
    """

    node_id: str
    char_start: int | None = None
    char_end: int | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "NodeSpan":
        """Parse one strict persisted node reference.

        View readers call this at the JSON trust boundary. The algorithm accepts
        exactly a node ID or a node ID plus both integer endpoints, validates the
        local shape, and returns an immutable span. It is deterministic, performs
        no I/O or external call, stores no text, and raises
        ``SegmentationViewValidationError`` rather than raw mapping/type errors.
        """
        allowed = {"node_id"}
        if "char_start" in value or "char_end" in value:
            allowed.update({"char_start", "char_end"})
        _require_exact_fields(value, allowed, "node span")
        span = cls(
            node_id=_required_id(value.get("node_id"), "node_id"),
            char_start=_optional_int(value.get("char_start"), "char_start"),
            char_end=_optional_int(value.get("char_end"), "char_end"),
        )
        span.validate()
        return span

    def to_dict(self) -> dict[str, object]:
        """Serialize this reference without adding a ``text`` field.

        View serializers call this pure deterministic method. Whole-node spans
        emit only ``node_id``; ranged spans emit both endpoints. It performs no
        parser/network/provider/LLM call, retains no reconstructed text, has no
        side effects, and cannot fail after validation.
        """
        value: dict[str, object] = {"node_id": self.node_id}
        if self.char_start is not None:
            value["char_start"] = self.char_start
            value["char_end"] = self.char_end
        return value

    def validate(self, *, text_length: int | None = None) -> None:
        """Validate local range rules and an optional canonical text bound.

        Constructors and bound services call this before use. It validates paired
        endpoints, a non-empty half-open range, and optionally that ``char_end``
        does not exceed the canonical node length. Repeated checks are identical,
        mutate nothing, perform no external call, retain no source text, and raise
        typed validation or reference errors.
        """
        _required_id(self.node_id, "node_id")
        if (self.char_start is None) != (self.char_end is None):
            raise SegmentationViewValidationError(
                "Node span character endpoints must appear together"
            )
        if self.char_start is None:
            return
        if isinstance(self.char_start, bool) or isinstance(self.char_end, bool):
            raise SegmentationViewValidationError(
                "Node span character endpoints must be integers"
            )
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise SegmentationViewValidationError(
                "Node span must contain a non-empty non-negative range"
            )
        if text_length is not None and self.char_end > text_length:
            raise SegmentationViewReferenceError(
                f"Node span is outside canonical node: {self.node_id}"
            )


@dataclass(frozen=True, slots=True)
class SegmentReturnScope:
    """Reference the canonical Division returned for a parent-child hit.

    Responsibility:
        Keep the small retrieval span separate from its larger return scope.
    Constructed by:
        Strict readers and the parent-child strategy builder.
    Used by:
        Parent-child segments and read-time division reconstruction.
    Main algorithm:
        Carry one division ID and resolve it through canonical division APIs.
    Invariants:
        The record contains no parent text, subtree copy, or source selector.
    Lifecycle/persistence:
        The frozen ID is persisted; reconstructed parent text is not.
    Side effects and typed failures:
        Parsing is pure and malformed values raise typed validation errors.
    Trust boundary and thread safety:
        One immutable string is safe for concurrent readers.
    """

    division_id: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SegmentReturnScope":
        """Parse one exact return-scope object.

        Strict view readers require only ``division_id`` and return an immutable
        scope. The operation is deterministic, performs no I/O or external call,
        retains no text, has no side effects, and raises typed validation errors.
        """
        _require_exact_fields(value, {"division_id"}, "return scope")
        return cls(division_id=_required_id(value.get("division_id"), "division_id"))

    def to_dict(self) -> dict[str, str]:
        """Return the reference-only persisted representation.

        Serializers call this pure, deterministic operation. It emits no text,
        performs no parser/network/provider/LLM call, mutates nothing, and raises
        no new failure after construction.
        """
        return {"division_id": self.division_id}


@dataclass(frozen=True, slots=True)
class SegmentationSegment:
    """Represent one strategy-specific segment using canonical references only.

    Responsibility:
        Preserve every frozen segment shape in one rigorously validated value
        object without inventing a copied text field.
    Constructed by:
        ``SegmentationView.from_dict`` and service strategy builders.
    Used by:
        Views, serializers, reconstruction APIs, and downstream consumers.
    Main algorithm:
        Carry the exact subset of spans, division, native pointer, seed/context,
        retrieval spans, and return scope required by its owning strategy.
    Invariants:
        ``segment_id`` is stable, roles remain separate, and ``text`` is a
        read-only compatibility property returning ``None``. Serialization omits
        the property entirely.
    Lifecycle/persistence:
        References may persist as JSON; source and reconstructed text never do.
    Side effects and typed failures:
        Validation and serialization are pure and raise typed shape failures.
    Trust boundary and thread safety:
        Frozen tuples and records are safe for shared concurrent reads.
    """

    segment_id: str
    node_spans: tuple[NodeSpan, ...] = ()
    division_id: str | None = None
    native_chunk_pointer: str | None = None
    seed: NodeSpan | None = None
    context: tuple[NodeSpan, ...] = ()
    retrieval_node_spans: tuple[NodeSpan, ...] = ()
    return_scope: SegmentReturnScope | None = None

    @property
    def text(self) -> None:
        """Return ``None`` for the frozen compatibility seam.

        Existing T06 scaffold callers inspect this property to prove text is not
        present. It performs no reconstruction, parser, network, provider, or LLM
        call; is deterministic and side-effect free; retains no source text; and
        is deliberately omitted by ``to_dict``.
        """
        return None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SegmentationSegment":
        """Parse one exact segment object without permissive field handling.

        Strict readers call this at the persisted trust boundary. The algorithm
        rejects unsupported fields, parses each reference role independently, and
        returns an immutable segment for strategy validation by its view. It does
        no I/O or external call, retains no source text, is deterministic, and
        raises typed validation errors instead of raw implementation exceptions.
        """
        _require_allowed_fields(
            value,
            {
                "segment_id",
                "node_spans",
                "division_id",
                "native_chunk_pointer",
                "seed",
                "context",
                "retrieval_node_spans",
                "return_scope",
            },
            "segment",
        )
        segment = cls(
            segment_id=_required_id(value.get("segment_id"), "segment_id"),
            node_spans=_parse_spans(value.get("node_spans"), "node_spans"),
            division_id=_optional_id(value.get("division_id"), "division_id"),
            native_chunk_pointer=_optional_text(
                value.get("native_chunk_pointer"), "native_chunk_pointer"
            ),
            seed=(
                NodeSpan.from_dict(_mapping(value["seed"], "seed"))
                if "seed" in value
                else None
            ),
            context=_parse_spans(value.get("context"), "context"),
            retrieval_node_spans=_parse_spans(
                value.get("retrieval_node_spans"), "retrieval_node_spans"
            ),
            return_scope=(
                SegmentReturnScope.from_dict(
                    _mapping(value["return_scope"], "return_scope")
                )
                if "return_scope" in value
                else None
            ),
        )
        _required_id(segment.segment_id, "segment_id")
        return segment

    def to_dict(self) -> dict[str, object]:
        """Serialize only IDs, spans, pointers, and scope metadata.

        View serializers call this deterministic pure method. Fields absent for a
        strategy remain absent, especially ``text``; no parser/network/provider/
        LLM call or reconstruction occurs, no source text is retained, and the
        operation has no side effects.
        """
        value: dict[str, object] = {"segment_id": self.segment_id}
        if self.node_spans:
            value["node_spans"] = [span.to_dict() for span in self.node_spans]
        if self.division_id is not None:
            value["division_id"] = self.division_id
        if self.native_chunk_pointer is not None:
            value["native_chunk_pointer"] = self.native_chunk_pointer
        if self.seed is not None:
            value["seed"] = self.seed.to_dict()
        if self.context:
            value["context"] = [span.to_dict() for span in self.context]
        if self.retrieval_node_spans:
            value["retrieval_node_spans"] = [
                span.to_dict() for span in self.retrieval_node_spans
            ]
        if self.return_scope is not None:
            value["return_scope"] = self.return_scope.to_dict()
        _reject_copy_fields(value)
        return value

    def validate(self, strategy: str) -> None:
        """Enforce the owning strategy's exact role combination.

        ``SegmentationView.validate`` calls this after parsing or building. The
        algorithm keeps paragraph/direct/native/fixed spans, sentence-window
        seed/context, and parent-child retrieval/return roles distinct. It is
        deterministic, performs no I/O or external call, stores no text, mutates
        nothing, and raises ``SegmentationStrategyError`` for invalid shapes.
        """
        _validate_segment_shape(self, strategy)


@dataclass(frozen=True, slots=True)
class SegmentationView:
    """Hold one alternative derived segmentation over bound canonical content.

    Responsibility:
        Keep one strategy, deterministic profile, and ordered immutable segments
        together without claiming that their boundaries are canonical truth.
    Constructed by:
        Strict view readers and ``SegmentationViewService`` builders.
    Used by:
        Audit, DataForge handoff preparation, retrieval preparation, and tests.
    Main algorithm:
        Validate strategy-specific records and derive a cache identity from the
        exact canonical digest plus profile and reference structure.
    Invariants:
        Segment IDs are unique and canonical-order sorted, every segment matches
        the strategy, and serialized data contains no source text.
    Lifecycle/persistence:
        The view can be deterministically serialized. Its digest binding is
        runtime metadata when nested in the frozen compact fixture and is stored
        once at production view-set level.
    Side effects and typed failures:
        Methods are pure; malformed records raise typed segmentation failures.
    Trust boundary and thread safety:
        Frozen fields are safe to share and strict readers accept no unknown data.
    """

    view_id: str
    strategy: str
    profile: SegmentationProfile | None
    segments: tuple[SegmentationSegment, ...]
    canonical_content_sha256: str

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        canonical_content_sha256: str,
    ) -> "SegmentationView":
        """Parse and validate one untrusted persisted view.

        View-set readers call this with the already established canonical digest.
        It enforces exact fields, parses an optional bounded profile and ordered
        segments, then validates the result. The operation is deterministic and
        idempotent, performs no I/O/parser/network/provider/LLM call, retains no
        source text, and raises typed segmentation errors.
        """
        allowed = {"view_id", "strategy", "segments"}
        if "profile" in value:
            allowed.add("profile")
        _require_exact_fields(value, allowed, "segmentation view")
        segments_value = value.get("segments")
        if not isinstance(segments_value, list):
            raise SegmentationViewValidationError("View segments must be an array")
        view = cls(
            view_id=_required_id(value.get("view_id"), "view_id"),
            strategy=_required_text(value.get("strategy"), "strategy"),
            profile=(
                SegmentationProfile.from_dict(_mapping(value["profile"], "profile"))
                if "profile" in value
                else None
            ),
            segments=tuple(
                SegmentationSegment.from_dict(_mapping(item, "segment"))
                for item in segments_value
            ),
            canonical_content_sha256=_sha256_value(canonical_content_sha256),
        )
        view.validate()
        return view

    def to_dict(self) -> dict[str, object]:
        """Return the exact reference-only view shape used by JSON artifacts.

        View and view-set serializers call this after validation. It emits the
        frozen strategy/profile/segment fields in deterministic order and omits
        the runtime digest because a production set owns that binding. Repeated
        calls are identical, have no side effects or external calls, retain no
        source text, and raise typed validation failures before serialization.
        """
        self.validate()
        value: dict[str, object] = {
            "view_id": self.view_id,
            "strategy": self.strategy,
        }
        if self.profile is not None:
            value["profile"] = self.profile.to_dict()
        value["segments"] = [segment.to_dict() for segment in self.segments]
        _reject_copy_fields(value)
        return value

    def to_json_bytes(self) -> bytes:
        """Serialize one view to stable compact UTF-8 JSON bytes.

        Cache identity tests and downstream transports call this pure method. It
        validates the view, sorts object keys, preserves array order, and appends
        one newline. It performs no parser/network/provider/LLM call, reconstructs
        or retains no text, is idempotent, and raises typed validation failures.
        """
        return _canonical_json_bytes(self.to_dict())

    @property
    def cache_identity(self) -> str:
        """Return a deterministic text-free SHA-256 cache identity.

        T06 callers and later T07 retention logic may compare this value. The
        algorithm hashes the exact canonical-content digest, strategy, profile,
        and segment reference bytes. It does not create a cache, read text, call
        parsers/networks/providers/LLMs, mutate state, or retain source content;
        repeated calls return the same lowercase digest.
        """
        payload = {
            "canonical_content_sha256": self.canonical_content_sha256,
            "view": self.to_dict(),
        }
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()

    def validate(self) -> None:
        """Validate strategy, profile, segment uniqueness, and ordering.

        Builders and strict readers call this before returning or serializing a
        view. It enforces one frozen strategy, strategy-specific profile fields,
        lexically ordered unique segment IDs, and each segment role shape. The
        check is deterministic, idempotent, side-effect free, makes no external
        call, retains no source text, and raises typed validation errors.
        """
        _required_id(self.view_id, "view_id")
        _sha256_value(self.canonical_content_sha256)
        if self.strategy not in SEGMENTATION_STRATEGIES:
            raise SegmentationStrategyError(
                "Unknown segmentation strategy"
            )
        _validate_profile(self.strategy, self.profile)
        identifiers = tuple(segment.segment_id for segment in self.segments)
        _validate_ordered_unique(identifiers, "segment IDs")
        for segment in self.segments:
            segment.validate(self.strategy)
        _reject_copy_fields(self.to_unvalidated_dict())

    def to_unvalidated_dict(self) -> dict[str, object]:
        """Build internal JSON data for recursive no-copy validation.

        ``validate`` uses this helper to avoid a serialization recursion. It is a
        pure deterministic projection, performs no external call, reconstructs
        and retains no text, has no side effects, and assumes nested value objects
        have already passed their local construction checks.
        """
        value: dict[str, object] = {
            "view_id": self.view_id,
            "strategy": self.strategy,
            "segments": [segment.to_dict() for segment in self.segments],
        }
        if self.profile is not None:
            value["profile"] = self.profile.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class SegmentationViewSet:
    """Aggregate ordered views with one explicit canonical-content binding.

    Responsibility:
        Represent either the exact compact frozen fixture or a digest-bound
        production catalog without silently accepting a partial hybrid shape.
    Constructed by:
        Strict JSON readers and ``SegmentationViewService.build_view_set``.
    Used by:
        Fixture tests, production persistence adapters, audit, and later T07.
    Main algorithm:
        Validate schema, canonical digest, alternative shape, view ordering, and
        cross-view uniqueness before deterministic serialization.
    Invariants:
        All views share one digest. Compact fixture metadata appears together;
        production sets store the digest explicitly. No view is selected as a
        winner and no source text appears in the aggregate.
    Lifecycle/persistence:
        The immutable aggregate is serializable but T06 creates no storage/cache.
    Side effects and typed failures:
        Reading and serialization are pure; malformed data raises typed errors.
    Trust boundary and thread safety:
        Strict duplicate-key and exact-field readers protect untrusted bytes;
        frozen records are safe for concurrent reads.
    """

    schema: str
    canonical_content_sha256: str
    views: tuple[SegmentationView, ...]
    canonical_content_artifact: str | None = None
    invariant: str | None = None

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        compact_canonical_sha256: str | None = None,
    ) -> "SegmentationViewSet":
        """Parse exactly one compact-fixture or complete-production shape.

        Fixture and production readers call this after strict JSON decoding. A
        compact shape requires its externally computed exact canonical digest;
        production bytes must contain their digest. Both forms parse ordered
        views and validate before return. The method is deterministic, read-only,
        performs no parser/network/provider/LLM call, stores no source text, and
        raises typed validation failures.
        """
        keys = set(value)
        compact_keys = {
            "schema",
            "canonical_content_artifact",
            "invariant",
            "views",
        }
        production_keys = {"schema", "canonical_content_sha256", "views"}
        if keys == compact_keys:
            if compact_canonical_sha256 is None:
                raise SegmentationFixtureError(
                    "Compact segmentation fixture requires canonical byte binding"
                )
            artifact_reference = _required_text(
                value.get("canonical_content_artifact"),
                "canonical_content_artifact",
            )
            invariant = _required_text(value.get("invariant"), "invariant")
            digest = _sha256_value(compact_canonical_sha256)
        elif keys == production_keys:
            if compact_canonical_sha256 is not None:
                raise SegmentationViewValidationError(
                    "Production segmentation set cannot use compact fixture binding"
                )
            artifact_reference = None
            invariant = None
            digest = _sha256_value(value.get("canonical_content_sha256"))
        else:
            raise SegmentationViewValidationError(
                "Segmentation view set has unsupported or partial fields"
            )
        views_value = value.get("views")
        if not isinstance(views_value, list):
            raise SegmentationViewValidationError("Views must be an array")
        aggregate = cls(
            schema=_required_text(value.get("schema"), "schema"),
            canonical_content_sha256=digest,
            views=tuple(
                SegmentationView.from_dict(
                    _mapping(item, "view"),
                    canonical_content_sha256=digest,
                )
                for item in views_value
            ),
            canonical_content_artifact=artifact_reference,
            invariant=invariant,
        )
        aggregate.validate()
        return aggregate

    @classmethod
    def from_json_bytes(
        cls,
        payload: bytes,
        *,
        compact_canonical_sha256: str | None = None,
    ) -> "SegmentationViewSet":
        """Decode strict JSON bytes and reject duplicate object keys.

        Persisted readers and the fixture adapter call this trust-boundary method.
        It rejects duplicate names, non-finite constants, invalid UTF-8, malformed
        JSON, and non-object roots before delegating to ``from_dict``. It is
        read-only and deterministic, makes no parser/network/provider/LLM call,
        retains no source text, and wraps implementation exceptions in typed
        validation or fixture failures.
        """
        value = _strict_json_loads(payload)
        return cls.from_dict(
            _mapping(value, "segmentation view set"),
            compact_canonical_sha256=compact_canonical_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the exact compact or production aggregate representation.

        Serializers call this after validation. Compact records preserve the
        frozen reference and invariant fields; production records emit one
        canonical digest. Both contain ordered view references only. Repeated
        calls are deterministic and side-effect free, perform no external call,
        retain no source text, and raise typed failures before emitting data.
        """
        self.validate()
        if self.canonical_content_artifact is not None:
            value: dict[str, object] = {
                "schema": self.schema,
                "canonical_content_artifact": self.canonical_content_artifact,
                "invariant": self.invariant,
                "views": [view.to_dict() for view in self.views],
            }
        else:
            value = {
                "schema": self.schema,
                "canonical_content_sha256": self.canonical_content_sha256,
                "views": [view.to_dict() for view in self.views],
            }
        _reject_copy_fields(value)
        return value

    def to_json_bytes(self) -> bytes:
        """Serialize the validated aggregate to deterministic UTF-8 bytes.

        Persistence adapters, cache identity tests, and audit tools call this
        method. It uses compact sorted-key JSON plus one newline and preserves
        canonical array order. It performs no parser/network/provider/LLM call,
        reconstruction, mutation, or caching; retains no source text; and raises
        typed validation errors for an invalid aggregate.
        """
        return _canonical_json_bytes(self.to_dict())

    def validate(self) -> None:
        """Validate internal schema, binding form, order, and cross-view identity.

        Readers and serializers call this value-level check before use. It proves
        the exact schema, one internally consistent digest, complete compact or
        production shape, strategy-order sorting, and unique view IDs. It does not
        prove that the digest or references belong to any available canonical
        artifact; production consumers use
        ``SegmentationViewService.validate_view_set`` for that stronger trust
        boundary. The operation is deterministic, idempotent, side-effect free,
        thread-safe, makes no external call, retains no source text, and raises
        typed validation errors.
        """
        if self.schema != SEGMENTATION_VIEWS_SCHEMA:
            raise SegmentationViewValidationError(
                "Unsupported segmentation schema"
            )
        _sha256_value(self.canonical_content_sha256)
        compact = self.canonical_content_artifact is not None or self.invariant is not None
        if compact:
            if self.canonical_content_artifact != _FIXTURE_CANONICAL_REFERENCE:
                raise SegmentationFixtureError(
                    "Compact segmentation fixture has unexpected canonical reference"
                )
            if self.invariant != _FIXTURE_INVARIANT:
                raise SegmentationFixtureError(
                    "Compact segmentation fixture has unexpected invariant"
                )
        elif self.canonical_content_artifact is not None or self.invariant is not None:
            raise SegmentationViewValidationError(
                "Segmentation compact binding must be complete"
            )
        identities = tuple(view.view_id for view in self.views)
        if len(set(identities)) != len(identities):
            raise SegmentationViewValidationError("Duplicate segmentation view ID")
        expected_order = tuple(
            sorted(
                self.views,
                key=lambda item: (_STRATEGY_ORDER.get(item.strategy, 999), item.view_id),
            )
        )
        if self.views != expected_order:
            raise SegmentationViewValidationError(
                "Segmentation views are not in canonical strategy order"
            )
        for view in self.views:
            if view.canonical_content_sha256 != self.canonical_content_sha256:
                raise SegmentationViewReferenceError(
                    f"View has different canonical binding: {view.view_id}"
                )
            view.validate()
        _reject_copy_fields(self.to_unvalidated_dict())

    def to_unvalidated_dict(self) -> dict[str, object]:
        """Project the aggregate for recursive no-copy checking without recursion.

        ``validate`` uses this pure deterministic helper. It performs no I/O or
        external call, reconstructs and retains no source text, mutates nothing,
        and emits only already constructed reference records.
        """
        value: dict[str, object] = {
            "schema": self.schema,
            "views": [view.to_unvalidated_dict() for view in self.views],
        }
        if self.canonical_content_artifact is not None:
            value["canonical_content_artifact"] = self.canonical_content_artifact
            value["invariant"] = self.invariant
        else:
            value["canonical_content_sha256"] = self.canonical_content_sha256
        return value


@dataclass(frozen=True, slots=True)
class _ReferenceCatalog:
    """Keep runtime canonical lookup facts outside serialized view records.

    The service builds this immutable catalog from either a validated production
    artifact or the exact compact fixture projections. It owns transient canonical
    text for reconstruction, canonical ordering and division ownership. No view
    receives these text mappings, preventing accidental persistence.
    """

    texts: Mapping[str, str]
    node_kinds: Mapping[str, str]
    node_order: Mapping[str, int]
    node_owners: Mapping[str, str]
    division_order: Mapping[str, int]
    direct_node_ids: Mapping[str, tuple[str, ...]]
    subtree_node_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class SegmentationViewService:
    """Build, validate, and reconstruct digest-bound non-copying views.

    Responsibility:
        Provide the public T06 composition seam for frozen fixture acceptance and
        normal production canonical content.
    Constructed by:
        ``from_fixture`` in focused tests or ``from_canonical`` in applications.
    Used by:
        Audit tooling, future DataForge handoff, retrieval preparation, and tests.
    Main algorithm:
        Bind exact canonical bytes, index IDs and ownership, validate supplied
        views, derive six deterministic strategies, and reconstruct text only on
        explicit read calls.
    Invariants:
        The service never opens the original source, reruns a parser, changes T05
        observations/fusion, mutates canonical content, copies text into records,
        calls a network/provider/LLM, or creates a cache.
    Lifecycle/persistence:
        It is an in-memory immutable composition object. Views are serializable;
        physical retention and purge belong to T07.
    Side effects and typed failures:
        Fixture creation performs bounded reads; all other operations are pure.
        Typed segmentation errors protect malformed data and unresolved IDs.
    Trust boundary and thread safety:
        Strict readers validate untrusted bytes. Frozen state is safe for
        concurrent reads when the injected token counter is itself thread-safe.
    """

    _catalog: _ReferenceCatalog
    _view_set: SegmentationViewSet
    _native_descriptors: Mapping[str, NativeArtifactDescriptor]
    _fixture_native_artifacts: Mapping[str, object]
    _token_counter: TokenCounter | None = None

    @classmethod
    def from_fixture(cls, v3_2_fixture_root: Path) -> "SegmentationViewService":
        """Load the exact frozen compact v3.2 fixture without modifying it.

        Focused acceptance tests call this bounded adapter with the fixture root.
        It reads the canonical, source-graph, native-binding, native-payload, and
        view JSON files; hashes exact canonical bytes; strictly rejects duplicate
        keys; builds canonical references; and validates all six views. Repeated
        calls return equivalent immutable services and never write, parse source
        documents, call a network/provider/LLM, or retain text in a view. Local
        filesystem/JSON failures become ``SegmentationFixtureError`` without raw
        paths in messages.
        """
        try:
            canonical_bytes = _read_fixture_bytes(
                v3_2_fixture_root, "expected/canonical_content.json"
            )
            graph_bytes = _read_fixture_bytes(
                v3_2_fixture_root, "expected/source_graph.json"
            )
            binding_bytes = _read_fixture_bytes(
                v3_2_fixture_root, "expected/native_bindings.json"
            )
            view_bytes = _read_fixture_bytes(
                v3_2_fixture_root, "segmentation_views/views.json"
            )
            canonical_value = _mapping(
                _strict_json_loads(canonical_bytes), "compact canonical content"
            )
            graph_value = _mapping(
                _strict_json_loads(graph_bytes), "compact source graph"
            )
            binding_value = _mapping(
                _strict_json_loads(binding_bytes), "compact native bindings"
            )
            digest = hashlib.sha256(canonical_bytes).hexdigest()
            catalog = _catalog_from_compact(canonical_value, graph_value)
            fixture_native_artifacts = _fixture_native_artifact_markers(
                v3_2_fixture_root, binding_value
            )
            view_set = SegmentationViewSet.from_json_bytes(
                view_bytes, compact_canonical_sha256=digest
            )
            service = cls(
                _catalog=catalog,
                _view_set=view_set,
                _native_descriptors=MappingProxyType({}),
                _fixture_native_artifacts=MappingProxyType(
                    fixture_native_artifacts
                ),
            )
            service._validate_bound_view_set(view_set, require_production=False)
            return service
        except SegmentationViewError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, KeyError) as error:
            raise SegmentationFixtureError(
                "Unable to load frozen segmentation fixture"
            ) from error

    @classmethod
    def from_canonical(
        cls,
        canonical_content: CanonicalContentArtifact,
        *,
        views: Sequence[SegmentationView] = (),
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
        token_counter: TokenCounter | None = None,
    ) -> "SegmentationViewService":
        """Create production composition from validated exact canonical bytes.

        Ingest application composition and tests call this with a T02 artifact,
        optional already-verified T01 descriptors, optional existing views, and a
        local token counter. The algorithm serializes/validates canonical content,
        hashes those exact bytes, creates immutable lookup indexes, requires every
        supplied view to carry that same immutable digest, and creates a production
        view set without rewriting any view identity. It does not
        mutate canonical content, open source files, run parsers, call a network,
        provider, or LLM, or retain canonical text in returned views. Repeated
        calls over equal inputs are deterministic. Typed canonical validation may
        propagate; malformed views raise typed segmentation failures.
        """
        descriptor_map = _validated_native_descriptor_map(native_descriptors)
        canonical_bytes = canonical_content.to_json_bytes(
            native_descriptors=descriptor_map or None
        )
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        catalog = _catalog_from_canonical(
            canonical_content, native_descriptors=descriptor_map or None
        )
        ordered_views = tuple(
            sorted(
                views,
                key=lambda item: (_STRATEGY_ORDER.get(item.strategy, 999), item.view_id),
            )
        )
        view_set = SegmentationViewSet(
            schema=SEGMENTATION_VIEWS_SCHEMA,
            canonical_content_sha256=digest,
            views=ordered_views,
        )
        service = cls(
            _catalog=catalog,
            _view_set=view_set,
            _native_descriptors=MappingProxyType(descriptor_map),
            _fixture_native_artifacts=MappingProxyType({}),
            _token_counter=token_counter,
        )
        service.validate_view_set(view_set)
        return service

    @property
    def view_set(self) -> SegmentationViewSet:
        """Return the immutable bound catalog without copying or selecting a view.

        Audit and production callers use this read-only property. It returns the
        same validated aggregate, performs no I/O/parser/network/provider/LLM call,
        reconstructs and retains no new text, is idempotent and side-effect free,
        and raises no failure after service construction.
        """
        return self._view_set

    def build(self, view_id: str) -> SegmentationView:
        """Return one already defined view by stable identity.

        The frozen scaffold and catalog consumers call this method. It validates
        the bounded ID, performs an immutable lookup, and returns the exact view;
        it does not choose a winning strategy, reparse content, call a network,
        provider, or LLM, mutate records, or retain reconstructed text. Repeated
        calls are identical. Unknown IDs raise ``SegmentationViewReferenceError``.
        """
        requested = _required_id(view_id, "view_id")
        for view in self._view_set.views:
            if view.view_id == requested:
                return view
        raise SegmentationViewReferenceError(f"Unknown segmentation view: {requested}")

    def build_view_set(
        self, views: Sequence[SegmentationView]
    ) -> SegmentationViewSet:
        """Create a deterministic production aggregate from alternative views.

        Application composition calls this after invoking one or more strategy
        builders. The method requires every view's existing immutable canonical
        digest to equal this service, sorts only the input tuple by frozen strategy
        order and ID, validates references and strategy semantics, and returns an
        immutable production set. It never repairs or rewrites a digest, strategy,
        profile, or segment reference. It creates no cache or persistent artifact,
        performs no parser/network/provider/LLM call, mutates no input, retains no
        source text in records, and raises typed reference or validation failures.
        """
        ordered_views = tuple(
            sorted(
                views,
                key=lambda item: (_STRATEGY_ORDER.get(item.strategy, 999), item.view_id),
            )
        )
        aggregate = SegmentationViewSet(
            schema=SEGMENTATION_VIEWS_SCHEMA,
            canonical_content_sha256=self._view_set.canonical_content_sha256,
            views=ordered_views,
        )
        return self.validate_view_set(aggregate)

    def validate_view_set(
        self, view_set: SegmentationViewSet
    ) -> SegmentationViewSet:
        """Prove a production view set belongs to this exact canonical artifact.

        T07 reuse logic, production readers, audit tools, and application
        composition call this after ``SegmentationViewSet.validate`` has established
        internal schema consistency. This stronger trust-boundary operation
        requires the complete production shape, compares the immutable aggregate
        and every view digest with this service's exact canonical bytes, validates
        node/division/native references, and enforces declared strategy semantics.
        It returns the same immutable object after proof; it never repairs,
        normalizes, rebinds, persists, parses, or calls a network/provider/LLM.
        Repeated validation is deterministic and side-effect free. Shape failures
        raise ``SegmentationViewValidationError`` and foreign canonical bindings
        raise ``SegmentationViewReferenceError`` without exposing source text.
        Frozen inputs and read-only service indexes are thread-safe.
        """
        self._validate_bound_view_set(view_set, require_production=True)
        return view_set

    def build_paragraph(
        self,
        view_id: str = "view-paragraph-v1",
        *,
        profile: Mapping[str, object] | None = None,
    ) -> SegmentationView:
        """Build one segment per canonical paragraph ContentNode.

        Production composition calls this for paragraph-QA and similar consumers.
        The algorithm selects only nodes whose ``node_kind`` is ``paragraph``,
        follows canonical node order, and creates one whole-node span per segment;
        adjacent paragraphs are never merged. The result is deterministic and
        immutable, performs no parser/network/provider/LLM call, does not mutate
        canonical content, persists or retains no source text, and raises typed
        strategy/reference failures.
        """
        nodes = self._ordered_nodes(kind="paragraph")
        segments = tuple(
            SegmentationSegment(
                segment_id=f"{_required_id(view_id, 'view_id')}:segment:{index:06d}",
                node_spans=(NodeSpan(node_id=node_id),),
            )
            for index, node_id in enumerate(nodes, start=1)
        )
        return self._new_view(view_id, "paragraph", profile, segments)

    def build_direct_division(
        self,
        view_id: str = "view-direct-division-v1",
        *,
        division_ids: Sequence[str] | None = None,
        profile: Mapping[str, object] | None = None,
    ) -> SegmentationView:
        """Build one reference-only segment from each division's direct nodes.

        Production composition calls this when it needs deepest direct ownership,
        not subtree content. The algorithm validates optional division IDs, sorts
        by canonical division order, and calls the catalog projected from
        ``CanonicalContentArtifact.direct_nodes`` semantics. It never copies
        section text or descendants, mutates canonical content, reparses a source,
        or calls a network/provider/LLM. Output is deterministic and source-text
        free; typed reference/strategy errors describe invalid inputs.
        """
        if division_ids is None:
            selected = tuple(
                division_id
                for division_id, _ in sorted(
                    self._catalog.division_order.items(), key=lambda item: item[1]
                )
                if self._catalog.direct_node_ids.get(division_id)
            )
        else:
            selected = tuple(
                sorted(
                    (_required_id(value, "division_id") for value in division_ids),
                    key=self._division_sort_key,
                )
            )
            if len(set(selected)) != len(selected):
                raise SegmentationViewValidationError("Duplicate division ID")
        segments = tuple(
            SegmentationSegment(
                segment_id=(
                    f"{_required_id(view_id, 'view_id')}:segment:{index:06d}"
                ),
                division_id=division_id,
                node_spans=tuple(
                    NodeSpan(node_id=node_id)
                    for node_id in self._catalog.direct_node_ids[division_id]
                ),
            )
            for index, division_id in enumerate(selected, start=1)
        )
        return self._new_view(view_id, "direct-division", profile, segments)

    def build_parser_native(
        self,
        view_id: str,
        *,
        chunker_id: str,
        native_artifact_id: str,
        segments: Sequence[SegmentationSegment],
    ) -> SegmentationView:
        """Bind caller-supplied native chunk pointers to canonical spans.

        A parser adapter or audit composition layer calls this only after T01 has
        preserved the native artifact. The algorithm creates the required bounded
        profile, orders immutable segment definitions by ID, and validates each
        pointer against the supplied verified descriptor when available. It does
        not import parser-private classes, invent pointers, reopen/reparse source,
        copy native chunk text, call a network/provider/LLM, or mutate T01/T05.
        Equal inputs produce equal view bytes; invalid bindings raise typed
        strategy/reference errors and no source text is retained.
        """
        profile = {
            "chunker_id": _required_id(chunker_id, "chunker_id"),
            "native_artifact_id": _required_id(
                native_artifact_id, "native_artifact_id"
            ),
        }
        ordered = tuple(sorted(segments, key=lambda item: item.segment_id))
        return self._new_view(
            view_id, "parser-native-structure", profile, ordered
        )

    def build_sentence_safe_fixed_size(
        self,
        view_id: str,
        *,
        max_tokens: int,
        tokenizer: str,
    ) -> SegmentationView:
        """Group sentence-aligned canonical spans under an injected token budget.

        Production composition calls this with a local deterministic
        ``TokenCounter``. The algorithm finds sentence boundaries inside canonical
        paragraphs, counts each transient slice, greedily groups consecutive
        references without crossing the budget, and permits one oversized sentence
        rather than splitting it unsafely. Partial nodes use character ranges.
        No tokenizer/model is downloaded, no parser/network/provider/LLM is called,
        canonical content is unchanged, and no counted text is retained or
        serialized. Equal inputs and counter results produce equal bytes. Missing
        counters or invalid counts raise ``SegmentationStrategyError``.
        """
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise SegmentationStrategyError("max_tokens must be a positive integer")
        if self._token_counter is None:
            raise SegmentationStrategyError(
                "Sentence-safe generation requires an injected TokenCounter"
            )
        tokenizer_id = _required_id(tokenizer, "tokenizer")
        sentence_spans: list[tuple[NodeSpan, int]] = []
        for node_id in self._ordered_nodes(kind="paragraph"):
            text = self._catalog.texts[node_id]
            for start, end in _sentence_ranges(text):
                try:
                    count = self._token_counter.count_tokens(text[start:end])
                except Exception as error:
                    raise SegmentationStrategyError(
                        "TokenCounter failed while counting a canonical sentence"
                    ) from error
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    raise SegmentationStrategyError(
                        "TokenCounter returned a non-positive integer"
                    )
                span = (
                    NodeSpan(node_id=node_id)
                    if start == 0 and end == len(text)
                    else NodeSpan(node_id=node_id, char_start=start, char_end=end)
                )
                sentence_spans.append((span, count))
        grouped: list[tuple[NodeSpan, ...]] = []
        current: list[NodeSpan] = []
        current_count = 0
        for span, count in sentence_spans:
            if current and current_count + count > max_tokens:
                grouped.append(tuple(current))
                current = []
                current_count = 0
            current.append(span)
            current_count += count
            if count > max_tokens:
                grouped.append(tuple(current))
                current = []
                current_count = 0
        if current:
            grouped.append(tuple(current))
        segments = tuple(
            SegmentationSegment(
                segment_id=f"{_required_id(view_id, 'view_id')}:segment:{index:06d}",
                node_spans=spans,
            )
            for index, spans in enumerate(grouped, start=1)
        )
        return self._new_view(
            view_id,
            "sentence-safe-fixed-size",
            {"max_tokens": max_tokens, "tokenizer": tokenizer_id},
            segments,
        )

    def build_sentence_window(
        self,
        view_id: str,
        *,
        seed_node_ids: Sequence[str] | None = None,
        context_before: int = 1,
        context_after: int = 1,
    ) -> SegmentationView:
        """Build explicit seed and neighbouring-context paragraph references.

        Retrieval preparation calls this to preserve the distinction between the
        sentence/paragraph that matched and nearby context. The algorithm orders
        canonical paragraphs, validates optional seeds, and records bounded
        neighbours separately from each seed. It does not flatten them into a new
        authoritative node, copy text, mutate canonical content, run parsers, or
        call a network/provider/LLM. Output is deterministic and idempotent;
        invalid window sizes or node IDs raise typed failures.
        """
        before = _non_negative_int(context_before, "context_before")
        after = _non_negative_int(context_after, "context_after")
        paragraphs = self._ordered_nodes(kind="paragraph")
        positions = {node_id: index for index, node_id in enumerate(paragraphs)}
        if seed_node_ids is None:
            seeds = paragraphs
        else:
            requested = {_required_id(item, "seed node ID") for item in seed_node_ids}
            missing = requested.difference(positions)
            if missing:
                raise SegmentationViewReferenceError(
                    f"Unknown sentence-window seed: {sorted(missing)[0]}"
                )
            seeds = tuple(node_id for node_id in paragraphs if node_id in requested)
        segments: list[SegmentationSegment] = []
        for index, node_id in enumerate(seeds, start=1):
            position = positions[node_id]
            neighbours = (
                paragraphs[max(0, position - before) : position]
                + paragraphs[position + 1 : position + after + 1]
            )
            segments.append(
                SegmentationSegment(
                    segment_id=(
                        f"{_required_id(view_id, 'view_id')}:segment:{index:06d}"
                    ),
                    seed=NodeSpan(node_id=node_id),
                    context=tuple(NodeSpan(node_id=item) for item in neighbours),
                )
            )
        return self._new_view(
            view_id,
            "sentence-window",
            {"context_before": before, "context_after": after},
            tuple(segments),
        )

    def build_parent_child(
        self,
        view_id: str,
        *,
        retrieval_node_ids: Sequence[str] | None = None,
    ) -> SegmentationView:
        """Build paragraph retrieval spans with canonical division return scopes.

        Retrieval preparation calls this when a small child should match but its
        owning division should be returned. The algorithm selects canonical
        paragraphs in source order, records each child separately, and points to
        the node's deepest owner division. It never copies parent/subtree text,
        materializes a parent chunk, mutates canonical content, runs a parser, or
        calls a network/provider/LLM. Equal inputs produce equal bytes; invalid
        IDs or ownership raise typed reference failures.
        """
        paragraphs = self._ordered_nodes(kind="paragraph")
        if retrieval_node_ids is None:
            selected = paragraphs
        else:
            requested = {
                _required_id(item, "retrieval node ID") for item in retrieval_node_ids
            }
            missing = requested.difference(paragraphs)
            if missing:
                raise SegmentationViewReferenceError(
                    f"Unknown parent-child retrieval node: {sorted(missing)[0]}"
                )
            selected = tuple(item for item in paragraphs if item in requested)
        segments = tuple(
            SegmentationSegment(
                segment_id=f"{_required_id(view_id, 'view_id')}:segment:{index:06d}",
                retrieval_node_spans=(NodeSpan(node_id=node_id),),
                return_scope=SegmentReturnScope(
                    division_id=self._catalog.node_owners[node_id]
                ),
            )
            for index, node_id in enumerate(selected, start=1)
        )
        return self._new_view(view_id, "parent-child", None, segments)

    def resolve_span(self, span: NodeSpan) -> str:
        """Resolve one canonical node slice without storing the returned text.

        Audit and downstream read-only callers use this after obtaining a view.
        The algorithm validates the node and optional range against the bound
        catalog, reads ``ContentNode.content.text``, applies ``[start, end)``, and
        returns a transient string. It is deterministic and side-effect free,
        performs no parser/network/provider/LLM call, mutates neither canonical
        content nor views, caches and retains nothing, and raises typed reference
        errors for invalid spans.
        """
        self._validate_span(span)
        text = self._catalog.texts[span.node_id]
        if span.char_start is None:
            return text
        return text[span.char_start : span.char_end]

    def resolve_segment_spans(
        self,
        segment_id: str,
        *,
        view_id: str | None = None,
        view: SegmentationView | None = None,
    ) -> tuple[str, ...]:
        """Resolve ordered slices for one segment while keeping roles unpersisted.

        Audit and downstream readers call this with a segment ID and optionally a
        catalog view ID or a newly built immutable view. It finds exactly one
        segment, orders ordinary spans or the sentence-window seed followed by
        context or parent-child retrieval spans, and delegates each slice to
        ``resolve_span``. It invents no concatenation semantics, performs no
        parser/network/provider/LLM call, mutates and caches nothing, retains no
        result, and raises typed reconstruction errors when an unqualified ID is
        ambiguous.
        """
        segment = self._find_segment(segment_id, view_id=view_id, view=view)
        spans = segment.node_spans
        if segment.seed is not None:
            spans = (segment.seed,) + segment.context
        elif segment.retrieval_node_spans:
            spans = segment.retrieval_node_spans
        return tuple(self.resolve_span(span) for span in spans)

    def resolve_return_scope(
        self,
        segment_id: str,
        *,
        view_id: str | None = None,
        view: SegmentationView | None = None,
    ) -> tuple[str, ...]:
        """Resolve a parent-child return division through canonical hierarchy.

        Parent-child consumers call this only after retrieval. The algorithm finds
        the segment in a catalog or newly built immutable view, requires a return
        scope, obtains the division's canonical subtree node IDs in source order,
        and returns separate text values rather than a duplicate parent chunk. It
        is deterministic and read-only, performs no parser/network/provider/LLM
        call, mutates and caches nothing, retains no result, and raises typed
        reconstruction/reference failures.
        """
        segment = self._find_segment(segment_id, view_id=view_id, view=view)
        if segment.return_scope is None:
            raise SegmentationReconstructionError(
                f"Segment has no return scope: {segment.segment_id}"
            )
        division_id = segment.return_scope.division_id
        node_ids = self._catalog.subtree_node_ids.get(division_id)
        if node_ids is None:
            raise SegmentationViewReferenceError(
                f"Unknown return-scope division: {division_id}"
            )
        return tuple(self._catalog.texts[node_id] for node_id in node_ids)

    def _new_view(
        self,
        view_id: str,
        strategy: str,
        profile: Mapping[str, object] | None,
        segments: tuple[SegmentationSegment, ...],
    ) -> SegmentationView:
        """Construct one bound view and enforce structural reference invariants.

        Strategy builders share this deterministic finalization step so no builder
        can bypass validation. It freezes profile metadata, binds the canonical
        digest, and validates all references and strategy semantics through the
        service's exact catalog. The closed value schema, not lexical comparison,
        enforces no-copy. It performs no I/O or external call and mutates nothing.
        """
        view = SegmentationView(
            view_id=_required_id(view_id, "view_id"),
            strategy=strategy,
            profile=(SegmentationProfile.from_dict(profile) if profile else None),
            segments=segments,
            canonical_content_sha256=self._view_set.canonical_content_sha256,
        )
        self._validate_view(view)
        return view

    def _validate_bound_view_set(
        self,
        aggregate: SegmentationViewSet,
        *,
        require_production: bool,
    ) -> None:
        """Validate every view against this service's exact canonical binding.

        Fixture construction and the public production validator share this
        algorithm after aggregate schema validation. ``require_production`` keeps
        the compact frozen adapter private while ensuring reusable production sets
        carry their digest in the production shape. The helper compares immutable
        digests and checks references/semantics without changing records, calling
        parsers/networks/providers/LLMs, or persisting text. Typed shape or
        reference failures cross the trust boundary; concurrent reads are safe.
        """
        aggregate.validate()
        if require_production and (
            aggregate.canonical_content_artifact is not None
            or aggregate.invariant is not None
        ):
            raise SegmentationViewValidationError(
                "Canonical-bound reuse requires a production segmentation view set"
            )
        if aggregate.canonical_content_sha256 != self._view_set.canonical_content_sha256:
            raise SegmentationViewReferenceError(
                "Segmentation set does not match bound canonical content"
            )
        for view in aggregate.views:
            self._validate_view(view)

    def _validate_view(self, view: SegmentationView) -> None:
        """Enforce canonical references and declared strategy semantics.

        All loading and builder paths converge here. It validates every span and
        division, then dispatches to the declared strategy's canonical semantic
        checks. No-copy is structural: the closed segment/profile schemas and
        recursive prohibited-field check admit references and bounded metadata,
        not arbitrary source payloads. The helper is deterministic, read-only,
        thread-safe for immutable inputs, makes no external call, and raises typed
        reference or strategy failures without exposing canonical text.
        """
        view.validate()
        if view.canonical_content_sha256 != self._view_set.canonical_content_sha256:
            raise SegmentationViewReferenceError(
                f"View does not match bound canonical content: {view.view_id}"
            )
        for segment in view.segments:
            all_spans = (
                segment.node_spans
                + segment.context
                + segment.retrieval_node_spans
                + ((segment.seed,) if segment.seed is not None else ())
            )
            for span in all_spans:
                self._validate_span(span)
            for spans in (
                segment.node_spans,
                segment.context,
                segment.retrieval_node_spans,
            ):
                self._validate_span_order(spans)
            if segment.division_id is not None:
                self._validate_division(segment.division_id)
            if segment.return_scope is not None:
                self._validate_division(segment.return_scope.division_id)
        if view.strategy == "paragraph":
            self._validate_paragraph_view(view)
        elif view.strategy == "direct-division":
            self._validate_direct_division_view(view)
        elif view.strategy == "parser-native-structure":
            self._validate_native_view(view)
        elif view.strategy == "sentence-safe-fixed-size":
            self._validate_fixed_size_view(view)
        elif view.strategy == "sentence-window":
            self._validate_sentence_window_view(view)
        elif view.strategy == "parent-child":
            self._validate_parent_child_view(view)

    def _validate_span(self, span: NodeSpan) -> None:
        """Resolve a NodeSpan identity and prove its range fits canonical text.

        View validation and reconstruction share this lookup to avoid divergent
        bounds behavior. The helper reads only the in-memory catalog, mutates and
        persists nothing, makes no external call, and exposes no source text in
        typed failures.
        """
        text = self._catalog.texts.get(span.node_id)
        if text is None:
            raise SegmentationViewReferenceError(
                f"Unknown canonical node: {span.node_id}"
            )
        span.validate(text_length=len(text))

    def _validate_division(self, division_id: str) -> None:
        """Require one division identity in the bound canonical catalog.

        Direct and parent-child validation call this pure lookup. It performs no
        reconstruction or external call, stores no text, and raises a bounded
        typed reference failure when the identity is absent.
        """
        if division_id not in self._catalog.division_order:
            raise SegmentationViewReferenceError(
                f"Unknown canonical division: {division_id}"
            )

    def _validate_span_order(self, spans: tuple[NodeSpan, ...]) -> None:
        """Reject duplicate or noncanonical persisted span ordering.

        Bound view validation calls this for every repeated span role. The sort
        key follows canonical node order and then local character range, ensuring
        equivalent reference structures have one byte representation. The check
        is read-only, exposes no text, and performs no external call.
        """
        identities = tuple(
            (span.node_id, span.char_start, span.char_end) for span in spans
        )
        if len(set(identities)) != len(identities):
            raise SegmentationViewValidationError("Duplicate node span")
        expected = tuple(
            sorted(spans, key=self._span_sort_key)
        )
        if spans != expected:
            raise SegmentationViewValidationError(
                "Node spans are not in canonical order"
            )

    def _validate_paragraph_view(self, view: SegmentationView) -> None:
        """Require unique whole canonical paragraph nodes in a paragraph view.

        Canonical-bound loading and paragraph builders call this semantic check
        after generic shape/reference validation. Each segment must contain one
        whole-node span whose bound node kind is ``paragraph``; subsets are valid,
        but the same paragraph cannot appear twice. The algorithm reads only the
        immutable catalog, performs no external call or persistence, retains no
        source text, is deterministic and thread-safe, and raises typed strategy
        failures without exposing canonical values.
        """
        seen: set[str] = set()
        for segment in view.segments:
            if len(segment.node_spans) != 1:
                raise SegmentationStrategyError(
                    "Paragraph segments require exactly one node span"
                )
            span = segment.node_spans[0]
            if span.char_start is not None:
                raise SegmentationStrategyError(
                    "Paragraph segments require whole-node spans"
                )
            if self._catalog.node_kinds[span.node_id] != "paragraph":
                raise SegmentationStrategyError(
                    "Paragraph view references a non-paragraph node"
                )
            if span.node_id in seen:
                raise SegmentationStrategyError(
                    "Paragraph view repeats a canonical paragraph"
                )
            seen.add(span.node_id)

    def _validate_direct_division_view(self, view: SegmentationView) -> None:
        """Require exact whole-node direct ownership and canonical direct order.

        Canonical-bound readers and builders call this to distinguish direct
        division content from subtree content. Every segment's node IDs must equal
        the bound division's complete ``direct_nodes`` projection in exact order,
        with no character ranges. The check is deterministic, read-only,
        thread-safe, performs no parser/network/provider/LLM call, stores no text,
        and raises a typed strategy failure for tampering.
        """
        for segment in view.segments:
            expected = self._catalog.direct_node_ids[segment.division_id]
            actual = tuple(span.node_id for span in segment.node_spans)
            if actual != expected or any(
                span.char_start is not None for span in segment.node_spans
            ):
                raise SegmentationStrategyError(
                    "Direct-division segment does not match canonical direct ownership"
                )

    def _validate_fixed_size_view(self, view: SegmentationView) -> None:
        """Require ordered non-overlapping spans over canonical paragraphs only.

        Reloaded sentence-safe views and the fixed-size builder use this semantic
        proof without requiring a ``TokenCounter``. The algorithm flattens spans in
        persisted segment order, verifies every bound node kind is ``paragraph``,
        requires global canonical span order, and rejects overlapping ranges for
        the same node. Whole-node references cover ``[0, len(text))`` for overlap
        calculation only; text is never serialized or emitted in failures. The
        read-only check is deterministic, thread-safe, has no external side effects,
        and raises typed strategy/validation failures.
        """
        spans = tuple(
            span for segment in view.segments for span in segment.node_spans
        )
        if spans != tuple(sorted(spans, key=self._span_sort_key)):
            raise SegmentationViewValidationError(
                "Fixed-size spans are not in canonical order"
            )
        previous_end: dict[str, int] = {}
        for span in spans:
            if self._catalog.node_kinds[span.node_id] != "paragraph":
                raise SegmentationStrategyError(
                    "Fixed-size view references a non-paragraph node"
                )
            start = 0 if span.char_start is None else span.char_start
            end = (
                len(self._catalog.texts[span.node_id])
                if span.char_end is None
                else span.char_end
            )
            if start < previous_end.get(span.node_id, -1):
                raise SegmentationStrategyError(
                    "Fixed-size view contains overlapping canonical ranges"
                )
            previous_end[span.node_id] = end

    def _validate_sentence_window_view(self, view: SegmentationView) -> None:
        """Require whole paragraph seed/context roles and optional exact neighbours.

        Canonical-bound readers and sentence-window builders call this proof. Seeds
        and context must be whole canonical paragraph spans, the seed cannot recur
        in context, and generic span validation already requires unique canonical
        context order. When a profile is present, its before/after widths must
        reproduce the exact neighbouring paragraphs around each seed. The compact
        frozen profile-less view remains valid. The algorithm is read-only,
        deterministic, thread-safe, performs no external call or persistence,
        retains no text in records, and raises typed strategy failures.
        """
        paragraphs = self._ordered_nodes(kind="paragraph")
        positions = {node_id: index for index, node_id in enumerate(paragraphs)}
        before = view.profile.get("context_before") if view.profile else None
        after = view.profile.get("context_after") if view.profile else None
        for segment in view.segments:
            seed = segment.seed
            if seed.char_start is not None or self._catalog.node_kinds[seed.node_id] != "paragraph":
                raise SegmentationStrategyError(
                    "Sentence-window seed must be a whole paragraph"
                )
            if any(
                span.char_start is not None
                or self._catalog.node_kinds[span.node_id] != "paragraph"
                for span in segment.context
            ):
                raise SegmentationStrategyError(
                    "Sentence-window context must contain whole paragraphs"
                )
            if any(span.node_id == seed.node_id for span in segment.context):
                raise SegmentationStrategyError(
                    "Sentence-window seed cannot also be context"
                )
            if view.profile is not None:
                position = positions[seed.node_id]
                expected = (
                    paragraphs[max(0, position - before) : position]
                    + paragraphs[position + 1 : position + after + 1]
                )
                actual = tuple(span.node_id for span in segment.context)
                if actual != expected:
                    raise SegmentationStrategyError(
                        "Sentence-window context does not match its canonical profile"
                    )

    def _validate_parent_child_view(self, view: SegmentationView) -> None:
        """Require paragraph retrieval spans to belong to their return division.

        Canonical-bound readers and parent-child builders call this semantic check.
        Every retrieval span must reference a paragraph in the canonical subtree
        of the declared return division, covering both direct ownership and a
        documented ancestor relationship. The algorithm materializes no parent
        text, performs no parser/network/provider/LLM call or persistence, is
        deterministic and thread-safe, and raises typed strategy failures without
        exposing source values.
        """
        for segment in view.segments:
            return_nodes = set(
                self._catalog.subtree_node_ids[segment.return_scope.division_id]
            )
            for span in segment.retrieval_node_spans:
                if self._catalog.node_kinds[span.node_id] != "paragraph":
                    raise SegmentationStrategyError(
                        "Parent-child retrieval span must reference a paragraph"
                    )
                if span.node_id not in return_nodes:
                    raise SegmentationStrategyError(
                        "Parent-child retrieval span is unrelated to its return division"
                    )

    def _span_sort_key(self, span: NodeSpan) -> tuple[int, int, int, str]:
        """Return the canonical node/range order used by semantic validators.

        Fixed-size validation shares this pure key with the bound catalog so
        persisted segment boundaries cannot reorder source spans. Whole nodes sort
        before ranged spans in their node. The helper exposes no text, performs no
        external call or mutation, is deterministic/thread-safe, and relies on
        prior typed reference validation for node existence.
        """
        return (
            self._catalog.node_order[span.node_id],
            -1 if span.char_start is None else span.char_start,
            -1 if span.char_end is None else span.char_end,
            span.node_id,
        )

    def _validate_native_view(self, view: SegmentationView) -> None:
        """Bind parser-native pointers to a real retained T01 artifact identity.

        Parser-native loading, builders, and future verified reuse call this after
        shape validation. The algorithm resolves the profile artifact ID through
        either the frozen fixture marker or supplied T01 descriptor, proves a
        descriptor has not been aliased under another key, and checks every bounded
        chunk pointer. A chunker pointer is not falsely interpreted as a JSON
        pointer into the opaque parser-native payload: T01 native pointers and T06
        chunker pointers have related identity but distinct meaning. This read-only
        trust-boundary check never opens Storage, reparses source content, persists
        data, or calls a network/provider/LLM; it copies no native payload values.
        Immutable catalogs make concurrent reads safe. Missing identities raise
        ``SegmentationViewReferenceError`` and malformed pointers raise
        ``SegmentationStrategyError`` without exposing source or native text.
        """
        artifact_value = view.profile.get("native_artifact_id") if view.profile else None
        artifact_id = _required_id(artifact_value, "native_artifact_id")
        fixture_artifact = self._fixture_native_artifacts.get(artifact_id)
        descriptor = self._native_descriptors.get(artifact_id)
        if fixture_artifact is None and descriptor is None:
            raise SegmentationViewReferenceError(
                f"Unknown native artifact: {artifact_id}"
            )
        if descriptor is not None and descriptor.artifact_id != artifact_id:
            raise SegmentationViewReferenceError(
                "Native descriptor mapping identity does not match its key"
            )
        for segment in view.segments:
            pointer = segment.native_chunk_pointer
            _validate_native_pointer(pointer)

    def _ordered_nodes(self, *, kind: str) -> tuple[str, ...]:
        """Return canonical node IDs of one kind in stable source order.

        Strategy builders call this immutable catalog projection. Ordering uses
        canonical sequence and ID, not mapping insertion or parser execution. The
        helper has no side effects or external calls and does not return text.
        """
        return tuple(
            node_id
            for node_id, _ in sorted(
                self._catalog.node_order.items(), key=lambda item: (item[1], item[0])
            )
            if self._catalog.node_kinds[node_id] == kind
        )

    def _division_sort_key(self, division_id: str) -> tuple[int, str]:
        """Validate a division and return its deterministic canonical sort key.

        Direct-division generation calls this pure helper. It exposes no text,
        performs no external call, and raises a typed reference error for an
        unknown division rather than leaking a mapping ``KeyError``.
        """
        self._validate_division(division_id)
        return (self._catalog.division_order[division_id], division_id)

    def _find_segment(
        self,
        segment_id: str,
        *,
        view_id: str | None,
        view: SegmentationView | None,
    ) -> SegmentationSegment:
        """Resolve one segment and reject ambiguous unqualified identities.

        Reconstruction APIs use this deterministic read-only lookup. It performs
        no reconstruction or external call, stores no text, and raises typed
        reference/reconstruction errors rather than indexing failures.
        """
        requested = _required_id(segment_id, "segment_id")
        if view is not None and view_id is not None:
            raise SegmentationReconstructionError(
                "Specify either a view object or a catalog view ID"
            )
        if view is not None:
            self._validate_view(view)
            views = (view,)
        else:
            views = (
                (self.build(view_id),)
                if view_id is not None
                else self._view_set.views
            )
        matches = tuple(
            segment
            for view in views
            for segment in view.segments
            if segment.segment_id == requested
        )
        if not matches:
            raise SegmentationViewReferenceError(
                f"Unknown segmentation segment: {requested}"
            )
        if len(matches) > 1:
            raise SegmentationReconstructionError(
                f"Segment identity requires a view: {requested}"
            )
        return matches[0]


def _validate_segment_shape(segment: SegmentationSegment, strategy: str) -> None:
    """Enforce exact role combinations so strategies cannot silently collapse.

    This is the central strategy-shape invariant. Every branch explicitly permits
    only its semantic roles, requires non-empty references, and rejects accidental
    text-like or cross-strategy fields before reference validation.
    """
    _required_id(segment.segment_id, "segment_id")
    if strategy in {"paragraph", "sentence-safe-fixed-size"}:
        valid = bool(segment.node_spans) and not any(
            (
                segment.division_id,
                segment.native_chunk_pointer,
                segment.seed,
                segment.context,
                segment.retrieval_node_spans,
                segment.return_scope,
            )
        )
    elif strategy == "direct-division":
        valid = (
            bool(segment.node_spans)
            and segment.division_id is not None
            and not any(
                (
                    segment.native_chunk_pointer,
                    segment.seed,
                    segment.context,
                    segment.retrieval_node_spans,
                    segment.return_scope,
                )
            )
        )
    elif strategy == "parser-native-structure":
        valid = (
            bool(segment.node_spans)
            and segment.native_chunk_pointer is not None
            and not any(
                (
                    segment.division_id,
                    segment.seed,
                    segment.context,
                    segment.retrieval_node_spans,
                    segment.return_scope,
                )
            )
        )
        if valid:
            _validate_native_pointer(segment.native_chunk_pointer)
    elif strategy == "sentence-window":
        valid = (
            segment.seed is not None
            and not segment.node_spans
            and segment.division_id is None
            and segment.native_chunk_pointer is None
            and not segment.retrieval_node_spans
            and segment.return_scope is None
        )
    elif strategy == "parent-child":
        valid = (
            bool(segment.retrieval_node_spans)
            and segment.return_scope is not None
            and not segment.node_spans
            and segment.division_id is None
            and segment.native_chunk_pointer is None
            and segment.seed is None
            and not segment.context
        )
    else:
        raise SegmentationStrategyError("Unknown segmentation strategy")
    if not valid:
        raise SegmentationStrategyError(
            f"Segment does not match strategy shape: {segment.segment_id}"
        )
    for span in (
        segment.node_spans
        + segment.context
        + segment.retrieval_node_spans
        + ((segment.seed,) if segment.seed is not None else ())
    ):
        span.validate()


def _validate_profile(
    strategy: str, profile: SegmentationProfile | None
) -> None:
    """Require frozen metadata where semantics depend on it and reject extras.

    Strict readers and service builders use this strategy trust boundary after the
    profile value object has frozen scalar identity. The algorithm applies a closed
    per-strategy field set, requires complete parser-native, fixed-size, and present
    sentence-window profiles, and validates IDs and numeric bounds. Paragraph and
    direct views may retain implementation identity; parent-child carries no
    profile. The pure check mutates and persists nothing, retains no source text,
    performs no parser/network/provider/LLM call, and is deterministic and safe for
    concurrent immutable inputs. Violations raise ``SegmentationStrategyError``
    without echoing untrusted values.
    """
    keys = set(dict(profile.values)) if profile is not None else set()
    allowed_by_strategy = {
        "paragraph": {"implementation", "version"},
        "direct-division": {"implementation", "version"},
        "parser-native-structure": {"chunker_id", "native_artifact_id"},
        "sentence-safe-fixed-size": {"max_tokens", "tokenizer"},
        "sentence-window": {"context_before", "context_after"},
        "parent-child": set(),
    }
    allowed = allowed_by_strategy[strategy]
    if not keys.issubset(allowed):
        raise SegmentationStrategyError("Profile does not match strategy")
    for key, value in profile.values if profile is not None else ():
        if key in {
            "implementation",
            "version",
            "chunker_id",
            "native_artifact_id",
            "tokenizer",
        }:
            _required_id(value, key)
    if strategy == "parser-native-structure":
        if keys != allowed:
            raise SegmentationStrategyError(
                "Parser-native profile requires chunker and native artifact IDs"
            )
        _required_id(profile.get("chunker_id"), "chunker_id")
        _required_id(profile.get("native_artifact_id"), "native_artifact_id")
    elif strategy == "sentence-safe-fixed-size":
        if keys != allowed:
            raise SegmentationStrategyError(
                "Sentence-safe profile requires max_tokens and tokenizer"
            )
        max_tokens = profile.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise SegmentationStrategyError("max_tokens must be a positive integer")
        _required_id(profile.get("tokenizer"), "tokenizer")
    elif strategy == "sentence-window" and profile is not None:
        if keys != {"context_before", "context_after"}:
            raise SegmentationStrategyError(
                "Sentence-window profile requires both context widths"
            )
        for key in keys:
            _non_negative_int(profile.get(key), key)


def _validated_native_descriptor_map(
    value: Mapping[str, NativeArtifactDescriptor] | None,
) -> dict[str, NativeArtifactDescriptor]:
    """Copy and validate trusted T01 descriptor composition by exact identity.

    ``from_canonical`` calls this before canonical serialization or native view
    validation. Every mapping key must be a valid ID exactly equal to the enclosed
    immutable descriptor's ``artifact_id``; aliases are rejected rather than
    becoming another name for native evidence. The returned ordinary dictionary
    is a local snapshot used only for read validation. The algorithm performs no
    Storage access, parser/network/provider/LLM call, persistence, or mutation,
    retains no native payload, is deterministic/thread-safe after freezing in the
    service, and raises ``SegmentationViewReferenceError`` at this trust boundary
    without exposing source text.
    """
    if value is not None and not isinstance(value, Mapping):
        raise SegmentationViewReferenceError(
            "Native descriptor composition must be a mapping"
        )
    result: dict[str, NativeArtifactDescriptor] = {}
    for key, descriptor in (value or {}).items():
        if (
            not isinstance(key, str)
            or not isinstance(descriptor, NativeArtifactDescriptor)
            or key != descriptor.artifact_id
        ):
            raise SegmentationViewReferenceError(
                "Native descriptor mapping identity does not match its key"
            )
        _required_id(key, "native artifact ID")
        result[key] = descriptor
    return result


def _catalog_from_canonical(
    artifact: CanonicalContentArtifact,
    *,
    native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
) -> _ReferenceCatalog:
    """Project validated T02 records into immutable T06 lookup facts.

    The service calls this after exact-byte validation. It preserves node and
    division sequence, direct ownership, and subtree traversal without copying
    text into any view. Optional T01 descriptors keep canonical NativeBinding
    validation active. Text remains only in this runtime reconstruction catalog.
    """
    texts = {node.node_id: node.content.text for node in artifact.content_nodes}
    node_kinds = {node.node_id: node.node_kind for node in artifact.content_nodes}
    node_order = {node.node_id: node.sequence_number for node in artifact.content_nodes}
    node_owners = {
        node.node_id: node.owner_division_id for node in artifact.content_nodes
    }
    division_order = {
        division.division_id: division.sequence_number for division in artifact.divisions
    }
    direct_node_ids = {
        division.division_id: tuple(
            node.node_id
            for node in artifact.direct_nodes(
                division.division_id, native_descriptors=native_descriptors
            )
        )
        for division in artifact.divisions
    }
    subtree_node_ids = {
        division.division_id: tuple(
            node.node_id
            for node in artifact.subtree_nodes(
                division.division_id, native_descriptors=native_descriptors
            )
        )
        for division in artifact.divisions
    }
    return _freeze_catalog(
        texts,
        node_kinds,
        node_order,
        node_owners,
        division_order,
        direct_node_ids,
        subtree_node_ids,
    )


def _catalog_from_compact(
    canonical: Mapping[str, object], graph: Mapping[str, object]
) -> _ReferenceCatalog:
    """Adapt the frozen compact canonical/source-graph projections explicitly.

    The fixture does not pretend to be the complete production T02 artifact. This
    helper reads its exact nodes and the graph's exact divisions, verifies direct
    ownership, and constructs the same runtime catalog without changing fixture
    bytes or accepting a partially extended shape.
    """
    _require_exact_fields(canonical, {"schema", "invariant", "resources", "content_nodes"}, "compact canonical content")
    nodes = _mapping_array(canonical.get("content_nodes"), "content_nodes")
    divisions = _mapping_array(graph.get("divisions"), "divisions")
    texts: dict[str, str] = {}
    node_kinds: dict[str, str] = {}
    node_order: dict[str, int] = {}
    node_owners: dict[str, str] = {}
    for index, item in enumerate(nodes):
        node_id = _required_id(item.get("node_id"), "node_id")
        if node_id in texts:
            raise SegmentationFixtureError("Compact canonical content has duplicate node ID")
        content = _mapping(item.get("content"), "content")
        text = _required_text(content.get("text"), "canonical text")
        expected_digest = _sha256_value(content.get("sha256"))
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_digest:
            raise SegmentationFixtureError("Compact canonical text digest does not match")
        texts[node_id] = text
        node_kinds[node_id] = _required_text(item.get("node_kind"), "node_kind")
        node_order[node_id] = index
        node_owners[node_id] = _required_id(
            item.get("owner_division_id"), "owner_division_id"
        )
    division_order: dict[str, int] = {}
    direct_node_ids: dict[str, tuple[str, ...]] = {}
    child_ids: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(divisions):
        division_id = _required_id(item.get("division_id"), "division_id")
        division_order[division_id] = index
        direct = tuple(
            _required_id(node_id, "direct node ID")
            for node_id in _array(item.get("direct_node_ids"), "direct_node_ids")
        )
        if any(node_owners.get(node_id) != division_id for node_id in direct):
            raise SegmentationFixtureError("Compact division direct ownership is invalid")
        direct_node_ids[division_id] = direct
        child_ids[division_id] = tuple(
            _required_id(child, "child division ID")
            for child in _array(item.get("child_division_ids"), "child_division_ids")
        )
    subtree_node_ids = {
        division_id: _compact_subtree_nodes(
            division_id, direct_node_ids, child_ids, node_order
        )
        for division_id in division_order
    }
    return _freeze_catalog(
        texts,
        node_kinds,
        node_order,
        node_owners,
        division_order,
        direct_node_ids,
        subtree_node_ids,
    )


def _compact_subtree_nodes(
    division_id: str,
    direct_node_ids: Mapping[str, tuple[str, ...]],
    child_ids: Mapping[str, tuple[str, ...]],
    node_order: Mapping[str, int],
) -> tuple[str, ...]:
    """Reconstruct compact fixture subtree references with cycle protection.

    This mirrors production canonical subtree lookup for the explicit fixture
    adapter. It returns existing node IDs in canonical order and never creates a
    copied subtree payload.
    """
    pending = [division_id]
    visited: set[str] = set()
    nodes: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            raise SegmentationFixtureError("Compact division hierarchy contains a cycle")
        visited.add(current)
        if current not in direct_node_ids:
            raise SegmentationFixtureError("Compact division hierarchy has missing child")
        nodes.update(direct_node_ids[current])
        pending.extend(child_ids[current])
    return tuple(sorted(nodes, key=lambda node_id: (node_order[node_id], node_id)))


def _freeze_catalog(
    texts: Mapping[str, str],
    node_kinds: Mapping[str, str],
    node_order: Mapping[str, int],
    node_owners: Mapping[str, str],
    division_order: Mapping[str, int],
    direct_node_ids: Mapping[str, tuple[str, ...]],
    subtree_node_ids: Mapping[str, tuple[str, ...]],
) -> _ReferenceCatalog:
    """Copy lookup mappings into read-only proxies for service thread safety.

    Construction uses local copies so caller mutation cannot change canonical
    binding after validation. Text stays in the private runtime catalog and never
    enters view value objects or serialized output.
    """
    return _ReferenceCatalog(
        texts=MappingProxyType(dict(texts)),
        node_kinds=MappingProxyType(dict(node_kinds)),
        node_order=MappingProxyType(dict(node_order)),
        node_owners=MappingProxyType(dict(node_owners)),
        division_order=MappingProxyType(dict(division_order)),
        direct_node_ids=MappingProxyType(dict(direct_node_ids)),
        subtree_node_ids=MappingProxyType(dict(subtree_node_ids)),
    )


def _fixture_native_artifact_markers(
    root: Path, binding_value: Mapping[str, object]
) -> dict[str, object]:
    """Verify declared frozen native JSON payloads for artifact identity binding.

    The compact fixture adapter calls this bounded reader. It verifies artifact
    IDs, relative approved paths, exact SHA-256, and strict JSON, then retains only
    an in-memory identity marker. Native payload values never enter service state
    or a segmentation record.
    """
    artifacts = _mapping_array(binding_value.get("artifacts"), "artifacts")
    result: dict[str, object] = {}
    for artifact in artifacts:
        artifact_id = _required_id(artifact.get("artifact_id"), "artifact_id")
        relative = _required_text(artifact.get("path"), "native artifact path")
        if not relative.startswith("native_artifacts/") or ".." in relative.split("/"):
            raise SegmentationFixtureError("Compact native artifact path is not allowed")
        payload = _read_fixture_bytes(root, relative)
        if hashlib.sha256(payload).hexdigest() != _sha256_value(artifact.get("sha256")):
            raise SegmentationFixtureError("Compact native artifact digest does not match")
        _strict_json_loads(payload)
        result[artifact_id] = True
    return result


def _read_fixture_bytes(root: Path, relative: str) -> bytes:
    """Read one known relative frozen fixture file without exposing its path.

    ``from_fixture`` and native fixture loading call this helper. It rejects parent
    traversal and reads bytes only; callers wrap operating-system failures in the
    typed fixture error and no write occurs.
    """
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SegmentationFixtureError("Invalid frozen fixture reference")
    return root.joinpath(*parts).read_bytes()


def _sentence_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Find deterministic sentence-safe half-open ranges without a tokenizer.

    Fixed-size generation uses terminal punctuation followed by whitespace as a
    conservative boundary. Whitespace between sentences is excluded from both
    adjacent ranges, empty ranges are never returned, and source text is not
    retained by the output.
    """
    if not text:
        return ()
    ranges: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+", text):
        end = match.start()
        if end > start:
            ranges.append((start, end))
        start = match.end()
    if start < len(text):
        ranges.append((start, len(text)))
    return tuple(ranges)


def _strict_json_loads(payload: bytes) -> object:
    """Decode UTF-8 JSON while rejecting duplicate keys and non-finite numbers.

    Every T06 persisted reader uses this shared trust boundary. Duplicate object
    names fail before last-key-wins decoding can hide data; parser constants such
    as NaN also fail. Implementation exceptions become typed validation errors.
    """
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Build one mapping while rejecting a repeated name immediately."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SegmentationViewValidationError("Duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        """Reject NaN and infinity before they enter deterministic identities."""
        raise SegmentationViewValidationError(
            "Segmentation JSON contains a non-finite number"
        )

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except SegmentationViewError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SegmentationViewValidationError(
            "Segmentation payload is not valid strict JSON"
        ) from error


def _canonical_json_bytes(value: object) -> bytes:
    """Encode deterministic sorted-key JSON plus one trailing newline.

    Views, view sets, and cache identities share this serializer so equivalent
    reference structures always produce equal bytes. It performs no I/O or text
    reconstruction and cannot introduce source content absent from ``value``.
    """
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _reject_copy_fields(value: object) -> None:
    """Recursively reject fields capable of becoming another source-text owner.

    The closed value model already limits allowed fields; this defense-in-depth
    traversal proves nested serializers did not add a prohibited text/content/
    excerpt/body/embedding field under any record. It does not inspect or retain
    canonical source values.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _COPY_FIELD_NAMES:
                raise SegmentationViewValidationError(
                    "Segmentation records cannot contain source text fields"
                )
            _reject_copy_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_copy_fields(item)


def _validate_native_pointer(value: object) -> str:
    """Require one bounded URI-fragment JSON pointer without exposing payloads.

    Parser-native shape and binding validation call this helper. It accepts root
    ``#`` or escaped slash-separated tokens, limits length, and raises a typed
    strategy failure before any navigation.
    """
    if not isinstance(value, str) or len(value) > 1024 or not _NATIVE_POINTER_RE.fullmatch(value):
        raise SegmentationStrategyError("Native chunk pointer has invalid syntax")
    return value


def _parse_spans(value: object, field_name: str) -> tuple[NodeSpan, ...]:
    """Parse an optional ordered array of strict node-span mappings.

    Segment readers share this helper to preserve explicit role order. Missing
    fields become an empty tuple; malformed values raise typed validation errors.
    """
    if value is None:
        return ()
    return tuple(
        NodeSpan.from_dict(_mapping(item, field_name))
        for item in _array(value, field_name)
    )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    """Require a string-key mapping and hide implementation ``TypeError`` values.

    Strict nested readers call this pure type guard. It returns the same mapping,
    stores no text, and raises a typed validation error using only the field name.
    """
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SegmentationViewValidationError(f"{field_name} must be an object")
    return value


def _mapping_array(value: object, field_name: str) -> tuple[Mapping[str, object], ...]:
    """Require an array of mappings for compact fixture projections.

    The fixture adapter calls this deterministic type guard before extracting
    canonical IDs. It performs no I/O and raises typed validation failures.
    """
    return tuple(_mapping(item, field_name) for item in _array(value, field_name))


def _array(value: object, field_name: str) -> list[object]:
    """Require a JSON array without accepting strings or tuples silently.

    Persisted readers use this exact-shape helper. It returns the existing list,
    mutates nothing, retains no text, and raises a typed validation error.
    """
    if not isinstance(value, list):
        raise SegmentationViewValidationError(f"{field_name} must be an array")
    return value


def _require_exact_fields(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    """Reject missing and unknown persisted fields without silent normalization.

    Every complete T06 record reader calls this trust-boundary helper. It compares
    field sets exactly and emits a bounded typed failure, preserving input bytes.
    """
    if set(value) != expected:
        raise SegmentationViewValidationError(
            f"{label} has missing or unsupported fields"
        )


def _require_allowed_fields(
    value: Mapping[str, object], allowed: set[str], label: str
) -> None:
    """Reject unknown fields while strategy validation handles required subsets.

    Segment parsing uses this because each strategy has a different exact subset.
    At least ``segment_id`` is required immediately; no unknown payload can pass.
    """
    if "segment_id" not in value or not set(value).issubset(allowed):
        raise SegmentationViewValidationError(
            f"{label} has missing or unsupported fields"
        )


def _required_text(value: object, field_name: str) -> str:
    """Require bounded non-empty text metadata, never canonical payload content.

    T06 readers use this for IDs, schema names, and fixture declarations. It
    strips nothing and rejects malformed values with a typed bounded message.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SegmentationViewValidationError(f"{field_name} must be non-empty text")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    """Return absent metadata as ``None`` or validate bounded non-empty text.

    Segment readers call this pure helper for optional native pointers. It stores
    no payload and raises a typed validation error for malformed values.
    """
    if value is None:
        return None
    return _required_text(value, field_name)


def _required_id(value: object, field_name: str) -> str:
    """Require one bounded provider-neutral record identity.

    Builders and readers share this validation to prevent paths, whitespace, or
    unbounded values from entering errors and cache identities. No normalization
    occurs, so persisted identities remain exact.
    """
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SegmentationViewValidationError(f"{field_name} must be a valid ID")
    return value


def _optional_id(value: object, field_name: str) -> str | None:
    """Validate an optional record identity without inventing a default.

    Segment parsing uses this pure helper. It returns ``None`` only for absence and
    delegates present values to strict ID validation.
    """
    return None if value is None else _required_id(value, field_name)


def _optional_int(value: object, field_name: str) -> int | None:
    """Validate an optional exact integer while rejecting booleans.

    NodeSpan readers use this for character endpoints. It performs no conversion,
    mutation, or external call and raises typed validation errors.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SegmentationViewValidationError(f"{field_name} must be an integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    """Require an exact non-negative integer for deterministic strategy settings.

    Sentence-window builders and profile validation call this helper. It rejects
    booleans and conversions, preserving deterministic caller intent.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SegmentationStrategyError(f"{field_name} must be non-negative")
    return value


def _json_scalar(value: object, field_name: str) -> JSONScalar:
    """Accept only finite JSON scalars for bounded strategy profile metadata.

    Profile construction calls this helper to exclude arrays, mappings, payloads,
    and non-finite numbers. Values are returned unchanged for deterministic JSON.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise SegmentationViewValidationError(f"{field_name} must be a JSON scalar")


def _sha256_value(value: object) -> str:
    """Require an exact lowercase SHA-256 digest for canonical identity binding.

    View and view-set validation use this helper. It performs no hashing itself,
    conversion, or normalization and exposes no source bytes in failures.
    """
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SegmentationViewValidationError(
            "Canonical content digest must be lowercase SHA-256"
        )
    return value


def _validate_ordered_unique(values: tuple[str, ...], label: str) -> None:
    """Reject duplicate or non-lexical persisted identity order.

    View validation calls this strict-reader invariant. Builders deliberately
    construct lexical IDs, so rejecting other persisted order prevents equivalent
    artifacts from acquiring different bytes.
    """
    if len(set(values)) != len(values):
        raise SegmentationViewValidationError(f"Duplicate {label}")
    if values != tuple(sorted(values)):
        raise SegmentationViewValidationError(f"{label} are not in canonical order")
