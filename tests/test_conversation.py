from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from codex_history.conversation import (
    build_conversation_export,
    parse_turn_range,
    write_conversation_export,
)
from codex_history.conversation_viewer import render_conversation_html
from codex_history.pipeline import build_full
from codex_history.schema import connect

from conftest import add_transcript, goal_row


def add_observed_artifact(
    database: Path,
    cas_root: Path,
    *,
    event_id: str,
    source_id: str,
    thread_id: str,
    original_path: str,
    data: bytes,
    mime_type: str,
    extension: str,
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    relative = Path("files") / digest[:2] / f"{digest}{extension}"
    destination = cas_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    uri = f"codex-history-artifact://sha256/{digest}"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO artifact_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                digest,
                len(data),
                f"{len(data)} B",
                relative.as_posix(),
                uri,
                mime_type,
                extension,
                original_path,
                "raw_evidence",
                "test_fixture",
                "document",
                1,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO artifact_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"observation-{digest}",
                event_id,
                source_id,
                thread_id,
                digest,
                None,
                original_path,
                original_path,
                "2026-07-14T01:00:00Z",
                "2026-07-14T01:00:00Z",
                "absolute_path_copy",
                "{}",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return digest


def test_turn_range_parser_uses_human_one_based_ranges():
    assert parse_turn_range("7").contains(7)
    assert not parse_turn_range("7").contains(6)
    assert parse_turn_range("4:12").contains(12)
    assert parse_turn_range(":5").contains(1)
    assert parse_turn_range("8:").contains(99)


def test_conversation_html_only_embeds_mermaid_runtime_when_needed():
    html = render_conversation_html(
        {
            "title": "Plain evidence",
            "export_id": "conversation-plain",
            "statistics": {"threads": 0, "messages": 0},
            "threads": [],
            "messages": [],
        }
    )
    assert 'data-vendor="marked-18.0.7"' in html
    assert 'data-vendor="dompurify-3.4.12"' in html
    assert 'data-vendor="mermaid-11.16.0"' not in html


def test_conversation_export_restores_visible_messages_and_embeds_images(
    portable_profile, tmp_path: Path, monkeypatch
):
    config, codex_home = portable_profile
    path = add_transcript(
        codex_home,
        "thread-conversation",
        "Conversation evidence sample",
        timestamp="2026-07-14T01:00:00Z",
        label="conversation",
        image=True,
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    user_text = "Inspect </script><script>alert(1)</script>\nand preserve the image."
    assistant_text = """Verified output.
The original line break remains.

| Check | Result |
| --- | --- |
| Exact text | Pass |

```mermaid
flowchart LR
  Source --> Evidence --> Review
```"""
    rows.insert(
        2,
        {
            "timestamp": "2026-07-14T01:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>private</environment_context>"}],
            },
        },
    )
    for row in rows:
        payload = row.get("payload", {})
        if row.get("type") == "response_item" and payload.get("type") == "message":
            if payload.get("role") == "user" and any(
                item.get("type") == "input_image" for item in payload.get("content", [])
            ):
                payload["content"][0]["text"] = user_text
            elif payload.get("role") == "assistant":
                payload["content"] = [{"type": "output_text", "text": assistant_text}]
        elif payload.get("type") == "user_message":
            payload["message"] = user_text
    assistant_index = next(
        index
        for index, row in enumerate(rows)
        if row.get("type") == "response_item"
        and row.get("payload", {}).get("type") == "message"
        and row.get("payload", {}).get("role") == "assistant"
    )
    rows.insert(
        assistant_index,
        {
            "timestamp": "2026-07-14T01:00:00Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": assistant_text},
        },
    )
    rows.insert(
        -1,
        goal_row(
            "thread-conversation",
            timestamp="2026-07-14T01:00:00Z",
            objective="Preserve exact evidence",
            status="complete",
        ),
    )
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    built = build_full(config)
    writable = sqlite3.connect(built["database"])
    writable.row_factory = sqlite3.Row
    user_event = writable.execute(
        "SELECT event_id,source_id,thread_id FROM canonical_events "
        "WHERE thread_id='thread-conversation' AND payload_type='user_message'"
    ).fetchone()
    writable.close()
    text_data = b"release checklist\n- verify evidence\n- preserve attachments\n"
    pdf_data = b"%PDF-1.4\n% portable attachment fixture\n%%EOF\n"
    text_digest = add_observed_artifact(
        Path(built["database"]),
        config.cas_dir,
        event_id=user_event["event_id"],
        source_id=user_event["source_id"],
        thread_id=user_event["thread_id"],
        original_path="D:/Evidence/release-checklist.txt",
        data=text_data,
        mime_type="text/plain",
        extension=".txt",
    )
    pdf_digest = add_observed_artifact(
        Path(built["database"]),
        config.cas_dir,
        event_id=user_event["event_id"],
        source_id=user_event["source_id"],
        thread_id=user_event["thread_id"],
        original_path="D:/Evidence/release-brief.pdf",
        data=pdf_data,
        mime_type="application/pdf",
        extension=".pdf",
    )
    connection = connect(Path(built["database"]), readonly=True)
    try:
        payload = build_conversation_export(
            connection,
            config.snapshots_dir,
            selectors=["Conversation evidence"],
            turn_range=parse_turn_range("1"),
            include_tools=True,
            include_goals=True,
            include_internal=False,
            include_raw=True,
            embed_attachments=True,
            artifact_roots=[config.cas_dir],
            title="Evidence viewer test",
        )
        metadata_only = build_conversation_export(
            connection,
            config.snapshots_dir,
            selectors=["Conversation evidence"],
            turn_range=parse_turn_range("1"),
            embed_images=False,
            embed_attachments=False,
            artifact_roots=[config.cas_dir],
        )
        limited = build_conversation_export(
            connection,
            config.snapshots_dir,
            selectors=["Conversation evidence"],
            turn_range=parse_turn_range("1"),
            embed_attachments=True,
            artifact_roots=[config.cas_dir],
            max_attachment_bytes=40,
            max_embedded_bytes=100,
        )
    finally:
        connection.close()

    assert payload["statistics"]["roles"] == {
        "assistant": 1,
        "goal": 1,
        "tool_call": 1,
        "tool_output": 1,
        "user": 1,
    }
    assert payload["threads"][0]["duplicate_events_suppressed"] == 2
    assert payload["threads"][0]["internal_events_suppressed"] == 1
    user = next(message for message in payload["messages"] if message["role"] == "user")
    assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
    assert user_text in user["content"]
    assert assistant["content"] == assistant_text
    assert user["raw_event"]["payload"]["type"] == "user_message"
    assert len(user["attachments"]) == 3
    assert all(item["available"] for item in user["attachments"])
    assert all(item["embedded"] for item in user["attachments"])
    assert {item["kind"] for item in user["attachments"]} == {"image", "pdf", "text"}
    assert payload["artifacts"][text_digest]["data_url"].startswith("data:text/plain;base64,")
    assert payload["artifacts"][text_digest]["text_preview"].startswith("release checklist")
    assert payload["artifacts"][pdf_digest]["data_url"].startswith(
        "data:application/pdf;base64,"
    )
    assert payload["statistics"]["referenced_attachments"] == 3
    assert payload["statistics"]["embedded_attachments"] == 3
    assert metadata_only["artifacts"] == {}
    metadata_user = next(
        message for message in metadata_only["messages"] if message["role"] == "user"
    )
    assert len(metadata_user["attachments"]) == 3
    assert {item["status"] for item in metadata_user["attachments"]} == {
        "available_not_embedded"
    }
    assert limited["statistics"]["embedded_attachments"] == 1
    assert limited["statistics"]["skipped_attachments"] == 2

    output = tmp_path / "conversation.html"
    report = write_conversation_export(payload, output, output_format="html")
    html = output.read_text(encoding="utf-8")
    assert report["messages"] == 5
    assert "codex-history-data" in html
    assert "evidence-tray" in html
    assert '<html lang="zh-CN" data-theme="light">' in html
    assert 'id="toggle-theme"' in html
    assert "codex-history-viewer-theme" in html
    assert 'data-vendor="dompurify-3.4.12"' in html
    assert 'data-vendor="marked-18.0.7"' in html
    assert 'data-vendor="mermaid-11.16.0"' in html
    assert 'data-view-mode="rendered"' in html
    assert 'data-view-mode="source"' in html
    assert "securityLevel:'strict'" in html
    assert "data:image/png;base64," in html
    assert "release-checklist.txt" in html
    assert "release-brief.pdf" in html
    assert "attachmentNode" in html
    assert "文本预览" in html
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)" in html
