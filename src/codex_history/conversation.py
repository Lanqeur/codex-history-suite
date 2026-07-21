from __future__ import annotations

import base64
import bisect
import hashlib
import json
import re
import sqlite3
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .util import atomic_write_json, atomic_write_text, utc_now


CONVERSATION_EXPORT_SCHEMA = "codex-history-conversation-export-v2"
ARTIFACT_URI_RE = re.compile(r"codex-history-artifact://sha256/([0-9a-f]{64})")
SAFE_INLINE_IMAGE_MIMES = {
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
TEXT_ATTACHMENT_MIMES = {
    "application/json",
    "application/toml",
    "application/x-httpd-php",
    "application/x-javascript",
    "application/x-ndjson",
    "application/x-sh",
    "application/xhtml+xml",
    "application/xml",
    "application/yaml",
}
ARCHIVE_EXTENSIONS = {
    ".7z",
    ".bz2",
    ".bundle",
    ".gz",
    ".rar",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
}
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_EMBEDDED_BYTES = 100 * 1024 * 1024
TEXT_PREVIEW_BYTES = 32 * 1024
INTERNAL_CONTEXT_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<plugins_instructions>",
)


@dataclass(frozen=True)
class TurnRange:
    start: int | None = None
    end: int | None = None

    def contains(self, turn_number: int | None) -> bool:
        if turn_number is None:
            return self.start is None and self.end is None
        if self.start is not None and turn_number < self.start:
            return False
        if self.end is not None and turn_number > self.end:
            return False
        return True


def parse_turn_range(value: str) -> TurnRange:
    text = value.strip()
    if not text:
        return TurnRange()
    if ":" not in text:
        number = int(text)
        if number < 1:
            raise ValueError("turn numbers start at 1")
        return TurnRange(number, number)
    left, right = text.split(":", 1)
    start = int(left) if left else None
    end = int(right) if right else None
    if start is not None and start < 1:
        raise ValueError("turn numbers start at 1")
    if end is not None and end < 1:
        raise ValueError("turn numbers start at 1")
    if start is not None and end is not None and start > end:
        raise ValueError("turn range start must not exceed its end")
    return TurnRange(start, end)


def _parse_timestamp(value: str, *, end_of_day: bool = False) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if len(text) == 10:
        text += "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _timestamp_in_range(value: str | None, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    if not value:
        return False
    try:
        timestamp = _parse_timestamp(value)
    except ValueError:
        return False
    if timestamp is None:
        return False
    if since is not None and timestamp < since:
        return False
    if until is not None and timestamp > until:
        return False
    return True


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def list_threads(
    connection: sqlite3.Connection,
    selectors: Sequence[str] = (),
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT thread_id,title,first_activity_at,last_activity_at,turn_count,"
            "user_message_count,assistant_message_count,tool_call_count,tool_output_count,"
            "compacted_count,parent_thread_id FROM threads "
            "ORDER BY COALESCE(last_activity_at,'') DESC,title,thread_id"
        )
    ]
    terms = [item.casefold() for item in selectors if item.strip()]
    if terms:
        rows = [
            row
            for row in rows
            if all(
                term in str(row["thread_id"]).casefold()
                or term in str(row["title"]).casefold()
                for term in terms
            )
        ]
    return rows[: max(1, limit)]


