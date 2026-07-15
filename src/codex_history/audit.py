from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .schema import connect, schema_version
from .util import canonical_json, utc_now


LOGICAL_TABLES: dict[str, tuple[str, ...]] = {
    "source_files": (
        "source_id", "adapter", "relative_path", "thread_id", "size_bytes",
        "content_sha256", "prefix_sha256", "line_count", "source_state",
    ),
    "source_chunks": (
        "source_id", "chunk_index", "chunk_sha256", "size_bytes", "cas_relative_path",
    ),
    "threads": (
        "thread_id", "group_name", "title", "transcript_relative_path", "source_relative_path",
        "source_size_bytes", "line_count", "first_activity_at", "last_activity_at",
        "event_count", "turn_count", "message_count", "user_message_count",
        "assistant_message_count", "tool_call_count", "tool_output_count", "goal_event_count",
        "compacted_count", "source_kind", "parent_thread_id", "source_id",
    ),
    "turns": (
        "turn_id", "thread_id", "turn_seq", "source_turn_id", "started_at", "completed_at",
        "status", "user_text", "assistant_text", "tool_call_count", "tool_output_count",
        "event_count", "content_sha256", "metadata_json",
    ),
    "canonical_events": (
        "event_id", "content_sha256", "source_id", "thread_id", "turn_id", "line_no",
        "byte_start", "byte_end", "timestamp", "event_type", "payload_type", "role", "text",
        "tool_name", "call_id", "raw_json", "metadata_json",
    ),
    "scopes": (
        "scope_id", "scope_type", "scope_title", "thread_ids_json", "thread_titles_json",
        "overview", "human_verdict", "evidence_rows", "overview_path", "ledger_path",
        "first_activity_at", "last_activity_at",
    ),
    "scope_threads": ("scope_id", "thread_id", "ordinal"),
    "evidence": (
        "evidence_id", "assignment", "evidence_chars", "source_task_id", "scope_ids_json",
        "thread_ids_json", "applies_to_json", "item_id", "sha256", "occurrence_count",
        "first_occurred_at", "last_occurred_at", "temporal_basis",
    ),
    "evidence_occurrences": (
        "occurrence_id", "evidence_id", "thread_id", "turn_seq", "position", "tier",
        "canonical_turn_id", "start_line", "end_line", "occurred_start_at", "occurred_end_at",
        "temporal_basis", "temporal_confidence", "metadata_json",
    ),
    "knowledge": (
        "record_id", "tier", "asset_type", "scope_id", "scope_type", "scope_title", "category",
        "theme", "phase", "text", "status", "status_group", "evidence_count",
        "evidence_refs_json", "source_path", "source_locator", "confidence", "metadata_json",
        "occurred_start_at", "occurred_end_at", "observed_at", "verified_at", "valid_from",
        "valid_to", "temporal_basis", "temporal_confidence",
    ),
    "knowledge_evidence": ("record_id", "evidence_id"),
    "record_evidence_occurrences": ("record_id", "occurrence_id", "scope_match"),
    "overview_claims": (
        "claim_id", "overview_record_id", "scope_id", "ordinal", "start_char", "end_char",
        "claim_text", "status", "confidence", "metadata_json",
    ),
    "overview_claim_records": (
        "claim_id", "record_id", "match_method", "score", "rank", "metadata_json",
    ),
    "knowledge_relations": (
        "relation_id", "source_record_id", "relation_type", "target_record_id",
        "evidence_refs_json", "confidence", "metadata_json",
    ),
    "artifact_files": (
        "sha256", "size_bytes", "size_human", "cas_relative_path", "artifact_uri", "mime_type",
        "extension", "source_open_path", "tiers", "keep_reasons", "categories", "path_count",
        "transcript_occurrences_mapped",
    ),
    "artifact_paths": (
        "path_key", "path", "sha256", "artifact_uri", "cas_relative_path", "size_bytes", "tier",
        "keep_reason", "category", "source_open_path",
    ),
    "ledger_artifacts": (
        "ledger_artifact_id", "scope_id", "ref", "role", "evidence_refs_json",
        "source_path", "source_locator",
    ),
    "semantic_documents": (
        "document_id", "content_sha256", "document_text", "record_count",
    ),
    "semantic_document_records": ("document_id", "record_id"),
    "aliases": ("alias", "canonical", "alias_kind", "weight"),
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)
        ).fetchone()
    )


