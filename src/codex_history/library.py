from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import tempfile
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Mapping

from .audit import audit_database
from .artifact_capture import (
    ARTIFACT_METADATA_SCHEMA,
    export_artifact_metadata,
)
from .artifacts import (
    artifact_export_entries,
    artifact_records,
    external_artifact_roots,
    normalize_cas_relative_path,
)
from .config import (
    ProfileConfig,
    catalog_path,
    ensure_profile_dirs,
    load_config,
    profile_names,
)
from .coverage import knowledge_coverage, source_inventory
from .pipeline import (
    active_database,
    active_info,
    apply_artifact_metadata_build,
    build_full,
    plan,
    update_incremental,
)
from .schema import connect, initialize
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_file,
    stable_id,
    utc_now,
)


CATALOG_SCHEMA = "codex-history-library-catalog-v1"
BUNDLE_SCHEMA = "codex-history-library-bundle-v1"
DELTA_SCHEMA = "codex-history-library-delta-v1"
PROFILE_SCHEMA = "codex-history-library-profile-v1"
MERGE_SCHEMA = "codex-history-library-merge-v1"


def _slug(value: str, fallback: str = "device") -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip().lower(), flags=re.UNICODE)
    value = re.sub(r"[-_.]{2,}", "-", value).strip("-_.")
    return value[:64] or fallback


def _new_device(name: str = "") -> dict[str, Any]:
    display_name = name.strip() or socket.gethostname() or platform.node() or "device"
    return {
        "device_id": f"device-{uuid.uuid4().hex}",
        "display_name": display_name,
        "slug": _slug(display_name),
        "platform": platform.platform(),
        "created_at": utc_now(),
    }


def load_catalog(home: Path, *, create: bool = False) -> dict[str, Any]:
    home = home.expanduser().resolve()
    path = catalog_path(home)
    value = read_json(path, {}) or {}
    if value and value.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError(
            f"Unsupported library catalog schema: {value.get('schema_version')}"
        )
    if not value:
        value = {
            "schema_version": CATALOG_SCHEMA,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "device": _new_device(),
            "profiles": {},
        }
        if create:
            atomic_write_json(path, value)
    return value


def save_catalog(home: Path, catalog: Mapping[str, Any]) -> None:
    value = dict(catalog)
    value["updated_at"] = utc_now()
    atomic_write_json(catalog_path(home), value)


def configure_device(home: Path, name: str = "") -> dict[str, Any]:
    catalog = load_catalog(home, create=True)
    if name:
        catalog["device"]["display_name"] = name.strip()
        catalog["device"]["slug"] = _slug(name)
        catalog["device"]["updated_at"] = utc_now()
        save_catalog(home, catalog)
    return dict(catalog["device"])


def _settings_from_config(config: ProfileConfig) -> dict[str, Any]:
    return {
        "include_archived": config.include_archived,
        "snapshot_chunk_bytes": config.snapshot_chunk_bytes,
        "summarization": {
            "mode": config.summary_mode,
            "provider": config.summary_provider,
            "model": config.summary_model,
            "endpoint": config.summary_endpoint,
            "api_key_env": config.summary_api_key_env,
            "env_file": "",
            "thinking_enabled": config.summary_thinking_enabled,
            "input_price_cny_per_million": config.summary_input_price_cny,
            "cached_input_price_cny_per_million": config.summary_cached_input_price_cny,
            "output_price_cny_per_million": config.summary_output_price_cny,
        },
        "estimation": {
            "bytes_per_token": config.estimate_bytes_per_token,
            "summary_input_ratio": config.estimate_summary_input_ratio,
            "summary_output_ratio": config.estimate_summary_output_ratio,
            "cached_input_ratio": config.estimate_cached_input_ratio,
            "sqlite_to_source_ratio": config.estimate_sqlite_to_source_ratio,
            "artifact_to_source_ratio": config.estimate_artifact_to_source_ratio,
            "semantic_to_source_ratio": config.estimate_semantic_to_source_ratio,
        },
        "embedding": {
            "enabled": config.embedding_enabled,
            "provider": config.embedding_provider,
            "model": config.embedding_model,
            "dimensions": config.embedding_dimensions,
            "endpoint": config.embedding_endpoint,
            "api_key_env": config.embedding_api_key_env,
            "env_file": "",
            "input_price_cny_per_million": config.embedding_input_price_cny,
        },
        "artifacts": {
            "capture_existing_paths": config.artifact_capture_paths,
            "max_file_bytes": config.artifact_max_file_bytes,
            "allowed_extensions": list(config.artifact_allowed_extensions),
            "excluded_roots": [str(path) for path in config.artifact_excluded_roots],
            "exclude_temporary": config.artifact_exclude_temporary,
            "capture_git_repositories": config.artifact_capture_git_repositories,
            "git_allow_network": config.artifact_git_allow_network,
            "git_capture_dirty_worktree": config.artifact_git_capture_dirty_worktree,
            "git_max_bytes": config.artifact_git_max_bytes,
            "git_command_timeout_seconds": config.artifact_git_command_timeout_seconds,
        },
        "runtime": {"python": ""},
    }


def _profile_identity(config: ProfileConfig, *, create: bool = True) -> dict[str, Any]:
    path = config.root / "library.json"
    value = read_json(path, {}) or {}
    if value:
        return value
    catalog = load_catalog(config.home, create=True)
    device = catalog["device"]
    value = {
        "schema_version": PROFILE_SCHEMA,
        "library_id": stable_id("library", device["device_id"], config.name, length=32),
        "lineage_kind": "local",
        "created_at": utc_now(),
        "origin_device_id": device["device_id"],
        "origin_device_name": device["display_name"],
        "origin_profile": config.name,
        "parent_library_ids": [],
    }
    if create:
        atomic_write_json(path, value)
    return value


def _catalog_profile(home: Path, name: str) -> dict[str, Any]:
    return dict((load_catalog(home).get("profiles", {}).get(name) or {}))


def list_libraries(home: Path) -> dict[str, Any]:
    catalog = load_catalog(home, create=True)
    entries: list[dict[str, Any]] = []
    for name in profile_names(home):
        config = load_config(home, name)
        identity = _profile_identity(config)
        catalog_item = catalog.get("profiles", {}).get(name, {})
        database = active_database(config)
        entries.append(
            {
                "profile": name,
                "display_name": catalog_item.get("display_name", name),
                "origin": catalog_item.get(
                    "origin", identity.get("lineage_kind", "local")
                ),
                "library_id": identity["library_id"],
                "origin_device_id": identity.get("origin_device_id", ""),
                "origin_device_name": identity.get("origin_device_name", ""),
                "source_profiles": catalog_item.get("source_profiles", []),
                "active_build_id": (active_info(config) or {}).get("build_id"),
                "database": str(database) if database else None,
                "queryable": bool(database),
                "source_roots": [str(path) for path in config.source_roots],
                "path_mappings": [
                    {"original_prefix": old, "local_prefix": new}
                    for old, new in config.path_mappings
                ],
                "history_coverage": (
                    knowledge_coverage(config, database, active=active_info(config))
                    if database
                    else None
                ),
            }
        )
    return {
        "schema_version": CATALOG_SCHEMA,
        "device": catalog["device"],
        "library_count": len(entries),
        "libraries": entries,
    }


def _zip_safe_name(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe bundle path: {value}")
    return path.as_posix()


def _file_record(path: Path, archive_path: str, role: str) -> dict[str, Any]:
    return {
        "path": _zip_safe_name(archive_path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "role": role,
    }


def _backup_database(source: Path, target: Path) -> None:
    source_connection = connect(source, readonly=True)
    target_connection = connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        source_connection.close()
        target_connection.close()


def _active_files(config: ProfileConfig, database: Path) -> list[tuple[Path, str, str]]:
    entries: dict[str, tuple[Path, str, str]] = {}
    connection = connect(database, readonly=True)
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_chunks'"
        ).fetchone():
            for row in connection.execute(
                "SELECT DISTINCT cas_relative_path FROM source_chunks"
            ):
                relative = str(row[0])
                path = config.snapshots_dir / relative
                if path.is_file():
                    archive = f"data/snapshots/{Path(relative).as_posix()}"
                    entries[archive] = (path, archive, "transcript_chunk")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_files'"
        ).fetchone():
            for row in connection.execute(
                "SELECT snapshot_manifest_path FROM source_files"
            ):
                path = Path(str(row[0]))
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(config.snapshots_dir).as_posix()
                except ValueError:
                    relative = f"manifests/imported/{path.name}"
                archive = f"data/snapshots/{relative}"
                entries[archive] = (path, archive, "transcript_manifest")
    finally:
        connection.close()

    for root, prefix, role in (
        (config.root / "semantic", "data/semantic", "semantic_index"),
        (config.cache_dir / "model", "data/cache/model", "model_cache"),
    ):
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    archive = f"{prefix}/{relative}"
                    entries[archive] = (path, archive, role)
    return list(entries.values())


def _artifact_inventory(database: Path) -> dict[str, Any]:
    records = [
        {
            "sha256": str(row["sha256"]),
            "size_bytes": int(row["size_bytes"]),
            "cas_relative_path": normalize_cas_relative_path(
                str(row["cas_relative_path"])
            ),
        }
        for row in artifact_records(database)
    ]
    records.sort(key=lambda item: (item["sha256"], item["cas_relative_path"]))
    digest = hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()
    return {
        "digest": digest,
        "file_count": len(records),
        "total_bytes": sum(item["size_bytes"] for item in records),
        "files": records,
    }


