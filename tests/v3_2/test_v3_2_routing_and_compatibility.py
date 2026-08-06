"""Routing-mode and legacy policy compatibility checks for production T04."""

from __future__ import annotations

import pytest

from cognityx_ingest import (
    ADAPTIVE_ROUTING_MODES,
    LEGACY_POLICY_TO_ADAPTIVE_MODE,
    ROUTING_PLAN_SCHEMA,
    ExtractionPolicy,
    RoutingPlan,
    adaptive_mode_for_legacy_policy,
)


def test_exactly_three_adaptive_routing_modes(v3_2_fixture_root):
    """Expose exactly the contractual deterministic, hybrid, and LLM modes."""
    assert ADAPTIVE_ROUTING_MODES == (
        "deterministic",
        "hybrid",
        "llm-directed",
    )


def test_routing_plan_files_match_the_three_modes(v3_2_fixture_root):
    """Load all frozen plans through the strict production routing seam."""
    names_and_modes = (
        ("deterministic_plan.json", "deterministic"),
        ("hybrid_plan.json", "hybrid"),
        ("llm_directed_plan.json", "llm-directed"),
    )
    for name, mode in names_and_modes:
        plan = RoutingPlan.from_json_bytes(
            (v3_2_fixture_root / "routing" / name).read_bytes()
        )
        assert plan.schema == ROUTING_PLAN_SCHEMA
        assert plan.mode == mode
        assert plan.validation_result.accepted is True
        assert plan.to_json_bytes() == plan.to_json_bytes()


def test_legacy_parser_policy_names_remain_compatible():
    """Validate legacy parser policy names against the current production API."""
    for mode in ("fixed", "rule", "fallback", "compare", "agent"):
        assert ExtractionPolicy(mode=mode).mode == mode
    with pytest.raises(ValueError):
        ExtractionPolicy(mode="deterministic")


def test_legacy_policy_mapping_is_explanatory_and_complete() -> None:
    """Map every old policy without introducing an LLM-directed legacy alias."""
    assert dict(LEGACY_POLICY_TO_ADAPTIVE_MODE) == {
        "fixed": "deterministic",
        "rule": "deterministic",
        "fallback": "deterministic",
        "compare": "deterministic",
        "agent": "hybrid",
    }
    for legacy, expected in LEGACY_POLICY_TO_ADAPTIVE_MODE.items():
        assert adaptive_mode_for_legacy_policy(ExtractionPolicy(mode=legacy)) == expected
    assert "llm-directed" not in LEGACY_POLICY_TO_ADAPTIVE_MODE.values()


def test_t04_introduces_no_alignment_fusion_or_adjudication_api() -> None:
    """Stop routing at invocation plans and leave observation handling to T05."""
    import cognityx_ingest.parser_routing as routing

    for name in (
        "align",
        "alignment",
        "fuse",
        "fusion",
        "adjudicate",
        "adjudication",
        "_fuse_results",
    ):
        assert not hasattr(routing, name)
