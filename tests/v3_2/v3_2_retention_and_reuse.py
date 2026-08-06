from __future__ import annotations

import json


def test_retention_fixture_covers_reuse_hold_and_purge(v3_2_fixture_root):
    retention = json.loads((v3_2_fixture_root / "retention" / "retention_cases.json").read_text(encoding="utf-8"))
    states = {item["state"] for item in retention["cases"]}
    assert {"reuse", "retain", "purge", "legal_hold"} <= states
