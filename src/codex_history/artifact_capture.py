from __future__ import annotations

import gzip
import hashlib
import io
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifacts import external_artifact_roots
from .config import ProfileConfig
from .util import canonical_json, stable_id, utc_now


QUOTED_PATH_RE = re.compile(r'''["']((?:[A-Za-z]:[\\/]|/)[^"'\r\n]+)["']''')
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(/[^\s\"'<>|]+)")
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\s\"'<>|]+)")

ARTIFACT_METADATA_SCHEMA = "codex-history-artifact-metadata-v1"
ARTIFACT_METADATA_TABLES: dict[str, tuple[str, ...]] = {
    "artifact_files": (
        "sha256",
        "size_bytes",
        "size_human",
        "cas_relative_path",
        "artifact_uri",
        "mime_type",
        "extension",
        "source_open_path",
        "tiers",
        "keep_reasons",
        "categories",
        "path_count",
        "transcript_occurrences_mapped",
    ),
    "artifact_paths": (
        "path_key",
        "path",
        "sha256",
        "artifact_uri",
        "cas_relative_path",
        "size_bytes",
        "tier",
        "keep_reason",
        "category",
        "source_open_path",
    ),
    "repository_checkpoints": (
        "checkpoint_id",
        "repository_root",
        "head_commit",
        "branch",
        "refs_sha256",
        "worktree_sha256",
        "capture_mode",
        "history_artifact_sha256",
        "worktree_artifact_sha256",
        "is_dirty",
        "is_partial_clone",
        "captured_at",
        "metadata_json",
    ),
    "artifact_observations": (
        "observation_id",
        "event_id",
        "source_id",
        "thread_id",
        "artifact_sha256",
        "repository_checkpoint_id",
        "original_path",
        "resolved_path",
        "occurrence_at",
        "captured_at",
        "capture_method",
        "metadata_json",
    ),
    "ledger_artifacts": (
        "ledger_artifact_id",
        "scope_id",
        "ref",
        "role",
        "evidence_refs_json",
        "source_path",
        "source_locator",
    ),
}


@dataclass(frozen=True)
class ArtifactReference:
    event_id: str
    source_id: str
    thread_id: str
    line_no: int
    occurred_at: str
    raw_path: str


@dataclass
class FileCaptureCandidate:
    path: Path
    resolved_path: Path
    size_bytes: int
    mtime_ns: int
    extension: str
    sha256: str
    storage_action: str
    source_paths: list[Path] = field(default_factory=list)
    references: list[ArtifactReference] = field(default_factory=list)
    pending_references: list[ArtifactReference] = field(default_factory=list)


@dataclass
class RepositoryCaptureCandidate:
    root: Path
    head_commit: str
    branch: str
    refs_sha256: str
    worktree_sha256: str
    capture_mode: str
    is_dirty: bool
    is_partial_clone: bool
    history_estimated_bytes: int
    worktree_estimated_bytes: int
    references: list[ArtifactReference] = field(default_factory=list)
    pending_references: list[ArtifactReference] = field(default_factory=list)
    existing_checkpoint_id: str = ""
    existing_history_sha256: str = ""
    existing_worktree_sha256: str = ""

    @property
    def fingerprint(self) -> str:
        return stable_id(
            "repository-checkpoint",
            str(self.root),
            self.refs_sha256,
            self.worktree_sha256,
            self.capture_mode,
            length=40,
        )


