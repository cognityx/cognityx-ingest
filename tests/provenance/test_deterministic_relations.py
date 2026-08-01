from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="GAP-DETERMINISTIC-RELATIONS: docs/provenance-gap-report.md#gap-deterministic-relations",
)
@pytest.mark.parametrize(
    "relation_id",
    [
        "rel-exact-7.2",
        "rel-plural-7.2-11.3",
        "rel-appendix-b",
        "rel-page-a-1",
        "rel-plain-url",
        "rel-missing-handbook",
    ],
)
def test_expected_relation_is_detected_without_model(
    relation_id: str, ground_truth: dict[str, object]
) -> None:
    # The production-facing relation detector will replace this empty result.
    canonical_relations: list[dict[str, object]] = []
    expected = next(
        item for item in ground_truth["relations"] if item["id"] == relation_id
    )
    assert any(item["literal"] == expected["literal"] for item in canonical_relations)


def test_ground_truth_separates_resolved_ambiguous_and_unresolved(
    ground_truth: dict[str, object]
) -> None:
    statuses = {item["status"] for item in ground_truth["relations"]}
    assert {"observed", "resolved", "rejected", "ambiguous", "unresolved"} <= statuses
    missing = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-missing-handbook"
    )
    ambiguous = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-ambiguous-travel"
    )
    assert missing["targets"] == []
    assert len(ambiguous["targets"]) == 2
