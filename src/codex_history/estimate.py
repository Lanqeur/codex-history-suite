from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import ProfileConfig
from .schema import connect


_BASE64_MARKER = b";base64,"
_BASE64_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n")


def _human_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} TiB"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_file():
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _base64_payload_bytes(path: Path, *, start: int, size: int) -> tuple[int, int]:
    payload_bytes = 0
    bytes_read = 0
    marker_index = 0
    in_payload = False
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = size
            while remaining > 0:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                bytes_read += len(block)
                for value in block:
                    if in_payload:
                        if value in _BASE64_BYTES:
                            payload_bytes += 1
                            continue
                        in_payload = False
                    if value == _BASE64_MARKER[marker_index]:
                        marker_index += 1
                        if marker_index == len(_BASE64_MARKER):
                            marker_index = 0
                            in_payload = True
                    else:
                        marker_index = 1 if value == _BASE64_MARKER[0] else 0
    except OSError:
        return 0, 0
    return payload_bytes, bytes_read


def _estimate_processed_content(changes: Iterable[Any]) -> dict[str, int]:
    total = 0
    scanned = 0
    base64_payload = 0
    for change in changes:
        if not change.source or change.kind not in {"added", "rewritten", "appended"}:
            continue
        start = 0
        size = int(change.source.size_bytes)
        if change.kind == "appended" and change.previous:
            start = int(change.previous.get("size_bytes", 0))
            size = max(0, size - start)
        total += size
        excluded, observed = _base64_payload_bytes(change.source.path, start=start, size=size)
        base64_payload += excluded
        scanned += observed
    return {
        "processed_bytes": total,
        "scanned_bytes": scanned,
        "inline_base64_payload_bytes": base64_payload,
        "model_relevant_bytes": max(0, total - base64_payload),
    }


def _calibration(
    config: ProfileConfig,
    database: Path | None,
) -> dict[str, Any]:
    if not database or not database.is_file():
        return {"basis": "configured-defaults"}
    connection = connect(database, readonly=True)
    try:
        source_bytes = int(
            connection.execute("SELECT COALESCE(SUM(size_bytes),0) FROM source_files").fetchone()[0]
        )
        artifact_bytes = int(
            connection.execute("SELECT COALESCE(SUM(size_bytes),0) FROM artifact_files").fetchone()[0]
        )
        fact_chars = int(
            connection.execute(
                "SELECT COALESCE(SUM(length(text)),0) FROM knowledge WHERE tier='fact_block'"
            ).fetchone()[0]
        )
        overview_chars = int(
            connection.execute(
                "SELECT COALESCE(SUM(length(text)),0) FROM knowledge WHERE tier='overview'"
            ).fetchone()[0]
        )
        scope_count = int(connection.execute("SELECT COUNT(*) FROM scopes").fetchone()[0])
    except sqlite3.Error:
        return {"basis": "configured-defaults"}
    finally:
        connection.close()
    if source_bytes <= 0:
        return {"basis": "configured-defaults"}

    bytes_per_token = max(0.5, config.estimate_bytes_per_token)
    raw_tokens = source_bytes / bytes_per_token
    observed_summary_tokens = (fact_chars * 1.12 + overview_chars) / 2 + scope_count * 500
    return {
        "basis": "active-profile-calibration",
        "source_bytes": source_bytes,
        "artifact_bytes": artifact_bytes,
        "summary_input_ratio": _clamp(observed_summary_tokens / max(1, raw_tokens), 0.03, 1.5),
        "sqlite_to_source_ratio": _clamp(database.stat().st_size / source_bytes, 0.01, 2.0),
        "artifact_to_source_ratio": _clamp(artifact_bytes / source_bytes, 0.0, 3.0),
        "semantic_to_source_ratio": _clamp(
            _directory_bytes(config.root / "semantic/chroma") / source_bytes,
            0.0,
            2.0,
        ),
    }


