from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Iterable

from .parser import ParsedEvent, ParsedThread, ParsedTurn
from .config import ProfileConfig
from .source import SnapshotFile
from .util import canonical_json, normalize_text, stable_id, truncate, utc_now


FAILURE_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback|exit code [1-9]|timed out|timeout|blocked)\b|失败|报错|异常|阻塞|超时",
    re.IGNORECASE,
)
VERIFIED_RE = re.compile(
    r"\b([1-9]\d* passed|exit code 0|tests? passed|build succeeded|verified)\b|测试通过|构建成功|已验证",
    re.IGNORECASE,
)
DECISION_RE = re.compile(r"决定|采用|选择|同意|确定|保留|放弃|\b(decided|choose|adopt|selected)\b", re.IGNORECASE)
PREFERENCE_RE = re.compile(r"必须|不要|不需要|希望|偏好|要求|只允许|\b(prefer|must|should not|require)\b", re.IGNORECASE)
CAPABILITY_RE = re.compile(r"完成|实现|通过|修复|支持|生成|成功|\b(implemented|completed|fixed|passed|supports?)\b", re.IGNORECASE)
UNRESOLVED_RE = re.compile(r"待|仍需|尚未|未解决|不确定|计划|下一步|\b(todo|pending|unresolved|uncertain|planned)\b", re.IGNORECASE)
HISTORY_QUERY_CALL_RE = re.compile(
    r"(?:codex_history\.py|(?:codex[-_]history[^\s/]*/)?history\.py)\s+"
    r"(?:search|context|trace|claims|compare|asset|unresolved|stats|conversation|library\s+search)\b",
    re.IGNORECASE,
)
HISTORY_SUPPORT_CALL_RE = re.compile(
    r"(?:"
    r"[/\\]\.codex[/\\](?:skills|plugins)[/\\].*?codex-history.*?"
    r"(?:SKILL\.md|references[/\\])"
    r"|[/\\]\.local[/\\]share[/\\]codex-history[/\\]"
    r"|CodexTranscriptArchive[/\\].*?codex[_-]history.*?\.sqlite3"
    r")",
    re.IGNORECASE,
)
HISTORY_OUTPUT_MARKERS = (
    "# Codex History Context",
    "# Linked knowledge",
    '"semantic_expansions"',
    '"retrieval_score"',
)
ENVIRONMENT_CONTEXT_RE = re.compile(
    r"<environment_context>.*?</environment_context>\s*",
    re.IGNORECASE | re.DOTALL,
)
MAX_DETERMINISTIC_ASSET_CHARS = 1_600
MIN_HISTORY_USER_ASSERTION_CHARS = 800
DEFAULT_ALIASES = (
    ("wsl2", "wsl", "technical", 1.0),
    ("visual studio code", "vscode", "technical", 1.0),
    ("vs code", "vscode", "technical", 1.0),
    ("knowledge base", "知识库", "translation", 0.9),
    ("transcript", "会话记录", "translation", 0.9),
    ("conversation", "会话", "translation", 0.8),
    ("tool call", "工具调用", "translation", 0.9),
)

CONSERVATIVE_RELATION_METHOD = "deterministic-event-transition-v1"


def _json_list(values: Iterable[str]) -> str:
    return canonical_json(sorted(set(value for value in values if value)))


def _parsed_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _source_uri(source_id: str, line_no: int | None = None) -> str:
    suffix = f"#line={line_no}" if line_no is not None else ""
    return f"codex-history-source://{source_id}{suffix}"


def _status_for_event(event: ParsedEvent) -> tuple[str, str, str]:
    if event.role == "goal":
        lowered = event.text.casefold()
        if "[complete]" in lowered or "[completed]" in lowered:
            return "goal_complete", "executed", "direct_goal_state"
        if "[failed]" in lowered:
            return "failed", "failed", "direct_goal_state"
        if "[blocked]" in lowered:
            return "blocked", "blocked", "direct_goal_state"
        return "active", "planned", "direct_goal_state"
    if event.role == "tool_call":
        return "executed", "executed", "direct_tool_call"
    if event.role == "tool_output":
        if FAILURE_RE.search(event.text):
            return "failed", "failed", "direct_tool_output"
        if VERIFIED_RE.search(event.text):
            return "verified", "verified", "direct_tool_output"
        return "executed", "executed", "direct_tool_output"
    if event.role == "assistant":
        return "reported_outcome", "mixed", "assistant_report"
    return "stated_intent", "planned", "user_statement"


def _event_category(event: ParsedEvent) -> str:
    return {
        "user": "user_intent",
        "assistant": "assistant_response",
        "tool_call": "tool_call",
        "tool_output": "tool_output",
        "goal": "goal_state",
    }.get(event.role, "event")


def _asset_types(event: ParsedEvent) -> list[str]:
    # Tool and assistant events remain first-class Core/Evidence. Promoting their
    # full payloads with keyword regexes creates recursive retrieval echoes and
    # lets large command output overwhelm curated/model-authored assets.
    if event.role not in {"user", "goal"}:
        return []
    text = event.text
    result: list[str] = []
    if event.role == "user" and PREFERENCE_RE.search(text):
        result.append("preferences")
    if event.role == "user" and DECISION_RE.search(text):
        result.append("decisions")
    if event.role == "user" and FAILURE_RE.search(text):
        result.append("failures")
    if event.role == "user" and UNRESOLVED_RE.search(text):
        result.append("unresolved")
    if event.role == "goal" and "[complete]" not in text.casefold() and "[completed]" not in text.casefold():
        result.append("unresolved")
    return result


def _deterministic_asset_text(event: ParsedEvent) -> str:
    return truncate(normalize_text(event.text), MAX_DETERMINISTIC_ASSET_CHARS)


def _looks_like_history_output(text: str) -> bool:
    if any(marker in text for marker in HISTORY_OUTPUT_MARKERS):
        return True
    return (
        '"record_id"' in text
        and '"scope_id"' in text
        and ('"evidence_refs"' in text or '"tier"' in text)
    )


def _history_support_call(event: ParsedEvent) -> bool:
    return bool(
        event.role == "tool_call"
        and (
            HISTORY_QUERY_CALL_RE.search(event.text)
            or HISTORY_SUPPORT_CALL_RE.search(event.text)
        )
    )


def history_retrieval_turn(events: list[ParsedEvent]) -> tuple[bool, set[str]]:
    history_call_ids = {
        event.call_id
        for event in events
        if event.call_id and _history_support_call(event)
    }
    history_call_ids.update(
        event.call_id
        for event in events
        if event.role == "tool_output"
        and event.call_id
        and _looks_like_history_output(event.text)
    )
    history_event_ids = {
        event.event_id
        for event in events
        if event.role == "tool_call"
        and event.call_id
        and event.call_id in history_call_ids
    }
    history_event_ids.update(
        {
            event.event_id
            for event in events
            if event.role == "tool_output"
            and (
                (event.call_id and event.call_id in history_call_ids)
                or _looks_like_history_output(event.text)
            )
        }
    )
    if not history_event_ids:
        return False, set()
    non_history_calls = [
        event
        for event in events
        if event.role == "tool_call"
        and not (event.call_id and event.call_id in history_call_ids)
    ]
    return not non_history_calls, history_event_ids


def history_user_assertion_eligible(user_text: str, pure_retrieval: bool) -> bool:
    return pure_retrieval and len(_user_fact_text(user_text)) >= MIN_HISTORY_USER_ASSERTION_CHARS


def _user_fact_text(text: str) -> str:
    return normalize_text(ENVIRONMENT_CONTEXT_RE.sub("", text))


def insert_source_snapshot(connection: sqlite3.Connection, snapshot: SnapshotFile) -> None:
    source = snapshot.source
    now = utc_now()
    connection.execute(
        """
        INSERT INTO source_files(
            source_id,adapter,source_root,source_path,relative_path,thread_id,size_bytes,
            mtime_ns,content_sha256,prefix_sha256,snapshot_format,snapshot_size_bytes,
            snapshot_content_sha256,line_count,snapshot_manifest_path,source_state,
            first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
            adapter=excluded.adapter,source_root=excluded.source_root,
            source_path=excluded.source_path,relative_path=excluded.relative_path,
            thread_id=excluded.thread_id,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
            content_sha256=excluded.content_sha256,prefix_sha256=excluded.prefix_sha256,
            snapshot_format=excluded.snapshot_format,
            snapshot_size_bytes=excluded.snapshot_size_bytes,
            snapshot_content_sha256=excluded.snapshot_content_sha256,
            line_count=excluded.line_count,snapshot_manifest_path=excluded.snapshot_manifest_path,
            source_state=excluded.source_state,last_seen_at=excluded.last_seen_at
        """,
        (
            source.source_id,
            source.adapter,
            str(source.root),
            str(source.path),
            source.relative_path,
            source.thread_id,
            source.size_bytes,
            source.mtime_ns,
            snapshot.content_sha256,
            snapshot.prefix_sha256,
            "normalized-jsonl-v1",
            snapshot.snapshot_size_bytes,
            snapshot.snapshot_content_sha256,
            snapshot.line_count,
            str(snapshot.manifest_path),
            "active",
            now,
            now,
        ),
    )
    connection.execute("DELETE FROM source_chunks WHERE source_id=?", (source.source_id,))
    connection.executemany(
        "INSERT INTO source_chunks(source_id,chunk_index,chunk_sha256,size_bytes,cas_relative_path) VALUES(?,?,?,?,?)",
        (
            (
                source.source_id,
                int(chunk["index"]),
                str(chunk["sha256"]),
                int(chunk["size_bytes"]),
                str(chunk["cas_relative_path"]),
            )
            for chunk in snapshot.chunks
        ),
    )


