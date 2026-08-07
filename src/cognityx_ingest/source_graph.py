"""Build and resolve Cognityx v3.2 source-evidence graphs and addresses.

Purpose
-------
Canonical content owns extracted text and parser-neutral records, but downstream
systems also need a connected, stable map of resources, divisions, node
ownership, explicit relations, and exact evidence locations. This module turns
those existing identities into the Cognityx Source Graph and resolves structured
provenance addresses without reopening a source or parser payload.

Design principles
-----------------
The graph records only observed canonical identities and explicit relations. It
is deterministic, immutable, parser-neutral, JSON serializable, and contains no
copied canonical text. Ambiguous, unresolved, contradicted, and rejected edges
remain visible but can never enter default gold traversal. A compact adapter
loads the frozen T08 fixture exactly; a separate complete production form carries
text-free references to nodes, selectors, representations, native bindings,
activities, and artifact descriptors. Mixed persisted shapes are rejected.

Processing flow
---------------
``SourceGraphBuilder`` validates one or more ``CanonicalContentArtifact`` values,
projects their existing IDs without parsing or inference, sorts production facts,
and hashes the graph content (excluding the revision field) into a stable
revision. ``SourceGraphRepository`` retains immutable revisions in process or
loads the two frozen fixture files through strict duplicate-key readers.
``ProvenanceAddressResolver`` then validates strong immutable addresses, selects
logical versions only through explicit metadata and deterministic policy, and
requires every evidence-set member to resolve without substitution.

Primary consumers
-----------------
``IngestService`` persists the production graph and generated strong addresses.
Audit tools resolve exact support. T09 DataForge will traverse validated edges for
paragraph Q/A and composite Knowledge Units. T10 may expose these already-stable
records through SDK and CLI surfaces without changing this module's semantics.

Ownership boundary
------------------
Ingest owns source structure, explicit relation state, revisions, addresses, and
deterministic resolution. Canonical content remains the only text owner. Storage
owns bytes. Existing control services own enterprise authorization. DataForge
owns claims, Knowledge Units, semantic graphs, embeddings, and query retrieval.

Non-goals
---------
This module does not implement a semantic Knowledge Graph, entity or claim
extraction, graph databases, GraphRAG, ranking, embeddings, parser execution,
network/provider/LLM calls, business-version inference, or SDK/CLI commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Mapping, Protocol, Sequence

from cognityx_ingest.canonical_content import (
    CanonicalContentArtifact,
    SourceSelector,
)


SOURCE_GRAPH_SCHEMA = "cognityx.ingest.source-graph/v3.2"
PROVENANCE_ADDRESS_SCHEMA = "cognityx.ingest.provenance-address/v3.2"
PROVENANCE_RESOLUTION_STATUSES = (
    "exact",
    "redirected",
    "ambiguous",
    "obsolete",
    "forbidden",
    "unresolved",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_GOLD_STATES = frozenset(
    {"ambiguous", "unresolved", "contradicted", "rejected"}
)
_CONCRETE_STATUSES = frozenset(
    {"validated", "human-validated", "resolved", "observed", "accepted"}
)


class SourceGraphError(Exception):
    """Base typed failure for source-graph construction and lookup.

    Responsibility:
        Keep JSON, mapping, and file implementation failures behind one bounded
        domain family that never includes source text or parser payloads.
    Constructed by:
        Strict readers, validators, builders, repositories, and traversal seams.
    Used by:
        Ingest orchestration, audit readers, T09 handoff, and tests.
    Main algorithm:
        Preserve a safe logical reason while chaining low-level exceptions only
        for local diagnostics.
    Invariants:
        Messages contain bounded record IDs, never credentials or source bytes.
    Lifecycle and side effects:
        Exceptions are transient and perform no I/O or repair.
    Typed failures and trust boundary:
        This is the public boundary replacing raw JSON and ``KeyError`` failures.
    Thread-safety assumptions:
        Instances carry immutable diagnostic text and no shared mutable state.
    """


class SourceGraphValidationError(SourceGraphError, ValueError):
    """Reject malformed records or unsupported persisted graph shapes.

    Responsibility:
        Protect graph invariants before untrusted or caller-built data is used.
    Constructed by:
        Record and aggregate validation.
    Used by:
        Fixture readers, production builders, persistence, and traversal callers.
    Main algorithm:
        Validate closed field sets, bounded scalars, ordering, and relation state.
    Invariants:
        No invalid aggregate is returned as usable graph truth.
    Lifecycle and side effects:
        Read-only, repeatable validation mutates nothing.
    Typed failures and trust boundary:
        Replaces raw type/value/JSON errors at persisted-data boundaries.
    Thread-safety assumptions:
        Validation uses method-local indexes only.
    """


class SourceGraphReferenceError(SourceGraphValidationError):
    """Report a dangling resource, hierarchy, node, selector, or edge reference.

    Responsibility:
        Distinguish referential corruption from malformed scalar values.
    Constructed by:
        Aggregate index and ownership validation.
    Used by:
        Builders, repositories, traversal, and address resolution.
    Main algorithm:
        Resolve every persisted ID against immutable local indexes.
    Invariants:
        A validated graph has no required dangling reference.
    Lifecycle and side effects:
        Detection is read-only and idempotent.
    Typed failures and trust boundary:
        Untrusted IDs fail without leaking unrelated records.
    Thread-safety assumptions:
        Local immutable lookups are safe for concurrent readers.
    """


class SourceGraphRevisionError(SourceGraphError):
    """Report missing or conflicting immutable graph revisions.

    Responsibility:
        Prevent one revision label from identifying two different graph contents.
    Constructed by:
        ``SourceGraphRepository`` registration and lookup.
    Used by:
        Ingest composition and provenance resolution.
    Main algorithm:
        Compare deterministic serializer bytes for duplicate revision labels.
    Invariants:
        One repository revision always has one immutable byte representation.
    Lifecycle and side effects:
        Failed registration changes no repository state.
    Typed failures and trust boundary:
        Missing and conflicting revisions do not leak filesystem details.
    Thread-safety assumptions:
        Repository state is frozen after construction.
    """


class ProvenanceAddressError(Exception):
    """Base typed failure for address catalog and resolver contracts.

    Responsibility:
        Separate malformed address data from normal resolution outcomes.
    Constructed by:
        Address readers, validators, catalog builders, and resolver composition.
    Used by:
        Ingest persistence, audit clients, and T09/T10 adapters.
    Main algorithm:
        Bound trust failures while reserving six status values for valid requests.
    Invariants:
        Diagnostics never contain selected source text or protected target facts.
    Lifecycle and side effects:
        Transient, read-only failures are never persisted as evidence.
    Typed failures and trust boundary:
        Replaces raw JSON, mapping, and callback exceptions.
    Thread-safety assumptions:
        Instances hold no mutable shared state.
    """


class ProvenanceAddressValidationError(ProvenanceAddressError, ValueError):
    """Reject malformed address records or inconsistent catalog membership.

    Responsibility:
        Enforce closed address families, selector safety, and evidence closure.
    Constructed by:
        Strict deserialization and catalog validation.
    Used by:
        Resolver composition and immutable persistence.
    Main algorithm:
        Validate exact fields, IDs, targets, ranges, and member references.
    Invariants:
        A validated catalog has unique IDs and resolvable strong members.
    Lifecycle and side effects:
        Validation is deterministic, idempotent, and read-only.
    Typed failures and trust boundary:
        Invalid external JSON never reaches resolution.
    Thread-safety assumptions:
        Only method-local indexes are created.
    """


class ProvenanceAddressNotFoundError(ProvenanceAddressError):
    """Report a direct catalog lookup that requires an existing address.

    Responsibility:
        Support strict application lookups separately from resolver ``unresolved``.
    Constructed by:
        ``ProvenanceAddressCatalog.get``.
    Used by:
        Administrative and audit code that treats absence as exceptional.
    Main algorithm:
        Search all immutable address families by one bounded ID.
    Invariants:
        No substitute or similarly named address is returned.
    Lifecycle and side effects:
        Lookup performs no writes and is idempotent.
    Typed failures and trust boundary:
        Missing IDs do not reveal catalog contents.
    Thread-safety assumptions:
        Frozen tuples support concurrent reads.
    """


class ProvenanceResolutionError(ProvenanceAddressError):
    """Report invalid resolver policy behavior rather than inventing evidence.

    Responsibility:
        Fail closed when injected access or version policy violates its protocol.
    Constructed by:
        ``ProvenanceAddressResolver`` policy validation.
    Used by:
        Production composition and security-sensitive audit callers.
    Main algorithm:
        Check policy outputs against known candidates before exposing a target.
    Invariants:
        A policy can filter or select known facts but cannot create graph facts.
    Lifecycle and side effects:
        Resolution remains read-only; failures publish nothing.
    Typed failures and trust boundary:
        Untrusted callback exceptions are wrapped without target leakage.
    Thread-safety assumptions:
        Callers must provide thread-safe policies when sharing a resolver.
    """


@dataclass(frozen=True, slots=True)
class SourceGraphResource:
    """Reference one immutable resource and optional explicit business identity.

    Responsibility:
        Connect a canonical resource ID and source hash to family/version facts
        only when application composition supplies those facts explicitly.
    Constructed by:
        The compact fixture adapter or ``SourceGraphBuilder``.
    Used by:
        Hierarchy validation, strong addresses, and logical version resolution.
    Main algorithm:
        Retain values unchanged; never infer family or version from filenames.
    Invariants:
        ID and SHA-256 are valid; family and version are both present or absent.
    Lifecycle and persistence:
        Frozen records live for one immutable graph revision.
    Side effects and typed failures:
        None; aggregate validation raises ``SourceGraphValidationError``.
    Trust boundary and thread-safety assumptions:
        Values are untrusted until graph validation; frozen scalars are shareable.
    """

    resource_id: str
    source_sha256: str
    family_id: str | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class SourceGraphPresentationUnit:
    """Connect one physical or temporal presentation surface to its resource.

    Responsibility:
        Preserve page, slide, sheet, frame, or document identity without making
        that presentation boundary a logical division or copying source content.
    Constructed by:
        Fixture loading or production canonical projection.
    Used by:
        Selectors, provenance readers, and future DataForge handoff.
    Main algorithm:
        Reuse the canonical presentation ID, resource ID, and unit type exactly.
    Invariants:
        IDs are nonempty and resource membership resolves.
    Lifecycle and persistence:
        Immutable for the graph revision and serialized in deterministic order.
    Side effects and typed failures:
        None; graph validation reports invalid references.
    Trust boundary and thread-safety assumptions:
        Frozen scalar values are safe after aggregate validation.
    """

    presentation_unit_id: str
    resource_id: str
    unit_type: str


@dataclass(frozen=True, slots=True)
class SourceGraphDivision:
    """Model logical hierarchy and direct node ownership without subtree copies.

    Responsibility:
        Preserve generalized division roles, reciprocal hierarchy, optional
        business numbering, and each deepest owner's ordered canonical node IDs.
    Constructed by:
        Fixture loading or ``SourceGraphBuilder`` from canonical divisions.
    Used by:
        Traversal, logical addressing, strong-target validation, and T09.
    Main algorithm:
        Refer to existing IDs; subtree content is recovered by traversal only.
    Invariants:
        Hierarchy is reciprocal/acyclic and every direct node has one owner.
    Lifecycle and persistence:
        Frozen reference records contain no title or source text copies.
    Side effects and typed failures:
        None; aggregate validation raises typed ownership/reference failures.
    Trust boundary and thread-safety assumptions:
        Tuples are untrusted until validation and immutable afterward.
    """

    division_id: str
    resource_id: str
    division_role: str
    parent_division_id: str | None
    child_division_ids: tuple[str, ...]
    direct_node_ids: tuple[str, ...]
    number: str | None = None


@dataclass(frozen=True, slots=True)
class SourceGraphRelation:
    """Retain one explicit relation and its epistemic/gold safety state.

    Responsibility:
        Represent concrete, candidate-bearing, or unresolved source relations
        without converting ambiguity into an accepted semantic edge.
    Constructed by:
        Fixture loading or production projection of ``CanonicalRelation`` facts.
    Used by:
        Gold-safe graph traversal, audit, and T09 relation expansion.
    Main algorithm:
        Preserve target/candidates and filter unsafe states only at traversal.
    Invariants:
        Concrete states have targets; ambiguous states are targetless/non-gold;
        candidates are unique; unsafe states can never be gold eligible.
    Lifecycle and persistence:
        Frozen and serialized for one graph revision; no relation is inferred.
    Side effects and typed failures:
        None; validation raises bounded graph errors.
    Trust boundary and thread-safety assumptions:
        External state is untrusted until aggregate validation; tuples are safe.
    """

    relation_id: str
    source_id: str
    target_id: str | None
    relation_type: str
    status: str
    epistemic_state: str
    gold_eligible: bool
    candidate_target_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceGraphContentNode:
    """Project a canonical text node as IDs and selectors, never copied text.

    Responsibility:
        Bind node identity, resource, deepest division owner, kind, and selector
        IDs into the complete production graph.
    Constructed by:
        ``SourceGraphBuilder`` from validated canonical content.
    Used by:
        Strong-address generation, ownership checks, and downstream bindings.
    Main algorithm:
        Reuse canonical IDs exactly while deliberately omitting ``content``.
    Invariants:
        Resource, owner, and selectors resolve and agree on resource membership.
    Lifecycle and persistence:
        Immutable production-only graph record survives native payload purge.
    Side effects and typed failures:
        None; validation fails typed before serialization.
    Trust boundary and thread-safety assumptions:
        Constructed from validated canonical input; frozen tuples are shareable.
    """

    node_id: str
    resource_id: str
    owner_division_id: str
    node_kind: str
    selector_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceGraphSelector:
    """Project one canonical source locator without opening or quoting its source.

    Responsibility:
        Preserve selector identity, range, geometry, presentation, and anchor IDs
        needed for exact provenance in the complete production graph.
    Constructed by:
        ``SourceGraphBuilder`` from embedded canonical selectors.
    Used by:
        Strong-address builders, audit tooling, and target validation.
    Main algorithm:
        Copy locator facts only; source paths remain logical relative locators.
    Invariants:
        Ranges/boxes are ordered, paths are safe, and at least one locator exists.
    Lifecycle and persistence:
        Immutable metadata remains after parser-native payload purge.
    Side effects and typed failures:
        No I/O; malformed locators raise typed validation failures.
    Trust boundary and thread-safety assumptions:
        Source paths are never treated as OS paths; frozen values are shareable.
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
    parser_source_anchor_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceGraphRepresentation:
    """Connect a canonical representation to its subject and supporting IDs.

    Responsibility:
        Retain object/image/table/media representation lineage without embedding
        represented payload or text.
    Constructed by:
        ``SourceGraphBuilder`` from canonical representations.
    Used by:
        Audit, projection descriptors, strong targets, and later DataForge work.
    Main algorithm:
        Reuse canonical subject, selector, caption, and artifact references.
    Invariants:
        Every referenced canonical ID exists in the complete production graph,
        and representation-to-representation subject lineage is acyclic.
    Lifecycle and persistence:
        Immutable metadata may outlive independently retained parser artifacts.
    Side effects and typed failures:
        None; dangling references fail graph validation.
    Trust boundary and thread-safety assumptions:
        No payload is trusted or loaded; frozen values support concurrent reads.
    """

    representation_id: str
    subject_id: str
    representation_type: str
    artifact_id: str | None
    selector_ids: tuple[str, ...]
    caption_node_id: str | None


@dataclass(frozen=True, slots=True)
class SourceGraphNativeBinding:
    """Carry a canonical-to-native pointer as purge-independent metadata.

    Responsibility:
        Keep exact T01 binding identity in the graph without loading parser bytes.
    Constructed by:
        ``SourceGraphBuilder`` from validated canonical NativeBindings.
    Used by:
        Audit and future native-aware adapters while payload retention permits.
    Main algorithm:
        Reuse binding, canonical, artifact, pointer, and role fields unchanged.
    Invariants:
        Canonical and artifact IDs resolve in the complete production graph.
    Lifecycle and persistence:
        Metadata survives T07 purge; pointer dereference may later be unavailable.
    Side effects and typed failures:
        None; validation never invokes the native store.
    Trust boundary and thread-safety assumptions:
        Pointer is opaque metadata and frozen records are shareable.
    """

    binding_id: str
    canonical_id: str
    artifact_id: str
    native_pointer: str
    binding_role: str


