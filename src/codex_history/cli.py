from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    default_codex_homes,
    default_data_home,
    ensure_profile_dirs,
    load_config,
    resolve_summarization,
    write_initial_config,
)
from .coverage import knowledge_coverage
from .pipeline import (
    active_database,
    active_info,
    build_full,
    compact_canonical_storage,
    equivalence_audit,
    hydrate_canonical_baseline,
    plan,
    update_incremental,
)
from .schema import connect
from .source import discover_sources
from .util import atomic_write_json, read_json, utc_now


QUERY_COMMANDS = {
    "search",
    "asset",
    "unresolved",
    "context",
    "trace",
    "claims",
    "compare",
    "artifacts",
    "stats",
}


def _print(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False)}")
            else:
                print(f"{key}: {item}")
    else:
        print(value)


def _management_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-history",
        description="Build, update, audit, migrate, and query a portable Codex history knowledge base.",
    )
    parser.add_argument("--home", type=Path, default=None, help="Codex History data home")
    parser.add_argument("--profile", default=None, help="profile name")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="inspect platform, Codex sources, and dependencies")
    doctor.add_argument("--json", action="store_true")

    init = subparsers.add_parser("init", help="initialize a portable profile")
    init.add_argument("--source", type=Path, action="append", default=[])
    init.add_argument("--force", action="store_true")
    init.add_argument("--json", action="store_true")

    discover = subparsers.add_parser("discover", help="discover transcript sources without changing them")
    discover.add_argument("--json", action="store_true")

    plan_parser = subparsers.add_parser("plan", help="estimate scope, cost, time, and storage")
    plan_parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    plan_parser.add_argument("--json", action="store_true")

    build = subparsers.add_parser("build", help="run a checkpointed full build")
    build.add_argument("--max-cost-cny", type=float, default=None)
    build.add_argument("--no-promote", action="store_true")
    build.add_argument("--json", action="store_true")

    hydrate = subparsers.add_parser(
        "hydrate-baseline",
        help="attach canonical transcript snapshots to a migrated curated knowledge base",
    )
    hydrate.add_argument("--max-cost-cny", type=float, default=None)
    hydrate.add_argument("--no-promote", action="store_true")
    hydrate.add_argument("--json", action="store_true")

    compact = subparsers.add_parser(
        "compact-storage",
        help="remove duplicate canonical payloads after snapshot-offset trace verification",
    )
    compact.add_argument("--no-promote", action="store_true")
    compact.add_argument("--json", action="store_true")

    update = subparsers.add_parser("update", help="plan or apply an incremental update")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--max-cost-cny", type=float, default=None)
    update.add_argument("--no-promote", action="store_true")
    update.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show active build and incomplete runs")
    status.add_argument("--json", action="store_true")

    coverage = subparsers.add_parser(
        "coverage", help="show represented history time range and source observation watermark"
    )
    coverage.add_argument("--json", action="store_true")

    audit = subparsers.add_parser("audit", help="audit integrity or full/incremental equivalence")
    audit.add_argument("--equivalence", action="store_true")
    audit.add_argument("--keep-reference", action="store_true")
    audit.add_argument("--verify-artifact-hashes", action="store_true")
    audit.add_argument("--json", action="store_true")

    repair = subparsers.add_parser("repair", help="inspect failed runs and remove stale locks")
    repair.add_argument("--clear-stale-lock", action="store_true")
    repair.add_argument("--json", action="store_true")

    migrate = subparsers.add_parser("migrate", help="import a legacy Codex History SQLite database")
    migrate.add_argument("--from-db", type=Path, required=True)
    migrate.add_argument("--skip-source-adoption", action="store_true")
    migrate.add_argument("--from-chroma", type=Path, default=None)
    migrate.add_argument("--from-artifacts", type=Path, default=None)
    migrate.add_argument(
        "--artifact-mode",
        choices=("reference", "copy", "hardlink", "auto"),
        default="reference",
    )
    migrate.add_argument("--no-promote", action="store_true")
    migrate.add_argument("--json", action="store_true")

    backup = subparsers.add_parser("backup", help="copy the active database and active manifest")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--json", action="store_true")

    library = subparsers.add_parser(
        "library", help="move, search, deduplicate, merge, and synchronize device libraries"
    )
    library_commands = library.add_subparsers(dest="library_command", required=True)

    device = library_commands.add_parser("device", help="show or name this device identity")
    device.add_argument("--name", default="")
    device.add_argument("--json", action="store_true")

    library_list = library_commands.add_parser("list", help="list local and imported libraries")
    library_list.add_argument("--json", action="store_true")

    library_export = library_commands.add_parser(
        "export", help="export a verified, portable library bundle"
    )
    library_export.add_argument("destination", type=Path)
    library_export.add_argument(
        "--artifacts",
        choices=("none", "referenced", "all"),
        default="referenced",
        help="omit artifacts, include database-referenced artifacts, or include the complete CAS",
    )
    library_export.add_argument("--without-semantic", action="store_true")
    library_export.add_argument("--without-model-cache", action="store_true")
    library_export.add_argument("--json", action="store_true")

    delta_export = library_commands.add_parser(
        "export-delta", help="export only changes since a canonical baseline or prior delta"
    )
    delta_export.add_argument("destination", type=Path)
    delta_export.add_argument("--base", type=Path, required=True)
    delta_export.add_argument(
        "--artifacts", choices=("none", "referenced", "all"), default="referenced"
    )
    delta_export.add_argument("--without-model-cache", action="store_true")
    delta_export.add_argument("--json", action="store_true")

    library_import = library_commands.add_parser(
        "import", help="verify and import a device library with automatic naming"
    )
    library_import.add_argument("bundle", type=Path)
    library_import.add_argument("--as", dest="as_name", default="")
    library_import.add_argument(
        "--path-map", action="append", default=[], metavar="OLD=NEW",
        help="add a display-time absolute path prefix mapping; repeatable",
    )
    library_import.add_argument("--json", action="store_true")

    delta_apply = library_commands.add_parser(
        "apply-delta", help="apply a verified source delta and run an incremental rebuild"
    )
    delta_apply.add_argument("delta", type=Path)
    delta_apply.add_argument("--max-cost-cny", type=float, default=None)
    delta_apply.add_argument("--json", action="store_true")

    library_verify = library_commands.add_parser("verify", help="verify every bundled file hash")
    library_verify.add_argument("bundle", type=Path)
    library_verify.add_argument("--json", action="store_true")

    artifact_audit = library_commands.add_parser(
        "artifact-audit", help="check database-to-CAS artifact closure"
    )
    artifact_audit.add_argument("--verify-hashes", action="store_true")
    artifact_audit.add_argument("--json", action="store_true")

    adopt_artifacts = library_commands.add_parser(
        "adopt-artifacts", help="verify and attach or materialize an existing artifact CAS"
    )
    adopt_artifacts.add_argument("source", type=Path)
    adopt_artifacts.add_argument(
        "--mode",
        choices=("reference", "copy", "hardlink", "auto"),
        default="reference",
    )
    adopt_artifacts.add_argument("--json", action="store_true")

    library_search = library_commands.add_parser(
        "search", help="search multiple libraries and collapse duplicate knowledge"
    )
    library_search.add_argument("query")
    library_search.add_argument("--from", dest="source_profiles", action="append", default=[])
    library_search.add_argument("--limit", type=int, default=10)
    library_search.add_argument("--deep", action="store_true")
    library_search.add_argument("--retrieval", choices=("lexical", "hybrid", "semantic"), default="hybrid")
    library_search.add_argument("--all", dest="query_mode", action="store_const", const="all")
    library_search.set_defaults(query_mode="any")
    library_search.add_argument("--since", default="")
    library_search.add_argument("--until", default="")
    library_search.add_argument("--time-match", choices=("overlaps", "contained"), default="overlaps")
    library_search.add_argument("--as-of", default="")
    library_search.add_argument("--json", action="store_true")

    library_merge = library_commands.add_parser(
        "merge", help="materialize an idempotent, non-destructive merged profile"
    )
    library_merge.add_argument("--from", dest="source_profiles", action="append", required=True)
    library_merge.add_argument("--as", dest="as_name", default="merged-history")
    library_merge.add_argument("--build", action="store_true")
    library_merge.add_argument("--max-cost-cny", type=float, default=None)
    library_merge.add_argument("--json", action="store_true")

    library_sync = library_commands.add_parser(
        "sync", help="merge, build, and export one convergence bundle for both devices"
    )
    library_sync.add_argument("destination", type=Path)
    library_sync.add_argument("--from", dest="source_profiles", action="append", required=True)
    library_sync.add_argument("--as", dest="as_name", default="shared-history")
    library_sync.add_argument("--max-cost-cny", type=float, default=None)
    library_sync.add_argument("--json", action="store_true")
    return parser