def resolve_threads(
    connection: sqlite3.Connection,
    selectors: Sequence[str],
    scope_selectors: Sequence[str] = (),
) -> list[dict[str, Any]]:
    thread_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT t.*,sf.source_path,sf.content_sha256 AS source_content_sha256,"
            "sf.snapshot_content_sha256,sf.snapshot_format,sf.snapshot_size_bytes "
            "FROM threads t LEFT JOIN source_files sf ON sf.source_id=t.source_id "
            "ORDER BY COALESCE(t.first_activity_at,''),t.thread_id"
        )
    ]
    by_id = {str(row["thread_id"]): row for row in thread_rows}
    selected_ids: list[str] = []

    def add(identifier: str) -> None:
        if identifier in by_id and identifier not in selected_ids:
            selected_ids.append(identifier)

    for scope_selector in scope_selectors:
        exact = connection.execute(
            "SELECT scope_id FROM scopes WHERE scope_id=? OR lower(scope_title)=lower(?) "
            "ORDER BY scope_id",
            (scope_selector, scope_selector),
        ).fetchall()
        scopes = exact or connection.execute(
            "SELECT scope_id FROM scopes WHERE lower(scope_id) LIKE ? OR lower(scope_title) LIKE ? "
            "ORDER BY scope_id",
            (f"%{scope_selector.casefold()}%", f"%{scope_selector.casefold()}%"),
        ).fetchall()
        if not scopes:
            raise ValueError(f"No scope matched: {scope_selector}")
        for scope in scopes:
            for row in connection.execute(
                "SELECT thread_id FROM scope_threads WHERE scope_id=? ORDER BY ordinal",
                (scope["scope_id"],),
            ):
                add(str(row["thread_id"]))

    for selector in selectors:
        value = selector.strip()
        if not value:
            continue
        if value in by_id:
            add(value)
            continue
        exact = [row for row in thread_rows if str(row["title"]).casefold() == value.casefold()]
        matches = exact or [
            row
            for row in thread_rows
            if value.casefold() in str(row["thread_id"]).casefold()
            or value.casefold() in str(row["title"]).casefold()
        ]
        if not matches:
            raise ValueError(f"No thread matched: {selector}")
        for row in matches:
            add(str(row["thread_id"]))

    if not selected_ids:
        raise ValueError("Select at least one thread or scope")
    return [by_id[identifier] for identifier in selected_ids]


