"""Exact acceptance checks for the frozen v3.2 T07 retention fixture.

The fixture omits constituent identity fields, so these tests preserve its
published identity digests rather than pretending to recompute them. Separate
production tests prove the complete six-field formula and lifecycle behavior.
"""

from __future__ import annotations

import json
import re


def test_retention_fixture_covers_reuse_hold_and_purge(v3_2_fixture_root):
    """Assert exact frozen references, decision reasons, and tombstone facts."""
    retention = json.loads(
        (v3_2_fixture_root / "retention" / "retention_cases.json").read_text(
            encoding="utf-8"
        )
    )
    assert retention["extraction_identity_formula"] == [
        "source_sha256",
        "parser_id",
        "parser_version",
        "parser_configuration_hash",
        "model_version",
        "scope",
    ]
    artifacts = {item["artifact_id"]: item for item in retention["artifacts"]}
    active = artifacts["art-docling-001"]
    assert active["reference_ids"] == [
        "bind-pol-heading-42-docling",
        "bind-pol-p2-docling",
        "consumer-dataforge-1",
    ]
    assert active["state"] == "validated"
    assert active["purge_eligible"] is False
    assert active["reason"] == "active references remain"
    assert re.fullmatch(r"[0-9a-f]{64}", active["extraction_identity"])

    expired = artifacts["art-old-parser-001"]
    assert expired["reference_ids"] == []
    assert expired["state"] == "retention-expired"
    assert expired["purge_eligible"] is True
    assert expired["post_purge_tombstone"] == {
        "parser_id": "future-parser",
        "parser_version": "fixture-0",
        "source_sha256": (
            "08ce21d34cc5efbbda589676c3d4b0fdcdcba0162c9afb80cef35bf09f3e4862"
        ),
        "artifact_sha256": "fixture-artifact-hash",
        "deletion_reason": "retention-expired",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", expired["extraction_identity"])

    held = artifacts["art-legal-hold-001"]
    assert held["reference_ids"] == []
    assert held["state"] == "retention-expired"
    assert held["legal_hold"] is True
    assert held["purge_eligible"] is False
    assert held["reason"] == "legal hold blocks purge"
