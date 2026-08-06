from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "v3_2_focused"


@pytest.fixture(scope="session")
def v3_2_fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def v3_2_manifest(v3_2_fixture_root: Path) -> dict[str, object]:
    path = v3_2_fixture_root / "fixture_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def provenance_fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "provenance_v1"


@pytest.fixture(scope="session")
def provenance_pdf(provenance_fixture_root: Path) -> Path:
    return provenance_fixture_root / "main_policy_v2.pdf"
