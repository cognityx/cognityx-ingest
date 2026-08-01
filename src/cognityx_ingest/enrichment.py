"""Stable identities for reusable downstream representations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class KnowledgeUnit:
    knowledge_unit_id: str
    source_anchor_ids: tuple[str, ...]
    text: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalUnit:
    retrieval_unit_id: str
    knowledge_unit_id: str
    source_anchor_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Representation:
    representation_id: str
    retrieval_unit_id: str
    representation_type: str
    artifact_uri: str


@dataclass(frozen=True, slots=True)
class IndexBinding:
    binding_id: str
    representation_id: str
    index_type: str
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class EnrichmentIdentity:
    source_content_hash: str
    source_anchor_ids: tuple[str, ...]
    representation_type: str
    generation_method: str
    model_version: str | None
    configuration_hash: str

    @property
    def enrichment_id(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "enr-" + hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        source_content_hash: str,
        source_anchor_ids: tuple[str, ...],
        representation_type: str,
        generation_method: str,
        model_version: str | None,
        configuration: Mapping[str, Any],
    ) -> "EnrichmentIdentity":
        config = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        return cls(
            source_content_hash=source_content_hash,
            source_anchor_ids=tuple(sorted(source_anchor_ids)),
            representation_type=representation_type,
            generation_method=generation_method,
            model_version=model_version,
            configuration_hash=hashlib.sha256(config.encode()).hexdigest(),
        )


class EnrichmentCatalog:
    """Discover immutable enrichment metadata without generating it."""

    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def key(self, identity: EnrichmentIdentity) -> str:
        return f"ingest/enrichments/{identity.enrichment_id}.json"

    def find(self, identity: EnrichmentIdentity) -> dict[str, Any] | None:
        key = self.key(identity)
        if not self._storage.exists(key):
            return None
        with self._storage.open(key) as stream:
            return json.load(stream)

    def register(
        self, identity: EnrichmentIdentity, artifact_uri: str
    ) -> dict[str, Any]:
        record = {
            "schema": "cognityx.ingest.enrichment/v1",
            "enrichment_id": identity.enrichment_id,
            **asdict(identity),
            "artifact_uri": artifact_uri,
        }
        key = self.key(identity)
        if not self._storage.exists(key):
            self._storage.put_json(key, record)
        return record