@dataclass
class ArtifactCapturePlan:
    profile: str
    active_build_id: str
    database: Path
    created_at: str
    since: str
    path_capture_enabled: bool
    git_capture_enabled: bool
    files: list[FileCaptureCandidate]
    repositories: list[RepositoryCaptureCandidate]
    git_errors: list[dict[str, str]]
    scan_counts: dict[str, int]
    excluded_counts: dict[str, int]
    existing_artifact_hashes: set[str]

    @property
    def work_required(self) -> bool:
        return any(item.pending_references for item in self.files) or any(
            (not item.existing_checkpoint_id) or item.pending_references
            for item in self.repositories
        )

    def public(self) -> dict[str, Any]:
        new_files = [item for item in self.files if item.storage_action == "copy"]
        reused_files = [item for item in self.files if item.storage_action == "reuse"]
        repository_new = [
            item for item in self.repositories if not item.existing_checkpoint_id
        ]
        repository_reused = [
            item for item in self.repositories if item.existing_checkpoint_id
        ]
        return {
            "schema_version": "codex-history-artifact-plan-v1",
            "created_at": self.created_at,
            "profile": self.profile,
            "active_build_id": self.active_build_id,
            "since": self.since,
            "path_capture_enabled": self.path_capture_enabled,
            "git_capture_enabled": self.git_capture_enabled,
            "work_required": self.work_required,
            "cost_cny": 0.0,
            "model_calls": 0,
            "embedding_calls": 0,
            "scan": dict(self.scan_counts),
            "excluded": dict(sorted(self.excluded_counts.items())),
            "ordinary_files": {
                "eligible": len(self.files),
                "new_content_files": len(new_files),
                "new_content_bytes": sum(item.size_bytes for item in new_files),
                "reused_content_files": len(reused_files),
                "reused_content_bytes": sum(item.size_bytes for item in reused_files),
                "pending_observations": sum(
                    len(item.pending_references) for item in self.files
                ),
                "files": [
                    {
                        "path": str(item.resolved_path),
                        "paths": [str(path) for path in item.source_paths],
                        "extension": item.extension,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "storage_action": item.storage_action,
                        "reference_count": len(item.references),
                        "pending_observations": len(item.pending_references),
                    }
                    for item in sorted(
                        self.files, key=lambda candidate: str(candidate.resolved_path)
                    )
                ],
            },
            "git_repositories": {
                "eligible": len(self.repositories),
                "new_checkpoints": len(repository_new),
                "reused_checkpoints": len(repository_reused),
                "history_estimated_bytes": sum(
                    item.history_estimated_bytes for item in repository_new
                ),
                "worktree_estimated_bytes": sum(
                    item.worktree_estimated_bytes for item in repository_new
                ),
                "repositories": [
                    {
                        "root": str(item.root),
                        "head_commit": item.head_commit,
                        "branch": item.branch,
                        "capture_mode": item.capture_mode,
                        "is_dirty": item.is_dirty,
                        "is_partial_clone": item.is_partial_clone,
                        "history_estimated_bytes": item.history_estimated_bytes,
                        "worktree_estimated_bytes": item.worktree_estimated_bytes,
                        "reference_count": len(item.references),
                        "pending_observations": len(item.pending_references),
                        "checkpoint_action": (
                            "reuse" if item.existing_checkpoint_id else "create"
                        ),
                    }
                    for item in sorted(
                        self.repositories, key=lambda candidate: str(candidate.root)
                    )
                ],
                "errors": list(self.git_errors),
            },
            "safety": {
                "allowed_extensions": sorted(
                    {item.extension for item in self.files}
                ),
                "content_addressed_deduplication": True,
                "self_ingestion_roots_excluded": True,
                "partial_clone_network_allowed": any(
                    item.is_partial_clone and item.capture_mode.startswith("bundle")
                    for item in self.repositories
                ),
            },
        }


def candidate_paths(text: str) -> list[str]:
    values: list[str] = []
    values.extend(match.group(1) for match in QUOTED_PATH_RE.finditer(text))
    values.extend(match.group(1) for match in POSIX_PATH_RE.finditer(text))
    values.extend(match.group(1) for match in WINDOWS_PATH_RE.finditer(text))
    cleaned: list[str] = []
    for value in values:
        candidate = value.rstrip("`.,;:!?)]}")
        if not candidate or "\x00" in candidate:
            continue
        if candidate.startswith("//") or "://" in candidate:
            continue
        if candidate not in cleaned:
            cleaned.append(candidate)
    return cleaned


def local_path(value: str) -> Path:
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
        drive = value[0].lower()
        mount = Path(f"/mnt/{drive}")
        if os.environ.get("WSL_DISTRO_NAME") or mount.is_dir():
            rest = value[2:].replace("\\", "/").lstrip("/")
            return mount / rest
    return Path(value).expanduser()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_under(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _is_codex_storage(path: Path) -> bool:
    return any(part.casefold() == ".codex" for part in path.parts)


def _excluded_roots(config: ProfileConfig) -> tuple[Path, ...]:
    roots = [
        config.home,
        config.root,
        *config.source_roots,
        *config.artifact_excluded_roots,
        *external_artifact_roots(config),
    ]
    if config.artifact_exclude_temporary:
        roots.append(Path(tempfile.gettempdir()))
    result: list[Path] = []
    for root in roots:
        resolved = _resolved(root.expanduser())
        if resolved and resolved not in result:
            result.append(resolved)
    return tuple(result)


def _sha256_file(path: Path) -> tuple[str, int, int]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"Artifact changed while hashing: {path}")
    return digest.hexdigest(), after.st_size, after.st_mtime_ns


def _find_git_root(path: Path) -> Path | None:
    try:
        current = path if path.is_dir() else path.parent
    except OSError:
        return None
    while current != current.parent:
        try:
            if (current / ".git").exists():
                return _resolved(current)
        except OSError:
            return None
        current = current.parent
    return None