def _archive_knowledge_versions(connection: sqlite3.Connection, scope_id: str, build_id: str) -> None:
    now = utc_now()
    for row in connection.execute("SELECT * FROM knowledge WHERE scope_id=?", (scope_id,)).fetchall():
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM knowledge_versions WHERE record_id=?",
            (row["record_id"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_versions(version_id,record_id,version_no,valid_from,valid_to,build_id,row_json) VALUES(?,?,?,?,?,?,?)",
            (
                stable_id("version", row["record_id"], version_no, build_id),
                row["record_id"],
                version_no,
                row["valid_from"] or row["asserted_at"] or now,
                now,
                build_id,
                canonical_json(dict(row)),
            ),
        )


def _archive_record_version(
    connection: sqlite3.Connection, record_id: str, build_id: str
) -> None:
    row = connection.execute(
        "SELECT * FROM knowledge WHERE record_id=?", (record_id,)
    ).fetchone()
    if row is None:
        return
    now = utc_now()
    version_no = connection.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 FROM knowledge_versions WHERE record_id=?",
        (record_id,),
    ).fetchone()[0]
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_versions(version_id,record_id,version_no,valid_from,valid_to,build_id,row_json) VALUES(?,?,?,?,?,?,?)",
        (
            stable_id("version", record_id, version_no, build_id),
            record_id,
            version_no,
            row["valid_from"] or row["asserted_at"] or now,
            now,
            build_id,
            canonical_json(dict(row)),
        ),
    )


def delete_thread(connection: sqlite3.Connection, thread_id: str, build_id: str) -> None:
    source_row = connection.execute(
        "SELECT source_id FROM threads WHERE thread_id=?", (thread_id,)
    ).fetchone()
    source_id = str(source_row["source_id"]) if source_row and source_row["source_id"] else ""
    family_scopes = [
        row[0]
        for row in connection.execute(
            "SELECT scope_id FROM scope_threads WHERE thread_id=? AND scope_id<>?",
            (thread_id, thread_id),
        )
    ]
    for scope_id in [thread_id, *family_scopes]:
        _archive_knowledge_versions(connection, scope_id, build_id)
        connection.execute("DELETE FROM scopes WHERE scope_id=?", (scope_id,))
    connection.execute("DELETE FROM threads WHERE thread_id=?", (thread_id,))
    if source_id:
        connection.execute(
            "DELETE FROM artifact_paths WHERE path LIKE ?", (f"inline-image:{source_id}:%",)
        )
        connection.execute(
            "DELETE FROM artifact_paths WHERE path LIKE ?", (f"transcript-ref:{source_id}:%",)
        )
        connection.execute(
            "DELETE FROM artifact_files WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM artifact_paths)"
        )
    connection.execute(
        "DELETE FROM evidence WHERE evidence_id NOT IN (SELECT DISTINCT evidence_id FROM evidence_occurrences)"
    )


def _insert_thread_structure(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
) -> None:
    stats = parsed.stats
    connection.execute(
        """
        INSERT INTO threads(
            thread_id,group_name,title,transcript_relative_path,source_relative_path,
            source_size_bytes,line_count,first_activity_at,last_activity_at,event_count,
            turn_count,message_count,user_message_count,assistant_message_count,
            tool_call_count,tool_output_count,goal_event_count,compacted_count,indexed_at,
            source_kind,parent_thread_id,source_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(thread_id) DO UPDATE SET
            title=excluded.title,transcript_relative_path=excluded.transcript_relative_path,
            source_relative_path=excluded.source_relative_path,
            source_size_bytes=excluded.source_size_bytes,line_count=excluded.line_count,
            first_activity_at=excluded.first_activity_at,last_activity_at=excluded.last_activity_at,
            event_count=excluded.event_count,turn_count=excluded.turn_count,
            message_count=excluded.message_count,user_message_count=excluded.user_message_count,
            assistant_message_count=excluded.assistant_message_count,
            tool_call_count=excluded.tool_call_count,tool_output_count=excluded.tool_output_count,
            goal_event_count=excluded.goal_event_count,compacted_count=excluded.compacted_count,
            indexed_at=excluded.indexed_at,source_kind=excluded.source_kind,
            parent_thread_id=excluded.parent_thread_id,source_id=excluded.source_id
        """,
        (
            parsed.thread_id,
            "",
            parsed.title,
            snapshot.source.relative_path,
            snapshot.source.relative_path,
            snapshot.source.size_bytes,
            snapshot.line_count,
            parsed.first_activity_at,
            parsed.last_activity_at,
            stats["event_count"],
            stats["turn_count"],
            stats["message_count"],
            stats["user_message_count"],
            stats["assistant_message_count"],
            stats["tool_call_count"],
            stats["tool_output_count"],
            stats["goal_event_count"],
            stats["compacted_count"],
            utc_now(),
            "codex_jsonl",
            parsed.parent_thread_id,
            snapshot.source.source_id,
        ),
    )
    connection.executemany(
        """
        INSERT INTO turns(
            turn_id,thread_id,turn_seq,source_turn_id,started_at,completed_at,status,
            user_text,assistant_text,tool_call_count,tool_output_count,event_count,
            content_sha256,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            (
                turn.turn_id,
                parsed.thread_id,
                turn.turn_seq,
                turn.source_turn_id,
                turn.started_at,
                turn.completed_at,
                turn.status,
                turn.user_text,
                turn.assistant_text,
                turn.tool_call_count,
                turn.tool_output_count,
                turn.event_count,
                turn.content_sha256,
                "{}",
            )
            for turn in parsed.turns
        ),
    )
    valid_turn_ids = {turn.turn_id for turn in parsed.turns}
    connection.executemany(
        """
        INSERT INTO canonical_events(
            event_id,content_sha256,source_id,thread_id,turn_id,line_no,byte_start,
            byte_end,timestamp,event_type,payload_type,role,text,tool_name,call_id,
            raw_json,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            (
                event.event_id,
                event.content_sha256,
                snapshot.source.source_id,
                parsed.thread_id,
                event.turn_id if event.turn_id in valid_turn_ids else None,
                event.line_no,
                event.byte_start,
                event.byte_end,
                event.timestamp,
                event.event_type,
                event.payload_type,
                event.role,
                truncate(event.text, 16000),
                event.tool_name,
                event.call_id,
                "",
                canonical_json(event.metadata),
            )
            for event in parsed.events
        ),
    )


def hydrate_parsed_thread(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
    config: ProfileConfig,
    *,
    index_new_knowledge: bool = False,
    build_id: str = "",
) -> dict[str, int]:
    existing = connection.execute(
        "SELECT 1 FROM threads WHERE thread_id=?", (parsed.thread_id,)
    ).fetchone()
    if not existing:
        inserted = insert_parsed_thread(
            connection,
            snapshot,
            parsed,
            config,
            ingest_build_id=build_id if index_new_knowledge else "",
            incremental_append=index_new_knowledge,
        )
        inserted["preserved_curated_scope"] = 0
        return inserted
    previous_events = {
        str(row[0])
        for row in connection.execute(
            "SELECT event_id FROM canonical_events WHERE thread_id=?", (parsed.thread_id,)
        )
    }
    previous_turns = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT turn_id,content_sha256 FROM turns WHERE thread_id=?", (parsed.thread_id,)
        )
    }
    connection.execute("DELETE FROM canonical_events WHERE thread_id=?", (parsed.thread_id,))
    connection.execute("DELETE FROM turns WHERE thread_id=?", (parsed.thread_id,))
    _insert_thread_structure(connection, snapshot, parsed)
    _insert_artifacts(connection, snapshot, parsed, config)
    if index_new_knowledge and not connection.execute(
        "SELECT 1 FROM scopes WHERE scope_id=?", (parsed.thread_id,)
    ).fetchone():
        _insert_scope(connection, parsed, _thread_overview(parsed))
    indexed = (
        _insert_incremental_knowledge(
            connection,
            snapshot,
            parsed,
            event_ids={event.event_id for event in parsed.events} - previous_events,
            turn_ids={
                turn.turn_id
                for turn in parsed.turns
                if previous_turns.get(turn.turn_id) != turn.content_sha256
            },
            build_id=build_id,
        )
        if index_new_knowledge
        else {"evidence": 0, "fact_blocks": 0}
    )
    return {
        "events": len(parsed.events),
        "turns": len(parsed.turns),
        "evidence": indexed["evidence"],
        "fact_blocks": indexed["fact_blocks"],
        "parse_errors": len(parsed.parse_errors),
        "artifacts": len(parsed.image_artifacts),
        "preserved_curated_scope": 1,
    }


