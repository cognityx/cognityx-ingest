"""Objective documentation-presence checks for the T01 architectural module.

The suite uses Python's standard ``ast`` module so normal CI needs no additional
lint package. It verifies structural presence and required concepts while leaving
accuracy, clarity, and prose quality to code review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cognityx_ingest.native_artifacts as native_artifacts


def _module_tree() -> ast.Module:
    """Parse the installed source module so tests inspect production code itself."""
    module_path = Path(native_artifacts.__file__)
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
