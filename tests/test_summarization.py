from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from codex_history.pipeline import build_full
from codex_history.pipeline import equivalence_audit, update_incremental
from codex_history.schema import connect
from codex_history.summarize import summarize_scopes

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


def test_evidence_linked_model_summary_and_cache(portable_profile):
    config, codex_home = portable_profile
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
    model_config = replace(
        config,
        summary_mode="openai-compatible",
        summary_endpoint="https://example.invalid/v1",
        summary_api_key_env="FAKE_KEY",
        summary_model="fake-model-pipeline",
    )
    calls: list[str] = []

    class PipelineFakeClient:
        def __init__(self, settings):
            pass

        def complete(self, prompt: str):
            import json

            calls.append(prompt)
            payload = json.loads(prompt.split("INPUT:\n", 1)[1])
            records = payload.get("records") or []
            record_id = records[0]["record_id"]
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
    assert first["run"]["stages"]["summarize"]["report"]["api_calls"] == 1

    add_transcript(
        codex_home,
        "thread-model-two",
        "Model two",
        timestamp="2026-07-14T02:00:00Z",
        label="model-two",
        parent_thread_id="thread-model-one",
    )
    second = update_incremental(model_config, max_cost_cny=1.0)
    assert second["run"]["stages"]["summarize"]["report"]["api_calls"] == 2
    assert len(calls) == 3
    assert equivalence_audit(model_config)["passed"] is True
    assert len(calls) == 3
