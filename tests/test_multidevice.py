from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from codex_history.config import load_config, write_initial_config
from codex_history.library import (
    configure_device,
    export_library,
    federated_search,
    import_library,
    list_libraries,
    merge_libraries,
    verify_bundle,
)
from codex_history.pipeline import active_database, build_full, update_incremental
from codex_history.schema import connect

from conftest import add_transcript


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
    assert ("/workspace/example", "/mnt/workspace/example") in imported_config.path_mappings
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
        assert connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3
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
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
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
    backups = list((target_home / "backups/imports" / first_import["profile"]).iterdir())
    assert len(backups) == 1
    updated_config = load_config(target_home, first_import["profile"])
    connection = connect(active_database(updated_config), readonly=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 2
    finally:
        connection.close()


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
    blob = target_home / "shared/blobs" / sha256_file_for_test(chunk)[:2] / sha256_file_for_test(chunk)
    assert chunk.stat().st_ino == blob.stat().st_ino


def sha256_file_for_test(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