def _fts_probe() -> dict[str, Any]:
    connection = sqlite3.connect(":memory:")
    result = {"fts5": False, "trigram": False, "error": ""}
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        result["fts5"] = True
        connection.execute("CREATE VIRTUAL TABLE probe_tri USING fts5(text,tokenize='trigram')")
        result["trigram"] = True
    except sqlite3.Error as error:
        result["error"] = str(error)
    finally:
        connection.close()
    return result


def _doctor(home: Path, profile_name: str | None) -> dict[str, Any]:
    config_file = home / "config.toml"
    codex_homes = default_codex_homes()
    result: dict[str, Any] = {
        "created_at": utc_now(),
        "version": __version__,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "wsl": bool(os.environ.get("WSL_DISTRO_NAME")),
        "data_home": str(home),
        "config_exists": config_file.exists(),
        "codex_home_candidates": [
            {"path": str(path), "exists": path.exists()} for path in codex_homes
        ],
        "sqlite": sqlite3.sqlite_version,
        "sqlite_features": _fts_probe(),
        "chromadb_available": importlib.util.find_spec("chromadb") is not None,
        "warnings": [],
    }
    if sys.version_info < (3, 11):
        result["warnings"].append("Python 3.11 or newer is required.")
    if not result["sqlite_features"]["fts5"]:
        result["warnings"].append("SQLite FTS5 is unavailable.")
    if result["wsl"] and str(home).startswith("/mnt/"):
        result["warnings"].append(
            "The active database is on a Windows-mounted filesystem; native WSL storage is recommended."
        )
    if config_file.exists():
        config = load_config(home, profile_name)
        result["profile"] = config.name
        result["source_roots"] = [
            {"path": str(path), "exists": path.exists()} for path in config.source_roots
        ]
        result["active"] = active_info(config)
        result["summarization"] = resolve_summarization(config)
        if result["summarization"]["fallback"]:
            result["warnings"].append(
                "Model-first summarization is falling back to extractive mode: "
                + str(result["summarization"]["fallback_reason"])
            )
        runtime = _runtime_probe(config.runtime_python)
        result["configured_runtime"] = runtime
        semantic_available = result["chromadb_available"] or bool(runtime.get("chromadb"))
        if config.embedding_enabled and not semantic_available:
            result["warnings"].append(
                "Embedding is enabled but ChromaDB is not installed; install the semantic extra."
            )
    result["passed"] = sys.version_info >= (3, 11) and result["sqlite_features"]["fts5"]
    return result