@dataclass(frozen=True, slots=True)
class SourceGraphProcessingActivity:
    """Preserve one canonical processing activity as compact lineage references.

    Responsibility:
        Connect the graph to run/correlation, parser, method, and artifact IDs.
    Constructed by:
        ``SourceGraphBuilder`` from canonical activities.
    Used by:
        Provenance audit and future graph-projection descriptors.
    Main algorithm:
        Reuse canonical activity facts with deterministic tuple ordering.
    Invariants:
        Every input/output artifact ID resolves; no execution log is embedded.
    Lifecycle and persistence:
        Frozen lineage remains after transient execution and payload cleanup.
    Side effects and typed failures:
        None; dangling artifacts fail validation.
    Trust boundary and thread-safety assumptions:
        IDs are metadata only and immutable for concurrent readers.
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
class SourceGraphArtifactDescriptor:
    """Reference a durable artifact supporting the complete production graph.

    Responsibility:
        Retain role, URI, media type, hash, and schema without copying bytes.
    Constructed by:
        ``SourceGraphBuilder`` from canonical artifact descriptors.
    Used by:
        Activity validation, audit, and derived projection lineage.
    Main algorithm:
        Reuse provider-neutral descriptor fields exactly.
    Invariants:
        IDs/URIs are nonempty and optional SHA-256 is valid.
    Lifecycle and persistence:
        The descriptor is frozen; Storage independently owns payload lifecycle.
    Side effects and typed failures:
        None; validation reports malformed descriptors.
    Trust boundary and thread-safety assumptions:
        A URI is metadata, never opened here; frozen values are safe to share.
    """

    artifact_id: str
    role: str
    uri: str
    media_type: str
    sha256: str | None = None
    schema_version: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceVersionMetadata:
    """Supply explicit business family and version facts to graph construction.

    Responsibility:
        Keep logical addressing metadata caller-owned and prevent filename/hash
        inference inside Ingest.
    Constructed by:
        Trusted application composition with authoritative business metadata.
    Used by:
        ``SourceGraphBuilder`` and deterministic logical resolution.
    Main algorithm:
        Bind one existing resource ID to exact family and version strings.
    Invariants:
        All three values are nonempty and apply to the same canonical resource.
    Lifecycle and persistence:
        Copied into an immutable graph revision when supplied.
    Side effects and typed failures:
        None; unknown resources fail typed graph construction.
    Trust boundary and thread-safety assumptions:
        Metadata is trusted only after explicit validation; scalars are immutable.
    """

    resource_id: str
    family_id: str
    version: str


@dataclass(frozen=True, slots=True)
class GraphProjectionDescriptor:
    """Describe a replaceable downstream graph projection and source bindings.

    Responsibility:
        Record that a semantic/retrieval graph was derived from one Source Graph
        revision using a named adapter/configuration/model and exact support IDs.
    Constructed by:
        Future downstream graph adapters, not T08 graph construction.
    Used by:
        Audit and lifecycle tooling that must distinguish projection from truth.
    Main algorithm:
        Validate content-bound configuration identity and ordered support IDs.
    Invariants:
        Descriptor is lineage metadata only and never promotes inferred edges.
    Lifecycle and persistence:
        Immutable per projection; changing adapter/config creates a new descriptor.
    Side effects and typed failures:
        None; ``validate`` raises ``SourceGraphValidationError``.
    Trust boundary and thread-safety assumptions:
        Model/adapter IDs are opaque metadata; frozen records are shareable.
    """

    projection_id: str
    source_graph_revision: str
    adapter_id: str
    adapter_version: str
    configuration_sha256: str
    support_ids: tuple[str, ...]
    retention_policy: str
    model_id: str | None = None
    model_version: str | None = None

    def validate(self) -> None:
        """Validate projection lineage without constructing a semantic graph.

        Future adapters call this before persistence. It checks bounded IDs,
        SHA-256, paired model metadata, unique sorted support IDs, and retention
        text. The method performs no I/O, is idempotent, and raises only typed
        graph validation failures at this metadata trust boundary.
        """
        for value, name in (
            (self.projection_id, "projection_id"),
            (self.source_graph_revision, "source_graph_revision"),
            (self.adapter_id, "adapter_id"),
            (self.adapter_version, "adapter_version"),
            (self.retention_policy, "retention_policy"),
        ):
            _require_text(value, name)
        _require_sha256(self.configuration_sha256, "configuration_sha256")
        if (self.model_id is None) != (self.model_version is None):
            raise SourceGraphValidationError(
                "Projection model_id and model_version must be supplied together"
            )
        _require_unique_ordered(self.support_ids, "projection support_ids")


@dataclass(frozen=True, slots=True)
class ProvenanceTarget:
    """Name exactly one canonical target without carrying target content.

    Responsibility:
        Give strong, logical, and evidence-set resolutions one closed target shape
        for nodes, divisions, or representations and optional node character span.
    Constructed by:
        Address readers, strong-address builders, and the resolver.
    Used by:
        Audit clients and T09 support records.
    Main algorithm:
        Enforce exactly one target ID and a complete non-negative span pair.
    Invariants:
        No target contains source text; span is permitted only for a node target.
    Lifecycle and persistence:
        Frozen values persist in addresses or transient resolution results.
    Side effects and typed failures:
        None; validation raises ``ProvenanceAddressValidationError``.
    Trust boundary and thread-safety assumptions:
        IDs are untrusted until resolution and immutable afterward.
    """

    node_id: str | None = None
    division_id: str | None = None
    representation_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    def validate(self) -> None:
        """Enforce one bounded target and optional complete node span.

        Catalog readers and resolution call this pure, idempotent check before
        graph lookup. It returns ``None``, performs no I/O, and raises a typed
        address validation failure without exposing any graph target details.
        """
        selected = tuple(
            value
            for value in (self.node_id, self.division_id, self.representation_id)
            if value is not None
        )
        if len(selected) != 1:
            raise ProvenanceAddressValidationError(
                "A provenance target must select exactly one canonical ID"
            )
        _require_address_text(selected[0], "canonical target ID")
        if (self.char_start is None) != (self.char_end is None):
            raise ProvenanceAddressValidationError(
                "Target character range must supply both bounds"
            )
        if self.char_start is not None:
            if self.node_id is None:
                raise ProvenanceAddressValidationError(
                    "Only a node target may carry a character range"
                )
            _validate_range(
                self.char_start,
                self.char_end,
                error_type=ProvenanceAddressValidationError,
                label="target character range",
            )

    @property
    def target_id(self) -> str:
        """Return the selected canonical ID after pure target validation.

        Resolver and access-policy composition use this convenience property. It
        performs no I/O or mutation, is idempotent, and raises the same typed
        validation failure if a caller constructed an invalid direct record.
        """
        self.validate()
        return self.node_id or self.division_id or self.representation_id or ""


@dataclass(frozen=True, slots=True)
class ProvenanceSelector:
    """Describe an exact source locator without treating it as a path to open.

    Responsibility:
        Preserve frozen text-position selectors and richer canonical selector
        metadata while never copying selected text or loading source bytes.
    Constructed by:
        Strict address readers or strong-address production builders.
    Used by:
        Strong address validation and exact audit responses.
    Main algorithm:
        Validate relative logical paths, paired ranges, boxes, and locator presence.
    Invariants:
        Paths are bounded/non-traversing and ranges are non-negative/ordered.
    Lifecycle and persistence:
        Immutable address metadata survives parser-native payload purge.
    Side effects and typed failures:
        Validation performs no I/O and raises typed address errors.
    Trust boundary and thread-safety assumptions:
        ``source_path`` is opaque logical metadata; frozen values are shareable.
    """

    selector_type: str
    source_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    selector_id: str | None = None
    presentation_unit_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_anchor_ids: tuple[str, ...] = ()
    parser_source_anchor_ids: tuple[str, ...] = ()

    def validate(self) -> None:
        """Validate selector safety and complete locator structure.

        Address catalog and resolver callers invoke this at their trust boundary.
        The algorithm checks bounded strings, logical relative paths, paired
        ranges, finite ordered geometry, and at least one locator. It is pure and
        idempotent; failures are typed and never cause filesystem access.
        """
        _require_address_text(self.selector_type, "selector_type")
        for value, name in (
            (self.selector_id, "selector_id"),
            (self.presentation_unit_id, "presentation_unit_id"),
        ):
            if value is not None:
                _require_address_text(value, name)
        if self.source_path is not None:
            _validate_logical_path(self.source_path)
        if (self.char_start is None) != (self.char_end is None):
            raise ProvenanceAddressValidationError(
                "Selector character range must supply both bounds"
            )
        if self.char_start is not None:
            _validate_range(
                self.char_start,
                self.char_end,
                error_type=ProvenanceAddressValidationError,
                label="selector character range",
            )
        if self.bbox is not None:
            _validate_bbox(self.bbox, ProvenanceAddressValidationError)
        _require_unique(self.source_anchor_ids, "selector source_anchor_ids")
        _require_unique(
            self.parser_source_anchor_ids,
            "selector parser_source_anchor_ids",
        )
        if not any(
            (
                self.source_path is not None,
                self.char_start is not None,
                self.selector_id is not None,
                self.presentation_unit_id is not None,
                self.bbox is not None,
                bool(self.source_anchor_ids),
                bool(self.parser_source_anchor_ids),
            )
        ):
            raise ProvenanceAddressValidationError(
                "A provenance selector requires at least one concrete locator"
            )


@dataclass(frozen=True, slots=True)
class StrongProvenanceAddress:
    """Bind immutable evidence to source hash, graph revision, target, and selectors.

    Responsibility:
        Provide exact reproducible support that is never redirected to newer data.
    Constructed by:
        Frozen catalog loading or ``build_strong_address_catalog``.
    Used by:
        Resolver, audit, QA support, and evidence-set members.
    Main algorithm:
        Validate local shape first; resolver then proves all graph bindings.
    Invariants:
        SHA/revision/resource/target are present and selectors are valid.
    Lifecycle and persistence:
        Immutable address remains tied to one frozen Source Graph revision.
    Side effects and typed failures:
        None; shape errors are typed and failed resolution returns a status.
    Trust boundary and thread-safety assumptions:
        External IDs are untrusted until graph resolution; records are frozen.
    """

    address_id: str
    source_sha256: str
    graph_revision: str
    resource_id: str
    canonical_target: ProvenanceTarget
    selectors: tuple[ProvenanceSelector, ...] = ()

    def validate(self) -> None:
        """Validate immutable address shape without claiming graph resolution.

        Catalog construction calls this before aggregate membership checks. It
        validates bounded IDs, SHA-256, one target, and every selector in order.
        It performs no I/O, is idempotent, and raises typed address errors only.
        """
        _require_address_text(self.address_id, "address_id")
        _require_address_sha256(self.source_sha256, "source_sha256")
        _require_address_text(self.graph_revision, "graph_revision")
        _require_address_text(self.resource_id, "resource_id")
        self.canonical_target.validate()
        for selector in self.selectors:
            selector.validate()


@dataclass(frozen=True, slots=True)
class LogicalProvenanceAddress:
    """Name business-stable evidence through explicit family and version policy.

    Responsibility:
        Let callers request a numbered division in a resource family without
        pretending that the logical request is itself immutable evidence.
    Constructed by:
        Business-aware application code or the frozen catalog adapter.
    Used by:
        Resolver with explicit graph family/version metadata and version policy.
    Main algorithm:
        Match family/division candidates, then permit deterministic policy choice.
    Invariants:
        Address, family, version rule, and division reference are nonempty.
    Lifecycle and persistence:
        Frozen indirection may resolve differently in a later graph context.
    Side effects and typed failures:
        None; malformed records fail typed and normal absence returns unresolved.
    Trust boundary and thread-safety assumptions:
        No lexical version inference is trusted; frozen scalars are shareable.
    """

    address_id: str
    resource_family_id: str
    version_rule: str
    division_reference: str

    def validate(self) -> None:
        """Validate bounded logical address terms before candidate selection.

        Catalog readers call this pure check. It returns ``None``, performs no
        version lookup or I/O, is idempotent, and raises typed validation failures
        for missing values without disclosing candidate resources.
        """
        for value, name in (
            (self.address_id, "address_id"),
            (self.resource_family_id, "resource_family_id"),
            (self.version_rule, "version_rule"),
            (self.division_reference, "division_reference"),
        ):
            _require_address_text(value, name)


@dataclass(frozen=True, slots=True)
class EvidenceSetAddress:
    """Bind one composite claim closure to ordered strong-address members.

    Responsibility:
        Preserve claim/member pairing and order without copying evidence or
        replacing an unavailable member with another target.
    Constructed by:
        Explicit DataForge/business composition or frozen catalog loading.
    Used by:
        Resolver and future T09 composite Knowledge Unit support.
    Main algorithm:
        Resolve each required strong member in order and aggregate conservatively.
    Invariants:
        Claim/member counts match, IDs are unique, and every member is strong.
    Lifecycle and persistence:
        Immutable intent record; member address identities remain authoritative.
    Side effects and typed failures:
        None; malformed closure fails typed before resolution.
    Trust boundary and thread-safety assumptions:
        Claim intent is explicit trusted input, not inferred; tuples are frozen.
    """

    address_id: str
    claim_ids: tuple[str, ...]
    member_address_ids: tuple[str, ...]

    def validate(self) -> None:
        """Validate nonempty one-to-one ordered claim/member closure.

        Catalog construction calls this before resolving member IDs. It checks
        counts, bounded values, and uniqueness without sorting away intent order.
        The pure check is idempotent and raises typed address validation failures.
        """
        _require_address_text(self.address_id, "address_id")
        if not self.claim_ids or len(self.claim_ids) != len(self.member_address_ids):
            raise ProvenanceAddressValidationError(
                "Evidence-set claim and member counts must match and be nonempty"
            )
        for value in (*self.claim_ids, *self.member_address_ids):
            _require_address_text(value, "evidence-set member")
        _require_unique(self.claim_ids, "evidence-set claim_ids")
        _require_unique(self.member_address_ids, "evidence-set member_address_ids")


@dataclass(frozen=True, slots=True)
class AddressResolution:
    """Return one of six deterministic outcomes without source-text duplication.

    Responsibility:
        Carry requested address, status, permitted exact/candidate/member targets,
        bounded reason, and graph revision while protecting denied target details.
    Constructed by:
        ``ProvenanceAddressResolver`` only after policy and graph validation.
    Used by:
        Audit clients, DataForge handoff, and future SDK/CLI adapters.
    Main algorithm:
        Validate one closed shape per status, recursively validate any evidence-set
        explanation, and never synthesize or leak a target.
    Invariants:
        Status is exact vocabulary; accepted, candidate, and member fields cannot
        contradict it; forbidden has no details; source text is structurally absent.
    Lifecycle and persistence:
        Transient immutable response; authoritative support remains the address.
    Side effects and typed failures:
        None; invalid direct construction fails ``validate``.
    Trust boundary and thread-safety assumptions:
        Protected details are removed before return; frozen tuples are shareable.
    """

    address_id: str
    status: str
    target: ProvenanceTarget | None = None
    targets: tuple[ProvenanceTarget, ...] = ()
    candidate_targets: tuple[ProvenanceTarget, ...] = ()
    member_resolutions: tuple[AddressResolution, ...] = ()
    reason: str = ""
    graph_revision: str | None = None

    def validate(self) -> None:
        """Validate the closed result shape before it crosses the public API.

        The resolver, future T09/T10 consumers, and direct constructors call this
        trust-boundary check. The algorithm validates bounded metadata and targets,
        recursively validates evidence members, rejects duplicate collected target
        identities, and applies the exact field shape for each of the six statuses.
        It performs no I/O or mutation, is deterministic and thread-safe for frozen
        records, and raises ``ProvenanceAddressValidationError`` rather than
        exposing contradictory accepted, candidate, or protected target details.
        """
        _require_address_text(self.address_id, "address_id")
        if self.status not in PROVENANCE_RESOLUTION_STATUSES:
            raise ProvenanceAddressValidationError(
                "Address resolution status is unsupported"
            )
        if self.reason:
            _require_address_text(self.reason, "resolution reason")
        if self.graph_revision is not None:
            _require_address_text(self.graph_revision, "graph_revision")
        if self.target is not None:
            self.target.validate()
        for item in (*self.targets, *self.candidate_targets):
            item.validate()
        for item in self.member_resolutions:
            item.validate()
        _require_unique(
            tuple(item.target_id for item in self.targets),
            "resolution target identities",
            ProvenanceAddressValidationError,
        )
        _require_unique(
            tuple(item.target_id for item in self.candidate_targets),
            "resolution candidate target identities",
            ProvenanceAddressValidationError,
        )

        if self.status == "exact":
            if self.candidate_targets:
                raise ProvenanceAddressValidationError(
                    "Exact resolution cannot expose candidate targets"
                )
            if self.target is not None:
                if self.targets:
                    raise ProvenanceAddressValidationError(
                        "One-address exact resolution cannot expose target collections"
                    )
                if any(item.status != "exact" for item in self.member_resolutions):
                    raise ProvenanceAddressValidationError(
                        "Exact resolution cannot contain non-exact members"
                    )
                return
            if not self.targets or not self.member_resolutions:
                raise ProvenanceAddressValidationError(
                    "Evidence-set exact resolution requires targets and members"
                )
            if len(self.targets) != len(self.member_resolutions) or any(
                item.status != "exact" or item.target != target
                for target, item in zip(self.targets, self.member_resolutions)
            ):
                raise ProvenanceAddressValidationError(
                    "Evidence-set exact targets must match ordered exact members"
                )
            return

        if self.status == "redirected":
            if (
                self.target is None
                or self.targets
                or self.candidate_targets
                or self.member_resolutions
            ):
                raise ProvenanceAddressValidationError(
                    "Redirected resolution requires exactly one accepted target"
                )
            return

        if self.status == "ambiguous":
            if self.target is not None or self.targets:
                raise ProvenanceAddressValidationError(
                    "Ambiguous resolution cannot expose an accepted target"
                )
            if any(
                item.target is not None or item.targets
                for item in self.member_resolutions
            ):
                raise ProvenanceAddressValidationError(
                    "Ambiguous member explanation cannot expose accepted targets"
                )
            return

        if self.status == "forbidden" and (
            self.target is not None
            or self.targets
            or self.candidate_targets
            or self.member_resolutions
        ):
            raise ProvenanceAddressValidationError(
                "Forbidden resolution cannot expose target details"
            )
        if self.status in {"unresolved", "obsolete"} and (
            self.target is not None or self.targets or self.candidate_targets
        ):
            raise ProvenanceAddressValidationError(
                "Non-resolving status cannot expose target details"
            )


@dataclass(frozen=True, slots=True)
class ProvenanceAddressCatalog:
    """Hold all three immutable address families under one strict schema.

    Responsibility:
        Validate unique IDs, ordered evidence closure, exact resolver vocabulary,
        and deterministic serialization for fixture and production addresses.
    Constructed by:
        Strict JSON loading, strong-address generation, or explicit composition.
    Used by:
        ``ProvenanceAddressResolver`` and Ingest persistence.
    Main algorithm:
        Validate each family, build local indexes, and prove every evidence member
        names an existing strong address without changing persisted order.
    Invariants:
        IDs are unique across families and outcome vocabulary is exact.
    Lifecycle and persistence:
        Frozen aggregate serializes immutably; indexes are never persisted.
    Side effects and typed failures:
        No I/O except explicit reader calls; malformed input fails typed.
    Trust boundary and thread-safety assumptions:
        Persisted JSON is untrusted; frozen tuples support concurrent reads.
    """

    schema: str
    strong_addresses: tuple[StrongProvenanceAddress, ...]
    logical_addresses: tuple[LogicalProvenanceAddress, ...]
    evidence_set_addresses: tuple[EvidenceSetAddress, ...]
    resolver_outcomes: tuple[str, ...] = PROVENANCE_RESOLUTION_STATUSES
    compact_fixture: bool = field(default=False, repr=False, compare=False)

    def validate(self) -> None:
        """Validate address families, global identity, and member closure.

        Readers, builders, and resolver composition call this before use. The
        algorithm validates every record, enforces one global ID namespace,
        checks exact outcome order, and resolves evidence members to strong IDs.
        It is read-only/idempotent and raises typed validation failures.
        """
        if self.schema != PROVENANCE_ADDRESS_SCHEMA:
            raise ProvenanceAddressValidationError(
                f"Unsupported provenance address schema: {self.schema}"
            )
        if self.resolver_outcomes != PROVENANCE_RESOLUTION_STATUSES:
            raise ProvenanceAddressValidationError(
                "Resolver outcomes must use the exact v3.2 ordering"
            )
        all_ids: list[str] = []
        for item in self.strong_addresses:
            item.validate()
            all_ids.append(item.address_id)
        for item in self.logical_addresses:
            item.validate()
            all_ids.append(item.address_id)
        for item in self.evidence_set_addresses:
            item.validate()
            all_ids.append(item.address_id)
        _require_unique(tuple(all_ids), "address IDs", ProvenanceAddressValidationError)
        strong_ids = {item.address_id for item in self.strong_addresses}
        for item in self.evidence_set_addresses:
            missing = tuple(
                address_id
                for address_id in item.member_address_ids
                if address_id not in strong_ids
            )
            if missing:
                raise ProvenanceAddressValidationError(
                    f"Evidence-set member is not a strong address: {missing[0]}"
                )

    def get(
        self, address_id: str
    ) -> StrongProvenanceAddress | LogicalProvenanceAddress | EvidenceSetAddress:
        """Return one exact address or raise a strict typed absence failure.

        Administrative callers use this when missing data is exceptional. The
        method validates the catalog, performs an exact ID scan without fuzzy
        fallback, returns a frozen record, and has no side effects. Resolver
        callers use ``find`` so normal unknown IDs become ``unresolved``.
        """
        item = self.find(address_id)
        if item is None:
            raise ProvenanceAddressNotFoundError(f"Unknown address: {address_id}")
        return item

    def find(
        self, address_id: str
    ) -> StrongProvenanceAddress | LogicalProvenanceAddress | EvidenceSetAddress | None:
        """Find an exact address for deterministic resolver use.

        ``ProvenanceAddressResolver`` calls this after catalog validation. The
        method scans immutable families in a fixed order, returns no substitute,
        performs no I/O, and is idempotent. Malformed IDs fail typed; valid absent
        IDs return ``None`` without revealing other catalog entries.
        """
        _require_address_text(address_id, "address_id")
        self.validate()
        return next(
            (
                item
                for item in (
                    *self.strong_addresses,
                    *self.logical_addresses,
                    *self.evidence_set_addresses,
                )
                if item.address_id == address_id
            ),
            None,
        )

    def to_dict(self) -> dict[str, object]:
        """Return deterministic JSON data while preserving authoritative order.

        Persistence calls this after validation. Compact fixture records serialize
        to the exact frozen field shape; production selectors may include their
        richer explicit locator fields. No source text or mutable index is emitted.
        Repeated calls are side-effect free and byte-stable after ``to_json_bytes``.
        """
        self.validate()
        return {
            "schema": self.schema,
            "strong_addresses": [
                _strong_address_to_dict(item, compact=self.compact_fixture)
                for item in self.strong_addresses
            ],
            "logical_addresses": [
                _logical_address_to_dict(item) for item in self.logical_addresses
            ],
            "evidence_set_addresses": [
                _evidence_address_to_dict(item)
                for item in self.evidence_set_addresses
            ],
            "resolver_outcomes": list(self.resolver_outcomes),
        }

    def to_json_bytes(self) -> bytes:
        """Serialize canonical UTF-8 JSON bytes with one trailing newline.

        Ingest persistence and immutable-retry checks call this method. It first
        validates the aggregate, sorts mapping keys while retaining list order,
        performs no I/O, and returns identical bytes for identical records.
        Typed address validation failures occur before serialization.
        """
        return _json_bytes(self.to_dict())

    @classmethod
    def from_json_bytes(
        cls, payload: bytes, *, compact_fixture: bool = False
    ) -> ProvenanceAddressCatalog:
        """Read untrusted address JSON with duplicate-key and field rejection.

        Fixture adapters and artifact readers call this explicit trust boundary.
        The algorithm decodes UTF-8, rejects duplicate object keys, requires the
        exact aggregate and nested field sets for compact or production form,
        constructs frozen records, then validates cross-family references. It
        performs no writes and wraps raw JSON/type errors in typed address errors.
        """
        value = _strict_json_loads(
            payload,
            error_type=ProvenanceAddressValidationError,
            label="provenance address catalog",
        )
        catalog = _parse_address_catalog(value, compact_fixture=compact_fixture)
        catalog.validate()
        return catalog


@dataclass(frozen=True, slots=True)
class SourceGraph:
    """Aggregate one immutable connected source/provenance graph revision.

    Responsibility:
        Hold compact structural truth and, for production graphs, text-free
        canonical node/selector/binding/activity/artifact references.
    Constructed by:
        The strict fixture adapter or ``SourceGraphBuilder``.
    Used by:
        Repository lookup, deterministic traversal, address resolution, and T09.
    Main algorithm:
        Build local indexes, validate hierarchy/ownership/relations, and serialize
        either the exact compact form or the complete production form.
    Invariants:
        IDs are unique, links resolve, division and representation lineage is
        acyclic, node ownership is singular, unsafe relations are non-gold, and
        canonical text is absent.
    Lifecycle and persistence:
        One frozen instance represents one immutable content-bound revision.
    Side effects and typed failures:
        Methods perform no I/O; validation/traversal raise typed graph failures.
    Trust boundary and thread-safety assumptions:
        Persisted facts are untrusted until ``validate``; all state is immutable.
    """

    schema: str
    graph_revision: str
    resources: tuple[SourceGraphResource, ...]
    presentation_units: tuple[SourceGraphPresentationUnit, ...]
    divisions: tuple[SourceGraphDivision, ...]
    relations: tuple[SourceGraphRelation, ...]
    content_nodes: tuple[SourceGraphContentNode, ...] = ()
    selectors: tuple[SourceGraphSelector, ...] = ()
    representations: tuple[SourceGraphRepresentation, ...] = ()
    native_bindings: tuple[SourceGraphNativeBinding, ...] = ()
    processing_activities: tuple[SourceGraphProcessingActivity, ...] = ()
    artifact_descriptors: tuple[SourceGraphArtifactDescriptor, ...] = ()
    address_catalog: ProvenanceAddressCatalog | None = field(
        default=None, repr=False, compare=False
    )
    compact_fixture: bool = field(default=False, repr=False, compare=False)

    def validate(self) -> None:
        """Validate schema, records, hierarchy, ownership, and relation safety.

        Builders, readers, traversal, and resolver composition call this before
        use. The algorithm validates scalar shapes, builds unique local indexes,
        proves parent/child reciprocity and acyclicity, assigns every direct node
        once, validates complete production references and acyclic representation
        subjects, and checks edge targets and epistemic/gold consistency. It is
        pure/idempotent and raises bounded validation/reference errors without
        source text or unbounded recursive traversal.
        """
        if self.schema != SOURCE_GRAPH_SCHEMA:
            raise SourceGraphValidationError(
                f"Unsupported source graph schema: {self.schema}"
            )
        _require_text(self.graph_revision, "graph_revision")
        indexes = _validate_graph_records(self)
        _validate_graph_hierarchy(self, indexes)
        _validate_graph_production_facts(self, indexes)
        _validate_graph_relations(self, indexes)
        if not self.compact_fixture:
            _validate_production_ordering(self)
        if self.compact_fixture and any(
            (
                self.content_nodes,
                self.selectors,
                self.representations,
                self.native_bindings,
                self.processing_activities,
                self.artifact_descriptors,
            )
        ):
            raise SourceGraphValidationError(
                "Compact Source Graph cannot contain production-only fields"
            )
        if self.address_catalog is not None:
            self.address_catalog.validate()

    def to_dict(self, *, include_revision: bool = True) -> dict[str, object]:
        """Return compact or production JSON data without copied canonical text.

        Revision hashing and persistence call this after validation. The compact
        fixture emits exactly six top-level fields; production emits the complete
        closed text-free form. ``include_revision=False`` removes the self-field
        from hash input. The method is deterministic, read-only, and raises typed
        validation failures before returning data.
        """
        self.validate()
        value: dict[str, object] = {
            "schema": self.schema,
            "resources": [_graph_resource_to_dict(item) for item in self.resources],
            "presentation_units": [
                _graph_presentation_to_dict(item) for item in self.presentation_units
            ],
            "divisions": [_graph_division_to_dict(item) for item in self.divisions],
            "relations": [_graph_relation_to_dict(item) for item in self.relations],
        }
        if include_revision:
            value["graph_revision"] = self.graph_revision
        if not self.compact_fixture:
            value.update(
                {
                    "content_nodes": [
                        _graph_node_to_dict(item) for item in self.content_nodes
                    ],
                    "selectors": [
                        _graph_selector_to_dict(item) for item in self.selectors
                    ],
                    "representations": [
                        _graph_representation_to_dict(item)
                        for item in self.representations
                    ],
                    "native_bindings": [
                        _graph_binding_to_dict(item) for item in self.native_bindings
                    ],
                    "processing_activities": [
                        _graph_activity_to_dict(item)
                        for item in self.processing_activities
                    ],
                    "artifact_descriptors": [
                        _graph_artifact_to_dict(item)
                        for item in self.artifact_descriptors
                    ],
                }
            )
        return value

    def to_json_bytes(self) -> bytes:
        """Serialize validated graph facts to canonical UTF-8 JSON bytes.

        Repository conflict checks and Ingest persistence call this method. It
        sorts mapping keys, preserves validated tuple order, appends one newline,
        performs no I/O, and is idempotent. Source text cannot appear because no
        graph record owns a text field. Typed validation failures occur first.
        """
        return _json_bytes(self.to_dict())

    @classmethod
    def from_json_bytes(
        cls, payload: bytes, *, compact_fixture: bool = False
    ) -> SourceGraph:
        """Read one strict compact or production graph from untrusted JSON bytes.

        Fixture and artifact readers call this trust boundary. The algorithm
        rejects duplicate keys, distinguishes only two exact top-level shapes,
        rejects unknown/mixed fields, parses frozen records, and validates all
        cross-references. It performs no writes and wraps raw decode/type errors
        in typed graph validation failures.
        """
        value = _strict_json_loads(
            payload,
            error_type=SourceGraphValidationError,
            label="source graph",
        )
        graph = _parse_source_graph(value, compact_fixture=compact_fixture)
        graph.validate()
        return graph

    def get_relation(self, relation_id: str) -> SourceGraphRelation:
        """Return one exact relation ID or raise a typed reference failure.

        Audit and T09 traversal code call this after loading a graph. The method
        validates the aggregate, performs an exact immutable scan, has no side
        effects, and returns no fuzzy substitute. Unknown IDs raise
        ``SourceGraphReferenceError`` without exposing unrelated edges.
        """
        _require_text(relation_id, "relation_id")
        self.validate()
        relation = next(
            (item for item in self.relations if item.relation_id == relation_id),
            None,
        )
        if relation is None:
            raise SourceGraphReferenceError(f"Unknown relation: {relation_id}")
        return relation

    def outgoing(
        self, source_id: str, *, gold_only: bool = True
    ) -> tuple[SourceGraphRelation, ...]:
        """Return deterministic explicit outgoing edges for one canonical ID.

        T09 and audit callers use this bounded traversal instead of a graph
        database. The method validates the graph, filters exact ``source_id``
        matches, and by default excludes unsafe/non-gold relations. It preserves
        persisted deterministic order, infers/ranks nothing, performs no I/O, and
        raises typed validation failures for malformed graph data.
        """
        _require_text(source_id, "source_id")
        self.validate()
        return tuple(
            item
            for item in self.relations
            if item.source_id == source_id
            and (not gold_only or _relation_is_gold(item))
        )

    def incoming(
        self, target_id: str, *, gold_only: bool = True
    ) -> tuple[SourceGraphRelation, ...]:
        """Return deterministic explicit incoming edges for one canonical ID.

        T09 and audit callers use this read-only inverse scan. It validates the
        graph, matches only accepted concrete targets, applies the same default
        gold safety filter as ``outgoing``, and preserves relation order. It never
        promotes candidate targets, performs no I/O, and raises typed failures for
        invalid aggregate data.
        """
        _require_text(target_id, "target_id")
        self.validate()
        return tuple(
            item
            for item in self.relations
            if item.target_id == target_id
            and (not gold_only or _relation_is_gold(item))
        )

    def target_resource_id(self, target: ProvenanceTarget) -> str | None:
        """Resolve a canonical target to its owning resource without payload access.

        The address resolver calls this after target shape validation. The
        algorithm follows node ownership, division membership, or an acyclic
        representation subject chain using an explicit visited set and local
        records only. It returns ``None`` for an unknown target, is deterministic,
        idempotent/read-only, and never opens source or parser bytes. Malformed
        in-memory cycles raise typed graph validation errors, never
        ``RecursionError``; frozen graph state is safe for concurrent readers.
        """
        target.validate()
        self.validate()
        indexes = _validate_graph_records(self)
        if target.node_id is not None:
            return indexes.node_resources.get(target.node_id)
        if target.division_id is not None:
            division = indexes.divisions.get(target.division_id)
            return division.resource_id if division is not None else None
        return _graph_subject_resource_id(target.representation_id or "", indexes)


@dataclass(frozen=True, slots=True)
class SourceGraphBuilder:
    """Project validated canonical artifacts into one complete production graph.

    Responsibility:
        Reuse canonical IDs and explicit facts, attach only caller-supplied
        family/version metadata, and compute a deterministic content revision.
    Constructed by:
        ``IngestService`` or parser-neutral application composition.
    Used by:
        Production persistence, strong-address generation, and T09 preparation.
    Main algorithm:
        Validate inputs; collect/sort records; map canonical relations without new
        semantics; hash canonical JSON facts excluding the revision field.
    Invariants:
        No source is reopened, no parser/LLM/network runs, no IDs or business
        versions are guessed, and canonical text is never copied.
    Lifecycle and persistence:
        Stateless frozen service returns a new immutable revision per fact set.
    Side effects and typed failures:
        No I/O; invalid/conflicting input raises typed graph failures.
    Trust boundary and thread-safety assumptions:
        Canonical artifacts are revalidated; stateless operation is thread-safe.
    """

    def build(
        self,
        artifacts: Sequence[CanonicalContentArtifact],
        *,
        resource_versions: Sequence[ResourceVersionMetadata] = (),
    ) -> SourceGraph:
        """Build one content-bound graph from existing canonical facts only.

        Ingest composition supplies one or more already-built canonical artifacts
        and optional authoritative family/version metadata. The method validates
        every artifact, rejects duplicate/conflicting canonical IDs, projects all
        supported records, sorts by stable IDs, computes ``sg-<sha256>`` over the
        production form without its revision, validates the result, and returns
        it. It performs no I/O and equivalent unordered inputs are idempotent.

        Raises:
            SourceGraphValidationError: Input is empty, malformed, or conflicting.
            SourceGraphReferenceError: Explicit metadata or relations do not map.
        """
        if not artifacts:
            raise SourceGraphValidationError(
                "Source Graph construction requires canonical artifacts"
            )
        for artifact in artifacts:
            artifact.validate()
        metadata = _resource_version_map(resource_versions)
        resources = tuple(
            sorted(
                (
                    SourceGraphResource(
                        resource_id=item.resource_id,
                        source_sha256=item.source_sha256,
                        family_id=(
                            metadata[item.resource_id].family_id
                            if item.resource_id in metadata
                            else None
                        ),
                        version=(
                            metadata[item.resource_id].version
                            if item.resource_id in metadata
                            else None
                        ),
                    )
                    for artifact in artifacts
                    for item in artifact.resources
                ),
                key=lambda item: item.resource_id,
            )
        )
        _reject_duplicate_attribute(resources, "resource_id", "resource IDs")
        unknown_metadata = tuple(sorted(set(metadata) - {item.resource_id for item in resources}))
        if unknown_metadata:
            raise SourceGraphReferenceError(
                f"Business version metadata resource is missing: {unknown_metadata[0]}"
            )
        presentations = tuple(
            sorted(
                (
                    SourceGraphPresentationUnit(
                        item.presentation_unit_id,
                        item.resource_id,
                        item.unit_type,
                    )
                    for artifact in artifacts
                    for item in artifact.presentation_units
                ),
                key=lambda item: item.presentation_unit_id,
            )
        )
        divisions = tuple(
            sorted(
                (
                    SourceGraphDivision(
                        division_id=item.division_id,
                        resource_id=item.resource_id,
                        division_role=item.division_role,
                        number=item.number,
                        parent_division_id=item.parent_division_id,
                        child_division_ids=tuple(sorted(item.child_division_ids)),
                        direct_node_ids=tuple(item.direct_node_ids),
                    )
                    for artifact in artifacts
                    for item in artifact.divisions
                ),
                key=lambda item: item.division_id,
            )
        )
        selectors = _project_graph_selectors(artifacts)
        nodes = tuple(
            sorted(
                (
                    SourceGraphContentNode(
                        node_id=item.node_id,
                        resource_id=item.resource_id,
                        owner_division_id=item.owner_division_id,
                        node_kind=item.node_kind,
                        selector_ids=tuple(
                            selector.selector_id for selector in item.source_selectors
                        ),
                    )
                    for artifact in artifacts
                    for item in artifact.content_nodes
                ),
                key=lambda item: item.node_id,
            )
        )
        representations = tuple(
            sorted(
                (
                    SourceGraphRepresentation(
                        representation_id=item.representation_id,
                        subject_id=item.subject_id,
                        representation_type=item.representation_type,
                        artifact_id=item.artifact_id,
                        selector_ids=tuple(
                            sorted(
                                {
                                    *item.selector_ids,
                                    *(selector.selector_id for selector in item.source_selectors),
                                }
                            )
                        ),
                        caption_node_id=item.caption_node_id,
                    )
                    for artifact in artifacts
                    for item in artifact.representations
                ),
                key=lambda item: item.representation_id,
            )
        )
        bindings = tuple(
            sorted(
                (
                    SourceGraphNativeBinding(
                        item.binding_id,
                        item.canonical_id,
                        item.artifact_id,
                        item.native_pointer,
                        item.binding_role,
                    )
                    for artifact in artifacts
                    for item in artifact.native_bindings
                ),
                key=lambda item: item.binding_id,
            )
        )
        activities = tuple(
            sorted(
                (
                    SourceGraphProcessingActivity(
                        item.activity_id,
                        item.activity_type,
                        item.run_id,
                        item.correlation_id,
                        item.method,
                        tuple(sorted(item.parser_ids)),
                        tuple(sorted(item.input_artifact_ids)),
                        tuple(sorted(item.output_artifact_ids)),
                    )
                    for artifact in artifacts
                    for item in artifact.processing_activities
                ),
                key=lambda item: item.activity_id,
            )
        )
        descriptors = tuple(
            sorted(
                (
                    SourceGraphArtifactDescriptor(
                        item.artifact_id,
                        item.role,
                        item.uri,
                        item.media_type,
                        item.sha256,
                        item.schema_version,
                    )
                    for artifact in artifacts
                    for item in artifact.artifact_descriptors
                ),
                key=lambda item: item.artifact_id,
            )
        )
        relations = tuple(
            sorted(
                (
                    _project_relation(item)
                    for artifact in artifacts
                    for item in artifact.relations
                ),
                key=lambda item: item.relation_id,
            )
        )
        graph = SourceGraph(
            schema=SOURCE_GRAPH_SCHEMA,
            graph_revision="pending",
            resources=resources,
            presentation_units=presentations,
            divisions=divisions,
            relations=relations,
            content_nodes=nodes,
            selectors=selectors,
            representations=representations,
            native_bindings=bindings,
            processing_activities=activities,
            artifact_descriptors=descriptors,
        )
        graph.validate()
        revision = "sg-" + hashlib.sha256(
            _json_bytes(graph.to_dict(include_revision=False)).rstrip(b"\n")
        ).hexdigest()
        completed = replace(graph, graph_revision=revision)
        completed.validate()
        return completed


@dataclass(frozen=True, slots=True, init=False)
class SourceGraphRepository:
    """Retain immutable graph revisions in one explicit application composition.

    Responsibility:
        Provide typed revision lookup and conflict detection without a database,
        network, filesystem-global registry, or hidden parser execution.
    Constructed by:
        ``from_graphs``, ``from_canonical_artifacts``, or the frozen fixture adapter.
    Used by:
        Resolver composition, Ingest tests, audit tools, and future T09.
    Main algorithm:
        Validate graphs, compare bytes for duplicate revisions, and freeze a
        deterministic tuple sorted by revision.
    Invariants:
        One revision maps to one byte-identical graph and revisions never mutate.
    Lifecycle and persistence:
        In-memory repository lifetime is caller-owned; fixture reads occur once.
    Side effects and typed failures:
        Only ``from_fixture`` reads two files; all failures are typed and bounded.
    Trust boundary and thread-safety assumptions:
        Fixture JSON is untrusted; frozen tuple lookup is safe for concurrent use.
    """

    _graphs: tuple[SourceGraph, ...]

    def __init__(self, graphs: Sequence[SourceGraph]) -> None:
        """Validate and freeze revisions while rejecting conflicting duplicates.

        Class constructors delegate here with trusted or parsed graph objects. The
        algorithm validates each graph, compares canonical bytes for repeated
        revision labels, attaches no mutable index, and stores sorted unique
        revisions. It performs no I/O, is deterministic, and raises
        ``SourceGraphRevisionError`` on content conflict.
        """
        by_revision: dict[str, SourceGraph] = {}
        for graph in graphs:
            graph.validate()
            existing = by_revision.get(graph.graph_revision)
            if existing is not None and existing.to_json_bytes() != graph.to_json_bytes():
                raise SourceGraphRevisionError(
                    f"Conflicting Source Graph revision: {graph.graph_revision}"
                )
            by_revision[graph.graph_revision] = graph
        object.__setattr__(
            self,
            "_graphs",
            tuple(by_revision[key] for key in sorted(by_revision)),
        )

    @classmethod
    def from_fixture(cls, fixture_root: Path) -> SourceGraphRepository:
        """Load the two frozen T08 artifacts through strict compact adapters.

        Focused tests call this with the authoritative fixture root. The method
        reads only ``expected/source_graph.json`` and
        ``expected/provenance_addresses.json``, rejects duplicate keys/unknown
        fields, attaches the validated catalog to the graph, and returns one
        immutable repository. It never opens fixture sources, executes parsers,
        or uses network/LLM services. File/decode failures become typed errors
        without embedding the local path.
        """
        try:
            graph_payload = (fixture_root / "expected" / "source_graph.json").read_bytes()
            address_payload = (
                fixture_root / "expected" / "provenance_addresses.json"
            ).read_bytes()
        except OSError as error:
            raise SourceGraphError("Frozen T08 fixture artifacts are unavailable") from error
        graph = SourceGraph.from_json_bytes(graph_payload, compact_fixture=True)
        catalog = ProvenanceAddressCatalog.from_json_bytes(
            address_payload, compact_fixture=True
        )
        return cls((replace(graph, address_catalog=catalog),))

    @classmethod
    def from_graphs(cls, graphs: Sequence[SourceGraph]) -> SourceGraphRepository:
        """Create a production repository from already-built immutable graphs.

        Application composition calls this when persistence is managed elsewhere.
        The method delegates validation/conflict checks to the constructor, does
        no I/O, copies no payload, and is idempotent for equivalent graph inputs.
        Typed graph/revision failures protect the composition boundary.
        """
        return cls(graphs)

    @classmethod
    def from_canonical_artifacts(
        cls,
        artifacts: Sequence[CanonicalContentArtifact],
        *,
        resource_versions: Sequence[ResourceVersionMetadata] = (),
        address_catalog: ProvenanceAddressCatalog | None = None,
    ) -> SourceGraphRepository:
        """Build and retain one production graph from validated canonical records.

        Ingest/application composition calls this convenience seam when it already
        owns canonical aggregates. It runs the pure ``SourceGraphBuilder``,
        optionally attaches an explicit catalog, and returns an immutable
        repository. It performs no source/parser/network I/O, is deterministic,
        and raises typed build/catalog errors before returning.
        """
        graph = SourceGraphBuilder().build(
            artifacts, resource_versions=resource_versions
        )
        if address_catalog is not None:
            address_catalog.validate()
            graph = replace(graph, address_catalog=address_catalog)
        return cls((graph,))

    def load(self, graph_revision: str) -> SourceGraph:
        """Return one immutable revision or raise a typed missing-revision error.

        Resolver and audit callers use exact revision identity. The method scans
        the frozen tuple, returns the existing graph without copying or mutation,
        performs no I/O, and is idempotent. It never falls back to latest or a
        lexical neighbor because that would corrupt strong-address semantics.
        """
        _require_text(graph_revision, "graph_revision")
        graph = next(
            (item for item in self._graphs if item.graph_revision == graph_revision),
            None,
        )
        if graph is None:
            raise SourceGraphRevisionError(
                f"Unknown Source Graph revision: {graph_revision}"
            )
        return graph


class ProvenanceAccessPolicy(Protocol):
    """Define the narrow authorization decision needed during address resolution.

    Responsibility:
        Decide whether one known address/resource may expose target metadata.
    Constructed by:
        Application composition, commonly as an adapter over an existing control
        service rather than a new authorization framework.
    Used by:
        ``ProvenanceAddressResolver`` before target details are returned.
    Main algorithm:
        Return a deterministic boolean for bounded IDs only.
    Invariants:
        Denial must not depend on or return protected source text.
    Lifecycle and persistence:
        Policy lifetime is external; resolver stores only the callable reference.
    Side effects and typed failures:
        Implementations define side effects; exceptions are wrapped fail-closed.
    Trust boundary and thread-safety assumptions:
        Shared resolvers require thread-safe policy implementations.
    """

    def allows(self, address_id: str, resource_id: str) -> bool:
        """Return whether target metadata may be disclosed for the known resource."""
        ...


class ProvenanceVersionPolicy(Protocol):
    """Define deterministic selection among explicit logical-address candidates.

    Responsibility:
        Select zero or one known resource ID without lexical or temporal guessing.
    Constructed by:
        Business-aware application composition.
    Used by:
        ``ProvenanceAddressResolver`` after family/division matching.
    Main algorithm:
        Inspect explicit resource records and return a member ID or ``None``.
    Invariants:
        A returned ID must be one supplied candidate; policy invents no version.
    Lifecycle and persistence:
        External immutable policy is not serialized into addresses.
    Side effects and typed failures:
        Implementations should be pure; invalid output is a resolution error.
    Trust boundary and thread-safety assumptions:
        Shared resolvers require thread-safe policy implementations.
    """

    def select(
        self,
        address: LogicalProvenanceAddress,
        candidates: tuple[SourceGraphResource, ...],
    ) -> str | None:
        """Return one candidate resource ID or ``None`` when no choice is proven."""
        ...


@dataclass(frozen=True, slots=True)
class AllowAllProvenanceAccess:
    """Permit local resolution when no enterprise authorization adapter is present.

    Responsibility:
        Preserve the frozen fixture and local artifact-reader behavior while
        allowing production composition to inject a stricter existing control.
    Constructed by:
        Resolver default composition.
    Used by:
        Local tests and applications whose graph is already access-scoped.
    Main algorithm:
        Return ``True`` for bounded IDs without inspecting target detail.
    Invariants:
        It grants only within the graph already supplied to the resolver.
    Lifecycle and persistence:
        Stateless policy is not persisted.
    Side effects and typed failures:
        None; method is pure and idempotent.
    Trust boundary and thread-safety assumptions:
        Use only after graph scoping; stateless instance is thread-safe.
    """

    def allows(self, address_id: str, resource_id: str) -> bool:
        """Permit the already-scoped graph resource without external I/O."""
        _require_address_text(address_id, "address_id")
        _require_address_text(resource_id, "resource_id")
        return True


@dataclass(frozen=True, slots=True)
class UniqueCandidateVersionPolicy:
    """Select a logical version only when explicit metadata leaves one candidate.

    Responsibility:
        Provide a deterministic fail-closed default without guessing version order.
    Constructed by:
        Resolver default composition.
    Used by:
        Logical resolution where graph family/division matching is sufficient.
    Main algorithm:
        Return the sole candidate resource ID; otherwise return ``None``.
    Invariants:
        Never compares version strings or selects among multiple candidates.
    Lifecycle and persistence:
        Stateless policy is not serialized.
    Side effects and typed failures:
        None; method is pure and idempotent.
    Trust boundary and thread-safety assumptions:
        Candidate facts were graph-validated; stateless instance is thread-safe.
    """

    def select(
        self,
        address: LogicalProvenanceAddress,
        candidates: tuple[SourceGraphResource, ...],
    ) -> str | None:
        """Return the only candidate, leaving zero/multiple choices unresolved."""
        address.validate()
        return candidates[0].resource_id if len(candidates) == 1 else None


@dataclass(frozen=True, slots=True)
class ProvenanceAddressResolver:
    """Resolve strong, logical, and evidence-set addresses conservatively.

    Responsibility:
        Produce exactly six validated outcomes from one explicit graph/catalog and
        narrow access/version policies, with no invented evidence or target leak.
    Constructed by:
        Fixture tests, Ingest/audit application composition, and future T09/T10.
    Used by:
        Exact evidence readers and DataForge support validation.
    Main algorithm:
        Validate graph/catalog; dispatch by address family; prove strong bindings;
        select logical candidates only by policy; resolve every evidence member.
    Invariants:
        Strong addresses never redirect, forbidden results expose no targets, and
        incomplete evidence sets never claim exact support.
    Lifecycle and persistence:
        Frozen resolver holds references only and persists no resolution result.
    Side effects and typed failures:
        Normal absence returns status; invalid policy behavior raises typed error.
    Trust boundary and thread-safety assumptions:
        Policies are injected trust boundaries and must be safe when shared.
    """

    graph: SourceGraph
    catalog: ProvenanceAddressCatalog | None = None
    access_policy: ProvenanceAccessPolicy = field(
        default_factory=AllowAllProvenanceAccess
    )
    version_policy: ProvenanceVersionPolicy = field(
        default_factory=UniqueCandidateVersionPolicy
    )
    obsolete_address_ids: frozenset[str] = frozenset()
    obsolete_resource_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Validate resolver composition before any address request is served.

        Direct construction calls this automatically. It validates graph and
        selected/attached catalog, checks obsolete ID sets, and mutates no input.
        The operation is deterministic and raises typed graph/address failures;
        policy callbacks are deliberately not invoked during construction.
        """
        self.graph.validate()
        selected_catalog = self.catalog or self.graph.address_catalog
        if selected_catalog is not None:
            selected_catalog.validate()
        _require_unique(tuple(self.obsolete_address_ids), "obsolete address IDs")
        _require_unique(tuple(self.obsolete_resource_ids), "obsolete resource IDs")

    def resolve(self, address_id: str) -> AddressResolution:
        """Resolve one catalog ID into a validated deterministic outcome.

        Audit/DataForge callers supply an opaque address ID. The method uses only
        its explicit graph, catalog, and policies; it never reads files, source
        text, parser payloads, network, models, embeddings, or providers. Missing
        IDs return ``unresolved``. Valid records dispatch to family-specific pure
        algorithms. Returned results are validated; invalid policy output raises
        ``ProvenanceResolutionError`` and no partial target is exposed.
        """
        _require_address_text(address_id, "address_id")
        catalog = self.catalog or self.graph.address_catalog
        if catalog is None:
            return _resolution(
                address_id,
                "unresolved",
                reason="No address catalog is attached",
                graph_revision=self.graph.graph_revision,
            )
        address = catalog.find(address_id)
        if address is None:
            return _resolution(
                address_id,
                "unresolved",
                reason="Address is not present",
                graph_revision=self.graph.graph_revision,
            )
        if isinstance(address, StrongProvenanceAddress):
            return self._resolve_strong(address)
        if isinstance(address, LogicalProvenanceAddress):
            return self._resolve_logical(address)
        return self._resolve_evidence_set(address, catalog)

    def _resolve_strong(self, address: StrongProvenanceAddress) -> AddressResolution:
        """Prove exact immutable graph/hash/resource/target/selector bindings.

        ``resolve`` and evidence-set resolution call this internal algorithm. It
        checks explicit obsolescence, authorization before target disclosure,
        graph revision, resource hash, target ownership, and production selector
        consistency. It performs no I/O and returns exact/obsolete/forbidden/
        unresolved only; a strong address is never redirected.
        """
        address.validate()
        if (
            address.address_id in self.obsolete_address_ids
            or address.resource_id in self.obsolete_resource_ids
        ):
            return _resolution(
                address.address_id,
                "obsolete",
                reason="Address is explicitly superseded",
                graph_revision=self.graph.graph_revision,
            )
        if not self._allows(address.address_id, address.resource_id):
            return _resolution(
                address.address_id,
                "forbidden",
                reason="Access policy denied this address",
                graph_revision=self.graph.graph_revision,
            )
        if address.graph_revision != self.graph.graph_revision:
            return _resolution(
                address.address_id,
                "obsolete",
                reason="Address graph revision does not match resolution context",
                graph_revision=self.graph.graph_revision,
            )
        resource = next(
            (
                item
                for item in self.graph.resources
                if item.resource_id == address.resource_id
            ),
            None,
        )
        if resource is None:
            return _resolution(
                address.address_id,
                "unresolved",
                reason="Address resource is unavailable",
                graph_revision=self.graph.graph_revision,
            )
        if resource.source_sha256 != address.source_sha256:
            return _resolution(
                address.address_id,
                "obsolete",
                reason="Address source hash does not match resolution context",
                graph_revision=self.graph.graph_revision,
            )
        target_resource = self.graph.target_resource_id(address.canonical_target)
        if target_resource is None or target_resource != address.resource_id:
            return _resolution(
                address.address_id,
                "unresolved",
                reason="Address target is unavailable for the resource",
                graph_revision=self.graph.graph_revision,
            )
        if not _address_selectors_match_graph(address, self.graph):
            return _resolution(
                address.address_id,
                "unresolved",
                reason="Address selector metadata does not match the graph",
                graph_revision=self.graph.graph_revision,
            )
        return _resolution(
            address.address_id,
            "exact",
            target=address.canonical_target,
            graph_revision=self.graph.graph_revision,
        )

    def _resolve_logical(self, address: LogicalProvenanceAddress) -> AddressResolution:
        """Select a business-family division using explicit metadata and policy.

        ``resolve`` calls this for logical addresses. It matches only resources
        carrying explicit family/version metadata and divisions with the exact
        numbered reference. Zero candidates are unresolved, multiple unselected
        candidates are ambiguous, explicit obsolete selections are obsolete, and
        denied selections are forbidden without target leakage. No lexical/latest
        inference or I/O occurs.
        """
        address.validate()
        resources = tuple(
            item
            for item in self.graph.resources
            if item.family_id == address.resource_family_id
            and item.version is not None
            and any(
                division.resource_id == item.resource_id
                and division.number == address.division_reference
                for division in self.graph.divisions
            )
        )
        if not resources:
            return _resolution(
                address.address_id,
                "unresolved",
                reason="No explicit family/version candidate exists",
                graph_revision=self.graph.graph_revision,
            )
        selected = self._select_version(address, resources)
        if selected is None:
            candidates = tuple(
                ProvenanceTarget(
                    division_id=next(
                        division.division_id
                        for division in self.graph.divisions
                        if division.resource_id == resource.resource_id
                        and division.number == address.division_reference
                    )
                )
                for resource in resources
                if self._allows(address.address_id, resource.resource_id)
            )
            if not candidates:
                return _resolution(
                    address.address_id,
                    "forbidden",
                    reason="Access policy denied all logical candidates",
                    graph_revision=self.graph.graph_revision,
                )
            if len(resources) > 1:
                return _resolution(
                    address.address_id,
                    "ambiguous",
                    candidate_targets=candidates,
                    reason="Version policy did not prove one candidate",
                    graph_revision=self.graph.graph_revision,
                )
            return _resolution(
                address.address_id,
                "unresolved",
                reason="Version policy did not accept the sole candidate",
                graph_revision=self.graph.graph_revision,
            )
        if selected in self.obsolete_resource_ids or address.address_id in self.obsolete_address_ids:
            return _resolution(
                address.address_id,
                "obsolete",
                reason="Selected logical resource is explicitly superseded",
                graph_revision=self.graph.graph_revision,
            )
        if not self._allows(address.address_id, selected):
            return _resolution(
                address.address_id,
                "forbidden",
                reason="Access policy denied the selected logical resource",
                graph_revision=self.graph.graph_revision,
            )
        division = next(
            item
            for item in self.graph.divisions
            if item.resource_id == selected
            and item.number == address.division_reference
        )
        return _resolution(
            address.address_id,
            "redirected",
            target=ProvenanceTarget(division_id=division.division_id),
            reason="Logical address selected one explicit resource version",
            graph_revision=self.graph.graph_revision,
        )

    def _resolve_evidence_set(
        self,
        address: EvidenceSetAddress,
        catalog: ProvenanceAddressCatalog,
    ) -> AddressResolution:
        """Resolve every ordered strong member and aggregate without substitution.

        ``resolve`` calls this for explicit claim closure. Members are resolved in
        persisted order. All exact members produce exact ordered targets. Any
        forbidden member makes the whole result forbidden and strips all target
        detail. Otherwise ambiguity, obsolescence, or absence prevents exact
        closure using a deterministic conservative precedence. No missing member
        is replaced and no source/parser I/O occurs.
        """
        address.validate()
        strong_by_id = {item.address_id: item for item in catalog.strong_addresses}
        members = tuple(
            self._resolve_strong(strong_by_id[item])
            if item in strong_by_id
            else _resolution(
                item,
                "unresolved",
                reason="Evidence member is unavailable",
                graph_revision=self.graph.graph_revision,
            )
            for item in address.member_address_ids
        )
        statuses = {item.status for item in members}
        if "forbidden" in statuses:
            return _resolution(
                address.address_id,
                "forbidden",
                reason="A required evidence member is forbidden",
                graph_revision=self.graph.graph_revision,
            )
        if statuses == {"exact"}:
            return _resolution(
                address.address_id,
                "exact",
                targets=tuple(
                    item.target for item in members if item.target is not None
                ),
                member_resolutions=members,
                graph_revision=self.graph.graph_revision,
            )
        status = next(
            (
                candidate
                for candidate in ("ambiguous", "obsolete", "unresolved")
                if candidate in statuses
            ),
            "unresolved",
        )
        return _resolution(
            address.address_id,
            status,
            member_resolutions=members,
            reason="Required evidence members did not all resolve exactly",
            graph_revision=self.graph.graph_revision,
        )

    def _allows(self, address_id: str, resource_id: str) -> bool:
        """Invoke injected access policy fail-closed without exposing target data.

        Strong/logical resolution calls this before returning target details. Only
        bounded address/resource IDs cross the policy boundary. Exceptions are
        wrapped as ``ProvenanceResolutionError`` and no partial result is emitted.
        The resolver itself performs no policy side effect or retry.
        """
        try:
            result = self.access_policy.allows(address_id, resource_id)
        except Exception as error:
            raise ProvenanceResolutionError("Access policy evaluation failed") from error
        if not isinstance(result, bool):
            raise ProvenanceResolutionError("Access policy must return a boolean")
        return result

    def _select_version(
        self,
        address: LogicalProvenanceAddress,
        candidates: tuple[SourceGraphResource, ...],
    ) -> str | None:
        """Validate injected version selection against explicit graph candidates.

        Logical resolution calls this with deterministic candidate order. Policy
        exceptions are wrapped. ``None`` means no proven choice; any nonmember ID
        is rejected as attempted invention. The method performs no I/O itself and
        returns a known resource ID only.
        """
        try:
            selected = self.version_policy.select(address, candidates)
        except Exception as error:
            raise ProvenanceResolutionError("Version policy evaluation failed") from error
        if selected is not None and selected not in {
            item.resource_id for item in candidates
        }:
            raise ProvenanceResolutionError(
                "Version policy selected an unknown resource"
            )
        return selected


