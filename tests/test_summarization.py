from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from codex_history.pipeline import build_full
from codex_history.pipeline import active_info, equivalence_audit, plan, update_incremental
from codex_history.knowledge import _semantic_projection, insert_model_ledger_items
from codex_history.schema import connect
from codex_history.summarize import _validate_writer_response, summarize_scopes

from conftest import add_transcript


class FakeChatClient:
    def __init__(self, record_id: str):
        self.record_id = record_id
        self.calls = 0

    def complete(self, prompt: str):
        self.calls += 1
        claim = "Alpha was implemented and its tests passed."
        return (
            {
                "overview": claim,
                "claims": [{"text": claim, "record_ids": [self.record_id]}],
                "assets": [
                    {
                        "type": "capabilities",
                        "text": "Alpha implementation has direct execution evidence.",
                        "status": "executed",
                        "record_ids": [self.record_id],
                    }
                ],
            },
            {"input_tokens": 100, "output_tokens": 40},
        )


class FailingChatClient:
    def complete(self, prompt: str):
        raise AssertionError("cache was not reused")


class CachedTokenChatClient(FakeChatClient):
    def complete(self, prompt: str):
        response, _usage = super().complete(prompt)
        return response, {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 40,
        }


class IncrementalFakeClient:
    def __init__(self, settings=None):
        self.settings = settings

    def complete(self, prompt: str):
        import json

        payload = json.loads(prompt.split("INPUT:\n", 1)[1])
        if "ledger_records" not in payload:
            record_ids = [row["record_id"] for row in payload.get("records") or []]
            return (
                {
                    "source_record_ids": record_ids,
                    "ledger_items": [
                        {
                            "text": f"Consolidated {len(record_ids)} new execution records.",
                            "status": "executed",
                            "category": "implementation",
                            "record_ids": record_ids,
                        }
                    ],
                    "no_new_fact_record_ids": [],
                },
                {"input_tokens": 40, "output_tokens": 20},
            )
        record_id = payload["ledger_records"][0]["record_id"]
        claim = f"The {payload['scope_type']} scope includes consolidated execution evidence."
        return (
            {
                "overview": claim,
                "claims": [{"text": claim, "record_ids": [record_id]}],
                "assets": [],
            },
            {"input_tokens": 30, "output_tokens": 15},
        )


def test_semantic_projection_bounds_provider_input_without_changing_authority_text():
    text = "HEAD-" + "a" * 9000 + "-MIDDLE-" + "b" * 9000 + "-TAIL"
    projected = _semantic_projection(text)
    assert len(projected) <= 3900
    assert projected.startswith("HEAD-")
    assert "MIDDLE" in projected
    assert projected.endswith("-TAIL")
    assert len(text) > len(projected)


def test_model_ledger_reuses_legacy_semantic_document_id(portable_profile):
    config, codex_home = portable_profile
    add_transcript(
        codex_home,
        "thread-legacy-semantic",
        "Legacy semantic ID",
        timestamp="2026-07-14T01:00:00Z",
        label="legacy-semantic",
    )
    built = build_full(config)
    connection = connect(Path(built["database"]))
    try:
        source_record_id = connection.execute(
            "SELECT record_id FROM knowledge WHERE scope_id='thread-legacy-semantic' "
            "AND tier='fact_block'"
        ).fetchone()[0]
        text = "A legacy semantic document ID remains reusable by a new ledger record."
        content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO semantic_documents(document_id,content_sha256,document_text,record_count,created_at) "
            "VALUES(?,?,?,?,?)",
            ("legacy-document-id", content_sha, text, 0, "2026-07-14T01:00:00Z"),
        )
        record_ids = insert_model_ledger_items(
            connection,
            scope_id="thread-legacy-semantic",
            items=[
                {
                    "text": text,
                    "status": "executed",
                    "category": "implementation",
                    "record_ids": [source_record_id],
                }
            ],
            generation_id="legacy-generation",
            build_id=str(built["build_id"]),
            model="fake-model",
            cache_key="fake-cache-key",
        )
        mapped = connection.execute(
            "SELECT document_id FROM semantic_document_records WHERE record_id=?",
            (record_ids[0],),
        ).fetchone()[0]
        assert mapped == "legacy-document-id"
    finally:
        connection.close()


def test_writer_claim_reconciles_only_terminal_punctuation():
    response = {
        "overview": "The baseline was verified; the delta remains pending.",
        "claims": [
            {
                "text": "The baseline was verified.",
                "record_ids": ["record-1"],
            }
        ],
        "assets": [],
    }
    normalized = _validate_writer_response(response, {"record-1"})
    assert normalized["claims"][0]["text"] == "The baseline was verified;"

    import pytest

    response["claims"][0]["text"] = "The baseline was not verified."
    with pytest.raises(ValueError, match="absent from overview"):
        _validate_writer_response(response, {"record-1"})


