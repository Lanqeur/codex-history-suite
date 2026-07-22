from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from codex_history.knowledge import (
    _turn_text,
    history_retrieval_turn,
    history_user_assertion_eligible,
)
from codex_history.pipeline import active_database, build_full, repair_knowledge_pollution
from codex_history.query import search_records
from codex_history.schema import connect
from codex_history.summarize import pending_model_fact_blocks


def _event(event_id: str, role: str, text: str, call_id: str = ""):
    return SimpleNamespace(
        event_id=event_id,
        role=role,
        text=text,
        call_id=call_id,
        tool_name="exec_command" if role == "tool_call" else "",
    )


def test_history_evidence_fence_distinguishes_research_from_real_execution():
    history_events = [
        _event(
            "skill-call",
            "tool_call",
            "sed -n '1,200p' /home/u/.codex/skills/codex-history/SKILL.md",
            "skill",
        ),
        _event("skill-output", "tool_output", "# Codex History skill", "skill"),
        _event(
            "query-call",
            "tool_call",
            "python3 /opt/codex-history/history.py search 'work focus'",
            "query",
        ),
        _event(
            "query-output",
            "tool_output",
            "# Codex History Context\nold knowledge",
            "query",
        ),
    ]
    pure, filtered = history_retrieval_turn(history_events)
    assert pure is True
    assert filtered == {event.event_id for event in history_events}

    mixed_events = history_events + [
        _event("test-call", "tool_call", "pytest -q", "test"),
        _event("test-output", "tool_output", "12 passed", "test"),
    ]
    pure, filtered = history_retrieval_turn(mixed_events)
    assert pure is False
    text = _turn_text(
        SimpleNamespace(
            user_text="先查历史，再修复代码",
            assistant_text="代码已修复并通过测试。",
            turn_seq=1,
            event_count=len(mixed_events),
        ),
        mixed_events,
    )
    assert "old knowledge" not in text
    assert "12 passed" in text
    assert "代码已修复" in text
    assert "pytest -q" not in text


def test_long_first_party_statement_survives_without_history_synthesis():
    statement = "这是我今天新增的一手事实。" * 80
    user_text = (
        "<environment_context><current_date>2026-07-22</current_date>"
        "</environment_context>\n" + statement
    )
    events = [
        _event(
            "query-call",
            "tool_call",
            "python3 /opt/codex-history/history.py context '核验事实'",
            "query",
        ),
        _event(
            "query-output",
            "tool_output",
            "# Codex History Context\nold synthesis",
            "query",
        ),
    ]
    assert history_user_assertion_eligible(user_text, True) is True
    text = _turn_text(
        SimpleNamespace(
            user_text=user_text,
            assistant_text="这是基于旧知识库得出的助手结论。",
            turn_seq=1,
            event_count=len(events),
        ),
        events,
    )
    assert statement[:100] in text
    assert "environment_context" not in text
    assert "old synthesis" not in text
    assert "助手结论" not in text


def test_search_collapses_exact_high_tier_duplicates_without_deleting_records(
    portable_profile,
):
    config, codex_home = portable_profile
    _history_retrieval_transcript(codex_home)
    built = build_full(config)
    connection = connect(Path(built["database"]))
    try:
        source = connection.execute(
            "SELECT * FROM knowledge WHERE tier='asset' LIMIT 1"
        ).fetchone()
        before = connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE text=? AND tier='asset'",
            (source["text"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO knowledge SELECT ?,tier,?,scope_id,scope_type,scope_title,"
            "category,theme,phase,text,status,status_group,evidence_count,evidence_refs_json,"
            "source_path,source_locator,confidence,metadata_json,occurred_start_at,occurred_end_at,"
            "observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,"
            "temporal_basis,temporal_confidence FROM knowledge WHERE record_id=?",
            ("duplicate-asset", "preferences", source["record_id"]),
        )
        connection.commit()
        rows = search_records(
            connection,
            "决定 历史 检索",
            tiers=("asset",),
            retrieval="lexical",
            limit=20,
        )
        matching = [row for row in rows if row["text"] == source["text"]]
        assert len(matching) == 1
        assert matching[0]["duplicate_count"] == before
        assert "duplicate-asset" in {
            matching[0]["record_id"],
            *matching[0]["duplicate_record_ids"],
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE text=? AND tier='asset'",
            (source["text"],),
        ).fetchone()[0] == before + 1
    finally:
        connection.close()


def _history_retrieval_transcript(codex_home: Path) -> None:
    thread_id = "thread-history-echo"
    turn_id = "turn-history-echo"
    timestamp = "2026-07-14T04:00:00Z"
    rows = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": thread_id, "timestamp": timestamp, "cwd": "/workspace"},
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "我决定采用历史检索结论，但仍需核查污染回声。",
                    }
                ],
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps(
                    {
                        "cmd": "python3 /opt/codex-history/scripts/codex_history.py search '污染回声'"
                    }
                ),
                "call_id": "call-history-query",
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-history-query",
                "output": (
                    "Script completed\n# Codex History Context\n"
                    "The previous decision failed, was fixed, and remains unresolved.\n"
                    + "historical tool output " * 2_000
                ),
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The retrieved history suggests a previous decision.",
                    }
                ],
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": "The retrieved history suggests a previous decision.",
            },
        },
    ]
    path = codex_home / "sessions/2026/07/14/rollout-thread-history-echo.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {"id": thread_id, "thread_name": "History echo", "updated_at": timestamp}
        )
        + "\n",
        encoding="utf-8",
    )


