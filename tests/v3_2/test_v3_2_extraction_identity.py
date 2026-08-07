"""Production tests for the frozen six-field T07 extraction identity.

These tests exercise the public immutable model rather than recomputing fixture
identities with missing data. They prove deterministic canonical JSON identity,
complete-field sensitivity, and rejection of secrets or run-local configuration.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cognityx_ingest.models import (
    EXTRACTION_IDENTITY_FIELDS,
    ExtractionIdentity,
    ExtractionIdentityError,
)


def _identity() -> ExtractionIdentity:
    """Return one complete production identity used by focused mutation tests."""
    return ExtractionIdentity.from_configuration(
        source_sha256="1" * 64,
        parser_id="docling",
        parser_version="2.5.0",
        parser_configuration={
            "adapter": "canonical-v3-2",
            "ocr": False,
            "pipeline": {"table_mode": "accurate"},
        },
        model_version="none",
        scope="tenant-a/policy",
    )


def test_frozen_identity_field_list_is_exact() -> None:
    """Keep the fixture's six components without adding top-level fields."""
    assert EXTRACTION_IDENTITY_FIELDS == (
        "source_sha256",
        "parser_id",
        "parser_version",
        "parser_configuration_hash",
        "model_version",
        "scope",
    )
    assert tuple(_identity().to_dict()) == EXTRACTION_IDENTITY_FIELDS


def test_production_identity_is_deterministic_and_order_independent() -> None:
    """Hash equal nested configuration identically regardless of mapping order."""
    first = _identity()
    second = ExtractionIdentity.from_configuration(
        source_sha256="1" * 64,
        parser_id="docling",
        parser_version="2.5.0",
        parser_configuration={
            "pipeline": {"table_mode": "accurate"},
            "ocr": False,
            "adapter": "canonical-v3-2",
        },
        model_version="none",
        scope="tenant-a/policy",
    )
    assert first == second
    assert first.digest == second.digest
    assert len(first.digest) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_sha256", "2" * 64),
        ("parser_id", "pymupdf"),
        ("parser_version", "2.5.1"),
        ("parser_configuration_hash", "3" * 64),
        ("model_version", "layout-model-v2"),
        ("scope", "tenant-a/contracts"),
    ),
)
def test_every_frozen_component_changes_identity(field: str, value: str) -> None:
    """Prove no one of the six execution facts is ignored by the digest."""
    original = _identity()
    assert replace(original, **{field: value}).digest != original.digest


@pytest.mark.parametrize("field", ("source_sha256", "parser_configuration_hash"))
def test_malformed_sha_is_rejected(field: str) -> None:
    """Reject non-lowercase or incomplete SHA values before any catalog access."""
    with pytest.raises(ExtractionIdentityError, match="lowercase SHA-256"):
        replace(_identity(), **{field: "ABC"})


@pytest.mark.parametrize(
    "configuration",
    (
        {"api_key": "secret"},
        {"apiKey": "secret"},
        {"APIKey": "secret"},
        {"apikey": "secret"},
        {"openai_api_key": "secret"},
        {"client-secret": "secret"},
        {"accessToken": "secret"},
        {"nested": {"run_id": "run-1"}},
        {"nested": {"correlationId": "cor-1"}},
        {"path": "/tmp/parser"},
        {"cache_dir": "/tmp/cache"},
        {"cache_dir": "tmp/parser-cache"},
        {"model_path": "/home/user/model"},
        {"outputPath": "C:\\temp\\x"},
        {"localPath": "C:\\models\\parser"},
        {"cache-directory": "../parser-cache"},
        {"score": float("nan")},
        {"unsupported": object()},
    ),
)
def test_configuration_rejects_secret_operational_or_non_json_values(
    configuration: dict[str, object],
) -> None:
    """Keep credentials, paths, run state, and unsafe values outside identity."""
    with pytest.raises(ExtractionIdentityError):
        ExtractionIdentity.from_configuration(
            source_sha256="1" * 64,
            parser_id="docling",
            parser_version="2.5.0",
            parser_configuration=configuration,
            model_version="none",
            scope="tenant-a/policy",
        )


@pytest.mark.parametrize(
    "configuration",
    (
        {"tokenizer": "cl100k_base"},
        {"tokenizer": "Qwen/Qwen3-8B"},
        {"model": "Qwen/Qwen3-8B"},
        {"model_id": "Qwen/Qwen3-8B"},
        {"model_path": "Qwen/Qwen3-8B"},
        {"parser_id": "docling"},
        {"adapter": "canonical-v3-2"},
        {"pipeline": "standard", "table_mode": "accurate"},
    ),
)
def test_configuration_allows_semantic_parser_settings(
    configuration: dict[str, object],
) -> None:
    """Keep real tokenizer and registry model identifiers in exact identity."""
    identity = ExtractionIdentity.from_configuration(
        source_sha256="1" * 64,
        parser_id="docling",
        parser_version="2.5.0",
        parser_configuration=configuration,
        model_version="Qwen/Qwen3-8B",
        scope="tenant-a/policy",
    )
    assert len(identity.parser_configuration_hash) == 64


@pytest.mark.parametrize(
    "configuration",
    (
        {"text": "x" * 4_097},
        {"items": list(range(1_025))},
    ),
)
def test_configuration_rejects_unbounded_identity_input(
    configuration: dict[str, object],
) -> None:
    """Bound canonicalization work before hashing untrusted configuration."""
    with pytest.raises(ExtractionIdentityError, match="too large"):
        ExtractionIdentity.from_configuration(
            source_sha256="1" * 64,
            parser_id="docling",
            parser_version="2.5.0",
            parser_configuration=configuration,
            model_version="none",
            scope="tenant-a/policy",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("parser_id", ""),
        ("parser_version", ""),
        ("model_version", ""),
        ("scope", ""),
        ("scope", "/tmp/local"),
        ("scope", "tenant/../other"),
    ),
)
def test_incomplete_or_local_identity_is_non_constructible(
    field: str, value: str
) -> None:
    """Make incomplete pre-parser identity non-reusable by construction."""
    with pytest.raises(ExtractionIdentityError):
        replace(_identity(), **{field: value})
