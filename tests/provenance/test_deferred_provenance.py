from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognityx_ingest import IngestService, PyMuPDFParser, SourceAssetRegistry
from cognityx_resource import ExecutionContext
from cognityx_storage import (
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "provenance_v1"


@pytest.mark.xfail(
    strict=True,
    reason="GAP-FIXTURE-SOURCES: related travel-policy fixtures are not supplied",
)
@pytest.mark.parametrize("version", ("v1", "v2"))
def test_p21_p22_related_travel_policy_fixture_is_available(version: str) -> None:
    assert (FIXTURE_ROOT / f"related_travel_policy_{version}.pdf").is_file()


@pytest.mark.xfail(
    strict=True,
    reason="GAP-BOUNDED-AMBIGUITY/P-24: travel-rule ambiguity remains deferred",
)
def test_p24_emits_bounded_travel_rule_ambiguity_task(
    tmp_path: Path, provenance_pdf: Path
) -> None:
    pytest.importorskip("fitz", reason="P-24 observation requires PyMuPDF")
    runtime = StorageRuntime.from_config(
        StorageConfig.built_in(root=tmp_path / "runtime")
    )
    registry = SourceAssetRegistry.load(runtime=runtime)
    storage = StorageClient(LocalStorageBackend(tmp_path / "artifacts")).for_shared_data()
    context = ExecutionContext(
        run_id="run-deferred-ambiguity",
        correlation_id="correlation-deferred-ambiguity",
        principal_id="provenance-test",
    )
    result = IngestService(
        storage, extractor=PyMuPDFParser(), registry=registry
    ).ingest(provenance_pdf, context=context, registry=registry)
    handoff = json.load(storage.open(result.provenance_key))

    assert any(
        item["target_text"] == "the relevant travel rule"
        and item["status"] == "ambiguous"
        and not item["gold"]
        for item in handoff["ambiguous"]
    )


@pytest.mark.xfail(
    strict=True,
    reason="GAP-FIXTURE-SOURCES: mixed native/scanned OCR fixture is not supplied",
)
def test_mixed_native_scanned_ocr_fixture_is_available() -> None:
    assert (FIXTURE_ROOT / "mixed_scanned_native.pdf").is_file()