def test_history_output_stays_in_evidence_but_cannot_reenter_high_tiers(
    portable_profile,
):
    config, codex_home = portable_profile
    _history_retrieval_transcript(codex_home)
    built = build_full(config)
    connection = connect(Path(built["database"]))
    try:
        assets = connection.execute(
            "SELECT k.text,k.metadata_json,c.role FROM knowledge k "
            "LEFT JOIN canonical_events c "
            "ON c.event_id=json_extract(k.metadata_json,'$.event_id') "
            "WHERE k.tier='asset'"
        ).fetchall()
        assert assets
        assert {row["role"] for row in assets} == {"user"}
        assert max(len(row["text"]) for row in assets) <= 1_600
        assert all(
            json.loads(row["metadata_json"])["derived_by"]
            == "deterministic-user-signal-v2"
            for row in assets
        )

        fact = connection.execute(
            "SELECT text,metadata_json FROM knowledge WHERE tier='fact_block'"
        ).fetchone()
        metadata = json.loads(fact["metadata_json"])
        assert metadata["retrieval_echo"] is True
        assert metadata["promotion_eligible"] is False
        assert "# Codex History Context" not in fact["text"]
        assert pending_model_fact_blocks(connection) == []

        source = assets[0]
        connection.execute(
            "INSERT INTO knowledge SELECT ?,tier,asset_type,scope_id,scope_type,scope_title,"
            "category,theme,phase,?,status,status_group,evidence_count,evidence_refs_json,"
            "source_path,source_locator,confidence,?,occurred_start_at,occurred_end_at,"
            "observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,"
            "temporal_basis,temporal_confidence FROM knowledge WHERE record_id=("
            "SELECT record_id FROM knowledge WHERE tier='asset' LIMIT 1)",
            (
                "legacy-polluted-asset",
                "污染回声 legacy tool output",
                json.dumps({"derived_by": "deterministic-asset-classifier-v1"}),
            ),
        )
        connection.commit()
        rows = search_records(
            connection,
            "污染回声",
            tiers=("asset",),
            retrieval="lexical",
            limit=20,
        )
        assert rows
        assert all(row["record_id"] != "legacy-polluted-asset" for row in rows)
    finally:
        connection.close()