def test_writer_validation_reports_ids_outside_ledger_allowlist():
    import pytest

    response = {
        "overview": "The baseline was verified.",
        "claims": [
            {
                "text": "The baseline was verified.",
                "record_ids": ["record-1", "record-from-source-evidence"],
            }
        ],
        "assets": [],
    }
    with pytest.raises(ValueError, match="record-from-source-evidence"):
        _validate_writer_response(response, {"record-1"})


def test_evidence_linked_model_summary_and_cache(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    monkeypatch.setenv("FAKE_KEY", "test-key")
    add_transcript(
        codex_home,
        "thread-summary",
        "Summary target",
        timestamp="2026-07-14T01:00:00Z",
        label="alpha",
    )
    built = build_full(config)
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-model",
        summary_input_price_cny=1.0,
        summary_output_price_cny=2.0,
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-model",
        writer_input_price_cny=1.0,
        writer_cached_input_price_cny=0.2,
        writer_output_price_cny=2.0,
    )
    connection = connect(Path(built["database"]))
    try:
        record_id = connection.execute(
            "SELECT record_id FROM knowledge WHERE scope_id='thread-summary' AND tier='fact_block'"
        ).fetchone()[0]
        client = FakeChatClient(record_id)
        first = summarize_scopes(
            model_config,
            connection,
            scope_ids=["thread-summary"],
            max_cost_cny=1.0,
            client=client,
        )
        connection.commit()
        assert first["api_calls"] == 1
        assert client.calls == 1
        claim = connection.execute(
            "SELECT status FROM overview_claims WHERE scope_id='thread-summary'"
        ).fetchone()
        assert claim[0] == "linked"
        asset = connection.execute(
            "SELECT confidence FROM knowledge WHERE scope_id='thread-summary' "
            "AND tier='asset' AND source_locator='model-summary:fake-model'"
        ).fetchone()
        assert asset[0] == "model_evidence_linked"

        second = summarize_scopes(
            model_config,
            connection,
            scope_ids=["thread-summary"],
            max_cost_cny=0.0,
            client=FailingChatClient(),
        )
        assert second["api_calls"] == 0
        assert second["cache_hits"] == 1
    finally:
        connection.close()


def test_model_pipeline_incremental_equals_clean_full(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    monkeypatch.setenv("FAKE_KEY", "test-key")
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-model-pipeline",
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-writer-pipeline",
    )
    calls: list[str] = []

    class PipelineFakeClient:
        def __init__(self, settings):
            pass

        def complete(self, prompt: str):
            import json

            calls.append(prompt)
            payload = json.loads(prompt.split("INPUT:\n", 1)[1])
            if "ledger_records" not in payload:
                records = payload.get("records") or []
                record_ids = [row["record_id"] for row in records]
                return (
                    {
                        "source_record_ids": record_ids,
                        "ledger_items": [
                            {
                                "text": f"Consolidated evidence for {payload['scope_id']}.",
                                "status": "executed",
                                "category": "implementation",
                                "record_ids": record_ids,
                            }
                        ],
                        "no_new_fact_record_ids": [],
                    },
                    {"input_tokens": 20, "output_tokens": 10},
                )
            record_id = payload["ledger_records"][0]["record_id"]
            text = f"Evidence-linked result for {payload['scope_id']}."
            return (
                {
                    "overview": text,
                    "claims": [{"text": text, "record_ids": [record_id]}],
                    "assets": [],
                },
                {"input_tokens": 20, "output_tokens": 10},
            )

    monkeypatch.setattr("codex_history.summarize.ChatClient", PipelineFakeClient)
    add_transcript(
        codex_home,
        "thread-model-one",
        "Model one",
        timestamp="2026-07-14T01:00:00Z",
        label="model-one",
    )
    first = build_full(model_config, max_cost_cny=1.0)
    assert first["run"]["stages"]["summarize"]["report"]["api_calls"] == 2
    assert first["usage"]["summary_input_tokens"] == 40
    assert first["usage"]["summary_output_tokens"] == 20
    assert first["usage"]["total_cost_cny"] > 0

    add_transcript(
        codex_home,
        "thread-model-two",
        "Model two",
        timestamp="2026-07-14T02:00:00Z",
        label="model-two",
        parent_thread_id="thread-model-one",
    )
    second = update_incremental(model_config, max_cost_cny=1.0)
    assert second["run"]["stages"]["summarize"]["report"]["api_calls"] == 4
    assert len(calls) == 6
    ledger = plan(model_config, mode="incremental")["usage_ledger"]
    assert ledger["successful_attempts"] == 6
    assert ledger["input_tokens"] == 120
    assert ledger["output_tokens"] == 60
    assert ledger["cost_cny"] > 0
    assert equivalence_audit(model_config, confirm_full_reference=True)["passed"] is True
    assert len(calls) >= 6


