"""Strict trust-boundary tests for parser capability registry records."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    ParserCapabilityConflictError,
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


def _canonical_payload(v3_2_fixture_root) -> dict[str, object]:
    """Return valid lexical input independent of the frozen ordering exception."""
    payload = _payload(v3_2_fixture_root)
    payload["registry_version"] = "canonical-order-test"
    docling = next(
        item for item in payload["parsers"] if item["parser_id"] == "docling"
    )
    docling["conflicts"].append(
        {
            "capability": "tables",
            "advertised": True,
            "runtime_available": False,
            "resolution": "declared-but-currently-unavailable",
        }
    )
    payload["parsers"].sort(key=lambda item: item["parser_id"])
    for parser in payload["parsers"]:
        sources = parser["capability_sources"]
        discovered = sources["parser-discovered"]
        discovered["official_documentation"].sort(
            key=lambda item: item["evidence_id"]
        )
        discovered["capabilities"] = dict(
            sorted(discovered["capabilities"].items())
        )
        sources["human-guided"]["guidance"].sort(
            key=lambda item: (item["condition"], item["recommendation"])
        )
        sources["auto-learned"]["measurements"].sort(
            key=lambda item: (item["document_class"], item["metric"])
        )
        parser["conflicts"].sort(
            key=lambda item: (item["capability"], item["resolution"])
        )
    return payload


def _reverse_collection(payload: dict[str, object], collection: str) -> None:
    """Reverse one ordered identity collection without changing its values."""
    if collection == "parsers":
        payload["parsers"].reverse()
        return
    docling = next(
        item for item in payload["parsers"] if item["parser_id"] == "docling"
    )
    sources = docling["capability_sources"]
    if collection == "official_documentation":
        sources["parser-discovered"][collection].reverse()
    elif collection == "capabilities":
        capabilities = sources["parser-discovered"][collection]
        sources["parser-discovered"][collection] = dict(
            reversed(tuple(capabilities.items()))
        )
    elif collection == "guidance":
        sources["human-guided"][collection].reverse()
    elif collection == "measurements":
        sources["auto-learned"][collection].reverse()
    else:
        docling[collection].reverse()


def _json_bytes(payload: dict[str, object]) -> bytes:
    """Encode a test registry without sorting away intentionally supplied order."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


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


@pytest.mark.parametrize(
    "collection",
    (
        "parsers",
        "official_documentation",
        "capabilities",
        "guidance",
        "measurements",
        "conflicts",
    ),
)
@pytest.mark.parametrize("reader", ("dict", "json"))
def test_reversed_ordered_collections_fail_without_normalization(
    v3_2_fixture_root, collection: str, reader: str
) -> None:
    """Reject every reversed sequence through both public deserialization seams."""
    payload = _canonical_payload(v3_2_fixture_root)
    ParserCapabilityRegistry.from_dict(payload)
    _reverse_collection(payload, collection)

    with pytest.raises(ParserCapabilityValidationError, match="ordered"):
        if reader == "dict":
            ParserCapabilityRegistry.from_dict(payload)
        else:
            ParserCapabilityRegistry.from_json_bytes(_json_bytes(payload))


def test_frozen_registry_requires_its_complete_legacy_order_fingerprint(
    v3_2_fixture_root,
) -> None:
    """Admit the untouched fixture but reject a partial imitation of its ordering."""
    payload = _payload(v3_2_fixture_root)
    assert ParserCapabilityRegistry.from_dict(payload).registry_version == (
        "2026-08-06.1"
    )
    payload["parsers"][0]["capability_sources"]["parser-discovered"][
        "official_documentation"
    ].reverse()
    with pytest.raises(ParserCapabilityValidationError, match="ordered"):
        ParserCapabilityRegistry.from_dict(payload)


@pytest.mark.parametrize(
    ("needle", "replacement", "duplicate_key"),
    (
        (
            '"schema":"cognityx.ingest.parser-capability-registry/v3.2"',
            '"schema":"cognityx.ingest.parser-capability-registry/v3.2",'
            '"schema":"other"',
            "schema",
        ),
        (
            '"parser_id":"docling"',
            '"parser_id":"docling","parser_id":"other"',
            "parser_id",
        ),
        (
            '"tables":"declared"',
            '"tables":"declared","tables":"unsupported"',
            "tables",
        ),
        (
            '"installed":false',
            '"installed":false,"installed":true',
            "installed",
        ),
        (
            '"metric":"structure_recall"',
            '"metric":"structure_recall","metric":"other"',
            "metric",
        ),
    ),
)
def test_duplicate_json_keys_fail_at_every_nested_boundary(
    v3_2_fixture_root, needle: str, replacement: str, duplicate_key: str
) -> None:
    """Reject duplicate JSON names before last-key-wins decoding can hide facts."""
    source = _json_bytes(_canonical_payload(v3_2_fixture_root)).decode("utf-8")
    assert source.count(needle) >= 1
    malformed = source.replace(needle, replacement, 1).encode("utf-8")
    with pytest.raises(
        ParserCapabilityValidationError,
        match=rf"Duplicate JSON object key: {duplicate_key}",
    ):
        ParserCapabilityRegistry.from_json_bytes(malformed)


def test_same_json_field_names_in_separate_objects_remain_valid(
    v3_2_fixture_root,
) -> None:
    """Scope duplicate detection to one object rather than the whole document."""
    payload = _json_bytes(_canonical_payload(v3_2_fixture_root))
    registry = ParserCapabilityRegistry.from_json_bytes(payload)
    assert len(registry.parsers) == 3


def test_frozen_runtime_available_fact_matches_derived_probe(
    v3_2_fixture_root,
) -> None:
    """Keep the authoritative false runtime fact when its legacy probe derives false."""
    registry = ParserCapabilityRegistry.from_dict(_payload(v3_2_fixture_root))
    assert registry.get("docling").parser_discovered.runtime_probe.runtime_available is False


@pytest.mark.parametrize(
    ("runtime_probe", "runtime_available"),
    (
        ({"installed": False}, True),
        (
            {
                "plugin_registered": True,
                "dependency_importable": True,
                "installed_version": "1.0",
                "adapter_module": "example.parser",
                "adapter_class": "ExampleParser",
                "reason": None,
            },
            False,
        ),
        (
            {
                "plugin_registered": None,
                "dependency_importable": True,
                "installed_version": None,
                "adapter_module": None,
                "adapter_class": None,
                "reason": "Registration was not observed.",
            },
            True,
        ),
    ),
)
def test_runtime_available_conflicts_or_unknown_derivations_fail(
    v3_2_fixture_root,
    runtime_probe: dict[str, object],
    runtime_available: bool,
) -> None:
    """Reject legacy runtime facts that contradict or outrun the runtime probe."""
    payload = _payload(v3_2_fixture_root)
    discovered = payload["parsers"][0]["capability_sources"]["parser-discovered"]
    discovered["runtime_probe"] = runtime_probe
    discovered["capabilities"]["runtime_available"] = runtime_available
    with pytest.raises(ParserCapabilityConflictError, match="runtime_available"):
        ParserCapabilityRegistry.from_dict(payload)


def test_runtime_available_legacy_field_may_be_absent(v3_2_fixture_root) -> None:
    """Allow the runtime probe to remain the sole availability source."""
    payload = _payload(v3_2_fixture_root)
    capabilities = payload["parsers"][0]["capability_sources"][
        "parser-discovered"
    ]["capabilities"]
    capabilities.pop("runtime_available")
    assert ParserCapabilityRegistry.from_dict(payload).get("docling")
