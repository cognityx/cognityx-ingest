"""Create bounded parser invocation plans without executing or fusing parsers.

Purpose
-------
Cognityx Ingest can know which parser adapters are present and what their
documentation claims, but that evidence does not itself decide which adapters
should run for one source. This module turns validated capability-registry facts
and explicit operator boundaries into an immutable parser invocation plan.

Design principles
-----------------
The T03 capability registry remains the factual authority. T04 routing may use
only those facts plus explicit typed policy; model memory and human-guidance prose
are not executable rules. Deterministic validation always decides whether a plan
is acceptable. A proposal provider, including an LLM-backed provider, may suggest
invocations but cannot change an allowlist, budget, security rule, runtime fact,
or validation result. Persisted plans are untrusted and are never silently
reordered or repaired.

Processing flow
---------------
``ParserRoutingService.plan`` validates a ``ParserRoutingRequest`` and its
capability registry. Deterministic mode evaluates typed rules and never calls a
proposal provider. Hybrid mode applies a deterministic boundary, calls one
provider inside it, and validates the proposal. LLM-directed mode lets one
provider propose parser order, scopes, purposes, and a stop condition, then runs
the same deterministic validation. The output is a ``RoutingPlan``; this module
does not call a parser.

An accepted plan contains only invocations that passed every hard check. A
rejected plan contains no executable selections and records bounded reason IDs
for audit. Deterministic planning keeps rule-matched, live-eligible work under the
separate name ``candidate_invocations``; candidates become selected only after
the whole plan passes. Proposal-backed modes retain rejected candidates inside
their untrusted proposal. This naming prevents a later consumer from treating a
rejected run as authorized work.

Every invocation purpose is attributed to that invocation's own parser. A
Docling purpose cannot be justified by a PyMuPDF capability elsewhere in the
plan. Each required input capability must appear as an invocation purpose backed
by that same parser's live T03 assertion. Malformed provider fields are
quarantined rather than copied into a valid-looking artifact. Deterministic rules
remain ordered policy records, hybrid selection order remains controlled by the
trusted boundary, and LLM-directed order remains a visible provider decision.

Frozen fixtures are read through exact compact compatibility shapes. New
service-built plans use complete canonical shapes that persist input facts, the
hard boundary, registry version, full validation, and proposal or rule evidence.
They also retain ``registry_sha256``, a SHA-256 digest over the exact canonical
registry bytes. The version helps people identify a release; the digest lets
audit and execution consumers identify the exact evidence snapshot. Persisting
facts and boundary prevents a reloaded decision from losing why it was accepted
or rejected. The plan does not embed or mutate the registry itself.

Proposal-backed planning also receives a trusted provider profile
(``RoutingProviderProfile``) from application composition. It states whether the
provider uses an external service and which governed security tags apply. The
service validates this profile before any provider call. An untrusted proposal
may record claimed service use and tags
for audit, but those claims cannot authorize the call or satisfy policy. Canonical
hybrid and LLM-directed plans persist the trusted profile so later consumers can
interpret the security result; compact fixtures deliberately do not.

``RoutingPlan.validate`` proves only that fields form an internally consistent
record. ``validate_against_registry`` additionally hashes the supplied T03
registry, checks its version, reconstructs deterministic validation, and compares
the exact result and selections. ``require_executable`` adds acceptance to that
proof. Compact fixtures remain audit-readable compatibility examples but are not
execution-authorized. Future orchestration and the legacy adapter must provide the
exact registry and use this stronger guard before treating selected invocations
as executable. Registry verification calls no proposal provider or parser.

The optional legacy adapter is a final representation check, not execution. It
can describe a purpose-free document plan with existing fixed or compare policy
names only when no scope, stop condition, security tag, or rejection would be
lost. Existing ``ParserRouter`` composition may consume that policy later; T04
does not construct a router or inspect extraction output.

Primary consumers
-----------------
Ingest orchestration, audit tools, tests, and a later execution coordinator read
``RoutingPlan``. A narrow compatibility adapter can explain a lossless plan as an
existing ``ExtractionPolicy``. T05 consumes routing and parser observations when
alignment, fusion, and adjudication are implemented.

Ownership boundary
------------------
Cognityx Ingest owns routing policy, proposal validation, and plan records. T03
owns capability evidence. The existing ``ParserRouter`` owns parser execution.
Proposal providers own only proposal generation, and operators own hard routing
boundaries. SDK commands, inference-server lifecycle, Storage, DataForge, and
query-time retrieval remain outside this module.

Non-goals
---------
T04 does not parse documents, call ``_fuse_results``, align observations, combine
pages or blocks, choose winning facts, adjudicate conflicts, or create a fused
``CanonicalDocument``. It does not create segmentation views, retention policy,
Source Graph records, provenance addresses, DataForge outputs, CLI commands,
network clients, model downloads, capability refresh, or a persistence database.
The legacy fixed, rule, fallback, compare, and agent execution behavior remains
unchanged. Deterministic, hybrid, and llm-directed are separate T04 plan modes,
not new ``ExtractionPolicy`` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from cognityx_ingest.parser import ExtractionPolicy
from cognityx_ingest.parser_capabilities import (
    ParserCapabilityError,
    ParserCapabilityRecord,
    ParserCapabilityRegistry,
)


ROUTING_PLAN_SCHEMA = "cognityx.ingest.routing-plan/v3.2"
ADAPTIVE_ROUTING_MODES: tuple[str, str, str] = (
    "deterministic",
    "hybrid",
    "llm-directed",
)
LEGACY_POLICY_TO_ADAPTIVE_MODE: Mapping[str, str] = MappingProxyType(
    {
        "fixed": "deterministic",
        "rule": "deterministic",
        "fallback": "deterministic",
        "compare": "deterministic",
        "agent": "hybrid",
    }
)

_SUPPORTED_SCOPES: tuple[str, str] = (
    "document",
    "pages-with-native-links",
)
_SUPPORTED_STOP_CONDITIONS = (
    "all-required-capabilities-observed-or-explicitly-unresolved",
)
_ROUTING_REQUIREMENTS = (
    "hierarchy",
    "tables",
    "native_links",
    "page_labels",
    "geometry",
    "pictures",
    "provenance",
    "native_pdf_text",
    "paragraph_text",
)
_REQUIREMENT_TO_CAPABILITY = MappingProxyType(
    {
        "hierarchy": "document_hierarchy",
        "tables": "tables",
        "native_links": "native_links",
        "page_labels": "page_labels",
        "geometry": "bounding_boxes",
        "pictures": "pictures",
        "provenance": "provenance",
        "native_pdf_text": "native_pdf_text",
        "paragraph_text": "paragraph_text",
    }
)
_ELIGIBLE_CAPABILITY_STATUSES = frozenset(
    {"available", "declared", "declared-when-available"}
)
_PARSER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
_RULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
_MAX_AUDIT_TEXT_LENGTH = 512


class ParserRoutingError(Exception):
    """Provide one stable domain boundary for all parser-routing failures.

    Responsibility:
        Prevent JSON, proposal-provider, registry, and mapping implementation
        errors from leaking through the public routing API.
    Constructed by:
        Routing readers, validators, compatibility adapters, and the service.
    Used by:
        Ingest orchestration, audit tools, tests, and future T05 coordinators.
    Invariants:
        Messages contain bounded logical identifiers and no source payloads,
        credentials, local paths, or parser-native bytes.
    Lifecycle/persistence:
        Failures are transient and are never serialized as routing evidence.
    Thread-safety assumptions:
        Immutable exception arguments contain no shared mutable state.
    """


class ParserRoutingValidationError(ParserRoutingError):
    """Report malformed routing records or persisted plan structure.

    Responsibility:
        Distinguish invalid routing data from rejected but well-shaped proposals.
    Constructed by:
        Strict JSON readers and record validators.
    Used by:
        Catalog readers, service callers, and trust-boundary tests.
    Invariants:
        Invalid data never becomes a validated ``RoutingPlan``.
    Lifecycle/persistence:
        Validation is read-only and never repairs supplied records.
    Thread-safety assumptions:
        The error owns only immutable diagnostic text.
    """


class ParserRoutingRejectedError(ParserRoutingError):
    """Report a caller's demand that a rejected auditable plan be executable.

    Responsibility:
        Keep plan rejection visible instead of silently executing selected IDs.
    Constructed by:
        ``RoutingPlan.require_accepted``.
    Used by:
        Future execution coordinators that require an accepted plan.
    Invariants:
        Rejection reasons are bounded identifiers copied from validation results.
    Lifecycle/persistence:
        The plan remains immutable and serializable after the transient failure.
    Thread-safety assumptions:
        Raising the error does not mutate shared state.
    """


class ParserRoutingProposalError(ParserRoutingError):
    """Contain missing, malformed, or failed proposal-provider interactions.

    Responsibility:
        Translate provider failures without turning hybrid or LLM-directed mode
        into an undocumented deterministic fallback.
    Constructed by:
        ``ParserRoutingService`` around its one bounded provider call.
    Used by:
        Service callers and provider-boundary tests.
    Invariants:
        Provider exception text and source content are not exposed.
    Lifecycle/persistence:
        Failed proposals are transient and produce no plan artifact.
    Thread-safety assumptions:
        The error contains immutable bounded context only.
    """


class ParserRoutingCapabilityError(ParserRoutingError):
    """Report invalid requirement vocabulary or unusable registry evidence.

    Responsibility:
        Keep capability lookup and registry validation failures inside the T04
        domain while preserving T03 records unchanged.
    Constructed by:
        Request validation and capability-eligibility helpers.
    Used by:
        Deterministic and proposal-backed planning callers.
    Invariants:
        No capability is inferred from model memory or free-form guidance.
    Lifecycle/persistence:
        Registry snapshots remain unmodified after the transient error.
    Thread-safety assumptions:
        No mutable registry state is attached to the exception.
    """


class ParserRoutingCompatibilityError(ParserRoutingError):
    """Report a routing plan that cannot be expressed by legacy execution policy.

    Responsibility:
        Prevent lossy conversion from dropping scope, purpose, stop conditions,
        validation failures, or other T04 semantics.
    Constructed by:
        Legacy mapping helpers and ``RoutingPlan.to_extraction_policy``.
    Used by:
        Compatibility-aware composition code and tests.
    Invariants:
        Existing ``ExtractionPolicy`` behavior is never changed or extended.
    Lifecycle/persistence:
        Conversion is read-only and leaves the plan unchanged.
    Thread-safety assumptions:
        The error contains only immutable diagnostics.
    """


@dataclass(frozen=True, slots=True)
class RoutingInputFacts:
    """Describe bounded source facts used for routing without source content.

    Responsibility:
        Carry media, size, page, class, and explicit capability requirements.
    Constructed by:
        Ingest orchestration or strict routing-plan readers.
    Used by:
        Deterministic rules, proposal providers, and deterministic validation.
    Invariants:
        Ratios are finite and bounded, requirements are unique and canonical,
        and no field stores source text, source bytes, or a local path.
    Lifecycle/persistence:
        The immutable snapshot may be serialized inside deterministic plans.
    Thread-safety assumptions:
        Frozen scalar values and tuples are safe to share.
    """

    media_type: str
    native_text_ratio: float | None = None
    required_capabilities: tuple[str, ...] = ()
    page_count: int | None = None
    source_size_bytes: int | None = None
    document_class: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingBoundary:
    """Define the hard parser, budget, service, scope, and security boundary.

    Responsibility:
        Express deterministic constraints that no proposal provider can alter.
    Constructed by:
        Trusted orchestration or strict fixture readers.
    Used by:
        All three routing modes and the common proposal validator.
    Invariants:
        Parser IDs and tags are unique and canonical, the run budget is positive,
        and scopes belong to the explicit T04 vocabulary.
    Lifecycle/persistence:
        Frozen boundaries are embedded in hybrid plans and never provider-owned.
    Thread-safety assumptions:
        Immutable values make one boundary safe for concurrent planning reads.
    """

    allowlist: tuple[str, ...]
    max_parser_runs: int
    external_services_allowed: bool
    allowed_scopes: tuple[str, ...] = _SUPPORTED_SCOPES
    required_security_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingProviderProfile:
    """Describe trusted provider deployment facts checked before invocation.

    Responsibility:
        Carry application-owned provider identity, external-service use, security
        tags, and optional deployment labels independently of untrusted proposals.
    Constructed by:
        Trusted application composition when wiring a proposal provider.
    Used by:
        ``ParserRoutingService`` preflight, canonical plan readers, registry-bound
        verification, audit tools, and future execution coordinators.
    Main algorithm:
        Validation checks bounded identifiers, an exact Boolean service fact, and
        unique canonical security tags before any provider can be called.
    Invariants:
        The profile is never generated by a provider or LLM, cannot contain source
        content or credentials, and alone determines proposal-provider security.
    Lifecycle/persistence:
        Canonical hybrid and LLM-directed plans retain the immutable profile;
        compact fixtures do not contain one.
    Side effects and failures:
        The record itself has no behavior or side effects; service preflight emits
        typed proposal errors and persisted readers emit typed validation errors.
    Trust boundary:
        Callers trust composition to supply this record and never substitute
        proposal-claimed service use or tags for it.
    Thread-safety assumptions:
        Frozen scalar values and tuples are safe to share across planning calls.
    """

    provider_id: str
    uses_external_services: bool
    security_tags: tuple[str, ...]
    provider_kind: str | None = None
    deployment_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParserInvocation:
    """Describe one parser run requested by a plan without executing it.

    Responsibility:
        Bind a parser identity to an explicit scope, routing purpose, and optional
        governed security tags.
    Constructed by:
        Deterministic rules, proposal providers, or strict plan readers.
    Used by:
        Plan validation, audit consumers, and future execution orchestration.
    Invariants:
        IDs and scopes use approved vocabularies; purposes and tags are unique and
        canonical. Page-scoped records remain plans, not hidden document runs.
    Lifecycle/persistence:
        Frozen invocations persist as part of one routing plan.
    Thread-safety assumptions:
        Immutable strings and tuples are safe to share.
    """

    parser_id: str
    scope: str
    purpose: tuple[str, ...] = ()
    security_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingProposal:
    """Represent one untrusted provider recommendation and its audit metadata.

    Responsibility:
        Carry proposed invocations, stop behavior, provider identifiers, external
        service observation, and security tags without granting authority.
    Constructed by:
        A ``RoutingProposalProvider`` or strict frozen-plan reader.
    Used by:
        Hybrid and LLM-directed deterministic validation.
    Invariants:
        Proposal metadata is bounded, contains no source payload, and cannot
        modify the request boundary or registry.
    Lifecycle/persistence:
        Accepted and rejected proposals may be retained inside immutable plans.
    Thread-safety assumptions:
        Frozen fields are safe for concurrent audit readers.
    """

    invocations: tuple[ParserInvocation, ...]
    reason: str | None = None
    stop_condition: str | None = None
    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    external_services_used: bool = False
    security_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingValidationResult:
    """Retain deterministic acceptance checks and bounded rejection reasons.

    Responsibility:
        Make every hard routing decision independently auditable.
    Constructed by:
        Persisted plan readers or the common deterministic validator.
    Used by:
        ``RoutingPlan``, compatibility adapters, and future execution guards.
    Invariants:
        Every flag is Boolean, acceptance equals the conjunction of checks,
        capability validity includes parser-specific purpose attribution and
        required-purpose coverage, and rejection reasons are deterministic
        identifiers rather than payload text.
    Lifecycle/persistence:
        Frozen results persist with accepted and rejected plans.
    Thread-safety assumptions:
        Immutable Booleans and tuples are safe to share.
    """

    accepted: bool
    allowlist_valid: bool
    budget_valid: bool
    security_valid: bool
    schema_valid: bool
    registry_valid: bool
    runtime_valid: bool
    capability_valid: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeterministicRoutingRule:
    """Describe one extensible routing rule as data rather than branching code.

    Responsibility:
        Match explicit input requirements and produce one bounded invocation.
    Constructed by:
        T04 defaults or trusted orchestration supplying request-specific policy.
    Used by:
        Deterministic mode only; proposal-backed modes do not rewrite rules.
    Invariants:
        Rule identity, trigger vocabulary, media types, parser, scope, and purpose
        are validated and deterministic; no human prose is executed.
    Lifecycle/persistence:
        Rules are immutable policy inputs and are not serialized in plans beyond
        their ordered IDs.
    Thread-safety assumptions:
        Frozen rule records are safe to reuse across planning calls.
    """

    rule_id: str
    trigger_capabilities: tuple[str, ...]
    parser_id: str
    scope: str
    purpose: tuple[str, ...]
    media_types: tuple[str, ...] = ("application/pdf",)
    minimum_native_text_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class ParserRoutingRequest:
    """Bind one routing mode to facts, policy boundary, and T03 registry.

    Responsibility:
        Supply every authoritative input needed for routing while keeping proposal
        providers outside the serializable request.
    Constructed by:
        Trusted Ingest orchestration and focused tests.
    Used by:
        ``ParserRoutingService`` and bounded proposal providers.
    Invariants:
        Mode is one of exactly three values, the registry validates, and optional
        deterministic rules remain explicit immutable records.
    Lifecycle/persistence:
        Requests are transient and are not serialized; resulting plans may persist.
    Thread-safety assumptions:
        Nested records are immutable and registry reads are side-effect free.
    """

    mode: str
    input_facts: RoutingInputFacts
    boundary: RoutingBoundary
    registry: ParserCapabilityRegistry
    deterministic_rules: tuple[DeterministicRoutingRule, ...] | None = None


class RoutingProposalProvider(Protocol):
    """Define the bounded proposal seam used by hybrid and LLM-directed modes.

    Responsibility:
        Propose routing records from registry facts and a hard boundary without
        gaining authority to accept them.
    Constructed by:
        Applications, deterministic test doubles, or future inference adapters.
    Used by:
        ``ParserRoutingService.plan`` exactly once per proposal-backed request.
    Invariants:
        Inputs contain no source bytes or native payloads and output is always
        treated as untrusted by deterministic validation.
    Lifecycle/persistence:
        Providers are external collaborators; T04 stores only bounded proposal
        audit facts, never provider credentials or clients.
    Thread-safety assumptions:
        Concurrency behavior belongs to the provider; the service keeps no provider
        state and makes one synchronous call.
    """

    def propose(
        self,
        request: ParserRoutingRequest,
        registry: ParserCapabilityRegistry,
        boundary: RoutingBoundary,
    ) -> RoutingProposal:
        """Return one proposal without parser execution or boundary mutation.

        Hybrid or LLM-directed service callers invoke this once with immutable
        facts. The provider may perform its own bounded work, but T04 supplies no
        source content and deterministically validates the returned proposal.
        Provider failures are translated to ``ParserRoutingProposalError``.
        """
        ...


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """Represent one validated mode-specific parser invocation plan.

    Responsibility:
        Aggregate candidates, selected invocations, validation, exact registry
        binding, input facts, hard boundary, proposal audit facts, deterministic
        rules, and frozen wire-shape compatibility.
    Constructed by:
        ``ParserRoutingService``, ``from_dict``, or ``from_json_bytes``.
    Used by:
        Audit readers use internal validation; future execution orchestration and
        compatibility adapters use registry-bound executable verification.
    Invariants:
        Schema and mode are exact, mode-specific fields are consistent, ordering
        is deterministic, rejected plans select nothing, and canonical plans
        retain complete context plus the exact registry SHA-256.
    Lifecycle/persistence:
        Compact fixtures reload unchanged; canonical plans serialize complete
        decision context deterministically and perform no storage writes.
    Thread-safety assumptions:
        All nested records are immutable; read and serialization methods use local
        values only.
    """

    schema: str
    mode: str
    selected_invocations: tuple[ParserInvocation, ...]
    validation_result: RoutingValidationResult
    registry_version: str | None = None
    llm_used: bool = False
    input_facts: RoutingInputFacts | None = None
    boundary: RoutingBoundary | None = None
    proposal: RoutingProposal | None = None
    rules_evaluated: tuple[str, ...] = ()
    registry_sha256: str | None = None
    candidate_invocations: tuple[ParserInvocation, ...] = ()
    provider_profile: RoutingProviderProfile | None = None
    _compact_fixture: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RoutingPlan:
        """Parse one strict mode-specific routing mapping without normalization.

        Artifact readers call this with untrusted mappings. The algorithm checks
        exact fields, preserves every supplied list, parses one of the three frozen
        shapes or its documented canonical extension, and validates the immutable
        aggregate. It mutates nothing, calls no provider or parser, and translates
        mapping/type failures to ``ParserRoutingValidationError``.
        """
        try:
            mapping = _mapping(value, "routing plan")
            mode = _required_text(mapping.get("mode"), "mode")
            if mode == "deterministic":
                plan = _parse_deterministic_plan(mapping)
            elif mode == "hybrid":
                plan = _parse_hybrid_plan(mapping)
            elif mode == "llm-directed":
                plan = _parse_llm_directed_plan(mapping)
            else:
                raise ParserRoutingValidationError(
                    f"Unsupported adaptive routing mode: {mode}"
                )
        except ParserRoutingError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ParserRoutingValidationError(
                "Routing plan contains malformed values"
            ) from error
        plan.validate()
        return plan

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> RoutingPlan:
        """Decode strict UTF-8 JSON with duplicate-key detection at every depth.

        Artifact readers supply untrusted bytes. The bounded decoder rejects
        duplicate object keys before mappings are built, requires a top-level
        object, and delegates field/order validation to ``from_dict``. It performs
        no I/O beyond decoding, is idempotent, calls no provider or parser, and
        exposes only typed routing failures.
        """
        if not isinstance(payload, bytes):
            raise ParserRoutingValidationError("Routing plan payload must be bytes")
        try:
            value = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_strict_json_object
            )
        except ParserRoutingError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ParserRoutingValidationError(
                "Routing plan payload is not valid UTF-8 JSON"
            ) from error
        if not isinstance(value, Mapping):
            raise ParserRoutingValidationError("Routing plan JSON must be an object")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic mode-specific JSON representation.

        Persistence and audit callers invoke this after validation. Frozen compact
        fixtures retain their exact field shapes; service-built plans emit richer
        validation records within the same schema. Repeated calls are equal,
        mutate no state, and call neither a provider nor a parser.
        """
        self.validate()
        if self.mode == "deterministic":
            return _deterministic_plan_to_dict(self)
        if self.mode == "hybrid":
            return _hybrid_plan_to_dict(self)
        return _llm_directed_plan_to_dict(self)

    def to_json_bytes(self) -> bytes:
        """Serialize one validated plan to stable compact UTF-8 JSON bytes.

        Artifact writers call this deterministic, side-effect-free method. Object
        keys are sorted, list order remains contractual, Unicode is preserved, and
        one newline is appended. Invalid direct construction raises a typed error;
        no provider, parser, network, or persistence service is called.
        """
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def validate(self) -> None:
        """Validate aggregate shape, ordering, records, and mode consistency.

        Readers, serializers, and execution guards call this idempotent method.
        It validates every nested record and exact mode combination without
        modifying the plan, consulting model memory, invoking a provider, or
        executing parsers. Raw type failures become typed validation errors.
        """
        try:
            _validate_plan(self)
        except ParserRoutingError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ParserRoutingValidationError(
                "Routing plan contains malformed typed records"
            ) from error

    def validate_against_registry(
        self, registry: ParserCapabilityRegistry
    ) -> RoutingPlan:
        """Prove a canonical plan against the exact referenced T03 evidence.

        Execution coordinators and audit tools call this after reading a plan.
        The algorithm performs ordinary internal validation, rejects compact
        compatibility records, validates and hashes the supplied registry,
        reconstructs the persisted routing request, reruns common deterministic
        validation over candidates or the proposal, and compares the complete
        result and expected selections. It returns this immutable plan on success.
        Digest, version, context, validation, and selection mismatches raise
        ``ParserRoutingValidationError``. The operation is idempotent and has no
        side effects: it calls no provider, parser, network, fusion, or persistence
        service. The caller owns the trusted registry supplied at this boundary.
        """
        self.validate()
        if self._compact_fixture:
            raise ParserRoutingValidationError(
                "Compact routing plans are audit-readable but not registry-verifiable"
            )
        if not isinstance(registry, ParserCapabilityRegistry):
            raise ParserRoutingValidationError(
                "Registry verification requires a ParserCapabilityRegistry"
            )
        try:
            registry.validate()
        except ParserCapabilityError as error:
            raise ParserRoutingValidationError(
                "Registry verification received invalid capability evidence"
            ) from error
        if self.registry_version != registry.registry_version:
            raise ParserRoutingValidationError(
                "Routing plan registry version does not match supplied registry"
            )
        if self.registry_sha256 != _registry_sha256(registry):
            raise ParserRoutingValidationError(
                "Routing plan registry SHA-256 does not match supplied registry"
            )
        if self.input_facts is None or self.boundary is None:
            raise ParserRoutingValidationError(
                "Canonical routing plan lacks persisted validation context"
            )
        request = ParserRoutingRequest(
            mode=self.mode,
            input_facts=self.input_facts,
            boundary=self.boundary,
            registry=registry,
        )
        _validate_request(request)
        if self.mode == "deterministic":
            evidence = RoutingProposal(invocations=self.candidate_invocations)
            provider_profile = None
        else:
            if self.proposal is None or self.provider_profile is None:
                raise ParserRoutingValidationError(
                    "Canonical proposal-backed plan lacks verification evidence"
                )
            evidence = self.proposal
            provider_profile = self.provider_profile
        recomputed = _validate_proposal(
            request,
            evidence,
            provider_profile=provider_profile,
        )
        if recomputed != self.validation_result:
            raise ParserRoutingValidationError(
                "Routing plan validation result does not match registry evidence"
            )
        expected = evidence.invocations if recomputed.accepted else ()
        if self.selected_invocations != expected:
            raise ParserRoutingValidationError(
                "Routing plan selections do not match registry-backed validation"
            )
        return self

    def require_executable(
        self, registry: ParserCapabilityRegistry
    ) -> RoutingPlan:
        """Return only an accepted plan verified against exact registry evidence.

        Future execution orchestration calls this guard before considering any
        selected invocation. Its algorithm first runs
        ``validate_against_registry`` and then applies the accepted-plan guard,
        returning this plan only when both pass.
        Compact, mismatched, malformed, or rejected plans raise typed routing
        errors. The method is idempotent, trusts only the supplied registry, and
        has no side effects: it calls no provider, parser, network, fusion, or
        persistence service.
        """
        return self.validate_against_registry(registry).require_accepted()

    def require_accepted(self) -> RoutingPlan:
        """Return this plan only when deterministic validation accepted it.

        Future execution coordinators call this guard before using invocations.
        It validates the immutable plan and returns the same object when accepted;
        otherwise it raises ``ParserRoutingRejectedError`` with bounded reason
        identifiers. It has no side effects and never executes a parser.
        """
        self.validate()
        if not self.validation_result.accepted:
            reasons = ",".join(self.validation_result.rejection_reasons)
            raise ParserRoutingRejectedError(
                f"Routing plan was rejected: {reasons or 'validation-failed'}"
            )
        return self

    def to_extraction_policy(
        self, *, registry: ParserCapabilityRegistry
    ) -> ExtractionPolicy:
        """Convert only a lossless accepted document plan to legacy policy.

        Compatibility-aware composition supplies the exact T03 registry before
        constructing an existing ``ParserRouter``. The algorithm first requires
        registry-bound executable verification. One purpose-free document
        invocation then returns ``fixed`` and several return existing ``compare``.
        Compact, mismatched, rejected, scoped, tagged, purposeful, or stopped
        plans raise ``ParserRoutingCompatibilityError`` rather than losing
        semantics. The method is idempotent, trusts no persisted acceptance flag
        by itself, and has no execution side effect: it never calls a provider,
        parser, network, or fusion path.
        """
        try:
            self.require_executable(registry)
        except ParserRoutingValidationError as error:
            raise ParserRoutingCompatibilityError(
                "Unverified routing plan cannot become an ExtractionPolicy"
            ) from error
        except ParserRoutingRejectedError as error:
            raise ParserRoutingCompatibilityError(
                "Rejected routing plan cannot become an ExtractionPolicy"
            ) from error
        stop_condition = self.proposal.stop_condition if self.proposal else None
        if stop_condition is not None:
            raise ParserRoutingCompatibilityError(
                "Routing stop condition cannot be represented by ExtractionPolicy"
            )
        if not self.selected_invocations:
            raise ParserRoutingCompatibilityError(
                "Empty routing plan cannot become an ExtractionPolicy"
            )
        if any(item.scope != "document" for item in self.selected_invocations):
            raise ParserRoutingCompatibilityError(
                "Page-scoped routing cannot be represented by ExtractionPolicy"
            )
        if any(
            item.purpose or item.security_tags for item in self.selected_invocations
        ):
            raise ParserRoutingCompatibilityError(
                "Routing purpose or security tags cannot be discarded"
            )
        if self.provider_profile is not None and (
            self.provider_profile.uses_external_services
            or self.provider_profile.security_tags
        ):
            raise ParserRoutingCompatibilityError(
                "Provider security or external-service semantics cannot be discarded"
            )
        mode = "fixed" if len(self.selected_invocations) == 1 else "compare"
        return ExtractionPolicy(
            mode=mode,
            backends=tuple(item.parser_id for item in self.selected_invocations),
        )


