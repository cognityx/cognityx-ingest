from __future__ import annotations

import json


def test_segmentation_views_reference_ids_and_spans_not_copied_text(v3_2_fixture_root):
    views = json.loads((v3_2_fixture_root / "segmentation_views" / "views.json").read_text(encoding="utf-8"))
    for view in views["views"]:
        for segment in view["segments"]:
            assert "text" not in segment
            assert "content" not in segment
            assert segment["node_ids"]
            assert segment["spans"]
