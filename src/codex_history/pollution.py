from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

from .knowledge import (
    _turn_text,
    history_retrieval_turn,
    history_user_assertion_eligible,
    supersede_noncanonical_overviews,
)
from .util import canonical_json, utc_now


LEGACY_DERIVED_ASSET = "deterministic-asset-classifier-v1"
INCREMENTAL_LEDGER_METHOD = "incremental-ledger-v1"
REPAIR_SCHEMA_VERSION = "codex-history-pollution-repair-v1"
DIRTY_RECORDS_TABLE = "pollution_repair_dirty_records"


def _metadata(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def pollution_audit(connection: sqlite3.Connection) -> dict[str, Any]:
    asset = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(text)),0),"
        "COUNT(DISTINCT json_extract(metadata_json,'$.event_id')) "
        "FROM knowledge WHERE tier='asset' "
        "AND json_extract(metadata_json,'$.derived_by')=?",
        (LEGACY_DERIVED_ASSET,),
    ).fetchone()
    tool_assets = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(k.text)),0) FROM knowledge k "
        "JOIN canonical_events c "
        "ON c.event_id=json_extract(k.metadata_json,'$.event_id') "
        "WHERE k.tier='asset' AND c.role IN ('tool_call','tool_output') "
        "AND json_extract(k.metadata_json,'$.derived_by')=?",
        (LEGACY_DERIVED_ASSET,),
    ).fetchone()
    ledgers = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(text)),0),COUNT(DISTINCT scope_id) "
        "FROM knowledge WHERE tier='ledger' "
        "AND json_extract(metadata_json,'$.method')=? "
        "AND COALESCE(json_extract(metadata_json,'$.promotion_policy'),'')!='evidence-fence-v1'",
        (INCREMENTAL_LEDGER_METHOD,),
    ).fetchone()
    fact_blocks = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(text)),0) FROM knowledge "
        "WHERE tier='fact_block' "
        "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1"
    ).fetchone()
    explicitly_quarantined = connection.execute(
        "SELECT COUNT(*) FROM knowledge "
        "WHERE COALESCE(json_extract(metadata_json,'$.promotion_eligible'),1)=0"
    ).fetchone()[0]
    duplicate_overviews = connection.execute(
        "SELECT COUNT(*),COUNT(DISTINCT old.scope_id) FROM knowledge old "
        "WHERE old.tier='overview' AND old.valid_to IS NULL AND EXISTS("
        "SELECT 1 FROM knowledge current WHERE current.scope_id=old.scope_id "
        "AND current.tier='overview' AND current.valid_to IS NULL "
        "AND current.source_locator LIKE 'model-summary:%' "
        "AND current.record_id!=old.record_id)"
    ).fetchone()
    return {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "created_at": utc_now(),
        "legacy_derived_assets": {
            "records": int(asset[0]),
            "text_chars": int(asset[1]),
            "source_events": int(asset[2]),
            "tool_records": int(tool_assets[0]),
            "tool_text_chars": int(tool_assets[1]),
        },
        "incremental_ledgers": {
            "records": int(ledgers[0]),
            "text_chars": int(ledgers[1]),
            "scopes": int(ledgers[2]),
        },
        "incremental_fact_blocks": {
            "records": int(fact_blocks[0]),
            "text_chars": int(fact_blocks[1]),
        },
        "explicitly_quarantined_records": int(explicitly_quarantined),
        "current_overview_duplicates": {
            "records": int(duplicate_overviews[0]),
            "scopes": int(duplicate_overviews[1]),
        },
        "repair_required": bool(asset[0] or ledgers[0] or duplicate_overviews[0]),
    }


def _turn_events(
    connection: sqlite3.Connection, turn_id: str
) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(**dict(row))
        for row in connection.execute(
            "SELECT event_id,role,text,tool_name,call_id FROM canonical_events "
            "WHERE turn_id=? ORDER BY line_no",
            (turn_id,),
        )
    ]