def append_parsed_thread(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
    config: ProfileConfig,
    *,
    build_id: str,
) -> dict[str, int]:
    """Append a parsed, turn-boundary-safe suffix without rewriting prior rows."""
    existing = connection.execute(
        "SELECT * FROM threads WHERE thread_id=?", (parsed.thread_id,)
    ).fetchone()
    if existing is None:
        raise ValueError("Append fast path requires an existing thread")
    if not parsed.events:
        raise ValueError("Append fast path produced no events")
    last_line = int(
        connection.execute(
            "SELECT COALESCE(MAX(line_no),0) FROM canonical_events WHERE thread_id=?",
            (parsed.thread_id,),
        ).fetchone()[0]
    )
    if min(event.line_no for event in parsed.events) <= last_line:
        raise ValueError("Append suffix overlaps existing canonical event lines")

    connection.executemany(
        """
        INSERT INTO turns(
            turn_id,thread_id,turn_seq,source_turn_id,started_at,completed_at,status,
            user_text,assistant_text,tool_call_count,tool_output_count,event_count,
            content_sha256,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            (
                turn.turn_id,
                parsed.thread_id,
                turn.turn_seq,
                turn.source_turn_id,
                turn.started_at,
                turn.completed_at,
                turn.status,
                turn.user_text,
                turn.assistant_text,
                turn.tool_call_count,
                turn.tool_output_count,
                turn.event_count,
                turn.content_sha256,
                "{}",
            )
            for turn in parsed.turns
        ),
    )
    valid_turn_ids = {turn.turn_id for turn in parsed.turns}
    connection.executemany(
        """
        INSERT INTO canonical_events(
            event_id,content_sha256,source_id,thread_id,turn_id,line_no,byte_start,
            byte_end,timestamp,event_type,payload_type,role,text,tool_name,call_id,
            raw_json,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            (
                event.event_id,
                event.content_sha256,
                snapshot.source.source_id,
                parsed.thread_id,
                event.turn_id if event.turn_id in valid_turn_ids else None,
                event.line_no,
                event.byte_start,
                event.byte_end,
                event.timestamp,
                event.event_type,
                event.payload_type,
                event.role,
                truncate(event.text, 16000),
                event.tool_name,
                event.call_id,
                "",
                canonical_json(event.metadata),
            )
            for event in parsed.events
        ),
    )
    stats = parsed.stats
    first_values = [value for value in (existing["first_activity_at"], parsed.first_activity_at) if value]
    last_values = [value for value in (existing["last_activity_at"], parsed.last_activity_at) if value]
    connection.execute(
        """
        UPDATE threads SET title=?,source_size_bytes=?,line_count=?,
            first_activity_at=?,last_activity_at=?,event_count=event_count+?,
            turn_count=turn_count+?,message_count=message_count+?,
            user_message_count=user_message_count+?,
            assistant_message_count=assistant_message_count+?,
            tool_call_count=tool_call_count+?,tool_output_count=tool_output_count+?,
            goal_event_count=goal_event_count+?,compacted_count=compacted_count+?,
            indexed_at=?,parent_thread_id=COALESCE(?,parent_thread_id),source_id=?
        WHERE thread_id=?
        """,
        (
            parsed.title,
            snapshot.source.size_bytes,
            snapshot.line_count,
            min(first_values) if first_values else None,
            max(last_values) if last_values else None,
            stats["event_count"],
            stats["turn_count"],
            stats["message_count"],
            stats["user_message_count"],
            stats["assistant_message_count"],
            stats["tool_call_count"],
            stats["tool_output_count"],
            stats["goal_event_count"],
            stats["compacted_count"],
            utc_now(),
            parsed.parent_thread_id,
            snapshot.source.source_id,
            parsed.thread_id,
        ),
    )
    _insert_artifacts(connection, snapshot, parsed, config)
    if not connection.execute(
        "SELECT 1 FROM scopes WHERE scope_id=?", (parsed.thread_id,)
    ).fetchone():
        _insert_scope(connection, parsed, _thread_overview(parsed))
    else:
        connection.execute(
            "UPDATE scopes SET scope_title=?,last_activity_at=MAX(COALESCE(last_activity_at,''),COALESCE(?,'')),indexed_at=? WHERE scope_id=?",
            (parsed.title, parsed.last_activity_at, utc_now(), parsed.thread_id),
        )
    indexed = _insert_incremental_knowledge(
        connection,
        snapshot,
        parsed,
        event_ids={event.event_id for event in parsed.events},
        turn_ids={turn.turn_id for turn in parsed.turns},
        build_id=build_id,
    )
    return {
        "events": len(parsed.events),
        "turns": len(parsed.turns),
        "evidence": indexed["evidence"],
        "fact_blocks": indexed["fact_blocks"],
        "parse_errors": len(parsed.parse_errors),
        "artifacts": len(parsed.image_artifacts),
        "preserved_curated_scope": 1,
        "append_fast_path": 1,
    }


