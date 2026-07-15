from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from codex_history.config import load_config, write_initial_config


def transcript_rows(
    thread_id: str,
    *,
    timestamp: str,
    label: str,
    image: bool = False,
    parent_thread_id: str | None = None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": f"Build {label} and verify it."}]
    if image:
        encoded = base64.b64encode(b"portable-image-fixture").decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{encoded}",
            }
        )
    turn_id = f"turn-{thread_id}"
    rows = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": timestamp,
                "cwd": "/workspace/example",
                "originator": "codex_cli_rs",
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id, "started_at": timestamp},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": content},
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"Build {label} and verify it."},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": f"test {label}"}),
                "call_id": f"call-{thread_id}",
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": f"call-{thread_id}",
                "output": "Process exited with code 0. 3 passed.",
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"Implemented {label}; tests passed."}],
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "completed_at": timestamp,
                "last_agent_message": f"Implemented {label}; tests passed.",
            },
        },
    ]
    if parent_thread_id:
        rows[0]["payload"]["parent_thread_id"] = parent_thread_id
    return rows


def goal_row(thread_id: str, *, timestamp: str, objective: str, status: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "thread_goal_updated",
            "threadId": thread_id,
            "turnId": f"turn-{thread_id}",
            "goal": {
                "threadId": thread_id,
                "objective": objective,
                "status": status,
                "tokensUsed": 100,
                "timeUsedSeconds": 10,
            },
        },
    }


def add_transcript(
    codex_home: Path,
    thread_id: str,
    title: str,
    *,
    timestamp: str,
    label: str,
    image: bool = False,
    parent_thread_id: str | None = None,
) -> Path:
    path = codex_home / "sessions/2026/07/14" / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = transcript_rows(
        thread_id,
        timestamp=timestamp,
        label=label,
        image=image,
        parent_thread_id=parent_thread_id,
    )
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    with (codex_home / "session_index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": thread_id, "thread_name": title, "updated_at": timestamp}) + "\n")
    return path


@pytest.fixture
def portable_profile(tmp_path: Path):
    codex_home = tmp_path / "codex-source"
    codex_home.mkdir()
    data_home = tmp_path / "history-home"
    write_initial_config(data_home, profile="default", source_roots=[codex_home])
    return load_config(data_home), codex_home
