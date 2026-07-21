from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from codex_history.conversation import resolve_threads
from codex_history.pipeline import build_full
from codex_history.schema import connect
from codex_history.session_restore import (
    DEFAULT_RESTORE_TITLE_CHARS,
    materialize_thread_snapshot,
    restore_native_thread,
)

from conftest import add_transcript, goal_row


def _fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.144.0-test")
    raise SystemExit(0)
if sys.argv[1:] != ["app-server"]:
    raise SystemExit(2)

home = Path(os.environ["CODEX_HOME"])
thread = {}
for line in sys.stdin:
    request = json.loads(line)
    identifier = request.get("id")
    if identifier is None:
        continue
    method = request["method"]
    params = request.get("params", {})
    if method == "initialize":
        result = {
            "codexHome": str(home),
            "platformFamily": "unix",
            "platformOs": "linux",
        }
    elif method == "thread/fork":
        destination = home / "sessions/2026/07/21/rollout-native-restored-id.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(params["path"], destination)
        thread = {
            "id": "native-restored-id",
            "forkedFromId": params["threadId"],
            "cwd": params["cwd"],
            "path": str(destination),
            "turns": [{"id": "turn-restored"}],
        }
        result = {"thread": thread}
    elif method == "thread/name/set":
        thread["name"] = params["name"]
        result = {}
    elif method == "thread/read":
        result = {"thread": thread}
    else:
        print(json.dumps({"id": identifier, "error": {"message": method}}), flush=True)
        continue
    print(json.dumps({"id": identifier, "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def _build_restore_fixture(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    path = add_transcript(
        codex_home,
        "thread-native-restore",
        "Native restore evidence",
        timestamp="2026-07-14T01:00:00Z",
        label="native restore",
        image=True,
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    user = next(
        row
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") == "message"
        and row["payload"].get("role") == "user"
    )
    image = next(
        item
        for item in user["payload"]["content"]
        if item["type"] == "input_image"
    )
    user["payload"]["content"].append(dict(image))
    rows.insert(
        -1,
        goal_row(
            "thread-native-restore",
            timestamp="2026-07-14T01:00:00Z",
            objective="Restore the complete execution trace",
            status="complete",
        ),
    )
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    built = build_full(config)
    return config, built


def test_materialized_restore_preserves_trace_and_deduplicates_images(
    portable_profile, tmp_path: Path, monkeypatch
):
    config, built = _build_restore_fixture(portable_profile, monkeypatch)
    connection = connect(Path(built["database"]), readonly=True)
    output = tmp_path / "materialized.jsonl"
    try:
        thread = resolve_threads(connection, ["thread-native-restore"])[0]
        report = materialize_thread_snapshot(
            connection,
            config.snapshots_dir,
            thread,
            output,
            artifact_roots=[config.cas_dir],
        )
    finally:
        connection.close()

    rendered = output.read_text(encoding="utf-8")
    restored_rows = [json.loads(line) for line in rendered.splitlines()]
    assert report["line_count"] == len(restored_rows)
    assert restored_rows[0]["type"] == "session_meta"
    assert restored_rows[0]["payload"]["id"] == "thread-native-restore"
    assert sum('"type":"function_call"' in line for line in rendered.splitlines()) == 1
    assert sum('"type":"function_call_output"' in line for line in rendered.splitlines()) == 1
    assert "thread_goal_updated" in rendered
    assert "2026-07-14T01:00:00Z" in rendered
    assert "codex-history-artifact://" not in rendered
    assert rendered.count("data:image/png;base64,") == 1
    assert "duplicate image omitted" in rendered
    assert report["images"]["artifact_uri_references"] == 2
    assert report["images"]["images_inlined"] == 1
    assert report["images"]["duplicate_image_occurrences_omitted"] == 1


def test_restore_dry_run_is_read_only_and_native_fork_is_audited(
    portable_profile, tmp_path: Path, monkeypatch
):
    config, built = _build_restore_fixture(portable_profile, monkeypatch)
    fake_codex = _fake_codex(tmp_path)
    target_home = tmp_path / "target-codex-home"
    cwd = tmp_path / "restored-workspace"
    cwd.mkdir()
    connection = connect(Path(built["database"]), readonly=True)
    try:
        dry_run = restore_native_thread(
            connection,
            config.snapshots_dir,
            selector="thread-native-restore",
            artifact_roots=[config.cas_dir],
            codex_home=target_home,
            codex_bin=fake_codex,
            cwd=cwd,
            title="x" * 500,
            dry_run=True,
        )
        assert not target_home.exists()
        assert dry_run["status"] == "dry_run"
        assert dry_run["target"]["codex_home_exists"] is False
        assert len(dry_run["target"]["title"]) == DEFAULT_RESTORE_TITLE_CHARS
        assert dry_run["cost"]["model_calls"] == 0

        restored = restore_native_thread(
            connection,
            config.snapshots_dir,
            selector="thread-native-restore",
            artifact_roots=[config.cas_dir],
            codex_home=target_home,
            codex_bin=fake_codex,
            cwd=cwd,
            title="Restored native evidence",
        )
    finally:
        connection.close()

    assert restored["status"] == "complete"
    assert restored["restored"]["thread_id"] == "native-restored-id"
    assert restored["restored"]["forked_from_thread_id"] == "thread-native-restore"
    assert restored["restored"]["title"] == "Restored native evidence"
    assert restored["restored"]["turn_count_verified"] == 1
    assert restored["restored"]["historical_turn_records"] == 1
    assert restored["restored"]["rollout"]["sha256"]
    assert restored["restored"]["cli_resume_command"] == "codex resume native-restored-id"
    assert Path(restored["restored"]["rollout_path"]).is_file()
    manifest = Path(restored["restore_manifest"])
    assert manifest.is_file()
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["source"]["thread_id"] == "thread-native-restore"
    assert manifest_payload["materialized"]["sha256"]
    assert manifest_payload["safety"]["mutates_existing_threads"] is False
    assert os.fspath(target_home) in os.fspath(manifest)
