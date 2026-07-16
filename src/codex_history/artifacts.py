from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .config import ProfileConfig, ensure_profile_dirs
from .schema import connect
from .util import (
    atomic_write_json,
    file_lock,
    read_json,
    sha256_file,
    stable_id,
    utc_now,
)


ARTIFACT_SOURCES_SCHEMA = "codex-history-artifact-sources-v1"


@dataclass(frozen=True)
class ArtifactResolution:
    sha256: str
    size_bytes: int
    cas_relative_path: str
    local_relative_path: str
    path: Path | None
    storage: str
    error: str = ""


def artifact_sources_path(config: ProfileConfig) -> Path:
    return config.root / "artifact-sources.json"


def normalize_cas_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe artifact CAS path: {value}")
    parts = path.parts[1:] if path.parts[0] == "cas" else path.parts
    if not parts:
        raise ValueError(f"Artifact CAS path has no file component: {value}")
    return PurePosixPath(*parts).as_posix()


def load_artifact_sources(config: ProfileConfig) -> list[dict[str, Any]]:
    value = read_json(artifact_sources_path(config), {}) or {}
    if not value:
        return []
    if value.get("schema_version") != ARTIFACT_SOURCES_SCHEMA:
        raise ValueError(f"Unsupported artifact source registry: {value.get('schema_version')}")
    sources = value.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("Artifact source registry contains an invalid sources list")
    return [dict(item) for item in sources if isinstance(item, dict)]


def external_artifact_roots(config: ProfileConfig) -> list[Path]:
    roots: list[Path] = []
    for item in load_artifact_sources(config):
        if item.get("access_mode") != "reference":
            continue
        value = str(item.get("cas_root") or "")
        if value:
            roots.append(Path(value).expanduser())
    return roots


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def artifact_records(database: Path) -> list[dict[str, Any]]:
    connection = connect(database, readonly=True)
    try:
        if not _table_exists(connection, "artifact_files"):
            return []
        return [
            dict(row)
            for row in connection.execute(
                "SELECT sha256,size_bytes,cas_relative_path,artifact_uri,mime_type,extension FROM artifact_files ORDER BY sha256"
            )
        ]
    finally:
        connection.close()


def _candidate_roots(
    config: ProfileConfig, extra_roots: Iterable[Path] = ()
) -> list[tuple[str, Path]]:
    values: list[tuple[str, Path]] = [("profile", config.cas_dir)]
    values.extend(("external", path.expanduser()) for path in external_artifact_roots(config))
    values.extend(("candidate", path.expanduser()) for path in extra_roots)
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for storage, path in values:
        key = os.path.normcase(str(path.absolute()))
        if key not in seen:
            seen.add(key)
            result.append((storage, path))
    return result


def _resolve_record(
    config: ProfileConfig,
    record: Mapping[str, Any],
    *,
    extra_roots: Iterable[Path] = (),
    verify_hash: bool = False,
    roots_override: Iterable[tuple[str, Path]] | None = None,
) -> ArtifactResolution:
    digest = str(record["sha256"])
    expected_size = int(record["size_bytes"])
    raw_relative = str(record["cas_relative_path"])
    try:
        relative = normalize_cas_relative_path(raw_relative)
    except ValueError as error:
        return ArtifactResolution(
            digest, expected_size, raw_relative, "", None, "missing", str(error)
        )
    size_error = ""
    hash_error = ""
    roots = (
        list(roots_override)
        if roots_override is not None
        else _candidate_roots(config, extra_roots)
    )
    for storage, root in roots:
        path = root / Path(relative)
        if not path.is_file():
            continue
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            size_error = f"expected {expected_size} bytes, found {actual_size}"
            continue
        if verify_hash:
            actual_digest = sha256_file(path)
            if actual_digest != digest:
                hash_error = f"expected sha256 {digest}, found {actual_digest}"
                continue
        return ArtifactResolution(
            digest,
            expected_size,
            raw_relative,
            relative,
            path,
            storage,
        )
    error = hash_error or size_error or "file not found"
    return ArtifactResolution(digest, expected_size, raw_relative, relative, None, "missing", error)


