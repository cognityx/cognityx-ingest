"""Define and build the additive v3.2 parser-neutral canonical content model.

Purpose
-------
The existing v2 ``document.json`` is a stable compatibility artifact, but its
shape mixes presentation, logical structure, and migration-era text copies. This
module adds ``canonical-content.json`` rather than silently reinterpreting v2.
It gives later Cognityx stages one parser-neutral source model while v2 document,
evidence, provenance, and manifest artifacts remain readable projections.

Design principles
-----------------
Extracted source text has exactly one owner inside this artifact:
``ContentNode.content.text``. A ``PresentationUnit`` says where something
appeared, such as a page or slide. A ``Division`` says how content is organized,
such as a document, section, clause, or appendix. Divisions retain direct node
IDs and child division IDs; they never copy titles or subtree text. Selectors,
representations, relations, activities, bindings, and artifact descriptors carry
references and facts rather than quoted source content.

Processing flow
---------------
``CanonicalContentBuilder`` creates one resource, maps current pages to
presentation units, creates a document-root division and logical child
divisions, assigns every block to its deepest explicit section owner, hashes the
exact UTF-8 text, and records only source facts that already exist. It then maps
safe relations, representations, processing lineage, generic artifact
descriptors, and optional caller-supplied native bindings. The completed frozen
aggregate is validated before deterministic serialization and immutable storage.

Direct versus subtree content
-----------------------------
Direct content belongs to exactly one deepest division. Subtree lookup walks
child divisions and returns existing node records in deterministic source order;
it does not materialize another stored text field. Equal text at two source
locations remains two nodes because identity follows occurrence and selectors,
not string equality.

NativeBinding and T01
---------------------
A ``NativeBinding`` says that one canonical record is supported by an exact
object or location inside a T01 parser-native artifact. Validation uses the T01
``NativeArtifactDescriptor`` and its retained pointers. This module neither
copies native bytes nor implements another native reader or JSON-pointer parser.

Primary consumers
-----------------
Ingest persistence writes the artifact. T06 will build non-copying segmentation
views from node IDs and spans. T08 will build Source Graph and provenance-address
services around these records. DataForge consumes later handoff projections,
not parser-native payloads, for paragraph Q/A and Knowledge Unit work.

Ownership boundary
------------------
Cognityx Ingest owns canonical records, structure, selectors, bindings, and
validation. Cognityx Storage owns physical immutable objects. Parser libraries
own native payload meaning. DataForge owns semantic Knowledge Graph, Q/A,
Knowledge Units, embeddings, and training records.

Non-goals
---------
This module does not add parser capability discovery, adaptive routing, fusion
redesign, segmentation, retention or purge policy, Source Graph repositories,
address resolution, graph traversal, DataForge processing, SDK commands, parser
private imports, embeddings, vector stores, tokenizers, or graph databases.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from cognityx_resource import ExecutionContext

from cognityx_ingest.models import Block, CanonicalDocument, SourceAsset
from cognityx_ingest.native_artifacts import NativeArtifactDescriptor


CANONICAL_CONTENT_SCHEMA_VERSION = "cognityx.ingest.canonical-content/v3.2"


class CanonicalContentError(Exception):
    """Base failure for canonical-content construction and persistence.

    Responsibility:
        Give callers one stable domain exception instead of leaking ``KeyError``,
        JSON implementation errors, or assertion failures.
    Constructed by:
        Public builders, readers, validators, and their bounded helpers.
    Used by:
        Ingest orchestration, artifact readers, tests, and future SDK adapters.
    Invariants:
        Messages identify logical records and never include source payloads,
        parser-native bytes, credentials, or local operating-system paths.
    Lifecycle/persistence:
        Instances are transient and are never serialized into the artifact.
    Thread-safety assumptions:
        Exceptions carry immutable diagnostic text and no shared mutable state.
    """


class CanonicalContentValidationError(CanonicalContentError):
    """Report malformed values or inconsistent canonical-content records.

    Responsibility:
        Distinguish invalid persisted content from storage and parser failures.
    Constructed by:
        ``CanonicalContentArtifact.from_dict`` and ``validate``.
    Used by:
        Trust-boundary readers and build-time validation.
    Invariants:
        Invalid artifacts are never returned as validated aggregates.
    Lifecycle/persistence:
        Validation is read-only and does not repair stored records.
    Thread-safety assumptions:
        The exception contains no mutable shared state.
    """


class CanonicalReferenceError(CanonicalContentValidationError):
    """Report a canonical ID that does not resolve to its required record.

    Responsibility:
        Make missing resource, presentation, node, relation, selector, or
        artifact references explicit.
    Constructed by:
        Aggregate reference-index validation.
    Used by:
        Builders, readers, and future graph or segmentation consumers.
    Invariants:
        A validated artifact has no dangling required canonical references.
    Lifecycle/persistence:
        This transient error never mutates the aggregate.
    Thread-safety assumptions:
        The exception has ordinary immutable exception semantics.
    """


class CanonicalOwnershipError(CanonicalContentValidationError):
    """Report invalid Division hierarchy or ContentNode ownership.

    Responsibility:
        Protect the deepest-direct-owner rule and non-copying subtree model.
    Constructed by:
        Hierarchy, cycle, parent-child, and direct-node validation.
    Used by:
        Canonical builders and consumers that reconstruct logical content.
    Invariants:
        Each validated content node appears directly in exactly one same-resource
        division, and the division graph is acyclic and internally consistent.
    Lifecycle/persistence:
        Detection is read-only; invalid data remains unchanged for diagnosis.
    Thread-safety assumptions:
        The exception contains no mutable shared state.
    """


class NativeBindingValidationError(CanonicalContentValidationError):
    """Report a canonical-to-native binding that cannot be trusted.

    Responsibility:
        Ensure canonical IDs, T01 artifact descriptors, and native pointers all
        resolve before a binding is accepted.
    Constructed by:
        ``CanonicalContentArtifact.validate`` during optional binding checks.
    Used by:
        Parser adapters, audit tooling, and future binding readers.
    Invariants:
        Validation never copies native bytes or implements parser-private pointer
        semantics; retained pointers remain governed by T01.
    Lifecycle/persistence:
        The error is transient and binding validation is read-only.
    Thread-safety assumptions:
        The exception contains no mutable shared state.
    """


@dataclass(frozen=True, slots=True)
class CanonicalResource:
    """Represent one logical source resource without embedding source bytes.

    Responsibility:
        Bind the v3.2 model to stable SourceAsset identity, hash, media metadata,
        filename, and provider-neutral logical URI.
    Constructed by:
        ``CanonicalContentBuilder`` or strict artifact deserialization.
    Used by:
        Every presentation unit, division, node, selector, and lineage consumer.
    Invariants:
        IDs and metadata are non-empty and the SHA-256 identifies immutable
        SourceAsset bytes held outside this artifact.
    Lifecycle/persistence:
        The frozen record is serialized inside ``canonical-content.json``.
    Thread-safety assumptions:
        All fields are immutable strings, so records are safe to share.
    """

    resource_id: str
    source_asset_id: str
    source_sha256: str
    media_type: str
    original_filename: str
    logical_uri: str


@dataclass(frozen=True, slots=True)
class PresentationUnit:
    """Describe where content appeared without storing extracted text.

    Responsibility:
        Model a page, slide, sheet, frame, time range, or document-level surface.
    Constructed by:
        The builder from current page facts or future format adapters.
    Used by:
        Source selectors, layout-aware consumers, and future Source Graph work.
    Invariants:
        The resource exists, sequence numbers are non-negative, and physical
        dimensions or indexes are recorded only when observed.
    Lifecycle/persistence:
        Frozen records are additive v3.2 metadata and never replace v2 pages.
    Thread-safety assumptions:
        Tuples and scalar fields are immutable and safe for concurrent reads.
    """

    presentation_unit_id: str
    resource_id: str
    unit_type: str
    sequence_number: int
    physical_index: int | None = None
    labels: tuple[str, ...] = ()
    width: float | None = None
    height: float | None = None


@dataclass(frozen=True, slots=True)
class Division:
    """Represent extensible logical structure without copying title or subtree text.

    Responsibility:
        Model document, section, subsection, clause, appendix, chapter, policy
        rule, or another role through one generalized record.
    Constructed by:
        The builder from the current section hierarchy or future adapters.
    Used by:
        Direct/subtree lookup, T06 views, T08 graph work, and DataForge handoff.
    Invariants:
        Parent and child links are reciprocal, hierarchy is acyclic, title points
        to a ContentNode, and direct nodes have this division as deepest owner.
    Lifecycle/persistence:
        Divisions persist references only; parent content is reconstructed.
    Thread-safety assumptions:
        Frozen scalar and tuple fields make records safe for shared reads.
    """

    division_id: str
    resource_id: str
    division_role: str
    parent_division_id: str | None
    child_division_ids: tuple[str, ...]
    title_node_id: str | None
    direct_node_ids: tuple[str, ...]
    sequence_number: int
    number: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalText:
    """Own one exact extracted text value and its UTF-8 SHA-256.

    Responsibility:
        Be the only record in ``canonical-content.json`` that stores source text.
    Constructed by:
        The builder from authoritative blocks or strict deserialization.
    Used by:
        Content nodes and consumers that verify exact source content.
    Invariants:
        ``sha256`` always hashes the exact UTF-8 bytes of ``text``.
    Lifecycle/persistence:
        Text remains embedded once per source occurrence while the artifact lives.
    Thread-safety assumptions:
        Strings are immutable and safe to share across readers.
    """

    text: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SourceSelector:
    """Identify a real source location without quoting the selected text.

    Responsibility:
        Retain page, source path, actual character range, geometry, or current v2
        anchor references that locate one canonical occurrence.
    Constructed by:
        The builder only from observed facts, or strict deserialization.
    Used by:
        Audit, provenance, future segmentation, and source-address consumers.
    Invariants:
        Resource and presentation references resolve; ranges and bounding boxes
        are ordered; at least one concrete locator is present.
    Lifecycle/persistence:
        Selectors live inside ContentNodes and never embed quoted source text.
    Thread-safety assumptions:
        Frozen tuples and scalars are safe for concurrent reads.
    """

    selector_id: str
    selector_type: str
    resource_id: str
    presentation_unit_id: str | None = None
    source_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_anchor_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentNode:
    """Represent one authoritative text-bearing occurrence in canonical order.

    Responsibility:
        Associate exact canonical text with one resource, one deepest logical
        owner, source selectors, semantic kind, and deterministic sequence.
    Constructed by:
        The builder from current canonical blocks or future parser-neutral input.
    Used by:
        Divisions, representations, relations, bindings, T06, T08, and handoff.
    Invariants:
        The owner and resource resolve, content hash matches, selectors locate the
        same resource, and the node appears directly in exactly one division.
    Lifecycle/persistence:
        A frozen node persists one source occurrence; equal strings are not merged.
    Thread-safety assumptions:
        Nested records and tuples are immutable and safe for shared reads.
    """

    node_id: str
    resource_id: str
    owner_division_id: str
    node_kind: str
    content: CanonicalText
    source_selectors: tuple[SourceSelector, ...]
    sequence_number: int


@dataclass(frozen=True, slots=True)
class Representation:
    """Reference a non-text or artifact-backed form without copying object content.

    Responsibility:
        Describe image, table, audio, layout, or another representation of an
        existing canonical subject through IDs and media metadata.
    Constructed by:
        The builder from current canonical objects or future adapters.
    Used by:
        Audit, T08 Source Graph work, and downstream representation consumers.
    Invariants:
        Subject, selectors, optional caption node, and optional artifact resolve.
    Lifecycle/persistence:
        Frozen records persist references; payloads remain separate artifacts.
    Thread-safety assumptions:
        Immutable scalar and tuple fields are safe for concurrent reads.
    """

    representation_id: str
    subject_id: str
    representation_type: str
    media_type: str
    artifact_id: str | None = None
    selector_ids: tuple[str, ...] = ()
    caption_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class NativeBinding:
    """Bind a canonical record to an exact pointer in a T01 native artifact.

    Responsibility:
        Preserve parser-native support for a canonical ID without embedding the
        parser payload or creating another native reader.
    Constructed by:
        Explicit parser-adapter or caller input to ``CanonicalContentBuilder``.
    Used by:
        Audit tooling, T08 provenance work, and future native-aware readers.
    Invariants:
        Canonical ID, T01 descriptor, and retained native pointer all resolve.
    Lifecycle/persistence:
        The frozen binding may outlive a parser run; T07 governs native retention.
    Thread-safety assumptions:
        String-only immutable records are safe for concurrent reads.
    """

    binding_id: str
    canonical_id: str
    artifact_id: str
    native_pointer: str
    binding_role: str


@dataclass(frozen=True, slots=True)
class CanonicalRelation:
    """Represent a safely mapped canonical relationship without free target text.

    Responsibility:
        Retain typed source/target references, status, epistemic state, and
        supporting content-node IDs while preserving unresolved outcomes.
    Constructed by:
        The builder from current relations only when source mapping is safe.
    Used by:
        Audit and later T08 graph/address work; not a graph traversal engine.
    Invariants:
        Source and concrete targets resolve; target may be absent only for an
        explicitly unresolved, ambiguous, contradicted, or rejected relation.
    Lifecycle/persistence:
        Frozen records remain auditable and never copy legacy ``target_text``.
    Thread-safety assumptions:
        Immutable scalar and tuple fields are safe for shared reads.
    """

    relation_id: str
    source_id: str
    target_id: str | None
    relation_type: str
    status: str
    epistemic_state: str
    evidence_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessingActivity:
    """Describe the normalization activity that produced canonical content.

    Responsibility:
        Connect execution identity, method, parsers, and input/output artifacts.
    Constructed by:
        ``CanonicalContentBuilder`` once per produced aggregate.
    Used by:
        Provenance, audit, T08 Source Graph, and downstream handoff readers.
    Invariants:
        Referenced artifacts exist in the aggregate and IDs contain no payloads.
    Lifecycle/persistence:
        The frozen activity persists as compact lineage, not execution logs.
    Thread-safety assumptions:
        Tuples and strings are immutable and safe for concurrent reads.
    """

    activity_id: str
    activity_type: str
    run_id: str
    correlation_id: str
    method: str
    parser_ids: tuple[str, ...]
    input_artifact_ids: tuple[str, ...]
    output_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalArtifactDescriptor:
    """Reference a canonical or supporting artifact without duplicating its bytes.

    Responsibility:
        Provide generic role, URI, media type, optional hash, and schema metadata.
    Constructed by:
        The builder from known v2, v3.2, source, and T01 artifact references.
    Used by:
        Processing activities, provenance, and future artifact readers.
    Invariants:
        IDs and provider-neutral URIs are stable; T01 descriptor semantics are not
        reimplemented in this generic reference.
    Lifecycle/persistence:
        The frozen descriptor lives in canonical content while payloads stay in
        their independently governed Storage objects.
    Thread-safety assumptions:
        Immutable scalar fields are safe for concurrent reads.
    """

    artifact_id: str
    role: str
    uri: str
    media_type: str
    sha256: str | None = None
    schema_version: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalContentArtifact:
    """Aggregate, validate, serialize, and navigate one v3.2 source model.

    Responsibility:
        Hold all parser-neutral records for one ingested document and enforce
        cross-record integrity before persistence or use.
    Constructed by:
        ``CanonicalContentBuilder`` or strict ``from_dict`` deserialization.
    Used by:
        Ingest persistence, T06 segmentation, T08 graph/address work, audit, and
        later DataForge handoff projections.
    Invariants:
        IDs are unique, references resolve, hierarchy is acyclic, each node has
        one deepest owner, hashes/selectors are valid, and ordering is canonical.
    Lifecycle/persistence:
        The frozen aggregate serializes immutably to ``canonical-content.json``;
        lookup indexes are rebuilt in memory and never persisted.
    Thread-safety assumptions:
        Records are immutable. Methods build local indexes and mutate no state,
        so concurrent reads are safe.
    """

    schema: str
    document_id: str
    resources: tuple[CanonicalResource, ...]
    presentation_units: tuple[PresentationUnit, ...]
    divisions: tuple[Division, ...]
    content_nodes: tuple[ContentNode, ...]
    representations: tuple[Representation, ...]
    native_bindings: tuple[NativeBinding, ...]
    relations: tuple[CanonicalRelation, ...]
    processing_activities: tuple[ProcessingActivity, ...]
    artifact_descriptors: tuple[CanonicalArtifactDescriptor, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the complete deterministic JSON-compatible representation.

        Artifact persistence and API readers call this after ``validate``. The
        algorithm serializes every frozen record in already-validated canonical
        order, converts tuples to lists, and retains optional values explicitly.
        It performs no I/O, is idempotent, and never emits local paths or payload
        bytes outside ``ContentNode.content.text``.
        """
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "resources": [_resource_to_dict(item) for item in self.resources],
            "presentation_units": [
                _presentation_unit_to_dict(item) for item in self.presentation_units
            ],
            "divisions": [_division_to_dict(item) for item in self.divisions],
            "content_nodes": [_content_node_to_dict(item) for item in self.content_nodes],
            "representations": [
                _representation_to_dict(item) for item in self.representations
            ],
            "native_bindings": [
                _native_binding_to_dict(item) for item in self.native_bindings
            ],
            "relations": [_relation_to_dict(item) for item in self.relations],
            "processing_activities": [
                _activity_to_dict(item) for item in self.processing_activities
            ],
            "artifact_descriptors": [
                _artifact_descriptor_to_dict(item)
                for item in self.artifact_descriptors
            ],
        }

    def to_json_bytes(
        self,
        *,
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    ) -> bytes:
        """Serialize canonical content to stable UTF-8 JSON bytes.

        Ingest persistence and integrity tests call this method. It validates the
        aggregate, sorts mapping keys, uses compact separators, preserves Unicode,
        and appends one newline. Repeated calls return identical bytes and have no
        side effects. Typed validation failures are raised before serialization.
        """
        self.validate(native_descriptors=native_descriptors)
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    ) -> "CanonicalContentArtifact":
        """Parse and validate one untrusted complete persisted artifact.

        Artifact readers call this at the JSON trust boundary. The algorithm
        checks the exact top-level field set, parses every nested field into a
        concrete immutable type, then runs all cross-reference validation using
        optional T01 descriptors for NativeBindings. It performs no writes and is
        idempotent. Malformed input raises typed canonical errors rather than raw
        ``KeyError``, ``TypeError``, or assertion failures.
        """
        try:
            _require_exact_fields(
                value,
                {
                    "schema",
                    "document_id",
                    "resources",
                    "presentation_units",
                    "divisions",
                    "content_nodes",
                    "representations",
                    "native_bindings",
                    "relations",
                    "processing_activities",
                    "artifact_descriptors",
                },
                "canonical content artifact",
            )
            artifact = cls(
                schema=_required_text(value["schema"], "schema"),
                document_id=_required_text(value["document_id"], "document_id"),
                resources=tuple(
                    _parse_resource(item)
                    for item in _mapping_items(value["resources"], "resources")
                ),
                presentation_units=tuple(
                    _parse_presentation_unit(item)
                    for item in _mapping_items(
                        value["presentation_units"], "presentation_units"
                    )
                ),
                divisions=tuple(
                    _parse_division(item)
                    for item in _mapping_items(value["divisions"], "divisions")
                ),
                content_nodes=tuple(
                    _parse_content_node(item)
                    for item in _mapping_items(
                        value["content_nodes"], "content_nodes"
                    )
                ),
                representations=tuple(
                    _parse_representation(item)
                    for item in _mapping_items(
                        value["representations"], "representations"
                    )
                ),
                native_bindings=tuple(
                    _parse_native_binding(item)
                    for item in _mapping_items(
                        value["native_bindings"], "native_bindings"
                    )
                ),
                relations=tuple(
                    _parse_relation(item)
                    for item in _mapping_items(value["relations"], "relations")
                ),
                processing_activities=tuple(
                    _parse_activity(item)
                    for item in _mapping_items(
                        value["processing_activities"], "processing_activities"
                    )
                ),
                artifact_descriptors=tuple(
                    _parse_artifact_descriptor(item)
                    for item in _mapping_items(
                        value["artifact_descriptors"], "artifact_descriptors"
                    )
                ),
            )
        except CanonicalContentError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise CanonicalContentValidationError(
                "Canonical content contains malformed nested values"
            ) from error
        artifact.validate(native_descriptors=native_descriptors)
        return artifact

    def validate(
        self,
        *,
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    ) -> None:
        """Validate identity, ownership, hierarchy, selectors, and lineage.

        Builders and untrusted artifact readers call this before persistence or
        use. The algorithm builds local ID indexes, checks deterministic ordering,
        validates resource and presentation references, detects hierarchy cycles,
        proves exactly-one direct ownership, verifies UTF-8 hashes and selectors,
        checks representations, relations, activities, and optional T01 bindings.
        It has no side effects and is idempotent. Typed validation subclasses
        identify reference, ownership, or binding failures without exposing text.
        """
        if self.schema != CANONICAL_CONTENT_SCHEMA_VERSION:
            raise CanonicalContentValidationError(
                f"Unsupported canonical content schema: {self.schema}"
            )
        _required_text(self.document_id, "document_id")
        _validate_record_shapes(self)
        indexes = _build_indexes(self)
        _validate_ordering(self)
        _validate_resources_and_presentation(self, indexes)
        _validate_divisions_and_ownership(self, indexes)
        _validate_content_nodes(self, indexes)
        _validate_representations(self, indexes)
        _validate_relations(self, indexes)
        _validate_activities(self, indexes)
        _validate_native_bindings(
            self,
            indexes,
            native_descriptors=native_descriptors,
        )

    def direct_nodes(
        self,
        division_id: str,
        *,
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    ) -> tuple[ContentNode, ...]:
        """Return existing nodes owned directly by one Division.

        T06, T08, audit, and handoff code call this when they need only a
        division's deepest-owned content. The method validates the aggregate,
        resolves ``direct_node_ids``, and returns immutable node references in
        deterministic source order. Bound artifacts accept their authoritative
        T01 descriptors through ``native_descriptors`` so the same trust checks
        remain active. It stores no reconstructed text and raises typed canonical
        validation errors or ``CanonicalReferenceError`` for an unknown division.
        """
        self.validate(native_descriptors=native_descriptors)
        divisions = {item.division_id: item for item in self.divisions}
        nodes = {item.node_id: item for item in self.content_nodes}
        division = divisions.get(division_id)
        if division is None:
            raise CanonicalReferenceError(f"Unknown division: {division_id}")
        return tuple(nodes[node_id] for node_id in division.direct_node_ids)

    def subtree_nodes(
        self,
        division_id: str,
        *,
        native_descriptors: Mapping[str, NativeArtifactDescriptor] | None = None,
    ) -> tuple[ContentNode, ...]:
        """Return referenced nodes in a Division subtree without copying text.

        Structural and segmentation consumers call this for parent reconstruction.
        The algorithm walks child division IDs, collects existing direct-node
        references once, and sorts them by canonical sequence and ID. Validation
        prevents cycles before traversal. Bound artifacts accept T01 descriptors
        through ``native_descriptors``. The read-only result is deterministic;
        typed canonical validation errors protect the trust boundary and an
        unknown division raises ``CanonicalReferenceError``.
        """
        self.validate(native_descriptors=native_descriptors)
        divisions = {item.division_id: item for item in self.divisions}
        nodes = {item.node_id: item for item in self.content_nodes}
        root = divisions.get(division_id)
        if root is None:
            raise CanonicalReferenceError(f"Unknown division: {division_id}")
        collected: set[str] = set()
        pending = [root]
        while pending:
            current = pending.pop()
            collected.update(current.direct_node_ids)
            pending.extend(divisions[child] for child in current.child_division_ids)
        return tuple(
            sorted(
                (nodes[node_id] for node_id in collected),
                key=lambda item: (item.sequence_number, item.node_id),
            )
        )


