from __future__ import annotations

from cognityx_ingest.enhancement import SectionEnhancer


class FakeInferenceClient:
    def __init__(self) -> None:
        self.model = ""

    def chat(self, *, model: str, messages: list[dict[str, str]], **parameters: object) -> dict[str, object]:
        self.model = model
        return {"choices": [{"message": {"content": '{"title": "Example", "tags": ["pdf"]}'}}]}


def test_enhancer_records_inference_provenance() -> None:
    client = FakeInferenceClient()
    value = SectionEnhancer(client, "example-model").enhance(["source text"])

    assert client.model == "example-model"
    assert value["provider"] == "cognityx-inference"
    assert value["metadata"]["title"] == "Example"
