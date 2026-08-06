"""Non-copying segmentation view fixture checks for v3.2.

Segmentation views are derived records over canonical node IDs and spans. The
tests walk the frozen view records and fail if source text is copied into a
segment. Future T06 implementers use this as the acceptance boundary.
"""

from __future__ import annotations

import json


def test_segmentation_views_reference_ids_and_spans_not_copied_text(v3_2_fixture_root):
    """Assert every view segment references IDs/spans rather than copied text."""
    views = json.loads(
        (v3_2_fixture_root / "segmentation_views" / "views.json").read_text(
            encoding="utf-8"
        )
    )
    for view in views["views"]:
        for segment in view["segments"]:
            assert "text" not in segment
            assert "content" not in segment
            assert any(
                key in segment
                for key in (
                    "node_spans",
                    "retrieval_node_spans",
                    "seed",
                    "context",
                    "native_chunk_pointer",
                )
            )
