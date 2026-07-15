from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import ProfileConfig
from .util import atomic_write_bytes, atomic_write_json, sha256_file, stable_id, utc_now


@dataclass(frozen=True)
class SourceCandidate:
    source_id: str
    adapter: str
    root: Path
    path: Path
    relative_path: str
    thread_id: str
    title: str
    size_bytes: int
    mtime_ns: int
    archived: bool
    session_meta: dict[str, Any]


@dataclass(frozen=True)
class SourceChange:
    kind: str
    source: SourceCandidate | None
    previous: dict[str, Any] | None
    reason: str


@dataclass(frozen=True)
class SnapshotFile:
    source: SourceCandidate
    content_sha256: str
    prefix_sha256: str
    line_count: int
    manifest_path: Path
    chunks: tuple[dict[str, Any], ...]


def _first_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.readline()
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _session_index(root: Path) -> dict[str, str]:
    path = root / "session_index.jsonl"
    result: dict[str, str] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = row.get("id")
            title = row.get("thread_name") or row.get("title")
            if identifier and title:
                result[str(identifier)] = str(title)
    return result


def _state_titles(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    candidates = [root / "state_5.sqlite", root / "sqlite/state_5.sqlite"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            uri = f"file:{path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
            }
            if {"id", "title"}.issubset(columns):
                for identifier, title in connection.execute("SELECT id,title FROM threads"):
                    if identifier and title:
                        result[str(identifier)] = str(title)
            connection.close()
        except sqlite3.Error:
            continue
    return result


def _thread_id(path: Path, first: dict[str, Any]) -> str:
    payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    identifier = payload.get("id") or first.get("thread_id")
    if identifier:
        return str(identifier)
    stem = path.stem
    marker = stem.rsplit("-", 5)
    if len(marker) >= 5:
        possible = "-".join(marker[-5:])
        if len(possible) >= 32:
            return possible
    return stable_id("thread", str(path.resolve()))


def discover_sources(config: ProfileConfig) -> list[SourceCandidate]:
    discovered: list[SourceCandidate] = []
    seen: set[str] = set()
    for root in config.source_roots:
        root = root.expanduser()
        if not root.exists():
            continue
        titles = _session_index(root)
        titles.update(_state_titles(root))
        paths: list[Path] = []
        sessions = root / "sessions"
        if sessions.is_dir():
            paths.extend(sessions.rglob("*.jsonl"))
        archived = root / "archived_sessions"
        if config.include_archived and archived.is_dir():
            paths.extend(archived.glob("*.jsonl"))
        if root.is_file() and root.suffix == ".jsonl":
            paths.append(root)
        for path in sorted(paths):
            try:
                resolved = str(path.resolve())
                stat = path.stat()
            except OSError:
                continue
            key = os.path.normcase(resolved)
            if key in seen:
                continue
            seen.add(key)
            first = _first_json(path)
            thread_id = _thread_id(path, first)
            payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
            title = titles.get(thread_id) or str(payload.get("title") or thread_id)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = path.name
            is_archived = "archived_sessions" in path.parts
            source_id = stable_id("source", "codex-jsonl", resolved)
            discovered.append(
                SourceCandidate(
                    source_id=source_id,
                    adapter="codex-jsonl-v1",
                    root=root.resolve(),
                    path=path.resolve(),
                    relative_path=relative,
                    thread_id=thread_id,
                    title=title,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    archived=is_archived,
                    session_meta=payload,
                )
            )
    return sorted(discovered, key=lambda item: (item.thread_id, item.relative_path))


def previous_sources(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_files'"
    ).fetchone():
        return {}
    return {str(row["source_id"]): dict(row) for row in connection.execute("SELECT * FROM source_files")}


def classify_changes(
    current: Iterable[SourceCandidate], previous: dict[str, dict[str, Any]]
) -> list[SourceChange]:
    changes: list[SourceChange] = []
    observed: set[str] = set()
    for source in current:
        observed.add(source.source_id)
        old = previous.get(source.source_id)
        if old is None:
            changes.append(SourceChange("added", source, None, "new source path"))
            continue
        if source.size_bytes == int(old["size_bytes"]) and source.mtime_ns == int(old["mtime_ns"]):
            changes.append(SourceChange("unchanged", source, old, "size and mtime match"))
            continue
        old_size = int(old["size_bytes"])
        if source.size_bytes > old_size:
            prefix = sha256_file(source.path, old_size)
            if prefix == old["content_sha256"]:
                changes.append(SourceChange("appended", source, old, "old content is exact prefix"))
                continue
        changes.append(SourceChange("rewritten", source, old, "content is not append-only"))
    for source_id, old in previous.items():
        if source_id not in observed:
            changes.append(SourceChange("deleted", None, old, "source path no longer discovered"))
    order = {"added": 0, "appended": 1, "rewritten": 2, "deleted": 3, "unchanged": 4}
    return sorted(changes, key=lambda item: (order[item.kind], (item.source or item.previous or {}).get("source_path", "") if isinstance(item.source, dict) else str(item.source.path) if item.source else str(item.previous.get("source_path", ""))))


def snapshot_source(config: ProfileConfig, source: SourceCandidate) -> SnapshotFile:
    chunk_root = config.snapshots_dir / "chunks"
    chunks: list[dict[str, Any]] = []
    full_digest = hashlib.sha256()
    line_count = 0
    last_byte = b""
    bytes_read = 0
    remaining = source.size_bytes
    with source.path.open("rb") as handle:
        index = 0
        while remaining > 0:
            block = handle.read(min(config.snapshot_chunk_bytes, remaining))
            if not block:
                break
            bytes_read += len(block)
            remaining -= len(block)
            full_digest.update(block)
            line_count += block.count(b"\n")
            last_byte = block[-1:]
            digest = hashlib.sha256(block).hexdigest()
            relative = Path("chunks") / digest[:2] / f"{digest}.bin"
            target = config.snapshots_dir / relative
            if not target.exists():
                atomic_write_bytes(target, block)
            chunks.append(
                {
                    "index": index,
                    "sha256": digest,
                    "size_bytes": len(block),
                    "cas_relative_path": relative.as_posix(),
                }
            )
            index += 1
    if bytes_read and last_byte != b"\n":
        line_count += 1
    sampled_source = replace(source, size_bytes=bytes_read)
    content_sha256 = full_digest.hexdigest()
    prefix_sha256 = content_sha256
    manifest = {
        "schema_version": "chunked-transcript-snapshot-v1",
        "created_at": utc_now(),
        "source_id": source.source_id,
        "adapter": source.adapter,
        "source_path": str(source.path),
        "relative_path": source.relative_path,
        "thread_id": source.thread_id,
        "size_bytes": bytes_read,
        "mtime_ns": source.mtime_ns,
        "content_sha256": content_sha256,
        "line_count": line_count,
        "chunks": chunks,
    }
    manifest_relative = Path("manifests") / source.source_id / f"{content_sha256}.json"
    manifest_path = config.snapshots_dir / manifest_relative
    if not manifest_path.exists():
        atomic_write_json(manifest_path, manifest)
    return SnapshotFile(
        source=sampled_source,
        content_sha256=content_sha256,
        prefix_sha256=prefix_sha256,
        line_count=line_count,
        manifest_path=manifest_path,
        chunks=tuple(chunks),
    )


def iter_snapshot_lines(snapshot: SnapshotFile, config: ProfileConfig) -> Iterator[tuple[int, int, int, bytes]]:
    line_no = 0
    byte_offset = 0
    pending: list[bytes] = []
    pending_start = 0
    for chunk in snapshot.chunks:
        path = config.snapshots_dir / chunk["cas_relative_path"]
        data = path.read_bytes()
        cursor = 0
        while True:
            newline = data.find(b"\n", cursor)
            if newline < 0:
                if cursor < len(data):
                    if not pending:
                        pending_start = byte_offset + cursor
                    pending.append(data[cursor:])
                break
            piece = data[cursor:newline]
            start = pending_start if pending else byte_offset + cursor
            raw = b"".join([*pending, piece]) if pending else piece
            pending = []
            line_no += 1
            end = byte_offset + newline + 1
            yield line_no, start, end, raw
            cursor = newline + 1
        byte_offset += len(data)
    if pending:
        line_no += 1
        yield line_no, pending_start, byte_offset, b"".join(pending)