class ParserRoutingService:
    """Generate accepted or rejected plans while keeping parser execution separate.

    Responsibility:
        Implement exactly three mode algorithms and one deterministic validation
        boundary over T03 registry evidence.
    Constructed by:
        Ingest composition or tests; it requires no network or inference client.
    Used by:
        Orchestration that needs a plan before existing parser execution.
    Invariants:
        Deterministic mode never calls a provider; proposal modes preflight trusted
        composition and then call once; every proposal remains untrusted; no
        method executes or fuses parser outputs.
    Lifecycle/persistence:
        The stateless service returns frozen plans and persists nothing.
    Thread-safety assumptions:
        Planning uses local collections and immutable inputs; provider concurrency
        behavior remains provider-owned.
    """

    def plan(
        self,
        request: ParserRoutingRequest,
        *,
        proposal_provider: RoutingProposalProvider | None = None,
        provider_profile: RoutingProviderProfile | None = None,
    ) -> RoutingPlan:
        """Build one mode-specific plan from validated facts and registry evidence.

        Ingest orchestration supplies a request and, only for hybrid or
        LLM-directed mode, a provider and trusted composition profile. The method
        validates request/registry, preflights external-service and security-tag
        policy before any provider call, evaluates rules or calls the provider
        exactly once, then runs common deterministic validation. It returns an
        immutable plan and persists the trusted profile separately from proposal
        claims. Provider and preflight failures become
        ``ParserRoutingProposalError``; capability and shape failures remain
        typed. It mutates nothing and never calls a parser or network API itself.
        """
        _validate_request(request)
        if request.mode == "deterministic":
            return _build_deterministic_plan(request)
        if proposal_provider is None:
            raise ParserRoutingProposalError(
                f"{request.mode} routing requires a proposal provider"
            )
        trusted_profile = _preflight_provider_profile(
            provider_profile,
            request.boundary,
        )
        proposal = _call_proposal_provider(proposal_provider, request)
        validation = _validate_proposal(
            request,
            proposal,
            provider_profile=trusted_profile,
        )
        persisted_proposal = (
            proposal
            if validation.schema_valid
            else _safe_rejected_proposal(proposal)
        )
        plan = RoutingPlan(
            schema=ROUTING_PLAN_SCHEMA,
            mode=request.mode,
            selected_invocations=(
                proposal.invocations if validation.accepted else ()
            ),
            validation_result=validation,
            registry_version=request.registry.registry_version,
            registry_sha256=_registry_sha256(request.registry),
            llm_used=True,
            input_facts=request.input_facts,
            boundary=request.boundary,
            proposal=persisted_proposal,
            provider_profile=trusted_profile,
        )
        plan.validate()
        return plan


