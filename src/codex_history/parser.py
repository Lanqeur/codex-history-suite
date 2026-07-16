from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import ProfileConfig
from .source import SnapshotFile, iter_snapshot_lines
from .util import atomic_write_bytes, canonical_json, normalize_text, stable_id


DATA_URI_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", re.DOTALL)
DATA_URI_BYTES_RE = re.compile(rb"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")


@dataclass
class ParsedEvent:
    event_id: str
    content_sha256: str
    line_no: int
    byte_start: int
    byte_end: int
    timestamp: str | None
    event_type: str
    payload_type: str
    role: str
    text: str
    tool_name: str
    call_id: str
    raw_json: str
    turn_seq: int | None = None
    turn_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedTurn:
    turn_id: str
    source_turn_id: str
    turn_seq: int
    started_at: str | None
    completed_at: str | None
    status: str
    user_text: str
    assistant_text: str
    tool_call_count: int
    tool_output_count: int
    event_count: int
    content_sha256: str
    event_ids: list[str]


@dataclass
class ParsedThread:
    thread_id: str
    title: str
    parent_thread_id: str | None
    first_activity_at: str | None
    last_activity_at: str | None
    events: list[ParsedEvent]
    turns: list[ParsedTurn]
    stats: dict[str, int]
    image_artifacts: list[dict[str, Any]]
    parse_errors: list[dict[str, Any]]


def _extension(mime: str) -> str:
    extension = mimetypes.guess_extension(mime) or ".bin"
    return ".jpg" if extension == ".jpe" else extension


