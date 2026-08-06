from __future__ import annotations

import pytest

from cognityx_ingest import DoclingParser, ParserRouter


@pytest.mark.xfail(strict=True, reason="T01: durable native-artifact store/read/reload seam is not implemented")
def test_native_artifact_store_read_reload_seam_is_exposed(provenance_pdf):
    result = DoclingParser().extract_document(provenance_pdf)
    assert result.raw_artifact is not None
    assert result.raw_artifacts.get("docling") == result.raw_artifact
    assert result.diagnostics.get("native_artifact_store")
    assert result.diagnostics.get("native_artifact_reload")


@pytest.mark.xfail(strict=True, reason="T03: parser capability registry seam is not implemented")
def test_parser_capability_registry_seam_is_exposed(provenance_pdf):
    router = ParserRouter()
    registry = router.capability_registry("docling")
    assert registry["allowed_capability_source_classes"] == ["parser-discovered", "human-guided", "auto-learned"]
