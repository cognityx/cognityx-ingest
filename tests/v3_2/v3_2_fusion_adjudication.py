from __future__ import annotations

import json


def test_fusion_cases_preserve_agreement_conflict_and_unresolved_states(v3_2_fixture_root):
    fusion = json.loads((v3_2_fixture_root / "parser_observations" / "fusion_cases.json").read_text(encoding="utf-8"))
    statuses = {case["status"] for case in fusion["cases"]}
    assert {"agreement", "conflict", "unresolved"} <= statuses


def test_ambiguous_and_unresolved_relations_are_never_gold_support(v3_2_fixture_root):
    graph = json.loads((v3_2_fixture_root / "expected" / "source_graph.json").read_text(encoding="utf-8"))
    forbidden = {"ambiguous", "unresolved"}
    assert all(not rel.get("gold_eligible", True) for rel in graph["relations"] if rel["status"] in forbidden)