def _store_image(
    blob: bytes,
    mime: str,
    config: ProfileConfig,
    artifacts: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256(blob).hexdigest()
    extension = _extension(mime)
    relative = Path("blobs") / digest[:2] / f"{digest}{extension}"
    target = config.cas_dir / relative
    if not target.exists():
        atomic_write_bytes(target, blob)
    uri = f"codex-history-artifact://sha256/{digest}"
    artifacts.append(
        {
            "sha256": digest,
            "size_bytes": len(blob),
            "mime_type": mime,
            "extension": extension,
            "cas_relative_path": relative.as_posix(),
            "artifact_uri": uri,
        }
    )
    return uri


def _externalize_raw_data_uris(
    raw: bytes,
    config: ProfileConfig,
    artifacts: list[dict[str, Any]],
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
        mime = match.group(1).decode("ascii")
        uri = _store_image(blob, mime, config, artifacts)
        output.extend(raw[cursor : match.start()])
        output.extend(uri.encode("ascii"))
        cursor = match.end()
    if cursor == 0:
        return raw
    output.extend(raw[cursor:])
    return bytes(output)


def _externalize(value: Any, config: ProfileConfig, artifacts: list[dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = DATA_URI_RE.match(value)
        if not match:
            return value
        mime, encoded = match.groups()
        try:
            blob = base64.b64decode(encoded, validate=False)
        except (ValueError, binascii.Error):
            return value
        uri = _store_image(blob, mime, config, artifacts)
        return {
            "$artifact": uri,
            "mime_type": mime,
            "size_bytes": len(blob),
        }
    if isinstance(value, list):
        return [_externalize(item, config, artifacts) for item in value]
    if isinstance(value, dict):
        return {key: _externalize(item, config, artifacts) for key, item in value.items()}
    return value


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    values: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(text, str):
                    values.append(text)
                artifact = item.get("$artifact")
                if isinstance(artifact, str):
                    values.append(artifact)
                image = item.get("image_url")
                if isinstance(image, dict) and image.get("$artifact"):
                    values.append(str(image["$artifact"]))
                elif isinstance(image, str) and image.startswith("codex-history-artifact://"):
                    values.append(image)
    elif isinstance(content, dict):
        for key in ("text", "message", "output", "result"):
            if isinstance(content.get(key), str):
                values.append(content[key])
    return "\n".join(value for value in values if value)


def _event_fields(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    event_type = str(row.get("type") or "unknown")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    role = str(payload.get("role") or "")
    tool_name = str(payload.get("name") or payload.get("tool_name") or "")
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    text = ""
    if event_type == "response_item" and payload_type == "message":
        text = _content_text(payload.get("content"))
    elif event_type == "event_msg" and payload_type == "user_message":
        role = role or "user"
        text = str(payload.get("message") or "")
    elif payload_type in {"function_call", "custom_tool_call"}:
        role = "tool_call"
        text = str(payload.get("arguments") or payload.get("input") or "")
    elif payload_type in {"function_call_output", "custom_tool_call_output"}:
        role = "tool_output"
        text = _content_text(payload.get("output") or payload.get("content"))
    elif payload_type in {"agent_message", "assistant_message"}:
        role = "assistant"
        text = str(payload.get("message") or payload.get("text") or "")
    elif payload_type in {"task_complete", "turn_aborted"}:
        text = str(payload.get("last_agent_message") or payload.get("reason") or "")
    elif "goal" in payload_type:
        goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else payload
        role = "goal"
        objective = str(goal.get("objective") or goal.get("description") or "")
        status = str(goal.get("status") or "unknown")
        text = f"Goal [{status}]: {objective}" if objective else f"Goal status: {status}"
        call_id = str(payload.get("turnId") or payload.get("turn_id") or call_id)
    elif "tool" in payload_type:
        role = "tool_output" if "output" in payload_type or "result" in payload_type else "tool_call"
        text = _content_text(payload.get("output") or payload.get("input") or payload.get("content"))
    return event_type, payload_type, role, normalize_text(text), tool_name or payload_type, call_id


def _parent_thread_id(meta: dict[str, Any]) -> str | None:
    for key in ("parent_thread_id", "source_thread_id", "forked_from_id", "parent_id"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    source = meta.get("thread_source")
    if isinstance(source, dict):
        for key in ("thread_id", "parent_thread_id", "id"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _deduplicated_join(values: Iterable[str]) -> str:
    result: list[str] = []
    for value in values:
        text = normalize_text(value)
        if text and (not result or text != result[-1]):
            result.append(text)
    return "\n\n".join(result)


def parse_snapshot(snapshot: SnapshotFile, config: ProfileConfig) -> ParsedThread:
    events: list[ParsedEvent] = []
    artifacts: list[dict[str, Any]] = [dict(item) for item in snapshot.artifacts]
    errors: list[dict[str, Any]] = []
    content_occurrences: Counter[str] = Counter()
    current_turn_seq: int | None = None
    current_source_turn = ""
    next_turn_seq = 0
    meta = dict(snapshot.source.session_meta)
    first_activity: str | None = None
    last_activity: str | None = None
    for line_no, byte_start, byte_end, raw in iter_snapshot_lines(snapshot, config):
        if not raw.strip():
            continue
        compact_raw = _externalize_raw_data_uris(raw, config, artifacts)
        try:
            row = json.loads(compact_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append({"line_no": line_no, "error": str(error)})
            continue
        if not isinstance(row, dict):
            errors.append({"line_no": line_no, "error": "JSON value is not an object"})
            continue
        normalized = _externalize(row, config, artifacts)
        raw_json = canonical_json(normalized)
        content_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        content_occurrences[content_sha] += 1
        timestamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None
        if timestamp:
            first_activity = min(first_activity, timestamp) if first_activity else timestamp
            last_activity = max(last_activity, timestamp) if last_activity else timestamp
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        event_type, payload_type, role, text, tool_name, call_id = _event_fields(normalized)
        if event_type == "session_meta":
            meta.update(payload)
        if payload_type == "task_started":
            current_turn_seq = next_turn_seq
            next_turn_seq += 1
            current_source_turn = str(payload.get("turn_id") or "")
        elif current_turn_seq is None and (role in {"user", "assistant", "tool_call", "tool_output"}):
            current_turn_seq = next_turn_seq
            next_turn_seq += 1
        source_turn = str(payload.get("turn_id") or payload.get("turnId") or current_source_turn)
        turn_id = (
            stable_id("turn", snapshot.source.thread_id, source_turn)
            if source_turn
            else stable_id("turn", snapshot.source.thread_id, current_turn_seq)
            if current_turn_seq is not None
            else None
        )
        event_id = stable_id(
            "event",
            snapshot.source.thread_id,
            content_sha,
            content_occurrences[content_sha],
        )
        events.append(
            ParsedEvent(
                event_id=event_id,
                content_sha256=content_sha,
                line_no=line_no,
                byte_start=byte_start,
                byte_end=byte_end,
                timestamp=timestamp,
                event_type=event_type,
                payload_type=payload_type,
                role=role,
                text=text,
                tool_name=tool_name,
                call_id=call_id,
                raw_json=raw_json,
                turn_seq=current_turn_seq,
                turn_id=turn_id,
            )
        )
        if payload_type in {"task_complete", "turn_aborted"}:
            current_turn_seq = None
            current_source_turn = ""

    grouped: dict[int, list[ParsedEvent]] = {}
    for event in events:
        if event.turn_seq is not None:
            grouped.setdefault(event.turn_seq, []).append(event)
    turns: list[ParsedTurn] = []
    for turn_seq, items in sorted(grouped.items()):
        source_turn_id = ""
        started_at = None
        completed_at = None
        status = "incomplete"
        for event in items:
            payload = json.loads(event.raw_json).get("payload", {})
            if event.payload_type == "task_started":
                source_turn_id = str(payload.get("turn_id") or "")
                started_at = event.timestamp
                status = "active"
            elif event.payload_type == "task_complete":
                completed_at = event.timestamp
                status = "complete"
            elif event.payload_type == "turn_aborted":
                completed_at = event.timestamp
                status = "aborted"
        turn_id = items[0].turn_id or stable_id("turn", snapshot.source.thread_id, turn_seq)
        digest = hashlib.sha256("\x1f".join(item.content_sha256 for item in items).encode()).hexdigest()
        turns.append(
            ParsedTurn(
                turn_id=turn_id,
                source_turn_id=source_turn_id,
                turn_seq=turn_seq,
                started_at=started_at or items[0].timestamp,
                completed_at=completed_at or items[-1].timestamp,
                status=status,
                user_text=_deduplicated_join(item.text for item in items if item.role == "user"),
                assistant_text=_deduplicated_join(
                    item.text for item in items if item.role == "assistant"
                ),
                tool_call_count=sum(item.role == "tool_call" for item in items),
                tool_output_count=sum(item.role == "tool_output" for item in items),
                event_count=len(items),
                content_sha256=digest,
                event_ids=[item.event_id for item in items],
            )
        )

    stats = {
        "event_count": len(events),
        "turn_count": len(turns),
        "message_count": sum(event.role in {"user", "assistant"} for event in events),
        "user_message_count": sum(event.role == "user" for event in events),
        "assistant_message_count": sum(event.role == "assistant" for event in events),
        "tool_call_count": sum(event.role == "tool_call" for event in events),
        "tool_output_count": sum(event.role == "tool_output" for event in events),
        "goal_event_count": sum("goal" in event.payload_type for event in events),
        "compacted_count": sum("compact" in event.payload_type or "compact" in event.event_type for event in events),
    }
    unique_artifacts = {item["sha256"]: item for item in artifacts}
    return ParsedThread(
        thread_id=snapshot.source.thread_id,
        title=snapshot.source.title,
        parent_thread_id=_parent_thread_id(meta),
        first_activity_at=first_activity,
        last_activity_at=last_activity,
        events=events,
        turns=turns,
        stats=stats,
        image_artifacts=list(unique_artifacts.values()),
        parse_errors=errors,
    )