def table_digest(
    connection: sqlite3.Connection, table: str, columns: Iterable[str]
) -> tuple[int, str]:
    columns = tuple(columns)
    if not _table_exists(connection, table):
        return 0, "missing"
    query = f"SELECT {','.join(columns)} FROM {table} ORDER BY {','.join(columns)}"
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        digest.update(canonical_json(list(row)).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def logical_digest(connection: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    aggregate = hashlib.sha256()
    for table, columns in LOGICAL_TABLES.items():
        count, digest = table_digest(connection, table, columns)
        tables[table] = {"rows": count, "sha256": digest}
        aggregate.update(f"{table}:{count}:{digest}\n".encode("utf-8"))
    return {"sha256": aggregate.hexdigest(), "tables": tables}


def audit_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    checks.append({"name": "sqlite_integrity", "passed": integrity == "ok", "detail": integrity})

    foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
    checks.append(
        {
            "name": "foreign_keys",
            "passed": not foreign_keys,
            "detail": {"violations": foreign_keys[:20], "count": len(foreign_keys)},
        }
    )

    knowledge_count = connection.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    evidence_count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    orphan_records = connection.execute(
        "SELECT COUNT(*) FROM knowledge WHERE evidence_count>0 AND record_id NOT IN (SELECT DISTINCT record_id FROM knowledge_evidence)"
    ).fetchone()[0]
    checks.append(
        {
            "name": "knowledge_evidence_links",
            "passed": orphan_records == 0,
            "detail": {"knowledge": knowledge_count, "evidence": evidence_count, "orphans": orphan_records},
        }
    )

    fts: dict[str, Any] = {}
    for table in ("knowledge_fts", "knowledge_terms_fts", "knowledge_body_fts", "knowledge_title_fts"):
        try:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            fts[table] = count
        except sqlite3.Error as error:
            fts[table] = str(error)
    checks.append(
        {
            "name": "fts_coverage",
            "passed": all(value == knowledge_count for value in fts.values()),
            "detail": {"knowledge": knowledge_count, "fts": fts},
        }
    )

    source_count = connection.execute("SELECT COUNT(*) FROM source_files WHERE source_state='active'").fetchone()[0]
    thread_count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    parse_event_count = connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
    legacy_import = connection.execute(
        "SELECT value FROM metadata WHERE key='legacy_import'"
    ).fetchone()
    source_coverage_passed = (
        source_count == thread_count and (source_count == 0 or parse_event_count > 0)
    ) or (bool(legacy_import) and knowledge_count > 0)
    checks.append(
        {
            "name": "source_thread_coverage",
            "passed": source_coverage_passed,
            "detail": {
                "sources": source_count,
                "threads": thread_count,
                "events": parse_event_count,
                "legacy_import": bool(legacy_import),
            },
        }
    )

    digest = logical_digest(connection)
    return {
        "schema_version": schema_version(connection),
        "created_at": utc_now(),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "logical_digest": digest,
    }


def audit_database(path: Path) -> dict[str, Any]:
    connection = connect(path, readonly=True)
    try:
        return audit_connection(connection)
    finally:
        connection.close()


def compare_databases(left: Path, right: Path) -> dict[str, Any]:
    left_connection = connect(left, readonly=True)
    right_connection = connect(right, readonly=True)
    try:
        left_digest = logical_digest(left_connection)
        right_digest = logical_digest(right_connection)
    finally:
        left_connection.close()
        right_connection.close()
    differences = {
        table: {"left": left_digest["tables"][table], "right": right_digest["tables"][table]}
        for table in LOGICAL_TABLES
        if left_digest["tables"][table] != right_digest["tables"][table]
    }
    return {
        "created_at": utc_now(),
        "passed": not differences,
        "left_sha256": left_digest["sha256"],
        "right_sha256": right_digest["sha256"],
        "differences": differences,
    }
