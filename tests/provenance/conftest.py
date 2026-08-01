from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "provenance_v1"


@pytest.fixture(scope="session")
def provenance_fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def provenance_pdf(provenance_fixture_root: Path) -> Path:
    return provenance_fixture_root / "main_policy_v2.pdf"


@pytest.fixture(scope="session")
def ground_truth(provenance_fixture_root: Path) -> dict[str, object]:
    path = provenance_fixture_root / "expected" / "ground_truth.json"
    return json.loads(path.read_text(encoding="utf-8"))
