from __future__ import annotations

import json
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from codex_history.artifact_capture import _find_git_root, _is_codex_storage
from codex_history.cli import main as cli_main
from codex_history.audit import audit_database
from codex_history.coverage import knowledge_coverage
from codex_history.pipeline import (
    active_database,
    artifact_capture_plan,
    build_full,
    capture_artifacts,
    equivalence_audit,
    plan,
    update_incremental,
)
from codex_history.schema import connect
from codex_history.source import classify_changes, discover_sources, snapshot_source

from conftest import add_transcript, goal_row


def test_git_root_probe_treats_permission_denied_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "restricted" / "source.py"
    source.parent.mkdir()
    source.write_text("print('test')\n", encoding="utf-8")
    original_exists = Path.exists

    def guarded_exists(path: Path) -> bool:
        if path.name == ".git":
            raise PermissionError(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    assert _find_git_root(source) is None


def test_codex_storage_detection_covers_windows_and_posix_paths():
    assert _is_codex_storage(Path("/home/user/.codex/sessions/example.jsonl"))
    assert _is_codex_storage(Path("/mnt/c/Users/A/.codex/.tmp/plugins/file.zip"))
    assert not _is_codex_storage(Path("/workspace/codex-history-suite/file.zip"))


def test_model_first_build_requires_an_explicit_cost_limit(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-paid-guard",
        "Paid guard",
        timestamp="2026-07-14T00:30:00Z",
        label="paid-guard",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only")
    build_plan = plan(config, mode="full")
    assert build_plan["effective_summarization_mode"] == "openai-compatible"
    assert build_plan["estimated_cost_cny"] > 0
    with pytest.raises(RuntimeError, match="explicit --max-cost-cny"):
        build_full(config)


def test_artifact_closure_failure_blocks_promotion(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-artifact-gate",
        "Artifact gate",
        timestamp="2026-07-14T00:30:00Z",
        label="artifact-gate",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        "codex_history.pipeline.inspect_artifact_closure",
        lambda *_args, **_kwargs: ({"complete": False, "missing_files": 1}, []),
    )
    with pytest.raises(RuntimeError, match="Build audit failed"):
        build_full(config)
    assert active_database(config) is None


def test_coverage_separates_activity_and_snapshot_watermarks(portable_profile):
    config, codex_home = portable_profile
    timestamp = "2026-07-14T00:30:00Z"
    add_transcript(
        codex_home,
        "thread-coverage",
        "Coverage",
        timestamp=timestamp,
        label="coverage",
    )
    built = build_full(config)
    coverage = knowledge_coverage(
        config,
        Path(built["database"]),
        active={"build_id": built["build_id"], "incremental_ready": True},
    )
    assert coverage["coverage_basis"] == "canonical-events"
    assert coverage["latest_activity_at"] == "2026-07-14T00:30:00.000Z"
    assert coverage["source_scan_started_at"] is not None
    assert coverage["source_snapshot_completed_at"] is not None
    assert coverage["authority_completed_at"] is not None
    assert coverage["logical_digest"]
    assert coverage["knowledge_version_id"].startswith("coverage-")


def test_full_build_incremental_update_and_equivalence(portable_profile):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-alpha",
        "Alpha implementation",
        timestamp="2026-07-14T01:00:00Z",
        label="alpha",
        image=True,
    )

    initial_plan = plan(config, mode="full")
    source_estimate = initial_plan["estimate"]["source"]
    assert source_estimate["inline_base64_payload_bytes_excluded_from_model_estimate"] > 0
    assert source_estimate["model_relevant_bytes"] < source_estimate["new_or_reprocessed_bytes"]

    first = build_full(config)
    assert first["status"] == "complete"
    assert first["audit"]["passed"] is True
    assert first["usage"]["total_api_tokens"] == 0
    assert first["storage"]["core_components_bytes"]["active_sqlite_build"] > 0
    assert first["storage"]["profile_total_bytes"] >= first["storage"]["core_total_bytes"]
    first_database = Path(first["database"])
    connection = connect(first_database, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifact_files").fetchone()[0] == 1
        source_row = connection.execute(
            "SELECT size_bytes,snapshot_size_bytes,snapshot_format FROM source_files"
        ).fetchone()
        assert source_row["snapshot_format"] == "normalized-jsonl-v1"
        assert source_row["snapshot_size_bytes"] > 0
        snapshot_payload = b"".join(
            (config.snapshots_dir / row[0]).read_bytes()
            for row in connection.execute(
                "SELECT cas_relative_path FROM source_chunks ORDER BY chunk_index"
            )
        )
        assert b"data:image/" not in snapshot_payload
        assert b"codex-history-artifact://" in snapshot_payload
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE raw_json LIKE '%data:image/%'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE raw_json<>''"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE role='tool_call' "
            "AND text LIKE '%test alpha%'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE role='tool_output' "
            "AND text LIKE '%3 passed%'"
        ).fetchone()[0] == 1
        from codex_history import query as query_module

        query_module.SNAPSHOT_ROOT = config.snapshots_dir
        evidence = dict(
            connection.execute(
                "SELECT * FROM evidence WHERE assignment LIKE '%test alpha%' LIMIT 1"
            ).fetchone()
        )
        raw_sources = query_module.portable_evidence_sources(connection, evidence)
        assert raw_sources
        assert "test alpha" in json.dumps(
            raw_sources[0]["content"]["raw"], ensure_ascii=False
        )
        relation = connection.execute(
            "SELECT kr.relation_type,source.category AS source_category,"
            "target.category AS target_category,kr.metadata_json "
            "FROM knowledge_relations kr "
            "JOIN knowledge source ON source.record_id=kr.source_record_id "
            "JOIN knowledge target ON target.record_id=kr.target_record_id"
        ).fetchone()
        assert relation["relation_type"] == "validates"
        assert relation["source_category"] == "tool_output"
        assert relation["target_category"] == "tool_call"
        assert "deterministic-event-transition-v1" in relation["metadata_json"]
        assert connection.execute("SELECT COUNT(*) FROM stage_checkpoints").fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE tier='fact_block'"
        ).fetchone()[0] == 1
    finally:
        connection.close()

    add_transcript(
        codex_home,
        "thread-beta",
        "Beta implementation",
        timestamp="2026-07-14T02:00:00Z",
        label="beta",
    )
    dry_run = plan(config, mode="incremental")
    assert dry_run["change_counts"] == {"added": 1, "unchanged": 1}
    assert dry_run["actionable_count"] == 1
    assert dry_run["estimated_cost_cny"] == 0
    assert dry_run["effective_summarization_mode"] == "extractive"
    assert dry_run["summarization"]["fallback"] is True
    assert dry_run["estimated_summary_cost_cny_if_model_enabled"] > 0
    assert dry_run["estimate"]["tokens"]["summary"]["input_expected"] > 0
    assert dry_run["estimated_storage_bytes"] > dry_run["estimate"]["source"]["total_bytes"]

    second = update_incremental(config)
    assert second["status"] == "complete"
    assert second["kind"] == "incremental"
    assert second["audit"]["passed"] is True
    assert active_database(config) == Path(second["database"])
    connection = connect(Path(second["database"]), readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] > 10
    finally:
        connection.close()
    add_transcript(
        codex_home,
        "thread-gamma",
        "Gamma implementation",
        timestamp="2026-07-14T03:00:00Z",
        label="gamma",
    )
    third = update_incremental(config)
    assert third["status"] == "complete"
    assert third["run"]["stages"]["ingest"]["report"]["threads"] == 1
    connection = connect(Path(third["database"]), readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 3
    finally:
        connection.close()

    equivalence = equivalence_audit(config)
    assert equivalence["passed"] is True, json.dumps(equivalence["differences"], indent=2)
    assert audit_database(Path(third["database"]))["passed"] is True


def test_append_rebuilds_only_affected_thread(portable_profile):
    config, codex_home = portable_profile
    first_path = add_transcript(
        codex_home,
        "thread-one",
        "One",
        timestamp="2026-07-14T01:00:00Z",
        label="one",
    )
    add_transcript(
        codex_home,
        "thread-two",
        "Two",
        timestamp="2026-07-14T02:00:00Z",
        label="two",
    )
    build_full(config)
    with first_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T03:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-one-extra"},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T03:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Add one more verified step."}],
                    },
                }
            )
            + "\n"
        )
    dry_run = plan(config, mode="incremental")
    assert dry_run["change_counts"] == {"appended": 1, "unchanged": 1}
    updated = update_incremental(config)
    ingest_report = updated["run"]["stages"]["ingest"]["report"]
    assert ingest_report["threads"] == 1
    assert equivalence_audit(config)["passed"] is True