def inspect_artifact_closure(
    config: ProfileConfig,
    database: Path,
    *,
    verify_hashes: bool = False,
    extra_roots: Iterable[Path] = (),
) -> tuple[dict[str, Any], list[ArtifactResolution]]:
    records = artifact_records(database)
    resolutions = [
        _resolve_record(
            config,
            record,
            extra_roots=extra_roots,
            verify_hash=verify_hashes,
        )
        for record in records
    ]
    available = [item for item in resolutions if item.path is not None]
    missing = [item for item in resolutions if item.path is None]
    storage_counts: dict[str, int] = {}
    for item in available:
        storage_counts[item.storage] = storage_counts.get(item.storage, 0) + 1
    digest = hashlib.sha256()
    for record in records:
        digest.update(f"{record['sha256']}:{int(record['size_bytes'])}\n".encode("ascii"))
    report = {
        "schema_version": "codex-history-artifact-closure-v1",
        "created_at": utc_now(),
        "indexed_files": len(records),
        "indexed_bytes": sum(int(record["size_bytes"]) for record in records),
        "available_files": len(available),
        "available_bytes": sum(item.size_bytes for item in available),
        "missing_files": len(missing),
        "complete": not missing,
        "hashes_verified": verify_hashes,
        "indexed_digest": digest.hexdigest(),
        "storage_counts": storage_counts,
        "registered_external_roots": [str(path) for path in external_artifact_roots(config)],
        "problems": [
            {
                "sha256": item.sha256,
                "cas_relative_path": item.cas_relative_path,
                "error": item.error,
            }
            for item in missing[:50]
        ],
        "problems_truncated": max(0, len(missing) - 50),
    }
    return report, resolutions


def _detect_cas_root(source: Path, records: list[dict[str, Any]]) -> Path:
    source = source.expanduser().resolve()
    candidates = [source]
    if (source / "cas").is_dir():
        candidates.insert(0, source / "cas")
    samples = records[: min(25, len(records))]
    for candidate in candidates:
        if all(
            (candidate / normalize_cas_relative_path(str(record["cas_relative_path"]))).is_file()
            for record in samples
        ):
            return candidate
    raise FileNotFoundError(
        f"No artifact CAS matching the active database was found under {source}"
    )


def _write_registry_entry(
    config: ProfileConfig,
    *,
    source: Path,
    cas_root: Path,
    mode: str,
    closure: Mapping[str, Any],
) -> Path:
    path = artifact_sources_path(config)
    value = read_json(path, {}) or {
        "schema_version": ARTIFACT_SOURCES_SCHEMA,
        "created_at": utc_now(),
        "sources": [],
    }
    sources = [
        dict(item)
        for item in value.get("sources", [])
        if isinstance(item, dict) and str(item.get("source") or "") != str(source)
    ]
    sources.append(
        {
            "source_id": stable_id("artifact-source", str(source), length=24),
            "source": str(source),
            "cas_root": str(cas_root),
            "access_mode": mode,
            "adopted_at": utc_now(),
            "indexed_files": int(closure["indexed_files"]),
            "indexed_bytes": int(closure["indexed_bytes"]),
            "indexed_digest": str(closure["indexed_digest"]),
        }
    )
    value.update(
        {
            "schema_version": ARTIFACT_SOURCES_SCHEMA,
            "updated_at": utc_now(),
            "sources": sources,
        }
    )
    atomic_write_json(path, value)
    return path


