from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .audit import audit_connection, compare_databases
from .artifacts import inspect_artifact_closure
from .config import ProfileConfig, config_path, ensure_profile_dirs, resolve_summarization
from .estimate import actual_managed_storage, estimate_build
from .knowledge import (
    append_parsed_thread,
    delete_thread,
    hydrate_parsed_thread,
    insert_parsed_thread,
    insert_source_snapshot,
    rebuild_conservative_relations,
    rebuild_family_scopes,
    refresh_evidence_rollups,
)
from .operations import (
    prune_profile_builds,
    require_resource_preflight,
    resource_preflight,
    usage_summary,
)
from .parser import parse_snapshot
from .schema import (
    connect,
    initialize,
    rebuild_fts,
    restore_knowledge_fts_triggers,
    suspend_knowledge_fts_triggers,
)
from .source import (
    SourceCandidate,
    SourceChange,
    classify_changes,
    discover_sources,
    previous_sources,
    snapshot_appended_source,
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


def _prune_unreferenced_profile_artifacts(
    config: ProfileConfig, connection: sqlite3.Connection
) -> dict[str, int]:
    referenced = {
        str(row[0]).replace("\\", "/").removeprefix("cas/")
        for row in connection.execute("SELECT DISTINCT cas_relative_path FROM artifact_files")
    }
    removed_files = 0
    removed_bytes = 0
    if config.cas_dir.is_dir():
        for path in config.cas_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(config.cas_dir).as_posix()
            if relative in referenced:
                continue
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1
    return {"removed_files": removed_files, "removed_bytes": removed_bytes}


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
            pending_row = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM knowledge "
                "WHERE tier='fact_block' "
                "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1 "
                "AND COALESCE(json_extract(metadata_json,'$.promotion_eligible'),1)!=0 "
                "AND COALESCE(json_extract(metadata_json,'$.model_consolidated_build_id'),'')=''"
            ).fetchone()
            pending_model_records = int(pending_row[0])
            pending_model_chars = int(pending_row[1])
            pending_thread_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT scope_id FROM knowledge WHERE tier='fact_block' "
                    "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1 "
                    "AND COALESCE(json_extract(metadata_json,'$.promotion_eligible'),1)!=0 "
                    "AND COALESCE(json_extract(metadata_json,'$.model_consolidated_build_id'),'')=''"
                )
            }
        finally:
            connection.close()
    else:
        pending_model_records = 0
        pending_model_chars = 0
        pending_thread_ids = set()
    if mode == "full" or not database:
        changes = [SourceChange("added", source, None, "full build") for source in sources]
        pending_model_records = 0
        pending_model_chars = 0
        pending_thread_ids = set()
    else:
        changes = classify_changes(sources, previous)
    actionable = [change for change in changes if change.kind != "unchanged"]
    affected_thread_ids = pending_thread_ids | {
        str(change.source.thread_id if change.source else change.previous.get("thread_id"))
        for change in actionable
        if change.source or change.previous
    }
    affected_scope_count = 0
    writer_context_chars = 0
    if database and affected_thread_ids:
        connection = connect(database, readonly=True)
        try:
            placeholders = ",".join("?" for _ in affected_thread_ids)
            affected_scope_ids = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT scope_id FROM scope_threads "
                    f"WHERE thread_id IN ({placeholders})",
                    sorted(affected_thread_ids),
                )
            }
            missing_direct = affected_thread_ids - {
                str(row[0])
                for row in connection.execute(
                    f"SELECT scope_id FROM scopes WHERE scope_type='thread' "
                    f"AND scope_id IN ({placeholders})",
                    sorted(affected_thread_ids),
                )
            }
            affected_scope_count = len(affected_scope_ids) + len(missing_direct)
            if affected_scope_ids:
                scope_placeholders = ",".join("?" for _ in affected_scope_ids)
                writer_context_chars = int(
                    connection.execute(
                        f"SELECT COALESCE(SUM(length(text)),0) FROM knowledge "
                        f"WHERE scope_id IN ({scope_placeholders}) AND tier IN ('ledger','overview')",
                        sorted(affected_scope_ids),
                    ).fetchone()[0]
                )
            writer_context_chars += len(missing_direct) * 1_000
        finally:
            connection.close()
    affected_scope_count = max(affected_scope_count, len(actionable))
    if mode == "full":
        affected_scope_count = max(affected_scope_count, len(sources) * 2)
        writer_context_chars = 0
    summarization = resolve_summarization(config)
    estimate = estimate_build(
        config,
        sources=sources,
        changes=changes,
        database=database,
        summarization=summarization,
        pending_model_records=pending_model_records,
        pending_model_chars=pending_model_chars,
        affected_scope_count=affected_scope_count,
        writer_context_chars=writer_context_chars,
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
    if pending_model_records:
        warnings.append(
            f"{pending_model_records} incrementally ingested fact blocks are waiting for model consolidation."
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
    resources = resource_preflight(
        config,
        database=database,
        estimate=estimate,
        operation=mode,
    )
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
        "pending_model_records": pending_model_records,
        "pending_model_chars": pending_model_chars,
        "work_required": bool(actionable)
        or bool(
            pending_model_records
            and summarization["effective_mode"] == "openai-compatible"
        ),
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
            summary_tokens["output_expected"]
            + summary_tokens.get("writer_output_expected", 0)
            if summary_tokens["would_call_model"]
            else 0
        ),
        "estimated_output_tokens_if_model_enabled": summary_tokens["output_expected"]
        + summary_tokens.get("writer_output_expected", 0),
        "estimated_embedding_tokens_upper_bound": token_estimate["embedding_input_upper"],
        "estimated_embedding_tokens_expected": token_estimate[
            "embedding_input_expected"
        ],
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
        "resource_preflight": resources,
        "usage_ledger": usage_summary(config),
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
    if kind in {"incremental", "hydrate", "compact", "artifact", "delta", "repair"}:
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


def pollution_repair_plan(config: ProfileConfig) -> dict[str, Any]:
    ensure_profile_dirs(config)
    database = active_database(config)
    if not database:
        raise RuntimeError("Pollution repair requires an active build")
    from .pollution import pollution_audit

    connection = connect(database, readonly=True)
    try:
        audit = pollution_audit(connection)
        pending_row = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM knowledge "
            "WHERE tier='fact_block' "
            "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1 "
            "AND COALESCE(json_extract(metadata_json,'$.promotion_eligible'),1)!=0 "
            "AND COALESCE(json_extract(metadata_json,'$.model_consolidated_build_id'),'')=''"
        ).fetchone()
        pending_threads = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT scope_id FROM knowledge WHERE tier='fact_block' "
                "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1 "
                "AND COALESCE(json_extract(metadata_json,'$.promotion_eligible'),1)!=0 "
                "AND COALESCE(json_extract(metadata_json,'$.model_consolidated_build_id'),'')=''"
            )
        }
        from .summarize import _affected_scopes

        affected = _affected_scopes(connection, pending_threads)
        affected_scope_ids = [str(row["scope_id"]) for row in affected]
        writer_context_chars = 0
        if affected_scope_ids:
            placeholders = ",".join("?" for _ in affected_scope_ids)
            writer_context_chars = int(
                connection.execute(
                    f"SELECT COALESCE(SUM(length(text)),0) FROM knowledge "
                    f"WHERE scope_id IN ({placeholders}) "
                    "AND tier IN ('ledger','overview')",
                    affected_scope_ids,
                ).fetchone()[0]
            )
    finally:
        connection.close()
    semantic_bytes = 0
    semantic_root = config.root / "semantic/chroma"
    if semantic_root.is_dir():
        semantic_bytes = sum(
            path.stat().st_size for path in semantic_root.rglob("*") if path.is_file()
        )
    free_bytes = shutil.disk_usage(config.root).free
    required_free_bytes = (
        database.stat().st_size + semantic_bytes + config.runtime_min_free_bytes
    )
    pending_fact_blocks = int(pending_row[0])
    fact_chars = int(pending_row[1])
    estimated_summary_input_tokens = max(0, fact_chars // 3 * 2)
    estimated_summary_output_tokens = max(0, int(estimated_summary_input_tokens * 0.10))
    estimated_reducer_cost = (
        estimated_summary_input_tokens / 1_000_000 * config.summary_input_price_cny
        + estimated_summary_output_tokens / 1_000_000 * config.summary_output_price_cny
    )
    affected_scope_count = len(affected_scope_ids)
    estimated_writer_input_tokens = (
        writer_context_chars // 2
        + estimated_summary_output_tokens
        + affected_scope_count * 1_500
    )
    estimated_writer_output_tokens = affected_scope_count * 3_500
    estimated_writer_cost = (
        estimated_writer_input_tokens / 1_000_000 * config.writer_input_price_cny
        + estimated_writer_output_tokens / 1_000_000 * config.writer_output_price_cny
    )
    estimated_embedding_tokens = (
        estimated_summary_output_tokens + estimated_writer_output_tokens
        if config.embedding_enabled
        else 0
    )
    estimated_embedding_cost = (
        estimated_embedding_tokens
        / 1_000_000
        * config.embedding_input_price_cny
    )
    estimated_total_cost = (
        estimated_reducer_cost + estimated_writer_cost + estimated_embedding_cost
    )
    return {
        "schema_version": "codex-history-pollution-repair-plan-v1",
        "created_at": utc_now(),
        "database": str(database),
        "active_build_id": (active_info(config) or {}).get("build_id"),
        "audit": audit,
        "work_required": bool(audit["repair_required"]),
        "pending_model_fact_blocks": pending_fact_blocks,
        "estimated_summary_input_tokens_upper_bound": estimated_summary_input_tokens,
        "estimated_summary_output_tokens": estimated_summary_output_tokens,
        "affected_scope_count": affected_scope_count,
        "writer_context_chars": writer_context_chars,
        "estimated_writer_input_tokens_upper_bound": estimated_writer_input_tokens,
        "estimated_writer_output_tokens": estimated_writer_output_tokens,
        "estimated_reducer_cost_cny": round(estimated_reducer_cost, 6),
        "estimated_writer_cost_cny": round(estimated_writer_cost, 6),
        "estimated_embedding_cost_cny": round(estimated_embedding_cost, 6),
        "estimated_summary_cost_cny": round(
            estimated_reducer_cost + estimated_writer_cost, 6
        ),
        "estimated_cost_cny": round(estimated_total_cost, 6),
        "resource_preflight": {
            "passed": free_bytes >= required_free_bytes,
            "free_bytes": free_bytes,
            "required_free_bytes": required_free_bytes,
            "database_clone_bytes": database.stat().st_size,
            "semantic_clone_bytes": semantic_bytes,
        },
        "summarization": resolve_summarization(config),
    }


def repair_knowledge_pollution(
    config: ProfileConfig,
    *,
    promote: bool = True,
    max_cost_cny: float | None,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    with file_lock(config.lock_path):
        repair_plan = pollution_repair_plan(config)
        if not repair_plan["work_required"]:
            return {"status": "clean", "plan": repair_plan}
        if not repair_plan["resource_preflight"]["passed"]:
            raise RuntimeError("Insufficient free space for a rollback-safe pollution repair")
        if (
            repair_plan["pending_model_fact_blocks"]
            and repair_plan["summarization"]["effective_mode"] != "openai-compatible"
        ):
            raise RuntimeError(
                "Pollution repair requires configured model summarization; refusing a lossy extractive repair"
            )
        if max_cost_cny is None and repair_plan["estimated_cost_cny"] > 0:
            raise RuntimeError(
                "Pollution repair can call paid APIs; pass an explicit --max-cost-cny limit"
            )
        if (
            max_cost_cny is not None
            and repair_plan["estimated_cost_cny"] > max_cost_cny
        ):
            raise RuntimeError(
                f"Estimated repair cost {repair_plan['estimated_cost_cny']:.6f} CNY "
                f"exceeds limit {max_cost_cny:.6f} CNY"
            )

        current = active_info(config) or {}
        build_id = _build_id("pollution-repair")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "source-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "repair",
            str(current.get("build_id") or "") or None,
            manifest_path,
        )
        run = RunState(config, build_id, "repair", build_dir)
        semantic_candidate: Path | None = None
        try:
            from .pollution import (
                finalize_pollution_repair,
                pollution_audit,
                prepare_pollution_repair,
            )
            from .summarize import summarize_incremental

            with run.stage(connection, "discover") as report:
                atomic_write_json(
                    manifest_path,
                    {
                        "schema_version": "codex-history-pollution-repair-manifest-v1",
                        "build_id": build_id,
                        "created_at": utc_now(),
                        "parent_build_id": current.get("build_id"),
                        "source_database": repair_plan["database"],
                        "before": repair_plan["audit"],
                    },
                )
                report.update(repair_plan["audit"])
            with run.stage(connection, "snapshot") as report:
                report.update(
                    {
                        "rollback_safe": True,
                        "parent_build_retained": True,
                        "database_clone": str(database),
                    }
                )
            with run.stage(connection, "ingest") as report:
                suspend_knowledge_fts_triggers(connection)
                report["knowledge_fts_triggers_suspended"] = True
                preparation = prepare_pollution_repair(
                    connection, build_id=build_id
                )
                report.update(preparation)
            with run.stage(connection, "lineage") as report:
                report.update(
                    {
                        "preserved_raw_evidence": True,
                        "preserved_canonical_events": True,
                        "preserved_artifacts": True,
                    }
                )
            with run.stage(connection, "summarize") as report:
                report.update(
                    summarize_incremental(
                        config,
                        connection,
                        build_id=build_id,
                        max_cost_cny=max_cost_cny,
                    )
                )
            with run.stage(connection, "index") as report:
                affected_scopes = list(
                    run.data["stages"]["ingest"]["report"].get(
                        "affected_scopes", []
                    )
                )
                report.update(
                    finalize_pollution_repair(
                        connection,
                        build_id=build_id,
                        affected_scopes=affected_scopes,
                    )
                )
                report["relations"] = rebuild_conservative_relations(connection)
                restore_knowledge_fts_triggers(connection)
                rebuild_fts(connection)
                report["knowledge_fts_rebuilt_from_authority"] = True
                semantic_report: dict[str, Any] = {"enabled": False}
                if config.embedding_enabled:
                    from .semantic import refresh_embeddings

                    semantic_candidate = _prepare_semantic_candidate(config, build_dir)
                    summary_cost = float(
                        run.data["stages"]["summarize"]["report"].get(
                            "cost_cny", 0.0
                        )
                    )
                    semantic_report = refresh_embeddings(
                        config,
                        connection,
                        max_cost_cny=(
                            None
                            if max_cost_cny is None
                            else max(0.0, max_cost_cny - summary_cost)
                        ),
                        chroma_path=semantic_candidate,
                    )
                report["semantic"] = semantic_report
                report["knowledge"] = connection.execute(
                    "SELECT COUNT(*) FROM knowledge"
                ).fetchone()[0]
            with run.stage(connection, "audit") as report:
                audit = audit_connection(connection)
                closure, _ = inspect_artifact_closure(
                    config, database, verify_hashes=False
                )
                audit["artifact_closure"] = closure
                audit["passed"] = audit["passed"] and closure["complete"]
                audit["pollution"] = pollution_audit(connection)
                report.update(audit)
                atomic_write_json(build_dir / "audit.json", audit)
                if not audit["passed"] or audit["pollution"]["repair_required"]:
                    raise RuntimeError("Pollution repair audit failed")
                connection.execute(
                    "UPDATE builds SET logical_digest=? WHERE build_id=?",
                    (audit["logical_digest"]["sha256"], build_id),
                )
            with run.stage(connection, "promote") as report:
                if promote:
                    completion = connection.execute(
                        "SELECT value FROM metadata WHERE key='knowledge_completion_status'"
                    ).fetchone()
                    report.update(
                        _promote(
                            config,
                            build_id,
                            database,
                            semantic_candidate=semantic_candidate,
                            completion_status=str(completion[0]) if completion else "unknown",
                        )
                    )
                    promoted_at = report["promoted_at"]
                else:
                    report["promoted"] = False
                    promoted_at = None
                connection.execute(
                    "UPDATE builds SET status='complete',completed_at=?,promoted_at=? WHERE build_id=?",
                    (utc_now(), promoted_at, build_id),
                )
            run.complete()
            summary = run.data["stages"]["summarize"]["report"]
            semantic = run.data["stages"]["index"]["report"].get("semantic", {})
            return {
                "status": "complete",
                "kind": "pollution-repair",
                "build_id": build_id,
                "database": str(database),
                "promoted": promote,
                "plan": repair_plan,
                "usage": {
                    "summary_input_tokens": int(summary.get("input_tokens", 0)),
                    "summary_cached_input_tokens": int(summary.get("cached_input_tokens", 0)),
                    "summary_output_tokens": int(summary.get("output_tokens", 0)),
                    "embedding_input_tokens": int(semantic.get("input_tokens", 0)),
                    "summary_cost_cny": float(summary.get("cost_cny", 0.0)),
                    "embedding_cost_cny": float(semantic.get("actual_cost_cny", 0.0)),
                },
                "audit": read_json(build_dir / "audit.json"),
                "run": read_json(run.path),
            }
        finally:
            connection.close()


def _prepare_semantic_candidate(config: ProfileConfig, build_dir: Path) -> Path:
    source = config.root / "semantic/chroma"
    candidate = build_dir / "semantic-candidate/chroma"
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, candidate)
    else:
        candidate.mkdir(parents=True)
    return candidate


