"""Optional semantic enhancement through the Cognityx inference boundary."""

from __future__ import annotations

import json
from typing import Any, Protocol


class InferenceClient(Protocol):
    def chat(self, *, model: str, messages: list[dict[str, str]], **parameters: Any) -> dict[str, Any]: ...


class SectionEnhancer:
    """Ask an approved Cognityx inference client for non-authoritative labels."""

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
            max_tokens=200,
        )
        choices = response.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        try:
            metadata = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            metadata = {"raw_response": content}
        return {"provider": "cognityx-inference", "model": self._model, "metadata": metadata}