_DEFAULT_DETERMINISTIC_RULES = (
    DeterministicRoutingRule(
        rule_id="prefer-docling-for-structured-native-pdf",
        trigger_capabilities=("hierarchy", "tables"),
        parser_id="docling",
        scope="document",
        purpose=("hierarchy", "tables"),
        minimum_native_text_ratio=0.5,
    ),
    DeterministicRoutingRule(
        rule_id="supplement-pymupdf-for-native-links",
        trigger_capabilities=("native_links", "page_labels", "geometry"),
        parser_id="pymupdf",
        scope="document",
        purpose=("native_links", "page_labels", "geometry"),
    ),
    DeterministicRoutingRule(
        rule_id="use-docling-for-visual-and-provenance-facts",
        trigger_capabilities=("pictures", "provenance"),
        parser_id="docling",
        scope="document",
        purpose=("pictures", "provenance"),
    ),
    DeterministicRoutingRule(
        rule_id="use-pymupdf-for-native-pdf-text",
        trigger_capabilities=("native_pdf_text",),
        parser_id="pymupdf",
        scope="document",
        purpose=("native_pdf_text",),
    ),
    DeterministicRoutingRule(
        rule_id="use-future-parser-for-paragraph-text",
        trigger_capabilities=("paragraph_text",),
        parser_id="future-parser",
        scope="document",
        purpose=("paragraph_text",),
    ),
)


def adaptive_mode_for_legacy_policy(policy: ExtractionPolicy) -> str:
    """Explain the adaptive analogue of one validated legacy extraction policy.

    Compatibility and audit callers pass an existing ``ExtractionPolicy``. The
    function validates its public type, looks up the immutable explanatory map,
    and returns one T04 mode without changing or executing the legacy policy.
    Repeated calls are idempotent; unsupported inputs raise a typed compatibility
    failure. There is deliberately no legacy alias for ``llm-directed``.
    """
    if not isinstance(policy, ExtractionPolicy):
        raise ParserRoutingCompatibilityError(
            "Legacy policy must be an ExtractionPolicy"
        )
    try:
        return LEGACY_POLICY_TO_ADAPTIVE_MODE[policy.mode]
    except (KeyError, TypeError) as error:
        raise ParserRoutingCompatibilityError(
            "Legacy extraction policy has no adaptive mapping"
        ) from error


def _build_deterministic_plan(request: ParserRoutingRequest) -> RoutingPlan:
    """Evaluate ordered typed rules and validate the resulting parser set.

    Deterministic service calls use request rules or the reviewed defaults. A rule
    matches explicit media, ratio, and required-capability fields; no human prose
    or model is interpreted. Matched eligible invocations remain candidates in
    evaluation order. The common validator promotes all candidates to selected
    only when the complete plan passes; rejection selects none while retaining
    candidate audit evidence and never falling back to a provider.
    """
    rules = (
        _DEFAULT_DETERMINISTIC_RULES
        if request.deterministic_rules is None
        else request.deterministic_rules
    )
    candidates: list[ParserInvocation] = []
    evaluated: list[str] = []
    for rule in rules:
        if not _rule_matches(rule, request.input_facts):
            continue
        evaluated.append(rule.rule_id)
        invocation = ParserInvocation(
            parser_id=rule.parser_id,
            scope=rule.scope,
            purpose=rule.purpose,
        )
        if _deterministic_invocation_is_eligible(request, rule, invocation) and (
            invocation not in candidates
        ):
            candidates.append(invocation)
    candidate_invocations = tuple(candidates)
    proposal = RoutingProposal(invocations=candidate_invocations)
    validation = _validate_proposal(request, proposal)
    plan = RoutingPlan(
        schema=ROUTING_PLAN_SCHEMA,
        mode="deterministic",
        selected_invocations=(candidate_invocations if validation.accepted else ()),
        validation_result=validation,
        registry_version=request.registry.registry_version,
        registry_sha256=_registry_sha256(request.registry),
        llm_used=False,
        input_facts=request.input_facts,
        boundary=request.boundary,
        rules_evaluated=tuple(evaluated),
        candidate_invocations=candidate_invocations,
    )
    plan.validate()
    return plan


def _deterministic_invocation_is_eligible(
    request: ParserRoutingRequest,
    rule: DeterministicRoutingRule,
    invocation: ParserInvocation,
) -> bool:
    """Filter deterministic selections through hard live eligibility first.

    Rule evaluation calls this before adding a selected invocation. The parser
    must be allowlisted, its scope allowed, its registry record present and
    runtime-available, and every requested rule trigger must have an eligible T03
    assertion. False returns leave the requirement unresolved for the common
    validator; no unavailable parser is silently selected or executed.
    """
    if invocation.parser_id not in request.boundary.allowlist:
        return False
    if invocation.scope not in request.boundary.allowed_scopes:
        return False
    try:
        record = request.registry.get(invocation.parser_id)
    except ParserCapabilityError:
        return False
    if record.parser_discovered.runtime_probe.runtime_available is not True:
        return False
    return all(
        _parser_is_capability_eligible(
            record, _REQUIREMENT_TO_CAPABILITY[requirement]
        )
        for requirement in invocation.purpose
    )


def _rule_matches(rule: DeterministicRoutingRule, facts: RoutingInputFacts) -> bool:
    """Return whether explicit facts activate one deterministic rule record.

    Rule evaluation checks exact media type, optional native-text threshold, and
    intersection with requested routing requirements. It performs no registry
    lookup, parser execution, text analysis, or provider call; capability
    eligibility is enforced later by common deterministic validation.
    """
    if facts.media_type not in rule.media_types:
        return False
    if rule.minimum_native_text_ratio is not None:
        if facts.native_text_ratio is None:
            return False
        if facts.native_text_ratio < rule.minimum_native_text_ratio:
            return False
    return bool(
        set(rule.trigger_capabilities).intersection(facts.required_capabilities)
    )