def test_snapshot_is_bounded_when_active_transcript_grows(portable_profile):
    config, codex_home = portable_profile
    path = add_transcript(
        codex_home,
        "thread-growing",
        "Growing",
        timestamp="2026-07-14T01:00:00Z",
        label="initial",
    )
    sampled = discover_sources(config)[0]
    sampled_size = sampled.size_bytes
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": "2026-07-14T02:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}}) + "\n")

    snapshot = snapshot_source(config, sampled)
    assert snapshot.source.size_bytes == sampled_size
    assert sum(chunk["size_bytes"] for chunk in snapshot.chunks) == sampled_size

    current = discover_sources(config)[0]
    previous = {
        sampled.source_id: {
            "source_id": sampled.source_id,
            "source_path": str(sampled.path),
            "thread_id": sampled.thread_id,
            "size_bytes": snapshot.source.size_bytes,
            "mtime_ns": sampled.mtime_ns,
            "content_sha256": snapshot.content_sha256,
        }
    }
    changes = classify_changes([current], previous)
    assert [(change.kind, change.reason) for change in changes] == [
        ("appended", "old content is exact prefix")
    ]


def test_consumer_cli_uses_active_profile(portable_profile, capsys):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-search",
        "Searchable implementation",
        timestamp="2026-07-14T01:00:00Z",
        label="searchable-contract",
    )
    build_full(config)
    result = cli_main(
        ["--home", str(config.home), "search", "searchable-contract", "--json"]
    )
    assert result == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows
    assert any("searchable-contract" in row["text"] for row in rows)


