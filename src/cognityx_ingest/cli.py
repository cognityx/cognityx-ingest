"""Command-line entrypoint for local PDF ingestion."""

from __future__ import annotations

import argparse
import json
import sys

from cognityx_storage import LocalStorageBackend, StorageClient

from cognityx_ingest.service import IngestService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cognityx-ingest")
    parser.add_argument("path", help="A PDF file or folder of PDFs.")
    parser.add_argument("--storage-root", required=True, help="Local root used by cognityx-storage.")
    parser.add_argument("--owner-id", default="local")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["ingest"]:
        arguments.pop(0)
    args = parser.parse_args(arguments)
    storage = StorageClient(LocalStorageBackend(args.storage_root)).for_shared_data()
    results = IngestService(storage).ingest_path(args.path, owner_id=args.owner_id)
    print(json.dumps([{"run_id": item.run_id, "job_id": item.job_id, "document_id": item.document.document_id, "manifest_key": item.manifest_key, "artifacts": [{"artifact_id": artifact.artifact_id, "uri": artifact.uri} for artifact in item.artifacts]} for item in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
