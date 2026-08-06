from __future__ import annotations

import json


def test_exactly_three_capability_source_classes(v3_2_fixture_root):
    registry = json.loads((v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(encoding="utf-8"))
    assert registry["allowed_capability_source_classes"] == ["parser-discovered", "human-guided", "auto-learned"]


def test_capability_registry_contains_real_parsers(v3_2_fixture_root):
    registry = json.loads((v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(encoding="utf-8"))
    assert {item["parser_name"] for item in registry["parsers"]} >= {"basic", "pymupdf", "docling"}
