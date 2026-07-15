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
from .audit import audit_database
from .config import (
    default_codex_homes,
    default_data_home,
    ensure_profile_dirs,
    load_config,
    resolve_summarization,
    write_initial_config,
)
from .pipeline import (
    active_database,
    active_info,
    build_full,
    equivalence_audit,
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

    update = subparsers.add_parser("update", help="plan or apply an incremental update")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--max-cost-cny", type=float, default=None)
    update.add_argument("--no-promote", action="store_true")
    update.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show active build and incomplete runs")
    status.add_argument("--json", action="store_true")

    audit = subparsers.add_parser("audit", help="audit integrity or full/incremental equivalence")
    audit.add_argument("--equivalence", action="store_true")
    audit.add_argument("--keep-reference", action="store_true")
    audit.add_argument("--json", action="store_true")

    repair = subparsers.add_parser("repair", help="inspect failed runs and remove stale locks")
    repair.add_argument("--clear-stale-lock", action="store_true")
    repair.add_argument("--json", action="store_true")

    migrate = subparsers.add_parser("migrate", help="import a legacy Codex History SQLite database")
    migrate.add_argument("--from-db", type=Path, required=True)
    migrate.add_argument("--skip-source-adoption", action="store_true")
    migrate.add_argument("--from-chroma", type=Path, default=None)
    migrate.add_argument("--no-promote", action="store_true")
    migrate.add_argument("--json", action="store_true")

    backup = subparsers.add_parser("backup", help="copy the active database and active manifest")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--json", action="store_true")
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
    os.environ["CODEX_HISTORY_RETRIEVAL"] = "hybrid" if config.embedding_enabled else "lexical"
    os.environ["CODEX_HISTORY_EMBEDDING_ENDPOINT"] = config.embedding_endpoint
    os.environ["CODEX_HISTORY_EMBEDDING_API_KEY_ENV"] = config.embedding_api_key_env
    os.environ["CODEX_HISTORY_EMBEDDING_MODEL"] = config.embedding_model
    os.environ["CODEX_HISTORY_EMBEDDING_DIMENSIONS"] = str(config.embedding_dimensions)
    os.environ["CODEX_HISTORY_EMBEDDING_INPUT_PRICE_CNY"] = str(
        config.embedding_input_price_cny
    )
    os.environ["CODEX_HISTORY_EMBEDDING_ENV_FILE"] = config.embedding_env_file
    from . import query

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
    if args.command == "audit":
        if args.equivalence:
            value = equivalence_audit(config, keep_reference=args.keep_reference)
        else:
            database = active_database(config)
            if not database:
                raise RuntimeError("No active database")
            value = audit_database(database)
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
        )
        _print(value, as_json=args.json)
        return 0
    if args.command == "backup":
        _print(_backup(config, args.destination), as_json=args.json)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
