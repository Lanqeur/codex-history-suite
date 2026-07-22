from __future__ import annotations

import hashlib
import base64
import binascii
import json
import mimetypes
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import ProfileConfig
from .util import atomic_write_bytes, atomic_write_json, read_json, sha256_file, stable_id, utc_now


SOURCE_IDENTITY_FILE = "source_identity.jsonl"
DATA_URI_BYTES_RE = re.compile(
    rb"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)"
)


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
    declared_size_bytes: int | None = None
    declared_content_sha256: str = ""
    snapshot_format: str = "raw-jsonl"
    declared_artifacts: tuple[dict[str, Any], ...] = ()


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
    snapshot_content_sha256: str
    snapshot_size_bytes: int
    line_count: int
    manifest_path: Path
    chunks: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...] = ()


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


def _portable_identities(root: Path) -> dict[str, dict[str, Any]]:
    path = root / SOURCE_IDENTITY_FILE
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            relative = str(row.get("relative_path") or "")
            source_id = str(row.get("source_id") or "")
            if relative and source_id:
                result[relative] = {
                    "source_id": source_id,
                    "thread_id": str(row.get("thread_id") or ""),
                    "title": str(row.get("title") or ""),
                    "source_size_bytes": str(row.get("source_size_bytes") or ""),
                    "source_content_sha256": str(row.get("source_content_sha256") or ""),
                    "snapshot_format": str(row.get("snapshot_format") or ""),
                    "artifacts": [
                        dict(item)
                        for item in row.get("artifacts", [])
                        if isinstance(item, dict)
                    ],
                }
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
        portable_identities = _portable_identities(root)
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
            portable = portable_identities.get(relative, {})
            thread_id = portable.get("thread_id") or thread_id
            title = portable.get("title") or titles.get(thread_id) or str(
                payload.get("title") or thread_id
            )
            is_archived = "archived_sessions" in path.parts
            source_id = portable.get("source_id") or stable_id(
                "source", "codex-jsonl", resolved
            )
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
                    declared_size_bytes=(
                        int(portable["source_size_bytes"])
                        if portable.get("source_size_bytes")
                        else None
                    ),
                    declared_content_sha256=portable.get("source_content_sha256", ""),
                    snapshot_format=portable.get("snapshot_format") or "raw-jsonl",
                    declared_artifacts=tuple(portable.get("artifacts") or ()),
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
        logical_size = source.declared_size_bytes or source.size_bytes
        logical_sha = source.declared_content_sha256
        if logical_sha:
            if logical_sha == str(old["content_sha256"]):
                changes.append(
                    SourceChange("unchanged", source, old, "portable source generation matches")
                )
            elif logical_size > int(old["size_bytes"]):
                changes.append(
                    SourceChange("appended", source, old, "portable source generation advanced")
                )
            else:
                changes.append(
                    SourceChange("rewritten", source, old, "portable source generation changed")
                )
            continue
        if source.size_bytes == int(old["size_bytes"]):
            if source.mtime_ns == int(old["mtime_ns"]):
                changes.append(SourceChange("unchanged", source, old, "size and mtime match"))
                continue
            if sha256_file(source.path) == old["content_sha256"]:
                changes.append(
                    SourceChange("unchanged", source, old, "content hash matches; mtime changed")
                )
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


def _snapshot_artifact(
    blob: bytes,
    mime: str,
    config: ProfileConfig,
    artifacts: dict[str, dict[str, Any]],
) -> str:
    digest = hashlib.sha256(blob).hexdigest()
    extension = mimetypes.guess_extension(mime) or ".bin"
    extension = ".jpg" if extension == ".jpe" else extension
    relative = Path("blobs") / digest[:2] / f"{digest}{extension}"
    target = config.cas_dir / relative
    if not target.exists():
        atomic_write_bytes(target, blob)
    uri = f"codex-history-artifact://sha256/{digest}"
    artifacts[digest] = {
        "sha256": digest,
        "size_bytes": len(blob),
        "mime_type": mime,
        "extension": extension,
        "cas_relative_path": relative.as_posix(),
        "artifact_uri": uri,
    }
    return uri


def _normalize_snapshot_line(
    raw: bytes,
    config: ProfileConfig,
    artifacts: dict[str, dict[str, Any]],
) -> bytes:
    if b"data:image/" not in raw:
        return raw
    output = bytearray()
    cursor = 0
    for match in DATA_URI_BYTES_RE.finditer(raw):
        try:
            blob = base64.b64decode(match.group(2), validate=False)
        except (ValueError, binascii.Error):
            continue
        uri = _snapshot_artifact(
            blob, match.group(1).decode("ascii"), config, artifacts
        )
        output.extend(raw[cursor : match.start()])
        output.extend(uri.encode("ascii"))
        cursor = match.end()
    if cursor == 0:
        return raw
    output.extend(raw[cursor:])
    return bytes(output)


