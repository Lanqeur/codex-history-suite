from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from .schema import rebuild_fts
from .util import canonical_json


AUTHORITY_STREAM_SCHEMA = "codex-history-authority-row-stream-v1"

AUTHORITY_TABLES = (
    "source_files",
    "source_chunks",
    "threads",
    "turns",
    "canonical_events",
    "scopes",
    "scope_threads",
    "evidence",
    "evidence_occurrences",
    "knowledge",
    "knowledge_evidence",
    "record_evidence_occurrences",
    "overview_claims",
    "overview_claim_records",
    "knowledge_relations",
    "knowledge_versions",
    "relation_candidates",
    "aliases",
    "semantic_documents",
    "semantic_document_records",
)


def _placeholders(values: Iterable[str]) -> tuple[list[str], str]:
    items = sorted(set(values))
    return items, ",".join("?" for _ in items)


def _rows(connection: sqlite3.Connection, query: str, parameters: list[str]) -> list[sqlite3.Row]:
    return connection.execute(query, parameters).fetchall() if parameters else []


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _table_pk(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = list(connection.execute(f"PRAGMA table_info({table})"))
    return tuple(str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5]))


def _selected_rows(
    connection: sqlite3.Connection,
    *,
    changed_source_ids: set[str],
    changed_thread_ids: set[str],
) -> tuple[dict[str, list[sqlite3.Row]], dict[str, Any]]:
    threads, thread_marks = _placeholders(changed_thread_ids)
    sources, source_marks = _placeholders(changed_source_ids)
    affected_scopes = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT DISTINCT scope_id FROM scope_threads WHERE thread_id IN ({thread_marks})",
            threads,
        )
    }
    affected_scopes.update(
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT scope_id FROM scopes WHERE scope_id IN ({thread_marks})",
            threads,
        )
    )
    scopes, scope_marks = _placeholders(affected_scopes)
    record_ids = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT record_id FROM knowledge WHERE scope_id IN ({scope_marks})",
            scopes,
        )
    }
    records, record_marks = _placeholders(record_ids)
    evidence_ids = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT evidence_id FROM knowledge_evidence WHERE record_id IN ({record_marks})",
            records,
        )
    }
    evidence_ids.update(
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT DISTINCT evidence_id FROM evidence_occurrences WHERE thread_id IN ({thread_marks})",
            threads,
        )
    )
    evidence, evidence_marks = _placeholders(evidence_ids)
    occurrence_ids = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT occurrence_id FROM evidence_occurrences WHERE evidence_id IN ({evidence_marks})",
            evidence,
        )
    }
    occurrence_ids.update(
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT occurrence_id FROM record_evidence_occurrences WHERE record_id IN ({record_marks})",
            records,
        )
    )
    occurrences, occurrence_marks = _placeholders(occurrence_ids)
    claim_ids = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT claim_id FROM overview_claims WHERE overview_record_id IN ({record_marks})",
            records,
        )
    }
    claims, claim_marks = _placeholders(claim_ids)
    document_ids = {
        str(row[0])
        for row in _rows(
            connection,
            f"SELECT document_id FROM semantic_document_records WHERE record_id IN ({record_marks})",
            records,
        )
    }
    documents, document_marks = _placeholders(document_ids)

    queries: dict[str, tuple[str, list[str]]] = {
        "source_files": (f"SELECT * FROM source_files WHERE source_id IN ({source_marks})", sources),
        "source_chunks": (f"SELECT * FROM source_chunks WHERE source_id IN ({source_marks})", sources),
        "threads": (f"SELECT * FROM threads WHERE thread_id IN ({thread_marks})", threads),
        "turns": (f"SELECT * FROM turns WHERE thread_id IN ({thread_marks})", threads),
        "canonical_events": (f"SELECT * FROM canonical_events WHERE thread_id IN ({thread_marks})", threads),
        "scopes": (f"SELECT * FROM scopes WHERE scope_id IN ({scope_marks})", scopes),
        "scope_threads": (f"SELECT * FROM scope_threads WHERE scope_id IN ({scope_marks})", scopes),
        "evidence": (f"SELECT * FROM evidence WHERE evidence_id IN ({evidence_marks})", evidence),
        "evidence_occurrences": (f"SELECT * FROM evidence_occurrences WHERE occurrence_id IN ({occurrence_marks})", occurrences),
        "knowledge": (f"SELECT * FROM knowledge WHERE record_id IN ({record_marks})", records),
        "knowledge_evidence": (f"SELECT * FROM knowledge_evidence WHERE record_id IN ({record_marks})", records),
        "record_evidence_occurrences": (f"SELECT * FROM record_evidence_occurrences WHERE record_id IN ({record_marks})", records),
        "overview_claims": (f"SELECT * FROM overview_claims WHERE claim_id IN ({claim_marks})", claims),
        "overview_claim_records": (f"SELECT * FROM overview_claim_records WHERE claim_id IN ({claim_marks})", claims),
        "knowledge_relations": (
            f"SELECT * FROM knowledge_relations WHERE source_record_id IN ({record_marks}) OR target_record_id IN ({record_marks})",
            records + records,
        ),
        "knowledge_versions": (f"SELECT * FROM knowledge_versions WHERE record_id IN ({record_marks})", records),
        "relation_candidates": (
            f"SELECT * FROM relation_candidates WHERE source_record_id IN ({record_marks}) OR target_record_id IN ({record_marks})",
            records + records,
        ),
        "aliases": ("SELECT * FROM aliases", ["all"]),
        "semantic_documents": (f"SELECT * FROM semantic_documents WHERE document_id IN ({document_marks})", documents),
        "semantic_document_records": (f"SELECT * FROM semantic_document_records WHERE record_id IN ({record_marks})", records),
    }
    selected: dict[str, list[sqlite3.Row]] = {}
    for table in AUTHORITY_TABLES:
        query, parameters = queries[table]
        if table == "aliases":
            selected[table] = connection.execute(query).fetchall()
        elif parameters:
            selected[table] = connection.execute(query, parameters).fetchall()
        else:
            selected[table] = []
    target_counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in AUTHORITY_TABLES
    }
    return selected, {
        "changed_source_ids": sources,
        "changed_thread_ids": threads,
        "affected_scope_ids": scopes,
        "record_ids": records,
        "evidence_ids": evidence,
        "occurrence_ids": occurrences,
        "claim_ids": claims,
        "document_ids": documents,
        "target_table_counts": target_counts,
    }