def _refresh_incremental_fact_blocks(
    connection: sqlite3.Connection, *, build_id: str, reset_model_consolidation: bool
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT record_id,category,status,status_group,metadata_json FROM knowledge "
        "WHERE tier='fact_block' "
        "AND COALESCE(json_extract(metadata_json,'$.incremental_append'),0)=1 "
        "ORDER BY record_id"
    ).fetchall()
    retrieval_echoes = 0
    filtered_outputs = 0
    eligible = 0
    affected_threads: set[str] = set()
    for row in rows:
        metadata = _metadata(str(row["metadata_json"]))
        turn_id = str(metadata.get("turn_id") or "")
        events = _turn_events(connection, turn_id) if turn_id else []
        retrieval_echo, filtered_event_ids = history_retrieval_turn(events)
        turn = connection.execute(
            "SELECT user_text,assistant_text FROM turns WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        user_assertion_only = bool(
            turn
            and history_user_assertion_eligible(
                str(turn["user_text"]), retrieval_echo
            )
        )
        if filtered_event_ids or retrieval_echo:
            if turn:
                text = _turn_text(
                    SimpleNamespace(
                        user_text=str(turn["user_text"]),
                        assistant_text=str(turn["assistant_text"]),
                    ),
                    events,
                )
                connection.execute(
                    "UPDATE knowledge SET text=?,category=?,status=?,status_group=? "
                    "WHERE record_id=?",
                    (
                        text,
                        (
                            "user_context"
                            if user_assertion_only
                            else (
                                "retrieval_query"
                                if retrieval_echo
                                else str(row["category"])
                            )
                        ),
                        "stated_intent" if retrieval_echo else str(row["status"]),
                        "planned" if retrieval_echo else str(row["status_group"]),
                        row["record_id"],
                    ),
                )
            filtered_outputs += len(filtered_event_ids)
        metadata["retrieval_echo"] = retrieval_echo
        metadata["retrieval_echo_event_ids"] = sorted(filtered_event_ids)
        metadata["history_user_assertion_only"] = user_assertion_only
        metadata["promotion_eligible"] = not retrieval_echo or user_assertion_only
        metadata["pollution_repair_build_id"] = build_id
        if reset_model_consolidation:
            metadata.pop("model_consolidated_at", None)
        if retrieval_echo and not user_assertion_only:
            retrieval_echoes += 1
            if reset_model_consolidation:
                metadata["model_consolidated_build_id"] = build_id
        else:
            eligible += 1
            if reset_model_consolidation:
                metadata.pop("model_consolidated_build_id", None)
                scope = connection.execute(
                    "SELECT scope_id FROM knowledge WHERE record_id=?", (row["record_id"],)
                ).fetchone()
                if scope:
                    affected_threads.add(str(scope[0]))
        connection.execute(
            "UPDATE knowledge SET metadata_json=?,indexed_at=? WHERE record_id=?",
            (canonical_json(metadata), utc_now(), row["record_id"]),
        )
    return {
        "fact_blocks": len(rows),
        "eligible_fact_blocks": eligible,
        "retrieval_echo_fact_blocks": retrieval_echoes,
        "filtered_history_events": filtered_outputs,
        "eligible_thread_scopes": sorted(affected_threads),
    }


def _normalize_current_overviews(
    connection: sqlite3.Connection, *, build_id: str
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT scope_id,record_id FROM knowledge WHERE tier='overview' "
        "AND valid_to IS NULL AND source_locator LIKE 'model-summary:%' "
        "ORDER BY asserted_at DESC,record_id"
    ).fetchall()
    normalized_scopes: list[str] = []
    superseded: list[str] = []
    seen: set[str] = set()
    for row in rows:
        scope_id = str(row["scope_id"])
        if scope_id in seen:
            continue
        seen.add(scope_id)
        record_ids = supersede_noncanonical_overviews(
            connection,
            scope_id=scope_id,
            current_record_id=str(row["record_id"]),
            build_id=build_id,
        )
        if record_ids:
            normalized_scopes.append(scope_id)
            superseded.extend(record_ids)
    return {
        "normalized_overview_scopes": normalized_scopes,
        "superseded_overview_records": superseded,
    }


