from __future__ import annotations

import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .audit import audit_connection, compare_databases
from .config import ProfileConfig, config_path, ensure_profile_dirs, resolve_summarization
from .estimate import actual_managed_storage, estimate_build
from .knowledge import (
    delete_thread,
    insert_parsed_thread,
    insert_source_snapshot,
    rebuild_conservative_relations,
    rebuild_family_scopes,
)
from .parser import parse_snapshot
from .schema import connect, initialize, rebuild_fts
from .source import (
    SourceCandidate,
    SourceChange,
    classify_changes,
    discover_sources,
    previous_sources,
    snapshot_source,
)
from .util import (
    atomic_write_json,
    canonical_json,
    file_lock,
    read_json,
    sha256_file,
    utc_now,
)


PIPELINE_VERSION = "portable-pipeline-v1"
STAGES = (
    "discover",
    "snapshot",
    "ingest",
    "lineage",
    "summarize",
    "index",
    "audit",
    "promote",
)


def _build_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}"


def active_info(config: ProfileConfig) -> dict[str, Any] | None:
    return read_json(config.active_path)


def active_database(config: ProfileConfig) -> Path | None:
    info = active_info(config)
    if not info:
        return None
    path = config.root / str(info["database"])
    return path if path.exists() else None


def _config_sha256(config: ProfileConfig) -> str:
    path = config_path(config.home)
    return sha256_file(path) if path.exists() else ""


def _source_public(source: SourceCandidate) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "adapter": source.adapter,
        "source_root": str(source.root),
        "source_path": str(source.path),
        "relative_path": source.relative_path,
        "thread_id": source.thread_id,
        "title": source.title,
        "size_bytes": source.size_bytes,
        "mtime_ns": source.mtime_ns,
        "archived": source.archived,
    }


def _change_public(change: SourceChange) -> dict[str, Any]:
    return {
        "kind": change.kind,
        "source_id": change.source.source_id if change.source else change.previous.get("source_id"),
        "thread_id": change.source.thread_id if change.source else change.previous.get("thread_id"),
        "source_path": str(change.source.path) if change.source else change.previous.get("source_path"),
        "size_bytes": change.source.size_bytes if change.source else change.previous.get("size_bytes", 0),
        "previous_size_bytes": int(change.previous.get("size_bytes", 0)) if change.previous else 0,
        "reason": change.reason,
    }


def plan(config: ProfileConfig, *, mode: str) -> dict[str, Any]:
    ensure_profile_dirs(config)
    sources = discover_sources(config)
    database = active_database(config)
    previous: dict[str, dict[str, Any]] = {}
    if database:
        connection = connect(database, readonly=True)
        try:
            previous = previous_sources(connection)
        finally:
            connection.close()
    if mode == "full" or not database:
        changes = [SourceChange("added", source, None, "full build") for source in sources]
    else:
        changes = classify_changes(sources, previous)
    actionable = [change for change in changes if change.kind != "unchanged"]
    summarization = resolve_summarization(config)
    estimate = estimate_build(
        config,
        sources=sources,
        changes=changes,
        database=database,
        summarization=summarization,
    )
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    active = active_info(config) or {}
    incremental_ready = not bool(active.get("migrated_from"))
    warnings: list[str] = []
    if mode != "full" and not incremental_ready:
        warnings.append(
            "The active database is a query-compatible legacy migration. "
            "Run a full build before the first incremental update."
        )
    if summarization["fallback"]:
        warnings.append(
            "Model-first auto mode will fall back to deterministic extractive summaries because "
            + str(summarization["fallback_reason"])
            + ". Configure the model API key for the recommended higher-quality build."
        )
    elif summarization["effective_mode"] == "unavailable":
        warnings.append(
            "Strict model summarization cannot start because "
            + str(summarization["fallback_reason"])
            + "."
        )
    token_estimate = estimate["tokens"]
    cost_estimate = estimate["cost_cny"]
    storage_estimate = estimate["storage"]
    summary_tokens = token_estimate["summary"]
    return {
        "schema_version": "codex-history-plan-v1",
        "created_at": utc_now(),
        "profile": config.name,
        "mode": mode,
        "active_build_id": active.get("build_id"),
        "incremental_ready": incremental_ready,
        "warnings": warnings,
        "source_count": len(sources),
        "change_counts": counts,
        "actionable_count": len(actionable),
        "changed_bytes": estimate["source"]["actionable_source_bytes"],
        "new_or_reprocessed_bytes": estimate["source"]["new_or_reprocessed_bytes"],
        "summarization_mode": config.summary_mode,
        "effective_summarization_mode": summarization["effective_mode"],
        "summarization": summarization,
        "estimated_input_tokens_upper_bound": (
            summary_tokens["input_upper"] if summary_tokens["would_call_model"] else 0
        ),
        "estimated_input_tokens_if_model_enabled": summary_tokens["input_expected"],
        "estimated_output_tokens": (
            summary_tokens["output_expected"] if summary_tokens["would_call_model"] else 0
        ),
        "estimated_output_tokens_if_model_enabled": summary_tokens["output_expected"],
        "estimated_embedding_tokens_upper_bound": token_estimate["embedding_input_upper"],
        "estimated_summary_cost_cny": cost_estimate["summary_upper_no_cache"],
        "estimated_summary_cost_cny_if_model_enabled": cost_estimate[
            "summary_if_model_enabled_upper_no_cache"
        ],
        "estimated_embedding_cost_cny": cost_estimate["embedding_upper"],
        "estimated_cost_cny_expected": cost_estimate["total_expected"],
        "estimated_cost_cny": cost_estimate["total_upper"],
        "estimated_storage_bytes": storage_estimate["expected_bytes"],
        "estimated_storage_bytes_range": [
            storage_estimate["low_bytes"],
            storage_estimate["upper_bytes"],
        ],
        "estimate": estimate,
        "changes": [_change_public(change) for change in changes],
        "sources": [_source_public(source) for source in sources],
    }