def _insert_scope(connection: sqlite3.Connection, parsed: ParsedThread, overview: str) -> None:
    connection.execute(
        """
        INSERT INTO scopes(
            scope_id,scope_type,scope_title,thread_ids_json,thread_titles_json,overview,
            human_verdict,evidence_rows,overview_path,ledger_path,first_activity_at,
            last_activity_at,indexed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            parsed.thread_id,
            "thread",
            parsed.title,
            canonical_json([parsed.thread_id]),
            canonical_json([parsed.title]),
            overview,
            "deterministic_extract",
            0,
            _source_uri(parsed.thread_id),
            _source_uri(parsed.thread_id),
            parsed.first_activity_at,
            parsed.last_activity_at,
            utc_now(),
        ),
    )
    connection.execute(
        "INSERT INTO scope_threads(scope_id,thread_id,ordinal) VALUES(?,?,0)",
        (parsed.thread_id, parsed.thread_id),
    )


def _upsert_evidence(
    connection: sqlite3.Connection,
    parsed: ParsedThread,
    event: ParsedEvent,
) -> tuple[str, str]:
    assignment = event.text or f"{event.event_type}:{event.payload_type}"
    semantic_sha = hashlib.sha256(
        canonical_json(
            {
                "assignment": normalize_text(assignment),
                "role": event.role,
                "payload_type": event.payload_type,
                "tool_name": event.tool_name,
            }
        ).encode("utf-8")
    ).hexdigest()
    evidence_id = stable_id("evidence", semantic_sha)
    turn_seq = event.turn_seq if event.turn_seq is not None else -1
    occurrence_id = stable_id("occurrence", parsed.thread_id, event.event_id)
    existing = connection.execute(
        "SELECT scope_ids_json,thread_ids_json,first_occurred_at,last_occurred_at FROM evidence WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    scope_ids = {parsed.thread_id}
    thread_ids = {parsed.thread_id}
    first_at = event.timestamp
    last_at = event.timestamp
    if existing:
        scope_ids.update(json.loads(existing["scope_ids_json"]))
        thread_ids.update(json.loads(existing["thread_ids_json"]))
        values = [value for value in (existing["first_occurred_at"], event.timestamp) if value]
        first_at = min(values) if values else None
        values = [value for value in (existing["last_occurred_at"], event.timestamp) if value]
        last_at = max(values) if values else None
    connection.execute(
        """
        INSERT INTO evidence(
            evidence_id,assignment,evidence_chars,source_task_id,scope_ids_json,
            thread_ids_json,applies_to_json,item_id,sha256,occurrence_count,
            first_occurred_at,last_occurred_at,temporal_basis
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(evidence_id) DO UPDATE SET
            scope_ids_json=excluded.scope_ids_json,thread_ids_json=excluded.thread_ids_json,
            first_occurred_at=excluded.first_occurred_at,last_occurred_at=excluded.last_occurred_at
        """,
        (
            evidence_id,
            assignment,
            len(assignment),
            event.turn_id or parsed.thread_id,
            _json_list(scope_ids),
            _json_list(thread_ids),
            canonical_json([_event_category(event)]),
            event.event_id,
            semantic_sha,
            0,
            first_at,
            last_at,
            "event_timestamp" if event.timestamp else "source_order",
        ),
    )
    connection.execute(
        """
        INSERT INTO evidence_occurrences(
            occurrence_id,evidence_id,thread_id,turn_seq,position,tier,canonical_turn_id,
            start_line,end_line,occurred_start_at,occurred_end_at,temporal_basis,
            temporal_confidence,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            occurrence_id,
            evidence_id,
            parsed.thread_id,
            turn_seq,
            event.line_no,
            "event",
            event.turn_id or parsed.thread_id,
            event.line_no,
            event.line_no,
            event.timestamp,
            event.timestamp,
            "event_timestamp" if event.timestamp else "source_order",
            "exact_event" if event.timestamp else "ordered_only",
            canonical_json({"event_id": event.event_id, "byte_start": event.byte_start, "byte_end": event.byte_end}),
        ),
    )
    return evidence_id, occurrence_id


def _insert_record(
    connection: sqlite3.Connection,
    *,
    record_id: str,
    tier: str,
    asset_type: str,
    scope_id: str,
    scope_type: str,
    scope_title: str,
    category: str,
    text: str,
    status: str,
    status_group: str,
    evidence_refs: list[str],
    source_path: str,
    source_locator: str,
    confidence: str,
    occurred_start_at: str | None,
    occurred_end_at: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO knowledge(
            record_id,tier,asset_type,scope_id,scope_type,scope_title,category,theme,
            phase,text,status,status_group,evidence_count,evidence_refs_json,source_path,
            source_locator,confidence,metadata_json,occurred_start_at,occurred_end_at,
            observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,
            temporal_basis,temporal_confidence
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET
            text=excluded.text,status=excluded.status,status_group=excluded.status_group,
            evidence_count=excluded.evidence_count,evidence_refs_json=excluded.evidence_refs_json,
            metadata_json=excluded.metadata_json,occurred_start_at=excluded.occurred_start_at,
            occurred_end_at=excluded.occurred_end_at,observed_at=excluded.observed_at,
            asserted_at=excluded.asserted_at,indexed_at=excluded.indexed_at
        """,
        (
            record_id,
            tier,
            asset_type,
            scope_id,
            scope_type,
            scope_title,
            category,
            "",
            "",
            text,
            status,
            status_group,
            len(evidence_refs),
            canonical_json(evidence_refs),
            source_path,
            source_locator,
            confidence,
            canonical_json(metadata or {}),
            occurred_start_at,
            occurred_end_at,
            occurred_end_at or occurred_start_at,
            now,
            occurred_end_at if status == "verified" else None,
            now,
            occurred_start_at,
            None,
            "event_timestamp" if occurred_start_at else "source_order",
            "exact_event" if occurred_start_at == occurred_end_at and occurred_start_at else "exact_turn_range",
        ),
    )
    for evidence_id in evidence_refs:
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_evidence(record_id,evidence_id) VALUES(?,?)",
            (record_id, evidence_id),
        )
        for occurrence in connection.execute(
            "SELECT occurrence_id,thread_id FROM evidence_occurrences WHERE evidence_id=?",
            (evidence_id,),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO record_evidence_occurrences(record_id,occurrence_id,scope_match) VALUES(?,?,?)",
                (record_id, occurrence["occurrence_id"], int(occurrence["thread_id"] == scope_id)),
            )
    semantic_text = _semantic_projection(text)
    content_sha = hashlib.sha256(normalize_text(semantic_text).encode("utf-8")).hexdigest()
    document_id = stable_id("semantic", content_sha)
    connection.execute(
        "INSERT OR IGNORE INTO semantic_documents(document_id,content_sha256,document_text,record_count,created_at) VALUES(?,?,?,?,?)",
        (document_id, content_sha, semantic_text, 0, now),
    )
    document = connection.execute(
        "SELECT document_id FROM semantic_documents WHERE content_sha256=?",
        (content_sha,),
    ).fetchone()
    if document is None:
        raise RuntimeError("Semantic document insert did not produce a resolvable row")
    document_id = str(document["document_id"])
    connection.execute(
        "INSERT OR REPLACE INTO semantic_document_records(document_id,record_id) VALUES(?,?)",
        (document_id, record_id),
    )


def _semantic_projection(text: str, max_chars: int = 3_900) -> str:
    if len(text) <= max_chars:
        return text
    first_marker = "\n[semantic projection: middle sample]\n"
    second_marker = "\n[semantic projection: tail sample]\n"
    content_budget = max_chars - len(first_marker) - len(second_marker)
    head_size = int(content_budget * 0.40)
    middle_size = int(content_budget * 0.25)
    tail_size = content_budget - head_size - middle_size
    midpoint = len(text) // 2
    middle_start = max(0, midpoint - middle_size // 2)
    return (
        text[:head_size]
        + first_marker
        + text[middle_start : middle_start + middle_size]
        + second_marker
        + text[-tail_size:]
    )


def _turn_text(turn: ParsedTurn, events: list[ParsedEvent]) -> str:
    pure_retrieval, history_event_ids = history_retrieval_turn(events)
    semantic_events = [event for event in events if event.event_id not in history_event_ids]
    tools = [
        event.tool_name
        for event in semantic_events
        if event.role == "tool_call" and event.tool_name
    ]
    parts = []
    user_text = _user_fact_text(turn.user_text)
    if user_text:
        parts.append(f"Intent: {truncate(user_text, 3000)}")
    if tools:
        parts.append(f"Tools: {', '.join(tools[:30])}")
    outputs = [
        event.text
        for event in semantic_events
        if event.role == "tool_output" and event.text
    ]
    if outputs:
        parts.append(f"Evidence: {truncate(' | '.join(outputs), 5000)}")
    # A pure History lookup is derived from this same knowledge base. Preserve
    # the user's first-party statement, but never feed the assistant's retrieved
    # synthesis back into the promotion pipeline.
    if turn.assistant_text and not pure_retrieval:
        parts.append(f"Outcome: {truncate(turn.assistant_text, 4000)}")
    return "\n".join(parts) or f"Turn {turn.turn_seq} contains {turn.event_count} events."


def _thread_overview(parsed: ParsedThread) -> str:
    first_intent = next((turn.user_text for turn in parsed.turns if turn.user_text), "")
    latest_outcome = next(
        (turn.assistant_text for turn in reversed(parsed.turns) if turn.assistant_text), ""
    )
    stats = parsed.stats
    parts = [
        f"{parsed.title}. Activity: {parsed.first_activity_at or 'unknown'} to {parsed.last_activity_at or 'unknown'}.",
        (
            f"Contains {stats['turn_count']} turns, {stats['user_message_count']} user messages, "
            f"{stats['assistant_message_count']} assistant messages, {stats['tool_call_count']} tool calls, "
            f"and {stats['tool_output_count']} tool outputs."
        ),
    ]
    if first_intent:
        parts.append(f"Initial intent: {truncate(first_intent, 1600)}")
    if latest_outcome:
        parts.append(f"Latest recorded outcome: {truncate(latest_outcome, 2400)}")
    if parsed.parse_errors:
        parts.append(f"Parser warning: {len(parsed.parse_errors)} malformed JSONL lines were excluded.")
    return "\n\n".join(parts)


def _insert_artifact_file(
    connection: sqlite3.Connection,
    *,
    digest: str,
    size: int,
    relative_path: str,
    uri: str,
    mime_type: str,
    extension: str,
    indexed_path: str,
    source_open_path: str,
    keep_reason: str,
    category: str,
) -> None:
    connection.execute(
        """
        INSERT INTO artifact_files(
            sha256,size_bytes,size_human,cas_relative_path,artifact_uri,mime_type,
            extension,source_open_path,tiers,keep_reasons,categories,path_count,
            transcript_occurrences_mapped
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sha256) DO NOTHING
        """,
        (
            digest,
            size,
            f"{size / 1024:.1f} KiB",
            relative_path,
            uri,
            mime_type,
            extension,
            source_open_path,
            "raw_evidence",
            keep_reason,
            category,
            1,
            1,
        ),
    )
    path_key = stable_id("artifact-path", indexed_path)
    connection.execute(
        """
        INSERT OR IGNORE INTO artifact_paths(
            path_key,path,sha256,artifact_uri,cas_relative_path,size_bytes,tier,
            keep_reason,category,source_open_path
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            path_key,
            indexed_path,
            digest,
            uri,
            relative_path,
            size,
            "raw_evidence",
            keep_reason,
            category,
            source_open_path,
        ),
    )


def _insert_artifacts(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
    config: ProfileConfig,
) -> None:
    for artifact in parsed.image_artifacts:
        size = int(artifact["size_bytes"])
        digest = str(artifact["sha256"])
        uri = str(artifact["artifact_uri"])
        source_path = f"inline-image:{snapshot.source.source_id}:{digest}"
        _insert_artifact_file(
            connection,
            digest=digest,
            size=size,
            relative_path=artifact["cas_relative_path"],
            uri=uri,
            mime_type=artifact["mime_type"],
            extension=artifact["extension"],
            indexed_path=source_path,
            source_open_path=source_path,
            keep_reason="inline_transcript_image",
            category="image",
        )


def _insert_incremental_knowledge(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
    *,
    event_ids: set[str],
    turn_ids: set[str],
    build_id: str,
) -> dict[str, int]:
    evidence_by_event: dict[str, tuple[str, str]] = {}
    for event in parsed.events:
        if (
            event.event_id in event_ids
            and event.role in {"user", "assistant", "tool_call", "tool_output", "goal"}
            and event.text
        ):
            evidence_by_event[event.event_id] = _upsert_evidence(
                connection, parsed, event
            )
    for event in parsed.events:
        linked = evidence_by_event.get(event.event_id)
        if not linked:
            continue
        evidence_id, _occurrence_id = linked
        status, status_group, confidence = _status_for_event(event)
        _insert_record(
            connection,
            record_id=stable_id("record", "event", parsed.thread_id, event.event_id),
            tier="core",
            asset_type="",
            scope_id=parsed.thread_id,
            scope_type="thread",
            scope_title=parsed.title,
            category=_event_category(event),
            text=event.text,
            status=status,
            status_group=status_group,
            evidence_refs=[evidence_id],
            source_path=_source_uri(snapshot.source.source_id, event.line_no),
            source_locator=f"line[{event.line_no}]",
            confidence=confidence,
            occurred_start_at=event.timestamp,
            occurred_end_at=event.timestamp,
            metadata={
                "event_id": event.event_id,
                "line_no": event.line_no,
                "payload_type": event.payload_type,
                "tool_name": event.tool_name,
                "call_id": event.call_id,
                "incremental_append": True,
                "ingest_build_id": build_id,
            },
        )
        for asset_type in _asset_types(event):
            _insert_record(
                connection,
                record_id=stable_id(
                    "record", "asset", asset_type, parsed.thread_id, event.event_id
                ),
                tier="asset",
                asset_type=asset_type,
                scope_id=parsed.thread_id,
                scope_type="thread",
                scope_title=parsed.title,
                category=asset_type,
                text=_deterministic_asset_text(event),
                status=status,
                status_group=status_group,
                evidence_refs=[evidence_id],
                source_path=_source_uri(snapshot.source.source_id, event.line_no),
                source_locator=f"line[{event.line_no}]",
                confidence=confidence,
                occurred_start_at=event.timestamp,
                occurred_end_at=event.timestamp,
                metadata={
                    "derived_by": "deterministic-user-signal-v2",
                    "event_id": event.event_id,
                    "retrieval_eligible": True,
                    "incremental_append": True,
                    "ingest_build_id": build_id,
                },
            )

    events_by_turn: dict[str, list[ParsedEvent]] = defaultdict(list)
    for event in parsed.events:
        if event.turn_id:
            events_by_turn[event.turn_id].append(event)
    fact_blocks = 0
    for turn in parsed.turns:
        if turn.turn_id not in turn_ids:
            continue
        events = events_by_turn.get(turn.turn_id, [])
        retrieval_echo, history_event_ids = history_retrieval_turn(events)
        user_assertion_only = history_user_assertion_eligible(
            turn.user_text, retrieval_echo
        )
        evidence_refs = list(
            dict.fromkeys(
                evidence_by_event[event.event_id][0]
                for event in events
                if event.event_id in evidence_by_event
            )
        )
        for event in events:
            if event.event_id in evidence_by_event:
                continue
            existing_record = connection.execute(
                "SELECT evidence_refs_json FROM knowledge WHERE record_id=?",
                (stable_id("record", "event", parsed.thread_id, event.event_id),),
            ).fetchone()
            if existing_record:
                evidence_refs.extend(json.loads(existing_record[0]))
        evidence_refs = list(dict.fromkeys(evidence_refs))
        _insert_record(
            connection,
            record_id=stable_id("record", "turn", parsed.thread_id, turn.turn_id),
            tier="fact_block",
            asset_type="",
            scope_id=parsed.thread_id,
            scope_type="thread",
            scope_title=parsed.title,
            category="user_context" if user_assertion_only else "turn_summary",
            text=_turn_text(turn, events),
            status=(
                "stated_intent"
                if user_assertion_only
                else ("executed" if turn.status == "complete" else turn.status)
            ),
            status_group=(
                "planned"
                if user_assertion_only
                else ("executed" if turn.status == "complete" else "uncertain")
            ),
            evidence_refs=evidence_refs,
            source_path=_source_uri(snapshot.source.source_id),
            source_locator=f"turn[{turn.turn_seq}]",
            confidence="deterministic_extract",
            occurred_start_at=turn.started_at,
            occurred_end_at=turn.completed_at,
            metadata={
                "turn_id": turn.turn_id,
                "turn_seq": turn.turn_seq,
                "source_status": turn.status,
                "retrieval_echo": retrieval_echo,
                "retrieval_echo_event_ids": sorted(history_event_ids),
                "history_user_assertion_only": user_assertion_only,
                "promotion_eligible": not retrieval_echo or user_assertion_only,
                "incremental_append": True,
                "ingest_build_id": build_id,
            },
        )
        fact_blocks += 1
    connection.execute(
        "UPDATE scopes SET evidence_rows=evidence_rows+? WHERE scope_id=?",
        (len(evidence_by_event), parsed.thread_id),
    )
    return {"evidence": len(evidence_by_event), "fact_blocks": fact_blocks}


def insert_parsed_thread(
    connection: sqlite3.Connection,
    snapshot: SnapshotFile,
    parsed: ParsedThread,
    config: ProfileConfig,
    *,
    ingest_build_id: str = "",
    incremental_append: bool = False,
) -> dict[str, int]:
    _insert_thread_structure(connection, snapshot, parsed)
    ingest_metadata = (
        {"incremental_append": True, "ingest_build_id": ingest_build_id}
        if incremental_append
        else {}
    )
    overview = _thread_overview(parsed)
    _insert_scope(connection, parsed, overview)
    evidence_by_event: dict[str, tuple[str, str]] = {}
    for event in parsed.events:
        if event.role in {"user", "assistant", "tool_call", "tool_output", "goal"} and event.text:
            evidence_by_event[event.event_id] = _upsert_evidence(connection, parsed, event)
    scope_evidence = 0
    for event in parsed.events:
        linked = evidence_by_event.get(event.event_id)
        if not linked:
            continue
        evidence_id, _occurrence_id = linked
        status, status_group, confidence = _status_for_event(event)
        record_id = stable_id("record", "event", parsed.thread_id, event.event_id)
        _insert_record(
            connection,
            record_id=record_id,
            tier="core",
            asset_type="",
            scope_id=parsed.thread_id,
            scope_type="thread",
            scope_title=parsed.title,
            category=_event_category(event),
            text=event.text,
            status=status,
            status_group=status_group,
            evidence_refs=[evidence_id],
            source_path=_source_uri(snapshot.source.source_id, event.line_no),
            source_locator=f"line[{event.line_no}]",
            confidence=confidence,
            occurred_start_at=event.timestamp,
            occurred_end_at=event.timestamp,
            metadata={
                "event_id": event.event_id,
                "line_no": event.line_no,
                "payload_type": event.payload_type,
                "tool_name": event.tool_name,
                "call_id": event.call_id,
                **ingest_metadata,
            },
        )
        scope_evidence += 1
        for asset_type in _asset_types(event):
            _insert_record(
                connection,
                record_id=stable_id("record", "asset", asset_type, parsed.thread_id, event.event_id),
                tier="asset",
                asset_type=asset_type,
                scope_id=parsed.thread_id,
                scope_type="thread",
                scope_title=parsed.title,
                category=asset_type,
                text=_deterministic_asset_text(event),
                status=status,
                status_group=status_group,
                evidence_refs=[evidence_id],
                source_path=_source_uri(snapshot.source.source_id, event.line_no),
                source_locator=f"line[{event.line_no}]",
                confidence=confidence,
                occurred_start_at=event.timestamp,
                occurred_end_at=event.timestamp,
                metadata={
                    "derived_by": "deterministic-user-signal-v2",
                    "event_id": event.event_id,
                    "retrieval_eligible": True,
                    **ingest_metadata,
                },
            )

    events_by_turn: dict[str, list[ParsedEvent]] = defaultdict(list)
    for event in parsed.events:
        if event.turn_id:
            events_by_turn[event.turn_id].append(event)
    fact_records: list[str] = []
    fact_evidence: list[str] = []
    for turn in parsed.turns:
        events = events_by_turn.get(turn.turn_id, [])
        retrieval_echo, history_event_ids = history_retrieval_turn(events)
        user_assertion_only = history_user_assertion_eligible(
            turn.user_text, retrieval_echo
        )
        evidence_refs = [
            evidence_by_event[event.event_id][0]
            for event in events
            if event.event_id in evidence_by_event
        ]
        evidence_refs = list(dict.fromkeys(evidence_refs))
        record_id = stable_id("record", "turn", parsed.thread_id, turn.turn_id)
        _insert_record(
            connection,
            record_id=record_id,
            tier="fact_block",
            asset_type="",
            scope_id=parsed.thread_id,
            scope_type="thread",
            scope_title=parsed.title,
            category="user_context" if user_assertion_only else "turn_summary",
            text=_turn_text(turn, events),
            status=(
                "stated_intent"
                if user_assertion_only
                else ("executed" if turn.status == "complete" else turn.status)
            ),
            status_group=(
                "planned"
                if user_assertion_only
                else ("executed" if turn.status == "complete" else "uncertain")
            ),
            evidence_refs=evidence_refs,
            source_path=_source_uri(snapshot.source.source_id),
            source_locator=f"turn[{turn.turn_seq}]",
            confidence="deterministic_extract",
            occurred_start_at=turn.started_at,
            occurred_end_at=turn.completed_at,
            metadata={
                "turn_id": turn.turn_id,
                "turn_seq": turn.turn_seq,
                "source_status": turn.status,
                "retrieval_echo": retrieval_echo,
                "retrieval_echo_event_ids": sorted(history_event_ids),
                "history_user_assertion_only": user_assertion_only,
                "promotion_eligible": not retrieval_echo or user_assertion_only,
                **ingest_metadata,
            },
        )
        fact_records.append(record_id)
        fact_evidence.extend(evidence_refs)

    overview_record_id = stable_id("record", "overview", parsed.thread_id)
    overview_evidence = list(dict.fromkeys(fact_evidence))[:64]
    _insert_record(
        connection,
        record_id=overview_record_id,
        tier="overview",
        asset_type="",
        scope_id=parsed.thread_id,
        scope_type="thread",
        scope_title=parsed.title,
        category="overview",
        text=overview,
        status="mixed",
        status_group="mixed",
        evidence_refs=overview_evidence,
        source_path=_source_uri(snapshot.source.source_id),
        source_locator="deterministic-thread-overview-v1",
        confidence="deterministic_extract",
        occurred_start_at=parsed.first_activity_at,
        occurred_end_at=parsed.last_activity_at,
        metadata={"claim_support": "extractive", "fact_record_count": len(fact_records)},
    )
    cursor = 0
    sentences = [item.strip() for item in re.split(r"(?<=[.!?。！？])\s+|\n+", overview) if item.strip()]
    for ordinal, sentence in enumerate(sentences):
        start = overview.find(sentence, cursor)
        end = start + len(sentence)
        cursor = end
        claim_id = stable_id("claim", overview_record_id, ordinal, sentence)
        connection.execute(
            "INSERT INTO overview_claims(claim_id,overview_record_id,scope_id,ordinal,start_char,end_char,claim_text,status,confidence,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id,
                overview_record_id,
                parsed.thread_id,
                ordinal,
                start,
                end,
                sentence,
                "linked" if fact_records else "unlinked",
                1.0 if fact_records else None,
                utc_now(),
                canonical_json({"method": "deterministic-overview-v1"}),
            ),
        )
        if fact_records:
            target = fact_records[min(ordinal, len(fact_records) - 1)]
            connection.execute(
                "INSERT INTO overview_claim_records(claim_id,record_id,match_method,score,rank,metadata_json) VALUES(?,?,?,?,?,?)",
                (claim_id, target, "ordered_extract", 1.0, 1, "{}"),
            )
    connection.execute(
        "UPDATE scopes SET evidence_rows=? WHERE scope_id=?", (scope_evidence, parsed.thread_id)
    )
    _insert_artifacts(connection, snapshot, parsed, config)
    return {
        "events": len(parsed.events),
        "turns": len(parsed.turns),
        "evidence": len(evidence_by_event),
        "fact_blocks": len(parsed.turns),
        "parse_errors": len(parsed.parse_errors),
        "artifacts": len(parsed.image_artifacts),
    }


class _DSU:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def refresh_evidence_rollups(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE evidence SET occurrence_count=(SELECT COUNT(*) FROM evidence_occurrences eo WHERE eo.evidence_id=evidence.evidence_id)"
    )
    for row in connection.execute("SELECT evidence_id FROM evidence").fetchall():
        occurrence_rows = connection.execute(
            "SELECT thread_id,canonical_turn_id,position,occurred_start_at,occurred_end_at,metadata_json "
            "FROM evidence_occurrences WHERE evidence_id=? "
            "ORDER BY thread_id,turn_seq,position,occurrence_id",
            (row["evidence_id"],),
        ).fetchall()
        thread_ids = sorted({str(item["thread_id"]) for item in occurrence_rows})
        scope_ids: set[str] = set(thread_ids)
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            scope_ids.update(
                str(item[0])
                for item in connection.execute(
                    f"SELECT DISTINCT scope_id FROM scope_threads WHERE thread_id IN ({placeholders})",
                    thread_ids,
                )
            )
        starts = [item["occurred_start_at"] for item in occurrence_rows if item["occurred_start_at"]]
        ends = [item["occurred_end_at"] for item in occurrence_rows if item["occurred_end_at"]]
        representative = occurrence_rows[0] if occurrence_rows else None
        representative_event = (
            (json.loads(representative["metadata_json"]) or {}).get("event_id")
            if representative
            else None
        )
        connection.execute(
            "UPDATE evidence SET thread_ids_json=?,scope_ids_json=?,occurrence_count=?,"
            "first_occurred_at=?,last_occurred_at=?,item_id=?,source_task_id=? WHERE evidence_id=?",
            (
                canonical_json(thread_ids),
                canonical_json(sorted(scope_ids)),
                len(occurrence_rows),
                min(starts) if starts else None,
                max(ends) if ends else None,
                representative_event,
                representative["canonical_turn_id"] if representative else "",
                row["evidence_id"],
            ),
        )
    connection.execute(
        "UPDATE semantic_documents SET record_count=(SELECT COUNT(*) FROM semantic_document_records sr WHERE sr.document_id=semantic_documents.document_id)"
    )
    connection.execute(
        "DELETE FROM semantic_documents WHERE document_id NOT IN (SELECT DISTINCT document_id FROM semantic_document_records)"
    )
    connection.execute(
        "UPDATE artifact_files SET path_count=(SELECT COUNT(*) FROM artifact_paths ap WHERE ap.sha256=artifact_files.sha256),transcript_occurrences_mapped=(SELECT COUNT(*) FROM artifact_paths ap WHERE ap.sha256=artifact_files.sha256)"
    )
    connection.execute("DELETE FROM record_evidence_occurrences")
    connection.execute(
        """
        INSERT INTO record_evidence_occurrences(record_id,occurrence_id,scope_match)
        SELECT ke.record_id,eo.occurrence_id,
          CASE WHEN k.scope_id=eo.thread_id OR EXISTS(
            SELECT 1 FROM scope_threads st
            WHERE st.scope_id=k.scope_id AND st.thread_id=eo.thread_id
          ) THEN 1 ELSE 0 END
        FROM knowledge_evidence ke
        JOIN knowledge k ON k.record_id=ke.record_id
        JOIN evidence_occurrences eo ON eo.evidence_id=ke.evidence_id
        """
    )
    connection.executemany(
        "INSERT OR IGNORE INTO aliases(alias,canonical,alias_kind,weight) VALUES(?,?,?,?)",
        DEFAULT_ALIASES,
    )


def rebuild_family_scopes(connection: sqlite3.Connection, build_id: str) -> int:
    old_families = [row[0] for row in connection.execute("SELECT scope_id FROM scopes WHERE scope_type='family'")]
    for scope_id in old_families:
        _archive_knowledge_versions(connection, scope_id, build_id)
        connection.execute("DELETE FROM scopes WHERE scope_id=?", (scope_id,))
    connection.execute("UPDATE threads SET group_name='' ")
    threads = [dict(row) for row in connection.execute("SELECT * FROM threads ORDER BY thread_id")]
    ids = {str(row["thread_id"]) for row in threads}
    dsu = _DSU(ids)
    for row in threads:
        parent = row["parent_thread_id"]
        if parent in ids:
            dsu.union(str(row["thread_id"]), str(parent))
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in threads:
        groups[dsu.find(str(row["thread_id"]))].append(row)
    count = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda row: (row["first_activity_at"] or "", row["thread_id"]))
        thread_ids = [str(row["thread_id"]) for row in members]
        titles = [str(row["title"]) for row in members]
        scope_id = stable_id("family", thread_ids)
        title = f"{titles[0]} family ({len(members)} threads)"
        overview_rows = connection.execute(
            f"SELECT record_id,text,evidence_refs_json FROM knowledge WHERE tier='overview' AND scope_id IN ({','.join('?' for _ in thread_ids)}) ORDER BY occurred_start_at,record_id",
            thread_ids,
        ).fetchall()
        title_by_scope = {str(row["thread_id"]): str(row["title"]) for row in members}
        overview = "\n\n".join(
            f"[{title_by_scope.get(str(row['scope_id']), str(row['scope_id']))}] {row['text']}"
            for row in connection.execute(
                f"SELECT scope_id,record_id,text,evidence_refs_json FROM knowledge WHERE tier='overview' AND scope_id IN ({','.join('?' for _ in thread_ids)}) ORDER BY occurred_start_at,record_id",
                thread_ids,
            ).fetchall()
        )
        first_at = min((row["first_activity_at"] for row in members if row["first_activity_at"]), default=None)
        last_at = max((row["last_activity_at"] for row in members if row["last_activity_at"]), default=None)
        evidence_refs: list[str] = []
        for row in overview_rows:
            evidence_refs.extend(json.loads(row["evidence_refs_json"]))
        evidence_refs = list(dict.fromkeys(evidence_refs))[:128]
        connection.execute(
            "INSERT INTO scopes(scope_id,scope_type,scope_title,thread_ids_json,thread_titles_json,overview,human_verdict,evidence_rows,overview_path,ledger_path,first_activity_at,last_activity_at,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scope_id,
                "family",
                title,
                canonical_json(thread_ids),
                canonical_json(titles),
                overview,
                "deterministic_lineage",
                len(evidence_refs),
                f"codex-history-scope://{scope_id}",
                f"codex-history-scope://{scope_id}",
                first_at,
                last_at,
                utc_now(),
            ),
        )
        for ordinal, thread_id in enumerate(thread_ids):
            connection.execute(
                "INSERT INTO scope_threads(scope_id,thread_id,ordinal) VALUES(?,?,?)",
                (scope_id, thread_id, ordinal),
            )
            connection.execute("UPDATE threads SET group_name=? WHERE thread_id=?", (scope_id, thread_id))
        _insert_record(
            connection,
            record_id=stable_id("record", "overview", scope_id),
            tier="overview",
            asset_type="",
            scope_id=scope_id,
            scope_type="family",
            scope_title=title,
            category="overview",
            text=overview,
            status="mixed",
            status_group="mixed",
            evidence_refs=evidence_refs,
            source_path=f"codex-history-scope://{scope_id}",
            source_locator="deterministic-family-overview-v1",
            confidence="deterministic_extract",
            occurred_start_at=first_at,
            occurred_end_at=last_at,
            metadata={"thread_ids": thread_ids, "method": "parent-lineage-v1"},
        )
        count += 1
    refresh_evidence_rollups(connection)
    return count