def _tree_inventory(root: Path, archive_prefix: str) -> dict[str, Any]:
    files = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            files.append(
                {
                    "path": f"{archive_prefix}/{path.relative_to(root).as_posix()}",
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return {
        "digest": hashlib.sha256(canonical_json(files).encode("utf-8")).hexdigest(),
        "file_count": len(files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "files": files,
    }


def export_library(
    config: ProfileConfig,
    destination: Path,
    *,
    include_semantic: bool = True,
    include_model_cache: bool = True,
    artifact_mode: str = "referenced",
) -> dict[str, Any]:
    database = active_database(config)
    if not database:
        raise RuntimeError(f"Profile {config.name!r} has no active build")
    ensure_profile_dirs(config)
    destination = destination.expanduser().resolve()
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    identity = _profile_identity(config)
    catalog = load_catalog(config.home, create=True)
    active = active_info(config) or {}
    audit = audit_database(database)
    history_coverage = knowledge_coverage(config, database, active=active)
    connection = connect(database, readonly=True)
    try:
        inventory = source_inventory(connection)
        artifact_metadata = export_artifact_metadata(connection)
    finally:
        connection.close()
    artifact_closure, artifact_entries = artifact_export_entries(
        config,
        database,
        mode=artifact_mode,
        verify_hashes=artifact_mode != "none",
    )
    artifact_inventory = _artifact_inventory(database)
    cache_inventory = _tree_inventory(config.cache_dir / "model", "data/cache/model")
    with tempfile.TemporaryDirectory(prefix="codex-history-export-") as temporary:
        temporary_root = Path(temporary)
        database_copy = temporary_root / "database.sqlite3"
        _backup_database(database, database_copy)
        entries = [(database_copy, "data/database.sqlite3", "database")]
        for path, archive, role in _active_files(config, database):
            if role == "semantic_index" and not include_semantic:
                continue
            if role == "model_cache" and not include_model_cache:
                continue
            entries.append((path, archive, role))
        entries.extend(artifact_entries)
        records = [_file_record(path, archive, role) for path, archive, role in entries]
        bundle_id = stable_id(
            "bundle",
            identity["library_id"],
            active.get("build_id"),
            audit["logical_digest"]["sha256"],
            artifact_mode,
            include_semantic,
            include_model_cache,
            length=32,
        )
        manifest = {
            "schema_version": BUNDLE_SCHEMA,
            "bundle_id": bundle_id,
            "created_at": utc_now(),
            "library_id": identity["library_id"],
            "profile_identity": identity,
            "source_device": catalog["device"],
            "source_profile": config.name,
            "source_build": active,
            "logical_digest": audit["logical_digest"]["sha256"],
            "source_generation_id": inventory["generation_id"],
            "source_inventory": inventory,
            "artifact_inventory": artifact_inventory,
            "artifact_metadata_digest": artifact_metadata["digest"],
            "cache_inventory": cache_inventory,
            "history_coverage": history_coverage,
            "profile_config": _settings_from_config(config),
            "source_roots": [str(path) for path in config.source_roots],
            "path_mappings": [
                {"original_prefix": old, "local_prefix": new}
                for old, new in config.path_mappings
            ],
            "artifact_closure": artifact_closure,
            "capabilities": {
                "query": True,
                "incremental_sources": inventory["snapshot_complete"],
                "artifacts": artifact_closure["package_complete"]
                and artifact_closure["indexed_files"] > 0,
                "artifact_metadata": artifact_closure["indexed_files"] > 0,
                "semantic": any(role == "semantic_index" for *_rest, role in entries),
                "model_cache": any(role == "model_cache" for *_rest, role in entries),
            },
            "files": records,
            "totals": {
                "file_count": len(records),
                "uncompressed_bytes": sum(record["size_bytes"] for record in records),
            },
        }
        temporary_zip = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with zipfile.ZipFile(
                temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr("bundle.json", canonical_json(manifest) + "\n")
                for path, archive_path, _role in entries:
                    archive.write(path, _zip_safe_name(archive_path))
            os.replace(temporary_zip, destination)
        finally:
            temporary_zip.unlink(missing_ok=True)
    verified = verify_bundle(destination)
    return {
        "status": "exported",
        "profile": config.name,
        "library_id": identity["library_id"],
        "bundle_id": manifest["bundle_id"],
        "bundle": str(destination),
        "bundle_bytes": destination.stat().st_size,
        "uncompressed_bytes": manifest["totals"]["uncompressed_bytes"],
        "file_count": manifest["totals"]["file_count"],
        "verified": verified["passed"],
        "capabilities": manifest["capabilities"],
        "artifact_closure": manifest["artifact_closure"],
        "history_coverage": manifest["history_coverage"],
    }


def _bundle_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("Bundle contains duplicate archive paths")
    for name in names:
        _zip_safe_name(name)
    if "bundle.json" not in names:
        raise ValueError("Bundle is missing bundle.json")
    if archive.getinfo("bundle.json").file_size > 16 * 1024 * 1024:
        raise ValueError("Bundle manifest is unexpectedly large")
    manifest = json.loads(archive.read("bundle.json"))
    if manifest.get("schema_version") != BUNDLE_SCHEMA:
        raise ValueError(f"Unsupported bundle schema: {manifest.get('schema_version')}")
    identifier = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
    for key in ("bundle_id", "library_id"):
        if not identifier.fullmatch(str(manifest.get(key) or "")):
            raise ValueError(f"Bundle has an invalid {key}")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("Bundle manifest contains no files")
    record_paths = [
        str(record.get("path") or "") for record in records if isinstance(record, dict)
    ]
    if len(record_paths) != len(records) or len(record_paths) != len(set(record_paths)):
        raise ValueError("Bundle manifest contains invalid or duplicate file records")
    return manifest


def _hash_stream(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = handle.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _verify_bundle_artifact_closure(
    archive: zipfile.ZipFile, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    records = [dict(record) for record in manifest.get("files", [])]
    database_record = next(
        (record for record in records if record.get("role") == "database"), None
    )
    if database_record is None:
        return {}, ["bundle contains no database record"], warnings
    with tempfile.TemporaryDirectory(prefix="codex-history-bundle-db-") as temporary:
        database = Path(temporary) / "database.sqlite3"
        with (
            archive.open(str(database_record["path"])) as source,
            database.open("wb") as target,
        ):
            shutil.copyfileobj(source, target, length=1024 * 1024)
        indexed = artifact_records(database)

    declared = dict(manifest.get("artifact_closure") or {})
    mode = str(declared.get("mode") or "legacy")
    if mode not in {"legacy", "none", "referenced", "all"}:
        errors.append(f"unsupported artifact export mode: {mode}")
    packaged = {
        str(record["path"]): record
        for record in records
        if record.get("role") == "artifact_cas"
    }
    indexed_bytes = sum(int(record["size_bytes"]) for record in indexed)
    indexed_digest = hashlib.sha256()
    expected_paths: set[str] = set()
    for record in indexed:
        indexed_digest.update(
            f"{record['sha256']}:{int(record['size_bytes'])}\n".encode("ascii")
        )
        expected_path = (
            f"data/cas/{normalize_cas_relative_path(str(record['cas_relative_path']))}"
        )
        expected_paths.add(expected_path)
        packaged_record = packaged.get(expected_path)
        if mode == "none":
            if packaged_record is not None:
                errors.append(
                    f"artifact mode none unexpectedly packages: {expected_path}"
                )
            continue
        if packaged_record is None:
            errors.append(f"indexed artifact missing from bundle: {expected_path}")
            continue
        if str(packaged_record.get("sha256")) != str(record["sha256"]):
            errors.append(
                f"indexed artifact sha256 disagrees with database: {expected_path}"
            )
        if int(packaged_record.get("size_bytes", -1)) != int(record["size_bytes"]):
            errors.append(
                f"indexed artifact size disagrees with database: {expected_path}"
            )

    if mode == "referenced" and set(packaged) != expected_paths:
        errors.append(
            "referenced artifact bundle contains files outside the active database"
        )
    if mode == "none" and packaged:
        errors.append("artifact mode none contains artifact CAS files")
    if mode == "legacy" and indexed:
        warnings.append("legacy bundle has no explicit artifact export policy")

    computed = {
        "mode": mode,
        "indexed_files": len(indexed),
        "indexed_bytes": indexed_bytes,
        "indexed_digest": indexed_digest.hexdigest(),
        "packaged_cas_files": len(packaged),
        "packaged_indexed_files": 0
        if mode == "none"
        else len(expected_paths & set(packaged)),
        "package_complete": mode != "none" and expected_paths.issubset(packaged),
        "intentional_omission": mode == "none" and bool(indexed),
    }
    for key in (
        "indexed_files",
        "indexed_bytes",
        "indexed_digest",
        "packaged_indexed_files",
        "packaged_cas_files",
        "package_complete",
    ):
        if key in declared and declared[key] != computed[key]:
            errors.append(
                f"artifact closure manifest mismatch for {key}: "
                f"declared {declared[key]!r}, computed {computed[key]!r}"
            )
    return computed, errors, warnings


def _verify_source_inventory(
    manifest: Mapping[str, Any],
    *,
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    inventory = dict(manifest.get("source_inventory") or {})
    errors: list[str] = []
    sources = inventory.get("sources")
    if (
        not inventory.get("snapshot_complete")
        or not isinstance(sources, list)
        or not sources
    ):
        return inventory, ["bundle has no complete canonical source inventory"]

    transcript_records = {
        str(record.get("path") or ""): record
        for record in records
        if record.get("role") == "transcript_chunk"
    }
    required_paths: set[str] = set()
    for source in sources:
        chunks = source.get("chunks") or []
        snapshot_size = int(source.get("snapshot_size_bytes") or 0)
        chunk_size = sum(int(chunk.get("size_bytes") or 0) for chunk in chunks)
        if snapshot_size != chunk_size:
            errors.append(
                f"source snapshot size mismatch: {source.get('source_id') or 'unknown'}"
            )
        for chunk in chunks:
            relative = normalize_cas_relative_path(
                str(chunk.get("cas_relative_path") or "")
            )
            archive_path = f"data/snapshots/{relative}"
            required_paths.add(archive_path)
            record = transcript_records.get(archive_path)
            if record is None:
                errors.append(
                    f"source inventory chunk missing from bundle: {archive_path}"
                )
                continue
            if str(record.get("sha256") or "") != str(chunk.get("sha256") or ""):
                errors.append(f"source inventory chunk sha256 mismatch: {archive_path}")
            if int(record.get("size_bytes") or -1) != int(chunk.get("size_bytes") or 0):
                errors.append(f"source inventory chunk size mismatch: {archive_path}")

    unexpected = set(transcript_records) - required_paths
    if unexpected:
        errors.append(
            f"bundle contains {len(unexpected)} unreferenced transcript chunks"
        )
    declared_generation = str(manifest.get("source_generation_id") or "")
    inventory_generation = _inventory_generation(inventory)
    if declared_generation and declared_generation != inventory_generation:
        errors.append("bundle source generation disagrees with source inventory")
    return inventory, errors


def verify_bundle(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0
    with zipfile.ZipFile(path, "r") as archive:
        manifest = _bundle_manifest(archive)
        available = set(archive.namelist())
        for record in manifest.get("files", []):
            name = _zip_safe_name(str(record.get("path") or ""))
            if name not in available:
                errors.append(f"missing: {name}")
                continue
            info = archive.getinfo(name)
            if info.file_size != int(record.get("size_bytes", -1)):
                errors.append(f"size mismatch: {name}")
                continue
            with archive.open(name) as handle:
                digest, size = _hash_stream(handle)
            if digest != record.get("sha256") or size != info.file_size:
                errors.append(f"sha256 mismatch: {name}")
                continue
            checked += 1
        source_inventory_value, source_errors = _verify_source_inventory(
            manifest,
            records=manifest.get("files", []),
        )
        errors.extend(source_errors)
        try:
            artifact_closure, closure_errors, closure_warnings = (
                _verify_bundle_artifact_closure(archive, manifest)
            )
            errors.extend(closure_errors)
            warnings.extend(closure_warnings)
        except (OSError, ValueError, sqlite3.DatabaseError) as error:
            artifact_closure = {}
            errors.append(f"artifact closure verification failed: {error}")
    return {
        "schema_version": BUNDLE_SCHEMA,
        "bundle": str(path),
        "bundle_id": manifest["bundle_id"],
        "library_id": manifest["library_id"],
        "passed": not errors,
        "checked_files": checked,
        "source_inventory": {
            key: source_inventory_value.get(key)
            for key in (
                "generation_id",
                "source_count",
                "thread_count",
                "snapshot_complete",
                "unique_chunk_count",
                "total_bytes",
            )
        },
        "artifact_closure": artifact_closure,
        "history_coverage": manifest.get("history_coverage"),
        "warnings": warnings,
        "errors": errors,
    }


def _transfer_manifest(path: Path) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(path.expanduser().resolve(), "r") as archive:
        names = set(archive.namelist())
        if "delta.json" in names:
            return DELTA_SCHEMA, json.loads(archive.read("delta.json"))
        if "bundle.json" in names:
            return BUNDLE_SCHEMA, _bundle_manifest(archive)
    raise ValueError("Archive contains neither bundle.json nor delta.json")


def _inventory_generation(inventory: Mapping[str, Any]) -> str:
    return str(inventory.get("generation_id") or "")


def _base_transfer(path: Path) -> dict[str, Any]:
    schema, manifest = _transfer_manifest(path)
    verification = verify_delta(path) if schema == DELTA_SCHEMA else verify_bundle(path)
    if not verification["passed"]:
        raise ValueError(
            "Base transfer failed verification: " + "; ".join(verification["errors"])
        )
    inventory = dict(manifest.get("source_inventory") or {})
    if not inventory.get("snapshot_complete") or not inventory.get("sources"):
        raise ValueError("Base transfer has no complete canonical source inventory")
    return manifest


def _source_change_kind(
    config: ProfileConfig,
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> str:
    if previous is None:
        return "added"
    if current.get("content_sha256") == previous.get("content_sha256"):
        return "unchanged"
    old_size = int(previous.get("size_bytes") or 0)
    current_size = int(current.get("size_bytes") or 0)
    path = Path(str(current.get("source_path") or ""))
    if current_size > old_size and path.is_file():
        if sha256_file(path, old_size) == str(previous.get("content_sha256") or ""):
            return "appended"
    return "rewritten"


def export_delta(
    config: ProfileConfig,
    destination: Path,
    *,
    base: Path,
    artifact_mode: str = "referenced",
    include_model_cache: bool = True,
) -> dict[str, Any]:
    database = active_database(config)
    if not database:
        raise RuntimeError(f"Profile {config.name!r} has no active build")
    identity = _profile_identity(config)
    base_manifest = _base_transfer(base)
    if str(base_manifest.get("library_id")) != str(identity["library_id"]):
        raise ValueError("Base transfer belongs to a different library lineage")
    connection = connect(database, readonly=True)
    try:
        current_inventory = source_inventory(connection)
        artifact_metadata = export_artifact_metadata(connection)
    finally:
        connection.close()
    if not current_inventory["snapshot_complete"]:
        raise RuntimeError(
            "The active profile has no complete canonical transcript snapshots; "
            "create or hydrate a canonical baseline first"
        )
    base_inventory = dict(base_manifest["source_inventory"])
    base_by_id = {
        str(item["source_id"]): item for item in base_inventory.get("sources", [])
    }
    current_by_id = {
        str(item["source_id"]): item for item in current_inventory.get("sources", [])
    }
    changes: list[dict[str, Any]] = []
    for source_id in sorted(set(base_by_id) | set(current_by_id)):
        current = current_by_id.get(source_id)
        previous = base_by_id.get(source_id)
        kind = (
            "deleted"
            if current is None
            else _source_change_kind(config, current, previous)
        )
        if kind == "unchanged":
            continue
        changes.append(
            {
                "kind": kind,
                "source_id": source_id,
                "thread_id": str((current or previous or {}).get("thread_id") or ""),
                "previous_content_sha256": (previous or {}).get("content_sha256"),
                "content_sha256": (current or {}).get("content_sha256"),
                "previous_size_bytes": int((previous or {}).get("size_bytes") or 0),
                "size_bytes": int((current or {}).get("size_bytes") or 0),
            }
        )

    base_chunks = {
        str(chunk["sha256"])
        for item in base_inventory.get("sources", [])
        for chunk in item.get("chunks", [])
    }
    entries: dict[str, tuple[Path, str, str]] = {}
    changed_ids = {item["source_id"] for item in changes if item["kind"] != "deleted"}
    for source_id in changed_ids:
        source = current_by_id[source_id]
        for chunk in source.get("chunks", []):
            if str(chunk["sha256"]) in base_chunks:
                continue
            relative = str(chunk["cas_relative_path"])
            path = config.snapshots_dir / relative
            if not path.is_file():
                raise FileNotFoundError(f"Missing transcript snapshot chunk: {path}")
            archive_path = f"data/snapshots/{Path(relative).as_posix()}"
            entries[archive_path] = (path, archive_path, "transcript_chunk")

    artifact_closure, artifact_entries = artifact_export_entries(
        config,
        database,
        mode=artifact_mode,
        verify_hashes=False,
    )
    current_artifacts = _artifact_inventory(database)
    base_artifact_inventory = dict(base_manifest.get("artifact_inventory") or {})
    base_artifacts = {
        str(item["sha256"]) for item in base_artifact_inventory.get("files", [])
    }
    artifact_sha_by_path = {
        f"data/cas/{item['cas_relative_path']}": str(item["sha256"])
        for item in current_artifacts.get("files", [])
    }
    for path, archive_path, role in artifact_entries:
        digest = artifact_sha_by_path.get(archive_path)
        if not digest:
            raise ValueError(
                f"Artifact export entry is absent from the database: {archive_path}"
            )
        if digest not in base_artifacts:
            if sha256_file(path) != digest:
                raise ValueError(f"Artifact content hash mismatch: {path}")
            entries[archive_path] = (path, archive_path, role)

    current_cache = _tree_inventory(config.cache_dir / "model", "data/cache/model")
    base_cache = {
        (str(item["path"]), str(item["sha256"]))
        for item in (base_manifest.get("cache_inventory") or {}).get("files", [])
    }
    if include_model_cache:
        for item in current_cache["files"]:
            key = (str(item["path"]), str(item["sha256"]))
            if key in base_cache:
                continue
            relative = str(item["path"]).removeprefix("data/cache/model/")
            path = config.cache_dir / "model" / relative
            entries[str(item["path"])] = (path, str(item["path"]), "model_cache")

    active = active_info(config) or {}
    audit = audit_database(database)
    history_coverage = knowledge_coverage(config, database, active=active)
    records = [_file_record(*entry) for entry in entries.values()]
    base_generation = _inventory_generation(base_inventory)
    target_generation = _inventory_generation(current_inventory)
    delta_id = stable_id(
        "delta",
        identity["library_id"],
        base_generation,
        target_generation,
        current_artifacts["digest"],
        artifact_metadata["digest"],
        artifact_mode,
        length=32,
    )
    manifest = {
        "schema_version": DELTA_SCHEMA,
        "delta_id": delta_id,
        "created_at": utc_now(),
        "library_id": identity["library_id"],
        "source_device": load_catalog(config.home, create=True)["device"],
        "source_profile": config.name,
        "base_bundle_id": base_manifest.get("bundle_id"),
        "base_delta_id": base_manifest.get("delta_id"),
        "base_source_generation_id": base_generation,
        "target_source_generation_id": target_generation,
        "base_source_inventory": base_inventory,
        "source_build": active,
        "logical_digest": audit["logical_digest"]["sha256"],
        "history_coverage": history_coverage,
        "source_inventory": current_inventory,
        "base_artifact_inventory": base_artifact_inventory,
        "artifact_inventory": current_artifacts,
        "artifact_metadata": artifact_metadata,
        "cache_inventory": current_cache,
        "target_artifact_closure": {
            key: artifact_closure.get(key)
            for key in (
                "complete",
                "hashes_verified",
                "indexed_files",
                "indexed_bytes",
                "indexed_digest",
                "available_files",
                "available_bytes",
                "missing_files",
                "registered_external_roots",
            )
        },
        "artifact_delta": {
            "mode": artifact_mode,
            "new_files": sum(record["role"] == "artifact_cas" for record in records),
            "new_bytes": sum(
                int(record["size_bytes"])
                for record in records
                if record["role"] == "artifact_cas"
            ),
            "metadata_digest": artifact_metadata["digest"],
        },
        "changes": changes,
        "capabilities": {
            "incremental_sources": True,
            "semantic_rebuild_required": config.embedding_enabled,
            "model_cache_delta": include_model_cache,
            "artifact_delta": artifact_mode != "none",
        },
        "files": records,
        "totals": {
            "file_count": len(records),
            "uncompressed_bytes": sum(item["size_bytes"] for item in records),
        },
    }
    destination = destination.expanduser().resolve()
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr("delta.json", canonical_json(manifest) + "\n")
            for path, archive_path, _role in entries.values():
                archive.write(path, _zip_safe_name(archive_path))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    verified = verify_delta(destination)
    return {
        "status": "exported",
        "delta": str(destination),
        "delta_id": delta_id,
        "library_id": identity["library_id"],
        "base_source_generation_id": base_generation,
        "target_source_generation_id": target_generation,
        "change_count": len(changes),
        "change_counts": dict(
            (kind, sum(item["kind"] == kind for item in changes))
            for kind in ("added", "appended", "rewritten", "deleted")
        ),
        "delta_bytes": destination.stat().st_size,
        "uncompressed_bytes": manifest["totals"]["uncompressed_bytes"],
        "file_count": len(records),
        "verified": verified["passed"],
        "history_coverage": history_coverage,
    }


def verify_delta(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    errors: list[str] = []
    checked = 0
    manifest: dict[str, Any] = {}
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            errors.append("delta contains duplicate archive paths")
        for name in names:
            try:
                _zip_safe_name(name)
            except ValueError as error:
                errors.append(str(error))
        if "delta.json" not in names:
            errors.append("delta is missing delta.json")
        else:
            manifest = json.loads(archive.read("delta.json"))
        if manifest.get("schema_version") != DELTA_SCHEMA:
            errors.append(f"unsupported delta schema: {manifest.get('schema_version')}")
        records = manifest.get("files") or []
        record_paths = [str(item.get("path") or "") for item in records]
        if len(record_paths) != len(set(record_paths)):
            errors.append("delta manifest contains duplicate file records")
        available = set(names)
        for record in records:
            name = str(record.get("path") or "")
            if name not in available:
                errors.append(f"missing: {name}")
                continue
            info = archive.getinfo(name)
            with archive.open(name) as handle:
                digest, size = _hash_stream(handle)
            if digest != record.get("sha256") or size != int(
                record.get("size_bytes", -1)
            ):
                errors.append(f"sha256 mismatch: {name}")
                continue
            if size != info.file_size:
                errors.append(f"size mismatch: {name}")
                continue
            checked += 1
        inventory = manifest.get("source_inventory") or {}
        if not inventory.get("snapshot_complete"):
            errors.append("delta target source inventory is incomplete")
        if not manifest.get("base_source_generation_id"):
            errors.append("delta has no base source generation")
        if _inventory_generation(inventory) != manifest.get(
            "target_source_generation_id"
        ):
            errors.append("delta target generation disagrees with source inventory")
        packaged_chunks = {
            str(record["sha256"])
            for record in records
            if record.get("role") == "transcript_chunk"
        }
        changed_ids = {
            str(item["source_id"])
            for item in manifest.get("changes", [])
            if item.get("kind") != "deleted"
        }
        base_chunks = {
            str(chunk["sha256"])
            for source in (manifest.get("base_source_inventory") or {}).get(
                "sources", []
            )
            for chunk in source.get("chunks", [])
        }
        required_chunks: set[str] = set()
        for source in inventory.get("sources", []):
            if str(source.get("source_id")) not in changed_ids:
                continue
            for chunk in source.get("chunks", []):
                digest = str(chunk["sha256"])
                if digest not in base_chunks:
                    required_chunks.add(digest)
        missing_chunks = required_chunks - packaged_chunks
        if missing_chunks:
            errors.append(
                f"delta is missing {len(missing_chunks)} required transcript chunks"
            )

        artifact_delta = dict(manifest.get("artifact_delta") or {})
        artifact_mode = str(artifact_delta.get("mode") or "")
        if artifact_mode not in {"none", "referenced", "all"}:
            errors.append(
                f"unsupported delta artifact mode: {artifact_mode or 'missing'}"
            )
        packaged_artifacts = {
            str(record.get("sha256") or "")
            for record in records
            if record.get("role") == "artifact_cas"
        }
        base_artifacts = {
            str(item.get("sha256") or "")
            for item in (manifest.get("base_artifact_inventory") or {}).get("files", [])
        }
        target_artifacts = {
            str(item.get("sha256") or "")
            for item in (manifest.get("artifact_inventory") or {}).get("files", [])
        }
        required_artifacts = target_artifacts - base_artifacts
        if artifact_mode == "none":
            if packaged_artifacts:
                errors.append("artifact mode none contains artifact CAS files")
        elif packaged_artifacts != required_artifacts:
            missing = required_artifacts - packaged_artifacts
            unexpected = packaged_artifacts - required_artifacts
            errors.append(
                "delta artifact closure mismatch: "
                f"{len(missing)} missing and {len(unexpected)} unexpected"
            )
        if int(artifact_delta.get("new_files") or 0) != len(packaged_artifacts):
            errors.append("delta artifact file count disagrees with manifest")
        artifact_metadata = dict(manifest.get("artifact_metadata") or {})
        if artifact_metadata.get("schema_version") != ARTIFACT_METADATA_SCHEMA:
            errors.append(
                "delta has unsupported artifact metadata schema: "
                f"{artifact_metadata.get('schema_version') or 'missing'}"
            )
        metadata_tables = dict(artifact_metadata.get("tables") or {})
        metadata_digest = hashlib.sha256(
            canonical_json(metadata_tables).encode("utf-8")
        ).hexdigest()
        if metadata_digest != str(artifact_metadata.get("digest") or ""):
            errors.append("delta artifact metadata digest mismatch")
        if str(artifact_delta.get("metadata_digest") or "") != metadata_digest:
            errors.append("delta artifact metadata digest disagrees with manifest")
        metadata_artifacts = {
            str(item.get("sha256") or "")
            for item in metadata_tables.get("artifact_files", [])
        }
        if metadata_artifacts != target_artifacts:
            errors.append(
                "delta artifact metadata does not match target artifact inventory"
            )
    return {
        "schema_version": DELTA_SCHEMA,
        "delta": str(path),
        "delta_id": manifest.get("delta_id"),
        "library_id": manifest.get("library_id"),
        "base_source_generation_id": manifest.get("base_source_generation_id"),
        "target_source_generation_id": manifest.get("target_source_generation_id"),
        "passed": not errors,
        "checked_files": checked,
        "errors": errors,
    }


def _available_profile_name(home: Path, preferred: str) -> str:
    preferred = _slug(preferred, "imported")
    existing = set(profile_names(home)) | {
        path.name for path in (home / "profiles").glob("*") if path.is_dir()
    }
    if preferred not in existing:
        return preferred
    index = 2
    while f"{preferred}-{index}" in existing:
        index += 1
    return f"{preferred}-{index}"


def _copy_from_zip(
    home: Path,
    archive: zipfile.ZipFile,
    record: Mapping[str, Any],
    target: Path,
    *,
    shared: bool,
) -> str:
    name = _zip_safe_name(str(record["path"]))
    expected = str(record["sha256"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if not shared:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(name) as source, temporary.open("wb") as destination:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    destination.write(block)
            if digest.hexdigest() != expected or size != int(record["size_bytes"]):
                raise ValueError(f"Bundle verification failed while importing {name}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return "copied"

    blob = home / "shared/blobs" / expected[:2] / expected
    if not blob.is_file():
        blob.parent.mkdir(parents=True, exist_ok=True)
        temporary = blob.with_name(f".{blob.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(name) as source, temporary.open("wb") as destination:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    destination.write(block)
            if digest.hexdigest() != expected or size != int(record["size_bytes"]):
                raise ValueError(f"Bundle verification failed while importing {name}")
            try:
                os.replace(temporary, blob)
            except OSError:
                if not blob.is_file():
                    raise
        finally:
            temporary.unlink(missing_ok=True)
    try:
        os.link(blob, target)
        return "hardlinked"
    except OSError:
        shutil.copy2(blob, target)
        return "copied_from_shared"


def _database_sources(root: Path, database: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    connection = connect(database, readonly=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"source_files", "source_chunks"}.issubset(tables):
            return result
        title_by_thread = {
            str(row["thread_id"]): str(row["title"])
            for row in connection.execute("SELECT thread_id,title FROM threads")
        }
        for source in connection.execute(
            "SELECT * FROM source_files WHERE source_state='active' ORDER BY thread_id,source_id"
        ):
            source_columns = set(source.keys())
            chunks = connection.execute(
                "SELECT * FROM source_chunks WHERE source_id=? ORDER BY chunk_index",
                (source["source_id"],),
            ).fetchall()
            data = b"".join(
                (root / "snapshots" / str(chunk["cas_relative_path"])).read_bytes()
                for chunk in chunks
            )
            if not data:
                old_path = Path(str(source["source_path"]))
                if old_path.is_file():
                    data = old_path.read_bytes()
            if not data:
                continue
            digest = hashlib.sha256(data).hexdigest()
            expected = str(
                source["snapshot_content_sha256"] or source["content_sha256"]
                if "snapshot_content_sha256" in source_columns
                else source["content_sha256"]
            )
            if expected and digest != expected:
                raise ValueError(
                    f"Transcript snapshot digest mismatch for {source['source_id']}"
                )
            thread_id = str(source["thread_id"])
            source_artifacts = [
                {
                    "sha256": str(item["sha256"]),
                    "size_bytes": int(item["size_bytes"]),
                    "mime_type": str(item["mime_type"]),
                    "extension": str(item["extension"]),
                    "cas_relative_path": str(item["cas_relative_path"]),
                    "artifact_uri": str(item["artifact_uri"]),
                }
                for item in connection.execute(
                    "SELECT DISTINCT f.sha256,f.size_bytes,f.mime_type,f.extension,"
                    "f.cas_relative_path,f.artifact_uri FROM artifact_paths ap "
                    "JOIN artifact_files f ON f.sha256=ap.sha256 WHERE ap.path LIKE ? "
                    "ORDER BY f.sha256",
                    (f"inline-image:{source['source_id']}:%",),
                )
            ]
            result[thread_id].append(
                {
                    "thread_id": thread_id,
                    "title": title_by_thread.get(thread_id, thread_id),
                    "source_id": str(source["source_id"]),
                    "source_path": str(source["source_path"]),
                    "relative_path": str(source["relative_path"]),
                    "adapter": str(source["adapter"]),
                    "mtime_ns": int(source["mtime_ns"]),
                    "line_count": int(source["line_count"]),
                    "source_size_bytes": int(source["size_bytes"]),
                    "source_content_sha256": str(source["content_sha256"]),
                    "snapshot_format": str(
                        source["snapshot_format"] or "normalized-jsonl-v1"
                        if "snapshot_format" in source_columns
                        else "raw-jsonl"
                    ),
                    "artifacts": source_artifacts,
                    "data": data,
                    "sha256": digest,
                }
            )
    finally:
        connection.close()
    return result


def _line_timestamp(row: Mapping[str, Any]) -> str:
    if isinstance(row.get("timestamp"), str):
        return str(row["timestamp"])
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for key in ("timestamp", "started_at", "completed_at", "created_at", "updated_at"):
        if isinstance(payload.get(key), str):
            return str(payload[key])
    return ""


def merge_transcript_variants(
    variants: Iterable[Mapping[str, Any]],
) -> tuple[bytes, dict[str, Any]]:
    values = list(variants)
    if not values:
        return b"", {"method": "empty", "variants": 0, "unique_lines": 0}
    unique_by_digest = {str(value["sha256"]): value for value in values}
    unique = sorted(
        unique_by_digest.values(),
        key=lambda value: (-len(value["data"]), str(value["sha256"])),
    )
    primary = bytes(unique[0]["data"])
    if len(unique) == 1:
        return primary, {
            "method": "exact",
            "variants": len(values),
            "unique_variants": 1,
            "unique_lines": primary.count(b"\n"),
        }
    if all(primary.startswith(bytes(value["data"])) for value in unique[1:]):
        return primary, {
            "method": "longest-prefix",
            "variants": len(values),
            "unique_variants": len(unique),
            "unique_lines": primary.count(b"\n"),
        }

    lines: dict[str, dict[str, Any]] = {}
    session_meta: dict[str, Any] | None = None
    malformed: list[bytes] = []
    for source_order, value in enumerate(unique):
        for line_order, raw in enumerate(bytes(value["data"]).splitlines()):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed.append(raw)
                continue
            if not isinstance(row, dict):
                malformed.append(raw)
                continue
            normalized = canonical_json(row).encode("utf-8")
            digest = hashlib.sha256(normalized).hexdigest()
            if row.get("type") == "session_meta":
                if session_meta is None:
                    session_meta = {
                        "raw": normalized,
                        "timestamp": _line_timestamp(row),
                        "source_order": source_order,
                        "line_order": line_order,
                    }
                continue
            lines.setdefault(
                digest,
                {
                    "raw": normalized,
                    "timestamp": _line_timestamp(row),
                    "source_order": source_order,
                    "line_order": line_order,
                },
            )
    ordered = sorted(
        lines.values(),
        key=lambda item: (
            0 if item["timestamp"] else 1,
            item["timestamp"],
            item["source_order"],
            item["line_order"],
        ),
    )
    output: list[bytes] = []
    if session_meta:
        output.append(session_meta["raw"])
    output.extend(item["raw"] for item in ordered)
    output.extend(dict.fromkeys(malformed))
    return b"\n".join(output) + b"\n", {
        "method": "event-union",
        "variants": len(values),
        "unique_variants": len(unique),
        "unique_lines": len(output),
        "malformed_lines": len(set(malformed)),
    }


def _materialize_sources(
    root: Path, database: Path, source_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    variants = _database_sources(root, database)
    session_root = source_root / "sessions/imported"
    session_root.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    mappings: list[dict[str, str]] = []
    index_by_thread: dict[str, dict[str, str]] = {}
    identity_rows: list[dict[str, Any]] = []
    for thread_id, thread_variants in sorted(variants.items()):
        for variant in thread_variants:
            data = bytes(variant["data"])
            digest = hashlib.sha256(data).hexdigest()
            source_id = str(variant["source_id"])
            target = session_root / f"{_slug(source_id, 'source')}.jsonl"
            atomic_write_bytes(target, data)
            try:
                os.utime(
                    target, ns=(int(variant["mtime_ns"]), int(variant["mtime_ns"]))
                )
            except OSError:
                pass
            relative = target.relative_to(source_root).as_posix()
            title = str(variant["title"])
            index_by_thread[thread_id] = {
                "id": thread_id,
                "thread_name": title,
                "updated_at": utc_now(),
            }
            identity_rows.append(
                {
                    "source_id": source_id,
                    "thread_id": thread_id,
                    "title": title,
                    "relative_path": relative,
                    "adapter": str(variant["adapter"]),
                    "content_sha256": digest,
                    "size_bytes": len(data),
                    "mtime_ns": int(variant["mtime_ns"]),
                    "source_size_bytes": int(variant["source_size_bytes"]),
                    "source_content_sha256": str(variant["source_content_sha256"]),
                    "snapshot_format": "normalized-jsonl-v1",
                    "artifacts": list(variant.get("artifacts") or []),
                }
            )
            mappings.append(
                {
                    "original_prefix": str(variant["source_path"]),
                    "local_prefix": str(target),
                    "mapping_kind": "exact-source",
                }
            )
            reports.append(
                {
                    "thread_id": thread_id,
                    "source_id": source_id,
                    "title": title,
                    "target": str(target),
                    "sha256": digest,
                    "method": "exact-source-snapshot",
                    "unique_lines": data.count(b"\n"),
                }
            )
    index_path = source_root / "session_index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "".join(
            canonical_json(row) + "\n"
            for row in sorted(index_by_thread.values(), key=lambda item: item["id"])
        ),
        encoding="utf-8",
    )
    (source_root / "source_identity.jsonl").write_text(
        "".join(
            canonical_json(row) + "\n"
            for row in sorted(identity_rows, key=lambda item: item["source_id"])
        ),
        encoding="utf-8",
    )
    return reports, mappings


def _write_path_mappings(
    database: Path, mappings: Iterable[Mapping[str, str]], device_id: str
) -> None:
    connection = connect(database)
    try:
        initialize(connection)
        for mapping in mappings:
            original = str(mapping.get("original_prefix") or "")
            local = str(mapping.get("local_prefix") or "")
            if not original or not local:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO path_mappings(mapping_id,original_prefix,local_prefix,"
                "mapping_kind,source_device_id,created_at) VALUES(?,?,?,?,?,?)",
                (
                    stable_id("path-map", device_id, original, local),
                    original,
                    local,
                    str(mapping.get("mapping_kind") or "prefix"),
                    device_id,
                    utc_now(),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _target_for_bundle_path(staging_root: Path, name: str, build_id: str) -> Path:
    relative = PurePosixPath(name)
    if name == "data/database.sqlite3":
        return staging_root / "builds" / build_id / "codex_history.sqlite3"
    prefixes = {
        "data/snapshots/": "snapshots",
        "data/cas/": "cas",
        "data/semantic/": "semantic",
        "data/cache/": "cache",
    }
    for prefix, target in prefixes.items():
        if name.startswith(prefix):
            suffix = PurePosixPath(name.removeprefix(prefix))
            return staging_root / target / Path(*suffix.parts)
    raise ValueError(f"Unsupported bundle data path: {relative}")


def import_library(
    home: Path,
    bundle: Path,
    *,
    as_name: str = "",
    path_mappings: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    bundle = bundle.expanduser().resolve()
    catalog = load_catalog(home, create=True)
    verification = verify_bundle(bundle)
    if not verification["passed"]:
        raise ValueError(
            "Bundle failed verification: " + "; ".join(verification["errors"])
        )
    backup_root: Path | None = None
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = _bundle_manifest(archive)
        library_id = str(manifest["library_id"])
        existing_name = next(
            (
                name
                for name, item in catalog.get("profiles", {}).items()
                if item.get("library_id") == library_id and item.get("enabled", True)
            ),
            None,
        )
        same_bundle = bool(
            existing_name
            and catalog["profiles"][existing_name].get("bundle_id")
            == manifest["bundle_id"]
        )
        if same_bundle:
            return {
                "status": "already_imported",
                "profile": existing_name,
                "library_id": library_id,
                "bundle_id": manifest["bundle_id"],
                "verified": True,
            }
        source_device = manifest.get("source_device", {})
        preferred = (
            as_name
            or f"{source_device.get('slug') or _slug(str(source_device.get('display_name') or 'device'))}-{manifest.get('source_profile', 'history')}"
        )
        updating = existing_name is not None
        profile_name = existing_name or _available_profile_name(home, preferred)
        final_root = home / "profiles" / profile_name
        staging_root = home / "profiles" / f".import-{uuid.uuid4().hex}"
        build_id = f"import-{str(manifest['bundle_id']).removeprefix('bundle-')[:20]}"
        database: Path | None = None
        stats = defaultdict(int)
        try:
            for record in manifest.get("files", []):
                name = str(record["path"])
                target = _target_for_bundle_path(staging_root, name, build_id)
                method = _copy_from_zip(
                    home,
                    archive,
                    record,
                    target,
                    shared=record.get("role") != "database",
                )
                stats[method] += 1
                if record.get("role") == "database":
                    database = target
            if database is None or not database.is_file():
                raise ValueError("Bundle contains no database")

            active_payload = {
                "schema_version": "codex-history-active-v1",
                "profile": profile_name,
                "build_id": build_id,
                "database": f"builds/{build_id}/codex_history.sqlite3",
                "promoted_at": utc_now(),
                "incremental_ready": False,
                "imported_bundle_id": manifest["bundle_id"],
                "artifact_mode": str(
                    (manifest.get("artifact_closure") or {}).get("mode") or "legacy"
                ),
                "artifact_package_complete": bool(
                    verification.get("artifact_closure", {}).get("package_complete")
                ),
                "history_coverage": manifest.get("history_coverage"),
                "source_generation_id": manifest.get("source_generation_id"),
            }
            source_slug = _slug(str(source_device.get("slug") or "device"))
            staging_source_root = staging_root / "imported_sources" / source_slug
            final_source_root = final_root / "imported_sources" / source_slug
            source_reports, automatic_mappings = _materialize_sources(
                staging_root, database, staging_source_root
            )
            expected_sources = int(
                (manifest.get("source_inventory") or {}).get("source_count") or 0
            )
            active_payload["incremental_ready"] = bool(
                (manifest.get("capabilities") or {}).get("incremental_sources")
                and expected_sources
                and len(source_reports) == expected_sources
            )
            atomic_write_json(staging_root / "active.json", active_payload)
            mappings = [*manifest.get("path_mappings", []), *automatic_mappings]
            for old_root in manifest.get("source_roots", []):
                mappings.append(
                    {
                        "original_prefix": str(old_root),
                        "local_prefix": str(final_source_root),
                        "mapping_kind": "source-root",
                    }
                )
            mappings.extend(
                {
                    "original_prefix": old,
                    "local_prefix": new,
                    "mapping_kind": "user-prefix",
                }
                for old, new in path_mappings
                if old and new
            )
            for mapping in mappings:
                local = str(mapping.get("local_prefix") or "")
                if local.startswith(str(staging_root)):
                    mapping["local_prefix"] = (
                        str(final_root) + local[len(str(staging_root)) :]
                    )
            deduplicated_mappings = list(
                {
                    (str(item["original_prefix"]), str(item["local_prefix"])): item
                    for item in mappings
                    if item.get("original_prefix") and item.get("local_prefix")
                }.values()
            )
            _write_path_mappings(
                database,
                deduplicated_mappings,
                str(source_device.get("device_id") or ""),
            )
            identity = dict(manifest.get("profile_identity") or {})
            identity.update(
                {
                    "schema_version": PROFILE_SCHEMA,
                    "library_id": library_id,
                    "imported_at": utc_now(),
                    "last_bundle_id": manifest["bundle_id"],
                }
            )
            atomic_write_json(staging_root / "library.json", identity)
            audit = audit_database(database)
            if not audit["passed"]:
                raise ValueError("Imported database failed integrity audit")

            if updating and final_root.exists():
                backup_root = (
                    home
                    / "backups/imports"
                    / profile_name
                    / str(
                        catalog["profiles"][profile_name].get("bundle_id") or utc_now()
                    ).replace(":", "-")
                )
                backup_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_root, backup_root)
            elif final_root.exists():
                raise FileExistsError(final_root)
            try:
                os.replace(staging_root, final_root)
            except BaseException:
                if (
                    backup_root is not None
                    and backup_root.exists()
                    and not final_root.exists()
                ):
                    os.replace(backup_root, final_root)
                raise
        except BaseException:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
    database = final_root / "builds" / build_id / "codex_history.sqlite3"
    source_root = final_root / "imported_sources" / source_slug
    config_item = dict(manifest.get("profile_config") or {})
    config_item["source_roots"] = [str(source_root)]
    config_item["path_mappings"] = deduplicated_mappings
    catalog.setdefault("profiles", {})[profile_name] = {
        "enabled": True,
        "origin": "imported",
        "display_name": f"{source_device.get('display_name') or 'Device'} / {manifest.get('source_profile')}",
        "library_id": library_id,
        "bundle_id": manifest["bundle_id"],
        "source_device_id": source_device.get("device_id", ""),
        "source_device_name": source_device.get("display_name", ""),
        "source_profile": manifest.get("source_profile", ""),
        "imported_at": utc_now(),
        "config": config_item,
    }
    save_catalog(home, catalog)
    return {
        "status": "updated" if updating else "imported",
        "profile": profile_name,
        "library_id": library_id,
        "bundle_id": manifest["bundle_id"],
        "verified": verification["passed"],
        "audit_passed": True,
        "materialized_threads": len({item["thread_id"] for item in source_reports}),
        "materialized_sources": len(source_reports),
        "path_mapping_count": len(deduplicated_mappings),
        "content_install": dict(stats),
        "artifact_closure": verification.get("artifact_closure", {}),
        "history_coverage": manifest.get("history_coverage", {}),
        "shared_blob_root": str(home / "shared/blobs"),
        "previous_version_preserved": updating,
    }


def _read_source_identities(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    path = root / "source_identity.jsonl"
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("source_id") and row.get("relative_path"):
            result[str(row["source_id"])] = row
    return result


def _write_inventory_sources(
    config: ProfileConfig,
    inventory: Mapping[str, Any],
    changes: Iterable[Mapping[str, Any]],
) -> list[Path]:
    if not config.source_roots:
        raise RuntimeError("Imported profile has no materialized source root")
    root = config.source_roots[0].expanduser().resolve()
    profile_root = config.root.resolve()
    if root != profile_root and profile_root not in root.parents:
        raise RuntimeError(
            "Delta application is restricted to imported, profile-managed source roots"
        )
    root.mkdir(parents=True, exist_ok=True)
    existing = _read_source_identities(root)
    target_sources = {
        str(item["source_id"]): dict(item) for item in inventory.get("sources", [])
    }
    changed = {str(item["source_id"]): str(item["kind"]) for item in changes}
    touched: list[Path] = []
    for source_id, kind in changed.items():
        old = existing.get(source_id)
        if kind == "deleted":
            if old:
                path = root / str(old["relative_path"])
                path.unlink(missing_ok=True)
                touched.append(path)
            continue
        source = target_sources[source_id]
        relative = (
            str(old["relative_path"])
            if old
            else f"sessions/imported/{_slug(source_id, 'source')}.jsonl"
        )
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                for chunk in source.get("chunks", []):
                    chunk_path = config.snapshots_dir / str(chunk["cas_relative_path"])
                    if not chunk_path.is_file():
                        raise FileNotFoundError(
                            f"Missing base or delta transcript chunk: {chunk_path}"
                        )
                    with chunk_path.open("rb") as handle:
                        while True:
                            block = handle.read(1024 * 1024)
                            if not block:
                                break
                            output.write(block)
                            digest.update(block)
                            size += len(block)
                output.flush()
                os.fsync(output.fileno())
            if size != int(source["snapshot_size_bytes"]) or digest.hexdigest() != str(
                source["snapshot_content_sha256"]
            ):
                raise ValueError(f"Reconstructed transcript mismatch for {source_id}")
            os.replace(temporary, target)
            try:
                mtime = int(source["mtime_ns"])
                os.utime(target, ns=(mtime, mtime))
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        touched.append(target)

    identity_rows: list[dict[str, Any]] = []
    index_by_thread: dict[str, dict[str, str]] = {}
    for source_id, source in sorted(target_sources.items()):
        old = existing.get(source_id)
        relative = (
            str(old["relative_path"])
            if old
            else f"sessions/imported/{_slug(source_id, 'source')}.jsonl"
        )
        identity_rows.append(
            {
                "source_id": source_id,
                "thread_id": str(source["thread_id"]),
                "title": str(source.get("title") or source["thread_id"]),
                "relative_path": relative,
                "adapter": str(source.get("adapter") or "codex-jsonl-v1"),
                "content_sha256": str(source["content_sha256"]),
                "size_bytes": int(source["snapshot_size_bytes"]),
                "mtime_ns": int(source["mtime_ns"]),
                "source_size_bytes": int(source["size_bytes"]),
                "source_content_sha256": str(source["content_sha256"]),
                "snapshot_format": "normalized-jsonl-v1",
                "artifacts": list(source.get("artifacts") or []),
            }
        )
        index_by_thread[str(source["thread_id"])] = {
            "id": str(source["thread_id"]),
            "thread_name": str(source.get("title") or source["thread_id"]),
            "updated_at": utc_now(),
        }
    atomic_write_bytes(
        root / "source_identity.jsonl",
        "".join(canonical_json(row) + "\n" for row in identity_rows).encode("utf-8"),
    )
    atomic_write_bytes(
        root / "session_index.jsonl",
        "".join(
            canonical_json(row) + "\n"
            for row in sorted(index_by_thread.values(), key=lambda item: item["id"])
        ).encode("utf-8"),
    )
    return touched


def _delta_target(config: ProfileConfig, archive_path: str) -> Path:
    if archive_path.startswith("data/snapshots/"):
        return config.snapshots_dir / archive_path.removeprefix("data/snapshots/")
    if archive_path.startswith("data/cas/"):
        return config.cas_dir / archive_path.removeprefix("data/cas/")
    if archive_path.startswith("data/cache/model/"):
        return (
            config.cache_dir / "model" / archive_path.removeprefix("data/cache/model/")
        )
    raise ValueError(f"Unsupported delta data path: {archive_path}")


def apply_delta(
    home: Path,
    delta: Path,
    *,
    profile_name: str = "",
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    delta = delta.expanduser().resolve()
    verification = verify_delta(delta)
    if not verification["passed"]:
        raise ValueError(
            "Delta failed verification: " + "; ".join(verification["errors"])
        )
    with zipfile.ZipFile(delta, "r") as archive:
        manifest = json.loads(archive.read("delta.json"))
        library_id = str(manifest["library_id"])
        catalog = load_catalog(home, create=True)
        candidates = [
            name
            for name, item in catalog.get("profiles", {}).items()
            if item.get("library_id") == library_id and item.get("enabled", True)
        ]
        if profile_name:
            if profile_name not in candidates:
                raise ValueError(
                    "Selected profile does not match the delta library lineage"
                )
            selected = profile_name
        elif len(candidates) == 1:
            selected = candidates[0]
        elif not candidates:
            raise ValueError(
                "Import the canonical baseline bundle before applying this delta"
            )
        else:
            raise ValueError(
                "Multiple matching profiles exist; select one with --profile"
            )
        config = load_config(home, selected)
        active = active_info(config) or {}
        if not active.get("incremental_ready"):
            raise RuntimeError("Target profile is not a canonical incremental baseline")
        database = active_database(config)
        if not database:
            raise RuntimeError("Target profile has no active database")
        connection = connect(database, readonly=True)
        try:
            current_inventory = source_inventory(connection)
            current_artifact_metadata = export_artifact_metadata(connection)
        finally:
            connection.close()
        current_artifact_inventory = _artifact_inventory(database)
        current_generation = _inventory_generation(current_inventory)
        required_generation = str(manifest["base_source_generation_id"])
        target_generation = str(manifest["target_source_generation_id"])
        target_artifact_inventory = dict(manifest.get("artifact_inventory") or {})
        target_artifact_digest = str(target_artifact_inventory.get("digest") or "")
        target_artifact_metadata = dict(manifest.get("artifact_metadata") or {})
        target_artifact_metadata_digest = str(
            target_artifact_metadata.get("digest") or ""
        )
        artifact_mode = str(
            (manifest.get("artifact_delta") or {}).get("mode") or "referenced"
        )
        artifacts_required = artifact_mode != "none"
        source_up_to_date = current_generation == target_generation
        artifacts_up_to_date = (
            not artifacts_required
            or (
                current_artifact_inventory["digest"] == target_artifact_digest
                and current_artifact_metadata["digest"]
                == target_artifact_metadata_digest
            )
        )
        if source_up_to_date and artifacts_up_to_date:
            return {
                "status": "already_applied",
                "profile": selected,
                "library_id": library_id,
                "delta_id": manifest["delta_id"],
                "target_source_generation_id": current_generation,
                "changes": 0,
            }
        if current_generation not in {required_generation, target_generation}:
            raise RuntimeError(
                "Delta base generation mismatch: target is "
                f"{current_generation}, delta requires {required_generation}"
            )

        install_stats = defaultdict(int)
        for record in manifest.get("files", []):
            archive_path = str(record["path"])
            target = _delta_target(config, archive_path)
            if target.is_file() and sha256_file(target) == str(record["sha256"]):
                install_stats["reused"] += 1
                continue
            method = _copy_from_zip(home, archive, record, target, shared=True)
            install_stats[method] += 1

    source_build: dict[str, Any] | None = None
    artifact_build: dict[str, Any] | None = None
    if not source_up_to_date:
        source_root = config.source_roots[0].resolve()
        backup_root = config.root / "backups/deltas" / str(manifest["delta_id"])
        backup_root.mkdir(parents=True, exist_ok=True)
        identities = _read_source_identities(source_root)
        restore: list[tuple[Path, Path | None]] = []
        for change in manifest.get("changes", []):
            old = identities.get(str(change["source_id"]))
            target = (
                source_root / str(old["relative_path"])
                if old
                else source_root
                / f"sessions/imported/{_slug(str(change['source_id']), 'source')}.jsonl"
            )
            if target.is_file():
                backup = backup_root / target.relative_to(source_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(target, backup)
                except OSError:
                    shutil.copy2(target, backup)
                restore.append((target, backup))
            else:
                restore.append((target, None))
        for name in ("source_identity.jsonl", "session_index.jsonl"):
            target = source_root / name
            if target.is_file():
                backup = backup_root / name
                shutil.copy2(target, backup)
                restore.append((target, backup))
        try:
            _write_inventory_sources(
                config, manifest["source_inventory"], manifest["changes"]
            )
            source_build = update_incremental(config, max_cost_cny=max_cost_cny)
        except BaseException:
            for target, backup in reversed(restore):
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            raise
        else:
            shutil.rmtree(backup_root, ignore_errors=True)

    updated_database = active_database(config)
    if not updated_database:
        raise RuntimeError("Delta application did not leave an active database")
    connection = connect(updated_database, readonly=True)
    try:
        metadata_after_source = export_artifact_metadata(connection)
    finally:
        connection.close()
    artifacts_after_source = _artifact_inventory(updated_database)
    if artifacts_required and (
        metadata_after_source["digest"] != target_artifact_metadata_digest
        or artifacts_after_source["digest"] != target_artifact_digest
    ):
        artifact_build = apply_artifact_metadata_build(
            config, target_artifact_metadata
        )
        updated_database = active_database(config)
        if not updated_database:
            raise RuntimeError("Artifact metadata update did not promote a database")

    connection = connect(updated_database, readonly=True)
    try:
        updated_inventory = source_inventory(connection)
        updated_artifact_metadata = export_artifact_metadata(connection)
    finally:
        connection.close()
    updated_artifact_inventory = _artifact_inventory(updated_database)
    if _inventory_generation(updated_inventory) != target_generation:
        raise RuntimeError(
            "Applied delta did not converge to its target source generation"
        )
    if (
        artifacts_required
        and updated_artifact_inventory["digest"] != target_artifact_digest
    ):
        raise RuntimeError(
            "Applied delta did not converge to its target artifact inventory"
        )
    if (
        artifacts_required
        and updated_artifact_metadata["digest"] != target_artifact_metadata_digest
    ):
        raise RuntimeError(
            "Applied delta did not converge to its target artifact metadata"
        )
    active_payload = active_info(config) or {}
    active_payload.update(
        {
            "incremental_ready": True,
            "source_generation_id": target_generation,
            "last_delta_id": manifest["delta_id"],
            "history_coverage": knowledge_coverage(
                config, updated_database, active=active_payload
            ),
        }
    )
    if artifacts_required:
        active_payload.update(
            {
                "artifact_inventory_digest": target_artifact_digest,
                "artifact_metadata_digest": target_artifact_metadata_digest,
            }
        )
    atomic_write_json(config.active_path, active_payload)
    catalog["profiles"][selected]["source_generation_id"] = target_generation
    catalog["profiles"][selected]["last_delta_id"] = manifest["delta_id"]
    if artifacts_required:
        catalog["profiles"][selected]["artifact_inventory_digest"] = (
            target_artifact_digest
        )
        catalog["profiles"][selected]["artifact_metadata_digest"] = (
            target_artifact_metadata_digest
        )
    save_catalog(home, catalog)
    return {
        "status": "applied",
        "profile": selected,
        "library_id": library_id,
        "delta_id": manifest["delta_id"],
        "base_source_generation_id": required_generation,
        "target_source_generation_id": target_generation,
        "changes": len(manifest.get("changes", [])),
        "content_install": dict(install_stats),
        "build": artifact_build or source_build,
        "source_build": source_build,
        "artifact_build": artifact_build,
        "history_coverage": active_payload["history_coverage"],
    }


def _knowledge_key(row: Mapping[str, Any]) -> str:
    text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip().casefold()
    return hashlib.sha256(
        "\x1f".join(
            (
                text,
                str(row.get("tier") or ""),
                str(row.get("asset_type") or ""),
                str(row.get("status_group") or ""),
            )
        ).encode("utf-8")
    ).hexdigest()


def federated_search(
    home: Path,
    query_text: str,
    *,
    profiles: Iterable[str] = (),
    limit: int = 10,
    deep: bool = False,
    retrieval: str = "hybrid",
    query_mode: str = "any",
    since: str = "",
    until: str = "",
    time_match: str = "overlaps",
    as_of: str = "",
) -> dict[str, Any]:
    from . import query as query_module

    selected = list(dict.fromkeys(profiles)) or profile_names(home)
    grouped: dict[str, dict[str, Any]] = {}
    searched: list[str] = []
    warnings: list[str] = []
    for profile_name in selected:
        config = load_config(home, profile_name)
        database = active_database(config)
        if not database:
            warnings.append(f"{profile_name}: no active build")
            continue
        searched.append(profile_name)
        previous_chroma = query_module.DEFAULT_CHROMA
        previous_model = query_module.SEMANTIC_MODEL
        previous_dimensions = query_module.SEMANTIC_DIMENSIONS
        previous_mappings = query_module.PATH_MAPPINGS
        embedding_environment = (
            "CODEX_HISTORY_EMBEDDING_ENDPOINT",
            "CODEX_HISTORY_EMBEDDING_API_KEY_ENV",
            "CODEX_HISTORY_EMBEDDING_MODEL",
            "CODEX_HISTORY_EMBEDDING_DIMENSIONS",
            "CODEX_HISTORY_EMBEDDING_INPUT_PRICE_CNY",
            "CODEX_HISTORY_EMBEDDING_ENV_FILE",
        )
        previous_environment = {
            key: os.environ.get(key) for key in embedding_environment
        }
        try:
            query_module.DEFAULT_CHROMA = config.root / "semantic/chroma"
            query_module.SEMANTIC_MODEL = config.embedding_model
            query_module.SEMANTIC_DIMENSIONS = config.embedding_dimensions
            query_module.PATH_MAPPINGS = [
                {"original_prefix": old, "local_prefix": new}
                for old, new in config.path_mappings
            ]
            os.environ["CODEX_HISTORY_EMBEDDING_ENDPOINT"] = config.embedding_endpoint
            os.environ["CODEX_HISTORY_EMBEDDING_API_KEY_ENV"] = (
                config.embedding_api_key_env
            )
            os.environ["CODEX_HISTORY_EMBEDDING_MODEL"] = config.embedding_model
            os.environ["CODEX_HISTORY_EMBEDDING_DIMENSIONS"] = str(
                config.embedding_dimensions
            )
            os.environ["CODEX_HISTORY_EMBEDDING_INPUT_PRICE_CNY"] = str(
                config.embedding_input_price_cny
            )
            os.environ["CODEX_HISTORY_EMBEDDING_ENV_FILE"] = config.embedding_env_file
            effective_retrieval = retrieval if config.embedding_enabled else "lexical"
            connection = query_module.connect(database)
            try:
                rows = query_module.search_records(
                    connection,
                    query_text,
                    limit=max(limit * 4, 40),
                    tiers=query_module.ALL_TIERS if deep else query_module.HIGH_TIERS,
                    retrieval=effective_retrieval,
                    query_mode=query_mode,
                    since=since,
                    until=until,
                    time_match=time_match,
                    as_of=as_of,
                )
            finally:
                connection.close()
        finally:
            query_module.DEFAULT_CHROMA = previous_chroma
            query_module.SEMANTIC_MODEL = previous_model
            query_module.SEMANTIC_DIMENSIONS = previous_dimensions
            query_module.PATH_MAPPINGS = previous_mappings
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        identity = _profile_identity(config)
        for row in rows:
            key = _knowledge_key(row)
            source = {
                "profile": profile_name,
                "library_id": identity["library_id"],
                "record_id": row["record_id"],
                "scope_id": row["scope_id"],
                "source_path": row.get("source_path", ""),
                "retrieval_score": row.get("retrieval_score", 0.0),
            }
            current = grouped.get(key)
            if current is None:
                current = dict(row)
                current["content_key"] = key
                current["library_matches"] = [source]
                current["duplicate_count"] = 1
                grouped[key] = current
            else:
                current["library_matches"].append(source)
                current["duplicate_count"] += 1
                if float(row.get("retrieval_score", 0.0)) > float(
                    current.get("retrieval_score", 0.0)
                ):
                    preserved = current["library_matches"]
                    duplicate_count = current["duplicate_count"]
                    current.clear()
                    current.update(row)
                    current["content_key"] = key
                    current["library_matches"] = preserved
                    current["duplicate_count"] = duplicate_count
    results = sorted(
        grouped.values(),
        key=lambda row: (
            -float(row.get("retrieval_score", 0.0)),
            -int(row.get("duplicate_count", 1)),
            str(row.get("record_id", "")),
        ),
    )[:limit]
    return {
        "schema_version": "codex-history-federated-search-v1",
        "query": query_text,
        "profiles": searched,
        "profile_count": len(searched),
        "result_count": len(results),
        "duplicates_collapsed": sum(int(row["duplicate_count"]) - 1 for row in results),
        "results": results,
        "warnings": warnings,
    }


def _install_local_file_dedup(home: Path, source: Path, target: Path) -> str:
    digest = sha256_file(source)
    blob = home / "shared/blobs" / digest[:2] / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        shutil.copy2(source, blob)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return "exists"
    try:
        os.link(blob, target)
        return "hardlinked"
    except OSError:
        shutil.copy2(blob, target)
        return "copied"


def _merged_identity(source_identities: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    identities = list(source_identities)
    parent_ids = sorted({str(item["library_id"]) for item in identities})
    return {
        "schema_version": PROFILE_SCHEMA,
        "library_id": stable_id("library-merge", parent_ids, length=32),
        "lineage_kind": "merged",
        "created_at": utc_now(),
        "origin_device_id": "",
        "origin_device_name": "multi-device merge",
        "origin_profile": "",
        "parent_library_ids": parent_ids,
    }


def merge_libraries(
    home: Path,
    source_profiles: Iterable[str],
    *,
    as_name: str = "merged-history",
    build: bool = False,
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    sources = list(dict.fromkeys(source_profiles))
    if len(sources) < 2:
        raise ValueError("At least two source profiles are required for a merge")
    configs = [load_config(home, name) for name in sources]
    for config in configs:
        if not active_database(config):
            raise RuntimeError(f"Profile {config.name!r} has no active build")
    identities = [_profile_identity(config) for config in configs]
    identity = _merged_identity(identities)
    catalog = load_catalog(home, create=True)
    existing_name = next(
        (
            name
            for name, item in catalog.get("profiles", {}).items()
            if item.get("library_id") == identity["library_id"]
            and item.get("enabled", True)
        ),
        None,
    )
    profile_name = existing_name or _available_profile_name(home, as_name)
    profile_root = home / "profiles" / profile_name
    profile_root.mkdir(parents=True, exist_ok=True)
    run_id = (
        f"merge-{utc_now().replace(':', '').replace('+', '-')}-{uuid.uuid4().hex[:8]}"
    )
    staging_sources = profile_root / "merged_sources" / f".{run_id}"
    current_sources = profile_root / "merged_sources/current"
    history_sources = profile_root / "merged_sources/history" / run_id
    all_variants: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_details: list[dict[str, Any]] = []
    for config, source_identity in zip(configs, identities):
        database = active_database(config)
        assert database is not None
        variants = _database_sources(config.root, database)
        if not variants:
            raise RuntimeError(
                f"Profile {config.name!r} has no reconstructable transcript snapshots; "
                "keep it as a federated query authority instead of merging it"
            )
        for thread_id, rows in variants.items():
            for row in rows:
                row = dict(row)
                row["profile"] = config.name
                row["library_id"] = source_identity["library_id"]
                all_variants[thread_id].append(row)
        source_details.append(
            {
                "profile": config.name,
                "library_id": source_identity["library_id"],
                "thread_count": len(variants),
            }
        )
    session_root = staging_sources / "sessions/imported"
    session_root.mkdir(parents=True, exist_ok=True)
    merge_reports: list[dict[str, Any]] = []
    index_rows: list[dict[str, str]] = []
    for thread_id, variants in sorted(all_variants.items()):
        data, report = merge_transcript_variants(variants)
        digest = hashlib.sha256(data).hexdigest()
        target = session_root / f"rollout-{_slug(thread_id, 'thread')}.jsonl"
        atomic_write_bytes(target, data)
        title = str(variants[0]["title"])
        index_rows.append(
            {"id": thread_id, "thread_name": title, "updated_at": utc_now()}
        )
        merge_reports.append(
            {
                "thread_id": thread_id,
                "title": title,
                "sha256": digest,
                "source_profiles": sorted({str(row["profile"]) for row in variants}),
                **report,
            }
        )
    (staging_sources / "session_index.jsonl").write_text(
        "".join(canonical_json(row) + "\n" for row in index_rows), encoding="utf-8"
    )
    source_digest = hashlib.sha256(
        canonical_json(
            [(item["thread_id"], item["sha256"]) for item in merge_reports]
        ).encode("utf-8")
    ).hexdigest()
    previous_manifest = read_json(profile_root / "merge.json", {}) or {}
    changed = previous_manifest.get("source_digest") != source_digest
    if changed:
        if current_sources.exists():
            history_sources.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current_sources, history_sources)
        os.replace(staging_sources, current_sources)
    else:
        shutil.rmtree(staging_sources, ignore_errors=True)

    # CAS and model response caches are immutable and therefore safe to deduplicate physically.
    shared_stats = defaultdict(int)
    for config in configs:
        content_roots = [
            *(
                (root, profile_root / "cas")
                for root in (config.cas_dir, *external_artifact_roots(config))
            ),
            (config.cache_dir / "model", profile_root / "cache/model"),
        ]
        for source_root, target_root in content_roots:
            if not source_root.is_dir():
                continue
            for path in source_root.rglob("*"):
                if path.is_file():
                    target = target_root / path.relative_to(source_root)
                    shared_stats[_install_local_file_dedup(home, path, target)] += 1

    settings = _settings_from_config(configs[0])
    settings["source_roots"] = [str(current_sources)]
    inherited_mappings = [
        {"original_prefix": old, "local_prefix": new, "mapping_kind": "inherited"}
        for config in configs
        for old, new in config.path_mappings
    ]
    settings["path_mappings"] = inherited_mappings
    catalog.setdefault("profiles", {})[profile_name] = {
        "enabled": True,
        "origin": "merged",
        "display_name": profile_name,
        "library_id": identity["library_id"],
        "source_profiles": sources,
        "parent_library_ids": identity["parent_library_ids"],
        "merged_at": utc_now(),
        "config": settings,
    }
    save_catalog(home, catalog)
    atomic_write_json(profile_root / "library.json", identity)
    merge_manifest = {
        "schema_version": MERGE_SCHEMA,
        "merge_id": run_id,
        "created_at": utc_now(),
        "profile": profile_name,
        "library_id": identity["library_id"],
        "source_profiles": source_details,
        "source_digest": source_digest,
        "changed": changed,
        "thread_count": len(merge_reports),
        "content_methods": dict(
            sorted(
                (
                    method,
                    sum(report["method"] == method for report in merge_reports),
                )
                for method in {report["method"] for report in merge_reports}
            )
        ),
        "threads": merge_reports,
        "shared_content": dict(shared_stats),
    }
    if previous_manifest.get("merge_id"):
        atomic_write_json(
            profile_root
            / "reports/merges"
            / f"{str(previous_manifest['merge_id']).replace(':', '-')}.json",
            previous_manifest,
        )
    atomic_write_json(profile_root / "merge.json", merge_manifest)
    merged_config = load_config(home, profile_name)
    ensure_profile_dirs(merged_config)
    build_result: dict[str, Any] | None = None
    if build:
        build_result = (
            update_incremental(merged_config, max_cost_cny=max_cost_cny)
            if active_database(merged_config)
            else build_full(merged_config, max_cost_cny=max_cost_cny)
        )
        database = active_database(merged_config)
        if database:
            _write_path_mappings(database, inherited_mappings, "merged")
    build_plan = plan(
        merged_config, mode="incremental" if active_database(merged_config) else "full"
    )
    return {
        "status": "complete",
        "profile": profile_name,
        "library_id": identity["library_id"],
        "source_profiles": sources,
        "changed": changed,
        "thread_count": len(merge_reports),
        "content_methods": merge_manifest["content_methods"],
        "source_digest": source_digest,
        "build": build_result,
        "plan": build_plan,
        "merge_manifest": str(profile_root / "merge.json"),
        "source_profiles_untouched": True,
    }


def sync_libraries(
    home: Path,
    source_profiles: Iterable[str],
    destination: Path,
    *,
    as_name: str = "shared-history",
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    merge = merge_libraries(
        home,
        source_profiles,
        as_name=as_name,
        build=True,
        max_cost_cny=max_cost_cny,
    )
    config = load_config(home, str(merge["profile"]))
    exported = export_library(config, destination)
    return {
        "status": "complete",
        "merge": merge,
        "bundle": exported,
        "convergence": (
            "Import this same bundle on every participating device. The stable library_id "
            "updates the prior imported generation while preserving it under backups/imports."
        ),
    }
