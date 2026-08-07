"""Objective documentation-presence checks for v3.2 architectural modules.

The suite uses Python's standard ``ast`` module so normal CI needs no additional
lint package. It verifies structural presence and required concepts while leaving
accuracy, clarity, and prose quality to code review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cognityx_ingest.canonical_content as canonical_content
import cognityx_ingest.native_artifacts as native_artifacts
import cognityx_ingest.parser as parser
import cognityx_ingest.parser_capabilities as parser_capabilities
import cognityx_ingest.parser_fusion as parser_fusion
import cognityx_ingest.parser_routing as parser_routing


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
