from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import ProfileConfig
from .schema import connect
from .util import canonical_json, stable_id, utc_now


COVERAGE_SCHEMA = "codex-history-coverage-v1"
SOURCE_INVENTORY_SCHEMA = "codex-history-source-inventory-v1"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(connection, "metadata"):
        return {}
    return {
        str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bound(values: Iterable[str | None], *, latest: bool) -> str | None:
    parsed = [item for value in values if (item := _parse_timestamp(value)) is not None]
    if not parsed:
        return None
    return _utc_text(max(parsed) if latest else min(parsed))


def source_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(connection, "source_files"):
        sources: list[dict[str, Any]] = []
    else:
        title_by_thread = (
            {
                str(row["thread_id"]): str(row["title"])
                for row in connection.execute("SELECT thread_id,title FROM threads")
            }
            if _table_exists(connection, "threads")
            else {}
        )
        sources = []
        for row in connection.execute(
            "SELECT * FROM source_files WHERE source_state='active' "
            "ORDER BY thread_id,source_id"
        ):
            columns = set(row.keys())
            chunks = [
                {
                    "index": int(chunk["chunk_index"]),
                    "sha256": str(chunk["chunk_sha256"]),
                    "size_bytes": int(chunk["size_bytes"]),
                    "cas_relative_path": str(chunk["cas_relative_path"]),
                }
                for chunk in connection.execute(
                    "SELECT * FROM source_chunks WHERE source_id=? ORDER BY chunk_index",
                    (row["source_id"],),
                )
            ] if _table_exists(connection, "source_chunks") else []
            chunk_bytes = sum(item["size_bytes"] for item in chunks)
            size_bytes = int(row["size_bytes"])
            snapshot_size = int(
                row["snapshot_size_bytes"] or 0
                if "snapshot_size_bytes" in columns
                else 0
            )
            snapshot_format = str(
                row["snapshot_format"] or "raw-jsonl"
                if "snapshot_format" in columns
                else "raw-jsonl"
            )
            if snapshot_size == 0 and chunks:
                snapshot_size = chunk_bytes
            snapshot_sha = str(
                row["snapshot_content_sha256"] or ""
                if "snapshot_content_sha256" in columns
                else ""
            )
            if not snapshot_sha and snapshot_format == "raw-jsonl":
                snapshot_sha = str(row["content_sha256"])
            artifacts = []
            if _table_exists(connection, "artifact_paths") and _table_exists(
                connection, "artifact_files"
            ):
                artifacts = [
                    {
                        "sha256": str(item["sha256"]),
                        "size_bytes": int(item["size_bytes"]),
                        "mime_type": str(item["mime_type"]),
                        "extension": str(item["extension"]),
                        "cas_relative_path": str(item["cas_relative_path"]),
                        "artifact_uri": str(item["artifact_uri"]),
                    }
                    for item in connection.execute(
                        "SELECT DISTINCT f.sha256,f.size_bytes,f.mime_type,f.extension,"
                        "f.cas_relative_path,f.artifact_uri FROM artifact_paths ap "
                        "JOIN artifact_files f ON f.sha256=ap.sha256 WHERE ap.path LIKE ? "
                        "ORDER BY f.sha256",
                        (f"inline-image:{row['source_id']}:%",),
                    )
                ]
            sources.append(
                {
                    "source_id": str(row["source_id"]),
                    "adapter": str(row["adapter"]),
                    "source_root": str(row["source_root"]),
                    "source_path": str(row["source_path"]),
                    "relative_path": str(row["relative_path"]),
                    "thread_id": str(row["thread_id"]),
                    "title": title_by_thread.get(str(row["thread_id"]), str(row["thread_id"])),
                    "size_bytes": size_bytes,
                    "mtime_ns": int(row["mtime_ns"]),
                    "content_sha256": str(row["content_sha256"]),
                    "snapshot_format": snapshot_format,
                    "snapshot_size_bytes": snapshot_size,
                    "snapshot_content_sha256": snapshot_sha,
                    "line_count": int(row["line_count"]),
                    "first_seen_at": str(row["first_seen_at"]),
                    "last_seen_at": str(row["last_seen_at"]),
                    "snapshot_complete": chunk_bytes == snapshot_size and (
                        snapshot_size == 0 or bool(chunks)
                    ),
                    "chunks": chunks,
                    "artifacts": artifacts,
                }
            )
    digest_payload = [
        {
            "source_id": source["source_id"],
            "thread_id": source["thread_id"],
            "content_sha256": source["content_sha256"],
            "size_bytes": source["size_bytes"],
            "snapshot_format": source["snapshot_format"],
            "snapshot_content_sha256": source["snapshot_content_sha256"],
            "snapshot_size_bytes": source["snapshot_size_bytes"],
            "line_count": source["line_count"],
            "chunks": [
                (chunk["index"], chunk["sha256"], chunk["size_bytes"])
                for chunk in source["chunks"]
            ],
            "artifacts": [item["sha256"] for item in source["artifacts"]],
        }
        for source in sources
    ]
    digest = hashlib.sha256(canonical_json(digest_payload).encode("utf-8")).hexdigest()
    complete = all(item["snapshot_complete"] for item in sources) and bool(sources)
    return {
        "schema_version": SOURCE_INVENTORY_SCHEMA,
        "digest": digest,
        "generation_id": stable_id("generation", digest, length=32),
        "source_count": len(sources),
        "thread_count": len({item["thread_id"] for item in sources}),
        "snapshot_complete": complete,
        "total_bytes": sum(item["snapshot_size_bytes"] for item in sources),
        "source_bytes": sum(item["size_bytes"] for item in sources),
        "unique_chunk_count": len(
            {chunk["sha256"] for item in sources for chunk in item["chunks"]}
        ),
        "sources": sources,
    }


def knowledge_coverage(
    config: ProfileConfig,
    database: Path,
    *,
    active: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    connection = connect(database, readonly=True)
    try:
        metadata = _metadata(connection)
        build_id = str((active or {}).get("build_id") or "")
        build = None
        if _table_exists(connection, "builds"):
            if build_id:
                build = connection.execute(
                    "SELECT * FROM builds WHERE build_id=?", (build_id,)
                ).fetchone()
            if build is None:
                build = connection.execute(
                    "SELECT * FROM builds WHERE status='complete' "
                    "ORDER BY COALESCE(completed_at,started_at) DESC LIMIT 1"
                ).fetchone()
        build_value = dict(build) if build is not None else {}
        build_id = str(build_value.get("build_id") or build_id)

        stage_times: dict[str, dict[str, str | None]] = {}
        if build_id and _table_exists(connection, "stage_checkpoints"):
            for row in connection.execute(
                "SELECT stage_name,started_at,completed_at FROM stage_checkpoints WHERE build_id=?",
                (build_id,),
            ):
                stage_times[str(row[0])] = {
                    "started_at": row[1],
                    "completed_at": row[2],
                }
        source_stage_times = stage_times
        if build_value.get("build_kind") == "compact" and build_value.get(
            "parent_build_id"
        ):
            parent_times: dict[str, dict[str, str | None]] = {}
            for row in connection.execute(
                "SELECT stage_name,started_at,completed_at FROM stage_checkpoints "
                "WHERE build_id=?",
                (build_value["parent_build_id"],),
            ):
                parent_times[str(row[0])] = {
                    "started_at": row[1],
                    "completed_at": row[2],
                }
            if parent_times:
                source_stage_times = parent_times

        thread_count = 0
        thread_first = None
        thread_last = None
        if _table_exists(connection, "threads"):
            row = connection.execute(
                "SELECT COUNT(*),MIN(NULLIF(first_activity_at,'')),"
                "MAX(NULLIF(last_activity_at,'')) FROM threads"
            ).fetchone()
            thread_count, thread_first, thread_last = int(row[0]), row[1], row[2]

        event_count = 0
        event_first = None
        event_last = None
        if _table_exists(connection, "canonical_events"):
            row = connection.execute(
                "SELECT COUNT(*),MIN(NULLIF(timestamp,'')),MAX(NULLIF(timestamp,'')) "
                "FROM canonical_events"
            ).fetchone()
            event_count, event_first, event_last = int(row[0]), row[1], row[2]

        source_count = 0
        active_source_count = 0
        if _table_exists(connection, "source_files"):
            row = connection.execute(
                "SELECT COUNT(*),SUM(CASE WHEN source_state='active' THEN 1 ELSE 0 END) "
                "FROM source_files"
            ).fetchone()
            source_count = int(row[0])
            active_source_count = int(row[1] or 0)

        inventory = source_inventory(connection)

        hydrated = metadata.get("canonical_snapshot_complete") == "true"
        legacy = metadata.get("legacy_import") == "true" and not hydrated
        earliest = _bound((thread_first, event_first), latest=False)
        latest = _bound((thread_last, event_last), latest=True)
        source_scan_started = source_stage_times.get("discover", {}).get("started_at") or build_value.get(
            "started_at"
        )
        snapshot_completed = (
            None
            if legacy
            else source_stage_times.get("snapshot", {}).get("completed_at")
        )
        authority_completed = build_value.get("completed_at") or (active or {}).get("promoted_at")
        logical_digest = str(build_value.get("logical_digest") or "")
        coverage_basis = (
            "legacy-thread-metadata"
            if legacy
            else "canonical-events"
            if event_count
            else "thread-metadata"
        )
        version_id = stable_id(
            "coverage",
            build_id,
            logical_digest,
            latest,
            thread_count,
            source_count,
            length=32,
        )
        return {
            "schema_version": COVERAGE_SCHEMA,
            "generated_at": utc_now(),
            "knowledge_version_id": version_id,
            "build_id": build_id or None,
            "build_kind": build_value.get("build_kind"),
            "build_status": build_value.get("status"),
            "logical_digest": logical_digest or None,
            "legacy_database_sha256": metadata.get("legacy_database_sha256"),
            "coverage_basis": coverage_basis,
            "coverage_confidence": "legacy-migrated" if legacy else "canonical",
            "earliest_activity_at": earliest,
            "latest_activity_at": latest,
            "source_scan_started_at": _utc_text(_parse_timestamp(source_scan_started)),
            "source_snapshot_completed_at": _utc_text(_parse_timestamp(snapshot_completed)),
            "authority_completed_at": _utc_text(_parse_timestamp(authority_completed)),
            "thread_count": thread_count,
            "canonical_event_count": event_count,
            "source_count": source_count,
            "active_source_count": active_source_count,
            "source_inventory_digest": inventory["digest"],
            "source_generation_id": inventory["generation_id"],
            "source_snapshot_complete": inventory["snapshot_complete"],
            "source_snapshot_bytes": inventory["total_bytes"],
            "source_snapshot_unique_chunks": inventory["unique_chunk_count"],
            "source_roots": [str(path) for path in config.source_roots],
            "include_archived": config.include_archived,
            "incremental_ready": bool((active or {}).get("incremental_ready", False)),
            "completeness_semantics": "represented-history-not-contiguous-time-guarantee",
        }
    finally:
        connection.close()
