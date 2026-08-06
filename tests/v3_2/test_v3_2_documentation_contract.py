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


def _module_tree(module: object = native_artifacts) -> ast.Module:
    """Parse an installed source module so tests inspect production code itself."""
    module_path = Path(module.__file__)
    return ast.parse(module_path.read_text(encoding="utf-8"))


def test_native_artifact_module_has_substantial_architectural_docstring() -> None:
    """Require the purpose, design, flow, consumers, ownership, and non-goals."""
    module_docstring = ast.get_docstring(_module_tree()) or ""
    normalized = module_docstring.lower()

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
