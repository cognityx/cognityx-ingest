"""Provide the compatibility-only Ingest command-line application boundary.

This module preserves historical ``cognityx-ingest`` workflows while directing
new users to the primary SDK-owned ``cogni`` command.  It parses local Resource
and Storage overrides, constructs existing Ingest services/managers, and renders
JSON without reimplementing parser, lifecycle, authorization, or persistence
algorithms.  Artifact reads reuse Ingest's closed vocabulary and manager-owned
document-plus-artifact authorization before any bytes are opened.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import warnings
from datetime import timedelta
from functools import partial
from pathlib import Path

from cognityx_jobs import JobRepository
from cognityx_storage import (
    StorageConfig,
    StorageRuntime,
)

from cognityx_ingest.cleanup import SourceAssetCleanupService
from cognityx_ingest.context import resolve_execution_context
from cognityx_ingest.human import render_human
from cognityx_ingest.management import ARTIFACT_READ_NAMES, IngestManager
from cognityx_ingest.models import SourceAssetBatchResult
from cognityx_ingest.service import IngestService
from cognityx_ingest.source_assets import SourceAssetRegistry


def main(argv: list[str] | None = None) -> int:
    """Run one compatibility CLI invocation through public Ingest components.

    Console entry points and tests call this function with process or explicit
    arguments.  It always emits the migration warning, parses a deterministic
    command tree, resolves one execution context, composes only the service needed
    by the command, and delegates policy/persistence to Ingest, Jobs, and Storage.
    Artifact choices come directly from ``ARTIFACT_READ_NAMES`` and bytes are read
    only through ``IngestManager.read_artifact``.  Argparse raises typed syntax
    exits, component authorization/validation failures propagate, mutating actions
    retain explicit confirmation, and no global mutable state survives a call.
    """
    warnings.warn(
        "The cognityx-ingest CLI is retained for compatibility; use the cogni CLI.",
        FutureWarning,
        stacklevel=2,
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    known_commands = {
        "ingest",
        "jobs",
        "documents",
        "runs",
        "artifacts",
        "assets",
        "doc-bundles",
        "sources",
        "bundles",
        "cleanup",
        "-h",
        "--help",
    }
    if arguments and arguments[0] not in known_commands:
        arguments.insert(0, "ingest")

    parser = argparse.ArgumentParser(prog="cognityx-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Ingest a PDF file or directory.")
    selection = ingest.add_mutually_exclusive_group(required=True)
    selection.add_argument("path", nargs="?", help="A PDF file or folder of PDFs.")
    selection.add_argument("--asset", help="An existing SourceAsset ID.")
    selection.add_argument("--bundle", help="An existing DocBundle ID.")
    _add_runtime_arguments(ingest, source_storage=True)

    jobs = commands.add_parser("jobs", help="Inspect or cancel owned ingest jobs.")
    job_commands = jobs.add_subparsers(dest="job_command", required=True)
    for name in ("list", "show", "cancel", "events"):
        command = job_commands.add_parser(name)
        if name != "list":
            command.add_argument("job_id")
        if name == "events":
            command.add_argument("--follow", action="store_true")
        _add_runtime_arguments(command)

    documents = commands.add_parser(
        "documents", help="Inspect or delete canonical documents."
    )
    document_commands = documents.add_subparsers(dest="document_command", required=True)
    for name in ("list", "show", "delete"):
        command = document_commands.add_parser(name)
        if name != "list":
            command.add_argument("document_id")
        if name == "delete":
            command.add_argument(
                "--yes",
                action="store_true",
                help="Confirm irreversible artifact deletion.",
            )
        _add_runtime_arguments(command)

    runs = commands.add_parser("runs", help="Inspect or delete generated ingest runs.")
    run_commands = runs.add_subparsers(dest="run_command", required=True)
    for name in ("list", "show", "delete"):
        command = run_commands.add_parser(name)
        if name != "list":
            command.add_argument("run_id")
        if name == "delete":
            command.add_argument("--yes", action="store_true")
        _add_runtime_arguments(command)

    artifacts = commands.add_parser(
        "artifacts", help="Read one generated document artifact."
    )
    artifact_commands = artifacts.add_subparsers(dest="artifact_command", required=True)
    read = artifact_commands.add_parser("read")
    read.add_argument("document_id")
    read.add_argument("name", choices=ARTIFACT_READ_NAMES)
    _add_runtime_arguments(read)

    _add_doc_bundle_commands(
        commands,
        "doc-bundles",
        help_text="Manage logical DocBundles.",
    )
    _add_asset_commands(
        commands,
        "assets",
        help_text="Register and inspect durable SourceAssets.",
    )
    _add_doc_bundle_commands(
        commands,
        "bundles",
        help_text="Compatibility alias for doc-bundles.",
    )
    _add_asset_commands(
        commands,
        "sources",
        help_text="Compatibility alias for assets.",
    )
    cleanup = commands.add_parser(
        "cleanup", help="Plan or execute physical Blob cleanup."
    )
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command", required=True)
    blobs = cleanup_commands.add_parser("blobs")
    blobs.add_argument("--older-than", default="7d")
    blobs.add_argument("--dry-run", action="store_true")
    blobs.add_argument("--yes", action="store_true")
    _add_runtime_arguments(blobs, source_storage=True)

    args = parser.parse_args(arguments)
    context = _context(args)
    write = partial(_write, human=args.human)

    if args.command in {"doc-bundles", "assets", "bundles", "sources"}:
        runtime = _source_runtime(args)
        registry = SourceAssetRegistry.load(
            runtime=runtime,
            catalog_path=args.catalog_path,
        )
        canonical = args.command in {"doc-bundles", "assets"}
        if args.command in {"bundles", "sources"}:
            replacement = "doc-bundles" if args.command == "bundles" else "assets"
            print(
                f"'{args.command}' is retained for compatibility; use '{replacement}'.",
                file=sys.stderr,
            )
        if args.command in {"doc-bundles", "bundles"}:
            if args.bundle_command == "list":
                items = (
                    registry.list_doc_bundles(context)
                    if canonical
                    else registry.list_bundles(context)
                )
                write([_plain(item) for item in items])
            elif args.bundle_command == "locate":
                value = (
                    registry.locate_doc_bundle(context, args.bundle_id)
                    if canonical
                    else registry.locate_bundle(context, args.bundle_id)
                )
                write(value)
            elif args.bundle_command == "deleted":
                items = registry.list_deleted_doc_bundles(context)
                write([_plain(item) for item in items])
            elif args.bundle_command == "delete":
                if not args.yes:
                    raise ValueError("Deleting a DocBundle requires --yes.")
                value = registry.delete_doc_bundle(
                    context,
                    args.bundle_id,
                    recursive=args.recursive,
                    reason=args.reason,
                )
                write(_plain(value))
            else:
                value = (
                    registry.resolve_doc_bundle(context, args.path, create=True)
                    if canonical
                    else registry.resolve_bundle(context, args.path, create=True)
                )
                write(_plain(value))
            return 0
        if args.asset_command == "add":
            value = registry.register_path(
                context,
                args.path,
                bundle=args.bundle,
                structure=args.structure,
                recursive=args.recursive,
            )
            if isinstance(value, SourceAssetBatchResult) and value.failed_count:
                print(
                    f"{value.failed_count} SourceAsset file registration(s) failed; "
                    "inspect the JSON batch items for safe details.",
                    file=sys.stderr,
                )
            write(_asset_plain(value) if canonical else _plain(value))
        elif args.asset_command == "list":
            items = (
                registry.list_assets(context, bundle=args.bundle)
                if canonical
                else registry.list_sources(context, bundle=args.bundle)
            )
            write([_asset_plain(item) if canonical else _plain(item) for item in items])
        elif args.asset_command == "show":
            value = (
                registry.show_asset(context, args.asset_id)
                if canonical
                else registry.show_source(context, args.asset_id)
            )
            write(_asset_plain(value) if canonical else _plain(value))
        elif args.asset_command == "deleted":
            write(
                [_asset_plain(item) for item in registry.list_deleted_assets(context)]
            )
        elif args.asset_command == "delete":
            if not args.yes:
                raise ValueError("Deleting a SourceAsset requires --yes.")
            value = registry.delete_asset(context, args.asset_id, reason=args.reason)
            write(_plain(value))
        else:
            value = (
                registry.locate_asset(context, args.asset_id)
                if canonical
                else registry.locate_source(context, args.asset_id)
            )
            write(_asset_plain(value) if canonical else _plain(value))
        return 0

    if args.command == "cleanup":
        runtime = _source_runtime(args)
        registry = SourceAssetRegistry.load(
            runtime=runtime, catalog_path=args.catalog_path
        )
        if args.dry_run and args.yes:
            raise ValueError("--dry-run and --yes cannot be combined.")
        if not args.dry_run and not args.yes:
            raise ValueError(
                "Physical cleanup requires --yes; use --dry-run to plan only."
            )
        service = SourceAssetCleanupService(registry=registry, storage_runtime=runtime)
        plan = service.plan_blobs(context, older_than=_parse_duration(args.older_than))
        if args.dry_run:
            write(plan.to_dict())
        else:
            write(service.execute_blobs(context, plan).to_dict())
        return 0

    if args.command == "ingest":
        runtime = _source_runtime(args)
        registry = SourceAssetRegistry.load(
            runtime=runtime, catalog_path=args.catalog_path
        )
        storage, repository = _runtime(args, runtime=runtime)
        service = IngestService(storage, jobs=repository, registry=registry)
        if args.asset:
            asset = registry.show_asset(context, args.asset)
            result = service.ingest_assets(
                (asset.asset_id,),
                registry,
                context,
                submitted_input={"type": "asset", "asset_id": asset.asset_id},
                root_bundle_id=asset.bundle_id,
            )
        elif args.bundle:
            result = service.ingest_bundle(args.bundle, registry, context)
        else:
            result = service.ingest_path(
                args.path,
                owner_id=args.owner_id,
                context=context,
                registry=registry,
            )
        write(_run_result_json(result))
        return 0

    storage, repository = _runtime(args)
    manager = IngestManager(storage, repository)
    if args.command == "jobs":
        if args.job_command == "list":
            write(manager.list_jobs(context, owner_id=args.owner_id))
        elif args.job_command == "show":
            write(manager.show_job(context, args.job_id, owner_id=args.owner_id))
        elif args.job_command == "events":
            if args.follow:
                _follow_job_events(
                    manager,
                    context,
                    args.job_id,
                    owner_id=args.owner_id,
                    human=args.human,
                )
            else:
                write(manager.job_events(context, args.job_id, owner_id=args.owner_id))
        else:
            write(manager.request_cancel(context, args.job_id, owner_id=args.owner_id))
        return 0
    if args.command == "documents":
        if args.document_command == "list":
            write(manager.list_documents(context))
        elif args.document_command == "show":
            write(manager.show_document(context, args.document_id))
        else:
            if not args.yes:
                parser.error("documents delete requires --yes.")
            manager.delete_document(context, args.document_id)
            write({"deleted_document_id": args.document_id})
        return 0
    if args.command == "runs":
        if args.run_command == "list":
            write(manager.list_runs(context))
        elif args.run_command == "show":
            write(manager.show_run(context, args.run_id))
        else:
            if not args.yes:
                parser.error("runs delete requires --yes.")
            manager.delete_run(context, args.run_id)
            write({"deleted_run_id": args.run_id})
        return 0

    payload = manager.read_artifact(context, args.document_id, args.name)
    if args.human:
        _write_artifact_human(args.name, payload)
    else:
        _write(_artifact_json(args.name, payload), human=False)
    return 0


def _add_doc_bundle_commands(
    commands: argparse._SubParsersAction,
    name: str,
    *,
    help_text: str,
) -> None:
    group = commands.add_parser(name, help=help_text)
    subcommands = group.add_subparsers(dest="bundle_command", required=True)
    for command_name in ("list", "create", "locate", "delete", "deleted"):
        command = subcommands.add_parser(command_name)
        if command_name == "create":
            command.add_argument("path")
        if command_name == "locate":
            command.add_argument("bundle_id")
        if command_name == "delete":
            command.add_argument("bundle_id")
            command.add_argument("--recursive", action="store_true")
            command.add_argument("--yes", action="store_true")
            command.add_argument("--reason")
        _add_runtime_arguments(command, source_storage=True)


def _add_asset_commands(
    commands: argparse._SubParsersAction,
    name: str,
    *,
    help_text: str,
) -> None:
    group = commands.add_parser(name, help=help_text)
    subcommands = group.add_subparsers(dest="asset_command", required=True)
    add = subcommands.add_parser("add")
    add.add_argument("path")
    add.add_argument("--bundle")
    add.add_argument(
        "--structure",
        choices=("preserve", "flat"),
        default="preserve",
    )
    recursion = add.add_mutually_exclusive_group()
    recursion.add_argument(
        "--recursive", dest="recursive", action="store_true", default=True
    )
    recursion.add_argument("--no-recursive", dest="recursive", action="store_false")
    _add_runtime_arguments(add, source_storage=True)
    listing = subcommands.add_parser("list")
    listing.add_argument("--bundle")
    _add_runtime_arguments(listing, source_storage=True)
    show = subcommands.add_parser("show")
    show.add_argument("asset_id")
    _add_runtime_arguments(show, source_storage=True)
    locate = subcommands.add_parser("locate")
    locate.add_argument("asset_id")
    _add_runtime_arguments(locate, source_storage=True)
    delete = subcommands.add_parser("delete")
    delete.add_argument("asset_id")
    delete.add_argument("--yes", action="store_true")
    delete.add_argument("--reason")
    _add_runtime_arguments(delete, source_storage=True)
    subcommands.add_parser("deleted")
    _add_runtime_arguments(subcommands.choices["deleted"], source_storage=True)


def _add_runtime_arguments(
    parser: argparse.ArgumentParser, *, source_storage: bool = False
) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--storage-config", help="Advanced Storage Runtime TOML override."
    )
    selection.add_argument(
        "--storage-root", help="Deprecated local storage-root override."
    )
    parser.add_argument("--catalog-path", help="Advanced SourceAsset catalog override.")
    parser.add_argument(
        "--jobs-database", help="Advanced SQLite jobs database override."
    )
    parser.add_argument(
        "--owner-id", default="local", help="Owner scope for lifecycle commands."
    )
    parser.add_argument(
        "--context", help="JSON file defining the base Cognityx context."
    )
    parser.add_argument("--context-type", choices=("user", "system"))
    parser.add_argument("--principal-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--project-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--scope", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--human", action="store_true")


def _runtime(
    args: argparse.Namespace, *, runtime: StorageRuntime | None = None
) -> tuple[object, JobRepository]:
    selected = runtime or _source_runtime(args)
    storage = selected.for_role("artifact")
    default_database = selected.for_role("catalog").native_path("ingest/jobs.sqlite3")
    database = Path(args.jobs_database) if args.jobs_database else default_database
    database.parent.mkdir(parents=True, exist_ok=True)
    return storage, JobRepository(str(database))


def _source_runtime(args: argparse.Namespace) -> StorageRuntime:
    if args.storage_root:
        warnings.warn(
            "--storage-root is deprecated; configure StorageRuntime or use --storage-config.",
            FutureWarning,
            stacklevel=3,
        )
        return StorageRuntime.from_config(
            StorageConfig.built_in(root=args.storage_root)
        )
    return StorageRuntime.load(config_file=args.storage_config)


def _context(args: argparse.Namespace):
    scopes: dict[str, str] = {}
    for item in args.scope:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise ValueError("--scope must use KEY=VALUE.")
        scopes[key] = value
    return resolve_execution_context(
        context_file=args.context,
        context_type=args.context_type,
        principal_id=args.principal_id
        if args.principal_id is not None
        else (args.owner_id if args.command in {"jobs", "ingest"} else None),
        tenant_id=args.tenant_id,
        project_id=args.project_id,
        workspace_id=args.workspace_id,
        scopes=scopes,
    )


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)([hd])", value.strip().lower())
    if match is None:
        raise ValueError("Duration must be a positive value such as 1h, 24h, or 7d.")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(hours=amount) if unit == "h" else timedelta(days=amount)


def _result_json(result: object) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "job_id": result.job_id,
        "document_id": result.document.document_id,
        "manifest_key": result.manifest_key,
        "artifacts": [
            {"artifact_id": artifact.artifact_id, "uri": artifact.uri}
            for artifact in result.artifacts
        ],
    }


def _run_result_json(result: object) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "job_id": result.job_id,
        "root_bundle_id": result.root_bundle_id,
        "document_count": result.document_count,
        "failed_count": result.failed_count,
        "run_manifest_uri": result.run_manifest_uri,
        "documents": [_result_json(item) for item in result.results],
        "failures": list(result.failures),
    }


def _follow_job_events(
    manager: IngestManager,
    context: object,
    job_id: str,
    *,
    owner_id: str,
    human: bool,
) -> None:
    after = 0
    while True:
        events = manager.job_events(context, job_id, owner_id=owner_id, after=after)
        for event in events:
            output = render_human(event) if human else json.dumps(event, sort_keys=True)
            print(output, flush=True)
            after = int(event["sequence"])
        state = manager.show_job(context, job_id, owner_id=owner_id)["job"]["state"]
        if state in {"completed", "failed", "cancelled", "interrupted"}:
            return
        time.sleep(0.25)


def _artifact_json(name: str, payload: bytes) -> dict[str, object]:
    try:
        return {
            "artifact": name,
            "encoding": "utf-8",
            "content": payload.decode("utf-8"),
        }
    except UnicodeDecodeError:
        return {
            "artifact": name,
            "encoding": "base64",
            "content": base64.b64encode(payload).decode("ascii"),
        }


def _write(value: object, *, human: bool) -> None:
    if human:
        print(render_human(value))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def _write_artifact_human(name: str, payload: bytes) -> None:
    try:
        encoding = "utf-8"
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "base64"
        content = base64.b64encode(payload).decode("ascii")
    sys.stdout.write(f"Artifact: {name}\nEncoding: {encoding}\nContent:\n")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")


def _plain(value: object) -> object:
    from dataclasses import asdict, is_dataclass

    return asdict(value) if is_dataclass(value) else value


def _asset_plain(value: object) -> object:
    result = _plain(value)
    if isinstance(result, dict) and "source_id" in result:
        result["asset_id"] = result.pop("source_id")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