def _materialize_file(source: Path, target: Path, mode: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        return "already_present"
    if mode == "hardlink":
        os.link(source, target)
        return "hardlinked"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if mode == "auto":
            temporary.unlink()
            try:
                os.link(source, temporary)
                os.replace(temporary, target)
                return "hardlinked"
            except OSError:
                temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        return "copied"
    finally:
        temporary.unlink(missing_ok=True)


def _adopt_artifacts_unlocked(
    config: ProfileConfig,
    database: Path,
    source: Path,
    *,
    mode: str,
) -> dict[str, Any]:
    if mode not in {"reference", "copy", "hardlink", "auto"}:
        raise ValueError(f"Unsupported artifact adoption mode: {mode}")
    ensure_profile_dirs(config)
    records = artifact_records(database)
    if not records:
        return {
            "status": "no_artifacts",
            "mode": mode,
            "indexed_files": 0,
            "indexed_bytes": 0,
        }
    source = source.expanduser().resolve()
    cas_root = _detect_cas_root(source, records)
    candidate_resolutions = [
        _resolve_record(
            config,
            record,
            verify_hash=True,
            roots_override=(("candidate", cas_root),),
        )
        for record in records
    ]
    if any(item.path is None for item in candidate_resolutions):
        missing = sum(item.path is None for item in candidate_resolutions)
        raise ValueError(
            f"Artifact source failed closure verification: {missing} files unavailable"
        )

    source_report = {
        "indexed_files": len(records),
        "indexed_bytes": sum(int(record["size_bytes"]) for record in records),
        "indexed_digest": hashlib.sha256(
            "".join(
                f"{record['sha256']}:{int(record['size_bytes'])}\n" for record in records
            ).encode("ascii")
        ).hexdigest(),
    }

    methods: dict[str, int] = {}
    if mode != "reference":
        for item in candidate_resolutions:
            assert item.path is not None
            target = config.cas_dir / Path(item.local_relative_path)
            if target.is_file() and (
                target.stat().st_size != item.size_bytes or sha256_file(target) != item.sha256
            ):
                target.unlink()
            method = _materialize_file(item.path, target, mode)
            methods[method] = methods.get(method, 0) + 1

    registry = _write_registry_entry(
        config,
        source=source,
        cas_root=cas_root,
        mode="reference" if mode == "reference" else mode,
        closure=source_report,
    )
    final_report, _ = inspect_artifact_closure(config, database, verify_hashes=mode != "reference")
    if mode == "reference":
        final_report["hashes_verified"] = True
        final_report["verification_basis"] = "registered source verified before attachment"
    if not final_report["complete"]:
        raise RuntimeError("Adopted artifact CAS did not close the active database")
    return {
        "status": "adopted",
        "mode": mode,
        "source": str(source),
        "cas_root": str(cas_root),
        "registry": str(registry),
        "materialization": methods,
        "closure": final_report,
    }


def adopt_artifacts(
    config: ProfileConfig,
    database: Path,
    source: Path,
    *,
    mode: str = "reference",
    acquire_lock: bool = True,
) -> dict[str, Any]:
    if acquire_lock:
        with file_lock(config.lock_path):
            return _adopt_artifacts_unlocked(config, database, source, mode=mode)
    return _adopt_artifacts_unlocked(config, database, source, mode=mode)


def artifact_export_entries(
    config: ProfileConfig,
    database: Path,
    *,
    mode: str,
    verify_hashes: bool = True,
) -> tuple[dict[str, Any], list[tuple[Path, str, str]]]:
    if mode not in {"none", "referenced", "all"}:
        raise ValueError(f"Unsupported artifact export mode: {mode}")
    closure, resolutions = inspect_artifact_closure(config, database, verify_hashes=verify_hashes)
    entries: dict[str, tuple[Path, str, str]] = {}
    if mode != "none":
        if not closure["complete"]:
            raise RuntimeError(
                "Artifact closure is incomplete; run `library artifact-audit` and `library adopt-artifacts` before exporting artifacts"
            )
        for item in resolutions:
            assert item.path is not None
            archive = f"data/cas/{item.local_relative_path}"
            entries[archive] = (item.path, archive, "artifact_cas")
    if mode == "all":
        for root in (config.cas_dir, *external_artifact_roots(config)):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                archive = f"data/cas/{relative}"
                entries.setdefault(archive, (path, archive, "artifact_cas"))
    package_complete = mode != "none" and closure["complete"]
    package = {
        **closure,
        "mode": mode,
        "packaged_indexed_files": closure["indexed_files"] if package_complete else 0,
        "packaged_indexed_bytes": closure["indexed_bytes"] if package_complete else 0,
        "package_complete": package_complete,
        "packaged_cas_files": len(entries),
        "intentional_omission": mode == "none" and closure["indexed_files"] > 0,
    }
    return package, list(entries.values())