def test_pollution_repair_creates_a_clean_promotable_generation(
    portable_profile, monkeypatch
):
    config, codex_home = portable_profile
    _history_retrieval_transcript(codex_home)
    built = build_full(config)
    connection = connect(Path(built["database"]))
    try:
        source = connection.execute(
            "SELECT * FROM knowledge WHERE tier='asset' LIMIT 1"
        ).fetchone()
        connection.execute(
            "INSERT INTO knowledge SELECT ?,tier,asset_type,scope_id,scope_type,scope_title,"
            "category,theme,phase,?,status,status_group,evidence_count,evidence_refs_json,"
            "source_path,source_locator,confidence,?,occurred_start_at,occurred_end_at,"
            "observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,"
            "temporal_basis,temporal_confidence FROM knowledge WHERE record_id=?",
            (
                "polluted-asset-for-repair",
                "Script completed old recursive history output",
                json.dumps({"derived_by": "deterministic-asset-classifier-v1"}),
                source["record_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setenv("FAKE_KEY", "test-key")
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_provider="fake",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-reducer",
        writer_provider="fake",
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-writer",
    )

    class RepairClient:
        def __init__(self, _settings):
            pass

        def complete(self, prompt: str):
            payload = json.loads(prompt.split("INPUT:\n", 1)[1])
            if "ledger_records" not in payload:
                ids = [row["record_id"] for row in payload.get("records") or []]
                return (
                    {
                        "source_record_ids": ids,
                        "ledger_items": [
                            {
                                "text": "A clean repair ledger fact.",
                                "status": "executed",
                                "category": "implementation",
                                "record_ids": ids,
                            }
                        ],
                        "no_new_fact_record_ids": [],
                    },
                    {"input_tokens": 20, "output_tokens": 10},
                )
            record_id = payload["ledger_records"][0]["record_id"]
            claim = "The repaired scope contains clean evidence."
            return (
                {
                    "overview": claim,
                    "claims": [{"text": claim, "record_ids": [record_id]}],
                    "assets": [],
                },
                {"input_tokens": 20, "output_tokens": 10},
            )

    monkeypatch.setattr("codex_history.summarize.ChatClient", RepairClient)
    repaired = repair_knowledge_pollution(model_config, max_cost_cny=1.0)
    assert repaired["status"] == "complete"
    assert active_database(model_config) == Path(repaired["database"])
    connection = connect(Path(repaired["database"]), readonly=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE "
            "json_extract(metadata_json,'$.derived_by')='deterministic-asset-classifier-v1'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE tier='ledger' AND "
            "json_extract(metadata_json,'$.promotion_policy')='evidence-fence-v1'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='retrieval_pollution_policy'"
        ).fetchone()[0] == "evidence-fence-v1"
    finally:
        connection.close()


def test_pollution_repair_rebuilds_drifted_external_content_fts(
    portable_profile, monkeypatch
):
    config, codex_home = portable_profile
    _history_retrieval_transcript(codex_home)
    built = build_full(config)
    connection = connect(Path(built["database"]))
    try:
        source = connection.execute(
            "SELECT * FROM knowledge WHERE tier='asset' LIMIT 1"
        ).fetchone()
        connection.execute(
            "INSERT INTO knowledge SELECT ?,tier,asset_type,scope_id,scope_type,scope_title,"
            "category,theme,phase,?,status,status_group,evidence_count,evidence_refs_json,"
            "source_path,source_locator,confidence,?,occurred_start_at,occurred_end_at,"
            "observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,"
            "temporal_basis,temporal_confidence FROM knowledge WHERE record_id=?",
            (
                "polluted-asset-with-fts-drift",
                "old recursive output that must be removed",
                json.dumps({"derived_by": "deterministic-asset-classifier-v1"}),
                source["record_id"],
            ),
        )
        connection.commit()
        # Deliberately remove this row from one FTS index. Its delete trigger
        # would otherwise try to remove it twice during the bulk cleanup.
        connection.execute(
            "INSERT INTO knowledge_fts(knowledge_fts,rowid,text,theme,scope_title,"
            "category,asset_type,tier) SELECT 'delete',rowid,text,theme,scope_title,"
            "category,asset_type,tier FROM knowledge WHERE record_id=?",
            ("polluted-asset-with-fts-drift",),
        )
        connection.execute(
            "INSERT INTO knowledge_relations(relation_id,source_record_id,relation_type,"
            "target_record_id,evidence_refs_json,confidence,created_at,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "relation-to-polluted-record",
                "polluted-asset-with-fts-drift",
                "supports",
                source["record_id"],
                "[]",
                "test",
                "2026-07-22T00:00:00Z",
                "{}",
            ),
        )
        overview = connection.execute(
            "SELECT * FROM knowledge WHERE tier='overview' LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE knowledge SET source_locator='model-summary:fake' WHERE record_id=?",
            (overview["record_id"],),
        )
        connection.execute(
            "INSERT INTO knowledge SELECT ?,tier,asset_type,scope_id,scope_type,scope_title,"
            "category,theme,phase,text,status,status_group,evidence_count,evidence_refs_json,"
            "source_path,?,confidence,metadata_json,occurred_start_at,occurred_end_at,"
            "observed_at,asserted_at,verified_at,indexed_at,valid_from,valid_to,"
            "temporal_basis,temporal_confidence FROM knowledge WHERE record_id=?",
            ("legacy-current-overview", "Writer-v1 Overview", overview["record_id"]),
        )
        connection.execute(
            "UPDATE knowledge SET evidence_count=0,evidence_refs_json='[]' "
            "WHERE record_id='legacy-current-overview'"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setenv("FAKE_KEY", "test-key")
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_provider="fake",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-reducer",
        writer_provider="fake",
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-writer",
    )

    class NoCallExpected:
        def __init__(self, _settings):
            pass

        def complete(self, _prompt: str):
            raise AssertionError("pure retrieval repair should not call a model")

    monkeypatch.setattr("codex_history.summarize.ChatClient", NoCallExpected)
    repaired = repair_knowledge_pollution(model_config, max_cost_cny=1.0)
    connection = connect(Path(repaired["database"]))
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'recursive'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_relations WHERE relation_id=?",
            ("relation-to-polluted-record",),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE tier='overview' AND valid_to IS NULL"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT valid_to FROM knowledge WHERE record_id='legacy-current-overview'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_relations WHERE source_record_id=? "
            "AND relation_type='supersedes' AND target_record_id='legacy-current-overview'",
            (overview["record_id"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
