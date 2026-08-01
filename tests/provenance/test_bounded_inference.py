from __future__ import annotations

import json

from cognityx_ingest import (
    BoundedInferenceResolver,
    InferenceResolutionConfig,
    InferenceTarget,
    ResolutionTask,
)


class ProposalClient:
    def __init__(self, target: str, relation_type: str = "references") -> None:
        self.target = target
        self.relation_type = relation_type

    def chat(self, **_kwargs):
        return {
            "id": "fixture-proposal",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "target_anchor_id": self.target,
                                "relation_type": self.relation_type,
                                "confidence": 0.75,
                                "reason": "bounded fixture proposal",
                            }
                        )
                    }
                }
            ],
        }


def _resolver(client: ProposalClient) -> BoundedInferenceResolver:
    return BoundedInferenceResolver(
        InferenceResolutionConfig(
            targets=(InferenceTarget(model="fixture/model"),), max_calls=1
        ),
        client=client,
    )


def test_ambiguity_proposal_is_bounded_and_deterministically_validated(
    ground_truth: dict[str, object]
) -> None:
    expected = next(
        item
        for item in ground_truth["relations"]
        if item["id"] == "rel-ambiguous-travel"
    )
    task = ResolutionTask(
        task_id=expected["id"],
        source_anchor_id=expected["source"],
        relation_type=expected["type"],
        target_text=expected["literal"],
    )
    result = _resolver(ProposalClient("sec-10.2")).resolve(
        (task,),
        valid_anchor_ids=frozenset(
            {expected["source"], "sec-10.2", "travel-v2:sec-6.4"}
        ),
    )

    assert result.relations[0].target_anchor_id == "sec-10.2"
    assert result.relations[0].method == "cognityx-inference"
    assert result.decisions[0].status == "accepted"


def test_nonexistent_anchor_proposal_is_rejected(
    ground_truth: dict[str, object]
) -> None:
    task = ResolutionTask(
        task_id="fake-anchor-control",
        source_anchor_id="page-014:block-014",
        relation_type="references",
        target_text="the relevant travel rule",
    )
    result = _resolver(ProposalClient("invented-section")).resolve(
        (task,),
        valid_anchor_ids=frozenset({"page-014:block-014", "sec-10.2"}),
    )

    assert not result.relations
    assert result.decisions[0].status == "rejected"
    assert result.unresolved[0].reason == "target_anchor_not_found"
