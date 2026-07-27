"""Command-line entrypoint for local ingest and lifecycle management."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from cognityx_jobs import JobRepository
from cognityx_storage import (
    DEFAULT_STORAGE_ROOT,
    LocalStorageBackend,
    StorageClient,
    StorageConfig,
    StorageRuntime,
)

from cognityx_ingest.management import IngestManager
from cognityx_ingest.context import resolve_execution_context
from cognityx_ingest.service import IngestService
from cognityx_ingest.sources import SourceRegistry


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] not in {"ingest", "jobs", "documents", "artifacts", "bundles", "sources", "-h", "--help"}:
        arguments.insert(0, "ingest")

    parser = argparse.ArgumentParser(prog="cognityx-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Ingest a PDF file or directory.")
    ingest.add_argument("path", help="A PDF file or folder of PDFs.")
    _add_runtime_arguments(ingest)

    jobs = commands.add_parser("jobs", help="Inspect or cancel owned ingest jobs.")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    for name in ("list", "show", "cancel"):
        command = job_commands.add_parser(name)
        if name != "list":
            command.add_argument("job_id")
        _add_runtime_arguments(command)

    documents = commands.add_parser("documents", help="Inspect or delete canonical documents.")
    document_commands = documents.add_subparsers(dest="document_command", required=True)
    for name in ("list", "show", "delete"):
        command = document_commands.add_parser(name)
        if name != "list":
            command.add_argument("document_id")
        if name == "delete":
            command.add_argument("--yes", action="store_true", help="Confirm irreversible artifact deletion.")
        _add_runtime_arguments(command)

    artifacts = commands.add_parser("artifacts", help="Read one generated document artifact.")
    artifact_commands = artifacts.add_subparsers(dest="artifact_command", required=True)
    read = artifact_commands.add_parser("read")
    read.add_argument("document_id")
    read.add_argument("name", choices=("source", "document", "evidence", "manifest"))
    _add_runtime_arguments(read)

    bundles = commands.add_parser("bundles", help="Manage logical source bundles.")
    bundle_commands = bundles.add_subparsers(dest="bundle_command", required=True)
    for name in ("list", "create", "locate"):
        command = bundle_commands.add_parser(name)
        if name == "create": command.add_argument("path")
        if name == "locate": command.add_argument("bundle_id")
        _add_runtime_arguments(command, source_storage=True)

    sources = commands.add_parser("sources", help="Register and inspect durable source files.")
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    source_add = source_commands.add_parser("add")
    source_add.add_argument("file")
    source_add.add_argument("--bundle")
    _add_runtime_arguments(source_add, source_storage=True)
    source_list = source_commands.add_parser("list")
    source_list.add_argument("--bundle")
    _add_runtime_arguments(source_list, source_storage=True)
    source_show = source_commands.add_parser("show")
    source_show.add_argument("source_id")
    _add_runtime_arguments(source_show, source_storage=True)
    source_locate = source_commands.add_parser("locate")
    source_locate.add_argument("source_id")
    _add_runtime_arguments(source_locate, source_storage=True)

    args = parser.parse_args(arguments)
    context = _context(args)

    if args.command in {"bundles", "sources"}:
        runtime = _source_runtime(args)
        registry = SourceRegistry(runtime, _source_catalog_path(args, runtime))
        if args.command == "bundles":
            if args.bundle_command == "list":
                _write([_plain(item) for item in registry.list_bundles(context)])
            elif args.bundle_command == "locate":
                _write(registry.locate_bundle(context, args.bundle_id))
            else:
                _write(_plain(registry.resolve_bundle(context, args.path, create=True)))
            return 0
        if args.source_command == "add":
            _write(_plain(registry.register_file(context, args.file, bundle=args.bundle)))
        elif args.source_command == "list":
            _write([_plain(item) for item in registry.list_sources(context, bundle=args.bundle)])
        elif args.source_command == "show":
            _write(_plain(registry.show_source(context, args.source_id)))
        else:
            _write(_plain(registry.locate_source(context, args.source_id)))
        return 0

    storage, repository = _runtime(args)
    if args.command == "ingest":
        results = IngestService(storage, jobs=repository).ingest_path(args.path, owner_id=args.owner_id, context=context)
        _write([_result_json(item) for item in results])
        return 0

    manager = IngestManager(storage, repository)
    if args.command == "jobs":
        if args.job_command == "list":
            _write(manager.list_jobs(context, owner_id=args.owner_id))
        elif args.job_command == "show":
            _write(manager.show_job(context, args.job_id, owner_id=args.owner_id))
        else:
            _write(manager.request_cancel(context, args.job_id, owner_id=args.owner_id))
        return 0
    if args.command == "documents":
        if args.document_command == "list":
            _write(manager.list_documents(context))
        elif args.document_command == "show":
            _write(manager.show_document(context, args.document_id))
        else:
            if not args.yes:
                parser.error("documents delete requires --yes.")
            manager.delete_document(context, args.document_id)
            _write({"deleted_document_id": args.document_id})
        return 0

    payload = manager.read_artifact(context, args.document_id, args.name)
    _write(_artifact_json(args.name, payload))
    return 0


def _add_runtime_arguments(
    parser: argparse.ArgumentParser, *, source_storage: bool = False
) -> None:
    if source_storage:
        selection = parser.add_mutually_exclusive_group()
        selection.add_argument(
            "--storage-root",
            help="Local-development shortcut for a built-in filesystem Storage Runtime.",
        )
        selection.add_argument(
            "--storage-config",
            help="Explicit Cognityx Storage Runtime TOML configuration.",
        )
        parser.add_argument(
            "--catalog-path",
            help="Explicit Source catalog path; required when no local root can be derived.",
        )
    else:
        parser.add_argument("--storage-root", default=str(DEFAULT_STORAGE_ROOT), help="Local root used by cognityx-storage.")
    parser.add_argument("--jobs-database", help="SQLite job database; defaults beneath the storage root.")
    parser.add_argument("--owner-id", default="local", help="Owner scope for lifecycle commands.")
    parser.add_argument("--context", help="JSON file defining the base Cognityx context.")
    parser.add_argument("--context-type", choices=("user", "system"))
    parser.add_argument("--principal-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--project-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--scope", action="append", default=[], metavar="KEY=VALUE")


def _runtime(args: argparse.Namespace) -> tuple[StorageClient, JobRepository]:
    storage_root = Path(args.storage_root)
    database = Path(args.jobs_database) if args.jobs_database else storage_root / ".cognityx-ingest" / "jobs.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    return StorageClient(LocalStorageBackend(storage_root)).for_shared_data(), JobRepository(str(database))


def _source_runtime(args: argparse.Namespace) -> StorageRuntime:
    if args.storage_root:
        return StorageRuntime.from_config(
            StorageConfig.built_in(root=args.storage_root)
        )
    return StorageRuntime.load(config_file=args.storage_config)


def _source_catalog_path(
    args: argparse.Namespace, runtime: StorageRuntime
) -> Path:
    if args.catalog_path:
        return Path(args.catalog_path)
    store = runtime.for_role("source_asset")
    profile = runtime.config.profiles[store.profile_name]
    root = profile.options.get("root")
    if profile.type != "filesystem" or not isinstance(root, str) or not root:
        raise ValueError(
            "A --catalog-path is required when the resolved source_asset "
            "profile has no local filesystem root."
        )
    return Path(root) / ".cognityx-ingest" / "source_catalog.sqlite3"


def _context(args: argparse.Namespace):
    scopes: dict[str, str] = {}
    for item in args.scope:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise ValueError("--scope must use KEY=VALUE.")
        scopes[key] = value
    return resolve_execution_context(
        context_file=args.context, context_type=args.context_type,
        principal_id=args.principal_id if args.principal_id is not None else (args.owner_id if args.command == "jobs" else None),
        tenant_id=args.tenant_id, project_id=args.project_id,
        workspace_id=args.workspace_id, scopes=scopes,
    )


def _result_json(result: object) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "job_id": result.job_id,
        "document_id": result.document.document_id,
        "manifest_key": result.manifest_key,
        "artifacts": [{"artifact_id": artifact.artifact_id, "uri": artifact.uri} for artifact in result.artifacts],
    }


def _artifact_json(name: str, payload: bytes) -> dict[str, object]:
    try:
        return {"artifact": name, "encoding": "utf-8", "content": payload.decode("utf-8")}
    except UnicodeDecodeError:
        return {"artifact": name, "encoding": "base64", "content": base64.b64encode(payload).decode("ascii")}


def _write(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _plain(value: object) -> object:
    from dataclasses import asdict, is_dataclass

    return asdict(value) if is_dataclass(value) else value


if __name__ == "__main__":
    raise SystemExit(main())
