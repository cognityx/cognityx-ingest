"""Command-line entrypoint for local PDF ingestion."""

from __future__ import annotations

import argparse
import json

from cognityx_storage import LocalStorageBackend, StorageClient

from cognityx_ingest.service import IngestService


def main() -> int:
    parser = argparse.ArgumentParser(prog="cognityx-ingest")
    parser.add_argument("path", help="A PDF file or folder of PDFs.")
    parser.add_argument("--storage-root", required=True, help="Local root used by cognityx-storage.")
    parser.add_argument("--owner-id", default="local")
    args = parser.parse_args()
    storage = StorageClient(LocalStorageBackend(args.storage_root)).for_shared_data()
    results = IngestService(storage).ingest_path(args.path, owner_id=args.owner_id)
    print(json.dumps([{"document_id": item.document.document_id, "manifest_key": item.manifest_key} for item in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
