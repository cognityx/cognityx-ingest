"""Runtime discovery tests for the production parser capability registry.

These tests prove discovery is read-only: registered adapters are inspected in
stable order, optional package facts remain separate, and no parser or network
operation is needed to construct the registry.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import socket

import pytest

from cognityx_ingest import (
    ExtractionPolicy,
    ExtractionResult,
    ParserCapabilityRegistry,
    ParserCapabilityValidationError,
    ParserRouter,
)
import cognityx_ingest.parser_capabilities as parser_capabilities


class _FutureParser:
    """Provide a registered custom parser with no package metadata for discovery."""

    name = "future-runtime"

    def __init__(self) -> None:
        """Initialize a call counter so discovery can prove it never executes."""
        self.calls = 0

    def extract_document(self, path: Path) -> ExtractionResult:
        """Return a stable empty result only when a test deliberately executes it."""
        self.calls += 1
        return ExtractionResult(pages=(), backend=self.name)


class _MalformedParser:
    """Expose an untrusted registration name and fail if extraction is invoked."""

    def __init__(self, name: object) -> None:
        """Store the supplied identity and initialize a no-execution counter."""
        self.name = name
        self.calls = 0

    def extract_document(self, path: Path) -> ExtractionResult:
        """Prove capability discovery never tests malformed adapters by execution."""
        self.calls += 1
        raise AssertionError("malformed parser extraction must not be called")


def test_default_router_registry_includes_all_registered_builtins() -> None:
    """Discover basic, PyMuPDF, and Docling adapters regardless of installation."""
    registry = ParserCapabilityRegistry.from_router(ParserRouter())
    assert tuple(record.parser_id for record in registry.list()) == (
        "basic",
        "docling",
        "pymupdf",
    )
    assert all(
        record.parser_discovered.runtime_probe.plugin_registered
        for record in registry.list()
    )


def test_missing_optional_dependency_does_not_remove_registered_parser(monkeypatch) -> None:
    """Retain Docling registration while recording its dependency as unavailable."""
    real_find_spec = parser_capabilities.importlib.util.find_spec

    def bounded_find_spec(name: str):
        """Simulate only Docling absence and delegate every other module lookup."""
        if name == "docling":
            return None
        return real_find_spec(name)

    monkeypatch.setattr(parser_capabilities.importlib.util, "find_spec", bounded_find_spec)
    docling = ParserCapabilityRegistry.from_router(ParserRouter()).get("docling")
    probe = docling.parser_discovered.runtime_probe
    assert probe.plugin_registered is True
    assert probe.dependency_importable is False
    assert probe.runtime_available is False


def test_custom_parser_remains_registered_with_unknown_package_facts() -> None:
    """Describe custom adapters honestly without inventing dependency or version data."""
    plugin = _FutureParser()
    record = ParserCapabilityRegistry.from_router(ParserRouter((plugin,))).get(
        plugin.name
    )
    probe = record.parser_discovered.runtime_probe
    assert probe.plugin_registered is True
    assert probe.dependency_importable is None
    assert probe.installed_version is None
    assert probe.adapter_class == "_FutureParser"
    assert plugin.calls == 0


def test_registry_discovery_never_invokes_parser_extraction() -> None:
    """Build repeatedly from a custom router while its extraction count stays zero."""
    plugin = _FutureParser()
    router = ParserRouter((plugin,))
    first = ParserCapabilityRegistry.from_router(router)
    second = ParserCapabilityRegistry.from_router(router)
    assert first == second
    assert plugin.calls == 0


@pytest.mark.parametrize("name", (None, 7, "", " parser-with-spaces "))
def test_malformed_plugin_names_raise_typed_validation_without_execution(
    name: object,
) -> None:
    """Reject non-string, empty, and untrimmed identities before indexing."""
    plugin = _MalformedParser(name)
    router = ParserRouter((plugin,))
    with pytest.raises(ParserCapabilityValidationError, match="parser plugin name"):
        ParserCapabilityRegistry.from_router(router)
    assert plugin.calls == 0


def test_mixed_valid_and_malformed_plugins_raise_typed_validation() -> None:
    """Validate the complete snapshot before sorting a mixed registration set."""
    valid = _FutureParser()
    malformed = _MalformedParser(None)
    router = ParserRouter((valid, malformed))
    with pytest.raises(ParserCapabilityValidationError, match="parser plugin name"):
        ParserCapabilityRegistry.from_router(router)
    assert valid.calls == 0
    assert malformed.calls == 0


def test_duplicate_plugin_names_fail_before_router_index_overwrite() -> None:
    """Retain duplicate registrations long enough for discovery to reject them."""
    first = _MalformedParser("duplicate")
    second = _MalformedParser("duplicate")
    router = ParserRouter((first, second))
    with pytest.raises(ParserCapabilityValidationError, match="Duplicate parser plugin"):
        ParserCapabilityRegistry.from_router(router)
    assert first.calls == 0
    assert second.calls == 0


def test_registry_creation_uses_no_network(monkeypatch) -> None:
    """Fail any socket construction while normal registry discovery still succeeds."""
    def fail_socket(*args, **kwargs):
        """Raise if production discovery attempts network access."""
        raise AssertionError("registry discovery must not open a socket")

    monkeypatch.setattr(socket, "socket", fail_socket)
    assert ParserCapabilityRegistry.from_router(ParserRouter()).get("basic")


def test_registry_discovery_does_not_change_extraction_results(tmp_path: Path) -> None:
    """Keep existing parser execution output unchanged after registry inspection."""
    plugin = _FutureParser()
    router = ParserRouter(
        (plugin,),
        policy=ExtractionPolicy(mode="fixed", backends=(plugin.name,)),
    )
    ParserCapabilityRegistry.from_router(router)
    result = router.extract_document(tmp_path / "unused.pdf")
    assert result == ExtractionResult(
        pages=(),
        backend=plugin.name,
        considered_backends=(plugin.name,),
        selected_reason="fixed_backend",
    )
    assert plugin.calls == 1


def test_runtime_records_are_frozen() -> None:
    """Reject mutation of live observations after publication."""
    probe = ParserCapabilityRegistry.from_router(ParserRouter()).get(
        "basic"
    ).parser_discovered.runtime_probe
    with pytest.raises(FrozenInstanceError):
        probe.reason = "changed"


def test_t03_introduces_no_route_or_score_api() -> None:
    """Keep registry evidence separate from the future T04 decision boundary."""
    registry = ParserCapabilityRegistry.from_router(ParserRouter())
    assert not hasattr(registry, "route")
    assert not hasattr(registry, "score")
    assert not hasattr(registry, "select_parser")
