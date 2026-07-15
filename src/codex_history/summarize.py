from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from .config import ProfileConfig, configured_secret, resolve_summarization
from .knowledge import apply_model_scope_summary
from .util import atomic_write_json, canonical_json, read_json, stable_id, utc_now


SUMMARY_STAGE_VERSION = "evidence-linked-writer-v2"
MAX_CHUNK_CHARS = 60_000


SYSTEM_PROMPT = """You build an evidence-linked historical knowledge base from Codex execution traces.
Use only supplied records. Distinguish requested, executed, verified, failed, blocked, and uncertain states.
Do not turn an assistant's claim into independent verification. Preserve important failures and unresolved work.
Return one JSON object and no prose outside JSON."""


WRITER_PROMPT = """Summarize the supplied scope records into durable historical knowledge.
Return this exact shape:
{
  "overview": "coherent overview",
  "claims": [{"text":"a sentence copied into the overview", "record_ids":["record-id"]}],
  "assets": [{"type":"decisions|unresolved|failures|capabilities|preferences", "text":"fact", "status":"verified|executed|planned|failed|blocked|uncertain|mixed", "record_ids":["record-id"]}]
}
Every claim and asset must cite only supplied record IDs. Omit unsupported content. Overview claims should appear verbatim in overview."""


CONDENSER_PROMPT = """Condense these execution records without semantic deletion.
Return JSON with arrays `claims` and `assets`. Each item must retain the supplied supporting record IDs.
Keep decisions, verified outcomes, failures, recoveries, unresolved work, constraints, and concrete implementation facts."""


@dataclass(frozen=True)
class ChatSettings:
    provider: str
    endpoint: str
    api_key_env: str
    model: str
    input_price_cny: float
    cached_input_price_cny: float
    output_price_cny: float
    env_file: str
    thinking_enabled: bool | None

    @classmethod
    def from_config(cls, config: ProfileConfig) -> "ChatSettings":
        return cls(
            provider=config.summary_provider,
            endpoint=config.summary_endpoint,
            api_key_env=config.summary_api_key_env,
            model=config.summary_model,
            input_price_cny=config.summary_input_price_cny,
            cached_input_price_cny=config.summary_cached_input_price_cny,
            output_price_cny=config.summary_output_price_cny,
            env_file=config.summary_env_file,
            thinking_enabled=config.summary_thinking_enabled,
        )