class RunState:
    def __init__(self, config: ProfileConfig, build_id: str, kind: str, build_dir: Path):
        self.config = config
        self.build_id = build_id
        self.kind = kind
        self.build_dir = build_dir
        self.path = config.runs_dir / build_id / "run.json"
        self.data = {
            "schema_version": "codex-history-run-v1",
            "pipeline_version": PIPELINE_VERSION,
            "build_id": build_id,
            "kind": kind,
            "status": "running",
            "started_at": utc_now(),
            "completed_at": None,
            "build_dir": str(build_dir),
            "stages": {
                stage: {"ordinal": index, "status": "pending", "report": {}}
                for index, stage in enumerate(STAGES)
            },
        }
        self.save()

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    @contextmanager
    def stage(self, connection: sqlite3.Connection, name: str) -> Iterator[dict[str, Any]]:
        state = self.data["stages"][name]
        state["status"] = "running"
        state["started_at"] = utc_now()
        self.save()
        connection.execute(
            """
            INSERT INTO stage_checkpoints(build_id,stage_name,ordinal,status,started_at,report_json)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(build_id,stage_name) DO UPDATE SET
                status=excluded.status,started_at=excluded.started_at,report_json=excluded.report_json
            """,
            (self.build_id, name, int(state["ordinal"]), "running", state["started_at"], "{}"),
        )
        connection.commit()
        report: dict[str, Any] = {}
        try:
            yield report
        except BaseException as error:
            connection.rollback()
            state["status"] = "failed"
            state["completed_at"] = utc_now()
            state["error"] = f"{type(error).__name__}: {error}"
            self.data["status"] = "failed"
            self.save()
            connection.execute(
                "UPDATE stage_checkpoints SET status='failed',completed_at=?,report_json=? WHERE build_id=? AND stage_name=?",
                (state["completed_at"], canonical_json({"error": state["error"]}), self.build_id, name),
            )
            connection.execute(
                "UPDATE builds SET status='failed',completed_at=? WHERE build_id=?",
                (state["completed_at"], self.build_id),
            )
            connection.commit()
            raise
        else:
            connection.commit()
            state["status"] = "complete"
            state["completed_at"] = utc_now()
            state["report"] = report
            self.save()
            connection.execute(
                "UPDATE stage_checkpoints SET status='complete',completed_at=?,report_json=? WHERE build_id=? AND stage_name=?",
                (state["completed_at"], canonical_json(report), self.build_id, name),
            )
            connection.commit()

    def complete(self) -> None:
        self.data["status"] = "complete"
        self.data["completed_at"] = utc_now()
        self.save()