def build_strong_address_catalog(
    graph: SourceGraph,
    artifacts: Sequence[CanonicalContentArtifact],
) -> ProvenanceAddressCatalog:
    """Generate deterministic strong node addresses from surviving canonical facts.

    ``IngestService`` calls this after building a production graph. The algorithm
    validates graph/artifacts, maps canonical selectors into address selectors,
    and creates one content-bound address ID per node from resource/hash/revision/
    target/selectors. It never invents logical/evidence-set intent, opens a source,
    or reads parser-native bytes. Equivalent unordered artifacts return identical
    sorted addresses. Typed graph/address failures occur before persistence.
    """
    graph.validate()
    nodes = {
        node.node_id: node
        for artifact in artifacts
        for node in artifact.content_nodes
    }
    for artifact in artifacts:
        artifact.validate()
    addresses: list[StrongProvenanceAddress] = []
    resource_hashes = {
        item.resource_id: item.source_sha256 for item in graph.resources
    }
    for node_id in sorted(nodes):
        node = nodes[node_id]
        selectors = tuple(_address_selector_from_canonical(item) for item in node.source_selectors)
        target = ProvenanceTarget(node_id=node.node_id)
        identity = {
            "source_sha256": resource_hashes[node.resource_id],
            "graph_revision": graph.graph_revision,
            "resource_id": node.resource_id,
            "canonical_target": _target_to_dict(target),
            "selectors": [_provenance_selector_to_dict(item) for item in selectors],
        }
        address_id = "addr-strong-" + hashlib.sha256(
            _json_bytes(identity).rstrip(b"\n")
        ).hexdigest()
        addresses.append(
            StrongProvenanceAddress(
                address_id=address_id,
                source_sha256=resource_hashes[node.resource_id],
                graph_revision=graph.graph_revision,
                resource_id=node.resource_id,
                canonical_target=target,
                selectors=selectors,
            )
        )
    catalog = ProvenanceAddressCatalog(
        schema=PROVENANCE_ADDRESS_SCHEMA,
        strong_addresses=tuple(addresses),
        logical_addresses=(),
        evidence_set_addresses=(),
    )
    catalog.validate()
    return catalog


