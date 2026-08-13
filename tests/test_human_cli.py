import json

from cognityx_ingest import cli
from cognityx_ingest.human import render_human


def test_human_renderer_preserves_full_values_and_empty_lists() -> None:
    assert render_human([]) == "No records."
    uri = "storage://local-main/artifacts/documents/full/provenance.json"
    output = render_human([{"document_id": "document-full-id", "uri": uri}])
    assert "document-full-id" in output
    assert uri in output
    assert "\x1b" not in output


def test_compatibility_list_human_invokes_registry_once(monkeypatch, capsys) -> None:
    calls = 0

    class Registry:
        def list_sources(self, context, *, bundle):
            nonlocal calls
            calls += 1
            return [{"source_id": "source-full-id", "status": "ready"}]

    monkeypatch.setattr(cli, "_context", lambda args: object())
    monkeypatch.setattr(cli, "_source_runtime", lambda args: object())
    monkeypatch.setattr(
        cli.SourceAssetRegistry,
        "load",
        lambda **kwargs: Registry(),
    )

    assert cli.main(["sources", "list", "--human"]) == 0
    assert calls == 1
    output = capsys.readouterr()
    assert "source-full-id" in output.out
    assert not output.out.lstrip().startswith("[")
    assert "retained for compatibility" in output.err


def test_follow_events_keeps_default_ndjson_and_human_is_incremental(
    capsys,
) -> None:
    event = {"sequence": 7, "event": "completed", "job_id": "job-full-id"}

    class Manager:
        def __init__(self):
            self.event_calls = 0

        def job_events(self, context, job_id, *, owner_id, after):
            self.event_calls += 1
            return [event]

        def show_job(self, context, job_id, *, owner_id):
            return {"job": {"state": "completed"}}

    machine = Manager()
    cli._follow_job_events(
        machine, object(), "job-full-id", owner_id="owner", human=False
    )
    assert capsys.readouterr().out == json.dumps(event, sort_keys=True) + "\n"
    assert machine.event_calls == 1

    readable = Manager()
    cli._follow_job_events(
        readable, object(), "job-full-id", owner_id="owner", human=True
    )
    output = capsys.readouterr().out
    assert "Sequence: 7" in output
    assert "job-full-id" in output
    assert readable.event_calls == 1


def test_artifact_human_labels_utf8_and_binary_without_transforming_content(
    capsys,
) -> None:
    cli._write_artifact_human("provenance", b"first line\nsecond line")
    assert capsys.readouterr().out == (
        "Artifact: provenance\nEncoding: utf-8\nContent:\nfirst line\nsecond line\n"
    )

    cli._write_artifact_human("source-graph", b"\xff\x00")
    assert capsys.readouterr().out == (
        "Artifact: source-graph\nEncoding: base64\nContent:\n/wA=\n"
    )