def rebuild_conservative_relations(connection: sqlite3.Connection) -> dict[str, int]:
    connection.execute(
        "DELETE FROM knowledge_relations WHERE json_extract(metadata_json,'$.method')=?",
        (CONSERVATIVE_RELATION_METHOD,),
    )
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT record_id,scope_id,category,text,status,status_group,evidence_refs_json,"
            "occurred_start_at,metadata_json FROM knowledge WHERE tier='core' "
            "AND category IN ('tool_call','tool_output','goal_state') "
            "ORDER BY scope_id,occurred_start_at,record_id"
        )
    ]
    for row in rows:
        row["metadata"] = _parsed_json(row["metadata_json"], {})
        row["evidence_refs"] = _parsed_json(row["evidence_refs_json"], [])

    def event_order(row: dict[str, Any]) -> tuple[str, int, str]:
        try:
            line_no = int(row["metadata"].get("line_no", 0))
        except (TypeError, ValueError):
            line_no = 0
        return row["occurred_start_at"] or "", line_no, row["record_id"]

    inserted = defaultdict(int)

    def add_relation(source: dict[str, Any], relation_type: str, target: dict[str, Any], reason: str) -> None:
        evidence_refs = sorted(
            set(source["evidence_refs"]) | set(target["evidence_refs"])
        )
        cursor = connection.execute(
            "INSERT OR IGNORE INTO knowledge_relations("
            "relation_id,source_record_id,relation_type,target_record_id,evidence_refs_json,"
            "confidence,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                stable_id(
                    "relation",
                    CONSERVATIVE_RELATION_METHOD,
                    source["record_id"],
                    relation_type,
                    target["record_id"],
                ),
                source["record_id"],
                relation_type,
                target["record_id"],
                canonical_json(evidence_refs),
                "high",
                utc_now(),
                canonical_json(
                    {
                        "method": CONSERVATIVE_RELATION_METHOD,
                        "reason": reason,
                        "unsafe_auto_types_disabled": [
                            "contradicts",
                            "invalidates",
                            "reopens",
                        ],
                    }
                ),
            ),
        )
        if cursor.rowcount:
            inserted[relation_type] += 1

    calls: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        call_id = str(row["metadata"].get("call_id") or "")
        if call_id:
            calls[(str(row["scope_id"]), call_id)].append(row)
    for events in calls.values():
        tool_calls = sorted(
            (row for row in events if row["category"] == "tool_call"),
            key=event_order,
        )
        verified_outputs = [
            row
            for row in events
            if row["category"] == "tool_output" and row["status_group"] == "verified"
        ]
        for output in verified_outputs:
            earlier = [
                row
                for row in tool_calls
                if event_order(row) <= event_order(output)
            ]
            if earlier:
                add_relation(
                    output,
                    "validates",
                    earlier[-1],
                    "verified tool output matches the same call_id",
                )

    goal_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["category"] != "goal_state":
            continue
        objective = re.sub(r"^Goal\s*\[[^]]+\]:\s*", "", row["text"], flags=re.I)
        objective_key = normalize_text(objective).casefold()
        if objective_key:
            goal_rows[(str(row["scope_id"]), objective_key)].append(row)
    for events in goal_rows.values():
        events.sort(key=event_order)
        active: list[dict[str, Any]] = []
        for event in events:
            if event["status"] == "active":
                active.append(event)
            elif event["status"] == "goal_complete" and active:
                add_relation(
                    event,
                    "validates",
                    active[-1],
                    "a later complete state confirms the same goal objective",
                )

    return {"total": sum(inserted.values()), **dict(sorted(inserted.items()))}


