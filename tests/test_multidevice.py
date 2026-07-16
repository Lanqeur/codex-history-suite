from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from codex_history.config import load_config, write_initial_config
from codex_history.artifacts import inspect_artifact_closure
from codex_history.library import (
    apply_delta,
    configure_device,
    export_delta,
    export_library,
    federated_search,
    import_library,
    list_libraries,
    merge_libraries,
    verify_bundle,
    verify_delta,
)
from codex_history.pipeline import active_database, build_full, update_incremental
from codex_history.pipeline import plan
from codex_history.schema import connect

from conftest import add_transcript, transcript_rows


def _device_profile(
    root: Path,
    device_name: str,
    transcripts: list[tuple[str, str, str]],
) -> tuple[Path, object]:
    codex_home = root / "codex"
    history_home = root / "history"
    codex_home.mkdir(parents=True)
    write_initial_config(history_home, profile="default", source_roots=[codex_home])
    configure_device(history_home, device_name)
    for thread_id, title, label in transcripts:
        add_transcript(
            codex_home,
            thread_id,
            title,
            timestamp="2026-07-14T01:00:00Z",
            label=label,
        )
    config = load_config(history_home, "default")
    build_full(config)
    return history_home, config


def test_device_bundle_import_federated_search_merge_and_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    home_a, config_a = _device_profile(
        tmp_path / "a",
        "Laptop A",
        [
            ("thread-shared", "Shared work", "shared-contract"),
            ("thread-a", "A-only work", "alpha-device-only"),
        ],
    )
    home_b, config_b = _device_profile(
        tmp_path / "b",
        "Desktop B",
        [
            ("thread-shared", "Shared work", "shared-contract"),
            ("thread-b", "B-only work", "beta-device-only"),
        ],
    )
    bundle_a = tmp_path / "laptop-a.zip"
    bundle_b = tmp_path / "desktop-b.zip"
    assert export_library(config_a, bundle_a)["verified"] is True
    assert export_library(config_b, bundle_b)["verified"] is True
    assert verify_bundle(bundle_a)["checked_files"] > 1

    hub = tmp_path / "hub-history"
    configure_device(hub, "History Hub")
    imported_a = import_library(
        hub,
        bundle_a,
        path_mappings=[("/workspace/example", "/mnt/workspace/example")],
    )
    imported_b = import_library(hub, bundle_b)
    assert imported_a["profile"] == "laptop-a-default"
    assert imported_b["profile"] == "desktop-b-default"
    assert imported_a["materialized_threads"] == 2
    assert imported_b["materialized_threads"] == 2
    assert imported_b["content_install"].get("hardlinked", 0) > 0
    assert import_library(hub, bundle_a)["status"] == "already_imported"

    imported_config = load_config(hub, imported_a["profile"])
    assert (
        "/workspace/example",
        "/mnt/workspace/example",
    ) in imported_config.path_mappings
    assert imported_config.source_roots[0].is_dir()
    libraries = list_libraries(hub)
    assert libraries["library_count"] == 2
    assert all(item["queryable"] for item in libraries["libraries"])

    search = federated_search(
        hub,
        "shared-contract",
        profiles=[imported_a["profile"], imported_b["profile"]],
        deep=True,
        retrieval="lexical",
    )
    assert search["profile_count"] == 2
    assert search["duplicates_collapsed"] > 0
    assert any(row["duplicate_count"] == 2 for row in search["results"])

    merged = merge_libraries(
        hub,
        [imported_a["profile"], imported_b["profile"]],
        as_name="shared-history",
        build=True,
    )
    assert merged["source_profiles_untouched"] is True
    assert merged["thread_count"] == 3
    assert merged["content_methods"]["exact"] == 3
    merged_config = load_config(hub, merged["profile"])
    merged_database = active_database(merged_config)
    assert merged_database is not None
    connection = connect(merged_database, readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 3
        assert (
            connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3
        )
    finally:
        connection.close()

    repeated = merge_libraries(
        hub,
        [imported_a["profile"], imported_b["profile"]],
        as_name="ignored-on-repeat",
        build=True,
    )
    assert repeated["profile"] == merged["profile"]
    assert repeated["changed"] is False
    assert repeated["source_digest"] == merged["source_digest"]
    assert repeated["build"]["status"] == "no_changes"

    convergence_bundle = tmp_path / "shared-history.zip"
    export_library(merged_config, convergence_bundle)
    imported_back = import_library(home_a, convergence_bundle)
    assert imported_back["status"] == "imported"
    assert list_libraries(home_a)["library_count"] == 2


def test_bundle_hash_verification_rejects_tampering(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    _home, config = _device_profile(
        tmp_path / "source",
        "Source",
        [("thread-source", "Source", "tamper-check")],
    )
    original = tmp_path / "original.zip"
    export_library(config, original)
    tampered = tmp_path / "tampered.zip"
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "data/database.sqlite3":
                payload += b"tampered"
            target.writestr(info.filename, payload)
    verification = verify_bundle(tampered)
    assert verification["passed"] is False
    assert any("mismatch" in error for error in verification["errors"])


def test_new_bundle_generation_updates_same_lineage_and_preserves_previous(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    _source_home, config = _device_profile(
        tmp_path / "source",
        "Source Device",
        [("thread-first", "First", "first-generation")],
    )
    first_bundle = tmp_path / "first.zip"
    first_export = export_library(config, first_bundle)
    target_home = tmp_path / "target"
    first_import = import_library(target_home, first_bundle)

    add_transcript(
        config.source_roots[0],
        "thread-second",
        "Second",
        timestamp="2026-07-14T02:00:00Z",
        label="second-generation",
    )
    update_incremental(config)
    second_bundle = tmp_path / "second.zip"
    second_export = export_library(config, second_bundle)
    assert second_export["library_id"] == first_export["library_id"]
    assert second_export["bundle_id"] != first_export["bundle_id"]

    second_import = import_library(target_home, second_bundle, as_name="ignored-name")
    assert second_import["status"] == "updated"
    assert second_import["profile"] == first_import["profile"]
    assert second_import["previous_version_preserved"] is True
    backups = list(
        (target_home / "backups/imports" / first_import["profile"]).iterdir()
    )
    assert len(backups) == 1
    updated_config = load_config(target_home, first_import["profile"])
    connection = connect(active_database(updated_config), readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 2
    finally:
        connection.close()


def test_canonical_baseline_and_delta_avoid_full_retransfer(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    source_home, config = _device_profile(
        tmp_path / "source",
        "Source Device",
        [("thread-delta", "Delta thread", "baseline-generation")],
    )
    baseline = tmp_path / "baseline.zip"
    baseline_export = export_library(config, baseline, artifact_mode="referenced")
    assert baseline_export["capabilities"]["incremental_sources"] is True

    target_home = tmp_path / "target"
    imported = import_library(target_home, baseline)
    imported_config = load_config(target_home, imported["profile"])
    imported_plan = plan(imported_config, mode="incremental")
    assert imported_plan["actionable_count"] == 0

    transcript = next(config.source_roots[0].rglob("rollout-thread-delta.jsonl"))
    rows = transcript_rows(
        "thread-delta",
        timestamp="2026-07-15T03:00:00Z",
        label="incremental-generation",
        image=True,
    )[1:]
    for row in rows:
        payload = row.get("payload", {})
        if isinstance(payload, dict):
            for key in ("turn_id", "call_id"):
                if key in payload:
                    payload[key] = str(payload[key]) + "-second"
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in rows
            )
        )
    update_incremental(config)

    delta = tmp_path / "generation-2.delta.zip"
    delta_export = export_delta(config, delta, base=baseline)
    assert delta_export["verified"] is True
    assert delta_export["change_counts"]["appended"] == 1
    assert delta_export["delta_bytes"] < baseline.stat().st_size
    delta_verification = verify_delta(delta)
    assert delta_verification["passed"] is True
    with zipfile.ZipFile(delta) as archive:
        delta_manifest = json.loads(archive.read("delta.json"))
    assert delta_manifest["artifact_delta"]["new_files"] == 1
    assert sum(item["role"] == "artifact_cas" for item in delta_manifest["files"]) == 1

    applied = apply_delta(target_home, delta)
    assert applied["status"] == "applied"
    assert (
        applied["target_source_generation_id"]
        == delta_export["target_source_generation_id"]
    )
    target_database = active_database(imported_config)
    connection = connect(target_database, readonly=True)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_events WHERE text LIKE '%incremental-generation%'"
            ).fetchone()[0]
            > 0
        )
    finally:
        connection.close()
    closure, _ = inspect_artifact_closure(
        imported_config, target_database, verify_hashes=True
    )
    assert closure["complete"] is True
    assert closure["indexed_files"] == 1
    assert apply_delta(target_home, delta)["status"] == "already_applied"


def test_bundle_rejects_zip_slip_paths(tmp_path: Path):
    bundle = tmp_path / "unsafe.zip"
    manifest = {
        "schema_version": "codex-history-library-bundle-v1",
        "bundle_id": "bundle-unsafe",
        "library_id": "library-unsafe",
        "files": [],
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("bundle.json", json.dumps(manifest))
        archive.writestr("../escape", "no")
    with pytest.raises(ValueError, match="Unsafe bundle path"):
        verify_bundle(bundle)


@pytest.mark.skipif(os.name == "nt", reason="inode identity is POSIX-specific")
def test_import_uses_shared_content_hardlinks(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    _home, config = _device_profile(
        tmp_path / "source",
        "Source",
        [("thread-source", "Source", "hardlink-check")],
    )
    bundle = tmp_path / "source.zip"
    export_library(config, bundle)
    target_home = tmp_path / "target"
    imported = import_library(target_home, bundle)
    target_config = load_config(target_home, imported["profile"])
    chunk = next((target_config.snapshots_dir / "chunks").rglob("*.bin"))
    blob = (
        target_home
        / "shared/blobs"
        / sha256_file_for_test(chunk)[:2]
        / sha256_file_for_test(chunk)
    )
    assert chunk.stat().st_ino == blob.stat().st_ino


def test_artifact_export_modes_are_explicit_and_closed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    codex_home = tmp_path / "source/codex"
    history_home = tmp_path / "source/history"
    codex_home.mkdir(parents=True)
    write_initial_config(history_home, profile="default", source_roots=[codex_home])
    add_transcript(
        codex_home,
        "thread-image",
        "Image evidence",
        timestamp="2026-07-14T01:00:00Z",
        label="portable-image",
        image=True,
    )
    config = load_config(history_home)
    build_full(config)

    query_only_bundle = tmp_path / "query-only.zip"
    query_only = export_library(config, query_only_bundle, artifact_mode="none")
    assert query_only["artifact_closure"]["intentional_omission"] is True
    query_verification = verify_bundle(query_only_bundle)
    assert query_verification["passed"] is True
    assert query_verification["artifact_closure"]["mode"] == "none"
    assert query_verification["artifact_closure"]["package_complete"] is False

    referenced_bundle = tmp_path / "referenced.zip"
    referenced = export_library(config, referenced_bundle, artifact_mode="referenced")
    assert referenced["artifact_closure"]["package_complete"] is True
    assert (
        referenced["history_coverage"]["latest_activity_at"]
        == "2026-07-14T01:00:00.000Z"
    )
    with zipfile.ZipFile(referenced_bundle) as archive:
        manifest = json.loads(archive.read("bundle.json"))
    assert manifest["history_coverage"]["knowledge_version_id"].startswith("coverage-")
    verification = verify_bundle(referenced_bundle)
    assert verification["passed"] is True
    assert verification["artifact_closure"]["packaged_indexed_files"] == 1
    assert verification["history_coverage"] == manifest["history_coverage"]

    imported = import_library(tmp_path / "target", referenced_bundle)
    imported_config = load_config(tmp_path / "target", imported["profile"])
    assert plan(imported_config, mode="incremental")["actionable_count"] == 0
    closure, _ = inspect_artifact_closure(
        imported_config, active_database(imported_config), verify_hashes=True
    )
    assert closure["complete"] is True
    assert closure["storage_counts"] == {"profile": 1}


def test_legacy_artifact_pack_adoption_survives_source_disconnect(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    codex_home = tmp_path / "legacy/codex"
    source_history = tmp_path / "legacy/history"
    codex_home.mkdir(parents=True)
    write_initial_config(source_history, profile="default", source_roots=[codex_home])
    add_transcript(
        codex_home,
        "thread-legacy-image",
        "Legacy image",
        timestamp="2026-07-14T01:00:00Z",
        label="legacy-image",
        image=True,
    )
    source_config = load_config(source_history)
    built = build_full(source_config)
    legacy_database = tmp_path / "legacy.sqlite3"
    legacy_database.write_bytes(Path(built["database"]).read_bytes())
    connection = sqlite3.connect(legacy_database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE threads SET source_id=NULL")
        connection.execute("DELETE FROM source_chunks")
        connection.execute("DELETE FROM source_files")
        connection.execute(
            "UPDATE metadata SET value='codex-history-v2.1.1' WHERE key='schema_version'"
        )
        connection.commit()
        artifact = connection.execute(
            "SELECT sha256,cas_relative_path FROM artifact_files"
        ).fetchone()
    finally:
        connection.close()

    pack = tmp_path / "artifact-pack"
    source_file = source_config.cas_dir / artifact["cas_relative_path"]
    legacy_relative = Path("files") / artifact["sha256"][:2] / artifact["sha256"]
    pack_file = pack / "cas" / legacy_relative
    pack_file.parent.mkdir(parents=True)
    pack_file.write_bytes(source_file.read_bytes())
    connection = connect(legacy_database)
    try:
        legacy_path = (Path("cas") / legacy_relative).as_posix()
        connection.execute(
            "UPDATE artifact_files SET cas_relative_path=?", (legacy_path,)
        )
        connection.execute(
            "UPDATE artifact_paths SET cas_relative_path=?", (legacy_path,)
        )
        connection.commit()
    finally:
        connection.close()

    target_home = tmp_path / "migrated"
    write_initial_config(target_home, profile="default", source_roots=[codex_home])
    target_config = load_config(target_home)
    from codex_history.migration import migrate_legacy_database

    migrated = migrate_legacy_database(
        target_config, legacy_database, source_artifacts=pack, artifact_mode="reference"
    )
    assert migrated["artifact_migration"]["closure"]["complete"] is True
    assert migrated["artifact_migration"]["closure"]["storage_counts"] == {
        "external": 1
    }

    bundle = tmp_path / "portable-complete.zip"
    export_library(target_config, bundle, artifact_mode="referenced")
    detached_home = tmp_path / "detached"
    imported = import_library(detached_home, bundle)
    pack.rename(tmp_path / "artifact-pack-disconnected")
    detached_config = load_config(detached_home, imported["profile"])
    closure, _ = inspect_artifact_closure(
        detached_config, active_database(detached_config), verify_hashes=True
    )
    assert closure["complete"] is True
    assert closure["storage_counts"] == {"profile": 1}


def test_referenced_export_rejects_tampered_artifact(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    codex_home = tmp_path / "source/codex"
    history_home = tmp_path / "source/history"
    codex_home.mkdir(parents=True)
    write_initial_config(history_home, profile="default", source_roots=[codex_home])
    add_transcript(
        codex_home,
        "thread-tampered-image",
        "Tampered image",
        timestamp="2026-07-14T01:00:00Z",
        label="tampered-image",
        image=True,
    )
    config = load_config(history_home)
    built = build_full(config)
    connection = connect(Path(built["database"]), readonly=True)
    try:
        relative = connection.execute(
            "SELECT cas_relative_path FROM artifact_files"
        ).fetchone()[0]
    finally:
        connection.close()
    path = config.cas_dir / relative
    payload = bytearray(path.read_bytes())
    payload[0] ^= 0xFF
    path.write_bytes(payload)

    with pytest.raises(RuntimeError, match="Artifact closure is incomplete"):
        export_library(config, tmp_path / "must-fail.zip", artifact_mode="referenced")


def sha256_file_for_test(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
