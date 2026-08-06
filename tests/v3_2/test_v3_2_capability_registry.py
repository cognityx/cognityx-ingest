"""Fixture checks for the v3.2 parser capability registry contract.

The registry fixture is the source of truth for T03. These tests read the
frozen JSON directly, validate the three capability-source classes, and avoid
inventing parser records that are not present in the pack. T03 implementers
and reviewers use this suite to keep future registry APIs aligned with the
authoritative fixture.
"""

from __future__ import annotations

import json


def test_exactly_three_capability_source_classes(v3_2_fixture_root):
    """Assert the registry freezes exactly the three approved source classes."""
    registry = json.loads(
        (v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["allowed_capability_source_classes"] == [
        "parser-discovered",
        "human-guided",
        "auto-learned",
    ]


def test_capability_registry_contains_real_parsers(v3_2_fixture_root):
    """Assert parser records match the actual frozen `parser_id` values."""
    registry = json.loads(
        (v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["parser_id"] for item in registry["parsers"]} == {
        "docling",
        "pymupdf",
        "future-parser",
    }
    for parser in registry["parsers"]:
        assert set(parser["capability_sources"]) == {
            "parser-discovered",
            "human-guided",
            "auto-learned",
        }