def _promote(
    config: ProfileConfig,
    build_id: str,
    database: Path,
    *,
    semantic_candidate: Path | None = None,
    completion_status: str = "unknown",
) -> dict[str, Any]:
    if completion_status == "unknown":
        status_connection = connect(database, readonly=True)
        try:
            row = status_connection.execute(
                "SELECT value FROM metadata WHERE key='knowledge_completion_status'"
            ).fetchone()
            if row:
                completion_status = str(row[0])
        finally:
            status_connection.close()
    relative = database.relative_to(config.root).as_posix()
    payload = {
        "schema_version": "codex-history-active-v1",
        "profile": config.name,
        "build_id": build_id,
        "database": relative,
        "promoted_at": utc_now(),
        "incremental_ready": True,
        "knowledge_completion_status": completion_status,
    }
    semantic_target = config.root / "semantic/chroma"
    semantic_previous = config.root / f"semantic/.previous-{build_id}"
    semantic_swapped = False
    try:
        if semantic_candidate is not None:
            shutil.rmtree(semantic_previous, ignore_errors=True)
            if semantic_target.exists():
                os.replace(semantic_target, semantic_previous)
            semantic_target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(semantic_candidate, semantic_target)
            semantic_swapped = True
        atomic_write_json(config.active_path, payload)
    except BaseException:
        if semantic_swapped:
            shutil.rmtree(semantic_target, ignore_errors=True)
            if semantic_previous.exists():
                os.replace(semantic_previous, semantic_target)
        raise
    else:
        shutil.rmtree(semantic_previous, ignore_errors=True)
    return {
        **payload,
        "retention": prune_profile_builds(config, active_build_id=build_id),
    }


