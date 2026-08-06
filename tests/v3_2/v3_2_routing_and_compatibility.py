from __future__ import annotations

import json


def test_exactly_three_adaptive_routing_modes(v3_2_fixture_root):
    registry = json.loads((v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(encoding="utf-8"))
    assert registry["allowed_routing_modes"] == ["deterministic", "hybrid", "llm-directed"]


def test_legacy_parser_policy_names_remain_compatible(v3_2_fixture_root):
    registry = json.loads((v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(encoding="utf-8"))
    assert registry["legacy_policy_names"] == ["fixed", "rule", "fallback", "compare", "agent"]