@dataclass(frozen=True, slots=True)
class _GraphIndexes:
    """Hold validation-only immutable ID sets and ownership maps.

    Aggregate validation constructs this private record from one graph and passes
    it among pure helpers. It avoids persistent mutable indexes, contains no text,
    performs no I/O, and is thread-local to each validation call.
    """

    resources: Mapping[str, SourceGraphResource]
    presentations: Mapping[str, SourceGraphPresentationUnit]
    divisions: Mapping[str, SourceGraphDivision]
    node_resources: Mapping[str, str]
    selectors: Mapping[str, SourceGraphSelector]
    representations: Mapping[str, SourceGraphRepresentation]
    artifacts: Mapping[str, SourceGraphArtifactDescriptor]
    canonical_ids: frozenset[str]


def _validate_graph_records(graph: SourceGraph) -> _GraphIndexes:
    """Validate scalar record shapes and build unique local graph indexes.

    ``SourceGraph.validate`` calls this first. It checks IDs, hashes, metadata
    pairs, locator facts, and duplicate namespaces without hierarchy traversal.
    Returned mappings are method-local; no graph mutation or external I/O occurs.
    Typed validation failures identify bounded record IDs only.
    """
    resources = _unique_attribute_index(
        graph.resources, "resource_id", "resource IDs"
    )
    for item in graph.resources:
        _require_text(item.resource_id, "resource_id")
        _require_sha256(item.source_sha256, "source_sha256")
        if (item.family_id is None) != (item.version is None):
            raise SourceGraphValidationError(
                "Resource family_id and version must be supplied together"
            )
        if item.family_id is not None:
            _require_text(item.family_id, "family_id")
            _require_text(item.version or "", "version")
    presentations = _unique_attribute_index(
        graph.presentation_units, "presentation_unit_id", "presentation-unit IDs"
    )
    for item in graph.presentation_units:
        for value, name in (
            (item.presentation_unit_id, "presentation_unit_id"),
            (item.resource_id, "presentation resource_id"),
            (item.unit_type, "unit_type"),
        ):
            _require_text(value, name)
        if item.resource_id not in resources:
            raise SourceGraphReferenceError(
                f"Presentation resource is missing: {item.presentation_unit_id}"
            )
    divisions = _unique_attribute_index(graph.divisions, "division_id", "division IDs")
    owned_nodes: dict[str, str] = {}
    for item in graph.divisions:
        for value, name in (
            (item.division_id, "division_id"),
            (item.resource_id, "division resource_id"),
            (item.division_role, "division_role"),
        ):
            _require_text(value, name)
        if item.number is not None:
            _require_text(item.number, "division number")
        if item.parent_division_id is not None:
            _require_text(item.parent_division_id, "parent_division_id")
        _require_unique(item.child_division_ids, "division child IDs")
        _require_unique(item.direct_node_ids, "division direct node IDs")
        if item.resource_id not in resources:
            raise SourceGraphReferenceError(
                f"Division resource is missing: {item.division_id}"
            )
        for node_id in item.direct_node_ids:
            _require_text(node_id, "direct node ID")
            if node_id in owned_nodes:
                raise SourceGraphValidationError(
                    f"Canonical node has multiple direct owners: {node_id}"
                )
            owned_nodes[node_id] = item.resource_id
    production_nodes = _unique_attribute_index(
        graph.content_nodes, "node_id", "production node IDs"
    )
    if production_nodes and set(production_nodes) != set(owned_nodes):
        raise SourceGraphValidationError(
            "Production node records and division ownership must contain identical IDs"
        )
    for item in graph.content_nodes:
        for value, name in (
            (item.node_id, "node_id"),
            (item.resource_id, "node resource_id"),
            (item.owner_division_id, "owner_division_id"),
            (item.node_kind, "node_kind"),
        ):
            _require_text(value, name)
        _require_unique(item.selector_ids, "node selector IDs")
    selectors = _unique_attribute_index(
        graph.selectors, "selector_id", "selector IDs"
    )
    for item in graph.selectors:
        _validate_graph_selector(item)
    representations = _unique_attribute_index(
        graph.representations, "representation_id", "representation IDs"
    )
    for item in graph.representations:
        for value, name in (
            (item.representation_id, "representation_id"),
            (item.subject_id, "representation subject_id"),
            (item.representation_type, "representation_type"),
        ):
            _require_text(value, name)
        _require_unique(item.selector_ids, "representation selector IDs")
    _unique_attribute_index(graph.native_bindings, "binding_id", "binding IDs")
    _unique_attribute_index(
        graph.processing_activities, "activity_id", "processing activity IDs"
    )
    artifacts = _unique_attribute_index(
        graph.artifact_descriptors, "artifact_id", "artifact IDs"
    )
    for item in graph.artifact_descriptors:
        for value, name in (
            (item.artifact_id, "artifact_id"),
            (item.role, "artifact role"),
            (item.uri, "artifact uri"),
            (item.media_type, "artifact media_type"),
        ):
            _require_text(value, name)
        if item.sha256 is not None:
            _require_sha256(item.sha256, "artifact sha256")
    canonical_ids = frozenset(
        {
            *resources,
            *presentations,
            *divisions,
            *owned_nodes,
            *selectors,
            *representations,
            *artifacts,
            *(item.binding_id for item in graph.native_bindings),
            *(item.activity_id for item in graph.processing_activities),
        }
    )
    return _GraphIndexes(
        resources,
        presentations,
        divisions,
        owned_nodes,
        selectors,
        representations,
        artifacts,
        canonical_ids,
    )