def write_authority_stream(
    connection: sqlite3.Connection,
    path: Path,
    *,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    changed_source_ids = {str(item["source_id"]) for item in changes}
    changed_thread_ids = {str(item["thread_id"]) for item in changes if item.get("thread_id")}
    selected, metadata = _selected_rows(
        connection,
        changed_source_ids=changed_source_ids,
        changed_thread_ids=changed_thread_ids,
    )
    metadata["safe_for_fast_apply"] = all(
        item.get("kind") in {"added", "appended", "rewritten"} for item in changes
    )
    metadata["change_kinds"] = sorted({str(item.get("kind")) for item in changes})
    columns = {table: _table_columns(connection, table) for table in AUTHORITY_TABLES}
    primary_keys = {table: _table_pk(connection, table) for table in AUTHORITY_TABLES}
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                output.write(
                    canonical_json(
                        {
                            "schema_version": AUTHORITY_STREAM_SCHEMA,
                            "metadata": metadata,
                            "columns": {key: list(value) for key, value in columns.items()},
                            "primary_keys": {key: list(value) for key, value in primary_keys.items()},
                        }
                    )
                    + "\n"
                )
                for table in AUTHORITY_TABLES:
                    rows = selected[table]
                    counts[table] = len(rows)
                    for row in rows:
                        row_value = {column: row[column] for column in columns[table]}
                        if table == "knowledge_versions":
                            row_value["build_id"] = None
                        payload = {
                            "table": table,
                            "row": row_value,
                        }
                        encoded = canonical_json(payload)
                        digest.update(encoded.encode("utf-8"))
                        digest.update(b"\n")
                        output.write(encoded + "\n")
                output.write(
                    canonical_json(
                        {"trailer": True, "digest": digest.hexdigest(), "counts": counts}
                    )
                    + "\n"
                )
    return {
        "schema_version": AUTHORITY_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        "compressed_bytes": path.stat().st_size,
        **metadata,
    }


def _read_header(rows: io.TextIOWrapper) -> dict[str, Any]:
    line = rows.readline()
    if not line:
        raise ValueError("Authority patch is empty")
    header = json.loads(line)
    if header.get("schema_version") != AUTHORITY_STREAM_SCHEMA:
        raise ValueError(f"Unsupported authority patch: {header.get('schema_version')}")
    return header


def inspect_authority_stream(source: BinaryIO) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts = {table: 0 for table in AUTHORITY_TABLES}
    trailer: dict[str, Any] | None = None
    with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8") as rows:
            header = _read_header(rows)
            for line_number, line in enumerate(rows, 2):
                payload = json.loads(line)
                if payload.get("trailer"):
                    trailer = payload
                    if rows.readline():
                        raise ValueError("Authority patch has rows after its trailer")
                    break
                table = str(payload.get("table") or "")
                row = payload.get("row")
                expected = tuple((header.get("columns") or {}).get(table) or ())
                if table not in counts or not isinstance(row, dict) or set(row) != set(expected):
                    raise ValueError(f"Invalid authority row at line {line_number}")
                encoded = canonical_json(payload)
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                counts[table] += 1
    if trailer is None or trailer.get("digest") != digest.hexdigest() or trailer.get("counts") != counts:
        raise ValueError("Authority patch digest or counts disagree")
    return {
        "schema_version": AUTHORITY_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        **dict(header.get("metadata") or {}),
    }


def apply_authority_stream(connection: sqlite3.Connection, source: BinaryIO) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts = {table: 0 for table in AUTHORITY_TABLES}
    with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8") as rows:
            header = _read_header(rows)
            metadata = dict(header.get("metadata") or {})
            if not metadata.get("safe_for_fast_apply"):
                raise ValueError("Authority patch requires canonical rebuild fallback")
            scopes = list(metadata.get("affected_scope_ids") or [])
            threads = list(metadata.get("changed_thread_ids") or [])
            sources = list(metadata.get("changed_source_ids") or [])
            if scopes:
                connection.execute(
                    "DELETE FROM scopes WHERE scope_id IN (" + ",".join("?" for _ in scopes) + ")",
                    scopes,
                )
            if threads:
                connection.execute(
                    "DELETE FROM threads WHERE thread_id IN (" + ",".join("?" for _ in threads) + ")",
                    threads,
                )
            if sources:
                connection.execute(
                    "DELETE FROM source_files WHERE source_id IN (" + ",".join("?" for _ in sources) + ")",
                    sources,
                )
            connection.execute("DELETE FROM aliases")

            columns_by_table = {
                table: tuple(values)
                for table, values in (header.get("columns") or {}).items()
            }
            pk_by_table = {
                table: tuple(values)
                for table, values in (header.get("primary_keys") or {}).items()
            }
            statements: dict[str, str] = {}
            trailer: dict[str, Any] | None = None
            for line_number, line in enumerate(rows, 2):
                payload = json.loads(line)
                if payload.get("trailer"):
                    trailer = payload
                    if rows.readline():
                        raise ValueError("Authority patch has rows after its trailer")
                    break
                table = str(payload.get("table") or "")
                row = payload.get("row")
                columns = columns_by_table.get(table, ())
                if table not in counts or not isinstance(row, dict) or set(row) != set(columns):
                    raise ValueError(f"Invalid authority row at line {line_number}")
                statement = statements.get(table)
                if statement is None:
                    keys = pk_by_table.get(table, ())
                    updates = [column for column in columns if column not in keys]
                    statement = (
                        f"INSERT INTO {table}({','.join(columns)}) VALUES("
                        + ",".join("?" for _ in columns)
                        + ")"
                    )
                    if keys:
                        statement += " ON CONFLICT(" + ",".join(keys) + ") "
                        if updates:
                            statement += "DO UPDATE SET " + ",".join(
                                f"{column}=excluded.{column}" for column in updates
                            )
                        else:
                            statement += "DO NOTHING"
                    statements[table] = statement
                connection.execute(statement, tuple(row[column] for column in columns))
                encoded = canonical_json(payload)
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                counts[table] += 1
            if trailer is None or trailer.get("digest") != digest.hexdigest() or trailer.get("counts") != counts:
                raise ValueError("Authority patch digest or counts disagree")

    connection.execute(
        "DELETE FROM evidence WHERE evidence_id NOT IN (SELECT DISTINCT evidence_id FROM evidence_occurrences)"
    )
    connection.execute(
        "DELETE FROM semantic_documents WHERE document_id NOT IN (SELECT DISTINCT document_id FROM semantic_document_records)"
    )
    connection.execute(
        "UPDATE semantic_documents SET record_count=(SELECT COUNT(*) FROM semantic_document_records sr WHERE sr.document_id=semantic_documents.document_id)"
    )
    rebuild_fts(connection)
    target_counts = dict(metadata.get("target_table_counts") or {})
    mismatches = {
        table: {
            "expected": int(expected),
            "actual": int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
        }
        for table, expected in target_counts.items()
        if int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) != int(expected)
    }
    if mismatches:
        raise ValueError(f"Authority patch table counts did not converge: {mismatches}")
    return {
        "schema_version": AUTHORITY_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        "target_table_counts": target_counts,
        "changed_thread_ids": metadata.get("changed_thread_ids", []),
        "affected_scope_ids": metadata.get("affected_scope_ids", []),
    }
