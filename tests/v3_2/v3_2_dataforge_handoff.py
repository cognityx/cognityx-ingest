from __future__ import annotations

import json


def test_dataforge_paragraph_qa_contract(v3_2_fixture_root):
    qa = json.loads((v3_2_fixture_root / "dataforge" / "paragraph_qa_contract.json").read_text(encoding="utf-8"))
    assert qa["contract"] == "paragraph_qa"
    assert qa["forbidden_fields"] == ["embedding", "vector_index", "vector_database"]


def test_dataforge_composite_ku_contract(v3_2_fixture_root):
    ku = json.loads((v3_2_fixture_root / "dataforge" / "composite_ku_contract.json").read_text(encoding="utf-8"))
    assert ku["contract"] == "composite_ku"
    assert ku["evidence_support_must_be_exact"] is True