def _validate_graph_hierarchy(graph: SourceGraph, indexes: _GraphIndexes) -> None:
    """Prove reciprocal same-resource division links and acyclic hierarchy.

    Aggregate validation calls this after unique indexes exist. It checks every
    parent/child direction, rejects cross-resource hierarchy, and runs a color-set
    depth-first walk over child IDs. It mutates only local sets, performs no I/O,
    and raises typed reference/validation failures before traversal is usable.
    """
    for division in graph.divisions:
        if division.parent_division_id is not None:
            parent = indexes.divisions.get(division.parent_division_id)
            if parent is None:
                raise SourceGraphReferenceError(
                    f"Division parent is missing: {division.division_id}"
                )
            if division.division_id not in parent.child_division_ids:
                raise SourceGraphValidationError(
                    f"Division hierarchy is not reciprocal: {division.division_id}"
                )
            if parent.resource_id != division.resource_id:
                raise SourceGraphValidationError(
                    f"Division hierarchy crosses resources: {division.division_id}"
                )
        for child_id in division.child_division_ids:
            child = indexes.divisions.get(child_id)
            if child is None:
                raise SourceGraphReferenceError(
                    f"Division child is missing: {child_id}"
                )
            if child.parent_division_id != division.division_id:
                raise SourceGraphValidationError(
                    f"Division hierarchy is not reciprocal: {child_id}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(division_id: str) -> None:
        """Walk one hierarchy branch and reject a back-edge as a cycle."""
        if division_id in visiting:
            raise SourceGraphValidationError(
                f"Division hierarchy contains a cycle: {division_id}"
            )
        if division_id in visited:
            return
        visiting.add(division_id)
        for child_id in indexes.divisions[division_id].child_division_ids:
            visit(child_id)
        visiting.remove(division_id)
        visited.add(division_id)

    for division_id in indexes.divisions:
        visit(division_id)


def _validate_graph_production_facts(
    graph: SourceGraph, indexes: _GraphIndexes
) -> None:
    """Validate complete production node, selector, representation, and lineage refs.

    ``SourceGraph.validate`` calls this for both forms; compact graphs naturally
    have empty production tuples. It proves node/resource/owner/selector agreement,
    representation support and acyclic subject lineage, native bindings,
    activities, and artifact references. The algorithm is deterministic and pure,
    uses bounded iterative subject walks, and does not dereference URIs, native
    pointers, source bytes, or parser payloads. Typed validation/reference failures
    protect every later traversal and address-resolution consumer.
    """
    for selector in graph.selectors:
        if selector.resource_id not in indexes.resources:
            raise SourceGraphReferenceError(
                f"Selector resource is missing: {selector.selector_id}"
            )
        if selector.presentation_unit_id is not None:
            presentation = indexes.presentations.get(selector.presentation_unit_id)
            if presentation is None or presentation.resource_id != selector.resource_id:
                raise SourceGraphReferenceError(
                    f"Selector presentation is missing or inconsistent: {selector.selector_id}"
                )
    for node in graph.content_nodes:
        division = indexes.divisions.get(node.owner_division_id)
        if division is None or node.node_id not in division.direct_node_ids:
            raise SourceGraphReferenceError(
                f"Node owner is missing or inconsistent: {node.node_id}"
            )
        if node.resource_id != division.resource_id:
            raise SourceGraphValidationError(
                f"Node resource differs from owner: {node.node_id}"
            )
        for selector_id in node.selector_ids:
            selector = indexes.selectors.get(selector_id)
            if selector is None or selector.resource_id != node.resource_id:
                raise SourceGraphReferenceError(
                    f"Node selector is missing or inconsistent: {selector_id}"
                )
    subject_ids = {
        *indexes.resources,
        *indexes.presentations,
        *indexes.divisions,
        *indexes.node_resources,
        *indexes.representations,
    }
    _validate_representation_subject_cycles(indexes)
    for item in graph.representations:
        if item.subject_id not in subject_ids:
            raise SourceGraphReferenceError(
                f"Representation subject is missing: {item.representation_id}"
            )
        if item.caption_node_id is not None and item.caption_node_id not in indexes.node_resources:
            raise SourceGraphReferenceError(
                f"Representation caption is missing: {item.representation_id}"
            )
        if item.artifact_id is not None and item.artifact_id not in indexes.artifacts:
            raise SourceGraphReferenceError(
                f"Representation artifact is missing: {item.representation_id}"
            )
        for selector_id in item.selector_ids:
            selector = indexes.selectors.get(selector_id)
            if selector is None:
                raise SourceGraphReferenceError(
                    f"Representation selector is missing: {selector_id}"
                )
            subject_resource = _graph_subject_resource_id(item.subject_id, indexes)
            if subject_resource is not None and selector.resource_id != subject_resource:
                raise SourceGraphValidationError(
                    f"Representation selector resource is inconsistent: {selector_id}"
                )
    for item in graph.native_bindings:
        for value, name in (
            (item.binding_id, "binding_id"),
            (item.canonical_id, "binding canonical_id"),
            (item.artifact_id, "binding artifact_id"),
            (item.native_pointer, "native_pointer"),
            (item.binding_role, "binding_role"),
        ):
            _require_text(value, name)
        if item.canonical_id not in indexes.canonical_ids:
            raise SourceGraphReferenceError(
                f"Native binding canonical target is missing: {item.binding_id}"
            )
        if item.artifact_id not in indexes.artifacts:
            raise SourceGraphReferenceError(
                f"Native binding artifact is missing: {item.binding_id}"
            )
    for item in graph.processing_activities:
        for value, name in (
            (item.activity_id, "activity_id"),
            (item.activity_type, "activity_type"),
            (item.run_id, "activity run_id"),
            (item.correlation_id, "activity correlation_id"),
            (item.method, "activity method"),
        ):
            _require_text(value, name)
        _require_unique(item.parser_ids, "activity parser IDs")
        _require_unique(item.input_artifact_ids, "activity input artifact IDs")
        _require_unique(item.output_artifact_ids, "activity output artifact IDs")
        for artifact_id in (*item.input_artifact_ids, *item.output_artifact_ids):
            if artifact_id not in indexes.artifacts:
                raise SourceGraphReferenceError(
                    f"Processing activity artifact is missing: {artifact_id}"
                )


def _validate_production_ordering(graph: SourceGraph) -> None:
    """Require deterministic ID ordering for the complete persisted graph form.

    Production builders sort independent record collections and child/candidate
    ID sets before hashing. Strict production readers call this through aggregate
    validation so a reordered equivalent JSON document cannot masquerade as the
    canonical persisted form. Direct node order remains canonical source order.
    The helper is pure and raises typed graph validation failures.
    """
    collections = (
        (graph.resources, "resource_id", "resources"),
        (graph.presentation_units, "presentation_unit_id", "presentation units"),
        (graph.divisions, "division_id", "divisions"),
        (graph.relations, "relation_id", "relations"),
        (graph.content_nodes, "node_id", "content nodes"),
        (graph.selectors, "selector_id", "selectors"),
        (graph.representations, "representation_id", "representations"),
        (graph.native_bindings, "binding_id", "native bindings"),
        (graph.processing_activities, "activity_id", "processing activities"),
        (graph.artifact_descriptors, "artifact_id", "artifact descriptors"),
    )
    for values, attribute, label in collections:
        current = tuple(getattr(item, attribute) for item in values)
        if current != tuple(sorted(current)):
            raise SourceGraphValidationError(
                f"Production {label} are not deterministically ordered"
            )
    for division in graph.divisions:
        if division.child_division_ids != tuple(sorted(division.child_division_ids)):
            raise SourceGraphValidationError(
                f"Division children are not deterministically ordered: {division.division_id}"
            )
    for relation in graph.relations:
        if relation.candidate_target_ids != tuple(sorted(relation.candidate_target_ids)):
            raise SourceGraphValidationError(
                f"Relation candidates are not deterministically ordered: {relation.relation_id}"
            )


def _graph_subject_resource_id(
    subject_id: str, indexes: _GraphIndexes
) -> str | None:
    """Resolve a production representation subject to resource metadata only.

    Representation validation and ``SourceGraph.target_resource_id`` call this
    trust-boundary helper after indexing immutable graph records. It follows
    resource, presentation, division, node, or representation references with an
    explicit visited set. The bounded iterative walk performs no I/O or mutation,
    returns ``None`` for an unknown terminal, and raises
    ``SourceGraphValidationError`` for a malformed cycle rather than relying on a
    Python recursion limit. Local indexes make deterministic concurrent reads safe.
    """
    current = subject_id
    visited: set[str] = set()
    while True:
        if current in visited:
            raise SourceGraphValidationError(
                f"Representation subject lineage contains a cycle: {current}"
            )
        visited.add(current)
        if current in indexes.resources:
            return current
        presentation = indexes.presentations.get(current)
        if presentation is not None:
            return presentation.resource_id
        division = indexes.divisions.get(current)
        if division is not None:
            return division.resource_id
        if current in indexes.node_resources:
            return indexes.node_resources[current]
        representation = indexes.representations.get(current)
        if representation is None:
            return None
        current = representation.subject_id


def _validate_representation_subject_cycles(indexes: _GraphIndexes) -> None:
    """Reject every self or multi-record representation subject cycle.

    ``SourceGraph.validate`` reaches this helper before representations can be
    traversed or used by the provenance resolver. For each representation, the
    algorithm follows only representation-to-representation subjects, records a
    local path, and stops at a non-representation terminal or a previously proven
    chain. It performs no I/O or graph mutation, is deterministic for immutable
    indexes, and raises ``SourceGraphValidationError`` with a bounded ID instead
    of allowing ``RecursionError`` to cross the persisted-data trust boundary.
    """
    proven: set[str] = set()
    for representation_id in indexes.representations:
        current = representation_id
        path: set[str] = set()
        while current in indexes.representations:
            if current in path:
                raise SourceGraphValidationError(
                    f"Representation subject lineage contains a cycle: {current}"
                )
            if current in proven:
                break
            path.add(current)
            current = indexes.representations[current].subject_id
        proven.update(path)


def _validate_graph_relations(graph: SourceGraph, indexes: _GraphIndexes) -> None:
    """Validate relation identity, targets, candidates, and gold eligibility.

    Aggregate validation calls this after all canonical IDs are indexed. Concrete
    relations require targets, unsafe states require no accepted gold claim, and
    every source/target/candidate resolves across resource boundaries. Candidate
    order is preserved but duplicates are rejected. No edge semantics are inferred.
    """
    _unique_attribute_index(graph.relations, "relation_id", "relation IDs")
    for item in graph.relations:
        for value, name in (
            (item.relation_id, "relation_id"),
            (item.source_id, "relation source_id"),
            (item.relation_type, "relation_type"),
            (item.status, "relation status"),
            (item.epistemic_state, "relation epistemic_state"),
        ):
            _require_text(value, name)
        if item.source_id not in indexes.canonical_ids:
            raise SourceGraphReferenceError(
                f"Relation source is missing: {item.relation_id}"
            )
        unsafe = (
            item.status in _UNSAFE_GOLD_STATES
            or item.epistemic_state in _UNSAFE_GOLD_STATES
        )
        if item.status in _CONCRETE_STATUSES and item.target_id is None:
            raise SourceGraphValidationError(
                f"Concrete relation requires a target: {item.relation_id}"
            )
        if item.status == "ambiguous" and item.target_id is not None:
            raise SourceGraphValidationError(
                f"Ambiguous relation cannot claim a target: {item.relation_id}"
            )
        if unsafe and item.gold_eligible:
            raise SourceGraphValidationError(
                f"Unsafe relation cannot be gold eligible: {item.relation_id}"
            )
        if item.target_id is not None and item.target_id not in indexes.canonical_ids:
            raise SourceGraphReferenceError(
                f"Relation target is missing: {item.relation_id}"
            )
        _require_unique(item.candidate_target_ids, "relation candidate target IDs")
        if item.target_id is not None and item.target_id in item.candidate_target_ids:
            raise SourceGraphValidationError(
                f"Relation target cannot also be a candidate: {item.relation_id}"
            )
        for target_id in item.candidate_target_ids:
            if target_id not in indexes.canonical_ids:
                raise SourceGraphReferenceError(
                    f"Relation candidate target is missing: {target_id}"
                )


def _validate_graph_selector(item: SourceGraphSelector) -> None:
    """Validate one production selector without opening its logical locator.

    Graph record validation calls this pure helper. It checks ID/type/resource,
    optional presentation/path/range/geometry, anchor uniqueness, and locator
    presence. It never reads a path or source and raises typed graph failures.
    """
    for value, name in (
        (item.selector_id, "selector_id"),
        (item.selector_type, "selector_type"),
        (item.resource_id, "selector resource_id"),
    ):
        _require_text(value, name)
    if item.presentation_unit_id is not None:
        _require_text(item.presentation_unit_id, "selector presentation_unit_id")
    if item.source_path is not None:
        try:
            _validate_logical_path(item.source_path)
        except ProvenanceAddressValidationError as error:
            raise SourceGraphValidationError("Selector source_path is unsafe") from error
    if (item.char_start is None) != (item.char_end is None):
        raise SourceGraphValidationError(
            "Selector character range must supply both bounds"
        )
    if item.char_start is not None:
        _validate_range(
            item.char_start,
            item.char_end,
            error_type=SourceGraphValidationError,
            label="selector character range",
        )
    if item.bbox is not None:
        _validate_bbox(item.bbox, SourceGraphValidationError)
    _require_unique(item.source_anchor_ids, "selector source anchor IDs")
    _require_unique(
        item.parser_source_anchor_ids, "selector parser source anchor IDs"
    )


def _project_graph_selectors(
    artifacts: Sequence[CanonicalContentArtifact],
) -> tuple[SourceGraphSelector, ...]:
    """Deduplicate equivalent canonical selectors and reject ID conflicts.

    ``SourceGraphBuilder`` calls this for selectors embedded in content nodes and
    representations. It projects exact locator facts, compares equal IDs by value,
    rejects conflicts, and returns ID-sorted immutable records. No source path is
    opened and no text is copied.
    """
    projected: dict[str, SourceGraphSelector] = {}
    selectors = (
        selector
        for artifact in artifacts
        for selector in (
            *(selector for node in artifact.content_nodes for selector in node.source_selectors),
            *(selector for representation in artifact.representations for selector in representation.source_selectors),
        )
    )
    for selector in selectors:
        item = _graph_selector_from_canonical(selector)
        existing = projected.get(item.selector_id)
        if existing is not None and existing != item:
            raise SourceGraphValidationError(
                f"Conflicting canonical selector ID: {item.selector_id}"
            )
        projected[item.selector_id] = item
    return tuple(projected[key] for key in sorted(projected))


def _graph_selector_from_canonical(item: SourceSelector) -> SourceGraphSelector:
    """Project one canonical selector into the complete graph without text or I/O."""
    return SourceGraphSelector(
        selector_id=item.selector_id,
        selector_type=item.selector_type,
        resource_id=item.resource_id,
        presentation_unit_id=item.presentation_unit_id,
        source_path=item.source_path,
        char_start=item.char_start,
        char_end=item.char_end,
        bbox=item.bbox,
        source_anchor_ids=item.source_anchor_ids,
        parser_source_anchor_ids=item.parser_source_anchor_ids,
    )


def _address_selector_from_canonical(item: SourceSelector) -> ProvenanceSelector:
    """Project canonical locator facts into a strong address without source access."""
    selector = ProvenanceSelector(
        selector_type=item.selector_type,
        source_path=item.source_path,
        char_start=item.char_start,
        char_end=item.char_end,
        selector_id=item.selector_id,
        presentation_unit_id=item.presentation_unit_id,
        bbox=item.bbox,
        source_anchor_ids=item.source_anchor_ids,
        parser_source_anchor_ids=item.parser_source_anchor_ids,
    )
    selector.validate()
    return selector


def _project_relation(item: object) -> SourceGraphRelation:
    """Map one canonical relation without adding candidates or semantic meaning.

    The builder passes a validated ``CanonicalRelation``. The helper preserves
    exact IDs/type/status/state, marks gold only for concrete safe accepted facts,
    and leaves targetless unsafe relations non-gold. Canonical relations do not
    carry candidate IDs, so none are invented. The pure result is deterministic.
    """
    status = str(getattr(item, "status"))
    epistemic_state = str(getattr(item, "epistemic_state"))
    target_id = getattr(item, "target_id")
    gold = (
        target_id is not None
        and status in _CONCRETE_STATUSES
        and status not in _UNSAFE_GOLD_STATES
        and epistemic_state not in _UNSAFE_GOLD_STATES
    )
    return SourceGraphRelation(
        relation_id=str(getattr(item, "relation_id")),
        source_id=str(getattr(item, "source_id")),
        target_id=str(target_id) if target_id is not None else None,
        relation_type=str(getattr(item, "relation_type")),
        status=status,
        epistemic_state=epistemic_state,
        gold_eligible=gold,
    )


def _resource_version_map(
    records: Sequence[ResourceVersionMetadata],
) -> dict[str, ResourceVersionMetadata]:
    """Validate explicit family/version metadata and reject duplicate resources.

    Graph construction calls this before reading canonical resources. Values are
    bounded and no version interpretation occurs. The returned local mapping is
    deterministic for lookup only, is not persisted, and conflicts fail typed.
    """
    result: dict[str, ResourceVersionMetadata] = {}
    for item in records:
        for value, name in (
            (item.resource_id, "resource version resource_id"),
            (item.family_id, "resource family_id"),
            (item.version, "resource version"),
        ):
            _require_text(value, name)
        if item.resource_id in result:
            raise SourceGraphValidationError(
                f"Duplicate resource version metadata: {item.resource_id}"
            )
        result[item.resource_id] = item
    return result


def _relation_is_gold(item: SourceGraphRelation) -> bool:
    """Apply the closed default traversal safety rule to one explicit relation."""
    return (
        item.gold_eligible
        and item.target_id is not None
        and item.status not in _UNSAFE_GOLD_STATES
        and item.epistemic_state not in _UNSAFE_GOLD_STATES
    )


def _address_selectors_match_graph(
    address: StrongProvenanceAddress, graph: SourceGraph
) -> bool:
    """Cross-check production selector IDs/facts while allowing compact fixtures.

    Strong resolution calls this after target/resource validation. Compact graphs
    intentionally lack selector records, so their structurally validated frozen
    compatibility selectors pass unchanged. In a complete production graph, an
    ID-bearing selector must name the addressed resource and equal its graph
    record; a selector without an ID must equal every represented locator fact of
    at least one selector on that resource after only the absent ID is ignored.
    The deterministic scan performs no source, filesystem, parser, or network I/O,
    mutates nothing, invents no ID, and returns ``False`` on proof failure.
    """
    if graph.compact_fixture:
        return True
    by_id = {item.selector_id: item for item in graph.selectors}
    for selector in address.selectors:
        if selector.selector_id is None:
            if not any(
                replace(
                    _address_selector_from_graph(graph_selector),
                    selector_id=None,
                )
                == selector
                for graph_selector in graph.selectors
                if graph_selector.resource_id == address.resource_id
            ):
                return False
            continue
        graph_selector = by_id.get(selector.selector_id)
        if graph_selector is None or graph_selector.resource_id != address.resource_id:
            return False
        expected = _address_selector_from_graph(graph_selector)
        if expected != selector:
            return False
    return True


def _address_selector_from_graph(item: SourceGraphSelector) -> ProvenanceSelector:
    """Convert a validated graph selector to the exact production address shape."""
    return ProvenanceSelector(
        selector_type=item.selector_type,
        source_path=item.source_path,
        char_start=item.char_start,
        char_end=item.char_end,
        selector_id=item.selector_id,
        presentation_unit_id=item.presentation_unit_id,
        bbox=item.bbox,
        source_anchor_ids=item.source_anchor_ids,
        parser_source_anchor_ids=item.parser_source_anchor_ids,
    )


def _resolution(
    address_id: str,
    status: str,
    *,
    target: ProvenanceTarget | None = None,
    targets: tuple[ProvenanceTarget, ...] = (),
    candidate_targets: tuple[ProvenanceTarget, ...] = (),
    member_resolutions: tuple[AddressResolution, ...] = (),
    reason: str = "",
    graph_revision: str | None = None,
) -> AddressResolution:
    """Construct and validate one resolver response before crossing the API boundary.

    Family-specific algorithms call this common guard. It freezes all supplied
    details, validates status/no-leak invariants, returns the immutable result, and
    performs no I/O. Invalid internal composition fails typed rather than exposing
    a malformed or partially protected response.
    """
    result = AddressResolution(
        address_id=address_id,
        status=status,
        target=target,
        targets=targets,
        candidate_targets=candidate_targets,
        member_resolutions=member_resolutions,
        reason=reason,
        graph_revision=graph_revision,
    )
    result.validate()
    return result


def _graph_resource_to_dict(item: SourceGraphResource) -> dict[str, object]:
    """Serialize one resource, omitting unknown business metadata in production."""
    value: dict[str, object] = {
        "resource_id": item.resource_id,
        "source_sha256": item.source_sha256,
    }
    if item.family_id is not None:
        value["family_id"] = item.family_id
        value["version"] = item.version
    return value


def _graph_presentation_to_dict(
    item: SourceGraphPresentationUnit,
) -> dict[str, object]:
    """Serialize one presentation reference without presentation content."""
    return {
        "presentation_unit_id": item.presentation_unit_id,
        "resource_id": item.resource_id,
        "unit_type": item.unit_type,
    }


def _graph_division_to_dict(item: SourceGraphDivision) -> dict[str, object]:
    """Serialize hierarchy and direct ownership while preserving node order."""
    value: dict[str, object] = {
        "division_id": item.division_id,
        "resource_id": item.resource_id,
        "division_role": item.division_role,
        "parent_division_id": item.parent_division_id,
        "child_division_ids": list(item.child_division_ids),
        "direct_node_ids": list(item.direct_node_ids),
    }
    if item.number is not None:
        value["number"] = item.number
    return value


def _graph_relation_to_dict(item: SourceGraphRelation) -> dict[str, object]:
    """Serialize one relation and include candidates only when explicitly present."""
    value: dict[str, object] = {
        "relation_id": item.relation_id,
        "source_id": item.source_id,
        "target_id": item.target_id,
        "relation_type": item.relation_type,
        "status": item.status,
        "epistemic_state": item.epistemic_state,
        "gold_eligible": item.gold_eligible,
    }
    if item.candidate_target_ids:
        value["candidate_target_ids"] = list(item.candidate_target_ids)
    return value


def _graph_node_to_dict(item: SourceGraphContentNode) -> dict[str, object]:
    """Serialize a production node reference with no canonical text field."""
    return {
        "node_id": item.node_id,
        "resource_id": item.resource_id,
        "owner_division_id": item.owner_division_id,
        "node_kind": item.node_kind,
        "selector_ids": list(item.selector_ids),
    }


def _graph_selector_to_dict(item: SourceGraphSelector) -> dict[str, object]:
    """Serialize all observed production locator facts without opening a path."""
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
        "parser_source_anchor_ids": list(item.parser_source_anchor_ids),
    }


