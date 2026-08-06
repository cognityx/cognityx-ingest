"""Extraction reuse, retention, and purge fixture checks for v3.2.

The tests freeze T07's boundary without implementing purge behavior. They read
the fixture artifact states and assert that active references, legal hold, and
purge tombstones remain distinguishable for later production work.
"""

from __future__ import annotations

import json


def test_retention_fixture_covers_reuse_hold_and_purge(v3_2_fixture_root):
    """Validate active retention, legal hold, and purge tombstone cases."""
    retention = json.loads(
        (v3_2_fixture_root / "retention" / "retention_cases.json").read_text(
            encoding="utf-8"
        )
    )
    artifacts = retention["artifacts"]
    assert any(
        item["state"] == "validated" and item["purge_eligible"] is False
        for item in artifacts
    )
    assert any(
        item["legal_hold"] is True and item["purge_eligible"] is False
        for item in artifacts
    )
    assert any(
        item["purge_eligible"] is True and item["post_purge_tombstone"]
        for item in artifacts
    )