@dataclass(frozen=True, slots=True)
class CanonicalContentBuilder:
    """Build one validated v3.2 aggregate from current parser-neutral records.

    Responsibility:
        Project the current ``CanonicalDocument`` and ``SourceAsset`` into the
        additive model while preserving stable IDs and exact text occurrences.
    Constructed by:
        ``IngestService`` or parser-neutral tests and future adapters.
    Used by:
        Normal production persistence and future adapters that can supply
        explicit NativeBindings.
    Invariants:
        The builder never imports parser-private classes, invents character
        offsets or native pointers, merges equal text occurrences, or copies text
        into non-content records.
    Lifecycle/persistence:
        The stateless frozen service returns a validated aggregate; persistence is
        owned by the caller.
    Thread-safety assumptions:
        It stores no mutable state and is safe to reuse across concurrent builds.
    """

    def build(
        self,
        document: CanonicalDocument,
        source_asset: SourceAsset,
        execution_context: ExecutionContext,
        *,
        native_descriptors: Sequence[NativeArtifactDescriptor] = (),
        native_bindings: Sequence[NativeBinding] = (),
        artifact_descriptors: Sequence[CanonicalArtifactDescriptor] = (),
    ) -> CanonicalContentArtifact:
        """Project v2 source records into one validated additive v3.2 artifact.

        ``IngestService`` calls this after current document/evidence and T01 native
        artifacts have stable identities. The algorithm maps the SourceAsset,
        pages, root/section hierarchy, deepest block ownership, exact UTF-8 text,
        observed selectors, non-text representations, safe relations, activity,
        and generic descriptors. Explicit bindings are accepted through this
        documented argument; absent bindings are valid.

        Args:
            document: Current parser-neutral v2 compatibility document.
            source_asset: Registered immutable source metadata.
            execution_context: Run and correlation identity for lineage.
            native_descriptors: T01 descriptors available to artifact references
                and binding validation.
            native_bindings: Explicit adapter-supplied canonical/native links.
            artifact_descriptors: Additional caller-known supporting artifacts.

        Returns:
            A fully validated immutable ``CanonicalContentArtifact``.

        Side effects and idempotency:
            The method performs no I/O. Equivalent inputs produce equivalent
            records and deterministic bytes; source and native payloads are not
            modified or copied.

        Raises:
            CanonicalContentValidationError: Generated facts violate the model.
            NativeBindingValidationError: A supplied binding cannot be proven
                against the supplied T01 descriptors.
        """
        resource = _build_resource(document, source_asset)
        presentation_units = _build_presentation_units(document, resource.resource_id)
        divisions, owner_by_node = _build_divisions(document, resource.resource_id)
        content_nodes = _build_content_nodes(
            document,
            resource.resource_id,
            owner_by_node,
        )
        representations = _build_representations(
            document,
            resource.resource_id,
            divisions,
            content_nodes,
        )
        relations = _build_relations(
            document,
            resource,
            presentation_units,
            divisions,
            content_nodes,
            representations,
        )
        descriptors = _build_artifact_descriptors(
            document,
            source_asset,
            native_descriptors,
            artifact_descriptors,
        )
        canonical_artifact_id = f"art-{document.document_id}-canonical_content"
        input_artifact_ids = tuple(
            item.artifact_id
            for item in descriptors
            if item.artifact_id != canonical_artifact_id
            and item.role in {"source", "document", "evidence", "parser_native"}
        )
        activity = ProcessingActivity(
            activity_id=f"{document.document_id}:activity:canonical-content",
            activity_type="canonical-content-build",
            run_id=execution_context.run_id,
            correlation_id=execution_context.correlation_id,
            method="deterministic-v2-projection",
            parser_ids=tuple(sorted({item.parser_id for item in native_descriptors})),
            input_artifact_ids=input_artifact_ids,
            output_artifact_ids=(canonical_artifact_id,),
        )
        artifact = CanonicalContentArtifact(
            schema=CANONICAL_CONTENT_SCHEMA_VERSION,
            document_id=document.document_id,
            resources=(resource,),
            presentation_units=presentation_units,
            divisions=divisions,
            content_nodes=content_nodes,
            representations=representations,
            native_bindings=tuple(sorted(native_bindings, key=lambda item: item.binding_id)),
            relations=relations,
            processing_activities=(activity,),
            artifact_descriptors=descriptors,
        )
        artifact.validate(
            native_descriptors={item.artifact_id: item for item in native_descriptors}
        )
        return artifact


