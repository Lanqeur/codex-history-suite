from __future__ import annotations

import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import audit_connection
from .config import ProfileConfig, config_path, ensure_profile_dirs
from .pipeline import PIPELINE_VERSION
from .schema import connect, initialize, rebuild_fts
from .source import discover_sources
from .util import (
    atomic_write_json,
    canonical_json,
    file_lock,
    sha256_and_line_count,
    sha256_file,
    utc_now,
)


def _migration_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"migration-{stamp}-{uuid.uuid4().hex[:8]}"


def _source_schema(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        return str(row[0]) if row else f"sqlite-user-version-{connection.execute('PRAGMA user_version').fetchone()[0]}"
    except sqlite3.Error:
        return "unknown"


def migrate_legacy_database(
    config: ProfileConfig,
    source: Path,
    *,
    promote: bool = True,
    adopt_sources: bool = True,
    source_chroma: Path | None = None,
    source_artifacts: Path | None = None,
    artifact_mode: str = "reference",
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    with file_lock(config.lock_path):
        source_connection = connect(source, readonly=True)
        try:
            if not source_connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
            ).fetchone():
                raise ValueError("Source is not a Codex History database: knowledge table missing")
            source_schema = _source_schema(source_connection)
            build_id = _migration_id()
            build_dir = config.builds_dir / build_id
            build_dir.mkdir(parents=True, exist_ok=False)
            database = build_dir / "codex_history.sqlite3"
            target_connection = connect(database)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()

        connection = connect(database)
        try:
            initialize(connection)
            now = utc_now()
            adoption = {"enabled": adopt_sources, "adopted": 0, "unmatched": 0, "skipped": 0}
            existing_source_count = connection.execute(
                "SELECT COUNT(*) FROM source_files"
            ).fetchone()[0]
            if adopt_sources and existing_source_count == 0:
                known_threads = {
                    str(row[0]) for row in connection.execute("SELECT thread_id FROM threads")
                }
                for candidate in discover_sources(config):
                    if candidate.thread_id not in known_threads:
                        adoption["unmatched"] += 1
                        continue
                    digest, line_count, sampled_size = sha256_and_line_count(
                        candidate.path, candidate.size_bytes
                    )
                    connection.execute(
                        """
                        INSERT INTO source_files(
                            source_id,adapter,source_root,source_path,relative_path,thread_id,
                            size_bytes,mtime_ns,content_sha256,prefix_sha256,line_count,
                            snapshot_manifest_path,source_state,first_seen_at,last_seen_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            candidate.source_id,
                            candidate.adapter,
                            str(candidate.root),
                            str(candidate.path),
                            candidate.relative_path,
                            candidate.thread_id,
                            sampled_size,
                            candidate.mtime_ns,
                            digest,
                            digest,
                            line_count,
                            f"legacy-adopted://{candidate.source_id}",
                            "active",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE threads SET source_id=? WHERE thread_id=?",
                        (candidate.source_id, candidate.thread_id),
                    )
                    adoption["adopted"] += 1
            elif existing_source_count:
                adoption["skipped"] = existing_source_count
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("legacy_import", "true"),
                    ("legacy_schema_version", source_schema),
                    ("legacy_database_sha256", sha256_file(source)),
                    ("pipeline_version", PIPELINE_VERSION),
                ),
            )
            connection.execute(
                "INSERT INTO builds(build_id,build_kind,status,parent_build_id,started_at,source_manifest_path,config_sha256,notes_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    build_id,
                    "migration",
                    "running",
                    None,
                    now,
                    str(build_dir / "migration-manifest.json"),
                    sha256_file(config_path(config.home)),
                    canonical_json(
                        {
                            "source_database": str(source),
                            "source_schema": source_schema,
                            "source_adoption": adoption,
                        }
                    ),
                ),
            )
            for ordinal, stage in enumerate(
                ("discover", "snapshot", "ingest", "lineage", "summarize", "index")
            ):
                connection.execute(
                    "INSERT INTO stage_checkpoints(build_id,stage_name,ordinal,status,started_at,completed_at,report_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        build_id,
                        stage,
                        ordinal,
                        "complete",
                        now,
                        now,
                        canonical_json(
                            {
                                "method": "lossless_sqlite_migration",
                                "source_schema": source_schema,
                                "source_adoption": adoption,
                            }
                        ),
                    ),
                )
            rebuild_fts(connection)
            audit = audit_connection(connection)
            atomic_write_json(build_dir / "audit.json", audit)
            connection.execute(
                "INSERT INTO stage_checkpoints(build_id,stage_name,ordinal,status,started_at,completed_at,report_json) VALUES(?,?,?,?,?,?,?)",
                (
                    build_id,
                    "audit",
                    6,
                    "complete" if audit["passed"] else "failed",
                    now,
                    utc_now(),
                    canonical_json(audit),
                ),
            )
            if not audit["passed"]:
                connection.execute(
                    "UPDATE builds SET status='failed',completed_at=? WHERE build_id=?",
                    (utc_now(), build_id),
                )
                connection.commit()
                raise RuntimeError("Migrated database failed integrity audit")
            promoted_at = utc_now() if promote else None
            connection.execute(
                "INSERT INTO stage_checkpoints(build_id,stage_name,ordinal,status,started_at,completed_at,report_json) VALUES(?,?,?,?,?,?,?)",
                (
                    build_id,
                    "promote",
                    7,
                    "complete",
                    utc_now(),
                    utc_now(),
                    canonical_json({"promoted": promote}),
                ),
            )
            connection.execute(
                "UPDATE builds SET status='complete',completed_at=?,promoted_at=?,logical_digest=? WHERE build_id=?",
                (utc_now(), promoted_at, audit["logical_digest"]["sha256"], build_id),
            )
            connection.commit()
        finally:
            connection.close()

        manifest = {
            "schema_version": "codex-history-migration-v1",
            "build_id": build_id,
            "created_at": utc_now(),
            "source_database": str(source),
            "source_schema": source_schema,
            "source_sha256": sha256_file(source),
            "destination_database": str(database),
            "lossless_sqlite_backup": True,
            "source_adoption": adoption,
        }
        chroma_migration: dict[str, Any] = {"copied": False}
        if source_chroma is not None:
            source_chroma = source_chroma.expanduser().resolve()
            if not source_chroma.is_dir():
                raise FileNotFoundError(source_chroma)
            destination_chroma = config.root / "semantic/chroma"
            if destination_chroma.exists() and any(destination_chroma.iterdir()):
                raise FileExistsError(
                    f"Semantic destination is not empty: {destination_chroma}"
                )
            destination_chroma.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_chroma, destination_chroma, dirs_exist_ok=True)
            chroma_migration = {
                "copied": True,
                "source": str(source_chroma),
                "destination": str(destination_chroma),
            }
        manifest["chroma_migration"] = chroma_migration
        artifact_migration: dict[str, Any] = {"status": "not_requested"}
        if source_artifacts is not None:
            from .artifacts import adopt_artifacts

            artifact_migration = adopt_artifacts(
                config,
                database,
                source_artifacts,
                mode=artifact_mode,
                acquire_lock=False,
            )
        manifest["artifact_migration"] = artifact_migration
        atomic_write_json(build_dir / "migration-manifest.json", manifest)
        if promote:
            active = {
                "schema_version": "codex-history-active-v1",
                "profile": config.name,
                "build_id": build_id,
                "database": database.relative_to(config.root).as_posix(),
                "promoted_at": promoted_at,
                "migrated_from": source_schema,
                "incremental_ready": False,
            }
            atomic_write_json(config.active_path, active)
        return {
            "status": "complete",
            "build_id": build_id,
            "source_schema": source_schema,
            "database": str(database),
            "promoted": promote,
            "audit": audit,
            "source_adoption": adoption,
            "chroma_migration": chroma_migration,
            "artifact_migration": artifact_migration,
            "manifest": str(build_dir / "migration-manifest.json"),
        }