def test_provider_cache_tokens_use_the_configured_discount(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    monkeypatch.setenv("FAKE_KEY", "test-key")
    add_transcript(
        codex_home,
        "thread-cached-price",
        "Cached price",
        timestamp="2026-07-14T01:00:00Z",
        label="cache-price",
    )
    built = build_full(config)
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-cached-price-model",
        summary_input_price_cny=10.0,
        summary_cached_input_price_cny=2.0,
        summary_output_price_cny=20.0,
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-cached-price-model",
        writer_input_price_cny=10.0,
        writer_cached_input_price_cny=2.0,
        writer_output_price_cny=20.0,
    )
    connection = connect(Path(built["database"]))
    try:
        record_id = connection.execute(
            "SELECT record_id FROM knowledge WHERE scope_id='thread-cached-price' "
            "AND tier='fact_block'"
        ).fetchone()[0]
        report = summarize_scopes(
            model_config,
            connection,
            scope_ids=["thread-cached-price"],
            max_cost_cny=1.0,
            client=CachedTokenChatClient(record_id),
        )
        assert report["input_tokens"] == 100
        assert report["cached_input_tokens"] == 80
        assert report["uncached_input_tokens"] == 20
        assert report["cost_cny"] == 0.00116
    finally:
        connection.close()


def test_pending_extractive_backlog_can_be_model_consolidated_without_source_changes(
    portable_profile, monkeypatch
):
    config, codex_home = portable_profile
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    add_transcript(
        codex_home,
        "thread-backlog",
        "Backlog",
        timestamp="2026-07-14T01:00:00Z",
        label="backlog",
    )
    fallback = build_full(config)
    assert active_info(config)["knowledge_completion_status"] == "pending_model_consolidation"
    assert fallback["run"]["stages"]["summarize"]["report"]["pending_after"] == 1

    monkeypatch.setenv("FAKE_KEY", "test-key")
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-reducer",
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
        writer_model="fake-writer",
    )
    dry_run = plan(model_config, mode="incremental")
    assert dry_run["actionable_count"] == 0
    assert dry_run["pending_model_records"] == 1
    assert dry_run["work_required"] is True
    monkeypatch.setattr("codex_history.summarize.ChatClient", IncrementalFakeClient)
    completed = update_incremental(model_config, max_cost_cny=1.0)
    report = completed["run"]["stages"]["summarize"]["report"]
    assert report["coverage_complete"] is True
    assert report["pending_after"] == 0
    assert active_info(model_config)["knowledge_completion_status"] == "model_complete"
    connection = connect(Path(completed["database"]), readonly=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge WHERE tier='ledger'"
        ).fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] > 0
    finally:
        connection.close()
    assert update_incremental(model_config)["status"] == "no_changes"


def test_incomplete_model_coverage_cannot_replace_active_build(portable_profile, monkeypatch):
    config, codex_home = portable_profile
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    add_transcript(
        codex_home,
        "thread-coverage-gate",
        "Coverage gate",
        timestamp="2026-07-14T01:00:00Z",
        label="coverage-gate",
    )
    baseline = build_full(config)
    baseline_active = active_info(config)

    class IncompleteClient:
        def __init__(self, settings=None):
            pass

        def complete(self, prompt: str):
            import json

            payload = json.loads(prompt.split("INPUT:\n", 1)[1])
            record_ids = [row["record_id"] for row in payload.get("records") or []]
            return (
                {
                    "source_record_ids": record_ids,
                    "ledger_items": [],
                    "no_new_fact_record_ids": [],
                },
                {"input_tokens": 10, "output_tokens": 5},
            )

    monkeypatch.setenv("FAKE_KEY", "test-key")
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        writer_endpoint="https://example.invalid/v1",
        writer_api_key_env="FAKE_KEY",
    )
    monkeypatch.setattr("codex_history.summarize.ChatClient", IncompleteClient)
    import pytest

    with pytest.raises(ValueError, match="omitted"):
        update_incremental(model_config, max_cost_cny=1.0)
    assert active_info(config) == baseline_active
    assert Path(baseline["database"]).is_file()