def _resource_to_dict(item: CanonicalResource) -> dict[str, object]:
    """Serialize resource identity without exposing source bytes or local paths."""
    return {
        "resource_id": item.resource_id,
        "source_asset_id": item.source_asset_id,
        "source_sha256": item.source_sha256,
        "media_type": item.media_type,
        "original_filename": item.original_filename,
        "logical_uri": item.logical_uri,
    }


def _presentation_unit_to_dict(item: PresentationUnit) -> dict[str, object]:
    """Serialize presentation facts while guaranteeing no page text field exists."""
    return {
        "presentation_unit_id": item.presentation_unit_id,
        "resource_id": item.resource_id,
        "unit_type": item.unit_type,
        "sequence_number": item.sequence_number,
        "physical_index": item.physical_index,
        "labels": list(item.labels),
        "width": item.width,
        "height": item.height,
    }


def _division_to_dict(item: Division) -> dict[str, object]:
    """Serialize hierarchy and node references without title or subtree text."""
    return {
        "division_id": item.division_id,
        "resource_id": item.resource_id,
        "division_role": item.division_role,
        "parent_division_id": item.parent_division_id,
        "child_division_ids": list(item.child_division_ids),
        "title_node_id": item.title_node_id,
        "number": item.number,
        "label": item.label,
        "direct_node_ids": list(item.direct_node_ids),
        "sequence_number": item.sequence_number,
    }


def _selector_to_dict(item: SourceSelector) -> dict[str, object]:
    """Serialize only source-location facts and never a selected quote."""
    return {
        "selector_id": item.selector_id,
        "selector_type": item.selector_type,
        "resource_id": item.resource_id,
        "presentation_unit_id": item.presentation_unit_id,
        "source_path": item.source_path,
        "char_start": item.char_start,
        "char_end": item.char_end,
        "bbox": list(item.bbox) if item.bbox is not None else None,
        "source_anchor_ids": list(item.source_anchor_ids),
    }


def _content_node_to_dict(item: ContentNode) -> dict[str, object]:
    """Serialize the artifact's sole source-text-bearing record shape."""
    return {
        "node_id": item.node_id,
        "resource_id": item.resource_id,
        "owner_division_id": item.owner_division_id,
        "node_kind": item.node_kind,
        "content": {
            "text": item.content.text,
            "sha256": item.content.sha256,
        },
        "source_selectors": [
            _selector_to_dict(selector) for selector in item.source_selectors
        ],
        "sequence_number": item.sequence_number,
    }