class ChatClient:
    def __init__(self, settings: ChatSettings, *, timeout: int = 180) -> None:
        self.settings = settings
        if not settings.endpoint:
            raise RuntimeError("summarization.endpoint is not configured")
        if not settings.model:
            raise RuntimeError("summarization.model is not configured")
        self.api_key = configured_secret(settings.api_key_env, settings.env_file)
        if not self.api_key:
            raise RuntimeError(f"Summarization API key is missing from ${settings.api_key_env}")
        endpoint = settings.endpoint.rstrip("/")
        self.url = endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"
        self.timeout = timeout

    def complete(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
        if (
            self.settings.provider.strip().lower() == "dashscope"
            and self.settings.thinking_enabled is not None
        ):
            body["enable_thinking"] = self.settings.thinking_enabled
        payload = json.dumps(
            body,
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Summarization API HTTP {error.code}: {detail}") from error
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("Summarization API returned no choices")
        content = choices[0].get("message", {}).get("content", "")
        result = parse_json_object(str(content))
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        return result, {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_input_tokens": int(
                details.get("cached_tokens")
                or details.get("cache_read_input_tokens")
                or usage.get("cached_input_tokens")
                or 0
            ),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }


def parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Model response does not contain a JSON object")
        result = json.loads(text[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("Model response is not a JSON object")
    return result


def _chunks(rows: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        row_size = len(canonical_json(row))
        if current and size + row_size > MAX_CHUNK_CHARS:
            yield current
            current = []
            size = 0
        current.append(row)
        size += row_size
    if current:
        yield current


def _cache_key(settings: ChatSettings, stage: str, payload: Any) -> str:
    return stable_id(
        "model",
        SUMMARY_STAGE_VERSION,
        settings.provider,
        settings.endpoint,
        settings.model,
        settings.thinking_enabled,
        stage,
        hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        length=48,
    )


def _cost(
    settings: ChatSettings,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    cached = min(max(0, cached_input_tokens), max(0, input_tokens))
    uncached = max(0, input_tokens - cached)
    return (
        uncached / 1_000_000 * settings.input_price_cny
        + cached / 1_000_000 * settings.cached_input_price_cny
        + output_tokens / 1_000_000 * settings.output_price_cny
    )


def _call_cached(
    config: ProfileConfig,
    client: ChatClient,
    settings: ChatSettings,
    *,
    stage: str,
    instruction: str,
    payload: Any,
    remaining_cost_cny: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = _cache_key(settings, stage, payload)
    cache_path = config.cache_dir / "model" / f"{key}.json"
    cached = read_json(cache_path)
    if cached:
        return cached["response"], {"cache_key": key, "cache_hit": True, "cost_cny": 0.0}
    prompt = instruction + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False)
    estimated_input = max(1, len(SYSTEM_PROMPT + prompt) // 2)
    estimated_output = 6000
    reserve = _cost(settings, estimated_input, estimated_output)
    if remaining_cost_cny is not None and reserve > remaining_cost_cny:
        raise RuntimeError(
            f"Model call reserve {reserve:.6f} CNY exceeds remaining limit {remaining_cost_cny:.6f} CNY"
        )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response, usage = client.complete(prompt)
            actual = _cost(
                settings,
                usage["input_tokens"],
                usage["output_tokens"],
                usage.get("cached_input_tokens", 0),
            )
            record = {
                "schema_version": "codex-history-model-cache-v1",
                "created_at": utc_now(),
                "cache_key": key,
                "stage": stage,
                "stage_version": SUMMARY_STAGE_VERSION,
                "model": settings.model,
                "response": response,
                "usage": usage,
                "cost_cny": actual,
            }
            atomic_write_json(cache_path, record)
            return response, {
                "cache_key": key,
                "cache_hit": False,
                "attempts": attempt,
                "cost_cny": actual,
                **usage,
            }
        except Exception as error:
            last_error = error
            if attempt < 3:
                time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) + random.random() * 0.2)
    assert last_error is not None
    raise last_error


def _scope_records(connection: sqlite3.Connection, scope_id: str) -> list[dict[str, Any]]:
    scope = connection.execute("SELECT * FROM scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if scope is None:
        return []
    if scope["scope_type"] == "thread":
        rows = connection.execute(
            "SELECT record_id,text,status_group,category,occurred_start_at,occurred_end_at "
            "FROM knowledge WHERE scope_id=? AND tier='fact_block' ORDER BY occurred_start_at,record_id",
            (scope_id,),
        )
    else:
        thread_ids = json.loads(scope["thread_ids_json"])
        if not thread_ids:
            return []
        placeholders = ",".join("?" for _ in thread_ids)
        rows = connection.execute(
            f"SELECT record_id,text,status_group,category,occurred_start_at,occurred_end_at "
            f"FROM knowledge WHERE scope_id IN ({placeholders}) AND tier='overview' "
            "ORDER BY occurred_start_at,record_id",
            thread_ids,
        )
    return [dict(row) for row in rows]


def _normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    overview = str(result.get("overview") or "").strip()
    claims = result.get("claims") if isinstance(result.get("claims"), list) else []
    assets = result.get("assets") if isinstance(result.get("assets"), list) else []
    if not overview and claims:
        overview = "\n\n".join(str(item.get("text") or "") for item in claims if item.get("text"))
    if not overview:
        raise ValueError("Model summary contains no overview")
    return {"overview": overview, "claims": claims, "assets": assets}


def summarize_scopes(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    *,
    scope_ids: list[str],
    max_cost_cny: float | None,
    client: ChatClient | None = None,
) -> dict[str, Any]:
    resolution = resolve_summarization(config)
    if resolution["effective_mode"] == "extractive":
        return {
            "enabled": False,
            "mode": config.summary_mode,
            "effective_mode": "extractive",
            "fallback": bool(resolution["fallback"]),
            "fallback_reason": resolution["fallback_reason"],
            "scopes": 0,
            "cost_cny": 0.0,
        }
    settings = ChatSettings.from_config(config)
    client = client or ChatClient(settings)
    total_cost = 0.0
    calls = 0
    cache_hits = 0
    summarized = 0
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    details: list[dict[str, Any]] = []
    for scope_id in scope_ids:
        records = _scope_records(connection, scope_id)
        if not records:
            continue
        ledgers: list[dict[str, Any]] = []
        call_meta: list[dict[str, Any]] = []
        chunks = list(_chunks(records))
        if len(chunks) == 1:
            writer_input: Any = {"scope_id": scope_id, "records": records}
        else:
            for index, chunk in enumerate(chunks):
                remaining = None if max_cost_cny is None else max_cost_cny - total_cost
                ledger, meta = _call_cached(
                    config,
                    client,
                    settings,
                    stage=f"condenser-{index}",
                    instruction=CONDENSER_PROMPT,
                    payload={"scope_id": scope_id, "records": chunk},
                    remaining_cost_cny=remaining,
                )
                ledgers.append(ledger)
                call_meta.append(meta)
                total_cost += float(meta["cost_cny"])
                calls += int(not meta["cache_hit"])
                cache_hits += int(meta["cache_hit"])
                input_tokens += int(meta.get("input_tokens", 0))
                cached_input_tokens += int(meta.get("cached_input_tokens", 0))
                output_tokens += int(meta.get("output_tokens", 0))
            writer_input = {"scope_id": scope_id, "condensed_ledgers": ledgers}
        remaining = None if max_cost_cny is None else max_cost_cny - total_cost
        result, meta = _call_cached(
            config,
            client,
            settings,
            stage="writer",
            instruction=WRITER_PROMPT,
            payload=writer_input,
            remaining_cost_cny=remaining,
        )
        call_meta.append(meta)
        total_cost += float(meta["cost_cny"])
        calls += int(not meta["cache_hit"])
        cache_hits += int(meta["cache_hit"])
        input_tokens += int(meta.get("input_tokens", 0))
        cached_input_tokens += int(meta.get("cached_input_tokens", 0))
        output_tokens += int(meta.get("output_tokens", 0))
        normalized = _normalize_result(result)
        applied = apply_model_scope_summary(
            connection,
            scope_id=scope_id,
            overview=normalized["overview"],
            claims=normalized["claims"],
            assets=normalized["assets"],
            model=settings.model,
            cache_key=meta["cache_key"],
        )
        details.append({"scope_id": scope_id, "records": len(records), "calls": call_meta, **applied})
        summarized += 1
    return {
        "enabled": True,
        "mode": config.summary_mode,
        "effective_mode": "openai-compatible",
        "model": settings.model,
        "scopes": summarized,
        "api_calls": calls,
        "cache_hits": cache_hits,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": max(0, input_tokens - cached_input_tokens),
        "output_tokens": output_tokens,
        "cost_cny": round(total_cost, 6),
        "details": details,
    }