def _runtime_probe(value: str) -> dict[str, Any]:
    if not value:
        return {"configured": False}
    path = Path(value).expanduser()
    result: dict[str, Any] = {
        "configured": True,
        "path": str(path),
        "exists": path.is_file(),
        "chromadb": False,
    }
    if not result["exists"]:
        return result
    try:
        probe = subprocess.run(
            [
                str(path),
                "-c",
                "import importlib.util,sys;"
                "print(sys.version.split()[0]);"
                "print(int(importlib.util.find_spec('chromadb') is not None))",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = probe.stdout.splitlines()
        result["python"] = lines[0] if lines else ""
        result["chromadb"] = len(lines) > 1 and lines[1] == "1"
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def _maybe_reexec_runtime(config, argv: list[str]) -> None:
    if not config.embedding_enabled or not config.runtime_python:
        return
    if os.environ.get("CODEX_HISTORY_RUNTIME_ACTIVE") == "1":
        return
    # Keep the virtual-environment launcher path intact. Resolving its symlink
    # would make it look identical to the base interpreter and skip activation.
    runtime = Path(config.runtime_python).expanduser().absolute()
    current = Path(sys.executable).absolute()
    if runtime == current:
        return
    if not runtime.is_file():
        raise RuntimeError(f"Configured runtime Python does not exist: {runtime}")
    environment = os.environ.copy()
    environment["CODEX_HISTORY_RUNTIME_ACTIVE"] = "1"
    entry_script = environment.get("CODEX_HISTORY_ENTRY_SCRIPT")
    if entry_script:
        command = [str(runtime), entry_script, *argv]
    else:
        command = [str(runtime), "-m", "codex_history.cli", *argv]
    os.execve(str(runtime), command, environment)


def _status(config) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    if config.runs_dir.exists():
        for path in sorted(config.runs_dir.glob("*/run.json"), reverse=True):
            value = read_json(path)
            if value:
                runs.append(
                    {
                        "build_id": value.get("build_id"),
                        "kind": value.get("kind"),
                        "status": value.get("status"),
                        "started_at": value.get("started_at"),
                        "completed_at": value.get("completed_at"),
                        "failed_stage": next(
                            (
                                name
                                for name, stage in value.get("stages", {}).items()
                                if stage.get("status") == "failed"
                            ),
                            None,
                        ),
                    }
                )
    database = active_database(config)
    counts: dict[str, int] = {}
    if database:
        connection = connect(database, readonly=True)
        try:
            for table in ("threads", "turns", "canonical_events", "evidence", "knowledge", "artifact_files"):
                counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()
    return {
        "profile": config.name,
        "active": active_info(config),
        "database": str(database) if database else None,
        "counts": counts,
        "history_coverage": (
            knowledge_coverage(config, database, active=active_info(config))
            if database
            else None
        ),
        "runs": runs[:20],
        "lock_exists": config.lock_path.exists(),
    }


def _backup(config, destination: Path) -> dict[str, Any]:
    database = active_database(config)
    if not database:
        raise RuntimeError("No active database")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / database.name
    source_connection = connect(database, readonly=True)
    target_connection = connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        source_connection.close()
        target_connection.close()
    active_target = destination / "active.json"
    atomic_write_json(active_target, active_info(config))
    return {"database": str(target), "active_manifest": str(active_target)}


def _query_main(
    argv: list[str],
    home: Path | None,
    profile_name: str | None,
    original_argv: list[str],
) -> int:
    config = load_config(home, profile_name)
    _maybe_reexec_runtime(config, original_argv)
    database = active_database(config)
    if not database:
        raise SystemExit("No active Codex History build. Run `codex-history build` first.")
    os.environ["CODEX_HISTORY_DB"] = str(database)
    os.environ["CODEX_HISTORY_CHROMA"] = str(config.root / "semantic/chroma")
    os.environ["CODEX_HISTORY_SNAPSHOTS"] = str(config.snapshots_dir)
    os.environ["CODEX_HISTORY_RETRIEVAL"] = "hybrid" if config.embedding_enabled else "lexical"
    os.environ["CODEX_HISTORY_EMBEDDING_ENDPOINT"] = config.embedding_endpoint
    os.environ["CODEX_HISTORY_EMBEDDING_API_KEY_ENV"] = config.embedding_api_key_env
    os.environ["CODEX_HISTORY_EMBEDDING_MODEL"] = config.embedding_model
    os.environ["CODEX_HISTORY_EMBEDDING_DIMENSIONS"] = str(config.embedding_dimensions)
    os.environ["CODEX_HISTORY_EMBEDDING_INPUT_PRICE_CNY"] = str(
        config.embedding_input_price_cny
    )
    os.environ["CODEX_HISTORY_EMBEDDING_ENV_FILE"] = config.embedding_env_file
    os.environ["CODEX_HISTORY_PATH_MAPPINGS"] = json.dumps(
        [
            {"original_prefix": old, "local_prefix": new}
            for old, new in config.path_mappings
        ],
        ensure_ascii=False,
    )
    from .artifacts import external_artifact_roots

    os.environ["CODEX_HISTORY_ARTIFACT_ROOTS"] = json.dumps(
        [str(config.cas_dir), *(str(path) for path in external_artifact_roots(config))],
        ensure_ascii=False,
    )
    from . import query

    query.PATH_MAPPINGS = [
        {"original_prefix": old, "local_prefix": new}
        for old, new in config.path_mappings
    ]
    query.ARTIFACT_ROOTS = [config.cas_dir, *external_artifact_roots(config)]
    query.SNAPSHOT_ROOT = config.snapshots_dir

    return query.main(["--db", str(database), *argv])


def _first_command(argv: list[str]) -> str | None:
    skip = False
    for index, value in enumerate(argv):
        if skip:
            skip = False
            continue
        if value in {"--home", "--profile"}:
            skip = True
            continue
        if value.startswith("--home=") or value.startswith("--profile="):
            continue
        if not value.startswith("-"):
            return value
    return None


def _global_values(argv: list[str]) -> tuple[Path | None, str | None, list[str]]:
    home: Path | None = None
    profile_name: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--home":
            home = Path(argv[index + 1])
            index += 2
        elif value.startswith("--home="):
            home = Path(value.split("=", 1)[1])
            index += 1
        elif value == "--profile":
            profile_name = argv[index + 1]
            index += 2
        elif value.startswith("--profile="):
            profile_name = value.split("=", 1)[1]
            index += 1
        else:
            remaining.append(value)
            index += 1
    return home, profile_name, remaining


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home_arg, profile_arg, remaining = _global_values(argv)
    if _first_command(remaining) in QUERY_COMMANDS:
        return _query_main(remaining, home_arg, profile_arg, argv)

    args = _management_parser().parse_args(argv)
    home = (args.home or default_data_home()).expanduser().resolve()
    if args.command == "doctor":
        _print(_doctor(home, args.profile), as_json=args.json)
        return 0
    if args.command == "init":
        sources = args.source or [path for path in default_codex_homes() if path.exists()]
        if not sources:
            sources = [default_codex_homes()[0]]
        profile_name = args.profile or "default"
        path = write_initial_config(
            home,
            profile=profile_name,
            source_roots=sources,
            force=args.force,
        )
        config = load_config(home, profile_name)
        ensure_profile_dirs(config)
        _print(
            {
                "status": "initialized",
                "config": str(path),
                "profile": profile_name,
                "source_roots": [str(path) for path in config.source_roots],
                "summarization": resolve_summarization(config),
                "next_step": (
                    "Set DASHSCOPE_API_KEY for the recommended DeepSeek V4 Flash summaries, "
                    "then run `plan --mode full`. Without a key, auto mode falls back to extractive."
                ),
            },
            as_json=args.json,
        )
        return 0

    if args.command == "library":
        from .library import (
            apply_delta,
            configure_device,
            export_delta,
            export_library,
            federated_search,
            import_library,
            list_libraries,
            merge_libraries,
            sync_libraries,
            verify_bundle,
            verify_delta,
        )
        from .artifacts import adopt_artifacts, inspect_artifact_closure

        if args.library_command == "device":
            _print(configure_device(home, args.name), as_json=args.json)
            return 0
        if args.library_command == "list":
            _print(list_libraries(home), as_json=args.json)
            return 0
        if args.library_command == "verify":
            import zipfile

            with zipfile.ZipFile(args.bundle, "r") as archive:
                is_delta = "delta.json" in archive.namelist()
            value = verify_delta(args.bundle) if is_delta else verify_bundle(args.bundle)
            _print(value, as_json=args.json)
            return 0 if value["passed"] else 2
        if args.library_command == "artifact-audit":
            config = load_config(home, args.profile)
            database = active_database(config)
            if not database:
                raise RuntimeError("No active database")
            value, _ = inspect_artifact_closure(
                config, database, verify_hashes=args.verify_hashes
            )
            _print(value, as_json=args.json)
            return 0 if value["complete"] else 2
        if args.library_command == "adopt-artifacts":
            config = load_config(home, args.profile)
            database = active_database(config)
            if not database:
                raise RuntimeError("No active database")
            _print(
                adopt_artifacts(config, database, args.source, mode=args.mode),
                as_json=args.json,
            )
            return 0
        if args.library_command == "import":
            mappings: list[tuple[str, str]] = []
            for value in args.path_map:
                if "=" not in value:
                    raise SystemExit("--path-map must use OLD=NEW")
                old, new = value.split("=", 1)
                if not old or not new:
                    raise SystemExit("--path-map must use non-empty OLD=NEW values")
                mappings.append((old, new))
            _print(
                import_library(home, args.bundle, as_name=args.as_name, path_mappings=mappings),
                as_json=args.json,
            )
            return 0
        if args.library_command == "apply-delta":
            _print(
                apply_delta(
                    home,
                    args.delta,
                    profile_name=args.profile or "",
                    max_cost_cny=args.max_cost_cny,
                ),
                as_json=args.json,
            )
            return 0
        if args.library_command == "export":
            config = load_config(home, args.profile)
            _print(
                export_library(
                    config,
                    args.destination,
                    include_semantic=not args.without_semantic,
                    include_model_cache=not args.without_model_cache,
                    artifact_mode=args.artifacts,
                ),
                as_json=args.json,
            )
            return 0
        if args.library_command == "export-delta":
            config = load_config(home, args.profile)
            _print(
                export_delta(
                    config,
                    args.destination,
                    base=args.base,
                    artifact_mode=args.artifacts,
                    include_model_cache=not args.without_model_cache,
                ),
                as_json=args.json,
            )
            return 0
        if args.library_command == "search":
            value = federated_search(
                home,
                args.query,
                profiles=args.source_profiles,
                limit=args.limit,
                deep=args.deep,
                retrieval=args.retrieval,
                query_mode=args.query_mode,
                since=args.since,
                until=args.until,
                time_match=args.time_match,
                as_of=args.as_of,
            )
            if args.json:
                _print(value, as_json=True)
            else:
                print(
                    f"Searched {value['profile_count']} libraries; "
                    f"{value['duplicates_collapsed']} duplicate matches collapsed."
                )
                for index, row in enumerate(value["results"], 1):
                    profiles = ", ".join(
                        sorted({item["profile"] for item in row["library_matches"]})
                    )
                    print(
                        f"{index}. [{row['tier']} | {row['status_group']}] "
                        f"{row['text']}\n   Libraries: {profiles}"
                    )
            return 0
        if args.library_command == "merge":
            _print(
                merge_libraries(
                    home,
                    args.source_profiles,
                    as_name=args.as_name,
                    build=args.build,
                    max_cost_cny=args.max_cost_cny,
                ),
                as_json=args.json,
            )
            return 0
        if args.library_command == "sync":
            _print(
                sync_libraries(
                    home,
                    args.source_profiles,
                    args.destination,
                    as_name=args.as_name,
                    max_cost_cny=args.max_cost_cny,
                ),
                as_json=args.json,
            )
            return 0
        raise AssertionError(args.library_command)

    config = load_config(home, args.profile)
    _maybe_reexec_runtime(config, argv)
    ensure_profile_dirs(config)
    if args.command == "discover":
        sources = discover_sources(config)
        _print(
            {
                "source_count": len(sources),
                "total_bytes": sum(source.size_bytes for source in sources),
                "sources": [
                    {
                        "thread_id": source.thread_id,
                        "title": source.title,
                        "path": str(source.path),
                        "size_bytes": source.size_bytes,
                        "archived": source.archived,
                    }
                    for source in sources
                ],
            },
            as_json=args.json,
        )
        return 0
    if args.command == "plan":
        _print(plan(config, mode=args.mode), as_json=args.json)
        return 0
    if args.command == "build":
        _print(
            build_full(
                config,
                promote=not args.no_promote,
                max_cost_cny=args.max_cost_cny,
            ),
            as_json=args.json,
        )
        return 0
    if args.command == "hydrate-baseline":
        _print(
            hydrate_canonical_baseline(
                config,
                promote=not args.no_promote,
                max_cost_cny=args.max_cost_cny,
            ),
            as_json=args.json,
        )
        return 0
    if args.command == "compact-storage":
        _print(
            compact_canonical_storage(
                config,
                promote=not args.no_promote,
            ),
            as_json=args.json,
        )
        return 0
    if args.command == "update":
        value = (
            plan(config, mode="incremental")
            if args.dry_run
            else update_incremental(
                config,
                promote=not args.no_promote,
                max_cost_cny=args.max_cost_cny,
            )
        )
        _print(value, as_json=args.json)
        return 0
    if args.command == "status":
        _print(_status(config), as_json=args.json)
        return 0
    if args.command == "coverage":
        database = active_database(config)
        if not database:
            raise RuntimeError("No active database")
        _print(
            knowledge_coverage(config, database, active=active_info(config)),
            as_json=args.json,
        )
        return 0
    if args.command == "audit":
        if args.equivalence:
            value = equivalence_audit(config, keep_reference=args.keep_reference)
        else:
            database = active_database(config)
            if not database:
                raise RuntimeError("No active database")
            from .audit import audit_profile

            value = audit_profile(
                config,
                database,
                verify_artifact_hashes=args.verify_artifact_hashes,
            )
        _print(value, as_json=args.json)
        return 0 if value["passed"] else 2
    if args.command == "repair":
        if args.clear_stale_lock and config.lock_path.exists():
            config.lock_path.unlink()
        value = _status(config)
        value["repair_note"] = (
            "stale lock cleared" if args.clear_stale_lock else "no state was modified"
        )
        _print(value, as_json=args.json)
        return 0
    if args.command == "migrate":
        from .migration import migrate_legacy_database

        value = migrate_legacy_database(
            config,
            args.from_db,
            promote=not args.no_promote,
            adopt_sources=not args.skip_source_adoption,
            source_chroma=args.from_chroma,
            source_artifacts=args.from_artifacts,
            artifact_mode=args.artifact_mode,
        )
        _print(value, as_json=args.json)
        return 0
    if args.command == "backup":
        _print(_backup(config, args.destination), as_json=args.json)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