def _representation_to_dict(item: Representation) -> dict[str, object]:
    """Serialize representation references without caption or object text copies."""
    return {
        "representation_id": item.representation_id,
        "subject_id": item.subject_id,
        "representation_type": item.representation_type,
        "media_type": item.media_type,
        "artifact_id": item.artifact_id,
        "selector_ids": list(item.selector_ids),
        "caption_node_id": item.caption_node_id,
    }


def _native_binding_to_dict(item: NativeBinding) -> dict[str, object]:
    """Serialize a canonical/native pointer link without native payload bytes."""
    return {
        "binding_id": item.binding_id,
        "canonical_id": item.canonical_id,
        "artifact_id": item.artifact_id,
        "native_pointer": item.native_pointer,
        "binding_role": item.binding_role,
    }


def _relation_to_dict(item: CanonicalRelation) -> dict[str, object]:
    """Serialize canonical references while omitting legacy free target text."""
    return {
        "relation_id": item.relation_id,
        "source_id": item.source_id,
        "target_id": item.target_id,
        "relation_type": item.relation_type,
        "status": item.status,
        "epistemic_state": item.epistemic_state,
        "evidence_node_ids": list(item.evidence_node_ids),
    }


def _activity_to_dict(item: ProcessingActivity) -> dict[str, object]:
    """Serialize compact processing lineage through IDs rather than payloads."""
    return {
        "activity_id": item.activity_id,
        "activity_type": item.activity_type,
        "run_id": item.run_id,
        "correlation_id": item.correlation_id,
        "method": item.method,
        "parser_ids": list(item.parser_ids),
        "input_artifact_ids": list(item.input_artifact_ids),
        "output_artifact_ids": list(item.output_artifact_ids),
    }


def _artifact_descriptor_to_dict(
    item: CanonicalArtifactDescriptor,
) -> dict[str, object]:
    """Serialize one generic external artifact reference and integrity metadata."""
    return {
        "artifact_id": item.artifact_id,
        "role": item.role,
        "uri": item.uri,
        "media_type": item.media_type,
        "sha256": item.sha256,
        "schema_version": item.schema_version,
    }


def _parse_resource(value: Mapping[str, object]) -> CanonicalResource:
    """Parse one untrusted resource using the complete persisted field set."""
    _require_exact_fields(
        value,
        {
            "resource_id",
            "source_asset_id",
            "source_sha256",
            "media_type",
            "original_filename",
            "logical_uri",
        },
        "resource",
    )
    return CanonicalResource(
        resource_id=_required_text(value["resource_id"], "resource_id"),
        source_asset_id=_required_text(
            value["source_asset_id"], "source_asset_id"
        ),
        source_sha256=_sha256_text(value["source_sha256"], "source_sha256"),
        media_type=_required_text(value["media_type"], "media_type"),
        original_filename=_required_text(
            value["original_filename"], "original_filename"
        ),
        logical_uri=_logical_uri(value["logical_uri"], "logical_uri"),
    )


def _parse_presentation_unit(value: Mapping[str, object]) -> PresentationUnit:
    """Parse one untrusted presentation surface without accepting text fields."""
    _require_exact_fields(
        value,
        {
            "presentation_unit_id",
            "resource_id",
            "unit_type",
            "sequence_number",
            "physical_index",
            "labels",
            "width",
            "height",
        },
        "presentation unit",
    )
    return PresentationUnit(
        presentation_unit_id=_required_text(
            value["presentation_unit_id"], "presentation_unit_id"
        ),
        resource_id=_required_text(value["resource_id"], "resource_id"),
        unit_type=_required_text(value["unit_type"], "unit_type"),
        sequence_number=_nonnegative_int(
            value["sequence_number"], "sequence_number"
        ),
        physical_index=_optional_nonnegative_int(
            value["physical_index"], "physical_index"
        ),
        labels=_text_tuple(value["labels"], "labels"),
        width=_optional_nonnegative_float(value["width"], "width"),
        height=_optional_nonnegative_float(value["height"], "height"),
    )


def _parse_division(value: Mapping[str, object]) -> Division:
    """Parse one untrusted logical division with reference-only hierarchy fields."""
    _require_exact_fields(
        value,
        {
            "division_id",
            "resource_id",
            "division_role",
            "parent_division_id",
            "child_division_ids",
            "title_node_id",
            "number",
            "label",
            "direct_node_ids",
            "sequence_number",
        },
        "division",
    )
    return Division(
        division_id=_required_text(value["division_id"], "division_id"),
        resource_id=_required_text(value["resource_id"], "resource_id"),
        division_role=_required_text(value["division_role"], "division_role"),
        parent_division_id=_optional_text(
            value["parent_division_id"], "parent_division_id"
        ),
        child_division_ids=_text_tuple(
            value["child_division_ids"], "child_division_ids"
        ),
        title_node_id=_optional_text(value["title_node_id"], "title_node_id"),
        number=_optional_text(value["number"], "number"),
        label=_optional_text(value["label"], "label"),
        direct_node_ids=_text_tuple(value["direct_node_ids"], "direct_node_ids"),
        sequence_number=_nonnegative_int(
            value["sequence_number"], "sequence_number"
        ),
    )


def _parse_canonical_text(value: Mapping[str, object]) -> CanonicalText:
    """Parse exact text and digest while deferring hash comparison to validation."""
    _require_exact_fields(value, {"text", "sha256"}, "canonical text")
    return CanonicalText(
        text=_text(value["text"], "content.text"),
        sha256=_sha256_text(value["sha256"], "content.sha256"),
    )


def _parse_selector(value: Mapping[str, object]) -> SourceSelector:
    """Parse one selector and reject any unrecognized quote or text field."""
    _require_exact_fields(
        value,
        {
            "selector_id",
            "selector_type",
            "resource_id",
            "presentation_unit_id",
            "source_path",
            "char_start",
            "char_end",
            "bbox",
            "source_anchor_ids",
        },
        "source selector",
    )
    raw_bbox = value["bbox"]
    bbox: tuple[float, float, float, float] | None
    if raw_bbox is None:
        bbox = None
    else:
        values = _sequence(raw_bbox, "bbox")
        if len(values) != 4:
            raise CanonicalContentValidationError("bbox must contain four numbers")
        bbox = tuple(_number(item, "bbox") for item in values)  # type: ignore[assignment]
    return SourceSelector(
        selector_id=_required_text(value["selector_id"], "selector_id"),
        selector_type=_required_text(value["selector_type"], "selector_type"),
        resource_id=_required_text(value["resource_id"], "resource_id"),
        presentation_unit_id=_optional_text(
            value["presentation_unit_id"], "presentation_unit_id"
        ),
        source_path=_optional_logical_path(value["source_path"], "source_path"),
        char_start=_optional_nonnegative_int(value["char_start"], "char_start"),
        char_end=_optional_nonnegative_int(value["char_end"], "char_end"),
        bbox=bbox,
        source_anchor_ids=_text_tuple(
            value["source_anchor_ids"], "source_anchor_ids"
        ),
    )


def _parse_content_node(value: Mapping[str, object]) -> ContentNode:
    """Parse the sole source-text-bearing record and its location selectors."""
    _require_exact_fields(
        value,
        {
            "node_id",
            "resource_id",
            "owner_division_id",
            "node_kind",
            "content",
            "source_selectors",
            "sequence_number",
        },
        "content node",
    )
    content = _mapping(value["content"], "content")
    return ContentNode(
        node_id=_required_text(value["node_id"], "node_id"),
        resource_id=_required_text(value["resource_id"], "resource_id"),
        owner_division_id=_required_text(
            value["owner_division_id"], "owner_division_id"
        ),
        node_kind=_required_text(value["node_kind"], "node_kind"),
        content=_parse_canonical_text(content),
        source_selectors=tuple(
            _parse_selector(item)
            for item in _mapping_items(
                value["source_selectors"], "source_selectors"
            )
        ),
        sequence_number=_nonnegative_int(
            value["sequence_number"], "sequence_number"
        ),
    )


def _parse_representation(value: Mapping[str, object]) -> Representation:
    """Parse reference-only representation metadata and reject copied content."""
    _require_exact_fields(
        value,
        {
            "representation_id",
            "subject_id",
            "representation_type",
            "media_type",
            "artifact_id",
            "selector_ids",
            "caption_node_id",
        },
        "representation",
    )
    return Representation(
        representation_id=_required_text(
            value["representation_id"], "representation_id"
        ),
        subject_id=_required_text(value["subject_id"], "subject_id"),
        representation_type=_required_text(
            value["representation_type"], "representation_type"
        ),
        media_type=_required_text(value["media_type"], "media_type"),
        artifact_id=_optional_text(value["artifact_id"], "artifact_id"),
        selector_ids=_text_tuple(value["selector_ids"], "selector_ids"),
        caption_node_id=_optional_text(
            value["caption_node_id"], "caption_node_id"
        ),
    )


def _parse_native_binding(value: Mapping[str, object]) -> NativeBinding:
    """Parse one explicit binding without interpreting its native pointer."""
    _require_exact_fields(
        value,
        {
            "binding_id",
            "canonical_id",
            "artifact_id",
            "native_pointer",
            "binding_role",
        },
        "native binding",
    )
    return NativeBinding(
        binding_id=_required_text(value["binding_id"], "binding_id"),
        canonical_id=_required_text(value["canonical_id"], "canonical_id"),
        artifact_id=_required_text(value["artifact_id"], "artifact_id"),
        native_pointer=_required_text(value["native_pointer"], "native_pointer"),
        binding_role=_required_text(value["binding_role"], "binding_role"),
    )


def _parse_relation(value: Mapping[str, object]) -> CanonicalRelation:
    """Parse one relation and reject the legacy free-text target field by shape."""
    _require_exact_fields(
        value,
        {
            "relation_id",
            "source_id",
            "target_id",
            "relation_type",
            "status",
            "epistemic_state",
            "evidence_node_ids",
        },
        "canonical relation",
    )
    return CanonicalRelation(
        relation_id=_required_text(value["relation_id"], "relation_id"),
        source_id=_required_text(value["source_id"], "source_id"),
        target_id=_optional_text(value["target_id"], "target_id"),
        relation_type=_required_text(value["relation_type"], "relation_type"),
        status=_required_text(value["status"], "status"),
        epistemic_state=_required_text(
            value["epistemic_state"], "epistemic_state"
        ),
        evidence_node_ids=_text_tuple(
            value["evidence_node_ids"], "evidence_node_ids"
        ),
    )


