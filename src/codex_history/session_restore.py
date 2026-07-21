from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from .conversation import resolve_threads
from .util import atomic_write_json, utc_now


RESTORE_SCHEMA = "codex-history-native-restore-v1"
ARTIFACT_URI_RE = re.compile(
    r"^codex-history-artifact://sha256/([0-9a-f]{64})$"
)
IMAGE_ITEM_TYPES = {"input_image", "input_image_url", "image"}
DEFAULT_MAX_IMAGE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_IMAGE_TOTAL_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
DEFAULT_RESTORE_TITLE_CHARS = 160


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def _single_thread(
    connection: sqlite3.Connection,
    selector: str,
) -> dict[str, Any]:
    selected = resolve_threads(connection, [selector])
    if len(selected) != 1:
        matches = ", ".join(
            f"{row['thread_id']} ({row['title']})" for row in selected[:8]
        )
        raise ValueError(
            f"Restore requires exactly one thread; {selector!r} matched "
            f"{len(selected)}: {matches}"
        )
    return selected[0]


def _safe_relative(value: str) -> Path:
    relative = Path(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe content-addressed path: {value}")
    return relative


def _snapshot_lines(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    source_id: str,
) -> Iterator[bytes]:
    pending = bytearray()
    chunks = connection.execute(
        "SELECT chunk_index,size_bytes,cas_relative_path FROM source_chunks "
        "WHERE source_id=? ORDER BY chunk_index",
        (source_id,),
    ).fetchall()
    if not chunks:
        raise FileNotFoundError(f"No canonical snapshot chunks for source {source_id}")
    for row in chunks:
        path = snapshot_root / _safe_relative(str(row["cas_relative_path"]))
        data = path.read_bytes()
        if len(data) != int(row["size_bytes"]):
            raise ValueError(
                f"Snapshot chunk size mismatch for {path}: "
                f"expected {row['size_bytes']}, got {len(data)}"
            )
        pending.extend(data)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            yield bytes(pending[: newline + 1])
            del pending[: newline + 1]
    if pending:
        yield bytes(pending)


def _resolve_artifact(relative_path: str, roots: Sequence[Path]) -> Path | None:
    relative = _safe_relative(relative_path)
    parts = (
        relative.parts[1:]
        if relative.parts and relative.parts[0] == "cas"
        else relative.parts
    )
    if not parts:
        return None
    for root in roots:
        candidate = root / Path(*parts)
        if candidate.is_file():
            return candidate
    return None


class _ImagePolicy:
    def __init__(
        self,
        connection: sqlite3.Connection,
        artifact_roots: Sequence[Path],
        *,
        mode: str,
        max_image_bytes: int,
        max_total_bytes: int,
    ) -> None:
        self.connection = connection
        self.artifact_roots = artifact_roots
        self.mode = mode
        self.max_image_bytes = max_image_bytes
        self.max_total_bytes = max_total_bytes
        self.seen: set[str] = set()
        self.cache: dict[str, tuple[str, int] | None] = {}
        self.total_bytes = 0
        self.references = 0
        self.inlined = 0
        self.deduplicated = 0
        self.omitted = 0
        self.missing = 0
        self.corrupt = 0

    def _data_url(self, digest: str) -> tuple[str, int] | None:
        if digest in self.cache:
            return self.cache[digest]
        row = self.connection.execute(
            "SELECT size_bytes,cas_relative_path,mime_type FROM artifact_files WHERE sha256=?",
            (digest,),
        ).fetchone()
        if not row:
            self.missing += 1
            self.cache[digest] = None
            return None
        size = int(row["size_bytes"])
        mime = str(row["mime_type"] or "")
        path = _resolve_artifact(str(row["cas_relative_path"]), self.artifact_roots)
        if (
            not path
            or not mime.startswith("image/")
            or size > self.max_image_bytes
        ):
            if not path:
                self.missing += 1
            self.cache[digest] = None
            return None
        actual_size = path.stat().st_size
        if actual_size != size:
            self.corrupt += 1
            self.cache[digest] = None
            return None
        data = path.read_bytes()
        if (
            len(data) > self.max_image_bytes
            or hashlib.sha256(data).hexdigest() != digest
        ):
            self.corrupt += 1
            self.cache[digest] = None
            return None
        value = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        loaded = (value, len(data))
        self.cache[digest] = loaded
        return loaded

    def replacement(self, value: str) -> tuple[str | None, str | None]:
        match = ARTIFACT_URI_RE.fullmatch(value)
        if not match:
            return value, None
        digest = match.group(1)
        self.references += 1
        if self.mode == "stored":
            return value, None
        if self.mode == "none":
            self.omitted += 1
            return None, self.placeholder(digest, "omitted by restore policy")
        if self.mode == "deduplicated" and digest in self.seen:
            self.deduplicated += 1
            return None, self.placeholder(digest, "duplicate image omitted")
        loaded = self._data_url(digest)
        self.seen.add(digest)
        if loaded is None:
            self.omitted += 1
            return None, self.placeholder(digest, "image unavailable or over size limit")
        data_url, decoded_size = loaded
        if self.total_bytes + decoded_size > self.max_total_bytes:
            self.omitted += 1
            return None, self.placeholder(digest, "combined image size limit reached")
        self.total_bytes += decoded_size
        self.inlined += 1
        return data_url, None

    @staticmethod
    def placeholder(digest: str, reason: str) -> str:
        return (
            f"[Historical image {reason}; sha256={digest}. "
            "Use Codex History artifact lookup to inspect the preserved file.]"
        )

    def report(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "artifact_uri_references": self.references,
            "unique_image_hashes_seen": len(self.seen),
            "images_inlined": self.inlined,
            "duplicate_image_occurrences_omitted": self.deduplicated,
            "images_omitted": self.omitted,
            "images_missing": self.missing,
            "images_corrupt": self.corrupt,
            "decoded_image_bytes_inlined": self.total_bytes,
            "decoded_image_human": _human_bytes(self.total_bytes),
        }


def _transform_image_values(value: Any, policy: _ImagePolicy) -> Any:
    if isinstance(value, list):
        transformed: list[Any] = []
        for item in value:
            if isinstance(item, dict) and str(item.get("type") or "") in IMAGE_ITEM_TYPES:
                image_value = item.get("image_url")
                if isinstance(image_value, str):
                    replacement, placeholder = policy.replacement(image_value)
                    if replacement is not None:
                        updated = dict(item)
                        updated["image_url"] = replacement
                        transformed.append(updated)
                    elif placeholder:
                        transformed.append({"type": "input_text", "text": placeholder})
                    continue
            transformed.append(_transform_image_values(item, policy))
        return transformed
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "images" and isinstance(item, list):
            images: list[Any] = []
            for image in item:
                if isinstance(image, str):
                    replacement, _ = policy.replacement(image)
                    if replacement is not None:
                        images.append(replacement)
                else:
                    images.append(_transform_image_values(image, policy))
            result[key] = images
        elif key == "image_url" and isinstance(item, str):
            replacement, placeholder = policy.replacement(item)
            result[key] = replacement if replacement is not None else ""
            if placeholder:
                result["codex_history_restore_note"] = placeholder
        else:
            result[key] = _transform_image_values(item, policy)
    return result


def _map_path(value: str, mappings: Sequence[tuple[str, str]]) -> str:
    normalized = value.replace("\\", "/")
    best: tuple[int, str, str] | None = None
    for original, local in mappings:
        prefix = original.replace("\\", "/").rstrip("/")
        casefold = bool(re.match(r"^[A-Za-z]:/", prefix))
        source_value = normalized.casefold() if casefold else normalized
        source_prefix = prefix.casefold() if casefold else prefix
        if source_value == source_prefix or source_value.startswith(source_prefix + "/"):
            candidate = (len(prefix), prefix, local)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if not best:
        return value
    suffix = normalized[len(best[1]) :].lstrip("/")
    return str(Path(best[2]) / suffix) if suffix else best[2]


def materialize_thread_snapshot(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    thread: dict[str, Any],
    output: Path,
    *,
    artifact_roots: Sequence[Path] = (),
    image_mode: str = "deduplicated",
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_image_total_bytes: int = DEFAULT_MAX_IMAGE_TOTAL_BYTES,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    if image_mode not in {"deduplicated", "none", "all", "stored"}:
        raise ValueError(f"Unsupported image restore mode: {image_mode}")
    if min(max_image_bytes, max_image_total_bytes, max_transcript_bytes) <= 0:
        raise ValueError("Restore size limits must be positive")
    source_id = str(thread.get("source_id") or "")
    if not source_id:
        raise ValueError(f"Thread {thread['thread_id']} has no canonical source snapshot")
    output.parent.mkdir(parents=True, exist_ok=True)
    policy = _ImagePolicy(
        connection,
        artifact_roots,
        mode=image_mode,
        max_image_bytes=max_image_bytes,
        max_total_bytes=max_image_total_bytes,
    )
    digest = hashlib.sha256()
    line_count = 0
    output_bytes = 0
    source_session_id = ""
    source_cwd = ""
    source_cli_version = ""
    try:
        with output.open("wb") as handle:
            for raw in _snapshot_lines(connection, snapshot_root, source_id):
                line_count += 1
                encoded = raw
                if b"codex-history-artifact://sha256/" in raw and image_mode != "stored":
                    value = json.loads(raw)
                    transformed = _transform_image_values(value, policy)
                    encoded = json.dumps(
                        transformed,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                if line_count == 1:
                    first = json.loads(encoded)
                    if first.get("type") != "session_meta":
                        raise ValueError("Canonical snapshot does not start with session_meta")
                    payload = (
                        first.get("payload")
                        if isinstance(first.get("payload"), dict)
                        else {}
                    )
                    source_session_id = str(payload.get("id") or "")
                    source_cwd = str(payload.get("cwd") or "")
                    source_cli_version = str(payload.get("cli_version") or "")
                output_bytes += len(encoded)
                if output_bytes > max_transcript_bytes:
                    raise ValueError(
                        f"Restored transcript exceeds {_human_bytes(max_transcript_bytes)}; "
                        "raise --max-transcript-mb only after reviewing the OOM risk"
                    )
                handle.write(encoded)
                digest.update(encoded)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    expected_thread_id = str(thread["thread_id"])
    if source_session_id and source_session_id != expected_thread_id:
        output.unlink(missing_ok=True)
        raise ValueError(
            f"Snapshot session id {source_session_id} does not match thread {expected_thread_id}"
        )
    return {
        "thread_id": expected_thread_id,
        "source_id": source_id,
        "source_cwd": source_cwd,
        "source_cli_version": source_cli_version,
        "line_count": line_count,
        "size_bytes": output_bytes,
        "size_human": _human_bytes(output_bytes),
        "sha256": digest.hexdigest(),
        "images": policy.report(),
    }


class _AppServerClient:
    def __init__(
        self,
        codex_bin: Path,
        codex_home: Path,
        *,
        timeout: float,
    ) -> None:
        self.timeout = timeout
        self.stderr_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        try:
            self.process = subprocess.Popen(
                [str(codex_bin), "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr_file,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=environment,
            )
        except OSError as error:
            self.stderr_file.close()
            raise RuntimeError(f"Unable to start Codex app-server: {error}") from error
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, method: str, identifier: int | None, params: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        message: dict[str, Any] = {"method": method, "params": params}
        if identifier is not None:
            message["id"] = identifier
        try:
            encoded = json.dumps(message, separators=(",", ":")) + "\n"
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except OSError as error:
            raise RuntimeError(
                f"Codex app-server disconnected while sending {method}: {self.stderr()}"
            ) from error

    def wait(self, identifier: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Timed out waiting for Codex app-server response {identifier}: "
                    f"{self.stderr()}"
                )
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise RuntimeError(
                    f"Timed out waiting for Codex app-server response {identifier}: "
                    f"{self.stderr()}"
                ) from error
            if message is None:
                raise RuntimeError(
                    f"Codex app-server exited before response {identifier}: {self.stderr()}"
                )
            if message.get("id") != identifier:
                continue
            if message.get("error"):
                error = message["error"]
                raise RuntimeError(
                    f"Codex app-server {identifier} failed: "
                    f"{error.get('message') or error}"
                )
            return dict(message.get("result") or {})

    def stderr(self) -> str:
        self.stderr_file.flush()
        self.stderr_file.seek(0)
        return self.stderr_file.read()[-4000:]

    def close(self) -> None:
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stderr_file.close()


def _codex_binary(value: Path | None) -> Path:
    if value:
        path = value.expanduser().resolve()
    else:
        discovered = shutil.which("codex")
        if not discovered:
            raise FileNotFoundError(
                "Codex executable not found on PATH; pass --codex-bin explicitly"
            )
        path = Path(discovered).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Codex executable not found: {path}")
    return path


def _codex_version(codex_bin: Path) -> str:
    try:
        result = subprocess.run(
            [str(codex_bin), "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = str(getattr(error, "stderr", "") or error)
        raise RuntimeError(
            f"Unable to query Codex version from {codex_bin}: {detail}"
        ) from error
    return result.stdout.strip()


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _restore_title(value: str, fallback: str) -> str:
    title = " ".join((value.strip() or fallback).split())
    if len(title) <= DEFAULT_RESTORE_TITLE_CHARS:
        return title
    return title[: DEFAULT_RESTORE_TITLE_CHARS - 3].rstrip() + "..."


def _thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": str(thread["thread_id"]),
        "title": str(thread["title"]),
        "first_activity_at": str(thread.get("first_activity_at") or ""),
        "last_activity_at": str(thread.get("last_activity_at") or ""),
        "turn_count": int(thread.get("turn_count") or 0),
        "snapshot_format": str(thread.get("snapshot_format") or ""),
        "snapshot_size_bytes": int(thread.get("snapshot_size_bytes") or 0),
        "snapshot_content_sha256": str(thread.get("snapshot_content_sha256") or ""),
    }


def _rollout_summary(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    lines = 0
    last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if size and last_byte != b"\n":
        lines += 1
    return {
        "path": str(path),
        "size_bytes": size,
        "size_human": _human_bytes(size),
        "line_count": lines,
        "sha256": digest.hexdigest(),
    }


def restore_native_thread(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    *,
    selector: str,
    artifact_roots: Sequence[Path] = (),
    path_mappings: Sequence[tuple[str, str]] = (),
    codex_home: Path | None = None,
    codex_bin: Path | None = None,
    cwd: Path | None = None,
    title: str = "",
    image_mode: str = "deduplicated",
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_image_total_bytes: int = DEFAULT_MAX_IMAGE_TOTAL_BYTES,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
    timeout: float = 60.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    thread = _single_thread(connection, selector)
    target_home = (codex_home or _default_codex_home()).expanduser().resolve()
    target_home_exists = target_home.is_dir()
    executable = _codex_binary(codex_bin)
    version = _codex_version(executable)
    restored_title = _restore_title(
        title,
        f"{thread['title']} (restored history)",
    )
    if timeout <= 0:
        raise ValueError("Restore timeout must be positive")

    with tempfile.TemporaryDirectory(prefix="codex-history-restore-") as temporary:
        staging = Path(temporary) / f"{thread['thread_id']}.jsonl"
        materialized = materialize_thread_snapshot(
            connection,
            snapshot_root,
            thread,
            staging,
            artifact_roots=artifact_roots,
            image_mode=image_mode,
            max_image_bytes=max_image_bytes,
            max_image_total_bytes=max_image_total_bytes,
            max_transcript_bytes=max_transcript_bytes,
        )
        original_cwd = str(materialized["source_cwd"] or "")
        mapped_cwd = _map_path(original_cwd, path_mappings) if original_cwd else ""
        if cwd:
            target_cwd = cwd.expanduser().resolve()
            cwd_basis = "explicit"
        elif mapped_cwd and Path(mapped_cwd).expanduser().is_dir():
            target_cwd = Path(mapped_cwd).expanduser().resolve()
            cwd_basis = "mapped_source"
        elif original_cwd and Path(original_cwd).expanduser().is_dir():
            target_cwd = Path(original_cwd).expanduser().resolve()
            cwd_basis = "source"
        else:
            target_cwd = Path.cwd().resolve()
            cwd_basis = "current_working_directory_fallback"
        if not target_cwd.is_dir():
            raise FileNotFoundError(f"Restore working directory does not exist: {target_cwd}")

        warnings: list[str] = []
        if cwd_basis == "current_working_directory_fallback":
            warnings.append(
                "The historical working directory is unavailable; the restored thread "
                "will use the current working directory. Pass --cwd to choose explicitly."
            )
        image_report = materialized["images"]
        if image_report["images_omitted"]:
            warnings.append(
                f"{image_report['images_omitted']} image occurrence(s) were replaced "
                "with traceable text placeholders by the restore limits."
            )
        if materialized["size_bytes"] > 128 * 1024 * 1024:
            warnings.append(
                "The materialized transcript exceeds 128 MiB and may be expensive for "
                "Codex clients to render."
            )
        if image_mode == "stored":
            warnings.append(
                "Stored image mode preserves Codex History CAS URIs; native Codex clients "
                "cannot display those images without the knowledge base."
            )

        plan = {
            "schema_version": RESTORE_SCHEMA,
            "status": "dry_run" if dry_run else "planned",
            "created_at": utc_now(),
            "source": _thread_summary(thread),
            "materialized": materialized,
            "target": {
                "codex_home": str(target_home),
                "codex_home_exists": target_home_exists,
                "codex_bin": str(executable),
                "codex_version": version,
                "cwd": str(target_cwd),
                "cwd_basis": cwd_basis,
                "source_cwd": original_cwd,
                "mapped_source_cwd": mapped_cwd,
                "title": restored_title,
            },
            "cost": {"model_calls": 0, "embedding_calls": 0, "cost_cny": 0.0},
            "warnings": warnings,
            "safety": {
                "creates_independent_native_fork": True,
                "mutates_existing_threads": False,
                "uses_codex_app_server": True,
                "transcript_format_is_version_sensitive": True,
            },
        }
        if dry_run:
            return plan

        target_home.mkdir(parents=True, exist_ok=True)
        client = _AppServerClient(executable, target_home, timeout=timeout)
        try:
            client.send(
                "initialize",
                0,
                {
                    "clientInfo": {
                        "name": "codex-history-suite",
                        "title": "Codex History Suite",
                        "version": RESTORE_SCHEMA,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            initialized = client.wait(0)
            client.send("initialized", None, {})
            client.send(
                "thread/fork",
                1,
                {
                    "threadId": str(thread["thread_id"]),
                    "path": str(staging),
                    "cwd": str(target_cwd),
                    "ephemeral": False,
                },
            )
            forked = client.wait(1)
            native = dict(forked.get("thread") or {})
            native_id = str(native.get("id") or "")
            if not native_id:
                raise RuntimeError("Codex app-server returned no native thread id")
            client.send(
                "thread/name/set",
                2,
                {"threadId": native_id, "name": restored_title},
            )
            client.wait(2)
            client.send(
                "thread/read",
                3,
                {"threadId": native_id, "includeTurns": True},
            )
            verified = dict(client.wait(3).get("thread") or {})
        finally:
            client.close()

        native_path = str(verified.get("path") or native.get("path") or "")
        native_turns = list(verified.get("turns") or native.get("turns") or [])
        native_rollout = None
        if native_path and Path(native_path).is_file():
            native_rollout = _rollout_summary(Path(native_path))
        historical_turns = int(thread.get("turn_count") or 0)
        if historical_turns != len(native_turns):
            warnings.append(
                "Codex exposed "
                f"{len(native_turns)} active native turn(s) from {historical_turns} "
                "historical turn record(s). Rolled-back, aborted, or unsupported records "
                "remain authoritative in the source knowledge base."
            )
        manifest = {
            **plan,
            "status": "complete",
            "completed_at": utc_now(),
            "app_server": {
                "codex_home": str(initialized.get("codexHome") or target_home),
                "platform_family": str(initialized.get("platformFamily") or ""),
                "platform_os": str(initialized.get("platformOs") or ""),
            },
            "restored": {
                "thread_id": native_id,
                "forked_from_thread_id": str(native.get("forkedFromId") or thread["thread_id"]),
                "title": str(verified.get("name") or restored_title),
                "cwd": str(verified.get("cwd") or native.get("cwd") or target_cwd),
                "rollout_path": native_path,
                "rollout": native_rollout,
                "turn_count_verified": len(native_turns),
                "historical_turn_records": historical_turns,
                "codex_rewrote_rollout": bool(
                    native_rollout
                    and (
                        native_rollout["sha256"] != materialized["sha256"]
                        or native_rollout["line_count"] != materialized["line_count"]
                    )
                ),
                "cli_resume_command": f"codex resume {native_id}",
                "desktop_deeplink": f"codex://threads/{native_id}",
            },
        }
        audit_dir = target_home / "codex-history-restores"
        audit_path = audit_dir / f"{native_id}.json"
        atomic_write_json(audit_path, manifest)
        manifest["restore_manifest"] = str(audit_path)
        return manifest
