"""Focused DataForge handoff fixture checks for v3.2 T00.

The tests freeze only the two downstream proofs required by the contract:
paragraph Q/A and one composite Knowledge Unit. The core algorithm is schema
and address validation against the fixture, not DataForge execution. Future
T09 work uses these records as the exact acceptance inputs.
"""

from __future__ import annotations

import json


def test_dataforge_paragraph_qa_contract(v3_2_fixture_root):
    """Validate the paragraph Q/A handoff fixture shape and support address."""
    qa = json.loads(
        (v3_2_fixture_root / "dataforge" / "paragraph_qa_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert qa["schema"] == "cognityx.dataforge.paragraph-qa-handoff/v1"
    assert qa["input"]["segmentation_view_id"] == "view-paragraph-v1"
    assert qa["expected_output"]["support_address_ids"] == ["addr-strong-pol-p2"]
    assert qa["expected_output"]["must_not_store_independent_source_copy"] is True


def test_dataforge_composite_ku_contract(v3_2_fixture_root):
    """Validate the composite Knowledge Unit fixture excludes ambiguous support."""
    ku = json.loads(
        (v3_2_fixture_root / "dataforge" / "composite_ku_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert ku["schema"] == "cognityx.dataforge.composite-ku-handoff/v1"
    assert ku["seed"] == {"division_id": "div-policy-4.2"}
    assert "rel-ambiguous-example" in ku["excluded_relation_ids"]
    assert ku["expected_knowledge_unit"]["gold_support_contains_only_validated_relations"] is True