def _graph_representation_to_dict(
    item: SourceGraphRepresentation,
) -> dict[str, object]:
    """Serialize representation lineage without payload or represented text."""
    return {
        "representation_id": item.representation_id,
        "subject_id": item.subject_id,
        "representation_type": item.representation_type,
        "artifact_id": item.artifact_id,
        "selector_ids": list(item.selector_ids),
        "caption_node_id": item.caption_node_id,
    }


def _graph_binding_to_dict(item: SourceGraphNativeBinding) -> dict[str, object]:
    """Serialize native pointer metadata without dereferencing parser bytes."""
    return {
        "binding_id": item.binding_id,
        "canonical_id": item.canonical_id,
        "artifact_id": item.artifact_id,
        "native_pointer": item.native_pointer,
        "binding_role": item.binding_role,
    }


def _graph_activity_to_dict(
    item: SourceGraphProcessingActivity,
) -> dict[str, object]:
    """Serialize compact processing lineage and artifact reference order."""
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


def _graph_artifact_to_dict(
    item: SourceGraphArtifactDescriptor,
) -> dict[str, object]:
    """Serialize an artifact reference, never the independently stored payload."""
    return {
        "artifact_id": item.artifact_id,
        "role": item.role,
        "uri": item.uri,
        "media_type": item.media_type,
        "sha256": item.sha256,
        "schema_version": item.schema_version,
    }