def _preflight_provider_profile(
    profile: RoutingProviderProfile | None,
    boundary: RoutingBoundary,
) -> RoutingProviderProfile:
    """Authorize trusted provider composition before any provider invocation.

    Proposal-backed service planning calls this after request validation and
    before ``RoutingProposalProvider.propose``. It validates the immutable profile,
    blocks external providers when forbidden, and requires every governed
    security tag to be present in trusted composition metadata. Missing or unsafe
    profiles raise ``ParserRoutingProposalError`` without a provider call. The
    untrusted proposal is deliberately unavailable to this algorithm.
    """
    if profile is None:
        raise ParserRoutingProposalError(
            "Proposal-backed routing requires a trusted provider profile"
        )
    try:
        _validate_provider_profile(profile)
    except ParserRoutingValidationError as error:
        raise ParserRoutingProposalError(
            "Routing provider profile is invalid"
        ) from error
    if profile.uses_external_services and not boundary.external_services_allowed:
        raise ParserRoutingProposalError(
            "Routing provider profile violates external-service policy"
        )
    if not set(boundary.required_security_tags).issubset(profile.security_tags):
        raise ParserRoutingProposalError(
            "Routing provider profile lacks required security tags"
        )
    return profile


def _validate_provider_profile(profile: RoutingProviderProfile) -> None:
    """Validate trusted provider facts without consulting proposal claims.

    Service preflight and canonical plan validation call this deterministic shape
    check. It accepts only the public immutable record, path-free bounded IDs, an
    exact Boolean service fact, and canonical unique tags. It has no side effects
    and raises ``ParserRoutingValidationError`` on malformed trusted composition.
    """
    if not isinstance(profile, RoutingProviderProfile):
        raise ParserRoutingValidationError(
            "provider_profile must be a RoutingProviderProfile"
        )
    _bounded_identifier(profile.provider_id, "provider_id")
    if not isinstance(profile.uses_external_services, bool):
        raise ParserRoutingValidationError(
            "uses_external_services must be Boolean"
        )
    _validate_tags(profile.security_tags, "provider profile security tags")
    for name, value in (
        ("provider_kind", profile.provider_kind),
        ("deployment_id", profile.deployment_id),
    ):
        if value is not None:
            _bounded_identifier(value, name)


def _call_proposal_provider(
    provider: RoutingProposalProvider, request: ParserRoutingRequest
) -> RoutingProposal:
    """Call one provider once and contain every provider-side failure.

    Hybrid and LLM-directed planning delegates only immutable request, registry,
    and boundary records. No source bytes or native payloads exist in those types.
    The return type remains untrusted and is checked before deterministic
    validation. Provider exceptions are replaced with a bounded typed error.
    """
    try:
        proposal = provider.propose(request, request.registry, request.boundary)
    except Exception as error:
        raise ParserRoutingProposalError(
            "Routing proposal provider failed"
        ) from error
    if not isinstance(proposal, RoutingProposal):
        raise ParserRoutingProposalError(
            "Routing proposal provider returned an invalid result type"
        )
    return proposal


def _safe_rejected_proposal(proposal: RoutingProposal) -> RoutingProposal:
    """Project malformed provider output into a safe rejected audit record.

    Service planning calls this only after deterministic schema rejection. Valid
    bounded metadata is retained, but malformed invocations, stop conditions,
    identifiers, tags, or external-service facts are omitted rather than persisted
    as an apparently valid plan. Rejection reasons remain in validation; nothing
    is selected or executed and no source payload is logged.
    """
    safe_invocations: tuple[ParserInvocation, ...] = ()
    try:
        identities: list[tuple[str, str]] = []
        for invocation in proposal.invocations:
            _validate_invocation(invocation)
            identities.append((invocation.parser_id, invocation.scope))
        _reject_duplicates(identities, "parser invocation")
        safe_invocations = proposal.invocations
    except ParserRoutingError:
        pass

    def safe_audit_text(value: object, context: str) -> str | None:
        """Return one valid bounded audit value or omit malformed provider text."""
        if value is None:
            return None
        try:
            return _bounded_audit_text(value, context)
        except ParserRoutingError:
            return None

    def safe_identifier(value: object, context: str) -> str | None:
        """Return one valid provider identifier or omit it from rejected output."""
        if value is None:
            return None
        try:
            return _bounded_identifier(value, context)
        except ParserRoutingError:
            return None

    stop_condition: str | None = None
    try:
        _validate_stop_condition(proposal.stop_condition)
        stop_condition = proposal.stop_condition
    except ParserRoutingError:
        pass
    security_tags: tuple[str, ...] = ()
    try:
        _validate_tags(proposal.security_tags, "proposal security tags")
        security_tags = proposal.security_tags
    except ParserRoutingError:
        pass
    return RoutingProposal(
        invocations=safe_invocations,
        reason=safe_audit_text(proposal.reason, "proposal reason"),
        stop_condition=stop_condition,
        provider=safe_identifier(proposal.provider, "provider"),
        model=safe_identifier(proposal.model, "model"),
        request_id=safe_identifier(proposal.request_id, "request_id"),
        external_services_used=(
            proposal.external_services_used
            if isinstance(proposal.external_services_used, bool)
            else False
        ),
        security_tags=security_tags,
    )


def _validate_request(request: ParserRoutingRequest) -> None:
    """Validate one service request and translate T03 registry failures.

    ``ParserRoutingService`` calls this before rules or providers. It checks exact
    mode, facts, hard boundary, registry type/version, and optional rules. The
    operation is read-only and deterministic; no provider or parser is invoked.
    """
    if not isinstance(request, ParserRoutingRequest):
        raise ParserRoutingValidationError(
            "Routing request must be a ParserRoutingRequest"
        )
    if request.mode not in ADAPTIVE_ROUTING_MODES:
        raise ParserRoutingValidationError(
            f"Unsupported adaptive routing mode: {request.mode}"
        )
    _validate_input_facts(request.input_facts)
    _validated_boundary(request.boundary)
    if not isinstance(request.registry, ParserCapabilityRegistry):
        raise ParserRoutingCapabilityError(
            "Routing request requires a ParserCapabilityRegistry"
        )
    try:
        request.registry.validate()
    except ParserCapabilityError as error:
        raise ParserRoutingCapabilityError(
            "Routing capability registry is invalid"
        ) from error
    if request.mode == "llm-directed":
        _required_text(request.registry.registry_version, "registry_version")
    if request.deterministic_rules is not None:
        _reject_duplicates(
            (rule.rule_id for rule in request.deterministic_rules), "rule ID"
        )
        for rule in request.deterministic_rules:
            _validate_rule(rule)


