from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .artifact_capture import ARTIFACT_METADATA_TABLES
from .util import canonical_json


ARTIFACT_STREAM_SCHEMA = "codex-history-artifact-row-stream-v1"


def _row_payload(table: str, columns: tuple[str, ...], row: sqlite3.Row) -> dict[str, Any]:
    return {
        "table": table,
        "key": row[columns[0]],
        "row": {column: row[column] for column in columns},
    }


def write_artifact_stream(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                output.write(canonical_json({"schema_version": ARTIFACT_STREAM_SCHEMA}) + "\n")
                for table, columns in ARTIFACT_METADATA_TABLES.items():
                    count = 0
                    query = f"SELECT {','.join(columns)} FROM {table} ORDER BY {columns[0]}"
                    for row in connection.execute(query):
                        payload = _row_payload(table, columns, row)
                        encoded = canonical_json(payload)
                        digest.update(encoded.encode("utf-8"))
                        digest.update(b"\n")
                        output.write(encoded + "\n")
                        count += 1
                    counts[table] = count
                output.write(
                    canonical_json(
                        {
                            "trailer": True,
                            "digest": digest.hexdigest(),
                            "counts": counts,
                        }
                    )
                    + "\n"
                )
    return {
        "schema_version": ARTIFACT_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        "compressed_bytes": path.stat().st_size,
    }


def artifact_metadata_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    for table, columns in ARTIFACT_METADATA_TABLES.items():
        count = 0
        query = f"SELECT {','.join(columns)} FROM {table} ORDER BY {columns[0]}"
        for row in connection.execute(query):
            encoded = canonical_json(_row_payload(table, columns, row))
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
            count += 1
        counts[table] = count
    return {
        "schema_version": ARTIFACT_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
    }


def read_artifact_stream(
    source: BinaryIO,
    *,
    on_row: Callable[[str, tuple[str, ...], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts = {table: 0 for table in ARTIFACT_METADATA_TABLES}
    artifact_sha256: list[str] = []
    trailer: dict[str, Any] | None = None
    with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8") as rows:
            first = rows.readline()
            if not first:
                raise ValueError("Artifact metadata stream is empty")
            header = json.loads(first)
            if header.get("schema_version") != ARTIFACT_STREAM_SCHEMA:
                raise ValueError(
                    f"Unsupported artifact row stream: {header.get('schema_version')}"
                )
            for line_number, line in enumerate(rows, 2):
                payload = json.loads(line)
                if payload.get("trailer"):
                    trailer = payload
                    if rows.readline():
                        raise ValueError("Artifact metadata stream has rows after its trailer")
                    break
                table = str(payload.get("table") or "")
                columns = ARTIFACT_METADATA_TABLES.get(table)
                row = payload.get("row")
                if columns is None or not isinstance(row, dict) or set(row) != set(columns):
                    raise ValueError(f"Invalid artifact metadata row at line {line_number}")
                if payload.get("key") != row[columns[0]]:
                    raise ValueError(f"Artifact metadata key mismatch at line {line_number}")
                encoded = canonical_json(payload)
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                counts[table] += 1
                if table == "artifact_files":
                    artifact_sha256.append(str(row[columns[0]]))
                if on_row is not None:
                    on_row(table, columns, row)
    if trailer is None:
        raise ValueError("Artifact metadata stream has no trailer")
    if trailer.get("digest") != digest.hexdigest() or trailer.get("counts") != counts:
        raise ValueError("Artifact metadata stream digest or counts disagree")
    return {
        "schema_version": ARTIFACT_STREAM_SCHEMA,
        "digest": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        "artifact_sha256": artifact_sha256,
    }


def apply_artifact_stream(connection: sqlite3.Connection, source: BinaryIO) -> dict[str, Any]:
    for table in reversed(tuple(ARTIFACT_METADATA_TABLES)):
        connection.execute(f"DELETE FROM {table}")

    statements: dict[str, str] = {}

    def insert(table: str, columns: tuple[str, ...], row: dict[str, Any]) -> None:
        statement = statements.get(table)
        if statement is None:
            statement = (
                f"INSERT INTO {table}({','.join(columns)}) VALUES("
                + ",".join("?" for _ in columns)
                + ")"
            )
            statements[table] = statement
        connection.execute(statement, tuple(row[column] for column in columns))

    result = read_artifact_stream(source, on_row=insert)
    return result
