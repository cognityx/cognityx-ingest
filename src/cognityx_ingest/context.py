"""Resolve stable execution context from optional CLI configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cognityx_ingest.models import ExecutionContext

_CONTEXT_FIELDS = {"context_type", "principal_id", "tenant_id", "project_id", "workspace_id", "scopes"}
_FORBIDDEN_FIELDS = {"run_id", "correlation_id", "job_id", "tokens", "credentials"}


def resolve_execution_context(
    *,
    context_file: str | Path | None = None,
    context_type: str | None = None,
    principal_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
    scopes: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> ExecutionContext:
    """Resolve one selected base Context, then apply explicit overrides."""
    base_path = _select_context_file(context_file, cwd=cwd)
    base = _load_context_file(base_path) if base_path else {}
    values: dict[str, Any] = {
        "context_type": base.get("context_type", "user"),
        "principal_id": base.get("principal_id", "local"),
        "tenant_id": base.get("tenant_id"),
        "project_id": base.get("project_id"),
        "workspace_id": base.get("workspace_id"),
        "scopes": dict(base.get("scopes") or {}),
    }
    for key, value in {
        "context_type": context_type, "principal_id": principal_id,
        "tenant_id": tenant_id, "project_id": project_id,
        "workspace_id": workspace_id,
    }.items():
        if value is not None:
            values[key] = value
    values["scopes"].update(dict(scopes or {}))
    if values["context_type"] not in {"user", "system"}:
        raise ValueError("context_type must be 'user' or 'system'.")
    if values["context_type"] == "system" and values["principal_id"] == "local":
        values["principal_id"] = None
    return ExecutionContext(run_id=str(uuid4()), correlation_id=str(uuid4()), **values)


def _select_context_file(explicit: str | Path | None, *, cwd: str | Path | None) -> Path | None:
    candidates = [Path(explicit)] if explicit else []
    if not explicit and os.environ.get("COGNITYX_CONTEXT_FILE"):
        candidates.append(Path(os.environ["COGNITYX_CONTEXT_FILE"]))
    if not explicit and not os.environ.get("COGNITYX_CONTEXT_FILE"):
        candidates.append(Path(cwd or Path.cwd()) / ".cognityx" / "context.json")
        configured = os.environ.get("COGNITYX_USER_CONTEXT_FILE")
        candidates.append(Path(configured) if configured else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cognityx" / "context.json")
    for path in candidates:
        if path.is_file():
            return path
    if explicit:
        raise FileNotFoundError(f"Context file does not exist: {explicit}")
    return None


def _load_context_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Context JSON must be an object.")
    unknown = set(value) - _CONTEXT_FIELDS
    forbidden = set(value) & _FORBIDDEN_FIELDS
    if unknown or forbidden:
        raise ValueError(f"Context JSON contains unsupported fields: {', '.join(sorted(unknown | forbidden))}")
    scopes = value.get("scopes", {})
    if not isinstance(scopes, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in scopes.items()):
        raise ValueError("Context JSON scopes must be a string-to-string object.")
    return value