def _validate_input_facts(facts: RoutingInputFacts) -> None:
    """Validate bounded source metadata without accepting content or paths.

    Request and plan readers call this before rule evaluation. It requires a media
    type, finite optional ratio, nonnegative counts, explicit canonical routing
    vocabulary, and bounded path-free document-class metadata.
    """
    if not isinstance(facts, RoutingInputFacts):
        raise ParserRoutingValidationError(
            "input_facts must be a RoutingInputFacts record"
        )
    if not isinstance(facts.media_type, str) or not _MEDIA_TYPE_PATTERN.fullmatch(
        facts.media_type
    ):
        raise ParserRoutingValidationError("media_type must be a valid media type")
    if facts.native_text_ratio is not None:
        ratio = facts.native_text_ratio
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ParserRoutingValidationError(
                "native_text_ratio must be numeric or null"
            )
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise ParserRoutingValidationError(
                "native_text_ratio must be finite and between 0 and 1"
            )
    _validate_requirement_order(
        facts.required_capabilities, "required capabilities"
    )
    for name, value in (
        ("page_count", facts.page_count),
        ("source_size_bytes", facts.source_size_bytes),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ParserRoutingValidationError(
                f"{name} must be a nonnegative integer or null"
            )
    if facts.document_class is not None:
        _bounded_audit_text(facts.document_class, "document_class")


def _validated_boundary(boundary: RoutingBoundary) -> RoutingBoundary:
    """Validate and return the immutable hard boundary unchanged.

    Service and plan readers call this before any provider interaction. Parser
    allowlists, budgets, external-service permission, scopes, and security tags
    are checked deterministically. Returning the same record makes clear that a
    proposal cannot construct, widen, or mutate the boundary.
    """
    if not isinstance(boundary, RoutingBoundary):
        raise ParserRoutingValidationError(
            "deterministic boundary must be a RoutingBoundary"
        )
    if not boundary.allowlist:
        raise ParserRoutingValidationError("Routing allowlist must not be empty")
    for parser_id in boundary.allowlist:
        _validate_parser_id(parser_id)
    _require_lexical_order(boundary.allowlist, "routing allowlist")
    if (
        isinstance(boundary.max_parser_runs, bool)
        or not isinstance(boundary.max_parser_runs, int)
        or boundary.max_parser_runs < 1
    ):
        raise ParserRoutingValidationError(
            "max_parser_runs must be a positive integer"
        )
    if not isinstance(boundary.external_services_allowed, bool):
        raise ParserRoutingValidationError(
            "external_services_allowed must be Boolean"
        )
    _validate_scope_collection(boundary.allowed_scopes)
    _validate_tags(boundary.required_security_tags, "required security tags")
    return boundary


def _validate_rule(rule: DeterministicRoutingRule) -> None:
    """Validate one data-driven deterministic rule and its explicit vocabulary.

    Request validation uses this helper so custom rules cannot hide arbitrary
    predicates or prose interpretation. It checks identity, triggers, parser,
    scope, purpose, media types, and optional finite ratio without executing code.
    """
    if not isinstance(rule, DeterministicRoutingRule):
        raise ParserRoutingValidationError(
            "deterministic_rules must contain DeterministicRoutingRule records"
        )
    if not isinstance(rule.rule_id, str) or not _RULE_ID_PATTERN.fullmatch(
        rule.rule_id
    ):
        raise ParserRoutingValidationError("rule_id is malformed")
    if not rule.trigger_capabilities:
        raise ParserRoutingValidationError(
            f"Deterministic rule has no triggers: {rule.rule_id}"
        )
    _validate_requirement_order(
        rule.trigger_capabilities, f"rule triggers for {rule.rule_id}"
    )
    _validate_parser_id(rule.parser_id)
    _validate_scope(rule.scope)
    _validate_requirement_order(rule.purpose, f"rule purpose for {rule.rule_id}")
    if not rule.media_types:
        raise ParserRoutingValidationError(
            f"Deterministic rule has no media types: {rule.rule_id}"
        )
    _require_lexical_order(rule.media_types, f"rule media types for {rule.rule_id}")
    for media_type in rule.media_types:
        if not _MEDIA_TYPE_PATTERN.fullmatch(media_type):
            raise ParserRoutingValidationError(
                f"Deterministic rule media type is malformed: {rule.rule_id}"
            )
    if rule.minimum_native_text_ratio is not None:
        value = rule.minimum_native_text_ratio
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParserRoutingValidationError(
                f"Rule ratio must be numeric: {rule.rule_id}"
            )
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ParserRoutingValidationError(
                f"Rule ratio must be between 0 and 1: {rule.rule_id}"
            )


def _validate_proposal(
    request: ParserRoutingRequest,
    proposal: RoutingProposal,
    *,
    provider_profile: RoutingProviderProfile | None = None,
) -> RoutingValidationResult:
    """Apply schema, boundary, registry, runtime, and capability checks in order.

    All three modes use this deterministic algorithm after invocation selection.
    It treats proposals as untrusted, attributes every purpose to the same
    invocation's parser, requires explicit purpose coverage for every input need,
    and accumulates bounded reasons in fixed stage order. Proposal-backed modes
    derive security only from the trusted provider profile; proposal claims remain
    audit facts and cannot authorize service use or tags. It never mutates registry
    evidence or resolves conflicts. No provider, parser, stop-condition execution,
    or output fusion occurs here.
    """
    reasons: list[str] = []
    schema_valid = _validate_proposal_schema(request.mode, proposal, reasons)
    scope_boundary_valid = all(
        item.scope in request.boundary.allowed_scopes
        for item in proposal.invocations
        if isinstance(item, ParserInvocation)
    )
    if not scope_boundary_valid:
        reasons.append("scope-outside-boundary")
        schema_valid = False
    if request.mode == "hybrid":
        proposed_ids = tuple(
            item.parser_id
            for item in proposal.invocations
            if isinstance(item, ParserInvocation)
        )
        expected_ids = tuple(
            parser_id
            for parser_id in request.boundary.allowlist
            if parser_id in proposed_ids
        )
        if proposed_ids != expected_ids:
            reasons.append("hybrid-invocation-order-invalid")
            schema_valid = False
    allowlist_valid = all(
        item.parser_id in request.boundary.allowlist
        for item in proposal.invocations
        if isinstance(item, ParserInvocation)
    )
    if not allowlist_valid:
        reasons.append("parser-outside-allowlist")
    budget_valid = _validate_budget(
        proposal.invocations, request.boundary.max_parser_runs
    )
    if not budget_valid:
        reasons.append("parser-run-budget-exceeded")
    if request.mode == "deterministic":
        security_valid = not (
            proposal.external_services_used
            and not request.boundary.external_services_allowed
        ) and set(request.boundary.required_security_tags).issubset(
            proposal.security_tags
        )
    else:
        security_valid = provider_profile is not None and not (
            provider_profile.uses_external_services
            and not request.boundary.external_services_allowed
        ) and set(request.boundary.required_security_tags).issubset(
            provider_profile.security_tags if provider_profile is not None else ()
        )
    if not security_valid:
        reasons.append("security-boundary-violated")

    registry_valid = True
    runtime_valid = True
    records: dict[str, ParserCapabilityRecord] = {}
    for invocation in proposal.invocations:
        if not isinstance(invocation, ParserInvocation):
            registry_valid = False
            runtime_valid = False
            continue
        try:
            record = request.registry.get(invocation.parser_id)
        except ParserCapabilityError:
            registry_valid = False
            runtime_valid = False
            continue
        records[invocation.parser_id] = record
        if record.parser_discovered.runtime_probe.runtime_available is not True:
            runtime_valid = False
    if not registry_valid:
        reasons.append("parser-not-in-registry")
    if not runtime_valid:
        reasons.append("parser-not-runtime-available")

    valid_invocations = tuple(
        item for item in proposal.invocations if isinstance(item, ParserInvocation)
    )
    invocation_purposes_valid = _invocation_purposes_supported(
        valid_invocations,
        records,
    )
    if not invocation_purposes_valid:
        reasons.append("invocation-purpose-unsupported")
    required_purposes_valid = _required_purposes_satisfied(
        request.input_facts.required_capabilities,
        valid_invocations,
        records,
    )
    if not required_purposes_valid:
        reasons.append("required-purpose-unresolved")
    capability_valid = invocation_purposes_valid and required_purposes_valid

    accepted = all(
        (
            allowlist_valid,
            budget_valid,
            security_valid,
            schema_valid,
            registry_valid,
            runtime_valid,
            capability_valid,
        )
    )
    return RoutingValidationResult(
        accepted=accepted,
        allowlist_valid=allowlist_valid,
        budget_valid=budget_valid,
        security_valid=security_valid,
        schema_valid=schema_valid,
        registry_valid=registry_valid,
        runtime_valid=runtime_valid,
        capability_valid=capability_valid,
        rejection_reasons=tuple(dict.fromkeys(reasons)),
    )


def _validate_proposal_schema(
    mode: str, proposal: RoutingProposal, reasons: list[str]
) -> bool:
    """Validate proposal records, invocation order, scopes, and stop conditions.

    The common proposal validator calls this first. Hybrid invocation order must
    follow its deterministic boundary and is checked separately; LLM-directed
    order remains provider-controlled. This helper validates record shape and
    uniqueness without looking up registry facts or executing stop conditions.
    """
    if not isinstance(proposal, RoutingProposal):
        reasons.append("proposal-shape-invalid")
        return False
    valid = True
    if not isinstance(proposal.external_services_used, bool):
        reasons.append("external-service-fact-invalid")
        valid = False
    try:
        _validate_tags(proposal.security_tags, "proposal security tags")
        if proposal.reason is not None:
            _bounded_audit_text(proposal.reason, "proposal reason")
        for name, value in (
            ("provider", proposal.provider),
            ("model", proposal.model),
            ("request_id", proposal.request_id),
        ):
            if value is not None:
                _bounded_identifier(value, name)
        _validate_stop_condition(proposal.stop_condition)
        identities: list[tuple[str, str]] = []
        for invocation in proposal.invocations:
            _validate_invocation(invocation)
            identities.append((invocation.parser_id, invocation.scope))
        _reject_duplicates(identities, "parser invocation")
    except ParserRoutingError:
        reasons.append("proposal-shape-invalid")
        valid = False
    if mode == "deterministic" and proposal.stop_condition is not None:
        reasons.append("deterministic-stop-condition-invalid")
        valid = False
    return valid


def _validate_budget(
    invocations: tuple[ParserInvocation, ...], max_parser_runs: int
) -> bool:
    """Return whether the immutable invocation count fits the hard run budget.

    Proposal validation calls this after shape inspection. Each invocation counts
    once regardless of parser or scope; no deduplication or execution is attempted,
    so duplicate records cannot be hidden to fit the budget.
    """
    return len(invocations) <= max_parser_runs


def _invocation_purposes_supported(
    invocations: tuple[ParserInvocation, ...],
    records: Mapping[str, ParserCapabilityRecord],
) -> bool:
    """Verify every declared purpose against that invocation's own parser.

    Common live-plan validation uses this per-invocation attribution check after
    registry lookup. Each purpose maps through reviewed routing policy and must
    be supported by the same runtime-available parser record; capability support
    from a different selected parser cannot rescue a swapped or fabricated
    purpose. Empty purposes are allowed only when request coverage does not need
    them, which the separate required-purpose check decides.
    """
    for invocation in invocations:
        record = records.get(invocation.parser_id)
        if record is None:
            if invocation.purpose:
                return False
            continue
        for purpose in invocation.purpose:
            capability = _REQUIREMENT_TO_CAPABILITY.get(purpose)
            if capability is None or not _parser_is_capability_eligible(
                record, capability
            ):
                return False
    return True


def _required_purposes_satisfied(
    requirements: tuple[str, ...],
    invocations: tuple[ParserInvocation, ...],
    records: Mapping[str, ParserCapabilityRecord],
) -> bool:
    """Require each input need to be attributed to an eligible invocation.

    Common live-plan validation calls this after parser-specific purpose checks.
    Every requested routing term must appear in at least one invocation purpose,
    and that same invocation's runtime-available parser must carry an eligible
    T03 assertion. Empty-purpose proposals therefore cannot satisfy a nonempty
    request, and set-wide capability support cannot hide incorrect attribution.
    Registry records and conflicts remain unchanged.
    """
    for requirement in requirements:
        capability = _REQUIREMENT_TO_CAPABILITY.get(requirement)
        if capability is None:
            return False
        satisfied = False
        for invocation in invocations:
            if requirement not in invocation.purpose:
                continue
            record = records.get(invocation.parser_id)
            if record is None or not _parser_is_capability_eligible(
                record, capability
            ):
                continue
            satisfied = True
            break
        if not satisfied:
            return False
    return True


def _parser_is_capability_eligible(
    record: ParserCapabilityRecord, capability: str
) -> bool:
    """Apply the explicit runtime and assertion-status eligibility invariant.

    Requirement validation calls this for one original registry record. Runtime
    availability must be exactly true and the named assertion must be available,
    declared, or declared-when-available. Existing conflicts remain untouched;
    known unavailable runtime always wins over advertised capability evidence.
    """
    if record.parser_discovered.runtime_probe.runtime_available is not True:
        return False
    return any(
        item.capability == capability
        and item.status in _ELIGIBLE_CAPABILITY_STATUSES
        for item in record.parser_discovered.capabilities
    )


def _registry_sha256(registry: ParserCapabilityRegistry) -> str:
    """Bind a live routing decision to the exact deterministic T03 snapshot.

    Service builders call this after registry validation. SHA-256 is computed
    over the registry's canonical ``to_json_bytes`` output, so consumers can
    distinguish two evidence snapshots that happen to share a human-readable
    version. The helper performs no persistence, network access, or registry
    mutation and returns a lowercase hexadecimal digest.
    """
    return hashlib.sha256(registry.to_json_bytes()).hexdigest()


def _validate_plan(plan: RoutingPlan) -> None:
    """Validate one typed aggregate and its exact mode-specific combination.

    Public ``RoutingPlan.validate`` delegates here. The helper checks schema,
    mode, nested records, ordering, validation consistency, registry binding, and
    forbidden cross-mode fields without serializing, normalizing, or executing.
    """
    if plan.schema != ROUTING_PLAN_SCHEMA:
        raise ParserRoutingValidationError(
            f"Unsupported routing plan schema: {plan.schema}"
        )
    if plan.mode not in ADAPTIVE_ROUTING_MODES:
        raise ParserRoutingValidationError(
            f"Unsupported adaptive routing mode: {plan.mode}"
        )
    if not isinstance(plan.llm_used, bool):
        raise ParserRoutingValidationError("llm_used must be Boolean")
    if plan.registry_version is not None:
        _bounded_identifier(plan.registry_version, "registry_version")
    if plan.registry_sha256 is not None:
        _validate_sha256(plan.registry_sha256, "registry_sha256")
    _validate_validation_result(plan.validation_result)
    for invocation in plan.selected_invocations:
        _validate_invocation(invocation)
    _reject_duplicates(
        (
            (invocation.parser_id, invocation.scope)
            for invocation in plan.selected_invocations
        ),
        "parser invocation",
    )
    for invocation in plan.candidate_invocations:
        _validate_invocation(invocation)
    _reject_duplicates(
        (
            (invocation.parser_id, invocation.scope)
            for invocation in plan.candidate_invocations
        ),
        "candidate parser invocation",
    )
    if not plan.validation_result.accepted and plan.selected_invocations:
        raise ParserRoutingValidationError(
            "Rejected routing plan cannot contain selected invocations"
        )
    if plan.mode in {"deterministic", "hybrid"}:
        parser_ids = tuple(item.parser_id for item in plan.selected_invocations)
        _require_lexical_order(parser_ids, f"{plan.mode} invocation parser IDs")
    if plan.mode == "deterministic":
        candidate_ids = tuple(
            item.parser_id for item in plan.candidate_invocations
        )
        _require_lexical_order(
            candidate_ids, "deterministic candidate invocation parser IDs"
        )
    if plan.input_facts is not None:
        _validate_input_facts(plan.input_facts)
    if plan.boundary is not None:
        _validated_boundary(plan.boundary)
    if plan.provider_profile is not None:
        _validate_provider_profile(plan.provider_profile)
    if plan.proposal is not None:
        reasons: list[str] = []
        if not _validate_proposal_schema(plan.mode, plan.proposal, reasons):
            raise ParserRoutingValidationError("Routing plan proposal is invalid")
        if (
            plan.validation_result.accepted
            and plan.proposal.invocations != plan.selected_invocations
        ):
            raise ParserRoutingValidationError(
                "Routing plan selected invocations differ from proposal"
            )
    _reject_duplicates(plan.rules_evaluated, "evaluated rule ID")
    for rule_id in plan.rules_evaluated:
        if not isinstance(rule_id, str) or not _RULE_ID_PATTERN.fullmatch(rule_id):
            raise ParserRoutingValidationError("Evaluated rule ID is malformed")
    if plan._compact_fixture:
        if (
            plan.registry_sha256 is not None
            or plan.candidate_invocations
            or plan.provider_profile is not None
        ):
            raise ParserRoutingValidationError(
                "Compact routing plan contains canonical-only fields"
            )
        if plan.mode == "deterministic":
            if (
                plan.input_facts is None
                or plan.llm_used
                or plan.boundary is not None
                or plan.proposal is not None
                or plan.registry_version is not None
            ):
                raise ParserRoutingValidationError(
                    "Compact deterministic plan has invalid mode-specific fields"
                )
        elif plan.mode == "hybrid":
            if (
                plan.boundary is None
                or plan.proposal is None
                or not plan.llm_used
                or plan.input_facts is not None
                or plan.registry_version is not None
                or plan.rules_evaluated
            ):
                raise ParserRoutingValidationError(
                    "Compact hybrid plan has invalid mode-specific fields"
                )
        elif (
            plan.registry_version is None
            or plan.proposal is None
            or not plan.llm_used
            or plan.input_facts is not None
            or plan.boundary is not None
            or plan.rules_evaluated
        ):
            raise ParserRoutingValidationError(
                "Compact LLM-directed plan has invalid mode-specific fields"
            )
    elif plan.mode == "deterministic":
        if (
            plan.input_facts is None
            or plan.boundary is None
            or plan.registry_version is None
            or plan.registry_sha256 is None
            or plan.llm_used
            or plan.proposal is not None
            or plan.provider_profile is not None
        ):
            raise ParserRoutingValidationError(
                "Canonical deterministic plan lacks exact validation context"
            )
        if plan.validation_result.accepted and (
            plan.selected_invocations != plan.candidate_invocations
        ):
            raise ParserRoutingValidationError(
                "Accepted deterministic selections differ from candidates"
            )
    elif plan.mode == "hybrid":
        if (
            plan.input_facts is None
            or plan.boundary is None
            or plan.proposal is None
            or plan.provider_profile is None
            or plan.registry_version is None
            or plan.registry_sha256 is None
            or not plan.llm_used
            or plan.candidate_invocations
            or plan.rules_evaluated
        ):
            raise ParserRoutingValidationError(
                "Canonical hybrid plan lacks exact validation context"
            )
        trusted_security_valid = not (
            plan.provider_profile.uses_external_services
            and not plan.boundary.external_services_allowed
        ) and set(plan.boundary.required_security_tags).issubset(
            plan.provider_profile.security_tags
        )
        if plan.validation_result.security_valid is not trusted_security_valid:
            raise ParserRoutingValidationError(
                "Hybrid security result conflicts with trusted provider profile"
            )
        expected_ids = tuple(
            parser_id
            for parser_id in plan.boundary.allowlist
            if parser_id
            in {item.parser_id for item in plan.selected_invocations}
        )
        actual_ids = tuple(item.parser_id for item in plan.selected_invocations)
        if actual_ids != expected_ids:
            raise ParserRoutingValidationError(
                "Hybrid invocations do not follow deterministic boundary order"
            )
    else:
        if (
            plan.input_facts is None
            or plan.boundary is None
            or plan.proposal is None
            or plan.provider_profile is None
            or plan.registry_version is None
            or plan.registry_sha256 is None
            or not plan.llm_used
            or plan.candidate_invocations
            or plan.rules_evaluated
        ):
            raise ParserRoutingValidationError(
                "Canonical LLM-directed plan lacks exact validation context"
            )
        trusted_security_valid = not (
            plan.provider_profile.uses_external_services
            and not plan.boundary.external_services_allowed
        ) and set(plan.boundary.required_security_tags).issubset(
            plan.provider_profile.security_tags
        )
        if plan.validation_result.security_valid is not trusted_security_valid:
            raise ParserRoutingValidationError(
                "LLM-directed security result conflicts with trusted provider profile"
            )


def _validate_validation_result(result: RoutingValidationResult) -> None:
    """Require Boolean checks and acceptance consistency without repair.

    Plan validation calls this for persisted and runtime-built results. Acceptance
    must equal all seven hard checks, and reasons must be unique, bounded, and
    absent for accepted plans. The helper does not infer missing evidence.
    """
    if not isinstance(result, RoutingValidationResult):
        raise ParserRoutingValidationError(
            "validation_result must be a RoutingValidationResult"
        )
    flags = (
        result.allowlist_valid,
        result.budget_valid,
        result.security_valid,
        result.schema_valid,
        result.registry_valid,
        result.runtime_valid,
        result.capability_valid,
    )
    if not isinstance(result.accepted, bool) or not all(
        isinstance(item, bool) for item in flags
    ):
        raise ParserRoutingValidationError("Validation result flags must be Boolean")
    if result.accepted is not all(flags):
        raise ParserRoutingValidationError(
            "Validation result acceptance conflicts with validation flags"
        )
    _reject_duplicates(result.rejection_reasons, "rejection reason")
    for reason in result.rejection_reasons:
        if not isinstance(reason, str) or not _RULE_ID_PATTERN.fullmatch(reason):
            raise ParserRoutingValidationError("Rejection reason is malformed")
    if result.accepted and result.rejection_reasons:
        raise ParserRoutingValidationError(
            "Accepted validation result cannot contain rejection reasons"
        )
    if not result.accepted and not result.rejection_reasons:
        raise ParserRoutingValidationError(
            "Rejected validation result requires a rejection reason"
        )


def _validate_invocation(invocation: ParserInvocation) -> None:
    """Validate parser identity, scope, purpose vocabulary, and security tags.

    Plan and proposal validation use this helper before registry lookup. It keeps
    page-scoped intent explicit, rejects copied-text-shaped free fields by schema,
    and performs no parser capability inference or execution.
    """
    if not isinstance(invocation, ParserInvocation):
        raise ParserRoutingValidationError(
            "invocations must contain ParserInvocation records"
        )
    _validate_parser_id(invocation.parser_id)
    _validate_scope(invocation.scope)
    _validate_requirement_order(invocation.purpose, "invocation purpose")
    _validate_tags(invocation.security_tags, "invocation security tags")


def _validate_scope_collection(scopes: tuple[str, ...]) -> None:
    """Require nonempty unique scopes in the explicit supported-scope order.

    Boundary validation calls this helper. It recognizes document and future
    native-link-page planning scopes only; recognizing a scope never causes a
    parser to execute at that granularity.
    """
    if not scopes:
        raise ParserRoutingValidationError("allowed_scopes must not be empty")
    for scope in scopes:
        _validate_scope(scope)
    _reject_duplicates(scopes, "allowed scope")
    expected = tuple(scope for scope in _SUPPORTED_SCOPES if scope in scopes)
    if scopes != expected:
        raise ParserRoutingValidationError(
            "allowed_scopes are not deterministically ordered"
        )


def _validate_scope(scope: str) -> None:
    """Require one explicitly supported planning scope without executing it."""
    if scope not in _SUPPORTED_SCOPES:
        raise ParserRoutingValidationError(f"Unsupported parser scope: {scope}")


def _validate_stop_condition(value: str | None) -> None:
    """Validate the frozen stop-condition vocabulary without evaluating output.

    Proposal and plan readers call this shape check. T04 records the approved
    condition but never inspects parser observations to decide whether it fired;
    that orchestration belongs to T05 or later.
    """
    if value is not None and value not in _SUPPORTED_STOP_CONDITIONS:
        raise ParserRoutingValidationError(
            f"Unsupported routing stop condition: {value}"
        )


def _validate_requirement_order(values: tuple[str, ...], context: str) -> None:
    """Require unique approved requirements in contractual routing-policy order.

    Facts, purposes, and deterministic rules use a reviewed policy vocabulary,
    not alphabetical guessing or human prose. The supplied tuple is compared
    without normalization so malformed persisted order remains visible.
    """
    if not isinstance(values, tuple):
        raise ParserRoutingValidationError(f"{context} must be an immutable tuple")
    unknown = [item for item in values if item not in _REQUIREMENT_TO_CAPABILITY]
    if unknown:
        raise ParserRoutingCapabilityError(
            f"Unsupported routing requirement: {_bounded_label(unknown[0])}"
        )
    _reject_duplicates(values, context)
    expected = tuple(item for item in _ROUTING_REQUIREMENTS if item in values)
    if values != expected:
        raise ParserRoutingValidationError(
            f"{context} are not deterministically ordered"
        )


def _validate_tags(values: tuple[str, ...], context: str) -> None:
    """Require unique lexical security tags with bounded identifier syntax."""
    if not isinstance(values, tuple):
        raise ParserRoutingValidationError(f"{context} must be an immutable tuple")
    for value in values:
        _bounded_identifier(value, context)
    _require_lexical_order(values, context)


def _parse_deterministic_plan(value: Mapping[str, object]) -> RoutingPlan:
    """Parse the frozen deterministic shape or its canonical runtime extension.

    ``RoutingPlan.from_dict`` delegates here after reading mode. Required fixture
    fields remain unchanged. A service-built record must instead contain every
    canonical context field, including boundary, candidates, validation, version,
    and exact registry digest. Partial extensions fail and every array retains
    supplied order.
    """
    compact_fields = {
        "schema",
        "mode",
        "input_facts",
        "rules_evaluated",
        "selected_invocations",
        "llm_used",
    }
    canonical_fields = compact_fields | {
        "candidate_invocations",
        "deterministic_boundary",
        "registry_sha256",
        "registry_version",
        "validation_result",
    }
    keys = set(value)
    if keys == compact_fields:
        compact = True
    elif keys == canonical_fields:
        compact = False
    else:
        raise ParserRoutingValidationError(
            "Deterministic routing plan fields must use an exact compact or canonical shape"
        )
    facts = _parse_input_facts(_mapping(value["input_facts"], "input_facts"))
    invocations = tuple(
        _parse_invocation(item, purpose_required=True)
        for item in _mapping_sequence(
            value["selected_invocations"], "selected_invocations"
        )
    )
    validation = (
        _accepted_validation_result()
        if compact
        else _parse_validation_result(
            _mapping(value["validation_result"], "validation_result")
        )
    )
    candidates = (
        ()
        if compact
        else tuple(
            _parse_invocation(item, purpose_required=True)
            for item in _mapping_sequence(
                value["candidate_invocations"], "candidate_invocations"
            )
        )
    )
    return RoutingPlan(
        schema=_required_text(value["schema"], "schema"),
        mode="deterministic",
        selected_invocations=invocations,
        validation_result=validation,
        registry_version=_optional_text(
            value.get("registry_version"), "registry_version"
        ),
        registry_sha256=(
            None
            if compact
            else _validate_sha256(value["registry_sha256"], "registry_sha256")
        ),
        llm_used=_required_bool(value["llm_used"], "llm_used"),
        input_facts=facts,
        boundary=(
            None
            if compact
            else _parse_boundary(
                _mapping(
                    value["deterministic_boundary"],
                    "deterministic_boundary",
                )
            )
        ),
        rules_evaluated=_string_tuple(
            value["rules_evaluated"], "rules_evaluated"
        ),
        candidate_invocations=candidates,
        _compact_fixture=compact,
    )


def _parse_hybrid_plan(value: Mapping[str, object]) -> RoutingPlan:
    """Parse the frozen hybrid boundary/proposal/result shape without repair.

    The compact fixture stores selected parser IDs and a result string. Canonical
    service output must additionally store input facts, registry version, exact
    registry digest, and full validation metadata. Partial forms are rejected;
    both exact forms become shared immutable records and retain list order.
    """
    compact_fields = {
        "schema",
        "mode",
        "deterministic_boundary",
        "frozen_llm_proposal",
        "validation_result",
    }
    canonical_fields = compact_fields | {
        "input_facts",
        "provider_profile",
        "registry_sha256",
        "registry_version",
    }
    keys = set(value)
    compact = keys == compact_fields and isinstance(
        value.get("validation_result"), str
    )
    canonical = keys == canonical_fields and isinstance(
        value.get("validation_result"), Mapping
    )
    if not compact and not canonical:
        raise ParserRoutingValidationError(
            "Hybrid routing plan fields must use an exact compact or canonical shape"
        )
    boundary = _parse_boundary(
        _mapping(value["deterministic_boundary"], "deterministic_boundary")
    )
    proposal_value = _mapping(value["frozen_llm_proposal"], "frozen_llm_proposal")
    proposal = _parse_hybrid_proposal(proposal_value, compact=compact)
    if compact:
        validation = _parse_compact_result(value["validation_result"])
    else:
        validation = _parse_validation_result(
            _mapping(value["validation_result"], "validation_result")
        )
    return RoutingPlan(
        schema=_required_text(value["schema"], "schema"),
        mode="hybrid",
        selected_invocations=(proposal.invocations if validation.accepted else ()),
        validation_result=validation,
        registry_version=_optional_text(
            value.get("registry_version"), "registry_version"
        ),
        registry_sha256=(
            None
            if compact
            else _validate_sha256(value["registry_sha256"], "registry_sha256")
        ),
        llm_used=True,
        input_facts=(
            None
            if compact
            else _parse_input_facts(
                _mapping(value["input_facts"], "input_facts")
            )
        ),
        boundary=boundary,
        proposal=proposal,
        provider_profile=(
            None
            if compact
            else _parse_provider_profile(
                _mapping(value["provider_profile"], "provider_profile")
            )
        ),
        _compact_fixture=compact,
    )


def _parse_llm_directed_plan(value: Mapping[str, object]) -> RoutingPlan:
    """Parse the frozen registry-bound LLM proposal and deterministic decision.

    The fixture's four validation flags are expanded into the shared result while
    canonical plans must persist every result field plus input facts, hard
    boundary, and exact registry digest. Proposal invocation order is preserved
    because LLM-directed order is an auditable provider decision.
    """
    compact_fields = {
        "schema",
        "mode",
        "registry_version",
        "frozen_llm_proposal",
        "deterministic_validation",
        "result",
    }
    canonical_fields = compact_fields | {
        "deterministic_boundary",
        "input_facts",
        "provider_profile",
        "registry_sha256",
    }
    keys = set(value)
    if keys == compact_fields:
        compact_shape = True
    elif keys == canonical_fields:
        compact_shape = False
    else:
        raise ParserRoutingValidationError(
            "LLM-directed routing plan fields must use an exact compact or canonical shape"
        )
    proposal = _parse_llm_proposal(
        _mapping(value["frozen_llm_proposal"], "frozen_llm_proposal")
    )
    validation_mapping = _mapping(
        value["deterministic_validation"], "deterministic_validation"
    )
    compact_validation = set(validation_mapping) == {
        "allowlist_valid",
        "budget_valid",
        "security_valid",
        "schema_valid",
    }
    if compact_shape is not compact_validation:
        raise ParserRoutingValidationError(
            "LLM-directed validation does not match its persisted plan shape"
        )
    validation = (
        _parse_compact_validation(validation_mapping, value["result"])
        if compact_shape
        else _parse_validation_result(validation_mapping)
    )
    accepted_text = _required_text(value["result"], "result")
    if accepted_text not in {"accepted", "rejected"}:
        raise ParserRoutingValidationError("result must be accepted or rejected")
    if validation.accepted is not (accepted_text == "accepted"):
        raise ParserRoutingValidationError(
            "result conflicts with deterministic validation"
        )
    return RoutingPlan(
        schema=_required_text(value["schema"], "schema"),
        mode="llm-directed",
        selected_invocations=(proposal.invocations if validation.accepted else ()),
        validation_result=validation,
        registry_version=_required_text(
            value["registry_version"], "registry_version"
        ),
        registry_sha256=(
            None
            if compact_shape
            else _validate_sha256(value["registry_sha256"], "registry_sha256")
        ),
        llm_used=True,
        input_facts=(
            None
            if compact_shape
            else _parse_input_facts(
                _mapping(value["input_facts"], "input_facts")
            )
        ),
        boundary=(
            None
            if compact_shape
            else _parse_boundary(
                _mapping(
                    value["deterministic_boundary"],
                    "deterministic_boundary",
                )
            )
        ),
        proposal=proposal,
        provider_profile=(
            None
            if compact_shape
            else _parse_provider_profile(
                _mapping(value["provider_profile"], "provider_profile")
            )
        ),
        _compact_fixture=compact_shape,
    )


def _parse_input_facts(value: Mapping[str, object]) -> RoutingInputFacts:
    """Parse deterministic input metadata while rejecting payload-shaped fields."""
    required = {"media_type", "native_text_ratio", "requires"}
    optional = {"page_count", "source_size_bytes", "document_class"}
    _require_fields(value, required, optional, "routing input facts")
    ratio = value["native_text_ratio"]
    if ratio is not None and (
        isinstance(ratio, bool) or not isinstance(ratio, (int, float))
    ):
        raise ParserRoutingValidationError(
            "native_text_ratio must be numeric or null"
        )
    return RoutingInputFacts(
        media_type=_required_text(value["media_type"], "media_type"),
        native_text_ratio=float(ratio) if ratio is not None else None,
        required_capabilities=_string_tuple(value["requires"], "requires"),
        page_count=_optional_nonnegative_int(value.get("page_count"), "page_count"),
        source_size_bytes=_optional_nonnegative_int(
            value.get("source_size_bytes"), "source_size_bytes"
        ),
        document_class=_optional_text(
            value.get("document_class"), "document_class"
        ),
    )


def _parse_boundary(value: Mapping[str, object]) -> RoutingBoundary:
    """Parse a hard boundary, applying defaults only for fixture-absent fields."""
    required = {"allowlist", "external_services_allowed", "max_parser_runs"}
    optional = {"allowed_scopes", "required_security_tags"}
    _require_fields(value, required, optional, "deterministic routing boundary")
    return RoutingBoundary(
        allowlist=_string_tuple(value["allowlist"], "allowlist"),
        max_parser_runs=_required_int(value["max_parser_runs"], "max_parser_runs"),
        external_services_allowed=_required_bool(
            value["external_services_allowed"], "external_services_allowed"
        ),
        allowed_scopes=(
            _string_tuple(value["allowed_scopes"], "allowed_scopes")
            if "allowed_scopes" in value
            else _SUPPORTED_SCOPES
        ),
        required_security_tags=(
            _string_tuple(
                value["required_security_tags"], "required_security_tags"
            )
            if "required_security_tags" in value
            else ()
        ),
    )


def _parse_provider_profile(
    value: Mapping[str, object],
) -> RoutingProviderProfile:
    """Parse one exact trusted provider profile without proposal substitution.

    Canonical hybrid and LLM-directed readers call this strict compatibility
    boundary. Required security facts cannot be omitted, unknown fields cannot
    hide provider behavior, and optional deployment labels remain bounded audit
    identifiers. The returned immutable record is validated with the same helper
    used by pre-call application composition.
    """
    required = {"provider_id", "security_tags", "uses_external_services"}
    optional = {"deployment_id", "provider_kind"}
    _require_fields(value, required, optional, "routing provider profile")
    profile = RoutingProviderProfile(
        provider_id=_required_text(value["provider_id"], "provider_id"),
        uses_external_services=_required_bool(
            value["uses_external_services"], "uses_external_services"
        ),
        security_tags=_string_tuple(value["security_tags"], "security_tags"),
        provider_kind=_optional_text(
            value.get("provider_kind"), "provider_kind"
        ),
        deployment_id=_optional_text(
            value.get("deployment_id"), "deployment_id"
        ),
    )
    _validate_provider_profile(profile)
    return profile


def _parse_invocation(
    value: Mapping[str, object], *, purpose_required: bool
) -> ParserInvocation:
    """Parse one invocation with strict fields and preserved collection order."""
    required = {"parser_id", "scope"}
    if purpose_required:
        required.add("purpose")
    optional = {"security_tags"}
    if not purpose_required:
        optional.add("purpose")
    _require_fields(value, required, optional, "parser invocation")
    return ParserInvocation(
        parser_id=_required_text(value["parser_id"], "parser_id"),
        scope=_required_text(value["scope"], "scope"),
        purpose=(
            _string_tuple(value["purpose"], "purpose")
            if "purpose" in value
            else ()
        ),
        security_tags=(
            _string_tuple(value["security_tags"], "security_tags")
            if "security_tags" in value
            else ()
        ),
    )


def _parse_hybrid_proposal(
    value: Mapping[str, object], *, compact: bool
) -> RoutingProposal:
    """Parse compact selected IDs or canonical hybrid invocation records."""
    if compact:
        required = {"selected", "reason"}
        optional = {
            "stop_condition",
            "provider",
            "model",
            "request_id",
            "external_services_used",
            "security_tags",
        }
        _require_fields(value, required, optional, "hybrid routing proposal")
        invocations = tuple(
            ParserInvocation(parser_id=item, scope="document")
            for item in _string_tuple(value["selected"], "selected")
        )
    else:
        required = {"invocations"}
        optional = {
            "reason",
            "stop_condition",
            "provider",
            "model",
            "request_id",
            "external_services_used",
            "security_tags",
        }
        _require_fields(value, required, optional, "hybrid routing proposal")
        invocations = tuple(
            _parse_invocation(item, purpose_required=False)
            for item in _mapping_sequence(value["invocations"], "invocations")
        )
    return RoutingProposal(
        invocations=invocations,
        reason=_optional_text(value.get("reason"), "reason"),
        stop_condition=_optional_text(
            value.get("stop_condition"), "stop_condition"
        ),
        provider=_optional_text(value.get("provider"), "provider"),
        model=_optional_text(value.get("model"), "model"),
        request_id=_optional_text(value.get("request_id"), "request_id"),
        external_services_used=_optional_bool_default_false(
            value.get("external_services_used"), "external_services_used"
        ),
        security_tags=(
            _string_tuple(value["security_tags"], "security_tags")
            if "security_tags" in value
            else ()
        ),
    )


def _parse_llm_proposal(value: Mapping[str, object]) -> RoutingProposal:
    """Parse one LLM-directed proposal without trusting or reordering it."""
    required = {"invocations"}
    optional = {
        "stop_condition",
        "reason",
        "provider",
        "model",
        "request_id",
        "external_services_used",
        "security_tags",
    }
    _require_fields(value, required, optional, "LLM-directed routing proposal")
    return RoutingProposal(
        invocations=tuple(
            _parse_invocation(item, purpose_required=False)
            for item in _mapping_sequence(value["invocations"], "invocations")
        ),
        reason=_optional_text(value.get("reason"), "reason"),
        stop_condition=_optional_text(
            value.get("stop_condition"), "stop_condition"
        ),
        provider=_optional_text(value.get("provider"), "provider"),
        model=_optional_text(value.get("model"), "model"),
        request_id=_optional_text(value.get("request_id"), "request_id"),
        external_services_used=_optional_bool_default_false(
            value.get("external_services_used"), "external_services_used"
        ),
        security_tags=(
            _string_tuple(value["security_tags"], "security_tags")
            if "security_tags" in value
            else ()
        ),
    )


def _parse_validation_result(
    value: Mapping[str, object]
) -> RoutingValidationResult:
    """Parse the full deterministic validation record with exact fields."""
    fields = {
        "accepted",
        "allowlist_valid",
        "budget_valid",
        "security_valid",
        "schema_valid",
        "registry_valid",
        "runtime_valid",
        "capability_valid",
        "rejection_reasons",
    }
    _require_fields(value, fields, set(), "routing validation result")
    return RoutingValidationResult(
        accepted=_required_bool(value["accepted"], "accepted"),
        allowlist_valid=_required_bool(
            value["allowlist_valid"], "allowlist_valid"
        ),
        budget_valid=_required_bool(value["budget_valid"], "budget_valid"),
        security_valid=_required_bool(
            value["security_valid"], "security_valid"
        ),
        schema_valid=_required_bool(value["schema_valid"], "schema_valid"),
        registry_valid=_required_bool(
            value["registry_valid"], "registry_valid"
        ),
        runtime_valid=_required_bool(value["runtime_valid"], "runtime_valid"),
        capability_valid=_required_bool(
            value["capability_valid"], "capability_valid"
        ),
        rejection_reasons=_string_tuple(
            value["rejection_reasons"], "rejection_reasons"
        ),
    )


def _parse_compact_validation(
    value: Mapping[str, object], result: object
) -> RoutingValidationResult:
    """Expand the four frozen LLM validation flags without inventing evidence."""
    fields = {
        "allowlist_valid",
        "budget_valid",
        "security_valid",
        "schema_valid",
    }
    _require_fields(value, fields, set(), "deterministic validation")
    accepted_text = _required_text(result, "result")
    accepted = accepted_text == "accepted"
    if accepted_text not in {"accepted", "rejected"}:
        raise ParserRoutingValidationError("result must be accepted or rejected")
    flags = {
        name: _required_bool(value[name], name)
        for name in fields
    }
    if accepted and not all(flags.values()):
        raise ParserRoutingValidationError(
            "Accepted result conflicts with deterministic validation"
        )
    inferred = accepted
    reasons = () if accepted else ("frozen-plan-rejected",)
    return RoutingValidationResult(
        accepted=accepted,
        allowlist_valid=flags["allowlist_valid"],
        budget_valid=flags["budget_valid"],
        security_valid=flags["security_valid"],
        schema_valid=flags["schema_valid"],
        registry_valid=inferred,
        runtime_valid=inferred,
        capability_valid=inferred,
        rejection_reasons=reasons,
    )


def _parse_compact_result(value: object) -> RoutingValidationResult:
    """Expand a frozen hybrid accepted/rejected string into typed validation."""
    result = _required_text(value, "validation_result")
    if result == "accepted":
        return _accepted_validation_result()
    if result != "rejected":
        raise ParserRoutingValidationError(
            "validation_result must be accepted or rejected"
        )
    return RoutingValidationResult(
        accepted=False,
        allowlist_valid=False,
        budget_valid=False,
        security_valid=False,
        schema_valid=False,
        registry_valid=False,
        runtime_valid=False,
        capability_valid=False,
        rejection_reasons=("frozen-plan-rejected",),
    )


def _accepted_validation_result() -> RoutingValidationResult:
    """Return the shared all-true result used by accepted compact fixtures."""
    return RoutingValidationResult(
        accepted=True,
        allowlist_valid=True,
        budget_valid=True,
        security_valid=True,
        schema_valid=True,
        registry_valid=True,
        runtime_valid=True,
        capability_valid=True,
    )


def _deterministic_plan_to_dict(plan: RoutingPlan) -> dict[str, object]:
    """Serialize deterministic fields in the frozen fixture-compatible shape."""
    if plan.input_facts is None:
        raise ParserRoutingValidationError("Deterministic plan lacks input facts")
    result: dict[str, object] = {
        "schema": plan.schema,
        "mode": plan.mode,
        "input_facts": _input_facts_to_dict(plan.input_facts),
        "rules_evaluated": list(plan.rules_evaluated),
        "selected_invocations": [
            _invocation_to_dict(item, include_empty_purpose=True)
            for item in plan.selected_invocations
        ],
        "llm_used": plan.llm_used,
    }
    if not plan._compact_fixture:
        if (
            plan.boundary is None
            or plan.registry_version is None
            or plan.registry_sha256 is None
        ):
            raise ParserRoutingValidationError(
                "Canonical deterministic plan lacks validation context"
            )
        result["candidate_invocations"] = [
            _invocation_to_dict(item, include_empty_purpose=True)
            for item in plan.candidate_invocations
        ]
        result["deterministic_boundary"] = _boundary_to_dict(
            plan.boundary, compact=False
        )
        result["registry_sha256"] = plan.registry_sha256
        result["registry_version"] = plan.registry_version
        result["validation_result"] = _validation_result_to_dict(
            plan.validation_result
        )
    return result


def _hybrid_plan_to_dict(plan: RoutingPlan) -> dict[str, object]:
    """Serialize compact frozen or canonical hybrid boundary/proposal records."""
    if plan.boundary is None or plan.proposal is None:
        raise ParserRoutingValidationError("Hybrid plan lacks boundary or proposal")
    result: dict[str, object] = {
        "schema": plan.schema,
        "mode": plan.mode,
        "deterministic_boundary": _boundary_to_dict(
            plan.boundary, compact=plan._compact_fixture
        ),
        "frozen_llm_proposal": _hybrid_proposal_to_dict(
            plan.proposal, compact=plan._compact_fixture
        ),
        "validation_result": (
            "accepted" if plan.validation_result.accepted else "rejected"
        )
        if plan._compact_fixture
        else _validation_result_to_dict(plan.validation_result),
    }
    if not plan._compact_fixture:
        if (
            plan.input_facts is None
            or plan.registry_version is None
            or plan.registry_sha256 is None
            or plan.provider_profile is None
        ):
            raise ParserRoutingValidationError(
                "Canonical hybrid plan lacks validation context"
            )
        result["input_facts"] = _input_facts_to_dict(plan.input_facts)
        result["provider_profile"] = _provider_profile_to_dict(
            plan.provider_profile
        )
        result["registry_sha256"] = plan.registry_sha256
        result["registry_version"] = plan.registry_version
    return result


def _llm_directed_plan_to_dict(plan: RoutingPlan) -> dict[str, object]:
    """Serialize registry-bound LLM proposal and deterministic validation facts."""
    if plan.proposal is None or plan.registry_version is None:
        raise ParserRoutingValidationError(
            "LLM-directed plan lacks proposal or registry version"
        )
    validation: object
    if plan._compact_fixture:
        validation = {
            "allowlist_valid": plan.validation_result.allowlist_valid,
            "budget_valid": plan.validation_result.budget_valid,
            "security_valid": plan.validation_result.security_valid,
            "schema_valid": plan.validation_result.schema_valid,
        }
    else:
        validation = _validation_result_to_dict(plan.validation_result)
    result = {
        "schema": plan.schema,
        "mode": plan.mode,
        "registry_version": plan.registry_version,
        "frozen_llm_proposal": _llm_proposal_to_dict(plan.proposal),
        "deterministic_validation": validation,
        "result": "accepted" if plan.validation_result.accepted else "rejected",
    }
    if not plan._compact_fixture:
        if (
            plan.input_facts is None
            or plan.boundary is None
            or plan.registry_sha256 is None
            or plan.provider_profile is None
        ):
            raise ParserRoutingValidationError(
                "Canonical LLM-directed plan lacks validation context"
            )
        result["deterministic_boundary"] = _boundary_to_dict(
            plan.boundary, compact=False
        )
        result["input_facts"] = _input_facts_to_dict(plan.input_facts)
        result["provider_profile"] = _provider_profile_to_dict(
            plan.provider_profile
        )
        result["registry_sha256"] = plan.registry_sha256
    return result


def _provider_profile_to_dict(
    profile: RoutingProviderProfile,
) -> dict[str, object]:
    """Serialize every trusted provider fact used by security validation.

    Canonical proposal-backed writers call this after aggregate validation. The
    output always includes provider identity, external-service use, and trusted
    tags; optional audit labels are emitted only when present. No proposal claim
    is consulted and no validated profile field is silently dropped.
    """
    value: dict[str, object] = {
        "provider_id": profile.provider_id,
        "uses_external_services": profile.uses_external_services,
        "security_tags": list(profile.security_tags),
    }
    if profile.provider_kind is not None:
        value["provider_kind"] = profile.provider_kind
    if profile.deployment_id is not None:
        value["deployment_id"] = profile.deployment_id
    return value


def _input_facts_to_dict(facts: RoutingInputFacts) -> dict[str, object]:
    """Serialize bounded facts using the authoritative fixture field names."""
    value: dict[str, object] = {
        "media_type": facts.media_type,
        "native_text_ratio": facts.native_text_ratio,
        "requires": list(facts.required_capabilities),
    }
    if facts.page_count is not None:
        value["page_count"] = facts.page_count
    if facts.source_size_bytes is not None:
        value["source_size_bytes"] = facts.source_size_bytes
    if facts.document_class is not None:
        value["document_class"] = facts.document_class
    return value


def _boundary_to_dict(boundary: RoutingBoundary, *, compact: bool) -> dict[str, object]:
    """Serialize a boundary while omitting only fixture-default optional fields."""
    value: dict[str, object] = {
        "allowlist": list(boundary.allowlist),
        "external_services_allowed": boundary.external_services_allowed,
        "max_parser_runs": boundary.max_parser_runs,
    }
    if not compact or boundary.allowed_scopes != _SUPPORTED_SCOPES:
        value["allowed_scopes"] = list(boundary.allowed_scopes)
    if not compact or boundary.required_security_tags:
        value["required_security_tags"] = list(boundary.required_security_tags)
    return value


def _hybrid_proposal_to_dict(
    proposal: RoutingProposal, *, compact: bool
) -> dict[str, object]:
    """Serialize selected IDs for frozen hybrid plans or full canonical records."""
    if compact:
        value: dict[str, object] = {
            "selected": [item.parser_id for item in proposal.invocations],
            "reason": proposal.reason,
        }
    else:
        value = {
            "invocations": [
                _invocation_to_dict(item, include_empty_purpose=False)
                for item in proposal.invocations
            ]
        }
        _add_optional_proposal_fields(value, proposal)
    return value


def _llm_proposal_to_dict(proposal: RoutingProposal) -> dict[str, object]:
    """Serialize LLM-directed invocations and the explicit stop condition."""
    value: dict[str, object] = {
        "invocations": [
            _invocation_to_dict(item, include_empty_purpose=False)
            for item in proposal.invocations
        ],
    }
    if proposal.stop_condition is not None:
        value["stop_condition"] = proposal.stop_condition
    _add_optional_proposal_fields(value, proposal, include_stop=False)
    return value


def _add_optional_proposal_fields(
    value: dict[str, object],
    proposal: RoutingProposal,
    *,
    include_stop: bool = True,
) -> None:
    """Add bounded nonempty proposal audit fields without payload logging."""
    if proposal.reason is not None:
        value["reason"] = proposal.reason
    if include_stop and proposal.stop_condition is not None:
        value["stop_condition"] = proposal.stop_condition
    for name in ("provider", "model", "request_id"):
        item = getattr(proposal, name)
        if item is not None:
            value[name] = item
    if proposal.external_services_used:
        value["external_services_used"] = True
    if proposal.security_tags:
        value["security_tags"] = list(proposal.security_tags)


def _invocation_to_dict(
    invocation: ParserInvocation, *, include_empty_purpose: bool
) -> dict[str, object]:
    """Serialize one invocation without dropping nonempty purpose or security tags."""
    value: dict[str, object] = {
        "parser_id": invocation.parser_id,
        "scope": invocation.scope,
    }
    if include_empty_purpose or invocation.purpose:
        value["purpose"] = list(invocation.purpose)
    if invocation.security_tags:
        value["security_tags"] = list(invocation.security_tags)
    return value


def _validation_result_to_dict(
    result: RoutingValidationResult,
) -> dict[str, object]:
    """Serialize every deterministic validation flag and bounded reason."""
    return {
        "accepted": result.accepted,
        "allowlist_valid": result.allowlist_valid,
        "budget_valid": result.budget_valid,
        "security_valid": result.security_valid,
        "schema_valid": result.schema_valid,
        "registry_valid": result.registry_valid,
        "runtime_valid": result.runtime_valid,
        "capability_valid": result.capability_valid,
        "rejection_reasons": list(result.rejection_reasons),
    }


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys at every object before mapping conversion.

    ``json.loads`` calls this for each nesting level. The helper preserves ordinary
    insertion order and reports only a bounded key identifier, never source values
    or surrounding payload. The same key in two separate objects remains valid.
    """
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ParserRoutingValidationError(
                f"Duplicate routing JSON key: {_bounded_label(key)}"
            )
        value[key] = item
    return value


def _require_fields(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    """Enforce exact object fields so payload or future semantics cannot hide."""
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ParserRoutingValidationError(
            f"Invalid {context} fields: required {sorted(required)}"
        )


def _mapping(value: object, context: str) -> Mapping[str, object]:
    """Return a string-key mapping or raise a typed routing-boundary error."""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ParserRoutingValidationError(f"{context} must be an object")
    return value


def _mapping_sequence(value: object, context: str) -> tuple[Mapping[str, object], ...]:
    """Parse an array of objects without accepting strings or normalizing order."""
    if not isinstance(value, list):
        raise ParserRoutingValidationError(f"{context} must be an array")
    return tuple(_mapping(item, context) for item in value)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    """Parse an ordered JSON string array into an immutable tuple."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ParserRoutingValidationError(f"{context} must be a string array")
    return tuple(value)


def _required_text(value: object, context: str) -> str:
    """Require nonempty already-trimmed text without coercion."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ParserRoutingValidationError(f"{context} must be non-empty text")
    return value


def _optional_text(value: object, context: str) -> str | None:
    """Accept null or delegate strict nonempty text validation."""
    if value is None:
        return None
    return _required_text(value, context)


def _required_bool(value: object, context: str) -> bool:
    """Require a real Boolean rather than accepting integer coercion."""
    if not isinstance(value, bool):
        raise ParserRoutingValidationError(f"{context} must be Boolean")
    return value


def _optional_bool_default_false(value: object, context: str) -> bool:
    """Interpret an absent optional Boolean as false without coercing other values."""
    if value is None:
        return False
    return _required_bool(value, context)


def _required_int(value: object, context: str) -> int:
    """Require an integer and reject Boolean values explicitly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParserRoutingValidationError(f"{context} must be an integer")
    return value


def _optional_nonnegative_int(value: object, context: str) -> int | None:
    """Accept null or a nonnegative integer without numeric coercion."""
    if value is None:
        return None
    parsed = _required_int(value, context)
    if parsed < 0:
        raise ParserRoutingValidationError(f"{context} must be nonnegative")
    return parsed


def _validate_parser_id(value: object) -> str:
    """Require one bounded lowercase logical parser identifier."""
    text = _required_text(value, "parser_id")
    if not _PARSER_ID_PATTERN.fullmatch(text):
        raise ParserRoutingValidationError("parser_id is malformed")
    return text


def _bounded_identifier(value: object, context: str) -> str:
    """Require bounded path-free single-line audit identifier text."""
    text = _required_text(value, context)
    if len(text) > 128 or any(item in text for item in ("/", "\\", "\n", "\r")):
        raise ParserRoutingValidationError(f"{context} is malformed")
    return text


def _validate_sha256(value: object, context: str) -> str:
    """Require one lowercase SHA-256 digest without accepting normalization."""
    text = _required_text(value, context)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ParserRoutingValidationError(
            f"{context} must be a lowercase SHA-256 digest"
        )
    return text


def _bounded_audit_text(value: object, context: str) -> str:
    """Require bounded single-line audit prose and reject path-like content."""
    text = _required_text(value, context)
    lowered = text.lower()
    if (
        len(text) > _MAX_AUDIT_TEXT_LENGTH
        or "\n" in text
        or "\r" in text
        or "file://" in lowered
        or "../" in text
        or "..\\" in text
        or text.startswith(("/", "\\"))
    ):
        raise ParserRoutingValidationError(f"{context} contains forbidden content")
    return text


def _require_lexical_order(values: tuple[str, ...], context: str) -> None:
    """Require unique lexical order without sorting or repairing supplied values."""
    _reject_duplicates(values, context)
    if values != tuple(sorted(values)):
        raise ParserRoutingValidationError(
            f"{context} are not deterministically ordered"
        )


def _reject_duplicates(values: object, context: str) -> None:
    """Reject duplicate logical identities before indexes can overwrite facts."""
    observed = tuple(values)
    try:
        duplicated = len(observed) != len(set(observed))
    except TypeError as error:
        raise ParserRoutingValidationError(
            f"{context} contains malformed identities"
        ) from error
    if duplicated:
        raise ParserRoutingValidationError(f"Duplicate {context}")


def _bounded_label(value: object) -> str:
    """Return a diagnostic identifier capped to avoid payload disclosure."""
    return str(value)[:64]


__all__ = [
    "ADAPTIVE_ROUTING_MODES",
    "LEGACY_POLICY_TO_ADAPTIVE_MODE",
    "ROUTING_PLAN_SCHEMA",
    "DeterministicRoutingRule",
    "ParserInvocation",
    "ParserRoutingCapabilityError",
    "ParserRoutingCompatibilityError",
    "ParserRoutingError",
    "ParserRoutingProposalError",
    "ParserRoutingRejectedError",
    "ParserRoutingRequest",
    "ParserRoutingService",
    "ParserRoutingValidationError",
    "RoutingBoundary",
    "RoutingInputFacts",
    "RoutingPlan",
    "RoutingProposal",
    "RoutingProposalProvider",
    "RoutingProviderProfile",
    "RoutingValidationResult",
    "adaptive_mode_for_legacy_policy",
]