def _parse_activity(value: Mapping[str, object]) -> ProcessingActivity:
    """Parse compact processing lineage and its artifact-reference lists."""
    _require_exact_fields(
        value,
        {
            "activity_id",
            "activity_type",
            "run_id",
            "correlation_id",
            "method",
            "parser_ids",
            "input_artifact_ids",
            "output_artifact_ids",
        },
        "processing activity",
    )
    return ProcessingActivity(
        activity_id=_required_text(value["activity_id"], "activity_id"),
        activity_type=_required_text(value["activity_type"], "activity_type"),
        run_id=_required_text(value["run_id"], "run_id"),
        correlation_id=_required_text(
            value["correlation_id"], "correlation_id"
        ),
        method=_required_text(value["method"], "method"),
        parser_ids=_text_tuple(value["parser_ids"], "parser_ids"),
        input_artifact_ids=_text_tuple(
            value["input_artifact_ids"], "input_artifact_ids"
        ),
        output_artifact_ids=_text_tuple(
            value["output_artifact_ids"], "output_artifact_ids"
        ),
    )


def _parse_artifact_descriptor(
    value: Mapping[str, object],
) -> CanonicalArtifactDescriptor:
    """Parse one generic artifact reference without treating it as a T01 descriptor."""
    _require_exact_fields(
        value,
        {"artifact_id", "role", "uri", "media_type", "sha256", "schema_version"},
        "artifact descriptor",
    )
    sha_value = value["sha256"]
    return CanonicalArtifactDescriptor(
        artifact_id=_required_text(value["artifact_id"], "artifact_id"),
        role=_required_text(value["role"], "role"),
        uri=_logical_uri(value["uri"], "uri"),
        media_type=_required_text(value["media_type"], "media_type"),
        sha256=(None if sha_value is None else _sha256_text(sha_value, "sha256")),
        schema_version=_optional_text(value["schema_version"], "schema_version"),
    )


@dataclass(frozen=True, slots=True)
class _CanonicalIndexes:
    """Hold ephemeral ID maps so validation never persists duplicate indexes."""

    resources: Mapping[str, CanonicalResource]
    presentation_units: Mapping[str, PresentationUnit]
    divisions: Mapping[str, Division]
    content_nodes: Mapping[str, ContentNode]
    selectors: Mapping[str, SourceSelector]
    representations: Mapping[str, Representation]
    relations: Mapping[str, CanonicalRelation]
    activities: Mapping[str, ProcessingActivity]
    artifacts: Mapping[str, CanonicalArtifactDescriptor]
    canonical_ids: frozenset[str]


def _validate_record_shapes(artifact: CanonicalContentArtifact) -> None:
    """Apply the strict JSON field rules to directly constructed typed records.

    Public validation accepts both deserialized artifacts and callers that create
    frozen records in Python. Serializing each record to its declared shape and
    parsing that shape through the existing trust helpers gives both paths one
    field-type, tuple-uniqueness, geometry-length, URI, and metadata rule set.
    This read-only pass does not rebuild the aggregate or inspect external data;
    cross-record ownership and references remain the later validation stages.
    """
    for item in artifact.resources:
        _parse_resource(_resource_to_dict(item))
    for item in artifact.presentation_units:
        _parse_presentation_unit(_presentation_unit_to_dict(item))
    for item in artifact.divisions:
        _parse_division(_division_to_dict(item))
    for item in artifact.content_nodes:
        _parse_content_node(_content_node_to_dict(item))
    for item in artifact.representations:
        _parse_representation(_representation_to_dict(item))
    for item in artifact.native_bindings:
        _parse_native_binding(_native_binding_to_dict(item))
    for item in artifact.relations:
        _parse_relation(_relation_to_dict(item))
    for item in artifact.processing_activities:
        _parse_activity(_activity_to_dict(item))
    for item in artifact.artifact_descriptors:
        _parse_artifact_descriptor(_artifact_descriptor_to_dict(item))


def _build_indexes(artifact: CanonicalContentArtifact) -> _CanonicalIndexes:
    """Build unique local indexes and reject duplicate IDs across record classes.

    Validation and lookup call this helper. A single global namespace for
    canonical records prevents an ID from resolving differently by consumer;
    artifact IDs remain a separate external-reference namespace. The indexes are
    local immutable-use mappings and never enter serialized output.
    """
    resources = _unique_index(artifact.resources, "resource_id", "resource")
    presentation_units = _unique_index(
        artifact.presentation_units,
        "presentation_unit_id",
        "presentation unit",
    )
    divisions = _unique_index(artifact.divisions, "division_id", "division")
    content_nodes = _unique_index(artifact.content_nodes, "node_id", "content node")
    representations = _unique_index(
        artifact.representations,
        "representation_id",
        "representation",
    )
    relations = _unique_index(artifact.relations, "relation_id", "relation")
    activities = _unique_index(
        artifact.processing_activities,
        "activity_id",
        "processing activity",
    )
    artifacts = _unique_index(
        artifact.artifact_descriptors,
        "artifact_id",
        "artifact descriptor",
    )
    _unique_index(artifact.native_bindings, "binding_id", "native binding")
    selectors: dict[str, SourceSelector] = {}
    for node in artifact.content_nodes:
        for selector in node.source_selectors:
            if selector.selector_id in selectors:
                raise CanonicalContentValidationError(
                    f"Duplicate source selector ID: {selector.selector_id}"
                )
            selectors[selector.selector_id] = selector

    canonical_groups = (
        set(resources),
        set(presentation_units),
        set(divisions),
        set(content_nodes),
        set(representations),
        set(relations),
        set(activities),
    )
    canonical_ids: set[str] = set()
    for group in canonical_groups:
        duplicate = canonical_ids.intersection(group)
        if duplicate:
            raise CanonicalContentValidationError(
                f"Duplicate canonical ID across record classes: {sorted(duplicate)[0]}"
            )
        canonical_ids.update(group)
    return _CanonicalIndexes(
        resources=resources,  # type: ignore[arg-type]
        presentation_units=presentation_units,  # type: ignore[arg-type]
        divisions=divisions,  # type: ignore[arg-type]
        content_nodes=content_nodes,  # type: ignore[arg-type]
        selectors=selectors,
        representations=representations,  # type: ignore[arg-type]
        relations=relations,  # type: ignore[arg-type]
        activities=activities,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        canonical_ids=frozenset(canonical_ids),
    )


def _unique_index(
    records: Sequence[object], attribute: str, record_name: str
) -> dict[str, object]:
    """Index records by one string attribute and fail before later overwrite."""
    result: dict[str, object] = {}
    for record in records:
        identifier = getattr(record, attribute, None)
        if not isinstance(identifier, str) or not identifier:
            raise CanonicalContentValidationError(
                f"{record_name} has an invalid {attribute}"
            )
        if identifier in result:
            raise CanonicalContentValidationError(
                f"Duplicate {record_name} ID: {identifier}"
            )
        result[identifier] = record
    return result


def _validate_ordering(artifact: CanonicalContentArtifact) -> None:
    """Require one canonical tie-broken order for deterministic serialization.

    Builders and readers use sequence number plus ID where source order exists,
    and stable ID elsewhere. Rejecting alternate order prevents equivalent
    records from producing different bytes and makes duplicate sequence numbers
    deterministic without collapsing distinct source occurrences.
    """
    expectations: tuple[tuple[str, Sequence[object], object], ...] = (
        ("resources", artifact.resources, lambda item: item.resource_id),
        (
            "presentation_units",
            artifact.presentation_units,
            lambda item: (item.sequence_number, item.presentation_unit_id),
        ),
        (
            "divisions",
            artifact.divisions,
            lambda item: (item.sequence_number, item.division_id),
        ),
        (
            "content_nodes",
            artifact.content_nodes,
            lambda item: (item.sequence_number, item.node_id),
        ),
        (
            "representations",
            artifact.representations,
            lambda item: item.representation_id,
        ),
        ("native_bindings", artifact.native_bindings, lambda item: item.binding_id),
        ("relations", artifact.relations, lambda item: item.relation_id),
        (
            "processing_activities",
            artifact.processing_activities,
            lambda item: item.activity_id,
        ),
        (
            "artifact_descriptors",
            artifact.artifact_descriptors,
            lambda item: item.artifact_id,
        ),
    )
    for name, records, key in expectations:
        if tuple(records) != tuple(sorted(records, key=key)):  # type: ignore[arg-type]
            raise CanonicalContentValidationError(
                f"{name} are not in deterministic canonical order"
            )