class SnapshotReader:
    def __init__(
        self,
        connection: sqlite3.Connection,
        snapshot_root: Path,
        *,
        cache_chunks: int = 8,
    ) -> None:
        self.connection = connection
        self.snapshot_root = snapshot_root
        self.cache_chunks = cache_chunks
        self._indexes: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    def _index(self, source_id: str) -> tuple[list[int], list[dict[str, Any]]]:
        cached = self._indexes.get(source_id)
        if cached is not None:
            return cached
        chunks: list[dict[str, Any]] = []
        starts: list[int] = []
        cursor = 0
        for row in self.connection.execute(
            "SELECT chunk_index,size_bytes,cas_relative_path FROM source_chunks "
            "WHERE source_id=? ORDER BY chunk_index",
            (source_id,),
        ):
            starts.append(cursor)
            item = dict(row)
            item["start"] = cursor
            cursor += int(row["size_bytes"])
            item["end"] = cursor
            chunks.append(item)
        if not chunks:
            raise FileNotFoundError(f"No canonical snapshot chunks for source {source_id}")
        self._indexes[source_id] = (starts, chunks)
        return starts, chunks

    def _chunk_bytes(self, relative_path: str) -> bytes:
        cached = self._cache.get(relative_path)
        if cached is not None:
            self._cache.move_to_end(relative_path)
            return cached
        relative = Path(relative_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe snapshot chunk path: {relative_path}")
        path = self.snapshot_root / relative
        data = path.read_bytes()
        self._cache[relative_path] = data
        self._cache.move_to_end(relative_path)
        while len(self._cache) > self.cache_chunks:
            self._cache.popitem(last=False)
        return data

    def raw_event(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        inline = str(row["raw_json"] or "")
        if inline:
            return json.loads(inline)
        source_id = str(row["source_id"])
        start = int(row["byte_start"])
        end = int(row["byte_end"])
        starts, chunks = self._index(source_id)
        index = max(0, bisect.bisect_right(starts, start) - 1)
        pieces: list[bytes] = []
        while index < len(chunks):
            chunk = chunks[index]
            if int(chunk["start"]) >= end:
                break
            if int(chunk["end"]) > start:
                data = self._chunk_bytes(str(chunk["cas_relative_path"]))
                local_start = max(0, start - int(chunk["start"]))
                local_end = min(len(data), end - int(chunk["start"]))
                pieces.append(data[local_start:local_end])
            index += 1
        raw = b"".join(pieces).rstrip(b"\r\n").decode("utf-8", errors="replace")
        if not raw:
            raise FileNotFoundError(
                f"Snapshot bytes unavailable for {source_id}:{row['line_no']}"
            )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"Snapshot event is not an object at {source_id}:{row['line_no']}")
        return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    values: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                for key in ("text", "input_text", "output_text"):
                    if isinstance(item.get(key), str):
                        values.append(str(item[key]))
                        break
                artifact = item.get("$artifact")
                if isinstance(artifact, str):
                    values.append(artifact)
                image = item.get("image_url")
                if isinstance(image, str) and image.startswith("codex-history-artifact://"):
                    values.append(image)
                elif isinstance(image, dict) and isinstance(image.get("$artifact"), str):
                    values.append(str(image["$artifact"]))
    elif isinstance(content, dict):
        for key in ("text", "message", "output", "result"):
            if key in content:
                values.append(_stringify(content[key]))
    return "\n".join(value for value in values if value)


def _event_content(raw: dict[str, Any], fallback: str) -> str:
    event_type = str(raw.get("type") or "")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    if event_type == "response_item" and payload_type == "message":
        return _content_text(payload.get("content"))
    if payload_type == "user_message":
        return str(payload.get("message") or "")
    if payload_type in {"agent_message", "assistant_message"}:
        return str(payload.get("message") or payload.get("text") or "")
    if payload_type in {"function_call", "custom_tool_call"}:
        return _stringify(payload.get("arguments") or payload.get("input"))
    if payload_type in {"function_call_output", "custom_tool_call_output"}:
        return _content_text(payload.get("output") or payload.get("content"))
    if "goal" in payload_type:
        goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else payload
        objective = str(goal.get("objective") or goal.get("description") or "")
        status = str(goal.get("status") or "unknown")
        return f"Goal [{status}]: {objective}" if objective else f"Goal status: {status}"
    if "tool" in payload_type:
        return _content_text(payload.get("output") or payload.get("input") or payload.get("content"))
    return fallback


def _is_internal_context(row: sqlite3.Row, content: str) -> bool:
    if row["event_type"] != "response_item" or row["payload_type"] != "message":
        return False
    stripped = content.lstrip()
    return any(stripped.startswith(prefix) for prefix in INTERNAL_CONTEXT_PREFIXES)


def _dedupe_text(value: str) -> str:
    without_artifacts = ARTIFACT_URI_RE.sub("", value)
    return re.sub(r"\s+", " ", without_artifacts).strip()


def _preferred_duplicate_counts(rows: Sequence[sqlite3.Row]) -> Counter[tuple[str, str, str]]:
    preferred: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        if row["event_type"] != "event_msg":
            continue
        role = str(row["role"])
        payload_type = str(row["payload_type"])
        if role == "user" and payload_type != "user_message":
            continue
        if role == "assistant" and payload_type not in {"agent_message", "assistant_message"}:
            continue
        if role not in {"user", "assistant"}:
            continue
        preferred[
            (str(row["turn_id"] or ""), role, _dedupe_text(str(row["text"])))
        ] += 1
    return preferred


def _artifact_records(connection: sqlite3.Connection, digests: set[str]) -> dict[str, dict[str, Any]]:
    if not digests or not _table_exists(connection, "artifact_files"):
        return {}
    result: dict[str, dict[str, Any]] = {}
    values = sorted(digests)
    for start in range(0, len(values), 500):
        batch = values[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            "SELECT sha256,size_bytes,cas_relative_path,artifact_uri,mime_type,extension,"
            "source_open_path "
            f"FROM artifact_files WHERE sha256 IN ({placeholders})",
            batch,
        ):
            result[str(row["sha256"])] = dict(row)
    return result


def _resolve_artifact(relative_path: str, roots: Sequence[Path]) -> Path | None:
    relative = Path(relative_path.replace("\\", "/"))
    parts = relative.parts[1:] if relative.parts and relative.parts[0] == "cas" else relative.parts
    if not parts or relative.is_absolute() or ".." in parts:
        return None
    for root in roots:
        candidate = root / Path(*parts)
        if candidate.is_file():
            return candidate
    return None


def _artifact_observations(
    connection: sqlite3.Connection, event_ids: Sequence[str]
) -> dict[str, list[dict[str, str]]]:
    if not event_ids or not _table_exists(connection, "artifact_observations"):
        return {}
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    values = sorted(set(event_ids))
    for start in range(0, len(values), 500):
        batch = values[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            "SELECT event_id,artifact_sha256,original_path,capture_method "
            f"FROM artifact_observations WHERE event_id IN ({placeholders}) "
            "ORDER BY event_id,original_path,artifact_sha256",
            batch,
        ):
            result[str(row["event_id"])].append(
                {
                    "sha256": str(row["artifact_sha256"]),
                    "original_path": str(row["original_path"] or ""),
                    "capture_method": str(row["capture_method"] or ""),
                }
            )
    return result


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _attachment_kind(mime_type: str, extension: str) -> str:
    mime = mime_type.casefold()
    suffix = extension.casefold()
    if mime in SAFE_INLINE_IMAGE_MIMES:
        return "image"
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if mime.startswith("text/") or mime in TEXT_ATTACHMENT_MIMES:
        return "text"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    return "document"


def _attachment_name(
    source_paths: Sequence[str],
    extension: str,
    digest: str,
    capture_methods: Sequence[str],
) -> str:
    if any(method.startswith("git_") for method in capture_methods):
        return f"repository-checkpoint-{digest[:16]}{extension or '.bin'}"
    for value in source_paths:
        name = Path(value.replace("\\", "/")).name.strip()
        if (
            name
            and not name.startswith("inline-image:")
            and (not extension or name.casefold().endswith(extension.casefold()))
        ):
            return name
    return f"artifact-{digest[:16]}{extension or '.bin'}"


def _message_artifact_refs(
    messages: Sequence[dict[str, Any]],
    observations: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    result: dict[str, list[dict[str, Any]]] = {}
    digests: set[str] = set()
    for message in messages:
        refs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for match in ARTIFACT_URI_RE.finditer(str(message["content"])):
            digest = match.group(1)
            refs.setdefault(
                digest,
                {"sha256": digest, "uri": match.group(0), "source_paths": [], "capture_methods": []},
            )
        for observation in observations.get(str(message["event_id"]), []):
            digest = observation["sha256"]
            ref = refs.setdefault(
                digest,
                {
                    "sha256": digest,
                    "uri": f"codex-history-artifact://sha256/{digest}",
                    "source_paths": [],
                    "capture_methods": [],
                },
            )
            path = observation["original_path"]
            method = observation["capture_method"]
            if path and path not in ref["source_paths"]:
                ref["source_paths"].append(path)
            if method and method not in ref["capture_methods"]:
                ref["capture_methods"].append(method)
        result[str(message["event_id"])] = list(refs.values())
        digests.update(refs)
    return result, digests


def _attach_artifacts(
    connection: sqlite3.Connection,
    messages: list[dict[str, Any]],
    artifact_roots: Sequence[Path],
    *,
    embed_images: bool,
    embed_attachments: bool,
    max_attachment_bytes: int,
    max_embedded_bytes: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    observations = _artifact_observations(
        connection, [str(message["event_id"]) for message in messages]
    )
    refs_by_event, digests = _message_artifact_refs(messages, observations)
    records = _artifact_records(connection, digests)
    payloads: dict[str, dict[str, Any]] = {}
    base: dict[str, dict[str, Any]] = {}
    embedded_images = 0
    embedded_documents = 0
    embedded_bytes = 0
    missing: set[str] = set()
    available: set[str] = set()
    skipped: set[str] = set()

    for digest in sorted(digests):
        record = records.get(digest)
        attachment: dict[str, Any] = {
            "sha256": digest,
            "uri": f"codex-history-artifact://sha256/{digest}",
            "available": False,
            "embedded": False,
            "kind": "document",
            "mime_type": "application/octet-stream",
            "extension": "",
            "size_bytes": 0,
            "size_human": "unknown",
            "status": "missing_record",
            "can_open": False,
        }
        if not record:
            missing.add(digest)
            base[digest] = attachment
            continue
        mime = str(record.get("mime_type") or "application/octet-stream")
        extension = str(record.get("extension") or "")
        size = int(record.get("size_bytes") or 0)
        kind = _attachment_kind(mime, extension)
        attachment.update(
            {
                "mime_type": mime,
                "extension": extension,
                "size_bytes": size,
                "size_human": _human_bytes(size),
                "kind": kind,
                "can_open": kind in {"image", "pdf", "text"},
                "status": "missing_file",
            }
        )
        path = _resolve_artifact(str(record.get("cas_relative_path") or ""), artifact_roots)
        if not path:
            missing.add(digest)
            base[digest] = attachment
            continue
        attachment.update({"available": True, "status": "available_not_embedded"})
        available.add(digest)
        wants_embedding = embed_attachments or (embed_images and kind == "image")
        if not wants_embedding:
            base[digest] = attachment
            continue
        if size > max_attachment_bytes:
            attachment["status"] = "skipped_file_limit"
            skipped.add(digest)
            base[digest] = attachment
            continue
        if embedded_bytes + size > max_embedded_bytes:
            attachment["status"] = "skipped_total_limit"
            skipped.add(digest)
            base[digest] = attachment
            continue
        data = path.read_bytes()
        if len(data) > max_attachment_bytes:
            attachment["size_bytes"] = len(data)
            attachment["size_human"] = _human_bytes(len(data))
            attachment["status"] = "skipped_file_limit"
            skipped.add(digest)
            base[digest] = attachment
            continue
        data_mime = mime
        if mime in {"image/svg+xml", "text/html", "application/xhtml+xml"}:
            data_mime = "application/octet-stream"
            attachment["can_open"] = False
        payload: dict[str, Any] = {
            "sha256": digest,
            "mime_type": mime,
            "data_url": f"data:{data_mime};base64,{base64.b64encode(data).decode('ascii')}",
        }
        if kind == "text":
            payload["text_preview"] = data[:TEXT_PREVIEW_BYTES].decode("utf-8", errors="replace")
            payload["text_preview_truncated"] = len(data) > TEXT_PREVIEW_BYTES
        payloads[digest] = payload
        attachment.update({"embedded": True, "status": "embedded"})
        embedded_bytes += len(data)
        if kind == "image":
            embedded_images += 1
        else:
            embedded_documents += 1
        base[digest] = attachment

    attachment_occurrences = 0
    for message in messages:
        attachments: list[dict[str, Any]] = []
        for ref in refs_by_event.get(str(message["event_id"]), []):
            digest = str(ref["sha256"])
            attachment = dict(base[digest])
            source_paths = list(ref["source_paths"])
            if not source_paths:
                fallback = str(records.get(digest, {}).get("source_open_path") or "")
                if fallback:
                    source_paths.append(fallback)
            attachment.update(
                {
                    "uri": ref["uri"],
                    "source_paths": source_paths,
                    "capture_methods": list(ref["capture_methods"]),
                    "display_name": _attachment_name(
                        source_paths,
                        str(attachment["extension"]),
                        digest,
                        ref["capture_methods"],
                    ),
                }
            )
            attachments.append(attachment)
            attachment_occurrences += 1
        message["attachments"] = attachments
    image_digests = {digest for digest, item in base.items() if item["kind"] == "image"}
    return (
        {
            "referenced_attachments": len(digests),
            "attachment_occurrences": attachment_occurrences,
            "available_attachments": len(available),
            "embedded_attachments": len(payloads),
            "embedded_attachment_bytes": embedded_bytes,
            "embedded_documents": embedded_documents,
            "skipped_attachments": len(skipped),
            "missing_attachments": len(missing),
            "referenced_images": len(image_digests),
            "embedded_images": embedded_images,
            "embedded_image_bytes": sum(
                int(base[digest]["size_bytes"])
                for digest in image_digests
                if base[digest]["embedded"]
            ),
            "missing_images": len(image_digests & missing),
        },
        payloads,
    )


def build_conversation_export(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    *,
    selectors: Sequence[str],
    scope_selectors: Sequence[str] = (),
    turn_range: TurnRange = TurnRange(),
    since: str = "",
    until: str = "",
    include_tools: bool = True,
    include_goals: bool = True,
    include_internal: bool = False,
    include_raw: bool = False,
    embed_images: bool = False,
    embed_attachments: bool = False,
    artifact_roots: Sequence[Path] = (),
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_embedded_bytes: int = DEFAULT_MAX_EMBEDDED_BYTES,
    title: str = "Codex conversation evidence",
) -> dict[str, Any]:
    if max_attachment_bytes <= 0 or max_embedded_bytes <= 0:
        raise ValueError("attachment size limits must be positive")
    selected = resolve_threads(connection, selectors, scope_selectors)
    since_time = _parse_timestamp(since)
    until_time = _parse_timestamp(until, end_of_day=True)
    if since_time and until_time and since_time > until_time:
        raise ValueError("since must not be later than until")
    reader = SnapshotReader(connection, snapshot_root)
    threads: list[dict[str, Any]] = []
    all_messages: list[dict[str, Any]] = []
    role_counts: Counter[str] = Counter()
    raw_bytes = 0

    for thread_order, thread in enumerate(selected):
        rows = connection.execute(
            "SELECT ce.*,tu.turn_seq,tu.status AS turn_status FROM canonical_events ce "
            "LEFT JOIN turns tu ON tu.turn_id=ce.turn_id "
            "WHERE ce.thread_id=? ORDER BY ce.line_no,ce.event_id",
            (thread["thread_id"],),
        ).fetchall()
        export_rows: list[sqlite3.Row] = []
        for row in rows:
            role = str(row["role"] or "")
            if role not in {"user", "assistant", "tool_call", "tool_output", "goal"}:
                continue
            if role in {"tool_call", "tool_output"} and not include_tools:
                continue
            if role == "goal" and not include_goals:
                continue
            turn_number = int(row["turn_seq"]) + 1 if row["turn_seq"] is not None else None
            if not turn_range.contains(turn_number):
                continue
            if not _timestamp_in_range(row["timestamp"], since_time, until_time):
                continue
            export_rows.append(row)

        duplicate_counts = _preferred_duplicate_counts(export_rows)
        duplicate_scan = duplicate_counts.copy()
        supplemental_artifacts: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for row in export_rows:
            if (
                row["event_type"] != "response_item"
                or row["payload_type"] != "message"
                or row["role"] not in {"user", "assistant"}
            ):
                continue
            key = (
                str(row["turn_id"] or ""),
                str(row["role"]),
                _dedupe_text(str(row["text"])),
            )
            if duplicate_scan[key] <= 0:
                continue
            duplicate_scan[key] -= 1
            raw = reader.raw_event(row)
            raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            for match in ARTIFACT_URI_RE.finditer(raw_text):
                uri = match.group(0)
                if uri not in supplemental_artifacts[key]:
                    supplemental_artifacts[key].append(uri)
        duplicate_suppressed = 0
        internal_suppressed = 0
        thread_messages: list[dict[str, Any]] = []
        for row in export_rows:
            role = str(row["role"] or "")
            turn_number = int(row["turn_seq"]) + 1 if row["turn_seq"] is not None else None
            if (
                row["event_type"] == "response_item"
                and row["payload_type"] == "message"
                and role in {"user", "assistant"}
            ):
                key = (
                    str(row["turn_id"] or ""),
                    role,
                    _dedupe_text(str(row["text"])),
                )
                if duplicate_counts[key] > 0:
                    duplicate_counts[key] -= 1
                    duplicate_suppressed += 1
                    continue
            raw = reader.raw_event(row)
            content = _event_content(raw, str(row["text"] or ""))
            key = (
                str(row["turn_id"] or ""),
                role,
                _dedupe_text(str(row["text"])),
            )
            for uri in supplemental_artifacts.get(key, []):
                if uri not in content:
                    content = f"{content}\n{uri}" if content else uri
            if not content and role not in {"goal"}:
                continue
            internal = _is_internal_context(row, content)
            if internal and not include_internal:
                internal_suppressed += 1
                continue
            raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            raw_bytes += len(raw_text.encode("utf-8"))
            message = {
                "id": str(row["event_id"]),
                "event_id": str(row["event_id"]),
                "content_sha256": str(row["content_sha256"]),
                "thread_id": str(thread["thread_id"]),
                "thread_order": thread_order,
                "turn_id": str(row["turn_id"] or ""),
                "turn_number": turn_number,
                "turn_status": str(row["turn_status"] or ""),
                "line_no": int(row["line_no"]),
                "timestamp": str(row["timestamp"] or ""),
                "event_type": str(row["event_type"]),
                "payload_type": str(row["payload_type"]),
                "role": role,
                "tool_name": str(row["tool_name"] or ""),
                "call_id": str(row["call_id"] or ""),
                "content": content,
                "internal": internal,
                "attachments": [],
                "raw_event": raw if include_raw else None,
            }
            thread_messages.append(message)
            all_messages.append(message)
            role_counts[role] += 1
        threads.append(
            {
                "thread_id": str(thread["thread_id"]),
                "title": str(thread["title"]),
                "parent_thread_id": str(thread.get("parent_thread_id") or ""),
                "first_activity_at": str(thread.get("first_activity_at") or ""),
                "last_activity_at": str(thread.get("last_activity_at") or ""),
                "source_path": str(thread.get("source_path") or ""),
                "source_content_sha256": str(thread.get("source_content_sha256") or ""),
                "snapshot_content_sha256": str(thread.get("snapshot_content_sha256") or ""),
                "snapshot_format": str(thread.get("snapshot_format") or ""),
                "snapshot_size_bytes": int(thread.get("snapshot_size_bytes") or 0),
                "message_count": len(thread_messages),
                "duplicate_events_suppressed": duplicate_suppressed,
                "internal_events_suppressed": internal_suppressed,
                "first_exported_at": thread_messages[0]["timestamp"] if thread_messages else "",
                "last_exported_at": thread_messages[-1]["timestamp"] if thread_messages else "",
            }
        )

    artifact_report, artifact_payloads = _attach_artifacts(
        connection,
        all_messages,
        artifact_roots,
        embed_images=embed_images,
        embed_attachments=embed_attachments,
        max_attachment_bytes=max_attachment_bytes,
        max_embedded_bytes=max_embedded_bytes,
    )

    digest = hashlib.sha256()
    for message in all_messages:
        digest.update(str(message["event_id"]).encode("ascii"))
        digest.update(b"\n")
    metadata = {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key,value FROM metadata")
    }
    return {
        "schema_version": CONVERSATION_EXPORT_SCHEMA,
        "export_id": f"conversation-{digest.hexdigest()[:24]}",
        "generated_at": utc_now(),
        "title": title,
        "selection": {
            "thread_selectors": list(selectors),
            "scope_selectors": list(scope_selectors),
            "turn_range": {"start": turn_range.start, "end": turn_range.end},
            "since": since,
            "until": until,
            "include_tools": include_tools,
            "include_goals": include_goals,
            "include_internal": include_internal,
            "include_raw": include_raw,
            "embed_images": embed_images,
            "embed_attachments": embed_attachments,
            "max_attachment_bytes": max_attachment_bytes,
            "max_embedded_bytes": max_embedded_bytes,
        },
        "authority": {
            "knowledge_generated_at": metadata.get("generated_at", ""),
            "canonical_snapshot_complete": metadata.get("canonical_snapshot_complete", ""),
            "canonical_payload_storage": metadata.get("canonical_payload_storage", ""),
            "last_ingest_run": metadata.get("last_ingest_run", ""),
        },
        "statistics": {
            "threads": len(threads),
            "messages": len(all_messages),
            "roles": dict(sorted(role_counts.items())),
            "raw_event_bytes": raw_bytes,
            **artifact_report,
        },
        "threads": threads,
        "messages": all_messages,
        "artifacts": artifact_payloads,
    }


def write_conversation_export(
    payload: dict[str, Any],
    output: Path,
    *,
    output_format: str,
    force: bool = False,
) -> dict[str, Any]:
    target = output.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"Output already exists: {target}; pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        atomic_write_json(target, payload)
    elif output_format == "html":
        from .conversation_viewer import render_conversation_html

        atomic_write_text(target, render_conversation_html(payload))
    else:
        raise ValueError(f"Unsupported conversation export format: {output_format}")
    return {
        "output": str(target),
        "format": output_format,
        "size_bytes": target.stat().st_size,
        "export_id": payload["export_id"],
        "threads": payload["statistics"]["threads"],
        "messages": payload["statistics"]["messages"],
        "roles": payload["statistics"]["roles"],
        "embedded_images": payload["statistics"]["embedded_images"],
        "referenced_attachments": payload["statistics"]["referenced_attachments"],
        "embedded_attachments": payload["statistics"]["embedded_attachments"],
        "missing_attachments": payload["statistics"]["missing_attachments"],
    }