def _build_locked(
    config: ProfileConfig,
    *,
    kind: str,
    promote: bool,
    max_cost_cny: float | None,
) -> dict[str, Any]:
    build_plan = plan(config, mode="full" if kind == "full" else "incremental")
    require_resource_preflight(build_plan["resource_preflight"])
    if max_cost_cny is None and build_plan["estimated_cost_cny"] > 0:
        raise RuntimeError(
            "This build can call paid APIs. Review `codex-history plan --json` and pass an "
            "explicit --max-cost-cny limit."
        )
    if max_cost_cny is not None and build_plan["estimated_cost_cny"] > max_cost_cny:
        raise RuntimeError(
            f"Estimated cost {build_plan['estimated_cost_cny']:.6f} CNY exceeds limit {max_cost_cny:.6f} CNY"
        )
    if kind == "incremental" and not build_plan["work_required"]:
        return {
            "status": (
                "pending_model_consolidation"
                if build_plan.get("pending_model_records")
                else "no_changes"
            ),
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
    semantic_candidate: Path | None = None
    hydrated_incremental = bool(
        kind == "incremental"
        and connection.execute(
            "SELECT 1 FROM metadata WHERE key='canonical_snapshot_complete' AND value='true'"
        ).fetchone()
    )
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
            append_reused_prefix = 0
            append_snapshot_fallback = 0
            for change in changes:
                if change.kind in {"added", "appended", "rewritten"} and change.source:
                    snapshot = None
                    if (
                        hydrated_incremental
                        and change.kind == "appended"
                        and change.previous
                        and change.source.declared_size_bytes is None
                    ):
                        old_chunks = [
                            dict(row)
                            for row in connection.execute(
                                "SELECT chunk_index,chunk_sha256,size_bytes,cas_relative_path "
                                "FROM source_chunks WHERE source_id=? ORDER BY chunk_index",
                                (change.source.source_id,),
                            )
                        ]
                        try:
                            snapshot = snapshot_appended_source(
                                config,
                                change.source,
                                change.previous,
                                old_chunks,
                            )
                        except (OSError, ValueError):
                            append_snapshot_fallback += 1
                        else:
                            append_reused_prefix += 1
                    if snapshot is None:
                        snapshot = snapshot_source(config, change.source)
                    snapshots[change.source.source_id] = snapshot
            manifest = read_json(manifest_path)
            manifest["snapshots"] = [
                {
                    "source_id": source_id,
                    "content_sha256": snapshot.content_sha256,
                    "snapshot_content_sha256": snapshot.snapshot_content_sha256,
                    "snapshot_size_bytes": snapshot.snapshot_size_bytes,
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
                    "append_reused_prefix": append_reused_prefix,
                    "append_snapshot_fallback": append_snapshot_fallback,
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
                "preserved_curated_scopes": 0,
                "append_fast_path": 0,
                "append_fallback": 0,
            }
            for change in changes:
                if change.kind == "unchanged":
                    continue
                old = change.previous
                if hydrated_incremental:
                    if change.kind == "deleted" or not change.source:
                        if old:
                            connection.execute(
                                "UPDATE source_files SET source_state='deleted',last_seen_at=? "
                                "WHERE source_id=?",
                                (utc_now(), old["source_id"]),
                            )
                        continue
                    snapshot = snapshots[change.source.source_id]
                    prior_chunk = connection.execute(
                        "SELECT cas_relative_path FROM source_chunks WHERE source_id=? ORDER BY chunk_index DESC LIMIT 1",
                        (change.source.source_id,),
                    ).fetchone()
                    insert_source_snapshot(connection, snapshot)
                    inserted = None
                    if change.kind == "appended" and old:
                        last_turn = connection.execute(
                            "SELECT turn_seq,status FROM turns WHERE thread_id=? ORDER BY turn_seq DESC LIMIT 1",
                            (change.source.thread_id,),
                        ).fetchone()
                        prior_ended_newline = False
                        if prior_chunk:
                            previous_chunk = config.snapshots_dir / str(prior_chunk[0])
                            if previous_chunk.is_file() and previous_chunk.stat().st_size:
                                with previous_chunk.open("rb") as handle:
                                    handle.seek(-1, os.SEEK_END)
                                    prior_ended_newline = handle.read(1) == b"\n"
                        if last_turn and str(last_turn["status"]) in {"complete", "aborted"} and prior_ended_newline:
                            prior_counts = {
                                str(row[0]): int(row[1])
                                for row in connection.execute(
                                    "SELECT content_sha256,COUNT(*) FROM canonical_events WHERE thread_id=? GROUP BY content_sha256",
                                    (change.source.thread_id,),
                                )
                            }
                            connection.execute("SAVEPOINT append_fast_path")
                            try:
                                parsed = parse_snapshot(
                                    snapshot,
                                    config,
                                    start_line=int(old.get("line_count") or 0),
                                    start_byte=int(old.get("snapshot_size_bytes") or 0),
                                    next_turn_seq=int(last_turn["turn_seq"]) + 1,
                                    prior_content_occurrences=prior_counts,
                                )
                                inserted = append_parsed_thread(
                                    connection,
                                    snapshot,
                                    parsed,
                                    config,
                                    build_id=build_id,
                                )
                            except (sqlite3.IntegrityError, ValueError):
                                connection.execute("ROLLBACK TO append_fast_path")
                                connection.execute("RELEASE append_fast_path")
                                inserted = None
                                totals["append_fallback"] += 1
                            else:
                                connection.execute("RELEASE append_fast_path")
                    if inserted is None:
                        parsed = parse_snapshot(snapshot, config)
                        inserted = hydrate_parsed_thread(
                            connection,
                            snapshot,
                            parsed,
                            config,
                            index_new_knowledge=True,
                            build_id=build_id,
                        )
                    totals["threads"] += 1
                    totals["preserved_curated_scopes"] += int(
                        inserted.pop("preserved_curated_scope", 0)
                    )
                    for key, value in inserted.items():
                        totals[key] += value
                    continue
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
                inserted = insert_parsed_thread(
                    connection,
                    snapshot,
                    parsed,
                    config,
                    ingest_build_id=build_id,
                    incremental_append=kind == "full",
                )
                totals["threads"] += 1
                for key, value in inserted.items():
                    totals[key] += value
            if kind == "full":
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('canonical_snapshot_complete','true') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('knowledge_completion_status','pending_model_consolidation') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
            if (
                config.artifact_capture_paths
                or config.artifact_capture_git_repositories
            ):
                from .artifact_capture import (
                    apply_artifact_capture,
                    plan_artifact_capture,
                )

                artifact_plan = plan_artifact_capture(
                    config,
                    connection,
                    active_build_id=build_id,
                    database=database,
                )
                totals["artifact_capture"] = apply_artifact_capture(
                    config,
                    connection,
                    artifact_plan,
                    build_dir=build_dir,
                )
            report.update(totals)

        with run.stage(connection, "lineage") as report:
            if kind == "full" or hydrated_incremental:
                curated_families = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM scopes WHERE scope_type='family' "
                        "AND human_verdict NOT LIKE 'deterministic%'"
                    ).fetchone()[0]
                )
                if curated_families:
                    refresh_evidence_rollups(connection)
                    report.update(
                        {
                            "preserved": True,
                            "curated_family_scopes": curated_families,
                            "note": "Curated family scopes retained; evidence rollups refreshed",
                        }
                    )
                else:
                    report["family_scopes"] = rebuild_family_scopes(connection, build_id)
                report["relations"] = rebuild_conservative_relations(connection)
            else:
                report["family_scopes"] = rebuild_family_scopes(connection, build_id)
                report["relations"] = rebuild_conservative_relations(connection)

        with run.stage(connection, "summarize") as report:
            from .summarize import summarize_incremental, summarize_scopes

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
            if kind == "full" or hydrated_incremental:
                summary_result = summarize_incremental(
                    config,
                    connection,
                    build_id=build_id,
                    max_cost_cny=max_cost_cny,
                )
            else:
                summary_result = summarize_scopes(
                    config,
                    connection,
                    scope_ids=scope_ids,
                    max_cost_cny=max_cost_cny,
                )
            report.update(summary_result)

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

                semantic_candidate = _prepare_semantic_candidate(config, build_dir)
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
                    chroma_path=semantic_candidate,
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
            artifact_closure, _ = inspect_artifact_closure(
                config,
                database,
                verify_hashes=not hydrated_incremental,
            )
            audit["artifact_closure"] = artifact_closure
            audit["checks"].append(
                {
                    "name": "artifact_closure",
                    "passed": artifact_closure["complete"],
                    "detail": artifact_closure,
                }
            )
            audit["passed"] = audit["passed"] and artifact_closure["complete"]
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
                completion_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='knowledge_completion_status'"
                ).fetchone()
                report.update(
                    _promote(
                        config,
                        build_id,
                        database,
                        semantic_candidate=semantic_candidate,
                        completion_status=(
                            str(completion_row[0]) if completion_row else "unknown"
                        ),
                    )
                )
                promoted_at = report["promoted_at"]
            else:
                report.update({"promoted": False})
                promoted_at = None
            connection.execute(
                "UPDATE builds SET status='complete',completed_at=?,promoted_at=? WHERE build_id=?",
                (utc_now(), promoted_at, build_id),
            )
        run.complete()
        retention = (
            dict(run.data["stages"]["promote"].get("report", {}).get("retention") or {})
            if promote
            else {
                "retained_builds": config.runtime_retained_builds,
                "removed_build_ids": [],
                "removed_build_count": 0,
                "reclaimed_bytes": 0,
                "skipped": "build was not promoted",
            }
        )
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
            "retention": retention,
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


