"""Compatibility imports for the shared Cognityx resource context layer."""

from cognityx_resource import (
    ResourceContext,
    load_resource_context,
    resolve_execution_context,
)

__all__ = [
    "ResourceContext",
    "load_resource_context",
    "resolve_execution_context",
]
