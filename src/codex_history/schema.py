from __future__ import annotations

import sqlite3
from pathlib import Path

from .util import utc_now


SCHEMA_NAME = "codex-history-suite"
SCHEMA_VERSION = 4


BASE_SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds(
    build_id TEXT PRIMARY KEY,
    build_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_build_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    promoted_at TEXT,
    source_manifest_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    logical_digest TEXT,
    notes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stage_checkpoints(
    build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    input_digest TEXT,
    output_digest TEXT,
    report_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(build_id, stage_name)
);

CREATE TABLE IF NOT EXISTS source_files(
    source_id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    source_root TEXT NOT NULL,
    source_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    prefix_sha256 TEXT NOT NULL,
    snapshot_format TEXT NOT NULL DEFAULT 'raw-jsonl',
    snapshot_size_bytes INTEGER NOT NULL DEFAULT 0,
    snapshot_content_sha256 TEXT NOT NULL DEFAULT '',
    line_count INTEGER NOT NULL,
    snapshot_manifest_path TEXT NOT NULL,
    source_state TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(adapter, source_path)
);

CREATE TABLE IF NOT EXISTS source_chunks(
    source_id TEXT NOT NULL REFERENCES source_files(source_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    cas_relative_path TEXT NOT NULL,
    PRIMARY KEY(source_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS threads(
    thread_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    transcript_relative_path TEXT NOT NULL,
    source_relative_path TEXT,
    source_size_bytes INTEGER,
    line_count INTEGER NOT NULL,
    first_activity_at TEXT,
    last_activity_at TEXT,
    event_count INTEGER NOT NULL,
    turn_count INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    user_message_count INTEGER NOT NULL,
    assistant_message_count INTEGER NOT NULL,
    tool_call_count INTEGER NOT NULL,
    tool_output_count INTEGER NOT NULL,
    goal_event_count INTEGER NOT NULL,
    compacted_count INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'codex_jsonl',
    parent_thread_id TEXT,
    source_id TEXT REFERENCES source_files(source_id)
);

CREATE TABLE IF NOT EXISTS turns(
    turn_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    turn_seq INTEGER NOT NULL,
    source_turn_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    tool_call_count INTEGER NOT NULL,
    tool_output_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(thread_id, turn_seq)
);

CREATE TABLE IF NOT EXISTS canonical_events(
    event_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES source_files(source_id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    turn_id TEXT REFERENCES turns(turn_id) ON DELETE SET NULL,
    line_no INTEGER NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    timestamp TEXT,
    event_type TEXT NOT NULL,
    payload_type TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    call_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, line_no)
);

CREATE TABLE IF NOT EXISTS scopes(
    scope_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_title TEXT NOT NULL,
    thread_ids_json TEXT NOT NULL,
    thread_titles_json TEXT NOT NULL,
    overview TEXT NOT NULL,
    human_verdict TEXT NOT NULL,
    evidence_rows INTEGER NOT NULL,
    overview_path TEXT NOT NULL,
    ledger_path TEXT NOT NULL,
    first_activity_at TEXT,
    last_activity_at TEXT,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS scope_threads(
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(scope_id, thread_id)
);

CREATE TABLE IF NOT EXISTS evidence(
    evidence_id TEXT PRIMARY KEY,
    assignment TEXT NOT NULL,
    evidence_chars INTEGER NOT NULL,
    source_task_id TEXT NOT NULL,
    scope_ids_json TEXT NOT NULL,
    thread_ids_json TEXT NOT NULL,
    applies_to_json TEXT NOT NULL,
    item_id TEXT,
    sha256 TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    first_occurred_at TEXT,
    last_occurred_at TEXT,
    temporal_basis TEXT NOT NULL DEFAULT 'event_timestamp'
);

CREATE TABLE IF NOT EXISTS evidence_occurrences(
    occurrence_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    turn_seq INTEGER NOT NULL,
    position INTEGER NOT NULL,
    tier TEXT NOT NULL,
    canonical_turn_id TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    occurred_start_at TEXT,
    occurred_end_at TEXT,
    temporal_basis TEXT NOT NULL,
    temporal_confidence TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(evidence_id, thread_id, turn_seq, position)
);

CREATE TABLE IF NOT EXISTS knowledge(
    record_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
    scope_type TEXT NOT NULL,
    scope_title TEXT NOT NULL,
    category TEXT NOT NULL,
    theme TEXT NOT NULL,
    phase TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    status_group TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    confidence TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    occurred_start_at TEXT,
    occurred_end_at TEXT,
    observed_at TEXT,
    asserted_at TEXT,
    verified_at TEXT,
    indexed_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    temporal_basis TEXT NOT NULL DEFAULT 'event_timestamp',
    temporal_confidence TEXT NOT NULL DEFAULT 'exact_event'
);

CREATE TABLE IF NOT EXISTS knowledge_evidence(
    record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    PRIMARY KEY(record_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS record_evidence_occurrences(
    record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    occurrence_id TEXT NOT NULL REFERENCES evidence_occurrences(occurrence_id) ON DELETE CASCADE,
    scope_match INTEGER NOT NULL,
    PRIMARY KEY(record_id, occurrence_id)
);

CREATE TABLE IF NOT EXISTS overview_claims(
    claim_id TEXT PRIMARY KEY,
    overview_record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    claim_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unlinked',
    confidence REAL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(overview_record_id, ordinal)
);

CREATE TABLE IF NOT EXISTS overview_claim_records(
    claim_id TEXT NOT NULL REFERENCES overview_claims(claim_id) ON DELETE CASCADE,
    record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    match_method TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(claim_id, record_id)
);

CREATE TABLE IF NOT EXISTS knowledge_relations(
    relation_id TEXT PRIMARY KEY,
    source_record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK(relation_type IN (
        'supersedes','resolves','contradicts','supports','refines','reopens','validates','invalidates'
    )),
    target_record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_record_id, relation_type, target_record_id)
);

CREATE TABLE IF NOT EXISTS knowledge_versions(
    version_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    build_id TEXT REFERENCES builds(build_id),
    row_json TEXT NOT NULL,
    UNIQUE(record_id, version_no)
);

CREATE TABLE IF NOT EXISTS aliases(
    alias TEXT NOT NULL COLLATE NOCASE,
    canonical TEXT NOT NULL COLLATE NOCASE,
    alias_kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY(alias, canonical)
);

CREATE TABLE IF NOT EXISTS artifact_files(
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    size_human TEXT NOT NULL,
    cas_relative_path TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    source_open_path TEXT NOT NULL,
    tiers TEXT NOT NULL,
    keep_reasons TEXT NOT NULL,
    categories TEXT NOT NULL,
    path_count INTEGER NOT NULL,
    transcript_occurrences_mapped INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_paths(
    path_key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES artifact_files(sha256) ON DELETE CASCADE,
    artifact_uri TEXT NOT NULL,
    cas_relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    tier TEXT NOT NULL,
    keep_reason TEXT NOT NULL,
    category TEXT NOT NULL,
    source_open_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_artifacts(
    ledger_artifact_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    role TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_locator TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repository_checkpoints(
    checkpoint_id TEXT PRIMARY KEY,
    repository_root TEXT NOT NULL,
    head_commit TEXT NOT NULL,
    branch TEXT NOT NULL,
    refs_sha256 TEXT NOT NULL,
    worktree_sha256 TEXT NOT NULL,
    capture_mode TEXT NOT NULL,
    history_artifact_sha256 TEXT REFERENCES artifact_files(sha256),
    worktree_artifact_sha256 TEXT REFERENCES artifact_files(sha256),
    is_dirty INTEGER NOT NULL,
    is_partial_clone INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(repository_root,refs_sha256,worktree_sha256,capture_mode)
);

CREATE TABLE IF NOT EXISTS artifact_observations(
    observation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES source_files(source_id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    artifact_sha256 TEXT NOT NULL REFERENCES artifact_files(sha256) ON DELETE CASCADE,
    repository_checkpoint_id TEXT REFERENCES repository_checkpoints(checkpoint_id) ON DELETE SET NULL,
    original_path TEXT NOT NULL,
    resolved_path TEXT NOT NULL,
    occurrence_at TEXT,
    captured_at TEXT NOT NULL,
    capture_method TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(event_id,original_path,artifact_sha256,capture_method)
);

CREATE INDEX IF NOT EXISTS repository_checkpoints_root_idx
    ON repository_checkpoints(repository_root,captured_at);
CREATE INDEX IF NOT EXISTS repository_checkpoints_head_idx
    ON repository_checkpoints(head_commit);
CREATE INDEX IF NOT EXISTS artifact_observations_event_idx
    ON artifact_observations(event_id);
CREATE INDEX IF NOT EXISTS artifact_observations_artifact_idx
    ON artifact_observations(artifact_sha256);
CREATE INDEX IF NOT EXISTS artifact_observations_checkpoint_idx
    ON artifact_observations(repository_checkpoint_id);

CREATE TABLE IF NOT EXISTS semantic_documents(
    document_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL UNIQUE,
    document_text TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_document_records(
    document_id TEXT NOT NULL REFERENCES semantic_documents(document_id) ON DELETE CASCADE,
    record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    PRIMARY KEY(document_id, record_id),
    UNIQUE(record_id)
);

CREATE TABLE IF NOT EXISTS model_cache(
    cache_key TEXT PRIMARY KEY,
    stage_name TEXT NOT NULL,
    stage_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_cny REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relation_candidates(
    candidate_id TEXT PRIMARY KEY,
    source_record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    target_record_id TEXT NOT NULL REFERENCES knowledge(record_id) ON DELETE CASCADE,
    proposed_type TEXT NOT NULL,
    score REAL NOT NULL,
    decision TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_record_id,target_record_id,proposed_type)
);

CREATE TABLE IF NOT EXISTS embedding_runs(
    run_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    requested_documents INTEGER NOT NULL,
    embedded_documents INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_cny REAL NOT NULL DEFAULT 0,
    actual_cost_cny REAL NOT NULL DEFAULT 0,
    max_cost_cny REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS embedding_batches(
    run_id TEXT NOT NULL REFERENCES embedding_runs(run_id) ON DELETE CASCADE,
    batch_number INTEGER NOT NULL,
    document_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cny REAL NOT NULL DEFAULT 0,
    error_text TEXT,
    completed_at TEXT,
    PRIMARY KEY(run_id,batch_number)
);

CREATE TABLE IF NOT EXISTS semantic_query_cache(
    query_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    query_text TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(query_sha256,model,dimensions)
);

CREATE TABLE IF NOT EXISTS semantic_query_expansions(
    trigger_text TEXT PRIMARY KEY COLLATE NOCASE,
    expansion_text TEXT NOT NULL,
    reason TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS sync_runs(
    sync_run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_snapshot_path TEXT,
    manifest_path TEXT,
    added_threads INTEGER NOT NULL DEFAULT 0,
    appended_threads INTEGER NOT NULL DEFAULT 0,
    rewritten_threads INTEGER NOT NULL DEFAULT 0,
    pending_turns INTEGER NOT NULL DEFAULT 0,
    new_evidence INTEGER NOT NULL DEFAULT 0,
    promoted_records INTEGER NOT NULL DEFAULT 0,
    estimated_cost_cny REAL NOT NULL DEFAULT 0,
    actual_cost_cny REAL NOT NULL DEFAULT 0,
    max_cost_cny REAL,
    notes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_checkpoints(
    thread_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    prefix_sha256 TEXT NOT NULL,
    last_complete_line INTEGER NOT NULL DEFAULT 0,
    last_complete_turn_seq INTEGER NOT NULL DEFAULT 0,
    last_event_timestamp TEXT,
    content_state TEXT NOT NULL,
    last_sync_run_id TEXT REFERENCES sync_runs(sync_run_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS path_mappings(
    mapping_id TEXT PRIMARY KEY,
    original_prefix TEXT NOT NULL,
    local_prefix TEXT NOT NULL,
    mapping_kind TEXT NOT NULL DEFAULT 'prefix',
    source_device_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(original_prefix,local_prefix,source_device_id)
);

CREATE TABLE IF NOT EXISTS pending_turns(
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_seq INTEGER,
    source_path TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    snapshot_size_bytes INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(thread_id,turn_id)
);

CREATE TABLE IF NOT EXISTS incremental_events(
    incremental_event_id TEXT PRIMARY KEY,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id),
    thread_id TEXT NOT NULL,
    turn_seq INTEGER,
    line_no INTEGER,
    timestamp TEXT,
    event_kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE(thread_id,line_no,content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_events_thread_turn ON canonical_events(thread_id, turn_id, line_no);
CREATE INDEX IF NOT EXISTS idx_events_content ON canonical_events(content_sha256);
CREATE INDEX IF NOT EXISTS idx_turns_thread_seq ON turns(thread_id, turn_seq);
CREATE INDEX IF NOT EXISTS idx_knowledge_scope_tier ON knowledge(scope_id, tier);
CREATE INDEX IF NOT EXISTS idx_knowledge_time ON knowledge(occurred_start_at, occurred_end_at);
CREATE INDEX IF NOT EXISTS idx_evidence_thread ON evidence_occurrences(thread_id, turn_seq);

CREATE VIEW IF NOT EXISTS decisions AS
SELECT * FROM knowledge WHERE tier='asset' AND asset_type='decisions';
CREATE VIEW IF NOT EXISTS unresolved AS
SELECT * FROM knowledge WHERE tier='asset' AND asset_type='unresolved';
CREATE VIEW IF NOT EXISTS failures AS
SELECT * FROM knowledge WHERE tier='asset' AND asset_type='failures';
CREATE VIEW IF NOT EXISTS capabilities AS
SELECT * FROM knowledge WHERE tier='asset' AND asset_type='capabilities';
CREATE VIEW IF NOT EXISTS preferences AS
SELECT * FROM knowledge WHERE tier='asset' AND asset_type='preferences';
"""


FTS_TABLES = (
    (
        "knowledge_fts",
        "text,theme,scope_title,category,asset_type,tier",
        "text,theme,scope_title,category,asset_type,tier",
        "trigram",
    ),
    (
        "knowledge_terms_fts",
        "text,theme,scope_title,category,asset_type,tier",
        "text,theme,scope_title,category,asset_type,tier",
        "unicode61 remove_diacritics 2 tokenchars '_-./:'",
    ),
    (
        "knowledge_body_fts",
        "text,theme,category,asset_type,tier",
        "text,theme,category,asset_type,tier",
        "trigram",
    ),
    (
        "knowledge_title_fts",
        "scope_title",
        "scope_title",
        "trigram",
    ),
)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _create_fts(connection: sqlite3.Connection) -> None:
    created: list[tuple[str, str]] = []
    for name, columns, _source_columns, tokenizer in FTS_TABLES:
        try:
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5("
                f"{columns},content='knowledge',content_rowid='rowid',tokenize=\"{tokenizer}\")"
            )
            created.append((name, columns))
        except sqlite3.OperationalError:
            if tokenizer != "trigram":
                raise
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5("
                f"{columns},content='knowledge',content_rowid='rowid',tokenize='unicode61')"
            )
            created.append((name, columns))
    for name, columns in created:
        column_names = [item.strip() for item in columns.split(",")]
        new_values = ",".join(f"new.{column}" for column in column_names)
        old_values = ",".join(f"old.{column}" for column in column_names)
        connection.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS {name}_ai AFTER INSERT ON knowledge BEGIN
              INSERT INTO {name}(rowid,{columns}) VALUES (new.rowid,{new_values});
            END;
            CREATE TRIGGER IF NOT EXISTS {name}_ad AFTER DELETE ON knowledge BEGIN
              INSERT INTO {name}({name},rowid,{columns}) VALUES('delete',old.rowid,{old_values});
            END;
            CREATE TRIGGER IF NOT EXISTS {name}_au AFTER UPDATE ON knowledge BEGIN
              INSERT INTO {name}({name},rowid,{columns}) VALUES('delete',old.rowid,{old_values});
              INSERT INTO {name}(rowid,{columns}) VALUES (new.rowid,{new_values});
            END;
            """
        )
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5("
            "path,source_open_path,category,keep_reason,"
            "content='artifact_paths',content_rowid='rowid',tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5("
            "path,source_open_path,category,keep_reason,"
            "content='artifact_paths',content_rowid='rowid',tokenize='unicode61')"
        )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS artifact_fts_ai AFTER INSERT ON artifact_paths BEGIN
          INSERT INTO artifact_fts(rowid,path,source_open_path,category,keep_reason)
          VALUES(new.rowid,new.path,new.source_open_path,new.category,new.keep_reason);
        END;
        CREATE TRIGGER IF NOT EXISTS artifact_fts_ad AFTER DELETE ON artifact_paths BEGIN
          INSERT INTO artifact_fts(artifact_fts,rowid,path,source_open_path,category,keep_reason)
          VALUES('delete',old.rowid,old.path,old.source_open_path,old.category,old.keep_reason);
        END;
        CREATE TRIGGER IF NOT EXISTS artifact_fts_au AFTER UPDATE ON artifact_paths BEGIN
          INSERT INTO artifact_fts(artifact_fts,rowid,path,source_open_path,category,keep_reason)
          VALUES('delete',old.rowid,old.path,old.source_open_path,old.category,old.keep_reason);
          INSERT INTO artifact_fts(rowid,path,source_open_path,category,keep_reason)
          VALUES(new.rowid,new.path,new.source_open_path,new.category,new.keep_reason);
        END;
        """
    )


def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(BASE_SCHEMA)
    _ensure_column(connection, "threads", "parent_thread_id TEXT")
    _ensure_column(connection, "threads", "source_id TEXT")
    _ensure_column(connection, "knowledge_versions", "build_id TEXT")
    _ensure_column(connection, "source_files", "snapshot_format TEXT NOT NULL DEFAULT 'raw-jsonl'")
    _ensure_column(connection, "source_files", "snapshot_size_bytes INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "source_files", "snapshot_content_sha256 TEXT NOT NULL DEFAULT ''")
    _create_fts(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,description) VALUES(?,?,?)",
        (
            SCHEMA_VERSION,
            utc_now(),
            "Add versioned artifact observations and Git repository checkpoints",
        ),
    )
    values = {
        "schema_name": SCHEMA_NAME,
        "schema_version": str(SCHEMA_VERSION),
        "fts_enabled": "true",
    }
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        values.items(),
    )
    connection.commit()


def rebuild_fts(connection: sqlite3.Connection) -> None:
    for name, _columns, _source_columns, _tokenizer in FTS_TABLES:
        connection.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
    connection.execute("INSERT INTO artifact_fts(artifact_fts) VALUES('rebuild')")
    connection.commit()


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])