def _filesystem_identity(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"path:{os.path.normcase(str(path))}"
    return f"inode:{stat.st_dev}:{stat.st_ino}"


def _git(
    config: ProfileConfig,
    root: Path,
    *args: str,
    allow_network: bool | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    environment = os.environ.copy()
    network = config.artifact_git_allow_network if allow_network is None else allow_network
    if not network:
        environment["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=text,
        env=environment,
        timeout=config.artifact_git_command_timeout_seconds,
    )


def _git_text(config: ProfileConfig, root: Path, *args: str) -> str:
    return str(_git(config, root, *args).stdout).strip()


def _git_partial_clone(config: ProfileConfig, root: Path) -> bool:
    try:
        value = _git_text(
            config,
            root,
            "config",
            "--local",
            "--get-regexp",
            r"remote\..*\.(promisor|partialclonefilter)|extensions\.partialclone",
        )
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    return bool(value)


def _git_status(config: ProfileConfig, root: Path) -> bytes:
    return bytes(
        _git(
            config,
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            text=False,
        ).stdout
    )


def _git_worktree_files(config: ProfileConfig, root: Path) -> list[Path]:
    output = bytes(
        _git(
            config,
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            text=False,
        ).stdout
    )
    return sorted(
        {
            Path(value.decode("utf-8", "surrogateescape"))
            for value in output.split(b"\0")
            if value
        },
        key=lambda path: path.as_posix(),
    )


def _git_worktree_fingerprint(
    config: ProfileConfig, root: Path, *, head_commit: str, status: bytes
) -> tuple[str, int]:
    if not status:
        return hashlib.sha256(f"clean:{head_commit}".encode()).hexdigest(), 0
    digest = hashlib.sha256()
    digest.update(status)
    total = 0
    for relative in _git_worktree_files(config, root):
        path = root / relative
        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
        try:
            stat = path.lstat()
        except OSError:
            digest.update(b"\0missing\n")
            continue
        digest.update(f"\0{stat.st_mode}:{stat.st_size}\n".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
        elif path.is_file():
            total += stat.st_size
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest(), total


def _git_tracked_bytes(config: ProfileConfig, root: Path) -> int:
    total = 0
    for relative in _git_worktree_files(config, root):
        path = root / relative
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _git_history_estimate(config: ProfileConfig, root: Path, mode: str) -> int:
    if mode.startswith("head-archive"):
        return _git_tracked_bytes(config, root)
    output = _git_text(config, root, "count-objects", "-v")
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            try:
                values[key.strip()] = int(value.strip())
            except ValueError:
                continue
    return (values.get("size", 0) + values.get("size-pack", 0)) * 1024


def _observation_exists(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    original_path: str,
    capture_method: str,
) -> bool:
    if not _table_exists(connection, "artifact_observations"):
        return False
    return bool(
        connection.execute(
            """
            SELECT 1 FROM artifact_observations
            WHERE event_id=? AND original_path=? AND capture_method=?
            """,
            (event_id, original_path, capture_method),
        ).fetchone()
    )


def _scope_for_thread(
    connection: sqlite3.Connection,
    thread_id: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT scopes.scope_id
        FROM scope_threads
        JOIN scopes ON scopes.scope_id=scope_threads.scope_id
        WHERE scope_threads.thread_id=?
        ORDER BY CASE scopes.scope_type
                   WHEN 'thread' THEN 0
                   WHEN 'family' THEN 1
                   ELSE 2
                 END,
                 scope_threads.ordinal,
                 scopes.scope_id
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _repository_candidate(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    root: Path,
    references: list[ArtifactReference],
) -> RepositoryCaptureCandidate:
    head_commit = _git_text(config, root, "rev-parse", "HEAD")
    branch = _git_text(config, root, "branch", "--show-current")
    partial = _git_partial_clone(config, root)
    status = _git_status(config, root)
    dirty = bool(status)
    refs = _git_text(
        config,
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )
    refs_sha256 = hashlib.sha256(
        f"{head_commit}\n{refs}\n".encode("utf-8", "surrogateescape")
    ).hexdigest()
    worktree_sha256, dirty_bytes = _git_worktree_fingerprint(
        config, root, head_commit=head_commit, status=status
    )
    base_mode = (
        "head-archive"
        if partial and not config.artifact_git_allow_network
        else "bundle-all"
    )
    capture_mode = (
        f"{base_mode}+worktree"
        if dirty and config.artifact_git_capture_dirty_worktree
        else base_mode
    )
    existing: sqlite3.Row | None = None
    if _table_exists(connection, "repository_checkpoints"):
        existing = connection.execute(
            """
            SELECT checkpoint_id,history_artifact_sha256,worktree_artifact_sha256
            FROM repository_checkpoints
            WHERE repository_root=? AND refs_sha256=? AND worktree_sha256=?
              AND capture_mode=?
            """,
            (str(root), refs_sha256, worktree_sha256, capture_mode),
        ).fetchone()
    pending = [
        reference
        for reference in references
        if not _observation_exists(
            connection,
            event_id=reference.event_id,
            original_path=reference.raw_path,
            capture_method="git_checkpoint",
        )
    ]
    return RepositoryCaptureCandidate(
        root=root,
        head_commit=head_commit,
        branch=branch,
        refs_sha256=refs_sha256,
        worktree_sha256=worktree_sha256,
        capture_mode=capture_mode,
        is_dirty=dirty,
        is_partial_clone=partial,
        history_estimated_bytes=_git_history_estimate(config, root, base_mode),
        worktree_estimated_bytes=dirty_bytes if "+worktree" in capture_mode else 0,
        references=references,
        pending_references=pending,
        existing_checkpoint_id=str(existing["checkpoint_id"]) if existing else "",
        existing_history_sha256=(
            str(existing["history_artifact_sha256"] or "") if existing else ""
        ),
        existing_worktree_sha256=(
            str(existing["worktree_artifact_sha256"] or "") if existing else ""
        ),
    )


def _deduplicate_references(
    references: Iterable[ArtifactReference],
) -> list[ArtifactReference]:
    values: dict[tuple[str, str], ArtifactReference] = {}
    for reference in references:
        values.setdefault((reference.event_id, reference.raw_path), reference)
    return sorted(values.values(), key=lambda item: (item.event_id, item.raw_path))


def plan_artifact_capture(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    *,
    active_build_id: str,
    database: Path,
    since: str = "",
) -> ArtifactCapturePlan:
    excluded_roots = _excluded_roots(config)
    allowed = set(config.artifact_allowed_extensions)
    known_hashes = {
        str(row[0]) for row in connection.execute("SELECT sha256 FROM artifact_files")
    }
    scan_counts = {
        "events": 0,
        "path_occurrences": 0,
        "unique_raw_paths": 0,
        "existing_files": 0,
        "existing_directories": 0,
    }
    excluded_counts: dict[str, int] = {}
    raw_references: dict[str, list[ArtifactReference]] = {}
    query = (
        "SELECT event_id,source_id,thread_id,line_no,COALESCE(timestamp,''),text "
        "FROM canonical_events"
    )
    parameters: tuple[Any, ...] = ()
    if since:
        query += " WHERE COALESCE(timestamp,'')>=?"
        parameters = (since,)
    query += " ORDER BY source_id,line_no,event_id"
    for row in connection.execute(query, parameters):
        scan_counts["events"] += 1
        for raw_path in candidate_paths(str(row["text"] or "")):
            scan_counts["path_occurrences"] += 1
            raw_references.setdefault(raw_path, []).append(
                ArtifactReference(
                    event_id=str(row["event_id"]),
                    source_id=str(row["source_id"]),
                    thread_id=str(row["thread_id"]),
                    line_no=int(row["line_no"]),
                    occurred_at=str(row[4] or ""),
                    raw_path=raw_path,
                )
            )
    scan_counts["unique_raw_paths"] = len(raw_references)

    file_groups: dict[str, dict[str, Any]] = {}
    repository_refs: dict[str, list[ArtifactReference]] = {}
    git_root_cache: dict[str, Path | None] = {}
    for raw_path, references in raw_references.items():
        path = local_path(raw_path)
        resolved = _resolved(path)
        if resolved is None:
            excluded_counts["invalid_path"] = excluded_counts.get("invalid_path", 0) + 1
            continue
        if _is_codex_storage(resolved):
            excluded_counts["codex_storage"] = (
                excluded_counts.get("codex_storage", 0) + 1
            )
            continue
        if _is_under(resolved, excluded_roots):
            excluded_counts["excluded_root"] = (
                excluded_counts.get("excluded_root", 0) + 1
            )
            continue
        try:
            exists = resolved.exists()
            is_file = resolved.is_file()
            is_directory = resolved.is_dir()
        except OSError:
            exists = is_file = is_directory = False
        if not exists:
            excluded_counts["missing"] = excluded_counts.get("missing", 0) + 1
            continue
        if is_file:
            scan_counts["existing_files"] += 1
        elif is_directory:
            scan_counts["existing_directories"] += 1

        if config.artifact_capture_git_repositories:
            cache_key = str(resolved if is_directory else resolved.parent)
            if cache_key not in git_root_cache:
                git_root_cache[cache_key] = _find_git_root(resolved)
            git_root = git_root_cache[cache_key]
            if git_root and not _is_under(git_root, excluded_roots):
                repository_refs.setdefault(str(git_root), []).extend(references)

        if not config.artifact_capture_paths or not is_file:
            continue
        extension = resolved.suffix.lower()
        if allowed and extension not in allowed:
            excluded_counts["extension_not_allowed"] = (
                excluded_counts.get("extension_not_allowed", 0) + 1
            )
            continue
        try:
            stat = resolved.stat()
        except OSError:
            excluded_counts["unreadable"] = excluded_counts.get("unreadable", 0) + 1
            continue
        if stat.st_size > config.artifact_max_file_bytes:
            excluded_counts["too_large"] = excluded_counts.get("too_large", 0) + 1
            continue
        group = file_groups.setdefault(
            str(resolved),
            {
                "path": path,
                "resolved": resolved,
                "extension": extension or ".bin",
                "references": [],
            },
        )
        group["references"].extend(references)

    files_by_digest: dict[str, FileCaptureCandidate] = {}
    for group in file_groups.values():
        digest, size, mtime_ns = _sha256_file(group["resolved"])
        unique_references = _deduplicate_references(group["references"])
        existing_candidate = files_by_digest.get(digest)
        if existing_candidate:
            existing_candidate.source_paths.append(group["resolved"])
            existing_candidate.references = _deduplicate_references(
                [*existing_candidate.references, *unique_references]
            )
            continue
        files_by_digest[digest] = FileCaptureCandidate(
            path=group["path"],
            resolved_path=group["resolved"],
            size_bytes=size,
            mtime_ns=mtime_ns,
            extension=group["extension"],
            sha256=digest,
            storage_action="reuse" if digest in known_hashes else "copy",
            source_paths=[group["resolved"]],
            references=unique_references,
        )
    files = list(files_by_digest.values())
    for candidate in files:
        candidate.pending_references = [
            reference
            for reference in candidate.references
            if not _observation_exists(
                connection,
                event_id=reference.event_id,
                original_path=reference.raw_path,
                capture_method="absolute_path",
            )
        ]

    repository_groups: dict[str, dict[str, Any]] = {}
    for root_value, references in sorted(repository_refs.items()):
        root = Path(root_value)
        group = repository_groups.setdefault(
            _filesystem_identity(root),
            {"root": root, "references": []},
        )
        group["references"].extend(references)

    repositories: list[RepositoryCaptureCandidate] = []
    git_errors: list[dict[str, str]] = []
    for group in repository_groups.values():
        root = group["root"]
        try:
            repositories.append(
                _repository_candidate(
                    config,
                    connection,
                    root,
                    _deduplicate_references(group["references"]),
                )
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            key = f"git_error:{type(error).__name__}"
            excluded_counts[key] = excluded_counts.get(key, 0) + 1
            git_errors.append(
                {
                    "root": str(root),
                    "error_type": type(error).__name__,
                    "message": str(error)[:500],
                }
            )

    return ArtifactCapturePlan(
        profile=config.name,
        active_build_id=active_build_id,
        database=database,
        created_at=utc_now(),
        since=since,
        path_capture_enabled=config.artifact_capture_paths,
        git_capture_enabled=config.artifact_capture_git_repositories,
        files=files,
        repositories=repositories,
        git_errors=git_errors,
        scan_counts=scan_counts,
        excluded_counts=excluded_counts,
        existing_artifact_hashes=known_hashes,
    )


def _artifact_record(
    connection: sqlite3.Connection, digest: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT sha256,size_bytes,cas_relative_path,artifact_uri,mime_type,extension
        FROM artifact_files WHERE sha256=?
        """,
        (digest,),
    ).fetchone()


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _store_file(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    source: Path,
    *,
    expected_sha256: str,
    extension: str,
    source_open_path: str,
    keep_reason: str,
    category: str,
) -> sqlite3.Row:
    existing = _artifact_record(connection, expected_sha256)
    if existing:
        return existing
    digest, size, _mtime_ns = _sha256_file(source)
    if digest != expected_sha256:
        raise RuntimeError(f"Artifact content changed after planning: {source}")
    extension = extension.lower()[:16] if extension else ".bin"
    relative = Path("files") / digest[:2] / f"{digest}{extension}"
    destination = config.cas_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=config.cas_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        copied_sha256, copied_size, _copied_mtime = _sha256_file(temporary)
        if copied_sha256 != digest or copied_size != size:
            raise RuntimeError(f"Artifact copy verification failed: {source}")
        if destination.exists():
            destination_sha256, _destination_size, _destination_mtime = _sha256_file(
                destination
            )
            if destination_sha256 != digest:
                raise RuntimeError(f"Artifact CAS collision: {destination}")
            temporary.unlink()
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    uri = f"codex-history-artifact://sha256/{digest}"
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
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
            _human_bytes(size),
            relative.as_posix(),
            uri,
            mime_type,
            extension,
            source_open_path,
            "raw_evidence",
            keep_reason,
            category,
            0,
            0,
        ),
    )
    record = _artifact_record(connection, digest)
    if not record:
        raise RuntimeError(f"Artifact record was not created: {digest}")
    return record


def _source_uri(reference: ArtifactReference) -> str:
    return f"codex-history-source://{reference.source_id}#line={reference.line_no}"


def _insert_observation(
    connection: sqlite3.Connection,
    *,
    artifact: sqlite3.Row,
    reference: ArtifactReference,
    resolved_path: str,
    capture_method: str,
    keep_reason: str,
    category: str,
    role: str,
    captured_at: str,
    checkpoint_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    observation_id = stable_id(
        "artifact-observation",
        reference.event_id,
        reference.raw_path,
        str(artifact["sha256"]),
        capture_method,
        length=40,
    )
    if connection.execute(
        "SELECT 1 FROM artifact_observations WHERE observation_id=?",
        (observation_id,),
    ).fetchone():
        return False
    indexed_path = (
        f"transcript-ref:{reference.source_id}:{reference.event_id}:{reference.raw_path}"
    )
    if checkpoint_id:
        indexed_path = f"{indexed_path}:checkpoint:{checkpoint_id}:{role}"
    path_key = stable_id("artifact-path", indexed_path, length=40)
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
            artifact["sha256"],
            artifact["artifact_uri"],
            artifact["cas_relative_path"],
            artifact["size_bytes"],
            "raw_evidence",
            keep_reason,
            category,
            reference.raw_path,
        ),
    )
    connection.execute(
        """
        INSERT INTO artifact_observations(
            observation_id,event_id,source_id,thread_id,artifact_sha256,
            repository_checkpoint_id,original_path,resolved_path,occurrence_at,
            captured_at,capture_method,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observation_id,
            reference.event_id,
            reference.source_id,
            reference.thread_id,
            artifact["sha256"],
            checkpoint_id or None,
            reference.raw_path,
            resolved_path,
            reference.occurred_at or None,
            captured_at,
            capture_method,
            canonical_json(metadata or {}),
        ),
    )
    evidence_row = connection.execute(
        "SELECT evidence_id FROM evidence WHERE item_id=?", (reference.event_id,)
    ).fetchone()
    scope_id = _scope_for_thread(connection, reference.thread_id)
    if scope_id:
        ledger_id = stable_id(
            "ledger-artifact",
            scope_id,
            reference.event_id,
            str(artifact["sha256"]),
            role,
            length=40,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO ledger_artifacts(
                ledger_artifact_id,scope_id,ref,role,evidence_refs_json,source_path,
                source_locator
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                ledger_id,
                scope_id,
                artifact["artifact_uri"],
                role,
                canonical_json([str(evidence_row[0])] if evidence_row else []),
                _source_uri(reference),
                reference.raw_path,
            ),
        )
    return True


def _temporary_artifact(build_dir: Path, name: str, extension: str) -> Path:
    directory = build_dir / "artifact-staging"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}{extension}"


def _create_git_history_artifact(
    config: ProfileConfig,
    repository: RepositoryCaptureCandidate,
    build_dir: Path,
) -> tuple[Path, str, str]:
    name = repository.fingerprint
    if repository.capture_mode.startswith("head-archive"):
        output = _temporary_artifact(build_dir, name, ".tar.gz")
        _git(
            config,
            repository.root,
            "archive",
            "--format=tar.gz",
            f"--prefix={repository.root.name}/",
            "-o",
            str(output),
            repository.head_commit,
            allow_network=False,
        )
        with tarfile.open(output, "r:gz") as archive:
            archive.getmembers()
        return output, ".tar.gz", "git_head_archive"
    output = _temporary_artifact(build_dir, name, ".bundle")
    _git(
        config,
        repository.root,
        "bundle",
        "create",
        str(output),
        "--all",
        allow_network=config.artifact_git_allow_network,
    )
    subprocess.run(
        ["git", "bundle", "verify", str(output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=config.artifact_git_command_timeout_seconds,
    )
    return output, ".bundle", "git_bundle_all"


def _tar_add_path(
    archive: tarfile.TarFile,
    root: Path,
    relative: Path,
) -> None:
    path = root / relative
    try:
        stat = path.lstat()
    except OSError:
        return
    name = f"{root.name}/{relative.as_posix()}"
    info = tarfile.TarInfo(name=name)
    info.mode = stat.st_mode & 0o777
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if path.is_symlink():
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        archive.addfile(info)
        return
    if not path.is_file():
        return
    info.size = stat.st_size
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def _create_git_worktree_artifact(
    config: ProfileConfig,
    repository: RepositoryCaptureCandidate,
    build_dir: Path,
) -> Path:
    output = _temporary_artifact(
        build_dir, f"{repository.fingerprint}-worktree", ".tar.gz"
    )
    metadata = canonical_json(
        {
            "schema_version": "codex-history-git-worktree-v1",
            "repository_name": repository.root.name,
            "head_commit": repository.head_commit,
            "branch": repository.branch,
            "refs_sha256": repository.refs_sha256,
            "worktree_sha256": repository.worktree_sha256,
            "is_dirty": repository.is_dirty,
        }
    ).encode("utf-8")
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                info = tarfile.TarInfo(
                    name=f"{repository.root.name}/.codex-history-checkpoint.json"
                )
                info.size = len(metadata)
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(metadata))
                for relative in _git_worktree_files(config, repository.root):
                    _tar_add_path(archive, repository.root, relative)
    with tarfile.open(output, "r:gz") as archive:
        archive.getmembers()
    return output


def _ensure_size(path: Path, maximum: int) -> None:
    size = path.stat().st_size
    if size > maximum:
        raise RuntimeError(
            f"Generated Git checkpoint exceeds configured limit "
            f"({size} > {maximum}): {path}"
        )


def apply_artifact_capture(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    plan: ArtifactCapturePlan,
    *,
    build_dir: Path,
) -> dict[str, Any]:
    captured_at = utc_now()
    report = {
        "ordinary_new_files": 0,
        "ordinary_reused_files": 0,
        "ordinary_observations": 0,
        "git_new_checkpoints": 0,
        "git_reused_checkpoints": 0,
        "git_observations": 0,
        "new_cas_bytes": 0,
    }
    for candidate in plan.files:
        if not candidate.pending_references:
            continue
        before_exists = _artifact_record(connection, candidate.sha256) is not None
        artifact = _store_file(
            config,
            connection,
            candidate.resolved_path,
            expected_sha256=candidate.sha256,
            extension=candidate.extension,
            source_open_path=str(candidate.resolved_path),
            keep_reason="existing_absolute_path",
            category="referenced_file",
        )
        if before_exists:
            report["ordinary_reused_files"] += 1
        else:
            report["ordinary_new_files"] += 1
            report["new_cas_bytes"] += int(artifact["size_bytes"])
        for reference in candidate.pending_references:
            if _insert_observation(
                connection,
                artifact=artifact,
                reference=reference,
                resolved_path=str(
                    _resolved(local_path(reference.raw_path))
                    or candidate.resolved_path
                ),
                capture_method="absolute_path",
                keep_reason="existing_absolute_path",
                category="referenced_file",
                role="referenced_file",
                captured_at=captured_at,
                metadata={
                    "temporal_semantics": "file-content-observed-at-capture-time",
                    "planned_sha256": candidate.sha256,
                },
            ):
                report["ordinary_observations"] += 1

    for repository in plan.repositories:
        checkpoint_id = repository.existing_checkpoint_id
        history_sha256 = repository.existing_history_sha256
        worktree_sha256 = repository.existing_worktree_sha256
        if not checkpoint_id:
            history_source, history_extension, history_reason = (
                _create_git_history_artifact(config, repository, build_dir)
            )
            _ensure_size(history_source, config.artifact_git_max_bytes)
            history_sha256, _size, _mtime = _sha256_file(history_source)
            before_history = _artifact_record(connection, history_sha256) is not None
            history_artifact = _store_file(
                config,
                connection,
                history_source,
                expected_sha256=history_sha256,
                extension=history_extension,
                source_open_path=str(repository.root),
                keep_reason=history_reason,
                category="git_repository_checkpoint",
            )
            if not before_history:
                report["new_cas_bytes"] += int(history_artifact["size_bytes"])
            if "+worktree" in repository.capture_mode:
                worktree_source = _create_git_worktree_artifact(
                    config, repository, build_dir
                )
                _ensure_size(worktree_source, config.artifact_git_max_bytes)
                worktree_sha256, _size, _mtime = _sha256_file(worktree_source)
                before_worktree = (
                    _artifact_record(connection, worktree_sha256) is not None
                )
                worktree_artifact = _store_file(
                    config,
                    connection,
                    worktree_source,
                    expected_sha256=worktree_sha256,
                    extension=".tar.gz",
                    source_open_path=str(repository.root),
                    keep_reason="git_dirty_worktree_snapshot",
                    category="git_repository_checkpoint",
                )
                if not before_worktree:
                    report["new_cas_bytes"] += int(worktree_artifact["size_bytes"])
            checkpoint_id = repository.fingerprint
            connection.execute(
                """
                INSERT INTO repository_checkpoints(
                    checkpoint_id,repository_root,head_commit,branch,refs_sha256,
                    worktree_sha256,capture_mode,history_artifact_sha256,
                    worktree_artifact_sha256,is_dirty,is_partial_clone,captured_at,
                    metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    checkpoint_id,
                    str(repository.root),
                    repository.head_commit,
                    repository.branch,
                    repository.refs_sha256,
                    repository.worktree_sha256,
                    repository.capture_mode,
                    history_sha256,
                    worktree_sha256 or None,
                    int(repository.is_dirty),
                    int(repository.is_partial_clone),
                    captured_at,
                    canonical_json(
                        {
                            "history_estimated_bytes": (
                                repository.history_estimated_bytes
                            ),
                            "worktree_estimated_bytes": (
                                repository.worktree_estimated_bytes
                            ),
                            "network_allowed": config.artifact_git_allow_network,
                        }
                    ),
                ),
            )
            report["git_new_checkpoints"] += 1
        else:
            report["git_reused_checkpoints"] += 1
        history_artifact = _artifact_record(connection, history_sha256)
        worktree_artifact = (
            _artifact_record(connection, worktree_sha256)
            if worktree_sha256
            else None
        )
        if not history_artifact:
            raise RuntimeError(
                f"Repository checkpoint history artifact is missing: {checkpoint_id}"
            )
        for reference in repository.pending_references:
            metadata = {
                "head_commit": repository.head_commit,
                "branch": repository.branch,
                "capture_mode": repository.capture_mode,
                "temporal_semantics": "repository-state-observed-at-capture-time",
            }
            if _insert_observation(
                connection,
                artifact=history_artifact,
                reference=reference,
                resolved_path=str(repository.root),
                capture_method="git_checkpoint",
                keep_reason=(
                    "git_head_archive"
                    if repository.capture_mode.startswith("head-archive")
                    else "git_bundle_all"
                ),
                category="git_repository_checkpoint",
                role="git_history_checkpoint",
                captured_at=captured_at,
                checkpoint_id=checkpoint_id,
                metadata=metadata,
            ):
                report["git_observations"] += 1
            if worktree_artifact and _insert_observation(
                connection,
                artifact=worktree_artifact,
                reference=reference,
                resolved_path=str(repository.root),
                capture_method="git_worktree_checkpoint",
                keep_reason="git_dirty_worktree_snapshot",
                category="git_repository_checkpoint",
                role="git_worktree_checkpoint",
                captured_at=captured_at,
                checkpoint_id=checkpoint_id,
                metadata=metadata,
            ):
                report["git_observations"] += 1

    connection.execute(
        """
        UPDATE artifact_files
        SET path_count=(
                SELECT COUNT(*) FROM artifact_paths
                WHERE artifact_paths.sha256=artifact_files.sha256
            ),
            transcript_occurrences_mapped=MAX(
                transcript_occurrences_mapped,
                (
                    SELECT COUNT(*) FROM artifact_observations
                    WHERE artifact_observations.artifact_sha256=artifact_files.sha256
                )
            )
        """
    )
    report["artifact_files_total"] = int(
        connection.execute("SELECT COUNT(*) FROM artifact_files").fetchone()[0]
    )
    report["artifact_observations_total"] = int(
        connection.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
    )
    report["repository_checkpoints_total"] = int(
        connection.execute("SELECT COUNT(*) FROM repository_checkpoints").fetchone()[0]
    )
    shutil.rmtree(build_dir / "artifact-staging", ignore_errors=True)
    return report


def export_artifact_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, columns in ARTIFACT_METADATA_TABLES.items():
        if not _table_exists(connection, table):
            tables[table] = []
            continue
        query = f"SELECT {','.join(columns)} FROM {table} ORDER BY {columns[0]}"
        tables[table] = [
            {column: row[column] for column in columns}
            for row in connection.execute(query)
        ]
    digest = hashlib.sha256(canonical_json(tables).encode("utf-8")).hexdigest()
    return {
        "schema_version": ARTIFACT_METADATA_SCHEMA,
        "digest": digest,
        "tables": tables,
        "counts": {table: len(rows) for table, rows in tables.items()},
    }


def apply_artifact_metadata(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("schema_version") != ARTIFACT_METADATA_SCHEMA:
        raise ValueError(
            f"Unsupported artifact metadata schema: {payload.get('schema_version')}"
        )
    tables = dict(payload.get("tables") or {})
    expected = str(payload.get("digest") or "")
    actual = hashlib.sha256(canonical_json(tables).encode("utf-8")).hexdigest()
    if not expected or actual != expected:
        raise ValueError("Artifact metadata digest mismatch")
    inserted: dict[str, int] = {}
    for table, columns in ARTIFACT_METADATA_TABLES.items():
        rows = list(tables.get(table) or [])
        before = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        placeholders = ",".join("?" for _ in columns)
        for row in rows:
            if set(row) != set(columns):
                raise ValueError(f"Artifact metadata columns disagree for {table}")
            connection.execute(
                f"INSERT OR IGNORE INTO {table}({','.join(columns)}) "
                f"VALUES({placeholders})",
                tuple(row[column] for column in columns),
            )
        after = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        inserted[table] = after - before
    connection.execute(
        """
        UPDATE artifact_files
        SET path_count=(
                SELECT COUNT(*) FROM artifact_paths
                WHERE artifact_paths.sha256=artifact_files.sha256
            ),
            transcript_occurrences_mapped=MAX(
                transcript_occurrences_mapped,
                (
                    SELECT COUNT(*) FROM artifact_observations
                    WHERE artifact_observations.artifact_sha256=artifact_files.sha256
                )
            )
        """
    )
    return {
        "schema_version": ARTIFACT_METADATA_SCHEMA,
        "digest": actual,
        "inserted": inserted,
        "counts": {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ARTIFACT_METADATA_TABLES
        },
    }