def _new_database(
    config: ProfileConfig,
    build_id: str,
    kind: str,
    parent_build_id: str | None,
    source_manifest_path: Path,
) -> tuple[Path, sqlite3.Connection]:
    build_dir = config.builds_dir / build_id
    build_dir.mkdir(parents=True, exist_ok=False)
    database = build_dir / "codex_history.sqlite3"
    if kind == "incremental":
        parent = active_database(config)
        if not parent:
            raise RuntimeError("Incremental update requires an active build")
        source_connection = connect(parent, readonly=True)
        target_connection = connect(database)
        try:
            source_connection.backup(target_connection)
        finally:
            source_connection.close()
            target_connection.close()
    connection = connect(database)
    initialize(connection)
    connection.execute(
        "INSERT INTO builds(build_id,build_kind,status,parent_build_id,started_at,source_manifest_path,config_sha256,notes_json) VALUES(?,?,?,?,?,?,?,?)",
        (
            build_id,
            kind,
            "running",
            parent_build_id,
            utc_now(),
            str(source_manifest_path),
            _config_sha256(config),
            canonical_json({"pipeline_version": PIPELINE_VERSION}),
        ),
    )
    connection.commit()
    return database, connection


def _promote(config: ProfileConfig, build_id: str, database: Path) -> dict[str, Any]:
    relative = database.relative_to(config.root).as_posix()
    payload = {
        "schema_version": "codex-history-active-v1",
        "profile": config.name,
        "build_id": build_id,
        "database": relative,
        "promoted_at": utc_now(),
        "incremental_ready": True,
    }
    atomic_write_json(config.active_path, payload)
    return payload