def _target_to_dict(item: ProvenanceTarget) -> dict[str, object]:
    """Serialize exactly one target ID and optional node span."""
    item.validate()
    value: dict[str, object] = {}
    if item.node_id is not None:
        value["node_id"] = item.node_id
    elif item.division_id is not None:
        value["division_id"] = item.division_id
    else:
        value["representation_id"] = item.representation_id
    if item.char_start is not None:
        value["char_start"] = item.char_start
        value["char_end"] = item.char_end
    return value


def _provenance_selector_to_dict(item: ProvenanceSelector) -> dict[str, object]:
    """Serialize present selector facts while preserving compact compatibility."""
    item.validate()
    value: dict[str, object] = {"selector_type": item.selector_type}
    for key, field_value in (
        ("source_path", item.source_path),
        ("char_start", item.char_start),
        ("char_end", item.char_end),
        ("selector_id", item.selector_id),
        ("presentation_unit_id", item.presentation_unit_id),
    ):
        if field_value is not None:
            value[key] = field_value
    if item.bbox is not None:
        value["bbox"] = list(item.bbox)
    if item.source_anchor_ids:
        value["source_anchor_ids"] = list(item.source_anchor_ids)
    if item.parser_source_anchor_ids:
        value["parser_source_anchor_ids"] = list(item.parser_source_anchor_ids)
    return value


def _strong_address_to_dict(
    item: StrongProvenanceAddress, *, compact: bool
) -> dict[str, object]:
    """Serialize one strong address using exact compact or richer selector shape."""
    item.validate()
    selectors = [_provenance_selector_to_dict(value) for value in item.selectors]
    if compact:
        allowed = {"selector_type", "source_path", "char_start", "char_end"}
        if any(set(value) != allowed for value in selectors):
            raise ProvenanceAddressValidationError(
                "Compact strong selectors require exact text-position fields"
            )
    return {
        "address_id": item.address_id,
        "source_sha256": item.source_sha256,
        "graph_revision": item.graph_revision,
        "resource_id": item.resource_id,
        "canonical_target": _target_to_dict(item.canonical_target),
        "selectors": selectors,
    }


def _logical_address_to_dict(item: LogicalProvenanceAddress) -> dict[str, object]:
    """Serialize explicit family/rule/division logical indirection."""
    item.validate()
    return {
        "address_id": item.address_id,
        "resource_family_id": item.resource_family_id,
        "version_rule": item.version_rule,
        "division_reference": item.division_reference,
    }


def _evidence_address_to_dict(item: EvidenceSetAddress) -> dict[str, object]:
    """Serialize evidence claims and member IDs in authoritative order."""
    item.validate()
    return {
        "address_id": item.address_id,
        "claim_ids": list(item.claim_ids),
        "member_address_ids": list(item.member_address_ids),
    }


_COMPACT_GRAPH_FIELDS = {
    "schema",
    "graph_revision",
    "resources",
    "presentation_units",
    "divisions",
    "relations",
}
_PRODUCTION_GRAPH_FIELDS = _COMPACT_GRAPH_FIELDS | {
    "content_nodes",
    "selectors",
    "representations",
    "native_bindings",
    "processing_activities",
    "artifact_descriptors",
}


def _parse_source_graph(value: object, *, compact_fixture: bool) -> SourceGraph:
    """Parse one exact persisted graph shape into immutable records.

    ``SourceGraph.from_json_bytes`` calls this after duplicate-key-safe decoding.
    The exact expected field set is selected explicitly; unknown or partially
    mixed shapes fail before nested parsing. Raw type/key/value errors are wrapped
    into typed graph validation failures without exposing source payloads.
    """
    try:
        mapping = _mapping(value, "source graph", SourceGraphValidationError)
        expected = _COMPACT_GRAPH_FIELDS if compact_fixture else _PRODUCTION_GRAPH_FIELDS
        _exact_fields(mapping, expected, "source graph", SourceGraphValidationError)
        graph = SourceGraph(
            schema=_text_value(mapping["schema"], "schema", SourceGraphValidationError),
            graph_revision=_text_value(
                mapping["graph_revision"], "graph_revision", SourceGraphValidationError
            ),
            resources=tuple(
                _parse_graph_resource(item, compact=compact_fixture)
                for item in _mapping_sequence(
                    mapping["resources"], "resources", SourceGraphValidationError
                )
            ),
            presentation_units=tuple(
                _parse_graph_presentation(item)
                for item in _mapping_sequence(
                    mapping["presentation_units"],
                    "presentation_units",
                    SourceGraphValidationError,
                )
            ),
            divisions=tuple(
                _parse_graph_division(item)
                for item in _mapping_sequence(
                    mapping["divisions"], "divisions", SourceGraphValidationError
                )
            ),
            relations=tuple(
                _parse_graph_relation(item)
                for item in _mapping_sequence(
                    mapping["relations"], "relations", SourceGraphValidationError
                )
            ),
            content_nodes=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_node(item)
                    for item in _mapping_sequence(
                        mapping["content_nodes"],
                        "content_nodes",
                        SourceGraphValidationError,
                    )
                )
            ),
            selectors=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_selector(item)
                    for item in _mapping_sequence(
                        mapping["selectors"], "selectors", SourceGraphValidationError
                    )
                )
            ),
            representations=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_representation(item)
                    for item in _mapping_sequence(
                        mapping["representations"],
                        "representations",
                        SourceGraphValidationError,
                    )
                )
            ),
            native_bindings=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_binding(item)
                    for item in _mapping_sequence(
                        mapping["native_bindings"],
                        "native_bindings",
                        SourceGraphValidationError,
                    )
                )
            ),
            processing_activities=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_activity(item)
                    for item in _mapping_sequence(
                        mapping["processing_activities"],
                        "processing_activities",
                        SourceGraphValidationError,
                    )
                )
            ),
            artifact_descriptors=(
                ()
                if compact_fixture
                else tuple(
                    _parse_graph_artifact(item)
                    for item in _mapping_sequence(
                        mapping["artifact_descriptors"],
                        "artifact_descriptors",
                        SourceGraphValidationError,
                    )
                )
            ),
            compact_fixture=compact_fixture,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, SourceGraphError):
            raise
        raise SourceGraphValidationError(
            "Source Graph contains malformed nested values"
        ) from error
    return graph


def _parse_graph_resource(value: Mapping[str, object], *, compact: bool) -> SourceGraphResource:
    """Parse exact compact or production resource fields without version inference."""
    required = {"resource_id", "source_sha256"}
    mapping = _mapping(value, "resource", SourceGraphValidationError)
    if compact:
        _exact_fields(
            mapping,
            required | {"family_id", "version"},
            "resource",
            SourceGraphValidationError,
        )
    elif set(mapping) not in (required, required | {"family_id", "version"}):
        raise SourceGraphValidationError("Production resource fields are invalid")
    return SourceGraphResource(
        resource_id=_text_value(
            mapping["resource_id"], "resource_id", SourceGraphValidationError
        ),
        source_sha256=_text_value(
            mapping["source_sha256"], "source_sha256", SourceGraphValidationError
        ),
        family_id=(
            _text_value(mapping["family_id"], "family_id", SourceGraphValidationError)
            if "family_id" in mapping
            else None
        ),
        version=(
            _text_value(mapping["version"], "version", SourceGraphValidationError)
            if "version" in mapping
            else None
        ),
    )


def _parse_graph_presentation(value: Mapping[str, object]) -> SourceGraphPresentationUnit:
    """Parse one exact presentation-unit reference."""
    mapping = _mapping(value, "presentation unit", SourceGraphValidationError)
    _exact_fields(
        mapping,
        {"presentation_unit_id", "resource_id", "unit_type"},
        "presentation unit",
        SourceGraphValidationError,
    )
    return SourceGraphPresentationUnit(
        *(
            _text_value(mapping[key], key, SourceGraphValidationError)
            for key in ("presentation_unit_id", "resource_id", "unit_type")
        )
    )


def _parse_graph_division(value: Mapping[str, object]) -> SourceGraphDivision:
    """Parse hierarchy/direct ownership with one optional number field."""
    mapping = _mapping(value, "division", SourceGraphValidationError)
    required = {
        "division_id",
        "resource_id",
        "division_role",
        "parent_division_id",
        "child_division_ids",
        "direct_node_ids",
    }
    if set(mapping) not in (required, required | {"number"}):
        raise SourceGraphValidationError("Division fields are invalid")
    return SourceGraphDivision(
        division_id=_text_value(
            mapping["division_id"], "division_id", SourceGraphValidationError
        ),
        resource_id=_text_value(
            mapping["resource_id"], "resource_id", SourceGraphValidationError
        ),
        division_role=_text_value(
            mapping["division_role"], "division_role", SourceGraphValidationError
        ),
        number=(
            _text_value(mapping["number"], "number", SourceGraphValidationError)
            if "number" in mapping
            else None
        ),
        parent_division_id=_optional_text_value(
            mapping["parent_division_id"],
            "parent_division_id",
            SourceGraphValidationError,
        ),
        child_division_ids=_text_tuple(
            mapping["child_division_ids"],
            "child_division_ids",
            SourceGraphValidationError,
        ),
        direct_node_ids=_text_tuple(
            mapping["direct_node_ids"],
            "direct_node_ids",
            SourceGraphValidationError,
        ),
    )


def _parse_graph_relation(value: Mapping[str, object]) -> SourceGraphRelation:
    """Parse exact relation fields with optional explicit candidate IDs."""
    mapping = _mapping(value, "relation", SourceGraphValidationError)
    required = {
        "relation_id",
        "source_id",
        "target_id",
        "relation_type",
        "status",
        "epistemic_state",
        "gold_eligible",
    }
    if set(mapping) not in (required, required | {"candidate_target_ids"}):
        raise SourceGraphValidationError("Relation fields are invalid")
    gold = mapping["gold_eligible"]
    if not isinstance(gold, bool):
        raise SourceGraphValidationError("Relation gold_eligible must be boolean")
    return SourceGraphRelation(
        relation_id=_text_value(
            mapping["relation_id"], "relation_id", SourceGraphValidationError
        ),
        source_id=_text_value(
            mapping["source_id"], "source_id", SourceGraphValidationError
        ),
        target_id=_optional_text_value(
            mapping["target_id"], "target_id", SourceGraphValidationError
        ),
        relation_type=_text_value(
            mapping["relation_type"], "relation_type", SourceGraphValidationError
        ),
        status=_text_value(mapping["status"], "status", SourceGraphValidationError),
        epistemic_state=_text_value(
            mapping["epistemic_state"], "epistemic_state", SourceGraphValidationError
        ),
        gold_eligible=gold,
        candidate_target_ids=(
            _text_tuple(
                mapping["candidate_target_ids"],
                "candidate_target_ids",
                SourceGraphValidationError,
            )
            if "candidate_target_ids" in mapping
            else ()
        ),
    )


def _parse_graph_node(value: Mapping[str, object]) -> SourceGraphContentNode:
    """Parse an exact production node-reference shape that has no text field."""
    mapping = _mapping(value, "content node", SourceGraphValidationError)
    _exact_fields(
        mapping,
        {"node_id", "resource_id", "owner_division_id", "node_kind", "selector_ids"},
        "content node",
        SourceGraphValidationError,
    )
    return SourceGraphContentNode(
        node_id=_text_value(mapping["node_id"], "node_id", SourceGraphValidationError),
        resource_id=_text_value(
            mapping["resource_id"], "resource_id", SourceGraphValidationError
        ),
        owner_division_id=_text_value(
            mapping["owner_division_id"], "owner_division_id", SourceGraphValidationError
        ),
        node_kind=_text_value(
            mapping["node_kind"], "node_kind", SourceGraphValidationError
        ),
        selector_ids=_text_tuple(
            mapping["selector_ids"], "selector_ids", SourceGraphValidationError
        ),
    )


def _parse_graph_selector(value: Mapping[str, object]) -> SourceGraphSelector:
    """Parse the exact complete production selector shape."""
    mapping = _mapping(value, "selector", SourceGraphValidationError)
    _exact_fields(
        mapping,
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
            "parser_source_anchor_ids",
        },
        "selector",
        SourceGraphValidationError,
    )
    return SourceGraphSelector(
        selector_id=_text_value(
            mapping["selector_id"], "selector_id", SourceGraphValidationError
        ),
        selector_type=_text_value(
            mapping["selector_type"], "selector_type", SourceGraphValidationError
        ),
        resource_id=_text_value(
            mapping["resource_id"], "resource_id", SourceGraphValidationError
        ),
        presentation_unit_id=_optional_text_value(
            mapping["presentation_unit_id"],
            "presentation_unit_id",
            SourceGraphValidationError,
        ),
        source_path=_optional_text_value(
            mapping["source_path"], "source_path", SourceGraphValidationError
        ),
        char_start=_optional_int_value(
            mapping["char_start"], "char_start", SourceGraphValidationError
        ),
        char_end=_optional_int_value(
            mapping["char_end"], "char_end", SourceGraphValidationError
        ),
        bbox=_optional_bbox_value(mapping["bbox"], SourceGraphValidationError),
        source_anchor_ids=_text_tuple(
            mapping["source_anchor_ids"],
            "source_anchor_ids",
            SourceGraphValidationError,
        ),
        parser_source_anchor_ids=_text_tuple(
            mapping["parser_source_anchor_ids"],
            "parser_source_anchor_ids",
            SourceGraphValidationError,
        ),
    )


def _parse_graph_representation(
    value: Mapping[str, object],
) -> SourceGraphRepresentation:
    """Parse exact production representation lineage without payload fields."""
    mapping = _mapping(value, "representation", SourceGraphValidationError)
    _exact_fields(
        mapping,
        {
            "representation_id",
            "subject_id",
            "representation_type",
            "artifact_id",
            "selector_ids",
            "caption_node_id",
        },
        "representation",
        SourceGraphValidationError,
    )
    return SourceGraphRepresentation(
        representation_id=_text_value(
            mapping["representation_id"],
            "representation_id",
            SourceGraphValidationError,
        ),
        subject_id=_text_value(
            mapping["subject_id"], "subject_id", SourceGraphValidationError
        ),
        representation_type=_text_value(
            mapping["representation_type"],
            "representation_type",
            SourceGraphValidationError,
        ),
        artifact_id=_optional_text_value(
            mapping["artifact_id"], "artifact_id", SourceGraphValidationError
        ),
        selector_ids=_text_tuple(
            mapping["selector_ids"], "selector_ids", SourceGraphValidationError
        ),
        caption_node_id=_optional_text_value(
            mapping["caption_node_id"], "caption_node_id", SourceGraphValidationError
        ),
    )


