"""Frozen address catalog fidelity and strict shape validation tests."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cognityx_ingest import (
    PROVENANCE_ADDRESS_SCHEMA,
    PROVENANCE_RESOLUTION_STATUSES,
    EvidenceSetAddress,
    ProvenanceAddressCatalog,
    ProvenanceAddressValidationError,
    ProvenanceSelector,
    StrongProvenanceAddress,
)


def _catalog(v3_2_fixture_root) -> ProvenanceAddressCatalog:
    """Load the frozen catalog through the production strict reader."""
    return ProvenanceAddressCatalog.from_json_bytes(
        (v3_2_fixture_root / "expected" / "provenance_addresses.json").read_bytes(),
        compact_fixture=True,
    )


def test_frozen_address_catalog_loads_without_reordering(v3_2_fixture_root) -> None:
    """Preserve schema, all address families, member order, and status vocabulary."""
    expected = json.loads(
        (v3_2_fixture_root / "expected" / "provenance_addresses.json").read_text()
    )
    catalog = _catalog(v3_2_fixture_root)

    assert catalog.schema == PROVENANCE_ADDRESS_SCHEMA
    assert catalog.to_dict() == expected
    assert [item.address_id for item in catalog.strong_addresses] == [
        "addr-strong-pol-p2",
        "addr-strong-pol-p5",
        "addr-strong-auth-21",
    ]
    assert catalog.logical_addresses[0].address_id == (
        "addr-logical-policy-4.2-effective"
    )
    evidence = catalog.evidence_set_addresses[0]
    assert evidence.member_address_ids == (
        "addr-strong-pol-p2",
        "addr-strong-pol-p5",
        "addr-strong-auth-21",
    )
    assert catalog.resolver_outcomes == PROVENANCE_RESOLUTION_STATUSES


@pytest.mark.parametrize("source_path", ("/etc/passwd", "../secret", "a/../b", "C:\\x"))
def test_selector_source_path_rejects_absolute_or_traversal_forms(source_path) -> None:
    """Treat source paths as safe logical locators, never operating-system paths."""
    selector = ProvenanceSelector(
        selector_type="text-position",
        source_path=source_path,
        char_start=0,
        char_end=1,
    )
    with pytest.raises(ProvenanceAddressValidationError, match="relative logical"):
        selector.validate()


@pytest.mark.parametrize(("start", "end"), ((-1, 1), (3, 2), (0, None)))
def test_selector_ranges_require_complete_nonnegative_ordered_bounds(start, end) -> None:
    """Reject invalid text-position bounds without attempting source access."""
    selector = ProvenanceSelector(
        selector_type="text-position",
        source_path="sources/policy.md",
        char_start=start,
        char_end=end,
    )
    with pytest.raises(ProvenanceAddressValidationError, match="range"):
        selector.validate()


def test_duplicate_address_ids_across_families_are_rejected(v3_2_fixture_root) -> None:
    """Maintain one catalog-wide identity namespace for deterministic lookup."""
    catalog = _catalog(v3_2_fixture_root)
    duplicate = replace(
        catalog.logical_addresses[0],
        address_id=catalog.strong_addresses[0].address_id,
    )
    with pytest.raises(ProvenanceAddressValidationError, match="address IDs"):
        replace(
            catalog,
            logical_addresses=(duplicate,),
            compact_fixture=False,
        ).validate()


def test_evidence_set_requires_existing_unique_strong_members(v3_2_fixture_root) -> None:
    """Reject incomplete closure intent before a resolver can claim exact support."""
    catalog = _catalog(v3_2_fixture_root)
    missing = EvidenceSetAddress(
        address_id="addr-missing-member",
        claim_ids=("claim",),
        member_address_ids=("addr-not-present",),
    )
    with pytest.raises(ProvenanceAddressValidationError, match="not a strong"):
        replace(
            catalog,
            evidence_set_addresses=(missing,),
            compact_fixture=False,
        ).validate()


def test_duplicate_address_json_keys_and_unknown_fields_are_rejected(
    v3_2_fixture_root,
) -> None:
    """Keep catalog JSON closed and immune to duplicate-key overwrite."""
    with pytest.raises(ProvenanceAddressValidationError, match="duplicate JSON key"):
        ProvenanceAddressCatalog.from_json_bytes(
            b'{"schema":"a","schema":"b"}', compact_fixture=True
        )
    value = json.loads(
        (v3_2_fixture_root / "expected" / "provenance_addresses.json").read_text()
    )
    value["strong_addresses"][0]["text"] = "forbidden copy"
    with pytest.raises(ProvenanceAddressValidationError, match="fields"):
        ProvenanceAddressCatalog.from_json_bytes(
            json.dumps(value).encode(), compact_fixture=True
        )


def test_strong_address_requires_valid_hash_and_one_target(v3_2_fixture_root) -> None:
    """Reject malformed immutable identity before graph resolution begins."""
    address = _catalog(v3_2_fixture_root).strong_addresses[0]
    with pytest.raises(ProvenanceAddressValidationError, match="SHA-256"):
        replace(address, source_sha256="bad").validate()
    assert isinstance(address, StrongProvenanceAddress)