def _build_locked(
    config: ProfileConfig,
    *,
    kind: str,
    promote: bool,
    max_cost_cny: float | None,
) -> dict[str, Any]:
    build_plan = plan(config, mode="full" if kind == "full" else "incremental")
    if max_cost_cny is None and build_plan["estimated_cost_cny"] > 0:
        raise RuntimeError(
            "This build can call paid APIs. Review `codex-history plan --json` and pass an "
            "explicit --max-cost-cny limit."
        )
    if max_cost_cny is not None and build_plan["estimated_cost_cny"] > max_cost_cny:
        raise RuntimeError(
            f"Estimated cost {build_plan['estimated_cost_cny']:.6f} CNY exceeds limit {max_cost_cny:.6f} CNY"
        )
    if kind == "incremental" and build_plan["actionable_count"] == 0:
        return {
            "status": "no_changes",
            "build_id": build_plan["active_build_id"],
            "plan": build_plan,
            "database": str(active_database(config)),
        }
    build_id = _build_id(kind)
    build_dir = config.builds_dir / build_id
    manifest_path = build_dir / "source-manifest.json"
    parent = build_plan.get("active_build_id") if kind == "incremental" else None
    database, connection = _new_database(
        config, build_id, kind, parent, manifest_path
    )
    run = RunState(config, build_id, kind, build_dir)
    sources = discover_sources(config)
    snapshots: dict[str, Any] = {}
    changes: list[SourceChange]
    try:
        with run.stage(connection, "discover") as report:
            previous = previous_sources(connection) if kind == "incremental" else {}
            changes = (
                classify_changes(sources, previous)
                if kind == "incremental"
                else [SourceChange("added", source, None, "full build") for source in sources]
            )
            report.update(
                {
                    "sources": len(sources),
                    "actionable": sum(change.kind != "unchanged" for change in changes),
                    "changes": [_change_public(change) for change in changes],
                }
            )
            atomic_write_json(
                manifest_path,
                {
                    "schema_version": "codex-history-source-manifest-v1",
                    "build_id": build_id,
                    "created_at": utc_now(),
                    "sources": [_source_public(source) for source in sources],
                    "changes": [_change_public(change) for change in changes],
                    "snapshots": [],
                },
            )

        with run.stage(connection, "snapshot") as report:
            for change in changes:
                if change.kind in {"added", "appended", "rewritten"} and change.source:
                    snapshot = snapshot_source(config, change.source)
                    snapshots[change.source.source_id] = snapshot
            manifest = read_json(manifest_path)
            manifest["snapshots"] = [
                {
                    "source_id": source_id,
                    "content_sha256": snapshot.content_sha256,
                    "line_count": snapshot.line_count,
                    "manifest_path": str(snapshot.manifest_path),
                    "chunks": len(snapshot.chunks),
                }
                for source_id, snapshot in sorted(snapshots.items())
            ]
            atomic_write_json(manifest_path, manifest)
            report.update(
                {
                    "snapshotted_sources": len(snapshots),
                    "unique_chunks": len(
                        {
                            chunk["sha256"]
                            for snapshot in snapshots.values()
                            for chunk in snapshot.chunks
                        }
                    ),
                }
            )

        with run.stage(connection, "ingest") as report:
            totals = {
                "threads": 0,
                "events": 0,
                "turns": 0,
                "evidence": 0,
                "fact_blocks": 0,
                "parse_errors": 0,
                "artifacts": 0,
            }
            for change in changes:
                if change.kind == "unchanged":
                    continue
                old = change.previous
                if old:
                    old_thread = str(old["thread_id"])
                    if connection.execute(
                        "SELECT 1 FROM threads WHERE thread_id=?", (old_thread,)
                    ).fetchone():
                        delete_thread(connection, old_thread, build_id)
                    connection.execute("DELETE FROM source_files WHERE source_id=?", (old["source_id"],))
                if change.kind == "deleted" or not change.source:
                    continue
                if not old and connection.execute(
                    "SELECT 1 FROM threads WHERE thread_id=?", (change.source.thread_id,)
                ).fetchone():
                    delete_thread(connection, change.source.thread_id, build_id)
                snapshot = snapshots[change.source.source_id]
                insert_source_snapshot(connection, snapshot)
                parsed = parse_snapshot(snapshot, config)
                inserted = insert_parsed_thread(connection, snapshot, parsed, config)
                totals["threads"] += 1
                for key, value in inserted.items():
                    totals[key] += value
            report.update(totals)

        with run.stage(connection, "lineage") as report:
            report["family_scopes"] = rebuild_family_scopes(connection, build_id)
            report["relations"] = rebuild_conservative_relations(connection)

        with run.stage(connection, "summarize") as report:
            from .summarize import summarize_scopes

            affected_threads = {
                str(change.source.thread_id if change.source else change.previous.get("thread_id"))
                for change in changes
                if change.kind != "unchanged"
            }
            if kind == "full":
                scope_ids = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT scope_id FROM scopes ORDER BY CASE scope_type WHEN 'thread' THEN 0 ELSE 1 END,scope_id"
                    )
                ]
            else:
                scope_ids = sorted(
                    {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT DISTINCT scope_id FROM scope_threads WHERE thread_id IN ("
                            + ",".join("?" for _ in affected_threads)
                            + ")",
                            sorted(affected_threads),
                        )
                    }
                ) if affected_threads else []
                if any(change.kind == "deleted" for change in changes):
                    scope_ids = sorted(
                        set(scope_ids)
                        | {
                            str(row[0])
                            for row in connection.execute(
                                "SELECT scope_id FROM scopes WHERE scope_type='family'"
                            )
                        }
                    )
                scope_ids = sorted(
                    set(scope_ids)
                    | {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT scope_id FROM scopes WHERE scope_type='family'"
                        )
                    }
                )
            if scope_ids:
                placeholders = ",".join("?" for _ in scope_ids)
                scope_types = {
                    str(row["scope_id"]): str(row["scope_type"])
                    for row in connection.execute(
                        f"SELECT scope_id,scope_type FROM scopes WHERE scope_id IN ({placeholders})",
                        scope_ids,
                    )
                }
                scope_ids = sorted(
                    scope_ids,
                    key=lambda scope_id: (
                        1 if scope_types.get(scope_id) == "family" else 0,
                        scope_id,
                    ),
                )
            report.update(
                summarize_scopes(
                    config,
                    connection,
                    scope_ids=scope_ids,
                    max_cost_cny=max_cost_cny,
                )
            )

        with run.stage(connection, "index") as report:
            connection.execute(
                "UPDATE semantic_documents SET record_count=(SELECT COUNT(*) FROM semantic_document_records sr WHERE sr.document_id=semantic_documents.document_id)"
            )
            connection.execute(
                "DELETE FROM semantic_documents WHERE document_id NOT IN (SELECT DISTINCT document_id FROM semantic_document_records)"
            )
            rebuild_fts(connection)
            semantic_report: dict[str, Any] = {"enabled": False}
            if config.embedding_enabled:
                from .semantic import refresh_embeddings

                summary_cost = float(
                    run.data["stages"]["summarize"].get("report", {}).get("cost_cny", 0.0)
                )
                remaining_cost = (
                    None if max_cost_cny is None else max(0.0, max_cost_cny - summary_cost)
                )
                semantic_report = refresh_embeddings(
                    config,
                    connection,
                    max_cost_cny=remaining_cost,
                )
            report.update(
                {
                    "knowledge": connection.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
                    "semantic_documents": connection.execute(
                        "SELECT COUNT(*) FROM semantic_documents"
                    ).fetchone()[0],
                    "semantic": semantic_report,
                }
            )

        with run.stage(connection, "audit") as report:
            audit = audit_connection(connection)
            report.update(audit)
            atomic_write_json(build_dir / "audit.json", audit)
            if not audit["passed"]:
                raise RuntimeError("Build audit failed")
            connection.execute(
                "UPDATE builds SET logical_digest=? WHERE build_id=?",
                (audit["logical_digest"]["sha256"], build_id),
            )

        with run.stage(connection, "promote") as report:
            if promote:
                report.update(_promote(config, build_id, database))
                promoted_at = report["promoted_at"]
            else:
                report.update({"promoted": False})
                promoted_at = None
            connection.execute(
                "UPDATE builds SET status='complete',completed_at=?,promoted_at=? WHERE build_id=?",
                (utc_now(), promoted_at, build_id),
            )
        run.complete()
        summary_report = run.data["stages"]["summarize"].get("report", {})
        semantic_report = (
            run.data["stages"]["index"].get("report", {}).get("semantic", {})
        )
        summary_input = int(summary_report.get("input_tokens", 0))
        summary_output = int(summary_report.get("output_tokens", 0))
        embedding_input = int(semantic_report.get("input_tokens", 0))
        summary_cost = float(summary_report.get("cost_cny", 0.0))
        embedding_cost = float(semantic_report.get("actual_cost_cny", 0.0))
        usage = {
            "summary_input_tokens": summary_input,
            "summary_cached_input_tokens": int(
                summary_report.get("cached_input_tokens", 0)
            ),
            "summary_uncached_input_tokens": int(
                summary_report.get("uncached_input_tokens", summary_input)
            ),
            "summary_output_tokens": summary_output,
            "embedding_input_tokens": embedding_input,
            "total_api_tokens": summary_input + summary_output + embedding_input,
            "summary_cost_cny": round(summary_cost, 6),
            "embedding_cost_cny": round(embedding_cost, 6),
            "total_cost_cny": round(summary_cost + embedding_cost, 6),
            "model_response_cache_hits": int(summary_report.get("cache_hits", 0)),
        }
        return {
            "status": "complete",
            "build_id": build_id,
            "kind": kind,
            "database": str(database),
            "build_dir": str(build_dir),
            "promoted": promote,
            "plan": build_plan,
            "usage": usage,
            "storage": actual_managed_storage(config, database),
            "audit": read_json(build_dir / "audit.json"),
            "run": read_json(run.path),
        }
    finally:
        connection.close()


