"""Objective documentation-presence checks for v3.2 architectural modules.

The suite uses Python's standard ``ast`` module so normal CI needs no additional
lint package. It verifies structural presence and required concepts while leaving
accuracy, clarity, and prose quality to code review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cognityx_ingest.canonical_content as canonical_content
import cognityx_ingest.cleanup as cleanup
import cognityx_ingest.models as models
import cognityx_ingest.native_artifacts as native_artifacts
import cognityx_ingest.parser as parser
import cognityx_ingest.parser_capabilities as parser_capabilities
import cognityx_ingest.parser_fusion as parser_fusion
import cognityx_ingest.parser_routing as parser_routing
import cognityx_ingest.segmentation_views as segmentation_views
import cognityx_ingest.source_assets as source_assets
import cognityx_ingest.source_graph as source_graph


def _module_tree(module: object = native_artifacts) -> ast.Module:
    """Parse an installed source module so tests inspect production code itself."""
    module_path = Path(module.__file__)
    return ast.parse(module_path.read_text(encoding="utf-8"))


def test_native_artifact_module_has_substantial_architectural_docstring() -> None:
    """Require the purpose, design, flow, consumers, ownership, and non-goals."""
    module_docstring = ast.get_docstring(_module_tree()) or ""
    normalized = " ".join(module_docstring.lower().split())

    assert len(module_docstring) >= 800
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "consumers",
        "ownership",
        "non-goals",
    ):
        assert concept in normalized


def test_every_public_native_artifact_class_has_a_docstring() -> None:
    """Require every deliberately public class to explain its architectural role."""
    public_classes = [
        node
        for node in _module_tree().body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]

    assert public_classes
    assert all(ast.get_docstring(node) for node in public_classes)


def test_every_nontrivial_public_method_and_function_has_a_docstring() -> None:
    """Require callable public seams to carry durable caller-facing guidance."""
    tree = _module_tree()
    public_callables: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            public_callables.append(node)

    assert public_callables
    assert all(ast.get_docstring(node) for node in public_callables)


def test_canonical_content_module_has_substantial_architectural_docstring() -> None:
    """Require T02 purpose, migration, ownership, flow, consumers, and boundaries."""
    module_docstring = ast.get_docstring(_module_tree(canonical_content)) or ""
    normalized = module_docstring.lower()

    assert len(module_docstring) >= 2_000
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "add",
        "contentnode.content.text",
        "presentationunit",
        "division",
        "direct versus subtree",
        "nativebinding",
        "typed presentation label",
        "representation-owned selector",
        "cross-descriptor consistency",
        "exact serializer bytes",
        "missing declared parent",
        "t01",
        "t06",
        "t08",
        "dataforge",
    ):
        assert concept in normalized


def test_every_public_canonical_content_class_documents_its_contract() -> None:
    """Require every public record, error, aggregate, and builder to explain its role."""
    public_classes = [
        node
        for node in _module_tree(canonical_content).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes
    for node in public_classes:
        docstring = (ast.get_docstring(node) or "").lower()
        assert docstring, node.name
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "invariants",
            "lifecycle",
            "thread-safety assumptions",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_canonical_content_function_and_method_has_a_docstring() -> None:
    """Require public seams and private invariant algorithms to explain why they exist."""
    callables = [
        node
        for node in ast.walk(_module_tree(canonical_content))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_named_canonical_algorithms_have_invariant_documentation() -> None:
    """Pin documentation to the high-risk indexing, validation, and build helpers."""
    required = {
        "_build_indexes",
        "_build_divisions",
        "_detect_division_cycles",
        "_build_content_nodes",
        "_validate_selector",
        "_validate_ordering",
        "_validate_native_bindings",
        "_content_node_to_dict",
    }
    functions = {
        node.name: node
        for node in ast.walk(_module_tree(canonical_content))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert required <= set(functions)
    assert all(ast.get_docstring(functions[name]) for name in required)


def test_segmentation_view_module_has_substantial_architectural_docstring() -> None:
    """Require T06 ownership, algorithms, consumers, and handoff boundaries."""
    module_docstring = ast.get_docstring(_module_tree(segmentation_views)) or ""
    normalized = " ".join(module_docstring.lower().split())

    assert len(module_docstring) >= 3_500
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "segmentation view",
        "derived read model",
        "canonical text",
        "nodespan",
        "source text",
        "paragraph",
        "direct",
        "parser-native",
        "sentence",
        "parent",
        "reconstruction",
        "fused boundary",
        "t05",
        "t07",
        "retrieval/dataforge",
    ):
        assert concept in normalized, concept


def test_every_public_segmentation_class_documents_its_full_contract() -> None:
    """Require every T06 record, protocol, error, aggregate, and service rationale."""
    public_classes = [
        node
        for node in _module_tree(segmentation_views).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]

    assert public_classes
    for node in public_classes:
        docstring = " ".join((ast.get_docstring(node) or "").lower().split())
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "main algorithm",
            "invariants",
            "lifecycle",
            "side effects",
            "typed failures",
            "trust boundary",
            "thread",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_segmentation_callable_has_a_docstring() -> None:
    """Require public seams and private trust algorithms to explain why they exist."""
    callables = [
        node
        for node in ast.walk(_module_tree(segmentation_views))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_named_segmentation_algorithms_have_invariant_documentation() -> None:
    """Pin docs to spans, strategies, reconstruction, identity, JSON, and no-copy."""
    required = {
        "validate",
        "resolve_span",
        "build_paragraph",
        "build_direct_division",
        "build_parser_native",
        "build_sentence_safe_fixed_size",
        "build_sentence_window",
        "build_parent_child",
        "from_fixture",
        "validate_view_set",
        "to_json_bytes",
        "cache_identity",
        "_validate_bound_view_set",
        "_validate_span",
        "_validate_paragraph_view",
        "_validate_direct_division_view",
        "_validate_fixed_size_view",
        "_validate_sentence_window_view",
        "_validate_parent_child_view",
        "_validate_native_view",
        "_validate_profile",
        "_validated_native_descriptor_map",
        "_strict_json_loads",
        "_reject_copy_fields",
    }
    callables = {
        node.name: node
        for node in ast.walk(_module_tree(segmentation_views))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert required <= set(callables)
    assert all(ast.get_docstring(callables[name]) for name in required)


def test_t07_modified_modules_explain_purpose_flow_consumers_and_ownership() -> None:
    """Require architectural context in every production module modified by T07."""
    for module in (cleanup, models, source_assets):
        module_docstring = " ".join(
            (ast.get_docstring(_module_tree(module)) or "").lower().split()
        )
        assert len(module_docstring) >= 700, module.__name__
        for concept in (
            "purpose",
            "storage",
            "metadata",
            "t07",
            "payload",
        ):
            assert concept in module_docstring, (module.__name__, concept)


def test_every_t07_record_and_service_has_a_substantial_docstring() -> None:
    """Require new public records and orchestration to explain their consumers."""
    required = {
        "ExtractionRetentionError",
        "ExtractionIdentityError",
        "ExtractionRetentionConflictError",
        "ExtractionRetentionReferenceError",
        "ExtractionReuseError",
        "ExtractionReuseIntegrityError",
        "ExtractionPurgeBlockedError",
        "ExtractionPurgeFinalizationError",
        "ExtractionRetentionState",
        "ExtractionRetentionEventType",
        "ExtractionIdentity",
        "RetentionTombstone",
        "ExtractionRetentionRecord",
        "ExtractionRetentionEvent",
        "ExtractionReuseResult",
        "ExtractionPurgeCandidate",
        "ExtractionPurgePlan",
    }
    model_classes = {
        node.name: node
        for node in _module_tree(models).body
        if isinstance(node, ast.ClassDef)
    }
    assert required <= set(model_classes)
    assert all(len(ast.get_docstring(model_classes[name]) or "") >= 180 for name in required)
    internal_handoff = model_classes["_ExtractionPayloadAbsenceProof"]
    assert len(ast.get_docstring(internal_handoff) or "") >= 400

    cleanup_classes = {
        node.name: node
        for node in _module_tree(cleanup).body
        if isinstance(node, ast.ClassDef)
    }
    assert len(
        ast.get_docstring(cleanup_classes["ExtractionRetentionService"]) or ""
    ) >= 600


def test_every_t07_callable_and_material_algorithm_has_a_docstring() -> None:
    """Pin documentation to identity, transactions, trust, and purge rechecks."""
    required_by_module = {
        models: {
            "from_configuration",
            "digest",
            "purge_reason",
            "purge_eligible",
            "_canonical_identity_json",
            "_identity_configuration",
            "_configuration_key_words",
            "_configuration_key_is_forbidden",
            "_looks_like_local_path",
            "_require_scope",
            "_require_reference_ids",
        },
        cleanup: {
            "register_extraction",
            "acquire_reusable",
            "add_reference",
            "remove_reference",
            "set_legal_hold",
            "mark_retention_expired",
            "plan_purge",
            "list_events",
            "finalize_purge",
            "_prove_payload_absent",
            "collect_reference_ids",
            "_segmentation_reference_id",
            "_verified_descriptor",
            "_assert_record_descriptor",
            "_ordered_reference_ids",
        },
        source_assets: {
            "register_extraction_record",
            "get_extraction_record",
            "find_reusable_extraction",
            "acquire_reusable_extraction",
            "add_extraction_reference",
            "remove_extraction_reference",
            "set_extraction_legal_hold",
            "mark_extraction_retention_expired",
            "list_extraction_records",
            "list_extraction_retention_events",
            "list_extraction_purge_candidates",
            "_finalize_extraction_purge_after_verified_absence",
            "_release_failed_reuse_acquisition",
            "_append_retention_event",
            "_retention_event_from_row",
            "_retention_record_from_row",
            "_stored_legal_hold",
        },
    }
    for module, required in required_by_module.items():
        functions = {
            node.name: node
            for node in ast.walk(_module_tree(module))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert required <= set(functions), module.__name__
        assert all(ast.get_docstring(functions[name]) for name in required)

    t07_model_classes = {
        "ExtractionIdentity",
        "RetentionTombstone",
        "ExtractionRetentionRecord",
        "_ExtractionPayloadAbsenceProof",
        "ExtractionRetentionEvent",
        "ExtractionReuseResult",
        "ExtractionPurgeCandidate",
        "ExtractionPurgePlan",
    }
    for node in _module_tree(models).body:
        if isinstance(node, ast.ClassDef) and node.name in t07_model_classes:
            methods = [
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            assert methods, node.name
            assert all(ast.get_docstring(item) for item in methods), node.name


def test_t07_developer_guide_explains_identity_policy_and_storage_boundary() -> None:
    """Require plain-language guidance from complete identity through tombstone."""
    guide = " ".join(
        (
            Path(__file__).parents[2]
            / "docs"
            / "extraction-reuse-retention-purge.md"
        )
        .read_text(encoding="utf-8")
        .lower()
        .split()
    )
    assert len(guide) >= 10_000
    for concept in (
        "background and purpose",
        "where it fits",
        "extraction identity",
        "exactly six fields",
        "incomplete identity",
        "identity idempotency",
        "lifecycle replay",
        "parser_configuration_hash",
        "adapter and pipeline",
        "exact reuse flow",
        "active reference",
        "segview-<sha256>",
        "nativebinding",
        "validate_view_set",
        "legal hold",
        "retention-expired",
        "purge eligibility algorithm",
        "metadata only",
        "storage-owned physical deletion",
        "supported public flow",
        "internal registry transaction",
        "not proof that storage bytes are absent",
        "applications must never manufacture",
        "same context-bound `nativeartifactstore`",
        "append-only changed-state facts",
        "stale-plan revalidation",
        "retentiontombstone",
        "cross-binding",
        "canonical `contentnode.content.text`",
        "immutable t01 `nativeartifactdescriptor`",
        "normal `ingestservice`",
        "t08",
        "dataforge",
        "sdk",
        "cli",
    ):
        assert concept in guide, concept


def test_segmentation_developer_guide_explains_all_six_views_and_boundaries() -> None:
    """Require ordinary-language guidance from canonical content to consumers."""
    guide = (
        Path(__file__).parents[2] / "docs" / "non-copying-segmentation-views.md"
    ).read_text(encoding="utf-8").lower()

    assert len(guide) >= 7_000
    for concept in (
        "background and purpose",
        "canonical content",
        "nodespan",
        "segment text is absent",
        "paragraph",
        "direct-division",
        "parser-native",
        "sentence-safe-fixed-size",
        "sentence-window",
        "parent-child",
        "read-time reconstruction",
        "deterministic identity",
        "native artifact",
        "immutable binding",
        "structural no-copy",
        "canonical-bound validation",
        "strategy semantic validation",
        "descriptor mapping identity",
        "overlapping views",
        "no fused canonical chunks",
        "t05 relationship",
        "t07 retention boundary",
        "retrieval and dataforge boundary",
        "t06 non-goals",
    ):
        assert concept in guide, concept


def test_parser_capability_module_has_substantial_architectural_docstring() -> None:
    """Require registry authority, source boundaries, consumers, and non-goals."""
    module_docstring = ast.get_docstring(_module_tree(parser_capabilities)) or ""
    normalized = module_docstring.lower()
    assert len(module_docstring) >= 2_500
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "registry authority",
        "parser-discovered",
        "human-guided",
        "auto-learned",
        "runtime",
        "official documentation",
        "preserve supplied",
        "frozen v3.2 fixture",
        "t04",
    ):
        assert concept in normalized


def test_every_public_parser_capability_class_documents_its_contract() -> None:
    """Require every T03 record, error, and registry class to explain its role."""
    public_classes = [
        node
        for node in _module_tree(parser_capabilities).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes
    for node in public_classes:
        docstring = (ast.get_docstring(node) or "").lower()
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "invariants",
            "lifecycle",
            "thread-safety assumptions",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_parser_capability_callable_has_a_docstring() -> None:
    """Require public seams and private validation/overlay algorithms to explain why."""
    callables = [
        node
        for node in ast.walk(_module_tree(parser_capabilities))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_named_parser_capability_algorithms_have_invariant_documentation() -> None:
    """Pin docs to runtime discovery, overlay, conflicts, source classes, and JSON."""
    required = {
        "from_json_bytes",
        "_strict_json_object",
        "_registered_plugin_index",
        "_registry_order_fingerprint",
        "_validate_parser_record_order",
        "_overlay_parser_record",
        "_probe_plugin",
        "_preserve_availability_conflicts",
        "_parse_runtime_probe",
        "_parser_record_to_dict",
    }
    functions = {
        node.name: node
        for node in ast.walk(_module_tree(parser_capabilities))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert required <= set(functions)
    assert all(ast.get_docstring(functions[name]) for name in required)


def test_parser_routing_module_has_substantial_architectural_docstring() -> None:
    """Require T04 purpose, mode meanings, boundaries, consumers, and non-goals."""
    module_docstring = ast.get_docstring(_module_tree(parser_routing)) or ""
    normalized = module_docstring.lower()
    assert len(module_docstring) >= 3_000
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "deterministic",
        "hybrid",
        "llm-directed",
        "capability registry",
        "proposal",
        "deterministic validation",
        "t05",
        "legacy",
        "candidate_invocations",
        "selected",
        "invocation purpose",
        "compact",
        "canonical",
        "registry_sha256",
        "trusted provider profile",
        "untrusted proposal",
        "before any provider call",
        "validate_against_registry",
        "require_executable",
        "audit-readable",
        "execution-authorized",
    ):
        assert concept in normalized


def test_every_public_parser_routing_class_documents_its_contract() -> None:
    """Require every T04 record, protocol, service, and error to explain its role."""
    public_classes = [
        node
        for node in _module_tree(parser_routing).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes
    for node in public_classes:
        docstring = (ast.get_docstring(node) or "").lower()
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "invariants",
            "lifecycle",
            "thread-safety assumptions",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_parser_routing_callable_has_a_docstring() -> None:
    """Require public seams and private routing algorithms to explain why they exist."""
    callables = [
        node
        for node in ast.walk(_module_tree(parser_routing))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_named_parser_routing_algorithms_have_invariant_documentation() -> None:
    """Pin docs to policy, validation, fixture, compatibility, and serialization."""
    required = {
        "adaptive_mode_for_legacy_policy",
        "_build_deterministic_plan",
        "_deterministic_invocation_is_eligible",
        "_validated_boundary",
        "_preflight_provider_profile",
        "_validate_provider_profile",
        "_validate_proposal",
        "_validate_budget",
        "_invocation_purposes_supported",
        "_required_purposes_satisfied",
        "_parser_is_capability_eligible",
        "_registry_sha256",
        "_validate_scope",
        "_validate_stop_condition",
        "_parse_deterministic_plan",
        "_parse_hybrid_plan",
        "_parse_llm_directed_plan",
        "_parse_provider_profile",
        "_strict_json_object",
        "_validation_result_to_dict",
        "_provider_profile_to_dict",
        "_validate_sha256",
    }
    functions = {
        node.name: node
        for node in ast.walk(_module_tree(parser_routing))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert required <= set(functions)
    assert all(ast.get_docstring(functions[name]) for name in required)


def test_adaptive_routing_guide_explains_corrected_persistence_semantics() -> None:
    """Keep candidate, purpose, canonical context, digest, consumer, and T05 prose."""
    repository_root = Path(parser_routing.__file__).parents[2]
    guide = " ".join((
        repository_root / "docs" / "adaptive-parser-routing.md"
    ).read_text(encoding="utf-8").lower().split())
    for concept in (
        "candidate invocation",
        "selected invocation",
        "rejected plans",
        "same parser",
        "compact compatibility",
        "complete canonical",
        "input facts",
        "deterministic boundary",
        "registry_sha256",
        "exact runtime evidence snapshot",
        "audit tools",
        "t05",
        "trusted provider profile",
        "before the provider is called",
        "proposal security tags cannot",
        "validate_against_registry",
        "require_executable",
        "audit-readable",
        "execution-authorized",
    ):
        assert concept in guide


def test_registry_bound_public_methods_document_executable_trust_boundary() -> None:
    """Require callers, algorithm, failures, effects, trust, and parser boundaries."""
    routing_plan = next(
        node
        for node in _module_tree(parser_routing).body
        if isinstance(node, ast.ClassDef) and node.name == "RoutingPlan"
    )
    methods = {
        node.name: (ast.get_docstring(node) or "").lower()
        for node in routing_plan.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in (
        "validate_against_registry",
        "require_executable",
        "to_extraction_policy",
    ):
        docstring = methods[name]
        for concept in (
            "call",
            "algorithm",
            "return",
            "raise",
            "side effect",
            "trust",
            "provider",
            "parser",
        ):
            assert concept in docstring, (name, concept)


def test_parser_fusion_module_has_substantial_architectural_docstring() -> None:
    """Require the complete T04-to-T06 decision boundary and consumer guidance."""
    module_docstring = ast.get_docstring(_module_tree(parser_fusion)) or ""
    normalized = " ".join(module_docstring.lower().split())
    assert len(module_docstring) >= 3_000
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "observation",
        "alignment",
        "fusion",
        "adjudication",
        "agreement",
        "complementary",
        "conflict",
        "unresolved",
        "compatibility projection",
        "confidence",
        "gold support",
        "observations.json",
        "fusion-decisions.json",
        "sha-256",
        "source-region",
        "page, block, object, relation, section, or generic",
        "cross-kind geometry",
        "relation source endpoints are facts, not relation-record identities",
        "reciprocal and unique",
        "unused policy is rejected",
        "known geometry must match exactly",
        "superseded",
        "fact-family",
        "replays",
        "missing confidence",
        "parser-local source-region",
        "value hashing only as a uniquely identifying fallback",
        "fails rather than selecting an arbitrary",
        "ordered reviewed priority",
        "exactly three fields",
        "cross-validated against the observation set",
        "t04",
        "t06",
        "source graph",
        "dataforge",
    ):
        assert concept in normalized, concept


def test_every_public_parser_fusion_class_documents_its_contract() -> None:
    """Require every public T05 record, service, and error to explain its design."""
    public_classes = [
        node
        for node in _module_tree(parser_fusion).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes
    for node in public_classes:
        docstring = (ast.get_docstring(node) or "").lower()
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "main algorithm",
            "invariants",
            "lifecycle",
            "side effects",
            "typed failures",
            "trust boundary",
            "thread-safety assumptions",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_parser_fusion_callable_has_a_docstring() -> None:
    """Require public seams and private invariant algorithms to explain why."""
    callables = [
        node
        for node in ast.walk(_module_tree(parser_fusion))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_named_parser_fusion_algorithms_have_invariant_documentation() -> None:
    """Pin docs to IDs, values, regions, geometry, policy, projection, and gold."""
    required = {
        "_canonical_value_bytes",
        "_adapt_result",
        "_block_region",
        "_bbox_iou",
        "_build_source_region_aggregates",
        "_compatible_region_location",
        "_relation_signature",
        "_region_alignment_candidate",
        "_region_kinds_compatible",
        "_resolve_exact_region_candidates",
        "_mutual_best_region_bbox",
        "_build_alignment_groups",
        "_parser_observation_id",
        "_parser_observation_set_id",
        "_alignment_evidence_id",
        "_aligned_group_id",
        "_fact_decision_id",
        "_region_decision_id",
        "_parser_fusion_id",
        "_policy_for_fact",
        "_adjudicate_fact",
        "_build_region_decisions",
        "_enrich_compatibility_fact_sources",
        "_enrich_source_details",
        "_select_compatibility_observation",
        "_compatibility_parser_id",
        "_processing_activity_threshold",
        "_strict_json_loads",
    }
    functions = {
        node.name: node
        for node in ast.walk(_module_tree(parser_fusion))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert required <= set(functions)
    assert all(ast.get_docstring(functions[name]) for name in required)


def test_parser_fusion_guide_explains_exact_binding_and_priority_semantics() -> None:
    """Pin occurrence, activity, preference, consumers, and future boundaries."""
    repository_root = Path(parser_fusion.__file__).parents[2]
    guide = " ".join(
        (
            repository_root / "docs" / "parser-alignment-fusion-adjudication.md"
        ).read_text(encoding="utf-8").lower().split()
    )
    for concept in (
        "exact parser-occurrence binding",
        "source-region kind",
        "cross-kind geometry",
        "relation record identity",
        "source endpoint",
        "reciprocal",
        "ambiguous exact",
        "unused retained policy",
        "missing geometry",
        "source anchor",
        "value-hash fallback",
        "more than one match raises",
        "source text is not copied",
        "ordered reviewed priority",
        "first listed value that occurs",
        "exactly three fields",
        "cross-validated against the observation set",
        "recomputing a fusion id cannot rescue",
        "canonicalfactsource",
        "t06",
        "t08",
    ):
        assert concept in guide, concept


def test_compatibility_projection_helpers_document_occurrence_provenance() -> None:
    """Require changed parser helpers to explain identity, consumers, and scope."""
    required = {
        "_fuse_page",
        "_fuse_blocks",
        "_fuse_objects",
        "_fuse_relations",
        "_source_detail",
        "_parser_source_region_id",
        "_block_fact_sources",
        "_object_fact_sources",
        "_relation_fact_sources",
    }
    functions = {
        node.name: node
        for node in ast.walk(_module_tree(parser))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert required <= set(functions)
    documentation = " ".join(
        (ast.get_docstring(functions[name]) or "").lower() for name in required
    )
    for concept in (
        "source-region",
        "anchor",
        "occurrence",
        "compatibility",
        "audit",
        "source text",
        "t06",
        "t08",
    ):
        assert concept in documentation, concept


def test_source_graph_module_explains_architecture_and_boundaries() -> None:
    """Require T08 purpose, flow, consumers, ownership, and explicit non-goals."""
    module_docstring = " ".join(
        (ast.get_docstring(_module_tree(source_graph)) or "").lower().split()
    )
    assert len(module_docstring) >= 2_500
    for concept in (
        "purpose",
        "design principles",
        "processing flow",
        "primary consumers",
        "ownership boundary",
        "non-goals",
        "source graph",
        "canonical text",
        "deterministic",
        "ambiguous",
        "provenance address",
        "t09",
        "t10",
        "semantic knowledge graph",
        "graph database",
        "parser execution",
        "network/provider/llm",
    ):
        assert concept in module_docstring, concept


def test_every_public_source_graph_class_documents_its_full_contract() -> None:
    """Require every T08 record, protocol, error, repository, and service rationale."""
    public_classes = [
        node
        for node in _module_tree(source_graph).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes
    for node in public_classes:
        docstring = " ".join((ast.get_docstring(node) or "").lower().split())
        for concept in (
            "responsibility",
            "constructed by",
            "used by",
            "main algorithm",
            "invariants",
            "lifecycle",
            "side effects",
            "typed failures",
            "trust boundary",
            "thread",
        ):
            assert concept in docstring, (node.name, concept)


def test_every_source_graph_callable_has_a_docstring() -> None:
    """Require public seams and private trust algorithms to explain why they exist."""
    callables = [
        node
        for node in ast.walk(_module_tree(source_graph))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert callables
    assert all(ast.get_docstring(node) for node in callables)


def test_source_graph_guide_explains_records_addresses_and_handoffs() -> None:
    """Pin ordinary-language documentation to T08 safety and compatibility facts."""
    repository_root = Path(source_graph.__file__).parents[2]
    guide = " ".join(
        (
            repository_root / "docs" / "source-graph-and-provenance-addresses.md"
        ).read_text(encoding="utf-8").lower().split()
    )
    for concept in (
        "source graph versus a knowledge graph",
        "does not need a graph database",
        "resources",
        "presentation units",
        "divisions and nodes",
        "selectors and bindings",
        "representation lineage is acyclic",
        "bounded visited set",
        "explicit relations",
        "graph revisions",
        "strong address",
        "each supplied selector must be provable",
        "compatibility form",
        "logical address",
        "evidence-set address",
        "exact",
        "redirected",
        "ambiguous",
        "obsolete",
        "forbidden",
        "unresolved",
        "no failure becomes fabricated evidence",
        "result shapes are closed",
        "ordered exact member results",
        "consumer trust boundary",
        "parser-payload purge independence",
        "t09",
        "t10",
    ):
        assert concept in guide, concept