def _parse_graph_binding(value: Mapping[str, object]) -> SourceGraphNativeBinding:
    """Parse exact native binding metadata without pointer dereference."""
    mapping = _mapping(value, "native binding", SourceGraphValidationError)
    fields = {
        "binding_id",
        "canonical_id",
        "artifact_id",
        "native_pointer",
        "binding_role",
    }
    _exact_fields(mapping, fields, "native binding", SourceGraphValidationError)
    return SourceGraphNativeBinding(
        *(
            _text_value(mapping[key], key, SourceGraphValidationError)
            for key in (
                "binding_id",
                "canonical_id",
                "artifact_id",
                "native_pointer",
                "binding_role",
            )
        )
    )


def _parse_graph_activity(
    value: Mapping[str, object],
) -> SourceGraphProcessingActivity:
    """Parse exact processing lineage and artifact ID arrays."""
    mapping = _mapping(value, "processing activity", SourceGraphValidationError)
    _exact_fields(
        mapping,
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
        SourceGraphValidationError,
    )
    return SourceGraphProcessingActivity(
        activity_id=_text_value(
            mapping["activity_id"], "activity_id", SourceGraphValidationError
        ),
        activity_type=_text_value(
            mapping["activity_type"], "activity_type", SourceGraphValidationError
        ),
        run_id=_text_value(mapping["run_id"], "run_id", SourceGraphValidationError),
        correlation_id=_text_value(
            mapping["correlation_id"], "correlation_id", SourceGraphValidationError
        ),
        method=_text_value(mapping["method"], "method", SourceGraphValidationError),
        parser_ids=_text_tuple(
            mapping["parser_ids"], "parser_ids", SourceGraphValidationError
        ),
        input_artifact_ids=_text_tuple(
            mapping["input_artifact_ids"],
            "input_artifact_ids",
            SourceGraphValidationError,
        ),
        output_artifact_ids=_text_tuple(
            mapping["output_artifact_ids"],
            "output_artifact_ids",
            SourceGraphValidationError,
        ),
    )


def _parse_graph_artifact(
    value: Mapping[str, object],
) -> SourceGraphArtifactDescriptor:
    """Parse exact artifact metadata without loading the referenced object."""
    mapping = _mapping(value, "artifact descriptor", SourceGraphValidationError)
    _exact_fields(
        mapping,
        {"artifact_id", "role", "uri", "media_type", "sha256", "schema_version"},
        "artifact descriptor",
        SourceGraphValidationError,
    )
    return SourceGraphArtifactDescriptor(
        artifact_id=_text_value(
            mapping["artifact_id"], "artifact_id", SourceGraphValidationError
        ),
        role=_text_value(mapping["role"], "role", SourceGraphValidationError),
        uri=_text_value(mapping["uri"], "uri", SourceGraphValidationError),
        media_type=_text_value(
            mapping["media_type"], "media_type", SourceGraphValidationError
        ),
        sha256=_optional_text_value(
            mapping["sha256"], "sha256", SourceGraphValidationError
        ),
        schema_version=_optional_text_value(
            mapping["schema_version"], "schema_version", SourceGraphValidationError
        ),
    )


def _parse_address_catalog(
    value: object, *, compact_fixture: bool
) -> ProvenanceAddressCatalog:
    """Parse exact catalog and nested address family field sets.

    The strict address reader calls this after duplicate-key decoding. The helper
    accepts only the frozen aggregate shape, selects compact or production strong
    selector parsing explicitly, and wraps malformed nested values in typed
    address validation failures. It performs no graph lookup or I/O.
    """
    try:
        mapping = _mapping(value, "address catalog", ProvenanceAddressValidationError)
        _exact_fields(
            mapping,
            {
                "schema",
                "strong_addresses",
                "logical_addresses",
                "evidence_set_addresses",
                "resolver_outcomes",
            },
            "address catalog",
            ProvenanceAddressValidationError,
        )
        return ProvenanceAddressCatalog(
            schema=_text_value(
                mapping["schema"], "schema", ProvenanceAddressValidationError
            ),
            strong_addresses=tuple(
                _parse_strong_address(item, compact=compact_fixture)
                for item in _mapping_sequence(
                    mapping["strong_addresses"],
                    "strong_addresses",
                    ProvenanceAddressValidationError,
                )
            ),
            logical_addresses=tuple(
                _parse_logical_address(item)
                for item in _mapping_sequence(
                    mapping["logical_addresses"],
                    "logical_addresses",
                    ProvenanceAddressValidationError,
                )
            ),
            evidence_set_addresses=tuple(
                _parse_evidence_address(item)
                for item in _mapping_sequence(
                    mapping["evidence_set_addresses"],
                    "evidence_set_addresses",
                    ProvenanceAddressValidationError,
                )
            ),
            resolver_outcomes=_text_tuple(
                mapping["resolver_outcomes"],
                "resolver_outcomes",
                ProvenanceAddressValidationError,
            ),
            compact_fixture=compact_fixture,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ProvenanceAddressError):
            raise
        raise ProvenanceAddressValidationError(
            "Provenance address catalog contains malformed nested values"
        ) from error


def _parse_strong_address(
    value: Mapping[str, object], *, compact: bool
) -> StrongProvenanceAddress:
    """Parse exact strong-address fields and compact/production selectors."""
    mapping = _mapping(value, "strong address", ProvenanceAddressValidationError)
    _exact_fields(
        mapping,
        {
            "address_id",
            "source_sha256",
            "graph_revision",
            "resource_id",
            "canonical_target",
            "selectors",
        },
        "strong address",
        ProvenanceAddressValidationError,
    )
    return StrongProvenanceAddress(
        address_id=_text_value(
            mapping["address_id"], "address_id", ProvenanceAddressValidationError
        ),
        source_sha256=_text_value(
            mapping["source_sha256"],
            "source_sha256",
            ProvenanceAddressValidationError,
        ),
        graph_revision=_text_value(
            mapping["graph_revision"],
            "graph_revision",
            ProvenanceAddressValidationError,
        ),
        resource_id=_text_value(
            mapping["resource_id"], "resource_id", ProvenanceAddressValidationError
        ),
        canonical_target=_parse_target(mapping["canonical_target"]),
        selectors=tuple(
            _parse_provenance_selector(item, compact=compact)
            for item in _mapping_sequence(
                mapping["selectors"], "selectors", ProvenanceAddressValidationError
            )
        ),
    )


def _parse_target(value: object) -> ProvenanceTarget:
    """Parse one of the closed node/division/representation target shapes."""
    mapping = _mapping(value, "canonical target", ProvenanceAddressValidationError)
    id_fields = {"node_id", "division_id", "representation_id"}
    present = id_fields & set(mapping)
    if len(present) != 1 or set(mapping) not in (
        present,
        present | {"char_start", "char_end"},
    ):
        raise ProvenanceAddressValidationError("Canonical target fields are invalid")
    return ProvenanceTarget(
        node_id=(
            _text_value(mapping["node_id"], "node_id", ProvenanceAddressValidationError)
            if "node_id" in mapping
            else None
        ),
        division_id=(
            _text_value(
                mapping["division_id"], "division_id", ProvenanceAddressValidationError
            )
            if "division_id" in mapping
            else None
        ),
        representation_id=(
            _text_value(
                mapping["representation_id"],
                "representation_id",
                ProvenanceAddressValidationError,
            )
            if "representation_id" in mapping
            else None
        ),
        char_start=(
            _int_value(
                mapping["char_start"], "char_start", ProvenanceAddressValidationError
            )
            if "char_start" in mapping
            else None
        ),
        char_end=(
            _int_value(
                mapping["char_end"], "char_end", ProvenanceAddressValidationError
            )
            if "char_end" in mapping
            else None
        ),
    )


def _parse_provenance_selector(
    value: Mapping[str, object], *, compact: bool
) -> ProvenanceSelector:
    """Parse exact compact text-position or closed richer selector fields."""
    mapping = _mapping(value, "provenance selector", ProvenanceAddressValidationError)
    compact_fields = {"selector_type", "source_path", "char_start", "char_end"}
    production_fields = compact_fields | {
        "selector_id",
        "presentation_unit_id",
        "bbox",
        "source_anchor_ids",
        "parser_source_anchor_ids",
    }
    if compact:
        _exact_fields(
            mapping,
            compact_fields,
            "provenance selector",
            ProvenanceAddressValidationError,
        )
    elif not set(mapping) <= production_fields or "selector_type" not in mapping:
        raise ProvenanceAddressValidationError("Provenance selector fields are invalid")
    return ProvenanceSelector(
        selector_type=_text_value(
            mapping["selector_type"],
            "selector_type",
            ProvenanceAddressValidationError,
        ),
        source_path=_optional_mapping_text(
            mapping, "source_path", ProvenanceAddressValidationError
        ),
        char_start=_optional_mapping_int(
            mapping, "char_start", ProvenanceAddressValidationError
        ),
        char_end=_optional_mapping_int(
            mapping, "char_end", ProvenanceAddressValidationError
        ),
        selector_id=_optional_mapping_text(
            mapping, "selector_id", ProvenanceAddressValidationError
        ),
        presentation_unit_id=_optional_mapping_text(
            mapping, "presentation_unit_id", ProvenanceAddressValidationError
        ),
        bbox=(
            _optional_bbox_value(mapping["bbox"], ProvenanceAddressValidationError)
            if "bbox" in mapping
            else None
        ),
        source_anchor_ids=(
            _text_tuple(
                mapping["source_anchor_ids"],
                "source_anchor_ids",
                ProvenanceAddressValidationError,
            )
            if "source_anchor_ids" in mapping
            else ()
        ),
        parser_source_anchor_ids=(
            _text_tuple(
                mapping["parser_source_anchor_ids"],
                "parser_source_anchor_ids",
                ProvenanceAddressValidationError,
            )
            if "parser_source_anchor_ids" in mapping
            else ()
        ),
    )


def _parse_logical_address(value: Mapping[str, object]) -> LogicalProvenanceAddress:
    """Parse exact logical family/version-rule/division fields."""
    mapping = _mapping(value, "logical address", ProvenanceAddressValidationError)
    fields = {
        "address_id",
        "resource_family_id",
        "version_rule",
        "division_reference",
    }
    _exact_fields(mapping, fields, "logical address", ProvenanceAddressValidationError)
    return LogicalProvenanceAddress(
        *(
            _text_value(mapping[key], key, ProvenanceAddressValidationError)
            for key in (
                "address_id",
                "resource_family_id",
                "version_rule",
                "division_reference",
            )
        )
    )


def _parse_evidence_address(value: Mapping[str, object]) -> EvidenceSetAddress:
    """Parse exact evidence-set claim and ordered member arrays."""
    mapping = _mapping(value, "evidence-set address", ProvenanceAddressValidationError)
    _exact_fields(
        mapping,
        {"address_id", "claim_ids", "member_address_ids"},
        "evidence-set address",
        ProvenanceAddressValidationError,
    )
    return EvidenceSetAddress(
        address_id=_text_value(
            mapping["address_id"], "address_id", ProvenanceAddressValidationError
        ),
        claim_ids=_text_tuple(
            mapping["claim_ids"], "claim_ids", ProvenanceAddressValidationError
        ),
        member_address_ids=_text_tuple(
            mapping["member_address_ids"],
            "member_address_ids",
            ProvenanceAddressValidationError,
        ),
    )


def _strict_json_loads(
    payload: bytes,
    *,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
    label: str,
) -> object:
    """Decode UTF-8 JSON while rejecting duplicate object keys at every depth.

    Strict graph/address readers call this trust helper. Its pairs hook builds a
    normal mapping only after proving each key appears once, so later exact-field
    checks cannot be bypassed. It performs no I/O, is deterministic, and wraps
    Unicode/JSON/type failures in the supplied bounded domain error.
    """
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Build one mapping and reject a repeated key before overwriting it."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise error_type(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise error_type(f"{label} is not valid UTF-8 JSON") from error


def _json_bytes(value: object) -> bytes:
    """Encode stable compact sorted-key UTF-8 JSON with one trailing newline.

    Revision hashing and persistence use this pure canonicalizer. Mapping order is
    normalized while list order remains meaningful. It performs no I/O and is
    idempotent for JSON-safe immutable graph/address projections.
    """
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _mapping(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> Mapping[str, object]:
    """Require one string-key mapping at a persisted-data trust boundary."""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise error_type(f"{label} must be a JSON object")
    return value


def _mapping_sequence(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> tuple[Mapping[str, object], ...]:
    """Require an array of mappings while rejecting strings and mixed records."""
    if not isinstance(value, list):
        raise error_type(f"{label} must be a JSON array")
    return tuple(_mapping(item, label, error_type) for item in value)


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> None:
    """Reject missing and unknown persisted fields to keep schemas closed."""
    if set(value) != expected:
        raise error_type(f"{label} fields do not match the v3.2 contract")


def _text_value(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> str:
    """Require a bounded nonempty string without coercing untrusted JSON values."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4_096:
        raise error_type(f"{label} must be bounded nonempty text")
    return value


def _optional_text_value(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> str | None:
    """Accept ``null`` or delegate to strict bounded text validation."""
    return None if value is None else _text_value(value, label, error_type)


def _optional_mapping_text(
    value: Mapping[str, object],
    key: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> str | None:
    """Read an absent/null/string optional mapping field without coercion."""
    return (
        _optional_text_value(value[key], key, error_type)
        if key in value
        else None
    )


def _int_value(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> int:
    """Require a real JSON integer while rejecting booleans and coercion."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise error_type(f"{label} must be an integer")
    return value


def _optional_int_value(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> int | None:
    """Accept ``null`` or one strict JSON integer."""
    return None if value is None else _int_value(value, label, error_type)


def _optional_mapping_int(
    value: Mapping[str, object],
    key: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> int | None:
    """Read an absent/null/integer optional mapping field without coercion."""
    return _optional_int_value(value[key], key, error_type) if key in value else None


def _text_tuple(
    value: object,
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> tuple[str, ...]:
    """Require a JSON string array and preserve its authoritative order."""
    if not isinstance(value, list):
        raise error_type(f"{label} must be a JSON array")
    return tuple(_text_value(item, label, error_type) for item in value)


def _optional_bbox_value(
    value: object,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> tuple[float, float, float, float] | None:
    """Parse optional four-number geometry without accepting booleans or NaN."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise error_type("bbox must contain four numeric coordinates")
    if any(
        not isinstance(item, (int, float)) or isinstance(item, bool)
        for item in value
    ):
        raise error_type("bbox coordinates must be numeric")
    result = tuple(float(item) for item in value)
    _validate_bbox(result, error_type)
    return result  # type: ignore[return-value]


def _require_text(value: str, label: str) -> None:
    """Validate a bounded graph scalar without echoing its potentially unsafe value."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4_096:
        raise SourceGraphValidationError(f"{label} must be bounded nonempty text")


def _require_address_text(value: str, label: str) -> None:
    """Validate a bounded address scalar under the address error hierarchy."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4_096:
        raise ProvenanceAddressValidationError(
            f"{label} must be bounded nonempty text"
        )


def _require_sha256(value: str, label: str) -> None:
    """Require one lowercase hexadecimal SHA-256 under graph validation."""
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SourceGraphValidationError(f"{label} must be a lowercase SHA-256")


def _require_address_sha256(value: str, label: str) -> None:
    """Require one lowercase hexadecimal SHA-256 under address validation."""
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProvenanceAddressValidationError(
            f"{label} must be a lowercase SHA-256"
        )


def _validate_logical_path(value: str) -> None:
    """Reject absolute, backslash, dot, traversal, and overlong logical locators.

    Selector validation calls this without filesystem I/O. ``PurePosixPath`` is
    used only for syntax; the locator is never opened. Typed address validation
    protects callers from path confusion and traversal semantics.
    """
    _require_address_text(value, "source_path")
    path = PurePosixPath(value)
    if (
        len(value) > 1_024
        or value.startswith(('/', '\\'))
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProvenanceAddressValidationError(
            "source_path must be a safe relative logical locator"
        )


def _validate_range(
    start: int,
    end: int | None,
    *,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
    label: str,
) -> None:
    """Require complete non-negative ordered character bounds without source access."""
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end < start
    ):
        raise error_type(f"{label} is invalid")


def _validate_bbox(
    bbox: tuple[float, float, float, float],
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError],
) -> None:
    """Require finite ordered rectangle coordinates as metadata only."""
    if len(bbox) != 4 or any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not (-1e12 < float(item) < 1e12)
        for item in bbox
    ):
        raise error_type("bbox coordinates are invalid")
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        raise error_type("bbox coordinates are not ordered")


def _require_unique(
    values: Sequence[str],
    label: str,
    error_type: type[SourceGraphValidationError]
    | type[ProvenanceAddressValidationError] = SourceGraphValidationError,
) -> None:
    """Reject duplicate ordered IDs without sorting away persisted intent."""
    if len(set(values)) != len(values):
        raise error_type(f"{label} must be unique")


def _require_unique_ordered(values: Sequence[str], label: str) -> None:
    """Require unique lexically ordered IDs for generated projection metadata."""
    _require_unique(values, label)
    if tuple(values) != tuple(sorted(values)):
        raise SourceGraphValidationError(f"{label} must be deterministically ordered")


def _unique_attribute_index(
    values: Sequence[object], attribute: str, label: str
) -> dict[str, object]:
    """Build a local exact ID index and reject duplicates before overwrite."""
    result: dict[str, object] = {}
    for item in values:
        key = getattr(item, attribute)
        if key in result:
            raise SourceGraphValidationError(f"{label} must be unique: {key}")
        result[key] = item
    return result


def _reject_duplicate_attribute(
    values: Sequence[object], attribute: str, label: str
) -> None:
    """Validate generated ID uniqueness without retaining a persistent index."""
    _unique_attribute_index(values, attribute, label)