def snapshot_source(config: ProfileConfig, source: SourceCandidate) -> SnapshotFile:
    chunks: list[dict[str, Any]] = []
    source_digest = hashlib.sha256()
    snapshot_digest = hashlib.sha256()
    artifacts: dict[str, dict[str, Any]] = {
        str(item["sha256"]): dict(item)
        for item in source.declared_artifacts
        if item.get("sha256")
    }
    line_count = 0
    last_byte = b""
    source_bytes_read = 0
    snapshot_bytes = 0
    remaining = source.size_bytes
    pending = bytearray()

    def emit(block: bytes) -> None:
        digest = hashlib.sha256(block).hexdigest()
        relative = Path("chunks") / digest[:2] / f"{digest}.bin"
        target = config.snapshots_dir / relative
        if not target.exists():
            atomic_write_bytes(target, block)
        chunks.append(
            {
                "index": len(chunks),
                "sha256": digest,
                "size_bytes": len(block),
                "cas_relative_path": relative.as_posix(),
            }
        )

    with source.path.open("rb") as handle:
        while remaining > 0:
            raw = handle.readline(remaining)
            if not raw:
                break
            source_bytes_read += len(raw)
            remaining -= len(raw)
            source_digest.update(raw)
            line_count += raw.count(b"\n")
            last_byte = raw[-1:]
            normalized = (
                raw
                if source.snapshot_format == "normalized-jsonl-v1"
                else _normalize_snapshot_line(raw, config, artifacts)
            )
            snapshot_digest.update(normalized)
            snapshot_bytes += len(normalized)
            pending.extend(normalized)
            while len(pending) >= config.snapshot_chunk_bytes:
                emit(bytes(pending[: config.snapshot_chunk_bytes]))
                del pending[: config.snapshot_chunk_bytes]
    if pending:
        emit(bytes(pending))
    if source_bytes_read and last_byte != b"\n":
        line_count += 1
    logical_size = source.declared_size_bytes or source_bytes_read
    sampled_source = replace(source, size_bytes=logical_size)
    content_sha256 = source.declared_content_sha256 or source_digest.hexdigest()
    prefix_sha256 = content_sha256
    snapshot_content_sha256 = snapshot_digest.hexdigest()
    manifest = {
        "schema_version": "chunked-transcript-snapshot-v1",
        "created_at": utc_now(),
        "source_id": source.source_id,
        "adapter": source.adapter,
        "source_path": str(source.path),
        "relative_path": source.relative_path,
        "thread_id": source.thread_id,
        "size_bytes": logical_size,
        "mtime_ns": source.mtime_ns,
        "content_sha256": content_sha256,
        "snapshot_format": "normalized-jsonl-v1",
        "snapshot_size_bytes": snapshot_bytes,
        "snapshot_content_sha256": snapshot_content_sha256,
        "line_count": line_count,
        "chunks": chunks,
        "artifacts": list(artifacts.values()),
    }
    manifest_relative = (
        Path("manifests")
        / source.source_id
        / f"{content_sha256}-{snapshot_content_sha256}.json"
    )
    manifest_path = config.snapshots_dir / manifest_relative
    if not manifest_path.exists():
        atomic_write_json(manifest_path, manifest)
    return SnapshotFile(
        source=sampled_source,
        content_sha256=content_sha256,
        prefix_sha256=prefix_sha256,
        snapshot_content_sha256=snapshot_content_sha256,
        snapshot_size_bytes=snapshot_bytes,
        line_count=line_count,
        manifest_path=manifest_path,
        chunks=tuple(chunks),
        artifacts=tuple(artifacts.values()),
    )


