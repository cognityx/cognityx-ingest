"""Strict trust-boundary tests for parser capability registry records."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    ParserCapabilityNotFoundError,
    ParserCapabilityRegistry,
    ParserCapabilityValidationError,
)


def _payload(v3_2_fixture_root) -> dict[str, object]:
    """Return a fresh mutable copy of the authoritative fixture for corruption tests."""
    return json.loads(
        (
            v3_2_fixture_root
            / "capability_registry"
            / "parser_capabilities.json"
        ).read_text(encoding="utf-8")
    )


def test_duplicate_parser_ids_fail(v3_2_fixture_root) -> None:
    """Reject parser identities before an index could overwrite evidence."""
    payload = _payload(v3_2_fixture_root)
    payload["parsers"].append(payload["parsers"][0])
    with pytest.raises(ParserCapabilityValidationError, match="Duplicate parser ID"):
        ParserCapabilityRegistry.from_dict(payload)


def test_duplicate_documentation_evidence_ids_fail(v3_2_fixture_root) -> None:
    """Reject repeated official evidence IDs across the whole registry."""
    payload = _payload(v3_2_fixture_root)
    evidence = payload["parsers"][0]["capability_sources"]["parser-discovered"][
        "official_documentation"
    ][0]
    payload["parsers"][1]["capability_sources"]["parser-discovered"][
        "official_documentation"
    ][0]["evidence_id"] = evidence["evidence_id"]
    with pytest.raises(
        ParserCapabilityValidationError, match="documentation evidence ID"
    ):
        ParserCapabilityRegistry.from_dict(payload)


def test_duplicate_capability_names_fail_direct_validation(v3_2_fixture_root) -> None:
    """Reject duplicate typed assertions even though JSON objects cannot repeat keys."""
    registry = ParserCapabilityRegistry.from_dict(_payload(v3_2_fixture_root))
    record = registry.get("docling")
    discovered = replace(
        record.parser_discovered,
        capabilities=(
            record.parser_discovered.capabilities[0],
            record.parser_discovered.capabilities[0],
        ),
    )
    invalid = replace(
        registry,
        parsers=tuple(
            replace(item, parser_discovered=discovered)
            if item.parser_id == record.parser_id
            else item
            for item in registry.parsers
        ),
    )
    with pytest.raises(ParserCapabilityValidationError, match="Duplicate capability"):
        invalid.validate()


def test_duplicate_measurement_identities_fail_direct_validation(v3_2_fixture_root) -> None:
    """Reject repeated document-class and metric pairs within one learned source."""
    registry = ParserCapabilityRegistry.from_dict(_payload(v3_2_fixture_root))
    record = registry.get("docling")
    measurement = record.auto_learned.measurements[0]
    invalid_record = replace(
        record,
        auto_learned=replace(
            record.auto_learned,
            measurements=(measurement, measurement),
        ),
    )
    invalid = replace(
        registry,
        parsers=tuple(
            invalid_record if item.parser_id == record.parser_id else item
            for item in registry.parsers
        ),
    )
    with pytest.raises(ParserCapabilityValidationError, match="Duplicate measurement"):
        invalid.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("source_url", "file:///tmp/evidence", "URL"),
        ("source_url", "http://localhost/evidence", "local"),
        ("retrieved_on", "2026-99-99", "date"),
    ),
)
def test_malformed_official_evidence_fails(
    v3_2_fixture_root, field: str, value: str, message: str
) -> None:
    """Reject local URLs and impossible retrieval dates at deserialization."""
    payload = _payload(v3_2_fixture_root)
    evidence = payload["parsers"][0]["capability_sources"]["parser-discovered"][
        "official_documentation"
    ][0]
    evidence[field] = value
    with pytest.raises(ParserCapabilityValidationError, match=message):
        ParserCapabilityRegistry.from_dict(payload)


@pytest.mark.parametrize(
    ("value", "sample_count", "message"),
    (
        (float("nan"), 1, "finite"),
        (float("inf"), 1, "finite"),
        (1.0, -1, "nonnegative"),
    ),
)
def test_invalid_measurements_fail(
    v3_2_fixture_root, value: float, sample_count: int, message: str
) -> None:
    """Reject non-finite metrics and negative sample counts without rescaling."""
    payload = _payload(v3_2_fixture_root)
    measurement = payload["parsers"][0]["capability_sources"]["auto-learned"][
        "measurements"
    ][0]
    measurement["value"] = value
    measurement["sample_count"] = sample_count
    with pytest.raises(ParserCapabilityValidationError, match=message):
        ParserCapabilityRegistry.from_dict(payload)


def test_invalid_runtime_version_and_unknown_fields_fail(v3_2_fixture_root) -> None:
    """Reject malformed canonical probes and unsupported nested fields."""
    registry = ParserCapabilityRegistry.from_dict(_payload(v3_2_fixture_root))
    payload = registry.to_dict()
    probe = payload["parsers"][0]["capability_sources"]["parser-discovered"][
        "runtime_probe"
    ]
    probe["installed_version"] = "2.0 invalid"
    with pytest.raises(ParserCapabilityValidationError, match="installed_version"):
        ParserCapabilityRegistry.from_dict(payload)

    payload = registry.to_dict()
    payload["unexpected"] = True
    with pytest.raises(ParserCapabilityValidationError, match="fields"):
        ParserCapabilityRegistry.from_dict(payload)


def test_unknown_parser_lookup_raises_typed_error(v3_2_fixture_root) -> None:
    """Translate missing IDs to ParserCapabilityNotFoundError rather than KeyError."""
    registry = ParserCapabilityRegistry.from_dict(_payload(v3_2_fixture_root))
    with pytest.raises(ParserCapabilityNotFoundError, match="unknown-parser"):
        registry.get("unknown-parser")


def test_invalid_schema_source_classes_and_routing_modes_fail(v3_2_fixture_root) -> None:
    """Pin the schema and both exact three-item contractual metadata tuples."""
    for field, value in (
        ("schema", "wrong"),
        ("allowed_capability_source_classes", ["parser-discovered"]),
        ("allowed_routing_modes", ["deterministic"]),
    ):
        payload = _payload(v3_2_fixture_root)
        payload[field] = value
        with pytest.raises(ParserCapabilityValidationError):
            ParserCapabilityRegistry.from_dict(payload)


def test_malformed_json_raises_typed_error() -> None:
    """Hide JSON and Unicode implementation errors behind the public error type."""
    with pytest.raises(ParserCapabilityValidationError):
        ParserCapabilityRegistry.from_json_bytes(b"{not-json")
