"""Parser alignment, fusion, and adjudication fixture checks for v3.2.

The suite exists to keep T05 bounded: routing can run multiple parsers, but
fusion must preserve agreement, complementary facts, conflicts, and unresolved
states. Tests read the fixture's expected states directly so future production
work cannot silently rename or flatten those distinctions.
"""

from __future__ import annotations

import json


def test_fusion_cases_preserve_agreement_conflict_and_unresolved_states(v3_2_fixture_root):
    """Assert the frozen fusion cases include every required adjudication state."""
    fusion = json.loads(
        (v3_2_fixture_root / "parser_observations" / "fusion_cases.json").read_text(
            encoding="utf-8"
        )
    )
    statuses = {case["expected"]["state"] for case in fusion["cases"]}
    assert {"agreement", "complementary", "conflict", "unresolved"} <= statuses


def test_ambiguous_and_unresolved_relations_are_never_gold_support(v3_2_fixture_root):
    """Assert ambiguous or unresolved graph relations are excluded from gold support."""
    graph = json.loads(
        (v3_2_fixture_root / "expected" / "source_graph.json").read_text(
            encoding="utf-8"
        )
    )
    forbidden = {"ambiguous", "unresolved"}
    assert all(
        not rel.get("gold_eligible", True)
        for rel in graph["relations"]
        if rel["status"] in forbidden
    )
