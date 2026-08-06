"""Routing-mode and legacy policy compatibility checks for v3.2.

The fixture freezes the three higher-level adaptive routing modes. Existing
production still exposes `ExtractionPolicy` with legacy mode names, so this
suite checks both boundaries without asking production code to implement T04.
"""

from __future__ import annotations

import json

import pytest

from cognityx_ingest import ExtractionPolicy


def test_exactly_three_adaptive_routing_modes(v3_2_fixture_root):
    """Assert the registry fixture freezes the three adaptive routing modes."""
    registry = json.loads(
        (v3_2_fixture_root / "capability_registry" / "parser_capabilities.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["allowed_routing_modes"] == [
        "deterministic",
        "hybrid",
        "llm-directed",
    ]


def test_routing_plan_files_match_the_three_modes(v3_2_fixture_root):
    """Assert each routing plan fixture advertises its own frozen mode."""
    assert json.loads((v3_2_fixture_root / "routing" / "deterministic_plan.json").read_text())[
        "mode"
    ] == "deterministic"
    assert json.loads((v3_2_fixture_root / "routing" / "hybrid_plan.json").read_text())[
        "mode"
    ] == "hybrid"
    assert json.loads((v3_2_fixture_root / "routing" / "llm_directed_plan.json").read_text())[
        "mode"
    ] == "llm-directed"


def test_legacy_parser_policy_names_remain_compatible():
    """Validate legacy parser policy names against the current production API."""
    for mode in ("fixed", "rule", "fallback", "compare", "agent"):
        assert ExtractionPolicy(mode=mode).mode == mode
    with pytest.raises(ValueError):
        ExtractionPolicy(mode="deterministic")