def test_fresh_machine_cli_acceptance(tmp_path: Path, capsys):
    codex_home = tmp_path / "codex-home"
    data_home = tmp_path / "history-home"
    add_transcript(
        codex_home,
        "thread-first",
        "First portable session",
        timestamp="2026-07-14T01:00:00Z",
        label="portable-first",
    )

    assert cli_main(["--home", str(data_home), "init", "--source", str(codex_home), "--json"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["status"] == "initialized"
    assert cli_main(["--home", str(data_home), "plan", "--mode", "full", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["source_count"] == 1
    assert planned["estimated_cost_cny"] == 0
    assert cli_main(["--home", str(data_home), "build", "--max-cost-cny", "0", "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["audit"]["passed"] is True
    assert cli_main(["--home", str(data_home), "search", "portable-first", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)

    add_transcript(
        codex_home,
        "thread-second",
        "Second portable session",
        timestamp="2026-07-14T02:00:00Z",
        label="portable-second",
    )
    assert cli_main(["--home", str(data_home), "update", "--dry-run", "--json"]) == 0
    update_plan = json.loads(capsys.readouterr().out)
    assert update_plan["change_counts"] == {"added": 1, "unchanged": 1}
    assert cli_main(["--home", str(data_home), "update", "--max-cost-cny", "0", "--json"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["run"]["stages"]["ingest"]["report"]["threads"] == 1
    assert cli_main(["--home", str(data_home), "audit", "--equivalence", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_goal_events_are_searchable_and_content_deduplicated(portable_profile):
    config, codex_home = portable_profile
    path = add_transcript(
        codex_home,
        "thread-goal",
        "Goal history",
        timestamp="2026-07-14T01:00:00Z",
        label="goal-work",
    )
    objective = "Complete the portable history release gate."
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(goal_row("thread-goal", timestamp="2026-07-14T01:01:00Z", objective=objective, status="active")) + "\n")
        repeated = goal_row("thread-goal", timestamp="2026-07-14T01:02:00Z", objective=objective, status="active")
        repeated["payload"]["goal"]["tokensUsed"] = 200
        handle.write(json.dumps(repeated) + "\n")
        handle.write(json.dumps(goal_row("thread-goal", timestamp="2026-07-14T01:03:00Z", objective=objective, status="complete")) + "\n")
    built = build_full(config)
    connection = connect(Path(built["database"]), readonly=True)
    try:
        goal_records = connection.execute(
            "SELECT status,status_group,evidence_refs_json FROM knowledge "
            "WHERE scope_id='thread-goal' AND category='goal_state' ORDER BY occurred_start_at"
        ).fetchall()
        assert [row["status"] for row in goal_records] == ["active", "active", "goal_complete"]
        active_evidence = [json.loads(row["evidence_refs_json"])[0] for row in goal_records[:2]]
        assert active_evidence[0] == active_evidence[1]
        occurrence_count = connection.execute(
            "SELECT occurrence_count FROM evidence WHERE evidence_id=?", (active_evidence[0],)
        ).fetchone()[0]
        assert occurrence_count == 2
        goal_relation = connection.execute(
            "SELECT kr.relation_type,source.status AS source_status,target.status AS target_status "
            "FROM knowledge_relations kr "
            "JOIN knowledge source ON source.record_id=kr.source_record_id "
            "JOIN knowledge target ON target.record_id=kr.target_record_id "
            "WHERE source.category='goal_state'"
        ).fetchone()
        assert tuple(goal_relation) == ("validates", "goal_complete", "active")
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_relations "
            "WHERE relation_type IN ('contradicts','invalidates','reopens')"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_optional_absolute_path_capture_uses_artifact_cas(portable_profile, tmp_path: Path):
    config, codex_home = portable_profile
    artifact = tmp_path / "valuable report.pdf"
    artifact.write_text("historical artifact payload", encoding="utf-8")
    path = add_transcript(
        codex_home,
        "thread-artifact",
        "Artifact history",
        timestamp="2026-07-14T01:00:00Z",
        label="artifact-work",
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T01:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f'Retain the evidence file "{artifact}".',
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    capture_config = replace(
        config,
        artifact_capture_paths=True,
        artifact_exclude_temporary=False,
    )
    built = build_full(capture_config)
    connection = connect(Path(built["database"]), readonly=True)
    try:
        row = connection.execute(
            "SELECT af.cas_relative_path,ap.source_open_path FROM artifact_files af "
            "JOIN artifact_paths ap ON ap.sha256=af.sha256 "
            "WHERE ap.category='referenced_file'"
        ).fetchone()
        assert row is not None
        assert row["source_open_path"] == str(artifact)
        assert (capture_config.cas_dir / row["cas_relative_path"]).read_text(encoding="utf-8") == "historical artifact payload"
        assert connection.execute("SELECT COUNT(*) FROM ledger_artifacts").fetchone()[0] == 1
    finally:
        connection.close()
    assert equivalence_audit(capture_config)["passed"] is True


def test_artifact_only_build_captures_document_and_git_checkpoint(
    portable_profile, tmp_path: Path
):
    config, codex_home = portable_profile
    document = tmp_path / "release-notes.pdf"
    document.write_bytes(b"portable release notes")
    document_alias = tmp_path / "release-notes-copy.pdf"
    document_alias.write_bytes(document.read_bytes())
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Codex History Test"],
        check=True,
    )
    source_file = repository / "main.py"
    source_file.write_text("print('checkpoint')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "main.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "checkpoint"],
        check=True,
    )
    transcript = add_transcript(
        codex_home,
        "thread-artifact-only",
        "Artifact-only capture",
        timestamp="2026-07-14T01:00:00Z",
        label="artifact-only",
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T01:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f'Preserve "{document}", "{document_alias}", '
                                    f'and the source file "{source_file}".'
                                ),
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    built = build_full(config)
    parent_build_id = built["build_id"]
    source_file.write_text("print('dirty checkpoint')\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("untracked evidence\n", encoding="utf-8")
    capture_config = replace(
        config,
        artifact_capture_paths=True,
        artifact_capture_git_repositories=True,
        artifact_exclude_temporary=False,
    )

    dry_run = artifact_capture_plan(capture_config)
    assert dry_run["work_required"] is True
    assert dry_run["ordinary_files"]["new_content_files"] == 1
    assert len(dry_run["ordinary_files"]["files"][0]["paths"]) == 2
    assert dry_run["git_repositories"]["new_checkpoints"] == 1
    assert (
        dry_run["git_repositories"]["repositories"][0]["capture_mode"]
        == "bundle-all+worktree"
    )
    assert dry_run["cost_cny"] == 0

    captured = capture_artifacts(capture_config)
    assert captured["status"] == "complete"
    assert captured["build_id"] != parent_build_id
    assert captured["usage"]["total_api_tokens"] == 0
    assert captured["usage"]["total_cost_cny"] == 0
    assert captured["audit"]["passed"] is True
    database = Path(captured["database"])
    connection = connect(database, readonly=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_observations"
        ).fetchone()[0] >= 3
        checkpoint = connection.execute(
            """
            SELECT capture_mode,history_artifact_sha256,worktree_artifact_sha256
            FROM repository_checkpoints
            """
        ).fetchone()
        assert checkpoint["capture_mode"] == "bundle-all+worktree"
        bundle = connection.execute(
            "SELECT cas_relative_path FROM artifact_files WHERE sha256=?",
            (checkpoint["history_artifact_sha256"],),
        ).fetchone()
        bundle_path = capture_config.cas_dir / bundle["cas_relative_path"]
        subprocess.run(
            ["git", "bundle", "verify", str(bundle_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        worktree = connection.execute(
            "SELECT cas_relative_path FROM artifact_files WHERE sha256=?",
            (checkpoint["worktree_artifact_sha256"],),
        ).fetchone()
        with tarfile.open(
            capture_config.cas_dir / worktree["cas_relative_path"], "r:gz"
        ) as archive:
            names = set(archive.getnames())
        assert "project/main.py" in names
        assert "project/untracked.txt" in names
        document_row = connection.execute(
            """
            SELECT af.cas_relative_path
            FROM artifact_files af
            JOIN artifact_paths ap ON ap.sha256=af.sha256
            WHERE ap.category='referenced_file'
            """
        ).fetchone()
        assert (capture_config.cas_dir / document_row["cas_relative_path"]).read_bytes() == (
            document.read_bytes()
        )
    finally:
        connection.close()

    second_plan = artifact_capture_plan(capture_config)
    assert second_plan["work_required"] is False
    assert second_plan["git_repositories"]["reused_checkpoints"] == 1


def test_partial_clone_defaults_to_head_archive(portable_profile, tmp_path: Path):
    config, codex_home = portable_profile
    repository = tmp_path / "partial-project"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Codex History Test"],
        check=True,
    )
    source_file = repository / "main.py"
    source_file.write_text("print('partial')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "main.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "partial checkpoint"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "remote.origin.promisor", "true"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "remote.origin.partialclonefilter",
            "blob:none",
        ],
        check=True,
    )
    transcript = add_transcript(
        codex_home,
        "thread-partial-clone",
        "Partial clone capture",
        timestamp="2026-07-14T02:00:00Z",
        label="partial-clone",
    )
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T02:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f'Preserve the source file "{source_file}".',
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    build_full(config)
    capture_config = replace(
        config,
        artifact_capture_git_repositories=True,
        artifact_exclude_temporary=False,
    )
    dry_run = artifact_capture_plan(capture_config)
    repository_plan = dry_run["git_repositories"]["repositories"][0]
    assert repository_plan["is_partial_clone"] is True
    assert repository_plan["capture_mode"] == "head-archive"
    captured = capture_artifacts(capture_config)
    connection = connect(Path(captured["database"]), readonly=True)
    try:
        checkpoint = connection.execute(
            "SELECT capture_mode,history_artifact_sha256 FROM repository_checkpoints"
        ).fetchone()
        assert checkpoint["capture_mode"] == "head-archive"
        artifact = connection.execute(
            "SELECT extension,cas_relative_path FROM artifact_files WHERE sha256=?",
            (checkpoint["history_artifact_sha256"],),
        ).fetchone()
        assert artifact["extension"] == ".tar.gz"
        with tarfile.open(capture_config.cas_dir / artifact["cas_relative_path"], "r:gz"):
            pass
    finally:
        connection.close()
