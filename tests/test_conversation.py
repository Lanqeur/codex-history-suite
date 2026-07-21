from __future__ import annotations

import json
from pathlib import Path

from codex_history.conversation import (
    build_conversation_export,
    parse_turn_range,
    write_conversation_export,
)
from codex_history.pipeline import build_full
from codex_history.schema import connect

from conftest import add_transcript, goal_row


def test_turn_range_parser_uses_human_one_based_ranges():
    assert parse_turn_range("7").contains(7)
    assert not parse_turn_range("7").contains(6)
    assert parse_turn_range("4:12").contains(12)
    assert parse_turn_range(":5").contains(1)
    assert parse_turn_range("8:").contains(99)


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
    assistant_text = "Verified output.\nThe original line break remains."
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
            embed_images=True,
            artifact_roots=[config.cas_dir],
            title="Evidence viewer test",
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
    assert len(user["attachments"]) == 1
    assert user["attachments"][0]["available"] is True
    assert user["attachments"][0]["data_url"].startswith("data:image/png;base64,")

    output = tmp_path / "conversation.html"
    report = write_conversation_export(payload, output, output_format="html")
    html = output.read_text(encoding="utf-8")
    assert report["messages"] == 5
    assert "codex-history-data" in html
    assert "evidence-tray" in html
    assert '<html lang="zh-CN" data-theme="light">' in html
    assert 'id="toggle-theme"' in html
    assert "codex-history-viewer-theme" in html
    assert "data:image/png;base64," in html
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)" in html