def build_full(
    config: ProfileConfig,
    *,
    promote: bool = True,
    max_cost_cny: float | None = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    if acquire_lock:
        with file_lock(config.lock_path):
            return _build_locked(
                config, kind="full", promote=promote, max_cost_cny=max_cost_cny
            )
    return _build_locked(config, kind="full", promote=promote, max_cost_cny=max_cost_cny)


def update_incremental(
    config: ProfileConfig,
    *,
    promote: bool = True,
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    if not active_database(config):
        raise RuntimeError("No active build. Run a full build first.")
    active = active_info(config) or {}
    if active.get("migrated_from"):
        raise RuntimeError(
            "The active database is a query-compatible legacy migration, not a "
            "canonical incremental baseline. Run `codex-history build` once; the "
            "migrated build remains preserved for rollback and comparison."
        )
    with file_lock(config.lock_path):
        return _build_locked(
            config, kind="incremental", promote=promote, max_cost_cny=max_cost_cny
        )


def equivalence_audit(config: ProfileConfig, *, keep_reference: bool = False) -> dict[str, Any]:
    active = active_database(config)
    if not active:
        raise RuntimeError("No active build to compare")
    with file_lock(config.lock_path):
        reference = build_full(config, promote=False, acquire_lock=False)
        result = compare_databases(active, Path(reference["database"]))
        result.update(
            {
                "active_database": str(active),
                "reference_database": reference["database"],
                "reference_build_id": reference["build_id"],
            }
        )
        report_path = config.reports_dir / f"equivalence-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
        atomic_write_json(report_path, result)
        result["report_path"] = str(report_path)
        if not keep_reference:
            shutil.rmtree(reference["build_dir"], ignore_errors=True)
        return result