def estimate_build(
    config: ProfileConfig,
    *,
    sources: list[Any],
    changes: list[Any],
    database: Path | None,
    summarization: dict[str, object],
) -> dict[str, Any]:
    total_source_bytes = sum(int(source.size_bytes) for source in sources)
    actionable_source_bytes = sum(
        int(change.source.size_bytes)
        if change.source
        else int((change.previous or {}).get("size_bytes", 0))
        for change in changes
        if change.kind != "unchanged"
    )
    processed_content = _estimate_processed_content(changes)
    processed_bytes = processed_content["processed_bytes"]
    calibration = _calibration(config, database)
    bytes_per_token = max(0.5, config.estimate_bytes_per_token)
    raw_token_equivalent = int(processed_content["model_relevant_bytes"] / bytes_per_token)
    summary_ratio = float(
        calibration.get("summary_input_ratio", config.estimate_summary_input_ratio)
    )
    expected_input = int(raw_token_equivalent * summary_ratio)
    if processed_bytes:
        expected_input += max(1, sum(change.kind != "unchanged" for change in changes)) * 500
    low_input = int(expected_input * 0.65)
    high_input = int(expected_input * 1.65)
    output_ratio = max(0.0, config.estimate_summary_output_ratio)
    expected_output = int(expected_input * output_ratio)
    high_output = int(high_input * max(output_ratio, 0.12))
    cache_ratio = _clamp(config.estimate_cached_input_ratio, 0.0, 1.0)
    cached_input = int(expected_input * cache_ratio)
    uncached_input = max(0, expected_input - cached_input)

    expected_model_cost = (
        uncached_input / 1_000_000 * config.summary_input_price_cny
        + cached_input / 1_000_000 * config.summary_cached_input_price_cny
        + expected_output / 1_000_000 * config.summary_output_price_cny
    )
    upper_model_cost = (
        high_input / 1_000_000 * config.summary_input_price_cny
        + high_output / 1_000_000 * config.summary_output_price_cny
    )
    model_enabled = summarization.get("effective_mode") == "openai-compatible"

    embedding_tokens = int(processed_bytes / 2) if config.embedding_enabled else 0
    embedding_cost = embedding_tokens / 1_000_000 * config.embedding_input_price_cny
    effective_expected_cost = (expected_model_cost if model_enabled else 0.0) + embedding_cost
    effective_upper_cost = (upper_model_cost if model_enabled else 0.0) + embedding_cost

    sqlite_ratio = float(
        calibration.get("sqlite_to_source_ratio", config.estimate_sqlite_to_source_ratio)
    )
    artifact_ratio = float(
        calibration.get("artifact_to_source_ratio", config.estimate_artifact_to_source_ratio)
    )
    semantic_ratio = (
        float(calibration.get("semantic_to_source_ratio", config.estimate_semantic_to_source_ratio))
        if config.embedding_enabled
        else 0.0
    )
    snapshot_expected = total_source_bytes
    sqlite_expected = int(total_source_bytes * sqlite_ratio)
    inline_artifact_upper = int(
        processed_content["inline_base64_payload_bytes"] * 0.75
    )
    artifact_expected = int(total_source_bytes * artifact_ratio)
    if calibration["basis"] == "configured-defaults":
        artifact_expected = max(artifact_expected, int(inline_artifact_upper * 0.35))
    artifact_upper = max(int(artifact_expected * 2.50), inline_artifact_upper)
    semantic_expected = int(total_source_bytes * semantic_ratio)
    existing_model_cache = _directory_bytes(config.cache_dir / "model")
    model_cache_expected = existing_model_cache + (
        int(expected_output * 6) if model_enabled else 0
    )
    storage_expected = (
        snapshot_expected
        + sqlite_expected
        + artifact_expected
        + semantic_expected
        + model_cache_expected
    )
    storage_low = (
        int(snapshot_expected * 0.70)
        + int(sqlite_expected * 0.65)
        + int(artifact_expected * 0.25)
        + int(semantic_expected * 0.65)
        + int(model_cache_expected * 0.65)
    )
    storage_high = (
        snapshot_expected
        + int(sqlite_expected * 1.60)
        + artifact_upper
        + int(semantic_expected * 1.60)
        + int(model_cache_expected * 1.80)
    )

    return {
        "source": {
            "transcript_count": len(sources),
            "total_bytes": total_source_bytes,
            "total_human": _human_bytes(total_source_bytes),
            "actionable_source_bytes": actionable_source_bytes,
            "new_or_reprocessed_bytes": processed_bytes,
            "new_or_reprocessed_human": _human_bytes(processed_bytes),
            "bytes_scanned_for_inline_payloads": processed_content["scanned_bytes"],
            "inline_base64_payload_bytes_excluded_from_model_estimate": processed_content[
                "inline_base64_payload_bytes"
            ],
            "model_relevant_bytes": processed_content["model_relevant_bytes"],
            "raw_token_equivalent": raw_token_equivalent,
        },
        "tokens": {
            "summary": {
                "would_call_model": model_enabled,
                "input_low": low_input,
                "input_expected": expected_input,
                "input_upper": high_input,
                "cached_input_expected": cached_input,
                "uncached_input_expected": uncached_input,
                "output_expected": expected_output,
                "output_upper": high_output,
            },
            "embedding_input_upper": embedding_tokens,
            "api_total_expected": (
                (expected_input + expected_output) if model_enabled else 0
            )
            + embedding_tokens,
            "api_total_upper": ((high_input + high_output) if model_enabled else 0)
            + embedding_tokens,
        },
        "pricing_cny_per_million": {
            "summary_input": config.summary_input_price_cny,
            "summary_cached_input": config.summary_cached_input_price_cny,
            "summary_output": config.summary_output_price_cny,
            "embedding_input": config.embedding_input_price_cny,
        },
        "cost_cny": {
            "summary_expected": round(expected_model_cost if model_enabled else 0.0, 6),
            "summary_upper_no_cache": round(upper_model_cost if model_enabled else 0.0, 6),
            "summary_if_model_enabled_expected": round(expected_model_cost, 6),
            "summary_if_model_enabled_upper_no_cache": round(upper_model_cost, 6),
            "embedding_upper": round(embedding_cost, 6),
            "total_expected": round(effective_expected_cost, 6),
            "total_upper": round(effective_upper_cost, 6),
            "model_cache_note": "Exact Codex History model-cache hits cost zero and can only reduce this estimate.",
        },
        "storage": {
            "definition": (
                "Projected managed footprint for the current source state; excludes original transcripts "
                "and retained obsolete builds."
            ),
            "expected_bytes": storage_expected,
            "expected_human": _human_bytes(storage_expected),
            "low_bytes": storage_low,
            "low_human": _human_bytes(storage_low),
            "upper_bytes": storage_high,
            "upper_human": _human_bytes(storage_high),
            "components_expected_bytes": {
                "content_addressed_snapshots": snapshot_expected,
                "sqlite_active_build": sqlite_expected,
                "artifact_cas": artifact_expected,
                "semantic_index": semantic_expected,
                "model_response_cache": model_cache_expected,
            },
            "existing_model_response_cache_bytes": existing_model_cache,
            "external_path_capture_note": (
                "Existing files referenced by transcripts can exceed this range when "
                "artifacts.capture_existing_paths is enabled."
            ),
            "inline_artifact_decoded_upper_bytes": inline_artifact_upper,
        },
        "assumptions": {
            "basis": calibration["basis"],
            "bytes_per_token": bytes_per_token,
            "summary_input_ratio": round(summary_ratio, 6),
            "summary_output_ratio": output_ratio,
            "expected_provider_cache_ratio": cache_ratio,
            "sqlite_to_source_ratio": round(sqlite_ratio, 6),
            "artifact_to_source_ratio": round(artifact_ratio, 6),
            "semantic_to_source_ratio": round(semantic_ratio, 6),
            "confidence": "planning range, not a provider invoice or exact disk reservation",
            "inline_payload_note": (
                "Inline data-URI base64 payload bytes are scanned and excluded from model-token "
                "estimates, but remain included in snapshot storage."
            ),
        },
    }


def actual_managed_storage(config: ProfileConfig, database: Path) -> dict[str, Any]:
    components = {
        "active_sqlite_build": database.stat().st_size if database.is_file() else 0,
        "content_addressed_snapshots": _directory_bytes(config.snapshots_dir),
        "artifact_cas": _directory_bytes(config.cas_dir),
        "semantic_index": _directory_bytes(config.root / "semantic"),
        "model_response_cache": _directory_bytes(config.cache_dir / "model"),
    }
    core_total = sum(components.values())
    profile_total = _directory_bytes(config.root)
    return {
        "core_components_bytes": components,
        "core_total_bytes": core_total,
        "core_total_human": _human_bytes(core_total),
        "profile_total_bytes": profile_total,
        "profile_total_human": _human_bytes(profile_total),
        "profile_total_note": "Includes retained builds, reports, run state, and shared stores.",
    }