def _validate_resources_and_presentation(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Validate source identity and presentation facts without reading source bytes."""
    if not artifact.resources:
        raise CanonicalContentValidationError(
            "Canonical content must contain at least one resource"
        )
    for resource in artifact.resources:
        _required_text(resource.resource_id, "resource_id")
        _required_text(resource.source_asset_id, "source_asset_id")
        _sha256_text(resource.source_sha256, "source_sha256")
        _required_text(resource.media_type, "media_type")
        _required_text(resource.original_filename, "original_filename")
        _logical_uri(resource.logical_uri, "logical_uri")
    for unit in artifact.presentation_units:
        if unit.resource_id not in indexes.resources:
            raise CanonicalReferenceError(
                f"Presentation unit references missing resource: {unit.presentation_unit_id}"
            )
        _required_text(unit.unit_type, "unit_type")
        _nonnegative_int(unit.sequence_number, "sequence_number")
        _optional_nonnegative_int(unit.physical_index, "physical_index")
        _optional_nonnegative_float(unit.width, "width")
        _optional_nonnegative_float(unit.height, "height")


def _validate_divisions_and_ownership(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Prove reciprocal acyclic hierarchy and exactly-one deepest node ownership."""
    for division in artifact.divisions:
        if division.resource_id not in indexes.resources:
            raise CanonicalReferenceError(
                f"Division references missing resource: {division.division_id}"
            )
        if division.parent_division_id is not None:
            parent = indexes.divisions.get(division.parent_division_id)
            if parent is None:
                raise CanonicalReferenceError(
                    f"Division parent is missing: {division.division_id}"
                )
            if division.division_id not in parent.child_division_ids:
                raise CanonicalOwnershipError(
                    f"Division parent/child links disagree: {division.division_id}"
                )
            if parent.resource_id != division.resource_id:
                raise CanonicalOwnershipError(
                    f"Division parent belongs to another resource: {division.division_id}"
                )
        for child_id in division.child_division_ids:
            child = indexes.divisions.get(child_id)
            if child is None:
                raise CanonicalReferenceError(
                    f"Division child is missing: {child_id}"
                )
            if child.parent_division_id != division.division_id:
                raise CanonicalOwnershipError(
                    f"Division child/parent links disagree: {child_id}"
                )
        expected_children = tuple(
            item.division_id
            for item in sorted(
                (indexes.divisions[child_id] for child_id in division.child_division_ids),
                key=lambda item: (item.sequence_number, item.division_id),
            )
        )
        if division.child_division_ids != expected_children:
            raise CanonicalContentValidationError(
                f"Division children are not deterministically ordered: {division.division_id}"
            )
    _detect_division_cycles(indexes.divisions)

    direct_owner_count = {node.node_id: 0 for node in artifact.content_nodes}
    for division in artifact.divisions:
        direct_nodes: list[ContentNode] = []
        for node_id in division.direct_node_ids:
            node = indexes.content_nodes.get(node_id)
            if node is None:
                raise CanonicalReferenceError(
                    f"Division direct node is missing: {node_id}"
                )
            if node.owner_division_id != division.division_id:
                raise CanonicalOwnershipError(
                    f"Content node direct owner disagrees: {node_id}"
                )
            if node.resource_id != division.resource_id:
                raise CanonicalOwnershipError(
                    f"Content node owner belongs to another resource: {node_id}"
                )
            direct_owner_count[node_id] += 1
            direct_nodes.append(node)
        expected_direct = tuple(
            item.node_id
            for item in sorted(
                direct_nodes,
                key=lambda item: (item.sequence_number, item.node_id),
            )
        )
        if division.direct_node_ids != expected_direct:
            raise CanonicalContentValidationError(
                f"Division direct nodes are not deterministically ordered: {division.division_id}"
            )
        if division.title_node_id is not None:
            title = indexes.content_nodes.get(division.title_node_id)
            if title is None:
                raise CanonicalReferenceError(
                    f"Division title node is missing: {division.division_id}"
                )
            if title.resource_id != division.resource_id:
                raise CanonicalOwnershipError(
                    f"Division title belongs to another resource: {division.division_id}"
                )
            if title.owner_division_id != division.division_id:
                raise CanonicalOwnershipError(
                    f"Division title is not directly owned: {division.division_id}"
                )
    for node_id, count in direct_owner_count.items():
        if count != 1:
            raise CanonicalOwnershipError(
                f"Content node must have exactly one direct owner: {node_id}"
            )


def _detect_division_cycles(divisions: Mapping[str, Division]) -> None:
    """Detect hierarchy cycles with a three-state depth-first traversal.

    Validation calls this before any subtree lookup. ``visiting`` identifies the
    active recursion path and therefore a back edge; ``visited`` prevents repeated
    work. The algorithm reports logical IDs only and never recurses into content.
    """
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(division_id: str) -> None:
        """Walk one hierarchy branch and reject an edge to the active path."""
        if division_id in visiting:
            raise CanonicalOwnershipError(
                f"Division hierarchy contains a cycle at: {division_id}"
            )
        if division_id in visited:
            return
        visiting.add(division_id)
        for child_id in divisions[division_id].child_division_ids:
            visit(child_id)
        visiting.remove(division_id)
        visited.add(division_id)

    for identifier in divisions:
        visit(identifier)


def _validate_content_nodes(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Verify node ownership, exact UTF-8 hashes, and real selector combinations."""
    for node in artifact.content_nodes:
        if node.resource_id not in indexes.resources:
            raise CanonicalReferenceError(
                f"Content node references missing resource: {node.node_id}"
            )
        owner = indexes.divisions.get(node.owner_division_id)
        if owner is None:
            raise CanonicalOwnershipError(
                f"Content node has no owner division: {node.node_id}"
            )
        if owner.resource_id != node.resource_id:
            raise CanonicalOwnershipError(
                f"Content node and owner use different resources: {node.node_id}"
            )
        expected_hash = hashlib.sha256(node.content.text.encode("utf-8")).hexdigest()
        if node.content.sha256 != expected_hash:
            raise CanonicalContentValidationError(
                f"Content SHA-256 does not match exact UTF-8 text: {node.node_id}"
            )
        if not node.source_selectors:
            raise CanonicalContentValidationError(
                f"Content node has no source selector: {node.node_id}"
            )
        expected_selectors = tuple(
            sorted(node.source_selectors, key=lambda item: item.selector_id)
        )
        if node.source_selectors != expected_selectors:
            raise CanonicalContentValidationError(
                f"Source selectors are not deterministically ordered: {node.node_id}"
            )
        for selector in node.source_selectors:
            _validate_selector(selector, node, indexes)


def _validate_selector(
    selector: SourceSelector,
    node: ContentNode,
    indexes: _CanonicalIndexes,
) -> None:
    """Validate one selector using only observed location combinations.

    Node validation calls this helper. Character endpoints must appear together,
    geometry must be ordered, references must stay in the same resource, and at
    least one page, path, or anchor locator must exist. No missing offset is
    inferred from text length or neighboring records.
    """
    if selector.resource_id != node.resource_id:
        raise CanonicalReferenceError(
            f"Source selector uses another resource: {selector.selector_id}"
        )
    if selector.presentation_unit_id is not None:
        unit = indexes.presentation_units.get(selector.presentation_unit_id)
        if unit is None:
            raise CanonicalReferenceError(
                f"Source selector presentation unit is missing: {selector.selector_id}"
            )
        if unit.resource_id != selector.resource_id:
            raise CanonicalReferenceError(
                f"Source selector presentation unit uses another resource: {selector.selector_id}"
            )
    has_start = selector.char_start is not None
    has_end = selector.char_end is not None
    if has_start != has_end:
        raise CanonicalContentValidationError(
            f"Source selector character range is incomplete: {selector.selector_id}"
        )
    if selector.char_start is not None and selector.char_end is not None:
        if selector.char_start < 0 or selector.char_end < selector.char_start:
            raise CanonicalContentValidationError(
                f"Source selector character range is invalid: {selector.selector_id}"
            )
    if selector.bbox is not None:
        if len(selector.bbox) != 4:
            raise CanonicalContentValidationError(
                f"Source selector bbox must contain four numbers: {selector.selector_id}"
            )
        left, top, right, bottom = (
            _number(value, "bbox") for value in selector.bbox
        )
        if right < left or bottom < top:
            raise CanonicalContentValidationError(
                f"Source selector bbox is invalid: {selector.selector_id}"
            )
    if selector.source_path is not None:
        _optional_logical_path(selector.source_path, "source_path")
    if (
        selector.presentation_unit_id is None
        and selector.source_path is None
        and not selector.source_anchor_ids
    ):
        raise CanonicalContentValidationError(
            f"Source selector has no source locator: {selector.selector_id}"
        )


def _validate_representations(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Resolve representation subjects, selectors, captions, and external artifacts."""
    allowed_subjects = (
        set(indexes.resources)
        | set(indexes.presentation_units)
        | set(indexes.divisions)
        | set(indexes.content_nodes)
    )
    for representation in artifact.representations:
        if representation.subject_id not in allowed_subjects:
            raise CanonicalReferenceError(
                f"Representation subject is missing: {representation.representation_id}"
            )
        for selector_id in representation.selector_ids:
            if selector_id not in indexes.selectors:
                raise CanonicalReferenceError(
                    f"Representation selector is missing: {selector_id}"
                )
        if (
            representation.caption_node_id is not None
            and representation.caption_node_id not in indexes.content_nodes
        ):
            raise CanonicalReferenceError(
                f"Representation caption node is missing: {representation.representation_id}"
            )
        if (
            representation.artifact_id is not None
            and representation.artifact_id not in indexes.artifacts
        ):
            raise CanonicalReferenceError(
                f"Representation artifact is missing: {representation.representation_id}"
            )


def _validate_relations(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Validate canonical relation endpoints without restoring legacy target text."""
    unresolved_states = {"unresolved", "ambiguous", "contradicted", "rejected"}
    for relation in artifact.relations:
        if relation.source_id not in indexes.canonical_ids:
            raise CanonicalReferenceError(
                f"Canonical relation source is missing: {relation.relation_id}"
            )
        if relation.target_id is None:
            if relation.status not in unresolved_states:
                raise CanonicalReferenceError(
                    f"Resolved relation has no target: {relation.relation_id}"
                )
        elif relation.target_id not in indexes.canonical_ids:
            raise CanonicalReferenceError(
                f"Canonical relation target is missing: {relation.relation_id}"
            )
        for node_id in relation.evidence_node_ids:
            if node_id not in indexes.content_nodes:
                raise CanonicalReferenceError(
                    f"Canonical relation evidence node is missing: {node_id}"
                )


def _validate_activities(
    artifact: CanonicalContentArtifact, indexes: _CanonicalIndexes
) -> None:
    """Require every processing input and output to resolve to an artifact reference."""
    for activity in artifact.processing_activities:
        for artifact_id in (
            *activity.input_artifact_ids,
            *activity.output_artifact_ids,
        ):
            if artifact_id not in indexes.artifacts:
                raise CanonicalReferenceError(
                    f"Processing activity artifact is missing: {artifact_id}"
                )


def _validate_native_bindings(
    artifact: CanonicalContentArtifact,
    indexes: _CanonicalIndexes,
    *,
    native_descriptors: Mapping[str, NativeArtifactDescriptor] | None,
) -> None:
    """Validate explicit bindings against canonical IDs and real T01 descriptors.

    Aggregate validation calls this only for supplied bindings. The descriptor
    mapping is an explicit trust input from T01; generic artifact references are
    insufficient because they do not carry retained native pointers. A pointer
    must already be retained by T01. This helper never loads or copies payloads
    and never duplicates T01 JSON-pointer resolution.
    """
    if not artifact.native_bindings:
        return
    descriptors = native_descriptors or {}
    for binding in artifact.native_bindings:
        if binding.canonical_id not in indexes.canonical_ids:
            raise NativeBindingValidationError(
                f"Native binding canonical ID is missing: {binding.binding_id}"
            )
        descriptor = descriptors.get(binding.artifact_id)
        if descriptor is None:
            raise NativeBindingValidationError(
                f"Native binding T01 descriptor is missing: {binding.binding_id}"
            )
        generic = indexes.artifacts.get(binding.artifact_id)
        if generic is None or generic.role != "parser_native":
            raise NativeBindingValidationError(
                f"Native binding artifact reference is missing: {binding.binding_id}"
            )
        if binding.native_pointer not in descriptor.native_pointers:
            raise NativeBindingValidationError(
                f"Native binding pointer is not retained by T01: {binding.binding_id}"
            )


def _build_resource(
    document: CanonicalDocument, source_asset: SourceAsset
) -> CanonicalResource:
    """Project one SourceAsset into a byte-free logical canonical resource."""
    if document.source.source_id != source_asset.asset_id:
        raise CanonicalReferenceError(
            "Canonical document and SourceAsset identities do not match"
        )
    return CanonicalResource(
        resource_id=source_asset.asset_id,
        source_asset_id=source_asset.asset_id,
        source_sha256=source_asset.sha256,
        media_type=source_asset.media_type,
        original_filename=source_asset.original_filename,
        logical_uri=document.source.storage_key,
    )


def _build_presentation_units(
    document: CanonicalDocument, resource_id: str
) -> tuple[PresentationUnit, ...]:
    """Map current pages to presentation units while retaining stable page IDs."""
    return tuple(
        sorted(
            (
                PresentationUnit(
                    presentation_unit_id=page.page_id,
                    resource_id=resource_id,
                    unit_type="page",
                    sequence_number=page.sequence_number,
                    physical_index=page.physical_page_index,
                    labels=tuple(
                        value
                        for value in (
                            page.pdf_page_label,
                            page.printed_page_label,
                        )
                        if value is not None
                    ),
                    width=page.width,
                    height=page.height,
                )
                for page in document.pages
            ),
            key=lambda item: (item.sequence_number, item.presentation_unit_id),
        )
    )


def _ordered_blocks(document: CanonicalDocument) -> tuple[Block, ...]:
    """Return each v2 block once in observed page order with deterministic fallback."""
    blocks = {item.block_id: item for item in document.blocks}
    ordered: list[Block] = []
    seen: set[str] = set()
    for page in sorted(
        document.pages,
        key=lambda item: (item.sequence_number, item.page_id),
    ):
        for block_id in page.block_ids:
            block = blocks.get(block_id)
            if block is not None and block_id not in seen:
                ordered.append(block)
                seen.add(block_id)
    ordered.extend(
        sorted(
            (item for item in document.blocks if item.block_id not in seen),
            key=lambda item: (item.reading_order, item.block_id),
        )
    )
    return tuple(ordered)


def _build_divisions(
    document: CanonicalDocument,
    resource_id: str,
) -> tuple[tuple[Division, ...], Mapping[str, str]]:
    """Build root/section hierarchy and resolve each block's deepest direct owner.

    The builder calls this before creating nodes. Current section ``block_ids``
    include child spans, so candidate membership is ranked by actual parent depth;
    a child wins over its ancestor. Two unrelated candidates at the same deepest
    level are rejected instead of assigning by page boundary or arbitrary order.
    Unowned blocks belong directly to the document-root division.
    """
    root_id = f"{document.document_id}:division:document"
    text_blocks = tuple(item for item in _ordered_blocks(document) if item.text)
    block_position = {item.block_id: index for index, item in enumerate(text_blocks)}
    sections = {item.section_id: item for item in document.sections}
    section_depths = {
        identifier: _section_depth(identifier, sections) for identifier in sections
    }
    section_sequence = {
        section.section_id: min(
            (
                block_position[block_id]
                for block_id in section.block_ids
                if block_id in block_position
            ),
            default=len(block_position) + index,
        )
        for index, section in enumerate(document.sections)
    }
    owner_by_node: dict[str, str] = {}
    direct_by_division: dict[str, list[str]] = {root_id: []}
    for section_id in sections:
        direct_by_division[section_id] = []
    for block in text_blocks:
        candidates = [
            section
            for section in document.sections
            if block.block_id in section.block_ids
        ]
        if not candidates:
            owner_id = root_id
        else:
            deepest = max(section_depths[item.section_id] for item in candidates)
            selected = [
                item
                for item in candidates
                if section_depths[item.section_id] == deepest
            ]
            if len(selected) != 1:
                raise CanonicalOwnershipError(
                    f"Block has ambiguous deepest Division owner: {block.block_id}"
                )
            owner_id = selected[0].section_id
        owner_by_node[block.block_id] = owner_id
        direct_by_division[owner_id].append(block.block_id)

    child_by_division: dict[str, list[str]] = {root_id: []}
    for section_id in sections:
        child_by_division[section_id] = []
    for section in document.sections:
        parent_id = (
            section.parent_section_id
            if section.parent_section_id in sections
            else root_id
        )
        child_by_division[parent_id].append(section.section_id)
    for child_ids in child_by_division.values():
        child_ids.sort(key=lambda item: (section_sequence.get(item, -1), item))

    block_by_id = {item.block_id: item for item in text_blocks}
    root_title = next(
        (
            item.block_id
            for item in text_blocks
            if item.text.strip().lstrip("#").strip() == document.title.strip()
            and owner_by_node[item.block_id] == root_id
        ),
        None,
    )
    result: list[Division] = [
        Division(
            division_id=root_id,
            resource_id=resource_id,
            division_role="document",
            parent_division_id=None,
            child_division_ids=tuple(child_by_division[root_id]),
            title_node_id=root_title,
            direct_node_ids=tuple(direct_by_division[root_id]),
            sequence_number=0,
            label=None,
            number=None,
        )
    ]
    ordered_sections = sorted(
        document.sections,
        key=lambda item: (section_sequence[item.section_id], item.section_id),
    )
    for sequence_number, section in enumerate(ordered_sections, start=1):
        parent_id = (
            section.parent_section_id
            if section.parent_section_id in sections
            else root_id
        )
        heading = block_by_id.get(section.heading_block_id or "")
        role = (
            "appendix"
            if heading is not None
            and heading.text.strip().lower().startswith("appendix ")
            else "section"
        )
        result.append(
            Division(
                division_id=section.section_id,
                resource_id=resource_id,
                division_role=role,
                parent_division_id=parent_id,
                child_division_ids=tuple(child_by_division[section.section_id]),
                title_node_id=(
                    section.heading_block_id
                    if owner_by_node.get(section.heading_block_id or "")
                    == section.section_id
                    else None
                ),
                number=section.number,
                label=None,
                direct_node_ids=tuple(direct_by_division[section.section_id]),
                sequence_number=sequence_number,
            )
        )
    return (
        tuple(sorted(result, key=lambda item: (item.sequence_number, item.division_id))),
        owner_by_node,
    )


def _section_depth(identifier: str, sections: Mapping[str, object]) -> int:
    """Measure explicit section ancestry and reject cycles before owner selection."""
    depth = 0
    current = identifier
    visited: set[str] = set()
    while current in sections:
        if current in visited:
            raise CanonicalOwnershipError(
                f"Current v2 section hierarchy contains a cycle: {identifier}"
            )
        visited.add(current)
        depth += 1
        parent = getattr(sections[current], "parent_section_id", None)
        if not isinstance(parent, str):
            break
        current = parent
    return depth


def _build_content_nodes(
    document: CanonicalDocument,
    resource_id: str,
    owner_by_node: Mapping[str, str],
) -> tuple[ContentNode, ...]:
    """Create one node per text-bearing block with exact hash and observed selectors.

    Builder calls this after deepest ownership is known. Page IDs, block IDs, and
    geometry are current v2 facts. Character offsets are deliberately absent
    because current blocks do not expose real source-byte or source-text offsets.
    Equal strings remain separate nodes with separate IDs and selectors.
    """
    nodes: list[ContentNode] = []
    presentation_unit_ids = {item.page_id for item in document.pages}
    for sequence_number, block in enumerate(_ordered_blocks(document)):
        if not block.text:
            continue
        selector = SourceSelector(
            selector_id=f"{block.block_id}:selector",
            selector_type=("source-region" if block.bbox is not None else "source-anchor"),
            resource_id=resource_id,
            presentation_unit_id=(
                block.page_id if block.page_id in presentation_unit_ids else None
            ),
            source_path=None,
            char_start=None,
            char_end=None,
            bbox=block.bbox,
            source_anchor_ids=(block.block_id,),
        )
        nodes.append(
            ContentNode(
                node_id=block.block_id,
                resource_id=resource_id,
                owner_division_id=owner_by_node[block.block_id],
                node_kind=block.block_type,
                content=CanonicalText(
                    text=block.text,
                    sha256=hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                ),
                source_selectors=(selector,),
                sequence_number=sequence_number,
            )
        )
    return tuple(sorted(nodes, key=lambda item: (item.sequence_number, item.node_id)))


def _build_representations(
    document: CanonicalDocument,
    resource_id: str,
    divisions: Sequence[Division],
    nodes: Sequence[ContentNode],
) -> tuple[Representation, ...]:
    """Map v2 non-text objects to references without copying captions or object text."""
    division_ids = {item.division_id for item in divisions}
    nodes_by_id = {item.node_id: item for item in nodes}
    selector_by_anchor = {
        anchor_id: selector.selector_id
        for node in nodes
        for selector in node.source_selectors
        for anchor_id in selector.source_anchor_ids
    }
    result: list[Representation] = []
    for item in document.objects:
        subject_id = (
            item.owner_section_id
            if item.owner_section_id in division_ids
            else item.page_id
            if any(page.page_id == item.page_id for page in document.pages)
            else resource_id
        )
        candidate_anchors = (
            *item.source_anchor_ids,
            *((item.caption_anchor_id,) if item.caption_anchor_id else ()),
            *((item.image_anchor_id,) if item.image_anchor_id else ()),
            *((item.note_anchor_id,) if item.note_anchor_id else ()),
        )
        selector_ids = tuple(
            sorted(
                {
                    selector_by_anchor[anchor]
                    for anchor in candidate_anchors
                    if anchor in selector_by_anchor
                }
            )
        )
        caption_node_id = (
            item.caption_anchor_id
            if item.caption_anchor_id in nodes_by_id
            else None
        )
        result.append(
            Representation(
                representation_id=item.object_id,
                subject_id=subject_id,
                representation_type=item.object_type,
                media_type="application/vnd.cognityx.representation+json",
                artifact_id=None,
                selector_ids=selector_ids,
                caption_node_id=caption_node_id,
            )
        )
    return tuple(sorted(result, key=lambda item: item.representation_id))


def _build_relations(
    document: CanonicalDocument,
    resource: CanonicalResource,
    presentation_units: Sequence[PresentationUnit],
    divisions: Sequence[Division],
    content_nodes: Sequence[ContentNode],
    representations: Sequence[Representation],
) -> tuple[CanonicalRelation, ...]:
    """Map only safely resolvable v2 relations and omit every free target string.

    Builder calls this after canonical IDs are known. Concrete targets that do not
    resolve are skipped rather than invented. Explicit unresolved records retain
    status and source evidence when their source resolves, but ``target_text`` is
    never copied into the v3.2 relation.
    """
    canonical_ids = {
        resource.resource_id,
        *(item.presentation_unit_id for item in presentation_units),
        *(item.division_id for item in divisions),
        *(item.node_id for item in content_nodes),
        *(item.representation_id for item in representations),
    }
    node_ids = {item.node_id for item in content_nodes}
    unresolved_states = {"unresolved", "ambiguous", "contradicted", "rejected"}
    result: list[CanonicalRelation] = []
    for item in document.relations:
        if item.source_anchor_id not in canonical_ids:
            continue
        target_id = (
            item.target_anchor_id
            if item.target_anchor_id in canonical_ids
            else None
        )
        if target_id is None and item.status not in unresolved_states:
            continue
        evidence = tuple(
            identifier
            for identifier in (item.source_anchor_id, target_id)
            if identifier in node_ids
        )
        result.append(
            CanonicalRelation(
                relation_id=item.relation_id,
                source_id=item.source_anchor_id,
                target_id=target_id,
                relation_type=item.relation_type,
                status=item.status,
                epistemic_state=_relation_epistemic_state(item.status, item.method),
                evidence_node_ids=evidence,
            )
        )
    existing_ids = {item.relation_id for item in result}
    for item in document.unresolved:
        if item.source_anchor_id not in canonical_ids:
            continue
        relation_id = f"{item.task_id}:unresolved"
        if relation_id in existing_ids:
            continue
        result.append(
            CanonicalRelation(
                relation_id=relation_id,
                source_id=item.source_anchor_id,
                target_id=None,
                relation_type=item.relation_type,
                status=item.status,
                epistemic_state=item.status,
                evidence_node_ids=(
                    (item.source_anchor_id,)
                    if item.source_anchor_id in node_ids
                    else ()
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.relation_id))


def _relation_epistemic_state(status: str, method: str) -> str:
    """Map existing audit facts to an explicit state without adjudicating anew."""
    normalized_status = status.lower()
    normalized_method = method.lower()
    if normalized_status in {"ambiguous", "contradicted", "unresolved", "rejected"}:
        return normalized_status
    if "human" in normalized_method:
        return "human-validated"
    if "model" in normalized_method or "inference" in normalized_method:
        return "model-inferred"
    if "parser" in normalized_method or normalized_status == "observed":
        return "parser-inferred"
    return "deterministically-derived"


def _build_artifact_descriptors(
    document: CanonicalDocument,
    source_asset: SourceAsset,
    native_descriptors: Sequence[NativeArtifactDescriptor],
    additional: Sequence[CanonicalArtifactDescriptor],
) -> tuple[CanonicalArtifactDescriptor, ...]:
    """Create generic references for source, compatibility, v3.2, and T01 artifacts.

    The builder uses known logical keys and IDs only; it does not open or hash
    generated artifacts and does not duplicate T01 descriptors. Caller-supplied
    descriptors may replace a generated compatibility descriptor by the same ID
    so persistence can provide its exact scoped URI. Duplicate caller IDs,
    duplicate T01 IDs, and attempts to replace T01 descriptors are rejected.
    """
    native_ids = tuple(item.artifact_id for item in native_descriptors)
    additional_ids = tuple(item.artifact_id for item in additional)
    if len(native_ids) != len(set(native_ids)):
        raise CanonicalContentValidationError(
            "Native artifact descriptors contain duplicate IDs"
        )
    if len(additional_ids) != len(set(additional_ids)):
        raise CanonicalContentValidationError(
            "Additional artifact descriptors contain duplicate IDs"
        )
    if set(native_ids).intersection(additional_ids):
        raise CanonicalContentValidationError(
            "Additional artifact descriptors cannot replace T01 descriptors"
        )
    prefix = f"ingest/documents/{document.document_id}"
    descriptors = [
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-source",
            role="source",
            uri=document.source.storage_key,
            media_type=source_asset.media_type,
            sha256=source_asset.sha256,
        ),
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-document",
            role="document",
            uri=f"storage://{prefix}/document.json",
            media_type="application/json",
            schema_version=document.schema_version,
        ),
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-evidence",
            role="evidence",
            uri=f"storage://{prefix}/evidence.jsonl",
            media_type="application/x-ndjson",
            schema_version="cognityx.ingest.evidence/v2",
        ),
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-canonical_content",
            role="canonical_content",
            uri=f"storage://{prefix}/canonical-content.json",
            media_type="application/json",
            schema_version=CANONICAL_CONTENT_SCHEMA_VERSION,
        ),
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-provenance",
            role="provenance",
            uri=f"storage://{prefix}/provenance.json",
            media_type="application/json",
            schema_version="cognityx.ingest.provenance/v2",
        ),
        CanonicalArtifactDescriptor(
            artifact_id=f"art-{document.document_id}-manifest",
            role="manifest",
            uri=f"storage://{prefix}/manifest.json",
            media_type="application/json",
            schema_version=document.schema_version,
        ),
    ]
    descriptors.extend(
        CanonicalArtifactDescriptor(
            artifact_id=item.artifact_id,
            role="parser_native",
            uri=item.uri,
            media_type=item.media_type,
            sha256=item.sha256,
            schema_version="cognityx.ingest.native-artifact-descriptor/v1",
        )
        for item in native_descriptors
    )
    by_id = {item.artifact_id: item for item in descriptors}
    by_id.update({item.artifact_id: item for item in additional})
    return tuple(sorted(by_id.values(), key=lambda item: item.artifact_id))


def _require_exact_fields(
    value: Mapping[str, object], expected: set[str], record_name: str
) -> None:
    """Reject missing and unknown fields so copied text cannot hide in extensions."""
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if unknown:
            detail.append(f"unknown={','.join(unknown)}")
        raise CanonicalContentValidationError(
            f"Invalid {record_name} fields ({'; '.join(detail)})"
        )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    """Require a string-keyed JSON object before nested field access."""
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise CanonicalContentValidationError(
            f"{field_name} must be a JSON object"
        )
    return value  # type: ignore[return-value]


def _mapping_items(
    value: object, field_name: str
) -> tuple[Mapping[str, object], ...]:
    """Require a JSON array of objects without accepting strings as sequences."""
    return tuple(
        _mapping(item, field_name) for item in _sequence(value, field_name)
    )


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    """Normalize an untrusted JSON array while rejecting text and mappings."""
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise CanonicalContentValidationError(f"{field_name} must be a JSON array")
    return tuple(value)


def _text(value: object, field_name: str) -> str:
    """Accept exact source text, including empty text, without normalization."""
    if not isinstance(value, str):
        raise CanonicalContentValidationError(f"{field_name} must be text")
    return value


def _required_text(value: object, field_name: str) -> str:
    """Require non-empty metadata text without controls or outer whitespace."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise CanonicalContentValidationError(
            f"{field_name} must be non-empty text without controls or outer whitespace"
        )
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    """Accept ``None`` or apply the same trust rule as required metadata text."""
    return None if value is None else _required_text(value, field_name)


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    """Freeze an ordered string array and reject duplicate ambiguous references."""
    result = tuple(
        _required_text(item, field_name) for item in _sequence(value, field_name)
    )
    if len(result) != len(set(result)):
        raise CanonicalContentValidationError(
            f"{field_name} must not contain duplicate values"
        )
    return result


def _nonnegative_int(value: object, field_name: str) -> int:
    """Require a real non-negative integer while excluding JSON booleans."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CanonicalContentValidationError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _optional_nonnegative_int(value: object, field_name: str) -> int | None:
    """Accept absent integer facts without inventing a default position."""
    return None if value is None else _nonnegative_int(value, field_name)


def _number(value: object, field_name: str) -> float:
    """Require a finite JSON number while excluding booleans and NaN values."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CanonicalContentValidationError(f"{field_name} must be numeric")
    converted = float(value)
    if converted != converted or converted in {float("inf"), float("-inf")}:
        raise CanonicalContentValidationError(f"{field_name} must be finite")
    return converted


def _optional_nonnegative_float(value: object, field_name: str) -> float | None:
    """Accept absent geometry and reject negative observed dimensions."""
    if value is None:
        return None
    converted = _number(value, field_name)
    if converted < 0:
        raise CanonicalContentValidationError(
            f"{field_name} must be non-negative"
        )
    return converted


def _sha256_text(value: object, field_name: str) -> str:
    """Require canonical lowercase SHA-256 text at every integrity boundary."""
    text = _required_text(value, field_name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CanonicalContentValidationError(
            f"{field_name} must be 64 lowercase hexadecimal characters"
        )
    return text


def _logical_uri(value: object, field_name: str) -> str:
    """Require a provider-neutral logical URI and reject local file locations."""
    text = _required_text(value, field_name)
    if (
        "://" not in text
        or text.startswith("file://")
        or "\\" in text
        or text.startswith("/")
    ):
        raise CanonicalContentValidationError(
            f"{field_name} must be a provider-neutral logical URI"
        )
    return text


def _optional_logical_path(value: object, field_name: str) -> str | None:
    """Accept a safe relative POSIX source path without exposing local OS paths."""
    if value is None:
        return None
    text = _required_text(value, field_name)
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or any(part in {"", ".", ".."} for part in path.parts):
        raise CanonicalContentValidationError(
            f"{field_name} must be a safe relative logical path"
        )
    return text


__all__ = [
    "CANONICAL_CONTENT_SCHEMA_VERSION",
    "CanonicalArtifactDescriptor",
    "CanonicalContentArtifact",
    "CanonicalContentBuilder",
    "CanonicalContentError",
    "CanonicalContentValidationError",
    "CanonicalOwnershipError",
    "CanonicalReferenceError",
    "CanonicalRelation",
    "CanonicalResource",
    "CanonicalText",
    "ContentNode",
    "Division",
    "NativeBinding",
    "NativeBindingValidationError",
    "PresentationUnit",
    "ProcessingActivity",
    "Representation",
    "SourceSelector",
]