def hydrate_canonical_baseline(
    config: ProfileConfig,
    *,
    promote: bool = True,
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    current_database = active_database(config)
    if not current_database:
        raise RuntimeError("Canonical hydration requires an active migrated knowledge base")
    current_active = active_info(config) or {}
    with file_lock(config.lock_path):
        build_id = _build_id("hydrate")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "source-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "hydrate",
            str(current_active.get("build_id") or "") or None,
            manifest_path,
        )
        run = RunState(config, build_id, "hydrate", build_dir)
        sources = discover_sources(config)
        snapshots: dict[str, Any] = {}
        try:
            with run.stage(connection, "discover") as report:
                atomic_write_json(
                    manifest_path,
                    {
                        "schema_version": "codex-history-source-manifest-v1",
                        "build_id": build_id,
                        "created_at": utc_now(),
                        "mode": "canonical-hydration-preserve-curated-knowledge",
                        "sources": [_source_public(source) for source in sources],
                        "changes": [
                            _change_public(SourceChange("added", source, None, "hydrate"))
                            for source in sources
                        ],
                        "snapshots": [],
                    },
                )
                report.update({"sources": len(sources), "preserve_curated_knowledge": True})

            with run.stage(connection, "snapshot") as report:
                for source in sources:
                    snapshots[source.source_id] = snapshot_source(config, source)
                manifest = read_json(manifest_path)
                manifest["snapshots"] = [
                    {
                        "source_id": source_id,
                        "content_sha256": snapshot.content_sha256,
                        "snapshot_content_sha256": snapshot.snapshot_content_sha256,
                        "snapshot_size_bytes": snapshot.snapshot_size_bytes,
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
                        "snapshot_bytes": sum(
                            item.snapshot_size_bytes for item in snapshots.values()
                        ),
                        "source_bytes": sum(
                            item.source.size_bytes for item in snapshots.values()
                        ),
                        "unique_chunks": len(
                            {
                                chunk["sha256"]
                                for item in snapshots.values()
                                for chunk in item.chunks
                            }
                        ),
                    }
                )

            with run.stage(connection, "ingest") as report:
                current_ids = {source.source_id for source in sources}
                if current_ids:
                    placeholders = ",".join("?" for _ in current_ids)
                    connection.execute(
                        f"UPDATE source_files SET source_state='deleted' "
                        f"WHERE source_id NOT IN ({placeholders})",
                        sorted(current_ids),
                    )
                totals = {
                    "threads": 0,
                    "events": 0,
                    "turns": 0,
                    "evidence": 0,
                    "fact_blocks": 0,
                    "parse_errors": 0,
                    "artifacts": 0,
                    "preserved_curated_scopes": 0,
                }
                for source in sources:
                    snapshot = snapshots[source.source_id]
                    insert_source_snapshot(connection, snapshot)
                    parsed = parse_snapshot(snapshot, config)
                    inserted = hydrate_parsed_thread(
                        connection, snapshot, parsed, config
                    )
                    totals["threads"] += 1
                    totals["preserved_curated_scopes"] += int(
                        inserted.pop("preserved_curated_scope", 0)
                    )
                    for key, value in inserted.items():
                        totals[key] += value
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('canonical_snapshot_complete','true') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('canonical_hydrated_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (utc_now(),),
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('canonical_hydration_build_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (build_id,),
                )
                totals["pruned_profile_artifacts"] = _prune_unreferenced_profile_artifacts(
                    config, connection
                )
                report.update(totals)

            with run.stage(connection, "lineage") as report:
                report.update(
                    {
                        "preserved": True,
                        "note": "Existing curated scopes and fact relations were retained",
                    }
                )
            with run.stage(connection, "summarize") as report:
                report.update(
                    {
                        "mode": "preserve-existing",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0.0,
                    }
                )
            with run.stage(connection, "index") as report:
                rebuild_fts(connection)
                semantic_report: dict[str, Any] = {
                    "enabled": config.embedding_enabled,
                    "mode": "preserve-existing",
                    "embedded": 0,
                    "input_tokens": 0,
                    "actual_cost_cny": 0.0,
                    "status": "partial-until-next-incremental-refresh",
                }
                report.update(
                    {
                        "knowledge": connection.execute(
                            "SELECT COUNT(*) FROM knowledge"
                        ).fetchone()[0],
                        "semantic": semantic_report,
                    }
                )
            with run.stage(connection, "audit") as report:
                audit = audit_connection(connection)
                artifact_closure, _ = inspect_artifact_closure(
                    config, database, verify_hashes=True
                )
                audit["artifact_closure"] = artifact_closure
                audit["checks"].append(
                    {
                        "name": "artifact_closure",
                        "passed": artifact_closure["complete"],
                        "detail": artifact_closure,
                    }
                )
                audit["passed"] = audit["passed"] and artifact_closure["complete"]
                report.update(audit)
                atomic_write_json(build_dir / "audit.json", audit)
                if not audit["passed"]:
                    raise RuntimeError("Canonical hydration audit failed")
                connection.execute(
                    "UPDATE builds SET logical_digest=? WHERE build_id=?",
                    (audit["logical_digest"]["sha256"], build_id),
                )
            with run.stage(connection, "promote") as report:
                if promote:
                    report.update(_promote(config, build_id, database))
                    promoted_at = report["promoted_at"]
                else:
                    report["promoted"] = False
                    promoted_at = None
                connection.execute(
                    "UPDATE builds SET status='complete',completed_at=?,promoted_at=? "
                    "WHERE build_id=?",
                    (utc_now(), promoted_at, build_id),
                )
            run.complete()
            return {
                "status": "complete",
                "build_id": build_id,
                "kind": "hydrate",
                "database": str(database),
                "build_dir": str(build_dir),
                "promoted": promote,
                "audit": read_json(build_dir / "audit.json"),
                "run": read_json(run.path),
            }
        finally:
            connection.close()


def compact_canonical_storage(
    config: ProfileConfig,
    *,
    promote: bool = True,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    current_database = active_database(config)
    if not current_database:
        raise RuntimeError("Canonical compaction requires an active database")
    with file_lock(config.lock_path):
        build_id = _build_id("compact")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "source-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "compact",
            str((active_info(config) or {}).get("build_id") or "") or None,
            manifest_path,
        )
        run = RunState(config, build_id, "compact", build_dir)
        try:
            with run.stage(connection, "discover") as report:
                report.update({"mode": "reuse-active-canonical-sources"})
                atomic_write_json(
                    manifest_path,
                    {
                        "schema_version": "codex-history-source-manifest-v1",
                        "build_id": build_id,
                        "created_at": utc_now(),
                        "mode": "canonical-payload-compaction",
                        "sources": [],
                        "changes": [],
                        "snapshots": [],
                    },
                )
            with run.stage(connection, "snapshot") as report:
                report.update({"reused": True, "snapshot_directory": str(config.snapshots_dir)})
            with run.stage(connection, "ingest") as report:
                before_bytes = database.stat().st_size
                rows = connection.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE raw_json<>'' OR length(text)>16000"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE canonical_events SET raw_json='',text=substr(text,1,16000) "
                    "WHERE raw_json<>'' OR length(text)>16000"
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('canonical_payload_storage','snapshot-offset-v1') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                connection.commit()
                connection.execute("VACUUM")
                report.update(
                    {
                        "compacted_events": int(rows),
                        "before_bytes": before_bytes,
                        "after_bytes": database.stat().st_size,
                        "reclaimed_bytes": before_bytes - database.stat().st_size,
                    }
                )
            with run.stage(connection, "lineage") as report:
                report["preserved"] = True
            with run.stage(connection, "summarize") as report:
                report.update({"mode": "preserve-existing", "cost_cny": 0.0})
            with run.stage(connection, "index") as report:
                report.update({"fts_preserved": True, "semantic_preserved": True})
            with run.stage(connection, "audit") as report:
                audit = audit_connection(connection)
                artifact_closure, _ = inspect_artifact_closure(
                    config, database, verify_hashes=False
                )
                audit["artifact_closure"] = artifact_closure
                audit["checks"].append(
                    {
                        "name": "artifact_closure",
                        "passed": artifact_closure["complete"],
                        "detail": artifact_closure,
                    }
                )
                audit["passed"] = audit["passed"] and artifact_closure["complete"]
                report.update(audit)
                atomic_write_json(build_dir / "audit.json", audit)
                if not audit["passed"]:
                    raise RuntimeError("Canonical storage compaction audit failed")
                connection.execute(
                    "UPDATE builds SET logical_digest=? WHERE build_id=?",
                    (audit["logical_digest"]["sha256"], build_id),
                )
            with run.stage(connection, "promote") as report:
                if promote:
                    report.update(_promote(config, build_id, database))
                    promoted_at = report["promoted_at"]
                else:
                    report["promoted"] = False
                    promoted_at = None
                connection.execute(
                    "UPDATE builds SET status='complete',completed_at=?,promoted_at=? "
                    "WHERE build_id=?",
                    (utc_now(), promoted_at, build_id),
                )
            run.complete()
            return {
                "status": "complete",
                "build_id": build_id,
                "kind": "compact",
                "database": str(database),
                "build_dir": str(build_dir),
                "promoted": promote,
                "compaction": run.data["stages"]["ingest"]["report"],
                "audit": read_json(build_dir / "audit.json"),
            }
        finally:
            connection.close()


def artifact_capture_plan(
    config: ProfileConfig,
    *,
    since: str = "",
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    database = active_database(config)
    active = active_info(config) or {}
    if not database:
        raise RuntimeError("Artifact capture planning requires an active database")
    from .artifact_capture import plan_artifact_capture

    connection = connect(database, readonly=True)
    try:
        return plan_artifact_capture(
            config,
            connection,
            active_build_id=str(active.get("build_id") or ""),
            database=database,
            since=since,
        ).public()
    finally:
        connection.close()


def capture_artifacts(
    config: ProfileConfig,
    *,
    since: str = "",
    promote: bool = True,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    current_database = active_database(config)
    current_active = active_info(config) or {}
    if not current_database:
        raise RuntimeError("Artifact capture requires an active database")
    if not (
        config.artifact_capture_paths
        or config.artifact_capture_git_repositories
    ):
        raise RuntimeError(
            "Artifact capture is disabled. Enable artifacts.capture_existing_paths "
            "or artifacts.capture_git_repositories, then run artifact-plan again."
        )
    from .artifact_capture import apply_artifact_capture, plan_artifact_capture

    with file_lock(config.lock_path):
        planning_connection = connect(current_database, readonly=True)
        try:
            capture_plan = plan_artifact_capture(
                config,
                planning_connection,
                active_build_id=str(current_active.get("build_id") or ""),
                database=current_database,
                since=since,
            )
        finally:
            planning_connection.close()
        public_plan = capture_plan.public()
        if not capture_plan.work_required:
            return {
                "status": "no_changes",
                "build_id": current_active.get("build_id"),
                "database": str(current_database),
                "plan": public_plan,
                "usage": {
                    "total_api_tokens": 0,
                    "total_cost_cny": 0.0,
                },
            }

        build_id = _build_id("artifact")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "artifact-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "artifact",
            str(current_active.get("build_id") or "") or None,
            manifest_path,
        )
        run = RunState(config, build_id, "artifact", build_dir)
        try:
            with run.stage(connection, "discover") as report:
                report.update(public_plan)
                atomic_write_json(
                    manifest_path,
                    {
                        "schema_version": "codex-history-artifact-manifest-v1",
                        "build_id": build_id,
                        "parent_build_id": current_active.get("build_id"),
                        "created_at": utc_now(),
                        "since": since,
                        "plan": public_plan,
                    },
                )
            with run.stage(connection, "snapshot") as report:
                report.update(
                    {
                        "mode": "content-addressed-file-and-repository-checkpoints",
                        "ordinary_files": len(capture_plan.files),
                        "git_repositories": len(capture_plan.repositories),
                    }
                )
            with run.stage(connection, "ingest") as report:
                report.update(
                    apply_artifact_capture(
                        config,
                        connection,
                        capture_plan,
                        build_dir=build_dir,
                    )
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('artifact_capture_last_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (utc_now(),),
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('artifact_capture_build_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (build_id,),
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('artifact_capture_since',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (since,),
                )
            with run.stage(connection, "lineage") as report:
                report.update(
                    {
                        "preserved": True,
                        "note": "Knowledge, evidence, scopes, and relations were preserved",
                    }
                )
            with run.stage(connection, "summarize") as report:
                report.update(
                    {
                        "mode": "preserve-existing",
                        "model_calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0.0,
                    }
                )
            with run.stage(connection, "index") as report:
                rebuild_fts(connection)
                report.update(
                    {
                        "artifact_fts_rebuilt": True,
                        "semantic_preserved": True,
                        "embedding_calls": 0,
                    }
                )
            with run.stage(connection, "audit") as report:
                audit = audit_connection(connection)
                artifact_closure, _ = inspect_artifact_closure(
                    config,
                    database,
                    verify_hashes=False,
                )
                audit["artifact_closure"] = artifact_closure
                audit["checks"].append(
                    {
                        "name": "artifact_closure",
                        "passed": artifact_closure["complete"],
                        "detail": artifact_closure,
                    }
                )
                audit["passed"] = audit["passed"] and artifact_closure["complete"]
                report.update(audit)
                atomic_write_json(build_dir / "audit.json", audit)
                if not audit["passed"]:
                    raise RuntimeError("Artifact-only build audit failed")
                connection.execute(
                    "UPDATE builds SET logical_digest=? WHERE build_id=?",
                    (audit["logical_digest"]["sha256"], build_id),
                )
            with run.stage(connection, "promote") as report:
                if promote:
                    report.update(_promote(config, build_id, database))
                    promoted_at = report["promoted_at"]
                else:
                    report["promoted"] = False
                    promoted_at = None
                connection.execute(
                    "UPDATE builds SET status='complete',completed_at=?,promoted_at=? "
                    "WHERE build_id=?",
                    (utc_now(), promoted_at, build_id),
                )
            run.complete()
            return {
                "status": "complete",
                "build_id": build_id,
                "kind": "artifact",
                "database": str(database),
                "build_dir": str(build_dir),
                "promoted": promote,
                "plan": public_plan,
                "capture": run.data["stages"]["ingest"]["report"],
                "usage": {
                    "summary_input_tokens": 0,
                    "summary_output_tokens": 0,
                    "embedding_input_tokens": 0,
                    "total_api_tokens": 0,
                    "summary_cost_cny": 0.0,
                    "embedding_cost_cny": 0.0,
                    "total_cost_cny": 0.0,
                },
                "storage": actual_managed_storage(config, database),
                "audit": read_json(build_dir / "audit.json"),
                "run": read_json(run.path),
            }
        finally:
            connection.close()


def apply_artifact_metadata_build(
    config: ProfileConfig,
    payload: dict[str, Any],
    *,
    promote: bool = True,
) -> dict[str, Any]:
    ensure_profile_dirs(config)
    current_database = active_database(config)
    current_active = active_info(config) or {}
    if not current_database:
        raise RuntimeError("Artifact metadata application requires an active database")
    from .artifact_capture import apply_artifact_metadata

    with file_lock(config.lock_path):
        build_id = _build_id("artifact")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "artifact-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "artifact",
            str(current_active.get("build_id") or "") or None,
            manifest_path,
        )
        run = RunState(config, build_id, "artifact", build_dir)
        try:
            with run.stage(connection, "discover") as report:
                report.update(
                    {
                        "mode": "portable-artifact-metadata",
                        "schema_version": payload.get("schema_version"),
                        "digest": payload.get("digest"),
                        "counts": payload.get("counts", {}),
                    }
                )
                atomic_write_json(
                    manifest_path,
                    {
                        "schema_version": "codex-history-artifact-manifest-v1",
                        "build_id": build_id,
                        "parent_build_id": current_active.get("build_id"),
                        "created_at": utc_now(),
                        "mode": "portable-artifact-metadata",
                        "metadata_digest": payload.get("digest"),
                    },
                )
            with run.stage(connection, "snapshot") as report:
                report.update(
                    {
                        "mode": "reuse-installed-content-addressed-artifacts",
                        "copied_transcript_bytes": 0,
                    }
                )
            with run.stage(connection, "ingest") as report:
                report.update(apply_artifact_metadata(connection, payload))
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('artifact_metadata_last_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (utc_now(),),
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('artifact_metadata_digest',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(payload.get("digest") or ""),),
                )
            with run.stage(connection, "lineage") as report:
                report["preserved"] = True
            with run.stage(connection, "summarize") as report:
                report.update(
                    {
                        "mode": "preserve-existing",
                        "model_calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0.0,
                    }
                )
            with run.stage(connection, "index") as report:
                rebuild_fts(connection)
                report.update(
                    {
                        "artifact_fts_rebuilt": True,
                        "semantic_preserved": True,
                        "embedding_calls": 0,
                    }
                )
            with run.stage(connection, "audit") as report:
                audit = audit_connection(connection)
                artifact_closure, _ = inspect_artifact_closure(
                    config,
                    database,
                    verify_hashes=False,
                )
                audit["artifact_closure"] = artifact_closure
                audit["checks"].append(
                    {
                        "name": "artifact_closure",
                        "passed": artifact_closure["complete"],
                        "detail": artifact_closure,
                    }
                )
                audit["passed"] = audit["passed"] and artifact_closure["complete"]
                report.update(audit)
                atomic_write_json(build_dir / "audit.json", audit)
                if not audit["passed"]:
                    raise RuntimeError("Portable artifact metadata audit failed")
                connection.execute(
                    "UPDATE builds SET logical_digest=? WHERE build_id=?",
                    (audit["logical_digest"]["sha256"], build_id),
                )
            with run.stage(connection, "promote") as report:
                if promote:
                    report.update(_promote(config, build_id, database))
                    promoted_at = report["promoted_at"]
                else:
                    report["promoted"] = False
                    promoted_at = None
                connection.execute(
                    "UPDATE builds SET status='complete',completed_at=?,promoted_at=? "
                    "WHERE build_id=?",
                    (utc_now(), promoted_at, build_id),
                )
            run.complete()
            return {
                "status": "complete",
                "build_id": build_id,
                "kind": "artifact",
                "database": str(database),
                "promoted": promote,
                "metadata": run.data["stages"]["ingest"]["report"],
                "usage": {
                    "total_api_tokens": 0,
                    "total_cost_cny": 0.0,
                },
                "audit": read_json(build_dir / "audit.json"),
            }
        finally:
            connection.close()


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


def resume_latest_failed(
    config: ProfileConfig,
    *,
    max_cost_cny: float | None = None,
) -> dict[str, Any]:
    failed: list[tuple[str, dict[str, Any]]] = []
    if config.runs_dir.is_dir():
        for path in config.runs_dir.glob("*/run.json"):
            value = read_json(path, {}) or {}
            if value.get("status") == "failed":
                failed.append((str(value.get("started_at") or ""), value))
    if not failed:
        raise RuntimeError("No failed build run is available to resume")
    previous = max(failed, key=lambda item: item[0])[1]
    result = update_incremental(config, max_cost_cny=max_cost_cny)
    result["recovery"] = {
        "mode": "checkpoint-assisted-retry",
        "resumed_from_build_id": previous.get("build_id"),
        "reused": [
            "content-addressed transcript snapshots",
            "artifact CAS",
            "successful model response cache entries",
        ],
        "note": (
            "Database stage transactions restart in a fresh candidate; immutable CAS and paid "
            "model results are reused so a failed candidate cannot contaminate the active build."
        ),
    }
    return result


def apply_precomputed_delta_build(
    config: ProfileConfig,
    *,
    authority_stream: Path,
    artifact_stream: Path | None,
    target_source_generation_id: str,
    target_artifact_metadata_digest: str,
    target_logical_digest: str,
    semantic_files: list[tuple[Path, str]],
    semantic_deleted_paths: list[str],
    promote: bool = True,
) -> dict[str, Any]:
    """Apply producer-computed authority and vector state without model calls."""
    from .artifact_transfer import apply_artifact_stream, artifact_metadata_summary
    from .authority_transfer import apply_authority_stream
    from .coverage import source_inventory

    ensure_profile_dirs(config)
    with file_lock(config.lock_path):
        build_plan = plan(config, mode="incremental")
        require_resource_preflight(build_plan["resource_preflight"])
        parent = str((active_info(config) or {}).get("build_id") or "") or None
        build_id = _build_id("delta")
        build_dir = config.builds_dir / build_id
        manifest_path = build_dir / "source-manifest.json"
        database, connection = _new_database(
            config,
            build_id,
            "delta",
            parent,
            manifest_path,
        )
        semantic_candidate: Path | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            with authority_stream.open("rb") as stream:
                authority = apply_authority_stream(connection, stream)
            artifact: dict[str, Any] | None = None
            if artifact_stream is not None:
                with artifact_stream.open("rb") as stream:
                    artifact = apply_artifact_stream(connection, stream)
            inventory = source_inventory(connection)
            if inventory["generation_id"] != target_source_generation_id:
                raise RuntimeError(
                    "Precomputed authority patch did not converge to the target source generation"
                )
            artifact_summary = artifact_metadata_summary(connection)
            if artifact_stream is not None and (
                artifact_summary["digest"] != target_artifact_metadata_digest
            ):
                raise RuntimeError(
                    "Precomputed artifact stream did not converge to the target metadata digest: "
                    f"expected {target_artifact_metadata_digest}, observed {artifact_summary['digest']}"
                )
            violations = list(connection.execute("PRAGMA foreign_key_check"))
            if violations:
                raise RuntimeError(
                    f"Precomputed delta introduced {len(violations)} foreign-key violations"
                )
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"Precomputed delta quick check failed: {integrity}")
            connection.execute(
                "UPDATE builds SET logical_digest=?,notes_json=? WHERE build_id=?",
                (
                    target_logical_digest,
                    canonical_json(
                        {
                            "pipeline_version": PIPELINE_VERSION,
                            "applied_via": "precomputed-delta-v1",
                            "authority_digest": authority["digest"],
                            "artifact_digest": artifact_summary["digest"],
                        }
                    ),
                    build_id,
                ),
            )
            connection.commit()

            if semantic_files or semantic_deleted_paths:
                semantic_candidate = _prepare_semantic_candidate(config, build_dir)
                for archive_path in semantic_deleted_paths:
                    prefix = "data/semantic/chroma/"
                    if archive_path.startswith(prefix):
                        (semantic_candidate / archive_path.removeprefix(prefix)).unlink(
                            missing_ok=True
                        )
                for local_path, archive_path in semantic_files:
                    prefix = "data/semantic/chroma/"
                    if not archive_path.startswith(prefix):
                        continue
                    target = semantic_candidate / archive_path.removeprefix(prefix)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_path, target)

            promoted_at = None
            active_payload: dict[str, Any] = {}
            if promote:
                active_payload = _promote(
                    config,
                    build_id,
                    database,
                    semantic_candidate=semantic_candidate,
                )
                promoted_at = active_payload["promoted_at"]
            connection.execute(
                "UPDATE builds SET status='complete',completed_at=?,promoted_at=? WHERE build_id=?",
                (utc_now(), promoted_at, build_id),
            )
            connection.commit()
            retention = (
                dict(active_payload.get("retention") or {})
                if promote
                else {"skipped": "build was not promoted", "reclaimed_bytes": 0}
            )
            return {
                "status": "complete",
                "mode": "precomputed-delta-v1",
                "build_id": build_id,
                "database": str(database),
                "promoted": promote,
                "authority": authority,
                "artifact": artifact,
                "artifact_metadata": artifact_summary,
                "semantic_changed_files": len(semantic_files),
                "semantic_deleted_files": len(semantic_deleted_paths),
                "usage": {"total_api_tokens": 0, "total_cost_cny": 0.0},
                "audit": {
                    "passed": True,
                    "mode": "quick-precomputed-delta",
                    "sqlite_quick_check": integrity,
                    "foreign_key_violations": 0,
                    "declared_logical_digest": target_logical_digest,
                },
                "retention": retention,
                "active": active_payload,
            }
        except BaseException:
            connection.rollback()
            shutil.rmtree(build_dir, ignore_errors=True)
            raise
        finally:
            connection.close()


def equivalence_audit(
    config: ProfileConfig,
    *,
    keep_reference: bool = False,
    confirm_full_reference: bool = False,
) -> dict[str, Any]:
    if not confirm_full_reference:
        raise RuntimeError(
            "Equivalence audit creates a complete reference database and can require roughly "
            "one additional active-database footprint. Re-run with --confirm-full-reference."
        )
    active = active_database(config)
    if not active:
        raise RuntimeError("No active build to compare")
    with file_lock(config.lock_path):
        reference_config = replace(
            config,
            summary_mode="extractive",
            embedding_enabled=False,
        )
        reference = build_full(reference_config, promote=False, acquire_lock=False)
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