def _remove_polluted_derived_records(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    connection.execute(
        f"CREATE TEMP TABLE {DIRTY_RECORDS_TABLE}(record_id TEXT PRIMARY KEY,tier TEXT NOT NULL)"
    )
    connection.execute(
        f"INSERT INTO {DIRTY_RECORDS_TABLE}(record_id,tier) "
        "SELECT record_id,tier FROM knowledge WHERE "
        "(tier='asset' AND json_extract(metadata_json,'$.derived_by')=?) OR "
        "(tier='ledger' AND json_extract(metadata_json,'$.method')=? "
        "AND COALESCE(json_extract(metadata_json,'$.promotion_policy'),'')!='evidence-fence-v1')",
        (LEGACY_DERIVED_ASSET, INCREMENTAL_LEDGER_METHOD),
    )
    counts = {
        str(row["tier"]): int(row["count"])
        for row in connection.execute(
            f"SELECT tier,COUNT(*) AS count FROM {DIRTY_RECORDS_TABLE} GROUP BY tier"
        )
    }
    dependent_rows: dict[str, int] = {}
    dependent_deletes = {
        "overview_claim_records": (
            "DELETE FROM overview_claim_records WHERE record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE})"
        ),
        "knowledge_relations": (
            "DELETE FROM knowledge_relations WHERE source_record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE}) OR target_record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE})"
        ),
        "relation_candidates": (
            "DELETE FROM relation_candidates WHERE source_record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE}) OR target_record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE})"
        ),
        "knowledge_versions": (
            "DELETE FROM knowledge_versions WHERE record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE})"
        ),
    }
    for table, statement in dependent_deletes.items():
        dependent_rows[table] = int(connection.execute(statement).rowcount)
    removed = int(
        connection.execute(
            "DELETE FROM knowledge WHERE record_id IN "
            f"(SELECT record_id FROM {DIRTY_RECORDS_TABLE})"
        ).rowcount
    )
    connection.execute(f"DROP TABLE {DIRTY_RECORDS_TABLE}")
    return {
        "removed_legacy_derived_assets": counts.get("asset", 0),
        "removed_incremental_ledgers": counts.get("ledger", 0),
        "removed_polluted_records": removed,
        "removed_dependent_rows": dependent_rows,
    }


def prepare_pollution_repair(
    connection: sqlite3.Connection, *, build_id: str
) -> dict[str, Any]:
    before = pollution_audit(connection)
    affected_scopes = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT scope_id FROM knowledge WHERE tier='ledger' "
            "AND json_extract(metadata_json,'$.method')=? "
            "AND COALESCE(json_extract(metadata_json,'$.promotion_policy'),'')!='evidence-fence-v1'",
            (INCREMENTAL_LEDGER_METHOD,),
        )
    }
    removal = _remove_polluted_derived_records(connection)
    fact_blocks = _refresh_incremental_fact_blocks(
        connection,
        build_id=build_id,
        reset_model_consolidation=bool(before["incremental_ledgers"]["records"]),
    )
    overview_lifecycle = _normalize_current_overviews(
        connection, build_id=build_id
    )
    return {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "before": before,
        "affected_scopes": sorted(affected_scopes),
        **removal,
        **fact_blocks,
        **overview_lifecycle,
    }


def finalize_pollution_repair(
    connection: sqlite3.Connection,
    *,
    build_id: str,
    affected_scopes: list[str],
) -> dict[str, Any]:
    refreshed: list[str] = []
    quarantined: list[str] = []
    for scope_id in affected_scopes:
        current = connection.execute(
            "SELECT record_id,metadata_json FROM knowledge WHERE scope_id=? "
            "AND tier='overview' AND source_locator LIKE 'model-summary:%' "
            "ORDER BY asserted_at DESC LIMIT 1",
            (scope_id,),
        ).fetchone()
        if current and _metadata(str(current["metadata_json"])).get("build_id") == build_id:
            refreshed.append(scope_id)
            continue
        rows = connection.execute(
            "SELECT record_id,metadata_json FROM knowledge WHERE scope_id=? "
            "AND tier IN ('overview','asset') AND source_locator LIKE 'model-summary:%'",
            (scope_id,),
        ).fetchall()
        for row in rows:
            metadata = _metadata(str(row["metadata_json"]))
            metadata["retrieval_eligible"] = False
            metadata["quarantine_reason"] = "pollution-repair-no-eligible-source-facts"
            metadata["pollution_repair_build_id"] = build_id
            connection.execute(
                "UPDATE knowledge SET metadata_json=?,indexed_at=? WHERE record_id=?",
                (canonical_json(metadata), utc_now(), row["record_id"]),
            )
        if rows:
            quarantined.append(scope_id)
    connection.execute(
        "UPDATE semantic_documents SET record_count=(SELECT COUNT(*) FROM semantic_document_records sr "
        "WHERE sr.document_id=semantic_documents.document_id)"
    )
    connection.execute(
        "DELETE FROM semantic_documents WHERE document_id NOT IN "
        "(SELECT DISTINCT document_id FROM semantic_document_records)"
    )
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('last_pollution_repair_build_id',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (build_id,),
    )
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('retrieval_pollution_policy','evidence-fence-v1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    return {
        "refreshed_scopes": refreshed,
        "quarantined_scopes": quarantined,
        "after": pollution_audit(connection),
    }