def insert_model_ledger_items(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    items: list[dict[str, Any]],
    generation_id: str,
    build_id: str,
    model: str,
    cache_key: str,
) -> list[str]:
    scope = connection.execute("SELECT * FROM scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if scope is None:
        raise KeyError(scope_id)
    inserted: list[str] = []
    valid_statuses = {
        "verified",
        "executed",
        "planned",
        "failed",
        "blocked",
        "uncertain",
        "mixed",
    }
    for ordinal, item in enumerate(items):
        text = normalize_text(str(item.get("text") or ""))
        supporting = list(dict.fromkeys(str(value) for value in item.get("record_ids") or []))
        if not text or not supporting:
            continue
        placeholders = ",".join("?" for _ in supporting)
        rows = connection.execute(
            f"SELECT record_id,evidence_refs_json,occurred_start_at,occurred_end_at "
            f"FROM knowledge WHERE record_id IN ({placeholders})",
            supporting,
        ).fetchall()
        found = {str(row["record_id"]) for row in rows}
        if found != set(supporting):
            raise ValueError("Ledger item references an unavailable source record")
        evidence_refs: list[str] = []
        starts: list[str] = []
        ends: list[str] = []
        for row in rows:
            evidence_refs.extend(json.loads(row["evidence_refs_json"]))
            if row["occurred_start_at"]:
                starts.append(str(row["occurred_start_at"]))
            if row["occurred_end_at"]:
                ends.append(str(row["occurred_end_at"]))
        requested_status = str(item.get("status") or "uncertain")
        status_group = requested_status if requested_status in valid_statuses else "uncertain"
        category = normalize_text(str(item.get("category") or "historical_fact"))
        record_id = stable_id(
            "record",
            "incremental-ledger",
            scope_id,
            generation_id,
            ordinal,
            text,
        )
        _insert_record(
            connection,
            record_id=record_id,
            tier="ledger",
            asset_type="",
            scope_id=scope_id,
            scope_type=str(scope["scope_type"]),
            scope_title=str(scope["scope_title"]),
            category=category,
            text=text,
            status=requested_status,
            status_group=status_group,
            evidence_refs=list(dict.fromkeys(evidence_refs))[:512],
            source_path=f"codex-history-scope://{scope_id}",
            source_locator=f"model-ledger:{model}",
            confidence="model_evidence_linked",
            occurred_start_at=min(starts) if starts else None,
            occurred_end_at=max(ends) if ends else None,
            metadata={
                "model": model,
                "cache_key": cache_key,
                "generation_id": generation_id,
                "ingest_build_id": build_id,
                "supporting_record_ids": supporting,
                "method": "incremental-ledger-v1",
                "promotion_policy": "evidence-fence-v1",
            },
        )
        inserted.append(record_id)
    return inserted


def mark_model_consolidated(
    connection: sqlite3.Connection,
    *,
    record_ids: list[str],
    build_id: str,
) -> None:
    if not record_ids:
        return
    now = utc_now()
    for record_id in record_ids:
        row = connection.execute(
            "SELECT metadata_json FROM knowledge WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            continue
        metadata = _parsed_json(row["metadata_json"], {})
        metadata["model_consolidated_build_id"] = build_id
        metadata["model_consolidated_at"] = now
        connection.execute(
            "UPDATE knowledge SET metadata_json=? WHERE record_id=?",
            (canonical_json(metadata), record_id),
        )


def supersede_noncanonical_overviews(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    current_record_id: str,
    build_id: str = "",
) -> list[str]:
    rows = connection.execute(
        "SELECT record_id,metadata_json FROM knowledge WHERE scope_id=? "
        "AND tier='overview' AND record_id!=? AND valid_to IS NULL",
        (scope_id, current_record_id),
    ).fetchall()
    if not rows:
        return []
    now = utc_now()
    superseded: list[str] = []
    for row in rows:
        record_id = str(row["record_id"])
        if build_id:
            _archive_record_version(connection, record_id, build_id)
        metadata = _parsed_json(str(row["metadata_json"]), {})
        metadata["superseded_by_record_id"] = current_record_id
        metadata["superseded_at"] = now
        connection.execute(
            "UPDATE knowledge SET valid_to=?,indexed_at=?,metadata_json=? WHERE record_id=?",
            (now, now, canonical_json(metadata), record_id),
        )
        relation_id = stable_id(
            "relation", current_record_id, "supersedes", record_id
        )
        connection.execute(
            "INSERT OR REPLACE INTO knowledge_relations("
            "relation_id,source_record_id,relation_type,target_record_id,"
            "evidence_refs_json,confidence,created_at,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                relation_id,
                current_record_id,
                "supersedes",
                record_id,
                "[]",
                "deterministic",
                now,
                canonical_json(
                    {
                        "method": "overview-lifecycle-v1",
                        "build_id": build_id,
                    }
                ),
            ),
        )
        superseded.append(record_id)
    return superseded


def apply_model_scope_summary(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    overview: str,
    claims: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    model: str,
    cache_key: str,
    build_id: str = "",
) -> dict[str, int]:
    scope = connection.execute("SELECT * FROM scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if scope is None:
        raise KeyError(scope_id)
    overview_record_id = stable_id("record", "overview", scope_id)
    superseded_overviews = supersede_noncanonical_overviews(
        connection,
        scope_id=scope_id,
        current_record_id=overview_record_id,
        build_id=build_id,
    )
    allowed_records = {
        str(row[0])
        for row in connection.execute(
            "SELECT record_id FROM knowledge WHERE scope_id=? AND valid_to IS NULL "
            "AND tier IN ('fact_block','core','ledger','overview')",
            (scope_id,),
        )
        if str(row[0]) != overview_record_id
    }
    if scope["scope_type"] == "family":
        thread_ids = json.loads(scope["thread_ids_json"])
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            allowed_records.update(
                str(row[0])
                for row in connection.execute(
                    f"SELECT record_id FROM knowledge WHERE scope_id IN ({placeholders}) "
                    "AND valid_to IS NULL AND tier IN ('overview','ledger','fact_block')",
                    thread_ids,
                )
            )

    clean_claims: list[dict[str, Any]] = []
    overview_evidence: list[str] = []
    for claim in claims:
        text = normalize_text(str(claim.get("text") or ""))
        record_ids = [
            str(value) for value in claim.get("record_ids") or [] if str(value) in allowed_records
        ]
        if not text:
            continue
        evidence_refs: list[str] = []
        for record_id in record_ids:
            row = connection.execute(
                "SELECT evidence_refs_json FROM knowledge WHERE record_id=?", (record_id,)
            ).fetchone()
            if row:
                evidence_refs.extend(json.loads(row[0]))
        evidence_refs = list(dict.fromkeys(evidence_refs))
        overview_evidence.extend(evidence_refs)
        clean_claims.append(
            {
                "text": text,
                "record_ids": list(dict.fromkeys(record_ids)),
                "evidence_refs": evidence_refs,
            }
        )

    if build_id:
        _archive_record_version(connection, overview_record_id, build_id)
    connection.execute(
        "DELETE FROM overview_claims WHERE overview_record_id=?", (overview_record_id,)
    )
    connection.execute(
        "DELETE FROM knowledge_evidence WHERE record_id=?", (overview_record_id,)
    )
    connection.execute(
        "DELETE FROM record_evidence_occurrences WHERE record_id=?", (overview_record_id,)
    )
    overview_evidence = list(dict.fromkeys(overview_evidence))[:256]
    _insert_record(
        connection,
        record_id=overview_record_id,
        tier="overview",
        asset_type="",
        scope_id=scope_id,
        scope_type=str(scope["scope_type"]),
        scope_title=str(scope["scope_title"]),
        category="overview",
        text=overview,
        status="mixed",
        status_group="mixed",
        evidence_refs=overview_evidence,
        source_path=f"codex-history-scope://{scope_id}",
        source_locator=f"model-summary:{model}",
        confidence="model_evidence_linked",
        occurred_start_at=scope["first_activity_at"],
        occurred_end_at=scope["last_activity_at"],
        metadata={
            "model": model,
            "cache_key": cache_key,
            "claim_count": len(clean_claims),
            "method": "evidence-linked-writer-v1",
            "build_id": build_id,
        },
    )
    connection.execute(
        "UPDATE scopes SET overview=?,human_verdict=? WHERE scope_id=?",
        (overview, "model_evidence_linked", scope_id),
    )
    cursor = 0
    for ordinal, claim in enumerate(clean_claims):
        start = overview.find(claim["text"], cursor)
        if start < 0:
            start = 0
        end = start + len(claim["text"])
        cursor = max(cursor, end)
        claim_id = stable_id("claim", overview_record_id, ordinal, claim["text"])
        connection.execute(
            "INSERT INTO overview_claims(claim_id,overview_record_id,scope_id,ordinal,start_char,end_char,claim_text,status,confidence,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id,
                overview_record_id,
                scope_id,
                ordinal,
                start,
                end,
                claim["text"],
                "linked" if claim["record_ids"] else "unlinked",
                1.0 if claim["record_ids"] else None,
                utc_now(),
                canonical_json({"model": model, "cache_key": cache_key}),
            ),
        )
        for rank, record_id in enumerate(claim["record_ids"], 1):
            connection.execute(
                "INSERT INTO overview_claim_records(claim_id,record_id,match_method,score,rank,metadata_json) VALUES(?,?,?,?,?,?)",
                (claim_id, record_id, "model_explicit_reference", 1.0, rank, "{}"),
            )

    model_asset_rows = connection.execute(
        "SELECT record_id FROM knowledge WHERE scope_id=? AND tier='asset' AND source_locator LIKE 'model-summary:%'",
        (scope_id,),
    ).fetchall()
    for row in model_asset_rows:
        if build_id:
            _archive_record_version(connection, str(row["record_id"]), build_id)
        connection.execute("DELETE FROM knowledge WHERE record_id=?", (row["record_id"],))
    accepted_assets = 0
    for ordinal, asset in enumerate(assets):
        asset_type = str(asset.get("type") or "")
        text = normalize_text(str(asset.get("text") or ""))
        if asset_type not in {"decisions", "unresolved", "failures", "capabilities", "preferences"} or not text:
            continue
        record_ids = [
            str(value) for value in asset.get("record_ids") or [] if str(value) in allowed_records
        ]
        evidence_refs: list[str] = []
        for record_id in record_ids:
            row = connection.execute(
                "SELECT evidence_refs_json FROM knowledge WHERE record_id=?", (record_id,)
            ).fetchone()
            if row:
                evidence_refs.extend(json.loads(row[0]))
        evidence_refs = list(dict.fromkeys(evidence_refs))
        requested_status = str(asset.get("status") or "uncertain")
        status_group = requested_status if requested_status in {
            "verified", "executed", "planned", "failed", "blocked", "uncertain", "mixed"
        } else "uncertain"
        _insert_record(
            connection,
            record_id=stable_id("record", "model-asset", scope_id, asset_type, text),
            tier="asset",
            asset_type=asset_type,
            scope_id=scope_id,
            scope_type=str(scope["scope_type"]),
            scope_title=str(scope["scope_title"]),
            category=asset_type,
            text=text,
            status=requested_status,
            status_group=status_group,
            evidence_refs=evidence_refs,
            source_path=f"codex-history-scope://{scope_id}",
            source_locator=f"model-summary:{model}",
            confidence="model_evidence_linked" if record_ids else "model_unlinked",
            occurred_start_at=scope["first_activity_at"],
            occurred_end_at=scope["last_activity_at"],
            metadata={
                "model": model,
                "cache_key": cache_key,
                "supporting_record_ids": record_ids,
                "ordinal": ordinal,
                "build_id": build_id,
            },
        )
        accepted_assets += 1
    return {
        "claims": len(clean_claims),
        "assets": accepted_assets,
        "superseded_overviews": len(superseded_overviews),
    }
