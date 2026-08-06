"""Shared fixtures for the v3.2 T00 contract scaffold.

The module exists so every focused test reads the same frozen fixture tree and
the same tracked design-input directory. The design principle is explicit
fixture access: tests should state exactly which frozen input they use instead
of discovering files implicitly. These fixtures are used only by the v3.2 T00
tests, not by production ingest code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "v3_2_focused"
DESIGN_INPUT_ROOT = Path(__file__).resolve().parents[2] / "design_input" / "v3_2"


@pytest.fixture(scope="session")
def v3_2_fixture_root() -> Path:
    """Return the installed v3.2 delta fixture root used by focused tests."""
    return FIXTURE_ROOT


@pytest.fixture(scope="session")
def design_input_v3_2_root() -> Path:
    """Return the tracked frozen design-input directory for checksum tests."""
    return DESIGN_INPUT_ROOT


@pytest.fixture(scope="session")
def v3_2_manifest(v3_2_fixture_root: Path) -> dict[str, object]:
    """Load the fixture manifest once so tests share the same contract input."""
    path = v3_2_fixture_root / "fixture_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def provenance_fixture_root() -> Path:
    """Return the existing provenance v1 fixture root reused by v3.2."""
    return Path(__file__).resolve().parents[1] / "fixtures" / "provenance_v1"


@pytest.fixture(scope="session")
def provenance_pdf(provenance_fixture_root: Path) -> Path:
    """Return the frozen base PDF that T00 must reuse without duplication."""
    return provenance_fixture_root / "main_policy_v2.pdf"
