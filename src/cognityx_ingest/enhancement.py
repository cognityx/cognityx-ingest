"""Bounded, non-authoritative resolution through Cognityx Inference."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Any, Mapping, Protocol
from uuid import uuid4

from cognityx_ingest.models import DecisionRecord, Relation, UnresolvedItem


class InferenceClient(Protocol):
    def chat(
        self, *, model: str, messages: list[dict[str, str]], **parameters: Any
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InferenceTarget:
    model: str
    provider: str = "local"
    backend: str = "vllm"
    profile: str = "bf16"
    server_profile: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceResolutionConfig:
    """Operator-approved bounds for provenance proposals."""

    targets: tuple[InferenceTarget, ...]
    base_url: str | None = None
    manager_url: str | None = None
    auto_start_local: bool = False
    discovery_policy: str = "require_existing"
    startup_timeout_seconds: float = 600
    request_timeout_seconds: float = 120
    max_output_tokens: int = 400
    max_calls: int = 8
    prompt_version: str = "cognityx.ingest.resolve/v1"
    allowed_relation_types: tuple[str, ...] = (
        "references",
        "continues",
        "links_to",
        "describes",
    )
    data_classification: str = "internal"
    permit_external_sensitive_data: bool = False

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("At least one inference target is required.")
        if self.max_calls < 1 or self.max_output_tokens < 1:
            raise ValueError("Inference bounds must be positive.")

    @classmethod
    def load(cls, path: str | Path) -> "InferenceResolutionConfig":
        value = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        section = value.get("inference", value)
        raw_targets = section.get("targets") or [section.get("target", section)]
        targets = tuple(
            InferenceTarget(
                model=str(item["model"]),
                provider=str(item.get("provider", "local")),
                backend=str(item.get("backend", "vllm")),
                profile=str(item.get("profile", "bf16")),
                server_profile=(
                    str(item["server_profile"])
                    if item.get("server_profile") is not None
                    else None
                ),
            )
            for item in raw_targets
        )
        fields = {
            key: section[key]
            for key in (
                "base_url",
                "manager_url",
                "auto_start_local",
                "discovery_policy",
                "startup_timeout_seconds",
                "request_timeout_seconds",
                "max_output_tokens",
                "max_calls",
                "prompt_version",
                "data_classification",
                "permit_external_sensitive_data",
            )
            if key in section
        }
        if "allowed_relation_types" in section:
            fields["allowed_relation_types"] = tuple(section["allowed_relation_types"])
        return cls(targets=targets, **fields)

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolutionTask:
    task_id: str
    source_anchor_id: str
    relation_type: str
    target_text: str | None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    relations: tuple[Relation, ...]
    decisions: tuple[DecisionRecord, ...]
    unresolved: tuple[UnresolvedItem, ...]


class BoundedInferenceResolver:
    """Request proposals, then accept only deterministically valid anchors."""

    def __init__(
        self,
        config: InferenceResolutionConfig,
        *,
        client: InferenceClient | None = None,
    ) -> None:
        self.config = config
        self._injected_client = client
        self._clients: dict[tuple[str, str | None], InferenceClient] = {}

    def resolve(
        self,
        tasks: tuple[ResolutionTask, ...],
        *,
        valid_anchor_ids: frozenset[str],
        execution_context: Mapping[str, Any] | None = None,
    ) -> ResolutionResult:
        relations: list[Relation] = []
        decisions: list[DecisionRecord] = []
        unresolved: list[UnresolvedItem] = []
        for index, task in enumerate(tasks):
            if index >= self.config.max_calls:
                unresolved.append(_unresolved(task, "inference_call_limit_reached"))
                continue
            target, client, preflight = self._select_target()
            decision_id = f"decision-{uuid4().hex}"
            lineage = {
                "task_id": task.task_id,
                "source_anchor_id": task.source_anchor_id,
                "prompt_version": self.config.prompt_version,
                **dict(execution_context or {}),
            }
            try:
                response = client.chat(
                    model=target.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Propose one provenance relation as JSON. Do not invent "
                                "anchors. Return target_anchor_id, relation_type, confidence, "
                                "and reason."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "task": asdict(task),
                                    "allowed_anchor_ids": sorted(valid_anchor_ids),
                                    "allowed_relation_types": list(
                                        self.config.allowed_relation_types
                                    ),
                                },
                                sort_keys=True,
                            ),
                        },
                    ],
                    provider=target.provider,
                    backend=target.backend,
                    profile=target.profile,
                    temperature=0,
                    max_output_tokens=self.config.max_output_tokens,
                    response_format={"type": "json_object"},
                    timeout_seconds=self.config.request_timeout_seconds,
                    discovery_policy=self.config.discovery_policy,
                    execution_context=lineage,
                    request_metadata={
                        **lineage,
                        "purpose": "ingest_provenance_resolution",
                    },
                )
                proposal = _response_json(response)
                reason = _validate_proposal(
                    proposal,
                    task,
                    valid_anchor_ids,
                    frozenset(self.config.allowed_relation_types),
                )
                metadata = _response_metadata(response)
                if reason is not None:
                    unresolved.append(_unresolved(task, reason))
                    decisions.append(
                        _decision(
                            decision_id,
                            task,
                            target,
                            self.config,
                            metadata,
                            preflight,
                            status="rejected",
                            reason=reason,
                        )
                    )
                    continue
                confidence = float(proposal.get("confidence", 0.0))
                relations.append(
                    Relation(
                        relation_id=f"relation-{uuid4().hex}",
                        source_anchor_id=task.source_anchor_id,
                        target_anchor_id=str(proposal["target_anchor_id"]),
                        relation_type=str(proposal["relation_type"]),
                        status="inferred",
                        target_text=task.target_text,
                        method="cognityx-inference",
                        confidence=max(0.0, min(1.0, confidence)),
                        decision_id=decision_id,
                    )
                )
                decisions.append(
                    _decision(
                        decision_id,
                        task,
                        target,
                        self.config,
                        metadata,
                        preflight,
                        status="accepted",
                        reason=str(proposal.get("reason") or "validated proposal"),
                        confidence=confidence,
                    )
                )
            except Exception as exc:
                unresolved.append(_unresolved(task, f"inference_failed:{type(exc).__name__}"))
                decisions.append(
                    _decision(
                        decision_id,
                        task,
                        target,
                        self.config,
                        {},
                        preflight,
                        status="failed",
                        reason=type(exc).__name__,
                    )
                )
        return ResolutionResult(tuple(relations), tuple(decisions), tuple(unresolved))

    def select(
        self, path: Path, candidates: tuple[str, ...], facts: Mapping[str, Any]
    ) -> tuple[str, str]:
        """Bounded parser selection; the result must remain in the allowlist."""
        if not candidates:
            raise ValueError("No parser candidates were supplied.")
        task = ResolutionTask(
            task_id=f"parser-{uuid4().hex}",
            source_anchor_id="document",
            relation_type="select_parser",
            target_text=None,
            context={"path_suffix": path.suffix.lower(), **dict(facts)},
        )
        target, client, _ = self._select_target()
        response = client.chat(
            model=target.model,
            messages=[
                {
                    "role": "system",
                    "content": "Select one parser from the supplied allowlist as JSON.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": asdict(task), "allowed_parsers": list(candidates)},
                        sort_keys=True,
                    ),
                },
            ],
            provider=target.provider,
            backend=target.backend,
            profile=target.profile,
            temperature=0,
            max_output_tokens=min(100, self.config.max_output_tokens),
            response_format={"type": "json_object"},
        )
        value = _response_json(response)
        selected = str(value.get("parser", ""))
        if selected not in candidates:
            raise ValueError("Inference selected a parser outside the allowlist.")
        return selected, str(value.get("reason") or "bounded_inference_selection")

    def _select_target(self) -> tuple[InferenceTarget, InferenceClient, dict[str, Any]]:
        errors: list[str] = []
        for target in self.config.targets:
            if (
                target.provider != "local"
                and self.config.data_classification
                in {"sensitive", "restricted", "confidential"}
                and not self.config.permit_external_sensitive_data
            ):
                errors.append(f"{target.provider}:blocked_by_data_classification")
                continue
            client = self._client_for(target)
            if target.provider == "local":
                return target, client, {"verification_source": "local_server_profile"}
            try:
                statuses = client.provider_status()  # type: ignore[attr-defined]
                status = next(
                    (item for item in statuses if item.get("provider") == target.provider),
                    None,
                )
                if not status or not status.get("enabled"):
                    raise RuntimeError("provider unavailable")
                capabilities = client.provider_capabilities(  # type: ignore[attr-defined]
                    target.provider, target.model
                )
                return target, client, capabilities
            except Exception as exc:
                errors.append(f"{target.provider}:{type(exc).__name__}")
        raise RuntimeError("No approved inference target is available: " + ", ".join(errors))

    def _client_for(self, target: InferenceTarget) -> InferenceClient:
        if self._injected_client is not None:
            return self._injected_client
        key = (target.provider, target.server_profile)
        if key not in self._clients:
            try:
                from cognityx_inference import CognityxInferenceClient
            except ImportError as exc:
                raise RuntimeError(
                    "Inference resolution requires cognityx-ingest[inference]."
                ) from exc
            local = target.provider == "local"
            self._clients[key] = CognityxInferenceClient(
                self.config.base_url,
                manager_url=self.config.manager_url,
                backend="local" if local else None,
                profile=target.server_profile if local else None,
                auto_start=bool(local and self.config.auto_start_local),
                discovery_policy=self.config.discovery_policy,
                timeout_seconds=self.config.request_timeout_seconds,
                startup_timeout_seconds=self.config.startup_timeout_seconds,
            )
        return self._clients[key]


class SectionEnhancer:
    """Compatibility wrapper for the original non-authoritative labels."""

    def __init__(self, client: InferenceClient, model: str) -> None:
        self._client = client
        self._model = model

    def enhance(self, page_text: list[str]) -> dict[str, Any]:
        response = self._client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": "Return concise JSON metadata only."},
                {"role": "user", "content": "Suggest a title and tags for: " + "\n".join(page_text)},
            ],
            temperature=0,
            max_output_tokens=200,
        )
        return {
            "provider": "cognityx-inference",
            "model": self._model,
            "metadata": _response_json(response, preserve_raw=True),
        }


def load_resolution_config(path: str | Path | None = None) -> InferenceResolutionConfig | None:
    selected = str(path or os.environ.get("COGNITYX_INGEST_INFERENCE_CONFIG", "")).strip()
    return InferenceResolutionConfig.load(selected) if selected else None


def _response_json(response: Mapping[str, Any], *, preserve_raw: bool = False) -> dict[str, Any]:
    choices = response.get("choices") or ()
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        if preserve_raw:
            return {"raw_response": content}
        raise ValueError("Inference response was not valid JSON.") from None
    if not isinstance(value, dict):
        raise ValueError("Inference response must be one JSON object.")
    return value


def _validate_proposal(
    proposal: Mapping[str, Any],
    task: ResolutionTask,
    valid_anchor_ids: frozenset[str],
    allowed_relation_types: frozenset[str],
) -> str | None:
    target = proposal.get("target_anchor_id")
    relation_type = proposal.get("relation_type")
    if task.source_anchor_id not in valid_anchor_ids:
        return "source_anchor_not_found"
    if not isinstance(target, str) or target not in valid_anchor_ids:
        return "target_anchor_not_found"
    if not isinstance(relation_type, str) or relation_type not in allowed_relation_types:
        return "relation_type_not_allowed"
    return None


def _response_metadata(response: Mapping[str, Any]) -> dict[str, Any]:
    cognityx = response.get("cognityx") or {}
    return {
        "request_id": response.get("id") or cognityx.get("request_id"),
        "usage": response.get("usage") or cognityx.get("usage") or {},
        "timings": cognityx.get("timings") or {},
    }


def _decision(
    decision_id: str,
    task: ResolutionTask,
    target: InferenceTarget,
    config: InferenceResolutionConfig,
    metadata: Mapping[str, Any],
    preflight: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    confidence: float | None = None,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        task_id=task.task_id,
        status=status,
        method="bounded_llm_proposal_with_deterministic_validation",
        considered_tools=tuple(item.provider for item in config.targets),
        invoked_tools=(target.provider,),
        selected_tool=target.provider,
        selected_reason=str(preflight.get("verification_source") or "configured_order"),
        provider=target.provider,
        model=target.model,
        backend=target.backend if target.provider == "local" else None,
        profile=target.profile if target.provider == "local" else None,
        server_profile=target.server_profile if target.provider == "local" else None,
        request_id=metadata.get("request_id"),
        prompt_version=config.prompt_version,
        configuration_hash=config.digest(),
        usage=dict(metadata.get("usage") or {}),
        timings=dict(metadata.get("timings") or {}),
        confidence=confidence,
        reason=reason,
    )


def _unresolved(task: ResolutionTask, reason: str) -> UnresolvedItem:
    return UnresolvedItem(
        task_id=task.task_id,
        source_anchor_id=task.source_anchor_id,
        relation_type=task.relation_type,
        target_text=task.target_text,
        reason=reason,
    )