def snapshot_appended_source(
    config: ProfileConfig,
    source: SourceCandidate,
    previous: dict[str, Any],
    previous_chunks: Iterable[dict[str, Any]],
) -> SnapshotFile:
    """Reuse normalized prefix chunks and normalize only a local raw append suffix."""
    if source.declared_size_bytes is not None:
        raise ValueError("Portable normalized sources use the full snapshot compatibility path")
    old_source_size = int(previous.get("size_bytes") or 0)
    old_snapshot_size = int(previous.get("snapshot_size_bytes") or 0)
    if old_source_size <= 0 or old_snapshot_size <= 0:
        raise ValueError("Append snapshot has no reusable prefix")
    chunks = [dict(item) for item in previous_chunks]
    if not chunks:
        raise ValueError("Append snapshot has no reusable chunks")
    with source.path.open("rb") as handle:
        handle.seek(old_source_size - 1)
        if handle.read(1) != b"\n":
            raise ValueError("Append prefix does not end at a JSONL line boundary")

    previous_manifest = read_json(Path(str(previous.get("snapshot_manifest_path") or "")), {}) or {}
    artifacts: dict[str, dict[str, Any]] = {
        str(item["sha256"]): dict(item)
        for item in previous_manifest.get("artifacts", [])
        if item.get("sha256")
    }
    output_chunks: list[dict[str, Any]] = []
    pending = bytearray()
    snapshot_digest = hashlib.sha256()
    for chunk in chunks:
        path = config.snapshots_dir / str(chunk["cas_relative_path"])
        data = path.read_bytes()
        snapshot_digest.update(data)
    if int(chunks[-1]["size_bytes"]) < config.snapshot_chunk_bytes:
        last = chunks.pop()
        pending.extend(
            (config.snapshots_dir / str(last["cas_relative_path"])).read_bytes()
        )
    for index, chunk in enumerate(chunks):
        output_chunks.append(
            {
                "index": index,
                "sha256": str(chunk.get("chunk_sha256") or chunk.get("sha256")),
                "size_bytes": int(chunk["size_bytes"]),
                "cas_relative_path": str(chunk["cas_relative_path"]),
            }
        )

    def emit(block: bytes) -> None:
        digest = hashlib.sha256(block).hexdigest()
        relative = Path("chunks") / digest[:2] / f"{digest}.bin"
        target = config.snapshots_dir / relative
        if not target.exists():
            atomic_write_bytes(target, block)
        output_chunks.append(
            {
                "index": len(output_chunks),
                "sha256": digest,
                "size_bytes": len(block),
                "cas_relative_path": relative.as_posix(),
            }
        )

    source_digest = hashlib.sha256()
    suffix_lines = 0
    suffix_snapshot_bytes = 0
    suffix_last_byte = b""
    with source.path.open("rb") as handle:
        remaining_prefix = old_source_size
        while remaining_prefix:
            block = handle.read(min(1024 * 1024, remaining_prefix))
            if not block:
                raise ValueError("Append source became shorter while snapshotting")
            source_digest.update(block)
            remaining_prefix -= len(block)
        while True:
            raw = handle.readline()
            if not raw:
                break
            source_digest.update(raw)
            suffix_lines += raw.count(b"\n")
            suffix_last_byte = raw[-1:]
            normalized = _normalize_snapshot_line(raw, config, artifacts)
            snapshot_digest.update(normalized)
            suffix_snapshot_bytes += len(normalized)
            pending.extend(normalized)
            while len(pending) >= config.snapshot_chunk_bytes:
                emit(bytes(pending[: config.snapshot_chunk_bytes]))
                del pending[: config.snapshot_chunk_bytes]
    if pending:
        emit(bytes(pending))
    if suffix_snapshot_bytes and suffix_last_byte != b"\n":
        suffix_lines += 1
    content_sha256 = source_digest.hexdigest()
    snapshot_content_sha256 = snapshot_digest.hexdigest()
    snapshot_size = old_snapshot_size + suffix_snapshot_bytes
    line_count = int(previous.get("line_count") or 0) + suffix_lines
    manifest = {
        "schema_version": "chunked-transcript-snapshot-v1",
        "created_at": utc_now(),
        "source_id": source.source_id,
        "adapter": source.adapter,
        "source_path": str(source.path),
        "relative_path": source.relative_path,
        "thread_id": source.thread_id,
        "size_bytes": source.size_bytes,
        "mtime_ns": source.mtime_ns,
        "content_sha256": content_sha256,
        "snapshot_format": "normalized-jsonl-v1",
        "snapshot_size_bytes": snapshot_size,
        "snapshot_content_sha256": snapshot_content_sha256,
        "line_count": line_count,
        "chunks": output_chunks,
        "artifacts": list(artifacts.values()),
        "append_reused_prefix": True,
        "reused_prefix_chunks": len(chunks),
    }
    manifest_relative = (
        Path("manifests")
        / source.source_id
        / f"{content_sha256}-{snapshot_content_sha256}.json"
    )
    manifest_path = config.snapshots_dir / manifest_relative
    if not manifest_path.exists():
        atomic_write_json(manifest_path, manifest)
    return SnapshotFile(
        source=source,
        content_sha256=content_sha256,
        prefix_sha256=content_sha256,
        snapshot_content_sha256=snapshot_content_sha256,
        snapshot_size_bytes=snapshot_size,
        line_count=line_count,
        manifest_path=manifest_path,
        chunks=tuple(output_chunks),
        artifacts=tuple(artifacts.values()),
    )


def iter_snapshot_lines(
    snapshot: SnapshotFile,
    config: ProfileConfig,
    *,
    start_line: int = 0,
    start_byte: int = 0,
) -> Iterator[tuple[int, int, int, bytes]]:
    line_no = start_line
    byte_offset = 0
    pending: list[bytes] = []
    pending_start = 0
    for chunk in snapshot.chunks:
        path = config.snapshots_dir / chunk["cas_relative_path"]
        chunk_size = int(chunk["size_bytes"])
        if byte_offset + chunk_size <= start_byte:
            byte_offset += chunk_size
            continue
        data = path.read_bytes()
        cursor = max(0, start_byte - byte_offset)
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
